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

// ---------------------------------------------------------------------------
// 20260930000100 (A5): el permiso de contacto del pago fallido lo concede la
// intencion con consentimiento con la que el evento quedo correlacionado, y la
// reevaluacion REAL (no un followup_action_reevaluated insertado a mano) da
// execute. Arriba, la intencion de este archivo se inserta sin formulario:
// por eso ese caso no recibio permiso.
//
// Politica y scope como los de ATT1 v1 (decision D2): pasos freeform y salida
// por plantilla (scope waba). Los tiempos salen del reloj de la base: el
// permiso nace con clock_timestamp() y la puerta de arranque exige +-5 minutos.
//
// Datos: no hay PURCHASE_CANCELED ni lead.precheckout capturados (deuda de
// A0). El pago fallido usa el precedente inline de este archivo (payload()) y
// el formulario el de validate_commercial_ally_portable_precheckout.mjs, con
// los valores del binding de arriba.
// ---------------------------------------------------------------------------
const CONSENT_SCOPE = 'att1-consented-failure';
const dbNow = async () => new Date(
  (await db.query('select clock_timestamp() as now')).rows[0].now,
);
const baseMs = Math.floor((await dbNow()).getTime() / 1000) * 1000;
const at = (minutes) => new Date(baseMs + minutes * 60_000);
const SUBMITTED_AT = at(-40);
const CONSENT_FAILED_AT = at(-10);

await db.exec(`
  insert into public.followup_policy_versions
    (policy_key, version, status, purpose, timezone, business_windows,
     grace_period, expires_after, max_automatic_messages, steps,
     approved_by, approved_at, published_at)
  values
    ('${CONSENT_SCOPE}',1,'published','cart_recovery','UTC',
     '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]',
     interval '0 seconds',interval '1 day',1,
     '[{"step_key":"first_contact","mode":"freeform"},
       {"step_key":"payment_failure_first_contact","mode":"freeform"}]',
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
    ('${CONSENT_SCOPE}',1,'published','att1',42,24,
     'whatsapp','waba','123456','hotmart','PURCHASE_CANCELED',
     '123456','att1offer','cart_recovery','${CONSENT_SCOPE}',1,'UTC',
     5,5,5,'operator-test',now(),now());
  insert into public.pilot_runtime_controls
    (scope_key,scope_version,runtime_state,generation,changed_by,change_reason)
  values ('${CONSENT_SCOPE}',1,'inactive',0,'test','default-off');
`);
await db.query(`select * from public.set_lancemos_pilot_runtime_state(
  '${CONSENT_SCOPE}',1,0,'armed','operator-test','controlled-test'
)`);

