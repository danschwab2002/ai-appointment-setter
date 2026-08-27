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
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
`);

const files = [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(join(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => join(root, 'supabase/migrations', name)),
];
for (const file of files) {
  await db.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}

const email = 'payment-failure@example.test';
const phone = '15550001111';
await db.query(`
  insert into public.purchase_intents (
    tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
    normalized_email, normalized_phone, submitted_at, lifecycle_state,
    current_classification, whatsapp_contact_authorized, provisional,
    provider_observed, activation_authorized
  ) values (
    'lancemos', 'psicologajohanna', 'ads-a', 'f106691755g', 'bxjge6zq',
    $1, $2, '2026-08-25T20:00:00Z', 'waiting_for_purchase',
    null, true, false, true, true
  )
`, [email, phone]);

await db.query(`
  with inserted as (
    insert into public.precheckout_submissions (
      external_submission_id, contract_version, raw_payload,
      canonical_payload, provisional, provider_observed, activation_authorized
    ) values (
      'payment-failure-submission-1', '1.1.0', '{}'::jsonb,
      jsonb_build_object(
        'lead', jsonb_build_object('full_name', 'Lead de Pago'),
        'identity', jsonb_build_object('email', $1::text, 'phone', $2::text),
        'commerce', jsonb_build_object(
          'offer_ref', 'bxjge6zq', 'product_name', 'Libre de Ansiedad'
        ),
        'consent', jsonb_build_object(
          'marketing_optin', true,
          'whatsapp_contact', true,
          'copy_version', 'johanna-precheckout-whatsapp-disclosure-v1'
        )
      ),
      false, true, true
    ) returning id
  )
  insert into public.purchase_intent_submissions (
    purchase_intent_id, submission_id, ordinal
  )
  select intent.id, inserted.id, 1
  from public.purchase_intents intent cross join inserted
  where intent.normalized_email = $1
`, [email, phone]);

function payload(id, transaction = 'HP12345678', productId = 8104005) {
  return {
    id,
    creation_date: 1787688000000,
    event: 'PURCHASE_CANCELED',
    version: '2.0.0',
    data: {
      product: { id: productId, name: 'Libre de Ansiedad' },
      buyer: { email, checkout_phone: phone },
      purchase: {
        transaction,
        status: 'CANCELED',
        offer: { code: 'bxjge6zq' },
        payment: { refusal_reason: 'processor-specific card rejection' },
      },
    },
  };
}

async function admit(eventPayload) {
  return db.query(`
    select * from public.admit_johanna_payment_failure(
      $1, $2::jsonb, $3, $4
    )
  `, [eventPayload.id, JSON.stringify(eventPayload), email, phone]);
}

const first = await admit(payload('failure-event-1'));
if (first.rows[0]?.outcome !== 'inserted'
    || first.rows[0]?.correlation_outcome !== 'resolved'
    || first.rows[0]?.case_status !== 'pending_human_review') {
  throw new Error(`resolved admission failed: ${JSON.stringify(first.rows)}`);
}
const caseId = first.rows[0].payment_failure_case_id;
const classification = await db.query(`
  select current_classification from public.purchase_intents
  where normalized_email = $1
`, [email]);
if (classification.rows[0]?.current_classification !== 'payment_failure_supported') {
  throw new Error('resolved failure did not set payment_failure_supported');
}

const started = await db.query(`
  select * from public.begin_johanna_payment_failure_hotmart_auto(
    $1, $2::uuid, 1, 9
  )
`, [`johanna-payment-failure-auto:${caseId}`, caseId]);
if (started.rows[0]?.outcome !== 'started'
    || started.rows[0]?.template_name !== 'johanna_compra_fallida_01'
    || started.rows[0]?.target_phone !== phone
    || started.rows[0]?.command_status !== 'request_started') {
  throw new Error(`payment failure command did not start: ${JSON.stringify(started.rows)}`);
}
const commandId = started.rows[0].command_id;
const commandReplay = await db.query(`
  select * from public.begin_johanna_payment_failure_hotmart_auto(
    $1, $2::uuid, 1, 9
  )
