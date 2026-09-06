import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
`);
for (const file of [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(join(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => join(root, 'supabase/migrations', name)),
]) {
  await db.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}

const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row`);
  return rows[0];
};
const reject = async (label, action) => {
  try {
    await action();
  } catch {
    return;
  }
  throw new Error(`${label} did not fail closed`);
};
const rejectRolledBack = async (label, action) => {
  await db.exec('begin');
  try {
    await reject(label, action);
  } finally {
    await db.exec('rollback');
  }
};
const EMAIL = 'payment-buyer@example.test';
const PHONE = '12025550124';
const CONTACT = '50000000-0000-4000-8000-000000000124';
const OTHER_CONTACT = '50000000-0000-4000-8000-000000000125';
const FAILED_AT = '2026-09-03T12:00:00Z';
const NOW = new Date().toISOString();
const payload = (id, overrides = {}) => ({
  id,
  creation_date: Date.parse(FAILED_AT),
  event: 'PURCHASE_CANCELED',
  version: '2.0.0',
  data: {
    buyer: {
      name: 'Payment Buyer',
      email: EMAIL,
      checkout_phone: '+1 (202) 555-0124',
    },
    product: { id: 123456, name: 'ATT1 Offer' },
    purchase: {
      transaction: 'ATT1-PAYMENT-FAIL-1',
      status: 'CANCELED',
      offer: { code: 'att1offer' },
      payment: { refusal_reason: 'insufficient_funds' },
    },
    checkout_country: { iso: 'MX', name: 'México' },
  },
  ...overrides,
});

await db.exec(`
  insert into public.commercial_ally_runtime_bindings
    (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
     lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
     product_name, product_price, currency, offer_code, consent_copy_version,
     hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
     inbound_scope_key, inbound_scope_version)
  values
    ('att1','att1-main',1,'active','att1','ATT1','att1-site','main',
     'att1.example','/offer','ATT1HOTLINK','ATT1 Offer',49,'USD','att1offer',
     'att1-whatsapp-v1',123456,42,24,'att1-inbound',1);

  insert into public.hotmart_purchase_intent_scopes
    (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
     offer_ref, max_lookback, active)
  values ('att1','att1-main','123456','ATT1HOTLINK','att1offer',
          interval '2 hours',true);

  insert into public.purchase_intents
    (tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
     normalized_email, normalized_phone, submitted_at, lifecycle_state,
     whatsapp_contact_authorized, provisional, provider_observed,
     activation_authorized)
  values ('att1','att1-main','main','ATT1HOTLINK','att1offer',
          '${EMAIL}','${PHONE}','2026-09-03T11:30:00Z','waiting_for_purchase',
          true,false,true,true);

  insert into public.followup_policy_versions
    (policy_key, version, status, purpose, timezone, business_windows,
     grace_period, expires_after, max_automatic_messages, steps,
     approved_by, approved_at, published_at)
  values
    ('att1-payment-failure',1,'published','cart_recovery','UTC',
     '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]',
     interval '0 seconds',interval '30 days',1,
     '[{"step_key":"payment_failure_first_contact","mode":"approved_template"}]',
     'operator-test',now(),now());

  insert into public.pilot_scope_versions
    (scope_key, version, status, tenant_key,
     chatwoot_account_id, chatwoot_inbox_id,
     channel, channel_provider, channel_account_ref,
     source, source_event_type, external_product_id, offer_code, purpose,
     policy_key, policy_version, timezone,
     max_cohort_contacts, max_outbound_request_starts_total,
     max_outbound_request_starts_per_day,
     approved_by, approved_at, published_at)
  values
    ('att1-payment-failure',1,'published','att1',42,24,
     'whatsapp','waba','123456','hotmart','PURCHASE_CANCELED',
     '123456','att1offer','cart_recovery','att1-payment-failure',1,'UTC',
     1,5,5,'operator-test',now(),now());
  insert into public.pilot_runtime_controls
    (scope_key,scope_version,runtime_state,generation,changed_by,change_reason)
  values ('att1-payment-failure',1,'inactive',0,'test','default-off');
`);

const admit = (body) => db.query(`
  select * from public.admit_portable_hotmart_payment_failure(
    'att1','att1-main',1,$1,$2::jsonb,$3,$4
  )
`, [body.id, JSON.stringify(body), EMAIL, PHONE]);

