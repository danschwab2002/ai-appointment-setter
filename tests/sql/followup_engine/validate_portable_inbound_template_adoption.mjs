// La respuesta a una plantilla del piloto entra por la admision portable (H7,
// migracion 20261001000400).
//
// Antes de esta migracion, quien respondia a una plantilla del dispatcher del
// piloto no pasaba la admision entrante: la aceptacion deja la conversacion del
// caso en automation_status = 'enabled' y admit_inbound_commercial_case_v2
// rechaza con 22000 inbound_canonical_conversation_conflict. Este validador
// recorre, sobre la cadena completa de migraciones y con las RPC reales, la
// cadena del dispatcher hasta la aceptacion (como validate_att1_portable_chain.mjs)
// y despues la respuesta por admit_portable_inbound_commercial_case_v1, como
// service_role, el rol del bridge.
//
// Casos:
//   1. la adopcion, para el primer contacto tras el formulario, el carrito y el
//      pago fallido: la v2 sola da el 22000 de hoy; la portable da
//      created/draft_only, la conversacion pasa a draft_only con version + 1,
//      queda un solo evento inbound_adopted_template_conversation (actor
//      integration, la plantilla y su accion) y el caso inbound_sales en esa
//      conversacion; el replay da already_exists por la portable y por la v2,
//      sin otro evento;
//   2. los frenos, cada uno dentro de una transaccion que se deshace y
//      comparado con la v2 en ese mismo estado (la portable no adopta y da
//      exactamente el error de la v2). Sobre la conversacion de la plantilla
//      aceptada del carrito: human_takeover, la conversacion en paused_human,
//      el contacto opted_out, blocked, restricted o do_not_contact, la baja
//      real desde otra conversacion de Chatwoot, la falta de la fila del
//      binding del piloto (ancla sin binding), la accion en pending,
//      deferred, retryable_failed o delivery_unknown, y un tercero que
//      escribe ahi (23505). Sobre la del primer contacto, la otra forma del
//      movil mexicano (521), sin identidad propia (23505), y la baja de esa
//      otra forma, que la RPC real deja unmatched (el contacto conserva su
//      permiso). Y una conversacion enabled sin ninguna plantilla (sin
//      ancla): 22000. Cada eslabon de la prueba de la plantilla roto de a
//      uno (el mensaje sin durable_followup, el intento no aceptado, el
//      caso en otra conversacion o con otra identidad) y, despues de la
//      adopcion, la conversacion ya admitida que vuelve a enabled: la
//      portable da lo mismo que la v2 y no adopta otra vez;
//   3. el paso pendiente de otro caso de la misma persona, por las RPC reales:
//      con el carrito aceptado, el pago fallido planificado (pending) y
//      despues en delivery_unknown frenan la adopcion; cuando la reconciliacion
//      acepta ese intento en la misma conversacion, la respuesta se adopta y
//      el evento apunta a la plantilla mas nueva (la del pago fallido);
//   3b. el primer contacto que ya no va a salir (20261009000200): con el
//      formulario planificado y el carrito aceptado antes de su hora, la
//      respuesta se adopta; sin la clasificacion del carrito sigue frenando
//      (decision 6), y a su hora la reevaluacion real lo cancela sin intento;
//   4. la forma de Johanna, sin fila previa: la portable da created igual que
//      la v2 y deja las mismas filas, sin evento de adopcion;
//   5. la respuesta en otra conversacion de Chatwoot: se crea esa conversacion
//      en draft_only, igual que con la v2, y la de la plantilla queda enabled y
//      sin evento; despues la respuesta en la de la plantilla se adopta;
//   6. el ACL: definer con search_path fijo, solo service_role la ejecuta;
//      anon y authenticated reciben 42501.
//
// Datos: binding, ofertas, landings, producto, Chatwoot y consentimiento de
// tests/fixtures/instances/att1/instancia.toml; politica y scopes del piloto de
// tests/fixtures/instances/att1/politica-piloto.json, con la misma desviacion
// documentada de la ventana de envio que validate_att1_portable_chain.mjs
// (00:00 a 23:59: la puerta de arranque exige el reloj real). Carrito: la
// captura tests/fixtures/hotmart_cart_abandonment_rejected_v1.json con
// producto, oferta, id, fecha y comprador sustituidos. Formulario del primer
// contacto: el golden del traductor de GHL de la landing -d (movil mexicano
// 52 + 10), con id, fecha y comprador sustituidos. El pago fallido y el
// formulario del carrito usan los precedentes inline de
// validate_att1_portable_chain.mjs (no hay PURCHASE_CANCELED ni
// lead.precheckout 1.1.0 capturados). Los telefonos son sinteticos. El texto
// aceptado es un marcador: la base guarda el que le pasa el bridge.
//
// Lo que se simula y por que: ATT1 tiene una politica de un toque (D2), asi
// que una accion viva al lado de una plantilla aceptada no sale de la cadena
// real salvo por otro caso de la misma persona (caso 3). Los estados vivos de
// la propia accion, la baja del contacto, la derivacion y la falta de binding
// se fijan con un update dentro de una transaccion que se deshace: el freno
// se prueba sobre el predicado exacto, y la base queda como estaba.
import { PGlite } from '@electric-sql/pglite';
import { randomBytes } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const db = new PGlite();
await db.waitReady;
// Los roles de la API con el execute por defecto de Supabase: una funcion
// nueva sin su revoke queda ejecutable por anon y authenticated, y el caso 6
// lo ve.
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  grant usage on schema public to service_role;
  alter default privileges grant execute on functions to anon, authenticated;
