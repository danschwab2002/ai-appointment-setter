-- close-resolved-conversation-v1 (parte 3 de E-539; contrato
-- docs/contracts/resume-paused-conversation-v1.md, «Cierre al resolver»).
--
-- Que problema resuelve. Resolver una conversacion en Chatwoot nunca llega
-- al setter: con lock_to_single_conversation = false en las bandejas 11 y 13,
-- cuando la persona vuelve a escribir Chatwoot abre otra conversacion y la
-- resuelta queda 'paused_human' para siempre en la base (25 de 50 el
-- 2026-11-13, diagnostico E-539). No existe una RPC que cierre una
-- conversacion del setter.
--
-- Que hace. Suma close_resolved_conversation_v1(conversacion, command_key,
-- now), que llama solo el bridge con CHATWOOT_RESOLVED_CLOSE_ENABLED cuando
-- un conversation_updated pasa la conversacion a 'resolved':
--   - cierra solo una conversacion que el equipo tomo (human_takeover), fuera
--     de 'completed', 'closed' y 'blocked'; si no, 'not_paused' sin escribir;
--   - la pasa a status 'closed' con closed_at, version + 1, y deja el evento
--     'chatwoot_resolved_conversation_closed' en conversation_events con el
--     command_key, el estado anterior y cuantas derivaciones 'projected'
--     quedaban sin atender;
--   - con una derivacion 'requested' o 'projection_failed' (el proyector
--     trabajando) devuelve 'blocked_pending_projection' sin escribir: el
--     bridge reintenta;
--   - la clave ya usada da 'replayed'; una ya cerrada, 'already_closed'; sin
--     conversacion, 'not_found'.
--
-- Lo que NO toca, a proposito (D-P3.2 y D-P3.3): human_takeover,
-- automation_status, el caso comercial y las derivaciones. Todas las
-- lecturas que deciden si a un contacto se le puede escribir (la admision,
-- el primer contacto, los disparadores nativos por audiencia y palabra
-- clave, el agente de recupero, la reactivacion, el AE) miran
-- human_takeover, automation_status o tratan 'closed' igual que
-- 'paused_human'. Asi, cerrar no cambia lo que ve ningun lead. Liberar al
-- contacto (apagar human_takeover, cerrar o atender la derivacion) cambia
-- que cuenta como «persona derivada» y lo decide Dan.
--
-- Lo que no hace: no crea tablas, columnas ni indices, no cambia ninguna
-- funcion vigente, no siembra filas. Correrla dos veces aborta con 55000 sin
-- cambiar nada.
--
-- Vuelta atras: drop de la funcion. Las conversaciones que haya cerrado
-- quedan cerradas: el estado anterior esta en el evento, y la reanudacion
-- por mensaje entrante (resume_paused_conversation) las sigue levantando
-- igual que a una 'paused_human'.
--
-- Errores: 22023 entrada invalida; 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $already$
begin
    if to_regclass('public.conversations') is null
       or to_regclass('public.conversation_events') is null
       or to_regclass('public.human_handoff_requests') is null
       or to_regclass('public.commercial_cases') is null then
        raise exception using errcode = '55000',
            message = 'close_resolved_conversation_missing';
    end if;
    if to_regprocedure('public.close_resolved_conversation_v1(bigint,text,timestamptz)') is not null then
        raise exception using errcode = '55000',
            message = 'close_resolved_conversation_already_applied';
    end if;
end;
$already$;

