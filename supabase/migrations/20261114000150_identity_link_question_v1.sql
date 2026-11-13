-- identity-link-question-v1: la pregunta del correlador que decide solo (E-536,
-- bloque B, D5 de Dan del 13/11). Contrato: docs/contracts/identity-link-decider-v1.md §6.
--
-- Por que. Cuando el modelo no esta seguro (same_person con confianza media, o
-- unsure), la rama es «pregunta»: una plantilla unica al telefono del permiso,
-- con dos respuestas rapidas, «Sí, fui yo» y «No más mensajes». Con la «Sí» la
-- base ata (decided_by = 'persona') y la conversacion sigue con el agente, que
-- tiene la ventana de 24 h abierta. «No más mensajes» es el boton de baja de
-- siempre: la entrada de Chatwoot lo registra como cualquier otra baja; aca
-- solo queda anotado para auditar. Sin respuesta, nada mas: a las 72 h la
-- respuesta queda sin_respuesta.
--
-- La pregunta no pasa por el despachador de seguimientos: es un solo mensaje,
-- sin seguimientos, y lo manda el worker del correlador con el sender de
-- Chatwoot de siempre. La puerta antes de enviar es esta base
-- (start_identity_link_question_v1): los frenos de la evidencia en vivo
-- (permiso, baja, compra, carrito de mas de 24 h, contacto ambiguo). Un envio
-- de resultado incierto no se reintenta nunca (estado unknown).
--
-- Lo que trae: identity_link_questions (la cola de la pregunta, mutable: su
-- lease), identity_link_answers (append-only), expire / claim / start / finish
-- de la pregunta, record_identity_link_answer_v1, y read_identity_link_notices_v1
-- con los envios de la pregunta (reemplaza la de 20261114000100 entera: es de
-- esta pieza y nadie mas la redefine).
--
-- Lo que no hace: no toca recovery_cases, followup_sequences ni
-- scheduled_actions; no crea la plantilla en Meta. Correrla dos veces aborta con
-- 55000 sin cambiar nada.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.identity_link_decisions') is null then
        raise exception using errcode = '55000', message = 'identity_link_decider_missing';
    end if;
    if to_regclass('public.identity_link_questions') is not null then
        raise exception using errcode = '55000', message = 'identity_link_question_already_applied';
    end if;
end;
$already$;

create table public.identity_link_questions (
    decision_id uuid primary key references public.identity_link_decisions(decision_id) on delete restrict,
    webhook_event_id uuid not null references public.identity_link_cases(webhook_event_id) on delete restrict,
    tenant_ref text not null,
    funnel_ref text not null,
    purchase_intent_id uuid not null references public.purchase_intents(id) on delete restrict,
    state text not null default 'planned'
        check (state in ('planned', 'request_started', 'sent', 'failed', 'unknown', 'cancelled')),
    claim_token uuid,
    claimed_by text,
    lease_generation bigint not null default 0 check (lease_generation >= 0),
    lease_expires_at timestamptz,
    request_started_at timestamptz,
    finished_at timestamptz,
    chatwoot_conversation_id bigint check (chatwoot_conversation_id is null or chatwoot_conversation_id > 0),
    chatwoot_message_id bigint check (chatwoot_message_id is null or chatwoot_message_id > 0),
    failure_code text check (failure_code is null or failure_code ~ '^[a-z0-9][a-z0-9_:-]{0,79}$'),
    created_at timestamptz not null default clock_timestamp(),
    check ((state in ('sent', 'failed', 'unknown', 'cancelled')) = (finished_at is not null)),
    check (state <> 'sent' or chatwoot_conversation_id is not null)
);
create index identity_link_questions_planned_idx
    on public.identity_link_questions (tenant_ref, funnel_ref, created_at) where state = 'planned';
create index identity_link_questions_conversation_idx
    on public.identity_link_questions (chatwoot_conversation_id) where state = 'sent';