`, [`johanna-payment-failure-auto:${caseId}`, caseId]);
if (commandReplay.rows[0]?.outcome !== 'replay'
    || commandReplay.rows[0]?.command_id !== commandId) {
  throw new Error(`payment failure command replay failed: ${JSON.stringify(commandReplay.rows)}`);
}
await db.query(`
  select * from public.finish_johanna_abandonment_one_shot(
    $1::uuid, 'delivery_unknown', null, null, 'response_lost'
  )
`, [commandId]);
const unknown = await db.query(`
  select case_status from public.johanna_payment_failure_cases where id = $1::uuid
`, [caseId]);
if (unknown.rows[0]?.case_status !== 'delivery_unknown') {
  throw new Error(`payment failure unknown state failed: ${JSON.stringify(unknown.rows)}`);
}
const ordinaryUnknownRetry = await prepareInvalidContactRetry(
  { caseId },
  `johanna-payment-failure-auto:${caseId}`,
);
if (ordinaryUnknownRetry.rows[0]?.outcome !== 'not_retryable'
    || ordinaryUnknownRetry.rows[0]?.command_status !== 'delivery_unknown') {
  throw new Error(
    `ordinary unknown became retryable: ${JSON.stringify(ordinaryUnknownRetry.rows)}`,
  );
}
const reconciled = await db.query(`
  select * from public.reconcile_johanna_abandonment_one_shot(
    $1, 901, 902
  )
`, [`johanna-payment-failure-auto:${caseId}`]);
if (reconciled.rows[0]?.command_status !== 'accepted_by_chatwoot') {
  throw new Error(`payment failure reconciliation failed: ${JSON.stringify(reconciled.rows)}`);
}
const terminal = await db.query(`
  select case_status from public.johanna_payment_failure_cases where id = $1::uuid
`, [caseId]);
if (terminal.rows[0]?.case_status !== 'outbound_accepted') {
  throw new Error(`payment failure terminal state failed: ${JSON.stringify(terminal.rows)}`);
}
const reconciliationReplay = await db.query(`
  select * from public.reconcile_johanna_abandonment_one_shot(
    $1, 901, 902
  )
`, [`johanna-payment-failure-auto:${caseId}`]);
if (reconciliationReplay.rows[0]?.command_id !== commandId
    || reconciliationReplay.rows[0]?.command_status !== 'accepted_by_chatwoot') {
  throw new Error(`payment failure reconciliation replay failed: ${JSON.stringify(reconciliationReplay.rows)}`);
}

const replay = await admit(payload('failure-event-1'));
if (replay.rows[0]?.outcome !== 'duplicate'
    || replay.rows[0]?.payment_failure_case_id !== caseId) {
  throw new Error(`exact replay failed: ${JSON.stringify(replay.rows)}`);
}
const conflict = await admit(payload('failure-event-1', 'HP87654321'));
if (conflict.rows[0]?.outcome !== 'semantic_conflict'
    || conflict.rows[0]?.payment_failure_case_id !== caseId) {
  throw new Error(`semantic conflict failed: ${JSON.stringify(conflict.rows)}`);
}
const count = await db.query(`
  select count(*)::integer count from public.johanna_payment_failure_cases
`);
if (count.rows[0]?.count !== 1) {
  throw new Error('replay or conflict appended another payment-failure case');
}

const foreignEmail = 'foreign-payment-failure@example.test';
const foreignPhone = '15550003333';
await db.query(`
  insert into public.purchase_intents (
    tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
    normalized_email, normalized_phone, submitted_at, lifecycle_state,
    current_classification, whatsapp_contact_authorized, provisional,
    provider_observed, activation_authorized
  ) values (
    'other-tenant', 'other-funnel', 'ads-a', '8104005', 'bxjge6zq',
    $1, $2, '2026-08-25T20:00:00Z', 'waiting_for_purchase',
    null, false, false, true, false
  )
