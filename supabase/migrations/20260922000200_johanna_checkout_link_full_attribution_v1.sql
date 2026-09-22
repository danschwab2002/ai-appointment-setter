-- Migration: el link del recuperador lleva el fbclid del lead y preserva el sck
-- del anuncio, en vez de reemplazarlo (decision de Dan del 2026-09-22).
--
-- Que estaba mal. El link salia con exactamente cuatro parametros: off,
-- checkoutMode, src=hermes y sck=hermes|v1|<ulid>. Eso tiene dos costos medidos
-- contra el estandar de tracking de Lancemos:
--
--   1. Sin fbclid, Meta solo puede matchear la venta recuperada por identidad
--      (mail y telefono del checkout). Atribuye igual, porque no lee el sck ni
--      las UTMs, asi que omitirlo no evita que la campana se lleve el credito:
--      solo degrada el match de determinista a probabilistico y baja el
--      tracking accuracy del aliado. Medido en ATT1 el 2026-09-20: Meta se
--      atribuia ventas de ManyChat que entraban sin fbclid.
--   2. El sck del recuperador pisaba el del anuncio, asi que la venta dejaba de
--      cruzar al anuncio puntual en el reporting y caia como etiqueta sin
--      resolver.
--
-- Que hace esta migracion. El sck pasa a ser <original>|hermes|v1|<ulid>: el del
-- anuncio queda primero y entero, el marcador del recuperador despues. Un parser
-- que corte por | encuentra el original en el primer campo. Y el fbclid del lead
-- viaja como su propio parametro cuando existe.
--
-- Dato real (Supabase, 2026-09-22, ultimos 30 dias, 246 formularios): el sck de
-- Johanna NO esta en el formato del estandar E10. Llega como fb.paid.<18 digitos>
-- (139), ig.paid.<18 digitos> (68), ig.social (5), y 33 sin sck. El tercer campo
-- es un id de Meta. Por eso esta migracion NO normaliza el formato: preserva lo
-- que venga. Cambiar el formato de origen toca el core y cuatro repos de
-- Lancemos, es otro trabajo y necesita el OK de Juan. El fbclid llega en 224 de
-- 264 formularios, con un largo maximo de 212 caracteres.
--
-- Lo que NO se toca: el nombre y la firma de la RPC siguen iguales, asi que el
-- bridge no cambia y no hace falta redeploy. La resolucion de oferta por intent
-- del lead (20260922000100) queda intacta.
--
-- Seguridad de la URL: solo se propaga lo que entra crudo en una query string.
-- Un sck o un fbclid con espacios, acentos o & partiria la URL en silencio, asi
-- que lo que no pasa la forma se descarta y queda registrado en
-- dropped_unsafe_fields. Ningun dato personal viaja en la URL: el prellenado de
-- mail, telefono y nombre sigue afuera por contrato.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- 1. Trazabilidad de que se pudo componer en cada emision.
alter table public.checkout_link_issuances
    add column attribution_resolution text not null default 'marker_only'
        check (attribution_resolution in (
            'full', 'sck_only', 'fbclid_only', 'marker_only'
        )),
    add column dropped_unsafe_fields text
        check (dropped_unsafe_fields is null or dropped_unsafe_fields in (
            'sck', 'fbclid', 'sck,fbclid'
        ));

-- 2. El sck ya no es solo el marcador: ahora puede traer el del anuncio delante.
--    La constraint vieja es anonima, asi que se la busca por su definicion en vez
--    de adivinarle el nombre generado.
do $constraint$
declare
    v_name text;
begin
    select con.conname into v_name
    from pg_catalog.pg_constraint con
    join pg_catalog.pg_class rel on rel.oid = con.conrelid
    join pg_catalog.pg_namespace nsp on nsp.oid = rel.relnamespace
    where nsp.nspname = 'public'
      and rel.relname = 'checkout_link_issuances'
      and con.contype = 'c'
      and pg_catalog.pg_get_constraintdef(con.oid) like '%sck_value%hermes|v1|%issuance_ulid%';

    if v_name is null then
        raise exception using errcode = '55000',
            message = 'checkout_link_issuances_sck_marker_constraint_not_found';
    end if;

    execute format(
        'alter table public.checkout_link_issuances drop constraint %I', v_name
    );
end
$constraint$;

alter table public.checkout_link_issuances
    add constraint checkout_link_issuances_sck_value_shape check (
        sck_value = 'hermes|v1|' || issuance_ulid
        or right(sck_value, length(issuance_ulid) + 11)
           = '|hermes|v1|' || issuance_ulid
    );

