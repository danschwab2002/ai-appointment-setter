// La cadena portable de ATT1 de punta a punta, con la reevaluacion REAL.
//
// Recorre, sobre la cadena completa de migraciones y con las RPC reales:
//   formulario (admit_portable_observed_lead_precheckout, la landing de cada
//   oferta) -> evento de Hotmart (admit_portable_hotmart_*) -> contacto y puntos
//   (lo que deja resolve_event) -> plan del piloto -> claim ->
//   reevaluate_followup_action REAL (nada de followup_action_reevaluated
//   insertado a mano) -> contexto de ejecucion -> reserva approved_template ->
//   mark_*_request_started -> record_and_finalize_followup_acceptance.
// Y, desde 1.3.0, el primer contacto tras el formulario:
//   formulario (admit_and_plan_portable_lead_precheckout) -> claim ->
//   reevaluate_portable_precheckout_action REAL -> contexto -> reserva ->
//   mark_portable_precheckout_request_started -> aceptacion.
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
//      con invalid_next_policy_step. Este validador lo fija;
//   7. la audiencia de produccion (consented_intent, 20260930000300): con la
//      v2 del scope, una intencion consentida entra sin cohorte y llega a la
//      aceptacion, con la intencion y el envio del formulario en el binding y
//      en la autorizacion; una intencion consentida que se da de baja en
//      Chatwoot entre el plan y el envio no sale, no consume el cupo y no
//      deja un permiso activo; sin formulario o con un formulario sin opt-in
//      no se planifica; y el tope total corta el arranque del envio
//      siguiente;
//   8. Hotmart en 521 contra una intencion en 52 (20261001000100): el pago
//      fallido de un movil mexicano que el formulario guardo como 52 + 10 y
//      Hotmart manda como 521 + 10 correlaciona con esa intencion, el permiso
//      lo concede la intencion con phone_match = whatsapp_equivalent y llega a
//      la aceptacion. En la audiencia de produccion, el carrito de un movil
//      argentino (formulario 54 + 10, Hotmart 549 + 10) entra por la misma
//      intencion;
//   9. el primer contacto tras el formulario (20261001000200), con el scope y
//      la politica que publica la instancia (first_contact de
//      politica-piloto.json, consented_intent_in_cohort) y en el orden de su
//      E2E: con el scope desarmado el formulario se admite y no planifica ni
//      crea el contacto; con el contacto sembrado e inscripto y el scope
//      armado, el reenvio planifica con la demora de la politica, la
//      reevaluacion propia deja ejecutar y llega a la aceptacion; y una
//      compra de Hotmart en 521 antes del envio (formulario en 52) hace que la
//      reevaluacion propia cancele el caso sin consumir cupo.
//  10. la respuesta a la plantilla (H7, 20261001000400 y 20261001000500), en
//      el primer contacto (identidad 52 del formulario, wa_id 521), el pago
//      fallido en 521 y el carrito en 549: la admision portable adopta la
//      conversacion (created/draft_only, un evento, replay already_exists),
//      el enlace sale por la reserva portable (H8) con la intencion del
//      formulario, y "Necesito ayuda" con derivacion vuelve al agente
//      (requested, attended, resumed, readmision already_exists); la baja de
//      otra persona en su plantilla da applied, y la v2 directa sobre otra
//      plantilla aceptada sigue dando 22000.
//  11. la compra hecha con nuestro link, en lo que hizo la persona
//      (20261016000300).
//  12. el link del producto que nombra el agente (20261016000400): con un
//      segundo producto en el mismo inbox, la reserva por funnel da la oferta
//      de ese producto, y frena un funnel sin binding en el inbox.
//  13. la compra de otra oferta del producto antes del toque (20261019000300):
//      con el carrito de una oferta planificado, una compra aprobada de otra
//      oferta de la misma persona, que cruza solo por el mail o solo por el
//      telefono, hace que reevaluate_followup_action
//      cancele el caso (cancelled, no won) con purchase_detected_any_offer,
//      sin intento; la compra de otra persona no lo frena.
//  14. la compra con nuestro link, en los dos frenos portables
//      (20261030000400, E-215): quien compro con el link y vuelve a llenar el
//      formulario no recibe el primer contacto (purchase_by_identity), y el
//      carrito de quien pago el link de otro (el comprador de Hotmart, sin
//      cruce con la intencion de la emision) se cancela antes del toque
//      (purchase_detected_any_offer); el link reservado sin compra (el del
//      primer contacto y el del segundo producto) y otra persona no frenan.
//
// Datos:
//   - Binding, ofertas, landings, producto, Chatwoot y consentimiento salen de
//     tests/fixtures/instances/att1/instancia.toml (se lee el archivo).
//   - Politica y scope del piloto: se leen de
//     tests/fixtures/instances/att1/politica-piloto.json, la copia versionada
//     de las constantes de setter-instancia-att1
//     (despliegue/base/aprovisionar-att1.sql, con su commit y sha256 en
//     _fixture): scope, politica de un toque con pasos freeform first_contact
//     y payment_failure_first_contact (decision D2), gracia, vencimiento,
//     topes y lookback de la intencion. Si la instancia los cambia, se
//     actualiza ese archivo y este validador prueba la cadena nueva.
//     DESVIACION DOCUMENTADA: la ventana de envio usa los dias del fixture
//     pero de 00:00 a 23:59, en vez de su horario (09:00-21:00,
//     America/Mexico_City). La puerta de arranque
//     (authorize_lancemos_pilot_request_start) exige p_now a +-5 minutos del
//     reloj de la base, asi que la cadena corre con el reloj real y no puede
//     elegir una hora habil de Ciudad de Mexico.
//   - Carrito: la captura tests/fixtures/hotmart_cart_abandonment_rejected_v1.json
//     con producto y oferta sustituidos (como test_commercial_ally_multi_offer.py),
//     y ademas id, creation_date y comprador: cada caso es otra persona y el
//     evento tiene que caer dentro del lookback de su intencion.
//   - Formulario de los casos 8 y 9: los goldens del traductor de GHL
//     (tests/fixtures/ghl/expected/), que salen de los dos envios capturados,
//     con id, fecha y comprador (email y telefono) sustituidos, igual que
//     validate_portable_precheckout_first_contact.mjs. Los telefonos son
//     sinteticos.
//   - Deuda (A0): no hay PURCHASE_CANCELED, PURCHASE_APPROVED ni lead.precheckout
//     capturados. El pago fallido usa el precedente inline de
//     validate_commercial_ally_payment_failure_recovery.mjs, la compra el de
//     validate_commercial_ally_multi_offer.mjs y el formulario de los casos 1 a
//     7 el de validate_commercial_ally_portable_precheckout.mjs, con los valores
//     de ATT1. El texto aceptado es un marcador: la base guarda el que le pasa
//     el bridge (el cuerpo de la plantilla capturada del inbox 11 lo prueba
//     tests/test_durable_dispatcher_approved_template.py).
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
  grant usage on schema public to service_role;
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

// Valores de aprovisionar-att1.sql de la instancia que el manifiesto no
// declara, desde su copia versionada (un campo que falte aborta).
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
const INTENT_LOOKBACK = required(
  PILOT.purchase_intent_scope?.max_lookback, 'purchase_intent_scope.max_lookback',
);
const SEND_WINDOWS = required(pilotPolicy.business_windows, 'policy.business_windows')
  .map((window) => ({ ...window, start: '00:00', end: '23:59' }));
if (CHANNEL_PROVIDER !== 'waba'
    || pilotPolicy.max_automatic_messages !== 1
    || STEPS.map((step) => step.step_key).join(',') !== 'first_contact,payment_failure_first_contact') {
  // Los casos de abajo (la plantilla aprobada, un toque, el riesgo de los dos
  // pasos) asumen esta forma. Si la instancia la cambia, hay que revisarlos.
  throw new Error(`politica-piloto.json changed shape: ${JSON.stringify({ CHANNEL_PROVIDER, pilotPolicy })}`);
}
// El scope y la politica del primer contacto tras el formulario (caso 9).
const fcScope = required(PILOT.first_contact?.pilot_scope, 'politica-piloto.first_contact.pilot_scope');
const fcPolicy = required(PILOT.first_contact?.policy, 'politica-piloto.first_contact.policy');
const FC_SCOPE = required(fcScope.scope_key, 'first_contact.pilot_scope.scope_key');
const FC_SCOPE_VERSION = required(fcScope.version, 'first_contact.pilot_scope.version');
const FC_POLICY = required(fcPolicy.policy_key, 'first_contact.policy.policy_key');
const FC_GRACE_MINUTES = 60;
if (fcScope.channel_provider !== CHANNEL_PROVIDER
    || fcScope.channel_account_ref_prefix !== pilotScope.channel_account_ref_prefix
    || fcScope.source !== 'landing'
    || fcScope.source_event_type !== 'PRECHECKOUT_FORM_SUBMITTED'
    || fcScope.audience_mode !== 'consented_intent_in_cohort'
    || fcScope.max_cohort_contacts < 2
    || FC_SCOPE === SCOPE
    || fcPolicy.grace_period !== `${FC_GRACE_MINUTES} minutes`
    || fcPolicy.max_automatic_messages !== 1
    || fcPolicy.steps.map((step) => `${step.step_key}:${step.mode}`).join(',') !== 'first_contact:freeform') {
  // El caso 9 (el E2E en cohorte con dos personas, la demora, un toque) asume
  // esta forma. Si la instancia la cambia, hay que revisarlo.
  throw new Error(`politica-piloto.json first_contact changed shape: ${JSON.stringify({ fcScope, fcPolicy })}`);
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
  JSON.stringify(additionalOffers.map(landingOf)),
]);
for (const offer of offers) {
  await db.query(`
    insert into public.hotmart_purchase_intent_scopes
      (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
       offer_ref, max_lookback, active)
    values ($1,$2,$3,$4,$5,$6::interval,true)
  `, [ATT1.tenant, ATT1.funnel, String(ATT1.productId), ATT1.hotlink, offer.offer_code,
    INTENT_LOOKBACK]);
}
await db.query(`
  insert into public.followup_policy_versions
    (policy_key, version, status, purpose, timezone, business_windows,
     grace_period, expires_after, max_automatic_messages, steps,
     approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5::jsonb,$6::interval,$7::interval,$8,$9::jsonb,
          'operator-test',now(),now())
`, [POLICY, POLICY_VERSION, required(pilotPolicy.purpose, 'policy.purpose'), ATT1.timezone,
  JSON.stringify(SEND_WINDOWS), required(pilotPolicy.grace_period, 'policy.grace_period'),
  required(pilotPolicy.expires_after, 'policy.expires_after'),
  pilotPolicy.max_automatic_messages, JSON.stringify(STEPS)]);
await db.query(`
  insert into public.pilot_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     channel, channel_provider, channel_account_ref, source, source_event_type,
     additional_source_event_types, external_product_id, offer_code,
     additional_offer_codes, purpose, policy_key, policy_version, timezone,
     max_cohort_contacts, max_outbound_request_starts_total,
     max_outbound_request_starts_per_day, approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5,'whatsapp',$6,$7,$8,$9,$10::text[],$11,$12,
          $13::text[],$14,$15,$16,$17,$18,$19,$20,'operator-test',now(),now())
`, [
  SCOPE, SCOPE_VERSION, ATT1.tenant, ATT1.accountId, ATT1.inboxId, CHANNEL_PROVIDER,
  CHANNEL_REF, required(pilotScope.source, 'pilot_scope.source'),
  required(pilotScope.source_event_type, 'pilot_scope.source_event_type'),
  required(pilotScope.additional_source_event_types, 'pilot_scope.additional_source_event_types'),
  String(ATT1.productId), defaultOffer.offer_code,
  additionalOffers.map((offer) => offer.offer_code),
  required(pilotScope.purpose, 'pilot_scope.purpose'), POLICY, POLICY_VERSION, ATT1.timezone,
  required(pilotScope.max_cohort_contacts, 'pilot_scope.max_cohort_contacts'),
  required(pilotScope.max_outbound_request_starts_total, 'pilot_scope.max_outbound_request_starts_total'),
  required(pilotScope.max_outbound_request_starts_per_day, 'pilot_scope.max_outbound_request_starts_per_day'),
]);
await db.query(`
  insert into public.pilot_runtime_controls
    (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
  values ($1,$2,'inactive',0,'operator-test','default-off')
`, [SCOPE, SCOPE_VERSION]);

