-- Dedicated Duel Commander project. Exactly two application tables.
-- Re-runnable: inserts/updates go through the functions below.
-- Refuse the previous, undeployed schema or an unrelated vault; never drop user data.
do $$ begin
  if to_regclass('public.mtg_decks') is not null and
     obj_description(to_regclass('public.mtg_decks')) is distinct from 'mtg-decks:duel-commander:two-tables-v1'
  then raise exception 'Existing mtg_decks uses another schema; conversion needs review'; end if;
  if to_regclass('public.cards') is not null and
     obj_description(to_regclass('public.cards')) is distinct from 'mtg-decks:duel-commander:two-tables-v1'
  then raise exception 'Existing cards uses another schema; conversion needs review'; end if;
end $$;

create or replace function public.normalize_card_name(p_name text) returns text
language sql immutable strict parallel safe set search_path = '' as $$
  select lower(regexp_replace(replace(replace(normalize(btrim(p_name),NFKC),
    chr(8216),chr(39)),chr(8217),chr(39)), '[[:space:]]+', ' ', 'g'));
$$;

create table if not exists public.cards (
  oracle_id uuid primary key,
  name text not null check (btrim(name) <> ''),
  face_names text[] not null default '{}',
  aliases jsonb not null default '[]' check (jsonb_typeof(aliases) = 'array'),
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
  is_land boolean generated always as
    (coalesce(faces->0->>'type_line',type_line) ~ '\mLand\M') stored,
  is_basic_land boolean generated always as
    (coalesce(faces->0->>'type_line',type_line) ~ '\mBasic\M' and
     coalesce(faces->0->>'type_line',type_line) ~ '\mLand\M') stored,
  created_at timestamptz not null default now(),
  last_updated timestamptz not null default now()
);
comment on table public.cards is 'mtg-decks:duel-commander:two-tables-v1';
create index if not exists cards_name_key_idx on public.cards (public.normalize_card_name(name));

create table if not exists public.mtg_decks (
  id uuid primary key default gen_random_uuid(),
  deck_id text not null unique,
  source_deck_id bigint not null unique check (source_deck_id > 0),
  deck_name text not null check (btrim(deck_name) <> ''),
  owner text,
  commander text not null,
  commander_oracle_ids uuid[] not null,
  cards jsonb not null check (jsonb_typeof(cards) = 'array'),
  total_cards integer not null check (total_cards = 100),
  source text not null default 'mtgtop8' check (source = 'mtgtop8'),
  format_id text not null default 'duel_commander' check (format_id = 'duel_commander'),
  source_url text not null,
  event_id bigint not null check (event_id > 0),
  event_name text not null check (btrim(event_name) <> ''),
  event_date date,
  event_location text,
  player_count integer check (player_count > 0),
  platform text not null default 'unknown' check (platform in ('paper','mtgo','other_online','unknown')),
  event_kind text not null default 'unknown' check (event_kind in ('tournament','league','unknown')),
  placement_text text,
  rank_min integer check (rank_min > 0),
  rank_max integer check (rank_max >= rank_min),
  result_kind text not null default 'unknown' check (result_kind in ('tournament_finish','league_result','unknown')),
  import_state text not null default 'ready' check (import_state in ('ready','quarantined','removed')),
  state_reason text,
  list_hash text not null check (list_hash ~ '^[0-9a-f]{64}$'),
  parser_version text not null check (btrim(parser_version) <> ''),
  raw_text text,
  created_at timestamptz not null default now(),
  last_updated timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  check (rank_max is null or rank_min is not null),
  check (result_kind = 'tournament_finish' or (rank_min is null and rank_max is null))
);
comment on table public.mtg_decks is 'mtg-decks:duel-commander:two-tables-v1';
create index if not exists mtg_decks_commander_idx on public.mtg_decks using gin (commander_oracle_ids);
create index if not exists mtg_decks_cards_idx on public.mtg_decks using gin (cards jsonb_path_ops);
create index if not exists mtg_decks_cohort_idx on public.mtg_decks (event_date,platform,event_kind);

create or replace function public.resolve_card_name(p_name text) returns jsonb
language sql stable set search_path = '' as $$
  with matches as (
    select c.oracle_id,c.name from public.cards c
    where public.normalize_card_name(c.name)=public.normalize_card_name(p_name)
       or exists (select 1 from unnest(c.face_names) f
                  where public.normalize_card_name(f)=public.normalize_card_name(p_name))
       or exists (select 1 from jsonb_array_elements(c.aliases) a
                  where public.normalize_card_name(a->>'name')=public.normalize_card_name(p_name))
  ) select jsonb_build_object('status',case count(*) when 0 then 'unresolved'
      when 1 then 'resolved' else 'ambiguous' end,
    'candidates',coalesce(jsonb_agg(to_jsonb(m) order by oracle_id),'[]'::jsonb)) from matches m;
