import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, inspect, text


st.set_page_config(
    page_title="Rover Telemetry Dashboard",
    page_icon="🚙",
    layout="wide",
)

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def require_passkey() -> bool:
    required_passkey = st.secrets.get("APP_PASSKEY")

    if not required_passkey:
        st.error("APP_PASSKEY is missing in Streamlit secrets.")
        return False

    if "auth_ok" not in st.session_state:
        st.session_state.auth_ok = False

    with st.sidebar:
        st.subheader("🔐 Access")
        entered_passkey = st.text_input("Enter passkey", type="password")

        if st.button("Unlock", use_container_width=True):
            if entered_passkey == required_passkey:
                st.session_state.auth_ok = True
                st.success("Access granted")
            else:
                st.error("Wrong passkey")

    return st.session_state.auth_ok


@st.cache_resource(show_spinner=False)
def get_engine():
    db = st.secrets["db"]

    database_url = (
        f"postgresql+psycopg2://{db['user']}:{db['password']}"
        f"@{db['host']}:{db['port']}/{db['dbname']}"
        "?sslmode=require&options=-csearch_path=public"
    )

    return create_engine(database_url, pool_pre_ping=True)


@st.cache_data(ttl=10, show_spinner=False)
def list_tables():
    engine = get_engine()
    inspector = inspect(engine)
    return sorted(inspector.get_table_names(schema="public"))


@st.cache_data(ttl=10, show_spinner=False)
def load_table(table_name: str, limit: int = 3000):
    engine = get_engine()

    safe_table = "".join(ch for ch in table_name if ch.isalnum() or ch == "_")
    query = text(f'SELECT * FROM public."{safe_table}" ORDER BY 1 DESC LIMIT :limit')

    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"limit": limit})

    return df


def detect_time_column(df: pd.DataFrame):
    candidates = ["timestamp", "created_at", "recorded_at", "time", "ts", "datetime"]
    lower_map = {col.lower(): col for col in df.columns}

    for candidate in candidates:
        if candidate in lower_map:
            return lower_map[candidate]

    return None


def detect_column(df: pd.DataFrame, candidates):
    lower_map = {col.lower(): col for col in df.columns}

    for candidate in candidates:
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]

    return None


def parse_time_column(df: pd.DataFrame, time_col):
    if time_col and time_col in df.columns:
        df = df.copy()
        df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
    return df


def format_metric_value(value, suffix=""):
    if pd.isna(value):
        return f"N/A{suffix}"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def show_metric(df: pd.DataFrame, label: str, column_options, suffix: str = ""):
    col = detect_column(df, column_options)
    if col and not df.empty:
        value = df.iloc[0][col]
        st.metric(label, format_metric_value(value, suffix))


def draw_line_chart(df: pd.DataFrame, title: str, y_options, x_col):
    y_col = detect_column(df, y_options)

    if x_col and y_col and x_col in df.columns and y_col in df.columns:
        chart_df = df[[x_col, y_col]].dropna().sort_values(x_col)
        if not chart_df.empty:
            st.subheader(title)
            st.line_chart(chart_df, x=x_col, y=y_col, use_container_width=True)


def draw_lidar_chart(df: pd.DataFrame, x_col):
    front_col = detect_column(df, ["front_mm", "front_distance", "front"])
    left_col = detect_column(df, ["left_mm", "left_distance", "left"])
    right_col = detect_column(df, ["right_mm", "right_distance", "right"])

    if x_col and any([front_col, left_col, right_col]):
        cols = [col for col in [front_col, left_col, right_col] if col]
        chart_df = df[[x_col] + cols].dropna().sort_values(x_col)

        if not chart_df.empty:
            st.subheader("LiDAR Distances")
            st.line_chart(chart_df, x=x_col, y=cols, use_container_width=True)


def draw_decision_chart(df: pd.DataFrame):
    decision_col = detect_column(df, ["decision", "action", "obstacle_decision"])

    if decision_col:
        counts = df[decision_col].astype(str).value_counts().reset_index()
        counts.columns = [decision_col, "count"]

        st.subheader("Obstacle Decisions")
        st.bar_chart(counts.set_index(decision_col), use_container_width=True)


def draw_map(df: pd.DataFrame):
    lat_col = detect_column(df, ["latitude", "lat", "gps_lat"])
    lon_col = detect_column(df, ["longitude", "lon", "lng", "gps_lon"])

    if lat_col and lon_col:
        map_df = df[[lat_col, lon_col]].dropna().copy()
        map_df.columns = ["lat", "lon"]

        if not map_df.empty:
            st.subheader("GPS Path")
            st.map(map_df, use_container_width=True)


