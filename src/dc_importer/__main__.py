import argparse
import json
import sys
from pathlib import Path

import psycopg

from .db import build_sql, check_identity, connect, migrate
from .models import catalog_card, validate_deck
from .repository import load_cards, load_decks, records, resolve_name


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Duel Commander database setup and data writes")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Apply pending migrations to the dedicated project")
    sql = sub.add_parser("build-sql", help="Build a one-time SQL Editor bootstrap")
    sql.add_argument("--output", type=Path, default=Path("supabase/bootstrap.sql"))
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
    if args.command == "build-sql":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(build_sql(), encoding="utf-8")
        return {"sql_file": str(args.output)}
    if args.command in {"load-cards", "load-decks"} and args.dry_run:
        validate = catalog_card if args.command == "load-cards" else validate_deck
        count = sum(1 for payload in records(args.file) if validate(payload))
        return {"validated_records": count, "dry_run": True, "database_checked": False}
    with connect() as conn:
        if args.command == "migrate":
            return {"applied": migrate(conn)}
        check_identity(conn)
        if args.command == "load-cards":
            return load_cards(conn, args.file)
        if args.command == "load-decks":
            return load_decks(conn, list(records(args.file)))
        if args.command == "resolve-name":
            return resolve_name(conn, args.name)
        if args.command == "add-alias":
            conn.execute(
                "select ingestion.add_reviewed_alias(%s,%s::uuid,%s)",
                (args.alias, args.oracle_id, args.note),
            )
            return {"alias_added": args.alias}
        if args.command == "set-state":
            conn.execute(
                "select ingestion.set_deck_state(%s,%s,%s)",
                (args.source_deck_id, args.state, args.reason),
            )
            return {"source_deck_id": args.source_deck_id, "state": args.state}
        common = (
            args.commanders,
            args.match,
            args.date_from,
            args.date_to,
            args.platform,
            args.event_kind,
            args.min_players,
            args.max_rank,
        )
        if args.command == "usage":
            return conn.execute(
                """select public.get_commander_card_usage(
                %s::uuid[],%s,%s::date,%s::date,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (*common, not args.exclude_lands, args.include_basics, args.limit, args.offset),
            ).fetchone()[0]
        return conn.execute(
            """select public.get_commander_decks(
            %s::uuid[],%s,%s::date,%s::date,%s,%s,%s,%s,%s::uuid,%s,%s)""",
            (*common, args.card_id, args.limit, args.offset),
        ).fetchone()[0]


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
