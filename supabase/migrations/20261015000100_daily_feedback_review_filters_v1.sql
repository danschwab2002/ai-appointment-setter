-- Revision diaria: filtros por tipo de conversacion y cierre del lote con
-- aprobacion explicita.
--
-- Pedido de Dan del 2026-10-14, despues de la reunion con Mariana: el lote de
-- ATT1 del 2026-10-13 traia 28 conversaciones y en la mayoria no habia nada
-- que revisar (siete eran la plantilla y un opt-out). La revisora elige que
-- tipos de conversacion revisar y, cuando termina, cierra el lote con una
-- aprobacion escrita de que lo que queda se cierra sin feedback.
--
-- Que cambia:
--   1. daily_feedback_batch_closures: un renglon por lote cerrado sin revisar
--      todo, con quien lo cerro, la frase que aprobo y cuantas quedaron sin
--      revisar. Las conversaciones sin revisar no reciben una decision: no
--      aparecen como "ya revisada" en el lote siguiente ni en el feedback.
--   2. get_daily_feedback_review_page_v1: un lote cerrado devuelve `complete`,
--      con lo que dejo el cierre.
--   3. get_daily_feedback_review_page_v2(p_session_hash, p_public_ref,
--      p_facets): la primera conversacion pendiente con alguna de las etiquetas
--      pedidas (context.summary.review_facets, las calcula el colector) y los
--      conteos por etiqueta de lo pendiente. Sin etiquetas, igual que la v1.
--      Si quedan pendientes pero ninguna coincide, `filtered_out`.
--   4. record_daily_feedback_decision_v1 acepta cualquier conversacion
--      pendiente del lote, no solo la primera: con un filtro, la que se ve no es
--      la primera por indice. Sigue exigiendo una sesion valida, un lote abierto
--      y una conversacion sin decision.
--   5. close_daily_feedback_batch_v1: cierra el lote con la frase exacta, si
--      queda al menos una conversacion pendiente. El lote pasa a `completed`.
--
-- No cambia la forma de los items ni de las decisiones. Un servicio anterior
-- sigue andando contra esta base: usa la pagina v1 y la decision v1.
--
-- Contrato: docs/contracts/daily-feedback-review-package-v2.md, seccion 11.

begin;

-- 1. El cierre del lote ------------------------------------------------------

create table public.daily_feedback_batch_closures (
  batch_id uuid primary key references public.daily_feedback_batches(id) on delete restrict,
  reviewer_binding_id uuid not null references public.daily_feedback_reviewer_bindings(id) on delete restrict,
  command_id uuid not null unique,
  acknowledgement text not null check (char_length(acknowledgement) between 1 and 300),
  decided_count integer not null check (decided_count >= 0),
  unreviewed_count integer not null check (unreviewed_count >= 1),
  closed_at timestamptz not null default clock_timestamp()
);

create trigger daily_feedback_batch_closures_immutable
before update or delete on public.daily_feedback_batch_closures
for each row execute function public.daily_feedback_immutable_content_guard();

alter table public.daily_feedback_batch_closures enable row level security;
revoke all on table public.daily_feedback_batch_closures from public;
do $roles$
declare v_role text;
begin
  for v_role in select rolname from pg_roles where rolname in ('anon','authenticated','service_role') loop
    execute format('revoke all on table public.daily_feedback_batch_closures from %I', v_role);
  end loop;
end;
$roles$;

-- 2. get_daily_feedback_review_page_v1: un lote cerrado esta completo ---------
-- Definicion de 20260927000100 con el chequeo del cierre antes de buscar item.

create or replace function public.get_daily_feedback_review_page_v1(p_session_hash text,p_public_ref uuid)
returns jsonb
language plpgsql security definer set search_path=''
as $$
declare
  v_session public.daily_feedback_sessions%rowtype;
  v_batch public.daily_feedback_batches%rowtype;
  v_binding public.daily_feedback_reviewer_bindings%rowtype;
  v_item public.daily_feedback_items%rowtype;
  v_closure public.daily_feedback_batch_closures%rowtype;
  v_done integer;
