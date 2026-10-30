// El motor nativo arranca con el pago rechazado (migracion 20261030000200,
// E-207, M1b, P18 del mapa de M16).
//
// Por que: el pago rechazado de Maribel corre por el legado (su scope propio,
// maribel-pago-rechazado) y el Automation Expert solo crea flujos nativos, que
// sabian la compra y el carrito.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera): el formulario con consentimiento y el rechazo pasan por las
// admisiones portables de hoy, que correlacionan el rechazo con la intencion,
// como en produccion. Los rechazos son los capturados de Maribel
// (tests/fixtures/hotmart_purchase_canceled_maribel_20261028.json, dos de ellos
// de la misma persona con 24 segundos de diferencia), cada uno con la persona
// sembrada; la compra, la capturada de la bienvenida de ATT1. El paso nativo
// nombra la plantilla de Maribel (maribel_retomar_proceso_01, la de
// WABA_PAYMENT_FAILURE_TEMPLATE_NAME en Maribel).
//
// Prueba:
//   0. la forma: cambian exactamente las funciones del encargo y nace la del
//      permiso, sin permisos de la API ni otros atributos; correrla dos veces
//      aborta;
//   1. el flujo de Maribel compila a la politica de hoy (la medida en
//      maribel_db el 2026-10-30: gracia 15 min, 09:00-21:00 los siete dias en
//      CDMX, vence a 1 dia, un mensaje);
//   2. la admision: en cohorte arranca (el caso con offer_code nulo, la
//      intencion en la fila del recorrido, la autorizacion con la evidencia del
//      formulario, el primer envio a los 15 min); fuera de la cohorte
//      fuera_de_cohorte; sin formulario no_entra:permiso; un pendiente cancelado
//      que no es un rechazo, no_es_un_rechazo; rojo: con la forma del evento de
//      antes, invalid_flow_engine_event;
//   3. dos rechazos seguidos de la misma persona: el segundo no abre otro caso
//      (ya_en_curso) y corre el envio a la gracia del ultimo; rojo: sin el
//      bloque, el envio queda a la gracia del primero;
//   4. el despacho: el contexto del paso con entry_permission formulario y la
//      plantilla de Maribel, el arranque autoriza contra la bolsa y la
//      aceptacion cierra; rojo: con la intencion caida, el arranque rechaza; un
//      rechazo dentro de las 72 h del caso que ya mando es ya_disparado;
//   5. la compra lo corta antes del envio (purchase_detected_any_offer): el
//      rechazo deja el binding portable que lee el freno;
//   6. el doble caso (a): con un caso del pago rechazado del legado abierto, el
//      nativo no arranca (caso_del_legado_en_curso) y el legado lo suma a su
//      caso como hoy; rojo: sin el bloque, el nativo abria el segundo caso;
//   7. el doble caso (b): con un pago rechazado nativo abierto y el nativo
//      pausado, el planificador del legado del pago y el del carrito no abren
//      otro caso (flujo_nativo_en_curso, nada escrito); rojo con las
//      definiciones de antes;
//   8. D6: un carrito nativo con no_arranca_mientras: [pago_rechazado] no
//      arranca con un pago rechazado nativo abierto;
//   9. lo de antes, igual: sin un flujo nativo del pago rechazado del producto,
//      sin_flujo; el legado del pago rechazado planifica como hoy; la
//      simulacion cuenta solo los rechazos.
//
// Con --emit escribe tests/fixtures/flow_engine/native_payment_failure_rows.json
// (las filas de la admision y el error del planificador, como los contesta la
// base) para tests/test_flow_engine_native_payment_failure.py; con --check (el
// de npm test) lo compara y falla si difiere.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, errorOf, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261030000200_flow_engine_native_payment_failure_v1.sql';
const FIXTURE = join(root, 'tests/fixtures/flow_engine/native_payment_failure_rows.json');
const EMIT = process.argv.includes('--emit');
const CHECK = process.argv.includes('--check');
const TZ = 'timestamp with time zone';
const ADMIT_EVENT = 'admit_flow_engine_event_v1(jsonb,jsonb)';
const ADMIT_NATIVE = `_flow_engine_admit_native_flow(flow_controls,webhook_events,uuid,text,${TZ},bigint,bigint,text)`;
const PLAN_CART_BASE = `plan_cart_recovery(uuid,uuid,text,text,text,text,integer,${TZ})`;
const PLAN_FAILURE_BASE = `plan_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,${TZ})`;
const CHANGED = [
  ADMIT_EVENT,
  '_flow_engine_native_trigger(jsonb)',
  '_flow_engine_native_steps(text)',
  ADMIT_NATIVE,
  `_flow_engine_authorize_native_tail(uuid,uuid,flow_engine_runs,recovery_cases,flow_controls,${TZ})`,
  `read_flow_engine_native_trigger_counts_v1(jsonb,text,text,${TZ})`,
  'read_automation_expert_native_history_v1(jsonb,uuid)',
  PLAN_CART_BASE,
  PLAN_FAILURE_BASE,
  '_flow_change_new_flow_blocker(text,text,jsonb)',
];
const ADDED = ['_flow_engine_native_payment_failure_audience(uuid,uuid,text,text,text)'];
// La politica del pago rechazado de Maribel, medida en maribel_db (solo lectura) el 2026-10-30 15:05Z:
// maribel-pago-rechazado-un-toque v1, la del scope maribel-pago-rechazado v1 (20 por dia, 1.000 en total).
const MARIBEL_POLICY = {
  timezone: 'America/Mexico_City',
  business_windows: [{ end: '21:00', days: [1, 2, 3, 4, 5, 6, 7], start: '09:00' }],
  grace_period: '00:15:00', expires_after: '1 day', max_automatic_messages: 1,
};
const maribel = JSON.parse(readFileSync(join(root,
  'tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json'), 'utf8')).message_templates[0];
const REFUSALS = JSON.parse(readFileSync(join(root,
  'tests/fixtures/hotmart_purchase_canceled_maribel_20261028.json'), 'utf8')).eventos;
