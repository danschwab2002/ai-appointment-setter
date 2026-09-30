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

// 20260930000200: el formulario se admite en cada landing del binding.
// Binding, ofertas, sitios y URLs copiados de
// tests/fixtures/instances/att1/instancia.toml (ATT1, medidos el 2026-09-28).
// No hay lead.precheckout capturado: el payload es el mismo precedente inline
// de arriba con los valores de cada landing de ATT1.
const legacyLandings = (await db.query(`
  select additional_offer_landings from public.commercial_ally_runtime_bindings
  where tenant_ref = 'att1' and funnel_ref = 'att1-main' and binding_version = 1
`)).rows[0]?.additional_offer_landings;
if (JSON.stringify(legacyLandings) !== '[]') {
  throw new Error(`existing binding did not keep empty landings: ${JSON.stringify(legacyLandings)}`);
}

const att1Landings = [
  { offer_code: 'gopi6lh7', site: 'metodoraizana', landing_id: 'ads-a',
    page_host: 'www.metodoraizana.com', page_path: '/att1/evg/vsl/ads-a' },
  { offer_code: 'bmaztyhg', site: 'metodoraizana', landing_id: 'org-a',
    page_host: 'www.metodoraizana.com', page_path: '/att1/evg/vsl/org-a' },
  { offer_code: '2uafw5bg', site: 'metodoraizana-mx', landing_id: 'alimenta-tu-tiroides-d',
    page_host: 'site.metodoraizana.com.mx', page_path: '/alimenta-tu-tiroides-d' },
];
const [att1Default, ...att1Additional] = att1Landings;
await db.query(`
  insert into public.commercial_ally_runtime_bindings
    (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
     lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
     product_name, product_price, currency, offer_code, consent_copy_version,
     hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
     inbound_scope_key, inbound_scope_version, additional_offer_codes,
     additional_offer_landings)
  values ('lancemos', 'att1', 1, 'active', 'att1', 'Dra. Nina Garza',
          $1, $2, $3, $4, 'D98014973Y', 'Alimenta Tu Tiroides', 47, 'USD', $5,
          'att1-whatsapp-contact-v1', 5071808, 2, 11, 'att1-inbound', 1,
          $6::text[], $7::jsonb)
`, [
  att1Default.site, att1Default.landing_id, att1Default.page_host,
  att1Default.page_path, att1Default.offer_code,
  att1Additional.map((landing) => landing.offer_code),
  JSON.stringify(att1Additional),
]);

const resolvedAtt1 = (await db.query(`
  select additional_offer_landings
  from public.resolve_commercial_ally_runtime_binding('lancemos', 'att1', 1)
`)).rows;
if (resolvedAtt1.length !== 1
    || JSON.stringify(resolvedAtt1[0].additional_offer_landings.map((landing) => landing.offer_code))
      !== JSON.stringify(['bmaztyhg', '2uafw5bg'])) {
  throw new Error(`binding resolution did not return the landings: ${JSON.stringify(resolvedAtt1)}`);
}

const setAtt1Landings = (landings, codes = ['bmaztyhg', '2uafw5bg']) => db.query(`
  update public.commercial_ally_runtime_bindings
  set additional_offer_landings = $1::jsonb, additional_offer_codes = $2::text[]
  where tenant_ref = 'lancemos' and funnel_ref = 'att1' and binding_version = 1
`, [JSON.stringify(landings), codes]);
const expectCheckViolation = async (label, landings, codes) => {
  let error = null;
  try { await setAtt1Landings(landings, codes); } catch (caught) { error = caught; }
  if (error === null) throw new Error(`landings shape accepted ${label}`);
  if (error.code !== '23514'
      || !String(error.message).includes('commercial_ally_runtime_bindings_offer_landings_shape')) {
    throw new Error(`landings shape ${label} failed for another reason: ${error.message}`);
  }
};
const withLanding = (index, patch) => att1Additional.map(
  (landing, position) => (position === index ? { ...landing, ...patch } : landing),
);
const { page_path: _droppedPath, ...landingWithoutPath } = att1Additional[0];
const shapeViolations = [
  ['not an array', { offer_code: 'bmaztyhg' }],
  ['order other than additional_offer_codes', [...att1Additional].reverse()],
  ['one landing for two offers', att1Additional.slice(0, 1)],
  ['landings without additional offers', att1Additional, []],
  ['missing key', [landingWithoutPath, att1Additional[1]]],
  ['extra key', withLanding(0, { url: 'https://www.metodoraizana.com/att1/evg/vsl/org-a' })],
  ['non-string path', withLanding(0, { page_path: 7 })],
  ['offer outside additional codes', withLanding(0, { offer_code: '83utgyow' })],
  ['landing of the default offer', withLanding(0, { landing_id: 'ads-a' })],
  ['repeated landing', withLanding(1, { site: 'metodoraizana', landing_id: 'org-a' })],
  ['slug with uppercase', withLanding(1, { site: 'Metodoraizana-MX' })],
  ['host without domain', withLanding(1, { page_host: 'localhost' })],
  ['host with scheme', withLanding(1, { page_host: 'https://site.metodoraizana.com.mx' })],
  ['relative path', withLanding(1, { page_path: 'alimenta-tu-tiroides-d' })],
  ['path with query', withLanding(1, { page_path: '/alimenta-tu-tiroides-d?x=1' })],
  ['path with fragment', withLanding(1, { page_path: '/alimenta-tu-tiroides-d#cta' })],
];
for (const [label, landings, codes] of shapeViolations) {
  await expectCheckViolation(label, landings, codes);
}
// Vacio sigue siendo valido con ofertas adicionales: es el estado de toda fila
// existente y significa "el formulario solo por la landing por defecto".
await setAtt1Landings([]);
await setAtt1Landings(att1Additional);

