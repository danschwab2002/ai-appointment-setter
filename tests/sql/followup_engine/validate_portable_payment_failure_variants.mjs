// B1, el primer toque del pago rechazado (migracion 20261019000200), con las
// RPC reales sobre la cadena completa de migraciones.
//
// Prueba:
//   0. la migracion: agrega exactamente las cuatro funciones nuevas, cambia
//      solo las cuatro que parcha en el lugar, y cada una es su definicion
//      anterior con los reemplazos documentados y nada mas; las dos entradas
//      son solo de service_role y las dos reglas de nadie;
//   1. payment_failure_refusal_kind sobre los 17 PURCHASE_CANCELED capturados:
//      9 declined, 3 insufficient_funds y 5 not_a_refusal;
//   2. el plan con el scope y la politica propios del pago rechazado (gracia
//      de 15 minutos): un rechazo por saldo, due_at = rechazo + 15 min y
//      payment_failure_count = 1; dos rechazos de la misma persona en un
//      minuto quedan en un caso, una accion, due_at = segundo rechazo + 15 min
//      y count = 2; un Mercado Pago cancelado sin motivo no se planifica
//      (payment_failure_not_a_refusal); un rechazo de otra oferta del mismo
//      producto se suma al caso;
//   3. el primer contacto del formulario se frena con un caso del pago
//      rechazado abierto (superseded_by_provider_event) y despues de que el
//      caso mando su mensaje (la intencion queda payment_failure_supported);
//   4. la siembra del cupon: el rechazo por saldo recibe el ordinal 1 con
//      vencimiento due_at + 24 h; los dos rechazos y el del banco, no;
//   5. el primer toque del banco de punta a punta con la reevaluacion real:
//      claim, contexto de envio (declined, count 1), reserva approved_template,
//      el link de recuperacion emitido para el pago rechazado, el arranque
//      del pago fallido contra su scope y la aceptacion;
//   6. un segundo rechazo despues del mensaje se suma al caso cerrado: no
//      abre otro caso ni crea otra accion; uno mas de 72 h despues, si;
//   7. un cupon vigente de la persona en otro caso que nunca se le mando (el
//      del carrito, sembrado sin A1) no cuenta desde 20261019000500: el
//      contexto de envio no lo da y el saldo sigue pidiendo el suyo. El cupon
//      que si llego (B1-cupon) esta en
//      validate_payment_failure_other_coupon_delivered.mjs.
//
// Datos:
//   - Binding, ofertas, producto, Chatwoot y consentimiento: tests/fixtures/
//     instances/att1/instancia.toml; lookback, politica de compra y ventana:
//     politica-piloto.json, al lado.
//   - Los rechazos: tests/fixtures/hotmart_purchase_canceled_att1_20261019.json
//     (los 17 de la base de ATT1 al 2026-10-19, anonimizados adentro de la base;
//     cada persona lleva el mismo numero en sus eventos). DERIVACION EXPLICITA:
//     creation_date se corre a una distancia fija del reloj de la base (la
//     gracia, el vencimiento y la puerta de arranque, +-5 min del reloj, lo
//     exigen). El caso 6 re-identifica un rechazo capturado (id y transaccion)
//     para que sea un evento nuevo de la misma persona. Motivo, medio, cuotas,
//     pais, oferta y producto quedan tal cual llegaron.
//   - El formulario: el precedente inline de validate_att1_portable_chain.mjs
//     (contrato lead.precheckout 1.1.0 con consentimiento), con el movil
//     mexicano de cada persona.
//   - El catalogo de ofertas del link: el de validate_recovery_checkout_
//     issuance.mjs, con las ofertas del manifiesto.
//   - La politica y el scope del pago rechazado no existen todavia en la
//     instancia (los publica el SQL de publicacion de la instancia): aca van
//     los valores del diseno (att1-pago-rechazado-un-toque, gracia 15 min, un
//     paso payment_failure_first_contact; att1-pago-rechazado con
//     PURCHASE_CANCELED y audiencia consented_intent). DESVIACION DOCUMENTADA,
//     la misma de la cadena de ATT1: la ventana de envio es de 00:00 a 23:59
//     en vez de 09:00-21:00 de Ciudad de Mexico, porque la puerta de arranque
//     exige el reloj real.
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
  grant usage on schema public to service_role;
