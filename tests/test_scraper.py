import json
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse
from uuid import NAMESPACE_URL, uuid5

import pytest
from bs4 import BeautifulSoup

from dc_importer.__main__ import parser
from dc_importer.catalog import CardResolver
from dc_importer.http import BudgetReached, Page
from dc_importer.models import validate_deck
from dc_importer.mtgtop8 import INDEX, PARSER_VERSION
from dc_importer.scraper import discover, due, load_state, scrape

SAMPLE = (Path(__file__).parent / "fixtures/mtgtop8/single.html").read_text(encoding="utf-8")


class FakeHTTP:
    def __init__(self, state, max_requests=150, max_seconds=900):
        self.requests = 0
        self.max_requests = max_requests
        self.by_host = {}

    def check_budget(self):
        if self.requests >= self.max_requests:
            raise BudgetReached("Test request budget")

    def count(self):
        self.check_budget()
        self.requests += 1

    def load_robots(self):
        self.count()

    def page(self, url):
        self.count()
        deck = parse_qs(urlparse(url).query).get("d", ["914043"])[0]
        soup = BeautifulSoup(SAMPLE, "html.parser")
        for row in soup.select(".chosen_tr,.hover_tr"):
            if not row.select_one("a.player"):
                continue
            link = row.select_one("a[href*='&d=']")
            row["class"] = ["chosen_tr" if f"d={deck}&" in link["href"] else "hover_tr"]
        return Page(str(soup), url)

    def collection(self, names):
        self.count()
        assert len(names) <= 75
        return {
            "data": [{"name": name, "oracle_id": str(uuid5(NAMESPACE_URL, name))} for name in names]
        }


class FakeDatabase:
    def __init__(self):
        self.decks = {}
        self.calls = []

    def rpc(self, function, arguments):
        self.calls.append((function, arguments))
        if function == "upsert_cards":
            return len(arguments["p_cards"])
        if function == "upsert_mtg_decks":
            payload = arguments["p_decks"][0]
            identity = payload["source_deck_id"]
            status = (
                "unchanged"
                if self.decks.get(identity) == payload
                else ("updated" if identity in self.decks else "inserted")
            )
            self.decks[identity] = payload
            return {"inserted": 0, "updated": 0, "unchanged": 0, status: 1}
        return {"status": "unresolved", "candidates": []}


@pytest.fixture
def runner(tmp_path, monkeypatch):
    database = FakeDatabase()

    @contextmanager
    def client():
        yield database

    monkeypatch.setattr("dc_importer.scraper.data_client", client)
    monkeypatch.setattr("dc_importer.scraper.PoliteHTTP", FakeHTTP)

    def run(*extra):
        args = parser().parse_args(
            [
                "scrape",
                "--event-id",
                "94041",
                "--max-decks",
                "1",
                "--state",
                str(tmp_path / "state.json"),
                "--report",
                str(tmp_path / "report.json"),
                *extra,
            ]
        )
        return scrape(args)

    return run, database, tmp_path


def test_partial_run_resumes_and_forced_repeat_is_unchanged(runner):
    run, database, tmp_path = runner
    first = run()
    assert first["inserted"] == 1 and first["remaining_due"] == 3
    assert first["status"] == "partial"
    resumed = run("--max-decks", "10")
    assert resumed["inserted"] == 3
    assert resumed["events_checked"] == 0
    assert resumed["status"] == "success"
    assert run()["deck_attempts"] == 0
    forced = run("--force", "--max-decks", "10")
    assert forced["unchanged"] == 4 and forced["status"] == "success"
    assert len(database.decks) == 4
    assert len(load_state(tmp_path / "state.json")["decks"]) == 4


def test_request_budget_saves_queue_and_dry_run_never_writes(runner):
    run, database, tmp_path = runner
    limited = run("--max-requests", "2")
    assert limited["status"] == "partial" and limited["parsed"] == 0
    assert limited["remaining_due"] == 4
    assert not database.decks
    preview = run("--dry-run", "--max-decks", "10")
    assert preview["parsed"] == 4 and preview["inserted"] == 0
    assert not database.decks
    assert run()["inserted"] == 1  # Preview did not mark any deck as imported.
    assert json.loads((tmp_path / "report.json").read_text())["inserted"] == 1


