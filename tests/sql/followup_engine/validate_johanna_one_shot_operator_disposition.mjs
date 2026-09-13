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

const submittedAt = '2026-09-01T12:00:00Z';
const finalizedAt = '2026-09-01T13:05:00Z';
const intent = await db.query(`
  insert into public.purchase_intents (
    tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
    normalized_email, normalized_phone, submitted_at, lifecycle_state,
    whatsapp_contact_authorized, provisional, provider_observed,
    activation_authorized
  ) values (
    'lancemos', 'psicologajohanna', 'ads-b', 'F106691755G', 'mgbgpp19',
    'operator-disposition@example.test', '12025551055', $1::timestamptz,
    'waiting_for_purchase', true, false, true, true
  ) returning id
`, [submittedAt]);
const purchaseIntentId = intent.rows[0].id;
const canonical = {
  event_type: 'PRECHECKOUT_FORM_SUBMITTED',
  contract_version: '1.1.0',
  external_submission_id: 'operator-disposition-submission-001',
  submitted_at: submittedAt,
  source: {
    tenant_ref: 'lancemos', funnel_ref: 'psicologajohanna', landing_ref: 'ads-b',
    page_url: 'https://example.test/precheckout', aliado: 'Psicologa Johanna',
  },
  identity: {
    email: 'operator-disposition@example.test', phone: '12025551055',
    phone_valid: true, phone_country_iso: 'US',
  },
  lead: { full_name: 'Synthetic Operator Disposition' },
  commerce: {
    product_ref: 'F106691755G', product_name: 'Libre de Ansiedad',
    price: '49', currency: 'USD', offer_ref: 'mgbgpp19',
    checkout_url: 'https://pay.example.test/opaque',
  },
  consent: {
    terms_accepted: false, privacy_accepted: false, marketing_optin: true,
    whatsapp_contact: true,
    copy_version: 'johanna-precheckout-whatsapp-disclosure-v1',
  },
  dedupe_key: 'psicologajohanna:mgbgpp19:operator-disposition@example.test',
  assurance: { provisional: false, provider_observed: true, activation_authorized: true },
};
const submission = await db.query(`
  insert into public.precheckout_submissions (
    external_submission_id, contract_version, raw_payload, canonical_payload,
    provisional, provider_observed, activation_authorized
  ) values (
    'operator-disposition-submission-001', '1.1.0', '{}'::jsonb, $1::jsonb,
    false, true, true
  ) returning id
`, [JSON.stringify(canonical)]);
await db.query(`
  insert into public.purchase_intent_submissions (
    purchase_intent_id, submission_id, ordinal
  ) values ($1::uuid, $2::uuid, 1)
`, [purchaseIntentId, submission.rows[0].id]);
const timer = await db.query(`
  insert into public.hotmart_abandonment_reevaluations (
    purchase_intent_id, source_kind, source_submission_id,
    source_webhook_event_id, source_scope_id,
    policy_binding_id, policy_binding_generation, policy_key, policy_version,
    delay_seconds_snapshot, observed_at, due_at, status, outcome,
    idempotency_key, completed_at
  )
  select $1::uuid, 'precheckout_intent', $2::uuid, null, null,
         binding.id, binding.generation, binding.policy_key, binding.policy_version,
         event.delay_seconds, $3::timestamptz,
         $3::timestamptz + interval '60 minutes', 'completed', 'command_reserved',
         'operator-disposition:' || $2::text, $4::timestamptz
  from public.hotmart_abandonment_timer_policy_bindings binding
  join public.hotmart_abandonment_timer_policy_binding_events event
    on event.binding_id = binding.id and event.generation = binding.generation
  where binding.tenant_ref = 'lancemos'
    and binding.funnel_ref = 'psicologajohanna'
    and lower(binding.product_ref) = lower('F106691755G')
    and binding.offer_ref = 'mgbgpp19'
  returning id
`, [purchaseIntentId, submission.rows[0].id, submittedAt, finalizedAt]);
const timerId = timer.rows[0].id;
const command = await db.query(`
  insert into public.johanna_abandonment_one_shot_commands (
    command_key, semantic_fingerprint, rollout_scope, purchase_intent_id,
    scope_key, scope_version, runtime_generation,
    chatwoot_account_id, chatwoot_inbox_id, target_phone,
    template_name, template_language, template_category, copy_version,
    max_messages, followups_allowed, status, failure_code,
    created_at, finalized_at, source_reevaluation_id
  ) values (
    'operator-disposition:test', repeat('a', 64),
    'johanna-precheckout-delayed-first-touch-v1', $1::uuid,
    'johanna-precheckout-delayed-first-touch-production', 1, 0,
    1, 9, '12025551055', 'johanna_interes_precheckout_01', 'es_EC',
    'MARKETING', 'johanna-precheckout-delayed-first-touch-v1',
    1, 0, 'delivery_unknown', 'chatwoot_http_error',
    $2::timestamptz, $3::timestamptz, $4::uuid
  ) returning id
`, [purchaseIntentId, submittedAt, finalizedAt, timerId]);
const commandId = command.rows[0].id;

