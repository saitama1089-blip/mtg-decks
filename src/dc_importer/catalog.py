"""Resolve only the cards a deck needs, caching Scryfall identities for 24 hours."""

import time

from .client import rpc
from .models import catalog_card, name_key, validate_deck


class CardResolver:
    def __init__(self, http, client, cache: dict):
        self.http = http
        self.client = client
        self.cache = cache
        self.cache.setdefault("cards", {})
        self.cache.setdefault("names", {})
        self.uploaded = set()

    def remember(self, card: dict):
        normalized = catalog_card(card)
        identity = normalized["oracle_id"]
        self.cache["cards"][identity] = {"card": normalized, "fetched_at": time.time()}
        for alias in [normalized["name"], *normalized["face_names"]]:
            self.cache["names"][name_key(alias)] = identity

    def cached_identity(self, name: str) -> str | None:
        identity = self.cache["names"].get(name_key(name))
        cached = self.cache["cards"].get(identity)
        if cached and time.time() - cached["fetched_at"] < 86400:
            return identity
        return None

    def resolve(self, payload: dict) -> tuple[dict, int]:
        names = list(dict.fromkeys(card["name"] for card in payload["cards"]))
        missing = [name for name in names if not self.cached_identity(name)]
        for offset in range(0, len(missing), 75):
            response = self.http.collection(missing[offset : offset + 75])
            if not isinstance(response.get("data"), list):
                raise ValueError("Scryfall returned an invalid collection response")
            for card in response["data"]:
                self.remember(card)
        resolved = {}
        card_rows = {}
        for name in names:
            identity = self.cached_identity(name)
            if identity:
                card_rows[identity] = self.cache["cards"][identity]["card"]
            else:
                # Allow an alias deliberately reviewed in the existing Supabase card row.
                match = rpc(self.client, "resolve_card_name", {"p_name": name})
                if match.get("status") != "resolved":
                    raise ValueError(f"Unresolved/ambiguous card name: {name}")
                identity = match["candidates"][0]["oracle_id"]
            resolved[name_key(name)] = identity
        needed = [card for identity, card in card_rows.items() if identity not in self.uploaded]
        if needed:
            rpc(self.client, "upsert_cards", {"p_cards": needed})
            self.uploaded.update(card["oracle_id"] for card in needed)
        result = {
            **payload,
            "cards": [
                {**card, "oracle_id": resolved[name_key(card["name"])]} for card in payload["cards"]
            ],
        }
        return validate_deck(result), len(needed)
