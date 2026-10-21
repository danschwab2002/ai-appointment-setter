// La consulta captura-paridad-recuperacion-v3 (la T2.2 del motor de flujos)
// sobre la cadena real, con un carrito de tres toques sembrado por las RPC de
// hoy.
//
// Especificacion: «T2.2: la captura v3 y la paridad del carrito de tres
// toques» (2026-10-21, en el vault). La consulta es
// tests/fixtures/flow_engine/att1_recovery_capture_query_v3.sql, tal cual la
// corre Andy en att1-db.
//
// Prueba:
//   0. sin la activacion de la v7 de att1-recuperacion la consulta falla
//      entera (no devuelve nada a medias);
//   1. con la v7 activa y armada, cuatro carritos que llegan por la cadena de
//      hoy (el formulario con consentimiento, el carrito capturado, el plan
//      del piloto, el claim, la reevaluacion, el intento, el arranque y la
//      aceptacion):
//      - «tres»: A1 aceptado, A2 aceptado y Z pendiente, con los dos cupones;
//      - «contesta»: A1 aceptado, la respuesta adoptada y A2 cancelado por
//        prospect_replied;
//      - «compra»: A1 aceptado con nuestro link, y la compra con ese sck gana
//        el caso y cancela A2;
//      - «renueva»: A1 y A2 aceptados, «Quiero mi 10 %» adelanta Z, y Z sale;
//      mas un carrito de la siembra (llego antes de la activacion);
//   2. la salida: una fila, un JSON, con las secciones de la v3, los conteos
//      recontados desde las filas, los pasos de la politica con sus marcas,
//      las secuencias, los cupones, el link comprado, la renovacion que
//      adelanto Z y las respuestas solo como hora;
//   3. la privacidad: ningun nombre, mail, telefono (en ninguna de sus formas),
//      codigo de cupon, URL, sck, ulid, transaccion, texto de mensaje ni nombre
//      de operador sembrado aparece en la salida; ninguna arroba, ningun
//      https:// y ningun hermes~.
//
// Con --salida <ruta> escribe la captura a ese archivo (para correrle la
// prueba de forma de Python: scripts/flow_engine_capture_fixture.py
// --check-privacy <ruta>). Sin opciones solo imprime el resultado.
//
// Datos: el armado de validate_recovery_followup_steps.mjs (el manifiesto de
// ATT1, el binding, el formulario y el carrito capturado), con el scope del
// carrito con su nombre de produccion (att1-recuperacion) en la v6 (un toque)
// y la v7 (tres toques). Desviaciones de produccion: los delays de A2 y Z son
// de 1 y 2 segundos (en produccion, 24 y 72 horas); la ventana es de 00:00 a
// 23:59; la gracia es 0; y un paso que tiene que esperar "su dia" se corre a
// manana con un update (postpone).
import { PGlite } from '@electric-sql/pglite';
import { randomBytes } from 'node:crypto';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
let OUT = null;
if (args.length === 2 && args[0] === '--salida') {
  [, OUT] = args;
} else if (args.length !== 0) {
  console.error('uso: node validate_flow_engine_capture_v3.mjs [--salida <ruta>]');
  process.exit(2);
}

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const QUERY = readFileSync(join(root, 'tests/fixtures/flow_engine/att1_recovery_capture_query_v3.sql'), 'utf8');
const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  grant usage on schema public to service_role;
`);
const apply = async (file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
));
await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort()) {
  await apply(join(root, 'supabase/migrations', name));
}

const results = {};
const fail = (message, detail) => {
  throw new Error(`${message}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
};
const one = (rows, label) => {
  if (rows.length !== 1) fail(`${label}: se esperaba una fila y hubo ${rows.length}`);
  return rows[0];
};
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
// La consulta corre con la sesion en UTC, como en att1-db (PGOPTIONS con
// -c timezone=UTC): la forma de Python pide las horas en UTC.
const capture = async () => {
  await db.exec("set timezone to 'UTC'");
  const rows = (await db.query(QUERY)).rows;
  const row = one(rows, 'la consulta v3');
  const keys = Object.keys(row);
  if (keys.length !== 1) fail('la consulta v3 devuelve mas de una columna', keys);
  return row[keys[0]];
};