const purchaseCaptured = JSON.parse(readFileSync(join(root,
  'tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json'), 'utf8')).payload;
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
if (position !== names.length - 1) fail(`${MIGRATION} tiene que ser la ultima`, names.slice(position + 1));
if (maribel.name !== 'maribel_retomar_proceso_01' || maribel.category !== 'MARKETING') fail('el fixture no es la plantilla de Maribel');
if (REFUSALS.length !== 3 || REFUSALS[1].persona !== REFUSALS[2].persona
    || REFUSALS.some((event) => event.payload.event !== 'PURCHASE_CANCELED' || !event.payload.data.purchase.payment.refusal_reason)) {
  fail('el fixture no son los tres rechazos de Maribel');
}

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile, p.proowner,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const previous = {};
{
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
  await applyFile(db, join(root, 'supabase/baseline/20260803_public_schema.sql'));
  for (const name of names.slice(0, position)) await applyFile(db, join(root, 'supabase/migrations', name));
  const before = await snapshot(db);
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.has(s) && after.get(s).definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && ['acl', 'prosecdef', 'provolatile', 'proowner'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, CHANGED) || !sameSet(added, ADDED) || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  for (const signature of ADDED) {
    if (after.get(signature).service_x || after.get(signature).api_x || after.get(signature).prosecdef) {
      fail(`${signature}: no es de nadie de la API`);
    }
  }
  for (const signature of CHANGED) {
    if (!after.get(signature).definition.includes('flow_engine_native_payment_failure')) fail(`${signature}: sin la marca`);
    previous[signature] = before.get(signature).definition;
  }
  const checks = (await db.query(`select conname, pg_get_constraintdef(oid) as d from pg_constraint
    where conrelid = 'public.flow_engine_native_runs'::regclass and contype = 'c'`)).rows;
  const trigger = checks.find((row) => row.conname === 'flow_engine_native_runs_trigger_event_check');
  const audience = checks.find((row) => row.conname === 'flow_engine_native_runs_audience_by_trigger');
  if (!trigger?.d.includes('pago_fallido') || !audience?.d.includes('pago_fallido')) fail('las restricciones del recorrido', checks);
  let error = null;
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  } catch (caught) {
    error = caught;
  }
  if (error?.message !== 'flow_engine_native_payment_failure_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  await db.exec('rollback').catch(() => {});
  if ((await snapshot(db)).get(ADMIT_NATIVE).definition !== after.get(ADMIT_NATIVE).definition) fail('la segunda corrida cambio algo');
  results.forma = { cambiadas: changed.length, nuevas: added.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 (buildWorld, la cadena entera) con un pago rechazado nativo.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, B, INSTANCE, register, cycle, versionItem, pauseFlow, dbNow, reserve, startNew, accept, person,
  admitForm, cartFor, refusalFor, rpc, THREE_TOUCHES, PLAN_CART } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
await db.exec('alter role service_role bypassrls');
const asService = async (run) => {
  await db.exec('set role service_role');
  try {
    return await run();
  } finally {
    await db.exec('reset role');
  }
};
const rolledBack = async (run) => {
  await db.exec('begin');
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};
const withPrevious = async (signatures, run) => rolledBack(async () => {
  for (const signature of signatures) await db.exec(previous[signature]);
  return run();
});

// El pago rechazado de Maribel, como lo crearia el AE con un item nuevo: los valores de hoy.
const MARIBEL_PAYMENT = {
  esquema: 'setter-flujo/v1', flujo: 'pago_rechazado', producto: A.funnel, estado: 'borrador', nombre: 'Pago rechazado',
  dispara: { evento: 'pago_fallido' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ esperar: '15m' }, { plantilla: maribel.name, idioma: maribel.language, variables: ['nombre', 'producto'] }],
  termina: { si: ['compra'], vence: '1d' },
  horario: { zona: 'America/Mexico_City', desde: '09:00', hasta: '21:00' },
  topes: { bolsa: 'att1-pago-rechazado-nativo', por_dia: 20, total: 1000,
    repeticion: { una_por: 'persona', otra_despues_de: '72h' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
// En el mundo de prueba, el mismo flujo con horario siempre: el despacho no depende de la hora en que corre.
const PAYMENT = { ...MARIBEL_PAYMENT, horario: 'siempre' };

// ---------------------------------------------------------------------------
// 1. El flujo de Maribel compila a la politica de hoy.
// ---------------------------------------------------------------------------
{
  const unsupported = one(await sql('select public._flow_engine_native_unsupported($1::jsonb) as r',
    [JSON.stringify(MARIBEL_PAYMENT)]), 'lo que no ejecuta').r;
  if (unsupported !== null) fail('el pago rechazado de Maribel se ejecuta', unsupported);
  const sendable = { ...MARIBEL_PAYMENT };
  // Los intervalos se comparan por su duracion: el nativo compila vence 1d a 24:00:00, igual a 1 day (CDMX no
  // tiene horario de verano desde 2022).
  const compiled = one(await sql(`select timezone, business_windows,
      case when grace_period = interval '15 minutes' then '00:15:00' else grace_period::text end as grace_period,
      case when expires_after = interval '1 day' then '1 day' else expires_after::text end as expires_after,
      max_automatic_messages, steps
    from public._flow_engine_compile_policy_core($1::jsonb, '00000000-0000-4000-8000-0000000e2071', 1, 'nativo:pago_fallido', 'America/Mexico_City')`,
  [JSON.stringify(sendable)]), 'politica');
  const got = { timezone: compiled.timezone, business_windows: compiled.business_windows, grace_period: compiled.grace_period,
    expires_after: compiled.expires_after, max_automatic_messages: compiled.max_automatic_messages };
  if (JSON.stringify(got) !== JSON.stringify(MARIBEL_POLICY) || compiled.steps.length !== 1
      || compiled.steps[0].step_key !== 'first_contact') {
    fail('la politica de Maribel', { got, want: MARIBEL_POLICY, steps: compiled.steps });
  }
  results.politica = got;
}

const openNative = async (flowKey, definition, product = A.funnel) => {
  const flowId = await register(product, flowKey);
  await cycle([versionItem(flowId, product, flowKey, { ...definition, producto: product })], `v1 de ${flowKey}`);
  const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'propuesta').proposal_id;
  const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [flowId, proposalId, A.timezone]), 'abrir').r);
  if (!opened.ok || opened.state !== 'cohorte') fail(`abrir ${flowKey}`, opened);
  return { flowId, opened };
};
const payment = await openNative('pago_rechazado', PAYMENT);
const cohort = async (flowId, contactId) => sql(
  `insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`, [flowId, contactId]);

