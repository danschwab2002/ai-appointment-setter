-- close-recovery-case-without-pending-touch-v1: cuando el ultimo toque de un
-- caso termina sin salir, el caso se cierra (E-423, decision de Dan del
-- 2026-11-10 sobre el informe E-413 del programa etapa-1).
--
-- Por que. _finalize_followup_delivery_attempt (vigente en 20260813000100)
-- cierra el caso en un solo lugar: la rama accepted_by_chatwoot, cuando ya no
-- queda paso. Las ramas que terminan el toque sin mandarlo (permanent_failed:
-- failed_before_request o rejected sin reintento; expired: el reintento cae
-- despues del vencimiento) cambian solo la accion. La secuencia queda active y
-- el caso en grace_period (o active, si ya habia salido un toque), sin nada
-- pendiente, y nada los vence: claim_due_followup_actions solo expira acciones
-- vivas, grace_expires_at no lo compara nadie y no hay pg_cron. Ese caso
-- abierto frena lo que venga despues de la misma persona: el carrito del
-- legado choca con followup_sequences_one_active_per_case_idx (23505), el
-- motor contesta ya_en_curso, el nativo caso_del_legado_en_curso, el primer
-- contacto del formulario superseded_by_provider_event o
-- precheckout_contact_already_planned, el pago rechazado se absorbe en el caso
-- sin mensaje y el reciclado de identidades cuenta casos_abiertos. Medido el
-- 2026-11-10 ~23:00Z en solo lectura: 18 casos asi en ATT1 y 10 en Maribel,
-- todos con su unico toque permanent_failed.
--
-- Que hace.
-- 1. _close_recovery_case_without_pending_touch_v1(accion, origen, ahora),
--    privada (ningun rol de la API la ejecuta). Si la accion termino sin salir
--    (permanent_failed o expired), el caso esta en grace_period o active y no
--    le queda ninguna accion viva (pending, deferred, retryable_failed,
--    delivery_unknown), y la persona no contesto ni la tomo alguien del equipo
--    (ningun toque cancelled por prospect_replied o human_takeover_active, y la
--    conversacion del caso sin human_takeover ni un entrante posterior al caso):
--      - completa la secuencia active del caso, con completion_reason
--        delivery_failed_no_touch_left;
--      - cierra el caso en unreachable si nunca salio un toque (ninguna accion
--        accepted_by_chatwoot), o en sequence_exhausted si ya habia salido
--        alguno; closed_at, next_contact_at null, version + 1;
--      - deja la marca closed_without_pending_touch en recovery_cases.context
--        (el estado de antes, la accion, su motivo, las secuencias que completo,
--        el origen y la hora): con ella se puede volver atras caso por caso;
--      - suma un conversation_events recovery_case_closed_without_pending_touch.
--    Si algo de eso no se cumple, no toca nada y contesta por que. Repetirla no
--    cambia nada (el caso ya no esta abierto).
-- 2. finalize_followup_delivery_attempt, el envoltorio publico que llama el
--    bridge (ultima definicion en 20260809000100), igual que antes y, al final,
--    si la accion quedo permanent_failed o expired, llama a la funcion de 1
--    con origen finalize. Devuelve la misma fila que antes.
--
-- Lo que NO cambia, a proposito:
-- - _finalize_followup_delivery_attempt: close_recovery_case_segment_coupon_v1
--   (20261106000100) la llama directo y despues cancela el caso; si la privada
--   lo cerrara antes, esa funcion contestaria case_not_open y el caso quedaria
--   unreachable en vez de cancelled.
-- - La baja (_finalize_opted_out_followup_not_applied): sale del envoltorio
--   antes y cierra el caso a su manera.
-- - La reconciliacion de un delivery_unknown (reconcile_followup_delivery_attempt):
--   puede dejar permanent_failed y no pasa por aca. Medido el 10/11: cero casos.
-- - Los casos cuyo ultimo toque quedo cancelled (la persona contesto o la tomo
--   alguien del equipo): Dan decidio esperar (10/11). Esta funcion solo mira
--   acciones permanent_failed o expired.
-- - Los casos paused: no se tocan.
-- - Los permisos del envoltorio: create or replace los conserva (service_role,
--   como en 20260812000100).
-- - El bridge: ya trata permanent_failed como final.
--
-- Los casos que ya estan trabados no los cierra esta migracion: van con el kit
-- del destrabe del programa (vista primero, --aplicar con conteo exacto), que
-- llama a la misma funcion con origen destrabe.
--
-- Errores: 55000 correrla dos veces (la funcion privada ya existe) o si la
-- definicion vigente del envoltorio no es la de 20260809000100.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $guard$
begin
    if to_regprocedure(
        'public._close_recovery_case_without_pending_touch_v1(uuid,text,timestamptz)'
    ) is not null then
        raise exception using errcode = '55000',
            message = 'close_recovery_case_without_pending_touch_already_applied';
    end if;
    -- El envoltorio vigente tiene que ser el de 20260809000100: la baja primero
    -- y despues la privada. Si otra migracion lo cambio, esto se revisa a mano.
    if position(
           '_finalize_opted_out_followup_not_applied' in pg_get_functiondef(
               'public.finalize_followup_delivery_attempt(uuid,uuid,text,bigint,text,text,uuid,text,timestamptz,timestamptz,timestamptz)'::regprocedure
           )
       ) = 0
       or position(
           '_close_recovery_case_without_pending_touch' in pg_get_functiondef(
               'public.finalize_followup_delivery_attempt(uuid,uuid,text,bigint,text,text,uuid,text,timestamptz,timestamptz,timestamptz)'::regprocedure
           )
       ) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_finalize_followup_delivery_attempt_definition';
    end if;
