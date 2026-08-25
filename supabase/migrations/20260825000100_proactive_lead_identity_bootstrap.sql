-- Operator-owned, effect-free bootstrap for one authorized proactive lead.

begin;

create table public.proactive_lead_bootstrap_targets (
    scope_key text not null,
    scope_version integer not null check (scope_version > 0),
    channel_identity_id uuid not null unique
        references public.channel_identities(id) on delete restrict,
    approved_by text not null check (nullif(btrim(approved_by), '') is not null),
    approved_at timestamptz not null,
    created_at timestamptz not null default clock_timestamp(),
    primary key (scope_key, scope_version),
    foreign key (scope_key, scope_version)
        references public.pilot_scope_versions(scope_key, version) on delete restrict
);

alter table public.proactive_lead_bootstrap_targets enable row level security;

create table public.proactive_lead_identity_bootstrap_commands (
    command_key text primary key check (command_key ~ '^[a-z0-9:_-]{1,200}$'),
    semantic_fingerprint text not null check (semantic_fingerprint ~ '^[0-9a-f]{64}$'),
    purchase_intent_id uuid not null references public.purchase_intents(id) on delete restrict,
    scope_key text not null,
    scope_version integer not null check (scope_version > 0),
    channel_identity_id uuid not null references public.channel_identities(id) on delete restrict,
    contact_id uuid not null references public.contacts(id) on delete restrict,
    runtime_generation bigint not null check (runtime_generation >= 0),
    outcome text not null check (outcome = 'proactive_bootstrap_completed'),
    actor text not null check (nullif(btrim(actor), '') is not null),
    reason text not null check (nullif(btrim(reason), '') is not null),
    created_at timestamptz not null default clock_timestamp(),
    foreign key (scope_key, scope_version)
        references public.pilot_scope_versions(scope_key, version) on delete restrict
);

alter table public.proactive_lead_identity_bootstrap_commands enable row level security;

create or replace function public.protect_proactive_lead_identity_bootstrap_command()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    raise exception using
        errcode = '55000',
        message = 'proactive_bootstrap_command_is_immutable';
end;
$function$;

create trigger proactive_lead_identity_bootstrap_commands_immutable
before update or delete on public.proactive_lead_identity_bootstrap_commands
for each row execute function public.protect_proactive_lead_identity_bootstrap_command();

create trigger proactive_lead_bootstrap_targets_immutable
before update or delete on public.proactive_lead_bootstrap_targets
for each row execute function public.protect_proactive_lead_identity_bootstrap_command();