// Una persona con (o sin) el formulario con consentimiento, y su contacto.
const leadFor = async (label, { form = true } = {}) => {
  const lead = person(label);
  if (form) await admitForm(lead, new Date((await dbNow()).getTime() - 50 * 60_000));
  return lead;
};
let refusalSerial = 0;
// Un rechazo capturado de Maribel con la persona sembrada, por la admision portable de hoy.
const refusalOf = async (lead, { index = 0, at = null, payment: pay = null, P = A } = {}) => {
  refusalSerial += 1;
  const payload = structuredClone(REFUSALS[index].payload);
  const moment = at ?? new Date((await dbNow()).getTime() - 10 * 60_000);
  payload.id = `nativo-rechazo-${lead.email}-${refusalSerial}`;
  payload.creation_date = moment.getTime();
  payload.data.buyer = { ...payload.data.buyer, name: lead.name, email: lead.email, checkout_phone: lead.phone };
  payload.data.product = { ...payload.data.product, id: P.productId, name: P.productName };
  payload.data.purchase.offer.code = lead.offer.offer_code;
  payload.data.purchase.transaction = `HPNATIVOPAGO${String(refusalSerial).padStart(4, '0')}`;
  if (pay) payload.data.purchase.payment = pay;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [P.tenant, P.funnel, P.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone]), `${lead.label} rechazo`);
  if (admitted.outcome !== 'inserted') fail(`${lead.label} rechazo`, admitted);
  if (!lead.contact) {
    lead.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
      values ($1,$2,$3,'MX') returning id`, [lead.name, lead.email, lead.phone]), 'contacto').id;
    await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id)
      values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)`,
    [lead.contact, lead.email, lead.phone, admitted.webhook_event_id]);
  }
  lead.event = admitted.webhook_event_id;
  lead.moment = moment.toISOString();
  return lead;
};
const flowEventOf = (lead) => ({
  fuente: 'hotmart', tipo: 'pago_fallido', evento_id: lead.event, producto_ref: A.funnel, oferta: lead.offer.offer_code,
  contacto_id: lead.contact, momento: lead.moment, chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
  destino: lead.phone,
});
const admitNative = async (lead) => asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)', [JSON.stringify(INSTANCE), JSON.stringify(flowEventOf(lead))])).rows);
const rowOf = (rows, flowId, label) => one(rows.filter((row) => row.flow_id === flowId), label);
const casesOf = async (lead) => sql(`select id, status, offer_code, policy_key, context from public.recovery_cases
  where contact_id = $1 order by created_at`, [lead.contact]);
// El planificador del pago rechazado del legado (el piloto, en el scope del carrito, como refusalFor).
const PLAN_FAILURE = 'select * from public.plan_portable_payment_failure_recovery($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)';
const failureArgs = (lead) => [lead.event, lead.contact, String(A.productId), A.productName, lead.offer.offer_code,
  THREE_TOUCHES.policy, 1, lead.moment, A.accountId, A.inboxId, lead.phone, A.scope, 1];
const planFailure = async (lead) => {
  let plan = null;
  const error = await errorOf(db, async () => {
    plan = one((await db.query(PLAN_FAILURE, failureArgs(lead))).rows, `${lead.label} plan del rechazo`);
  });
  return error === null ? { recovery_case_id: plan.recovery_case_id, created: plan.created }
    : { error: { code: error.code, message: error.message, details: error.detail ?? null } };
};
const emitted = {};

// ---------------------------------------------------------------------------
// 2. La admision.
// ---------------------------------------------------------------------------
const maria = await refusalOf(await leadFor('pago-maria'));
await cohort(payment.flowId, maria.contact);
const started = rowOf(await admitNative(maria), payment.flowId, 'pago rechazado nativo');
if (started.outcome !== 'motor_arranco' || started.created !== true) fail('el rechazo abre el recorrido nativo', started);
{
  const kase = one(await sql('select * from public.recovery_cases where id = $1', [started.recovery_case_id]), 'caso');
  const native = one(await sql('select * from public.flow_engine_native_runs where recovery_case_id = $1',
    [started.recovery_case_id]), 'recorrido nativo');
  const details = one(await sql(`select purchase_intent_id from public.commercial_ally_payment_failure_details
    where webhook_event_id = $1 and correlation_outcome = 'resolved'`, [maria.event]), 'correlacion');
  if (kase.offer_code !== null || kase.policy_key !== `flujo:${payment.flowId}` || kase.abandonment_event_id !== maria.event
      || kase.context.flow_engine_trigger !== 'pago_fallido' || kase.context.flow_engine_offer !== maria.offer.offer_code
      || 'trigger_kind' in kase.context || native.trigger_event !== 'pago_fallido'
      || native.audience_purchase_intent_id !== details.purchase_intent_id || !native.audience_precheckout_submission_id) {
    fail('el caso nativo del pago rechazado', { kase, native });
  }
  const grant = one(await sql(`select * from public.contact_authorizations where contact_id = $1 and purpose = 'cart_recovery'`,
    [maria.contact]), 'autorizacion');
  if (grant.authorization_source !== 'system' || grant.evidence.reason !== 'precheckout_whatsapp_consent'
      || grant.evidence.purchase_intent_id !== details.purchase_intent_id) {
    fail('la autorizacion del pago rechazado', grant);
  }
  const action = one(await sql('select due_at, expires_at from public.scheduled_actions where id = $1',
    [started.scheduled_action_id]), 'accion');
  const wait = (new Date(action.due_at) - new Date(maria.moment)) / 60_000;
  if (Math.abs(wait - 15) > 0.01) fail('el primer envio sale a los 15 min del rechazo', { wait });
  // La repeticion del mismo evento devuelve su caso.
  const replay = rowOf(await admitNative(maria), payment.flowId, 'repeticion');
  if (replay.outcome !== 'motor_arranco' || replay.created !== false || replay.recovery_case_id !== started.recovery_case_id) {
    fail('la repeticion del evento', replay);
  }
  results.admision = { salida: started.outcome, espera_min: wait, permiso: grant.evidence.reason };
}
emitted.admission = { outcome: started.outcome, reason: started.reason, created: started.created };
{
  const outsider = await refusalOf(await leadFor('pago-afuera'));
  const outside = rowOf(await admitNative(outsider), payment.flowId, 'fuera de la cohorte');
  if (outside.outcome !== 'motor_no_arranca' || outside.reason !== 'fuera_de_cohorte') fail('fuera de la cohorte', outside);
  // Sin formulario: la correlacion no resuelve y el rechazo no da permiso (el legado: payment_failure_correlation_unresolved).
  const noForm = await refusalOf(await leadFor('pago-sin-formulario', { form: false }));
  await cohort(payment.flowId, noForm.contact);
  const refused = rowOf(await admitNative(noForm), payment.flowId, 'sin formulario');
  if (refused.outcome !== 'motor_no_arranca' || refused.reason !== 'no_entra:permiso') fail('sin formulario', refused);
  emitted.no_permission = { outcome: refused.outcome, reason: refused.reason, created: refused.created };
  // Un pendiente de MERCADO_PAGO que se cancelo, sin motivo: no es un rechazo (el legado: payment_failure_not_a_refusal).
  const pending = await refusalOf(await leadFor('pago-pendiente'), { payment: { type: 'MERCADO_PAGO', installments_number: 1 } });
  await cohort(payment.flowId, pending.contact);
  const correlated = await sql(`select correlation_outcome from public.commercial_ally_payment_failure_details
    where webhook_event_id = $1`, [pending.event]);
  const notRefusal = rowOf(await admitNative(pending), payment.flowId, 'no es un rechazo');
  const legacyPending = await planFailure(pending);
  if (correlated[0]?.correlation_outcome !== 'resolved') fail('el pendiente cancelado se correlaciona', correlated);
  if (notRefusal.reason !== 'no_es_un_rechazo' || legacyPending.error?.message !== 'payment_failure_not_a_refusal') {
    fail('un pendiente cancelado no es un rechazo', { notRefusal, legacyPending });
  }
  emitted.not_a_refusal = { outcome: notRefusal.outcome, reason: notRefusal.reason, created: notRefusal.created };
  // Rojo: si el pendiente contara como rechazo, el nativo le escribiria.
  const pendingRed = await rolledBack(async () => {
    await db.exec(`create or replace function public.payment_failure_refusal_kind(p_refusal_reason text, p_payment_type text)
      returns text language sql immutable set search_path = pg_catalog, pg_temp as $$ select 'declined'::text $$`);
    await refusalOf(pending, { payment: { type: 'MERCADO_PAGO', installments_number: 1 } });
    return rowOf(await admitNative(pending), payment.flowId, 'pendiente como rechazo').outcome;
  });
  if (pendingRed !== 'motor_arranco') fail('sin el control del rechazo, el pendiente arrancaba', pendingRed);
  // Rojo: con la forma del evento de antes, el pago rechazado no entra al motor.
  const red = await withPrevious([ADMIT_EVENT], () => errorOf(db, () => db.query(
    'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)', [JSON.stringify(INSTANCE), JSON.stringify(flowEventOf(maria))])));
  if (red?.message !== 'invalid_flow_engine_event') fail('sin la migracion, el evento no entra', red?.message);
  results.cortes = { afuera: outside.reason, sin_formulario: refused.reason, pendiente: notRefusal.reason,
    sin_la_migracion: red.message };
}