const firstPayload = payload('att1-payment-failure-exact');
const inserted = one((await admit(firstPayload)).rows, 'inserted admission');
const duplicate = one((await admit(firstPayload)).rows, 'duplicate admission');
const changed = structuredClone(firstPayload);
changed.data.purchase.payment.refusal_reason = 'do_not_honor';
const conflict = one((await admit(changed)).rows, 'conflicting admission');
if (inserted.outcome !== 'inserted'
    || duplicate.outcome !== 'duplicate'
    || conflict.outcome !== 'semantic_conflict') {
  throw new Error('payment failure admission idempotency diverged');
}

const intentState = one((await db.query(`
  select id,lifecycle_state,current_classification
  from public.purchase_intents where normalized_email=$1
`, [EMAIL])).rows, 'purchase intent');
const evidence = one((await db.query(`
  select transaction_ref,refusal_reason,correlation_outcome,purchase_intent_id
  from public.commercial_ally_payment_failure_details
  where webhook_event_id=$1
`, [inserted.webhook_event_id])).rows, 'payment failure evidence');
if (intentState.current_classification !== 'payment_failure_supported'
    || evidence.transaction_ref !== 'ATT1-PAYMENT-FAIL-1'
    || evidence.refusal_reason !== 'insufficient_funds'
    || evidence.correlation_outcome !== 'resolved'
    || evidence.purchase_intent_id !== intentState.id) {
  throw new Error('payment failure evidence/correlation diverged');
}

const dualPhonePayload = payload('att1-payment-failure-dual-phone');
dualPhonePayload.data.buyer.phone = '+1 (202) 555-0999';
dualPhonePayload.data.purchase.transaction = 'ATT1-PAYMENT-FAIL-2';
const dualPhoneAdmission = one(
  (await admit(dualPhonePayload)).rows,
  'dual-phone admission',
);
const dualPhoneEvidence = one((await db.query(`
  select correlation_outcome,purchase_intent_id
  from public.commercial_ally_payment_failure_details
  where webhook_event_id=$1
`, [dualPhoneAdmission.webhook_event_id])).rows, 'dual-phone evidence');
if (dualPhoneEvidence.correlation_outcome !== 'resolved'
    || dualPhoneEvidence.purchase_intent_id !== intentState.id) {
  throw new Error('payment failure phone precedence diverged');
}

await db.query(`
  insert into public.contacts (id,full_name,email,phone)
  values ($1,'Payment Buyer',$2,$3)
`, [CONTACT, EMAIL, PHONE]);
await db.query(`
  insert into public.contacts (id,full_name,email,phone)
  values ($1,'Other Buyer','other-buyer@example.test','12025550125')
`, [OTHER_CONTACT]);
await db.query(`
  insert into public.contact_points
    (contact_id,type,raw_value,normalized_value,source,source_event_id)
  values
    ($1,'email',$2,$2,'hotmart',$4),
    ($1,'phone',$3,$3,'hotmart',$4)
`, [CONTACT, EMAIL, PHONE, inserted.webhook_event_id]);
const contact = { contact_id: CONTACT };
await reject('inactive planning', () => db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,'123456','ATT1 Offer','att1offer',
    'att1-payment-failure',1,$3,42,24,'${PHONE}',
    'att1-payment-failure',1
  )