def show_logs(df: pd.DataFrame):
    log_col = detect_column(df, ["log_line", "message", "log_message", "stdout", "console_output"])

    if log_col:
        st.subheader("Script Output / Logs")
        logs = df[log_col].dropna().astype(str).head(200).tolist()
        st.code("\n".join(reversed(logs)), language="text")


# ------------------------------------------------------------
# App UI
# ------------------------------------------------------------

st.title("🚙 Rover Telemetry Dashboard")
st.caption("Telemetry and rover status from CSC Pukki DBaaS")

if not require_passkey():
    st.info("Enter the passkey in the sidebar to continue.")
    st.stop()

try:
    tables = list_tables()
except Exception as e:
    st.error(f"Database connection failed: {e}")
    st.stop()

if not tables:
    st.warning("No tables found in the database.")
    st.stop()

preferred_tables = [
    "rover_telemetry",
    "rover_logs",
    "rover_status",
    "lidar_scans",
    "system_health",
    "rovvv",
]

default_table = tables[0]
for table_name in preferred_tables:
    if table_name in tables:
        default_table = table_name
        break

with st.sidebar:
    st.subheader("⚙️ Dashboard Settings")
    selected_table = st.selectbox(
        "Choose table",
        tables,
        index=tables.index(default_table)
    )
    row_limit = st.slider("Rows to load", 100, 10000, 3000, 100)
    refresh_data = st.button("Refresh data", use_container_width=True)

if refresh_data:
    st.cache_data.clear()

try:
    df = load_table(selected_table, row_limit)
except Exception as e:
    st.error(f"Could not load table '{selected_table}': {e}")
    st.stop()

if df.empty:
    st.warning(f"Table 'public.{selected_table}' is empty.")
    st.stop()

time_col = detect_time_column(df)
df = parse_time_column(df, time_col)

st.success(f"Loaded {len(df)} rows from 'public.{selected_table}'")

# ------------------------------------------------------------
# Top metrics
# ------------------------------------------------------------

col1, col2, col3, col4 = st.columns(4)

with col1:
    show_metric(df, "Battery", ["battery_percent", "remaining_percent"], "%")

with col2:
    show_metric(df, "Speed", ["speed_m_s", "speed", "ground_speed"], " m/s")

with col3:
    show_metric(df, "Front Distance", ["front_mm", "front_distance", "front"], " mm")

with col4:
    decision_col = detect_column(df, ["decision", "action", "obstacle_decision"])
    if decision_col:
        st.metric("Current Decision", str(df.iloc[0][decision_col]))

# ------------------------------------------------------------
# Main dashboard
# ------------------------------------------------------------

left_col, right_col = st.columns([2, 1])

with left_col:
    draw_map(df)
    draw_line_chart(df, "Speed Over Time", ["speed_m_s", "speed", "ground_speed"], time_col)
    draw_line_chart(df, "Battery Over Time", ["battery_percent", "remaining_percent", "battery_voltage", "voltage_v"], time_col)
    draw_lidar_chart(df, time_col)

with right_col:
    draw_decision_chart(df)

    gps_col = detect_column(df, ["gps_satellites", "num_satellites", "satellites"])
    if gps_col and time_col:
        gps_df = df[[time_col, gps_col]].dropna().sort_values(time_col)
        if not gps_df.empty:
            st.subheader("GPS Satellites")
            st.line_chart(gps_df, x=time_col, y=gps_col, use_container_width=True)

show_logs(df)

st.subheader("Raw Data")
st.dataframe(df, use_container_width=True, height=400)

with st.expander("Secrets format"):
    st.code(
        """
APP_PASSKEY = "your-passkey"

[db]
host = "your-host"
port = 5432
dbname = "rover"
user = "your-username"
password = "your-password"
        """.strip(),
        language="toml",
    )

with st.expander("Recommended table columns"):
    st.markdown(
        """
- `timestamp`
- `latitude`, `longitude`
- `speed_m_s`
- `battery_percent`
- `front_mm`, `left_mm`, `right_mm`
- `decision`
- `gps_satellites`
- `log_line`
        """
    )













# import pandas as pd
# import streamlit as st
# from sqlalchemy import create_engine, inspect, text


# st.set_page_config(
#     page_title="Rover Telemetry Dashboard",
#     page_icon="🚙",
#     layout="wide", )
# # ------------------------------------------------------------
# # Helpers
# # ------------------------------------------------------------

