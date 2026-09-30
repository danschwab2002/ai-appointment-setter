// La audiencia del scope del piloto (migracion 20260930000300), con las RPC
// reales y la reevaluacion REAL.
//
// Tres scopes publicados con los valores de ATT1, uno por modo, cada uno con su
// control armado:
//   - manual_cohort: la regresion. Con intencion consentida pero sin cohorte se
//     rechaza igual que hoy; inscripto sale, el binding queda
//     (manual_cohort, null) y el data del evento de control es identico.
//   - consented_intent_in_cohort: exige las dos cosas. Consentido y fuera de
//     la cohorte, o inscripto sin formulario o con un formulario sin opt-in, no
//     entra; con las dos, sale con el binding (modo, intencion).
//   - consented_intent: no mira la cohorte. Entra la intencion consentida del
//     evento (carrito y pago fallido); sin formulario, con un formulario sin
//     opt-in, de otra oferta, de otro telefono o fuera del mapeo activo, no.
//     La autorizacion del envio re-verifica la intencion antes del
//     presupuesto: el tope diario corta, y un consentimiento perdido entre el
//     plan y el envio corta antes que el tope y no consume nada.
// Ademas la forma: toda fila existente queda en manual_cohort, un modo
// invalido se rechaza, un modo consentido se admite con source landing, la
// version publicada es inmutable y el binding exige su forma.
//
// Datos: binding, ofertas, landings, producto, Chatwoot y consentimiento de
// tests/fixtures/instances/att1/instancia.toml; politica y ventana de la
// intencion de tests/fixtures/instances/att1/politica-piloto.json (como
// validate_att1_portable_chain.mjs, con la misma desviacion documentada de la
// ventana de envio). Carrito: la captura
// tests/fixtures/hotmart_cart_abandonment_rejected_v1.json con producto,
// oferta, id, fecha y comprador sustituidos. Deuda (A0): no hay
// PURCHASE_CANCELED ni lead.precheckout capturados; el pago fallido y el
// formulario usan el precedente inline de
// validate_commercial_ally_payment_failure_recovery.mjs con los valores de ATT1.
// Revocar el consentimiento de una intencion es un UPDATE de la base como
// superusuario: es estado de la base, no un payload.
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

const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};
const results = {};

// ---------------------------------------------------------------------------
// 0. Toda fila que sembraron las migraciones queda en manual_cohort, y todo
//    binding existente con el default.
// ---------------------------------------------------------------------------
const seeded = one((await db.query(`
  select
    (select count(*)::integer from public.pilot_scope_versions) as scopes,
    (select count(*)::integer from public.pilot_scope_versions
      where audience_mode <> 'manual_cohort') as consented_scopes,
    (select count(*)::integer from public.pilot_recovery_case_bindings
      where audience_mode <> 'manual_cohort' or audience_purchase_intent_id is not null)
      as consented_bindings
`)).rows, 'seeded scopes');
if (seeded.consented_scopes !== 0 || seeded.consented_bindings !== 0) {
  throw new Error(`an existing row left manual_cohort: ${JSON.stringify(seeded)}`);
}
results.seeded_scopes_manual_cohort = seeded.scopes;

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

