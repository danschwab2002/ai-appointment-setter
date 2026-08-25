-- Activate Johanna inbound identity scoping and a default-off durable
-- payment-failure first-touch command using the shared recovery budget.

begin;

create table public.johanna_payment_failure_cases (
    id uuid primary key default gen_random_uuid(),
    external_event_id text not null unique,
    semantic_fingerprint text not null,
    purchase_intent_id uuid references public.purchase_intents(id)
        on delete restrict,
    correlation_outcome text not null,
    case_status text not null default 'pending_human_review',
    transaction_ref text not null,
    normalized_email text,
    normalized_phone text,
    product_ref text not null,
    offer_ref text not null,
    purchase_status text not null,
    refusal_reason text not null,
    observed_at timestamptz not null,
    created_at timestamptz not null default clock_timestamp(),
    check (btrim(external_event_id) <> ''),
    check (semantic_fingerprint ~ '^[0-9a-f]{64}$'),
    check (correlation_outcome in (
        'resolved', 'unmatched', 'ambiguous', 'conflict'
    )),
    check (case_status = 'pending_human_review'),
    check (transaction_ref ~ '^HP[A-Z0-9]{6,62}$'),
    check (normalized_email is null or normalized_email = lower(btrim(normalized_email))),
    check (normalized_phone is null or normalized_phone ~ '^[1-9][0-9]{7,14}$'),
    check (product_ref = '8104005'),
    check (offer_ref = 'bxjge6zq'),
    check (purchase_status = 'CANCELLED'),
    check (refusal_reason = 'NO_FUNDS')
);

create index johanna_payment_failure_cases_review_idx
on public.johanna_payment_failure_cases (created_at, id)
where case_status = 'pending_human_review';

alter table public.johanna_payment_failure_cases
    drop constraint johanna_payment_failure_cases_case_status_check,
    add constraint johanna_payment_failure_cases_case_status_check
        check (case_status in (
            'pending_human_review',
            'outbound_started',
            'outbound_accepted',
            'delivery_unknown'
        )),
    add column outbound_command_id uuid;

alter table public.johanna_abandonment_one_shot_commands
    drop constraint johanna_abandonment_one_shot_commands_rollout_scope_key,
    drop constraint johanna_abandonment_one_shot_commands_rollout_scope_check,
    drop constraint johanna_abandonment_one_shot_commands_scope_version_check,
    drop constraint johanna_abandonment_one_shot_commands_runtime_generation_check,
    drop constraint johanna_abandonment_one_shot_commands_template_name_check,
    drop constraint johanna_abandonment_one_shot_commands_copy_version_check,
    add column payment_failure_case_id uuid unique
        references public.johanna_payment_failure_cases(id) on delete restrict,
    add constraint johanna_abandonment_one_shot_commands_rollout_scope_check
        check (rollout_scope in (
            'johanna-abandonment-template-e2e-v1',
            'johanna-abandonment-template-e2e-v2',
            'johanna-payment-failure-template-v1'
        )),
    add constraint johanna_abandonment_one_shot_commands_scope_version_check
        check (
            (rollout_scope = 'johanna-abandonment-template-e2e-v1'
                and scope_version = 1)
            or (rollout_scope = 'johanna-abandonment-template-e2e-v2'
                and scope_version = 2)
            or (rollout_scope = 'johanna-payment-failure-template-v1'
                and scope_version = 1)
        ),
    add constraint johanna_abandonment_one_shot_commands_runtime_generation_check
        check (
            (rollout_scope = 'johanna-abandonment-template-e2e-v1'
                and runtime_generation = 0)
            or (rollout_scope = 'johanna-abandonment-template-e2e-v2'
                and runtime_generation = 1)
            or (rollout_scope = 'johanna-payment-failure-template-v1'
                and runtime_generation = 0)
        ),
    add constraint johanna_abandonment_one_shot_commands_template_name_check
        check (template_name in (
            'johanna_carrito_abandonado_01',
            'johanna_compra_fallida_01'
        )),
    add constraint johanna_abandonment_one_shot_commands_copy_version_check
        check (copy_version in (
            'johanna-abandonment-one-shot-v1',
            'johanna-payment-failure-one-shot-v1'
        )),
    add constraint johanna_abandonment_one_shot_commands_failure_binding_check
        check (
            (rollout_scope = 'johanna-payment-failure-template-v1'
                and payment_failure_case_id is not null)
            or (rollout_scope <> 'johanna-payment-failure-template-v1'
                and payment_failure_case_id is null)
        );