// Lo que la instancia publica para el primer contacto: su politica, su scope
// (con el control inactive, como nace en aprovisionar-att1.sql) y la politica
// de compra del binding, que es la que deja admitir un PURCHASE_APPROVED.
await db.query(`
  insert into public.followup_policy_versions
    (policy_key, version, status, purpose, timezone, business_windows,
     grace_period, expires_after, max_automatic_messages, steps,
     approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5::jsonb,$6::interval,$7::interval,$8,$9::jsonb,
          'operator-test',now(),now())
`, [FC_POLICY, required(fcPolicy.version, 'first_contact.policy.version'),
  required(fcPolicy.purpose, 'first_contact.policy.purpose'), ATT1.timezone,
  JSON.stringify(required(fcPolicy.business_windows, 'first_contact.policy.business_windows')
    .map((window) => ({ ...window, start: '00:00', end: '23:59' }))),
  fcPolicy.grace_period, required(fcPolicy.expires_after, 'first_contact.policy.expires_after'),
  fcPolicy.max_automatic_messages, JSON.stringify(fcPolicy.steps)]);
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
  FC_SCOPE, FC_SCOPE_VERSION, ATT1.tenant, ATT1.accountId, ATT1.inboxId, CHANNEL_PROVIDER,
  CHANNEL_REF, fcScope.source, fcScope.source_event_type,
  required(fcScope.additional_source_event_types, 'first_contact.pilot_scope.additional_source_event_types'),
  String(ATT1.productId), defaultOffer.offer_code,
  additionalOffers.map((offer) => offer.offer_code),
  required(fcScope.purpose, 'first_contact.pilot_scope.purpose'), FC_POLICY, fcPolicy.version,
  ATT1.timezone, fcScope.max_cohort_contacts,
  required(fcScope.max_outbound_request_starts_total, 'first_contact.pilot_scope.max_outbound_request_starts_total'),
  required(fcScope.max_outbound_request_starts_per_day, 'first_contact.pilot_scope.max_outbound_request_starts_per_day'),
  fcScope.audience_mode,
]);
await db.query(`
  insert into public.pilot_runtime_controls
    (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
  values ($1,$2,'inactive',0,'operator-test','default-off')
`, [FC_SCOPE, FC_SCOPE_VERSION]);
await db.query(`
  insert into public.commercial_ally_hotmart_purchase_policies
    (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
  values ($1,$2,$3,true,$4::interval)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion,
  required(PILOT.purchase_policy?.max_lookback, 'purchase_policy.max_lookback')]);

const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};
// Los entrypoints de 1.3.0 se llaman como service_role, el rol del bridge; el
// resto de la cadena, como hasta ahora, con el rol de la sesion.
const asService = async (action) => {
  await db.exec('set role service_role');
  try {
    return await action();
  } finally {
    await db.exec('reset role');
  }
};
const armed = one((await db.query(`
  select * from public.set_lancemos_pilot_runtime_state($1,$2,0,'armed','operator-test','controlled-test')
`, [SCOPE, SCOPE_VERSION])).rows, 'arm');
const status = one((await db.query(`
  select * from public.get_lancemos_pilot_runtime_status($1,$2,$3,$4,$5)
`, [SCOPE, SCOPE_VERSION, ATT1.tenant, CHANNEL_PROVIDER, CHANNEL_REF])).rows, 'runtime status');
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

// Casos 8 y 9: el formulario es el golden del traductor de GHL de la landing
// (el de -d es un movil mexicano, 52 + 10 digitos; el de ads-a uno argentino,
// 54 + 10), con id, fecha y comprador sustituidos.
const golden = (name, { country, callingCode, offerCode }) => {
  const file = JSON.parse(readFileSync(join(root, 'tests/fixtures/ghl/expected', name), 'utf8'));
  const { raw_payload: raw, canonical_payload: canonical } = file;
  const offer = offers.find((candidate) => candidate.offer_code === offerCode);
  if (!raw || !canonical || raw.id !== canonical.external_submission_id
      || raw.version !== '1.1.0'
      || canonical.identity.phone_country_iso !== country
      || raw.data.buyer.phone_country_code !== callingCode
      || !new RegExp(`^${callingCode}[0-9]{10}$`).test(canonical.identity.phone)
      || canonical.commerce.offer_ref !== offerCode
      || canonical.consent.copy_version !== ATT1.copyVersion
      || canonical.consent.whatsapp_contact !== true
      || offer === undefined) {
    throw new Error(`${name} is not the ${country} translator golden this validator expects`);
  }
  return { raw, canonical, offer, callingCode, country, name: canonical.lead.full_name };
};
const GOLDEN = {
  MX: golden('ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json',
    { country: 'MX', callingCode: '52', offerCode: '2uafw5bg' }),
  AR: golden('ghl_form_webhook_att1_ads_a_20260929.lead_precheckout.json',
    { country: 'AR', callingCode: '54', offerCode: 'gopi6lh7' }),
};
// ULID como el del adaptador: 48 bits de milisegundos y 80 aleatorios.
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
// Una persona con un movil sintetico en sus dos formas: formPhone (52 / 54 +
// 10 digitos, la que guarda el formulario) y whatsapp (521 / 549 + 10, la de
// Hotmart y el wa_id). phone es el telefono del contacto de la base: el que
// trae el evento que lo crea (Hotmart) o el que siembra el operador (el del
// formulario).
const mobilePerson = (label, country, { contactForm }) => {
  personIndex += 1;
  const suffix = String(personIndex).padStart(2, '0');
  const { callingCode, offer, name } = GOLDEN[country];
  const national = `${country === 'MX' ? '55' : '11'}555506${suffix}`;
  const formPhone = `${callingCode}${national}`;
  const whatsapp = `${callingCode}${country === 'MX' ? '1' : '9'}${national}`;
  return {
    label,
    country,
    offer,
    name,
    callingCode,
    email: `att1-chain-${suffix}@example.test`,
    formPhone,
    whatsapp,
    phone: contactForm === 'whatsapp' ? whatsapp : formPhone,
  };
};
const goldenForm = (lead, submittedAt) => {
  const source = GOLDEN[lead.country];
  const raw = structuredClone(source.raw);
  const canonical = structuredClone(source.canonical);
  const id = ulidAt(submittedAt.getTime());
  const dedupeKey = `${raw.source.site}:${raw.data.offer.code}:${lead.email}`;
  raw.id = id;
  raw.created_at = submittedAt.toISOString();
  raw.data.buyer.email = lead.email;
  raw.data.buyer.phone = `+${lead.formPhone}`;
  raw.data.buyer.phone_country_code = lead.callingCode;
  raw.data.buyer.phone_national = lead.formPhone.slice(lead.callingCode.length);
  raw.dedupe_key = dedupeKey;
  canonical.external_submission_id = id;
  canonical.submitted_at = submittedAt.toISOString().replace('.000Z', 'Z');
  canonical.identity.email = lead.email;
  canonical.identity.phone = lead.formPhone;
  canonical.dedupe_key = dedupeKey;
  return { id, raw, canonical, submittedAt };
};
const intentOf = async (id) => one((await db.query(`
  select normalized_phone, offer_ref, lifecycle_state
  from public.purchase_intents where id = $1
`, [id])).rows, 'intent');
// El formulario por la admision de siempre (la de una instancia sin el flag
// del primer contacto): deja la intencion con el telefono en la forma 52 / 54.
const admitGoldenForm = async (lead) => {
  const form = goldenForm(lead, SUBMITTED_AT);
  const admitted = one((await db.query(`
    select * from public.admit_portable_observed_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, form.id, JSON.stringify(form.raw),
    JSON.stringify(form.canonical)])).rows, `${lead.label} form`);
  const intent = await intentOf(admitted.purchase_intent_id);
  if (admitted.outcome !== 'inserted'
      || intent.normalized_phone !== lead.formPhone
      || intent.normalized_phone === lead.whatsapp
      || intent.offer_ref !== lead.offer.offer_code) {
    throw new Error(`${lead.label}: the GHL form did not leave the intent in the form phone: ${JSON.stringify({ outcome: admitted.outcome, offer: intent.offer_ref })}`);
  }
  return admitted.purchase_intent_id;
};

// Formulario de la landing de la oferta (precedente inline del contrato).
// consented=false es un envio 1.0.0 sin opt-in: la admision lo acepta y la
// intencion queda sin los dos permisos.
const admitForm = async (lead, offer, { consented = true } = {}) => {
  const version = consented ? '1.1.0' : '1.0.0';
  const id = `att1-chain-form-${lead.email}-${offer.offer_code}`;
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
    select landing_ref, offer_ref, whatsapp_contact_authorized, activation_authorized
    from public.purchase_intents where id = $1
  `, [admitted.purchase_intent_id])).rows, `${lead.label} intent`);
  if (admitted.outcome !== 'inserted'
      || intent.landing_ref !== offer.landing_id
      || intent.offer_ref !== offer.offer_code
      || intent.whatsapp_contact_authorized !== consented
      || intent.activation_authorized !== consented) {
    throw new Error(`${lead.label}: the form of ${offer.landing_id} did not leave the expected intent: ${JSON.stringify({ admitted, intent })}`);
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
    select * from public.set_lancemos_pilot_cohort_member($1,$2,$3,$4,'active','operator-test','controlled-test')
  `, [SCOPE, SCOPE_VERSION, lead.contact, generation])).rows, `${lead.label} enrollment`);
  if (member.member_status !== 'active') {
    throw new Error(`${lead.label} was not enrolled: ${JSON.stringify(member)}`);
  }
};

// Los mismos argumentos que arma resolution.resolve_event con el binding
// portable. version es LANCEMOS_PILOT_SCOPE_VERSION del bridge.
const planCart = (lead, offer, cart, { version = SCOPE_VERSION } = {}) => db.query(`
  select * from public.plan_lancemos_pilot_cart_recovery(
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
  )
`, [cart.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  offer.offer_code, POLICY, POLICY_VERSION, cart.abandonedAt.toISOString(), ATT1.accountId,
  ATT1.inboxId, lead.phone, SCOPE, version]);
const planFailure = (lead, offer, failure, { version = SCOPE_VERSION } = {}) => db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
  )
`, [failure.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  offer.offer_code, POLICY, POLICY_VERSION, failure.failedAt.toISOString(), ATT1.accountId,
  ATT1.inboxId, lead.phone, SCOPE, version]);

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
// Desde 1.3.0 el ancla precheckout_intent (el primer contacto tras el
// formulario) tiene tambien su propia reevaluacion: la compartida sola
// ejecutaria sin mirar sus frenos (compra, carrito, opt-out, consentimiento).
const DIRECT_DELIVERY_MODE = 'approved_template';
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
const operationFor = (operations, anchorType) => {
  const operation = operations[anchorType];
  if (operation === undefined) throw new Error(`no RPC declared for anchor_type ${anchorType}`);
  return operation;
};
const startOperationFor = (anchorType) => operationFor(START_OPERATION_BY_ANCHOR, anchorType);
const reevaluateOperationFor = (anchorType) => operationFor(REEVALUATE_OPERATION_BY_ANCHOR, anchorType);
// Las RPC propias del primer contacto se llaman como service_role.
const asBridge = (anchorType, action) => (
  anchorType === 'precheckout_intent' ? asService(action) : action());

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
  const reevaluateOperation = reevaluateOperationFor(claimed[0].anchor_type);
  const decision = one((await asBridge(claimed[0].anchor_type, () => db.query(`
    select * from public.${reevaluateOperation}($1,$2,$3,$4)
  `, [plan.scheduled_action_id, worker, lease, now]))).rows, `${lead.label} reevaluation`);
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
  const startedAt = await dbNow();
  const started = one((await asBridge(claimed[0].anchor_type, () => db.query(`
    select * from public.${startOperation}($1,$2,$3,$4,$5)
  `, [plan.scheduled_action_id, attempt.id, worker, lease, startedAt]))).rows,
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
  // chatwoot: la conversacion de Chatwoot en la que salio la plantilla, la que
  // registro la aceptacion (el caso 10 responde ahi).
  return {
    decision, context, started, reevaluateOperation, startOperation,
    chatwoot: 900100 + messageNumber, actionId: plan.scheduled_action_id,
  };
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
// Las personas de los carritos, con la conversacion de su plantilla: el caso
// 10 usa dos (la baja y el control de la v2).
const cartRuns = [];
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
  cartRuns.push({ lead, run });
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

// ---------------------------------------------------------------------------
// 8a. Hotmart en 521 contra una intencion en 52. El formulario de GHL guarda el
//     movil mexicano como 52 + 10 digitos; Hotmart manda el pago fallido con
//     521 + 10, y con ese telefono crea resolve_event el contacto y arma el
//     plan. Antes de 20261001000100 el evento no correlacionaba con la
//     intencion y el plan cerraba sin permiso.
// ---------------------------------------------------------------------------
const mxFailure = mobilePerson('pago-fallido-521-contra-52', 'MX', { contactForm: 'whatsapp' });
const mxFailureIntent = await admitGoldenForm(mxFailure);
const mxFailureEvent = await admitFailure(mxFailure, mxFailure.offer, 'HPATT1CHAINMX1');
if (mxFailureEvent.intentId !== mxFailureIntent
    || mxFailure.phone !== mxFailure.whatsapp
    || !/^521[0-9]{10}$/.test(mxFailure.phone)
    || !/^52[0-9]{10}$/.test(mxFailure.formPhone)) {
  throw new Error('the payment failure in 521 did not correlate with the form intent in 52');
}
await createContact(mxFailure, mxFailureEvent.eventId);
await enroll(mxFailure);
const mxFailurePlan = one((await planFailure(mxFailure, mxFailure.offer, mxFailureEvent)).rows,
  'payment failure in 521 plan');
const mxFailureGrants = await authorizationsOf(mxFailure);
if (!mxFailurePlan.created
    || mxFailureGrants.length !== 1
    || mxFailureGrants[0].authorization_status !== 'allowed'
    || mxFailureGrants[0].authorization_source !== 'system'
    || mxFailureGrants[0].evidence.reason !== 'precheckout_whatsapp_consent'
    || mxFailureGrants[0].evidence.purchase_intent_id !== mxFailureIntent
    || mxFailureGrants[0].evidence.phone_match !== 'whatsapp_equivalent') {
  throw new Error(`the payment failure in 521 did not get the consented permission of the intent in 52: ${JSON.stringify({ created: mxFailurePlan.created, grants: mxFailureGrants.length, source: mxFailureGrants[0]?.authorization_source, phone_match: mxFailureGrants[0]?.evidence?.phone_match })}`);
}
// El control: en los casos de arriba formulario y Hotmart traen el mismo
// telefono, y la evidencia lo dice.
if (failureOnlyGrants[0].evidence.phone_match !== 'exact') {
  throw new Error(`an exact phone did not record phone_match = exact: ${failureOnlyGrants[0].evidence.phone_match}`);
}
const mxFailureRun = await dispatch(mxFailure, mxFailurePlan, {
  anchor: 'payment_failure', stepKey: 'payment_failure_first_contact', offer: mxFailure.offer,
});

// Cada envio consumio exactamente una autorizacion del piloto.
const starts = one((await db.query(`
  select count(*)::integer as count from public.pilot_outbound_request_authorizations
  where scope_key = $1
`, [SCOPE])).rows, 'pilot authorizations');
if (starts.count !== 7) {
  throw new Error(`expected seven pilot request starts, got ${starts.count}`);
}

// ---------------------------------------------------------------------------
// 9. El primer contacto tras el formulario, con el scope de la instancia
//    (consented_intent_in_cohort) y en el orden de su E2E. El formulario entra
//    por admit_and_plan_portable_lead_precheckout, lo que llama el bridge con
//    PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED.
// ---------------------------------------------------------------------------
const FC_DUE = at(-(FC_GRACE_MINUTES + 1));
const fcStarts = async () => one((await db.query(`
  select count(*)::integer as count from public.pilot_outbound_request_authorizations
  where scope_key = $1
