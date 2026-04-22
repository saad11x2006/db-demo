import psycopg2
from datetime import datetime


DB_CONFIG = {
    "host": "86.50.228.28",
    "dbname": "rover",
    "user": "rover",
    "password": "rover11-11",
    "port": 5432,
    "sslmode": "require",
}


def get_connection():
    return psycopg2.connect(**DB_CONFIG)


def insert_telemetry(
    lat,
    lon,
    speed,
    battery_percent,
    voltage,
    front,
    left,
    right,
    gps_sat,
    decision
):
    conn = None
    cursor = None

    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO public.rover_telemetry (
                timestamp,
                latitude,
                longitude,
                speed_m_s,
                battery_percent,
                battery_voltage,
                front_mm,
                left_mm,
                right_mm,
                gps_satellites,
                decision
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                datetime.utcnow(),
                lat,
                lon,
                speed,
                battery_percent,
                voltage,
                front,
                left,
                right,
                gps_sat,
                decision,
            ),
        )

        conn.commit()

    except Exception as e:
        print("insert_telemetry failed:", e)

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def insert_log(log_line):
    conn = None
    cursor = None

    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO public.rover_logs (
                timestamp,
                log_line
            )
            VALUES (%s, %s)
            """,
            (
                datetime.utcnow(),
                log_line,
            ),
        )

        conn.commit()

    except Exception as e:
        print("insert_log failed:", e)

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()