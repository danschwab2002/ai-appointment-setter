-- Real PostgreSQL probe: cart-abandonment auto-grant makes the authoritative
-- reevaluation reach `execute` for a first_contact action, closing the
-- contact_authorization_unknown gap observed in production.
\set ON_ERROR_STOP on
begin;

insert into public.webhook_events (id, source, external_event_id, event_type, payload)
values ('00000000-0000-0000-0000-0000000000f1', 'hotmart', 'authz-execute-probe',
        'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb);
insert into public.contacts (id, full_name)
values ('00000000-0000-0000-0000-0000000000f2', 'Authz Execute Probe');

select recovery_case_id, scheduled_action_id
from public.plan_cart_recovery_with_identity(
    '00000000-0000-0000-0000-0000000000f1',
    '00000000-0000-0000-0000-0000000000f2',
    'authz-product', 'Authz Product', 'authz-offer',
    'cart-recovery-test', 1, now() - interval '2 minutes',
    1, 7, '5531977777777'
) \gset

-- The plan must have granted exactly one active allowed authorization.
select 'grant_rows=' || count(*)::text as grant_check
from public.contact_authorizations
where contact_id='00000000-0000-0000-0000-0000000000f2'
  and channel='whatsapp' and purpose='cart_recovery'
  and authorization_status='allowed' and authorization_source='hotmart';

-- Make the action due, then claim + reevaluate as the dispatcher would.
update public.scheduled_actions set due_at = now() - interval '1 minute'
where id = :'scheduled_action_id';

-- Use clock_timestamp() to reflect real elapsed wall-clock time. The grant's
-- valid_from is clock_timestamp() at planning; within one transaction now()
-- equals transaction start and would predate that grant. In production the
-- dispatcher reevaluates in a later transaction, minutes after planning, so
-- the grant's valid_from is always in the past relative to its evaluation now.
select id as claimed_action_id, lease_generation as claimed_lease
from public.claim_due_followup_actions('probe-worker', clock_timestamp(), interval '5 minutes', 1)
\gset

select 'decision=' || decision || ' reason=' || coalesce(reason_code, 'null') as reevaluation
from public.reevaluate_followup_action(:'claimed_action_id', 'probe-worker', :claimed_lease, clock_timestamp());

rollback;
