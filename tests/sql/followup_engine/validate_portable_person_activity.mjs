// Lo que hizo la persona, para la entrada del agente (migracion 20261016000200,
// paso 2b de varios productos por aliada), con las admisiones reales.
//
// Prueba:
//   0. que la migracion agrega exactamente una funcion, no cambia ninguna otra
//      definicion ni ACL, y que es solo de service_role;
//   1. get_portable_person_activity_v1 devuelve, de la mas vieja a la mas nueva,
//      los dos envios del formulario (admit_portable_observed_lead_precheckout
//      con el golden mexicano del traductor de GHL), el carrito
//      (admit_portable_hotmart_cart_abandonment con el carrito capturado), el
//      pago rechazado, la ficha impresa, la ficha vencida y la compra, cada uno
//      por su admision portable real, con el nombre de actividad que lee el
//      bridge (PERSON_ACTIVITY_KINDS de src/bridge/supabase.py); y lo mismo por
//      las dos formas del movil (el formulario guarda 52 + 10 y WhatsApp
//      escribe desde 521 + 10);
//   2. otro movil, otro funnel y otro tenant no devuelven nada; lo de hace mas
//      de 30 dias no entra; con mas de 20 cosas quedan las 20 mas recientes, en
//      orden; una entrada invalida da 22023.
//
// Datos: binding, ofertas y producto de tests/fixtures/instances/att1/
// instancia.toml. Formulario: tests/fixtures/ghl/expected/
// ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json. Carrito:
// tests/fixtures/hotmart_cart_abandonment_rejected_v1.json. Pago rechazado,
// fichas y compra: el sobre de compra 2.0.0 inline, como en
// validate_whatsapp_phone_equivalence.mjs (la admision solo mira el sobre).
// La forma de lo que devuelve la funcion contra la base real de ATT1 esta en
// tests/fixtures/att1_person_activity_20261016.json.
import { PGlite } from '@electric-sql/pglite';
import { randomBytes } from 'node:crypto';
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

const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};
const results = {};

