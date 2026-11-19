create function ingestion.validate_cohort(
  p_ids uuid[],p_match text,p_from date,p_to date,p_platform text,p_kind text,p_min integer,p_rank integer
) returns uuid[] language plpgsql immutable set search_path = '' as $$
declare ids uuid[];
begin
  select array_agg(distinct x order by x) into ids from unnest(p_ids) x where x is not null;
  if coalesce(cardinality(ids),0) not between 1 and 2 or cardinality(ids) <> cardinality(p_ids)
     or p_match is null or p_match not in ('exact','contains')
     or (p_match='contains' and cardinality(ids)<>1) then
    raise exception 'Use one/two distinct commander IDs; contains mode accepts one';
  end if;
  if p_from is null or p_to is null or p_to<=p_from
     or (p_min is not null and p_min<1) or (p_rank is not null and p_rank<1)
     or (p_platform is not null and p_platform not in ('paper','mtgo','other_online','unknown'))
     or (p_kind is not null and p_kind not in ('tournament','league','unknown')) then
    raise exception 'Invalid date range or cohort filters';
  end if;
  return ids;
end $$;

create function ingestion.cohort(
  p_ids uuid[],p_match text,p_from date,p_to date,p_platform text,p_kind text,p_min integer,p_rank integer
) returns table (deck_id uuid,version_id uuid,event_id uuid,owner text,event_date date,source_url text)
language sql stable set search_path = '' as $$
  select d.id,v.id,e.id,d.owner,e.event_date,d.source_url
  from public.mtg_decks d
  join public.deck_versions v on v.id=d.current_version_id
  join public.commander_groups g on g.group_key=v.commander_group_key
  join public.events e on e.id=d.event_id
  where d.import_state='ready' and e.event_date>=p_from and e.event_date<p_to
    and ((p_match='exact' and g.commander_oracle_ids=p_ids)
      or (p_match='contains' and g.commander_oracle_ids @> p_ids))
    and (p_platform is null or e.platform=p_platform)
    and (p_kind is null or e.event_kind=p_kind)
    and (p_min is null or e.player_count>=p_min)
    and (p_rank is null or (e.event_kind='tournament' and d.result_kind='tournament_finish'
                           and d.rank_max<=p_rank));
$$;

create function public.get_commander_card_usage(
  p_commander_ids uuid[],p_match_mode text default 'exact',
  p_date_from date default current_date-89,p_date_to date default current_date+1,
  p_platform text default null,p_event_kind text default null,
  p_min_players integer default null,p_max_rank integer default null,
  p_include_lands boolean default true,p_include_basics boolean default false,
  p_limit integer default 50,p_offset integer default 0
) returns jsonb language plpgsql stable security definer set search_path = '' as $$
declare ids uuid[]; result jsonb;
begin
  ids:=ingestion.validate_cohort(p_commander_ids,p_match_mode,p_date_from,p_date_to,
    p_platform,p_event_kind,p_min_players,p_max_rank);
  if p_limit is null or p_limit not between 1 and 200
     or p_offset is null or p_offset not between 0 and 100000
     or p_include_lands is null or p_include_basics is null then
    raise exception 'Invalid pagination or land filters';
  end if;
  with eligible as materialized (
    select * from ingestion.cohort(ids,p_match_mode,p_date_from,p_date_to,
      p_platform,p_event_kind,p_min_players,p_max_rank)
  ), counts as (
    select dc.oracle_id,count(*) decks_with_card,sum(dc.quantity) total_copies
    from eligible x join public.deck_cards dc on dc.version_id=x.version_id
    where dc.zone='main' group by dc.oracle_id
  ), ranked as (
    select c.oracle_id,c.name,c.image_uri,c.type_line,n.decks_with_card,n.total_copies,
      round(100.0*n.decks_with_card/nullif((select count(*) from eligible),0),2)
        inclusion_percentage
    from counts n join public.cards c using (oracle_id)
    where (p_include_lands or not c.is_land) and (p_include_basics or not c.is_basic_land)
    order by n.decks_with_card desc,c.oracle_id limit p_limit offset p_offset
  ) select jsonb_build_object(
    'eligible_decks',(select count(*) from eligible),
    'distinct_events',(select count(distinct event_id) from eligible),
    'known_player_labels',(select count(distinct owner) from eligible where owner is not null),
    'observed_date_from',(select min(event_date) from eligible),
    'observed_date_to',(select max(event_date) from eligible),
    'filters',jsonb_build_object('commander_ids',ids,'match_mode',p_match_mode,
      'date_from',p_date_from,'date_to_exclusive',p_date_to,'platform',p_platform,
      'event_kind',p_event_kind,'min_players',p_min_players,'max_rank',p_max_rank,
      'include_lands',p_include_lands,'include_basics',p_include_basics),
    'limit',p_limit,'offset',p_offset,
    'last_successful_import_at',(select max(finished_at) from ingestion.import_runs
      where status='success' and mode='load-decks'),
    'cards',coalesce((select jsonb_agg(to_jsonb(r) order by decks_with_card desc,oracle_id)
                     from ranked r),'[]'::jsonb)
  ) into result;
  return result;