`);

const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};
const results = {};

// ---------------------------------------------------------------------------
// 0. La migracion.
// ---------------------------------------------------------------------------
const TARGET = '20261019000200_portable_payment_failure_variants_v1.sql';
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
         md5(pg_get_functiondef(p.oid)) as md5,
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

const ENTRYPOINTS = [
  'get_portable_payment_failure_pilot_runtime_status(text,integer,text,text,text)',
  'get_portable_payment_failure_send_context_v1(uuid,text,bigint,timestamp with time zone)',
];
const PRIVATE = [
  'payment_failure_refusal_kind(text,text)',
  'payment_failure_wants_first_coupon(uuid)',
];
const PLAN = 'plan_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamp with time zone)';
const PORTABLE_PLAN = 'plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamp with time zone,bigint,bigint,text,text,integer)';
const COUPONS = 'claim_due_recovery_coupons_v1(timestamp with time zone,text,integer,text,interval,interval,numeric,integer)';
const LINK = 'reserve_recovery_checkout_issuance_v1(uuid,text,bigint,uuid,text,timestamp with time zone)';
const PATCHED = [PLAN, PORTABLE_PLAN, COUPONS, LINK];
const sameSet = (left, right) => JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
const added = [...after.keys()].filter((signature) => !before.has(signature));
const removed = [...before.keys()].filter((signature) => !after.has(signature));
const changed = [...before.keys()].filter((signature) => after.has(signature)
  && (after.get(signature).md5 !== before.get(signature).md5
      || after.get(signature).service_x !== before.get(signature).service_x
      || after.get(signature).api_x !== before.get(signature).api_x));
if (!sameSet(added, [...ENTRYPOINTS, ...PRIVATE]) || removed.length !== 0
    || !sameSet(changed, PATCHED)) {
  throw new Error(`the migration changed something else: ${JSON.stringify({ added, removed, changed })}`);
}
for (const signature of ENTRYPOINTS) {
  const row = after.get(signature);
  if (row.service_x !== true || row.api_x !== false) {
    throw new Error(`${signature} is not service_role only: ${JSON.stringify(row)}`);
  }
}
for (const signature of PRIVATE) {
  const row = after.get(signature);
  if (row.service_x !== false || row.api_x !== false) {
    throw new Error(`${signature} is executable by an API role`);
  }
}
for (const signature of PATCHED) {
  // create or replace conserva el ACL: el parche no lo toca.
  if (after.get(signature).service_x !== before.get(signature).service_x
      || after.get(signature).api_x !== before.get(signature).api_x) {
    throw new Error(`${signature} changed its ACL`);
  }
}

// Cada parche es la definicion anterior con sus reemplazos y nada mas. Los
// fragmentos se leen de la migracion misma, entre sus marcas $oN$ y $nN$.
const count = (text, needle) => text.split(needle).length - 1;
const migrationText = readFileSync(join(root, 'supabase/migrations', TARGET), 'utf8');
const patchBlocks = migrationText.split('select pg_temp.payment_failure_variants_patch(').slice(1);
const patchProof = {};
for (const block of patchBlocks) {
  const signature = block.match(/'public\.([^']+)'/)[1]
    .replace(/timestamptz/g, 'timestamp with time zone');
  const pairs = [];
  for (const match of block.matchAll(/\$o(\d+)\$([\s\S]*?)\$o\1\$/g)) {
    const next = block.match(new RegExp(`\\$n${match[1]}\\$([\\s\\S]*?)\\$n${match[1]}\\$`));
    pairs.push([match[2], next[1]]);
  }
  let out = before.get(signature).definition;
  for (const [old, next] of pairs) {
    if (count(out, old) !== 1) throw new Error(`${signature}: a fragment does not appear exactly once`);
    out = out.split(old).join(next);
  }
  if (out !== after.get(signature).definition) {
    throw new Error(`${signature} is not its previous definition plus the documented patch`);
  }
  patchProof[signature.split('(')[0]] = pairs.length;
}
if (!sameSet(Object.keys(patchProof), PATCHED.map((signature) => signature.split('(')[0]))) {
  throw new Error(`patch proof incomplete: ${JSON.stringify(patchProof)}`);
}
results.migration = {
  added: added.length, patched: changed.length, unchanged: before.size - changed.length,
  replacements: patchProof,
};

// ---------------------------------------------------------------------------
// 1. El tipo de rechazo sobre los 17 capturados.
// ---------------------------------------------------------------------------
const fixture = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));
const CAPTURE = fixture('hotmart_purchase_canceled_att1_20261019.json').eventos;
if (CAPTURE.length !== 17) throw new Error(`the capture changed: ${CAPTURE.length} events`);
const EXPECTED_KINDS = [
  'declined', 'declined', 'declined', 'insufficient_funds', 'declined', 'not_a_refusal',
  'declined', 'not_a_refusal', 'declined', 'not_a_refusal', 'not_a_refusal', 'declined',
  'declined', 'declined', 'insufficient_funds', 'insufficient_funds', 'not_a_refusal',
];
{
  const kinds = [];
  for (const event of CAPTURE) {
    const payment = event.payload.data.purchase.payment;
    kinds.push(one((await db.query(
      'select public.payment_failure_refusal_kind($1, $2) as kind',
      [payment.refusal_reason ?? null, payment.type],
    )).rows, 'kind').kind);
  }
  if (JSON.stringify(kinds) !== JSON.stringify(EXPECTED_KINDS)) {
    throw new Error(`refusal kinds: ${JSON.stringify(kinds)}`);
  }
  const tally = kinds.reduce((acc, kind) => ({ ...acc, [kind]: (acc[kind] ?? 0) + 1 }), {});
  results.refusal_kind = tally;
}

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
const offerByCode = (code) => {
  const offer = offers.find((candidate) => candidate.offer_code === code);
  if (!offer) throw new Error(`offer ${code} is not in the ATT1 manifest`);
  return offer;
};
const PILOT = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/instances/att1/politica-piloto.json'), 'utf8',
));
const cartScope = required(PILOT.pilot_scope, 'politica-piloto.pilot_scope');
const cartPolicy = required(PILOT.policy, 'politica-piloto.policy');
const CHANNEL_PROVIDER = required(cartScope.channel_provider, 'pilot_scope.channel_provider');
const CHANNEL_REF = `${required(cartScope.channel_account_ref_prefix, 'pilot_scope.channel_account_ref_prefix')}${ATT1.inboxId}`;
const ALL_DAY = (windows) => windows.map((window) => ({ ...window, start: '00:00', end: '23:59' }));

// El pago rechazado: valores del diseno (B1), con la desviacion de la ventana.
const REFUSAL = {
  scope: 'att1-pago-rechazado',
  scopeVersion: 1,
  policy: 'att1-pago-rechazado-un-toque',
  policyVersion: 1,
  graceMinutes: 15,
  expiresAfter: '1 day',
  step: 'payment_failure_first_contact',
};

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
  // El catalogo del link de recuperacion (el de validate_recovery_checkout_issuance.mjs).
  await db.query(`
    insert into public.checkout_offer_catalog (
      tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref,
      offer_code, checkout_base_url, checkout_mode, default_for_inbound, status,
      version, approved_by, approved_at
    ) values ($1,$2,$3,$4,$5,$6,$7,$8,10,$9,'active',1,'operator-test',now())
  `, [ATT1.tenant, ATT1.funnel, ATT1.inboundScope, ATT1.inboundVersion, ATT1.hotlink,
    offer.landing_id, offer.offer_code, `https://pay.hotmart.com/${ATT1.hotlink}`,
    offer === defaultOffer]);
}
await db.query(`
  insert into public.commercial_ally_hotmart_purchase_policies
    (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
  values ($1,$2,$3,true,$4::interval)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion,
  required(PILOT.purchase_policy?.max_lookback, 'purchase_policy.max_lookback')]);

const insertPolicy = (key, version, grace, expires, steps, windows) => db.query(`
  insert into public.followup_policy_versions
    (policy_key, version, status, purpose, timezone, business_windows,
     grace_period, expires_after, max_automatic_messages, steps,
     approved_by, approved_at, published_at)
  values ($1,$2,'published','cart_recovery',$3,$4::jsonb,$5::interval,$6::interval,1,$7::jsonb,
          'operator-test',now(),now())
`, [key, version, ATT1.timezone, JSON.stringify(windows), grace, expires, JSON.stringify(steps)]);
const insertScope = (key, version, eventType, additionalTypes, policy, policyVersion, audience) => db.query(`
  insert into public.pilot_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     channel, channel_provider, channel_account_ref, source, source_event_type,
     additional_source_event_types, external_product_id, offer_code,
     additional_offer_codes, purpose, policy_key, policy_version, timezone,
     max_cohort_contacts, max_outbound_request_starts_total,
     max_outbound_request_starts_per_day, audience_mode,
     approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5,'whatsapp',$6,$7,'hotmart',$8,$9::text[],$10,$11,
          $12::text[],'cart_recovery',$13,$14,$15,20,20,20,$16,'operator-test',now(),now())
