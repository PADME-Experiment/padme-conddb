# PADME Conditions Database

A lightweight conditions database for the PADME experiment, built on [DuckDB](https://duckdb.org/).

## Schema

Four tables, each with a distinct purpose:

| Table | Description |
|---|---|
| `runs` | Immutable per-run metadata (beam energy, run type, quality flag) |
| `tags` | Named condition snapshots — one per processing campaign |
| `conditions` | IoV-scoped scalar conditions (calibration constants, alignment, …) |
| `monitoring` | Dense time-series observables within a run (EAV layout) |

The core idea is the **Interval of Validity (IoV)**: every condition is valid from `since_run` to `until_run` (inclusive; `NULL` = open-ended), under a named `tag`. This lets you store multiple versions of the same quantity and retrieve the right one by run number and tag.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install duckdb pyyaml pytz pandas
```

## CLI

```bash
# Create the database tables (safe to re-run — uses CREATE IF NOT EXISTS)
python padme_conditions_db.py --build

# Wipe and rebuild from scratch
python padme_conditions_db.py --fresh --build

# Ingest all data files found under target_observables/ and leadglass_observables/
python padme_conditions_db.py --ingest

# Print a summary of what's in the DB
python padme_conditions_db.py --check

# Interactive SQL shell
python padme_conditions_db.py --shell
```

## Python API examples

### Register a run

```python
from padme_conditions_db import open_db, upsert_run

con = open_db()

upsert_run(con,
    run_number     = 80344,
    run_type       = "production",
    ebeam_nominal  = 293.5,
    sqrt_s_nominal = 17.32,
    is_good        = True,
    notes          = "nominal conditions",
)
```

### Create a tag and insert conditions

```python
from padme_conditions_db import create_tag, insert_condition

# Create a tag once per processing campaign
create_tag(con, "reprocessing_2025v1", description="Updated LG calibration")

# Insert a calibration constant valid from run 80000 onwards
insert_condition(con,
    tag         = "reprocessing_2025v1",
    detector    = "leadglass",
    quantity    = "charge_calib_factor",
    value       = 1.042,
    uncertainty = 0.003,
    unit        = "a.u.",
    since_run   = 80000,          # until_run=None → open-ended
)

# A condition valid only over a specific run range
insert_condition(con,
    tag         = "reprocessing_2025v1",
    detector    = "target",
    quantity    = "x_offset_mm",
    value       = 0.15,
    uncertainty = 0.02,
    unit        = "mm",
    since_run   = 80344,
    until_run   = 80500,
)
```

### Query conditions for a run

```python
from padme_conditions_db import get_conditions

# All conditions valid for run 80344 under a given tag
conds = get_conditions(con, run_number=80344, tag="reprocessing_2025v1")

# Narrow to one detector and quantity
calib = get_conditions(con, run_number=80344, tag="reprocessing_2025v1",
                       detector="leadglass", quantity="charge_calib_factor")
print(calib[0]["value"], "±", calib[0]["uncertainty"])
# → 1.042 ± 0.003
```

### Versioning — same quantity, different tags

```python
create_tag(con, "online_v1",           description="First online pass")
create_tag(con, "reprocessing_2025v1", description="Updated LG calibration")

insert_condition(con, tag="online_v1",
                 detector="leadglass", quantity="charge_calib_factor",
                 value=1.000, uncertainty=0.010, unit="a.u.", since_run=80000)

insert_condition(con, tag="reprocessing_2025v1",
                 detector="leadglass", quantity="charge_calib_factor",
                 value=1.042, uncertainty=0.003, unit="a.u.", since_run=80000)

# Each tag returns its own version — old data is never overwritten
for tag in ("online_v1", "reprocessing_2025v1"):
    c = get_conditions(con, 80344, tag, quantity="charge_calib_factor")[0]
    print(f"{tag:25s}  {c['value']} ± {c['uncertainty']}")
```

### Ingest data files

```python
from padme_conditions_db import ingest_target_file, ingest_leadglass_file, ingest_all

# Ingest a single file
n = ingest_target_file(con, "target_observables/DB_run_0080344_NewReco_100files.txt")
print(f"{n} time slices ingested")

# Or ingest everything at once
ingest_all(con)
```

### Query monitoring data

```python
import duckdb

# Raw time-series for one run and detector
df = con.execute("""
    SELECT unix_time, quantity, value, uncertainty, n_events
    FROM monitoring
    WHERE run_number = 80344 AND detector = 'target'
    ORDER BY unix_time, quantity
""").fetchdf()

# Pivot to a wide table (one column per quantity)
wide = con.execute("""
    PIVOT (
        SELECT unix_time, quantity, value
        FROM monitoring
        WHERE run_number = 80344 AND detector = 'target'
    )
    ON quantity
    USING first(value)
    ORDER BY unix_time
""").fetchdf()
```

### Apply a calibration to monitoring data

```python
calib = get_conditions(con, run_number=80344, tag="reprocessing_2025v1",
                       detector="leadglass", quantity="charge_calib_factor")[0]

df = con.execute("""
    SELECT unix_time, value FROM monitoring
    WHERE run_number = 80344 AND detector = 'leadglass' AND quantity = 'lg_charge'
    ORDER BY unix_time
""").fetchdf()

df["calibrated_charge"] = df["value"] * calib["value"]
```

## Extending the schema

Add new tables or columns to `schema.yaml`, then run:

```bash
python padme_conditions_db.py --build
```

New tables are created; existing tables are left untouched (`CREATE TABLE IF NOT EXISTS`). To add a new sub-detector, just start ingesting rows with a new `detector` label — no schema change required for the `monitoring` table.
