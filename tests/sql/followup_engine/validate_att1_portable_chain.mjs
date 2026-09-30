// La cadena portable de ATT1 de punta a punta, con la reevaluacion REAL.
//
// Recorre, sobre la cadena completa de migraciones y con las RPC reales:
//   formulario (admit_portable_observed_lead_precheckout, la landing de cada
//   oferta) -> evento de Hotmart (admit_portable_hotmart_*) -> contacto y puntos
//   (lo que deja resolve_event) -> plan del piloto -> claim ->
//   reevaluate_followup_action REAL (nada de followup_action_reevaluated
//   insertado a mano) -> contexto de ejecucion -> reserva approved_template ->
//   mark_*_request_started -> record_and_finalize_followup_acceptance.
//
// Casos:
//   1. con la cohorte vacia, carrito y pago fallido se rechazan con
//      pilot_contact_not_in_cohort y no dejan trabajo durable;
//   2. el carrito de cada una de las tres ofertas llega a la aceptacion;
//   3. el pago fallido sin carrito previo: el permiso lo concede la intencion
//      con consentimiento (20260930000100) y llega a la aceptacion;
//   4. el pago fallido con carrito previo (ya aceptado): usa el permiso del
//      carrito, no suma otro, y llega a la aceptacion;
//   5. despues de cada aceptacion no aparece ninguna accion nueva: la politica
//      de un toque cierra la secuencia (policy_exhausted);
//   6. el riesgo de la politica de dos pasos: con max_automatic_messages = 2 el
//      paso siguiente se elige por posicion y la aceptacion del carrito falla
//      con invalid_next_policy_step. Este validador lo fija.
//
// Datos:
//   - Binding, ofertas, landings, producto, Chatwoot y consentimiento salen de
//     tests/fixtures/instances/att1/instancia.toml (se lee el archivo).
//   - Politica y scope del piloto: los de setter-instancia-att1
//     (despliegue/base/aprovisionar-att1.sql, 2026-09-30): att1-recuperacion v1
//     con att1-recuperacion-un-toque v1, pasos freeform first_contact y
//     payment_failure_first_contact (decision D2), un toque, gracia 0, vence a
//     1 dia, topes 20, lookback de la intencion 24 h.
//     DESVIACION DOCUMENTADA: la ventana de envio es 00:00-23:59 en vez de
//     09:00-21:00 (America/Mexico_City). La puerta de arranque
//     (authorize_lancemos_pilot_request_start) exige p_now a +-5 minutos del
//     reloj de la base, asi que la cadena corre con el reloj real y no puede
//     elegir una hora habil de Ciudad de Mexico.
//   - Carrito: la captura tests/fixtures/hotmart_cart_abandonment_rejected_v1.json
//     con producto y oferta sustituidos (como test_commercial_ally_multi_offer.py),
//     y ademas id, creation_date y comprador: cada caso es otra persona y el
//     evento tiene que caer dentro del lookback de su intencion.
//   - Deuda (A0): no hay PURCHASE_CANCELED ni lead.precheckout capturados, ni el
//     catalogo del inbox 11. El pago fallido usa el precedente inline de
//     validate_commercial_ally_payment_failure_recovery.mjs y el formulario el de
//     validate_commercial_ally_portable_precheckout.mjs, con los valores de ATT1.
//     El texto aceptado es un marcador: la base guarda el que le pasa el bridge.
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
// El manifiesto de ATT1. Lector minimo del subconjunto de TOML que usan los
// campos de aca: tablas, arrays de tablas y claves con texto, entero o booleano.
// Lo demas (arrays, tablas en linea) se ignora; un campo que falte aborta.
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
const defaultOffers = offers.filter((offer) => offer.default);
if (offers.length !== 3 || defaultOffers.length !== 1 || !offers[0].default) {
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

// Valores de aprovisionar-att1.sql de la instancia (no estan en el manifiesto).
const SCOPE = 'att1-recuperacion';
const POLICY = 'att1-recuperacion-un-toque';
const CHANNEL_REF = `chatwoot-inbox:${ATT1.inboxId}`;
const STEPS = [
  { mode: 'freeform', step_key: 'first_contact' },
  { mode: 'freeform', step_key: 'payment_failure_first_contact' },
];

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
    values ($1,$2,$3,$4,$5,interval '24 hours',true)
  `, [ATT1.tenant, ATT1.funnel, String(ATT1.productId), ATT1.hotlink, offer.offer_code]);
}
await db.query(`
  insert into public.followup_policy_versions
    (policy_key, version, status, purpose, timezone, business_windows,
     grace_period, expires_after, max_automatic_messages, steps,
     approved_by, approved_at, published_at)
  values ($1,1,'published','cart_recovery',$2,
          '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]',
          interval '0 seconds',interval '1 day',1,$3::jsonb,
          'operator-test',now(),now())
`, [POLICY, ATT1.timezone, JSON.stringify(STEPS)]);
await db.query(`
  insert into public.pilot_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     channel, channel_provider, channel_account_ref, source, source_event_type,
     additional_source_event_types, external_product_id, offer_code,
     additional_offer_codes, purpose, policy_key, policy_version, timezone,
     max_cohort_contacts, max_outbound_request_starts_total,
     max_outbound_request_starts_per_day, approved_by, approved_at, published_at)
  values ($1,1,'published',$2,$3,$4,'whatsapp','waba',$5,'hotmart',
          'PURCHASE_OUT_OF_SHOPPING_CART','{PURCHASE_CANCELED}',$6,$7,$8::text[],
          'cart_recovery',$9,1,$10,20,20,20,'operator-test',now(),now())