// Un scope por modo, con los valores de ATT1 salvo la clave, el modo y los topes.
const insertScope = (scope, { status = 'published' } = {}) => db.query(`
  insert into public.pilot_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     channel, channel_provider, channel_account_ref, source, source_event_type,
     additional_source_event_types, external_product_id, offer_code,
     additional_offer_codes, purpose, policy_key, policy_version, timezone,
     max_cohort_contacts, max_outbound_request_starts_total,
     max_outbound_request_starts_per_day, audience_mode,
     approved_by, approved_at, published_at)
  values ($1,$2,$3,$4,$5,$6,'whatsapp',$7,$8,$9,$10,$11::text[],$12,$13,
          $14::text[],$15,$16,$17,$18,$19,$20,$21,$22,
          'operator-test',now(),now())
`, [
  scope.key, scope.version, status, ATT1.tenant, ATT1.accountId, ATT1.inboxId,
  CHANNEL_PROVIDER, CHANNEL_REF, scope.source ?? 'hotmart',
  scope.sourceEventType ?? 'PURCHASE_OUT_OF_SHOPPING_CART',
  scope.additionalSourceEventTypes ?? ['PURCHASE_CANCELED'],
  String(ATT1.productId), defaultOffer.offer_code,
  additionalOffers.map((offer) => offer.offer_code),
  'cart_recovery', POLICY, POLICY_VERSION, ATT1.timezone,
  scope.maxCohort ?? 5, scope.total ?? 20, scope.perDay ?? 20, scope.mode,
]);
const armScope = async (scope) => {
  await db.query(`
    insert into public.pilot_runtime_controls
      (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
    values ($1,$2,'inactive',0,'operator-test','default-off')
  `, [scope.key, scope.version]);
  const armed = one((await db.query(`
    select * from public.set_lancemos_pilot_runtime_state($1,$2,0,'armed','operator-test','controlled-test')
  `, [scope.key, scope.version])).rows, `${scope.key} arm`);
  if (armed.runtime_state !== 'armed') throw new Error(`${scope.key} was not armed`);
};
const MANUAL = { key: 'att1-audiencia-manual', version: 1, mode: 'manual_cohort' };
const IN_COHORT = { key: 'att1-audiencia-cohorte', version: 1, mode: 'consented_intent_in_cohort' };
const OPEN = { key: 'att1-audiencia-abierta', version: 1, mode: 'consented_intent', perDay: 3 };
for (const scope of [MANUAL, IN_COHORT, OPEN]) {
  await insertScope(scope);
  await armScope(scope);
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
// 1. Forma del scope.
// ---------------------------------------------------------------------------
await expectError('an unknown audience mode',
  () => insertScope({ key: 'att1-audiencia-invalida', version: 1, mode: 'everyone' },
    { status: 'draft' }),
  { code: '23514' });
// El primer contacto del formulario (bloque B) va a usar un scope landing.
await insertScope({
  key: 'att1-audiencia-landing', version: 1, mode: 'consented_intent', source: 'landing',
  sourceEventType: 'PRECHECKOUT_FORM_SUBMITTED', additionalSourceEventTypes: [],
}, { status: 'draft' });
await expectError('changing the mode of a published version',
  () => db.query(`
    update public.pilot_scope_versions set audience_mode = 'consented_intent'
    where scope_key = $1 and version = $2
  `, [MANUAL.key, MANUAL.version]),
  { code: '55000', message: 'published_pilot_scope_is_immutable' });
results.scope_shape = 'OK';

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
const CAPTURED_CART = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/hotmart_cart_abandonment_rejected_v1.json'), 'utf8',
)).payload;

let personIndex = 0;
const person = (label) => {
  personIndex += 1;
  const suffix = String(personIndex).padStart(2, '0');
  return {
    label,
    name: `Compradora audiencia ${label}`,
    email: `att1-audience-${suffix}@example.test`,
    phone: `120255502${suffix}`,
  };
};

