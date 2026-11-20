import gzip
import json
from collections.abc import Iterator
from pathlib import Path

import ijson

from .client import rpc
from .models import catalog_card, validate_deck


def records(path: Path) -> Iterator[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as stream:
        first = b" "
        while first and first.isspace():
            first = stream.read(1)
        stream.seek(0)
        if first == b"[":
            yield from ijson.items(stream, "item", use_float=True)
        elif ".jsonl" in path.suffixes:
            for line in stream:
                if line.strip():
                    yield json.loads(line)
        else:
            value = json.load(stream)
            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object, array, or .jsonl file")
            yield value


def load_decks(client, payloads: list[dict]) -> dict:
    if not 1 <= len(payloads) <= 100:
        raise ValueError("A deck batch must contain 1 to 100 records")
    validated = [validate_deck(payload) for payload in payloads]
    result = rpc(client, "upsert_mtg_decks", {"p_decks": validated})
    return {"decks": len(validated), **result}


def load_cards(client, path: Path, batch_size: int = 500) -> dict:
    if not 1 <= batch_size <= 2000:
        raise ValueError("Catalog batch size must be between 1 and 2000")
    count = 0
    batch = []
    for payload in records(path):
        batch.append(catalog_card(payload))
        if len(batch) == batch_size:
            count += rpc(client, "upsert_cards", {"p_cards": batch})
            batch = []
    if batch:
        count += rpc(client, "upsert_cards", {"p_cards": batch})
    return {"cards_processed": count}


def resolve_name(client, name: str) -> dict:
    return rpc(client, "resolve_card_name", {"p_name": name})
