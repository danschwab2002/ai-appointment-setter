-- Migration: la pausa de una conversacion deja de ser definitiva.
--
-- Que estaba mal. Cuando una persona del equipo escribe en una conversacion, el
-- bridge la pausa (etiqueta automation_paused en Chatwoot) y, si ademas hubo una
-- derivacion, request_inbound_human_handoff deja la conversacion en
-- status='paused_human', automation_status='paused' y human_takeover=true, y el
-- caso en status='paused', automation_status='disabled'.
--
-- Ninguna de esas dos transiciones tenia vuelta. Medido el 2026-09-23 sobre las
-- 75 migraciones del repo: human_takeover aparece en quince de ellas y en todas
-- se lee para bloquear; se escribe en true en un unico lugar
-- (20260823000100_inbound_durable_handoff.sql:639) y no existe ninguna funcion
-- que lo devuelva a false. Con ese campo en true, la guarda de admision
-- (20260826000200_inbound_paused_replay_guard.sql:110-121) devuelve 'blocked' y
-- el agente no vuelve a correr en esa conversacion nunca mas: el lead escribe al
-- vacio.
--
-- El costo medido ese mismo dia sobre el inbox 9 de Johanna: 27 de 109
-- conversaciones pausadas, y de las 10 donde el lead escribio ultimo y espera
-- respuesta, 9 estaban pausadas. Tres de ellas habian pedido el enlace de pago y
-- su emision quedo en 'reserved' sin salir (conversaciones 110, 124 y 138).
--
-- Que hace esta migracion. Agrega la transicion inversa, con la misma forma que
-- la de ida: una funcion security definer, idempotente por command_key, que
-- devuelve la conversacion a status='active', automation_status='draft_only' y
-- human_takeover=false, y el caso a status='active',
-- automation_status='draft_only', que es exactamente lo que la guarda de
-- admision exige para no bloquear. Cada reactivacion deja una fila de auditoria
-- en public.conversation_resume_events con el estado previo de las dos
-- entidades, el motivo y cuanto silencio hubo del equipo.
--
-- Lo que NO hace, a proposito:
--
--   1. No toca public.human_handoff_requests. Su columna status solo admite
--      requested/projected/projection_failed/dead_letter, y agregarle un estado
--      terminal cambiaria una tabla que leen el proyector de notas y el
--      conector de Slack. La reactivacion se audita en tabla propia.
--   2. No decide cuando reactivar. La politica (cuantas horas de silencio del
--      equipo hacen falta) vive en el bridge y es configurable; esta funcion
--      solo ejecuta la transicion y registra por que se la pidieron.
--   3. No reactiva a quien pidio no ser contactado: si el contacto tiene
--      contact_permission en opted_out, blocked o restricted, devuelve
--      'blocked_contact' sin tocar nada. Es la misma barrera que usan el motor
--      de seguimientos (20260803000100:1066) y la admision.

begin;

-- 1. El caso comercial inbound era inmutable salvo para pausarse.
--
-- protect_inbound_commercial_case (20260823000100:176-258) acepta exactamente
-- una transicion de UPDATE: active/draft_only -> paused/disabled. Cualquier otra
-- levanta 'inbound_commercial_case_is_immutable'. O sea que la pausa no era
-- terminal por olvido: estaba codificada como invariante en un trigger.
--
-- Se agrega la transicion inversa con la misma severidad: paused/disabled ->
-- active/draft_only, version + 1, updated_at estrictamente mayor y todos los
-- demas campos identicos. No se relaja ninguna de las condiciones existentes ni
-- se toca la rama de INSERT.

