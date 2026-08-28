"""
Bot Flag POC — automated upload to Adobe Analytics Classifications API 2.0

Takes the CSVs produced by process_bot_flags.py (output/bot_flag_upload*.csv,
columns Key,Bot Flag) and uploads them to the eVar23 classification dataset
using the Classifications API 2.0 file-import flow:

    1. createApiJob   -> creates the import job and returns api_job_id
    2. uploadFile     -> uploads the CSV (multipart) tied to the api_job_id
    3. commitApiJob   -> confirms the import
    4. (polling)      -> checks the status until it finishes

For many parts, the work is done in TWO parallel phases instead of processing
each file sequentially and blocking:
    Phase 1: create+upload+commit of all files in parallel
             (bounded by config.UPLOAD_CONCURRENCY).
    Phase 2: parallel polling of all committed jobs
             (bounded by config.POLL_CONCURRENCY).
This way the total time is bounded by the slowest phase, not by the sum of each
job's processing time. With --no-wait only phase 1 runs (fire-and-forget). On
HTTP 429 (API 2.0 rate limit, ~120 req/min per user) or 5xx, each request
retries with exponential backoff.

Authentication: OAuth Server-to-Server (client credentials) against Adobe IMS.
JWT is deprecated; this script uses the current flow.

--- SECURITY ---
Credentials are read EXCLUSIVELY from environment variables; they are never
written in the repo or printed. ECIDs are visitor data (PII): row contents are
not logged. TLS is always verified.

Environment variables (see README):
    ADOBE_CLIENT_ID      (required)
    ADOBE_CLIENT_SECRET  (required)
    ADOBE_DATASET_ID     (required)
    ADOBE_RSID           (optional; used by --list-datasets to find the
                          DATASET_ID. Can also be passed with --rsid)
    ADOBE_COMPANY_ID     (optional; if missing, discovered via Discovery API)
    ADOBE_SCOPES         (optional; default in config.DEFAULT_SCOPES)

Usage:
    export ADOBE_CLIENT_ID=...       # never put it in the code
    export ADOBE_CLIENT_SECRET=...
    export ADOBE_DATASET_ID=...

    # Upload all CSVs in output/:
    python upload_classifications.py

    # Discover the DATASET_ID of your classifications (one time only).
    # Requires the report suite ID (RSID), via --rsid or ADOBE_RSID:
    python upload_classifications.py --list-datasets --rsid your_report_suite

    # Upload a specific file:
    python upload_classifications.py --file output/bot_flag_upload_part2.csv
"""

import argparse
import glob
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import config

# Load .env if python-dotenv is installed (optional). If it is not, the
# script still works with variables already exported in the environment.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(config.BASE_DIR, ".env"))
except ImportError:
    pass


class ConfigError(Exception):
    """Missing configuration (credentials/env) needed to operate."""


class UploadError(Exception):
    """Error during the import flow against the API."""


# ------------------------------------------------------------------
# Credentials and authentication
# ------------------------------------------------------------------
def _require_env(name):
    """Read a required environment variable without exposing its value."""
    value = os.environ.get(name)
    if not value:
        raise ConfigError(
            f"Missing environment variable {name}. Export it before running "
            f"the script (do not write it in the code or in the repo)."
        )
    return value


def get_access_token():
    """
    Get an access token from Adobe IMS using the client_credentials flow.
    Does not print or return the client_secret; the token is not logged.
    """
    client_id = _require_env("ADOBE_CLIENT_ID")
    client_secret = _require_env("ADOBE_CLIENT_SECRET")
    scopes = os.environ.get("ADOBE_SCOPES", config.DEFAULT_SCOPES)

    resp = requests.post(
        config.IMS_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scopes,
        },
        timeout=config.HTTP_TIMEOUT,
        # verify=True is the requests default; TLS is always validated.
    )
    if resp.status_code != 200:
        # Generic message: we do not include the body in case it has sensitive details.
        raise UploadError(
            f"Could not obtain the IMS access token (HTTP {resp.status_code}). "
            f"Check ADOBE_CLIENT_ID / ADOBE_CLIENT_SECRET / ADOBE_SCOPES."
        )
    token = resp.json().get("access_token")
    if not token:
        raise UploadError("The IMS response did not include an access_token.")
    return client_id, token