-- 3. La forma de la URL admite el sck compuesto y el fbclid opcional.
alter table public.checkout_link_issuances
    drop constraint checkout_link_issuances_url_shape;

alter table public.checkout_link_issuances
    add constraint checkout_link_issuances_url_shape check (
        checkout_url_final ~ ('^https://pay[.]hotmart[.]com/[A-Za-z0-9_-]+'
            || '[?]off=[A-Za-z0-9_-]+&checkoutMode=[1-9][0-9]*&src=hermes'
            || '&sck=([A-Za-z0-9._%-]+%7C)?hermes%7Cv1%7C'
            || '[0-7][0-9A-HJKMNP-TV-Z]{25}'
            || '(&fbclid=[A-Za-z0-9._-]+)?$')
    );

-- 4. Las columnas nuevas tambien son inmutables.
create or replace function public.protect_checkout_link_issuance()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op = 'DELETE'
       or old.id is distinct from new.id
       or old.issuance_ulid is distinct from new.issuance_ulid
       or old.commercial_case_id is distinct from new.commercial_case_id
       or old.purchase_intent_id is distinct from new.purchase_intent_id
       or old.contact_id is distinct from new.contact_id
       or old.channel_identity_id is distinct from new.channel_identity_id
       or old.offer_catalog_id is distinct from new.offer_catalog_id
       or old.chatwoot_account_id is distinct from new.chatwoot_account_id
       or old.chatwoot_inbox_id is distinct from new.chatwoot_inbox_id
       or old.chatwoot_conversation_id is distinct from new.chatwoot_conversation_id
       or old.trigger_external_message_id is distinct from new.trigger_external_message_id
       or old.source_kind is distinct from new.source_kind
       or old.source_submission_id is distinct from new.source_submission_id
       or old.original_sck is distinct from new.original_sck
       or old.source_value is distinct from new.source_value
       or old.sck_format_version is distinct from new.sck_format_version
       or old.sck_value is distinct from new.sck_value
       or old.checkout_url_final is distinct from new.checkout_url_final
       or old.offer_resolution is distinct from new.offer_resolution
       or old.lead_offer_code is distinct from new.lead_offer_code
       or old.attribution_resolution is distinct from new.attribution_resolution
       or old.dropped_unsafe_fields is distinct from new.dropped_unsafe_fields
       or old.created_at is distinct from new.created_at
       or not (
            (old.status = 'reserved'
                and new.status in ('request_started', 'purchase_matched'))
            or (old.status = 'request_started'
                and new.status in ('accepted_by_chatwoot', 'delivery_unknown', 'purchase_matched'))
            or (old.status = 'delivery_unknown'
                and new.status in ('accepted_by_chatwoot', 'purchase_matched'))
            or (old.status = 'accepted_by_chatwoot'
                and new.status = 'purchase_matched')
       ) then
        raise exception using errcode = '55000', message = 'checkout_link_issuance_immutable';
    end if;
    return new;
end;
$function$;

-- 5. La RPC compone el sck y agrega el fbclid. Misma firma, mismo nombre.
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
    if v_original_sck is not null
       and length(v_original_sck) <= 200
       and v_original_sck ~ '^[A-Za-z0-9._|-]+$' then
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

-- 6. La correlacion de la compra acepta el sck con el del anuncio delante.
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
          '^([A-Za-z0-9._|-]+[|])?hermes[|]v1[|][0-7][0-9A-HJKMNP-TV-Z]{25}$' then
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

revoke all on function public.protect_checkout_link_issuance() from public;
revoke all on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) from public;

do $roles$
begin
    if exists (select 1 from pg_catalog.pg_roles where rolname = 'anon') then
        execute 'revoke all on function public.protect_checkout_link_issuance() from anon';
        execute 'revoke all on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) from anon';
    end if;

    if exists (select 1 from pg_catalog.pg_roles where rolname = 'authenticated') then
        execute 'revoke all on function public.protect_checkout_link_issuance() from authenticated';
        execute 'revoke all on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) from authenticated';
    end if;

    if exists (select 1 from pg_catalog.pg_roles where rolname = 'service_role') then
        execute 'grant execute on function public.reserve_chatwoot_checkout_issuance_v2(uuid, text, bigint, bigint, bigint, text, text, timestamptz) to service_role';
    end if;
end
$roles$;

commit;
