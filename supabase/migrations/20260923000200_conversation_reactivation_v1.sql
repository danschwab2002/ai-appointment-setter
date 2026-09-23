-- Migration: reactivar la conversacion que se cayo fuera de la ventana de 24 h.
--
-- Que problema resuelve. El 2026-09-23 se desplego el sistema de reanudacion
-- (20260923000100_resume_paused_conversation_v1.sql): una conversacion pausada
-- vuelve a ser admisible cuando el equipo lleva horas en silencio. Pero ese
-- disparador vive dentro del worker del webhook de Chatwoot: corre unicamente
-- cuando entra un mensaje del lead. Si el lead ya escribio y esta esperando, no
-- va a escribir de nuevo, y nada lo destraba.
--
-- Medido ese mismo dia a las 19:36 UTC sobre el inbox 9 (WABA whatsapp_cloud,
-- canal 8), las 6 conversaciones abiertas cuyo ultimo mensaje publico era del
-- lead:
--
--   conv 124  can_reply=true   9,6 h    automation_paused
--   conv 110  can_reply=true  17,0 h    automation_paused
--   conv 143  can_reply=false 24,5 h    automation_paused
--   conv 136  can_reply=false 25,9 h    automation_paused
--   conv  63  can_reply=false 27,2 h    automation_paused
--   conv 126  can_reply=false 28,2 h    automation_paused
--
-- can_reply de Chatwoot es exactamente la ventana de servicio de 24 h de Meta:
-- las cuatro que la cruzaron ya no admiten texto libre, solo una plantilla
-- aprobada. Y la tabla conversation_resume_events tenia 0 filas: el sistema
-- desplegado no se habia disparado una sola vez, porque no hay quien produzca
-- el mensaje entrante que lo dispara.
--
-- Que hace esta migracion. Registra y limita el envio de esa plantilla de
-- reactivacion. El envio en si lo hace el bridge contra Chatwoot; aca vive lo
-- que no puede vivir en la memoria de un proceso que se redespliega: la
-- idempotencia (no mandarle dos veces la misma plantilla al mismo lead) y el
-- limite por conversacion.
--
-- El ciclo completo queda: plantilla -> el lead responde -> el webhook entra ->
-- resume_paused_conversation levanta la pausa -> el agente retoma.
--
-- Lo que NO hace, a proposito:
--
--   1. No decide a quien reactivar. El criterio (fuera de ventana, silencio del
--      equipo, sin opt-out, con etiqueta de pausa) lo evalua el bridge contra
--      Chatwoot, que es la fuente de esos hechos. Aca solo se reserva el envio.
--   2. No reactiva a quien pidio no ser contactado: contact_permission en
--      opted_out, blocked o restricted devuelve 'blocked_contact'. Misma
--      barrera que el motor de seguimientos y que resume_paused_conversation.
--   3. No levanta la pausa. Mandar una plantilla no despausa nada: la pausa la
--      levanta resume_paused_conversation cuando el lead contesta, con su propia
--      auditoria. Una conversacion reactivada que nadie contesta se queda
--      exactamente como estaba.
--   4. No toca conversaciones que Chatwoot conoce y Supabase no: sin caso
--      comercial devuelve 'not_found' y no se manda nada. Medido el 23/09: la
--      conversacion 107 estaba en ese estado.

begin;

-- 1. Auditoria y control de cada intento de reactivacion.
--
-- El ciclo de vida de una fila es claimed -> sent | failed. Se reserva ANTES de
-- llamar a Chatwoot, no despues: si el proceso muere entre la reserva y el
-- envio, la fila queda en 'claimed' y bloquea el reintento. Es deliberado, y es
-- el lado seguro del error: mandarle dos veces una plantilla de marketing al
-- mismo lead es peor que no mandarsela. 'failed' es el unico estado que libera,
-- porque ahi Chatwoot dijo explicitamente que no salio.

