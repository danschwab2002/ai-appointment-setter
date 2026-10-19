-- Migration: los recordatorios del vencimiento de la ficha (C2 y C3 de los
-- flujos comerciales de ATT1): las RPC de la ficha, la reevaluacion y la
-- adopcion de la respuesta los reconocen.
--
-- Por que. C1 (20261019000100) manda la ficha una hora despues de generada. La
-- especificacion (2b de «los flujos comerciales de ATT1 como tienen que
-- quedar», 2026-10-17) pide dos toques mas: el dia anterior al vencimiento (C2)
-- y el dia del vencimiento (C3). El motor ya agenda el paso siguiente de una
-- politica de varios pasos: al aceptarse un toque,
-- _finalize_followup_delivery_attempt (20260813000100) inserta un
-- no_reply_review con anchor_type = 'accepted_outbound_message', anclado al
-- mensaje aceptado. Pero las dos RPC de la ficha que usa el envio exigen
-- anchor_type = 'payment_slip':
--   - get_portable_payment_slip_send_context, con la que el bridge lee la
--     ficha para componer (payment_slip_action_required);
--   - mark_portable_payment_slip_request_started, el ultimo cerco antes del
--     envio, que autoriza contra el scope de la ficha con
--     PURCHASE_BILLET_PRINTED (pilot_request_start_rejected /
--     payment_slip_action_required).
-- Sin esta migracion, C2 no podria leer la ficha, y si arrancara por la RPC
-- compartida (mark_lancemos_pilot_request_started) se autorizaria con
-- PURCHASE_OUT_OF_SHOPPING_CART y el scope de la ficha lo rechazaria con
-- pilot_source_event_mismatch.
--
-- Que cambia. Un «recordatorio de la ficha» es un no_reply_review cuya
-- secuencia es de una ficha (followup_sequences.reason = 'payment_slip', la
-- que escribe plan_payment_slip_recovery). Cuatro funciones lo reconocen:
--   1. y 2. las dos RPC de la ficha lo aceptan ademas de la accion
--      payment_slip de C1. Todo lo demas queda igual: el cerco de la lease, la
--      ficha mas reciente del caso, la autorizacion contra el scope del caso
--      con PURCHASE_BILLET_PRINTED y sus topes (que cuentan cada envio, asi que
--      C2 y C3 cuentan en el tope por dia y en el total de att1-ficha);
--   3. reevaluate_followup_action no lo pausa por la ventana de 24 h
--      (p_chatwoot_can_reply = false) ni por una conversacion resuelta. Es una
--      plantilla aprobada: Meta la acepta fuera de la ventana, y despues de C1,
--      que es otra plantilla, la ventana esta cerrada salvo que la persona haya
--      escrito. Medido el 2026-10-19 en el inbox 11 de ATT1: de las 221
--      conversaciones de los ultimos 14 dias sin respuesta a la plantilla
--      despues de 24 h, 166 seguian abiertas y 55 las habia resuelto una
--      persona del equipo (no hay resolucion automatica), con una mediana de
--      16,7 h despues de la plantilla: limpieza del inbox, no una respuesta.
--      Sin esto C2 quedaba en pause (chatwoot_conversation_blocked) y el caso
--      abierto para siempre. Siguen frenandolo como a todo seguimiento: la
--      automatizacion pausada, la actividad humana despues del toque y la
--      conversacion en espera (snoozed); y lo cancela que la persona conteste
--      (prospect_replied);
--   4. admit_portable_inbound_commercial_case_v1 (la adopcion de la respuesta
--      a una plantilla, 20261001000400) no lo cuenta como paso pendiente: C2
--      espera su dia en retryable_failed cerca de un dia y medio, y en ese rato
--      quien contesta C1 (que termina con una pregunta) no llegaria al agente:
--      el 22000 inbound_canonical_conversation_conflict del 09/10
--      (20261009000200). Si la persona contesto, la reevaluacion cancela el
--      recordatorio cuando le toca. delivery_unknown sigue frenando.
--
-- Como. Cada funcion se redefine desde su definicion viva
-- (pg_get_functiondef), reemplazando un texto exacto que tiene que aparecer
-- una sola vez: si otra migracion lo cambio, esta aborta con 55000 en vez de
-- pisarlo. reevaluate_followup_action se toma con el parche de 20261019000300
-- (la compra de cualquier oferta), que no toca este texto. La firma, el dueno,
-- security definer o invoker, el search_path y los permisos no cambian
-- (create or replace los conserva; los permisos se verifican al final).
--
-- Que NO hace: no agrega los pasos (son datos de la politica, los publica la
-- instancia: publicar-ficha-v2.sql de ATT1) ni cambia cuando sale cada paso
-- (lo decide el bridge con el vencimiento, que solo el calcula:
-- bridge.payment_slip_reminders). Ningun seguimiento que no sea de una ficha
-- cambia: todas las condiciones nuevas exigen la secuencia payment_slip.

begin;

do $reminders$
declare
    v_spec record;
    v_definition text;
    v_derived text;
    v_found integer;