// ---------------------------------------------------------------------------
// 3. Dos rechazos seguidos de la misma persona: un solo mensaje, a la gracia del ultimo.
// ---------------------------------------------------------------------------
{
  const gap = REFUSALS[2].payload.creation_date - REFUSALS[1].payload.creation_date;
  const firstAt = new Date((await dbNow()).getTime() - 10 * 60_000);
  const lucia = await refusalOf(await leadFor('pago-dos-rechazos'), { index: 1, at: firstAt });
  await cohort(payment.flowId, lucia.contact);
  const first = rowOf(await admitNative(lucia), payment.flowId, 'el primer rechazo');
  if (first.outcome !== 'motor_arranco') fail('el primer rechazo arranca', first);
  const dueOf = async () => new Date(one(await sql('select due_at from public.scheduled_actions where id = $1',
    [first.scheduled_action_id]), 'accion').due_at).getTime();
  const due1 = await dueOf();
  await refusalOf(lucia, { index: 2, at: new Date(firstAt.getTime() + gap) });
  // Rojo: sin el bloque del ultimo rechazo, el envio queda a la gracia del primero.
  const red = await rolledBack(async () => {
    const definition = one(await sql('select pg_get_functiondef($1::regprocedure) as d', [ADMIT_NATIVE]), 'definicion').d;
    const begin = definition.indexOf('    -- flow_engine_native_payment_failure_last_refusal: begin');
    const end = definition.indexOf('    -- flow_engine_native_payment_failure_last_refusal: end\n') + '    -- flow_engine_native_payment_failure_last_refusal: end\n\n'.length;
    await db.exec(definition.slice(0, begin) + definition.slice(end));
    const row = rowOf(await admitNative(lucia), payment.flowId, 'segundo rechazo sin el bloque');
    return { reason: row.reason, due: await dueOf() };
  });
  if (red.reason !== 'ya_en_curso' || red.due !== due1) fail('sin el bloque, el envio no se corre', red);
  const second = rowOf(await admitNative(lucia), payment.flowId, 'el segundo rechazo');
  if (second.outcome !== 'motor_no_arranca' || second.reason !== 'ya_en_curso' || second.recovery_case_id !== null) {
    fail('el segundo rechazo no abre otro caso', second);
  }
  const due2 = await dueOf();
  if (due2 - due1 !== gap || (await casesOf(lucia)).length !== 1) fail('el envio pasa a la gracia del ultimo rechazo', { due1, due2, gap });
  // El rechazo queda en el caso, como en el legado.
  const trace = await sql(`select event_role from public.recovery_case_events where recovery_case_id = $1 and webhook_event_id = $2`,
    [first.recovery_case_id, lucia.event]);
  if (trace.length !== 1 || trace[0].event_role !== 'payment_failure') fail('el segundo rechazo queda en el caso', trace);
  // La repeticion del segundo rechazo no corre nada mas.
  const again = rowOf(await admitNative(lucia), payment.flowId, 'repeticion del segundo');
  if (again.reason !== 'ya_en_curso' || await dueOf() !== due2) fail('la repeticion del segundo rechazo', again);
  emitted.second_refusal = { outcome: second.outcome, reason: second.reason, created: second.created };
  results.dos_rechazos = { segundo: second.reason, corrido_seg: gap / 1000, casos: 1, sin_el_bloque: red.due === due1 };
}