const precheckout = (id, lead, { consented = true, submittedAt = SUBMITTED_AT } = {}) => {
  const national = lead.phone.slice(1);
  const version = consented ? '1.1.0' : '1.0.0';
  const raw = {
    id,
    event: 'lead.precheckout',
    version,
    created_at: submittedAt.toISOString(),
    source: {
      system: 'landing', site: 'att1-site', aliado: 'ATT1',
      landing_id: 'main', page_url: 'https://att1.example/offer',
    },
    data: {
      buyer: {
        name: lead.name, email: lead.email, phone: `+${lead.phone}`,
        phone_country_code: '1', phone_national: national,
      },
      product: {
        hotlink: 'ATT1HOTLINK', id: null, name: 'ATT1 Offer', price: 49,
        currency: 'USD',
      },
      offer: { code: 'att1offer' },
      checkout_url: 'https://pay.hotmart.com/ATT1HOTLINK?off=att1offer&checkoutMode=10',
      checkout_country: { iso: 'US', source: 'phone_country_code' },
      consent: consented
        ? { marketing_optin: true, whatsapp_contact: true, copy_version: 'att1-whatsapp-v1' }
        : { marketing_optin: false, notice: 'Aviso de privacidad sin opt-in explicito.' },
    },
    dedupe_key: `att1-site:att1offer:${lead.email}`,
  };
  const canonical = {
    external_submission_id: id,
    event_type: 'PRECHECKOUT_FORM_SUBMITTED',
    contract_version: version,
    submitted_at: raw.created_at,
    source: {
      tenant_ref: 'att1', funnel_ref: 'att1-main', landing_ref: 'main',
      page_url: raw.source.page_url, aliado: 'ATT1',
    },
    identity: {
      email: lead.email, phone: lead.phone, phone_valid: true,
      phone_country_iso: 'US',
    },
    lead: { full_name: lead.name },
    commerce: {
      product_ref: 'ATT1HOTLINK', product_name: 'ATT1 Offer', offer_ref: 'att1offer',
      price: '49', currency: 'USD', checkout_url: raw.data.checkout_url,
    },
    dedupe_key: raw.dedupe_key,
    consent: {
      terms_accepted: false, privacy_accepted: false,
      marketing_optin: consented, whatsapp_contact: consented,
      copy_version: consented ? 'att1-whatsapp-v1' : 'lead-precheckout-v1-no-explicit-optin',
    },
    assurance: {
      provisional: false, provider_observed: true, activation_authorized: consented,
    },
  };
  return { raw, canonical };
};
const admitPrecheckout = async (id, lead, options) => {
  const { raw, canonical } = precheckout(id, lead, options);
  return one((await db.query(`
    select * from public.admit_portable_observed_lead_precheckout(
      'att1','att1-main',1,$1,$2::jsonb,$3::jsonb
    )
  `, [id, JSON.stringify(raw), JSON.stringify(canonical)])).rows, `${id} admission`);
};
const admitConsentFailure = async (id, lead, transaction) => {
  const body = payload(id);
  body.creation_date = CONSENT_FAILED_AT.getTime();
  body.data.buyer = {
    name: lead.name, email: lead.email, checkout_phone: `+${lead.phone}`,
  };
  body.data.purchase.transaction = transaction;
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_payment_failure(
      'att1','att1-main',1,$1,$2::jsonb,$3,$4
    )
  `, [id, JSON.stringify(body), lead.email, lead.phone])).rows, `${id} admission`);
  const detail = one((await db.query(`
    select correlation_outcome, purchase_intent_id
    from public.commercial_ally_payment_failure_details
    where webhook_event_id=$1
  `, [admitted.webhook_event_id])).rows, `${id} correlation`);
  if (admitted.outcome !== 'inserted' || detail.correlation_outcome !== 'resolved') {
    throw new Error(`${id} was not admitted and correlated: ${JSON.stringify({ admitted, detail })}`);
  }
  return { eventId: admitted.webhook_event_id, intentId: detail.purchase_intent_id };
};
// resolve_event crea el contacto y sus puntos antes de planificar; el punto
// 'system' es el que deja bootstrap_proactive_lead_identity.
const createContact = async (lead, eventId, { phoneSource = 'hotmart' } = {}) => {
  await db.query(`
    insert into public.contacts (id,full_name,email,phone) values ($1,$2,$3,$4)
  `, [lead.contact, lead.name, lead.email, lead.phone]);
  await db.query(`
    insert into public.contact_points
      (contact_id,type,raw_value,normalized_value,source,source_event_id)
    values ($1,'email',$2,$2,'hotmart',$3)
  `, [lead.contact, lead.email, eventId]);
  if (phoneSource === 'hotmart') {
    await db.query(`
      insert into public.contact_points
        (contact_id,type,raw_value,normalized_value,source,source_event_id)
      values ($1,'phone',$2,$2,'hotmart',$3)
    `, [lead.contact, lead.phone, eventId]);
  } else {
    await db.query(`
      insert into public.contact_points
        (contact_id,type,raw_value,normalized_value,source,verification_status,
         is_primary,verified_at,metadata)
      values ($1,'phone',$2,$3,'system','verified',true,now(),
        jsonb_build_object('reason','authorized_precheckout_identity_bootstrap'))
    `, [lead.contact, `+${lead.phone}`, lead.phone]);
  }
  const { generation } = one((await db.query(`
    select generation from public.pilot_runtime_controls where scope_key=$1
  `, [CONSENT_SCOPE])).rows, 'consent scope generation');
  await db.query(`select * from public.set_lancemos_pilot_cohort_member(
    '${CONSENT_SCOPE}',1,$1,$2,'active','operator-test','controlled-test'
  )`, [lead.contact, generation]);
};
const planConsentFailure = (eventId, lead) => db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,'123456','ATT1 Offer','att1offer',
    '${CONSENT_SCOPE}',1,$3,42,24,$4,
    '${CONSENT_SCOPE}',1
  )
`, [eventId, lead.contact, CONSENT_FAILED_AT.toISOString(), lead.phone]);
const authorizationsOf = async (contactId) => (await db.query(`
  select authorization_status, authorization_source, evidence, valid_until
  from public.contact_authorizations
  where contact_id=$1 and channel='whatsapp' and purpose='cart_recovery'
  order by recorded_at, id
`, [contactId])).rows;
// La reevaluacion real, como la llama el dispatcher en un primer contacto sin
// conversacion: sin evidencia de Chatwoot (p_chatwoot_checked = false).
const reevaluateReal = async (actionId, worker) => {
  const now = await dbNow();
  const claimed = (await db.query(`
    select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
  `, [worker, now])).rows;
  if (claimed.length !== 1 || claimed[0].id !== actionId) {
    throw new Error(`${worker} claimed ${JSON.stringify(claimed.map((row) => row.id))}, expected ${actionId}`);
  }
  const decision = one((await db.query(`
    select * from public.reevaluate_followup_action($1,$2,$3,$4)
  `, [actionId, worker, claimed[0].lease_generation, now])).rows, `${worker} reevaluation`);
  return { claimed: claimed[0], decision };
};

