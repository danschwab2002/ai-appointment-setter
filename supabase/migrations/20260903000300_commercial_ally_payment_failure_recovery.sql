begin;

alter table public.pilot_scope_versions
    drop constraint pilot_scope_versions_tenant_key_check;
alter table public.pilot_scope_versions
    add constraint pilot_scope_versions_tenant_key_check
    check (nullif(btrim(tenant_key), '') is not null);

alter table public.recovery_case_events
    drop constraint recovery_case_events_event_role_check;
alter table public.recovery_case_events
    add constraint recovery_case_events_event_role_check
    check (event_role in ('cart_abandonment', 'payment_failure'));

alter table public.hotmart_purchase_intent_correlations
    drop constraint hotmart_purchase_intent_correlations_event_type_check;
alter table public.hotmart_purchase_intent_correlations
    add constraint hotmart_purchase_intent_correlations_event_type_check
    check (event_type in (
        'PURCHASE_APPROVED',
        'PURCHASE_OUT_OF_SHOPPING_CART',
        'PURCHASE_CANCELED'
    ));

alter table public.followup_sequences
    drop constraint followup_sequences_reason_check;
alter table public.followup_sequences
    add constraint followup_sequences_reason_check
    check (reason = any (array[
        'cart_abandonment', 'payment_failure', 'no_reply',
        'contact_requested', 'prospect_commitment', 'agent_commitment',
        'proposal_pending', 'booking_pending', 'payment_pending',
        'nurture', 'manual', 'recovery'
    ]));

alter table public.recovery_cases
    add column if not exists hotmart_purchase_intent_id uuid
        references public.purchase_intents(id) on delete restrict;
create index if not exists recovery_cases_hotmart_purchase_intent_idx
    on public.recovery_cases (hotmart_purchase_intent_id)
    where hotmart_purchase_intent_id is not null;

