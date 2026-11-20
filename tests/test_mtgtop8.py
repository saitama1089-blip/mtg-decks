from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from dc_importer.mtgtop8 import INDEX, ParseError, parse_deck, parse_event, parse_index

SAMPLES = Path(__file__).parent / "fixtures" / "mtgtop8"


def sample(name):
    return (SAMPLES / f"{name}.html").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name,event_id,deck_id,commanders,platform",
    [
        ("single", 94041, 914043, 1, "paper"),
        ("partner", 94012, 913882, 2, "other_online"),
        ("league", 93856, 912701, 1, "mtgo"),
    ],
)
def test_real_source_lists(name, event_id, deck_id, commanders, platform):
    parsed = parse_deck(sample(name), event_id, deck_id)
    assert parsed["event"]["platform"] == platform
    assert parsed["source_deck_id"] == deck_id
    assert sum(c["quantity"] for c in parsed["cards"] if c["zone"] in {"main", "commander"}) == 100
    assert sum(c["zone"] == "commander" for c in parsed["cards"]) == commanders
    assert parsed["source_cards"] and parsed["raw_text"]


def test_relative_deck_links_and_league_labels():
    event = parse_event(sample("single"), 94041)
    assert len(event.decks) == 4
    assert event.event["player_count"] == 20
    assert event.decks[914043]["rank_min"] == 1
    assert event.decks[914043]["player_name"] == "Andrea Fiora"
    league = parse_deck(sample("league"), 93856, 912701)
    assert league["event"]["player_count"] is None
    assert league["rank_min"] is None
    assert league["result_kind"] == "league_result"
    assert any(c["quantity"] > 1 for c in league["cards"])


def test_selected_deck_page_has_its_own_title():
    parsed = parse_deck(sample("selected"), 94041, 914044)
    assert parsed["event"]["title"] == "Marte Duel"
    assert parsed["deck_name"] == "Jace, Multiverse Architect"
    assert parsed["rank_min"] == 2


def test_main_listing_excludes_featured_old_events_and_preserves_pagination():
    result = parse_index(sample("index"))
    assert len(result.events) == 20
    assert all(e["event_date"] >= "2026-11-10" for e in result.events)
    assert result.next_url == INDEX + "&meta=115&cp=2"
    assert result.all_url == INDEX + "&meta=56&a="


def test_fail_closed_on_wrong_format_id_or_incomplete_list():
    with pytest.raises(ParseError, match="not Duel Commander"):
        parse_deck(sample("single").replace("Duel Commander", "Pauper"), 94041)
    with pytest.raises(ParseError, match="differs"):
        parse_deck(sample("single"), 94041, 123)
    with pytest.raises(ParseError, match="missing"):
        parse_deck(sample("single"), 123)
    soup = BeautifulSoup(sample("single"), "html.parser")
    soup.select(".deck_line")[-1].decompose()
    with pytest.raises(ValueError, match="100"):
        parse_deck(str(soup), 94041)


def test_unknown_card_section_is_rejected():
    html = sample("single").replace("35 LANDS", "UNRECOGNIZED SECTION")
    with pytest.raises(ParseError, match="section"):
        parse_deck(html, 94041)
