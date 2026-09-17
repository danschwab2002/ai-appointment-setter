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
  create role service_role nologin bypassrls;
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
  alter default privileges in schema public grant all on tables to service_role;
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

const scope = '10000000-0000-4000-8000-000000000161';
const unmatched = '20000000-0000-4000-8000-000000000161';
const ambiguous = '20000000-0000-4000-8000-000000000162';
const nearCandidate = '30000000-0000-4000-8000-000000000161';
const farCandidate = '30000000-0000-4000-8000-000000000162';
await db.exec(`
  insert into public.hotmart_purchase_intent_scopes (
    id, tenant_ref, funnel_ref, hotmart_product_id,
    purchase_intent_product_ref, offer_ref, max_lookback, active
  ) values (
    '${scope}', 'ai-correlation-test', 'ai-correlation-main', '99161',
    'ai-product', 'ai-offer', interval '1 day', false
  );
  insert into public.purchase_intents (
    id, tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
    normalized_email, normalized_phone, submitted_at,
    whatsapp_contact_authorized, provisional, provider_observed,
    activation_authorized
  ) values
    ('${nearCandidate}', 'ai-correlation-test', 'ai-correlation-main', 'landing-a',
     'ai-product', 'ai-offer', 'near@example.test', '5491100000161',
     '2026-09-16T11:56:00Z', false, false, true, false),
    ('${farCandidate}', 'ai-correlation-test', 'ai-correlation-main', 'landing-a',
     'ai-product', 'ai-offer', 'far@example.test', '5491100000162',
     '2026-09-16T11:40:00Z', false, false, true, false);
  insert into public.webhook_events (id, source, external_event_id, event_type, payload)
  values
    ('${unmatched}', 'hotmart', 'ai-unmatched', 'PURCHASE_APPROVED', '{}'::jsonb),
    ('${ambiguous}', 'hotmart', 'ai-ambiguous', 'PURCHASE_APPROVED', '{}'::jsonb);
  insert into public.hotmart_purchase_intent_correlations (
    webhook_event_id, scope_id, event_type, outcome, purchase_intent_id,
    matched_by, candidate_count, reason_code, manual_handoff_required, observed_at
  ) values
    ('${unmatched}', '${scope}', 'PURCHASE_APPROVED', 'unmatched', null, null,
     0, 'identity_not_found', true, '2026-09-16T11:59:00Z'),
    ('${ambiguous}', '${scope}', 'PURCHASE_APPROVED', 'ambiguous', null, null,
     2, 'multiple_candidates', true, '2026-09-16T12:00:00Z');
  insert into public.hotmart_purchase_intent_correlation_candidates (
    webhook_event_id, purchase_intent_id, email_match, phone_match
  ) values
    ('${ambiguous}', '${nearCandidate}', true, false),
    ('${ambiguous}', '${farCandidate}', true, false);
`);

await db.exec('set role service_role');
for (const statement of [
  'select * from public.operator_correlation_preresolutions limit 0',
  'insert into public.operator_correlation_preresolutions default values',
  "update public.operator_correlation_preresolutions set status = 'pending' where false",
  'delete from public.operator_correlation_preresolutions where false',
  'truncate public.operator_correlation_preresolutions',
]) {
  let directAccessBlocked = false;
  try {
    await db.exec(statement);
  } catch {
    directAccessBlocked = true;
  }
  if (!directAccessBlocked) {
    throw new Error(`service_role direct ledger access was not blocked: ${statement}`);
  }
}
const legacyBypass = (await db.query(`
  select * from public.claim_slack_correlation_notifications_v2(
    'ai-correlation-test', 'ai-correlation-main', 'legacy-v2', 1, 60
  )
`)).rows;
if (legacyBypass.length !== 0) {
  throw new Error(`v2 bypassed AI gate: ${JSON.stringify(legacyBypass)}`);
}