`, [FC_SCOPE])).rows, 'first contact pilot authorizations').count;
const fcStatus = async () => one((await asService(() => db.query(`
  select * from public.get_portable_precheckout_pilot_runtime_status($1,$2,$3,$4,$5)
`, [FC_SCOPE, FC_SCOPE_VERSION, ATT1.tenant, CHANNEL_PROVIDER, CHANNEL_REF]))).rows,
'first contact status');
// Envia el formulario por el entrypoint y devuelve la admision con el renglon
// del plan y, si se planifico, su accion, con la forma que espera dispatch.
const submitFirstContact = async (lead, submittedAt) => {
  const form = goldenForm(lead, submittedAt);
  const admitted = one((await asService(() => db.query(`
    select * from public.admit_and_plan_portable_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, form.id, JSON.stringify(form.raw),
    JSON.stringify(form.canonical), FC_SCOPE, FC_SCOPE_VERSION]))).rows, `${lead.label} form`);
  const ledger = one((await db.query(`
    select * from public.portable_precheckout_first_contact_plans where submission_id = $1
  `, [admitted.submission_id])).rows, `${lead.label} plan row`);
  if (admitted.outcome !== 'inserted'
      || ledger.outcome !== admitted.plan_outcome
      || ledger.reason_code !== admitted.plan_reason
      || ledger.purchase_intent_id !== admitted.purchase_intent_id) {
    throw new Error(`${lead.label}: the form was not admitted with its plan row: ${JSON.stringify({ outcome: admitted.outcome, plan: admitted.plan_outcome, reason: admitted.plan_reason })}`);
  }
  const result = {
    form,
    submission: admitted.submission_id,
    intent: admitted.purchase_intent_id,
    result: `${ledger.outcome}:${ledger.reason_code}`,
    contact: ledger.contact_id,
    recovery_case_id: ledger.recovery_case_id,
  };
  if (ledger.outcome === 'planned') {
    const action = one((await db.query(`
      select id, followup_sequence_id, due_at, anchor_type, step_key, action_type
      from public.scheduled_actions where recovery_case_id = $1
    `, [ledger.recovery_case_id])).rows, `${lead.label} action`);
    result.scheduled_action_id = action.id;
    result.followup_sequence_id = action.followup_sequence_id;
    result.action = action;
  }
  return result;
};
const peopleFootprint = async () => JSON.stringify(one((await db.query(`
  select
    (select count(*)::integer from public.contacts) as contacts,
    (select count(*)::integer from public.contact_points) as points,
    (select count(*)::integer from public.channel_identities) as identities,
    (select count(*)::integer from public.recovery_cases) as cases,
    (select count(*)::integer from public.scheduled_actions) as actions
`)).rows, 'people footprint'));
// Lo que deja el script de siembra de la instancia para el E2E.
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
const enrollFirstContact = async (lead) => {
  const { generation } = one((await db.query(`
    select generation from public.pilot_runtime_controls where scope_key = $1
  `, [FC_SCOPE])).rows, 'first contact generation');
  const member = one((await db.query(`
    select * from public.set_lancemos_pilot_cohort_member($1,$2,$3,$4,'active','operator-test','controlled-test')
  `, [FC_SCOPE, FC_SCOPE_VERSION, lead.contact, generation])).rows, `${lead.label} enrollment`);
  if (member.member_status !== 'active') {
    throw new Error(`${lead.label} was not enrolled in the first contact scope: ${JSON.stringify(member)}`);
  }
};
const expectFirstContactPlan = (label, lead, plan) => {
  const dueMs = plan.action ? new Date(plan.action.due_at).getTime() : null;
  if (plan.result !== 'planned:first_contact_scheduled'
      || plan.contact !== lead.contact
      || plan.action.anchor_type !== 'precheckout_intent'
      || plan.action.step_key !== 'first_contact'
      || plan.action.action_type !== 'first_contact_review'
      || dueMs !== plan.form.submittedAt.getTime() + FC_GRACE_MINUTES * 60_000) {
    throw new Error(`${label}: the form did not plan the first contact after the grace period: ${JSON.stringify({ result: plan.result, action: plan.action })}`);
  }
};

