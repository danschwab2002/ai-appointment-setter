// El primer contacto portable tras el formulario (migracion 20261001000200),
// con las RPC reales, la reevaluacion REAL y el arranque real.
//
// Recorre, sobre la cadena completa de migraciones:
//   formulario (admit_and_plan_portable_lead_precheckout) -> contacto, ancla,
//   caso, accion, identidad, permiso y binding del piloto -> claim ->
//   reevaluate_portable_precheckout_action (que delega en
//   reevaluate_followup_action; nada de followup_action_reevaluated insertado
//   a mano) -> contexto de ejecucion -> reserva approved_template ->
//   mark_portable_precheckout_request_started ->
//   record_and_finalize_followup_acceptance.
//
// Casos:
//   0. la migracion cambia exactamente lo que dice: ocho funciones nuevas,
//      ninguna reemplazada, ningun grant distinto, las que ejecuta Johanna
//      identicas (pg_get_functiondef), y los tres checks aceptan lo que
//      aceptaban mas un valor; el check de recovery_cases.source se quita por
//      definicion, no por nombre (con otro nombre lo encuentra; con cero o con
//      dos aborta);
//   1. el E2E de la instancia, con su scope (consented_intent_in_cohort): con
//      el scope desarmado no se planifica ni se crea nada; armado y sin
//      contacto en la cohorte, tampoco; con el contacto sembrado e inscripto,
//      el reenvio planifica y llega a la aceptacion; y la compra antes de la
//      demora cancela el caso (cancelled, no won) sin consumir cupo;
//   2. un scope abierto (consented_intent): la demora (due_at = submitted_at
//      del envio + gracia; no sale antes), dos entregas del mismo envio, un
//      solo primer contacto vivo por persona, un segundo envio de la misma
//      intencion con el primer caso cerrado (ancla nueva, due_at del envio
//      nuevo) y el toque aceptado que frena 24 h;
//   3. quien escribio primero por WhatsApp y despues deja el formulario: se
//      reutilizan el contacto y la identidad (521... con formulario 52...) y
//      se completan telefono, nombre y email;
//   4. opt-out: previo y unmatched en la otra forma, no planifica; entre el
//      plan y el envio, el arranque lo rechaza; aplicado al contacto, cierra
//      el caso;
//  4b. derivacion a una persona: con la conversacion del contacto derivada
//      (paused_human) el formulario no planifica; derivada entre el plan y el
//      envio, la reevaluacion cancela; y entre la reevaluacion y el arranque,
//      el arranque la rechaza sin consumir cupo;
//   5. compra: resuelta, ambigua antes de la reevaluacion, ambigua entre la
//      reevaluacion y el arranque, intencion de mas de 7 dias reenviada (el
//      due_at es el del reenvio) y comprada (frena por identidad), y compra
//      previa al formulario;
//   6. carrito o pago fallido que lo reemplazan; el flujo de Hotmart sigue
//      planificando para la misma persona, y con su caso abierto el formulario
//      no planifica;
//   7. consentimiento perdido (authorization lost) e intencion que deja de
//      estar viva;
//   8. el scope: manual_cohort rechazado, sin publicar, de otra fuente, y
//      parametros invalidos;
//   9. los triggers diferidos corren adentro del bloque: una compra conocida
//      cierra el caso al nacer sin que la admision falle, y un error de un
//      trigger diferido deja plan_failed con la admision inserted;
//  10. un error del planificador no tumba la admision: identidad de otro
//      inbox (23514), plan_failed y nada creado;
//  11. envio sin opt-in, F1 (telefono del contacto) y contacto ambiguo;
//  12. una accion de otra ancla no pasa por estas funciones, ni esta por las
//      de las otras;
//  13. privacidad y ACL: el renglon del plan y el ancla llevan solo ids;
//      service_role ejecuta los cuatro entrypoints y nada mas;
//  14. el inventario de esquema reconoce la migracion.
//
// Datos: binding, ofertas, landings, producto, Chatwoot y consentimiento de
// tests/fixtures/instances/att1/instancia.toml; la politica y el scope del
// primer contacto, y los de la recuperacion, de
// tests/fixtures/instances/att1/politica-piloto.json (first_contact). El scope
// abierto y el manual_cohort son derivados del de la instancia: cambian la
// clave, el modo y los topes.
// DESVIACION DOCUMENTADA (la misma de validate_att1_portable_chain.mjs): la
// ventana de envio usa los dias del fixture pero de 00:00 a 23:59, porque la
// puerta de arranque exige p_now a +-5 minutos del reloj de la base. Por lo
// mismo, la accion que llega a enviarse nace de un formulario fechado 61
// minutos antes: la demora de 60 se prueba aparte, con el reloj del claim.
// Formulario: los goldens del traductor de GHL (tests/fixtures/ghl/expected/),
// que salen de los dos envios capturados, con id, fecha y comprador (email y
// telefono) sustituidos. Variantes derivadas, no capturas: el envio 1.0.0 sin
// opt-in (el bloque consent del precedente de validate_att1_portable_chain.mjs
// sobre el golden) y la persona que envia por la otra landing (el golden de esa
// landing con su pais y su telefono). Los telefonos son sinteticos.
// Carrito: la captura tests/fixtures/hotmart_cart_abandonment_rejected_v1.json
// con producto, oferta, id, fecha y comprador sustituidos. Deuda: no hay
// PURCHASE_CANCELED ni PURCHASE_APPROVED capturados; el pago fallido usa el
// precedente inline de validate_commercial_ally_payment_failure_recovery.mjs y
// la compra el de validate_commercial_ally_multi_offer.mjs, con los valores de
// ATT1. El texto aceptado es un marcador: la base guarda el que le pasa el
// bridge.
// Derivacion: la politica de proyeccion att1-derivacion-entrante con el equipo
// del manifiesto (chatwoot.equipo_derivacion), como la deja aprovisionar-att1
// (leida en la base de ATT1 el 2026-10-01); el texto de la nota es un marcador.
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

const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};
const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);
const results = {};

