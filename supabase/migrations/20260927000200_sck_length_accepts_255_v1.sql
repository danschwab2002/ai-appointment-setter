-- Migration: el sck del anuncio acepta hasta 255 caracteres (antes 200) en el
-- guard de la reserva. Decision de Dan del 2026-09-27 (E02 del estandar de
-- tracking de Lancemos, v0.14, punto 3: "el recuperador sube su limite de largo
-- del sck de 200 a 255").
--
-- Que cambia del lado del emisor. El core de los sitios v1.13.0 (lancemos/core#54
-- dcdc9aa y su porte draninagarza#35 0ad4e8e, mergeados y desplegados el
-- 2026-09-27 14:04Z, verificados en produccion en draninagarza ~14:20Z) lee
-- utm_id y lo suma como SEXTO campo posicional del sck, al final:
--     utm_source~utm_term~utm_content~utm_medium~utm_campaign~utm_id
-- Sin utm_id el sck sigue de cinco campos, sin ~ colgando. utm_id es el
-- campaign.id de Meta: 18 digitos. Observado en produccion:
--     fb~tiroides~ad3~cpc~E02-prueba~120210000000000001
--
-- Por que 255. El sck mas largo medido en las ventas de Hotmart tiene 177
-- caracteres (2026-09-24, 707 sck con etiqueta; mediana 143) y lo alarga el
-- nombre del anuncio. Con el ~ y el id de 18 digitos pasa a 196: por debajo de
-- 200 todavia, pero al borde, y cualquier nombre de anuncio mas largo caeria a
-- null en silencio (el guard descarta, la venta queda como marker_only, sin
-- error ni alerta). 255 deja margen y es el tope que fija el estandar. El tope
-- sigue fuera de la regex: Postgres limita las repeticiones a 255.
--
-- Dato real (Supabase, 2026-09-27 01:40Z): 352 sck en precheckout_submissions,
-- maximo 27 (mediana 26, todos fb.paid.<id> / ig.paid.<id>), 0 con ~, 0 mayores
-- a 200, 0 de seis campos; 9 emisiones, el marcador |hermes|v1|<ulid> mide 37.
-- Ningun sck real se acerca hoy al tope: el cambio acompana al core, que ya
-- emite el sexto campo, antes de que Santi cargue utm_id en los anuncios.
--
-- Lo que se decide y lo que no:
--   - Solo cambia el guard de reserve_chatwoot_checkout_issuance_v2. El
--     reconocedor de la compra (correlate_hotmart_checkout_issuance_v2) y el
--     CHECK de forma de la URL no tienen tope de largo: no se tocan.
--   - El alfabeto no cambia: [A-Za-z0-9._|~-]. utm_id es numerico.
--   - El bridge exige la misma forma sobre la fila que la RPC devuelve
--     (_CHECKOUT_SAFE_SCK en src/bridge/supabase.py): se despliega el bridge
--     primero y esta migracion despues. Al reves, con la base aceptando 255 y
--     el bridge en 200, un sck de 201..255 haria que el link no salga.
--   - src=hermes, el marcador |hermes|v1|<ulid> y la firma de la RPC no cambian.
--
-- Se redefine desde la version vigente (20260925000100), no desde la original
-- (20260914000100): el cuerpo es identico salvo el tope y su comentario.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- La RPC de reserva: el guard del sck del anuncio acepta hasta 255. Misma firma,
-- mismo nombre, mismo cuerpo salvo el tope.
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
    -- 2026-09-27: el tope sube de 200 a 255 por el sexto campo utm_id (E02),
    -- el campaign.id de Meta de 18 digitos al final del sck.
    if v_original_sck is not null
       and length(v_original_sck) <= 255
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
