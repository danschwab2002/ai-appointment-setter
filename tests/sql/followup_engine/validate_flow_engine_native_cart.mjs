// El motor nativo arranca con el carrito abandonado (migracion 20261028000200,
// E-161, el PR 2 del hito 4 de la version 1).
//
// Por que: el carrito de Maribel (un toque, permiso formulario, la plantilla
// MARKETING maribel_retomar_proceso_01) no cabia en el motor nativo, que solo
// sabia la compra, y el Automation Expert solo crea flujos nativos.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera): el formulario con consentimiento y el carrito pasan por las
// admisiones portables de hoy, que correlacionan el carrito con la intencion,
// como en produccion. El carrito es el capturado de esa prueba
// (hotmart_cart_abandonment_rejected_v1.json) y la compra, la capturada de la
// bienvenida de ATT1 (hotmart_purchase_approved_att1_bienvenida_20261026.json),
// cada uno con la persona sembrada. El paso nativo nombra la plantilla de
// Maribel (tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json).
//
// Prueba:
//   0. la forma: cambian exactamente las funciones del encargo, nacen las dos
//      del permiso del carrito, sin tocar permisos ni atributos de las que ya
//      estaban; correrla dos veces aborta;
//   1. un carrito nativo en cohorte se publica y se abre; con un contacto de la
//      cohorte que dejo el formulario con consentimiento, la admision arranca:
//      el caso con la oferta en el context y offer_code nulo, la intencion en
//      la fila del recorrido y la autorizacion con la evidencia del formulario;
//      fuera de la cohorte, fuera_de_cohorte; sin formulario, no_entra:permiso;
//   2. el despacho: reevaluacion, intento, el contexto del paso con
//      entry_permission formulario y la plantilla de Maribel, el arranque
//      nativo autoriza y la aceptacion cierra; rojo: con la intencion caida
//      despues de admitir, el arranque rechaza con no_entra:permiso;
//   3. la compra lo corta: una compra de la persona antes del envio y la
//      reevaluacion cancela con purchase_detected_any_offer;
//   4. lo de antes, igual: la bienvenida de ATT1 (compra) arranca y su contexto
//      dice compra; un carrito sin flujo nativo de su producto da sin_flujo; el
//      planificador portable del carrito planifica como hoy; las formas
//      cruzadas (carrito con permiso compra, compra con formulario) no se
//      ejecutan;
//   5. la simulacion de un item nuevo cuenta los carritos.
//
// Con --emit escribe tests/fixtures/flow_engine/native_cart_execution_context_rows.json
// (lo que contesta la base, con uuid y horas normalizados) para
// tests/test_flow_engine_native_cart.py; con --check (el de npm test) lo
// compara y falla si difiere.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261028000200_flow_engine_native_cart_v1.sql';
const FIXTURE = join(root, 'tests/fixtures/flow_engine/native_cart_execution_context_rows.json');
const EMIT = process.argv.includes('--emit');
const CHECK = process.argv.includes('--check');
const TZ = 'timestamp with time zone';
const CHANGED = [
  '_flow_engine_native_trigger(jsonb)',
  '_flow_engine_native_steps(text)',
  '_flow_engine_native_unsupported(jsonb)',
  '_flow_engine_admit_native_flow(flow_controls,webhook_events,uuid,text,timestamp with time zone,bigint,bigint,text)',
  `_flow_engine_authorize_native_tail(uuid,uuid,flow_engine_runs,recovery_cases,flow_controls,${TZ})`,
  `read_flow_engine_native_trigger_counts_v1(jsonb,text,text,${TZ})`,
  'read_automation_expert_native_history_v1(jsonb,uuid)',
  'flow_engine_execution_context_v1(uuid)',
];
const ADDED = ['_flow_engine_native_cart_reason(uuid,uuid,text)', '_flow_engine_native_cart_audience(uuid,uuid,text,text,text)'];
const maribel = JSON.parse(readFileSync(join(root,
  'tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json'), 'utf8')).message_templates[0];