// ---------------------------------------------------------------------------
// 4. El despacho.
// ---------------------------------------------------------------------------
{
  const lead = { label: 'pago-maria', chatwoot: '630' };
  const reserved = await reserve(lead, started.scheduled_action_id);
  const step = one(await asService(async () => (await db.query('select * from public.flow_engine_execution_context_v1($1)',
    [started.scheduled_action_id])).rows), 'contexto del paso');
  if (step.template_name !== maribel.name || step.entry_permission !== 'formulario' || step.step_key !== 'first_contact'
      || JSON.stringify(step.template_variables) !== JSON.stringify(['nombre', 'producto'])) {
    fail('el contexto del paso', step);
  }
  const intentId = one(await sql('select audience_purchase_intent_id as i from public.flow_engine_native_runs where recovery_case_id = $1',
    [started.recovery_case_id]), 'intencion').i;
  const red = await rolledBack(async () => {
    await db.query('update public.purchase_intents set whatsapp_contact_authorized = false where id = $1', [intentId]);
    return startNew(reserved);
  });
  if (red.outcome !== 'rechazado' || red.reason !== 'no_entra:permiso') fail('el arranque vuelve a mirar la intencion', red);
  const startedSend = await startNew(reserved);
  if (startedSend.outcome !== 'request_started' || !startedSend.flow_authorization_id) fail('el arranque nativo', startedSend);
  const accepted = await accept(lead, reserved);
  const pool = one(await sql('select count(*)::int as n from public.flow_engine_request_authorizations where pool = $1',
    [PAYMENT.topes.bolsa]), 'bolsa').n;
  if (pool !== 1) fail('la bolsa del pago rechazado cuenta el envio', pool);
  // Otro rechazo dentro de las 72 h del caso que ya mando: ya_disparado, como el legado (lo suma a su caso, sin mensaje).
  const caseStatus = one(await sql('select status from public.recovery_cases where id = $1', [started.recovery_case_id]), 'caso').status;
  if (['grace_period', 'active', 'paused'].includes(caseStatus)) fail('el caso que ya mando queda cerrado', caseStatus);
  await refusalOf(maria);
  const again = rowOf(await admitNative(maria), payment.flowId, 'rechazo despues del mensaje');
  if (again.reason !== 'ya_disparado') fail('un rechazo dentro de las 72 h no manda otro', { again, caseStatus });
  // Se suma al caso que ya mando, como el legado.
  const sentTrace = await sql(`select event_role from public.recovery_case_events where recovery_case_id = $1
    and webhook_event_id = $2`, [started.recovery_case_id, maria.event]);
  if (sentTrace.length !== 1 || sentTrace[0].event_role !== 'payment_failure') fail('el rechazo queda en el caso que ya mando', sentTrace);
  emitted.after_sent = { outcome: again.outcome, reason: again.reason, created: again.created };
  results.despacho = { permiso: step.entry_permission, plantilla: step.template_name, rojo: red.reason,
    arranque: startedSend.outcome, aceptacion: accepted.status, despues: again.reason };
}

// ---------------------------------------------------------------------------
// 5. La compra lo corta antes del envio.
// ---------------------------------------------------------------------------
{
  const buyer = await refusalOf(await leadFor('pago-compra'));
  await cohort(payment.flowId, buyer.contact);
  const buyerStart = rowOf(await admitNative(buyer), payment.flowId, 'rechazo de quien compra');
  if (buyerStart.outcome !== 'motor_arranco') fail('el rechazo de quien compra', buyerStart);
  const binding = await sql('select 1 from public.commercial_ally_hotmart_event_bindings where webhook_event_id = $1', [buyer.event]);
  if (binding.length !== 1) fail('el rechazo deja el binding portable');
  const payload = structuredClone(purchaseCaptured);
  const now = await dbNow();
  const shift = now.getTime() - 60_000 - payload.creation_date;
  for (const key of ['approved_date', 'order_date']) payload.data.purchase[key] += shift;
  payload.creation_date += shift;
  payload.id = `nativo-pago-compra-${buyer.email}`;
  payload.data.buyer = { ...payload.data.buyer, name: buyer.name, email: buyer.email, checkout_phone: buyer.phone };
  payload.data.product = { ...payload.data.product, id: A.productId };
  payload.data.purchase.offer.code = buyer.offer.offer_code;
  payload.data.purchase.transaction = `HPNATIVOPAGOCOMPRA`;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), buyer.email, buyer.phone]), 'compra');
  if (admitted.outcome !== 'inserted') fail('admitir la compra', admitted);
  await sql('select * from public.apply_hotmart_purchase_approved($1,$2,$3,$4,$5,$6,$7::timestamptz)',
    [admitted.webhook_event_id, buyer.email, buyer.phone, String(A.productId), buyer.offer.offer_code,
      payload.data.purchase.transaction, new Date(payload.data.purchase.approved_date).toISOString()]);
  const cut = await reserve({ label: 'pago-compra', chatwoot: '631' }, buyerStart.scheduled_action_id, { expect: 'cancel' });
  if (cut.decision.reason_code !== 'purchase_detected_any_offer') fail('la compra corta el pago rechazado nativo', cut.decision);
  results.compra = { motivo: cut.decision.reason_code };
}

// ---------------------------------------------------------------------------
// 6. (a) Un caso del pago rechazado del legado abierto: el nativo no arranca.
// ---------------------------------------------------------------------------
{
  const ana = await leadFor('pago-legado');
  ana.offer = A.offers.at(-1);
  const first = await refusalFor(1, ana);
  if (first.created !== true) fail('el legado planifica el primer rechazo', first);
  await cohort(payment.flowId, ana.contact);
  await refusalOf(ana);
  const native = rowOf(await admitNative(ana), payment.flowId, 'nativo sobre el legado');
  if (native.outcome !== 'motor_no_arranca' || native.reason !== 'caso_del_legado_en_curso') fail('el nativo no arranca', native);
  // El legado, como hoy: suma el rechazo a su caso, sin abrir otro.
  const today = await withPrevious([ADMIT_NATIVE, PLAN_FAILURE_BASE], () => planFailure(ana));
  const now = await planFailure(ana);
  if (JSON.stringify(now) !== JSON.stringify(today) || now.recovery_case_id !== first.recovery_case_id || now.created !== false) {
    fail('el legado suma el rechazo a su caso, como hoy', { now, today });
  }
  // Rojo: sin el bloque, el nativo abria el segundo caso.
  const red = await rolledBack(async () => {
    const definition = one(await sql('select pg_get_functiondef($1::regprocedure) as d', [ADMIT_NATIVE]), 'definicion').d;
    const begin = definition.indexOf('    -- flow_engine_native_payment_failure_double_case: begin');
    const end = definition.indexOf('    -- flow_engine_native_payment_failure_double_case: end\n') + '    -- flow_engine_native_payment_failure_double_case: end\n'.length;
    await db.exec(definition.slice(0, begin) + definition.slice(end));
    await refusalOf(ana);
    const row = rowOf(await admitNative(ana), payment.flowId, 'sin el bloque');
    return { outcome: row.outcome, cases: (await casesOf(ana)).length };
  });
  if (red.outcome !== 'motor_arranco' || red.cases !== 2) fail('sin la migracion, el doble caso', red);
  // Cerrado hace menos de 72 h con mensaje, compra o persona: el rechazo sigue siendo del legado (E-211).
  const recentA = {};
  for (const status of ['sequence_exhausted', 'won', 'escalated']) {
    await sql(`update public.recovery_cases set status = $2, closed_at = now(), created_at = now() - interval '1 hour',
      won_at = case when $2 = 'won' then now() end where id = $1`, [first.recovery_case_id, status]);
    await refusalOf(ana);
    recentA[status] = rowOf(await admitNative(ana), payment.flowId, `legado ${status} reciente`).reason;
    if (recentA[status] !== 'caso_del_legado_en_curso') fail(`con el caso del legado ${status} hace 1 h, el nativo no arranca`, recentA);
  }
  // Rojo: sin la rama de las 72 h, el nativo arrancaba sobre el caso que ya mando.
  const recentRedA = await rolledBack(async () => {
    const definition = one(await sql('select pg_get_functiondef($1::regprocedure) as d', [ADMIT_NATIVE]), 'definicion').d;
    const cut = definition.replace(/\n\s+or \(legacy_case\.status in \('sequence_exhausted', 'won', 'escalated'\)\n\s+and legacy_case\.created_at > p_moment - interval '72 hours'\)\)/, ')');
    if (cut === definition) fail('el rojo de las 72 h no encontro la rama');
    await db.exec(cut);
    await refusalOf(ana);
    return rowOf(await admitNative(ana), payment.flowId, 'sin la rama de las 72 h').outcome;
  });
  if (recentRedA !== 'motor_arranco') fail('sin la rama de las 72 h, el nativo arrancaba', recentRedA);
  // Cerrado hace mas de 72 h, no: el nativo arranca.
  await sql(`update public.recovery_cases set status = 'sequence_exhausted', closed_at = now(),
    created_at = now() - interval '73 hours' where id = $1`, [first.recovery_case_id]);
  await refusalOf(ana);
  const later = rowOf(await admitNative(ana), payment.flowId, 'legado viejo');
  if (later.outcome !== 'motor_arranco') fail('con el caso del legado de hace mas de 72 h, el nativo arranca', later);
  emitted.over_legacy = { outcome: native.outcome, reason: native.reason, created: native.created };
  results.a = { nativo: native.reason, legado: now.created, sin_el_bloque: red.outcome, reciente: recentA,
    sin_las_72h: recentRedA, viejo: later.outcome };
}