const before = await db.query(
  'select * from public.get_precheckout_delayed_first_touch_readiness()',
);
if (before.rows[0]?.delivery_unknown_count !== 1) {
  throw new Error(`precondition did not expose unresolved delivery: ${JSON.stringify(before.rows)}`);
}

const recorded = await db.query(`
  select * from public.resolve_johanna_one_shot_unverifiable_contact_deleted(
    $1::uuid, 'dan-operator'
  )
`, [commandId]);
const after = await db.query(
  'select * from public.get_precheckout_delayed_first_touch_readiness()',
);
const durable = await db.query(`
  select command.status, command.failure_code,
         command.chatwoot_conversation_id, command.chatwoot_message_id,
         disposition.disposition_code, disposition.evidence_code,
         disposition.operator_ref
  from public.johanna_abandonment_one_shot_commands command
  join public.johanna_one_shot_operator_dispositions disposition
    on disposition.command_id = command.id
  where command.id = $1::uuid
`, [commandId]);
if (recorded.rows[0]?.outcome !== 'recorded'
    || recorded.rows[0]?.command_status !== 'delivery_unknown'
    || after.rows[0]?.delivery_unknown_count !== 0
    || durable.rows[0]?.status !== 'delivery_unknown'
    || durable.rows[0]?.failure_code !== 'chatwoot_http_error'
    || durable.rows[0]?.chatwoot_conversation_id !== null
    || durable.rows[0]?.chatwoot_message_id !== null
    || durable.rows[0]?.disposition_code !== 'unverifiable_external_contact_deleted'
    || durable.rows[0]?.evidence_code !== 'operator_confirmed_chatwoot_contact_deleted') {
  throw new Error(`operator disposition diverged: ${JSON.stringify({ recorded: recorded.rows, after: after.rows, durable: durable.rows })}`);
}

const replay = await db.query(`
  select * from public.resolve_johanna_one_shot_unverifiable_contact_deleted(
    $1::uuid, 'dan-operator'
  )
`, [commandId]);
const count = await db.query(`
  select count(*)::integer as count
  from public.johanna_one_shot_operator_dispositions
  where command_id = $1::uuid
`, [commandId]);
if (replay.rows[0]?.outcome !== 'replay'
    || replay.rows[0]?.disposition_id !== recorded.rows[0]?.disposition_id
    || count.rows[0]?.count !== 1) {
  throw new Error(`exact replay was not idempotent: ${JSON.stringify({ replay: replay.rows, count: count.rows })}`);
}

let conflictBlocked = false;
try {
  await db.query(`
    select * from public.resolve_johanna_one_shot_unverifiable_contact_deleted(
      $1::uuid, 'different-operator'
    )
  `, [commandId]);
} catch (error) {
  conflictBlocked = String(error).includes('johanna_one_shot_operator_disposition_conflict');
}
if (!conflictBlocked) throw new Error('changed operator did not conflict');