// 9.1 Con el scope desarmado: el formulario se admite, no planifica y no crea
//     ni contacto ni trabajo.
const fcLead = mobilePerson('primer-contacto', 'MX', { contactForm: 'form' });
const fcInactive = await fcStatus();
const fcEmpty = await peopleFootprint();
const fcDisarmed = await submitFirstContact(fcLead, FC_DUE);
if (fcInactive.configured !== true || fcInactive.runtime_state !== 'inactive'
    || fcDisarmed.result !== 'not_planned:pilot_runtime_not_armed'
    || fcDisarmed.contact !== null
    || (await peopleFootprint()) !== fcEmpty) {
  throw new Error(`the disarmed first contact scope planned or created something: ${JSON.stringify({ state: fcInactive.runtime_state, result: fcDisarmed.result })}`);
}
// 9.2 Sembrar el contacto, inscribirlo y armar; el reenvio planifica.
await seedContact(fcLead);
await enrollFirstContact(fcLead);
const fcArmed = one((await db.query(`
  select * from public.set_lancemos_pilot_runtime_state($1,$2,
    (select generation from public.pilot_runtime_controls where scope_key = $1),
    'armed','operator-test','controlled-test')
`, [FC_SCOPE, FC_SCOPE_VERSION])).rows, 'arm first contact');
const fcArmedStatus = await fcStatus();
if (fcArmed.runtime_state !== 'armed' || fcArmedStatus.configured !== true
    || fcArmedStatus.runtime_state !== 'armed') {
  throw new Error(`the first contact scope is not configured and armed: ${JSON.stringify({ fcArmed, fcArmedStatus })}`);
}
const fcPlan = await submitFirstContact(fcLead, FC_DUE);
expectFirstContactPlan('first contact', fcLead, fcPlan);
if (fcPlan.intent !== fcDisarmed.intent || fcPlan.submission === fcDisarmed.submission) {
  throw new Error('the resend did not reuse the intent with a new submission');
}
// 9.3 El caso queda atado al scope del primer contacto, no al de recuperacion,
//     con la intencion y el envio del formulario como evidencia de audiencia.
const fcBinding = one((await db.query(`
  select scope_key, scope_version, audience_mode, audience_purchase_intent_id,
         audience_precheckout_submission_id
  from public.pilot_recovery_case_bindings where recovery_case_id = $1
`, [fcPlan.recovery_case_id])).rows, 'first contact binding');
if (fcBinding.scope_key !== FC_SCOPE || fcBinding.scope_version !== FC_SCOPE_VERSION
    || fcBinding.audience_mode !== fcScope.audience_mode
    || fcBinding.audience_purchase_intent_id !== fcPlan.intent
    || fcBinding.audience_precheckout_submission_id !== fcPlan.submission) {
  throw new Error(`the first contact case is not bound to its own scope: ${JSON.stringify(fcBinding)}`);
}
// 9.4 La cadena del dispatcher, con la reevaluacion y el arranque propios.
const fcRun = await dispatch(fcLead, fcPlan, {
  anchor: 'precheckout_intent', stepKey: 'first_contact', offer: fcLead.offer,
});
if (fcRun.reevaluateOperation !== 'reevaluate_portable_precheckout_action'
    || fcRun.startOperation !== 'mark_portable_precheckout_request_started'
    || fcRun.context.buyer_name !== fcLead.name
    || (await fcStarts()) !== 1) {
  throw new Error(`the first contact did not go through its own RPCs: ${JSON.stringify({ reevaluate: fcRun.reevaluateOperation, start: fcRun.startOperation, starts: await fcStarts() })}`);
}
const fcGrants = await authorizationsOf(fcLead);
const fcControl = one((await db.query(`
  select data from public.pilot_control_events
  where event_type = 'pilot_outbound_request_authorized' and scope_key = $1
`, [FC_SCOPE])).rows, 'first contact control event').data;
if (fcGrants.length !== 1
    || fcGrants[0].authorization_source !== 'system'
    || fcGrants[0].evidence.reason !== 'precheckout_whatsapp_consent'
    || fcGrants[0].evidence.purchase_intent_id !== fcPlan.intent
    || fcControl.audience_mode !== fcScope.audience_mode
    || fcControl.audience_purchase_intent_id !== fcPlan.intent
    || fcControl.audience_precheckout_submission_id !== fcPlan.submission) {
  throw new Error(`the first contact did not record the consent of the form: ${JSON.stringify({ grants: fcGrants.length, mode: fcControl.audience_mode })}`);
}
// 9.4b La respuesta a esta plantilla se prueba en el caso 10, al final: junto
//      con la del carrito en 549 (8b), que se manda despues.
// 9.5 La compra antes del envio, con otra persona sembrada e inscripta. El
//     formulario guardo 52 + 10 y Hotmart manda la compra con 521 + 10: la
//     intencion queda purchased y la reevaluacion propia cancela el caso
//     (cancelled, no won) sin intento y sin consumir cupo.
const fcBuyer = mobilePerson('primer-contacto-compra', 'MX', { contactForm: 'form' });
await seedContact(fcBuyer);
await enrollFirstContact(fcBuyer);
const fcBuyerPlan = await submitFirstContact(fcBuyer, FC_DUE);
expectFirstContactPlan('first contact buyer', fcBuyer, fcBuyerPlan);
const fcPurchasePayload = {
  id: `att1-chain-purchase-${fcBuyer.email}`,
  creation_date: at(-2).getTime(),
  event: 'PURCHASE_APPROVED',
  version: '2.0.0',
  data: {
    product: { id: ATT1.productId, ucode: 'ATT1-CHAIN-UCODE' },
    buyer: { email: fcBuyer.email, checkout_phone: `+${fcBuyer.whatsapp}` },
    purchase: {
      approved_date: at(-2).getTime(),
      status: 'APPROVED',
      transaction: 'HPATT1CHAINBUY1',
      offer: { code: fcBuyer.offer.offer_code },
    },
  },
};
const fcPurchase = one((await db.query(`
  select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, fcPurchasePayload.id,
  JSON.stringify(fcPurchasePayload), fcBuyer.email, fcBuyer.whatsapp])).rows,
'first contact purchase');
const fcCorrelation = one((await db.query(`
  select outcome, purchase_intent_id from public.portable_hotmart_purchase_correlations
  where webhook_event_id = $1
`, [fcPurchase.webhook_event_id])).rows, 'first contact purchase correlation');
if (fcPurchase.outcome !== 'inserted'
    || fcCorrelation.outcome !== 'resolved'
    || fcCorrelation.purchase_intent_id !== fcBuyerPlan.intent
    || (await intentOf(fcBuyerPlan.intent)).lifecycle_state !== 'purchased'
    || (await intentOf(fcBuyerPlan.intent)).normalized_phone !== fcBuyer.formPhone) {
  throw new Error(`the purchase in 521 did not close the intent in 52: ${JSON.stringify({ outcome: fcPurchase.outcome, correlation: fcCorrelation.outcome })}`);
}
const fcBuyerWorker = `att1-chain-${fcBuyer.label}`;
const fcBuyerNow = await dbNow();
const fcBuyerClaim = (await db.query(`
  select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
`, [fcBuyerWorker, fcBuyerNow])).rows;
if (fcBuyerClaim.length !== 1 || fcBuyerClaim[0].id !== fcBuyerPlan.scheduled_action_id
    || fcBuyerClaim[0].anchor_type !== 'precheckout_intent') {
  throw new Error(`first contact buyer: claimed ${JSON.stringify(fcBuyerClaim.map((row) => [row.id, row.anchor_type]))}`);
}
const fcBuyerDecision = one((await asService(() => db.query(`
  select * from public.${reevaluateOperationFor(fcBuyerClaim[0].anchor_type)}($1,$2,$3,$4)
`, [fcBuyerPlan.scheduled_action_id, fcBuyerWorker, fcBuyerClaim[0].lease_generation,
  fcBuyerNow]))).rows, 'first contact buyer reevaluation');
const fcBuyerState = one((await db.query(`
  select
    (select status from public.recovery_cases where id = $1) as case_status,
    (select purchase_event_id from public.recovery_cases where id = $1) as purchase_event_id,
    (select status from public.scheduled_actions where id = $2) as action_status,
    (select terminal_reason from public.scheduled_actions where id = $2) as terminal_reason,
    (select count(*)::integer from public.followup_delivery_attempts where action_id = $2) as attempts
`, [fcBuyerPlan.recovery_case_id, fcBuyerPlan.scheduled_action_id])).rows, 'first contact buyer state');
if (fcBuyerDecision.decision !== 'cancel'
    || fcBuyerDecision.reason_code !== 'intent_purchased'
    || fcBuyerState.case_status !== 'cancelled'
    || fcBuyerState.purchase_event_id !== null
    || fcBuyerState.action_status !== 'cancelled'
    || fcBuyerState.terminal_reason !== 'intent_purchased'
    || fcBuyerState.attempts !== 0
    || (await fcStarts()) !== 1
    || (await openActions()).length !== 0) {
  throw new Error(`the purchase before the send did not cancel the first contact: ${JSON.stringify({ decision: fcBuyerDecision, state: fcBuyerState, starts: await fcStarts() })}`);
}
// El primer contacto no toco el scope de recuperacion.
const startsAfterFirstContact = one((await db.query(`
  select count(*)::integer as count from public.pilot_outbound_request_authorizations
  where scope_key = $1
`, [SCOPE])).rows, 'recovery starts after the first contact').count;
if (startsAfterFirstContact !== starts.count) {
  throw new Error(`the first contact consumed starts of the recovery scope: ${startsAfterFirstContact}`);
}

// ---------------------------------------------------------------------------
// 5. La audiencia de produccion: consented_intent (migracion 20260930000300),
//    con el procedimiento del operador de docs/design/lancemos-pilot-boundary.md
//    (seccion 2.4): pausar la v1, publicar la v2 igual a la v1 salvo
//    audience_mode y los topes, activarla (queda inactive), apuntar
//    LANCEMOS_PILOT_SCOPE_VERSION a la v2 y armar. La cohorte de la v2 esta
//    vacia: la membresia es por version y no se copia.
//    La v2 todavia no esta en politica-piloto.json (la instancia la publica
//    despues de este bloque): sus valores salen de la v1 del fixture. El total
//    cuenta lo que ya consumio la v1, asi que total = consumido + 2 deja salir
//    dos (la intencion consentida y el carrito argentino del caso 8b) y corta
//    el siguiente.
//    Casos: una intencion consentida entra sin cohorte; sin formulario, con un
//    formulario sin opt-in (carrito y pago fallido), no; el tope corta.
// ---------------------------------------------------------------------------
const OPEN_VERSION = SCOPE_VERSION + 1;
const OPEN_TOTAL = starts.count + 2;
const generationOf = async () => one((await db.query(`
  select generation from public.pilot_runtime_controls where scope_key = $1
`, [SCOPE])).rows, 'scope generation').generation;
const paused = one((await db.query(`
  select * from public.set_lancemos_pilot_runtime_state($1,$2,$3,'paused','operator-test','open-audience')
`, [SCOPE, SCOPE_VERSION, await generationOf()])).rows, 'pause v1');
await db.query(`
  insert into public.pilot_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     channel, channel_provider, channel_account_ref, source, source_event_type,
     additional_source_event_types, external_product_id, offer_code,
     additional_offer_codes, purpose, policy_key, policy_version, timezone,
     max_cohort_contacts, max_outbound_request_starts_total,
     max_outbound_request_starts_per_day, audience_mode,
     approved_by, approved_at, published_at)
  select scope_key, $2, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
         channel, channel_provider, channel_account_ref, source, source_event_type,
         additional_source_event_types, external_product_id, offer_code,
         additional_offer_codes, purpose, policy_key, policy_version, timezone,
         max_cohort_contacts, $3, $3, 'consented_intent',
         'operator-test', now(), now()
  from public.pilot_scope_versions where scope_key = $1 and version = $4
`, [SCOPE, OPEN_VERSION, OPEN_TOTAL, SCOPE_VERSION]);
const activated = one((await db.query(`
  select * from public.activate_lancemos_pilot_scope_version($1,$2,$3,'operator-test','open-audience')
`, [SCOPE, OPEN_VERSION, await generationOf()])).rows, 'activate v2');
const rearmed = one((await db.query(`
  select * from public.set_lancemos_pilot_runtime_state($1,$2,$3,'armed','operator-test','open-audience')
`, [SCOPE, OPEN_VERSION, await generationOf()])).rows, 'arm v2');
const openStatus = one((await db.query(`
  select * from public.get_lancemos_pilot_runtime_status($1,$2,$3,$4,$5)
`, [SCOPE, OPEN_VERSION, ATT1.tenant, CHANNEL_PROVIDER, CHANNEL_REF])).rows, 'v2 status');
const openMembers = one((await db.query(`
  select count(*)::integer as count from public.pilot_cohort_memberships
  where scope_key = $1 and scope_version = $2
`, [SCOPE, OPEN_VERSION])).rows, 'v2 cohort').count;
if (paused.runtime_state !== 'paused'
    || activated.runtime_state !== 'inactive'
    || rearmed.runtime_state !== 'armed'
    || openStatus.configured !== true || openStatus.runtime_state !== 'armed'
    || openMembers !== 0) {
  throw new Error(`the v2 with consented_intent is not armed with an empty cohort: ${JSON.stringify({ paused, activated, rearmed, openStatus, openMembers })}`);
}
const openPlan = { version: OPEN_VERSION };
const submissionOf = async (intentId) => one((await db.query(`
  select submission_id from public.purchase_intent_submissions where purchase_intent_id = $1
`, [intentId])).rows, 'form submission').submission_id;

// El opt-out conserva precedencia sin cohorte ni operador: una intencion
// consentida se planifica en la v2 y la persona se da de baja en Chatwoot
// (apply_chatwoot_inbound_opt_out) antes del envio. La accion queda cancelada,
// el permiso del carrito se cierra y no se autoriza ningun arranque. Va antes
// de las personas siguientes a proposito: la v2 tiene dos cupos, y si la baja
// hubiera consumido uno, el segundo envio de abajo no saldria.
const optedOut = person('audiencia-opt-out');
const optedOutOffer = additionalOffers[1];
await admitForm(optedOut, optedOutOffer);
const optedOutCart = await admitCart(optedOut, optedOutOffer);
await createContact(optedOut, optedOutCart.eventId);
const optedOutPlan = one((await planCart(optedOut, optedOutOffer, optedOutCart, openPlan)).rows,
  'opted-out plan');
const optedOutResult = one((await db.query(`
  select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,$6,'unsubscribe')
`, [ATT1.accountId, ATT1.inboxId, 8801, 98801, optedOut.phone, await dbNow()])).rows,
'opt-out');
const optedOutState = one((await db.query(`
  select
    (select status from public.scheduled_actions where id = $1) as action_status,
    (select count(*)::integer from public.contact_authorizations
      where contact_id = $2 and authorization_status = 'allowed'
        and valid_from <= clock_timestamp()
        and (valid_until is null or valid_until > clock_timestamp())) as active_allowed,
    (select count(*)::integer from public.pilot_outbound_request_authorizations
      where scope_key = $3) as starts
`, [optedOutPlan.scheduled_action_id, optedOut.contact, SCOPE])).rows, 'opted-out state');
if (optedOutResult.outcome !== 'applied'
    || optedOutResult.matched_contact_id !== optedOut.contact
    || optedOutState.action_status !== 'cancelled'
    || optedOutState.active_allowed !== 0
    || optedOutState.starts !== starts.count) {
  throw new Error(`the opt-out did not keep precedence over the consented audience: ${JSON.stringify({ optedOutResult, optedOutState })}`);
}

// Entra sin cohorte, y el binding y la autorizacion dicen con que evidencia:
// la intencion y el envio 1.1.0 del formulario.
const openLead = person('audiencia-consentida');
const openOffer = defaultOffer;
const openIntent = await admitForm(openLead, openOffer);
const openSubmission = await submissionOf(openIntent);
const openCart = await admitCart(openLead, openOffer);
await createContact(openLead, openCart.eventId);
const openCartPlan = one((await planCart(openLead, openOffer, openCart, openPlan)).rows,
  'consented intent cart plan');
const openBinding = one((await db.query(`
  select scope_version, audience_mode, audience_purchase_intent_id,
         audience_precheckout_submission_id
  from public.pilot_recovery_case_bindings where recovery_case_id = $1
`, [openCartPlan.recovery_case_id])).rows, 'consented intent binding');
if (!openCartPlan.created
    || openBinding.scope_version !== OPEN_VERSION
    || openBinding.audience_mode !== 'consented_intent'
    || openBinding.audience_purchase_intent_id !== openIntent
    || openBinding.audience_precheckout_submission_id !== openSubmission) {
  throw new Error(`the consented intent did not bind the case: ${JSON.stringify({ openCartPlan, openBinding })}`);
}
const openRun = await dispatch(openLead, openCartPlan, {
  anchor: 'cart_abandonment', stepKey: 'first_contact', offer: openOffer,
});
const openControl = one((await db.query(`
  select data from public.pilot_control_events
  where event_type = 'pilot_outbound_request_authorized' and scope_version = $1
`, [OPEN_VERSION])).rows, 'consented intent control event').data;
if (openControl.audience_mode !== 'consented_intent'
    || openControl.audience_purchase_intent_id !== openIntent
    || openControl.audience_precheckout_submission_id !== openSubmission
    || openRun.started.pilot_authorization_id == null) {
  throw new Error(`the authorization did not record the audience: ${JSON.stringify(openControl)}`);
}

// 8b. Hotmart en 549 contra una intencion en 54, en la audiencia de produccion:
//     el carrito de un movil argentino entra sin cohorte por la intencion que
//     dejo el formulario de GHL con 54 + 10 digitos. Antes de 20261001000100
//     se rechazaba con pilot_audience_intent_unresolved.
const arCart = mobilePerson('carrito-549-contra-54', 'AR', { contactForm: 'whatsapp' });
const arCartIntent = await admitGoldenForm(arCart);
const arCartSubmission = await submissionOf(arCartIntent);
const arCartEvent = await admitCart(arCart, arCart.offer);
await createContact(arCart, arCartEvent.eventId);
const arCartPlan = one((await planCart(arCart, arCart.offer, arCartEvent, openPlan)).rows,
  'cart in 549 plan');
const arCartBinding = one((await db.query(`
  select audience_mode, audience_purchase_intent_id, audience_precheckout_submission_id
  from public.pilot_recovery_case_bindings where recovery_case_id = $1
`, [arCartPlan.recovery_case_id])).rows, 'cart in 549 binding');
if (!arCartPlan.created
    || !/^549[0-9]{10}$/.test(arCart.phone)
    || !/^54[0-9]{10}$/.test(arCart.formPhone)
    || arCartBinding.audience_mode !== 'consented_intent'
    || arCartBinding.audience_purchase_intent_id !== arCartIntent
    || arCartBinding.audience_precheckout_submission_id !== arCartSubmission) {
  throw new Error(`the cart in 549 did not enter by the intent in 54: ${JSON.stringify({ created: arCartPlan.created, mode: arCartBinding.audience_mode })}`);
}
const arCartRun = await dispatch(arCart, arCartPlan, {
  anchor: 'cart_abandonment', stepKey: 'first_contact', offer: arCart.offer,
});

// Sin consentimiento no entra: sin formulario, o con un formulario sin opt-in
// (carrito y pago fallido). No queda ni caso ni permiso.
const noForm = person('audiencia-sin-formulario');
const noFormCart = await admitCart(noForm, openOffer);
await createContact(noForm, noFormCart.eventId);
await expectPilotRejection('consented_intent without a form',
  () => planCart(noForm, openOffer, noFormCart, openPlan), 'pilot_audience_intent_unresolved');
const noOptIn = person('audiencia-sin-opt-in');
const noOptInOffer = additionalOffers[0];
await admitForm(noOptIn, noOptInOffer, { consented: false });
const noOptInCart = await admitCart(noOptIn, noOptInOffer);
await createContact(noOptIn, noOptInCart.eventId);
await expectPilotRejection('consented_intent with a form without opt-in (cart)',
  () => planCart(noOptIn, noOptInOffer, noOptInCart, openPlan),
  'pilot_audience_consented_intent_not_authorized');
const noOptInFailure = await admitFailure(noOptIn, noOptInOffer, 'HPATT1CHAINPF3');
await expectPilotRejection('consented_intent with a form without opt-in (payment failure)',
  () => planFailure(noOptIn, noOptInOffer, noOptInFailure, openPlan),
  'pilot_audience_consented_intent_not_authorized');
const rejectedEffects = one((await db.query(`
  select
    (select count(*)::integer from public.recovery_cases where contact_id = any($1::uuid[])) as cases,
    (select count(*)::integer from public.contact_authorizations where contact_id = any($1::uuid[])) as grants
`, [[noForm.contact, noOptIn.contact]])).rows, 'rejected audience effects');
if (rejectedEffects.cases !== 0 || rejectedEffects.grants !== 0) {
  throw new Error(`a contact outside the audience left durable work: ${JSON.stringify(rejectedEffects)}`);
}

// El tope corta: otra intencion consentida se planifica, pero el arranque del
// envio se rechaza sin consumir. La accion queda reservada: va al final.
const capped = person('audiencia-tope');
const cappedOffer = additionalOffers[1];
await admitForm(capped, cappedOffer);
const cappedCart = await admitCart(capped, cappedOffer);
await createContact(capped, cappedCart.eventId);
const cappedPlan = one((await planCart(capped, cappedOffer, cappedCart, openPlan)).rows,
  'capped plan');
const cappedWorker = `att1-chain-${capped.label}`;
const cappedNow = await dbNow();
const cappedClaim = (await db.query(`
  select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
`, [cappedWorker, cappedNow])).rows;
if (cappedClaim.length !== 1 || cappedClaim[0].id !== cappedPlan.scheduled_action_id) {
  throw new Error(`capped: claimed ${JSON.stringify(cappedClaim.map((row) => row.id))}`);
}
const cappedDecision = one((await db.query(`
  select * from public.reevaluate_followup_action($1,$2,$3,$4)
`, [cappedPlan.scheduled_action_id, cappedWorker, cappedClaim[0].lease_generation, cappedNow])).rows,
'capped reevaluation');
const cappedAttempt = one((await db.query(`
  select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp',$6,$7)
`, [cappedPlan.scheduled_action_id, cappedWorker, cappedClaim[0].lease_generation,
  cappedDecision.case_version, cappedDecision.sequence_revision, DIRECT_DELIVERY_MODE,
  cappedNow])).rows, 'capped reservation');
let cappedError = null;
try {
  await db.query(`select * from public.${startOperationFor(cappedClaim[0].anchor_type)}($1,$2,$3,$4,$5)`, [
    cappedPlan.scheduled_action_id, cappedAttempt.id, cappedWorker,
    cappedClaim[0].lease_generation, await dbNow(),
  ]);
} catch (caught) {
  cappedError = caught;
}
const finalStarts = one((await db.query(`
  select count(*)::integer as count from public.pilot_outbound_request_authorizations
  where scope_key = $1
`, [SCOPE])).rows, 'final pilot authorizations').count;
if (cappedDecision.decision !== 'execute'
    || cappedError?.code !== '55000'
    || cappedError?.message !== 'pilot_request_start_rejected'
    || cappedError?.detail !== 'pilot_total_budget_exhausted'
    || finalStarts !== OPEN_TOTAL) {
  throw new Error(`the total cap did not cut the consented audience: ${JSON.stringify({ cappedDecision, code: cappedError?.code, message: cappedError?.message, detail: cappedError?.detail, finalStarts })}`);
}

// ---------------------------------------------------------------------------
// 10. La respuesta a la plantilla (H7: migraciones 20261001000400 y
//     20261001000500), sobre tres plantillas que ya salieron por la cadena
//     real de arriba y que la aceptacion dejo en 'enabled': el primer
//     contacto (9.4, identidad 52 del formulario, wa_id 521), el pago fallido
//     en 521 contra la intencion en 52 (8a, identidad 521) y el carrito en 549
//     contra la intencion en 54 (8b, identidad 549). Va al final porque el
//     carrito en 549 sale en la audiencia de produccion.
//     El bridge con manifiesto resuelve el wa_id a la identidad guardada
//     (resolve_inbound_external_user_id de src/bridge/app.py, con
//     equivalent_whatsapp_phones de src/bridge/phones.py; aca, el mismo
//     criterio sobre channel_identities) y hace sus tres llamadas a la
//     admision (admision, reautorizacion y readmision tras reanudar) por
//     admit_portable_inbound_commercial_case_v1, como service_role:
//     - la respuesta adopta la conversacion: created/draft_only, un solo
//       evento inbound_adopted_template_conversation (con la plantilla y su
//       accion) y los casos cart_recovery + inbound_sales; el replay da
//       already_exists sin otro evento;
//     - "Enviame el enlace": la reserva portable (H8,
//       reserve_portable_checkout_issuance_v2) con el id resuelto da el
//       enlace con la oferta, la intencion y la atribucion del formulario.
//       Sobre la identidad 521 la compartida daria la oferta por defecto: se
//       mide como control dentro de una transaccion que se deshace;
//     - "Necesito ayuda", si el agente deriva: la derivacion da requested y,
//       mientras la tiene el equipo, la reautorizacion no admite; la persona
//       del equipo contesta (mark_human_handoff_attended: attended, sobre el
//       inbound_sales), el macro reanuda (resume_paused_conversation con
//       operator_request: resumed, sobre el inbound_sales) y la readmision da
//       already_exists. Sin la 000500 la marca de atencion y la reanudacion
//       abortan como ambiguas (dos casos en la conversacion);
//     - "No mas mensajes" de otra persona (un carrito del caso 2): la
//       respuesta se adopta y la baja da applied.
//     Control: la v2 directa, sobre la plantilla aceptada de otro carrito del
//     caso 2, sigue dando 22000 inbound_canonical_conversation_conflict y la
//     conversacion queda enabled y sin evento.
//     Datos: el scope entrante y el catalogo de las tres ofertas se siembran
//     como despliegue/base/aprovisionar-att1.sql de la instancia (el hotlink
//     como external_product_id y la primera oferta por defecto), igual que
//     validate_whatsapp_phone_equivalence.mjs; la politica de la nota de
//     derivacion, con el equipo del manifiesto, como
//     validate_portable_precheckout_first_contact.mjs. Lo que se simula: la
//     nota de la derivacion pasa a 'projected' con un update, que es lo que
//     deja el proyector de notas (como validate_handoff_attendance.mjs).
// ---------------------------------------------------------------------------
const ADOPTION_EVENT = 'inbound_adopted_template_conversation';
const HANDOFF = { policy: 'att1-derivacion-entrante', version: 1 };
await db.query(`
  insert into public.inbound_commercial_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     external_product_id, offer_code, approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5,$6,$7,'operator-test',now(),now())