// Formulario de la landing de la oferta. consented=false es un envio 1.0.0 sin
// opt-in: la admision lo acepta y deja la intencion sin los dos permisos.
const admitForm = async (lead, offer, { consented = true } = {}) => {
  const version = consented ? '1.1.0' : '1.0.0';
  const id = `att1-audience-form-${lead.email}-${offer.offer_code}`;
  const pageUrl = `https://${offer.page_host}${offer.page_path}`;
  const checkoutUrl = `https://pay.hotmart.com/${ATT1.hotlink}?off=${offer.offer_code}&checkoutMode=10`;
  const raw = {
    id,
    event: 'lead.precheckout',
    version,
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
      consent: consented
        ? { marketing_optin: true, whatsapp_contact: true, copy_version: ATT1.copyVersion }
        : { marketing_optin: false, notice: 'Aviso de privacidad sin opt-in explicito.' },
    },
    dedupe_key: `${offer.site}:${offer.offer_code}:${lead.email}`,
  };
  const canonical = {
    external_submission_id: id,
    event_type: 'PRECHECKOUT_FORM_SUBMITTED',
    contract_version: version,
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
      terms_accepted: false, privacy_accepted: false, marketing_optin: consented,
      whatsapp_contact: consented,
      copy_version: consented ? ATT1.copyVersion : 'lead-precheckout-v1-no-explicit-optin',
    },
    assurance: { provisional: false, provider_observed: true, activation_authorized: consented },
  };
  const admitted = one((await db.query(`
    select * from public.admit_portable_observed_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, id, JSON.stringify(raw),
    JSON.stringify(canonical)])).rows, `${lead.label} form`);
  const intent = one((await db.query(`
    select offer_ref, whatsapp_contact_authorized, activation_authorized
    from public.purchase_intents where id = $1
  `, [admitted.purchase_intent_id])).rows, `${lead.label} intent`);
  if (admitted.outcome !== 'inserted'
      || intent.offer_ref !== offer.offer_code
      || intent.whatsapp_contact_authorized !== consented
      || intent.activation_authorized !== consented) {
    throw new Error(`${lead.label}: unexpected form admission: ${JSON.stringify({ admitted, intent })}`);
  }
  return admitted.purchase_intent_id;
};

// Carrito capturado. correlationPhone=null es un comprador sin telefono en el
// evento: se correlaciona solo por email.
const admitCart = async (lead, offer, { correlationPhone = lead.phone } = {}) => {
  const payload = structuredClone(CAPTURED_CART);
  payload.id = `att1-audience-cart-${lead.email}-${offer.offer_code}`;
  payload.creation_date = CART_AT.getTime();
  payload.data.product = { id: ATT1.productId, name: ATT1.productName };
  payload.data.offer = { code: offer.offer_code };
  payload.data.buyer = correlationPhone === null
    ? { name: lead.name, email: lead.email }
    : { name: lead.name, email: lead.email, phone: lead.phone };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
    JSON.stringify(payload), lead.email, correlationPhone])).rows, `${lead.label} cart`);
  if (admitted.outcome !== 'inserted') {
    throw new Error(`${lead.label}: cart was not admitted: ${JSON.stringify(admitted)}`);
  }
  const correlation = (await db.query(`
    select outcome, purchase_intent_id from public.hotmart_purchase_intent_correlations
    where webhook_event_id = $1
  `, [admitted.webhook_event_id])).rows[0] ?? null;
  return {
    eventId: admitted.webhook_event_id,
    abandonedAt: new Date(payload.creation_date),
    correlation,
  };
};

// Pago fallido (precedente inline, sin captura).
let transactionIndex = 0;
const admitFailure = async (lead, offer) => {
  transactionIndex += 1;
  const payload = {
    id: `att1-audience-failure-${lead.email}`,
    creation_date: FAILED_AT.getTime(),
    event: 'PURCHASE_CANCELED',
    version: '2.0.0',
    data: {
      buyer: { name: lead.name, email: lead.email, checkout_phone: `+${lead.phone}` },
      product: { id: ATT1.productId, name: ATT1.productName },
      purchase: {
        transaction: `HPATT1AUD${String(transactionIndex).padStart(3, '0')}`,
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
  const detail = one((await db.query(`
    select correlation_outcome, purchase_intent_id
    from public.commercial_ally_payment_failure_details where webhook_event_id = $1
  `, [admitted.webhook_event_id])).rows, `${lead.label} failure correlation`);
  if (admitted.outcome !== 'inserted' || detail.correlation_outcome !== 'resolved') {
    throw new Error(`${lead.label}: payment failure was not admitted and correlated: ${JSON.stringify({ admitted, detail })}`);
  }
  return {
    eventId: admitted.webhook_event_id,
    failedAt: new Date(payload.creation_date),
    intentId: detail.purchase_intent_id,
  };
};

// Lo que deja resolve_event antes de planificar: el contacto y sus puntos.
const createContact = async (lead, eventId, { phone = lead.phone } = {}) => {
  lead.contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone, country_iso)
    values ($1,$2,$3,'MX') returning id
  `, [lead.name, lead.email, phone])).rows, `${lead.label} contact`).id;
  await db.query(`
    insert into public.contact_points
      (contact_id, type, raw_value, normalized_value, source, source_event_id)
    values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)
  `, [lead.contact, lead.email, phone, eventId]);
  lead.destination = phone;
};
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
const evaluate = async (lead, scope, offer, eventType = 'PURCHASE_OUT_OF_SHOPPING_CART') => one((await db.query(`
  select * from public.evaluate_lancemos_pilot_scope($1,$2,$3,$4,$5,$6,$7,'hotmart',$8,$9,$10,$11)
`, [scope.key, scope.version, ATT1.tenant, ATT1.accountId, ATT1.inboxId, CHANNEL_PROVIDER,
  CHANNEL_REF, eventType, String(ATT1.productId), offer.offer_code, lead.contact])).rows,
`${lead.label} evaluation`);