`);
for (const file of [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(join(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => join(root, 'supabase/migrations', name)),
]) {
  await db.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}
const PORTABLE_SIGNATURE = 'public.admit_portable_inbound_commercial_case_v1(text,integer,bigint,text)';
if ((await db.query('select to_regprocedure($1) as oid', [PORTABLE_SIGNATURE])).rows[0].oid === null) {
  throw new Error(`${PORTABLE_SIGNATURE} does not exist: migration 20261001000400 is missing`);
}
const PORTABLE_SQL = 'select * from public.admit_portable_inbound_commercial_case_v1($1,$2,$3,$4)';
const V2_SQL = 'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)';
const ADOPTION_EVENT = 'inbound_adopted_template_conversation';

// ---------------------------------------------------------------------------
// El manifiesto de ATT1. Lector minimo del subconjunto de TOML que usan los
// campos de aca (el mismo de validate_att1_portable_chain.mjs).
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
const manifest = readManifest(readFileSync(
  join(root, 'tests/fixtures/instances/att1/instancia.toml'), 'utf8',
));
const required = (value, label) => {
  if (value === undefined || value === null || value === '') {
    throw new Error(`instancia.toml de ATT1 sin ${label}`);
  }
  return value;
};
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
  throw new Error(`ATT1 declara tres ofertas y la primera es la por defecto: ${JSON.stringify(offers)}`);
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
const landingOf = (offer) => ({
  offer_code: offer.offer_code, site: offer.site, landing_id: offer.landing_id,
  page_host: offer.page_host, page_path: offer.page_path,
});

const PILOT = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/instances/att1/politica-piloto.json'), 'utf8',
));
const pilotScope = required(PILOT.pilot_scope, 'politica-piloto.pilot_scope');
const pilotPolicy = required(PILOT.policy, 'politica-piloto.policy');
const SCOPE = required(pilotScope.scope_key, 'pilot_scope.scope_key');
const SCOPE_VERSION = required(pilotScope.version, 'pilot_scope.version');
const POLICY = required(pilotPolicy.policy_key, 'policy.policy_key');
const POLICY_VERSION = required(pilotPolicy.version, 'policy.version');
const CHANNEL_PROVIDER = required(pilotScope.channel_provider, 'pilot_scope.channel_provider');
const CHANNEL_REF = `${required(pilotScope.channel_account_ref_prefix, 'pilot_scope.channel_account_ref_prefix')}${ATT1.inboxId}`;
const STEPS = required(pilotPolicy.steps, 'policy.steps');
const fcScope = required(PILOT.first_contact?.pilot_scope, 'politica-piloto.first_contact.pilot_scope');
const fcPolicy = required(PILOT.first_contact?.policy, 'politica-piloto.first_contact.policy');
const FC_SCOPE = required(fcScope.scope_key, 'first_contact.pilot_scope.scope_key');
const FC_SCOPE_VERSION = required(fcScope.version, 'first_contact.pilot_scope.version');
const FC_POLICY = required(fcPolicy.policy_key, 'first_contact.policy.policy_key');
const FC_GRACE_MINUTES = 60;
if (CHANNEL_PROVIDER !== 'waba'
    || pilotPolicy.max_automatic_messages !== 1
    || STEPS.map((step) => step.step_key).join(',') !== 'first_contact,payment_failure_first_contact'
    || fcScope.source !== 'landing'
    || fcScope.audience_mode !== 'consented_intent_in_cohort'
    || fcPolicy.grace_period !== `${FC_GRACE_MINUTES} minutes`
    || fcPolicy.max_automatic_messages !== 1) {
  // Los casos de abajo asumen un toque por caso y el primer contacto en
  // cohorte con la demora de la politica. Si la instancia los cambia, hay que
  // revisarlos.
  throw new Error(`politica-piloto.json changed shape: ${JSON.stringify({ pilotPolicy, fcScope, fcPolicy })}`);
}

const businessWindows = (policy) => JSON.stringify(required(policy.business_windows, 'business_windows')
  .map((window) => ({ ...window, start: '00:00', end: '23:59' })));
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
  JSON.stringify(additionalOffers.map(landingOf)),
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
for (const [policyKey, policy] of [[POLICY, pilotPolicy], [FC_POLICY, fcPolicy]]) {
  await db.query(`
    insert into public.followup_policy_versions
      (policy_key, version, status, purpose, timezone, business_windows,
       grace_period, expires_after, max_automatic_messages, steps,
       approved_by, approved_at, published_at)
    values ($1,$2,'published',$3,$4,$5::jsonb,$6::interval,$7::interval,$8,$9::jsonb,
            'operator-test',now(),now())
  `, [policyKey, required(policy.version, 'policy.version'), required(policy.purpose, 'policy.purpose'),
    ATT1.timezone, businessWindows(policy), required(policy.grace_period, 'policy.grace_period'),
    required(policy.expires_after, 'policy.expires_after'), policy.max_automatic_messages,
    JSON.stringify(required(policy.steps, 'policy.steps'))]);
}
for (const [scopeKey, scope, policyKey] of [[SCOPE, pilotScope, POLICY], [FC_SCOPE, fcScope, FC_POLICY]]) {
  await db.query(`
    insert into public.pilot_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
       channel, channel_provider, channel_account_ref, source, source_event_type,
       additional_source_event_types, external_product_id, offer_code,
       additional_offer_codes, purpose, policy_key, policy_version, timezone,
       max_cohort_contacts, max_outbound_request_starts_total,
       max_outbound_request_starts_per_day, audience_mode,
       approved_by, approved_at, published_at)
    values ($1,$2,'published',$3,$4,$5,'whatsapp',$6,$7,$8,$9,$10::text[],$11,$12,
            $13::text[],$14,$15,$16,$17,$18,$19,$20,$21,'operator-test',now(),now())
  `, [
    scopeKey, required(scope.version, 'pilot_scope.version'), ATT1.tenant, ATT1.accountId,
    ATT1.inboxId, CHANNEL_PROVIDER, CHANNEL_REF, required(scope.source, 'pilot_scope.source'),
    required(scope.source_event_type, 'pilot_scope.source_event_type'),
    required(scope.additional_source_event_types, 'pilot_scope.additional_source_event_types'),
    String(ATT1.productId), defaultOffer.offer_code,
    additionalOffers.map((offer) => offer.offer_code),
    required(scope.purpose, 'pilot_scope.purpose'), policyKey, 1, ATT1.timezone,
    required(scope.max_cohort_contacts, 'pilot_scope.max_cohort_contacts'),
    required(scope.max_outbound_request_starts_total, 'pilot_scope.max_outbound_request_starts_total'),
    required(scope.max_outbound_request_starts_per_day, 'pilot_scope.max_outbound_request_starts_per_day'),
    scope.audience_mode ?? 'manual_cohort',
  ]);
  await db.query(`
    insert into public.pilot_runtime_controls
      (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
    values ($1,$2,'inactive',0,'operator-test','default-off')
  `, [scopeKey, scope.version]);
}
await db.query(`
  insert into public.commercial_ally_hotmart_purchase_policies
    (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
  values ($1,$2,$3,true,$4::interval)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion,
  required(PILOT.purchase_policy?.max_lookback, 'purchase_policy.max_lookback')]);
// El scope entrante de la instancia, publicado como lo deja aprovisionar-att1.sql.
await db.query(`
  insert into public.inbound_commercial_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     external_product_id, offer_code, approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5,$6,$7,'operator-test',now(),now())
`, [ATT1.inboundScope, ATT1.inboundVersion, ATT1.tenant, ATT1.accountId, ATT1.inboxId,
  String(ATT1.productId), defaultOffer.offer_code]);

const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};
const asService = async (action) => {
  await db.exec('set role service_role');
  try {
    return await action();
  } finally {
    await db.exec('reset role');
  }
};
const tryRows = async (sql, params) => {
  try {
    return { ok: true, rows: (await db.query(sql, params)).rows };
  } catch (caught) {
    return { ok: false, code: caught.code, message: caught.message };
  }
};
const show = (result) => (result.ok
  ? `${result.rows[0].outcome}:${result.rows[0].automation_status}`
  : `${result.code} ${result.message}`);
const armed = one((await db.query(`
  select * from public.set_lancemos_pilot_runtime_state($1,$2,0,'armed','operator-test','controlled-test')
`, [SCOPE, SCOPE_VERSION])).rows, 'arm the recovery scope');
if (armed.runtime_state !== 'armed') throw new Error(`the recovery scope is not armed: ${JSON.stringify(armed)}`);

// ---------------------------------------------------------------------------
// Tiempos: todo sale del reloj de la base (la puerta de arranque exige +-5
// minutos).
// ---------------------------------------------------------------------------
const dbNow = async () => new Date((await db.query('select clock_timestamp() as now')).rows[0].now);
const baseMs = Math.floor((await dbNow()).getTime() / 1000) * 1000;
const at = (minutes) => new Date(baseMs + minutes * 60_000);
const SUBMITTED_AT = at(-50);
const CART_AT = at(-30);
const FAILED_AT = at(-10);
const FC_DUE = at(-(FC_GRACE_MINUTES + 1));

const CAPTURED_CART = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/hotmart_cart_abandonment_rejected_v1.json'), 'utf8',
)).payload;

let personIndex = 0;
const person = (label) => {
  personIndex += 1;
  const suffix = String(personIndex).padStart(2, '0');
  return {
    label,
    name: `Compradora ATT1 ${label}`,
    email: `att1-adoption-${suffix}@example.test`,
    phone: `120255502${suffix}`,
  };
};
// El formulario del primer contacto: el golden del traductor de GHL de la
// landing -d (movil mexicano, 52 + 10), con id, fecha y comprador sustituidos.
const GOLDEN_MX = JSON.parse(readFileSync(join(root,
  'tests/fixtures/ghl/expected/ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json'), 'utf8'));