// ---------------------------------------------------------------------------
// El armado de ATT1 (copiado de validate_recovery_followup_steps.mjs).
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
  if (value === undefined || value === null || value === '') fail(`al fixture de ATT1 le falta ${label}`);
  return value;
};
const manifest = readManifest(readFileSync(join(root, 'tests/fixtures/instances/att1/instancia.toml'), 'utf8'));
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
const PILOT = JSON.parse(readFileSync(join(root, 'tests/fixtures/instances/att1/politica-piloto.json'), 'utf8'));
const cartScope = required(PILOT.pilot_scope, 'politica-piloto.pilot_scope');
const cartPolicy = required(PILOT.policy, 'politica-piloto.policy');
const CHANNEL_PROVIDER = required(cartScope.channel_provider, 'pilot_scope.channel_provider');
const CHANNEL_REF = `${required(cartScope.channel_account_ref_prefix, 'pilot_scope.channel_account_ref_prefix')}${ATT1.inboxId}`;
const ALL_DAY = required(cartPolicy.business_windows, 'policy.business_windows')
  .map((window) => ({ ...window, start: '00:00', end: '23:59' }));
const OPERATOR = 'operador-v3-prueba';
const OPERATOR_REASON = 'razon-del-operador-v3';

// El scope del carrito con su nombre de produccion: la v6 de un toque y la v7
// de tres (la del contrato, con los delays cortos de la cabecera).
const SCOPE = 'att1-recuperacion';
const ONE_TOUCH = { policy: 'att1-recuperacion-un-toque', steps: [{ step_key: 'first_contact', mode: 'freeform' }], max: 1 };
const THREE_TOUCHES = {
  policy: 'att1-carrito-tres-toques',
  steps: [
    { step_key: 'first_contact', mode: 'freeform' },
    { step_key: 'cart_second_touch', mode: 'freeform', delay: '1 second', approved_template: true },
    {
      step_key: 'cart_last_touch', mode: 'freeform', delay: '2 seconds',
      approved_template: true, coupon_ordinal: 2,
    },
  ],
  max: 3,
};
const FLOW = { scope: SCOPE, version: 7, policy: THREE_TOUCHES.policy };

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
await db.query(`
  insert into public.inbound_commercial_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     external_product_id, offer_code, approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5,$6,$7,$8,now(),now())
`, [ATT1.inboundScope, ATT1.inboundVersion, ATT1.tenant, ATT1.accountId, ATT1.inboxId,
  ATT1.hotlink, defaultOffer.offer_code, OPERATOR]);
for (const offer of offers) {
  await db.query(`
    insert into public.checkout_offer_catalog (
      tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref,
      offer_code, checkout_base_url, checkout_mode, default_for_inbound, status,
      version, approved_by, approved_at
    ) values ($1,$2,$3,$4,$5,$6,$7,$8,10,$9,'active',1,$10,now())
  `, [ATT1.tenant, ATT1.funnel, ATT1.inboundScope, ATT1.inboundVersion, ATT1.hotlink,
    offer.landing_id, offer.offer_code, `https://pay.hotmart.com/${ATT1.hotlink}`,
    offer === defaultOffer, OPERATOR]);
}
for (const policy of [ONE_TOUCH, THREE_TOUCHES]) {
  await db.query(`
    insert into public.followup_policy_versions
      (policy_key, version, status, purpose, timezone, business_windows,
       grace_period, expires_after, max_automatic_messages, steps,
       approved_by, approved_at, published_at)
    values ($1,1,'published','cart_recovery',$2,$3::jsonb,interval '0 seconds',
            interval '5 days',$4,$5::jsonb,$6,now(),now())
  `, [policy.policy, ATT1.timezone, JSON.stringify(ALL_DAY), policy.max,
    JSON.stringify(policy.steps), OPERATOR]);
}
for (const [version, policy] of [[6, ONE_TOUCH], [7, THREE_TOUCHES]]) {
  await db.query(`
    insert into public.pilot_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
       channel, channel_provider, channel_account_ref, source, source_event_type,
       additional_source_event_types, external_product_id, offer_code,
       additional_offer_codes, purpose, policy_key, policy_version, timezone,
       max_cohort_contacts, max_outbound_request_starts_total,
       max_outbound_request_starts_per_day, approved_by, approved_at, published_at)
    values ($1,$2,'published',$3,$4,$5,'whatsapp',$6,$7,'hotmart',
            'PURCHASE_OUT_OF_SHOPPING_CART','{}'::text[],$8,$9,$10::text[],
            'cart_recovery',$11,1,$12,20,50,50,$13,now(),now())
  `, [SCOPE, version, ATT1.tenant, ATT1.accountId, ATT1.inboxId, CHANNEL_PROVIDER, CHANNEL_REF,
    String(ATT1.productId), defaultOffer.offer_code,
    additionalOffers.map((offer) => offer.offer_code), policy.policy, ATT1.timezone, OPERATOR]);
}
await db.query(`
  insert into public.pilot_runtime_controls
    (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
  values ($1,6,'inactive',0,$2,$3)
`, [SCOPE, OPERATOR, OPERATOR_REASON]);
const generationOf = async () => one((await db.query(`
  select generation from public.pilot_runtime_controls where scope_key = $1
`, [SCOPE])).rows, 'generacion del control').generation;
const setState = async (version, state) => db.query(`
  select * from public.set_lancemos_pilot_runtime_state($1,$2,$3,$4,$5,$6)
`, [SCOPE, version, await generationOf(), state, OPERATOR, OPERATOR_REASON]);
await setState(6, 'armed');