// ---------------------------------------------------------------------------
// 7. (b) Un pago rechazado nativo abierto y el nativo pausado: el legado no abre otro.
// ---------------------------------------------------------------------------
let carla = null;
{
  carla = await refusalOf(await leadFor('pago-nativo-abierto'));
  await cohort(payment.flowId, carla.contact);
  const open = rowOf(await admitNative(carla), payment.flowId, 'el pago nativo');
  if (open.outcome !== 'motor_arranco') fail('el pago nativo arranca', open);
  carla.nativeCase = open.recovery_case_id;
}
// D6 antes de pausar: un carrito nativo que no arranca mientras el pago rechazado nativo esta abierto.
{
  const CART = {
    esquema: 'setter-flujo/v1', flujo: 'carrito_nativo', producto: A.funnel, estado: 'borrador', nombre: 'Carrito nativo',
    dispara: { evento: 'carrito' }, entra: { permiso: 'formulario', contesto_a: [] },
    pasos: [{ plantilla: maribel.name, idioma: maribel.language, variables: ['nombre', 'producto'] }],
    termina: { si: ['compra'], vence: '1d' }, horario: 'siempre',
    topes: { bolsa: 'att1-carrito-nativo', por_dia: 50, total: 3000,
      repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
    prioridad: { le_gana_a: [], no_arranca_mientras: ['pago_rechazado'] },
  };
  const cart = await openNative('carrito_nativo', CART);
  await cohort(cart.flowId, carla.contact);
  const lead = { ...carla };
  await cartFor(lead);
  const rows = await asService(async () => (await db.query('select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
    [JSON.stringify(INSTANCE), JSON.stringify({ ...flowEventOf(lead), tipo: 'carrito', momento: lead.cartArgs[6] })])).rows);
  const blocked = rowOf(rows, cart.flowId, 'carrito con el pago abierto');
  if (blocked.outcome !== 'motor_no_arranca' || blocked.reason !== 'no_arranca_mientras:pago_rechazado') {
    fail('el carrito nativo no arranca con un pago rechazado nativo abierto', blocked);
  }
  // El carrito del legado de la misma persona tampoco (hoy lo frena el pago rechazado del legado abierto).
  const cartLegacy = await errorOf(db, () => db.query(PLAN_CART, lead.cartArgs));
  if (cartLegacy?.message !== 'pilot_scope_rejected' || cartLegacy.detail !== 'flujo_nativo_en_curso') {
    fail('el carrito del legado ve el pago rechazado nativo', cartLegacy);
  }
  const cartRed = await withPrevious([PLAN_CART_BASE], async () => {
    const plan = one((await db.query(PLAN_CART, lead.cartArgs)).rows, 'carrito de antes');
    return plan.created;
  });
  if (cartRed !== true) fail('sin la migracion, el carrito del legado mandaba', cartRed);
  emitted.cart_blocked = { outcome: blocked.outcome, reason: blocked.reason, created: blocked.created };
  results.d6 = { carrito_nativo: blocked.reason, carrito_legado: cartLegacy.detail, sin_la_migracion: cartRed };
}
{
  await pauseFlow(payment.flowId, A.funnel, 'pago_rechazado');
  await refusalOf(carla);
  const paused = rowOf(await admitNative(carla), payment.flowId, 'nativo pausado');
  if (paused.reason !== 'flujo_pausado') fail('el nativo pausado', paused);
  const before = (await casesOf(carla)).length;
  const legacy = await planFailure(carla);
  if (legacy.error?.code !== '55000' || legacy.error.message !== 'pilot_scope_rejected'
      || legacy.error.details !== 'flujo_nativo_en_curso') {
    fail('el legado ve el pago rechazado nativo', legacy);
  }
  if ((await casesOf(carla)).length !== before) fail('el legado no escribio nada');
  const red = await withPrevious([PLAN_FAILURE_BASE], () => planFailure(carla));
  if (red.created !== true) fail('sin la migracion, el legado abria el segundo caso', red);
  // El caso nativo cerrado hace menos de 72 h con mensaje, compra o persona: el legado tampoco (E-211).
  const recentB = {};
  for (const status of ['sequence_exhausted', 'won', 'escalated']) {
    await sql(`update public.recovery_cases set status = $2, closed_at = now(), created_at = now() - interval '1 hour',
      won_at = case when $2 = 'won' then now() end where id = $1`, [carla.nativeCase, status]);
    recentB[status] = (await planFailure(carla)).error?.details ?? 'planifico';
    if (recentB[status] !== 'flujo_nativo_en_curso') fail(`con el caso nativo ${status} hace 1 h, el legado no planifica`, recentB);
  }
  const recentRedB = await rolledBack(async () => {
    const definition = one(await sql('select pg_get_functiondef($1::regprocedure) as d', [PLAN_FAILURE_BASE]), 'definicion').d;
    const cut = definition.replace(/\n\s+or \(native_case\.status in \('sequence_exhausted', 'won', 'escalated'\)\n\s+and native_case\.created_at > p_failed_at - interval '72 hours'\)\)/, ')');
    if (cut === definition) fail('el rojo de las 72 h del legado no encontro la rama');
    await db.exec(cut);
    return (await planFailure(carla)).created;
  });
  if (recentRedB !== true) fail('sin la rama de las 72 h, el legado abria otro caso', recentRedB);
  // Con el caso nativo cerrado hace mas de 72 h, el legado planifica.
  await sql(`update public.recovery_cases set status = 'sequence_exhausted', closed_at = now(),
    created_at = now() - interval '73 hours' where id = $1`, [carla.nativeCase]);
  const after = await planFailure(carla);
  if (after.created !== true) fail('con el caso nativo viejo, el legado planifica', after);
  emitted.legacy_over_native = legacy.error;
  results.b = { legado: legacy.error.details, sin_la_migracion: red.created, reciente: recentB,
    sin_las_72h: recentRedB, viejo: after.created };
}