`, [inserted.webhook_event_id, contact.contact_id, FAILED_AT]));

await db.query(`select * from public.set_lancemos_pilot_runtime_state(
  'att1-payment-failure',1,0,'armed','operator-test','controlled-test'
)`);
await db.query(`select * from public.set_lancemos_pilot_cohort_member(
  'att1-payment-failure',1,$1,1,'active','operator-test','controlled-test'
)`, [contact.contact_id]);

await db.exec(`
  insert into public.pilot_scope_versions
    (scope_key, version, status, tenant_key,
     chatwoot_account_id, chatwoot_inbox_id,
     channel, channel_provider, channel_account_ref,
     source, source_event_type, external_product_id, offer_code, purpose,
     policy_key, policy_version, timezone,
     max_cohort_contacts, max_outbound_request_starts_total,
     max_outbound_request_starts_per_day,
     approved_by, approved_at, published_at)
  values
    ('other-tenant-payment-failure',1,'published','other-tenant',42,24,
     'whatsapp','waba','123456','hotmart','PURCHASE_CANCELED',
     '123456','att1offer','cart_recovery','att1-payment-failure',1,'UTC',
     1,5,5,'operator-test',now(),now());
  insert into public.pilot_scope_versions
    (scope_key, version, status, tenant_key,
     chatwoot_account_id, chatwoot_inbox_id,
     channel, channel_provider, channel_account_ref,
     source, source_event_type, external_product_id, offer_code, purpose,
     policy_key, policy_version, timezone,
     max_cohort_contacts, max_outbound_request_starts_total,
     max_outbound_request_starts_per_day,
     approved_by, approved_at, published_at)
  values
    ('other-contact-payment-failure',1,'published','att1',42,24,
     'whatsapp','waba','123456','hotmart','PURCHASE_CANCELED',
     '123456','att1offer','cart_recovery','att1-payment-failure',1,'UTC',
     1,5,5,'operator-test',now(),now());
  insert into public.pilot_runtime_controls
    (scope_key,scope_version,runtime_state,generation,changed_by,change_reason)
  values ('other-tenant-payment-failure',1,'inactive',0,'test','default-off');
  insert into public.pilot_runtime_controls
    (scope_key,scope_version,runtime_state,generation,changed_by,change_reason)
  values ('other-contact-payment-failure',1,'inactive',0,'test','default-off');
`);
await db.query(`select * from public.set_lancemos_pilot_runtime_state(
  'other-tenant-payment-failure',1,0,'armed','operator-test','controlled-test'
)`);
await db.query(`select * from public.set_lancemos_pilot_cohort_member(
  'other-tenant-payment-failure',1,$1,1,'active','operator-test','controlled-test'
)`, [contact.contact_id]);
await db.query(`select * from public.set_lancemos_pilot_runtime_state(
  'other-contact-payment-failure',1,0,'armed','operator-test','controlled-test'
)`);
await db.query(`select * from public.set_lancemos_pilot_cohort_member(
  'other-contact-payment-failure',1,$1,1,'active','operator-test','controlled-test'
)`, [OTHER_CONTACT]);

await rejectRolledBack('cross-tenant payment planning', () => db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,'123456','ATT1 Offer','att1offer',
    'att1-payment-failure',1,$3,42,24,'${PHONE}',
    'other-tenant-payment-failure',1
  )
`, [inserted.webhook_event_id, contact.contact_id, FAILED_AT]));
await rejectRolledBack('mismatched payment recipient', () => db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,'123456','ATT1 Offer','att1offer',
    'att1-payment-failure',1,$3,42,24,'12025550999',
    'att1-payment-failure',1
  )
`, [inserted.webhook_event_id, contact.contact_id, FAILED_AT]));
await rejectRolledBack('mismatched payment failure timestamp', () => db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,'123456','ATT1 Offer','att1offer',
    'att1-payment-failure',1,'2026-09-03T10:00:00Z',42,24,'${PHONE}',
    'att1-payment-failure',1
  )
`, [inserted.webhook_event_id, contact.contact_id]));
await rejectRolledBack('mismatched payment contact', () => db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,'123456','ATT1 Offer','att1offer',
    'att1-payment-failure',1,$3,42,24,'${PHONE}',
    'other-contact-payment-failure',1
  )
