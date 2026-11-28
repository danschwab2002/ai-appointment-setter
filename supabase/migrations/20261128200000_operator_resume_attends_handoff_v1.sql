-- operator-resume-attends-handoff-v1 (E-577, parte 1 de E-539; contrato
-- docs/contracts/resume-paused-conversation-v1.md, «Reanudación al sacar la
-- etiqueta»).
--
-- Que problema resuelve. resume_paused_conversation (vigente en
-- 20261001000500) corta con blocked_pending_handoff ante una derivacion
-- 'projected' con attended_at nulo sin mirar p_reason_code, en contra del
-- ADR-0019 (sacar la etiqueta o correr el macro devuelve la conversacion al
-- agente): sacar la etiqueta de una derivacion que nadie contesto no la
-- reanuda, tampoco con CHATWOOT_LABEL_REMOVAL_RESUME_ENABLED prendido (Maribel
-- conversacion 260, 2026-11-12). Y el camino del conversation_updated (E-571)
-- llama a la RPC sin saber si la conversacion estaba pausada (D-2 del #468):
-- sobre una que no lo estaba, la pasaria a 'active' aunque estuviera
-- 'closed', 'completed' o 'blocked'.
--
-- Que hace. Suma resume_paused_conversation_by_operator_v1: la reanudacion
-- por una decision de una persona que el bridge VIO (el conversation_updated
-- que saca automation_paused). La vigente NO cambia: el camino del mensaje
-- entrante deduce operator_request por la AUSENCIA de la etiqueta, y como la
-- etiqueta y la pausa durable se escriben por caminos distintos, una etiqueta
-- que nunca se llego a poner se ve igual que una que alguien saco; ahi una
-- derivacion sin atender sigue bloqueando, como desde 20260927000300
-- (conversacion 186). La nueva:
--   - reanuda solo una conversacion pausada (human_takeover o 'paused_human',
--     fuera de 'completed', 'closed' y 'blocked'); si no, 'not_paused' sin
--     escribir nada (tambien el eco de la remocion que hace el propio bridge
--     al reanudar);
--   - bloquea los casos de la conversacion en el orden de la vigente
--     (conversacion -> caso), marca atendidas (attended_at = now, nunca antes
--     de su created_at) las derivaciones 'projected' sin atender creadas hasta
--     ahora y delega en la vigente con operator_request;
--   - 'requested' y 'projection_failed' (el proyector trabajando) siguen
--     bloqueando: lo dice la vigente;
--   - si la vigente no reanuda (tope, contacto bloqueado, ...), un subbloque
--     con una excepcion propia (E5770) deshace la marca y devuelve la salida
--     de la vigente tal cual;
--   - con la clave ya usada o sin conversacion delega igual (replayed /
--     not_found).
-- La traza de quien atendio es la fila operator_request de
-- conversation_resume_events, en la misma transaccion (sin columna nueva).
--
-- Lo que no hace: no crea tablas, columnas ni indices, no toca la vigente ni
-- ninguna otra funcion (su duenio y su ACL quedan como estan), no siembra
-- filas. Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Vuelta atras: drop de la funcion. Las attended_at que haya marcado quedan:
-- son decisiones de personas del equipo que ya ocurrieron.
--
-- Errores: 22023 entrada invalida; 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $already$
declare
    c_vigente constant text :=
        'public.resume_paused_conversation(bigint,text,text,integer,integer,timestamptz)';
    v_definition text;
begin
    if to_regprocedure(c_vigente) is null
       or to_regclass('public.conversation_resume_events') is null
       or not exists (
           select 1
           from pg_attribute
           where attrelid = to_regclass('public.human_handoff_requests')
             and attname = 'attended_at'
             and attnum > 0
             and not attisdropped
       ) then
        raise exception using errcode = '55000',
            message = 'operator_resume_attends_handoff_missing';
    end if;
    if to_regprocedure('public.resume_paused_conversation_by_operator_v1(bigint,text,integer,timestamptz)') is not null then
        raise exception using errcode = '55000',
            message = 'operator_resume_attends_handoff_already_applied';
    end if;
    -- La nueva delega en la vigente: tiene que ser la de la atencion
    -- (20260927000300 o posterior) ...
    v_definition := pg_get_functiondef(c_vigente::regprocedure);
    if position('request.attended_at is null' in v_definition) = 0
       or position('''operator_request''' in v_definition) = 0
       or position('blocked_resume_limit' in v_definition) = 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_resume_paused_conversation_definition';
    end if;
    -- ... y del duenio que corre la migracion: la nueva es security definer y
    -- la llama como su duenio, sin grant de por medio.
    if (select p.proowner from pg_proc p where p.oid = c_vigente::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000',
            message = 'operator_resume_attends_handoff_owner_mismatch';
    end if;
end;
$already$;

create function public.resume_paused_conversation_by_operator_v1(
    p_external_conversation_id bigint,
    p_command_key text,
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
    v_now timestamptz;
    v_outcome text;
    v_conversation_id uuid;
    v_case_id uuid;
    v_event_id uuid;
begin
    if p_external_conversation_id is null
       or p_external_conversation_id <= 0
       or p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_max_resumes is null
       or p_max_resumes < 1 then
        raise exception using errcode = '22023',
            message = 'resume_paused_conversation_by_operator_invalid_input';
    end if;

    v_now := coalesce(p_now, clock_timestamp());

    select conversation.* into v_conversation
    from public.conversations conversation
    where conversation.commercial_context = jsonb_build_object(
        'chatwoot_conversation_id', p_external_conversation_id::text
    )
    for update;

    -- Sin conversacion o con la clave ya usada: la vigente contesta
    -- not_found o replayed sin escribir.
    if v_conversation.id is null or exists (
        select 1
        from public.conversation_resume_events event
        where event.command_key = p_command_key
    ) then
        return query
        select resumed.outcome, resumed.resumed_conversation_id,
               resumed.resumed_commercial_case_id, resumed.resume_event_id
        from public.resume_paused_conversation(
            p_external_conversation_id, p_command_key, 'operator_request',
            null, p_max_resumes, v_now
        ) resumed;
        return;
    end if;

    -- Sin pausa no hay decision que respetar (tambien el eco de la remocion
    -- que hace el propio bridge al reanudar). Una conversacion cerrada,
    -- completada o bloqueada no vuelve al agente por una etiqueta.
    if not (v_conversation.human_takeover
            or v_conversation.status = 'paused_human')
       or v_conversation.status in ('completed', 'closed', 'blocked') then
        outcome := 'not_paused';
        resumed_conversation_id := v_conversation.id;
        return next;
        return;
    end if;

    -- operator_resume_attends_handoff (E-539 parte 1): la persona que saco la
    -- etiqueta atendio la derivacion que llego a Chatwoot. Primero los casos,
    -- en el orden de la vigente (conversacion -> caso); despues la derivacion.
    perform 1
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id
    order by commercial_case.id
    for update;

    begin
        update public.human_handoff_requests request
        set attended_at = greatest(v_now, request.created_at),
            updated_at = greatest(v_now, request.created_at)
        from public.commercial_cases commercial_case
        where commercial_case.id = request.commercial_case_id
          and commercial_case.conversation_id = v_conversation.id
          and request.status = 'projected'
          and request.attended_at is null
          and request.created_at <= v_now;

        select resumed.outcome, resumed.resumed_conversation_id,
               resumed.resumed_commercial_case_id, resumed.resume_event_id
        into v_outcome, v_conversation_id, v_case_id, v_event_id
        from public.resume_paused_conversation(
            p_external_conversation_id, p_command_key, 'operator_request',
            null, p_max_resumes, v_now
        ) resumed;

        if v_outcome is distinct from 'resumed' then
            raise exception using errcode = 'E5770',
                message = 'resume_paused_conversation_by_operator_undo';
        end if;
    exception
        when sqlstate 'E5770' then
            -- El subbloque deshace la marca; las variables conservan la
            -- salida de la vigente.
            null;
    end;

    outcome := v_outcome;
    resumed_conversation_id := v_conversation_id;
    resumed_commercial_case_id := v_case_id;
    resume_event_id := v_event_id;
    return next;
end;
$function$;

comment on function public.resume_paused_conversation_by_operator_v1(
    bigint, text, integer, timestamptz) is
    'E-577 (20261128200000): reanuda por la decision vista de una persona (la etiqueta '
    'automation_paused sacada); marca atendida la derivacion projected y delega en '
    'resume_paused_conversation con operator_request; not_paused si no estaba pausada. '
    'Solo service_role.';

do $roles$
declare
    v_role text;
    c_function constant text :=
        'public.resume_paused_conversation_by_operator_v1(bigint,text,integer,timestamptz)';
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