// ---------------------------------------------------------------------------
// 3c. Una accion diferida por la ventana de 09:00 a 21:00 (E-211): el segundo rechazo la corre.
// ---------------------------------------------------------------------------
{
  // Otro producto (B) con el flujo de Maribel y su horario de verdad. El primer rechazo a las 20:50 de CDMX
  // del dia anterior: el envio vence a las 21:05 y la reevaluacion de esa hora lo difiere a las 09:00. El
  // segundo rechazo llega a las 08:58: el envio pasa a las 09:13, a los 15 min del ultimo intento.
  // Antes, sin un flujo nativo del pago rechazado del producto B: sin_flujo (lo de antes, igual).
  const other = person('pago-otro-producto', B);
  await admitForm(other, new Date((await dbNow()).getTime() - 50 * 60_000));
  await refusalOf(other, { P: B });
  const rows = await asService(async () => (await db.query('select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
    [JSON.stringify(INSTANCE), JSON.stringify({ ...flowEventOf(other), producto_ref: B.funnel })])).rows);
  if (rows.length !== 1 || rows[0].outcome !== 'sin_flujo') fail('sin flujo nativo del pago rechazado', rows);
  const real = await openNative('pago_rechazado', MARIBEL_PAYMENT, B.funnel);
  const CDMX = -6 * 60 * 60_000;
  const nowMs = (await dbNow()).getTime();
  const local = new Date(nowMs + CDMX);
  let day = Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate());
  if (local.getUTCHours() * 60 + local.getUTCMinutes() < 9 * 60 + 5) day -= 86_400_000;
  const at = (dayMs, hh, mm) => new Date(dayMs + (hh * 60 + mm) * 60_000 - CDMX);
  const firstAt = at(day - 86_400_000, 20, 50);
  const secondAt = at(day, 8, 58);
  const rosa = person('pago-diferido', B);
  await admitForm(rosa, new Date(firstAt.getTime() - 40 * 60_000));
  await refusalOf(rosa, { index: 1, at: firstAt, P: B });
  await cohort(real.flowId, rosa.contact);
  const eventOf = (lead) => ({ ...flowEventOf(lead), producto_ref: B.funnel });
  const admitB = async (lead) => asService(async () => (await db.query(
    'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)', [JSON.stringify(INSTANCE), JSON.stringify(eventOf(lead))])).rows);
  const first = rowOf(await admitB(rosa), real.flowId, 'el primer rechazo de la noche');
  if (first.outcome !== 'motor_arranco') fail('el rechazo de la noche arranca', first);
  // La reevaluacion de las 21:06 (la del despachador, con su hora): fuera de la ventana, difiere. La
  // admision corrio ahora y no a las 20:50: la autorizacion que concedio se fecha a la hora del rechazo, como
  // habria quedado en produccion (lo unico sembrado del recorrido).
  await sql(`update public.contact_authorizations set valid_from = $2::timestamptz where contact_id = $1`,
    [rosa.contact, firstAt.toISOString()]);
  const reviewAt = at(day - 86_400_000, 21, 6);
  await sql(`update public.scheduled_actions set due_at = least(now() + interval '1 day', expires_at - interval '1 minute')
    where id <> $1 and status in ('pending', 'deferred', 'retryable_failed') and due_at <= $2::timestamptz + interval '1 minute'`,
  [first.scheduled_action_id, reviewAt.toISOString()]);
  const claimed = await sql(`select * from public.claim_due_followup_actions('motor-worker-diferido', $1::timestamptz,
    interval '5 minutes', 100)`, [reviewAt.toISOString()]);
  if (claimed.length !== 1 || claimed[0].id !== first.scheduled_action_id) fail('el claim de las 21:06', claimed.map((row) => row.step_key));
  const decision = one(await sql('select * from public.reevaluate_followup_action($1, $2, $3, $4::timestamptz)',
    [first.scheduled_action_id, 'motor-worker-diferido', claimed[0].lease_generation, reviewAt.toISOString()]), 'reevaluacion');
  const deferred = one(await sql('select status, due_at from public.scheduled_actions where id = $1', [first.scheduled_action_id]), 'diferida');
  const nine = at(day, 9, 0).getTime();
  if (decision.decision !== 'defer' || deferred.status !== 'deferred' || new Date(deferred.due_at).getTime() !== nine) {
    fail('la ventana difiere el envio a las 09:00', { decision, deferred });
  }
  await refusalOf(rosa, { index: 2, at: secondAt, P: B });
  // Rojo: si el corrimiento mirara solo las pendientes, la diferida saldria a las 09:00.
  const redDeferred = await rolledBack(async () => {
    const definition = one(await sql('select pg_get_functiondef($1::regprocedure) as d', [ADMIT_NATIVE]), 'definicion').d;
    const cut = definition.replace("and action_row.status in ('pending', 'deferred', 'retryable_failed');",
      "and action_row.status in ('pending');");
    if (cut === definition) fail('el rojo de la diferida no encontro el filtro');
    await db.exec(cut);
    await admitB(rosa);
    return new Date(one(await sql('select due_at from public.scheduled_actions where id = $1', [first.scheduled_action_id]), 'r').due_at).getTime();
  });
  if (redDeferred !== nine) fail('sin la diferida en el filtro, el envio quedaba a las 09:00', redDeferred);
  const second = rowOf(await admitB(rosa), real.flowId, 'el rechazo de la manana');
  const moved = one(await sql('select status, due_at from public.scheduled_actions where id = $1', [first.scheduled_action_id]), 'corrida');
  if (second.reason !== 'ya_en_curso' || moved.status !== 'deferred'
      || new Date(moved.due_at).getTime() !== secondAt.getTime() + 15 * 60_000) {
    fail('el segundo rechazo corre la diferida a los 15 min', { second, moved });
  }
  // Un reintento (retryable_failed, con next_attempt_at): tambien se corre. El estado lo deja el despachador
  // despues de un envio que fallo; aca se siembra, porque el fallo depende de Chatwoot.
  const retry = await rolledBack(async () => {
    await sql(`update public.scheduled_actions set status = 'retryable_failed', next_attempt_at = $2::timestamptz
      where id = $1`, [first.scheduled_action_id, at(day, 9, 20).toISOString()]);
    await refusalOf(rosa, { index: 2, at: new Date(secondAt.getTime() + 10 * 60_000), P: B });
    await admitB(rosa);
    return new Date(one(await sql('select next_attempt_at from public.scheduled_actions where id = $1',
      [first.scheduled_action_id]), 'reintento').next_attempt_at).getTime();
  });
  if (retry !== secondAt.getTime() + 25 * 60_000) fail('el reintento pasa a los 15 min del ultimo rechazo', retry);
  results.diferida = { decision: decision.decision, a_las_nueve: true, corrida_min: 15, rojo: redDeferred === nine };
}

