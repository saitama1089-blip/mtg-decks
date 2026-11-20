"""Apply the two-table SQL through PostgreSQL; use REST for ordinary data writes."""

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict

PROJECT_REF = "djayrdiyastspmoaqkml"
ROOT = Path(__file__).resolve().parents[2]
TABLE_MARKER = "mtg-decks:duel-commander:two-tables-v1"


def settings() -> dict:
    load_dotenv(ROOT / ".env", override=True)
    if os.getenv("SUPABASE_PROJECT_REF", PROJECT_REF) != PROJECT_REF:
        raise ValueError("SUPABASE_PROJECT_REF does not match the dedicated project")
    return {
        "url": os.getenv("SUPABASE_URL", f"https://{PROJECT_REF}.supabase.co"),
        "secret": os.getenv("SUPABASE_SECRET_KEY", ""),
        "database_url": os.getenv("SUPABASE_DB_URL", ""),
    }


def connect() -> psycopg.Connection:
    dsn = settings()["database_url"]
    if not dsn or "YOUR_PASSWORD" in dsn:
        raise ValueError("Set SUPABASE_DB_URL to apply SQL, or run supabase/bootstrap.sql once")
    try:
        parts = conninfo_to_dict(dsn)
    except psycopg.Error as exc:
        raise ValueError("Invalid PostgreSQL connection string") from exc
    host = parts.get("host", "")
    username = parts.get("user", "")
    direct = host == f"db.{PROJECT_REF}.supabase.co"
    pooled = host.endswith(".pooler.supabase.com") and username.endswith(f".{PROJECT_REF}")
    if not (direct or pooled):
        raise ValueError("Connection must target the dedicated Supabase project")
    if parts.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
        raise ValueError("Hosted connections require sslmode=require or stronger")
    try:
        return psycopg.connect(dsn, autocommit=True, connect_timeout=15)
    except psycopg.Error as exc:
        raise ValueError("Database connection failed; check .env") from exc


def check_identity(conn: psycopg.Connection) -> None:
    rows = conn.execute("""select obj_description(to_regclass('public.mtg_decks')),
        obj_description(to_regclass('public.cards'))""").fetchone()
    if rows != (TABLE_MARKER, TABLE_MARKER):
        raise ValueError("The two-table Duel Commander schema has not been installed")


def migrations() -> list[Path]:
    return sorted((ROOT / "supabase" / "migrations").glob("*.sql"))


def migrate(conn: psycopg.Connection) -> list[str]:
    # Re-runnable DDL/functions; no migration-log table is needed for this small schema.
    with conn.transaction():
        conn.execute("select pg_advisory_xact_lock(hashtextextended('dc:schema',0))")
        for path in migrations():
            conn.execute(path.read_text(encoding="utf-8"))
        check_identity(conn)
    return [path.name for path in migrations()]


def build_sql() -> str:
    chunks = ["-- Dedicated Duel Commander project: two tables.\nBEGIN;"]
    for path in migrations():
        chunks.append(f"-- {path.name}\n{path.read_text(encoding='utf-8')}")
    chunks.append("COMMIT;\n")
    return "\n\n".join(chunks)
