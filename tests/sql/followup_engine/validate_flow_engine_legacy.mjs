// La gemela del horario y los casos de hoy, generados con la cadena real (la
// tajada 1 del motor de flujos: diseno del 2026-10-16, §3.2 y §5).
//
// La cadena de supabase/migrations/ se aplica UNA vez, con el cargador de
// validate_flow_store.mjs, y sobre esa base se hacen dos cosas:
//
//   1. La gemela del horario. Los casos de tests/fixtures/flow_engine/
//      window_cases.json contra reevaluate_followup_action real (la version
//      vigente, 20260805000300_per_case_conversation_anchor.sql; la ventana
//      en :219-254). La misma tabla la corre pytest contra
//      bridge.flow_schedule (tests/test_flow_schedule.py). Por caso: su
//      politica (_formato.policy_base con su timezone y su business_windows),
//      un carrito admitido por la admision portable, el contacto y sus puntos
//      como los deja resolve_event, el plan del planificador base
//      (plan_cart_recovery_with_identity: el caso con su identidad, la
//      secuencia y la accion first_contact_review, sin el scope), un permiso
//      allowed de marzo de 2026 y la toma con p_now = at, que deja el lease
//      vigente en at. Se compara la decision, el motivo y, con defer, el due_at
//      que queda en scheduled_actions (next_open). Los permisos del plan nacen
//      con el reloj, y hay casos de marzo: por eso el permiso de marzo se
//      siembra antes del plan (y el plan ya no siembra otro). La reevaluacion
//      es pura en p_now: no la compara con el reloj.
//
//   2. Los 24 casos de hoy (G1 a G24, la tabla de §3.2). Lo que hace hoy cada
//      caso no lo escribe nadie: lo genera esta cadena y queda en
//      tests/fixtures/flow_engine/legacy_recovery_cases.json. Con --check, la
//      corrida tiene que dar exactamente ese archivo; con --write, lo
//      reescribe. Cada caso:
//      - tiene su scope y su politica, para que los topes y las ventanas de un
//        caso no toquen a otro. El scope copia la v5 activa de
//        att1-recuperacion y la politica copia att1-recuperacion-un-toque v1,
//        las dos de la captura de ATT1 (att1_recovery_capture_20261016.json),
//        con la zona, la ventana y los topes del caso;
//      - corre con el reloj real. La autorizacion exige p_now a +-5 minutos
//        del reloj (20260930000300:459-465, en la base aplicada
//        authorize_lancemos_pilot_request_start), y el permiso del plan nace
//        con el reloj (plan_cart_recovery_with_identity: valid_from =
//        clock_timestamp()), asi que la reevaluacion tiene que ir despues del
//        plan en el reloj real. T0, el instante de la primera toma, es el
//        minuto en curso al empezar el caso mas 2 minutos; los disparos van
//        hacia atras (el primero en T0 - 10 minutos) y quedan en el pasado
//        del reloj. Lo que sucede despues de un envio se pone en T0 + 60 s;
//      - en los reintentos (G18, G21 y G23) el lease es de 1 minuto y no de 5:
//        dos leases de 5 minutos no caben en la ventana de +-5 minutos de la
//        autorizacion. El lease no cambia ninguna decision de la base, solo
//        cuando se puede volver a tomar la accion; el de 5 minutos lo fija D5
//        en pytest (worker.py:705);
//      - usa una zona de offset fijo (Etc/GMT+-N) elegida para que la hora
//        local de T0 caiga entre las 11:30 y las 12:30, y la ventana se arma
//        alrededor de esa hora: abierta, de -3 h a +3 h; cerrada, de +4 h a
//        +6 h. Ninguna cruza la medianoche, corra la CI a la hora que corra
//        (zoneOf() se prueba para los 1440 minutos del dia);
//      - guarda cada hora como segundos desde el disparo, y cada dia local
//        como "corrida" o "anterior". Lo que sale del reloj (request_started_at,
//        authorized_at, el least(due_at, now()) del pago fallido repetido) se
//        guarda como "reloj". Asi --check no depende de la fecha ni de la hora.
//
//      Los disparos entran como hoy, por las funciones de la cadena que usa
//      el bridge:
//      - el formulario, por admit_portable_observed_lead_precheckout (la
//        admision sin el primer contacto: ese flujo no es de la tajada 1), con
//        el golden del traductor de GHL de la landing d (un movil mexicano en
//        52 + 10 digitos), como validate_att1_portable_chain.mjs;
//      - el carrito, el pago fallido y la compra, por las admisiones
//        portables, con las capturas de tests/fixtures/
//        (hotmart_cart_abandonment_rejected_v1.json,
//        hotmart_purchase_canceled_att1_20261012.json y
//        hotmart_cash_payment_approved_att1_20261012.json), con id, fechas,
//        comprador, transaccion y oferta sustituidos por datos sinteticos;
//      - lo que hace el ResolutionWorker con cada evento: resolve_event
//        (src/bridge/resolution.py:153-377, el contacto y sus puntos como los
//        escribe el SupabaseClient) y el plan del piloto, con
//        update_event_status (supabase.py:5185-5210) y el motivo como lo
//        arma _pilot_plan_rejection (supabase.py:194-216); y, para la compra,
//        apply_hotmart_purchase_approved (worker.py:2358-2392);
//      - lo que hace el DurableDispatcher en modo directo (worker.py:1120-1600):
//        la toma con lease de 5 minutos, la reevaluacion, la reserva en
//        approved_template, el contexto, la segunda reevaluacion y el arranque
//        por el ancla (supabase.py:6506-6525). La aceptacion es la de
//        record_and_finalize_followup_acceptance, con ids sinteticos;
//      - la baja, por apply_chatwoot_inbound_opt_out, con el id que resuelve
//        el bridge (app.py:4440-4481: la identidad guardada si la hay; si no,
//        el wa_id textual);
//      - el operador, por set_lancemos_pilot_runtime_state y
//        activate_lancemos_pilot_scope_version. Lo que el operador inserta a
//        mano hoy (el binding, el mapeo de la oferta, la politica de compra, la
//        politica, el scope y el control inactive) se inserta igual.
//      La unica fila del motor que se siembra es la autorizacion de ayer de
//      G5, sobre la accion y el intento reservado de una persona que llego por
//      la cadena: ningun camino autoriza con otra fecha que la del reloj
//      (authorize_lancemos_pilot_request_start toma clock_timestamp()), y la
//      tabla es append-only. Es el precedente de validate_pilot_boundary.mjs.
//
//      Una sola base: los casos corren uno detras de otro, y cada uno guarda
//      lo suyo al terminar. La toma es global (claim_due_followup_actions), asi
//      que la de un caso tambien toma o barre lo que quedo abierto de los
//      anteriores; nada de eso cambia lo que ya se guardo. G17 corre ultimo:
//      su conflicto semantico de compra queda sin resolver y frena todo
//      arranque de la base (20260808000500:302-335).
//
// Lo que no se genera aca es lo del despachador en Python (el nombre
// rechazado, el lease, el reintento a 1 minuto, el incierto): va en pytest
// (tests/test_flow_engine_dispatcher_legacy.py). G14 llama al cierre de la
// base con los parametros que pasa worker.py:1054-1070 y :878-920.
//
// Uso, desde tests/sql/followup_engine/:
//   node validate_flow_engine_legacy.mjs --check   compara con el archivo
//   node validate_flow_engine_legacy.mjs --write   lo reescribe

import { PGlite } from '@electric-sql/pglite';
import { randomBytes } from 'node:crypto';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const MODES = { '--check': 'check', '--write': 'write' };
const args = process.argv.slice(2);
const MODE = args.length === 1 ? MODES[args[0]] : undefined;
if (MODE === undefined) {
  console.error('uso: node validate_flow_engine_legacy.mjs --check | --write');
  process.exit(2);
}

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const FLOW_FIXTURES = join(root, 'tests/fixtures/flow_engine');
const LEGACY_FILE = join(FLOW_FIXTURES, 'legacy_recovery_cases.json');
const BASELINE = join(root, 'supabase/baseline/20260803_public_schema.sql');
const MIGRATIONS = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const readJson = (path) => JSON.parse(readFileSync(path, 'utf8'));

