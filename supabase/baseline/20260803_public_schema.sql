-- Snapshot declarativo del esquema public observado el 2026-08-03.
-- Fuente: catálogos PostgreSQL consultados en modo lectura.
-- Este archivo recrea el punto de partida en una base vacía; no debe aplicarse
-- como migración sobre la base existente.

begin;

create extension if not exists pgcrypto;

create table public.webhook_events (
    id uuid primary key default gen_random_uuid(),
    source text not null check (source = any (array['hotmart', 'instagram', 'zernio', 'system', 'simulator'])),
    external_event_id text not null,
    event_type text not null,
    payload jsonb not null default '{}'::jsonb,
    processing_status text not null default 'received' check (processing_status = any (array['received', 'processing', 'processed', 'ignored', 'failed'])),
    processing_error text,
    received_at timestamptz not null default now(),
    processed_at timestamptz,
    created_at timestamptz not null default now(),
    unique (source, external_event_id)
);

create table public.contacts (
    id uuid primary key default gen_random_uuid(),
    full_name text,
    email text,
    phone text,
    country_iso text,
    locale text,
    timezone text,
    timezone_confidence text not null default 'unknown' check (timezone_confidence = any (array['explicit', 'platform', 'inferred', 'default', 'unknown'])),
    contact_permission text not null default 'unknown' check (contact_permission = any (array['unknown', 'allowed', 'opted_out', 'blocked', 'restricted'])),
    lifecycle_status text not null default 'lead' check (lifecycle_status = any (array['lead', 'qualified_lead', 'opportunity', 'customer', 'nurture', 'unqualified', 'closed_lost', 'do_not_contact'])),
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.contact_points (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null references public.contacts(id) on delete cascade,
    type text not null check (type = any (array['email', 'phone', 'instagram_username', 'other'])),
    raw_value text not null,
    normalized_value text not null check (length(btrim(normalized_value)) > 0),
    source text not null check (source = any (array['hotmart', 'instagram', 'zernio', 'manual', 'crm', 'simulator', 'system'])),
    source_event_id uuid references public.webhook_events(id) on delete set null,
    verification_status text not null default 'unverified' check (verification_status = any (array['unverified', 'valid', 'invalid', 'verified', 'blocked', 'unknown'])),
    is_primary boolean not null default false,
    verified_at timestamptz,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (verification_status = 'verified' and verified_at is not null or verification_status <> 'verified')
);

create table public.channel_identities (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null references public.contacts(id) on delete cascade,
    channel text not null check (channel = any (array['instagram', 'whatsapp', 'email', 'sms', 'other'])),
    account_id text not null,
    external_user_id text,
    external_conversation_id text,
    username text,
    identity_status text not null default 'active' check (identity_status = any (array['active', 'unreachable', 'blocked', 'unknown'])),
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (external_user_id is not null or external_conversation_id is not null)
);

create table public.conversations (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null references public.contacts(id) on delete cascade,
    channel_identity_id uuid not null references public.channel_identities(id) on delete restrict,
    status text not null default 'active' check (status = any (array['active', 'awaiting_agent', 'awaiting_contact', 'snoozed', 'paused_human', 'completed', 'closed', 'blocked'])),
    automation_status text not null default 'enabled' check (automation_status = any (array['enabled', 'draft_only', 'paused', 'disabled', 'restricted', 'error'])),
    human_takeover boolean not null default false,
    human_owner_id text,
    version bigint not null default 1 check (version > 0),
    last_message_id uuid,
    last_message_direction text check (last_message_direction = any (array['inbound', 'outbound'])),
    last_inbound_at timestamptz,
    last_outbound_at timestamptz,
    paused_until timestamptz,
    closed_at timestamptz,
    commercial_context jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.messages (
    id uuid primary key default gen_random_uuid(),
    conversation_id uuid not null references public.conversations(id) on delete cascade,
    external_message_id text,
    direction text not null check (direction = any (array['inbound', 'outbound'])),
    actor_type text not null check (actor_type = any (array['prospect', 'ai_agent', 'human_agent', 'system'])),
    message_type text not null default 'unknown' check (message_type = any (array['question', 'answer', 'information', 'acknowledgement', 'followup', 'objection', 'proposal', 'booking_link', 'booking_confirmation', 'commitment', 'closing', 'opt_out', 'system_notice', 'unknown'])),
    content text not null,
    delivery_status text not null default 'pending' check (delivery_status = any (array['pending', 'accepted', 'sent', 'delivered', 'read', 'failed', 'unknown'])),
    expects_reply boolean,
    reply_expectation_type text check (reply_expectation_type = any (array['answer', 'confirmation', 'decision', 'document', 'booking', 'payment', 'review', 'freeform'])),
    in_reply_to_message_id uuid references public.messages(id) on delete set null,
    is_followup boolean not null default false,
    followup_step integer check (followup_step is null or followup_step > 0),
    semantic_metadata jsonb not null default '{}'::jsonb,
    occurred_at timestamptz not null default now(),
    delivered_at timestamptz,
    created_at timestamptz not null default now(),
    check (expects_reply is null or direction = 'outbound')
);

alter table public.conversations
    add constraint conversations_last_message_fk
    foreign key (last_message_id) references public.messages(id) on delete set null;

create table public.recovery_cases (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null references public.contacts(id) on delete restrict,
    conversation_id uuid references public.conversations(id) on delete set null,
    abandonment_event_id uuid not null unique references public.webhook_events(id) on delete restrict,
    purchase_event_id uuid unique references public.webhook_events(id) on delete restrict,
    source text not null default 'hotmart' check (source = any (array['hotmart', 'simulator'])),
    external_product_id text not null,
    product_name text not null,
    offer_code text,
    checkout_reference text,
    status text not null default 'grace_period' check (status = any (array['grace_period', 'active', 'paused', 'won', 'sequence_exhausted', 'lost', 'cancelled', 'unreachable', 'error'])),
    version bigint not null default 1 check (version > 0),
    grace_expires_at timestamptz not null,
    next_contact_at timestamptz,
    next_contact_reason text,
    current_goal text,
    lead_stage text not null default 'new' check (lead_stage = any (array['new', 'discovery', 'qualifying', 'qualified', 'solution_presented', 'proposal_pending', 'objection_handling', 'booking_pending', 'booked', 'nurture', 'won', 'lost', 'unqualified'])),
    context jsonb not null default '{}'::jsonb,
    won_at timestamptz,
    closed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    selected_channel_identity_id uuid references public.channel_identities(id) on delete set null,
    identity_resolution_status text not null default 'pending' check (identity_resolution_status = any (array['pending', 'resolving', 'resolved', 'ambiguous', 'not_found', 'retryable_failed', 'permanent_failed', 'restricted'])),
    identity_resolution_last_attempt_at timestamptz,
    identity_resolution_attempt_count integer not null default 0 check (identity_resolution_attempt_count >= 0),
    identity_resolution_error text,
    check (status = 'won' and won_at is not null or status <> 'won'),
    check (purchase_event_id is null or status = 'won'),
    check (identity_resolution_status = 'resolved' and selected_channel_identity_id is not null or identity_resolution_status <> 'resolved')
);

create table public.identity_resolution_attempts (
    id uuid primary key default gen_random_uuid(),
    recovery_case_id uuid not null references public.recovery_cases(id) on delete cascade,
    channel text not null check (channel = any (array['instagram', 'whatsapp', 'email', 'sms', 'other'])),
    strategy text not null check (strategy = any (array['existing_identity_by_email', 'existing_identity_by_phone', 'existing_conversation', 'instagram_username_from_checkout', 'whatsapp_phone_normalization', 'manual_mapping', 'crm_lookup', 'other'])),
    status text not null check (status = any (array['matched', 'not_found', 'ambiguous', 'invalid_input', 'restricted', 'retryable_failed', 'permanent_failed'])),
    matched_channel_identity_id uuid references public.channel_identities(id) on delete set null,
    confidence numeric check (confidence is null or confidence >= 0 and confidence <= 1),
    input_fingerprint text,
    evidence jsonb not null default '{}'::jsonb,
    error_code text,
    error_message text,
    attempted_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    check (status = 'matched' and matched_channel_identity_id is not null or status <> 'matched')
);

create table public.followup_sequences (
    id uuid primary key default gen_random_uuid(),
    recovery_case_id uuid not null references public.recovery_cases(id) on delete cascade,
    conversation_id uuid references public.conversations(id) on delete set null,
    status text not null default 'active' check (status = any (array['active', 'paused', 'completed', 'cancelled', 'failed'])),
    reason text not null check (reason = any (array['cart_abandonment', 'no_reply', 'contact_requested', 'prospect_commitment', 'agent_commitment', 'proposal_pending', 'booking_pending', 'payment_pending', 'nurture', 'manual', 'recovery'])),
    policy_key text not null,
    policy_version integer not null default 1 check (policy_version > 0),
    trigger_message_id uuid references public.messages(id) on delete set null,
    current_step integer not null default 0 check (current_step >= 0),
    max_attempts integer not null check (max_attempts > 0),
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    cancelled_at timestamptz,
    cancel_reason text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.scheduled_actions (
    id uuid primary key default gen_random_uuid(),
    recovery_case_id uuid not null references public.recovery_cases(id) on delete cascade,
    followup_sequence_id uuid references public.followup_sequences(id) on delete cascade,
    conversation_id uuid references public.conversations(id) on delete set null,
    action_type text not null check (action_type = any (array['send_first_touch', 'send_followup', 'contact_on_date', 'reconcile'])),
    status text not null default 'pending' check (status = any (array['pending', 'scheduled', 'claimed', 'validating', 'generating', 'sending', 'sent', 'skipped', 'cancelled', 'retryable_failed', 'permanent_failed', 'expired'])),
    due_at timestamptz not null,
    not_before timestamptz,
    expires_at timestamptz,
    expected_case_version bigint not null check (expected_case_version > 0),
    expected_conversation_version bigint check (expected_conversation_version is null or expected_conversation_version > 0),
    expected_last_outbound_message_id uuid references public.messages(id) on delete set null,
    cron_job_id text,
    idempotency_key text not null unique,
    execution_attempt_count integer not null default 0 check (execution_attempt_count >= 0),
    max_execution_retries integer not null default 3 check (max_execution_retries >= 0),
    claimed_at timestamptz,
    executed_at timestamptz,
    skip_reason text,
    error_code text,
    error_message text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (expires_at is null or expires_at > due_at)
);

create table public.conversation_events (
    id uuid primary key default gen_random_uuid(),
    conversation_id uuid references public.conversations(id) on delete cascade,
    recovery_case_id uuid references public.recovery_cases(id) on delete cascade,
    event_type text not null,
    actor_type text not null default 'system' check (actor_type = any (array['prospect', 'ai_agent', 'human_agent', 'system', 'integration'])),
    related_message_id uuid references public.messages(id) on delete set null,
    related_action_id uuid references public.scheduled_actions(id) on delete set null,
    data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    check (conversation_id is not null or recovery_case_id is not null)
);

create unique index contacts_email_unique_idx on public.contacts (lower(email)) where email is not null;
create index contacts_phone_idx on public.contacts (phone) where phone is not null;
create unique index contact_points_contact_value_unique_idx on public.contact_points (contact_id, type, normalized_value);
create index contact_points_lookup_idx on public.contact_points (type, normalized_value);
create unique index contact_points_one_primary_per_type_idx on public.contact_points (contact_id, type) where is_primary;
create index contact_points_source_event_idx on public.contact_points (source_event_id) where source_event_id is not null;
create unique index channel_identities_conversation_unique_idx on public.channel_identities (channel, account_id, external_conversation_id) where external_conversation_id is not null;
create unique index channel_identities_user_unique_idx on public.channel_identities (channel, account_id, external_user_id) where external_user_id is not null;
create index conversations_contact_idx on public.conversations (contact_id);
create index conversations_status_idx on public.conversations (status, automation_status);
create index messages_conversation_time_idx on public.messages (conversation_id, occurred_at desc);
create unique index messages_external_unique_idx on public.messages (conversation_id, external_message_id) where external_message_id is not null;
create index recovery_cases_contact_idx on public.recovery_cases (contact_id, created_at desc);
create index recovery_cases_due_idx on public.recovery_cases (status, grace_expires_at) where status = any (array['grace_period', 'active', 'paused']);
create index recovery_cases_identity_resolution_idx on public.recovery_cases (identity_resolution_status, grace_expires_at) where status = any (array['grace_period', 'active', 'paused']);
create index recovery_cases_product_offer_idx on public.recovery_cases (source, external_product_id, offer_code);
create index recovery_cases_selected_identity_idx on public.recovery_cases (selected_channel_identity_id) where selected_channel_identity_id is not null;
create index identity_resolution_attempts_case_time_idx on public.identity_resolution_attempts (recovery_case_id, attempted_at desc);
create index identity_resolution_attempts_status_idx on public.identity_resolution_attempts (status, attempted_at desc);
create unique index followup_sequences_one_active_per_case_idx on public.followup_sequences (recovery_case_id) where status = 'active';
create index scheduled_actions_cron_job_idx on public.scheduled_actions (cron_job_id) where cron_job_id is not null;
create index scheduled_actions_due_idx on public.scheduled_actions (due_at) where status = any (array['pending', 'scheduled', 'retryable_failed']);
create unique index scheduled_actions_one_live_per_case_idx on public.scheduled_actions (recovery_case_id) where status = any (array['pending', 'scheduled', 'claimed', 'validating', 'generating', 'sending', 'retryable_failed']);
create index conversation_events_case_time_idx on public.conversation_events (recovery_case_id, created_at desc) where recovery_case_id is not null;
create index conversation_events_conversation_time_idx on public.conversation_events (conversation_id, created_at desc) where conversation_id is not null;
create index webhook_events_type_received_idx on public.webhook_events (source, event_type, received_at desc);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create or replace function public.validate_recovery_case_channel_identity()
returns trigger
language plpgsql
as $$
declare
    identity_contact_id uuid;
begin
    if new.selected_channel_identity_id is null then
        return new;
    end if;

    select contact_id into identity_contact_id
    from public.channel_identities
    where id = new.selected_channel_identity_id;

    if identity_contact_id is distinct from new.contact_id then
        raise exception using
            errcode = '23514',
            message = 'selected channel identity must belong to the recovery case contact';
    end if;

    return new;
end;
$$;

create or replace function public.validate_resolution_attempt_identity()
returns trigger
language plpgsql
as $$
declare
    case_contact_id uuid;
    identity_contact_id uuid;
    identity_channel text;
begin
    if new.matched_channel_identity_id is null then
        return new;
    end if;

    select contact_id into case_contact_id
    from public.recovery_cases
    where id = new.recovery_case_id;

    select contact_id, channel into identity_contact_id, identity_channel
    from public.channel_identities
    where id = new.matched_channel_identity_id;

    if identity_contact_id is distinct from case_contact_id then
        raise exception using
            errcode = '23514',
            message = 'matched channel identity must belong to the recovery case contact';
    end if;

    if identity_channel is distinct from new.channel then
        raise exception using
            errcode = '23514',
            message = 'matched channel identity channel must match the attempted channel';
    end if;

    return new;
end;
$$;

create trigger channel_identities_set_updated_at before update on public.channel_identities for each row execute function public.set_updated_at();
create trigger contact_points_set_updated_at before update on public.contact_points for each row execute function public.set_updated_at();
create trigger contacts_set_updated_at before update on public.contacts for each row execute function public.set_updated_at();
create trigger conversations_set_updated_at before update on public.conversations for each row execute function public.set_updated_at();
create trigger followup_sequences_set_updated_at before update on public.followup_sequences for each row execute function public.set_updated_at();
create trigger identity_resolution_attempts_validate_identity before insert or update of recovery_case_id, channel, matched_channel_identity_id on public.identity_resolution_attempts for each row execute function public.validate_resolution_attempt_identity();
create trigger recovery_cases_set_updated_at before update on public.recovery_cases for each row execute function public.set_updated_at();
create trigger recovery_cases_validate_channel_identity before insert or update of contact_id, selected_channel_identity_id on public.recovery_cases for each row execute function public.validate_recovery_case_channel_identity();
create trigger scheduled_actions_set_updated_at before update on public.scheduled_actions for each row execute function public.set_updated_at();

alter table public.webhook_events enable row level security;
alter table public.contacts enable row level security;
alter table public.contact_points enable row level security;
alter table public.channel_identities enable row level security;
alter table public.conversations enable row level security;
alter table public.messages enable row level security;
alter table public.recovery_cases enable row level security;
alter table public.identity_resolution_attempts enable row level security;
alter table public.followup_sequences enable row level security;
alter table public.scheduled_actions enable row level security;
alter table public.conversation_events enable row level security;

commit;