`, [ATT1.inboundScope, ATT1.inboundVersion, ATT1.tenant, ATT1.accountId, ATT1.inboxId,
  ATT1.hotlink, defaultOffer.offer_code]);
for (const offer of offers) {
  await db.query(`
    insert into public.checkout_offer_catalog
      (tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref,
       offer_code, checkout_base_url, checkout_mode, default_for_inbound, status,
       version, approved_by, approved_at)
    values ($1,$2,$3,$4,$5,$6,$7,$8,10,$9,'active',1,'operator-test',now())
  `, [ATT1.tenant, ATT1.funnel, ATT1.inboundScope, ATT1.inboundVersion, ATT1.hotlink,
    offer.landing_id, offer.offer_code, `https://pay.hotmart.com/${ATT1.hotlink}`,
    offer.default]);
}
await db.query(`
  insert into public.human_handoff_projection_policies (
    policy_key, policy_version, scope_key, scope_version,
    inbound_scope_key, inbound_scope_version, expected_team_id,
    note_template_key, note_template_version, private_note_body, active
  ) values ($1,$2,null,null,$3,$4,$5,'att1-nota-derivacion',1,
    'Derivacion automatica de prueba.',true)
`, [HANDOFF.policy, HANDOFF.version, ATT1.inboundScope, ATT1.inboundVersion,
  required(manifest.chatwoot?.equipo_derivacion, 'chatwoot.equipo_derivacion')]);

const tryRows = async (sql, params) => {
  try {
    return { ok: true, rows: (await db.query(sql, params)).rows };
  } catch (caught) {
    return { ok: false, code: caught.code, message: caught.message };
  }
};
const asServiceTry = (sql, params) => asService(() => tryRows(sql, params));
const showResult = (result, field = 'automation_status') => (result.ok
  ? `${result.rows[0].outcome}:${result.rows[0][field]}`
  : `${result.code} ${result.message}`);
const PORTABLE_ADMISSION = 'select * from public.admit_portable_inbound_commercial_case_v1($1,$2,$3,$4)';
const SHARED_ADMISSION = 'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)';

// El id con que el bridge admite y reserva: entre las formas equivalentes del
// wa_id, la unica identidad activa del inbox si no es la textual; si no, el
// wa_id (app.py resolve_inbound_external_user_id).
const equivalentForms = (waId) => {
  let canonical = waId;
  if (/^521[0-9]{10}$/.test(waId)) canonical = `52${waId.slice(3)}`;
  if (/^549[0-9]{10}$/.test(waId)) canonical = `54${waId.slice(3)}`;
  if (/^52[0-9]{10}$/.test(canonical)) return [canonical, `521${canonical.slice(2)}`];
  if (/^54[0-9]{10}$/.test(canonical)) return [canonical, `549${canonical.slice(2)}`];
  return [canonical];
};
const bridgeExternalUserId = async (waId) => {
  const forms = equivalentForms(waId);
  if (forms.length < 2) return waId;
  const stored = [...new Set((await db.query(`
    select external_user_id from public.channel_identities
    where channel = 'whatsapp' and account_id = $1 and external_user_id = any($2::text[])
      and identity_status = 'active' and metadata ->> 'inbox_id' = $3
  `, [`chatwoot:${ATT1.accountId}`, forms, String(ATT1.inboxId)])).rows
    .map((row) => row.external_user_id))].sort();
  return stored.length === 1 && stored[0] !== waId ? stored[0] : waId;
};
// La conversacion de la plantilla, con sus casos y sus eventos de adopcion.
const templateConversation = async (chatwoot) => one((await db.query(`
  select conversation.id, conversation.status, conversation.automation_status,
         conversation.human_takeover, conversation.version::text as version,
         conversation.contact_id, identity.external_user_id,
         (select string_agg(commercial_case.case_kind, '+' order by commercial_case.case_kind)
            from public.commercial_cases commercial_case
           where commercial_case.conversation_id = conversation.id) as kinds,
         (select count(*)::integer from public.conversation_events event
           where event.conversation_id = conversation.id and event.event_type = $2) as adoptions
  from public.conversations conversation
  join public.channel_identities identity on identity.id = conversation.channel_identity_id
  where conversation.commercial_context = jsonb_build_object('chatwoot_conversation_id', $1::text)
`, [String(chatwoot), ADOPTION_EVENT])).rows, `conversation ${chatwoot}`);
const conversationLine = (state) => (
  `${state.status}/${state.automation_status}/takeover=${state.human_takeover}`);
const allAdoptions = async () => one((await db.query(`
  select count(*)::integer as count from public.conversation_events where event_type = $1
`, [ADOPTION_EVENT])).rows, 'adoption events').count;

// El enlace como lo pide el bridge con manifiesto: la reserva portable con el
// id resuelto, y la emision leida con el rol de la sesion.
let replyMessage = 990500;
const issuanceOf = async (issuanceId) => (issuanceId === null ? null : one((await db.query(`
  select issuance.source_kind, issuance.offer_resolution, issuance.attribution_resolution,
         issuance.purchase_intent_id, issuance.checkout_url_final, offer.offer_code
  from public.checkout_link_issuances issuance
  join public.checkout_offer_catalog offer on offer.id = issuance.offer_catalog_id
  where issuance.id = $1
`, [issuanceId])).rows, 'issuance'));
const reserveSql = (rpc) => `select * from public.${rpc}($1,$2,$3,$4,$5,$6,$7,clock_timestamp())`;
// El reloj de PGlite tiene resolucion de milisegundo y las transiciones del
// caso exigen updated_at estrictamente mayor que el anterior
// (protect_inbound_commercial_case): antes de cada una se deja pasar el reloj.
const tick = () => new Promise((done) => { setTimeout(done, 3); });
const formSck = GOLDEN.MX.raw.data.attribution.sck;
const formFbclid = GOLDEN.MX.raw.data.attribution.fbclid;

const REPLIES = [
  { label: 'primer-contacto', lead: fcLead, run: fcRun, intent: fcPlan.intent, attribution: 'full' },
  { label: 'pago-fallido-521', lead: mxFailure, run: mxFailureRun, intent: mxFailureIntent, attribution: 'full', sharedControl: true },
  { label: 'carrito-549', lead: arCart, run: arCartRun, intent: arCartIntent, attribution: 'marker_only' },
];
const templateReplies = {};
// Las emisiones de cada respuesta, para la compra con nuestro link del final.
const reservedLinks = {};
for (const reply of REPLIES) {
  const { label, lead, run, intent, attribution } = reply;
  const before = await templateConversation(run.chatwoot);
  const userId = await bridgeExternalUserId(lead.whatsapp);
  const args = [ATT1.inboundScope, ATT1.inboundVersion, run.chatwoot, userId];
  const template = one((await db.query(`
    select accepted_message_id from public.followup_delivery_attempts
    where action_id = $1 and outcome = 'accepted_by_chatwoot'
  `, [run.actionId])).rows, `${label} template`).accepted_message_id;

  // La respuesta: adopta; el replay no adopta otra vez.
  const admitted = await asServiceTry(PORTABLE_ADMISSION, args);
  const adopted = await templateConversation(run.chatwoot);
  const events = (await db.query(`
    select actor_type, related_message_id, related_action_id from public.conversation_events
    where conversation_id = $1 and event_type = $2
  `, [before.id, ADOPTION_EVENT])).rows;
  const event = events[0] ?? {};
  const replay = await asServiceTry(PORTABLE_ADMISSION, args);
  const replayed = await templateConversation(run.chatwoot);
  if (before.automation_status !== 'enabled' || before.adoptions !== 0
      || before.external_user_id !== userId
      || showResult(admitted) !== 'created:draft_only'
      || admitted.rows[0].conversation_id !== before.id
      || admitted.rows[0].contact_id !== lead.contact
      || adopted.automation_status !== 'draft_only'
      || adopted.adoptions !== 1
      || events.length !== 1
      || adopted.kinds !== 'cart_recovery+inbound_sales'
      || event.actor_type !== 'integration'
      || event.related_message_id !== template
      || event.related_action_id !== run.actionId
      || showResult(replay) !== 'already_exists:draft_only'
      || replay.rows[0].commercial_case_id !== admitted.rows[0].commercial_case_id
      || replayed.adoptions !== 1
      || replayed.version !== adopted.version) {
    throw new Error(`${label}: the reply to the template was not adopted once: ${JSON.stringify({ before: conversationLine(before), user: [before.external_user_id === userId], admitted: showResult(admitted), adopted: [conversationLine(adopted), adopted.kinds, adopted.adoptions], event: [event.actor_type, event.related_message_id === template, event.related_action_id === run.actionId], replay: showResult(replay), replayed: replayed.adoptions })}`);
  }
  const caseId = admitted.rows[0].commercial_case_id;

  // "Enviame el enlace": la reserva portable con el id resuelto.
  replyMessage += 1;
  const ulid = ulidAt(Date.now());
  const reserveArgs = [caseId, userId, ATT1.accountId, ATT1.inboxId, run.chatwoot,
    String(replyMessage), ulid];
  const reserved = await asServiceTry(reserveSql('reserve_portable_checkout_issuance_v2'), reserveArgs);
  const link = reserved.ok ? await issuanceOf(reserved.rows[0].issuance_id) : null;
  const url = link?.checkout_url_final ?? '';
  // El marcador se separa con ~ desde 20261005000100; la ~ viaja literal.
  const sckInUrl = attribution === 'full'
    ? `&sck=${formSck}~hermes~v1~${ulid}`
    : `&sck=hermes~v1~${ulid}`;
  if (!reserved.ok || reserved.rows[0].outcome !== 'reserved'
      || link.source_kind !== 'precheckout_request'
      || link.offer_resolution !== 'lead_intent'
      || link.offer_code !== lead.offer.offer_code
      || link.purchase_intent_id !== intent
      || link.attribution_resolution !== attribution
      || !url.includes(`?off=${lead.offer.offer_code}&`)
      || !url.includes(sckInUrl)
      || (attribution === 'full') !== url.endsWith(`&fbclid=${formFbclid}`)) {
    throw new Error(`${label}: the link is not the one of the form: ${JSON.stringify({ reserved: reserved.ok ? reserved.rows[0].outcome : `${reserved.code} ${reserved.message}`, ...link, offer: lead.offer.offer_code, intent: link?.purchase_intent_id === intent })}`);
  }
  reservedLinks[label] = {
    issuanceId: reserved.rows[0].issuance_id, lead, userId, intent, caseId, chatwoot: run.chatwoot,
  };
  let shared = null;
  if (reply.sharedControl) {
    // La compartida (la de Johanna) con el mismo caso y la identidad 521:
    // busca la intencion por igualdad exacta y no ve la del formulario (52).
    await db.exec('begin');
    try {
      await db.exec('set local role service_role');
      replyMessage += 1;
      const row = one((await db.query(reserveSql('reserve_chatwoot_checkout_issuance_v2'), [
        caseId, userId, ATT1.accountId, ATT1.inboxId, run.chatwoot, String(replyMessage),
        ulidAt(Date.now()),
      ])).rows, `${label} shared reserve`);
      await db.exec('reset role');
      shared = await issuanceOf(row.issuance_id);
    } finally {
      await db.exec('rollback');
    }
    if (!/^521[0-9]{10}$/.test(userId)
        || shared?.offer_resolution !== 'default_no_intent'
        || shared?.purchase_intent_id === intent) {
      throw new Error(`${label}: the shared reserve control changed: ${JSON.stringify({ identity521: /^521[0-9]{10}$/.test(userId), ...shared })}`);
    }
  }

  // "Necesito ayuda" con derivacion: la persona del equipo contesta, el macro
  // reanuda y la persona vuelve a escribir.
  await tick();
  const handoff = await asServiceTry(`
    select * from public.request_inbound_human_handoff(
      $1::uuid, $2, 'explicit_human_request', $3, $4, clock_timestamp(), null)
  `, [caseId, `handoff:att1-chain:${run.chatwoot}`, HANDOFF.policy, HANDOFF.version]);
  await db.query(`
    update public.human_handoff_requests
    set status = 'projected', projected_at = clock_timestamp(), updated_at = clock_timestamp()
    where commercial_case_id = $1
  `, [caseId]);
  const handedOff = await templateConversation(run.chatwoot);
  const whileHandedOff = await asServiceTry(PORTABLE_ADMISSION, args);
  await tick();
  const attended = await asServiceTry(`
    select * from public.mark_human_handoff_attended($1::bigint, clock_timestamp(), clock_timestamp())
  `, [run.chatwoot]);
  await tick();
  const resumed = await asServiceTry(`
    select * from public.resume_paused_conversation(
      $1::bigint, $2, 'operator_request', null, 3, clock_timestamp())
  `, [run.chatwoot, `resume:att1-chain:${run.chatwoot}`]);
  await tick();
  const readmitted = await asServiceTry(PORTABLE_ADMISSION, args);
  const after = await templateConversation(run.chatwoot);
  const help = {
    handoff: handoff.ok ? handoff.rows[0].outcome : `${handoff.code} ${handoff.message}`,
    handed_off: conversationLine(handedOff),
    reply_while_handed_off: showResult(whileHandedOff),
    attended: attended.ok
      ? `${attended.rows[0].outcome}:${attended.rows[0].attended_count}:${attended.rows[0].attended_commercial_case_id === caseId ? 'inbound_sales' : 'other'}`
      : `${attended.code} ${attended.message}`,
    resumed: resumed.ok
      ? `${resumed.rows[0].outcome}:${resumed.rows[0].resumed_commercial_case_id === caseId ? 'inbound_sales' : 'other'}`
      : `${resumed.code} ${resumed.message}`,
    readmitted: showResult(readmitted),
    after: conversationLine(after),
  };
  const expectedHelp = {
    handoff: 'requested',
    handed_off: 'paused_human/paused/takeover=true',
    reply_while_handed_off: 'blocked:disabled',
    attended: 'attended:1:inbound_sales',
    resumed: 'resumed:inbound_sales',
    readmitted: 'already_exists:draft_only',
    after: 'active/draft_only/takeover=false',
  };
  if (JSON.stringify(help) !== JSON.stringify(expectedHelp)
      || readmitted.rows[0].commercial_case_id !== caseId
      || after.adoptions !== 1) {
    throw new Error(`${label}: "Necesito ayuda" did not go back to the agent: ${JSON.stringify(help)}`);
  }
  templateReplies[label] = {
    identity: `${userId === lead.whatsapp ? 'the wa_id' : 'the form phone'} (${userId.slice(0, userId.length - 10)} + 10)`,
    reply: `${showResult(admitted)}, adoption events ${adopted.adoptions}, replay ${showResult(replay)}`,
    link: `${reserved.rows[0].outcome}:${link.offer_resolution}:${link.offer_code}:${link.attribution_resolution}`,
    ...(shared ? { shared_reserve_control: shared.offer_resolution } : {}),
    help: `${help.handoff} -> ${help.attended} -> ${help.resumed} -> ${help.readmitted}`,
  };
}

