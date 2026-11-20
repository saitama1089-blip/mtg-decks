# Duel Commander data

Two ordinary Supabase tables, following the existing PauperBrews deck-storage pattern:

| Table | One row per | Stored data |
| --- | --- | --- |
| `mtg_decks` | MTGTop8 deck ID | Player, commander(s), event details, JSON decklist, timestamps |
| `cards` | Scryfall Oracle ID | Card names, faces, types, images, legality, reviewed aliases |

Project: `djayrdiyastspmoaqkml`. Source scope: **MTGTop8 Duel Commander only**.
There are no separate event, revision, queue, or logging tables. A corrected deck updates its
existing row. The current list is retained; historical revisions are not stored.

**Deployment:** installed in this Supabase project on November 19, 2026, through its authenticated
MCP connection. Supabase migration: `20261120030316_create_duel_commander_decks_and_cards`.
Verified both tables, insert/update/query function permissions, and a live Python REST usage query.
The Python worker below imports real published lists into this schema.

## Create the tables once

Run [supabase/bootstrap.sql](supabase/bootstrap.sql) in the new project's SQL Editor.
The script creates the two tables, indexes, timestamp/validation trigger, and RPC functions in
one transaction. It is safe to rerun for this schema and refuses a table from another schema.
It never drops existing user data. The previous eight-table draft was not deployed to this project.

Alternatively, configure the project's PostgreSQL connection as `SUPABASE_DB_URL` in `.env` and run:

```bash
python -m dc_importer migrate
```

A Supabase REST secret is sufficient for normal inserts, updates, and queries **after** this SQL
has been applied. Creating tables requires the SQL Editor or a PostgreSQL connection.

## Python setup

```powershell
py -3.14 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock.txt
python -m pip install --no-deps -e .
# Copy .env.example to .env only if you do not already have a configured .env.
```

Set `SUPABASE_URL` and `SUPABASE_SECRET_KEY` in the ignored `.env`, or in GitHub Actions
variables/secrets. The worker checks that its endpoint belongs to this dedicated project.
It sends opaque secret keys in the `apikey` header. Credentials are never committed or printed.

## MTGTop8 scraper and Actions

[Import MTGTop8 Duel Commander](.github/workflows/mtgtop8-sync.yml) runs hourly at minute 17
and supports **Run workflow** for a manual batch. It discovers the latest 14 days of events,
imports up to 50 published decks per run, and checks known lists for corrections every 24 hours.
GitHub schedules can be delayed; the next run resumes saved work.
Change the cron to `17 12 * * *` for a daily run at 12:17 UTC.

The only required repository Actions secret is `SUPABASE_SECRET_KEY`, from this dedicated
project. The project URL/reference are fixed in the workflow. The existing migration workflow
uses a separate database credential and is not needed for scraping.

```bash
# Preview source parsing, with no database writes or Scryfall requests.
python -m dc_importer scrape --dry-run --max-decks 5

# Normal incremental import; a card-catalog download is not required first.
python -m dc_importer scrape

# Target one event, or recheck it immediately, bypassing the 24-hour cache.
python -m dc_importer scrape --event-id 94041 --force

# Historical import: repeat the same command to work through bounded batches.
python -m dc_importer scrape --since 2026-08-22
```

Manual workflow inputs expose the date window, historical start, deck batch size, event ID,
preview mode, and forced refresh. A historical run follows the site's **All Commander decks**
link and its pagination. Historical work outside the normal 14-day window must be resumed
with the same `--since` value (or manual workflow input). Initial runs are bounded batches,
not a claim that every deck in the requested period has already been imported.

The worker reads server-rendered HTML, honors the source's robots directives, and spaces
MTGTop8 requests at least five seconds apart. One run allows at most five listing pages,
20 event manifests, 50 decks, 150 total upstream requests, and 15 minutes by default.
Scryfall's collection API resolves exact card/face names in batches of at most 75; identities
and card metadata are cached for 24 hours. Only cards needed by imported decks are seeded.

Source format and selected deck IDs are checked. A main list plus its one/two commanders
must total 100. League display labels do not become tournament placements. Unresolved or
ambiguous names, incomplete lists, and changed layouts produce errors and a per-record
retry delay of 1-24 hours; they never replace an existing valid list. A reviewed alias can
be added with `add-alias`, then retried with `--force`.

`data/mtgtop8-state.json` holds discovery cursors, queues, retry times, request timing, and
the public card cache. Actions restores and saves this file even after partial/failed runs.
Preview and live Actions caches are separate. No database tables are added for job state.
Cache loss safely replays database upserts. Keep a fixed `--state` path when resuming locally;
use a separate path for local previews if you want an independent discovery checkpoint.

Each run writes `data/run-report.json` and an Actions summary. Reports are downloadable as
artifacts for 14 days. `success` means the discovered, due work within the requested window
finished; `partial` means a configured limit left work queued. Both exit successfully.
`error` makes the Action fail and records the affected IDs or upstream failure.
HTTP 403/challenges stop the run; HTTP 429 also persists a cooldown. There is no bypass.

