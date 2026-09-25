-- Migration: el sck del anuncio acepta ~ (el separador del core de Lancemos
-- desde v1.10.0), en el guard de la reserva, en el reconocedor de la compra y en
-- el CHECK de forma de la URL. Decision de Dan del 2026-09-25 (E10 y E13 del
-- estandar de tracking, v0.7).
--
-- Que cambia del lado del emisor. El core de los sitios (lancemos/core#51, merge
-- b035eeb del 2026-09-25) compone el sck como
--     utm_source~utm_term~utm_content~utm_medium~utm_campaign
-- cinco campos posicionales separados por ~, cada uno saneado a [A-Za-z0-9._~-].
-- Antes eran tres campos con punto. El ~ se eligio porque Santi usa | dentro de
-- los nombres de campana (LNC | DraNina | ATT + ATT2 | LIVE), asi que la barra
-- como separador nace ambigua; y porque ~ es unreserved en RFC 3986 y viaja
-- literal en la query string, mientras | se encodea a %7C.
--
-- Que estaba mal aca. El guard de reserve_chatwoot_checkout_issuance_v2 validaba
-- el sck del anuncio contra un alfabeto sin ~ (letras, digitos, . _ | -). Con ~
-- afuera del alfabeto, cada sck nuevo caia a null, el link salia solo con el
-- marcador y la venta quedaba como marker_only. En silencio: sin error ni
-- alerta. Y el bridge exige la misma forma sobre la fila que la RPC devuelve
-- (_CHECKOUT_SAFE_SCK), asi que esta migracion se despliega junto con el bridge
-- que la acompana, el bridge primero: con la base aceptando ~ y el bridge sin
-- aceptarlo, el link directamente no sale.
--
-- Dato real (Supabase, 2026-09-25 19:14Z): 273 sck en precheckout_submissions,
-- todos del linaje fb.paid.<id> / ig.paid.<id>, 0 con ~ y 0 con |. Los sitios
-- siguen pinneados en core v1.9.0: el bump espera a que esto este en produccion.
--
-- Lo que se decide y lo que no:
--   - Este repo es un LECTOR: acepta | y ~ (y el punto de siempre). Los linajes
--     viejos (drceo con |, El Protocolo con ., la agencia con _) siguen llegando.
--   - El marcador del recuperador conserva la |: <anuncio>|hermes|v1|<ulid>. Que
--     el tramo del anuncio use ~ y el del recuperador | los hace distinguibles
--     sin ambiguedad. No se unifica sin una decision nueva.
--   - src=hermes no cambia. La firma de las dos RPC no cambia.
--
-- Se redefine desde la version vigente (20260922000200), no desde la original
-- (20260914000100): los cuerpos son identicos salvo las tres regex.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- 1. La forma de la URL emitida: el tramo del anuncio, ya URL-encodeado, admite ~
--    (que no se encodea) ademas del %7C de la barra.
alter table public.checkout_link_issuances
    drop constraint checkout_link_issuances_url_shape;

alter table public.checkout_link_issuances
    add constraint checkout_link_issuances_url_shape check (
        checkout_url_final ~ ('^https://pay[.]hotmart[.]com/[A-Za-z0-9_-]+'
            || '[?]off=[A-Za-z0-9_-]+&checkoutMode=[1-9][0-9]*&src=hermes'
            || '&sck=([A-Za-z0-9._%~-]+%7C)?hermes%7Cv1%7C'
            || '[0-7][0-9A-HJKMNP-TV-Z]{25}'
            || '(&fbclid=[A-Za-z0-9._-]+)?$')
    );

