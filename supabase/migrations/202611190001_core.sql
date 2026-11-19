-- Fresh dedicated project only. Run in filename order, in a transaction.
-- No dependency on another Supabase project or on extensions.
do $$
begin
  if to_regclass('public.mtg_decks') is not null
     or to_regclass('public.cards') is not null then
    raise exception 'Core tables already exist. Use the migration runner; do not overwrite a vault.';
  end if;
end $$;

create schema if not exists ingestion;
revoke all on schema ingestion from public;

create table ingestion.deployment (
  singleton boolean primary key default true check (singleton),
  application text not null check (application = 'duel-commander-data'),
  project_ref text not null,
  created_at timestamptz not null default now()
);
insert into ingestion.deployment (application, project_ref)
values ('duel-commander-data', 'djayrdiyastspmoaqkml');

create function ingestion.name_key(p_name text) returns text
language sql immutable strict parallel safe set search_path = '' as $$
  select lower(regexp_replace(
    replace(replace(normalize(btrim(p_name), NFKC), chr(8216), chr(39)), chr(8217), chr(39)),
    '[[:space:]]+', ' ', 'g'));
$$;

create table public.cards (
  oracle_id uuid primary key,
  name text not null check (btrim(name) <> ''),
  face_names text[] not null default '{}',
  layout text not null default 'normal',
  mana_cost text not null default '',
  mana_value numeric not null default 0 check (mana_value >= 0),
  type_line text not null default '',
  oracle_text text not null default '',
  color_identity text[] not null default '{}' check (color_identity <@ array['W','U','B','R','G']),
  legalities jsonb not null default '{}' check (jsonb_typeof(legalities) = 'object'),
  faces jsonb not null default '[]' check (jsonb_typeof(faces) = 'array'),
  image_uri text,
  scryfall_uri text,
  is_land boolean not null default false,
  is_basic_land boolean not null default false,
  first_seen_at timestamptz not null default now(),
  synced_at timestamptz not null default now()
);
create index cards_name_idx on public.cards (name);

create table public.card_names (
  alias_name text not null check (btrim(alias_name) <> ''),
  name_key text generated always as (ingestion.name_key(alias_name)) stored,
  oracle_id uuid not null references public.cards,
  alias_kind text not null check (alias_kind in ('catalog', 'reviewed')),
  provenance text not null,
  primary key (name_key, oracle_id)
);

