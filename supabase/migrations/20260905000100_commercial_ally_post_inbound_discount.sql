-- Plan exactly one ATT1 payment-failure discount action after a canonical inbound.
-- The action remains non-dispatchable until the later template/effect activation cut.

begin;

alter table public.scheduled_actions
    drop constraint scheduled_actions_action_type_check;

alter table public.scheduled_actions
    add constraint scheduled_actions_action_type_check
    check (action_type = any (array[
        'first_contact_review', 'no_reply_review', 'reconcile_delivery',
        'inbound_reply_offer'
    ]));

create table public.commercial_ally_post_inbound_discount_bindings (
    recovery_case_id uuid primary key
        references public.recovery_cases(id) on delete restrict,
    scheduled_action_id uuid not null unique
        references public.scheduled_actions(id) on delete restrict,
    initial_action_id uuid not null
        references public.scheduled_actions(id) on delete restrict,
    initial_accepted_message_id uuid not null
        references public.messages(id) on delete restrict,
    inbound_message_id uuid not null unique
        references public.messages(id) on delete restrict,
    inbound_external_message_id bigint not null check (inbound_external_message_id > 0),
    canonical_account_id bigint not null check (canonical_account_id > 0),
    canonical_inbox_id bigint not null check (canonical_inbox_id > 0),
    canonical_conversation_id bigint not null check (canonical_conversation_id > 0),
    canonical_channel_identity_id uuid not null
        references public.channel_identities(id) on delete restrict,
    canonical_external_user_id text not null
        check (canonical_external_user_id ~ '^[0-9]{5,20}$'),
    tenant_ref text not null,
    funnel_ref text not null,
    binding_version integer not null check (binding_version > 0),
    discount_policy_key text not null,
    discount_policy_version integer not null check (discount_policy_version > 0),
    trigger_kind text not null check (trigger_kind = 'payment_failure'),
    discount_kind text not null check (discount_kind = 'percentage'),
    discount_value numeric not null check (discount_value = 10),
    coupon_reference text not null check (btrim(coupon_reference) <> ''),
    offer_expiration_mode text not null check (offer_expiration_mode = 'indefinite'),
    presentation_stage text not null check (presentation_stage = 'later_step'),
    template_key text not null check (btrim(template_key) <> ''),
    copy_version text not null check (btrim(copy_version) <> ''),
    coupon_delivery_mode text not null
        check (coupon_delivery_mode = 'meta_template_variable'),
    urgency_copy_allowed boolean not null check (not urgency_copy_allowed),
    template_language text not null check (btrim(template_language) <> ''),
    template_category text not null check (btrim(template_category) <> ''),
    coupon_template_component text not null
        check (coupon_template_component = 'body'),
    coupon_template_parameter_index integer not null
        check (coupon_template_parameter_index > 0),
    inbound_received_at timestamptz not null,
    planned_at timestamptz not null default clock_timestamp(),
    foreign key (tenant_ref, funnel_ref, binding_version)
        references public.commercial_ally_runtime_bindings(
            tenant_ref, funnel_ref, binding_version
        ) on delete restrict,
    foreign key (
        tenant_ref, funnel_ref, binding_version,
        discount_policy_key, discount_policy_version, trigger_kind
    ) references public.commercial_ally_discount_policy_versions(
        tenant_ref, funnel_ref, binding_version,
        policy_key, policy_version, trigger_kind
    ) on delete restrict,
    unique (
        canonical_account_id, canonical_inbox_id,
        canonical_conversation_id, inbound_external_message_id
    )
);

create function public.protect_commercial_ally_post_inbound_discount_binding()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    raise exception using
        errcode = '55000',
        message = 'commercial_ally_post_inbound_discount_binding_immutable';
end;
$function$;

create trigger commercial_ally_post_inbound_discount_bindings_immutable
before update or delete on public.commercial_ally_post_inbound_discount_bindings
for each row execute function
    public.protect_commercial_ally_post_inbound_discount_binding();

revoke all on function
    public.protect_commercial_ally_post_inbound_discount_binding()
    from public;

do $acl$
declare
    v_role text;
begin
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format(
                'revoke all on function public.protect_commercial_ally_post_inbound_discount_binding() from %I',
                v_role
            );
        end if;
    end loop;
end;
$acl$;

alter table public.commercial_ally_post_inbound_discount_bindings
    enable row level security;

