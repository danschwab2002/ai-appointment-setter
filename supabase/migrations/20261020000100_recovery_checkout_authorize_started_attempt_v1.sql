-- Migration: la autorizacion del link de recuperacion acepta el intento ya
-- arrancado.
--
-- Por que. El 2026-10-20 a las 02:07Z, en ATT1, el primer mensaje real con
-- link de recuperacion (B1 del pago rechazado) no salio: el intento termino
-- delivery_unknown con reason_code = recovery_checkout_blocked_attempt, la
-- emision del link quedo en reserved y no se mando nada. El bridge
-- (src/bridge/worker.py, _authorize_recovery_issuance) llama a
-- authorize_recovery_checkout_issuance_v1 a proposito DESPUES de marcar el
-- inicio del pedido (mark_followup_request_started y sus variantes del piloto,
-- del pago rechazado y de la ficha): si autorizara antes y el inicio fallara,
-- el link volveria como replay en el lease siguiente y cerraria el toque sin
-- mandarlo. Pero la funcion de 20261018000200 exigia el intento en
-- phase = 'reserved', y el inicio lo deja en 'request_started': la autorizacion
-- devolvia blocked_attempt siempre. Le pasaba a todo mensaje con link de
-- recuperacion (A1 con cupon, B1 con link, Z del carrito, B2 o Z del pago
-- rechazado). El validador de 20261018000200 autorizaba con el intento todavia
-- en reserved y nunca recorrio el orden real; los tests de Python del bridge
-- simulan el HTTP.
--
-- Que cambia. Una sola funcion, authorize_recovery_checkout_issuance_v1: el
-- cerco del intento acepta phase in ('reserved', 'request_started') en vez de
-- solo 'reserved'. Se mantiene igual:
--   - el cerco del lease: el intento tiene que ser el del lease actual
--     (lease_generation = p_lease_generation), ademas del lease de la accion
--     que ya se revisa antes. Un intento de un lease anterior sigue dando
--     blocked_attempt;
--   - un intento completed (aceptado, rechazado, fallido antes del pedido o
--     delivery_unknown) sigue dando blocked_attempt;
--   - todos los frenos que vienen despues (caso activo, contacto sin baja ni
--     bloqueo, identidad, baja de Chatwoot, intencion esperando compra) y la
--     transicion de la emision reserved -> request_started.
-- La autorizacion con el intento todavia en reserved sigue funcionando
-- (compatibilidad con cualquier llamador que autorice antes del inicio).
--
-- Por que no se exige ademas request_started_at is not null (ni outcome is
-- null) cuando la fase es request_started: ya lo garantiza el CHECK de
-- followup_delivery_attempts (20260803000100: phase = 'request_started' and
-- request_started_at is not null and outcome is null). Repetirlo seria una
-- rama que nunca puede tomarse. Lo que da garantia aca es el cerco del lease:
-- el intento arrancado es el del lease que tiene el worker que autoriza.
--
-- Como. Igual que 20261019000800: la funcion se redefine desde su definicion
-- viva (pg_get_functiondef), reemplazando un texto exacto que tiene que
-- aparecer una sola vez, el de 20261018000200. Si otra migracion lo cambio,
-- aborta con 55000 sin pisarlo; si ya corrio (la marca
-- recovery_checkout_authorize_started_attempt), aborta igual. Firma, dueno,
-- security definer, search_path y permisos sin cambios (los permisos se
-- verifican al final: solo service_role).
--
-- Que NO hace: no toca el bridge ni el orden de las llamadas, ni la reserva
-- del link (reserve_recovery_checkout_issuance_v1), ni el cierre
-- (finalize_chatwoot_checkout_issuance_v2). No reabre los intentos que ya
-- quedaron delivery_unknown con recovery_checkout_blocked_attempt ni las
-- emisiones que quedaron en reserved: esas filas se reconcilian aparte.

begin;

do $authorize_started$
declare
    c_signature constant text :=
        'public.authorize_recovery_checkout_issuance_v1(uuid,uuid,text,bigint,timestamptz)';
    c_marker constant text := 'recovery_checkout_authorize_started_attempt';
    c_old constant text := $old$    if v_attempt.lease_generation <> p_lease_generation
       or v_attempt.phase <> 'reserved' then
        return query select 'blocked_attempt'::text, v_issuance.id, v_issuance.status;
$old$;
    c_new constant text := $new$    -- recovery_checkout_authorize_started_attempt (20261020000100): el bridge
    -- autoriza despues de marcar el inicio del pedido, con el intento ya en
    -- request_started; el cerco del lease del intento se mantiene.
    if v_attempt.lease_generation <> p_lease_generation
       or v_attempt.phase not in ('reserved', 'request_started') then
        return query select 'blocked_attempt'::text, v_issuance.id, v_issuance.status;
$new$;
    v_definition text;
    v_found integer;
begin
    if to_regprocedure(c_signature) is null then
        raise exception using errcode = '55000',
            message = 'recovery_checkout_authorize_started_function_missing',
            detail = c_signature;
    end if;
    select pg_get_functiondef(c_signature::regprocedure) into v_definition;
    if position(c_marker in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'recovery_checkout_authorize_started_already_applied',
            detail = c_signature;
    end if;
    v_found := (length(v_definition) - length(replace(v_definition, c_old, '')))
               / length(c_old);
    if v_found <> 1 then
        raise exception using errcode = '55000',
            message = 'unexpected_recovery_checkout_authorize_definition',
            detail = format(
                '%s: el cerco del intento de 20261018000200 aparece %s veces, se esperaba 1',
                c_signature, v_found
            );
    end if;
    execute replace(v_definition, c_old, c_new);
    select pg_get_functiondef(c_signature::regprocedure) into v_definition;
    if position(c_marker in v_definition) = 0
       or position(c_old in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'recovery_checkout_authorize_started_not_applied',
            detail = c_signature;
    end if;
    -- Los permisos que create or replace conserva: solo service_role.
    if exists (
           select 1 from pg_roles
           where rolname = 'service_role'
             and not has_function_privilege(rolname, c_signature, 'EXECUTE')
       )
       or exists (
           select 1 from pg_roles
           where rolname in ('anon', 'authenticated')
             and has_function_privilege(rolname, c_signature, 'EXECUTE')
       ) then
        raise exception using errcode = '55000',
            message = 'recovery_checkout_authorize_started_grants_changed',
            detail = c_signature;
    end if;
end;
$authorize_started$;

commit;
