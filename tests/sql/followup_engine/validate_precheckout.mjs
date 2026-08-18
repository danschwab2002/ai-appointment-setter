import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { PGlite } from '@electric-sql/pglite';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const read = (path) => readFile(`${root}/${path}`, 'utf8');
const baseline = (await read('supabase/baseline/20260803_public_schema.sql'))
  .replace('create extension if not exists pgcrypto;', '-- omitted in PGlite');
const migration = await read(
  'supabase/migrations/20260814000200_precheckout_purchase_intents.sql',
);

const db = new PGlite();
await db.waitReady;
await db.exec(baseline);
await db.exec(migration);

const raw = {
  id: 'precheckout-sql-001',
  event: 'PRECHECKOUT_FORM_SUBMITTED',
  version: '1.0.0-emulated',
  created_at: '2099-01-01T00:00:00Z',
  lead: { full_name: 'Lead de Prueba', phone_e164: '+12025550123' },
};
const canonical = {
  external_submission_id: raw.id,
  event_type: raw.event,
  contract_version: raw.version,
  submitted_at: raw.created_at,
  source: {
    tenant_ref: 'joana',
    funnel_ref: 'libre-de-ansiedad',
    landing_ref: 'bcl-main',
  },
  lead: { full_name: raw.lead.full_name },
  identity: { email: null, phone: '12025550123', phone_country_iso: null },
  commerce: { product_ref: 'F106691755G', offer_ref: 'bxjge6zq' },
  consent: {
    terms_accepted: false,
    privacy_accepted: false,
    whatsapp_contact: false,
    copy_version: 'form-screenshot-2026-08-14',
  },
  assurance: {
    provisional: true,
    provider_observed: false,
    activation_authorized: false,
  },
};

const admit = (rawPayload, canonicalPayload) => db.query(
  'select * from public.admit_precheckout_form_submission($1, $2::jsonb, $3::jsonb)',
  [raw.id, JSON.stringify(rawPayload), JSON.stringify(canonicalPayload)],
);

const inserted = await admit(raw, canonical);
const duplicate = await admit(raw, canonical);
const changedRaw = structuredClone(raw);
changedRaw.lead.full_name = 'Nombre Distinto';
const conflict = await admit(changedRaw, canonical);
const conflictReplay = await admit(changedRaw, canonical);

if (inserted.rows[0]?.outcome !== 'inserted'
    || duplicate.rows[0]?.outcome !== 'duplicate'
    || conflict.rows[0]?.outcome !== 'semantic_conflict') {
  throw new Error('precheckout admission outcomes diverged');
}
if (conflictReplay.rows[0]?.outcome !== 'semantic_conflict') {
  throw new Error('precheckout conflict replay outcome diverged');
}
if (inserted.rows[0]?.purchase_intent_id !== duplicate.rows[0]?.purchase_intent_id
    || inserted.rows[0]?.purchase_intent_id !== conflict.rows[0]?.purchase_intent_id) {
  throw new Error('precheckout replay changed purchase intent');
}

await db.query(`
  update public.precheckout_submission_conflicts
  set incoming_raw_payload = '{"tampered":true}'::jsonb
`);
let collisionRejected = false;
try {
  await admit(changedRaw, canonical);
} catch (error) {
  collisionRejected = String(error).includes(
    'precheckout_conflict_fingerprint_collision',
  );
}
if (!collisionRejected) {
  throw new Error('precheckout conflict fingerprint collision did not fail closed');
}

const state = await db.query(`
  select
    (select count(*)::integer from public.precheckout_submissions) submissions,
    (select count(*)::integer from public.purchase_intents) intents,
    (select count(*)::integer from public.purchase_intent_submissions) links,
    (select count(*)::integer from public.precheckout_submission_conflicts) conflicts,
    (select bool_and(not provisional) from public.purchase_intents) any_non_provisional,
    (select bool_or(provider_observed) from public.purchase_intents) provider_observed,
    (select bool_or(activation_authorized) from public.purchase_intents) activation_authorized,
    (select bool_or(whatsapp_contact_authorized) from public.purchase_intents)
      whatsapp_contact_authorized
`);
const row = state.rows[0];
if (row?.submissions !== 1 || row?.intents !== 1 || row?.links !== 1
    || row?.conflicts !== 1 || row?.any_non_provisional !== false
    || row?.provider_observed !== false || row?.activation_authorized !== false
    || row?.whatsapp_contact_authorized !== false) {
  throw new Error(`precheckout durable state diverged: ${JSON.stringify(row)}`);
}

console.log('precheckout_insert_replay_conflict=OK');
console.log('precheckout_activation_default_off=OK');
