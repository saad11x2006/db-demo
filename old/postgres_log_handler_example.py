"""
Example helper for saving console-style logs into PostgreSQL so the dashboard
can show your rover script output too.
"""

import logging
from sqlalchemy import create_engine, text

DATABASE_URL = "postgresql+psycopg2://USER:PASSWORD@HOST:PORT/DBNAME"
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

with engine.begin() as conn:
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS rover_logs (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ DEFAULT NOW(),
            log_line TEXT
        )
    """))

class PostgresLogHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO rover_logs (log_line) VALUES (:msg)"),
                    {"msg": msg},
                )
        except Exception:
            pass
