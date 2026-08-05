begin;

create function public.record_resolved_identity_attempt()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    v_identity_contact_id uuid;
    v_identity_channel text;
    v_attempted_at timestamptz := clock_timestamp();
    v_attempt_count integer;
    v_last_attempt_at timestamptz;
begin
    select ci.contact_id, ci.channel
    into strict v_identity_contact_id, v_identity_channel
    from public.channel_identities ci
    where ci.id = new.selected_channel_identity_id;

    if v_identity_contact_id <> new.contact_id then
        raise exception using
            errcode = '23514',
            message = 'selected channel identity must belong to the recovery case contact';
    end if;

    if v_identity_channel <> 'whatsapp' then
        raise exception using
            errcode = '23514',
            message = 'resolved cart recovery identity must use whatsapp';
    end if;

    insert into public.identity_resolution_attempts (
        recovery_case_id,
        channel,
        strategy,
        status,
        matched_channel_identity_id,
        confidence,
        evidence,
        attempted_at
    ) values (
        new.id,
        'whatsapp',
        'other',
        'matched',
        new.selected_channel_identity_id,
        1.0,
        jsonb_build_object(
            'source', 'selected_channel_identity_transition'
        ),
        v_attempted_at
    );

    select count(*)::integer, max(ira.attempted_at)
    into v_attempt_count, v_last_attempt_at
    from public.identity_resolution_attempts ira
    where ira.recovery_case_id = new.id;

    new.identity_resolution_attempt_count := v_attempt_count;
    new.identity_resolution_last_attempt_at := v_last_attempt_at;
    new.identity_resolution_error := null;

    return new;
end;
$$;

revoke execute on function public.record_resolved_identity_attempt()
from public;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke execute on function public.record_resolved_identity_attempt()
        from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke execute on function public.record_resolved_identity_attempt()
        from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke execute on function public.record_resolved_identity_attempt()
        from service_role;
    end if;
end;
$$;

create trigger recovery_cases_record_resolved_identity_attempt
before update on public.recovery_cases
for each row
when (
    new.identity_resolution_status = 'resolved'
    and new.selected_channel_identity_id is not null
    and (
        old.identity_resolution_status is distinct from new.identity_resolution_status
        or old.selected_channel_identity_id is distinct from new.selected_channel_identity_id
    )
)
execute function public.record_resolved_identity_attempt();

insert into public.identity_resolution_attempts (
    recovery_case_id,
    channel,
    strategy,
    status,
    matched_channel_identity_id,
    confidence,
    evidence,
    attempted_at
)
select
    rc.id,
    'whatsapp',
    'other',
    'matched',
    rc.selected_channel_identity_id,
    1.0,
    jsonb_build_object(
        'source', 'selected_channel_identity_transition',
        'backfilled', true
    ),
    coalesce(rc.identity_resolution_last_attempt_at, rc.updated_at)
from public.recovery_cases rc
join public.channel_identities ci
    on ci.id = rc.selected_channel_identity_id
where rc.identity_resolution_status = 'resolved'
  and ci.channel = 'whatsapp'
  and not exists (
      select 1
      from public.identity_resolution_attempts ira
      where ira.recovery_case_id = rc.id
        and ira.status = 'matched'
        and ira.matched_channel_identity_id = rc.selected_channel_identity_id
  );

with attempt_stats as (
    select
        ira.recovery_case_id,
        count(*)::integer as attempt_count,
        max(ira.attempted_at) as last_attempt_at
    from public.identity_resolution_attempts ira
    group by ira.recovery_case_id
)
update public.recovery_cases rc
set identity_resolution_attempt_count = stats.attempt_count,
    identity_resolution_last_attempt_at = stats.last_attempt_at,
    identity_resolution_error = case
        when rc.identity_resolution_status = 'resolved' then null
        else rc.identity_resolution_error
    end
from attempt_stats stats
where stats.recovery_case_id = rc.id
  and (
      rc.identity_resolution_attempt_count is distinct from stats.attempt_count
      or rc.identity_resolution_last_attempt_at is distinct from stats.last_attempt_at
      or (
          rc.identity_resolution_status = 'resolved'
          and rc.identity_resolution_error is not null
      )
  );

commit;
