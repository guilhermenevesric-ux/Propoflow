from sqlalchemy import inspect, text
from db import engine

def ensure_column(conn, table: str, column: str, ddl: str):
    insp = inspect(conn)
    cols = [c["name"] for c in insp.get_columns(table)]
    if column not in cols:
        conn.execute(text(ddl))

with engine.begin() as conn:
    # USERS
    ensure_column(conn, "users", "plan", "ALTER TABLE users ADD COLUMN plan VARCHAR(32) DEFAULT 'free'")
    ensure_column(conn, "users", "proposal_limit", "ALTER TABLE users ADD COLUMN proposal_limit INTEGER DEFAULT 5")
    ensure_column(conn, "users", "delete_credits", "ALTER TABLE users ADD COLUMN delete_credits INTEGER DEFAULT 1")

    ensure_column(conn, "users", "plan_updated_at", "ALTER TABLE users ADD COLUMN plan_updated_at TIMESTAMP")
    ensure_column(conn, "users", "mp_last_preapproval_id", "ALTER TABLE users ADD COLUMN mp_last_preapproval_id VARCHAR(64)")

print("✅ migrate.py OK (SQLite/Postgres)")