end;
$guard$;

-- ---------------------------------------------------------------------------
-- 1. El cierre.
-- ---------------------------------------------------------------------------
create function public._close_recovery_case_without_pending_touch_v1(
    p_action_id uuid,
    p_origin text,
    p_now timestamptz
)
returns text
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    -- close_recovery_case_without_pending_touch: el motivo vive aca y en el kit
    -- del destrabe.
    c_reason constant text := 'delivery_failed_no_touch_left';
    v_action public.scheduled_actions%rowtype;
    v_case public.recovery_cases%rowtype;
    v_to_status text;
    v_sequences uuid[];
begin
    if p_origin is null or p_origin not in ('finalize', 'destrabe') then
        raise exception using errcode = '22023', message = 'close_without_pending_touch_origin_invalid';
    end if;
    if p_now is null then
        raise exception using errcode = '22023', message = 'close_without_pending_touch_now_required';
    end if;

    select action_row.* into v_action
      from public.scheduled_actions action_row
     where action_row.id = p_action_id;
    if not found then
        return 'action_not_found';
    end if;

    -- El candado del caso, como _finalize_followup_delivery_attempt (que ya lo
    -- tiene cuando llama el envoltorio). Las acciones hermanas se leen bajo ese
    -- candado sin tomarlas: todo camino que cambia el estado de una accion toma
    -- antes el del caso, y claim_due_followup_actions toma una hermana viva y
    -- despues pide el caso (por la FK de conversation_events): tomarla aca
    -- podria cerrar un ciclo de candados. Las secuencias las toma el update.
    select case_row.* into v_case
      from public.recovery_cases case_row
     where case_row.id = v_action.recovery_case_id
     for update;
    select action_row.* into strict v_action
      from public.scheduled_actions action_row
     where action_row.id = p_action_id;

    if v_action.status not in ('permanent_failed', 'expired') then
        return 'action_not_terminal';
    end if;
    if v_case.status not in ('grace_period', 'active') then
        return 'case_not_open';
    end if;
    if exists (
        select 1
          from public.scheduled_actions action_row
         where action_row.recovery_case_id = v_case.id
           and action_row.status in ('pending', 'deferred', 'retryable_failed', 'delivery_unknown')
    ) then
        return 'touch_pending';
    end if;
    -- La persona contesto o la tomo alguien del equipo: es la otra clase de caso
    -- sin toque (el toque quedo cancelled), y Dan decidio esperar (10/11). Si
    -- ademas un intento en vuelo termino sin salir, el caso tampoco se cierra.
    if exists (
        select 1
          from public.scheduled_actions action_row
         where action_row.recovery_case_id = v_case.id
           and action_row.status = 'cancelled'
           and action_row.terminal_reason in ('prospect_replied', 'human_takeover_active')
    )
    or exists (
        select 1
          from public.conversations conversation
         where conversation.id in (
                   select v_case.conversation_id
                   union
                   select sequence_row.conversation_id
                     from public.followup_sequences sequence_row
                    where sequence_row.recovery_case_id = v_case.id
               )
           and (
               conversation.human_takeover
               or conversation.last_inbound_at > v_case.created_at
           )
    ) then
        return 'conversation_in_progress';
    end if;

    v_to_status := case
        when exists (
            select 1
              from public.scheduled_actions action_row
             where action_row.recovery_case_id = v_case.id
               and action_row.status = 'accepted_by_chatwoot'
        ) then 'sequence_exhausted'
        else 'unreachable'
    end;

    with completed as (
        update public.followup_sequences sequence_row
           set status = 'completed',
               completion_reason = c_reason,
               completed_at = p_now,
               revision = sequence_row.revision + 1
         where sequence_row.recovery_case_id = v_case.id
           and sequence_row.status = 'active'
        returning sequence_row.id
    )
    select coalesce(array_agg(id order by id), array[]::uuid[]) into v_sequences from completed;

    update public.recovery_cases case_row
       set status = v_to_status,
           closed_at = p_now,
           next_contact_at = null,
           next_contact_reason = c_reason,
           version = case_row.version + 1,
           context = case_row.context || jsonb_build_object(
               'closed_without_pending_touch', jsonb_build_object(
                   'from_status', v_case.status,
                   'to_status', v_to_status,
                   'from_next_contact_at', v_case.next_contact_at,
                   'from_next_contact_reason', v_case.next_contact_reason,
                   'action_id', v_action.id,
                   'action_status', v_action.status,
                   'terminal_reason', v_action.terminal_reason,
                   'sequence_ids', to_jsonb(v_sequences),
                   'origin', p_origin,
                   'closed_at', p_now
               )
           )
     where case_row.id = v_case.id;

    insert into public.conversation_events (
        recovery_case_id,
        event_type,
        actor_type,
        related_action_id,
        data
    ) values (
        v_case.id,
        'recovery_case_closed_without_pending_touch',
        'system',
        v_action.id,
        jsonb_build_object(
            'from_status', v_case.status,
            'to_status', v_to_status,
            'action_status', v_action.status,
            'terminal_reason', v_action.terminal_reason,
            'sequence_ids', to_jsonb(v_sequences),
            'origin', p_origin
        )
    );

    return 'closed_' || v_to_status;