if (GOLDEN_MX.raw_payload?.id !== GOLDEN_MX.canonical_payload?.external_submission_id
    || GOLDEN_MX.raw_payload.version !== '1.1.0'
    || GOLDEN_MX.canonical_payload.identity.phone_country_iso !== 'MX'
    || GOLDEN_MX.raw_payload.data.buyer.phone_country_code !== '52'
    || GOLDEN_MX.canonical_payload.consent.copy_version !== ATT1.copyVersion
    || GOLDEN_MX.canonical_payload.consent.whatsapp_contact !== true) {
  throw new Error('the -d translator golden is not the MX golden this validator expects');
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
const mexicanPerson = (label) => {
  personIndex += 1;
  const suffix = String(personIndex).padStart(2, '0');
  const national = `55555507${suffix}`;
  return {
    label,
    name: GOLDEN_MX.canonical_payload.lead.full_name,
    email: `att1-adoption-${suffix}@example.test`,
    formPhone: `52${national}`,
    whatsapp: `521${national}`,
    phone: `52${national}`,
    country: 'MX',
    offer: offers.find((offer) => offer.offer_code === GOLDEN_MX.canonical_payload.commerce.offer_ref),
  };
};
const goldenForm = (lead, submittedAt) => {
  const raw = structuredClone(GOLDEN_MX.raw_payload);
  const canonical = structuredClone(GOLDEN_MX.canonical_payload);
  const id = ulidAt(submittedAt.getTime());
  const dedupeKey = `${raw.source.site}:${raw.data.offer.code}:${lead.email}`;
  raw.id = id;
  raw.created_at = submittedAt.toISOString();
  raw.data.buyer.email = lead.email;
  raw.data.buyer.phone = `+${lead.formPhone}`;
  raw.data.buyer.phone_national = lead.formPhone.slice(2);
  raw.dedupe_key = dedupeKey;
  canonical.external_submission_id = id;
  canonical.submitted_at = submittedAt.toISOString().replace('.000Z', 'Z');
  canonical.identity.email = lead.email;
  canonical.identity.phone = lead.formPhone;
  canonical.dedupe_key = dedupeKey;
  return { id, raw, canonical, submittedAt };
};

// Formulario de la landing de la oferta (precedente inline de
// validate_att1_portable_chain.mjs), con consentimiento.
const admitForm = async (lead, offer) => {
  const id = `att1-adoption-form-${lead.email}-${offer.offer_code}`;
  const pageUrl = `https://${offer.page_host}${offer.page_path}`;
  const checkoutUrl = `https://pay.hotmart.com/${ATT1.hotlink}?off=${offer.offer_code}&checkoutMode=10`;
  const raw = {
    id,
    event: 'lead.precheckout',
    version: '1.1.0',
    created_at: SUBMITTED_AT.toISOString(),
    source: {
      system: 'landing', site: offer.site, aliado: ATT1.brand,
      landing_id: offer.landing_id, page_url: pageUrl,
    },
    data: {
      buyer: {
        name: lead.name, email: lead.email, phone: `+${lead.phone}`,
        phone_country_code: '1', phone_national: lead.phone.slice(1),
      },
      product: {
        hotlink: ATT1.hotlink, id: null, name: ATT1.productName,
        price: Number(ATT1.price), currency: ATT1.currency,
      },
      offer: { code: offer.offer_code },
      checkout_url: checkoutUrl,
      checkout_country: { iso: 'US', source: 'phone_country_code' },
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
    identity: {
      email: lead.email, phone: lead.phone, phone_valid: true, phone_country_iso: 'US',
    },
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
    JSON.stringify(canonical)])).rows, `${lead.label} form`);
  if (admitted.outcome !== 'inserted') {
    throw new Error(`${lead.label}: the form was not admitted: ${JSON.stringify(admitted)}`);
  }
  return admitted.purchase_intent_id;
};
const admitCart = async (lead, offer) => {
  const payload = structuredClone(CAPTURED_CART);
  payload.id = `att1-adoption-cart-${lead.email}`;
  payload.creation_date = CART_AT.getTime();
  payload.data.product = { id: ATT1.productId, name: ATT1.productName };
  payload.data.offer = { code: offer.offer_code };
  payload.data.buyer = { name: lead.name, email: lead.email, phone: lead.phone };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
    JSON.stringify(payload), lead.email, lead.phone])).rows, `${lead.label} cart`);
  if (admitted.outcome !== 'inserted') {
    throw new Error(`${lead.label}: the cart was not admitted: ${JSON.stringify(admitted)}`);
  }
  return { eventId: admitted.webhook_event_id, abandonedAt: new Date(payload.creation_date) };
};
const admitFailure = async (lead, offer, transaction) => {
  const payload = {
    id: `att1-adoption-failure-${lead.email}`,
    creation_date: FAILED_AT.getTime(),
    event: 'PURCHASE_CANCELED',
    version: '2.0.0',
    data: {
      buyer: { name: lead.name, email: lead.email, checkout_phone: `+${lead.phone}` },
      product: { id: ATT1.productId, name: ATT1.productName },
      purchase: {
        transaction,
        status: 'CANCELED',
        offer: { code: offer.offer_code },
        payment: { refusal_reason: 'insufficient_funds' },
      },
      checkout_country: { iso: 'MX', name: 'México' },
    },
  };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
    JSON.stringify(payload), lead.email, lead.phone])).rows, `${lead.label} failure`);
  if (admitted.outcome !== 'inserted') {
    throw new Error(`${lead.label}: the payment failure was not admitted: ${JSON.stringify(admitted)}`);
  }
  return { eventId: admitted.webhook_event_id, failedAt: new Date(payload.creation_date) };
};
// Lo que deja resolve_event antes de planificar: contacto nuevo y sus puntos.
const createContact = async (lead, eventId) => {
  lead.contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone, country_iso)
    values ($1,$2,$3,'MX') returning id
  `, [lead.name, lead.email, lead.phone])).rows, `${lead.label} contact`).id;
  await db.query(`
    insert into public.contact_points
      (contact_id, type, raw_value, normalized_value, source, source_event_id)
    values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)
  `, [lead.contact, lead.email, lead.phone, eventId]);
};
// Lo que deja el script de siembra de la instancia para el E2E del primer
// contacto (consented_intent_in_cohort exige el contacto inscripto).
const seedContact = async (lead) => {
  lead.contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone, country_iso)
    values ($1,$2,$3,$4) returning id
  `, [lead.name, lead.email, lead.phone, lead.country])).rows, `${lead.label} contact`).id;
  await db.query(`
    insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1,'email',$2,$2,'manual'), ($1,'phone',$3,$3,'manual')
  `, [lead.contact, lead.email, lead.phone]);
};
const enroll = async (lead, scopeKey, scopeVersion) => {
  const { generation } = one((await db.query(`
    select generation from public.pilot_runtime_controls where scope_key = $1
  `, [scopeKey])).rows, 'scope generation');
  const member = one((await db.query(`
    select * from public.set_lancemos_pilot_cohort_member($1,$2,$3,$4,'active','operator-test','controlled-test')
  `, [scopeKey, scopeVersion, lead.contact, generation])).rows, `${lead.label} enrollment`);
  if (member.member_status !== 'active') {
    throw new Error(`${lead.label} was not enrolled in ${scopeKey}: ${JSON.stringify(member)}`);
  }
};
const planCart = async (lead, offer, cart) => one((await db.query(`
  select * from public.plan_lancemos_pilot_cart_recovery(
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
  )
`, [cart.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  offer.offer_code, POLICY, POLICY_VERSION, cart.abandonedAt.toISOString(), ATT1.accountId,
  ATT1.inboxId, lead.phone, SCOPE, SCOPE_VERSION])).rows, `${lead.label} cart plan`);