`, [
  SCOPE, ATT1.tenant, ATT1.accountId, ATT1.inboxId, CHANNEL_REF,
  String(ATT1.productId), defaultOffer.offer_code,
  additionalOffers.map((offer) => offer.offer_code), POLICY, ATT1.timezone,
]);
await db.query(`
  insert into public.pilot_runtime_controls
    (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
  values ($1,1,'inactive',0,'operator-test','default-off')
`, [SCOPE]);

const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};
const armed = one((await db.query(`
  select * from public.set_lancemos_pilot_runtime_state($1,1,0,'armed','operator-test','controlled-test')
`, [SCOPE])).rows, 'arm');
const status = one((await db.query(`
  select * from public.get_lancemos_pilot_runtime_status($1,1,$2,'waba',$3)
`, [SCOPE, ATT1.tenant, CHANNEL_REF])).rows, 'runtime status');
if (armed.runtime_state !== 'armed' || status.configured !== true || status.runtime_state !== 'armed') {
  throw new Error(`the ATT1 pilot is not configured and armed: ${JSON.stringify({ armed, status })}`);
}

// ---------------------------------------------------------------------------
// Tiempos: todo sale del reloj de la base (los permisos nacen con
// clock_timestamp() y la puerta de arranque exige +-5 minutos).
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
    name: `Compradora ATT1 ${label}`,
    email: `att1-chain-${suffix}@example.test`,
    phone: `120255501${suffix}`,
  };
};

// Formulario de la landing de la oferta (precedente inline del contrato).
const admitForm = async (lead, offer) => {
  const id = `att1-chain-form-${lead.email}-${offer.offer_code}`;
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
      consent: {
        marketing_optin: true, whatsapp_contact: true, copy_version: ATT1.copyVersion,
      },
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
  const intent = one((await db.query(`
    select landing_ref, offer_ref, whatsapp_contact_authorized, activation_authorized
    from public.purchase_intents where id = $1
  `, [admitted.purchase_intent_id])).rows, `${lead.label} intent`);
  if (admitted.outcome !== 'inserted'
      || intent.landing_ref !== offer.landing_id
      || intent.offer_ref !== offer.offer_code
      || !intent.whatsapp_contact_authorized || !intent.activation_authorized) {
    throw new Error(`${lead.label}: the form of ${offer.landing_id} did not leave its consented intent: ${JSON.stringify({ admitted, intent })}`);
  }
  return admitted.purchase_intent_id;
};

// Carrito capturado, con producto, oferta, id, fecha y comprador sustituidos.
const admitCart = async (lead, offer) => {
  const payload = structuredClone(CAPTURED_CART);
  payload.id = `att1-chain-cart-${lead.email}`;
  payload.creation_date = CART_AT.getTime();
  payload.data.product = { id: ATT1.productId, name: ATT1.productName };
  payload.data.offer = { code: offer.offer_code };
  payload.data.buyer = { name: lead.name, email: lead.email, phone: lead.phone };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
    JSON.stringify(payload), lead.email, lead.phone])).rows, `${lead.label} cart`);
  const provenance = one((await db.query(`
    select offer_ref from public.commercial_ally_hotmart_event_bindings where webhook_event_id = $1
  `, [admitted.webhook_event_id])).rows, `${lead.label} cart provenance`);
  if (admitted.outcome !== 'inserted' || provenance.offer_ref !== offer.offer_code) {
    throw new Error(`${lead.label}: cart of ${offer.offer_code} was not admitted: ${JSON.stringify({ admitted, provenance })}`);
  }
  return { eventId: admitted.webhook_event_id, abandonedAt: new Date(payload.creation_date) };
};

// Pago fallido (precedente inline, sin captura).
const admitFailure = async (lead, offer, transaction) => {
  const payload = {
    id: `att1-chain-failure-${lead.email}`,
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
const enroll = async (lead) => {
  const { generation } = one((await db.query(`
    select generation from public.pilot_runtime_controls where scope_key = $1
  `, [SCOPE])).rows, 'scope generation');
  const member = one((await db.query(`
    select * from public.set_lancemos_pilot_cohort_member($1,1,$2,$3,'active','operator-test','controlled-test')
  `, [SCOPE, lead.contact, generation])).rows, `${lead.label} enrollment`);
  if (member.member_status !== 'active') {
    throw new Error(`${lead.label} was not enrolled: ${JSON.stringify(member)}`);
  }
};

// Los mismos argumentos que arma resolution.resolve_event con el binding portable.
const planCart = (lead, offer, cart) => db.query(`
  select * from public.plan_lancemos_pilot_cart_recovery(
    $1,$2,$3,$4,$5,$6,1,$7,$8,$9,$10,$11,1
  )
`, [cart.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  offer.offer_code, POLICY, cart.abandonedAt.toISOString(), ATT1.accountId,
  ATT1.inboxId, lead.phone, SCOPE]);
const planFailure = (lead, offer, failure) => db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,$3,$4,$5,$6,1,$7,$8,$9,$10,$11,1
  )
`, [failure.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  offer.offer_code, POLICY, failure.failedAt.toISOString(), ATT1.accountId,
  ATT1.inboxId, lead.phone, SCOPE]);

