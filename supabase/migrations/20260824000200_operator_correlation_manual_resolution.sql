-- Supervised manual resolution for non-unequivocal Hotmart correlations.
-- This migration preserves deterministic evidence and creates no activation,
-- timer, delivery, message, conversation, worker, or outbound effect.

begin;

create table public.operator_correlation_resolution_commands (
    id uuid primary key default gen_random_uuid(),
    idempotency_key uuid not null unique,
    request_fingerprint jsonb not null,
    webhook_event_id uuid not null
        references public.hotmart_purchase_intent_correlations(webhook_event_id)
        on delete restrict,
    scope_id uuid not null
        references public.hotmart_purchase_intent_scopes(id) on delete restrict,
    tenant_ref text not null,
    funnel_ref text not null,
    product_ref text not null,
    offer_ref text not null,
    actor_ref text not null,
    action text not null,
    selected_purchase_intent_id uuid
        references public.purchase_intents(id) on delete restrict,
    verification_basis text not null,
    deterministic_outcome text not null,
    deterministic_reason_code text not null,
    candidate_count integer not null,
    candidate_snapshot jsonb not null,
    prepared_at timestamptz not null default clock_timestamp(),
    expires_at timestamptz not null,
    unique (id, webhook_event_id),
    check (tenant_ref = btrim(tenant_ref) and tenant_ref <> ''),
    check (funnel_ref = btrim(funnel_ref) and funnel_ref <> ''),
    check (product_ref = btrim(product_ref) and product_ref <> ''),
    check (offer_ref = btrim(offer_ref) and offer_ref <> ''),
    check (actor_ref ~ '^[a-z0-9][a-z0-9._-]{1,63}$'),
    check (deterministic_outcome in ('unmatched', 'ambiguous', 'conflict')),
    check (candidate_count >= 0),
    check (jsonb_typeof(request_fingerprint) = 'object'),
    check (jsonb_typeof(candidate_snapshot) = 'array'),
    check (expires_at > prepared_at),
    check (
        (
            action = 'resolve_with_candidate'
            and selected_purchase_intent_id is not null
            and verification_basis in (
                'external_transaction_reference',
                'operator_source_record',
                'customer_confirmation'
            )
        )
        or (
            action = 'close_without_match'
            and selected_purchase_intent_id is null
            and verification_basis = 'no_valid_candidate_after_review'
        )
    )
);

create table public.operator_correlation_resolutions (
    id uuid primary key default gen_random_uuid(),
    command_id uuid not null,
    webhook_event_id uuid not null,
    resolution_outcome text not null,
    effective_purchase_intent_id uuid
        references public.purchase_intents(id) on delete restrict,
    actor_ref text not null,
    verification_basis text not null,
    deterministic_outcome text not null,
    applied_at timestamptz not null default clock_timestamp(),
    unique (command_id),
    unique (webhook_event_id),
    foreign key (command_id, webhook_event_id)
        references public.operator_correlation_resolution_commands(id, webhook_event_id)
        on delete restrict,
    check (actor_ref ~ '^[a-z0-9][a-z0-9._-]{1,63}$'),
    check (deterministic_outcome in ('unmatched', 'ambiguous', 'conflict')),
    check (
        (
            resolution_outcome = 'linked_candidate'
            and effective_purchase_intent_id is not null
        )
        or (
            resolution_outcome = 'closed_without_match'
            and effective_purchase_intent_id is null
        )
    )
);

alter table public.operator_correlation_resolution_commands enable row level security;
alter table public.operator_correlation_resolutions enable row level security;

create or replace function public.operator_correlation_resolution_rows_are_immutable()
returns trigger
language plpgsql
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    raise exception using
        errcode = '55000',
        message = 'operator_correlation_resolution_rows_are_immutable';
end;
$function$;

create trigger protect_operator_correlation_resolution_commands
before update or delete on public.operator_correlation_resolution_commands
for each row execute function public.operator_correlation_resolution_rows_are_immutable();

create trigger protect_operator_correlation_resolutions
before update or delete on public.operator_correlation_resolutions
for each row execute function public.operator_correlation_resolution_rows_are_immutable();

