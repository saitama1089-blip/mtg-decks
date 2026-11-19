# Duel Commander data

Database foundation for MTGTop8 **Duel Commander only**. Target Supabase project:
`djayrdiyastspmoaqkml` (`https://djayrdiyastspmoaqkml.supabase.co`).

This repository includes SQL migrations, transactional insert/update functions, a Python CLI,
commander card-usage queries, and database tests. Crawling MTGTop8 and scheduling hourly imports
are the next stage. No scraper runs from this version.

## Create the tables

Choose one method for the new project.

**SQL Editor:** paste [supabase/bootstrap.sql](supabase/bootstrap.sql) into the new project's
SQL Editor and run it once. It creates the complete schema in a transaction and records the
migration checksums. Future updates can use the Python migration runner.

**Python:** install from this checkout, copy `.env.example` to `.env`, and replace the placeholder
with the new project's PostgreSQL direct or session-pooler connection from Dashboard > Connect.
The HTTPS project URL is not a PostgreSQL connection string. Keep `.env` private.

```powershell
py -3.14 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock.txt
python -m pip install --no-deps -e .
Copy-Item .env.example .env
# Set SUPABASE_DB_URL in .env, then:
python -m dc_importer migrate
```

The runner applies each migration once and rejects edited migration files. Its connection check
requires the dedicated project reference and TLS; subsequent writes also verify the database's
deployment marker. No database password is included in this repository.

Alternatively, store the same admin connection string as the GitHub repository secret
`SUPABASE_MIGRATION_DB_URL` and manually run **Apply database migrations** under Actions.
That workflow has no schedule. It uses admin credentials only for migrations.

## Tables and permissions

| Schema | Tables |
| --- | --- |
| `public` | `cards`, `card_names`, `events`, `mtg_decks`, `deck_versions`, `deck_cards`, `commander_groups`, `commander_group_members` |
| `ingestion` (private) | `deployment`, `schema_migrations`, `import_runs`, `source_fetches`, `crawl_tasks`, `source_state`, `unresolved_card_names` |

`mtg_decks` preserves the useful PauperBrews fields, including the `{section, qty, name}` JSON
projection. Normalized cards and immutable revisions power statistics. Only one accepted current
revision per source deck contributes. Identical lists from different players/events remain
distinct observations. The format is enforced in the database.

Public tables have RLS enabled. Anonymous and ordinary authenticated API clients have no access
by default. `dc_reader` can execute bounded usage/source-list queries. `dc_importer` can execute
validated write functions, read the catalog, and manage ingestion records. Both are NOLOGIN group
roles. The Supabase `service_role`, when present, inherits these groups. A direct worker login can
be granted `dc_importer` by the database administrator; group-role creation does not create a login
or password. Keep database-owner credentials for migrations and use an importer login for jobs.

The CLI currently uses a PostgreSQL connection, not the Supabase REST key. Read API functions are
closed to `anon` until a deliberate application access decision is made. Functions use explicit
search paths. Raw source capture storage is represented by private paths; bucket provisioning and
the storage uploader are part of the later crawler stage.

## Insert and update data

