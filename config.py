"""
Bot Flag POC project configuration.
Adjust the case-specific parameters here — do not change the processing
logic in process_bot_flags.py just to tweak these values.
"""

import os

# ============================================================
# FOLDERS
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_FOLDER = os.path.join(BASE_DIR, "input")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "output")

# ============================================================
# INPUT COLUMNS (Adobe Data Warehouse export)
# ============================================================
ECID_COLUMN = "Marketing Cloud Visitor ID"

# ============================================================
# OUTPUT COLUMNS (Classification/SAINT format for eVar23)
# ============================================================
OUTPUT_KEY_COLUMN = "Key"
OUTPUT_FLAG_COLUMN = "Bot Flag"

# ============================================================
# FILE -> CONFIDENCE LEVEL MAPPING
# ============================================================
# The script looks in INPUT_FOLDER for files whose name STARTS WITH
# each of these patterns (regardless of extension: .zip or .csv).
TIER_MAP = {
    "Bots_Tier_1": "high",
    "Bots_Tier_2": "medium",
    "Bots_Tier_3": "low",
}

# Priority for reconciling ECIDs that appear in more than one tier
# (the highest number wins).
TIER_PRIORITY = {"high": 3, "medium": 2, "low": 1}

# ============================================================
# PROCESSING LIMITS
# ============================================================
MAX_OUTPUT_SIZE_MB = 50    # the final CSV is split into parts if it exceeds this
CHUNK_SIZE = 200_000       # rows per chunk when reading large files

# ============================================================
# UPLOAD VIA CLASSIFICATIONS API 2.0 (upload_classifications.py)
# ============================================================
# IMPORTANT: no secrets go here. Credentials are ALWAYS read from
# environment variables (see README). These are only non-sensitive
# endpoints and parameters.
#
# Required environment variables:
#   ADOBE_CLIENT_ID       -> client_id of the Developer Console project
#   ADOBE_CLIENT_SECRET   -> client_secret (NEVER write it in the code)
#   ADOBE_DATASET_ID      -> id of the eVar23 classification dataset
# Optional:
#   ADOBE_COMPANY_ID      -> global company id (if unset, discovered via API)
#   ADOBE_SCOPES          -> OAuth scopes (comma-separated)

# IMS endpoint for OAuth Server-to-Server (client credentials).
IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"

# Default scopes for the Adobe Analytics API (overridable via env).
DEFAULT_SCOPES = "openid,AdobeID,additional_info.projectedProductContext,read_organizations,additional_info.roles,session"

# Base of the Analytics 2.0 API.
ANALYTICS_API_HOST = "https://analytics.adobe.io"
DISCOVERY_URL = f"{ANALYTICS_API_HOST}/discovery/me"

# Format the file is declared with when creating the job.
UPLOAD_DATA_FORMAT = "csv"     # our output is CSV (Key,Bot Flag)
UPLOAD_ENCODING = "UTF8"
UPLOAD_LIST_DELIMITER = ","

# Timeouts (seconds) and status polling.
HTTP_TIMEOUT = 60
STATUS_POLL_INTERVAL = 10
STATUS_POLL_MAX_ATTEMPTS = 60   # ~10 min max waiting for each job

# ------------------------------------------------------------------
# CONCURRENCY AND RATE LIMIT (uploading many parts in parallel)
# ------------------------------------------------------------------
# The flow processes files in two parallel phases: first it uploads and
# commits all jobs, then it polls all of them until they finish. This way
# the total time is no longer linear (Nx the processing time) and is instead
# bounded by the slowest phase.
#
# The Analytics 2.0 API enforces ~120 requests/minute per user (12 every 6s);
# exceeding it returns HTTP 429. That is why concurrency is low by default and
# there are retries with backoff on 429/5xx. Raise it carefully if your account
# has a higher limit.
UPLOAD_CONCURRENCY = 4          # uploads (create+upload+commit) in parallel
POLL_CONCURRENCY = 4            # jobs polled in parallel during polling

# Retries on rate limit (429) or transient server errors (5xx).
RETRY_MAX_ATTEMPTS = 5          # total attempts per request before failing
RETRY_BACKOFF_BASE = 2.0        # seconds; backoff is BASE * 2**(attempt-1)
RETRY_BACKOFF_MAX = 30.0        # cap on the per-attempt backoff
