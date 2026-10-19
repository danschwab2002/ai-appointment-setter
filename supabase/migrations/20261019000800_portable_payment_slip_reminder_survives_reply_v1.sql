-- Migration: el recordatorio del vencimiento de la ficha no se cancela porque
-- la persona conteste.
--
-- Por que. Los recordatorios de la ficha (20261019000400, C2 y C3) se
-- cancelaban si la persona escribia despues del toque anterior: la
-- reevaluacion compartida (reevaluate_followup_action) decide cancel con
-- prospect_replied ante p_chatwoot_inbound_after_anchor. La especificacion de
-- los flujos comerciales de ATT1 (2b, 2026-10-17) pide lo contrario: el
-- recordatorio es utilidad sobre un pago que la persona genero, y sigue hasta
-- la baja o la compra. Y C1 termina con una pregunta («¿Que dia te queda bien
-- pasar a pagarla?»), que invita a contestar: con el corte, quien mas
-- interes mostro se quedaba sin el «vence hoy». La unica venta en efectivo
-- recuperada por el link del recuperador (2026-10-17) fue de alguien que
-- contesto. En ATT1 la ficha va con dos toques (C1 y C3, decision de Dan del
-- 2026-10-19), asi que lo que cambia es C3.
--
-- Que cambia. Una sola funcion, reevaluate_followup_action: la rama de
-- prospect_replied no aplica a un recordatorio de la ficha (un
-- no_reply_review cuya secuencia es de una ficha, followup_sequences.reason =
-- 'payment_slip', el mismo predicado que 20261019000400). Todo lo demas lo
-- sigue frenando como antes:
--   - la baja (el boton «No mas mensajes» o una frase de baja: el contacto
--     queda opted_out, contact_blocked) y la compra de cualquier oferta
--     (20261019000300), que van antes en la misma cadena;
--   - una persona del equipo que escribe despues del toque
--     (p_chatwoot_human_activity_after_anchor; los mensajes del agente salen
--     como AgentBot y no cuentan), la toma de la conversacion
--     (human_takeover), la automatizacion pausada y la conversacion en espera
--     (snoozed): pausan.
-- La respuesta la sigue tomando el agente (la adopcion de 20261019000400 no
-- cuenta el recordatorio como pendiente), y el recordatorio sale igual el dia
-- que le toca, en la misma conversacion. Ningun otro seguimiento cambia: un
-- no_reply_review de otra secuencia sigue cancelandose con prospect_replied.
--
-- Como. Igual que 20261019000400: la funcion se redefine desde su definicion
-- viva (pg_get_functiondef), reemplazando un texto exacto que tiene que
-- aparecer una sola vez, el de 20261019000600 (la renovacion del carrito). Si
-- otra migracion lo cambio, aborta con 55000 sin pisarlo; si ya corrio (la
-- marca portable_payment_slip_reminder_survives_reply), aborta igual. Firma,
-- dueno, security definer o invoker, search_path y permisos sin cambios (los
-- permisos se verifican al final).
--
-- Que NO hace: no toca las politicas ni los scopes (la de ATT1 sigue igual) ni
-- el bridge. Un «no» escrito que no sea una frase de baja no corta el
-- recordatorio.

begin;

do $survives_reply$
declare
    c_signature constant text :=
        'public.reevaluate_followup_action(uuid,text,bigint,timestamptz,boolean,text,text,timestamptz,text,boolean,boolean,boolean,boolean,boolean)';
    c_marker constant text := 'portable_payment_slip_reminder_survives_reply';
    c_old constant text := $old$       and not public._recovery_renewal_requested(v_action.id) then
        v_decision := 'cancel';
        v_reason := 'prospect_replied';
$old$;
    c_new constant text := $new$       and not public._recovery_renewal_requested(v_action.id)
       -- portable_payment_slip_reminder_survives_reply (20261019000800): el
       -- recordatorio de la ficha es sobre un pago que la persona genero;
       -- contestar no lo cancela (la baja y la compra si, mas arriba).
       and not (
           v_action.action_type = 'no_reply_review'
           and exists (
               select 1
               from public.followup_sequences reply_slip_sequence
               where reply_slip_sequence.id = v_action.followup_sequence_id
                 and reply_slip_sequence.reason = 'payment_slip'
           )
       ) then
        v_decision := 'cancel';
        v_reason := 'prospect_replied';
$new$;
    v_definition text;
    v_found integer;
begin
    if to_regprocedure(c_signature) is null then
        raise exception using errcode = '55000',
            message = 'payment_slip_survives_reply_function_missing',
            detail = c_signature;
    end if;
    select pg_get_functiondef(c_signature::regprocedure) into v_definition;
    if position(c_marker in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'payment_slip_survives_reply_already_applied',
            detail = c_signature;
    end if;
    v_found := (length(v_definition) - length(replace(v_definition, c_old, '')))
               / length(c_old);
    if v_found <> 1 then
        raise exception using errcode = '55000',
            message = 'unexpected_reevaluation_definition',
            detail = format(
                '%s: la rama de prospect_replied de 20261019000600 aparece %s veces, se esperaba 1',
                c_signature, v_found
            );
    end if;
    execute replace(v_definition, c_old, c_new);
    select pg_get_functiondef(c_signature::regprocedure) into v_definition;
    if position(c_marker in v_definition) = 0 then
        raise exception using errcode = '55000',
            message = 'payment_slip_survives_reply_not_applied',
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
            message = 'payment_slip_survives_reply_grants_changed',
            detail = c_signature;
    end if;
end;
$survives_reply$;

commit;
