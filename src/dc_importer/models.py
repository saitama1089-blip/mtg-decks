"""Validate the interchange format before opening a write transaction."""

import copy
import unicodedata
from collections import defaultdict
from uuid import UUID

ZONES = {"main", "commander", "companion", "sideboard", "maybeboard"}


def name_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("‘", "'").replace("’", "'")
    return " ".join(value.lower().split())


def positive_integer(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def validate_deck(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Deck must be a JSON object")
    result = copy.deepcopy(payload)
    if result.get("format_id") != "duel_commander" or result.get("source") != "mtgtop8":
        raise ValueError("Only mtgtop8 Duel Commander payloads are accepted")
    positive_integer(result.get("source_deck_id"), "source_deck_id")
    event = result.get("event")
    if not isinstance(event, dict):
        raise ValueError("event is required")
    positive_integer(event.get("source_event_id"), "source_event_id")
    for value, field in [
        (event.get("title"), "event.title"),
        (result.get("deck_name"), "deck_name"),
        (result.get("parser_version"), "parser_version"),
    ]:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} is required")
    cards = result.get("cards")
    if not isinstance(cards, list) or not 1 <= len(cards) <= 500:
        raise ValueError("cards must contain 1 to 500 rows")
    totals: dict[tuple[str, str], int] = defaultdict(int)
    identities = {}
    sections = {
        "commander": "commander",
        "deck": "main",
        "main": "main",
        "companion": "companion",
        "sideboard": "sideboard",
        "maybeboard": "maybeboard",
    }
    for card in cards:
        if not isinstance(card, dict):
            raise ValueError("Each card must be an object")
        zone = card.get("zone") or sections.get(str(card.get("section", "")).lower())
        if zone not in ZONES:
            raise ValueError("Each card needs a known zone")
        if card.get("oracle_id") is not None:
            try:
                identity = str(UUID(card["oracle_id"]))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("Each card needs a valid oracle_id UUID") from exc
            identities[identity] = {"oracle_id": identity}
        elif isinstance(card.get("name"), str) and card["name"].strip():
            identity = "name:" + name_key(card["name"])
            identities[identity] = {"name": card["name"]}
        else:
            raise ValueError("Each card needs an Oracle UUID or a card name")
        totals[(zone, identity)] += positive_integer(
            card.get("quantity", card.get("qty")), "quantity"
        )
    commanders = {identity: qty for (zone, identity), qty in totals.items() if zone == "commander"}
    if not 1 <= len(commanders) <= 2 or any(qty != 1 for qty in commanders.values()):
        raise ValueError("Need one or two distinct commanders, one copy each")
    if any(("main", identity) in totals for identity in commanders):
        raise ValueError("Commander cannot also appear in the main list")
    count = sum(qty for (zone, _), qty in totals.items() if zone in {"commander", "main"})
    if count != 100:
        raise ValueError(f"Expected 100 main plus commander cards; got {count}")
    result.setdefault("source_cards", copy.deepcopy(cards))
    result["cards"] = [
        {"zone": zone, **identities[identity], "quantity": qty}
        for (zone, identity), qty in sorted(totals.items())
    ]
    return result


def catalog_card(card: dict) -> dict:
    """Accept a Scryfall Oracle object or the normalized catalog contract."""
    if not isinstance(card, dict) or not card.get("name"):
        raise ValueError("Card needs a name and Oracle UUID")
    identity = str(UUID(card["oracle_id"]))
    faces = card.get("card_faces", card.get("faces", []))
    types = card.get("type_line", "")
    front = faces[0] if faces else {}
    images = card.get("image_uris", front.get("image_uris", {}))
    return {
        "oracle_id": identity,
        "name": card["name"],
        "face_names": card.get("face_names", [face["name"] for face in faces]),
        "layout": card.get("layout", "normal"),
        "mana_cost": card.get("mana_cost", front.get("mana_cost", "")),
        "mana_value": card.get("mana_value", card.get("cmc", 0)),
        "type_line": types,
        "oracle_text": card.get(
            "oracle_text", "\n//\n".join(f.get("oracle_text", "") for f in faces)
        ),
        "color_identity": card.get("color_identity", []),
        "legalities": card.get("legalities", {}),
        "faces": faces,
        "image_uri": card.get("image_uri", images.get("normal")),
        "scryfall_uri": card.get("scryfall_uri"),
        "is_land": card.get("is_land", "Land" in front.get("type_line", types)),
        "is_basic_land": card.get("is_basic_land", "Basic Land" in types),
    }