create or replace function public.validate_operator_correlation_resolution_command_insert()
returns trigger
language plpgsql
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_correlation public.hotmart_purchase_intent_correlations%rowtype;
    v_scope public.hotmart_purchase_intent_scopes%rowtype;
    v_candidate_snapshot jsonb;
    v_request_fingerprint jsonb;
begin
    select correlation.* into v_correlation
    from public.hotmart_purchase_intent_correlations correlation
    where correlation.webhook_event_id = new.webhook_event_id
    for share;

    if not found
       or v_correlation.scope_id is distinct from new.scope_id
       or v_correlation.outcome is distinct from new.deterministic_outcome
       or v_correlation.reason_code is distinct from new.deterministic_reason_code
       or v_correlation.candidate_count is distinct from new.candidate_count
       or not v_correlation.manual_handoff_required
       or v_correlation.purchase_intent_id is not null then
        raise exception using
            errcode = '23514',
            message = 'operator_correlation_resolution_command_invalid';
    end if;

    select scope.* into v_scope
    from public.hotmart_purchase_intent_scopes scope
    where scope.id = new.scope_id
      and scope.tenant_ref = new.tenant_ref
      and scope.funnel_ref = new.funnel_ref
      and scope.purchase_intent_product_ref = new.product_ref
      and scope.offer_ref = new.offer_ref
    for share;
    if not found then
        raise exception using
            errcode = '23514',
            message = 'operator_correlation_resolution_command_invalid';
    end if;

    perform intent.id
    from public.hotmart_purchase_intent_correlation_candidates candidate
    join public.purchase_intents intent
      on intent.id = candidate.purchase_intent_id
     and intent.tenant_ref = v_scope.tenant_ref
     and intent.funnel_ref = v_scope.funnel_ref
     and intent.product_ref = v_scope.purchase_intent_product_ref
     and intent.offer_ref = v_scope.offer_ref
     and intent.lifecycle_state = 'waiting_for_purchase'
    where candidate.webhook_event_id = new.webhook_event_id
    order by intent.id
    for share of intent;

    select coalesce(jsonb_agg(
        jsonb_build_object(
            'purchase_intent_id', candidate.purchase_intent_id,
            'email_match', candidate.email_match,
            'phone_match', candidate.phone_match,
            'lifecycle_state', intent.lifecycle_state,
            'provisional', intent.provisional,
            'provider_observed', intent.provider_observed,
            'updated_at', intent.updated_at
        ) order by candidate.purchase_intent_id
    ), '[]'::jsonb)
    into v_candidate_snapshot
    from public.hotmart_purchase_intent_correlation_candidates candidate
    join public.purchase_intents intent
      on intent.id = candidate.purchase_intent_id
     and intent.tenant_ref = v_scope.tenant_ref
     and intent.funnel_ref = v_scope.funnel_ref
     and intent.product_ref = v_scope.purchase_intent_product_ref
     and intent.offer_ref = v_scope.offer_ref
     and intent.lifecycle_state = 'waiting_for_purchase'
    where candidate.webhook_event_id = new.webhook_event_id;

    v_request_fingerprint := jsonb_build_object(
        'tenant_ref', new.tenant_ref,
        'funnel_ref', new.funnel_ref,
        'actor_ref', new.actor_ref,
        'webhook_event_id', new.webhook_event_id,
        'action', new.action,
        'selected_purchase_intent_id', new.selected_purchase_intent_id,
        'verification_basis', new.verification_basis
    );

    if new.candidate_snapshot is distinct from v_candidate_snapshot
       or new.candidate_count <> jsonb_array_length(v_candidate_snapshot)
       or new.request_fingerprint is distinct from v_request_fingerprint
       or new.expires_at is distinct from new.prepared_at + interval '10 minutes'
       or (
           new.action = 'resolve_with_candidate'
           and not exists (
               select 1
               from jsonb_array_elements(v_candidate_snapshot) item
               where item ->> 'purchase_intent_id'
                     = new.selected_purchase_intent_id::text
           )
       ) then
        raise exception using
            errcode = '23514',
            message = 'operator_correlation_resolution_command_invalid';
    end if;

    return new;
