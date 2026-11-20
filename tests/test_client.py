import io
import json
from unittest.mock import patch

import pytest

from dc_importer.client import RestClient
from dc_importer.db import PROJECT_REF
from dc_importer.models import validate_deck


def test_rest_rpc_sends_secret_only_in_header():
    client = RestClient(f"https://{PROJECT_REF}.supabase.co", "test-only-secret")
    with patch("dc_importer.client.urlopen", return_value=io.BytesIO(b'{"inserted":1}')) as send:
        assert client.rpc("upsert_mtg_decks", {"p_decks": []}) == {"inserted": 1}
    request = send.call_args.args[0]
    assert request.get_header("Apikey") == "test-only-secret"
    assert request.get_header("Authorization") is None
    assert "test-only-secret" not in request.full_url
    assert json.loads(request.data) == {"p_decks": []}


def test_rest_refuses_other_project():
    with pytest.raises(ValueError, match="dedicated"):
        RestClient("https://other.supabase.co", "test-only")


def test_pauperbrews_card_shape_is_accepted(deck):
    deck["cards"] = [
        {"section": "Commander", "qty": 1, "name": "Test Commander A"},
        {"section": "Deck", "qty": 99, "name": "Test Island"},
    ]
    parsed = validate_deck(deck)
    assert parsed["cards"][0]["zone"] == "commander"
    assert parsed["cards"][1]["quantity"] == 99