def test_bad_deck_records_failure_and_backs_off(runner, monkeypatch):
    run, database, tmp_path = runner
    monkeypatch.setattr(
        "dc_importer.scraper.parse_deck", Mock(side_effect=ValueError("Incomplete"))
    )
    result = run()
    assert result["status"] == "error"
    assert result["errors"][0]["error"] == "Incomplete"
    assert not database.decks
    failed = next(
        r for r in load_state(tmp_path / "state.json")["decks"].values() if r.get("failures")
    )
    assert not due(failed, 86400)
    assert due(failed, 86400, force=True)
    assert due({"last_success": 10**20, "parser_version": "old"}, 86400)
    assert PARSER_VERSION


def test_catalog_resolves_front_faces_uses_cache_and_rejects_missing_names(deck):
    deck["cards"] = [
        {"zone": "commander", "quantity": 1, "name": "Front"},
        {"zone": "main", "quantity": 99, "name": "Island"},
    ]
    deck = validate_deck(deck)
    identities = [str(uuid5(NAMESPACE_URL, name)) for name in ("Front", "Island")]
    http = Mock()
    http.collection.return_value = {
        "data": [
            {
                "name": "Front // Back",
                "oracle_id": identities[0],
                "card_faces": [{"name": "Front"}, {"name": "Back"}],
            },
            {"name": "Island", "oracle_id": identities[1]},
        ]
    }
    database = FakeDatabase()
    cache = {}
    resolver = CardResolver(http, database, cache)
    resolved, uploaded = resolver.resolve(deck)
    assert {c["oracle_id"] for c in resolved["cards"]} == set(identities)
    assert uploaded == 2
    assert resolver.resolve(deck)[1] == 0
    assert http.collection.call_count == 1
    unknown = {
        **deck,
        "cards": [
            {"zone": "commander", "quantity": 1, "name": "Missing"},
            {"zone": "main", "quantity": 99, "name": "Island"},
        ],
    }
    http.collection.return_value = {"data": []}
    with pytest.raises(ValueError, match="Unresolved"):
        resolver.resolve(unknown)


def listing(events, next_url=None, archive_url=None):
    rows = "".join(
        f'<tr class="hover_tr"><td><a href="event?e={identity}&f=EDH">Event</a></td>'
        f"<td>{when}</td></tr>"
        for identity, when in events
    )
    links = "".join(
        f'<a href="{url}">{label}</a>'
        for url, label in [(next_url, "Next"), (archive_url, "All Commander decks")]
        if url
    )
    return (
        "<title>Duel Commander events and metagame</title>"
        f'<table class="Stable">{rows}</table>{links}'
    )


def test_discovery_resumes_archive_cursor_and_stops_at_window(tmp_path):
    state = load_state(tmp_path / "state.json")
    archive = INDEX + "&meta=56"
    page2 = archive + "&cp=2"
    pages = {
        INDEX: listing([(4, "17/11/26"), (3, "16/11/26")], archive_url=archive),
        archive: listing([(4, "17/11/26"), (3, "16/11/26")], next_url=page2),
        page2: listing([(2, "10/11/26"), (1, "31/10/26")], next_url=archive + "&cp=3"),
    }
    http = Mock()
    http.page.side_effect = lambda url: Page(pages[url], url)
    first = {"listing_pages": 0}
    discover(http, state, date(2026, 11, 1), 2, True, first)
    assert first["discovery_truncated"]
    assert state["discovery"]["archive"]["next_url"] == page2
    second = {"listing_pages": 0}
    discover(http, state, date(2026, 11, 1), 2, True, second)
    assert not second["discovery_truncated"]
    assert set(state["events"]) == {"2", "3", "4"}
    assert http.page.call_args_list[-1].args[0] == page2
    third = {"listing_pages": 0}
    discover(http, state, date(2026, 11, 1), 2, True, third)
    assert third["listing_pages"] == 1  # Completed cursor still polls the current root.


def test_runtime_failure_report_is_not_a_success(runner, monkeypatch):
    run, _, tmp_path = runner
    monkeypatch.setattr(
        "dc_importer.scraper.parse_event", Mock(side_effect=RuntimeError("Unexpected"))
    )
    with pytest.raises(RuntimeError):
        run()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == "error"
    assert report["stop_reason"] == "Unexpected RuntimeError"