`, [key, version, ATT1.tenant, ATT1.accountId, ATT1.inboxId, CHANNEL_PROVIDER, CHANNEL_REF,
  eventType, additionalTypes, String(ATT1.productId), defaultOffer.offer_code,
  additionalOffers.map((offer) => offer.offer_code), policy, policyVersion, ATT1.timezone,
  audience]);
const insertControl = (key, version) => db.query(`
  insert into public.pilot_runtime_controls
    (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
  values ($1,$2,'inactive',0,'operator-test','default-off')
`, [key, version]);

await insertPolicy(REFUSAL.policy, REFUSAL.policyVersion, `${REFUSAL.graceMinutes} minutes`,
  // freeform como la politica de la instancia: reevaluate_followup_action
  // escala un paso approved_template, y el bridge reserva en approved_template
  // porque el scope es waba.
  REFUSAL.expiresAfter, [{ step_key: REFUSAL.step, mode: 'freeform' }],
  ALL_DAY(cartPolicy.business_windows));
await insertScope(REFUSAL.scope, REFUSAL.scopeVersion, 'PURCHASE_CANCELED', [],
  REFUSAL.policy, REFUSAL.policyVersion, 'consented_intent');
await insertControl(REFUSAL.scope, REFUSAL.scopeVersion);
await db.query(`
  select * from public.set_lancemos_pilot_runtime_state($1,$2,0,'armed','operator-test','controlled-test')
`, [REFUSAL.scope, REFUSAL.scopeVersion]);

// ---------------------------------------------------------------------------
// Las capturas y las derivaciones.
// ---------------------------------------------------------------------------
const dbNow = async () => new Date((await db.query('select clock_timestamp() as now')).rows[0].now);
const baseMs = Math.floor((await dbNow()).getTime() / 1000) * 1000;
const at = (minutes) => new Date(baseMs + minutes * 60_000);
const digits = (value) => String(value ?? '').replace(/\D/g, '');
const shifted = (payload, when, suffix = '') => {
  const copy = structuredClone(payload);
  copy.creation_date = when.getTime();
  if (suffix) {
    copy.id = `${payload.id}-${suffix}`;
    copy.data.purchase.transaction = `${payload.data.purchase.transaction}${suffix.toUpperCase().replace(/[^A-Z0-9]/g, '')}`;
  }
  return copy;
};
const byPerson = (persona) => CAPTURE.filter((event) => event.persona === persona)
  .map((event) => event.payload);
const expectError = async (label, run, { code, message, detail } = {}) => {
  let error = null;
  await db.exec('begin');
  try {
    await run();
  } catch (caught) {
    error = caught;
  } finally {
    await db.exec('rollback');
  }
  if (error === null
      || (code !== undefined && error.code !== code)
      || (message !== undefined && error.message !== message)
      || (detail !== undefined && error.detail !== detail)) {
    throw new Error(`${label}: expected ${code} ${message} ${detail ?? ''}, got ${error ? `${error.code} ${error.message} ${error.detail}` : 'success'}`);
  }
  return error;
};

// El formulario con consentimiento con el movil mexicano de la persona.
const admitForm = async (lead, submittedAt) => {
  const offer = lead.offer;
  const id = `refusal-form-${lead.email}-${offer.offer_code}`;
  const pageUrl = `https://${offer.page_host}${offer.page_path}`;
  const checkoutUrl = `https://pay.hotmart.com/${ATT1.hotlink}?off=${offer.offer_code}&checkoutMode=10`;
  const raw = {
    id,
    event: 'lead.precheckout',
    version: '1.1.0',
    created_at: submittedAt.toISOString(),
    source: {
      system: 'landing', site: offer.site, aliado: ATT1.brand,
      landing_id: offer.landing_id, page_url: pageUrl,
    },
    data: {
      buyer: {
        name: lead.name, email: lead.email, phone: `+${lead.phone}`,
        phone_country_code: '52', phone_national: lead.phone.slice(2),
      },
      product: {
        hotlink: ATT1.hotlink, id: null, name: ATT1.productName,
        price: Number(ATT1.price), currency: ATT1.currency,
      },
      offer: { code: offer.offer_code },
      checkout_url: checkoutUrl,
      checkout_country: { iso: 'MX', source: 'phone_country_code' },
      consent: { marketing_optin: true, whatsapp_contact: true, copy_version: ATT1.copyVersion },
    },
    dedupe_key: `${offer.site}:${offer.offer_code}:${lead.email}`,
  };
  const canonical = {
    external_submission_id: id,
    event_type: 'PRECHECKOUT_FORM_SUBMITTED',
    contract_version: '1.1.0',
    submitted_at: raw.created_at,
    source: {
      tenant_ref: ATT1.tenant, funnel_ref: ATT1.funnel, landing_ref: offer.landing_id,
      page_url: pageUrl, aliado: ATT1.brand,
    },
    identity: { email: lead.email, phone: lead.phone, phone_valid: true, phone_country_iso: 'MX' },
    lead: { full_name: lead.name },
    commerce: {
      product_ref: ATT1.hotlink, product_name: ATT1.productName, offer_ref: offer.offer_code,
      price: String(ATT1.price), currency: ATT1.currency, checkout_url: checkoutUrl,
    },
    dedupe_key: raw.dedupe_key,
    consent: {
      terms_accepted: false, privacy_accepted: false, marketing_optin: true,
      whatsapp_contact: true, copy_version: ATT1.copyVersion,
    },
    assurance: { provisional: false, provider_observed: true, activation_authorized: true },
  };
  const admitted = one((await db.query(`
    select * from public.admit_portable_observed_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, id, JSON.stringify(raw),
    JSON.stringify(canonical)])).rows, `form ${lead.email}`);
  if (admitted.outcome !== 'inserted') throw new Error(`form ${lead.email}: ${admitted.outcome}`);
  return admitted.purchase_intent_id;
};
const createContact = async (lead, eventId) => {
  lead.contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone, country_iso)
    values ($1,$2,$3,'MX') returning id
  `, [lead.name, lead.email, lead.phone])).rows, `contact ${lead.email}`).id;
  await db.query(`
    insert into public.contact_points
      (contact_id, type, raw_value, normalized_value, source, source_event_id)
    values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)
  `, [lead.contact, lead.email, lead.phone, eventId]);
};
const admitRefusal = async (payload) => one((await db.query(`
  select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload),
  payload.data.buyer.email.toLowerCase(), digits(payload.data.buyer.checkout_phone)])).rows,
`refusal ${payload.id}`);
// Los mismos argumentos que va a armar resolution.resolve_event.
const planRefusal = (lead, refusal) => db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
  )
`, [refusal.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  refusal.offer, REFUSAL.policy, REFUSAL.policyVersion, refusal.createdAt.toISOString(),
  ATT1.accountId, ATT1.inboxId, lead.phone, REFUSAL.scope, REFUSAL.scopeVersion]);
const actionOf = async (id) => one((await db.query(`
  select status, step_key, anchor_type, due_at, expires_at, next_attempt_at
  from public.scheduled_actions where id = $1
`, [id])).rows, 'action');
const caseState = async (caseId) => one((await db.query(`
  select rc.status, (rc.context ->> 'payment_failure_count')::integer as count,
         rc.context ->> 'latest_payment_failure_event_id' as latest,
         (select count(*)::integer from public.scheduled_actions where recovery_case_id = rc.id) as actions,
         (select count(*)::integer from public.recovery_case_events
           where recovery_case_id = rc.id and event_role = 'payment_failure') as refusals,
         public.payment_failure_wants_first_coupon(rc.id) as wants_coupon
  from public.recovery_cases rc where rc.id = $1
`, [caseId])).rows, 'case');
const casesOf = async (contact) => one((await db.query(`
  select count(*)::integer as count from public.recovery_cases
  where contact_id = $1 and context ->> 'trigger_kind' = 'payment_failure'
`, [contact])).rows, 'cases').count;

// Una persona por captura: su formulario 30 minutos antes y su rechazo a la
// hora pedida.
const people = new Map();
const refusalFor = async (payload, createdAt, { suffix = '' } = {}) => {
  let lead = people.get(payload.data.buyer.email);
  if (!lead) {
    lead = {
      email: payload.data.buyer.email,
      phone: digits(payload.data.buyer.checkout_phone),
      name: payload.data.buyer.name,
      offer: offerByCode(payload.data.purchase.offer.code),
    };
    people.set(lead.email, lead);
    lead.intent = await admitForm(lead, new Date(createdAt.getTime() - 30 * 60_000));
  }
  const body = shifted(payload, createdAt, suffix);
  const admitted = await admitRefusal(body);
  if (admitted.outcome !== 'inserted') throw new Error(`admission ${body.id}: ${admitted.outcome}`);
  if (lead.contact === undefined) await createContact(lead, admitted.webhook_event_id);
  return {
    lead, body, eventId: admitted.webhook_event_id, createdAt,
    offer: payload.data.purchase.offer.code,
  };
};

// ---------------------------------------------------------------------------
// 2. El plan.
// ---------------------------------------------------------------------------
// S: el «Saldo insuficiente.» de Mexico (persona 12). D: los dos rechazos de
// la persona 2 en un minuto. B: el «negada pelo banco» de la persona 4, que se
// manda en el caso 5. M: el Mercado Pago sin motivo de la persona 6.
const [S_PAYLOAD] = byPerson(12);
const D_PAYLOADS = byPerson(2);
const [B_PAYLOAD] = byPerson(4);
const [M_PAYLOAD] = byPerson(6);
if (S_PAYLOAD.data.purchase.payment.refusal_reason !== 'Saldo insuficiente.'
    || D_PAYLOADS.length !== 2
    || B_PAYLOAD.data.purchase.payment.refusal_reason !== 'Transação negada pelo banco emissor'
    || M_PAYLOAD.data.purchase.payment.type !== 'MERCADO_PAGO'
    || [S_PAYLOAD, ...D_PAYLOADS, B_PAYLOAD, M_PAYLOAD]
      .some((payload) => payload.data.purchase.checkout_country.iso !== 'MX')) {
  throw new Error('the capture no longer has the cases this validator uses');
}
const S = await refusalFor(S_PAYLOAD, at(-5));
const D1 = await refusalFor(D_PAYLOADS[0], at(-8));
const D2 = await refusalFor(D_PAYLOADS[1], at(-7));
const B = await refusalFor(B_PAYLOAD, at(-16));
const M = await refusalFor(M_PAYLOAD, at(-5));
const plans = {};
{
  plans.S = one((await planRefusal(S.lead, S)).rows, 'S plan');
  const action = await actionOf(plans.S.scheduled_action_id);
  const state = await caseState(plans.S.recovery_case_id);
  if (!plans.S.created || action.step_key !== REFUSAL.step || action.anchor_type !== 'payment_failure'
      || action.due_at.getTime() !== S.createdAt.getTime() + REFUSAL.graceMinutes * 60_000
      || state.count !== 1 || state.actions !== 1 || state.wants_coupon !== true) {
    throw new Error(`S plan: ${JSON.stringify({ plan: plans.S, action, state })}`);
  }

  plans.D = one((await planRefusal(D1.lead, D1)).rows, 'D first plan');
  const second = one((await planRefusal(D2.lead, D2)).rows, 'D second plan');
  const dAction = await actionOf(plans.D.scheduled_action_id);
  const dState = await caseState(plans.D.recovery_case_id);
  if (!plans.D.created || second.created
      || second.recovery_case_id !== plans.D.recovery_case_id
      || second.scheduled_action_id !== plans.D.scheduled_action_id
      || dAction.due_at.getTime() !== D2.createdAt.getTime() + REFUSAL.graceMinutes * 60_000
      || dAction.next_attempt_at !== null
      || dState.count !== 2 || dState.actions !== 1 || dState.refusals !== 2
      || dState.latest !== D2.eventId || dState.wants_coupon !== false
      || (await casesOf(D1.lead.contact)) !== 1) {
    throw new Error(`D plans: ${JSON.stringify({ first: plans.D, second, dAction, dState })}`);
  }
  // El replay del mismo evento no cuenta dos veces.
  const replay = one((await planRefusal(D2.lead, D2)).rows, 'D replay');
  if (replay.created || (await caseState(plans.D.recovery_case_id)).count !== 2) {
    throw new Error('D replay counted twice');
  }

  plans.B = one((await planRefusal(B.lead, B)).rows, 'B plan');
  await expectError('a pending that was cancelled', () => planRefusal(M.lead, M),
    { code: '55000', message: 'payment_failure_not_a_refusal' });

  // Un rechazo de la otra oferta del mismo producto se suma al caso de S. La
  // persona llego a esa oferta por su propio formulario: sin el, el rechazo
  // no se correlaciona.
  const otherOffer = offers.find((offer) => offer.offer_code !== S.offer);
  await admitForm({ ...S.lead, offer: otherOffer }, at(-20));
  const crossBody = structuredClone(S_PAYLOAD);
  crossBody.data.purchase.offer.code = otherOffer.offer_code;
  const cross = await refusalFor(crossBody, at(-4), { suffix: 'otra-oferta' });
  const crossPlan = one((await planRefusal(S.lead, cross)).rows, 'S other offer');
  const sState = await caseState(plans.S.recovery_case_id);
  if (crossPlan.created || crossPlan.recovery_case_id !== plans.S.recovery_case_id
      || sState.count !== 2 || sState.actions !== 1) {
    throw new Error(`S other offer: ${JSON.stringify({ crossPlan, sState })}`);
  }
  // Ese segundo rechazo vuelve a S «dos rechazos»: sin cupon propio. Para el
  // resto del validador S tiene que seguir siendo un saldo de un rechazo: se
  // deshace con la fila de contexto, que es lo unico que cambio de S.
  await db.query(`
    update public.recovery_cases
    set context = context || jsonb_build_object(
          'payment_failure_count', 1,
          'latest_payment_failure_event_id', $2::uuid)
    where id = $1
  `, [plans.S.recovery_case_id, S.eventId]);
  results.plan = {
    grace_minutes: REFUSAL.graceMinutes,
    two_refusals: { cases: 1, actions: dState.actions, count: dState.count,
      due_after_second_minutes: (dAction.due_at.getTime() - D2.createdAt.getTime()) / 60_000 },
    pending_cancelled: 'payment_failure_not_a_refusal',
    other_offer: 'aggregated',
  };
}

// ---------------------------------------------------------------------------
// 3. El primer contacto del formulario cede ante el pago rechazado.
// ---------------------------------------------------------------------------
const stopReason = async (lead) => one((await db.query(
  'select public._portable_precheckout_stop_reason($1, $2) as reason',
  [lead.intent, lead.contact],
)).rows, 'stop reason').reason;
results.first_contact_with_open_refusal = await stopReason(S.lead);
if (results.first_contact_with_open_refusal !== 'superseded_by_provider_event') {
  throw new Error(`first contact with an open refusal: ${results.first_contact_with_open_refusal}`);
}

// ---------------------------------------------------------------------------
// 4. La siembra del cupon.
// ---------------------------------------------------------------------------
{
  await db.exec('begin');
  try {
    const now = await dbNow();
    await db.query(`
      select * from public.claim_due_recovery_coupons_v1($1,'coupon-test',60,'ATT1',
        interval '15 minutes', interval '24 hours', 0.10, 50)
    `, [now]);
    const seeded = (await db.query(`
      select recovery_case_id, ordinal, expires_at from public.recovery_coupons
    `)).rows;
    const sAction = await actionOf(plans.S.scheduled_action_id);
    const forS = seeded.filter((row) => row.recovery_case_id === plans.S.recovery_case_id);
    if (forS.length !== 1 || forS[0].ordinal !== 1
        || forS[0].expires_at.getTime() !== sAction.due_at.getTime() + 24 * 3_600_000
        || seeded.some((row) => row.recovery_case_id === plans.D.recovery_case_id
          || row.recovery_case_id === plans.B.recovery_case_id)) {
      throw new Error(`coupon seeding: ${JSON.stringify(seeded)}`);
    }
    results.coupon_seeding = {
      insufficient_funds: 'ordinal 1, due + 24 h', two_refusals: 'none', declined: 'none',
    };
  } finally {
    await db.exec('rollback');
  }
}

// ---------------------------------------------------------------------------
// 5. B de punta a punta, con la reevaluacion real y el link.
// ---------------------------------------------------------------------------
{
  const worker = 'refusal-worker-b';
  const now = await dbNow();
  const claimed = (await db.query(`
    select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
  `, [worker, now])).rows;
  if (claimed.length !== 1 || claimed[0].id !== plans.B.scheduled_action_id) {
    throw new Error(`B claim: ${JSON.stringify(claimed.map((row) => [row.id, row.anchor_type]))}`);
  }
  const lease = claimed[0].lease_generation;
  const decision = one((await db.query(`
    select * from public.reevaluate_followup_action($1,$2,$3,$4)
  `, [plans.B.scheduled_action_id, worker, lease, now])).rows, 'B reevaluation');
  if (decision.decision !== 'execute') {
    throw new Error(`B real reevaluation: ${JSON.stringify(decision)}`);
  }
  const context = one((await db.query(`
    select * from public.get_portable_payment_failure_send_context_v1($1,$2,$3,$4)
  `, [plans.B.scheduled_action_id, worker, lease, now])).rows, 'B send context');
  if (context.refusal_kind !== 'declined' || context.payment_failure_count !== 1
      || context.latest_failed_at.getTime() !== B.createdAt.getTime()
      || context.payment_type !== 'CREDIT_CARD' || context.other_coupon_code !== null) {
    throw new Error(`B send context: ${JSON.stringify(context)}`);
  }
  await expectError('send context with another lease', () => db.query(`
    select * from public.get_portable_payment_failure_send_context_v1($1,$2,$3,$4)
  `, [plans.B.scheduled_action_id, worker, lease + 1, now]),
  { code: '55000', message: 'current_action_lease_not_found' });
  const attempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
  `, [plans.B.scheduled_action_id, worker, lease, decision.case_version,
    decision.sequence_revision, now])).rows, 'B reservation');
  const link = one((await db.query(`
    select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)
  `, [plans.B.scheduled_action_id, worker, lease, attempt.id, '01JR0000000000000000000001', now])).rows,
  'B link');
  if (link.outcome !== 'reserved'
      || !link.checkout_url_final?.startsWith(`https://pay.hotmart.com/${ATT1.hotlink}`)
      || !link.checkout_url_final.includes(`off=${B.offer}`)) {
    throw new Error(`B link: ${JSON.stringify(link)}`);
  }
  const startedAt = await dbNow();
  const started = one((await db.query(`
    select * from public.mark_portable_payment_failure_request_started($1,$2,$3,$4,$5)
  `, [plans.B.scheduled_action_id, attempt.id, worker, lease, startedAt])).rows, 'B start');
  const authorization = one((await db.query(`
    select scope_key from public.pilot_outbound_request_authorizations where id = $1
  `, [started.pilot_authorization_id])).rows, 'B authorization');
  const accepted = one((await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,'920001','refusal-wamid-1',$5,$6)
  `, [plans.B.scheduled_action_id, attempt.id, worker, lease,
    `Plantilla aprobada del pago rechazado para ${B.lead.name}`, await dbNow()])).rows, 'B acceptance');
  const closed = await caseState(plans.B.recovery_case_id);
  if (started.phase !== 'request_started' || authorization.scope_key !== REFUSAL.scope
      || accepted.status !== 'accepted_by_chatwoot' || closed.status !== 'sequence_exhausted') {
    throw new Error(`B dispatch: ${JSON.stringify({ started, authorization, accepted, closed })}`);
  }
  results.dispatch = {
    send_context: `${context.refusal_kind}/${context.payment_failure_count}`,
    link: link.outcome, start_scope: authorization.scope_key,
    acceptance: accepted.status, case_after: closed.status,
  };
  // Con el caso cerrado, el primer contacto del formulario sigue frenado.
  results.first_contact_after_refusal_sent = await stopReason(B.lead);
  if (results.first_contact_after_refusal_sent !== 'superseded_by_provider_event') {
    throw new Error(`first contact after the refusal message: ${results.first_contact_after_refusal_sent}`);
  }
}

// ---------------------------------------------------------------------------
// 6. Un segundo rechazo de B despues del mensaje, y otro 73 h despues.
// ---------------------------------------------------------------------------
{
  const again = await refusalFor(B_PAYLOAD, at(-2), { suffix: 'otra-vez' });
  const plan = one((await planRefusal(B.lead, again)).rows, 'B again');
  const state = await caseState(plans.B.recovery_case_id);
  if (plan.created || plan.recovery_case_id !== plans.B.recovery_case_id
      || state.status !== 'sequence_exhausted' || state.actions !== 1 || state.count !== 2
      || (await casesOf(B.lead.contact)) !== 1) {
    throw new Error(`B again: ${JSON.stringify({ plan, state })}`);
  }
  // El caso de B, corrido 73 h al pasado: un rechazo nuevo abre otro caso.
  await db.query(`
    update public.recovery_cases set created_at = created_at - interval '73 hours' where id = $1
  `, [plans.B.recovery_case_id]);
  const later = await refusalFor(B_PAYLOAD, at(-1), { suffix: 'mas-tarde' });
  const laterPlan = one((await planRefusal(B.lead, later)).rows, 'B later');
  if (!laterPlan.created || laterPlan.recovery_case_id === plans.B.recovery_case_id
      || (await casesOf(B.lead.contact)) !== 2) {
    throw new Error(`B after 72 h: ${JSON.stringify(laterPlan)}`);
  }
  results.after_message = { within_72h: 'aggregated, no new action', after_72h: 'new case' };
}

// ---------------------------------------------------------------------------
// 7. El cupon vigente de la persona en otro caso (B1-cupon).
// ---------------------------------------------------------------------------
{
  const cartCase = one((await db.query(`
    insert into public.recovery_cases
      (contact_id, abandonment_event_id, source, external_product_id, product_name,
       offer_code, status, lead_stage, grace_expires_at, policy_key, policy_version, context)
    values ($1,$2,'hotmart',$3,$4,$5,'active','new',now(),$6,$7,'{"trigger_kind":"cart_abandonment"}'::jsonb)
    returning id
  `, [S.lead.contact, M.eventId, String(ATT1.productId), ATT1.productName, S.offer,
    REFUSAL.policy, REFUSAL.policyVersion])).rows, 'cart case').id;
  // El evento de origen es el Mercado Pago cancelado de M, que nunca abrio un
  // caso (no es un rechazo), y la politica es la que ya esta publicada: el
  // caso del carrito es solo el dueno del cupon.
  const expiresAt = at(20 * 60);
  await db.query(`
    insert into public.recovery_coupons
      (recovery_case_id, ordinal, code, external_product_id, discount, expires_at, status,
       hotmart_start_ms, hotmart_end_ms, verified_at, created_at, updated_at)
    values ($1,1,'ATT1CARRITO0001',$2,0.10,$3,'valid',$4,$5,now(),now(),now())
  `, [cartCase, String(ATT1.productId), expiresAt, baseMs - 4 * 3_600_000,
    expiresAt.getTime() - 3 * 3_600_000]);
  // Sin A1 que lo haya mandado, el cupon no le llego a la persona
  // (20261019000500): S sigue pidiendo el suyo.
  const state = await caseState(plans.S.recovery_case_id);
  if (state.wants_coupon !== true) throw new Error('S stopped asking for its own coupon for an unsent one');
  await db.exec('begin');
  try {
    const due = await actionOf(plans.S.scheduled_action_id);
    const when = new Date(due.due_at.getTime() + 60_000);
    const claimed = (await db.query(`
      select * from public.claim_due_followup_actions('refusal-s',$1,interval '5 minutes',100)
    `, [when])).rows.find((row) => row.id === plans.S.scheduled_action_id);
    if (!claimed) throw new Error('S action was not claimable at its due time');
    const context = one((await db.query(`
      select * from public.get_portable_payment_failure_send_context_v1($1,'refusal-s',$2,$3)
    `, [claimed.id, claimed.lease_generation, when])).rows, 'S send context');
    if (context.refusal_kind !== 'insufficient_funds' || context.payment_failure_count !== 1
        || context.other_coupon_code !== null
        || context.other_coupon_expires_at !== null) {
      throw new Error(`S send context: ${JSON.stringify(context)}`);
    }
  } finally {
    await db.exec('rollback');
  }
  results.other_coupon = 'an unsent coupon of another case: not given; own coupon still asked';
}

console.log(JSON.stringify(results, null, 2));