const purchaseCaptured = JSON.parse(readFileSync(join(root,
  'tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json'), 'utf8')).payload;
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
if (position !== names.length - 1) fail(`${MIGRATION} tiene que ser la ultima`, names.slice(position + 1));
if (maribel.name !== 'maribel_retomar_proceso_01' || maribel.category !== 'MARKETING') {
  fail('el fixture no es la plantilla de Maribel');
}

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const previousDefinitions = {};
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
  // El contexto del paso se vuelve a crear: su ACL es nueva, con el mismo efecto.
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && (s === 'flow_engine_execution_context_v1(uuid)'
      ? ['prosecdef', 'provolatile', 'service_x', 'api_x'] : ['acl', 'prosecdef', 'provolatile'])
      .some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, CHANGED) || !sameSet(added, ADDED) || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  for (const signature of ADDED) {
    if (after.get(signature).service_x || after.get(signature).api_x) fail(`${signature}: no es de nadie de la API`);
  }
  if (!after.get('flow_engine_execution_context_v1(uuid)').service_x
      || after.get('flow_engine_execution_context_v1(uuid)').api_x) {
    fail('el contexto del paso es solo de service_role');
  }
  for (const signature of CHANGED) {
    if (!after.get(signature).definition.includes('flow_engine_native_cart')) fail(`${signature}: sin la marca`);
    previousDefinitions[signature] = before.get(signature).definition;
  }
  const columns = (await db.query(`select column_name from information_schema.columns
    where table_schema = 'public' and table_name = 'flow_engine_native_runs' order by ordinal_position`)).rows
    .map((row) => row.column_name);
  if (!columns.includes('audience_purchase_intent_id') || !columns.includes('audience_precheckout_submission_id')) {
    fail('flow_engine_native_runs sin las columnas de la intencion', columns);
  }
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'flow_engine_native_cart_already_applied') fail('la segunda corrida aborto por otra cosa', error.message);
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { cambiadas: changed.length, nuevas: added.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 (buildWorld, la cadena entera) con un carrito nativo.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, dbNow, reserve, startNew, accept, cartFor, planCart, rpc } = world;
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

// El carrito de un toque de Maribel (instancia.toml de Maribel, [flujos]
// carrito: gracia 0, un envio), como lo crearia el AE con un item nuevo.
const CART = {
  esquema: 'setter-flujo/v1', flujo: 'carrito_nativo', producto: A.funnel, estado: 'borrador', nombre: 'Carrito nativo',
  dispara: { evento: 'carrito' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ plantilla: maribel.name, idioma: maribel.language, variables: ['nombre', 'producto'] }],
  termina: { si: ['compra'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-carrito-nativo', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
const openNative = async (flowKey, definition, extra = {}) => {
  const flowId = await register(A.funnel, flowKey);
  await cycle([{ ...versionItem(flowId, A.funnel, flowKey, definition), ...extra }], `v1 de ${flowKey}`);
  const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'propuesta').proposal_id;
  const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [flowId, proposalId, A.timezone]), 'abrir').r);
  return { flowId, opened };
};
const cart = await openNative('carrito_nativo', CART);
if (!cart.opened.ok || cart.opened.state !== 'cohorte' || cart.opened.pool !== 'att1-carrito-nativo') {
  fail('abrir el carrito nativo', cart.opened);
}
const cohort = async (flowId, contactId) => sql(
  `insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`, [flowId, contactId]);
const flowEventOf = (lead, tipo = 'carrito') => ({
  fuente: 'hotmart', tipo, evento_id: lead.event, producto_ref: A.funnel, oferta: lead.offer.offer_code,
  contacto_id: lead.contact, momento: lead.cartArgs[6], chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
  destino: lead.phone,
});
const admitNative = async (flowEvent) => asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)', [JSON.stringify(INSTANCE), JSON.stringify(flowEvent)])).rows);
const rowOf = (rows, flowId, label) => one(rows.filter((row) => row.flow_id === flowId), label);

