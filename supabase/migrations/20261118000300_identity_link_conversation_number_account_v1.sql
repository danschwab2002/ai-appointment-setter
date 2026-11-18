-- Migration: el numero de conversacion de los avisos del correlador encuentra
-- la identidad de WhatsApp (E-656, 2026-11-17).
--
-- Por que. _identity_link_conversation_number (20261114000100, seccion 6)
-- busca la identidad de WhatsApp del telefono con
--     identity.account_id = p_account_id::text
-- es decir '3', pero todos los que escriben channel_identities guardan la
-- cuenta como 'chatwoot:<n>':
--   - 20261001000200_portable_precheckout_first_contact.sql (v_account_id := 'chatwoot:' || ...);
--   - 20260816000200_inbound_commercial_case_draft_only.sql;
--   - 20261024000200_flow_engine_native_v1.sql;
--   - 20260805000200_followup_contact_authorization_grant.sql.
-- La funcion devuelve null siempre, y los avisos IDL-001 de tipo «decision»
-- (read_identity_link_notices_v1, definicion viva: 20261114000150) salen sin
-- numero de conversacion. Los de tipo «envio» no: leen la identidad del caso
-- directo. Encontrado por E-655 leyendo las compras de Maribel del 15/11.
--
-- Que cambia. Una sola linea de una sola funcion, con un reemplazo contado
-- sobre la definicion viva: la cuenta se compara con las dos formas,
-- 'chatwoot:<n>' (la que escribe el producto) y '<n>' (la que comparaba). El
-- canal, el telefono en sus variantes, la forma del numero y el orden no
-- cambian. read_identity_link_notices_v1 no se toca: llama a esta funcion.
--
-- Grants: el reemplazo (execute de la definicion) es un create or replace,
-- que conserva los privilegios; la funcion sigue sin execute para anon,
-- authenticated y service_role.

begin;

set local lock_timeout = '5s';

do $identity_link_conversation_number$
declare
    v_signature constant text := 'public._identity_link_conversation_number(text,bigint)';
    v_old constant text :=
        E'       and identity.account_id = p_account_id::text\n';
    v_new constant text :=
        E'       and identity.account_id in (''chatwoot:'' || p_account_id::text, p_account_id::text)\n';
    v_definition text;
begin
    if to_regprocedure(v_signature) is null then
        raise exception using errcode = '55000',
            message = 'unexpected_identity_link_conversation_number_definition',
            detail = v_signature;
    end if;
    select pg_get_functiondef(v_signature::regprocedure) into v_definition;
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_old, '')) <> length(v_old)
       or position('''chatwoot:'' || p_account_id' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_identity_link_conversation_number_definition',
            detail = v_signature;
    end if;
    execute replace(v_definition, v_old, v_new);
    if (
        select length(definition) - length(replace(definition, v_new, ''))
        from (select pg_get_functiondef(v_signature::regprocedure) as definition) live
    ) <> length(v_new) then
        raise exception using errcode = '55000',
            message = 'identity_link_conversation_number_not_applied',
            detail = v_signature;
    end if;
end;
$identity_link_conversation_number$;

commit;
