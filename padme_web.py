#!/usr/bin/env python3
"""
PADME Conditions DB — Web Interface (Streamlit)

Launch with:
    streamlit run padme_web.py

No SQL knowledge required. Point-and-click filtering, plotting, and export.
"""

import datetime
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st
import yaml

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────
DB_FILE = Path(__file__).resolve().parent / "padme_conditions.duckdb"
SCHEMA_FILE = Path(__file__).resolve().parent / "schema.yaml"


def _load_column_groups() -> dict[str, list[str]]:
    """
    Load column groups from schema.yaml.
    Returns {Pretty Group Label → [col_name, …]}.
    """
    with open(SCHEMA_FILE, "r") as f:
        schema = yaml.safe_load(f)
    groups: dict[str, list[str]] = {}
    for group_key, columns in schema.items():
        label = group_key.replace("_", " ").title()
        groups[label] = list(columns.keys())
    return groups


COLUMN_GROUPS = _load_column_groups()
ALL_COLUMNS = [col for cols in COLUMN_GROUPS.values() for col in cols]


# ──────────────────────────────────────────────────────────────────────
# Database helpers (cached)
# ──────────────────────────────────────────────────────────────────────

@st.cache_resource
def get_connection():
    """Persistent read-only connection (cached across reruns)."""
    return duckdb.connect(str(DB_FILE), read_only=True)


@st.cache_data(ttl=600)
def get_run_list():
    con = get_connection()
    return con.execute(
        "SELECT DISTINCT run_number FROM events ORDER BY run_number"
    ).fetchdf()["run_number"].tolist()


@st.cache_data(ttl=600)
def get_db_stats():
    con = get_connection()
    total = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    runs = con.execute("SELECT COUNT(DISTINCT run_number) FROM events").fetchone()[0]
    cols = con.execute(
        "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='events'"
    ).fetchone()[0]
    return total, runs, cols


@st.cache_data(ttl=600)
def get_ebeam_range():
    con = get_connection()
    row = con.execute(
        "SELECT MIN(ebeam), MAX(ebeam) FROM events WHERE ebeam IS NOT NULL"
    ).fetchone()
    return float(row[0]), float(row[1])


def run_query(sql: str) -> pd.DataFrame:
    con = get_connection()
    return con.execute(sql).fetchdf()


# ──────────────────────────────────────────────────────────────────────
# Page setup
# ──────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="PADME Conditions DB",
    page_icon="⚛️",
    layout="wide",
)
st.title("⚛️ PADME Conditions Database")

total_rows, n_runs, n_cols = get_db_stats()
st.caption(
    f"**{total_rows:,}** rows · **{n_runs}** runs · **{n_cols}** columns · "
    f"DB file: `{DB_FILE.name}`"
)

# ──────────────────────────────────────────────────────────────────────
# Tabs
# ──────────────────────────────────────────────────────────────────────
tab_browse, tab_plot, tab_sql = st.tabs(["🔍 Browse & Filter", "📊 Plot", "🛠️ Raw SQL"])

# ──────────────────────────────────────────────────────────────────────
# Dynamic filter builder helpers
# ──────────────────────────────────────────────────────────────────────
FILTER_OPERATORS = [
    "=", "!=", ">", ">=", "<", "<=",
    "between", "is null", "is not null",
]

# Operators that don't need a value
_NO_VALUE_OPS = {"is null", "is not null"}


def _init_filters():
    """Ensure session-state list for dynamic filters exists."""
    if "filter_ids" not in st.session_state:
        st.session_state.filter_ids = []      # list of unique int IDs
        st.session_state.filter_next_id = 0   # monotonically increasing counter


def _add_filter():
    """Append a new filter ID."""
    st.session_state.filter_ids.append(st.session_state.filter_next_id)
    st.session_state.filter_next_id += 1


def _remove_filter(fid: int):
    """Remove a filter by ID and clean up its widget keys from session state."""
    if fid in st.session_state.filter_ids:
        st.session_state.filter_ids.remove(fid)
    # Clean up widget state so stale values don't persist if the ID is reused
    for suffix in ("col", "op", "val", "lo", "hi", "rm", "prev_op"):
        key = f"filt_{suffix}_{fid}"
        st.session_state.pop(key, None)


def _clear_all_filters():
    """Remove every filter and clean up all widget keys."""
    for fid in list(st.session_state.filter_ids):
        _remove_filter(fid)
    st.session_state.filter_ids = []


