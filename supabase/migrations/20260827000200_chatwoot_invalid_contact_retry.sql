-- Retry exactly one Johanna payment-failure command whose first Chatwoot
-- contact create succeeded but the legacy adapter could not parse payload.contact.id.
-- No other delivery_unknown outcome becomes retryable.

begin;

alter table public.johanna_abandonment_one_shot_commands
    add column invalid_contact_retry_count integer not null default 0
        check (invalid_contact_retry_count between 0 and 1);

create or replace function public.protect_johanna_abandonment_one_shot_command()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    old_identity jsonb;
    new_identity jsonb;
begin
    if tg_op = 'DELETE' then
        raise exception using errcode = '55000',
            message = 'johanna_abandonment_one_shot_command_immutable';
    end if;

    old_identity := to_jsonb(old) - array[
        'status', 'chatwoot_conversation_id', 'chatwoot_message_id',
        'failure_code', 'finalized_at', 'invalid_contact_retry_count'
    ];
    new_identity := to_jsonb(new) - array[
        'status', 'chatwoot_conversation_id', 'chatwoot_message_id',
        'failure_code', 'finalized_at', 'invalid_contact_retry_count'
    ];

    if old_identity is distinct from new_identity then
        raise exception using errcode = '55000',
            message = 'johanna_abandonment_one_shot_command_immutable';
    end if;

    if current_setting('app.johanna_one_shot_invalid_contact_retry', true) = 'on'
       and old.status = 'delivery_unknown'
       and new.status = 'request_started'
       and old.failure_code = 'invalid_contact_id'
       and old.chatwoot_conversation_id is null
       and old.chatwoot_message_id is null
       and old.finalized_at is not null
       and old.invalid_contact_retry_count = 0
       and new.chatwoot_conversation_id is null
       and new.chatwoot_message_id is null
       and new.failure_code is null
       and new.finalized_at is null
       and new.invalid_contact_retry_count = 1 then
        return new;
    end if;

    if current_setting('app.johanna_one_shot_reconciliation', true) = 'on'
       and old.status = 'delivery_unknown'
       and new.status = 'accepted_by_chatwoot'
       and old.invalid_contact_retry_count = new.invalid_contact_retry_count
       and new.chatwoot_conversation_id is not null
       and new.chatwoot_conversation_id > 0
       and new.chatwoot_message_id is not null
       and new.chatwoot_message_id > 0
       and new.failure_code is null
       and new.finalized_at is not null then
        return new;
    end if;

    if old.status = 'request_started'
       and new.status in ('accepted_by_chatwoot', 'delivery_unknown')
       and old.invalid_contact_retry_count = new.invalid_contact_retry_count
       and new.finalized_at is not null
       and (
           (new.status = 'accepted_by_chatwoot'
               and new.chatwoot_conversation_id is not null
               and new.chatwoot_conversation_id > 0
               and new.chatwoot_message_id is not null
               and new.chatwoot_message_id > 0
               and new.failure_code is null)
           or
           (new.status = 'delivery_unknown'
               and new.failure_code is not null
               and nullif(btrim(new.failure_code), '') is not null)
       ) then
        return new;
    end if;

    raise exception using errcode = '55000',
        message = 'johanna_abandonment_one_shot_command_immutable';
end;
$function$;

