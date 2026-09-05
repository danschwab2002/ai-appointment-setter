-- ATT1 UC-01 allows at most one initial payment-failure contact per case.
-- Repeated failures remain evidence on the existing case after that contact is terminal.

create or replace function public.plan_payment_failure_recovery(
    p_webhook_event_id uuid,
    p_contact_id uuid,
    p_external_product_id text,
    p_product_name text,
    p_offer_code text,
    p_policy_key text,
    p_policy_version integer,
    p_failed_at timestamptz
)
returns table (
    recovery_case_id uuid,
    followup_sequence_id uuid,
    scheduled_action_id uuid,
    created boolean
)
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_policy public.followup_policy_versions%rowtype;
    v_event public.webhook_events%rowtype;
    v_case_id uuid;
    v_sequence_id uuid;
    v_action_id uuid;
    v_existing_event_case_id uuid;
    v_case_status text;
    v_case_version bigint;
    v_case_policy_key text;
    v_case_policy_version integer;
    v_sequence_status text;
    v_original_abandoned_at timestamptz;
begin
    if p_external_product_id is null or btrim(p_external_product_id) = '' then
        raise exception using errcode = '22023', message = 'external_product_id_required';
    end if;
    if p_product_name is null or btrim(p_product_name) = '' then
        raise exception using errcode = '22023', message = 'product_name_required';
    end if;

    select * into strict v_policy
    from public.followup_policy_versions
    where policy_key = p_policy_key
      and version = p_policy_version
      and status = 'published'
      and purpose = 'cart_recovery';

    select * into strict v_event
    from public.webhook_events
    where id = p_webhook_event_id
      and source in ('hotmart', 'simulator')
      and event_type = 'PURCHASE_CANCELED'
    for update;

    perform 1
    from public.contacts
    where id = p_contact_id
    for update;
    if not found then
        raise exception using errcode = '23503', message = 'contact_not_found';
    end if;

    select rce.recovery_case_id
      into v_existing_event_case_id
    from public.recovery_case_events rce
    where rce.webhook_event_id = p_webhook_event_id;

    if v_existing_event_case_id is not null then
        select fs.id, sa.id
          into strict v_sequence_id, v_action_id
        from public.followup_sequences fs
        join public.scheduled_actions sa
          on sa.followup_sequence_id = fs.id
        where fs.recovery_case_id = v_existing_event_case_id
        order by sa.created_at asc
        limit 1;

        return query
        select v_existing_event_case_id, v_sequence_id, v_action_id, false;
        return;
    end if;

    select rc.id, rc.status, rc.policy_key, rc.policy_version
      into v_case_id, v_case_status, v_case_policy_key, v_case_policy_version
    from public.recovery_cases rc
    where rc.contact_id = p_contact_id
      and rc.source = v_event.source
      and rc.external_product_id = p_external_product_id
      and rc.offer_code is not distinct from p_offer_code
      and rc.status in ('grace_period', 'active', 'paused')
      and rc.context ->> 'trigger_kind' = 'payment_failure'
    order by rc.created_at asc
    limit 1
    for update;

    if v_case_id is not null then

        update public.recovery_cases rc
        set hotmart_purchase_intent_id = correlation.purchase_intent_id
        from public.hotmart_purchase_intent_correlations correlation
        where rc.id = v_case_id
          and correlation.webhook_event_id = p_webhook_event_id
          and (rc.hotmart_purchase_intent_id is null
               or rc.hotmart_purchase_intent_id = correlation.purchase_intent_id);

        insert into public.recovery_case_events (
            recovery_case_id,
            webhook_event_id,
            event_role,
            observed_at
        ) values (
            v_case_id,
            p_webhook_event_id,
            'payment_failure',
            p_failed_at
        );

        update public.recovery_cases
        set context = context || jsonb_build_object(
                'latest_payment_failure_event_id', p_webhook_event_id,
                'latest_payment_failure_at', p_failed_at
            ),
            version = version + 1
        where id = v_case_id
        returning version into v_case_version;

        select fs.id, fs.status, sa.id
          into v_sequence_id, v_sequence_status, v_action_id
        from public.followup_sequences fs
        join public.scheduled_actions sa
          on sa.followup_sequence_id = fs.id
        where fs.recovery_case_id = v_case_id
          and sa.step_key = 'payment_failure_first_contact'
        order by sa.created_at asc
        limit 1
        for update of fs, sa;

        if v_action_id is not null then
            update public.scheduled_actions
            set due_at = case
                    when v_sequence_status = 'active' then least(due_at, now())
                    else due_at
                end,
                next_attempt_at = case
                    when v_sequence_status = 'active' then null
                    else next_attempt_at
                end,
                expected_case_version = v_case_version
            where id = v_action_id
              and status in ('pending', 'deferred', 'retryable_failed');
        elsif v_case_status = 'paused' or v_sequence_status = 'paused' then
            null;
        else
            select * into strict v_policy
            from public.followup_policy_versions
            where policy_key = v_case_policy_key
              and version = v_case_policy_version;

            select min(rce.observed_at) into strict v_original_abandoned_at
            from public.recovery_case_events rce
            where rce.recovery_case_id = v_case_id
              and rce.event_role = 'payment_failure';

            insert into public.followup_sequences (
                recovery_case_id,
                status,
                reason,
                policy_key,
                policy_version,
                current_step,
                max_attempts
            ) values (
                v_case_id,
                'active',
                'payment_failure',
                v_case_policy_key,
                v_case_policy_version,
                0,
                v_policy.max_automatic_messages
            )
            returning id into v_sequence_id;

            insert into public.scheduled_actions (
                recovery_case_id,
                followup_sequence_id,
                action_type,
                status,
                due_at,
                expires_at,
                expected_case_version,
                idempotency_key,
                policy_key,
                policy_version,
                step_key,
                anchor_type,
                anchor_subject_internal_id,
                anchor_observed_at,
                anchor_checkpoint
            ) values (
                v_case_id,
                v_sequence_id,
                'first_contact_review',
                'pending',
                now(),
                v_original_abandoned_at + v_policy.expires_after,
                v_case_version,
                'payment_failure:reevaluate:' || p_webhook_event_id::text,
                v_case_policy_key,
                v_case_policy_version,
                'payment_failure_first_contact',
                'payment_failure',
                p_webhook_event_id,
                p_failed_at,
                jsonb_build_object('webhook_event_id', p_webhook_event_id)
            )
            returning id into v_action_id;
        end if;

        insert into public.conversation_events (
            recovery_case_id,
            event_type,
            actor_type,
            related_action_id,
            data
        ) values (
            v_case_id,
            'payment_failure_aggregated',
            'integration',
            v_action_id,
            jsonb_build_object(
                'policy_key', v_case_policy_key,
                'policy_version', v_case_policy_version,
                'reason_code', 'repeated_payment_failure'
            )
        );

        return query select v_case_id, v_sequence_id, v_action_id, false;
        return;
    end if;

    insert into public.recovery_cases (
        contact_id,
        abandonment_event_id,
        source,
        external_product_id,
        product_name,
        offer_code,
        status,
        lead_stage,
        grace_expires_at,
        policy_key,
        policy_version,
        context
    ) values (
        p_contact_id,
        p_webhook_event_id,
        v_event.source,
        p_external_product_id,
        p_product_name,
        p_offer_code,
        'grace_period',
        'new',
        p_failed_at + v_policy.grace_period,
        p_policy_key,
        p_policy_version,
        jsonb_build_object(
            'trigger_kind', 'payment_failure',
            'latest_payment_failure_event_id', p_webhook_event_id,
            'latest_payment_failure_at', p_failed_at
        )
    )
    returning id into v_case_id;


    update public.recovery_cases rc
    set hotmart_purchase_intent_id = correlation.purchase_intent_id
    from public.hotmart_purchase_intent_correlations correlation
    where rc.id = v_case_id
      and correlation.webhook_event_id = p_webhook_event_id
      and (rc.hotmart_purchase_intent_id is null
           or rc.hotmart_purchase_intent_id = correlation.purchase_intent_id);

    insert into public.recovery_case_events (
        recovery_case_id,
        webhook_event_id,
        event_role,
        observed_at
    ) values (
        v_case_id,
        p_webhook_event_id,
        'payment_failure',
        p_failed_at
    );

    insert into public.followup_sequences (
        recovery_case_id,
        status,
        reason,
        policy_key,
        policy_version,
        current_step,
        max_attempts
    ) values (
        v_case_id,
        'active',
        'payment_failure',
        p_policy_key,
        p_policy_version,
        0,
        v_policy.max_automatic_messages
    )
    returning id into v_sequence_id;

    insert into public.scheduled_actions (
        recovery_case_id,
        followup_sequence_id,
        action_type,
        status,
        due_at,
        expires_at,
        expected_case_version,
        idempotency_key,
        policy_key,
        policy_version,
        step_key,
        anchor_type,
        anchor_subject_internal_id,
        anchor_observed_at,
        anchor_checkpoint
    ) values (
        v_case_id,
        v_sequence_id,
        'first_contact_review',
        'pending',
        p_failed_at + v_policy.grace_period,
        p_failed_at + v_policy.expires_after,
        1,
        'payment_failure:first_contact:' || v_case_id::text,
        p_policy_key,
        p_policy_version,
        'payment_failure_first_contact',
        'payment_failure',
        p_webhook_event_id,
        p_failed_at,
        jsonb_build_object('webhook_event_id', p_webhook_event_id)
    )
    returning id into v_action_id;

    insert into public.conversation_events (
        recovery_case_id,
        event_type,
        actor_type,
        related_action_id,
        data
    ) values (
        v_case_id,
        'cart_recovery_planned',
        'system',
        v_action_id,
        jsonb_build_object(
            'policy_key', p_policy_key,
            'policy_version', p_policy_version,
            'from_status', null,
            'to_status', 'pending',
            'reason_code', 'payment_failure'
        )
    );

    return query select v_case_id, v_sequence_id, v_action_id, true;
end;
$function$;