const expectPilotRejection = async (label, action, reason) => {
  let error = null;
  await db.exec('begin');
  try {
    await action();
  } catch (caught) {
    error = caught;
  } finally {
    await db.exec('rollback');
  }
  if (error?.code !== '55000'
      || error?.message !== 'pilot_scope_rejected'
      || error?.detail !== reason) {
    throw new Error(`${label}: expected pilot_scope_rejected:${reason}, got ${error?.code} ${error?.message} ${error?.detail}`);
  }
};
const authorizationsOf = async (lead) => (await db.query(`
  select authorization_status, authorization_source, evidence
  from public.contact_authorizations
  where contact_id = $1 and channel = 'whatsapp' and purpose = 'cart_recovery'
  order by recorded_at, id
`, [lead.contact])).rows;
const openActions = async () => (await db.query(`
  select id from public.scheduled_actions
  where status in ('pending','deferred','retryable_failed')
`)).rows;

// Lo que elige el bridge en modo directo: la reserva va en approved_template
// (el scope del piloto es waba) y el arranque del envio sale del anchor_type de
// la accion reclamada, con la misma regla que
// SupabaseClient.mark_followup_request_started con la frontera del piloto.
// tests/test_durable_dispatcher_approved_template.py lee estas dos
// definiciones y las compara con lo que manda el SupabaseClient real del
// DurableDispatcher: si una capa cambia el modo o la RPC, la otra se entera.
const DIRECT_DELIVERY_MODE = 'approved_template';
const startOperationFor = (anchorType) => (anchorType === 'payment_failure'
  ? 'mark_portable_payment_failure_request_started'
  : 'mark_lancemos_pilot_request_started');