$$;

create or replace function public.prepare_mtg_deck() returns trigger
language plpgsql set search_path = '' as $$
declare x jsonb; entries jsonb := '[]'; r jsonb; cid uuid; v_section text; q numeric;
  normalized jsonb; ids uuid[]; commanders integer; count_cards integer; label text;
begin
  if tg_op='UPDATE' and new.source_deck_id<>old.source_deck_id then
    raise exception 'A source deck ID cannot change';
  end if;
  if jsonb_typeof(new.cards) is distinct from 'array'
     or jsonb_array_length(new.cards) not between 1 and 500 then
    raise exception 'Expected 1 to 500 card rows';
  end if;
  for x in select value from jsonb_array_elements(new.cards) loop
    v_section := case lower(coalesce(x->>'section',x->>'zone'))
      when 'commander' then 'Commander' when 'main' then 'Deck' when 'deck' then 'Deck'
      when 'companion' then 'Companion' when 'sideboard' then 'Sideboard'
      when 'maybeboard' then 'Maybeboard' end;
    if v_section is null then raise exception 'Card section is missing or unknown'; end if;
    r:=coalesce(x->'qty',x->'quantity');
    if jsonb_typeof(r) is distinct from 'number' then
      raise exception 'Card quantity must be a positive integer'; end if;
    q:=r::text::numeric;
    if q<=0 or q<>trunc(q) or q>1000 then raise exception 'Invalid card quantity'; end if;
    if x->>'oracle_id' is not null then cid:=(x->>'oracle_id')::uuid;
    else
      r:=public.resolve_card_name(x->>'name');
      if r->>'status'<>'resolved' then raise exception 'Unresolved or ambiguous card: %',x->>'name'; end if;
      cid:=(r->'candidates'->0->>'oracle_id')::uuid;
    end if;
    if not exists (select 1 from public.cards where oracle_id=cid) then
      raise exception 'Unknown Oracle ID; load cards first'; end if;
    entries:=entries || jsonb_build_object('oracle_id',cid,'section',v_section,'qty',q::integer);
  end loop;
  select jsonb_agg(jsonb_build_object('oracle_id',a.oracle_id,'section',a.section,
    'qty',a.qty,'name',c.name) order by a.section,a.oracle_id) into normalized
  from (select oracle_id,section,sum(qty)::integer qty from jsonb_to_recordset(entries)
    as t(oracle_id uuid,section text,qty integer) group by oracle_id,section) a
    join public.cards c using (oracle_id);
  select array_agg(oracle_id order by oracle_id),sum(qty) into ids,commanders
    from jsonb_to_recordset(normalized) as t(oracle_id uuid,section text,qty integer)
    where section='Commander';
  if coalesce(cardinality(ids),0) not between 1 and 2 or commanders<>cardinality(ids)
    or exists (select 1 from jsonb_to_recordset(normalized) as t(oracle_id uuid,section text)
      where section='Deck' and oracle_id=any(ids)) then
    raise exception 'Need one or two distinct commanders, one copy each, outside the main list';
  end if;
  select sum(qty) into count_cards from jsonb_to_recordset(normalized)
    as t(section text,qty integer) where section in ('Commander','Deck');
  if count_cards<>100 then raise exception 'Expected 100 main plus commander cards; got %',count_cards; end if;
  select string_agg(name,' + ' order by oracle_id) into label from public.cards where oracle_id=any(ids);
  new.cards:=normalized; new.commander_oracle_ids:=ids; new.commander:=label;
  new.total_cards:=count_cards; new.deck_id:='mtgtop8:duel_commander:' || new.source_deck_id::text;
  new.source_url:='https://mtgtop8.com/event?e=' || new.event_id || '&d=' || new.source_deck_id || '&f=EDH';
  select encode(sha256(convert_to(string_agg(section || ':' || oracle_id || ':' || qty,
    E'\n' order by section,oracle_id),'UTF8')),'hex') into new.list_hash
    from jsonb_to_recordset(normalized) as t(oracle_id uuid,section text,qty integer);
  new.last_seen_at:=clock_timestamp();
  if tg_op='UPDATE' then
    new.created_at:=old.created_at;
    if (to_jsonb(new)-array['last_seen_at','last_updated']) is not distinct from
       (to_jsonb(old)-array['last_seen_at','last_updated']) then new.last_updated:=old.last_updated;
    else new.last_updated:=clock_timestamp(); end if;
  end if;
  return new;
