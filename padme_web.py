#!/usr/bin/env python3
"""
PADME Conditions DB — Web Interface (Streamlit + Plotly)

Launch with:
    streamlit run padme_web.py
"""

from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st

DB_FILE = Path(__file__).resolve().parent / "padme_conditions.duckdb"


# ──────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────

def _q(sql: str, params: list | None = None) -> pd.DataFrame:
    con = duckdb.connect(str(DB_FILE), read_only=True)
    df = con.execute(sql, params or []).fetchdf()
    con.close()
    return df


@st.cache_data(ttl=30)
def load_stats() -> dict:
    con = duckdb.connect(str(DB_FILE), read_only=True)
    stats = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("runs", "tags", "conditions", "monitoring")}
    con.close()
    return stats


@st.cache_data(ttl=60)
def load_runs() -> pd.DataFrame:
    return _q("SELECT * FROM runs ORDER BY run_number")


@st.cache_data(ttl=60)
def load_tags() -> pd.DataFrame:
    return _q("SELECT * FROM tags ORDER BY created_at DESC")


@st.cache_data(ttl=60)
def load_run_numbers() -> list[int]:
    return _q("SELECT run_number FROM runs ORDER BY run_number")["run_number"].tolist()


@st.cache_data(ttl=60)
def load_tag_names() -> list[str]:
    return _q("SELECT tag_name FROM tags ORDER BY tag_name")["tag_name"].tolist()


@st.cache_data(ttl=60)
def load_mon_detectors() -> list[str]:
    df = _q("SELECT DISTINCT detector FROM monitoring WHERE detector IS NOT NULL ORDER BY detector")
    return df["detector"].tolist()


@st.cache_data(ttl=60)
def load_mon_quantities(detector: str) -> list[str]:
    df = _q("SELECT DISTINCT quantity FROM monitoring WHERE detector = ? ORDER BY quantity", [detector])
    return df["quantity"].tolist()


@st.cache_data(ttl=60)
def load_cond_detectors() -> list[str]:
    df = _q("SELECT DISTINCT detector FROM conditions WHERE detector IS NOT NULL ORDER BY detector")
    return df["detector"].tolist()


@st.cache_data(ttl=60)
def load_cond_quantities(detector: str | None = None) -> list[str]:
    if detector:
        df = _q("SELECT DISTINCT quantity FROM conditions WHERE detector = ? ORDER BY quantity", [detector])
    else:
        df = _q("SELECT DISTINCT quantity FROM conditions ORDER BY quantity")
    return df["quantity"].tolist()


# ──────────────────────────────────────────────────────────────────────
# Page config
# ──────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="PADME Conditions DB", layout="wide")

db_ok = DB_FILE.exists()

# ──────────────────────────────────────────────────────────────────────
# Sidebar
# ──────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("PADME CondDB")
    st.caption(f"`{DB_FILE.name}`")
    st.divider()

    if db_ok:
        try:
            stats = load_stats()
            st.metric("Runs", stats["runs"])
            st.metric("Tags", stats["tags"])
            st.metric("Conditions", stats["conditions"])
            st.metric("Monitoring rows", f"{stats['monitoring']:,}")
            st.success("Connected", icon="✅")
        except Exception as e:
            st.error(f"DB error: {e}")
            db_ok = False
    else:
        st.error("DB file not found.")
        st.info("Run:\n```\npython padme_conditions_db.py --build\n```")

    st.divider()
    if st.button("↺ Refresh data"):
        st.cache_data.clear()
        st.rerun()

if not db_ok:
    st.stop()

# ──────────────────────────────────────────────────────────────────────
# Tabs
# ──────────────────────────────────────────────────────────────────────

tab_overview, tab_conditions, tab_monitoring, tab_sql = st.tabs([
    "📋 Overview", "🔬 Conditions", "📈 Monitoring", "🛠 SQL",
])


# ══════════════════════════════════════════════════════════════════════
# TAB 1 — Overview
# ══════════════════════════════════════════════════════════════════════