// La cadena del dispatcher para una accion recien planificada. Es la unica
// accion viva de la base: el claim la tiene que devolver sola.
let messageNumber = 0;
const dispatch = async (lead, plan, { anchor, stepKey, offer, beforeAcceptance }) => {
  const worker = `att1-chain-${lead.label}`;
  const now = await dbNow();
  const claimed = (await db.query(`
    select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
  `, [worker, now])).rows;
  if (claimed.length !== 1 || claimed[0].id !== plan.scheduled_action_id) {
    throw new Error(`${lead.label}: claimed ${JSON.stringify(claimed.map((row) => row.id))}, expected ${plan.scheduled_action_id}`);
  }
  const lease = claimed[0].lease_generation;
  // El anchor_type lo escribe el planificador; el bridge elige la RPC de
  // arranque con el, no con lo que el test cree que planifico.
  if (claimed[0].anchor_type !== anchor) {
    throw new Error(`${lead.label}: claimed anchor_type ${claimed[0].anchor_type}, expected ${anchor}`);
  }
  // Primer contacto sin conversacion: sin evidencia de Chatwoot.
  const decision = one((await db.query(`
    select * from public.reevaluate_followup_action($1,$2,$3,$4)
  `, [plan.scheduled_action_id, worker, lease, now])).rows, `${lead.label} reevaluation`);
  if (decision.decision !== 'execute' || decision.reason_code !== 'eligible_for_execution') {
    throw new Error(`${lead.label}: the real reevaluation did not execute: ${JSON.stringify(decision)}`);
  }
  const context = one((await db.query(`
    select * from public.get_followup_execution_context($1,$2,$3,$4)
  `, [plan.scheduled_action_id, worker, lease, now])).rows, `${lead.label} context`);
  // El dispatcher autoriza al destinatario portable con la oferta del caso
  // (worker._is_authorized_followup_recipient contra accepted_offer_codes).
  if (context.step_key !== stepKey
      || context.action_type !== 'first_contact_review'
      || context.offer_code !== offer.offer_code
      || context.buyer_phone !== lead.phone
      || context.product_name !== ATT1.productName) {
    throw new Error(`${lead.label}: execution context diverged: ${JSON.stringify(context)}`);
  }
  // El worker reserva en approved_template porque el scope del piloto es waba.
  const attempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt(
      $1,$2,$3,$4,$5,'whatsapp',$6,$7
    )
  `, [plan.scheduled_action_id, worker, lease, decision.case_version,
    decision.sequence_revision, DIRECT_DELIVERY_MODE, now])).rows,
  `${lead.label} reservation`);
  const startOperation = startOperationFor(claimed[0].anchor_type);
  const started = one((await db.query(`
    select * from public.${startOperation}($1,$2,$3,$4,$5)
  `, [plan.scheduled_action_id, attempt.id, worker, lease, await dbNow()])).rows,
  `${lead.label} request start`);
  if (started.phase !== 'request_started'
      || started.mode !== DIRECT_DELIVERY_MODE
      || started.pilot_authorization_id == null
      || started.pilot_authorization_replayed !== false) {
    throw new Error(`${lead.label}: ${startOperation} did not start: ${JSON.stringify(started)}`);
  }
  const acceptance = {
    actionId: plan.scheduled_action_id, attemptId: attempt.id, worker, lease,
  };
  if (beforeAcceptance) await beforeAcceptance(acceptance);
  messageNumber += 1;
  const accepted = one((await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
  `, [plan.scheduled_action_id, attempt.id, worker, lease, String(900100 + messageNumber),
    `att1-chain-wamid-${messageNumber}`,
    `Plantilla aprobada de ATT1 para ${lead.name} (${ATT1.productName})`,
    await dbNow()])).rows, `${lead.label} acceptance`);
  if (accepted.status !== 'accepted_by_chatwoot') {
    throw new Error(`${lead.label}: acceptance did not finalize: ${JSON.stringify(accepted)}`);
  }

  // Despues de aceptar no aparece ninguna accion nueva: un toque.
  const caseActions = (await db.query(`
    select id, status from public.scheduled_actions where recovery_case_id = $1
  `, [plan.recovery_case_id])).rows;
  const sequence = one((await db.query(`
    select status, completion_reason, automatic_messages_accepted
    from public.followup_sequences where id = $1
  `, [plan.followup_sequence_id])).rows, `${lead.label} sequence`);
  const recoveryCase = one((await db.query(`
    select status, conversation_id from public.recovery_cases where id = $1
  `, [plan.recovery_case_id])).rows, `${lead.label} case`);
  if (caseActions.length !== 1
      || caseActions[0].status !== 'accepted_by_chatwoot'
      || sequence.status !== 'completed'
      || sequence.completion_reason !== 'policy_exhausted'
      || sequence.automatic_messages_accepted !== 1
      || recoveryCase.status !== 'sequence_exhausted'
      || recoveryCase.conversation_id === null
      || (await openActions()).length !== 0) {
    throw new Error(`${lead.label}: acceptance left a new action or an open sequence: ${JSON.stringify({ caseActions, sequence, recoveryCase })}`);
  }
  const later = (await db.query(`
    select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
  `, [`${worker}-later`, new Date((await dbNow()).getTime() + 2 * 24 * 3_600_000)])).rows;
  if (later.length !== 0) {
    throw new Error(`${lead.label}: a claim two days later found work: ${JSON.stringify(later)}`);
  }
  return { decision, context, started };
};

