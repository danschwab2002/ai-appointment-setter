// Pasar un flujo del legado al nativo sin mensajes dobles, con varios toques
// (E-483, C10 del mapa «el AE maneja todos los flujos», 2026-11-12, riesgo 2).
// Sin migracion: mide que la guarda del doble caso (20261030000100, la del pago
// rechazado de 20261030000200) cubre tambien los toques SIGUIENTES, no solo el
// arranque, y fija la paridad de la politica del carrito de ATT1 v1 <-> nativo.
//
// Por que: la guarda se escribio con el nativo de un toque. Con tres toques,
// una persona puede tener A1 del legado mandado y A2 esperando cuando se prende
// el nativo (o al reves). La regla de C10: una persona nunca recibe dos caminos
// del mismo tipo a la vez, y el que ya empezo sigue (D-C10-1: el default que no
// pierde ventas en ATT1; es el de la guarda).
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera; el scope del legado att1-recuperacion toma el carrito Y el pago
// rechazado, como maribel-recuperacion en Maribel; la politica del legado es
// la de tres toques), el carrito capturado, los rechazos capturados de Maribel
// (tests/fixtures/hotmart_purchase_canceled_maribel_20261028.json). El carrito
// nativo es el de tres toques de ATT1 (A1 con cupon, A2, Z con cupon); el pago
// rechazado nativo, la cadena completa de C3 (B1 por motivo, 24 h, B2 o Z).
//
// Prueba:
//   1. legado en curso: A1 del legado aceptado y A2 esperando; llega otro
//      carrito de la persona con el nativo de tres toques en cohorte y con el
//      nativo para todos: el nativo no arranca (caso_del_legado_en_curso), el
//      legado hace lo de hoy (pilot_case_binding_conflict) y A2 del legado sale
//      (execute). Rojo: sin el bloque de la guarda, el nativo abre su caso;
//   2. nativo en curso: A1 nativo aceptado y native_touch_2 esperando; otro
//      carrito: el nativo contesta ya_en_curso (el bridge lo deja failed, sin
//      legado); pausado, el legado no abre caso (flujo_nativo_en_curso) y el
//      toque 2 nativo sale. Rojo: sin el bloque, el legado abre el segundo caso;
//   3. ATT1 por la ruta motor contra el nativo en cohorte: con A2 del motor
//      esperando el nativo no arranca y el motor contesta ya_en_curso; con el
//      toque 2 nativo esperando, fuera de la cohorte el nativo contesta
//      ya_en_curso (el motor no se llama) y pausado el motor no abre caso (legado_rechazo, pilot_scope_rejected:flujo_nativo_en_curso).
//      Rojo: sin el bloque de plan_cart_recovery, el motor abre el segundo;
//   4. el scope compartido (Maribel): un pago rechazado del legado con su caso
//      abierto frena el pago y el carrito nativos; el pago rechazado nativo con
//      su segundo toque esperando suma el rechazo nuevo (ya_en_curso) y,
//      pausado, frena el pago y el carrito del legado; y un
//      carrito en curso con un pago rechazado nuevo hace lo mismo con el legado
//      que con el nativo (los dos tipos siguen: no es un doble del mismo tipo);
//   5. la paridad de la politica: la definicion del carrito de ATT1 (la del
//      21/10, la de la ruta motor) compila por el motor v1 y por el nativo a la
//      misma fila salvo las diferencias con nombre (las fija
//      tests/test_e483_native_legacy_double_case_touches.py contra la captura).
//
// Con --emit escribe tests/fixtures/flow_engine/native_legacy_double_case_touches_rows.json (lo que
// contesta la base, y el contexto del toque 2 del carrito y del de la rama del banco del pago rechazado,
// como los lee el bridge, para el despacho entero de tests/test_e483_native_dispatch_due.py); con --check
// (el de npm test) lo compara y falla si difiere.
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, errorOf, fail, one, root } from './validate_flow_engine_motor_v1.mjs';

const FIXTURE = join(root, 'tests/fixtures/flow_engine/native_legacy_double_case_touches_rows.json');
const EMIT = process.argv.includes('--emit');
const CHECK = process.argv.includes('--check');
const fixtureFile = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));
const REFUSALS = fixtureFile('hotmart_purchase_canceled_maribel_20261028.json').eventos;
const results = {};
const emitted = {};