// Los mismos argumentos que arma resolution.resolve_event con el binding portable.
const planCart = (lead, offer, cart, scope, { offerCode = offer.offer_code } = {}) => db.query(`
  select * from public.plan_lancemos_pilot_cart_recovery(
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
  )
`, [cart.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  offerCode, POLICY, POLICY_VERSION, cart.abandonedAt.toISOString(), ATT1.accountId,
  ATT1.inboxId, lead.destination, scope.key, scope.version]);
const planFailure = (lead, offer, failure, scope) => db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
  )
`, [failure.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  offer.offer_code, POLICY, POLICY_VERSION, failure.failedAt.toISOString(), ATT1.accountId,
  ATT1.inboxId, lead.destination, scope.key, scope.version]);

// Un rechazo de la planificacion no deja caso, binding ni permiso.
const expectPlanRejection = async (label, lead, action, detail) => {
  await expectError(label, action, { code: '55000', message: 'pilot_scope_rejected', detail });
  const effects = one((await db.query(`
    select
      (select count(*)::integer from public.recovery_cases where contact_id = $1) as cases,
      (select count(*)::integer from public.contact_authorizations where contact_id = $1) as grants
  `, [lead.contact])).rows, `${label} effects`);
  if (effects.cases !== 0 || effects.grants !== 0) {
    throw new Error(`${label}: a rejected plan left durable work: ${JSON.stringify(effects)}`);
  }
  return detail;
};
const bindingOf = async (plan) => one((await db.query(`
  select scope_key, audience_mode, audience_purchase_intent_id
  from public.pilot_recovery_case_bindings where recovery_case_id = $1
`, [plan.recovery_case_id])).rows, 'binding');
const expectBinding = async (label, plan, scope, intentId) => {
  const binding = await bindingOf(plan);
  if (binding.scope_key !== scope.key
      || binding.audience_mode !== scope.mode
      || binding.audience_purchase_intent_id !== intentId) {
    throw new Error(`${label}: binding ${JSON.stringify(binding)}, expected ${scope.mode} ${intentId}`);
  }
};
const startsOf = async (scope) => one((await db.query(`
  select count(*)::integer as count from public.pilot_outbound_request_authorizations
  where scope_key = $1