// ---------------------------------------------------------------------------
// 1. Cohorte vacia: el piloto esta armado pero nadie esta inscripto. Carrito y
//    pago fallido se rechazan con el motivo que A1 guarda en el evento, y no
//    queda ni caso ni permiso.
// ---------------------------------------------------------------------------
const outsider = person('fuera-de-cohorte');
const outsiderOffer = additionalOffers[1];
await admitForm(outsider, outsiderOffer);
const outsiderCart = await admitCart(outsider, outsiderOffer);
const outsiderFailure = await admitFailure(outsider, outsiderOffer, 'HPATT1CHAINOUT');
await createContact(outsider, outsiderCart.eventId);
const members = one((await db.query(`
  select count(*)::integer as count from public.pilot_cohort_memberships where scope_key = $1
`, [SCOPE])).rows, 'cohort size');
if (members.count !== 0) throw new Error(`the cohort is not empty: ${members.count}`);
await expectPilotRejection('cart with an empty cohort',
  () => planCart(outsider, outsiderOffer, outsiderCart), 'pilot_contact_not_in_cohort');
await expectPilotRejection('payment failure with an empty cohort',
  () => planFailure(outsider, outsiderOffer, outsiderFailure), 'pilot_contact_not_in_cohort');
const outsiderEffects = one((await db.query(`
  select
    (select count(*)::integer from public.recovery_cases where contact_id = $1) as cases,
    (select count(*)::integer from public.contact_authorizations where contact_id = $1) as grants,
    (select count(*)::integer from public.scheduled_actions) as actions
`, [outsider.contact])).rows, 'outsider effects');
if (outsiderEffects.cases !== 0 || outsiderEffects.grants !== 0 || outsiderEffects.actions !== 0) {
  throw new Error(`a rejected plan left durable work: ${JSON.stringify(outsiderEffects)}`);
}

// ---------------------------------------------------------------------------
// 2. El carrito de cada oferta, cada uno desde su landing. En el primero se
//    fija el riesgo de la politica de dos pasos (punto 0.10 del plan).
// ---------------------------------------------------------------------------
const cartResults = [];
for (const [index, offer] of offers.entries()) {
  const lead = person(`carrito-${offer.offer_code}`);
  await admitForm(lead, offer);
  const cart = await admitCart(lead, offer);
  await createContact(lead, cart.eventId);
  await enroll(lead);
  const plan = one((await planCart(lead, offer, cart)).rows, `${lead.label} plan`);
  const grants = await authorizationsOf(lead);
  if (!plan.created
      || grants.length !== 1
      || grants[0].authorization_status !== 'allowed'
      || grants[0].evidence.reason !== 'cart_abandonment') {
    throw new Error(`${lead.label}: cart plan did not create the case and its permission: ${JSON.stringify({ plan, grants })}`);
  }
  const beforeAcceptance = index !== 0 ? undefined : async ({ actionId, attemptId, worker, lease }) => {
    // Con max_automatic_messages = 2 (una v2 de la politica con los mismos
    // pasos), el paso siguiente sale por posicion: payment_failure_first_contact,
    // que no tiene delay, y la aceptacion falla. Se simula en la secuencia
    // (que copia el tope al planificar) y se revierte.
    let error = null;
    await db.exec('begin');
    try {
      await db.query(`
        update public.followup_sequences set max_attempts = 2 where id = $1
      `, [plan.followup_sequence_id]);
      await db.query(`
        select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,'900099','att1-chain-wamid-risk','riesgo',$5)
      `, [actionId, attemptId, worker, lease, await dbNow()]);
    } catch (caught) {
      error = caught;
    } finally {
      await db.exec('rollback');
    }
    if (error?.message !== 'invalid_next_policy_step') {
      throw new Error(`the two-step policy with two messages did not fail on the next step: ${error?.message}`);
    }
  };
  const run = await dispatch(lead, plan, {
    anchor: 'cart_abandonment', stepKey: 'first_contact', offer, beforeAcceptance,
  });
  cartResults.push(`${offer.offer_code}:${run.decision.reason_code}`);
}