Load the Oracle card catalog **before** decks. The card loader accepts a Scryfall Oracle JSON
array, normalized JSON objects, `.jsonl`, or `.jsonl.gz` files and streams large arrays. Get the
current download URL from [Scryfall bulk metadata](https://api.scryfall.com/bulk-data/oracle_cards).
It does not filter to Arena cards. Each batch is atomic; a failed large catalog load reports how
many earlier batches committed and is safe to repeat.

```bash
python -m dc_importer load-cards data/oracle-cards.jsonl.gz --dry-run
python -m dc_importer load-cards data/oracle-cards.jsonl.gz
python -m dc_importer load-decks data/decks.json --dry-run
python -m dc_importer load-decks data/decks.json
```

Deck input is one object or an array. See [the input fixture](tests/fixtures/deck.json) for the
contract. Fixtures contain **synthetic IDs** and are for tests only. Production rows need actual
Scryfall Oracle UUIDs, explicit source/format, an event, source deck ID, parser version, and card
rows with `oracle_id`, `zone`, and `quantity`. Main plus commander must total 100; companions and
other outside-deck zones are retained separately. This validates import completeness, not full
historical rules legality.

A repeated source deck/list keeps its revision. Different list content creates a new immutable
revision and updates the current pointer atomically. Reverting A -> B -> A preserves three
revisions. Repeated fetches do not change the content-update timestamp. A deck batch is limited
to 1,000 decks and rolls back fully if any deck fails. Each committed batch gets an import log.
Dry runs validate the file without connecting or writing; they do not verify catalog foreign keys.

Resolve names exactly against canonical names/face aliases, or add a reviewed spelling:

```bash
python -m dc_importer resolve-name "Ertai Resurrected"
python -m dc_importer add-alias "Source spelling" ACTUAL_ORACLE_UUID --note "Reviewed against source"
python -m dc_importer set-state 913883 quarantined --reason "Source list is incomplete"
```

Ambiguous aliases return all candidates. They never choose a fuzzy match. Quarantined/removed
decks retain history and are excluded from usage queries. A validated import restores readiness.
Do not mark a deck removed because a single fetch failed.

The underlying SQL entry points are `ingestion.upsert_cards(jsonb)`,
`ingestion.import_deck(jsonb)`, `ingestion.add_reviewed_alias(text,uuid,text)`, and
`ingestion.set_deck_state(bigint,text,text)`. The Python CLI binds parameters; raw SQL strings are
not interpolated from deck data.

## Fetch cards used by a commander

```bash
python -m dc_importer usage COMMANDER_ORACLE_UUID --from 2026-08-22 --to 2026-11-20
python -m dc_importer usage FIRST_UUID SECOND_UUID --from 2026-08-22 --to 2026-11-20
python -m dc_importer decks COMMANDER_ORACLE_UUID --from 2026-08-22 --to 2026-11-20 --card-id CARD_UUID
```

Dates refer to the event; `--to` is exclusive. Exact matching separates a single commander from
partner pairs, and pair order does not matter. `--match contains` includes every pairing with one
selected commander. Filters include platform, event kind, minimum entrants, and maximum tournament
rank. Unknown source values cannot satisfy an active filter. League display order is not treated
as a tournament rank.

`public.get_commander_card_usage` returns the sample size, events, observed interval, filter
definition, and ranked cards. Inclusion is **decks containing the card / eligible decks**, counting
one inclusion per main deck. Lands/basic-land filtering changes the displayed cards, never the
denominator. Commanders and outside-deck zones are excluded from the numerator. Basic lands are
hidden by default; use `--include-basics` to show them. `public.get_commander_decks` supplies paged
supporting lists. Both queries cap page size at 200 and order results deterministically.

These are published-list usage counts, not win rates or complete tournament-field shares.

## Verification and development

```bash
ruff check .
ruff format --check .
pytest -q
python -m dc_importer build-sql
```

Without `TEST_DATABASE_URL`, local pytest runs the offline tests and skips the database scenario.
To run database integration checks locally, set it to an **empty, disposable PostgreSQL database**.
The tests never drop existing tables and refuse a nonempty target. GitHub CI supplies PostgreSQL
16 and runs the migration, import, revision, rollback, partner, usage, RLS/role, and immutability
checks. Test data is not loaded into Supabase automatically.

Edit migrations only before deployment. After deployment, add a new migration and regenerate
`bootstrap.sql` with `python -m dc_importer build-sql`; the runner detects checksum drift. The
bootstrap is a fresh-project convenience, not an update script to rerun on existing tables.

References: [Supabase database connections](https://supabase.com/docs/guides/database/connecting-to-postgres),
[Supabase RLS](https://supabase.com/docs/guides/database/postgres/row-level-security),
[Psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html).