`, [foreignEmail, foreignPhone]);
const foreignPayload = payload('failure-event-foreign', 'HP11223344');
foreignPayload.data.buyer.email = foreignEmail;
foreignPayload.data.buyer.checkout_phone = foreignPhone;
const foreign = await db.query(`
  select * from public.admit_johanna_payment_failure(
    $1, $2::jsonb, $3, $4
  )
`, [
  foreignPayload.id,
  JSON.stringify(foreignPayload),
  foreignEmail,
  foreignPhone,
]);
if (foreign.rows[0]?.correlation_outcome !== 'unmatched') {
  throw new Error(`foreign scope resolved: ${JSON.stringify(foreign.rows)}`);
}
const foreignClassification = await db.query(`
  select current_classification from public.purchase_intents
  where normalized_email = $1
`, [foreignEmail]);
if (foreignClassification.rows[0]?.current_classification !== null) {
  throw new Error('foreign scope intent was mutated');
}

let wrongScopeRejected = false;
try {
  await admit(payload('failure-event-wrong-product', 'HP11223344', 9999999));
} catch (error) {
  wrongScopeRejected = String(error).includes('invalid_johanna_payment_failure_scope');
}
if (!wrongScopeRejected) {
  throw new Error('wrong product did not fail closed');
}

async function provisionAuthorizedFailure(suffix, includeRefusalReason = true) {
  const fixtureEmail = `payment-${suffix}@example.test`;
  const fixturePhone = `155501${suffix.padStart(5, '0')}`;
  const intentRow = await db.query(`
    insert into public.purchase_intents (
      tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
      normalized_email, normalized_phone, submitted_at, lifecycle_state,
      current_classification, whatsapp_contact_authorized, provisional,
      provider_observed, activation_authorized
    ) values (
      'lancemos', 'psicologajohanna', 'ads-a', 'F106691755G', 'bxjge6zq',
      $1, $2, '2026-08-25T19:00:00Z', 'waiting_for_purchase',
      null, true, false, true, true
    ) returning id
  `, [fixtureEmail, fixturePhone]);
  const intentId = intentRow.rows[0].id;
  const submissionRow = await db.query(`
    insert into public.precheckout_submissions (
      external_submission_id, contract_version, raw_payload,
      canonical_payload, provisional, provider_observed, activation_authorized
    ) values (
      $1, '1.1.0', '{}'::jsonb,
      jsonb_build_object(
        'lead', jsonb_build_object('full_name', 'Lead Adversarial'),
        'identity', jsonb_build_object(
          'email', $2::text, 'phone', $3::text
        ),
        'commerce', jsonb_build_object(
          'offer_ref', 'bxjge6zq', 'product_name', 'Libre de Ansiedad'
        ),
        'consent', jsonb_build_object(
          'marketing_optin', true,
          'whatsapp_contact', true,
          'copy_version', 'johanna-precheckout-whatsapp-disclosure-v1'
        )
      ), false, true, true
    ) returning id
  `, [`payment-submission-${suffix}`, fixtureEmail, fixturePhone]);
  await db.query(`
    insert into public.purchase_intent_submissions (
      purchase_intent_id, submission_id, ordinal
    ) values ($1::uuid, $2::uuid, 1)
  `, [intentId, submissionRow.rows[0].id]);
  const eventPayload = payload(
    `failure-adversarial-${suffix}`,
    `HP9${suffix.padStart(7, '0')}`,
  );
  eventPayload.data.buyer.email = fixtureEmail;
  eventPayload.data.buyer.checkout_phone = fixturePhone;
  if (!includeRefusalReason) {
    delete eventPayload.data.purchase.payment.refusal_reason;
  }
  const admission = await db.query(`
    select * from public.admit_johanna_payment_failure(
      $1, $2::jsonb, $3, $4
    )
  `, [
    eventPayload.id,
    JSON.stringify(eventPayload),
    fixtureEmail,
    fixturePhone,
  ]);
  if (admission.rows[0]?.correlation_outcome !== 'resolved') {
    throw new Error(`adversarial fixture did not resolve: ${suffix}`);
  }
  return {
    caseId: admission.rows[0].payment_failure_case_id,
    email: fixtureEmail,
    intentId,
    phone: fixturePhone,
  };
}

async function beginFailure(fixture, key) {
  return db.query(`
    select * from public.begin_johanna_payment_failure_hotmart_auto(
      $1, $2::uuid, 1, 9
    )
  `, [key, fixture.caseId]);
}

async function prepareInvalidContactRetry(fixture, key) {
  return db.query(`
    select *
    from public.prepare_johanna_payment_failure_invalid_contact_retry(
      $1, $2::uuid, 1, 9
    )
  `, [key, fixture.caseId]);
}

// Carrito and payment failure consume one physical budget per person.
const budgetFixture = await provisionAuthorizedFailure('201');
await db.query(`
  insert into public.johanna_abandonment_one_shot_commands (
    command_key, semantic_fingerprint, rollout_scope, purchase_intent_id,
    scope_key, scope_version, runtime_generation,
    chatwoot_account_id, chatwoot_inbox_id, target_phone,
    template_name, template_language, template_category, copy_version,
    max_messages, followups_allowed, status
  ) values (
    'existing-cart-budget-201', repeat('a', 64),
    'johanna-abandonment-template-e2e-v1', $1::uuid,
    'johanna-abandonment-template-e2e', 1, 0,
    1, 9, $2, 'johanna_carrito_abandonado_01', 'es_EC', 'MARKETING',
    'johanna-abandonment-one-shot-v1', 1, 0, 'request_started'
  )
