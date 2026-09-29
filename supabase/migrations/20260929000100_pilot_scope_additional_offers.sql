-- Varias ofertas por scope en la frontera del piloto (F2c-2a).
--
-- Una aliada vende el mismo producto con una oferta por landing. F2c
-- (20260928000200) hizo que la admision de carrito y pago fallido acepte
-- cualquier oferta del binding, pero la frontera del piloto seguia comparando
-- contra la unica `pilot_scope_versions.offer_code`: el caso entraba y moria
-- en la planificacion (`pilot_offer_mismatch`, `payment_failure_scope_binding_mismatch`)
-- o en el arranque del envio. Esta migracion:
--
-- 1. suma `additional_offer_codes` al scope del piloto, con la misma forma que
--    en el binding (la `offer_code` sigue siendo la oferta por defecto);
-- 2. `evaluate_lancemos_pilot_scope` y `authorize_lancemos_pilot_request_start`
--    aceptan cualquier oferta del conjunto del scope;
-- 3. `plan_portable_payment_failure_recovery` acepta cualquier oferta del
--    conjunto del scope y del binding, y exige que la oferta pedida sea la del
--    evento (antes lo garantizaba la igualdad con la unica del binding).
--
-- Las funciones se copian de su definicion vigente (20260810000100 y
-- 20260903000300) y solo cambia la comparacion de oferta; `create or replace`
-- conserva los grants. Los scopes existentes quedan con el conjunto vacio, asi
-- que Johanna no cambia. No siembra filas de ningun cliente.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

alter table public.pilot_scope_versions
    add column additional_offer_codes text[] not null default '{}'::text[];

alter table public.pilot_scope_versions
    add constraint pilot_scope_versions_additional_offer_codes_shape
    check (
        cardinality(additional_offer_codes) <= 16
        and array_position(additional_offer_codes, null) is null
        and array_position(additional_offer_codes, offer_code) is null
        and array_to_string(additional_offer_codes, ',')
            ~ '^([A-Za-z0-9]{4,32}(,[A-Za-z0-9]{4,32})*)?$'
    );

create or replace function public.evaluate_lancemos_pilot_scope(
    p_scope_key text,
    p_scope_version integer,
    p_tenant_key text,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_channel_provider text,
    p_channel_account_ref text,
    p_source text,
    p_source_event_type text,
    p_external_product_id text,
    p_offer_code text,
    p_contact_id uuid
)
returns table (
    allowed boolean,
    reason_code text,
    runtime_generation bigint
)
language plpgsql
security definer
stable
set search_path = public, pg_temp
as $function$
declare
    v_scope public.pilot_scope_versions%rowtype;
    v_control public.pilot_runtime_controls%rowtype;
    v_reason text;
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1
       or p_tenant_key is null or btrim(p_tenant_key) = ''
       or p_chatwoot_account_id is null or p_chatwoot_account_id < 1
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1
       or p_channel_provider is null or btrim(p_channel_provider) = ''
       or p_channel_account_ref is null or btrim(p_channel_account_ref) = ''
       or p_source is null or btrim(p_source) = ''
       or p_source_event_type is null or btrim(p_source_event_type) = ''
       or p_external_product_id is null or btrim(p_external_product_id) = ''
       or p_offer_code is null or btrim(p_offer_code) = ''
       or p_contact_id is null then
        return query select false, 'pilot_scope_input_invalid'::text, null::bigint;
        return;
    end if;

    select scope.* into v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';
    if not found then
        return query select false, 'pilot_scope_not_published'::text, null::bigint;
        return;
    end if;

    select control.* into v_control
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key;
    if not found or v_control.scope_version <> p_scope_version then
        return query select false, 'pilot_scope_version_mismatch'::text,
            coalesce(v_control.generation, null::bigint);
        return;
    end if;

    if v_control.runtime_state <> 'armed' then
        v_reason := 'pilot_runtime_not_armed';
    elsif p_tenant_key <> v_scope.tenant_key then
        v_reason := 'pilot_tenant_mismatch';
    elsif p_chatwoot_account_id <> v_scope.chatwoot_account_id then
        v_reason := 'pilot_chatwoot_account_mismatch';
    elsif p_chatwoot_inbox_id <> v_scope.chatwoot_inbox_id then
        v_reason := 'pilot_chatwoot_inbox_mismatch';
    elsif p_channel_provider <> v_scope.channel_provider
       or p_channel_account_ref <> v_scope.channel_account_ref then
        v_reason := 'pilot_channel_account_mismatch';
    elsif p_source <> v_scope.source
       or p_source_event_type <> v_scope.source_event_type then
        v_reason := 'pilot_source_event_mismatch';
    elsif p_external_product_id <> v_scope.external_product_id then
        v_reason := 'pilot_product_mismatch';
    elsif p_offer_code <> v_scope.offer_code
       and p_offer_code <> all(v_scope.additional_offer_codes) then
        v_reason := 'pilot_offer_mismatch';
    elsif not exists (
        select 1 from public.pilot_cohort_memberships member
        where member.scope_key = p_scope_key
          and member.scope_version = p_scope_version
          and member.contact_id = p_contact_id
          and member.member_status = 'active'
    ) then
        v_reason := 'pilot_contact_not_in_cohort';
    else
        v_reason := 'pilot_scope_allowed';
    end if;

    return query select
        v_reason = 'pilot_scope_allowed',
        v_reason,
        v_control.generation;
