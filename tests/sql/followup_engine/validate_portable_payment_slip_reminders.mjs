// Los recordatorios del vencimiento de la ficha (C2 y C3, migracion
// 20261019000400), con las RPC reales sobre la cadena completa de migraciones.
//
// Prueba:
//   0. la migracion: cambia exactamente cuatro funciones (el arranque y el
//      contexto de envio de la ficha, la reevaluacion compartida y la adopcion
//      de la respuesta), cada una es su definicion anterior con un solo texto
//      reemplazado y nada mas, no agrega ni saca ninguna, y correrla dos veces
//      aborta sin tocar nada;
//   1. A de punta a punta con una politica de tres pasos (C1 y los dos
//      recordatorios): al aceptarse C1 el motor inserta C2 como no_reply_review
//      anclado al mensaje aceptado, con la conversacion del caso y el mismo
//      vencimiento; C2 lee la ficha con get_portable_payment_slip_send_context
//      y arranca por mark_portable_payment_slip_request_started (autoriza contra
//      el scope de la ficha y cuenta en sus topes; el arranque compartido del
//      carrito lo rechaza); al aceptarse C2 entra C3, y al aceptarse C3 la
//      secuencia termina con policy_exhausted. La evidencia de Chatwoot es la
//      de una plantilla sin respuesta: la ventana de 24 h cerrada
//      (can_reply = false), y C3 con la conversacion resuelta por el equipo.
//      En espera (snoozed), el recordatorio se pausa;
//   2. el predicado: un no_reply_review de una secuencia que no es de una
//      ficha sigue rechazado por las dos RPC, y con la ventana cerrada se
//      pausa como antes;
//   3. B contesta C1 con C2 esperando su dia: la adopcion de la respuesta
//      (admit_portable_inbound_commercial_case_v1) la toma; con la secuencia de
//      otro flujo, la misma accion la frena con 22000, como antes;
//   4. y cuando C2 se reclama, la respuesta no lo cancela
//      (20261019000800): sale igual, como pide la especificacion. Lo siguen
//      frenando la baja (contact_blocked) y una persona del equipo que escribe
//      despues del toque (pausa); con la secuencia de otro flujo, la misma
//      respuesta sigue cancelando con prospect_replied. La migracion
//      20261019000800 cambia solo reevaluate_followup_action, con un solo
//      texto reemplazado, y una segunda corrida aborta.
//
// Datos: el mismo armado de validate_portable_payment_slip_recovery.mjs (el
// manifiesto de ATT1, las fichas capturadas el 2026-10-18 corridas en el
// tiempo, el formulario con consentimiento). La politica: la de
// publicar-ficha-v2.sql de la instancia (tres pasos, tres mensajes, vence a los
// 4 dias), con dos desviaciones documentadas: la ventana es de 00:00 a 23:59
// (la puerta de arranque exige el reloj real) y el delay de los recordatorios
// es de 1 segundo en vez de 1 minuto, para no esperar en la prueba. El momento
// de cada recordatorio (el dia anterior al vencimiento, el dia del vencimiento)
// lo decide el bridge y se prueba en tests/test_payment_slip_reminders.py.
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
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

// ---------------------------------------------------------------------------
// 0. La migracion.
// ---------------------------------------------------------------------------
const TARGET = '20261019000400_portable_payment_slip_reminders_v1.sql';
const migrationNames = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const targetIndex = migrationNames.indexOf(TARGET);
if (targetIndex < 0) throw new Error(`${TARGET} is missing from the canonical stack`);
const apply = async (file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
));
const definitions = async () => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
  where n.nspname = 'public'
