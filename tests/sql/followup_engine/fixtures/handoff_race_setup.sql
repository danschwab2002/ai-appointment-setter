\set ON_ERROR_STOP on
insert into public.followup_policy_versions (
  policy_key, version, status, purpose, timezone, business_windows,
  grace_period, expires_after, max_automatic_messages, steps,
  approved_by, approved_at, published_at
) values (
  'handoff-race-policy', 1, 'published', 'cart_recovery', 'UTC',
  '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]',
  interval '0 seconds', interval '30 days', 1,
  '[{"step_key":"first_contact","mode":"approved_template"}]',
  'test', now(), now()
);
insert into public.pilot_scope_versions (
  scope_key, version, status, tenant_key,
  chatwoot_account_id, chatwoot_inbox_id,
  channel, channel_provider, channel_account_ref,
  source, source_event_type, external_product_id, offer_code, purpose,
  policy_key, policy_version, timezone,
  max_cohort_contacts, max_outbound_request_starts_total,
  max_outbound_request_starts_per_day,
  approved_by, approved_at, published_at
) values (
  'handoff-race-scope', 1, 'published', 'lancemos',
  10, 20, 'whatsapp', 'waba', 'race-account',
  'hotmart', 'PURCHASE_OUT_OF_SHOPPING_CART', '3526906', 'race-offer',
  'cart_recovery', 'handoff-race-policy', 1, 'UTC',
  1, 5, 5, 'test', now(), now()
);
insert into public.human_handoff_projection_policies (
  policy_key, policy_version, scope_key, scope_version, active,
  expected_team_id, note_template_key, note_template_version, private_note_body
) values (
  'handoff-race-projection', 1, 'handoff-race-scope', 1, true,
  77, 'race-note', 1, 'Race handoff note'
);
insert into public.pilot_runtime_controls (
  scope_key, scope_version, runtime_state, generation, changed_by, change_reason
) values ('handoff-race-scope', 1, 'inactive', 0, 'test', 'setup');
select * from public.admit_hotmart_cart_abandonment(
  'handoff-race-event',
  jsonb_build_object(
    'id','handoff-race-event',
    'creation_date',(extract(epoch from now() - interval '1 minute') * 1000)::bigint,
    'event','PURCHASE_OUT_OF_SHOPPING_CART','version','2.0.0',
    'data',jsonb_build_object(
      'buyer',jsonb_build_object(
        'email','handoff-race@example.com','phone','5491100000200'
      ),
      'product',jsonb_build_object('id',3526906,'name','Product One'),
      'offer',jsonb_build_object('code','race-offer')
    )
  )
);
insert into public.contacts (id, full_name, email, phone)
values (
  '73000000-0000-0000-0000-000000000002', 'Handoff Race',
  'handoff-race@example.com', '5491100000200'
);
insert into public.contact_points (
  contact_id, type, raw_value, normalized_value, source, source_event_id
) values
  (
    '73000000-0000-0000-0000-000000000002','email',
    'handoff-race@example.com','handoff-race@example.com','hotmart',
    (select id from public.webhook_events
     where external_event_id='handoff-race-event')
  ),
  (
    '73000000-0000-0000-0000-000000000002','phone',
    '5491100000200','5491100000200','hotmart',
    (select id from public.webhook_events
     where external_event_id='handoff-race-event')
  );
select * from public.set_lancemos_pilot_runtime_state(
  'handoff-race-scope',1,0,'armed','test','race setup'
);
select * from public.set_lancemos_pilot_cohort_member(
  'handoff-race-scope',1,'73000000-0000-0000-0000-000000000002',1,
  'active','test','race setup'
);
select * from public.plan_lancemos_pilot_cart_recovery(
  (select id from public.webhook_events
   where external_event_id='handoff-race-event'),
  '73000000-0000-0000-0000-000000000002',
  '3526906','Product One','race-offer','handoff-race-policy',1,
  (select to_timestamp((payload->>'creation_date')::bigint / 1000.0)
   from public.webhook_events where external_event_id='handoff-race-event'),
  10,20,'5491100000200','handoff-race-scope',1
);
insert into public.conversations (
  id, contact_id, channel_identity_id, status, commercial_context
)
select
  '73000000-0000-0000-0000-000000000003',
  '73000000-0000-0000-0000-000000000002',
  selected_channel_identity_id, 'active',
  jsonb_build_object('chatwoot_conversation_id','9001')
from public.recovery_cases
where contact_id = '73000000-0000-0000-0000-000000000002';
update public.recovery_cases
set conversation_id = '73000000-0000-0000-0000-000000000003'
where contact_id = '73000000-0000-0000-0000-000000000002';
select * from public.claim_due_followup_actions(
  'handoff-race-worker', now() + interval '1 minute', interval '5 minutes', 1
);
insert into public.conversation_events (
  recovery_case_id, event_type, actor_type, related_action_id, data
)
select
  action.recovery_case_id, 'followup_action_reevaluated', 'system', action.id,
  jsonb_build_object(
    'decision','execute','reason_code','eligible_for_execution',
    'worker_id','handoff-race-worker',
    'lease_generation',action.lease_generation,
    'case_version',action.expected_case_version,'sequence_revision',1
  )
from public.scheduled_actions action
join public.recovery_cases cases on cases.id = action.recovery_case_id
where cases.contact_id = '73000000-0000-0000-0000-000000000002';
select * from public.reserve_followup_delivery_attempt(
  (select action.id from public.scheduled_actions action
   join public.recovery_cases cases on cases.id=action.recovery_case_id
   where cases.contact_id='73000000-0000-0000-0000-000000000002'),
  'handoff-race-worker',
  (select action.lease_generation from public.scheduled_actions action
   join public.recovery_cases cases on cases.id=action.recovery_case_id
   where cases.contact_id='73000000-0000-0000-0000-000000000002'),
  (select action.expected_case_version from public.scheduled_actions action
   join public.recovery_cases cases on cases.id=action.recovery_case_id
   where cases.contact_id='73000000-0000-0000-0000-000000000002'),
  1,'whatsapp','approved_template',now() + interval '1 minute'
);
