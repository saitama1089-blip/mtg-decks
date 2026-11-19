import gzip
import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import ijson
from psycopg import Connection
from psycopg.types.json import Jsonb

from .db import check_identity
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


def begin_run(conn: Connection, mode: str) -> str:
    check_identity(conn)
    return conn.execute(
        "insert into ingestion.import_runs(mode) values (%s) returning id", (mode,)
    ).fetchone()[0]


def finish_run(conn: Connection, run_id: str, stats: dict, error: bool = False) -> None:
    conn.execute(
        """update ingestion.import_runs set status=%s,stats=%s,error_summary=%s,
        finished_at=now() where id=%s""",
        (
            "error" if error else "success",
            Jsonb(stats),
            "Load rolled back" if error else None,
            run_id,
        ),
    )


def load_decks(conn: Connection, payloads: list[dict]) -> dict:
    # Validate the complete file before starting writes. A bad deck rolls back this entire batch.
    validated = [validate_deck(payload) for payload in payloads]
    if not 1 <= len(validated) <= 1000:
        raise ValueError("A deck batch must contain 1 to 1000 records")
    run_id = begin_run(conn, "load-decks")
    counts = Counter()
    try:
        with conn.transaction():
            for payload in validated:
                result = conn.execute(
                    "select ingestion.import_deck(%s)", (Jsonb(payload),)
                ).fetchone()[0]
                counts[result["action"]] += 1
            stats = {"decks": len(validated), **counts}
            finish_run(conn, run_id, stats)
        return {"run_id": str(run_id), **stats}
    except Exception:
        finish_run(conn, run_id, {"decks": 0}, error=True)
        raise


def load_cards(conn: Connection, path: Path, batch_size: int = 500) -> dict:
    if not 1 <= batch_size <= 2000:
        raise ValueError("Catalog batch size must be between 1 and 2000")
    run_id = begin_run(conn, "load-cards")
    count = 0
    batch = []
    try:
        for payload in records(path):
            batch.append(catalog_card(payload))
            if len(batch) == batch_size:
                with conn.transaction():
                    count += conn.execute(
                        "select ingestion.upsert_cards(%s)", (Jsonb(batch),)
                    ).fetchone()[0]
                batch = []
        if batch:
            with conn.transaction():
                count += conn.execute(
                    "select ingestion.upsert_cards(%s)", (Jsonb(batch),)
                ).fetchone()[0]
        finish_run(conn, run_id, {"cards": count})
        return {"run_id": str(run_id), "cards": count}
    except Exception:
        conn.execute(
            """update ingestion.import_runs set status=%s,stats=%s,
            error_summary='Catalog batch failed; earlier batches remain committed',finished_at=now()
            where id=%s""",
            ("partial" if count else "error", Jsonb({"cards": count}), run_id),
        )
        raise


def resolve_name(conn: Connection, name: str) -> dict:
    rows = conn.execute(
        """select c.oracle_id,c.name from public.card_names n join public.cards c using(oracle_id)
        where n.name_key=ingestion.name_key(%s) order by c.oracle_id""",
        (name,),
    ).fetchall()
    return {
        "status": "resolved" if len(rows) == 1 else "ambiguous" if rows else "unresolved",
        "candidates": [{"oracle_id": str(row[0]), "name": row[1]} for row in rows],
    }
