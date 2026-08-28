"""
Configuración del proyecto Bot Flag POC.
Ajustar aquí los parámetros específicos del caso — no modificar la
lógica de procesamiento en process_bot_flags.py para cambiar esto.
"""

import os

# ============================================================
# CARPETAS
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_FOLDER = os.path.join(BASE_DIR, "input")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "output")

# ============================================================
# COLUMNAS DE ENTRADA (export de Adobe Data Warehouse)
# ============================================================
ECID_COLUMN = "Marketing Cloud Visitor ID"

# ============================================================
# COLUMNAS DE SALIDA (formato Classification/SAINT para eVar23)
# ============================================================
OUTPUT_KEY_COLUMN = "Key"
OUTPUT_FLAG_COLUMN = "Bot Flag"

# ============================================================
# MAPEO DE ARCHIVO -> NIVEL DE CONFIANZA
# ============================================================
# El script busca archivos en INPUT_FOLDER cuyo nombre EMPIECE con
# cada uno de estos patrones (sin importar la extensión: .zip o .csv).
TIER_MAP = {
    "Bots_Tier_1": "high",
    "Bots_Tier_2": "medium",
    "Bots_Tier_3": "low",
}

# Prioridad para reconciliar ECIDs que aparecen en más de un tier
# (gana el número más alto).
TIER_PRIORITY = {"high": 3, "medium": 2, "low": 1}

# ============================================================
# LÍMITES DE PROCESAMIENTO
# ============================================================
MAX_OUTPUT_SIZE_MB = 50    # el CSV final se divide en partes si excede esto
CHUNK_SIZE = 200_000       # filas por chunk al leer archivos grandes

# ============================================================
# SUBIDA VÍA CLASSIFICATIONS API 2.0 (upload_classifications.py)
# ============================================================
# IMPORTANTE: aquí NO se ponen secretos. Las credenciales se leen SIEMPRE
# desde variables de entorno (ver README). Estos son solo endpoints y
# parámetros no sensibles.
#
# Variables de entorno requeridas:
#   ADOBE_CLIENT_ID       -> client_id del proyecto de Developer Console
#   ADOBE_CLIENT_SECRET   -> client_secret (NUNCA lo escribas en el código)
#   ADOBE_DATASET_ID      -> id del dataset de clasificación de eVar23
# Opcionales:
#   ADOBE_COMPANY_ID      -> global company id (si no, se descubre vía API)
#   ADOBE_SCOPES          -> scopes OAuth (separados por coma)

# Endpoint de IMS para OAuth Server-to-Server (client credentials).
IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"

# Scopes por defecto para la API de Adobe Analytics (ajustables por env).
DEFAULT_SCOPES = "openid,AdobeID,additional_info.projectedProductContext,read_organizations,additional_info.roles,session"

# Base de la API de Analytics 2.0.
ANALYTICS_API_HOST = "https://analytics.adobe.io"
DISCOVERY_URL = f"{ANALYTICS_API_HOST}/discovery/me"

# Formato con el que se declara el archivo al crear el job.
UPLOAD_DATA_FORMAT = "csv"     # nuestro output es CSV (Key,Bot Flag)
UPLOAD_ENCODING = "UTF8"
UPLOAD_LIST_DELIMITER = ","

# Timeouts (segundos) y polling de estado.
HTTP_TIMEOUT = 60
STATUS_POLL_INTERVAL = 10
STATUS_POLL_MAX_ATTEMPTS = 60   # ~10 min máximo esperando cada job

# ------------------------------------------------------------------
# CONCURRENCIA Y RATE LIMIT (subida de muchas partes en paralelo)
# ------------------------------------------------------------------
# El flujo procesa los archivos en dos fases paralelas: primero sube y
# confirma todos los jobs, luego hace polling de todos hasta que terminen.
# Así el tiempo deja de ser lineal (Nx el tiempo de procesamiento) y queda
# acotado por la fase más lenta.
#
# La Analytics 2.0 API impone ~120 requests/minuto por usuario (12 cada 6s);
# al pasarse devuelve HTTP 429. Por eso la concurrencia es baja por defecto y
# hay reintentos con backoff ante 429/5xx. Súbela con cuidado si tu cuenta
# tiene un límite mayor.
UPLOAD_CONCURRENCY = 4          # subidas (create+upload+commit) en paralelo
POLL_CONCURRENCY = 4            # jobs consultados en paralelo durante el polling

# Reintentos ante rate limit (429) o errores transitorios de servidor (5xx).
RETRY_MAX_ATTEMPTS = 5          # intentos totales por request antes de fallar
RETRY_BACKOFF_BASE = 2.0        # segundos; el backoff es BASE * 2**(intento-1)
RETRY_BACKOFF_MAX = 30.0        # tope del backoff por intento