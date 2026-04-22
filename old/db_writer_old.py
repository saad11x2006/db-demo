import psycopg2
from datetime import datetime

conn = psycopg2.connect(
    host="YOUR_HOST",
    dbname="rover",
    user="YOUR_USER",
    password="YOUR_PASSWORD",
    port=5432,
    sslmode="require"
    
)

cursor = conn.cursor()


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
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
            decision
        )
    )

    conn.commit()