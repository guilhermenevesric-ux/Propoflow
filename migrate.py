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

def add_column(conn, ddl_sqlite: str, ddl_pg: str):
    if is_postgres():
        conn.execute(text(ddl_pg))
    else:
        conn.execute(text(ddl_sqlite))

with engine.begin() as conn:
    # USERS: PIX (se não existir)
    if not column_exists(conn, "users", "pix_key"):
        add_column(conn,
                   "ALTER TABLE users ADD COLUMN pix_key VARCHAR(120)",
                   "ALTER TABLE users ADD COLUMN IF NOT EXISTS pix_key VARCHAR(120)")
    if not column_exists(conn, "users", "pix_name"):
        add_column(conn,
                   "ALTER TABLE users ADD COLUMN pix_name VARCHAR(120)",
                   "ALTER TABLE users ADD COLUMN IF NOT EXISTS pix_name VARCHAR(120)")

        # SERVICES: favorite
        if not column_exists(conn, "services", "favorite"):
            if is_postgres():
                conn.execute(text("ALTER TABLE services ADD COLUMN IF NOT EXISTS favorite BOOLEAN DEFAULT FALSE"))
            else:
                conn.execute(text("ALTER TABLE services ADD COLUMN favorite BOOLEAN DEFAULT 0"))

        # CLIENTS: favorite
        if not column_exists(conn, "clients", "favorite"):
            if is_postgres():
                conn.execute(text("ALTER TABLE clients ADD COLUMN IF NOT EXISTS favorite BOOLEAN DEFAULT FALSE"))
            else:
                conn.execute(text("ALTER TABLE clients ADD COLUMN favorite BOOLEAN DEFAULT 0"))

    # PROPOSALS: orçamento (campos novos)
    if not column_exists(conn, "proposals", "revision"):
        add_column(conn,
                   "ALTER TABLE proposals ADD COLUMN revision INTEGER DEFAULT 1",
                   "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS revision INTEGER DEFAULT 1")
    if not column_exists(conn, "proposals", "updated_at"):
        add_column(conn,
                   "ALTER TABLE proposals ADD COLUMN updated_at DATETIME",
                   "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NULL")
    if not column_exists(conn, "proposals", "overhead_percent"):
        add_column(conn,
                   "ALTER TABLE proposals ADD COLUMN overhead_percent INTEGER DEFAULT 10",
                   "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS overhead_percent INTEGER DEFAULT 10")
    if not column_exists(conn, "proposals", "margin_percent"):
        add_column(conn,
                   "ALTER TABLE proposals ADD COLUMN margin_percent INTEGER DEFAULT 0",
                   "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS margin_percent INTEGER DEFAULT 0")
    if not column_exists(conn, "proposals", "total_cents"):
        add_column(conn,
                   "ALTER TABLE proposals ADD COLUMN total_cents INTEGER DEFAULT 0",
                   "ALTER TABLE proposals ADD COLUMN IF NOT EXISTS total_cents INTEGER DEFAULT 0")

        # USERS: defaults (etapa 7)
        if not column_exists(conn, "users", "default_validity_days"):
            if is_postgres():
                conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS default_validity_days INTEGER DEFAULT 7"))
            else:
                conn.execute(text("ALTER TABLE users ADD COLUMN default_validity_days INTEGER DEFAULT 7"))

        if not column_exists(conn, "users", "default_payment_plan"):
            if is_postgres():
                conn.execute(text(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_payment_plan VARCHAR(40) DEFAULT 'avista'"))
            else:
                conn.execute(text("ALTER TABLE users ADD COLUMN default_payment_plan VARCHAR(40) DEFAULT 'avista'"))

        if not column_exists(conn, "users", "default_message_template"):
            if is_postgres():
                conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS default_message_template TEXT"))
            else:
                conn.execute(text("ALTER TABLE users ADD COLUMN default_message_template TEXT"))

        if not column_exists(conn, "users", "default_terms"):
            if is_postgres():
                conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS default_terms TEXT"))
            else:
                conn.execute(text("ALTER TABLE users ADD COLUMN default_terms TEXT"))

# PROPOSALS: client_id
    if not column_exists(conn, "proposals", "client_id"):
        if is_postgres():
            conn.execute(text("ALTER TABLE proposals ADD COLUMN IF NOT EXISTS client_id INTEGER NULL"))
        else:
            conn.execute(text("ALTER TABLE proposals ADD COLUMN client_id INTEGER"))

# SERVICES: favorite
    if not column_exists(conn, "services", "favorite"):
        if is_postgres():
            conn.execute(text("ALTER TABLE services ADD COLUMN IF NOT EXISTS favorite BOOLEAN DEFAULT FALSE"))
        else:
            conn.execute(text("ALTER TABLE services ADD COLUMN favorite INTEGER DEFAULT 0"))

    # CLIENTS: favorite
    if not column_exists(conn, "clients", "favorite"):
        if is_postgres():
            conn.execute(text("ALTER TABLE clients ADD COLUMN IF NOT EXISTS favorite BOOLEAN DEFAULT FALSE"))
        else:
            conn.execute(text("ALTER TABLE clients ADD COLUMN favorite INTEGER DEFAULT 0"))

# PROPOSALS: terms_text (condições congeladas)
    if not column_exists(conn, "proposals", "terms_text"):
        if is_postgres():
            conn.execute(text("ALTER TABLE proposals ADD COLUMN IF NOT EXISTS terms_text TEXT"))
        else:
            conn.execute(text("ALTER TABLE proposals ADD COLUMN terms_text TEXT"))

print("✅ migrate.py OK")