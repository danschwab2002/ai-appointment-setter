-- Reserve a delayed precheckout first-touch in the existing Johanna physical
-- command ledger. No worker due-list, sender call, template publication,
-- scope publication, or runtime activation is added here.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

alter table public.pilot_scope_versions
    drop constraint pilot_scope_versions_source_check,
    drop constraint pilot_scope_versions_source_event_type_check,
    add constraint pilot_scope_versions_source_check
        check (source in ('hotmart', 'landing')),
    add constraint pilot_scope_versions_source_event_type_check
        check (source_event_type in (
            'PURCHASE_OUT_OF_SHOPPING_CART',
            'PURCHASE_CANCELED',
            'PRECHECKOUT_FORM_SUBMITTED'
        ));

alter table public.johanna_abandonment_one_shot_commands
    add column source_reevaluation_id uuid unique
        references public.hotmart_abandonment_reevaluations(id) on delete restrict;

alter table public.johanna_abandonment_one_shot_commands
    drop constraint johanna_abandonment_one_shot_commands_rollout_scope_check,
    drop constraint johanna_abandonment_one_shot_commands_scope_version_check,
    drop constraint johanna_abandonment_one_shot_commands_runtime_generation_check,
    drop constraint johanna_abandonment_one_shot_commands_template_name_check,
    drop constraint johanna_abandonment_one_shot_commands_copy_version_check,
    add constraint johanna_abandonment_one_shot_commands_rollout_scope_check
        check (rollout_scope in (
            'johanna-abandonment-template-e2e-v1',
            'johanna-abandonment-template-e2e-v2',
            'johanna-payment-failure-template-v1',
            'johanna-precheckout-delayed-first-touch-v1'
        )),
    add constraint johanna_abandonment_one_shot_commands_scope_version_check
        check (
            (rollout_scope = 'johanna-abandonment-template-e2e-v1'
                and scope_version = 1)
            or (rollout_scope = 'johanna-abandonment-template-e2e-v2'
                and scope_version = 2)
            or (rollout_scope = 'johanna-payment-failure-template-v1'
                and scope_version = 1)
            or (rollout_scope = 'johanna-precheckout-delayed-first-touch-v1'
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
            or (rollout_scope = 'johanna-precheckout-delayed-first-touch-v1'
                and runtime_generation = 0)
        ),
    add constraint johanna_abandonment_one_shot_commands_template_name_check
        check (template_name in (
            'johanna_carrito_abandonado_01',
            'johanna_compra_fallida_01',
            'johanna_interes_precheckout_01'
        )),
    add constraint johanna_abandonment_one_shot_commands_copy_version_check
        check (copy_version in (
            'johanna-abandonment-one-shot-v1',
            'johanna-payment-failure-one-shot-v1',
            'johanna-precheckout-delayed-first-touch-v1'
        )),
    add constraint johanna_abandonment_one_shot_commands_source_binding_check
        check (
            (rollout_scope = 'johanna-abandonment-template-e2e-v1'
                and hotmart_webhook_event_id is null
                and payment_failure_case_id is null
                and source_reevaluation_id is null)
            or (rollout_scope = 'johanna-abandonment-template-e2e-v2'
                and hotmart_webhook_event_id is not null
                and payment_failure_case_id is null
                and source_reevaluation_id is null)
            or (rollout_scope = 'johanna-payment-failure-template-v1'
                and hotmart_webhook_event_id is null
                and payment_failure_case_id is not null
                and source_reevaluation_id is null)
            or (rollout_scope = 'johanna-precheckout-delayed-first-touch-v1'
                and hotmart_webhook_event_id is null
                and payment_failure_case_id is null
                and source_reevaluation_id is not null)
        ),
    add constraint johanna_abandonment_one_shot_commands_route_metadata_check
        check (
            (rollout_scope in (
                'johanna-abandonment-template-e2e-v1',
                'johanna-abandonment-template-e2e-v2'
            )
                and template_name = 'johanna_carrito_abandonado_01'
                and copy_version = 'johanna-abandonment-one-shot-v1')
            or (rollout_scope = 'johanna-payment-failure-template-v1'
                and template_name = 'johanna_compra_fallida_01'
                and copy_version = 'johanna-payment-failure-one-shot-v1')
            or (rollout_scope = 'johanna-precheckout-delayed-first-touch-v1'
                and template_name = 'johanna_interes_precheckout_01'
                and copy_version = 'johanna-precheckout-delayed-first-touch-v1')
        );

alter table public.hotmart_abandonment_reevaluations
    drop constraint hotmart_abandonment_reevaluations_outcome_check,
    add constraint hotmart_abandonment_reevaluations_outcome_check
        check (outcome in (
            'cancelled_purchased',
            'blocked_not_authorized',
            'blocked_contact_binding_missing',
            'cancelled_intent_changed',
            'superseded_by_provider_event',
            'blocked_contact',
            'blocked_identity',
            'blocked_handoff',
            'budget_consumed',
            'command_reserved'
        ));

create or replace function public._reevaluate_precheckout_delayed_first_touch(
    p_reevaluation_id uuid,
    p_now timestamptz
)
returns table (
    reevaluation_id uuid,
    reevaluation_status text,
    reevaluation_outcome text,
    completed_at timestamptz,
    replayed boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_purchase_intent_id uuid;
    v_intent public.purchase_intents%rowtype;
    v_reevaluation public.hotmart_abandonment_reevaluations%rowtype;
    v_submission public.precheckout_submissions%rowtype;
    v_scope public.pilot_scope_versions%rowtype;
    v_runtime public.pilot_runtime_controls%rowtype;
    v_budget_command public.johanna_abandonment_one_shot_commands%rowtype;
    v_command_id uuid;
    v_fingerprint text;
    v_outcome text;
    v_owner_count integer;
    v_blocked_owner_count integer;
    v_handoff_count integer;
begin
    if p_reevaluation_id is null or p_now is null then
        raise exception using errcode = '22023',
            message = 'precheckout_delayed_reevaluation_input_invalid';
    end if;

    -- Same global command fence used by the automatic Hotmart V2 begin RPC.
    perform pg_advisory_xact_lock(hashtextextended('johanna-abandonment-template-e2e-v2', 0));

    select timer.purchase_intent_id into v_purchase_intent_id
    from public.hotmart_abandonment_reevaluations timer
    where timer.id = p_reevaluation_id
      and timer.source_kind = 'precheckout_intent';
    if not found then
        raise exception using errcode = 'P0002',
            message = 'precheckout_delayed_reevaluation_not_found';
    end if;

    -- Canonical order after the global fence: intent, timer, source submission.
    select intent.* into strict v_intent
    from public.purchase_intents intent
    where intent.id = v_purchase_intent_id
    for update;

    select timer.* into strict v_reevaluation
    from public.hotmart_abandonment_reevaluations timer
    where timer.id = p_reevaluation_id
      and timer.purchase_intent_id = v_intent.id
      and timer.source_kind = 'precheckout_intent'
    for update;

    if v_reevaluation.status = 'completed' then
        return query select v_reevaluation.id, v_reevaluation.status,
            v_reevaluation.outcome, v_reevaluation.completed_at, true;
        return;
    end if;
    if v_reevaluation.due_at > p_now then
        raise exception using errcode = '55000',
            message = 'hotmart_abandonment_reevaluation_not_due';
    end if;

    select submission.* into v_submission
    from public.precheckout_submissions submission
    join public.purchase_intent_submissions link
      on link.submission_id = submission.id
     and link.purchase_intent_id = v_intent.id
    where submission.id = v_reevaluation.source_submission_id
    for share of submission;
    if not found then
        v_outcome := 'blocked_identity';
    end if;

    if v_outcome is null and v_intent.lifecycle_state = 'purchased' then
        v_outcome := 'cancelled_purchased';
    elsif v_outcome is null and v_intent.lifecycle_state <> 'waiting_for_purchase' then
        v_outcome := 'cancelled_intent_changed';
    end if;

    if v_outcome is null and (
        v_intent.current_classification in (
            'confirmed_abandonment', 'payment_failure_supported'
        )
        or exists (
            select 1
            from public.hotmart_purchase_intent_correlations correlation
            where correlation.purchase_intent_id = v_intent.id
              and correlation.outcome = 'resolved'
              and not correlation.manual_handoff_required
              and correlation.event_type in (
                  'PURCHASE_OUT_OF_SHOPPING_CART', 'PURCHASE_APPROVED'
              )
        )
        or exists (
            select 1
            from public.operator_correlation_resolutions resolution
            where resolution.effective_purchase_intent_id = v_intent.id
              and resolution.resolution_outcome = 'linked_candidate'
        )
        or exists (
            select 1
            from public.johanna_payment_failure_cases failure_case
            where failure_case.purchase_intent_id = v_intent.id
        )
    ) then
        v_outcome := 'superseded_by_provider_event';
    end if;

    if v_outcome is null and (
        v_intent.current_classification is not null
        or exists (
            select 1
            from public.hotmart_purchase_intent_correlation_candidates candidate
            join public.hotmart_purchase_intent_correlations correlation
              on correlation.webhook_event_id = candidate.webhook_event_id
            where candidate.purchase_intent_id = v_intent.id
              and correlation.manual_handoff_required
        )
    ) then
        v_outcome := 'blocked_identity';
    end if;

    if v_outcome is null and (
        v_submission.contract_version is distinct from '1.1.0'
        or v_submission.provisional
        or not v_submission.provider_observed
        or not v_submission.activation_authorized
        or v_submission.canonical_payload #>> '{consent,marketing_optin}'
           is distinct from 'true'
        or v_submission.canonical_payload #>> '{consent,whatsapp_contact}'
           is distinct from 'true'
        or v_submission.canonical_payload #>> '{consent,copy_version}'
           is distinct from 'johanna-precheckout-whatsapp-disclosure-v1'
        or v_submission.canonical_payload #>> '{identity,email}'
           is distinct from v_intent.normalized_email
        or v_submission.canonical_payload #>> '{identity,phone}'
           is distinct from v_intent.normalized_phone
        or v_intent.tenant_ref is distinct from 'lancemos'
        or v_intent.funnel_ref is distinct from 'psicologajohanna'
        or v_intent.landing_ref is distinct from 'ads-a'
        or lower(v_intent.product_ref) is distinct from lower('F106691755G')
        or v_intent.offer_ref is distinct from 'bxjge6zq'
        or v_intent.provisional
        or not v_intent.provider_observed
        or not v_intent.activation_authorized
        or not v_intent.whatsapp_contact_authorized
        or v_intent.normalized_phone is null
        or exists (
            select 1 from public.precheckout_submission_conflicts conflict
            where conflict.existing_submission_id = v_submission.id
              and conflict.resolved_at is null
        )
    ) then
        v_outcome := 'blocked_not_authorized';
    end if;

    if v_outcome is null then
        select scope.* into v_scope
        from public.pilot_scope_versions scope
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
        for share;
        if not found then
            v_outcome := 'blocked_contact_binding_missing';
        end if;
    end if;

    if v_outcome is null then
        select runtime.* into v_runtime
        from public.pilot_runtime_controls runtime
        where runtime.scope_key = v_scope.scope_key
          and runtime.scope_version = v_scope.version
          and runtime.runtime_state = 'inactive'
          and runtime.generation = 0
        for update;
        if not found then
            v_outcome := 'blocked_contact_binding_missing';
        end if;
    end if;

    if v_outcome is null then
        -- Serialize with the canonical inbound opt-out writer.
        perform pg_advisory_xact_lock(hashtextextended(concat_ws(
            ':', 'chatwoot-opt-out-user', 1, v_intent.normalized_phone
        ), 0));

        perform 1
        from public.contacts owner
        where owner.id in (
            select point.contact_id
            from public.contact_points point
            where point.type = 'phone'
              and point.normalized_value = v_intent.normalized_phone
            union
            select identity.contact_id
            from public.channel_identities identity
            where identity.channel = 'whatsapp'
              and identity.account_id = 'chatwoot:1'
              and identity.external_user_id = v_intent.normalized_phone
              and identity.identity_status = 'active'
              and identity.metadata ->> 'inbox_id' = '9'
        )
        order by owner.id
        for update of owner;

        if exists (
            select 1
            from public.contact_opt_out_events stop
            where stop.channel = 'whatsapp'
              and stop.purpose = 'cart_recovery'
              and stop.source = 'chatwoot'
              and stop.canonical_account_id = 1
              and stop.external_user_id = v_intent.normalized_phone
        ) then
            v_outcome := 'blocked_contact';
        end if;
    end if;

    if v_outcome is null then
        with owners as (
            select point.contact_id
            from public.contact_points point
            where point.type = 'phone'
              and point.normalized_value = v_intent.normalized_phone
            union
            select identity.contact_id
            from public.channel_identities identity
            where identity.channel = 'whatsapp'
              and identity.account_id = 'chatwoot:1'
              and identity.external_user_id = v_intent.normalized_phone
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
                       select 1 from public.conversations conversation
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
            v_outcome := 'blocked_identity';
        elsif v_blocked_owner_count > 0 then
            v_outcome := 'blocked_contact';
        elsif v_handoff_count > 0 then
            v_outcome := 'blocked_handoff';
        end if;
    end if;

    if v_outcome is null then
        -- Shared recipient fence used by Hotmart abandonment and payment failure.
        perform pg_advisory_xact_lock(hashtextextended(
            'johanna-recovery-budget:' || v_intent.normalized_phone, 0
        ));

        select cmd.* into v_budget_command
        from public.johanna_abandonment_one_shot_commands cmd
        where cmd.target_phone = v_intent.normalized_phone
        for update;
        if found then
            v_outcome := case
                when v_budget_command.source_reevaluation_id = v_reevaluation.id
                    then 'command_reserved'
                else 'budget_consumed'
            end;
        end if;
    end if;

    if v_outcome is null then
        v_fingerprint := encode(sha256(convert_to(concat_ws(
            chr(31),
            v_reevaluation.id::text,
            v_intent.id::text,
            v_intent.normalized_phone,
            'johanna-precheckout-delayed-first-touch',
            '1',
            '0',
            'johanna_interes_precheckout_01',
            'es_EC',
            'MARKETING',
            'johanna-precheckout-delayed-first-touch-v1'
        ), 'UTF8')), 'hex');

        insert into public.johanna_abandonment_one_shot_commands (
            command_key,
            semantic_fingerprint,
            rollout_scope,
            purchase_intent_id,
            hotmart_webhook_event_id,
            payment_failure_case_id,
            source_reevaluation_id,
            scope_key,
            scope_version,
            runtime_generation,
            chatwoot_account_id,
            chatwoot_inbox_id,
            target_phone,
            template_name,
            template_language,
            template_category,
            copy_version,
            max_messages,
            followups_allowed,
            status
        ) values (
            'precheckout-delayed:' || v_reevaluation.id::text,
            v_fingerprint,
            'johanna-precheckout-delayed-first-touch-v1',
            v_intent.id,
            null,
            null,
            v_reevaluation.id,
            'johanna-precheckout-delayed-first-touch',
            1,
            0,
            1,
            9,
            v_intent.normalized_phone,
            'johanna_interes_precheckout_01',
            'es_EC',
            'MARKETING',
            'johanna-precheckout-delayed-first-touch-v1',
            1,
            0,
            'reserved'
        ) returning id into v_command_id;
        v_outcome := 'command_reserved';
    end if;

    update public.hotmart_abandonment_reevaluations
    set status = 'completed',
        outcome = v_outcome,
        completed_at = p_now,
        updated_at = p_now
    where id = v_reevaluation.id
    returning * into strict v_reevaluation;

    return query select v_reevaluation.id, v_reevaluation.status,
        v_reevaluation.outcome, v_reevaluation.completed_at, false;
end;
$function$;

-- Preserve the historical Hotmart behavior and delegate only the new source.
create or replace function public.reevaluate_hotmart_abandonment_timer(
    p_reevaluation_id uuid,
    p_now timestamptz
)
returns table (
    reevaluation_id uuid,
    reevaluation_status text,
    reevaluation_outcome text,
    completed_at timestamptz,
    replayed boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_purchase_intent_id uuid;
    v_source_kind text;
    v_intent public.purchase_intents%rowtype;
    v_reevaluation public.hotmart_abandonment_reevaluations%rowtype;
    v_outcome text;
begin
    if p_reevaluation_id is null or p_now is null then
        raise exception using errcode = '22023',
            message = 'hotmart_abandonment_reevaluation_input_invalid';
    end if;

    select timer.purchase_intent_id, timer.source_kind
    into v_purchase_intent_id, v_source_kind
    from public.hotmart_abandonment_reevaluations timer
    where timer.id = p_reevaluation_id;
    if not found then
        raise exception using errcode = 'P0002',
            message = 'hotmart_abandonment_reevaluation_not_found';
    end if;

    if v_source_kind = 'precheckout_intent' then
        return query
        select result.*
        from public._reevaluate_precheckout_delayed_first_touch(
            p_reevaluation_id, p_now
        ) result;
        return;
    end if;

    -- Historical Hotmart lock order and outcomes remain unchanged.
    select intent.* into strict v_intent
    from public.purchase_intents intent
    where intent.id = v_purchase_intent_id
    for update;

    select timer.* into strict v_reevaluation
    from public.hotmart_abandonment_reevaluations timer
    where timer.id = p_reevaluation_id
      and timer.purchase_intent_id = v_intent.id
      and timer.source_kind = 'hotmart_event'
    for update;

    if v_reevaluation.status = 'completed' then
        return query select v_reevaluation.id, v_reevaluation.status,
            v_reevaluation.outcome, v_reevaluation.completed_at, true;
        return;
    end if;
    if v_reevaluation.due_at > p_now then
        raise exception using errcode = '55000',
            message = 'hotmart_abandonment_reevaluation_not_due';
    end if;

    v_outcome := case
        when v_intent.lifecycle_state = 'purchased'
            then 'cancelled_purchased'
        when v_intent.lifecycle_state = 'waiting_for_purchase'
         and v_intent.current_classification = 'confirmed_abandonment'
         and (
             not v_intent.activation_authorized
             or not v_intent.whatsapp_contact_authorized
         )
            then 'blocked_not_authorized'
        when v_intent.lifecycle_state = 'waiting_for_purchase'
         and v_intent.current_classification = 'confirmed_abandonment'
         and v_intent.activation_authorized
         and v_intent.whatsapp_contact_authorized
            then 'blocked_contact_binding_missing'
        else 'cancelled_intent_changed'
    end;

    update public.hotmart_abandonment_reevaluations
    set status = 'completed',
        outcome = v_outcome,
        completed_at = p_now,
        updated_at = p_now
    where id = v_reevaluation.id
    returning * into strict v_reevaluation;

    return query select v_reevaluation.id, v_reevaluation.status,
        v_reevaluation.outcome, v_reevaluation.completed_at, false;
end;
$function$;

revoke all on function public._reevaluate_precheckout_delayed_first_touch(uuid,timestamptz)
from public;

do $acl$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on function public._reevaluate_precheckout_delayed_first_touch(uuid,timestamptz)
        from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on function public._reevaluate_precheckout_delayed_first_touch(uuid,timestamptz)
        from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on function public._reevaluate_precheckout_delayed_first_touch(uuid,timestamptz)
        from service_role;
    end if;
end;
$acl$;

do $postflight$
declare
    v_helper regprocedure := to_regprocedure(
        'public._reevaluate_precheckout_delayed_first_touch(uuid,timestamptz)'
    );
    v_wrapper regprocedure := to_regprocedure(
        'public.reevaluate_hotmart_abandonment_timer(uuid,timestamptz)'
    );
    v_helper_definition text;
    v_wrapper_definition text;
begin
    if v_helper is null or v_wrapper is null then
        raise exception 'precheckout_delayed_reservation_function_missing';
    end if;
    select pg_get_functiondef(v_helper) into strict v_helper_definition;
    select pg_get_functiondef(v_wrapper) into strict v_wrapper_definition;
    if position('johanna_abandonment_one_shot_commands' in v_helper_definition) = 0
       or position('johanna-abandonment-template-e2e-v2'
           in v_helper_definition) = 0
       or position('johanna-recovery-budget:'
           in v_helper_definition) = 0
       or position('_reevaluate_precheckout_delayed_first_touch'
           in v_wrapper_definition) = 0 then
        raise exception 'precheckout_delayed_reservation_postflight_failed';
    end if;
    if not exists (
        select 1
        from pg_indexes index_row
        where index_row.schemaname = 'public'
          and index_row.indexname =
              'johanna_abandonment_one_shot_commands_target_phone_idx'
          and index_row.indexdef like '%UNIQUE%target_phone%'
    ) then
        raise exception 'precheckout_delayed_shared_recipient_budget_missing';
    end if;
    if has_function_privilege(
        'public', v_helper, 'EXECUTE'
    ) then
        raise exception 'precheckout_delayed_reservation_helper_acl_failed';
    end if;
end;
$postflight$;

commit;