// ---------------------------------------------------------------------------
// 0. La migracion cambia exactamente lo que dice.
// ---------------------------------------------------------------------------
const TARGET = '20261001000200_portable_precheckout_first_contact.sql';
const migrationNames = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const targetIndex = migrationNames.indexOf(TARGET);
if (targetIndex < 0) throw new Error(`${TARGET} is missing from the canonical stack`);
const apply = async (file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
));
const snapshot = async () => new Map((await db.query(`
  select p.oid::regprocedure::text as signature,
         pg_get_functiondef(p.oid) as definition,
         p.prosecdef as security_definer,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const CHECKS = [
  ['recovery_cases', 'recovery_cases_source_check'],
  ['recovery_case_events', 'recovery_case_events_event_role_check'],
  ['followup_sequences', 'followup_sequences_reason_check'],
];
const checkValues = async () => Object.fromEntries((await db.query(`
  select conname,
         array(
           select match[1]
           from regexp_matches(pg_get_constraintdef(oid), '''([a-z_]+)''::text', 'g') match
           order by 1
         ) as accepted
  from pg_constraint
  where conname = any($1::text[]) and contype = 'c'
`, [CHECKS.map(([, name]) => name)])).rows.map((row) => [row.conname, row.accepted]));

await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of migrationNames.slice(0, targetIndex)) {
  await apply(join(root, 'supabase/migrations', name));
}
const before = await snapshot();
const checksBefore = await checkValues();
await apply(join(root, 'supabase/migrations', TARGET));
const after = await snapshot();
const checksAfter = await checkValues();
for (const name of migrationNames.slice(targetIndex + 1)) {
  await apply(join(root, 'supabase/migrations', name));
}

const TS = 'timestamp with time zone';
const ENTRYPOINTS = [
  'admit_and_plan_portable_lead_precheckout(text,text,integer,text,jsonb,jsonb,text,integer)',
  `reevaluate_portable_precheckout_action(uuid,text,bigint,${TS},boolean,text,text,${TS},text,boolean,boolean,boolean,boolean,boolean)`,
  `mark_portable_precheckout_request_started(uuid,uuid,text,bigint,${TS})`,
  'get_portable_precheckout_pilot_runtime_status(text,integer,text,text,text)',
];
const PRIVATE_HELPERS = {
  '_portable_precheckout_stop_reason(uuid,uuid)': false,
  '_find_portable_precheckout_contact(uuid)': false,
  '_ensure_portable_precheckout_contact(uuid,uuid)': true,
  '_plan_portable_precheckout_first_contact(uuid,uuid,uuid,text,integer)': true,
};
// Lo que ejecuta Johanna (sin manifiesto, con el piloto y los portable_*
// apagados) y lo que el primer contacto comparte sin redefinir.
const UNTOUCHED = [
  'correlate_hotmart_purchase_intent(uuid)',
  '_admit_hotmart_purchase_intent_identity(uuid,text,text)',
  'admit_and_correlate_hotmart_cart_abandonment(text,jsonb,text,text)',
  'admit_and_correlate_hotmart_purchase_approved(text,jsonb,text,text)',
  'admit_johanna_hotmart_cart_abandonment(text,jsonb,text,text)',
  'admit_johanna_payment_failure(text,jsonb,text,text)',
  'admit_observed_lead_precheckout(text,jsonb,jsonb)',
  'admit_precheckout_form_submission(text,jsonb,jsonb)',
  `apply_chatwoot_inbound_opt_out(bigint,bigint,bigint,bigint,text,${TS},text)`,
  `apply_hotmart_purchase_approved(uuid,text,text,text,text,text,${TS})`,
  `claim_due_followup_actions(text,${TS},interval,integer)`,
  `mark_followup_request_started(uuid,uuid,text,bigint,${TS})`,
  `reevaluate_followup_action(uuid,text,bigint,${TS},boolean,text,text,${TS},text,boolean,boolean,boolean,boolean,boolean)`,
  `reserve_followup_delivery_attempt(uuid,text,bigint,bigint,bigint,text,text,${TS})`,
  `record_and_finalize_followup_acceptance(uuid,uuid,text,bigint,text,text,text,${TS})`,
  `get_followup_execution_context(uuid,text,bigint,${TS})`,
  'stop_cart_recovery_for_known_purchase()',
  'fail_closed_ambiguous_known_purchase()',
  // Del runtime portable y del piloto: se llaman, no se reemplazan.
  'admit_portable_observed_lead_precheckout(text,text,integer,text,jsonb,jsonb)',
  '_portable_consented_intent_reason(uuid,uuid,text)',
  '_lancemos_pilot_audience_intent(text,integer,uuid,text,uuid,text)',
  'evaluate_lancemos_pilot_scope(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid)',
  `authorize_lancemos_pilot_request_start(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid,uuid,uuid,${TS})`,
  `mark_lancemos_pilot_request_started(uuid,uuid,text,bigint,${TS})`,
  `mark_portable_payment_failure_request_started(uuid,uuid,text,bigint,${TS})`,
  'get_lancemos_pilot_runtime_status(text,integer,text,text,text)',
  `plan_lancemos_pilot_cart_recovery(uuid,uuid,text,text,text,text,integer,${TS},bigint,bigint,text,text,integer)`,
  `plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,${TS},bigint,bigint,text,text,integer)`,
];
const sameSet = (left, right) => same([...left].sort(), [...right].sort());
const added = [...after.keys()].filter((signature) => !before.has(signature));
const removed = [...before.keys()].filter((signature) => !after.has(signature));
const changed = [...before.keys()].filter((signature) => after.has(signature)
  && after.get(signature).definition !== before.get(signature).definition);
const aclChanged = [...before.keys()].filter((signature) => after.has(signature)
  && (after.get(signature).service_x !== before.get(signature).service_x
      || after.get(signature).api_x !== before.get(signature).api_x));
if (!sameSet(added, [...ENTRYPOINTS, ...Object.keys(PRIVATE_HELPERS)])
    || removed.length !== 0 || changed.length !== 0 || aclChanged.length !== 0) {
  throw new Error(`the migration changed something else: ${JSON.stringify({ added, removed, changed, aclChanged })}`);
}
for (const signature of UNTOUCHED) {
  if (!before.has(signature)
      || after.get(signature)?.definition !== before.get(signature).definition) {
    throw new Error(`a function the first contact must not touch changed or is missing: ${signature}`);
  }
}
for (const signature of ENTRYPOINTS) {
  const row = after.get(signature);
  if (row.service_x !== true || row.api_x !== false || row.security_definer !== true) {
    throw new Error(`entrypoint ACL: ${JSON.stringify({ signature, ...row, definition: undefined })}`);
  }
}
for (const [signature, definer] of Object.entries(PRIVATE_HELPERS)) {
  const row = after.get(signature);
  if (row.service_x !== false || row.api_x !== false || row.security_definer !== definer) {
    throw new Error(`a private helper is executable by an API role: ${JSON.stringify({ signature, service_x: row.service_x, api_x: row.api_x })}`);
  }
}
// Los tres checks: lo de antes, mas un valor.
const ADDED_CHECK_VALUES = {
  recovery_cases_source_check: 'landing',
  recovery_case_events_event_role_check: 'precheckout_intent',
  followup_sequences_reason_check: 'precheckout_intent',
};
for (const [, name] of CHECKS) {
  const expected = [...checksBefore[name], ADDED_CHECK_VALUES[name]].sort();
  if (!Array.isArray(checksBefore[name]) || checksBefore[name].length === 0
      || checksBefore[name].includes(ADDED_CHECK_VALUES[name])
      || !same(checksAfter[name], expected)) {
    throw new Error(`${name} did not only gain ${ADDED_CHECK_VALUES[name]}: ${JSON.stringify({ before: checksBefore[name], after: checksAfter[name] })}`);
  }
}
// El check de recovery_cases.source se quita por definicion. Nacio inline y
// sin nombre, asi que una base que no salio del baseline puede tenerlo con
// otro: se corre el bloque de la migracion (el do y el add constraint que le
// sigue) sobre tres estados armados adentro de una transaccion que se deshace.
const sourceCheckStatements = readFileSync(join(root, 'supabase/migrations', TARGET), 'utf8')
  .match(/do \$source_check\$\n[\s\S]*?\n\$source_check\$;\nalter table public\.recovery_cases\n    add constraint recovery_cases_source_check\n[^;]*;/);
if (sourceCheckStatements === null) {
  throw new Error('the migration does not drop the source check through the $source_check$ block');
}
const OLD_SOURCE_CHECK = "check (source = any (array['hotmart', 'simulator']))";
const sourceChecks = async () => (await db.query(`
  select conname, pg_get_constraintdef(oid) as definition
  from pg_constraint
  where conrelid = 'public.recovery_cases'::regclass and contype = 'c'
    and pg_get_constraintdef(oid) like '%simulator%'
  order by conname
`)).rows;
const withSourceChecks = async (names) => {
  await db.exec('begin');
  try {
    await db.exec('alter table public.recovery_cases drop constraint recovery_cases_source_check');
    for (const name of names) {
      await db.exec(`alter table public.recovery_cases add constraint ${name} ${OLD_SOURCE_CHECK}`);
    }
    let error = null;
    try {
      await db.exec(sourceCheckStatements[0]);
    } catch (caught) {
      error = caught;
    }
    return { error, checks: error === null ? await sourceChecks() : null };
  } finally {
    await db.exec('rollback');
  }
};
const renamed = await withSourceChecks(['recovery_cases_origen_valido']);
const missing = await withSourceChecks([]);
const doubled = await withSourceChecks(['recovery_cases_origen_valido', 'recovery_cases_origen_repetido']);
const restored = await sourceChecks();
if (renamed.error !== null
    || renamed.checks.length !== 1
    || renamed.checks[0].conname !== 'recovery_cases_source_check'
    || !renamed.checks[0].definition.includes("'landing'")
    || missing.error?.code !== '55000'
    || missing.error?.message !== 'recovery_cases_source_check_not_found'
    || missing.error?.detail !== '0'
    || doubled.error?.code !== '55000'
    || doubled.error?.message !== 'recovery_cases_source_check_not_found'
    || doubled.error?.detail !== '2'
    || restored.length !== 1 || restored[0].conname !== 'recovery_cases_source_check'
    || !restored[0].definition.includes("'landing'")) {
  throw new Error(`the source check is not dropped by definition: ${JSON.stringify({ renamed: renamed.error?.message ?? renamed.checks, missing: [missing.error?.message, missing.error?.detail], doubled: [doubled.error?.message, doubled.error?.detail], restored })}`);
}
results.migration_scope = {
  new_functions: added.length,
  replaced: changed.length,
  untouched_identical: UNTOUCHED.length,
  checks_gained_one_value: CHECKS.length,
  source_check_dropped_by_definition: 'another name found; zero or two abort with 55000',
};

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
const recoveryScope = required(PILOT.pilot_scope, 'politica-piloto.pilot_scope');
const recoveryPolicy = required(PILOT.policy, 'politica-piloto.policy');
const fcScope = required(PILOT.first_contact?.pilot_scope, 'politica-piloto.first_contact.pilot_scope');
const fcPolicy = required(PILOT.first_contact?.policy, 'politica-piloto.first_contact.policy');
const CHANNEL_PROVIDER = required(fcScope.channel_provider, 'first_contact.pilot_scope.channel_provider');
const CHANNEL_REF = `${required(fcScope.channel_account_ref_prefix, 'first_contact.pilot_scope.channel_account_ref_prefix')}${ATT1.inboxId}`;
const GRACE_MINUTES = 60;
if (CHANNEL_PROVIDER !== 'waba'
    || fcScope.source !== 'landing'
    || fcScope.source_event_type !== 'PRECHECKOUT_FORM_SUBMITTED'
    || fcScope.audience_mode !== 'consented_intent_in_cohort'
    || fcScope.max_cohort_contacts !== 2
    || fcPolicy.grace_period !== `${GRACE_MINUTES} minutes`
    || fcPolicy.expires_after !== '1 day'
    || fcPolicy.max_automatic_messages !== 1
    || fcPolicy.steps.map((step) => `${step.step_key}:${step.mode}`).join(',') !== 'first_contact:freeform'
    || recoveryPolicy.max_automatic_messages !== 1) {
  // Los casos de abajo (la demora, el E2E en cohorte con dos personas, un
  // toque) asumen esta forma. Si la instancia la cambia, hay que revisarlos.
  throw new Error(`politica-piloto.json changed shape: ${JSON.stringify({ fcScope, fcPolicy })}`);
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
await db.query(`
  insert into public.commercial_ally_hotmart_purchase_policies
    (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
  values ($1,$2,$3,true,$4::interval)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion,
  required(PILOT.purchase_policy?.max_lookback, 'purchase_policy.max_lookback')]);
// El scope entrante de la instancia (quien escribe primero por WhatsApp) y uno
// de otro inbox de la misma cuenta.
const OTHER_INBOX = { key: 'att1-otro-inbox', version: 1, inboxId: ATT1.inboxId + 88 };
for (const [key, version, inboxId] of [
  [ATT1.inboundScope, ATT1.inboundVersion, ATT1.inboxId],
  [OTHER_INBOX.key, OTHER_INBOX.version, OTHER_INBOX.inboxId],
]) {
  await db.query(`
    insert into public.inbound_commercial_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
       external_product_id, offer_code, approved_by, approved_at, published_at)
    values ($1,$2,'published',$3,$4,$5,$6,$7,'operator-test',now(),now())
  `, [key, version, ATT1.tenant, ATT1.accountId, inboxId, String(ATT1.productId),
    defaultOffer.offer_code]);
}

const allDay = (windows) => JSON.stringify(windows.map((window) => (
  { ...window, start: '00:00', end: '23:59' })));
for (const policy of [fcPolicy, recoveryPolicy]) {
  await db.query(`
    insert into public.followup_policy_versions
      (policy_key, version, status, purpose, timezone, business_windows,
       grace_period, expires_after, max_automatic_messages, steps,
       approved_by, approved_at, published_at)
    values ($1,$2,'published',$3,$4,$5::jsonb,$6::interval,$7::interval,$8,$9::jsonb,
            'operator-test',now(),now())
  `, [required(policy.policy_key, 'policy.policy_key'), required(policy.version, 'policy.version'),
    required(policy.purpose, 'policy.purpose'), ATT1.timezone,
    allDay(required(policy.business_windows, 'policy.business_windows')),
    required(policy.grace_period, 'policy.grace_period'),
    required(policy.expires_after, 'policy.expires_after'),
    policy.max_automatic_messages, JSON.stringify(required(policy.steps, 'policy.steps'))]);
}

// El scope de la instancia, y tres derivados: uno abierto (consented_intent,
// el modo de produccion), uno manual_cohort y uno de recuperacion de Hotmart
// abierto, para el flujo que reemplaza al primer contacto.
const FIXTURE_SCOPE = {
  key: required(fcScope.scope_key, 'first_contact.pilot_scope.scope_key'),
  version: required(fcScope.version, 'first_contact.pilot_scope.version'),
  mode: fcScope.audience_mode,
  source: fcScope.source,
  eventType: fcScope.source_event_type,
  additionalEventTypes: required(fcScope.additional_source_event_types, 'first_contact.pilot_scope.additional_source_event_types'),
  policy: fcPolicy,
  cohort: fcScope.max_cohort_contacts,
  total: required(fcScope.max_outbound_request_starts_total, 'first_contact.pilot_scope.max_outbound_request_starts_total'),
  perDay: required(fcScope.max_outbound_request_starts_per_day, 'first_contact.pilot_scope.max_outbound_request_starts_per_day'),
};
const OPEN_SCOPE = {
  ...FIXTURE_SCOPE, key: 'att1-primer-contacto-abierto', mode: 'consented_intent',
  cohort: 5, total: 60, perDay: 60,
};
const MANUAL_SCOPE = {
  ...FIXTURE_SCOPE, key: 'att1-primer-contacto-manual', mode: 'manual_cohort',
  cohort: 5, total: 5, perDay: 5,
};
const HOTMART_SCOPE = {
  key: 'att1-primer-contacto-recuperacion', version: 1, mode: 'consented_intent',
  source: required(recoveryScope.source, 'pilot_scope.source'),
  eventType: required(recoveryScope.source_event_type, 'pilot_scope.source_event_type'),
  additionalEventTypes: required(recoveryScope.additional_source_event_types, 'pilot_scope.additional_source_event_types'),
  policy: recoveryPolicy,
  cohort: 5, total: 20, perDay: 20,
};
const generationOf = async (scope) => one((await db.query(`
  select generation from public.pilot_runtime_controls where scope_key = $1
`, [scope.key])).rows, `${scope.key} generation`).generation;
const arm = async (scope) => {
  const armed = one((await db.query(`
    select * from public.set_lancemos_pilot_runtime_state($1,$2,$3,'armed','operator-test','controlled-test')
  `, [scope.key, scope.version, await generationOf(scope)])).rows, `${scope.key} arm`);
  if (armed.runtime_state !== 'armed') throw new Error(`${scope.key} was not armed`);
};
for (const scope of [FIXTURE_SCOPE, OPEN_SCOPE, MANUAL_SCOPE, HOTMART_SCOPE]) {
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
            $13::text[],'cart_recovery',$14,$15,$16,$17,$18,$19,$20,
            'operator-test',now(),now())
  `, [scope.key, scope.version, ATT1.tenant, ATT1.accountId, ATT1.inboxId,
    CHANNEL_PROVIDER, CHANNEL_REF, scope.source, scope.eventType, scope.additionalEventTypes,
    String(ATT1.productId), defaultOffer.offer_code,
    additionalOffers.map((offer) => offer.offer_code),
    scope.policy.policy_key, scope.policy.version, ATT1.timezone,
    scope.cohort, scope.total, scope.perDay, scope.mode]);
  await db.query(`
    insert into public.pilot_runtime_controls
      (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
    values ($1,$2,'inactive',0,'operator-test','default-off')
  `, [scope.key, scope.version]);
}
// El de la instancia queda desarmado: asi nace en aprovisionar-att1.sql.
for (const scope of [OPEN_SCOPE, MANUAL_SCOPE, HOTMART_SCOPE]) await arm(scope);

// ---------------------------------------------------------------------------
// Tiempos, personas y formularios.
// ---------------------------------------------------------------------------
const dbNow = async () => new Date(
  (await db.query('select clock_timestamp() as now')).rows[0].now,
);
const baseMs = Math.floor((await dbNow()).getTime() / 1000) * 1000;
const at = (minutes) => new Date(baseMs + minutes * 60_000);
// Un formulario de hace 61 minutos: su accion ya vencio la demora y se puede
// enviar con el reloj real. Uno de hace 10: todavia esta en la demora.
const DUE = at(-(GRACE_MINUTES + 1));
const WAITING = at(-10);
const CART_AT = at(-30);
const FAILED_AT = at(-20);
const PURCHASED_AT = at(-2);

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
      || offer === undefined) {
    throw new Error(`${name} is not the ${country} translator golden this validator expects`);
  }
  return { raw, canonical, offer, callingCode, country, name: canonical.lead.full_name };
};
// El golden de la landing -d es un movil mexicano (52 + 10 digitos) y el de
// ads-a uno argentino (54 + 10).
const GOLDEN = {
  MX: golden('ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json',
    { country: 'MX', callingCode: '52', offerCode: '2uafw5bg' }),
  AR: golden('ghl_form_webhook_att1_ads_a_20260929.lead_precheckout.json',
    { country: 'AR', callingCode: '54', offerCode: 'gopi6lh7' }),
};
const CAPTURED_CART = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/hotmart_cart_abandonment_rejected_v1.json'), 'utf8',
)).payload;

// ULID como el del adaptador (generate_issuance_ulid): 48 bits de milisegundos
// y 80 aleatorios, en base32 de Crockford.
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

// Una persona con un movil sintetico en sus dos formas: plain (52 / 54 + 10
// digitos, la que guarda el formulario) y whatsapp (521 / 549 + 10, la de
// Hotmart y el wa_id).
let personIndex = 0;
const person = (label, country) => {
  personIndex += 1;
  const suffix = String(personIndex).padStart(2, '0');
  const { callingCode, offer, name } = GOLDEN[country];
  const national = `${country === 'MX' ? '55' : '11'}555504${suffix}`;
  return {
    label,
    country,
    offer,
    name,
    callingCode,
    email: `att1-first-contact-${suffix}@example.test`,
    plain: `${callingCode}${national}`,
    whatsapp: `${callingCode}${country === 'MX' ? '1' : '9'}${national}`,
    other: `${callingCode}${country === 'MX' ? '33' : '35'}555509${suffix}`,
  };
};

// El formulario de una persona: el golden de la landing con id, fecha y
// comprador sustituidos. landing permite enviar por la otra landing (variante
// derivada: el golden de esa landing con el pais y el telefono de la persona)
// y consented=false es el envio 1.0.0 sin opt-in (variante derivada).
const formOf = (lead, {
  landing = lead.country, submittedAt = DUE, phone = lead.plain, consented = true,
} = {}) => {
  const source = GOLDEN[landing];
  const raw = structuredClone(source.raw);
  const canonical = structuredClone(source.canonical);
  const id = ulidAt(submittedAt.getTime());
  const dedupeKey = `${raw.source.site}:${raw.data.offer.code}:${lead.email}`;
  raw.id = id;
  raw.created_at = submittedAt.toISOString();
  raw.data.buyer.email = lead.email;
  raw.data.buyer.phone = `+${phone}`;
  raw.data.buyer.phone_country_code = lead.callingCode;
  raw.data.buyer.phone_national = phone.slice(lead.callingCode.length);
  raw.data.checkout_country.iso = lead.country;
  raw.dedupe_key = dedupeKey;
  canonical.external_submission_id = id;
  canonical.submitted_at = submittedAt.toISOString().replace('.000Z', 'Z');
  canonical.identity.email = lead.email;
  canonical.identity.phone = phone;
  canonical.identity.phone_country_iso = lead.country;
  canonical.dedupe_key = dedupeKey;
  if (!consented) {
    raw.version = '1.0.0';
    raw.data.consent = {
      marketing_optin: false, notice: 'Aviso de privacidad sin opt-in explicito.',
    };
    canonical.contract_version = '1.0.0';
    canonical.consent = {
      terms_accepted: false, privacy_accepted: false, marketing_optin: false,
      whatsapp_contact: false, copy_version: 'lead-precheckout-v1-no-explicit-optin',
    };
    canonical.assurance.activation_authorized = false;
  }
  return { id, raw, canonical, offer: source.offer, submittedAt };
};

// Lo que un rol de la API puede hacer: los entrypoints se llaman como
// service_role (el bridge); el resto de la cadena, como en los otros
// validadores, con el rol de la sesion.
const asService = async (action) => {
  await db.exec('set role service_role');
  try {
    return await action();
  } finally {
    await db.exec('reset role');
  }
};
const deliver = (form, scope) => asService(() => db.query(`
  select * from public.admit_and_plan_portable_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, form.id, JSON.stringify(form.raw),
  JSON.stringify(form.canonical), scope.key, scope.version]));
const ledgerOf = async (submissionId) => (await db.query(`
  select * from public.portable_precheckout_first_contact_plans where submission_id = $1
`, [submissionId])).rows[0] ?? null;
// Envia un formulario por el entrypoint y devuelve la admision, el renglon del
// plan y, si se planifico, el caso y su accion.
const submit = async (lead, scope, options = {}) => {
  const form = formOf(lead, options);
  const admitted = one((await deliver(form, scope)).rows, `${lead.label} form`);
  const ledger = await ledgerOf(admitted.submission_id);
  if (admitted.outcome !== 'inserted'
      || ledger === null
      || ledger.purchase_intent_id !== admitted.purchase_intent_id
      || ledger.scope_key !== scope.key
      || ledger.scope_version !== scope.version
      || ledger.outcome !== admitted.plan_outcome
      || ledger.reason_code !== admitted.plan_reason) {
    throw new Error(`${lead.label}: the form was not admitted with its plan row: ${JSON.stringify({ outcome: admitted.outcome, plan: admitted.plan_outcome, reason: admitted.plan_reason, ledger: ledger?.outcome })}`);
  }
  lead.intent = admitted.purchase_intent_id;
  const result = {
    form,
    submission: admitted.submission_id,
    intent: admitted.purchase_intent_id,
    outcome: ledger.outcome,
    reason: ledger.reason_code,
    sqlstate: ledger.error_sqlstate,
    contact: ledger.contact_id,
    caseId: ledger.recovery_case_id,
    scope,
  };
  if (ledger.outcome === 'planned') {
    const action = one((await db.query(`
      select id, followup_sequence_id, status, due_at, expires_at, anchor_type,
             step_key, action_type, anchor_subject_internal_id, terminal_reason
      from public.scheduled_actions where recovery_case_id = $1
    `, [ledger.recovery_case_id])).rows, `${lead.label} action`);
    result.actionId = action.id;
    result.sequenceId = action.followup_sequence_id;
    result.action = action;
    lead.contact = ledger.contact_id;
  }
  return result;
};
const expectPlan = (label, plan, outcome, reason) => {
  if (plan.outcome !== outcome || plan.reason !== reason) {
    throw new Error(`${label}: expected ${outcome}:${reason}, got ${plan.outcome}:${plan.reason} (${plan.sqlstate})`);
  }
  if ((outcome === 'planned') !== (plan.caseId !== null)) {
    throw new Error(`${label}: plan row and case disagree: ${JSON.stringify({ outcome, caseId: plan.caseId })}`);
  }
  return `${outcome}:${reason}`;
};

const PEOPLE_TABLES = ['contacts', 'contact_points', 'channel_identities', 'recovery_cases',
  'scheduled_actions', 'contact_authorizations', 'pilot_recovery_case_bindings'];
const footprint = async () => {
  const row = one((await db.query(`
    select ${PEOPLE_TABLES.map((table) => `(select count(*)::integer from public.${table}) as ${table}`).join(', ')},
           (select count(*)::integer from public.webhook_events where source = 'system') as anchors
  `)).rows, 'footprint');
  return row;
};
const expectNoFootprint = async (label, before) => {
  const now = await footprint();
  if (!same(now, before)) {
    throw new Error(`${label}: an unplanned form left rows behind: ${JSON.stringify({ before, now })}`);
  }
};
const contactOf = async (id) => one((await db.query(`
  select full_name, email, phone, country_iso, contact_permission, lifecycle_status
  from public.contacts where id = $1
`, [id])).rows, 'contact');
const pointsOf = async (id) => (await db.query(`
  select type, normalized_value, raw_value, source, metadata
  from public.contact_points where contact_id = $1 order by type, normalized_value
`, [id])).rows;
const identitiesOf = async (id) => (await db.query(`
  select id, external_user_id, identity_status, metadata ->> 'inbox_id' as inbox_id
  from public.channel_identities where contact_id = $1 and channel = 'whatsapp'
  order by external_user_id
`, [id])).rows;
const caseOf = async (id) => one((await db.query(`
  select rc.status, rc.source, rc.contact_id, rc.offer_code, rc.external_product_id,
         rc.product_name, rc.purchase_event_id, rc.hotmart_purchase_intent_id,
         rc.selected_channel_identity_id, rc.identity_resolution_status, rc.context,
         rc.policy_key, rc.policy_version, rc.abandonment_event_id, rc.conversation_id,
         rc.version
  from public.recovery_cases rc where rc.id = $1
`, [id])).rows, 'case');
const actionOf = async (id) => one((await db.query(`
  select status, terminal_reason, due_at, expires_at from public.scheduled_actions where id = $1
`, [id])).rows, 'action');
const sequenceOf = async (id) => one((await db.query(`
  select status, completion_reason, cancel_reason, automatic_messages_accepted
  from public.followup_sequences where id = $1
`, [id])).rows, 'sequence');
const activeAllowed = async (contactId) => (await db.query(`
  select authorization_source, evidence from public.contact_authorizations
  where contact_id = $1 and channel = 'whatsapp' and purpose = 'cart_recovery'
    and authorization_status = 'allowed'
    and valid_from <= clock_timestamp()
    and (valid_until is null or valid_until > clock_timestamp())
`, [contactId])).rows;
const startsOf = async (scope) => one((await db.query(`
  select count(*)::integer as count from public.pilot_outbound_request_authorizations
  where scope_key = $1
`, [scope.key])).rows, `${scope.key} starts`).count;
const attemptsOf = async (actionId) => one((await db.query(`
  select count(*)::integer as count from public.followup_delivery_attempts where action_id = $1
`, [actionId])).rows, 'attempts').count;
const intentOf = async (id) => one((await db.query(`
  select normalized_phone, offer_ref, lifecycle_state, current_classification,
         whatsapp_contact_authorized, activation_authorized, submitted_at
  from public.purchase_intents where id = $1
`, [id])).rows, 'intent');
const isoOf = (value) => new Date(value).toISOString();

// Todo lo que deja un plan, contra lo que deberia dejar.
const expectPlanned = async (label, lead, plan, {
  identity = lead.plain, phoneMatch = 'exact', identities = 1,
} = {}) => {
  const recoveryCase = await caseOf(plan.caseId);
  const anchor = one((await db.query(`
    select source, external_event_id, event_type, processing_status, payload
    from public.webhook_events where id = $1
  `, [recoveryCase.abandonment_event_id])).rows, `${label} anchor`);
  const submittedMs = plan.form.submittedAt.getTime();
  const ownIdentities = await identitiesOf(plan.contact);
  const selected = ownIdentities.find((row) => row.id === recoveryCase.selected_channel_identity_id);
  const binding = one((await db.query(`
    select scope_key, scope_version, source_event_id, audience_mode,
           audience_purchase_intent_id, audience_precheckout_submission_id
    from public.pilot_recovery_case_bindings where recovery_case_id = $1
  `, [plan.caseId])).rows, `${label} binding`);
  const events = (await db.query(`
    select event_role, observed_at, webhook_event_id from public.recovery_case_events
    where recovery_case_id = $1
  `, [plan.caseId])).rows;
  const sequence = one((await db.query(`
    select reason, status, max_attempts from public.followup_sequences where id = $1
  `, [plan.sequenceId])).rows, `${label} sequence`);
  const grants = await activeAllowed(plan.contact);
  const problems = [];
  const check = (condition, name) => { if (!condition) problems.push(name); };
  check(recoveryCase.source === 'landing', 'case.source');
  check(recoveryCase.status === 'grace_period', 'case.status');
  check(recoveryCase.contact_id === plan.contact, 'case.contact');
  check(recoveryCase.offer_code === plan.form.offer.offer_code, 'case.offer');
  check(recoveryCase.external_product_id === String(ATT1.productId), 'case.product');
  check(recoveryCase.product_name === ATT1.productName, 'case.product_name');
  check(recoveryCase.hotmart_purchase_intent_id === plan.intent, 'case.intent');
  check(recoveryCase.identity_resolution_status === 'resolved', 'case.identity_status');
  check(recoveryCase.policy_key === plan.scope.policy.policy_key, 'case.policy');
  check(recoveryCase.conversation_id === null && recoveryCase.purchase_event_id === null, 'case.links');
  check(same(Object.keys(recoveryCase.context).sort(),
    ['precheckout_submission_id', 'precheckout_submitted_at', 'purchase_intent_id', 'trigger_kind'])
    && recoveryCase.context.trigger_kind === 'precheckout_intent'
    && recoveryCase.context.precheckout_submission_id === plan.submission, 'case.context');
  // El ancla: de fuente system, por envio, ya processed y solo con ids.
  check(anchor.source === 'system'
    && anchor.external_event_id === `precheckout-submission:${plan.submission}`
    && anchor.event_type === 'PRECHECKOUT_FORM_SUBMITTED'
    && anchor.processing_status === 'processed', 'anchor');
  check(same(Object.keys(anchor.payload).sort(),
    ['creation_date', 'precheckout_submission_id', 'purchase_intent_id'])
    && anchor.payload.creation_date === submittedMs
    && anchor.payload.purchase_intent_id === plan.intent
    && anchor.payload.precheckout_submission_id === plan.submission, 'anchor.payload');
  check(events.length === 1 && events[0].event_role === 'precheckout_intent'
    && events[0].webhook_event_id === recoveryCase.abandonment_event_id, 'case_event');
  check(sequence.reason === 'precheckout_intent' && sequence.status === 'active'
    && sequence.max_attempts === 1, 'sequence');
  // La demora y el vencimiento corren desde el envio que dispara el plan.
  check(plan.action.action_type === 'first_contact_review'
    && plan.action.step_key === 'first_contact'
    && plan.action.anchor_type === 'precheckout_intent'
    && plan.action.anchor_subject_internal_id === recoveryCase.abandonment_event_id
    && plan.action.status === 'pending', 'action');
  check(new Date(plan.action.due_at).getTime() === submittedMs + GRACE_MINUTES * 60_000, 'action.due_at');
  check(new Date(plan.action.expires_at).getTime() === submittedMs + 24 * 3_600_000, 'action.expires_at');
  check(selected !== undefined && selected.external_user_id === identity
    && selected.identity_status === 'active'
    && selected.inbox_id === String(ATT1.inboxId), 'identity');
  check(ownIdentities.length === identities, 'identity.count');
  check(binding.scope_key === plan.scope.key && binding.scope_version === plan.scope.version
    && binding.source_event_id === recoveryCase.abandonment_event_id
    && binding.audience_mode === plan.scope.mode
    && binding.audience_purchase_intent_id === plan.intent
    && binding.audience_precheckout_submission_id === plan.submission, 'binding');
  check(grants.length === 1 && grants[0].authorization_source === 'system'
    && grants[0].evidence.reason === 'precheckout_whatsapp_consent'
    && grants[0].evidence.purchase_intent_id === plan.intent
    && grants[0].evidence.consent_copy_version === ATT1.copyVersion
    && grants[0].evidence.phone_match === phoneMatch, 'grant');
  if (problems.length !== 0) {
    throw new Error(`${label}: the plan is not what the contract says: ${problems.join(', ')}`);
  }
  return { recoveryCase, anchor, grants };
};

// El dispatcher en modo directo. El claim tiene que devolver solo esta accion:
// es la unica vencida de la base.
const claimNow = async (label, { now } = {}) => {
  const moment = now ?? await dbNow();
  return {
    now: moment,
    worker: `att1-first-contact-${label}`,
    rows: (await db.query(`
      select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
    `, [`att1-first-contact-${label}`, moment])).rows,
  };
};
const claimPlan = async (lead, plan, anchor = 'precheckout_intent') => {
  const claim = await claimNow(lead.label);
  if (claim.rows.length !== 1 || claim.rows[0].id !== plan.actionId
      || claim.rows[0].anchor_type !== anchor) {
    throw new Error(`${lead.label}: claimed ${JSON.stringify(claim.rows.map((row) => [row.id, row.anchor_type]))}, expected ${plan.actionId} ${anchor}`);
  }
  return { worker: claim.worker, lease: claim.rows[0].lease_generation, now: claim.now };
};
// La reevaluacion del primer contacto, como service_role: es la RPC que elige
// el bridge por el anchor_type de la accion reclamada.
const reevaluate = async (plan, claim) => one((await asService(() => db.query(`
  select * from public.reevaluate_portable_precheckout_action($1,$2,$3,$4)
`, [plan.actionId, claim.worker, claim.lease, claim.now]))).rows, 'reevaluation');
const reserve = async (lead, plan) => {
  const claim = await claimPlan(lead, plan);
  const decision = await reevaluate(plan, claim);
  if (decision.decision !== 'execute' || decision.reason_code !== 'eligible_for_execution') {
    throw new Error(`${lead.label}: the real reevaluation did not execute: ${JSON.stringify(decision)}`);
  }
  const context = one((await db.query(`
    select * from public.get_followup_execution_context($1,$2,$3,$4)
  `, [plan.actionId, claim.worker, claim.lease, claim.now])).rows, `${lead.label} context`);
  const attempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
  `, [plan.actionId, claim.worker, claim.lease, decision.case_version,
    decision.sequence_revision, claim.now])).rows, `${lead.label} reservation`);
  return { ...claim, decision, context, attempt };
};
const start = async (plan, reservation) => asService(async () => db.query(`
  select * from public.mark_portable_precheckout_request_started($1,$2,$3,$4,$5)
`, [plan.actionId, reservation.attempt.id, reservation.worker, reservation.lease,
  await dbNow()]));
const expectStartRejected = async (label, plan, reservation, detail) => {
  const startsBefore = await startsOf(plan.scope);
  let error = null;
  try {
    await start(plan, reservation);
  } catch (caught) {
    error = caught;
  }
  if (error?.code !== '55000' || error?.message !== 'pilot_request_start_rejected'
      || error?.detail !== detail || (await startsOf(plan.scope)) !== startsBefore) {
    throw new Error(`${label}: expected pilot_request_start_rejected:${detail} without a consumed start, got ${error?.code} ${error?.message} ${error?.detail}`);
  }
  return `${error.message}:${error.detail}`;
};
let messageNumber = 0;
const dispatch = async (lead, plan, { buyerPhone = lead.plain } = {}) => {
  const reservation = await reserve(lead, plan);
  const { context } = reservation;
  if (context.step_key !== 'first_contact'
      || context.action_type !== 'first_contact_review'
      || context.offer_code !== plan.form.offer.offer_code
      || context.buyer_phone !== buyerPhone
      || context.buyer_name !== lead.name
      || context.product_name !== ATT1.productName) {
    throw new Error(`${lead.label}: execution context diverged: ${JSON.stringify({ step: context.step_key, offer: context.offer_code, phone: context.buyer_phone === buyerPhone, name: context.buyer_name === lead.name })}`);
  }
  const startsBefore = await startsOf(plan.scope);
  const started = one((await start(plan, reservation)).rows, `${lead.label} request start`);
  if (started.phase !== 'request_started' || started.mode !== 'approved_template'
      || started.pilot_authorization_id == null
      || started.pilot_authorization_replayed !== false
      || (await startsOf(plan.scope)) !== startsBefore + 1) {
    throw new Error(`${lead.label}: the request did not start: ${JSON.stringify(started)}`);
  }
  // El evento de control guarda el modo y la evidencia de audiencia.
  const control = one((await db.query(`
    select data from public.pilot_control_events
    where attempt_id = $1 and event_type = 'pilot_outbound_request_authorized'
  `, [reservation.attempt.id])).rows, `${lead.label} control event`).data;
  if (control.audience_mode !== plan.scope.mode
      || control.audience_purchase_intent_id !== plan.intent
      || control.audience_precheckout_submission_id !== plan.submission) {
    throw new Error(`${lead.label}: the authorization did not record the audience evidence`);
  }
  // El replay de un arranque ya autorizado devuelve la misma autorizacion.
  const replayed = one((await start(plan, reservation)).rows, `${lead.label} start replay`);
  if (replayed.pilot_authorization_replayed !== true
      || replayed.pilot_authorization_id !== started.pilot_authorization_id
      || (await startsOf(plan.scope)) !== startsBefore + 1) {
    throw new Error(`${lead.label}: the start replay consumed another start`);
  }
  messageNumber += 1;
  const accepted = one((await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
  `, [plan.actionId, reservation.attempt.id, reservation.worker, reservation.lease,
    String(930100 + messageNumber), `att1-first-contact-wamid-${messageNumber}`,
    `Plantilla aprobada de ATT1, primer contacto (${ATT1.productName})`,
    await dbNow()])).rows, `${lead.label} acceptance`);
  const recoveryCase = await caseOf(plan.caseId);
  const sequence = await sequenceOf(plan.sequenceId);
  if (accepted.status !== 'accepted_by_chatwoot'
      || recoveryCase.status !== 'sequence_exhausted'
      || sequence.status !== 'completed' || sequence.completion_reason !== 'policy_exhausted'
      || sequence.automatic_messages_accepted !== 1
      || (await claimNow(`${lead.label}-later`, { now: at(2 * 24 * 60) })).rows.length !== 0) {
    throw new Error(`${lead.label}: acceptance left open work: ${JSON.stringify({ accepted: accepted.status, recoveryCase: recoveryCase.status, sequence })}`);
  }
  return 'accepted';
};
// La reevaluacion que cancela: el caso queda cancelled (no won), sin intento
// y sin consumir cupo, y el replay devuelve la misma decision.
const expectCancelled = async (lead, plan, reason, { detail } = {}) => {
  const startsBefore = await startsOf(plan.scope);
  const claim = await claimPlan(lead, plan);
  const decision = await reevaluate(plan, claim);
  const replay = await reevaluate(plan, claim);
  const recoveryCase = await caseOf(plan.caseId);
  const action = await actionOf(plan.actionId);
  const sequence = await sequenceOf(plan.sequenceId);
  const event = one((await db.query(`
    select data from public.conversation_events
    where related_action_id = $1 and event_type = 'followup_action_reevaluated'
  `, [plan.actionId])).rows, `${lead.label} reevaluation event`).data;
  if (decision.decision !== 'cancel' || decision.reason_code !== reason
      || !same(replay, decision)
      || recoveryCase.status !== 'cancelled' || recoveryCase.purchase_event_id !== null
      || action.status !== 'cancelled' || action.terminal_reason !== reason
      || sequence.status !== 'completed' || sequence.completion_reason !== reason
      || (detail !== undefined && event.detail !== detail)
      || (await attemptsOf(plan.actionId)) !== 0
      || (await startsOf(plan.scope)) !== startsBefore
      || (await claimNow(`${lead.label}-after`)).rows.length !== 0) {
    throw new Error(`${lead.label}: expected cancel:${reason}, got ${JSON.stringify({ decision, replay, recoveryCase: recoveryCase.status, action, sequence, detail: event.detail })}`);
  }
  return `cancel:${reason}`;
};

// Lo que deja el script de siembra de la instancia para el E2E: el contacto con
// telefono, nombre, email y sus puntos.
const seedContact = async (lead, { contactPhone = lead.plain, pointPhone = lead.plain } = {}) => {
  lead.contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone, country_iso)
    values ($1,$2,$3,$4) returning id
  `, [lead.name, lead.email, contactPhone, lead.country])).rows, `${lead.label} contact`).id;
  await db.query(`
    insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1,'email',$2,$2,'manual'), ($1,'phone',$3,$3,'manual')
  `, [lead.contact, lead.email, pointPhone]);
  return lead.contact;
};
const enroll = async (lead, scope) => {
  const member = one((await db.query(`
    select * from public.set_lancemos_pilot_cohort_member($1,$2,$3,$4,'active','operator-test','controlled-test')
  `, [scope.key, scope.version, lead.contact, await generationOf(scope)])).rows,
  `${lead.label} enrollment`);
  if (member.member_status !== 'active') {
    throw new Error(`${lead.label} was not enrolled: ${JSON.stringify(member)}`);
  }
};

// Compra aprobada por la admision portable (precedente inline, sin captura).
let transactionIndex = 0;
const purchasePayload = (lead, { phone = lead.whatsapp, approvedAt = PURCHASED_AT, offer = lead.offer } = {}) => {
  transactionIndex += 1;
  return {
    id: `att1-first-contact-purchase-${transactionIndex}`,
    creation_date: approvedAt.getTime(),
    event: 'PURCHASE_APPROVED',
    version: '2.0.0',
    data: {
      product: { id: ATT1.productId, ucode: 'ATT1-FIRST-CONTACT-UCODE' },
      buyer: { email: lead.email, checkout_phone: `+${phone}` },
      purchase: {
        approved_date: approvedAt.getTime(),
        status: 'APPROVED',
        transaction: `HPATT1FC${String(transactionIndex).padStart(4, '0')}`,
        offer: { code: offer.offer_code },
      },
    },
  };
};
const admitPurchase = async (lead, options = {}) => {
  const payload = purchasePayload(lead, options);
  const phone = options.phone ?? lead.whatsapp;
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload),
    lead.email, phone])).rows, `${lead.label} purchase`);
  const correlation = one((await db.query(`
    select outcome, purchase_intent_id, candidate_count
    from public.portable_hotmart_purchase_correlations where webhook_event_id = $1
  `, [admitted.webhook_event_id])).rows, `${lead.label} purchase correlation`);
  if (admitted.outcome !== 'inserted') {
    throw new Error(`${lead.label}: the purchase was not admitted: ${admitted.outcome}`);
  }
  return { eventId: admitted.webhook_event_id, ...correlation };
};
// La compra por la admision compartida (la de una instancia que todavia no
// prendio el freno portable): queda received y sin correlacion portable.
const admitSharedPurchase = async (lead, options = {}) => {
  const payload = purchasePayload(lead, options);
  const admitted = one((await db.query(`
    select * from public.admit_and_correlate_hotmart_purchase_approved($1,$2::jsonb,$3,$4)
  `, [payload.id, JSON.stringify(payload), lead.email, options.phone ?? lead.whatsapp])).rows,
  `${lead.label} shared purchase`);
  const event = one((await db.query(`
    select processing_status from public.webhook_events where id = $1
  `, [admitted.webhook_event_id])).rows, `${lead.label} shared purchase event`);
  if (admitted.outcome !== 'inserted' || event.processing_status !== 'received') {
    throw new Error(`${lead.label}: the shared purchase is not waiting in received: ${JSON.stringify({ outcome: admitted.outcome, status: event.processing_status })}`);
  }
  return admitted.webhook_event_id;
};
// Carrito capturado, con el telefono que manda Hotmart (521... / 549...).
const admitCart = async (lead, { phone = lead.whatsapp, offer = lead.offer } = {}) => {
  const payload = structuredClone(CAPTURED_CART);
  payload.id = `att1-first-contact-cart-${lead.email}-${offer.offer_code}`;
  payload.creation_date = CART_AT.getTime();
  payload.data.product = { id: ATT1.productId, name: ATT1.productName };
  payload.data.offer = { code: offer.offer_code };
  payload.data.buyer = { name: lead.name, email: lead.email, phone };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
    JSON.stringify(payload), lead.email, phone])).rows, `${lead.label} cart`);
  if (admitted.outcome !== 'inserted') {
    throw new Error(`${lead.label}: cart was not admitted: ${admitted.outcome}`);
  }
  return { eventId: admitted.webhook_event_id, abandonedAt: new Date(payload.creation_date), phone, offer };
};
// Pago fallido (precedente inline, sin captura).
const admitFailure = async (lead, { phone = lead.whatsapp } = {}) => {
  transactionIndex += 1;
  const payload = {
    id: `att1-first-contact-failure-${lead.email}`,
    creation_date: FAILED_AT.getTime(),
    event: 'PURCHASE_CANCELED',
    version: '2.0.0',
    data: {
      buyer: { name: lead.name, email: lead.email, checkout_phone: `+${phone}` },
      product: { id: ATT1.productId, name: ATT1.productName },
      purchase: {
        transaction: `HPATT1FC${String(transactionIndex).padStart(4, '0')}`,
        status: 'CANCELED',
        offer: { code: lead.offer.offer_code },
        payment: { refusal_reason: 'insufficient_funds' },
      },
      checkout_country: { iso: lead.country, name: lead.country },
    },
  };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
    JSON.stringify(payload), lead.email, phone])).rows, `${lead.label} failure`);
  if (admitted.outcome !== 'inserted') {
    throw new Error(`${lead.label}: payment failure was not admitted: ${admitted.outcome}`);
  }
  return { eventId: admitted.webhook_event_id, failedAt: new Date(payload.creation_date), phone };
};
// Lo que suma resolve_event cuando el evento de Hotmart encuentra al contacto:
// los puntos del evento, con fuente hotmart.
const addHotmartPoints = async (lead, eventId, phone) => db.query(`
  insert into public.contact_points
    (contact_id, type, raw_value, normalized_value, source, source_event_id)
  select $1, point.type, point.value, point.value, 'hotmart', $4
  from (values ('email', $2::text), ('phone', $3::text)) as point(type, value)
  where not exists (
    select 1 from public.contact_points existing
    where existing.contact_id = $1 and existing.type = point.type
      and existing.normalized_value = point.value
  )
`, [lead.contact, lead.email, phone, eventId]);
// El opt-out real de Chatwoot (apply_chatwoot_inbound_opt_out), desde un wa_id.
let optOutMessage = 0;
const optOutFrom = async (waId) => {
  optOutMessage += 1;
  return one((await db.query(`
    select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,$6,'unsubscribe')
  `, [ATT1.accountId, ATT1.inboxId, 7700 + optOutMessage, 97700 + optOutMessage, waId,
    await dbNow()])).rows, 'opt-out');
};
const statusOf = async (scope) => one((await asService(() => db.query(`
  select * from public.get_portable_precheckout_pilot_runtime_status($1,$2,$3,$4,$5)
`, [scope.key, scope.version, ATT1.tenant, CHANNEL_PROVIDER, CHANNEL_REF]))).rows,
`${scope.key} status`);

// ---------------------------------------------------------------------------
// 1. El E2E de la instancia, con su scope (consented_intent_in_cohort), en el
//    orden del runbook: formulario con el scope desarmado, sembrar e inscribir,
//    armar, reenviar, y la compra antes de la demora con otra identidad.
// ---------------------------------------------------------------------------
{
  const inactive = await statusOf(FIXTURE_SCOPE);
  if (inactive.configured !== true || inactive.runtime_state !== 'inactive'
      || inactive.reason_code !== 'pilot_runtime_inactive') {
    throw new Error(`the instance scope is not configured and inactive: ${JSON.stringify(inactive)}`);
  }
  const first = person('e2e-envio', 'MX');
  const empty = await footprint();
  const disarmed = await submit(first, FIXTURE_SCOPE);
  const disarmedResult = expectPlan('disarmed scope', disarmed, 'not_planned', 'pilot_runtime_not_armed');
  await expectNoFootprint('disarmed scope', empty);
  if (disarmed.contact !== null) throw new Error('the disarmed plan row names a contact');

  await arm(FIXTURE_SCOPE);
  const armed = await statusOf(FIXTURE_SCOPE);
  const withoutContact = await submit(first, FIXTURE_SCOPE);
  const withoutContactResult = expectPlan('cohort without a contact', withoutContact,
    'not_planned', 'pilot_contact_not_in_cohort');
  await expectNoFootprint('cohort without a contact', empty);

  // El contacto existe pero no esta inscripto: tampoco, y no se lo toca.
  await seedContact(first);
  const seeded = await footprint();
  const notEnrolled = await submit(first, FIXTURE_SCOPE);
  const notEnrolledResult = expectPlan('cohort with a contact outside it', notEnrolled,
    'not_planned', 'pilot_contact_not_in_cohort');
  await expectNoFootprint('cohort with a contact outside it', seeded);
  if (notEnrolled.contact !== first.contact) {
    throw new Error('the plan row does not name the contact that was found');
  }

  await enroll(first, FIXTURE_SCOPE);
  const planned = await submit(first, FIXTURE_SCOPE);
  const plannedResult = expectPlan('cohort plan', planned, 'planned', 'first_contact_scheduled');
  await expectPlanned('cohort plan', first, planned);
  if (planned.intent !== disarmed.intent || planned.submission === disarmed.submission
      || (await pointsOf(first.contact)).some((point) => point.source !== 'manual')) {
    throw new Error('the resend did not reuse the intent, or touched the seeded contact points');
  }
  const sent = await dispatch(first, planned);

  // La compra antes de la demora, con otra identidad sembrada e inscripta.
  const buyer = person('e2e-compra', 'AR');
  await seedContact(buyer);
  await enroll(buyer, FIXTURE_SCOPE);
  const buyerPlan = await submit(buyer, FIXTURE_SCOPE);
  expectPlan('cohort buyer plan', buyerPlan, 'planned', 'first_contact_scheduled');
  await expectPlanned('cohort buyer plan', buyer, buyerPlan);
  const purchase = await admitPurchase(buyer);
  if (purchase.outcome !== 'resolved' || purchase.purchase_intent_id !== buyerPlan.intent
      || (await intentOf(buyerPlan.intent)).lifecycle_state !== 'purchased') {
    throw new Error(`the purchase did not close the intent: ${JSON.stringify(purchase)}`);
  }
  const bought = await expectCancelled(buyer, buyerPlan, 'intent_purchased');
  if (armed.runtime_state !== 'armed' || (await startsOf(FIXTURE_SCOPE)) !== 1) {
    throw new Error('the instance scope consumed more than one start');
  }
  results.instance_e2e = {
    disarmed: disarmedResult,
    armed_without_contact: withoutContactResult,
    contact_outside_cohort: notEnrolledResult,
    enrolled: `${plannedResult}:${sent}`,
    purchase_before_the_delay: bought,
    starts: await startsOf(FIXTURE_SCOPE),
  };
}

// ---------------------------------------------------------------------------
// 2. El scope abierto (consented_intent): el contacto nuevo, la demora, dos
//    entregas, un primer contacto vivo por persona, el segundo envio de la
//    misma intencion con el primer caso cerrado, y las 24 h tras el toque.
// ---------------------------------------------------------------------------
{
  const lead = person('demora', 'MX');
  const before = await footprint();
  const waiting = await submit(lead, OPEN_SCOPE, { submittedAt: WAITING });
  expectPlan('open plan', waiting, 'planned', 'first_contact_scheduled');
  await expectPlanned('open plan', lead, waiting);
  // El contacto nace del formulario, con sus puntos de fuente system.
  const contact = await contactOf(waiting.contact);
  const points = await pointsOf(waiting.contact);
  const created = await footprint();
  if (contact.full_name !== lead.name || contact.email !== lead.email
      || contact.phone !== lead.plain || contact.country_iso !== 'MX'
      || contact.contact_permission !== 'unknown' || contact.lifecycle_status !== 'lead'
      || points.length !== 2
      || points.some((point) => point.source !== 'system'
        || point.metadata.reason !== 'precheckout_form_submission'
        || point.metadata.purchase_intent_id !== waiting.intent)
      || points.map((point) => `${point.type}:${point.normalized_value}`).join(',')
        !== `email:${lead.email},phone:${lead.plain}`
      || created.contacts !== before.contacts + 1
      || created.anchors !== before.anchors + 1
      || created.recovery_cases !== before.recovery_cases + 1) {
    throw new Error(`the form did not create the contact as the contract says: ${JSON.stringify({ contact: { ...contact, full_name: contact.full_name === lead.name, email: contact.email === lead.email, phone: contact.phone === lead.plain }, points: points.length })}`);
  }

  // La demora: no sale antes de submitted_at + 60 minutos.
  const dueAt = new Date(waiting.action.due_at);
  const early = (await claimNow('demora-antes')).rows.length;
  const almost = (await claimNow('demora-casi', { now: new Date(dueAt.getTime() - 60_000) })).rows;
  // Dos entregas del mismo envio: duplicate, el mismo plan y un solo caso.
  const again = one((await deliver(waiting.form, OPEN_SCOPE)).rows, 'duplicate delivery');
  // Otro formulario con el caso abierto: un primer contacto vivo por persona.
  const second = await submit(lead, OPEN_SCOPE, { submittedAt: at(-9) });
  const secondResult = expectPlan('second form with an open case', second,
    'not_planned', 'precheckout_contact_already_planned');
  await expectNoFootprint('second form with an open case', created);
  const due = (await claimNow('demora-vencida', { now: new Date(dueAt.getTime() + 60_000) })).rows;
  if (early !== 0 || almost.length !== 0
      || due.length !== 1 || due[0].id !== waiting.actionId
      || again.outcome !== 'duplicate' || again.submission_id !== waiting.submission
      || again.plan_outcome !== 'planned' || again.plan_reason !== 'first_contact_scheduled'
      || second.intent !== waiting.intent || second.contact !== waiting.contact) {
    throw new Error(`the delay or the duplicate delivery diverged: ${JSON.stringify({ early, almost: almost.length, due: due.length, again: again.outcome, plan: again.plan_outcome })}`);
  }

  // El primer caso se cierra sin toque (vence). Un envio nuevo de la misma
  // intencion planifica otra vez: ancla nueva, y la demora corre desde ESE
  // envio, no desde el submitted_at de la intencion.
  await claimNow('demora-vence', { now: new Date(new Date(waiting.action.expires_at).getTime() + 60_000) });
  const expired = await caseOf(waiting.caseId);
  const resend = await submit(lead, OPEN_SCOPE);
  expectPlan('resend after the first case closed', resend, 'planned', 'first_contact_scheduled');
  await expectPlanned('resend after the first case closed', lead, resend);
  const intent = await intentOf(resend.intent);
  const anchors = one((await db.query(`
    select count(*)::integer as count from public.webhook_events
    where source = 'system' and payload ->> 'purchase_intent_id' = $1
  `, [resend.intent])).rows, 'anchors of the intent').count;
  if (expired.status !== 'expired' || (await actionOf(waiting.actionId)).status !== 'expired'
      || resend.intent !== waiting.intent || resend.caseId === waiting.caseId
      || resend.contact !== waiting.contact
      || isoOf(intent.submitted_at) !== WAITING.toISOString()
      || anchors !== 2
      || (await footprint()).contacts !== created.contacts) {
    throw new Error(`the resend did not get its own anchor and delay: ${JSON.stringify({ expired: expired.status, sameIntent: resend.intent === waiting.intent, anchors })}`);
  }
  const sent = await dispatch(lead, resend);

  // Con el toque aceptado, otro formulario no planifica otro por 24 h.
  const third = await submit(lead, OPEN_SCOPE);
  const thirdResult = expectPlan('form after an accepted touch', third,
    'not_planned', 'precheckout_contact_already_planned');
  results.open_scope = {
    delay: `due_at = submitted_at + ${GRACE_MINUTES} min; not claimed before`,
    duplicate_delivery: `${again.outcome}:${again.plan_outcome}`,
    second_form_with_open_case: secondResult,
    resend_after_closed_case: `planned with its own anchor:${sent}`,
    form_after_accepted_touch: thirdResult,
  };
}

// ---------------------------------------------------------------------------
// 3. Quien escribio primero por WhatsApp. La admision entrante deja un
//    contacto sin nombre, email ni telefono, y la identidad con el wa_id
//    (521...). El formulario trae 52...: se reutilizan el contacto y la
//    identidad, y se completan los datos para que el envio pueda salir.
// ---------------------------------------------------------------------------
{
  const lead = person('entrante-primero', 'MX');
  const inbound = one((await db.query(`
    select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)
  `, [ATT1.inboundScope, ATT1.inboundVersion, 880001, lead.whatsapp])).rows, 'inbound admission');
  const bare = await contactOf(inbound.contact_id);
  const before = await footprint();
  const plan = await submit(lead, OPEN_SCOPE);
  expectPlan('inbound first', plan, 'planned', 'first_contact_scheduled');
  await expectPlanned('inbound first', lead, plan,
    { identity: lead.whatsapp, phoneMatch: 'whatsapp_equivalent' });
  const completed = await contactOf(plan.contact);
  const after = await footprint();
  const recoveryCase = await caseOf(plan.caseId);
  if (bare.full_name !== null || bare.email !== null || bare.phone !== null
      || plan.contact !== inbound.contact_id
      || recoveryCase.selected_channel_identity_id !== inbound.channel_identity_id
      || completed.full_name !== lead.name || completed.email !== lead.email
      || completed.phone !== lead.plain
      || after.contacts !== before.contacts
      || after.channel_identities !== before.channel_identities
      || after.contact_points !== before.contact_points + 2) {
    throw new Error(`the inbound contact was not reused and completed: ${JSON.stringify({ sameContact: plan.contact === inbound.contact_id, contacts: after.contacts - before.contacts, identities: after.channel_identities - before.channel_identities })}`);
  }
  results.inbound_first = `reused contact and identity, completed:${await dispatch(lead, plan)}`;
}

// ---------------------------------------------------------------------------
// 4. Opt-out.
// ---------------------------------------------------------------------------
{
  // a. Previo y unmatched desde el wa_id (521...), formulario en 52...: no
  //    planifica y no crea el contacto.
  const prior = person('opt-out-previo', 'MX');
  const priorOptOut = await optOutFrom(prior.whatsapp);
  const before = await footprint();
  const priorPlan = await submit(prior, OPEN_SCOPE);
  const priorResult = expectPlan('prior opt-out in the other form', priorPlan,
    'not_planned', 'precheckout_prior_opt_out');
  await expectNoFootprint('prior opt-out in the other form', before);

  // b. Entre el plan y el envio, desde la otra forma (549...): no cierra el
  //    caso (no hay identidad con ese id), pero el arranque no sale.
  const inFlight = person('opt-out-en-vuelo', 'AR');
  const inFlightPlan = await submit(inFlight, OPEN_SCOPE);
  expectPlan('opt-out in flight plan', inFlightPlan, 'planned', 'first_contact_scheduled');
  const reservation = await reserve(inFlight, inFlightPlan);
  const inFlightOptOut = await optOutFrom(inFlight.whatsapp);
  const inFlightResult = await expectStartRejected('opt-out in flight', inFlightPlan,
    reservation, 'pilot_audience_consented_intent_prior_opt_out');

  // c. Aplicado al contacto (desde el id de su identidad): el opt-out
  //    compartido cierra el caso de fuente landing, y un formulario posterior
  //    no planifica.
  const applied = person('opt-out-aplicado', 'MX');
  const appliedPlan = await submit(applied, OPEN_SCOPE, { submittedAt: WAITING });
  expectPlan('opt-out applied plan', appliedPlan, 'planned', 'first_contact_scheduled');
  const appliedOptOut = await optOutFrom(applied.plain);
  const appliedCase = await caseOf(appliedPlan.caseId);
  const appliedAction = await actionOf(appliedPlan.actionId);
  const afterApplied = await submit(applied, OPEN_SCOPE);
  const afterAppliedResult = expectPlan('form after an applied opt-out', afterApplied,
    'not_planned', 'precheckout_prior_opt_out');
  if (priorOptOut.outcome !== 'recorded_unmatched'
      || inFlightOptOut.outcome !== 'recorded_unmatched'
      || appliedOptOut.outcome !== 'applied' || appliedOptOut.affected_cases !== 1
      || appliedCase.status !== 'cancelled' || appliedAction.status !== 'cancelled'
      || appliedAction.terminal_reason !== 'contact_opted_out'
      || (await activeAllowed(appliedPlan.contact)).length !== 0) {
    throw new Error(`opt-out: ${JSON.stringify({ prior: priorOptOut.outcome, inFlight: inFlightOptOut.outcome, applied: appliedOptOut.outcome, appliedCase: appliedCase.status, appliedAction })}`);
  }
  results.opt_out = {
    prior_unmatched_other_form: priorResult,
    between_plan_and_send: inFlightResult,
    applied_to_the_contact: `case ${appliedCase.status}:${appliedAction.terminal_reason}`,
    form_after_applied: afterAppliedResult,
  };
}

// ---------------------------------------------------------------------------
// 4b. Derivacion a una persona. La derivacion del entrante marca la
//     CONVERSACION del contacto (paused_human, automatizacion en paused,
//     human_takeover), no el caso de fuente landing, que nace sin
//     conversacion: la reevaluacion compartida sola no la ve. La frena el
//     criterio blocked_handoff del primer toque de Johanna, en los tres
//     puntos: al planificar, al reevaluar y al arrancar.
// ---------------------------------------------------------------------------
{
  const HANDOFF = { policy: 'att1-derivacion-entrante', version: 1 };
  await db.query(`
    insert into public.human_handoff_projection_policies (
      policy_key, policy_version, scope_key, scope_version,
      inbound_scope_key, inbound_scope_version, expected_team_id,
      note_template_key, note_template_version, private_note_body, active
    ) values ($1,$2,null,null,$3,$4,$5,'att1-nota-derivacion',1,
      'Derivacion automatica de prueba.',true)
  `, [HANDOFF.policy, HANDOFF.version, ATT1.inboundScope, ATT1.inboundVersion,
    required(manifest.chatwoot?.equipo_derivacion, 'chatwoot.equipo_derivacion')]);
  let handoffConversation = 881000;
  // La persona escribe (la admision entrante, con el id que le pasa el
  // bridge) y el agente la deriva a una persona del equipo.
  const handOff = async (lead, externalUserId) => {
    handoffConversation += 1;
    const inbound = one((await db.query(`
      select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)
    `, [ATT1.inboundScope, ATT1.inboundVersion, handoffConversation, externalUserId])).rows,
    `${lead.label} inbound admission`);
    const handoff = one((await db.query(`
      select * from public.request_inbound_human_handoff(
        $1::uuid, $2, 'explicit_human_request', $3, $4, clock_timestamp(), null)
    `, [inbound.commercial_case_id, `handoff:first-contact:${handoffConversation}`,
      HANDOFF.policy, HANDOFF.version])).rows, `${lead.label} handoff`);
    const conversation = one((await db.query(`
      select status, automation_status, human_takeover
      from public.conversations where id = $1
    `, [inbound.conversation_id])).rows, `${lead.label} conversation`);
    if (handoff.outcome !== 'requested' || conversation.status !== 'paused_human'
        || conversation.automation_status !== 'paused' || conversation.human_takeover !== true) {
      throw new Error(`${lead.label}: the conversation was not handed off: ${JSON.stringify({ handoff: handoff.outcome, conversation })}`);
    }
    return inbound;
  };

  // a. Derivada antes del formulario (escribio desde el wa_id, 521...): no se
  //    planifica ni se toca nada, y el renglon nombra al contacto encontrado.
  const before = person('derivado-antes', 'MX');
  const beforeInbound = await handOff(before, before.whatsapp);
  const beforeFootprint = await footprint();
  const beforePlan = await submit(before, OPEN_SCOPE);
  const beforeResult = expectPlan('form after a handoff', beforePlan,
    'not_planned', 'precheckout_conversation_handoff');
  await expectNoFootprint('form after a handoff', beforeFootprint);
  if (beforePlan.contact !== beforeInbound.contact_id) {
    throw new Error('the plan row of a handed off person does not name the contact');
  }

  // b. Formulario primero; la persona escribe y la derivan antes del envio. El
  //    resolvedor entrante del bridge entrega la identidad que creo el plan
  //    (52...). La derivacion no toca la accion del caso landing: la cancela
  //    la reevaluacion del primer contacto.
  const between = person('derivado-entre-plan-y-envio', 'MX');
  const betweenPlan = await submit(between, OPEN_SCOPE);
  expectPlan('plan before a handoff', betweenPlan, 'planned', 'first_contact_scheduled');
  const betweenInbound = await handOff(between, between.plain);
  const untouched = await actionOf(betweenPlan.actionId);
  const betweenResult = await expectCancelled(between, betweenPlan, 'precheckout_conversation_handoff');

  // c. Derivada entre la reevaluacion y el arranque: el arranque la rechaza y
  //    no consume cupo.
  const late = person('derivado-en-vuelo', 'AR');
  const latePlan = await submit(late, OPEN_SCOPE);
  expectPlan('plan before a late handoff', latePlan, 'planned', 'first_contact_scheduled');
  const lateReservation = await reserve(late, latePlan);
  const lateInbound = await handOff(late, late.plain);
  const lateResult = await expectStartRejected('handoff in flight', latePlan,
    lateReservation, 'precheckout_conversation_handoff');

  if (betweenInbound.contact_id !== betweenPlan.contact || untouched.status !== 'pending'
      || lateInbound.contact_id !== latePlan.contact) {
    throw new Error(`handoff: ${JSON.stringify({ sameContact: betweenInbound.contact_id === betweenPlan.contact, action: untouched.status })}`);
  }
  results.handoff = {
    form_after_a_handoff: beforeResult,
    between_plan_and_send: betweenResult,
    between_reevaluation_and_start: lateResult,
  };
}

// ---------------------------------------------------------------------------
// 5. Compra.
// ---------------------------------------------------------------------------
{
  // a. Dos intenciones vivas de la persona (una por landing): la compra queda
  //    ambigua y ninguna pasa a purchased. Antes de la reevaluacion, cancela.
  const twoLandings = async (label) => {
    const lead = person(label, 'MX');
    const plan = await submit(lead, OPEN_SCOPE);
    expectPlan(`${label} plan`, plan, 'planned', 'first_contact_scheduled');
    const other = await submit(lead, OPEN_SCOPE, { landing: 'AR' });
    expectPlan(`${label} other landing`, other, 'not_planned', 'precheckout_contact_already_planned');
    if (other.intent === plan.intent || other.form.offer.offer_code === plan.form.offer.offer_code) {
      throw new Error(`${label}: the other landing did not open its own intent`);
    }
    return { lead, plan, other };
  };
  const expectAmbiguous = async (label, lead, plan, other) => {
    const purchase = await admitPurchase(lead);
    const states = [await intentOf(plan.intent), await intentOf(other.intent)];
    if (purchase.outcome !== 'ambiguous' || purchase.candidate_count !== 2
        || states.some((intent) => intent.lifecycle_state !== 'waiting_for_purchase')) {
      throw new Error(`${label}: the purchase was not ambiguous: ${JSON.stringify(purchase)}`);
    }
  };
  const early = await twoLandings('compra-ambigua');
  await expectAmbiguous('ambiguous purchase', early.lead, early.plan, early.other);
  const ambiguousBefore = await expectCancelled(early.lead, early.plan, 'intent_purchase_ambiguous');

  // b. La misma compra ambigua, entre la reevaluacion y el arranque: la
  //    autorizacion del piloto mira solo el estado de la intencion (viva), y
  //    la frena el arranque del primer contacto, sin consumir cupo.
  const late = await twoLandings('compra-ambigua-en-vuelo');
  const lateReservation = await reserve(late.lead, late.plan);
  await expectAmbiguous('ambiguous purchase in flight', late.lead, late.plan, late.other);
  const ambiguousAtStart = await expectStartRejected('ambiguous purchase in flight',
    late.plan, lateReservation, 'intent_purchase_ambiguous');

  // c. La compra resuelta entre la reevaluacion y el arranque: la intencion ya
  //    no esta viva y la autorizacion del piloto la rechaza.
  const resolved = person('compra-en-vuelo', 'AR');
  const resolvedPlan = await submit(resolved, OPEN_SCOPE);
  const resolvedReservation = await reserve(resolved, resolvedPlan);
  const resolvedPurchase = await admitPurchase(resolved);
  const resolvedAtStart = await expectStartRejected('purchase in flight', resolvedPlan,
    resolvedReservation, 'pilot_audience_consented_intent_not_live');

  // d. Una intencion de mas de 7 dias (el lookback de la compra de la
  //    instancia) reenviada hoy y comprada. El envio viejo ya vencio y no se
  //    planifica; el reenvio si, con su propia demora (la intencion conserva
  //    el submitted_at de hace 8 dias: con ese naceria vencido). La compra no
  //    encuentra una intencion tan vieja, asi que no queda purchased: la
  //    frena la compra por identidad.
  const old = person('intencion-vieja', 'MX');
  const STALE_AT = at(-8 * 24 * 60);
  const stale = await submit(old, OPEN_SCOPE, { submittedAt: STALE_AT });
  const staleResult = expectPlan('stale form', stale, 'not_planned', 'precheckout_submission_expired');
  const oldPlan = await submit(old, OPEN_SCOPE);
  expectPlan('resend of an old intent', oldPlan, 'planned', 'first_contact_scheduled');
  await expectPlanned('resend of an old intent', old, oldPlan);
  if (isoOf((await intentOf(oldPlan.intent)).submitted_at) !== STALE_AT.toISOString()
      || new Date(oldPlan.action.due_at).getTime() !== DUE.getTime() + GRACE_MINUTES * 60_000) {
    throw new Error('the resend of an old intent did not get the delay of its own submission');
  }
  const oldPurchase = await admitPurchase(old);
  const oldIntent = await intentOf(oldPlan.intent);
  const byIdentity = await expectCancelled(old, oldPlan, 'purchase_by_identity');

  // e. Quien ya compro y despues deja el formulario: no se planifica ni se
  //    crea el contacto.
  const customer = person('ya-compro', 'AR');
  const customerPurchase = await admitPurchase(customer, { approvedAt: at(-3 * 60) });
  const beforeCustomer = await footprint();
  const customerPlan = await submit(customer, OPEN_SCOPE);
  const customerResult = expectPlan('form after a purchase', customerPlan,
    'not_planned', 'purchase_by_identity');
  await expectNoFootprint('form after a purchase', beforeCustomer);

  if (resolvedPurchase.outcome !== 'resolved'
      || oldPlan.intent !== stale.intent
      || oldPurchase.outcome !== 'unmatched' || oldIntent.lifecycle_state !== 'waiting_for_purchase'
      || customerPurchase.outcome !== 'unmatched') {
    throw new Error(`purchase cases: ${JSON.stringify({ resolved: resolvedPurchase.outcome, old: oldPurchase.outcome, oldIntent: oldIntent.lifecycle_state, customer: customerPurchase.outcome })}`);
  }
  results.purchase = {
    ambiguous_before_reevaluation: ambiguousBefore,
    ambiguous_between_reevaluation_and_start: ambiguousAtStart,
    resolved_between_reevaluation_and_start: resolvedAtStart,
    stale_form: staleResult,
    old_intent_resent_and_purchased: byIdentity,
    form_after_a_purchase: customerResult,
  };
}

// ---------------------------------------------------------------------------
// 6. Carrito o pago fallido que lo reemplazan. Hotmart informa algo mas preciso
//    de la intencion: el primer contacto se cancela y le escribe el flujo de
//    recuperacion, sobre el contacto que creo el formulario.
// ---------------------------------------------------------------------------
{
  const startOperationFor = (anchorType) => (anchorType === 'payment_failure'
    ? 'mark_portable_payment_failure_request_started'
    : 'mark_lancemos_pilot_request_started');
  // La cadena de recuperacion de Hotmart, con la reevaluacion compartida.
  const dispatchRecovery = async (lead, plan, anchor) => {
    const claim = await claimPlan(lead, { actionId: plan.scheduled_action_id }, anchor);
    const decision = one((await db.query(`
      select * from public.reevaluate_followup_action($1,$2,$3,$4)
    `, [plan.scheduled_action_id, claim.worker, claim.lease, claim.now])).rows,
    `${lead.label} recovery reevaluation`);
    if (decision.decision !== 'execute') {
      throw new Error(`${lead.label}: the recovery did not execute: ${JSON.stringify(decision)}`);
    }
    const attempt = one((await db.query(`
      select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
    `, [plan.scheduled_action_id, claim.worker, claim.lease, decision.case_version,
      decision.sequence_revision, claim.now])).rows, `${lead.label} recovery reservation`);
    await db.query(`select * from public.${startOperationFor(anchor)}($1,$2,$3,$4,$5)`,
      [plan.scheduled_action_id, attempt.id, claim.worker, claim.lease, await dbNow()]);
    messageNumber += 1;
    const accepted = one((await db.query(`
      select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
    `, [plan.scheduled_action_id, attempt.id, claim.worker, claim.lease,
      String(930100 + messageNumber), `att1-first-contact-wamid-${messageNumber}`,
      `Plantilla aprobada de ATT1, recuperacion (${ATT1.productName})`, await dbNow()])).rows,
    `${lead.label} recovery acceptance`);
    return accepted.status;
  };

  // a. El carrito, con el telefono de Hotmart (549...) contra el formulario
  //    (54...): la correlacion clasifica la intencion y el primer contacto se
  //    cancela. El carrito se planifica en su scope y, con ese caso abierto,
  //    otro formulario (por la otra landing, otra intencion) no planifica.
  const cartLead = person('carrito-lo-reemplaza', 'AR');
  const cartFirst = await submit(cartLead, OPEN_SCOPE);
  expectPlan('first contact before the cart', cartFirst, 'planned', 'first_contact_scheduled');
  const cart = await admitCart(cartLead);
  const cartIntent = await intentOf(cartFirst.intent);
  const cartSuperseded = await expectCancelled(cartLead, cartFirst, 'superseded_by_provider_event');
  await addHotmartPoints(cartLead, cart.eventId, cart.phone);
  const cartPlan = one((await db.query(`
    select * from public.plan_lancemos_pilot_cart_recovery($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
  `, [cart.eventId, cartLead.contact, String(ATT1.productId), ATT1.productName,
    cart.offer.offer_code, recoveryPolicy.policy_key, recoveryPolicy.version,
    cart.abandonedAt.toISOString(), ATT1.accountId, ATT1.inboxId, cart.phone,
    HOTMART_SCOPE.key, HOTMART_SCOPE.version])).rows, 'cart plan');
  const beforeOtherLanding = await footprint();
  const withOpenCase = await submit(cartLead, OPEN_SCOPE, { landing: 'MX' });
  const withOpenCaseResult = expectPlan('form with an open recovery case', withOpenCase,
    'not_planned', 'superseded_by_provider_event');
  if ((await intentOf(withOpenCase.intent)).current_classification !== null
      || withOpenCase.intent === cartFirst.intent) {
    throw new Error('the other landing did not open an unclassified intent');
  }
  await expectNoFootprint('form with an open recovery case', {
    ...beforeOtherLanding, contact_points: (await footprint()).contact_points,
  });
  const cartSent = await dispatchRecovery(cartLead, cartPlan, 'cart_abandonment');

  // b. El pago fallido. El permiso que dejo el primer contacto sigue activo:
  //    el pago fallido se planifica y no suma otro.
  const failureLead = person('pago-fallido-lo-reemplaza', 'MX');
  const failureFirst = await submit(failureLead, OPEN_SCOPE);
  expectPlan('first contact before the payment failure', failureFirst, 'planned', 'first_contact_scheduled');
  const failure = await admitFailure(failureLead);
  const failureIntent = await intentOf(failureFirst.intent);
  const failureSuperseded = await expectCancelled(failureLead, failureFirst, 'superseded_by_provider_event');
  await addHotmartPoints(failureLead, failure.eventId, failure.phone);
  const failurePlan = one((await db.query(`
    select * from public.plan_portable_payment_failure_recovery($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
  `, [failure.eventId, failureLead.contact, String(ATT1.productId), ATT1.productName,
    failureLead.offer.offer_code, recoveryPolicy.policy_key, recoveryPolicy.version,
    failure.failedAt.toISOString(), ATT1.accountId, ATT1.inboxId, failure.phone,
    HOTMART_SCOPE.key, HOTMART_SCOPE.version])).rows, 'payment failure plan');
  const failureGrants = await activeAllowed(failureLead.contact);
  const failureSent = await dispatchRecovery(failureLead, failurePlan, 'payment_failure');
  // Con el caso de Hotmart cerrado, la intencion sigue clasificada: otro
  // formulario de esa oferta tampoco dispara el primer contacto.
  const afterFailure = await submit(failureLead, OPEN_SCOPE);
  const afterFailureResult = expectPlan('form after the payment failure', afterFailure,
    'not_planned', 'superseded_by_provider_event');

  if (cartIntent.current_classification !== 'confirmed_abandonment'
      || failureIntent.current_classification !== 'payment_failure_supported'
      || !cartPlan.created || !failurePlan.created
      || failureGrants.length !== 1
      || failureGrants[0].evidence.recovery_case_id !== failureFirst.caseId
      || cartSent !== 'accepted_by_chatwoot' || failureSent !== 'accepted_by_chatwoot') {
    throw new Error(`superseded: ${JSON.stringify({ cart: cartIntent.current_classification, failure: failureIntent.current_classification, grants: failureGrants.length, cartSent, failureSent })}`);
  }
  results.superseded = {
    cart: `${cartSuperseded}, cart recovery ${cartSent}`,
    form_with_open_recovery_case: withOpenCaseResult,
    payment_failure: `${failureSuperseded}, payment failure recovery ${failureSent}`,
    form_after_the_payment_failure: afterFailureResult,
  };
}

// ---------------------------------------------------------------------------
// 7. El consentimiento se pierde entre el plan y el envio, por caminos reales.
// ---------------------------------------------------------------------------
{
  // a. El mismo envio vuelve a llegar con otro contenido: la admision lo
  //    registra como conflicto sin resolver y ese envio deja de ser evidencia
  //    de consentimiento. La intencion sigue viva: authorization lost.
  const conflicted = person('consentimiento-perdido', 'AR');
  const conflictedPlan = await submit(conflicted, OPEN_SCOPE);
  expectPlan('lost consent plan', conflictedPlan, 'planned', 'first_contact_scheduled');
  const tampered = structuredClone(conflictedPlan.form);
  tampered.raw.data.buyer.name = `${conflicted.name} Dos`;
  tampered.canonical.lead.full_name = `${conflicted.name} Dos`;
  const conflict = one((await deliver(tampered, OPEN_SCOPE)).rows, 'conflicting delivery');
  const lost = await expectCancelled(conflicted, conflictedPlan, 'precheckout_authorization_lost',
    { detail: 'pilot_audience_consented_intent_submission_missing' });

  // b. Otro formulario de la misma oferta con otro telefono: la admision deja
  //    la intencion en identity_conflict y sin permisos. Ya no esta viva.
  const changed = person('otro-telefono', 'MX');
  const changedPlan = await submit(changed, OPEN_SCOPE);
  expectPlan('identity conflict plan', changedPlan, 'planned', 'first_contact_scheduled');
  const otherPhone = await submit(changed, OPEN_SCOPE, { phone: changed.other });
  const otherPhoneResult = expectPlan('form with another phone', otherPhone,
    'not_planned', 'precheckout_intent_not_live');
  const notLive = await expectCancelled(changed, changedPlan, 'precheckout_intent_not_live');

  if (conflict.outcome !== 'semantic_conflict' || conflict.submission_id !== conflictedPlan.submission
      || conflict.plan_outcome !== 'planned'
      || otherPhone.intent !== changedPlan.intent
      || (await intentOf(changedPlan.intent)).current_classification !== 'identity_conflict') {
    throw new Error(`lost consent: ${JSON.stringify({ conflict: conflict.outcome, plan: conflict.plan_outcome })}`);
  }
  results.lost_consent = {
    conflicting_redelivery: lost,
    form_with_another_phone: otherPhoneResult,
    intent_in_identity_conflict: notLive,
  };
}

// ---------------------------------------------------------------------------
// 8. El scope.
// ---------------------------------------------------------------------------
{
  const lead = person('scope', 'MX');
  const before = await footprint();
  const manual = await submit(lead, MANUAL_SCOPE);
  const manualResult = expectPlan('manual_cohort scope', manual,
    'not_planned', 'precheckout_scope_audience_unsupported');
  const manualStatus = await statusOf(MANUAL_SCOPE);
  const unpublished = await submit(lead, { ...OPEN_SCOPE, key: 'att1-primer-contacto-sin-publicar' });
  const unpublishedResult = expectPlan('unpublished scope', unpublished,
    'not_planned', 'pilot_scope_not_published');
  const otherVersion = await submit(lead, { ...OPEN_SCOPE, version: OPEN_SCOPE.version + 1 });
  const otherVersionResult = expectPlan('unpublished scope version', otherVersion,
    'not_planned', 'pilot_scope_not_published');
  const hotmart = await submit(lead, HOTMART_SCOPE);
  const hotmartResult = expectPlan('hotmart scope', hotmart,
    'not_planned', 'pilot_source_event_mismatch');
  const hotmartStatus = await statusOf(HOTMART_SCOPE);
  await expectNoFootprint('scope rejections', before);
  // Sin scope no se admite nada: es un error del llamador, no un rechazo.
  const submissionsBefore = one((await db.query(
    'select count(*)::integer as count from public.precheckout_submissions',
  )).rows, 'submissions').count;
  let invalid = null;
  try {
    await deliver(formOf(lead), { key: '', version: 1 });
  } catch (caught) {
    invalid = caught;
  }
  const submissionsAfter = one((await db.query(
    'select count(*)::integer as count from public.precheckout_submissions',
  )).rows, 'submissions').count;
  if (manualStatus.configured !== false || manualStatus.reason_code !== 'pilot_scope_config_mismatch'
      || hotmartStatus.configured !== false || hotmartStatus.reason_code !== 'pilot_scope_config_mismatch'
      || (await statusOf(OPEN_SCOPE)).reason_code !== 'pilot_runtime_armed'
      || (await statusOf({ ...OPEN_SCOPE, version: 2 })).reason_code !== 'pilot_scope_config_mismatch'
      || invalid?.code !== '22023' || invalid?.message !== 'invalid_pilot_plan_parameters'
      || submissionsAfter !== submissionsBefore) {
    throw new Error(`scope: ${JSON.stringify({ manualStatus, hotmartStatus, invalid: invalid?.message })}`);
  }
  results.scope = {
    manual_cohort: manualResult,
    unpublished: unpublishedResult,
    unpublished_version: otherVersionResult,
    hotmart_scope: hotmartResult,
    status_of_a_manual_or_hotmart_scope: manualStatus.reason_code,
    without_scope: `${invalid.code}:${invalid.message}`,
  };
}

// ---------------------------------------------------------------------------
// 9. Los triggers diferidos corren adentro del bloque del plan.
// ---------------------------------------------------------------------------
{
  // a. Una compra aprobada que quedo en received (admitida por el camino
  //    compartido, antes del formulario): el trigger de compra conocida cierra
  //    el caso al nacer. Corre adentro de la RPC (el estado ya esta cuando
  //    vuelve, antes del commit), la admision no falla y el renglon lo dice.
  const known = person('compra-conocida', 'MX');
  const knownPurchaseEvent = await admitSharedPurchase(known, { approvedAt: at(-5) });
  const knownForm = formOf(known, { submittedAt: at(-8) });
  await db.exec('begin');
  let knownAdmission;
  let knownInside;
  try {
    knownAdmission = one((await deliver(knownForm, OPEN_SCOPE)).rows, 'known purchase form');
    knownInside = one((await db.query(`
      select rc.status, rc.purchase_event_id, plan.outcome, plan.reason_code
      from public.portable_precheckout_first_contact_plans plan
      join public.recovery_cases rc on rc.id = plan.recovery_case_id
      where plan.submission_id = $1
    `, [knownAdmission.submission_id])).rows, 'known purchase inside the transaction');
    await db.exec('commit');
  } catch (caught) {
    await db.exec('rollback');
    throw caught;
  }
  const knownEvent = one((await db.query(`
    select processing_status from public.webhook_events where id = $1
  `, [knownPurchaseEvent])).rows, 'known purchase event');
  if (knownAdmission.outcome !== 'inserted' || knownAdmission.plan_outcome !== 'planned'
      || knownAdmission.plan_reason !== 'purchase_detected'
      || knownInside.status !== 'won' || knownInside.purchase_event_id !== knownPurchaseEvent
      || knownInside.reason_code !== 'purchase_detected'
      || knownEvent.processing_status !== 'processed'
      || (await claimNow('compra-conocida')).rows.length !== 0) {
    throw new Error(`known purchase: ${JSON.stringify({ admission: knownAdmission.outcome, plan: knownAdmission.plan_outcome, reason: knownAdmission.plan_reason, inside: knownInside.status })}`);
  }

  // b. El trigger diferido falla. Se arma con un estado inconsistente a
  //    proposito (el unico cambio directo de estado de este validador): otra
  //    compra en received que un caso ya cerrado tiene tomada como su compra.
  //    Al atribuirla, el trigger choca con el indice unico (23505). Sin el set
  //    constraints del bloque, ese error saldria en el commit y se llevaria la
  //    admision; con el, queda plan_failed y el formulario admitido.
  const broken = person('trigger-diferido-falla', 'AR');
  const brokenPurchaseEvent = await admitSharedPurchase(broken, { approvedAt: at(-5) });
  const donor = one((await db.query(`
    select id from public.recovery_cases
    where status = 'expired' and source = 'landing' limit 1
  `)).rows, 'a closed case');
  await db.query(`
    update public.recovery_cases
    set status = 'won', won_at = clock_timestamp(), purchase_event_id = $2
    where id = $1
  `, [donor.id, brokenPurchaseEvent]);
  const beforeBroken = await footprint();
  const brokenPlan = await submit(broken, OPEN_SCOPE, { submittedAt: at(-8) });
  const brokenResult = expectPlan('failing deferred trigger', brokenPlan,
    'plan_failed', 'plan_error_unclassified');
  await expectNoFootprint('failing deferred trigger', beforeBroken);
  if (brokenPlan.sqlstate !== '23505' || brokenPlan.contact !== null) {
    throw new Error(`failing deferred trigger: ${JSON.stringify({ sqlstate: brokenPlan.sqlstate })}`);
  }
  results.deferred_triggers = {
    known_purchase_inside_the_call: `${knownAdmission.plan_outcome}:${knownAdmission.plan_reason}, case ${knownInside.status}`,
    failing_trigger_contained: `${brokenResult}:${brokenPlan.sqlstate}, admission inserted`,
  };
}

// ---------------------------------------------------------------------------
// 10. Un error del planificador no tumba la admision: la identidad del
//     telefono es de otro inbox de la misma cuenta (23514).
// ---------------------------------------------------------------------------
{
  const lead = person('otro-inbox', 'MX');
  const inbound = one((await db.query(`
    select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)
  `, [OTHER_INBOX.key, OTHER_INBOX.version, 880002, lead.plain])).rows, 'other inbox admission');
  const before = await footprint();
  const plan = await submit(lead, OPEN_SCOPE);
  const result = expectPlan('identity of another inbox', plan,
    'plan_failed', 'channel_identity_inbox_mismatch');
  await expectNoFootprint('identity of another inbox', before);
  const contact = await contactOf(inbound.contact_id);
  const intent = await intentOf(plan.intent);
  if (plan.sqlstate !== '23514' || plan.contact !== inbound.contact_id
      || contact.phone !== null || contact.full_name !== null
      || intent.lifecycle_state !== 'waiting_for_purchase'
      || intent.whatsapp_contact_authorized !== true) {
    throw new Error(`identity of another inbox: ${JSON.stringify({ sqlstate: plan.sqlstate, phone: contact.phone === null })}`);
  }
  results.planner_error_contained = `${result}:${plan.sqlstate}, admission inserted, nothing created`;
}

// ---------------------------------------------------------------------------
// 11. El envio sin opt-in, F1 (el telefono del contacto) y el contacto ambiguo.
// ---------------------------------------------------------------------------
{
  // a. Un envio 1.0.0, sin opt-in: la intencion no tiene los permisos.
  const withoutConsent = person('sin-opt-in', 'AR');
  const before = await footprint();
  const noOptIn = await submit(withoutConsent, OPEN_SCOPE, { consented: false });
  const noOptInResult = expectPlan('form without opt-in', noOptIn,
    'not_planned', 'precheckout_intent_not_authorized');
  await expectNoFootprint('form without opt-in', before);
  // Un envio 1.0.0 sobre una intencion que ya consintio no dispara nada: el
  // envio que dispara es el que trae el consentimiento.
  const consented = person('opt-in-y-despues-sin', 'MX');
  const consentedPlan = await submit(consented, OPEN_SCOPE, { submittedAt: WAITING });
  expectPlan('consented plan', consentedPlan, 'planned', 'first_contact_scheduled');
  const later = await submit(consented, OPEN_SCOPE, { consented: false });
  const laterResult = expectPlan('later form without opt-in', later,
    'not_planned', 'precheckout_submission_not_consented');
  if (later.intent !== consentedPlan.intent
      || (await intentOf(later.intent)).whatsapp_contact_authorized !== true) {
    throw new Error('the later form without opt-in changed the intent');
  }

  // b. F1: el contacto se encuentra por email y tiene otro numero en
  //    contacts.phone, que es adonde saldria el envio. No se planifica, no se
  //    pisa el telefono y no queda nada de lo que el bloque alcanzo a hacer.
  const otherNumber = person('f1-otro-numero', 'MX');
  await seedContact(otherNumber, { contactPhone: otherNumber.other, pointPhone: otherNumber.other });
  const beforeOtherNumber = await footprint();
  const otherNumberPlan = await submit(otherNumber, OPEN_SCOPE);
  const otherNumberResult = expectPlan('F1 with another number', otherNumberPlan,
    'not_planned', 'pilot_audience_consented_intent_contact_phone_mismatch');
  await expectNoFootprint('F1 with another number', beforeOtherNumber);
  if ((await contactOf(otherNumber.contact)).phone !== otherNumber.other) {
    throw new Error('F1 overwrote the phone of the contact');
  }
  // Con '+', espacios y la otra forma es el mismo movil: planifica.
  const formatted = person('f1-con-formato', 'MX');
  await seedContact(formatted, {
    contactPhone: `+52 1 ${formatted.plain.slice(2, 4)} ${formatted.plain.slice(4, 8)} ${formatted.plain.slice(8)}`,
    pointPhone: formatted.whatsapp,
  });
  const formattedPlan = await submit(formatted, OPEN_SCOPE, { submittedAt: WAITING });
  expectPlan('F1 with a formatted phone', formattedPlan, 'planned', 'first_contact_scheduled');
  await expectPlanned('F1 with a formatted phone', formatted, formattedPlan);
  if ((await pointsOf(formatted.contact)).length !== 2) {
    throw new Error('the form added a phone point to a contact that already had the other form');
  }

  // c. El email es de un contacto y el telefono de otro: dos duenos.
  const ambiguous = person('contacto-ambiguo', 'AR');
  const stranger = person('otro-dueno', 'AR');
  await seedContact(ambiguous, { pointPhone: ambiguous.other, contactPhone: ambiguous.other });
  await seedContact(stranger, { pointPhone: ambiguous.whatsapp, contactPhone: ambiguous.whatsapp });
  const beforeAmbiguous = await footprint();
  const ambiguousPlan = await submit(ambiguous, OPEN_SCOPE);
  const ambiguousResult = expectPlan('ambiguous contact', ambiguousPlan,
    'not_planned', 'precheckout_contact_ambiguous');
  await expectNoFootprint('ambiguous contact', beforeAmbiguous);
  results.consent_and_contact = {
    form_without_opt_in: noOptInResult,
    later_form_without_opt_in: laterResult,
    contact_phone_is_another_number: otherNumberResult,
    contact_phone_formatted_other_form: 'planned',
    two_owners: ambiguousResult,
  };
}

// ---------------------------------------------------------------------------
// 12. Cada ancla por su RPC. Una accion de carrito no pasa por la reevaluacion
//     ni por el arranque del primer contacto, y una accion de primer contacto
//     no arranca por las RPC del carrito ni del pago fallido (lo que haria un
//     bridge anterior: falla cerrado).
// ---------------------------------------------------------------------------
{
  const expectRejected = async (label, action, { code, message, detail }) => {
    let error = null;
    try {
      await action();
    } catch (caught) {
      error = caught;
    }
    if (error?.code !== code || error?.message !== message
        || (detail !== undefined && error?.detail !== detail)) {
      throw new Error(`${label}: expected ${code} ${message} ${detail ?? ''}, got ${error?.code} ${error?.message} ${error?.detail}`);
    }
    return `${error.message}${error.detail ? `:${error.detail}` : ''}`;
  };
  // Una accion de carrito, reclamada.
  const cartLead = person('ancla-carrito', 'AR');
  cartLead.intent = one((await db.query(`
    select * from public.admit_portable_observed_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb)
  `, (() => {
    const form = formOf(cartLead);
    return [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, form.id,
      JSON.stringify(form.raw), JSON.stringify(form.canonical)];
  })())).rows, 'cart anchor form').purchase_intent_id;
  const cart = await admitCart(cartLead);
  await seedContact(cartLead, { contactPhone: cart.phone, pointPhone: cart.phone });
  await addHotmartPoints(cartLead, cart.eventId, cart.phone);
  const cartPlan = one((await db.query(`
    select * from public.plan_lancemos_pilot_cart_recovery($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
  `, [cart.eventId, cartLead.contact, String(ATT1.productId), ATT1.productName,
    cart.offer.offer_code, recoveryPolicy.policy_key, recoveryPolicy.version,
    cart.abandonedAt.toISOString(), ATT1.accountId, ATT1.inboxId, cart.phone,
    HOTMART_SCOPE.key, HOTMART_SCOPE.version])).rows, 'cart anchor plan');
  const cartClaim = await claimPlan(cartLead, { actionId: cartPlan.scheduled_action_id },
    'cart_abandonment');
  const wrapperOnCart = await expectRejected('first contact reevaluation on a cart action',
    () => reevaluate({ actionId: cartPlan.scheduled_action_id }, cartClaim),
    { code: '55000', message: 'precheckout_intent_action_required' });
  const cartDecision = one((await db.query(`
    select * from public.reevaluate_followup_action($1,$2,$3,$4)
  `, [cartPlan.scheduled_action_id, cartClaim.worker, cartClaim.lease, cartClaim.now])).rows,
  'cart reevaluation');
  const cartAttempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
  `, [cartPlan.scheduled_action_id, cartClaim.worker, cartClaim.lease, cartDecision.case_version,
    cartDecision.sequence_revision, cartClaim.now])).rows, 'cart reservation');
  const startOnCart = await expectRejected('first contact start on a cart action',
    () => start({ actionId: cartPlan.scheduled_action_id },
      { attempt: cartAttempt, worker: cartClaim.worker, lease: cartClaim.lease }),
    { code: '55000', message: 'pilot_request_start_rejected', detail: 'precheckout_intent_action_required' });
  // El carrito sale por su RPC: la base queda sin trabajo vencido.
  await db.query('select * from public.mark_lancemos_pilot_request_started($1,$2,$3,$4,$5)',
    [cartPlan.scheduled_action_id, cartAttempt.id, cartClaim.worker, cartClaim.lease, await dbNow()]);
  messageNumber += 1;
  await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
  `, [cartPlan.scheduled_action_id, cartAttempt.id, cartClaim.worker, cartClaim.lease,
    String(930100 + messageNumber), `att1-first-contact-wamid-${messageNumber}`,
    `Plantilla aprobada de ATT1, recuperacion (${ATT1.productName})`, await dbNow()]);

  // Una accion de primer contacto, reservada, por las RPC de las otras anclas.
  const lead = person('ancla-primer-contacto', 'MX');
  const plan = await submit(lead, OPEN_SCOPE);
  expectPlan('anchor plan', plan, 'planned', 'first_contact_scheduled');
  const reservation = await reserve(lead, plan);
  const args = [plan.actionId, reservation.attempt.id, reservation.worker, reservation.lease];
  const cartStart = await expectRejected('cart start on a first contact action',
    async () => db.query('select * from public.mark_lancemos_pilot_request_started($1,$2,$3,$4,$5)',
      [...args, await dbNow()]),
    { code: '55000', message: 'pilot_request_start_rejected', detail: 'pilot_source_event_mismatch' });
  const failureStart = await expectRejected('payment failure start on a first contact action',
    async () => db.query('select * from public.mark_portable_payment_failure_request_started($1,$2,$3,$4,$5)',
      [...args, await dbNow()]),
    { code: '55000', message: 'pilot_request_start_rejected', detail: 'payment_failure_action_required' });
  // La reevaluacion compartida, sola, ejecutaria la accion sin mirar los
  // frenos del formulario: por eso el bridge elige la RPC por el ancla.
  if ((await startsOf(OPEN_SCOPE)) < 1 || cartDecision.decision !== 'execute') {
    throw new Error('anchor routing setup diverged');
  }
  const started = one((await start(plan, reservation)).rows, 'anchor start');
  messageNumber += 1;
  await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
  `, [plan.actionId, reservation.attempt.id, reservation.worker, reservation.lease,
    String(930100 + messageNumber), `att1-first-contact-wamid-${messageNumber}`,
    `Plantilla aprobada de ATT1, primer contacto (${ATT1.productName})`, await dbNow()]);
  if (started.phase !== 'request_started') throw new Error('the first contact did not start by its own RPC');
  results.anchor_routing = {
    first_contact_reevaluation_on_cart: wrapperOnCart,
    first_contact_start_on_cart: startOnCart,
    cart_start_on_first_contact: cartStart,
    payment_failure_start_on_first_contact: failureStart,
  };
}

// ---------------------------------------------------------------------------
// 13. Privacidad y ACL.
// ---------------------------------------------------------------------------
{
  // El renglon del plan: ids, codigos y fecha. Ninguna columna de texto libre.
  const columns = (await db.query(`
    select column_name, data_type from information_schema.columns
    where table_schema = 'public' and table_name = 'portable_precheckout_first_contact_plans'
    order by ordinal_position
  `)).rows.map((row) => `${row.column_name}:${row.data_type}`);
  const expectedColumns = [
    'submission_id:uuid', 'purchase_intent_id:uuid', 'contact_id:uuid',
    'recovery_case_id:uuid', 'scope_key:text', 'scope_version:integer', 'outcome:text',
    'reason_code:text', 'error_sqlstate:text', `created_at:${TS}`,
  ];
  const ledger = one((await db.query(`
    select count(*)::integer as rows,
           count(*) filter (where outcome = 'planned')::integer as planned,
           count(*) filter (where outcome = 'not_planned')::integer as not_planned,
           count(*) filter (where outcome = 'plan_failed')::integer as plan_failed,
           bool_and(reason_code ~ '^[a-z0-9_]{1,64}$') as coded,
           bool_and(scope_key = any($1::text[])) as scopes,
           (select count(*)::integer from public.precheckout_submissions) as submissions
    from public.portable_precheckout_first_contact_plans
  `, [[FIXTURE_SCOPE.key, OPEN_SCOPE.key, MANUAL_SCOPE.key, HOTMART_SCOPE.key,
    'att1-primer-contacto-sin-publicar']])).rows, 'ledger');
  // Todas las anclas de fuente system: payload de tres claves, sin datos de la
  // persona, y ninguna queda en received (el worker de resolucion no las toma).
  const anchors = one((await db.query(`
    select count(*)::integer as rows,
           bool_and(processing_status = 'processed') as processed,
           bool_and((select array_agg(key order by key) from jsonb_object_keys(payload) key)
             = array['creation_date','precheckout_submission_id','purchase_intent_id']) as ids_only,
           bool_and(payload::text !~ '@' and payload::text !~ '555504') as no_personal_data
    from public.webhook_events where source = 'system'
  `)).rows, 'anchors');
  // Los casos de fuente landing: todos del primer contacto.
  const cases = one((await db.query(`
    select count(*)::integer as rows,
           bool_and(context ->> 'trigger_kind' = 'precheckout_intent') as kind,
           bool_and(context::text !~ '@' and context::text !~ '555504') as no_personal_data
    from public.recovery_cases where source = 'landing'
  `)).rows, 'landing cases');
  if (!same(columns, expectedColumns)
      || ledger.planned + ledger.not_planned + ledger.plan_failed !== ledger.rows
      || ledger.planned === 0 || ledger.not_planned === 0 || ledger.plan_failed !== 2
      || ledger.coded !== true || ledger.scopes !== true
      || anchors.rows !== ledger.planned || !anchors.processed || !anchors.ids_only
      || !anchors.no_personal_data
      || cases.rows !== ledger.planned || !cases.kind || !cases.no_personal_data) {
    throw new Error(`privacy: ${JSON.stringify({ columns, ledger, anchors, cases })}`);
  }

  // service_role ejecuta los cuatro entrypoints (asi corrio todo lo de arriba)
  // y nada mas: ni los helpers, ni la tabla.
  const denied = {};
  for (const [name, sql] of Object.entries({
    table: 'select count(*) from public.portable_precheckout_first_contact_plans',
    stop_reason: 'select public._portable_precheckout_stop_reason(gen_random_uuid(), null)',
    find_contact: 'select * from public._find_portable_precheckout_contact(gen_random_uuid())',
    ensure_contact: 'select public._ensure_portable_precheckout_contact(gen_random_uuid(), gen_random_uuid())',
    planner: "select * from public._plan_portable_precheckout_first_contact(gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 'x', 1)",
  })) {
    let error = null;
    try {
      await asService(() => db.query(sql));
    } catch (caught) {
      error = caught;
    }
    denied[name] = error?.code ?? 'allowed';
  }
  const table = one((await db.query(`
    select c.relrowsecurity as rls,
           has_table_privilege('service_role', c.oid, 'select') as service_select,
           has_table_privilege('service_role', c.oid, 'insert') as service_insert,
           has_table_privilege('anon', c.oid, 'select') as anon_select,
           has_table_privilege('authenticated', c.oid, 'select') as authenticated_select
    from pg_class c
    where c.oid = 'public.portable_precheckout_first_contact_plans'::regclass
  `)).rows, 'table ACL');
  if (Object.values(denied).some((code) => code !== '42501')
      || table.rls !== true || table.service_select || table.service_insert
      || table.anon_select || table.authenticated_select) {
    throw new Error(`ACL: ${JSON.stringify({ denied, table })}`);
  }
  results.privacy_and_acl = {
    plan_rows: `${ledger.planned} planned, ${ledger.not_planned} not planned, ${ledger.plan_failed} failed, of ${ledger.submissions} submissions`,
    anchors_with_ids_only: anchors.rows,
    service_role_denied: Object.keys(denied).length,
  };
}

// ---------------------------------------------------------------------------
// 14. El inventario de esquema reconoce esta migracion y las que definen las
//     funciones en las que delega.
// ---------------------------------------------------------------------------
const inventory = (await db.query(readFileSync(
  join(root, 'scripts/supabase_schema_inventory.sql'), 'utf8',
))).rows;
const FINGERPRINTS = [
  '20260903000300', '20260929000200', '20260930000100', '20260930000200',
  '20260930000300', '20261001000100', '20261001000200',
];
for (const version of FINGERPRINTS) {
  const row = inventory.find((candidate) => candidate.version === version);
  if (row?.fingerprint_status !== 'fingerprint_present') {
    throw new Error(`schema fingerprint ${version}: ${JSON.stringify(row)}`);
  }
}
results.schema_fingerprints = FINGERPRINTS.length;

console.log(JSON.stringify({ portable_precheckout_first_contact: 'OK', ...results }));
await db.close();
