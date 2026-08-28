"""
Bot Flag POC — processing of Adobe Data Warehouse exports

Takes the 3 Data Warehouse exports (Bots_Tier_1/2/3, one per confidence
level), removes rows with an empty ECID (all zeros), reconciles the ECIDs that
appear in more than one tier (the highest level wins), and produces a final CSV
in Classification/SAINT format (Key, Bot Flag) ready to upload as an eVar23
classification in Adobe Analytics.

Usage:
    1. Place the 3 files (.zip or .csv) inside the input/ folder
    2. Run: python process_bot_flags.py
    3. The result is written to the output/ folder

Expected input format (standard Data Warehouse export):
    - CSV with headers in the first row.
    - "Marketing Cloud Visitor ID" column (the ECID).
    - "Unique Visitors" column (not used, ignored).
    - May come inside a .zip or as a direct .csv.

Output format:
    - UTF-8 CSV, comma-separated.
    - Columns: Key, Bot Flag (values: high / medium / low).
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
    """Find the 3 input files (zip or csv) by their name pattern."""
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
            f"No files found for these patterns in '{folder}': {missing}. "
            f"Check that the 3 files are in the input/ folder."
        )
    return found


def open_as_dataframe_iterator(filepath):
    """
    Open a .zip file (with a .csv/.txt inside) or a .csv directly,
    returning a pandas chunk iterator.
    """
    if filepath.lower().endswith(".zip"):
        with zipfile.ZipFile(filepath) as z:
            data_names = [
                n for n in z.namelist()
                if n.lower().endswith(".csv") or n.lower().endswith(".txt")
            ]
            if not data_names:
                raise ValueError(f"No CSV/TXT found inside {filepath}")
            with z.open(data_names[0]) as f:
                content = f.read()
            buffer = io.BytesIO(content)
            return pd.read_csv(buffer, chunksize=config.CHUNK_SIZE, dtype=str)
    else:
        return pd.read_csv(filepath, chunksize=config.CHUNK_SIZE, dtype=str)


def process_file(filepath, tier, stats):
    """Process one tier's file and return a dict {ecid: tier}."""
    ecid_to_tier = {}
    total_rows = 0
    zero_rows_removed = 0

    for chunk in open_as_dataframe_iterator(filepath):
        if config.ECID_COLUMN not in chunk.columns:
            raise ValueError(
                f"The file {filepath} does not have the expected column "
                f"'{config.ECID_COLUMN}'. Columns found: {list(chunk.columns)}"
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
    """Merge the ECID->tier dicts, keeping the highest tier per ECID."""
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
    """Write the final CSV, splitting it into parts if it exceeds the max size."""
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
    print("Bot Flag POC — processing Data Warehouse exports")
    print("=" * 60)

    input_files = find_input_files(config.INPUT_FOLDER)
    stats = {}
    all_tiers_dicts = {}

    for tier, filepath in input_files.items():
        print(f"\nProcessing tier '{tier}': {os.path.basename(filepath)}")
        ecid_dict = process_file(filepath, tier, stats)
        all_tiers_dicts[tier] = ecid_dict
        s = stats[tier]
        print(f"  Rows read: {s['total_rows_read']}")
        print(f"  Zero-ECID rows removed: {s['zero_ecid_rows_removed']}")
        print(f"  Unique ECIDs: {s['unique_ecids']}")

    print("\nReconciling duplicates across tiers (highest level wins)...")
    combined, duplicates_resolved = reconcile(all_tiers_dicts)
    print(f"  Duplicates resolved: {duplicates_resolved}")
    print(f"  Total unique combined ECIDs: {len(combined)}")

    print("\nWriting output file(s)...")
    output_files, total_output_rows = write_output(combined, config.OUTPUT_FOLDER)
    for f in output_files:
        size_mb = os.path.getsize(f) / (1024 * 1024)
        print(f"  {f}  ({size_mb:.1f} MB)")

    print("\n" + "=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)
    for tier, s in stats.items():
        print(
            f"  {tier:8s} -> read: {s['total_rows_read']:>10} | "
            f"zeros removed: {s['zero_ecid_rows_removed']:>6} | "
            f"unique: {s['unique_ecids']:>10}"
        )
    print(f"  Duplicates resolved across tiers: {duplicates_resolved}")
    print(f"  Rows in output file(s): {total_output_rows}")


if __name__ == "__main__":
    main()
