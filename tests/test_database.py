import json
from copy import deepcopy

import psycopg
import pytest
from conftest import FIXTURES
from psycopg.types.json import Jsonb

from dc_importer.db import migrate
from dc_importer.repository import load_decks

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


def test_complete_database_contract(database, deck):
    db = database
    assert migrate(db) == []
    cards = json.loads((FIXTURES / "cards.json").read_text(encoding="utf-8"))
    db.execute("select ingestion.upsert_cards(%s)", (Jsonb(cards),))
    first = load_decks(db, [deck])
    assert first["inserted"] == 1
    timestamp = db.execute("select last_updated from public.mtg_decks").fetchone()[0]
    assert load_decks(db, [deck])["unchanged"] == 1
    assert db.execute("select last_updated from public.mtg_decks").fetchone()[0] == timestamp
    assert db.execute("select count(*) from public.deck_versions").fetchone()[0] == 1

    second = deepcopy(deck)
    second["source_deck_id"] = 1002
    second["cards"].pop()
    second["cards"][1]["quantity"] = 99
    load_decks(db, [second])
    result = usage(db)
    assert result["eligible_decks"] == 2
    assert result["cards"][0]["decks_with_card"] == 1
    assert float(result["cards"][0]["inclusion_percentage"]) == 50.0
    assert len(result["cards"]) == 1
    assert result["cards"][0]["oracle_id"] == SPELL
    assert usage(db, basics=True)["eligible_decks"] == 2
    sources = db.execute(
        """select public.get_commander_decks(array[%s::uuid],
        p_date_from=>'2026-11-01',p_date_to=>'2026-12-01',p_card_id=>%s::uuid)""",
        (A, SPELL),
    ).fetchone()[0]
    assert sources["total"] == 1
    assert sources["decks"][0]["source_url"].endswith("&d=1001&f=EDH")

    partner = deepcopy(deck)
    partner["source_deck_id"] = 1003
    partner["cards"].append({"oracle_id": B, "zone": "commander", "quantity": 1})
    partner["cards"][1]["quantity"] -= 1
    load_decks(db, [partner])
    assert usage(db)["eligible_decks"] == 2
    assert usage(db, [B, A])["eligible_decks"] == 1
    assert usage(db, [A], "contains")["eligible_decks"] == 3

    corrected = deepcopy(second)
    corrected["source_deck_id"] = 1001
    assert load_decks(db, [corrected])["revised"] == 1
    assert usage(db)["cards"] == []
    assert load_decks(db, [deck])["revised"] == 1  # A -> B -> A is three revisions.
    assert (
        db.execute("""select max(revision_number) from public.deck_versions v
        join public.mtg_decks d on d.id=v.deck_id where d.source_deck_id=1001""").fetchone()[0]
        == 3
    )

    invalid = deepcopy(deck)
    invalid["source_deck_id"] = 9999
    invalid["cards"][2]["oracle_id"] = "00000000-0000-4000-8000-000000000099"
    before = db.execute("select count(*) from public.mtg_decks").fetchone()[0]
    with pytest.raises(psycopg.Error):
        load_decks(db, [second, invalid])
    assert db.execute("select count(*) from public.mtg_decks").fetchone()[0] == before
    wrong_format = deepcopy(deck)
    wrong_format["format_id"] = "commander"
    with pytest.raises(psycopg.Error):
        db.execute("select ingestion.import_deck(%s)", (Jsonb(wrong_format),))
    with pytest.raises(psycopg.Error):
        with db.transaction():
            db.execute("update public.mtg_decks set cards='[]' where source_deck_id=1001")

    db.execute("select ingestion.set_deck_state(1001,'quarantined','fixture correction')")
    assert usage(db)["eligible_decks"] == 1
    load_decks(db, [deck])
    assert usage(db)["eligible_decks"] == 2
    with pytest.raises(psycopg.Error):
        db.execute("update public.deck_cards set quantity=1")
    db.execute("set role dc_reader")
    assert usage(db)["eligible_decks"] == 2
    with pytest.raises(psycopg.Error):
        db.execute("select * from public.mtg_decks")
    with pytest.raises(psycopg.Error):
        db.execute("select ingestion.import_deck(%s)", (Jsonb(deck),))
    db.execute("reset role")
    for api_role in ("anon", "authenticated"):
        # These are fixed role identifiers, not externally supplied SQL.
        db.execute(f"set role {api_role}")
        with pytest.raises(psycopg.Error):
            usage(db)
        with pytest.raises(psycopg.Error):
            db.execute("select * from public.cards")
        db.execute("reset role")
    db.execute("set role dc_importer")
    assert load_decks(db, [deck])["unchanged"] == 1  # Includes deferred trigger permissions.
    db.execute("reset role")
