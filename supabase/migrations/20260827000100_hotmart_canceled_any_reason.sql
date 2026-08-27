-- Accept the observed Hotmart v2 PURCHASE_CANCELED contract regardless of
-- the provider-specific card refusal text. All existing scope, correlation,
-- consent, budget, opt-out, takeover and idempotency gates remain unchanged.

begin;

alter table public.johanna_payment_failure_cases
    drop constraint johanna_payment_failure_cases_purchase_status_check,
    drop constraint johanna_payment_failure_cases_refusal_reason_check,
    alter column refusal_reason drop not null,
    add constraint johanna_payment_failure_cases_purchase_status_check
        check (purchase_status in ('CANCELLED', 'CANCELED'));

create or replace function public.admit_johanna_payment_failure(
    p_external_event_id text,
    p_payload jsonb,
    p_normalized_email text,
    p_normalized_phone text
)
returns table (
    outcome text,
    payment_failure_case_id uuid,
    correlation_outcome text,
    case_status text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_existing public.johanna_payment_failure_cases%rowtype;
    v_case_id uuid;
    v_creation_ms bigint;
    v_transaction text;
    v_email text;
    v_phone text;
    v_raw_phone text;
    v_product_ref text;
    v_offer_ref text;
    v_refusal_reason text;
    v_fingerprint text;
    v_candidate_count integer;
    v_candidate_id uuid;
    v_candidate public.purchase_intents%rowtype;
    v_correlation text;
begin
    if p_external_event_id is null
       or btrim(p_external_event_id) = ''
       or p_payload is null
       or jsonb_typeof(p_payload) <> 'object'
       or p_payload #>> '{id}' is distinct from p_external_event_id
       or jsonb_typeof(p_payload #> '{id}') is distinct from 'string'
       or p_payload #>> '{event}' is distinct from 'PURCHASE_CANCELED'
       or p_payload #>> '{version}' is distinct from '2.0.0'
       or p_payload #>> '{data,purchase,status}' is distinct from 'CANCELED'
       or jsonb_typeof(p_payload #> '{creation_date}') is distinct from 'number'
       or jsonb_typeof(p_payload #> '{data,purchase,transaction}')
            is distinct from 'string'
       or jsonb_typeof(p_payload #> '{data,product,id}') is distinct from 'number'
       or jsonb_typeof(p_payload #> '{data,purchase,offer,code}')
            is distinct from 'string' then
        raise exception using errcode = '22023',
            message = 'invalid_johanna_payment_failure_payload';
    end if;

    begin
        v_creation_ms := (p_payload #>> '{creation_date}')::bigint;
        v_product_ref := (p_payload #>> '{data,product,id}')::bigint::text;
    exception when others then
        raise exception using errcode = '22023',
            message = 'invalid_johanna_payment_failure_payload';
    end;

    v_transaction := nullif(btrim(
        p_payload #>> '{data,purchase,transaction}'
    ), '');
    v_offer_ref := nullif(btrim(
        p_payload #>> '{data,purchase,offer,code}'
    ), '');
    v_refusal_reason := p_payload #>> '{data,purchase,payment,refusal_reason}';
    v_email := nullif(lower(btrim(p_payload #>> '{data,buyer,email}')), '');

    v_raw_phone := p_payload #>> '{data,buyer,checkout_phone}';
    if v_raw_phone is null or v_raw_phone !~ '^\+?[0-9 ()-]+$' then
        v_raw_phone := p_payload #>> '{data,buyer,phone}';
    end if;
    if v_raw_phone is not null and v_raw_phone ~ '^\+?[0-9 ()-]+$' then
        v_phone := nullif(regexp_replace(v_raw_phone, '[^0-9]', '', 'g'), '');
    end if;

    if v_creation_ms <= 0
       or v_transaction is null
       or v_transaction !~ '^HP[A-Z0-9]{6,62}$'
       or v_product_ref is distinct from '8104005'
       or v_offer_ref is distinct from 'bxjge6zq'
       or (v_email is null and v_phone is null)
       or (v_email is not null and v_email !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$')
       or (v_phone is not null and v_phone !~ '^[1-9][0-9]{7,14}$')
       or p_normalized_email is distinct from v_email
       or p_normalized_phone is distinct from v_phone then
        raise exception using errcode = '22023',
            message = 'invalid_johanna_payment_failure_scope';
    end if;

    v_fingerprint := encode(sha256(convert_to(concat_ws(
        chr(31),
        v_transaction,
        'CANCELED',
        coalesce(v_refusal_reason, ''),
        v_product_ref,
        v_offer_ref,
        coalesce(v_email, ''),
        coalesce(v_phone, '')
    ), 'UTF8')), 'hex');

    perform pg_advisory_xact_lock(hashtextextended(
        'johanna-payment-failure:' || p_external_event_id,
        0
    ));

    select payment_case.* into v_existing
    from public.johanna_payment_failure_cases payment_case
    where payment_case.external_event_id = p_external_event_id
    for update;

    if found then
        if v_existing.semantic_fingerprint = v_fingerprint then
            return query select
                'duplicate'::text,
                v_existing.id,
                v_existing.correlation_outcome,
                v_existing.case_status;
        else
            return query select
                'semantic_conflict'::text,
                v_existing.id,
                v_existing.correlation_outcome,
                v_existing.case_status;
        end if;
        return;
    end if;

    perform pg_advisory_xact_lock(hashtextextended(concat_ws(
        chr(31),
        'johanna-payment-failure-identity',
        coalesce(v_email, ''),
        coalesce(v_phone, '')
    ), 0));

    select
        count(*)::integer,
        (array_agg(intent.id order by intent.id))[1]
    into v_candidate_count, v_candidate_id
    from public.purchase_intents intent
    where intent.lifecycle_state = 'waiting_for_purchase'
      and intent.tenant_ref = 'lancemos'
      and intent.funnel_ref = 'psicologajohanna'
      and intent.landing_ref = 'ads-a'
      and lower(intent.product_ref) = 'f106691755g'
      and intent.offer_ref = 'bxjge6zq'
      and (
          (v_email is not null and intent.normalized_email = v_email)
          or
          (v_phone is not null and intent.normalized_phone = v_phone)
      );

    if v_candidate_count = 0 then
        v_correlation := 'unmatched';
        v_candidate_id := null;
    elsif v_candidate_count > 1 then
        v_correlation := 'ambiguous';
        v_candidate_id := null;
    else
        select intent.* into strict v_candidate
        from public.purchase_intents intent
        where intent.id = v_candidate_id
        for update;

        if (v_email is not null and v_candidate.normalized_email is distinct from v_email)
           or (v_phone is not null and v_candidate.normalized_phone is distinct from v_phone) then
            v_correlation := 'conflict';
            v_candidate_id := null;
        else
            v_correlation := 'resolved';
            update public.purchase_intents intent
            set current_classification = 'payment_failure_supported',
                updated_at = clock_timestamp()
            where intent.id = v_candidate.id
              and intent.lifecycle_state = 'waiting_for_purchase';
            if not found then
                v_correlation := 'conflict';
                v_candidate_id := null;
            end if;
        end if;
    end if;

    insert into public.johanna_payment_failure_cases (
        external_event_id,
        semantic_fingerprint,
        purchase_intent_id,
        correlation_outcome,
        case_status,
        transaction_ref,
        normalized_email,
        normalized_phone,
        product_ref,
        offer_ref,
        purchase_status,
        refusal_reason,
        observed_at
    ) values (
        p_external_event_id,
        v_fingerprint,
        v_candidate_id,
        v_correlation,
        'pending_human_review',
        v_transaction,
        v_email,
        v_phone,
        v_product_ref,
        v_offer_ref,
        'CANCELED',
        v_refusal_reason,
        to_timestamp(v_creation_ms / 1000.0)
    ) returning id into v_case_id;

    return query select
        'inserted'::text,
        v_case_id,
        v_correlation,
        'pending_human_review'::text;
end;
$function$;

create or replace function public.begin_johanna_payment_failure_hotmart_auto(
    p_command_key text,
    p_payment_failure_case_id uuid,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint
)
returns table (
    outcome text,
    command_id uuid,
    command_status text,
    target_phone text,
    buyer_name text,
    buyer_email text,
    product_name text,
    template_name text,
    template_language text,
    template_category text,
    copy_version text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    existing public.johanna_abandonment_one_shot_commands%rowtype;
    budget_command public.johanna_abandonment_one_shot_commands%rowtype;
    failure_case public.johanna_payment_failure_cases%rowtype;
    intent public.purchase_intents%rowtype;
    submission public.precheckout_submissions%rowtype;
    command_id_value uuid;
    fingerprint text;
    phone_owner_count integer;
    blocked_owner_count integer;
begin
    if p_command_key is null or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_payment_failure_case_id is null
       or p_chatwoot_account_id is distinct from 1
       or p_chatwoot_inbox_id is distinct from 9 then
        raise exception using errcode = '22023',
            message = 'johanna_payment_failure_hotmart_auto_input_invalid';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        'johanna-payment-failure:' || p_payment_failure_case_id::text, 0
    ));

    select case_row.* into strict failure_case
    from public.johanna_payment_failure_cases case_row
    where case_row.id = p_payment_failure_case_id
    for update;

    fingerprint := encode(sha256(convert_to(concat_ws(
        chr(31), p_payment_failure_case_id::text,
        failure_case.purchase_intent_id::text,
        p_chatwoot_account_id::text, p_chatwoot_inbox_id::text
    ), 'UTF8')), 'hex');

    select cmd.* into existing
    from public.johanna_abandonment_one_shot_commands cmd
    where cmd.command_key = p_command_key
    for update;

    if found then
        if existing.semantic_fingerprint is distinct from fingerprint
           or existing.payment_failure_case_id is distinct from failure_case.id then
            raise exception using errcode = '23514',
                message = 'johanna_payment_failure_hotmart_auto_command_conflict';
        end if;
        select ps.* into strict submission
        from public.purchase_intent_submissions link
        join public.precheckout_submissions ps on ps.id = link.submission_id
        where link.purchase_intent_id = existing.purchase_intent_id
        order by link.ordinal desc
        limit 1;
        return query select 'replay'::text, existing.id, existing.status,
            existing.target_phone,
            submission.canonical_payload #>> '{lead,full_name}',
            submission.canonical_payload #>> '{identity,email}',
            submission.canonical_payload #>> '{commerce,product_name}',
            existing.template_name, existing.template_language,
            existing.template_category, existing.copy_version;
        return;
    end if;

    if failure_case.correlation_outcome <> 'resolved'
       or failure_case.case_status <> 'pending_human_review'
       or failure_case.purchase_intent_id is null
       or failure_case.product_ref <> '8104005'
       or failure_case.offer_ref <> 'bxjge6zq'
       or failure_case.purchase_status <> 'CANCELED' then
        raise exception using errcode = '23514',
            message = 'johanna_payment_failure_hotmart_auto_case_not_authorized';
    end if;

    perform 1
    from public.pilot_scope_versions scope
    join public.pilot_runtime_controls runtime
      on runtime.scope_key = scope.scope_key
     and runtime.scope_version = scope.version
    where scope.scope_key = 'johanna-payment-failure-template'
      and scope.version = 1
      and scope.status = 'published'
      and scope.tenant_key = 'lancemos'
      and scope.chatwoot_account_id = p_chatwoot_account_id
      and scope.chatwoot_inbox_id = p_chatwoot_inbox_id
      and scope.channel_provider = 'waba'
      and scope.channel_account_ref = 'chatwoot-inbox:9'
      and scope.source = 'hotmart'
      and scope.source_event_type = 'PURCHASE_CANCELED'
      and scope.external_product_id = '8104005'
      and scope.offer_code = 'bxjge6zq'
      and runtime.runtime_state = 'inactive'
      and runtime.generation = 0
    for share of scope, runtime;
    if not found then
        raise exception using errcode = '55000',
            message = 'johanna_payment_failure_hotmart_auto_scope_not_authorized';
    end if;

    select candidate.* into strict intent
    from public.purchase_intents candidate
    where candidate.id = failure_case.purchase_intent_id
    for update;

    if intent.tenant_ref <> 'lancemos'
       or intent.funnel_ref <> 'psicologajohanna'
       or intent.landing_ref <> 'ads-a'
       or lower(intent.product_ref) <> 'f106691755g'
       or intent.offer_ref <> 'bxjge6zq'
       or intent.lifecycle_state <> 'waiting_for_purchase'
       or intent.provisional
       or not intent.provider_observed
       or not intent.whatsapp_contact_authorized
       or not intent.activation_authorized
       or intent.current_classification <> 'payment_failure_supported'
       or intent.normalized_phone is null
       or intent.normalized_phone is distinct from failure_case.normalized_phone
       or failure_case.observed_at < intent.submitted_at
       or failure_case.observed_at > intent.submitted_at + interval '24 hours' then
        raise exception using errcode = '23514',
            message = 'johanna_payment_failure_hotmart_auto_intent_not_authorized';
    end if;

    select ps.* into strict submission
    from public.purchase_intent_submissions link
    join public.precheckout_submissions ps on ps.id = link.submission_id
    where link.purchase_intent_id = intent.id
      and ps.contract_version = '1.1.0'
      and not ps.provisional
      and ps.provider_observed
      and ps.activation_authorized
      and ps.canonical_payload #>> '{consent,marketing_optin}' = 'true'
      and ps.canonical_payload #>> '{consent,whatsapp_contact}' = 'true'
      and ps.canonical_payload #>> '{consent,copy_version}' = 'johanna-precheckout-whatsapp-disclosure-v1'
      and ps.canonical_payload #>> '{identity,phone}' = intent.normalized_phone
      and ps.canonical_payload #>> '{commerce,offer_ref}' = 'bxjge6zq'
      and nullif(btrim(ps.canonical_payload #>> '{lead,full_name}'), '') is not null
      and nullif(btrim(ps.canonical_payload #>> '{commerce,product_name}'), '') is not null
      and not exists (
          select 1 from public.precheckout_submission_conflicts conflict
          where conflict.existing_submission_id = ps.id
            and conflict.resolved_at is null
      )
    order by link.ordinal desc
    limit 1;

    perform pg_advisory_xact_lock(hashtextextended(
        concat_ws(':',
            'chatwoot-opt-out-user',
            p_chatwoot_account_id,
            intent.normalized_phone
        ),
        0
    ));

    perform 1
    from public.channel_identities identity
    where identity.channel = 'whatsapp'
      and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
      and identity.external_user_id = intent.normalized_phone
    order by identity.id
    for update of identity;

    perform 1
    from public.contact_points point
    join public.contacts owner on owner.id = point.contact_id
    where point.type = 'phone'
      and point.normalized_value = intent.normalized_phone
    order by point.id, owner.id
    for update of point, owner;

    perform pg_advisory_xact_lock(hashtextextended(
        'johanna-recovery-budget:' || intent.normalized_phone, 0
    ));

    select cmd.* into budget_command
    from public.johanna_abandonment_one_shot_commands cmd
    where cmd.target_phone = intent.normalized_phone
    for update;
    if found then
        return query select 'budget_consumed'::text, budget_command.id,
            budget_command.status, intent.normalized_phone,
            submission.canonical_payload #>> '{lead,full_name}',
            submission.canonical_payload #>> '{identity,email}',
            submission.canonical_payload #>> '{commerce,product_name}',
            'johanna_compra_fallida_01'::text, 'es_EC'::text,
            'MARKETING'::text, 'johanna-payment-failure-one-shot-v1'::text;
        return;
    end if;

    if exists (
        select 1 from public.contact_opt_out_events stop
        where stop.channel = 'whatsapp'
          and stop.purpose = 'cart_recovery'
          and stop.source = 'chatwoot'
          and stop.canonical_account_id = p_chatwoot_account_id
          and stop.external_user_id = intent.normalized_phone
    ) then
        raise exception using errcode = '23514',
            message = 'johanna_payment_failure_hotmart_auto_contact_blocked';
    end if;

    select count(distinct point.contact_id)::integer,
           count(distinct point.contact_id) filter (
               where owner.contact_permission in ('opted_out', 'blocked', 'restricted')
                  or owner.lifecycle_status = 'do_not_contact'
           )::integer
    into phone_owner_count, blocked_owner_count
    from public.contact_points point
    join public.contacts owner on owner.id = point.contact_id
    where point.type = 'phone'
      and point.normalized_value = intent.normalized_phone;
    if phone_owner_count > 1 then
        raise exception using errcode = '23514',
            message = 'johanna_payment_failure_hotmart_auto_phone_ambiguous';
    end if;
    if blocked_owner_count > 0 then
        raise exception using errcode = '23514',
            message = 'johanna_payment_failure_hotmart_auto_contact_blocked';
    end if;

    insert into public.johanna_abandonment_one_shot_commands (
        command_key, semantic_fingerprint, rollout_scope, purchase_intent_id,
        hotmart_webhook_event_id, payment_failure_case_id,
        scope_key, scope_version, runtime_generation,
        chatwoot_account_id, chatwoot_inbox_id, target_phone,
        template_name, template_language, template_category, copy_version,
        max_messages, followups_allowed, status
    ) values (
        p_command_key, fingerprint, 'johanna-payment-failure-template-v1', intent.id,
        null, failure_case.id, 'johanna-payment-failure-template', 1, 0,
        p_chatwoot_account_id, p_chatwoot_inbox_id, intent.normalized_phone,
        'johanna_compra_fallida_01', 'es_EC', 'MARKETING',
        'johanna-payment-failure-one-shot-v1', 1, 0, 'request_started'
    ) returning id into command_id_value;

    update public.johanna_payment_failure_cases
    set case_status = 'outbound_started',
        outbound_command_id = command_id_value
    where id = failure_case.id;

    return query select 'started'::text, command_id_value,
        'request_started'::text, intent.normalized_phone,
        submission.canonical_payload #>> '{lead,full_name}',
        submission.canonical_payload #>> '{identity,email}',
        submission.canonical_payload #>> '{commerce,product_name}',
        'johanna_compra_fallida_01'::text, 'es_EC'::text,
        'MARKETING'::text, 'johanna-payment-failure-one-shot-v1'::text;
end;
$function$;

commit;
