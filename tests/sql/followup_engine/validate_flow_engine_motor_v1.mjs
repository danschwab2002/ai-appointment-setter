// El camino del motor para el carrito (migracion 20261022000100, la T2.3b del
// motor de flujos), con las RPC reales sobre la cadena completa de migraciones.
//
// Contrato: docs/contracts/flow-engine-v1.md. Especificacion: «T2.3: el motor
// en la base» (2026-10-21, en el vault), parte T2.3b y sus decisiones 1 a 8.
// Diseno: «el motor de flujos» (2026-10-16), §2.3, los principios.
//
// Prueba, con un rojo a proposito por principio (el rojo se afirma: tiene que
// fallar, no alcanza con que el verde pase):
//   0. la migracion: agrega exactamente sus quince funciones; cambia
//      exactamente siete, seis con su bloque insertado y nada mas, y
//      _flow_engine_route_kinds = {carrito}; solo las cuatro de service_role
//      tienen permiso; correrla dos veces aborta y otro duenio aborta;
//   1. la ruta: el carrito a motor ok, el pago fallido
//      flow_route_kind_not_supported, crear la ruta con el flujo apagado
//      flow_route_flow_off (decision 8);
//   2. A1 por el motor, por el envoltorio de hoy y por la admision;
//   3. A1, A2 y Z con la imagen nueva: el contexto de cada paso, request_started,
//      A2 con el delay compilado, el cupon 2, el link de Z, la renovacion que
//      adelanta Z y policy_exhausted;
//   4. principio 4: los tres toques por mark_lancemos_pilot_request_started
//      (la imagen de hoy) escriben la autorizacion del motor; rojos:
//      flow_engine_needs_engine_image y pilot_request_authorization_required;
//   5. principio 1: (a) la politica compilada sembrada con otra ventana da
//      legado_por_divergencia con count 1 y first, y otra vez count 2 sin
//      first; (b) con el scope del legado pausado, legado_rechazo con la
//      divergencia escrita; (c) p_force_legacy; el rojo: sin el bloque
//      protegido, el error escapa;
//   6. principio 2: el rojo del domingo como 0 lo atrapa la relectura;
//   7. principio 6: no_entra:permiso y baja, con su fila de rechazo;
//   8. principio 7: con el carrito en motor, la guarda del legado en curso es
//      la de legado (un A2 del legado sale prendido y se frena pausado);
//   9. principio 8: tope_del_flujo con count por lease, la pausa y el prender,
//      terminado, apagado-y-prendido cancela en la reevaluacion; el rojo: sin
//      el bloque (C) la reevaluacion ejecuta;
//  10. principio 10: una v2 que cambia una espera no toca lo en curso, y el
//      contexto da la plantilla de la version fijada;
//  11. principio 11: dos productos, el carrito de A en motor y el de B en
//      legado; un evento de B nunca entra al flujo de A;
//  12. principio 13: no_arranca_mientras:pago_fallido sin mover el due_at,
//      ya_en_curso, no_arranca_mientras:ficha, y la repeticion created=false;
//  13. la bolsa compartida en los dos sentidos;
//  14. read_flow_engine_v1 cuenta lo en curso del motor por version;
//  15. el disparador del tope por persona (decision 4), en el INSERT y en el
//      UPDATE;
//  16. la baja entre la reevaluacion y el arranque, por la imagen nueva y por
//      la vieja: rechazada, sin autorizacion y sin cupo consumido;
//  17. la audiencia vuelta a mirar al arrancar (la intencion ya no esta viva);
//  18. decision 2: otra oferta, otro inbox u otra cuenta es motor_no_arranca
//      con el codigo estatico, sin divergencia;
//  19. decision 5: cohorte, repeticion distinta de 0m y bolsa distinta del
//      scope caen al legado con su divergencia de ruta;
//  20. la delegacion del envoltorio con una entrada fuera de forma decide
//      como el legado;
//  21. p_force_legacy sin ruta del carrito es legado, sin divergencia;
//  22. la re-entrada: cerrado el caso del motor, otro carrito arranca otro
//      recorrido.
//
// Datos sinteticos: personas de example.test con telefonos 1202555xxxx (rango
// de ficcion), el manifiesto de ATT1 de tests/fixtures/instances/att1/ y el
// carrito capturado de tests/fixtures/hotmart_cart_abandonment_rejected_v1.json.
// Las esperas del motor son de minutos (la granularidad del esquema): la prueba
// verifica el due_at compilado y despues lo adelanta para tomar la accion.
import { PGlite } from '@electric-sql/pglite';
import { randomBytes } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
export const TARGET = '20261022000100_flow_engine_motor_carrito_v1.sql';
const fixture = (path) => JSON.parse(readFileSync(join(root, 'tests/fixtures', path), 'utf8'));
export const fail = (message, detail) => {
  throw new Error(`${message}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
};
export const one = (rows, label) => {
  if (rows.length !== 1) fail(`${label}: se esperaba una fila y hubo ${rows.length}`);
  return rows[0];
};
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
export const sameSet = (left, right) => JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());

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
    if (match) table[match[1]] = match[2] ?? (match[3] !== undefined ? Number(match[3]) : match[4] === 'true');
  }
  return manifest;
};

// El mundo: la cadena aplicada, ATT1 (producto A) y un producto B sintetico,
// el almacen con el carrito y el pago fallido de A y el carrito de B, las
// rutas creadas en legado, y los helpers del despacho. hooks.beforeTarget y
// hooks.afterTarget corren alrededor de la migracion de la T2.3b.
export const buildWorld = async (hooks = {}) => {
  const db = new PGlite();
  await db.waitReady;
  await db.exec(`
    create role anon nologin;
    create role authenticated nologin;
    create role service_role nologin;
    alter default privileges in schema public grant execute on functions to anon, authenticated;
    alter default privileges in schema public grant all on functions to service_role;
    alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
    grant usage on schema public to service_role, anon;
  `);
  const migrationNames = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
  const targetIndex = migrationNames.indexOf(TARGET);
  if (targetIndex < 0) fail(`${TARGET} no esta en la cadena`);
  const apply = async (file) => db.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
  await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
  for (const name of migrationNames.slice(0, targetIndex)) await apply(join(root, 'supabase/migrations', name));
  const applyTarget = () => apply(join(root, 'supabase/migrations', TARGET));
  if (hooks.beforeTarget) await hooks.beforeTarget(db, applyTarget);
  await applyTarget();
  if (hooks.afterTarget) await hooks.afterTarget(db, applyTarget);
  for (const name of migrationNames.slice(targetIndex + 1)) await apply(join(root, 'supabase/migrations', name));

  const required = (value, label) => {
    if (value === undefined || value === null || value === '') fail(`al fixture de ATT1 le falta ${label}`);
    return value;
  };
  const manifest = readManifest(readFileSync(join(root, 'tests/fixtures/instances/att1/instancia.toml'), 'utf8'));
  const offersA = required(manifest.hotmart?.ofertas, 'hotmart.ofertas').map((offer) => {
    const url = new URL(required(offer.url, 'ofertas.url'));
    return { offer_code: offer.codigo, site: offer.site, landing_id: offer.landing_id, page_host: url.host, page_path: url.pathname };
  });
  const PILOT = fixture('instances/att1/politica-piloto.json');
  const A = {
    key: 'A',
    tenant: manifest.instancia.tenant_ref, funnel: manifest.instancia.funnel_ref, ally: manifest.instancia.ally_ref,
    bindingVersion: manifest.instancia.binding_version, brand: manifest.instancia.marca,
    timezone: manifest.instancia.zona_horaria, productId: manifest.hotmart.product_id,
    hotlink: manifest.hotmart.hotlink, productName: manifest.hotmart.product_name,
    currency: manifest.hotmart.moneda, price: manifest.hotmart.precio,
    accountId: manifest.chatwoot.account_id, inboxId: manifest.chatwoot.inbox_id,
    inboundScope: manifest.inbound.scope_key, inboundVersion: manifest.inbound.scope_version,
    copyVersion: manifest.consentimiento.copy_version, offers: offersA, scope: 'att1-recuperacion',
  };
  // B: otro producto sintetico de la misma instancia (otro funnel, otro id de
  // Hotmart, otra oferta), con su propio scope del legado.
  const B = {
    ...A, key: 'B', funnel: 'att9', productId: 9900001, hotlink: 'H9900001X', productName: 'Producto Sintetico B',
    inboundScope: 'att9-inbound', scope: 'att9-recuperacion',
    offers: [{ offer_code: 'offb9001', site: 'sitio-b', landing_id: 'landing-b', page_host: 'b.example.test', page_path: '/b' }],
  };
  const CHANNEL_PROVIDER = PILOT.pilot_scope.channel_provider;
  const CHANNEL_REF = `${PILOT.pilot_scope.channel_account_ref_prefix}${A.inboxId}`;
  const ALL_DAY = PILOT.policy.business_windows.map((window) => ({ ...window, start: '00:00', end: '23:59' }));
  const OPERATOR = 'operador-motor-prueba';
  const OPERATOR_REASON = 'razon-del-operador-del-motor';
  const THREE_TOUCHES = {
    policy: 'att1-carrito-tres-toques',
    steps: [
      { step_key: 'first_contact', mode: 'freeform' },
      { step_key: 'cart_second_touch', mode: 'freeform', delay: '1 second', approved_template: true },
      { step_key: 'cart_last_touch', mode: 'freeform', delay: '2 seconds', approved_template: true, coupon_ordinal: 2 },
    ],
    max: 3,
  };
  const ONE_TOUCH = { policy: 'att1-recuperacion-un-toque', steps: [{ step_key: 'first_contact', mode: 'freeform' }], max: 1 };

  const seedProduct = async (P, legacyPolicy) => {
    const [defaultOffer, ...additional] = P.offers;
    await db.query(`
      insert into public.commercial_ally_runtime_bindings
        (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
         lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
         product_name, product_price, currency, offer_code, consent_copy_version,
         hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
         inbound_scope_key, inbound_scope_version, additional_offer_codes, additional_offer_landings)
      values ($1,$2,$3,'active',$4,$5,$6,$7,$8,$9,$10,$11,$12::numeric,$13,$14,$15,
              $16,$17,$18,$19,$20,$21::text[],$22::jsonb)
    `, [P.tenant, P.funnel, P.bindingVersion, P.ally, P.brand, defaultOffer.site, defaultOffer.landing_id,
      defaultOffer.page_host, defaultOffer.page_path, P.hotlink, P.productName, P.price, P.currency,
      defaultOffer.offer_code, P.copyVersion, P.productId, P.accountId, P.inboxId, P.inboundScope, P.inboundVersion,
      additional.map((offer) => offer.offer_code),
      JSON.stringify(additional.map((offer) => ({ offer_code: offer.offer_code, site: offer.site,
        landing_id: offer.landing_id, page_host: offer.page_host, page_path: offer.page_path })))]);
    for (const offer of P.offers) {
      await db.query(`
        insert into public.hotmart_purchase_intent_scopes
          (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref, offer_ref, max_lookback, active)
        values ($1,$2,$3,$4,$5,$6::interval,true)
      `, [P.tenant, P.funnel, String(P.productId), P.hotlink, offer.offer_code, PILOT.purchase_intent_scope.max_lookback]);
      await db.query(`
        insert into public.checkout_offer_catalog (
          tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref, offer_code,
          checkout_base_url, checkout_mode, default_for_inbound, status, version, approved_by, approved_at
        ) values ($1,$2,$3,$4,$5,$6,$7,$8,10,$9,'active',1,$10,now())
      `, [P.tenant, P.funnel, P.inboundScope, P.inboundVersion, P.hotlink, offer.landing_id, offer.offer_code,
        `https://pay.hotmart.com/${P.hotlink}`, offer === defaultOffer, OPERATOR]);
    }
    await db.query(`
      insert into public.commercial_ally_hotmart_purchase_policies (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
      values ($1,$2,$3,true,$4::interval)
    `, [P.tenant, P.funnel, P.bindingVersion, PILOT.purchase_policy.max_lookback]);
    await db.query(`
      insert into public.inbound_commercial_scope_versions
        (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
         external_product_id, offer_code, approved_by, approved_at, published_at)
      values ($1,$2,'published',$3,$4,$5,$6,$7,$8,now(),now())
    `, [P.inboundScope, P.inboundVersion, P.tenant, P.accountId, P.inboxId, P.hotlink, defaultOffer.offer_code, OPERATOR]);
    await db.query(`
      insert into public.pilot_scope_versions
        (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
         channel, channel_provider, channel_account_ref, source, source_event_type,
         additional_source_event_types, external_product_id, offer_code,
         additional_offer_codes, purpose, policy_key, policy_version, timezone,
         max_cohort_contacts, max_outbound_request_starts_total,
         max_outbound_request_starts_per_day, audience_mode, approved_by, approved_at, published_at)
      values ($1,1,'published',$2,$3,$4,'whatsapp',$5,$6,'hotmart',
              'PURCHASE_OUT_OF_SHOPPING_CART',array['PURCHASE_CANCELED'],$7,$8,$9::text[],
              'cart_recovery',$10,1,$11,20,1000,500,'consented_intent',$12,now(),now())
    `, [P.scope, P.tenant, P.accountId, P.inboxId, CHANNEL_PROVIDER, CHANNEL_REF, String(P.productId),
      defaultOffer.offer_code, additional.map((offer) => offer.offer_code), legacyPolicy.policy, P.timezone, OPERATOR]);
    await db.query(`
      insert into public.pilot_runtime_controls (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
      values ($1,1,'inactive',0,$2,$3)
    `, [P.scope, OPERATOR, OPERATOR_REASON]);
    await setState(P.scope, 1, 'armed');
  };
  const generationOf = async (scope) => one((await db.query(
    'select generation from public.pilot_runtime_controls where scope_key = $1', [scope])).rows, 'generacion').generation;
  const setState = async (scope, version, state) => db.query(
    'select * from public.set_lancemos_pilot_runtime_state($1,$2,$3,$4,$5,$6)',
    [scope, version, await generationOf(scope), state, OPERATOR, OPERATOR_REASON]);
  for (const policy of [ONE_TOUCH, THREE_TOUCHES]) {
    await db.query(`
      insert into public.followup_policy_versions
        (policy_key, version, status, purpose, timezone, business_windows, grace_period, expires_after,
         max_automatic_messages, steps, approved_by, approved_at, published_at)
      values ($1,1,'published','cart_recovery',$2,$3::jsonb,interval '0 seconds',interval '5 days',$4,$5::jsonb,$6,now(),now())
    `, [policy.policy, A.timezone, JSON.stringify(ALL_DAY), policy.max, JSON.stringify(policy.steps), OPERATOR]);
  }
  await seedProduct(A, THREE_TOUCHES);
  await seedProduct(B, ONE_TOUCH);
  const LEGACY = { [A.scope]: THREE_TOUCHES.policy, [B.scope]: ONE_TOUCH.policy };

  // Las personas, el formulario y el carrito.
  const dbNow = async () => new Date((await db.query('select clock_timestamp() as now')).rows[0].now);
  const at = (base, minutes) => new Date(base.getTime() + minutes * 60_000);
  const CAPTURED_CART = fixture('hotmart_cart_abandonment_rejected_v1.json').payload;
  const REFUSALS = fixture('hotmart_purchase_canceled_att1_20261019.json').eventos;
  let personIndex = 0;
  const person = (label, P = A, phonePrefix = '120255504') => {
    personIndex += 1;
    const suffix = String(personIndex).padStart(2, '0');
    return {
      label, product: P, name: `Compradora Motor ${label}`, email: `motor-${P.key.toLowerCase()}-${suffix}@example.test`,
      phone: `${phonePrefix}${suffix}`, country: '1', iso: 'US', offer: P.offers.at(-1) ?? P.offers[0],
      chatwoot: String(940000 + personIndex),
    };
  };
  const admitForm = async (lead, submittedAt) => {
    const P = lead.product;
    const { offer } = lead;
    const id = `motor-form-${lead.email}-${offer.offer_code}-${submittedAt.getTime()}`;
    const pageUrl = `https://${offer.page_host}${offer.page_path}`;
    const checkoutUrl = `https://pay.hotmart.com/${P.hotlink}?off=${offer.offer_code}&checkoutMode=10`;
    const raw = {
      id, event: 'lead.precheckout', version: '1.1.0', created_at: submittedAt.toISOString(),
      source: { system: 'landing', site: offer.site, aliado: P.brand, landing_id: offer.landing_id, page_url: pageUrl },
      data: {
        buyer: { name: lead.name, email: lead.email, phone: `+${lead.phone}`, phone_country_code: lead.country,
          phone_national: lead.phone.slice(lead.country.length) },
        product: { hotlink: P.hotlink, id: null, name: P.productName, price: Number(P.price), currency: P.currency },
        offer: { code: offer.offer_code },
        checkout_url: checkoutUrl,
        checkout_country: { iso: lead.iso, source: 'phone_country_code' },
        consent: { marketing_optin: true, whatsapp_contact: true, copy_version: P.copyVersion },
      },
      dedupe_key: `${offer.site}:${offer.offer_code}:${lead.email}`,
    };
    const canonicalForm = {
      external_submission_id: id, event_type: 'PRECHECKOUT_FORM_SUBMITTED', contract_version: '1.1.0',
      submitted_at: raw.created_at,
      source: { tenant_ref: P.tenant, funnel_ref: P.funnel, landing_ref: offer.landing_id, page_url: pageUrl, aliado: P.brand },
      identity: { email: lead.email, phone: lead.phone, phone_valid: true, phone_country_iso: lead.iso },
      lead: { full_name: lead.name },
      commerce: { product_ref: P.hotlink, product_name: P.productName, offer_ref: offer.offer_code,
        price: String(P.price), currency: P.currency, checkout_url: checkoutUrl },
      dedupe_key: raw.dedupe_key,
      consent: { terms_accepted: false, privacy_accepted: false, marketing_optin: true, whatsapp_contact: true,
        copy_version: P.copyVersion },
      assurance: { provisional: false, provider_observed: true, activation_authorized: true },
    };
    const admitted = one((await db.query(`
      select * from public.admit_portable_observed_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb)
    `, [P.tenant, P.funnel, P.bindingVersion, id, JSON.stringify(raw), JSON.stringify(canonicalForm)])).rows,
    `${lead.label} formulario`);
    if (admitted.outcome !== 'inserted') fail(`${lead.label} formulario`, admitted.outcome);
  };
  const ensureContact = async (lead, eventId) => {
    if (lead.contact) return;
    lead.contact = one((await db.query(`
      insert into public.contacts (full_name, email, phone, country_iso) values ($1,$2,$3,'MX') returning id
    `, [lead.name, lead.email, lead.phone])).rows, `${lead.label} contacto`).id;
    await db.query(`
      insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id)
      values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)
    `, [lead.contact, lead.email, lead.phone, eventId]);
  };
  let cartSerial = 0;
  // Un carrito de la persona (nueva, o la misma otra vez), con o sin formulario.
  const cartFor = async (labelOrLead, { P = A, form = true, scope = null } = {}) => {
    const lead = typeof labelOrLead === 'string' ? person(labelOrLead, P) : labelOrLead;
    const base = await dbNow();
    if (form) await admitForm(lead, at(base, -50));
    cartSerial += 1;
    const payload = structuredClone(CAPTURED_CART);
    payload.id = `motor-cart-${lead.email}-${cartSerial}`;
    payload.creation_date = at(base, -30).getTime();
    payload.data.product = { id: lead.product.productId, name: lead.product.productName };
    payload.data.offer = { code: lead.offer.offer_code };
    payload.data.buyer = { name: lead.name, email: lead.email, phone: lead.phone };
    const P2 = lead.product;
    const admitted = one((await db.query(`
      select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
    `, [P2.tenant, P2.funnel, P2.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone])).rows,
    `${lead.label} carrito`);
    if (admitted.outcome !== 'inserted') fail(`${lead.label} carrito`, admitted.outcome);
    await ensureContact(lead, admitted.webhook_event_id);
    const useScope = scope ?? P2.scope;
    lead.event = admitted.webhook_event_id;
    lead.cartArgs = [admitted.webhook_event_id, lead.contact, String(P2.productId), P2.productName,
      lead.offer.offer_code, LEGACY[useScope], at(base, -30).toISOString(), P2.accountId, P2.inboxId, lead.phone,
      useScope, 1];
    lead.admitArgs = [admitted.webhook_event_id, lead.contact, String(P2.productId), P2.productName,
      lead.offer.offer_code, at(base, -30).toISOString(), P2.accountId, P2.inboxId, lead.phone,
      useScope, 1, LEGACY[useScope], 1];
    return lead;
  };
  const PLAN_CART = 'select * from public.plan_lancemos_pilot_cart_recovery($1,$2,$3,$4,$5,$6,1,$7,$8,$9,$10,$11,$12)';
  const ADMIT = `select * from public.admit_flow_engine_hotmart_v1($1,$2,$3,$4,$5,$6::timestamptz,$7,$8,$9,$10,$11,$12,$13,$14)`;
  const planCart = async (lead) => {
    lead.plan = one((await db.query(PLAN_CART, lead.cartArgs)).rows, `${lead.label} plan`);
    return lead.plan;
  };
  const admit = async (lead, force = false) => {
    const row = one((await db.query(ADMIT, [...lead.admitArgs, force])).rows, `${lead.label} admision`);
    if (row.recovery_case_id) lead.plan = { recovery_case_id: row.recovery_case_id, scheduled_action_id: row.scheduled_action_id };
    return row;
  };
  // El pago rechazado capturado, en el scope del carrito.
  const refusalFor = async (personaNumber, lead) => {
    const payload = structuredClone(REFUSALS.find((event) => event.persona === personaNumber).payload);
    const base = await dbNow();
    payload.id = `motor-refusal-${lead.email}`;
    payload.creation_date = at(base, -20).getTime();
    payload.data.buyer = { ...payload.data.buyer, name: lead.name, email: lead.email, checkout_phone: lead.phone };
    payload.data.purchase.offer.code = lead.offer.offer_code;
    const admitted = one((await db.query(`
      select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)
    `, [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone])).rows,
    `${lead.label} rechazo`);
    if (admitted.outcome !== 'inserted') fail(`${lead.label} rechazo`, admitted.outcome);
    await ensureContact(lead, admitted.webhook_event_id);
    const plan = one((await db.query(
      'select * from public.plan_portable_payment_failure_recovery($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)',
      [admitted.webhook_event_id, lead.contact, String(A.productId), A.productName, lead.offer.offer_code,
        THREE_TOUCHES.policy, 1, at(base, -20).toISOString(), A.accountId, A.inboxId, lead.phone, A.scope, 1])).rows,
    `${lead.label} plan del rechazo`);
    if (!plan.created) fail(`${lead.label} plan del rechazo`, plan);
    return plan;
  };

  // El despacho de un paso. El claim toma todo lo vencido: lo demas se corre.
  const claim = async (label, actionId) => {
    await db.query(`
      update public.scheduled_actions set due_at = least(now() + interval '1 day', expires_at - interval '1 minute')
       where id <> $1 and status in ('pending', 'deferred', 'retryable_failed') and due_at <= now() + interval '1 minute'
    `, [actionId]);
    await db.query(`update public.scheduled_actions set due_at = least(due_at, now()), next_attempt_at = null
      where id = $1`, [actionId]);
    const worker = `motor-worker-${label}`;
    const now = await dbNow();
    const claimed = (await db.query(
      "select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)", [worker, now])).rows;
    if (claimed.length !== 1 || claimed[0].id !== actionId) fail(`${label}: el claim tomo otra cosa`, claimed.map((row) => row.step_key));
    return { worker, now, lease: claimed[0].lease_generation };
  };
  const evidence = (lead, now) => ({
    p_chatwoot_checked: true, p_chatwoot_conversation_id: lead.chatwoot, p_chatwoot_checkpoint_message_id: '1',
    p_chatwoot_checkpoint_at: now.toISOString(), p_chatwoot_status: 'open', p_chatwoot_can_reply: false,
    p_chatwoot_anchor_found: true, p_chatwoot_automation_paused: false, p_chatwoot_inbound_after_anchor: false,
    p_chatwoot_human_activity_after_anchor: false,
  });
  const reevaluate = async (actionId, fence, chatwoot) => {
    if (!chatwoot) {
      return one((await db.query('select * from public.reevaluate_followup_action($1,$2,$3,$4)',
        [actionId, fence.worker, fence.lease, fence.now])).rows, 'reevaluacion');
    }
    const names = Object.keys(chatwoot);
    const list = names.map((name, index) => `${name} => $${index + 5}`).join(', ');
    return one((await db.query(`
      select * from public.reevaluate_followup_action(p_action_id => $1, p_worker_id => $2, p_lease_generation => $3,
        p_now => $4, ${list})
    `, [actionId, fence.worker, fence.lease, fence.now, ...names.map((name) => chatwoot[name])])).rows, 'reevaluacion');
  };
  const reserve = async (lead, actionId, { withEvidence = null, expect = 'execute' } = {}) => {
    const fence = await claim(lead.label, actionId);
    const chatwoot = withEvidence ?? (await stepOfId(actionId)).action_type === 'no_reply_review';
    const decision = await reevaluate(actionId, fence, chatwoot ? evidence(lead, fence.now) : null);
    if (decision.decision !== expect) fail(`${lead.label} reevaluacion`, decision);
    if (expect !== 'execute') return { ...fence, decision, actionId };
    const attempt = one((await db.query(`
      select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
    `, [actionId, fence.worker, fence.lease, decision.case_version, decision.sequence_revision, fence.now])).rows, 'intento');
    return { ...fence, attempt, actionId, decision };
  };
  const MARK_CART = 'select * from public.mark_lancemos_pilot_request_started($1,$2,$3,$4,$5)';
  const MARK_ENGINE = 'select * from public.mark_flow_engine_request_started_v1($1,$2,$3,$4,$5)';
  const startOld = async (reserved) => one((await db.query(MARK_CART,
    [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, await dbNow()])).rows, 'arranque viejo');
  const startNew = async (reserved) => one((await db.query(MARK_ENGINE,
    [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, await dbNow()])).rows, 'arranque nuevo');
  let messageNumber = 0;
  const accept = async (lead, reserved) => {
    messageNumber += 1;
    const accepted = one((await db.query(`
      select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
    `, [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, lead.chatwoot,
      `motor-wamid-${messageNumber}`, `Texto sembrado ${messageNumber}`, await dbNow()])).rows, `${lead.label} aceptacion`);
    if (accepted.status !== 'accepted_by_chatwoot') fail(`${lead.label} aceptacion`, accepted);
    return accepted;
  };
  const stepOfId = async (actionId) => one((await db.query('select * from public.scheduled_actions where id = $1',
    [actionId])).rows, 'accion');
  const stepOf = async (lead, stepKey) => one((await db.query(
    'select * from public.scheduled_actions where recovery_case_id = $1 and step_key = $2 order by created_at desc limit 1',
    [lead.plan.recovery_case_id, stepKey])).rows, `${lead.label} ${stepKey}`);
  const caseOf = async (lead) => one((await db.query('select * from public.recovery_cases where id = $1',
    [lead.plan.recovery_case_id])).rows, `${lead.label} caso`);
  const runOf = async (caseId) => (await db.query('select * from public.flow_engine_runs where recovery_case_id = $1',
    [caseId])).rows[0] ?? null;

  // El almacen.
  const INSTANCE = { tenant_ref: A.tenant, ally_ref: A.ally, funnel_ref: A.funnel };
  const VALIDATOR = { contract: 'setter-flujo/v1', rules_version: 1, phase: 'publish', actor: 'operador',
    product_version: 'v1.9.0', git_sha: 'abcdef0' };
  const CLEAN = { blocks: [], warnings: [] };
  const rpc = async (fn, ...params) => {
    const marks = params.map((_, i) => `$${i + 1}`).join(', ');
    return JSON.parse(one((await db.query(`select public.${fn}(${marks})::text as r`,
      params.map((p) => (p !== null && typeof p === 'object' ? JSON.stringify(p) : p)))).rows, fn).r);
  };
  const register = async (product, flowKey) => {
    const registered = await rpc('operator_register_flow_v1', INSTANCE, product, flowKey, 'dan', 'alta del flujo');
    if (!registered.ok) fail(`registrar ${product}/${flowKey}`, registered);
    return registered.flow_id;
  };
  const draftBase = async (flowIds) => (await db.query(`
    select coalesce(jsonb_object_agg(flow_id::text, jsonb_build_object('active_version', active_version)), '{}'::jsonb) b
      from public.flow_controls where flow_id = any($1::uuid[])`, [flowIds])).rows[0].b;
  const instanceBase = async (flowIds) => (await db.query('select public._flow_store_base($1, $2, $3::uuid[]) b',
    [A.tenant, A.ally, flowIds])).rows[0].b;
  const cycle = async (items, label) => {
    const flowIds = [...new Set(items.map((item) => item.flow_id))];
    const saved = await rpc('operator_save_flow_change_draft_v1', INSTANCE, null, null, items, await draftBase(flowIds),
      'el motor del carrito', 'dan');
    const validated = await rpc('operator_record_flow_change_validation_v1', INSTANCE, saved.proposal_id, saved.revision,
      await instanceBase(flowIds), VALIDATOR, CLEAN, {}, 'dan');
    const approved = await rpc('operator_approve_flow_change_v1', INSTANCE, saved.proposal_id, validated.fingerprint, 'dan');
    const published = await rpc('operator_publish_flow_change_v1', INSTANCE, saved.proposal_id, validated.fingerprint, {
      fingerprint: validated.fingerprint, validator: VALIDATOR, result: CLEAN, catalog_sha256: 'a'.repeat(64),
      validated_at: new Date().toISOString(),
    });
    if (!saved.ok || validated.status !== 'validada' || !approved.ok || !published.ok) {
      fail(`ciclo ${label}`, { saved, validated, approved, published });
    }
  };
  const A1 = { plantilla: 'att1_carrito_descuento_01', idioma: 'es_MX',
    variables: ['nombre', 'producto', 'cuando', 'precio', 'vence'], boton: { link: 'checkout', cupon: 'personal' } };
  const A2 = { plantilla: 'att1_carrito_dudas_01', idioma: 'es_MX', variables: ['nombre', 'producto'] };
  const Z = { plantilla: 'att1_ultima_renovacion_01', idioma: 'es_MX', variables: ['nombre', 'producto', 'vence'],
    boton: { link: 'checkout', cupon: 'personal' } };
  const cartDefinition = (product, { a2Wait = '1m', zWait = '2m', a2 = A2, topes = {}, horario = null } = {}) => ({
    esquema: 'setter-flujo/v1', flujo: 'carrito', producto: product, estado: 'borrador', nombre: 'Carrito abandonado',
    dispara: { evento: 'carrito' }, entra: { permiso: 'formulario', contesto_a: [] },
    pasos: [A1, { esperar: a2Wait }, a2, { esperar: zWait }, Z],
    termina: { si: ['compra'], vence: '1d' },
    horario: horario ?? { zona: 'America/Mexico_City', desde: '00:00', hasta: '23:59' },
    topes: { bolsa: product === A.funnel ? A.scope : B.scope, por_dia: 400, total: 900,
      repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' }, ...topes },
    prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  });
  const versionItem = (flowId, product, flujo, definicion) => ({
    producto: product, flujo, flow_id: flowId, tipo: 'version', definicion, en_curso: 'seguir' });
  const stateItem = (flowId, product, flujo, tipo) => ({ producto: product, flujo, flow_id: flowId, tipo });
  const FLOWS = {
    carrito: await register(A.funnel, 'carrito'),
    pago_fallido: await register(A.funnel, 'pago_fallido'),
    carritoB: await register(B.funnel, 'carrito'),
  };
  const payDefinition = { ...cartDefinition(A.funnel), flujo: 'pago_fallido', dispara: { evento: 'pago_fallido' },
    pasos: [{ plantilla: 'att1_compra_fallida_01', idioma: 'es_MX', variables: ['nombre', 'producto'] }] };
  await cycle([
    versionItem(FLOWS.carrito, A.funnel, 'carrito', cartDefinition(A.funnel)), stateItem(FLOWS.carrito, A.funnel, 'carrito', 'prender'),
    versionItem(FLOWS.pago_fallido, A.funnel, 'pago_fallido', payDefinition), stateItem(FLOWS.pago_fallido, A.funnel, 'pago_fallido', 'prender'),
    versionItem(FLOWS.carritoB, B.funnel, 'carrito', cartDefinition(B.funnel)), stateItem(FLOWS.carritoB, B.funnel, 'carrito', 'prender'),
  ], 'v1 de los flujos');
  const control = async (flowId) => one((await db.query(
    'select state, active_version, generation::int as generation from public.flow_controls where flow_id = $1', [flowId])).rows, 'control');
  const publishCart = async (options, label) => {
    await cycle([versionItem(FLOWS.carrito, A.funnel, 'carrito', cartDefinition(A.funnel, options))], label);
    return (await control(FLOWS.carrito)).active_version;
  };
  const pauseFlow = async (flowId, product, flowKey) => {
    const paused = await rpc('operator_pause_flow_v1', INSTANCE, product, flowKey, (await control(flowId)).generation, 'dan', 'pausa de prueba');
    if (!paused.ok || !paused.changed) fail(`pausar ${flowKey}`, paused);
  };
  const setFlow = async (flowId, product, flowKey, tipo) => cycle([stateItem(flowId, product, flowKey, tipo)], `${tipo} ${flowKey}`);
  const createRoute = (product, flowKey, kind, scope) => rpc('operator_create_flow_route_v1', INSTANCE, product, flowKey, kind, scope,
    'dan', 'la ruta del flujo');
  const activeSha = async (flowId) => one((await db.query(`
    select d.definition_sha256 from public.flow_definitions d join public.flow_controls c
      on c.flow_id = d.flow_id and c.active_version = d.version where d.flow_id = $1`, [flowId])).rows, 'sha').definition_sha256;
  const revalidationFor = async (flowId) => ({
    version: (await control(flowId)).active_version, definition_sha256: await activeSha(flowId), rules_version: 2,
    capabilities: 'v1', blocks: [], validator: { ...VALIDATOR, rules_version: 2 },
  });
  const routeGeneration = async (flowId) => Number(one((await db.query(
    'select generation from public.flow_engine_routes where flow_id = $1', [flowId])).rows, 'ruta').generation);
  const route = async (product, flowKey, flowId, executor) => rpc('operator_route_flow_v1', INSTANCE, product, flowKey, executor,
    await routeGeneration(flowId), 'dan', 'mover la ruta', executor === 'motor' ? await revalidationFor(flowId) : null);

  return {
    db, A, B, FLOWS, SCOPE: A.scope, LEGACY, THREE_TOUCHES, OPERATOR, INSTANCE, A1, A2, Z,
    dbNow, person, admitForm, cartFor, planCart, admit, refusalFor, claim, reevaluate, reserve, evidence,
    startOld, startNew, accept, stepOf, stepOfId, caseOf, runOf, rpc, register, cycle, cartDefinition,
    versionItem, stateItem, control, publishCart, pauseFlow, setFlow, createRoute, route, revalidationFor,
    setState, generationOf, PLAN_CART, ADMIT, MARK_CART, MARK_ENGINE, sleep,
  };
};