def _auth_headers(client_id, token, content_type="application/json"):
    headers = {
        "x-api-key": client_id,
        "Authorization": f"Bearer {token}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _request_with_retry(method, url, **kwargs):
    """
    Execute a request retrying on rate limit (HTTP 429) and transient server
    errors (5xx), with exponential backoff + jitter.

    Returns the Response when the status is NOT 429/5xx (the caller decides
    whether that status is acceptable). Raises UploadError only if the retries
    against 429/5xx are exhausted, or requests.RequestException on repeated
    network failures.

    It is safe for concurrent use: it shares no mutable state.
    """
    last_exc = None
    for attempt in range(1, config.RETRY_MAX_ATTEMPTS + 1):
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            # Network failure: retry unless this is the last attempt.
            last_exc = exc
            if attempt == config.RETRY_MAX_ATTEMPTS:
                raise
            _sleep_backoff(attempt)
            continue

        # 429 = rate limit; 5xx = transient server error -> retry.
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt == config.RETRY_MAX_ATTEMPTS:
                raise UploadError(
                    f"The API responded {resp.status_code} after "
                    f"{config.RETRY_MAX_ATTEMPTS} attempts on {method} {_safe_path(url)}. "
                    f"It may be rate limiting (429); lower UPLOAD_CONCURRENCY/POLL_CONCURRENCY."
                )
            # Respect Retry-After if present; otherwise exponential backoff.
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), config.RETRY_BACKOFF_MAX))
            else:
                _sleep_backoff(attempt)
            continue

        return resp

    # Should not be reached, but just in case:
    if last_exc:
        raise last_exc
    raise UploadError(f"Could not complete {method} {_safe_path(url)}.")


def _sleep_backoff(attempt):
    """Bounded exponential backoff with jitter to avoid synchronizing threads."""
    delay = min(
        config.RETRY_BACKOFF_BASE * (2 ** (attempt - 1)),
        config.RETRY_BACKOFF_MAX,
    )
    time.sleep(delay + random.uniform(0, 0.5))


def _safe_path(url):
    """Return only the URL path for logs (avoids leaking query/host)."""
    return url.split("//", 1)[-1].split("/", 1)[-1].split("?", 1)[0]