alter table public.johanna_payment_failure_cases
    add constraint johanna_payment_failure_cases_outbound_command_fk
        foreign key (outbound_command_id)
        references public.johanna_abandonment_one_shot_commands(id)
        on delete restrict;

insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
) values (
    'johanna-payment-failure-single-touch', 1, 'published', 'cart_recovery',
    'UTC', '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
    interval '0 seconds', interval '1 day', 1,
    '[{"step_key":"first_contact","mode":"approved_template"}]'::jsonb,
    'operator-authorized-production-activation-20260825',
    clock_timestamp(), clock_timestamp()
);

alter table public.pilot_scope_versions
    drop constraint pilot_scope_versions_source_event_type_check,
    add constraint pilot_scope_versions_source_event_type_check
        check (source_event_type in (
            'PURCHASE_OUT_OF_SHOPPING_CART',
            'PURCHASE_CANCELED'
        ));

insert into public.pilot_scope_versions (
    scope_key, version, status, tenant_key, chatwoot_account_id,
    chatwoot_inbox_id, channel, channel_provider, channel_account_ref,
    source, source_event_type, external_product_id, offer_code, purpose,
    policy_key, policy_version, timezone, max_cohort_contacts,
    max_outbound_request_starts_total, max_outbound_request_starts_per_day,
    approved_by, approved_at, published_at
) values (
    'johanna-payment-failure-template', 1, 'published', 'lancemos', 1, 9,
    'whatsapp', 'waba', 'chatwoot-inbox:9', 'hotmart',
    'PURCHASE_CANCELED', '8104005', 'bxjge6zq', 'cart_recovery',
    'johanna-payment-failure-single-touch', 1, 'UTC', 100, 100, 25,
    'operator-authorized-production-activation-20260825',
    clock_timestamp(), clock_timestamp()
);

insert into public.pilot_runtime_controls (
    scope_key, scope_version, runtime_state, generation,
    changed_by, change_reason
) values (
    'johanna-payment-failure-template', 1, 'inactive', 0,
    'operator-authorized-production-activation-20260825',
    'Publish payment-failure template authorization; runtime HTTP gate remains independent.'
);

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
       or p_payload #>> '{data,purchase,status}' is distinct from 'CANCELLED'
       or p_payload #>> '{data,purchase,payment,refusal_reason}'
            is distinct from 'NO_FUNDS'
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
        'CANCELLED',
        'NO_FUNDS',
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
        'CANCELLED',
        'NO_FUNDS',
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
       or failure_case.purchase_status <> 'CANCELLED'
       or failure_case.refusal_reason <> 'NO_FUNDS' then
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