const planFailure = async (lead, offer, failure) => one((await db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
  )
`, [failure.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  offer.offer_code, POLICY, POLICY_VERSION, failure.failedAt.toISOString(), ATT1.accountId,
  ATT1.inboxId, lead.phone, SCOPE, SCOPE_VERSION])).rows, `${lead.label} failure plan`);

// La cadena del dispatcher (la misma que validate_att1_portable_chain.mjs fija
// contra el bridge): claim, reevaluacion real, reserva approved_template,
// arranque del piloto por el anchor_type de la accion y, al final, la
// aceptacion de Chatwoot en la conversacion indicada, o un delivery_unknown.
const START_OPERATION_BY_ANCHOR = {
  cart_abandonment: 'mark_lancemos_pilot_request_started',
  payment_failure: 'mark_portable_payment_failure_request_started',
  precheckout_intent: 'mark_portable_precheckout_request_started',
};
const REEVALUATE_OPERATION_BY_ANCHOR = {
  cart_abandonment: 'reevaluate_followup_action',
  payment_failure: 'reevaluate_followup_action',
  precheckout_intent: 'reevaluate_portable_precheckout_action',
};
const asBridge = (anchorType, action) => (
  anchorType === 'precheckout_intent' ? asService(action) : action());
let messageNumber = 0;
const accept = async (lead, delivery, chatwoot) => {
  messageNumber += 1;
  const accepted = one((await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
  `, [delivery.actionId, delivery.attemptId, delivery.worker, delivery.lease, String(chatwoot),
    `att1-adoption-wamid-${messageNumber}`,
    `Plantilla aprobada de ATT1 para ${lead.name} (${ATT1.productName})`,
    await dbNow()])).rows, `${lead.label} acceptance`);
  if (accepted.status !== 'accepted_by_chatwoot') {
    throw new Error(`${lead.label}: acceptance did not finalize: ${JSON.stringify(accepted)}`);
  }
};
const dispatch = async (lead, plan, { anchor, stepKey, offer, chatwoot, finish = 'accept' }) => {
  const worker = `att1-adoption-${lead.label}`;
  const now = await dbNow();
  const claimed = (await db.query(`
    select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
  `, [worker, now])).rows;
  if (claimed.length !== 1 || claimed[0].id !== plan.scheduled_action_id
      || claimed[0].anchor_type !== anchor) {
    throw new Error(`${lead.label}: claimed ${JSON.stringify(claimed.map((row) => [row.id, row.anchor_type]))}, expected ${plan.scheduled_action_id}`);
  }
  const lease = claimed[0].lease_generation;
  const decision = one((await asBridge(anchor, () => db.query(`
    select * from public.${REEVALUATE_OPERATION_BY_ANCHOR[anchor]}($1,$2,$3,$4)
  `, [plan.scheduled_action_id, worker, lease, now]))).rows, `${lead.label} reevaluation`);
  if (decision.decision !== 'execute') {
    throw new Error(`${lead.label}: the real reevaluation did not execute: ${JSON.stringify(decision)}`);
  }
  const context = one((await db.query(`
    select * from public.get_followup_execution_context($1,$2,$3,$4)
  `, [plan.scheduled_action_id, worker, lease, now])).rows, `${lead.label} context`);
  if (context.step_key !== stepKey || context.offer_code !== offer.offer_code) {
    throw new Error(`${lead.label}: execution context diverged: ${JSON.stringify(context)}`);
  }
  const attempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
  `, [plan.scheduled_action_id, worker, lease, decision.case_version,
    decision.sequence_revision, now])).rows, `${lead.label} reservation`);
  const started = one((await asBridge(anchor, () => db.query(`
    select * from public.${START_OPERATION_BY_ANCHOR[anchor]}($1,$2,$3,$4,$5)
  `, [plan.scheduled_action_id, attempt.id, worker, lease, now]))).rows,
  `${lead.label} request start`);
  if (started.phase !== 'request_started' || started.pilot_authorization_id == null) {
    throw new Error(`${lead.label}: the send did not start: ${JSON.stringify(started)}`);
  }
  const delivery = { actionId: plan.scheduled_action_id, attemptId: attempt.id, worker, lease };
  if (finish === 'accept') {
    await accept(lead, delivery, chatwoot);
  } else if (finish === 'delivery_unknown') {
    const unknown = one((await db.query(`
      select * from public.finalize_followup_delivery_attempt(
        $1,$2,$3,$4,'delivery_unknown',null,null,'ambiguous_timeout',null,$5,$6)
    `, [delivery.actionId, delivery.attemptId, worker, lease,
      new Date((await dbNow()).getTime() + 3_600_000), await dbNow()])).rows,
    `${lead.label} delivery unknown`);
    if (unknown.status !== 'delivery_unknown') {
      throw new Error(`${lead.label}: the attempt did not stay delivery_unknown: ${JSON.stringify(unknown)}`);
    }
  } else {
    throw new Error(`unknown finish ${finish}`);
  }
  return delivery;
};

// ---------------------------------------------------------------------------
// Lectura del estado y las sondas.
// ---------------------------------------------------------------------------
const conversationState = async (chatwoot) => {
  const rows = (await db.query(`
    select conversation.id, conversation.status, conversation.automation_status,
           conversation.human_takeover, conversation.version::text as version,
           identity.external_user_id
    from public.conversations conversation
    join public.channel_identities identity on identity.id = conversation.channel_identity_id
    where conversation.commercial_context ->> 'chatwoot_conversation_id' = $1
  `, [String(chatwoot)])).rows;
  if (rows.length > 1) throw new Error(`more than one conversation for ${chatwoot}`);
  return rows[0] ?? null;
};
const adoptionEvents = async (conversationId) => (await db.query(`
  select recovery_case_id, actor_type, related_message_id, related_action_id, data
  from public.conversation_events
  where conversation_id = $1 and event_type = $2
`, [conversationId, ADOPTION_EVENT])).rows;
const allAdoptionEvents = async () => one((await db.query(`
  select count(*)::integer as count from public.conversation_events where event_type = $1
`, [ADOPTION_EVENT])).rows, 'adoption events').count;
const acceptedMessageOf = async (actionId) => one((await db.query(`
  select accepted_message_id from public.followup_delivery_attempts
  where action_id = $1 and outcome = 'accepted_by_chatwoot'
`, [actionId])).rows, 'accepted template').accepted_message_id;
const inboundArgs = (chatwoot, user) => [ATT1.inboundScope, ATT1.inboundVersion, chatwoot, user];
const CONFLICT = '22000 inbound_canonical_conversation_conflict';
const OTHER_IDENTITY = '23505 inbound_external_conversation_owned_by_another_identity';

// Una llamada como service_role dentro de la transaccion abierta, en un
// savepoint que se deshace: la llamada, su error y el set role.
const inSavepoint = async (sql, params) => {
  await db.exec('savepoint adoption_probe');
  let result;
  try {
    await db.exec('set local role service_role');
    result = { ok: true, rows: (await db.query(sql, params)).rows };
    // service_role no lee las tablas: el estado se mira con el rol de la sesion.
    await db.exec('reset role');
    const state = await conversationState(params[2]);
    result.automation_status_after = state?.automation_status ?? null;
  } catch (caught) {
    result = { ok: false, code: caught.code, message: caught.message };
  }
  await db.exec('rollback to savepoint adoption_probe');
  return result;
};
// Un freno: con el estado que deja mutate (todo dentro de una transaccion que
// se deshace), la portable no adopta y da el mismo error que la v2 en ese
// estado, que es el esperado.
const brakes = {};
const expectBrake = async (label, { chatwoot, user }, mutate, expected = CONFLICT) => {
  await db.exec('begin');
  let probe;
  try {
    if (mutate) await mutate();
    const before = await conversationState(chatwoot);
    const v2 = await inSavepoint(V2_SQL, inboundArgs(chatwoot, user));
    const portable = await inSavepoint(PORTABLE_SQL, inboundArgs(chatwoot, user));
    const after = await conversationState(chatwoot);
    const events = before === null ? [] : await adoptionEvents(before.id);
    probe = { before, after, v2: show(v2), portable: show(portable), portableOk: portable.ok, events: events.length };
  } finally {
    await db.exec('rollback');
  }
  brakes[label] = probe.portable;
  if (probe.portableOk || probe.portable !== expected || probe.v2 !== expected
      || probe.before?.automation_status !== probe.after?.automation_status
      || probe.before?.version !== probe.after?.version
      || probe.events !== 0) {
    throw new Error(`brake ${label}: ${JSON.stringify(probe)}`);
  }
};

// La adopcion: la v2 sola da el 22000 de hoy; la portable adopta una vez y el
// replay no vuelve a adoptar.
const adoptions = {};
const expectAdoption = async (label, { chatwoot, user, recoveryCaseId, actionId }) => {
  const before = await conversationState(chatwoot);
  const template = await acceptedMessageOf(actionId);
  const today = await asService(() => tryRows(V2_SQL, inboundArgs(chatwoot, user)));
  const admitted = await asService(() => tryRows(PORTABLE_SQL, inboundArgs(chatwoot, user)));
  const after = await conversationState(chatwoot);
  const events = await adoptionEvents(after.id);
  const inboundCase = admitted.ok ? (await db.query(`
    select commercial_case.case_kind, commercial_case.conversation_id,
           commercial_case.automation_status, admission.external_conversation_id::text as chatwoot
    from public.commercial_cases commercial_case
    join public.inbound_commercial_case_admissions admission
      on admission.commercial_case_id = commercial_case.id
    where commercial_case.id = $1
  `, [admitted.rows[0].commercial_case_id])).rows : [];
  const replay = await asService(() => tryRows(PORTABLE_SQL, inboundArgs(chatwoot, user)));
  const replayV2 = await asService(() => tryRows(V2_SQL, inboundArgs(chatwoot, user)));
  const afterReplay = await conversationState(chatwoot);
  const eventsAfterReplay = await adoptionEvents(after.id);
  const summary = {
    before: before?.automation_status,
    today: show(today),
    portable: show(admitted),
    after: after.automation_status,
    events: events.length,
    replay: show(replay),
    replay_v2: show(replayV2),
  };
  const event = events[0];
  if (before?.automation_status !== 'enabled' || before.external_user_id !== user
      || show(today) !== CONFLICT
      || show(admitted) !== 'created:draft_only'
      || admitted.rows[0].conversation_id !== before.id
      || after.automation_status !== 'draft_only'
      || BigInt(after.version) !== BigInt(before.version) + 1n
      || events.length !== 1
      || event.actor_type !== 'integration'
      || event.related_message_id !== template
      || event.related_action_id !== actionId
      || event.recovery_case_id !== recoveryCaseId
      || event.data.chatwoot_conversation_id !== String(chatwoot)
      || event.data.scope_key !== ATT1.inboundScope
      || event.data.previous_automation_status !== 'enabled'
      || inboundCase.length !== 1
      || inboundCase[0].case_kind !== 'inbound_sales'
      || inboundCase[0].conversation_id !== before.id
      || inboundCase[0].chatwoot !== String(chatwoot)
      || show(replay) !== 'already_exists:draft_only'
      || replay.rows[0].commercial_case_id !== admitted.rows[0].commercial_case_id
      || show(replayV2) !== 'already_exists:draft_only'
      || afterReplay.version !== after.version
      || eventsAfterReplay.length !== 1) {
    throw new Error(`adoption ${label}: ${JSON.stringify({ ...summary, event, inboundCase })}`);
  }
  adoptions[label] = `${summary.portable}, ${summary.events} event, replay ${summary.replay}`;
  return admitted.rows[0];
};

// ---------------------------------------------------------------------------
// 1a. El primer contacto tras el formulario, con el scope de la instancia
//     (consented_intent_in_cohort): contacto sembrado e inscripto, scope
//     armado, formulario por admit_and_plan_portable_lead_precheckout y la
//     cadena del dispatcher con su reevaluacion y su arranque propios. La
//     identidad del plan es la del formulario (52 + 10).
// ---------------------------------------------------------------------------
const fcLead = mexicanPerson('primer-contacto');
await seedContact(fcLead);
await enroll(fcLead, FC_SCOPE, FC_SCOPE_VERSION);
const fcArmed = one((await db.query(`
  select * from public.set_lancemos_pilot_runtime_state($1,$2,
    (select generation from public.pilot_runtime_controls where scope_key = $1),
    'armed','operator-test','controlled-test')
`, [FC_SCOPE, FC_SCOPE_VERSION])).rows, 'arm the first contact scope');
if (fcArmed.runtime_state !== 'armed') throw new Error('the first contact scope is not armed');
const fcForm = goldenForm(fcLead, FC_DUE);
const fcAdmitted = one((await asService(() => db.query(`
  select * from public.admit_and_plan_portable_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, fcForm.id, JSON.stringify(fcForm.raw),
  JSON.stringify(fcForm.canonical), FC_SCOPE, FC_SCOPE_VERSION]))).rows, 'first contact form');