`, [scope.key])).rows, `${scope.key} starts`).count;

// El dispatcher en modo directo: claim, la reevaluacion REAL, reserva en
// approved_template y el arranque por el anchor_type de la accion.
const DIRECT_DELIVERY_MODE = 'approved_template';
const startOperationFor = (anchorType) => (anchorType === 'payment_failure'
  ? 'mark_portable_payment_failure_request_started'
  : 'mark_lancemos_pilot_request_started');
const reserve = async (lead, plan, anchor) => {
  const worker = `att1-audience-${lead.label}`;
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
  if (decision.decision !== 'execute' || decision.reason_code !== 'eligible_for_execution') {
    throw new Error(`${lead.label}: the real reevaluation did not execute: ${JSON.stringify(decision)}`);
  }
  const attempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp',$6,$7)
  `, [plan.scheduled_action_id, worker, lease, decision.case_version,
    decision.sequence_revision, DIRECT_DELIVERY_MODE, now])).rows, `${lead.label} reservation`);
  return { worker, lease, attempt, operation: startOperationFor(claimed[0].anchor_type) };
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
  `, [plan.scheduled_action_id, attempt.id, worker, lease, String(910100 + messageNumber),
    `att1-audience-wamid-${messageNumber}`,
    `Plantilla aprobada de ATT1 para ${lead.name} (${ATT1.productName})`,
    await dbNow()])).rows, `${lead.label} acceptance`);
  if (accepted.status !== 'accepted_by_chatwoot') {
    throw new Error(`${lead.label}: acceptance did not finalize: ${JSON.stringify(accepted)}`);
  }
  return one((await db.query(`
    select data from public.pilot_control_events
    where event_type = 'pilot_outbound_request_authorized' and attempt_id = $1
  `, [attempt.id])).rows, `${lead.label} control event`).data;
};
// El arranque rechazado no deja autorizacion ni evento de control. La accion
// queda reservada con su lease: estos casos van al final.
const expectStartRejection = async (lead, plan, anchor, scope, detail) => {
  const { worker, lease, attempt, operation } = await reserve(lead, plan, anchor);
  const before = await startsOf(scope);
  let error = null;
  try {
    await db.query(`select * from public.${operation}($1,$2,$3,$4,$5)`,
      [plan.scheduled_action_id, attempt.id, worker, lease, await dbNow()]);
  } catch (caught) {
    error = caught;
  }
  const events = one((await db.query(`
    select count(*)::integer as count from public.pilot_control_events where attempt_id = $1
  `, [attempt.id])).rows, `${lead.label} control events`).count;
  if (error?.code !== '55000'
      || error?.message !== 'pilot_request_start_rejected'
      || error?.detail !== detail
      || (await startsOf(scope)) !== before
      || events !== 0) {
    throw new Error(`${lead.label}: expected pilot_request_start_rejected:${detail} without consuming, got ${error?.code} ${error?.message} ${error?.detail}`);
  }
  return detail;
};
const expectAudienceData = (label, data, scope, intentId) => {
  const keys = Object.keys(data).sort().join(',');
  const expectedKeys = scope.mode === 'manual_cohort'
    ? 'local_budget_date'
    : 'audience_mode,audience_purchase_intent_id,local_budget_date';
  if (keys !== expectedKeys
      || (scope.mode !== 'manual_cohort'
          && (data.audience_mode !== scope.mode || data.audience_purchase_intent_id !== intentId))) {
    throw new Error(`${label}: control event data ${JSON.stringify(data)}`);
  }
};

// ---------------------------------------------------------------------------
// 2. manual_cohort: la regresion. La intencion consentida no alcanza.
// ---------------------------------------------------------------------------
{
  const lead = person('manual');
  const offer = additionalOffers[0];
  const intentId = await admitForm(lead, offer);
  const cart = await admitCart(lead, offer);
  await createContact(lead, cart.eventId);
  const outside = await evaluate(lead, MANUAL, offer);
  if (outside.allowed || outside.reason_code !== 'pilot_contact_not_in_cohort') {
    throw new Error(`manual_cohort evaluation without cohort: ${JSON.stringify(outside)}`);
  }
  await expectPlanRejection('manual_cohort without cohort', lead,
    () => planCart(lead, offer, cart, MANUAL), 'pilot_contact_not_in_cohort');
  await enroll(lead, MANUAL);
  const plan = one((await planCart(lead, offer, cart, MANUAL)).rows, 'manual plan');
  await expectBinding('manual_cohort', plan, MANUAL, null);
  const data = await dispatch(lead, plan, 'cart_abandonment');
  expectAudienceData('manual_cohort', data, MANUAL, null);
  if (!intentId) throw new Error('manual_cohort: the form did not leave an intent');
  results.manual_cohort = {
    consented_without_cohort: 'pilot_contact_not_in_cohort',
    enrolled: 'accepted',
    control_event_data_keys: Object.keys(data),
  };
}

// ---------------------------------------------------------------------------
// 3. consented_intent_in_cohort: cohorte Y consentimiento.
// ---------------------------------------------------------------------------
{
  const outside = person('cohorte-fuera');
  const outsideOffer = defaultOffer;
  await admitForm(outside, outsideOffer);
  const outsideCart = await admitCart(outside, outsideOffer);
  await createContact(outside, outsideCart.eventId);
  const outsideEval = await evaluate(outside, IN_COHORT, outsideOffer);
  if (outsideEval.allowed || outsideEval.reason_code !== 'pilot_contact_not_in_cohort') {
    throw new Error(`in_cohort evaluation without cohort: ${JSON.stringify(outsideEval)}`);
  }
  await expectPlanRejection('in_cohort consented without cohort', outside,
    () => planCart(outside, outsideOffer, outsideCart, IN_COHORT), 'pilot_contact_not_in_cohort');

  const noForm = person('cohorte-sin-formulario');
  const noFormCart = await admitCart(noForm, defaultOffer);
  await createContact(noForm, noFormCart.eventId);
  await enroll(noForm, IN_COHORT);
  // La evaluacion no mira la intencion; la planificacion la ata al evento.
  const noFormEval = await evaluate(noForm, IN_COHORT, defaultOffer);
  if (!noFormEval.allowed) {
    throw new Error(`in_cohort evaluation of an enrolled contact: ${JSON.stringify(noFormEval)}`);
  }
  await expectPlanRejection('in_cohort enrolled without form', noForm,
    () => planCart(noForm, defaultOffer, noFormCart, IN_COHORT), 'pilot_audience_intent_unresolved');

  const noOptIn = person('cohorte-sin-opt-in');
  await admitForm(noOptIn, defaultOffer, { consented: false });
  const noOptInCart = await admitCart(noOptIn, defaultOffer);
  if (noOptInCart.correlation?.outcome !== 'resolved') {
    throw new Error(`the cart of a form without opt-in did not correlate: ${JSON.stringify(noOptInCart.correlation)}`);
  }
  await createContact(noOptIn, noOptInCart.eventId);
  await enroll(noOptIn, IN_COHORT);
  await expectPlanRejection('in_cohort enrolled with a form without opt-in', noOptIn,
    () => planCart(noOptIn, defaultOffer, noOptInCart, IN_COHORT),
    'pilot_audience_consented_intent_not_authorized');

  const both = person('cohorte-y-consentimiento');
  const bothIntent = await admitForm(both, defaultOffer);
  const bothCart = await admitCart(both, defaultOffer);
  await createContact(both, bothCart.eventId);
  await enroll(both, IN_COHORT);
  const plan = one((await planCart(both, defaultOffer, bothCart, IN_COHORT)).rows, 'in_cohort plan');
  await expectBinding('in_cohort', plan, IN_COHORT, bothIntent);
  const data = await dispatch(both, plan, 'cart_abandonment');
  expectAudienceData('in_cohort', data, IN_COHORT, bothIntent);
  results.consented_intent_in_cohort = {
    consented_without_cohort: 'pilot_contact_not_in_cohort',
    enrolled_without_form: 'pilot_audience_intent_unresolved',
    enrolled_without_opt_in: 'pilot_audience_consented_intent_not_authorized',
    enrolled_and_consented: 'accepted',
  };
}

// ---------------------------------------------------------------------------
// 4. consented_intent: sin cohorte, la intencion consentida del evento.
// ---------------------------------------------------------------------------
const openResults = {};
{
  // Sin formulario: la evaluacion dice allowed (no es un permiso de envio) y
  // la planificacion rechaza.
  const noForm = person('abierto-sin-formulario');
  const noFormCart = await admitCart(noForm, defaultOffer);
  await createContact(noForm, noFormCart.eventId);
  const noFormEval = await evaluate(noForm, OPEN, defaultOffer);
  if (!noFormEval.allowed || noFormEval.reason_code !== 'pilot_scope_allowed') {
    throw new Error(`consented_intent evaluation: ${JSON.stringify(noFormEval)}`);
  }
  openResults.without_form = await expectPlanRejection('open without form', noForm,
    () => planCart(noForm, defaultOffer, noFormCart, OPEN), 'pilot_audience_intent_unresolved');

  const noOptIn = person('abierto-sin-opt-in');
  await admitForm(noOptIn, additionalOffers[0], { consented: false });
  const noOptInCart = await admitCart(noOptIn, additionalOffers[0]);
  await createContact(noOptIn, noOptInCart.eventId);
  openResults.form_without_opt_in = await expectPlanRejection('open with a form without opt-in',
    noOptIn, () => planCart(noOptIn, additionalOffers[0], noOptInCart, OPEN),
    'pilot_audience_consented_intent_not_authorized');

  // El formulario de una oferta y el carrito de otra: la correlacion no los
  // junta, no hay evidencia.
  const otherOffer = person('abierto-otra-oferta');
  await admitForm(otherOffer, defaultOffer);
  const otherOfferCart = await admitCart(otherOffer, additionalOffers[1]);
  if (otherOfferCart.correlation?.outcome !== 'unmatched') {
    throw new Error(`a cart of another offer correlated: ${JSON.stringify(otherOfferCart.correlation)}`);
  }
  await createContact(otherOffer, otherOfferCart.eventId);
  openResults.other_offer = await expectPlanRejection('open with the form of another offer',
    otherOffer, () => planCart(otherOffer, additionalOffers[1], otherOfferCart, OPEN),
    'pilot_audience_intent_unresolved');

  // Defensivo: el evento se correlaciono por email y el llamador escribe a
  // otro telefono.
  const otherPhone = person('abierto-otro-telefono');
  await admitForm(otherPhone, defaultOffer);
  const otherPhoneCart = await admitCart(otherPhone, defaultOffer, { correlationPhone: null });
  if (otherPhoneCart.correlation?.outcome !== 'resolved') {
    throw new Error(`the email-only cart did not correlate: ${JSON.stringify(otherPhoneCart.correlation)}`);
  }
  await createContact(otherPhone, otherPhoneCart.eventId, { phone: '12025559901' });
  openResults.other_phone = await expectPlanRejection('open to another phone', otherPhone,
    () => planCart(otherPhone, defaultOffer, otherPhoneCart, OPEN),
    'pilot_audience_consented_intent_phone_mismatch');

  // La intencion no es de la oferta que dice el llamador, o el mapeo de su
  // oferta se apago despues de correlacionar.
  const mismatch = person('abierto-fuera-del-mapeo');
  await admitForm(mismatch, additionalOffers[1]);
  const mismatchCart = await admitCart(mismatch, additionalOffers[1]);
  await createContact(mismatch, mismatchCart.eventId);
  await expectPlanRejection('open with the offer argument of another offer', mismatch,
    () => planCart(mismatch, additionalOffers[1], mismatchCart, OPEN,
      { offerCode: defaultOffer.offer_code }),
    'pilot_audience_intent_scope_mismatch');
  await db.query(`
    update public.hotmart_purchase_intent_scopes set active = false where offer_ref = $1
  `, [additionalOffers[1].offer_code]);
  try {
    openResults.mapping_inactive = await expectPlanRejection('open with the mapping inactive',
      mismatch, () => planCart(mismatch, additionalOffers[1], mismatchCart, OPEN),
      'pilot_audience_intent_scope_mismatch');
  } finally {
    await db.query(`
      update public.hotmart_purchase_intent_scopes set active = true where offer_ref = $1
    `, [additionalOffers[1].offer_code]);
  }

  // Entra sin cohorte: carrito.
  const cartLead = person('abierto-carrito');
  const cartIntent = await admitForm(cartLead, additionalOffers[1]);
  const cartEvent = await admitCart(cartLead, additionalOffers[1]);
  await createContact(cartLead, cartEvent.eventId);
  const cartPlan = one((await planCart(cartLead, additionalOffers[1], cartEvent, OPEN)).rows,
    'open cart plan');
  await expectBinding('open cart', cartPlan, OPEN, cartIntent);
  expectAudienceData('open cart', await dispatch(cartLead, cartPlan, 'cart_abandonment'),
    OPEN, cartIntent);
  openResults.cart_without_cohort = 'accepted';

  // Entra sin cohorte: pago fallido sin carrito (el permiso lo concede la
  // intencion, A5). Sin opt-in, no entra y no concede nada.
  const failureNoOptIn = person('abierto-pago-sin-opt-in');
  await admitForm(failureNoOptIn, additionalOffers[0], { consented: false });
  const failureNoOptInEvent = await admitFailure(failureNoOptIn, additionalOffers[0]);
  await createContact(failureNoOptIn, failureNoOptInEvent.eventId);
  openResults.payment_failure_without_opt_in = await expectPlanRejection(
    'open payment failure without opt-in', failureNoOptIn,
    () => planFailure(failureNoOptIn, additionalOffers[0], failureNoOptInEvent, OPEN),
    'pilot_audience_consented_intent_not_authorized');

  const failureLead = person('abierto-pago-fallido');
  const failureIntent = await admitForm(failureLead, additionalOffers[0]);
  const failureEvent = await admitFailure(failureLead, additionalOffers[0]);
  await createContact(failureLead, failureEvent.eventId);
  const failurePlan = one((await planFailure(failureLead, additionalOffers[0], failureEvent, OPEN)).rows,
    'open payment failure plan');
  await expectBinding('open payment failure', failurePlan, OPEN, failureIntent);
  expectAudienceData('open payment failure',
    await dispatch(failureLead, failurePlan, 'payment_failure'), OPEN, failureIntent);
  openResults.payment_failure_without_cohort = 'accepted';

  // El tope diario (3) corta: el tercero sale, el cuarto no arranca.
  const third = person('abierto-tercero');
  const thirdIntent = await admitForm(third, defaultOffer);
  const thirdCart = await admitCart(third, defaultOffer);
  await createContact(third, thirdCart.eventId);
  const thirdPlan = one((await planCart(third, defaultOffer, thirdCart, OPEN)).rows, 'third plan');
  expectAudienceData('open third', await dispatch(third, thirdPlan, 'cart_abandonment'),
    OPEN, thirdIntent);
  if ((await startsOf(OPEN)) !== OPEN.perDay) {
    throw new Error(`the open scope did not consume its daily budget: ${await startsOf(OPEN)}`);
  }
  const fourth = person('abierto-cuarto');
  await admitForm(fourth, defaultOffer);
  const fourthCart = await admitCart(fourth, defaultOffer);
  await createContact(fourth, fourthCart.eventId);
  const fourthPlan = one((await planCart(fourth, defaultOffer, fourthCart, OPEN)).rows, 'fourth plan');
  openResults.daily_budget = await expectStartRejection(fourth, fourthPlan, 'cart_abandonment',
    OPEN, 'pilot_daily_budget_exhausted');

  // El consentimiento se pierde entre el plan y el envio: la autorizacion lo
  // ve antes que el tope (que ya esta agotado) y no consume nada.
  const revoked = person('abierto-revocado');
  const revokedIntent = await admitForm(revoked, defaultOffer);
  const revokedCart = await admitCart(revoked, defaultOffer);
  await createContact(revoked, revokedCart.eventId);
  const revokedPlan = one((await planCart(revoked, defaultOffer, revokedCart, OPEN)).rows,
    'revoked plan');
  await expectBinding('open revoked', revokedPlan, OPEN, revokedIntent);
  await db.query(`
    update public.purchase_intents set whatsapp_contact_authorized = false where id = $1
  `, [revokedIntent]);
  openResults.consent_lost_before_start = await expectStartRejection(revoked, revokedPlan,
    'cart_abandonment', OPEN, 'pilot_audience_consented_intent_not_authorized');
  results.consented_intent = openResults;
}

// ---------------------------------------------------------------------------
// 5. La forma del binding: manual sin intencion, los otros dos con intencion.
//    El check corre antes que la clave primaria.
// ---------------------------------------------------------------------------
const anyBinding = one((await db.query(`
  select recovery_case_id, scope_key, scope_version, source_event_id, audience_purchase_intent_id
  from public.pilot_recovery_case_bindings where audience_mode = 'consented_intent' limit 1