// A. Intencion con consentimiento y sin carrito previo: el plan concede el
// permiso y la cadena llega hasta request_started con la reevaluacion real.
const consented = {
  contact: '50000000-0000-4000-8000-000000000131',
  name: 'Consented Buyer', email: 'consented-buyer@example.test', phone: '12025550131',
};
const consentedForm = await admitPrecheckout('att1-consent-form-1', consented);
const consentedFailure = await admitConsentFailure(
  'att1-consent-failure-1', consented, 'ATT1-CONSENT-FAIL-1',
);
if (consentedForm.outcome !== 'inserted'
    || consentedFailure.intentId !== consentedForm.purchase_intent_id) {
  throw new Error('consented payment failure did not correlate with the form intent');
}
await createContact(consented, consentedFailure.eventId);
const consentedPlan = one((await planConsentFailure(
  consentedFailure.eventId, consented,
)).rows, 'consented payment plan');
const consentedGrants = await authorizationsOf(consented.contact);
const grant = consentedGrants[0];
if (!consentedPlan.created
    || consentedGrants.length !== 1
    || grant.authorization_status !== 'allowed'
    || grant.authorization_source !== 'system'
    || grant.valid_until !== null
    || grant.evidence.reason !== 'precheckout_whatsapp_consent'
    || grant.evidence.purchase_intent_id !== consentedForm.purchase_intent_id
    || grant.evidence.precheckout_submission_id !== consentedForm.submission_id
    || grant.evidence.consent_copy_version !== 'att1-whatsapp-v1'
    || grant.evidence.webhook_event_id !== consentedFailure.eventId
    || grant.evidence.recovery_case_id !== consentedPlan.recovery_case_id) {
  throw new Error(`consented intent did not grant the exact contact permission: ${JSON.stringify({ consentedPlan, consentedGrants })}`);
}
const consentedReplay = one((await planConsentFailure(
  consentedFailure.eventId, consented,
)).rows, 'consented payment replay');
if (consentedReplay.created
    || consentedReplay.recovery_case_id !== consentedPlan.recovery_case_id
    || (await authorizationsOf(consented.contact)).length !== 1) {
  throw new Error('payment failure replay duplicated the contact permission');
}
const consentedRun = await reevaluateReal(
  consentedPlan.scheduled_action_id, 'consent-worker',
);
if (consentedRun.decision.decision !== 'execute'
    || consentedRun.decision.reason_code !== 'eligible_for_execution') {
  throw new Error(`real reevaluation did not execute the consented case: ${JSON.stringify(consentedRun.decision)}`);
}
const consentedStartNow = await dbNow();
const consentedAttempt = one((await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1,'consent-worker',$2,$3,$4,'whatsapp','approved_template',$5
  )
`, [consentedPlan.scheduled_action_id, consentedRun.claimed.lease_generation,
  consentedRun.decision.case_version, consentedRun.decision.sequence_revision,
  consentedStartNow])).rows, 'consented attempt');
await db.exec('begin');
const consentedStart = one((await db.query(`
  select * from public.mark_portable_payment_failure_request_started(
    $1,$2,'consent-worker',$3,$4
  )
`, [consentedPlan.scheduled_action_id, consentedAttempt.id,
  consentedRun.claimed.lease_generation, consentedStartNow])).rows,
'consented request start');
await db.exec('rollback');
if (consentedStart.phase !== 'request_started') {
  throw new Error('consented payment failure did not reach request_started');
}

// B. El telefono del contacto es un punto 'system' (el del bootstrap del
// formulario): antes la planificacion moria en payment_failure_contact_mismatch.
const systemPoint = {
  contact: '50000000-0000-4000-8000-000000000132',
  name: 'System Point Buyer', email: 'system-point@example.test', phone: '12025550132',
};
const systemForm = await admitPrecheckout('att1-consent-form-2', systemPoint);
const systemFailure = await admitConsentFailure(
  'att1-consent-failure-2', systemPoint, 'ATT1-CONSENT-FAIL-2',
);
await createContact(systemPoint, systemFailure.eventId, { phoneSource: 'system' });
const systemPlan = one((await planConsentFailure(
  systemFailure.eventId, systemPoint,
)).rows, 'system point payment plan');
const systemGrants = await authorizationsOf(systemPoint.contact);
if (!systemPlan.created
    || systemGrants.length !== 1
    || systemGrants[0].authorization_status !== 'allowed'
    || systemGrants[0].evidence.precheckout_submission_id !== systemForm.submission_id) {
  throw new Error(`system phone point did not plan and grant: ${JSON.stringify({ systemPlan, systemGrants })}`);
}
const systemRun = await reevaluateReal(systemPlan.scheduled_action_id, 'system-worker');
if (systemRun.decision.decision !== 'execute') {
  throw new Error(`system point case did not execute: ${JSON.stringify(systemRun.decision)}`);
}

// C. Formulario sin opt-in explicito (1.0.0): el plan crea el caso pero no
// concede nada, y la reevaluacion real escala como hasta hoy.
const withoutConsent = {
  contact: '50000000-0000-4000-8000-000000000133',
  name: 'No Optin Buyer', email: 'no-optin@example.test', phone: '12025550133',
};
const withoutConsentForm = await admitPrecheckout(
  'att1-consent-form-3', withoutConsent, { consented: false },
);
const withoutConsentFailure = await admitConsentFailure(
  'att1-consent-failure-3', withoutConsent, 'ATT1-CONSENT-FAIL-3',
);
await createContact(withoutConsent, withoutConsentFailure.eventId);
const withoutConsentPlan = one((await planConsentFailure(
  withoutConsentFailure.eventId, withoutConsent,
)).rows, 'unconsented payment plan');
if (!withoutConsentPlan.created
    || (await authorizationsOf(withoutConsent.contact)).length !== 0) {
  throw new Error('an intent without WhatsApp consent granted contact permission');
}
const withoutConsentRun = await reevaluateReal(
  withoutConsentPlan.scheduled_action_id, 'no-optin-worker',
);
if (withoutConsentRun.decision.decision !== 'escalate'
    || withoutConsentRun.decision.reason_code !== 'contact_authorization_unknown') {
  throw new Error(`unconsented case was not escalated: ${JSON.stringify(withoutConsentRun.decision)}`);
}

// D. Un opt-out previo gana: la fila denied activa impide conceder y la
// reevaluacion real cancela.
const optedOut = {
  contact: '50000000-0000-4000-8000-000000000134',
  name: 'Opted Out Buyer', email: 'opted-out@example.test', phone: '12025550134',
};
await admitPrecheckout('att1-consent-form-4', optedOut);
const optedOutFailure = await admitConsentFailure(
  'att1-consent-failure-4', optedOut, 'ATT1-CONSENT-FAIL-4',
);
await createContact(optedOut, optedOutFailure.eventId);
await db.query(`
  insert into public.channel_identities
    (contact_id, channel, account_id, external_user_id, identity_status, metadata)
  values ($1,'whatsapp','chatwoot:42',$2,'active',jsonb_build_object('inbox_id',24))
`, [optedOut.contact, optedOut.phone]);
const optOut = one((await db.query(`
  select * from public.apply_chatwoot_inbound_opt_out(42,24,9301,9302,$1,$2,'baja')
`, [optedOut.phone, (await dbNow()).toISOString()])).rows, 'opt-out');
if (optOut.outcome !== 'applied' || optOut.matched_contact_id !== optedOut.contact) {
  throw new Error(`opt-out fixture did not apply: ${JSON.stringify(optOut)}`);
}
const optedOutPlan = one((await planConsentFailure(
  optedOutFailure.eventId, optedOut,
)).rows, 'opted-out payment plan');
const optedOutRows = await authorizationsOf(optedOut.contact);
if (optedOutRows.length !== 1
    || optedOutRows[0].authorization_status !== 'denied'
    || optedOutRows[0].valid_until !== null) {
  throw new Error(`consented intent overrode a previous opt-out: ${JSON.stringify(optedOutRows)}`);
}
const optedOutRun = await reevaluateReal(optedOutPlan.scheduled_action_id, 'opt-out-worker');
if (optedOutRun.decision.decision !== 'cancel'
    || optedOutRun.decision.reason_code !== 'contact_blocked') {
  throw new Error(`opted-out case was not cancelled: ${JSON.stringify(optedOutRun.decision)}`);
}

// E. El helper directo: un motivo por cada cosa que falla.
const consentReason = async (intentId, contactId, phone) => one((await db.query(`
  select * from public._portable_consented_intent_reason($1,$2,$3)
`, [intentId, contactId, phone])).rows, 'consented intent reason');
const expectReason = async (label, row, reasonCode) => {
  if (row.reason_code !== reasonCode
      || (reasonCode === 'consented_intent_ok') !== (row.precheckout_submission_id !== null)) {
    throw new Error(`${label}: expected ${reasonCode}, got ${JSON.stringify(row)}`);
  }
};
const consentedIntent = consentedForm.purchase_intent_id;
await expectReason('consented intent',
  await consentReason(consentedIntent, consented.contact, consented.phone),
  'consented_intent_ok');
await expectReason('missing input',
  await consentReason(consentedIntent, consented.contact, null),
  'consented_intent_input_invalid');
await expectReason('unknown intent',
  await consentReason('50000000-0000-4000-8000-000000000199', consented.contact, consented.phone),
  'consented_intent_not_found');
await expectReason('other destination phone',
  await consentReason(consentedIntent, consented.contact, '12025550199'),
  'consented_intent_phone_mismatch');
await expectReason('phone of another contact',
  await consentReason(consentedIntent, withoutConsent.contact, consented.phone),
  'consented_intent_phone_mismatch');
await expectReason('form without opt-in',
  await consentReason(withoutConsentForm.purchase_intent_id, withoutConsent.contact, withoutConsent.phone),
  'consented_intent_not_authorized');
await expectReason('intent inserted without form',
  await consentReason(intentState.id, CONTACT, PHONE),
  'consented_intent_submission_missing');
await db.exec('begin');
await db.query(`
  update public.purchase_intents set lifecycle_state='purchased' where id=$1
`, [consentedIntent]);
const purchasedReason = await consentReason(consentedIntent, consented.contact, consented.phone);
await db.exec('rollback');
await expectReason('purchased intent', purchasedReason, 'consented_intent_not_live');
await db.exec('begin');
await db.query(`
  update public.commercial_ally_runtime_bindings set status='retired'
  where tenant_ref='att1' and funnel_ref='att1-main' and binding_version=1
`);
const retiredReason = await consentReason(consentedIntent, consented.contact, consented.phone);
await db.exec('rollback');
await expectReason('retired binding', retiredReason, 'consented_intent_binding_unavailable');
// Un reenvio del mismo formulario con otro contenido deja un conflicto abierto:
// ese envio deja de probar el consentimiento.
{
  const { raw, canonical } = precheckout('att1-consent-form-1', consented);
  raw.data.buyer.name = 'Changed Consented Buyer';
  canonical.lead.full_name = 'Changed Consented Buyer';
  const conflicted = one((await db.query(`
    select * from public.admit_portable_observed_lead_precheckout(
      'att1','att1-main',1,$1,$2::jsonb,$3::jsonb
    )
  `, ['att1-consent-form-1', JSON.stringify(raw), JSON.stringify(canonical)])).rows,
  'conflicting form');
  if (conflicted.outcome !== 'semantic_conflict') {
    throw new Error(`form conflict fixture diverged: ${JSON.stringify(conflicted)}`);
  }
}
await expectReason('form with an open conflict',
  await consentReason(consentedIntent, consented.contact, consented.phone),
  'consented_intent_submission_missing');

// F. La ventana entre el formulario y el pago fallido. Johanna la exige en su
// criterio (el evento entre submitted_at y submitted_at + 24 h); el helper no
// la repite porque la garantiza la correlacion: solo se resuelve una intencion
// con submitted_at en [observed_at - max_lookback, observed_at] (2 h en este
// archivo) y el plan exige correlation_outcome = 'resolved'. Un pago fallido
// fuera de esa ventana, antes o despues, no se correlaciona: el plan se
// rechaza y no concede permiso, aunque la intencion tenga consentimiento.
const admitUncorrelatedFailure = async (id, lead, transaction) => {
  const body = payload(id);
  body.creation_date = CONSENT_FAILED_AT.getTime();
  body.data.buyer = {
    name: lead.name, email: lead.email, checkout_phone: `+${lead.phone}`,
  };
  body.data.purchase.transaction = transaction;
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_payment_failure(
      'att1','att1-main',1,$1,$2::jsonb,$3,$4
    )
  `, [id, JSON.stringify(body), lead.email, lead.phone])).rows, `${id} admission`);
  const detail = one((await db.query(`
    select correlation_outcome, purchase_intent_id
    from public.commercial_ally_payment_failure_details
    where webhook_event_id=$1
  `, [admitted.webhook_event_id])).rows, `${id} correlation`);
  if (admitted.outcome !== 'inserted'
      || detail.correlation_outcome !== 'unmatched'
      || detail.purchase_intent_id !== null) {
    throw new Error(`${id} was correlated outside the lookback: ${JSON.stringify({ admitted, detail })}`);
  }
  return admitted.webhook_event_id;
};
const expectOutsideWindow = async (label, lead, submittedAt, transaction) => {
  const form = await admitPrecheckout(`att1-consent-form-${lead.phone}`, lead, { submittedAt });
  if (form.outcome !== 'inserted' || form.purchase_intent_id == null) {
    throw new Error(`${label}: the consented form was not admitted: ${JSON.stringify(form)}`);
  }
  const eventId = await admitUncorrelatedFailure(
    `att1-consent-failure-${lead.phone}`, lead, transaction,
  );
  await createContact(lead, eventId);
  let error = null;
  await db.exec('begin');
  try {
    await planConsentFailure(eventId, lead);
  } catch (caught) {
    error = caught;
  } finally {
    await db.exec('rollback');
  }
  if (error?.code !== '55000' || error?.message !== 'payment_failure_correlation_unresolved') {
    throw new Error(`${label}: expected payment_failure_correlation_unresolved, got ${error?.code} ${error?.message}`);
  }
  if ((await authorizationsOf(lead.contact)).length !== 0) {
    throw new Error(`${label}: a payment failure outside the window granted contact permission`);
  }
  // El helper solo no mira el tiempo: la ventana es de la correlacion.
  await expectReason(`${label} helper`,
    await consentReason(form.purchase_intent_id, lead.contact, lead.phone),
    'consented_intent_ok');
};
await expectOutsideWindow('payment failure after the lookback', {
  contact: '50000000-0000-4000-8000-000000000135',
  name: 'Stale Form Buyer', email: 'stale-form@example.test', phone: '12025550135',
}, at(-200), 'ATT1-CONSENT-FAIL-6');
await expectOutsideWindow('payment failure before the form', {
  contact: '50000000-0000-4000-8000-000000000136',
  name: 'Late Form Buyer', email: 'late-form@example.test', phone: '12025550136',
}, at(-5), 'ATT1-CONSENT-FAIL-7');

console.log('commercial_ally_payment_failure_recovery=OK');
