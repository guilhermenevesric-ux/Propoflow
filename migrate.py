from db import engine
from sqlalchemy import text

def column_exists(conn, table_name: str, column_name: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    cols = [r[1] for r in rows]  # pragma columns: cid, name, type, notnull, dflt_value, pk
    return column_name in cols

with engine.begin() as conn:
    # USERS
    if not column_exists(conn, "users", "plan"):
        conn.execute(text("ALTER TABLE users ADD COLUMN plan VARCHAR DEFAULT 'free'"))
    if not column_exists(conn, "users", "proposal_limit"):
        conn.execute(text("ALTER TABLE users ADD COLUMN proposal_limit INTEGER DEFAULT 5"))
    if not column_exists(conn, "users", "delete_credits"):
        conn.execute(text("ALTER TABLE users ADD COLUMN delete_credits INTEGER DEFAULT 1"))

    if not column_exists(conn, "users", "plan_updated_at"):
        conn.execute(text("ALTER TABLE users ADD COLUMN plan_updated_at DATETIME"))
    if not column_exists(conn, "users", "mp_last_payment_id"):
        conn.execute(text("ALTER TABLE users ADD COLUMN mp_last_payment_id VARCHAR(64)"))

print("✅ migrate.py OK")