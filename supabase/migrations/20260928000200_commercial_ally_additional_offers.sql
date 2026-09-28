-- Varias ofertas por binding en los eventos de Hotmart del runtime portable.
--
-- Una aliada vende el mismo producto con una oferta por landing. El binding
-- guardaba una sola `offer_code`, y las RPC
-- portables exigian oferta == binding: un carrito o un pago fallido por otra
-- landing se perdia. Esta migracion:
--
-- 1. suma `additional_offer_codes` al binding (la `offer_code` sigue siendo la
--    oferta por defecto);
-- 2. carrito y pago fallido aceptan cualquier oferta del binding y resuelven el
--    scope de intencion de ESA oferta, que tiene que existir y estar activo;
-- 3. la compra aprobada frena la recuperacion con cualquier oferta del producto:
--    una compra es una compra, aunque entre por una oferta que el setter no ofrece.
--
-- Las funciones se copian de su definicion vigente (20260901000300,
-- 20260903000100, 20260903000300) y solo cambia la comparacion de oferta.
-- `create or replace` conserva los grants. No siembra filas de ningun cliente.

alter table public.commercial_ally_runtime_bindings
    add column additional_offer_codes text[] not null default '{}'::text[];

alter table public.commercial_ally_runtime_bindings
    add constraint commercial_ally_runtime_bindings_additional_offer_codes_shape
    check (
        cardinality(additional_offer_codes) <= 16
        and array_position(additional_offer_codes, null) is null
        and array_position(additional_offer_codes, offer_code) is null
        and array_to_string(additional_offer_codes, ',')
            ~ '^([A-Za-z0-9]{4,32}(,[A-Za-z0-9]{4,32})*)?$'
    );

