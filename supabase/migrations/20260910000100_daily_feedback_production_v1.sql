-- Shared, fail-closed production workflow for daily owner feedback.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

create table public.daily_feedback_reviewer_bindings (
  id uuid primary key default gen_random_uuid(),
  tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
  scope_ref text not null check (scope_ref ~ '^[a-z0-9][a-z0-9_-]{0,127}$'),
  reviewer_ref text not null check (reviewer_ref ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
  oidc_issuer text not null check (oidc_issuer = 'https://slack.com'),
  oidc_subject text not null check (oidc_subject ~ '^https://slack\.com/user_id/[UW][A-Z0-9]{8,}$'),
  slack_team_id text not null check (slack_team_id ~ '^T[A-Z0-9]{8,}$'),
  slack_user_id text not null check (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'),
  binding_generation bigint not null default 1 check (binding_generation > 0),
  active boolean not null default true,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  unique (tenant_ref, scope_ref, reviewer_ref),
  unique (tenant_ref, scope_ref, oidc_issuer, oidc_subject),
  unique (tenant_ref, scope_ref, slack_team_id, slack_user_id)
);

create table public.daily_feedback_schedules (
  id uuid primary key default gen_random_uuid(),
  tenant_ref text not null,
  scope_ref text not null,
  chatwoot_account_id bigint not null check (chatwoot_account_id > 0),
  chatwoot_inbox_id bigint not null check (chatwoot_inbox_id > 0),
  chatwoot_agent_bot_id bigint not null check (chatwoot_agent_bot_id > 0),
  timezone_name text not null,
  cutoff_local time not null,
  retention_hours integer not null check (retention_hours between 1 and 168),
  sanitizer_version text not null,
  selection_version text not null,
  renderer_version text not null,
  reviewer_binding_id uuid not null references public.daily_feedback_reviewer_bindings(id) on delete restrict,
  reviewer_binding_generation bigint not null check (reviewer_binding_generation > 0),
  enabled boolean not null default false,
  schedule_generation bigint not null default 1 check (schedule_generation > 0),
  lease_owner text,
  lease_generation bigint not null default 0 check (lease_generation >= 0),
  lease_expires_at timestamptz,
  pending_local_date date,
  pending_window_start timestamptz,
  pending_window_end timestamptz,
  next_attempt_at timestamptz not null default clock_timestamp(),
  last_completed_local_date date,
  last_completed_window_end timestamptz,
  last_error_code text,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  unique (tenant_ref, scope_ref),
  check ((lease_owner is null) = (lease_expires_at is null)),
  check ((pending_local_date is null) = (pending_window_start is null)),
  check ((pending_local_date is null) = (pending_window_end is null)),
  check (pending_window_end is null or pending_window_start < pending_window_end)
);

create table public.daily_feedback_batches (
  id uuid primary key default gen_random_uuid(),
  public_ref uuid not null unique default gen_random_uuid(),
  schedule_id uuid not null references public.daily_feedback_schedules(id) on delete restrict,
  tenant_ref text not null,
  scope_ref text not null,
  local_date date not null,
  window_start timestamptz not null,
  window_end timestamptz not null,
  sanitizer_version text not null,
  selection_version text not null,
  renderer_version text not null,
  release_lineage text not null,
  package_fingerprint text not null check (package_fingerprint ~ '^[a-f0-9]{64}$'),
  item_count integer not null check (item_count >= 0),
  reviewer_binding_id uuid not null references public.daily_feedback_reviewer_bindings(id) on delete restrict,
  reviewer_binding_generation bigint not null check (reviewer_binding_generation > 0),
  state text not null default 'committed' check (state in ('committed','completed','purged')),
  notification_state text not null default 'pending' check (notification_state in ('pending','claimed','request_started','delivery_unknown','admitted','retry')),
  notification_lease_owner text,
  notification_lease_generation bigint not null default 0 check (notification_lease_generation >= 0),
  notification_lease_expires_at timestamptz,
  notification_attempts integer not null default 0 check (notification_attempts >= 0),
  notification_next_attempt_at timestamptz not null default clock_timestamp(),
  notification_last_error text,
  notification_admitted_at timestamptz,
  completed_at timestamptz,
  retention_expires_at timestamptz not null,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  unique (tenant_ref, scope_ref, local_date),
  check (window_start < window_end),
  check ((notification_lease_owner is null) = (notification_lease_expires_at is null))
);

create table public.daily_feedback_items (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.daily_feedback_batches(id) on delete restrict,
  item_index integer not null check (item_index > 0),
  conversation_ref text not null check (conversation_ref ~ '^conv_[a-f0-9]{20}$'),
  display_label text not null check (char_length(display_label) between 1 and 80),
  apparent_objective text not null check (char_length(apparent_objective) between 1 and 300),
  observed_outcome text not null check (char_length(observed_outcome) between 1 and 300),
  release_id text not null check (char_length(release_id) between 1 and 128),
  release_version integer not null check (release_version >= 0),
  messages jsonb not null,
  created_at timestamptz not null default clock_timestamp(),
  unique (batch_id, item_index),
  unique (batch_id, conversation_ref),
  check (jsonb_typeof(messages) = 'array' and jsonb_array_length(messages) between 2 and 1000)
);

create table public.daily_feedback_decisions (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.daily_feedback_batches(id) on delete restrict,
  item_id uuid not null references public.daily_feedback_items(id) on delete restrict,
  reviewer_binding_id uuid not null references public.daily_feedback_reviewer_bindings(id) on delete restrict,
  command_id uuid not null unique,
  decision text not null check (decision in ('correct','correct_with_feedback','skip')),
  verbatim_feedback text,
  created_at timestamptz not null default clock_timestamp(),
  unique (batch_id, item_id),
  check (
    (decision = 'correct_with_feedback' and verbatim_feedback is not null and char_length(btrim(verbatim_feedback)) between 1 and 4000)
    or (decision in ('correct','skip') and verbatim_feedback is null)
  )
);

create table public.daily_feedback_oidc_states (
  state_hash text primary key check (state_hash ~ '^[a-f0-9]{64}$'),
  batch_id uuid not null references public.daily_feedback_batches(id) on delete cascade,
  return_path text not null check (return_path ~ '^/daily-feedback/review/[0-9a-f-]{36}$'),
  expires_at timestamptz not null,
  consumed_at timestamptz,
  created_at timestamptz not null default clock_timestamp(),
  check (expires_at > created_at and expires_at <= created_at + interval '10 minutes')
);

create table public.daily_feedback_sessions (
  session_hash text primary key check (session_hash ~ '^[a-f0-9]{64}$'),
  batch_id uuid not null references public.daily_feedback_batches(id) on delete restrict,
  reviewer_binding_id uuid not null references public.daily_feedback_reviewer_bindings(id) on delete restrict,
  binding_generation bigint not null check (binding_generation > 0),
  oidc_issuer text not null,
  oidc_subject text not null,
  slack_team_id text not null,
  slack_user_id text not null,
  issued_at timestamptz not null default clock_timestamp(),
  expires_at timestamptz not null,
  revoked_at timestamptz,
  last_seen_at timestamptz not null default clock_timestamp(),
  check (expires_at > issued_at and expires_at <= issued_at + interval '8 hours')
);

create table public.daily_feedback_workflow_commands (
  command_id uuid primary key,
  operation text not null,
  semantic_fingerprint text not null check (semantic_fingerprint ~ '^[a-f0-9]{64}$'),
  server_payload_hash text not null check (server_payload_hash ~ '^[a-f0-9]{64}$'),
  result jsonb,
  created_at timestamptz not null default clock_timestamp(),
  completed_at timestamptz
);

create table public.daily_feedback_purge_tombstones (
  batch_id uuid primary key,
  tenant_ref text not null,
  scope_ref text not null,
  local_date date not null,
  package_fingerprint text not null check (package_fingerprint ~ '^[a-f0-9]{64}$'),
  purged_item_count integer not null check (purged_item_count >= 0),
  purged_decision_count integer not null check (purged_decision_count >= 0),
  deletion_owner text not null,
  purged_at timestamptz not null default clock_timestamp()
);

create index daily_feedback_schedule_due_idx on public.daily_feedback_schedules (next_attempt_at) where enabled;
create index daily_feedback_batch_notify_idx on public.daily_feedback_batches (notification_next_attempt_at) where notification_state in ('pending','retry','claimed','request_started','delivery_unknown');
create index daily_feedback_batch_retention_idx on public.daily_feedback_batches (retention_expires_at) where state <> 'purged';
create index daily_feedback_decision_batch_idx on public.daily_feedback_decisions (batch_id, item_id);
create index daily_feedback_session_binding_idx on public.daily_feedback_sessions (reviewer_binding_id, expires_at);
create index daily_feedback_session_batch_idx on public.daily_feedback_sessions (batch_id, expires_at);

create function public.daily_feedback_messages_valid(p_messages jsonb)
returns boolean
language sql
immutable
set search_path = ''
as $$
  select jsonb_typeof(p_messages) = 'array'
     and jsonb_array_length(p_messages) between 2 and 1000
     and not exists (
       select 1
       from jsonb_array_elements(p_messages) m
       where jsonb_typeof(m) <> 'object'
          or (select array_agg(k order by k) from jsonb_object_keys(m) k) is distinct from array['actor','occurred_at','text']::text[]
          or m->>'actor' not in ('prospect','agent')
          or coalesce(char_length(m->>'text'), 0) not between 1 and 4000
          or (m->>'occurred_at') !~ '^20[0-9]{2}-[0-9]{2}-[0-9]{2}T.*Z$'
          or (m->>'text') ~* 'https?://|www\.|bearer[[:space:]]+[a-z0-9._~+/=-]{12,}|[[:alnum:]._%+-]+@[[:alnum:].-]+\.[[:alpha:]]{2,}'
          or (m->>'text') ~ '[[:digit:]][[:space:]().+-]*[[:digit:]][[:space:]().+-]*[[:digit:]][[:space:]().+-]*[[:digit:]][[:space:]().+-]*[[:digit:]][[:space:]().+-]*[[:digit:]][[:space:]().+-]*[[:digit:]][[:space:]().+-]*[[:digit:]]'
          or (m->>'text') ~ '[[:cntrl:]]'
     );
$$;

alter table public.daily_feedback_items
  add constraint daily_feedback_items_messages_valid
  check (public.daily_feedback_messages_valid(messages));

create function public.daily_feedback_immutable_content_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if current_setting('app.daily_feedback_purge', true) = 'on' and tg_op = 'DELETE' then
    return old;
  end if;
  raise exception 'daily_feedback_content_immutable';
end;
$$;

create trigger daily_feedback_items_immutable
before update or delete on public.daily_feedback_items
for each row execute function public.daily_feedback_immutable_content_guard();

create trigger daily_feedback_decisions_immutable
before update or delete on public.daily_feedback_decisions
for each row execute function public.daily_feedback_immutable_content_guard();

create function public.daily_feedback_server_payload_hash(p_payload jsonb)
returns text
language sql
immutable
strict
set search_path = ''
as $$
  select pg_catalog.encode(
    pg_catalog.sha256(pg_catalog.convert_to(p_payload::text, 'UTF8')),
    'hex'
  )
$$;

create function public.daily_feedback_begin_command(
  p_command_id uuid,
  p_operation text,
  p_semantic_fingerprint text,
  p_server_payload_hash text
) returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row public.daily_feedback_workflow_commands%rowtype;
begin
  if p_operation is null or p_operation !~ '^[a-z0-9_]{3,80}$'
     or p_semantic_fingerprint !~ '^[a-f0-9]{64}$'
     or p_server_payload_hash !~ '^[a-f0-9]{64}$' then
    raise exception 'invalid_workflow_command';
  end if;
  insert into public.daily_feedback_workflow_commands(
    command_id, operation, semantic_fingerprint, server_payload_hash
  ) values (p_command_id, p_operation, p_semantic_fingerprint, p_server_payload_hash)
  on conflict (command_id) do nothing;
  select * into v_row from public.daily_feedback_workflow_commands where command_id = p_command_id for update;
  if v_row.operation <> p_operation
     or v_row.semantic_fingerprint <> p_semantic_fingerprint
     or v_row.server_payload_hash <> p_server_payload_hash then
    raise exception 'idempotency_conflict';
  end if;
  return v_row.result;
end;
$$;

create function public.daily_feedback_finish_command(p_command_id uuid, p_result jsonb)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_result is null or jsonb_typeof(p_result) <> 'object' then
    raise exception 'invalid_workflow_result';
  end if;
  update public.daily_feedback_workflow_commands
  set result = p_result, completed_at = clock_timestamp()
  where command_id = p_command_id and result is null;
  return p_result;
end;
$$;

create function public.configure_daily_feedback_scope_v1(
  p_command_id uuid,
  p_semantic_fingerprint text,
  p_tenant_ref text,
  p_scope_ref text,
  p_reviewer_ref text,
  p_oidc_issuer text,
  p_oidc_subject text,
  p_slack_team_id text,
  p_slack_user_id text,
  p_chatwoot_account_id bigint,
  p_chatwoot_inbox_id bigint,
  p_chatwoot_agent_bot_id bigint,
  p_timezone_name text,
  p_cutoff_local time,
  p_retention_hours integer,
  p_sanitizer_version text,
  p_selection_version text,
  p_renderer_version text,
  p_enabled boolean
) returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_prior jsonb;
  v_binding public.daily_feedback_reviewer_bindings%rowtype;
  v_schedule public.daily_feedback_schedules%rowtype;
  v_result jsonb;
begin
  v_prior := public.daily_feedback_begin_command(
    p_command_id,
    'configure_scope',
    p_semantic_fingerprint,
    public.daily_feedback_server_payload_hash(jsonb_build_object(
      'tenant_ref',p_tenant_ref,'scope_ref',p_scope_ref,'reviewer_ref',p_reviewer_ref,
      'oidc_issuer',p_oidc_issuer,'oidc_subject',p_oidc_subject,
      'slack_team_id',p_slack_team_id,'slack_user_id',p_slack_user_id,
      'chatwoot_account_id',p_chatwoot_account_id,'chatwoot_inbox_id',p_chatwoot_inbox_id,
      'chatwoot_agent_bot_id',p_chatwoot_agent_bot_id,'timezone_name',p_timezone_name,
      'cutoff_local',p_cutoff_local,'retention_hours',p_retention_hours,
      'sanitizer_version',p_sanitizer_version,'selection_version',p_selection_version,
      'renderer_version',p_renderer_version,'enabled',p_enabled
    ))
  );
  if v_prior is not null then return v_prior || jsonb_build_object('status','replayed'); end if;
  if not exists (select 1 from pg_catalog.pg_timezone_names where name = p_timezone_name)
     or p_retention_hours not between 1 and 168
     or p_oidc_issuer <> 'https://slack.com'
     or p_oidc_subject <> 'https://slack.com/user_id/' || p_slack_user_id
     or least(p_chatwoot_account_id,p_chatwoot_inbox_id,p_chatwoot_agent_bot_id) < 1 then
    raise exception 'invalid_daily_feedback_scope';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(p_tenant_ref || pg_catalog.chr(31) || p_scope_ref, 0)
  );
  insert into public.daily_feedback_reviewer_bindings(
    tenant_ref, scope_ref, reviewer_ref, oidc_issuer, oidc_subject,
    slack_team_id, slack_user_id, active
  ) values (
    p_tenant_ref, p_scope_ref, p_reviewer_ref, p_oidc_issuer, p_oidc_subject,
    p_slack_team_id, p_slack_user_id, true
  )
  on conflict (tenant_ref, scope_ref, reviewer_ref) do update
  set oidc_issuer = excluded.oidc_issuer,
      oidc_subject = excluded.oidc_subject,
      slack_team_id = excluded.slack_team_id,
      slack_user_id = excluded.slack_user_id,
      active = true,
      binding_generation = public.daily_feedback_reviewer_bindings.binding_generation +
        (row(
          public.daily_feedback_reviewer_bindings.oidc_issuer,
          public.daily_feedback_reviewer_bindings.oidc_subject,
          public.daily_feedback_reviewer_bindings.slack_team_id,
          public.daily_feedback_reviewer_bindings.slack_user_id,
          public.daily_feedback_reviewer_bindings.active
        ) is distinct from row(
          excluded.oidc_issuer, excluded.oidc_subject,
          excluded.slack_team_id, excluded.slack_user_id, true
        ))::integer,
      updated_at = case when row(
          public.daily_feedback_reviewer_bindings.oidc_issuer,
          public.daily_feedback_reviewer_bindings.oidc_subject,
          public.daily_feedback_reviewer_bindings.slack_team_id,
          public.daily_feedback_reviewer_bindings.slack_user_id,
          public.daily_feedback_reviewer_bindings.active
        ) is distinct from row(
          excluded.oidc_issuer, excluded.oidc_subject,
          excluded.slack_team_id, excluded.slack_user_id, true
        ) then clock_timestamp()
        else public.daily_feedback_reviewer_bindings.updated_at end
  returning * into v_binding;

  update public.daily_feedback_reviewer_bindings
  set active = false,
      binding_generation = binding_generation + 1,
      updated_at = clock_timestamp()
  where tenant_ref = p_tenant_ref
    and scope_ref = p_scope_ref
    and id <> v_binding.id
    and active;

  insert into public.daily_feedback_schedules(
    tenant_ref, scope_ref, chatwoot_account_id, chatwoot_inbox_id, chatwoot_agent_bot_id,
    timezone_name, cutoff_local, retention_hours,
    sanitizer_version, selection_version, renderer_version,
    reviewer_binding_id, reviewer_binding_generation, enabled
  ) values (
    p_tenant_ref, p_scope_ref, p_chatwoot_account_id, p_chatwoot_inbox_id,
    p_chatwoot_agent_bot_id, p_timezone_name, p_cutoff_local, p_retention_hours,
    p_sanitizer_version, p_selection_version, p_renderer_version,
    v_binding.id, v_binding.binding_generation, p_enabled
  )
  on conflict (tenant_ref, scope_ref) do update
  set chatwoot_account_id = excluded.chatwoot_account_id,
      chatwoot_inbox_id = excluded.chatwoot_inbox_id,
      chatwoot_agent_bot_id = excluded.chatwoot_agent_bot_id,
      timezone_name = excluded.timezone_name,
      cutoff_local = excluded.cutoff_local,
      retention_hours = excluded.retention_hours,
      sanitizer_version = excluded.sanitizer_version,
      selection_version = excluded.selection_version,
      renderer_version = excluded.renderer_version,
      reviewer_binding_id = excluded.reviewer_binding_id,
      reviewer_binding_generation = excluded.reviewer_binding_generation,
      enabled = excluded.enabled,
      schedule_generation = public.daily_feedback_schedules.schedule_generation +
        (row(
          public.daily_feedback_schedules.chatwoot_account_id,
          public.daily_feedback_schedules.chatwoot_inbox_id,
          public.daily_feedback_schedules.chatwoot_agent_bot_id,
          public.daily_feedback_schedules.timezone_name,
          public.daily_feedback_schedules.cutoff_local,
          public.daily_feedback_schedules.retention_hours,
          public.daily_feedback_schedules.sanitizer_version,
          public.daily_feedback_schedules.selection_version,
          public.daily_feedback_schedules.renderer_version,
          public.daily_feedback_schedules.reviewer_binding_id,
          public.daily_feedback_schedules.reviewer_binding_generation,
          public.daily_feedback_schedules.enabled
        ) is distinct from row(
          excluded.chatwoot_account_id, excluded.chatwoot_inbox_id,
          excluded.chatwoot_agent_bot_id, excluded.timezone_name,
          excluded.cutoff_local, excluded.retention_hours,
          excluded.sanitizer_version, excluded.selection_version,
          excluded.renderer_version, excluded.reviewer_binding_id,
          excluded.reviewer_binding_generation, excluded.enabled
        ))::integer,
      lease_owner = case when row(
          public.daily_feedback_schedules.chatwoot_account_id,
          public.daily_feedback_schedules.chatwoot_inbox_id,
          public.daily_feedback_schedules.chatwoot_agent_bot_id,
          public.daily_feedback_schedules.timezone_name,
          public.daily_feedback_schedules.cutoff_local,
          public.daily_feedback_schedules.retention_hours,
          public.daily_feedback_schedules.sanitizer_version,
          public.daily_feedback_schedules.selection_version,
          public.daily_feedback_schedules.renderer_version,
          public.daily_feedback_schedules.reviewer_binding_id,
          public.daily_feedback_schedules.reviewer_binding_generation,
          public.daily_feedback_schedules.enabled
        ) is distinct from row(
          excluded.chatwoot_account_id, excluded.chatwoot_inbox_id,
          excluded.chatwoot_agent_bot_id, excluded.timezone_name,
          excluded.cutoff_local, excluded.retention_hours,
          excluded.sanitizer_version, excluded.selection_version,
          excluded.renderer_version, excluded.reviewer_binding_id,
          excluded.reviewer_binding_generation, excluded.enabled
        ) then null else public.daily_feedback_schedules.lease_owner end,
      lease_expires_at = case when row(
          public.daily_feedback_schedules.chatwoot_account_id,
          public.daily_feedback_schedules.chatwoot_inbox_id,
          public.daily_feedback_schedules.chatwoot_agent_bot_id,
          public.daily_feedback_schedules.timezone_name,
          public.daily_feedback_schedules.cutoff_local,
          public.daily_feedback_schedules.retention_hours,
          public.daily_feedback_schedules.sanitizer_version,
          public.daily_feedback_schedules.selection_version,
          public.daily_feedback_schedules.renderer_version,
          public.daily_feedback_schedules.reviewer_binding_id,
          public.daily_feedback_schedules.reviewer_binding_generation,
          public.daily_feedback_schedules.enabled
        ) is distinct from row(
          excluded.chatwoot_account_id, excluded.chatwoot_inbox_id,
          excluded.chatwoot_agent_bot_id, excluded.timezone_name,
          excluded.cutoff_local, excluded.retention_hours,
          excluded.sanitizer_version, excluded.selection_version,
          excluded.renderer_version, excluded.reviewer_binding_id,
          excluded.reviewer_binding_generation, excluded.enabled
        ) then null else public.daily_feedback_schedules.lease_expires_at end,
      pending_local_date = case when row(
          public.daily_feedback_schedules.chatwoot_account_id,
          public.daily_feedback_schedules.chatwoot_inbox_id,
          public.daily_feedback_schedules.chatwoot_agent_bot_id,
          public.daily_feedback_schedules.timezone_name,
          public.daily_feedback_schedules.cutoff_local,
          public.daily_feedback_schedules.retention_hours,
          public.daily_feedback_schedules.sanitizer_version,
          public.daily_feedback_schedules.selection_version,
          public.daily_feedback_schedules.renderer_version,
          public.daily_feedback_schedules.reviewer_binding_id,
          public.daily_feedback_schedules.reviewer_binding_generation,
          public.daily_feedback_schedules.enabled
        ) is distinct from row(
          excluded.chatwoot_account_id, excluded.chatwoot_inbox_id,
          excluded.chatwoot_agent_bot_id, excluded.timezone_name,
          excluded.cutoff_local, excluded.retention_hours,
          excluded.sanitizer_version, excluded.selection_version,
          excluded.renderer_version, excluded.reviewer_binding_id,
          excluded.reviewer_binding_generation, excluded.enabled
        ) then null else public.daily_feedback_schedules.pending_local_date end,
      pending_window_start = case when row(
          public.daily_feedback_schedules.chatwoot_account_id,
          public.daily_feedback_schedules.chatwoot_inbox_id,
          public.daily_feedback_schedules.chatwoot_agent_bot_id,
          public.daily_feedback_schedules.timezone_name,
          public.daily_feedback_schedules.cutoff_local,
          public.daily_feedback_schedules.retention_hours,
          public.daily_feedback_schedules.sanitizer_version,
          public.daily_feedback_schedules.selection_version,
          public.daily_feedback_schedules.renderer_version,
          public.daily_feedback_schedules.reviewer_binding_id,
          public.daily_feedback_schedules.reviewer_binding_generation,
          public.daily_feedback_schedules.enabled
        ) is distinct from row(
          excluded.chatwoot_account_id, excluded.chatwoot_inbox_id,
          excluded.chatwoot_agent_bot_id, excluded.timezone_name,
          excluded.cutoff_local, excluded.retention_hours,
          excluded.sanitizer_version, excluded.selection_version,
          excluded.renderer_version, excluded.reviewer_binding_id,
          excluded.reviewer_binding_generation, excluded.enabled
        ) then null else public.daily_feedback_schedules.pending_window_start end,
      pending_window_end = case when row(
          public.daily_feedback_schedules.chatwoot_account_id,
          public.daily_feedback_schedules.chatwoot_inbox_id,
          public.daily_feedback_schedules.chatwoot_agent_bot_id,
          public.daily_feedback_schedules.timezone_name,
          public.daily_feedback_schedules.cutoff_local,
          public.daily_feedback_schedules.retention_hours,
          public.daily_feedback_schedules.sanitizer_version,
          public.daily_feedback_schedules.selection_version,
          public.daily_feedback_schedules.renderer_version,
          public.daily_feedback_schedules.reviewer_binding_id,
          public.daily_feedback_schedules.reviewer_binding_generation,
          public.daily_feedback_schedules.enabled
        ) is distinct from row(
          excluded.chatwoot_account_id, excluded.chatwoot_inbox_id,
          excluded.chatwoot_agent_bot_id, excluded.timezone_name,
          excluded.cutoff_local, excluded.retention_hours,
          excluded.sanitizer_version, excluded.selection_version,
          excluded.renderer_version, excluded.reviewer_binding_id,
          excluded.reviewer_binding_generation, excluded.enabled
        ) then null else public.daily_feedback_schedules.pending_window_end end,
      updated_at = case when row(
          public.daily_feedback_schedules.chatwoot_account_id,
          public.daily_feedback_schedules.chatwoot_inbox_id,
          public.daily_feedback_schedules.chatwoot_agent_bot_id,
          public.daily_feedback_schedules.timezone_name,
          public.daily_feedback_schedules.cutoff_local,
          public.daily_feedback_schedules.retention_hours,
          public.daily_feedback_schedules.sanitizer_version,
          public.daily_feedback_schedules.selection_version,
          public.daily_feedback_schedules.renderer_version,
          public.daily_feedback_schedules.reviewer_binding_id,
          public.daily_feedback_schedules.reviewer_binding_generation,
          public.daily_feedback_schedules.enabled
        ) is distinct from row(
          excluded.chatwoot_account_id, excluded.chatwoot_inbox_id,
          excluded.chatwoot_agent_bot_id, excluded.timezone_name,
          excluded.cutoff_local, excluded.retention_hours,
          excluded.sanitizer_version, excluded.selection_version,
          excluded.renderer_version, excluded.reviewer_binding_id,
          excluded.reviewer_binding_generation, excluded.enabled
        ) then clock_timestamp() else public.daily_feedback_schedules.updated_at end
  returning * into v_schedule;
  v_result := jsonb_build_object(
    'status','configured','schedule_id',v_schedule.id,'schedule_generation',v_schedule.schedule_generation,
    'binding_id',v_binding.id,'binding_generation',v_binding.binding_generation,'enabled',v_schedule.enabled
  );
  return public.daily_feedback_finish_command(p_command_id, v_result);
end;
$$;

create function public.claim_daily_feedback_collection_v1(
  p_command_id uuid,
  p_semantic_fingerprint text,
  p_worker_id text,
  p_tenant_ref text,
  p_scope_ref text,
  p_now timestamptz,
  p_force boolean default false,
  p_lease_seconds integer default 120
) returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_prior jsonb;
  v_schedule public.daily_feedback_schedules%rowtype;
  v_local_date date;
  v_start timestamptz;
  v_end timestamptz;
  v_result jsonb;
begin
  v_prior := public.daily_feedback_begin_command(
    p_command_id,'claim_collection',p_semantic_fingerprint,
    public.daily_feedback_server_payload_hash(jsonb_build_object(
      'worker_id',p_worker_id,'tenant_ref',p_tenant_ref,'scope_ref',p_scope_ref,
      'now',p_now,'force',p_force,'lease_seconds',p_lease_seconds
    ))
  );
  if v_prior is not null then return v_prior || jsonb_build_object('status','replayed'); end if;
  if p_worker_id !~ '^[a-zA-Z0-9._:-]{3,128}$'
     or p_tenant_ref !~ '^[a-z0-9][a-z0-9_-]{0,63}$'
     or p_scope_ref !~ '^[a-z0-9][a-z0-9_-]{0,127}$'
     or p_lease_seconds not between 30 and 900 then
    raise exception 'invalid_collection_claim';
  end if;
  select * into v_schedule
  from public.daily_feedback_schedules s
  where s.enabled
    and s.tenant_ref=p_tenant_ref and s.scope_ref=p_scope_ref
    and s.next_attempt_at <= p_now
    and (s.lease_expires_at is null or s.lease_expires_at <= p_now)
    and (
      p_force
      or (p_now at time zone s.timezone_name)::time >= s.cutoff_local
    )
    and not exists (
      select 1 from public.daily_feedback_batches b
      where b.tenant_ref=s.tenant_ref and b.scope_ref=s.scope_ref
        and b.local_date=(p_now at time zone s.timezone_name)::date
    )
  order by s.next_attempt_at, s.id
  for update skip locked
  limit 1;
  if not found then
    v_result := jsonb_build_object('status','idle');
    delete from public.daily_feedback_workflow_commands where command_id=p_command_id;
    return v_result;
  end if;
  v_local_date := (p_now at time zone v_schedule.timezone_name)::date;
  v_start := coalesce(
    v_schedule.last_completed_window_end,
    (((v_local_date - 1)::timestamp + v_schedule.cutoff_local) at time zone v_schedule.timezone_name)
  );
  v_end := least(
    p_now,
    ((v_local_date::timestamp + v_schedule.cutoff_local) at time zone v_schedule.timezone_name)
  );
  if v_start >= v_end then raise exception 'invalid_collection_window'; end if;
  update public.daily_feedback_schedules
  set lease_owner=p_worker_id,
      lease_generation=lease_generation+1,
      lease_expires_at=p_now + make_interval(secs => p_lease_seconds),
      pending_local_date=v_local_date,
      pending_window_start=v_start,
      pending_window_end=v_end,
      last_error_code=null,
      updated_at=clock_timestamp()
  where id=v_schedule.id
  returning * into v_schedule;
  v_result := jsonb_build_object(
    'status','claimed','schedule_id',v_schedule.id,'tenant_ref',v_schedule.tenant_ref,
    'scope_ref',v_schedule.scope_ref,'local_date',v_schedule.pending_local_date,
    'chatwoot_account_id',v_schedule.chatwoot_account_id,
    'chatwoot_inbox_id',v_schedule.chatwoot_inbox_id,
    'chatwoot_agent_bot_id',v_schedule.chatwoot_agent_bot_id,
    'window_start',v_schedule.pending_window_start,'window_end',v_schedule.pending_window_end,
    'lease_generation',v_schedule.lease_generation,'retention_hours',v_schedule.retention_hours,
    'sanitizer_version',v_schedule.sanitizer_version,'selection_version',v_schedule.selection_version,
    'renderer_version',v_schedule.renderer_version
  );
  return public.daily_feedback_finish_command(p_command_id, v_result);
end;
$$;

create function public.commit_daily_feedback_batch_v1(
  p_command_id uuid,
  p_semantic_fingerprint text,
  p_worker_id text,
  p_schedule_id uuid,
  p_lease_generation bigint,
  p_package_fingerprint text,
  p_release_lineage text,
  p_items jsonb
) returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_prior jsonb;
  v_schedule public.daily_feedback_schedules%rowtype;
  v_batch public.daily_feedback_batches%rowtype;
  v_item jsonb;
  v_expected integer := 1;
  v_result jsonb;
begin
  v_prior := public.daily_feedback_begin_command(
    p_command_id,'commit_batch',p_semantic_fingerprint,
    public.daily_feedback_server_payload_hash(jsonb_build_object(
      'worker_id',p_worker_id,'schedule_id',p_schedule_id,'lease_generation',p_lease_generation,
      'package_fingerprint',p_package_fingerprint,'release_lineage',p_release_lineage,'items',p_items
    ))
  );
  if v_prior is not null then return v_prior || jsonb_build_object('status','replayed'); end if;
  if p_package_fingerprint !~ '^[a-f0-9]{64}$' or jsonb_typeof(p_items) <> 'array'
     or jsonb_array_length(p_items) > 500 or char_length(p_release_lineage) not between 1 and 128 then
    raise exception 'invalid_daily_feedback_package';
  end if;
  select * into v_schedule from public.daily_feedback_schedules where id=p_schedule_id for update;
  if not found or v_schedule.lease_owner is distinct from p_worker_id
     or v_schedule.lease_generation <> p_lease_generation
     or v_schedule.lease_expires_at <= clock_timestamp()
     or v_schedule.pending_local_date is null then
    raise exception 'stale_collection_lease';
  end if;
  insert into public.daily_feedback_batches(
    schedule_id, tenant_ref, scope_ref, local_date, window_start, window_end,
    sanitizer_version, selection_version, renderer_version, release_lineage,
    package_fingerprint, item_count, reviewer_binding_id, reviewer_binding_generation,
    retention_expires_at
  ) values (
    v_schedule.id, v_schedule.tenant_ref, v_schedule.scope_ref, v_schedule.pending_local_date,
    v_schedule.pending_window_start, v_schedule.pending_window_end,
    v_schedule.sanitizer_version, v_schedule.selection_version, v_schedule.renderer_version,
    p_release_lineage, p_package_fingerprint, jsonb_array_length(p_items),
    v_schedule.reviewer_binding_id, v_schedule.reviewer_binding_generation,
    clock_timestamp() + make_interval(hours => v_schedule.retention_hours)
  )
  on conflict (tenant_ref, scope_ref, local_date) do nothing
  returning * into v_batch;
  if not found then
    select * into v_batch from public.daily_feedback_batches
    where tenant_ref=v_schedule.tenant_ref and scope_ref=v_schedule.scope_ref
      and local_date=v_schedule.pending_local_date for update;
    if v_batch.package_fingerprint <> p_package_fingerprint then
      raise exception 'logical_batch_conflict';
    end if;
  else
    for v_item in select value from jsonb_array_elements(p_items) loop
      if jsonb_typeof(v_item) <> 'object'
         or (select array_agg(k order by k) from jsonb_object_keys(v_item) k)
            is distinct from array['apparent_objective','conversation_ref','display_label','messages','observed_outcome','release_id','release_version']::text[]
         or (v_item->>'conversation_ref') !~ '^conv_[a-f0-9]{20}$'
         or not public.daily_feedback_messages_valid(v_item->'messages') then
        raise exception 'invalid_daily_feedback_item';
      end if;
      insert into public.daily_feedback_items(
        batch_id,item_index,conversation_ref,display_label,apparent_objective,
        observed_outcome,release_id,release_version,messages
      ) values (
        v_batch.id,v_expected,v_item->>'conversation_ref',v_item->>'display_label',
        v_item->>'apparent_objective',v_item->>'observed_outcome',v_item->>'release_id',
        (v_item->>'release_version')::integer,v_item->'messages'
      );
      v_expected := v_expected + 1;
    end loop;
  end if;
  update public.daily_feedback_schedules
  set last_completed_local_date=pending_local_date,
      last_completed_window_end=pending_window_end,
      lease_owner=null,lease_expires_at=null,
      pending_local_date=null,pending_window_start=null,pending_window_end=null,
      next_attempt_at=clock_timestamp(),updated_at=clock_timestamp()
  where id=v_schedule.id;
  v_result := jsonb_build_object(
    'status','committed','batch_id',v_batch.id,'public_ref',v_batch.public_ref,
    'item_count',v_batch.item_count,'retention_expires_at',v_batch.retention_expires_at
  );
  return public.daily_feedback_finish_command(p_command_id, v_result);
end;
$$;

create function public.fail_daily_feedback_collection_v1(
  p_command_id uuid,p_semantic_fingerprint text,p_worker_id text,p_schedule_id uuid,
  p_lease_generation bigint,p_error_code text,p_retry_seconds integer default 60
) returns jsonb
language plpgsql security definer set search_path=''
as $$
declare v_prior jsonb; v_result jsonb;
begin
  v_prior:=public.daily_feedback_begin_command(
    p_command_id,'fail_collection',p_semantic_fingerprint,
    public.daily_feedback_server_payload_hash(jsonb_build_object(
      'worker_id',p_worker_id,'schedule_id',p_schedule_id,'lease_generation',p_lease_generation,
      'error_code',p_error_code,'retry_seconds',p_retry_seconds
    ))
  );
  if v_prior is not null then return v_prior||jsonb_build_object('status','replayed'); end if;
  if p_error_code !~ '^[a-z0-9_]{3,80}$' or p_retry_seconds not between 5 and 3600 then raise exception 'invalid_collection_failure'; end if;
  update public.daily_feedback_schedules set lease_owner=null,lease_expires_at=null,
    pending_local_date=null,pending_window_start=null,pending_window_end=null,
    next_attempt_at=clock_timestamp()+make_interval(secs=>p_retry_seconds),last_error_code=p_error_code,updated_at=clock_timestamp()
  where id=p_schedule_id and lease_owner=p_worker_id and lease_generation=p_lease_generation and lease_expires_at>clock_timestamp();
  if not found then raise exception 'stale_collection_lease'; end if;
  v_result:=jsonb_build_object('status','retry_scheduled');
  return public.daily_feedback_finish_command(p_command_id,v_result);
end;
$$;

create function public.claim_daily_feedback_notification_v1(
  p_command_id uuid,p_semantic_fingerprint text,p_worker_id text,
  p_tenant_ref text,p_scope_ref text,p_now timestamptz,p_lease_seconds integer default 120
) returns jsonb
language plpgsql security definer set search_path=''
as $$
declare v_prior jsonb; v_batch public.daily_feedback_batches%rowtype; v_result jsonb;
begin
  v_prior:=public.daily_feedback_begin_command(
    p_command_id,'claim_notification',p_semantic_fingerprint,
    public.daily_feedback_server_payload_hash(jsonb_build_object(
      'worker_id',p_worker_id,'tenant_ref',p_tenant_ref,'scope_ref',p_scope_ref,
      'now',p_now,'lease_seconds',p_lease_seconds
    ))
  );
  if v_prior is not null then return v_prior||jsonb_build_object('status','replayed'); end if;
  if p_worker_id !~ '^[a-zA-Z0-9._:-]{3,128}$'
     or p_tenant_ref !~ '^[a-z0-9][a-z0-9_-]{0,63}$'
     or p_scope_ref !~ '^[a-z0-9][a-z0-9_-]{0,127}$'
     or p_lease_seconds not between 30 and 900 then raise exception 'invalid_notification_claim'; end if;
  update public.daily_feedback_batches
  set notification_state='delivery_unknown',notification_lease_owner=null,
    notification_lease_expires_at=null,notification_next_attempt_at=p_now,
    notification_last_error='worker_lost_after_request_start',updated_at=clock_timestamp()
  where tenant_ref=p_tenant_ref and scope_ref=p_scope_ref
    and notification_state='request_started' and notification_lease_expires_at<=p_now;
  select * into v_batch from public.daily_feedback_batches b
  where b.state<>'purged' and b.retention_expires_at>p_now
    and b.tenant_ref=p_tenant_ref and b.scope_ref=p_scope_ref
    and b.notification_state in ('pending','retry','claimed','delivery_unknown')
    and b.notification_next_attempt_at<=p_now
    and (b.notification_lease_expires_at is null or b.notification_lease_expires_at<=p_now)
  order by b.notification_next_attempt_at,b.created_at,b.id
  for update skip locked limit 1;
  if not found then
    v_result:=jsonb_build_object('status','idle');
    delete from public.daily_feedback_workflow_commands where command_id=p_command_id;
    return v_result;
  end if;
  update public.daily_feedback_batches set notification_state='claimed',notification_lease_owner=p_worker_id,
    notification_lease_generation=notification_lease_generation+1,
    notification_lease_expires_at=p_now+make_interval(secs=>p_lease_seconds),updated_at=clock_timestamp()
  where id=v_batch.id returning * into v_batch;
  v_result:=jsonb_build_object('status','claimed','batch_id',v_batch.id,'public_ref',v_batch.public_ref,
    'tenant_ref',v_batch.tenant_ref,'scope_ref',v_batch.scope_ref,'item_count',v_batch.item_count,
    'local_date',v_batch.local_date,'notification_occurred_at',v_batch.created_at,
    'lease_generation',v_batch.notification_lease_generation);
  return public.daily_feedback_finish_command(p_command_id,v_result);
end;
$$;

create function public.mark_daily_feedback_notification_started_v1(
  p_command_id uuid,p_semantic_fingerprint text,p_worker_id text,p_batch_id uuid,p_lease_generation bigint
) returns jsonb
language plpgsql security definer set search_path=''
as $$
declare v_prior jsonb; v_result jsonb;
begin
  v_prior:=public.daily_feedback_begin_command(
    p_command_id,'notification_started',p_semantic_fingerprint,
    public.daily_feedback_server_payload_hash(jsonb_build_object(
      'worker_id',p_worker_id,'batch_id',p_batch_id,'lease_generation',p_lease_generation
    ))
  );
  if v_prior is not null then return v_prior||jsonb_build_object('status','replayed'); end if;
  update public.daily_feedback_batches set notification_state='request_started',notification_attempts=notification_attempts+1,updated_at=clock_timestamp()
  where id=p_batch_id and notification_state='claimed' and notification_lease_owner=p_worker_id
    and notification_lease_generation=p_lease_generation and notification_lease_expires_at>clock_timestamp()
    and retention_expires_at>clock_timestamp();
  if not found then
    if exists(select 1 from public.daily_feedback_batches where id=p_batch_id and retention_expires_at<=clock_timestamp()) then
      raise exception 'notification_expired';
    end if;
    raise exception 'stale_notification_lease';
  end if;
  v_result:=jsonb_build_object('status','request_started');
  return public.daily_feedback_finish_command(p_command_id,v_result);
end;
$$;

create function public.complete_daily_feedback_notification_v1(
  p_command_id uuid,p_semantic_fingerprint text,p_worker_id text,p_batch_id uuid,p_lease_generation bigint
) returns jsonb
language plpgsql security definer set search_path=''
as $$
declare v_prior jsonb; v_result jsonb;
begin
  v_prior:=public.daily_feedback_begin_command(
    p_command_id,'complete_notification',p_semantic_fingerprint,
    public.daily_feedback_server_payload_hash(jsonb_build_object(
      'worker_id',p_worker_id,'batch_id',p_batch_id,'lease_generation',p_lease_generation
    ))
  );
  if v_prior is not null then return v_prior||jsonb_build_object('status','replayed'); end if;
  update public.daily_feedback_batches set notification_state='admitted',notification_admitted_at=clock_timestamp(),
    notification_lease_owner=null,notification_lease_expires_at=null,notification_last_error=null,updated_at=clock_timestamp()
  where id=p_batch_id and notification_state='request_started' and notification_lease_owner=p_worker_id
    and notification_lease_generation=p_lease_generation and notification_lease_expires_at>clock_timestamp();
  if not found then raise exception 'stale_notification_lease'; end if;
  v_result:=jsonb_build_object('status','admitted');
  return public.daily_feedback_finish_command(p_command_id,v_result);
end;
$$;

create function public.retry_daily_feedback_notification_v1(
  p_command_id uuid,p_semantic_fingerprint text,p_worker_id text,p_batch_id uuid,p_lease_generation bigint,
  p_error_code text,p_retry_seconds integer default 60
) returns jsonb
language plpgsql security definer set search_path=''
as $$
declare v_prior jsonb; v_result jsonb; v_state text;
begin
  v_prior:=public.daily_feedback_begin_command(
    p_command_id,'retry_notification',p_semantic_fingerprint,
    public.daily_feedback_server_payload_hash(jsonb_build_object(
      'worker_id',p_worker_id,'batch_id',p_batch_id,'lease_generation',p_lease_generation,
      'error_code',p_error_code,'retry_seconds',p_retry_seconds
    ))
  );
  if v_prior is not null then return v_prior||jsonb_build_object('status','replayed'); end if;
  update public.daily_feedback_batches
  set notification_state=case when notification_state='request_started' then 'delivery_unknown' else 'retry' end,
    notification_lease_owner=null,notification_lease_expires_at=null,
    notification_next_attempt_at=clock_timestamp()+make_interval(secs=>p_retry_seconds),notification_last_error=p_error_code,updated_at=clock_timestamp()
  where id=p_batch_id and notification_state in ('claimed','request_started') and notification_lease_owner=p_worker_id
    and notification_lease_generation=p_lease_generation
    and retention_expires_at>clock_timestamp()
  returning notification_state into v_state;
  if not found then
    if exists(select 1 from public.daily_feedback_batches where id=p_batch_id and retention_expires_at<=clock_timestamp()) then
      raise exception 'notification_expired';
    end if;
    raise exception 'stale_notification_lease';
  end if;
  v_result:=jsonb_build_object('status',case when v_state='delivery_unknown' then 'delivery_unknown' else 'retry_scheduled' end);
  return public.daily_feedback_finish_command(p_command_id,v_result);
end;
$$;

create function public.begin_daily_feedback_oidc_v1(
  p_state_hash text,p_public_ref uuid,p_return_path text,p_expires_at timestamptz
) returns jsonb
language plpgsql security definer set search_path=''
as $$
declare v_batch public.daily_feedback_batches%rowtype;
begin
  select * into v_batch from public.daily_feedback_batches
  where public_ref=p_public_ref and state<>'purged' and retention_expires_at>clock_timestamp();
  if not found or p_return_path <> '/daily-feedback/review/'||p_public_ref::text
     or p_expires_at<=clock_timestamp() or p_expires_at>clock_timestamp()+interval '10 minutes' then
    raise exception 'review_batch_unavailable';
  end if;
  insert into public.daily_feedback_oidc_states(state_hash,batch_id,return_path,expires_at)
  values(p_state_hash,v_batch.id,p_return_path,p_expires_at);
  return jsonb_build_object('status','created');
end;
$$;

create function public.complete_daily_feedback_oidc_v1(
  p_state_hash text,p_session_hash text,p_oidc_issuer text,p_oidc_subject text,
  p_slack_team_id text,p_slack_user_id text,p_session_expires_at timestamptz
) returns jsonb
language plpgsql security definer set search_path=''
as $$
declare v_state public.daily_feedback_oidc_states%rowtype; v_batch public.daily_feedback_batches%rowtype; v_binding public.daily_feedback_reviewer_bindings%rowtype;
begin
  select * into v_state from public.daily_feedback_oidc_states where state_hash=p_state_hash for update;
  if not found or v_state.consumed_at is not null or v_state.expires_at<=clock_timestamp() then raise exception 'invalid_oidc_state'; end if;
  select * into v_batch from public.daily_feedback_batches where id=v_state.batch_id for update;
  if v_batch.state='purged' or v_batch.retention_expires_at<=clock_timestamp() then raise exception 'review_batch_unavailable'; end if;
  select * into v_binding from public.daily_feedback_reviewer_bindings
  where id=v_batch.reviewer_binding_id and active and binding_generation=v_batch.reviewer_binding_generation
    and tenant_ref=v_batch.tenant_ref and scope_ref=v_batch.scope_ref
    and oidc_issuer=p_oidc_issuer and oidc_subject=p_oidc_subject
    and slack_team_id=p_slack_team_id and slack_user_id=p_slack_user_id;
  if not found then raise exception 'reviewer_not_authorized'; end if;
  if p_session_expires_at<=clock_timestamp() or p_session_expires_at>clock_timestamp()+interval '8 hours' then raise exception 'invalid_session_expiry'; end if;
  update public.daily_feedback_oidc_states set consumed_at=clock_timestamp() where state_hash=p_state_hash;
  insert into public.daily_feedback_sessions(
    session_hash,batch_id,reviewer_binding_id,binding_generation,oidc_issuer,oidc_subject,
    slack_team_id,slack_user_id,expires_at
  ) values(
    p_session_hash,v_batch.id,v_binding.id,v_binding.binding_generation,p_oidc_issuer,p_oidc_subject,
    p_slack_team_id,p_slack_user_id,p_session_expires_at
  );
  return jsonb_build_object('status','authenticated','return_path',v_state.return_path,'public_ref',v_batch.public_ref);
end;
$$;

create function public.get_daily_feedback_review_page_v1(p_session_hash text,p_public_ref uuid)
returns jsonb
language plpgsql security definer set search_path=''
as $$
declare v_session public.daily_feedback_sessions%rowtype; v_batch public.daily_feedback_batches%rowtype; v_binding public.daily_feedback_reviewer_bindings%rowtype; v_item public.daily_feedback_items%rowtype; v_done integer;
begin
  select * into v_session from public.daily_feedback_sessions where session_hash=p_session_hash for update;
  if not found or v_session.revoked_at is not null or v_session.expires_at<=clock_timestamp() then raise exception 'review_session_invalid'; end if;
  select * into v_batch from public.daily_feedback_batches where public_ref=p_public_ref for update;
  if not found or v_session.batch_id<>v_batch.id or v_batch.state='purged' or v_batch.retention_expires_at<=clock_timestamp() then raise exception 'review_batch_unavailable'; end if;
  select * into v_binding from public.daily_feedback_reviewer_bindings where id=v_session.reviewer_binding_id;
  if not found or not v_binding.active or v_binding.binding_generation<>v_session.binding_generation
    or v_binding.id<>v_batch.reviewer_binding_id or v_binding.binding_generation<>v_batch.reviewer_binding_generation
    or v_binding.tenant_ref<>v_batch.tenant_ref or v_binding.scope_ref<>v_batch.scope_ref then raise exception 'reviewer_not_authorized'; end if;
  update public.daily_feedback_sessions set last_seen_at=clock_timestamp() where session_hash=p_session_hash;
  select count(*) into v_done from public.daily_feedback_decisions where batch_id=v_batch.id;
  select i.* into v_item from public.daily_feedback_items i
  where i.batch_id=v_batch.id and not exists(select 1 from public.daily_feedback_decisions d where d.item_id=i.id)
  order by i.item_index limit 1;
  if not found then
    return jsonb_build_object('status','complete','local_date',v_batch.local_date,'item_count',v_batch.item_count,'decided_count',v_done,'retention_expires_at',v_batch.retention_expires_at);
  end if;
  return jsonb_build_object('status','item','local_date',v_batch.local_date,'item_count',v_batch.item_count,'decided_count',v_done,
    'retention_expires_at',v_batch.retention_expires_at,'item',jsonb_build_object(
      'item_id',v_item.id,'position',v_item.item_index,'display_label',v_item.display_label,
      'apparent_objective',v_item.apparent_objective,'observed_outcome',v_item.observed_outcome,
      'release_id',v_item.release_id,'release_version',v_item.release_version,'messages',v_item.messages));
end;
$$;

create function public.record_daily_feedback_decision_v1(
  p_command_id uuid,p_semantic_fingerprint text,p_session_hash text,p_public_ref uuid,p_item_id uuid,
  p_decision text,p_verbatim_feedback text
) returns jsonb
language plpgsql security definer set search_path=''
as $$
declare v_prior jsonb; v_page jsonb; v_batch public.daily_feedback_batches%rowtype; v_session public.daily_feedback_sessions%rowtype; v_item public.daily_feedback_items%rowtype; v_done integer; v_result jsonb;
begin
  v_prior:=public.daily_feedback_begin_command(
    p_command_id,'record_decision',p_semantic_fingerprint,
    public.daily_feedback_server_payload_hash(jsonb_build_object(
      'session_hash',p_session_hash,'public_ref',p_public_ref,'item_id',p_item_id,
      'decision',p_decision,'verbatim_feedback',p_verbatim_feedback
    ))
  );
  if v_prior is not null then return v_prior||jsonb_build_object('status','replayed'); end if;
  v_page:=public.get_daily_feedback_review_page_v1(p_session_hash,p_public_ref);
  if v_page->>'status'<>'item' or (v_page->'item'->>'item_id')::uuid<>p_item_id then raise exception 'review_item_not_current'; end if;
  if p_decision not in ('correct','correct_with_feedback','skip')
    or (p_decision='correct_with_feedback' and (p_verbatim_feedback is null or char_length(btrim(p_verbatim_feedback)) not between 1 and 4000))
    or (p_decision in ('correct','skip') and p_verbatim_feedback is not null)
    or (p_verbatim_feedback is not null and pg_catalog.regexp_replace(p_verbatim_feedback, E'[\\n\\t]', '', 'g') ~ '[[:cntrl:]]') then raise exception 'invalid_review_decision'; end if;
  select * into v_batch from public.daily_feedback_batches where public_ref=p_public_ref for update;
  select * into v_session from public.daily_feedback_sessions where session_hash=p_session_hash;
  select * into v_item from public.daily_feedback_items where id=p_item_id and batch_id=v_batch.id for update;
  insert into public.daily_feedback_decisions(batch_id,item_id,reviewer_binding_id,command_id,decision,verbatim_feedback)
  values(v_batch.id,v_item.id,v_session.reviewer_binding_id,p_command_id,p_decision,p_verbatim_feedback);
  select count(*) into v_done from public.daily_feedback_decisions where batch_id=v_batch.id;
  if v_done=v_batch.item_count then update public.daily_feedback_batches set state='completed',completed_at=clock_timestamp(),updated_at=clock_timestamp() where id=v_batch.id; end if;
  v_result:=jsonb_build_object('status','recorded','decided_count',v_done,'item_count',v_batch.item_count,'batch_complete',v_done=v_batch.item_count);
  return public.daily_feedback_finish_command(p_command_id,v_result);
end;
$$;

create function public.purge_expired_daily_feedback_v1(
  p_now timestamptz,p_deletion_owner text,p_tenant_ref text,p_scope_ref text,p_limit integer default 20
)
returns jsonb
language plpgsql security definer set search_path=''
as $$
declare v_batch public.daily_feedback_batches%rowtype; v_items integer; v_decisions integer; v_purged integer:=0;
begin
  if char_length(btrim(p_deletion_owner)) not between 1 and 128
     or p_tenant_ref !~ '^[a-z0-9][a-z0-9_-]{0,63}$'
     or p_scope_ref !~ '^[a-z0-9][a-z0-9_-]{0,127}$'
     or p_limit not between 1 and 100 then raise exception 'invalid_purge_request'; end if;
  perform set_config('app.daily_feedback_purge','on',true);
  for v_batch in select * from public.daily_feedback_batches b
    where b.tenant_ref=p_tenant_ref and b.scope_ref=p_scope_ref
      and b.state<>'purged' and b.retention_expires_at<=p_now
    order by b.retention_expires_at,b.id for update skip locked limit p_limit loop
    select count(*) into v_items from public.daily_feedback_items where batch_id=v_batch.id;
    select count(*) into v_decisions from public.daily_feedback_decisions where batch_id=v_batch.id;
    insert into public.daily_feedback_purge_tombstones(batch_id,tenant_ref,scope_ref,local_date,package_fingerprint,purged_item_count,purged_decision_count,deletion_owner)
    values(v_batch.id,v_batch.tenant_ref,v_batch.scope_ref,v_batch.local_date,v_batch.package_fingerprint,v_items,v_decisions,p_deletion_owner)
    on conflict (batch_id) do nothing;
    delete from public.daily_feedback_decisions where batch_id=v_batch.id;
    delete from public.daily_feedback_items where batch_id=v_batch.id;
    update public.daily_feedback_batches set state='purged',notification_state=case when notification_state='admitted' then 'admitted' else 'retry' end,
      notification_lease_owner=null,notification_lease_expires_at=null,updated_at=clock_timestamp() where id=v_batch.id;
    delete from public.daily_feedback_sessions where batch_id=v_batch.id;
    delete from public.daily_feedback_oidc_states where batch_id=v_batch.id;
    v_purged:=v_purged+1;
  end loop;
  return jsonb_build_object('status','purged','count',v_purged);
end;
$$;

do $acl$
declare v_table text; v_role text; v_function text;
begin
  foreach v_table in array array[
    'daily_feedback_reviewer_bindings','daily_feedback_schedules','daily_feedback_batches',
    'daily_feedback_items','daily_feedback_decisions','daily_feedback_oidc_states',
    'daily_feedback_sessions','daily_feedback_workflow_commands','daily_feedback_purge_tombstones'
  ] loop
    execute format('alter table public.%I enable row level security',v_table);
    execute format('revoke all on table public.%I from public',v_table);
    for v_role in select rolname from pg_roles where rolname in ('anon','authenticated','service_role') loop
      execute format('revoke all on table public.%I from %I',v_table,v_role);
    end loop;
  end loop;
  foreach v_function in array array[
    'daily_feedback_messages_valid(jsonb)',
    'daily_feedback_immutable_content_guard()',
    'daily_feedback_server_payload_hash(jsonb)',
    'daily_feedback_begin_command(uuid,text,text,text)',
    'daily_feedback_finish_command(uuid,jsonb)',
    'configure_daily_feedback_scope_v1(uuid,text,text,text,text,text,text,text,text,bigint,bigint,bigint,text,time,integer,text,text,text,boolean)',
    'claim_daily_feedback_collection_v1(uuid,text,text,text,text,timestamptz,boolean,integer)',
    'commit_daily_feedback_batch_v1(uuid,text,text,uuid,bigint,text,text,jsonb)',
    'fail_daily_feedback_collection_v1(uuid,text,text,uuid,bigint,text,integer)',
    'claim_daily_feedback_notification_v1(uuid,text,text,text,text,timestamptz,integer)',
    'mark_daily_feedback_notification_started_v1(uuid,text,text,uuid,bigint)',
    'complete_daily_feedback_notification_v1(uuid,text,text,uuid,bigint)',
    'retry_daily_feedback_notification_v1(uuid,text,text,uuid,bigint,text,integer)',
    'begin_daily_feedback_oidc_v1(text,uuid,text,timestamptz)',
    'complete_daily_feedback_oidc_v1(text,text,text,text,text,text,timestamptz)',
    'get_daily_feedback_review_page_v1(text,uuid)',
    'record_daily_feedback_decision_v1(uuid,text,text,uuid,uuid,text,text)',
    'purge_expired_daily_feedback_v1(timestamptz,text,text,text,integer)'
  ] loop
    execute format('revoke all on function public.%s from public',v_function);
    for v_role in select rolname from pg_roles where rolname in ('anon','authenticated','service_role') loop
      execute format('revoke all on function public.%s from %I',v_function,v_role);
    end loop;
  end loop;
  if exists(select 1 from pg_roles where rolname='service_role') then
    foreach v_function in array array[
      'configure_daily_feedback_scope_v1(uuid,text,text,text,text,text,text,text,text,bigint,bigint,bigint,text,time,integer,text,text,text,boolean)',
      'claim_daily_feedback_collection_v1(uuid,text,text,text,text,timestamptz,boolean,integer)',
      'commit_daily_feedback_batch_v1(uuid,text,text,uuid,bigint,text,text,jsonb)',
      'fail_daily_feedback_collection_v1(uuid,text,text,uuid,bigint,text,integer)',
      'claim_daily_feedback_notification_v1(uuid,text,text,text,text,timestamptz,integer)',
      'mark_daily_feedback_notification_started_v1(uuid,text,text,uuid,bigint)',
      'complete_daily_feedback_notification_v1(uuid,text,text,uuid,bigint)',
      'retry_daily_feedback_notification_v1(uuid,text,text,uuid,bigint,text,integer)',
      'begin_daily_feedback_oidc_v1(text,uuid,text,timestamptz)',
      'complete_daily_feedback_oidc_v1(text,text,text,text,text,text,timestamptz)',
      'get_daily_feedback_review_page_v1(text,uuid)',
      'record_daily_feedback_decision_v1(uuid,text,text,uuid,uuid,text,text)',
      'purge_expired_daily_feedback_v1(timestamptz,text,text,text,integer)'
    ] loop
      execute format('grant execute on function public.%s to service_role',v_function);
    end loop;
  end if;
end;
$acl$;

commit;