// ---------------------------------------------------------------------------
// 0. Sin la activacion de la v7, la consulta falla entera.
// ---------------------------------------------------------------------------
{
  let error = null;
  try {
    await capture();
  } catch (caught) {
    error = caught;
  }
  if (error === null || error.code !== '22007' || !String(error.message).includes('captura_sin_activacion')) {
    fail('sin la activacion de la v7 la consulta no fallo como se espera',
      error ? { code: error.code } : 'devolvio una fila');
  }
  results.sin_activacion = error.code;
}

// ---------------------------------------------------------------------------
// Las personas, el formulario, el carrito y el plan.
// ---------------------------------------------------------------------------
const dbNow = async () => new Date((await db.query('select clock_timestamp() as now')).rows[0].now);
const baseMs = Math.floor((await dbNow()).getTime() / 1000) * 1000;
const at = (minutes) => new Date(baseMs + minutes * 60_000);
const CAPTURED_CART = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/hotmart_cart_abandonment_rejected_v1.json'), 'utf8',
)).payload;
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
const asService = async (run) => {
  await db.exec('set role service_role');
  try {
    return await run();
  } finally {
    await db.exec('reset role');
  }
};

// Todo lo que se siembra y no puede salir: se junta aca a medida que se crea.
const SECRETS = new Set([OPERATOR, OPERATOR_REASON]);
let personIndex = 0;
const person = (label) => {
  personIndex += 1;
  const suffix = String(personIndex).padStart(2, '0');
  const lead = {
    label,
    name: `Compradora Captura Tres ${label}`,
    email: `att1-v3-${suffix}@example.test`,
    phone: `120255503${suffix}`,
    offer: additionalOffers[0],
    chatwoot: String(930000 + personIndex),
  };
  for (const secret of [lead.name, lead.email, lead.phone, `+${lead.phone}`, lead.phone.slice(1)]) {
    SECRETS.add(secret);
  }
  return lead;
};
const admitForm = async (lead) => {
  const { offer } = lead;
  const id = `att1-v3-form-${lead.email}`;
  const submittedAt = at(-50).toISOString();
  const pageUrl = `https://${offer.page_host}${offer.page_path}`;
  const checkoutUrl = `https://pay.hotmart.com/${ATT1.hotlink}?off=${offer.offer_code}&checkoutMode=10`;
  const raw = {
    id,
    event: 'lead.precheckout',
    version: '1.1.0',
    created_at: submittedAt,
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
    submitted_at: submittedAt,
    source: {
      tenant_ref: ATT1.tenant, funnel_ref: ATT1.funnel, landing_ref: offer.landing_id,
      page_url: pageUrl, aliado: ATT1.brand,
    },
    identity: { email: lead.email, phone: lead.phone, phone_valid: true, phone_country_iso: 'US' },
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
    JSON.stringify(canonical)])).rows, `${lead.label} formulario`);
  if (admitted.outcome !== 'inserted') fail(`${lead.label} formulario`, admitted.outcome);
};
const admitCart = async (lead, number, createdAt) => {
  const payload = structuredClone(CAPTURED_CART);
  payload.id = `att1-v3-cart-${number}-${lead.email}`;
  payload.creation_date = createdAt.getTime();
  payload.data.product = { id: ATT1.productId, name: ATT1.productName };
  payload.data.offer = { code: lead.offer.offer_code };
  payload.data.buyer = { name: lead.name, email: lead.email, phone: lead.phone };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
    JSON.stringify(payload), lead.email, lead.phone])).rows, `${lead.label} carrito ${number}`);
  if (admitted.outcome !== 'inserted') fail(`${lead.label} carrito ${number}`, admitted.outcome);
  return { eventId: admitted.webhook_event_id, abandonedAt: createdAt };
};
const start = async (label) => {
  const lead = person(label);
  await admitForm(lead);
  const cart = await admitCart(lead, 1, at(-30));
  lead.contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone, country_iso)
    values ($1,$2,$3,'MX') returning id
  `, [lead.name, lead.email, lead.phone])).rows, `${label} contacto`).id;
  await db.query(`
    insert into public.contact_points
      (contact_id, type, raw_value, normalized_value, source, source_event_id)
    values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)
  `, [lead.contact, lead.email, lead.phone, cart.eventId]);
  const member = one((await db.query(`
    select * from public.set_lancemos_pilot_cohort_member($1,$2,$3,$4,'active',$5,$6)
  `, [SCOPE, FLOW.version, lead.contact, await generationOf(), OPERATOR, OPERATOR_REASON])).rows,
  `${label} cohorte`);
  if (member.member_status !== 'active') fail(`${label} cohorte`, member);
  lead.plan = one((await db.query(`
    select * from public.plan_lancemos_pilot_cart_recovery(
      $1,$2,$3,$4,$5,$6,1,$7,$8,$9,$10,$11,$12
    )
  `, [cart.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
    lead.offer.offer_code, FLOW.policy, cart.abandonedAt.toISOString(), ATT1.accountId,
    ATT1.inboxId, lead.phone, SCOPE, FLOW.version])).rows, `${label} plan`);
  if (!lead.plan.created) fail(`${label} plan`, lead.plan);
  return lead;
};
const actionOf = async (id) => one((await db.query(`
  select * from public.scheduled_actions where id = $1