create or replace function public.protect_inbound_commercial_case()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    if tg_op = 'INSERT' then
        if new.case_kind <> 'inbound_sales'
           or new.recovery_case_id is not null
           or new.status <> 'active'
           or new.automation_status <> 'draft_only'
           or new.identity_resolution_status <> 'resolved'
           or new.authority_mode <> 'shadow'
           or new.version <> 1
           or new.selected_channel_identity_id is null
           or new.conversation_id is null
           or new.inbound_scope_key is null
           or new.inbound_scope_version is null
           or nullif(btrim(new.tenant_ref), '') is null
           or nullif(btrim(new.product_ref), '') is null
           or nullif(btrim(new.offer_ref), '') is null then
            raise exception using errcode = '23514',
                message = 'invalid_inbound_commercial_case_state';
        end if;
        if not exists (
            select 1
            from public.inbound_commercial_scope_versions scope
            join public.channel_identities identity
              on identity.channel = 'whatsapp'
             and identity.account_id = 'chatwoot:' || scope.chatwoot_account_id::text
             and identity.metadata ->> 'inbox_id' = scope.chatwoot_inbox_id::text
            join public.conversations conversation
              on conversation.channel_identity_id = identity.id
             and conversation.contact_id = identity.contact_id
            where scope.scope_key = new.inbound_scope_key
              and scope.version = new.inbound_scope_version
              and scope.status = 'published'
              and scope.tenant_key = new.tenant_ref
              and scope.external_product_id = new.product_ref
              and scope.offer_code = new.offer_ref
              and identity.id = new.selected_channel_identity_id
              and identity.contact_id = new.contact_id
              and identity.identity_status = 'active'
              and conversation.id = new.conversation_id
              and conversation.commercial_context = jsonb_build_object(
                  'chatwoot_conversation_id',
                  conversation.commercial_context ->> 'chatwoot_conversation_id'
              )
        ) then
            raise exception using errcode = '23514',
                message = 'inbound_commercial_case_canonical_mismatch';
        end if;
        return new;
    end if;

    if tg_op = 'UPDATE'
       and old.case_kind = 'inbound_sales'
       and old.status = 'active'
       and old.automation_status = 'draft_only'
       and new.status = 'paused'
       and new.automation_status = 'disabled'
       and new.version = old.version + 1
       and new.updated_at > old.updated_at
       and new.id = old.id
       and new.recovery_case_id is not distinct from old.recovery_case_id
       and new.case_kind = old.case_kind
       and new.contact_id = old.contact_id
       and new.selected_channel_identity_id = old.selected_channel_identity_id
       and new.conversation_id = old.conversation_id
       and new.product_ref is not distinct from old.product_ref
       and new.offer_ref is not distinct from old.offer_ref
       and new.identity_resolution_status is not distinct from old.identity_resolution_status
       and new.authority_mode = old.authority_mode
       and new.created_at = old.created_at
       and new.inbound_scope_key = old.inbound_scope_key
       and new.inbound_scope_version = old.inbound_scope_version
       and new.tenant_ref = old.tenant_ref then
        return new;
    end if;

    -- Reanudacion: la inversa exacta de la transicion de arriba.
    if tg_op = 'UPDATE'
       and old.case_kind = 'inbound_sales'
       and old.status = 'paused'
       and old.automation_status = 'disabled'
       and new.status = 'active'
       and new.automation_status = 'draft_only'
       and new.version = old.version + 1
       and new.updated_at > old.updated_at
       and new.id = old.id
       and new.recovery_case_id is not distinct from old.recovery_case_id
       and new.case_kind = old.case_kind
       and new.contact_id = old.contact_id
       and new.selected_channel_identity_id = old.selected_channel_identity_id
       and new.conversation_id = old.conversation_id
       and new.product_ref is not distinct from old.product_ref
       and new.offer_ref is not distinct from old.offer_ref
       and new.identity_resolution_status is not distinct from old.identity_resolution_status
       and new.authority_mode = old.authority_mode
       and new.created_at = old.created_at
       and new.inbound_scope_key = old.inbound_scope_key
       and new.inbound_scope_version = old.inbound_scope_version
       and new.tenant_ref = old.tenant_ref then
        return new;
    end if;

    raise exception using errcode = '55000',
        message = 'inbound_commercial_case_is_immutable';
