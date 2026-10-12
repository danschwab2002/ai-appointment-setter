// El ultimo pago rechazado para la entrada del agente y los eventos de la ficha
// (migracion 20261012000100, version 1.6.0), con las RPC reales.
//
// Prueba:
//   0. que la migracion agrega exactamente dos funciones, no cambia ninguna
//      otra definicion ni ACL, y que las dos son solo de service_role;
//   1. get_portable_recent_payment_failure_v1 con los cuatro PURCHASE_CANCELED
//      reales de ATT1, admitidos por la admision portable real: devuelve la
//      oferta, el medio, las cuotas, el motivo y el pais tal como llegaron,
//      por las dos formas del movil mexicano (Hotmart manda 52 + 10 y WhatsApp
//      escribe desde 521 + 10), y el motivo en portugues de la compra desde
//      Panama sin tocar;
//   2. el mas reciente gana; un movil sin rechazo, otra aliada, un rechazo de
//      hace mas de una semana y quien compro despues (PURCHASE_APPROVED real,
//      re-telefoneado) no devuelven nada; una entrada invalida da 22023;
//   3. admit_portable_hotmart_payment_slip_event_v1 guarda un
//      PURCHASE_BILLET_PRINTED y un PURCHASE_EXPIRED con su procedencia, es
//      idempotente, marca el conflicto de un id repetido con otro payload,
//      no guarda otra oferta ni otro producto (ignored_binding_mismatch, sin
//      error) y rechaza otro tipo de evento o un id que no coincide (22023);
//      una ficha no cuenta como pago rechazado;
//   4. el inventario de esquema da fingerprint_present en la fila de la
//      migracion.
//
// Datos: binding, ofertas y producto de tests/fixtures/instances/att1/
// instancia.toml. Rechazos: tests/fixtures/hotmart_purchase_canceled_att1_
// 20261012.json (capturados de la base de ATT1 el 2026-10-12, anonimizados).
// Compra y ficha: tests/fixtures/hotmart_cash_payment_approved_att1_20261012.json,
// las dos compras reales pagadas en OXXO. DERIVADO, no capturado: ATT1 todavia
// no recibia los eventos de la ficha, asi que PURCHASE_BILLET_PRINTED y
// PURCHASE_EXPIRED se arman desde ese sobre cambiando solo event, id y
// data.purchase.status. La admision depende solo del sobre comun.
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
const results = {};

// ---------------------------------------------------------------------------
// 0. La migracion agrega dos funciones y nada mas.
// ---------------------------------------------------------------------------
const TARGET = '20261012000100_portable_payment_failure_context_and_slip_events.sql';
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

const NEW_ENTRYPOINTS = [
  'get_portable_recent_payment_failure_v1(text,text,text)',
  'admit_portable_hotmart_payment_slip_event_v1(text,text,integer,text,jsonb)',
];
const sameSet = (left, right) => JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
const added = [...after.keys()].filter((signature) => !before.has(signature));
const removed = [...before.keys()].filter((signature) => !after.has(signature));
const changed = [...before.keys()].filter((signature) => after.has(signature)
  && (after.get(signature).definition !== before.get(signature).definition
      || after.get(signature).service_x !== before.get(signature).service_x
      || after.get(signature).api_x !== before.get(signature).api_x));
