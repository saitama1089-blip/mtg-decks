"""Extract the selected Duel Commander deck, never navigation or other formats."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from .models import validate_deck

BASE = "https://mtgtop8.com"
INDEX = BASE + "/format?f=EDH"
PARSER_VERSION = "mtgtop8-html-v1"
DATE = re.compile(r"\b\d{2}/\d{2}/(?:\d{4}|\d{2})\b")


class ParseError(ValueError):
    pass


def event_url(event_id: int, deck_id: int | None = None) -> str:
    params = {"e": event_id, "f": "EDH"}
    if deck_id is not None:
        params["d"] = deck_id
    return BASE + "/event?" + urlencode(params)


def source_link(href: str, base: str) -> tuple[str, dict]:
    url = urljoin(base, href)
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"mtgtop8.com", "www.mtgtop8.com"}:
        raise ParseError("Unexpected source link")
    if parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ParseError("Unexpected source URL credentials or port")
    return url, parse_qs(parsed.query)


def source_date(value: str) -> date:
    for pattern in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    raise ParseError(f"Invalid event date: {value}")


@dataclass
class IndexPage:
    events: list[dict]
    next_url: str | None
    all_url: str | None


@dataclass
class EventPage:
    event: dict
    decks: dict[int, dict]
    selected_id: int


def parse_index(html: str, url: str = INDEX) -> IndexPage:
    soup = BeautifulSoup(html, "html.parser")
    if not soup.title or not soup.title.get_text().startswith("Duel Commander events"):
        raise ParseError("Expected the Duel Commander event listing")
    events = {}
    # The featured-events sidebar includes old events. Only paginate the main list.
    tables = soup.select("table.Stable")
    rows = max(
        (
            [row for row in table.select("tr.hover_tr") if row.find_parent("table") is table]
            for table in tables
        ),
        key=len,
        default=[],
    )
    for row in rows:
        links = row.select("a[href]")
        for link in links:
            try:
                _, query = source_link(link["href"], url)
            except ParseError:
                continue
            if query.get("f") != ["EDH"] or not query.get("e"):
                continue
            match = DATE.search(row.get_text(" ", strip=True))
            if not match:
                raise ParseError("Event row is missing its date")
            identity = int(query["e"][0])
            events.setdefault(
                identity,
                {
                    "source_event_id": identity,
                    "title": link.get_text(" ", strip=True),
                    "event_date": source_date(match[0]).isoformat(),
                },
            )
            break
    next_url = all_url = None
    for link in soup.select("a[href]"):
        label = link.get_text(" ", strip=True)
        if label not in {"Next", "All Commander decks"}:
            continue
        candidate, query = source_link(link["href"], url)
        if query.get("f") != ["EDH"]:
            continue
        if label == "Next":
            next_url = candidate
        else:
            all_url = candidate
    if not events:
        raise ParseError("No event rows found; the listing layout may have changed")
    return IndexPage(list(events.values()), next_url, all_url)


def parse_event(html: str, expected_event_id: int) -> EventPage:
    soup = BeautifulSoup(html, "html.parser")
    marker = soup.select_one(".meta_arch")
    if not marker or marker.get_text(" ", strip=True) != "Duel Commander":
        raise ParseError("The source page is not Duel Commander")
    event_title = soup.select_one(".event_title")
    if event_title:
        title = event_title.get_text(" ", strip=True)
    else:
        title = soup.title.get_text() if soup.title else ""
        match = re.fullmatch(r"Duel Commander event - (.+) @ mtgtop8\.com", title)
        if not match:
            raise ParseError("Event title is missing")
        title = match[1]
    title, separator, location = title.partition(" @ ")
    meta = marker.parent.get_text(" ", strip=True)
    match_date = DATE.search(meta)
    if not match_date:
        raise ParseError("Event date is missing")
    participants = re.search(r"([\d, ]+) players\b", meta)
    platform_image = marker.find("img", title=True)
    platform = {
        "Paper": "paper",
        "MTG Online": "mtgo",
        "Online Other": "other_online",
    }.get(platform_image.get("title") if platform_image else None, "unknown")
    # MTGO League labels enumerate published lists. A paper store's "Weekly League"
    # can have real tournament finishes, so its name alone does not imply that behavior.
    kind = (
        "league" if platform == "mtgo" and re.search(r"\bleague\b", title, re.I) else "tournament"
    )
    event = {
        "source_event_id": expected_event_id,
        "title": title,
        "location": location if separator else None,
        "event_date": source_date(match_date[0]).isoformat(),
        "player_count": int(re.sub(r"\D", "", participants[1])) if participants else None,
        "platform": platform,
        "event_kind": kind,
    }
    decks = {}
    selected_id = None
    for link in soup.select("a[href]"):
        if not link.get_text(strip=True):
            continue
        try:
            _, query = source_link(link["href"], event_url(expected_event_id))
        except ParseError:
            continue
        if query.get("f") != ["EDH"] or not query.get("d") or not query.get("e"):
            continue
        if int(query["e"][0]) != expected_event_id:
            continue
        row = link.find_parent(class_=lambda x: x and x in {"chosen_tr", "hover_tr"})
        if row is None:
            continue
        identity = int(query["d"][0])
        rank_node = row.select_one(".S14")
        player = row.select_one("a.player")
        label = rank_node.get_text(" ", strip=True) if rank_node else None
        rank = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", label or "")
        reliable_rank = kind == "tournament" and rank and int(rank[2] or rank[1]) >= int(rank[1])
        decks[identity] = {
            "source_deck_id": identity,
            "deck_name": link.get_text(" ", strip=True),
            "player_name": player.get_text(" ", strip=True) if player else None,
            "placement_text": label,
            "rank_min": int(rank[1]) if reliable_rank else None,
            "rank_max": int(rank[2] or rank[1]) if reliable_rank else None,
            "result_kind": "league_result"
            if kind == "league"
            else "tournament_finish"
            if reliable_rank
            else "unknown",
        }
        if "chosen_tr" in row.get("class", []):
            selected_id = identity
    if not decks or selected_id not in decks:
        raise ParseError("Published deck IDs or selected deck are missing")
    return EventPage(event, decks, selected_id)


def parse_deck(html: str, event_id: int, expected_deck_id: int | None = None) -> dict:
    event = parse_event(html, event_id)
    if expected_deck_id is not None and event.selected_id != expected_deck_id:
        raise ParseError("Returned deck ID differs from the requested deck")
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    zone = None
    sections = {
        "COMMANDER": "commander",
        "SIDEBOARD": "sideboard",
        "COMPANION": "companion",
        "MAYBEBOARD": "maybeboard",
    }
    for node in soup.select(".O14, .deck_line"):
        text = node.get_text(" ", strip=True)
        if "deck_line" not in node.get("class", []):
            label = re.sub(r"^\d+\s+", "", text).upper()
            if label in sections:
                zone = sections[label]
            elif re.match(
                r"^\d+\s+(LAND|CREATURE|INSTANT|SORCER|OTHER|SPELL|ARTIFACT|ENCHANT|PLANESWALKER)",
                text,
                re.I,
            ):
                zone = "main"
            else:
                zone = None
            continue
        name = node.select_one(".L14")
        quantity = re.match(r"^(\d+)\s", text)
        if zone is None or name is None or quantity is None:
            raise ParseError("Card row is missing its section, name, or quantity")
        cards.append(
            {"name": name.get_text(" ", strip=True), "quantity": int(quantity[1]), "zone": zone}
        )
    payload = {
        "source": "mtgtop8",
        "format_id": "duel_commander",
        "parser_version": PARSER_VERSION,
        "event": event.event,
        **event.decks[event.selected_id],
        "cards": cards,
        "raw_text": "\n".join(f"{c['zone']}: {c['quantity']} {c['name']}" for c in cards),
    }
    return validate_deck(payload)
