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

const signature = 'public.complete_operator_correlation_preresolution(uuid,uuid,bigint,text,uuid,jsonb,text,text,text)';
const oldSignature = 'public.complete_operator_correlation_preresolution(uuid,uuid,bigint,text,uuid,jsonb,text,text)';
const acl = (await db.query(`
  select
    to_regprocedure('${signature}') is not null as new_exists,
    to_regprocedure('${oldSignature}') is not null as old_exists,
    has_function_privilege('service_role', '${signature}', 'execute') as service_execute,
    has_function_privilege('service_role', '${oldSignature}', 'execute') as old_service_execute,
    has_function_privilege('anon', '${signature}', 'execute') as anon_execute,
    has_function_privilege('authenticated', '${signature}', 'execute') as authenticated_execute
`)).rows[0];
if (!acl?.new_exists || !acl?.old_exists || !acl?.service_execute
    || !acl?.old_service_execute || acl?.anon_execute || acl?.authenticated_execute) {
  throw new Error(`abstention RPC ACL mismatch: ${JSON.stringify(acl)}`);
}

const columns = (await db.query(`
  select column_name
  from information_schema.columns
  where table_schema = 'public'
    and table_name = 'operator_correlation_preresolutions'
    and column_name = 'decision_reason_code'
`)).rows;
if (columns.length !== 1) throw new Error('decision_reason_code column missing');

const scope = '71000000-0000-4000-8000-000000000001';
const event = '72000000-0000-4000-8000-000000000001';
const candidate1 = '73000000-0000-4000-8000-000000000001';
const candidate2 = '73000000-0000-4000-8000-000000000002';
await db.exec(`
  insert into public.hotmart_purchase_intent_scopes (
    id, tenant_ref, funnel_ref, hotmart_product_id,
    purchase_intent_product_ref, offer_ref, max_lookback, active
  ) values (
    '${scope}', 'reason-test', 'reason-main', '99701',
    'reason-product', 'reason-offer', interval '1 day', false
  );
  insert into public.purchase_intents (
    id, tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
    normalized_email, normalized_phone, submitted_at,
    whatsapp_contact_authorized, provisional, provider_observed,
    activation_authorized
  ) values
    ('${candidate1}', 'reason-test', 'reason-main', 'landing-a',
     'reason-product', 'reason-offer', 'one@example.test', null,
     '2026-09-17T11:59:00Z', false, true, false, false),
    ('${candidate2}', 'reason-test', 'reason-main', 'landing-a',
     'reason-product', 'reason-offer', 'two@example.test', null,
     '2026-09-17T10:00:00Z', false, true, false, false);
  insert into public.webhook_events (id, source, external_event_id, event_type, payload)
  values ('${event}', 'hotmart', 'reason-event', 'PURCHASE_APPROVED', '{}'::jsonb);
  insert into public.hotmart_purchase_intent_correlations (
    webhook_event_id, scope_id, event_type, outcome, purchase_intent_id,
    matched_by, candidate_count, reason_code, manual_handoff_required, observed_at
  ) values (
    '${event}', '${scope}', 'PURCHASE_APPROVED', 'ambiguous', null, null,
    2, 'multiple_candidates', true, '2026-09-17T12:00:00Z'
  );
  insert into public.hotmart_purchase_intent_correlation_candidates (
    webhook_event_id, purchase_intent_id, email_match, phone_match
  ) values
    ('${event}', '${candidate1}', true, false),
    ('${event}', '${candidate2}', true, false);
`);

await db.exec('set role service_role');
const claim = (await db.query(`
  select * from public.claim_operator_correlation_preresolutions(
    'reason-test', 'reason-main', 'reason-worker', 120
  )
`)).rows[0];
if (!claim) throw new Error('missing abstention reason claim');
const completion = (await db.query(`
  select * from public.complete_operator_correlation_preresolution(
    '${event}', '${claim.claim_token}', ${claim.lease_generation},
    'abstained', null, '[]'::jsonb, 'resolver-model',
    'correlation-preresolution-v1', 'model_abstained_missing_information'
  )
`)).rows[0];
if (completion?.status !== 'abstained') {
  throw new Error(`abstention did not complete: ${JSON.stringify(completion)}`);
}
await db.exec('reset role');

