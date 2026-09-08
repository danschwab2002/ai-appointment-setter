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

const eventId = '01K4N9YQ2T7W3H5J8M6P0R1SVC';
const sessionId = '01K4N9YQ2T7W3H5J8M6P0R1SVD';
const rpc = (eventType) => `
  select * from public.admit_johanna_funnel_event_v1(
    '1.0.0', '${eventId}', '${eventType}',
    '2026-09-08T08:00:00Z', '${sessionId}', 'ads-a', 'bxjge6zq',
    'meta', 'paid_social', null, null, null
  )
`;
await db.exec('set role service_role');
let staleEventBlocked = false;
try {
  await db.query(`
    select * from public.admit_johanna_funnel_event_v1(
      '1.0.0', '01K4N9YQ2T7W3H5J8M6P0R1SVE', 'page_view',
      '2000-01-01T00:00:00Z', '01K4N9YQ2T7W3H5J8M6P0R1SVF',
      'ads-a', 'bxjge6zq', null, null, null, null, null
    )
  `);
} catch (error) {
  staleEventBlocked = String(error).includes('invalid_johanna_funnel_event');
}
if (!staleEventBlocked) {
  throw new Error('stale funnel event was admitted');
}
let futureEventBlocked = false;
try {
  await db.query(`
    select * from public.admit_johanna_funnel_event_v1(
      '1.0.0', '01K4N9YQ2T7W3H5J8M6P0R1SVH', 'page_view',
      statement_timestamp() + interval '6 minutes',
      '01K4N9YQ2T7W3H5J8M6P0R1SVJ',
      'ads-a', 'bxjge6zq', null, null, null, null, null
    )
  `);
} catch (error) {
  futureEventBlocked = String(error).includes('invalid_johanna_funnel_event');
}
if (!futureEventBlocked) {
  throw new Error('future funnel event beyond tolerance was admitted');
}
const outsideWindow = (await db.query(`
  select * from public.admit_johanna_funnel_event_v1(
    '1.0.0', '01K4N9YQ2T7W3H5J8M6P0R1SVG', 'page_view',
    statement_timestamp() - interval '8 days', '${sessionId}',
    'ads-a', 'bxjge6zq', null, null, null, null, null
  )
`)).rows[0];
if (outsideWindow?.outcome !== 'inserted') {
  throw new Error(`outside-window setup failed: ${JSON.stringify(outsideWindow)}`);
}
const inserted = (await db.query(rpc('page_view'))).rows[0];
const duplicate = (await db.query(rpc('page_view'))).rows[0];
const conflict = (await db.query(rpc('preform_opened'))).rows[0];
if (inserted?.outcome !== 'inserted'
    || duplicate?.outcome !== 'duplicate'
    || conflict?.outcome !== 'semantic_conflict') {
  throw new Error(`funnel replay contract failed: ${JSON.stringify({ inserted, duplicate, conflict })}`);
}
const submittedAt = new Date().toISOString();
const rawPrecheckout = {
  id: sessionId,
  event: 'PRECHECKOUT_FORM_SUBMITTED',
  version: '1.0.0-emulated',
  created_at: submittedAt,
  lead: { full_name: 'Synthetic Test', phone_e164: '+12025550123' },
};
const canonicalPrecheckout = {
  external_submission_id: sessionId,
  event_type: 'PRECHECKOUT_FORM_SUBMITTED',
  contract_version: '1.0.0-emulated',
  submitted_at: submittedAt,
  source: {
    tenant_ref: 'lancemos',
    funnel_ref: 'psicologajohanna',
    landing_ref: 'ads-a',
  },
  lead: { full_name: 'Synthetic Test' },
  identity: { email: null, phone: '12025550123', phone_country_iso: null },
  commerce: { product_ref: 'F106691755G', offer_ref: 'bxjge6zq' },
  consent: {
    terms_accepted: false,
    privacy_accepted: false,
    whatsapp_contact: false,
    copy_version: 'observability-sql-probe',
  },
  assurance: {
    provisional: true,
    provider_observed: false,
    activation_authorized: false,
  },
};
const precheckout = await db.query(
  'select * from public.admit_precheckout_form_submission($1, $2::jsonb, $3::jsonb)',
  [sessionId, JSON.stringify(rawPrecheckout), JSON.stringify(canonicalPrecheckout)],
);
if (precheckout.rows[0]?.outcome !== 'inserted') {
  throw new Error(`precheckout setup failed: ${JSON.stringify(precheckout.rows)}`);
}
const dashboard = await db.query(`
  select * from public.read_johanna_funnel_dashboard_v2(7)
`);
const metadata = dashboard.rows.filter((row) => row.row_kind === 'meta');
const cases = dashboard.rows.filter((row) => row.row_kind === 'case');
if (metadata.length !== 1
    || !metadata[0]?.snapshot_at
    || !metadata[0]?.window_start
    || cases.length !== 1
    || cases[0]?.page_view_count !== 1
    || cases[0]?.last_funnel_event_type !== 'page_view'
    || String(cases[0]?.snapshot_at) !== String(metadata[0]?.snapshot_at)
    || String(cases[0]?.window_start) !== String(metadata[0]?.window_start)) {
  throw new Error(`correlated funnel aggregate missing: ${JSON.stringify(dashboard.rows)}`);
}
let directDmlBlocked = false;
try {
  await db.exec(`delete from public.johanna_funnel_events where event_id = '${eventId}'`);
} catch {
  directDmlBlocked = true;
}
await db.exec('reset role');
if (!directDmlBlocked) {
  throw new Error('service_role direct funnel-event DML was not blocked');
}
let ownerMutationBlocked = false;
try {
  await db.exec(`
    update public.johanna_funnel_events
    set utm_source = 'tampered'
    where event_id = '${eventId}'
  `);
} catch (error) {
  ownerMutationBlocked = String(error).includes(
    'johanna_funnel_events_are_append_only',
  );
}
if (!ownerMutationBlocked) {
  throw new Error('owner-level funnel-event mutation was not blocked');
}
const count = (await db.query(`
  select count(*)::integer as count from public.johanna_funnel_events
  where event_id = '${eventId}'
`)).rows[0]?.count;
if (count !== 1) {
  throw new Error(`append-only replay row count invalid: ${count}`);
}
console.log('JOHANNA_FUNNEL_OBSERVABILITY_SQL_OK');
