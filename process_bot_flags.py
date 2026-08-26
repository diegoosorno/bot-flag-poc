"""
Bot Flag POC — procesamiento de exports de Adobe Data Warehouse

Toma los 3 exports de Data Warehouse (Bots_Tier_1/2/3, uno por nivel de
confianza), remueve las filas con ECID vacío (solo ceros), reconcilia los
ECID que aparecen en más de un tier (gana el nivel más alto), y genera
un CSV final en formato Classification/SAINT (Key, Bot Flag) listo para
subir como clasificación de eVar23 en Adobe Analytics.

Uso:
    1. Coloca los 3 archivos (.zip o .csv) dentro de la carpeta input/
    2. Ejecuta: python process_bot_flags.py
    3. El resultado queda en la carpeta output/

Formato de entrada esperado (export estándar de Data Warehouse):
    - CSV con encabezados en la primera fila.
    - Columna "Marketing Cloud Visitor ID" (el ECID).
    - Columna "Unique Visitors" (no se usa, se ignora).
    - Puede venir dentro de un .zip o como .csv directo.

Formato de salida:
    - CSV UTF-8, separado por comas.
    - Columnas: Key, Bot Flag (valores: high / medium / low).
"""

import pandas as pd
import zipfile
import io
import re
import os
import glob

import config

ZERO_ECID_PATTERN = re.compile(r"^0+$")


def find_input_files(folder):
    """Encuentra los 3 archivos de entrada (zip o csv) según el patrón de nombre."""
    found = {}
    missing = []
    for pattern, tier in config.TIER_MAP.items():
        matches = glob.glob(os.path.join(folder, f"{pattern}*"))
        if not matches:
            missing.append(pattern)
        else:
            found[tier] = matches[0]
    if missing:
        raise FileNotFoundError(
            f"No se encontraron archivos para estos patrones en '{folder}': {missing}. "
            f"Verifica que los 3 archivos estén en la carpeta input/."
        )
    return found


def open_as_dataframe_iterator(filepath):
    """
    Abre un archivo .zip (con un .csv/.txt dentro) o un .csv directamente,
    devolviendo un iterador de chunks de pandas.
    """
    if filepath.lower().endswith(".zip"):
        with zipfile.ZipFile(filepath) as z:
            data_names = [
                n for n in z.namelist()
                if n.lower().endswith(".csv") or n.lower().endswith(".txt")
            ]
            if not data_names:
                raise ValueError(f"No se encontró un CSV/TXT dentro de {filepath}")
            with z.open(data_names[0]) as f:
                content = f.read()
            buffer = io.BytesIO(content)
            return pd.read_csv(buffer, chunksize=config.CHUNK_SIZE, dtype=str)
    else:
        return pd.read_csv(filepath, chunksize=config.CHUNK_SIZE, dtype=str)


def process_file(filepath, tier, stats):
    """Procesa un archivo de un tier y devuelve un dict {ecid: tier}."""
    ecid_to_tier = {}
    total_rows = 0
    zero_rows_removed = 0

    for chunk in open_as_dataframe_iterator(filepath):
        if config.ECID_COLUMN not in chunk.columns:
            raise ValueError(
                f"El archivo {filepath} no tiene la columna esperada "
                f"'{config.ECID_COLUMN}'. Columnas encontradas: {list(chunk.columns)}"
            )
        total_rows += len(chunk)

        chunk[config.ECID_COLUMN] = chunk[config.ECID_COLUMN].astype(str).str.strip()

        is_zero = chunk[config.ECID_COLUMN].apply(
            lambda x: bool(ZERO_ECID_PATTERN.match(x.replace("-", "")))
        )
        zero_rows_removed += int(is_zero.sum())
        chunk = chunk[~is_zero]

        for ecid in chunk[config.ECID_COLUMN]:
            if ecid and ecid.lower() != "nan":
                ecid_to_tier[ecid] = tier

    stats[tier] = {
        "total_rows_read": total_rows,
        "zero_ecid_rows_removed": zero_rows_removed,
        "unique_ecids": len(ecid_to_tier),
    }
    return ecid_to_tier