def _render_filter_row(fid: int):
    """Render one filter row and return (column, operator, value(s))."""
    c1, c2, c3, c4 = st.columns([3, 2, 3, 1])

    with c1:
        col = st.selectbox("Column", ALL_COLUMNS, key=f"filt_col_{fid}")
    with c2:
        op = st.selectbox("Operator", FILTER_OPERATORS, key=f"filt_op_{fid}")

    # Detect operator change: if user switches away from a no-value operator,
    # reset the value field so it doesn't keep showing "(no value needed)".
    prev_op_key = f"filt_prev_op_{fid}"
    prev_op = st.session_state.get(prev_op_key)
    if prev_op is not None and prev_op != op:
        # Operator just changed — clear stale value widgets
        for suffix in ("val", "lo", "hi"):
            st.session_state.pop(f"filt_{suffix}_{fid}", None)
    st.session_state[prev_op_key] = op

    with c3:
        if op in _NO_VALUE_OPS:
            val = None
            st.text_input("Value", value="", disabled=True,
                          key=f"filt_val_{fid}")
        elif op == "between":
            lo = st.text_input("Min", value="", key=f"filt_lo_{fid}")
            hi = st.text_input("Max", value="", key=f"filt_hi_{fid}")
            val = (lo.strip(), hi.strip())
        else:
            val = st.text_input("Value", value="", key=f"filt_val_{fid}")
            val = val.strip()
    with c4:
        st.markdown("<div style='height:1.8rem'></div>", unsafe_allow_html=True)
        if st.button("✕", key=f"filt_rm_{fid}"):
            _remove_filter(fid)
            st.rerun()

    return col, op, val


def _filter_to_sql(col: str, op: str, val) -> str | None:
    """Convert one filter row into a SQL WHERE clause fragment."""
    if op == "is null":
        return f"{col} IS NULL"
    if op == "is not null":
        return f"{col} IS NOT NULL"
    if op == "between":
        lo, hi = val
        if not lo or not hi:
            return None
        return f"{col} BETWEEN {float(lo)} AND {float(hi)}"
    if not val:
        return None
    # Try to interpret as number, otherwise quote as string
    try:
        num = float(val)
        # Keep as int if it looks like one (e.g. run_number = 80677)
        if num == int(num) and "." not in val:
            return f"{col} {op} {int(num)}"
        return f"{col} {op} {num}"
    except ValueError:
        safe = val.replace("'", "''")
        return f"{col} {op} '{safe}'"


# ========================  TAB 1: Browse & Filter  ====================
with tab_browse:
    st.subheader("Filter events")

    # ---------- Quick filters (run, beam energy) ----------------------
    qf1, qf2 = st.columns(2)
    with qf1:
        all_runs = get_run_list()
        selected_runs = st.multiselect(
            "Run number(s)",
            options=all_runs,
            default=[],
            help="Leave empty to include all runs",
        )
    with qf2:
        ebeam_min, ebeam_max = get_ebeam_range()
        ebeam_range = st.slider(
            "Beam energy range [MeV]",
            min_value=ebeam_min,
            max_value=ebeam_max,
            value=(ebeam_min, ebeam_max),
            step=0.5,
        )

    # ---------- Dynamic arbitrary-column filters ----------------------
    _init_filters()
    st.markdown("**Column filters**")

    # Render existing filter rows
    filter_clauses: list[str] = []
    for fid in st.session_state.filter_ids:
        with st.container():
            col, op, val = _render_filter_row(fid)
            clause = _filter_to_sql(col, op, val)
            if clause:
                filter_clauses.append(clause)

    # "Add filter" / "Clear all" buttons
    af1, af2, _ = st.columns([1, 1, 4])
    with af1:
        if st.button("➕ Add filter"):
            _add_filter()
            st.rerun()
    with af2:
        if st.session_state.filter_ids and st.button("🗑️ Clear all"):
            _clear_all_filters()
            st.rerun()

    # ---------- Column selector ---------------------------------------
    st.markdown("**Columns to display**")
    col_preset, col_custom = st.columns([1, 3])
    with col_preset:
        group_keys = list(COLUMN_GROUPS.keys())
        group_choice = st.multiselect(
            "Column groups",
            options=group_keys,
            default=[group_keys[0]] if group_keys else [],
        )
    preset_cols = []
    for g in group_choice:
        preset_cols.extend(COLUMN_GROUPS[g])

    with col_custom:
        extra_cols = st.multiselect(
            "Additional individual columns",
            options=[c for c in ALL_COLUMNS if c not in preset_cols],
            default=[],
        )
    display_cols = preset_cols + extra_cols
    if not display_cols:
        display_cols = ["run_number", "unix_time"]

    # ---------- Row limit ---------------------------------------------
    max_rows = st.number_input("Max rows to return", 10, 1_000_000, 1000, step=100)

    # ---------- Build & execute query ---------------------------------
    where_clauses = []
    if selected_runs:
        runs_str = ", ".join(str(r) for r in selected_runs)
        where_clauses.append(f"run_number IN ({runs_str})")
    where_clauses.append(f"ebeam >= {ebeam_range[0]}")
    where_clauses.append(f"ebeam <= {ebeam_range[1]}")
    # Append dynamic filters
    where_clauses.extend(filter_clauses)

    where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"
    cols_sql = ", ".join(display_cols)
    query = f"SELECT {cols_sql} FROM events WHERE {where_sql} LIMIT {max_rows}"

    with st.expander("Generated SQL"):
        st.code(query, language="sql")

    if st.button("🔎 Run query", type="primary", key="browse_run"):
        df = run_query(query)
        st.success(f"Returned **{len(df):,}** rows")
        st.dataframe(df, width='stretch', height=500)

        # Download button
        csv_data = df.to_csv(index=False)
        st.download_button(
            "⬇️ Download CSV",
            csv_data,
            file_name="padme_query_result.csv",
            mime="text/csv",
        )


