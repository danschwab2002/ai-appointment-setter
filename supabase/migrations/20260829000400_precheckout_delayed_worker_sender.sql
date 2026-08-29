-- Expose the existing timer and one-shot command to the existing worker/sender.
-- This migration creates no effect row, publishes no scope, and activates no flag.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

alter table public.johanna_abandonment_one_shot_commands
    drop constraint johanna_abandonment_one_shot_commands_status_check,
    drop constraint johanna_abandonment_one_shot_commands_check,
    add constraint johanna_abandonment_one_shot_commands_status_check
        check (status in (
            'reserved', 'request_started', 'accepted_by_chatwoot', 'delivery_unknown'
        )),
    add constraint johanna_abandonment_one_shot_commands_check check (
        (status in ('reserved', 'request_started')
            and chatwoot_conversation_id is null
            and chatwoot_message_id is null
            and failure_code is null
            and finalized_at is null)
        or
        (status = 'accepted_by_chatwoot'
            and chatwoot_conversation_id is not null
            and chatwoot_conversation_id > 0
            and chatwoot_message_id is not null
            and chatwoot_message_id > 0
            and failure_code is null
            and finalized_at is not null)
        or
        (status = 'delivery_unknown'
            and failure_code is not null
            and nullif(btrim(failure_code), '') is not null
            and finalized_at is not null)
    );

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

    if old.status = 'reserved'
       and new.status = 'request_started'
       and old.invalid_contact_retry_count = new.invalid_contact_retry_count
       and new.chatwoot_conversation_id is null
       and new.chatwoot_message_id is null
       and new.failure_code is null
       and new.finalized_at is null then
        return new;
    end if;

    if old.status in ('reserved', 'request_started')
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

create or replace function public.list_due_hotmart_abandonment_reevaluations_v2(
    p_now timestamptz,
    p_batch_size integer,
    p_include_precheckout boolean
)
returns table (reevaluation_id uuid)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_now is null
       or p_batch_size is null
       or p_batch_size < 1
       or p_batch_size > 100
       or p_include_precheckout is null then
        raise exception using errcode = '22023',
            message = 'hotmart_abandonment_reevaluation_list_input_invalid';
    end if;

    return query
    select timer.id
    from public.hotmart_abandonment_reevaluations timer
    left join public.johanna_abandonment_one_shot_commands command
      on command.source_reevaluation_id = timer.id
    where (
        timer.status = 'scheduled'
        and timer.due_at <= p_now
        and (
            timer.source_kind = 'hotmart_event'
            or (
                p_include_precheckout
                and timer.source_kind = 'precheckout_intent'
            )
        )
      ) or (
        p_include_precheckout
        and timer.source_kind = 'precheckout_intent'
        and timer.status = 'completed'
        and timer.outcome = 'command_reserved'
        and command.status in ('reserved', 'request_started')
      )
    order by timer.due_at, timer.id
    limit p_batch_size;
end;
$function$;