-- 2. La RPC de reserva: el guard del sck del anuncio acepta ~. Misma firma,
--    mismo nombre, mismo cuerpo salvo la regex.
create or replace function public.reserve_chatwoot_checkout_issuance_v2(
    p_commercial_case_id uuid,
    p_external_user_id text,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_trigger_external_message_id text,
    p_issuance_ulid text,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    issuance_id uuid,
    issuance_ulid text,
    purchase_intent_id uuid,
    source_kind text,
    checkout_url_final text,
    source_value text,
    sck_value text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_case public.commercial_cases%rowtype;
    v_contact public.contacts%rowtype;
    v_conversation public.conversations%rowtype;
    v_identity public.channel_identities%rowtype;
    v_scope public.inbound_commercial_scope_versions%rowtype;
    v_default_offer public.checkout_offer_catalog%rowtype;
    v_offer public.checkout_offer_catalog%rowtype;
    v_lead_intent public.purchase_intents%rowtype;
    v_intent public.purchase_intents%rowtype;
    v_submission public.precheckout_submissions%rowtype;
    v_issuance public.checkout_link_issuances%rowtype;
    v_source_kind text;
    v_original_sck text;
    v_preserved_sck text;
    v_lead_fbclid text;
    v_sck_value text;
    v_attribution_resolution text;
    v_dropped_unsafe text;
    v_offer_resolution text;
    v_lead_offer_code text;
    v_url text;
begin
    if p_external_user_id is null or p_external_user_id !~ '^[1-9][0-9]{7,14}$'
       or p_chatwoot_account_id is null or p_chatwoot_account_id <= 0
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_chatwoot_conversation_id is null or p_chatwoot_conversation_id <= 0
       or p_trigger_external_message_id is null
       or p_trigger_external_message_id !~ '^[1-9][0-9]*$'
       or p_issuance_ulid is null
       or p_issuance_ulid !~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
       or p_now is null then
        return query select 'invalid_request'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        p_chatwoot_account_id::text || ':' || p_chatwoot_inbox_id::text || ':' ||
        p_chatwoot_conversation_id::text || ':' || p_trigger_external_message_id,
        0
    ));

    select issuance.* into v_issuance
    from public.checkout_link_issuances issuance
    where issuance.chatwoot_account_id = p_chatwoot_account_id
      and issuance.chatwoot_inbox_id = p_chatwoot_inbox_id
      and issuance.chatwoot_conversation_id = p_chatwoot_conversation_id
      and issuance.trigger_external_message_id = p_trigger_external_message_id
    for update;
    if found then
        if v_issuance.commercial_case_id is distinct from p_commercial_case_id
           or not exists (
                select 1
                from public.commercial_cases replay_case
                join public.channel_identities replay_identity
                  on replay_identity.id = replay_case.selected_channel_identity_id
                where replay_case.id = p_commercial_case_id
                  and replay_case.contact_id = v_issuance.contact_id
                  and replay_case.selected_channel_identity_id = v_issuance.channel_identity_id
                  and replay_identity.external_user_id = p_external_user_id
                  and replay_identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
                  and replay_identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
                  and replay_identity.external_conversation_id = p_chatwoot_conversation_id::text
           ) then
            return query select 'replay_conflict'::text, null::uuid, null::text,
                null::uuid, null::text, null::text, null::text, null::text;
            return;
        end if;
        return query select
            case
                when v_issuance.status = 'accepted_by_chatwoot' then 'already_accepted'
                when v_issuance.status = 'delivery_unknown' then 'delivery_unknown'
                when v_issuance.status = 'purchase_matched' then 'purchase_already_approved'
                when v_issuance.status = 'request_started' then 'request_started_replay'
                else 'reserved'
            end,
            v_issuance.id, v_issuance.issuance_ulid,
            v_issuance.purchase_intent_id, v_issuance.source_kind,
            v_issuance.checkout_url_final, v_issuance.source_value,
            v_issuance.sck_value;
        return;
    end if;

    select commercial_case.* into v_case
    from public.commercial_cases commercial_case
    where commercial_case.id = p_commercial_case_id
      and commercial_case.case_kind = 'inbound_sales'
    for update;
    if not found or v_case.status <> 'active'
       or v_case.automation_status not in ('draft_only', 'enabled') then
        return query select 'blocked_case'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    select scope.* into v_scope
    from public.inbound_commercial_scope_versions scope
    where scope.scope_key = v_case.inbound_scope_key
      and scope.version = v_case.inbound_scope_version
      and scope.status = 'published'
      and scope.tenant_key = v_case.tenant_ref
      and scope.chatwoot_account_id = p_chatwoot_account_id
      and scope.chatwoot_inbox_id = p_chatwoot_inbox_id
      and lower(scope.external_product_id) = lower(v_case.product_ref)
    for share;
    if not found then
        return query select 'blocked_scope'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    select offer.* into v_default_offer
    from public.checkout_offer_catalog offer
    where offer.tenant_ref = v_case.tenant_ref
      and offer.scope_key = v_case.inbound_scope_key
      and offer.scope_version = v_case.inbound_scope_version
      and lower(offer.product_ref) = lower(v_case.product_ref)
      and offer.status = 'active'
      and offer.default_for_inbound
    for share;
    if not found then
        return query select 'missing_default_offer'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    select contact.* into v_contact
    from public.contacts contact where contact.id = v_case.contact_id
    for update;
    if not found
       or v_contact.contact_permission in ('opted_out', 'blocked', 'restricted')
       or v_contact.lifecycle_status = 'do_not_contact' then
        return query select 'blocked_contact'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;
    if public.has_chatwoot_opt_out_stop(
        p_chatwoot_account_id, p_chatwoot_inbox_id,
        p_chatwoot_conversation_id, p_external_user_id
    ) then
        return query select 'blocked_opt_out'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    select conversation.* into v_conversation
    from public.conversations conversation
    where conversation.id = v_case.conversation_id
      and conversation.contact_id = v_case.contact_id
      and conversation.channel_identity_id = v_case.selected_channel_identity_id
    for update;
    if not found or v_conversation.human_takeover
       or v_conversation.status in (
            'snoozed', 'paused_human', 'completed', 'closed', 'blocked'
       )
       or v_conversation.automation_status not in ('draft_only', 'enabled')
       or v_conversation.commercial_context #>> '{chatwoot_conversation_id}'
            <> p_chatwoot_conversation_id::text then
        return query select 'blocked_conversation'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    select identity.* into v_identity
    from public.channel_identities identity
    where identity.id = v_case.selected_channel_identity_id
      and identity.external_user_id = p_external_user_id
      and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
      and identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
      and identity.external_conversation_id = p_chatwoot_conversation_id::text
      and identity.identity_status = 'active'
    for update;
    if not found then
        return query select 'blocked_identity'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    -- A known purchase of the product blocks, whichever offer it came through.
    -- With per-lead offers a per-offer guard would let a buyer receive a second
    -- link just by having seen another landing.
    if exists (
        select 1 from public.purchase_intents intent
        where intent.tenant_ref = v_case.tenant_ref
          and intent.funnel_ref = v_default_offer.funnel_ref
          and lower(intent.product_ref) = lower(v_default_offer.product_ref)
          and intent.normalized_phone = p_external_user_id
          and intent.lifecycle_state = 'purchased'
    ) then
        return query select 'purchase_already_approved'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;

    -- The lead's offer: the latest live intent of this phone in the funnel.
    -- Intents backed by a real precheckout submission win over intents this
    -- RPC fabricated for earlier inbound links; among equals, the newest.
    select intent.* into v_lead_intent
    from public.purchase_intents intent
    where intent.tenant_ref = v_case.tenant_ref
      and intent.funnel_ref = v_default_offer.funnel_ref
      and lower(intent.product_ref) = lower(v_default_offer.product_ref)
      and intent.normalized_phone = p_external_user_id
      and intent.lifecycle_state = 'waiting_for_purchase'
    order by
        exists (
            select 1 from public.purchase_intent_submissions link
            where link.purchase_intent_id = intent.id
        ) desc,
        intent.submitted_at desc,
        intent.id desc
    limit 1;

    if found then
        v_lead_offer_code := v_lead_intent.offer_ref;
        select offer.* into v_offer
        from public.checkout_offer_catalog offer
        where offer.tenant_ref = v_default_offer.tenant_ref
          and offer.scope_key = v_default_offer.scope_key
          and offer.scope_version = v_default_offer.scope_version
          and lower(offer.product_ref) = lower(v_default_offer.product_ref)
          and offer.landing_ref = v_lead_intent.landing_ref
          and offer.offer_code = v_lead_intent.offer_ref
          and offer.status = 'active'
        order by offer.version desc
        limit 1
        for share;
        if found then
            v_offer_resolution := 'lead_intent';
        else
            v_offer := v_default_offer;
            v_offer_resolution := 'default_offer_not_in_catalog';
        end if;
    else
        v_offer := v_default_offer;
        v_offer_resolution := 'default_no_intent';
    end if;

    select intent.* into v_intent
    from public.purchase_intents intent
    where intent.tenant_ref = v_case.tenant_ref
      and intent.funnel_ref = v_offer.funnel_ref
      and intent.landing_ref = v_offer.landing_ref
      and lower(intent.product_ref) = lower(v_offer.product_ref)
      and intent.offer_ref = v_offer.offer_code
      and intent.normalized_phone = p_external_user_id
      and intent.lifecycle_state = 'waiting_for_purchase'
    order by intent.submitted_at desc, intent.id desc
    limit 1
    for update;

    if not found then
        insert into public.purchase_intents (
            tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
            normalized_email, normalized_phone, submitted_at, lifecycle_state,
            whatsapp_contact_authorized, provisional, provider_observed,
            activation_authorized
        ) values (
            v_offer.tenant_ref, v_offer.funnel_ref, v_offer.landing_ref,
            v_offer.product_ref, v_offer.offer_code, null, p_external_user_id,
            p_now, 'waiting_for_purchase', true, false, false, true
        ) returning * into v_intent;
    end if;

    select submission.* into v_submission
    from public.purchase_intent_submissions link
    join public.precheckout_submissions submission on submission.id = link.submission_id
    where link.purchase_intent_id = coalesce(v_lead_intent.id, v_intent.id)
    order by link.ordinal desc, submission.id desc
    limit 1
    for share of submission;

    if found then
        v_source_kind := 'precheckout_request';
        v_original_sck := nullif(
            btrim(v_submission.raw_payload #>> '{data,attribution,sck}'), ''
        );
        v_lead_fbclid := nullif(
            btrim(v_submission.raw_payload #>> '{data,attribution,fbclid}'), ''
        );
    else
        v_source_kind := 'inbound_request';
        v_original_sck := null;
        v_lead_fbclid := null;
    end if;

    -- Solo se propaga lo que entra crudo en una query string sin escapar nada
    -- mas que el separador. Lo que no pasa la forma se descarta y se registra:
    -- un valor con espacios o acentos partiria la URL en silencio.
    -- 2026-09-25: el alfabeto suma ~, el separador del core v1.10.0 (E10/E13);
    -- | sigue aceptado porque los linajes viejos lo siguen mandando.
    if v_original_sck is not null
       and length(v_original_sck) <= 200
       and v_original_sck ~ '^[A-Za-z0-9._|~-]+$' then
        v_preserved_sck := v_original_sck;
    else
        v_preserved_sck := null;
    end if;

    if v_lead_fbclid is not null and (
           length(v_lead_fbclid) > 512
           or v_lead_fbclid !~ '^[A-Za-z0-9._-]+$'
       ) then
        v_lead_fbclid := null;
        v_dropped_unsafe := 'fbclid';
    end if;

    if v_original_sck is not null and v_preserved_sck is null then
        v_dropped_unsafe := case
            when v_dropped_unsafe = 'fbclid' then 'sck,fbclid'
            else 'sck'
        end;
    end if;

    if v_preserved_sck is not null then
        v_sck_value := v_preserved_sck || '|hermes|v1|' || p_issuance_ulid;
    else
        v_sck_value := 'hermes|v1|' || p_issuance_ulid;
    end if;

    v_attribution_resolution := case
        when v_preserved_sck is not null and v_lead_fbclid is not null then 'full'
        when v_preserved_sck is not null then 'sck_only'
        when v_lead_fbclid is not null then 'fbclid_only'
        else 'marker_only'
    end;

    v_url := v_offer.checkout_base_url || '?off=' || v_offer.offer_code
        || '&checkoutMode=' || v_offer.checkout_mode::text
        || '&src=hermes&sck=' || replace(v_sck_value, '|', '%7C');

    if v_lead_fbclid is not null then
        v_url := v_url || '&fbclid=' || v_lead_fbclid;
    end if;

    insert into public.checkout_link_issuances (
        issuance_ulid, commercial_case_id, purchase_intent_id,
        contact_id, channel_identity_id, offer_catalog_id,
        chatwoot_account_id, chatwoot_inbox_id, chatwoot_conversation_id,
        trigger_external_message_id, source_kind, source_submission_id,
        original_sck, source_value, sck_format_version, sck_value,
        checkout_url_final, offer_resolution, lead_offer_code,
        attribution_resolution, dropped_unsafe_fields
    ) values (
        p_issuance_ulid, v_case.id, v_intent.id,
        v_case.contact_id, v_case.selected_channel_identity_id, v_offer.id,
        p_chatwoot_account_id, p_chatwoot_inbox_id, p_chatwoot_conversation_id,
        p_trigger_external_message_id, v_source_kind, v_submission.id,
        v_original_sck, 'hermes', 'v1', v_sck_value,
        v_url, v_offer_resolution, v_lead_offer_code,
        v_attribution_resolution, v_dropped_unsafe
    ) on conflict (
        chatwoot_account_id, chatwoot_inbox_id,
        chatwoot_conversation_id, trigger_external_message_id
    ) do nothing
    returning * into v_issuance;

    if v_issuance.id is null then
        select issuance.* into strict v_issuance
        from public.checkout_link_issuances issuance
        where issuance.chatwoot_account_id = p_chatwoot_account_id
          and issuance.chatwoot_inbox_id = p_chatwoot_inbox_id
          and issuance.chatwoot_conversation_id = p_chatwoot_conversation_id
          and issuance.trigger_external_message_id = p_trigger_external_message_id
        for update;
        return query select 'reserved'::text,
            v_issuance.id, v_issuance.issuance_ulid,
            v_issuance.purchase_intent_id, v_issuance.source_kind,
            v_issuance.checkout_url_final, v_issuance.source_value,
            v_issuance.sck_value;
        return;
    end if;

    return query select 'reserved'::text,
        v_issuance.id, v_issuance.issuance_ulid,
        v_issuance.purchase_intent_id, v_issuance.source_kind,
        v_issuance.checkout_url_final, v_issuance.source_value,
        v_issuance.sck_value;
end;
$function$;

-- 3. La correlacion de la compra acepta el sck con el del anuncio delante.
create or replace function public.correlate_hotmart_checkout_issuance_v2(
    p_hotmart_webhook_event_id uuid,
    p_sck_value text,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    issuance_id uuid,
    purchase_intent_id uuid,
    commercial_case_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_issuance public.checkout_link_issuances%rowtype;
begin
    if p_hotmart_webhook_event_id is null or p_now is null
       or p_sck_value is null
       or p_sck_value !~
          '^([A-Za-z0-9._|~-]+[|])?hermes[|]v1[|][0-7][0-9A-HJKMNP-TV-Z]{25}$' then
        return query select 'invalid_hermes_sck'::text,
            null::uuid, null::uuid, null::uuid;
        return;
    end if;

    select issuance.* into v_issuance
    from public.checkout_link_issuances issuance
    where issuance.sck_value = p_sck_value
    for update;
    if not found then
        return query select 'not_found'::text, null::uuid, null::uuid, null::uuid;
        return;
    end if;
    if v_issuance.hotmart_webhook_event_id = p_hotmart_webhook_event_id then
        return query select 'replay'::text, v_issuance.id,
            v_issuance.purchase_intent_id, v_issuance.commercial_case_id;
        return;
    end if;
    if v_issuance.hotmart_webhook_event_id is not null then
        return query select 'conflict'::text, v_issuance.id,
            v_issuance.purchase_intent_id, v_issuance.commercial_case_id;
        return;
    end if;
    if not exists (
        select 1 from public.webhook_events event
        where event.id = p_hotmart_webhook_event_id
          and event.source = 'hotmart'
          and event.event_type = 'PURCHASE_APPROVED'
    ) then
        return query select 'invalid_purchase_event'::text,
            null::uuid, null::uuid, null::uuid;
        return;
    end if;

    perform 1
    from public.purchase_intents intent
    where intent.id = v_issuance.purchase_intent_id
      and intent.lifecycle_state = 'waiting_for_purchase'
    for update;
    if not found then
        return query select 'purchase_already_approved'::text,
            v_issuance.id, v_issuance.purchase_intent_id,
            v_issuance.commercial_case_id;
        return;
    end if;

    update public.purchase_intents
    set lifecycle_state = 'purchased', current_classification = null,
        activation_authorized = false, updated_at = p_now
    where id = v_issuance.purchase_intent_id
      and lifecycle_state = 'waiting_for_purchase';
    if not found then
        raise exception using errcode = '40001',
            message = 'checkout_issuance_purchase_intent_changed_concurrently';
    end if;

    perform public.cancel_hotmart_abandonment_reevaluations_for_purchase(
        v_issuance.purchase_intent_id, p_now
    );

    update public.checkout_link_issuances
    set status = 'purchase_matched',
        hotmart_webhook_event_id = p_hotmart_webhook_event_id,
        purchased_at = p_now
    where id = v_issuance.id;

    return query select 'matched'::text, v_issuance.id,
        v_issuance.purchase_intent_id, v_issuance.commercial_case_id;
end;
$function$;

revoke all on function public.correlate_hotmart_checkout_issuance_v2(uuid, text, timestamptz) from public;

revoke all on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) from public;

do $roles$
begin
    if exists (select 1 from pg_catalog.pg_roles where rolname = 'anon') then
        execute 'revoke all on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) from anon';
    end if;

    if exists (select 1 from pg_catalog.pg_roles where rolname = 'authenticated') then
        execute 'revoke all on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) from authenticated';
    end if;

    if exists (select 1 from pg_catalog.pg_roles where rolname = 'service_role') then
        execute 'grant execute on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) to service_role';
    end if;
end
$roles$;

commit;
