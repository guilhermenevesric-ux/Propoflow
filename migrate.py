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
    if not column_exists(conn, "users", "email_verify_last_sent_at"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_last_sent_at TIMESTAMP NULL"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN email_verify_last_sent_at DATETIME"))

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

# USERS: logo (white-label)
    if not column_exists(conn, "users", "logo_mime"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS logo_mime VARCHAR(50)"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN logo_mime VARCHAR(50)"))

    if not column_exists(conn, "users", "logo_b64"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS logo_b64 TEXT"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN logo_b64 TEXT"))

    # PROPOSALS: views tracking (etapa 13)
    if not column_exists(conn, "proposals", "view_count"):
        if is_postgres():
            conn.execute(text("ALTER TABLE proposals ADD COLUMN IF NOT EXISTS view_count INTEGER DEFAULT 0"))
        else:
            conn.execute(text("ALTER TABLE proposals ADD COLUMN view_count INTEGER DEFAULT 0"))

    if not column_exists(conn, "proposals", "first_viewed_at"):
        if is_postgres():
            conn.execute(text("ALTER TABLE proposals ADD COLUMN IF NOT EXISTS first_viewed_at TIMESTAMP NULL"))
        else:
            conn.execute(text("ALTER TABLE proposals ADD COLUMN first_viewed_at DATETIME"))

    if not column_exists(conn, "proposals", "last_viewed_at"):
        if is_postgres():
            conn.execute(text("ALTER TABLE proposals ADD COLUMN IF NOT EXISTS last_viewed_at TIMESTAMP NULL"))
        else:
            conn.execute(text("ALTER TABLE proposals ADD COLUMN last_viewed_at DATETIME"))


    # USERS: email verification
    if not column_exists(conn, "users", "email_verified"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0"))

    if not column_exists(conn, "users", "email_verify_code_hash"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_code_hash VARCHAR(255)"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN email_verify_code_hash VARCHAR(255)"))

    if not column_exists(conn, "users", "email_verify_expires_at"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_expires_at TIMESTAMP NULL"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN email_verify_expires_at DATETIME"))

    # users.default_message_template
    if not column_exists(conn, "users", "default_message_template"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS default_message_template TEXT"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN default_message_template TEXT"))

    def ensure_events_table(conn):
            conn.execute(text("""
          CREATE TABLE IF NOT EXISTS events (
          id SERIAL PRIMARY KEY,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          name VARCHAR(80) NOT NULL,
          path VARCHAR(255),
          user_id INTEGER,
          proposal_id INTEGER,
          ip VARCHAR(64),
          ua VARCHAR(255),
          ref VARCHAR(512),
          meta TEXT
          );
          """))

    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_events_name_created_at ON events (name, created_at);"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_events_user_created_at ON events (user_id, created_at);"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_events_proposal_created_at ON events (proposal_id, created_at);"))

# ... dentro do run_migrations():
# with engine.connect() as conn:
#    ...
#    ensure_events_table(conn)

# ... dentro do seu bloco engine.begin() as conn:
    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS default_terms TEXT"))

# NOVO: defaults do usuário (settings)
    if not column_exists(conn, "users", "default_validity_days"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS default_validity_days INTEGER DEFAULT 7"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN default_validity_days INTEGER DEFAULT 7"))

    if not column_exists(conn, "users", "default_payment_plan"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS default_payment_plan VARCHAR(40) DEFAULT 'avista'"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN default_payment_plan VARCHAR(40) DEFAULT 'avista'"))

    if not column_exists(conn, "users", "default_message_template"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS default_message_template TEXT"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN default_message_template TEXT"))

    if not column_exists(conn, "users", "default_terms"):
        if is_postgres():
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS default_terms TEXT DEFAULT ''"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN default_terms TEXT"))


print("✅ migrate.py OK")