create or replace function public.get_precheckout_delayed_one_shot_command(
    p_reevaluation_id uuid
)
returns table (
    command_id uuid,
    command_status text,
    target_phone text,
    buyer_name text,
    buyer_email text,
    product_name text,
    template_name text,
    template_language text,
    template_category text,
    copy_version text,
    send_authorized boolean,
    authorization_reason text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_row record;
    v_reason text;
    v_owner_count integer;
    v_blocked_owner_count integer;
    v_handoff_count integer;
    v_target_phone text;
begin
    if p_reevaluation_id is null then
        raise exception using errcode = '22023',
            message = 'precheckout_delayed_command_input_invalid';
    end if;

    select command.target_phone into v_target_phone
    from public.johanna_abandonment_one_shot_commands command
    where command.source_reevaluation_id = p_reevaluation_id;
    if not found then
        raise exception using errcode = 'P0002',
            message = 'precheckout_delayed_command_not_found';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        'johanna-abandonment-template-e2e-v2', 0
    ));

    perform 1
    from public.hotmart_abandonment_reevaluations timer
    join public.purchase_intents intent
      on intent.id = timer.purchase_intent_id
    where timer.id = p_reevaluation_id
      and timer.source_kind = 'precheckout_intent'
    for update of intent, timer;

    perform pg_advisory_xact_lock(hashtextextended(concat_ws(
        ':', 'chatwoot-opt-out-user', 1, v_target_phone
    ), 0));

    perform 1
    from public.contacts owner
    where owner.id in (
        select point.contact_id
        from public.contact_points point
        where point.type = 'phone'
          and point.normalized_value = v_target_phone
        union
        select identity.contact_id
        from public.channel_identities identity
        where identity.channel = 'whatsapp'
          and identity.account_id = 'chatwoot:1'
          and identity.external_user_id = v_target_phone
          and identity.identity_status = 'active'
          and identity.metadata ->> 'inbox_id' = '9'
    )
    order by owner.id
    for update of owner;

    perform 1
    from public.conversations conversation
    where conversation.contact_id in (
        select point.contact_id
        from public.contact_points point
        where point.type = 'phone'
          and point.normalized_value = v_target_phone
        union
        select identity.contact_id
        from public.channel_identities identity
        where identity.channel = 'whatsapp'
          and identity.account_id = 'chatwoot:1'
          and identity.external_user_id = v_target_phone
          and identity.identity_status = 'active'
          and identity.metadata ->> 'inbox_id' = '9'
    )
    order by conversation.id
    for update of conversation;

    perform pg_advisory_xact_lock(hashtextextended(
        'johanna-recovery-budget:' || v_target_phone, 0
    ));

    select
        command.id as command_id,
        command.status as command_status,
        command.target_phone,
        command.rollout_scope,
        command.scope_key,
        command.scope_version,
        command.runtime_generation,
        command.chatwoot_account_id,
        command.chatwoot_inbox_id,
        command.template_name,
        command.template_language,
        command.template_category,
        command.copy_version,
        command.max_messages,
        command.followups_allowed,
        intent.id as intent_id,
        intent.lifecycle_state,
        intent.current_classification,
        intent.tenant_ref,
        intent.funnel_ref,
        intent.landing_ref,
        intent.product_ref,
        intent.offer_ref,
        intent.normalized_email,
        intent.normalized_phone,
        intent.provisional as intent_provisional,
        intent.provider_observed as intent_provider_observed,
        intent.activation_authorized as intent_activation_authorized,
        intent.whatsapp_contact_authorized,
        submission.id as submission_id,
        submission.canonical_payload #>> '{lead,full_name}' as buyer_name,
        submission.canonical_payload #>> '{identity,email}' as buyer_email,
        submission.canonical_payload #>> '{commerce,product_name}' as product_name
    into v_row
    from public.hotmart_abandonment_reevaluations timer
    join public.johanna_abandonment_one_shot_commands command
      on command.source_reevaluation_id = timer.id
     and command.source_reevaluation_id = p_reevaluation_id
    join public.purchase_intents intent
      on intent.id = timer.purchase_intent_id
     and command.purchase_intent_id = intent.id
    left join lateral (
        select candidate.*
        from public.purchase_intent_submissions intent_submission
        join public.precheckout_submissions candidate
          on candidate.id = intent_submission.submission_id
        where intent_submission.purchase_intent_id = intent.id
          and candidate.contract_version = '1.1.0'
          and not candidate.provisional
          and candidate.provider_observed
          and candidate.activation_authorized
          and candidate.canonical_payload #>> '{event_type}' =
              'PRECHECKOUT_FORM_SUBMITTED'
          and candidate.canonical_payload #>> '{contract_version}' = '1.1.0'
          and candidate.canonical_payload #>> '{source,tenant_ref}' = intent.tenant_ref
          and candidate.canonical_payload #>> '{source,funnel_ref}' = intent.funnel_ref
          and candidate.canonical_payload #>> '{source,landing_ref}' = intent.landing_ref
          and candidate.canonical_payload #>> '{commerce,product_ref}' = intent.product_ref
          and candidate.canonical_payload #>> '{commerce,offer_ref}' = intent.offer_ref
          and candidate.canonical_payload #>> '{identity,phone}' = intent.normalized_phone
          and candidate.canonical_payload #>> '{identity,email}' = intent.normalized_email
          and candidate.canonical_payload #>> '{identity,phone_valid}' = 'true'
          and candidate.canonical_payload #>> '{consent,marketing_optin}' = 'true'
          and candidate.canonical_payload #>> '{consent,whatsapp_contact}' = 'true'
          and candidate.canonical_payload #>> '{consent,copy_version}' =
              'johanna-precheckout-whatsapp-disclosure-v1'
          and candidate.canonical_payload #>> '{assurance,provisional}' = 'false'
          and candidate.canonical_payload #>> '{assurance,provider_observed}' = 'true'
          and candidate.canonical_payload #>> '{assurance,activation_authorized}' = 'true'
          and not exists (
              select 1
              from public.precheckout_submission_conflicts conflict
              where conflict.existing_submission_id = candidate.id
                and conflict.resolved_at is null
          )
          and nullif(btrim(candidate.canonical_payload #>> '{submitted_at}'), '')
              is not null
        order by (candidate.canonical_payload #>> '{submitted_at}')::timestamptz desc,
                 intent_submission.ordinal desc,
                 candidate.id desc
        limit 1
    ) submission on true
    where timer.id = p_reevaluation_id
      and timer.source_kind = 'precheckout_intent'
      and timer.status = 'completed'
      and timer.outcome = 'command_reserved'
      and command.status in (
          'reserved', 'request_started', 'accepted_by_chatwoot', 'delivery_unknown'
      )
    for update of command, intent, timer;

    if not found then
        raise exception using errcode = 'P0002',
            message = 'precheckout_delayed_command_not_found';
    end if;

    if v_row.command_status = 'request_started' then
        update public.johanna_abandonment_one_shot_commands command
        set status = 'delivery_unknown',
            failure_code = 'precheckout_inflight_recovered',
            finalized_at = clock_timestamp()
        where command.id = v_row.command_id
          and command.status = 'request_started';
        v_row.command_status := 'delivery_unknown';
        v_reason := 'precheckout_inflight_recovered';
    elsif v_row.command_status in (
        'accepted_by_chatwoot', 'delivery_unknown'
    ) then
        v_reason := 'command_terminal';
    elsif v_row.rollout_scope <> 'johanna-precheckout-delayed-first-touch-v1'
       or v_row.scope_key <> 'johanna-precheckout-delayed-first-touch'
       or v_row.scope_version <> 1
       or v_row.runtime_generation <> 0
       or v_row.chatwoot_account_id <> 1
       or v_row.chatwoot_inbox_id <> 9
       or v_row.template_name <> 'johanna_interes_precheckout_01'
       or v_row.template_language <> 'es_EC'
       or v_row.template_category <> 'MARKETING'
       or v_row.copy_version <> 'johanna-precheckout-delayed-first-touch-v1'
       or v_row.max_messages <> 1
       or v_row.followups_allowed <> 0
       or v_row.target_phone is distinct from v_row.normalized_phone then
        v_reason := 'template_metadata_mismatch';
    elsif v_row.submission_id is null
       or nullif(btrim(v_row.buyer_name), '') is null
       or nullif(btrim(v_row.buyer_email), '') is null
       or nullif(btrim(v_row.product_name), '') is null then
        v_reason := 'blocked_not_authorized';
    elsif v_row.lifecycle_state = 'purchased' then
        v_reason := 'cancelled_purchased';
    elsif v_row.lifecycle_state <> 'waiting_for_purchase' then
        v_reason := 'cancelled_intent_changed';
    elsif v_row.current_classification in (
        'confirmed_abandonment', 'payment_failure_supported'
    ) or exists (
        select 1
        from public.hotmart_purchase_intent_correlations correlation
        where correlation.purchase_intent_id = v_row.intent_id
          and correlation.outcome = 'resolved'
          and not correlation.manual_handoff_required
          and correlation.event_type in (
              'PURCHASE_OUT_OF_SHOPPING_CART', 'PURCHASE_APPROVED'
          )
    ) or exists (
        select 1
        from public.operator_correlation_resolutions resolution
        where resolution.effective_purchase_intent_id = v_row.intent_id
          and resolution.resolution_outcome = 'linked_candidate'
    ) or exists (
        select 1
        from public.johanna_payment_failure_cases failure_case
        where failure_case.purchase_intent_id = v_row.intent_id
    ) then
        v_reason := 'superseded_by_provider_event';
    elsif v_row.current_classification is not null
       or exists (
            select 1
            from public.hotmart_purchase_intent_correlation_candidates candidate
            join public.hotmart_purchase_intent_correlations correlation
              on correlation.webhook_event_id = candidate.webhook_event_id
            where candidate.purchase_intent_id = v_row.intent_id
              and correlation.manual_handoff_required
       ) then
        v_reason := 'blocked_identity';
    elsif v_row.tenant_ref <> 'lancemos'
       or v_row.funnel_ref <> 'psicologajohanna'
       or v_row.landing_ref <> 'ads-a'
       or lower(v_row.product_ref) <> lower('F106691755G')
       or v_row.offer_ref <> 'bxjge6zq'
       or v_row.intent_provisional
       or not v_row.intent_provider_observed
       or not v_row.intent_activation_authorized
       or not v_row.whatsapp_contact_authorized then
        v_reason := 'blocked_not_authorized';
    elsif not exists (
        select 1
        from public.pilot_scope_versions scope
        join public.pilot_runtime_controls runtime
          on runtime.scope_key = scope.scope_key
         and runtime.scope_version = scope.version
        where scope.scope_key = 'johanna-precheckout-delayed-first-touch'
          and scope.version = 1
          and scope.status = 'published'
          and scope.tenant_key = 'lancemos'
          and scope.chatwoot_account_id = 1
          and scope.chatwoot_inbox_id = 9
          and scope.channel = 'whatsapp'
          and scope.channel_provider = 'waba'
          and scope.channel_account_ref = 'chatwoot-inbox:9'
          and scope.source = 'landing'
          and scope.source_event_type = 'PRECHECKOUT_FORM_SUBMITTED'
          and scope.external_product_id = 'F106691755G'
          and scope.offer_code = 'bxjge6zq'
          and scope.purpose = 'cart_recovery'
          and runtime.runtime_state = 'inactive'
          and runtime.generation = 0
    ) then
        v_reason := 'blocked_contact_binding_missing';
    elsif exists (
        select 1
        from public.contact_opt_out_events stop
        where stop.channel = 'whatsapp'
          and stop.purpose = 'cart_recovery'
          and stop.source = 'chatwoot'
          and stop.canonical_account_id = 1
          and stop.external_user_id = v_row.normalized_phone
    ) then
        v_reason := 'blocked_contact';
    end if;

    if v_reason is null then
        with owners as (
            select point.contact_id
            from public.contact_points point
            where point.type = 'phone'
              and point.normalized_value = v_row.normalized_phone
            union
            select identity.contact_id
            from public.channel_identities identity
            where identity.channel = 'whatsapp'
              and identity.account_id = 'chatwoot:1'
              and identity.external_user_id = v_row.normalized_phone
              and identity.identity_status = 'active'
              and identity.metadata ->> 'inbox_id' = '9'
        )
        select count(*)::integer,
               count(*) filter (
                   where contact.contact_permission in (
                       'opted_out', 'blocked', 'restricted'
                   ) or contact.lifecycle_status = 'do_not_contact'
               )::integer,
               count(*) filter (
                   where exists (
                       select 1
                       from public.conversations conversation
                       where conversation.contact_id = contact.id
                         and (
                             conversation.human_takeover
                             or conversation.status in (
                                 'paused_human', 'closed', 'blocked'
                             )
                             or conversation.automation_status in (
                                 'paused', 'disabled', 'restricted', 'error'
                             )
                         )
                   )
               )::integer
        into v_owner_count, v_blocked_owner_count, v_handoff_count
        from owners
        join public.contacts contact on contact.id = owners.contact_id;

        if v_owner_count > 1 then
            v_reason := 'blocked_identity';
        elsif v_blocked_owner_count > 0 then
            v_reason := 'blocked_contact';
        elsif v_handoff_count > 0 then
            v_reason := 'blocked_handoff';
        end if;
    end if;

    if v_row.command_status = 'reserved' then
        if v_reason is null then
            update public.johanna_abandonment_one_shot_commands command
            set status = 'request_started'
            where command.id = v_row.command_id
              and command.status = 'reserved';
            if not found then
                raise exception using errcode = '40001',
                    message = 'precheckout_request_start_conflict';
            end if;
            v_row.command_status := 'request_started';
        else
            update public.johanna_abandonment_one_shot_commands command
            set status = 'delivery_unknown',
                failure_code = v_reason,
                finalized_at = clock_timestamp()
            where command.id = v_row.command_id
              and command.status = 'reserved';
            if not found then
                raise exception using errcode = '40001',
                    message = 'precheckout_terminalization_conflict';
            end if;
            v_row.command_status := 'delivery_unknown';
        end if;
    end if;

    return query select
        v_row.command_id::uuid,
        v_row.command_status::text,
        v_row.target_phone::text,
        case when v_reason is null then v_row.buyer_name::text else null::text end,
        case when v_reason is null then v_row.buyer_email::text else null::text end,
        case when v_reason is null then v_row.product_name::text else null::text end,
        v_row.template_name::text,
        v_row.template_language::text,
        v_row.template_category::text,
        v_row.copy_version::text,
        v_reason is null,
        v_reason;
end;
$function$;

revoke all on function public.list_due_hotmart_abandonment_reevaluations_v2(timestamptz,integer,boolean)
from public;
revoke all on function public.get_precheckout_delayed_one_shot_command(uuid)
from public;

do $acl$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public.list_due_hotmart_abandonment_reevaluations_v2(timestamptz,integer,boolean)
        from anon;
        revoke all on function public.get_precheckout_delayed_one_shot_command(uuid)
        from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public.list_due_hotmart_abandonment_reevaluations_v2(timestamptz,integer,boolean)
        from authenticated;
        revoke all on function public.get_precheckout_delayed_one_shot_command(uuid)
        from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on function public.list_due_hotmart_abandonment_reevaluations_v2(timestamptz,integer,boolean)
        from service_role;
        revoke all on function public.get_precheckout_delayed_one_shot_command(uuid)
        from service_role;
        grant execute on function public.list_due_hotmart_abandonment_reevaluations_v2(timestamptz,integer,boolean)
        to service_role;
        grant execute on function public.get_precheckout_delayed_one_shot_command(uuid)
        to service_role;
    end if;
end;
$acl$;

do $postflight$
declare
    v_due regprocedure := to_regprocedure(
        'public.list_due_hotmart_abandonment_reevaluations_v2(timestamptz,integer,boolean)'
    );
    v_projection regprocedure := to_regprocedure(
        'public.get_precheckout_delayed_one_shot_command(uuid)'
    );
    v_due_definition text;
    v_projection_definition text;
begin
    if v_due is null or v_projection is null then
        raise exception 'precheckout_delayed_worker_rpc_missing';
    end if;
    select pg_get_functiondef(v_due) into strict v_due_definition;
    select pg_get_functiondef(v_projection)
    into strict v_projection_definition;
    if position('p_include_precheckout' in v_due_definition) = 0
       or position('precheckout_intent' in v_due_definition) = 0
       or position('source_reevaluation_id' in v_projection_definition) = 0
       or position('send_authorized' in v_projection_definition) = 0
       or position('cancelled_purchased' in v_projection_definition) = 0
       or position('johanna_interes_precheckout_01'
           in v_projection_definition) = 0 then
        raise exception 'precheckout_delayed_worker_rpc_postflight_failed';
    end if;
    if has_function_privilege('public', v_due, 'EXECUTE')
       or has_function_privilege('public', v_projection, 'EXECUTE') then
        raise exception 'precheckout_delayed_worker_rpc_acl_failed';
    end if;
end;
$postflight$;

commit;

