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

with engine.begin() as conn:
    # USERS
    if not column_exists(conn, "users", "plan"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan VARCHAR(20) DEFAULT 'free'"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN plan VARCHAR DEFAULT 'free'"))

    if not column_exists(conn, "users", "proposal_limit"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS proposal_limit INTEGER DEFAULT 5"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN proposal_limit INTEGER DEFAULT 5"))

    if not column_exists(conn, "users", "delete_credits"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS delete_credits INTEGER DEFAULT 1"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN delete_credits INTEGER DEFAULT 1"))

    if not column_exists(conn, "users", "plan_updated_at"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_updated_at TIMESTAMP NULL"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN plan_updated_at DATETIME"))

    # NOVO: cpf/cnpj
    if not column_exists(conn, "users", "cpf_cnpj"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS cpf_cnpj VARCHAR(18)"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN cpf_cnpj VARCHAR(18)"))

    # NOVO: paid_until
    if not column_exists(conn, "users", "paid_until"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS paid_until TIMESTAMP NULL"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN paid_until DATETIME"))

    # NOVO: asaas ids
    if not column_exists(conn, "users", "asaas_customer_id"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS asaas_customer_id VARCHAR(40)"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN asaas_customer_id VARCHAR(40)"))

    if not column_exists(conn, "users", "asaas_subscription_id"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS asaas_subscription_id VARCHAR(40)"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN asaas_subscription_id VARCHAR(40)"))

print("✅ migrate.py OK")