`, [id])).rows, 'accion');
const stepOf = async (lead, stepKey) => actionOf(one((await db.query(`
  select id from public.scheduled_actions where recovery_case_id = $1 and step_key = $2
`, [lead.plan.recovery_case_id, stepKey])).rows, `${lead.label} ${stepKey}`).id);
const evidence = (lead, now, { replied = false } = {}) => ({
  p_chatwoot_checked: true,
  p_chatwoot_conversation_id: lead.chatwoot,
  p_chatwoot_checkpoint_message_id: '1',
  p_chatwoot_checkpoint_at: now.toISOString(),
  p_chatwoot_status: 'open',
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
    `, [actionId, worker, lease, now])).rows, 'reevaluacion');
  }
  const names = Object.keys(chatwoot);
  const list = names.map((name, index) => `${name} => $${index + 5}`).join(', ');
  return one((await db.query(`
    select * from public.reevaluate_followup_action(
      p_action_id => $1, p_worker_id => $2, p_lease_generation => $3, p_now => $4, ${list}
    )
  `, [actionId, worker, lease, now, ...names.map((name) => chatwoot[name])])).rows, 'reevaluacion');
};
// El claim toma todo lo vencido: se deja vencida una sola accion a la vez.
const claim = async (lead, actionId) => {
  const worker = `v3-worker-${lead.label}`;
  const now = await dbNow();
  const claimed = (await db.query(`
    select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
  `, [worker, now])).rows;
  if (claimed.length !== 1 || claimed[0].id !== actionId) {
    fail(`${lead.label}: el claim tomo otra cosa`, claimed.map((row) => row.step_key));
  }
  return { worker, now, lease: claimed[0].lease_generation };
};
let messageNumber = 0;
const send = async (lead, actionId, { chatwoot = null, beforeStart, afterStart } = {}) => {
  const fence = await claim(lead, actionId);
  const decision = await reevaluate(actionId, fence.worker, fence.lease, fence.now,
    chatwoot ? chatwoot(fence.now) : null);
  if (decision.decision !== 'execute') fail(`${lead.label} reevaluacion`, decision);
  const attempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
  `, [actionId, fence.worker, fence.lease, decision.case_version, decision.sequence_revision,
    fence.now])).rows, 'intento');
  if (beforeStart) await beforeStart({ ...fence, attempt });
  const started = one((await db.query(`
    select * from public.mark_lancemos_pilot_request_started($1,$2,$3,$4,$5)
  `, [actionId, attempt.id, fence.worker, fence.lease, await dbNow()])).rows, `${lead.label} arranque`);
  if (started.phase !== 'request_started') fail(`${lead.label} arranque`, started);
  if (afterStart) await afterStart({ ...fence, attempt });
  messageNumber += 1;
  const text = `Texto del mensaje sembrado numero ${messageNumber}`;
  SECRETS.add(text);
  const accepted = one((await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
  `, [actionId, attempt.id, fence.worker, fence.lease, lead.chatwoot, `v3-wamid-${messageNumber}`,
    text, await dbNow()])).rows, `${lead.label} aceptacion`);
  if (accepted.status !== 'accepted_by_chatwoot') fail(`${lead.label} aceptacion`, accepted);
  return { ...fence, attempt };
};
const sleepUntil = async (date) => {
  const wait = date.getTime() - (await dbNow()).getTime() + 150;
  if (wait > 0) await sleep(wait);
};
const postpone = (actionId) => db.query(`
  update public.scheduled_actions set due_at = now() + interval '1 day' where id = $1
`, [actionId]);
const due = (actionId) => db.query(`
  update public.scheduled_actions set due_at = now() where id = $1
`, [actionId]);
const COUPONS = `
  select * from public.claim_due_recovery_coupons_v1(
    $1::timestamptz,'v3-coupons',60,'ATT','45 minutes'::interval,'24 hours'::interval,0.1::numeric,100
  )
`;
const claimCoupons = async () => db.query(COUPONS, [await dbNow()]);
const externalUserOf = async (lead) => one((await db.query(`
  select identity.external_user_id
  from public.recovery_cases recovery
  join public.channel_identities identity on identity.id = recovery.selected_channel_identity_id
  where recovery.id = $1
`, [lead.plan.recovery_case_id])).rows, `${lead.label} identidad`).external_user_id;

