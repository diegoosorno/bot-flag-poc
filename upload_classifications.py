"""
Bot Flag POC — subida automática a Adobe Analytics Classifications API 2.0

Toma los CSV generados por process_bot_flags.py (output/bot_flag_upload*.csv,
columnas Key,Bot Flag) y los sube al dataset de clasificación de eVar23 usando
el flujo de import por archivo de la Classifications API 2.0:

    1. createApiJob   -> crea el job de import y devuelve api_job_id
    2. uploadFile     -> sube el CSV (multipart) asociado al api_job_id
    3. commitApiJob   -> confirma el import
    4. (polling)      -> consulta el estado hasta que termine

Para muchas partes, el trabajo se hace en DOS fases paralelas en vez de
procesar cada archivo de forma secuencial y bloqueante:
    Fase 1: create+upload+commit de todos los archivos en paralelo
            (acotado por config.UPLOAD_CONCURRENCY).
    Fase 2: polling en paralelo de todos los jobs confirmados
            (acotado por config.POLL_CONCURRENCY).
Así el tiempo total queda acotado por la fase más lenta, no por la suma de
los tiempos de procesamiento de cada job. Con --no-wait solo se hace la
fase 1 (fire-and-forget). Ante HTTP 429 (rate limit de la API 2.0, ~120
req/min por usuario) o 5xx, cada request reintenta con backoff exponencial.

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
    ADOBE_RSID           (opcional; usada por --list-datasets para hallar el
                          DATASET_ID. También se puede pasar con --rsid)
    ADOBE_COMPANY_ID     (opcional; si falta se descubre vía Discovery API)
    ADOBE_SCOPES         (opcional; default en config.DEFAULT_SCOPES)

Uso:
    export ADOBE_CLIENT_ID=...       # nunca lo pongas en el código
    export ADOBE_CLIENT_SECRET=...
    export ADOBE_DATASET_ID=...

    # Subir todos los CSV de output/:
    python upload_classifications.py

    # Descubrir el DATASET_ID de tus clasificaciones (una sola vez).
    # Requiere el report suite ID (RSID), vía --rsid o ADOBE_RSID:
    python upload_classifications.py --list-datasets --rsid tu_report_suite

    # Subir un archivo específico:
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

# Carga .env si python-dotenv está instalado (opcional). Si no lo está, el
# script sigue funcionando con variables ya exportadas en el entorno.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(config.BASE_DIR, ".env"))
except ImportError:
    pass


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


def _request_with_retry(method, url, **kwargs):
    """
    Ejecuta una request reintentando ante rate limit (HTTP 429) y errores
    transitorios de servidor (5xx), con backoff exponencial + jitter.

    Devuelve la Response cuando el status NO es 429/5xx (el caller decide si
    ese status es aceptable). Lanza UploadError solo si se agotan los intentos
    contra 429/5xx, o requests.RequestException ante fallos de red repetidos.

    Es seguro para uso concurrente: no comparte estado mutable.
    """
    last_exc = None
    for attempt in range(1, config.RETRY_MAX_ATTEMPTS + 1):
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            # Fallo de red: reintentar salvo que sea el último intento.
            last_exc = exc
            if attempt == config.RETRY_MAX_ATTEMPTS:
                raise
            _sleep_backoff(attempt)
            continue

        # 429 = rate limit; 5xx = error transitorio de servidor -> reintentar.
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt == config.RETRY_MAX_ATTEMPTS:
                raise UploadError(
                    f"La API respondió {resp.status_code} tras "
                    f"{config.RETRY_MAX_ATTEMPTS} intentos en {method} {_safe_path(url)}. "
                    f"Puede ser rate limit (429); baja UPLOAD_CONCURRENCY/POLL_CONCURRENCY."
                )
            # Respeta Retry-After si viene; si no, backoff exponencial.
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), config.RETRY_BACKOFF_MAX))
            else:
                _sleep_backoff(attempt)
            continue

        return resp

    # No debería alcanzarse, pero por seguridad:
    if last_exc:
        raise last_exc
    raise UploadError(f"No se pudo completar {method} {_safe_path(url)}.")


def _sleep_backoff(attempt):
    """Backoff exponencial acotado con jitter para no sincronizar hilos."""
    delay = min(
        config.RETRY_BACKOFF_BASE * (2 ** (attempt - 1)),
        config.RETRY_BACKOFF_MAX,
    )
    time.sleep(delay + random.uniform(0, 0.5))


def _safe_path(url):
    """Devuelve solo el path de la URL para logs (evita filtrar query/host)."""
    return url.split("//", 1)[-1].split("/", 1)[-1].split("?", 1)[0]


# ------------------------------------------------------------------
# Descubrimiento de company id y datasets
# ------------------------------------------------------------------
def get_company_id(client_id, token):
    """Devuelve el global company id (de env, o vía Discovery API)."""
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


def list_datasets(client_id, token, company_id, rsid):
    """
    Lista los datasets de clasificación de un report suite (RSID) para
    ayudar a hallar el DATASET_ID.

    La Classifications API 2.0 no expone un listado global de datasets: el
    endpoint requiere el report suite ID y devuelve, por cada dimensión
    (evar/prop), los dataset IDs asociados.
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
            f"No se pudieron listar los datasets del report suite '{rsid}' "
            f"(HTTP {resp.status_code}). Verifica que el RSID sea correcto y que "
            f"el proyecto tenga acceso a ese report suite."
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
    resp = _request_with_retry(
        "POST",
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
    # Leemos el contenido a bytes una vez: si hay reintento por 429/5xx,
    # requests reconstruye el multipart desde cero (un file handle ya
    # consumido no se rebobinaría entre intentos).
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
            f"uploadFile falló para job {api_job_id} (HTTP {resp.status_code})."
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
            f"commitApiJob falló para job {api_job_id} (HTTP {resp.status_code})."
        )
    data = resp.json() if resp.content else {}
    return data.get("import_job_id") or data.get("jobId") or api_job_id


