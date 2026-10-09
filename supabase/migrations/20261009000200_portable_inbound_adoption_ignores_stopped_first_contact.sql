-- La adopcion de la respuesta a una plantilla del piloto (H7, 20261001000400)
-- no se frena por un primer contacto que ya no va a salir.
--
-- El freno del paso pendiente (decision 6 de 20261001000400) no adopta la
-- conversacion de la plantilla mientras algun caso de recuperacion de la
-- persona (el de esa conversacion o uno todavia sin conversacion) tenga una
-- accion en pending, deferred, retryable_failed o delivery_unknown: el agente
-- no toma una conversacion en la que todavia va a salir otra plantilla.
--
-- El primer contacto (20261001000200) nace con su accion en pending hasta la
-- hora en que le toca salir: 60 minutos despues del formulario en ATT1. Si
-- antes llega el carrito o el pago fallido de esa intencion, o la persona
-- compra, el primer contacto ya no va a salir: la reevaluacion
-- (reevaluate_portable_precheckout_action) lo cancela con el motivo de
-- _portable_precheckout_stop_reason, pero recien cuando el despachador lo
-- toma, a su hora. En ese rato la accion sigue en pending y frena la
-- adopcion: quien contesta la plantilla del carrito o del pago fallido no
-- llega al agente, y el bridge, que trata el 22000 como definitivo, deja el
-- mensaje failed despues de 8 intentos (unos 5 minutos).
--
-- Medido en ATT1 el 2026-10-09, el dia que se armo el primer contacto: el
-- carrito llega entre 41 y 52 minutos despues del formulario, asi que la
-- ventana es de unos 18 minutos despues de cada plantilla del carrito, y de
-- casi una hora para el pago fallido. A las 16:15:55Z una persona toco
-- «Envíame el enlace» en la plantilla del carrito; la admision portable dio
-- 22000 inbound_canonical_conversation_conflict en los 8 intentos del bridge
-- (16:16:28Z a 16:25:12Z) y el mensaje quedo failed. Su primer contacto,
-- que la frenaba, se cancelo a las 16:27:23Z (superseded_by_provider_event).
-- El agente no le contesto.
--
-- El cambio: el freno del paso pendiente deja de contar una accion del primer
-- contacto (anchor_type = 'precheckout_intent') en pending, deferred o
-- retryable_failed cuya intencion (la audiencia de su binding) ya tiene un
-- freno que no vuelve atras:
--   - la clasificacion del carrito o del pago fallido (confirmed_abandonment,
--     payment_failure_supported), que pone la correlacion de Hotmart;
--   - una compra: intent_purchased, purchase_by_identity o
--     intent_purchase_ambiguous de _portable_precheckout_stop_reason.
-- Con cualquiera de esos, _portable_precheckout_stop_reason da un motivo y la
-- reevaluacion cancela la accion: no hay camino a execute. Siguen frenando
-- como hoy: el primer contacto en delivery_unknown (puede haber salido), el
-- que todavia va a salir, el que solo frena un caso de Hotmart abierto de
-- otra intencion (ese motivo se va cuando el caso se cierra) y las acciones
-- de cualquier otro ancla. La baja ya la frena otra condicion de la adopcion.
--
-- Como se aplica. La funcion se modifica sobre su definicion VIVA
-- (pg_get_functiondef), sumando el bloque despues de un texto exacto que se
-- cuenta: si no aparece exactamente una vez, falla con 55000 y no cambia nada
-- (el metodo de 20261005000100 y 20261007000100). create or replace conserva
-- el owner, security definer, el search_path y los grants.
-- - Si el bloque ya esta exactamente como lo deja esta migracion, no cambia
--   nada: una instancia la puede aplicar antes que el resto de su cadena (ATT1
--   el 2026-10-09, sobre v1.3.3) y la cadena la vuelve a correr despues.
-- - Si la funcion no existe (una base sin 20261001000400, como la de Johanna),
--   no hay nada que arreglar: avisa y no cambia nada.
-- No toca ninguna otra funcion ni tabla y no siembra filas.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $adopcion$
declare
    v_signature constant text :=
        'public.admit_portable_inbound_commercial_case_v1(text,integer,bigint,text)';
    v_anchor constant text :=
        repeat(' ', 29) || E'and action.status in (\n'
        || repeat(' ', 33) || E'''pending'', ''deferred'', ''retryable_failed'',\n'
        || repeat(' ', 33) || E'''delivery_unknown''\n'
        || repeat(' ', 29) || E')\n';
    v_block constant text := $bloque$                             -- portable_adoption_stopped_first_contact: begin
                             -- Un primer contacto que ya no va a salir no es un
                             -- paso pendiente (20261009000200): con la
                             -- clasificacion del carrito o del pago fallido, o
                             -- con una compra, la reevaluacion lo cancela siempre.
                             and not (
                                 action.anchor_type = 'precheckout_intent'
                                 and action.status in (
                                     'pending', 'deferred', 'retryable_failed'
                                 )
                                 and exists (
                                     select 1
                                     from public.pilot_recovery_case_bindings stopped_binding
                                     join public.purchase_intents stopped_intent
                                       on stopped_intent.id
                                          = stopped_binding.audience_purchase_intent_id
                                     where stopped_binding.recovery_case_id = recovery.id
                                       and (
                                           coalesce(stopped_intent.current_classification, '') in (
                                               'confirmed_abandonment',
                                               'payment_failure_supported'
                                           )
                                           or public._portable_precheckout_stop_reason(
                                               stopped_intent.id, recovery.contact_id
                                           ) in (
                                               'intent_purchased',
                                               'intent_purchase_ambiguous',
                                               'purchase_by_identity'
                                           )
                                       )
                                 )
                             )
                             -- portable_adoption_stopped_first_contact: end
$bloque$;
    v_definition text;
    v_after text;
begin
    if to_regprocedure(v_signature) is null then
        raise notice 'portable_adoption_stopped_first_contact: % no existe en esta base (sin 20261001000400); no se cambio nada',
            v_signature;
        return;
    end if;
    v_definition := pg_get_functiondef(v_signature::regprocedure);
    if position('portable_adoption_stopped_first_contact: begin' in v_definition) > 0 then
        if length(v_definition) - length(replace(v_definition, v_anchor || v_block, ''))
               <> length(v_anchor || v_block) then
            raise exception using errcode = '55000',
                message = 'portable_adoption_stopped_first_contact: el bloque esta, pero no como lo deja esta migracion; no se cambio nada';
        end if;
        raise notice 'portable_adoption_stopped_first_contact: el bloque ya estaba; no se cambio nada';
        return;
    end if;
    if length(v_definition) - length(replace(v_definition, v_anchor, ''))
           <> length(v_anchor) then
        raise exception using errcode = '55000',
            message = 'portable_adoption_stopped_first_contact: el texto del freno del paso pendiente no aparece exactamente una vez; no se cambio nada';
    end if;
    execute replace(v_definition, v_anchor, v_anchor || v_block);
    v_after := pg_get_functiondef(v_signature::regprocedure);
    if length(v_after) - length(replace(v_after, v_anchor || v_block, ''))
           <> length(v_anchor || v_block) then
        raise exception using errcode = '55000',
            message = 'portable_adoption_stopped_first_contact: la funcion no quedo como se esperaba';
    end if;
    -- Los grants que create or replace conserva: solo service_role la ejecuta.
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
            message = 'portable_adoption_stopped_first_contact: los permisos de la funcion cambiaron';
    end if;
end
$adopcion$;

commit;