// "No mas mensajes" de otra persona (un carrito del caso 2): la respuesta se
// adopta (una admision por la portable) y la baja va por el camino de siempre.
const leaving = cartRuns[1];
const leavingUser = await bridgeExternalUserId(leaving.lead.phone);
const leavingAdmitted = await asServiceTry(PORTABLE_ADMISSION,
  [ATT1.inboundScope, ATT1.inboundVersion, leaving.run.chatwoot, leavingUser]);
replyMessage += 1;
await tick();
const leavingOptOut = await asServiceTry(`
  select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,clock_timestamp(),'stop_receiving_messages')
`, [ATT1.accountId, ATT1.inboxId, leaving.run.chatwoot, replyMessage, leavingUser]);
const leavingAfter = one((await db.query(`
  select contact_permission from public.contacts where id = $1
`, [leaving.lead.contact])).rows, 'contact after the opt-out').contact_permission;
const leavingConversation = await templateConversation(leaving.run.chatwoot);
if (leavingUser !== leaving.lead.phone
    || showResult(leavingAdmitted) !== 'created:draft_only'
    || leavingConversation.adoptions !== 1
    || !leavingOptOut.ok || leavingOptOut.rows[0].outcome !== 'applied'
    || leavingOptOut.rows[0].matched_contact_id !== leaving.lead.contact
    || leavingAfter !== 'opted_out') {
  throw new Error(`the opt-out on an adopted template conversation: ${JSON.stringify({ admitted: showResult(leavingAdmitted), adoptions: leavingConversation.adoptions, optOut: leavingOptOut.ok ? leavingOptOut.rows[0].outcome : `${leavingOptOut.code} ${leavingOptOut.message}`, permission: leavingAfter })}`);
}

// Control: la v2 directa, sobre la plantilla aceptada de otro carrito, sigue
// rechazando y no adopta.
const control = cartRuns[2];
const controlResult = await asServiceTry(SHARED_ADMISSION,
  [ATT1.inboundScope, ATT1.inboundVersion, control.run.chatwoot, control.lead.phone]);
const controlConversation = await templateConversation(control.run.chatwoot);
const totalAdoptions = await allAdoptions();
if (showResult(controlResult) !== '22000 inbound_canonical_conversation_conflict'
    || controlConversation.automation_status !== 'enabled'
    || controlConversation.adoptions !== 0
    || totalAdoptions !== REPLIES.length + 1) {
  throw new Error(`the shared admission on another template conversation changed: ${JSON.stringify({ result: showResult(controlResult), conversation: conversationLine(controlConversation), adoptions: controlConversation.adoptions, total: totalAdoptions })}`);
}

// 11. La compra hecha con nuestro link, en lo que hizo la persona
//     (20261016000300). El bridge manda la compra con el marcador de Hermes en
//     el sck a admit_and_correlate_hotmart_checkout_issuance_v2 (app.py), que
//     marca la emision purchase_matched y no escribe la correlacion portable:
//     medido en att1-db el 2026-10-16, las 2 compras con nuestro link son las
//     unicas sin esa fila. Se compra el link del pago fallido en 521 (el
//     formulario lo guardo en 52): la lectura con el wa_id tiene que traer la
//     compra una sola vez, con la hora del evento.
const linkBuyer = reservedLinks['pago-fallido-521'];
const linkIssuance = one((await db.query(`
  select sck_value from public.checkout_link_issuances where id = $1
`, [linkBuyer.issuanceId])).rows, 'link purchase issuance');
const linkPurchaseId = `att1-chain-link-purchase-${linkBuyer.lead.email}`;
// La hora de Hotmart, dos horas antes: el caso 14 vuelve a ver a esta persona
// despues de su compra (la guarda de la compra conocida antes del plan,
// 20260808000200, frena solo una compra de los 5 minutos previos).
const linkPurchasePayload = {
  id: linkPurchaseId,
  creation_date: at(-120).getTime(),
  event: 'PURCHASE_APPROVED',
  version: '2.0.0',
  data: {
    product: { id: ATT1.productId, ucode: 'ATT1-CHAIN-UCODE' },
    buyer: { email: linkBuyer.lead.email, checkout_phone: `+${linkBuyer.lead.whatsapp}` },
    purchase: {
      approved_date: at(-120).getTime(),
      status: 'APPROVED',
      transaction: 'HPATT1CHAINLINK1',
      offer: { code: linkBuyer.lead.offer.offer_code },
      origin: { sck: linkIssuance.sck_value },
    },
  },
};
const linkPurchase = await asServiceTry(`
  select * from public.admit_and_correlate_hotmart_checkout_issuance_v2(
    $1, $2::jsonb, $3, clock_timestamp()
  )
`, [linkPurchaseId, JSON.stringify(linkPurchasePayload), linkIssuance.sck_value]);
const linkEvent = linkPurchase.ok ? linkPurchase.rows[0].webhook_event_id : null;
const linkPortableRows = one((await db.query(`
  select count(*)::integer as count from public.portable_hotmart_purchase_correlations
  where webhook_event_id = $1
`, [linkEvent])).rows, 'link purchase portable correlation').count;
const linkEventAt = linkEvent === null ? null : one((await db.query(`
  select received_at from public.webhook_events where id = $1
`, [linkEvent])).rows, 'link purchase event').received_at;
const linkActivity = await asServiceTry(`
  select activity, occurred_at
  from public.get_portable_person_activity_v1($1, $2, $3)
`, [ATT1.tenant, ATT1.funnel, linkBuyer.lead.whatsapp]);
const linkPurchases = linkActivity.ok
  ? linkActivity.rows.filter((row) => row.activity === 'purchase')
  : [];
if (!linkPurchase.ok
    || linkPurchase.rows[0].admission_outcome !== 'inserted'
    || linkPurchase.rows[0].correlation_outcome !== 'matched'
    || linkPurchase.rows[0].issuance_id !== linkBuyer.issuanceId
    || linkPurchase.rows[0].purchase_intent_id !== linkBuyer.intent
    || linkPortableRows !== 0
    || linkPurchases.length !== 1
    || linkPurchases[0].occurred_at.getTime() !== linkEventAt.getTime()
    || linkActivity.rows.at(-1).activity !== 'purchase') {
  throw new Error(`the purchase with our link is not in what the person did: ${JSON.stringify({
    purchase: linkPurchase.ok
      ? `${linkPurchase.rows[0].admission_outcome}:${linkPurchase.rows[0].correlation_outcome}`
      : `${linkPurchase.code} ${linkPurchase.message}`,
    portableRows: linkPortableRows,
    activity: linkActivity.ok ? linkActivity.rows.map((row) => row.activity) : `${linkActivity.code} ${linkActivity.message}`,
  })}`);
}

// 12. El link del producto que nombra el agente (20261016000400). Con dos
//     productos en la instancia, el agente dice de cual es el link y el
//     bridge reserva con reserve_portable_checkout_issuance_for_funnel_v1 y
//     el funnel de ese producto. Se siembra un segundo producto como lo deja
//     el manifiesto (2c-1): su binding en la misma cuenta y el mismo inbox, y
//     su catalogo dentro del scope entrante del principal, con su hotlink y
//     sus ofertas (el producto de prueba de tests/test_multi_product_manifest.py).
//     Un tercer binding activo vive en otro inbox. Sobre la conversacion del
//     primer contacto (caso 10) y la de quien compro con nuestro link (caso 11):
//     - con el funnel del principal, el link es el mismo que da la portable:
//       la oferta y la intencion del formulario;
//     - con el del segundo producto, sale su oferta por defecto y su hotlink,
//       con una intencion nueva de ese funnel, sin la del formulario;
//     - el replay del mismo mensaje con el mismo funnel devuelve la misma
//       emision; con otro funnel, replay_conflict;
//     - un funnel sin binding, o con el binding en otro inbox, da
//       blocked_funnel y no deja emision; uno que no es un slug,
//       invalid_request;
//     - quien compro el principal no recibe otro link del principal
//       (purchase_already_approved) y si el del segundo producto.
const SECOND_PRODUCT = {
  funnel: 'att2',
  productId: 8437428,
  hotlink: 'P8437428PRUEBA',
  offers: [
    { offer_code: 'prueba2a', landing_id: 'att2-prueba-a', default: true },
    { offer_code: 'prueba2b', landing_id: 'att2-prueba-b', default: false },
  ],
};
const OTHER_INBOX_FUNNEL = 'att3';
for (const [funnel, productId, hotlink, offer, inboxId] of [
  [SECOND_PRODUCT.funnel, SECOND_PRODUCT.productId, SECOND_PRODUCT.hotlink,
    SECOND_PRODUCT.offers[0], ATT1.inboxId],
  [OTHER_INBOX_FUNNEL, 8437429, 'P8437429OTRO',
    { offer_code: 'prueba3a', landing_id: 'att3-prueba-a' }, ATT1.inboxId + 1],
]) {
  await db.query(`
    insert into public.commercial_ally_runtime_bindings
      (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
       lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
       product_name, product_price, currency, offer_code, consent_copy_version,
       hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
       inbound_scope_key, inbound_scope_version, additional_offer_codes,
       additional_offer_landings)
    values ($1,$2,1,'active',$3,$4,'metodoraizana',$5,'www.metodoraizana.com',$6,$7,
            'Producto de prueba','67'::numeric,'USD',$8,$9,$10,$11,$12,$13,$14,
            '{}'::text[],'[]'::jsonb)
  `, [ATT1.tenant, funnel, ATT1.ally, ATT1.brand, offer.landing_id,
    `/${funnel}/prueba-a`, hotlink, offer.offer_code, ATT1.copyVersion, productId,
    ATT1.accountId, inboxId, ATT1.inboundScope, ATT1.inboundVersion]);
}
for (const offer of SECOND_PRODUCT.offers) {
  await db.query(`
    insert into public.checkout_offer_catalog
      (tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref,
       offer_code, checkout_base_url, checkout_mode, default_for_inbound, status,
       version, approved_by, approved_at)
    values ($1,$2,$3,$4,$5,$6,$7,$8,10,$9,'active',1,'operator-test',now())
  `, [ATT1.tenant, SECOND_PRODUCT.funnel, ATT1.inboundScope, ATT1.inboundVersion,
    SECOND_PRODUCT.hotlink, offer.landing_id, offer.offer_code,
    `https://pay.hotmart.com/${SECOND_PRODUCT.hotlink}`, offer.default]);
}

