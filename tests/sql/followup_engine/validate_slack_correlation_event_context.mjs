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

const scope = '10000000-0000-4000-8000-000000000099';
const events = [
  ['20000000-0000-4000-8000-000000000091', 'PURCHASE_APPROVED', 'event-approved'],
  ['20000000-0000-4000-8000-000000000092', 'PURCHASE_OUT_OF_SHOPPING_CART', 'event-cart'],
  ['20000000-0000-4000-8000-000000000093', 'PURCHASE_CANCELED', 'event-canceled'],
];
await db.exec(`
  insert into public.hotmart_purchase_intent_scopes (
    id, tenant_ref, funnel_ref, hotmart_product_id,
    purchase_intent_product_ref, offer_ref, max_lookback, active
  ) values (
    '${scope}', 'event-copy-test', 'event-copy-main', '990099',
    'event-copy-product', 'event-copy-offer', interval '1 day', false
  );
  ${events.map(([id, type, external], index) => `
    insert into public.webhook_events (id, source, external_event_id, event_type, payload)
    values ('${id}', 'hotmart', '${external}', '${type}', '{}'::jsonb);
    insert into public.hotmart_purchase_intent_correlations (
      webhook_event_id, scope_id, event_type, outcome, purchase_intent_id,
      matched_by, candidate_count, reason_code, manual_handoff_required, observed_at
    ) values (
      '${id}', '${scope}', '${type}', 'unmatched', null, null, 0,
      'identity_not_found', true, '2026-09-13T12:0${index}:00Z'
    );
  `).join('\n')}
`);

await db.exec('set role service_role');
for (let index = 0; index < events.length; index += 1) {
  const [expectedId, expectedType] = events[index];
  const rows = (await db.query(`
    select * from public.claim_slack_correlation_notifications_v2(
      'event-copy-test', 'event-copy-main', 'worker-copy', 1, 60
    )
  `)).rows;
  if (rows.length !== 1
      || rows[0]?.source_event_id !== expectedId
      || rows[0]?.source_event_type !== expectedType
      || rows[0]?.notification_contract_version !== 2) {
    throw new Error(`event-specific claim invalid: ${JSON.stringify(rows)}`);
  }
  const applied = (await db.query(`
    select * from public.complete_slack_correlation_notification(
      '${expectedId}', '${rows[0].claim_token}', ${rows[0].lease_generation},
      '30000000-0000-4000-8000-00000000009${index}'
    )
  `)).rows[0]?.applied;
  if (applied !== true) {
    throw new Error(`event-specific claim did not complete: ${expectedId}`);
  }
}

const legacyId = '20000000-0000-4000-8000-000000000094';
await db.exec('reset role');
await db.exec(`
  insert into public.webhook_events (id, source, external_event_id, event_type, payload)
  values ('${legacyId}', 'hotmart', 'event-legacy-reclaim',
          'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb);
  insert into public.hotmart_purchase_intent_correlations (
    webhook_event_id, scope_id, event_type, outcome, purchase_intent_id,
    matched_by, candidate_count, reason_code, manual_handoff_required, observed_at
  ) values (
    '${legacyId}', '${scope}', 'PURCHASE_OUT_OF_SHOPPING_CART', 'conflict',
    null, null, 1, 'email_phone_conflict', true, '2026-09-13T12:04:00Z'
  );
`);
await db.exec('set role service_role');
const legacyClaim = (await db.query(`
  select * from public.claim_slack_correlation_notifications(
    'event-copy-test', 'event-copy-main', 'old-worker', 1, 30
  )
`)).rows[0];
if (legacyClaim?.source_event_id !== legacyId) {
  throw new Error(`old worker did not claim legacy row: ${JSON.stringify(legacyClaim)}`);
}
await db.exec('reset role');
await db.exec(`
  update public.slack_correlation_notification_projection
  set lease_expires_at = clock_timestamp() - interval '1 second'
  where source_event_id = '${legacyId}'
`);
await db.exec('set role service_role');
const reclaimed = (await db.query(`
  select * from public.claim_slack_correlation_notifications_v2(
    'event-copy-test', 'event-copy-main', 'new-worker', 1, 30
  )
`)).rows[0];
if (reclaimed?.source_event_id !== legacyId
    || reclaimed?.source_event_type !== 'PURCHASE_OUT_OF_SHOPPING_CART'
    || reclaimed?.notification_contract_version !== 1) {
  throw new Error(`new worker changed legacy contract: ${JSON.stringify(reclaimed)}`);
}
const legacyApplied = (await db.query(`
  select * from public.complete_slack_correlation_notification(
    '${legacyId}', '${reclaimed.claim_token}', ${reclaimed.lease_generation},
    '30000000-0000-4000-8000-000000000099'
  )
`)).rows[0]?.applied;
if (legacyApplied !== true) {
  throw new Error('reclaimed legacy projection did not complete');
}
await db.exec('reset role');

await db.exec('set role anon');
let anonBlocked = false;
try {
  await db.query(`
    select * from public.claim_slack_correlation_notifications_v2(
      'event-copy-test', 'event-copy-main', 'anon-worker', 1, 60
    )
  `);
} catch {
  anonBlocked = true;
}
if (!anonBlocked) {
  throw new Error('anon could execute event-specific correlation claim');
}

console.log('SLACK_CORRELATION_EVENT_CONTEXT_SQL_OK');