create table public.identity_link_answers (
    decision_id uuid primary key references public.identity_link_questions(decision_id) on delete restrict,
    answer text not null check (answer in ('si', 'baja', 'sin_respuesta')),
    chatwoot_message_id bigint check (chatwoot_message_id is null or chatwoot_message_id > 0),
    person_decision_id uuid references public.identity_link_decisions(decision_id) on delete restrict,
    created_at timestamptz not null default clock_timestamp(),
    check (person_decision_id is null or answer = 'si')
);

create trigger identity_link_answers_append_only before update or delete on public.identity_link_answers
    for each row execute function public.reject_identity_link_mutation();

do $tables_acl$
declare
    v_table text;
    v_role text;
begin
    foreach v_table in array array['identity_link_questions', 'identity_link_answers'] loop
        execute format('alter table public.%I enable row level security', v_table);
        execute format('revoke all on table public.%I from public', v_table);
        for v_role in select r.rolname from pg_roles r
                       where r.rolname in ('anon', 'authenticated', 'service_role') order by r.rolname loop
            execute format('revoke all on table public.%I from %I', v_table, v_role);
        end loop;
    end loop;
end;
$tables_acl$;

-- El telefono de la pregunta: el de la decision (el del permiso, o con el +1 el
-- del checkout). Nunca otro.
create function public._identity_link_question_phone(p_decision_id uuid)
returns text
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case decision.destination_source
        when 'intent_phone' then intent.normalized_phone
        when 'intent_national_digits' then '52' || right(regexp_replace(intent.normalized_phone, '[^0-9]', '', 'g'), 10)
    end
      from public.identity_link_decisions decision
      join public.purchase_intents intent on intent.id = decision.purchase_intent_id
     where decision.decision_id = p_decision_id
$function$;

-- Vence lo viejo: la pregunta planificada con un freno vivo (o pasadas 24 h del
-- carrito) se cancela; la que quedo en request_started con el lease vencido
-- queda unknown (no se reintenta); la enviada sin respuesta a las 72 h queda
-- sin_respuesta.
create function public.expire_identity_link_questions_v1(p_tenant_ref text, p_funnel_ref text)
returns integer
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_row record;
    v_count integer := 0;
begin
    for v_row in
        select question.decision_id, question.webhook_event_id
          from public.identity_link_questions question
         where question.tenant_ref = p_tenant_ref and question.funnel_ref = p_funnel_ref
           and question.state = 'planned'
           and (question.lease_expires_at is null or question.lease_expires_at < clock_timestamp())
         for update skip locked
    loop
        if (public.get_identity_link_evidence_v1(v_row.webhook_event_id) ->> 'guard_reason') is not null then
            update public.identity_link_questions question
               set state = 'cancelled', finished_at = clock_timestamp(),
                   failure_code = 'identity_link_' || (public.get_identity_link_evidence_v1(v_row.webhook_event_id) ->> 'guard_reason')
             where question.decision_id = v_row.decision_id;
            v_count := v_count + 1;
        end if;
    end loop;
    update public.identity_link_questions question
       set state = 'unknown', finished_at = clock_timestamp(), failure_code = 'identity_link_request_unconfirmed'
     where question.tenant_ref = p_tenant_ref and question.funnel_ref = p_funnel_ref
       and question.state = 'request_started' and question.lease_expires_at < clock_timestamp();
    insert into public.identity_link_answers (decision_id, answer)
    select question.decision_id, 'sin_respuesta'
      from public.identity_link_questions question
     where question.tenant_ref = p_tenant_ref and question.funnel_ref = p_funnel_ref
       and question.state = 'sent' and question.finished_at < clock_timestamp() - interval '72 hours'
       and not exists (select 1 from public.identity_link_answers answer where answer.decision_id = question.decision_id)
    on conflict do nothing;
    return v_count;
end;
$function$;

create function public.claim_identity_link_question_v1(
    p_tenant_ref text, p_funnel_ref text, p_worker_id text, p_lease_seconds integer default 120)