// La siembra: un carrito que llega antes de la activacion de la v7.
const seed = person('siembra');
await admitForm(seed);
const seedCart = await admitCart(seed, 1, at(-60));

// La activacion de la v7: pausa, activa y arma.
await setState(6, 'paused');
await db.query(`
  select * from public.activate_lancemos_pilot_scope_version($1,7,$2,$3,$4)
`, [SCOPE, await generationOf(), OPERATOR, OPERATOR_REASON]);
await sleep(20);
await setState(7, 'armed');

// ---------------------------------------------------------------------------
// 1. Los cuatro carritos de la ventana.
// ---------------------------------------------------------------------------
const nextStep = async (lead, stepKey) => {
  const action = await stepOf(lead, stepKey);
  if (action.action_type !== 'no_reply_review' || action.status !== 'pending') {
    fail(`${lead.label} ${stepKey}`, { type: action.action_type, status: action.status });
  }
  return action;
};

// «tres»: A1 y A2 aceptados, Z pendiente, con los dos cupones.
const tres = await start('tres');
await claimCoupons();
await send(tres, tres.plan.scheduled_action_id);
tres.a2 = await nextStep(tres, 'cart_second_touch');
await sleepUntil(tres.a2.due_at);
await send(tres, tres.a2.id, { chatwoot: (now) => evidence(tres, now) });
tres.z = await nextStep(tres, 'cart_last_touch');
await claimCoupons();
await postpone(tres.z.id);