const FUNNEL_RESERVE = 'select * from public.reserve_portable_checkout_issuance_for_funnel_v1($1,$2,$3,$4,$5,$6,$7,$8,clock_timestamp())';
const reserveForFunnel = async (reply, funnel, message, ulid) => {
  const result = await asServiceTry(FUNNEL_RESERVE, [reply.caseId, reply.userId,
    ATT1.accountId, ATT1.inboxId, reply.chatwoot, String(message), ulid, funnel]);
  const row = result.ok ? result.rows[0] : null;
  const link = row?.issuance_id ? await issuanceOf(row.issuance_id) : null;
  const intentFunnel = row?.purchase_intent_id ? one((await db.query(`
    select funnel_ref from public.purchase_intents where id = $1
  `, [row.purchase_intent_id])).rows, 'funnel reserve intent').funnel_ref : null;
  return { result, row, link, intentFunnel };
};
const outcomeOf = ({ result }) => (result.ok
  ? result.rows[0].outcome : `${result.code} ${result.message}`);
const issuancesOfMessage = async (reply, message) => one((await db.query(`
  select count(*)::integer as count from public.checkout_link_issuances
  where chatwoot_conversation_id = $1 and trigger_external_message_id = $2
`, [reply.chatwoot, String(message)])).rows, 'issuances of a message').count;

const asker = reservedLinks['primer-contacto'];
const buyer = reservedLinks['pago-fallido-521'];

// El principal, por funnel: lo mismo que dio la portable en el caso 10.
replyMessage += 1;
const principalLink = await reserveForFunnel(asker, ATT1.funnel, replyMessage, ulidAt(Date.now()));
// El segundo producto, y el replay de ese mismo mensaje.
replyMessage += 1;
const secondMessage = replyMessage;
const secondUlid = ulidAt(Date.now());
const secondLink = await reserveForFunnel(asker, SECOND_PRODUCT.funnel, secondMessage, secondUlid);
const secondReplay = await reserveForFunnel(asker, SECOND_PRODUCT.funnel, secondMessage, secondUlid);
const secondReplayOtherFunnel = await reserveForFunnel(asker, ATT1.funnel, secondMessage, secondUlid);
// Sin binding, con el binding en otro inbox, y un funnel que no es un slug.
replyMessage += 1;
const unboundMessage = replyMessage;
const unbound = await reserveForFunnel(asker, 'att9', unboundMessage, ulidAt(Date.now()));
replyMessage += 1;
const otherInboxMessage = replyMessage;
const otherInbox = await reserveForFunnel(asker, OTHER_INBOX_FUNNEL, otherInboxMessage, ulidAt(Date.now()));
replyMessage += 1;
const notASlug = await reserveForFunnel(asker, 'ATT2', replyMessage, ulidAt(Date.now()));
// Quien compro el principal con nuestro link (caso 11).
replyMessage += 1;
const buyerPrincipal = await reserveForFunnel(buyer, ATT1.funnel, replyMessage, ulidAt(Date.now()));
replyMessage += 1;
const buyerSecond = await reserveForFunnel(buyer, SECOND_PRODUCT.funnel, replyMessage, ulidAt(Date.now()));

const secondUrl = secondLink.link?.checkout_url_final ?? '';
const funnelLinks = {
  principal: `${outcomeOf(principalLink)}:${principalLink.link?.offer_resolution}:${principalLink.link?.offer_code}:${principalLink.intentFunnel}`,
  second_product: `${outcomeOf(secondLink)}:${secondLink.link?.offer_resolution}:${secondLink.link?.offer_code}:${secondLink.intentFunnel}:${secondLink.link?.attribution_resolution}`,
  second_product_replay: `${outcomeOf(secondReplay)}:${secondReplay.row?.issuance_id === secondLink.row?.issuance_id ? 'same_issuance' : 'other'}`,
  replay_with_another_funnel: outcomeOf(secondReplayOtherFunnel),
  funnel_without_binding: `${outcomeOf(unbound)}:${await issuancesOfMessage(asker, unboundMessage)}`,
  binding_in_another_inbox: `${outcomeOf(otherInbox)}:${await issuancesOfMessage(asker, otherInboxMessage)}`,
  funnel_not_a_slug: outcomeOf(notASlug),
  buyer_of_the_principal: outcomeOf(buyerPrincipal),
  buyer_asks_the_second_product: `${outcomeOf(buyerSecond)}:${buyerSecond.link?.offer_code}`,
};
const expectedFunnelLinks = {
  principal: `reserved:lead_intent:${asker.lead.offer.offer_code}:${ATT1.funnel}`,
  second_product: `reserved:default_no_intent:prueba2a:${SECOND_PRODUCT.funnel}:marker_only`,
  second_product_replay: 'reserved:same_issuance',
  replay_with_another_funnel: 'replay_conflict',
  funnel_without_binding: 'blocked_funnel:0',
  binding_in_another_inbox: 'blocked_funnel:0',
  funnel_not_a_slug: 'invalid_request',
  buyer_of_the_principal: 'purchase_already_approved',
  buyer_asks_the_second_product: 'reserved:prueba2a',
};
if (JSON.stringify(funnelLinks) !== JSON.stringify(expectedFunnelLinks)
    || principalLink.row.purchase_intent_id !== asker.intent
    || !secondUrl.startsWith(`https://pay.hotmart.com/${SECOND_PRODUCT.hotlink}?off=prueba2a&`)
    || !secondUrl.includes(`&sck=hermes~v1~${secondUlid}`)
    || secondLink.row.purchase_intent_id === asker.intent) {
  throw new Error(`the link of the product the agent names: ${JSON.stringify({ ...funnelLinks, principalIntent: principalLink.row?.purchase_intent_id === asker.intent, secondUrl: secondUrl.split('?')[0] })}`);
}