`)).rows, 'a consented binding');
for (const [mode, intentId] of [
  ['manual_cohort', anyBinding.audience_purchase_intent_id],
  ['consented_intent', null],
  ['consented_intent_in_cohort', null],
  ['everyone', anyBinding.audience_purchase_intent_id],
]) {
  await expectError(`binding shape ${mode}`,
    () => db.query(`
      insert into public.pilot_recovery_case_bindings
        (recovery_case_id, scope_key, scope_version, source_event_id,
         audience_mode, audience_purchase_intent_id)
      values ($1,$2,$3,$4,$5,$6)
    `, [anyBinding.recovery_case_id, anyBinding.scope_key, anyBinding.scope_version,
      anyBinding.source_event_id, mode, intentId]),
    { code: '23514' });
}
const shapes = (await db.query(`
  select audience_mode, count(*)::integer as count,
         count(audience_purchase_intent_id)::integer as with_intent
  from public.pilot_recovery_case_bindings group by audience_mode order by audience_mode
`)).rows;
for (const shape of shapes) {
  const expected = shape.audience_mode === 'manual_cohort' ? 0 : shape.count;
  if (shape.with_intent !== expected) {
    throw new Error(`binding shape: ${JSON.stringify(shapes)}`);
  }
}
results.binding_shape = Object.fromEntries(shapes.map((shape) => [shape.audience_mode, shape.count]));

console.log(JSON.stringify({ pilot_scope_audience_mode: 'OK', ...results }));
await db.close();