## Inserts and updates

The normal write pattern is one RPC call with a payload:

```python
supabase.rpc("upsert_mtg_deck", {"p_deck": payload}).execute()
supabase.rpc("upsert_cards", {"p_cards": card_batch}).execute()
```

`upsert_mtg_deck` uses `INSERT ... ON CONFLICT (source_deck_id) DO UPDATE` and returns
`inserted`, `updated`, or `unchanged`. It keeps the same deck row/UUID when the list changes.
`upsert_mtg_decks` accepts an atomic batch of 1-100 decks. An invalid deck rolls back that batch.
`upsert_cards` inserts or updates up to 2,000 card records by `oracle_id`.

Load the card catalog first, then decks:

```bash
python -m dc_importer load-cards data/oracle-cards.jsonl.gz --dry-run
python -m dc_importer load-cards data/oracle-cards.jsonl.gz
python -m dc_importer load-decks data/decks.json --dry-run
python -m dc_importer load-decks data/decks.json
```

Card input accepts Scryfall Oracle JSON arrays, JSONL, or gzip JSONL and streams large files.
Catalog batches commit separately; repeating a partially completed catalog load is safe.
The [Scryfall metadata endpoint](https://api.scryfall.com/bulk-data/oracle_cards) provides its
current download location. All relevant Oracle cards are included, without an Arena-only filter.

See [tests/fixtures/deck.json](tests/fixtures/deck.json) for the deck payload's source/event fields.
Its IDs are synthetic test data. The `cards` array can use the PauperBrews shape:

```json
[
  {"section":"Commander", "qty":1, "name":"Ertai Resurrected"},
  {"section":"Deck", "qty":1, "name":"Counterspell"}
]
```

That fragment illustrates the format; a complete main list plus commander must total 100.
Names are resolved exactly against the card catalog and its face names/reviewed aliases.
For resolved source data, each entry can instead contain `oracle_id`, `zone`, and `quantity`.
The database stores canonical `{section, qty, name, oracle_id}` entries and combines repeated
lines. It derives commander IDs, commander display text, total cards, the source URL, and a
list hash. Commander pairs are order independent. Companion/sideboard/maybeboard rows remain
separate from the main 100-card count.

On a repeat import, `last_seen_at` advances. `last_updated` advances only for a meaningful list
or metadata change. Invalid quantities, ambiguous names, unknown Oracle IDs, other formats,
and incomplete decks are rejected. This validates import completeness, not historical legality.
Dry runs validate file structure without checking catalog foreign keys or writing anything.

```bash
python -m dc_importer resolve-name "Ertai Resurrected"
python -m dc_importer add-alias "Source spelling" ORACLE_UUID --note "Reviewed against source"
python -m dc_importer set-state 913883 quarantined --reason "Source list incomplete"
```

Reviewed aliases live inside the card row and survive catalog updates. Quarantined/removed
rows retain their data and are excluded from usage queries. A valid upsert restores readiness.

## Most-used cards by commander

```bash
python -m dc_importer usage COMMANDER_UUID --from 2026-08-22 --to 2026-11-20
python -m dc_importer usage FIRST_UUID SECOND_UUID --from 2026-08-22 --to 2026-11-20
python -m dc_importer decks COMMANDER_UUID --from 2026-08-22 --to 2026-11-20 --card-id CARD_UUID
```

`get_commander_card_usage` expands the JSON decklists in SQL and joins the card catalog.
It counts one inclusion per main deck, returns the sample size and date interval, and supports
exact pairs or `contains` mode for a selected commander. Land filters change displayed cards,
not the denominator. `get_commander_decks` supplies supporting lists. Both cap pages at 200.

Dates refer to the event; the upper date is exclusive. Platform, event kind, minimum entrants,
and tournament-finish filters are available. Unknown source values do not satisfy an active
filter. League display order is not a tournament rank. These are published-list inclusion
percentages, not card win rates or complete tournament-field shares.

## Permissions and verification

RLS is enabled on both tables. Anonymous/authenticated public clients cannot write or call the
RPCs. Writes and queries run through the server-side `service_role` credential. No extra
application login roles are created. SQL functions use the caller's privileges.

```bash
ruff check .
ruff format --check .
pytest -q
python -m dc_importer build-sql
```

GitHub CI supplies a disposable PostgreSQL 16 instance. It checks that exactly two tables exist,
repeat imports, updates, batch rollback, name resolution, partner matching, usage percentages,
and public access restrictions. Offline scraper tests cover reduced source fixtures, partner and
league parsing, pagination, encoding, rate limits, checkpoint recovery, and idempotent repeats.
Local pytest skips integration tests unless `TEST_DATABASE_URL`
points to an empty disposable database. Tests never load fixture rows into Supabase automatically.

`Apply database migrations` is a manual workflow using `SUPABASE_MIGRATION_DB_URL`.
The scheduled importer uses the ordinary REST upsert functions, without running schema migrations.
