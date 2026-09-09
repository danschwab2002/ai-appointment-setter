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

const johannaScope = '10000000-0000-4000-8000-000000000001';
const att1Scope = '10000000-0000-4000-8000-000000000002';
const johannaEvent = '20000000-0000-4000-8000-000000000001';
const att1Event = '20000000-0000-4000-8000-000000000002';
await db.exec(`
  insert into public.hotmart_purchase_intent_scopes (
    id, tenant_ref, funnel_ref, hotmart_product_id,
    purchase_intent_product_ref, offer_ref, max_lookback, active
  ) values
    ('${johannaScope}', 'slack-johanna-test', 'johanna-main', '990001', 'johanna-product', 'johanna-offer', interval '1 day', false),
    ('${att1Scope}', 'slack-att1-test', 'att1-main', '990002', 'att1-product', 'att1-offer', interval '1 day', false);
  insert into public.webhook_events (id, source, external_event_id, event_type, payload)
  values
    ('${johannaEvent}', 'hotmart', 'slack-johanna-event', 'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb),
    ('${att1Event}', 'hotmart', 'slack-att1-event', 'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb);
  insert into public.hotmart_purchase_intent_correlations (
    webhook_event_id, scope_id, event_type, outcome, purchase_intent_id,
    matched_by, candidate_count, reason_code, manual_handoff_required, observed_at
  ) values
    ('${johannaEvent}', '${johannaScope}', 'PURCHASE_OUT_OF_SHOPPING_CART', 'unmatched', null, null, 0, 'identity_not_found', true, '2026-09-08T12:00:00Z'),
    ('${att1Event}', '${att1Scope}', 'PURCHASE_OUT_OF_SHOPPING_CART', 'ambiguous', null, null, 2, 'multiple_candidates', true, '2026-09-08T12:01:00Z');
`);

await db.exec('set role service_role');
let inactiveBindingRejected = false;
try {
  await db.query(`
    select * from public.claim_slack_correlation_notifications(
      'slack-att1-test', 'att1-main', 'worker-att1', 1, 60, 1
    )
  `);
} catch (error) {
  inactiveBindingRejected = String(error).includes('commercial_ally_binding_not_active');
}
if (!inactiveBindingRejected) {
  throw new Error('inactive commercial ally binding was not rejected');
}
const first = (await db.query(`
  select * from public.claim_slack_correlation_notifications(
    'slack-johanna-test', 'johanna-main', 'worker-a', 1, 60
  )
`)).rows;
if (first.length !== 1 || first[0]?.source_event_id !== johannaEvent
    || first[0]?.lease_generation !== 1) {
  throw new Error(`first scoped claim invalid: ${JSON.stringify(first)}`);
}
const blockedConcurrent = (await db.query(`
  select * from public.claim_slack_correlation_notifications(
    'slack-johanna-test', 'johanna-main', 'worker-b', 1, 60
  )
`)).rows;
if (blockedConcurrent.length !== 0) {
  throw new Error(`active lease was concurrently reclaimed: ${JSON.stringify(blockedConcurrent)}`);
}
const release = (await db.query(`
  select * from public.release_slack_correlation_notification(
    '${johannaEvent}', '${first[0].claim_token}', 1, 'connector_admission_unknown'
  )
`)).rows[0];
if (release?.applied !== true) {
  throw new Error(`retryable release failed: ${JSON.stringify(release)}`);
}
const prematureRetry = (await db.query(`
  select * from public.claim_slack_correlation_notifications(
    'slack-johanna-test', 'johanna-main', 'worker-b', 1, 60
  )
`)).rows;
if (prematureRetry.length !== 0) {
  throw new Error(`bounded backoff was bypassed: ${JSON.stringify(prematureRetry)}`);
}
await db.exec('reset role');
await db.exec(`
  update public.slack_correlation_notification_projection
  set next_attempt_at = clock_timestamp() - interval '1 second'
  where source_event_id = '${johannaEvent}'
`);
await db.exec('set role service_role');
const retry = (await db.query(`
  select * from public.claim_slack_correlation_notifications(
    'slack-johanna-test', 'johanna-main', 'worker-b', 1, 60
  )
`)).rows;
if (retry.length !== 1 || retry[0]?.lease_generation !== 2
    || retry[0]?.claim_token === first[0]?.claim_token) {
  throw new Error(`fenced retry claim invalid: ${JSON.stringify(retry)}`);
}
const completed = (await db.query(`
  select * from public.complete_slack_correlation_notification(
    '${johannaEvent}', '${retry[0].claim_token}', 2,
    '30000000-0000-4000-8000-000000000001'
  )
`)).rows[0];
if (completed?.applied !== true) {
  throw new Error(`completion failed: ${JSON.stringify(completed)}`);
}
const staleFinalize = (await db.query(`
  select * from public.complete_slack_correlation_notification(
    '${johannaEvent}', '${first[0].claim_token}', 1,
    '30000000-0000-4000-8000-000000000001'
  )
`)).rows[0];
if (staleFinalize?.applied !== false) {
  throw new Error(`stale lease finalized projection: ${JSON.stringify(staleFinalize)}`);
}
const att1 = (await db.query(`
  select * from public.claim_slack_correlation_notifications(
    'slack-att1-test', 'att1-main', 'worker-att1', 1, 60
  )
`)).rows;
if (att1.length !== 1 || att1[0]?.source_event_id !== att1Event) {
  throw new Error(`ATT1 scoped claim invalid: ${JSON.stringify(att1)}`);
}
let directDmlBlocked = false;
try {
  await db.exec(`delete from public.slack_correlation_notification_projection`);
} catch {
  directDmlBlocked = true;
}
if (!directDmlBlocked) {
  throw new Error('service_role direct projection DML was not blocked');
}
await db.exec('reset role');

console.log('SLACK_CORRELATION_PROJECTION_SQL_OK');