let disposedReconciliationBlocked = false;
try {
  await db.query(`
    select * from public.reconcile_johanna_abandonment_one_shot(
      'operator-disposition:test', 777, 888
    )
  `);
} catch (error) {
  disposedReconciliationBlocked = String(error).includes(
    'johanna_abandonment_one_shot_disposed_immutable',
  );
}
if (!disposedReconciliationBlocked) {
  throw new Error('disposed command reconciliation was not blocked');
}

async function insertVariant({ suffix, variantFinalizedAt, failureCode, conversationId = null }) {
  await db.exec('reset role');
  const variantIntent = await db.query(`
    insert into public.purchase_intents (
      tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
      normalized_email, normalized_phone, submitted_at, lifecycle_state,
      whatsapp_contact_authorized, provisional, provider_observed,
      activation_authorized
    ) values (
      'lancemos', 'psicologajohanna', 'ads-b', 'F106691755G', 'mgbgpp19',
      $1, $2, $3::timestamptz, 'waiting_for_purchase', true, false, true, true
    ) returning id
  `, [`operator-disposition-${suffix}@example.test`, `12025551${suffix}`, submittedAt]);
  const variantIntentId = variantIntent.rows[0].id;
  const variantCanonical = {
    ...canonical,
    external_submission_id: `operator-disposition-submission-${suffix}`,
    identity: {
      ...canonical.identity,
      email: `operator-disposition-${suffix}@example.test`,
      phone: `12025551${suffix}`,
    },
    dedupe_key: `psicologajohanna:mgbgpp19:operator-disposition-${suffix}@example.test`,
  };
  const variantSubmission = await db.query(`
    insert into public.precheckout_submissions (
      external_submission_id, contract_version, raw_payload, canonical_payload,
      provisional, provider_observed, activation_authorized
    ) values ($1, '1.1.0', '{}'::jsonb, $2::jsonb, false, true, true)
    returning id
  `, [`operator-disposition-submission-${suffix}`, JSON.stringify(variantCanonical)]);
  await db.query(`
    insert into public.purchase_intent_submissions (
      purchase_intent_id, submission_id, ordinal
    ) values ($1::uuid, $2::uuid, 1)
  `, [variantIntentId, variantSubmission.rows[0].id]);
  const variantTimer = await db.query(`
    insert into public.hotmart_abandonment_reevaluations (
      purchase_intent_id, source_kind, source_submission_id,
      source_webhook_event_id, source_scope_id,
      policy_binding_id, policy_binding_generation, policy_key, policy_version,
      delay_seconds_snapshot, observed_at, due_at, status, outcome,
      idempotency_key, completed_at
    )
    select $1::uuid, 'precheckout_intent', $2::uuid, null, null,
           binding.id, binding.generation, binding.policy_key, binding.policy_version,
           event.delay_seconds, $3::timestamptz,
           $3::timestamptz + interval '60 minutes', 'completed', 'command_reserved',
           'operator-disposition:' || $2::text, $4::timestamptz
    from public.hotmart_abandonment_timer_policy_bindings binding
    join public.hotmart_abandonment_timer_policy_binding_events event
      on event.binding_id = binding.id and event.generation = binding.generation
    where binding.tenant_ref = 'lancemos'
      and binding.funnel_ref = 'psicologajohanna'
      and lower(binding.product_ref) = lower('F106691755G')
      and binding.offer_ref = 'mgbgpp19'
    returning id
  `, [variantIntentId, variantSubmission.rows[0].id, submittedAt, variantFinalizedAt]);
  const variantCommand = await db.query(`
    insert into public.johanna_abandonment_one_shot_commands (
      command_key, semantic_fingerprint, rollout_scope, purchase_intent_id,
      scope_key, scope_version, runtime_generation,
      chatwoot_account_id, chatwoot_inbox_id, target_phone,
      template_name, template_language, template_category, copy_version,
      max_messages, followups_allowed, status, failure_code,
      chatwoot_conversation_id, created_at, finalized_at, source_reevaluation_id
    ) values (
      $1, repeat($2, 64), 'johanna-precheckout-delayed-first-touch-v1', $3::uuid,
      'johanna-precheckout-delayed-first-touch-production', 1, 0,
      1, 9, $4, 'johanna_interes_precheckout_01', 'es_EC', 'MARKETING',
      'johanna-precheckout-delayed-first-touch-v1', 1, 0,
      'delivery_unknown', $5, $6, $7::timestamptz, $7::timestamptz, $8::uuid
    ) returning id
  `, [
    `operator-disposition:${suffix}`,
    suffix.slice(-1),
    variantIntentId,
    `12025551${suffix}`,
    failureCode,
    conversationId,
    variantFinalizedAt,
    variantTimer.rows[0].id,
  ]);
  await db.exec('set role service_role');
  return variantCommand.rows[0].id;
}

