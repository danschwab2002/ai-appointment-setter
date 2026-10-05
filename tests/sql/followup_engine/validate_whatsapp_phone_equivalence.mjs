// La equivalencia de telefonos de WhatsApp del runtime portable (migracion
// 20261001000100), con las RPC reales, la reevaluacion REAL y el arranque real.
//
// El mismo movil llega con dos formas: el formulario guarda 52 + 10 digitos
// (Mexico) o 54 + 10 (Argentina), y Hotmart y el wa_id traen 521 + 10 y
// 549 + 10. Este validador prueba:
//   0. que la migracion cambia exactamente lo que dice: cuatro helpers
//      nuevos y una RPC nueva (la reserva portable del enlace), siete
//      reemplazadas, ninguna otra definicion ni grant de service_role
//      distinto, y las funciones que ejecuta Johanna identicas antes y
//      despues (pg_get_functiondef), la reserva compartida del enlace
//      incluida;
//   1. la tabla de la forma canonica y de sus variantes: Mexico y Argentina en
//      los dos sentidos, con '+', espacios y guiones, los 12 digitos que
//      empiezan con 521 o 549 intactos, Brasil y otros paises intactos, vacio
//      y nulo, idempotencia, y que buscar por variantes equivale a comparar
//      canonicas;
//   2. carrito: la intencion del formulario en una forma y el evento de
//      Hotmart en la otra quedan correlacionados (resolved, no conflict), se
//      planifican en un scope consented_intent y salen por la reevaluacion y
//      el arranque reales. Mexico y Argentina, en los dos sentidos, mas la
//      regresion con el telefono identico;
//   3. pago fallido: se planifica, la intencion concede el permiso y la
//      evidencia dice phone_match = whatsapp_equivalent (o exact);
//   4. compra aprobada con el telefono en la otra forma: la intencion pasa a
//      purchased;
//   5. F1: contacts.phone, que es adonde sale el envio, tiene que ser
//      canonicamente el telefono de la intencion (con '+' y espacios pasa; con
//      otro numero o nulo, consented_intent_contact_phone_mismatch);
//   6. opt-out previo en la otra forma: guardado unmatched bajo el wa_id
//      (521...), con la intencion y la identidad en 52..., no planifica en un
//      modo con consentimiento, no arranca si llega con el envio en vuelo y no
//      concede permiso al pago fallido en manual_cohort. En manual_cohort el
//      carrito (Mexico y Argentina) y el pago fallido que usa el permiso de
//      ese carrito se reservan, pero el arranque los rechaza
//      (pilot_chatwoot_opt_out_stop) sin consumir cupo; tambien con la baja
//      del numero de contacts.phone, que es adonde sale el envio. El opt-out
//      de otra cuenta no frena, y sin opt-out el carrito de la cohorte sale;
//   7. negativos: otros diez digitos siguen dando conflict en la correlacion y
//      consented_intent_phone_mismatch en el helper;
//   8. Johanna: el correlador compartido sigue exacto (52 contra 521 da
//      conflict por admit_and_correlate_hotmart_cart_abandonment) y el
//      portable es el compartido con sus tres reemplazos y nada mas (deriva);
//   9. el inventario de esquema da fingerprint_present en las filas de las
//      migraciones cuyas funciones se copiaron;
//  10. el enlace de pago del entrante (reserve_portable_checkout_issuance_v2):
//      quien dejo el formulario con 52... (54...) y escribe desde su wa_id
//      521... (549...) recibe el enlace con la oferta de su landing, el sck y
//      el fbclid del formulario y la intencion del formulario, sin fabricar
//      una segunda; tambien si escribio antes del formulario; quien ya compro
//      no recibe otro enlace; un opt-out en la otra forma, en otra
//      conversacion, frena. La reserva compartida (Johanna) sigue exacta con
//      el mismo caso, y la portable es la compartida con sus reemplazos y nada
//      mas (deriva). El scope entrante y el catalogo de las tres ofertas se
//      siembran como despliegue/base/aprovisionar-att1.sql de la instancia: el
//      hotlink como external_product_id y la primera oferta por defecto.
//
// Datos: binding, ofertas, landings, producto, Chatwoot y consentimiento de
// tests/fixtures/instances/att1/instancia.toml; politica y ventanas de
// tests/fixtures/instances/att1/politica-piloto.json (con la misma desviacion
// documentada de la ventana de envio que validate_att1_portable_chain.mjs).
// Formulario: los goldens del traductor de GHL
// (tests/fixtures/ghl/expected/), que salen de los dos envios capturados, con
// id, fecha y comprador (email y telefono) sustituidos; el pais, la landing,
// la oferta y el consentimiento son los del golden. Las variantes con 521 y
// 549 en el formulario son derivadas del golden, no capturas: el adaptador no
// le saca el 9 al numero argentino (asi que un formulario real puede traer
// 549), y nunca produce 521; ese sentido se prueba porque la comparacion es
// simetrica. Carrito: la captura
// tests/fixtures/hotmart_cart_abandonment_rejected_v1.json con producto,
// oferta, id, fecha y comprador sustituidos. Deuda: no hay PURCHASE_CANCELED
// ni PURCHASE_APPROVED capturados; el pago fallido usa el precedente inline de
// validate_commercial_ally_payment_failure_recovery.mjs y la compra el de
// validate_commercial_ally_multi_offer.mjs, con los valores de ATT1. Los
// telefonos son sinteticos.
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
// 0. La migracion cambia exactamente lo que dice. Se saca una foto de todas
//    las funciones de public antes y despues de aplicarla.
// ---------------------------------------------------------------------------
const TARGET = '20261001000100_whatsapp_phone_equivalence.sql';
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

const NEW_FUNCTIONS = [
  '_correlate_portable_hotmart_purchase_intent(uuid)',
  '_portable_chatwoot_opt_out_stop(bigint,uuid,text)',
  '_whatsapp_phone_canonical(text)',
  '_whatsapp_phone_variants(text)',
];
// La reserva portable del enlace: un entrypoint del bridge, solo service_role.
const NEW_ENTRYPOINTS = [
  'reserve_portable_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamp with time zone)',
];
const REPLACED_FUNCTIONS = [
  '_portable_consented_intent_reason(uuid,uuid,text)',
  'admit_portable_hotmart_cart_abandonment(text,text,integer,text,jsonb,text,text)',
  'admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text)',
  'admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text)',
  'plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamp with time zone,bigint,bigint,text,text,integer)',
  // Los dos arranques del piloto: solo los llama el bridge con la frontera.
  'mark_lancemos_pilot_request_started(uuid,uuid,text,bigint,timestamp with time zone)',
  'mark_portable_payment_failure_request_started(uuid,uuid,text,bigint,timestamp with time zone)',
];
// Lo que ejecuta Johanna (sin manifiesto, con el piloto y los portable_*
// apagados) y lo que comparte con el runtime portable.
const JOHANNA_FUNCTIONS = [
  'correlate_hotmart_purchase_intent(uuid)',
  '_admit_hotmart_purchase_intent_identity(uuid,text,text)',
  'admit_and_correlate_hotmart_cart_abandonment(text,jsonb,text,text)',
  'admit_and_correlate_hotmart_purchase_approved(text,jsonb,text,text)',
  'admit_johanna_hotmart_cart_abandonment(text,jsonb,text,text)',
  'admit_johanna_payment_failure(text,jsonb,text,text)',
  'admit_observed_lead_precheckout(text,jsonb,jsonb)',
  'apply_chatwoot_inbound_opt_out(bigint,bigint,bigint,bigint,text,timestamp with time zone,text)',
  'mark_followup_request_started(uuid,uuid,text,bigint,timestamp with time zone)',
  'reevaluate_followup_action(uuid,text,bigint,timestamp with time zone,boolean,text,text,timestamp with time zone,text,boolean,boolean,boolean,boolean,boolean)',
  'bootstrap_proactive_lead_identity(text,uuid,text,integer,bigint,text,text)',
  // El enlace de pago del entrante: la reserva compartida no se redefine.
  'reserve_chatwoot_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamp with time zone)',
  'authorize_chatwoot_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,timestamp with time zone)',
  'finalize_chatwoot_checkout_issuance_v2(uuid,text,bigint,text,timestamp with time zone)',
  'has_chatwoot_opt_out_stop(bigint,bigint,bigint,text)',
  // Del piloto, no reemplazadas: delegan en el helper del consentimiento.
  '_lancemos_pilot_audience_intent(text,integer,uuid,text,uuid,text)',
  'evaluate_lancemos_pilot_scope(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid)',
  'authorize_lancemos_pilot_request_start(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid,uuid,uuid,timestamp with time zone)',
  'plan_lancemos_pilot_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamp with time zone,bigint,bigint,text,text,integer)',
];
const sameSet = (left, right) => JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
const added = [...after.keys()].filter((signature) => !before.has(signature));
const removed = [...before.keys()].filter((signature) => !after.has(signature));
const changed = [...before.keys()].filter((signature) => after.has(signature)
  && after.get(signature).definition !== before.get(signature).definition);
const aclChanged = [...before.keys()].filter((signature) => after.has(signature)
  && (after.get(signature).service_x !== before.get(signature).service_x
      || after.get(signature).api_x !== before.get(signature).api_x));
if (!sameSet(added, [...NEW_FUNCTIONS, ...NEW_ENTRYPOINTS]) || removed.length !== 0
    || !sameSet(changed, REPLACED_FUNCTIONS) || aclChanged.length !== 0) {
  throw new Error(`the migration changed something else: ${JSON.stringify({ added, removed, changed, aclChanged })}`);
}
for (const signature of JOHANNA_FUNCTIONS) {
  if (!before.has(signature)
      || after.get(signature)?.definition !== before.get(signature).definition) {
    throw new Error(`a function Johanna runs changed or is missing: ${signature}`);
  }
}
for (const signature of NEW_FUNCTIONS) {
  const row = after.get(signature);
  if (row.service_x !== false || row.api_x !== false) {
    throw new Error(`a new helper is executable by an API role: ${JSON.stringify({ signature, service_x: row.service_x, api_x: row.api_x })}`);
  }
}
for (const signature of NEW_ENTRYPOINTS) {
  const row = after.get(signature);
  if (row.service_x !== true || row.api_x !== false) {
    throw new Error(`a new entrypoint is not service_role only: ${JSON.stringify({ signature, service_x: row.service_x, api_x: row.api_x })}`);
  }
}
for (const signature of REPLACED_FUNCTIONS) {
  const expectedService = signature !== '_portable_consented_intent_reason(uuid,uuid,text)';
  const row = after.get(signature);
  if (row.service_x !== expectedService || row.api_x !== false) {
    throw new Error(`a replaced function changed its grants: ${JSON.stringify({ signature, service_x: row.service_x, api_x: row.api_x })}`);
  }
}
results.migration_scope = {
  new: added.length,
  replaced: changed.length,
  johanna_identical: JOHANNA_FUNCTIONS.length,
  functions_unchanged: before.size - changed.length,
};