end $$;

create function public.get_commander_decks(
  p_commander_ids uuid[],p_match_mode text default 'exact',
  p_date_from date default current_date-89,p_date_to date default current_date+1,
  p_platform text default null,p_event_kind text default null,
  p_min_players integer default null,p_max_rank integer default null,
  p_card_id uuid default null,p_limit integer default 50,p_offset integer default 0
) returns jsonb language plpgsql stable security definer set search_path = '' as $$
declare ids uuid[]; result jsonb;
begin
  ids:=ingestion.validate_cohort(p_commander_ids,p_match_mode,p_date_from,p_date_to,
    p_platform,p_event_kind,p_min_players,p_max_rank);
  if p_limit is null or p_limit not between 1 and 200 or p_offset is null
     or p_offset not between 0 and 100000 then raise exception 'Invalid pagination'; end if;
  with eligible as (
    select * from ingestion.cohort(ids,p_match_mode,p_date_from,p_date_to,
      p_platform,p_event_kind,p_min_players,p_max_rank)
  ), filtered as (
    select x.* from eligible x where p_card_id is null or exists (
      select 1 from public.deck_cards dc where dc.version_id=x.version_id
      and dc.zone='main' and dc.oracle_id=p_card_id)
  ), page as (
    select f.deck_id,d.deck_name,f.owner,f.event_date,f.source_url,
      e.title event_name,d.placement_text,v.revision_number
    from filtered f join public.mtg_decks d on d.id=f.deck_id
    join public.events e on e.id=f.event_id
    join public.deck_versions v on v.id=f.version_id
    order by f.event_date desc,f.deck_id limit p_limit offset p_offset
  ) select jsonb_build_object('total',(select count(*) from filtered),'limit',p_limit,
    'offset',p_offset,'decks',coalesce((select jsonb_agg(to_jsonb(x)
      order by event_date desc,deck_id) from page x),'[]'::jsonb)) into result;
  return result;
end $$;

-- Database group roles have no passwords and cannot log in by themselves.
do $$ begin
  if not exists (select from pg_roles where rolname='dc_importer') then
    create role dc_importer nologin;
  end if;
  if not exists (select from pg_roles where rolname='dc_reader') then
    create role dc_reader nologin;
  end if;
end $$;

revoke all on all tables in schema public from public;
revoke all on all tables in schema ingestion from public;
revoke all on all functions in schema ingestion from public;
revoke all on function public.get_commander_card_usage from public;
revoke all on function public.get_commander_decks from public;
grant usage on schema public,ingestion to dc_importer;
grant usage on schema public to dc_reader;
grant select on ingestion.deployment to dc_importer;
grant select on public.cards,public.card_names to dc_importer;
grant execute on function ingestion.name_key(text) to dc_importer;
grant execute on function ingestion.upsert_cards(jsonb),ingestion.import_deck(jsonb),
  ingestion.add_reviewed_alias(text,uuid,text),ingestion.set_deck_state(bigint,text,text)
  to dc_importer;
grant select,insert,update on ingestion.import_runs,ingestion.source_fetches,
  ingestion.crawl_tasks,ingestion.source_state,ingestion.unresolved_card_names to dc_importer;
grant execute on function public.get_commander_card_usage,public.get_commander_decks
  to dc_reader,dc_importer;

-- All API-facing tables are closed by default. Read access is through bounded functions.
do $$ declare t text; r text;
begin
  foreach t in array array['cards','card_names','events','commander_groups',
    'commander_group_members','mtg_decks','deck_versions','deck_cards'] loop
    execute format('alter table public.%I enable row level security',t);
    foreach r in array array['anon','authenticated'] loop
      if exists (select from pg_roles where rolname=r) then
        execute format('revoke all on public.%I from %I',t,r);
      end if;
    end loop;
  end loop;
  foreach r in array array['anon','authenticated'] loop
    if exists (select from pg_roles where rolname=r) then
      execute format('revoke all on all functions in schema ingestion from %I',r);
      execute format('revoke all on function public.get_commander_card_usage from %I',r);
      execute format('revoke all on function public.get_commander_decks from %I',r);
    end if;
  end loop;
  if exists (select from pg_roles where rolname='service_role') then
    grant dc_importer,dc_reader to service_role;
  end if;
end $$;
create policy importer_card_read on public.cards for select to dc_importer using (true);
create policy importer_name_read on public.card_names for select to dc_importer using (true);

-- Prevent later helpers from accidentally inheriting PUBLIC execute permission.
alter default privileges in schema ingestion revoke execute on functions from public;