`, [inserted.webhook_event_id, OTHER_CONTACT, FAILED_AT]));

const planned = one((await db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,'123456','ATT1 Offer','att1offer',
    'att1-payment-failure',1,$3,42,24,'${PHONE}',
    'att1-payment-failure',1
  )
`, [inserted.webhook_event_id, contact.contact_id, FAILED_AT])).rows, 'payment plan');
if (!planned.created) throw new Error('payment failure plan was not created');
const repeatedPlan = one((await db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,'123456','ATT1 Offer','att1offer',
    'att1-payment-failure',1,$3,42,24,'${PHONE}',
    'att1-payment-failure',1
  )
`, [dualPhoneAdmission.webhook_event_id, contact.contact_id, FAILED_AT])).rows,
'repeated payment plan');
if (repeatedPlan.created
    || repeatedPlan.recovery_case_id !== planned.recovery_case_id) {
  throw new Error('repeated payment failure did not aggregate into the active case');
}
const eventCount = one((await db.query(`
  select count(*)::int as count
  from public.recovery_case_events
  where recovery_case_id=$1 and event_role='payment_failure'
`, [planned.recovery_case_id])).rows, 'aggregated payment event count');
if (Number(eventCount.count) !== 2) {
  throw new Error('repeated payment failure evidence was not attached to the case');
}
const implicitAuthorization = one((await db.query(`
  select count(*)::int as count
  from public.contact_authorizations
  where contact_id=$1
    and channel='whatsapp'
    and purpose='cart_recovery'
    and authorization_status='allowed'
`, [contact.contact_id])).rows, 'implicit payment authorization count');
const implicitAuthorizationCount = Number(implicitAuthorization.count);
if (implicitAuthorizationCount !== 0) {
  throw new Error('payment failure event granted contact authorization implicitly');
}
const action = one((await db.query(`
  select anchor_type,step_key,status from public.scheduled_actions where id=$1
`, [planned.scheduled_action_id])).rows, 'payment action');
if (action.anchor_type !== 'payment_failure'
    || action.step_key !== 'payment_failure_first_contact'
    || action.status !== 'pending') {
  throw new Error('payment failure action identity diverged');
}

await db.query(`
  insert into public.contact_authorizations (
    contact_id, channel, purpose, authorization_status,
    authorization_source, evidence, valid_from
  ) values (
    $1, 'whatsapp', 'cart_recovery', 'allowed', 'manual',
    jsonb_build_object('source', 'precheckout_form_consent_fixture'),
    $2::timestamptz - interval '1 minute'
  )
`, [contact.contact_id, NOW]);

const claimed = one((await db.query(`
  select * from public.claim_due_followup_actions(
    'payment-worker',$1,interval '5 minutes',1
  )
`, [NOW])).rows, 'claimed payment action');
await db.query(`
  insert into public.conversation_events
    (recovery_case_id,event_type,actor_type,related_action_id,data)
  values ($1,'followup_action_reevaluated','system',$2,
    jsonb_build_object(
      'decision','execute','reason_code','eligible_for_execution',
      'worker_id','payment-worker','lease_generation',$3::bigint,
      'case_version',$4::bigint,'sequence_revision',1::bigint))
`, [claimed.recovery_case_id, claimed.id,
  claimed.lease_generation, claimed.expected_case_version]);
const attempt = one((await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1,'payment-worker',$2,$3,1,'whatsapp','approved_template',
    $4
  )
`, [claimed.id, claimed.lease_generation,
  claimed.expected_case_version, NOW])).rows, 'reserved payment attempt');
await db.exec('begin');
const started = one((await db.query(`
  select * from public.mark_portable_payment_failure_request_started(
    $1,$2,'payment-worker',$3,$4
  )
`, [claimed.id, attempt.id, claimed.lease_generation, NOW])).rows, 'request start');
if (started.phase !== 'request_started') {
  throw new Error('payment request-start gate did not authorize exact action');
}
await db.exec('rollback');

await db.query(`
  update public.scheduled_actions
  set status='accepted_by_chatwoot'
  where id=$1
`, [planned.scheduled_action_id]);

const recoveryIdentity = one((await db.query(`
  select rc.contact_id, rc.selected_channel_identity_id,
         identity.external_user_id, identity.account_id,
         identity.channel, identity.identity_status,
         fs.id as initial_sequence_id
  from public.recovery_cases rc
  join public.followup_sequences fs on fs.recovery_case_id=rc.id
  join public.channel_identities identity
    on identity.id=rc.selected_channel_identity_id
  where rc.id=$1
`, [planned.recovery_case_id])).rows, 'payment recovery identity');
const conversation = one((await db.query(`
  insert into public.conversations (
    contact_id, channel_identity_id, status, automation_status,
    commercial_context
  ) values (
    $1,$2,'active','enabled',
    jsonb_build_object('chatwoot_conversation_id','9001')
  ) returning id
`, [recoveryIdentity.contact_id,
  recoveryIdentity.selected_channel_identity_id])).rows, 'payment conversation');