end;
$function$;

create or replace function public.authorize_lancemos_pilot_request_start(
    p_scope_key text,
    p_scope_version integer,
    p_tenant_key text,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_channel_provider text,
    p_channel_account_ref text,
    p_source text,
    p_source_event_type text,
    p_external_product_id text,
    p_offer_code text,
    p_contact_id uuid,
    p_action_id uuid,
    p_attempt_id uuid,
    p_now timestamptz
)
returns table (
    authorized boolean,
    reason_code text,
    runtime_generation bigint,
    request_authorization_id uuid,
    replayed boolean
)
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_scope public.pilot_scope_versions%rowtype;
    v_control public.pilot_runtime_controls%rowtype;
    v_existing public.pilot_outbound_request_authorizations%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_case public.recovery_cases%rowtype;
    v_identity public.channel_identities%rowtype;
    v_local_date date;
    v_total integer;
    v_daily integer;
    v_authorization_id uuid;
    v_authorized_at timestamptz;
    v_control_exists boolean;
    v_reason text;
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1
       or p_tenant_key is null or btrim(p_tenant_key) = ''
       or p_chatwoot_account_id is null or p_chatwoot_account_id < 1
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1
       or p_channel_provider is null or btrim(p_channel_provider) = ''
       or p_channel_account_ref is null or btrim(p_channel_account_ref) = ''
       or p_source is null or btrim(p_source) = ''
       or p_source_event_type is null or btrim(p_source_event_type) = ''
       or p_external_product_id is null or btrim(p_external_product_id) = ''
       or p_offer_code is null or btrim(p_offer_code) = ''
       or p_contact_id is null or p_action_id is null or p_attempt_id is null
       or p_now is null then
        return query select false, 'pilot_scope_input_invalid'::text,
            null::bigint, null::uuid, false;
        return;
    end if;

    select scope.* into v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';
    if not found then
        return query select false, 'pilot_scope_not_published'::text,
            null::bigint, null::uuid, false;
        return;
    end if;

    select authrow.* into v_existing
    from public.pilot_outbound_request_authorizations authrow
    where authrow.attempt_id = p_attempt_id;

    if found and (
        v_existing.scope_key <> p_scope_key
        or v_existing.scope_version <> p_scope_version
        or v_existing.action_id <> p_action_id
        or v_existing.contact_id <> p_contact_id
    ) then
        return query select false, 'pilot_attempt_mismatch'::text,
            v_existing.runtime_generation, null::uuid, false;
        return;
    end if;

    if p_tenant_key <> v_scope.tenant_key then
        v_reason := 'pilot_tenant_mismatch';
    elsif p_chatwoot_account_id <> v_scope.chatwoot_account_id then
        v_reason := 'pilot_chatwoot_account_mismatch';
    elsif p_chatwoot_inbox_id <> v_scope.chatwoot_inbox_id then
        v_reason := 'pilot_chatwoot_inbox_mismatch';
    elsif p_channel_provider <> v_scope.channel_provider
       or p_channel_account_ref <> v_scope.channel_account_ref then
        v_reason := 'pilot_channel_account_mismatch';
    elsif p_source <> v_scope.source
       or p_source_event_type <> v_scope.source_event_type then
        v_reason := 'pilot_source_event_mismatch';
    elsif p_external_product_id <> v_scope.external_product_id then
        v_reason := 'pilot_product_mismatch';
    elsif p_offer_code <> v_scope.offer_code
       and p_offer_code <> all(v_scope.additional_offer_codes) then
        v_reason := 'pilot_offer_mismatch';
    else
        v_reason := null;
    end if;

    if v_reason is not null then
        return query select false, v_reason,
            case when v_existing.id is null then null else v_existing.runtime_generation end,
            null::uuid, false;
        return;
    end if;

    if v_existing.id is not null then
        return query select true, 'pilot_request_start_authorized'::text,
            v_existing.runtime_generation, v_existing.id, true;
        return;
    end if;

    v_authorized_at := clock_timestamp();
    if p_now < v_authorized_at - interval '5 minutes'
       or p_now > v_authorized_at + interval '5 minutes' then
        return query select false, 'pilot_request_time_invalid'::text,
            null::bigint, null::uuid, false;
        return;
    end if;

    select control.* into v_control
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key
    for update;
    v_control_exists := found;

    select authrow.* into v_existing
    from public.pilot_outbound_request_authorizations authrow
    where authrow.attempt_id = p_attempt_id;
    if found then
        if v_existing.scope_key <> p_scope_key
           or v_existing.scope_version <> p_scope_version
           or v_existing.action_id <> p_action_id
           or v_existing.contact_id <> p_contact_id then
            return query select false, 'pilot_attempt_mismatch'::text,
                v_existing.runtime_generation, null::uuid, false;
            return;
        end if;
        return query select true, 'pilot_request_start_authorized'::text,
            v_existing.runtime_generation, v_existing.id, true;
        return;
    end if;

    if not v_control_exists or v_control.scope_version <> p_scope_version then
        return query select false, 'pilot_scope_version_mismatch'::text,
            coalesce(v_control.generation, null::bigint), null::uuid, false;
        return;
    end if;

    if v_control.runtime_state <> 'armed' then
        return query select false, 'pilot_runtime_not_armed'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    if not exists (
        select 1 from public.pilot_cohort_memberships member
        where member.scope_key = p_scope_key
          and member.scope_version = p_scope_version
          and member.contact_id = p_contact_id
          and member.member_status = 'active'
    ) then
        return query select false, 'pilot_contact_not_in_cohort'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    select attempt.* into v_attempt
    from public.followup_delivery_attempts attempt
    where attempt.id = p_attempt_id
      and attempt.action_id = p_action_id;
    if not found or v_attempt.phase <> 'reserved' or v_attempt.outcome is not null then
        return query select false, 'pilot_attempt_mismatch'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    select recovery_case.* into v_case
    from public.scheduled_actions action
    join public.recovery_cases recovery_case
      on recovery_case.id = action.recovery_case_id
    where action.id = p_action_id
      and recovery_case.contact_id = p_contact_id;
    if not found
       or v_case.external_product_id <> p_external_product_id
       or v_case.offer_code is distinct from p_offer_code
       or v_case.source <> p_source
       or v_case.policy_key <> v_scope.policy_key
       or v_case.policy_version <> v_scope.policy_version
       or v_case.selected_channel_identity_id is null then
        return query select false, 'pilot_attempt_mismatch'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    select identity.* into v_identity
    from public.channel_identities identity
    where identity.id = v_case.selected_channel_identity_id
      and identity.contact_id = p_contact_id
      and identity.channel = 'whatsapp'
      and identity.identity_status = 'active';
    if not found
       or v_identity.account_id <> 'chatwoot:' || p_chatwoot_account_id::text
       or v_identity.metadata ->> 'inbox_id' <> p_chatwoot_inbox_id::text then
        return query select false, 'pilot_attempt_mismatch'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    v_local_date := (v_authorized_at at time zone v_scope.timezone)::date;

    select count(*)::integer into v_total
    from public.pilot_outbound_request_authorizations authrow
    where authrow.scope_key = p_scope_key;
    if v_total >= v_scope.max_outbound_request_starts_total then
        return query select false, 'pilot_total_budget_exhausted'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    select count(*)::integer into v_daily
    from public.pilot_outbound_request_authorizations authrow
    where authrow.scope_key = p_scope_key
      and authrow.local_budget_date = v_local_date;
    if v_daily >= v_scope.max_outbound_request_starts_per_day then
        return query select false, 'pilot_daily_budget_exhausted'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    insert into public.pilot_outbound_request_authorizations (
        scope_key, scope_version, action_id, attempt_id, contact_id,
        local_budget_date, runtime_generation, reason_code, authorized_at
    ) values (
        p_scope_key, p_scope_version, p_action_id, p_attempt_id, p_contact_id,
        v_local_date, v_control.generation,
        'pilot_request_start_authorized', v_authorized_at
    ) returning id into v_authorization_id;

    insert into public.pilot_control_events (
        scope_key, scope_version, event_type, runtime_generation,
        contact_id, action_id, attempt_id, actor, reason_code,
        data
    ) values (
        p_scope_key, p_scope_version, 'pilot_outbound_request_authorized',
        v_control.generation, p_contact_id, p_action_id, p_attempt_id,
        'system', 'pilot_request_start_authorized',
        jsonb_build_object('local_budget_date', v_local_date)
    );

    return query select true, 'pilot_request_start_authorized'::text,
        v_control.generation, v_authorization_id, false;
