-- Migration: quien se dio de baja y escribe sola recibe el link si lo pide
-- (F-036, decisiones D9 y D10 de Dan del 2026-10-24).
--
-- Por que. La baja (20260809000100) frena todo lo que sale de nosotros: deja
-- el contacto en contact_permission = 'opted_out' y lifecycle_status =
-- 'do_not_contact', y guarda el evento en contact_opt_out_events. La reserva y
-- la autorizacion del link del agente miran las dos cosas y devuelven
-- blocked_contact o blocked_opt_out. Desde F-036, cuando la persona dada de
-- baja escribe por su cuenta, el agente le contesta (el bridge, con manifiesto),
-- y si pide el link lo recibe: contestar dentro de la ventana de 24 h a quien
-- escribio es atencion, no un envio nuestro. Sin este cambio, el «Envíame el
-- enlace» de la conversacion 257 de ATT1 (17/10) seguiria terminando en una
-- derivacion por payment_link_blocked_contact.
--
-- Que cambia. Un helper nuevo, _chatwoot_reply_follows_opt_out_v1, y cuatro
-- funciones reemplazadas con reemplazos contados sobre la definicion viva:
--   - reserve_chatwoot_checkout_issuance_v2 (la compartida),
--   - reserve_portable_checkout_issuance_v2 (la del runtime con manifiesto),
--   - reserve_portable_checkout_issuance_for_funnel_v1 (la de varios productos),
--   - authorize_chatwoot_checkout_issuance_v2 (la autorizacion, compartida).
-- En las cuatro, la baja deja de frenar el link solo si el mensaje que lo
-- dispara (p_trigger_external_message_id, el id de Chatwoot) es POSTERIOR a
-- toda baja registrada para esa persona en esa cuenta y ese inbox: por su
-- conversacion, por cualquiera de las dos formas de su movil o por su contacto.
-- Los ids de mensaje de Chatwoot son una secuencia de la instalacion, asi que
-- un id mayor es un mensaje escrito despues. Lo demas queda igual:
--   - contact_permission 'blocked' o 'restricted' sigue frenando siempre;
--   - do_not_contact sin una baja registrada (puesto a mano) sigue frenando;
--   - la conversacion donde se pidio la baja sigue 'blocked' (la puso la
--     baja), asi que alli el link sigue sin salir;
--   - una baja nueva, posterior al mensaje, vuelve a frenar.
--
-- Por que no frena el seguimiento ni la reactivacion. claim_conversation_
-- followup_v1, su portable y claim_conversation_reactivation miran
-- contact_permission antes de reservar y devuelven blocked_contact; no cambian.
-- El seguimiento ademas reserva anclado en NUESTRO ultimo mensaje, nunca en uno
-- de la persona. Los toques programados los sigue frenando la baja en la base
-- (scheduled_actions y followup_sequences cancelados al registrarla).
--
-- Los reemplazos dejan intacto el texto que validate_whatsapp_phone_equivalence
-- usa para derivar la portable de la compartida: el prefijo nuevo va igual en
-- las dos, antes de has_chatwoot_opt_out_stop(...) en la compartida y antes del
-- exists (...) de las dos formas en la portable.

begin;

create or replace function public._chatwoot_reply_follows_opt_out_v1(
    p_contact_id uuid,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_external_user_id text,
    p_trigger_external_message_id text
)
returns boolean
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_trigger bigint;
    v_user_ids text[];
begin
    if p_trigger_external_message_id is null
       or p_trigger_external_message_id !~ '^[1-9][0-9]{0,17}$'
       or p_chatwoot_account_id is null
       or p_chatwoot_inbox_id is null then
        return false;
    end if;
    v_trigger := p_trigger_external_message_id::bigint;
    v_user_ids := coalesce(
        public._whatsapp_phone_variants(p_external_user_id), array[]::text[]
    );
    if p_external_user_id is not null then
        v_user_ids := v_user_ids || p_external_user_id;
    end if;

    -- Las mismas bajas que frenan: las de esta conversacion, las de cualquiera
    -- de las formas del movil y las del contacto. Hace falta al menos una, y
    -- todas tienen que ser anteriores al mensaje que pide el link.
    return exists (
        select 1
        from public.contact_opt_out_events optout
        where optout.source = 'chatwoot'
          and optout.canonical_account_id = p_chatwoot_account_id
          and optout.canonical_inbox_id = p_chatwoot_inbox_id
          and (
              optout.canonical_conversation_id = p_chatwoot_conversation_id
              or optout.external_user_id = any(v_user_ids)
              or (p_contact_id is not null and optout.contact_id = p_contact_id)
          )
    )
    and not exists (
        select 1
        from public.contact_opt_out_events optout
        where optout.source = 'chatwoot'
          and optout.canonical_account_id = p_chatwoot_account_id
          and optout.canonical_inbox_id = p_chatwoot_inbox_id
          and (
              optout.canonical_conversation_id = p_chatwoot_conversation_id
              or optout.external_user_id = any(v_user_ids)
              or (p_contact_id is not null and optout.contact_id = p_contact_id)
          )
          and optout.canonical_message_id >= v_trigger
    );