// «contesta»: A1 aceptado, la respuesta adoptada y A2 cancelado por la respuesta.
const contesta = await start('contesta');
await send(contesta, contesta.plan.scheduled_action_id);
contesta.a2 = await nextStep(contesta, 'cart_second_touch');
await postpone(contesta.a2.id);
const contestaUser = await externalUserOf(contesta);
const adopted = one((await asService(() => db.query(
  'select * from public.admit_portable_inbound_commercial_case_v1($1,$2,$3,$4)',
  [ATT1.inboundScope, ATT1.inboundVersion, Number(contesta.chatwoot), contestaUser],
))).rows, 'adopcion');
if (adopted.automation_status !== 'draft_only') fail('adopcion', adopted);
await due(contesta.a2.id);
await sleep(50);
{
  const fence = await claim(contesta, contesta.a2.id);
  const cancelled = await reevaluate(contesta.a2.id, fence.worker, fence.lease, fence.now,
    evidence(contesta, fence.now, { replied: true }));
  if (cancelled.decision !== 'cancel' || cancelled.reason_code !== 'prospect_replied') {
    fail('A2 con respuesta', cancelled);
  }
}

// «compra»: A1 con nuestro link, y la compra con ese sck.
const compra = await start('compra');
await claimCoupons();
let issuance = null;
await send(compra, compra.plan.scheduled_action_id, {
  beforeStart: async ({ worker, lease, now, attempt }) => {
    const ulid = ulidAt(Date.now());
    SECRETS.add(ulid);
    issuance = one((await db.query(`
      select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)
    `, [compra.plan.scheduled_action_id, worker, lease, attempt.id, ulid, now])).rows, 'link de A1');
    if (issuance.outcome !== 'reserved') fail('link de A1', issuance.outcome);
  },
  afterStart: async ({ worker, lease }) => {
    const authorized = one((await db.query(`
      select * from public.authorize_recovery_checkout_issuance_v1($1,$2,$3,$4,$5)
    `, [issuance.issuance_id, compra.plan.scheduled_action_id, worker, lease, await dbNow()])).rows,
    'autorizacion del link');
    if (authorized.outcome !== 'request_started') fail('autorizacion del link', authorized.outcome);
  },
});
SECRETS.add(issuance.sck_value);
SECRETS.add(issuance.checkout_url_final);
compra.a2 = await nextStep(compra, 'cart_second_touch');
await postpone(compra.a2.id);
const TRANSACTION = 'HPCAPTURAV3COMPRA01';
SECRETS.add(TRANSACTION);
{
  const nowMs = (await dbNow()).getTime();
  const payload = {
    id: 'att1-v3-compra-1',
    creation_date: nowMs,
    event: 'PURCHASE_APPROVED',
    version: '2.0.0',
    data: {
      buyer: { email: compra.email, checkout_phone: compra.phone, name: compra.name },
      product: { id: ATT1.productId, ucode: ATT1.hotlink },
      purchase: {
        status: 'APPROVED',
        transaction: TRANSACTION,
        approved_date: nowMs - 1000,
        offer: { code: compra.offer.offer_code },
        origin: { sck: issuance.sck_value },
      },
    },
  };
  const purchase = one((await db.query(`
    select * from public.admit_and_correlate_hotmart_checkout_issuance_v2($1,$2::jsonb,$3,clock_timestamp())
  `, [payload.id, JSON.stringify(payload), issuance.sck_value])).rows, 'compra');
  if (purchase.correlation_outcome !== 'matched') fail('compra', purchase);
}

// «renueva»: A1 y A2 aceptados, «Quiero mi 10 %» adelanta Z, y Z sale.
const renueva = await start('renueva');
await send(renueva, renueva.plan.scheduled_action_id);
renueva.a2 = await nextStep(renueva, 'cart_second_touch');
await sleepUntil(renueva.a2.due_at);
await send(renueva, renueva.a2.id, { chatwoot: (now) => evidence(renueva, now) });
renueva.z = await nextStep(renueva, 'cart_last_touch');
await postpone(renueva.z.id);
{
  const userId = await externalUserOf(renueva);
  const outcome = one((await asService(() => db.query(
    'select * from public.request_recovery_renewal_v1($1,$2,$3,$4,$5,$6)',
    [ATT1.accountId, ATT1.inboxId, Number(renueva.chatwoot), 7101, userId, (new Date()).toISOString()],
  ))).rows, 'renovacion').outcome;
  if (outcome !== 'advanced') fail('renovacion', outcome);
}
await sleep(50);
await send(renueva, renueva.z.id, { chatwoot: (now) => evidence(renueva, now, { replied: true }) });