`)).rows.map((row) => [row.signature, row.definition]));

await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of migrationNames.slice(0, targetIndex)) {
  await apply(join(root, 'supabase/migrations', name));
}
const before = await definitions();
await apply(join(root, 'supabase/migrations', TARGET));
const after = await definitions();
// La respuesta no cancela el recordatorio (20261019000800): se mide al pasar.
const SURVIVES = '20261019000800_portable_payment_slip_reminder_survives_reply_v1.sql';
let beforeSurvives = null;
let afterSurvives = null;
for (const name of migrationNames.slice(targetIndex + 1)) {
  if (name === SURVIVES) beforeSurvives = await definitions();
  await apply(join(root, 'supabase/migrations', name));
  if (name === SURVIVES) afterSurvives = await definitions();
}
if (afterSurvives === null) throw new Error(`${SURVIVES} is missing from the canonical stack`);

const CHANGED = [
  'mark_portable_payment_slip_request_started(uuid,uuid,text,bigint,timestamp with time zone)',
  'get_portable_payment_slip_send_context(uuid,text,bigint,timestamp with time zone)',
  'reevaluate_followup_action(uuid,text,bigint,timestamp with time zone,boolean,text,text,timestamp with time zone,text,boolean,boolean,boolean,boolean,boolean)',
  'admit_portable_inbound_commercial_case_v1(text,integer,bigint,text)',
];
const changed = [...after.keys()].filter((signature) => before.get(signature) !== after.get(signature)).sort();
const added = [...after.keys()].filter((signature) => !before.has(signature));
const removed = [...before.keys()].filter((signature) => !after.has(signature));
if (JSON.stringify(changed) !== JSON.stringify([...CHANGED].sort())
    || added.length !== 0 || removed.length !== 0) {
  throw new Error(`the migration changed other functions: ${JSON.stringify({ changed, added, removed })}`);
}
// Cada una es la anterior con un solo texto reemplazado, nada mas.
const OLD_TEXTS = [
  "          and action.anchor_type = 'payment_slip'\n",
  "    if v_action.anchor_type is distinct from 'payment_slip' then\n",
  "    elsif p_chatwoot_automation_paused\n"
    + "       or p_chatwoot_human_activity_after_anchor\n"
    + "       or not p_chatwoot_can_reply\n"
    + "       or p_chatwoot_status in ('snoozed', 'resolved') then\n",
  `${' '.repeat(29)}-- portable_adoption_stopped_first_contact: end\n`,
];
for (const [signature, old] of CHANGED.map((signature, index) => [signature, OLD_TEXTS[index]])) {
  const previous = before.get(signature);
  const current = after.get(signature);
  const [head, tail] = previous.split(old);
  if (previous.split(old).length !== 2 || !current.startsWith(head) || !current.endsWith(tail)
      || !current.includes("slip_sequence.reason = 'payment_slip'")) {
    throw new Error(`${signature} is not the previous definition with one text replaced`);
  }
}
let rerun = null;
await db.exec('begin');
try {
  await apply(join(root, 'supabase/migrations', TARGET));
} catch (error) {
  rerun = error;
} finally {
  await db.exec('rollback');
}
if (rerun === null || rerun.code !== '55000' || rerun.message !== 'payment_slip_reminders_already_applied') {
  throw new Error(`a second run did not abort: ${rerun ? `${rerun.code} ${rerun.message}` : 'success'}`);
}
results.migration = { changed: changed.length, added: added.length, rerun: rerun.message };
{
  const REEVALUATION = CHANGED[2];
  const changedSurvives = [...afterSurvives.keys()]
    .filter((signature) => beforeSurvives.get(signature) !== afterSurvives.get(signature));
  const addedSurvives = [...afterSurvives.keys()].filter((signature) => !beforeSurvives.has(signature));
  if (JSON.stringify(changedSurvives) !== JSON.stringify([REEVALUATION]) || addedSurvives.length !== 0) {
    throw new Error(`${SURVIVES} changed other functions: ${JSON.stringify({ changedSurvives, addedSurvives })}`);
  }
  const old = "       and not public._recovery_renewal_requested(v_action.id) then\n";
  const previous = beforeSurvives.get(REEVALUATION);
  const current = afterSurvives.get(REEVALUATION);
  const [head, tail] = previous.split(old);
  if (previous.split(old).length !== 2 || !current.startsWith(head) || !current.endsWith(tail)
      || !current.includes('portable_payment_slip_reminder_survives_reply')) {
    throw new Error(`${SURVIVES}: the reevaluation is not the previous definition with one text replaced`);
  }
  let rerunSurvives = null;
  await db.exec('begin');
  try {
    await apply(join(root, 'supabase/migrations', SURVIVES));
  } catch (error) {
    rerunSurvives = error;
  } finally {
    await db.exec('rollback');
  }
  if (rerunSurvives === null || rerunSurvives.code !== '55000'
      || rerunSurvives.message !== 'payment_slip_survives_reply_already_applied') {
    throw new Error(`a second run of ${SURVIVES} did not abort: ${rerunSurvives ? `${rerunSurvives.code} ${rerunSurvives.message}` : 'success'}`);
  }
  results.survives_reply_migration = { changed: changedSurvives.length, rerun: rerunSurvives.message };
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

// La politica de publicar-ficha-v2.sql, con las desviaciones de la cabecera.
const SLIP = {
  scope: 'att1-ficha',
  scopeVersion: 2,
  policy: 'att1-ficha-tres-toques',
  policyVersion: 1,
  graceMinutes: 60,
  expiresAfter: '4 days',
  steps: [
    { step_key: 'payment_slip_first_contact', mode: 'freeform' },
    { step_key: 'payment_slip_due_eve', mode: 'freeform', delay: '1 second' },
    { step_key: 'payment_slip_due_day', mode: 'freeform', delay: '2 seconds' },
  ],
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
}
await db.query(`
  insert into public.commercial_ally_hotmart_purchase_policies
    (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
  values ($1,$2,$3,true,$4::interval)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion,
  required(PILOT.purchase_policy?.max_lookback, 'purchase_policy.max_lookback')]);

