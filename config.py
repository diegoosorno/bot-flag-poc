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
