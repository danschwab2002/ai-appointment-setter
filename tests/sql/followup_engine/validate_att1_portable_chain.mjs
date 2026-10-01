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
  return { decision, context, started, reevaluateOperation, startOperation };
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
    throw new Error(`${lead.label} was not enrolled in the first contact scope`);
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
// 9.4b LIMITE CONOCIDO, fijado aca para que no pase inadvertido: quien
//      responde a la plantilla NO pasa la admision entrante. La aceptacion deja
//      la conversacion del caso en automation_status = 'enabled'
//      (record_and_finalize_followup_acceptance) y la admision entrante solo
//      toma una conversacion 'draft_only': rechaza con 22000
//      inbound_canonical_conversation_conflict. Vale igual para carrito y pago
//      fallido. Adoptar esa conversacion para el agente entrante es una
//      decision de diseno pendiente; el dia que se resuelva, este bloque falla
//      y hay que cambiarlo por la cadena completa (respuesta, enlace,
//      derivacion). Lo que SI funciona sobre esa conversacion es el opt-out
//      durable, que el bridge aplica aunque la admision rechace.
{
  await db.query(`
    insert into public.inbound_commercial_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
       external_product_id, offer_code, approved_by, approved_at, published_at)
    values ($1,$2,'published',$3,$4,$5,$6,$7,'operator-test',now(),now())
  `, [ATT1.inboundScope, ATT1.inboundVersion, ATT1.tenant, ATT1.accountId, ATT1.inboxId,
    String(ATT1.productId), fcLead.offer.offer_code]);
  // La conversacion de Chatwoot en la que salio la plantilla (la que registro
  // la aceptacion) y la identidad que resuelve el entrante del bridge: la que
  // creo el plan, con el telefono del formulario (52 + 10).
  const replyConversation = 900100 + messageNumber;
  const fcConversation = one((await db.query(`
    select conversation.automation_status, conversation.status, identity.external_user_id
    from public.recovery_cases recovery
    join public.conversations conversation on conversation.id = recovery.conversation_id
    join public.channel_identities identity on identity.id = conversation.channel_identity_id
    where recovery.id = $1
      and conversation.commercial_context ->> 'chatwoot_conversation_id' = $2
  `, [fcPlan.recovery_case_id, String(replyConversation)])).rows, 'first contact conversation');
  let replyError = null;
  try {
    await db.query('select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)',
      [ATT1.inboundScope, ATT1.inboundVersion, replyConversation, fcLead.formPhone]);
  } catch (caught) {
    replyError = caught;
  }
  const replyOptOut = one((await db.query(`
    select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,$6,'stop_receiving_messages')
  `, [ATT1.accountId, ATT1.inboxId, replyConversation, 990100 + messageNumber,
    fcLead.formPhone, await dbNow()])).rows, 'opt-out on the template conversation');
  const fcContactAfter = one((await db.query(`
    select contact_permission from public.contacts where id = $1
  `, [fcLead.contact])).rows, 'first contact contact after the opt-out');
  if (fcConversation.automation_status !== 'enabled'
      || fcConversation.external_user_id !== fcLead.formPhone
      || replyError?.code !== '22000'
      || replyError?.message !== 'inbound_canonical_conversation_conflict'
      || replyOptOut.outcome !== 'applied'
      || fcContactAfter.contact_permission !== 'opted_out') {
    throw new Error(`the reply to the template is no longer what the known limit says: ${JSON.stringify({ conversation: fcConversation.automation_status, reply: [replyError?.code, replyError?.message], optOut: replyOptOut.outcome, permission: fcContactAfter.contact_permission })}`);
  }
}
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
    known_limit_reply_to_the_template: 'inbound admission 22000 inbound_canonical_conversation_conflict; opt-out applied',
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
}));
await db.close();