create or replace function public.finish_johanna_abandonment_one_shot(
    p_command_id uuid,
    p_outcome text,
    p_chatwoot_conversation_id bigint,
    p_chatwoot_message_id bigint,
    p_failure_code text
)
returns table (command_id uuid, command_status text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    command public.johanna_abandonment_one_shot_commands%rowtype;
begin
    if p_command_id is null
       or p_outcome not in ('accepted_by_chatwoot', 'delivery_unknown')
       or (p_outcome = 'accepted_by_chatwoot' and (
           p_chatwoot_conversation_id is null or p_chatwoot_conversation_id < 1
           or p_chatwoot_message_id is null or p_chatwoot_message_id < 1
           or p_failure_code is not null
       ))
       or (p_outcome = 'delivery_unknown' and (
           p_failure_code is null or nullif(btrim(p_failure_code), '') is null
       )) then
        raise exception using errcode = '22023',
            message = 'johanna_abandonment_one_shot_finish_invalid';
    end if;

    select cmd.* into strict command
    from public.johanna_abandonment_one_shot_commands cmd
    where cmd.id = p_command_id
    for update;
    if command.status = p_outcome then
        return query select command.id, command.status;
        return;
    end if;
    if command.status <> 'request_started' then
        raise exception using errcode = '23514',
            message = 'johanna_abandonment_one_shot_finish_conflict';
    end if;

    update public.johanna_abandonment_one_shot_commands
    set status = p_outcome,
        chatwoot_conversation_id = p_chatwoot_conversation_id,
        chatwoot_message_id = p_chatwoot_message_id,
        failure_code = case
            when p_outcome = 'delivery_unknown' then btrim(p_failure_code)
        end,
        finalized_at = clock_timestamp()
    where id = command.id;

    if command.payment_failure_case_id is not null then
        update public.johanna_payment_failure_cases
        set case_status = case
                when p_outcome = 'accepted_by_chatwoot' then 'outbound_accepted'
                else 'delivery_unknown'
            end
        where id = command.payment_failure_case_id
          and outbound_command_id = command.id;
    end if;

    return query select command.id, p_outcome;
end;
$function$;

create or replace function public.reconcile_johanna_abandonment_one_shot(
    p_command_key text,
    p_chatwoot_conversation_id bigint,
    p_chatwoot_message_id bigint
)
returns table (command_id uuid, command_status text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    command public.johanna_abandonment_one_shot_commands%rowtype;
begin
    if p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_chatwoot_conversation_id is null
       or p_chatwoot_conversation_id < 1
       or p_chatwoot_message_id is null
       or p_chatwoot_message_id < 1 then
        raise exception using errcode = '22023',
            message = 'johanna_abandonment_one_shot_reconcile_invalid';
    end if;

    select cmd.* into strict command
    from public.johanna_abandonment_one_shot_commands cmd
    where cmd.command_key = p_command_key
    for update;

    if command.status = 'accepted_by_chatwoot' then
        if command.chatwoot_conversation_id is distinct from p_chatwoot_conversation_id
           or command.chatwoot_message_id is distinct from p_chatwoot_message_id then
            raise exception using errcode = '23514',
                message = 'johanna_abandonment_one_shot_reconcile_conflict';
        end if;
        if command.payment_failure_case_id is not null then
            update public.johanna_payment_failure_cases
            set case_status = 'outbound_accepted'
            where id = command.payment_failure_case_id
              and outbound_command_id = command.id
              and case_status = 'delivery_unknown';
        end if;
        return query select command.id, command.status;
        return;
    end if;

    if command.status <> 'delivery_unknown' then
        raise exception using errcode = '23514',
            message = 'johanna_abandonment_one_shot_reconcile_conflict';
    end if;

    perform set_config('app.johanna_one_shot_reconciliation', 'on', true);

    update public.johanna_abandonment_one_shot_commands
    set status = 'accepted_by_chatwoot',
        chatwoot_conversation_id = p_chatwoot_conversation_id,
        chatwoot_message_id = p_chatwoot_message_id,
        failure_code = null,
        finalized_at = clock_timestamp()
    where id = command.id;

    if command.payment_failure_case_id is not null then
        update public.johanna_payment_failure_cases
        set case_status = 'outbound_accepted'
        where id = command.payment_failure_case_id
          and outbound_command_id = command.id
          and case_status = 'delivery_unknown';
    end if;

    return query select command.id, 'accepted_by_chatwoot'::text;
end;
$function$;

drop function public.claim_chatwoot_opt_out_projections(
    text, timestamptz, interval, integer
);

create function public.claim_chatwoot_opt_out_projections(
    p_worker_id text,
    p_now timestamptz,
    p_lease_duration interval,
    p_batch_size integer
)
returns table (
    opt_out_event_id uuid,
    chatwoot_account_id bigint,
    chatwoot_inbox_id bigint,
    chatwoot_conversation_id bigint,
    external_user_id text,
    lease_generation bigint
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_worker_id is null or btrim(p_worker_id) = ''
       or p_now is null
       or p_lease_duration is null or p_lease_duration <= interval '0 seconds'
       or p_batch_size is null or p_batch_size < 1 or p_batch_size > 100 then
        raise exception using errcode = '22023',
            message = 'invalid_chatwoot_opt_out_projection_claim_parameters';
    end if;

    return query
    with candidates as (
        select event.id
        from public.contact_opt_out_events event
        where event.projection_status in ('pending', 'retryable_failed')
          and (event.projection_next_attempt_at is null
               or event.projection_next_attempt_at <= p_now)
          and (event.projection_lease_expires_at is null
               or event.projection_lease_expires_at <= p_now)
        order by event.created_at, event.id
        for update skip locked
        limit p_batch_size
    ), claimed as (
        update public.contact_opt_out_events event
        set projection_lease_owner = p_worker_id,
            projection_lease_generation = event.projection_lease_generation + 1,
            projection_lease_expires_at = p_now + p_lease_duration
        from candidates
        where event.id = candidates.id
        returning event.id,
                  event.canonical_account_id,
                  event.canonical_inbox_id,
                  event.canonical_conversation_id,
                  event.external_user_id,
                  event.projection_lease_generation
    )
    select claimed.id,
           claimed.canonical_account_id,
           claimed.canonical_inbox_id,
           claimed.canonical_conversation_id,
           claimed.external_user_id,
           claimed.projection_lease_generation
    from claimed;
end;
$function$;

revoke all on function public.claim_chatwoot_opt_out_projections(
    text, timestamptz, interval, integer
) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public.claim_chatwoot_opt_out_projections(
            text, timestamptz, interval, integer
        ) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public.claim_chatwoot_opt_out_projections(
            text, timestamptz, interval, integer
        ) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.claim_chatwoot_opt_out_projections(
            text, timestamptz, interval, integer
        ) to service_role;
    end if;
end
$roles$;

-- The handoff worker must carry the canonical contact identity to every
-- Chatwoot reauthorization read. PostgreSQL cannot replace a table-returning
-- function when the return shape changes, so replace it transactionally.
drop function public.claim_human_handoff_projection_effects(text, integer, integer, timestamptz);

create function public.claim_human_handoff_projection_effects(
    p_worker_id text,
    p_limit integer default 10,
    p_lease_seconds integer default 60,
    p_now timestamptz default clock_timestamp()
)
returns table (
    effect_id uuid,
    handoff_request_id uuid,
    effect_kind text,
    current_effect_status text,
    attempt_count integer,
    lease_generation bigint,
    expected_team_id bigint,
    chatwoot_account_id bigint,
    chatwoot_inbox_id bigint,
    chatwoot_conversation_id bigint,
    external_user_id text,
    private_note_body text,
    idempotency_marker text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_now timestamptz := clock_timestamp();
begin
    if p_worker_id is null
       or btrim(p_worker_id) = ''
       or char_length(p_worker_id) > 200
       or p_limit is null or p_limit < 1 or p_limit > 100
       or p_lease_seconds is null
       or p_lease_seconds < 5 or p_lease_seconds > 900
       or p_now is null then
        raise exception using errcode = '22023',
            message = 'invalid_handoff_projection_claim_parameters';
    end if;

    return query
    with candidates as (
        select effect.id
        from public.human_handoff_projection_effects effect
        join public.human_handoff_requests request
          on request.id = effect.handoff_request_id
        join public.commercial_cases commercial_case
          on commercial_case.id = request.commercial_case_id
        left join public.recovery_cases recovery_case
          on recovery_case.id = request.recovery_case_id
         and recovery_case.commercial_case_id = commercial_case.id
        join public.conversations conversation
          on conversation.id = request.conversation_id
         and conversation.id = commercial_case.conversation_id
         and conversation.contact_id = commercial_case.contact_id
         and conversation.channel_identity_id = commercial_case.selected_channel_identity_id
         and conversation.status = 'paused_human'
         and conversation.automation_status = 'paused'
        join public.channel_identities identity
          on identity.id = commercial_case.selected_channel_identity_id
         and identity.contact_id = commercial_case.contact_id
         and identity.channel = 'whatsapp'
         and identity.identity_status = 'active'
        left join public.pilot_recovery_case_bindings pilot_binding
          on pilot_binding.recovery_case_id = request.recovery_case_id
         and pilot_binding.scope_key = request.scope_key
         and pilot_binding.scope_version = request.scope_version
        left join public.pilot_scope_versions pilot_scope
          on pilot_scope.scope_key = pilot_binding.scope_key
         and pilot_scope.version = pilot_binding.scope_version
         and pilot_scope.status = 'published'
        left join public.inbound_commercial_case_admissions inbound_admission
          on inbound_admission.commercial_case_id = commercial_case.id
         and inbound_admission.conversation_id = request.conversation_id
         and inbound_admission.channel_identity_id = identity.id
         and inbound_admission.scope_key = request.inbound_scope_key
         and inbound_admission.scope_version = request.inbound_scope_version
        left join public.inbound_commercial_scope_versions inbound_scope
          on inbound_scope.scope_key = inbound_admission.scope_key
         and inbound_scope.version = inbound_admission.scope_version
         and inbound_scope.status = 'published'
        where effect.effect_status in (
            'pending', 'retryable_failed', 'delivery_unknown'
        )
          and (effect.next_attempt_at is null or effect.next_attempt_at <= v_now)
          and (effect.lease_expires_at is null or effect.lease_expires_at <= v_now)
          and request.status in ('requested', 'projection_failed', 'dead_letter')
          and request.chatwoot_account_id > 0
          and request.chatwoot_inbox_id > 0
          and request.external_conversation_id > 0
          and conversation.commercial_context ->> 'chatwoot_conversation_id'
              = request.external_conversation_id::text
          and (
              (
                  request.recovery_case_id is not null
                  and commercial_case.case_kind = 'cart_recovery'
                  and commercial_case.id = request.recovery_case_id
                  and commercial_case.status = 'paused'
                  and commercial_case.automation_status = 'paused'
                  and recovery_case.status = 'paused'
                  and recovery_case.conversation_id = request.conversation_id
                  and pilot_scope.scope_key is not null
                  and request.inbound_scope_key is null
                  and identity.account_id =
                      'chatwoot:' || pilot_scope.chatwoot_account_id::text
                  and identity.metadata ->> 'inbox_id' =
                      pilot_scope.chatwoot_inbox_id::text
                  and request.chatwoot_account_id = pilot_scope.chatwoot_account_id
                  and request.chatwoot_inbox_id = pilot_scope.chatwoot_inbox_id
              )
              or
              (
                  request.recovery_case_id is null
                  and commercial_case.case_kind = 'inbound_sales'
                  and commercial_case.status = 'paused'
                  and commercial_case.automation_status = 'disabled'
                  and inbound_scope.scope_key is not null
                  and request.scope_key is null
                  and conversation.commercial_context = jsonb_build_object(
                      'chatwoot_conversation_id',
                      request.external_conversation_id::text
                  )
                  and identity.account_id =
                      'chatwoot:' || inbound_scope.chatwoot_account_id::text
                  and identity.metadata ->> 'inbox_id' =
                      inbound_scope.chatwoot_inbox_id::text
                  and request.chatwoot_account_id = inbound_scope.chatwoot_account_id
                  and request.chatwoot_inbox_id = inbound_scope.chatwoot_inbox_id
              )
          )
        order by
          request.created_at,
          case effect.effect_kind when 'assignment' then 0 else 1 end,
          effect.created_at,
          effect.id
        for update of effect skip locked
        limit p_limit
    ),
    claimed as (
        update public.human_handoff_projection_effects effect
        set lease_owner = p_worker_id,
            lease_generation = effect.lease_generation + 1,
            lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
            attempt_count = effect.attempt_count + 1,
            updated_at = v_now
        from candidates
        where effect.id = candidates.id
        returning effect.*
    )
    select
        claimed.id,
        request.id,
        claimed.effect_kind,
        claimed.effect_status,
        claimed.attempt_count,
        claimed.lease_generation,
        request.expected_team_id,
        request.chatwoot_account_id,
        request.chatwoot_inbox_id,
        request.external_conversation_id,
        identity.external_user_id,
        request.private_note_body,
        format(
            '[supportmagician-handoff:%s:%s:v%s]',
            request.id,
            request.note_template_key,
            request.note_template_version
        )
    from claimed
    join public.human_handoff_requests request
      on request.id = claimed.handoff_request_id
    join public.commercial_cases commercial_case
      on commercial_case.id = request.commercial_case_id
    join public.channel_identities identity
      on identity.id = commercial_case.selected_channel_identity_id
     and identity.contact_id = commercial_case.contact_id;
end;
$function$;

revoke all on table public.johanna_payment_failure_cases from public;
revoke all on function public.admit_johanna_payment_failure(text, jsonb, text, text) from public;
revoke all on function public.begin_johanna_payment_failure_hotmart_auto(text, uuid, bigint, bigint) from public;
revoke all on function public.claim_human_handoff_projection_effects(text, integer, integer, timestamptz) from public;

do $acl$
begin
    if to_regrole('anon') is not null then
        execute 'revoke all on table public.johanna_payment_failure_cases from anon';
        execute 'revoke all on function public.admit_johanna_payment_failure(text, jsonb, text, text) from anon';
        execute 'revoke all on function public.begin_johanna_payment_failure_hotmart_auto(text, uuid, bigint, bigint) from anon';
        execute 'revoke all on function public.claim_human_handoff_projection_effects(text, integer, integer, timestamptz) from anon';
    end if;
    if to_regrole('authenticated') is not null then
        execute 'revoke all on table public.johanna_payment_failure_cases from authenticated';
        execute 'revoke all on function public.admit_johanna_payment_failure(text, jsonb, text, text) from authenticated';
        execute 'revoke all on function public.begin_johanna_payment_failure_hotmart_auto(text, uuid, bigint, bigint) from authenticated';
        execute 'revoke all on function public.claim_human_handoff_projection_effects(text, integer, integer, timestamptz) from authenticated';
    end if;
    if to_regrole('service_role') is not null then
        execute 'revoke all on table public.johanna_payment_failure_cases from service_role';
        execute 'grant execute on function public.admit_johanna_payment_failure(text, jsonb, text, text) to service_role';
        execute 'grant execute on function public.begin_johanna_payment_failure_hotmart_auto(text, uuid, bigint, bigint) to service_role';
        execute 'grant execute on function public.claim_human_handoff_projection_effects(text, integer, integer, timestamptz) to service_role';
    end if;
end;
$acl$;

commit;