with tab_overview:
    runs_df = load_runs()
    tags_df = load_tags()

    st.subheader("Runs")
    if runs_df.empty:
        st.info("No runs registered yet. Run `--ingest` to populate.")
    else:
        # Colour the is_good flag
        display = runs_df.copy()
        for col in ("start_time", "end_time"):
            if col in display.columns:
                display[col] = pd.to_datetime(display[col], utc=True, errors="coerce")
        st.dataframe(
            display,
            use_container_width=True,
            column_config={
                "is_good": st.column_config.CheckboxColumn("Good?"),
                "ebeam_nominal": st.column_config.NumberColumn("EBeam [MeV]", format="%.1f"),
                "sqrt_s_nominal": st.column_config.NumberColumn("√s [MeV]", format="%.2f"),
            },
        )

    st.divider()
    st.subheader("Tags")
    if tags_df.empty:
        st.info("No tags registered yet.")
    else:
        st.dataframe(tags_df, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════
# TAB 2 — Conditions
# ══════════════════════════════════════════════════════════════════════

with tab_conditions:

    tag_names   = load_tag_names()
    cond_dets   = load_cond_detectors()
    run_numbers = load_run_numbers()

    if not tag_names:
        st.info("No conditions in the database yet.")
        st.stop()

    # ── Browse ────────────────────────────────────────────────────────
    st.subheader("Browse")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        sel_tag = st.selectbox("Tag", tag_names, key="cond_tag")
    with c2:
        sel_det = st.selectbox("Detector", ["(all)"] + cond_dets, key="cond_det")
    with c3:
        cond_qty_opts = load_cond_quantities(sel_det if sel_det != "(all)" else None)
        sel_qty = st.selectbox("Quantity", ["(all)"] + cond_qty_opts, key="cond_qty")
    with c4:
        sel_run = st.selectbox(
            "Valid for run", ["(any)"] + [str(r) for r in run_numbers], key="cond_run"
        )

    where, params = ["tag = ?"], [sel_tag]
    if sel_det != "(all)":
        where.append("detector = ?");   params.append(sel_det)
    if sel_qty != "(all)":
        where.append("quantity = ?");   params.append(sel_qty)
    if sel_run != "(any)":
        where += ["since_run <= ?", "(until_run IS NULL OR until_run >= ?)"]
        params += [int(sel_run), int(sel_run)]

    cond_df = _q(
        f"SELECT condition_id, detector, quantity, value, uncertainty, unit, "
        f"since_run, until_run, source_file, created_at "
        f"FROM conditions WHERE {' AND '.join(where)} "
        f"ORDER BY detector, quantity, since_run",
        params,
    )

    if cond_df.empty:
        st.info("No conditions match.")
    else:
        st.success(f"**{len(cond_df)}** condition(s)")
        st.dataframe(
            cond_df,
            use_container_width=True,
            column_config={
                "value":       st.column_config.NumberColumn(format="%.6g"),
                "uncertainty": st.column_config.NumberColumn(format="%.6g"),
            },
        )
        st.download_button(
            "⬇ Download CSV", cond_df.to_csv(index=False),
            "conditions.csv", "text/csv", key="cond_dl",
        )

    # ── Tag comparison ────────────────────────────────────────────────
    st.divider()
    st.subheader("Compare tags")
    st.caption("Side-by-side view of the same quantity across different processing campaigns.")

    all_cond_qtys = load_cond_quantities()
    if not all_cond_qtys:
        st.info("No conditions to compare yet.")
    else:
        cc1, cc2, cc3 = st.columns(3)
        with cc1:
            comp_qty = st.selectbox("Quantity", all_cond_qtys, key="comp_qty")
        with cc2:
            comp_det = st.selectbox("Detector", ["(all)"] + cond_dets, key="comp_det")
        with cc3:
            comp_run = st.number_input(
                "Run number", min_value=0,
                value=int(run_numbers[0]) if run_numbers else 0,
                key="comp_run",
            )

        comp_where = ["quantity = ?", "since_run <= ?", "(until_run IS NULL OR until_run >= ?)"]
        comp_params = [comp_qty, int(comp_run), int(comp_run)]
        if comp_det != "(all)":
            comp_where.append("detector = ?"); comp_params.append(comp_det)

        comp_df = _q(
            f"SELECT tag, detector, quantity, value, uncertainty, unit, since_run, until_run "
            f"FROM conditions WHERE {' AND '.join(comp_where)} ORDER BY tag",
            comp_params,
        )

        if comp_df.empty:
            st.info("No conditions found for this combination.")
        else:
            st.dataframe(comp_df, use_container_width=True)
            if len(comp_df) > 1:
                has_err = comp_df["uncertainty"].notna().any()
                fig = px.bar(
                    comp_df, x="tag", y="value",
                    error_y="uncertainty" if has_err else None,
                    color="tag",
                    labels={"value": f"{comp_qty}" + (f" [{comp_df['unit'].iloc[0]}]" if comp_df["unit"].notna().any() else "")},
                    title=f"{comp_qty} by tag  (run {comp_run})",
                )
                fig.update_layout(showlegend=False)
                st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════
# TAB 3 — Monitoring
# ══════════════════════════════════════════════════════════════════════

with tab_monitoring:

    mon_dets = load_mon_detectors()
    run_numbers = load_run_numbers()

    if not mon_dets:
        st.info("No monitoring data yet. Run `python padme_conditions_db.py --ingest`.")
        st.stop()

    # ── Time series ───────────────────────────────────────────────────
    st.subheader("Time series")

    ts1, ts2, ts3 = st.columns([1, 2, 2])
    with ts1:
        ts_det = st.selectbox("Detector", mon_dets, key="ts_det")
    qty_list = load_mon_quantities(ts_det)
    with ts2:
        ts_qtys = st.multiselect("Quantities", qty_list, default=qty_list[:1], key="ts_qtys")
    with ts3:
        ts_runs = st.multiselect("Runs", run_numbers, default=run_numbers[:1], key="ts_runs")

    ts_errors = st.checkbox("Show error bars", value=True, key="ts_errors")

    if st.button("Plot", type="primary", key="ts_plot"):
        if not ts_qtys or not ts_runs:
            st.warning("Select at least one quantity and one run.")
        else:
            runs_sql = ", ".join(str(r) for r in ts_runs)
            qty_sql  = ", ".join(f"'{q}'" for q in ts_qtys)
            ts_df = _q(f"""
                SELECT run_number, unix_time, quantity, value, uncertainty, n_events
                FROM monitoring
                WHERE run_number IN ({runs_sql})
                  AND detector = '{ts_det}'
                  AND quantity IN ({qty_sql})
                ORDER BY run_number, unix_time, quantity
            """)

            if ts_df.empty:
                st.warning("No data found for this selection.")
            else:
                ts_df["time"] = pd.to_datetime(ts_df["unix_time"], unit="s", utc=True)
                ts_df["run"]  = "run " + ts_df["run_number"].astype(str)
                has_err = ts_errors and ts_df["uncertainty"].notna().any()

                # Layout: facet by quantity when >1 selected so scales don't mix
                multi_qty = len(ts_qtys) > 1
                multi_run = len(ts_runs) > 1
                color_col = "run" if multi_run else ("quantity" if multi_qty else None)
                facet_col = "quantity" if multi_qty else None

                fig = px.scatter(
                    ts_df, x="time", y="value",
                    error_y="uncertainty" if has_err else None,
                    color=color_col,
                    facet_row=facet_col,
                    hover_data={"unix_time": True, "n_events": True,
                                "uncertainty": ":.4g", "run": True},
                    title=f"{ts_det}  —  {', '.join(ts_qtys)}",
                    height=300 * len(ts_qtys) if multi_qty else 450,
                )
                fig.update_traces(mode="lines+markers")
                if multi_qty:
                    fig.update_yaxes(matches=None)   # independent y-scales per facet
                st.plotly_chart(fig, use_container_width=True)

                with st.expander("Data table"):
                    st.dataframe(ts_df.drop(columns=["time"]), use_container_width=True)
                    st.download_button(
                        "⬇ Download CSV",
                        ts_df.drop(columns=["time"]).to_csv(index=False),
                        "monitoring_timeseries.csv", "text/csv", key="ts_dl",
                    )

    # ── Scatter ───────────────────────────────────────────────────────
    st.divider()
    st.subheader("Scatter")
    st.caption("Plot one quantity against another for the same time slices.")

    sc1, sc2, sc3 = st.columns(3)
    with sc1:
        sc_det = st.selectbox("Detector", mon_dets, key="sc_det")
    sc_qty_list = load_mon_quantities(sc_det)
    with sc2:
        sc_x = st.selectbox("X quantity", sc_qty_list, key="sc_x",
                             index=0)
    with sc3:
        sc_y = st.selectbox("Y quantity", sc_qty_list, key="sc_y",
                             index=min(1, len(sc_qty_list) - 1))

    sc_runs = st.multiselect(
        "Runs", run_numbers,
        default=run_numbers[:min(2, len(run_numbers))],
        key="sc_runs",
    )

    if st.button("Plot scatter", type="primary", key="sc_plot"):
        if not sc_runs:
            st.warning("Select at least one run.")
        else:
            runs_sql = ", ".join(str(r) for r in sc_runs)
            sc_df = _q(f"""
                SELECT run_number, unix_time,
                    MAX(CASE WHEN quantity = '{sc_x}' THEN value END) AS x_val,
                    MAX(CASE WHEN quantity = '{sc_y}' THEN value END) AS y_val
                FROM monitoring
                WHERE run_number IN ({runs_sql})
                  AND detector = '{sc_det}'
                  AND quantity IN ('{sc_x}', '{sc_y}')
                GROUP BY run_number, unix_time
                HAVING x_val IS NOT NULL AND y_val IS NOT NULL
                ORDER BY run_number, unix_time
            """)

            if sc_df.empty:
                st.warning("No data found.")
            else:
                fig = px.scatter(
                    sc_df, x="x_val", y="y_val",
                    color=sc_df["run_number"].astype(str),
                    labels={"x_val": sc_x, "y_val": sc_y, "color": "run"},
                    hover_data={"unix_time": True, "run_number": True},
                    title=f"{sc_x} vs {sc_y}  ({sc_det})",
                )
                st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════
# TAB 4 — Raw SQL
# ══════════════════════════════════════════════════════════════════════

with tab_sql:
    st.subheader("Raw SQL")

    with st.expander("Table reference"):
        st.markdown("""
| Table | Key columns |
|---|---|
| `runs` | `run_number`, `run_type`, `start_time`, `end_time`, `ebeam_nominal`, `sqrt_s_nominal`, `is_good`, `notes` |
| `tags` | `tag_name`, `description`, `created_at`, `created_by` |
| `conditions` | `tag`, `detector`, `quantity`, `value`, `uncertainty`, `unit`, `since_run`, `until_run`, `created_at` |
| `monitoring` | `run_number`, `unix_time`, `detector`, `quantity`, `value`, `uncertainty`, `n_events`, `source_file` |

**Useful patterns**
```sql
-- All conditions valid for run 80344 under a tag
SELECT * FROM conditions
WHERE tag = 'reprocessing_2025v1'
  AND since_run <= 80344 AND (until_run IS NULL OR until_run >= 80344)
ORDER BY detector, quantity;

-- Pivot monitoring to wide format (one column per quantity)
PIVOT (SELECT unix_time, quantity, value FROM monitoring WHERE run_number = 80344 AND detector = 'target')
ON quantity USING first(value) ORDER BY unix_time;

-- Apply a calibration factor inline
SELECT m.unix_time, m.value * c.value AS calibrated_charge
FROM monitoring m
JOIN conditions c ON c.tag = 'reprocessing_2025v1'
  AND c.detector = 'leadglass' AND c.quantity = 'charge_calib_factor'
  AND c.since_run <= m.run_number AND (c.until_run IS NULL OR c.until_run >= m.run_number)
WHERE m.run_number = 80677 AND m.detector = 'leadglass' AND m.quantity = 'lg_charge';
```
""")

    default_sql = (
        "SELECT run_number, detector, quantity, value, uncertainty\n"
        "FROM monitoring\n"
        "WHERE run_number = 80344\n"
        "  AND detector = 'target'\n"
        "ORDER BY unix_time, quantity\n"
        "LIMIT 50"
    )
    user_sql  = st.text_area("Query", value=default_sql, height=160)
    row_limit = st.number_input("Row limit", 10, 100_000, 1_000, step=100, key="sql_limit")

    if st.button("▶ Execute", type="primary", key="sql_run"):
        try:
            df = _q(f"SELECT * FROM ({user_sql}) AS _q LIMIT {row_limit}")
            st.success(f"**{len(df):,}** rows")
            st.dataframe(df, use_container_width=True)
            st.download_button(
                "⬇ Download CSV", df.to_csv(index=False),
                "query_result.csv", "text/csv", key="sql_dl",
            )
        except Exception as e:
            st.error(f"Query error: {e}")

# ──────────────────────────────────────────────────────────────────────
st.markdown("---")
st.caption("PADME Conditions DB · DuckDB + Streamlit + Plotly")
