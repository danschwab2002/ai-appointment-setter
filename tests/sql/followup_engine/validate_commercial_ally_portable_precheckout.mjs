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
const stack = [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(join(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => join(root, 'supabase/migrations', name)),
];
for (const file of stack) {
  await db.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}

const binding = {
  tenant_ref: 'att1',
  funnel_ref: 'att1-main',
  binding_version: 1,
  status: 'active',
  ally_ref: 'ally-one',
  lead_ally_name: 'Ally One',
  lead_site: 'ally-one-site',
  lead_landing_id: 'main',
  lead_page_host: 'ally-one.example',
  lead_page_path: '/offer/main',
  product_hotlink: 'ATT1HOTLINK',
  product_name: 'ATT1 Offer',
  product_price: '49',
  currency: 'USD',
  offer_code: 'att1offer',
  consent_copy_version: 'att1-whatsapp-v1',
  hotmart_product_id: 123456,
  chatwoot_account_id: 42,
  chatwoot_inbox_id: 24,
  inbound_scope_key: 'att1-inbound',
  inbound_scope_version: 1,
};
const columns = Object.keys(binding);
await db.query(
  `insert into public.commercial_ally_runtime_bindings (${columns.join(',')})
   values (${columns.map((_, index) => `$${index + 1}`).join(',')})`,
  Object.values(binding),
);
await db.query(`
  insert into public.commercial_ally_runtime_bindings
    (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
     lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
     product_name, product_price, currency, offer_code, consent_copy_version,
     hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
     inbound_scope_key, inbound_scope_version)
  select tenant_ref, funnel_ref, 2, 'draft', ally_ref, lead_ally_name,
         lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
         product_name, product_price, currency, offer_code, consent_copy_version,
         hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
         inbound_scope_key, inbound_scope_version
  from public.commercial_ally_runtime_bindings where binding_version = 1
`);

const payloads = (id) => {
  const raw = {
    id,
    event: 'lead.precheckout',
    version: '1.1.0',
    created_at: '2026-09-01T12:00:00Z',
    source: {
      system: 'landing', site: 'ally-one-site', aliado: 'Ally One',
      landing_id: 'main', page_url: 'https://ally-one.example/offer/main',
    },
    data: {
      buyer: {
        name: 'Test Buyer', email: 'buyer@example.test', phone: '+12025550123',
        phone_country_code: '1', phone_national: '2025550123',
      },
      product: {
        hotlink: 'ATT1HOTLINK', id: null, name: 'ATT1 Offer', price: 49,
        currency: 'USD',
      },
      offer: { code: 'att1offer' },
      checkout_url: 'https://pay.hotmart.com/ATT1HOTLINK?off=att1offer&checkoutMode=10',
      checkout_country: { iso: 'US', source: 'phone_country_code' },
      attribution: {
        utm_source: 'test', utm_medium: 'test', utm_campaign: 'test',
        utm_content: 'test', utm_term: '', sck: 'test.test.test',
        fbclid: 'fixture', referrer: 'https://example.test/',
      },
      consent: {
        marketing_optin: true, whatsapp_contact: true,
        copy_version: 'att1-whatsapp-v1',
      },
    },
    dedupe_key: 'ally-one-site:att1offer:buyer@example.test',
  };
  const canonical = {
    external_submission_id: id,
    event_type: 'PRECHECKOUT_FORM_SUBMITTED',
    contract_version: '1.1.0',
    submitted_at: raw.created_at,
    source: {
      tenant_ref: 'att1', funnel_ref: 'att1-main', landing_ref: 'main',
      page_url: raw.source.page_url, aliado: 'Ally One',
    },
    identity: {
      email: 'buyer@example.test', phone: '12025550123', phone_valid: true,
      phone_country_iso: 'US',
    },
    lead: { full_name: 'Test Buyer' },
    commerce: {
      product_ref: 'ATT1HOTLINK', product_name: 'ATT1 Offer', offer_ref: 'att1offer',
      price: '49', currency: 'USD', checkout_url: raw.data.checkout_url,
    },
    dedupe_key: raw.dedupe_key,
    consent: {
      terms_accepted: false, privacy_accepted: false, marketing_optin: true,
      whatsapp_contact: true, copy_version: 'att1-whatsapp-v1',
    },
    assurance: {
      provisional: false, provider_observed: true, activation_authorized: true,
    },
  };
  return { raw, canonical };
};
const admit = (tenant, funnel, version, id, raw, canonical) => db.query(
  `select * from public.admit_portable_observed_lead_precheckout(
     $1, $2, $3, $4, $5::jsonb, $6::jsonb
   )`,
  [tenant, funnel, version, id, JSON.stringify(raw), JSON.stringify(canonical)],
);
const expectRejected = async (label, action) => {
  let rejected = false;
  try { await action(); } catch { rejected = true; }
  if (!rejected) throw new Error(`${label} did not fail closed`);
};

{
  const { raw, canonical } = payloads('missing-binding');
  await expectRejected('missing exact binding', () => admit(
    'missing', 'att1-main', 1, raw.id, raw, canonical,
  ));
}
{
  const { raw, canonical } = payloads('inactive-binding');
  await expectRejected('inactive exact binding', () => admit(
    'att1', 'att1-main', 2, raw.id, raw, canonical,
  ));
}
const canonicalDrifts = [
  ['tenant', (canonical) => { canonical.source.tenant_ref = 'other'; }],
  ['funnel', (canonical) => { canonical.source.funnel_ref = 'other'; }],
  ['landing', (canonical) => { canonical.source.landing_ref = 'other'; }],
  ['product', (canonical) => { canonical.commerce.product_ref = 'OTHER'; }],
  ['product name', (canonical) => { canonical.commerce.product_name = 'Other'; }],
  ['offer', (canonical) => { canonical.commerce.offer_ref = 'other'; }],
  ['price', (canonical) => { canonical.commerce.price = '50'; }],
  ['currency', (canonical) => { canonical.commerce.currency = 'EUR'; }],
  ['consent copy', (canonical) => { canonical.consent.copy_version = 'other'; }],
];
for (const [label, mutate] of canonicalDrifts) {
  const { raw, canonical } = payloads(`wrong-canonical-${label.replace(' ', '-')}`);
  mutate(canonical);
  await expectRejected(`wrong canonical ${label}`, () => admit(
    'att1', 'att1-main', 1, raw.id, raw, canonical,
  ));
}
{
  await db.query(`update public.commercial_ally_runtime_bindings
                  set product_price = 50 where binding_version = 1`);
  const { raw, canonical } = payloads('binding-drift');
  await expectRejected('binding drift', () => admit(
    'att1', 'att1-main', 1, raw.id, raw, canonical,
  ));
  await db.query(`update public.commercial_ally_runtime_bindings
                  set product_price = 49 where binding_version = 1`);
}

const { raw, canonical } = payloads('portable-admission-001');
const inserted = await admit('att1', 'att1-main', 1, raw.id, raw, canonical);
const duplicate = await admit('att1', 'att1-main', 1, raw.id, raw, canonical);
const changedRaw = structuredClone(raw);
const changedCanonical = structuredClone(canonical);
changedRaw.data.buyer.name = 'Changed Buyer';
changedCanonical.lead.full_name = 'Changed Buyer';
const conflict = await admit(
  'att1', 'att1-main', 1, raw.id, changedRaw, changedCanonical,
);
const conflictReplay = await admit(
  'att1', 'att1-main', 1, raw.id, changedRaw, changedCanonical,
);
if (inserted.rows[0]?.outcome !== 'inserted'
    || duplicate.rows[0]?.outcome !== 'duplicate'
    || conflict.rows[0]?.outcome !== 'semantic_conflict'
    || conflictReplay.rows[0]?.outcome !== 'semantic_conflict') {
  throw new Error('portable admission replay/conflict semantics diverged');
}
if (new Set([
  inserted.rows[0]?.purchase_intent_id,
  duplicate.rows[0]?.purchase_intent_id,
  conflict.rows[0]?.purchase_intent_id,
]).size !== 1) {
  throw new Error('portable admission replay changed purchase intent');
}

const durable = (await db.query(`
  select
    (select count(*)::integer from public.precheckout_submissions) submissions,
    (select count(*)::integer from public.purchase_intents) intents,
    (select count(*)::integer from public.purchase_intent_submissions) links,
    (select count(*)::integer from public.precheckout_submission_conflicts) conflicts
`)).rows[0];
if (durable?.submissions !== 1 || durable.intents !== 1
    || durable.links !== 1 || durable.conflicts !== 1) {
  throw new Error(`portable durable state diverged: ${JSON.stringify(durable)}`);
}

const effectTables = (await db.query(`
  select tablename from pg_tables
  where schemaname = 'public'
    and (tablename = 'scheduled_actions'
      or tablename ~ '(reevaluation|command|message|delivery)')
`)).rows.map((row) => row.tablename);
for (const table of effectTables) {
  const count = (await db.query(
    `select count(*)::integer count from public.${table}`,
  )).rows[0]?.count;
  if (count !== 0) throw new Error(`unexpected effect row in ${table}: ${count}`);
}

console.log('portable_binding_fail_closed=OK');
console.log('portable_insert_replay_conflict=OK');
console.log(`portable_effect_tables_zero=${effectTables.length}`);
