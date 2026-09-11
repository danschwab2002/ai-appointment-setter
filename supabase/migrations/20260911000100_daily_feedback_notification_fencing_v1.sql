-- Forward-only hardening for immutable Slack notification envelopes and lease fencing.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

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
    'retention_expires_at',v_batch.retention_expires_at,
    'lease_generation',v_batch.notification_lease_generation);
  return public.daily_feedback_finish_command(p_command_id,v_result);
end;
$$;

create or replace function public.retry_daily_feedback_notification_v1(
  p_command_id uuid,p_semantic_fingerprint text,p_worker_id text,p_batch_id uuid,p_lease_generation bigint,
  p_error_code text,p_retry_seconds integer default 60
) returns jsonb
language plpgsql security definer set search_path=''
as $$
declare v_prior jsonb; v_result jsonb; v_state text;
begin
  if p_worker_id !~ '^[a-zA-Z0-9._:-]{3,128}$'
     or p_batch_id is null
     or p_lease_generation is null or p_lease_generation < 1
     or p_error_code is null or p_error_code !~ '^[a-z][a-z0-9_]{2,63}$'
     or p_retry_seconds is null or p_retry_seconds not between 1 and 900 then
    raise exception 'invalid_notification_retry';
  end if;
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
    and notification_lease_expires_at>clock_timestamp()
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

create or replace function public.complete_daily_feedback_oidc_v1(
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

commit;