const persisted = (await db.query(`
  select status, decision_reason_code
  from public.operator_correlation_preresolutions
  where webhook_event_id = '${event}'
`)).rows[0];
if (persisted?.status !== 'abstained'
    || persisted?.decision_reason_code !== 'model_abstained_missing_information') {
  throw new Error(`bounded reason was not persisted: ${JSON.stringify(persisted)}`);
}

let nullReasonRejected = false;
try {
  await db.exec(`
    update public.operator_correlation_preresolutions
    set decision_reason_code = null
    where webhook_event_id = '${event}'
  `);
} catch {
  nullReasonRejected = true;
}
if (!nullReasonRejected) throw new Error('abstained row accepted a null reason');

const legacyClaim = '74000000-0000-4000-8000-000000000001';
await db.exec(`
  update public.operator_correlation_preresolutions
  set status = 'leased',
      decision_reason_code = null,
      claim_token = '${legacyClaim}',
      lease_generation = lease_generation + 1,
      lease_owner = 'legacy-worker',
      lease_expires_at = clock_timestamp() + interval '2 minutes',
      decided_at = null,
      updated_at = clock_timestamp()
  where webhook_event_id = '${event}'
`);
await db.exec('set role service_role');
const legacyCompletion = (await db.query(`
  select * from public.complete_operator_correlation_preresolution(
    '${event}', '${legacyClaim}', ${Number(claim.lease_generation) + 1},
    'abstained', null, '[]'::jsonb, 'resolver-model',
    'correlation-preresolution-v1'
  )
`)).rows[0];
await db.exec('reset role');
const legacyPersisted = (await db.query(`
  select status, decision_reason_code
  from public.operator_correlation_preresolutions
  where webhook_event_id = '${event}'
`)).rows[0];
if (legacyCompletion?.status !== 'abstained'
    || legacyPersisted?.decision_reason_code !== 'legacy_unclassified') {
  throw new Error(`legacy RPC compatibility failed: ${JSON.stringify({
    legacyCompletion,
    legacyPersisted,
  })}`);
}

const nonUniqueClaim = '75000000-0000-4000-8000-000000000001';
await db.exec(`
  update public.operator_correlation_preresolutions
  set status = 'leased',
      decision_reason_code = null,
      recommendation_ref = null,
      recommended_purchase_intent_id = null,
      supporting_evidence = '[]'::jsonb,
      model_name = null,
      prompt_version = null,
      claim_token = '${nonUniqueClaim}',
      lease_generation = lease_generation + 1,
      lease_owner = 'non-unique-worker',
      lease_expires_at = clock_timestamp() + interval '2 minutes',
      evidence_snapshot = jsonb_build_object(
        'facts', jsonb_build_array(
          jsonb_build_object(
            'candidate_id', '${candidate1}',
            'kind', 'prior_verified_identity',
            'value', null,
            'independent', true,
            'discriminating', true
          ),
          jsonb_build_object(
            'candidate_id', '${candidate2}',
            'kind', 'prior_verified_identity',
            'value', null,
            'independent', true,
            'discriminating', true
          )
        )
      ),
      evidence_fingerprint = repeat('ab', 32),
      decided_at = null,
      updated_at = clock_timestamp()
  where webhook_event_id = '${event}'
`);
let nonUniqueRejected = false;
await db.exec('set role service_role');
try {
  await db.query(`
    select * from public.complete_operator_correlation_preresolution(
      '${event}', '${nonUniqueClaim}', ${Number(claim.lease_generation) + 2},
      'recommended', '${candidate1}',
      '[{"kind":"prior_verified_identity","value":null}]'::jsonb,
      'resolver-model', 'correlation-preresolution-v2', null
    )
  `);
} catch {
  nonUniqueRejected = true;
}
await db.exec('reset role');
if (!nonUniqueRejected) {
  throw new Error('recommendation accepted multiple discriminated candidates');
}
const projections = (await db.query(`
  select count(*)::integer as count
  from public.slack_correlation_notification_projection
  where source_event_id = '${event}'
`)).rows[0]?.count;
if (projections !== 0) throw new Error(`abstention projected to Slack: ${projections}`);

console.log('SLACK_CORRELATION_ABSTENTION_REASON_SQL_OK');