const att1Payloads = (id, { offer_code: offer, site, landing_id: landing, page_host: host, page_path: path },
  email = 'buyer@example.test') => {
  const raw = {
    id,
    event: 'lead.precheckout',
    version: '1.1.0',
    created_at: '2026-09-30T12:00:00Z',
    source: {
      system: 'landing', site, aliado: 'Dra. Nina Garza',
      landing_id: landing, page_url: `https://${host}${path}`,
    },
    data: {
      buyer: {
        name: 'Test Buyer', email, phone: '+12025550123',
        phone_country_code: '1', phone_national: '2025550123',
      },
      product: {
        hotlink: 'D98014973Y', id: null, name: 'Alimenta Tu Tiroides', price: 47,
        currency: 'USD',
      },
      offer: { code: offer },
      checkout_url: `https://pay.hotmart.com/D98014973Y?off=${offer}&checkoutMode=10`,
      checkout_country: { iso: 'US', source: 'phone_country_code' },
      attribution: {
        utm_source: 'test', utm_medium: 'test', utm_campaign: 'test',
        utm_content: 'test', utm_term: '', sck: 'test.test.test',
        fbclid: 'fixture', referrer: 'https://example.test/',
      },
      consent: {
        marketing_optin: true, whatsapp_contact: true,
        copy_version: 'att1-whatsapp-contact-v1',
      },
    },
    dedupe_key: `${site}:${offer}:${email}`,
  };
  const canonical = {
    external_submission_id: id,
    event_type: 'PRECHECKOUT_FORM_SUBMITTED',
    contract_version: '1.1.0',
    submitted_at: raw.created_at,
    source: {
      tenant_ref: 'lancemos', funnel_ref: 'att1', landing_ref: landing,
      page_url: raw.source.page_url, aliado: 'Dra. Nina Garza',
    },
    identity: {
      email, phone: '12025550123', phone_valid: true, phone_country_iso: 'US',
    },
    lead: { full_name: 'Test Buyer' },
    commerce: {
      product_ref: 'D98014973Y', product_name: 'Alimenta Tu Tiroides', offer_ref: offer,
      price: '47', currency: 'USD', checkout_url: raw.data.checkout_url,
    },
    dedupe_key: raw.dedupe_key,
    consent: {
      terms_accepted: false, privacy_accepted: false, marketing_optin: true,
      whatsapp_contact: true, copy_version: 'att1-whatsapp-contact-v1',
    },
    assurance: {
      provisional: false, provider_observed: true, activation_authorized: true,
    },
  };
  return { raw, canonical };
};
const admitAtt1 = (id, landing, email) => {
  const { raw, canonical } = att1Payloads(id, landing, email);
  return admit('lancemos', 'att1', 1, id, raw, canonical);
};
const expectAdmissionError = async (label, message, action) => {
  let error = null;
  try { await action(); } catch (caught) { error = caught; }
  if (error === null) throw new Error(`${label} was admitted`);
  if (!String(error.message).includes(message)) {
    throw new Error(`${label} failed with ${error.message}, expected ${message}`);
  }
};