const insertPolicy = (key, version, grace, expires, maxMessages, steps, windows) => db.query(`
  insert into public.followup_policy_versions
    (policy_key, version, status, purpose, timezone, business_windows,
     grace_period, expires_after, max_automatic_messages, steps,
     approved_by, approved_at, published_at)
  values ($1,$2,'published','cart_recovery',$3,$4::jsonb,$5::interval,$6::interval,$7,$8::jsonb,
          'operator-test',now(),now())
`, [key, version, ATT1.timezone, JSON.stringify(windows), grace, expires, maxMessages,
  JSON.stringify(steps)]);
const insertScope = (key, version, eventType, additionalTypes, policy, policyVersion) => db.query(`
  insert into public.pilot_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     channel, channel_provider, channel_account_ref, source, source_event_type,
     additional_source_event_types, external_product_id, offer_code,
     additional_offer_codes, purpose, policy_key, policy_version, timezone,
     max_cohort_contacts, max_outbound_request_starts_total,
     max_outbound_request_starts_per_day, audience_mode,
     approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5,'whatsapp',$6,$7,'hotmart',$8,$9::text[],$10,$11,
          $12::text[],'cart_recovery',$13,$14,$15,20,20,20,'consented_intent','operator-test',now(),now())
`, [key, version, ATT1.tenant, ATT1.accountId, ATT1.inboxId, CHANNEL_PROVIDER, CHANNEL_REF,
  eventType, additionalTypes, String(ATT1.productId), defaultOffer.offer_code,
  additionalOffers.map((offer) => offer.offer_code), policy, policyVersion, ATT1.timezone]);
const arm = async (key, version) => {
  await db.query(`
    insert into public.pilot_runtime_controls
      (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
    values ($1,$2,'inactive',0,'operator-test','default-off')
  `, [key, version]);
  await db.query(`
    select * from public.set_lancemos_pilot_runtime_state($1,$2,0,'armed','operator-test','controlled-test')
  `, [key, version]);
};

await insertPolicy(SLIP.policy, SLIP.policyVersion, `${SLIP.graceMinutes} minutes`,
  SLIP.expiresAfter, SLIP.steps.length, SLIP.steps, ALL_DAY(cartPolicy.business_windows));
await insertScope(SLIP.scope, SLIP.scopeVersion, 'PURCHASE_BILLET_PRINTED', [],
  SLIP.policy, SLIP.policyVersion);
await arm(SLIP.scope, SLIP.scopeVersion);
await insertPolicy(cartPolicy.policy_key, cartPolicy.version, cartPolicy.grace_period,
  cartPolicy.expires_after, 1, cartPolicy.steps, ALL_DAY(cartPolicy.business_windows));
await insertScope(cartScope.scope_key, cartScope.version, cartScope.source_event_type,
  cartScope.additional_source_event_types, cartPolicy.policy_key, cartPolicy.version);
await arm(cartScope.scope_key, cartScope.version);