create table public.commercial_ally_payment_failure_details (
    webhook_event_id uuid primary key references public.webhook_events(id) on delete restrict,
    transaction_ref text not null,
    refusal_reason text,
    correlation_outcome text,
    purchase_intent_id uuid references public.purchase_intents(id) on delete restrict,
    trigger_kind text not null default 'payment_failure' check (trigger_kind = 'payment_failure'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.commercial_ally_payment_failure_conflicts (
    id uuid primary key default gen_random_uuid(),
    existing_event_id uuid not null references public.webhook_events(id) on delete restrict,
    incoming_external_event_id text not null,
    incoming_payload jsonb not null,
    created_at timestamptz not null default now(),
    unique (existing_event_id, incoming_external_event_id, incoming_payload)
);

create function public.protect_commercial_ally_payment_failure_evidence()
returns trigger language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $function$
begin
    if tg_table_name = 'commercial_ally_payment_failure_details'
       and tg_op = 'UPDATE'
       and current_setting('app.payment_failure_evidence_finalize', true) = 'on'
       and old.webhook_event_id = new.webhook_event_id
       and old.transaction_ref = new.transaction_ref
       and old.refusal_reason is not distinct from new.refusal_reason
       and old.trigger_kind = new.trigger_kind
       and old.correlation_outcome is null
       and old.purchase_intent_id is null then
        return new;
    end if;
    raise exception using errcode = '55000', message = 'commercial_ally_payment_failure_evidence_immutable';
end;
$function$;
create trigger commercial_ally_payment_failure_details_append_only
before update or delete on public.commercial_ally_payment_failure_details
for each row execute function public.protect_commercial_ally_payment_failure_evidence();
create trigger commercial_ally_payment_failure_conflicts_append_only
before update or delete on public.commercial_ally_payment_failure_conflicts
for each row execute function public.protect_commercial_ally_payment_failure_evidence();

create function public.hotmart_payment_failure_payload_is_processable(
    p_external_event_id text, p_payload jsonb
) returns boolean language sql immutable security definer
set search_path = pg_catalog, public, pg_temp as $function$
    select p_external_event_id is not null
       and nullif(btrim(p_external_event_id), '') is not null
       and jsonb_typeof(p_payload) = 'object'
       and p_payload ->> 'id' = p_external_event_id
       and p_payload ->> 'event' = 'PURCHASE_CANCELED'
       and p_payload ->> 'version' = '2.0.0'
       and jsonb_typeof(p_payload #> '{data,product,id}') = 'number'
       and nullif(btrim(p_payload #>> '{data,product,name}'), '') is not null
       and nullif(btrim(p_payload #>> '{data,purchase,offer,code}'), '') is not null
       and p_payload #>> '{data,purchase,status}' = 'CANCELED'
       and nullif(btrim(p_payload #>> '{data,purchase,transaction}'), '') ~ '^[A-Za-z0-9._:-]+$'
       and (p_payload ->> 'creation_date') ~ '^[0-9]+$'
       and (p_payload ->> 'creation_date')::numeric > 0
       and (
           nullif(btrim(p_payload #>> '{data,buyer,email}'), '') is not null
           or nullif(regexp_replace(coalesce(
               p_payload #>> '{data,buyer,checkout_phone}',
               p_payload #>> '{data,buyer,phone}', ''
           ), '[^0-9]', '', 'g'), '') is not null
       );
$function$;

create or replace function public._hotmart_purchase_intent_payload_identity(
    p_event_type text,
    p_payload jsonb
)
returns table (
    normalized_email text,
    normalized_phone text
)
language plpgsql
immutable
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_email_value jsonb;
    v_phone_value jsonb;
    v_checkout_phone_value jsonb;
begin
    v_email_value := p_payload #> '{data,buyer,email}';
    normalized_email := case
        when jsonb_typeof(v_email_value) = 'string'
            then nullif(lower(btrim(p_payload #>> '{data,buyer,email}')), '')
        else null
    end;

    if p_event_type = 'PURCHASE_APPROVED' then
        v_checkout_phone_value := p_payload #> '{data,buyer,checkout_phone}';
        normalized_phone := case
            when jsonb_typeof(v_checkout_phone_value) = 'string'
                then public._normalize_hotmart_purchase_intent_phone(
                    p_payload #>> '{data,buyer,checkout_phone}'
                )
            else null
        end;
    elsif p_event_type = 'PURCHASE_CANCELED' then
        v_phone_value := p_payload #> '{data,buyer,phone}';
        v_checkout_phone_value := p_payload #> '{data,buyer,checkout_phone}';
        normalized_phone := coalesce(
            case
                when jsonb_typeof(v_checkout_phone_value) = 'string'
                    then public._normalize_hotmart_purchase_intent_phone(
                        p_payload #>> '{data,buyer,checkout_phone}'
                    )
                else null
            end,
            case
                when jsonb_typeof(v_phone_value) = 'string'
                    then public._normalize_hotmart_purchase_intent_phone(
                        p_payload #>> '{data,buyer,phone}'
                    )
                else null
            end
        );
    elsif p_event_type = 'PURCHASE_OUT_OF_SHOPPING_CART' then
        v_phone_value := p_payload #> '{data,buyer,phone}';
        v_checkout_phone_value := p_payload #> '{data,buyer,checkout_phone}';
        normalized_phone := coalesce(
            case
                when jsonb_typeof(v_phone_value) = 'string'
                    then public._normalize_hotmart_purchase_intent_phone(
                        p_payload #>> '{data,buyer,phone}'
                    )
                else null
            end,
            case
                when jsonb_typeof(v_checkout_phone_value) = 'string'
                    then public._normalize_hotmart_purchase_intent_phone(
                        p_payload #>> '{data,buyer,checkout_phone}'
                    )
                else null
            end
        );
    else
        raise exception using
            errcode = '22023',
            message = 'unsupported_hotmart_intent_event';
    end if;

    return next;
end;
$function$;


do $migration$
declare
    v_definition text;
    v_old text := $old$    if v_event.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART' then
        return coalesce(public.hotmart_cart_abandonment_payload_is_processable(
            v_event.external_event_id,
            v_event.payload
        ), false);
    end if;
    return false;$old$;
    v_new text := $new$    if v_event.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART' then
        return coalesce(public.hotmart_cart_abandonment_payload_is_processable(
            v_event.external_event_id,
            v_event.payload
        ), false);
    end if;
    if v_event.event_type = 'PURCHASE_CANCELED' then
        return coalesce(public.hotmart_payment_failure_payload_is_processable(
            v_event.external_event_id,
            v_event.payload
        ), false);
    end if;
    return false;$new$;
begin
    select pg_get_functiondef(
        'public.hotmart_purchase_intent_payload_is_processable(uuid)'::regprocedure
    ) into v_definition;
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_old, ''))
          <> length(v_old) then
        raise exception using errcode = '55000',
            message = 'unexpected_hotmart_intent_processable_definition';
    end if;
    execute replace(v_definition, v_old, v_new);
end;
$migration$;



do $migration$
declare
    v_definition text;
    v_old text := $old$    if v_event_type = 'PURCHASE_APPROVED' then
        v_offer_ref := nullif(btrim(
            v_event.payload #>> '{data,purchase,offer,code}'
        ), '');
        v_observed_at := to_timestamp(
            (v_event.payload #>> '{data,purchase,approved_date}')::numeric / 1000
        );
    else
        v_offer_ref := nullif(btrim(v_event.payload #>> '{data,offer,code}'), '');
        v_observed_at := to_timestamp(
            (v_event.payload ->> 'creation_date')::numeric / 1000
        );
    end if;$old$;
    v_new text := $new$    if v_event_type = 'PURCHASE_APPROVED' then
        v_offer_ref := nullif(btrim(
            v_event.payload #>> '{data,purchase,offer,code}'
        ), '');
        v_observed_at := to_timestamp(
            (v_event.payload #>> '{data,purchase,approved_date}')::numeric / 1000
        );
    elsif v_event_type = 'PURCHASE_CANCELED' then
        v_offer_ref := nullif(btrim(
            v_event.payload #>> '{data,purchase,offer,code}'
        ), '');
        v_observed_at := to_timestamp(
            (v_event.payload ->> 'creation_date')::numeric / 1000
        );
    else
        v_offer_ref := nullif(btrim(v_event.payload #>> '{data,offer,code}'), '');
        v_observed_at := to_timestamp(
            (v_event.payload ->> 'creation_date')::numeric / 1000
        );
    end if;$new$;
begin
    select pg_get_functiondef(
        'public.correlate_hotmart_purchase_intent(uuid)'::regprocedure
    ) into v_definition;
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_old, ''))
          <> length(v_old) then
        raise exception using errcode = '55000',
            message = 'unexpected_hotmart_intent_correlator_definition';
    end if;
    execute replace(v_definition, v_old, v_new);
end;
$migration$;



do $migration$
declare
    v_definition text;
    v_old text := $old$    if v_outcome = 'resolved' then
        if v_event_type = 'PURCHASE_APPROVED' then
            update public.purchase_intents
            set lifecycle_state = 'purchased',
                current_classification = null,
                activation_authorized = false,
                updated_at = clock_timestamp()
            where id = v_resolved_intent_id
              and lifecycle_state = 'waiting_for_purchase';
        else
            update public.purchase_intents
            set current_classification = 'confirmed_abandonment',
                updated_at = clock_timestamp()
            where id = v_resolved_intent_id
              and lifecycle_state = 'waiting_for_purchase';
        end if;$old$;
    v_new text := $new$    if v_outcome = 'resolved' then
        if v_event_type = 'PURCHASE_APPROVED' then
            update public.purchase_intents
            set lifecycle_state = 'purchased',
                current_classification = null,
                activation_authorized = false,
                updated_at = clock_timestamp()
            where id = v_resolved_intent_id
              and lifecycle_state = 'waiting_for_purchase';
        elsif v_event_type = 'PURCHASE_CANCELED' then
            update public.purchase_intents
            set current_classification = 'payment_failure_supported',
                updated_at = clock_timestamp()
            where id = v_resolved_intent_id
              and lifecycle_state = 'waiting_for_purchase';
        else
            update public.purchase_intents
            set current_classification = 'confirmed_abandonment',
                updated_at = clock_timestamp()
            where id = v_resolved_intent_id
              and lifecycle_state = 'waiting_for_purchase';
        end if;$new$;
begin
    select pg_get_functiondef(
        'public.correlate_hotmart_purchase_intent(uuid)'::regprocedure
    ) into v_definition;
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_old, ''))
          <> length(v_old) then
        raise exception using errcode = '55000',
            message = 'unexpected_hotmart_intent_classification_definition';
    end if;
    execute replace(v_definition, v_old, v_new);
end;
$migration$;



create function public.admit_portable_hotmart_payment_failure(
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
       or p_payload #>> '{data,purchase,offer,code}' is distinct from v_binding.offer_code then
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
      and scope.offer_ref=v_binding.offer_code and scope.active for update;
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

        select fs.id, fs.status into v_sequence_id, v_sequence_status
        from public.followup_sequences fs
        where fs.recovery_case_id = v_case_id
        order by fs.created_at desc
        limit 1
        for update;

        if v_sequence_id is not null then
            select sa.id into v_action_id
            from public.scheduled_actions sa
            where sa.followup_sequence_id = v_sequence_id
              and sa.status in (
                  'pending', 'deferred', 'retryable_failed', 'delivery_unknown'
              )
            for update;
        end if;

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

CREATE OR REPLACE FUNCTION public.plan_payment_failure_recovery_with_identity(
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
    p_external_user_id text
)
RETURNS TABLE (
    recovery_case_id uuid,
    followup_sequence_id uuid,
    scheduled_action_id uuid,
    created boolean
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $function$
DECLARE
    v_plan record;
    v_contact public.contacts%rowtype;
    v_case public.recovery_cases%rowtype;
    v_identity public.channel_identities%rowtype;
    v_account_id text;
    v_now timestamptz := clock_timestamp();
BEGIN
    IF p_chatwoot_account_id IS NULL OR p_chatwoot_account_id < 1 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'chatwoot_account_id_invalid';
    END IF;
    IF p_chatwoot_inbox_id IS NULL OR p_chatwoot_inbox_id < 1 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'chatwoot_inbox_id_invalid';
    END IF;
    IF p_external_user_id IS NULL
       OR btrim(p_external_user_id) = ''
       OR NOT (p_external_user_id ~ '^[0-9]+$') THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'external_user_id_invalid';
    END IF;

    v_account_id := 'chatwoot:' || p_chatwoot_account_id::text;

    SELECT * INTO STRICT v_plan
    FROM public.plan_payment_failure_recovery(
        p_webhook_event_id,
        p_contact_id,
        p_external_product_id,
        p_product_name,
        p_offer_code,
        p_policy_key,
        p_policy_version,
        p_failed_at
    );

    -- Preserve the global lock order already established by plan_cart_recovery.
    SELECT * INTO STRICT v_contact
    FROM public.contacts c
    WHERE c.id = p_contact_id
    FOR UPDATE;

    SELECT * INTO STRICT v_case
    FROM public.recovery_cases rc
    WHERE rc.id = v_plan.recovery_case_id
      AND rc.contact_id = p_contact_id
    FOR UPDATE;

    SELECT * INTO v_identity
    FROM public.channel_identities ci
    WHERE ci.channel = 'whatsapp'
      AND ci.account_id = v_account_id
      AND ci.external_user_id = p_external_user_id
    FOR UPDATE;

    IF v_identity.id IS NULL THEN
        BEGIN
            INSERT INTO public.channel_identities (
                contact_id,
                channel,
                account_id,
                external_user_id,
                identity_status,
                metadata
            ) VALUES (
                p_contact_id,
                'whatsapp',
                v_account_id,
                p_external_user_id,
                'active',
                jsonb_build_object('inbox_id', p_chatwoot_inbox_id)
            )
            RETURNING * INTO v_identity;
        EXCEPTION WHEN unique_violation THEN
            SELECT * INTO STRICT v_identity
            FROM public.channel_identities ci
            WHERE ci.channel = 'whatsapp'
              AND ci.account_id = v_account_id
              AND ci.external_user_id = p_external_user_id
            FOR UPDATE;
        END;
    END IF;

    IF v_identity.contact_id <> p_contact_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'channel_identity_contact_mismatch';
    END IF;
    IF v_identity.identity_status <> 'active' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'channel_identity_not_active';
    END IF;
    IF v_identity.metadata ? 'inbox_id'
       AND v_identity.metadata ->> 'inbox_id' <> p_chatwoot_inbox_id::text THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'channel_identity_inbox_mismatch';
    END IF;
    IF v_case.selected_channel_identity_id IS NOT NULL
       AND v_case.selected_channel_identity_id <> v_identity.id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'recovery_case_channel_identity_mismatch';
    END IF;

    UPDATE public.channel_identities
    SET metadata = metadata || jsonb_build_object('inbox_id', p_chatwoot_inbox_id),
        updated_at = clock_timestamp()
    WHERE id = v_identity.id;

    UPDATE public.recovery_cases
    SET selected_channel_identity_id = v_identity.id,
        identity_resolution_status = 'resolved',
        identity_resolution_error = NULL,
        identity_resolution_last_attempt_at = CASE
            WHEN identity_resolution_status = 'resolved'
                THEN identity_resolution_last_attempt_at
            ELSE clock_timestamp()
        END,
        identity_resolution_attempt_count = CASE
            WHEN identity_resolution_status = 'resolved'
                THEN identity_resolution_attempt_count
            ELSE identity_resolution_attempt_count + 1
        END
    WHERE id = v_case.id;

    RETURN QUERY
    SELECT
        v_plan.recovery_case_id::uuid,
        v_plan.followup_sequence_id::uuid,
        v_plan.scheduled_action_id::uuid,
        v_plan.created::boolean;
END;
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
       or v_scope.offer_code is distinct from v_provenance.offer_ref
       or v_runtime_binding.hotmart_product_id::text is distinct from p_external_product_id
       or v_runtime_binding.product_name is distinct from p_product_name
       or v_runtime_binding.offer_code is distinct from p_offer_code
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

create or replace function public.mark_portable_payment_failure_request_started(
    p_action_id uuid,
    p_attempt_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_now timestamptz
)
returns table (
    id uuid,
    action_id uuid,
    idempotency_key text,
    attempt_number integer,
    channel text,
    mode text,
    phase text,
    lease_generation bigint,
    expected_case_version bigint,
    expected_sequence_revision bigint,
    pilot_authorization_id uuid,
    pilot_runtime_generation bigint,
    pilot_authorization_replayed boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_case public.recovery_cases%rowtype;
    v_identity public.channel_identities%rowtype;
    v_binding public.pilot_recovery_case_bindings%rowtype;
    v_scope public.pilot_scope_versions%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_account_id bigint;
    v_inbox_id bigint;
    v_authorized boolean;
    v_reason text;
    v_runtime_generation bigint;
    v_authorization_id uuid;
    v_replayed boolean;
begin
    if p_action_id is null
       or p_attempt_id is null
       or p_worker_id is null or btrim(p_worker_id) = ''
       or p_lease_generation is null or p_lease_generation < 1
       or p_now is null then
        raise exception using
            errcode = '22023',
            message = 'invalid_pilot_request_start_parameters';
    end if;

    if not exists (
        select 1 from public.scheduled_actions action
        where action.id = p_action_id
          and action.anchor_type = 'payment_failure'
    ) then
        raise exception using
            errcode = '55000',
            message = 'pilot_request_start_rejected',
            detail = 'payment_failure_action_required';
    end if;

    select recovery_case.* into v_case
    from public.scheduled_actions action
    join public.recovery_cases recovery_case
      on recovery_case.id = action.recovery_case_id
    join public.pilot_recovery_case_bindings binding
      on binding.recovery_case_id = recovery_case.id
    where action.id = p_action_id;
    if not found or v_case.selected_channel_identity_id is null then
        raise exception using
            errcode = '55000',
            message = 'pilot_request_start_rejected',
            detail = 'pilot_attempt_mismatch';
    end if;

    select binding.* into strict v_binding
    from public.pilot_recovery_case_bindings binding
    where binding.recovery_case_id = v_case.id;
    select scope.* into strict v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = v_binding.scope_key
      and scope.version = v_binding.scope_version;

    select attempt.* into v_attempt
    from public.followup_delivery_attempts attempt
    where attempt.id = p_attempt_id
      and attempt.action_id = p_action_id;
    if not found
       or v_attempt.channel <> 'whatsapp'
       or (
           v_scope.channel_provider = 'waba'
           and v_attempt.mode <> 'approved_template'
       )
       or (
           v_scope.channel_provider <> 'waba'
           and v_attempt.mode <> 'freeform'
       ) then
        raise exception using
            errcode = '55000',
            message = 'pilot_request_start_rejected',
            detail = 'pilot_delivery_mode_mismatch';
    end if;

    select identity.* into v_identity
    from public.channel_identities identity
    where identity.id = v_case.selected_channel_identity_id;
    if not found
       or v_identity.account_id !~ '^chatwoot:[0-9]+$'
       or coalesce(v_identity.metadata ->> 'inbox_id', '') !~ '^[0-9]+$' then
        raise exception using
            errcode = '55000',
            message = 'pilot_request_start_rejected',
            detail = 'pilot_attempt_mismatch';
    end if;

    v_account_id := substring(v_identity.account_id from '^chatwoot:([0-9]+)$')::bigint;
    v_inbox_id := (v_identity.metadata ->> 'inbox_id')::bigint;

    select auth_result.authorized,
           auth_result.reason_code,
           auth_result.runtime_generation,
           auth_result.request_authorization_id,
           auth_result.replayed
      into v_authorized,
           v_reason,
           v_runtime_generation,
           v_authorization_id,
           v_replayed
    from public.authorize_lancemos_pilot_request_start(
        v_binding.scope_key,
        v_binding.scope_version,
        v_scope.tenant_key,
        v_account_id,
        v_inbox_id,
        v_scope.channel_provider,
        v_scope.channel_account_ref,
        'hotmart',
        'PURCHASE_CANCELED',
        v_case.external_product_id,
        v_case.offer_code,
        v_case.contact_id,
        p_action_id,
        p_attempt_id,
        p_now
    ) auth_result;

    if not coalesce(v_authorized, false) then
        raise exception using
            errcode = '55000',
            message = 'pilot_request_start_rejected',
            detail = coalesce(v_reason, 'pilot_request_start_unknown');
    end if;

    if v_replayed then
        select attempt.* into v_attempt
        from public.followup_delivery_attempts attempt
        where attempt.id = p_attempt_id
          and attempt.action_id = p_action_id;
        if not found or v_attempt.phase <> 'request_started' then
            raise exception using
                errcode = '55000',
                message = 'pilot_authorization_without_request_start';
        end if;
    end if;

    select attempt.* into strict v_attempt
    from public.mark_followup_request_started(
        p_action_id,
        p_attempt_id,
        p_worker_id,
        p_lease_generation,
        p_now
    ) attempt;

    return query select
        v_attempt.id,
        v_attempt.action_id,
        v_attempt.idempotency_key,
        v_attempt.attempt_number,
        v_attempt.channel,
        v_attempt.mode,
        v_attempt.phase,
        v_attempt.lease_generation,
        v_attempt.expected_case_version,
        v_attempt.expected_sequence_revision,
        v_authorization_id,
        v_runtime_generation,
        v_replayed;
end;
$function$;

revoke all on table public.commercial_ally_payment_failure_details from public;
revoke all on table public.commercial_ally_payment_failure_conflicts from public;
revoke all on function public.admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text) from public;
revoke all on function public.plan_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz) from public;
revoke all on function public.plan_payment_failure_recovery_with_identity(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text) from public;
revoke all on function public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) from public;
revoke all on function public.mark_portable_payment_failure_request_started(uuid,uuid,text,bigint,timestamptz) from public;
revoke all on function public.hotmart_payment_failure_payload_is_processable(text,jsonb) from public;
revoke all on function public.protect_commercial_ally_payment_failure_evidence() from public;
do $roles$
declare v_role text;
begin
 for v_role in select rolname from pg_roles where rolname in ('anon','authenticated','service_role') loop
  execute format('revoke all on table public.commercial_ally_payment_failure_details from %I',v_role);
  execute format('revoke all on table public.commercial_ally_payment_failure_conflicts from %I',v_role);
  execute format('revoke all on function public.admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text) from %I',v_role);
  execute format('revoke all on function public.plan_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz) from %I',v_role);
  execute format('revoke all on function public.plan_payment_failure_recovery_with_identity(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text) from %I',v_role);
  execute format('revoke all on function public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) from %I',v_role);
  execute format('revoke all on function public.mark_portable_payment_failure_request_started(uuid,uuid,text,bigint,timestamptz) from %I',v_role);
  execute format('revoke all on function public.hotmart_payment_failure_payload_is_processable(text,jsonb) from %I',v_role);
  execute format('revoke all on function public.protect_commercial_ally_payment_failure_evidence() from %I',v_role);
 end loop;
 if exists(select 1 from pg_roles where rolname='service_role') then
  grant execute on function public.admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text) to service_role;
  grant execute on function public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) to service_role;
  grant execute on function public.mark_portable_payment_failure_request_started(uuid,uuid,text,bigint,timestamptz) to service_role;
 end if;
end;
$roles$;

commit;