# ------------------------------------------------------------------
# Company id and dataset discovery
# ------------------------------------------------------------------
def get_company_id(client_id, token):
    """Return the global company id (from env, or via Discovery API)."""
    env_company = os.environ.get("ADOBE_COMPANY_ID")
    if env_company:
        return env_company

    resp = _request_with_retry(
        "GET",
        config.DISCOVERY_URL,
        headers=_auth_headers(client_id, token, content_type=None),
        timeout=config.HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise UploadError(
            f"Discovery API failed (HTTP {resp.status_code}). Set ADOBE_COMPANY_ID "
            f"manually to skip this step."
        )
    data = resp.json()
    for org in data.get("imsOrgs", []):
        for company in org.get("companies", []):
            gid = company.get("globalCompanyId")
            if gid:
                return gid
    raise UploadError(
        "globalCompanyId not found in the Discovery response. "
        "Set ADOBE_COMPANY_ID manually."
    )


def _api_base(company_id):
    return f"{config.ANALYTICS_API_HOST}/api/{company_id}"


def list_datasets(client_id, token, company_id, rsid):
    """
    List the classification datasets of a report suite (RSID) to help find
    the DATASET_ID.

    The Classifications API 2.0 does not expose a global dataset listing: the
    endpoint requires the report suite ID and returns, for each dimension
    (evar/prop), the associated dataset IDs.
        GET /api/{company}/classifications/datasets/compatibilityMetrics/{rsid}
    """
    url = f"{_api_base(company_id)}/classifications/datasets/compatibilityMetrics/{rsid}"
    resp = _request_with_retry(
        "GET",
        url,
        headers=_auth_headers(client_id, token, content_type=None),
        timeout=config.HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise UploadError(
            f"Could not list the datasets of report suite '{rsid}' "
            f"(HTTP {resp.status_code}). Check that the RSID is correct and that "
            f"the project has access to that report suite."
        )
    return resp.json()


# ------------------------------------------------------------------
# File-import flow
# ------------------------------------------------------------------
def create_job(client_id, token, company_id, dataset_id, job_name):
    url = f"{_api_base(company_id)}/classifications/job/import/createApiJob/{dataset_id}"
    body = {
        "dataFormat": config.UPLOAD_DATA_FORMAT,
        "encoding": config.UPLOAD_ENCODING,
        "jobName": job_name,
        "listDelimiter": config.UPLOAD_LIST_DELIMITER,
        "source": "bot-flag-poc automated upload",
    }
    resp = _request_with_retry(
        "POST",
        url,
        headers=_auth_headers(client_id, token),
        json=body,
        timeout=config.HTTP_TIMEOUT,
    )
    if resp.status_code not in (200, 201):
        raise UploadError(
            f"createApiJob failed for dataset {dataset_id} (HTTP {resp.status_code})."
        )
    data = resp.json()
    job_id = data.get("api_job_id") or data.get("apiJobId") or data.get("jobId")
    if not job_id:
        raise UploadError(f"createApiJob did not return an api_job_id. Response: {data}")
    return job_id


def upload_file(client_id, token, company_id, api_job_id, filepath):
    url = f"{_api_base(company_id)}/classifications/job/import/uploadFile/{api_job_id}"
    # requests sets the multipart Content-Type with the boundary; we do not force it.
    headers = _auth_headers(client_id, token, content_type=None)
    filename = os.path.basename(filepath)
    # Read the content into bytes once: if there is a retry on 429/5xx,
    # requests rebuilds the multipart from scratch (an already-consumed file
    # handle would not be rewound between attempts).
    with open(filepath, "rb") as fh:
        content = fh.read()
    files = {"file": (filename, content, "text/csv")}
    resp = _request_with_retry(
        "POST",
        url,
        headers=headers,
        files=files,
        timeout=config.HTTP_TIMEOUT,
    )
    if resp.status_code not in (200, 201, 202):
        raise UploadError(
            f"uploadFile failed for job {api_job_id} (HTTP {resp.status_code})."
        )
    return True


def commit_job(client_id, token, company_id, api_job_id):
    url = f"{_api_base(company_id)}/classifications/job/import/commitApiJob/{api_job_id}"
    resp = _request_with_retry(
        "POST",
        url,
        headers=_auth_headers(client_id, token),
        timeout=config.HTTP_TIMEOUT,
    )
    if resp.status_code not in (200, 201, 202):
        raise UploadError(
            f"commitApiJob failed for job {api_job_id} (HTTP {resp.status_code})."
        )
    data = resp.json() if resp.content else {}
    return data.get("import_job_id") or data.get("jobId") or api_job_id


def check_status(client_id, token, company_id, job_id):
    """
    Check the job status ONCE and normalize it.

    Returns one of: "completed", "failed" or "pending". It does not block or
    sleep; polling (with its waits) is orchestrated by poll_all_jobs.
    """
    url = f"{_api_base(company_id)}/classifications/job/{job_id}"
    headers = _auth_headers(client_id, token, content_type=None)
    resp = _request_with_retry("GET", url, headers=headers, timeout=config.HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise UploadError(
            f"Status check failed for job {job_id} (HTTP {resp.status_code})."
        )
    data = resp.json()
    status = (data.get("status") or data.get("state") or "").lower()
    if status in ("completed", "success", "done", "finished"):
        return "completed"
    if status in ("failed", "error", "cancelled", "canceled"):
        return "failed"
    return "pending"


# ------------------------------------------------------------------
# PHASE 1 — upload and commit (create + upload + commit) in parallel
# ------------------------------------------------------------------
def submit_one(client_id, token, company_id, dataset_id, filepath):
    """
    Create the job, upload the file and commit for ONE file. Does not wait for
    processing: it returns the committed job_id so the polling phase can check
    it later. Intended to run concurrently.
    """
    filename = os.path.basename(filepath)
    api_job_id = create_job(
        client_id, token, company_id, dataset_id, job_name=f"bot-flag {filename}"
    )
    upload_file(client_id, token, company_id, api_job_id, filepath)
    committed_id = commit_job(client_id, token, company_id, api_job_id)
    return {"file": filename, "job_id": committed_id}


def submit_all(client_id, token, company_id, dataset_id, files):
    """
    Upload and commit all files in parallel (bounded by UPLOAD_CONCURRENCY).
    Returns (submitted, submit_failures):
      - submitted: [{"file", "job_id"}] of the ones that reached commit
      - submit_failures: [{"file", "error"}] of the ones that failed to upload
    """
    submitted = []
    submit_failures = []
    workers = max(1, config.UPLOAD_CONCURRENCY)
    print(f"\nPhase 1/2: uploading {len(files)} file(s) (concurrency {workers})...")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_file = {
            pool.submit(
                submit_one, client_id, token, company_id, dataset_id, fp
            ): fp
            for fp in files
        }
        for future in as_completed(future_to_file):
            filename = os.path.basename(future_to_file[future])
            try:
                result = future.result()
                submitted.append(result)
                print(f"   uploaded and committed: {filename} (job {result['job_id']})")
            except (UploadError, requests.RequestException, OSError) as exc:
                submit_failures.append({"file": filename, "error": str(exc)})
                print(f"   UPLOAD FAILED: {filename} ({exc})", file=sys.stderr)

    return submitted, submit_failures


# ------------------------------------------------------------------
# PHASE 2 — poll all jobs in parallel until they finish
# ------------------------------------------------------------------
def poll_all_jobs(client_id, token, company_id, submitted):
    """
    Poll all submitted jobs until they finish (completed/failed) or
    STATUS_POLL_MAX_ATTEMPTS is exhausted. Each round checks the still-pending
    jobs in parallel (bounded by POLL_CONCURRENCY) and then sleeps
    STATUS_POLL_INTERVAL before the next round.

    Returns a dict {job_id: "completed"|"failed"|"timeout"}.
    """
    workers = max(1, config.POLL_CONCURRENCY)
    pending = {s["job_id"] for s in submitted}
    results = {}
    print(
        f"\nPhase 2/2: waiting for processing of {len(pending)} job(s) "
        f"(concurrency {workers})..."
    )

    for attempt in range(1, config.STATUS_POLL_MAX_ATTEMPTS + 1):
        if not pending:
            break
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_job = {
                pool.submit(
                    check_status, client_id, token, company_id, job_id
                ): job_id
                for job_id in pending
            }
            for future in as_completed(future_to_job):
                job_id = future_to_job[future]
                try:
                    state = future.result()
                except (UploadError, requests.RequestException) as exc:
                    # Error while checking: leave it pending to retry on the
                    # next round, unless this is the last one.
                    print(
                        f"   warning: could not check job {job_id} ({exc})",
                        file=sys.stderr,
                    )
                    continue
                if state in ("completed", "failed"):
                    results[job_id] = state

        pending = {j for j in pending if j not in results}
        if pending and attempt < config.STATUS_POLL_MAX_ATTEMPTS:
            print(f"   {len(pending)} job(s) still processing...")
            time.sleep(config.STATUS_POLL_INTERVAL)

    # Whatever is still pending when attempts run out is marked as timeout.
    for job_id in pending:
        results[job_id] = "timeout"
    return results


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def find_output_csvs():
    pattern = os.path.join(config.OUTPUT_FOLDER, "bot_flag_upload*.csv")
    return sorted(glob.glob(pattern))


def main():
    parser = argparse.ArgumentParser(
        description="Upload the classification CSVs to Adobe Analytics (API 2.0)."
    )
    parser.add_argument(
        "--file",
        help="Path to a specific CSV. If omitted, uploads all files in output/.",
    )
    parser.add_argument(
        "--list-datasets",
        action="store_true",
        help="List the classification datasets (to find the DATASET_ID) and exit.",
    )
    parser.add_argument(
        "--rsid",
        help="Report suite ID to query with --list-datasets. If omitted, the "
        "ADOBE_RSID environment variable is used.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Upload and commit everything, print the job IDs and exit WITHOUT "
        "waiting for processing (fire-and-forget). Check the status later.",
    )
    args = parser.parse_args()

    try:
        client_id, token = get_access_token()
        company_id = get_company_id(client_id, token)
        print(f"Authenticated. Global company id: {company_id}")

        if args.list_datasets:
            rsid = args.rsid or os.environ.get("ADOBE_RSID")
            if not rsid:
                raise ConfigError(
                    "To list datasets you must provide the report suite ID: "
                    "pass --rsid <RSID> or set the ADOBE_RSID environment variable."
                )
            datasets = list_datasets(client_id, token, company_id, rsid)
            print(f"\nClassification datasets for report suite '{rsid}':")
            print(datasets)
            return 0

        dataset_id = _require_env("ADOBE_DATASET_ID")

        if args.file:
            files = [args.file]
        else:
            files = find_output_csvs()

        if not files:
            print(
                "No CSVs found to upload. Run process_bot_flags.py first "
                "or pass --file.",
                file=sys.stderr,
            )
            return 1

        # Filter out the ones that do not exist before starting the phases.
        existing = []
        for filepath in files:
            if not os.path.isfile(filepath):
                print(f"   skipped (does not exist): {filepath}", file=sys.stderr)
                continue
            existing.append(filepath)

        if not existing:
            print("No valid files to upload.", file=sys.stderr)
            return 1

        print(f"Files to upload: {len(existing)} (dataset {dataset_id})")

        # --- Phase 1: upload and commit everything in parallel ---
        submitted, submit_failures = submit_all(
            client_id, token, company_id, dataset_id, existing
        )

        # Fire-and-forget: we do not wait for processing on Adobe's side.
        if args.no_wait:
            print("\n" + "=" * 60)
            print("UPLOAD SUMMARY (not waiting for processing)")
            print("=" * 60)
            for s in submitted:
                print(f"  {s['file']:35s} submitted  job={s['job_id']}")
            for f in submit_failures:
                print(f"  {f['file']:35s} UPLOAD FAILED", file=sys.stderr)
            print(
                f"\n{len(submitted)} submitted, {len(submit_failures)} with upload errors."
            )
            return 1 if submit_failures else 0

        # --- Phase 2: parallel polling of all committed jobs ---
        job_states = {}
        if submitted:
            job_states = poll_all_jobs(client_id, token, company_id, submitted)

        print("\n" + "=" * 60)
        print("UPLOAD SUMMARY")
        print("=" * 60)
        failures = 0
        for s in submitted:
            state = job_states.get(s["job_id"], "unknown")
            print(f"  {s['file']:35s} {state:11s} job={s['job_id']}")
            if state != "completed":
                failures += 1
        for f in submit_failures:
            print(f"  {f['file']:35s} upload-error", file=sys.stderr)
            failures += 1
        print(
            f"\nTotal: {len(existing)} | ok: {len(existing) - failures} | "
            f"with problems: {failures}"
        )
        return 1 if failures else 0

    except (ConfigError, UploadError) as exc:
        # Fail-closed: clear message, without dumping secrets or stack traces.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except requests.RequestException as exc:
        print(f"NETWORK ERROR: {exc.__class__.__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