// ---------------------------------------------------------------------------
// 13. La compra de otra oferta del producto antes del toque (20261019000300).
//     Hasta esa migracion la compra cortaba el caso solo si era de la misma
//     oferta (apply_hotmart_purchase_approved, 20260808000100:202), y la
//     reevaluacion miraba solo si el caso ya estaba won: con A2 y Z, quien
//     abandono una oferta y compro por otra seguia recibiendo toques. Cada
//     persona compra por otra oferta que la de su carrito. La compra entra por
//     la admision portable y su correlacion no resuelve la intencion del
//     formulario (cruza solo un dato): el freno es la identidad, no la
//     intencion. service_role ejecuta el freno (lo llama la reevaluacion,
//     security invoker, desde el bridge).
// ---------------------------------------------------------------------------
const anyOfferPurchase = (lead, offer, buyer, transaction) => ({
  id: `att1-chain-any-offer-${transaction}`,
  creation_date: at(-2).getTime(),
  event: 'PURCHASE_APPROVED',
  version: '2.0.0',
  data: {
    product: { id: ATT1.productId, ucode: 'ATT1-CHAIN-UCODE' },
    buyer: { email: buyer.email, checkout_phone: `+${buyer.phone}` },
    purchase: {
      approved_date: at(-2).getTime(),
      status: 'APPROVED',
      transaction,
      offer: { code: offer.offer_code },
    },
  },
});
const admitAnyOfferPurchase = async (label, payload, buyer) => one((await db.query(`
  select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
  JSON.stringify(payload), buyer.email, buyer.phone])).rows, `${label} purchase`);
const reevaluateAnyOffer = async (lead, plan) => {
  const worker = `att1-chain-${lead.label}`;
  const now = await dbNow();
  const claimed = (await db.query(`
    select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
  `, [worker, now])).rows;
  // Los casos anteriores pueden dejar acciones vivas: el claim tiene que
  // traer la de este carrito.
  const mine = claimed.find((row) => row.id === plan.scheduled_action_id);
  if (mine === undefined || mine.anchor_type !== 'cart_abandonment') {
    throw new Error(`${lead.label}: claimed ${JSON.stringify(claimed.map((row) => [row.id, row.anchor_type]))}`);
  }
  // Como en dispatch: la compartida corre con los permisos de la suite (aca
  // service_role no tiene las tablas del motor). El permiso de service_role
  // sobre el freno se prueba abajo.
  const decision = one((await asBridge('cart_abandonment', () => db.query(`
    select * from public.${reevaluateOperationFor('cart_abandonment')}($1,$2,$3,$4)
  `, [plan.scheduled_action_id, worker, mine.lease_generation, now]))).rows,
  `${lead.label} reevaluation`);
  const state = one((await db.query(`
    select
      (select status from public.recovery_cases where id = $1) as case_status,
      (select purchase_event_id from public.recovery_cases where id = $1) as purchase_event_id,
      (select status from public.scheduled_actions where id = $2) as action_status,
      (select terminal_reason from public.scheduled_actions where id = $2) as terminal_reason,
      (select completion_reason from public.followup_sequences where id = $3) as completion_reason,
      (select count(*)::integer from public.followup_delivery_attempts where action_id = $2) as attempts
  `, [plan.recovery_case_id, plan.scheduled_action_id, plan.followup_sequence_id])).rows,
  `${lead.label} state`);
  return { decision, state };
};
const planAnyOfferCart = async (lead, offer) => {
  await admitForm(lead, offer);
  const cart = await admitCart(lead, offer);
  await createContact(lead, cart.eventId);
  // La audiencia de produccion (caso 7): la intencion consentida, sin cohorte.
  const plan = one((await planCart(lead, offer, cart, openPlan)).rows, `${lead.label} plan`);
  if (!plan.created) throw new Error(`${lead.label}: the cart was not planned: ${JSON.stringify(plan)}`);
  return plan;
};
const anyOffer = {};
for (const [label, cartOffer, purchaseOffer, cruce] of [
  ['compra-otra-oferta-mail', offers[0], offers[1], 'email'],
  ['compra-otra-oferta-telefono', offers[1], offers[2], 'phone'],
]) {
  const lead = person(label);
  const plan = await planAnyOfferCart(lead, cartOffer);
  const stranger = person(`${label}-otro-dato`);
  const buyer = cruce === 'email'
    ? { email: lead.email, phone: stranger.phone }
    : { email: stranger.email, phone: lead.phone };
  const purchase = await admitAnyOfferPurchase(
    label, anyOfferPurchase(lead, purchaseOffer, buyer, `HPATT1ANY${cruce.toUpperCase()}`), buyer,
  );
  const { decision, state } = await reevaluateAnyOffer(lead, plan);
  if (purchase.outcome !== 'inserted'
      || decision.decision !== 'cancel'
      || decision.reason_code !== 'purchase_detected_any_offer'
      || state.case_status !== 'cancelled'
      || state.purchase_event_id !== null
      || state.action_status !== 'cancelled'
      || state.terminal_reason !== 'purchase_detected_any_offer'
      || state.completion_reason !== 'purchase_detected_any_offer'
      || state.attempts !== 0) {
    throw new Error(`${label}: the purchase of ${purchaseOffer.offer_code} did not cut the cart of ${cartOffer.offer_code}: ${JSON.stringify({ purchase: purchase.outcome, decision, state })}`);
  }
  anyOffer[cruce] = `${decision.decision}:${decision.reason_code}:${state.case_status}`;
}
// El control: la compra de otra persona, con otra oferta, no frena.
{
  const lead = person('compra-otra-oferta-control');
  const plan = await planAnyOfferCart(lead, offers[2]);
  const stranger = person('compra-otra-oferta-control-comprador');
  await admitAnyOfferPurchase('control', anyOfferPurchase(
    stranger, offers[0], stranger, 'HPATT1ANYCONTROL',
  ), stranger);
  const { decision, state } = await reevaluateAnyOffer(lead, plan);
  if (decision.decision !== 'execute'
      || decision.reason_code !== 'eligible_for_execution'
      || state.case_status !== 'active' && state.case_status !== 'grace_period') {
    throw new Error(`the purchase of another person cut the cart: ${JSON.stringify({ decision, state })}`);
  }
  anyOffer.another_person = `${decision.decision}:${decision.reason_code}`;
}
const anyOfferGrant = one((await db.query(`
  select has_function_privilege('service_role',
           'public._portable_case_purchase_stop_reason(uuid)', 'execute') as service,
         has_function_privilege('anon',
           'public._portable_case_purchase_stop_reason(uuid)', 'execute') as anon,
         has_function_privilege('authenticated',
           'public._portable_case_purchase_stop_reason(uuid)', 'execute') as authenticated
`)).rows, 'any offer stop grant');
if (!anyOfferGrant.service || anyOfferGrant.anon || anyOfferGrant.authenticated) {
  throw new Error(`the any offer stop has the wrong grants: ${JSON.stringify(anyOfferGrant)}`);
}

// ---------------------------------------------------------------------------
// 14. La compra con nuestro link, en los dos frenos portables (20261030000400,
//     E-215). La compra del caso 11 entro por la emision: no dejo la
//     correlacion portable (linkPortableRows === 0) y no gano ningun caso. Es
//     lo que midio E-212 en Maribel (conversaciones 4 y 20): sin esta
//     migracion, ninguno de los dos frenos la ve.
// ---------------------------------------------------------------------------
// La compra por la emision, como la manda el bridge, con la hora de Hotmart
// dos horas antes: quien vuelve despues de comprar. (Una compra dentro de los 5
// minutos previos al plan la frena otra guarda, la de la compra conocida
// antes del plan, 20260808000200, que no es la que falta.)
const buyWithOurLink = async (reserved, buyer, transaction) => {
  const sck = one((await db.query(`
    select sck_value from public.checkout_link_issuances where id = $1
  `, [reserved.issuanceId])).rows, `${transaction} issuance`).sck_value;
  const id = `att1-chain-link-purchase-${transaction}`;
  const result = await asServiceTry(`
    select * from public.admit_and_correlate_hotmart_checkout_issuance_v2(
      $1, $2::jsonb, $3, clock_timestamp()
    )
  `, [id, JSON.stringify({
    id,
    creation_date: at(-120).getTime(),
    event: 'PURCHASE_APPROVED',
    version: '2.0.0',
    data: {
      product: { id: ATT1.productId, ucode: 'ATT1-CHAIN-UCODE' },
      buyer: { email: buyer.email, checkout_phone: `+${buyer.phone}` },
      purchase: {
        approved_date: at(-120).getTime(),
        status: 'APPROVED',
        transaction,
        offer: { code: reserved.lead.offer.offer_code },
        origin: { sck },
      },
    },
  }), sck]);
  const portableRows = result.ok ? one((await db.query(`
    select count(*)::integer as count from public.portable_hotmart_purchase_correlations
    where webhook_event_id = $1
  `, [result.rows[0].webhook_event_id])).rows, `${transaction} portable correlation`).count : null;
  if (!result.ok || result.rows[0].correlation_outcome !== 'matched' || portableRows !== 0) {
    throw new Error(`${transaction}: the purchase with our link was not matched by the issuance alone: ${JSON.stringify({ result: result.ok ? result.rows[0] : `${result.code} ${result.message}`, portableRows })}`);
  }
  return result.rows[0];
};

// 14a. Quien compro con el link en el caso 11 vuelve a llenar el formulario:
//      el formulario solo reusa intenciones esperando, asi que nace otra. Sin
//      la tercera fuente, intent_purchased mira la intencion nueva,
//      purchase_by_identity la correlacion portable, y el primer contacto se
//      planifica.
const refill = linkBuyer.lead;
// La cohorte del primer contacto tiene cupo 2: sale quien ya compro en 9.5.
{
  const { generation } = one((await db.query(`
    select generation from public.pilot_runtime_controls where scope_key = $1
  `, [FC_SCOPE])).rows, 'first contact generation');
  const removed = one((await db.query(`
    select * from public.set_lancemos_pilot_cohort_member($1,$2,$3,$4,'removed','operator-test','controlled-test')
  `, [FC_SCOPE, FC_SCOPE_VERSION, fcBuyer.contact, generation])).rows, 'first contact buyer removal');
  if (removed.member_status !== 'removed') {
    throw new Error(`the first contact buyer did not leave the cohort: ${JSON.stringify(removed)}`);
  }
}
await enrollFirstContact(refill);
const refillPlan = await submitFirstContact(refill, FC_DUE);
const refillIntent = await intentOf(refillPlan.intent);
if (refillPlan.intent === linkBuyer.intent
    || refillIntent.lifecycle_state !== 'waiting_for_purchase'
    || (await intentOf(linkBuyer.intent)).lifecycle_state !== 'purchased'
    || refillPlan.result !== 'not_planned:purchase_by_identity'
    || refillPlan.recovery_case_id !== null) {
  throw new Error(`the buyer with our link who fills the form again gets the first contact: ${JSON.stringify({ result: refillPlan.result, newIntent: refillPlan.intent !== linkBuyer.intent, state: refillIntent.lifecycle_state })}`);
}

// 14b. Pago el link de otro: el link es el del carrito en 549 (caso 10) y el
//      comprador que informa Hotmart es otra persona, con otro mail y otro
//      telefono. Despues esa persona llena el formulario y abandona un
//      carrito: la intencion purchased es la del dueno del link (no cruza) y
//      la compra no tiene correlacion portable ni caso ganado. El freno lo
//      tiene que ver por el comprador del evento.
const payerLink = reservedLinks['carrito-549'];
const payer = person('pago-el-link-de-otro');
await buyWithOurLink(payerLink, payer, 'HPATT1CHAINLINK3');
const payerPlan = await planAnyOfferCart(payer, offers[1]);
const payerRun = await reevaluateAnyOffer(payer, payerPlan);
if (payerRun.decision.decision !== 'cancel'
    || payerRun.decision.reason_code !== 'purchase_detected_any_offer'
    || payerRun.state.case_status !== 'cancelled'
    || payerRun.state.purchase_event_id !== null
    || payerRun.state.action_status !== 'cancelled'
    || payerRun.state.terminal_reason !== 'purchase_detected_any_offer'
    || payerRun.state.attempts !== 0) {
  throw new Error(`the cart of who paid our link did not stop before the touch: ${JSON.stringify(payerRun)}`);
}

// 14c. La tercera fuente sola, con sus controles. La ejecuta solo el duenio
//      (la llaman los dos frenos); ningun rol de la API.
const linkPurchaseSeenIn = async (tenant, funnel, contact, email, phone,
  fn = '_portable_checkout_link_purchase_exists') => one((await db.query(`
  select public.${fn}($1,$2,$3,$4,$5) as seen
`, [tenant, funnel, contact, email, phone])).rows, 'link purchase stop').seen;
const linkPurchaseSeen = (funnel, contact, email, phone) => linkPurchaseSeenIn(
  ATT1.tenant, funnel, contact, email, phone);
// D2, aislada: el duenio del link de 549, que pago otra persona (14b), sin
// ningun dato suyo que cruce al comprador ni a la intencion (se le sacan los
// contact_points dentro de una transaccion que se deshace). Lo frena solo la
// rama del contacto de la emision: la misma funcion sin esa rama da false.
// D2 es una decision abierta de Dan: si cambia, cambia esta asercion.
const ownerOfLinkPaidByOther = {};
await db.exec('begin');
try {
  await db.query('delete from public.contact_points where contact_id = $1', [payerLink.lead.contact]);
  const definition = one((await db.query(`
    select pg_get_functiondef(
      'public._portable_checkout_link_purchase_exists(text,text,uuid,text,text)'::regprocedure
    ) as definition
  `)).rows, 'link purchase stop definition').definition;
  const branch = '              issuance.contact_id = p_contact_id\n              or ';
  if (definition.split(branch).length !== 2) {
    throw new Error('the contact branch of the link purchase stop is not where the mutation expects it');
  }
  await db.exec(definition
    .replace('public._portable_checkout_link_purchase_exists(', 'public._link_purchase_without_contact_branch(')
    .replace(branch, '              '));
  ownerOfLinkPaidByOther.with_the_branch = await linkPurchaseSeenIn(ATT1.tenant, ATT1.funnel,
    payerLink.lead.contact, null, null);
  ownerOfLinkPaidByOther.without_the_branch = await linkPurchaseSeenIn(ATT1.tenant, ATT1.funnel,
    payerLink.lead.contact, null, null, '_link_purchase_without_contact_branch');
} finally {
  await db.exec('rollback');
}
if (ownerOfLinkPaidByOther.with_the_branch !== true
    || ownerOfLinkPaidByOther.without_the_branch !== false) {
  throw new Error(`D2: the owner of a link paid by another person: ${JSON.stringify(ownerOfLinkPaidByOther)}`);
}
const linkStop = {
  buyer_by_contact: await linkPurchaseSeen(ATT1.funnel, linkBuyer.lead.contact, null, null),
  buyer_by_email: await linkPurchaseSeen(ATT1.funnel, null, linkBuyer.lead.email, null),
  buyer_by_form_phone: await linkPurchaseSeen(ATT1.funnel, null, null, linkBuyer.lead.formPhone),
  payer_by_phone: await linkPurchaseSeen(ATT1.funnel, null, null, payer.phone),
  // El filtro por funnel: el comprador del caso 11, en el funnel del segundo
  // producto (donde su link quedo reservado y nadie lo compro).
  buyer_in_another_funnel: await linkPurchaseSeen(SECOND_PRODUCT.funnel,
    linkBuyer.lead.contact, linkBuyer.lead.email, linkBuyer.lead.whatsapp),
  // El filtro por tenant: el mismo comprador y el mismo funnel, en otro tenant.
  buyer_in_another_tenant: await linkPurchaseSeenIn('otra-aliada', ATT1.funnel,
    linkBuyer.lead.contact, linkBuyer.lead.email, linkBuyer.lead.whatsapp),
  // D2: el duenio del link que pago otra persona, con sus datos.
  owner_of_link_paid_by_other: await linkPurchaseSeen(ATT1.funnel, payerLink.lead.contact,
    payerLink.lead.email, payerLink.lead.whatsapp),
  // Quien pidio el link en el primer contacto (caso 10) y no compro.
  link_without_purchase: await linkPurchaseSeen(ATT1.funnel, asker.lead.contact,
    asker.lead.email, asker.lead.whatsapp),
  another_person: await linkPurchaseSeen(ATT1.funnel, null, 'nadie@example.test', '12025550999'),
};
const expectedLinkStop = {
  buyer_by_contact: true,
  buyer_by_email: true,
  buyer_by_form_phone: true,
  payer_by_phone: true,
  buyer_in_another_funnel: false,
  buyer_in_another_tenant: false,
  owner_of_link_paid_by_other: true,
  link_without_purchase: false,
  another_person: false,
};
const linkStopGrant = one((await db.query(`
  select has_function_privilege('service_role', $1, 'execute') as service,
         has_function_privilege('anon', $1, 'execute') as anon,
         has_function_privilege('authenticated', $1, 'execute') as authenticated
`, ['public._portable_checkout_link_purchase_exists(text,text,uuid,text,text)'])).rows,
'link purchase stop grant');
// El indice parcial de las emisiones compradas (E-217, H2).
const linkStopIndex = one((await db.query(`
  select count(*)::integer as count from pg_indexes
  where schemaname = 'public' and tablename = 'checkout_link_issuances'
    and indexname = 'checkout_link_issuances_purchase_matched_idx'
    and indexdef like '%(purchase_intent_id) WHERE (status = ''purchase_matched''::text)'
`)).rows, 'link purchase stop index').count;
if (linkStopIndex !== 1) throw new Error(`the purchase_matched partial index is missing: ${linkStopIndex}`);
// Correrla dos veces aborta con 55000 sin cambiar nada.
const linkStopMigration = readFileSync(join(root,
  'supabase/migrations/20261030000400_portable_purchase_stops_see_our_link_v1.sql'), 'utf8');
let linkStopTwice = 'applied twice';
try {
  await db.exec(linkStopMigration);
} catch (caught) {
  linkStopTwice = `${caught.code} ${caught.message}`;
  await db.exec('rollback');
}
if (linkStopTwice !== '55000 portable_checkout_link_purchase_stop_already_applied'
    || JSON.stringify(linkStop) !== JSON.stringify(expectedLinkStop)
    || linkStopGrant.service || linkStopGrant.anon || linkStopGrant.authenticated) {
  throw new Error(`the link purchase stop: ${JSON.stringify({ linkStop, linkStopGrant, linkStopTwice })}`);
}

console.log(JSON.stringify({
  att1_portable_chain: 'OK',
  empty_cohort: 'pilot_scope_rejected:pilot_contact_not_in_cohort',
  carts: cartResults,
  payment_failure_without_cart: failureOnlyRun.decision.reason_code,
  payment_failure_after_cart: bothRun.decision.reason_code,
  pilot_request_starts: starts.count,
  two_message_policy_risk: 'invalid_next_policy_step',
  hotmart_in_whatsapp_form: {
    mx_payment_failure_521_against_intent_52: `${mxFailureGrants[0].evidence.phone_match}:${mxFailureRun.decision.reason_code}`,
    ar_cart_549_against_intent_54: `consented_intent:${arCartRun.decision.reason_code}`,
    mx_purchase_521_against_intent_52: 'purchased',
  },
  first_contact: {
    scope: `${FC_SCOPE} v${FC_SCOPE_VERSION} (${fcScope.audience_mode})`,
    disarmed: fcDisarmed.result,
    planned: fcPlan.result,
    grace_minutes: FC_GRACE_MINUTES,
    reevaluation: `${fcRun.reevaluateOperation}:${fcRun.decision.reason_code}`,
    request_start: fcRun.startOperation,
    purchase_before_the_send: `${fcBuyerDecision.decision}:${fcBuyerDecision.reason_code}`,
    pilot_request_starts: await fcStarts(),
  },
  consented_intent: {
    scope_version: OPEN_VERSION,
    consented_without_cohort: openRun.decision.reason_code,
    opt_out_before_send: `${optedOutState.action_status}, starts ${optedOutState.starts}, active allowed ${optedOutState.active_allowed}`,
    without_form: 'pilot_scope_rejected:pilot_audience_intent_unresolved',
    form_without_opt_in: 'pilot_scope_rejected:pilot_audience_consented_intent_not_authorized',
    cap: `pilot_request_start_rejected:${cappedError.detail}`,
    pilot_request_starts: finalStarts,
  },
  template_reply: {
    ...templateReplies,
    opt_out_of_another_lead: `${showResult(leavingAdmitted)}, opt-out ${leavingOptOut.rows[0].outcome}`,
    shared_admission_control: showResult(controlResult),
    adoption_events: totalAdoptions,
  },
  link_purchase_in_person_activity: {
    purchase: `${linkPurchase.rows[0].admission_outcome}:${linkPurchase.rows[0].correlation_outcome}`,
    portable_correlation_rows: linkPortableRows,
    activity: linkActivity.rows.map((row) => row.activity).join(','),
  },
  link_for_funnel: funnelLinks,
  purchase_of_another_offer: anyOffer,
  purchase_with_our_link_stops: {
    form_again: refillPlan.result,
    cart_of_who_paid_the_link: `${payerRun.decision.decision}:${payerRun.decision.reason_code}:${payerRun.state.case_status}`,
    third_source: linkStop,
    owner_of_link_paid_by_other_isolated: ownerOfLinkPaidByOther,
    applied_twice: linkStopTwice,
  },
}));
await db.close();