create function public.close_resolved_conversation_v1(
    p_external_conversation_id bigint,
    p_command_key text,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    closed_conversation_id uuid,
    close_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_conversation public.conversations%rowtype;
    v_existing_event_id uuid;
    v_unattended integer;
    v_event_id uuid;
    v_now timestamptz;
begin
    if p_external_conversation_id is null
       or p_external_conversation_id <= 0
       or p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$' then
        raise exception using errcode = '22023',
            message = 'close_resolved_conversation_invalid_input';
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

    -- Idempotencia: el command_key vive en el evento (la tabla no tiene
    -- columna propia); el lock de la conversacion serializa los reintentos.
    select event.id into v_existing_event_id
    from public.conversation_events event
    where event.conversation_id = v_conversation.id
      and event.event_type = 'chatwoot_resolved_conversation_closed'
      and event.data ->> 'command_key' = p_command_key
    order by event.created_at
    limit 1;
    if v_existing_event_id is not null then
        outcome := 'replayed';
        closed_conversation_id := v_conversation.id;
        close_event_id := v_existing_event_id;
        return next;
        return;
    end if;

    if v_conversation.status = 'closed' then
        outcome := 'already_closed';
        closed_conversation_id := v_conversation.id;
        return next;
        return;
    end if;

    -- Solo la conversacion que quedo con las personas (D-P3.4): es el estado
    -- muerto que deja una resuelta. Una que maneja el agente no se cierra, y
    -- una 'paused_human' sin human_takeover tampoco: al dejar de ser
    -- 'paused_human' los disparadores nativos por audiencia y palabra clave,
    -- que no miran 'closed', podrian volver a escribirle.
    if not v_conversation.human_takeover
       or v_conversation.status in ('completed', 'blocked') then
        outcome := 'not_paused';
        closed_conversation_id := v_conversation.id;
        return next;
        return;
    end if;

    -- El proyector de notas todavia trabaja: no se le cierra la conversacion
    -- abajo.
    if exists (
        select 1
        from public.human_handoff_requests request
        join public.commercial_cases commercial_case
          on commercial_case.id = request.commercial_case_id
        where commercial_case.conversation_id = v_conversation.id
          and request.status in ('requested', 'projection_failed')
    ) then
        outcome := 'blocked_pending_projection';
        closed_conversation_id := v_conversation.id;
        return next;
        return;
    end if;

    select count(*) into v_unattended
    from public.human_handoff_requests request
    join public.commercial_cases commercial_case
      on commercial_case.id = request.commercial_case_id
    where commercial_case.conversation_id = v_conversation.id
      and request.status = 'projected'
      and request.attended_at is null;

    v_now := coalesce(p_now, clock_timestamp());

    update public.conversations conversation
    set status = 'closed',
        closed_at = v_now,
        version = conversation.version + 1,
        updated_at = v_now
    where conversation.id = v_conversation.id;

    insert into public.conversation_events (
        conversation_id, event_type, actor_type, data, created_at
    ) values (
        v_conversation.id,
        'chatwoot_resolved_conversation_closed',
        'human_agent',
        jsonb_build_object(
            'command_key', p_command_key,
            'external_conversation_id', p_external_conversation_id,
            'previous_status', v_conversation.status,
            'previous_automation_status', v_conversation.automation_status,
            'human_takeover', v_conversation.human_takeover,
            'unattended_projected_handoffs', v_unattended
        ),
        v_now
    )
    returning id into v_event_id;

    outcome := 'closed';
    closed_conversation_id := v_conversation.id;
    close_event_id := v_event_id;
    return next;
end;
$function$;

comment on function public.close_resolved_conversation_v1(
    bigint, text, timestamptz) is
    'Parte 3 de E-539 (20261128300000): cierra la conversacion del setter que el '
    'equipo resolvio en Chatwoot, si la tomo el equipo; no toca human_takeover, '
    'automation_status, el caso ni las derivaciones. Solo service_role.';

do $roles$
declare
    v_role text;
    c_function constant text :=
        'public.close_resolved_conversation_v1(bigint,text,timestamptz)';
begin
    execute format('revoke all on function %s from public', c_function);
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format('revoke all on function %s from %I', c_function, v_role);
        end if;
    end loop;
    if to_regrole('service_role') is not null then
        execute format('grant execute on function %s to service_role', c_function);
    end if;
end;
$roles$;

commit;