create table public.events (
  id uuid primary key default gen_random_uuid(),
  source text not null default 'mtgtop8' check (source = 'mtgtop8'),
  source_event_id bigint not null check (source_event_id > 0),
  format_id text not null default 'duel_commander' check (format_id = 'duel_commander'),
  title text not null check (btrim(title) <> ''),
  location text,
  event_date date,
  raw_date text,
  player_count integer check (player_count > 0),
  platform text not null default 'unknown' check (platform in ('paper','mtgo','other_online','unknown')),
  event_kind text not null default 'unknown' check (event_kind in ('tournament','league','unknown')),
  coverage text not null default 'unknown' check (coverage in ('full','top_finishes','unknown')),
  source_url text not null,
  organizer_source_url text,
  metadata jsonb not null default '{}' check (jsonb_typeof(metadata) = 'object'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (source, source_event_id, format_id)
);
create index events_cohort_idx on public.events (event_date, platform, event_kind);

create table public.commander_groups (
  group_key text primary key,
  commander_oracle_ids uuid[] not null,
  display_name text not null,
  check (cardinality(commander_oracle_ids) between 1 and 2)
);
create table public.commander_group_members (
  group_key text not null references public.commander_groups,
  oracle_id uuid not null references public.cards,
  primary key (group_key, oracle_id)
);
create index commander_members_card_idx on public.commander_group_members (oracle_id, group_key);

create table ingestion.import_runs (
  id uuid primary key default gen_random_uuid(),
  mode text not null,
  status text not null default 'running' check (status in ('running','success','partial','error')),
  requested_from date,
  requested_to date,
  parser_version text,
  stats jsonb not null default '{}',
  error_summary text,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  check (requested_to is null or requested_from is null or requested_to > requested_from)
);
create table ingestion.source_fetches (
  id uuid primary key default gen_random_uuid(),
  run_id uuid references ingestion.import_runs,
  url text not null,
  http_status integer not null check (http_status between 100 and 599),
  fetched_at timestamptz not null default now(),
  content_type text,
  charset text,
  etag text,
  last_modified text,
  body_sha256 text check (body_sha256 ~ '^[0-9a-f]{64}$'),
  storage_path text,
  byte_count bigint check (byte_count >= 0)
);

create table public.mtg_decks (
  id uuid primary key default gen_random_uuid(),
  deck_id text not null unique,
  deck_name text not null,
  owner text,
  commander text not null,
  cards jsonb not null default '[]' check (jsonb_typeof(cards) = 'array'),
  total_cards integer not null check (total_cards > 0),
  source text not null default 'mtgtop8' check (source = 'mtgtop8'),
  source_deck_id bigint not null check (source_deck_id > 0),
  format_id text not null default 'duel_commander' check (format_id = 'duel_commander'),
  event_id uuid not null references public.events,
  source_url text not null,
  placement_text text,
  rank_min integer check (rank_min > 0),
  rank_max integer check (rank_max >= rank_min),
  result_kind text not null default 'unknown' check (result_kind in ('tournament_finish','league_result','unknown')),
  import_state text not null default 'pending' check (import_state in ('pending','ready','quarantined','removed')),
  state_reason text,
  current_version_id uuid,
  created_at timestamptz not null default now(),
  last_updated timestamptz not null default now(),
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  unique (source, source_deck_id, format_id),
  check (deck_id = 'mtgtop8:duel_commander:' || source_deck_id::text),
  check (rank_max is null or rank_min is not null),
  check (result_kind = 'tournament_finish' or (rank_min is null and rank_max is null))
);
create index mtg_decks_event_idx on public.mtg_decks (event_id);

create table public.deck_versions (
  id uuid primary key default gen_random_uuid(),
  deck_id uuid not null references public.mtg_decks,
  revision_number integer not null check (revision_number > 0),
  list_hash text not null check (list_hash ~ '^[0-9a-f]{64}$'),
  hash_version integer not null default 1 check (hash_version = 1),
  parser_version text not null,
  commander_group_key text not null references public.commander_groups,
  source_cards jsonb not null check (jsonb_typeof(source_cards) = 'array'),
  raw_text text,
  html_fetch_id uuid references ingestion.source_fetches,
  export_fetch_id uuid references ingestion.source_fetches,
  validation_findings jsonb not null default '[]',
  accepted_at timestamptz not null default now(),
  unique (deck_id, revision_number),
  unique (deck_id, id)
);
alter table public.mtg_decks add constraint mtg_decks_current_version_fk
foreign key (id, current_version_id) references public.deck_versions (deck_id, id)
deferrable initially deferred;
alter table public.mtg_decks add constraint mtg_decks_ready_has_version
check (import_state <> 'ready' or current_version_id is not null);
create index deck_versions_group_idx on public.deck_versions (commander_group_key, id);

create table public.deck_cards (
  version_id uuid not null references public.deck_versions,
  oracle_id uuid not null references public.cards,
  zone text not null check (zone in ('commander','main','companion','sideboard','maybeboard')),
  quantity integer not null check (quantity > 0),
  primary key (version_id, zone, oracle_id)
);
create index deck_cards_reverse_idx on public.deck_cards (oracle_id, version_id);

create function ingestion.deny_revision_mutation() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception 'Accepted revisions and card rows are immutable; import a new revision';
end $$;
create trigger deck_versions_immutable before update or delete on public.deck_versions
for each row execute function ingestion.deny_revision_mutation();
create trigger deck_cards_immutable before update or delete on public.deck_cards
for each row execute function ingestion.deny_revision_mutation();

create table ingestion.crawl_tasks (
  id uuid primary key default gen_random_uuid(),
  task_key text not null unique,
  task_kind text not null check (task_kind in ('discovery','event','deck','reconcile','catalog')),
  url text not null,
  run_id uuid references ingestion.import_runs,
  source_event_id bigint,
  source_deck_id bigint,
  state text not null default 'pending' check (state in ('pending','leased','done','error')),
  priority integer not null default 0,
  attempts integer not null default 0 check (attempts >= 0),
  next_check_at timestamptz not null default now(),
  lease_expires_at timestamptz,
  last_checked_at timestamptz,
  last_success_at timestamptz,
  parsed_content_hash text,
  payload jsonb not null default '{}',
  last_error text
);
create index crawl_tasks_due_idx on ingestion.crawl_tasks (state, next_check_at, priority);
create table ingestion.source_state (
  source text primary key,
  last_request_started_at timestamptz,
  cooldown_until timestamptz,
  checkpoint jsonb not null default '{}',
  last_source_change_at timestamptz,
  updated_at timestamptz not null default now()
);
create table ingestion.unresolved_card_names (
  id uuid primary key default gen_random_uuid(),
  source_name text not null,
  name_key text generated always as (ingestion.name_key(source_name)) stored,
  fetch_id uuid references ingestion.source_fetches,
  source_deck_id bigint,
  candidates uuid[] not null default '{}',
  reason text not null,
  state text not null default 'open' check (state in ('open','resolved','ignored')),
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);

-- A deferred consistency check also protects direct administrator inserts.
create function ingestion.check_current_deck() returns trigger
language plpgsql security definer set search_path = '' as $$
declare v public.deck_versions; d public.mtg_decks; ids uuid[]; n bigint; projection jsonb;
begin
  select * into d from public.mtg_decks where id = new.id;
  if d.import_state <> 'ready' then return null; end if;
  select * into strict v from public.deck_versions where id = d.current_version_id;
  select array_agg(oracle_id order by oracle_id), sum(quantity)
    into ids, n from public.deck_cards where version_id = v.id and zone = 'commander';
  if ids is distinct from (select commander_oracle_ids from public.commander_groups
                           where group_key = v.commander_group_key)
     or n <> cardinality(ids) then raise exception 'Commander group does not match list'; end if;
  select sum(quantity), jsonb_agg(jsonb_build_object(
      'section', case when dc.zone = 'commander' then 'Commander' else 'Deck' end,
      'qty', dc.quantity, 'name', c.name) order by dc.zone, dc.oracle_id)
    into n, projection
    from public.deck_cards dc join public.cards c using (oracle_id)
    where version_id = v.id and zone in ('main','commander');
  if n <> 100 or n <> d.total_cards or projection is distinct from d.cards then
    raise exception 'Ready list must contain 100 main/commander cards and a consistent JSON projection';
  end if;
  return null;
end $$;
create constraint trigger mtg_decks_consistency after insert or update on public.mtg_decks
deferrable initially deferred for each row execute function ingestion.check_current_deck();
