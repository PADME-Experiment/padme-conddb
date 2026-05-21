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
import re
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
    unix_time: int | None = None,
) -> list[dict]:
    """
    Return all conditions valid for run_number under tag.
    Optionally narrow by detector and/or quantity.

    unix_time (optional) — Unix timestamp [seconds] for mid-run precision.
    When provided, also filters on the wall-clock IoV columns:
        (valid_since IS NULL OR valid_since <= ts)
        AND (valid_until IS NULL OR valid_until >= ts)
    Conditions with no wall-clock IoV set (valid_since/valid_until both NULL)
    always pass through, so this is fully backward-compatible.
    """
    filters = ["tag = ?", "since_run <= ?", "(until_run IS NULL OR until_run >= ?)"]
    params: list = [tag, run_number, run_number]

    if unix_time is not None:
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(unix_time, tz=timezone.utc)
        filters += [
            "(valid_since IS NULL OR valid_since <= ?)",
            "(valid_until IS NULL OR valid_until >= ?)",
        ]
        params += [ts, ts]

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
# Ingestion helpers
# ──────────────────────────────────────────────────────────────────────

def _to_quantity(raw: str) -> str:
    """Normalise a CSV column header to a snake_case quantity name."""
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', raw)   # LGCharge → LG_Charge
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s)         # qXTarget → q_X_Target
    return s.lower()


def _pair_columns(headers: list[str]) -> dict[str, tuple[str, str | None]]:
    """
    Return {csv_col: (quantity_name, err_csv_col_or_None)} for every
    value column.  Columns whose name starts with 'err_' are recognised
    as uncertainties and excluded as standalone entries.
    """
    lower_to_raw = {h.lower(): h for h in headers}
    result: dict[str, tuple[str, str | None]] = {}
    for raw in headers:
        if raw.lower().startswith("err_"):
            continue
        err_raw = lower_to_raw.get("err_" + raw.lower())
        result[raw] = (_to_quantity(raw), err_raw)
    return result


def _parse_run_meta(line: str) -> dict:
    """Parse the metadata header line from a target observable file.
    Example: ' RunNumber = 80344 - EBeam = 293.5 - SqrtS = 17.3193'
    """
    meta: dict = {}
    for pattern, key, cast in [
        (r"RunNumber\s*=\s*(\d+)",      "run_number",    int),
        (r"EBeam\s*=\s*([\d.]+)",       "ebeam_nominal", float),
        (r"SqrtS\s*=\s*([\d.]+)",       "sqrt_s_nominal",float),
    ]:
        m = re.search(pattern, line)
        if m:
            meta[key] = cast(m.group(1))
    return meta


def ingest_target_file(con: duckdb.DuckDBPyConnection,
                       filepath: str | Path) -> int:
    """
    Ingest one target-observable CSV into the monitoring table.

    File format
    -----------
    Line 1  metadata   ' RunNumber = XXXXX - EBeam = YYY - SqrtS = ZZZ'
    Line 2  CSV header 'UnixTime,q_LG,err_q_LG,qX_Target,err_qX_Target,…'
    Lines 3+ data rows '1750091156,714.64,1.32,734.35,9.55,…'

    Returns the number of time-slice rows ingested.
    """
    filepath = Path(filepath)
    with open(filepath) as f:
        meta_line   = f.readline()
        header_line = f.readline().strip()
        data_lines  = [l.strip() for l in f if l.strip()]

    meta = _parse_run_meta(meta_line)
    upsert_run(con, meta["run_number"],
               ebeam_nominal=meta.get("ebeam_nominal"),
               sqrt_s_nominal=meta.get("sqrt_s_nominal"))

    headers    = [h.strip() for h in header_line.split(",")]
    col_index  = {h: i for i, h in enumerate(headers)}
    time_col   = next(
        h for h in headers if h.lower() in ("unixtime", "unix_time", "timestamp")
    )
    time_idx   = col_index[time_col]
    pairs      = _pair_columns([h for h in headers if h != time_col])

    monitoring_rows = []
    for line in data_lines:
        vals     = line.split(",")
        unix_time = int(float(vals[time_idx]))
        for csv_col, (quantity, err_col) in pairs.items():
            try:
                value = float(vals[col_index[csv_col]])
            except (ValueError, IndexError):
                continue
            uncertainty = None
            if err_col:
                try:
                    uncertainty = float(vals[col_index[err_col]])
                except (ValueError, IndexError):
                    pass
            monitoring_rows.append(dict(
                run_number  = meta["run_number"],
                unix_time   = unix_time,
                detector    = "target",
                quantity    = quantity,
                value       = value,
                uncertainty = uncertainty,
                source_file = str(filepath),
            ))

    insert_monitoring_rows(con, monitoring_rows)
    return len(data_lines)


