-- Temporary compressed-timing policy for the controlled final Hotmart E2E test.
-- Initial contact is due immediately; successor delays are relative to prior acceptance.
-- These timings are deliberately unrealistic and must not become client defaults.

begin;

do $timezone_preflight$
begin
    if not exists (
        select 1
        from pg_timezone_names
        where name = 'America/Argentina/Buenos_Aires'
    ) then
        raise exception using
            errcode = '22023',
            message = 'final_e2e_policy_timezone_not_available';
    end if;
end;
$timezone_preflight$;

insert into public.followup_policy_versions (
    policy_key,
    version,
    status,
    purpose,
    timezone,
    business_windows,
    grace_period,
    expires_after,
    max_automatic_messages,
    steps,
    approved_by,
    approved_at,
    published_at
) values (
    'cart-recovery-e2e-final', 1, 'published',
    'cart_recovery',
    'America/Argentina/Buenos_Aires',
    '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
    interval '0 seconds',
    interval '1 hour',
    4,
    '[{"step_key":"first_contact","mode":"freeform"},{"step_key":"followup_1","delay":"2 minutes","mode":"freeform"},{"step_key":"followup_2","delay":"5 minutes","mode":"freeform"},{"step_key":"followup_3","delay":"10 minutes","mode":"freeform"}]'::jsonb,
    'operator-e2e-final-policy',
    now(),
    now()
)
on conflict (policy_key, version) do nothing;

do $verify_policy$
declare
    v_policy public.followup_policy_versions%rowtype;
begin
    select * into strict v_policy
    from public.followup_policy_versions
    where policy_key = 'cart-recovery-e2e-final'
      and version = 1;

    if v_policy.status is distinct from 'published'
       or v_policy.purpose is distinct from 'cart_recovery'
       or v_policy.timezone is distinct from 'America/Argentina/Buenos_Aires'
       or v_policy.business_windows is distinct from
          '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb
       or v_policy.grace_period is distinct from interval '0 seconds'
       or v_policy.expires_after is distinct from interval '1 hour'
       or v_policy.max_automatic_messages is distinct from 4
       or v_policy.approved_by is distinct from 'operator-e2e-final-policy'
       or v_policy.steps is distinct from
          '[{"step_key":"first_contact","mode":"freeform"},{"step_key":"followup_1","delay":"2 minutes","mode":"freeform"},{"step_key":"followup_2","delay":"5 minutes","mode":"freeform"},{"step_key":"followup_3","delay":"10 minutes","mode":"freeform"}]'::jsonb then
        raise exception using
            errcode = '55000',
            message = 'final_e2e_policy_v1_mismatch';
    end if;
end;
$verify_policy$;

commit;
