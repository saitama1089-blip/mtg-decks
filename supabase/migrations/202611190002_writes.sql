create function ingestion.upsert_cards(p_cards jsonb) returns integer
language plpgsql security definer set search_path = '' as $$
declare x jsonb; cid uuid; affected integer := 0;
begin
  if jsonb_typeof(p_cards) is distinct from 'array' or jsonb_array_length(p_cards) > 2000 then
    raise exception 'Expected an array of at most 2000 cards';
  end if;
  for x in select value from jsonb_array_elements(p_cards) loop
    if jsonb_typeof(x) <> 'object' or nullif(btrim(x->>'name'), '') is null
       or x->>'oracle_id' is null then raise exception 'Card needs oracle_id and name'; end if;
    cid := (x->>'oracle_id')::uuid;
    insert into public.cards as old (
      oracle_id, name, face_names, layout, mana_cost, mana_value, type_line, oracle_text,
      color_identity, legalities, faces, image_uri, scryfall_uri, is_land, is_basic_land
    ) values (
      cid, x->>'name', array(select jsonb_array_elements_text(coalesce(x->'face_names','[]'))),
      coalesce(x->>'layout','normal'), coalesce(x->>'mana_cost',''),
      coalesce((x->>'mana_value')::numeric,0), coalesce(x->>'type_line',''),
      coalesce(x->>'oracle_text',''),
      array(select jsonb_array_elements_text(coalesce(x->'color_identity','[]'))),
      coalesce(x->'legalities','{}'), coalesce(x->'faces','[]'), x->>'image_uri',
      x->>'scryfall_uri', coalesce((x->>'is_land')::boolean,false),
      coalesce((x->>'is_basic_land')::boolean,false)
    ) on conflict (oracle_id) do update set
      name = excluded.name, face_names = excluded.face_names, layout = excluded.layout,
      mana_cost = excluded.mana_cost, mana_value = excluded.mana_value,
      type_line = excluded.type_line, oracle_text = excluded.oracle_text,
      color_identity = excluded.color_identity, legalities = excluded.legalities,
      faces = excluded.faces, image_uri = excluded.image_uri,
      scryfall_uri = excluded.scryfall_uri, is_land = excluded.is_land,
      is_basic_land = excluded.is_basic_land, synced_at = now();
    -- Replace catalog aliases only; reviewed source spellings survive catalog refreshes.
    delete from public.card_names where oracle_id = cid and alias_kind = 'catalog';
    insert into public.card_names (alias_name, oracle_id, alias_kind, provenance)
    select distinct on (ingestion.name_key(n)) n, cid, 'catalog', 'scryfall'
    from (select x->>'name' n union all
          select jsonb_array_elements_text(coalesce(x->'face_names','[]'))) names
    where nullif(btrim(n),'') is not null
    on conflict (name_key, oracle_id) do nothing;
    affected := affected + 1;
  end loop;
  return affected;
end $$;

create function ingestion.add_reviewed_alias(p_alias text, p_oracle_id uuid, p_note text)
returns void language plpgsql security definer set search_path = '' as $$
begin
  if nullif(btrim(p_note),'') is null then raise exception 'Alias review note is required'; end if;
  insert into public.card_names (alias_name, oracle_id, alias_kind, provenance)
  values (p_alias, p_oracle_id, 'reviewed', p_note)
  on conflict (name_key, oracle_id) do update
    set alias_kind = 'reviewed', provenance = excluded.provenance;
end $$;

create function ingestion.import_deck(p jsonb) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare
  e jsonb; ev uuid; did uuid; vid uuid; existing public.mtg_decks;
  ids uuid[]; group_id text; group_name text; normalized jsonb; projection jsonb;
  content_hash text; total integer; next_revision integer; was_new boolean; changed boolean;
