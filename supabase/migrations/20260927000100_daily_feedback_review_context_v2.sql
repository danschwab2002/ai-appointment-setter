-- Revision diaria con contexto identificado (daily-feedback-review-package-v2).
--
-- Decision: docs/decisions/0018-daily-feedback-identified-review-context.md
-- (Dan, 2026-09-26). La revision diaria dejo de anonimizar: los cuatro revisores
-- autenticados por Slack ven el nombre y el telefono del lead, el link a la
-- conversacion en Chatwoot, los mensajes del equipo, las notas de derivacion,
-- las plantillas (reactivacion, primer toque, seguimiento), el link de pago y
-- los eventos internos (pausa, reanudacion, opt-out, compra).
--
-- Que cambia:
--   1. daily_feedback_items.messages acepta la forma v2 (actor prospect/agent/team/
--      system, kind, status, meta) ademas de la forma v1; el minimo baja a 1
--      mensaje para que una conversacion sin respuesta del agente tambien entre.
--   2. daily_feedback_items.context (jsonb) guarda identidad del lead, link,
--      estado de la conversacion, eventos, links de pago y revisiones previas.
--   3. commit_daily_feedback_batch_v1 acepta items con `context` (y sigue
--      aceptando items v1 para que el orden de deploy no importe);
--      get_daily_feedback_review_page_v1 lo devuelve.
--   4. get_daily_feedback_conversation_context_v1 lee, por conversacion de
--      Chatwoot, las derivaciones, reactivaciones, reanudaciones, opt-outs,
--      links de pago y decisiones de revision anteriores.
--
-- Contrato: docs/contracts/daily-feedback-review-package-v2.md

begin;

-- 1. Mensajes: forma v1 (minimizada) o forma v2 (identificada) ------------------

create or replace function public.daily_feedback_messages_valid(p_messages jsonb)
returns boolean
language sql
immutable
set search_path = ''
as $$
  select jsonb_typeof(p_messages) = 'array'
     and jsonb_array_length(p_messages) between 1 and 1000
     and not exists (
       select 1
       from jsonb_array_elements(p_messages) m
       where jsonb_typeof(m) <> 'object'
          or coalesce(char_length(m->>'text'), 0) not between 1 and 4000
          or (m->>'occurred_at') !~ '^20[0-9]{2}-[0-9]{2}-[0-9]{2}T.*Z$'
          or not (
            (
              -- forma v1: texto minimizado, sin URLs, mails, telefonos ni control
              (select array_agg(k order by k) from jsonb_object_keys(m) k)
                = array['actor','occurred_at','text']::text[]
              and m->>'actor' in ('prospect','agent')
              and (m->>'text') !~* 'https?://|www\.|bearer[[:space:]]+[a-z0-9._~+/=-]{12,}|[[:alnum:]._%+-]+@[[:alnum:].-]+\.[[:alpha:]]{2,}'
              and (m->>'text') !~ '[[:digit:]][[:space:]().+-]*[[:digit:]][[:space:]().+-]*[[:digit:]][[:space:]().+-]*[[:digit:]][[:space:]().+-]*[[:digit:]][[:space:]().+-]*[[:digit:]][[:space:]().+-]*[[:digit:]][[:space:]().+-]*[[:digit:]]'
              and (m->>'text') !~ '[[:cntrl:]]'
            )
            or
            (
              -- forma v2: texto tal cual salio (salvo secretos y control), con
              -- actor, tipo, estado de entrega y metadatos acotados
              (select array_agg(k order by k) from jsonb_object_keys(m) k)
                = array['actor','kind','meta','occurred_at','status','text']::text[]
              and m->>'actor' in ('prospect','agent','team','system')
              and (m->>'kind') ~ '^[a-z][a-z0-9_]{0,63}$'
              and (m->>'status') ~ '^[a-z][a-z0-9_]{0,31}$'
              and jsonb_typeof(m->'meta') = 'object'
              and char_length((m->'meta')::text) <= 4000
              and (m->>'text') !~* 'bearer[[:space:]]+[a-z0-9._~+/=-]{12,}'
              and regexp_replace(m->>'text', E'[\\n\\r\\t]', '', 'g') !~ '[[:cntrl:]]'
            )
          )
     );
$$;

revoke all on function public.daily_feedback_messages_valid(jsonb) from public, anon, authenticated, service_role;

