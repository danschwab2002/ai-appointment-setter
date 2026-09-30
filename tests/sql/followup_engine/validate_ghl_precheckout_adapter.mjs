// El adaptador de GHL contra la admision REAL (contrato
// docs/contracts/ghl-precheckout-adapter-v1.md, prueba de conformidad 5).
//
// Admite, sobre la cadena completa de migraciones y con la RPC real
// admit_portable_observed_lead_precheckout, los eventos que produjo el
// traductor para los dos envios capturados de GHL (los goldens de
// tests/fixtures/ghl/expected/), y afirma el EFECTO en la base, no solo el
// outcome:
//
//   1. ads_a entra: inserted, con la intencion en la landing ads-a, la oferta
//      gopi6lh7 y whatsapp_contact_authorized, y _portable_consented_intent_reason
//      (el permiso del pago fallido y la audiencia consented_intent) la da
//      consented_intent_ok con ese envio;
//   2. una segunda entrega del mismo envio (lo que hace GHL al reintentar): el
//      adaptador saca otro id (ULID aleatorio) y otro created_at, y la RPC la
//      enlaza a la MISMA intencion: dos submissions enlazadas (ordinal 1 y 2),
//      cero filas en precheckout_submission_conflicts, y la intencion sigue
//      siendo elegible (consented_intent_ok, ahora con la segunda submission).
//      Ademas fija el riesgo E10: la intencion reusada conserva el submitted_at
//      de la primera entrega;
//   3. el -d derivado entra con la oferta 2uafw5bg y la landing
//      alimenta-tu-tiroides-d, y el sck que queda guardado en raw_payload es
//      el del golden (y calza el alfabeto de emision);
//   4. caso de control, POR QUE EL ID NO PUEDE SER DETERMINISTA: una segunda
//      entrega con el MISMO id y otro created_at (lo que produciria un id
//      derivado del envio, porque created_at es el reloj del bridge) da
//      semantic_conflict, deja una fila sin resolver en
//      precheckout_submission_conflicts y la intencion deja de ser elegible
//      (consented_intent_submission_missing). Nada pone resolved_at: un
//      reintento de GHL la envenenaria para siempre.
//
// Datos:
//   - Binding, ofertas y consentimiento salen de
//     tests/fixtures/instances/att1/instancia.toml (se lee el archivo), con la
//     misma preparacion que validate_att1_portable_chain.mjs.
//   - Los eventos son los goldens del traductor (tests/fixtures/ghl/expected/),
//     que salen de los dos envios capturados en tests/fixtures/ghl/. La segunda
//     entrega cambia solo lo que el traductor cambia entre dos traducciones del
//     mismo cuerpo: el id (otro ULID) y created_at / submitted_at. Todo lo demas
//     es el golden tal cual.
//   - El contacto que pide _portable_consented_intent_reason (el que dejaria
//     resolve_event con el evento de Hotmart) se inserta a mano con el telefono
//     normalizado de la intencion, como en validate_att1_portable_chain.mjs.
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
const ADS_A = golden('ghl_form_webhook_att1_ads_a_20260929.lead_precheckout.json');
const LANDING_D = golden('ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json');