def check_status(client_id, token, company_id, job_id):
    """
    Consulta UNA vez el estado del job y lo normaliza.

    Devuelve uno de: "completed", "failed" o "pending". No bloquea ni duerme;
    el polling (con sus esperas) lo orquesta poll_all_jobs.
    """
    url = f"{_api_base(company_id)}/classifications/job/{job_id}"
    headers = _auth_headers(client_id, token, content_type=None)
    resp = _request_with_retry("GET", url, headers=headers, timeout=config.HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise UploadError(
            f"Consulta de estado falló para job {job_id} (HTTP {resp.status_code})."
        )
    data = resp.json()
    status = (data.get("status") or data.get("state") or "").lower()
    if status in ("completed", "success", "done", "finished"):
        return "completed"
    if status in ("failed", "error", "cancelled", "canceled"):
        return "failed"
    return "pending"


# ------------------------------------------------------------------
# FASE 1 — subir y confirmar (create + upload + commit) en paralelo
# ------------------------------------------------------------------
def submit_one(client_id, token, company_id, dataset_id, filepath):
    """
    Crea el job, sube el archivo y hace commit para UN archivo. No espera el
    procesamiento: devuelve el job_id confirmado para que la fase de polling
    lo consulte después. Pensada para correr concurrentemente.
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
    Sube y confirma todos los archivos en paralelo (acotado por
    UPLOAD_CONCURRENCY). Devuelve (submitted, submit_failures):
      - submitted: [{"file", "job_id"}] de los que llegaron a commit
      - submit_failures: [{"file", "error"}] de los que fallaron al subir
    """
    submitted = []
    submit_failures = []
    workers = max(1, config.UPLOAD_CONCURRENCY)
    print(f"\nFase 1/2: subiendo {len(files)} archivo(s) (concurrencia {workers})...")

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
                print(f"   subido y confirmado: {filename} (job {result['job_id']})")
            except (UploadError, requests.RequestException, OSError) as exc:
                submit_failures.append({"file": filename, "error": str(exc)})
                print(f"   FALLÓ la subida: {filename} ({exc})", file=sys.stderr)

    return submitted, submit_failures


# ------------------------------------------------------------------
# FASE 2 — polling de todos los jobs en paralelo hasta que terminen
# ------------------------------------------------------------------
def poll_all_jobs(client_id, token, company_id, submitted):
    """
    Hace polling de todos los jobs enviados hasta que terminen (completed/
    failed) o se agote STATUS_POLL_MAX_ATTEMPTS. Cada ronda consulta los jobs
    aún pendientes en paralelo (acotado por POLL_CONCURRENCY) y luego duerme
    STATUS_POLL_INTERVAL antes de la siguiente ronda.

    Devuelve un dict {job_id: "completed"|"failed"|"timeout"}.
    """
    workers = max(1, config.POLL_CONCURRENCY)
    pending = {s["job_id"] for s in submitted}
    results = {}
    print(
        f"\nFase 2/2: esperando procesamiento de {len(pending)} job(s) "
        f"(concurrencia {workers})..."
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
                    # Error consultando: lo dejamos pendiente para reintentar
                    # en la próxima ronda, salvo que sea la última.
                    print(
                        f"   aviso: no se pudo consultar job {job_id} ({exc})",
                        file=sys.stderr,
                    )
                    continue
                if state in ("completed", "failed"):
                    results[job_id] = state

        pending = {j for j in pending if j not in results}
        if pending and attempt < config.STATUS_POLL_MAX_ATTEMPTS:
            print(f"   {len(pending)} job(s) aún en proceso...")
            time.sleep(config.STATUS_POLL_INTERVAL)

    # Lo que quede pendiente al agotar intentos se marca como timeout.
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
    parser.add_argument(
        "--rsid",
        help="Report suite ID a consultar con --list-datasets. Si se omite, se "
        "usa la variable de entorno ADOBE_RSID.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Sube y confirma todo, imprime los job IDs y sale SIN esperar el "
        "procesamiento (fire-and-forget). Verifica el estado luego.",
    )
    args = parser.parse_args()

    try:
        client_id, token = get_access_token()
        company_id = get_company_id(client_id, token)
        print(f"Autenticado. Global company id: {company_id}")

        if args.list_datasets:
            rsid = args.rsid or os.environ.get("ADOBE_RSID")
            if not rsid:
                raise ConfigError(
                    "Para listar datasets necesitas indicar el report suite ID: "
                    "pasa --rsid <RSID> o define la variable de entorno ADOBE_RSID."
                )
            datasets = list_datasets(client_id, token, company_id, rsid)
            print(f"\nDatasets de clasificación del report suite '{rsid}':")
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

        # Filtramos los que no existen antes de arrancar las fases.
        existing = []
        for filepath in files:
            if not os.path.isfile(filepath):
                print(f"   omitido (no existe): {filepath}", file=sys.stderr)
                continue
            existing.append(filepath)

        if not existing:
            print("No hay archivos válidos para subir.", file=sys.stderr)
            return 1

        print(f"Archivos a subir: {len(existing)} (dataset {dataset_id})")

        # --- Fase 1: subir y confirmar todo en paralelo ---
        submitted, submit_failures = submit_all(
            client_id, token, company_id, dataset_id, existing
        )

        # Fire-and-forget: no esperamos el procesamiento del lado de Adobe.
        if args.no_wait:
            print("\n" + "=" * 60)
            print("RESUMEN DE SUBIDA (sin esperar procesamiento)")
            print("=" * 60)
            for s in submitted:
                print(f"  {s['file']:35s} enviado    job={s['job_id']}")
            for f in submit_failures:
                print(f"  {f['file']:35s} FALLÓ subida", file=sys.stderr)
            print(
                f"\n{len(submitted)} enviado(s), {len(submit_failures)} con error de subida."
            )
            return 1 if submit_failures else 0

        # --- Fase 2: polling paralelo de todos los jobs confirmados ---
        job_states = {}
        if submitted:
            job_states = poll_all_jobs(client_id, token, company_id, submitted)

        print("\n" + "=" * 60)
        print("RESUMEN DE SUBIDA")
        print("=" * 60)
        failures = 0
        for s in submitted:
            state = job_states.get(s["job_id"], "desconocido")
            print(f"  {s['file']:35s} {state:11s} job={s['job_id']}")
            if state != "completed":
                failures += 1
        for f in submit_failures:
            print(f"  {f['file']:35s} error-subida", file=sys.stderr)
            failures += 1
        print(
            f"\nTotal: {len(existing)} | ok: {len(existing) - failures} | "
            f"con problemas: {failures}"
        )
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