// ---------------------------------------------------------------------------
// 0. La migracion agrega una funcion y nada mas.
// ---------------------------------------------------------------------------
const TARGET = '20261016000200_portable_person_activity_v1.sql';
const migrationNames = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const targetIndex = migrationNames.indexOf(TARGET);
if (targetIndex < 0) throw new Error(`${TARGET} is missing from the canonical stack`);
const apply = async (file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
));
const snapshot = async () => new Map((await db.query(`
  select p.oid::regprocedure::text as signature,
         pg_get_functiondef(p.oid) as definition,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));

await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of migrationNames.slice(0, targetIndex)) {
  await apply(join(root, 'supabase/migrations', name));
}
const before = await snapshot();
await apply(join(root, 'supabase/migrations', TARGET));
const after = await snapshot();
for (const name of migrationNames.slice(targetIndex + 1)) {
  await apply(join(root, 'supabase/migrations', name));
}

const NEW_ENTRYPOINT = 'get_portable_person_activity_v1(text,text,text)';
const added = [...after.keys()].filter((signature) => !before.has(signature));
const removed = [...before.keys()].filter((signature) => !after.has(signature));
const changed = [...before.keys()].filter((signature) => after.has(signature)
  && (after.get(signature).definition !== before.get(signature).definition
      || after.get(signature).service_x !== before.get(signature).service_x
      || after.get(signature).api_x !== before.get(signature).api_x));
if (JSON.stringify(added) !== JSON.stringify([NEW_ENTRYPOINT])
    || removed.length !== 0 || changed.length !== 0) {
  throw new Error(`the migration changed something else: ${JSON.stringify({ added, removed, changed })}`);
}
{
  const row = after.get(NEW_ENTRYPOINT);
  if (row.service_x !== true || row.api_x !== false) {
    throw new Error(`${NEW_ENTRYPOINT} is not service_role only: ${JSON.stringify(row)}`);
  }
}
results.migration = { added: 1, changed: 0, functions_unchanged: before.size };

// ---------------------------------------------------------------------------
// El manifiesto de ATT1 (el mismo lector minimo que la cadena de ATT1).
// ---------------------------------------------------------------------------
const readManifest = (text) => {
  const manifest = {};
  const walk = (path) => path.reduce((node, key) => (node[key] ??= {}), manifest);
  let table = manifest;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    let match = line.match(/^\[\[([a-z_.]+)\]\]$/);
    if (match) {
      const path = match[1].split('.');
      const parent = walk(path.slice(0, -1));
      table = {};
      (parent[path.at(-1)] ??= []).push(table);
      continue;
    }
    match = line.match(/^\[([a-z_.]+)\]$/);
    if (match) {
      table = walk(match[1].split('.'));
      continue;
    }
    match = line.match(/^([a-z_]+)\s*=\s*(?:"([^"]*)"|(-?\d+)|(true|false))\s*(?:#.*)?$/);
    if (match) {
      table[match[1]] = match[2] ?? (match[3] !== undefined ? Number(match[3]) : match[4] === 'true');
    }
  }
  return manifest;
};
const required = (value, label) => {
  if (value === undefined || value === null || value === '') {
    throw new Error(`ATT1 fixture without ${label}`);
  }
  return value;
};
const manifest = readManifest(readFileSync(
  join(root, 'tests/fixtures/instances/att1/instancia.toml'), 'utf8',
));
const offers = required(manifest.hotmart?.ofertas, 'hotmart.ofertas').map((offer) => {
  const url = new URL(required(offer.url, 'ofertas.url'));
  return {
    offer_code: required(offer.codigo, 'ofertas.codigo'),
    site: required(offer.site, 'ofertas.site'),
    landing_id: required(offer.landing_id, 'ofertas.landing_id'),
    page_host: url.host,
    page_path: url.pathname,
  };
});
const ATT1 = {
  tenant: required(manifest.instancia?.tenant_ref, 'instancia.tenant_ref'),
  funnel: required(manifest.instancia?.funnel_ref, 'instancia.funnel_ref'),
  ally: required(manifest.instancia?.ally_ref, 'instancia.ally_ref'),
  bindingVersion: required(manifest.instancia?.binding_version, 'instancia.binding_version'),
  brand: required(manifest.instancia?.marca, 'instancia.marca'),
  productId: required(manifest.hotmart?.product_id, 'hotmart.product_id'),
  hotlink: required(manifest.hotmart?.hotlink, 'hotmart.hotlink'),
  productName: required(manifest.hotmart?.product_name, 'hotmart.product_name'),
  currency: required(manifest.hotmart?.moneda, 'hotmart.moneda'),
  price: required(manifest.hotmart?.precio, 'hotmart.precio'),
  accountId: required(manifest.chatwoot?.account_id, 'chatwoot.account_id'),
  inboxId: required(manifest.chatwoot?.inbox_id, 'chatwoot.inbox_id'),
  inboundScope: required(manifest.inbound?.scope_key, 'inbound.scope_key'),
  inboundVersion: required(manifest.inbound?.scope_version, 'inbound.scope_version'),
  copyVersion: required(manifest.consentimiento?.copy_version, 'consentimiento.copy_version'),
};
const [defaultOffer, ...additionalOffers] = offers;
const PILOT = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/instances/att1/politica-piloto.json'), 'utf8',
));

await db.query(`
  insert into public.commercial_ally_runtime_bindings
    (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
     lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
     product_name, product_price, currency, offer_code, consent_copy_version,
     hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
     inbound_scope_key, inbound_scope_version, additional_offer_codes,
     additional_offer_landings)
  values ($1,$2,$3,'active',$4,$5,$6,$7,$8,$9,$10,$11,$12::numeric,$13,$14,$15,
          $16,$17,$18,$19,$20,$21::text[],$22::jsonb)
`, [
  ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, ATT1.ally, ATT1.brand,
  defaultOffer.site, defaultOffer.landing_id, defaultOffer.page_host,
  defaultOffer.page_path, ATT1.hotlink, ATT1.productName, ATT1.price,
  ATT1.currency, defaultOffer.offer_code, ATT1.copyVersion, ATT1.productId,
  ATT1.accountId, ATT1.inboxId, ATT1.inboundScope, ATT1.inboundVersion,
  additionalOffers.map((offer) => offer.offer_code),
  JSON.stringify(additionalOffers.map((offer) => ({
    offer_code: offer.offer_code, site: offer.site, landing_id: offer.landing_id,
    page_host: offer.page_host, page_path: offer.page_path,
  }))),
]);
for (const offer of offers) {
  await db.query(`
    insert into public.hotmart_purchase_intent_scopes
      (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
       offer_ref, max_lookback, active)
    values ($1,$2,$3,$4,$5,$6::interval,true)
  `, [ATT1.tenant, ATT1.funnel, String(ATT1.productId), ATT1.hotlink, offer.offer_code,
    required(PILOT.purchase_intent_scope?.max_lookback, 'purchase_intent_scope.max_lookback')]);
}
await db.query(`
  insert into public.commercial_ally_hotmart_purchase_policies
    (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
  values ($1,$2,$3,true,$4::interval)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion,
  required(PILOT.purchase_policy?.max_lookback, 'purchase_policy.max_lookback')]);

// ---------------------------------------------------------------------------
// Las admisiones reales.
// ---------------------------------------------------------------------------
const GOLDEN = JSON.parse(readFileSync(join(root,
  'tests/fixtures/ghl/expected/ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json'), 'utf8'));
const CAPTURED_CART = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/hotmart_cart_abandonment_rejected_v1.json'), 'utf8',
)).payload;
const FORM_OFFER = offers.find((offer) => offer.offer_code === GOLDEN.canonical_payload.commerce.offer_ref);
if (!FORM_OFFER || GOLDEN.canonical_payload.identity.phone_country_iso !== 'MX') {
  throw new Error('the Mexican translator golden changed');
}

