-- Migration: al comprar, el bridge busca las conversaciones de la persona con
-- el agente para pausarlas y pasarlas al equipo (E-100, 2026-10-25; la replica
-- de «Ventas maribel» de GHL).
--
-- Por que. En GHL, el workflow «Ventas maribel» se dispara con la compra: le
-- quita el usuario asignado, le quita la etiqueta del carrito y apaga el bot
-- (relevamiento de E-093). En el producto, la compra aprobada ya cierra los
-- casos de recuperacion y corta los toques (apply_hotmart_purchase_approved),
-- pero el agente le sigue contestando como a un prospecto: un tercio de las
-- conversaciones de clientas que midio E-092 salen de mensajes de venta a
-- quien ya pago.
--
-- Por que en la base y no en Chatwoot. El comprador llega con el mail y el
-- telefono que tipeo en Hotmart (checkout_phone, sin el 1 de Mexico:
-- fixture hotmart_cash_payment_approved_att1_20261012.json), y la conversacion
-- del agente nacio del numero de WhatsApp (channel_identities.external_user_id,
-- con o sin el 1). La base ya tiene las dos puntas y las conversaciones que el
-- agente atiende; Chatwoot solo tendria el contacto, sin saber cual
-- conversacion es la del agente.
--
-- Que agrega. Una sola funcion de lectura, nueva:
-- read_purchase_agent_pause_conversations_v1(p_chatwoot_account_id,
-- p_chatwoot_inbox_id, p_inbound_scope_key, p_buyer_email, p_buyer_phone).
-- Devuelve las conversaciones de Chatwoot (hasta 5, la mas reciente primero)
-- de esa cuenta que son de la instancia (el entrante de su scope, la
-- identidad atada a su inbox o un caso de recuperacion de un scope de su
-- inbox) y cuya persona coincide con el comprador:
--   - por telefono: los ultimos 10 digitos del numero de WhatsApp de la
--     conversacion iguales a los ultimos 10 del telefono de la compra (la
--     misma regla que find_automation_expert_lead_v1: cubre 52/521 y 54/549);
--   - o por mail: el contacto de la conversacion tiene ese mail.
-- Si las conversaciones encontradas son de mas de un numero de WhatsApp (las
-- que no tienen numero, como la del carrito, no cuentan), no devuelve ninguna: una sola fila con outcome 'ambiguous'. Pausar a otra
-- persona por un mail compartido le quitaria el agente a alguien que no
-- compro. Sin coincidencias, cero filas.
--
-- Lo que no hace: no escribe nada, no crea tablas ni roles, no toca ninguna
-- funcion existente. La pausa la pone el bridge en Chatwoot (la etiqueta
-- automation_paused, el team y una nota privada). Solo service_role la
-- ejecuta.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $already$
begin
    if to_regclass('public.channel_identities') is null
       or to_regclass('public.conversations') is null
       or to_regclass('public.contact_points') is null
       or to_regclass('public.commercial_cases') is null
       or to_regclass('public.recovery_cases') is null
       or to_regclass('public.pilot_recovery_case_bindings') is null
       or to_regclass('public.pilot_scope_versions') is null then
        raise exception using errcode = '55000', message = 'purchase_agent_pause_tables_missing';
    end if;
    if to_regprocedure(
        'public.read_purchase_agent_pause_conversations_v1(bigint,bigint,text,text,text)'
    ) is not null then
        raise exception using errcode = '55000', message = 'purchase_agent_pause_already_applied';
    end if;
end;
$already$;

create function public.read_purchase_agent_pause_conversations_v1(
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_inbound_scope_key text,
    p_buyer_email text,
    p_buyer_phone text
)
returns table (
    outcome text,
    chatwoot_conversation_id bigint,
    last_activity_at timestamptz
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_email text;
    v_phone10 text;
begin
    if p_chatwoot_account_id is null or p_chatwoot_account_id <= 0
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_inbound_scope_key is null
       or p_inbound_scope_key !~ '^[a-z0-9_-]{1,100}$' then
        raise exception using errcode = '22023', message = 'invalid_purchase_agent_pause_read';
    end if;

    v_email := nullif(lower(btrim(coalesce(p_buyer_email, ''))), '');
    v_phone10 := regexp_replace(coalesce(p_buyer_phone, ''), '\D', '', 'g');
    v_phone10 := case when length(v_phone10) >= 10 then right(v_phone10, 10) end;
    if v_email is null and v_phone10 is null then
        return;
    end if;

    return query
    with matches as (
        select distinct on (identity.external_conversation_id)
               identity.external_conversation_id::bigint as number,
               case when length(regexp_replace(coalesce(identity.external_user_id, ''), '\D', '', 'g')) >= 10
                    then right(regexp_replace(identity.external_user_id, '\D', '', 'g'), 10)
               end as phone10,
               conversation.updated_at as activity_at
          from public.conversations conversation
          join public.channel_identities identity
            on identity.id = conversation.channel_identity_id
         where identity.channel = 'whatsapp'
           and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
           and identity.external_conversation_id ~ '^[1-9][0-9]{0,17}$'
           and (
                identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
                or exists (
                    select 1 from public.commercial_cases commercial
                     where commercial.conversation_id = conversation.id
                       and commercial.inbound_scope_key = p_inbound_scope_key)
                or exists (
                    select 1
                      from public.recovery_cases rc
                      join public.pilot_recovery_case_bindings binding
                        on binding.recovery_case_id = rc.id
                      join public.pilot_scope_versions scope
                        on scope.scope_key = binding.scope_key
                       and scope.version = binding.scope_version
                     where rc.conversation_id = conversation.id
                       and scope.chatwoot_inbox_id = p_chatwoot_inbox_id)
           )
           and (
                (v_phone10 is not null
                 and length(regexp_replace(coalesce(identity.external_user_id, ''), '\D', '', 'g')) >= 10
                 and right(regexp_replace(identity.external_user_id, '\D', '', 'g'), 10) = v_phone10)
                or (v_email is not null and exists (
                    select 1 from public.contact_points point
                     where point.contact_id = conversation.contact_id
                       and point.type = 'email'
                       and lower(btrim(point.normalized_value)) = v_email))
           )
         order by identity.external_conversation_id, conversation.updated_at desc
    ),
    -- Las conversaciones sin numero (la del carrito, atada al contacto de
    -- Hotmart) no cuentan para la ambiguedad: no son de otra persona.
    phones as (
        select count(distinct matches.phone10) as total from matches
    )
    select 'ambiguous'::text, null::bigint, null::timestamptz
      from phones
     where phones.total > 1
    union all
    (select 'found'::text, ranked.number, ranked.activity_at
       from matches ranked, phones
      where phones.total <= 1
      order by ranked.activity_at desc nulls last, ranked.number desc
      limit 5);
end;
$function$;

revoke all on function public.read_purchase_agent_pause_conversations_v1(bigint, bigint, text, text, text) from public;

do $roles$
declare
    v_role text;
begin
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format(
                'revoke all on function public.read_purchase_agent_pause_conversations_v1(bigint, bigint, text, text, text) from %I',
                v_role
            );
        end if;
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.read_purchase_agent_pause_conversations_v1(bigint, bigint, text, text, text) to service_role;
    end if;
end;
$roles$;

commit;
