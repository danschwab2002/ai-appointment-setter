-- Migration: los recordatorios del vencimiento de la ficha (C2 y C3 de los
-- flujos comerciales de ATT1) arrancan por las RPC de la ficha.
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
-- Que cambia: las dos funciones aceptan, ademas de la accion payment_slip de
-- C1, un no_reply_review cuya secuencia es de una ficha
-- (followup_sequences.reason = 'payment_slip', la que escribe
-- plan_payment_slip_recovery). Todo lo demas queda igual: el cerco de la lease,
-- la ficha mas reciente del caso, la autorizacion contra el scope del caso con
-- PURCHASE_BILLET_PRINTED y sus topes (que cuentan cada envio, asi que C2 y C3
-- cuentan en el tope por dia y en el total de att1-ficha).
--
-- Como. Cada funcion se redefine desde su definicion viva
-- (pg_get_functiondef), reemplazando la condicion del ancla con la cantidad
-- exacta de ocurrencias medida: si otra migracion cambio esa condicion, esta
-- aborta con 55000 en vez de pisarla. La firma, el dueno y los permisos no
-- cambian (create or replace conserva los grants: solo service_role).
--
-- Que NO hace: no agrega los pasos (son datos de la politica, los publica la
-- instancia: publicar-ficha-v2.sql de ATT1), no cambia cuando sale cada paso
-- (lo decide el bridge con el vencimiento, que solo el calcula:
-- bridge.payment_slip_reminders) ni lo que corta un paso: la reevaluacion
-- compartida sigue cancelando un no_reply_review si la persona contesto
-- (prospect_replied), y la compra de cualquier oferta del producto lo corta
-- (20261019000300).

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
    end loop;
end;
$reminders$;

commit;
