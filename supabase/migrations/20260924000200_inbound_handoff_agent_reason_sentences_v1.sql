-- Migration: las frases de los motivos que realmente emite el agente.
--
-- Que problema resuelve. 20260924000100 hizo que el motivo de una derivacion
-- llegue a la fila y a la nota privada de Chatwoot, con un mapa de codigo a
-- frase en castellano. Ese mapa quedo mal calibrado: cubre los motivos que
-- compone el worker cuando falla la emision del enlace (payment_link_*) y no
-- cubre ninguno de los tres que emite el agente, que son los que mas van a
-- aparecer.
--
-- Medido sobre el SOUL que corre en produccion (/opt/data/profiles/
-- agente-comercial/SOUL.md, md5 4c4425f10f21ea4a5c043a6b7adbfb5e, verificado
-- contra el de git: la lista es identica en los dos):
--
--   "Con decision="handoff", elegi un solo reason_code:
--    explicit_human_request si la persona pide hablar con alguien;
--    commercial_exception para excepciones comerciales; o
--    policy_requires_human para complejidad, contradiccion, falta de
--    informacion, revision particular o cualquier otra condicion restrictiva."
--
-- Ninguno de los tres estaba en el mapa, asi que una derivacion pedida por el
-- agente mostraba el codigo crudo ("Motivo: commercial_exception.") en vez de
-- una frase. El fallback funcionaba, pero se llevaba el caso frecuente.
--
-- Ademas se agrega payment_link_requested. Con el arreglo que acompana a esta
-- migracion en app.py ya no deberia llegar, pero es el motivo que el agente usa
-- al pedir el enlace (SOUL linea 121) y hasta ahora podia colarse cuando una
-- regla del worker reescribia decision sin tocar reason_code.
--
-- Que NO cambia: la firma, los privilegios (ninguno de los tres roles de la API
-- puede ejecutarla; la resuelve la RPC, que es security definer) y el contrato
-- de un codigo desconocido, que se sigue mostrando crudo.

begin;

create or replace function public.inbound_handoff_reason_sentence(
    p_detail_reason_code text
)
returns text
language sql
immutable
set search_path = pg_catalog, public, pg_temp
as $function$
    select case p_detail_reason_code
        -- Los tres que elige el agente al pedir la derivacion.
        when 'explicit_human_request' then
            'la persona pidio hablar con alguien del equipo'
        when 'commercial_exception' then
            'la persona pidio una condicion comercial que el agente no puede conceder, como un descuento, cuotas o una excepcion'
        when 'policy_requires_human' then
            'el caso es complejo, contradictorio o le falta informacion, y la politica pide que lo revise una persona'
        when 'payment_link_requested' then
            'la persona pidio el enlace de pago y la conversacion se derivo antes de enviarlo'
        -- El que fuerza el worker cuando el mensaje pide indicaciones clinicas.
        when 'direct_medication_guidance' then
            'el contacto pidio indicaciones sobre medicacion y eso lo responde una persona'
        -- Los que compone el worker cuando la emision del enlace devuelve blocked.
        when 'payment_link_purchase_already_approved' then
            'el contacto ya compro este producto, asi que no se le envio un enlace de pago nuevo'
        when 'payment_link_disabled' then
            'el envio automatico de enlaces de pago esta apagado por configuracion'
        when 'payment_link_reply_rejected' then
            'el texto con el enlace de pago no paso la validacion de formato'
        when 'payment_link_builder_rejected' then
            'no se pudo construir un enlace de pago valido para este contacto'
        when 'payment_link_blocked_case' then
            'el caso comercial no estaba activo al intentar emitir el enlace'
        when 'payment_link_blocked_conversation' then
            'la conversacion ya estaba pausada o intervenida al intentar emitir el enlace'
        when 'payment_link_blocked_contact' then
            'el contacto esta bloqueado, dado de baja o marcado como no contactar'
        when 'payment_link_blocked_opt_out' then
            'el contacto pidio no recibir mas mensajes'
        when 'payment_link_blocked_identity' then
            'no se pudo confirmar que el numero de la conversacion sea el del caso'
        when 'payment_link_blocked_scope' then
            'la configuracion comercial del producto no esta publicada'
        when 'payment_link_missing_default_offer' then
            'el producto no tiene una oferta activa por defecto para responder por aqui'
        when 'payment_link_replay_conflict' then
            'ya existia un enlace emitido para este mensaje con otros datos'
        when 'payment_link_invalid_request' then
            'los datos de la conversacion no permitieron emitir el enlace'
        when 'payment_link_chatwoot_blocked' then
            'Chatwoot rechazo el envio del mensaje con el enlace'
        when 'payment_link_delivery_unknown' then
            'no se pudo confirmar si el enlace llego, asi que no se reintento solo'
        else null
    end;
$function$;

-- Idempotente y explicito: el stack de pruebas aplica default privileges
-- hostiles, y repetir los revokes cuesta nada.
revoke all on function public.inbound_handoff_reason_sentence(text) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public.inbound_handoff_reason_sentence(text)
        from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public.inbound_handoff_reason_sentence(text)
        from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on function public.inbound_handoff_reason_sentence(text)
        from service_role;
    end if;
end;
$roles$;

commit;
