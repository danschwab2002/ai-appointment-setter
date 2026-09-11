-- Forward-only multi-reviewer authority and deletion accountability.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

alter table public.daily_feedback_reviewer_bindings
  add column deletion_accountable boolean not null default false;

alter table public.daily_feedback_schedules
  add column reviewer_set_hash text,
  add column deletion_policy_ref text;

alter table public.daily_feedback_batches
  add column reviewer_set_hash text,
  add column deletion_policy_ref text;

alter table public.daily_feedback_purge_tombstones
  add column accountable_reviewer_refs text[] not null default '{}'::text[],
  add column accountable_reviewers jsonb not null default '[]'::jsonb,
  add column purge_actor_ref text not null default 'legacy-purge-worker';

update public.daily_feedback_purge_tombstones t
set accountable_reviewer_refs = array[rb.reviewer_ref],
    accountable_reviewers = jsonb_build_array(jsonb_build_object(
      'reviewer_ref',rb.reviewer_ref,
      'reviewer_binding_id',rb.id,
      'reviewer_binding_generation',b.reviewer_binding_generation,
      'oidc_issuer',rb.oidc_issuer,
      'oidc_subject',rb.oidc_subject,
      'slack_team_id',rb.slack_team_id,
      'slack_user_id',rb.slack_user_id
    )),
    purge_actor_ref = 'legacy-purge-worker'
from public.daily_feedback_batches b
join public.daily_feedback_reviewer_bindings rb on rb.id=b.reviewer_binding_id
where b.id=t.batch_id;

update public.daily_feedback_reviewer_bindings rb
set deletion_accountable = true
where exists (
  select 1 from public.daily_feedback_schedules s
  where s.reviewer_binding_id = rb.id
);

update public.daily_feedback_schedules s
set reviewer_set_hash = pg_catalog.encode(
      pg_catalog.sha256(pg_catalog.convert_to(
        jsonb_build_array(jsonb_build_object(
          'reviewer_ref', rb.reviewer_ref,
          'slack_user_id', rb.slack_user_id,
          'deletion_accountable', rb.deletion_accountable
        ))::text,
        'UTF8'
      )),
      'hex'
    ),
    deletion_policy_ref = 'legacy-single-reviewer-v1',
    enabled = false,
    schedule_generation = schedule_generation + 1,
    lease_owner = null,
    lease_expires_at = null,
    pending_local_date = null,
    pending_window_start = null,
    pending_window_end = null
from public.daily_feedback_reviewer_bindings rb
where rb.id = s.reviewer_binding_id;

update public.daily_feedback_batches b
set reviewer_set_hash = s.reviewer_set_hash,
    deletion_policy_ref = s.deletion_policy_ref
from public.daily_feedback_schedules s
where s.id = b.schedule_id;

alter table public.daily_feedback_schedules
  alter column reviewer_set_hash set not null,
  alter column deletion_policy_ref set not null,
  add constraint daily_feedback_schedule_reviewer_set_hash_valid
    check (reviewer_set_hash ~ '^[a-f0-9]{64}$'),
  add constraint daily_feedback_schedule_deletion_policy_valid
    check (deletion_policy_ref ~ '^[a-z0-9][a-z0-9._-]{1,127}$');

alter table public.daily_feedback_batches
  alter column reviewer_set_hash set not null,
  alter column deletion_policy_ref set not null,
  add constraint daily_feedback_batch_reviewer_set_hash_valid
    check (reviewer_set_hash ~ '^[a-f0-9]{64}$'),
  add constraint daily_feedback_batch_deletion_policy_valid
    check (deletion_policy_ref ~ '^[a-z0-9][a-z0-9._-]{1,127}$');

alter table public.daily_feedback_purge_tombstones
  alter column accountable_reviewer_refs drop default,
  alter column accountable_reviewers drop default,
  alter column purge_actor_ref drop default,
  add constraint daily_feedback_tombstone_accountable_reviewers_required
    check (cardinality(accountable_reviewer_refs) > 0),
  add constraint daily_feedback_tombstone_accountable_identity_required
    check (jsonb_typeof(accountable_reviewers)='array' and jsonb_array_length(accountable_reviewers)>0),
  add constraint daily_feedback_tombstone_purge_actor_valid
    check (purge_actor_ref ~ '^[a-zA-Z0-9._:-]{3,128}$');

