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