returns table (decision_id uuid, claim_token uuid, lease_generation bigint, phone text,
               full_name text, product_name text)
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_question public.identity_link_questions%rowtype;
    v_token uuid := gen_random_uuid();
begin
    if nullif(btrim(p_worker_id), '') is null or length(p_worker_id) > 200
       or p_lease_seconds is null or p_lease_seconds not between 10 and 900 then
        raise exception using errcode = '22023', message = 'identity_link_input_invalid';
    end if;
    if not exists (select 1 from public.identity_link_controls control
                    where control.tenant_ref = p_tenant_ref and control.funnel_ref = p_funnel_ref
                      and control.disabled_at is null) then
        return;
    end if;
    select question.* into v_question
      from public.identity_link_questions question
     where question.tenant_ref = p_tenant_ref and question.funnel_ref = p_funnel_ref
       and question.state = 'planned'
       and (question.lease_expires_at is null or question.lease_expires_at < clock_timestamp())
     order by question.created_at
     limit 1
     for update skip locked;
    if not found then
        return;
    end if;
    update public.identity_link_questions question
       set claim_token = v_token, claimed_by = p_worker_id,
           lease_generation = question.lease_generation + 1,
           lease_expires_at = clock_timestamp() + make_interval(secs => p_lease_seconds)
     where question.decision_id = v_question.decision_id
     returning question.* into v_question;
    return query
    select v_question.decision_id, v_question.claim_token, v_question.lease_generation,
           public._identity_link_question_phone(v_question.decision_id),
           (select submission.canonical_payload #>> '{lead,full_name}'
              from public.purchase_intent_submissions link
              join public.precheckout_submissions submission on submission.id = link.submission_id
             where link.purchase_intent_id = v_question.purchase_intent_id
             order by link.ordinal desc limit 1),
           (select nullif(btrim(event.payload #>> '{data,product,name}'), '')
              from public.webhook_events event where event.id = v_question.webhook_event_id);
end;
$function$;

-- La puerta antes de enviar: los frenos en vivo. 'ok' pasa la pregunta a
-- request_started (irreversible: desde aca nunca se reintenta).
create function public.start_identity_link_question_v1(
    p_decision_id uuid, p_claim_token uuid, p_lease_generation bigint)
returns text
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_question public.identity_link_questions%rowtype;
    v_guard text;
begin
    select question.* into v_question from public.identity_link_questions question
     where question.decision_id = p_decision_id for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'identity_link_question_not_found';
    end if;
    if v_question.state <> 'planned' or v_question.claim_token is distinct from p_claim_token
       or v_question.lease_generation <> p_lease_generation then
        return 'identity_link_lease_lost';
    end if;
    v_guard := public.get_identity_link_evidence_v1(v_question.webhook_event_id) ->> 'guard_reason';
    if v_guard is not null then
        update public.identity_link_questions question
           set state = 'cancelled', finished_at = clock_timestamp(), failure_code = 'identity_link_' || v_guard
         where question.decision_id = p_decision_id;
        return 'identity_link_' || v_guard;
    end if;
    update public.identity_link_questions question
       set state = 'request_started', request_started_at = clock_timestamp()
     where question.decision_id = p_decision_id;
    return 'ok';
end;
$function$;

create function public.finish_identity_link_question_v1(
    p_decision_id uuid, p_claim_token uuid, p_lease_generation bigint, p_outcome text,
    p_conversation_id bigint, p_message_id bigint, p_failure_code text)
returns boolean
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_outcome not in ('sent', 'failed', 'unknown')
       or (p_outcome = 'sent' and (p_conversation_id is null or p_conversation_id < 1))
       or (p_failure_code is not null and p_failure_code !~ '^[a-z0-9][a-z0-9_:-]{0,79}$') then
        raise exception using errcode = '22023', message = 'identity_link_input_invalid';
    end if;
    update public.identity_link_questions question
       set state = p_outcome, finished_at = clock_timestamp(),
           chatwoot_conversation_id = p_conversation_id, chatwoot_message_id = p_message_id,
           failure_code = case when p_outcome = 'sent' then null else coalesce(p_failure_code, 'send_failed') end,
           claim_token = null, lease_expires_at = null
     where question.decision_id = p_decision_id
       and question.claim_token = p_claim_token
       and question.lease_generation = p_lease_generation
       and question.state in ('planned', 'request_started');
    return found;
end;
$function$;

-- La respuesta a la pregunta, por el numero de conversacion de Chatwoot. «si»
-- ata con decided_by = 'persona' si no hay un freno vivo; la conversacion sigue
-- con el agente (el carrito no se vuelve a encolar: su primer toque saldria
-- encima de la conversacion). «baja» solo se anota: la baja la registra la
-- entrada de Chatwoot por el camino de siempre.
create function public.record_identity_link_answer_v1(
    p_tenant_ref text, p_funnel_ref text, p_conversation_id bigint, p_answer text, p_message_id bigint)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_question public.identity_link_questions%rowtype;
    v_decision public.identity_link_decisions%rowtype;
    v_case public.identity_link_cases%rowtype;
    v_person_decision uuid;
    v_guard text;
begin
    if p_answer not in ('si', 'baja') or p_conversation_id is null or p_conversation_id < 1 then
        raise exception using errcode = '22023', message = 'identity_link_input_invalid';
    end if;
    select question.* into v_question
      from public.identity_link_questions question
     where question.tenant_ref = p_tenant_ref and question.funnel_ref = p_funnel_ref
       and question.chatwoot_conversation_id = p_conversation_id
       and question.state = 'sent'
       and question.finished_at >= clock_timestamp() - interval '72 hours'
       and not exists (select 1 from public.identity_link_answers answer where answer.decision_id = question.decision_id)
     order by question.finished_at desc
     limit 1
     for update;
    if not found then
        return jsonb_build_object('recorded', false);
    end if;
    if p_answer = 'si' then
        v_guard := public.get_identity_link_evidence_v1(v_question.webhook_event_id) ->> 'guard_reason';
        if v_guard is null then
            select decision.* into v_decision from public.identity_link_decisions decision
             where decision.decision_id = v_question.decision_id;
            select ilc.* into v_case from public.identity_link_cases ilc
             where ilc.webhook_event_id = v_question.webhook_event_id;
            insert into public.identity_link_decisions (
                webhook_event_id, tenant_ref, funnel_ref, purchase_intent_id, mode, decided_by,
                evidence_snapshot, evidence_sha256, reason_codes, branch, destination_source, destination_sha256)
            values (
                v_question.webhook_event_id, v_question.tenant_ref, v_question.funnel_ref,
                v_question.purchase_intent_id, 'vivo', 'persona', v_case.evidence_snapshot, v_case.evidence_sha256,
                array['customer_confirmation']::text[], 'ata', v_decision.destination_source,
                v_decision.destination_sha256)
            on conflict do nothing
            returning decision_id into v_person_decision;
            if v_person_decision is not null then
                insert into public.identity_link_effects (decision_id, effect) values (v_person_decision, 'ninguno');
            end if;
        end if;
    end if;
    insert into public.identity_link_answers (decision_id, answer, chatwoot_message_id, person_decision_id)
    values (v_question.decision_id, p_answer, p_message_id, v_person_decision);
    return jsonb_build_object('recorded', true, 'answer', p_answer,
        'person_decision_id', v_person_decision, 'guard_reason', v_guard);
end;
$function$;

-- Los avisos, con los envios de la pregunta (reemplaza la de 20261114000100).
create or replace function public.read_identity_link_notices_v1(p_tenant_ref text, p_funnel_ref text, p_limit integer default 20)
returns table (notice_key text, kind text, branch text, decided_by text, outcome text,
               conversation_id bigint, occurred_at timestamptz)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    with pending as (
        select 'decision:' || decision.decision_id::text as notice_key, 'decision'::text as kind,
               decision.branch, decision.decided_by, null::text as outcome,
               public._identity_link_conversation_number(intent.normalized_phone, binding.chatwoot_account_id)
                   as conversation_id,
               decision.created_at as occurred_at
          from public.identity_link_decisions decision
          join public.purchase_intents intent on intent.id = decision.purchase_intent_id
          left join public.commercial_ally_runtime_bindings binding
            on binding.tenant_ref = decision.tenant_ref and binding.funnel_ref = decision.funnel_ref
           and binding.status = 'active'
         where decision.tenant_ref = p_tenant_ref and decision.funnel_ref = p_funnel_ref
           and decision.mode = 'vivo' and decision.branch in ('ata', 'pregunta')
        union all
        select 'envio:' || attempt.id::text || ':' || case when attempt.outcome = 'accepted_by_chatwoot'
                   then 'sale' else 'falla' end,
               'envio', decision.branch, decision.decided_by,
               case when attempt.outcome = 'accepted_by_chatwoot' then 'sale' else 'falla' end,
               case when identity.external_conversation_id ~ '^[1-9][0-9]{0,11}$'
                    then identity.external_conversation_id::bigint end,
               coalesce(attempt.accepted_at, attempt.updated_at)
          from public.identity_link_decisions decision
          join public.recovery_cases recovery on recovery.abandonment_event_id = decision.webhook_event_id
          join public.scheduled_actions action on action.recovery_case_id = recovery.id
          join public.followup_delivery_attempts attempt on attempt.action_id = action.id
          left join public.channel_identities identity on identity.id = recovery.selected_channel_identity_id
         where decision.tenant_ref = p_tenant_ref and decision.funnel_ref = p_funnel_ref
           and decision.mode = 'vivo' and decision.branch = 'ata' and decision.decided_by <> 'persona'
           and attempt.phase = 'completed'
        union all
        select 'envio:' || question.decision_id::text || ':pregunta_' || case when question.state = 'sent'
                   then 'sale' else 'falla' end,
               'envio', 'pregunta', decision.decided_by,
               case when question.state = 'sent' then 'sale' else 'falla' end,
               question.chatwoot_conversation_id, question.finished_at
          from public.identity_link_questions question
          join public.identity_link_decisions decision on decision.decision_id = question.decision_id
         where question.tenant_ref = p_tenant_ref and question.funnel_ref = p_funnel_ref
           and question.state in ('sent', 'failed', 'unknown')
    )
    select pending.*
      from pending
     where not exists (select 1 from public.identity_link_notices notice where notice.notice_key = pending.notice_key)
     order by pending.occurred_at
     limit greatest(1, least(coalesce(p_limit, 20), 100))
$function$;

do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public._identity_link_question_phone(uuid)',
        'public.expire_identity_link_questions_v1(text,text)',
        'public.claim_identity_link_question_v1(text,text,text,integer)',
        'public.start_identity_link_question_v1(uuid,uuid,bigint)',
        'public.finish_identity_link_question_v1(uuid,uuid,bigint,text,bigint,bigint,text)',
        'public.record_identity_link_answer_v1(text,text,bigint,text,bigint)',
        'public.read_identity_link_notices_v1(text,text,integer)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in select r.rolname from pg_roles r
                       where r.rolname in ('anon', 'authenticated', 'service_role') order by r.rolname loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.expire_identity_link_questions_v1(text,text) to service_role;
        grant execute on function public.claim_identity_link_question_v1(text,text,text,integer) to service_role;
        grant execute on function public.start_identity_link_question_v1(uuid,uuid,bigint) to service_role;
        grant execute on function public.finish_identity_link_question_v1(uuid,uuid,bigint,text,bigint,bigint,text) to service_role;
        grant execute on function public.record_identity_link_answer_v1(text,text,bigint,text,bigint) to service_role;
        grant execute on function public.read_identity_link_notices_v1(text,text,integer) to service_role;
    end if;
end;
$acl$;

commit;