# def require_passkey() -> bool:
#     required_passkey = st.secrets.get("APP_PASSKEY")

#     if not required_passkey:
#         st.error("APP_PASSKEY is missing in Streamlit secrets.")
#         return False

#     if "auth_ok" not in st.session_state:
#         st.session_state.auth_ok = False

#     with st.sidebar:
#         st.subheader("🔐 Access")
#         entered_passkey = st.text_input("Enter passkey", type="password")

#         if st.button("Unlock", use_container_width=True):
#             if entered_passkey == required_passkey:
#                 st.session_state.auth_ok = True
#                 st.success("Access granted")
#             else:
#                 st.error("Wrong passkey")

#     return st.session_state.auth_ok


# @st.cache_resource(show_spinner=False)
# def get_engine():
#     db = st.secrets["db"]

#     database_url = (
#         f"postgresql+psycopg2://{db['user']}:{db['password']}"
#         f"@{db['host']}:{db['port']}/{db['dbname']}?sslmode=require"
#     )

#     return create_engine(database_url, pool_pre_ping=True)


# @st.cache_data(ttl=10, show_spinner=False)
# def list_tables():
#     engine = get_engine()
#     inspector = inspect(engine)
#     return sorted(inspector.get_table_names())


# @st.cache_data(ttl=10, show_spinner=False)
# def load_table(table_name: str, limit: int = 3000):
#     engine = get_engine()

#     safe_table = "".join(ch for ch in table_name if ch.isalnum() or ch == "_")
#     query = text(f'SELECT * FROM "{safe_table}" ORDER BY 1 DESC LIMIT :limit')

#     with engine.connect() as conn:
#         df = pd.read_sql(query, conn, params={"limit": limit})

#     return df


# def detect_time_column(df: pd.DataFrame):
#     candidates = ["timestamp", "created_at", "recorded_at", "time", "ts", "datetime"]
#     lower_map = {col.lower(): col for col in df.columns}

#     for candidate in candidates:
#         if candidate in lower_map:
#             return lower_map[candidate]

#     return None


# def detect_column(df: pd.DataFrame, candidates):
#     lower_map = {col.lower(): col for col in df.columns}

#     for candidate in candidates:
#         if candidate.lower() in lower_map:
#             return lower_map[candidate.lower()]

#     return None


# def parse_time_column(df: pd.DataFrame, time_col: str | None):
#     if time_col and time_col in df.columns:
#         df = df.copy()
#         df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
#     return df


# def show_metric(df: pd.DataFrame, label: str, column_options, suffix: str = ""):
#     col = detect_column(df, column_options)
#     if col and not df.empty:
#         value = df.iloc[0][col]
#         st.metric(label, f"{value}{suffix}")


# def draw_line_chart(df: pd.DataFrame, title: str, y_options, x_col: str | None):
#     y_col = detect_column(df, y_options)

#     if x_col and y_col and x_col in df.columns and y_col in df.columns:
#         chart_df = df[[x_col, y_col]].dropna().sort_values(x_col)
#         if not chart_df.empty:
#             st.subheader(title)
#             st.line_chart(chart_df, x=x_col, y=y_col, use_container_width=True)


# def draw_lidar_chart(df: pd.DataFrame, x_col: str | None):
#     front_col = detect_column(df, ["front_mm", "front_distance", "front"])
#     left_col = detect_column(df, ["left_mm", "left_distance", "left"])
#     right_col = detect_column(df, ["right_mm", "right_distance", "right"])

#     if x_col and any([front_col, left_col, right_col]):
#         cols = [col for col in [front_col, left_col, right_col] if col]
#         chart_df = df[[x_col] + cols].dropna().sort_values(x_col)

#         if not chart_df.empty:
#             st.subheader("LiDAR Distances")
#             st.line_chart(chart_df, x=x_col, y=cols, use_container_width=True)


# def draw_decision_chart(df: pd.DataFrame):
#     decision_col = detect_column(df, ["decision", "action", "obstacle_decision"])

#     if decision_col:
#         counts = df[decision_col].astype(str).value_counts().reset_index()
#         counts.columns = [decision_col, "count"]

#         st.subheader("Obstacle Decisions")
#         st.bar_chart(counts.set_index(decision_col), use_container_width=True)


# def draw_map(df: pd.DataFrame):
#     lat_col = detect_column(df, ["latitude", "lat", "gps_lat"])
#     lon_col = detect_column(df, ["longitude", "lon", "lng", "gps_lon"])

#     if lat_col and lon_col:
#         map_df = df[[lat_col, lon_col]].dropna().copy()
#         map_df.columns = ["lat", "lon"]

