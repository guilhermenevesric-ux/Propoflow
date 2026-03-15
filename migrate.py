from db import engine
from sqlalchemy import text


def is_sqlite() -> bool:
    return engine.dialect.name == "sqlite"


def is_postgres() -> bool:
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


def add_column(conn, ddl_sqlite: str, ddl_pg: str):
    if is_postgres():
        conn.execute(text(ddl_pg))
    else:
        conn.execute(text(ddl_sqlite))


def ensure_column(conn, table_name: str, column_name: str, ddl_sqlite: str, ddl_pg: str):
    if not column_exists(conn, table_name, column_name):
        add_column(conn, ddl_sqlite, ddl_pg)


def ensure_events_table(conn):
    if is_postgres():
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                name VARCHAR(80) NOT NULL,
                path VARCHAR(255),
                user_id INTEGER,
                proposal_id INTEGER,
                ip VARCHAR(64),
                ua VARCHAR(255),
                ref VARCHAR(512),
                meta TEXT
            )
        """))
    else:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                name VARCHAR(80) NOT NULL,
                path VARCHAR(255),
                user_id INTEGER,
                proposal_id INTEGER,
                ip VARCHAR(64),
                ua VARCHAR(255),
                ref VARCHAR(512),
                meta TEXT
            )
        """))

    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_events_name_created_at ON events (name, created_at)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_events_user_created_at ON events (user_id, created_at)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_events_proposal_created_at ON events (proposal_id, created_at)"
    ))


with engine.begin() as conn:
    # ===== USERS =====
    ensure_column(
        conn, "users", "paid_until",
        "ALTER TABLE users ADD COLUMN paid_until DATETIME",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS paid_until TIMESTAMP NULL"
    )

    ensure_column(
        conn, "users", "plan_updated_at",
        "ALTER TABLE users ADD COLUMN plan_updated_at DATETIME",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_updated_at TIMESTAMP NULL"
    )

    ensure_column(
        conn, "users", "asaas_customer_id",
        "ALTER TABLE users ADD COLUMN asaas_customer_id VARCHAR(40)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS asaas_customer_id VARCHAR(40)"
    )

    ensure_column(
        conn, "users", "asaas_subscription_id",
        "ALTER TABLE users ADD COLUMN asaas_subscription_id VARCHAR(40)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS asaas_subscription_id VARCHAR(40)"
    )

    ensure_column(
        conn, "users", "email_verify_last_sent_at",
        "ALTER TABLE users ADD COLUMN email_verify_last_sent_at DATETIME",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_last_sent_at TIMESTAMP NULL"
    )

    ensure_column(
        conn, "users", "pix_key",
        "ALTER TABLE users ADD COLUMN pix_key VARCHAR(120)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS pix_key VARCHAR(120)"
    )

    ensure_column(
        conn, "users", "pix_name",
        "ALTER TABLE users ADD COLUMN pix_name VARCHAR(120)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS pix_name VARCHAR(120)"
    )

    ensure_column(
        conn, "users", "default_validity_days",
        "ALTER TABLE users ADD COLUMN default_validity_days INTEGER DEFAULT 7",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_validity_days INTEGER DEFAULT 7"
    )

    ensure_column(
        conn, "users", "default_payment_plan",
        "ALTER TABLE users ADD COLUMN default_payment_plan VARCHAR(40) DEFAULT 'avista'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_payment_plan VARCHAR(40) DEFAULT 'avista'"
    )

    ensure_column(
        conn, "users", "default_message_template",
        "ALTER TABLE users ADD COLUMN default_message_template TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_message_template TEXT"
    )

    ensure_column(
        conn, "users", "default_terms",
        "ALTER TABLE users ADD COLUMN default_terms TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_terms TEXT"
    )

    ensure_column(
        conn, "users", "logo_mime",
        "ALTER TABLE users ADD COLUMN logo_mime VARCHAR(64)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS logo_mime VARCHAR(64)"
    )

    ensure_column(
        conn, "users", "logo_b64",
        "ALTER TABLE users ADD COLUMN logo_b64 TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS logo_b64 TEXT"
    )

    ensure_column(
        conn, "users", "email_verified",
        "ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE"
    )

    ensure_column(
        conn, "users", "email_verify_code_hash",
        "ALTER TABLE users ADD COLUMN email_verify_code_hash VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_code_hash VARCHAR(255)"
    )

    ensure_column(
        conn, "users", "email_verify_expires_at",
        "ALTER TABLE users ADD COLUMN email_verify_expires_at DATETIME",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_expires_at TIMESTAMP NULL"
    )

    # ===== SERVICES =====
    ensure_column(
        conn, "services", "favorite",
        "ALTER TABLE services ADD COLUMN favorite INTEGER DEFAULT 0",
        "ALTER TABLE services ADD COLUMN IF NOT EXISTS favorite BOOLEAN DEFAULT FALSE"
    )

    # ===== CLIENTS =====
    ensure_column(
        conn, "clients", "favorite",
        "ALTER TABLE clients ADD COLUMN favorite INTEGER DEFAULT 0",
        "ALTER TABLE clients ADD COLUMN IF NOT EXISTS favorite BOOLEAN DEFAULT FALSE"
    )

    # ===== PROPOSALS =====
    ensure_column(
        conn, "proposals", "revision",
        "ALTER TABLE proposals ADD COLUMN revision INTEGER DEFAULT 1",
        "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS revision INTEGER DEFAULT 1"
    )

    ensure_column(
        conn, "proposals", "updated_at",
        "ALTER TABLE proposals ADD COLUMN updated_at DATETIME",
        "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NULL"
    )

    ensure_column(
        conn, "proposals", "overhead_percent",
        "ALTER TABLE proposals ADD COLUMN overhead_percent INTEGER DEFAULT 10",
        "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS overhead_percent INTEGER DEFAULT 10"
    )

    ensure_column(
        conn, "proposals", "margin_percent",
        "ALTER TABLE proposals ADD COLUMN margin_percent INTEGER DEFAULT 0",
        "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS margin_percent INTEGER DEFAULT 0"
    )

    ensure_column(
        conn, "proposals", "total_cents",
        "ALTER TABLE proposals ADD COLUMN total_cents INTEGER DEFAULT 0",
        "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS total_cents INTEGER DEFAULT 0"
    )

    ensure_column(
        conn, "proposals", "client_id",
        "ALTER TABLE proposals ADD COLUMN client_id INTEGER",
        "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS client_id INTEGER NULL"
    )

    ensure_column(
        conn, "proposals", "terms_text",
        "ALTER TABLE proposals ADD COLUMN terms_text TEXT",
        "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS terms_text TEXT"
    )

    ensure_column(
        conn, "proposals", "view_count",
        "ALTER TABLE proposals ADD COLUMN view_count INTEGER DEFAULT 0",
        "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS view_count INTEGER DEFAULT 0"
    )

    ensure_column(
        conn, "proposals", "first_viewed_at",
        "ALTER TABLE proposals ADD COLUMN first_viewed_at DATETIME",
        "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS first_viewed_at TIMESTAMP NULL"
    )

    ensure_column(
        conn, "proposals", "last_viewed_at",
        "ALTER TABLE proposals ADD COLUMN last_viewed_at DATETIME",
        "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS last_viewed_at TIMESTAMP NULL"
    )

    # ===== EVENTS =====
    ensure_events_table(conn)

print("✅ migrate.py OK")