// ---------------------------------------------------------------------------
// 1. La forma canonica y sus variantes.
// ---------------------------------------------------------------------------
const CANONICAL_TABLE = [
  // Mexico: 521 + 10 pasa a 52 + 10; 52 + 10 queda.
  ['5215555550101', '525555550101'],
  ['525555550101', '525555550101'],
  ['+52 1 55 5555 0101', '525555550101'],
  ['+52 (55) 5555-0101', '525555550101'],
  // Argentina: 549 + 10 pasa a 54 + 10; 54 + 10 queda.
  ['5491155550101', '541155550101'],
  ['541155550101', '541155550101'],
  ['+54 9 11 5555-0101', '541155550101'],
  // 12 digitos que empiezan con 521 o 549: un nacional de 10 que arranca con
  // 1 o con 9. No se toca.
  ['521555555010', '521555555010'],
  ['549115555010', '549115555010'],
  // 14 digitos: tampoco.
  ['52155555501019', '52155555501019'],
  // Brasil (el noveno digito) queda afuera: las dos formas no se igualan.
  ['5511955550101', '5511955550101'],
  ['551155550101', '551155550101'],
  // Otros paises.
  ['12025550101', '12025550101'],
  ['+1 (202) 555-0101', '12025550101'],
  ['34600555010', '34600555010'],
  // Sin digitos.
  ['', null],
  ['sin telefono', null],
  [null, null],
];
const canonicalRows = (await db.query(`
  select item.ordinality::integer as position,
         public._whatsapp_phone_canonical(item.phone) as canonical,
         public._whatsapp_phone_canonical(public._whatsapp_phone_canonical(item.phone)) as twice,
         public._whatsapp_phone_variants(item.phone) as variants
  from unnest($1::text[]) with ordinality as item(phone, ordinality)
  order by item.ordinality
`, [CANONICAL_TABLE.map(([input]) => input)])).rows;
canonicalRows.forEach((row, index) => {
  const [input, expected] = CANONICAL_TABLE[index];
  if (row.canonical !== expected || row.twice !== expected) {
    throw new Error(`canonical(${JSON.stringify(input)}) = ${row.canonical} (twice ${row.twice}), expected ${expected}`);
  }
  if (expected === null ? row.variants !== null
    : row.variants[0] !== expected || row.variants.length > 2) {
    throw new Error(`variants(${JSON.stringify(input)}) = ${JSON.stringify(row.variants)}`);
  }
});
const variantsOf = (input) => canonicalRows[CANONICAL_TABLE.findIndex(([value]) => value === input)].variants;
if (JSON.stringify(variantsOf('525555550101')) !== JSON.stringify(['525555550101', '5215555550101'])
    || JSON.stringify(variantsOf('5215555550101')) !== JSON.stringify(variantsOf('525555550101'))
    || JSON.stringify(variantsOf('541155550101')) !== JSON.stringify(['541155550101', '5491155550101'])
    || JSON.stringify(variantsOf('5491155550101')) !== JSON.stringify(variantsOf('541155550101'))
    || JSON.stringify(variantsOf('5511955550101')) !== JSON.stringify(['5511955550101'])
    || JSON.stringify(variantsOf('521555555010')) !== JSON.stringify(['521555555010', '5211555555010'])
    || JSON.stringify(variantsOf('12025550101')) !== JSON.stringify(['12025550101'])) {
  throw new Error(`unexpected variants: ${JSON.stringify(canonicalRows.map((row) => row.variants))}`);
}
// Sobre valores de solo digitos (lo que guardan las columnas), buscar por
// variantes es lo mismo que comparar canonicas.
const digitInputs = CANONICAL_TABLE.map(([input]) => input)
  .filter((input) => input !== null && /^[0-9]+$/.test(input));
const disagreements = one((await db.query(`
  select count(*)::integer as count
  from unnest($1::text[]) as stored(phone)
  cross join unnest($1::text[]) as probe(phone)
  where (stored.phone = any(public._whatsapp_phone_variants(probe.phone)))
        is distinct from (
          public._whatsapp_phone_canonical(stored.phone)
            = public._whatsapp_phone_canonical(probe.phone)
        )
`, [digitInputs])).rows, 'variants against canonical').count;
if (disagreements !== 0) {
  throw new Error(`variants and canonical disagree on ${disagreements} pairs`);
}
results.canonical_table = {
  rows: CANONICAL_TABLE.length,
  pairs_checked: digitInputs.length ** 2,
};

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
    default: offer.por_defecto === true,
  };
});
if (offers.length !== 3 || !offers[0].default || offers.filter((offer) => offer.default).length !== 1) {
  throw new Error(`ATT1 declares three offers and the first is the default: ${JSON.stringify(offers)}`);
}
const ATT1 = {
  tenant: required(manifest.instancia?.tenant_ref, 'instancia.tenant_ref'),
  funnel: required(manifest.instancia?.funnel_ref, 'instancia.funnel_ref'),
  ally: required(manifest.instancia?.ally_ref, 'instancia.ally_ref'),
  bindingVersion: required(manifest.instancia?.binding_version, 'instancia.binding_version'),
  brand: required(manifest.instancia?.marca, 'instancia.marca'),
  timezone: required(manifest.instancia?.zona_horaria, 'instancia.zona_horaria'),
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
const pilotScope = required(PILOT.pilot_scope, 'politica-piloto.pilot_scope');
const pilotPolicy = required(PILOT.policy, 'politica-piloto.policy');
const POLICY = required(pilotPolicy.policy_key, 'policy.policy_key');
const POLICY_VERSION = required(pilotPolicy.version, 'policy.version');
const CHANNEL_PROVIDER = required(pilotScope.channel_provider, 'pilot_scope.channel_provider');
const CHANNEL_REF = `${required(pilotScope.channel_account_ref_prefix, 'pilot_scope.channel_account_ref_prefix')}${ATT1.inboxId}`;
if (CHANNEL_PROVIDER !== 'waba' || pilotPolicy.max_automatic_messages !== 1) {
  throw new Error(`politica-piloto.json changed shape: ${JSON.stringify({ CHANNEL_PROVIDER, pilotPolicy })}`);
}

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
// DESVIACION DOCUMENTADA (la misma de la cadena de ATT1): la ventana de envio
// usa los dias del fixture de 00:00 a 23:59, porque la puerta de arranque exige
// p_now a +-5 minutos del reloj de la base.
await db.query(`
  insert into public.followup_policy_versions
    (policy_key, version, status, purpose, timezone, business_windows,
     grace_period, expires_after, max_automatic_messages, steps,
     approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5::jsonb,$6::interval,$7::interval,$8,$9::jsonb,
          'operator-test',now(),now())
`, [POLICY, POLICY_VERSION, required(pilotPolicy.purpose, 'policy.purpose'), ATT1.timezone,
  JSON.stringify(required(pilotPolicy.business_windows, 'policy.business_windows')
    .map((window) => ({ ...window, start: '00:00', end: '23:59' }))),
  required(pilotPolicy.grace_period, 'policy.grace_period'),
  required(pilotPolicy.expires_after, 'policy.expires_after'),
  pilotPolicy.max_automatic_messages,
  JSON.stringify(required(pilotPolicy.steps, 'policy.steps'))]);

// Dos scopes con los valores de ATT1: el de produccion (consented_intent) y el
// de cohorte manual, cada uno con su control armado. La cohorte admite diez
// contactos: el caso 6 inscribe siete.
const OPEN = { key: 'att1-telefonos-abierta', version: 1, mode: 'consented_intent' };
const MANUAL = { key: 'att1-telefonos-manual', version: 1, mode: 'manual_cohort' };
for (const scope of [OPEN, MANUAL]) {
  await db.query(`
    insert into public.pilot_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
       channel, channel_provider, channel_account_ref, source, source_event_type,
       additional_source_event_types, external_product_id, offer_code,
       additional_offer_codes, purpose, policy_key, policy_version, timezone,
       max_cohort_contacts, max_outbound_request_starts_total,
       max_outbound_request_starts_per_day, audience_mode,
       approved_by, approved_at, published_at)
    values ($1,$2,'published',$3,$4,$5,'whatsapp',$6,$7,'hotmart',
            'PURCHASE_OUT_OF_SHOPPING_CART',$8::text[],$9,$10,$11::text[],
            'cart_recovery',$12,$13,$14,10,20,20,$15,'operator-test',now(),now())
  `, [scope.key, scope.version, ATT1.tenant, ATT1.accountId, ATT1.inboxId,
    CHANNEL_PROVIDER, CHANNEL_REF, ['PURCHASE_CANCELED'], String(ATT1.productId),
    defaultOffer.offer_code, additionalOffers.map((offer) => offer.offer_code),
    POLICY, POLICY_VERSION, ATT1.timezone, scope.mode]);
  await db.query(`
    insert into public.pilot_runtime_controls
      (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
    values ($1,$2,'inactive',0,'operator-test','default-off')
  `, [scope.key, scope.version]);
  const armed = one((await db.query(`
    select * from public.set_lancemos_pilot_runtime_state($1,$2,0,'armed','operator-test','controlled-test')
  `, [scope.key, scope.version])).rows, `${scope.key} arm`);
  if (armed.runtime_state !== 'armed') throw new Error(`${scope.key} was not armed`);
}

const expectError = async (label, action, { code, message, detail }) => {
  let error = null;
  await db.exec('begin');
  try {
    await action();
  } catch (caught) {
    error = caught;
  } finally {
    await db.exec('rollback');
  }
  if (error?.code !== code
      || (message !== undefined && error?.message !== message)
      || (detail !== undefined && error?.detail !== detail)) {
    throw new Error(`${label}: expected ${code} ${message ?? ''} ${detail ?? ''}, got ${error?.code} ${error?.message} ${error?.detail}`);
  }
};

// ---------------------------------------------------------------------------
// Tiempos, personas y la cadena (formulario, evento, contacto, plan, envio).
// ---------------------------------------------------------------------------
const dbNow = async () => new Date(
  (await db.query('select clock_timestamp() as now')).rows[0].now,
);
const baseMs = Math.floor((await dbNow()).getTime() / 1000) * 1000;
const at = (minutes) => new Date(baseMs + minutes * 60_000);
const SUBMITTED_AT = at(-50);
const CART_AT = at(-30);
const FAILED_AT = at(-10);
const PURCHASED_AT = at(-5);

// Los goldens del traductor de GHL: el de la landing -d es un movil mexicano
// (52 + 10 digitos) y el de ads-a uno argentino (54 + 10).
const golden = (name, { country, callingCode, offerCode }) => {
  const file = JSON.parse(readFileSync(join(root, 'tests/fixtures/ghl/expected', name), 'utf8'));
  const { raw_payload: raw, canonical_payload: canonical } = file;
  const offer = offers.find((candidate) => candidate.offer_code === offerCode);
  if (!raw || !canonical || raw.id !== canonical.external_submission_id
      || canonical.identity.phone_country_iso !== country
      || raw.data.buyer.phone_country_code !== callingCode
      || !new RegExp(`^${callingCode}[0-9]{10}$`).test(canonical.identity.phone)
      || canonical.commerce.offer_ref !== offerCode
      || offer === undefined) {
    throw new Error(`${name} is not the ${country} translator golden this validator expects`);
  }
  return { raw, canonical, offer, callingCode };
};
const GOLDEN = {
  MX: golden('ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json',
    { country: 'MX', callingCode: '52', offerCode: '2uafw5bg' }),
  AR: golden('ghl_form_webhook_att1_ads_a_20260929.lead_precheckout.json',
    { country: 'AR', callingCode: '54', offerCode: 'gopi6lh7' }),
};
const CAPTURED_CART = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/hotmart_cart_abandonment_rejected_v1.json'), 'utf8',
)).payload;

