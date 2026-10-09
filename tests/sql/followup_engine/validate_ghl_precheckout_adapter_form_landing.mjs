// La landing del formulario contra la admision REAL (1.4.0, contrato
// docs/contracts/ghl-precheckout-adapter-v1.md, "La landing de un formulario
// declarado" y prueba de conformidad 5).
//
// GHL manda en attributionSource.url la pagina donde empezo la visita. Medido en
// ATT1 el 2026-10-08: el formulario de la landing B (UHTa) llego con la URL de la
// -d de quien entro por un anuncio, y el adaptador le dio la oferta de la -d. Con
// landing_por_formulario en activo, el adaptador le da la de la B. Este validador
// admite ese evento (el golden del traductor) con la RPC real
// admit_portable_observed_lead_precheckout, sobre la cadena completa de
// migraciones, y afirma el EFECTO en la base:
//
//   1. el envio de UHTa traducido en activo entra: inserted, con la intencion en
//      alimenta-tu-tiroides / bmaztyhg, whatsapp_contact_authorized y
//      activation_authorized, y _portable_consented_intent_reason la da
//      consented_intent_ok con ese envio. La atribucion es la de la URL de la
//      visita: el sck guardado es el del anuncio de la -d;
//   2. la misma persona con el envio de la -d (el golden de siempre) deja otra
//      intencion, en la -d con 2uafw5bg: cada oferta tiene la suya, y las dos
//      quedan elegibles.
//
// Datos: los de validate_ghl_precheckout_adapter.mjs (la preparacion esta
// copiada de ahi), con la oferta bmaztyhg reatada a la landing B como en la
// instancia. El golden sale de la captura del editor de la -d con el mediumId de
// UHTa (ver su _golden).
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

// ---------------------------------------------------------------------------
// El manifiesto de ATT1 (lector copiado de validate_att1_portable_chain.mjs):
// tablas, arrays de tablas y claves con texto, entero o booleano.
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
// ATT1 como esta hoy: bmaztyhg reatada de org-a a la landing B (instancia#26,
// 2026-10-07, despliegue/base/reatar-bmaztyhg-a-la-landing-b.sql de la
// instancia). El fixture del producto todavia la tiene en org-a.
const LANDING_B = {
  site: 'metodoraizana-mx',
  landing_id: 'alimenta-tu-tiroides',
  url: 'https://site.metodoraizana.com.mx/alimenta-tu-tiroides',
};
let rebound = 0;
for (const offer of manifest.hotmart?.ofertas ?? []) {
  if (offer.codigo === 'bmaztyhg') {
    Object.assign(offer, LANDING_B);
    rebound += 1;
  }
}
if (rebound !== 1) throw new Error(`el fixture de ATT1 no trae una sola bmaztyhg: ${rebound}`);
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
if (offers.length !== 3 || offers.filter((offer) => offer.default).length !== 1 || !offers[0].default) {
  throw new Error(`ATT1 declara tres ofertas y la primera es la por defecto: ${JSON.stringify(offers)}`);
}
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
const landingOf = (offer) => ({
  offer_code: offer.offer_code, site: offer.site, landing_id: offer.landing_id,
  page_host: offer.page_host, page_path: offer.page_path,
});

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

// ---------------------------------------------------------------------------
// Los goldens del traductor y la segunda entrega.
// ---------------------------------------------------------------------------
const golden = (name) => {
  const file = JSON.parse(readFileSync(join(root, 'tests/fixtures/ghl/expected', name), 'utf8'));
  if (!file.raw_payload || !file.canonical_payload
      || file.raw_payload.id !== file.canonical_payload.external_submission_id) {
    throw new Error(`${name} is not a translator golden`);
  }
  return { raw: file.raw_payload, canonical: file.canonical_payload };
};
const UHTA_ACTIVE = golden(
  'ghl_form_webhook_uhta_from_landing_d_active_derived_20260929.lead_precheckout.json',
);
const LANDING_D = golden('ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json');