#         if not map_df.empty:
#             st.subheader("GPS Path")
#             st.map(map_df, use_container_width=True)


# def show_logs(df: pd.DataFrame):
#     log_col = detect_column(df, ["log_line", "message", "log_message", "stdout", "console_output"])

#     if log_col:
#         st.subheader("Script Output / Logs")
#         logs = df[log_col].dropna().astype(str).head(200).tolist()
#         st.code("\n".join(reversed(logs)), language="text")


# # ------------------------------------------------------------
# # App UI
# # ------------------------------------------------------------

# st.title("🚙 Rover Telemetry Dashboard")
# st.caption("Telemetry and rover status from CSC Pukki DBaaS")

# if not require_passkey():
#     st.info("Enter the passkey in the sidebar to continue.")
#     st.stop()

# try:
#     tables = list_tables()
# except Exception as e:
#     st.error(f"Database connection failed: {e}")
#     st.stop()

# if not tables:
#     st.warning("No tables found in the database.")
#     st.stop()

# preferred_tables = [
#     "rover_telemetry",
#     "telemetry_logs",
#     "telemetry",
#     "rover_logs",
#     "logs",
# ]

# default_table = tables[0]
# for table_name in preferred_tables:
#     if table_name in tables:
#         default_table = table_name
#         break

# with st.sidebar:
#     st.subheader("⚙️ Dashboard Settings")
#     selected_table = st.selectbox(
#         "Choose table",
#         tables,
#         index=tables.index(default_table)
#     )
#     row_limit = st.slider("Rows to load", 100, 10000, 3000, 100)
#     refresh_data = st.button("Refresh data", use_container_width=True)

# if refresh_data:
#     st.cache_data.clear()

# try:
#     df = load_table(selected_table, row_limit)
# except Exception as e:
#     st.error(f"Could not load table '{selected_table}': {e}")
#     st.stop()

# if df.empty:
#     st.warning(f"Table '{selected_table}' is empty.")
#     st.stop()

# time_col = detect_time_column(df)
# df = parse_time_column(df, time_col)

# st.success(f"Loaded {len(df)} rows from '{selected_table}'")

# # ------------------------------------------------------------
# # Top metrics
# # ------------------------------------------------------------

# col1, col2, col3, col4 = st.columns(4)

# with col1:
#     show_metric(df, "Battery", ["battery_percent", "remaining_percent"], "%")

# with col2:
#     show_metric(df, "Speed", ["speed_m_s", "speed", "ground_speed"], " m/s")

# with col3:
#     show_metric(df, "Front Distance", ["front_mm", "front_distance", "front"], " mm")

# with col4:
#     decision_col = detect_column(df, ["decision", "action", "obstacle_decision"])
#     if decision_col:
#         st.metric("Current Decision", str(df.iloc[0][decision_col]))

# # ------------------------------------------------------------
# # Main dashboard
# # ------------------------------------------------------------

# left_col, right_col = st.columns([2, 1])

# with left_col:
#     draw_map(df)
#     draw_line_chart(df, "Speed Over Time", ["speed_m_s", "speed", "ground_speed"], time_col)
#     draw_line_chart(df, "Battery Over Time", ["battery_percent", "remaining_percent", "battery_voltage", "voltage_v"], time_col)
#     draw_lidar_chart(df, time_col)

# with right_col:
#     draw_decision_chart(df)

#     gps_col = detect_column(df, ["gps_satellites", "num_satellites", "satellites"])
#     if gps_col and time_col:
#         gps_df = df[[time_col, gps_col]].dropna().sort_values(time_col)
#         if not gps_df.empty:
#             st.subheader("GPS Satellites")
#             st.line_chart(gps_df, x=time_col, y=gps_col, use_container_width=True)

# show_logs(df)

# st.subheader("Raw Data")
# st.dataframe(df, use_container_width=True, height=400)

# with st.expander("Secrets format"):
#     st.code(
#         """
# APP_PASSKEY = "your-passkey"

# [db]
# host = "your-host"
# port = 5432
# dbname = "your-db-name"
# user = "your-username"
# password = "your-password"
#         """.strip(),
#         language="toml",
#     )

# with st.expander("Recommended table columns"):
#     st.markdown(
#         """
# - `timestamp`
# - `latitude`, `longitude`
# - `speed_m_s`
# - `battery_percent`
# - `front_mm`, `left_mm`, `right_mm`
# - `decision`
# - `gps_satellites`
# - `log_line`
#         """
#     )