create table public.daily_feedback_batch_reviewer_bindings (
  batch_id uuid not null references public.daily_feedback_batches(id) on delete restrict,
  reviewer_binding_id uuid not null references public.daily_feedback_reviewer_bindings(id) on delete restrict,
  reviewer_binding_generation bigint not null check (reviewer_binding_generation > 0),
  reviewer_ref text not null check (reviewer_ref ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
  oidc_issuer text not null check (oidc_issuer='https://slack.com'),
  oidc_subject text not null check (oidc_subject ~ '^https://slack.com/user_id/[UW][A-Z0-9]{8,}$'),
  slack_team_id text not null check (slack_team_id ~ '^T[A-Z0-9]{8,}$'),
  slack_user_id text not null check (slack_user_id ~ '^[UW][A-Z0-9]{8,}$'),
  deletion_accountable boolean not null,
  created_at timestamptz not null default clock_timestamp(),
  primary key (batch_id, reviewer_binding_id),
  unique (batch_id, reviewer_ref)
);

insert into public.daily_feedback_batch_reviewer_bindings(
  batch_id, reviewer_binding_id, reviewer_binding_generation, reviewer_ref,
  oidc_issuer,oidc_subject,slack_team_id,slack_user_id,deletion_accountable
)
select b.id, rb.id, b.reviewer_binding_generation, rb.reviewer_ref,
       rb.oidc_issuer,rb.oidc_subject,rb.slack_team_id,rb.slack_user_id,
       rb.deletion_accountable
from public.daily_feedback_batches b
join public.daily_feedback_reviewer_bindings rb on rb.id = b.reviewer_binding_id
where b.state <> 'purged';

update public.daily_feedback_batches b
set notification_state='delivery_unknown',
    notification_lease_owner=null,
    notification_lease_expires_at=null,
    notification_last_error='legacy_reviewer_set_blocked',
    updated_at=clock_timestamp()
where b.state<>'purged'
  and 4 <> (
    select count(*) from public.daily_feedback_batch_reviewer_bindings brb
    where brb.batch_id=b.id and brb.deletion_accountable
  );

create index daily_feedback_batch_reviewer_binding_idx
  on public.daily_feedback_batch_reviewer_bindings(reviewer_binding_id, batch_id);

create trigger daily_feedback_batch_reviewer_bindings_immutable
before update or delete on public.daily_feedback_batch_reviewer_bindings
for each row execute function public.daily_feedback_immutable_content_guard();

create function public.daily_feedback_tombstone_immutable_guard()
returns trigger
language plpgsql
set search_path=''
as $$
begin
  raise exception 'daily_feedback_tombstone_immutable';
end;
$$;

create trigger daily_feedback_purge_tombstones_immutable
before update or delete on public.daily_feedback_purge_tombstones
for each row execute function public.daily_feedback_tombstone_immutable_guard();

create function public.configure_daily_feedback_scope_v2(
  p_command_id uuid,
  p_semantic_fingerprint text,
  p_tenant_ref text,
  p_scope_ref text,
  p_oidc_issuer text,
  p_slack_team_id text,
  p_reviewers jsonb,
  p_deletion_policy_ref text,
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
  v_reviewer jsonb;
  v_canonical_reviewers jsonb;
  v_reviewer_set_hash text;
  v_primary public.daily_feedback_reviewer_bindings%rowtype;
  v_schedule public.daily_feedback_schedules%rowtype;
  v_changed boolean;
  v_result jsonb;
begin
  v_prior := public.daily_feedback_begin_command(
    p_command_id,
    'configure_scope_v2',
    p_semantic_fingerprint,
    public.daily_feedback_server_payload_hash(jsonb_build_object(
      'tenant_ref',p_tenant_ref,'scope_ref',p_scope_ref,
      'oidc_issuer',p_oidc_issuer,'slack_team_id',p_slack_team_id,
      'reviewers',p_reviewers,'deletion_policy_ref',p_deletion_policy_ref,
      'chatwoot_account_id',p_chatwoot_account_id,'chatwoot_inbox_id',p_chatwoot_inbox_id,
      'chatwoot_agent_bot_id',p_chatwoot_agent_bot_id,'timezone_name',p_timezone_name,
      'cutoff_local',p_cutoff_local,'retention_hours',p_retention_hours,
      'sanitizer_version',p_sanitizer_version,'selection_version',p_selection_version,
      'renderer_version',p_renderer_version,'enabled',p_enabled
    ))
  );
  if v_prior is not null then
    return v_prior || jsonb_build_object('status','replayed');
  end if;

  if p_tenant_ref !~ '^[a-z0-9][a-z0-9_-]{0,63}$'
     or p_scope_ref !~ '^[a-z0-9][a-z0-9_-]{0,127}$'
     or p_oidc_issuer <> 'https://slack.com'
     or p_slack_team_id !~ '^T[A-Z0-9]{8,}$'
     or p_deletion_policy_ref !~ '^[a-z0-9][a-z0-9._-]{1,127}$'
     or jsonb_typeof(p_reviewers) <> 'array'
     or not exists (select 1 from pg_catalog.pg_timezone_names where name = p_timezone_name)
     or p_retention_hours not between 1 and 168
     or least(p_chatwoot_account_id,p_chatwoot_inbox_id,p_chatwoot_agent_bot_id) < 1 then
    raise exception 'invalid_daily_feedback_scope_v2';
  end if;
  if jsonb_array_length(p_reviewers) <> 4 then
    raise exception 'reviewer_set_must_have_four';
  end if;

  for v_reviewer in select value from jsonb_array_elements(p_reviewers) loop
    if jsonb_typeof(v_reviewer) <> 'object' then
      raise exception 'unknown_reviewer_key';
    end if;
    if (select array_agg(k order by k) from jsonb_object_keys(v_reviewer) k)
       is distinct from array['deletion_accountable','reviewer_ref','slack_user_id']::text[] then
      raise exception 'unknown_reviewer_key';
    end if;
    if (v_reviewer->>'reviewer_ref') !~ '^[a-z0-9][a-z0-9_-]{0,63}$'
       or (v_reviewer->>'slack_user_id') !~ '^[UW][A-Z0-9]{8,}$'
       or jsonb_typeof(v_reviewer->'deletion_accountable') <> 'boolean' then
      raise exception 'invalid_reviewer';
    end if;
  end loop;

  if (select count(*) from jsonb_array_elements(p_reviewers)) <>
     (select count(distinct value->>'reviewer_ref') from jsonb_array_elements(p_reviewers)) then
    raise exception 'duplicate_reviewer_ref';
  end if;
  if (select count(*) from jsonb_array_elements(p_reviewers)) <>
     (select count(distinct value->>'slack_user_id') from jsonb_array_elements(p_reviewers)) then
    raise exception 'duplicate_slack_user_id';
  end if;
  if exists (
    select 1 from jsonb_array_elements(p_reviewers)
    where not (value->>'deletion_accountable')::boolean
  ) then
    raise exception 'all_reviewers_must_be_deletion_accountable';
  end if;

  select jsonb_agg(
    jsonb_build_object(
      'reviewer_ref', reviewer_ref,
      'slack_user_id', slack_user_id,
      'deletion_accountable', deletion_accountable
    ) order by reviewer_ref
  ) into v_canonical_reviewers
  from (
    select value->>'reviewer_ref' as reviewer_ref,
           value->>'slack_user_id' as slack_user_id,
           (value->>'deletion_accountable')::boolean as deletion_accountable
    from jsonb_array_elements(p_reviewers)
  ) desired;
  v_reviewer_set_hash := pg_catalog.encode(
    pg_catalog.sha256(pg_catalog.convert_to(v_canonical_reviewers::text, 'UTF8')),
    'hex'
  );

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(p_tenant_ref || pg_catalog.chr(31) || p_scope_ref, 0)
  );
  select * into v_schedule
  from public.daily_feedback_schedules
  where tenant_ref = p_tenant_ref and scope_ref = p_scope_ref
  for update;

  for v_reviewer in
    select value from jsonb_array_elements(p_reviewers)
    order by value->>'reviewer_ref'
  loop
    insert into public.daily_feedback_reviewer_bindings(
      tenant_ref,scope_ref,reviewer_ref,oidc_issuer,oidc_subject,
      slack_team_id,slack_user_id,deletion_accountable,active
    ) values (
      p_tenant_ref,p_scope_ref,v_reviewer->>'reviewer_ref',p_oidc_issuer,
      p_oidc_issuer || '/user_id/' || (v_reviewer->>'slack_user_id'),
      p_slack_team_id,v_reviewer->>'slack_user_id',
      (v_reviewer->>'deletion_accountable')::boolean,true
    )
    on conflict (tenant_ref,scope_ref,reviewer_ref) do update
    set oidc_issuer=excluded.oidc_issuer,
        oidc_subject=excluded.oidc_subject,
        slack_team_id=excluded.slack_team_id,
        slack_user_id=excluded.slack_user_id,
        deletion_accountable=excluded.deletion_accountable,
        active=true,
        binding_generation=public.daily_feedback_reviewer_bindings.binding_generation+
          (row(
            public.daily_feedback_reviewer_bindings.oidc_issuer,
            public.daily_feedback_reviewer_bindings.oidc_subject,
            public.daily_feedback_reviewer_bindings.slack_team_id,
            public.daily_feedback_reviewer_bindings.slack_user_id,
            public.daily_feedback_reviewer_bindings.deletion_accountable,
            public.daily_feedback_reviewer_bindings.active
          ) is distinct from row(
            excluded.oidc_issuer,excluded.oidc_subject,excluded.slack_team_id,
            excluded.slack_user_id,excluded.deletion_accountable,true
          ))::integer,
        updated_at=case when row(
            public.daily_feedback_reviewer_bindings.oidc_issuer,
            public.daily_feedback_reviewer_bindings.oidc_subject,
            public.daily_feedback_reviewer_bindings.slack_team_id,
            public.daily_feedback_reviewer_bindings.slack_user_id,
            public.daily_feedback_reviewer_bindings.deletion_accountable,
            public.daily_feedback_reviewer_bindings.active
          ) is distinct from row(
            excluded.oidc_issuer,excluded.oidc_subject,excluded.slack_team_id,
            excluded.slack_user_id,excluded.deletion_accountable,true
          ) then clock_timestamp()
          else public.daily_feedback_reviewer_bindings.updated_at end;
  end loop;

  update public.daily_feedback_reviewer_bindings rb
  set active=false,
      binding_generation=rb.binding_generation+1,
      updated_at=clock_timestamp()
  where rb.tenant_ref=p_tenant_ref and rb.scope_ref=p_scope_ref and rb.active
    and not exists (
      select 1 from jsonb_array_elements(p_reviewers) desired
      where desired->>'reviewer_ref'=rb.reviewer_ref
    );

  update public.daily_feedback_sessions s
  set revoked_at=coalesce(s.revoked_at,clock_timestamp())
  from public.daily_feedback_reviewer_bindings rb
  where s.reviewer_binding_id=rb.id and s.revoked_at is null
    and rb.tenant_ref=p_tenant_ref and rb.scope_ref=p_scope_ref
    and (not rb.active or s.binding_generation<>rb.binding_generation);

  select * into v_primary
  from public.daily_feedback_reviewer_bindings rb
  where rb.tenant_ref=p_tenant_ref and rb.scope_ref=p_scope_ref and rb.active
  order by rb.reviewer_ref
  limit 1;
  if not found then raise exception 'deletion_accountable_reviewer_required'; end if;

  if v_schedule.id is null then
    insert into public.daily_feedback_schedules(
      tenant_ref,scope_ref,chatwoot_account_id,chatwoot_inbox_id,chatwoot_agent_bot_id,
      timezone_name,cutoff_local,retention_hours,sanitizer_version,selection_version,
      renderer_version,reviewer_binding_id,reviewer_binding_generation,enabled,
      reviewer_set_hash,deletion_policy_ref
    ) values (
      p_tenant_ref,p_scope_ref,p_chatwoot_account_id,p_chatwoot_inbox_id,p_chatwoot_agent_bot_id,
      p_timezone_name,p_cutoff_local,p_retention_hours,p_sanitizer_version,p_selection_version,
      p_renderer_version,v_primary.id,v_primary.binding_generation,p_enabled,
      v_reviewer_set_hash,p_deletion_policy_ref
    ) returning * into v_schedule;
  else
    v_changed := row(
      v_schedule.chatwoot_account_id,v_schedule.chatwoot_inbox_id,
      v_schedule.chatwoot_agent_bot_id,v_schedule.timezone_name,v_schedule.cutoff_local,
      v_schedule.retention_hours,v_schedule.sanitizer_version,v_schedule.selection_version,
      v_schedule.renderer_version,v_schedule.reviewer_binding_id,
      v_schedule.reviewer_binding_generation,v_schedule.enabled,
      v_schedule.reviewer_set_hash,v_schedule.deletion_policy_ref
    ) is distinct from row(
      p_chatwoot_account_id,p_chatwoot_inbox_id,p_chatwoot_agent_bot_id,
      p_timezone_name,p_cutoff_local,p_retention_hours,p_sanitizer_version,
      p_selection_version,p_renderer_version,v_primary.id,v_primary.binding_generation,
      p_enabled,v_reviewer_set_hash,p_deletion_policy_ref
    );
    update public.daily_feedback_schedules
    set chatwoot_account_id=p_chatwoot_account_id,
        chatwoot_inbox_id=p_chatwoot_inbox_id,
        chatwoot_agent_bot_id=p_chatwoot_agent_bot_id,
        timezone_name=p_timezone_name,
        cutoff_local=p_cutoff_local,
        retention_hours=p_retention_hours,
        sanitizer_version=p_sanitizer_version,
        selection_version=p_selection_version,
        renderer_version=p_renderer_version,
        reviewer_binding_id=v_primary.id,
        reviewer_binding_generation=v_primary.binding_generation,
        enabled=p_enabled,
        reviewer_set_hash=v_reviewer_set_hash,
        deletion_policy_ref=p_deletion_policy_ref,
        schedule_generation=schedule_generation+v_changed::integer,
        lease_owner=case when v_changed then null else lease_owner end,
        lease_expires_at=case when v_changed then null else lease_expires_at end,
        pending_local_date=case when v_changed then null else pending_local_date end,
        pending_window_start=case when v_changed then null else pending_window_start end,
        pending_window_end=case when v_changed then null else pending_window_end end,
        updated_at=case when v_changed then clock_timestamp() else updated_at end
    where id=v_schedule.id
    returning * into v_schedule;
  end if;

  v_result:=jsonb_build_object(
    'status','configured','schedule_id',v_schedule.id,
    'schedule_generation',v_schedule.schedule_generation,
    'reviewer_set_hash',v_schedule.reviewer_set_hash,
    'reviewer_count',jsonb_array_length(p_reviewers),
    'enabled',v_schedule.enabled
  );
  return public.daily_feedback_finish_command(p_command_id,v_result);
end;
$$;

create or replace function public.configure_daily_feedback_scope_v1(
  p_command_id uuid,p_semantic_fingerprint text,p_tenant_ref text,p_scope_ref text,
  p_reviewer_ref text,p_oidc_issuer text,p_oidc_subject text,p_slack_team_id text,
  p_slack_user_id text,p_chatwoot_account_id bigint,p_chatwoot_inbox_id bigint,
  p_chatwoot_agent_bot_id bigint,p_timezone_name text,p_cutoff_local time,
  p_retention_hours integer,p_sanitizer_version text,p_selection_version text,
  p_renderer_version text,p_enabled boolean
) returns jsonb
language plpgsql security definer set search_path=''
as $$
begin
  raise exception 'daily_feedback_config_v1_disabled';
end;
$$;

create or replace function public.claim_daily_feedback_collection_v1(
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
  where (s.enabled or p_force)
    and s.tenant_ref=p_tenant_ref and s.scope_ref=p_scope_ref
    and s.next_attempt_at <= p_now
    and (s.lease_expires_at is null or s.lease_expires_at <= p_now)
    and (
      p_force
      or (p_now at time zone s.timezone_name)::time >= s.cutoff_local
    )
    and 4 = (
      select count(*) from public.daily_feedback_reviewer_bindings rb
      where rb.tenant_ref=s.tenant_ref and rb.scope_ref=s.scope_ref and rb.active
    )
    and 4 = (
      select count(*) from public.daily_feedback_reviewer_bindings rb
      where rb.tenant_ref=s.tenant_ref and rb.scope_ref=s.scope_ref
        and rb.active and rb.deletion_accountable
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

create or replace function public.claim_daily_feedback_notification_v1(
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
    and b.notification_state in ('pending','retry','claimed')
    and b.notification_next_attempt_at<=p_now
    and (b.notification_lease_expires_at is null or b.notification_lease_expires_at<=p_now)
    and 4=(select count(*) from public.daily_feedback_batch_reviewer_bindings brb where brb.batch_id=b.id)
    and 4=(select count(*) from public.daily_feedback_batch_reviewer_bindings brb where brb.batch_id=b.id and brb.deletion_accountable)
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
    'retention_expires_at',v_batch.retention_expires_at,
    'lease_generation',v_batch.notification_lease_generation);
  return public.daily_feedback_finish_command(p_command_id,v_result);
end;
$$;

create or replace function public.commit_daily_feedback_batch_v1(
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
  v_reviewer_count integer;
  v_accountable_count integer;
  v_canonical_reviewers jsonb;
  v_current_reviewer_set_hash text;
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

  select count(*),count(*) filter(where rb.deletion_accountable),
         jsonb_agg(jsonb_build_object(
           'reviewer_ref',rb.reviewer_ref,
           'slack_user_id',rb.slack_user_id,
           'deletion_accountable',rb.deletion_accountable
         ) order by rb.reviewer_ref)
  into v_reviewer_count,v_accountable_count,v_canonical_reviewers
  from public.daily_feedback_reviewer_bindings rb
  where rb.tenant_ref=v_schedule.tenant_ref and rb.scope_ref=v_schedule.scope_ref and rb.active;
  v_current_reviewer_set_hash:=pg_catalog.encode(
    pg_catalog.sha256(pg_catalog.convert_to(v_canonical_reviewers::text,'UTF8')),'hex'
  );
  if v_reviewer_count<>4 or v_accountable_count<>4
     or v_current_reviewer_set_hash<>v_schedule.reviewer_set_hash then
    raise exception 'reviewer_set_changed';
  end if;

  insert into public.daily_feedback_batches(
    schedule_id,tenant_ref,scope_ref,local_date,window_start,window_end,
    sanitizer_version,selection_version,renderer_version,release_lineage,
    package_fingerprint,item_count,reviewer_binding_id,reviewer_binding_generation,
    retention_expires_at,reviewer_set_hash,deletion_policy_ref
  ) values (
    v_schedule.id,v_schedule.tenant_ref,v_schedule.scope_ref,v_schedule.pending_local_date,
    v_schedule.pending_window_start,v_schedule.pending_window_end,
    v_schedule.sanitizer_version,v_schedule.selection_version,v_schedule.renderer_version,
    p_release_lineage,p_package_fingerprint,jsonb_array_length(p_items),
    v_schedule.reviewer_binding_id,v_schedule.reviewer_binding_generation,
    clock_timestamp()+make_interval(hours=>v_schedule.retention_hours),
    v_schedule.reviewer_set_hash,v_schedule.deletion_policy_ref
  )
  on conflict (tenant_ref,scope_ref,local_date) do nothing
  returning * into v_batch;
  if not found then
    select * into v_batch from public.daily_feedback_batches
    where tenant_ref=v_schedule.tenant_ref and scope_ref=v_schedule.scope_ref
      and local_date=v_schedule.pending_local_date for update;
    if v_batch.package_fingerprint<>p_package_fingerprint
       or v_batch.reviewer_set_hash<>v_schedule.reviewer_set_hash then
      raise exception 'logical_batch_conflict';
    end if;
  else
    insert into public.daily_feedback_batch_reviewer_bindings(
      batch_id,reviewer_binding_id,reviewer_binding_generation,reviewer_ref,
      oidc_issuer,oidc_subject,slack_team_id,slack_user_id,deletion_accountable
    )
    select v_batch.id,rb.id,rb.binding_generation,rb.reviewer_ref,
           rb.oidc_issuer,rb.oidc_subject,rb.slack_team_id,rb.slack_user_id,
           rb.deletion_accountable
    from public.daily_feedback_reviewer_bindings rb
    where rb.tenant_ref=v_schedule.tenant_ref and rb.scope_ref=v_schedule.scope_ref and rb.active
    order by rb.reviewer_ref;
    for v_item in select value from jsonb_array_elements(p_items) loop
      if jsonb_typeof(v_item)<>'object'
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
      v_expected:=v_expected+1;
    end loop;
  end if;
  update public.daily_feedback_schedules
  set last_completed_local_date=pending_local_date,last_completed_window_end=pending_window_end,
      lease_owner=null,lease_expires_at=null,pending_local_date=null,
      pending_window_start=null,pending_window_end=null,next_attempt_at=clock_timestamp(),
      updated_at=clock_timestamp()
  where id=v_schedule.id;
  v_result:=jsonb_build_object(
    'status','committed','batch_id',v_batch.id,'public_ref',v_batch.public_ref,
    'item_count',v_batch.item_count,'reviewer_count',v_reviewer_count,
    'retention_expires_at',v_batch.retention_expires_at
  );
  return public.daily_feedback_finish_command(p_command_id,v_result);
end;
$$;

create or replace function public.complete_daily_feedback_oidc_v1(
  p_state_hash text,p_session_hash text,p_oidc_issuer text,p_oidc_subject text,
  p_slack_team_id text,p_slack_user_id text,p_session_expires_at timestamptz
) returns jsonb
language plpgsql security definer set search_path=''
as $$
declare
  v_state public.daily_feedback_oidc_states%rowtype;
  v_batch public.daily_feedback_batches%rowtype;
  v_binding public.daily_feedback_reviewer_bindings%rowtype;
begin
  select * into v_state from public.daily_feedback_oidc_states where state_hash=p_state_hash for update;
  if not found or v_state.consumed_at is not null or v_state.expires_at<=clock_timestamp() then
    raise exception 'invalid_oidc_state';
  end if;
  select * into v_batch from public.daily_feedback_batches where id=v_state.batch_id for update;
  if v_batch.state='purged' or v_batch.retention_expires_at<=clock_timestamp() then
    raise exception 'review_batch_unavailable';
  end if;
  if 4 <> (select count(*) from public.daily_feedback_batch_reviewer_bindings where batch_id=v_batch.id)
     or 4 <> (select count(*) from public.daily_feedback_batch_reviewer_bindings where batch_id=v_batch.id and deletion_accountable) then
    raise exception 'reviewer_set_incomplete';
  end if;
  select rb.* into v_binding
  from public.daily_feedback_reviewer_bindings rb
  join public.daily_feedback_batch_reviewer_bindings brb
    on brb.batch_id=v_batch.id and brb.reviewer_binding_id=rb.id
   and brb.reviewer_binding_generation=rb.binding_generation
   and brb.oidc_issuer=rb.oidc_issuer and brb.oidc_subject=rb.oidc_subject
   and brb.slack_team_id=rb.slack_team_id and brb.slack_user_id=rb.slack_user_id
  where rb.active
    and rb.tenant_ref=v_batch.tenant_ref and rb.scope_ref=v_batch.scope_ref
    and brb.oidc_issuer=p_oidc_issuer and brb.oidc_subject=p_oidc_subject
    and brb.slack_team_id=p_slack_team_id and brb.slack_user_id=p_slack_user_id;
  if not found then raise exception 'reviewer_not_authorized'; end if;
  if p_session_expires_at<=clock_timestamp()
     or p_session_expires_at>clock_timestamp()+interval '8 hours'
     or p_session_expires_at>v_batch.retention_expires_at then
    raise exception 'invalid_session_expiry';
  end if;
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

create or replace function public.get_daily_feedback_review_page_v1(p_session_hash text,p_public_ref uuid)
returns jsonb
language plpgsql security definer set search_path=''
as $$
declare
  v_session public.daily_feedback_sessions%rowtype;
  v_batch public.daily_feedback_batches%rowtype;
  v_binding public.daily_feedback_reviewer_bindings%rowtype;
  v_item public.daily_feedback_items%rowtype;
  v_done integer;
begin
  select * into v_session from public.daily_feedback_sessions where session_hash=p_session_hash for update;
  if not found or v_session.revoked_at is not null or v_session.expires_at<=clock_timestamp() then
    raise exception 'review_session_invalid';
  end if;
  select * into v_batch from public.daily_feedback_batches where public_ref=p_public_ref for update;
  if not found or v_session.batch_id<>v_batch.id or v_batch.state='purged'
     or v_batch.retention_expires_at<=clock_timestamp() then
    raise exception 'review_batch_unavailable';
  end if;
  if 4 <> (select count(*) from public.daily_feedback_batch_reviewer_bindings where batch_id=v_batch.id)
     or 4 <> (select count(*) from public.daily_feedback_batch_reviewer_bindings where batch_id=v_batch.id and deletion_accountable) then
    raise exception 'reviewer_set_incomplete';
  end if;
  select rb.* into v_binding
  from public.daily_feedback_reviewer_bindings rb
  join public.daily_feedback_batch_reviewer_bindings brb
    on brb.batch_id=v_batch.id and brb.reviewer_binding_id=rb.id
   and brb.reviewer_binding_generation=v_session.binding_generation
  where rb.id=v_session.reviewer_binding_id and rb.active
    and rb.binding_generation=v_session.binding_generation
    and rb.tenant_ref=v_batch.tenant_ref and rb.scope_ref=v_batch.scope_ref;
  if not found then raise exception 'reviewer_not_authorized'; end if;
  update public.daily_feedback_sessions set last_seen_at=clock_timestamp() where session_hash=p_session_hash;
  select count(*) into v_done from public.daily_feedback_decisions where batch_id=v_batch.id;
  select i.* into v_item from public.daily_feedback_items i
  where i.batch_id=v_batch.id
    and not exists(select 1 from public.daily_feedback_decisions d where d.item_id=i.id)
  order by i.item_index limit 1;
  if not found then
    return jsonb_build_object('status','complete','local_date',v_batch.local_date,
      'item_count',v_batch.item_count,'decided_count',v_done,
      'retention_expires_at',v_batch.retention_expires_at);
  end if;
  return jsonb_build_object('status','item','local_date',v_batch.local_date,
    'item_count',v_batch.item_count,'decided_count',v_done,
    'retention_expires_at',v_batch.retention_expires_at,'item',jsonb_build_object(
      'item_id',v_item.id,'position',v_item.item_index,'display_label',v_item.display_label,
      'apparent_objective',v_item.apparent_objective,'observed_outcome',v_item.observed_outcome,
      'release_id',v_item.release_id,'release_version',v_item.release_version,
      'messages',v_item.messages));
end;
$$;

create function public.purge_expired_daily_feedback_v2(
  p_now timestamptz,p_purge_actor_ref text,p_tenant_ref text,p_scope_ref text,
  p_limit integer default 20
) returns jsonb
language plpgsql security definer set search_path=''
as $$
declare
  v_batch public.daily_feedback_batches%rowtype;
  v_authoritative_now timestamptz;
  v_items integer;
  v_decisions integer;
  v_purged integer:=0;
  v_accountable_reviewer_refs text[];
  v_accountable_reviewers jsonb;
begin
  if p_now is null
     or p_purge_actor_ref !~ '^[a-zA-Z0-9._:-]{3,128}$'
     or p_tenant_ref !~ '^[a-z0-9][a-z0-9_-]{0,63}$'
     or p_scope_ref !~ '^[a-z0-9][a-z0-9_-]{0,127}$'
     or p_limit is null or p_limit not between 1 and 100 then
    raise exception 'invalid_purge_request';
  end if;
  v_authoritative_now:=clock_timestamp();
  perform set_config('app.daily_feedback_purge','on',true);
  for v_batch in select * from public.daily_feedback_batches b
    where b.tenant_ref=p_tenant_ref and b.scope_ref=p_scope_ref
      and b.state<>'purged' and b.retention_expires_at<=v_authoritative_now
    order by b.retention_expires_at,b.id for update skip locked limit p_limit
  loop
    select count(*) into v_items from public.daily_feedback_items where batch_id=v_batch.id;
    select count(*) into v_decisions from public.daily_feedback_decisions where batch_id=v_batch.id;
    select array_agg(reviewer_ref order by reviewer_ref),
           jsonb_agg(jsonb_build_object(
             'reviewer_ref',reviewer_ref,
             'reviewer_binding_id',reviewer_binding_id,
             'reviewer_binding_generation',reviewer_binding_generation,
             'oidc_issuer',oidc_issuer,
             'oidc_subject',oidc_subject,
             'slack_team_id',slack_team_id,
             'slack_user_id',slack_user_id
           ) order by reviewer_ref)
    into v_accountable_reviewer_refs,v_accountable_reviewers
    from public.daily_feedback_batch_reviewer_bindings
    where batch_id=v_batch.id and deletion_accountable;
    if coalesce(cardinality(v_accountable_reviewer_refs),0)=0 then
      raise exception 'deletion_accountable_reviewer_required';
    end if;
    insert into public.daily_feedback_purge_tombstones(
      batch_id,tenant_ref,scope_ref,local_date,package_fingerprint,
      purged_item_count,purged_decision_count,deletion_owner,
      accountable_reviewer_refs,purge_actor_ref,accountable_reviewers
    ) values (
      v_batch.id,v_batch.tenant_ref,v_batch.scope_ref,v_batch.local_date,
      v_batch.package_fingerprint,v_items,v_decisions,v_batch.deletion_policy_ref,
      v_accountable_reviewer_refs,p_purge_actor_ref,v_accountable_reviewers
    ) on conflict (batch_id) do nothing;
    delete from public.daily_feedback_decisions where batch_id=v_batch.id;
    delete from public.daily_feedback_items where batch_id=v_batch.id;
    delete from public.daily_feedback_sessions where batch_id=v_batch.id;
    delete from public.daily_feedback_oidc_states where batch_id=v_batch.id;
    delete from public.daily_feedback_batch_reviewer_bindings where batch_id=v_batch.id;
    update public.daily_feedback_batches
    set state='purged',
        notification_state=case when notification_state='admitted' then 'admitted' else 'retry' end,
        notification_lease_owner=null,notification_lease_expires_at=null,
        updated_at=clock_timestamp()
    where id=v_batch.id;
    v_purged:=v_purged+1;
  end loop;
  return jsonb_build_object('status','purged','count',v_purged);
end;
$$;

create or replace function public.purge_expired_daily_feedback_v1(
  p_now timestamptz,p_deletion_owner text,p_tenant_ref text,p_scope_ref text,
  p_limit integer default 20
) returns jsonb
language plpgsql security definer set search_path=''
as $$
begin
  raise exception 'daily_feedback_purge_v1_disabled';
end;
$$;

create or replace function public.get_daily_feedback_readiness_v1(
  p_tenant_ref text,p_scope_ref text,p_now timestamptz
) returns jsonb
language plpgsql stable security definer set search_path=''
as $$
declare v_delivery_unknown_count integer;
begin
  if p_tenant_ref !~ '^[a-z0-9][a-z0-9_-]{0,63}$'
     or p_scope_ref !~ '^[a-z0-9][a-z0-9_-]{0,127}$'
     or p_now is null then
    raise exception 'invalid_daily_feedback_readiness_scope';
  end if;
  select count(*)::integer into v_delivery_unknown_count
  from public.daily_feedback_batches b
  where b.tenant_ref=p_tenant_ref and b.scope_ref=p_scope_ref
    and b.state<>'purged' and b.notification_state='delivery_unknown';
  return jsonb_build_object(
    'status','ok','delivery_unknown_count',v_delivery_unknown_count,'checked_at',p_now
  );
end;
$$;

alter table public.daily_feedback_batch_reviewer_bindings enable row level security;
revoke all on table public.daily_feedback_batch_reviewer_bindings from public;

do $acl$
declare v_role text;
begin
  for v_role in select rolname from pg_roles where rolname in ('anon','authenticated','service_role') loop
    execute format('revoke all on table public.daily_feedback_batch_reviewer_bindings from %I',v_role);
  end loop;
end;
$acl$;

revoke all on function public.configure_daily_feedback_scope_v2(uuid,text,text,text,text,text,jsonb,text,bigint,bigint,bigint,text,time,integer,text,text,text,boolean) from public;
revoke all on function public.purge_expired_daily_feedback_v2(timestamptz,text,text,text,integer) from public;
revoke all on function public.get_daily_feedback_readiness_v1(text,text,timestamptz) from public;
revoke all on function public.daily_feedback_tombstone_immutable_guard() from public;
revoke execute on function public.configure_daily_feedback_scope_v1(uuid,text,text,text,text,text,text,text,text,bigint,bigint,bigint,text,time,integer,text,text,text,boolean) from public;
revoke execute on function public.purge_expired_daily_feedback_v1(timestamptz,text,text,text,integer) from public;

do $acl$
declare v_role text;
begin
  for v_role in select rolname from pg_roles where rolname in ('anon','authenticated','service_role') loop
    execute format('revoke all on function public.configure_daily_feedback_scope_v2(uuid,text,text,text,text,text,jsonb,text,bigint,bigint,bigint,text,time,integer,text,text,text,boolean) from %I',v_role);
    execute format('revoke all on function public.purge_expired_daily_feedback_v2(timestamptz,text,text,text,integer) from %I',v_role);
    execute format('revoke all on function public.get_daily_feedback_readiness_v1(text,text,timestamptz) from %I',v_role);
    execute format('revoke all on function public.daily_feedback_tombstone_immutable_guard() from %I',v_role);
    execute format('revoke execute on function public.configure_daily_feedback_scope_v1(uuid,text,text,text,text,text,text,text,text,bigint,bigint,bigint,text,time,integer,text,text,text,boolean) from %I',v_role);
    execute format('revoke execute on function public.purge_expired_daily_feedback_v1(timestamptz,text,text,text,integer) from %I',v_role);
  end loop;
  if exists(select 1 from pg_roles where rolname='service_role') then
    grant execute on function public.configure_daily_feedback_scope_v2(uuid,text,text,text,text,text,jsonb,text,bigint,bigint,bigint,text,time,integer,text,text,text,boolean) to service_role;
    grant execute on function public.purge_expired_daily_feedback_v2(timestamptz,text,text,text,integer) to service_role;
    grant execute on function public.get_daily_feedback_readiness_v1(text,text,timestamptz) to service_role;
  end if;
end;
$acl$;

commit;
