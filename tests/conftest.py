import copy
import json
import os
from pathlib import Path

import psycopg
import pytest

from dc_importer.db import migrate

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def deck():
    return copy.deepcopy(json.loads((FIXTURES / "deck.json").read_text(encoding="utf-8")))


@pytest.fixture(scope="session")
def database():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is not set; SQL integration tests require a disposable DB")
    conn = psycopg.connect(dsn, autocommit=True)
    # Do not reset or drop anything. The integration target must be empty.
    if conn.execute("select to_regclass('public.mtg_decks')").fetchone()[0]:
        pytest.fail("TEST_DATABASE_URL must point to an empty disposable database")
    # Simulate Supabase's API roles and permissive defaults before applying our restrictions.
    conn.execute("create role anon nologin")
    conn.execute("create role authenticated nologin")
    conn.execute("create role service_role nologin bypassrls")
    conn.execute("""alter default privileges in schema public
        grant all on tables to anon,authenticated,service_role""")
    conn.execute("""alter default privileges in schema public
        grant execute on functions to anon,authenticated,service_role""")
    migrate(conn)
    yield conn
    conn.close()
