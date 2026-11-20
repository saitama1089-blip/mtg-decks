-- Query functions only; this script adds no tables.
create or replace function public.commander_cohort(
  p_ids uuid[],p_match text,p_from date,p_to date,p_platform text,p_kind text,p_min integer,p_rank integer
) returns setof public.mtg_decks language plpgsql stable set search_path = '' as $$
declare ids uuid[];
begin
  select array_agg(distinct x order by x) into ids from unnest(p_ids) x where x is not null;
  if coalesce(cardinality(ids),0) not between 1 and 2 or cardinality(ids)<>cardinality(p_ids)
     or p_match is null or p_match not in ('exact','contains')
     or (p_match='contains' and cardinality(ids)<>1) then
    raise exception 'Use one/two distinct commander IDs; contains mode accepts one'; end if;
  if p_from is null or p_to is null or p_to<=p_from
     or (p_min is not null and p_min<1) or (p_rank is not null and p_rank<1)
     or (p_platform is not null and p_platform not in ('paper','mtgo','other_online','unknown'))
     or (p_kind is not null and p_kind not in ('tournament','league','unknown')) then
    raise exception 'Invalid date range or cohort filters'; end if;
  return query select d.* from public.mtg_decks d where import_state='ready'
    and event_date>=p_from and event_date<p_to
    and ((p_match='exact' and commander_oracle_ids=ids)
      or (p_match='contains' and commander_oracle_ids @> ids))
    and (p_platform is null or platform=p_platform)
    and (p_kind is null or event_kind=p_kind)
    and (p_min is null or player_count>=p_min)
    and (p_rank is null or (event_kind='tournament' and result_kind='tournament_finish' and rank_max<=p_rank));
end $$;

create or replace function public.get_commander_card_usage(
  p_commander_ids uuid[],p_match_mode text default 'exact',
  p_date_from date default current_date-89,p_date_to date default current_date+1,
  p_platform text default null,p_event_kind text default null,
  p_min_players integer default null,p_max_rank integer default null,
  p_include_lands boolean default true,p_include_basics boolean default false,
  p_limit integer default 50,p_offset integer default 0
) returns jsonb language plpgsql stable set search_path = '' as $$
declare result jsonb;
begin
  if p_limit is null or p_limit not between 1 and 200 or p_offset is null
     or p_offset not between 0 and 100000 or p_include_lands is null or p_include_basics is null
    then raise exception 'Invalid pagination or land filters'; end if;
  with eligible as materialized (
    select * from public.commander_cohort(p_commander_ids,p_match_mode,p_date_from,p_date_to,
      p_platform,p_event_kind,p_min_players,p_max_rank)
  ), counts as (
    select x.oracle_id,count(*) decks_with_card,sum(x.qty) total_copies from eligible d
    cross join lateral jsonb_to_recordset(d.cards) as x(oracle_id uuid,section text,qty integer)
    where x.section='Deck' group by x.oracle_id
  ), ranked as (
    select c.oracle_id,c.name,c.image_uri,c.type_line,n.decks_with_card,n.total_copies,
      round(100.0*n.decks_with_card/nullif((select count(*) from eligible),0),2) inclusion_percentage
    from counts n join public.cards c using (oracle_id)
    where (p_include_lands or not c.is_land) and (p_include_basics or not c.is_basic_land)
    order by n.decks_with_card desc,c.oracle_id limit p_limit offset p_offset
  ) select jsonb_build_object(
    'eligible_decks',(select count(*) from eligible),
    'distinct_events',(select count(distinct event_id) from eligible),
    'known_player_labels',(select count(distinct owner) from eligible where owner is not null),
    'observed_date_from',(select min(event_date) from eligible),
    'observed_date_to',(select max(event_date) from eligible),
    'last_checked_at',(select max(last_seen_at) from eligible),
    'last_source_change_at',(select max(last_updated) from eligible),
    'filters',jsonb_build_object('commander_ids',p_commander_ids,'match_mode',p_match_mode,
      'date_from',p_date_from,'date_to_exclusive',p_date_to,'platform',p_platform,
      'event_kind',p_event_kind,'min_players',p_min_players,'max_rank',p_max_rank,
      'include_lands',p_include_lands,'include_basics',p_include_basics),
    'limit',p_limit,'offset',p_offset,
    'cards',coalesce((select jsonb_agg(to_jsonb(r) order by decks_with_card desc,oracle_id)
      from ranked r),'[]'::jsonb)) into result;
  return result;
end $$;

create or replace function public.get_commander_decks(
  p_commander_ids uuid[],p_match_mode text default 'exact',
  p_date_from date default current_date-89,p_date_to date default current_date+1,
  p_platform text default null,p_event_kind text default null,
  p_min_players integer default null,p_max_rank integer default null,
  p_card_id uuid default null,p_limit integer default 50,p_offset integer default 0
) returns jsonb language plpgsql stable set search_path = '' as $$
declare result jsonb;
begin
  if p_limit is null or p_limit not between 1 and 200 or p_offset is null
     or p_offset not between 0 and 100000 then raise exception 'Invalid pagination'; end if;
  with filtered as (
    select d.* from public.commander_cohort(p_commander_ids,p_match_mode,p_date_from,p_date_to,
      p_platform,p_event_kind,p_min_players,p_max_rank) d
    where p_card_id is null or cards @> jsonb_build_array(jsonb_build_object(
      'section','Deck','oracle_id',p_card_id))
  ), page as (
    select id,deck_id,deck_name,owner,event_date,event_name,source_url,placement_text from filtered
    order by event_date desc,id limit p_limit offset p_offset
  ) select jsonb_build_object('total',(select count(*) from filtered),'limit',p_limit,
    'offset',p_offset,'decks',coalesce((select jsonb_agg(to_jsonb(x) order by event_date desc,id)
      from page x),'[]'::jsonb)) into result;
  return result;
end $$;

revoke execute on function public.commander_cohort,public.get_commander_card_usage,
  public.get_commander_decks from public;
do $$ declare r text;
begin
  foreach r in array array['anon','authenticated'] loop
    if exists (select from pg_roles where rolname=r) then
      execute format('revoke execute on function public.commander_cohort,
        public.get_commander_card_usage,public.get_commander_decks from %I',r);
    end if;
  end loop;
  if exists (select from pg_roles where rolname='service_role') then
    grant execute on function public.commander_cohort,public.get_commander_card_usage,
      public.get_commander_decks to service_role;
  end if;
end $$;
notify pgrst,'reload schema';