create table public.conversation_reactivation_events (
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
            'outside_service_window',
            'operator_request'
        )),
    status text not null default 'claimed'
        check (status in ('claimed', 'sent', 'failed')),
    template_name text not null
        check (length(btrim(template_name)) between 1 and 512),
    template_language text not null
        check (length(btrim(template_language)) between 2 and 32),
    last_inbound_message_id bigint not null
        check (last_inbound_message_id > 0),
    inbound_age_seconds integer not null
        check (inbound_age_seconds >= 0),
    quiet_seconds integer
        check (quiet_seconds is null or quiet_seconds >= 0),
    previous_conversation_status text not null,
    previous_conversation_automation_status text not null,
    previous_human_takeover boolean not null,
    provider_message_id bigint
        check (provider_message_id is null or provider_message_id > 0),
    failure_reason text
        check (failure_reason is null
               or length(btrim(failure_reason)) between 1 and 200),
    created_at timestamptz not null default clock_timestamp(),
    settled_at timestamptz,
    check (
        (status = 'claimed' and settled_at is null
         and provider_message_id is null and failure_reason is null)
        or (status = 'sent' and settled_at is not null
            and failure_reason is null)
        or (status = 'failed' and settled_at is not null
            and provider_message_id is null)
    )
);

-- Idempotencia: el mismo command_key no manda dos veces. El indice es parcial
-- porque un envio fallido tiene que poder reintentarse en el proximo barrido;
-- uno reservado o entregado, no.
create unique index conversation_reactivation_events_command_key_idx
on public.conversation_reactivation_events (command_key)
where status <> 'failed';

create index conversation_reactivation_events_conversation_idx
on public.conversation_reactivation_events (conversation_id, created_at desc);

alter table public.conversation_reactivation_events enable row level security;

-- 2. Reservar un envio de reactivacion.