const claims = [];
for (let index = 0; index < 2; index += 1) {
  const claim = (await db.query(`
    select * from public.claim_operator_correlation_preresolutions(
      'ai-correlation-test', 'ai-correlation-main', 'ai-worker', 120
    )
  `)).rows[0];
  if (!claim) throw new Error('missing AI pre-resolution claim');
  claims.push(claim);
  if (claim.outcome === 'unmatched') {
    const completion = (await db.query(`
      select * from public.complete_operator_correlation_preresolution(
        '${claim.webhook_event_id}', '${claim.claim_token}', ${claim.lease_generation},
        'suppressed_unmatched'
      )
    `)).rows[0];
    if (completion?.status !== 'suppressed_unmatched') {
      throw new Error(`unmatched was not suppressed: ${JSON.stringify(completion)}`);
    }
    continue;
  }

  const evidence = (await db.query(`
    select * from public.get_operator_correlation_preresolution_evidence(
      'ai-correlation-test', 'ai-correlation-main', '${claim.webhook_event_id}',
      '${claim.claim_token}', ${claim.lease_generation}
    )
  `)).rows[0]?.evidence_data;
  if (JSON.stringify(evidence).includes('near@example.test')
      || JSON.stringify(evidence).includes('5491100000161')) {
    throw new Error('PII leaked into model evidence');
  }
  const nearFact = evidence?.facts?.find((fact) => (
    fact.candidate_id === nearCandidate
    && fact.kind === 'precheckout_time_proximity_minutes'
  ));
  if (nearFact?.value !== 4 || nearFact?.independent !== true
      || nearFact?.discriminating !== true) {
    throw new Error(`bounded timing evidence invalid: ${JSON.stringify(evidence)}`);
  }

  const completion = (await db.query(`
    select * from public.complete_operator_correlation_preresolution(
      '${claim.webhook_event_id}', '${claim.claim_token}', ${claim.lease_generation},
      'recommended', '${nearCandidate}',
      '[{"kind":"precheckout_time_proximity_minutes","value":4}]'::jsonb,
      'resolver-model', 'correlation-preresolution-v1'
    )
  `)).rows[0];
  if (!completion?.recommendation_ref || completion?.status !== 'recommended') {
    throw new Error(`recommendation did not persist: ${JSON.stringify(completion)}`);
  }
}

await db.exec('reset role');
await db.exec(`
  update public.operator_correlation_preresolutions
  set evidence_fingerprint = repeat('0', 64)
  where webhook_event_id = '${ambiguous}';
`);
await db.exec('set role service_role');
const corruptedClaim = (await db.query(`
  select * from public.claim_slack_correlation_notifications_v3(
    'ai-correlation-test', 'ai-correlation-main', 'slack-v3-corrupted', 1, 60
  )
`)).rows;
if (corruptedClaim.length !== 0) {
  throw new Error(`corrupted evidence fingerprint was claimable: ${JSON.stringify(corruptedClaim)}`);
}
await db.exec('reset role');
await db.exec(`
  update public.operator_correlation_preresolutions
  set evidence_fingerprint = encode(
    sha256(convert_to(evidence_snapshot::text, 'UTF8')),
    'hex'
  )
  where webhook_event_id = '${ambiguous}';
`);
await db.exec('set role service_role');
const slackClaim = (await db.query(`
  select * from public.claim_slack_correlation_notifications_v3(
    'ai-correlation-test', 'ai-correlation-main', 'slack-v3', 1, 60
  )
`)).rows[0];
if (slackClaim?.source_event_id !== ambiguous
    || slackClaim?.notification_contract_version !== 3
    || slackClaim?.recommendation_data?.candidate_id !== nearCandidate
    || slackClaim?.recommendation_data?.candidate_label !== 'Persona 1'
    || !/^[a-f0-9]{64}$/.test(
      slackClaim?.recommendation_data?.evidence_fingerprint ?? ''
    )) {
  throw new Error(`v3 recommendation claim invalid: ${JSON.stringify(slackClaim)}`);
}
await db.exec('reset role');
const unmatchedProjection = (await db.query(`
  select projection_status, notification_contract_version
  from public.slack_correlation_notification_projection
  where source_event_id = '${unmatched}'
`)).rows[0];
if (unmatchedProjection?.projection_status !== 'suppressed'
    || unmatchedProjection?.notification_contract_version !== null) {
  throw new Error(`unmatched projection was not suppressed: ${JSON.stringify(unmatchedProjection)}`);
}

await db.exec('reset role');
await db.exec('set role anon');
let anonBlocked = false;
try {
  await db.query(`
    select * from public.claim_operator_correlation_preresolutions(
      'ai-correlation-test', 'ai-correlation-main', 'anon-worker', 120
    )
  `);
} catch {
  anonBlocked = true;
}
if (!anonBlocked) throw new Error('anon could execute AI pre-resolution claim');

console.log('SLACK_CORRELATION_AI_PRERESOLUTION_SQL_OK');