// ---------------------------------------------------------------------------
// 1. La admision.
// ---------------------------------------------------------------------------
const maria = await cartFor('nativo-maria');
await cohort(cart.flowId, maria.contact);
const flowEvent = flowEventOf(maria);
const started = rowOf(await admitNative(flowEvent), cart.flowId, 'carrito nativo');
if (started.outcome !== 'motor_arranco' || started.created !== true) fail('el carrito abre el recorrido nativo', started);
const replay = rowOf(await admitNative(flowEvent), cart.flowId, 'repeticion');
if (replay.outcome !== 'motor_arranco' || replay.created !== false || replay.recovery_case_id !== started.recovery_case_id) {
  fail('la repeticion del evento devuelve su caso', replay);
}
const kase = one(await sql('select * from public.recovery_cases where id = $1', [started.recovery_case_id]), 'caso');
const native = one(await sql('select * from public.flow_engine_native_runs where recovery_case_id = $1',
  [started.recovery_case_id]), 'recorrido nativo');
const intentId = one(await sql(`select purchase_intent_id from public.hotmart_purchase_intent_correlations
  where webhook_event_id = $1 and outcome = 'resolved'`, [maria.event]), 'correlacion').purchase_intent_id;
if (kase.offer_code !== null || kase.policy_key !== `flujo:${cart.flowId}` || kase.abandonment_event_id !== maria.event
    || kase.context.flow_engine_offer !== maria.offer.offer_code || kase.context.flow_engine_trigger !== 'carrito'
    || native.trigger_event !== 'carrito' || native.audience_purchase_intent_id !== intentId
    || !native.audience_precheckout_submission_id) {
  fail('el caso nativo del carrito', { kase, native });
}
const grant = one(await sql(`select * from public.contact_authorizations where contact_id = $1 and purpose = 'cart_recovery'`,
  [maria.contact]), 'autorizacion');
if (grant.authorization_status !== 'allowed' || grant.authorization_source !== 'system'
    || grant.evidence.reason !== 'precheckout_whatsapp_consent' || grant.evidence.purchase_intent_id !== intentId
    || grant.evidence.recovery_case_id !== started.recovery_case_id) {
  fail('la autorizacion del carrito', grant);
}
const policy = one(await sql('select * from public.followup_policy_versions where policy_key = $1', [kase.policy_key]), 'politica');
if (policy.max_automatic_messages !== 1 || policy.steps.length !== 1 || policy.steps[0].step_key !== 'first_contact') {
  fail('la politica del carrito nativo', policy);
}
// Fuera de la cohorte: no arranca y no deja fila.
const outsider = await cartFor('nativo-afuera');
const outside = rowOf(await admitNative(flowEventOf(outsider)), cart.flowId, 'fuera de la cohorte');
if (outside.outcome !== 'motor_no_arranca' || outside.reason !== 'fuera_de_cohorte') fail('fuera de la cohorte', outside);
// Sin formulario (sin la intencion con consentimiento): no entra.
const noForm = await cartFor('nativo-sin-formulario', { form: false });
await cohort(cart.flowId, noForm.contact);
const refused = rowOf(await admitNative(flowEventOf(noForm)), cart.flowId, 'sin formulario');
if (refused.outcome !== 'motor_no_arranca' || refused.reason !== 'no_entra:permiso') fail('sin formulario', refused);
// Rojo: con las definiciones de antes, el carrito no lo ejecuta el motor nativo.
const before = await cartFor('nativo-antes');
await cohort(cart.flowId, before.contact);
const old = await rolledBack(async () => {
  for (const signature of ['_flow_engine_native_trigger(jsonb)', '_flow_engine_native_unsupported(jsonb)']) {
    await db.exec(previousDefinitions[signature]);
  }
  return rowOf(await admitNative(flowEventOf(before)), cart.flowId, 'antes de la migracion');
});
if (old.outcome !== 'motor_no_arranca' || old.reason !== 'motor_nativo:disparador') fail('sin la migracion', old);
const rejections = await sql(`select source_ref::text as source_ref, reason from public.flow_engine_start_rejections
  where flow_id = $1 order by reason`, [cart.flowId]);