// ULID como el del adaptador (generate_issuance_ulid): 48 bits de milisegundos
// y 80 aleatorios, en base32 de Crockford.
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

// Una persona con un movil sintetico en sus dos formas: plain (52 / 54 + 10
// digitos, la que guarda el formulario) y whatsapp (521 / 549 + 10, la de
// Hotmart y el wa_id).
let personIndex = 0;
const person = (label, country) => {
  personIndex += 1;
  const suffix = String(personIndex).padStart(2, '0');
  const { callingCode, offer } = GOLDEN[country];
  const national = `${country === 'MX' ? '55' : '11'}555502${suffix}`;
  return {
    label,
    country,
    offer,
    name: `Compradora telefonos ${label}`,
    email: `att1-phones-${suffix}@example.test`,
    plain: `${callingCode}${national}`,
    whatsapp: `${callingCode}${country === 'MX' ? '1' : '9'}${national}`,
    other: `${callingCode}${country === 'MX' ? '33' : '35'}555509${suffix}`,
  };
};

// El formulario: el golden del pais con id, fecha y comprador sustituidos, por
// la admision real. Devuelve la intencion y el envio.
const submitForm = async (lead, { phone = lead.plain } = {}) => {
  const { raw: goldenRaw, canonical: goldenCanonical, callingCode } = GOLDEN[lead.country];
  const raw = structuredClone(goldenRaw);
  const canonical = structuredClone(goldenCanonical);
  const id = ulidAt(SUBMITTED_AT.getTime());
  const dedupeKey = `${raw.source.site}:${raw.data.offer.code}:${lead.email}`;
  raw.id = id;
  raw.created_at = SUBMITTED_AT.toISOString();
  raw.data.buyer.email = lead.email;
  raw.data.buyer.phone = `+${phone}`;
  raw.data.buyer.phone_national = phone.slice(callingCode.length);
  raw.dedupe_key = dedupeKey;
  canonical.external_submission_id = id;
  canonical.submitted_at = SUBMITTED_AT.toISOString().replace('.000Z', 'Z');
  canonical.identity.email = lead.email;
  canonical.identity.phone = phone;
  canonical.dedupe_key = dedupeKey;
  const admitted = one((await db.query(`
    select * from public.admit_portable_observed_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, id, JSON.stringify(raw),
    JSON.stringify(canonical)])).rows, `${lead.label} form`);
  const intent = await intentOf(admitted.purchase_intent_id, `${lead.label} intent`);
  if (admitted.outcome !== 'inserted'
      || intent.normalized_phone !== phone
      || intent.offer_ref !== lead.offer.offer_code
      || intent.whatsapp_contact_authorized !== true
      || intent.activation_authorized !== true) {
    throw new Error(`${lead.label}: unexpected form admission: ${JSON.stringify({ outcome: admitted.outcome, offer: intent.offer_ref })}`);
  }
  lead.intent = admitted.purchase_intent_id;
  lead.submission = admitted.submission_id;
  return { intent: admitted.purchase_intent_id, submission: admitted.submission_id };
};
const intentOf = async (id, label) => one((await db.query(`
  select normalized_phone, offer_ref, lifecycle_state, current_classification,
         whatsapp_contact_authorized, activation_authorized
  from public.purchase_intents where id = $1
`, [id])).rows, label);

// Carrito capturado, con el telefono que manda Hotmart.
const cartPayload = (lead, phone) => {
  const payload = structuredClone(CAPTURED_CART);
  payload.id = `att1-phones-cart-${lead.email}`;
  payload.creation_date = CART_AT.getTime();
  payload.data.product = { id: ATT1.productId, name: ATT1.productName };
  payload.data.offer = { code: lead.offer.offer_code };
  payload.data.buyer = { name: lead.name, email: lead.email, phone };
  return payload;
};
const correlationOf = async (eventId) => (await db.query(`
  select outcome, purchase_intent_id, matched_by, reason_code
  from public.hotmart_purchase_intent_correlations where webhook_event_id = $1
`, [eventId])).rows[0] ?? null;
const admitCart = async (lead, { phone }) => {
  const payload = cartPayload(lead, phone);
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
    JSON.stringify(payload), lead.email, phone])).rows, `${lead.label} cart`);
  if (admitted.outcome !== 'inserted') {
    throw new Error(`${lead.label}: cart was not admitted: ${admitted.outcome}`);
  }
  return {
    eventId: admitted.webhook_event_id,
    abandonedAt: new Date(payload.creation_date),
    correlation: await correlationOf(admitted.webhook_event_id),
  };
};

// Pago fallido (precedente inline, sin captura).
let transactionIndex = 0;
const admitFailure = async (lead, { phone }) => {
  transactionIndex += 1;
  const payload = {
    id: `att1-phones-failure-${lead.email}`,
    creation_date: FAILED_AT.getTime(),
    event: 'PURCHASE_CANCELED',
    version: '2.0.0',
    data: {
      buyer: { name: lead.name, email: lead.email, checkout_phone: `+${phone}` },
      product: { id: ATT1.productId, name: ATT1.productName },
      purchase: {
        transaction: `HPATT1TEL${String(transactionIndex).padStart(3, '0')}`,
        status: 'CANCELED',
        offer: { code: lead.offer.offer_code },
        payment: { refusal_reason: 'insufficient_funds' },
      },
      checkout_country: { iso: lead.country, name: lead.country },
    },
  };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
    JSON.stringify(payload), lead.email, phone])).rows, `${lead.label} failure`);
  const detail = one((await db.query(`
    select correlation_outcome, purchase_intent_id
    from public.commercial_ally_payment_failure_details where webhook_event_id = $1
  `, [admitted.webhook_event_id])).rows, `${lead.label} failure correlation`);
  if (admitted.outcome !== 'inserted') {
    throw new Error(`${lead.label}: payment failure was not admitted: ${admitted.outcome}`);
  }
  return {
    eventId: admitted.webhook_event_id,
    failedAt: new Date(payload.creation_date),
    correlationOutcome: detail.correlation_outcome,
    intentId: detail.purchase_intent_id,
  };
};

// Compra aprobada (precedente inline, sin captura).
const admitPurchase = async (lead, { phone }) => {
  transactionIndex += 1;
  const payload = {
    id: `att1-phones-purchase-${lead.email}`,
    creation_date: PURCHASED_AT.getTime(),
    event: 'PURCHASE_APPROVED',
    version: '2.0.0',
    data: {
      product: { id: ATT1.productId, ucode: 'ATT1-PHONES-UCODE' },
      buyer: { email: lead.email, checkout_phone: `+${phone}` },
      purchase: {
        approved_date: PURCHASED_AT.getTime(),
        status: 'APPROVED',
        transaction: `HPATT1TEL${String(transactionIndex).padStart(3, '0')}`,
        offer: { code: lead.offer.offer_code },
      },
    },
  };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload),
    lead.email, phone])).rows, `${lead.label} purchase`);
  const correlation = one((await db.query(`
    select outcome, purchase_intent_id, matched_by, reason_code
    from public.portable_hotmart_purchase_correlations where webhook_event_id = $1
  `, [admitted.webhook_event_id])).rows, `${lead.label} purchase correlation`);
  return { outcome: admitted.outcome, correlation };
};

// Lo que deja resolve_event antes de planificar: el contacto con el telefono
// del evento de Hotmart en contacts.phone y en su contact_point. contactPhone
// permite que contacts.phone difiera (F1).
const createContact = async (lead, eventId, { phone, contactPhone = phone, source = 'hotmart' }) => {
  lead.contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone, country_iso)
    values ($1,$2,$3,$4) returning id
  `, [lead.name, lead.email, contactPhone, lead.country])).rows, `${lead.label} contact`).id;
  await db.query(`
    insert into public.contact_points
      (contact_id, type, raw_value, normalized_value, source, source_event_id)
    values ($1,'email',$2,$2,$4,$5), ($1,'phone',$3,$3,$4,$5)
  `, [lead.contact, lead.email, phone, source, eventId]);
  lead.destination = phone;
};
const consentReason = async (lead, destination = lead.destination) => one((await db.query(`
  select * from public._portable_consented_intent_reason($1,$2,$3)
`, [lead.intent, lead.contact, destination])).rows, `${lead.label} consent reason`);
const enroll = async (lead, scope) => {
  const { generation } = one((await db.query(`
    select generation from public.pilot_runtime_controls where scope_key = $1
  `, [scope.key])).rows, `${scope.key} generation`);
  const member = one((await db.query(`
    select * from public.set_lancemos_pilot_cohort_member($1,$2,$3,$4,'active','operator-test','controlled-test')
  `, [scope.key, scope.version, lead.contact, generation])).rows, `${lead.label} enrollment`);
  if (member.member_status !== 'active') {
    throw new Error(`${lead.label} was not enrolled: ${JSON.stringify(member)}`);
  }
};