const fcLedger = one((await db.query(`
  select * from public.portable_precheckout_first_contact_plans where submission_id = $1
`, [fcAdmitted.submission_id])).rows, 'first contact plan row');
if (fcLedger.outcome !== 'planned' || fcLedger.contact_id !== fcLead.contact) {
  throw new Error(`the first contact was not planned: ${JSON.stringify({ outcome: fcLedger.outcome, reason: fcLedger.reason_code })}`);
}
const fcPlan = {
  recovery_case_id: fcLedger.recovery_case_id,
  scheduled_action_id: one((await db.query(`
    select id from public.scheduled_actions where recovery_case_id = $1
  `, [fcLedger.recovery_case_id])).rows, 'first contact action').id,
};
const FC_CHATWOOT = 910001;
await dispatch(fcLead, fcPlan, {
  anchor: 'precheckout_intent', stepKey: 'first_contact', offer: fcLead.offer, chatwoot: FC_CHATWOOT,
});
// Dos identidades (H9): la respuesta desde la otra forma del movil (521), sin
// identidad propia, no adopta: la conversacion es de la identidad del
// formulario y la base da el 23505 de hoy.
await expectBrake('other_form_of_the_mobile', { chatwoot: FC_CHATWOOT, user: fcLead.whatsapp },
  null, OTHER_IDENTITY);
// Una baja que no quedo aplicada a este contacto (decision 5): entro por la
// otra forma del movil (521), que no tiene identidad, y la RPC real la deja
// unmatched. El contacto sigue con su permiso, pero la persona pidio no
// recibir mas mensajes: la portable no adopta, como el arranque del piloto
// (_portable_chatwoot_opt_out_stop), y la v2 da el conflicto de hoy.
await expectBrake('opt_out_unmatched_other_form', { chatwoot: FC_CHATWOOT, user: fcLead.formPhone },
  async () => {
    const optOut = one((await db.query(`
      select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,$6,'stop_receiving_messages')
    `, [ATT1.accountId, ATT1.inboxId, 919001, 999001, fcLead.whatsapp, await dbNow()])).rows,
    'unmatched opt-out');
    const contact = one((await db.query(`
      select contact_permission, lifecycle_status from public.contacts where id = $1
    `, [fcLead.contact])).rows, 'contact after the unmatched opt-out');
    if (optOut.outcome !== 'recorded_unmatched' || optOut.matched_contact_id !== null
        || ['opted_out', 'blocked', 'restricted'].includes(contact.contact_permission)
        || contact.lifecycle_status === 'do_not_contact') {
      throw new Error(`the opt-out of the other form was not left unmatched: ${JSON.stringify({ optOut, contact })}`);
    }
  });
await expectAdoption('first_contact', {
  chatwoot: FC_CHATWOOT, user: fcLead.formPhone,
  recoveryCaseId: fcPlan.recovery_case_id, actionId: fcPlan.scheduled_action_id,
});