end;
$function$;

create trigger validate_operator_correlation_command_before_insert
before insert on public.operator_correlation_resolution_commands
for each row execute function public.validate_operator_correlation_resolution_command_insert();

create or replace function public.validate_operator_correlation_resolution_insert()
returns trigger
language plpgsql
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_command public.operator_correlation_resolution_commands%rowtype;
begin
    select command.* into v_command
    from public.operator_correlation_resolution_commands command
    where command.id = new.command_id
      and command.webhook_event_id = new.webhook_event_id;
    if not found
       or new.actor_ref is distinct from v_command.actor_ref
       or new.verification_basis is distinct from v_command.verification_basis
       or new.deterministic_outcome is distinct from v_command.deterministic_outcome
       or (
           v_command.action = 'resolve_with_candidate'
           and (
               new.resolution_outcome <> 'linked_candidate'
               or new.effective_purchase_intent_id
                    is distinct from v_command.selected_purchase_intent_id
           )
       )
       or (
           v_command.action = 'close_without_match'
           and (
               new.resolution_outcome <> 'closed_without_match'
               or new.effective_purchase_intent_id is not null
           )
       ) then
        raise exception using
            errcode = '23514',
            message = 'operator_correlation_resolution_command_mismatch';
    end if;
    return new;
end;
$function$;

create trigger validate_operator_correlation_resolution_before_insert
before insert on public.operator_correlation_resolutions
for each row execute function public.validate_operator_correlation_resolution_insert();

create or replace function public.prepare_operator_correlation_resolution(
    p_tenant_ref text,
    p_funnel_ref text,
    p_actor_ref text,
    p_webhook_event_id uuid,
    p_action text,
    p_selected_purchase_intent_id uuid,
    p_verification_basis text,
    p_idempotency_key uuid
)
returns table (command_data jsonb)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_correlation public.hotmart_purchase_intent_correlations%rowtype;
    v_scope public.hotmart_purchase_intent_scopes%rowtype;
    v_command public.operator_correlation_resolution_commands%rowtype;
    v_existing public.operator_correlation_resolution_commands%rowtype;
    v_candidate_snapshot jsonb;
    v_request_fingerprint jsonb;