end $$;
drop trigger if exists trg_prepare_mtg_deck on public.mtg_decks;
create trigger trg_prepare_mtg_deck before insert or update on public.mtg_decks
for each row execute function public.prepare_mtg_deck();

create or replace function public.upsert_cards(p_cards jsonb) returns integer
language plpgsql set search_path = '' as $$
declare x jsonb; affected integer:=0;
begin
  if jsonb_typeof(p_cards) is distinct from 'array' or jsonb_array_length(p_cards)>2000 then
    raise exception 'Expected an array of at most 2000 cards'; end if;
  for x in select value from jsonb_array_elements(p_cards) loop
    insert into public.cards as old (oracle_id,name,face_names,layout,mana_cost,mana_value,
      type_line,oracle_text,color_identity,legalities,faces,image_uri,scryfall_uri)
    values ((x->>'oracle_id')::uuid,x->>'name',
      array(select jsonb_array_elements_text(coalesce(x->'face_names','[]'))),
      coalesce(x->>'layout','normal'),coalesce(x->>'mana_cost',''),
      coalesce((x->>'mana_value')::numeric,0),coalesce(x->>'type_line',''),
      coalesce(x->>'oracle_text',''),
      array(select jsonb_array_elements_text(coalesce(x->'color_identity','[]'))),
      coalesce(x->'legalities','{}'),coalesce(x->'faces','[]'),x->>'image_uri',x->>'scryfall_uri')
    on conflict (oracle_id) do update set name=excluded.name,face_names=excluded.face_names,
      layout=excluded.layout,mana_cost=excluded.mana_cost,mana_value=excluded.mana_value,
      type_line=excluded.type_line,oracle_text=excluded.oracle_text,
      color_identity=excluded.color_identity,legalities=excluded.legalities,faces=excluded.faces,
      image_uri=excluded.image_uri,scryfall_uri=excluded.scryfall_uri,last_updated=clock_timestamp()
    where (old.name,old.face_names,old.layout,old.mana_cost,old.mana_value,old.type_line,
      old.oracle_text,old.color_identity,old.legalities,old.faces,old.image_uri,old.scryfall_uri)
      is distinct from (excluded.name,excluded.face_names,excluded.layout,excluded.mana_cost,
      excluded.mana_value,excluded.type_line,excluded.oracle_text,excluded.color_identity,
      excluded.legalities,excluded.faces,excluded.image_uri,excluded.scryfall_uri);
    affected:=affected+1;
  end loop;
  return affected;
end $$;

create or replace function public.upsert_mtg_deck(p_deck jsonb) returns jsonb
language plpgsql set search_path = '' as $$
declare p jsonb:=p_deck; e jsonb; previous jsonb; saved public.mtg_decks; action text;
begin
  if p->>'source' is distinct from 'mtgtop8' or p->>'format_id' is distinct from 'duel_commander'
     then raise exception 'Only mtgtop8 Duel Commander payloads are accepted'; end if;
  e:=p->'event';
  if jsonb_typeof(e) is distinct from 'object' or nullif(btrim(p->>'parser_version'),'') is null
     then raise exception 'Event and parser_version are required'; end if;
  perform pg_advisory_xact_lock(hashtextextended('mtgtop8:deck:' || (p->>'source_deck_id'),0));
  select to_jsonb(d)-array['last_seen_at','last_updated'] into previous from public.mtg_decks d
    where source_deck_id=(p->>'source_deck_id')::bigint for update;
  insert into public.mtg_decks (source_deck_id,deck_name,owner,cards,event_id,event_name,
    event_date,event_location,player_count,platform,event_kind,placement_text,rank_min,rank_max,
    result_kind,parser_version,raw_text)
  values ((p->>'source_deck_id')::bigint,p->>'deck_name',coalesce(p->>'player_name',p->>'owner'),
    p->'cards',(e->>'source_event_id')::bigint,e->>'title',(e->>'event_date')::date,
    e->>'location',(e->>'player_count')::integer,coalesce(e->>'platform','unknown'),
    coalesce(e->>'event_kind','unknown'),p->>'placement_text',(p->>'rank_min')::integer,
    (p->>'rank_max')::integer,coalesce(p->>'result_kind','unknown'),p->>'parser_version',p->>'raw_text')
  on conflict (source_deck_id) do update set deck_name=excluded.deck_name,owner=excluded.owner,
    cards=excluded.cards,event_id=excluded.event_id,event_name=excluded.event_name,
    event_date=excluded.event_date,event_location=excluded.event_location,
    player_count=excluded.player_count,platform=excluded.platform,event_kind=excluded.event_kind,
    placement_text=excluded.placement_text,rank_min=excluded.rank_min,rank_max=excluded.rank_max,
    result_kind=excluded.result_kind,parser_version=excluded.parser_version,raw_text=excluded.raw_text,
    import_state='ready',state_reason=null returning * into saved;
  action:=case when previous is null then 'inserted'
    when previous=(to_jsonb(saved)-array['last_seen_at','last_updated']) then 'unchanged' else 'updated' end;
  return jsonb_build_object('id',saved.id,'deck_id',saved.deck_id,'action',action,'list_hash',saved.list_hash);