def reconcile(all_tiers_dicts):
    """Combina los diccionarios ECID->tier, quedándose con el tier más alto por ECID."""
    combined = {}
    duplicates_resolved = 0

    for tier_name, ecid_dict in all_tiers_dicts.items():
        for ecid, tier in ecid_dict.items():
            if ecid in combined:
                duplicates_resolved += 1
                if config.TIER_PRIORITY[tier] > config.TIER_PRIORITY[combined[ecid]]:
                    combined[ecid] = tier
            else:
                combined[ecid] = tier

    return combined, duplicates_resolved


def write_output(combined, output_folder):
    """Escribe el CSV final, dividiéndolo en partes si excede el tamaño máximo."""
    os.makedirs(output_folder, exist_ok=True)
    rows = list(combined.items())
    df = pd.DataFrame(rows, columns=[config.OUTPUT_KEY_COLUMN, config.OUTPUT_FLAG_COLUMN])

    est_size_mb = df.memory_usage(deep=True).sum() / (1024 * 1024)
    n_parts = max(1, int(est_size_mb // config.MAX_OUTPUT_SIZE_MB) + 1)

    output_files = []
    if n_parts == 1:
        path = os.path.join(output_folder, "bot_flag_upload.csv")
        df.to_csv(path, index=False, encoding="utf-8")
        output_files.append(path)
    else:
        chunk_len = len(df) // n_parts + 1
        for i in range(n_parts):
            part_df = df.iloc[i * chunk_len:(i + 1) * chunk_len]
            if len(part_df) == 0:
                continue
            path = os.path.join(output_folder, f"bot_flag_upload_part{i+1}.csv")
            part_df.to_csv(path, index=False, encoding="utf-8")
            output_files.append(path)

    return output_files, len(df)


def main():
    print("=" * 60)
    print("Bot Flag POC — procesando exports de Data Warehouse")
    print("=" * 60)

    input_files = find_input_files(config.INPUT_FOLDER)
    stats = {}
    all_tiers_dicts = {}

    for tier, filepath in input_files.items():
        print(f"\nProcesando tier '{tier}': {os.path.basename(filepath)}")
        ecid_dict = process_file(filepath, tier, stats)
        all_tiers_dicts[tier] = ecid_dict
        s = stats[tier]
        print(f"  Filas leídas: {s['total_rows_read']}")
        print(f"  Filas con ECID de ceros removidas: {s['zero_ecid_rows_removed']}")
        print(f"  ECIDs únicos: {s['unique_ecids']}")

    print("\nReconciliando duplicados entre tiers (gana el nivel más alto)...")
    combined, duplicates_resolved = reconcile(all_tiers_dicts)
    print(f"  Duplicados resueltos: {duplicates_resolved}")
    print(f"  Total ECIDs únicos combinados: {len(combined)}")

    print("\nEscribiendo archivo(s) de salida...")
    output_files, total_output_rows = write_output(combined, config.OUTPUT_FOLDER)
    for f in output_files:
        size_mb = os.path.getsize(f) / (1024 * 1024)
        print(f"  {f}  ({size_mb:.1f} MB)")

    print("\n" + "=" * 60)
    print("RESUMEN FINAL")
    print("=" * 60)
    for tier, s in stats.items():
        print(
            f"  {tier:8s} -> leídas: {s['total_rows_read']:>10} | "
            f"ceros removidos: {s['zero_ecid_rows_removed']:>6} | "
            f"únicos: {s['unique_ecids']:>10}"
        )
    print(f"  Duplicados resueltos entre tiers: {duplicates_resolved}")
    print(f"  Filas en archivo(s) de salida: {total_output_rows}")


if __name__ == "__main__":
    main()
