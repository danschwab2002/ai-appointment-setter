begin;

create table public.pilot_recovery_case_bindings (
    recovery_case_id uuid primary key
        references public.recovery_cases(id) on delete restrict,
    scope_key text not null,
    scope_version integer not null,
    source_event_id uuid not null
        references public.webhook_events(id) on delete restrict,
    bound_at timestamptz not null default clock_timestamp(),
    foreign key (scope_key, scope_version)
        references public.pilot_scope_versions(scope_key, version)
        on delete restrict
);

alter table public.pilot_recovery_case_bindings enable row level security;

create trigger pilot_recovery_case_bindings_append_only
before update or delete on public.pilot_recovery_case_bindings
for each row execute function public.reject_pilot_append_only_mutation();

create or replace function public.get_lancemos_pilot_runtime_status(
    p_scope_key text,
    p_scope_version integer,
    p_tenant_key text,
    p_channel_provider text,
    p_channel_account_ref text
)
returns table (
    configured boolean,
    runtime_state text,
    runtime_generation bigint,
    reason_code text
)
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $function$
declare
    v_scope public.pilot_scope_versions%rowtype;
    v_control public.pilot_runtime_controls%rowtype;
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1
       or p_tenant_key is null or btrim(p_tenant_key) = ''
       or p_channel_provider is null or btrim(p_channel_provider) = ''
       or p_channel_account_ref is null or btrim(p_channel_account_ref) = '' then
        return query select false, null::text, null::bigint,
            'pilot_runtime_config_invalid'::text;
        return;
    end if;

    select scope.* into v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';
    if not found
       or v_scope.tenant_key <> p_tenant_key
       or v_scope.channel_provider <> p_channel_provider
       or v_scope.channel_account_ref <> p_channel_account_ref
       or v_scope.source <> 'hotmart'
       or v_scope.source_event_type <> 'PURCHASE_OUT_OF_SHOPPING_CART' then
        return query select false, null::text, null::bigint,
            'pilot_scope_config_mismatch'::text;
        return;
    end if;

    select control.* into v_control
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key;
    if not found or v_control.scope_version <> p_scope_version then
        return query select false, null::text, null::bigint,
            'pilot_active_scope_mismatch'::text;
        return;
    end if;

    return query select
        true,
        v_control.runtime_state,
        v_control.generation,
        ('pilot_runtime_' || v_control.runtime_state)::text;
end;
$function$;

create or replace function public.plan_lancemos_pilot_cart_recovery(
    p_webhook_event_id uuid,
    p_contact_id uuid,
    p_external_product_id text,
    p_product_name text,
    p_offer_code text,
    p_policy_key text,
    p_policy_version integer,
    p_abandoned_at timestamptz,
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
set search_path = public, pg_temp
as $function$
declare
    v_scope public.pilot_scope_versions%rowtype;
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
        'PURCHASE_OUT_OF_SHOPPING_CART',
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
    from public.plan_cart_recovery_with_identity(
        p_webhook_event_id,
        p_contact_id,
        p_external_product_id,
        p_product_name,
        p_offer_code,
        p_policy_key,
        p_policy_version,
        p_abandoned_at,
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
       or v_binding.scope_version <> p_scope_version
       or v_binding.source_event_id <> p_webhook_event_id then
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

-- The historical request-start guard only admitted freeform attempts. Keep its
-- complete locking/authorization logic and widen the private helper to the one
-- additional durable mode used by an approved WABA template. The public pilot
-- wrapper below remains responsible for provider/mode compatibility.
do $migration$
declare
    v_definition text;
    v_old text := 'v_attempt.mode = ''freeform''';
    v_new text := 'v_attempt.mode in (''freeform'', ''approved_template'')';
begin
    select pg_get_functiondef(
        'public._mark_followup_request_started_without_opt_out_guard(uuid,uuid,text,bigint,timestamptz)'::regprocedure
    ) into v_definition;
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_old, ''))
          <> length(v_old) then
        raise exception using
            errcode = '55000',
            message = 'unexpected_historical_request_start_definition';
    end if;
    execute replace(v_definition, v_old, v_new);
end;
$migration$;

alter function public.mark_followup_request_started(
    uuid, uuid, text, bigint, timestamptz
) rename to _mark_followup_request_started_without_pilot_guard;

create or replace function public.mark_followup_request_started(
    p_action_id uuid,
    p_attempt_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_now timestamptz
)
returns setof public.followup_delivery_attempts
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_account_id bigint;
    v_external_user_id text;