if (!sameSet(added, NEW_ENTRYPOINTS) || removed.length !== 0 || changed.length !== 0) {
  throw new Error(`the migration changed something else: ${JSON.stringify({ added, removed, changed })}`);
}
for (const signature of NEW_ENTRYPOINTS) {
  const row = after.get(signature);
  if (row.service_x !== true || row.api_x !== false) {
    throw new Error(`${signature} is not service_role only: ${JSON.stringify(row)}`);
  }
}
results.migration = { added: added.length, changed: 0, functions_unchanged: before.size };

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
// Las capturas.
// ---------------------------------------------------------------------------
const fixture = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));
const CANCELED = fixture('hotmart_purchase_canceled_att1_20261012.json').eventos.map((event) => event.payload);
const CASH = fixture('hotmart_cash_payment_approved_att1_20261012.json').eventos.map((event) => event.payload);
if (CANCELED.length !== 4 || CASH.length !== 2) {
  throw new Error(`the captures changed: ${CANCELED.length} canceled, ${CASH.length} cash`);
}
for (const payload of CANCELED) {
  if (!defaultOffer.offer_code || ![defaultOffer, ...additionalOffers]
    .some((offer) => offer.offer_code === payload.data.purchase.offer.code)
      || payload.data.product.id !== ATT1.productId) {
    throw new Error(`a captured failure is not of the ATT1 binding: ${payload.id}`);
  }
}
const digits = (value) => String(value ?? '').replace(/\D/g, '');
const withPhone = (payload, phone, suffix) => {
  const copy = structuredClone(payload);
  copy.id = `${payload.id}-${suffix}`;
  copy.data.buyer.checkout_phone = phone;
  return copy;
};
const admitFailure = async (payload) => one((await db.query(`
  select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload),
  payload.data.buyer.email, digits(payload.data.buyer.checkout_phone)])).rows, `failure ${payload.id}`);
const admitPurchase = async (payload) => one((await db.query(`
  select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload),
  payload.data.buyer.email, digits(payload.data.buyer.checkout_phone)])).rows, `purchase ${payload.id}`);
const recent = async (phone, { tenant = ATT1.tenant, funnel = ATT1.funnel } = {}) => (await db.query(`
  select failed_at, offer_code, payment_type, installments_number, refusal_reason, checkout_country_iso
  from public.get_portable_recent_payment_failure_v1($1,$2,$3)
`, [tenant, funnel, phone])).rows;
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
// 1. Los cuatro rechazos reales, por la admision real.
// ---------------------------------------------------------------------------
for (const payload of CANCELED) {
  const admitted = await admitFailure(payload);
  if (admitted.outcome !== 'inserted') throw new Error(`${payload.id}: ${admitted.outcome}`);
}
{
  // Hotmart: 52 + 10 digitos; WhatsApp escribe desde 521 + 10.
  const [mx] = CANCELED;
  const stored = digits(mx.data.buyer.checkout_phone);
  if (!/^52\d{10}$/.test(stored)) throw new Error(`the captured Mexican phone changed shape: ${stored.length}`);
  const fromWhatsapp = `521${stored.slice(2)}`;
  for (const phone of [fromWhatsapp, stored]) {
    const row = one(await recent(phone), `recent ${phone.length}`);
    if (row.offer_code !== mx.data.purchase.offer.code
        || row.payment_type !== 'CREDIT_CARD'
        || row.installments_number !== mx.data.purchase.payment.installments_number
        || row.refusal_reason !== mx.data.purchase.payment.refusal_reason
        || row.checkout_country_iso !== 'MX'
        || !(row.failed_at instanceof Date)) {
      throw new Error(`recent failure of the Mexican capture: ${JSON.stringify(row)}`);
    }
  }
  const panama = CANCELED.find((payload) => payload.data.purchase.checkout_country.iso === 'PA');
  const row = one(await recent(digits(panama.data.buyer.checkout_phone)), 'recent Panama');
  if (row.refusal_reason !== panama.data.purchase.payment.refusal_reason
      || !row.refusal_reason.startsWith('Saldo insuficiente para o cart')
      || row.checkout_country_iso !== 'PA') {
    throw new Error(`recent failure of the Panama capture: ${JSON.stringify(row)}`);
  }
  results.recent_failure = {
    mexico_by_both_forms: true,
    installments: row.installments_number,
    panama_reason_kept: true,
  };
}