begin
    if p_tenant_ref is null or p_tenant_ref <> btrim(p_tenant_ref)
       or p_tenant_ref = ''
       or p_funnel_ref is null or p_funnel_ref <> btrim(p_funnel_ref)
       or p_funnel_ref = ''
       or p_actor_ref is null
       or p_actor_ref !~ '^[a-z0-9][a-z0-9._-]{1,63}$'
       or p_idempotency_key is null
       or p_webhook_event_id is null
       or p_action not in ('resolve_with_candidate', 'close_without_match') then
        raise exception using
            errcode = '22023',
            message = 'invalid_operator_correlation_resolution';
    end if;
    if (
        p_action = 'resolve_with_candidate'
        and (
            p_selected_purchase_intent_id is null
            or p_verification_basis not in (
                'external_transaction_reference',
                'operator_source_record',
                'customer_confirmation'
            )
        )
    ) or (
        p_action = 'close_without_match'
        and (
            p_selected_purchase_intent_id is not null
            or p_verification_basis <> 'no_valid_candidate_after_review'
        )
    ) then
        raise exception using
            errcode = '22023',
            message = 'invalid_operator_correlation_resolution';
    end if;

    v_request_fingerprint := jsonb_build_object(
        'tenant_ref', p_tenant_ref,
        'funnel_ref', p_funnel_ref,
        'actor_ref', p_actor_ref,
        'webhook_event_id', p_webhook_event_id,
        'action', p_action,
        'selected_purchase_intent_id', p_selected_purchase_intent_id,
        'verification_basis', p_verification_basis
    );

    perform pg_advisory_xact_lock(
        hashtextextended('operator-correlation-prepare:' || p_idempotency_key::text, 0)
    );
    select command.* into v_existing
    from public.operator_correlation_resolution_commands command
    where command.idempotency_key = p_idempotency_key;
    if found then
        if v_existing.request_fingerprint is distinct from v_request_fingerprint then
            raise exception using
                errcode = '23505',
                message = 'operator_correlation_idempotency_conflict';
        end if;
        return query select jsonb_build_object(
            'command_id', v_existing.id,
            'idempotency_key', v_existing.idempotency_key,
            'webhook_event_id', v_existing.webhook_event_id,
            'action', v_existing.action,
            'selected_purchase_intent_id', v_existing.selected_purchase_intent_id,
            'verification_basis', v_existing.verification_basis,
            'deterministic_outcome', v_existing.deterministic_outcome,
            'deterministic_reason_code', v_existing.deterministic_reason_code,
            'candidate_count', v_existing.candidate_count,
            'expires_at', v_existing.expires_at,
            'requires_human_approval', true,
            'automation_blocked', true
        );
        return;
    end if;

    perform pg_advisory_xact_lock(hashtextextended(p_webhook_event_id::text, 0));

    select correlation.* into v_correlation
    from public.hotmart_purchase_intent_correlations correlation
    where correlation.webhook_event_id = p_webhook_event_id
    for update;
    if not found then
        raise exception using
            errcode = 'P0002',
            message = 'operator_correlation_case_not_found';
    end if;

    select scope.* into v_scope
    from public.hotmart_purchase_intent_scopes scope
    where scope.id = v_correlation.scope_id
      and scope.tenant_ref = p_tenant_ref
      and scope.funnel_ref = p_funnel_ref
    for share;
    if not found then
        raise exception using
            errcode = 'P0002',
            message = 'operator_correlation_case_not_found';
    end if;

    if v_correlation.outcome not in ('unmatched', 'ambiguous', 'conflict')
       or not v_correlation.manual_handoff_required
       or v_correlation.purchase_intent_id is not null then
        raise exception using
            errcode = '55000',
            message = 'operator_correlation_stale_evidence';
    end if;
    if exists (
        select 1 from public.operator_correlation_resolutions resolution
        where resolution.webhook_event_id = p_webhook_event_id
    ) then
        raise exception using
            errcode = '23505',
            message = 'operator_correlation_already_resolved';
    end if;

    select coalesce(jsonb_agg(
        jsonb_build_object(
            'purchase_intent_id', candidate.purchase_intent_id,
            'email_match', candidate.email_match,
            'phone_match', candidate.phone_match,
            'lifecycle_state', intent.lifecycle_state,
            'provisional', intent.provisional,
            'provider_observed', intent.provider_observed,
            'updated_at', intent.updated_at
        ) order by candidate.purchase_intent_id
    ), '[]'::jsonb)
    into v_candidate_snapshot
    from public.hotmart_purchase_intent_correlation_candidates candidate
    join public.purchase_intents intent
      on intent.id = candidate.purchase_intent_id
     and intent.tenant_ref = v_scope.tenant_ref
     and intent.funnel_ref = v_scope.funnel_ref
     and intent.product_ref = v_scope.purchase_intent_product_ref
     and intent.offer_ref = v_scope.offer_ref
     and intent.lifecycle_state = 'waiting_for_purchase'
    where candidate.webhook_event_id = p_webhook_event_id;

    if jsonb_array_length(v_candidate_snapshot) <> v_correlation.candidate_count then
        raise exception using
            errcode = '55000',
            message = 'operator_correlation_stale_evidence';
    end if;
    if p_action = 'resolve_with_candidate' and not exists (
        select 1
        from jsonb_array_elements(v_candidate_snapshot) candidate
        where candidate ->> 'purchase_intent_id'
            = p_selected_purchase_intent_id::text
    ) then
        raise exception using
            errcode = '22023',
            message = 'invalid_operator_correlation_resolution';
    end if;

    insert into public.operator_correlation_resolution_commands (
        idempotency_key,
        request_fingerprint,
        webhook_event_id,
        scope_id,
        tenant_ref,
        funnel_ref,
        product_ref,
        offer_ref,
        actor_ref,
        action,
        selected_purchase_intent_id,
        verification_basis,
        deterministic_outcome,
        deterministic_reason_code,
        candidate_count,
        candidate_snapshot,
        expires_at
    ) values (
        p_idempotency_key,
        v_request_fingerprint,
        p_webhook_event_id,
        v_scope.id,
        v_scope.tenant_ref,
        v_scope.funnel_ref,
        v_scope.purchase_intent_product_ref,
        v_scope.offer_ref,
        p_actor_ref,
        p_action,
        p_selected_purchase_intent_id,
        p_verification_basis,
        v_correlation.outcome,
        v_correlation.reason_code,
        v_correlation.candidate_count,
        v_candidate_snapshot,
        clock_timestamp() + interval '10 minutes'
    ) returning * into v_command;

    return query select jsonb_build_object(
        'command_id', v_command.id,
        'idempotency_key', v_command.idempotency_key,
        'webhook_event_id', v_command.webhook_event_id,
        'action', v_command.action,
        'selected_purchase_intent_id', v_command.selected_purchase_intent_id,
        'verification_basis', v_command.verification_basis,
        'deterministic_outcome', v_command.deterministic_outcome,
        'deterministic_reason_code', v_command.deterministic_reason_code,
        'candidate_count', v_command.candidate_count,
        'expires_at', v_command.expires_at,
        'requires_human_approval', true,
        'automation_blocked', true
    );
