# Rover Streamlit Dashboard

This Streamlit app is designed for your CSC Pukki DBaaS PostgreSQL database.

## What it does

- Protects the dashboard with a passkey from Streamlit secrets
- Connects to PostgreSQL using `DATABASE_URL` from Streamlit secrets
- Loads rover telemetry from a selected table
- Shows:
  - latest metrics
  - GPS path
  - speed chart
  - battery chart
  - LiDAR distance chart
  - obstacle decision counts
  - script output / logs
  - raw table data

## Files

- `app.py`
- `requirements.txt`

## Streamlit secrets

In Streamlit Community Cloud, add:

```toml
APP_PASSKEY = "your-passkey"
DATABASE_URL = "postgresql+psycopg2://USER:PASSWORD@HOST:PORT/DBNAME"
```

## Local run

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Important

To display the printed output from your rover Python script, you need to store those log lines in the database in a column such as:

- `log_line`
- `message`
- `log_message`
- `stdout`
- `console_output`

If your current rover script only prints to terminal and does not save logs to the database, the dashboard cannot show those prints yet.

## Recommended telemetry table columns

```text
timestamp
latitude
longitude
speed_m_s
battery_percent
front_mm
left_mm
right_mm
decision
gps_satellites
log_line
```
