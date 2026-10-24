// El modo «solo guardar» de Hotmart (migracion 20261024000100, E-057), con las
// admisiones reales.
//
// Prueba:
//   0. que la migracion agrega exactamente una funcion, no cambia ninguna otra
//      definicion ni ACL, y que es solo de service_role;
//   1. admit_portable_hotmart_listen_only_capture_v1 guarda el carrito y el
//      pago rechazado capturados en att1-db con la admision portable de hoy
//      (procedencia, identidad, correlacion y, en el rechazo, el detalle), y
//      los deja ignored / listen_only_capture; con cero filas en casos, toques,
//      intentos, emisiones de link y cupones, contactos y planes;
//   2. el reintento de Hotmart es duplicate y sigue ignored;
//   3. la cola del worker de resolucion (la misma consulta que
//      fetch_pending_events: processing_status = 'received') no los ve, ni
//      despues de prender el flujo: un carrito nuevo por la admision del flujo
//      entra en received y es el unico que la cola ve; el mismo carrito
//      capturado reenviado por la admision del flujo es duplicate y sigue
//      ignored;
//   4. un evento de otra oferta o de otro tipo no se guarda (22023).
//
// Datos: binding, ofertas y producto de tests/fixtures/instances/att1/
// instancia.toml. Carrito: el caso carrito_2uafw5bg de
// tests/fixtures/hotmart_routing_att1_20261016.json. Pago rechazado: el primero
// de tests/fixtures/hotmart_purchase_canceled_att1_20261019.json.
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

const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};
const expectError = async (label, code, message, run) => {
  try {
    await run();
  } catch (error) {
    if (error.code !== code || (message && error.message !== message)) {
      throw new Error(`${label}: expected ${code} ${message ?? ''}, got ${error.code} ${error.message}`);
    }
    return;
  }
  throw new Error(`${label}: expected ${code}, it passed`);
};
const results = {};

// ---------------------------------------------------------------------------
// 0. La migracion agrega una funcion y nada mas.
// ---------------------------------------------------------------------------
const TARGET = '20261024000100_portable_hotmart_listen_only_capture.sql';
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

