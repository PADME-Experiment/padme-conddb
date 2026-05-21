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

### Mid-run precision with `unix_time`

By default, `get_conditions()` uses run-number IoV only — it returns every
condition whose `since_run`/`until_run` range covers the requested run,
regardless of when within the run you're asking about.

Pass `unix_time` to also apply the wall-clock IoV (`valid_since`/`valid_until`).
This is useful when a condition changed mid-run (e.g. an HV trip that was
corrected at a known timestamp):

```python
from datetime import datetime, timezone

# Store a run-scoped condition (no wall-clock IoV — always passes through)
insert_condition(con, tag="online_v1", detector="leadglass",
                 quantity="charge_calib_factor",
                 value=1.000, uncertainty=0.010, unit="a.u.",
                 since_run=80000)

# Store a mid-run condition: HV correction applied at 14:30 UTC
insert_condition(con, tag="online_v1", detector="pveto",
                 quantity="hv_correction",
                 value=0.95, uncertainty=0.01, unit="a.u.",
                 since_run=80344, until_run=80344,
                 valid_since=datetime(2025, 6, 16, 14, 30, tzinfo=timezone.utc),
                 valid_until=datetime(2025, 6, 16, 23, 59, tzinfo=timezone.utc))

# Query at 14:00 — HV correction not yet valid, only LG calib returned
t_before = int(datetime(2025, 6, 16, 14,  0, tzinfo=timezone.utc).timestamp())
get_conditions(con, 80344, "online_v1", unix_time=t_before)
# → [leadglass/charge_calib_factor]

# Query at 14:30 — both conditions now valid
t_after = int(datetime(2025, 6, 16, 14, 30, tzinfo=timezone.utc).timestamp())
get_conditions(con, 80344, "online_v1", unix_time=t_after)
# → [leadglass/charge_calib_factor, pveto/hv_correction]
```

**Backward-compatible:** conditions with no `valid_since`/`valid_until` set
always pass through, so existing run-scoped conditions work unchanged whether
or not you pass `unix_time`.

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

## Querying from C++

DuckDB ships a single-file C++ amalgamation (`duckdb.hpp` / `duckdb.cpp`) that you can drop directly into your analysis framework — no external database server required.

### Getting DuckDB

```bash
# conda (easiest if you already use it)
conda install -c conda-forge duckdb

# or download the amalgamation directly
wget https://github.com/duckdb/duckdb/releases/latest/download/libduckdb-src.zip
unzip libduckdb-src.zip -d duckdb/
```

### CMakeLists.txt

```cmake
cmake_minimum_required(VERSION 3.16)
project(padme_analysis)

find_package(DuckDB REQUIRED)          # system/conda install

add_executable(query_conditions query_conditions.cpp)
target_link_libraries(query_conditions PRIVATE duckdb)
```

With the amalgamation instead:

```cmake
add_executable(query_conditions query_conditions.cpp duckdb/duckdb.cpp)
target_include_directories(query_conditions PRIVATE duckdb/)
```

### Fetch a calibration constant for a run

```cpp
#include "duckdb.hpp"
#include <iostream>
#include <stdexcept>

struct Condition {
    double value;
    double uncertainty;   // NaN if NULL
    std::string unit;
};

Condition get_condition(duckdb::Connection& con,
                        int         run_number,
                        const char* tag,
                        const char* detector,
                        const char* quantity)
{
    auto res = con.Query(R"(
        SELECT value, uncertainty, unit
        FROM   conditions
        WHERE  tag       = $1
          AND  detector  = $2
          AND  quantity  = $3
          AND  since_run <= $4
          AND  (until_run IS NULL OR until_run >= $4)
        ORDER BY condition_id DESC
        LIMIT 1
    )", tag, detector, quantity, run_number);

    if (res->HasError())
        throw std::runtime_error(res->GetError());
    if (res->RowCount() == 0)
        throw std::runtime_error("no condition found");

    auto v_val  = res->GetValue(0, 0);
    auto v_unc  = res->GetValue(1, 0);
    auto v_unit = res->GetValue(2, 0);

    return {
        v_val.GetValue<double>(),
        v_unc.IsNull() ? std::numeric_limits<double>::quiet_NaN()
                       : v_unc.GetValue<double>(),
        v_unit.IsNull() ? "" : v_unit.GetValue<std::string>(),
    };
}

int main() {
    duckdb::DuckDB db("padme_conditions.duckdb");
    duckdb::Connection con(db);

    auto c = get_condition(con, 80344,
                           "reprocessing_2025v1",
                           "leadglass", "charge_calib_factor");

    std::cout << "charge_calib_factor = " << c.value
              << " ± " << c.uncertainty
              << " " << c.unit << "\n";
    return 0;
}
```

### Fetch monitoring time-series for a run

```cpp
#include "duckdb.hpp"
#include <iostream>
#include <vector>

struct Slice {
    int64_t unix_time;
    double  value;
    double  uncertainty;
    int32_t n_events;
};

std::vector<Slice> get_monitoring(duckdb::Connection& con,
                                  int         run_number,
                                  const char* detector,
                                  const char* quantity)
{
    auto res = con.Query(R"(
        SELECT unix_time, value, uncertainty, n_events
        FROM   monitoring
        WHERE  run_number = $1
          AND  detector   = $2
          AND  quantity   = $3
        ORDER BY unix_time
    )", run_number, detector, quantity);

    if (res->HasError())
        throw std::runtime_error(res->GetError());

    std::vector<Slice> slices;
    slices.reserve(res->RowCount());

    for (duckdb::idx_t row = 0; row < res->RowCount(); ++row) {
        auto v_unc      = res->GetValue(2, row);
        auto v_n_events = res->GetValue(3, row);
        slices.push_back({
            res->GetValue(0, row).GetValue<int64_t>(),
            res->GetValue(1, row).GetValue<double>(),
            v_unc.IsNull()      ? 0.0 : v_unc.GetValue<double>(),
            v_n_events.IsNull() ? 0   : v_n_events.GetValue<int32_t>(),
        });
    }
    return slices;
}

int main() {
    duckdb::DuckDB db("padme_conditions.duckdb");
    duckdb::Connection con(db);

    auto slices = get_monitoring(con, 80677, "leadglass", "lg_charge");

    for (const auto& s : slices)
        std::cout << s.unix_time << "  " << s.value
                  << " ± " << s.uncertainty
                  << "  (" << s.n_events << " events)\n";
    return 0;
}
```

### Combining both: apply a calibration inline

```cpp
// Pull the calib factor and apply it to the raw time-series in one SQL query
auto res = con.Query(R"(
    SELECT m.unix_time,
           m.value * c.value  AS calibrated_charge,
           m.n_events
    FROM   monitoring m
    JOIN   conditions c
           ON  c.tag      = $1
           AND c.detector = 'leadglass'
           AND c.quantity = 'charge_calib_factor'
           AND c.since_run <= m.run_number
           AND (c.until_run IS NULL OR c.until_run >= m.run_number)
    WHERE  m.run_number = $2
      AND  m.detector   = 'leadglass'
      AND  m.quantity   = 'lg_charge'
    ORDER BY m.unix_time
)", "reprocessing_2025v1", 80677);
```

This pushes the calibration application into DuckDB and returns already-corrected values — no loop needed on the C++ side.

## Extending the schema

Add new tables or columns to `schema.yaml`, then run:

```bash
python padme_conditions_db.py --build
```

New tables are created; existing tables are left untouched (`CREATE TABLE IF NOT EXISTS`). To add a new sub-detector, just start ingesting rows with a new `detector` label — no schema change required for the `monitoring` table.