await db.query(`
  update public.channel_identities
  set external_conversation_id='9001'
  where id=$1
`, [recoveryIdentity.selected_channel_identity_id]);
const initialMessage = one((await db.query(`
  insert into public.messages (
    conversation_id, external_message_id, direction, actor_type,
    message_type, content, delivery_status, semantic_metadata,
    occurred_at, delivered_at
  ) values (
    $1,'8001','outbound','ai_agent','followup','[template]',
    'accepted',jsonb_build_object('action_id',$2::text),
    '2026-09-03T12:01:00Z','2026-09-03T12:01:00Z'
  ) returning id
`, [conversation.id, planned.scheduled_action_id])).rows,
'initial accepted message');
await db.query(`
  update public.scheduled_actions
  set conversation_id=$2
  where id=$1
`, [planned.scheduled_action_id, conversation.id]);
await db.query(`
  update public.followup_sequences
  set status='completed', completed_at='2026-09-03T12:01:00Z',
      conversation_id=$2, current_step=1
  where id=$1
`, [recoveryIdentity.initial_sequence_id, conversation.id]);
await db.query(`
  update public.recovery_cases
  set conversation_id=$2, status='sequence_exhausted',
      closed_at='2026-09-03T12:01:00Z', version=version+1
  where id=$1
`, [planned.recovery_case_id, conversation.id]);

await db.exec(`
  insert into public.commercial_ally_discount_policy_versions (
    tenant_ref, funnel_ref, binding_version, policy_key, policy_version,
    trigger_kind, discount_kind, discount_value, coupon_reference,
    offer_valid_for, offer_expiration_mode, presentation_stage,
    template_key, copy_version, release_requires_exact_trigger_set,
    requires_inbound_reply_after_initial_template, coupon_delivery_mode,
    urgency_copy_allowed, channel_provider, delivery_mode,
    template_language, template_category,
    coupon_template_component, coupon_template_parameter_index,
    valid_from
  ) values
    ('att1','att1-main',1,'att1-recovery-triplet',1,
     'payment_failure','percentage',10,'meta-variable',
     null,'indefinite','later_step','att1_discount_later','att1-discount-v1',
     true,true,'meta_template_variable',false,'waba','approved_template',
     'es_MX','marketing','body',1,statement_timestamp()-interval '1 hour'),
    ('att1','att1-main',1,'att1-recovery-triplet',1,
     'confirmed_cart_abandonment','percentage',10,'meta-variable',
     null,'indefinite','later_step','att1_discount_later','att1-discount-v1',
     true,true,'meta_template_variable',false,'waba','approved_template',
     'es_MX','marketing','body',1,statement_timestamp()-interval '1 hour'),
    ('att1','att1-main',1,'att1-recovery-triplet',1,
     'precheckout_without_purchase_signal','percentage',10,'meta-variable',
     null,'indefinite','later_step','att1_discount_later','att1-discount-v1',
     true,true,'meta_template_variable',false,'waba','approved_template',
     'es_MX','marketing','body',1,statement_timestamp()-interval '1 hour');
  update public.commercial_ally_discount_policy_versions
  set status='approved', approved_by='operator-test',
      approved_at=statement_timestamp()
  where policy_key='att1-recovery-triplet' and policy_version=1;
  update public.commercial_ally_discount_policy_versions
  set status='published', published_at=statement_timestamp()
  where policy_key='att1-recovery-triplet' and policy_version=1;
`);

const noSilenceAction = one((await db.query(`
  select count(*)::int as count
  from public.scheduled_actions
  where recovery_case_id=$1 and step_key='payment_failure_discount_offer'
`, [planned.recovery_case_id])).rows, 'silence action count');
if (Number(noSilenceAction.count) !== 0) {
  throw new Error('silence created a post-inbound discount action');
}