if (rejections.length !== 1 || rejections[0].reason !== 'no_entra:permiso' || rejections[0].source_ref !== noForm.event) {
  fail('las filas de rechazo', rejections);
}
results.admision = { salida: started.outcome, afuera: outside.reason, sin_formulario: refused.reason,
  autorizacion: grant.evidence.reason, sin_la_migracion: old.reason };

// ---------------------------------------------------------------------------
// 2. El despacho.
// ---------------------------------------------------------------------------
const lead = { label: 'nativo-maria', chatwoot: '530' };
const reserved = await reserve(lead, started.scheduled_action_id);
if (reserved.attempt.mode !== 'approved_template') fail('el intento', reserved.attempt);
const step = one(await asService(async () => (await db.query('select * from public.flow_engine_execution_context_v1($1)',
  [started.scheduled_action_id])).rows), 'contexto del paso');
if (step.template_name !== maribel.name || step.entry_permission !== 'formulario'
    || JSON.stringify(step.template_variables) !== JSON.stringify(['nombre', 'producto'])) {
  fail('el contexto del paso', step);
}
const context = await asService(async () => one((await db.query(
  'select * from public.get_followup_execution_context($1, $2, $3, $4)',
  [started.scheduled_action_id, reserved.worker, reserved.lease, reserved.now])).rows, 'contexto'));
if (context.offer_code !== maria.offer.offer_code || context.product_name !== A.productName) fail('el contexto del despacho', context);
// Rojo: la intencion deja de dar el permiso despues de admitir; el arranque la vuelve a mirar.
const red = await rolledBack(async () => {
  await db.query('update public.purchase_intents set whatsapp_contact_authorized = false where id = $1', [intentId]);
  return startNew(reserved);
});
if (red.outcome !== 'rechazado' || red.reason !== 'no_entra:permiso') fail('el arranque vuelve a mirar la intencion', red);
const startedSend = await startNew(reserved);
if (startedSend.outcome !== 'request_started' || !startedSend.flow_authorization_id) fail('el arranque nativo', startedSend);
const accepted = await accept(lead, reserved);
const pool = one(await sql('select count(*)::int as n from public.flow_engine_request_authorizations where pool = $1',
  ['att1-carrito-nativo']), 'bolsa').n;
if (pool !== 1) fail('la bolsa del carrito nativo cuenta el envio', pool);
results.despacho = { permiso: step.entry_permission, plantilla: step.template_name, rojo: red.reason,
  arranque: startedSend.outcome, aceptacion: accepted.status };

