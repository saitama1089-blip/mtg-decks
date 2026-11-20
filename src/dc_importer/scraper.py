"""A bounded, resumable job. Checkpoints live in a file, not extra database tables."""

import json
import os
import time
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from .catalog import CardResolver
from .client import data_client, rpc
from .db import PROJECT_REF
from .http import BudgetReached, PoliteHTTP, SourceUnavailable, utc_now
from .mtgtop8 import INDEX, PARSER_VERSION, event_url, parse_deck, parse_event, parse_index
from .repository import load_decks


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def load_state(path: Path) -> dict:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("version") != 1 or state.get("project") != PROJECT_REF:
            raise ValueError("Checkpoint belongs to another project/version")
        return state
    return {
        "version": 1,
        "project": PROJECT_REF,
        "http": {},
        "catalog": {},
        "events": {},
        "decks": {},
        "discovery": {},
    }


def due(record: dict, refresh_seconds: float, force: bool = False) -> bool:
    if record.get("retry_at", 0) > time.time() and not force:
        return False
    return (
        force
        or not record.get("last_success")
        or record.get("parser_version") != PARSER_VERSION
        or time.time() - record["last_success"] >= refresh_seconds
    )


def queue_events(state: dict, events: list[dict], since: date):
    for item in events:
        if date.fromisoformat(item["event_date"]) >= since:
            identity = str(item["source_event_id"])
            state["events"].setdefault(identity, {}).update({"discovered": item})


def discover(http, state: dict, since: date, max_pages: int, archive: bool, report: dict):
    page = http.page(INDEX)
    root = parse_index(page.text, page.url)
    queue_events(state, root.events, since)
    report["listing_pages"] += 1
    root_ids = [e["source_event_id"] for e in root.events]
    mode = "archive" if archive else "recent"
    cursor = state["discovery"].get(mode, {})
    if cursor.get("since") != since.isoformat():
        cursor = {}
    if cursor.get("root_ids") == root_ids and cursor.get("completed_at", 0) > time.time() - 86400:
        return
    url = cursor.get("next_url") or (root.all_url if archive else root.next_url)
    if archive and not url:
        raise ValueError("The source no longer exposes its historical listing")
    if min(date.fromisoformat(e["event_date"]) for e in root.events) < since:
        url = None
    seen = {INDEX}
    while url and report["listing_pages"] < max_pages:
        if url in seen:
            raise ValueError("Pagination repeated a page")
        seen.add(url)
        page = http.page(url)
        parsed = parse_index(page.text, page.url)
        report["listing_pages"] += 1
        queue_events(state, parsed.events, since)
        url = parsed.next_url
        if min(date.fromisoformat(e["event_date"]) for e in parsed.events) < since:
            url = None
        state["discovery"][mode] = {
            "since": since.isoformat(),
            "next_url": url,
            "root_ids": root_ids,
        }
    state["discovery"][mode] = {
        "since": since.isoformat(),
        "next_url": url,
        "root_ids": root_ids,
        "completed_at": time.time() if not url else 0,
    }
    report["discovery_truncated"] = bool(url)


def fail_record(record: dict, error: Exception):
    record["failures"] = record.get("failures", 0) + 1
    record["retry_at"] = time.time() + min(86400, 3600 * 2 ** min(record["failures"] - 1, 5))
    record["last_error"] = str(error)[:500]


def summarize(report: dict):
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with Path(summary).open("a", encoding="utf-8") as stream:
        stream.write("## MTGTop8 Duel Commander\n\n")
        for field in (
            "status",
            "dry_run",
            "listing_pages",
            "events_checked",
            "deck_attempts",
            "parsed",
            "inserted",
            "updated",
            "unchanged",
            "cards_upserted",
            "source_requests",
            "remaining_events",
            "remaining_due",
        ):
            stream.write(f"- {field}: {report.get(field)}\n")
        if report["errors"]:
            stream.write("\nErrors are recorded in the run-report artifact.\n")


