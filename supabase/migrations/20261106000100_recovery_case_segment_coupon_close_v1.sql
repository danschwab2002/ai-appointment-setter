-- recovery-case-segment-coupon-close-v1: el freno del cupon de la campana cierra
-- el caso del carrito en vez de dejarlo abierto sin accion (E-339, 2026-11-06).
-- Contrato: docs/contracts/segment-knowledge-v1.md.
--
-- Por que. Con AGENT_SEGMENTS_ENABLED, A2 y Z del carrito no salen a quien tiene
-- un segmento vigente con cupon (decision de Dan sobre D6 de E-338). Cerrar solo
-- la accion (_finalize_followup_delivery_attempt, permanent_failed) deja la
-- secuencia y el caso en active sin nada pendiente, y nada los vence. Ese caso
-- abierto frena lo que venga despues: un carrito nuevo de la misma oferta choca
-- en el legado (followup_sequences_one_active_per_case_idx, medido en
-- validate_recovery_case_segment_coupon_close.mjs), no arranca en el motor
-- (ya_en_curso, 20261022000100) ni en el nativo (caso_del_legado_en_curso,
-- 20261030000100), y cuenta en _flow_engine_legacy_in_flight y en casos_abiertos
-- del reciclado de identidades (20261102000400). Dan decidio cerrarlo.
--
-- Que suma. Una sola funcion, close_recovery_case_segment_coupon_v1, solo para
-- service_role. En una transaccion, con el intento reservado del paso:
--   1. cierra el intento y la accion por el mecanismo de hoy, con su cerco de
--      lease y su evento (failed_before_request, sin proximo intento, motivo
--      segment_coupon_active): la accion queda permanent_failed;
--   2. completa la secuencia con ese motivo;
--   3. cancela el caso, como una baja o una compra de otra oferta: no se gana y
--      no se reintenta.
-- Solo A2 y Z (cart_second_touch, cart_last_touch). Si otra cosa cerro el intento
-- o la accion (una compra entre la reevaluacion y este cierre:
-- finalize_purchase_stopped_delivery_attempts, 20260808000400) no toca nada y
-- contesta case_not_open. Repetir la llamada con el mismo intento no cambia nada.
--
-- No cambia ninguna tabla, funcion ni permiso existente. Sin esta migracion, el
-- bridge con AGENT_SEGMENTS_ENABLED apagado no la llama nunca. Con el flag
-- prendido y sin la migracion, A2 y Z tampoco salen: el intento se reintenta y
-- la accion termina cerrada con el caso abierto.

begin;

do $guard$
begin
    if to_regprocedure(
        'public.close_recovery_case_segment_coupon_v1(uuid,uuid,text,bigint,timestamptz)'
    ) is not null then
        raise exception using errcode = '55000',
            message = 'recovery_case_segment_coupon_close_already_applied';
    end if;
end;
$guard$;

create function public.close_recovery_case_segment_coupon_v1(
    p_action_id uuid,
    p_attempt_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_now timestamptz
)
returns table (outcome text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    -- recovery_case_segment_coupon_close: el motivo vive solo aca y en el bridge.
    c_reason constant text := 'segment_coupon_active';
    v_action public.scheduled_actions%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_closed integer;
begin
    select action_row.* into v_action
      from public.scheduled_actions action_row
     where action_row.id = p_action_id;
    if not found then
        raise exception using errcode = 'P0002', message = 'scheduled_action_not_found';
    end if;
    if v_action.action_type <> 'no_reply_review'
       or v_action.step_key is null
       or v_action.step_key not in ('cart_second_touch', 'cart_last_touch') then
        raise exception using errcode = '22023', message = 'segment_coupon_close_step_invalid';
    end if;

    -- Los candados, en el orden de _finalize_followup_delivery_attempt.
    perform 1 from public.recovery_cases case_row
     where case_row.id = v_action.recovery_case_id for update;
    perform 1 from public.followup_sequences sequence_row
     where sequence_row.id = v_action.followup_sequence_id for update;
    select action_row.* into strict v_action
      from public.scheduled_actions action_row
     where action_row.id = p_action_id for update;
    select attempt_row.* into v_attempt
      from public.followup_delivery_attempts attempt_row
     where attempt_row.id = p_attempt_id
       and attempt_row.action_id = p_action_id
       and attempt_row.lease_generation = p_lease_generation
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'delivery_attempt_not_found';
    end if;

    -- Otra cosa ya cerro el intento o la accion: una compra lo hace con su motivo,
    -- y finalizar encima daria delivery_attempt_already_finalized_differently.
    if (v_attempt.phase = 'completed' and v_attempt.reason_code is distinct from c_reason)
       or (v_attempt.phase <> 'completed'
           and v_action.status not in ('pending', 'deferred', 'retryable_failed')) then
        outcome := 'case_not_open';
        return next;
        return;
    end if;

    -- Exige el lease vigente. Si el intento ya cerro igual, no hace nada.
    perform 1 from public._finalize_followup_delivery_attempt(
        p_action_id, p_attempt_id, p_worker_id, p_lease_generation,
        'failed_before_request', null, null, c_reason, null, null, p_now
    );

    update public.followup_sequences
       set status = 'completed', completion_reason = c_reason,
           completed_at = p_now, revision = revision + 1
     where id = v_action.followup_sequence_id and status = 'active';

    update public.recovery_cases
       set status = 'cancelled', closed_at = p_now, version = version + 1
     where id = v_action.recovery_case_id and status in ('grace_period', 'active');
    get diagnostics v_closed = row_count;

    outcome := case when v_closed = 1 then 'closed' else 'case_not_open' end;
    return next;
end;
$function$;

do $acl$
declare
    v_signature text := 'public.close_recovery_case_segment_coupon_v1(uuid,uuid,text,bigint,timestamptz)';
    v_role text;
begin
    execute format('revoke all on function %s from public', v_signature);
    foreach v_role in array array['anon', 'authenticated'] loop
        if to_regrole(v_role) is not null then
            execute format('revoke all on function %s from %I', v_signature, v_role);
        end if;
    end loop;
    if to_regrole('service_role') is not null then
        execute format('grant execute on function %s to service_role', v_signature);
    end if;
    if exists (
           select 1 from pg_roles
            where rolname = 'service_role'
              and not has_function_privilege(rolname, v_signature, 'EXECUTE')
       )
       or exists (
           select 1 from pg_roles
            where rolname in ('anon', 'authenticated')
              and has_function_privilege(rolname, v_signature, 'EXECUTE')
       ) then
        raise exception using errcode = '55000',
            message = 'recovery_case_segment_coupon_close_acl_unexpected',
            detail = format('solo service_role ejecuta %s', v_signature);
    end if;
end;
$acl$;

commit;