const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, stateItem, pauseFlow, reserve, startNew, startOld, accept,
  cartFor, planCart, PLAN_CART, A1, A2, Z, claim, reevaluate, evidence, dbNow } = world;
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
// El rojo: la definicion vigente sin el bloque de la guarda, dentro de una transaccion que se deshace.
const definitionOf = async (signature) => one(await sql('select pg_get_functiondef($1::regprocedure) as d', [signature]), signature).d;
const withoutBlock = async (signature, marker, run) => rolledBack(async () => {
  const definition = await definitionOf(signature);
  const begin = definition.indexOf(`    -- ${marker}: begin`);
  const endText = `    -- ${marker}: end\n`;
  const end = definition.indexOf(endText);
  if (begin < 0 || end < begin) fail(`${signature}: sin el bloque ${marker}`);
  await db.exec(definition.slice(0, begin) + definition.slice(end + endText.length));
  return run();
});
const ADMIT_NATIVE = 'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)';
const PLAN_BASE = 'public.plan_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz)';
const LEGACY_GUARD = 'flow_engine_native_legacy_double_case';

// El flujo nativo: registrar, publicar y abrir en cohorte.
const openNative = async (definition) => {
  const flowId = await register(A.funnel, definition.flujo);
  await cycle([versionItem(flowId, A.funnel, definition.flujo, definition)], `v1 de ${definition.flujo}`);
  const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'propuesta').proposal_id;
  const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [flowId, proposalId, A.timezone]), 'abrir').r);
  if (!opened.ok || opened.state !== 'cohorte') fail(`abrir ${definition.flujo}`, opened);
  return flowId;
};
// El carrito nativo de tres toques de ATT1 (A1 con cupon, A2, Z con cupon), con esperas de minutos.
const CART = {
  esquema: 'setter-flujo/v1', flujo: 'carrito_nativo', producto: A.funnel, estado: 'borrador', nombre: 'Carrito nativo',
  dispara: { evento: 'carrito' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [A1, { esperar: '1m' }, A2, { esperar: '2m' }, Z],
  termina: { si: ['compra', 'contesta'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-carrito-nativo', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
// El pago rechazado nativo completo (C3): B1 por motivo, 24 h, B2 o Z.
const COUPON = { link: 'checkout', cupon: 'PROTOCOLO10' };
const b1 = (branch, coupon) => ({ plantilla: `maribel_pago_rechazado_${branch}_01`, idioma: 'es_MX',
  variables: ['nombre', 'producto'], boton: coupon ? { ...COUPON } : { link: 'checkout' } });
const follow = (template) => ({ plantilla: template, idioma: 'es_MX', variables: ['nombre', 'producto'] });
// El seguimiento de cada rama con una plantilla que entra en un toque siguiente nativo (nombre y producto, sin
// boton): B2 y Z de ATT1 tal como estan aprobadas no entran todavia (test_e483_native_condition_after_send.py).
// Sin cupon, la de Maribel; con cupon, A2 de ATT1: dos plantillas distintas para ver que sale la de la rama.
const payPath = (branch, coupon) => [b1(branch, coupon), { esperar: '24h' },
  follow(coupon ? 'att1_carrito_dudas_01' : 'maribel_retomar_proceso_01')];
const PAY = {
  esquema: 'setter-flujo/v1', flujo: 'pago_rechazado_nativo', producto: A.funnel, estado: 'borrador',
  nombre: 'Pago rechazado nativo', dispara: { evento: 'pago_fallido' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ esperar: '15m' }, {
    condicion: { dato: 'descuento_previo', es: ['si'] }, entonces: payPath('cupon', true),
    si_no: [{ condicion: { dato: 'rechazos', es: ['varios'] }, entonces: payPath('dos_veces', false),
      si_no: [{ condicion: { dato: 'tipo_de_rechazo', es: ['sin_saldo'] }, entonces: payPath('saldo', true),
        si_no: payPath('banco', false) }] }],
  }],
  termina: { si: ['compra', 'contesta'], vence: '3d' }, horario: 'siempre',
  topes: { bolsa: 'att1-pago-nativo', por_dia: 20, total: 1000, repeticion: { una_por: 'persona', otra_despues_de: '72h' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
const cartFlow = await openNative(CART);
const payFlow = await openNative(PAY);
const cohort = async (flowId, contactId) => sql(
  `insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')
   on conflict do nothing`, [flowId, contactId]);
const uncohort = async (flowId, contactId) => sql('delete from public.flow_cohort_members where flow_id = $1 and contact_id = $2',
  [flowId, contactId]);

const admitEvent = async (flowId, event) => one((await asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)', [JSON.stringify(INSTANCE), JSON.stringify(event)])).rows))
  .filter((row) => row.flow_id === flowId), 'admision nativa');
const admitCart = async (lead) => admitEvent(cartFlow, {
  fuente: 'hotmart', tipo: 'carrito', evento_id: lead.event, producto_ref: A.funnel, oferta: lead.offer.offer_code,
  contacto_id: lead.contact, momento: lead.cartArgs[6], chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
  destino: lead.phone,
});
// El contexto de un paso como lo lee el bridge (flow_engine_execution_context_v1), con el flow_id tapado.
const contextOf = async (actionId) => ({ ...one((await asService(async () => (await db.query(
  'select * from public.flow_engine_execution_context_v1($1)', [actionId])).rows)), 'contexto'), flow_id: '<flow_id>' });
const casesOf = async (lead) => sql(`select id, status, policy_key, context ->> 'flow_engine_trigger' as trigger
  from public.recovery_cases where contact_id = $1 order by created_at`, [lead.contact]);
const pendingOf = async (caseId, stepKey) => one(await sql(`select * from public.scheduled_actions
  where recovery_case_id = $1 and step_key = $2 and status = 'pending'`, [caseId, stepKey]), `${stepKey} pendiente`);
const planError = async (lead) => {
  const error = await errorOf(db, () => db.query(PLAN_CART, lead.cartArgs));
  return error === null ? null : `${error.message}${error.detail ? `:${error.detail}` : ''}`;
};
// La reevaluacion de un toque siguiente con la evidencia de Chatwoot sin respuesta: lo que decide la base.
const decide = async (lead, actionId) => {
  await sql("update public.scheduled_actions set due_at = now() - interval '1 second' where id = $1", [actionId]);
  const fence = await claim(lead.label, actionId);
  const decision = await reevaluate(actionId, fence, evidence(lead, fence.now));
  return `${decision.decision}:${decision.reason_code}`;
};
// A1 mandado y aceptado: la imagen del legado (mark_lancemos_pilot_request_started) o la del motor.
const sendFirst = async (lead, actionId, image) => {
  const reserved = await reserve(lead, actionId);
  const started = image === 'legado' ? await startOld(reserved) : await startNew(reserved);
  if (image !== 'legado' && started.outcome !== 'request_started') fail(`${lead.label}: el arranque de A1`, started);
  await accept(lead, reserved);
};

// ---------------------------------------------------------------------------
// 1. El legado en curso (A1 mandado, A2 esperando) y se prende el nativo de tres toques.
// ---------------------------------------------------------------------------
{
  results.legado_en_curso = {};
  for (const mode of ['cohorte', 'para_todos']) {
    if (mode === 'para_todos') await cycle([stateItem(cartFlow, A.funnel, CART.flujo, 'prender')], 'prender el carrito nativo');
    const ana = await cartFor(`c10-legado-${mode}`);
    if (mode === 'cohorte') await cohort(cartFlow, ana.contact);
    const plan = await planCart(ana);
    await sendFirst(ana, plan.scheduled_action_id, 'legado');
    const a2 = await pendingOf(plan.recovery_case_id, 'cart_second_touch');
    const status = one(await sql('select status from public.recovery_cases where id = $1', [plan.recovery_case_id]), 'caso').status;
    if (status !== 'active') fail('con A2 esperando el caso del legado esta active', status);
    await cartFor(ana);
    const native = await admitCart(ana);
    if (native.outcome !== 'motor_no_arranca' || native.reason !== 'caso_del_legado_en_curso') {
      fail(`${mode}: el nativo no arranca sobre el legado con A2 esperando`, native);
    }
    const legacy = await planError(ana);
    if (legacy !== 'pilot_case_binding_conflict') fail(`${mode}: el legado hace lo de hoy`, legacy);
    const red = await withoutBlock(ADMIT_NATIVE, LEGACY_GUARD, async () => (await admitCart(ana)).outcome);
    if (red !== 'motor_arranco') fail(`${mode}: sin la guarda, el nativo abria el segundo camino`, red);
    const a2Decision = await decide(ana, a2.id);
    if (!a2Decision.startsWith('execute')) fail(`${mode}: A2 del legado sale`, a2Decision);
    const cases = await casesOf(ana);
    if (cases.length !== 1 || cases[0].trigger !== null) fail(`${mode}: un solo caso, el del legado`, cases);
    results.legado_en_curso[mode] = { nativo: native.reason, legado: legacy, a2_del_legado: a2Decision, sin_la_guarda: red };
    if (mode === 'cohorte') emitted.legado_en_curso = { status, nativo: native.reason, legado: legacy, a2_del_legado: a2Decision };
  }
}

// ---------------------------------------------------------------------------
// 2. El nativo en curso (A1 mandado, native_touch_2 esperando) y llega el carrito que el legado tomaria.
// ---------------------------------------------------------------------------
let nativeRunner = null;
{
  const bea = await cartFor('c10-nativo');
  await cohort(cartFlow, bea.contact);
  const start = await admitCart(bea);
  if (start.outcome !== 'motor_arranco') fail('el carrito nativo de tres toques arranca', start);
  await sendFirst(bea, start.scheduled_action_id, 'motor');
  const touch2 = await pendingOf(start.recovery_case_id, 'native_touch_2');
  const status = one(await sql('select status from public.recovery_cases where id = $1', [start.recovery_case_id]), 'caso').status;
  if (status !== 'active') fail('con el toque 2 esperando el caso nativo esta active', status);
  // El toque 2 del carrito (A2) como lo lee el bridge: para el despacho entero de tests/test_e483_native_dispatch_due.py.
  emitted.carrito_toque_2_context = await contextOf(touch2.id);
  if (emitted.carrito_toque_2_context.template_name !== A2.plantilla) fail('el contexto de A2', emitted.carrito_toque_2_context);
  await cartFor(bea);
  const again = await admitCart(bea);
  if (again.reason !== 'ya_en_curso') fail('el nativo en curso contesta ya_en_curso', again);
  await pauseFlow(cartFlow, A.funnel, CART.flujo);
  const paused = await admitCart(bea);
  if (paused.reason !== 'flujo_pausado') fail('pausado, el nativo no lo toma', paused);
  const legacy = await planError(bea);
  if (legacy !== 'pilot_scope_rejected:flujo_nativo_en_curso') fail('el legado ve el nativo con el toque 2 esperando', legacy);
  const red = await withoutBlock(PLAN_BASE, LEGACY_GUARD, async () => {
    const plan = one((await db.query(PLAN_CART, bea.cartArgs)).rows, 'plan sin la guarda');
    return { created: plan.created, cases: (await casesOf(bea)).length };
  });
  if (red.created !== true || red.cases !== 2) fail('sin la guarda, el legado abria el segundo camino', red);
  if ((await casesOf(bea)).length !== 1) fail('el legado no escribio nada');
  const touch2Decision = await decide(bea, touch2.id);
  if (!touch2Decision.startsWith('execute')) fail('el toque 2 nativo sale con el nativo pausado', touch2Decision);
  await cycle([stateItem(cartFlow, A.funnel, CART.flujo, 'prender')], 'prender el carrito nativo');
  results.nativo_en_curso = { otra_vez: again.reason, pausado: paused.reason, legado: legacy, toque_2: touch2Decision,
    sin_la_guarda: red.created };
  emitted.nativo_en_curso = { status, otra_vez: again.reason, legado: legacy, toque_2: touch2Decision };
  nativeRunner = { lead: bea, caseId: start.recovery_case_id };
}

// ---------------------------------------------------------------------------
// 3. ATT1 por la ruta motor (v1) contra el carrito nativo en cohorte.
// ---------------------------------------------------------------------------
{
  const { FLOWS, SCOPE, createRoute, route, admit } = world;
  const created = await createRoute(A.funnel, 'carrito', 'carrito', SCOPE);
  if (!created.ok) fail('la ruta del carrito', created);
  const toMotor = await route(A.funnel, 'carrito', FLOWS.carrito, 'motor');
  if (!toMotor.ok || toMotor.route.executor !== 'motor') fail('el carrito a motor', toMotor);
  // (a) El motor en curso, A2 esperando: el nativo no arranca y el motor no abre otro.
  const caro = await cartFor('c10-motor');
  await cohort(cartFlow, caro.contact);
  const first = await admit(caro);
  if (first.outcome !== 'motor_arranco') fail('A1 por el motor', first);
  await sendFirst(caro, first.scheduled_action_id, 'motor');
  const motorA2 = await pendingOf(first.recovery_case_id, 'cart_second_touch');
  await cartFor(caro);
  const native = await admitCart(caro);
  if (native.reason !== 'caso_del_legado_en_curso') fail('el nativo no arranca sobre el motor con A2 esperando', native);
  const motorAgain = await admit(caro);
  if (motorAgain.outcome !== 'motor_no_arranca' || motorAgain.reason !== 'ya_en_curso') fail('el motor, ya_en_curso', motorAgain);
  const motorA2Decision = await decide(caro, motorA2.id);
  if (!motorA2Decision.startsWith('execute')) fail('A2 del motor sale', motorA2Decision);
  // (b) El nativo en curso, toque 2 esperando, la persona sale de la cohorte: el carrito va al motor, que no abre caso.
  const dani = await cartFor('c10-nativo-y-motor');
  await cohort(cartFlow, dani.contact);
  const start = await admitCart(dani);
  if (start.outcome !== 'motor_arranco') fail('el nativo de dani arranca', start);
  await sendFirst(dani, start.scheduled_action_id, 'motor');
  const touch2 = await pendingOf(start.recovery_case_id, 'native_touch_2');
  // Fuera de la cohorte la repeticion se mira antes: ya_en_curso (el bridge deja el evento failed, sin el motor).
  await uncohort(cartFlow, dani.contact);
  await cartFor(dani);
  const outside = await admitCart(dani);
  if (outside.reason !== 'ya_en_curso') fail('fuera de la cohorte, con su recorrido en curso', outside);
  // Pausado, el nativo no lo toma y el bridge lo manda al motor.
  await pauseFlow(cartFlow, A.funnel, CART.flujo);
  const paused = await admitCart(dani);
  if (paused.reason !== 'flujo_pausado') fail('pausado', paused);
  const motor = await admit(dani);
  if (motor.outcome !== 'legado_rechazo' || motor.reason !== 'pilot_scope_rejected:flujo_nativo_en_curso' || motor.recovery_case_id) {
    fail('el motor no abre caso sobre el nativo en curso', motor);
  }
  const red = await withoutBlock(PLAN_BASE, LEGACY_GUARD, async () => {
    const row = await admit(dani);
    return { outcome: row.outcome, cases: (await casesOf(dani)).length };
  });
  if (red.cases !== 2) fail('sin la guarda, el motor abria el segundo camino', red);
  const touch2Decision = await decide(dani, touch2.id);
  if (!touch2Decision.startsWith('execute')) fail('el toque 2 nativo sale', touch2Decision);
  await cycle([stateItem(cartFlow, A.funnel, CART.flujo, 'prender')], 'prender el carrito nativo');
  results.ruta_motor = { nativo_sobre_motor: native.reason, fuera_de_cohorte: outside.reason, motor_otra_vez: motorAgain.reason, a2_del_motor: motorA2Decision,
    motor_sobre_nativo: motor.reason, toque_2: touch2Decision, sin_la_guarda: red.outcome };
  emitted.ruta_motor = { nativo_sobre_motor: native.reason, fuera_de_cohorte: outside.reason, motor_otra_vez: motorAgain.reason,
    motor_sobre_nativo: `${motor.outcome}:${motor.reason}` };
  // La ruta vuelve al legado para lo que sigue.
  const back = await route(A.funnel, 'carrito', FLOWS.carrito, 'legado');
  if (!back.ok) fail('la ruta de vuelta al legado', back);
}

// ---------------------------------------------------------------------------
// 4. El scope compartido: el carrito y el pago rechazado (maribel-recuperacion en Maribel).
// ---------------------------------------------------------------------------
{
  let serial = 0;
  const refusal = async (lead, index) => {
    serial += 1;
    const payload = structuredClone(REFUSALS[index].payload);
    const moment = new Date((await dbNow()).getTime() - 10 * 60_000);
    payload.id = `c10-rechazo-${lead.email}-${serial}`;
    payload.creation_date = moment.getTime();
    payload.data.buyer = { ...payload.data.buyer, name: lead.name, email: lead.email, checkout_phone: lead.phone };
    payload.data.product = { ...payload.data.product, id: A.productId, name: A.productName };
    payload.data.purchase.offer.code = lead.offer.offer_code;
    payload.data.purchase.transaction = `HPC10${String(serial).padStart(4, '0')}`;
    const admitted = one(await sql('select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)',
      [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone]), `${lead.label} rechazo`);
    if (admitted.outcome !== 'inserted') fail(`${lead.label} rechazo`, admitted);
    lead.refusal = { event: admitted.webhook_event_id, moment: moment.toISOString() };
    return lead;
  };
  const admitPay = async (lead) => admitEvent(payFlow, {
    fuente: 'hotmart', tipo: 'pago_fallido', evento_id: lead.refusal.event, producto_ref: A.funnel, oferta: lead.offer.offer_code,
    contacto_id: lead.contact, momento: lead.refusal.moment, chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
    destino: lead.phone,
  });
  const PLAN_PAY = 'select * from public.plan_portable_payment_failure_recovery($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)';
  const payArgs = (lead) => [lead.refusal.event, lead.contact, String(A.productId), A.productName, lead.offer.offer_code,
    world.THREE_TOUCHES.policy, 1, lead.refusal.moment, A.accountId, A.inboxId, lead.phone, A.scope, 1];
  const planPayError = async (lead) => {
    const error = await errorOf(db, () => db.query(PLAN_PAY, payArgs(lead)));
    return error === null ? null : `${error.message}${error.detail ? `:${error.detail}` : ''}`;
  };
  const shared = {};

  // (a) Un pago rechazado del legado con su caso abierto: el pago y el carrito nativos no arrancan.
  {
    const fer = await cartFor('c10-pago-legado');
    await cohort(payFlow, fer.contact);
    await cohort(cartFlow, fer.contact);
    await refusal(fer, 1);
    const plan = one((await db.query(PLAN_PAY, payArgs(fer))).rows, 'el plan del pago del legado');
    if (!plan.created) fail('el legado planifica el pago rechazado', plan);
    await refusal(fer, 2);
    const nativePay = await admitPay(fer);
    if (nativePay.reason !== 'caso_del_legado_en_curso') fail('el pago nativo no arranca sobre el legado', nativePay);
    await cartFor(fer);
    const nativeCart = await admitCart(fer);
    if (nativeCart.reason !== 'caso_del_legado_en_curso') fail('el carrito nativo no arranca sobre el pago del legado', nativeCart);
    shared.pago_legado_en_curso = { pago_nativo: nativePay.reason, carrito_nativo: nativeCart.reason };
  }
  // (b) El pago rechazado nativo con su segundo toque esperando.
  {
    const eva = await cartFor('c10-pago-nativo');
    await cohort(payFlow, eva.contact);
    await cohort(cartFlow, eva.contact);
    await refusal(eva, 1);
    const start = await admitPay(eva);
    if (start.outcome !== 'motor_arranco') fail('el pago rechazado nativo arranca', start);
    const reserved = await reserve(eva, start.scheduled_action_id);
    const data = one(await asService(async () => (await db.query(
      'select * from public.get_flow_engine_native_payment_failure_context_v1($1,$2,$3,$4)',
      [reserved.actionId, reserved.worker, reserved.lease, reserved.now])).rows), 'datos');
    const branch = data.descuento_previo === 'si' ? 0 : data.rechazos === 'varios' ? 1 : data.tipo_de_rechazo === 'sin_saldo' ? 2 : 3;
    const recorded = one(await asService(async () => (await db.query(
      'select * from public.record_flow_engine_native_branch_v1($1,$2,$3,$4,$5,$6)',
      [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, branch, reserved.now])).rows), 'registro');
    if (recorded.outcome !== 'recorded') fail('la rama de B1', recorded);
    if ((await startNew(reserved)).outcome !== 'request_started') fail('el arranque de B1 nativo');
    await accept(eva, reserved);
    const payTouch2 = await pendingOf(start.recovery_case_id, 'native_touch_2');
    // El toque 2 de la rama elegida (banco: B1 sin cupon y su seguimiento), para el despacho entero.
    emitted.pago_toque_2_context = { rama: branch, ...(await contextOf(payTouch2.id)) };
    if (branch !== 3 || emitted.pago_toque_2_context.template_name !== 'maribel_retomar_proceso_01') {
      fail('el toque 2 de la rama del banco', emitted.pago_toque_2_context);
    }
    // Otro rechazo de la persona: el nativo lo suma (ya_en_curso; el bridge deja el evento processed, sin el legado).
    await refusal(eva, 2);
    const payAgain = await admitPay(eva);
    if (payAgain.reason !== 'ya_en_curso') fail('el pago nativo en curso suma el rechazo', payAgain);
    // Pausado (la vuelta atras), el rechazo siguiente va al legado del pago, que no abre otro caso.
    await pauseFlow(payFlow, A.funnel, PAY.flujo);
    await refusal(eva, 1);
    const payPaused = await admitPay(eva);
    if (payPaused.reason !== 'flujo_pausado') fail('el pago nativo pausado', payPaused);
    const legacyPay = await planPayError(eva);
    if (legacyPay !== 'pilot_scope_rejected:flujo_nativo_en_curso') fail('el legado del pago ve el nativo en curso', legacyPay);
    // Un carrito de la persona: el legado del carrito tampoco abre (la guarda mira el pago nativo, 20261030000200).
    await cartFor(eva);
    const legacyCart = await planError(eva);
    if (legacyCart !== 'pilot_scope_rejected:flujo_nativo_en_curso') fail('el legado del carrito ve el pago nativo', legacyCart);
    shared.pago_nativo_en_curso = { otro_rechazo: payAgain.reason, pausado: payPaused.reason, legado_del_pago: legacyPay, legado_del_carrito: legacyCart };
  }
  // (c) Un carrito en curso (A2 esperando) y llega un pago rechazado: lo mismo con el legado que con el nativo.
  {
    const legacyLead = await cartFor('c10-carrito-legado-y-pago');
    const plan = await planCart(legacyLead);
    await sendFirst(legacyLead, plan.scheduled_action_id, 'legado');
    const a2 = await pendingOf(plan.recovery_case_id, 'cart_second_touch');
    await refusal(legacyLead, 1);
    const legacyPay = one((await db.query(PLAN_PAY, payArgs(legacyLead))).rows, 'pago con el carrito del legado');
    const legacyA2 = await decide(legacyLead, a2.id);
    // El carrito nativo de la seccion 2, con su recorrido abierto.
    const { lead: nativeLead, caseId } = nativeRunner;
    await refusal(nativeLead, 1);
    const nativePay = one((await db.query(PLAN_PAY, payArgs(nativeLead))).rows, 'pago con el carrito nativo');
    const nativeCase = one(await sql('select status from public.recovery_cases where id = $1', [caseId]), 'caso nativo').status;
    if (legacyPay.created !== true || nativePay.created !== true || !legacyA2.startsWith('execute') || nativeCase !== 'active') {
      fail('el carrito sigue y el pago abre su caso, igual con el legado y con el nativo',
        { legacyPay, nativePay, legacyA2, nativeCase });
    }
    shared.carrito_en_curso_y_pago = { legado: { pago: legacyPay.created, a2: legacyA2 },
      nativo: { pago: nativePay.created, caso_del_carrito: nativeCase } };
  }
  results.scope_compartido = shared;
  emitted.scope_compartido = {
    pago_nativo_en_curso: shared.pago_nativo_en_curso, pago_legado_en_curso: shared.pago_legado_en_curso,
    carrito_en_curso_y_pago: { legado_pago: shared.carrito_en_curso_y_pago.legado.pago,
      nativo_pago: shared.carrito_en_curso_y_pago.nativo.pago },
  };
}

// ---------------------------------------------------------------------------
// 5. La paridad de la politica: el carrito de ATT1 por el motor v1 y por el nativo.
// ---------------------------------------------------------------------------
{
  // La definicion del carrito de ATT1 del 21/10 (la que corre la ruta motor; la fija
  // test_flow_policy.py contra att1-carrito-tres-toques), con lo que el nativo exige:
  // contesta para varios toques, una bolsa que no es el scope del legado y otra clave.
  const ATT1_CART = {
    flujo: 'carrito', nombre: 'Carrito abandonado', dispara: { evento: 'carrito' }, entra: { permiso: 'formulario' },
    pasos: [A1, { esperar: '24h' }, A2, { esperar: '48h' }, Z],
    termina: { si: ['compra'], vence: '5d' }, horario: { zona: 'America/Mexico_City', desde: '09:00', hasta: '21:00' },
    topes: { bolsa: 'att1-recuperacion', por_dia: 240, total: 5000,
      repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
    prioridad: { le_gana_a: ['precheckout'], no_arranca_mientras: ['pago_fallido'] },
  };
  const NATIVE_CART = { ...ATT1_CART, flujo: 'carrito_nativo', termina: { si: ['compra', 'contesta'], vence: '5d' },
    topes: { ...ATT1_CART.topes, bolsa: 'att1-carrito-nativo' } };
  const FLOW_ID = '0b3c5f00-0e30-4000-8000-0000000c1000';
  const compile = async (definition, kind) => {
    const row = one(await sql(`select (c).timezone, (c).business_windows, (c).grace_period::text as grace_period,
        (c).expires_after::text as expires_after, (c).max_automatic_messages, (c).steps, (c).purpose
      from (select public._flow_engine_compile_policy_core($1::jsonb, $2, 1, $3, null) as c) x`,
    [JSON.stringify(definition), FLOW_ID, kind]), `compilar ${kind}`);
    return row;
  };
  const motor = await compile(ATT1_CART, 'carrito');
  const nativo = await compile(NATIVE_CART, 'nativo:carrito');
  const nativeRefusal = one(await sql('select public._flow_engine_native_unsupported($1::jsonb) as r',
    [JSON.stringify({ ...NATIVE_CART, esquema: 'setter-flujo/v1', producto: A.funnel, estado: 'borrador' })]), 'forma').r;
  if (nativeRefusal !== null) fail('el nativo ejecuta el carrito de ATT1', nativeRefusal);
  // Las esperas, el horario, el plazo y el cupon de Z, iguales; los nombres de los pasos y el cupon de A1, con nombre.
  const sameButSteps = ['timezone', 'business_windows', 'grace_period', 'expires_after', 'max_automatic_messages', 'purpose']
    .filter((key) => JSON.stringify(motor[key]) !== JSON.stringify(nativo[key]));
  if (sameButSteps.length !== 0) fail('la politica difiere fuera de los pasos', sameButSteps);
  const delays = (row) => row.steps.map((step) => step.delay ?? null);
  if (JSON.stringify(delays(motor)) !== JSON.stringify(delays(nativo))) fail('las esperas', { motor: motor.steps, nativo: nativo.steps });
  results.paridad = { motor: motor.steps.map((s) => s.step_key), nativo: nativo.steps.map((s) => s.step_key) };
  emitted.paridad = { definicion_motor: ATT1_CART, definicion_nativa: NATIVE_CART, motor, nativo };
}

// ---------------------------------------------------------------------------
// El fixture para la prueba de Python.
// ---------------------------------------------------------------------------
const stored = {
  _capture: {
    origen: 'validate_e483_native_legacy_double_case_touches.mjs: la cadena entera de migraciones (PGlite), el mundo '
      + 'de ATT1 con el carrito nativo de tres toques y el pago rechazado nativo completo',
    encargo: 'E-483, C10',
    regenerar: 'cd tests/sql/followup_engine && node validate_e483_native_legacy_double_case_touches.mjs --emit',
  },
  ...emitted,
};
// jsonb ordena las claves: se compara con las claves ordenadas.
const canonical = (value) => JSON.stringify(value, (key, item) => (item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.keys(item).sort().map((k) => [k, item[k]])) : item));
if (EMIT) {
  writeFileSync(FIXTURE, `${JSON.stringify(JSON.parse(canonical(stored)), null, 2)}\n`);
} else if (CHECK) {
  const onDisk = JSON.parse(readFileSync(FIXTURE, 'utf8'));
  if (canonical(onDisk) !== canonical(stored)) {
    fail('native_legacy_double_case_touches_rows.json no es lo que da la cadena: correr con --emit', stored);
  }
}
await db.close();
console.log(`validate_e483_native_legacy_double_case_touches: ok ${JSON.stringify(results)}`);