const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};
const admit = async (event, label) => one((await db.query(`
  select * from public.admit_portable_observed_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, event.raw.id,
  JSON.stringify(event.raw), JSON.stringify(event.canonical)])).rows, label);
const intentOf = async (id, label) => one((await db.query(`
  select landing_ref, offer_ref, normalized_phone, submitted_at, lifecycle_state,
         current_classification, whatsapp_contact_authorized, activation_authorized
  from public.purchase_intents where id = $1
`, [id])).rows, label);
const totalConflicts = async () => one((await db.query(`
  select count(*)::integer as count from public.precheckout_submission_conflicts
`)).rows, 'conflict count').count;

// El contacto que deja resolve_event con el evento de Hotmart: el
// contact_point del telefono es lo que _portable_consented_intent_reason cruza
// contra la intencion.
const contactFor = async (event) => {
  const { email, phone } = event.canonical.identity;
  const contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone, country_iso)
    values ($1,$2,$3,$4) returning id
  `, [event.canonical.lead.full_name, email, phone,
    event.canonical.identity.phone_country_iso])).rows, `${email} contact`).id;
  await db.query(`
    insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1,'email',$2,$2,'system'), ($1,'phone',$3,$3,'system')
  `, [contact, email, phone]);
  return contact;
};
const consentReason = async (intentId, contactId, phone) => one((await db.query(`
  select * from public._portable_consented_intent_reason($1,$2,$3)
`, [intentId, contactId, phone])).rows, 'consented intent reason');


// ---------------------------------------------------------------------------
// 1. El envio de UHTa traducido en activo entra en la landing B.
// ---------------------------------------------------------------------------
const uhta = await admit(UHTA_ACTIVE, 'uhta activo');
const uhtaIntent = await intentOf(uhta.purchase_intent_id, 'uhta activo intent');
const uhtaSck = one((await db.query(`
  select raw_payload -> 'data' -> 'attribution' ->> 'sck' as sck
  from public.precheckout_submissions where id = $1
`, [uhta.submission_id])).rows, 'uhta activo submission').sck;
if (uhta.outcome !== 'inserted'
    || uhtaIntent.landing_ref !== 'alimenta-tu-tiroides'
    || uhtaIntent.offer_ref !== 'bmaztyhg'
    || uhtaIntent.whatsapp_contact_authorized !== true
    || uhtaIntent.activation_authorized !== true
    || uhtaSck !== UHTA_ACTIVE.raw.data.attribution.sck
    || uhtaSck !== LANDING_D.raw.data.attribution.sck) {
  throw new Error(`the active UHTa did not leave the landing B intent: ${JSON.stringify({ uhta, uhtaIntent, uhtaSck })}`);
}
const contact = await contactFor(UHTA_ACTIVE);
const uhtaReason = await consentReason(
  uhta.purchase_intent_id, contact, UHTA_ACTIVE.canonical.identity.phone,
);
if (uhtaReason.reason_code !== 'consented_intent_ok'
    || uhtaReason.precheckout_submission_id !== uhta.submission_id) {
  throw new Error(`the active UHTa is not a consented intent: ${JSON.stringify(uhtaReason)}`);
}

// ---------------------------------------------------------------------------
// 2. La misma persona por la -d deja su propia intencion.
// ---------------------------------------------------------------------------
const landingD = await admit(LANDING_D, 'landing -d');
const landingDIntent = await intentOf(landingD.purchase_intent_id, 'landing -d intent');
const landingDReason = await consentReason(
  landingD.purchase_intent_id, contact, LANDING_D.canonical.identity.phone,
);
const uhtaReasonAfter = await consentReason(
  uhta.purchase_intent_id, contact, UHTA_ACTIVE.canonical.identity.phone,
);
if (landingD.outcome !== 'inserted'
    || landingD.purchase_intent_id === uhta.purchase_intent_id
    || landingDIntent.landing_ref !== 'alimenta-tu-tiroides-d'
    || landingDIntent.offer_ref !== '2uafw5bg'
    || landingDReason.reason_code !== 'consented_intent_ok'
    || uhtaReasonAfter.reason_code !== 'consented_intent_ok'
    || (await totalConflicts()) !== 0) {
  throw new Error(`the -d of the same person did not leave its own intent: ${JSON.stringify({ landingD, landingDIntent, landingDReason, uhtaReasonAfter })}`);
}

console.log(JSON.stringify({
  ghl_precheckout_adapter_form_landing: 'OK',
  uhta_active: {
    outcome: uhta.outcome,
    intent: `${uhtaIntent.landing_ref}/${uhtaIntent.offer_ref}`,
    consent: uhtaReason.reason_code,
    sck: uhtaSck,
  },
  same_person_landing_d: {
    outcome: landingD.outcome,
    intent: `${landingDIntent.landing_ref}/${landingDIntent.offer_ref}`,
    consent: landingDReason.reason_code,
  },
}));
await db.close();