end;
$function$;

-- 2. Auditoria de cada reactivacion.

create table public.conversation_resume_events (
    id uuid primary key default gen_random_uuid(),
    conversation_id uuid not null
        references public.conversations(id) on delete restrict,
    commercial_case_id uuid not null
        references public.commercial_cases(id) on delete restrict,
    external_conversation_id bigint not null
        check (external_conversation_id > 0),
    command_key text not null
        check (command_key ~ '^[a-z0-9:_-]{1,200}$'),
    reason_code text not null
        check (reason_code in (
            'inbound_after_quiet_period',
            'operator_request'
        )),
    quiet_seconds integer
        check (quiet_seconds is null or quiet_seconds >= 0),
    previous_conversation_status text not null,
    previous_conversation_automation_status text not null,
    previous_human_takeover boolean not null,
    previous_case_status text not null,
    previous_case_automation_status text not null,
    created_at timestamptz not null default clock_timestamp()
);

create unique index conversation_resume_events_command_key_idx
on public.conversation_resume_events (command_key);

create index conversation_resume_events_conversation_idx
on public.conversation_resume_events (conversation_id, created_at desc);

alter table public.conversation_resume_events enable row level security;

create or replace function public.resume_paused_conversation(
    p_external_conversation_id bigint,
    p_command_key text,
    p_reason_code text,
    p_quiet_seconds integer default null,
    p_max_resumes integer default 3,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    resumed_conversation_id uuid,
    resumed_commercial_case_id uuid,
    resume_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_conversation public.conversations%rowtype;
    v_case public.commercial_cases%rowtype;
    v_contact public.contacts%rowtype;
    v_existing public.conversation_resume_events%rowtype;
    v_case_count integer;
    v_resume_count integer;
    v_event_id uuid;
    v_now timestamptz;
begin
    if p_external_conversation_id is null
       or p_external_conversation_id <= 0
       or p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_reason_code not in (
           'inbound_after_quiet_period',
           'operator_request'
       )
       or (p_quiet_seconds is not null and p_quiet_seconds < 0)
       or p_max_resumes is null
       or p_max_resumes < 1 then
        raise exception using errcode = '22023',
            message = 'resume_paused_conversation_invalid_input';
    end if;

    -- Idempotencia: el mismo command_key no reactiva dos veces.
    select event.* into v_existing
    from public.conversation_resume_events event
    where event.command_key = p_command_key;
    if v_existing.id is not null then
        outcome := 'replayed';
        resumed_conversation_id := v_existing.conversation_id;
        resumed_commercial_case_id := v_existing.commercial_case_id;
        resume_event_id := v_existing.id;
        return next;
        return;
    end if;

    select conversation.* into v_conversation
    from public.conversations conversation
    where conversation.commercial_context = jsonb_build_object(
        'chatwoot_conversation_id', p_external_conversation_id::text
    )
    for update;
    if v_conversation.id is null then
        outcome := 'not_found';
        return next;
        return;
    end if;

    -- Falla cerrado ante ambiguedad: reactivar un caso arbitrario de dos seria
    -- un error silencioso.
    select count(*) into v_case_count
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id;
    if v_case_count > 1 then
        raise exception using errcode = 'P0001',
            message = 'resume_paused_conversation_ambiguous_case';
    end if;

    select commercial_case.* into v_case
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id
    for update;
    if v_case.id is null then
        outcome := 'not_found';
        resumed_conversation_id := v_conversation.id;
        return next;
        return;
    end if;

    select contact.* into v_contact
    from public.contacts contact
    where contact.id = v_conversation.contact_id;
    if v_contact.id is null
       or v_contact.contact_permission in (
           'opted_out', 'blocked', 'restricted'
       ) then
        outcome := 'blocked_contact';
        resumed_conversation_id := v_conversation.id;
        resumed_commercial_case_id := v_case.id;
        return next;
        return;
    end if;

    -- Anti-loop: reactivar una conversacion que el agente vuelve a derivar la
    -- devolveria a la cola del equipo una y otra vez. Pasado el limite, la
    -- conversacion se queda con las personas.
    select count(*) into v_resume_count
    from public.conversation_resume_events event
    where event.conversation_id = v_conversation.id;
    if v_resume_count >= p_max_resumes then
        outcome := 'blocked_resume_limit';
        resumed_conversation_id := v_conversation.id;
        resumed_commercial_case_id := v_case.id;
        return next;
        return;
    end if;

    -- Una derivacion a medio proyectar todavia es trabajo en curso del
    -- proyector de notas: reactivar ahi se pisa con el, y ademas dejaria viva
    -- una fila que hace fallar al proximo handoff con
    -- 'inbound_handoff_live_request_conflict'.
    if exists (
        select 1
        from public.human_handoff_requests request
        where request.commercial_case_id = v_case.id
          and request.status in ('requested', 'projection_failed')
    ) then
        outcome := 'blocked_pending_handoff';
        resumed_conversation_id := v_conversation.id;
        resumed_commercial_case_id := v_case.id;
        return next;
        return;
    end if;

    -- Ya admisible: no hay nada que levantar.
    if v_case.status = 'active'
       and v_case.automation_status = 'draft_only'
       and v_conversation.status in (
           'active', 'awaiting_agent', 'awaiting_contact', 'snoozed'
       )
       and v_conversation.automation_status = 'draft_only'
       and not v_conversation.human_takeover then
        outcome := 'already_active';
        resumed_conversation_id := v_conversation.id;
        resumed_commercial_case_id := v_case.id;
        return next;
        return;
    end if;

    v_now := coalesce(p_now, clock_timestamp());

    update public.conversations conversation
    set status = 'active',
        automation_status = 'draft_only',
        human_takeover = false,
        version = conversation.version + 1,
        updated_at = v_now
    where conversation.id = v_conversation.id;
    if not found then
        raise exception using errcode = '40001',
            message = 'resume_paused_conversation_transition_rejected';
    end if;

    update public.commercial_cases commercial_case
    set status = 'active',
        automation_status = 'draft_only',
        version = commercial_case.version + 1,
        updated_at = v_now
    where commercial_case.id = v_case.id;
    if not found then
        raise exception using errcode = '40001',
            message = 'resume_paused_conversation_case_transition_rejected';
    end if;

    insert into public.conversation_resume_events (
        conversation_id, commercial_case_id, external_conversation_id,
        command_key, reason_code, quiet_seconds,
        previous_conversation_status,
        previous_conversation_automation_status,
        previous_human_takeover,
        previous_case_status,
        previous_case_automation_status,
        created_at
    ) values (
        v_conversation.id, v_case.id, p_external_conversation_id,
        p_command_key, p_reason_code, p_quiet_seconds,
        v_conversation.status,
        v_conversation.automation_status,
        v_conversation.human_takeover,
        v_case.status,
        v_case.automation_status,
        v_now
    )
    returning id into v_event_id;

    outcome := 'resumed';
    resumed_conversation_id := v_conversation.id;
    resumed_commercial_case_id := v_case.id;
    resume_event_id := v_event_id;
    return next;
end;
$function$;

-- Postgres concede execute a PUBLIC en toda funcion nueva: sin este revoke, una
-- RPC security definer que levanta la pausa queda al alcance de anon.
revoke execute on function public.resume_paused_conversation(
    bigint, text, text, integer, integer, timestamptz
) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke execute on function public.resume_paused_conversation(
            bigint, text, text, integer, integer, timestamptz
        ) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke execute on function public.resume_paused_conversation(
            bigint, text, text, integer, integer, timestamptz
        ) from authenticated;
        revoke all on table public.conversation_resume_events from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.resume_paused_conversation(
            bigint, text, text, integer, integer, timestamptz
        ) to service_role;
    end if;
end
$roles$;

commit;