end;
$function$;

create or replace function public.plan_portable_payment_failure_recovery(
    p_webhook_event_id uuid,
    p_contact_id uuid,
    p_external_product_id text,
    p_product_name text,
    p_offer_code text,
    p_policy_key text,
    p_policy_version integer,
    p_failed_at timestamptz,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_external_user_id text,
    p_scope_key text,
    p_scope_version integer
)
returns table (
    recovery_case_id uuid,
    followup_sequence_id uuid,
    scheduled_action_id uuid,
    created boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_scope public.pilot_scope_versions%rowtype;
    v_provenance public.commercial_ally_hotmart_event_bindings%rowtype;
    v_webhook public.webhook_events%rowtype;
    v_payment_failure public.commercial_ally_payment_failure_details%rowtype;
    v_purchase_intent public.purchase_intents%rowtype;
    v_runtime_binding public.commercial_ally_runtime_bindings%rowtype;
    v_canonical_failed_at timestamptz;
    v_allowed boolean;
    v_reason text;
    v_generation bigint;
    v_recovery_case_id uuid;
    v_followup_sequence_id uuid;
    v_scheduled_action_id uuid;
    v_created boolean;
    v_binding public.pilot_recovery_case_bindings%rowtype;
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1
       or p_policy_key is null or btrim(p_policy_key) = ''
       or p_policy_version is null or p_policy_version < 1 then
        raise exception using
            errcode = '22023',
            message = 'invalid_pilot_plan_parameters';
    end if;

    select provenance.*
      into v_provenance
    from public.commercial_ally_hotmart_event_bindings provenance
    join public.webhook_events event
      on event.id = provenance.webhook_event_id
     and event.source = 'hotmart'
     and event.event_type = 'PURCHASE_CANCELED'
    where provenance.webhook_event_id = p_webhook_event_id;
    if not found then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_provenance_missing';
    end if;

    select event.*
      into v_webhook
    from public.webhook_events event
    where event.id = p_webhook_event_id
      and event.source = 'hotmart'
      and event.event_type = 'PURCHASE_CANCELED';
    v_canonical_failed_at := to_timestamp(
        (v_webhook.payload ->> 'creation_date')::double precision / 1000.0
    );
    if not found
       or p_failed_at is null
       or p_failed_at is distinct from v_canonical_failed_at then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_timestamp_mismatch';
    end if;

    select details.*
      into v_payment_failure
    from public.commercial_ally_payment_failure_details details
    where details.webhook_event_id = p_webhook_event_id
      and details.trigger_kind = 'payment_failure'
      and details.correlation_outcome = 'resolved'
      and details.purchase_intent_id is not null;
    if not found then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_correlation_unresolved';
    end if;

    select intent.*
      into v_purchase_intent
    from public.purchase_intents intent
    where intent.id = v_payment_failure.purchase_intent_id
      and intent.tenant_ref = v_provenance.tenant_ref
      and intent.funnel_ref = v_provenance.funnel_ref
      and intent.product_ref = v_provenance.purchase_intent_product_ref
      and intent.offer_ref = v_provenance.offer_ref
      and intent.normalized_phone = p_external_user_id;
    if not found then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_recipient_mismatch';
    end if;

    if not exists (
        select 1
        from public.contact_points point
        where point.contact_id = p_contact_id
          and point.type = 'phone'
          and point.normalized_value = v_purchase_intent.normalized_phone
          and point.source = 'hotmart'
    ) then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_contact_mismatch';
    end if;

    select binding.*
      into v_runtime_binding
    from public.commercial_ally_runtime_bindings binding
    where binding.tenant_ref = v_provenance.tenant_ref
      and binding.funnel_ref = v_provenance.funnel_ref
      and binding.binding_version = v_provenance.binding_version
      and binding.status = 'active';
    if not found then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_binding_unavailable';
    end if;

    -- Serialize planning with runtime pause/version activation and cohort changes.
    perform 1
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key
    for update;

    select scope.* into v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';

    if v_scope.tenant_key is distinct from v_provenance.tenant_ref
       or v_scope.external_product_id is distinct from v_provenance.hotmart_product_id
       or (
           v_provenance.offer_ref is distinct from v_scope.offer_code
           and v_provenance.offer_ref <> all(v_scope.additional_offer_codes)
       )
       or v_runtime_binding.hotmart_product_id::text is distinct from p_external_product_id
       or v_runtime_binding.product_name is distinct from p_product_name
       or p_offer_code is distinct from v_provenance.offer_ref
       or (
           p_offer_code is distinct from v_runtime_binding.offer_code
           and p_offer_code <> all(v_runtime_binding.additional_offer_codes)
       )
       or v_runtime_binding.chatwoot_account_id is distinct from p_chatwoot_account_id
       or v_runtime_binding.chatwoot_inbox_id is distinct from p_chatwoot_inbox_id then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_scope_binding_mismatch';
    end if;

    select evaluation.allowed,
           evaluation.reason_code,
           evaluation.runtime_generation
      into v_allowed, v_reason, v_generation
    from public.evaluate_lancemos_pilot_scope(
        p_scope_key,
        p_scope_version,
        v_scope.tenant_key,
        p_chatwoot_account_id,
        p_chatwoot_inbox_id,
        v_scope.channel_provider,
        v_scope.channel_account_ref,
        'hotmart',
        'PURCHASE_CANCELED',
        p_external_product_id,
        p_offer_code,
        p_contact_id
    ) evaluation;

    if not coalesce(v_allowed, false) then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = coalesce(v_reason, 'pilot_scope_unknown');
    end if;

    if v_scope.policy_key is distinct from p_policy_key
       or v_scope.policy_version is distinct from p_policy_version then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = 'pilot_policy_mismatch';
    end if;

    select plan.recovery_case_id,
           plan.followup_sequence_id,
           plan.scheduled_action_id,
           plan.created
      into v_recovery_case_id,
           v_followup_sequence_id,
           v_scheduled_action_id,
           v_created
    from public.plan_payment_failure_recovery_with_identity(
        p_webhook_event_id,
        p_contact_id,
        p_external_product_id,
        p_product_name,
        p_offer_code,
        p_policy_key,
        p_policy_version,
        v_canonical_failed_at,
        p_chatwoot_account_id,
        p_chatwoot_inbox_id,
        p_external_user_id
    ) plan;

    insert into public.pilot_recovery_case_bindings (
        recovery_case_id, scope_key, scope_version, source_event_id
    ) values (
        v_recovery_case_id, p_scope_key, p_scope_version, p_webhook_event_id
    ) on conflict on constraint pilot_recovery_case_bindings_pkey do nothing;

    select binding.* into strict v_binding
    from public.pilot_recovery_case_bindings binding
    where binding.recovery_case_id = v_recovery_case_id;
    if v_binding.scope_key <> p_scope_key
       or v_binding.scope_version <> p_scope_version then
        raise exception using
            errcode = '55000',
            message = 'pilot_case_binding_conflict';
    end if;

    return query select
        v_recovery_case_id,
        v_followup_sequence_id,
        v_scheduled_action_id,
        v_created;
end;
$function$;

commit;
