# Bot Flag POC — Data Warehouse → Classification Upload

Processes the 3 Adobe Data Warehouse exports (one per bot confidence level)
and produces a single CSV ready to upload as an eVar23 (ECID)
**Classification** in Adobe Analytics.

## What it does

1. Reads the 3 files (`.zip` or `.csv`) from the `input/` folder.
2. Detects and removes rows with an empty ECID (all zeros) — otherwise
   every visitor without a real ECID would be labeled as a bot.
3. Reconciles ECIDs that appear in more than one tier: the highest level
   wins (`high` > `medium` > `low`).
4. Produces a final CSV with columns `Key`, `Bot Flag`, split into parts
   if it exceeds the configured maximum size.
5. Prints a summary of rows read, duplicates resolved and final count.

## Project structure

```
bot-flag-poc/
├── config.py              # all adjustable parameters
├── process_bot_flags.py   # processing logic
├── requirements.txt
├── input/                 # place the 3 Data Warehouse files here
└── output/                # the final CSV is written here
```

## Input file requirements

- File names starting with `Bots_Tier_1`, `Bots_Tier_2` and
  `Bots_Tier_3` (mapped to `high`, `medium`, `low` respectively —
  adjustable in `config.py`).
- Format: CSV with headers in the first row, a
  `Marketing Cloud Visitor ID` column with the ECID (name adjustable in `config.py`).
- May be compressed as `.zip` or a plain `.csv`.

## Installation

```bash
git clone <repo-url>
cd bot-flag-poc
python -m venv venv
source venv/bin/activate   # on Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

1. Copy the 3 Data Warehouse files into `input/`.
2. Run:

```bash
python process_bot_flags.py
```

3. The result is written to `output/bot_flag_upload.csv` (or
   `bot_flag_upload_part1.csv`, `part2.csv`... if the file is large).

## Configuration

All case-specific parameters live in `config.py`: column names,
file → confidence level mapping, maximum output file size, read chunk
size. You do not need to touch `process_bot_flags.py` to adjust these
values.

## Important — sensitive data

This repository must **not** contain real ECID data. The `input/` and
`output/` folders are excluded in `.gitignore` (only the folder
structure is versioned via `.gitkeep`). Do not remove that rule from
`.gitignore` when cloning/replicating the project on another machine.

## Next step — upload to Classifications

The output file (`Key`, `Bot Flag`) can be uploaded in two ways:

**Manual:** Admin > Classifications > eVar23 > Import File (or via FTP if
the file is large), mapping `Key` to the ECID and `Bot Flag` to the
classification created for that purpose.

**Automated (recommended):** `upload_classifications.py` uploads the CSVs
from `output/` via the **Classifications API 2.0** (createApiJob →
uploadFile → commitApiJob → status polling flow). Authentication uses
OAuth Server-to-Server (JWT is deprecated).

### Credentials (never stored in the repo)

The script reads credentials only from environment variables. The
recommended way is a local `.env` file (ignored by git):

```bash
cp .env.example .env
# edit .env and put your real values
```

`.env` is in `.gitignore` and must **never** be pushed (the repo is public).
The script only loads it if you have `python-dotenv` installed
(`pip install -r requirements.txt`).

Variables:

```bash
ADOBE_CLIENT_ID=...        # client_id of the Developer Console project
ADOBE_CLIENT_SECRET=...    # client_secret — NEVER in the code
ADOBE_DATASET_ID=...       # eVar23 classification dataset
# Optional:
ADOBE_RSID=...             # report suite ID; used by --list-datasets (or --rsid)
ADOBE_COMPANY_ID=...       # if omitted, discovered via the Discovery API
ADOBE_SCOPES=...           # if omitted, uses the default from config.py
```

Alternative without `.env` (exporting them manually in the terminal):

```bash
export ADOBE_CLIENT_ID=...
export ADOBE_CLIENT_SECRET=...
export ADOBE_DATASET_ID=...
```

### How to find the DATASET_ID (one time only)

The Classifications API 2.0 dataset listing is **per report suite**, so
you need to provide the RSID (with `--rsid` or the `ADOBE_RSID`
variable):

```bash
python upload_classifications.py --list-datasets --rsid your_report_suite
# or, if you already set ADOBE_RSID in .env:
python upload_classifications.py --list-datasets
```

In the output, look for the entry whose `id` is `evar23`: the value of
`datasets` is the id you should copy to `ADOBE_DATASET_ID`.

> The `DATASET_ID` of this API is the id of an Adobe Analytics
> *Classification Set* (Components > Classification Sets), not an
> Experience Platform dataset.

### Upload

```bash
# Upload all output/bot_flag_upload*.csv:
python upload_classifications.py

# Upload a specific one:
python upload_classifications.py --file output/bot_flag_upload_part2.csv

# Upload and commit everything, but do NOT wait for processing (fire-and-forget):
python upload_classifications.py --no-wait
```

The script prints the status of each job and exits with a non-zero code
if any of them failed, so it can be chained in CI/cron.

### Performance with many parts

With dozens or hundreds of parts, the flow does **not** process one file
and then wait for Adobe to finish it before moving to the next. Instead
it works in two parallel phases:

1. **Upload:** `create + upload + commit` of all files in parallel. The
   import processing runs on Adobe's servers, so there is no need to wait
   for it before launching the next one.
2. **Polling:** it checks the status of all committed jobs in parallel,
   in rounds, until they finish.

This way the total time is bounded by the slowest phase and not by the
sum of each job's processing time. If you only want to upload and verify
later, use `--no-wait` (it runs phase 1 only).

The 2.0 API enforces a limit of ~120 requests/minute per user and returns
HTTP 429 when exceeded
([API 2.0 FAQ](https://dev.adobe.com/analytics-apis/docs/2.0/guides/faq)).
That is why the default concurrency is low and each request retries with
exponential backoff on 429 or 5xx errors. The parameters live in
`config.py` and you can adjust them to your account's limit:

```python
UPLOAD_CONCURRENCY = 4     # simultaneous uploads
POLL_CONCURRENCY   = 4     # jobs checked at a time during polling
RETRY_MAX_ATTEMPTS = 5     # retries per request on 429/5xx
RETRY_BACKOFF_BASE = 2.0   # seconds; backoff = BASE * 2**(attempt-1)
RETRY_BACKOFF_MAX  = 30.0  # cap on the per-attempt backoff
```

> Note: each file consumes 3 requests in the upload phase
> (create+upload+commit) plus 1 per polling round. With 100 parts that is
> ~300 requests just for the upload; if you raise the concurrency too
> much, you may hit the rate limit.

> Note: the exact field names of the 2.0 API (e.g. the multipart field in
> `uploadFile` and the response JSON keys) may vary by version; the script
> handles common variants, but if Adobe changes the contract it is worth
> validating against the
> [official docs](https://developer.adobe.com/analytics-apis/docs/2.0/guides/endpoints/classifications/import-file).