alter table public.daily_feedback_items
  drop constraint if exists daily_feedback_items_messages_check;
alter table public.daily_feedback_items
  add constraint daily_feedback_items_messages_length_check
  check (jsonb_typeof(messages) = 'array' and jsonb_array_length(messages) between 1 and 1000);

-- 2. Contexto del item --------------------------------------------------------------

create function public.daily_feedback_item_context_valid(p_context jsonb)
returns boolean
language sql
immutable
set search_path = ''
as $$
  select jsonb_typeof(p_context) = 'object'
     and octet_length(p_context::text) <= 32768
     and not exists (
       select 1 from jsonb_object_keys(p_context) k
       where k not in (
         'chatwoot_conversation_id','conversation_url','contact','conversation',
         'origin','events','payment_links','prior_reviews','summary'
       )
     )
     and (
       not (p_context ? 'chatwoot_conversation_id')
       or (jsonb_typeof(p_context->'chatwoot_conversation_id') = 'number'
           and (p_context->>'chatwoot_conversation_id') ~ '^[1-9][0-9]{0,17}$')
     )
     and (
       not (p_context ? 'conversation_url')
       or (jsonb_typeof(p_context->'conversation_url') = 'string'
           and char_length(p_context->>'conversation_url') between 9 and 400
           and (p_context->>'conversation_url') ~ '^https://[^[:space:]"''<>\\]+$')
     )
     and (not (p_context ? 'contact') or jsonb_typeof(p_context->'contact') = 'object')
     and (not (p_context ? 'conversation') or jsonb_typeof(p_context->'conversation') = 'object')
     and (not (p_context ? 'origin') or jsonb_typeof(p_context->'origin') = 'string')
     and (not (p_context ? 'events') or jsonb_typeof(p_context->'events') = 'array')
     and (not (p_context ? 'payment_links') or jsonb_typeof(p_context->'payment_links') = 'array')
     and (not (p_context ? 'prior_reviews') or jsonb_typeof(p_context->'prior_reviews') = 'array')
     and (not (p_context ? 'summary') or jsonb_typeof(p_context->'summary') = 'object')
     and p_context::text !~* 'bearer[[:space:]]+[a-z0-9._~+/=-]{12,}'
     and p_context::text !~* '(javascript|vbscript):'
$$;

revoke all on function public.daily_feedback_item_context_valid(jsonb) from public, anon, authenticated, service_role;

alter table public.daily_feedback_items
  add column if not exists context jsonb not null default '{}'::jsonb;
alter table public.daily_feedback_items
  add constraint daily_feedback_items_context_valid
  check (public.daily_feedback_item_context_valid(context));

-- 3. commit_daily_feedback_batch_v1: acepta items v1 e items v2 (con context) -------

create or replace function public.commit_daily_feedback_batch_v1(
  p_command_id uuid,
  p_semantic_fingerprint text,
  p_worker_id text,
  p_schedule_id uuid,
  p_lease_generation bigint,
  p_package_fingerprint text,
  p_release_lineage text,
  p_items jsonb
) returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_prior jsonb;
  v_schedule public.daily_feedback_schedules%rowtype;
  v_batch public.daily_feedback_batches%rowtype;
  v_item jsonb;
  v_item_keys text[];
  v_context jsonb;
  v_expected integer := 1;
  v_reviewer_count integer;
  v_accountable_count integer;
  v_canonical_reviewers jsonb;
  v_current_reviewer_set_hash text;
  v_result jsonb;