revoke all on table public.commercial_ally_post_inbound_discount_bindings
    from public;

do $acl$
declare
    v_role text;
begin
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format(
                'revoke all on table public.commercial_ally_post_inbound_discount_bindings from %I',
                v_role
            );
        end if;
    end loop;
end;
$acl$;

create function public.plan_commercial_ally_post_inbound_discount(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
    p_discount_policy_key text,
    p_discount_policy_version integer,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_chatwoot_message_id bigint,
    p_external_user_id text,
    p_inbound_received_at timestamptz
)
returns table (
    outcome text,
    recovery_case_id uuid,
    scheduled_action_id uuid,
    inbound_message_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_runtime public.commercial_ally_runtime_bindings%rowtype;
    v_discount public.commercial_ally_discount_policy_versions%rowtype;
    v_case public.recovery_cases%rowtype;
    v_conversation public.conversations%rowtype;
    v_identity public.channel_identities%rowtype;
    v_initial_action public.scheduled_actions%rowtype;
    v_initial_message public.messages%rowtype;
    v_existing public.commercial_ally_post_inbound_discount_bindings%rowtype;
    v_inbound_message public.messages%rowtype;
    v_sequence_id uuid;
    v_action_id uuid;
    v_case_version bigint;
    v_initial_action_count bigint;
    v_initial_message_count bigint;
begin
    if p_tenant_ref is null or p_tenant_ref !~ '^[a-z0-9][a-z0-9-]{0,127}$'
       or p_funnel_ref is null or p_funnel_ref !~ '^[a-z0-9][a-z0-9-]{0,127}$'
       or p_binding_version is null or p_binding_version < 1
       or p_discount_policy_key is null
       or p_discount_policy_key !~ '^[a-z0-9][a-z0-9-]{0,127}$'
       or p_discount_policy_version is null or p_discount_policy_version < 1
       or p_chatwoot_account_id is null or p_chatwoot_account_id < 1
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1
       or p_chatwoot_conversation_id is null or p_chatwoot_conversation_id < 1
       or p_chatwoot_message_id is null or p_chatwoot_message_id < 1
       or p_external_user_id is null
       or p_external_user_id !~ '^[0-9]{5,20}$'
       or p_inbound_received_at is null
       or not isfinite(p_inbound_received_at) then
        raise exception using
            errcode = '22023',
            message = 'commercial_ally_post_inbound_discount_invalid';
    end if;

    select binding.* into v_existing
    from public.commercial_ally_post_inbound_discount_bindings binding
    where binding.tenant_ref = p_tenant_ref
      and binding.tenant_ref = 'att1'
      and binding.funnel_ref = p_funnel_ref
      and binding.binding_version = p_binding_version
      and binding.discount_policy_key = p_discount_policy_key
      and binding.discount_policy_version = p_discount_policy_version
      and binding.trigger_kind = 'payment_failure'
      and binding.canonical_account_id = p_chatwoot_account_id
      and binding.canonical_inbox_id = p_chatwoot_inbox_id
      and binding.canonical_conversation_id = p_chatwoot_conversation_id
      and binding.canonical_external_user_id = p_external_user_id;
    if found then
        return query select 'already_exists'::text,
            v_existing.recovery_case_id, v_existing.scheduled_action_id,
            v_existing.inbound_message_id;
        return;
    end if;

    select binding.* into v_runtime
    from public.commercial_ally_runtime_bindings binding
    where binding.tenant_ref = p_tenant_ref
      and p_tenant_ref = 'att1'
      and binding.funnel_ref = p_funnel_ref
      and binding.binding_version = p_binding_version
      and binding.status = 'active'
      and binding.chatwoot_account_id = p_chatwoot_account_id
      and binding.chatwoot_inbox_id = p_chatwoot_inbox_id
    for share;
    if not found then
        return query select 'runtime_not_applicable'::text, null::uuid, null::uuid, null::uuid;
        return;
    end if;

    select policy.* into v_discount
    from public.commercial_ally_discount_policy_versions policy
    where policy.tenant_ref = p_tenant_ref
      and policy.funnel_ref = p_funnel_ref
      and policy.binding_version = p_binding_version
      and policy.policy_key = p_discount_policy_key
      and policy.policy_version = p_discount_policy_version
      and policy.trigger_kind = 'payment_failure'
      and policy.status = 'published'
      and policy.valid_from <= p_inbound_received_at
      and (policy.valid_until is null or policy.valid_until > p_inbound_received_at)
      and policy.discount_kind = 'percentage'
      and policy.discount_value = 10
      and policy.offer_expiration_mode = 'indefinite'
      and policy.offer_valid_for is null
      and policy.presentation_stage = 'later_step'
      and policy.requires_inbound_reply_after_initial_template
      and policy.coupon_delivery_mode = 'meta_template_variable'
      and not policy.urgency_copy_allowed
      and policy.channel_provider = 'waba'
      and policy.delivery_mode = 'approved_template'
      and policy.coupon_template_component = 'body'
      and policy.coupon_template_parameter_index > 0
    for share;
    if not found then
        return query select 'discount_policy_not_applicable'::text, null::uuid, null::uuid, null::uuid;
        return;
    end if;

    select identity.* into v_identity
    from public.channel_identities identity
    where identity.channel = 'whatsapp'
      and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
      and identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
      and identity.external_user_id = p_external_user_id
      and identity.identity_status = 'active'
    for share;
    if not found then
        return query select 'identity_not_applicable'::text, null::uuid, null::uuid, null::uuid;
        return;
    end if;

    select conversation.* into v_conversation
    from public.conversations conversation
    where conversation.channel_identity_id = v_identity.id
      and conversation.contact_id = v_identity.contact_id
      and conversation.commercial_context ->> 'chatwoot_conversation_id'
          = p_chatwoot_conversation_id::text
      and conversation.status = 'active'
      and conversation.automation_status = 'enabled'
      and not conversation.human_takeover
    for update;
    if not found then
        return query select 'conversation_not_applicable'::text, null::uuid, null::uuid, null::uuid;
        return;
    end if;

    select recovery.* into v_case
    from public.recovery_cases recovery
    join public.pilot_recovery_case_bindings case_binding
      on case_binding.recovery_case_id = recovery.id
    join public.pilot_scope_versions scope
      on scope.scope_key = case_binding.scope_key
     and scope.version = case_binding.scope_version
    where recovery.conversation_id = v_conversation.id
      and recovery.contact_id = v_identity.contact_id
      and recovery.selected_channel_identity_id = v_identity.id
      and recovery.identity_resolution_status = 'resolved'
      and recovery.status in ('sequence_exhausted', 'active')
      and scope.status = 'published'
      and scope.tenant_key = v_runtime.tenant_ref
      and scope.chatwoot_account_id = p_chatwoot_account_id
      and scope.chatwoot_inbox_id = p_chatwoot_inbox_id
      and scope.source_event_type = 'PURCHASE_CANCELED'
      and scope.external_product_id = v_runtime.hotmart_product_id::text
      and scope.offer_code = v_runtime.offer_code
      and scope.policy_key = recovery.policy_key
      and scope.policy_version = recovery.policy_version
    for update of recovery;
    if not found then
        return query select 'recovery_case_not_applicable'::text, null::uuid, null::uuid, null::uuid;
        return;
    end if;

    select binding.* into v_existing
    from public.commercial_ally_post_inbound_discount_bindings binding
    where binding.recovery_case_id = v_case.id;
    if found then
        if v_existing.tenant_ref = p_tenant_ref
           and v_existing.tenant_ref = 'att1'
           and v_existing.funnel_ref = p_funnel_ref
           and v_existing.binding_version = p_binding_version
           and v_existing.discount_policy_key = p_discount_policy_key
           and v_existing.discount_policy_version = p_discount_policy_version
           and v_existing.trigger_kind = 'payment_failure'
           and v_existing.canonical_account_id = p_chatwoot_account_id
           and v_existing.canonical_inbox_id = p_chatwoot_inbox_id
           and v_existing.canonical_conversation_id = p_chatwoot_conversation_id
           and v_existing.canonical_channel_identity_id = v_identity.id
           and v_existing.canonical_external_user_id = p_external_user_id then
            return query select 'already_exists'::text, v_case.id,
                v_existing.scheduled_action_id, v_existing.inbound_message_id;
        else
            return query select 'recovery_case_not_applicable'::text,
                null::uuid, null::uuid, null::uuid;
        end if;
        return;
    end if;

    if v_case.status <> 'sequence_exhausted' then
        return query select 'recovery_case_not_applicable'::text,
            null::uuid, null::uuid, null::uuid;
        return;
    end if;

    select count(*) into v_initial_action_count
    from public.scheduled_actions action
    where action.recovery_case_id = v_case.id
      and action.conversation_id = v_conversation.id
      and action.action_type = 'first_contact_review'
      and action.step_key = 'payment_failure_first_contact'
      and action.status = 'accepted_by_chatwoot';
    if v_initial_action_count <> 1 then
        return query select 'initial_contact_not_applicable'::text, null::uuid, null::uuid, null::uuid;
        return;
    end if;

    select action.* into strict v_initial_action
    from public.scheduled_actions action
    where action.recovery_case_id = v_case.id
      and action.conversation_id = v_conversation.id
      and action.action_type = 'first_contact_review'
      and action.step_key = 'payment_failure_first_contact'
      and action.status = 'accepted_by_chatwoot';

    select count(*) into v_initial_message_count
    from public.messages message
    where message.conversation_id = v_conversation.id
      and message.direction = 'outbound'
      and message.delivery_status in ('accepted', 'sent', 'delivered', 'read')
      and message.semantic_metadata ->> 'action_id' = v_initial_action.id::text;
    if v_initial_message_count <> 1 then
        return query select 'initial_contact_not_applicable'::text, null::uuid, null::uuid, null::uuid;
        return;
    end if;

    select message.* into strict v_initial_message
    from public.messages message
    where message.conversation_id = v_conversation.id
      and message.direction = 'outbound'
      and message.delivery_status in ('accepted', 'sent', 'delivered', 'read')
      and message.semantic_metadata ->> 'action_id' = v_initial_action.id::text;
    if p_inbound_received_at <= v_initial_message.occurred_at then
        return query select 'inbound_timing_not_applicable'::text, null::uuid, null::uuid, null::uuid;
        return;
    end if;

    insert into public.messages (
        conversation_id, external_message_id, direction, actor_type,
        message_type, content, delivery_status, in_reply_to_message_id,
        semantic_metadata, occurred_at
    ) values (
        v_conversation.id, p_chatwoot_message_id::text, 'inbound', 'prospect',
        'answer', '[redacted-inbound]', 'accepted', v_initial_message.id,
        jsonb_build_object(
            'source', 'chatwoot',
            'account_id', p_chatwoot_account_id,
            'inbox_id', p_chatwoot_inbox_id,
            'conversation_id', p_chatwoot_conversation_id,
            'message_id', p_chatwoot_message_id,
            'purpose', 'post_inbound_discount_trigger'
        ),
        p_inbound_received_at
    )
    on conflict (conversation_id, external_message_id)
        where external_message_id is not null
    do nothing;

    select message.* into strict v_inbound_message
    from public.messages message
    where message.conversation_id = v_conversation.id
      and message.external_message_id = p_chatwoot_message_id::text;
    if v_inbound_message.direction <> 'inbound'
       or v_inbound_message.actor_type <> 'prospect'
       or v_inbound_message.occurred_at <> p_inbound_received_at
       or v_inbound_message.semantic_metadata ->> 'purpose'
          <> 'post_inbound_discount_trigger' then
        raise exception using
            errcode = '23514',
            message = 'commercial_ally_post_inbound_discount_message_conflict';
    end if;

    insert into public.followup_sequences (
        recovery_case_id, conversation_id, status, reason,
        policy_key, policy_version, trigger_message_id,
        current_step, max_attempts, automatic_messages_accepted
    ) values (
        v_case.id, v_conversation.id, 'active', 'prospect_commitment',
        v_case.policy_key, v_case.policy_version, v_inbound_message.id,
        0, 1, 0
    ) returning id into v_sequence_id;

    update public.recovery_cases
    set status = 'active',
        version = version + 1,
        next_contact_at = null,
        next_contact_reason = null,
        current_goal = 'deliver_approved_discount_after_inbound',
        closed_at = null,
        updated_at = clock_timestamp()
    where id = v_case.id
    returning version into v_case_version;

    insert into public.scheduled_actions (
        recovery_case_id, followup_sequence_id, conversation_id,
        action_type, status, due_at, expires_at, next_attempt_at,
        expected_case_version, expected_conversation_version,
        idempotency_key, policy_key, policy_version, step_key,
        anchor_type, anchor_subject_internal_id, anchor_observed_at,
        anchor_checkpoint, metadata
    ) values (
        v_case.id, v_sequence_id, v_conversation.id,
        'inbound_reply_offer', 'deferred', p_inbound_received_at,
        'infinity'::timestamptz, 'infinity'::timestamptz,
        v_case_version, v_conversation.version,
        'commercial-ally:post-inbound-discount:' || v_case.id::text,
        v_case.policy_key, v_case.policy_version,
        'payment_failure_discount_offer',
        'inbound_message', v_inbound_message.id, p_inbound_received_at,
        jsonb_build_object(
            'chatwoot_account_id', p_chatwoot_account_id,
            'chatwoot_inbox_id', p_chatwoot_inbox_id,
            'chatwoot_conversation_id', p_chatwoot_conversation_id,
            'chatwoot_message_id', p_chatwoot_message_id
        ),
        jsonb_build_object(
            'effect_authorized', false,
            'planning_state', 'template_activation_required',
            'discount_policy_key', p_discount_policy_key,
            'discount_policy_version', p_discount_policy_version
        )
    ) returning id into v_action_id;

    insert into public.commercial_ally_post_inbound_discount_bindings (
        recovery_case_id, scheduled_action_id, initial_action_id,
        initial_accepted_message_id, inbound_message_id,
        inbound_external_message_id, canonical_account_id,
        canonical_inbox_id, canonical_conversation_id,
        canonical_channel_identity_id, canonical_external_user_id,
        tenant_ref, funnel_ref, binding_version,
        discount_policy_key, discount_policy_version, trigger_kind,
        discount_kind, discount_value, coupon_reference,
        offer_expiration_mode, presentation_stage, template_key,
        copy_version, coupon_delivery_mode, urgency_copy_allowed,
        template_language, template_category,
        coupon_template_component, coupon_template_parameter_index,
        inbound_received_at
    ) values (
        v_case.id, v_action_id, v_initial_action.id,
        v_initial_message.id, v_inbound_message.id,
        p_chatwoot_message_id, p_chatwoot_account_id,
        p_chatwoot_inbox_id, p_chatwoot_conversation_id,
        v_identity.id, p_external_user_id,
        p_tenant_ref, p_funnel_ref, p_binding_version,
        p_discount_policy_key, p_discount_policy_version, 'payment_failure',
        v_discount.discount_kind, v_discount.discount_value,
        v_discount.coupon_reference, v_discount.offer_expiration_mode,
        v_discount.presentation_stage, v_discount.template_key,
        v_discount.copy_version, v_discount.coupon_delivery_mode,
        v_discount.urgency_copy_allowed, v_discount.template_language,
        v_discount.template_category, v_discount.coupon_template_component,
        v_discount.coupon_template_parameter_index, p_inbound_received_at
    );

    insert into public.conversation_events (
        conversation_id, recovery_case_id, event_type, actor_type,
        related_message_id, related_action_id, data
    ) values (
        v_conversation.id, v_case.id,
        'commercial_ally_post_inbound_discount_planned', 'integration',
        v_inbound_message.id, v_action_id,
        jsonb_build_object(
            'discount_value', v_discount.discount_value,
            'offer_expiration_mode', v_discount.offer_expiration_mode,
            'presentation_stage', v_discount.presentation_stage,
            'effect_authorized', false
        )
    );

    return query select 'created'::text, v_case.id, v_action_id,
        v_inbound_message.id;
end;
$function$;

revoke all on function public.plan_commercial_ally_post_inbound_discount(
    text, text, integer, text, integer,
    bigint, bigint, bigint, bigint, text, timestamptz
) from public;

do $acl$
begin
    if to_regrole('anon') is not null then
        revoke all on function public.plan_commercial_ally_post_inbound_discount(
            text, text, integer, text, integer,
            bigint, bigint, bigint, bigint, text, timestamptz
        ) from anon;
    end if;
    if to_regrole('authenticated') is not null then
        revoke all on function public.plan_commercial_ally_post_inbound_discount(
            text, text, integer, text, integer,
            bigint, bigint, bigint, bigint, text, timestamptz
        ) from authenticated;
    end if;
    if to_regrole('service_role') is not null then
        revoke all on function public.plan_commercial_ally_post_inbound_discount(
            text, text, integer, text, integer,
            bigint, bigint, bigint, bigint, text, timestamptz
        ) from service_role;
        grant execute on function public.plan_commercial_ally_post_inbound_discount(
            text, text, integer, text, integer,
            bigint, bigint, bigint, bigint, text, timestamptz
        ) to service_role;
    end if;
end;
$acl$;

commit;