const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
const ulidAt = (ms) => {
  let value = (BigInt(ms) << 80n) | BigInt(`0x${randomBytes(10).toString('hex')}`);
  let out = '';
  for (let i = 0; i < 26; i += 1) {
    out = CROCKFORD[Number(value & 31n)] + out;
    value >>= 5n;
  }
  return out;
};

let personIndex = 0;
const person = (label) => {
  personIndex += 1;
  const suffix = String(personIndex).padStart(2, '0');
  const national = `55555503${suffix}`;
  return {
    label,
    email: `att1-activity-${suffix}@example.test`,
    name: `Persona actividad ${label}`,
    plain: `52${national}`,
    whatsapp: `521${national}`,
  };
};

const submitForm = async (lead) => {
  const raw = structuredClone(GOLDEN.raw_payload);
  const canonical = structuredClone(GOLDEN.canonical_payload);
  // En segundos enteros: el crudo y el canonico tienen que decir la misma hora.
  const now = Math.floor(Date.now() / 1000) * 1000;
  const id = ulidAt(now);
  const dedupeKey = `${raw.source.site}:${raw.data.offer.code}:${lead.email}:${id}`;
  raw.id = id;
  raw.created_at = new Date(now).toISOString();
  raw.data.buyer.email = lead.email;
  raw.data.buyer.phone = `+${lead.plain}`;
  raw.data.buyer.phone_national = lead.plain.slice(2);
  raw.dedupe_key = dedupeKey;
  canonical.external_submission_id = id;
  canonical.submitted_at = new Date(now).toISOString().replace('.000Z', 'Z');
  canonical.identity.email = lead.email;
  canonical.identity.phone = lead.plain;
  canonical.dedupe_key = dedupeKey;
  const admitted = one((await db.query(`
    select * from public.admit_portable_observed_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, id, JSON.stringify(raw),
    JSON.stringify(canonical)])).rows, `${lead.label} form`);
  if (!['inserted', 'attached'].includes(admitted.outcome) && admitted.submission_id == null) {
    throw new Error(`${lead.label}: form not admitted: ${admitted.outcome}`);
  }
  return admitted;
};

let eventIndex = 0;
const envelope = (lead, event, extra = {}) => {
  eventIndex += 1;
  return {
    id: `att1-activity-${event.toLowerCase()}-${eventIndex}`,
    creation_date: Date.now(),
    event,
    version: '2.0.0',
    data: {
      buyer: { name: lead.name, email: lead.email, checkout_phone: `+${lead.whatsapp}` },
      product: { id: ATT1.productId, name: ATT1.productName },
      purchase: {
        transaction: `HPATT1ACT${String(eventIndex).padStart(3, '0')}`,
        offer: { code: FORM_OFFER.offer_code },
        ...extra,
      },
    },
  };
};

const admitCart = async (lead) => {
  const payload = structuredClone(CAPTURED_CART);
  eventIndex += 1;
  payload.id = `att1-activity-cart-${eventIndex}`;
  payload.creation_date = Date.now();
  payload.data.product = { id: ATT1.productId, name: ATT1.productName };
  payload.data.offer = { code: FORM_OFFER.offer_code };
  // Medido en att1-db: el carrito trae el telefono en buyer.phone.
  payload.data.buyer = { name: lead.name, email: lead.email, phone: lead.whatsapp };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
    JSON.stringify(payload), lead.email, lead.whatsapp])).rows, `${lead.label} cart`);
  if (admitted.outcome !== 'inserted') throw new Error(`${lead.label} cart: ${admitted.outcome}`);
  return admitted.webhook_event_id;
};
const admitFailure = async (lead) => {
  const payload = envelope(lead, 'PURCHASE_CANCELED', {
    status: 'CANCELED', payment: { type: 'CREDIT_CARD', refusal_reason: 'Transaction refused' },
  });
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
    JSON.stringify(payload), lead.email, lead.whatsapp])).rows, `${lead.label} failure`);
  if (admitted.outcome !== 'inserted') throw new Error(`${lead.label} failure: ${admitted.outcome}`);
};
const admitSlip = async (lead, event, status) => {
  const payload = envelope(lead, event, { status });
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_payment_slip_event_v1($1,$2,$3,$4,$5::jsonb)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload)])).rows,
  `${lead.label} ${event}`);
  if (admitted.outcome !== 'inserted') throw new Error(`${lead.label} ${event}: ${admitted.outcome}`);
};
const admitPurchase = async (lead) => {
  const payload = envelope(lead, 'PURCHASE_APPROVED', {
    status: 'APPROVED', approved_date: Date.now(),
  });
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload),
    lead.email, lead.whatsapp])).rows, `${lead.label} purchase`);
  if (!['inserted', 'stored'].includes(admitted.outcome) && admitted.webhook_event_id == null) {
    throw new Error(`${lead.label} purchase: ${admitted.outcome}`);
  }
};

const activity = async (phone, { tenant = ATT1.tenant, funnel = ATT1.funnel } = {}) => (await db.query(`
  select activity, occurred_at from public.get_portable_person_activity_v1($1,$2,$3)
`, [tenant, funnel, phone])).rows;
const kinds = (rows) => rows.map((row) => row.activity);
const ascending = (rows) => rows.every((row, index) => index === 0
  || rows[index - 1].occurred_at.getTime() <= row.occurred_at.getTime());
const expectError = async (label, code, run) => {
  try {
    await run();
  } catch (error) {
    if (error.code !== code) throw new Error(`${label}: expected ${code}, got ${error.code} ${error.message}`);
    return;
  }
  throw new Error(`${label}: expected ${code}, it passed`);
};

// ---------------------------------------------------------------------------
// 1. Una persona con todo, en orden y por las dos formas del movil.
// ---------------------------------------------------------------------------
const EXPECTED = [
  'form', 'form', 'cart', 'payment_rejected', 'payment_slip_printed',
  'payment_slip_expired', 'purchase',
];
const KINDS = new Set([
  'form', 'cart', 'payment_rejected', 'payment_slip_printed', 'payment_slip_expired', 'purchase',
]);
const full = person('completa');
await submitForm(full);
await submitForm(full);
await admitCart(full);
await admitFailure(full);
await admitSlip(full, 'PURCHASE_BILLET_PRINTED', 'BILLET_PRINTED');
await admitSlip(full, 'PURCHASE_EXPIRED', 'EXPIRED');
await admitPurchase(full);
for (const phone of [full.plain, full.whatsapp]) {
  const rows = await activity(phone);
  if (JSON.stringify(kinds(rows)) !== JSON.stringify(EXPECTED) || !ascending(rows)
      || rows.some((row) => !(row.occurred_at instanceof Date) || !KINDS.has(row.activity))
      || rows.some((row) => Object.keys(row).sort().join() !== 'activity,occurred_at')) {
    throw new Error(`activity by ${phone.length} digits: ${JSON.stringify(kinds(rows))}`);
  }
}
results.full_person = { activities: EXPECTED.length, by_both_forms: true };

// ---------------------------------------------------------------------------
// 2. Lo que no entra.
// ---------------------------------------------------------------------------
{
  const stranger = person('sin nada');
  if ((await activity(stranger.whatsapp)).length !== 0) throw new Error('another phone saw activity');
  if ((await activity(full.whatsapp, { funnel: 'att2' })).length !== 0) throw new Error('another funnel saw activity');
  if ((await activity(full.whatsapp, { tenant: 'otra-aliada' })).length !== 0) throw new Error('another tenant saw activity');

  // Hace 31 dias: el carrito y el primer envio del formulario salen.
  await db.query(`
    update public.webhook_events set received_at = clock_timestamp() - interval '31 days'
    where id = (
      select event.id from public.webhook_events event
      join public.commercial_ally_hotmart_event_bindings provenance on provenance.webhook_event_id = event.id
      where event.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
        and event.payload #>> '{data,buyer,email}' = $1)
  `, [full.email]);
  await db.query(`
    update public.precheckout_submissions set received_at = clock_timestamp() - interval '31 days'
    where id = (
      select link.submission_id from public.purchase_intent_submissions link
      join public.purchase_intents intent on intent.id = link.purchase_intent_id
      where intent.normalized_email = $1 order by link.ordinal limit 1)
  `, [full.email]);
  const rows = await activity(full.whatsapp);
  const expected = EXPECTED.filter((kind, index) => index !== 0 && kind !== 'cart');
  if (JSON.stringify(kinds(rows)) !== JSON.stringify(expected)) {
    throw new Error(`the 30-day window: ${JSON.stringify(kinds(rows))}`);
  }

  // 25 carritos: quedan los 20 mas recientes, del mas viejo al mas nuevo.
  const busy = person('25 carritos');
  const carts = [];
  for (let index = 0; index < 25; index += 1) carts.push(await admitCart(busy));
  const busyRows = await activity(busy.whatsapp);
  const kept = (await db.query(`
    select received_at from public.webhook_events where id = any($1::uuid[]) order by received_at desc limit 20
  `, [carts])).rows.map((row) => row.received_at.getTime()).reverse();
  if (busyRows.length !== 20 || !ascending(busyRows)
      || JSON.stringify(busyRows.map((row) => row.occurred_at.getTime())) !== JSON.stringify(kept)) {
    throw new Error(`the cap of 20: ${busyRows.length}`);
  }

  await expectError('a short phone', '22023', () => activity('5255'));
  await expectError('a phone with a plus', '22023', () => activity(`+${full.whatsapp}`));
  await expectError('an empty tenant', '22023', () => activity(full.whatsapp, { tenant: ' ' }));
  await expectError('an empty funnel', '22023', () => activity(full.whatsapp, { funnel: '' }));
  results.excluded = {
    other_phone: true, other_funnel: true, other_tenant: true,
    older_than_30_days: true, capped_at: busyRows.length, invalid_input: 4,
  };
}

console.log(JSON.stringify(results));