begin
    for v_spec in
        select *
        from (values
            (1,
             'public.mark_portable_payment_slip_request_started(uuid,uuid,text,bigint,timestamptz)',
             $old$          and action.anchor_type = 'payment_slip'
$old$,
             $new$          and (
              action.anchor_type = 'payment_slip'
              or (
                  action.action_type = 'no_reply_review'
                  and exists (
                      select 1
                      from public.followup_sequences slip_sequence
                      where slip_sequence.id = action.followup_sequence_id
                        and slip_sequence.reason = 'payment_slip'
                  )
              )
          )
$new$),
            (2,
             'public.get_portable_payment_slip_send_context(uuid,text,bigint,timestamptz)',
             $old$    if v_action.anchor_type is distinct from 'payment_slip' then
$old$,
             $new$    if v_action.anchor_type is distinct from 'payment_slip'
       and not (
           v_action.action_type = 'no_reply_review'
           and exists (
               select 1
               from public.followup_sequences slip_sequence
               where slip_sequence.id = v_action.followup_sequence_id
                 and slip_sequence.reason = 'payment_slip'
           )
       ) then
$new$),
            (3,
             'public.reevaluate_followup_action(uuid,text,bigint,timestamptz,boolean,text,text,timestamptz,text,boolean,boolean,boolean,boolean,boolean)',
             $old$    elsif p_chatwoot_automation_paused
       or p_chatwoot_human_activity_after_anchor
       or not p_chatwoot_can_reply
       or p_chatwoot_status in ('snoozed', 'resolved') then
$old$,
             $new$    elsif p_chatwoot_automation_paused
       or p_chatwoot_human_activity_after_anchor
       or p_chatwoot_status = 'snoozed'
       or (
           (not p_chatwoot_can_reply or p_chatwoot_status = 'resolved')
           -- portable_payment_slip_reminders (20261019000400): un recordatorio
           -- de la ficha es una plantilla aprobada; no necesita la ventana de
           -- 24 h, y una conversacion resuelta sin respuesta no lo frena.
           and not (
               v_action.action_type = 'no_reply_review'
               and exists (
                   select 1
                   from public.followup_sequences slip_sequence
                   where slip_sequence.id = v_action.followup_sequence_id
                     and slip_sequence.reason = 'payment_slip'
               )
           )
       ) then
$new$),
            (4,
             'public.admit_portable_inbound_commercial_case_v1(text,integer,bigint,text)',
             $old$                             -- portable_adoption_stopped_first_contact: end
$old$,
             $new$                             -- portable_adoption_stopped_first_contact: end
                             -- portable_payment_slip_reminders: begin
                             -- Un recordatorio de la ficha que espera su dia
                             -- (20261019000400) no frena que el agente tome la
                             -- respuesta: si la persona contesto, la
                             -- reevaluacion lo cancela (prospect_replied).
                             and not (
                                 action.action_type = 'no_reply_review'
                                 and action.status in (
                                     'pending', 'deferred', 'retryable_failed'
                                 )
                                 and exists (
                                     select 1
                                     from public.followup_sequences slip_sequence
                                     where slip_sequence.id = action.followup_sequence_id
                                       and slip_sequence.reason = 'payment_slip'
                                 )
                             )
                             -- portable_payment_slip_reminders: end
$new$)
        ) as spec(ord, signature, old_text, new_text)
        order by spec.ord
    loop
        if to_regprocedure(v_spec.signature) is null then
            raise exception using errcode = '55000',
                message = 'payment_slip_reminders_function_missing',
                detail = v_spec.signature;
        end if;
        select pg_get_functiondef(v_spec.signature::regprocedure)
          into v_definition;
        if position('slip_sequence' in v_definition) > 0 then
            raise exception using errcode = '55000',
                message = 'payment_slip_reminders_already_applied',
                detail = v_spec.signature;
        end if;
        v_found := (
            length(v_definition)
            - length(replace(v_definition, v_spec.old_text, ''))
        ) / length(v_spec.old_text);
        if v_found <> 1 then
            raise exception using errcode = '55000',
                message = 'unexpected_payment_slip_definition',
                detail = format(
                    '%s: la condicion del ancla aparece %s veces, se esperaba 1',
                    v_spec.signature, v_found
                );
        end if;
        v_derived := replace(v_definition, v_spec.old_text, v_spec.new_text);
        execute v_derived;
        select pg_get_functiondef(v_spec.signature::regprocedure)
          into v_definition;
        if position('slip_sequence' in v_definition) = 0 then
            raise exception using errcode = '55000',
                message = 'payment_slip_reminders_not_applied',
                detail = v_spec.signature;
        end if;
        -- Los permisos que create or replace conserva: solo service_role.
        if exists (
               select 1 from pg_roles
               where rolname = 'service_role'
                 and not has_function_privilege(rolname, v_spec.signature, 'EXECUTE')
           )
           or exists (
               select 1 from pg_roles
               where rolname in ('anon', 'authenticated')
                 and has_function_privilege(rolname, v_spec.signature, 'EXECUTE')
           ) then
            raise exception using errcode = '55000',
                message = 'payment_slip_reminders_grants_changed',
                detail = v_spec.signature;
        end if;
    end loop;
end;
$reminders$;

commit;