// ---------------------------------------------------------------------------
// 8b. D1: un flujo nativo no usa la clave de un flujo del legado.
// ---------------------------------------------------------------------------
{
  const BLOCKER = 'select public._flow_change_new_flow_blocker($1, $2, $3::jsonb) as r';
  const itemOf = (flujo) => ({ producto: B.funnel, flujo, tipo: 'nuevo', prender: 'cohorte',
    definicion: { ...PAYMENT, producto: B.funnel, flujo, topes: { ...PAYMENT.topes, bolsa: `att9-${flujo.replace(/_/g, '-')}` } } });
  const reserved = {};
  for (const flujo of ['precheckout', 'carrito', 'pago_fallido', 'reactivacion', 'descuento']) {
    reserved[flujo] = one(await sql(BLOCKER, [A.tenant, A.ally, JSON.stringify(itemOf(flujo))]), flujo).r;
    if (reserved[flujo] !== 'flow_key_reserved_for_legacy') fail(`la clave ${flujo} es del legado`, reserved);
  }
  const own = one(await sql(BLOCKER, [A.tenant, A.ally, JSON.stringify(itemOf('pago_rechazado_b'))]), 'propia').r;
  if (own === 'flow_key_reserved_for_legacy') fail('una clave nativa propia no esta reservada', own);
  // Rojo: con el bloqueo de antes, pago_fallido (sin registrar en B) pasaba.
  const redD1 = await withPrevious(['_flow_change_new_flow_blocker(text,text,jsonb)'],
    async () => one(await sql(BLOCKER, [A.tenant, A.ally, JSON.stringify(itemOf('pago_fallido'))]), 'antes').r);
  if (redD1 === 'flow_key_reserved_for_legacy') fail('sin la migracion, la clave del legado pasaba', redD1);
  results.d1 = { reservadas: Object.keys(reserved).length, propia: own, antes: redD1 };
}

// ---------------------------------------------------------------------------
// 9. Lo de antes, igual.
// ---------------------------------------------------------------------------
{
  // El legado de un rechazo de quien no esta en la cohorte: el nativo no lo toma y el legado planifica como hoy.
  const eva = await leadFor('pago-legado-hoy');
  eva.offer = A.offers.at(-1);
  const plan = await refusalFor(1, eva);
  if (plan.created !== true) fail('el legado del pago rechazado, como hoy', plan);
  const policy = one(await sql('select policy_key from public.recovery_cases where id = $1', [plan.recovery_case_id]), 'caso').policy_key;
  if (policy !== THREE_TOUCHES.policy) fail('el caso del legado', policy);
  // La simulacion cuenta los rechazos, no los pendientes cancelados.
  const since = new Date((await dbNow()).getTime() - 7 * 86_400_000).toISOString();
  const counts = await asService(async () => rpc('read_flow_engine_native_trigger_counts_v1', INSTANCE, A.funnel, 'pago_fallido', since));
  const all = one(await sql(`select count(distinct payload #>> '{data,purchase,transaction}')::int as n,
      count(distinct payload #>> '{data,purchase,transaction}') filter (
        where public.payment_failure_refusal_kind(payload #>> '{data,purchase,payment,refusal_reason}',
                                                  payload #>> '{data,purchase,payment,type}') <> 'not_a_refusal')::int as r
    from public.webhook_events where event_type = 'PURCHASE_CANCELED' and payload #>> '{data,product,id}' = $1`,
    [String(A.productId)]), 'rechazos');
  if (counts.trigger !== 'pago_fallido' || counts.events !== all.r || all.r >= all.n) fail('los conteos de la simulacion', { counts, all });
  const countsRed = await withPrevious([`read_flow_engine_native_trigger_counts_v1(jsonb,text,text,${TZ})`],
    () => errorOf(db, () => db.query('select public.read_flow_engine_native_trigger_counts_v1($1::jsonb, $2, $3, $4::timestamptz)',
      [JSON.stringify(INSTANCE), A.funnel, 'pago_fallido', since])));
  if (countsRed?.message !== 'flow_engine_native_trigger_invalid') fail('sin la migracion, la simulacion no cuenta rechazos', countsRed?.message);
  results.legado = { plan: plan.created, simulacion: counts.events, cancelados: all.n };
}

// ---------------------------------------------------------------------------
// El fixture para el bridge.
// ---------------------------------------------------------------------------
const fixture = {
  _capture: {
    origen: 'validate_flow_engine_native_payment_failure.mjs: la cadena entera de migraciones (PGlite), el formulario y '
      + 'los rechazos capturados de Maribel por las admisiones portables',
    encargo: 'E-207',
    regenerar: 'cd tests/sql/followup_engine && node validate_flow_engine_native_payment_failure.mjs --emit',
  },
  ...emitted,
};
if (EMIT) {
  writeFileSync(FIXTURE, `${JSON.stringify(fixture, null, 2)}\n`);
} else if (CHECK) {
  const stored = JSON.parse(readFileSync(FIXTURE, 'utf8'));
  if (JSON.stringify(stored) !== JSON.stringify(fixture)) fail('el fixture del bridge no es el de hoy', { stored, fixture });
}
await db.close();
console.log(JSON.stringify({ ok: true, ...results }));