begin
  select * into v_session from public.daily_feedback_sessions where session_hash=p_session_hash for update;
  if not found or v_session.revoked_at is not null or v_session.expires_at<=clock_timestamp() then
    raise exception 'review_session_invalid';
  end if;
  select * into v_batch from public.daily_feedback_batches where public_ref=p_public_ref for update;
  if not found or v_session.batch_id<>v_batch.id or v_batch.state='purged'
     or v_batch.retention_expires_at<=clock_timestamp() then
    raise exception 'review_batch_unavailable';
  end if;
  if 4 <> (select count(*) from public.daily_feedback_batch_reviewer_bindings where batch_id=v_batch.id)
     or 4 <> (select count(*) from public.daily_feedback_batch_reviewer_bindings where batch_id=v_batch.id and deletion_accountable) then
    raise exception 'reviewer_set_incomplete';
  end if;
  select rb.* into v_binding
  from public.daily_feedback_reviewer_bindings rb
  join public.daily_feedback_batch_reviewer_bindings brb
    on brb.batch_id=v_batch.id and brb.reviewer_binding_id=rb.id
   and brb.reviewer_binding_generation=v_session.binding_generation
  where rb.id=v_session.reviewer_binding_id and rb.active
    and rb.binding_generation=v_session.binding_generation
    and rb.tenant_ref=v_batch.tenant_ref and rb.scope_ref=v_batch.scope_ref;
  if not found then raise exception 'reviewer_not_authorized'; end if;
  update public.daily_feedback_sessions set last_seen_at=clock_timestamp() where session_hash=p_session_hash;
  select count(*) into v_done from public.daily_feedback_decisions where batch_id=v_batch.id;
  select * into v_closure from public.daily_feedback_batch_closures where batch_id=v_batch.id;
  if found then
    return jsonb_build_object('status','complete','local_date',v_batch.local_date,
      'item_count',v_batch.item_count,'decided_count',v_done,
      'retention_expires_at',v_batch.retention_expires_at,
      'closed_unreviewed_count',v_closure.unreviewed_count,'closed_at',v_closure.closed_at);
  end if;
  select i.* into v_item from public.daily_feedback_items i
  where i.batch_id=v_batch.id
    and not exists(select 1 from public.daily_feedback_decisions d where d.item_id=i.id)
  order by i.item_index limit 1;
  if not found then
    return jsonb_build_object('status','complete','local_date',v_batch.local_date,
      'item_count',v_batch.item_count,'decided_count',v_done,
      'retention_expires_at',v_batch.retention_expires_at);
  end if;
  return jsonb_build_object('status','item','local_date',v_batch.local_date,
    'item_count',v_batch.item_count,'decided_count',v_done,
    'retention_expires_at',v_batch.retention_expires_at,'item',jsonb_build_object(
      'item_id',v_item.id,'position',v_item.item_index,'display_label',v_item.display_label,
      'apparent_objective',v_item.apparent_objective,'observed_outcome',v_item.observed_outcome,
      'release_id',v_item.release_id,'release_version',v_item.release_version,
      'messages',v_item.messages,'context',v_item.context));
end;
$$;

-- 3. get_daily_feedback_review_page_v2: la cola filtrada ----------------------

create function public.get_daily_feedback_review_page_v2(
  p_session_hash text,p_public_ref uuid,p_facets text[]
) returns jsonb
language plpgsql security definer set search_path=''
as $$
declare
  v_page jsonb;
  v_batch_id uuid;
  v_item public.daily_feedback_items%rowtype;
  v_faceted boolean;
  v_pending integer;
  v_matching integer;
  v_counts jsonb;
  v_queue jsonb;
  v_filtering boolean;
