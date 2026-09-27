-- Migration: una derivacion sin atender bloquea la automatizacion.
--
-- Que problema resuelve. El 2026-09-27 se midio en produccion el choque entre
-- dos sistemas que tratan 'automation_paused' como si significara lo mismo:
--
--   * La derivacion pausa porque el lead pidio una persona (o porque el
--     sistema no puede seguir solo). La pausa es correcta y tiene que durar
--     hasta que alguien del equipo conteste.
--   * La reactivacion de conversaciones (20260923000200) asume que toda pausa
--     es un lead que se enfrio y hay que rescatarlo con una plantilla.
--   * La reanudacion (20260923000100) apaga human_takeover cuando el lead
--     responde, sin preguntar si una persona llego a atenderlo.
--
-- Lo medido: de los 10 envios de reactivacion, 9 salieron sobre conversaciones
-- con una derivacion en 'projected' que nadie habia contestado, y a dos de
-- ellas se les mando 'soy el asistente virtual' habiendo pedido un humano. La
-- conversacion 186 ademas se despauso dos veces, borrando la derivacion.
--
-- Por que faltaba el dato. human_handoff_requests.status va
-- requested -> projected y se queda ahi para siempre: ningun estado significa
-- 'ya la contestaron'. Sin eso, ni la reactivacion ni la reanudacion pueden
-- saber si la derivacion sigue viva.
--
-- Que hace esta migracion.
--
--   1. Agrega human_handoff_requests.attended_at: cuando una persona del
--      equipo escribio en esa conversacion despues de la derivacion.
--   2. mark_human_handoff_attended: la marca. La llama el bridge en el mismo
--      punto en el que ya detecta que una persona escribio y pausa la
--      automatizacion, asi que se alimenta sola y sigue funcionando con la
--      reactivacion apagada.
--   3. Redefine claim_conversation_reactivation: con una derivacion sin
--      atender devuelve 'blocked_pending_handoff' y no reserva el envio.
--   4. Redefine resume_paused_conversation: una derivacion 'projected' sin
--      atender ya cuenta como pendiente, asi que la respuesta del lead deja
--      de levantar la pausa.
--
-- Lo que NO hace, a proposito:
--
--   1. No toca el CHECK de human_handoff_requests.status. Esa columna la leen
--      el proyector de notas y el conector de Slack; un estado terminal nuevo
--      les cambiaria el contrato. attended_at es una columna aparte, nulable,
--      que nadie tiene que leer para seguir funcionando.
--   2. No toca human_handoff_requests_one_live_per_commercial_case_idx. Ese
--      indice define cuando request_inbound_human_handoff rechaza una
--      derivacion nueva por 'inbound_handoff_live_request_conflict', y
--      ampliarlo a 'projected' haria fallar derivaciones legitimas.
--   3. No desbloquea nada sola. Una derivacion sin atender se destraba porque
--      una persona contesta, saca la etiqueta o corre el macro de
--      reanudacion; las tres son decisiones humanas.
--   4. No marca atendidas las derivaciones historicas. Las filas existentes
--      quedan con attended_at nulo, que es lo unico honesto: el dato de si
--      alguien las contesto no esta en esta base. En la practica eso deja las
--      conversaciones derivadas viejas en manos del equipo, que es el estado
--      correcto.

begin;

-- 1. Cuando una persona del equipo atendio la derivacion.

alter table public.human_handoff_requests
    add column attended_at timestamptz;

alter table public.human_handoff_requests
    add constraint human_handoff_requests_attended_after_created
    check (attended_at is null or attended_at >= created_at);

comment on column public.human_handoff_requests.attended_at is
    'Momento en que una persona del equipo escribio en la conversacion despues '
    'de esta derivacion. Nulo = sigue esperando. Lo marca '
    'mark_human_handoff_attended desde el bridge.';

-- El indice de la cola de pendientes: que derivaciones espera el equipo y
-- desde cuando. Es la metrica que no existia.
create index human_handoff_requests_unattended_idx
on public.human_handoff_requests (created_at)
where attended_at is null
  and status in ('requested', 'projected', 'projection_failed');

-- 2. Marcar que una persona atendio.
--
-- Recibe el momento del mensaje del equipo y marca las derivaciones de ese
-- caso que son anteriores. La comparacion contra created_at importa: si la
-- persona escribio a las 10:00 y el agente derivo a las 11:00, esa derivacion
-- nueva sigue sin atender.