// ---------------------------------------------------------------------------
// La cadena, una vez, con el cargador de validate_flow_store.mjs.
// ---------------------------------------------------------------------------
const SUPABASE_ROLES = `
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin bypassrls;
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
  alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
  alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
`;
const applyFile = async (target, path) => {
  await target.exec(readFileSync(path, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
};
const timings = {};
const tick = (label, since) => { timings[label] = Date.now() - since; };
const startedAt = Date.now();
const db = new PGlite();
await db.waitReady;
await db.exec(SUPABASE_ROLES);
await applyFile(db, BASELINE);
for (const name of MIGRATIONS) {
  await applyFile(db, join(root, 'supabase/migrations', name));
}
tick('cadena', startedAt);

const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: se esperaba una fila y hubo ${rows.length}`);
  return rows[0];
};
const query = async (sql, params = []) => (await db.query(sql, params)).rows;
const dbNow = async () => new Date(one(await query('select clock_timestamp() as now'), 'reloj').now);

// ---------------------------------------------------------------------------
// El dato real: el manifiesto de ATT1, la copia de aprovisionar-att1.sql y la
// configuracion de la captura.
// ---------------------------------------------------------------------------
// El lector minimo de TOML de validate_att1_portable_chain.mjs.
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
    throw new Error(`falta ${label}`);
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
const PILOT = readJson(join(root, 'tests/fixtures/instances/att1/politica-piloto.json'));
const CHANNEL_PROVIDER = required(PILOT.pilot_scope?.channel_provider, 'pilot_scope.channel_provider');
const CHANNEL_REF = `${required(PILOT.pilot_scope?.channel_account_ref_prefix, 'pilot_scope.channel_account_ref_prefix')}${ATT1.inboxId}`;

// La v5 de att1-recuperacion (la del control armado de la captura) y su
// politica, tal como estaban en att1-db el 2026-10-16.
const CAPTURE = readJson(join(FLOW_FIXTURES, 'att1_recovery_capture_20261016.json'));
const capturedControl = one(CAPTURE.config.pilot_runtime_controls
  .filter((control) => control.scope_key === 'att1-recuperacion'), 'control de att1-recuperacion');
const ATT1_SCOPE = one(CAPTURE.config.pilot_scope_versions.filter((scope) => (
  scope.scope_key === capturedControl.scope_key && scope.version === capturedControl.scope_version
)), 'scope activo de att1-recuperacion');
const ATT1_POLICY = one(CAPTURE.config.followup_policy_versions.filter((policy) => (
  policy.policy_key === ATT1_SCOPE.policy_key && policy.version === ATT1_SCOPE.policy_version
)), 'politica del scope activo');
if (ATT1_SCOPE.audience_mode !== 'consented_intent'
    || ATT1_SCOPE.external_product_id !== String(ATT1.productId)
    || ATT1_SCOPE.offer_code !== defaultOffer.offer_code
    || ATT1_POLICY.steps.map((step) => step.step_key).join(',') !== 'first_contact,payment_failure_first_contact'
    || ATT1_POLICY.max_automatic_messages !== 1) {
  // Los casos de abajo asumen la audiencia de produccion y la politica
  // compartida de un toque. Si la captura cambia, hay que revisarlos.
  throw new Error(`la configuracion de ATT1 de la captura cambio de forma: ${JSON.stringify({
    audience: ATT1_SCOPE.audience_mode, steps: ATT1_POLICY.steps.length,
  })}`);
}

// Los disparos capturados.
const CAPTURED_CART = readJson(join(root, 'tests/fixtures/hotmart_cart_abandonment_rejected_v1.json')).payload;
const CAPTURED_FAILURES = readJson(join(root, 'tests/fixtures/hotmart_purchase_canceled_att1_20261012.json'))
  .eventos.map((event) => event.payload);
const CAPTURED_PURCHASES = readJson(join(root, 'tests/fixtures/hotmart_cash_payment_approved_att1_20261012.json'))
  .eventos.map((event) => event.payload);
// El formulario de la landing d: oferta 2uafw5bg, movil mexicano en 52 + 10.
const GOLDEN = readJson(join(root,
  'tests/fixtures/ghl/expected/ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json'));
const OFFER = defaultOffer.offer_code === GOLDEN.canonical_payload?.commerce?.offer_ref
  ? defaultOffer
  : additionalOffers.find((offer) => offer.offer_code === GOLDEN.canonical_payload?.commerce?.offer_ref);
const CAPTURED_FAILURE = CAPTURED_FAILURES.find((payload) => (
  payload.data.purchase.offer.code === OFFER?.offer_code
  && payload.data.purchase.checkout_country.iso === 'MX'
));
const CAPTURED_PURCHASE = CAPTURED_PURCHASES.find((payload) => (
  payload.data.purchase.offer.code === OFFER?.offer_code
));
if (OFFER === undefined
    || GOLDEN.raw_payload.version !== '1.1.0'
    || GOLDEN.raw_payload.data.buyer.phone_country_code !== '52'
    || GOLDEN.canonical_payload.consent.copy_version !== ATT1.copyVersion
    || GOLDEN.canonical_payload.consent.whatsapp_contact !== true
    || CAPTURED_FAILURE === undefined
    || CAPTURED_PURCHASE === undefined
    || CAPTURED_CART.event !== 'PURCHASE_OUT_OF_SHOPPING_CART') {
  throw new Error('las capturas o el golden del formulario no son los que este script espera');
}

// ---------------------------------------------------------------------------
// El mundo comun: lo que la instancia de ATT1 publica una vez.
// ---------------------------------------------------------------------------
const landingOf = (offer) => ({
  offer_code: offer.offer_code, site: offer.site, landing_id: offer.landing_id,
  page_host: offer.page_host, page_path: offer.page_path,
});
await query(`
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
  await query(`
    insert into public.hotmart_purchase_intent_scopes
      (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
       offer_ref, max_lookback, active)
    values ($1,$2,$3,$4,$5,$6::interval,true)
  `, [ATT1.tenant, ATT1.funnel, String(ATT1.productId), ATT1.hotlink, offer.offer_code,
    required(PILOT.purchase_intent_scope?.max_lookback, 'purchase_intent_scope.max_lookback')]);
}
await query(`
  insert into public.commercial_ally_hotmart_purchase_policies
    (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
  values ($1,$2,$3,true,$4::interval)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion,
  required(PILOT.purchase_policy?.max_lookback, 'purchase_policy.max_lookback')]);

// ---------------------------------------------------------------------------
// 1. La gemela del horario.
// ---------------------------------------------------------------------------
const gemelaStarted = Date.now();
const WINDOW = readJson(join(FLOW_FIXTURES, 'window_cases.json'));
const gemelaFailures = [];
const MARCH = '2026-03-01T00:00:00Z';
let gemelaIndex = 0;
for (const windowCase of WINDOW.cases) {
  gemelaIndex += 1;
  const n = String(gemelaIndex).padStart(2, '0');
  const policyKey = `gemela-${windowCase.id}`.replaceAll('_', '-');
  const base = WINDOW._formato.policy_base;
  await query(`
    insert into public.followup_policy_versions
      (policy_key, version, status, purpose, timezone, business_windows,
       grace_period, expires_after, max_automatic_messages, steps,
       approved_by, approved_at, published_at)
    values ($1,1,$2,$3,$4,$5::jsonb,$6::interval,$7::interval,$8,$9::jsonb,
            'operator-test',now(),now())
  `, [policyKey, base.status, base.purpose, windowCase.policy.timezone,
    JSON.stringify(windowCase.policy.business_windows), base.grace_period,
    base.expires_after, base.max_automatic_messages, JSON.stringify(base.steps)]);
  const expiresAt = new Date(windowCase.expires_at);
  const abandonedAt = new Date(expiresAt.getTime() - 24 * 3_600_000);
  const phone = `525559${n}0000`;
  const email = `gemela-${n}@example.test`;
  const payload = structuredClone(CAPTURED_CART);
  payload.id = `gemela-${windowCase.id}`;
  payload.creation_date = abandonedAt.getTime();
  payload.data.product = { id: ATT1.productId, name: ATT1.productName };
  payload.data.offer = { code: OFFER.offer_code };
  payload.data.buyer = { name: `Gemela ${n}`, email, phone };
  const admitted = one(await query(`
    select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload),
    email, phone]), `gemela ${windowCase.id} admision`);
  const contact = one(await query(`
    insert into public.contacts (full_name, email, phone, country_iso, lifecycle_status, contact_permission)
    values ($1,$2,$3,'MX','lead','unknown') returning id
  `, [`Gemela ${n}`, email, phone]), 'gemela contacto').id;
  await query(`
    insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id)
    values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)
  `, [contact, email, phone, admitted.webhook_event_id]);
  await query(`
    insert into public.contact_authorizations
      (contact_id, channel, purpose, authorization_status, authorization_source, evidence, valid_from)
    values ($1,'whatsapp','cart_recovery','allowed','hotmart',
            jsonb_build_object('reason','cart_abandonment','webhook_event_id',$2::text),$3)
  `, [contact, admitted.webhook_event_id, MARCH]);
  const plan = one(await query(`
    select * from public.plan_cart_recovery_with_identity($1,$2,$3,$4,$5,$6,1,$7,$8,$9,$10)
  `, [admitted.webhook_event_id, contact, String(ATT1.productId), ATT1.productName,
    OFFER.offer_code, policyKey, abandonedAt.toISOString(), ATT1.accountId, ATT1.inboxId,
    phone]), `gemela ${windowCase.id} plan`);
  const worker = `gemela-${n}`;
  const claimed = (await query(`
    select id, lease_generation from public.claim_due_followup_actions($1,$2::timestamptz,interval '5 minutes',100)
  `, [worker, windowCase.at])).find((row) => row.id === plan.scheduled_action_id);
  if (claimed === undefined) throw new Error(`gemela ${windowCase.id}: la toma en at no devolvio la accion`);
  const decision = one(await query(`
    select decision, reason_code from public.reevaluate_followup_action($1,$2,$3,$4::timestamptz,false)
  `, [plan.scheduled_action_id, worker, claimed.lease_generation, windowCase.at]),
  `gemela ${windowCase.id} reevaluacion`);
  const after = one(await query(`
    select status, due_at from public.scheduled_actions where id = $1
  `, [plan.scheduled_action_id]), 'gemela accion');
  // Con defer, el due_at que queda; con execute o escalate, null: la
  // reevaluacion no lo reescribe, y se mira que siga siendo el del plan.
  const rewritten = after.due_at.getTime() !== abandonedAt.getTime();
  const dueAt = decision.decision === 'defer' || rewritten
    ? after.due_at.toISOString().replace('.000Z', 'Z')
    : null;
  const expected = windowCase.reevaluation;
  const got = { decision: decision.decision, reason_code: decision.reason_code, due_at: dueAt };
  if (got.decision !== expected.decision || got.reason_code !== expected.reason_code
      || got.due_at !== expected.due_at
      || (expected.decision === 'defer' && after.status !== 'deferred')) {
    gemelaFailures.push(`${windowCase.id}: se esperaba ${JSON.stringify(expected)} y la base dio ${JSON.stringify(got)}`);
  }
}
tick('gemela', gemelaStarted);
for (const failure of gemelaFailures) console.log(`FAIL gemela ${failure}`);