// ---------------------------------------------------------------------------
// 3. La compra lo corta antes del envio.
// ---------------------------------------------------------------------------
const buyer = await cartFor('nativo-compra');
await cohort(cart.flowId, buyer.contact);
const buyerStart = rowOf(await admitNative(flowEventOf(buyer)), cart.flowId, 'carrito de quien compra');
if (buyerStart.outcome !== 'motor_arranco') fail('el carrito de quien compra', buyerStart);
{
  const payload = structuredClone(purchaseCaptured);
  const now = await dbNow();
  const shift = now.getTime() - 60_000 - payload.creation_date;
  for (const key of ['approved_date', 'order_date']) payload.data.purchase[key] += shift;
  payload.creation_date += shift;
  payload.id = `nativo-compra-${buyer.email}`;
  payload.data.buyer = { ...payload.data.buyer, name: buyer.name, email: buyer.email, checkout_phone: buyer.phone };
  payload.data.product = { ...payload.data.product, id: A.productId };
  payload.data.purchase.offer.code = buyer.offer.offer_code;
  payload.data.purchase.transaction = `HPNATIVO${buyer.contact.replace(/-/g, '').slice(0, 10).toUpperCase()}`;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), buyer.email, buyer.phone]), 'compra');
  if (admitted.outcome !== 'inserted') fail('admitir la compra', admitted);
  await sql('select * from public.apply_hotmart_purchase_approved($1,$2,$3,$4,$5,$6,$7::timestamptz)',
    [admitted.webhook_event_id, buyer.email, buyer.phone, String(A.productId), buyer.offer.offer_code,
      payload.data.purchase.transaction, new Date(payload.data.purchase.approved_date).toISOString()]);
}
const cut = await reserve({ label: 'nativo-compra', chatwoot: '531' }, buyerStart.scheduled_action_id, { expect: 'cancel' });
if (cut.decision.reason_code !== 'purchase_detected_any_offer') fail('la compra corta el carrito nativo', cut.decision);
const cutCase = one(await sql('select status from public.recovery_cases where id = $1', [buyerStart.recovery_case_id]), 'caso cortado');
if (!['cancelled', 'won'].includes(cutCase.status)) fail('el caso cortado', cutCase);
results.compra = { reevaluacion: cut.decision.decision, motivo: cut.decision.reason_code, caso: cutCase.status };

