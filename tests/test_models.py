import gzip
import json
from copy import deepcopy

import pytest

from dc_importer.models import catalog_card, name_key, validate_deck
from dc_importer.repository import records


def test_normalization_and_face_names():
    assert name_key("  Glarb’s   CARD ") == "glarb's card"
    card = catalog_card(
        {
            "oracle_id": "00000000-0000-4000-8000-000000000001",
            "name": "Front // Back",
            "type_line": "Creature // Land",
            "card_faces": [
                {"name": "Front", "type_line": "Creature"},
                {"name": "Back", "type_line": "Land"},
            ],
        }
    )
    assert card["face_names"] == ["Front", "Back"]
    assert not card["is_land"]  # An MDFC with a creature front is not counted as a land.


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5, "1"])
def test_invalid_quantities(deck, quantity):
    deck["cards"][0]["quantity"] = quantity
    with pytest.raises(ValueError):
        validate_deck(deck)


def test_scope_and_count(deck):
    deck["format_id"] = "commander"
    with pytest.raises(ValueError, match="Only mtgtop8"):
        validate_deck(deck)
    deck["format_id"] = "duel_commander"
    deck["cards"][1]["quantity"] = 97
    with pytest.raises(ValueError, match="100"):
        validate_deck(deck)


def test_duplicate_lines_are_combined_and_original_retained(deck):
    extra = deepcopy(deck["cards"][1])
    extra["quantity"] = 1
    deck["cards"][1]["quantity"] -= 1
    deck["cards"].append(extra)
    result = validate_deck(deck)
    assert len(result["cards"]) == 3
    assert len(result["source_cards"]) == 4


def test_commander_not_in_main(deck):
    deck["cards"][2]["oracle_id"] = deck["cards"][0]["oracle_id"]
    with pytest.raises(ValueError, match="also appear"):
        validate_deck(deck)


def test_companion_does_not_change_100_card_count(deck):
    deck["cards"].append(
        {
            "oracle_id": "00000000-0000-4000-8000-000000000005",
            "zone": "companion",
            "quantity": 1,
        }
    )
    assert validate_deck(deck)


def test_stream_json_array_and_compressed_jsonl(tmp_path):
    data = [{"name": "Lórien Revealed"}, {"name": "Second card"}]
    array = tmp_path / "cards.json"
    array.write_text(json.dumps(data), encoding="utf-8")
    assert list(records(array)) == data
    compressed = tmp_path / "cards.jsonl.gz"
    with gzip.open(compressed, "wt", encoding="utf-8") as stream:
        stream.write("\n".join(json.dumps(item) for item in data))
    assert list(records(compressed)) == data