def scrape(args) -> dict:
    for name in ("max_pages", "max_events", "max_decks", "max_requests", "max_runtime_minutes"):
        if not 1 <= getattr(args, name) <= 10000:
            raise ValueError(f"{name} must be between 1 and 10000")
    if args.lookback_days < 1 or args.refresh_hours < 1:
        raise ValueError("Lookback and refresh settings must be positive")
    if any(identity <= 0 for identity in (args.event_id or [])):
        raise ValueError("Event IDs must be positive")
    today = datetime.now(ZoneInfo(args.timezone)).date()
    since = (
        date.fromisoformat(args.since)
        if args.since
        else today - timedelta(days=args.lookback_days - 1)
    )
    if since > today:
        raise ValueError("The discovery window starts in the future")
    state = load_state(args.state)
    http = PoliteHTTP(state["http"], args.max_requests, args.max_runtime_minutes * 60)
    refresh = args.refresh_hours * 3600
    report = {
        "started_at": utc_now(),
        "status": "success",
        "dry_run": args.dry_run,
        "window_from": since.isoformat(),
        "listing_pages": 0,
        "events_checked": 0,
        "deck_attempts": 0,
        "parsed": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "cards_upserted": 0,
        "discovery_truncated": False,
        "errors": [],
    }
    selected_pages = {}
    completed_events, completed_decks = set(), set()
    explicit = {str(identity) for identity in (args.event_id or [])}
    try:
        with nullcontext(None) if args.dry_run else data_client() as client:
            # Checkpoints must reflect committed writes even if a later request fails.
            if isinstance(client, psycopg.Connection):
                client.autocommit = True
            if client is not None:
                rpc(client, "upsert_cards", {"p_cards": []})  # Verify access before crawling.
            resolver = CardResolver(http, client, state["catalog"])
            http.load_robots()
            if explicit:
                for identity in explicit:
                    state["events"].setdefault(
                        identity,
                        {
                            "discovered": {
                                "source_event_id": int(identity),
                                "event_date": today.isoformat(),
                            }
                        },
                    )
            else:
                discover(
                    http,
                    state,
                    since,
                    args.max_pages,
                    bool(args.since) or args.lookback_days > 14,
                    report,
                )
            write_json(args.state, state)
            events = sorted(
                state["events"].items(),
                key=lambda pair: (
                    bool(pair[1].get("last_success")),
                    pair[1].get("discovered", {}).get("event_date", ""),
                ),
            )
            for identity, record in events:
                if explicit and identity not in explicit:
                    continue
                if not explicit and record["discovered"]["event_date"] < since.isoformat():
                    continue
                if not due(record, refresh, args.force):
                    continue
                if report["events_checked"] >= args.max_events:
                    break
                http.check_budget()
                report["events_checked"] += 1
                try:
                    page = http.page(event_url(int(identity)))
                    parsed = parse_event(page.text, int(identity))
                    record.update(
                        last_success=time.time(),
                        parser_version=PARSER_VERSION,
                        failures=0,
                        retry_at=0,
                        event=parsed.event,
                    )
                    for deck_id in parsed.decks:
                        state["decks"].setdefault(str(deck_id), {}).update(
                            event_id=int(identity), event_date=parsed.event["event_date"]
                        )
                    selected_pages[parsed.selected_id] = page.text
                    completed_events.add(identity)
                    record.pop("last_error", None)
                except (BudgetReached, SourceUnavailable):
                    raise
                except ValueError as error:
                    fail_record(record, error)
                    report["errors"].append({"event_id": int(identity), "error": str(error)[:500]})
                finally:
                    write_json(args.state, state)
            decks = sorted(
                state["decks"].items(),
                key=lambda pair: (
                    bool(pair[1].get("last_success")),
                    pair[1].get("last_success", 0),
                    int(pair[0]),
                ),
            )
            for identity, record in decks:
                if explicit and str(record["event_id"]) not in explicit:
                    continue
                if not explicit and record["event_date"] < since.isoformat():
                    continue
                if not due(record, refresh, args.force):
                    continue
                if report["deck_attempts"] >= args.max_decks:
                    break
                http.check_budget()
                report["deck_attempts"] += 1
                try:
                    html = selected_pages.get(int(identity))
                    if html is None:
                        html = http.page(event_url(record["event_id"], int(identity))).text
                    payload = parse_deck(html, record["event_id"], int(identity))
                    report["parsed"] += 1
                    if not args.dry_run:
                        payload, uploaded = resolver.resolve(payload)
                        counts = load_decks(client, [payload])
                        report["cards_upserted"] += uploaded
                        for key in ("inserted", "updated", "unchanged"):
                            report[key] += counts.get(key, 0)
                        record.update(
                            last_success=time.time(),
                            parser_version=PARSER_VERSION,
                            failures=0,
                            retry_at=0,
                        )
                        record.pop("last_error", None)
                    completed_decks.add(identity)
                except (BudgetReached, SourceUnavailable):
                    raise
                except ValueError as error:
                    fail_record(record, error)
                    report["errors"].append({"deck_id": int(identity), "error": str(error)[:500]})
                finally:
                    write_json(args.state, state)
    except BudgetReached as error:
        report.update(status="partial", stop_reason=str(error))
    except (SourceUnavailable, ValueError) as error:
        report.update(status="error", stop_reason=str(error))
    except Exception as error:
        # Preserve the checkpoint/report without exposing credentials in diagnostics.
        report.update(status="error", stop_reason=f"Unexpected {type(error).__name__}")
        raise
    finally:
        report["source_requests"] = http.requests
        report["requests_by_host"] = http.by_host
        report["ended_at"] = utc_now()
        report["remaining_events"] = sum(
            due(record, refresh, args.force)
            for identity, record in state["events"].items()
            if (not explicit or identity in explicit)
            and (explicit or record["discovered"]["event_date"] >= since.isoformat())
            and identity not in completed_events
        )
        report["remaining_due"] = sum(
            due(record, refresh, args.force)
            for identity, record in state["decks"].items()
            if (not explicit or str(record["event_id"]) in explicit)
            and (explicit or record["event_date"] >= since.isoformat())
            and identity not in completed_decks
        )
        if report["errors"]:
            report["status"] = "error"
        elif report["status"] == "success" and (
            report["remaining_events"] or report["remaining_due"] or report["discovery_truncated"]
        ):
            report["status"] = "partial"
        write_json(args.state, state)
        write_json(args.report, report)
        summarize(report)
    return report