create or replace function public.claim_conversation_reactivation(
    p_external_conversation_id bigint,
    p_command_key text,
    p_reason_code text,
    p_template_name text,
    p_template_language text,
    p_last_inbound_message_id bigint,
    p_inbound_age_seconds integer,
    p_quiet_seconds integer default null,
    p_max_reactivations integer default 1,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    reactivation_event_id uuid,
    reactivated_conversation_id uuid,
    reactivated_commercial_case_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_conversation public.conversations%rowtype;
    v_case public.commercial_cases%rowtype;
    v_contact public.contacts%rowtype;
    v_existing public.conversation_reactivation_events%rowtype;
    v_case_count integer;
    v_live_count integer;
    v_event_id uuid;
    v_now timestamptz;
begin
    if p_external_conversation_id is null
       or p_external_conversation_id <= 0
       or p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_reason_code not in ('outside_service_window', 'operator_request')
       or p_template_name is null
       or length(btrim(p_template_name)) not between 1 and 512
       or p_template_language is null
       or length(btrim(p_template_language)) not between 2 and 32
       or p_last_inbound_message_id is null
       or p_last_inbound_message_id <= 0
       or p_inbound_age_seconds is null
       or p_inbound_age_seconds < 0
       or (p_quiet_seconds is not null and p_quiet_seconds < 0)
       or p_max_reactivations is null
       or p_max_reactivations < 1 then
        raise exception using errcode = '22023',
            message = 'claim_conversation_reactivation_invalid_input';
    end if;

    -- Idempotencia. Una reserva viva con el mismo command_key significa que el
    -- envio ya se pidio: no se vuelve a pedir ni se toca su fila.
    select event.* into v_existing
    from public.conversation_reactivation_events event
    where event.command_key = p_command_key
      and event.status <> 'failed';
    if v_existing.id is not null then
        outcome := 'replayed';
        reactivation_event_id := v_existing.id;
        reactivated_conversation_id := v_existing.conversation_id;
        reactivated_commercial_case_id := v_existing.commercial_case_id;
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

    -- Falla cerrado ante ambiguedad, igual que resume_paused_conversation: un
    -- select ... into de plpgsql con dos filas toma una arbitraria en silencio.
    select count(*) into v_case_count
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id;
    if v_case_count > 1 then
        raise exception using errcode = 'P0001',
            message = 'claim_conversation_reactivation_ambiguous_case';
    end if;

    select commercial_case.* into v_case
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id
    for update;
    if v_case.id is null then
        outcome := 'not_found';
        reactivated_conversation_id := v_conversation.id;
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
        reactivated_conversation_id := v_conversation.id;
        reactivated_commercial_case_id := v_case.id;
        return next;
        return;
    end if;

    -- Limite por conversacion. Cuenta las reservas vivas y las entregadas: a
    -- quien ya recibio la plantilla y no contesto no se le insiste.
    select count(*) into v_live_count
    from public.conversation_reactivation_events event
    where event.conversation_id = v_conversation.id
      and event.status in ('claimed', 'sent');
    if v_live_count >= p_max_reactivations then
        outcome := 'blocked_reactivation_limit';
        reactivation_event_id := null;
        reactivated_conversation_id := v_conversation.id;
        reactivated_commercial_case_id := v_case.id;
        return next;
        return;
    end if;

    v_now := coalesce(p_now, clock_timestamp());

    insert into public.conversation_reactivation_events (
        conversation_id, commercial_case_id, external_conversation_id,
        command_key, reason_code, status, template_name, template_language,
        last_inbound_message_id, inbound_age_seconds, quiet_seconds,
        previous_conversation_status,
        previous_conversation_automation_status,
        previous_human_takeover,
        created_at
    ) values (
        v_conversation.id, v_case.id, p_external_conversation_id,
        p_command_key, p_reason_code, 'claimed',
        btrim(p_template_name), btrim(p_template_language),
        p_last_inbound_message_id, p_inbound_age_seconds, p_quiet_seconds,
        v_conversation.status,
        v_conversation.automation_status,
        v_conversation.human_takeover,
        v_now
    )
    returning id into v_event_id;

    outcome := 'claimed';
    reactivation_event_id := v_event_id;
    reactivated_conversation_id := v_conversation.id;
    reactivated_commercial_case_id := v_case.id;
    return next;
end;
$function$;

-- 3. Cerrar la reserva con lo que Chatwoot respondio.

create or replace function public.settle_conversation_reactivation(
    p_command_key text,
    p_status text,
    p_provider_message_id bigint default null,
    p_failure_reason text default null,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    reactivation_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_existing public.conversation_reactivation_events%rowtype;
    v_now timestamptz;
begin
    if p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_status not in ('sent', 'failed')
       or (p_status = 'failed' and p_provider_message_id is not null)
       or (p_provider_message_id is not null and p_provider_message_id <= 0)
       or (p_failure_reason is not null
           and length(btrim(p_failure_reason)) not between 1 and 200) then
        raise exception using errcode = '22023',
            message = 'settle_conversation_reactivation_invalid_input';
    end if;

    select event.* into v_existing
    from public.conversation_reactivation_events event
    where event.command_key = p_command_key
      and event.status = 'claimed'
    for update;
    if v_existing.id is null then
        -- O nunca se reservo, o ya se cerro. Las dos cosas son 'nada que hacer'
        -- y ninguna justifica reescribir una fila terminal.
        outcome := 'not_found';
        return next;
        return;
    end if;

    v_now := coalesce(p_now, clock_timestamp());

    update public.conversation_reactivation_events event
    set status = p_status,
        provider_message_id = case
            when p_status = 'sent' then p_provider_message_id else null end,
        failure_reason = case
            when p_status = 'failed' then btrim(p_failure_reason) else null end,
        settled_at = v_now
    where event.id = v_existing.id;
    if not found then
        raise exception using errcode = '40001',
            message = 'settle_conversation_reactivation_rejected';
    end if;

    outcome := 'settled';
    reactivation_event_id := v_existing.id;
    return next;
end;
$function$;

-- Postgres concede execute a PUBLIC en toda funcion nueva: sin este revoke, una
-- RPC security definer que autoriza mandarle un WhatsApp a un lead queda al
-- alcance de anon.
revoke execute on function public.claim_conversation_reactivation(
    bigint, text, text, text, text, bigint, integer, integer, integer, timestamptz
) from public;
revoke execute on function public.settle_conversation_reactivation(
    text, text, bigint, text, timestamptz
) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke execute on function public.claim_conversation_reactivation(
            bigint, text, text, text, text, bigint, integer, integer, integer,
            timestamptz
        ) from anon;
        revoke execute on function public.settle_conversation_reactivation(
            text, text, bigint, text, timestamptz
        ) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke execute on function public.claim_conversation_reactivation(
            bigint, text, text, text, text, bigint, integer, integer, integer,
            timestamptz
        ) from authenticated;
        revoke execute on function public.settle_conversation_reactivation(
            text, text, bigint, text, timestamptz
        ) from authenticated;
        revoke all on table public.conversation_reactivation_events
            from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.claim_conversation_reactivation(
            bigint, text, text, text, text, bigint, integer, integer, integer,
            timestamptz
        ) to service_role;
        grant execute on function public.settle_conversation_reactivation(
            text, text, bigint, text, timestamptz
        ) to service_role;
    end if;
end
$roles$;

commit;