// ---------------------------------------------------------------------------
// Las capturas, la persona y el plan (como la prueba de C1).
// ---------------------------------------------------------------------------
const fixture = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));
const SLIPS = fixture('hotmart_payment_slip_printed_att1_20261018.json').eventos.map((event) => event.payload);
const MX_CASH = SLIPS.filter((payload) => payload.data.purchase.payment.type === 'CASHPAYMENT'
  && payload.data.purchase.checkout_country.iso === 'MX');
if (MX_CASH.length !== 5) throw new Error(`the captures changed: ${MX_CASH.length} MX cash`);

const dbNow = async () => new Date((await db.query('select clock_timestamp() as now')).rows[0].now);
const baseMs = Math.floor((await dbNow()).getTime() / 1000) * 1000;
const at = (minutes) => new Date(baseMs + minutes * 60_000);
const digits = (value) => String(value ?? '').replace(/\D/g, '');
const shifted = (payload, when) => {
  const copy = structuredClone(payload);
  const gap = payload.creation_date - payload.data.purchase.order_date;
  copy.creation_date = when.getTime();
  copy.data.purchase.order_date = when.getTime() - gap;
  return copy;
};
const leadOf = (payload) => ({
  email: payload.data.buyer.email,
  phone: digits(payload.data.buyer.checkout_phone),
  name: payload.data.buyer.name,
  offer: offerByCode(payload.data.purchase.offer.code),
});
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
const admitForm = async (lead, submittedAt) => {
  const offer = lead.offer;
  const id = `slip-form-${lead.email}-${offer.offer_code}`;
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
const person = async (payload, createdAt) => {
  const lead = leadOf(payload);
  lead.intent = await admitForm(lead, new Date(createdAt.getTime() - 30 * 60_000));
  const body = shifted(payload, createdAt);
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_payment_slip_v2($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, body.id, JSON.stringify(body),
    lead.email.toLowerCase(), lead.phone])).rows, `slip ${body.id}`);
  await createContact(lead, admitted.webhook_event_id);
  const plan = one((await db.query(`
    select * from public.plan_portable_payment_slip_recovery(
      $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
    )
  `, [admitted.webhook_event_id, lead.contact, String(ATT1.productId), ATT1.productName,
    lead.offer.offer_code, SLIP.policy, SLIP.policyVersion, createdAt.toISOString(),
    ATT1.accountId, ATT1.inboxId, lead.phone, SLIP.scope, SLIP.scopeVersion])).rows, 'plan');
  return { lead, body, eventId: admitted.webhook_event_id, createdAt, plan };
};
const actionOf = async (id) => one((await db.query(`
  select * from public.scheduled_actions where id = $1
`, [id])).rows, 'action');
const caseActions = async (caseId) => (await db.query(`
  select id, step_key, action_type, status from public.scheduled_actions
  where recovery_case_id = $1 order by created_at
`, [caseId])).rows;
const starts = async () => one((await db.query(`
  select count(*)::integer as count from public.pilot_outbound_request_authorizations
  where scope_key = $1 and scope_version = $2
`, [SLIP.scope, SLIP.scopeVersion])).rows, 'starts').count;

const [A_SLIP, B_SLIP] = MX_CASH;
// B se planifica recien en el caso 2: si no, el claim de A tomaria su C1.
const people = { A: await person(A_SLIP, at(-70)) };
const CONVERSATION = { A: '910001', B: '910002' };