// ---------------------------------------------------------------------------
// 3. Pago fallido sin carrito previo, por la landing .mx (2uafw5bg): el
//    permiso lo concede la intencion con consentimiento.
// ---------------------------------------------------------------------------
const failureOnly = person('pago-fallido-sin-carrito');
const failureOnlyOffer = additionalOffers[1];
const failureOnlyIntent = await admitForm(failureOnly, failureOnlyOffer);
const failureOnlyEvent = await admitFailure(failureOnly, failureOnlyOffer, 'HPATT1CHAINPF1');
if (failureOnlyEvent.intentId !== failureOnlyIntent) {
  throw new Error('payment failure did not correlate with the form intent of its landing');
}
await createContact(failureOnly, failureOnlyEvent.eventId);
await enroll(failureOnly);
const failureOnlyPlan = one((await planFailure(failureOnly, failureOnlyOffer, failureOnlyEvent)).rows,
  'payment failure plan');
const failureOnlyGrants = await authorizationsOf(failureOnly);
if (!failureOnlyPlan.created
    || failureOnlyGrants.length !== 1
    || failureOnlyGrants[0].authorization_status !== 'allowed'
    || failureOnlyGrants[0].authorization_source !== 'system'
    || failureOnlyGrants[0].evidence.reason !== 'precheckout_whatsapp_consent'
    || failureOnlyGrants[0].evidence.purchase_intent_id !== failureOnlyIntent
    || failureOnlyGrants[0].evidence.consent_copy_version !== ATT1.copyVersion) {
  throw new Error(`payment failure without a cart did not get the consented permission: ${JSON.stringify({ failureOnlyPlan, failureOnlyGrants })}`);
}
const failureOnlyRun = await dispatch(failureOnly, failureOnlyPlan, {
  anchor: 'payment_failure', stepKey: 'payment_failure_first_contact', offer: failureOnlyOffer,
});

// ---------------------------------------------------------------------------
// 4. Pago fallido con carrito previo (org-a, bmaztyhg): el carrito se manda y
//    se acepta primero; el pago fallido abre otro caso, usa el permiso del
//    carrito y no suma otro.
// ---------------------------------------------------------------------------
const both = person('carrito-y-pago-fallido');
const bothOffer = additionalOffers[0];
const bothIntent = await admitForm(both, bothOffer);
const bothCart = await admitCart(both, bothOffer);
await createContact(both, bothCart.eventId);
await enroll(both);
const bothCartPlan = one((await planCart(both, bothOffer, bothCart)).rows, 'cart before failure plan');
await dispatch(both, bothCartPlan, { anchor: 'cart_abandonment', stepKey: 'first_contact', offer: bothOffer });
const bothFailure = await admitFailure(both, bothOffer, 'HPATT1CHAINPF2');
if (bothFailure.intentId !== bothIntent) {
  throw new Error('payment failure after a cart did not correlate with the same intent');
}
const bothFailurePlan = one((await planFailure(both, bothOffer, bothFailure)).rows,
  'payment failure after cart plan');
const bothGrants = await authorizationsOf(both);
if (!bothFailurePlan.created
    || bothFailurePlan.recovery_case_id === bothCartPlan.recovery_case_id
    || bothGrants.length !== 1
    || bothGrants[0].authorization_source !== 'hotmart'
    || bothGrants[0].evidence.reason !== 'cart_abandonment') {
  throw new Error(`payment failure after a cart did not reuse the cart permission: ${JSON.stringify({ bothFailurePlan, bothGrants })}`);
}
const bothRun = await dispatch(both, bothFailurePlan, {
  anchor: 'payment_failure', stepKey: 'payment_failure_first_contact', offer: bothOffer,
});

// Cada envio consumio exactamente una autorizacion del piloto.
const starts = one((await db.query(`
  select count(*)::integer as count from public.pilot_outbound_request_authorizations
  where scope_key = $1
`, [SCOPE])).rows, 'pilot authorizations');
if (starts.count !== 6) {
  throw new Error(`expected six pilot request starts, got ${starts.count}`);
}

console.log(JSON.stringify({
  att1_portable_chain: 'OK',
  empty_cohort: 'pilot_scope_rejected:pilot_contact_not_in_cohort',
  carts: cartResults,
  payment_failure_without_cart: failureOnlyRun.decision.reason_code,
  payment_failure_after_cart: bothRun.decision.reason_code,
  pilot_request_starts: starts.count,
  two_message_policy_risk: 'invalid_next_policy_step',
}));
await db.close();
