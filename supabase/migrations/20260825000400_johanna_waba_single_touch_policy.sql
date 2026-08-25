-- Publish and authorize one Hotmart-bound Johanna WABA automatic touch.

begin;

alter table public.johanna_abandonment_one_shot_commands
    add column hotmart_webhook_event_id uuid
        references public.webhook_events(id) on delete restrict;

alter table public.johanna_abandonment_one_shot_commands
    drop constraint johanna_abandonment_one_shot_commands_rollout_scope_check,
    drop constraint johanna_abandonment_one_shot_commands_scope_version_check,
    drop constraint johanna_abandonment_one_shot_commands_runtime_generation_check;

alter table public.johanna_abandonment_one_shot_commands
    add constraint johanna_abandonment_one_shot_commands_rollout_scope_check
        check (rollout_scope in (
            'johanna-abandonment-template-e2e-v1',
            'johanna-abandonment-template-e2e-v2'
        )),
    add constraint johanna_abandonment_one_shot_commands_scope_version_check
        check (
            (rollout_scope = 'johanna-abandonment-template-e2e-v1'
                and scope_version = 1
                and hotmart_webhook_event_id is null)
            or
            (rollout_scope = 'johanna-abandonment-template-e2e-v2'
                and scope_version = 2
                and hotmart_webhook_event_id is not null)
        ),
    add constraint johanna_abandonment_one_shot_commands_runtime_generation_check
        check (
            (rollout_scope = 'johanna-abandonment-template-e2e-v1'
                and runtime_generation = 0)
            or
            (rollout_scope = 'johanna-abandonment-template-e2e-v2'
                and runtime_generation = 1)
        );

create unique index johanna_abandonment_one_shot_commands_hotmart_event_idx
on public.johanna_abandonment_one_shot_commands (hotmart_webhook_event_id)
where hotmart_webhook_event_id is not null;

create unique index johanna_abandonment_one_shot_commands_target_phone_idx
on public.johanna_abandonment_one_shot_commands (target_phone);

create or replace function public.protect_johanna_abandonment_one_shot_command()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op = 'DELETE' then
        raise exception using errcode = '55000', message = 'johanna_abandonment_one_shot_command_immutable';
    end if;

    if current_setting('app.johanna_one_shot_reconciliation', true) = 'on'
       and old.status = 'delivery_unknown'
       and new.status = 'accepted_by_chatwoot'
       and old.id is not distinct from new.id
       and old.command_key is not distinct from new.command_key
       and old.semantic_fingerprint is not distinct from new.semantic_fingerprint
       and old.rollout_scope is not distinct from new.rollout_scope
       and old.purchase_intent_id is not distinct from new.purchase_intent_id
       and old.hotmart_webhook_event_id is not distinct from new.hotmart_webhook_event_id
       and old.scope_key is not distinct from new.scope_key
       and old.scope_version is not distinct from new.scope_version
       and old.runtime_generation is not distinct from new.runtime_generation
       and old.chatwoot_account_id is not distinct from new.chatwoot_account_id
       and old.chatwoot_inbox_id is not distinct from new.chatwoot_inbox_id
       and old.target_phone is not distinct from new.target_phone
       and old.template_name is not distinct from new.template_name
       and old.template_language is not distinct from new.template_language
       and old.template_category is not distinct from new.template_category
       and old.copy_version is not distinct from new.copy_version
       and old.max_messages is not distinct from new.max_messages
       and old.followups_allowed is not distinct from new.followups_allowed
       and old.created_at is not distinct from new.created_at
       and new.chatwoot_conversation_id is not null
       and new.chatwoot_conversation_id > 0
       and new.chatwoot_message_id is not null
       and new.chatwoot_message_id > 0
       and new.failure_code is null
       and new.finalized_at is not null then
        return new;
    end if;

    if old.id is distinct from new.id
       or old.command_key is distinct from new.command_key
       or old.semantic_fingerprint is distinct from new.semantic_fingerprint
       or old.rollout_scope is distinct from new.rollout_scope
       or old.purchase_intent_id is distinct from new.purchase_intent_id
       or old.hotmart_webhook_event_id is distinct from new.hotmart_webhook_event_id
       or old.scope_key is distinct from new.scope_key
       or old.scope_version is distinct from new.scope_version
       or old.runtime_generation is distinct from new.runtime_generation
       or old.chatwoot_account_id is distinct from new.chatwoot_account_id
       or old.chatwoot_inbox_id is distinct from new.chatwoot_inbox_id
       or old.target_phone is distinct from new.target_phone
       or old.template_name is distinct from new.template_name
       or old.template_language is distinct from new.template_language
       or old.template_category is distinct from new.template_category
       or old.copy_version is distinct from new.copy_version
       or old.max_messages is distinct from new.max_messages
       or old.followups_allowed is distinct from new.followups_allowed
       or old.created_at is distinct from new.created_at
       or old.status <> 'request_started'
       or new.status not in ('accepted_by_chatwoot', 'delivery_unknown') then
        raise exception using errcode = '55000', message = 'johanna_abandonment_one_shot_command_immutable';
    end if;
    return new;
