#!/usr/bin/env python3
"""
PADME Conditions Database — built on DuckDB.

Creates and manages a single-file columnar database for ~400M physics events.
All sub-detector observables live in one flat table (`events`).
New sub-detectors can be added later by appending columns.

Usage
-----
    # Build the DB from the example CSV files shipped with this repo
    python padme_conditions_db.py --build

    # Interactive DuckDB shell on the resulting file
    python padme_conditions_db.py --shell

    # Run a quick sanity check
    python padme_conditions_db.py --check
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from pathlib import Path

import duckdb
import yaml

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────
DB_FILE = "padme_conditions.duckdb"
BASE_DIR = Path(__file__).resolve().parent
SCHEMA_FILE = BASE_DIR / "schema.yaml"

# ──────────────────────────────────────────────────────────────────────
# Schema loading from schema.yaml
# ──────────────────────────────────────────────────────────────────────

def load_schema(schema_path: str | Path = SCHEMA_FILE) -> dict:
    """
    Load and return the raw schema dict from the YAML file.
    Top-level keys are detector groups; each group maps column names to
    dicts with at least a 'type' key.
    """
    with open(schema_path, "r") as f:
        return yaml.safe_load(f)


def schema_to_flat_columns(schema: dict) -> dict[str, str]:
    """
    Flatten the grouped schema into an ordered dict of
    column_name → SQL type (e.g. "DOUBLE", "INTEGER NOT NULL").
    """
    flat: dict[str, str] = {}
    for group_name, columns in schema.items():
        for col_name, col_info in columns.items():
            flat[col_name] = col_info["type"]
    return flat


def schema_to_groups(schema: dict) -> dict[str, list[str]]:
    """
    Return {group_label → [col_name, …]} preserving YAML order.
    Group labels are prettified from the YAML key (underscores → spaces,
    title-cased).
    """
    groups: dict[str, list[str]] = {}
    for group_key, columns in schema.items():
        label = group_key.replace("_", " ").title()
        groups[label] = list(columns.keys())
    return groups


def schema_to_csv_alias_map(schema: dict) -> dict[str, str]:
    """
    Build a lowercased CSV-header → DB-column mapping.
    Uses the 'csv_alias' field if present, otherwise the column name itself.
    """
    mapping: dict[str, str] = {}
    for columns in schema.values():
        for col_name, col_info in columns.items():
            alias = col_info.get("csv_alias", col_name).lower()
            mapping[alias] = col_name
    return mapping


# ──────────────────────────────────────────────────────────────────────
# Database creation
# ──────────────────────────────────────────────────────────────────────

def create_database(db_path: str | Path = DB_FILE,
                    schema_path: str | Path = SCHEMA_FILE) -> duckdb.DuckDBPyConnection:
    """Create (or open) the DuckDB database and ensure the events table exists."""
    con = duckdb.connect(str(db_path))

    schema = load_schema(schema_path)
    all_columns = schema_to_flat_columns(schema)

    cols_sql = ",\n    ".join(f"{col} {dtype}" for col, dtype in all_columns.items())
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS events (
            {cols_sql}
        );
    """)

    # Index on unix_time (the unique row identifier) for fast lookups.
    # DuckDB uses ART indexes; safe to call IF NOT EXISTS.
    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_unix_time
        ON events (unix_time);
    """)

    return con


# ──────────────────────────────────────────────────────────────────────
# Ingestion helpers
# ──────────────────────────────────────────────────────────────────────

# The CSV-to-DB mapping is built dynamically from schema.yaml.
# Extra aliases for CSV headers that don't match the DB column name
# (and aren't covered by the 'csv_alias' field in the YAML) go here:
_EXTRA_CSV_ALIASES = {
    "unixtime":  "unix_time",
    "nrun":      "run_number",
    "timestamp": "unix_time",
}


def _get_csv_to_db_map() -> dict[str, str]:
    """
    Merge the YAML-derived aliases with the extra hardcoded aliases.
    Returns a lowercased-CSV-header → DB-column mapping.
    """
    schema = load_schema()
    mapping = schema_to_csv_alias_map(schema)
    # Extra aliases take lower priority (don't overwrite YAML ones)
    for alias, db_col in _EXTRA_CSV_ALIASES.items():
        mapping.setdefault(alias, db_col)
    return mapping


def _parse_target_header(line: str) -> dict:
    """
    Parse the metadata line at the top of a target CSV file.
    Example: " RunNumber = 80344 - EBeam = 293.5 - SqrtS = 17.3193"
    Returns dict with keys run_number, ebeam, sqrt_s.
    """
    meta = {}
    m = re.search(r"RunNumber\s*=\s*(\d+)", line)
    if m:
        meta["run_number"] = int(m.group(1))
    m = re.search(r"EBeam\s*=\s*([\d.]+)", line)
    if m:
        meta["ebeam"] = float(m.group(1))
    m = re.search(r"SqrtS\s*=\s*([\d.]+)", line)
    if m:
        meta["sqrt_s"] = float(m.group(1))
    return meta


def ingest_target_file(con: duckdb.DuckDBPyConnection, filepath: str | Path) -> int:
    """
    Ingest one target-observable CSV file into the events table.

    File format
    -----------
    Line 1 :  metadata   " RunNumber = XXXXX - EBeam = YYY - SqrtS = ZZZ"
    Line 2 :  CSV header "UnixTime,q_LG,err_q_LG,..."
    Lines 3+: CSV data   "1750091156,714.6400,1.3164,..."

    Returns the number of rows inserted.
    """
    filepath = Path(filepath)
    with open(filepath, "r") as f:
        meta_line = f.readline()
        header_line = f.readline().strip()
        data_lines = [l.strip() for l in f if l.strip()]

    meta = _parse_target_header(meta_line)
    csv_cols = [c.strip().lower() for c in header_line.split(",")]
    csv_to_db = _get_csv_to_db_map()

    # Build the list of DB columns we will insert into
    db_cols = []
    csv_indices = []
    for i, csv_col in enumerate(csv_cols):
        db_col = csv_to_db.get(csv_col)
        if db_col is not None:
            db_cols.append(db_col)
            csv_indices.append(i)

    # Add the metadata columns that come from the header line
    extra_cols = ["run_number", "ebeam", "sqrt_s"]
    all_insert_cols = extra_cols + db_cols

    placeholders = ", ".join(["?"] * len(all_insert_cols))
    insert_sql = f"INSERT INTO events ({', '.join(all_insert_cols)}) VALUES ({placeholders})"

    rows = []
    for line in data_lines:
        vals = line.split(",")
        row = [
            meta.get("run_number"),
            meta.get("ebeam"),
            meta.get("sqrt_s"),
        ]
        for idx in csv_indices:
            row.append(float(vals[idx]))
        rows.append(row)

    con.executemany(insert_sql, rows)
    return len(rows)


def ingest_leadglass_file(con: duckdb.DuckDBPyConnection, filepath: str | Path) -> int:
    """
    Ingest one lead-glass observable file into the events table.

    File format (space-separated, first line is header):
        Nrun Nevent TimeStamp LGCharge LGChargeRMS
        80677 10000 1762641474 688.124 52.0083

    Returns the number of rows inserted.
    """
    filepath = Path(filepath)
    with open(filepath, "r") as f:
        header_line = f.readline().strip()
        data_lines = [l.strip() for l in f if l.strip()]

    csv_cols = [c.strip().lower() for c in header_line.split()]
    csv_to_db = _get_csv_to_db_map()

    db_cols = []
    csv_indices = []
    for i, csv_col in enumerate(csv_cols):
        db_col = csv_to_db.get(csv_col)
        if db_col is not None:
            db_cols.append(db_col)
            csv_indices.append(i)

    placeholders = ", ".join(["?"] * len(db_cols))
    insert_sql = f"INSERT INTO events ({', '.join(db_cols)}) VALUES ({placeholders})"

    rows = []
    for line in data_lines:
        vals = line.split()
        row = []
        for idx in csv_indices:
            val = vals[idx]
            db_col = db_cols[csv_indices.index(idx)]
            if db_col in ("run_number",):
                row.append(int(val))
            elif db_col == "unix_time":
                row.append(int(val))
            else:
                row.append(float(val))
        rows.append(row)

    con.executemany(insert_sql, rows)
    return len(rows)


def ingest_all(con: duckdb.DuckDBPyConnection, base_dir: str | Path = BASE_DIR) -> None:
    """Discover and ingest all available CSV files under base_dir."""
    base_dir = Path(base_dir)

    # Target files
    target_dir = base_dir / "target_observables"
    if target_dir.is_dir():
        files = sorted(target_dir.glob("DB_run_*.txt"))
        for f in files:
            n = ingest_target_file(con, f)
            print(f"  target  | {f.name:50s} | {n:>6d} rows")

    # Lead-glass files
    lg_dir = base_dir / "leadglass_observables"
    if lg_dir.is_dir():
        files = sorted(lg_dir.glob("*.txt"))
        for f in files:
            n = ingest_leadglass_file(con, f)
            print(f"  leadgl  | {f.name:50s} | {n:>6d} rows")


# ──────────────────────────────────────────────────────────────────────
# Diagnostic / query helpers
# ──────────────────────────────────────────────────────────────────────

def print_summary(con: duckdb.DuckDBPyConnection) -> None:
    """Print a quick summary of the database contents."""
    total = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    runs  = con.execute("SELECT COUNT(DISTINCT run_number) FROM events").fetchone()[0]
    cols  = con.execute(
        "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='events'"
    ).fetchone()[0]

    print(f"\n{'='*60}")
    print(f"  PADME Conditions DB  —  {DB_FILE}")
    print(f"{'='*60}")
    print(f"  Total rows:        {total:>12,}")
    print(f"  Distinct runs:     {runs:>12,}")
    print(f"  Columns:           {cols:>12,}")
    print()

    # Per-run breakdown
    print("  Run-level breakdown:")
    rows = con.execute("""
        SELECT run_number,
               COUNT(*) AS n_rows,
               MIN(unix_time) AS t_start,
               MAX(unix_time) AS t_end,
               AVG(ebeam) AS avg_ebeam
        FROM events
        GROUP BY run_number
        ORDER BY run_number
    """).fetchall()
    for r in rows:
        ebeam_str = f"{r[4]:.1f}" if r[4] is not None else "N/A"
        print(f"    run {r[0]:>7d}  |  {r[1]:>5d} rows  |  "
              f"t=[{r[2]}..{r[3]}]  |  EBeam={ebeam_str}")
    print()

    # Column listing
    print("  Column listing:")
    col_rows = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name='events' ORDER BY ordinal_position"
    ).fetchall()
    for cname, ctype in col_rows:
        print(f"    {cname:45s}  {ctype}")
    print()


def interactive_shell(db_path: str | Path = DB_FILE) -> None:
    """Drop into an interactive DuckDB SQL shell."""
    print(f"\nOpening DuckDB shell on {db_path} …")
    print("Type SQL queries, or .exit / Ctrl-D to quit.\n")
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        while True:
            try:
                query = input("padme> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not query or query.lower() in (".exit", "exit", "quit"):
                break
            try:
                result = con.execute(query)
                print(result.fetchdf().to_string())
            except Exception as e:
                print(f"ERROR: {e}")
    finally:
        con.close()


# ──────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="PADME Conditions Database manager (DuckDB)"
    )
    parser.add_argument("--build", action="store_true",
                        help="(Re)create the DB and ingest all example CSV files")
    parser.add_argument("--shell", action="store_true",
                        help="Open an interactive SQL shell on the DB")
    parser.add_argument("--check", action="store_true",
                        help="Print a summary / sanity check of the DB")
    parser.add_argument("--db", type=str, default=DB_FILE,
                        help=f"Path to the DuckDB file (default: {DB_FILE})")
    args = parser.parse_args()

    if not any([args.build, args.shell, args.check]):
        parser.print_help()
        sys.exit(0)

    if args.build:
        db_path = Path(args.db)
        if db_path.exists():
            db_path.unlink()
            print(f"Removed existing {db_path}")
        con = create_database(db_path)
        print("Ingesting data …")
        ingest_all(con)
        print_summary(con)
        con.close()
        size_mb = db_path.stat().st_size / 1024 / 1024
        print(f"Database written to {db_path}  ({size_mb:.2f} MB)\n")

    if args.check:
        con = duckdb.connect(str(args.db), read_only=True)
        print_summary(con)
        con.close()

    if args.shell:
        interactive_shell(args.db)


if __name__ == "__main__":
    main()