// Los codigos de los cupones sembrados: no pueden salir.
for (const row of (await db.query('select code from public.recovery_coupons')).rows) SECRETS.add(row.code);

// ---------------------------------------------------------------------------
// 2. La salida.
// ---------------------------------------------------------------------------
const data = await capture();
const text = JSON.stringify(data);
const TOP = [
  '_captured', 'config', 'eventos', 'conflictos_de_compra', 'intenciones', 'contactos', 'casos',
  'secuencias', 'acciones', 'cupones', 'links', 'renovaciones', 'autorizaciones',
  'reevaluaciones', 'primer_contacto',
];
if (JSON.stringify(Object.keys(data)) !== JSON.stringify(TOP)) fail('las secciones de la v3', Object.keys(data));
const captured = data._captured;
if (captured.query !== 'captura-paridad-recuperacion-v3'
    || captured.desde_origen !== 'pilot_scope_version_activated:att1-recuperacion:v>=7') {
  fail('_captured', { query: captured.query, desde_origen: captured.desde_origen });
}
const desde = new Date(captured.desde);
const sum = (rows, key) => rows.reduce((total, row) => total + row[key], 0);
const tramos = data.acciones.flatMap((action) => action.intentos);
const recount = {
  eventos: data.eventos.length,
  disparos_desde: data.eventos.filter((event) => event.event_type === 'PURCHASE_OUT_OF_SHOPPING_CART'
    && new Date(event.received_at) >= desde).length,
  casos: data.casos.length,
  casos_del_carrito: data.casos.filter((row) => row.binding?.scope_key === SCOPE).length,
  secuencias: data.secuencias.length,
  acciones: data.acciones.length,
  intentos: sum(tramos, 'n'),
  intentos_tramos: tramos.length,
  envios: tramos.filter((tramo) => tramo.primero.request_started_at !== null)
    .reduce((total, tramo) => total + tramo.n, 0),
  reevaluaciones: sum(data.reevaluaciones, 'n'),
  reevaluaciones_tramos: data.reevaluaciones.length,
  cupones: data.cupones.length,
  links: data.links.length,
  renovaciones: data.renovaciones.length,
  autorizaciones: data.autorizaciones.length,
  primer_contacto: data.primer_contacto.length,
  intenciones: data.intenciones.length,
  contactos: data.contactos.length,
};
if (JSON.stringify(recount) !== JSON.stringify(captured.conteos)) {
  fail('los conteos no se recuentan desde las filas', { recount, conteos: captured.conteos });
}
// La siembra entra como evento y no como disparo desde «desde».
const seedEvent = data.eventos.find((event) => event.id === seedCart.eventId);
if (seedEvent === undefined || new Date(seedEvent.received_at) >= desde
    || recount.disparos_desde !== 4 || recount.casos_del_carrito !== 4) {
  fail('la ventana', { siembra: seedEvent !== undefined, disparos: recount.disparos_desde,
    casos: recount.casos_del_carrito });
}
// Los pasos de la politica con sus marcas, el delay como intervalo de Postgres.
const threeSteps = data.config.followup_policy_versions.find((row) => row.policy_key === THREE_TOUCHES.policy).steps;
const expectedSteps = [
  { step_key: 'first_contact', mode: 'freeform', delay: null, approved_template: null, coupon_ordinal: null },
  { step_key: 'cart_second_touch', mode: 'freeform', delay: '00:00:01', approved_template: true, coupon_ordinal: null },
  { step_key: 'cart_last_touch', mode: 'freeform', delay: '00:00:02', approved_template: true, coupon_ordinal: 2 },
];
if (JSON.stringify(threeSteps) !== JSON.stringify(expectedSteps)) fail('los pasos de la politica', threeSteps);

const caseOf = (lead) => lead.plan.recovery_case_id;
const actionsOf = (lead) => Object.fromEntries(data.acciones
  .filter((row) => row.recovery_case_id === caseOf(lead)).map((row) => [row.step_key, row]));
