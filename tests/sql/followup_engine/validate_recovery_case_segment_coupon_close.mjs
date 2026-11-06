// El freno del cupon de la campana cierra el caso del carrito (migracion
// 20261106000100, E-339; contrato docs/contracts/segment-knowledge-v1.md).
//
// Por que: A2 o Z frenados por un segmento con cupon dejaban la secuencia y el
// caso en active sin accion, y nada los cerraba: un carrito nuevo de esa oferta
// no arrancaba. Dan decidio cerrar el caso (06/11).
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera): el formulario, el carrito capturado y los toques reservados por los
// caminos de hoy. La compra es la capturada de la bienvenida de ATT1, con la
// oferta B sustituida.
//
// Prueba:
//   0. la forma: suma exactamente una funcion, solo de service_role, no cambia
//      ninguna otra ni su ACL, y correrla dos veces aborta;
//   1. el rojo: cerrar solo la accion deja el caso abierto, y el carrito nuevo
//      de la misma oferta revienta en el planificador;
//   2. A2: cierra intento, accion, secuencia y caso con su motivo, no planifica
//      Z, sale de _flow_engine_legacy_in_flight, y el carrito nuevo arranca
//      limpio en otro caso;
//   3. Z: igual, con A2 ya aceptado;
//   4. el mismo intento otra vez no cambia nada;
//   5. otro lease o un paso que no es A2 ni Z no cierran nada;
//   6. el orden inverso: con la compra por otra oferta primero, la reevaluacion
//      cancela el toque, el freno no llega a correr; si una compra real llega
//      entre la reevaluacion y el cierre, el cierre contesta case_not_open sin
//      tocar nada y sin 22000;
//   7. el motor: cerrado el caso, el carrito nuevo arranca otro recorrido (sin
//      el cierre, ya_en_curso).
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, errorOf, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261106000100_recovery_case_segment_coupon_close_v1.sql';
const SIGNATURE = 'close_recovery_case_segment_coupon_v1(uuid,uuid,text,bigint,timestamp with time zone)';
const REASON = 'segment_coupon_active';
const results = {};
const fixture = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));
const CAPTURED_PURCHASE = fixture('hotmart_purchase_approved_att1_bienvenida_20261026.json').payload;

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
for (const name of names.slice(position + 1)) {
  const text = readFileSync(join(root, 'supabase/migrations', name), 'utf8');
  if (text.includes('close_recovery_case_segment_coupon') || text.includes('_finalize_followup_delivery_attempt')) {
    fail(`${name} toca el cierre o la finalizacion: revisar esta prueba`);
  }
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
  if (before.has(SIGNATURE)) fail('el cierre ya existe antes de la migracion');
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.has(s) && after.get(s).definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && ['acl', 'prosecdef', 'provolatile'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (changed.length !== 0 || !sameSet(added, [SIGNATURE]) || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  const fn = after.get(SIGNATURE);
  if (!fn.service_x || fn.api_x || !fn.prosecdef || fn.provolatile !== 'v') {
    fail('el cierre es solo de service_role, security definer y volatile', fn);
  }
  if (!fn.definition.includes('recovery_case_segment_coupon_close')) fail('el cierre sin su marca');
  let error = null;
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  } catch (caught) {
    error = caught;
  }
  await db.exec('rollback').catch(() => {});
  if (error?.code !== '55000' || error.message !== 'recovery_case_segment_coupon_close_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  results.forma = { nuevas: added.length, cambiadas: changed.length, segunda_corrida: error.message };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, FLOWS, cartFor, planCart, admit, reserve, startOld, startNew, accept, stepOf, stepOfId, caseOf, runOf,
  route, createRoute, dbNow, sleep, SCOPE } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
await db.exec('alter role service_role bypassrls');
const asService = async (run) => {
  await db.exec('set role service_role');
  try {
    return await run();
  } finally {
    await db.exec('reset role').catch(() => {});
  }
};
const close = async (reserved, over = {}) => one(await asService(async () => sql(
  'select outcome from public.close_recovery_case_segment_coupon_v1($1,$2,$3,$4,$5)',
  [over.action ?? reserved.actionId, reserved.attempt.id, reserved.worker, over.lease ?? reserved.lease, await dbNow()])),
'cierre').outcome;
const sendTouch = async (lead, actionId, start = startOld) => {
  const reserved = await reserve(lead, actionId);
  const started = await start(reserved);
  if ((started.phase ?? started.outcome) !== 'request_started') fail(`${lead.label}: no arranco`, started);
  await accept(lead, reserved);
};
const atA2 = async (label) => {
  const lead = await cartFor(label);
  const plan = await planCart(lead);
  if (!plan.scheduled_action_id) fail(`${label}: el carrito no planifico`, plan);
  await sendTouch(lead, plan.scheduled_action_id);
  await sleep(1_100);
  return lead;
};
const sequenceOf = async (lead) => one(await sql(
  'select status, completion_reason from public.followup_sequences where recovery_case_id = $1',
  [lead.plan.recovery_case_id]), 'secuencia');
const closedIntact = async (lead, actionId, label) => {
  const [action, attempt, sequence, recovery] = [await stepOfId(actionId), one(await sql(
    'select outcome, reason_code, finalized_next_attempt_at from public.followup_delivery_attempts where action_id = $1',
    [actionId]), 'intento'), await sequenceOf(lead), await caseOf(lead)];
  if (action.status !== 'permanent_failed' || action.terminal_reason !== REASON
      || attempt.outcome !== 'failed_before_request' || attempt.reason_code !== REASON
      || attempt.finalized_next_attempt_at !== null
      || sequence.status !== 'completed' || sequence.completion_reason !== REASON
      || recovery.status !== 'cancelled' || recovery.closed_at === null || recovery.won_at !== null) {
    fail(`${label}: el cierre`, { action, attempt, sequence, recovery });
  }
};
const inFlight = async () => one(await sql(
  "select public._flow_engine_legacy_in_flight($1, 'carrito') as n", [SCOPE]), 'en vuelo').n;

// ---------------------------------------------------------------------------
// 1. El rojo: sin el cierre, el caso queda abierto y el carrito nuevo choca.
// ---------------------------------------------------------------------------
{
  const lead = await atA2('rojo');
  const reserved = await reserve(lead, (await stepOf(lead, 'cart_second_touch')).id);
  await db.query('select * from public.finalize_followup_delivery_attempt($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)',
    [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, 'failed_before_request', null, null,
      REASON, null, null, await dbNow()]);
  if ((await caseOf(lead)).status !== 'active' || (await sequenceOf(lead)).status !== 'active') {
    fail('el rojo: sin el cierre el caso tendria que seguir abierto');
  }
  await cartFor(lead);
  const error = await errorOf(db, () => planCart(lead));
  // El planificador reusa el caso abierto y le abre otra secuencia: la base lo rechaza.
  if (!error?.message.includes('followup_sequences_one_active_per_case_idx')) {
    fail('el rojo: el carrito nuevo', error?.message ?? 'paso');
  }
  results.rojo = error.message;
}

// ---------------------------------------------------------------------------
// 2. A2.
// ---------------------------------------------------------------------------
{
  const lead = await atA2('freno-a2');
  const flying = await inFlight();
  const a2 = await stepOf(lead, 'cart_second_touch');
  const reserved = await reserve(lead, a2.id);
  if (await close(reserved) !== 'closed') fail('A2: el cierre');
  await closedIntact(lead, a2.id, 'A2');
  const planned = await sql(`select id from public.scheduled_actions
    where recovery_case_id = $1 and step_key = 'cart_last_touch'`, [lead.plan.recovery_case_id]);
  if (planned.length !== 0) fail('A2: quedo un Z planificado', planned);
  if (await inFlight() !== flying - 1) fail('A2: sigue contando en el legado en vuelo');
  const closed = lead.plan.recovery_case_id;
  await cartFor(lead);
  const plan = await planCart(lead);
  if (plan.recovery_case_id === closed || !plan.scheduled_action_id || plan.created !== true) {
    fail('A2: el carrito nuevo no arranco limpio', plan);
  }
  results.a2 = { caso: 'cancelled', carrito_nuevo: 'otro caso' };

  // 4. El mismo intento otra vez.
  lead.plan = { recovery_case_id: closed };
  if (await close(reserved) !== 'case_not_open') fail('repetir: el cierre');
  await closedIntact(lead, a2.id, 'repetir');
  results.repetir = 'case_not_open';
}

// ---------------------------------------------------------------------------
// 3. Z.
// ---------------------------------------------------------------------------
{
  const lead = await atA2('freno-z');
  await sendTouch(lead, (await stepOf(lead, 'cart_second_touch')).id);
  await sleep(1_100);
  const z = await stepOf(lead, 'cart_last_touch');
  const reserved = await reserve(lead, z.id);
  if (await close(reserved) !== 'closed') fail('Z: el cierre');
  await closedIntact(lead, z.id, 'Z');
  results.z = 'closed';
}

// ---------------------------------------------------------------------------
// 5. Otro lease y otro paso.
// ---------------------------------------------------------------------------
{
  const lead = await atA2('cerco');
  const a2 = await stepOf(lead, 'cart_second_touch');
  const reserved = await reserve(lead, a2.id);
  const stale = await errorOf(db, () => close(reserved, { lease: reserved.lease + 1 }));
  if (!stale || (await caseOf(lead)).status !== 'active' || (await stepOfId(a2.id)).status === 'permanent_failed') {
    fail('otro lease: tendria que frenar sin tocar nada', stale?.message ?? 'paso');
  }
  results.otro_lease = stale.code ?? stale.message;

  const first = await cartFor('primer-toque');
  const plan = await planCart(first);
  const a1 = await reserve(first, plan.scheduled_action_id);
  const wrong = await errorOf(db, () => close(a1));
  if (wrong?.message !== 'segment_coupon_close_step_invalid' || (await caseOf(first)).status === 'cancelled') {
    fail('A1: no se cierra por aca', wrong?.message ?? 'paso');
  }
  results.otro_paso = wrong.message;
}

// ---------------------------------------------------------------------------
// 6. El orden inverso: la compra por otra oferta.
// ---------------------------------------------------------------------------
let purchaseSerial = 0;
const purchase = async (lead, { sameOffer = false } = {}) => {
  purchaseSerial += 1;
  const now = await dbNow();
  const other = sameOffer ? lead.offer : A.offers.find((offer) => offer.offer_code !== lead.offer.offer_code);
  if (!other) fail('el mundo no tiene otra oferta');
  const payload = structuredClone(CAPTURED_PURCHASE);
  payload.id = `freno-compra-${purchaseSerial}`;
  payload.creation_date = now.getTime() - 60_000;
  Object.assign(payload.data.buyer, { name: lead.name, first_name: lead.name.split(' ')[0], email: lead.email,
    checkout_phone: lead.phone });
  payload.data.product.id = A.productId;
  payload.data.purchase.offer.code = other.offer_code;
  payload.data.purchase.transaction = `HP${String(970000000 + purchaseSerial)}`;
  payload.data.purchase.approved_date = now.getTime() - 60_000;
  payload.data.purchase.order_date = now.getTime() - 120_000;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone]), 'la compra');
  if (admitted.outcome !== 'inserted') fail('admitir la compra', admitted);
  if (sameOffer) {
    one(await sql('select * from public.apply_hotmart_purchase_approved($1,$2,$3,$4,$5,$6,$7::timestamptz)',
      [admitted.webhook_event_id, lead.email, lead.phone, String(A.productId), other.offer_code,
        payload.data.purchase.transaction, new Date(payload.data.purchase.approved_date).toISOString()]), 'aplicar');
  }
};
{
  // Primero la compra: la reevaluacion cancela A2 y el bridge no llega a armarlo.
  const lead = await atA2('compra-primero');
  await purchase(lead);
  const a2 = await stepOf(lead, 'cart_second_touch');
  const cancelled = await reserve(lead, a2.id, { expect: 'cancel' });
  if (cancelled.decision.reason_code !== 'purchase_detected_any_offer'
      || (await caseOf(lead)).status !== 'cancelled') {
    fail('compra primero', cancelled.decision);
  }
  results.compra_primero = cancelled.decision.reason_code;

  // La compra de la misma oferta entre la reevaluacion y el cierre, por el camino
  // real: gana el caso, cancela la accion y el trigger cierra el intento con
  // purchase_detected. El cierre no toca nada y no da 22000.
  const late = await atA2('compra-en-el-medio');
  const a2Late = await stepOf(late, 'cart_second_touch');
  const reserved = await reserve(late, a2Late.id);
  await purchase(late, { sameOffer: true });
  const state = async () => ({ recovery: (await caseOf(late)).status, action: (await stepOfId(a2Late.id)).status,
    sequence: (await sequenceOf(late)).status, attempt: one(await sql(
      'select phase, outcome, reason_code from public.followup_delivery_attempts where id = $1',
      [reserved.attempt.id]), 'intento') });
  const bought = await state();
  if (bought.recovery !== 'won' || bought.action !== 'cancelled' || bought.attempt.reason_code !== 'purchase_detected') {
    fail('compra en el medio: la compra real', bought);
  }
  if (await close(reserved) !== 'case_not_open') fail('compra en el medio: el cierre');
  if (JSON.stringify(await state()) !== JSON.stringify(bought)) fail('compra en el medio: el cierre toco algo');
  results.compra_en_el_medio = { cierre: 'case_not_open', intento: bought.attempt.reason_code };
}