const NEW_ENTRYPOINT = 'admit_portable_hotmart_listen_only_capture_v1(text,text,integer,text,jsonb,text,text)';
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
// Los eventos capturados.
// ---------------------------------------------------------------------------
const CART = structuredClone(JSON.parse(readFileSync(
  join(root, 'tests/fixtures/hotmart_routing_att1_20261016.json'), 'utf8',
)).eventos.find((event) => event.caso === 'carrito_2uafw5bg').payload);
const CANCELED = structuredClone(JSON.parse(readFileSync(
  join(root, 'tests/fixtures/hotmart_purchase_canceled_att1_20261019.json'), 'utf8',
)).eventos[0].payload);
if (CART.event !== 'PURCHASE_OUT_OF_SHOPPING_CART' || CANCELED.event !== 'PURCHASE_CANCELED') {
  throw new Error('the captured fixtures changed');
}
// La identidad como la normaliza el bridge (hotmart.py): el mail en
// minusculas y sin espacios, el telefono solo con digitos.
const identity = (payload) => [
  payload.data.buyer.email.trim().toLowerCase(),
  (payload.data.buyer.checkout_phone ?? payload.data.buyer.phone).replace(/[^0-9]/g, ''),
];
const capture = async (payload) => one((await db.query(`
  select * from public.admit_portable_hotmart_listen_only_capture_v1($1,$2,$3,$4,$5::jsonb,$6,$7)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload),
  ...identity(payload)])).rows, `capture ${payload.id}`);
const eventRow = async (id) => one((await db.query(`
  select event_type, processing_status, processing_error, processed_at
    from public.webhook_events where id = $1
`, [id])).rows, `event ${id}`);
// La misma cola que lee el worker de resolucion (SupabaseClient.fetch_pending_events).
const workerQueue = async () => (await db.query(`
  select id from public.webhook_events
   where processing_status = 'received'
   order by received_at asc, id asc
`)).rows.map((row) => row.id);
const DOWNSTREAM = [
  'recovery_cases', 'recovery_case_events', 'scheduled_actions',
  'followup_delivery_attempts', 'followup_sequences', 'checkout_link_issuances',
  'recovery_coupons', 'contacts', 'contact_points', 'commercial_cases',
  'pilot_recovery_case_bindings', 'johanna_payment_failure_cases',
  'portable_precheckout_first_contact_plans', 'identity_resolution_attempts',
];
const downstream = async () => {
  const counts = {};
  for (const table of DOWNSTREAM) {
    counts[table] = Number(one((await db.query(
      `select count(*)::int as n from public.${table}`,
    )).rows, table).n);
  }
  return counts;
};
const zero = (counts, label) => {
  const written = Object.entries(counts).filter(([, n]) => n !== 0);
  if (written.length !== 0) throw new Error(`${label}: rows downstream ${JSON.stringify(written)}`);
};

// ---------------------------------------------------------------------------
// 1. El carrito y el pago rechazado quedan guardados e ignored.
// ---------------------------------------------------------------------------
zero(await downstream(), 'before');
const cart = await capture(CART);
const failure = await capture(CANCELED);
for (const [label, admitted, type] of [
  ['cart', cart, 'PURCHASE_OUT_OF_SHOPPING_CART'],
  ['failure', failure, 'PURCHASE_CANCELED'],
]) {
  if (admitted.outcome !== 'inserted') throw new Error(`${label}: ${admitted.outcome}`);
  const row = await eventRow(admitted.webhook_event_id);
  if (row.event_type !== type || row.processing_status !== 'ignored'
      || row.processing_error !== 'listen_only_capture' || row.processed_at === null) {
    throw new Error(`${label}: not left ignored / listen_only_capture: ${JSON.stringify(row)}`);
  }
  const provenance = one((await db.query(`
    select tenant_ref, funnel_ref, binding_version
      from public.commercial_ally_hotmart_event_bindings where webhook_event_id = $1
  `, [admitted.webhook_event_id])).rows, `${label} provenance`);
  if (provenance.tenant_ref !== ATT1.tenant || provenance.funnel_ref !== ATT1.funnel) {
    throw new Error(`${label}: provenance of another funnel ${JSON.stringify(provenance)}`);
  }
  one((await db.query(`
    select 1 from public.hotmart_purchase_intent_event_identities where webhook_event_id = $1
  `, [admitted.webhook_event_id])).rows, `${label} identity`);
  one((await db.query(`
    select 1 from public.hotmart_purchase_intent_correlations where webhook_event_id = $1
  `, [admitted.webhook_event_id])).rows, `${label} correlation`);
}
one((await db.query(`
  select 1 from public.commercial_ally_payment_failure_details where webhook_event_id = $1
`, [failure.webhook_event_id])).rows, 'failure details');
zero(await downstream(), 'after the capture');
results.captured = { cart: 'ignored/listen_only_capture', failure: 'ignored/listen_only_capture' };

// ---------------------------------------------------------------------------
// 2. El reintento de Hotmart es duplicate y sigue ignored.
// ---------------------------------------------------------------------------
for (const [label, payload, admitted] of [['cart', CART, cart], ['failure', CANCELED, failure]]) {
  const retry = await capture(payload);
  if (retry.outcome !== 'duplicate' || retry.webhook_event_id !== admitted.webhook_event_id) {
    throw new Error(`${label} retry: ${JSON.stringify(retry)}`);
  }
  if ((await eventRow(admitted.webhook_event_id)).processing_status !== 'ignored') {
    throw new Error(`${label} retry changed the status`);
  }
}
results.retry = 'duplicate';

// ---------------------------------------------------------------------------
// 3. La cola del worker no los ve, ni con el flujo prendido despues.
// ---------------------------------------------------------------------------
if ((await workerQueue()).length !== 0) throw new Error('the worker queue sees a captured event');
// El flujo se prende: el mismo carrito reenviado por la admision del flujo.
{
  const replay = one((await db.query(`
    select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, CART.id, JSON.stringify(CART),
    ...identity(CART)])).rows, 'replay by the flow');
  if (replay.outcome !== 'duplicate'
      || (await eventRow(cart.webhook_event_id)).processing_status !== 'ignored') {
    throw new Error(`the flow took the captured cart: ${JSON.stringify(replay)}`);
  }
}
// Y un carrito nuevo por la admision del flujo: ese si va a la cola, solo.
const fresh = structuredClone(CART);
fresh.id = `${CART.id}-flujo-prendido`;
const flowCart = one((await db.query(`
  select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, fresh.id, JSON.stringify(fresh),
  ...identity(fresh)])).rows, 'cart by the flow');
{
  const queue = await workerQueue();
  if (JSON.stringify(queue) !== JSON.stringify([flowCart.webhook_event_id])) {
    throw new Error(`the worker queue after the flow: ${JSON.stringify(queue)}`);
  }
}
results.worker_queue = { captured_seen: 0, flow_cart_seen: 1 };

// ---------------------------------------------------------------------------
// 4. Lo que no es del binding o de otro tipo no se guarda.
// ---------------------------------------------------------------------------
const countEvents = async () => Number(one((await db.query(
  'select count(*)::int as n from public.webhook_events',
)).rows, 'events').n);
const eventsBefore = await countEvents();
{
  const otherOffer = structuredClone(CART);
  otherOffer.id = `${CART.id}-otra-oferta`;
  otherOffer.data.offer.code = 'zzzz9999';
  await expectError('a cart of another offer', '22023', 'portable_hotmart_cart_binding_mismatch',
    () => capture(otherOffer));
  const purchase = structuredClone(JSON.parse(readFileSync(
    join(root, 'tests/fixtures/hotmart_routing_att1_20261016.json'), 'utf8',
  )).eventos.find((event) => event.caso === 'compra_83utgyow').payload);
  await expectError('a purchase', '22023', 'listen_only_capture_event_unsupported',
    () => capture(purchase));
  const slip = structuredClone(JSON.parse(readFileSync(
    join(root, 'tests/fixtures/hotmart_routing_att1_20261016.json'), 'utf8',
  )).eventos.find((event) => event.caso === 'ficha_impresa_2uafw5bg').payload);
  await expectError('a payment slip', '22023', 'listen_only_capture_event_unsupported',
    () => capture(slip));
}
if (await countEvents() !== eventsBefore) throw new Error('a rejected capture left a row');
zero(await downstream(), 'at the end');
results.rejected = { other_offer: true, purchase: true, payment_slip: true };

console.log(JSON.stringify(results));