// A second runtime sharing account/inbox must not claim ATT1's recovery case.
await db.exec(`
  insert into public.commercial_ally_runtime_bindings
    (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
     lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
     product_name, product_price, currency, offer_code, consent_copy_version,
     hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
     inbound_scope_key, inbound_scope_version)
  values
    ('foreign','foreign-main',1,'active','foreign','Foreign','foreign-site','main',
     'foreign.example','/offer','FOREIGNHOTLINK','Foreign Offer',49,'USD','foreignoffer',
     'foreign-whatsapp-v1',654321,42,24,'foreign-inbound',1);

  insert into public.commercial_ally_discount_policy_versions
    (tenant_ref, funnel_ref, binding_version, policy_key, policy_version,
     trigger_kind, status, discount_kind, discount_value, currency,
     coupon_reference, offer_expiration_mode, offer_valid_for,
     presentation_stage, template_key, copy_version,
     requires_inbound_reply_after_initial_template,
     coupon_delivery_mode, urgency_copy_allowed, channel_provider,
     delivery_mode, template_language, template_category,
     coupon_template_component, coupon_template_parameter_index,
     release_requires_exact_trigger_set, approved_by, approved_at,
     published_at, valid_from)
  values
    ('foreign','foreign-main',1,'foreign-discount',1,
     'payment_failure','draft','percentage',10,null,
     'FOREIGN10','indefinite',null,
     'later_step','foreign_discount_template','foreign-copy-v1',true,
     'meta_template_variable',false,'waba','approved_template','es_MX',
     'marketing','body',1,false,null,null,null,now()-interval '1 hour');

  update public.commercial_ally_discount_policy_versions
  set status='approved', approved_by='operator-test', approved_at=now()
  where tenant_ref='foreign' and funnel_ref='foreign-main'
    and binding_version=1 and policy_key='foreign-discount';

  update public.commercial_ally_discount_policy_versions
  set status='published', published_at=now()
  where tenant_ref='foreign' and funnel_ref='foreign-main'
    and binding_version=1 and policy_key='foreign-discount';
`);

const foreignPlan = await db.query(
  `select * from public.plan_commercial_ally_post_inbound_discount(
     'foreign','foreign-main',1,'foreign-discount',1,
     42,24,9001,9002,$1,now()
   )`,
  [recoveryIdentity.external_user_id],
);
if (foreignPlan.rows[0]?.outcome !== 'runtime_not_applicable') {
  throw new Error(`cross-tenant case fence failed: ${JSON.stringify(foreignPlan.rows)}`);
}

await db.query(
  `update public.channel_identities
   set metadata=jsonb_set(metadata,'{inbox_id}','999'::jsonb)
   where id=$1`,
  [recoveryIdentity.selected_channel_identity_id],
);
const wrongInboxPlan = await db.query(
  `select * from public.plan_commercial_ally_post_inbound_discount(
     'att1','att1-main',1,'att1-recovery-triplet',1,
     42,24,9001,9002,$1,now()
   )`,
  [recoveryIdentity.external_user_id],
);
if (wrongInboxPlan.rows[0]?.outcome !== 'identity_not_applicable') {
  throw new Error(`identity inbox fence failed: ${JSON.stringify(wrongInboxPlan.rows)}`);
}
await db.query(
  `update public.channel_identities
   set metadata=jsonb_set(metadata,'{inbox_id}','24'::jsonb)
   where id=$1`,
  [recoveryIdentity.selected_channel_identity_id],
);

let infiniteTimestampRejected = false;
try {
  await db.query(
    `select * from public.plan_commercial_ally_post_inbound_discount(
       'att1','att1-main',1,'att1-recovery-triplet',1,
       42,24,9001,9002,$1,'infinity'::timestamptz
     )`,
    [recoveryIdentity.external_user_id],
  );
} catch (error) {
  infiniteTimestampRejected = String(error).includes(
    'commercial_ally_post_inbound_discount_invalid',
  );
}
if (!infiniteTimestampRejected) {
  throw new Error('infinite inbound timestamp was not rejected at the RPC boundary');
}

const inboundPlan = async (messageId) => db.query(`
  select * from public.plan_commercial_ally_post_inbound_discount(
    'att1','att1-main',1,'att1-recovery-triplet',1,
    42,24,9001,$1,$2,$3
  )
`, [messageId, recoveryIdentity.external_user_id, NOW]);
const inboundPlanned = one((await inboundPlan(9002)).rows,
  'post-inbound discount plan');
if (inboundPlanned.outcome !== 'created'
    || inboundPlanned.recovery_case_id !== planned.recovery_case_id) {
  throw new Error(`post-inbound plan was not created exactly: ${JSON.stringify({inboundPlanned, recoveryIdentity})}`);
}
const inboundReplay = one((await inboundPlan(9002)).rows,
  'post-inbound discount replay');