begin
  if p->>'format_id' is distinct from 'duel_commander'
     or p->>'source' is distinct from 'mtgtop8' then
    raise exception 'Only mtgtop8 Duel Commander payloads are accepted';
  end if;
  e := p->'event';
  if jsonb_typeof(e) is distinct from 'object'
     or jsonb_typeof(p->'cards') is distinct from 'array'
     or nullif(btrim(p->>'parser_version'),'') is null
     or nullif(btrim(p->>'deck_name'),'') is null then
    raise exception 'Missing event, cards, deck_name, or parser_version';
  end if;
  if jsonb_array_length(p->'cards') = 0 or jsonb_array_length(p->'cards') > 500 then
    raise exception 'Expected 1 to 500 card rows';
  end if;
  if exists (select 1 from jsonb_array_elements(p->'cards') x
    where jsonb_typeof(x->'quantity') is distinct from 'number'
       or (x->>'quantity')::numeric <> trunc((x->>'quantity')::numeric)
       or (x->>'quantity')::numeric <= 0
       or x->>'zone' not in ('main','commander','companion','sideboard','maybeboard')
       or x->>'zone' is null or x->>'oracle_id' is null) then
    raise exception 'Each card needs a UUID, known zone, and positive integer quantity';
  end if;
  select jsonb_agg(to_jsonb(r) order by r.zone, r.oracle_id) into normalized from (
    select oracle_id, zone, sum(quantity)::integer quantity
    from jsonb_to_recordset(p->'cards') as x(oracle_id uuid, zone text, quantity integer)
    group by oracle_id, zone
  ) r;
  if exists (select 1 from jsonb_to_recordset(normalized) as x(oracle_id uuid)
             left join public.cards c using (oracle_id) where c.oracle_id is null) then
    raise exception 'Unresolved Oracle IDs; load the card catalog first';
  end if;
  select array_agg(oracle_id order by oracle_id) into ids
  from jsonb_to_recordset(normalized) as x(oracle_id uuid, zone text, quantity integer)
  where zone = 'commander';
  if coalesce(cardinality(ids),0) not between 1 and 2
     or exists (select 1 from jsonb_to_recordset(normalized)
        as x(oracle_id uuid, zone text, quantity integer)
        where zone = 'commander' and quantity <> 1)
     or exists (select 1 from jsonb_to_recordset(normalized)
        as x(oracle_id uuid, zone text) where zone = 'main' and oracle_id = any(ids)) then
    raise exception 'Need one or two distinct commanders, one copy each, outside the main list';
  end if;
  select sum(quantity) into total from jsonb_to_recordset(normalized)
    as x(zone text, quantity integer) where zone in ('main','commander');
  if total <> 100 then raise exception 'Expected 100 main plus commander cards; got %', total; end if;
  group_id := array_to_string(ids, '+');
  select string_agg(name, ' + ' order by oracle_id) into group_name
  from public.cards where oracle_id = any(ids);
  select encode(sha256(convert_to(string_agg(
    zone || ':' || oracle_id::text || ':' || quantity::text, E'\n' order by zone, oracle_id),
    'UTF8')), 'hex') into content_hash
    from jsonb_to_recordset(normalized) as x(oracle_id uuid, zone text, quantity integer);
  select jsonb_agg(jsonb_build_object(
    'section',case when x.zone = 'commander' then 'Commander' else 'Deck' end,
    'qty',x.quantity,'name',c.name) order by x.zone,x.oracle_id) into projection
    from jsonb_to_recordset(normalized) as x(oracle_id uuid,zone text,quantity integer)
    join public.cards c using (oracle_id) where x.zone in ('commander','main');

  -- Serialize imports of the same source deck, including the first insert.
  perform pg_advisory_xact_lock(hashtextextended('mtgtop8:deck:' || (p->>'source_deck_id'),0));
  insert into public.events as old (
    source_event_id,title,location,event_date,raw_date,player_count,platform,event_kind,
    coverage,source_url,organizer_source_url,metadata
  ) values (
    (e->>'source_event_id')::bigint,e->>'title',e->>'location',(e->>'event_date')::date,
    e->>'raw_date',(e->>'player_count')::integer,coalesce(e->>'platform','unknown'),
    coalesce(e->>'event_kind','unknown'),coalesce(e->>'coverage','unknown'),
    'https://mtgtop8.com/event?e=' || (e->>'source_event_id') || '&f=EDH',
    e->>'organizer_source_url',coalesce(e->'metadata','{}')
  ) on conflict (source,source_event_id,format_id) do update set
    title=excluded.title,location=excluded.location,event_date=excluded.event_date,
    raw_date=excluded.raw_date,player_count=excluded.player_count,platform=excluded.platform,
    event_kind=excluded.event_kind,coverage=excluded.coverage,
    organizer_source_url=excluded.organizer_source_url,metadata=excluded.metadata,
    updated_at=case when (old.title,old.location,old.event_date,old.raw_date,old.player_count,
      old.platform,old.event_kind,old.coverage,old.organizer_source_url,old.metadata)
      is distinct from (excluded.title,excluded.location,excluded.event_date,excluded.raw_date,
      excluded.player_count,excluded.platform,excluded.event_kind,excluded.coverage,
      excluded.organizer_source_url,excluded.metadata) then now() else old.updated_at end
  returning id into ev;
  insert into public.commander_groups (group_key,commander_oracle_ids,display_name)
  values (group_id,ids,group_name) on conflict (group_key) do nothing;
  insert into public.commander_group_members (group_key,oracle_id)
  select group_id,unnest(ids) on conflict do nothing;
  select * into existing from public.mtg_decks
    where source='mtgtop8' and source_deck_id=(p->>'source_deck_id')::bigint for update;
  was_new := not found;
  if not was_new and existing.event_id <> ev then
    raise exception 'Source deck already belongs to another event';
  end if;
  if was_new then
    insert into public.mtg_decks (
      deck_id,deck_name,owner,commander,total_cards,source_deck_id,event_id,source_url
    ) values (
      'mtgtop8:duel_commander:' || ((p->>'source_deck_id')::bigint)::text,
      p->>'deck_name',p->>'player_name',group_name,total,(p->>'source_deck_id')::bigint,ev,
      'https://mtgtop8.com/event?e=' || (e->>'source_event_id') || '&d=' ||
      (p->>'source_deck_id') || '&f=EDH') returning id into did;
  else did := existing.id;
  end if;
  select id,revision_number into vid,next_revision from public.deck_versions
    where id=existing.current_version_id;
  changed := was_new or (select list_hash from public.deck_versions where id=vid)
                         is distinct from content_hash;
  if changed then
    select coalesce(max(revision_number),0)+1 into next_revision
      from public.deck_versions where deck_id=did;
    insert into public.deck_versions (
      deck_id,revision_number,list_hash,parser_version,commander_group_key,source_cards,
      raw_text,html_fetch_id,export_fetch_id,validation_findings
    ) values (
      did,next_revision,content_hash,p->>'parser_version',group_id,
      coalesce(p->'source_cards',p->'cards'),p->>'raw_text',
      (p->>'html_fetch_id')::uuid,(p->>'export_fetch_id')::uuid,
      coalesce(p->'validation_findings','[]')
    ) returning id into vid;
    insert into public.deck_cards (version_id,oracle_id,zone,quantity)
      select vid,x.oracle_id,x.zone,x.quantity from jsonb_to_recordset(normalized)
      as x(oracle_id uuid,zone text,quantity integer);
  end if;
  update public.mtg_decks set
    deck_name=p->>'deck_name',owner=p->>'player_name',commander=group_name,cards=projection,
    total_cards=total,current_version_id=vid,import_state='ready',state_reason=null,
    placement_text=p->>'placement_text',rank_min=(p->>'rank_min')::integer,
    rank_max=(p->>'rank_max')::integer,result_kind=coalesce(p->>'result_kind','unknown'),
    last_seen_at=now(),
    last_updated=case when changed or
      (deck_name,owner,cards,import_state,placement_text,rank_min,rank_max,result_kind)
      is distinct from (p->>'deck_name',p->>'player_name',projection,'ready',p->>'placement_text',
      (p->>'rank_min')::integer,(p->>'rank_max')::integer,coalesce(p->>'result_kind','unknown'))
      then now() else last_updated end
  where id=did;
  return jsonb_build_object('deck_id',did,'version_id',vid,'revision',next_revision,
    'action',case when was_new then 'inserted' when changed then 'revised' else 'unchanged' end);
end $$;

create function ingestion.set_deck_state(p_source_deck_id bigint,p_state text,p_reason text)
returns void language plpgsql security definer set search_path = '' as $$
begin
  if p_state not in ('quarantined','removed') or nullif(btrim(p_reason),'') is null then
    raise exception 'Use quarantined/removed with a reason; restore through a validated import';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('mtgtop8:deck:' || p_source_deck_id::text,0));
  update public.mtg_decks set import_state=p_state,state_reason=p_reason,last_updated=now()
    where source_deck_id=p_source_deck_id;
  if not found then raise exception 'Source deck not found'; end if;
end $$;