// ---------------------------------------------------------------------------
// 1b + 2. El carrito: la plantilla aceptada, cada freno sobre esa
//     conversacion (dentro de una transaccion que se deshace) y despues la
//     adopcion.
// ---------------------------------------------------------------------------
const cartLead = person('carrito');
await admitForm(cartLead, defaultOffer);
const cartEvent = await admitCart(cartLead, defaultOffer);
await createContact(cartLead, cartEvent.eventId);
await enroll(cartLead, SCOPE, SCOPE_VERSION);
const cartPlan = await planCart(cartLead, defaultOffer, cartEvent);
const CART_CHATWOOT = 910002;
await dispatch(cartLead, cartPlan, {
  anchor: 'cart_abandonment', stepKey: 'first_contact', offer: defaultOffer, chatwoot: CART_CHATWOOT,
});
const cartReply = { chatwoot: CART_CHATWOOT, user: cartLead.phone };
const cartConversation = await conversationState(CART_CHATWOOT);
await expectBrake('human_takeover', cartReply, () => db.query(`
  update public.conversations set human_takeover = true where id = $1
`, [cartConversation.id]));
await expectBrake('paused_human', cartReply, () => db.query(`
  update public.conversations set status = 'paused_human' where id = $1
`, [cartConversation.id]));
for (const permission of ['opted_out', 'blocked', 'restricted']) {
  await expectBrake(`contact_${permission}`, cartReply, () => db.query(`
    update public.contacts set contact_permission = $2 where id = $1
  `, [cartLead.contact, permission]));
}
await expectBrake('contact_do_not_contact', cartReply, () => db.query(`
  update public.contacts set lifecycle_status = 'do_not_contact' where id = $1
`, [cartLead.contact]));
// La baja por el camino real, desde otra conversacion de Chatwoot del mismo
// lead (decision 5: no se adopta y la atiende una persona).
await expectBrake('opt_out_in_another_conversation', cartReply, async () => {
  const optOut = one((await db.query(`
    select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,$6,'stop_receiving_messages')
  `, [ATT1.accountId, ATT1.inboxId, 919002, 999002, cartLead.phone, await dbNow()])).rows, 'opt-out');
  if (optOut.outcome !== 'applied') throw new Error(`the opt-out was not applied: ${JSON.stringify(optOut)}`);
});
// Ancla sin binding: el mismo caso sin su fila en pilot_recovery_case_bindings
// (append-only: el trigger se apaga dentro de la transaccion que se deshace).
await expectBrake('anchor_without_pilot_binding', cartReply, async () => {
  await db.exec('alter table public.pilot_recovery_case_bindings disable trigger pilot_recovery_case_bindings_append_only');
  await db.query('delete from public.pilot_recovery_case_bindings where recovery_case_id = $1',
    [cartPlan.recovery_case_id]);
});
// Un paso pendiente del caso: los cuatro estados vivos del constraint.
for (const status of ['pending', 'deferred', 'retryable_failed', 'delivery_unknown']) {
  await expectBrake(`action_${status}`, cartReply, () => db.query(`
    update public.scheduled_actions set status = $2 where id = $1
  `, [cartPlan.scheduled_action_id, status]));
}
// Una conversacion enabled sin ninguna plantilla (sin ancla), de otra persona.
await expectBrake('enabled_without_template', { chatwoot: 920010, user: '120255509901' }, async () => {
  const contact = one((await db.query(`
    insert into public.contacts (phone) values ('120255509901') returning id
  `)).rows, 'contact without template').id;
  const identity = one((await db.query(`
    insert into public.channel_identities
      (contact_id, channel, account_id, external_user_id, external_conversation_id,
       identity_status, metadata)
    values ($1,'whatsapp',$2,'120255509901','920010','active',jsonb_build_object('inbox_id',$3::text))
    returning id
  `, [contact, `chatwoot:${ATT1.accountId}`, String(ATT1.inboxId)])).rows, 'identity without template').id;
  await db.query(`
    insert into public.conversations
      (contact_id, channel_identity_id, status, automation_status, human_takeover, commercial_context)
    values ($1,$2,'active','enabled',false,jsonb_build_object('chatwoot_conversation_id','920010'))
  `, [contact, identity]);
});
// Un tercero que escribe en la conversacion de la plantilla.
await expectBrake('third_party_identity', { chatwoot: CART_CHATWOOT, user: '120255509902' },
  null, OTHER_IDENTITY);
// Cada eslabon de la prueba de la plantilla, roto de a uno sobre la plantilla
// aceptada del carrito. Ninguna RPC deja hoy estos estados (solo la
// aceptacion escribe un mensaje outbound de ai_agent con accepted_message_id,
// y lo escribe con durable_followup sobre el caso de la conversacion): se
// fijan con un update para probar el predicado exacto, no un camino real.
const cartTemplate = await acceptedMessageOf(cartPlan.scheduled_action_id);
// El mensaje no es una plantilla del dispatcher (sin strategy durable_followup).
await expectBrake('template_without_durable_followup', cartReply, () => db.query(`
  update public.messages set semantic_metadata = semantic_metadata - 'strategy' where id = $1
`, [cartTemplate]));
// El intento que apunta al mensaje no quedo aceptado por Chatwoot (el check
// de la tabla exige el mensaje al aceptado, no al reves).
await expectBrake('template_attempt_not_accepted', cartReply, () => db.query(`
  update public.followup_delivery_attempts set outcome = 'rejected' where accepted_message_id = $1
`, [cartTemplate]));
// El caso de la plantilla esta atado a otra conversacion de la misma persona.
await expectBrake('template_case_in_another_conversation', cartReply, async () => {
  const other = one((await db.query(`
    insert into public.conversations
      (contact_id, channel_identity_id, status, automation_status, human_takeover, commercial_context)
    select contact_id, channel_identity_id, 'active', 'draft_only', false,
           jsonb_build_object('chatwoot_conversation_id', '920020')
    from public.conversations where id = $1
    returning id
  `, [cartConversation.id])).rows, 'another conversation of the cart lead').id;
  await db.query('update public.recovery_cases set conversation_id = $2 where id = $1',
    [cartPlan.recovery_case_id, other]);
});
// El caso de la plantilla eligio otra identidad de la misma persona. Con el
// caso atado a esta conversacion, la base no deja llegar a este estado: el
// trigger de sombra copia el cambio a commercial_cases y
// protect_commercial_case_shadow exige que la conversacion del caso sea de su
// identidad elegida (23514 commercial_case_conversation_mismatch). El
// predicado es defensa en profundidad; para probarlo se apaga la copia a la
// sombra dentro de la transaccion que se deshace.
await expectBrake('template_case_with_another_identity', cartReply, async () => {
  await db.exec('alter table public.recovery_cases disable trigger recovery_cases_sync_commercial_case');
  const other = one((await db.query(`
    insert into public.channel_identities
      (contact_id, channel, account_id, external_user_id, external_conversation_id,
       identity_status, metadata)
    values ($1,'whatsapp',$2,'120255509920','920021','active',jsonb_build_object('inbox_id',$3::text))
    returning id
  `, [cartLead.contact, `chatwoot:${ATT1.accountId}`, String(ATT1.inboxId)])).rows,
  'another identity of the cart lead').id;
  await db.query('update public.recovery_cases set selected_channel_identity_id = $2 where id = $1',
    [cartPlan.recovery_case_id, other]);
});
await expectAdoption('cart', {
  chatwoot: CART_CHATWOOT, user: cartLead.phone,
  recoveryCaseId: cartPlan.recovery_case_id, actionId: cartPlan.scheduled_action_id,
});
// Una conversacion ya admitida que volvio a enabled (en Johanna hay
// conversaciones enabled) con la plantilla todavia ahi: como ya tiene fila de
// admision, la portable no la adopta otra vez y da exactamente lo que da la
// v2, sin otro evento ni otra version.
await db.exec('begin');
let readmitted;
try {
  await db.query(`update public.conversations set automation_status = 'enabled' where id = $1`,
    [cartConversation.id]);
  const before = await conversationState(CART_CHATWOOT);
  const v2 = await inSavepoint(V2_SQL, inboundArgs(CART_CHATWOOT, cartLead.phone));
  const portable = await inSavepoint(PORTABLE_SQL, inboundArgs(CART_CHATWOOT, cartLead.phone));
  const after = await conversationState(CART_CHATWOOT);
  readmitted = {
    before, after, v2: show(v2), portable: show(portable),
    portable_status_after: portable.automation_status_after,
    v2_status_after: v2.automation_status_after,
    events: (await adoptionEvents(cartConversation.id)).length,
  };
} finally {
  await db.exec('rollback');
}
brakes.already_admitted_back_to_enabled = readmitted.portable;
if (readmitted.portable !== readmitted.v2
    || readmitted.portable_status_after !== readmitted.v2_status_after
    || readmitted.before.automation_status !== 'enabled'
    || readmitted.after.version !== readmitted.before.version
    || readmitted.events !== 1) {
  throw new Error(`brake already_admitted_back_to_enabled: ${JSON.stringify(readmitted)}`);
}

// ---------------------------------------------------------------------------
// 1c + 5. El pago fallido sin carrito previo. Primero la respuesta llega en
//     otra conversacion de Chatwoot: se crea esa conversacion, igual que con
//     la v2, y la de la plantilla queda enabled y sin evento. Despues la
//     respuesta en la de la plantilla se adopta.
// ---------------------------------------------------------------------------
const failureLead = person('pago-fallido');
const failureOffer = additionalOffers[1];
await admitForm(failureLead, failureOffer);
const failureEvent = await admitFailure(failureLead, failureOffer, 'HPATT1ADOPTPF1');
await createContact(failureLead, failureEvent.eventId);
await enroll(failureLead, SCOPE, SCOPE_VERSION);
const failurePlan = await planFailure(failureLead, failureOffer, failureEvent);
const FAILURE_CHATWOOT = 910003;
const FAILURE_OTHER_CHATWOOT = 910103;
await dispatch(failureLead, failurePlan, {
  anchor: 'payment_failure', stepKey: 'payment_failure_first_contact', offer: failureOffer,
  chatwoot: FAILURE_CHATWOOT,
});
await db.exec('begin');
const otherByV2 = await inSavepoint(V2_SQL, inboundArgs(FAILURE_OTHER_CHATWOOT, failureLead.phone));
await db.exec('rollback');
const otherByPortable = await asService(() => tryRows(PORTABLE_SQL,
  inboundArgs(FAILURE_OTHER_CHATWOOT, failureLead.phone)));