end;
$function$;

create or replace function public.confirm_operator_correlation_resolution(
    p_tenant_ref text,
    p_funnel_ref text,
    p_actor_ref text,
    p_command_id uuid,
    p_expected_action text,
    p_expected_purchase_intent_id uuid
)
returns table (resolution_data jsonb)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_event_id uuid;
    v_command public.operator_correlation_resolution_commands%rowtype;
    v_correlation public.hotmart_purchase_intent_correlations%rowtype;
    v_scope public.hotmart_purchase_intent_scopes%rowtype;
    v_existing public.operator_correlation_resolutions%rowtype;
    v_resolution public.operator_correlation_resolutions%rowtype;
    v_candidate_snapshot jsonb;
begin
    if p_tenant_ref is null or p_tenant_ref <> btrim(p_tenant_ref)
       or p_tenant_ref = ''
       or p_funnel_ref is null or p_funnel_ref <> btrim(p_funnel_ref)
       or p_funnel_ref = ''
       or p_actor_ref is null
       or p_actor_ref !~ '^[a-z0-9][a-z0-9._-]{1,63}$'
       or p_command_id is null
       or p_expected_action not in ('resolve_with_candidate', 'close_without_match')
       or (
           p_expected_action = 'resolve_with_candidate'
           and p_expected_purchase_intent_id is null
       )
       or (
           p_expected_action = 'close_without_match'
           and p_expected_purchase_intent_id is not null
       ) then
        raise exception using
            errcode = '22023',
            message = 'invalid_operator_correlation_resolution';
    end if;

    select command.webhook_event_id into v_event_id
    from public.operator_correlation_resolution_commands command
    where command.id = p_command_id;
    if not found then
        raise exception using
            errcode = 'P0002',
            message = 'operator_correlation_case_not_found';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(v_event_id::text, 0));

    select command.* into v_command
    from public.operator_correlation_resolution_commands command
    where command.id = p_command_id
    for share;
    if not found
       or v_command.tenant_ref <> p_tenant_ref
       or v_command.funnel_ref <> p_funnel_ref
       or v_command.actor_ref <> p_actor_ref then
        raise exception using
            errcode = 'P0002',
            message = 'operator_correlation_case_not_found';
    end if;
    if v_command.action <> p_expected_action
       or v_command.selected_purchase_intent_id
            is distinct from p_expected_purchase_intent_id then
        raise exception using
            errcode = '55000',
            message = 'operator_correlation_stale_evidence';
    end if;

    select resolution.* into v_existing
    from public.operator_correlation_resolutions resolution
    where resolution.command_id = p_command_id;
    if found then
        return query select jsonb_build_object(
            'resolution_id', v_existing.id,
            'command_id', v_existing.command_id,
            'webhook_event_id', v_existing.webhook_event_id,
            'resolution_outcome', v_existing.resolution_outcome,
            'effective_purchase_intent_id', v_existing.effective_purchase_intent_id,
            'deterministic_outcome', v_existing.deterministic_outcome,
            'applied_at', v_existing.applied_at,
            'replayed', true,
            'automation_blocked', true
        );
        return;
    end if;

    if exists (
        select 1 from public.operator_correlation_resolutions resolution
        where resolution.webhook_event_id = v_command.webhook_event_id
    ) then
        raise exception using
            errcode = '23505',
            message = 'operator_correlation_already_resolved';
    end if;
    if clock_timestamp() >= v_command.expires_at then
        raise exception using
            errcode = '55000',
            message = 'operator_correlation_command_expired';
    end if;

    select correlation.* into v_correlation
    from public.hotmart_purchase_intent_correlations correlation
    where correlation.webhook_event_id = v_command.webhook_event_id
    for update;
    if not found
       or v_correlation.outcome <> v_command.deterministic_outcome
       or v_correlation.reason_code <> v_command.deterministic_reason_code
       or v_correlation.candidate_count <> v_command.candidate_count
       or not v_correlation.manual_handoff_required
       or v_correlation.purchase_intent_id is not null then
        raise exception using
            errcode = '55000',
            message = 'operator_correlation_stale_evidence';
    end if;

    select scope.* into v_scope
    from public.hotmart_purchase_intent_scopes scope
    where scope.id = v_command.scope_id
      and scope.tenant_ref = v_command.tenant_ref
      and scope.funnel_ref = v_command.funnel_ref
      and scope.purchase_intent_product_ref = v_command.product_ref
      and scope.offer_ref = v_command.offer_ref
    for share;
    if not found then
        raise exception using
            errcode = '55000',
            message = 'operator_correlation_stale_evidence';
    end if;

    perform intent.id
    from public.hotmart_purchase_intent_correlation_candidates candidate
    join public.purchase_intents intent
      on intent.id = candidate.purchase_intent_id
     and intent.tenant_ref = v_scope.tenant_ref
     and intent.funnel_ref = v_scope.funnel_ref
     and intent.product_ref = v_scope.purchase_intent_product_ref
     and intent.offer_ref = v_scope.offer_ref
     and intent.lifecycle_state = 'waiting_for_purchase'
    where candidate.webhook_event_id = v_command.webhook_event_id
    order by intent.id
    for share of intent;

    select coalesce(jsonb_agg(
        jsonb_build_object(
            'purchase_intent_id', candidate.purchase_intent_id,
            'email_match', candidate.email_match,
            'phone_match', candidate.phone_match,
            'lifecycle_state', intent.lifecycle_state,
            'provisional', intent.provisional,
            'provider_observed', intent.provider_observed,
            'updated_at', intent.updated_at
        ) order by candidate.purchase_intent_id
    ), '[]'::jsonb)
    into v_candidate_snapshot
    from public.hotmart_purchase_intent_correlation_candidates candidate
    join public.purchase_intents intent
      on intent.id = candidate.purchase_intent_id
     and intent.tenant_ref = v_scope.tenant_ref
     and intent.funnel_ref = v_scope.funnel_ref
     and intent.product_ref = v_scope.purchase_intent_product_ref
     and intent.offer_ref = v_scope.offer_ref
     and intent.lifecycle_state = 'waiting_for_purchase'
    where candidate.webhook_event_id = v_command.webhook_event_id;

    if v_candidate_snapshot is distinct from v_command.candidate_snapshot then
        raise exception using
            errcode = '55000',
            message = 'operator_correlation_stale_evidence';
    end if;

    insert into public.operator_correlation_resolutions (
        command_id,
        webhook_event_id,
        resolution_outcome,
        effective_purchase_intent_id,
        actor_ref,
        verification_basis,
        deterministic_outcome
    ) values (
        v_command.id,
        v_command.webhook_event_id,
        case v_command.action
            when 'resolve_with_candidate' then 'linked_candidate'
            else 'closed_without_match'
        end,
        v_command.selected_purchase_intent_id,
        v_command.actor_ref,
        v_command.verification_basis,
        v_command.deterministic_outcome
    ) returning * into v_resolution;

    return query select jsonb_build_object(
        'resolution_id', v_resolution.id,
        'command_id', v_resolution.command_id,
        'webhook_event_id', v_resolution.webhook_event_id,
        'resolution_outcome', v_resolution.resolution_outcome,
        'effective_purchase_intent_id', v_resolution.effective_purchase_intent_id,
        'deterministic_outcome', v_resolution.deterministic_outcome,
        'applied_at', v_resolution.applied_at,
        'replayed', false,
        'automation_blocked', true
    );