const secondInbound = one((await inboundPlan(9003)).rows,
  'second post-inbound message');
if (inboundReplay.outcome !== 'already_exists'
    || secondInbound.outcome !== 'already_exists'
    || inboundReplay.scheduled_action_id !== inboundPlanned.scheduled_action_id
    || secondInbound.scheduled_action_id !== inboundPlanned.scheduled_action_id) {
  throw new Error('post-inbound planning was not idempotent per recovery case');
}

const replayAfterMutation = async (label, mutation, params, messageId) => {
  await db.exec('begin');
  await db.query(mutation, params);
  const replay = one((await inboundPlan(messageId)).rows, label);
  await db.exec('rollback');
  if (replay.outcome !== 'already_exists'
      || replay.scheduled_action_id !== inboundPlanned.scheduled_action_id
      || replay.inbound_message_id !== inboundPlanned.inbound_message_id) {
    throw new Error(`${label} did not resolve the immutable action`);
  }
};
await replayAfterMutation(
  'replay after runtime retirement',
  `update public.commercial_ally_runtime_bindings
   set status='retired'
   where tenant_ref='att1' and funnel_ref='att1-main' and binding_version=1`,
  [],
  9010,
);
await replayAfterMutation(
  'replay after policy retirement',
  `update public.commercial_ally_discount_policy_versions
   set status='retired'
   where tenant_ref='att1' and funnel_ref='att1-main'
     and binding_version=1 and policy_key='att1-recovery-triplet'
     and policy_version=1 and trigger_kind='payment_failure'`,
  [],
  9011,
);
await replayAfterMutation(
  'replay after human takeover',
  `update public.conversations set human_takeover=true where id=$1`,
  [conversation.id],
  9012,
);
await replayAfterMutation(
  'replay after automation disablement',
  `update public.conversations set automation_status='disabled' where id=$1`,
  [conversation.id],
  9015,
);
await replayAfterMutation(
  'replay after terminal case transition',
  `update public.recovery_cases
   set status='won', won_at=now(), closed_at=now()
   where id=$1`,
  [planned.recovery_case_id],
  9013,
);