// ---------------------------------------------------------------------------
// 2. Los casos de hoy.
// ---------------------------------------------------------------------------
// La zona de offset fijo para que la hora local de un instante caiga entre las
// 11:30 y las 12:30. Etc/GMT tiene el signo al reves: UTC-5 es Etc/GMT+5.
const zoneOf = (instant) => {
  const utcMinutes = instant.getUTCHours() * 60 + instant.getUTCMinutes();
  const offsetHours = Math.max(-12, Math.min(14, Math.round((720 - utcMinutes) / 60)));
  return {
    name: offsetHours === 0 ? 'Etc/GMT' : `Etc/GMT${offsetHours > 0 ? '-' : '+'}${Math.abs(offsetHours)}`,
    offsetHours,
    localMinute: (utcMinutes + offsetHours * 60 + 1440) % 1440,
  };
};
const WINDOWS = {
  abierta: { from: -180, to: 180, texto: 'abierta: de -3 h a +3 h de la hora local de T0' },
  cerrada: { from: 240, to: 360, texto: 'cerrada: de +4 h a +6 h de la hora local de T0' },
};
// zoneOf para los 1440 minutos del dia: la hora local entre 11:30 y 12:30, la
// zona dentro de las que existen, y las dos ventanas dentro del mismo dia.
for (let minute = 0; minute < 1440; minute += 1) {
  const probe = zoneOf(new Date(Date.UTC(2026, 9, 16, 0, minute)));
  const windows = Object.values(WINDOWS)
    .map((window) => [probe.localMinute + window.from, probe.localMinute + window.to]);
  if (probe.localMinute < 690 || probe.localMinute > 750
      || probe.offsetHours < -12 || probe.offsetHours > 14
      || windows.some(([from, to]) => from < 0 || to > 1439)) {
    throw new Error(`zoneOf no deja la ventana dentro del dia a las ${minute} UTC: ${JSON.stringify(probe)}`);
  }
}
const hhmm = (minute) => `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
const localDate = (instant, offsetHours) => new Date(instant.getTime() + offsetHours * 3_600_000)
  .toISOString().slice(0, 10);

// ULID como el del adaptador de GHL: 48 bits de milisegundos y 80 aleatorios.
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

const WORKER_LEASE = '5 minutes';
const RETRY_LEASE = '1 minute';
const DIRECT_DELIVERY_MODE = 'approved_template';
const START_OPERATION_BY_ANCHOR = {
  cart_abandonment: 'mark_lancemos_pilot_request_started',
  payment_failure: 'mark_portable_payment_failure_request_started',
};
// El motivo con que el ResolutionWorker deja fallido un evento cuyo plan se
// rechazo: _pilot_plan_rejection (supabase.py:194-216) copia el mensaje y el
// detalle de un 55000 con forma de codigo; cualquier otro error del plan es
// create_recovery_case_failed (resolution.py:371-377).
const PLAN_REJECTION_TOKEN = /^[a-z][a-z_]{0,79}$/;
const planRejection = (error) => {
  if (error?.code !== '55000' || !PLAN_REJECTION_TOKEN.test(error?.message ?? '')) {
    return 'create_recovery_case_failed';
  }
  if (error.detail === undefined || error.detail === null) return error.message;
  return PLAN_REJECTION_TOKEN.test(error.detail) ? `${error.message}:${error.detail}` : 'create_recovery_case_failed';
};

// Un caso: su scope, su politica, su reloj y lo que va guardando.
const newCase = async (spec, number) => {
  const slug = `flujo-legado-${spec.id.toLowerCase()}`;
  // T0: entre 1 y 2 minutos despues del reloj, en un minuto entero. Los planes
  // del caso corren antes (su permiso nace con el reloj) y los arranques,
  // en T0 o en T0 + 61 s, quedan a menos de 5 minutos del reloj.
  const runStarted = await dbNow();
  const T0 = new Date(Math.floor(runStarted.getTime() / 60_000) * 60_000 + 120_000);
  const zone = zoneOf(T0);
  const window = WINDOWS[spec.ventana];
  const expiresAfter = spec.vence ?? ATT1_POLICY.expires_after;
  const disparo = new Date(T0.getTime() - 600_000);
  const ctx = {
    spec, slug, number, T0, zone, disparo,
    worker: `${slug}-worker`,
    scopeVersion: 1,
    bridgeVersion: 1,
    pasos: [],
    labels: { e: new Map(), c: new Map(), a: new Map() },
    persons: new Map(),
    runStarted,
  };
  // Los instantes del caso, en segundos desde T0. T2 es el segundo lease de
  // un reintento: el primero, de RETRY_LEASE, se toma en T0 y vence en T0 + 60.
  ctx.at = (seconds) => new Date(T0.getTime() + seconds * 1000);
  ctx.T2 = ctx.at(61);
  ctx.rel = (instant) => {
    const ms = new Date(instant).getTime() - disparo.getTime();
    if (ms % 1000 !== 0) throw new Error(`${spec.id}: una hora que no es del caso: ${new Date(instant).toISOString()}`);
    return ms / 1000;
  };
  ctx.step = (entry) => ctx.pasos.push(entry);
  ctx.label = (kind, id) => {
    if (id === null || id === undefined) return null;
    const map = ctx.labels[kind];
    if (!map.has(id)) map.set(id, `${kind}${map.size + 1}`);
    return map.get(id);
  };
  ctx.localDay = (date) => {
    const run = localDate(T0, zone.offsetHours);
    const iso = typeof date === 'string' ? date : new Date(date).toISOString().slice(0, 10);
    if (iso === run) return 'corrida';
    if (iso === localDate(new Date(T0.getTime() - 86_400_000), zone.offsetHours)) return 'anterior';
    return iso;
  };
  ctx.policy = {
    timezone: zone.name,
    business_windows: [{
      days: [1, 2, 3, 4, 5, 6, 7],
      start: hhmm(zone.localMinute + window.from),
      end: hhmm(zone.localMinute + window.to),
    }],
  };
  await query(`
    insert into public.followup_policy_versions
      (policy_key, version, status, purpose, timezone, business_windows,
       grace_period, expires_after, max_automatic_messages, steps,
       approved_by, approved_at, published_at)
    values ($1,1,'published',$2,$3,$4::jsonb,$5::interval,$6::interval,$7,$8::jsonb,
            'operator-test',now(),now())
  `, [slug, ATT1_POLICY.purpose, zone.name, JSON.stringify(ctx.policy.business_windows),
    ATT1_POLICY.grace_period, expiresAfter, ATT1_POLICY.max_automatic_messages,
    JSON.stringify(ATT1_POLICY.steps)]);
  ctx.caps = {
    tope_diario: spec.tope_diario ?? ATT1_SCOPE.max_outbound_request_starts_per_day,
    tope_total: spec.tope_total ?? ATT1_SCOPE.max_outbound_request_starts_total,
  };
  await publishScope(ctx, 1);
  await query(`
    insert into public.pilot_runtime_controls
      (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
    values ($1,1,'inactive',0,'operator-test','default-off')
  `, [slug]);
  await operator(ctx, 'arma', 1, { record: false });
  ctx.config = {
    zona: 'Etc/GMT+-N, con la hora local de T0 entre las 11:30 y las 12:30',
    ventana: window.texto,
    vence: expiresAfter,
    gracia: ATT1_POLICY.grace_period,
    pasos_de_la_politica: ATT1_POLICY.steps.map((step) => step.step_key),
    audiencia: ATT1_SCOPE.audience_mode,
    tope_diario: ctx.caps.tope_diario,
    tope_total: ctx.caps.tope_total,
    disparo: 'T0 - 600 s',
  };
  return ctx;
};
const publishScope = async (ctx, version) => {
  await query(`
    insert into public.pilot_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
       channel, channel_provider, channel_account_ref, source, source_event_type,
       additional_source_event_types, external_product_id, offer_code,
       additional_offer_codes, purpose, policy_key, policy_version, timezone,
       max_cohort_contacts, max_outbound_request_starts_total,
       max_outbound_request_starts_per_day, audience_mode,
       approved_by, approved_at, published_at)
    values ($1,$2,'published',$3,$4,$5,'whatsapp',$6,$7,$8,$9,$10::text[],$11,$12,
            $13::text[],$14,$15,1,$16,$17,$18,$19,$20,'operator-test',now(),now())
  `, [ctx.slug, version, ATT1_SCOPE.tenant_key, ATT1.accountId, ATT1.inboxId,
    CHANNEL_PROVIDER, CHANNEL_REF, ATT1_SCOPE.source, ATT1_SCOPE.source_event_type,
    ATT1_SCOPE.additional_source_event_types, ATT1_SCOPE.external_product_id,
    ATT1_SCOPE.offer_code, ATT1_SCOPE.additional_offer_codes, ATT1_SCOPE.purpose,
    ctx.slug, ctx.zone.name,
    required(PILOT.pilot_scope?.max_cohort_contacts, 'pilot_scope.max_cohort_contacts'),
    ctx.caps.tope_total, ctx.caps.tope_diario, ATT1_SCOPE.audience_mode]);
};
// El operador: armar, pausar, publicar y activar otra version (el
// procedimiento de docs/design/lancemos-pilot-boundary.md, seccion 2.4, que
// sigue validate_att1_portable_chain.mjs).
const operator = async (ctx, action, version, { record = true } = {}) => {
  const { generation } = one(await query(`
    select generation from public.pilot_runtime_controls where scope_key = $1
  `, [ctx.slug]), 'generacion del control');
  let state;
  if (action === 'arma' || action === 'pausa') {
    state = one(await query(`
      select * from public.set_lancemos_pilot_runtime_state($1,$2,$3,$4,'operator-test','flujo-legado')
    `, [ctx.slug, version, generation, action === 'arma' ? 'armed' : 'paused']), `${ctx.spec.id} ${action}`).runtime_state;
  } else if (action === 'publica') {
    await publishScope(ctx, version);
    state = 'published';
  } else if (action === 'activa') {
    state = one(await query(`
      select * from public.activate_lancemos_pilot_scope_version($1,$2,$3,'operator-test','flujo-legado')
    `, [ctx.slug, version, generation]), `${ctx.spec.id} activa`).runtime_state;
    ctx.scopeVersion = version;
  } else {
    throw new Error(`operador: ${action}`);
  }
  if (record) ctx.step({ paso: 'operador', que: action, version, estado: state });
  return state;
};

// Las personas del caso, con datos sinteticos: un movil mexicano en la forma
// de 52 + 10 digitos (la del formulario y la de checkout_phone de Hotmart en
// ATT1) y un email de example.test.
const personOf = (ctx, letter) => {
  if (!ctx.persons.has(letter)) {
    const index = String(letter.charCodeAt(0) - 64).padStart(2, '0');
    const n = String(ctx.number).padStart(2, '0');
    ctx.persons.set(letter, {
      letter,
      name: `Compradora ${ctx.spec.id} ${letter}`,
      email: `${ctx.slug}-${letter.toLowerCase()}@example.test`,
      phone: `525550${n}${index}00`,
      contact: null,
      intent: null,
    });
  }
  return ctx.persons.get(letter);
};
// La otra forma del mismo movil: el wa_id que reporta WhatsApp (521 + 10).
const waIdOf = (phone) => `521${phone.slice(2)}`;
// Un telefono que no es de nadie del caso.
const otherPhoneOf = (ctx, letter) => `525550${String(ctx.number).padStart(2, '0')}${String(letter.charCodeAt(0) - 64 + 50).padStart(2, '0')}00`;

// El formulario: el golden de GHL con id, fecha y comprador sustituidos.
const admitForm = async (ctx, lead, submittedAt, { phone = lead.phone } = {}) => {
  const raw = structuredClone(GOLDEN.raw_payload);
  const canonical = structuredClone(GOLDEN.canonical_payload);
  const id = ulidAt(submittedAt.getTime());
  const dedupeKey = `${raw.source.site}:${raw.data.offer.code}:${lead.email}`;
  raw.id = id;
  raw.created_at = submittedAt.toISOString();
  raw.data.buyer.name = lead.name;
  raw.data.buyer.email = lead.email;
  raw.data.buyer.phone = `+${phone}`;
  raw.data.buyer.phone_country_code = '52';
  raw.data.buyer.phone_national = phone.slice(2);
  raw.dedupe_key = dedupeKey;
  canonical.external_submission_id = id;
  canonical.submitted_at = submittedAt.toISOString().replace('.000Z', 'Z');
  canonical.identity.email = lead.email;
  canonical.identity.phone = phone;
  canonical.lead.full_name = lead.name;
  canonical.dedupe_key = dedupeKey;
  const admitted = one(await query(`
    select * from public.admit_portable_observed_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, id, JSON.stringify(raw),
    JSON.stringify(canonical)]), `${ctx.spec.id} formulario de ${lead.letter}`);
  lead.intent ??= admitted.purchase_intent_id;
  ctx.step({
    paso: 'formulario', persona: lead.letter, t: ctx.rel(submittedAt),
    otro_telefono: phone !== lead.phone, admision: admitted.outcome,
    misma_intencion: admitted.purchase_intent_id === lead.intent,
  });
  return admitted;
};