create or replace function public.admit_portable_hotmart_cart_abandonment(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
    p_external_event_id text,
    p_payload jsonb,
    p_normalized_email text,
    p_normalized_phone text
)
returns table (
    outcome text,
    webhook_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_scope public.hotmart_purchase_intent_scopes%rowtype;
    v_provenance public.commercial_ally_hotmart_event_bindings%rowtype;
    v_admission_outcome text;
    v_event_id uuid;
    v_correlation_outcome text;
begin
    if p_tenant_ref is null or nullif(btrim(p_tenant_ref), '') is null
       or p_funnel_ref is null or nullif(btrim(p_funnel_ref), '') is null
       or p_binding_version is null or p_binding_version < 1
       or p_external_event_id is null or nullif(btrim(p_external_event_id), '') is null
       or p_payload is null or jsonb_typeof(p_payload) <> 'object' then
        raise exception using errcode = '22023',
            message = 'invalid_portable_hotmart_cart_input';
    end if;

    select b.* into v_binding
    from public.commercial_ally_runtime_bindings b
    where b.tenant_ref = p_tenant_ref
      and b.funnel_ref = p_funnel_ref
      and b.binding_version = p_binding_version
      and b.status = 'active'
    for update;
    if not found then
        raise exception using errcode = '22023',
            message = 'commercial_ally_binding_unavailable';
    end if;

    if p_payload #>> '{id}' is distinct from p_external_event_id
       or p_payload #>> '{event}' is distinct from 'PURCHASE_OUT_OF_SHOPPING_CART'
       or p_payload #>> '{version}' is distinct from '2.0.0'
       or jsonb_typeof(p_payload #> '{data,product,id}') is distinct from 'number'
       or (p_payload #>> '{data,product,id}')::numeric
            is distinct from v_binding.hotmart_product_id::numeric
       or not coalesce((p_payload #>> '{data,offer,code}' = v_binding.offer_code
            or p_payload #>> '{data,offer,code}' = any(v_binding.additional_offer_codes)), false) then
        raise exception using errcode = '22023',
            message = 'portable_hotmart_cart_binding_mismatch';
    end if;

    select scope.* into v_scope
    from public.hotmart_purchase_intent_scopes scope
    where scope.tenant_ref = v_binding.tenant_ref
      and scope.funnel_ref = v_binding.funnel_ref
      and scope.hotmart_product_id = v_binding.hotmart_product_id::text
      and scope.purchase_intent_product_ref = v_binding.product_hotlink
      and scope.offer_ref = p_payload #>> '{data,offer,code}'
      and scope.active
    for update;
    if not found then
        raise exception using errcode = '22023',
            message = 'portable_hotmart_cart_scope_unavailable';
    end if;

    select admission.outcome, admission.webhook_event_id
    into strict v_admission_outcome, v_event_id
    from public._admit_hotmart_cart_abandonment_base(
        p_external_event_id,
        p_payload
    ) admission;

    if v_admission_outcome = 'inserted' then
        insert into public.commercial_ally_hotmart_event_bindings (
            webhook_event_id,
            scope_id,
            tenant_ref,
            funnel_ref,
            binding_version,
            hotmart_product_id,
            purchase_intent_product_ref,
            offer_ref
        ) values (
            v_event_id,
            v_scope.id,
            v_binding.tenant_ref,
            v_binding.funnel_ref,
            v_binding.binding_version,
            v_scope.hotmart_product_id,
            v_scope.purchase_intent_product_ref,
            v_scope.offer_ref
        );
    end if;

    select provenance.* into v_provenance
    from public.commercial_ally_hotmart_event_bindings provenance
    where provenance.webhook_event_id = v_event_id;
    if not found
       or v_provenance.scope_id is distinct from v_scope.id
       or v_provenance.tenant_ref is distinct from v_binding.tenant_ref
       or v_provenance.funnel_ref is distinct from v_binding.funnel_ref
       or v_provenance.binding_version is distinct from v_binding.binding_version
       or v_provenance.hotmart_product_id is distinct from v_scope.hotmart_product_id
       or v_provenance.purchase_intent_product_ref
            is distinct from v_scope.purchase_intent_product_ref
       or v_provenance.offer_ref is distinct from v_scope.offer_ref then
        raise exception using errcode = '22023',
            message = 'portable_hotmart_cart_replay_binding_mismatch';
    end if;

    if v_admission_outcome <> 'semantic_conflict' then
        perform public._admit_hotmart_purchase_intent_identity(
            v_event_id,
            p_normalized_email,
            p_normalized_phone
        );
        select correlation.outcome into strict v_correlation_outcome
        from public.correlate_hotmart_purchase_intent(v_event_id) correlation;
    end if;

    return query select v_admission_outcome, v_event_id;
end;
$function$;

create or replace function public.admit_portable_hotmart_purchase_approved(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
    p_external_event_id text,
    p_payload jsonb,
    p_normalized_email text,
    p_normalized_phone text
)
returns table (
    outcome text,
    webhook_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_policy public.commercial_ally_hotmart_purchase_policies%rowtype;
    v_existing public.portable_hotmart_purchase_correlations%rowtype;
    v_admission_outcome text;
    v_event_id uuid;
    v_email text;
    v_phone text;
    v_approved_at timestamptz;
    v_email_ids uuid[] := array[]::uuid[];
    v_phone_ids uuid[] := array[]::uuid[];
    v_candidate_ids uuid[] := array[]::uuid[];
    v_resolved_intent_id uuid;
    v_correlation_outcome text;
    v_matched_by text;
    v_reason_code text;
    v_candidate_count integer := 0;
begin
    if p_tenant_ref is null or nullif(btrim(p_tenant_ref), '') is null
       or p_funnel_ref is null or nullif(btrim(p_funnel_ref), '') is null
       or p_binding_version is null or p_binding_version < 1
       or p_external_event_id is null or nullif(btrim(p_external_event_id), '') is null
       or p_payload is null or jsonb_typeof(p_payload) <> 'object' then
        raise exception using errcode = '22023', message = 'invalid_portable_hotmart_purchase_input';
    end if;

    select binding.* into v_binding
    from public.commercial_ally_runtime_bindings binding
    where binding.tenant_ref = p_tenant_ref
      and binding.funnel_ref = p_funnel_ref
      and binding.binding_version = p_binding_version
      and binding.status = 'active'
    for update;
    if not found then
        raise exception using errcode = '22023', message = 'commercial_ally_binding_unavailable';
    end if;

    if p_payload #>> '{id}' is distinct from p_external_event_id
       or p_payload #>> '{event}' is distinct from 'PURCHASE_APPROVED'
       or p_payload #>> '{version}' is distinct from '2.0.0'
       or p_payload #>> '{data,purchase,status}' is distinct from 'APPROVED'
       or jsonb_typeof(p_payload #> '{data,product,id}') is distinct from 'number'
       or (p_payload #>> '{data,product,id}')::numeric
            is distinct from v_binding.hotmart_product_id::numeric
       or nullif(btrim(p_payload #>> '{data,purchase,offer,code}'), '') is null then
        raise exception using errcode = '22023', message = 'portable_hotmart_purchase_binding_mismatch';
    end if;

    select policy.* into v_policy
    from public.commercial_ally_hotmart_purchase_policies policy
    where policy.tenant_ref = v_binding.tenant_ref
      and policy.funnel_ref = v_binding.funnel_ref
      and policy.binding_version = v_binding.binding_version
      and policy.enabled
    for update;
    if not found then
        raise exception using errcode = '22023', message = 'portable_hotmart_purchase_policy_unavailable';
    end if;

    begin
        v_approved_at := to_timestamp(
            (p_payload #>> '{data,purchase,approved_date}')::numeric / 1000
        );
    exception when others then
        raise exception using errcode = '22023', message = 'portable_hotmart_purchase_invalid_approved_date';
    end;
    if v_approved_at is null then
        raise exception using errcode = '22023', message = 'portable_hotmart_purchase_invalid_approved_date';
    end if;

    select admission.outcome, admission.webhook_event_id
    into strict v_admission_outcome, v_event_id
    from public._admit_hotmart_purchase_approved_base(
        p_external_event_id, p_payload
    ) admission;

    if v_admission_outcome = 'semantic_conflict' then
        return query select v_admission_outcome, v_event_id;
        return;
    end if;

    select correlation.* into v_existing
    from public.portable_hotmart_purchase_correlations correlation
    where correlation.webhook_event_id = v_event_id;
    if found then
        if v_existing.tenant_ref is distinct from v_binding.tenant_ref
           or v_existing.funnel_ref is distinct from v_binding.funnel_ref
           or v_existing.binding_version is distinct from v_binding.binding_version then
            raise exception using errcode = '23514', message = 'portable_hotmart_purchase_replay_binding_conflict';
        end if;
        return query select v_admission_outcome, v_event_id;
        return;
    end if;
    if v_admission_outcome = 'duplicate' then
        raise exception using errcode = '23514', message = 'portable_hotmart_purchase_preexisting_admission';
    end if;

    perform public._admit_hotmart_purchase_intent_identity(
        v_event_id, p_normalized_email, p_normalized_phone
    );
    select identity.normalized_email, identity.normalized_phone
    into strict v_email, v_phone
    from public.hotmart_purchase_intent_event_identities identity
    where identity.webhook_event_id = v_event_id;

    perform intent.id
    from public.purchase_intents intent
    where intent.tenant_ref = v_binding.tenant_ref
      and intent.funnel_ref = v_binding.funnel_ref
      and intent.product_ref = v_binding.product_hotlink
      and intent.lifecycle_state = 'waiting_for_purchase'
      and intent.provider_observed
      and not intent.provisional
      and intent.submitted_at >= v_approved_at - v_policy.max_lookback
      and intent.submitted_at <= v_approved_at
      and ((v_email is not null and intent.normalized_email = v_email)
        or (v_phone is not null and intent.normalized_phone = v_phone))
    order by intent.id
    for update;

    select coalesce(array_agg(intent.id order by intent.id), array[]::uuid[])
    into v_email_ids
    from public.purchase_intents intent
    where intent.tenant_ref = v_binding.tenant_ref
      and intent.funnel_ref = v_binding.funnel_ref
      and intent.product_ref = v_binding.product_hotlink
      and intent.lifecycle_state = 'waiting_for_purchase'
      and intent.provider_observed and not intent.provisional
      and intent.submitted_at >= v_approved_at - v_policy.max_lookback
      and intent.submitted_at <= v_approved_at
      and v_email is not null and intent.normalized_email = v_email;

    select coalesce(array_agg(intent.id order by intent.id), array[]::uuid[])
    into v_phone_ids
    from public.purchase_intents intent
    where intent.tenant_ref = v_binding.tenant_ref
      and intent.funnel_ref = v_binding.funnel_ref
      and intent.product_ref = v_binding.product_hotlink
      and intent.lifecycle_state = 'waiting_for_purchase'
      and intent.provider_observed and not intent.provisional
      and intent.submitted_at >= v_approved_at - v_policy.max_lookback
      and intent.submitted_at <= v_approved_at
      and v_phone is not null and intent.normalized_phone = v_phone;

    select coalesce(array_agg(candidate_id order by candidate_id), array[]::uuid[])
    into v_candidate_ids
    from (
        select unnest(v_email_ids) as candidate_id
        union
        select unnest(v_phone_ids) as candidate_id
    ) candidates;
    v_candidate_count := cardinality(v_candidate_ids);

    if v_email is not null and v_phone is not null then
        if cardinality(v_email_ids) = 0 and cardinality(v_phone_ids) = 0 then
            v_correlation_outcome := 'unmatched';
            v_reason_code := 'identity_not_found';
        elsif cardinality(v_email_ids) = 1 and cardinality(v_phone_ids) = 1
          and v_email_ids[1] = v_phone_ids[1] then
            v_correlation_outcome := 'resolved';
            v_resolved_intent_id := v_email_ids[1];
            v_matched_by := 'email_and_phone';
            v_reason_code := 'exact_email_and_phone';
            v_candidate_count := 1;
        elsif cardinality(v_email_ids) = 0 or cardinality(v_phone_ids) = 0
          or not exists (
              select 1 from unnest(v_email_ids) email_id
              join unnest(v_phone_ids) phone_id on phone_id = email_id
          ) then
            v_correlation_outcome := 'conflict';
            v_reason_code := 'email_phone_conflict';
        else
            v_correlation_outcome := 'ambiguous';
            v_reason_code := 'multiple_candidates';
        end if;
    elsif v_email is not null then
        if cardinality(v_email_ids) = 0 then
            v_correlation_outcome := 'unmatched';
            v_reason_code := 'identity_not_found';
        elsif cardinality(v_email_ids) = 1 then
            v_correlation_outcome := 'resolved';
            v_resolved_intent_id := v_email_ids[1];
            v_matched_by := 'email';
            v_reason_code := 'exact_email';
            v_candidate_count := 1;
        else
            v_correlation_outcome := 'ambiguous';
            v_reason_code := 'multiple_candidates';
        end if;
    else
        if cardinality(v_phone_ids) = 0 then
            v_correlation_outcome := 'unmatched';
            v_reason_code := 'identity_not_found';
        elsif cardinality(v_phone_ids) = 1 then
            v_correlation_outcome := 'resolved';
            v_resolved_intent_id := v_phone_ids[1];
            v_matched_by := 'phone';
            v_reason_code := 'exact_phone';
            v_candidate_count := 1;
        else
            v_correlation_outcome := 'ambiguous';
            v_reason_code := 'multiple_candidates';
        end if;
    end if;

    insert into public.portable_hotmart_purchase_correlations (
        webhook_event_id, tenant_ref, funnel_ref, binding_version,
        policy_max_lookback, outcome, purchase_intent_id, matched_by,
        candidate_count, reason_code, observed_at
    ) values (
        v_event_id, v_binding.tenant_ref, v_binding.funnel_ref,
        v_binding.binding_version, v_policy.max_lookback,
        v_correlation_outcome, v_resolved_intent_id, v_matched_by,
        v_candidate_count, v_reason_code, v_approved_at
    );

    if cardinality(v_candidate_ids) > 0 then
        insert into public.portable_hotmart_purchase_correlation_candidates (
            webhook_event_id, purchase_intent_id, email_match, phone_match
        )
        select v_event_id, candidate_id,
               candidate_id = any(v_email_ids), candidate_id = any(v_phone_ids)
        from unnest(v_candidate_ids) candidate_id;
    end if;

    if v_correlation_outcome = 'resolved' then
        update public.purchase_intents
        set lifecycle_state = 'purchased',
            current_classification = null,
            activation_authorized = false,
            updated_at = clock_timestamp()
        where id = v_resolved_intent_id
          and lifecycle_state = 'waiting_for_purchase';
        if not found then
            raise exception using errcode = '40001', message = 'purchase_intent_changed_concurrently';
        end if;
        perform public.cancel_hotmart_abandonment_reevaluations_for_purchase(
            v_resolved_intent_id, clock_timestamp()
        );
    end if;

    return query select v_admission_outcome, v_event_id;
end;
$function$;

create or replace function public.admit_portable_hotmart_payment_failure(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
    p_external_event_id text,
    p_payload jsonb,
    p_normalized_email text,
    p_normalized_phone text
) returns table (outcome text, webhook_event_id uuid)
language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $function$
declare
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_scope public.hotmart_purchase_intent_scopes%rowtype;
    v_existing public.webhook_events%rowtype;
    v_event_id uuid;
    v_admission_outcome text;
    v_correlation record;
    v_payload_email text;
    v_payload_phone text;
begin
    if not public.hotmart_payment_failure_payload_is_processable(p_external_event_id, p_payload)
       or p_binding_version is null or p_binding_version < 1 then
        raise exception using errcode = '22023', message = 'invalid_portable_hotmart_payment_failure_input';
    end if;
    select b.* into v_binding from public.commercial_ally_runtime_bindings b
    where b.tenant_ref = p_tenant_ref and b.funnel_ref = p_funnel_ref
      and b.binding_version = p_binding_version and b.status = 'active' for update;
    if not found then raise exception using errcode='22023', message='commercial_ally_binding_unavailable'; end if;
    if (p_payload #>> '{data,product,id}')::numeric is distinct from v_binding.hotmart_product_id::numeric
       or p_payload #>> '{data,product,name}' is distinct from v_binding.product_name
       or not coalesce((p_payload #>> '{data,purchase,offer,code}' = v_binding.offer_code
            or p_payload #>> '{data,purchase,offer,code}' = any(v_binding.additional_offer_codes)), false) then
        raise exception using errcode='22023', message='portable_hotmart_payment_failure_binding_mismatch';
    end if;
    v_payload_email := lower(nullif(btrim(p_payload #>> '{data,buyer,email}'), ''));
    v_payload_phone := nullif(regexp_replace(coalesce(
        p_payload #>> '{data,buyer,checkout_phone}', p_payload #>> '{data,buyer,phone}', ''
    ), '[^0-9]', '', 'g'), '');
    if p_normalized_email is distinct from v_payload_email
       or p_normalized_phone is distinct from v_payload_phone then
        raise exception using errcode='22023', message='portable_hotmart_payment_failure_identity_mismatch';
    end if;
    select scope.* into v_scope from public.hotmart_purchase_intent_scopes scope
    where scope.tenant_ref=v_binding.tenant_ref and scope.funnel_ref=v_binding.funnel_ref
      and scope.hotmart_product_id=v_binding.hotmart_product_id::text
      and scope.purchase_intent_product_ref=v_binding.product_hotlink
      and scope.offer_ref=p_payload #>> '{data,purchase,offer,code}' and scope.active for update;
    if not found then raise exception using errcode='22023', message='portable_hotmart_payment_failure_scope_unavailable'; end if;
    perform pg_advisory_xact_lock(hashtextextended('payment-failure:' || p_external_event_id, 0));
    select event.* into v_existing from public.webhook_events event
    where event.source='hotmart' and event.external_event_id=p_external_event_id for update;
    if not found then
        insert into public.webhook_events(source,external_event_id,event_type,payload,processing_status)
        values ('hotmart',p_external_event_id,'PURCHASE_CANCELED',p_payload,'received') returning id into v_event_id;
        v_admission_outcome := 'inserted';
        insert into public.commercial_ally_hotmart_event_bindings(
            webhook_event_id,scope_id,tenant_ref,funnel_ref,binding_version,
            hotmart_product_id,purchase_intent_product_ref,offer_ref
        ) values (v_event_id,v_scope.id,v_binding.tenant_ref,v_binding.funnel_ref,
            v_binding.binding_version,v_scope.hotmart_product_id,
            v_scope.purchase_intent_product_ref,v_scope.offer_ref);
        insert into public.commercial_ally_payment_failure_details(
            webhook_event_id,transaction_ref,refusal_reason
        ) values (v_event_id,p_payload #>> '{data,purchase,transaction}',
            nullif(btrim(p_payload #>> '{data,purchase,payment,refusal_reason}'),''));
    elsif v_existing.event_type='PURCHASE_CANCELED' and v_existing.payload=p_payload then
        v_event_id := v_existing.id; v_admission_outcome := 'duplicate';
    else
        insert into public.commercial_ally_payment_failure_conflicts(
            existing_event_id,incoming_external_event_id,incoming_payload
        ) values (v_existing.id,p_external_event_id,p_payload) on conflict do nothing;
        return query select 'semantic_conflict'::text, v_existing.id; return;
    end if;
    if not exists (
        select 1 from public.commercial_ally_hotmart_event_bindings provenance
        where provenance.webhook_event_id=v_event_id and provenance.scope_id=v_scope.id
          and provenance.tenant_ref=v_binding.tenant_ref
          and provenance.funnel_ref=v_binding.funnel_ref
          and provenance.binding_version=v_binding.binding_version
    ) then raise exception using errcode='22023', message='portable_hotmart_payment_failure_replay_binding_mismatch'; end if;
    perform public._admit_hotmart_purchase_intent_identity(v_event_id,p_normalized_email,p_normalized_phone);
    select correlation.* into strict v_correlation from public.correlate_hotmart_purchase_intent(v_event_id) correlation;
    if v_admission_outcome='inserted' then
        perform set_config('app.payment_failure_evidence_finalize', 'on', true);
        update public.commercial_ally_payment_failure_details details set
            correlation_outcome=v_correlation.outcome,
            purchase_intent_id=v_correlation.purchase_intent_id,
            updated_at=clock_timestamp()
        where details.webhook_event_id=v_event_id;
    end if;
    return query select v_admission_outcome,v_event_id;
end;
$function$;