end;
$function$;

do $preflight$
begin
    if exists (
        select 1 from public.followup_policy_versions
        where policy_key = 'johanna-abandonment-single-touch-e2e' and version = 2
    ) or exists (
        select 1 from public.pilot_scope_versions
        where scope_key = 'johanna-abandonment-template-e2e' and version = 2
    ) then
        raise exception using errcode = '55000', message = 'johanna_hotmart_auto_v2_already_exists';
    end if;
    if exists (
        select 1
        from public.pilot_runtime_controls
        where scope_key = 'johanna-abandonment-template-e2e'
          and not (
              scope_version = 1
              and runtime_state = 'inactive'
              and generation = 0
          )
    ) then
        raise exception using errcode = '55000', message = 'johanna_hotmart_auto_v1_control_not_ready';
    end if;
end;
$preflight$;

insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
) values (
    'johanna-abandonment-single-touch-e2e', 2, 'published', 'cart_recovery',
    'UTC', '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
    interval '0 seconds', interval '1 day', 1,
    '[{"step_key":"first_contact","mode":"approved_template"}]'::jsonb,
    'operator-authorized-production-activation-20260825',
    clock_timestamp(), clock_timestamp()
);

insert into public.pilot_scope_versions (
    scope_key, version, status, tenant_key, chatwoot_account_id,
    chatwoot_inbox_id, channel, channel_provider, channel_account_ref,
    source, source_event_type, external_product_id, offer_code, purpose,
    policy_key, policy_version, timezone, max_cohort_contacts,
    max_outbound_request_starts_total, max_outbound_request_starts_per_day,
    approved_by, approved_at, published_at
) values (
    'johanna-abandonment-template-e2e', 2, 'published', 'lancemos', 1, 9,
    'whatsapp', 'waba', 'chatwoot-inbox:9', 'hotmart',
    'PURCHASE_OUT_OF_SHOPPING_CART', '8104005', 'bxjge6zq', 'cart_recovery',
    'johanna-abandonment-single-touch-e2e', 2, 'UTC', 1, 1, 1,
    'operator-authorized-production-activation-20260825',
    clock_timestamp(), clock_timestamp()
);