begin
  if p_facets is not null and (
       cardinality(p_facets) > 8
       or exists(select 1 from unnest(p_facets) f where f is null or f !~ '^[a-z][a-z0-9_]{0,31}$')
     ) then
    raise exception 'invalid_review_facets';
  end if;
  -- La v1 autentica, bloquea el lote y la sesion, y resuelve el cierre.
  v_page:=public.get_daily_feedback_review_page_v1(p_session_hash,p_public_ref);
  if v_page->>'status'<>'item' then
    return v_page;
  end if;
  select b.id into v_batch_id from public.daily_feedback_batches b where b.public_ref=p_public_ref;
  v_faceted:=exists(
    select 1 from public.daily_feedback_items i
    where i.batch_id=v_batch_id
      and jsonb_typeof(i.context->'summary'->'review_facets')='array'
  );
  v_filtering:=v_faceted and p_facets is not null and cardinality(p_facets)>0;
  select count(*),
         count(*) filter (where not v_filtering or (
           jsonb_typeof(i.context->'summary'->'review_facets')='array'
           and (i.context->'summary'->'review_facets') ?| p_facets))
    into v_pending, v_matching
  from public.daily_feedback_items i
  where i.batch_id=v_batch_id
    and not exists(select 1 from public.daily_feedback_decisions d where d.item_id=i.id);
  select coalesce(jsonb_object_agg(f.facet,f.n),'{}'::jsonb) into v_counts
  from (
    select facet, count(*) as n
    from public.daily_feedback_items i
    cross join lateral jsonb_array_elements_text(
      case when jsonb_typeof(i.context->'summary'->'review_facets')='array'
           then i.context->'summary'->'review_facets' else '[]'::jsonb end
    ) as facet
    where i.batch_id=v_batch_id
      and not exists(select 1 from public.daily_feedback_decisions d where d.item_id=i.id)
    group by facet
  ) f;
  v_queue:=jsonb_build_object('pending',v_pending,'matching',v_matching,
    'faceted',v_faceted,'filtering',v_filtering,'facets',v_counts);
  if not v_filtering then
    return v_page||jsonb_build_object('queue',v_queue);
  end if;
  select i.* into v_item from public.daily_feedback_items i
  where i.batch_id=v_batch_id
    and not exists(select 1 from public.daily_feedback_decisions d where d.item_id=i.id)
    and jsonb_typeof(i.context->'summary'->'review_facets')='array'
    and (i.context->'summary'->'review_facets') ?| p_facets
  order by i.item_index limit 1;
  if not found then
    return (v_page-'item')||jsonb_build_object('status','filtered_out','queue',v_queue);
  end if;
  return v_page||jsonb_build_object('queue',v_queue,'item',jsonb_build_object(
    'item_id',v_item.id,'position',v_item.item_index,'display_label',v_item.display_label,
    'apparent_objective',v_item.apparent_objective,'observed_outcome',v_item.observed_outcome,
    'release_id',v_item.release_id,'release_version',v_item.release_version,
    'messages',v_item.messages,'context',v_item.context));
end;
$$;

-- 4. record_daily_feedback_decision_v1: cualquier conversacion pendiente ------
-- Definicion de 20260910000100; cambia solo el chequeo del item actual.

create or replace function public.record_daily_feedback_decision_v1(
  p_command_id uuid,p_semantic_fingerprint text,p_session_hash text,p_public_ref uuid,p_item_id uuid,
  p_decision text,p_verbatim_feedback text
) returns jsonb
language plpgsql security definer set search_path=''
as $$
declare v_prior jsonb; v_page jsonb; v_batch public.daily_feedback_batches%rowtype; v_session public.daily_feedback_sessions%rowtype; v_item public.daily_feedback_items%rowtype; v_done integer; v_result jsonb;
begin
  v_prior:=public.daily_feedback_begin_command(
    p_command_id,'record_decision',p_semantic_fingerprint,
    public.daily_feedback_server_payload_hash(jsonb_build_object(
      'session_hash',p_session_hash,'public_ref',p_public_ref,'item_id',p_item_id,
      'decision',p_decision,'verbatim_feedback',p_verbatim_feedback
    ))
  );
  if v_prior is not null then return v_prior||jsonb_build_object('status','replayed'); end if;
  v_page:=public.get_daily_feedback_review_page_v1(p_session_hash,p_public_ref);
  if v_page->>'status'<>'item' then raise exception 'review_item_not_current'; end if;
  if p_decision not in ('correct','correct_with_feedback','skip')
    or (p_decision='correct_with_feedback' and (p_verbatim_feedback is null or char_length(btrim(p_verbatim_feedback)) not between 1 and 4000))
    or (p_decision in ('correct','skip') and p_verbatim_feedback is not null)
    or (p_verbatim_feedback is not null and pg_catalog.regexp_replace(p_verbatim_feedback, E'[\\n\\t]', '', 'g') ~ '[[:cntrl:]]') then raise exception 'invalid_review_decision'; end if;
  select * into v_batch from public.daily_feedback_batches where public_ref=p_public_ref for update;
  select * into v_session from public.daily_feedback_sessions where session_hash=p_session_hash;
  select * into v_item from public.daily_feedback_items where id=p_item_id and batch_id=v_batch.id for update;
  if not found or exists(select 1 from public.daily_feedback_decisions d where d.item_id=v_item.id) then
    raise exception 'review_item_not_current';
  end if;
  insert into public.daily_feedback_decisions(batch_id,item_id,reviewer_binding_id,command_id,decision,verbatim_feedback)
  values(v_batch.id,v_item.id,v_session.reviewer_binding_id,p_command_id,p_decision,p_verbatim_feedback);
  select count(*) into v_done from public.daily_feedback_decisions where batch_id=v_batch.id;
  if v_done=v_batch.item_count then update public.daily_feedback_batches set state='completed',completed_at=clock_timestamp(),updated_at=clock_timestamp() where id=v_batch.id; end if;
  v_result:=jsonb_build_object('status','recorded','decided_count',v_done,'item_count',v_batch.item_count,'batch_complete',v_done=v_batch.item_count);
  return public.daily_feedback_finish_command(p_command_id,v_result);