end;
$function$;

do $acl$
declare
    v_signature constant text := 'public._close_recovery_case_without_pending_touch_v1(uuid,text,timestamptz)';
    v_role text;
begin
    execute format('revoke all on function %s from public', v_signature);
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format('revoke all on function %s from %I', v_signature, v_role);
        end if;
    end loop;
    if exists (
        select 1 from pg_roles
         where rolname in ('anon', 'authenticated', 'service_role')
           and has_function_privilege(rolname, v_signature, 'EXECUTE')
    ) then
        raise exception using errcode = '55000',
            message = 'close_recovery_case_without_pending_touch_acl_unexpected',
            detail = format('ningun rol de la API ejecuta %s', v_signature);
    end if;
end;
$acl$;

-- ---------------------------------------------------------------------------
-- 2. El envoltorio publico: lo mismo que 20260809000100 y, al final, el cierre.
-- ---------------------------------------------------------------------------
create or replace function public.finalize_followup_delivery_attempt(
    p_action_id uuid,
    p_attempt_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_outcome text,
    p_remote_message_id text,
    p_accepted_message_id uuid,
    p_reason_code text,
    p_next_attempt_at timestamptz,
    p_reconciliation_deadline timestamptz,
    p_now timestamptz
)
returns setof public.scheduled_actions
language plpgsql
security definer
set search_path = public, pg_temp
as $finalize$
declare
    v_row public.scheduled_actions%rowtype;
begin
    if p_outcome = 'accepted_by_chatwoot'
       or p_accepted_message_id is not null then
        raise exception using
            errcode = '55000',
            message = 'canonical_acceptance_required';
    end if;

    if p_outcome = 'rejected'
       and exists (
           select 1
           from public.followup_delivery_attempts attempt
           where attempt.id = p_attempt_id
             and attempt.action_id = p_action_id
             and attempt.lease_generation = p_lease_generation
             and (
                 (
                     attempt.outcome = 'delivery_unknown'
                     and attempt.reason_code = 'contact_opted_out_after_request_started'
                 )
                 or (
                     attempt.outcome = 'rejected'
                     and attempt.reason_code = 'contact_opted_out_not_applied'
                 )
             )
       ) then
        return query
        select * from public._finalize_opted_out_followup_not_applied(
            p_action_id, p_attempt_id, p_lease_generation, p_now
        );
        return;
    end if;

    for v_row in
        select * from public._finalize_followup_delivery_attempt(
            p_action_id, p_attempt_id, p_worker_id, p_lease_generation,
            p_outcome, p_remote_message_id, null, p_reason_code,
            p_next_attempt_at, p_reconciliation_deadline, p_now
        )
    loop
        return next v_row;
    end loop;

    -- close_recovery_case_without_pending_touch: si el toque termino sin salir
    -- y al caso no le queda otro, se cierra (E-423).
    if v_row.id is not null and v_row.status in ('permanent_failed', 'expired') then
        perform public._close_recovery_case_without_pending_touch_v1(
            v_row.id, 'finalize', p_now
        );
    end if;
end;
$finalize$;

commit;
