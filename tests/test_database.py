import json
from copy import deepcopy

import psycopg
import pytest
from conftest import FIXTURES
from psycopg.types.json import Jsonb

from dc_importer.db import migrate
from dc_importer.repository import load_decks, resolve_name

pytestmark = pytest.mark.integration
A = "00000000-0000-4000-8000-000000000001"
B = "00000000-0000-4000-8000-000000000002"
SPELL = "00000000-0000-4000-8000-000000000004"


def usage(db, ids=None, mode="exact", basics=False):
    return db.execute(
        """select public.get_commander_card_usage(
        %s::uuid[],%s,'2026-11-01','2026-12-01',p_include_basics=>%s)""",
        (ids or [A], mode, basics),
    ).fetchone()[0]


def test_two_table_database_contract(database, deck):
    db = database
    assert len(migrate(db)) == 2
    assert db.execute("""select tablename from pg_tables
        where schemaname in ('public','ingestion') order by tablename""").fetchall() == [
        ("cards",),
        ("mtg_decks",),
    ]
    cards = json.loads((FIXTURES / "cards.json").read_text(encoding="utf-8"))
    db.execute("select public.upsert_cards(%s)", (Jsonb(cards),))
    assert resolve_name(db, "Test Commander A")["status"] == "resolved"
    assert load_decks(db, [deck])["inserted"] == 1
    saved = db.execute("select id,last_updated,list_hash from public.mtg_decks").fetchone()
    assert load_decks(db, [deck])["unchanged"] == 1
    assert db.execute("select id,last_updated,list_hash from public.mtg_decks").fetchone() == saved
    assert db.execute("select count(*) from public.mtg_decks").fetchone()[0] == 1
    assert db.execute("select cards->0 ? 'section' from public.mtg_decks").fetchone()[0]

    second = deepcopy(deck)
    second["source_deck_id"] = 1002
    second["cards"].pop()
    second["cards"][1]["quantity"] = 99
    load_decks(db, [second])
    result = usage(db)
    assert result["eligible_decks"] == 2
    assert result["cards"][0]["oracle_id"] == SPELL
    assert float(result["cards"][0]["inclusion_percentage"]) == 50.0
    assert len(result["cards"]) == 1
    assert usage(db, basics=True)["eligible_decks"] == 2
    assert usage(db, basics=True)["cards"][0]["total_copies"] == 197
    supporting = db.execute(
        """select public.get_commander_decks(array[%s::uuid],
        p_date_from=>'2026-11-01',p_date_to=>'2026-12-01',p_card_id=>%s::uuid)""",
        (A, SPELL),
    ).fetchone()[0]
    assert supporting["total"] == 1
    assert supporting["decks"][0]["source_url"].endswith("&d=1001&f=EDH")

    partner = deepcopy(deck)
    partner["source_deck_id"] = 1003
    partner["cards"].append({"oracle_id": B, "zone": "commander", "quantity": 1})
    partner["cards"][1]["quantity"] -= 1
    load_decks(db, [partner])
    assert usage(db)["eligible_decks"] == 2
    assert usage(db, [B, A])["eligible_decks"] == 1
    assert usage(db, [A], "contains")["eligible_decks"] == 3
    partner["cards"].reverse()
    assert load_decks(db, [partner])["unchanged"] == 1

    corrected = deepcopy(second)
    corrected["source_deck_id"] = 1001
    assert load_decks(db, [corrected])["updated"] == 1
    assert usage(db)["cards"] == []
    assert load_decks(db, [deck])["updated"] == 1
    assert (
        db.execute("select id from public.mtg_decks where source_deck_id=1001").fetchone()[0]
        == saved[0]
    )
    assert db.execute("select count(*) from public.mtg_decks").fetchone()[0] == 3

    invalid = deepcopy(deck)
    invalid["source_deck_id"] = 9999
    invalid["cards"][2]["oracle_id"] = "00000000-0000-4000-8000-000000000099"
    good = deepcopy(deck)
    good["source_deck_id"] = 1004
    with pytest.raises(psycopg.Error):
        load_decks(db, [good, invalid])
    assert db.execute("select count(*) from public.mtg_decks").fetchone()[0] == 3
    with pytest.raises(psycopg.Error):
        db.execute("update public.mtg_decks set cards='[]' where source_deck_id=1001")
    with pytest.raises(psycopg.Error):
        db.execute("update public.mtg_decks set format_id='commander' where source_deck_id=1001")

    db.execute("select public.set_deck_state(1001,'quarantined','fixture correction')")
    assert usage(db)["eligible_decks"] == 1
    load_decks(db, [deck])
    assert usage(db)["eligible_decks"] == 2
    db.execute("select public.add_reviewed_alias('Fixture alternate',%s::uuid,'reviewed')", (A,))
    assert resolve_name(db, "Fixture alternate")["candidates"][0]["oracle_id"] == A
    db.execute("select public.upsert_cards(%s)", (Jsonb(cards),))
    assert resolve_name(db, "Fixture alternate")["status"] == "resolved"

    named = deepcopy(deck)
    named["source_deck_id"] = 1005
    named["cards"] = [
        {"section": "Commander", "qty": 1, "name": "Test Commander A"},
        {"section": "Deck", "qty": 99, "name": "Test Island"},
    ]
    assert load_decks(db, [named])["inserted"] == 1
    assert (
        db.execute("select total_cards from public.mtg_decks where source_deck_id=1005").fetchone()[
            0
        ]
        == 100
    )
    for role in ("anon", "authenticated"):
        db.execute(f"set role {role}")
        with pytest.raises(psycopg.Error):
            usage(db)
        with pytest.raises(psycopg.Error):
            db.execute("select * from public.cards")
        db.execute("reset role")
    db.execute("set role service_role")
    assert load_decks(db, [deck])["unchanged"] == 1
    assert usage(db)["eligible_decks"] == 3
    db.execute("reset role")
    migrate(db)
    assert db.execute("select count(*) from public.mtg_decks").fetchone()[0] == 4