// ---------------------------------------------------------------------------
// 4. Lo de antes, igual.
// ---------------------------------------------------------------------------
{
  // La bienvenida de ATT1 (compra, permiso compra) arranca y su paso dice compra.
  const WELCOME = {
    esquema: 'setter-flujo/v1', flujo: 'bienvenida', producto: A.funnel, estado: 'borrador', nombre: 'Bienvenida att1',
    dispara: { evento: 'compra' }, entra: { permiso: 'compra', contesto_a: [] },
    pasos: [{ plantilla: 'att1_bienvenida_01', idioma: 'es_MX', variables: ['producto'] }, { etiquetar: 'cliente' }],
    termina: { si: [], vence: '1d' }, horario: 'siempre',
    topes: { bolsa: 'att1-bienvenida', por_dia: 'sin_tope', total: 'sin_tope',
      repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } },
    prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  };
  const welcome = await openNative('bienvenida', WELCOME, { ok_del_cliente: { quien: 'dan', medio: 'whatsapp', fecha: '2026-10-26' } });
  if (!welcome.opened.ok) fail('abrir la bienvenida', welcome.opened);
  const payload = structuredClone(purchaseCaptured);
  const now = await dbNow();
  const shift = now.getTime() - 60_000 - payload.creation_date;
  for (const key of ['approved_date', 'order_date']) payload.data.purchase[key] += shift;
  payload.creation_date += shift;
  const phone = payload.data.buyer.checkout_phone.replace(/\D/g, '');
  const contactId = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
    values ($1, $2, $3, 'MX') returning id`, [payload.data.buyer.name, payload.data.buyer.email, phone]), 'contacto').id;
  await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1, 'email', $2, $2, 'hotmart'), ($1, 'phone', $3, $3, 'hotmart')`, [contactId, payload.data.buyer.email, phone]);
  await cohort(welcome.flowId, contactId);
  const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), payload.data.buyer.email, phone]), 'compra');
  await sql('select * from public.apply_hotmart_purchase_approved($1,$2,$3,$4,$5,$6,$7::timestamptz)',
    [admitted.webhook_event_id, payload.data.buyer.email, phone, String(payload.data.product.id),
      payload.data.purchase.offer.code, payload.data.purchase.transaction,
      new Date(payload.data.purchase.approved_date).toISOString()]);
  const purchaseEvent = {
    fuente: 'hotmart', tipo: 'compra', evento_id: admitted.webhook_event_id, producto_ref: A.funnel,
    oferta: payload.data.purchase.offer.code, contacto_id: contactId, momento: new Date(payload.creation_date).toISOString(),
    chatwoot: { account_id: A.accountId, inbox_id: A.inboxId }, destino: phone,
  };
  const rows = await admitNative(purchaseEvent);
  const welcomeRow = rowOf(rows, welcome.flowId, 'bienvenida');
  if (welcomeRow.outcome !== 'motor_arranco' || rows.some((row) => row.flow_id === cart.flowId)) fail('la compra abre la bienvenida', rows);
  const welcomeNative = one(await sql('select * from public.flow_engine_native_runs where recovery_case_id = $1',
    [welcomeRow.recovery_case_id]), 'recorrido de la bienvenida');
  const welcomeGrant = one(await sql(`select * from public.contact_authorizations where contact_id = $1 and purpose = 'cart_recovery'`,
    [contactId]), 'autorizacion de la compra');
  if (welcomeNative.trigger_event !== 'compra' || welcomeNative.audience_purchase_intent_id !== null
      || welcomeGrant.authorization_source !== 'hotmart' || welcomeGrant.evidence.reason !== 'purchase_approved') {
    fail('la bienvenida, como antes', { welcomeNative, welcomeGrant });
  }
  const welcomeStep = one(await asService(async () => (await db.query('select * from public.flow_engine_execution_context_v1($1)',
    [welcomeRow.scheduled_action_id])).rows), 'contexto de la bienvenida');
  if (welcomeStep.entry_permission !== 'compra' || welcomeStep.template_name !== 'att1_bienvenida_01') {
    fail('el contexto de la bienvenida', welcomeStep);
  }
  results.bienvenida = { salida: welcomeRow.outcome, permiso: welcomeStep.entry_permission };
}
{
  // Un carrito de un producto sin flujo nativo del carrito: sin_flujo.
  const other = await cartFor('nativo-otro-producto', { P: world.B });
  const rows = await asService(async () => (await db.query('select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
    [JSON.stringify(INSTANCE), JSON.stringify({ ...flowEventOf(other), producto_ref: world.B.funnel })])).rows);
  if (rows.length !== 1 || rows[0].outcome !== 'sin_flujo') fail('sin flujo nativo del carrito', rows);
  // El planificador portable del carrito, como hoy: planifica y el caso no es nativo.
  const plan = await planCart(other);
  if (plan.created !== true) fail('el planificador del carrito', plan);
  const nativeRows = await sql('select 1 from public.flow_engine_native_runs where recovery_case_id = $1', [plan.recovery_case_id]);
  if (nativeRows.length !== 0) fail('el caso del legado no es nativo');
  results.legado = { sin_flujo: rows[0].outcome, plan: plan.created };
}
{
  // Las formas cruzadas no se ejecutan.
  const crossed = one(await sql(`select public._flow_engine_native_unsupported($1::jsonb) as a,
      public._flow_engine_native_unsupported($2::jsonb) as b, public._flow_engine_native_unsupported($3::jsonb) as c,
      public._flow_engine_native_unsupported($4::jsonb) as d, public._flow_engine_native_unsupported($5::jsonb) as e,
      public._flow_engine_native_unsupported($6::jsonb) as f`,
  [JSON.stringify({ ...CART, entra: { permiso: 'compra' } }),
    JSON.stringify({ ...CART, dispara: { evento: 'compra' } }),
    JSON.stringify({ ...CART, dispara: { evento: 'pago_fallido' } }),
    // El carrito de tres toques del legado (A1, A2 y Z): el nativo manda uno solo.
    JSON.stringify(world.cartDefinition(A.funnel)),
    JSON.stringify({ ...CART, pasos: [{ conversar: 'agente' }] }),
    // etiquetar marca clienta: con el carrito, nunca.
    JSON.stringify({ ...CART, pasos: [...CART.pasos, { etiquetar: 'cliente' }] })]), 'cruzadas');
  if (crossed.a !== 'motor_nativo:permiso' || crossed.b !== 'motor_nativo:permiso' || crossed.c !== 'motor_nativo:disparador'
      || crossed.d !== 'motor_nativo:forma' || crossed.e !== 'motor_nativo:forma' || crossed.f !== 'motor_nativo:forma') {
    fail('las formas cruzadas', crossed);
  }
  const fine = one(await sql('select public._flow_engine_native_unsupported($1::jsonb) as r', [JSON.stringify(CART)]), 'carrito').r;
  if (fine !== null) fail('el carrito de un toque se ejecuta', fine);
}

