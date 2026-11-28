-- close-resolved-conversation-v1 (parte 3 de E-539; contrato
-- docs/contracts/resume-paused-conversation-v1.md, «Cierre al resolver»).
--
-- Que problema resuelve. Resolver una conversacion en Chatwoot nunca llega
-- al setter: con lock_to_single_conversation = false en las bandejas 11 y 13,
-- cuando la persona vuelve a escribir Chatwoot abre otra conversacion y la
-- resuelta queda 'paused_human' con human_takeover para siempre (25 de 50 el
-- 2026-11-13, diagnostico E-539). Y como toda lectura por contacto mira todas
-- sus conversaciones, ese contacto queda «derivado» para siempre: sin primer
-- contacto (_portable_precheckout_stop_reason), sin disparadores nativos por
-- audiencia ni por palabra clave (conversacion_derivada), sin agente de
-- recupero (any_conversation_human_takeover) y con el AE mostrandolo tomado.
--
-- Que hace. Suma close_resolved_conversation_v1(conversacion, command_key,
-- now), que llama solo el bridge con CHATWOOT_RESOLVED_CLOSE_ENABLED cuando
-- un conversation_updated pasa la conversacion a 'resolved' en una bandeja
-- sin lock_to_single_conversation (Dan, 28/11, P-1 = c):
--   - cierra solo una conversacion que el equipo tomo (human_takeover), fuera
--     de 'completed', 'closed' y 'blocked'; si no, 'not_paused' sin escribir;
--   - LIBERA al contacto (Dan, 28/11): la conversacion pasa a status
--     'completed' (no 'closed': _portable_precheckout_stop_reason frena el
--     primer contacto ante cualquier conversacion 'closed' del contacto, y
--     'completed' no lo frena en ninguna lectura por contacto), con
--     closed_at, human_takeover = false, automation_status = 'draft_only' y
--     version + 1; y marca atendidas (attended_at = now, nunca antes de su
--     created_at) las derivaciones 'projected' sin atender de sus casos: el
--     equipo que resuelve atendio la derivacion (el AE deja de mostrarla
--     pendiente y el aviso de derivacion sin atender deja de salir);
--   - deja el evento 'chatwoot_resolved_conversation_closed' en
--     conversation_events con el command_key, el estado anterior y cuantas
--     derivaciones marco;
--   - con una derivacion 'requested' o 'projection_failed' (el proyector
--     trabajando) devuelve 'blocked_pending_projection' sin escribir: el
--     bridge reintenta;
--   - la clave ya usada da 'replayed'; una ya cerrada por este camino,
--     'already_closed'; sin conversacion, 'not_found'.
--
-- Lo que NO toca: el caso comercial (su guarda solo deja pausar y reanudar,
-- y ninguna lectura por contacto mira el caso inbound) ni las derivaciones en
-- otro estado. No crea tablas, columnas ni indices, no cambia ninguna funcion
-- vigente, no siembra filas. Correrla dos veces aborta con 55000.
--
-- Vuelta atras: drop de la funcion. Las conversaciones que haya cerrado y
-- las attended_at que haya marcado quedan: son el registro de que el equipo
-- resolvio; el estado anterior esta en el evento.
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
       or to_regclass('public.commercial_cases') is null
       or not exists (
           select 1
           from pg_attribute
           where attrelid = to_regclass('public.human_handoff_requests')
             and attname = 'attended_at'
             and attnum > 0
             and not attisdropped
       ) then
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
    v_attended integer;
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

    -- Ya cerrada por este camino (otra entrega de la misma resolucion).
    if v_conversation.status = 'completed'
       and exists (
           select 1
           from public.conversation_events event
           where event.conversation_id = v_conversation.id
             and event.event_type = 'chatwoot_resolved_conversation_closed'
       ) then
        outcome := 'already_closed';
        closed_conversation_id := v_conversation.id;
        return next;
        return;
    end if;

    -- Solo la conversacion que quedo con las personas (D-P3.4): es el estado
    -- muerto que deja una resuelta. Una que maneja el agente no se cierra.
    if not v_conversation.human_takeover
       or v_conversation.status in ('completed', 'closed', 'blocked') then
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

    v_now := coalesce(p_now, clock_timestamp());

    -- Primero los casos, en el orden de la reanudacion (conversacion -> caso);
    -- despues la derivacion. El equipo que resolvio atendio la derivacion.
    perform 1
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id
    order by commercial_case.id
    for update;

    update public.human_handoff_requests request
    set attended_at = greatest(v_now, request.created_at),
        updated_at = greatest(v_now, request.created_at)
    from public.commercial_cases commercial_case
    where commercial_case.id = request.commercial_case_id
      and commercial_case.conversation_id = v_conversation.id
      and request.status = 'projected'
      and request.attended_at is null
      and request.created_at <= v_now;
    get diagnostics v_attended = row_count;

    -- Liberar al contacto (Dan, 28/11): 'completed' con human_takeover y la
    -- automatizacion sueltos no frena ninguna lectura por contacto.
    update public.conversations conversation
    set status = 'completed',
        automation_status = 'draft_only',
        human_takeover = false,
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
            'previous_human_takeover', v_conversation.human_takeover,
            'handoffs_marked_attended', v_attended
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
    'equipo resolvio en Chatwoot, si la tomo el equipo, y libera al contacto '
    '(completed, sin human_takeover, draft_only, derivacion projected atendida). '
    'Solo service_role.';

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