// Lo que el dispatcher arma de Chatwoot antes de un no_reply_review.
// Despues de una plantilla sin respuesta, la ventana de 24 h de WhatsApp esta
// cerrada: Chatwoot da can_reply = false (lo que lee chatwoot.py del
// conversation). Una respuesta la abre.
const evidence = (conversation, now, { replied = false, status = 'open' } = {}) => ({
  p_chatwoot_checked: true,
  p_chatwoot_conversation_id: conversation,
  p_chatwoot_checkpoint_message_id: '1',
  p_chatwoot_checkpoint_at: now.toISOString(),
  p_chatwoot_status: status,
  p_chatwoot_can_reply: replied,
  p_chatwoot_anchor_found: true,
  p_chatwoot_automation_paused: false,
  p_chatwoot_inbound_after_anchor: replied,
  p_chatwoot_human_activity_after_anchor: false,
});
const reevaluate = async (actionId, worker, lease, now, chatwoot) => {
  if (!chatwoot) {
    return one((await db.query(`
      select * from public.reevaluate_followup_action($1,$2,$3,$4)
    `, [actionId, worker, lease, now])).rows, 'reevaluation');
  }
  const names = Object.keys(chatwoot);
  const args = names.map((name, index) => `${name} => $${index + 5}`).join(', ');
  return one((await db.query(`
    select * from public.reevaluate_followup_action(
      p_action_id => $1, p_worker_id => $2, p_lease_generation => $3, p_now => $4, ${args}
    )
  `, [actionId, worker, lease, now, ...names.map((name) => chatwoot[name])])).rows, 'reevaluation');
};
const rolledBack = async (run) => {
  await db.exec('begin');
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};
const claim = async (worker, actionId, now) => {
  const claimed = (await db.query(`
    select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
  `, [worker, now])).rows.filter((row) => row.id === actionId);
  return one(claimed, `claim ${actionId}`);
};

// Un toque de la ficha de punta a punta: claim, reevaluacion, contexto de la
// ficha, reserva, arranque por la RPC de la ficha y aceptacion.
const sendStep = async (who, actionId, label, { chatwoot = false, status = 'open', replied = false } = {}) => {
  const worker = `slip-worker-${who}`;
  const now = await dbNow();
  const claimed = await claim(worker, actionId, now);
  const lease = claimed.lease_generation;
  const decision = await reevaluate(actionId, worker, lease, now,
    chatwoot ? evidence(CONVERSATION[who], now, { status, replied }) : null);
  if (decision.decision !== 'execute' || decision.reason_code !== 'eligible_for_execution') {
    throw new Error(`${label} reevaluation: ${JSON.stringify(decision)}`);
  }
  const slipContext = one((await db.query(`
    select * from public.get_portable_payment_slip_send_context($1,$2,$3,$4)
  `, [actionId, worker, lease, now])).rows, `${label} slip context`);
  if (slipContext.webhook_event_id !== people[who].eventId
      || slipContext.billet_barcode !== people[who].body.data.purchase.payment.billet_barcode) {
    throw new Error(`${label} slip context: ${JSON.stringify(slipContext)}`);
  }
  const attempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
  `, [actionId, worker, lease, decision.case_version, decision.sequence_revision, now])).rows,
  `${label} reservation`);
  const startedAt = await dbNow();
  if (chatwoot) {
    // El arranque compartido (el del carrito) no arranca un recordatorio de la
    // ficha: autorizaria con PURCHASE_OUT_OF_SHOPPING_CART contra el scope de
    // la ficha.
    await expectError(`${label} through the shared start`, () => db.query(`
      select * from public.mark_lancemos_pilot_request_started($1,$2,$3,$4,$5)
    `, [actionId, attempt.id, worker, lease, startedAt]), { code: '55000' });
  }
  const started = one((await db.query(`
    select * from public.mark_portable_payment_slip_request_started($1,$2,$3,$4,$5)
  `, [actionId, attempt.id, worker, lease, startedAt])).rows, `${label} start`);
  const authorization = one((await db.query(`
    select scope_key, scope_version, action_id from public.pilot_outbound_request_authorizations
    where id = $1
  `, [started.pilot_authorization_id])).rows, `${label} authorization`);
  if (started.phase !== 'request_started' || authorization.scope_key !== SLIP.scope
      || authorization.scope_version !== SLIP.scopeVersion || authorization.action_id !== actionId) {
    throw new Error(`${label} start: ${JSON.stringify({ started, authorization })}`);
  }
  const accepted = one((await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
  `, [actionId, attempt.id, worker, lease, CONVERSATION[who], `slip-wamid-${who}-${label}`,
    `Plantilla aprobada ${label} de la ficha`, await dbNow()])).rows, `${label} acceptance`);
  if (accepted.status !== 'accepted_by_chatwoot') {
    throw new Error(`${label} acceptance: ${JSON.stringify(accepted)}`);
  }
  return accepted;
};
const nextStep = async (who, stepKey, label) => {
  const rows = (await caseActions(people[who].plan.recovery_case_id))
    .filter((row) => row.step_key === stepKey);
  const row = one(rows, `${label} next step`);
  const action = await actionOf(row.id);
  const c1 = await actionOf(people[who].plan.scheduled_action_id);
  const anchor = one((await db.query(`
    select m.conversation_id, m.direction from public.messages m where m.id = $1
  `, [action.anchor_subject_internal_id])).rows, `${label} anchor`);
  if (action.action_type !== 'no_reply_review' || action.status !== 'pending'
      || action.anchor_type !== 'accepted_outbound_message'
      || action.conversation_id === null || anchor.conversation_id !== action.conversation_id
      || anchor.direction !== 'outbound'
      || action.expires_at.getTime() !== c1.expires_at.getTime()) {
    throw new Error(`${label} next step: ${JSON.stringify({ action, anchor })}`);
  }
  return action;
};