end;
$function$;

-- Exclude manually resolved cases while preserving the original deterministic row.
create or replace function public.list_operator_unresolved_correlations(
    p_tenant_ref text,
    p_funnel_ref text,
    p_limit integer default 20,
    p_webhook_event_id uuid default null
)
returns table (case_data jsonb)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_tenant_ref is null or nullif(btrim(p_tenant_ref), '') is null
       or p_funnel_ref is null or nullif(btrim(p_funnel_ref), '') is null then
        raise exception using
            errcode = '22023',
            message = 'invalid_operator_correlation_scope';
    end if;
    if p_limit is null or p_limit < 1 or p_limit > 50 then
        raise exception using
            errcode = '22023',
            message = 'invalid_operator_correlation_limit';
    end if;

    return query
    select jsonb_build_object(
        'webhook_event_id', correlation.webhook_event_id,
        'scope_id', correlation.scope_id,
        'event_type', correlation.event_type,
        'outcome', correlation.outcome,
        'candidate_count', correlation.candidate_count,
        'reason_code', correlation.reason_code,
        'manual_handoff_required', correlation.manual_handoff_required,
        'observed_at', correlation.observed_at,
        'scope', case
            when scope.id is null then null
            else jsonb_build_object(
                'tenant_ref', scope.tenant_ref,
                'funnel_ref', scope.funnel_ref,
                'product_ref', scope.purchase_intent_product_ref,
                'offer_ref', scope.offer_ref
            )
        end,
        'identity', jsonb_build_object(
            'email_present', identity.normalized_email is not null,
            'phone_present', identity.normalized_phone is not null,
            'masked_email', case
                when identity.normalized_email is null then null
                when length(split_part(identity.normalized_email, '@', 1)) <= 2
                    then '***@' || split_part(identity.normalized_email, '@', 2)
                else left(split_part(identity.normalized_email, '@', 1), 1)
                    || '***'
                    || right(split_part(identity.normalized_email, '@', 1), 1)
                    || '@'
                    || split_part(identity.normalized_email, '@', 2)
            end,
            'masked_phone', case
                when identity.normalized_phone is null then null
                else repeat('*', greatest(length(identity.normalized_phone) - 4, 0))
                    || right(identity.normalized_phone, 4)
            end
        ),
        'candidates', coalesce((
            select jsonb_agg(
                jsonb_build_object(
                    'purchase_intent_id', candidate.purchase_intent_id,
                    'email_match', candidate.email_match,
                    'phone_match', candidate.phone_match,
                    'submitted_at', intent.submitted_at,
                    'lifecycle_state', intent.lifecycle_state,
                    'masked_email', case
                        when intent.normalized_email is null then null
                        when length(split_part(intent.normalized_email, '@', 1)) <= 2
                            then '***@' || split_part(intent.normalized_email, '@', 2)
                        else left(split_part(intent.normalized_email, '@', 1), 1)
                            || '***'
                            || right(split_part(intent.normalized_email, '@', 1), 1)
                            || '@'
                            || split_part(intent.normalized_email, '@', 2)
                    end,
                    'masked_phone', repeat(
                        '*', greatest(length(intent.normalized_phone) - 4, 0)
                    ) || right(intent.normalized_phone, 4)
                )
                order by candidate.purchase_intent_id
            )
            from public.hotmart_purchase_intent_correlation_candidates candidate
            join public.purchase_intents intent
              on intent.id = candidate.purchase_intent_id
             and intent.tenant_ref = scope.tenant_ref
             and intent.funnel_ref = scope.funnel_ref
             and intent.product_ref = scope.purchase_intent_product_ref
             and intent.offer_ref = scope.offer_ref
            where candidate.webhook_event_id = correlation.webhook_event_id
        ), '[]'::jsonb)
    )
    from public.hotmart_purchase_intent_correlations correlation
    left join public.hotmart_purchase_intent_scopes scope
      on scope.id = correlation.scope_id
    left join public.hotmart_purchase_intent_event_identities identity
      on identity.webhook_event_id = correlation.webhook_event_id
    where correlation.manual_handoff_required
      and correlation.purchase_intent_id is null
      and correlation.outcome in ('unmatched', 'ambiguous', 'conflict')
      and scope.tenant_ref = p_tenant_ref
      and scope.funnel_ref = p_funnel_ref
      and not exists (
          select 1
          from public.operator_correlation_resolutions resolution
          where resolution.webhook_event_id = correlation.webhook_event_id
      )
      and (
          p_webhook_event_id is null
          or correlation.webhook_event_id = p_webhook_event_id
      )
    order by correlation.observed_at desc, correlation.webhook_event_id asc
    limit p_limit;