async function expectDispositionBlocked(blockedCommandId, marker) {
  let blocked = false;
  let observedError = '';
  try {
    await db.query(`
      select * from public.resolve_johanna_one_shot_unverifiable_contact_deleted(
        $1::uuid, 'dan-operator'
      )
    `, [blockedCommandId]);
  } catch (error) {
    observedError = String(error);
    blocked = observedError.includes(marker);
  }
  if (!blocked) throw new Error(`expected disposition rejection: ${marker}; observed=${observedError}`);
}

const tooRecentId = await insertVariant({
  suffix: '0002',
  variantFinalizedAt: new Date().toISOString(),
  failureCode: 'chatwoot_http_error',
});
await expectDispositionBlocked(tooRecentId, 'johanna_one_shot_operator_disposition_ineligible');

const wrongFailureId = await insertVariant({
  suffix: '0003',
  variantFinalizedAt: finalizedAt,
  failureCode: 'chatwoot_protocol_error',
});
await expectDispositionBlocked(wrongFailureId, 'johanna_one_shot_operator_disposition_ineligible');

const providerIdPresent = await insertVariant({
  suffix: '0004',
  variantFinalizedAt: finalizedAt,
  failureCode: 'chatwoot_http_error',
  conversationId: 777,
});
await expectDispositionBlocked(providerIdPresent, 'johanna_one_shot_operator_disposition_ineligible');

const undisposedReadiness = await db.query(
  'select * from public.get_precheckout_delayed_first_touch_readiness()',
);
await db.exec('reset role');
const physicalUnknowns = await db.query(`
  select count(*)::integer as count
  from public.johanna_abandonment_one_shot_commands
  where status = 'delivery_unknown'
`);
if (undisposedReadiness.rows[0]?.delivery_unknown_count !== 3
    || physicalUnknowns.rows[0]?.count !== 4) {
  throw new Error(`undisposed unknowns were hidden: ${JSON.stringify({
    readiness: undisposedReadiness.rows,
    physical: physicalUnknowns.rows,
  })}`);
}

await db.exec('reset role');
let directMutationBlocked = false;
try {
  await db.query(`
    update public.johanna_one_shot_operator_dispositions
    set operator_ref = 'rewritten-operator'
    where command_id = $1::uuid
  `, [commandId]);
} catch (error) {
  directMutationBlocked = String(error).includes('johanna_one_shot_operator_disposition_immutable');
}
if (!directMutationBlocked) throw new Error('disposition ledger was mutable');

await db.exec('set role service_role');
const serviceReplay = await db.query(`
  select * from public.resolve_johanna_one_shot_unverifiable_contact_deleted(
    $1::uuid, 'dan-operator'
  )
`, [commandId]);
let directReadBlocked = false;
try {
  await db.query('select * from public.johanna_one_shot_operator_dispositions');
} catch {
  directReadBlocked = true;
}
await db.exec('reset role');
if (serviceReplay.rows[0]?.outcome !== 'replay' || !directReadBlocked) {
  throw new Error('service-role RPC/table boundary diverged');
}

await db.exec('set role anon');
let anonExecuteBlocked = false;
try {
  await db.query(`
    select * from public.resolve_johanna_one_shot_unverifiable_contact_deleted(
      $1::uuid, 'dan-operator'
    )
  `, [commandId]);
} catch {
  anonExecuteBlocked = true;
}
await db.exec('reset role');
if (!anonExecuteBlocked) throw new Error('anon could execute disposition RPC');

console.log('JOHANNA_ONE_SHOT_OPERATOR_DISPOSITION_SQL_OK');
await db.close();