`, [budgetFixture.intentId, budgetFixture.phone]);
const budgetResult = await beginFailure(
  budgetFixture,
  'johanna-payment-failure-auto:budget-201',
);
if (budgetResult.rows[0]?.outcome !== 'budget_consumed') {
  throw new Error(`shared budget bypassed: ${JSON.stringify(budgetResult.rows)}`);
}
const budgetCount = await db.query(`
  select count(*)::integer count
  from public.johanna_abandonment_one_shot_commands
  where target_phone = $1
`, [budgetFixture.phone]);
if (budgetCount.rows[0]?.count !== 1) {
  throw new Error('shared budget created a second command');
}

// A purchase approved before request-start removes effect authority.
const approvedFixture = await provisionAuthorizedFailure('202');
await db.query(`
  update public.purchase_intents
  set lifecycle_state = 'purchased', current_classification = null
  where id = $1::uuid
`, [approvedFixture.intentId]);
let approvedRejected = false;
try {
  await beginFailure(approvedFixture, 'johanna-payment-failure-auto:approved-202');
} catch (error) {
  approvedRejected = String(error).includes(
    'johanna_payment_failure_hotmart_auto_intent_not_authorized',
  );
}
if (!approvedRejected) {
  throw new Error('PURCHASE_APPROVED state did not block payment recovery');
}

// A durable stop blocks the exact canonical WhatsApp identity.
const stoppedFixture = await provisionAuthorizedFailure('203');
await db.query(`
  insert into public.contact_opt_out_events (
    channel, purpose, source, canonical_account_id, canonical_inbox_id,
    canonical_conversation_id, canonical_message_id, external_user_id,
    occurred_at, normalized_rule_key, correlation_status
  ) values (
    'whatsapp', 'cart_recovery', 'chatwoot', 1, 9, 9203, 9303, $1,
    '2026-08-25T20:30:00Z', 'no_more_messages', 'unmatched'
  )
`, [stoppedFixture.phone]);
let stopRejected = false;
try {
  await beginFailure(stoppedFixture, 'johanna-payment-failure-auto:stopped-203');
} catch (error) {
  stopRejected = String(error).includes(
    'johanna_payment_failure_hotmart_auto_contact_blocked',
  );
}
if (!stopRejected) {
  throw new Error('durable opt-out did not block payment recovery');
}

// Two canonical contact owners for one phone fail closed.
const ambiguousFixture = await provisionAuthorizedFailure('204');
await db.query(`
  insert into public.contacts (full_name, email, phone)
  values
    ('Ambiguous One', 'ambiguous-one@example.test', $1),
    ('Ambiguous Two', 'ambiguous-two@example.test', $1)