// Los mismos argumentos que arma resolution.resolve_event con el binding
// portable: el destino es el telefono del evento de Hotmart.
const planCart = (lead, cart, scope) => db.query(`
  select * from public.plan_lancemos_pilot_cart_recovery(
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
  )
`, [cart.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  lead.offer.offer_code, POLICY, POLICY_VERSION, cart.abandonedAt.toISOString(),
  ATT1.accountId, ATT1.inboxId, lead.destination, scope.key, scope.version]);
const planFailure = (lead, failure, scope) => db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
  )
`, [failure.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  lead.offer.offer_code, POLICY, POLICY_VERSION, failure.failedAt.toISOString(),
  ATT1.accountId, ATT1.inboxId, lead.destination, scope.key, scope.version]);
const durableWork = async (lead) => one((await db.query(`
  select
    (select count(*)::integer from public.recovery_cases where contact_id = $1) as cases,
    (select count(*)::integer from public.contact_authorizations where contact_id = $1) as grants
`, [lead.contact])).rows, `${lead.label} durable work`);
// Un rechazo de la planificacion no deja caso ni permiso.
const expectPlanRejection = async (label, lead, action, detail) => {
  await expectError(label, action, { code: '55000', message: 'pilot_scope_rejected', detail });
  const effects = await durableWork(lead);
  if (effects.cases !== 0 || effects.grants !== 0) {
    throw new Error(`${label}: a rejected plan left durable work: ${JSON.stringify(effects)}`);
  }
  return `pilot_scope_rejected:${detail}`;
};
const expectBinding = async (label, plan, scope, lead) => {
  const binding = one((await db.query(`
    select scope_key, audience_mode, audience_purchase_intent_id,
           audience_precheckout_submission_id
    from public.pilot_recovery_case_bindings where recovery_case_id = $1
  `, [plan.recovery_case_id])).rows, `${label} binding`);
  const consented = scope.mode !== 'manual_cohort';
  if (binding.scope_key !== scope.key
      || binding.audience_mode !== scope.mode
      || binding.audience_purchase_intent_id !== (consented ? lead.intent : null)
      || binding.audience_precheckout_submission_id !== (consented ? lead.submission : null)) {
    throw new Error(`${label}: unexpected binding ${JSON.stringify(binding)}`);
  }
};
const startsOf = async (scope) => one((await db.query(`
  select count(*)::integer as count from public.pilot_outbound_request_authorizations
  where scope_key = $1
`, [scope.key])).rows, `${scope.key} starts`).count;
const activeAllowed = async (lead) => (await db.query(`
  select evidence from public.contact_authorizations
  where contact_id = $1 and channel = 'whatsapp' and purpose = 'cart_recovery'
    and authorization_status = 'allowed'
    and valid_from <= clock_timestamp()
    and (valid_until is null or valid_until > clock_timestamp())
