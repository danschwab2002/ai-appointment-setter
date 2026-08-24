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

await db.exec(`
insert into public.purchase_intents (
  id, tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
  normalized_email, normalized_phone, submitted_at, lifecycle_state,
  current_classification, whatsapp_contact_authorized, provisional,
  provider_observed, activation_authorized
) values
(
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1', 'lancemos',
  'psicologajohanna', 'fixture', 'f106691755g', 'bxjge6zq',
  'a@example.com', '593999999991', '2026-08-24T09:00:00Z',
  'waiting_for_purchase', 'tracking_incomplete', false, true, false, false
),
(
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2', 'lancemos',
  'psicologajohanna', 'fixture', 'f106691755g', 'bxjge6zq',
  'a@example.com', '593999999992', '2026-08-24T09:05:00Z',
  'waiting_for_purchase', 'tracking_incomplete', false, true, false, false
),
(
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3', 'lancemos',
  'psicologajohanna', 'fixture', 'f106691755g', 'bxjge6zq',
  'resolved@example.com', '593999999993', '2026-08-24T09:10:00Z',
  'purchased', null, false, true, true, false
),
(
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4', 'foreign-tenant',
  'foreign-funnel', 'fixture', 'foreign-product', 'foreign-offer',
  'victim@foreign.example', '12025554567', '2026-08-24T09:15:00Z',
  'waiting_for_purchase', 'tracking_incomplete', false, true, false, false
);

insert into public.webhook_events (
  id, source, external_event_id, event_type, payload,
  processing_status, received_at
) values
(
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1', 'hotmart', 'fixture-ambiguous',
  'PURCHASE_APPROVED', '{}'::jsonb, 'received', '2026-08-24T10:00:00Z'
),
(
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2', 'hotmart', 'fixture-unmatched',
  'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb, 'received',
  '2026-08-24T10:05:00Z'
),
(
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3', 'hotmart', 'fixture-resolved',
  'PURCHASE_APPROVED', '{}'::jsonb, 'received', '2026-08-24T10:10:00Z'
);

insert into public.hotmart_purchase_intent_event_identities (
  webhook_event_id, normalized_email, normalized_phone
) values
(
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1',
  'a@example.com', '593999999999'
),
(
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2',
  'missing@example.com', '593988888888'
),
(
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3',
  'resolved@example.com', '593999999993'
);

insert into public.hotmart_purchase_intent_correlations (
  webhook_event_id, scope_id, event_type, outcome, purchase_intent_id,
  matched_by, candidate_count, reason_code, manual_handoff_required,
  observed_at
) values
(
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1',
  (select id from public.hotmart_purchase_intent_scopes where active limit 1),
  'PURCHASE_APPROVED', 'ambiguous', null, null, 2,
  'multiple_candidates', true, '2026-08-24T10:00:00Z'
),
(
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2',
  (select id from public.hotmart_purchase_intent_scopes where active limit 1),
  'PURCHASE_OUT_OF_SHOPPING_CART', 'unmatched', null, null, 0,
  'identity_not_found', true, '2026-08-24T10:05:00Z'
),
(
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3',
  (select id from public.hotmart_purchase_intent_scopes where active limit 1),
  'PURCHASE_APPROVED', 'resolved',
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3', 'email_and_phone', 1,
  'exact_email_and_phone', false, '2026-08-24T10:10:00Z'
);

insert into public.hotmart_purchase_intent_correlation_candidates (
  webhook_event_id, purchase_intent_id, email_match, phone_match
) values
(
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1',
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1', true, false
),
(
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1',
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2', true, false
),
(
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3',
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3', true, true
),
(
  'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1',
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4', true, false
);
`);

const before = await db.query(`
  select
    (select count(*)::integer from public.hotmart_purchase_intent_correlations)
      correlations,
    (select count(*)::integer
     from public.hotmart_purchase_intent_correlation_candidates) candidates,
    (select count(*)::integer from public.purchase_intents) intents
`);