// ---------------------------------------------------------------------------
// 2. El mas reciente gana, y lo que no tiene que devolver nada.
// ---------------------------------------------------------------------------
{
  const [, first, second] = CANCELED;
  const phone = '525500000077';
  await admitFailure(withPhone(first, phone, 'older'));
  const later = withPhone(second, phone, 'newer');
  later.data.purchase.payment = { ...later.data.purchase.payment, installments_number: 6 };
  await admitFailure(later);
  await db.query(`
    update public.webhook_events set received_at = received_at - interval '1 hour'
    where external_event_id = $1
  `, [`${first.id}-older`]);
  const row = one(await recent(phone), 'latest wins');
  if (row.installments_number !== 6) throw new Error(`the latest failure did not win: ${JSON.stringify(row)}`);

  if ((await recent('525599999999')).length !== 0) throw new Error('a phone without failures returned a row');
  if ((await recent(phone, { tenant: 'otra-aliada' })).length !== 0) throw new Error('another ally returned a row');
  if ((await recent(phone, { funnel: 'otro-embudo' })).length !== 0) throw new Error('another funnel returned a row');

  // Mas de una semana.
  const stale = withPhone(first, '525500000088', 'stale');
  await admitFailure(stale);
  await db.query(`
    update public.webhook_events set received_at = clock_timestamp() - interval '8 days'
    where external_event_id = $1
  `, [stale.id]);
  if ((await recent('525500000088')).length !== 0) throw new Error('a failure older than a week returned a row');

  // Quien compro despues ya no tiene un pago rechazado. La compra real de OXXO,
  // re-telefoneada al mismo movil, por la admision real.
  const buyer = '525500000099';
  await admitFailure(withPhone(first, buyer, 'then-bought'));
  if ((await recent(buyer)).length !== 1) throw new Error('the failure before the purchase is missing');
  const purchase = withPhone(CASH[0], `521${buyer.slice(2)}`, 'bought');
  const bought = await admitPurchase(purchase);
  if (!['inserted', 'duplicate'].includes(bought.outcome)) {
    throw new Error(`the captured purchase was not admitted: ${bought.outcome}`);
  }
  if ((await recent(buyer)).length !== 0) throw new Error('a failure followed by a purchase returned a row');

  for (const bad of ['', '12', '+5215500000000', 'abc', '0525500000000']) {
    await expectError(`phone ${JSON.stringify(bad)}`, '22023', () => recent(bad));
  }
  await expectError('empty tenant', '22023', () => recent('525500000077', { tenant: ' ' }));
  results.recent_failure_negatives = {
    latest_wins: true, unknown_phone: 0, other_ally: 0, older_than_a_week: 0,
    bought_after: 0, invalid_input: '22023',
  };
}