create or replace function public.mark_human_handoff_attended(
    p_external_conversation_id bigint,
    p_attended_at timestamptz,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    attended_count integer,
    attended_commercial_case_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_conversation public.conversations%rowtype;
    v_case public.commercial_cases%rowtype;
    v_case_count integer;
    v_marked integer;
    v_attended_at timestamptz;
begin
    if p_external_conversation_id is null
       or p_external_conversation_id <= 0
       or p_attended_at is null then
        raise exception using errcode = '22023',
            message = 'mark_human_handoff_attended_invalid_input';
    end if;

    attended_count := 0;

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

    -- Falla cerrado ante ambiguedad, igual que las otras dos funciones.
    select count(*) into v_case_count
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id;
    if v_case_count > 1 then
        raise exception using errcode = 'P0001',
            message = 'mark_human_handoff_attended_ambiguous_case';
    end if;

    select commercial_case.* into v_case
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id;
    if v_case.id is null then
        outcome := 'not_found';
        return next;
        return;
    end if;

    -- Un reloj adelantado del lado de Chatwoot no puede grabar una atencion en
    -- el futuro: se recorta a ahora.
    v_attended_at := least(p_attended_at, coalesce(p_now, clock_timestamp()));

    update public.human_handoff_requests request
    set attended_at = v_attended_at,
        updated_at = coalesce(p_now, clock_timestamp())
    where request.commercial_case_id = v_case.id
      and request.attended_at is null
      and request.status in ('requested', 'projected', 'projection_failed')
      and request.created_at <= v_attended_at;
    get diagnostics v_marked = row_count;

    attended_count := v_marked;
    attended_commercial_case_id := v_case.id;
    outcome := case when v_marked > 0 then 'attended' else 'noop' end;
    return next;
end;
$function$;

-- 3. La reactivacion no reserva un envio sobre una derivacion sin atender.

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

    -- Una derivacion que ninguna persona atendio todavia es trabajo pendiente
    -- del equipo, no una conversacion que se enfrio. Medido el 2026-09-27
    -- sobre produccion: 9 de los 10 envios de reactivacion salieron sobre
    -- conversaciones derivadas y sin atender, y en dos de ellas la persona
    -- habia pedido explicitamente hablar con alguien del equipo
    -- (detail_reason_code = 'explicit_human_request', conversaciones 186 y
    -- 185). Reactivar ahi le contesta con un bot a quien pidio un humano y
    -- gasta la unica plantilla que podia reabrir la ventana de 24 h.
    --
    -- El bridge ya no deberia llegar hasta aca con una derivacion viva: la
    -- saltea al evaluar el candidato. Este guard es la capa durable, la que no
    -- se puede evadir si el scanner se reescribe o alguien llama a la RPC a
    -- mano.
    if exists (
        select 1
        from public.human_handoff_requests request
        where request.commercial_case_id = v_case.id
          and request.status in ('requested', 'projected', 'projection_failed')
          and request.attended_at is null
    ) then
        outcome := 'blocked_pending_handoff';
        reactivated_conversation_id := v_conversation.id;
        reactivated_commercial_case_id := v_case.id;
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

-- 4. La respuesta del lead no levanta una pausa que el equipo debe atender.

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

    -- Una derivacion sin atender bloquea la reanudacion, por dos motivos
    -- distintos que esta condicion junta.
    --
    -- 'requested' y 'projection_failed' son trabajo en curso del proyector de
    -- notas: reanudar ahi se pisa con el, y ademas dejaria viva una fila que
    -- hace fallar al proximo handoff con
    -- 'inbound_handoff_live_request_conflict'. Eso ya era asi.
    --
    -- 'projected' con attended_at nulo es la derivacion que llego a Chatwoot y
    -- que ninguna persona contesto todavia. Antes del 2026-09-27 esta funcion
    -- la despausaba igual: apagaba human_takeover y devolvia la conversacion
    -- al agente sin que nadie hubiera atendido al lead. Medido ese dia en la
    -- conversacion 186: se despauso dos veces (26/09 17:08 y 27/09 18:11)
    -- sobre una derivacion por pedido explicito de humano que seguia sin
    -- respuesta. La derivacion se borraba sola.
    --
    -- La salida de este estado no es automatica y es a proposito: una persona
    -- contesta (y el bridge marca attended_at), o saca la etiqueta a mano, o
    -- corre el macro de reanudacion. En los tres casos la decision de devolver
    -- la conversacion al agente la toma un humano.
    if exists (
        select 1
        from public.human_handoff_requests request
        where request.commercial_case_id = v_case.id
          and (
              request.status in ('requested', 'projection_failed')
              or (request.status = 'projected' and request.attended_at is null)
          )
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

revoke execute on function public.mark_human_handoff_attended(
    bigint, timestamptz, timestamptz
) from public;
revoke execute on function public.claim_conversation_reactivation(
    bigint, text, text, text, text, bigint, integer, integer, integer, timestamptz
) from public;
revoke execute on function public.resume_paused_conversation(
    bigint, text, text, integer, integer, timestamptz
) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke execute on function public.mark_human_handoff_attended(
            bigint, timestamptz, timestamptz
        ) from anon;
        revoke execute on function public.claim_conversation_reactivation(
            bigint, text, text, text, text, bigint, integer, integer, integer,
            timestamptz
        ) from anon;
        revoke execute on function public.resume_paused_conversation(
            bigint, text, text, integer, integer, timestamptz
        ) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke execute on function public.mark_human_handoff_attended(
            bigint, timestamptz, timestamptz
        ) from authenticated;
        revoke execute on function public.claim_conversation_reactivation(
            bigint, text, text, text, text, bigint, integer, integer, integer,
            timestamptz
        ) from authenticated;
        revoke execute on function public.resume_paused_conversation(
            bigint, text, text, integer, integer, timestamptz
        ) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.mark_human_handoff_attended(
            bigint, timestamptz, timestamptz
        ) to service_role;
        grant execute on function public.claim_conversation_reactivation(
            bigint, text, text, text, text, bigint, integer, integer, integer,
            timestamptz
        ) to service_role;
        grant execute on function public.resume_paused_conversation(
            bigint, text, text, integer, integer, timestamptz
        ) to service_role;
    end if;
end
$roles$;

commit;
