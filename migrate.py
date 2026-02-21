import os
import sqlite3

DB_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")
if DB_URL.startswith("sqlite:///"):
    path = DB_URL.replace("sqlite:///", "")
elif DB_URL.startswith("sqlite:////"):
    path = DB_URL.replace("sqlite:////", "/")
else:
    raise RuntimeError("This migration script only supports sqlite")

conn = sqlite3.connect(path)
cur = conn.cursor()

def add_column(table, coldef):
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
        print("OK:", table, coldef)
    except Exception as e:
        print("SKIP/ERR:", table, coldef, "->", e)

add_column("users", 'plan VARCHAR DEFAULT "free"')
add_column("users", "proposal_limit INTEGER DEFAULT 10")

conn.commit()
conn.close()
print("Migration done.")