// ---------------------------------------------------------------------------
// 5. La simulacion cuenta los carritos.
// ---------------------------------------------------------------------------
{
  const since = new Date((await dbNow()).getTime() - 7 * 86_400_000).toISOString();
  const counts = await asService(async () => ({
    carrito: await rpc('read_flow_engine_native_trigger_counts_v1', INSTANCE, A.funnel, 'carrito', since),
    compra: await rpc('read_flow_engine_native_trigger_counts_v1', INSTANCE, A.funnel, 'compra', since),
  }));
  // maria, afuera, sin formulario, antes y la de la compra: cinco carritos de cinco personas de A.
  if (counts.carrito.trigger !== 'carrito' || counts.carrito.events !== 5 || counts.carrito.people !== 5
      || counts.compra.events !== 2) {
    fail('los conteos de la simulacion', counts);
  }
  let error = null;
  try {
    await rpc('read_flow_engine_native_trigger_counts_v1', INSTANCE, A.funnel, 'pago_fallido', since);
  } catch (caught) {
    error = caught;
  }
  if (error?.message !== 'flow_engine_native_trigger_invalid') fail('otro disparador', error?.message);
  results.simulacion = { carritos: counts.carrito.events, compras: counts.compra.events };
}

// ---------------------------------------------------------------------------
// El fixture para el bridge.
// ---------------------------------------------------------------------------
const ids = new Map([[started.scheduled_action_id, 'ACTION'], [started.recovery_case_id, 'CASE'],
  [maria.contact, 'CONTACT'], [maria.event, 'EVENT'], [cart.flowId, 'FLOW']]);
const normalize = (value) => JSON.parse(JSON.stringify(value, (key, v) => {
  if (typeof v === 'string' && ids.has(v)) return `<${ids.get(v)}>`;
  if (key === 'momento') return '<MOMENTO>';
  return v;
}));
const emitted = {
  _capture: {
    origen: 'validate_flow_engine_native_cart.mjs: la cadena entera de migraciones (PGlite), el formulario y el '
      + 'carrito por las admisiones portables, y el paso con la plantilla de Maribel',
    encargo: 'E-161',
    regenerar: 'cd tests/sql/followup_engine && node validate_flow_engine_native_cart.mjs --emit',
  },
  flow_event: normalize({ ...flowEvent, oferta: '<OFERTA>', destino: '<DESTINO>' }),
  admission: normalize({ flow_id: started.flow_id, outcome: started.outcome, reason: started.reason, created: started.created }),
  step_context: normalize({
    flow_id: step.flow_id, version: step.version, product_ref: step.product_ref, flow_key: step.flow_key,
    step_key: step.step_key, send_ordinal: step.send_ordinal, template_name: step.template_name,
    template_language: step.template_language, template_variables: step.template_variables,
    template_button: step.template_button, legacy_dispatch_ok: step.legacy_dispatch_ok,
    entry_permission: step.entry_permission,
  }),
};
if (EMIT) {
  writeFileSync(FIXTURE, `${JSON.stringify(emitted, null, 2)}\n`);
} else if (CHECK) {
  const stored = JSON.parse(readFileSync(FIXTURE, 'utf8'));
  if (JSON.stringify(stored) !== JSON.stringify(emitted)) {
    fail('native_cart_execution_context_rows.json no es lo que da la cadena: correr con --emit', emitted);
  }
}
await db.close();
console.log(`validate_flow_engine_native_cart: ok ${JSON.stringify(results)}`);