`, [ambiguousFixture.phone]);
await db.query(`
  insert into public.contact_points (
    contact_id, type, raw_value, normalized_value, source
  )
  select id, 'phone', $1, $1, 'system'
  from public.contacts
  where email in ('ambiguous-one@example.test', 'ambiguous-two@example.test')
`, [ambiguousFixture.phone]);
let ambiguousRejected = false;
try {
  await beginFailure(
    ambiguousFixture,
    'johanna-payment-failure-auto:ambiguous-204',
  );
} catch (error) {
  ambiguousRejected = String(error).includes(
    'johanna_payment_failure_hotmart_auto_phone_ambiguous',
  );
}
if (!ambiguousRejected) {
  throw new Error('ambiguous phone ownership did not fail closed');
}

// Concurrent same-key starts converge on one command plus one replay.
const concurrentFixture = await provisionAuthorizedFailure('205');
const concurrentKey = 'johanna-payment-failure-auto:concurrent-205';
const concurrentResults = await Promise.all([
  beginFailure(concurrentFixture, concurrentKey),
  beginFailure(concurrentFixture, concurrentKey),
]);
const concurrentOutcomes = concurrentResults
  .map((result) => result.rows[0]?.outcome)
  .sort();
if (JSON.stringify(concurrentOutcomes) !== JSON.stringify(['replay', 'started'])) {
  throw new Error(`concurrent replay failed: ${JSON.stringify(concurrentOutcomes)}`);
}
const concurrentCount = await db.query(`
  select count(*)::integer count
  from public.johanna_abandonment_one_shot_commands
  where command_key = $1
`, [concurrentKey]);
if (concurrentCount.rows[0]?.count !== 1) {
  throw new Error('concurrent replay created multiple commands');
}

// Refusal reason is provider metadata, not an eligibility gate.
const noReasonFixture = await provisionAuthorizedFailure('206', false);
const noReasonStarted = await beginFailure(
  noReasonFixture,
  'johanna-payment-failure-auto:no-reason-206',
);
if (noReasonStarted.rows[0]?.outcome !== 'started') {
  throw new Error(
    `missing refusal reason blocked recovery: ${JSON.stringify(noReasonStarted.rows)}`,
  );
}
const noReasonCase = await db.query(`
  select refusal_reason from public.johanna_payment_failure_cases
  where id = $1::uuid
`, [noReasonFixture.caseId]);
if (noReasonCase.rows[0]?.refusal_reason !== null) {
  throw new Error('missing refusal reason was not preserved as null');
}

// Only the observed legacy nested-contact parse failure gets one retry.
const retryFixture = await provisionAuthorizedFailure('207');
const retryKey = 'johanna-payment-failure-auto:invalid-contact-207';
const retryCommand = await beginFailure(retryFixture, retryKey);
await db.query(`
  select * from public.finish_johanna_abandonment_one_shot(
    $1::uuid, 'delivery_unknown', null, null, 'invalid_contact_id'
  )
`, [retryCommand.rows[0].command_id]);
const retryStarted = await prepareInvalidContactRetry(retryFixture, retryKey);
if (retryStarted.rows[0]?.outcome !== 'retry_started'
    || retryStarted.rows[0]?.command_status !== 'request_started') {
  throw new Error(`invalid contact retry did not start: ${JSON.stringify(retryStarted.rows)}`);
}
const retryState = await db.query(`
  select cmd.status, cmd.invalid_contact_retry_count,
         cmd.failure_code, cmd.finalized_at, payment_case.case_status
  from public.johanna_abandonment_one_shot_commands cmd
  join public.johanna_payment_failure_cases payment_case
    on payment_case.outbound_command_id = cmd.id
  where cmd.id = $1::uuid