// ---------------------------------------------------------------------------
// 3. Los eventos de la ficha.
// ---------------------------------------------------------------------------
const slipFrom = (cash, eventType, status, suffix) => {
  const payload = structuredClone(cash);
  payload.id = `${cash.id}-${suffix}`;
  payload.event = eventType;
  payload.data.purchase.status = status;
  delete payload.data.purchase.approved_date;
  return payload;
};
const admitSlip = async (payload, { productId } = {}) => one((await db.query(`
  select * from public.admit_portable_hotmart_payment_slip_event_v1($1,$2,$3,$4,$5::jsonb)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload)])).rows,
`slip ${payload.id}${productId ?? ''}`);
const storedEvents = async () => one((await db.query(`
  select count(*)::integer as events,
         count(*) filter (where provenance.webhook_event_id is not null)::integer as with_provenance
  from public.webhook_events event
  left join public.commercial_ally_hotmart_event_bindings provenance
    on provenance.webhook_event_id = event.id
  where event.event_type in ('PURCHASE_BILLET_PRINTED', 'PURCHASE_EXPIRED')
`)).rows, 'stored slips');
{
  const printed = slipFrom(CASH[0], 'PURCHASE_BILLET_PRINTED', 'BILLET_PRINTED', 'printed');
  const expired = slipFrom(CASH[1], 'PURCHASE_EXPIRED', 'EXPIRED', 'expired');
  const first = await admitSlip(printed);
  if (first.outcome !== 'inserted' || !first.webhook_event_id) throw new Error(`printed: ${JSON.stringify(first)}`);
  const stored = one((await db.query(`
    select event.event_type, event.processing_status, event.payload = $2::jsonb as same_payload,
           provenance.offer_ref, provenance.tenant_ref
    from public.webhook_events event
    join public.commercial_ally_hotmart_event_bindings provenance
      on provenance.webhook_event_id = event.id
    where event.id = $1
  `, [first.webhook_event_id, JSON.stringify(printed)])).rows, 'stored printed');
  if (stored.event_type !== 'PURCHASE_BILLET_PRINTED' || stored.processing_status !== 'received'
      || !stored.same_payload || stored.offer_ref !== printed.data.purchase.offer.code
      || stored.tenant_ref !== ATT1.tenant) {
    throw new Error(`printed was stored differently: ${JSON.stringify(stored)}`);
  }
  if ((await admitSlip(printed)).outcome !== 'duplicate') throw new Error('a replay was not a duplicate');
  const changedReplay = structuredClone(printed);
  changedReplay.data.purchase.status = 'EXPIRED';
  if ((await admitSlip(changedReplay)).outcome !== 'semantic_conflict') {
    throw new Error('a changed replay was not a semantic conflict');
  }
  if ((await admitSlip(expired)).outcome !== 'inserted') throw new Error('expired was not stored');

  const otherOffer = slipFrom(CASH[0], 'PURCHASE_BILLET_PRINTED', 'BILLET_PRINTED', 'other-offer');
  otherOffer.data.purchase.offer.code = 'zzzzzzzz';
  const otherProduct = slipFrom(CASH[0], 'PURCHASE_BILLET_PRINTED', 'BILLET_PRINTED', 'other-product');
  otherProduct.data.product.id = 1;
  for (const payload of [otherOffer, otherProduct]) {
    const admitted = await admitSlip(payload);
    if (admitted.outcome !== 'ignored_binding_mismatch' || admitted.webhook_event_id !== null) {
      throw new Error(`${payload.id}: ${JSON.stringify(admitted)}`);
    }
  }
  const counts = await storedEvents();
  if (counts.events !== 2 || counts.with_provenance !== 2) {
    throw new Error(`slip events stored: ${JSON.stringify(counts)}`);
  }

  const approved = slipFrom(CASH[0], 'PURCHASE_APPROVED', 'APPROVED', 'approved');
  await expectError('another event type', '22023', () => admitSlip(approved));
  const mismatched = slipFrom(CASH[0], 'PURCHASE_EXPIRED', 'EXPIRED', 'mismatch');
  await expectError('payload id mismatch', '22023', () => db.query(`
    select * from public.admit_portable_hotmart_payment_slip_event_v1($1,$2,$3,$4,$5::jsonb)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, 'otro-id', JSON.stringify(mismatched)]));

  // Una ficha no es un pago rechazado.
  if ((await recent(digits(printed.data.buyer.checkout_phone))).length !== 0) {
    throw new Error('a printed slip counted as a payment failure');
  }
  results.slip_events = {
    printed: 'inserted', replay: 'duplicate', changed_replay: 'semantic_conflict',
    expired: 'inserted', other_offer_or_product: 'ignored_binding_mismatch',
    other_type: '22023', stored: counts.events,
  };
}

// ---------------------------------------------------------------------------
// 4. La huella del inventario.
// ---------------------------------------------------------------------------
{
  const inventory = readFileSync(join(root, 'scripts/supabase_schema_inventory.sql'), 'utf8');
  const row = (await db.query(inventory)).rows.find((entry) => entry.version === TARGET.slice(0, 14));
  if (row?.fingerprint_status !== 'fingerprint_present' || row.present_markers !== 3) {
    throw new Error(`schema fingerprint ${TARGET}: ${JSON.stringify(row)}`);
  }
  results.schema_fingerprint = `${row.fingerprint_status} ${row.present_markers}/${row.total_markers}`;
}

console.log(`portable_payment_failure_context=OK ${JSON.stringify(results)}`);