# ========================  TAB 2: Plot  ===============================
with tab_plot:
    st.subheader("Quick plots")

    numeric_cols = [c for c in ALL_COLUMNS if c not in ("run_number",)]

    pc1, pc2 = st.columns(2)
    with pc1:
        x_col = st.selectbox("X axis", options=numeric_cols, index=numeric_cols.index("unix_time"))
    with pc2:
        y_col = st.selectbox("Y axis", options=numeric_cols, index=numeric_cols.index("ebeam") if "ebeam" in numeric_cols else 0)

    color_col = st.selectbox("Color by", options=["(none)", "run_number"] + numeric_cols, index=0)

    plot_runs = st.multiselect("Runs to plot", options=get_run_list(), default=[], key="plot_runs",
                                help="Leave empty for all runs")
    plot_limit = st.number_input("Max points", 100, 500_000, 10_000, step=1000, key="plot_limit")

    if st.button("📊 Draw plot", type="primary", key="plot_run"):
        where_parts = [f"{x_col} IS NOT NULL", f"{y_col} IS NOT NULL"]
        if plot_runs:
            where_parts.append(f"run_number IN ({', '.join(str(r) for r in plot_runs)})")
        where_sql = " AND ".join(where_parts)

        fetch_cols = list({x_col, y_col})
        if color_col != "(none)":
            fetch_cols.append(color_col)
        fetch_cols = list(dict.fromkeys(fetch_cols))  # deduplicate, preserve order

        sql = f"SELECT {', '.join(fetch_cols)} FROM events WHERE {where_sql} LIMIT {plot_limit}"
        df = run_query(sql)
        st.caption(f"{len(df):,} points")

        if color_col != "(none)" and color_col in df.columns:
            # Convert run_number to string so it's treated as categorical
            if color_col == "run_number":
                df["run_number"] = df["run_number"].astype(str)
            st.scatter_chart(df, x=x_col, y=y_col, color=color_col, height=500)
        else:
            st.scatter_chart(df, x=x_col, y=y_col, height=500)

    # ---- Histogram ---------------------------------------------------
    st.markdown("---")
    st.subheader("Histogram")
    hist_col = st.selectbox("Column", options=numeric_cols, key="hist_col")
    hist_bins = st.slider("Bins", 10, 200, 50, key="hist_bins")

    if st.button("📊 Draw histogram", key="hist_run"):
        sql = f"""
            SELECT {hist_col} FROM events
            WHERE {hist_col} IS NOT NULL
        """
        if plot_runs:
            sql += f" AND run_number IN ({', '.join(str(r) for r in plot_runs)})"
        sql += f" LIMIT {plot_limit}"
        df = run_query(sql)
        import altair as alt
        chart = alt.Chart(df).mark_bar().encode(
            alt.X(f"{hist_col}:Q", bin=alt.Bin(maxbins=hist_bins)),
            y="count()",
        ).properties(height=400)
        st.altair_chart(chart, width='stretch')

# ========================  TAB 3: Raw SQL  ============================
with tab_sql:
    st.subheader("Run custom SQL")
    st.info("For advanced users. Type any DuckDB SQL query.")

    user_sql = st.text_area(
        "SQL query",
        value="SELECT run_number, COUNT(*) AS n_events\nFROM events\nGROUP BY run_number\nORDER BY run_number",
        height=150,
    )

    sql_limit = st.number_input("Row limit", 10, 1_000_000, 1000, step=100, key="sql_limit")

    if st.button("▶️ Execute", type="primary", key="sql_run"):
        try:
            # Wrap in a limit for safety
            safe_sql = f"SELECT * FROM ({user_sql}) AS q LIMIT {sql_limit}"
            df = run_query(safe_sql)
            st.success(f"Returned **{len(df):,}** rows")
            st.dataframe(df, width='stretch', height=500)
            csv_data = df.to_csv(index=False)
            st.download_button("⬇️ Download CSV", csv_data,
                               file_name="padme_sql_result.csv", mime="text/csv",
                               key="sql_download")
        except Exception as e:
            st.error(f"Query error: {e}")

# ──────────────────────────────────────────────────────────────────────
# Footer
# ──────────────────────────────────────────────────────────────────────
st.markdown("---")
st.caption("PADME Conditions DB · Powered by DuckDB + Streamlit")