// Un error esperado, dentro de una transaccion que se deshace.
export const expectError = async (db, label, run, { code, message, detail } = {}) => {
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
    fail(`${label}: se esperaba ${code} ${message} ${detail ?? ''}`,
      error ? { code: error.code, message: error.message, detail: error.detail } : 'paso');
  }
  return error;
};
// Un error esperado sin deshacer lo de antes (savepoint).
export const errorOf = async (db, run) => {
  let inTransaction = true;
  try {
    await db.exec('savepoint esperado');
  } catch (error) {
    if (error.code !== '25P01') throw error;
    inTransaction = false;
  }
  try {
    await run();
    if (inTransaction) await db.exec('release savepoint esperado');
    return null;
  } catch (error) {
    if (inTransaction) {
      await db.exec('rollback to savepoint esperado');
      await db.exec('release savepoint esperado');
    }
    return error;
  }
};

const main = async () => {
  const results = {};
  const TZ = 'timestamp with time zone';
  let before;
  let after;
  const snapshot = async (db) => new Map((await db.query(`
    select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
           has_function_privilege('service_role', p.oid, 'execute') as service_x,
           has_function_privilege('anon', p.oid, 'execute')
             or has_function_privilege('authenticated', p.oid, 'execute') as api_x
    from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
  `)).rows.map((row) => [row.signature, row]));
  const world = await buildWorld({
    beforeTarget: async (db, applyTarget) => {
      before = await snapshot(db);
      // Otro duenio en una de las funciones parchadas: aborta sin cambiar nada.
      await db.exec('create role otro_duenio nologin');
      let error = null;
      await db.exec('begin');
      try {
        await db.exec(`alter function public.mark_followup_request_started(uuid,uuid,text,bigint,timestamptz) owner to otro_duenio`);
        await applyTarget();
      } catch (caught) {
        error = caught;
      } finally {
        await db.exec('rollback');
      }
      if (error === null || error.code !== '55000' || error.message !== 'flow_engine_motor_owner_mismatch') {
        fail('otro duenio en una funcion parchada', error ? { code: error.code, message: error.message } : 'paso');
      }
      if ((await db.query("select to_regproc('public.admit_flow_engine_hotmart_v1') is not null as x")).rows[0].x) {
        fail('el aborto por el duenio dejo la admision');
      }
      results.duenio = `${error.code}:${error.message}`;
    },
    afterTarget: async (db, applyTarget) => {
      after = await snapshot(db);
      const error = await expectError(db, 'correr la migracion dos veces', applyTarget,
        { code: '55000', message: 'flow_engine_motor_already_applied' });
      results.segunda_corrida = `${error.code}:${error.message}`;
    },
  });
  const {
    db, A, B, FLOWS, SCOPE, A1, A2, Z, dbNow, person, cartFor, planCart, admit, refusalFor, claim, reevaluate, reserve,
    startOld, startNew, accept, stepOf, caseOf, runOf, rpc, register, cycle, cartDefinition, versionItem, stateItem,
    control, publishCart, pauseFlow, setFlow, createRoute, route, setState, PLAN_CART, MARK_CART, MARK_ENGINE,
  } = world;

  // -------------------------------------------------------------------------
  // 0. La forma de la migracion.
  // -------------------------------------------------------------------------
  {
    const SERVICE = [
      '_flow_engine_run_turned_off(uuid)',
      `admit_flow_engine_hotmart_v1(uuid,uuid,text,text,text,${TZ},bigint,bigint,text,text,integer,text,integer,boolean)`,
      `mark_flow_engine_request_started_v1(uuid,uuid,text,bigint,${TZ})`,
      'flow_engine_execution_context_v1(uuid)',
    ];
    const ADDED = [
      ...SERVICE,
      '_flow_engine_sends(jsonb)',
      '_flow_engine_scope_static_reason(pilot_scope_versions,text,bigint,bigint,text,text)',
      '_portable_cart_audience_checks(text,integer,uuid,uuid,text,text)',
      'reject_proactive_contact_cap_with_engine_route()',
      '_flow_engine_reason(text,text)',
      '_flow_engine_record_start_rejection(uuid,integer,uuid,text,text)',
      '_flow_engine_record_divergence(text,uuid,integer,text,text,uuid)',
      `_flow_engine_plan_legacy_cart(uuid,uuid,text,text,text,${TZ},bigint,bigint,text,text,integer,text,integer)`,
      '_flow_engine_legacy_dispatch_ok(uuid,integer)',
      `_flow_engine_admit_cart_motor(uuid,uuid,text,text,text,${TZ},bigint,bigint,text,text)`,
      `_flow_engine_authorize_start(uuid,uuid,${TZ},boolean)`,
    ];
    const PATCHED = {
      [`plan_lancemos_pilot_cart_recovery(uuid,uuid,text,text,text,text,integer,${TZ},bigint,bigint,text,text,integer)`]:
        [['flow_engine_motor_delegation', '    -- flow_engine_legacy_guard: begin\n']],
      [`mark_lancemos_pilot_request_started(uuid,uuid,text,bigint,${TZ})`]:
        [['flow_engine_motor_delegation', '    -- flow_engine_legacy_guard: begin\n']],
      [`mark_followup_request_started(uuid,uuid,text,bigint,${TZ})`]:
        [['flow_engine_motor_authorization', '    if not exists (\n']],
      [`reevaluate_followup_action(uuid,text,bigint,${TZ},boolean,text,text,${TZ},text,boolean,boolean,boolean,boolean,boolean)`]:
        [['flow_engine_turned_off', "    if v_decision = 'defer' then\n"], ['flow_engine_turned_off_close', '\n        ) then\n']],
      'operator_create_flow_route_v1(jsonb,text,text,text,text,text,text)':
        [['flow_engine_route_flow_on', '    insert into public.flow_engine_routes (']],
      '_flow_engine_legacy_guard(text,text)': [['flow_engine_motor_guard', "    if v_route.executor = 'motor' then\n"]],
    };
    const KINDS = '_flow_engine_route_kinds()';
    const added = [...after.keys()].filter((signature) => !before.has(signature));
    const removed = [...before.keys()].filter((signature) => !after.has(signature));
    const changed = [...before.keys()].filter((signature) => after.has(signature)
      && ['definition', 'service_x', 'api_x'].some((key) => after.get(signature)[key] !== before.get(signature)[key]));
    if (!sameSet(added, ADDED) || removed.length !== 0 || !sameSet(changed, [...Object.keys(PATCHED), KINDS])) {
      fail('la migracion cambio otra cosa', { added, removed, changed });
    }
    for (const signature of added) {
      const row = after.get(signature);
      if (row.api_x || row.service_x !== SERVICE.includes(signature)) fail(`${signature}: permisos`, row);
    }
    const blockRe = (marker) => (marker === 'flow_engine_turned_off_close'
      ? new RegExp(`,\\n {12}-- ${marker}: begin\\n[\\s\\S]*? {12}-- ${marker}: end`)
      : new RegExp(` {4}-- ${marker}: begin\\n[\\s\\S]*? {4}-- ${marker}: end\\n\\n?`));
    // El problema de forma de una definicion parchada: null si es la vigente
    // con sus bloques insertados (cada uno una vez, antes de su ancla) y nada mas.
    const shapeProblem = (signature, definition) => {
      let current = definition;
      for (const [marker, follows] of PATCHED[signature]) {
        const match = current.match(blockRe(marker));
        if (match === null || !current.slice(match.index + match[0].length).startsWith(follows)
            || (current.match(new RegExp(`${marker}: begin`, 'g')) || []).length !== 1) {
          return `el bloque ${marker} no esta, o no esta antes de su ancla`;
        }
        current = current.replace(blockRe(marker), '');
      }
      return current === before.get(signature).definition ? null : 'no es su definicion vigente con sus bloques insertados';
    };
    for (const signature of Object.keys(PATCHED)) {
      const problem = shapeProblem(signature, after.get(signature).definition);
      if (problem !== null
          || after.get(signature).service_x !== before.get(signature).service_x
          || after.get(signature).api_x !== before.get(signature).api_x) {
        fail(`${signature}: ${problem ?? 'cambio un permiso'}`);
      }
    }
    if (!after.get(KINDS).definition.includes("select array['carrito']::text[]")
        || before.get(KINDS).definition.includes("'carrito'")) {
      fail('_flow_engine_route_kinds no es {carrito}');
    }
    // El rojo de la forma: una sentencia de mas fuera de los bloques, o un
    // bloque que falta, lo ve el mismo chequeo de arriba (shapeProblem). Antes
    // comparaba contra `before` un string adulterado a mano, y eso pasaba
    // siempre aunque el chequeo estuviera roto; ahora el adulterado pasa por
    // el chequeo real y tiene que dar un problema.
    {
      const signature = `mark_followup_request_started(uuid,uuid,text,bigint,${TZ})`;
      const real = after.get(signature).definition;
      const extra = real.replace('\nbegin\n', '\nbegin\n    perform 1;\n');
      const missing = real.replace(blockRe('flow_engine_motor_authorization'), '');
      if (extra === real || missing === real
          || shapeProblem(signature, extra) === null || shapeProblem(signature, missing) === null) {
        fail('el rojo de la forma no es rojo');
      }
    }
    results.migracion = { agregadas: added.length, cambiadas: changed.length, service_role: SERVICE.length };
  }

  // -------------------------------------------------------------------------
  // 1. La ruta.
  // -------------------------------------------------------------------------
  const created = await createRoute(A.funnel, 'carrito', 'carrito', SCOPE);
  const createdPay = await createRoute(A.funnel, 'pago_fallido', 'pago_fallido', SCOPE);
  const createdB = await createRoute(B.funnel, 'carrito', 'carrito', B.scope);
  if (!created.ok || !createdPay.ok || !createdB.ok) fail('crear las rutas', { created, createdPay, createdB });
  {
    // Decision 8: con el flujo apagado no se crea la ruta (y el rojo: prendido si).
    const flowOff = await register(A.funnel, 'carrito_apagado');
    await cycle([versionItem(flowOff, A.funnel, 'carrito_apagado',
      { ...cartDefinition(A.funnel), flujo: 'carrito_apagado' })], 'v1 apagada');
    if ((await control(flowOff)).state !== 'apagado') fail('el flujo nuevo no nacio apagado');
    const off = await createRoute(A.funnel, 'carrito_apagado', 'carrito', B.scope);
    if (off.reason_code !== 'flow_route_flow_off') fail('crear la ruta con el flujo apagado', off);
    await db.exec('begin');
    try {
      await cycle([stateItem(flowOff, A.funnel, 'carrito_apagado', 'prender')], 'prender el apagado');
      const on = await createRoute(A.funnel, 'carrito_apagado', 'carrito', B.scope);
      if (on.reason_code === 'flow_route_flow_off') fail('el rojo de flow_route_flow_off: prendido tambien rechaza', on);
    } finally {
      await db.exec('rollback');
    }
    results.crear_con_flujo_apagado = off.reason_code;
  }

  // Lo en curso del legado antes de mover: A1 aceptado y A2 esperando.
  const espera = await cartFor('espera-legado');
  await planCart(espera);
  if (await runOf(espera.plan.recovery_case_id)) fail('el caso del legado tiene recorrido del motor');
  {
    const reserved = await reserve(espera, espera.plan.scheduled_action_id);
    const started = await startOld(reserved);
    if (started.phase !== 'request_started') fail('A1 del legado', started);
    await accept(espera, reserved);
    espera.a2 = await stepOf(espera, 'cart_second_touch');
    if (espera.a2.status !== 'pending') fail('A2 del legado', espera.a2.status);
  }
  results.ruta = {};
  results.ruta.pago_a_motor = (await route(A.funnel, 'pago_fallido', FLOWS.pago_fallido, 'motor')).reason_code;
  if (results.ruta.pago_a_motor !== 'flow_route_kind_not_supported') fail('el pago fallido a motor', results.ruta);
  const toMotor = await route(A.funnel, 'carrito', FLOWS.carrito, 'motor');
  if (!toMotor.ok || toMotor.route.executor !== 'motor') fail('el carrito a motor', toMotor);
  results.ruta.carrito_a_motor = 'ok';

  // Helpers de la prueba.
  const CONTEXT = 'select * from public.flow_engine_execution_context_v1($1)';
  const contextOf = async (actionId) => (await db.query(CONTEXT, [actionId])).rows;
  const sql1 = async (text, params, label) => one((await db.query(text, params)).rows, label);
  const rejectionsOf = async (sourceRef) => (await db.query(`
    select phase, reason, count::int as count, version from public.flow_engine_start_rejections
     where flow_id = $1 and source_ref = $2 order by reason`, [FLOWS.carrito, sourceRef])).rows;
  const divergenceOf = async (kind, reason) => (await db.query(`
    select id, count::int as count, sqlstate, last_source_ref from public.flow_engine_divergences
     where flow_id = $1 and kind = $2 and reason = $3`, [FLOWS.carrito, kind, reason])).rows[0] ?? null;
  const firstAcceptance = async (caseId) => new Date((await sql1(`
    select min(attempt.accepted_at) as at from public.followup_delivery_attempts attempt
      join public.scheduled_actions action on action.id = attempt.action_id
     where action.recovery_case_id = $1 and attempt.outcome = 'accepted_by_chatwoot'`, [caseId], 'primera aceptacion')).at);
  const minutesBetween = (a, b) => Math.round((new Date(b).getTime() - new Date(a).getTime()) / 60_000);
  const sequenceOf = async (lead) => sql1(`select status, completion_reason from public.followup_sequences
    where recovery_case_id = $1 order by created_at desc limit 1`, [lead.plan.recovery_case_id], 'secuencia');
  const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
  const ulid = () => {
    let value = (BigInt(Date.now()) << 80n) | BigInt(`0x${randomBytes(10).toString('hex')}`);
    let out = '';
    for (let i = 0; i < 26; i += 1) {
      out = CROCKFORD[Number(value & 31n)] + out;
      value >>= 5n;
    }
    return out;
  };
  const claimCoupons = async () => db.query(`
    select * from public.claim_due_recovery_coupons_v1(
      $1::timestamptz,'motor-cupones',60,'ATT','45 minutes'::interval,'24 hours'::interval,0.1::numeric,100)
  `, [await dbNow()]);
  // Un error de una consulta, sin deshacer lo de antes.
  const errOf = (run) => errorOf(db, run);
  const motorCart = async (label, options) => {
    const lead = await cartFor(label, options);
    const row = await admit(lead);
    if (row.outcome !== 'motor_arranco' || row.created !== true) fail(`${label}: la admision`, row);
    return lead;
  };
  // Los tres toques de un caso del motor, con la imagen nueva o la vieja.
  const runStep = async (lead, actionId, image) => {
    const reserved = await reserve(lead, actionId);
    const started = image === 'nueva' ? await startNew(reserved) : await startOld(reserved);
    const phase = image === 'nueva' ? started.outcome : started.phase;
    if (phase !== 'request_started') fail(`${lead.label}: el arranque (${image})`, started);
    await accept(lead, reserved);
    return { reserved, started };
  };

  // -------------------------------------------------------------------------
  // 2. A1 por el motor: por el envoltorio de hoy y por la admision.
  // -------------------------------------------------------------------------
  const policyKey = `flujo:${FLOWS.carrito}`;
  const viaWrapper = await cartFor('motor-envoltorio');
  const viaAdmit = await cartFor('motor-admision');
  {
    const planned = await planCart(viaWrapper);
    const admitted = await admit(viaAdmit);
    if (!planned.created || admitted.outcome !== 'motor_arranco' || admitted.created !== true
        || admitted.reason !== null || admitted.divergence_id !== null) {
      fail('A1 por el motor', { planned, admitted });
    }
    for (const lead of [viaWrapper, viaAdmit]) {
      const run = await runOf(lead.plan.recovery_case_id);
      const kase = await caseOf(lead);
      const binding = await sql1('select * from public.pilot_recovery_case_bindings where recovery_case_id = $1',
        [lead.plan.recovery_case_id], 'binding');
      if (!run || run.flow_id !== FLOWS.carrito || run.version !== 1 || run.pool !== SCOPE || !run.legacy_dispatch_ok
          || run.trigger_source_ref !== lead.event || run.person_ref !== lead.contact
          || kase.policy_key !== policyKey || kase.policy_version !== 1
          || binding.scope_key !== SCOPE || binding.scope_version !== 1 || binding.audience_mode !== 'consented_intent'
          || binding.audience_purchase_intent_id === null) {
        fail(`${lead.label}: el caso del motor`, { run, kase, binding });
      }
    }
    const policy = await sql1(`select status, max_automatic_messages, steps, approved_by, timezone
      from public.followup_policy_versions where policy_key = $1 and version = 1`, [policyKey], 'politica compilada');
    if (policy.status !== 'published' || policy.max_automatic_messages !== 3 || policy.steps.length !== 3
        || policy.steps[1].delay !== '1 minute' || policy.steps[2].delay !== '3 minutes' || policy.approved_by !== 'dan') {
      fail('la politica compilada', policy);
    }
    // La repeticion del mismo evento: el mismo caso, created = false.
    const again = await admit(viaAdmit);
    if (again.outcome !== 'motor_arranco' || again.created !== false
        || again.recovery_case_id !== viaAdmit.plan.recovery_case_id) fail('la repeticion', again);
    // La admision espera un lock a lo sumo 3 s, y deja el lock_timeout de quien
    // la llama como estaba.
    await db.exec('begin');
    let kept;
    try {
      await db.exec("set local lock_timeout = '7s'");
      await admit(await cartFor('lock-timeout'));
      kept = one((await db.query('show lock_timeout')).rows, 'lock_timeout').lock_timeout;
    } finally {
      await db.exec('rollback');
    }
    if (kept !== '7s') fail('la admision cambio el lock_timeout de la transaccion', kept);
    results.a1_por_el_motor = { envoltorio: 'motor_arranco', admision: admitted.outcome, repeticion: 'created_false',
      lock_timeout: kept };
  }

  // -------------------------------------------------------------------------
  // 3. A1, A2 y Z con la imagen nueva.
  // -------------------------------------------------------------------------
  {
    const lead = viaAdmit;
    await claimCoupons();
    const wantContext = (row, ordinal, send, stepKey) => row
      && row.send_ordinal === ordinal && row.step_key === stepKey && row.template_name === send.plantilla
      && row.template_language === send.idioma && JSON.stringify(row.template_variables) === JSON.stringify(send.variables)
      && JSON.stringify(row.template_button ?? null) === JSON.stringify(send.boton ?? null)
      && row.flow_id === FLOWS.carrito && row.version === 1 && row.legacy_dispatch_ok === true;
    // A1.
    const a1 = await reserve(lead, lead.plan.scheduled_action_id);
    const [ctx1] = await contextOf(a1.actionId);
    if (!wantContext(ctx1, 1, A1, 'first_contact')) fail('el contexto de A1', ctx1);
    const started = await startNew(a1);
    const auth = await sql1('select * from public.flow_engine_request_authorizations where attempt_id = $1',
      [a1.attempt.id], 'autorizacion del motor');
    if (started.outcome !== 'request_started' || started.phase !== 'request_started' || started.reason !== null
        || started.flow_authorization_id !== auth.id || started.authorization_replayed !== false
        || auth.pool !== SCOPE || auth.version !== 1 || started.id !== a1.attempt.id) {
      fail('A1 request_started', { started, auth });
    }
    if ((await db.query('select 1 from public.pilot_outbound_request_authorizations where attempt_id = $1',
      [a1.attempt.id])).rows.length !== 0) fail('A1 escribio la autorizacion del piloto');
    const replay = await startNew(a1);
    if (replay.outcome !== 'request_started' || replay.authorization_replayed !== true
        || replay.flow_authorization_id !== auth.id) fail('el replay de A1', replay);
    await accept(lead, a1);
    // A2: el delay compilado (1 minuto desde la primera aceptacion).
    const a2 = await stepOf(lead, 'cart_second_touch');
    const first = await firstAcceptance(lead.plan.recovery_case_id);
    if (a2.policy_key !== policyKey || minutesBetween(first, a2.due_at) !== 1) fail('el due_at de A2', a2);
    const a2r = await reserve(lead, a2.id);
    if (a2r.decision.decision !== 'execute') fail('la reevaluacion de A2', a2r.decision);
    const [ctx2] = await contextOf(a2.id);
    if (!wantContext(ctx2, 2, A2, 'cart_second_touch')) fail('el contexto de A2', ctx2);
    if ((await startNew(a2r)).outcome !== 'request_started') fail('A2 request_started');
    await accept(lead, a2r);
    // Z: 3 minutos desde la primera aceptacion, el cupon 2, la renovacion y el link.
    const z = await stepOf(lead, 'cart_last_touch');
    if (minutesBetween(first, z.due_at) !== 3) fail('el due_at de Z', z);
    await claimCoupons();
    const coupon2 = (await db.query('select status from public.recovery_coupons where recovery_case_id = $1 and ordinal = 2',
      [lead.plan.recovery_case_id])).rows;
    if (coupon2.length !== 1) fail('el cupon 2 de Z', coupon2);
    const renewal = await sql1('select * from public.request_recovery_renewal_v1($1,$2,$3,$4,$5,$6)',
      [A.accountId, A.inboxId, Number(lead.chatwoot), 770001, lead.phone, (await dbNow()).toISOString()], 'renovacion');
    const zAfter = await stepOf(lead, 'cart_last_touch');
    if (renewal.outcome !== 'advanced' || new Date(zAfter.due_at) > await dbNow()) fail('la renovacion adelanta Z', { renewal, zAfter });
    const zr = await reserve(lead, z.id);
    const [ctx3] = await contextOf(z.id);
    if (!wantContext(ctx3, 3, Z, 'cart_last_touch')) fail('el contexto de Z', ctx3);
    const link = await sql1('select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)',
      [z.id, zr.worker, zr.lease, zr.attempt.id, ulid(), zr.now], 'link de Z');
    if (link.outcome !== 'reserved') fail('el link de Z', link.outcome);
    if ((await startNew(zr)).outcome !== 'request_started') fail('Z request_started');
    await accept(lead, zr);
    const sequence = await sequenceOf(lead);
    const kase = await caseOf(lead);
    if (sequence.status !== 'completed' || sequence.completion_reason !== 'policy_exhausted'
        || kase.status !== 'sequence_exhausted') fail('policy_exhausted', { sequence, kase: kase.status });
    // Una accion del legado no tiene contexto del motor.
    if ((await contextOf(espera.a2.id)).length !== 0) fail('el contexto de una accion del legado');
    results.imagen_nueva = { toques: 3, a2_minutos: 1, z_minutos: 3, cupon2: true, renovacion: renewal.outcome,
      link: link.outcome, cierre: sequence.completion_reason };
  }

  // -------------------------------------------------------------------------
  // 4. Principio 4: la imagen de hoy arranca lo del motor por mark_lancemos_*.
  // -------------------------------------------------------------------------
  {
    const lead = viaWrapper;
    const a1 = await runStep(lead, lead.plan.scheduled_action_id, 'vieja');
    const engine = (await db.query('select id from public.flow_engine_request_authorizations where attempt_id = $1',
      [a1.reserved.attempt.id])).rows;
    const pilot = (await db.query('select id from public.pilot_outbound_request_authorizations where attempt_id = $1',
      [a1.reserved.attempt.id])).rows;
    if (engine.length !== 1 || pilot.length !== 0 || a1.started.pilot_authorization_id !== engine[0].id
        || a1.started.pilot_authorization_replayed !== false) fail('A1 por mark_lancemos', { started: a1.started, engine, pilot });
    await runStep(lead, (await stepOf(lead, 'cart_second_touch')).id, 'vieja');
    await runStep(lead, (await stepOf(lead, 'cart_last_touch')).id, 'vieja');
    if ((await sequenceOf(lead)).completion_reason !== 'policy_exhausted') fail('los tres toques por mark_lancemos');
    // El rojo: un intento del motor sin autorizacion no cruza el arranque compartido.
    const red = await cartFor('sin-autorizacion');
    await admit(red);
    const reserved = await reserve(red, red.plan.scheduled_action_id);
    await expectError(db, 'el arranque compartido sin autorizacion', () => db.query(
      'select * from public.mark_followup_request_started($1,$2,$3,$4,$5)',
      [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, new Date()]),
    { code: '55000', message: 'pilot_request_authorization_required' });
    red.reserved = reserved;
    world.pending = red;
    results.principio_4 = { imagen_vieja: 'tres_toques', sin_autorizacion: 'pilot_request_authorization_required' };
  }

  // -------------------------------------------------------------------------
  // 10. Principio 10: lo en curso sigue con su version; la plantilla fijada.
  // -------------------------------------------------------------------------
  const v1Lead = world.pending;
  {
    const v2 = await publishCart({ a2Wait: '5m' }, 'v2 cambia la espera de A2');
    // El A1 en curso de la v1 arranca y su A2 sale con la espera de la v1.
    if ((await startNew(v1Lead.reserved)).outcome !== 'request_started') fail('A1 de la v1 con la v2 activa');
    await accept(v1Lead, v1Lead.reserved);
    const a2v1 = await stepOf(v1Lead, 'cart_second_touch');
    const firstV1 = await firstAcceptance(v1Lead.plan.recovery_case_id);
    // La v2: un caso nuevo, la misma plantilla (legacy_dispatch_ok) y otra espera.
    const lead = await motorCart('v2');
    const run = await runOf(lead.plan.recovery_case_id);
    await runStep(lead, lead.plan.scheduled_action_id, 'nueva');
    const a2v2 = await stepOf(lead, 'cart_second_touch');
    const firstV2 = await firstAcceptance(lead.plan.recovery_case_id);
    if (a2v1.policy_version !== 1 || minutesBetween(firstV1, a2v1.due_at) !== 1
        || run.version !== v2 || !run.legacy_dispatch_ok || a2v2.policy_version !== v2
        || minutesBetween(firstV2, a2v2.due_at) !== 5) {
      fail('la v2 que cambia una espera', { a2v1, run, a2v2 });
    }
    // Una v3 que cambia la plantilla de A2: la imagen vieja no la puede mandar.
    const v3 = await publishCart({ a2: { ...A2, plantilla: 'att1_carrito_dudas_02' } }, 'v3 cambia la plantilla de A2');
    const lead3 = await motorCart('v3');
    const run3 = await runOf(lead3.plan.recovery_case_id);
    const reserved = await reserve(lead3, lead3.plan.scheduled_action_id);
    await expectError(db, 'la imagen vieja con otra plantilla', () => startOld(reserved),
      { code: '55000', message: 'pilot_request_start_rejected', detail: 'flow_engine_needs_engine_image' });
    if (run3.version !== v3 || run3.legacy_dispatch_ok !== false) fail('legacy_dispatch_ok de la v3', run3);
    if ((await startNew(reserved)).outcome !== 'request_started') fail('la imagen nueva con la v3');
    await accept(lead3, reserved);
    const ctxV3 = (await contextOf((await stepOf(lead3, 'cart_second_touch')).id))[0];
    const ctxV1 = (await contextOf(a2v1.id))[0];
    if (ctxV3?.template_name !== 'att1_carrito_dudas_02' || ctxV3.version !== v3
        || ctxV1?.template_name !== A2.plantilla || ctxV1.version !== 1) {
      fail('la plantilla de la version fijada', { ctxV3, ctxV1 });
    }
    // De vuelta a los envios de la v1, para lo que sigue.
    const v4 = await publishCart({}, 'v4 vuelve a la v1');
    results.principio_10 = { v1_espera: 1, v2_espera: 5, v3_imagen_vieja: 'flow_engine_needs_engine_image', v4 };
  }

  // -------------------------------------------------------------------------
  // 5. Principio 1: la caida al legado con divergencia.
  // -------------------------------------------------------------------------
  let fallbackLead;
  {
    const vp = await publishCart({}, 'vP con la politica sembrada');
    await db.query(`
      insert into public.followup_policy_versions
        (policy_key, version, status, purpose, timezone, business_windows, grace_period, expires_after,
         max_automatic_messages, steps, approved_by, approved_at, published_at)
      select policy_key, $2, status, purpose, timezone,
             '[{"days": [1,2,3,4,5,6,7], "start": "08:00", "end": "20:00"}]'::jsonb,
             grace_period, expires_after, max_automatic_messages, steps, approved_by, now(), now()
        from public.followup_policy_versions where policy_key = $1 and version = 1
    `, [policyKey, vp]);
    const reason = 'politica_compilada:business_windows';
    const p1 = await cartFor('divergencia-1');
    const first = await admit(p1);
    const row1 = await divergenceOf('politica_compilada', reason);
    const kase = await caseOf(p1);
    if (first.outcome !== 'legado_por_divergencia' || first.divergence_first !== true || first.divergence_id !== row1?.id
        || row1.count !== 1 || kase.policy_key !== world.THREE_TOUCHES.policy || await runOf(kase.id)) {
      fail('(a) la primera divergencia', { first, row1, kase: kase.policy_key });
    }
    fallbackLead = p1;
    const p2 = await cartFor('divergencia-2');
    const second = await admit(p2);
    if (second.outcome !== 'legado_por_divergencia' || second.divergence_first !== false
        || (await divergenceOf('politica_compilada', reason)).count !== 2) fail('(a) la segunda divergencia', second);
    // Por el envoltorio (la imagen de hoy): el legado planifica y la divergencia queda.
    const p3 = await cartFor('divergencia-3');
    const viaOld = await planCart(p3);
    if (!viaOld.created || (await runOf(viaOld.recovery_case_id))
        || (await divergenceOf('politica_compilada', reason)).count !== 3) fail('(a) por el envoltorio', viaOld);
    // (b) Con el scope del legado pausado: legado_rechazo y la divergencia escrita.
    await setState(SCOPE, 1, 'paused');
    const p4 = await cartFor('divergencia-4');
    const rejected = await admit(p4);
    if (rejected.outcome !== 'legado_rechazo' || rejected.reason !== 'pilot_scope_rejected:pilot_runtime_not_armed'
        || rejected.recovery_case_id !== null || (await divergenceOf('politica_compilada', reason)).count !== 4) {
      fail('(b) legado_rechazo', rejected);
    }
    // Por el envoltorio, el rechazo es el de hoy (y su raise deshace la divergencia).
    const p5 = await cartFor('divergencia-5');
    const oldError = await errOf(() => planCart(p5));
    if (oldError?.message !== 'pilot_scope_rejected' || oldError.detail !== 'pilot_runtime_not_armed'
        || (await divergenceOf('politica_compilada', reason)).count !== 4) fail('(b) por el envoltorio', oldError?.message);
    await setState(SCOPE, 1, 'armed');
    // (c) p_force_legacy: sin el camino del motor, una divergencia transitoria.
    const p6 = await cartFor('forzado');
    const forced = await admit(p6, true);
    const transient = await divergenceOf('transitorio', 'transitorio:forzado_al_legado');
    if (forced.outcome !== 'legado_por_divergencia' || forced.divergence_first !== true || transient?.count !== 1) {
      fail('(c) p_force_legacy', { forced, transient });
    }
    // El rojo: sin el bloque protegido, la divergencia escapa como error.
    const p7 = await cartFor('sin-proteccion');
    await expectError(db, 'el camino del motor sin el bloque protegido', () => db.query(
      'select * from public._flow_engine_admit_cart_motor($1,$2,$3,$4,$5,$6::timestamptz,$7,$8,$9,$10)',
      p7.admitArgs.slice(0, 10)), { code: '55000', message: 'flow_engine_divergence', detail: reason });
    await publishCart({}, 'vQ sin la politica sembrada');
    results.principio_1 = { a: 'legado_por_divergencia', count: 4, b: rejected.reason, c: 'transitorio', rojo: 'escapa' };
  }

  // -------------------------------------------------------------------------
  // 6. Principio 2: un error de compilacion lo atrapa la relectura.
  // -------------------------------------------------------------------------
  {
    await motorCart('compila-bien');
    await db.exec('begin');
    let red;
    try {
      await db.exec(`create or replace function public._flow_engine_compile_policy(
          p_definition jsonb, p_flow_id uuid, p_version integer, p_legacy_kind text)
        returns public.followup_policy_versions language plpgsql stable security invoker
        set search_path = pg_catalog, public, pg_temp as $f$
        declare v_row public.followup_policy_versions;
        begin
          v_row := public._flow_engine_compile_policy_core(p_definition, p_flow_id::text, p_version, p_legacy_kind,
              public._flow_engine_fixed_zone(p_flow_id));
          v_row.business_windows := jsonb_set(v_row.business_windows, '{0,days}', '[0,1,2,3,4,5,6]'::jsonb);
          return v_row;
        end $f$`);
      red = await admit(await cartFor('domingo-cero'));
    } finally {
      await db.exec('rollback');
    }
    if (red.outcome !== 'legado_por_divergencia') fail('el rojo del domingo como 0 no lo atrapa la relectura', red);
    results.principio_2 = { rojo_domingo_cero: red.outcome };
  }

  // -------------------------------------------------------------------------
  // 7. Principio 6: la audiencia.
  // -------------------------------------------------------------------------
  {
    const noForm = await cartFor('sin-formulario', { form: false });
    const row = await admit(noForm);
    if (row.outcome !== 'motor_no_arranca' || row.reason !== 'no_entra:permiso') fail('no_entra:permiso', row);
    const rejections = await rejectionsOf(noForm.event);
    if (rejections.length !== 1 || rejections[0].phase !== 'admision' || rejections[0].reason !== 'no_entra:permiso') {
      fail('la fila de no_entra:permiso', rejections);
    }
    const viaOld = await errOf(() => planCart(noForm));
    if (viaOld?.message !== 'pilot_scope_rejected' || viaOld.detail !== 'no_entra_permiso') fail('no_entra por el envoltorio', viaOld?.detail);
    // La baja previa al entrar.
    const lead = person('baja-previa');
    await sql1(`select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,$6::timestamptz,'unsubscribe')`,
      [A.accountId, A.inboxId, 880001, 9880001, lead.phone, new Date(Date.now() - 90 * 60_000).toISOString()], 'baja');
    await cartFor(lead);
    const baja = await admit(lead);
    if (baja.outcome !== 'motor_no_arranca' || baja.reason !== 'baja') fail('baja', baja);
    // El rojo: sin el chequeo de la audiencia (la intencion correlacionada
    // tomada como permitida), la persona dada de baja arranca.
    const redLead = person('baja-previa-rojo');
    await sql1(`select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,$6::timestamptz,'unsubscribe')`,
      [A.accountId, A.inboxId, 880002, 9880002, redLead.phone, new Date(Date.now() - 90 * 60_000).toISOString()], 'baja');
    await cartFor(redLead);
    await db.exec('begin');
    let red;
    try {
      await db.exec(`create or replace function public._portable_cart_audience_checks(
          p_scope_key text, p_scope_version integer, p_webhook_event_id uuid, p_contact_id uuid,
          p_offer_code text, p_external_user_id text)
        returns table (purchase_intent_id uuid, precheckout_submission_id uuid, reason_code text)
        language sql stable as $f$
          select c.purchase_intent_id,
                 (select l.submission_id from public.purchase_intent_submissions l
                   where l.purchase_intent_id = c.purchase_intent_id order by l.ordinal desc limit 1),
                 'pilot_audience_allowed'::text
            from public.hotmart_purchase_intent_correlations c
           where c.webhook_event_id = p_webhook_event_id and c.outcome = 'resolved' $f$`);
      red = await admit(redLead);
    } finally {
      await db.exec('rollback');
    }
    if (red.outcome !== 'motor_arranco') fail('el rojo de la audiencia no es rojo', red);
    results.principio_6 = { sin_formulario: row.reason, baja: baja.reason, rojo: red.outcome };
  }

  // -------------------------------------------------------------------------
  // 8. Principio 7: con el carrito en motor, el legado en curso.
  // -------------------------------------------------------------------------
  {
    await pauseFlow(FLOWS.carrito, A.funnel, 'carrito');
    const reserved = await reserve(espera, espera.a2.id);
    await expectError(db, 'el A2 del legado con el flujo pausado', () => startOld(reserved),
      { code: '55000', message: 'pilot_request_start_rejected', detail: 'flujo_pausado' });
    // Pausado, la admision no arranca y deja su fila.
    const paused = await cartFor('admision-pausada');
    const pausedRow = await admit(paused);
    if (pausedRow.outcome !== 'motor_no_arranca' || pausedRow.reason !== 'flujo_pausado'
        || (await rejectionsOf(paused.event)).length !== 1) fail('la admision pausada', pausedRow);
    await setFlow(FLOWS.carrito, A.funnel, 'carrito', 'prender');
    // El rojo: con la guarda de la T2.3a, el legado en curso no sale nunca.
    await db.exec('begin');
    let red;
    try {
      const current = one((await db.query(
        "select pg_get_functiondef('public._flow_engine_legacy_guard(text,text)'::regprocedure) as d")).rows, 'guarda').d;
      await db.exec(current.replace(/ {4}-- flow_engine_motor_guard: begin\n[\s\S]*? {4}-- flow_engine_motor_guard: end\n/, ''));
      red = await errOf(() => startOld(reserved));
    } finally {
      await db.exec('rollback');
    }
    if (red?.detail !== 'flow_engine_not_available') fail('el rojo de la guarda', red?.detail);
    const started = await startOld(reserved);
    if (started.phase !== 'request_started') fail('el A2 del legado al prender', started);
    await accept(espera, reserved);
    results.principio_7 = { pausado: 'flujo_pausado', prendido: 'request_started', rojo: red.detail };
  }

  // -------------------------------------------------------------------------
  // 9. Principio 8: el protocolo del rechazo.
  // -------------------------------------------------------------------------
  {
    const lead = await motorCart('tope');
    const reserved = await reserve(lead, lead.plan.scheduled_action_id);
    // El tope del dia de la definicion (400), gastado por filas de la bolsa.
    await db.exec('begin');
    let outcomes;
    try {
      const used = await sql1(`
        select (select count(*) from public.pilot_outbound_request_authorizations where scope_key = $1
                  and local_budget_date = (now() at time zone 'America/Mexico_City')::date)
             + (select count(*) from public.flow_engine_request_authorizations where pool = $1
                  and local_budget_date = (now() at time zone 'America/Mexico_City')::date) as n`, [SCOPE], 'usado');
      await db.query(`
        insert into public.flow_engine_request_authorizations
          (flow_id, version, pool, legacy_scope_key, action_id, attempt_id, contact_id, local_budget_date, flow_generation)
        select $1, 1, $2, $2, gen_random_uuid(), gen_random_uuid(), gen_random_uuid(),
               (now() at time zone 'America/Mexico_City')::date, 0
          from generate_series(1, $3)`, [FLOWS.carrito, SCOPE, 400 - Number(used.n)]);
      const first = await startNew(reserved);
      const second = await startNew(reserved);
      const rows = await rejectionsOf(reserved.actionId);
      const attempt = await sql1('select phase from public.followup_delivery_attempts where id = $1', [reserved.attempt.id], 'intento');
      outcomes = { first, second, rows, attempt };
    } finally {
      await db.exec('rollback');
    }
    if (outcomes.first.outcome !== 'rechazado' || outcomes.first.reason !== 'tope_del_flujo'
        || outcomes.second.outcome !== 'rechazado' || outcomes.rows.length !== 1 || outcomes.rows[0].count !== 2
        || outcomes.rows[0].phase !== 'despacho' || outcomes.attempt.phase !== 'reserved'
        || outcomes.first.phase !== 'reserved' || outcomes.first.flow_authorization_id !== null) {
      fail('tope_del_flujo', outcomes);
    }
    // Pausado: rechazado y se confirma; al prender, sale.
    await pauseFlow(FLOWS.carrito, A.funnel, 'carrito');
    const pausedStart = await startNew(reserved);
    const pausedRows = await rejectionsOf(reserved.actionId);
    if (pausedStart.outcome !== 'rechazado' || pausedStart.reason !== 'flujo_pausado'
        || pausedRows.find((row) => row.reason === 'flujo_pausado')?.count !== 1) fail('rechazado pausado', { pausedStart, pausedRows });
    await setFlow(FLOWS.carrito, A.funnel, 'carrito', 'prender');
    if ((await startNew(reserved)).outcome !== 'request_started') fail('al prender sale');
    await accept(lead, reserved);
    // terminado: apagado despues de reservar.
    const ending = await motorCart('terminado');
    const endReserved = await reserve(ending, ending.plan.scheduled_action_id);
    await setFlow(FLOWS.carrito, A.funnel, 'carrito', 'apagar');
    const offAdmission = await admit(await cartFor('admision-apagada'));
    if (offAdmission.outcome !== 'motor_no_arranca' || offAdmission.reason !== 'flujo_apagado') fail('admision apagada', offAdmission);
    await expectError(db, 'la imagen vieja con el flujo apagado', () => startOld(endReserved),
      { code: '55000', message: 'pilot_request_start_rejected', detail: 'flujo_apagado' });
    const ended = await startNew(endReserved);
    const endAttempt = await sql1('select phase, outcome, reason_code from public.followup_delivery_attempts where id = $1',
      [endReserved.attempt.id], 'intento terminado');
    const endAction = await sql1('select status, terminal_reason from public.scheduled_actions where id = $1',
      [endReserved.actionId], 'accion terminada');
    const endSequence = await sequenceOf(ending);
    const endCase = await caseOf(ending);
    if (ended.outcome !== 'terminado' || ended.reason !== 'flujo_apagado' || endAttempt.phase !== 'completed'
        || endAttempt.outcome !== 'failed_before_request' || endAction.status !== 'cancelled'
        || endAction.terminal_reason !== 'flujo_apagado' || endSequence.completion_reason !== 'flujo_apagado'
        || endCase.status !== 'cancelled') {
      fail('terminado', { ended, endAttempt, endAction, endSequence, endCase: endCase.status });
    }
    await setFlow(FLOWS.carrito, A.funnel, 'carrito', 'prender');
    // Apagado y prendido: el A2 del caso de antes se cancela en la reevaluacion.
    const a2 = await stepOf(lead, 'cart_second_touch');
    const fence = await claim(lead.label, a2.id);
    await db.exec('begin');
    let red;
    try {
      const current = one((await db.query(`select pg_get_functiondef(p.oid) as d from pg_proc p
        where p.proname = 'reevaluate_followup_action' and p.pronamespace = 'public'::regnamespace`)).rows, 'reevaluacion').d;
      await db.exec(current.replace(/ {4}-- flow_engine_turned_off: begin\n[\s\S]*? {4}-- flow_engine_turned_off: end\n\n/, ''));
      red = await reevaluate(a2.id, fence, world.evidence(lead, fence.now));
    } finally {
      await db.exec('rollback');
    }
    if (red.decision !== 'execute') fail('el rojo del apagado en la reevaluacion', red);
    const cancelled = await reevaluate(a2.id, fence, world.evidence(lead, fence.now));
    const afterCancel = await caseOf(lead);
    if (cancelled.decision !== 'cancel' || cancelled.reason_code !== 'flujo_apagado' || afterCancel.status !== 'cancelled'
        || (await sequenceOf(lead)).completion_reason !== 'flujo_apagado') {
      fail('apagado y prendido cancela', { cancelled, status: afterCancel.status });
    }
    results.principio_8 = { tope: 'tope_del_flujo', count_por_lease: 2, pausado: 'flujo_pausado', terminado: ended.reason,
      apagado_y_prendido: cancelled.reason_code, rojo: red.decision };
  }

  // -------------------------------------------------------------------------
  // 11. Principio 11: dos productos.
  // -------------------------------------------------------------------------
  {
    const b1 = await cartFor('producto-b', { P: B });
    const planned = await planCart(b1);
    if (!planned.created || await runOf(planned.recovery_case_id)) fail('B por el legado', planned);
    // Un evento de B con los argumentos del scope de A: nunca entra al flujo de A.
    const b2 = await cartFor('producto-b-en-a', { P: B, scope: SCOPE });
    const row = await admit(b2);
    const div = await divergenceOf('producto', 'producto:evento_de_otro_producto');
    if (row.outcome !== 'legado_rechazo' || row.reason !== 'pilot_scope_rejected:pilot_product_mismatch'
        || div?.count !== 1 || div.last_source_ref !== b2.event) fail('B en el scope de A', { row, div });
    if ((await db.query('select 1 from public.flow_engine_runs where trigger_source_ref = $1', [b2.event])).rows.length !== 0) {
      fail('un evento de B entro al flujo de A');
    }
    results.principio_11 = { b_legado: 'planificado', b_en_a: row.reason, divergencia: 'producto' };
  }

  // -------------------------------------------------------------------------
  // 12. Principio 13: la persona.
  // -------------------------------------------------------------------------
  {
    // Un pago fallido abierto: el carrito no arranca y su due_at no se mueve.
    const lead = person('pago-abierto');
    await world.admitForm(lead, new Date(Date.now() - 40 * 60_000));
    const refusal = await refusalFor(4, lead);
    await db.query("update public.scheduled_actions set due_at = now() + interval '2 hours' where id = $1",
      [refusal.scheduled_action_id]);
    const due = (await world.stepOfId(refusal.scheduled_action_id)).due_at;
    await cartFor(lead);
    const row = await admit(lead);
    const dueAfter = (await world.stepOfId(refusal.scheduled_action_id)).due_at;
    if (row.outcome !== 'motor_no_arranca' || row.reason !== 'no_arranca_mientras:pago_fallido'
        || new Date(due).getTime() !== new Date(dueAfter).getTime()) fail('pago fallido abierto', { row, due, dueAfter });
    const viaOld = await errOf(() => planCart(lead));
    if (viaOld?.detail !== 'no_arranca_mientras_pago_fallido') fail('pago fallido por el envoltorio', viaOld?.detail);
    // El rojo: el planificador base, sin el chequeo, le corre el due_at.
    await db.exec('begin');
    let moved;
    try {
      await db.query('select * from public.plan_cart_recovery_with_identity($1,$2,$3,$4,$5,$6,$7,$8::timestamptz,$9,$10,$11)',
        [lead.event, lead.contact, String(A.productId), A.productName, lead.offer.offer_code, policyKey,
          (await control(FLOWS.carrito)).active_version, lead.admitArgs[5], A.accountId, A.inboxId, lead.phone]);
      moved = (await world.stepOfId(refusal.scheduled_action_id)).due_at;
    } finally {
      await db.exec('rollback');
    }
    if (new Date(moved).getTime() === new Date(due).getTime()) fail('el rojo del due_at no es rojo');
    // ya_en_curso: un segundo carrito de la misma persona con el primero abierto.
    const twice = await motorCart('dos-carritos');
    await cartFor(twice);
    const again = await admit(twice);
    if (again.outcome !== 'motor_no_arranca' || again.reason !== 'ya_en_curso') fail('ya_en_curso', again);
    // no_arranca_mientras:ficha, con el caso abierto pasado a ficha en una
    // transaccion que se deshace (la ficha entera no es de esta prueba).
    const slip = person('ficha-abierta');
    await world.admitForm(slip, new Date(Date.now() - 40 * 60_000));
    const slipPlan = await refusalFor(12, slip);
    await db.exec('begin');
    let slipRow;
    try {
      await db.query("update public.followup_sequences set reason = 'payment_slip' where recovery_case_id = $1",
        [slipPlan.recovery_case_id]);
      await cartFor(slip);
      slipRow = await admit(slip);
    } finally {
      await db.exec('rollback');
    }
    if (slipRow.outcome !== 'motor_no_arranca' || slipRow.reason !== 'no_arranca_mientras:ficha') fail('ficha', slipRow);
    // La repeticion de un evento que planifico el legado es del legado (decision 6).
    espera.admitArgs = [...espera.cartArgs.slice(0, 5), espera.cartArgs[6], ...espera.cartArgs.slice(7, 11), 1, espera.cartArgs[5], 1];
    const legacyReplay = await admit(espera);
    if (legacyReplay.outcome !== 'legado' || legacyReplay.created !== false) fail('la repeticion del legado', legacyReplay);
    results.principio_13 = { pago_fallido: row.reason, ya_en_curso: again.reason, ficha: slipRow.reason,
      repeticion_del_legado: legacyReplay.outcome };
  }

  // -------------------------------------------------------------------------
  // 13. La bolsa compartida, en los dos sentidos.
  // -------------------------------------------------------------------------
  {
    // Del motor al legado: con las filas del motor, el legado agota el dia (500).
    const reserved = await reserve(fallbackLead, fallbackLead.plan.recovery_case_id
      ? (await stepOf(fallbackLead, 'first_contact')).id : null);
    await db.exec('begin');
    let toLegacy;
    try {
      const used = await sql1(`
        select (select count(*) from public.pilot_outbound_request_authorizations where scope_key = $1
                  and local_budget_date = (now() at time zone $2)::date)
             + (select count(*) from public.flow_engine_request_authorizations where pool = $1
                  and local_budget_date = (now() at time zone $2)::date) as n`, [SCOPE, A.timezone], 'usado');
      await db.query(`
        insert into public.flow_engine_request_authorizations
          (flow_id, version, pool, legacy_scope_key, action_id, attempt_id, contact_id, local_budget_date, flow_generation)
        select $1, 1, $2, $2, gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), (now() at time zone $3)::date, 0
          from generate_series(1, $4)`, [FLOWS.carrito, SCOPE, A.timezone, 500 - Number(used.n)]);
      toLegacy = await errOf(() => startOld(reserved));
    } finally {
      await db.exec('rollback');
    }
    if (toLegacy?.detail !== 'pilot_daily_budget_exhausted') fail('del motor al legado', toLegacy?.detail);
    if ((await startOld(reserved)).phase !== 'request_started') fail('el legado sin el tope');
    await accept(fallbackLead, reserved);
    // Del legado al motor: una version con el tope del dia justo en lo usado.
    await db.exec('begin');
    let toEngine;
    try {
      const counts = await sql1(`
        select (select count(*)::int from public.pilot_outbound_request_authorizations where scope_key = $1
                  and local_budget_date = (now() at time zone 'America/Mexico_City')::date) as legacy,
               (select count(*)::int from public.flow_engine_request_authorizations where pool = $1
                  and local_budget_date = (now() at time zone 'America/Mexico_City')::date) as engine`, [SCOPE], 'cuentas');
      if (counts.legacy < 1) fail('no hay autorizaciones del legado hoy: el sentido no se prueba');
      await publishCart({ topes: { por_dia: counts.legacy + counts.engine } }, 'tope justo');
      const lead = await motorCart('bolsa-del-legado');
      toEngine = await startNew(await reserve(lead, lead.plan.scheduled_action_id));
      toEngine.counts = counts;
    } finally {
      await db.exec('rollback');
    }
    if (toEngine.outcome !== 'rechazado' || toEngine.reason !== 'tope_del_flujo') fail('del legado al motor', toEngine);
    results.bolsa = { motor_al_legado: 'pilot_daily_budget_exhausted', legado_al_motor: toEngine.reason };
  }

  // -------------------------------------------------------------------------
  // 14. read_flow_engine_v1: lo en curso del motor por version.
  // -------------------------------------------------------------------------
  {
    const read = await rpc('read_flow_engine_v1', world.INSTANCE, null);
    const cart = read.flows.find((flow) => flow.flow_id === FLOWS.carrito);
    const open = (await db.query(`
      select run.version, count(*)::int as n from public.flow_engine_runs run
        join public.recovery_cases rc on rc.id = run.recovery_case_id
       where run.flow_id = $1 and rc.status in ('grace_period', 'active', 'paused') group by run.version`,
    [FLOWS.carrito])).rows;
    const byVersion = Object.fromEntries(cart.versions.map((version) => [version.version, version.in_flight]));
    if (cart.route.executor !== 'motor' || open.length === 0
        || open.some((row) => byVersion[row.version] !== row.n)
        || !(cart.active_version in byVersion)) fail('read_flow_engine_v1', { versions: byVersion, open });
    results.lectura = byVersion;
  }

  // -------------------------------------------------------------------------
  // 15. El tope por persona no se carga con una ruta en motor (decision 4).
  // -------------------------------------------------------------------------
  {
    const insertCap = () => db.query(`insert into public.pilot_proactive_contact_caps
      (tenant_key, max_request_starts, request_window, approved_by) values ($1, 2, interval '1 day', 'dan')`, [A.tenant]);
    await expectError(db, 'el tope por persona con la ruta en motor', insertCap,
      { code: '55000', message: 'pilot_proactive_contact_cap_with_engine_route' });
    // El rojo: con la ruta de vuelta en legado, el renglon entra.
    await db.exec('begin');
    let red;
    try {
      const back = await route(A.funnel, 'carrito', FLOWS.carrito, 'legado');
      if (!back.ok) fail('de vuelta a legado', back);
      red = await errOf(insertCap);
    } finally {
      await db.exec('rollback');
    }
    if (red !== null) fail('el rojo del tope por persona no es rojo', red.message);
    // El UPDATE de un renglon que ya estaba (cargado con el disparador
    // apagado, en una transaccion que se deshace) tambien se rechaza.
    await db.exec('begin');
    let updated;
    try {
      await db.exec('alter table public.pilot_proactive_contact_caps disable trigger pilot_proactive_contact_caps_no_engine_route');
      await insertCap();
      await db.exec('alter table public.pilot_proactive_contact_caps enable trigger pilot_proactive_contact_caps_no_engine_route');
      updated = await errOf(() => db.query(
        'update public.pilot_proactive_contact_caps set max_request_starts = 3 where tenant_key = $1', [A.tenant]));
    } finally {
      await db.exec('rollback');
    }
    if (updated?.code !== '55000' || updated.message !== 'pilot_proactive_contact_cap_with_engine_route') {
      fail('el UPDATE del tope por persona con la ruta en motor', updated ? { code: updated.code, message: updated.message } : 'paso');
    }
    results.tope_por_persona = 'pilot_proactive_contact_cap_with_engine_route';
    results.tope_por_persona_update = updated.message;
  }

  const usedToday = async () => Number((await sql1(`select count(*)::int as n from public.flow_engine_request_authorizations
    where pool = $1 and local_budget_date = (now() at time zone $2)::date`, [SCOPE, A.timezone], 'cupo del dia')).n);
  const authorizationsOf = async (attemptId) => (await db.query(
    'select id from public.flow_engine_request_authorizations where attempt_id = $1', [attemptId])).rows;

  // -------------------------------------------------------------------------
  // 16. La baja entre la reevaluacion y el arranque, por las dos imagenes.
  // -------------------------------------------------------------------------
  // La baja que llega despues de leer la audiencia y de autorizar: la simula
  // un disparador que, al escribirse la autorizacion del motor, registra el
  // opt-out de Chatwoot (unmatched) con el wa_id textual 521... de una persona
  // de Mexico. Asi solo la ve el chequeo con las dos formas del telefono
  // (_portable_chatwoot_opt_out_stop) que corre despues de autorizar; el freno
  // del arranque compartido busca el id exacto de la identidad y no la ve.
  {
    const mexican = async (label) => {
      const lead = person(label, A, '5255550400');
      lead.country = '52';
      lead.iso = 'MX';
      await cartFor(lead);
      const row = await admit(lead);
      if (row.outcome !== 'motor_arranco') fail(`${label}: la admision`, row);
      return lead;
    };
    const lateOptOut = async () => db.exec(`
      create function pg_temp.baja_tardia() returns trigger language plpgsql as $f$
      begin
        insert into public.contact_opt_out_events (channel, purpose, source, canonical_account_id, canonical_inbox_id,
          canonical_conversation_id, canonical_message_id, external_user_id, occurred_at, normalized_rule_key,
          correlation_status)
        select 'whatsapp', 'cart_recovery', 'chatwoot', ${Number(A.accountId)}, ${Number(A.inboxId)},
               883000 + (select count(*) from public.contact_opt_out_events), 9883000, '521' || substr(c.phone, 3),
               clock_timestamp(), 'unsubscribe', 'unmatched'
          from public.contacts c where c.id = new.contact_id;
        return new;
      end $f$;
      create trigger baja_tardia after insert on public.flow_engine_request_authorizations
        for each row execute function pg_temp.baja_tardia();`);
    // La imagen nueva: rechazado por baja, la fila del despacho, sin
    // autorizacion y sin cupo consumido; el intento sigue reservado.
    const fresh = await mexican('baja-tardia-nueva');
    const freshReserved = await reserve(fresh, fresh.plan.scheduled_action_id);
    await db.exec('begin');
    let viaNew;
    try {
      await lateOptOut();
      const used = await usedToday();
      const started = await startNew(freshReserved);
      viaNew = { started, auth: await authorizationsOf(freshReserved.attempt.id), used, usedAfter: await usedToday(),
        rows: await rejectionsOf(freshReserved.actionId) };
    } finally {
      await db.exec('rollback');
    }
    if (viaNew.started.outcome !== 'rechazado' || viaNew.started.reason !== 'baja'
        || viaNew.started.phase !== 'reserved' || viaNew.started.flow_authorization_id !== null
        || viaNew.auth.length !== 0 || viaNew.usedAfter !== viaNew.used
        || viaNew.rows.length !== 1 || viaNew.rows[0].reason !== 'baja' || viaNew.rows[0].phase !== 'despacho') {
      fail('la baja tardia por la imagen nueva', viaNew);
    }
    // La imagen vieja (la delegacion en mark_lancemos_pilot_request_started).
    const old = await mexican('baja-tardia-vieja');
    const oldReserved = await reserve(old, old.plan.scheduled_action_id);
    await db.exec('begin');
    let viaOld;
    try {
      await lateOptOut();
      const used = await usedToday();
      const error = await errOf(() => startOld(oldReserved));
      const attempt = await sql1('select phase from public.followup_delivery_attempts where id = $1',
        [oldReserved.attempt.id], 'intento');
      viaOld = { error: error && { code: error.code, message: error.message, detail: error.detail },
        auth: await authorizationsOf(oldReserved.attempt.id), used, usedAfter: await usedToday(), phase: attempt.phase };
    } finally {
      await db.exec('rollback');
    }
    if (viaOld.error?.code !== '55000' || viaOld.error.message !== 'pilot_request_start_rejected'
        || viaOld.error.detail !== 'pilot_chatwoot_opt_out_stop' || viaOld.auth.length !== 0
        || viaOld.usedAfter !== viaOld.used || viaOld.phase !== 'reserved') {
      fail('la baja tardia por la imagen vieja', viaOld);
    }
    results.baja_al_arrancar = { nueva: viaNew.started.reason, vieja: viaOld.error.detail };
  }

  // -------------------------------------------------------------------------
  // 17. La audiencia otra vez al arrancar: la intencion ya no esta viva.
  // -------------------------------------------------------------------------
  {
    const lead = await motorCart('audiencia-al-arrancar');
    const reserved = await reserve(lead, lead.plan.scheduled_action_id);
    const intent = (await sql1('select audience_purchase_intent_id as id from public.pilot_recovery_case_bindings where recovery_case_id = $1',
      [lead.plan.recovery_case_id], 'binding')).id;
    await db.exec('begin');
    let outcome;
    try {
      await db.query("update public.purchase_intents set lifecycle_state = 'cancelled' where id = $1", [intent]);
      const used = await usedToday();
      const started = await startNew(reserved);
      outcome = { started, auth: await authorizationsOf(reserved.attempt.id), used, usedAfter: await usedToday(),
        rows: await rejectionsOf(reserved.actionId) };
    } finally {
      await db.exec('rollback');
    }
    if (outcome.started.outcome !== 'rechazado' || outcome.started.reason !== 'ya_no_entra'
        || outcome.auth.length !== 0 || outcome.usedAfter !== outcome.used
        || outcome.rows.find((row) => row.reason === 'ya_no_entra')?.phase !== 'despacho') {
      fail('la audiencia al arrancar', outcome);
    }
    results.audiencia_al_arrancar = outcome.started.reason;
  }

  // -------------------------------------------------------------------------
  // 18. Decision 2: lo estatico no es divergencia (el evento es del producto
  //     del flujo, asi que pasa el chequeo del producto y llega al estatico).
  // -------------------------------------------------------------------------
  {
    const divergences = async () => Number((await sql1('select count(*)::int as n from public.flow_engine_divergences where flow_id = $1',
      [FLOWS.carrito], 'divergencias')).n);
    const cases = {
      pilot_offer_mismatch: (args) => { args[4] = 'offzz9999'; },
      pilot_chatwoot_inbox_mismatch: (args) => { args[7] = Number(A.inboxId) + 1000; },
      pilot_chatwoot_account_mismatch: (args) => { args[6] = Number(A.accountId) + 1000; },
    };
    const seen = {};
    for (const [code, edit] of Object.entries(cases)) {
      const lead = await cartFor(`estatico-${code}`);
      edit(lead.admitArgs);
      const before = await divergences();
      const row = await admit(lead);
      const after = await divergences();
      if (row.outcome !== 'motor_no_arranca' || row.reason !== code || row.divergence_id !== null
          || row.recovery_case_id !== null || after !== before || (await rejectionsOf(lead.event)).length !== 0
          || (await db.query('select 1 from public.flow_engine_runs where trigger_source_ref = $1', [lead.event])).rows.length !== 0) {
        fail(`lo estatico: ${code}`, { row, before, after });
      }
      seen[code] = row.outcome;
    }
    results.estatico = seen;
  }

  // -------------------------------------------------------------------------
  // 19. Decision 5: lo que la v1 no ejecuta cae al legado con su divergencia.
  // -------------------------------------------------------------------------
  {
    const expectFallback = async (label, reason) => {
      const lead = await cartFor(label);
      const row = await admit(lead);
      const div = await divergenceOf('ruta', reason);
      if (row.outcome !== 'legado_por_divergencia' || row.created !== true || div === null || row.divergence_id !== div.id
          || div.last_source_ref !== lead.event || await runOf(row.recovery_case_id)
          || (await caseOf(lead)).policy_key !== world.THREE_TOUCHES.policy) {
        fail(`${label}: ${reason}`, { row, div });
      }
      return div.count;
    };
    // (a) Un scope del legado con cohorte (en una transaccion que se deshace;
    // el scope publicado es inmutable, asi que se apaga su validacion).
    await db.exec('begin');
    let cohort;
    let cohortError = null;
    try {
      await db.exec('alter table public.pilot_scope_versions disable trigger pilot_scope_versions_validate');
      await db.query("update public.pilot_scope_versions set audience_mode = 'consented_intent_in_cohort' where scope_key = $1",
        [SCOPE]);
      await db.exec('alter table public.pilot_scope_versions enable trigger pilot_scope_versions_validate');
      const lead = await cartFor('con-cohorte');
      // La persona en la cohorte, para que el legado planifique.
      await db.query(`insert into public.pilot_cohort_memberships (scope_key, scope_version, contact_id, member_status,
          enrolled_by, enrollment_reason, last_runtime_generation)
        values ($1, 1, $2, 'active', 'dan', 'cohorte de prueba', $3)`, [SCOPE, lead.contact, await world.generationOf(SCOPE)]);
      const row = await admit(lead);
      cohort = { row, div: await divergenceOf('ruta', 'ruta:audiencia_con_cohorte'), run: await runOf(row.recovery_case_id) };
    } catch (error) {
      cohortError = error;
    } finally {
      await db.exec('rollback');
    }
    if (cohortError) throw cohortError;
    if (cohort.row.outcome !== 'legado_por_divergencia' || cohort.row.created !== true || cohort.div === null
        || cohort.row.divergence_id !== cohort.div.id || cohort.run !== null) {
      fail('(a) audiencia con cohorte', cohort);
    }
    // (b) La repeticion despues de un recorrido.
    await publishCart({ topes: { repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '1d' } } }, 'repeticion 1d');
    const repetition = await expectFallback('repeticion-1d', 'ruta:repeticion_no_soportada');
    await publishCart({}, 'de vuelta sin repeticion');
    // (c) Una bolsa distinta del scope del legado.
    await publishCart({ topes: { bolsa: B.scope } }, 'bolsa de B');
    const pool = await expectFallback('bolsa-de-b', 'ruta:bolsa_distinta_del_scope');
    await publishCart({}, 'de vuelta a la bolsa del scope');
    // Y con la definicion normal, otra vez el motor.
    await motorCart('despues-de-la-decision-5');
    results.decision_5 = { cohorte: cohort.row.outcome, repeticion: repetition, bolsa: pool };
  }

  // -------------------------------------------------------------------------
  // 20. La delegacion del envoltorio con una entrada que la admision no acepta:
  //     el legado decide como hoy, no un 22023 de la admision.
  // -------------------------------------------------------------------------
  {
    const lead = await cartFor('telefono-con-mas');
    lead.cartArgs[9] = `+${lead.phone}`;
    const outcomeOf = async (executor) => {
      await db.exec('begin');
      try {
        if (executor === 'legado') {
          const back = await route(A.funnel, 'carrito', FLOWS.carrito, 'legado');
          if (!back.ok) fail('de vuelta a legado', back);
        }
        let row = null;
        const error = await errOf(async () => { row = one((await db.query(PLAN_CART, lead.cartArgs)).rows, 'plan'); });
        return {
          error: error && { code: error.code, message: error.message, detail: error.detail ?? null },
          created: row?.created ?? null,
          run: row?.recovery_case_id ? (await runOf(row.recovery_case_id)) !== null : null,
        };
      } finally {
        await db.exec('rollback');
      }
    };
    const legacy = await outcomeOf('legado');
    const motor = await outcomeOf('motor');
    if (JSON.stringify(motor) !== JSON.stringify(legacy) || motor.error?.message === 'invalid_flow_engine_admission_parameters'
        || motor.run === true) {
      fail('la delegacion con un telefono que no es de digitos', { legacy, motor });
    }
    results.delegacion_fuera_de_forma = legacy.error ? legacy.error.message : 'planificado';
  }

  // -------------------------------------------------------------------------
  // 21. p_force_legacy sin ruta del carrito: legado, sin divergencia.
  // -------------------------------------------------------------------------
  {
    const lead = await cartFor('forzado-sin-ruta', { P: B });
    await db.exec('begin');
    let forced;
    try {
      // Las rutas son permanentes: se borra la de B con su guarda apagada.
      await db.exec('alter table public.flow_engine_routes disable trigger flow_engine_routes_permanent');
      await db.query("delete from public.flow_engine_routes where legacy_scope_key = $1 and legacy_kind = 'carrito'", [B.scope]);
      await db.exec('alter table public.flow_engine_routes enable trigger flow_engine_routes_permanent');
      const row = await admit(lead, true);
      forced = { row, divergences: Number((await sql1(
        'select count(*)::int as n from public.flow_engine_divergences where last_source_ref = $1', [lead.event], 'div')).n),
      run: row.recovery_case_id ? await runOf(row.recovery_case_id) : 'sin caso' };
    } finally {
      await db.exec('rollback');
    }
    if (forced.row.outcome !== 'legado' || forced.row.divergence_id !== null || forced.row.divergence_first !== null
        || forced.row.created !== true || forced.divergences !== 0 || forced.run !== null) {
      fail('p_force_legacy sin ruta', forced);
    }
    results.forzado_sin_ruta = forced.row.outcome;
  }

  // -------------------------------------------------------------------------
  // 22. La re-entrada: con el caso del motor cerrado (otra_despues_de 0m), un
  //     carrito nuevo de la misma persona y oferta arranca otro recorrido.
  // -------------------------------------------------------------------------
  {
    // viaAdmit cerro por policy_exhausted en la seccion 3.
    const closed = viaAdmit.plan.recovery_case_id;
    if ((await caseOf(viaAdmit)).status !== 'sequence_exhausted') fail('la re-entrada: el caso no esta cerrado');
    await cartFor(viaAdmit);
    const row = await admit(viaAdmit);
    const run = row.recovery_case_id ? await runOf(row.recovery_case_id) : null;
    if (row.outcome !== 'motor_arranco' || row.created !== true || row.recovery_case_id === closed
        || run?.flow_id !== FLOWS.carrito || run.trigger_source_ref !== viaAdmit.event) {
      fail('la re-entrada', { row, run });
    }
    results.re_entrada = row.outcome;
  }

  console.log(JSON.stringify(results));
};

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    await main();
  } catch (error) {
    console.error(error.stack?.split('\n').slice(0, 3).join('\n'),
      JSON.stringify({ code: error.code, detail: error.detail, where: error.where, query: error.query?.slice(0, 200) }));
    process.exit(1);
  }
}