begin
  v_prior := public.daily_feedback_begin_command(
    p_command_id,'commit_batch',p_semantic_fingerprint,
    public.daily_feedback_server_payload_hash(jsonb_build_object(
      'worker_id',p_worker_id,'schedule_id',p_schedule_id,'lease_generation',p_lease_generation,
      'package_fingerprint',p_package_fingerprint,'release_lineage',p_release_lineage,'items',p_items
    ))
  );
  if v_prior is not null then return v_prior || jsonb_build_object('status','replayed'); end if;
  if p_package_fingerprint !~ '^[a-f0-9]{64}$' or jsonb_typeof(p_items) <> 'array'
     or jsonb_array_length(p_items) > 500 or char_length(p_release_lineage) not between 1 and 128 then
    raise exception 'invalid_daily_feedback_package';
  end if;
  select * into v_schedule from public.daily_feedback_schedules where id=p_schedule_id for update;
  if not found or v_schedule.lease_owner is distinct from p_worker_id
     or v_schedule.lease_generation <> p_lease_generation
     or v_schedule.lease_expires_at <= clock_timestamp()
     or v_schedule.pending_local_date is null then
    raise exception 'stale_collection_lease';
  end if;

  select count(*),count(*) filter(where rb.deletion_accountable),
         jsonb_agg(jsonb_build_object(
           'reviewer_ref',rb.reviewer_ref,
           'slack_user_id',rb.slack_user_id,
           'deletion_accountable',rb.deletion_accountable
         ) order by rb.reviewer_ref)
  into v_reviewer_count,v_accountable_count,v_canonical_reviewers
  from public.daily_feedback_reviewer_bindings rb
  where rb.tenant_ref=v_schedule.tenant_ref and rb.scope_ref=v_schedule.scope_ref and rb.active;
  v_current_reviewer_set_hash:=pg_catalog.encode(
    pg_catalog.sha256(pg_catalog.convert_to(v_canonical_reviewers::text,'UTF8')),'hex'
  );
  if v_reviewer_count<>4 or v_accountable_count<>4
     or v_current_reviewer_set_hash<>v_schedule.reviewer_set_hash then
    raise exception 'reviewer_set_changed';
  end if;

  insert into public.daily_feedback_batches(
    schedule_id,tenant_ref,scope_ref,local_date,window_start,window_end,
    sanitizer_version,selection_version,renderer_version,release_lineage,
    package_fingerprint,item_count,reviewer_binding_id,reviewer_binding_generation,
    retention_expires_at,reviewer_set_hash,deletion_policy_ref
  ) values (
    v_schedule.id,v_schedule.tenant_ref,v_schedule.scope_ref,v_schedule.pending_local_date,
    v_schedule.pending_window_start,v_schedule.pending_window_end,
    v_schedule.sanitizer_version,v_schedule.selection_version,v_schedule.renderer_version,
    p_release_lineage,p_package_fingerprint,jsonb_array_length(p_items),
    v_schedule.reviewer_binding_id,v_schedule.reviewer_binding_generation,
    clock_timestamp()+make_interval(hours=>v_schedule.retention_hours),
    v_schedule.reviewer_set_hash,v_schedule.deletion_policy_ref
  )
  on conflict (tenant_ref,scope_ref,local_date) do nothing
  returning * into v_batch;
  if not found then
    select * into v_batch from public.daily_feedback_batches
    where tenant_ref=v_schedule.tenant_ref and scope_ref=v_schedule.scope_ref
      and local_date=v_schedule.pending_local_date for update;
    if v_batch.package_fingerprint<>p_package_fingerprint
       or v_batch.reviewer_set_hash<>v_schedule.reviewer_set_hash then
      raise exception 'logical_batch_conflict';
    end if;
  else
    insert into public.daily_feedback_batch_reviewer_bindings(
      batch_id,reviewer_binding_id,reviewer_binding_generation,reviewer_ref,
      oidc_issuer,oidc_subject,slack_team_id,slack_user_id,deletion_accountable
    )
    select v_batch.id,rb.id,rb.binding_generation,rb.reviewer_ref,
           rb.oidc_issuer,rb.oidc_subject,rb.slack_team_id,rb.slack_user_id,
           rb.deletion_accountable
    from public.daily_feedback_reviewer_bindings rb
    where rb.tenant_ref=v_schedule.tenant_ref and rb.scope_ref=v_schedule.scope_ref and rb.active
    order by rb.reviewer_ref;
    for v_item in select value from jsonb_array_elements(p_items) loop
      if jsonb_typeof(v_item)<>'object' then
        raise exception 'invalid_daily_feedback_item';
      end if;
      select array_agg(k order by k) into v_item_keys from jsonb_object_keys(v_item) k;
      if v_item_keys is null
         or (
           v_item_keys <> array['apparent_objective','conversation_ref','display_label','messages','observed_outcome','release_id','release_version']::text[]
           and v_item_keys <> array['apparent_objective','context','conversation_ref','display_label','messages','observed_outcome','release_id','release_version']::text[]
         )
         or (v_item->>'conversation_ref') !~ '^conv_[a-f0-9]{20}$'
         or not public.daily_feedback_messages_valid(v_item->'messages') then
        raise exception 'invalid_daily_feedback_item';
      end if;
      v_context := coalesce(v_item->'context','{}'::jsonb);
      if not public.daily_feedback_item_context_valid(v_context) then
        raise exception 'invalid_daily_feedback_item_context';
      end if;
      insert into public.daily_feedback_items(
        batch_id,item_index,conversation_ref,display_label,apparent_objective,
        observed_outcome,release_id,release_version,messages,context
      ) values (
        v_batch.id,v_expected,v_item->>'conversation_ref',v_item->>'display_label',
        v_item->>'apparent_objective',v_item->>'observed_outcome',v_item->>'release_id',
        (v_item->>'release_version')::integer,v_item->'messages',v_context
      );
      v_expected:=v_expected+1;
    end loop;
  end if;
  update public.daily_feedback_schedules
  set last_completed_local_date=pending_local_date,last_completed_window_end=pending_window_end,
      lease_owner=null,lease_expires_at=null,pending_local_date=null,
      pending_window_start=null,pending_window_end=null,next_attempt_at=clock_timestamp(),
      updated_at=clock_timestamp()
  where id=v_schedule.id;
  v_result:=jsonb_build_object(
    'status','committed','batch_id',v_batch.id,'public_ref',v_batch.public_ref,
    'item_count',v_batch.item_count,'reviewer_count',v_reviewer_count,
    'retention_expires_at',v_batch.retention_expires_at
  );
  return public.daily_feedback_finish_command(p_command_id,v_result);