// ---------------------------------------------------------------------------
// 1. A de punta a punta: C1, C2 y C3.
// ---------------------------------------------------------------------------
{
  await sendStep('A', people.A.plan.scheduled_action_id, 'C1');
  const sequenceAfterC1 = one((await db.query(`
    select status, reason, current_step from public.followup_sequences where id = $1
  `, [people.A.plan.followup_sequence_id])).rows, 'A sequence after C1');
  if (sequenceAfterC1.status !== 'active' || sequenceAfterC1.reason !== 'payment_slip'
      || sequenceAfterC1.current_step !== 1) {
    throw new Error(`A sequence after C1: ${JSON.stringify(sequenceAfterC1)}`);
  }
  const c2 = await nextStep('A', 'payment_slip_due_eve', 'C2');
  await sleep(1200);
  await sendStep('A', c2.id, 'C2', { chatwoot: true });
  const c3 = await nextStep('A', 'payment_slip_due_day', 'C3');
  await sleep(1200);
  // En espera (snoozed) se pausa, como todo seguimiento.
  const snoozed = await rolledBack(async () => {
    const now = await dbNow();
    const claimed = await claim('slip-worker-A', c3.id, now);
    return reevaluate(c3.id, 'slip-worker-A', claimed.lease_generation, now,
      evidence(CONVERSATION.A, now, { status: 'snoozed' }));
  });
  if (snoozed.decision !== 'pause' || snoozed.reason_code !== 'chatwoot_conversation_blocked') {
    throw new Error(`C3 snoozed: ${JSON.stringify(snoozed)}`);
  }
  // Resuelta por el equipo sin respuesta, sale.
  await sendStep('A', c3.id, 'C3', { chatwoot: true, status: 'resolved' });
  const sequence = one((await db.query(`
    select status, completion_reason, automatic_messages_accepted
    from public.followup_sequences where id = $1
  `, [people.A.plan.followup_sequence_id])).rows, 'A sequence');
  const actions = await caseActions(people.A.plan.recovery_case_id);
  if (sequence.status !== 'completed' || sequence.completion_reason !== 'policy_exhausted'
      || sequence.automatic_messages_accepted !== 3 || actions.length !== 3
      || actions.some((row) => row.status !== 'accepted_by_chatwoot')) {
    throw new Error(`A chain: ${JSON.stringify({ sequence, actions })}`);
  }
  if (await starts() !== 3) throw new Error(`A starts: ${await starts()}`);
  results.chain = {
    steps: actions.map((row) => `${row.action_type}:${row.step_key}`),
    sequence: sequence.completion_reason,
    starts_in_slip_scope: 3,
    window_closed: 'execute',
    resolved: 'execute',
    snoozed: snoozed.reason_code,
  };
}