def ingest_leadglass_file(con: duckdb.DuckDBPyConnection,
                          filepath: str | Path) -> int:
    """
    Ingest one lead-glass observable file into the monitoring table.

    File format (space-separated)
    ------------------------------
    Nrun  Nevent  TimeStamp  LGCharge  LGChargeRMS
    80677  10000  1762641474  688.124  52.0083

    Nevent is a cumulative event counter; n_events per slice is the
    step between consecutive rows (or the value itself for the first row).

    Returns the number of time-slice rows ingested.
    """
    filepath = Path(filepath)
    with open(filepath) as f:
        header_line = f.readline().strip()
        data_lines  = [l.strip() for l in f if l.strip()]

    headers    = header_line.split()
    lower      = [h.lower() for h in headers]
    run_idx    = lower.index("nrun")
    event_idx  = lower.index("nevent")
    time_idx   = lower.index("timestamp")

    skip = {run_idx, event_idx, time_idx}
    quantity_cols = [
        (i, _to_quantity(headers[i]))
        for i in range(len(headers))
        if i not in skip
    ]

    monitoring_rows = []
    prev_nevent: dict[int, int] = {}
    seen_runs: set[int] = set()

    for line in data_lines:
        vals       = line.split()
        run_number = int(vals[run_idx])
        nevent     = int(vals[event_idx])
        unix_time  = int(vals[time_idx])
        n_events   = nevent - prev_nevent.get(run_number, 0)
        prev_nevent[run_number] = nevent
        seen_runs.add(run_number)

        for idx, quantity in quantity_cols:
            try:
                value = float(vals[idx])
            except (ValueError, IndexError):
                continue
            monitoring_rows.append(dict(
                run_number  = run_number,
                unix_time   = unix_time,
                detector    = "leadglass",
                quantity    = quantity,
                value       = value,
                n_events    = n_events,
                source_file = str(filepath),
            ))

    for run_number in seen_runs:
        upsert_run(con, run_number)

    insert_monitoring_rows(con, monitoring_rows)
    return len(data_lines)


def ingest_all(con: duckdb.DuckDBPyConnection,
               base_dir: str | Path = BASE_DIR) -> None:
    """Discover and ingest all known data files under base_dir."""
    base_dir = Path(base_dir)

    target_dir = base_dir / "target_observables"
    if target_dir.is_dir():
        for f in sorted(target_dir.glob("DB_run_*.txt")):
            n = ingest_target_file(con, f)
            print(f"  target    | {f.name:<50s} | {n:>5d} slices")

    lg_dir = base_dir / "leadglass_observables"
    if lg_dir.is_dir():
        for f in sorted(lg_dir.glob("*.txt")):
            n = ingest_leadglass_file(con, f)
            print(f"  leadglass | {f.name:<50s} | {n:>5d} slices")


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
    parser.add_argument("--ingest", action="store_true",
                        help="Ingest all data files found under the repo directory")
    parser.add_argument("--db", default=DB_FILE,
                        help=f"Path to the DuckDB file (default: {DB_FILE})")
    args = parser.parse_args()

    if not any([args.build, args.check, args.shell, args.ingest]):
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

    if args.ingest:
        con = open_db(args.db)
        print("Ingesting data …")
        ingest_all(con)
        print_summary(con)
        con.close()

    if args.check:
        con = duckdb.connect(str(args.db), read_only=True)
        print_summary(con)
        con.close()

    if args.shell:
        interactive_shell(args.db)


if __name__ == "__main__":
    main()