// Cada landing entra con su oferta y deja una intencion de esa oferta y landing.
const att1Intents = [];
for (const [index, landing] of att1Landings.entries()) {
  const admitted = (await admitAtt1(`att1-landing-${index}`, landing)).rows[0];
  if (admitted?.outcome !== 'inserted') {
    throw new Error(`landing ${landing.landing_id} was not admitted: ${JSON.stringify(admitted)}`);
  }
  const intent = (await db.query(`
    select landing_ref, offer_ref, whatsapp_contact_authorized, activation_authorized
    from public.purchase_intents where id = $1
  `, [admitted.purchase_intent_id])).rows[0];
  if (intent?.landing_ref !== landing.landing_id
      || intent.offer_ref !== landing.offer_code
      || intent.whatsapp_contact_authorized !== true
      || intent.activation_authorized !== true) {
    throw new Error(`intent of ${landing.landing_id} diverged: ${JSON.stringify(intent)}`);
  }
  att1Intents.push(admitted.purchase_intent_id);
}
// La misma persona en las tres landings: una intencion por oferta.
if (new Set(att1Intents).size !== 3) {
  throw new Error(`one intent per offer expected: ${JSON.stringify(att1Intents)}`);
}
// Un segundo envio en la misma landing reusa la intencion de esa oferta.
const secondMx = (await admitAtt1('att1-landing-2-again', att1Landings[2])).rows[0];
if (secondMx?.outcome !== 'inserted' || secondMx.purchase_intent_id !== att1Intents[2]) {
  throw new Error(`same landing did not reuse its intent: ${JSON.stringify(secondMx)}`);
}

// Pares cruzados: la oferta decide la landing, el sitio, el host y la ruta.
const crossed = [
  ['offer of org-a on the ads-a page', 'observed_precheckout_assurance_mismatch',
    { ...att1Landings[0], offer_code: 'bmaztyhg' }],
  ['offer of .mx declared on the .com site', 'observed_precheckout_raw_canonical_mismatch',
    { ...att1Landings[2], site: 'metodoraizana' }],
  ['offer of .mx served from the .com host', 'observed_precheckout_assurance_mismatch',
    { ...att1Landings[2], page_host: 'www.metodoraizana.com' }],
  ['path of org-a with the ads-a landing', 'observed_precheckout_assurance_mismatch',
    { ...att1Landings[0], page_path: '/att1/evg/vsl/org-a' }],
  ['offer outside the binding', 'observed_precheckout_assurance_mismatch',
    { ...att1Landings[1], offer_code: '83utgyow' }],
];
for (const [index, [label, message, landing]] of crossed.entries()) {
  await expectAdmissionError(label, message, () => admitAtt1(
    `att1-crossed-${index}`, landing, `crossed-${index}@example.test`,
  ));
}

// Sin landings declaradas se comporta como antes: solo la oferta por defecto.
await setAtt1Landings([]);
await expectAdmissionError('additional landing without declared landings',
  'observed_precheckout_assurance_mismatch',
  () => admitAtt1('att1-undeclared-org-a', att1Landings[1]));
const defaultOnly = (await admitAtt1('att1-undeclared-ads-a', att1Landings[0])).rows[0];
if (defaultOnly?.outcome !== 'inserted' || defaultOnly.purchase_intent_id !== att1Intents[0]) {
  throw new Error(`default landing stopped working without landings: ${JSON.stringify(defaultOnly)}`);
}
await setAtt1Landings(att1Additional);

const att1Durable = (await db.query(`
  select
    (select count(*)::integer from public.purchase_intents
      where tenant_ref = 'lancemos' and funnel_ref = 'att1') intents,
    (select count(*)::integer from public.purchase_intents
      where tenant_ref = 'lancemos' and funnel_ref = 'att1'
        and current_classification is not null) classified,
    (select count(*)::integer from public.precheckout_submissions
      where external_submission_id like 'att1-%') submissions
`)).rows[0];
if (att1Durable?.intents !== 3 || att1Durable.classified !== 0
    || att1Durable.submissions !== 5) {
  throw new Error(`multi-landing durable state diverged: ${JSON.stringify(att1Durable)}`);
}
for (const table of effectTables) {
  const count = (await db.query(
    `select count(*)::integer count from public.${table}`,
  )).rows[0]?.count;
  if (count !== 0) throw new Error(`unexpected effect row in ${table}: ${count}`);
}

console.log('portable_binding_fail_closed=OK');
console.log('portable_insert_replay_conflict=OK');
console.log(`portable_effect_tables_zero=${effectTables.length}`);
console.log(`portable_offer_landings_shape_rejections=${shapeViolations.length}`);
console.log(`portable_offer_landings_admitted=${att1Landings.length} crossed_rejected=${crossed.length}`);