// ---------------------------------------------------------------------------
// 2. El predicado: un no_reply_review de una secuencia que no es de una ficha.
// ---------------------------------------------------------------------------
{
  people.B = await person(B_SLIP, at(-65));
  await sendStep('B', people.B.plan.scheduled_action_id, 'C1');
  const c2 = await nextStep('B', 'payment_slip_due_eve', 'B C2');
  await sleep(1200);
  const worker = 'slip-worker-B';
  const now = await dbNow();
  const claimed = await claim(worker, c2.id, now);
  for (const [label, run] of [
    ['context of a cart follow-up', (lease) => db.query(`
      select * from public.get_portable_payment_slip_send_context($1,$2,$3,$4)
    `, [c2.id, worker, lease, now])],
  ]) {
    await expectError(label, async () => {
      await db.query(`
        update public.followup_sequences set reason = 'cart_abandonment' where id = $1
      `, [people.B.plan.followup_sequence_id]);
      return run(claimed.lease_generation);
    }, { code: '55000', message: 'payment_slip_action_required' });
  }
  // La ventana cerrada: un seguimiento de otro flujo se pausa como antes.
  const otherFlow = await rolledBack(async () => {
    await db.query(`
      update public.followup_sequences set reason = 'cart_abandonment' where id = $1
    `, [people.B.plan.followup_sequence_id]);
    return reevaluate(c2.id, worker, claimed.lease_generation, now, evidence(CONVERSATION.B, now));
  });
  if (otherFlow.decision !== 'pause' || otherFlow.reason_code !== 'chatwoot_conversation_blocked') {
    throw new Error(`a cart follow-up with the window closed: ${JSON.stringify(otherFlow)}`);
  }
  // El arranque: con la secuencia de otro flujo, tampoco.
  const decision = await reevaluate(c2.id, worker, claimed.lease_generation, now,
    evidence(CONVERSATION.B, now));
  const attempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
  `, [c2.id, worker, claimed.lease_generation, decision.case_version,
    decision.sequence_revision, now])).rows, 'B C2 reservation');
  await expectError('start of a cart follow-up', async () => {
    await db.query(`
      update public.followup_sequences set reason = 'cart_abandonment' where id = $1
    `, [people.B.plan.followup_sequence_id]);
    return db.query(`
      select * from public.mark_portable_payment_slip_request_started($1,$2,$3,$4,$5)
    `, [c2.id, attempt.id, worker, claimed.lease_generation, await dbNow()]);
  }, { code: '55000', message: 'pilot_request_start_rejected', detail: 'payment_slip_action_required' });
  // Se libera el intento sin arrancar, como el dispatcher ante un rechazo que
  // no es de la base: queda para el caso 3.
  await db.query(`
    select * from public.finalize_followup_delivery_attempt(
      $1,$2,$3,$4,'failed_before_request',null,null,'test_released',$5,null,$6
    )
  `, [c2.id, attempt.id, worker, claimed.lease_generation,
    new Date(now.getTime() + 1000).toISOString(), now]);
  results.predicate = { rpcs: 'payment_slip_action_required', window_closed: otherFlow.reason_code };
}

// ---------------------------------------------------------------------------
// 3. B contesta C1 mientras C2 espera su dia: el agente toma la respuesta.
// ---------------------------------------------------------------------------
{
  // El scope entrante de ATT1, como lo siembra aprovisionar-att1.sql.
  await db.query(`
    insert into public.inbound_commercial_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
       external_product_id, offer_code, approved_by, approved_at, published_at)
    values ($1,$2,'published',$3,$4,$5,$6,$7,'operator-test',now(),now())
  `, [ATT1.inboundScope, ATT1.inboundVersion, ATT1.tenant, ATT1.accountId, ATT1.inboxId,
    ATT1.hotlink, defaultOffer.offer_code]);
  const userId = one((await db.query(`
    select identity.external_user_id
    from public.recovery_cases recovery
    join public.channel_identities identity on identity.id = recovery.selected_channel_identity_id
    where recovery.id = $1
  `, [people.B.plan.recovery_case_id])).rows, 'B identity').external_user_id;
  const pending = (await caseActions(people.B.plan.recovery_case_id))
    .filter((row) => row.step_key === 'payment_slip_due_eve');
  if (pending.length !== 1 || pending[0].status !== 'retryable_failed') {
    throw new Error(`B C2 is not waiting: ${JSON.stringify(pending)}`);
  }
  const ADMISSION = 'select * from public.admit_portable_inbound_commercial_case_v1($1,$2,$3,$4)';
  const args = [ATT1.inboundScope, ATT1.inboundVersion, Number(CONVERSATION.B), userId];
  const asService = async (run) => {
    await db.exec('set role service_role');
    try {
      return await run();
    } finally {
      await db.exec('reset role');
    }
  };
  // Con la secuencia de otro flujo, el paso pendiente frena la adopcion (el
  // 22000 del 09/10): lo que hace la migracion es solo para la ficha.
  await expectError('adoption with a pending follow-up of another flow', async () => {
    await db.query(`
      update public.followup_sequences set reason = 'cart_abandonment' where id = $1
    `, [people.B.plan.followup_sequence_id]);
    // set local: el rollback de expectError devuelve el rol.
    await db.exec('set local role service_role');
    return db.query(ADMISSION, args);
  }, { code: '22000' });
  const adopted = one((await asService(() => db.query(ADMISSION, args))).rows, 'B adoption');
  if (adopted.outcome !== 'created' || adopted.automation_status !== 'draft_only') {
    throw new Error(`B adoption: ${JSON.stringify(adopted)}`);
  }
  results.adoption = `${adopted.outcome}:${adopted.automation_status}`;
}

// ---------------------------------------------------------------------------
// 4. B contesto despues de C1: el recordatorio sale igual (20261019000800).
// ---------------------------------------------------------------------------
{
  // El C2 que el caso 2 libero sin arrancar (queda retryable_failed).
  const c2 = one((await caseActions(people.B.plan.recovery_case_id))
    .filter((row) => row.step_key === 'payment_slip_due_eve'
      && row.status === 'retryable_failed'), 'B C2 released');
  await sleep(1200);
  const worker = 'slip-worker-B';
  // Cada variante corre en su transaccion y se deshace: el C2 queda igual.
  const decide = (prepare, chatwoot) => rolledBack(async () => {
    await prepare();
    const now = await dbNow();
    const claimed = await claim(worker, c2.id, now);
    return reevaluate(c2.id, worker, claimed.lease_generation, now, chatwoot(now));
  });
  const nothing = async () => {};
  const replied = await decide(nothing, (now) => evidence(CONVERSATION.B, now, { replied: true }));
  if (replied.decision !== 'execute' || replied.reason_code !== 'eligible_for_execution') {
    throw new Error(`B replied, the reminder must go out: ${JSON.stringify(replied)}`);
  }
  // Una persona del equipo escribio despues del toque: pausa, como antes.
  const human = await decide(nothing, (now) => ({
    ...evidence(CONVERSATION.B, now, { replied: true }),
    p_chatwoot_human_activity_after_anchor: true,
  }));
  if (human.decision !== 'pause' || human.reason_code !== 'chatwoot_conversation_blocked') {
    throw new Error(`B replied and a person wrote: ${JSON.stringify(human)}`);
  }
  // La baja (el boton o una frase): cancela, como antes.
  const optedOut = await decide(async () => {
    await db.query(`
      update public.contacts set contact_permission = 'opted_out'
      where id = (select contact_id from public.recovery_cases where id = $1)
    `, [people.B.plan.recovery_case_id]);
  }, (now) => evidence(CONVERSATION.B, now, { replied: true }));
  if (optedOut.decision !== 'cancel' || optedOut.reason_code !== 'contact_blocked') {
    throw new Error(`B opted out: ${JSON.stringify(optedOut)}`);
  }
  // Con la secuencia de otro flujo, la misma respuesta sigue cancelando.
  const otherFlow = await decide(async () => {
    await db.query(`
      update public.followup_sequences set reason = 'cart_abandonment' where id = $1
    `, [people.B.plan.followup_sequence_id]);
  }, (now) => evidence(CONVERSATION.B, now, { replied: true }));
  if (otherFlow.decision !== 'cancel' || otherFlow.reason_code !== 'prospect_replied') {
    throw new Error(`another flow replied: ${JSON.stringify(otherFlow)}`);
  }
  // Y de verdad: B contesto y el recordatorio sale.
  await sendStep('B', c2.id, 'B C2', { chatwoot: true, replied: true });
  const sent = await actionOf(c2.id);
  if (sent.status !== 'accepted_by_chatwoot') {
    throw new Error(`B C2 after the reply: ${JSON.stringify(sent)}`);
  }
  results.replied = {
    reminder: `${replied.decision}:${replied.reason_code}`,
    sent: sent.status,
    human_after: human.reason_code,
    opted_out: optedOut.reason_code,
    other_flow: otherFlow.reason_code,
  };
}

console.log(`portable_payment_slip_reminders=OK ${JSON.stringify(results)}`);
