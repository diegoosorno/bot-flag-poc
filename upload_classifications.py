"""
Bot Flag POC — subida automática a Adobe Analytics Classifications API 2.0

Toma los CSV generados por process_bot_flags.py (output/bot_flag_upload*.csv,
columnas Key,Bot Flag) y los sube al dataset de clasificación de eVar23 usando
el flujo de import por archivo de la Classifications API 2.0:

    1. createApiJob   -> crea el job de import y devuelve api_job_id
    2. uploadFile     -> sube el CSV (multipart) asociado al api_job_id
    3. commitApiJob   -> confirma el import
    4. (polling)      -> consulta el estado hasta que termine

Autenticación: OAuth Server-to-Server (client credentials) contra Adobe IMS.
JWT quedó deprecado; este script usa el flujo actual.

--- SEGURIDAD ---
Las credenciales se leen EXCLUSIVAMENTE de variables de entorno; nunca se
escriben en el repo ni se imprimen. Los ECID son datos de visitante (PII):
no se loggea el contenido de las filas. TLS siempre verificado.

Variables de entorno (ver README):
    ADOBE_CLIENT_ID      (requerida)
    ADOBE_CLIENT_SECRET  (requerida)
    ADOBE_DATASET_ID     (requerida)
    ADOBE_COMPANY_ID     (opcional; si falta se descubre vía Discovery API)
    ADOBE_SCOPES         (opcional; default en config.DEFAULT_SCOPES)

Uso:
    export ADOBE_CLIENT_ID=...       # nunca lo pongas en el código
    export ADOBE_CLIENT_SECRET=...
    export ADOBE_DATASET_ID=...

    # Subir todos los CSV de output/:
    python upload_classifications.py

    # Descubrir el DATASET_ID de tus clasificaciones (una sola vez):
    python upload_classifications.py --list-datasets

    # Subir un archivo específico:
    python upload_classifications.py --file output/bot_flag_upload_part2.csv
"""

import argparse
import glob
import os
import sys
import time

import requests

import config


class ConfigError(Exception):
    """Falta configuración (credenciales/env) necesaria para operar."""


class UploadError(Exception):
    """Error durante el flujo de import contra la API."""


# ------------------------------------------------------------------
# Credenciales y autenticación
# ------------------------------------------------------------------
def _require_env(name):
    """Lee una variable de entorno obligatoria sin exponer su valor."""
    value = os.environ.get(name)
    if not value:
        raise ConfigError(
            f"Falta la variable de entorno {name}. Expórtala antes de correr "
            f"el script (no la escribas en el código ni en el repo)."
        )
    return value