create or replace function public.prepare_johanna_payment_failure_invalid_contact_retry(
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
    command public.johanna_abandonment_one_shot_commands%rowtype;
    failure_case public.johanna_payment_failure_cases%rowtype;
    intent public.purchase_intents%rowtype;
    submission public.precheckout_submissions%rowtype;
    fingerprint text;
    phone_owner_count integer;
    blocked_owner_count integer;
    updated_count integer;
begin
    if p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_payment_failure_case_id is null
       or p_chatwoot_account_id is distinct from 1
       or p_chatwoot_inbox_id is distinct from 9 then
        raise exception using errcode = '22023',
            message = 'johanna_payment_failure_invalid_contact_retry_input_invalid';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        'johanna-payment-failure:' || p_payment_failure_case_id::text, 0
    ));

    select case_row.* into strict failure_case
    from public.johanna_payment_failure_cases case_row
    where case_row.id = p_payment_failure_case_id
    for update;

    select cmd.* into strict command
    from public.johanna_abandonment_one_shot_commands cmd
    where cmd.command_key = p_command_key
    for update;

    fingerprint := encode(sha256(convert_to(concat_ws(
        chr(31), p_payment_failure_case_id::text,
        failure_case.purchase_intent_id::text,
        p_chatwoot_account_id::text, p_chatwoot_inbox_id::text
    ), 'UTF8')), 'hex');

    if command.semantic_fingerprint is distinct from fingerprint
       or command.payment_failure_case_id is distinct from failure_case.id
       or failure_case.outbound_command_id is distinct from command.id then
        raise exception using errcode = '23514',
            message = 'johanna_payment_failure_invalid_contact_retry_conflict';
    end if;

    select ps.* into strict submission
    from public.purchase_intent_submissions link
    join public.precheckout_submissions ps on ps.id = link.submission_id
    where link.purchase_intent_id = command.purchase_intent_id
    order by link.ordinal desc
    limit 1;

    if failure_case.correlation_outcome <> 'resolved'
       or failure_case.case_status <> 'delivery_unknown'
       or command.status <> 'delivery_unknown'
       or command.failure_code is distinct from 'invalid_contact_id'
       or command.chatwoot_conversation_id is not null
       or command.chatwoot_message_id is not null
       or command.invalid_contact_retry_count <> 0 then
        return query select
            'not_retryable'::text, command.id, command.status,
            command.target_phone,
            submission.canonical_payload #>> '{lead,full_name}',
            submission.canonical_payload #>> '{identity,email}',
            submission.canonical_payload #>> '{commerce,product_name}',
            command.template_name, command.template_language,
            command.template_category, command.copy_version;
        return;
    end if;

    if failure_case.purchase_intent_id is null
       or failure_case.product_ref <> '8104005'
       or failure_case.offer_ref <> 'bxjge6zq'
       or failure_case.purchase_status <> 'CANCELED' then
        raise exception using errcode = '23514',
            message = 'johanna_payment_failure_invalid_contact_retry_case_not_authorized';
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
            message = 'johanna_payment_failure_invalid_contact_retry_scope_not_authorized';
    end if;

    select candidate.* into strict intent
    from public.purchase_intents candidate
    where candidate.id = failure_case.purchase_intent_id
    for update;

    if intent.id is distinct from command.purchase_intent_id
       or intent.tenant_ref <> 'lancemos'
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
       or intent.normalized_phone is distinct from command.target_phone
       or failure_case.observed_at < intent.submitted_at
       or failure_case.observed_at > intent.submitted_at + interval '24 hours' then
        raise exception using errcode = '23514',
            message = 'johanna_payment_failure_invalid_contact_retry_intent_not_authorized';
    end if;

    if submission.contract_version <> '1.1.0'
       or submission.provisional
       or not submission.provider_observed
       or not submission.activation_authorized
       or submission.canonical_payload #>> '{consent,marketing_optin}' <> 'true'
       or submission.canonical_payload #>> '{consent,whatsapp_contact}' <> 'true'
       or submission.canonical_payload #>> '{consent,copy_version}'
            <> 'johanna-precheckout-whatsapp-disclosure-v1'
       or submission.canonical_payload #>> '{identity,phone}'
            is distinct from intent.normalized_phone
       or submission.canonical_payload #>> '{commerce,offer_ref}' <> 'bxjge6zq'
       or nullif(btrim(submission.canonical_payload #>> '{lead,full_name}'), '') is null
       or nullif(btrim(submission.canonical_payload #>> '{commerce,product_name}'), '') is null
       or exists (
           select 1 from public.precheckout_submission_conflicts conflict
           where conflict.existing_submission_id = submission.id
             and conflict.resolved_at is null
       ) then
        raise exception using errcode = '23514',
            message = 'johanna_payment_failure_invalid_contact_retry_consent_not_authorized';
    end if;

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

    if exists (
        select 1
        from public.johanna_abandonment_one_shot_commands other_command
        where other_command.target_phone = intent.normalized_phone
          and other_command.id <> command.id
    ) then
        raise exception using errcode = '23514',
            message = 'johanna_payment_failure_invalid_contact_retry_budget_consumed';
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
            message = 'johanna_payment_failure_invalid_contact_retry_contact_blocked';
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
            message = 'johanna_payment_failure_invalid_contact_retry_phone_ambiguous';
    end if;
    if blocked_owner_count > 0 then
        raise exception using errcode = '23514',
            message = 'johanna_payment_failure_invalid_contact_retry_contact_blocked';
    end if;

    perform set_config(
        'app.johanna_one_shot_invalid_contact_retry', 'on', true
    );

    update public.johanna_abandonment_one_shot_commands
    set invalid_contact_retry_count = 1,
        status = 'request_started',
        chatwoot_conversation_id = null,
        chatwoot_message_id = null,
        failure_code = null,
        finalized_at = null
    where id = command.id;

    update public.johanna_payment_failure_cases
    set case_status = 'outbound_started'
    where id = failure_case.id
      and outbound_command_id = command.id
      and case_status = 'delivery_unknown';
    get diagnostics updated_count = row_count;
    if updated_count <> 1 then
        raise exception using errcode = '40001',
            message = 'johanna_payment_failure_invalid_contact_retry_case_race';
    end if;

    return query select
        'retry_started'::text, command.id, 'request_started'::text,
        command.target_phone,
        submission.canonical_payload #>> '{lead,full_name}',
        submission.canonical_payload #>> '{identity,email}',
        submission.canonical_payload #>> '{commerce,product_name}',
        command.template_name, command.template_language,
        command.template_category, command.copy_version;
end;
$function$;

revoke all on function public.prepare_johanna_payment_failure_invalid_contact_retry(text,uuid,bigint,bigint) from public;

do $roles$
begin
    if to_regrole('anon') is not null then
        execute 'revoke all on function public.prepare_johanna_payment_failure_invalid_contact_retry(text,uuid,bigint,bigint) from anon';
    end if;
    if to_regrole('authenticated') is not null then
        execute 'revoke all on function public.prepare_johanna_payment_failure_invalid_contact_retry(text,uuid,bigint,bigint) from authenticated';
    end if;
    if to_regrole('service_role') is not null then
        execute 'grant execute on function public.prepare_johanna_payment_failure_invalid_contact_retry(text,uuid,bigint,bigint) to service_role';
    end if;
end
$roles$;

commit;
