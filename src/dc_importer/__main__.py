import argparse
import json
import sys
from pathlib import Path

import psycopg

from .client import data_client, rpc
from .db import build_sql, connect, migrate
from .models import catalog_card, validate_deck
from .repository import load_cards, load_decks, records, resolve_name


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Duel Commander database setup and data writes")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Create/update the dedicated two-table schema")
    sql = sub.add_parser("build-sql", help="Build the re-runnable SQL Editor setup")
    sql.add_argument("--output", type=Path, default=Path("supabase/bootstrap.sql"))
    scrape = sub.add_parser("scrape", help="Import published MTGTop8 Duel Commander lists")
    scrape.add_argument("--state", type=Path, default=Path("data/mtgtop8-state.json"))
    scrape.add_argument("--report", type=Path, default=Path("data/run-report.json"))
    scrape.add_argument("--lookback-days", type=int, default=14)
    scrape.add_argument("--since", help="Historical discovery start date, YYYY-MM-DD")
    scrape.add_argument("--max-pages", type=int, default=5)
    scrape.add_argument("--max-events", type=int, default=20)
    scrape.add_argument("--max-decks", type=int, default=50)
    scrape.add_argument("--max-requests", type=int, default=150)
    scrape.add_argument("--max-runtime-minutes", type=int, default=15)
    scrape.add_argument("--refresh-hours", type=int, default=24)
    scrape.add_argument("--event-id", type=int, action="append")
    scrape.add_argument("--timezone", default="America/El_Salvador")
    scrape.add_argument("--force", action="store_true")
    scrape.add_argument("--dry-run", action="store_true")
    for command in ("load-cards", "load-decks"):
        cmd = sub.add_parser(command)
        cmd.add_argument("file", type=Path)
        cmd.add_argument("--dry-run", action="store_true")
    resolve = sub.add_parser("resolve-name")
    resolve.add_argument("name")
    alias = sub.add_parser("add-alias")
    alias.add_argument("alias")
    alias.add_argument("oracle_id")
    alias.add_argument("--note", required=True)
    state = sub.add_parser("set-state")
    state.add_argument("source_deck_id", type=int)
    state.add_argument("state", choices=["quarantined", "removed"])
    state.add_argument("--reason", required=True)
    for name in ("usage", "decks"):
        query = sub.add_parser(name)
        query.add_argument("commanders", nargs="+", help="One commander UUID or an exact pair")
        query.add_argument("--match", choices=["exact", "contains"], default="exact")
        query.add_argument("--from", dest="date_from", required=True)
        query.add_argument("--to", dest="date_to", required=True, help="Exclusive upper event date")
        query.add_argument("--platform", choices=["paper", "mtgo", "other_online", "unknown"])
        query.add_argument("--event-kind", choices=["tournament", "league", "unknown"])
        query.add_argument("--min-players", type=int)
        query.add_argument("--max-rank", type=int)
        query.add_argument("--limit", type=int, default=50)
        query.add_argument("--offset", type=int, default=0)
        if name == "usage":
            query.add_argument("--exclude-lands", action="store_true")
            query.add_argument("--include-basics", action="store_true")
        else:
            query.add_argument("--card-id")
    return root


def run(args: argparse.Namespace) -> dict:
    if args.command == "scrape":
        from .scraper import scrape

        return scrape(args)
    if args.command == "build-sql":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(build_sql(), encoding="utf-8")
        return {"sql_file": str(args.output)}
    if args.command in {"load-cards", "load-decks"} and args.dry_run:
        validate = catalog_card if args.command == "load-cards" else validate_deck
        count = sum(1 for payload in records(args.file) if validate(payload))
        return {"validated_records": count, "dry_run": True, "database_checked": False}
    if args.command == "migrate":
        with connect() as conn:
            return {"applied": migrate(conn)}
    with data_client() as client:
        if args.command == "load-cards":
            return load_cards(client, args.file)
        if args.command == "load-decks":
            return load_decks(client, list(records(args.file)))
        if args.command == "resolve-name":
            return resolve_name(client, args.name)
        if args.command == "add-alias":
            rpc(
                client,
                "add_reviewed_alias",
                {
                    "p_alias": args.alias,
                    "p_oracle_id": args.oracle_id,
                    "p_note": args.note,
                },
            )
            return {"alias_added": args.alias}
        if args.command == "set-state":
            rpc(
                client,
                "set_deck_state",
                {
                    "p_source_deck_id": args.source_deck_id,
                    "p_state": args.state,
                    "p_reason": args.reason,
                },
            )
            return {"source_deck_id": args.source_deck_id, "state": args.state}
        arguments = {
            "p_commander_ids": args.commanders,
            "p_match_mode": args.match,
            "p_date_from": args.date_from,
            "p_date_to": args.date_to,
            "p_platform": args.platform,
            "p_event_kind": args.event_kind,
            "p_min_players": args.min_players,
            "p_max_rank": args.max_rank,
            "p_limit": args.limit,
            "p_offset": args.offset,
        }
        if args.command == "usage":
            arguments.update(
                p_include_lands=not args.exclude_lands, p_include_basics=args.include_basics
            )
            return rpc(client, "get_commander_card_usage", arguments)
        arguments["p_card_id"] = args.card_id
        return rpc(client, "get_commander_decks", arguments)


def main() -> int:
    args = parser().parse_args()
    try:
        result = run(args)
    except (ValueError, OSError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        # Server diagnostics contain the SQL error, never connection credentials.
        print(f"Database error: {exc.diag.message_primary or exc.sqlstate}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 1 if args.command == "scrape" and result["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