const sequenceOf = (lead) => one(data.secuencias.filter((row) => row.recovery_case_id === caseOf(lead)),
  `${lead.label} secuencia`);
const sent = (action) => action.intentos.some((tramo) => tramo.primero.request_started_at !== null);

const tresActions = actionsOf(tres);
if (!sent(tresActions.first_contact) || !sent(tresActions.cart_second_touch)
    || tresActions.cart_last_touch?.status !== 'pending' || sent(tresActions.cart_last_touch)
    || sequenceOf(tres).current_step !== 2 || sequenceOf(tres).automatic_messages_accepted !== 2) {
  fail('«tres»', { acciones: Object.keys(tresActions), secuencia: sequenceOf(tres) });
}
const tresCoupons = data.cupones.filter((row) => row.recovery_case_id === caseOf(tres))
  .map((row) => row.ordinal).sort();
if (JSON.stringify(tresCoupons) !== '[1,2]') fail('los cupones de «tres»', tresCoupons);

const contestaActions = actionsOf(contesta);
const contestaReply = contestaActions.first_contact.respuesta;
if (contestaActions.cart_second_touch.status !== 'cancelled'
    || contestaActions.cart_second_touch.terminal_reason !== 'prospect_replied'
    || contestaReply === null || contestaReply.adopcion_at === null || contestaReply.contesto_at === null
    || contestaReply.renovacion_at !== null) {
  fail('«contesta»', { a2: contestaActions.cart_second_touch.status, respuesta: contestaReply });
}

const compraCase = one(data.casos.filter((row) => row.id === caseOf(compra)), 'caso de «compra»');
const compraLink = one(data.links.filter((row) => row.recovery_case_id === caseOf(compra)), 'link de «compra»');
const compraActions = actionsOf(compra);
if (compraCase.status !== 'won' || compraLink.comprado !== true || compraLink.status !== 'purchase_matched'
    || compraLink.action_id !== compra.plan.scheduled_action_id || compraLink.attempt_number !== 1
    || compraLink.hotmart_webhook_event_id !== compraCase.purchase_event_id
    || compraActions.cart_second_touch.status !== 'cancelled'
    || compraActions.cart_second_touch.terminal_reason !== 'purchase_detected') {
  fail('«compra»', { caso: compraCase.status, link: compraLink.status,
    a2: compraActions.cart_second_touch.terminal_reason });
}

const renewal = one(data.renovaciones, 'renovaciones');
const renuevaActions = actionsOf(renueva);
const renuevaSequence = sequenceOf(renueva);
if (renewal.action_id !== renueva.z.id || renewal.efecto !== 'adelanto'
    || !sent(renuevaActions.cart_last_touch)
    || renuevaActions.cart_second_touch.respuesta?.renovacion_at !== renewal.occurred_at
    || renuevaSequence.completion_reason !== 'policy_exhausted'
    || renuevaSequence.automatic_messages_accepted !== 3) {
  fail('«renueva»', { renovacion: renewal, secuencia: renuevaSequence });
}
// Una accion sin envio no trae respuesta.
if (data.acciones.some((row) => !sent(row) && row.respuesta !== null)) fail('respuesta sin envio');

// ---------------------------------------------------------------------------
// 3. La privacidad.
// ---------------------------------------------------------------------------
const leaks = [...SECRETS].filter((secret) => text.includes(secret)).length;
const patterns = [/@/, /https?:\/\//i, /hermes~/i, /hermes\|/i, /pay\.hotmart/i, /offDiscount/i];
const shapeLeaks = patterns.filter((pattern) => pattern.test(text)).map(String);
if (leaks !== 0 || shapeLeaks.length !== 0) {
  // Nunca se imprime el valor: solo cuantos y que forma.
  fail('la salida trae datos sembrados', { sembrados: leaks, formas: shapeLeaks });
}

if (OUT !== null) writeFileSync(OUT, `${text}\n`);
results.captura = {
  bytes: Buffer.byteLength(text),
  conteos: captured.conteos,
  sembrados_vigilados: SECRETS.size,
};
console.log(JSON.stringify(results));