const otherConversation = await conversationState(FAILURE_OTHER_CHATWOOT);
const templateConversation = await conversationState(FAILURE_CHATWOOT);
const otherEvents = await allAdoptionEvents();
if (show(otherByV2) !== 'created:draft_only'
    || show(otherByPortable) !== 'created:draft_only'
    || otherByPortable.rows[0].conversation_id !== otherConversation?.id
    || otherByPortable.rows[0].contact_id !== failureLead.contact
    || otherConversation.automation_status !== 'draft_only'
    || otherConversation.external_user_id !== failureLead.phone
    || templateConversation.automation_status !== 'enabled'
    || (await adoptionEvents(templateConversation.id)).length !== 0
    || (await adoptionEvents(otherConversation.id)).length !== 0
    || otherEvents !== 2) {
  throw new Error(`the reply in another conversation: ${JSON.stringify({ v2: show(otherByV2), portable: show(otherByPortable), other: otherConversation, template: templateConversation, events: otherEvents })}`);
}
await expectAdoption('payment_failure', {
  chatwoot: FAILURE_CHATWOOT, user: failureLead.phone,
  recoveryCaseId: failurePlan.recovery_case_id, actionId: failurePlan.scheduled_action_id,
});

// ---------------------------------------------------------------------------
// 3. El paso pendiente de otro caso de la misma persona, por las RPC reales:
//    el carrito se acepta, el pago fallido se planifica (pending, sin
//    conversacion) y despues queda en delivery_unknown. Mientras tanto la
//    respuesta a la plantilla del carrito no se adopta. La reconciliacion
//    acepta ese intento en la misma conversacion y la respuesta se adopta con
//    la plantilla mas nueva.
// ---------------------------------------------------------------------------
const bothLead = person('carrito-y-pago-fallido');
const bothOffer = additionalOffers[0];
await admitForm(bothLead, bothOffer);
const bothCart = await admitCart(bothLead, bothOffer);
await createContact(bothLead, bothCart.eventId);
await enroll(bothLead, SCOPE, SCOPE_VERSION);
const bothCartPlan = await planCart(bothLead, bothOffer, bothCart);
const BOTH_CHATWOOT = 910004;
await dispatch(bothLead, bothCartPlan, {
  anchor: 'cart_abandonment', stepKey: 'first_contact', offer: bothOffer, chatwoot: BOTH_CHATWOOT,
});
const bothFailure = await admitFailure(bothLead, bothOffer, 'HPATT1ADOPTPF2');
const bothFailurePlan = await planFailure(bothLead, bothOffer, bothFailure);
const bothReply = { chatwoot: BOTH_CHATWOOT, user: bothLead.phone };
const pendingFailure = one((await db.query(`
  select action.status, recovery.conversation_id
  from public.scheduled_actions action
  join public.recovery_cases recovery on recovery.id = action.recovery_case_id
  where action.id = $1
`, [bothFailurePlan.scheduled_action_id])).rows, 'pending payment failure');
if (!bothFailurePlan.created || pendingFailure.status !== 'pending' || pendingFailure.conversation_id !== null) {
  throw new Error(`the payment failure after the cart is not pending: ${JSON.stringify(pendingFailure)}`);
}
await expectBrake('other_case_pending', bothReply, null);
const bothDelivery = await dispatch(bothLead, bothFailurePlan, {
  anchor: 'payment_failure', stepKey: 'payment_failure_first_contact', offer: bothOffer,
  finish: 'delivery_unknown',
});
await expectBrake('other_case_delivery_unknown', bothReply, null);
await accept(bothLead, bothDelivery, BOTH_CHATWOOT);
const reconciled = one((await db.query(`
  select attempt.reconciliation_resolution, action.status, recovery.conversation_id
  from public.followup_delivery_attempts attempt
  join public.scheduled_actions action on action.id = attempt.action_id
  join public.recovery_cases recovery on recovery.id = action.recovery_case_id
  where attempt.id = $1
`, [bothDelivery.attemptId])).rows, 'reconciled payment failure');
const bothConversation = await conversationState(BOTH_CHATWOOT);
if (reconciled.reconciliation_resolution !== 'accepted_by_chatwoot'
    || reconciled.status !== 'accepted_by_chatwoot'
    || reconciled.conversation_id !== bothConversation.id) {
  throw new Error(`the reconciliation did not accept the payment failure in the cart conversation: ${JSON.stringify(reconciled)}`);
}
await expectAdoption('cart_then_reconciled_payment_failure', {
  chatwoot: BOTH_CHATWOOT, user: bothLead.phone,
  recoveryCaseId: bothFailurePlan.recovery_case_id, actionId: bothFailurePlan.scheduled_action_id,
});