end;
$$;

-- 4. get_daily_feedback_review_page_v1: devuelve el contexto del item ----------------

create or replace function public.get_daily_feedback_review_page_v1(p_session_hash text,p_public_ref uuid)
returns jsonb
language plpgsql security definer set search_path=''
as $$
declare
  v_session public.daily_feedback_sessions%rowtype;
  v_batch public.daily_feedback_batches%rowtype;
  v_binding public.daily_feedback_reviewer_bindings%rowtype;
  v_item public.daily_feedback_items%rowtype;
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

-- 5. Contexto durable por conversacion de Chatwoot ----------------------------------

create function public.get_daily_feedback_conversation_context_v1(
  p_tenant_ref text,
  p_scope_ref text,
  p_chatwoot_account_id bigint,
  p_chatwoot_inbox_id bigint,
  p_conversation_ids bigint[]
) returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_ids bigint[];
  v_id bigint;
  v_result jsonb := '{}'::jsonb;
  v_handoffs jsonb;
  v_reactivations jsonb;
  v_resumes jsonb;
  v_opt_outs jsonb;
  v_links jsonb;
  v_prior jsonb;
begin
  if p_tenant_ref is null or p_tenant_ref !~ '^[a-z0-9][a-z0-9_-]{0,63}$'
     or p_scope_ref is null or p_scope_ref !~ '^[a-z0-9][a-z0-9_-]{0,127}$'
     or p_chatwoot_account_id is null or p_chatwoot_account_id < 1
     or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1
     or p_conversation_ids is null or cardinality(p_conversation_ids) > 500 then
    raise exception 'invalid_daily_feedback_context_request';
  end if;
  if not exists (
    select 1 from public.daily_feedback_schedules s
    where s.tenant_ref = p_tenant_ref and s.scope_ref = p_scope_ref
      and s.chatwoot_account_id = p_chatwoot_account_id
      and s.chatwoot_inbox_id = p_chatwoot_inbox_id
  ) then
    raise exception 'daily_feedback_scope_not_configured';
  end if;
  select array_agg(distinct id order by id) into v_ids
  from unnest(p_conversation_ids) as id where id is not null and id > 0;
  if v_ids is null then return v_result; end if;

  foreach v_id in array v_ids loop
    select coalesce(jsonb_agg(jsonb_build_object(
             'occurred_at', h.created_at,
             'primary_reason_code', h.primary_reason_code,
             'detail_reason_code', h.detail_reason_code,
             'requested_by', h.requested_by,
             'status', h.status
           ) order by h.created_at), '[]'::jsonb)
    into v_handoffs
    from (
      select * from public.human_handoff_requests h0
      where h0.chatwoot_account_id = p_chatwoot_account_id
        and h0.chatwoot_inbox_id = p_chatwoot_inbox_id
        and h0.external_conversation_id = v_id
      order by h0.created_at desc limit 50
    ) h;

    select coalesce(jsonb_agg(jsonb_build_object(
             'occurred_at', r.created_at,
             'status', r.status,
             'template_name', r.template_name,
             'reason_code', r.reason_code,
             'provider_message_id', r.provider_message_id,
             'quiet_seconds', r.quiet_seconds,
             'failure_reason', r.failure_reason
           ) order by r.created_at), '[]'::jsonb)
    into v_reactivations
    from (
      select * from public.conversation_reactivation_events r0
      where r0.external_conversation_id = v_id
      order by r0.created_at desc limit 50
    ) r;

    select coalesce(jsonb_agg(jsonb_build_object(
             'occurred_at', e.created_at,
             'reason_code', e.reason_code,
             'quiet_seconds', e.quiet_seconds
           ) order by e.created_at), '[]'::jsonb)
    into v_resumes
    from (
      select * from public.conversation_resume_events e0
      where e0.external_conversation_id = v_id
      order by e0.created_at desc limit 50
    ) e;

    select coalesce(jsonb_agg(jsonb_build_object(
             'occurred_at', o.occurred_at,
             'chatwoot_message_id', o.canonical_message_id
           ) order by o.occurred_at), '[]'::jsonb)
    into v_opt_outs
    from (
      select * from public.contact_opt_out_events o0
      where o0.canonical_account_id = p_chatwoot_account_id
        and o0.canonical_inbox_id = p_chatwoot_inbox_id
        and o0.canonical_conversation_id = v_id
      order by o0.occurred_at desc limit 20
    ) o;

    select coalesce(jsonb_agg(jsonb_build_object(
             'occurred_at', l.created_at,
             'status', l.status,
             'source_kind', l.source_kind,
             'chatwoot_message_id', l.chatwoot_message_id,
             'sck_value', l.sck_value,
             'original_sck', l.original_sck,
             'checkout_url_final', l.checkout_url_final,
             'purchased_at', l.purchased_at,
             'offer_code', l.offer_code,
             'landing_ref', l.landing_ref
           ) order by l.created_at), '[]'::jsonb)
    into v_links
    from (
      select l0.created_at, l0.status, l0.source_kind, l0.chatwoot_message_id,
             l0.sck_value, l0.original_sck, l0.checkout_url_final, l0.purchased_at,
             c.offer_code, c.landing_ref
      from public.checkout_link_issuances l0
      left join public.checkout_offer_catalog c on c.id = l0.offer_catalog_id
      where l0.chatwoot_account_id = p_chatwoot_account_id
        and l0.chatwoot_inbox_id = p_chatwoot_inbox_id
        and l0.chatwoot_conversation_id = v_id
      order by l0.created_at desc limit 20
    ) l;

    select coalesce(jsonb_agg(jsonb_build_object(
             'local_date', p.local_date,
             'decision', p.decision,
             'verbatim_feedback', p.verbatim_feedback,
             'decided_at', p.decided_at
           ) order by p.local_date desc), '[]'::jsonb)
    into v_prior
    from (
      select b.local_date, d.decision, d.verbatim_feedback, d.created_at as decided_at
      from public.daily_feedback_items i
      join public.daily_feedback_batches b on b.id = i.batch_id
      join public.daily_feedback_decisions d on d.item_id = i.id
      where b.tenant_ref = p_tenant_ref and b.scope_ref = p_scope_ref
        and b.state <> 'purged'
        and (i.context->>'chatwoot_conversation_id') = v_id::text
      order by b.local_date desc limit 10
    ) p;

    v_result := v_result || jsonb_build_object(v_id::text, jsonb_build_object(
      'handoffs', v_handoffs,
      'reactivations', v_reactivations,
      'resumes', v_resumes,
      'opt_outs', v_opt_outs,
      'payment_links', v_links,
      'prior_reviews', v_prior
    ));
  end loop;
  return v_result;
end;
$$;

revoke all on function public.get_daily_feedback_conversation_context_v1(text,text,bigint,bigint,bigint[]) from public, anon, authenticated;
grant execute on function public.get_daily_feedback_conversation_context_v1(text,text,bigint,bigint,bigint[]) to service_role;

commit;