do $promote$
begin
    if not exists (
        select 1 from public.pilot_runtime_controls
        where scope_key = 'johanna-abandonment-template-e2e'
    ) then
        insert into public.followup_policy_versions (
            policy_key, version, status, purpose, timezone, business_windows,
            grace_period, expires_after, max_automatic_messages, steps,
            approved_by, approved_at, published_at
        ) values (
            'johanna-abandonment-single-touch-e2e', 1, 'published',
            'cart_recovery', 'UTC',
            '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
            interval '0 seconds', interval '1 day', 1,
            '[{"step_key":"first_contact","mode":"approved_template"}]'::jsonb,
            'clean-stack-bootstrap-20260825', clock_timestamp(), clock_timestamp()
        ) on conflict (policy_key, version) do nothing;

        insert into public.pilot_scope_versions (
            scope_key, version, status, tenant_key, chatwoot_account_id,
            chatwoot_inbox_id, channel, channel_provider, channel_account_ref,
            source, source_event_type, external_product_id, offer_code, purpose,
            policy_key, policy_version, timezone, max_cohort_contacts,
            max_outbound_request_starts_total,
            max_outbound_request_starts_per_day,
            approved_by, approved_at, published_at
        ) values (
            'johanna-abandonment-template-e2e', 1, 'published', 'lancemos', 1, 9,
            'whatsapp', 'waba', 'chatwoot-inbox:9', 'hotmart',
            'PURCHASE_OUT_OF_SHOPPING_CART', '8104005', 'bxjge6zq',
            'cart_recovery', 'johanna-abandonment-single-touch-e2e', 1,
            'UTC', 1, 1, 1, 'clean-stack-bootstrap-20260825',
            clock_timestamp(), clock_timestamp()
        ) on conflict (scope_key, version) do nothing;

        insert into public.pilot_runtime_controls (
            scope_key, scope_version, runtime_state, generation,
            changed_by, change_reason
        ) values (
            'johanna-abandonment-template-e2e', 1, 'inactive', 0,
            'clean-stack-bootstrap-20260825',
            'Bootstrap V1 only to apply the audited V2 promotion.'
        );
    end if;

    perform * from public.activate_lancemos_pilot_scope_version(
        'johanna-abandonment-template-e2e', 2, 0,
        'operator-authorized-production-activation-20260825',
        'Promote dedicated Hotmart auto-trigger; general dispatcher remains disabled.'
    );
end;
$promote$;