def get_access_token():
    """
    Obtiene un access token de Adobe IMS con el flujo client_credentials.
    No imprime ni devuelve el client_secret; el token no se loggea.
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
        # verify=True es el default de requests; TLS se valida siempre.
    )
    if resp.status_code != 200:
        # Mensaje genérico: no incluimos el cuerpo por si trae detalles sensibles.
        raise UploadError(
            f"No se pudo obtener el access token de IMS (HTTP {resp.status_code}). "
            f"Verifica ADOBE_CLIENT_ID / ADOBE_CLIENT_SECRET / ADOBE_SCOPES."
        )
    token = resp.json().get("access_token")
    if not token:
        raise UploadError("La respuesta de IMS no incluyó access_token.")
    return client_id, token


def _auth_headers(client_id, token, content_type="application/json"):
    headers = {
        "x-api-key": client_id,
        "Authorization": f"Bearer {token}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


# ------------------------------------------------------------------
# Descubrimiento de company id y datasets
# ------------------------------------------------------------------
def get_company_id(client_id, token):
    """Devuelve el global company id (de env, o vía Discovery API)."""
    env_company = os.environ.get("ADOBE_COMPANY_ID")
    if env_company:
        return env_company

    resp = requests.get(
        config.DISCOVERY_URL,
        headers=_auth_headers(client_id, token, content_type=None),
        timeout=config.HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise UploadError(
            f"Discovery API falló (HTTP {resp.status_code}). Define ADOBE_COMPANY_ID "
            f"manualmente para saltarte este paso."
        )
    data = resp.json()
    for org in data.get("imsOrgs", []):
        for company in org.get("companies", []):
            gid = company.get("globalCompanyId")
            if gid:
                return gid
    raise UploadError(
        "No se encontró globalCompanyId en la respuesta de Discovery. "
        "Define ADOBE_COMPANY_ID manualmente."
    )


def _api_base(company_id):
    return f"{config.ANALYTICS_API_HOST}/api/{company_id}"


def list_datasets(client_id, token, company_id):
    """Lista los datasets de clasificación para ayudar a hallar el DATASET_ID."""
    url = f"{_api_base(company_id)}/classifications/datasets"
    resp = requests.get(
        url,
        headers=_auth_headers(client_id, token, content_type=None),
        timeout=config.HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise UploadError(
            f"No se pudieron listar los datasets (HTTP {resp.status_code})."
        )
    return resp.json()


# ------------------------------------------------------------------
# Flujo de import por archivo
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
    resp = requests.post(
        url,
        headers=_auth_headers(client_id, token),
        json=body,
        timeout=config.HTTP_TIMEOUT,
    )
    if resp.status_code not in (200, 201):
        raise UploadError(
            f"createApiJob falló para dataset {dataset_id} (HTTP {resp.status_code})."
        )
    data = resp.json()
    job_id = data.get("api_job_id") or data.get("apiJobId") or data.get("jobId")
    if not job_id:
        raise UploadError(f"createApiJob no devolvió api_job_id. Respuesta: {data}")
    return job_id


def upload_file(client_id, token, company_id, api_job_id, filepath):
    url = f"{_api_base(company_id)}/classifications/job/import/uploadFile/{api_job_id}"
    # Content-Type multipart lo pone requests con boundary; no lo forzamos.
    headers = _auth_headers(client_id, token, content_type=None)
    filename = os.path.basename(filepath)
    with open(filepath, "rb") as fh:
        files = {"file": (filename, fh, "text/csv")}
        resp = requests.post(
            url,
            headers=headers,
            files=files,
            timeout=config.HTTP_TIMEOUT,
        )
    if resp.status_code not in (200, 201, 202):
        raise UploadError(
            f"uploadFile falló para job {api_job_id} (HTTP {resp.status_code})."
        )
    return True


def commit_job(client_id, token, company_id, api_job_id):
    url = f"{_api_base(company_id)}/classifications/job/import/commitApiJob/{api_job_id}"
    resp = requests.post(
        url,
        headers=_auth_headers(client_id, token),
        timeout=config.HTTP_TIMEOUT,
    )
    if resp.status_code not in (200, 201, 202):
        raise UploadError(
            f"commitApiJob falló para job {api_job_id} (HTTP {resp.status_code})."
        )
    data = resp.json() if resp.content else {}
    return data.get("import_job_id") or data.get("jobId") or api_job_id


def poll_status(client_id, token, company_id, job_id):
    """Consulta el estado del job hasta que termine o se agoten los intentos."""
    url = f"{_api_base(company_id)}/classifications/job/{job_id}"
    headers = _auth_headers(client_id, token, content_type=None)
    for attempt in range(1, config.STATUS_POLL_MAX_ATTEMPTS + 1):
        resp = requests.get(url, headers=headers, timeout=config.HTTP_TIMEOUT)
        if resp.status_code != 200:
            raise UploadError(
                f"Consulta de estado falló para job {job_id} (HTTP {resp.status_code})."
            )
        data = resp.json()
        status = (data.get("status") or data.get("state") or "").lower()
        if status in ("completed", "success", "done", "finished"):
            return "completed", data
        if status in ("failed", "error", "cancelled", "canceled"):
            return "failed", data
        time.sleep(config.STATUS_POLL_INTERVAL)
    return "timeout", {"attempts": config.STATUS_POLL_MAX_ATTEMPTS}


def upload_one(client_id, token, company_id, dataset_id, filepath):
    """Ejecuta el flujo completo para un archivo y devuelve un resumen."""
    filename = os.path.basename(filepath)
    print(f"\n-> {filename}")

    api_job_id = create_job(
        client_id, token, company_id, dataset_id, job_name=f"bot-flag {filename}"
    )
    print(f"   job creado: {api_job_id}")

    upload_file(client_id, token, company_id, api_job_id, filepath)
    print("   archivo subido")

    committed_id = commit_job(client_id, token, company_id, api_job_id)
    print(f"   commit ok (job {committed_id}), esperando procesamiento...")

    status, _ = poll_status(client_id, token, company_id, committed_id)
    print(f"   estado final: {status}")
    return {"file": filename, "job_id": committed_id, "status": status}


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def find_output_csvs():
    pattern = os.path.join(config.OUTPUT_FOLDER, "bot_flag_upload*.csv")
    return sorted(glob.glob(pattern))


def main():
    parser = argparse.ArgumentParser(
        description="Sube los CSV de clasificación a Adobe Analytics (API 2.0)."
    )
    parser.add_argument(
        "--file",
        help="Ruta a un CSV específico. Si se omite, sube todos los de output/.",
    )
    parser.add_argument(
        "--list-datasets",
        action="store_true",
        help="Lista los datasets de clasificación (para hallar el DATASET_ID) y sale.",
    )
    args = parser.parse_args()

    try:
        client_id, token = get_access_token()
        company_id = get_company_id(client_id, token)
        print(f"Autenticado. Global company id: {company_id}")

        if args.list_datasets:
            datasets = list_datasets(client_id, token, company_id)
            print("\nDatasets de clasificación disponibles:")
            print(datasets)
            return 0

        dataset_id = _require_env("ADOBE_DATASET_ID")

        if args.file:
            files = [args.file]
        else:
            files = find_output_csvs()

        if not files:
            print(
                "No se encontraron CSV para subir. Corre primero process_bot_flags.py "
                "o pasa --file.",
                file=sys.stderr,
            )
            return 1

        print(f"Archivos a subir: {len(files)} (dataset {dataset_id})")

        results = []
        for filepath in files:
            if not os.path.isfile(filepath):
                print(f"   omitido (no existe): {filepath}", file=sys.stderr)
                continue
            results.append(
                upload_one(client_id, token, company_id, dataset_id, filepath)
            )

        print("\n" + "=" * 60)
        print("RESUMEN DE SUBIDA")
        print("=" * 60)
        failures = 0
        for r in results:
            print(f"  {r['file']:35s} {r['status']:10s} job={r['job_id']}")
            if r["status"] != "completed":
                failures += 1
        return 1 if failures else 0

    except (ConfigError, UploadError) as exc:
        # Fail-closed: mensaje claro, sin volcar secretos ni stack traces.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except requests.RequestException as exc:
        print(f"ERROR de red: {exc.__class__.__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