// Los tres disparos de Hotmart, desde sus capturas.
let transactionSerial = 0;
const transactionOf = (ctx) => {
  transactionSerial += 1;
  return `HP${String(ctx.number).padStart(4, '0')}${String(transactionSerial).padStart(8, '0')}`;
};
const admitEvent = async (ctx, kind, lead, when, options = {}) => {
  const key = `${ctx.slug}-${kind}-${lead.letter.toLowerCase()}-${ctx.rel(when)}`;
  let payload;
  let operation;
  let email = lead.email;
  let phone = lead.phone;
  if (kind === 'carrito') {
    payload = structuredClone(CAPTURED_CART);
    payload.id = key;
    payload.creation_date = when.getTime();
    payload.data.product = { id: ATT1.productId, name: ATT1.productName };
    payload.data.offer = { code: OFFER.offer_code };
    payload.data.buyer = { name: lead.name, email, phone };
    operation = 'admit_portable_hotmart_cart_abandonment';
  } else if (kind === 'pago_fallido') {
    payload = structuredClone(CAPTURED_FAILURE);
    payload.id = key;
    payload.creation_date = when.getTime();
    Object.assign(payload.data.buyer, {
      name: lead.name, first_name: lead.name.split(' ')[0], last_name: lead.name.split(' ').slice(1).join(' '),
      email, checkout_phone: phone,
    });
    payload.data.purchase.transaction = transactionOf(ctx);
    payload.data.purchase.order_date = when.getTime() - 10_000;
    payload.data.purchase.offer.code = OFFER.offer_code;
    operation = 'admit_portable_hotmart_payment_failure';
  } else if (kind === 'compra') {
    email = options.email ?? lead.email;
    phone = options.phone ?? lead.phone;
    payload = structuredClone(CAPTURED_PURCHASE);
    payload.id = options.id ?? key;
    payload.creation_date = when.getTime() + 10_000;
    Object.assign(payload.data.buyer, {
      name: lead.name, first_name: lead.name.split(' ')[0], last_name: lead.name.split(' ').slice(1).join(' '),
      email, checkout_phone: phone,
    });
    payload.data.purchase.transaction = options.transaction ?? transactionOf(ctx);
    payload.data.purchase.approved_date = when.getTime();
    payload.data.purchase.order_date = when.getTime() - 60_000;
    payload.data.purchase.offer.code = OFFER.offer_code;
    operation = 'admit_portable_hotmart_purchase_approved';
  } else {
    throw new Error(`disparo: ${kind}`);
  }
  const admitted = one(await query(`
    select * from public.${operation}($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload),
    email.toLowerCase(), phone]), `${ctx.spec.id} ${kind} de ${lead.letter}`);
  const event = {
    id: admitted.webhook_event_id, kind, lead, when, payload, email, phone,
    label: ctx.label('e', admitted.webhook_event_id),
  };
  ctx.step({
    paso: 'disparo', evento: event.label, tipo: kind, persona: lead.letter,
    t: ctx.rel(when), admision: admitted.outcome,
  });
  return event;
};

// Lo que hace el ResolutionWorker con un carrito o un pago fallido: el
// contacto (resolve_event, pasos 2 a 4) y el plan del piloto (paso 5), con
// el estado del evento al final.
const resolveAndPlan = async (ctx, event) => {
  const { lead } = event;
  const failed = async (error) => {
    await query(`
      update public.webhook_events set processing_status = 'failed', processing_error = $2 where id = $1
    `, [event.id, error]);
    return error;
  };
  // Paso 2: el contacto por el email y por las formas equivalentes del movil
  // (find_contact_by_email y find_contact_by_phones; la base tiene la misma
  // equivalencia en _whatsapp_phone_variants).
  const byEmail = await query(`
    select contact_id from public.contact_points
    where type = 'email' and normalized_value = $1 limit 2
  `, [event.email.toLowerCase()]);
  const byPhone = await query(`
    select distinct contact_id from public.contact_points
    where type = 'phone' and normalized_value = any(public._whatsapp_phone_variants($1))
  `, [event.phone]);
  let motivo = null;
  let sqlError = null;
  if (byEmail.length > 1 || byPhone.length > 1
      || (byEmail.length === 1 && byPhone.length === 1 && byEmail[0].contact_id !== byPhone[0].contact_id)) {
    motivo = await failed('identity_ambiguous');
  }
  let contact = byEmail[0]?.contact_id ?? byPhone[0]?.contact_id ?? null;
  const matched = contact !== null;
  let plan = null;
  if (motivo === null) {
    // Paso 3 y 4: el contacto nuevo y sus puntos, como create_contact y
    // create_contact_point (resolution=ignore-duplicates).
    if (!matched) {
      // El pais es data.checkout_country.iso, que las capturas de ATT1 no
      // traen (el del pago fallido esta en data.purchase): queda nulo.
      contact = one(await query(`
        insert into public.contacts (full_name, email, phone, country_iso, lifecycle_status, contact_permission)
        values ($1,$2,$3,$4,'lead','unknown') returning id
      `, [lead.name, event.email.toLowerCase(), event.phone,
        event.payload.data.checkout_country?.iso ?? null]), 'contacto').id;
    }
    await query(`
      insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id)
      values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)
      on conflict do nothing
    `, [contact, event.email.toLowerCase(), event.phone, event.id]);
    lead.contact ??= contact;
    // _stored_whatsapp_identity (resolution.py:43-100): un contacto que ya
    // existia usa su identidad de la otra forma del movil, si tiene una sola.
    let externalUserId = event.phone;
    if (matched) {
      const own = await query(`
        select distinct external_user_id from public.channel_identities
        where contact_id = $1 and channel = 'whatsapp' and account_id = $2
          and identity_status = 'active' and metadata ->> 'inbox_id' = $3
          and external_user_id = any(public._whatsapp_phone_variants($4))
      `, [contact, `chatwoot:${ATT1.accountId}`, String(ATT1.inboxId), event.phone]);
      if (own.length === 1) externalUserId = own[0].external_user_id;
    }
    const planner = event.kind === 'pago_fallido'
      ? 'plan_portable_payment_failure_recovery'
      : 'plan_lancemos_pilot_cart_recovery';
    try {
      plan = one(await query(`
        select * from public.${planner}($1,$2,$3,$4,$5,$6,1,$7::timestamptz,$8,$9,$10,$11,$12)
      `, [event.id, contact, String(ATT1.productId), ATT1.productName, OFFER.offer_code,
        ctx.slug, event.when.toISOString(), ATT1.accountId, ATT1.inboxId, externalUserId,
        ctx.slug, ctx.bridgeVersion]), `${ctx.spec.id} plan de ${event.label}`);
      await query(`
        update public.webhook_events set processing_status = 'processed', processed_at = now() where id = $1
      `, [event.id]);
    } catch (error) {
      if (error?.message?.startsWith(`${ctx.spec.id} plan`)) throw error;
      motivo = await failed(planRejection(error));
      // Lo que contesto la base cuando el motivo es el generico del
      // adaptador: el bridge lo deja solo en el log.
      if (motivo === 'create_recovery_case_failed') sqlError = sqlErrorOf(error);
    }
  }
  const entry = {
    paso: 'plan', evento: event.label, arranco: plan !== null, motivo,
    contacto_existia: matched,
  };
  if (sqlError !== null) entry.error_de_la_base = `${sqlError.code} ${sqlError.message}`;
  if (plan !== null) {
    entry.caso = ctx.label('c', plan.recovery_case_id);
    entry.accion = ctx.label('a', plan.scheduled_action_id);
    entry.creado = plan.created;
  }
  ctx.step(entry);
  return plan;
};

// Lo que hace el ResolutionWorker con una compra (worker.py:2358-2392).
const applyPurchase = async (ctx, event) => {
  const result = one(await query(`
    select * from public.apply_hotmart_purchase_approved($1,$2,$3,$4,$5,$6,$7::timestamptz)
  `, [event.id, event.email.toLowerCase(), event.phone, String(ATT1.productId), OFFER.offer_code,
    event.payload.data.purchase.transaction, event.when.toISOString()]), `${ctx.spec.id} compra`);
  ctx.step({
    paso: 'compra_aplicada', evento: event.label, resultado: result.outcome,
    caso: ctx.label('c', result.recovery_case_id), por: result.matched_by,
  });
  return result;
};

// La baja que llega por Chatwoot.
let optOutSerial = 0;
const optOut = async (ctx, lead, when, externalUserId, forma) => {
  optOutSerial += 1;
  const result = one(await query(`
    select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,$6::timestamptz,'unsubscribe')
  `, [ATT1.accountId, ATT1.inboxId, 880000 + optOutSerial, 9880000 + optOutSerial,
    externalUserId, when.toISOString()]), `${ctx.spec.id} baja`);
  ctx.step({
    paso: 'baja', persona: lead.letter, t: ctx.rel(when), forma, resultado: result.outcome,
    acciones_tocadas: result.affected_actions,
  });
  return result;
};

const sqlErrorOf = (error) => ({
  code: error?.code ?? null, message: error?.message ?? null, detail: error?.detail ?? null,
});

// Un lease del despachador sobre la accion `actionId`, todo en el instante
// simulado `now` (la toma, las dos reevaluaciones, la reserva y el arranque).
// Devuelve lo que paso; el detalle queda en los pasos del caso.
let acceptanceSerial = 0;
const lease = async (ctx, actionId, now, {
  accept = false, closeBeforeRequest = null, duration = WORKER_LEASE,
} = {}) => {
  const label = ctx.label('a', actionId);
  const t = ctx.rel(now);
  const claimed = (await query(`
    select id, lease_generation, anchor_type
    from public.claim_due_followup_actions($1,$2::timestamptz,$3::interval,100)
  `, [ctx.worker, now.toISOString(), duration])).find((row) => row.id === actionId);
  ctx.step({
    paso: 'toma', accion: label, t, tomada: claimed !== undefined,
    ...(duration === WORKER_LEASE ? {} : { lease: duration }),
  });
  if (claimed === undefined) return { claimed: false };
  const generation = claimed.lease_generation;
  const reevaluate = async () => {
    const decision = one(await query(`
      select * from public.reevaluate_followup_action($1,$2,$3,$4::timestamptz)
    `, [actionId, ctx.worker, generation, now.toISOString()]), `${ctx.spec.id} reevaluacion`);
    ctx.step({
      paso: 'reevaluacion', accion: label, t, decision: decision.decision, motivo: decision.reason_code,
    });
    return decision;
  };
  const decision = await reevaluate();
  if (decision.decision !== 'execute') return { claimed: true, decision };
  const attempt = one(await query(`
    select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp',$6,$7::timestamptz)
  `, [actionId, ctx.worker, generation, decision.case_version, decision.sequence_revision,
    DIRECT_DELIVERY_MODE, now.toISOString()]), `${ctx.spec.id} reserva`);
  ctx.step({ paso: 'reserva', accion: label, t, intento: attempt.attempt_number });
  one(await query(`
    select * from public.get_followup_execution_context($1,$2,$3,$4::timestamptz)
  `, [actionId, ctx.worker, generation, now.toISOString()]), `${ctx.spec.id} contexto`);
  if (closeBeforeRequest !== null) {
    // El cierre de un intento sin pedido, sin reintento: lo que hace
    // _finalize_pre_request_failure con retryable=False (worker.py:878-920).
    const closed = one(await query(`
      select status, terminal_reason
      from public.finalize_followup_delivery_attempt($1,$2,$3,$4,'failed_before_request',null,null,$5,null,null,$6::timestamptz)
    `, [actionId, attempt.id, ctx.worker, generation, closeBeforeRequest, now.toISOString()]),
    `${ctx.spec.id} cierre`);
    ctx.step({
      paso: 'cierre_sin_pedido', accion: label, t, motivo: closeBeforeRequest,
      estado: closed.status, terminal_reason: closed.terminal_reason,
    });
    return { claimed: true, decision, closed };
  }
  // La reevaluacion final, antes del interruptor de Meta y del arranque.
  const final = await reevaluate();
  if (final.decision !== 'execute') return { claimed: true, decision: final };
  const operation = START_OPERATION_BY_ANCHOR[claimed.anchor_type];
  if (operation === undefined) throw new Error(`${ctx.spec.id}: un ancla sin arranque: ${claimed.anchor_type}`);
  try {
    one(await query(`
      select * from public.${operation}($1,$2,$3,$4,$5::timestamptz)
    `, [actionId, attempt.id, ctx.worker, generation, now.toISOString()]), `${ctx.spec.id} arranque`);
  } catch (error) {
    const sql = sqlErrorOf(error);
    if (sql.code === '55000' && sql.message === 'pilot_request_start_rejected') {
      ctx.step({ paso: 'arranque', accion: label, t, resultado: 'rechazado', motivo: sql.detail });
    } else {
      ctx.step({ paso: 'arranque', accion: label, t, resultado: 'error', motivo: sql.message, codigo: sql.code });
    }
    return { claimed: true, decision: final, started: false, error: sql };
  }
  const authorization = one(await query(`
    select authrow.local_budget_date::text as local_budget_date, authrow.authorized_at,
           attempt.phase, attempt.request_started_at
    from public.pilot_outbound_request_authorizations authrow
    join public.followup_delivery_attempts attempt on attempt.id = authrow.attempt_id
    where authrow.attempt_id = $1
  `, [attempt.id]), `${ctx.spec.id} autorizacion`);
  ctx.step({
    paso: 'arranque', accion: label, t, resultado: authorization.phase,
    motivo: 'pilot_request_start_authorized', dia_local: ctx.localDay(authorization.local_budget_date),
    autorizada: clockStamp(ctx, authorization.authorized_at),
    request_started_at: clockStamp(ctx, authorization.request_started_at),
  });
  if (accept) {
    acceptanceSerial += 1;
    const accepted = one(await query(`
      select status from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8::timestamptz)
    `, [actionId, attempt.id, ctx.worker, generation, String(770000 + acceptanceSerial),
      `flujo-legado-wamid-${acceptanceSerial}`, `Plantilla aprobada de ATT1 (${ATT1.productName})`,
      now.toISOString()]), `${ctx.spec.id} aceptacion`);
    ctx.step({ paso: 'aceptacion', accion: label, t, estado: accepted.status });
  }
  return { claimed: true, decision: final, started: true };
};
// Una hora que sale del reloj de la base: se guarda como "reloj", despues de
// ver que cae dentro de la corrida del caso.
const clockStamp = (ctx, value) => {
  const ms = new Date(value).getTime();
  if (!(ms >= ctx.runStarted.getTime() - 1000 && ms <= Date.now() + 60_000)) {
    throw new Error(`${ctx.spec.id}: una hora del reloj fuera de la corrida: ${new Date(value).toISOString()}`);
  }
  return 'reloj';
};
// La autorizacion de ayer de G5, sembrada (ver la cabecera).
const seedYesterdayAuthorization = async (ctx, actionId, lead) => {
  const attempt = one(await query(`
    select id from public.followup_delivery_attempts where action_id = $1 and phase = 'reserved'
  `, [actionId]), 'intento reservado de ayer');
  const { generation } = one(await query(`
    select generation from public.pilot_runtime_controls where scope_key = $1
  `, [ctx.slug]), 'generacion');
  await query(`
    insert into public.pilot_outbound_request_authorizations
      (scope_key, scope_version, action_id, attempt_id, contact_id, local_budget_date,
       runtime_generation, reason_code, authorized_at)
    values ($1,$2,$3,$4,$5,($6::timestamptz at time zone $7)::date,$8,'pilot_request_start_authorized',$6::timestamptz)
  `, [ctx.slug, ctx.scopeVersion, actionId, attempt.id, lead.contact,
    new Date(ctx.T0.getTime() - 86_400_000).toISOString(), ctx.zone.name, generation]);
  const day = one(await query(`
    select local_budget_date::text as day from public.pilot_outbound_request_authorizations where attempt_id = $1
  `, [attempt.id]), 'dia sembrado').day;
  ctx.step({
    paso: 'autorizacion_sembrada', accion: ctx.label('a', actionId), persona: lead.letter,
    dia_local: ctx.localDay(day),
  });
};
// El barrido de la toma despues del vencimiento.
const sweepAfter = async (ctx, actionId) => {
  const action = one(await query(`select expires_at from public.scheduled_actions where id = $1`, [actionId]), 'vence');
  const now = new Date(action.expires_at.getTime() + 1000);
  await query(`
    select id from public.claim_due_followup_actions($1,$2::timestamptz,interval '${WORKER_LEASE}',100)
  `, [ctx.worker, now.toISOString()]);
  const after = one(await query(`
    select status, terminal_reason from public.scheduled_actions where id = $1
  `, [actionId]), 'barrido');
  ctx.step({
    paso: 'barrido', accion: ctx.label('a', actionId), t: ctx.rel(now),
    estado: after.status, terminal_reason: after.terminal_reason,
  });
  return after;
};

// Lo que queda al final del caso: los eventos, los casos de sus personas con
// su secuencia y su binding, las acciones con sus intentos, y de cada persona
// su intencion y sus permisos.
const snapshot = async (ctx) => {
  const eventos = {};
  for (const [id, label] of ctx.labels.e) {
    const row = one(await query(`
      select event_type, processing_status, processing_error from public.webhook_events where id = $1
    `, [id]), 'evento');
    eventos[label] = { tipo: row.event_type, estado: row.processing_status, error: row.processing_error };
  }
  const contacts = [...ctx.persons.values()].map((lead) => lead.contact).filter(Boolean);
  const caseRows = await query(`
    select rc.id, rc.status, rc.lead_stage, rc.context ->> 'purchase_correlation' as purchase_correlation,
           rc.abandonment_event_id, rc.purchase_event_id, rc.conversation_id is not null as con_conversacion,
           binding.scope_version, binding.audience_mode,
           (select array_agg(rce.webhook_event_id::text order by rce.observed_at, rce.created_at)
              from public.recovery_case_events rce where rce.recovery_case_id = rc.id) as eventos
    from public.recovery_cases rc
    left join public.pilot_recovery_case_bindings binding on binding.recovery_case_id = rc.id
    where rc.contact_id = any($1::uuid[]) or rc.id = any($2::uuid[])
    order by rc.created_at, rc.id
  `, [contacts, [...ctx.labels.c.keys()]]);
  const casos = {};
  for (const row of caseRows) {
    const label = ctx.label('c', row.id);
    const sequences = await query(`
      select status, completion_reason, automatic_messages_accepted
      from public.followup_sequences where recovery_case_id = $1 order by created_at, id
    `, [row.id]);
    casos[label] = {
      estado: row.status,
      etapa: row.lead_stage,
      compra: ctx.labels.e.get(row.purchase_event_id) ?? (row.purchase_event_id === null ? null : 'otro'),
      correlacion_de_compra: row.purchase_correlation,
      con_conversacion: row.con_conversacion,
      eventos: (row.eventos ?? []).map((id) => ctx.labels.e.get(id) ?? 'otro'),
      scope_version: row.scope_version,
      audiencia: row.audience_mode,
      secuencias: sequences.map((sequence) => ({
        estado: sequence.status,
        cierre: sequence.completion_reason,
        aceptados: sequence.automatic_messages_accepted,
      })),
    };
  }
  const actionRows = await query(`
    select id, recovery_case_id, action_type, step_key, anchor_type, status, terminal_reason,
           due_at, expires_at, execution_attempt_count
    from public.scheduled_actions
    where recovery_case_id = any($1::uuid[])
    order by created_at, id
  `, [caseRows.map((row) => row.id)]);
  const acciones = {};
  for (const row of actionRows) {
    const attempts = await query(`
      select attempt_number, phase, outcome, reason_code,
             exists (select 1 from public.pilot_outbound_request_authorizations authrow
                     where authrow.attempt_id = attempt.id) as autorizado
      from public.followup_delivery_attempts attempt where action_id = $1 order by attempt_number, id
    `, [row.id]);
    acciones[ctx.label('a', row.id)] = {
      caso: ctx.label('c', row.recovery_case_id),
      tipo: row.action_type,
      paso: row.step_key,
      ancla: row.anchor_type,
      estado: row.status,
      terminal_reason: row.terminal_reason,
      due_at: ctx.rel(row.due_at),
      expires_at: ctx.rel(row.expires_at),
      intentos: attempts.map((attempt) => ({
        numero: attempt.attempt_number,
        fase: attempt.phase,
        resultado: attempt.outcome,
        motivo: attempt.reason_code,
        autorizado: attempt.autorizado,
      })),
    };
  }
  const personas = {};
  for (const lead of ctx.persons.values()) {
    const intent = lead.intent === null ? null : one(await query(`
      select lifecycle_state, current_classification, whatsapp_contact_authorized, activation_authorized
      from public.purchase_intents where id = $1
    `, [lead.intent]), 'intencion');
    const grants = lead.contact === null ? [] : await query(`
      select authorization_status, authorization_source, evidence ->> 'reason' as reason
      from public.contact_authorizations
      where contact_id = $1 and channel = 'whatsapp' and purpose = 'cart_recovery'
      order by recorded_at, id
    `, [lead.contact]);
    personas[lead.letter] = {
      contacto: lead.contact !== null,
      intencion: intent === null ? null : {
        estado: intent.lifecycle_state,
        clasificacion: intent.current_classification,
        permisos: intent.whatsapp_contact_authorized && intent.activation_authorized,
      },
      permisos: grants.map((grant) => ({
        estado: grant.authorization_status, fuente: grant.authorization_source, razon: grant.reason,
      })),
    };
  }
  const budget = await query(`
    select local_budget_date::text as day, count(*)::integer as count
    from public.pilot_outbound_request_authorizations where scope_key = $1
    group by local_budget_date order by local_budget_date
  `, [ctx.slug]);
  // Por etiqueta, que sale del orden de los pasos: dos filas creadas en el
  // mismo milisegundo no cambian el archivo.
  const byLabel = (object) => Object.fromEntries(Object.entries(object)
    .sort(([left], [right]) => left.slice(1) - right.slice(1)));
  return {
    eventos: byLabel(eventos),
    casos: byLabel(casos),
    acciones: byLabel(acciones),
    personas,
    autorizaciones_del_scope: Object.fromEntries(budget.map((row) => [ctx.localDay(row.day), row.count])),
  };
};

// ---------------------------------------------------------------------------
// Los 24 casos. `fija` es la fila de §3.2; `check` mira, al generar, que el
// caso fije de verdad lo que dice (un caso que no lo fija no se escribe).
// ---------------------------------------------------------------------------
const stepsOf = (record, paso) => record.pasos.filter((entry) => entry.paso === paso);
const CASES = [
  {
    id: 'G1', ventana: 'abierta',
    que: 'Un carrito de dia: el plan, la reevaluacion, la autorizacion y el request_started.',
    fija: 'el camino feliz',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const cart = await admitEvent(ctx, 'carrito', lead, ctx.disparo);
      const plan = await resolveAndPlan(ctx, cart);
      await lease(ctx, plan.scheduled_action_id, ctx.T0);
    },
    check: (record) => stepsOf(record, 'arranque')[0]?.resultado === 'request_started',
  },
  {
    id: 'G2', ventana: 'cerrada',
    que: 'Un carrito con la ventana cerrada: la reevaluacion difiere al proximo inicio (T0 + 4 h).',
    fija: 'fuera_de_horario',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const cart = await admitEvent(ctx, 'carrito', lead, ctx.disparo);
      const plan = await resolveAndPlan(ctx, cart);
      await lease(ctx, plan.scheduled_action_id, ctx.T0);
    },
    check: (record) => stepsOf(record, 'reevaluacion')[0]?.motivo === 'business_window_closed'
      && record.final.acciones.a1.due_at === 600 + 4 * 3600,
  },
  {
    id: 'G3', ventana: 'cerrada', vence: '02:00:00',
    que: 'Sin horario antes de vencer (vence a las 2 h y la ventana abre a las 4 h): escala.',
    fija: 'la 3 (sin_horario_escala)',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const cart = await admitEvent(ctx, 'carrito', lead, ctx.disparo);
      const plan = await resolveAndPlan(ctx, cart);
      await lease(ctx, plan.scheduled_action_id, ctx.T0);
    },
    check: (record) => stepsOf(record, 'reevaluacion')[0]?.motivo === 'no_business_window_before_expiry'
      && record.final.casos.c1.estado === 'escalated',
  },
  {
    id: 'G4', ventana: 'abierta', tope_diario: 1, tope_total: 10,
    que: 'El tope del dia agotado hoy: A sale y a B se le rechaza el arranque; su intento queda reservado.',
    fija: 'tope_del_flujo (P4), el diario',
    run: async (ctx) => {
      const a = personOf(ctx, 'A');
      const b = personOf(ctx, 'B');
      await admitForm(ctx, a, ctx.at(-1800));
      await admitForm(ctx, b, ctx.at(-1740));
      const cartA = await admitEvent(ctx, 'carrito', a, ctx.disparo);
      const planA = await resolveAndPlan(ctx, cartA);
      await lease(ctx, planA.scheduled_action_id, ctx.T0);
      const cartB = await admitEvent(ctx, 'carrito', b, ctx.at(-540));
      const planB = await resolveAndPlan(ctx, cartB);
      await lease(ctx, planB.scheduled_action_id, ctx.T0);
    },
    check: (record) => stepsOf(record, 'arranque').map((entry) => entry.motivo).join(',')
      === 'pilot_request_start_authorized,pilot_daily_budget_exhausted'
      && record.final.acciones.a2.intentos[0]?.fase === 'reserved',
  },
  {
    id: 'G5', ventana: 'abierta', tope_diario: 1, tope_total: 10,
    que: 'El tope del dia gastado ayer (una autorizacion sembrada con el dia local anterior) no frena hoy: A sale.',
    fija: 'el dia local',
    run: async (ctx) => {
      const y = personOf(ctx, 'Y');
      const a = personOf(ctx, 'A');
      await admitForm(ctx, y, ctx.at(-1800));
      await admitForm(ctx, a, ctx.at(-1740));
      const cartY = await admitEvent(ctx, 'carrito', y, ctx.disparo);
      const planY = await resolveAndPlan(ctx, cartY);
      // Y llega hasta la reserva; su autorizacion es la de ayer.
      const claimed = (await query(`
        select id, lease_generation from public.claim_due_followup_actions($1,$2::timestamptz,interval '${WORKER_LEASE}',100)
      `, [ctx.worker, ctx.T0.toISOString()])).find((row) => row.id === planY.scheduled_action_id);
      const decision = one(await query(`
        select * from public.reevaluate_followup_action($1,$2,$3,$4::timestamptz)
      `, [planY.scheduled_action_id, ctx.worker, claimed.lease_generation, ctx.T0.toISOString()]), 'Y');
      await query(`
        select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp',$6,$7::timestamptz)
      `, [planY.scheduled_action_id, ctx.worker, claimed.lease_generation, decision.case_version,
        decision.sequence_revision, DIRECT_DELIVERY_MODE, ctx.T0.toISOString()]);
      await seedYesterdayAuthorization(ctx, planY.scheduled_action_id, y);
      const cartA = await admitEvent(ctx, 'carrito', a, ctx.at(-540));
      const planA = await resolveAndPlan(ctx, cartA);
      await lease(ctx, planA.scheduled_action_id, ctx.T0);
    },
    check: (record) => stepsOf(record, 'autorizacion_sembrada')[0]?.dia_local === 'anterior'
      && stepsOf(record, 'arranque')[0]?.resultado === 'request_started'
      && stepsOf(record, 'arranque')[0]?.dia_local === 'corrida',
  },
  {
    id: 'G6', ventana: 'abierta', tope_diario: 1, tope_total: 1,
    que: 'El tope total agotado: A sale y a B se le rechaza el arranque por el total (que se mira antes que el del dia).',
    fija: 'tope_del_flujo, el total',
    run: async (ctx) => {
      const a = personOf(ctx, 'A');
      const b = personOf(ctx, 'B');
      await admitForm(ctx, a, ctx.at(-1800));
      await admitForm(ctx, b, ctx.at(-1740));
      const cartA = await admitEvent(ctx, 'carrito', a, ctx.disparo);
      const planA = await resolveAndPlan(ctx, cartA);
      await lease(ctx, planA.scheduled_action_id, ctx.T0);
      const cartB = await admitEvent(ctx, 'carrito', b, ctx.at(-540));
      const planB = await resolveAndPlan(ctx, cartB);
      await lease(ctx, planB.scheduled_action_id, ctx.T0);
    },
    check: (record) => stepsOf(record, 'arranque')[1]?.motivo === 'pilot_total_budget_exhausted',
  },
  {
    id: 'G7', ventana: 'cerrada',
    que: 'Un carrito con un pago fallido abierto (diferido a la apertura): el carrito no arranca, y el due_at del pago fallido no se mueve.',
    fija: '7 (M, no_arranca_mientras:pago_fallido) y el rollback del least(due_at, now())',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const failure = await admitEvent(ctx, 'pago_fallido', lead, ctx.disparo);
      const plan = await resolveAndPlan(ctx, failure);
      await lease(ctx, plan.scheduled_action_id, ctx.T0);
      const cart = await admitEvent(ctx, 'carrito', lead, ctx.at(60));
      await resolveAndPlan(ctx, cart);
    },
    check: (record) => stepsOf(record, 'plan')[1]?.motivo === 'pilot_case_binding_conflict'
      && record.final.acciones.a1.due_at === 600 + 4 * 3600
      && record.final.acciones.a1.estado === 'deferred',
  },
  {
    id: 'G8', ventana: 'abierta',
    que: 'Un pago fallido con un carrito abierto: abre su propio caso.',
    fija: 'el punto 4 de la convivencia',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const cart = await admitEvent(ctx, 'carrito', lead, ctx.disparo);
      await resolveAndPlan(ctx, cart);
      const failure = await admitEvent(ctx, 'pago_fallido', lead, ctx.at(-540));
      await resolveAndPlan(ctx, failure);
    },
    check: (record) => stepsOf(record, 'plan').every((entry) => entry.arranco && entry.creado)
      && Object.keys(record.final.casos).length === 2,
  },
  {
    id: 'G9', ventana: 'abierta',
    que: 'Un segundo carrito mientras el primero espera: no arranca.',
    fija: '7 (M, ya_en_curso)',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const first = await admitEvent(ctx, 'carrito', lead, ctx.disparo);
      await resolveAndPlan(ctx, first);
      const second = await admitEvent(ctx, 'carrito', lead, ctx.at(-480));
      await resolveAndPlan(ctx, second);
    },
    check: (record) => stepsOf(record, 'plan')[1]?.motivo === 'pilot_case_binding_conflict'
      && record.final.acciones.a1.estado === 'pending',
  },
  {
    id: 'G10', ventana: 'abierta',
    que: 'Un carrito despues de que el anterior salio y se acepto: arranca otro caso.',
    fija: 'la repeticion despues del envio',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const first = await admitEvent(ctx, 'carrito', lead, ctx.disparo);
      const plan = await resolveAndPlan(ctx, first);
      await lease(ctx, plan.scheduled_action_id, ctx.T0, { accept: true });
      const second = await admitEvent(ctx, 'carrito', lead, ctx.at(60));
      await resolveAndPlan(ctx, second);
    },
    check: (record) => stepsOf(record, 'plan')[1]?.creado === true
      && record.final.casos.c1.estado === 'sequence_exhausted',
  },
  {
    id: 'G11', ventana: 'cerrada',
    que: 'Un pago fallido repetido antes del envio: queda como evidencia del mismo caso y su due_at se adelanta a now(); la reevaluacion siguiente lo vuelve a diferir.',
    fija: '8 (D, F#11)',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const first = await admitEvent(ctx, 'pago_fallido', lead, ctx.disparo);
      const plan = await resolveAndPlan(ctx, first);
      await lease(ctx, plan.scheduled_action_id, ctx.T0);
      const second = await admitEvent(ctx, 'pago_fallido', lead, ctx.at(60));
      await resolveAndPlan(ctx, second);
      const moved = one(await query(`select due_at from public.scheduled_actions where id = $1`,
        [plan.scheduled_action_id]), 'due_at adelantado');
      ctx.step({ paso: 'due_at', accion: ctx.label('a', plan.scheduled_action_id), due_at: clockStamp(ctx, moved.due_at) });
      await lease(ctx, plan.scheduled_action_id, ctx.T2);
    },
    check: (record) => stepsOf(record, 'plan')[1]?.creado === false
      && stepsOf(record, 'plan')[1]?.caso === 'c1'
      && stepsOf(record, 'due_at')[0]?.due_at === 'reloj'
      && stepsOf(record, 'reevaluacion').length === 2,
  },
  {
    id: 'G12', ventana: 'abierta',
    que: 'Un pago fallido repetido despues del envio (aceptado): abre otro recorrido.',
    fija: '8',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const first = await admitEvent(ctx, 'pago_fallido', lead, ctx.disparo);
      const plan = await resolveAndPlan(ctx, first);
      await lease(ctx, plan.scheduled_action_id, ctx.T0, { accept: true });
      const second = await admitEvent(ctx, 'pago_fallido', lead, ctx.at(60));
      await resolveAndPlan(ctx, second);
    },
    check: (record) => stepsOf(record, 'plan')[1]?.creado === true
      && stepsOf(record, 'plan')[1]?.caso === 'c2',
  },
  {
    id: 'G13', ventana: 'abierta',
    que: 'Una compra ambigua conocida antes del plan (el email de A y el telefono de B, que ya es contacto): la accion se cancela y el caso queda paused; despues, otro carrito de A de la misma oferta no arranca.',
    fija: '2 y 2b (F#6)',
    run: async (ctx) => {
      const a = personOf(ctx, 'A');
      const b = personOf(ctx, 'B');
      // B: un carrito sin formulario. resolve_event crea su contacto y el plan
      // se rechaza por la audiencia.
      const cartB = await admitEvent(ctx, 'carrito', b, ctx.at(-900));
      await resolveAndPlan(ctx, cartB);
      await admitForm(ctx, a, ctx.at(-1800));
      const cartA = await admitEvent(ctx, 'carrito', a, ctx.disparo);
      await admitEvent(ctx, 'compra', a, ctx.at(-570), { phone: b.phone });
      await resolveAndPlan(ctx, cartA);
      const again = await admitEvent(ctx, 'carrito', a, ctx.at(-480));
      await resolveAndPlan(ctx, again);
    },
    check: (record) => record.final.casos.c1?.estado === 'paused'
      && record.final.acciones.a1?.terminal_reason === 'purchase_correlation_ambiguous'
      && stepsOf(record, 'plan')[2]?.motivo === 'pilot_case_binding_conflict',
  },
  {
    id: 'G14', ventana: 'abierta',
    que: 'El cierre que llama el despachador con un nombre rechazado (failed_before_request, template_parameters_missing, sin reintento): la accion queda permanent_failed y el caso abierto, con su secuencia activa; despues, otro carrito de la misma oferta no arranca. No es el binding: plan_cart_recovery intenta abrir otra secuencia en el caso, la base da 23505 (followup_sequences_one_active_per_case_idx) y el adaptador deja el evento con create_recovery_case_failed.',
    fija: '5b (F#7)',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      // El nombre que el filtro de worker.py rechaza (template_greeting_name_is_safe).
      lead.name = 'www.compradora-g14.test';
      await admitForm(ctx, lead, ctx.at(-1800));
      const cart = await admitEvent(ctx, 'carrito', lead, ctx.disparo);
      const plan = await resolveAndPlan(ctx, cart);
      await lease(ctx, plan.scheduled_action_id, ctx.T0, { closeBeforeRequest: 'template_parameters_missing' });
      const again = await admitEvent(ctx, 'carrito', lead, ctx.at(60));
      await resolveAndPlan(ctx, again);
    },
    check: (record) => record.final.acciones.a1.estado === 'permanent_failed'
      && ['grace_period', 'active'].includes(record.final.casos.c1.estado)
      && stepsOf(record, 'plan')[1]?.arranco === false,
  },
  {
    id: 'G15', ventana: 'abierta',
    que: 'La compra antes del envio, aplicada por el worker de compras: el caso queda won.',
    fija: 'compro',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const cart = await admitEvent(ctx, 'carrito', lead, ctx.disparo);
      const plan = await resolveAndPlan(ctx, cart);
      // La compra va despues del plan en el reloj: apply_hotmart_purchase_approved
      // busca casos creados hasta 5 minutos despues de la compra.
      const purchase = await admitEvent(ctx, 'compra', lead, ctx.at(-30));
      await applyPurchase(ctx, purchase);
      await lease(ctx, plan.scheduled_action_id, ctx.T0);
    },
    check: (record) => record.final.casos.c1.estado === 'won'
      && record.final.acciones.a1.terminal_reason === 'purchase_detected',
  },
  {
    id: 'G16', ventana: 'abierta',
    que: 'La compra conocida antes del plan. A paga con otro telefono: la compra no cierra su intencion, y el plan nace won por el disparador. B paga con su telefono: la compra cierra la intencion y el plan se rechaza por la audiencia, antes de llegar al disparador.',
    fija: '4b (F#3); y, con la intencion cerrada, no_entra',
    run: async (ctx) => {
      const a = personOf(ctx, 'A');
      await admitForm(ctx, a, ctx.at(-1800));
      const cartA = await admitEvent(ctx, 'carrito', a, ctx.disparo);
      await admitEvent(ctx, 'compra', a, ctx.at(-540), { phone: otherPhoneOf(ctx, 'A') });
      await resolveAndPlan(ctx, cartA);
      const b = personOf(ctx, 'B');
      await admitForm(ctx, b, ctx.at(-1740));
      const cartB = await admitEvent(ctx, 'carrito', b, ctx.at(-480));
      const purchaseB = await admitEvent(ctx, 'compra', b, ctx.at(-420));
      await resolveAndPlan(ctx, cartB);
      await applyPurchase(ctx, purchaseB);
    },
    check: (record) => record.final.casos.c1?.estado === 'won'
      && record.final.casos.c1?.correlacion_de_compra === 'known_purchase_before_recovery_plan'
      && stepsOf(record, 'plan')[1]?.motivo === 'pilot_scope_rejected:pilot_audience_consented_intent_not_live',
  },
  {
    id: 'G17', ventana: 'abierta', ultimo: true,
    que: 'Un conflicto semantico de compra sin resolver (la compra de otra persona reenviada con otro email): el arranque de A falla. Corre ultimo: el conflicto frena todo arranque de la base.',
    fija: '4c (F#3)',
    run: async (ctx) => {
      const a = personOf(ctx, 'A');
      const b = personOf(ctx, 'B');
      await admitForm(ctx, a, ctx.at(-1800));
      const cart = await admitEvent(ctx, 'carrito', a, ctx.disparo);
      const plan = await resolveAndPlan(ctx, cart);
      const purchase = await admitEvent(ctx, 'compra', b, ctx.at(-540));
      await applyPurchase(ctx, purchase);
      await admitEvent(ctx, 'compra', b, ctx.at(-540), {
        id: purchase.payload.id,
        transaction: purchase.payload.data.purchase.transaction,
        email: `otro-${b.email}`,
      });
      await lease(ctx, plan.scheduled_action_id, ctx.T0);
    },
    check: (record) => stepsOf(record, 'arranque')[0]?.motivo === 'unresolved_purchase_semantic_conflict',
  },
  {
    id: 'G18', ventana: 'abierta',
    que: 'La intencion pasa a identity_conflict (un formulario posterior con otro telefono): el arranque se rechaza, se reintenta en el lease siguiente, y la accion vence.',
    fija: '4 (F#9, intencion_viva)',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const cart = await admitEvent(ctx, 'carrito', lead, ctx.disparo);
      const plan = await resolveAndPlan(ctx, cart);
      await admitForm(ctx, lead, ctx.at(-540), { phone: otherPhoneOf(ctx, 'A') });
      await lease(ctx, plan.scheduled_action_id, ctx.T0, { duration: RETRY_LEASE });
      await lease(ctx, plan.scheduled_action_id, ctx.T2, { duration: RETRY_LEASE });
      await sweepAfter(ctx, plan.scheduled_action_id);
    },
    check: (record) => stepsOf(record, 'arranque').length === 2
      && stepsOf(record, 'arranque').every((entry) => entry.resultado === 'rechazado')
      && stepsOf(record, 'barrido')[0]?.terminal_reason === 'expired_after_worker_crash',
  },
  {
    id: 'G19', ventana: 'abierta',
    que: 'La baja antes del envio (el bridge la pasa con la identidad guardada): cancela.',
    fija: 'baja',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const cart = await admitEvent(ctx, 'carrito', lead, ctx.disparo);
      const plan = await resolveAndPlan(ctx, cart);
      const identity = one(await query(`
        select external_user_id from public.channel_identities where contact_id = $1 and channel = 'whatsapp'
      `, [lead.contact]), 'identidad').external_user_id;
      await optOut(ctx, lead, ctx.at(-300), identity, 'identidad_guardada');
      await lease(ctx, plan.scheduled_action_id, ctx.T0);
    },
    check: (record) => record.final.acciones.a1.estado === 'cancelled'
      && stepsOf(record, 'toma')[0]?.tomada === false,
  },
  {
    id: 'G20', ventana: 'abierta',
    que: 'La baja previa al entrar (llega con el wa_id, sin contacto todavia): el carrito no arranca.',
    fija: '11b (baja)',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      await optOut(ctx, lead, ctx.at(-1200), waIdOf(lead.phone), 'wa_id');
      const cart = await admitEvent(ctx, 'carrito', lead, ctx.disparo);
      await resolveAndPlan(ctx, cart);
    },
    check: (record) => stepsOf(record, 'plan')[0]?.motivo
      === 'pilot_scope_rejected:pilot_audience_consented_intent_prior_opt_out',
  },
  {
    id: 'G21', ventana: 'abierta',
    que: 'El cambio de version con un carrito esperando (pausa la v1, publica y activa la v2, arma): el arranque se rechaza en cada lease hasta vencer.',
    fija: '10 (C, lo_en_curso_sigue)',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const cart = await admitEvent(ctx, 'carrito', lead, ctx.disparo);
      const plan = await resolveAndPlan(ctx, cart);
      await operator(ctx, 'pausa', 1);
      await operator(ctx, 'publica', 2);
      await operator(ctx, 'activa', 2);
      await operator(ctx, 'arma', 2);
      ctx.bridgeVersion = 2;
      await lease(ctx, plan.scheduled_action_id, ctx.T0, { duration: RETRY_LEASE });
      await lease(ctx, plan.scheduled_action_id, ctx.T2, { duration: RETRY_LEASE });
      await sweepAfter(ctx, plan.scheduled_action_id);
    },
    check: (record) => stepsOf(record, 'arranque').length === 2
      && stepsOf(record, 'arranque').every((entry) => entry.motivo === 'pilot_scope_version_mismatch')
      && stepsOf(record, 'barrido')[0]?.estado === 'expired',
  },
  {
    id: 'G22', ventana: 'abierta',
    que: 'La version que el bridge pasa por variable (la v1) no es la activa (la v2): el plan se rechaza.',
    fija: '10b (C, F#4)',
    run: async (ctx) => {
      await operator(ctx, 'pausa', 1);
      await operator(ctx, 'publica', 2);
      await operator(ctx, 'activa', 2);
      await operator(ctx, 'arma', 2);
      ctx.bridgeVersion = 1;
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const cart = await admitEvent(ctx, 'carrito', lead, ctx.disparo);
      await resolveAndPlan(ctx, cart);
    },
    check: (record) => stepsOf(record, 'plan')[0]?.motivo === 'pilot_scope_rejected:pilot_scope_version_mismatch',
  },
  {
    id: 'G23', ventana: 'abierta',
    que: 'El scope pausado con un carrito esperando: el arranque se rechaza; rearmado antes de vencer, en el lease siguiente sale.',
    fija: 'flujo_pausado',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const cart = await admitEvent(ctx, 'carrito', lead, ctx.disparo);
      const plan = await resolveAndPlan(ctx, cart);
      await operator(ctx, 'pausa', 1);
      await lease(ctx, plan.scheduled_action_id, ctx.T0, { duration: RETRY_LEASE });
      await operator(ctx, 'arma', 1);
      await lease(ctx, plan.scheduled_action_id, ctx.T2, { duration: RETRY_LEASE });
    },
    check: (record) => stepsOf(record, 'arranque').map((entry) => entry.motivo).join(',')
      === 'pilot_runtime_not_armed,pilot_request_start_authorized',
  },
  {
    id: 'G24', ventana: 'abierta',
    que: 'Un pago fallido sin carrito previo: el permiso lo concede la intencion con consentimiento, y llega a request_started.',
    fija: 'el principio 6',
    run: async (ctx) => {
      const lead = personOf(ctx, 'A');
      await admitForm(ctx, lead, ctx.at(-1800));
      const failure = await admitEvent(ctx, 'pago_fallido', lead, ctx.disparo);
      const plan = await resolveAndPlan(ctx, failure);
      await lease(ctx, plan.scheduled_action_id, ctx.T0);
    },
    check: (record) => stepsOf(record, 'arranque')[0]?.resultado === 'request_started'
      && record.final.personas.A.permisos[0]?.razon === 'precheckout_whatsapp_consent',
  },
];

const casesStarted = Date.now();
const records = new Map();
const order = [...CASES.filter((spec) => !spec.ultimo), ...CASES.filter((spec) => spec.ultimo)];
for (const spec of order) {
  const ctx = await newCase(spec, CASES.indexOf(spec) + 1);
  await spec.run(ctx);
  const record = {
    id: spec.id,
    que: spec.que,
    fija: spec.fija,
    config: ctx.config,
    pasos: ctx.pasos,
    final: await snapshot(ctx),
  };
  if (!spec.check(record)) {
    console.log(`REGISTRO ${JSON.stringify(record)}`);
    throw new Error(`${spec.id} no fija lo que dice: ${spec.fija}`);
  }
  records.set(spec.id, record);
}
tick('casos', casesStarted);

const FORMAT = {
  que_es: 'Lo que hace hoy el legado (el carrito y el pago fallido del recuperador de ATT1) en 24 casos, G1 a G24 (diseno del motor de flujos, §3.2). Lo genera tests/sql/followup_engine/validate_flow_engine_legacy.mjs con la cadena real de migraciones en PGlite; nadie lo escribe a mano. Con --check, la CI falla si regenerarlo da otra cosa.',
  como_se_genera: 'Una sola base con la cadena aplicada una vez; un scope y una politica por caso (copias de la v5 de att1-recuperacion y de att1-recuperacion-un-toque v1 de la captura de ATT1, con la zona, la ventana y los topes del caso). Los disparos entran por las admisiones portables y los planificadores del piloto, como los llama el bridge; el despacho, por la toma, la reevaluacion, la reserva y el arranque de la base. La cabecera del script dice que hace cada paso y de donde sale.',
  tiempos: 'Toda hora es un entero de segundos desde el disparo del caso, que es T0 - 600 s. T0 (t = 600) es la primera toma: el minuto en curso al empezar el caso mas 2 minutos, para que los planes corran antes en el reloj real (el permiso del carrito nace con el reloj) y los arranques queden a menos de 5 minutos de el. Lo que pasa despues de un envio va en T0 + 60 s (t = 660). En los reintentos, la toma es con un lease de 1 minuto (paso toma, campo lease) y la segunda va en T0 + 61 s (t = 661). "reloj" es una hora que pone el reloj de la base (request_started_at, authorized_at, el now() de un plan): solo se verifica que caiga dentro de la corrida.',
  dias: 'Un dia local es "corrida" (el de T0 en la zona del caso) o "anterior".',
  zona: 'Cada caso corre en Etc/GMT+-N, elegida para que la hora local de T0 caiga entre las 11:30 y las 12:30: ninguna ventana cruza la medianoche.',
  config: 'La configuracion del caso: la ventana (relativa a la hora local de T0), el vencimiento, la gracia, los pasos de la politica, la audiencia del scope y sus topes.',
  pasos: 'Lo que paso, en orden. formulario (persona, t, admision, otro_telefono, misma_intencion); disparo (evento, tipo, persona, t, admision); plan (evento, arranco, motivo = el processing_error que deja el ResolutionWorker, contacto_existia, y si arranco caso, accion y creado; con create_recovery_case_failed, error_de_la_base es lo que contesto la base, que el bridge deja solo en el log); compra_aplicada (resultado de apply_hotmart_purchase_approved); baja (forma: identidad_guardada o wa_id, resultado, acciones_tocadas); operador (que, version, estado); toma (accion, t, tomada, y lease si no es el de 5 minutos); reevaluacion (accion, t, decision, motivo); reserva (accion, t, intento); cierre_sin_pedido (motivo, estado, terminal_reason); arranque (accion, t, resultado = request_started, rechazado o error, motivo = el detail de pilot_request_start_rejected o el mensaje del error, dia_local, autorizada, request_started_at); aceptacion (estado); autorizacion_sembrada (G5); due_at (G11: el que deja el plan del pago fallido repetido); barrido (la toma despues de vencer: estado y terminal_reason).',
  final: 'El estado al terminar el caso. eventos: tipo, estado (processing_status) y error (processing_error). casos: estado, etapa, compra, correlacion_de_compra, con_conversacion, eventos del caso, scope_version y audiencia del binding, y las secuencias (estado, cierre, aceptados). acciones: caso, tipo, paso, ancla, estado, terminal_reason, due_at, expires_at e intentos (numero, fase, resultado, motivo, autorizado). personas: si tiene contacto, su intencion (estado, clasificacion, permisos del formulario) y sus permisos de contacto (estado, fuente, razon). autorizaciones_del_scope: los arranques autorizados del scope del caso, por dia local.',
  referencias: 'e1, c1, a1...: el evento, el caso y la accion en el orden en que aparecen en el caso. "otro" es uno que no es del caso.',
};
const document = {
  _formato: FORMAT,
  casos: CASES.map((spec) => records.get(spec.id)),
};
const text = `${JSON.stringify(document, null, 2)}\n`;

// La comparacion: por caso y por campo.
const differences = [];
const compare = (path, expected, actual) => {
  if (differences.length >= 40) return;
  const kind = (value) => (value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value);
  if (kind(expected) !== kind(actual)) {
    differences.push(`${path}: en el archivo ${JSON.stringify(expected)}, generado ${JSON.stringify(actual)}`);
    return;
  }
  if (kind(expected) === 'array') {
    if (expected.length !== actual.length) {
      differences.push(`${path}: en el archivo ${expected.length} elementos, generados ${actual.length}`);
    }
    for (let i = 0; i < Math.min(expected.length, actual.length); i += 1) compare(`${path}[${i}]`, expected[i], actual[i]);
    return;
  }
  if (kind(expected) === 'object') {
    for (const key of new Set([...Object.keys(expected), ...Object.keys(actual)])) {
      compare(path === '' ? key : `${path}.${key}`, expected[key] ?? null, actual[key] ?? null);
    }
    return;
  }
  if (expected !== actual) {
    differences.push(`${path}: en el archivo ${JSON.stringify(expected)}, generado ${JSON.stringify(actual)}`);
  }
};

let exitCode = gemelaFailures.length === 0 ? 0 : 1;
if (MODE === 'write') {
  writeFileSync(LEGACY_FILE, text);
  console.log(`escrito ${LEGACY_FILE}`);
} else {
  let stored = null;
  try {
    stored = readJson(LEGACY_FILE);
  } catch (error) {
    console.log(`FAIL no se pudo leer ${LEGACY_FILE}: ${error.message}`);
    exitCode = 1;
  }
  if (stored !== null) {
    compare('_formato', stored._formato, FORMAT);
    const storedCases = new Map((stored.casos ?? []).map((record) => [record.id, record]));
    for (const spec of CASES) {
      if (!storedCases.has(spec.id)) {
        differences.push(`${spec.id}: no esta en el archivo`);
        continue;
      }
      compare(spec.id, storedCases.get(spec.id), records.get(spec.id));
    }
    for (const id of storedCases.keys()) {
      if (!records.has(id)) differences.push(`${id}: esta en el archivo y no se genero`);
    }
    if (differences.length === 0 && readFileSync(LEGACY_FILE, 'utf8') !== text) {
      differences.push('el archivo tiene los mismos datos con otro formato: regeneralo con --write');
    }
    for (const difference of differences) console.log(`FAIL ${difference}`);
    if (differences.length > 0) exitCode = 1;
  }
}

await db.close();
tick('total', startedAt);
console.log(`gemela del horario: ${WINDOW.cases.length - gemelaFailures.length}/${WINDOW.cases.length} casos iguales`);
console.log(`casos de hoy: ${records.size} generados${MODE === 'check' ? `, ${differences.length === 0 ? 'iguales al archivo' : `${differences.length} diferencias`}` : ''}`);
console.log(`tiempos (ms): ${JSON.stringify(timings)}`);
console.log(exitCode === 0 ? 'flow engine legacy validation passed' : 'flow engine legacy validation FAILED');
process.exit(exitCode);