// ULID como el de generate_issuance_ulid (checkout_issuance.py): 48 bits de
// milisegundos y 80 bits aleatorios, en base32 de Crockford.
const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
const ULID_PATTERN = /^[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
const ulidAt = (ms) => {
  let value = (BigInt(ms) << 80n) | BigInt(`0x${randomBytes(10).toString('hex')}`);
  let out = '';
  for (let i = 0; i < 26; i += 1) {
    out = CROCKFORD[Number(value & 31n)] + out;
    value >>= 5n;
  }
  return out;
};

// Lo unico que cambia entre dos traducciones del mismo cuerpo: el id y la hora
// del bridge (created_at en el evento, submitted_at en el canonico, que el
// parser escribe sin milisegundos cuando son cero). keepId fija el id, que es
// lo que haria un id determinista.
const redeliver = (event, { afterSeconds, keepId = false }) => {
  const raw = structuredClone(event.raw);
  const canonical = structuredClone(event.canonical);
  const at = new Date(Date.parse(raw.created_at) + afterSeconds * 1000);
  const id = keepId ? raw.id : ulidAt(at.getTime());
  raw.id = id;
  raw.created_at = at.toISOString();
  canonical.external_submission_id = id;
  canonical.submitted_at = at.toISOString().replace('.000Z', 'Z');
  return { raw, canonical };
};

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
const linksOf = async (intentId) => (await db.query(`
  select link.ordinal, submission.id, submission.external_submission_id
  from public.purchase_intent_submissions link
  join public.precheckout_submissions submission on submission.id = link.submission_id
  where link.purchase_intent_id = $1
  order by link.ordinal
`, [intentId])).rows;
const conflictsOf = async (intentId) => (await db.query(`
  select conflict.external_submission_id, conflict.resolved_at
  from public.precheckout_submission_conflicts conflict
  join public.purchase_intent_submissions link on link.submission_id = conflict.existing_submission_id
  where link.purchase_intent_id = $1
`, [intentId])).rows;
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
// 1. ads_a entra y queda elegible.
// ---------------------------------------------------------------------------
const adsA = await admit(ADS_A, 'ads_a');
const adsAIntent = await intentOf(adsA.purchase_intent_id, 'ads_a intent');
if (adsA.outcome !== 'inserted'
    || adsAIntent.landing_ref !== 'ads-a'
    || adsAIntent.offer_ref !== 'gopi6lh7'
    || adsAIntent.normalized_phone !== ADS_A.canonical.identity.phone
    || adsAIntent.lifecycle_state !== 'waiting_for_purchase'
    || adsAIntent.current_classification !== null
    || adsAIntent.whatsapp_contact_authorized !== true
    || adsAIntent.activation_authorized !== true) {
  throw new Error(`ads_a did not leave the expected intent: ${JSON.stringify({ adsA, adsAIntent })}`);
}
const adsAContact = await contactFor(ADS_A);
const adsAFirstReason = await consentReason(
  adsA.purchase_intent_id, adsAContact, ADS_A.canonical.identity.phone,
);
if (adsAFirstReason.reason_code !== 'consented_intent_ok'
    || adsAFirstReason.precheckout_submission_id !== adsA.submission_id) {
  throw new Error(`ads_a is not a consented intent: ${JSON.stringify(adsAFirstReason)}`);
}

// ---------------------------------------------------------------------------
// 2. La segunda entrega del mismo envio (reintento de GHL): otro id y otro
//    created_at. Misma intencion, dos submissions, cero conflictos, elegible.
// ---------------------------------------------------------------------------
const adsARetryEvent = redeliver(ADS_A, { afterSeconds: 90 });
if (!ULID_PATTERN.test(adsARetryEvent.raw.id) || adsARetryEvent.raw.id === ADS_A.raw.id) {
  throw new Error(`the second delivery needs another valid ULID: ${adsARetryEvent.raw.id}`);
}
const adsARetry = await admit(adsARetryEvent, 'ads_a second delivery');
const adsALinks = await linksOf(adsA.purchase_intent_id);
const adsAConflicts = await conflictsOf(adsA.purchase_intent_id);
const adsAIntentAfter = await intentOf(adsA.purchase_intent_id, 'ads_a intent after retry');
const adsARetryReason = await consentReason(
  adsA.purchase_intent_id, adsAContact, ADS_A.canonical.identity.phone,
);
if (adsARetry.outcome !== 'inserted'
    || adsARetry.purchase_intent_id !== adsA.purchase_intent_id
    || adsARetry.submission_id === adsA.submission_id
    || adsALinks.map((link) => `${link.ordinal}:${link.external_submission_id}`).join(',')
      !== `1:${ADS_A.raw.id},2:${adsARetryEvent.raw.id}`
    || adsAConflicts.length !== 0
    || await totalConflicts() !== 0
    || adsAIntentAfter.whatsapp_contact_authorized !== true
    || adsAIntentAfter.activation_authorized !== true
    || adsAIntentAfter.current_classification !== null) {
  throw new Error(`the second delivery of ads_a was not linked to the same intent without conflicts: ${JSON.stringify({ adsA, adsARetry, adsALinks, adsAConflicts, adsAIntentAfter })}`);
}
if (adsARetryReason.reason_code !== 'consented_intent_ok'
    || adsARetryReason.precheckout_submission_id !== adsARetry.submission_id) {
  throw new Error(`ads_a stopped being a consented intent after a retry: ${JSON.stringify(adsARetryReason)}`);
}
// Riesgo E10 del contrato: la intencion reusada conserva el submitted_at de la
// primera entrega (la correlacion filtra por esa fecha).
if (new Date(adsAIntentAfter.submitted_at).toISOString() !== ADS_A.raw.created_at) {
  throw new Error(`the reused intent changed its submitted_at: ${JSON.stringify(adsAIntentAfter)}`);
}

// ---------------------------------------------------------------------------
// 3. El -d derivado: la oferta mexicana, con su sck guardado tal cual.
// ---------------------------------------------------------------------------
const landingD = await admit(LANDING_D, 'landing -d');
const landingDIntent = await intentOf(landingD.purchase_intent_id, 'landing -d intent');
const stored = one((await db.query(`
  select raw_payload, canonical_payload from public.precheckout_submissions where id = $1
`, [landingD.submission_id])).rows, 'landing -d submission');
const storedSck = stored.raw_payload.data.attribution.sck;
const storedAdsASck = one((await db.query(`
  select raw_payload #>> '{data,attribution,sck}' as sck
  from public.precheckout_submissions where id = $1
`, [adsA.submission_id])).rows, 'ads_a submission').sck;
if (landingD.outcome !== 'inserted'
    || landingD.purchase_intent_id === adsA.purchase_intent_id
    || landingDIntent.landing_ref !== 'alimenta-tu-tiroides-d'
    || landingDIntent.offer_ref !== '2uafw5bg'
    || landingDIntent.whatsapp_contact_authorized !== true
    || landingDIntent.activation_authorized !== true
    || storedSck !== LANDING_D.raw.data.attribution.sck
    || !/^[A-Za-z0-9._|~-]{1,255}$/.test(storedSck)
    || stored.raw_payload.data.attribution.utm_campaign !== LANDING_D.raw.data.attribution.utm_campaign
    || storedAdsASck !== ADS_A.raw.data.attribution.sck) {
  throw new Error(`the derived -d did not leave the expected intent and sck: ${JSON.stringify({ landingD, landingDIntent, storedSck, storedAdsASck })}`);
}
const landingDContact = await contactFor(LANDING_D);
const landingDReason = await consentReason(
  landingD.purchase_intent_id, landingDContact, LANDING_D.canonical.identity.phone,
);
if (landingDReason.reason_code !== 'consented_intent_ok'
    || landingDReason.precheckout_submission_id !== landingD.submission_id) {
  throw new Error(`the derived -d is not a consented intent: ${JSON.stringify(landingDReason)}`);
}

// ---------------------------------------------------------------------------
// 4. Control: el MISMO id con otro created_at, lo que daria un id
//    determinista. Conflicto sin resolver y la intencion deja de ser elegible.
// ---------------------------------------------------------------------------
const sameIdEvent = redeliver(LANDING_D, { afterSeconds: 90, keepId: true });
const sameId = await admit(sameIdEvent, 'landing -d same id');
const landingDLinks = await linksOf(landingD.purchase_intent_id);
const landingDConflicts = await conflictsOf(landingD.purchase_intent_id);
const poisonedReason = await consentReason(
  landingD.purchase_intent_id, landingDContact, LANDING_D.canonical.identity.phone,
);
if (sameId.outcome !== 'semantic_conflict'
    || sameId.purchase_intent_id !== landingD.purchase_intent_id
    || sameId.submission_id !== landingD.submission_id
    || landingDLinks.length !== 1
    || landingDConflicts.length !== 1
    || landingDConflicts[0].external_submission_id !== LANDING_D.raw.id
    || landingDConflicts[0].resolved_at !== null
    || await totalConflicts() !== 1) {
  throw new Error(`the same id with another created_at did not leave one open conflict: ${JSON.stringify({ sameId, landingDLinks, landingDConflicts })}`);
}
if (poisonedReason.reason_code !== 'consented_intent_submission_missing'
    || poisonedReason.precheckout_submission_id !== null) {
  throw new Error(`an open conflict did not remove the consent: ${JSON.stringify(poisonedReason)}`);
}
// La base de ads_a no se toco con el conflicto del -d.
const adsAFinalReason = await consentReason(
  adsA.purchase_intent_id, adsAContact, ADS_A.canonical.identity.phone,
);
if (adsAFinalReason.reason_code !== 'consented_intent_ok') {
  throw new Error(`the -d conflict leaked into ads_a: ${JSON.stringify(adsAFinalReason)}`);
}

console.log(JSON.stringify({
  ghl_precheckout_adapter: 'OK',
  ads_a: {
    outcome: adsA.outcome,
    intent: `${adsAIntent.landing_ref}/${adsAIntent.offer_ref}`,
    whatsapp_contact_authorized: adsAIntent.whatsapp_contact_authorized,
    consent: adsAFirstReason.reason_code,
  },
  second_delivery_new_id: {
    outcome: adsARetry.outcome,
    same_intent: adsARetry.purchase_intent_id === adsA.purchase_intent_id,
    linked_submissions: adsALinks.length,
    conflicts: adsAConflicts.length,
    consent: adsARetryReason.reason_code,
  },
  landing_d_derived: {
    outcome: landingD.outcome,
    intent: `${landingDIntent.landing_ref}/${landingDIntent.offer_ref}`,
    sck: storedSck,
  },
  control_same_id: {
    outcome: sameId.outcome,
    conflicts: landingDConflicts.length,
    consent: poisonedReason.reason_code,
  },
}));
await db.close();