begin
    select substring(identity.account_id from '^chatwoot:([0-9]+)$')::bigint,
           identity.external_user_id
      into v_account_id, v_external_user_id
    from public.scheduled_actions action
    join public.recovery_cases recovery_case
      on recovery_case.id = action.recovery_case_id
    join public.channel_identities identity
      on identity.id = recovery_case.selected_channel_identity_id
    where action.id = p_action_id
      and identity.channel = 'whatsapp'
      and identity.identity_status = 'active';

    if v_account_id is not null and v_external_user_id is not null then
        perform pg_advisory_xact_lock(hashtextextended(
            concat_ws(
                ':', 'chatwoot-opt-out-user', v_account_id, v_external_user_id
            ),
            0
        ));
        if exists (
            select 1
            from public.contact_opt_out_events optout
            where optout.source = 'chatwoot'
              and optout.channel = 'whatsapp'
              and optout.canonical_account_id = v_account_id
              and optout.external_user_id = v_external_user_id
              and optout.correlation_status in (
                  'applied', 'unmatched', 'ambiguous', 'evidence_conflict'
              )
        ) then
            raise exception using
                errcode = '55000',
                message = 'pending_chatwoot_opt_out_stop';
        end if;
    end if;

    if not exists (
        select 1
        from public.pilot_outbound_request_authorizations authrow
        where authrow.action_id = p_action_id
          and authrow.attempt_id = p_attempt_id
    ) then
        raise exception using
            errcode = '55000',
            message = 'pilot_request_authorization_required';
    end if;

    return query
    select *
    from public._mark_followup_request_started_without_pilot_guard(
        p_action_id,
        p_attempt_id,
        p_worker_id,
        p_lease_generation,
        p_now
    );
end;
$function$;

create or replace function public.mark_lancemos_pilot_request_started(
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
set search_path = public, pg_temp
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
        'PURCHASE_OUT_OF_SHOPPING_CART',
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

revoke execute on function public.get_lancemos_pilot_runtime_status(
    text, integer, text, text, text
) from public;
revoke execute on function public.plan_lancemos_pilot_cart_recovery(
    uuid, uuid, text, text, text, text, integer, timestamptz,
    bigint, bigint, text, text, integer
) from public;
revoke execute on function public.mark_lancemos_pilot_request_started(
    uuid, uuid, text, bigint, timestamptz
) from public;
revoke all on table public.pilot_recovery_case_bindings from public;
revoke execute on function public.mark_followup_request_started(
    uuid, uuid, text, bigint, timestamptz
) from public;
revoke execute on function public._mark_followup_request_started_without_pilot_guard(
    uuid, uuid, text, bigint, timestamptz
) from public;

-- The standalone phase-1 authorization and legacy planning RPCs are not runtime
-- entrypoints after this cut. Only the two atomic wrappers may cross these gates.
do $roles$
declare
    v_role text;
begin
    for v_role in
        select role.rolname
        from pg_roles role
        where role.rolname in ('anon', 'authenticated', 'service_role')
    loop
        execute format(
            'revoke execute on function public.get_lancemos_pilot_runtime_status(text, integer, text, text, text) from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.plan_lancemos_pilot_cart_recovery(uuid, uuid, text, text, text, text, integer, timestamptz, bigint, bigint, text, text, integer) from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.mark_lancemos_pilot_request_started(uuid, uuid, text, bigint, timestamptz) from %I',
            v_role
        );
        execute format(
            'revoke all on table public.pilot_recovery_case_bindings from %I',
            v_role
        );
        execute format(
            'revoke execute on function public._mark_followup_request_started_without_pilot_guard(uuid, uuid, text, bigint, timestamptz) from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.authorize_lancemos_pilot_request_start(text, integer, text, bigint, bigint, text, text, text, text, text, text, uuid, uuid, uuid, timestamptz) from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.plan_cart_recovery(uuid, uuid, text, text, text, text, integer, timestamptz) from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.plan_cart_recovery_with_identity(uuid, uuid, text, text, text, text, integer, timestamptz, bigint, bigint, text) from %I',
            v_role
        );
        execute format(
            'revoke execute on function public.mark_followup_request_started(uuid, uuid, text, bigint, timestamptz) from %I',
            v_role
        );
    end loop;

    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.get_lancemos_pilot_runtime_status(
            text, integer, text, text, text
        ) to service_role;
        grant execute on function public.plan_lancemos_pilot_cart_recovery(
            uuid, uuid, text, text, text, text, integer, timestamptz,
            bigint, bigint, text, text, integer
        ) to service_role;
        grant execute on function public.mark_lancemos_pilot_request_started(
            uuid, uuid, text, bigint, timestamptz
        ) to service_role;
    end if;
end;
$roles$;

commit;