end $$;

create or replace function public.upsert_mtg_decks(p_decks jsonb) returns jsonb
language plpgsql set search_path = '' as $$
declare p jsonb; r jsonb; inserted integer:=0; updated integer:=0; unchanged integer:=0;
begin
  if jsonb_typeof(p_decks) is distinct from 'array' or jsonb_array_length(p_decks) not between 1 and 100 then
    raise exception 'Expected a batch of 1 to 100 decks'; end if;
  for p in select value from jsonb_array_elements(p_decks) loop
    r:=public.upsert_mtg_deck(p);
    case r->>'action' when 'inserted' then inserted:=inserted+1;
      when 'updated' then updated:=updated+1; else unchanged:=unchanged+1; end case;
  end loop;
  return jsonb_build_object('inserted',inserted,'updated',updated,'unchanged',unchanged);
end $$;

create or replace function public.add_reviewed_alias(p_alias text,p_oracle_id uuid,p_note text)
returns void language plpgsql set search_path = '' as $$
begin
  if nullif(btrim(p_alias),'') is null or nullif(btrim(p_note),'') is null then
    raise exception 'Alias and review note are required'; end if;
  update public.cards set aliases=coalesce((select jsonb_agg(a) from jsonb_array_elements(aliases) a
    where public.normalize_card_name(a->>'name')<>public.normalize_card_name(p_alias)),'[]'::jsonb)
    || jsonb_build_object('name',p_alias,'note',p_note),last_updated=clock_timestamp()
    where oracle_id=p_oracle_id;
  if not found then raise exception 'Unknown Oracle ID'; end if;
end $$;

create or replace function public.set_deck_state(p_source_deck_id bigint,p_state text,p_reason text)
returns void language plpgsql set search_path = '' as $$
begin
  if p_state is null or p_state not in ('quarantined','removed') or nullif(btrim(p_reason),'') is null
    then raise exception 'Use quarantined/removed and a reason'; end if;
  perform pg_advisory_xact_lock(hashtextextended('mtgtop8:deck:' || p_source_deck_id,0));
  update public.mtg_decks set import_state=p_state,state_reason=p_reason where source_deck_id=p_source_deck_id;
  if not found then raise exception 'Source deck not found'; end if;
end $$;

alter table public.cards enable row level security;
alter table public.mtg_decks enable row level security;
revoke all on public.cards,public.mtg_decks from public;
revoke execute on function public.upsert_cards,public.upsert_mtg_deck,public.upsert_mtg_decks,
  public.resolve_card_name,public.add_reviewed_alias,public.set_deck_state,public.prepare_mtg_deck from public;
do $$ declare r text;
begin
  foreach r in array array['anon','authenticated'] loop
    if exists (select from pg_roles where rolname=r) then
      execute format('revoke all on public.cards,public.mtg_decks from %I',r);
      execute format('revoke execute on function public.upsert_cards,public.upsert_mtg_deck,
        public.upsert_mtg_decks,public.resolve_card_name,public.add_reviewed_alias,
        public.set_deck_state,public.prepare_mtg_deck from %I',r);
    end if;
  end loop;
  if exists (select from pg_roles where rolname='service_role') then
    grant select,insert,update on public.cards,public.mtg_decks to service_role;
    grant execute on function public.upsert_cards,public.upsert_mtg_deck,public.upsert_mtg_decks,
      public.resolve_card_name,public.add_reviewed_alias,public.set_deck_state,public.prepare_mtg_deck
      to service_role;
  end if;
end $$;