`, [lead.contact])).rows;

// El dispatcher en modo directo: claim, la reevaluacion REAL, reserva en
// approved_template y el arranque por el anchor_type de la accion.
const startOperationFor = (anchorType) => (anchorType === 'payment_failure'
  ? 'mark_portable_payment_failure_request_started'
  : 'mark_lancemos_pilot_request_started');
const claim = async (lead, plan, anchor) => {
  const worker = `att1-phones-${lead.label}`;
  const now = await dbNow();
  const claimed = (await db.query(`
    select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
  `, [worker, now])).rows;
  if (claimed.length !== 1 || claimed[0].id !== plan.scheduled_action_id
      || claimed[0].anchor_type !== anchor) {
    throw new Error(`${lead.label}: claimed ${JSON.stringify(claimed.map((row) => [row.id, row.anchor_type]))}, expected ${plan.scheduled_action_id} ${anchor}`);
  }
  const lease = claimed[0].lease_generation;
  const decision = one((await db.query(`
    select * from public.reevaluate_followup_action($1,$2,$3,$4)
  `, [plan.scheduled_action_id, worker, lease, now])).rows, `${lead.label} reevaluation`);
  return { worker, lease, now, decision, operation: startOperationFor(anchor) };
};
const reserve = async (lead, plan, anchor) => {
  const { worker, lease, now, decision, operation } = await claim(lead, plan, anchor);
  if (decision.decision !== 'execute' || decision.reason_code !== 'eligible_for_execution') {
    throw new Error(`${lead.label}: the real reevaluation did not execute: ${JSON.stringify(decision)}`);
  }
  const attempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
  `, [plan.scheduled_action_id, worker, lease, decision.case_version,
    decision.sequence_revision, now])).rows, `${lead.label} reservation`);
  return { worker, lease, attempt, operation };
};
let messageNumber = 0;
const dispatch = async (lead, plan, anchor) => {
  const { worker, lease, attempt, operation } = await reserve(lead, plan, anchor);
  const started = one((await db.query(`
    select * from public.${operation}($1,$2,$3,$4,$5)
  `, [plan.scheduled_action_id, attempt.id, worker, lease, await dbNow()])).rows,
  `${lead.label} request start`);
  if (started.phase !== 'request_started' || started.pilot_authorization_id == null) {
    throw new Error(`${lead.label}: ${operation} did not start: ${JSON.stringify(started)}`);
  }
  messageNumber += 1;
  const accepted = one((await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
  `, [plan.scheduled_action_id, attempt.id, worker, lease, String(920100 + messageNumber),
    `att1-phones-wamid-${messageNumber}`,
    `Plantilla aprobada de ATT1 para ${lead.name} (${ATT1.productName})`,
    await dbNow()])).rows, `${lead.label} acceptance`);
  if (accepted.status !== 'accepted_by_chatwoot') {
    throw new Error(`${lead.label}: acceptance did not finalize: ${JSON.stringify(accepted)}`);
  }
  return 'accepted';
};

// El opt-out real de Chatwoot (apply_chatwoot_inbound_opt_out), desde un wa_id.
let optOutMessage = 0;
const optOutFrom = async (waId, { accountId = ATT1.accountId, inboxId = ATT1.inboxId } = {}) => {
  optOutMessage += 1;
  return one((await db.query(`
    select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,$6,'unsubscribe')
  `, [accountId, inboxId, 8800 + optOutMessage, 98800 + optOutMessage, waId,
    await dbNow()])).rows, 'opt-out');
};

// ---------------------------------------------------------------------------
// 2. Carrito: la intencion en una forma y Hotmart en la otra.
// ---------------------------------------------------------------------------
const cartCase = async (label, country, { form, hotmart }) => {
  const lead = person(label, country);
  await submitForm(lead, { phone: lead[form] });
  const cart = await admitCart(lead, { phone: lead[hotmart] });
  const intent = await intentOf(lead.intent, `${label} intent after the cart`);
  if (cart.correlation?.outcome !== 'resolved'
      || cart.correlation.purchase_intent_id !== lead.intent
      || cart.correlation.matched_by !== 'email_and_phone'
      || intent.current_classification !== 'confirmed_abandonment'
      || intent.activation_authorized !== true) {
    throw new Error(`${label}: the cart did not correlate with the form intent: ${JSON.stringify({ correlation: cart.correlation?.outcome, reason: cart.correlation?.reason_code, classification: intent.current_classification })}`);
  }
  await createContact(lead, cart.eventId, { phone: lead[hotmart] });
  const consent = await consentReason(lead);
  if (consent.reason_code !== 'consented_intent_ok'
      || consent.precheckout_submission_id !== lead.submission) {
    throw new Error(`${label}: the intent is not consented for the Hotmart phone: ${consent.reason_code}`);
  }
  const plan = one((await planCart(lead, cart, OPEN)).rows, `${label} plan`);
  await expectBinding(label, plan, OPEN, lead);
  return `${cart.correlation.outcome}:${await dispatch(lead, plan, 'cart_abandonment')}`;
};
results.cart = {
  mx_form_52_hotmart_521: await cartCase('mx-carrito', 'MX', { form: 'plain', hotmart: 'whatsapp' }),
  mx_exact_regression: await cartCase('mx-carrito-exacto', 'MX', { form: 'plain', hotmart: 'plain' }),
  mx_form_521_hotmart_52_derived: await cartCase('mx-carrito-inverso', 'MX', { form: 'whatsapp', hotmart: 'plain' }),
  ar_form_54_hotmart_549: await cartCase('ar-carrito', 'AR', { form: 'plain', hotmart: 'whatsapp' }),
  ar_form_549_hotmart_54_derived: await cartCase('ar-carrito-inverso', 'AR', { form: 'whatsapp', hotmart: 'plain' }),
};

// ---------------------------------------------------------------------------
// 3. Pago fallido: plan, permiso de la intencion y phone_match en la evidencia.
// ---------------------------------------------------------------------------
const failureCase = async (label, country, { form, hotmart, phoneMatch }) => {
  const lead = person(label, country);
  await submitForm(lead, { phone: lead[form] });
  const failure = await admitFailure(lead, { phone: lead[hotmart] });
  const intent = await intentOf(lead.intent, `${label} intent after the failure`);
  if (failure.correlationOutcome !== 'resolved' || failure.intentId !== lead.intent
      || intent.current_classification !== 'payment_failure_supported') {
    throw new Error(`${label}: the payment failure did not correlate with the form intent: ${JSON.stringify({ outcome: failure.correlationOutcome, classification: intent.current_classification })}`);
  }
  await createContact(lead, failure.eventId, { phone: lead[hotmart] });
  const plan = one((await planFailure(lead, failure, OPEN)).rows, `${label} plan`);
  await expectBinding(label, plan, OPEN, lead);
  const grants = await activeAllowed(lead);
  const evidence = grants[0]?.evidence;
  if (grants.length !== 1
      || evidence.reason !== 'precheckout_whatsapp_consent'
      || evidence.purchase_intent_id !== lead.intent
      || evidence.precheckout_submission_id !== lead.submission
      || evidence.webhook_event_id !== failure.eventId
      || evidence.recovery_case_id !== plan.recovery_case_id
      || evidence.consent_copy_version !== ATT1.copyVersion
      || evidence.phone_match !== phoneMatch
      || Object.keys(evidence).length !== 7) {
    throw new Error(`${label}: unexpected consent grant: ${JSON.stringify({ grants: grants.length, keys: evidence && Object.keys(evidence), phone_match: evidence?.phone_match })}`);
  }
  return `${evidence.phone_match}:${await dispatch(lead, plan, 'payment_failure')}`;
};
results.payment_failure = {
  mx_form_52_hotmart_521: await failureCase('mx-pago', 'MX',
    { form: 'plain', hotmart: 'whatsapp', phoneMatch: 'whatsapp_equivalent' }),
  mx_exact_regression: await failureCase('mx-pago-exacto', 'MX',
    { form: 'plain', hotmart: 'plain', phoneMatch: 'exact' }),
  ar_form_54_hotmart_549: await failureCase('ar-pago', 'AR',
    { form: 'plain', hotmart: 'whatsapp', phoneMatch: 'whatsapp_equivalent' }),
};

// ---------------------------------------------------------------------------
// 4. Compra aprobada con el telefono en la otra forma: la intencion queda
//    purchased. Antes daba conflict y la intencion de quien ya compro seguia
//    viva.
// ---------------------------------------------------------------------------
const purchaseCase = async (label, country, { form, hotmart }) => {
  const lead = person(label, country);
  await submitForm(lead, { phone: lead[form] });
  const purchase = await admitPurchase(lead, { phone: lead[hotmart] });
  const intent = await intentOf(lead.intent, `${label} intent after the purchase`);
  if (purchase.outcome !== 'inserted'
      || purchase.correlation.outcome !== 'resolved'
      || purchase.correlation.purchase_intent_id !== lead.intent
      || purchase.correlation.matched_by !== 'email_and_phone'
      || intent.lifecycle_state !== 'purchased'
      || intent.activation_authorized !== false) {
    throw new Error(`${label}: the purchase did not close the intent: ${JSON.stringify({ outcome: purchase.outcome, correlation: purchase.correlation.outcome, reason: purchase.correlation.reason_code, state: intent.lifecycle_state })}`);
  }
  return `${purchase.correlation.outcome}:${intent.lifecycle_state}`;
};
results.purchase = {
  mx_form_52_hotmart_521: await purchaseCase('mx-compra', 'MX', { form: 'plain', hotmart: 'whatsapp' }),
  ar_form_54_hotmart_549: await purchaseCase('ar-compra', 'AR', { form: 'plain', hotmart: 'whatsapp' }),
  ar_form_549_hotmart_54_derived: await purchaseCase('ar-compra-inversa', 'AR', { form: 'whatsapp', hotmart: 'plain' }),
};

// ---------------------------------------------------------------------------
// 5. F1: el envio sale a contacts.phone, asi que tiene que ser canonicamente
//    el telefono de la intencion.
// ---------------------------------------------------------------------------
{
  // Con '+', espacios y la otra forma: es el mismo movil.
  const formatted = person('f1-con-formato', 'MX');
  await submitForm(formatted);
  await createContact(formatted, null, {
    phone: formatted.whatsapp,
    contactPhone: `+52 1 ${formatted.plain.slice(2, 4)} ${formatted.plain.slice(4, 8)} ${formatted.plain.slice(8)}`,
    source: 'system',
  });
  const formattedReason = await consentReason(formatted);

  // El contacto se encontro por email y tiene otro numero en contacts.phone,
  // con el contact_point del telefono consentido: no sale, con motivo propio.
  const otherNumber = person('f1-otro-numero', 'MX');
  await submitForm(otherNumber);
  const otherNumberCart = await admitCart(otherNumber, { phone: otherNumber.whatsapp });
  await createContact(otherNumber, otherNumberCart.eventId, {
    phone: otherNumber.whatsapp, contactPhone: otherNumber.other,
  });
  const otherNumberReason = await consentReason(otherNumber);
  const otherNumberPlan = await expectPlanRejection('F1 with another number in contacts.phone',
    otherNumber, () => planCart(otherNumber, otherNumberCart, OPEN),
    'pilot_audience_consented_intent_contact_phone_mismatch');

  const withoutPhone = person('f1-sin-telefono', 'AR');
  await submitForm(withoutPhone);
  await createContact(withoutPhone, null, {
    phone: withoutPhone.whatsapp, contactPhone: null, source: 'system',
  });
  const withoutPhoneReason = await consentReason(withoutPhone);

  if (formattedReason.reason_code !== 'consented_intent_ok'
      || otherNumberReason.reason_code !== 'consented_intent_contact_phone_mismatch'
      || otherNumberReason.precheckout_submission_id !== null
      || withoutPhoneReason.reason_code !== 'consented_intent_contact_phone_mismatch') {
    throw new Error(`F1: ${JSON.stringify({ formatted: formattedReason.reason_code, otherNumber: otherNumberReason.reason_code, withoutPhone: withoutPhoneReason.reason_code })}`);
  }
  results.contact_phone = {
    formatted_other_form: formattedReason.reason_code,
    another_number: otherNumberReason.reason_code,
    another_number_plan: otherNumberPlan,
    null_phone: withoutPhoneReason.reason_code,
  };
}

// ---------------------------------------------------------------------------
// 6. Opt-out previo en la otra forma. La persona escribio "No mas mensajes"
//    desde su wa_id (521...) antes de tener contacto: el opt-out queda
//    unmatched con ese id. La intencion, el evento de Hotmart y la identidad
//    quedan en 52..., asi que el freno del arranque (que busca el id exacto de
//    la identidad) no lo cruza. Lo frena el helper del consentimiento.
// ---------------------------------------------------------------------------
{
  // a. Al planificar, en un modo con consentimiento.
  const prior = person('opt-out-previo', 'MX');
  const priorOptOut = await optOutFrom(prior.whatsapp);
  await submitForm(prior);
  const priorCart = await admitCart(prior, { phone: prior.plain });
  await createContact(prior, priorCart.eventId, { phone: prior.plain });
  const priorReason = await consentReason(prior);
  const priorPlan = await expectPlanRejection('a prior opt-out in the other form', prior,
    () => planCart(prior, priorCart, OPEN), 'pilot_audience_consented_intent_prior_opt_out');
  if (priorOptOut.outcome !== 'recorded_unmatched'
      || priorCart.correlation?.outcome !== 'resolved'
      || priorReason.reason_code !== 'consented_intent_prior_opt_out'
      || priorReason.precheckout_submission_id !== null) {
    throw new Error(`prior opt-out: ${JSON.stringify({ optOut: priorOptOut.outcome, correlation: priorCart.correlation?.outcome, reason: priorReason.reason_code })}`);
  }

  // b. El opt-out de otra cuenta de Chatwoot no frena.
  const otherAccount = person('opt-out-otra-cuenta', 'AR');
  const otherAccountOptOut = await optOutFrom(otherAccount.whatsapp, { accountId: 999, inboxId: 1 });
  await submitForm(otherAccount);
  await createContact(otherAccount, null, { phone: otherAccount.whatsapp, source: 'system' });
  const otherAccountReason = await consentReason(otherAccount);
  if (otherAccountOptOut.outcome !== 'recorded_unmatched'
      || otherAccountReason.reason_code !== 'consented_intent_ok') {
    throw new Error(`opt-out of another account: ${JSON.stringify({ optOut: otherAccountOptOut.outcome, reason: otherAccountReason.reason_code })}`);
  }

  // c. En manual_cohort la audiencia es la cohorte y el pago fallido se
  //    planifica, pero la intencion no concede el permiso: la reevaluacion
  //    real no lo ejecuta.
  const manual = person('opt-out-manual', 'MX');
  const manualOptOut = await optOutFrom(manual.whatsapp);
  await submitForm(manual);
  const manualFailure = await admitFailure(manual, { phone: manual.plain });
  await createContact(manual, manualFailure.eventId, { phone: manual.plain });
  await enroll(manual, MANUAL);
  const manualPlan = one((await planFailure(manual, manualFailure, MANUAL)).rows,
    'manual plan after a prior opt-out');
  await expectBinding('manual after a prior opt-out', manualPlan, MANUAL, manual);
  const manualGrants = await activeAllowed(manual);
  const manualClaim = await claim(manual, manualPlan, 'payment_failure');
  if (manualOptOut.outcome !== 'recorded_unmatched'
      || manualFailure.correlationOutcome !== 'resolved'
      || manualGrants.length !== 0
      || manualClaim.decision.decision === 'execute'
      || manualClaim.decision.reason_code !== 'contact_authorization_unknown') {
    throw new Error(`manual_cohort after a prior opt-out: ${JSON.stringify({ optOut: manualOptOut.outcome, grants: manualGrants.length, decision: manualClaim.decision })}`);
  }

  // d. Con el envio en vuelo: el opt-out llega desde el wa_id entre la reserva
  //    y el arranque. No cierra el intento (no hay identidad con ese id), pero
  //    el arranque re-verifica la intencion y no sale ni consume cupo.
  const inFlight = person('opt-out-en-vuelo', 'AR');
  await submitForm(inFlight);
  const inFlightCart = await admitCart(inFlight, { phone: inFlight.plain });
  await createContact(inFlight, inFlightCart.eventId, { phone: inFlight.plain });
  const inFlightPlan = one((await planCart(inFlight, inFlightCart, OPEN)).rows,
    'opt-out in flight plan');
  const startsBefore = await startsOf(OPEN);
  const reservation = await reserve(inFlight, inFlightPlan, 'cart_abandonment');
  const inFlightOptOut = await optOutFrom(inFlight.whatsapp);
  let startError = null;
  try {
    await db.query(`select * from public.${reservation.operation}($1,$2,$3,$4,$5)`,
      [inFlightPlan.scheduled_action_id, reservation.attempt.id, reservation.worker,
        reservation.lease, await dbNow()]);
  } catch (caught) {
    startError = caught;
  }
  if (inFlightOptOut.outcome !== 'recorded_unmatched'
      || startError?.code !== '55000'
      || startError?.message !== 'pilot_request_start_rejected'
      || startError?.detail !== 'pilot_audience_consented_intent_prior_opt_out'
      || (await startsOf(OPEN)) !== startsBefore) {
    throw new Error(`opt-out in flight: ${JSON.stringify({ optOut: inFlightOptOut.outcome, code: startError?.code, message: startError?.message, detail: startError?.detail })}`);
  }

  // e. Carrito en manual_cohort. La audiencia es la cohorte: el chequeo del
  //    consentimiento no corre, y el carrito concede su propio permiso. El
  //    freno compartido del arranque busca el id exacto de la identidad (52...
  //    o 54...), y el envio saldria al wa_id que pidio la baja. Lo frena el
  //    arranque del piloto, que mira las dos formas con el lock tomado: la
  //    reevaluacion real ejecuta, el intento queda reservado y no consume
  //    cupo.
  const attemptPhase = async (attemptId) => one((await db.query(`
    select phase from public.followup_delivery_attempts where id = $1
  `, [attemptId])).rows, 'attempt phase').phase;
  const manualCartStop = async (label, country) => {
    const lead = person(label, country);
    const optOut = await optOutFrom(lead.whatsapp);
    const cart = await admitCart(lead, { phone: lead.plain });
    await createContact(lead, cart.eventId, { phone: lead.plain });
    await enroll(lead, MANUAL);
    const plan = one((await planCart(lead, cart, MANUAL)).rows, `${label} plan`);
    await expectBinding(label, plan, MANUAL, lead);
    const grants = (await activeAllowed(lead)).length;
    const startsBefore = await startsOf(MANUAL);
    const reservation = await reserve(lead, plan, 'cart_abandonment');
    if (optOut.outcome !== 'recorded_unmatched' || grants !== 1
        || reservation.operation !== 'mark_lancemos_pilot_request_started') {
      throw new Error(`${label}: unexpected setup ${JSON.stringify({ optOut: optOut.outcome, grants, operation: reservation.operation })}`);
    }
    await expectError(`${label} start`, async () => db.query(
      `select * from public.${reservation.operation}($1,$2,$3,$4,$5)`,
      [plan.scheduled_action_id, reservation.attempt.id, reservation.worker,
        reservation.lease, await dbNow()],
    ), { code: '55000', message: 'pilot_request_start_rejected', detail: 'pilot_chatwoot_opt_out_stop' });
    const phase = await attemptPhase(reservation.attempt.id);
    if ((await startsOf(MANUAL)) !== startsBefore || phase !== 'reserved') {
      throw new Error(`${label}: the rejected start consumed budget or moved the attempt: ${phase}`);
    }
    return `execute, ${reservation.operation}: pilot_request_start_rejected:pilot_chatwoot_opt_out_stop, starts +0, attempt ${phase}`;
  };
  const manualCartMx = await manualCartStop('opt-out-carrito-manual-mx', 'MX');
  const manualCartAr = await manualCartStop('opt-out-carrito-manual-ar', 'AR');

  // h. El envio sale a contacts.phone (get_followup_execution_context), no a
  //    la identidad. Un contacto encontrado por email con otro numero en
  //    contacts.phone: la baja de ese otro numero, en su forma wa_id, tambien
  //    frena el arranque, aunque la identidad del caso sea otra.
  const otherPhone = person('opt-out-contacts-phone', 'MX');
  const otherPhoneWaId = `521${otherPhone.other.slice(2)}`;
  const otherPhoneOptOut = await optOutFrom(otherPhoneWaId);
  const otherPhoneCart = await admitCart(otherPhone, { phone: otherPhone.plain });
  await createContact(otherPhone, otherPhoneCart.eventId, {
    phone: otherPhone.plain, contactPhone: otherPhone.other,
  });
  await enroll(otherPhone, MANUAL);
  const otherPhonePlan = one((await planCart(otherPhone, otherPhoneCart, MANUAL)).rows,
    'contacts.phone opt-out plan');
  const otherPhoneStartsBefore = await startsOf(MANUAL);
  const otherPhoneReservation = await reserve(otherPhone, otherPhonePlan, 'cart_abandonment');
  await expectError('contacts.phone opt-out start', async () => db.query(
    `select * from public.${otherPhoneReservation.operation}($1,$2,$3,$4,$5)`,
    [otherPhonePlan.scheduled_action_id, otherPhoneReservation.attempt.id,
      otherPhoneReservation.worker, otherPhoneReservation.lease, await dbNow()],
  ), { code: '55000', message: 'pilot_request_start_rejected', detail: 'pilot_chatwoot_opt_out_stop' });
  if (otherPhoneOptOut.outcome !== 'recorded_unmatched'
      || (await startsOf(MANUAL)) !== otherPhoneStartsBefore
      || (await attemptPhase(otherPhoneReservation.attempt.id)) !== 'reserved') {
    throw new Error(`contacts.phone opt-out: ${JSON.stringify({ optOut: otherPhoneOptOut.outcome })}`);
  }

  // f. El camino compuesto: el carrito en manual_cohort concede el permiso
  //    cart_recovery y el pago fallido de la misma persona lo usa, asi que su
  //    reevaluacion ejecuta. Se reclaman las dos acciones juntas y ninguno de
  //    los dos arranques sale ni consume cupo.
  const composite = person('opt-out-compuesto', 'MX');
  const compositeOptOut = await optOutFrom(composite.whatsapp);
  await submitForm(composite);
  const compositeCart = await admitCart(composite, { phone: composite.plain });
  await createContact(composite, compositeCart.eventId, { phone: composite.plain });
  await enroll(composite, MANUAL);
  const compositeCartPlan = one((await planCart(composite, compositeCart, MANUAL)).rows,
    'composite cart plan');
  const compositeGrants = (await activeAllowed(composite)).length;
  const compositeFailure = await admitFailure(composite, { phone: composite.plain });
  const compositeFailurePlan = one((await planFailure(composite, compositeFailure, MANUAL)).rows,
    'composite payment failure plan');
  const compositeWorker = 'att1-phones-opt-out-compuesto';
  const compositeNow = await dbNow();
  const compositeClaimed = (await db.query(`
    select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
  `, [compositeWorker, compositeNow])).rows;
  const compositeActions = [
    [compositeFailurePlan, 'payment_failure'],
    [compositeCartPlan, 'cart_abandonment'],
  ];
  if (compositeOptOut.outcome !== 'recorded_unmatched'
      || compositeFailure.correlationOutcome !== 'resolved'
      || compositeGrants !== 1
      || !sameSet(compositeClaimed.map((row) => `${row.id}:${row.anchor_type}`),
        compositeActions.map(([plan, anchor]) => `${plan.scheduled_action_id}:${anchor}`))) {
    throw new Error(`composite setup: ${JSON.stringify({ optOut: compositeOptOut.outcome, correlation: compositeFailure.correlationOutcome, grants: compositeGrants, claimed: compositeClaimed.map((row) => row.anchor_type) })}`);
  }
  const compositeStartsBefore = await startsOf(MANUAL);
  const compositeResults = {};
  for (const [plan, anchor] of compositeActions) {
    const { lease_generation: lease } = compositeClaimed.find(
      (row) => row.id === plan.scheduled_action_id,
    );
    const decision = one((await db.query(`
      select * from public.reevaluate_followup_action($1,$2,$3,$4)
    `, [plan.scheduled_action_id, compositeWorker, lease, compositeNow])).rows,
    `composite ${anchor} reevaluation`);
    if (decision.decision !== 'execute' || decision.reason_code !== 'eligible_for_execution') {
      throw new Error(`composite ${anchor}: the real reevaluation did not execute: ${JSON.stringify(decision)}`);
    }
    const attempt = one((await db.query(`
      select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
    `, [plan.scheduled_action_id, compositeWorker, lease, decision.case_version,
      decision.sequence_revision, compositeNow])).rows, `composite ${anchor} reservation`);
    const operation = startOperationFor(anchor);
    await expectError(`composite ${anchor} start`, async () => db.query(
      `select * from public.${operation}($1,$2,$3,$4,$5)`,
      [plan.scheduled_action_id, attempt.id, compositeWorker, lease, await dbNow()],
    ), { code: '55000', message: 'pilot_request_start_rejected', detail: 'pilot_chatwoot_opt_out_stop' });
    compositeResults[anchor] = `${decision.decision}, ${operation}: pilot_chatwoot_opt_out_stop, attempt ${await attemptPhase(attempt.id)}`;
  }
  if ((await startsOf(MANUAL)) !== compositeStartsBefore
      || Object.values(compositeResults).some((value) => !value.endsWith('attempt reserved'))) {
    throw new Error(`composite: a rejected start consumed budget or moved the attempt: ${JSON.stringify(compositeResults)}`);
  }

  // g. Controles en manual_cohort: el mismo carrito sin opt-out, y con un
  //    opt-out de otra cuenta de Chatwoot en la otra forma, salen.
  const manualCartControl = async (label, country, { foreignOptOut }) => {
    const lead = person(label, country);
    const optOut = foreignOptOut
      ? await optOutFrom(lead.whatsapp, { accountId: 999, inboxId: 1 })
      : null;
    const cart = await admitCart(lead, { phone: lead.plain });
    await createContact(lead, cart.eventId, { phone: lead.plain });
    await enroll(lead, MANUAL);
    const plan = one((await planCart(lead, cart, MANUAL)).rows, `${label} plan`);
    const startsBefore = await startsOf(MANUAL);
    const outcome = await dispatch(lead, plan, 'cart_abandonment');
    if ((optOut !== null && optOut.outcome !== 'recorded_unmatched')
        || (await startsOf(MANUAL)) !== startsBefore + 1) {
      throw new Error(`${label}: the control did not start once: ${JSON.stringify({ optOut: optOut?.outcome })}`);
    }
    return outcome;
  };
  const manualWithout = await manualCartControl('carrito-manual-sin-opt-out', 'MX',
    { foreignOptOut: false });
  const manualForeign = await manualCartControl('carrito-manual-opt-out-otra-cuenta', 'AR',
    { foreignOptOut: true });

  results.prior_opt_out = {
    other_form_unmatched: priorPlan,
    other_account: otherAccountReason.reason_code,
    manual_cohort: `planned, grants ${manualGrants.length}, ${manualClaim.decision.decision}:${manualClaim.decision.reason_code}`,
    in_flight: `${startError.message}:${startError.detail}`,
    manual_cohort_cart_mx: manualCartMx,
    manual_cohort_cart_ar: manualCartAr,
    manual_cohort_contacts_phone: 'pilot_request_start_rejected:pilot_chatwoot_opt_out_stop',
    manual_cohort_cart_then_payment_failure: compositeResults,
    manual_cohort_cart_without_opt_out: manualWithout,
    manual_cohort_cart_other_account: manualForeign,
  };
}

// ---------------------------------------------------------------------------
// 7. Negativos: la equivalencia no junta otros numeros.
// ---------------------------------------------------------------------------
{
  // El mismo email con otros diez digitos: sigue siendo un conflicto.
  const conflict = person('otros-diez-digitos', 'MX');
  await submitForm(conflict);
  const conflictCart = await admitCart(conflict, { phone: conflict.other });
  const conflictIntent = await intentOf(conflict.intent, 'conflict intent');
  if (conflictCart.correlation?.outcome !== 'conflict'
      || conflictCart.correlation.reason_code !== 'email_phone_conflict'
      || conflictIntent.current_classification !== 'identity_conflict') {
    throw new Error(`another number correlated: ${JSON.stringify({ correlation: conflictCart.correlation?.outcome, classification: conflictIntent.current_classification })}`);
  }

  // El helper, con el contacto correcto y otro destino.
  const mismatch = person('otro-destino', 'AR');
  await submitForm(mismatch);
  await createContact(mismatch, null, { phone: mismatch.whatsapp, source: 'system' });
  const control = await consentReason(mismatch);
  const otherDestination = await consentReason(mismatch, mismatch.other);
  const brazilian = await consentReason(mismatch, `55${mismatch.plain.slice(2)}`);
  const noDigits = await consentReason(mismatch, 'sin telefono');
  if (control.reason_code !== 'consented_intent_ok'
      || otherDestination.reason_code !== 'consented_intent_phone_mismatch'
      || brazilian.reason_code !== 'consented_intent_phone_mismatch'
      || noDigits.reason_code !== 'consented_intent_phone_mismatch') {
    throw new Error(`destination mismatch: ${JSON.stringify({ control: control.reason_code, otherDestination: otherDestination.reason_code, brazilian: brazilian.reason_code, noDigits: noDigits.reason_code })}`);
  }
  results.negatives = {
    same_email_other_number: `${conflictCart.correlation.outcome}:${conflictCart.correlation.reason_code}`,
    other_destination: otherDestination.reason_code,
    other_country_code: brazilian.reason_code,
    destination_without_digits: noDigits.reason_code,
  };
}

// ---------------------------------------------------------------------------
// 8. Johanna: el correlador compartido sigue comparando exacto, y el portable
//    es el compartido con sus tres reemplazos y nada mas.
// ---------------------------------------------------------------------------
{
  const shared = person('correlador-compartido', 'MX');
  await submitForm(shared);
  const payload = cartPayload(shared, shared.whatsapp);
  const admitted = one((await db.query(`
    select * from public.admit_and_correlate_hotmart_cart_abandonment($1,$2::jsonb,$3,$4)
  `, [payload.id, JSON.stringify(payload), shared.email, shared.whatsapp])).rows,
  'shared cart admission');
  const correlation = await correlationOf(admitted.webhook_event_id);
  const intent = await intentOf(shared.intent, 'shared intent');
  if (admitted.outcome !== 'inserted'
      || correlation?.outcome !== 'conflict'
      || correlation.reason_code !== 'email_phone_conflict'
      || intent.current_classification !== 'identity_conflict') {
    throw new Error(`the shared correlator changed: ${JSON.stringify({ outcome: admitted.outcome, correlation: correlation?.outcome, classification: intent.current_classification })}`);
  }

  const drift = one((await db.query(`
    with definitions as (
      select
        pg_get_functiondef('public.correlate_hotmart_purchase_intent(uuid)'::regprocedure) as shared,
        pg_get_functiondef('public._correlate_portable_hotmart_purchase_intent(uuid)'::regprocedure) as portable
    )
    select
      replace(
        replace(shared, 'public.correlate_hotmart_purchase_intent(',
          'public._correlate_portable_hotmart_purchase_intent('),
        'intent.normalized_phone = v_phone',
        'intent.normalized_phone = any(public._whatsapp_phone_variants(v_phone))'
      ) = portable as derived,
      (length(shared) - length(replace(shared, 'intent.normalized_phone = v_phone', '')))
        / length('intent.normalized_phone = v_phone') as exact_comparisons,
      position('_whatsapp_phone_' in shared) as shared_mentions_equivalence
    from definitions
  `)).rows, 'correlator drift');
  if (drift.derived !== true || drift.exact_comparisons !== 2
      || drift.shared_mentions_equivalence !== 0) {
    throw new Error(`the portable correlator drifted from the shared one: ${JSON.stringify(drift)}`);
  }
  results.shared_correlator = {
    exact_52_against_521: `${correlation.outcome}:${correlation.reason_code}`,
    portable_is_shared_plus_replacements: drift.derived,
  };
}

// ---------------------------------------------------------------------------
// 9. El inventario de esquema reconoce esta migracion y las migraciones cuyas
//    funciones se copiaron (sus fingerprints buscan literales dentro de esas
//    funciones, y uno es negativo).
// ---------------------------------------------------------------------------
const inventory = (await db.query(readFileSync(
  join(root, 'scripts/supabase_schema_inventory.sql'), 'utf8',
))).rows;
const FINGERPRINTS = [
  '20260901000300', '20260903000100', '20260903000300', '20260928000200',
  '20260929000100', '20260930000100', '20260930000300', '20261001000100',
];
for (const version of FINGERPRINTS) {
  const row = inventory.find((candidate) => candidate.version === version);
  if (row?.fingerprint_status !== 'fingerprint_present') {
    throw new Error(`schema fingerprint ${version}: ${JSON.stringify(row)}`);
  }
}
results.schema_fingerprints = FINGERPRINTS.length;

// ---------------------------------------------------------------------------
// 10. El enlace de pago del entrante. La persona dejo el formulario (52... o
//     54...) y escribe por WhatsApp desde su wa_id (521... o 549...). El bridge
//     admite el caso entrante con el external_user_id resuelto
//     (resolve_inbound_external_user_id: sin identidad guardada, el wa_id
//     textual) y, con manifiesto, reserva el enlace con la reserva portable.
//     La compartida busca la intencion con ese mismo id exacto: daba la
//     oferta por defecto sin el sck del formulario, fabricaba una segunda
//     intencion viva y no veia la compra de quien ya compro.
// ---------------------------------------------------------------------------
{
  // El scope entrante y el catalogo de las tres ofertas, como
  // despliegue/base/aprovisionar-att1.sql de la instancia: el hotlink como
  // external_product_id y la primera oferta por defecto. Con el product_id
  // numerico la reserva da missing_default_offer para cualquier telefono.
  await db.query(`
    insert into public.inbound_commercial_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
       external_product_id, offer_code, approved_by, approved_at, published_at)
    values ($1,$2,'published',$3,$4,$5,$6,$7,'operator-test',now(),now())
  `, [ATT1.inboundScope, ATT1.inboundVersion, ATT1.tenant, ATT1.accountId, ATT1.inboxId,
    ATT1.hotlink, defaultOffer.offer_code]);
  for (const offer of offers) {
    await db.query(`
      insert into public.checkout_offer_catalog
        (tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref,
         offer_code, checkout_base_url, checkout_mode, default_for_inbound, status,
         version, approved_by, approved_at)
      values ($1,$2,$3,$4,$5,$6,$7,$8,10,$9,'active',1,'operator-test',now())
    `, [ATT1.tenant, ATT1.funnel, ATT1.inboundScope, ATT1.inboundVersion, ATT1.hotlink,
      offer.landing_id, offer.offer_code, `https://pay.hotmart.com/${ATT1.hotlink}`,
      offer.default]);
  }

  let inboundConversation = 930000;
  let inboundMessage = 940000;
  const admitInbound = async (userId, conversation) => one((await db.query(`
    select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)
  `, [ATT1.inboundScope, ATT1.inboundVersion, conversation, userId])).rows,
  `inbound admission ${conversation}`);
  // Una reserva por mensaje del lead, con la RPC que elige el bridge.
  const reserveLink = async (rpc, caseId, userId, conversation) => {
    inboundMessage += 1;
    const ulid = ulidAt(Date.now());
    const row = one((await db.query(`
      select * from public.${rpc}($1,$2,$3,$4,$5,$6,$7,clock_timestamp())
    `, [caseId, userId, ATT1.accountId, ATT1.inboxId, conversation,
      String(inboundMessage), ulid])).rows, `${rpc} ${conversation}`);
    const issuance = row.issuance_id === null ? null : one((await db.query(`
      select issuance.source_kind, issuance.offer_resolution,
             issuance.attribution_resolution, issuance.purchase_intent_id,
             issuance.checkout_url_final, offer.offer_code
      from public.checkout_link_issuances issuance
      join public.checkout_offer_catalog offer on offer.id = issuance.offer_catalog_id
      where issuance.id = $1
    `, [row.issuance_id])).rows, `${rpc} issuance`);
    return { outcome: row.outcome, ulid, issuance };
  };
  const PORTABLE = 'reserve_portable_checkout_issuance_v2';
  const SHARED = 'reserve_chatwoot_checkout_issuance_v2';
  const liveIntents = async (lead) => one((await db.query(`
    select count(*)::integer as count from public.purchase_intents
    where normalized_phone = any($1::text[]) and lifecycle_state = 'waiting_for_purchase'
  `, [[lead.plain, lead.whatsapp]])).rows, `${lead.label} live intents`).count;
  const formSck = GOLDEN.MX.raw.data.attribution.sck;
  const formFbclid = GOLDEN.MX.raw.data.attribution.fbclid;
  // El enlace del formulario: su oferta, su intencion, su sck y su fbclid.
  const expectFormLink = (label, lead, link, { attribution }) => {
    const url = link.issuance?.checkout_url_final ?? '';
    // El marcador se separa con ~ desde 20261005000100; la ~ viaja literal.
    const sckInUrl = attribution === 'full'
      ? `&sck=${formSck}~hermes~v1~${link.ulid}`
      : `&sck=hermes~v1~${link.ulid}`;
    if (link.outcome !== 'reserved'
        || link.issuance.source_kind !== 'precheckout_request'
        || link.issuance.offer_resolution !== 'lead_intent'
        || link.issuance.offer_code !== lead.offer.offer_code
        || link.issuance.purchase_intent_id !== lead.intent
        || link.issuance.attribution_resolution !== attribution
        || !url.includes(`?off=${lead.offer.offer_code}&`)
        || !url.includes(sckInUrl)
        || (attribution === 'full') !== url.endsWith(`&fbclid=${formFbclid}`)) {
      throw new Error(`${label}: the link is not the one of the form: ${JSON.stringify({ outcome: link.outcome, ...link.issuance, form_offer: lead.offer.offer_code })}`);
    }
    return `${link.outcome}:${link.issuance.source_kind}:${link.issuance.offer_resolution}:${link.issuance.offer_code}:${link.issuance.attribution_resolution}`;
  };

  // a. Mexico: formulario 52 por la landing -d (2uafw5bg) y entrante desde
  //    521 sin identidad previa. Una sola intencion viva antes y despues.
  const mx = person('enlace-mx', 'MX');
  await submitForm(mx);
  inboundConversation += 1;
  const mxCase = await admitInbound(mx.whatsapp, inboundConversation);
  const mxLink = await reserveLink(PORTABLE, mxCase.commercial_case_id, mx.whatsapp,
    inboundConversation);
  const mxSummary = expectFormLink('MX 52 form, 521 inbound', mx, mxLink, { attribution: 'full' });
  if (mxCase.outcome !== 'created' || (await liveIntents(mx)) !== 1) {
    throw new Error(`MX link: ${JSON.stringify({ admission: mxCase.outcome, live: await liveIntents(mx) })}`);
  }

  // b. Argentina: formulario 54 por ads-a (la oferta por defecto, sin sck) y
  //    entrante desde 549. Despues compra con el telefono de Hotmart (549): no
  //    queda ninguna intencion viva y el proximo mensaje no recibe otro enlace.
  const ar = person('enlace-ar', 'AR');
  await submitForm(ar);
  inboundConversation += 1;
  const arConversation = inboundConversation;
  const arCase = await admitInbound(ar.whatsapp, arConversation);
  const arLink = await reserveLink(PORTABLE, arCase.commercial_case_id, ar.whatsapp,
    arConversation);
  const arSummary = expectFormLink('AR 54 form, 549 inbound', ar, arLink,
    { attribution: 'marker_only' });
  const arLiveAfterLink = await liveIntents(ar);
  const arPurchase = await admitPurchase(ar, { phone: ar.whatsapp });
  const arLiveAfterPurchase = await liveIntents(ar);
  const arAgain = await reserveLink(PORTABLE, arCase.commercial_case_id, ar.whatsapp,
    arConversation);
  if (arLiveAfterLink !== 1 || arPurchase.correlation.outcome !== 'resolved'
      || arLiveAfterPurchase !== 0 || arAgain.outcome !== 'purchase_already_approved') {
    throw new Error(`AR link then purchase: ${JSON.stringify({ arLiveAfterLink, purchase: arPurchase.correlation.outcome, arLiveAfterPurchase, again: arAgain.outcome })}`);
  }

  // c. Mexico: escribio primero desde 521 (la admision crea la identidad
  //    521), despues dejo el formulario 52 y vuelve a escribir en la misma
  //    conversacion.
  const first = person('enlace-escribio-primero', 'MX');
  inboundConversation += 1;
  const firstCase = await admitInbound(first.whatsapp, inboundConversation);
  await submitForm(first);
  const firstAgain = await admitInbound(first.whatsapp, inboundConversation);
  const firstLink = await reserveLink(PORTABLE, firstAgain.commercial_case_id, first.whatsapp,
    inboundConversation);
  const firstSummary = expectFormLink('MX wrote first, then the form', first, firstLink,
    { attribution: 'full' });
  if (firstCase.outcome !== 'created' || firstAgain.commercial_case_id !== firstCase.commercial_case_id
      || (await liveIntents(first)) !== 1) {
    throw new Error(`MX wrote first: ${JSON.stringify({ first: firstCase.outcome, again: firstAgain.outcome })}`);
  }

  // d. Mexico: formulario 52 y compra aprobada (Hotmart 521); despues escribe
  //    desde 521. No recibe un enlace de pago.
  const bought = person('enlace-ya-compro', 'MX');
  await submitForm(bought);
  const boughtPurchase = await admitPurchase(bought, { phone: bought.whatsapp });
  inboundConversation += 1;
  const boughtCase = await admitInbound(bought.whatsapp, inboundConversation);
  const boughtLink = await reserveLink(PORTABLE, boughtCase.commercial_case_id,
    bought.whatsapp, inboundConversation);
  if (boughtPurchase.correlation.outcome !== 'resolved'
      || boughtLink.outcome !== 'purchase_already_approved' || boughtLink.issuance !== null
      || (await liveIntents(bought)) !== 0) {
    throw new Error(`a buyer received a link: ${JSON.stringify({ purchase: boughtPurchase.correlation.outcome, link: boughtLink.outcome, live: await liveIntents(bought) })}`);
  }

  // e. Opt-out en la otra forma y en otra conversacion: pidio la baja desde
  //    521 (queda unmatched, sin contacto), despues dejo el formulario y la
  //    base lo conoce como 52 en otra conversacion. La compartida solo mira
  //    esta conversacion y el id exacto.
  const stopped = person('enlace-opt-out', 'MX');
  const stoppedOptOut = await optOutFrom(stopped.whatsapp);
  await submitForm(stopped);
  inboundConversation += 1;
  const stoppedCase = await admitInbound(stopped.plain, inboundConversation);
  const stoppedLink = await reserveLink(PORTABLE, stoppedCase.commercial_case_id,
    stopped.plain, inboundConversation);
  if (stoppedOptOut.outcome !== 'recorded_unmatched'
      || stoppedLink.outcome !== 'blocked_opt_out' || stoppedLink.issuance !== null) {
    throw new Error(`an opt-out in the other form did not stop the link: ${JSON.stringify({ optOut: stoppedOptOut.outcome, admission: stoppedCase.outcome, link: stoppedLink.outcome })}`);
  }

  // f. Johanna: la compartida, con el mismo caso que a., sigue exacta: la
  //    oferta por defecto, sin el formulario, y una segunda intencion viva.
  const johanna = person('enlace-compartida', 'MX');
  await submitForm(johanna);
  inboundConversation += 1;
  const johannaCase = await admitInbound(johanna.whatsapp, inboundConversation);
  const johannaLink = await reserveLink(SHARED, johannaCase.commercial_case_id,
    johanna.whatsapp, inboundConversation);
  if (johannaLink.outcome !== 'reserved'
      || johannaLink.issuance.source_kind !== 'inbound_request'
      || johannaLink.issuance.offer_resolution !== 'default_no_intent'
      || johannaLink.issuance.offer_code !== defaultOffer.offer_code
      || johannaLink.issuance.purchase_intent_id === johanna.intent
      || (await liveIntents(johanna)) !== 2) {
    throw new Error(`the shared reserve changed: ${JSON.stringify({ outcome: johannaLink.outcome, ...johannaLink.issuance })}`);
  }

  // g. La portable es la compartida con sus reemplazos y nada mas.
  const reserveDrift = one((await db.query(`
    with definitions as (
      select
        pg_get_functiondef('public.reserve_chatwoot_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamptz)'::regprocedure) as shared,
        pg_get_functiondef('public.reserve_portable_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamptz)'::regprocedure) as portable
    )
    select
      replace(
        replace(
          replace(shared, 'public.reserve_chatwoot_checkout_issuance_v2(',
            'public.reserve_portable_checkout_issuance_v2('),
          'intent.normalized_phone = p_external_user_id',
          'intent.normalized_phone = any(public._whatsapp_phone_variants(p_external_user_id))'
        ),
        $1, $2
      ) = portable as derived,
      (length(shared) - length(replace(shared, 'intent.normalized_phone = p_external_user_id', '')))
        / length('intent.normalized_phone = p_external_user_id') as exact_intent_lookups,
      position('_whatsapp_phone_' in shared) as shared_mentions_equivalence,
      position('identity.external_user_id = p_external_user_id' in portable) > 0
        and position('replay_identity.external_user_id = p_external_user_id' in portable) > 0
        as identity_stays_exact
    from definitions
  `, [
    'public.has_chatwoot_opt_out_stop(\n'
      + '        p_chatwoot_account_id, p_chatwoot_inbox_id,\n'
      + '        p_chatwoot_conversation_id, p_external_user_id\n'
      + '    )',
    'exists (\n'
      + '        select 1\n'
      + '        from unnest(public._whatsapp_phone_variants(p_external_user_id))\n'
      + '            as opt_out_form(user_id)\n'
      + '        where public.has_chatwoot_opt_out_stop(\n'
      + '            p_chatwoot_account_id, p_chatwoot_inbox_id,\n'
      + '            p_chatwoot_conversation_id, opt_out_form.user_id\n'
      + '        )\n'
      + '    )',
  ])).rows, 'reserve drift');
  if (reserveDrift.derived !== true || reserveDrift.exact_intent_lookups !== 3
      || reserveDrift.shared_mentions_equivalence !== 0
      || reserveDrift.identity_stays_exact !== true) {
    throw new Error(`the portable reserve drifted from the shared one: ${JSON.stringify(reserveDrift)}`);
  }

  results.inbound_payment_link = {
    mx_form_52_inbound_521: mxSummary,
    ar_form_54_inbound_549: arSummary,
    ar_after_purchase: `live intents ${arLiveAfterPurchase}, next message ${arAgain.outcome}`,
    mx_wrote_first_then_form: firstSummary,
    mx_bought_then_wrote: boughtLink.outcome,
    opt_out_other_form_other_conversation: stoppedLink.outcome,
    shared_reserve_same_case: `${johannaLink.issuance.source_kind}:${johannaLink.issuance.offer_resolution}:${johannaLink.issuance.offer_code}, live intents 2`,
    portable_is_shared_plus_replacements: reserveDrift.derived,
  };
}

console.log(JSON.stringify({ whatsapp_phone_equivalence: 'OK', ...results }));
await db.close();