// ---------------------------------------------------------------------------
// 3b. El primer contacto que ya no va a salir no es un paso pendiente
//     (20261009000200). El caso de ATT1 del 2026-10-09: el formulario
//     planifica el primer contacto a los 60 minutos, el carrito llega antes
//     (a los 15 de la prueba; 41 a 52 en ATT1), su plantilla se acepta y la
//     persona contesta con el primer contacto todavia en pending. Con la
//     cadena de 20261001000400 esa respuesta daba el 22000 hasta que el
//     despachador tomaba el primer contacto a su hora; ahora se adopta. Sin la
//     clasificacion del carrito (el primer contacto todavia va a salir) sigue
//     frenando. Y a su hora la reevaluacion real lo cancela igual
//     (superseded_by_provider_event), sin intento.
// ---------------------------------------------------------------------------
const stopLead = mexicanPerson('primer-contacto-reemplazado');
await seedContact(stopLead);
await enroll(stopLead, FC_SCOPE, FC_SCOPE_VERSION);
await enroll(stopLead, SCOPE, SCOPE_VERSION);
const stopForm = goldenForm(stopLead, SUBMITTED_AT);
const stopAdmitted = one((await asService(() => db.query(`
  select * from public.admit_and_plan_portable_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, stopForm.id, JSON.stringify(stopForm.raw),
  JSON.stringify(stopForm.canonical), FC_SCOPE, FC_SCOPE_VERSION]))).rows, 'replaced first contact form');
const stopLedger = one((await db.query(`
  select * from public.portable_precheckout_first_contact_plans where submission_id = $1
`, [stopAdmitted.submission_id])).rows, 'replaced first contact plan row');
if (stopLedger.outcome !== 'planned' || stopLedger.contact_id !== stopLead.contact) {
  throw new Error(`the first contact to replace was not planned: ${JSON.stringify({ outcome: stopLedger.outcome, reason: stopLedger.reason_code })}`);
}
const stopFirstContactAction = one((await db.query(`
  select id from public.scheduled_actions where recovery_case_id = $1
`, [stopLedger.recovery_case_id])).rows, 'replaced first contact action').id;
const stopCart = await admitCart(stopLead, stopLead.offer);
const stopCartPlan = await planCart(stopLead, stopLead.offer, stopCart);
const STOP_CHATWOOT = 910005;
await dispatch(stopLead, stopCartPlan, {
  anchor: 'cart_abandonment', stepKey: 'first_contact', offer: stopLead.offer, chatwoot: STOP_CHATWOOT,
});
const stopReply = { chatwoot: STOP_CHATWOOT, user: stopLead.phone };
const firstContactState = async () => one((await db.query(`
  select action.status, action.terminal_reason, action.due_at, recovery.conversation_id,
         intent.id as intent_id, intent.current_classification,
         public._portable_precheckout_stop_reason(intent.id, recovery.contact_id) as stop_reason,
         (select count(*)::integer from public.followup_delivery_attempts attempt
           where attempt.action_id = action.id) as attempts
  from public.scheduled_actions action
  join public.recovery_cases recovery on recovery.id = action.recovery_case_id
  join public.pilot_recovery_case_bindings binding on binding.recovery_case_id = recovery.id
  join public.purchase_intents intent on intent.id = binding.audience_purchase_intent_id
  where action.id = $1
`, [stopFirstContactAction])).rows, 'replaced first contact state');
const replaced = await firstContactState();
if (replaced.status !== 'pending' || replaced.conversation_id !== null || replaced.attempts !== 0
    || replaced.current_classification !== 'confirmed_abandonment'
    || replaced.stop_reason !== 'superseded_by_provider_event'
    || !(new Date(replaced.due_at) > await dbNow())) {
  throw new Error(`the first contact is not pending and replaced by the cart: ${JSON.stringify(replaced)}`);
}
// Sin la clasificacion (dentro de la transaccion que se deshace) el primer
// contacto todavia va a salir: el caso del carrito ya esta agotado, asi que
// ningun freno lo cancela, y la decision 6 sigue frenando la adopcion.
await expectBrake('first_contact_still_going_out', stopReply, () => db.query(`
  update public.purchase_intents set current_classification = null where id = $1
`, [replaced.intent_id]));
await expectAdoption('cart_with_replaced_first_contact', {
  chatwoot: STOP_CHATWOOT, user: stopLead.phone,
  recoveryCaseId: stopCartPlan.recovery_case_id, actionId: stopCartPlan.scheduled_action_id,
});
// A su hora, el despachador lo toma y la reevaluacion real lo cancela: la
// conversacion adoptada no recibe el primer contacto.
const stopWorker = 'att1-adoption-first-contact-due';
const dueNow = new Date(new Date(replaced.due_at).getTime() + 60_000);
const claimedAtDue = (await db.query(`
  select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
`, [stopWorker, dueNow])).rows;
if (claimedAtDue.length !== 1 || claimedAtDue[0].id !== stopFirstContactAction) {
  throw new Error(`claimed at the first contact hour: ${JSON.stringify(claimedAtDue.map((row) => [row.id, row.anchor_type]))}`);
}
const atDue = one((await asService(() => db.query(`
  select * from public.reevaluate_portable_precheckout_action($1,$2,$3,$4)
`, [stopFirstContactAction, stopWorker, claimedAtDue[0].lease_generation, dueNow]))).rows,
'first contact reevaluation at its hour');
const cancelled = await firstContactState();
const stillAdopted = await conversationState(STOP_CHATWOOT);
if (atDue.decision !== 'cancel' || atDue.reason_code !== 'superseded_by_provider_event'
    || cancelled.status !== 'cancelled' || cancelled.attempts !== 0
    || stillAdopted.automation_status !== 'draft_only') {
  throw new Error(`the replaced first contact was not cancelled at its hour: ${JSON.stringify({ atDue, cancelled, conversation: stillAdopted.automation_status })}`);
}
const replacedFirstContact = {
  adopted_with_first_contact: `${replaced.status}/${replaced.stop_reason}`,
  at_its_hour: `${atDue.decision}/${atDue.reason_code}`,
  attempts: cancelled.attempts,
  conversation: stillAdopted.automation_status,
};

// ---------------------------------------------------------------------------
// 4. La forma de Johanna, sin fila previa: la portable da created igual que la
//    v2 y deja las mismas filas, sin evento de adopcion.
// ---------------------------------------------------------------------------
const johannaShape = async (sql, chatwoot, user) => {
  const admitted = await asService(() => tryRows(sql, inboundArgs(chatwoot, user)));
  if (!admitted.ok) return { result: show(admitted) };
  const row = admitted.rows[0];
  const persisted = one((await db.query(`
    select contact.metadata as contact_metadata,
           identity.channel, identity.account_id, identity.external_user_id,
           identity.external_conversation_id, identity.identity_status,
           identity.metadata as identity_metadata,
           conversation.status, conversation.automation_status,
           conversation.human_takeover, conversation.version::text as version,
           conversation.commercial_context,
           commercial_case.case_kind, commercial_case.status as case_status,
           commercial_case.automation_status as case_automation_status,
           commercial_case.inbound_scope_key, commercial_case.offer_ref,
           (select count(*)::integer from public.conversation_events event
             where event.conversation_id = conversation.id) as events
    from public.commercial_cases commercial_case
    join public.contacts contact on contact.id = commercial_case.contact_id
    join public.channel_identities identity on identity.id = commercial_case.selected_channel_identity_id
    join public.conversations conversation on conversation.id = commercial_case.conversation_id
    where commercial_case.id = $1
      and contact.id = $2 and identity.id = $3 and conversation.id = $4
  `, [row.commercial_case_id, row.contact_id, row.channel_identity_id, row.conversation_id])).rows,
  `johanna shape ${chatwoot}`);
  // Lo que depende del entrante (su numero y su conversacion) se normaliza.
  persisted.external_user_id = persisted.external_user_id === user ? 'USER' : persisted.external_user_id;
  persisted.external_conversation_id = persisted.external_conversation_id === String(chatwoot)
    ? 'CHATWOOT' : persisted.external_conversation_id;
  persisted.commercial_context = persisted.commercial_context.chatwoot_conversation_id === String(chatwoot)
    ? 'CHATWOOT' : persisted.commercial_context;
  return { result: show(admitted), persisted };
};
const johannaPortable = await johannaShape(PORTABLE_SQL, 920001, '5215550001001');
const johannaV2 = await johannaShape(V2_SQL, 920002, '5215550001002');
if (johannaPortable.result !== 'created:draft_only'
    || JSON.stringify(johannaPortable) !== JSON.stringify(johannaV2)
    || johannaPortable.persisted.events !== 0) {
  throw new Error(`the Johanna shape: ${JSON.stringify({ johannaPortable, johannaV2 })}`);
}
// El replay cruzado: la portable sobre lo que creo la v2 da already_exists.
const crossReplay = await asService(() => tryRows(PORTABLE_SQL, inboundArgs(920002, '5215550001002')));
if (show(crossReplay) !== 'already_exists:draft_only') {
  throw new Error(`the portable replay over a v2 admission: ${show(crossReplay)}`);
}

// ---------------------------------------------------------------------------
// 6. El ACL: definer con search_path fijo, solo service_role la ejecuta.
// ---------------------------------------------------------------------------
const shape = one((await db.query(`
  select p.prosecdef as security_definer, p.proconfig as config,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute') as anon_x,
         has_function_privilege('authenticated', p.oid, 'execute') as auth_x,
         has_function_privilege('public', p.oid, 'execute') as public_x
  from pg_proc p
  where p.oid = to_regprocedure($1)
`, [PORTABLE_SIGNATURE])).rows, 'the portable admission');
if (shape.security_definer !== true
    || JSON.stringify(shape.config) !== JSON.stringify(['search_path=pg_catalog, public, pg_temp'])
    || shape.service_x !== true || shape.anon_x !== false
    || shape.auth_x !== false || shape.public_x !== false) {
  throw new Error(`the portable admission ACL: ${JSON.stringify(shape)}`);
}
const denied = {};
for (const role of ['anon', 'authenticated']) {
  await db.exec(`set role ${role}`);
  let caught = null;
  try {
    await db.query(PORTABLE_SQL, inboundArgs(930001, '120255509903'));
  } catch (error) {
    caught = error;
  } finally {
    await db.exec('reset role');
  }
  if (caught?.code !== '42501') {
    throw new Error(`${role} executed the portable admission: ${caught?.code} ${caught?.message}`);
  }
  denied[role] = caught.code;
}

const totalEvents = await allAdoptionEvents();
if (totalEvents !== 5) throw new Error(`expected five adoption events, got ${totalEvents}`);
console.log(JSON.stringify({
  portable_inbound_template_adoption: 'OK',
  adoptions,
  brakes,
  replaced_first_contact: replacedFirstContact,
  reply_in_another_conversation: `${show(otherByPortable)} (v2 ${show(otherByV2)}), template conversation stays enabled`,
  johanna_shape: `${johannaPortable.result} (v2 ${johannaV2.result}), cross replay ${show(crossReplay)}`,
  acl: { service_role: 'execute', ...denied },
  adoption_events: totalEvents,
}));
await db.close();