create or replace function public.begin_johanna_abandonment_hotmart_auto(
    p_command_key text,
    p_hotmart_webhook_event_id uuid,
    p_purchase_intent_id uuid,
    p_allowed_external_user_id text,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_scope_key text,
    p_scope_version integer,
    p_expected_generation bigint
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
    intent public.purchase_intents%rowtype;
    submission public.precheckout_submissions%rowtype;
    published_scope public.pilot_scope_versions%rowtype;
    control public.pilot_runtime_controls%rowtype;
    correlation_row public.hotmart_purchase_intent_correlations%rowtype;
    command_id_value uuid;
    fingerprint text;
    phone_owner_count integer;
    blocked_owner_count integer;
begin
    if p_command_key is null or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_hotmart_webhook_event_id is null
       or p_purchase_intent_id is null
       or p_allowed_external_user_id is null
       or p_allowed_external_user_id !~ '^[1-9][0-9]{7,14}$'
       or p_chatwoot_account_id is distinct from 1
       or p_chatwoot_inbox_id is distinct from 9
       or p_scope_key is distinct from 'johanna-abandonment-template-e2e'
       or p_scope_version is distinct from 2
       or p_expected_generation is distinct from 1 then
        raise exception using errcode = '22023', message = 'johanna_abandonment_hotmart_auto_input_invalid';
    end if;

    fingerprint := encode(sha256(convert_to(concat_ws(
        chr(31), p_hotmart_webhook_event_id::text, p_purchase_intent_id::text,
        p_allowed_external_user_id, p_chatwoot_account_id::text,
        p_chatwoot_inbox_id::text, p_scope_key, p_scope_version::text,
        p_expected_generation::text
    ), 'UTF8')), 'hex');

    perform pg_advisory_xact_lock(hashtextextended('johanna-abandonment-template-e2e-v2', 0));

    select cmd.* into existing
    from public.johanna_abandonment_one_shot_commands cmd
    where cmd.command_key = p_command_key
    for update;

    if found then
        if existing.semantic_fingerprint is distinct from fingerprint then
            raise exception using errcode = '23514', message = 'johanna_abandonment_hotmart_auto_command_conflict';
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

    select scope.* into strict published_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published'
      and scope.tenant_key = 'lancemos'
      and scope.channel_provider = 'waba'
      and scope.channel_account_ref = 'chatwoot-inbox:9'
      and scope.chatwoot_account_id = p_chatwoot_account_id
      and scope.chatwoot_inbox_id = p_chatwoot_inbox_id
      and scope.source = 'hotmart'
      and scope.source_event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
      and scope.external_product_id = '8104005'
      and scope.offer_code = 'bxjge6zq'
      and scope.max_cohort_contacts = 1
      and scope.max_outbound_request_starts_total = 1
      and scope.max_outbound_request_starts_per_day = 1
    for share;

    select runtime.* into strict control
    from public.pilot_runtime_controls runtime
    where runtime.scope_key = p_scope_key and runtime.scope_version = p_scope_version
    for update;

    if control.runtime_state <> 'inactive' then
        raise exception using errcode = '55000', message = 'johanna_abandonment_hotmart_auto_runtime_not_inactive';
    end if;
    if control.generation is distinct from p_expected_generation then
        raise exception using errcode = '40001', message = 'johanna_abandonment_hotmart_auto_generation_mismatch';
    end if;

    select correlation.* into strict correlation_row
    from public.hotmart_purchase_intent_correlations correlation
    join public.hotmart_purchase_intent_scopes source_scope
      on source_scope.id = correlation.scope_id
    where correlation.webhook_event_id = p_hotmart_webhook_event_id
      and correlation.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
      and correlation.outcome = 'resolved'
      and correlation.purchase_intent_id = p_purchase_intent_id
      and correlation.candidate_count = 1
      and not correlation.manual_handoff_required
      and source_scope.active
      and source_scope.tenant_ref = 'lancemos'
      and source_scope.funnel_ref = 'psicologajohanna'
      and source_scope.hotmart_product_id = '8104005'
      and source_scope.purchase_intent_product_ref = 'f106691755g'
      and source_scope.offer_ref = 'bxjge6zq'
    for share of correlation, source_scope;

    select candidate.* into strict intent
    from public.purchase_intents candidate
    where candidate.id = p_purchase_intent_id
    for update;

    if intent.tenant_ref <> 'lancemos'
       or intent.funnel_ref <> 'psicologajohanna'
       or intent.landing_ref <> 'ads-a'
       or intent.product_ref <> 'F106691755G'
       or intent.offer_ref <> 'bxjge6zq'
       or intent.lifecycle_state <> 'waiting_for_purchase'
       or intent.provisional
       or not intent.provider_observed
       or not intent.whatsapp_contact_authorized
       or not intent.activation_authorized
       or intent.current_classification <> 'confirmed_abandonment'
       or intent.normalized_phone is distinct from p_allowed_external_user_id then
        raise exception using errcode = '23514', message = 'johanna_abandonment_hotmart_auto_intent_not_authorized';
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
          where conflict.existing_submission_id = ps.id and conflict.resolved_at is null
      )
    order by link.ordinal desc
    limit 1;

    select cmd.* into budget_command
    from public.johanna_abandonment_one_shot_commands cmd
    where cmd.target_phone = p_allowed_external_user_id
    for update;

    if found then
        select ps.* into strict submission
        from public.purchase_intent_submissions link
        join public.precheckout_submissions ps on ps.id = link.submission_id
        where link.purchase_intent_id = budget_command.purchase_intent_id
        order by link.ordinal desc
        limit 1;
        return query select 'budget_consumed'::text, budget_command.id,
            budget_command.status, budget_command.target_phone,
            submission.canonical_payload #>> '{lead,full_name}',
            submission.canonical_payload #>> '{identity,email}',
            submission.canonical_payload #>> '{commerce,product_name}',
            budget_command.template_name, budget_command.template_language,
            budget_command.template_category, budget_command.copy_version;
        return;
    end if;

    perform pg_advisory_xact_lock(hashtextextended(concat_ws(
        ':', 'chatwoot-opt-out-user', p_chatwoot_account_id, intent.normalized_phone
    ), 0));

    perform 1
    from public.contacts owner
    where owner.id in (
        select point.contact_id from public.contact_points point
        where point.type = 'phone' and point.normalized_value = intent.normalized_phone
        union
        select identity.contact_id from public.channel_identities identity
        where identity.channel = 'whatsapp'
          and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
          and identity.external_user_id = intent.normalized_phone
          and identity.identity_status = 'active'
          and identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
    )
    order by owner.id
    for update of owner;

    perform 1
    from public.channel_identities identity
    where identity.channel = 'whatsapp'
      and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
      and identity.external_user_id = intent.normalized_phone
      and identity.identity_status = 'active'
      and identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
    order by identity.id
    for update of identity;

    if exists (
        select 1 from public.contact_opt_out_events stop
        where stop.channel = 'whatsapp'
          and stop.purpose = 'cart_recovery'
          and stop.source = 'chatwoot'
          and stop.canonical_account_id = p_chatwoot_account_id
          and stop.external_user_id = intent.normalized_phone
    ) then
        raise exception using errcode = '23514', message = 'johanna_abandonment_hotmart_auto_contact_blocked';
    end if;

    select count(distinct point.contact_id)::integer,
           count(distinct point.contact_id) filter (
               where owner.contact_permission in ('opted_out', 'blocked', 'restricted')
                  or owner.lifecycle_status = 'do_not_contact'
           )::integer
    into phone_owner_count, blocked_owner_count
    from public.contact_points point
    join public.contacts owner on owner.id = point.contact_id
    where point.type = 'phone' and point.normalized_value = intent.normalized_phone;

    if phone_owner_count > 1 then
        raise exception using errcode = '23514', message = 'johanna_abandonment_hotmart_auto_phone_ambiguous';
    end if;
    if blocked_owner_count > 0 then
        raise exception using errcode = '23514', message = 'johanna_abandonment_hotmart_auto_contact_blocked';
    end if;

    insert into public.johanna_abandonment_one_shot_commands (
        command_key, semantic_fingerprint, rollout_scope, purchase_intent_id,
        hotmart_webhook_event_id, scope_key, scope_version, runtime_generation,
        chatwoot_account_id, chatwoot_inbox_id, target_phone,
        template_name, template_language, template_category, copy_version,
        max_messages, followups_allowed, status
    ) values (
        p_command_key, fingerprint, 'johanna-abandonment-template-e2e-v2', intent.id,
        p_hotmart_webhook_event_id, p_scope_key, p_scope_version, p_expected_generation,
        p_chatwoot_account_id, p_chatwoot_inbox_id, intent.normalized_phone,
        'johanna_carrito_abandonado_01', 'es_EC', 'MARKETING',
        'johanna-abandonment-one-shot-v1', 1, 0, 'request_started'
    ) returning id into command_id_value;

    return query select 'started'::text, command_id_value, 'request_started'::text,
        intent.normalized_phone,
        submission.canonical_payload #>> '{lead,full_name}',
        submission.canonical_payload #>> '{identity,email}',
        submission.canonical_payload #>> '{commerce,product_name}',
        'johanna_carrito_abandonado_01'::text, 'es_EC'::text,
        'MARKETING'::text, 'johanna-abandonment-one-shot-v1'::text;
end;
$function$;

revoke all on function public.begin_johanna_abandonment_hotmart_auto(text,uuid,uuid,text,bigint,bigint,text,integer,bigint) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public.begin_johanna_abandonment_hotmart_auto(text,uuid,uuid,text,bigint,bigint,text,integer,bigint) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public.begin_johanna_abandonment_hotmart_auto(text,uuid,uuid,text,bigint,bigint,text,integer,bigint) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.begin_johanna_abandonment_hotmart_auto(text,uuid,uuid,text,bigint,bigint,text,integer,bigint) to service_role;
    end if;
end
$roles$;

commit;
