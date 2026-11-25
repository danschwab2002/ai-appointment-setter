-- flow-engine-admission-identity-mismatch-v1: el choque de identidad en la
-- admision de un evento es un rechazo con nombre, no una falla del motor.
--
-- Por que. La admision nativa (_flow_engine_admit_native_flow, 20261024000200)
-- busca la identidad de WhatsApp del destino y, si es de OTRO contacto, aborta
-- con 23514 channel_identity_contact_mismatch. admit_flow_engine_event_v1
-- corre cada flujo en su subtransaccion y atrapa todo error como
-- error_interno:<sqlstate>, sin el mensaje: el rechazo queda como
-- error_interno:23514, que se lee y se avisa (MOT-002) como una falla del
-- motor. Medido en Maribel (25/11, lectura): 7 filas error_interno:23514 desde
-- el 23/11 en flow_engine_start_rejections (carrito, pago rechazado y
-- bienvenida de un mismo producto), las 7 con la misma forma: la persona
-- escribio primero por WhatsApp (un contacto con la identidad de canal activa,
-- sin mail ni telefono) y el evento de Hotmart es de otro contacto, el de su
-- mail. No es una falla del motor: la persona tiene dos contactos.
--
-- Que cambia. Un reemplazo contado sobre la definicion VIGENTE de
-- admit_flow_engine_event_v1, en el bloque que atrapa el error de cada flujo:
-- con 23514 y el mensaje channel_identity_contact_mismatch, el motivo es
-- no_entra:identidad_de_otro_contacto (la fila de rechazo y la fila que
-- devuelve). Cualquier otro error sigue siendo error_interno:<sqlstate>, con
-- 23514 incluido (channel_identity_not_active, channel_identity_inbox_mismatch
-- o una CHECK de verdad).
--
-- Lo que no cambia. El flujo sigue sin arrancar y la subtransaccion se deshace
-- igual que hoy (el error se atrapa en el mismo lugar). El bridge trata el
-- motivo nuevo como trataba error_interno:23514: motor_no_arranca con un
-- motivo fuera de NATIVE_CART_UNCLAIMED_REASONS, el evento queda failed con
-- flow_engine_no_start:<motivo>. Deja de avisarse como falla del motor
-- (_flow_engine_rejection_is_alertable mira error_interno:%), que es el punto.
-- La entrada del formulario (admit_and_route_portable_lead_precheckout_v1) y
-- el disparo por audiencia (_flow_engine_admit_native_audience_v1) tienen su
-- propio bloque y no cambian: en el formulario un error_interno:% cuenta como
-- sin reclamo y vuelve al legado, y un motivo nuevo ahi cambiaria ese camino.
-- Atar o reusar el contacto es del correlador y no se toca.
--
-- Lo que no hace: no crea funciones ni roles, no cambia permisos ni atributos
-- (create or replace conserva el ACL), no toca filas. Correrla dos veces
-- aborta con 55000 sin cambiar nada.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public.admit_flow_engine_event_v1(jsonb,jsonb)') is null then
        raise exception using errcode = '55000', message = 'flow_engine_admission_identity_mismatch_function_missing',
            detail = 'public.admit_flow_engine_event_v1(jsonb,jsonb)';
    end if;
    if position('flow_engine_admission_identity_mismatch'
                in pg_get_functiondef('public.admit_flow_engine_event_v1(jsonb,jsonb)'::regprocedure)) > 0 then
        raise exception using errcode = '55000', message = 'flow_engine_admission_identity_mismatch_already_applied',
            detail = 'public.admit_flow_engine_event_v1(jsonb,jsonb)';
    end if;
end;
$already$;

create function pg_temp.flow_engine_admission_identity_mismatch_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
begin
    if to_regprocedure(p_signature) is null then
        raise exception using errcode = '55000', message = 'flow_engine_admission_identity_mismatch_function_missing',
            detail = p_signature;
    end if;
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_engine_admission_identity_mismatch_owner_mismatch',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'flow_engine_admission_identity_mismatch_already_applied',
            detail = p_signature;
    end if;
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_admission_identity_mismatch_definition',
            detail = p_signature;
    end if;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_admission_identity_mismatch_not_replaced',
            detail = p_signature;
    end if;
end;
$function$;

select pg_temp.flow_engine_admission_identity_mismatch_patch(
    'public.admit_flow_engine_event_v1(jsonb,jsonb)',
    $anchor$            get stacked diagnostics v_sqlstate = returned_sqlstate;
            perform public._flow_engine_record_start_rejection(
                v_control.flow_id, v_control.active_version, v_event.id, 'admision',
                'error_interno:' || lower(v_sqlstate));
            select 'motor_no_arranca'::text as outcome, 'error_interno:' || lower(v_sqlstate) as reason,
                   null::uuid as recovery_case_id, null::uuid as followup_sequence_id,
                   null::uuid as scheduled_action_id, null::boolean as created
              into v_one;
        end;
$anchor$,
    $replacement$            get stacked diagnostics v_sqlstate = returned_sqlstate;
            -- flow_engine_admission_identity_mismatch: begin
            -- La identidad de WhatsApp del destino es de otro contacto
            -- (20261125000100): la persona tiene dos contactos, no fallo el
            -- motor. Sigue sin arrancar, con el motivo con nombre.
            if v_sqlstate = '23514' and sqlerrm = 'channel_identity_contact_mismatch' then
                perform public._flow_engine_record_start_rejection(
                    v_control.flow_id, v_control.active_version, v_event.id, 'admision',
                    'no_entra:identidad_de_otro_contacto');
                select 'motor_no_arranca'::text as outcome, 'no_entra:identidad_de_otro_contacto'::text as reason,
                       null::uuid as recovery_case_id, null::uuid as followup_sequence_id,
                       null::uuid as scheduled_action_id, null::boolean as created
                  into v_one;
            else
            -- flow_engine_admission_identity_mismatch: end
            perform public._flow_engine_record_start_rejection(
                v_control.flow_id, v_control.active_version, v_event.id, 'admision',
                'error_interno:' || lower(v_sqlstate));
            select 'motor_no_arranca'::text as outcome, 'error_interno:' || lower(v_sqlstate) as reason,
                   null::uuid as recovery_case_id, null::uuid as followup_sequence_id,
                   null::uuid as scheduled_action_id, null::boolean as created
              into v_one;
            end if;
        end;
$replacement$,
    'flow_engine_admission_identity_mismatch');

commit;