create or replace function public.bootstrap_proactive_lead_identity(
    p_command_key text,
    p_purchase_intent_id uuid,
    p_scope_key text,
    p_scope_version integer,
    p_expected_generation bigint,
    p_actor text,
    p_reason text
)
returns table (
    outcome text,
    command_key text,
    contact_id uuid,
    channel_identity_id uuid,
    runtime_generation bigint,
    changed boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    command public.proactive_lead_identity_bootstrap_commands%rowtype;
    scope public.pilot_scope_versions%rowtype;
    control public.pilot_runtime_controls%rowtype;
    intent public.purchase_intents%rowtype;
    identity public.channel_identities%rowtype;
    contact public.contacts%rowtype;
    target public.proactive_lead_bootstrap_targets%rowtype;
    cohort record;
    semantic_fingerprint text;
    phone_owner_count integer;
    phone_owned_by_identity boolean;
    matching_submission_count integer;
begin
    if p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_purchase_intent_id is null
       or p_scope_key is null or nullif(btrim(p_scope_key), '') is null
       or p_scope_version is null or p_scope_version < 1
       or p_expected_generation is null or p_expected_generation < 0
       or p_actor is null or nullif(btrim(p_actor), '') is null
       or p_reason is null or nullif(btrim(p_reason), '') is null then
        raise exception using errcode = '22023', message = 'proactive_bootstrap_input_invalid';
    end if;

    semantic_fingerprint := encode(sha256(convert_to(concat_ws(
        chr(31), p_purchase_intent_id::text, p_scope_key,
        p_scope_version::text, p_expected_generation::text
    ), 'UTF8')), 'hex');

    perform pg_advisory_xact_lock(hashtextextended('proactive_bootstrap:' || p_command_key, 0));

    select existing.* into command
    from public.proactive_lead_identity_bootstrap_commands existing
    where existing.command_key = p_command_key;

    if found then
        if command.semantic_fingerprint is distinct from semantic_fingerprint then
            raise exception using errcode = '23514', message = 'proactive_bootstrap_command_conflict';
        end if;
        return query select command.outcome, command.command_key, command.contact_id,
            command.channel_identity_id, command.runtime_generation, false;
        return;
    end if;

    select published.* into strict scope
    from public.pilot_scope_versions published
    where published.scope_key = p_scope_key
      and published.version = p_scope_version
      and published.status = 'published'
    for share;

    select runtime.* into strict control
    from public.pilot_runtime_controls runtime
    where runtime.scope_key = p_scope_key
      and runtime.scope_version = p_scope_version
    for update;

    if control.generation is distinct from p_expected_generation then
        raise exception using errcode = '40001', message = 'proactive_bootstrap_generation_mismatch';
    end if;
    if control.runtime_state not in ('inactive', 'paused') then
        raise exception using errcode = '55000', message = 'proactive_bootstrap_runtime_not_quiescent';
    end if;

    select configured.* into strict target
    from public.proactive_lead_bootstrap_targets configured
    where configured.scope_key = p_scope_key
      and configured.scope_version = p_scope_version
    for share;

    select candidate.* into strict intent
    from public.purchase_intents candidate
    where candidate.id = p_purchase_intent_id
    for update;

    if intent.lifecycle_state <> 'waiting_for_purchase'
       or not intent.provider_observed
       or not intent.whatsapp_contact_authorized
       or not intent.activation_authorized
       or intent.provisional
       or intent.normalized_phone !~ '^[1-9][0-9]{7,14}$'
       or intent.current_classification in ('identity_conflict', 'tracking_incomplete') then
        raise exception using errcode = '23514', message = 'proactive_bootstrap_intent_not_authorized';
    end if;

    select count(*)::integer into matching_submission_count
    from public.purchase_intent_submissions link
    join public.precheckout_submissions submission on submission.id = link.submission_id
    where link.purchase_intent_id = intent.id
      and submission.contract_version = '1.1.0'
      and submission.provider_observed
      and submission.activation_authorized
      and submission.canonical_payload #>> '{consent,whatsapp_contact}' = 'true'
      and submission.canonical_payload #>> '{consent,copy_version}'
          = 'johanna-precheckout-whatsapp-disclosure-v1'
      and submission.canonical_payload #>> '{identity,phone}' = intent.normalized_phone
      and not exists (
          select 1
          from public.precheckout_submission_conflicts conflict
          where conflict.existing_submission_id = submission.id
      );

    if matching_submission_count < 1 then
        raise exception using errcode = '23514', message = 'proactive_bootstrap_v1_1_evidence_missing';
    end if;

    if not exists (
        select 1
        from public.hotmart_purchase_intent_scopes mapping
        where mapping.active
          and mapping.tenant_ref = intent.tenant_ref
          and mapping.funnel_ref = intent.funnel_ref
          and lower(mapping.purchase_intent_product_ref) = lower(intent.product_ref)
          and mapping.offer_ref = intent.offer_ref
          and mapping.hotmart_product_id = scope.external_product_id
          and scope.offer_code = intent.offer_ref
          and scope.tenant_key = intent.tenant_ref
    ) then
        raise exception using errcode = '23514', message = 'proactive_bootstrap_scope_intent_mismatch';
    end if;

    select candidate.* into strict identity
    from public.channel_identities candidate
    where candidate.id = target.channel_identity_id
    for update;

    if identity.channel <> 'whatsapp'
       or identity.identity_status <> 'active'
       or identity.external_user_id is distinct from intent.normalized_phone
       or identity.account_id is distinct from 'chatwoot:' || scope.chatwoot_account_id::text
       or identity.metadata ->> 'inbox_id' is distinct from scope.chatwoot_inbox_id::text then
        raise exception using errcode = '23514', message = 'proactive_bootstrap_channel_identity_mismatch';
    end if;

    select owner.* into strict contact
    from public.contacts owner
    where owner.id = identity.contact_id
    for update;

    if contact.contact_permission in ('opted_out', 'blocked', 'restricted')
       or contact.lifecycle_status = 'do_not_contact' then
        raise exception using errcode = '23514', message = 'proactive_bootstrap_contact_blocked';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        'proactive_bootstrap_phone:' || intent.normalized_phone,
        0
    ));

    select count(distinct point.contact_id)::integer,
           coalesce(bool_or(point.contact_id = identity.contact_id), false)
    into phone_owner_count, phone_owned_by_identity
    from public.contact_points point
    where point.type = 'phone'
      and point.normalized_value = intent.normalized_phone;

    if phone_owner_count > 1 then
        raise exception using errcode = '23514', message = 'proactive_bootstrap_phone_ambiguous';
    end if;
    if phone_owner_count = 1 and not phone_owned_by_identity then
        raise exception using errcode = '23514', message = 'proactive_bootstrap_phone_owner_mismatch';
    end if;

    if phone_owner_count = 0 then
        insert into public.contact_points (
            contact_id, type, raw_value, normalized_value,
            source, verification_status, is_primary, verified_at, metadata
        ) values (
            identity.contact_id, 'phone', '+' || intent.normalized_phone,
            intent.normalized_phone, 'system', 'verified',
            not exists (
                select 1 from public.contact_points primary_point
                where primary_point.contact_id = identity.contact_id
                  and primary_point.type = 'phone'
                  and primary_point.is_primary
            ),
            clock_timestamp(),
            jsonb_build_object(
                'reason', 'authorized_precheckout_identity_bootstrap',
                'purchase_intent_id', intent.id
            )
        );
    end if;

    select result.* into strict cohort
    from public.set_lancemos_pilot_cohort_member(
        p_scope_key,
        p_scope_version,
        identity.contact_id,
        p_expected_generation,
        'active',
        btrim(p_actor),
        btrim(p_reason)
    ) result;

    if cohort.member_status <> 'active'
       or cohort.reason_code not in (
           'pilot_cohort_member_enrolled',
           'pilot_cohort_member_unchanged'
       ) then
        raise exception using errcode = '55000', message = 'proactive_bootstrap_cohort_enrollment_failed';
    end if;

    insert into public.proactive_lead_identity_bootstrap_commands (
        command_key, semantic_fingerprint, purchase_intent_id,
        scope_key, scope_version, channel_identity_id, contact_id,
        runtime_generation, outcome, actor, reason
    ) values (
        p_command_key, semantic_fingerprint, intent.id,
        p_scope_key, p_scope_version, identity.id, identity.contact_id,
        cohort.generation, 'proactive_bootstrap_completed',
        btrim(p_actor), btrim(p_reason)
    ) returning * into strict command;

    return query select command.outcome, command.command_key, command.contact_id,
        command.channel_identity_id, command.runtime_generation, true;
end;
$function$;

revoke all on table public.proactive_lead_identity_bootstrap_commands from public;
revoke all on table public.proactive_lead_bootstrap_targets from public;
revoke all on function public.bootstrap_proactive_lead_identity(
    text, uuid, text, integer, bigint, text, text
) from public;

revoke all on function public.bootstrap_proactive_lead_identity(
    text, uuid, text, integer, bigint, text, text
) from anon, authenticated;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.bootstrap_proactive_lead_identity(
            text, uuid, text, integer, bigint, text, text
        ) to service_role;
    end if;
end;
$roles$;

commit;
