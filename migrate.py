# migrate.py
from db import engine
from sqlalchemy import text

def is_sqlite():
    return engine.dialect.name == "sqlite"

def is_postgres():
    return engine.dialect.name in ("postgresql", "postgres")

def sqlite_column_exists(conn, table_name: str, column_name: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    cols = [r[1] for r in rows]
    return column_name in cols

def postgres_column_exists(conn, table_name: str, column_name: str) -> bool:
    q = text("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = :t
          AND column_name = :c
        LIMIT 1
    """)
    return conn.execute(q, {"t": table_name, "c": column_name}).fetchone() is not None

def column_exists(conn, table_name: str, column_name: str) -> bool:
    if is_sqlite():
        return sqlite_column_exists(conn, table_name, column_name)
    if is_postgres():
        return postgres_column_exists(conn, table_name, column_name)
    return False

def add_column(conn, table: str, ddl_sqlite: str, ddl_pg: str):
    if is_postgres():
        conn.execute(text(ddl_pg))
    else:
        conn.execute(text(ddl_sqlite))

with engine.begin() as conn:
    # ===== USERS =====
    if not column_exists(conn, "users", "plan"):
        add_column(conn, "users",
                   "ALTER TABLE users ADD COLUMN plan VARCHAR DEFAULT 'free'",
                   "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan VARCHAR(20) DEFAULT 'free'")

    if not column_exists(conn, "users", "proposal_limit"):
        add_column(conn, "users",
                   "ALTER TABLE users ADD COLUMN proposal_limit INTEGER DEFAULT 5",
                   "ALTER TABLE users ADD COLUMN IF NOT EXISTS proposal_limit INTEGER DEFAULT 5")

    if not column_exists(conn, "users", "delete_credits"):
        add_column(conn, "users",
                   "ALTER TABLE users ADD COLUMN delete_credits INTEGER DEFAULT 1",
                   "ALTER TABLE users ADD COLUMN IF NOT EXISTS delete_credits INTEGER DEFAULT 1")

    if not column_exists(conn, "users", "plan_updated_at"):
        add_column(conn, "users",
                   "ALTER TABLE users ADD COLUMN plan_updated_at DATETIME",
                   "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_updated_at TIMESTAMP NULL")

    if not column_exists(conn, "users", "cpf_cnpj"):
        add_column(conn, "users",
                   "ALTER TABLE users ADD COLUMN cpf_cnpj VARCHAR(18)",
                   "ALTER TABLE users ADD COLUMN IF NOT EXISTS cpf_cnpj VARCHAR(18)")

    if not column_exists(conn, "users", "paid_until"):
        add_column(conn, "users",
                   "ALTER TABLE users ADD COLUMN paid_until DATETIME",
                   "ALTER TABLE users ADD COLUMN IF NOT EXISTS paid_until TIMESTAMP NULL")

    if not column_exists(conn, "users", "asaas_customer_id"):
        add_column(conn, "users",
                   "ALTER TABLE users ADD COLUMN asaas_customer_id VARCHAR(40)",
                   "ALTER TABLE users ADD COLUMN IF NOT EXISTS asaas_customer_id VARCHAR(40)")

    if not column_exists(conn, "users", "asaas_subscription_id"):
        add_column(conn, "users",
                   "ALTER TABLE users ADD COLUMN asaas_subscription_id VARCHAR(40)",
                   "ALTER TABLE users ADD COLUMN IF NOT EXISTS asaas_subscription_id VARCHAR(40)")

    # NOVO: pix
    if not column_exists(conn, "users", "pix_key"):
        add_column(conn, "users",
                   "ALTER TABLE users ADD COLUMN pix_key VARCHAR(120)",
                   "ALTER TABLE users ADD COLUMN IF NOT EXISTS pix_key VARCHAR(120)")

    if not column_exists(conn, "users", "pix_name"):
        add_column(conn, "users",
                   "ALTER TABLE users ADD COLUMN pix_name VARCHAR(120)",
                   "ALTER TABLE users ADD COLUMN IF NOT EXISTS pix_name VARCHAR(120)")

    # ===== PROPOSALS (NOVOS CAMPOS) =====
    if not column_exists(conn, "proposals", "client_whatsapp"):
        add_column(conn, "proposals",
                   "ALTER TABLE proposals ADD COLUMN client_whatsapp VARCHAR(30)",
                   "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS client_whatsapp VARCHAR(30)")

    if not column_exists(conn, "proposals", "status"):
        add_column(conn, "proposals",
                   "ALTER TABLE proposals ADD COLUMN status VARCHAR DEFAULT 'created'",
                   "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'created'")

    if not column_exists(conn, "proposals", "valid_until"):
        add_column(conn, "proposals",
                   "ALTER TABLE proposals ADD COLUMN valid_until DATETIME",
                   "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS valid_until TIMESTAMP NULL")

    if not column_exists(conn, "proposals", "view_count"):
        add_column(conn, "proposals",
                   "ALTER TABLE proposals ADD COLUMN view_count INTEGER DEFAULT 0",
                   "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS view_count INTEGER DEFAULT 0")

    if not column_exists(conn, "proposals", "first_viewed_at"):
        add_column(conn, "proposals",
                   "ALTER TABLE proposals ADD COLUMN first_viewed_at DATETIME",
                   "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS first_viewed_at TIMESTAMP NULL")

    if not column_exists(conn, "proposals", "last_viewed_at"):
        add_column(conn, "proposals",
                   "ALTER TABLE proposals ADD COLUMN last_viewed_at DATETIME",
                   "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS last_viewed_at TIMESTAMP NULL")

    if not column_exists(conn, "proposals", "last_activity_at"):
        add_column(conn, "proposals",
                   "ALTER TABLE proposals ADD COLUMN last_activity_at DATETIME",
                   "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMP NULL")

print("✅ migrate.py OK")