end;
$function$;

do $opted_out_reply$
declare
    v_signature text;
    v_definition text;
    v_contact_old constant text :=
        E'       or v_contact.contact_permission in (''opted_out'', ''blocked'', ''restricted'')\n'
        || E'       or v_contact.lifecycle_status = ''do_not_contact'' then\n';
    v_contact_new constant text :=
        E'       or v_contact.contact_permission in (''blocked'', ''restricted'')\n'
        || E'       or (\n'
        || E'           (v_contact.contact_permission = ''opted_out''\n'
        || E'            or v_contact.lifecycle_status = ''do_not_contact'')\n'
        || E'           -- F-036: la baja no frena el link que pide quien escribio despues.\n'
        || E'           and not (\n'
        || E'               v_contact.contact_permission = ''opted_out''\n'
        || E'               and public._chatwoot_reply_follows_opt_out_v1(\n'
        || E'                   v_contact.id, p_chatwoot_account_id, p_chatwoot_inbox_id,\n'
        || E'                   p_chatwoot_conversation_id, p_external_user_id,\n'
        || E'                   p_trigger_external_message_id\n'
        || E'               )\n'
        || E'           )\n'
        || E'       ) then\n';
    v_reply_guard constant text :=
        E'    if not public._chatwoot_reply_follows_opt_out_v1(\n'
        || E'        v_contact.id, p_chatwoot_account_id, p_chatwoot_inbox_id,\n'
        || E'        p_chatwoot_conversation_id, p_external_user_id,\n'
        || E'        p_trigger_external_message_id\n'
        || E'    ) and ';
    v_exact_stop_old constant text :=
        E'    if public.has_chatwoot_opt_out_stop(\n';
    v_variant_stop_old constant text :=
        E'    if exists (\n'
        || E'        select 1\n'
        || E'        from unnest(public._whatsapp_phone_variants(p_external_user_id))\n'
        || E'            as opt_out_form(user_id)\n';
    v_stop_old text;
begin
    foreach v_signature in array array[
        'public.reserve_chatwoot_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamptz)',
        'public.reserve_portable_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamptz)',
        'public.reserve_portable_checkout_issuance_for_funnel_v1(uuid,text,bigint,bigint,bigint,text,text,text,timestamptz)',
        'public.authorize_chatwoot_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,timestamptz)'
    ] loop
        select pg_get_functiondef(v_signature::regprocedure) into v_definition;
        v_stop_old := case
            when position('_whatsapp_phone_variants(p_external_user_id))' in v_definition) > 0
                then v_variant_stop_old
            else v_exact_stop_old
        end;
        if v_definition is null
           or length(v_definition) - length(replace(v_definition, v_contact_old, ''))
              <> length(v_contact_old)
           or length(v_definition) - length(replace(v_definition, v_stop_old, ''))
              <> length(v_stop_old)
           or position('_chatwoot_reply_follows_opt_out_v1' in v_definition) > 0 then
            raise exception using errcode = '55000',
                message = 'unexpected_checkout_issuance_opt_out_definition',
                detail = v_signature;
        end if;
        v_definition := replace(v_definition, v_contact_old, v_contact_new);
        v_definition := replace(
            v_definition,
            v_stop_old,
            v_reply_guard || substr(v_stop_old, length('    if ') + 1)
        );
        execute v_definition;
        if (
            select length(definition) - length(replace(
                definition, '_chatwoot_reply_follows_opt_out_v1(', ''
            ))
            from (select pg_get_functiondef(v_signature::regprocedure) as definition) live
        ) <> 2 * length('_chatwoot_reply_follows_opt_out_v1(') then
            raise exception using errcode = '55000',
                message = 'checkout_issuance_opt_out_reply_not_applied',
                detail = v_signature;
        end if;
    end loop;
end;
$opted_out_reply$;

-- El helper es interno: lo llaman las cuatro funciones de arriba, que son
-- security definer. Ningun rol de la API lo ejecuta, tampoco service_role.
do $roles$
declare
    v_signature constant text :=
        'public._chatwoot_reply_follows_opt_out_v1(uuid,bigint,bigint,bigint,text,text)';
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
            message = 'opted_out_reply_helper_acl_unexpected',
            detail = format('ningun rol de la API ejecuta %s', v_signature);
    end if;
end
$roles$;

commit;