`, [retryCommand.rows[0].command_id]);
if (retryState.rows[0]?.status !== 'request_started'
    || retryState.rows[0]?.invalid_contact_retry_count !== 1
    || retryState.rows[0]?.failure_code !== null
    || retryState.rows[0]?.finalized_at !== null
    || retryState.rows[0]?.case_status !== 'outbound_started') {
  throw new Error(`invalid contact retry state mismatch: ${JSON.stringify(retryState.rows)}`);
}
const retryReplay = await prepareInvalidContactRetry(retryFixture, retryKey);
if (retryReplay.rows[0]?.outcome !== 'not_retryable'
    || retryReplay.rows[0]?.command_status !== 'request_started') {
  throw new Error(`invalid contact retry budget repeated: ${JSON.stringify(retryReplay.rows)}`);
}

// A stop arriving before retry preserves the unknown predecessor without mutation.
const retryStoppedFixture = await provisionAuthorizedFailure('208');
const retryStoppedKey = 'johanna-payment-failure-auto:invalid-contact-stopped-208';
const retryStoppedCommand = await beginFailure(retryStoppedFixture, retryStoppedKey);
await db.query(`
  select * from public.finish_johanna_abandonment_one_shot(
    $1::uuid, 'delivery_unknown', null, null, 'invalid_contact_id'
  )
`, [retryStoppedCommand.rows[0].command_id]);
await db.query(`
  insert into public.contact_opt_out_events (
    channel, purpose, source, canonical_account_id, canonical_inbox_id,
    canonical_conversation_id, canonical_message_id, external_user_id,
    occurred_at, normalized_rule_key, correlation_status
  ) values (
    'whatsapp', 'cart_recovery', 'chatwoot', 1, 9, 9208, 9308, $1,
    '2026-08-25T20:30:00Z', 'no_more_messages', 'unmatched'
  )
`, [retryStoppedFixture.phone]);
let retryStopRejected = false;
try {
  await prepareInvalidContactRetry(retryStoppedFixture, retryStoppedKey);
} catch (error) {
  retryStopRejected = String(error).includes(
    'johanna_payment_failure_invalid_contact_retry_contact_blocked',
  );
}
if (!retryStopRejected) {
  throw new Error('durable opt-out did not block invalid contact retry');
}
const retryStoppedState = await db.query(`
  select cmd.status, cmd.invalid_contact_retry_count, payment_case.case_status
  from public.johanna_abandonment_one_shot_commands cmd
  join public.johanna_payment_failure_cases payment_case
    on payment_case.outbound_command_id = cmd.id
  where cmd.id = $1::uuid
`, [retryStoppedCommand.rows[0].command_id]);
if (retryStoppedState.rows[0]?.status !== 'delivery_unknown'
    || retryStoppedState.rows[0]?.invalid_contact_retry_count !== 0
    || retryStoppedState.rows[0]?.case_status !== 'delivery_unknown') {
  throw new Error(`blocked retry mutated state: ${JSON.stringify(retryStoppedState.rows)}`);
}

const acl = await db.query(`
  select
    has_function_privilege(
      'anon',
      'public.admit_johanna_payment_failure(text,jsonb,text,text)',
      'execute'
    ) anon_execute,
    has_function_privilege(
      'service_role',
      'public.admit_johanna_payment_failure(text,jsonb,text,text)',
      'execute'
    ) service_execute,
    has_function_privilege(
      'anon',
      'public.prepare_johanna_payment_failure_invalid_contact_retry(text,uuid,bigint,bigint)',
      'execute'
    ) retry_anon_execute,
    has_function_privilege(
      'service_role',
      'public.prepare_johanna_payment_failure_invalid_contact_retry(text,uuid,bigint,bigint)',
      'execute'
    ) retry_service_execute
`);
if (acl.rows[0]?.anon_execute !== false
    || acl.rows[0]?.service_execute !== true
    || acl.rows[0]?.retry_anon_execute !== false
    || acl.rows[0]?.retry_service_execute !== true) {
  throw new Error(`payment failure ACL failed: ${JSON.stringify(acl.rows)}`);
}

console.log('JOHANNA_PAYMENT_FAILURE_DURABLE_REVIEW_OK');