const listed = await db.query(`
  select case_data
  from public.list_operator_unresolved_correlations(
    'lancemos', 'psicologajohanna', 20, null
  )
`);
if (listed.rows.length !== 2) {
  throw new Error(`unresolved list diverged: ${JSON.stringify(listed.rows)}`);
}
const listedCases = listed.rows.map((row) => row.case_data);
if (listedCases[0]?.webhook_event_id !== 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2'
    || listedCases[1]?.webhook_event_id !== 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1'
    || listedCases.some((row) => row.outcome === 'resolved')) {
  throw new Error(`unresolved ordering/filter diverged: ${JSON.stringify(listedCases)}`);
}
const ambiguous = listedCases[1];
const rendered = JSON.stringify(ambiguous);
if (ambiguous?.identity?.masked_email !== '***@example.com'
    || ambiguous?.identity?.masked_phone !== '********9999'
    || ambiguous?.candidates?.length !== 2
    || rendered.includes('a@example.com')
    || rendered.includes('593999999999')
    || rendered.includes('victim@foreign.example')
    || rendered.includes('12025554567')) {
  throw new Error(`identity masking diverged: ${rendered}`);
}

const detail = await db.query(`
  select case_data
  from public.get_operator_unresolved_correlation(
    'lancemos',
    'psicologajohanna',
    'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1'::uuid
  )
`);
if (detail.rows.length !== 1
    || detail.rows[0]?.case_data?.outcome !== 'ambiguous') {
  throw new Error(`exact detail diverged: ${JSON.stringify(detail.rows)}`);
}
const resolvedDetail = await db.query(`
  select case_data
  from public.get_operator_unresolved_correlation(
    'lancemos',
    'psicologajohanna',
    'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3'::uuid
  )
`);
if (resolvedDetail.rows.length !== 0) {
  throw new Error('resolved correlation leaked into operator review');
}
const foreignScope = await db.query(`
  select case_data
  from public.list_operator_unresolved_correlations(
    'otro-tenant', 'otro-funnel', 20, null
  )
`);
if (foreignScope.rows.length !== 0) {
  throw new Error('operator read crossed tenant scope');
}

const after = await db.query(`
  select
    (select count(*)::integer from public.hotmart_purchase_intent_correlations)
      correlations,
    (select count(*)::integer
     from public.hotmart_purchase_intent_correlation_candidates) candidates,
    (select count(*)::integer from public.purchase_intents) intents
`);
if (JSON.stringify(before.rows[0]) !== JSON.stringify(after.rows[0])) {
  throw new Error('read RPC changed durable state');
}

let directReadBlocked = false;
try {
  await db.exec(`
    set role service_role;
    select * from public.purchase_intents limit 1;
    reset role;
  `);
} catch (error) {
  directReadBlocked = String(error).includes('permission denied');
  await db.exec('reset role');
}
if (!directReadBlocked) throw new Error('service_role direct PII read was not blocked');

await db.exec('set role service_role');
const serviceRead = await db.query(`
  select case_data from public.list_operator_unresolved_correlations(
    'lancemos', 'psicologajohanna', 1, null
  )
`);
await db.exec('reset role');
if (serviceRead.rows.length !== 1) {
  throw new Error('service_role could not execute operator read RPC');
}

let anonBlocked = false;
try {
  await db.exec(`
    set role anon;
    select case_data from public.list_operator_unresolved_correlations(
      'lancemos', 'psicologajohanna', 1, null
    );
    reset role;
  `);
} catch (error) {
  anonBlocked = String(error).includes('permission denied');
  await db.exec('reset role');
}
if (!anonBlocked) throw new Error('anon executed operator read RPC');

console.log('operator_correlation_review_read=OK');
console.log('operator_correlation_review_pii_masking=OK');
console.log('operator_correlation_review_acl=OK');
await db.close();