end;
$function$;

revoke all on table public.operator_correlation_resolution_commands from public;
revoke all on table public.operator_correlation_resolutions from public;
revoke execute on function public.operator_correlation_resolution_rows_are_immutable() from public;
revoke execute on function public.validate_operator_correlation_resolution_command_insert() from public;
revoke execute on function public.validate_operator_correlation_resolution_insert() from public;
revoke execute on function public.prepare_operator_correlation_resolution(text, text, text, uuid, text, uuid, text, uuid) from public;
revoke execute on function public.confirm_operator_correlation_resolution(text, text, text, uuid, text, uuid) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on table public.operator_correlation_resolution_commands from anon;
        revoke all on table public.operator_correlation_resolutions from anon;
        revoke execute on function public.operator_correlation_resolution_rows_are_immutable() from anon;
        revoke execute on function public.validate_operator_correlation_resolution_command_insert() from anon;
        revoke execute on function public.validate_operator_correlation_resolution_insert() from anon;
        revoke execute on function public.prepare_operator_correlation_resolution(text, text, text, uuid, text, uuid, text, uuid) from anon;
        revoke execute on function public.confirm_operator_correlation_resolution(text, text, text, uuid, text, uuid) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on table public.operator_correlation_resolution_commands from authenticated;
        revoke all on table public.operator_correlation_resolutions from authenticated;
        revoke execute on function public.operator_correlation_resolution_rows_are_immutable() from authenticated;
        revoke execute on function public.validate_operator_correlation_resolution_command_insert() from authenticated;
        revoke execute on function public.validate_operator_correlation_resolution_insert() from authenticated;
        revoke execute on function public.prepare_operator_correlation_resolution(text, text, text, uuid, text, uuid, text, uuid) from authenticated;
        revoke execute on function public.confirm_operator_correlation_resolution(text, text, text, uuid, text, uuid) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on table public.operator_correlation_resolution_commands from service_role;
        revoke all on table public.operator_correlation_resolutions from service_role;
        revoke execute on function public.operator_correlation_resolution_rows_are_immutable() from service_role;
        revoke execute on function public.validate_operator_correlation_resolution_command_insert() from service_role;
        revoke execute on function public.validate_operator_correlation_resolution_insert() from service_role;
        revoke execute on function public.prepare_operator_correlation_resolution(text, text, text, uuid, text, uuid, text, uuid) from service_role;
        revoke execute on function public.confirm_operator_correlation_resolution(text, text, text, uuid, text, uuid) from service_role;
        grant execute on function public.prepare_operator_correlation_resolution(text, text, text, uuid, text, uuid, text, uuid) to service_role;
        grant execute on function public.confirm_operator_correlation_resolution(text, text, text, uuid, text, uuid) to service_role;
    end if;
end;
$roles$;

commit;