await db.exec('begin');
await db.exec(`
  update public.commercial_ally_discount_policy_versions
  set status='retired'
  where tenant_ref='att1' and funnel_ref='att1-main'
    and binding_version=1 and policy_key='att1-recovery-triplet'
    and policy_version=1 and trigger_kind='payment_failure';
  insert into public.commercial_ally_discount_policy_versions (
    tenant_ref, funnel_ref, binding_version, policy_key, policy_version,
    trigger_kind, discount_kind, discount_value, coupon_reference,
    offer_valid_for, offer_expiration_mode, presentation_stage,
    template_key, copy_version, release_requires_exact_trigger_set,
    requires_inbound_reply_after_initial_template, coupon_delivery_mode,
    urgency_copy_allowed, channel_provider, delivery_mode,
    template_language, template_category,
    coupon_template_component, coupon_template_parameter_index,
    valid_from
  ) values (
    'att1','att1-main',1,'att1-recovery-alternate',1,
    'payment_failure','percentage',10,'meta-variable-alternate',
    null,'indefinite','later_step','att1_discount_alternate',
    'att1-discount-alternate-v1',true,true,'meta_template_variable',false,
    'waba','approved_template','es_MX','marketing','body',1,
    statement_timestamp()-interval '1 hour'
  );
  update public.commercial_ally_discount_policy_versions
  set status='approved', approved_by='operator-test',
      approved_at=statement_timestamp()
  where tenant_ref='att1' and funnel_ref='att1-main'
    and binding_version=1 and policy_key='att1-recovery-alternate'
    and policy_version=1 and trigger_kind='payment_failure';
  update public.commercial_ally_discount_policy_versions
  set status='published', published_at=statement_timestamp()
  where tenant_ref='att1' and funnel_ref='att1-main'
    and binding_version=1 and policy_key='att1-recovery-alternate'
    and policy_version=1 and trigger_kind='payment_failure';
`);
const crossPolicyReplay = one((await db.query(`
  select * from public.plan_commercial_ally_post_inbound_discount(
    'att1','att1-main',1,'att1-recovery-alternate',1,
    42,24,9001,9014,$1,$2
  )
`, [recoveryIdentity.external_user_id, NOW])).rows, 'cross-policy replay');
await db.exec('rollback');
if (crossPolicyReplay.outcome !== 'recovery_case_not_applicable'
    || crossPolicyReplay.scheduled_action_id !== null
    || crossPolicyReplay.inbound_message_id !== null) {
  throw new Error('cross-policy replay exposed the immutable original action');
}
const discountedAction = one((await db.query(`
  select sa.action_type, sa.status, sa.step_key, sa.due_at,
         binding.discount_value, binding.offer_expiration_mode,
         binding.presentation_stage, binding.coupon_delivery_mode,
         binding.urgency_copy_allowed, binding.inbound_external_message_id
  from public.scheduled_actions sa
  join public.commercial_ally_post_inbound_discount_bindings binding
    on binding.scheduled_action_id=sa.id
  where sa.id=$1
`, [inboundPlanned.scheduled_action_id])).rows, 'discounted action');
if (discountedAction.action_type !== 'inbound_reply_offer'
    || discountedAction.status !== 'deferred'
    || discountedAction.step_key !== 'payment_failure_discount_offer'
    || Number(discountedAction.discount_value) !== 10
    || discountedAction.offer_expiration_mode !== 'indefinite'
    || discountedAction.presentation_stage !== 'later_step'
    || discountedAction.coupon_delivery_mode !== 'meta_template_variable'
    || discountedAction.urgency_copy_allowed !== false
    || Number(discountedAction.inbound_external_message_id) !== 9002) {
  throw new Error(`post-inbound action contract drifted: ${JSON.stringify(discountedAction)}`);
}
const onlyOneDiscount = one((await db.query(`
  select count(*)::int as count
  from public.commercial_ally_post_inbound_discount_bindings
  where recovery_case_id=$1
`, [planned.recovery_case_id])).rows, 'discount action count');
if (Number(onlyOneDiscount.count) !== 1) {
  throw new Error('more than one post-inbound discount action exists');
}
const prematurelyClaimed = (await db.query(`
  select id from public.claim_due_followup_actions(
    'discount-worker',$1,interval '5 minutes',100
  ) where id=$2
`, [NOW, inboundPlanned.scheduled_action_id])).rows;
if (prematurelyClaimed.length !== 0) {
  throw new Error('deferred discount action became claimable before activation');
}
await reject('post-inbound discount binding update', () => db.query(`
  update public.commercial_ally_post_inbound_discount_bindings
  set coupon_reference='changed' where recovery_case_id=$1
`, [planned.recovery_case_id]));

const terminalPayload = payload('att1-payment-failure-after-first-contact');
terminalPayload.data.purchase.transaction = 'ATT1-PAYMENT-FAIL-3';
const terminalAdmission = one(
  (await admit(terminalPayload)).rows,
  'post-contact payment admission',
);
const postContactPlan = one((await db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,'123456','ATT1 Offer','att1offer',
    'att1-payment-failure',1,$3,42,24,'${PHONE}',
    'att1-payment-failure',1
  )
`, [terminalAdmission.webhook_event_id, contact.contact_id, FAILED_AT])).rows,
'post-contact payment plan');
const initialContactCount = one((await db.query(`
  select count(*)::int as count
  from public.scheduled_actions
  where recovery_case_id=$1
    and step_key='payment_failure_first_contact'
`, [planned.recovery_case_id])).rows, 'initial contact count');
if (postContactPlan.created
    || postContactPlan.recovery_case_id !== planned.recovery_case_id
    || Number(initialContactCount.count) !== 1) {
  throw new Error('payment failure planned more than one initial contact');
}

await reject('payment evidence update', () => db.query(`
  update public.commercial_ally_payment_failure_details
  set transaction_ref='changed' where webhook_event_id=$1
`, [inserted.webhook_event_id]));
await reject('payment provenance delete', () => db.query(`
  delete from public.commercial_ally_hotmart_event_bindings
  where webhook_event_id=$1
`, [inserted.webhook_event_id]));

console.log('commercial_ally_payment_failure_recovery=OK');
