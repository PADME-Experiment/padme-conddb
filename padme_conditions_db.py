#!/usr/bin/env python3
"""
PADME Conditions Database — built on DuckDB.

Four tables driven by schema.yaml:
  runs        — immutable per-run metadata
  tags        — named condition snapshots (versioning)
  conditions  — IoV-scoped, versioned scalar conditions
  monitoring  — dense time-series observables (EAV layout)

Usage
-----
    python padme_conditions_db.py --build          # create/migrate tables
    python padme_conditions_db.py --check          # print summary
    python padme_conditions_db.py --shell          # interactive SQL shell
    python padme_conditions_db.py --fresh --build  # wipe and rebuild from scratch
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import yaml

DB_FILE = "padme_conditions.duckdb"
BASE_DIR = Path(__file__).resolve().parent
SCHEMA_FILE = BASE_DIR / "schema.yaml"


# ──────────────────────────────────────────────────────────────────────
# Schema loading
# ──────────────────────────────────────────────────────────────────────

def load_schema(schema_path: str | Path = SCHEMA_FILE) -> dict:
    with open(schema_path) as f:
        return yaml.safe_load(f)


# ──────────────────────────────────────────────────────────────────────
# Table creation
# ──────────────────────────────────────────────────────────────────────

def _build_col_sql(col_name: str, col_info: dict, sequences: list[str]) -> str:
    col_type = col_info["type"]
    parts = [col_name, col_type]

    if col_info.get("auto_increment"):
        seq = f"seq_{col_name}"
        sequences.append(f"CREATE SEQUENCE IF NOT EXISTS {seq} START 1;")
        parts.append(f"DEFAULT nextval('{seq}')")

    elif "default" in col_info:
        parts.append(f"DEFAULT {col_info['default']}")

    return " ".join(parts)


def create_tables(con: duckdb.DuckDBPyConnection, schema: dict) -> None:
    for table_name, table_def in schema.get("tables", {}).items():
        columns = table_def.get("columns", {})
        sequences: list[str] = []
        col_defs: list[str] = []
        pk_cols: list[str] = []
        fk_defs: list[str] = []

        for col_name, col_info in columns.items():
            col_defs.append(_build_col_sql(col_name, col_info, sequences))
            if col_info.get("primary_key"):
                pk_cols.append(col_name)
            if fk := col_info.get("fk"):
                ref_table, ref_col = fk.split(".")
                fk_defs.append(
                    f"FOREIGN KEY ({col_name}) REFERENCES {ref_table}({ref_col})"
                )

        if pk_cols:
            col_defs.append(f"PRIMARY KEY ({', '.join(pk_cols)})")
        col_defs.extend(fk_defs)

        for seq_sql in sequences:
            con.execute(seq_sql)

        con.execute(
            f"CREATE TABLE IF NOT EXISTS {table_name} (\n    "
            + ",\n    ".join(col_defs)
            + "\n);"
        )

        for col_name, col_info in columns.items():
            if col_info.get("index"):
                con.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_{col_name} "
                    f"ON {table_name} ({col_name});"
                )

        print(f"  table ready: {table_name}")


def open_db(
    db_path: str | Path = DB_FILE,
    schema_path: str | Path = SCHEMA_FILE,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(db_path), read_only=read_only)
    if not read_only:
        create_tables(con, load_schema(schema_path))
    return con


# ──────────────────────────────────────────────────────────────────────
# runs helpers
# ──────────────────────────────────────────────────────────────────────

def upsert_run(con: duckdb.DuckDBPyConnection, run_number: int, **kwargs) -> None:
    """Insert or replace a row in the runs table."""
    kwargs["run_number"] = run_number
    cols = ", ".join(kwargs)
    placeholders = ", ".join(["?"] * len(kwargs))
    con.execute(
        f"INSERT OR REPLACE INTO runs ({cols}) VALUES ({placeholders})",
        list(kwargs.values()),
    )


# ──────────────────────────────────────────────────────────────────────
# tags helpers
# ──────────────────────────────────────────────────────────────────────

def create_tag(
    con: duckdb.DuckDBPyConnection,
    tag_name: str,
    description: str = "",
    created_by: str = "",
) -> None:
    """Register a new tag. Silently no-ops if the tag already exists."""
    con.execute(
        "INSERT OR IGNORE INTO tags (tag_name, description, created_by) VALUES (?, ?, ?)",
        [tag_name, description, created_by],
    )


# ──────────────────────────────────────────────────────────────────────
# conditions helpers
# ──────────────────────────────────────────────────────────────────────

def insert_condition(
    con: duckdb.DuckDBPyConnection,
    tag: str,
    quantity: str,
    value: float,
    *,
    detector: str | None = None,
    uncertainty: float | None = None,
    unit: str | None = None,
    since_run: int | None = None,
    until_run: int | None = None,
    valid_since=None,
    valid_until=None,
    source_file: str | None = None,
    notes: str | None = None,
) -> int:
    """
    Insert one condition row. Returns the new condition_id.

    Example
    -------
    insert_condition(con, tag="reprocessing_2025v1",
                     detector="leadglass", quantity="charge_calib",
                     value=1.042, uncertainty=0.003, unit="a.u.",
                     since_run=80000, until_run=81000)
    """
    row = {
        "tag": tag,
        "detector": detector,
        "quantity": quantity,
        "value": value,
        "uncertainty": uncertainty,
        "unit": unit,
        "since_run": since_run,
        "until_run": until_run,
        "valid_since": valid_since,
        "valid_until": valid_until,
        "source_file": source_file,
        "notes": notes,
    }
    row = {k: v for k, v in row.items() if v is not None}
    cols = ", ".join(row)
    placeholders = ", ".join(["?"] * len(row))
    result = con.execute(
        f"INSERT INTO conditions ({cols}) VALUES ({placeholders}) RETURNING condition_id",
        list(row.values()),
    ).fetchone()
    return result[0]


def get_conditions(
    con: duckdb.DuckDBPyConnection,
    run_number: int,
    tag: str,
    detector: str | None = None,
    quantity: str | None = None,
) -> list[dict]:
    """
    Return all conditions valid for run_number under tag.
    Optionally narrow by detector and/or quantity.

    A condition row is valid for run_number when:
        since_run <= run_number AND (until_run IS NULL OR until_run >= run_number)
    """
    filters = ["tag = ?", "since_run <= ?", "(until_run IS NULL OR until_run >= ?)"]
    params: list = [tag, run_number, run_number]

    if detector:
        filters.append("detector = ?")
        params.append(detector)
    if quantity:
        filters.append("quantity = ?")
        params.append(quantity)

    rows = con.execute(
        f"SELECT * FROM conditions WHERE {' AND '.join(filters)} "
        "ORDER BY detector, quantity",
        params,
    ).fetchall()
    col_names = [d[0] for d in con.description]
    return [dict(zip(col_names, r)) for r in rows]


# ──────────────────────────────────────────────────────────────────────
# monitoring helpers
# ──────────────────────────────────────────────────────────────────────

def insert_monitoring_rows(
    con: duckdb.DuckDBPyConnection,
    rows: list[dict],
) -> int:
    """
    Bulk-insert monitoring rows.  Each dict must have at minimum:
        run_number, unix_time, detector, quantity, value

    Returns the number of rows inserted.
    """
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(cols))
    data = [[r[c] for c in cols] for r in rows]
    con.executemany(
        f"INSERT INTO monitoring ({', '.join(cols)}) VALUES ({placeholders})",
        data,
    )
    return len(data)


# ──────────────────────────────────────────────────────────────────────
# Diagnostics
# ──────────────────────────────────────────────────────────────────────

def print_summary(con: duckdb.DuckDBPyConnection) -> None:
    def n(table):
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    sep = "=" * 64
    print(f"\n{sep}")
    print("  PADME Conditions DB")
    print(sep)
    print(f"  {'runs':<20} {n('runs'):>10,} rows")
    print(f"  {'tags':<20} {n('tags'):>10,} rows")
    print(f"  {'conditions':<20} {n('conditions'):>10,} rows")
    print(f"  {'monitoring':<20} {n('monitoring'):>10,} rows")
    print()

    tags = con.execute(
        "SELECT tag_name, description, created_at FROM tags ORDER BY created_at"
    ).fetchall()
    if tags:
        print("  Tags:")
        for tag_name, desc, ts in tags:
            print(f"    {tag_name:30s}  {desc or '':40s}  {ts or ''}")
        print()

    runs = con.execute(
        "SELECT run_number, run_type, start_time, ebeam_nominal, is_good "
        "FROM runs ORDER BY run_number"
    ).fetchall()
    if runs:
        print("  Runs:")
        for run_number, rtype, t_start, ebeam, good in runs:
            quality = "OK " if good else "BAD"
            e_str = f"{ebeam:.1f} MeV" if ebeam else "?"
            print(f"    {run_number:>8d}  {str(rtype or ''):15s}  "
                  f"{e_str:12s}  [{quality}]  {t_start or ''}")
        print()

    cond_groups = con.execute("""
        SELECT tag, detector, quantity, COUNT(*) AS n_iov
        FROM conditions
        GROUP BY tag, detector, quantity
        ORDER BY tag, detector, quantity
    """).fetchall()
    if cond_groups:
        print("  Conditions (tag / detector / quantity / #IoVs):")
        for tag, det, qty, n_iov in cond_groups:
            print(f"    {str(tag):25s}  {str(det or '?'):15s}  "
                  f"{str(qty):35s}  {n_iov} IoV(s)")
        print()


def interactive_shell(db_path: str | Path = DB_FILE) -> None:
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
                print(con.execute(query).fetchdf().to_string())
            except Exception as e:
                print(f"ERROR: {e}")
    finally:
        con.close()


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="PADME Conditions Database manager (DuckDB)"
    )
    parser.add_argument("--build", action="store_true",
                        help="Create / migrate tables from schema.yaml")
    parser.add_argument("--fresh", action="store_true",
                        help="Delete the DB file before --build (full rebuild)")
    parser.add_argument("--check", action="store_true",
                        help="Print a summary of the DB contents")
    parser.add_argument("--shell", action="store_true",
                        help="Open an interactive SQL shell")
    parser.add_argument("--db", default=DB_FILE,
                        help=f"Path to the DuckDB file (default: {DB_FILE})")
    args = parser.parse_args()

    if not any([args.build, args.check, args.shell]):
        parser.print_help()
        sys.exit(0)

    if args.build:
        db_path = Path(args.db)
        if args.fresh and db_path.exists():
            db_path.unlink()
            print(f"Removed {db_path}")
        print(f"Building {db_path} …")
        con = open_db(db_path)
        print_summary(con)
        con.close()
        size_mb = db_path.stat().st_size / 1024 / 1024
        print(f"Done. {db_path}  ({size_mb:.3f} MB)\n")

    if args.check:
        con = duckdb.connect(str(args.db), read_only=True)
        print_summary(con)
        con.close()

    if args.shell:
        interactive_shell(args.db)


if __name__ == "__main__":
    main()
