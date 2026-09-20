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
const newRows = (await db.query(`
  select * from public.claim_slack_correlation_notifications_v2(
    'event-copy-test', 'event-copy-main', 'worker-copy', 1, 60
  )
`)).rows;
if (newRows.length !== 0) {
  throw new Error(`v2 worker bypassed pre-resolution gate: ${JSON.stringify(newRows)}`);
}
await db.exec('reset role');
const suppressed = (await db.query(`
  select count(*)::integer as count
  from public.slack_correlation_notification_projection
  where source_event_id = any(array[${events.map(([id]) => `'${id}'::uuid`).join(',')}])
    and projection_status = 'suppressed'
    and notification_contract_version is null
`)).rows[0]?.count;
if (suppressed !== events.length) {
  throw new Error(`new projections were not suppressed: ${suppressed}`);
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
await db.exec(`
  alter table public.slack_correlation_notification_projection disable trigger
    slack_correlation_preresolution_gate;
  insert into public.slack_correlation_notification_projection (
    source_event_id, tenant_ref, funnel_ref, outcome, reason_code,
    candidate_count, occurred_at, projection_status,
    notification_contract_version, attempt_count, lease_generation,
    lease_owner, claim_token, lease_expires_at
  ) values (
    '${legacyId}', 'event-copy-test', 'event-copy-main', 'conflict',
    'email_phone_conflict', 1, '2026-09-13T12:04:00Z', 'leased',
    1, 1, 1, 'old-worker', '40000000-0000-4000-8000-000000000094',
    clock_timestamp() - interval '1 second'
  );
  alter table public.slack_correlation_notification_projection enable trigger
    slack_correlation_preresolution_gate;
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