end;
$$;

-- 5. close_daily_feedback_batch_v1: cerrar con la aprobacion escrita ----------

create function public.close_daily_feedback_batch_v1(
  p_command_id uuid,p_semantic_fingerprint text,p_session_hash text,p_public_ref uuid,
  p_acknowledgement text
) returns jsonb
language plpgsql security definer set search_path=''
as $$
declare
  v_prior jsonb;
  v_page jsonb;
  v_batch public.daily_feedback_batches%rowtype;
  v_session public.daily_feedback_sessions%rowtype;
  v_done integer;
  v_unreviewed integer;
  v_result jsonb;
begin
  v_prior:=public.daily_feedback_begin_command(
    p_command_id,'close_batch',p_semantic_fingerprint,
    public.daily_feedback_server_payload_hash(jsonb_build_object(
      'session_hash',p_session_hash,'public_ref',p_public_ref,
      'acknowledgement',p_acknowledgement
    ))
  );
  if v_prior is not null then return v_prior||jsonb_build_object('status','replayed'); end if;
  if p_acknowledgement is distinct from
     'Apruebo que este reporte se cierre sin completar el feedback de todas las conversaciones.' then
    raise exception 'review_closure_not_acknowledged';
  end if;
  v_page:=public.get_daily_feedback_review_page_v1(p_session_hash,p_public_ref);
  if v_page->>'status'<>'item' then raise exception 'review_batch_not_open'; end if;
  select * into v_batch from public.daily_feedback_batches where public_ref=p_public_ref for update;
  select * into v_session from public.daily_feedback_sessions where session_hash=p_session_hash;
  select count(*) into v_done from public.daily_feedback_decisions where batch_id=v_batch.id;
  v_unreviewed:=v_batch.item_count-v_done;
  insert into public.daily_feedback_batch_closures(
    batch_id,reviewer_binding_id,command_id,acknowledgement,decided_count,unreviewed_count)
  values(v_batch.id,v_session.reviewer_binding_id,p_command_id,p_acknowledgement,v_done,v_unreviewed);
  update public.daily_feedback_batches
     set state='completed',completed_at=clock_timestamp(),updated_at=clock_timestamp()
   where id=v_batch.id;
  v_result:=jsonb_build_object('status','closed','decided_count',v_done,
    'unreviewed_count',v_unreviewed,'item_count',v_batch.item_count);
  return public.daily_feedback_finish_command(p_command_id,v_result);
end;
$$;

-- 6. Permisos: solo service_role ejecuta las dos funciones nuevas -------------

revoke all on function public.get_daily_feedback_review_page_v2(text,uuid,text[]) from public;
revoke all on function public.close_daily_feedback_batch_v1(uuid,text,text,uuid,text) from public;
do $roles$
begin
  if to_regrole('anon') is not null then
    revoke all on function public.get_daily_feedback_review_page_v2(text,uuid,text[]) from anon;
    revoke all on function public.close_daily_feedback_batch_v1(uuid,text,text,uuid,text) from anon;
  end if;
  if to_regrole('authenticated') is not null then
    revoke all on function public.get_daily_feedback_review_page_v2(text,uuid,text[]) from authenticated;
    revoke all on function public.close_daily_feedback_batch_v1(uuid,text,text,uuid,text) from authenticated;
  end if;
  if to_regrole('service_role') is not null then
    grant execute on function public.get_daily_feedback_review_page_v2(text,uuid,text[]) to service_role;
    grant execute on function public.close_daily_feedback_batch_v1(uuid,text,text,uuid,text) to service_role;
  end if;
end;
$roles$;

commit;