// ---------------------------------------------------------------------------
// 7. El motor.
// ---------------------------------------------------------------------------
{
  const created = await createRoute(A.funnel, 'carrito', 'carrito', SCOPE);
  if (!created.ok) fail('la ruta del carrito', created);
  const toMotor = await route(A.funnel, 'carrito', FLOWS.carrito, 'motor');
  if (!toMotor.ok) fail('el carrito a motor', toMotor);
  const start = async (label) => {
    const lead = await cartFor(label);
    const row = await admit(lead);
    if (row.outcome !== 'motor_arranco') fail(`${label}: la admision`, row);
    await sendTouch(lead, lead.plan.scheduled_action_id, startNew);
    await sleep(1_100);
    return lead;
  };
  const stuck = await start('motor-sin-cierre');
  const reservedStuck = await reserve(stuck, (await stepOf(stuck, 'cart_second_touch')).id);
  await db.query('select * from public.finalize_followup_delivery_attempt($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)',
    [reservedStuck.actionId, reservedStuck.attempt.id, reservedStuck.worker, reservedStuck.lease,
      'failed_before_request', null, null, REASON, null, null, await dbNow()]);
  await cartFor(stuck);
  const blocked = await admit(stuck);
  if (blocked.outcome !== 'motor_no_arranca' || blocked.reason !== 'ya_en_curso') fail('el motor: sin el cierre', blocked);

  const lead = await start('motor-con-cierre');
  const closed = lead.plan.recovery_case_id;
  const reserved = await reserve(lead, (await stepOf(lead, 'cart_second_touch')).id);
  if (await close(reserved) !== 'closed') fail('el motor: el cierre');
  await cartFor(lead);
  const row = await admit(lead);
  const run = row.recovery_case_id ? await runOf(row.recovery_case_id) : null;
  if (row.outcome !== 'motor_arranco' || row.recovery_case_id === closed || run?.flow_id !== FLOWS.carrito) {
    fail('el motor: el carrito nuevo', { row, run });
  }
  results.motor = { sin_cierre: blocked.reason, con_cierre: row.outcome };
}

console.log(JSON.stringify(results));
await db.close();
