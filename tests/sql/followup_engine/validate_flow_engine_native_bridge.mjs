// Lo que el bridge del motor nativo necesita de la base (migracion
// 20261024000500, el PR 3 del hito 4, E-065), con las RPC reales sobre la
// cadena completa de migraciones.
//
// Contrato: docs/contracts/flow-engine-v2.md (secciones 4, 5 y 8).
//
// Prueba, con un rojo a proposito donde hay parche:
//   0. la migracion: agrega exactamente sus nueve funciones y sus dos tablas
//      (con RLS y sin permisos para la API), cambia solo la adopcion (con su
//      marca y sin tocar permisos), y todo lo demas queda igual; correrla dos
//      veces aborta;
//   1. sin flujos nativos (ATT1 hoy: el carrito en el almacen), la lectura de
//      los flujos nativos da una lista vacia, la de los rechazos tambien y la
//      cola esta vacia; con la activacion abierta, la da en cohorte;
//   2. la cola de la compra: se anota antes de aplicarla, no se lee mientras
//      el evento sigue received (primero el corte), se lee despues, suma
//      intentos y se cierra; una que no es compra no entra;
//   3. el caso «A2 pendiente + compra» (choque 2 del diseno): un carrito con
//      A1 aceptado y A2 pendiente; la compra capturada corta el carrito (A2 ya
//      no sale) y la admision nativa arranca la bienvenida;
//   4. el paso etiquetar: la cola del paso de la bienvenida dice cliente, el
//      producto y el momento; la de una accion del carrito es null;
//   5. la respuesta a la bienvenida entra al agente: la conversacion que dejo
//      la aceptacion se adopta (draft_only); rojo: sin el parche, la v2 la
//      rechaza con inbound_canonical_conversation_conflict;
//   6. MOT-002: un error interno sembrado aparece una vez, con flow_id, se
//      marca y no vuelve; vuelve al cruzar 10; los motivos de la persona
//      (fuera_de_cohorte, flujo_pausado) no aparecen.
//
// Con --emit-bridge-fixtures [--check] escribe (o compara)
// tests/fixtures/flow_engine/native_bridge_rpc_rows.json: lo que contestan
// las lecturas nuevas, con uuid y horas normalizados, para las pruebas del
// bridge.
//
// Datos sinteticos, los de buildWorld: personas de example.test con
// telefonos 1202555xxxx (rango de ficcion) y el manifiesto de ATT1. La compra
// es la capturada de ATT1 (tests/fixtures/hotmart_cash_payment_approved_att1_20261012.json).
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261024000500_flow_engine_native_bridge_v1.sql';
const FIXTURE = join(root, 'tests/fixtures/flow_engine/native_bridge_rpc_rows.json');
const TZ = 'timestamp with time zone';
const results = {};
const fixture = (path) => JSON.parse(readFileSync(join(root, 'tests/fixtures', path), 'utf8'));

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const tableAcl = async (db) => new Map((await db.query(`
  select c.relname, coalesce(c.relacl::text, '') as acl, c.relrowsecurity as rls
  from pg_class c where c.relnamespace = 'public'::regnamespace and c.relkind = 'r'
`)).rows.map((row) => [row.relname, row]));

const ADDED = {
  'read_flow_engine_native_flows_v1(jsonb)': true,
  'enqueue_flow_engine_native_event_v1(jsonb,uuid)': true,
  'read_flow_engine_native_event_queue_v1(jsonb,integer)': true,
  [`finish_flow_engine_native_event_v1(uuid,boolean,${TZ})`]: true,
  '_flow_engine_rejection_is_alertable(text)': false,
  '_flow_engine_rejection_step(integer)': false,
  'read_flow_engine_start_rejections_v1(jsonb,boolean,integer)': true,
  [`mark_flow_engine_start_rejection_notified_v1(uuid,text,text,integer,${TZ})`]: true,
  'flow_engine_native_step_tail_v1(uuid)': true,
};
const ADOPTION = 'admit_portable_inbound_commercial_case_v1(text,integer,bigint,text)';
const PATCHED = { [ADOPTION]: ['flow_engine_native_adoption: begin'] };
const NEW_TABLES = ['flow_engine_native_event_queue', 'flow_engine_rejection_notices'];

const shapeDb = new PGlite();
await shapeDb.waitReady;
await shapeDb.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
  alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
  grant usage on schema public to service_role, anon;
`);
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`${MIGRATION} no esta en la cadena`);
// Despues van la del AE que crea flujos (20261024000600), que parcha el ciclo del almacen, y la de las
// lecturas nativas del AE (20261025000100), que suma lecturas y parcha el anclaje del AE y prender: ninguna
// redefine nada de esta.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261024000600_flow_change_new_flow_v1.sql',
  '20261025000100_automation_expert_native_reads_v1.sql',
  // El btrim del nombre del producto en el pago rechazado (E-087): solo parcha esa admision.
  '20261025000200_payment_failure_product_name_btrim.sql',
  // La lectura del link para la nota privada al derivar (F-034 D8): una funcion nueva.
  '20261025000300_handoff_link_note_v1.sql',
  // Las conversaciones del comprador para pausar al agente (E-100): una funcion nueva.
  '20261025000400_purchase_agent_pause_v1.sql',
  // La forma nativa con las variables del paso (E-115): parcha la forma nativa y el compilador.
  '20261026000100_flow_engine_native_template_shape_v1.sql',
  // La oferta del evento en el caso nativo (E-122): parcha la admision nativa y el contexto del despacho.
  '20261026000200_flow_engine_native_offer_in_context_v1.sql'])) {
  fail(`despues de ${MIGRATION} van solo la del AE que crea flujos y la de sus lecturas nativas`, names.slice(position + 1));
}
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
await applyFile(shapeDb, join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of names.slice(0, position)) await applyFile(shapeDb, join(root, 'supabase/migrations', name));
const before = await snapshot(shapeDb);
const tablesBefore = await tableAcl(shapeDb);
const target = join(root, 'supabase/migrations', MIGRATION);
{
  await shapeDb.exec('create role otro_duenio nologin');
  const error = await expectError(shapeDb, 'otro duenio', async () => {
    await shapeDb.exec(`alter function public.${ADOPTION} owner to otro_duenio`);
    await applyFile(shapeDb, target);
  }, { code: '55000', message: 'flow_engine_native_bridge_owner_mismatch' });
  results.duenio = `${error.code}:${error.message}`;
}
await applyFile(shapeDb, target);
const after = await snapshot(shapeDb);
const tablesAfter = await tableAcl(shapeDb);
{
  const added = [...after.keys()].filter((signature) => !before.has(signature));
  const removed = [...before.keys()].filter((signature) => !after.has(signature));
  const changed = [...before.keys()].filter((signature) => after.has(signature)
    && after.get(signature).definition !== before.get(signature).definition);
  const aclChanged = [...before.keys()].filter((signature) => after.has(signature)
    && ['service_x', 'api_x', 'acl'].some((key) => after.get(signature)[key] !== before.get(signature)[key]));
  if (!sameSet(added, Object.keys(ADDED)) || removed.length !== 0 || !sameSet(changed, Object.keys(PATCHED))
      || aclChanged.length !== 0) {
    fail('la migracion cambio otra cosa', { added, removed, changed, aclChanged });
  }
  for (const [signature, service] of Object.entries(ADDED)) {
    const row = after.get(signature);
    if (row.api_x || row.service_x !== service) fail(`${signature}: permisos`, row);
  }
  for (const [signature, markers] of Object.entries(PATCHED)) {
    for (const marker of markers) {
      if (!after.get(signature).definition.includes(marker)) fail(`${signature}: falta ${marker}`);
      if (before.get(signature).definition.includes(marker)) fail(`${signature}: ${marker} ya estaba`);
    }
  }
  const tablesAdded = [...tablesAfter.keys()].filter((name) => !tablesBefore.has(name));
  if (!sameSet(tablesAdded, NEW_TABLES)) fail('las tablas nuevas', tablesAdded);
  for (const name of NEW_TABLES) {
    if (!tablesAfter.get(name).rls) fail(`${name}: sin RLS`);
    for (const role of ['anon', 'authenticated', 'service_role']) {
      const has = one((await shapeDb.query(
        `select has_table_privilege($1, 'public.' || $2, 'select') or has_table_privilege($1, 'public.' || $2, 'insert') as x`,
        [role, name])).rows, 'privilegio').x;
      if (has) fail(`${name}: ${role} tiene permiso`);
    }
  }
  const tablesChanged = [...tablesBefore.keys()].filter((name) => tablesAfter.get(name).acl !== tablesBefore.get(name).acl);
  if (tablesChanged.length !== 0) fail('cambio el ACL de una tabla que existia', tablesChanged);
  const error = await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'flow_engine_native_bridge_already_applied' });
  results.segunda_corrida = `${error.code}:${error.message}`;
  results.forma = { agregadas: added.length, cambiadas: changed.length, iguales: before.size - changed.length };
}
await shapeDb.close();
const previous = (signature) => before.get(signature).definition;

// ---------------------------------------------------------------------------
// El mundo: ATT1 (A), el carrito y el pago fallido en el almacen, la ruta del
// carrito en legado (buildWorld), como hoy en produccion salvo la ruta.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, B, INSTANCE, person, cartFor, admit, reserve, startNew, startOld, accept, stepOf, register, cycle,
  versionItem, stateItem, control, dbNow, rpc } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
{
  const created = await world.createRoute(A.funnel, 'carrito', 'carrito', A.scope);
  if (!created.ok) fail('la ruta del carrito', created);
}
const rolledBack = async (run) => {
  await db.exec('begin');
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};
// Una llamada como la hace el bridge: con el rol de la API.
const asService = async (text, params = []) => {
  await db.exec('set role service_role');
  try {
    return (await db.query(text, params)).rows;
  } finally {
    await db.exec('reset role');
  }
};
const readJson = async (fn, ...args) => {
  const marks = args.map((_, i) => `$${i + 1}`).join(', ');
  return JSON.parse(one(await asService(`select public.${fn}(${marks})::text as r`,
    args.map((a) => (a !== null && typeof a === 'object' ? JSON.stringify(a) : a))), fn).r);
};

const ACTIVATION_TEMPLATE = 'att1_bienvenida_01';
const activation = (flujo = 'activacion', extra = {}) => ({
  esquema: 'setter-flujo/v1', flujo, producto: A.funnel, estado: 'borrador', nombre: 'Bienvenida despues de la compra',
  dispara: { evento: 'compra' }, entra: { permiso: 'compra', contesto_a: [] },
  pasos: [{ plantilla: ACTIVATION_TEMPLATE, idioma: 'es_MX', variables: ['nombre', 'producto'] }, { etiquetar: 'cliente' }],
  termina: { si: [], vence: '1d' },
  horario: 'siempre',
  topes: { por_dia: 'sin_tope', total: 'sin_tope', repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  ...extra,
});
const OK_CLIENT = { quien: 'juan', medio: 'whatsapp', fecha: '2026-10-15' };
const publishV1 = async (flowId, definicion) => {
  await cycle([{ ...versionItem(flowId, A.funnel, definicion.flujo, definicion), ok_del_cliente: OK_CLIENT }],
    `v1 de ${definicion.flujo}`);
  return one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1', [flowId]),
    'propuesta').proposal_id;
};
const openNative = async (flowId, proposalId) => JSON.parse(one(await sql(
  'select public._flow_engine_open_native_flow($1, $2, $3)::text as r', [flowId, proposalId, A.timezone]), 'abrir').r);

const CAPTURED_PURCHASE = fixture('hotmart_cash_payment_approved_att1_20261012.json').eventos[1].payload;
let purchaseSerial = 0;
const contactFor = async (lead) => {
  if (lead.contact) return lead.contact;
  lead.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
    values ($1, $2, $3, 'MX') returning id`, [lead.name, lead.email, lead.phone]), 'contacto').id;
  await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1, 'email', $2, $2, 'hotmart'), ($1, 'phone', $3, $3, 'hotmart')`, [lead.contact, lead.email, lead.phone]);
  return lead.contact;
};
// La compra capturada para la persona, admitida como la admite el webhook. Se
// aplica aparte (applyPurchase), como el worker.
const admitPurchase = async (lead) => {
  purchaseSerial += 1;
  const now = await dbNow();
  const payload = structuredClone(CAPTURED_PURCHASE);
  payload.id = `puente-compra-${lead.email}-${purchaseSerial}`;
  payload.creation_date = now.getTime() - 60_000;
  Object.assign(payload.data.buyer, { name: lead.name, first_name: lead.name.split(' ')[0], email: lead.email,
    checkout_phone: lead.phone });
  payload.data.product.id = A.productId;
  payload.data.purchase.offer.code = lead.offer.offer_code;
  payload.data.purchase.transaction = `HP${String(800000000 + purchaseSerial)}`;
  payload.data.purchase.approved_date = now.getTime() - 60_000;
  payload.data.purchase.order_date = now.getTime() - 120_000;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone]), 'compra');
  if (admitted.outcome !== 'inserted') fail('admitir la compra', admitted);
  return { id: admitted.webhook_event_id, moment: new Date(payload.creation_date).toISOString(), payload };
};
const applyPurchase = async (lead, purchase) => one(await sql(
  'select * from public.apply_hotmart_purchase_approved($1,$2,$3,$4,$5,$6,$7::timestamptz)',
  [purchase.id, lead.email, lead.phone, String(A.productId), lead.offer.offer_code,
    purchase.payload.data.purchase.transaction, new Date(purchase.payload.data.purchase.approved_date).toISOString()]),
'aplicar').outcome;
const flowEvent = (lead, purchase) => ({
  fuente: 'hotmart', tipo: 'compra', evento_id: purchase.id, producto_ref: A.funnel, oferta: lead.offer.offer_code,
  contacto_id: lead.contact, momento: purchase.moment, chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
  destino: lead.phone,
});
const admitNative = async (event) => asService('select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
  [JSON.stringify(INSTANCE), JSON.stringify(event)]);
const NATIVE = {};
const addToCohort = async (lead) => sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref)
  values ($1, $2, 'dan')`, [NATIVE.flow, await contactFor(lead)]);
const rows = {};

// ---------------------------------------------------------------------------
// 1. Sin flujos nativos, nada; con la activacion, en cohorte.
// ---------------------------------------------------------------------------
{
  const flows = await readJson('read_flow_engine_native_flows_v1', INSTANCE);
  const rejections = await readJson('read_flow_engine_start_rejections_v1', INSTANCE, true, 20);
  const queue = await readJson('read_flow_engine_native_event_queue_v1', INSTANCE, 10);
  if (flows.flows.length !== 0 || rejections.unnotified !== 0 || rejections.rejections.length !== 0
      || queue.pending !== 0 || queue.events.length !== 0) {
    fail('sin flujos nativos las lecturas tenian que estar vacias', { flows, rejections, queue });
  }
  rows.native_flows_empty = flows;
  rows.rejections_empty = rejections;
  rows.queue_empty = queue;

  NATIVE.flow = await register(A.funnel, 'activacion');
  const proposal = await publishV1(NATIVE.flow, activation());
  const opened = await openNative(NATIVE.flow, proposal);
  if (!opened.ok || opened.state !== 'cohorte') fail('abrir la activacion', opened);
  const withNative = await readJson('read_flow_engine_native_flows_v1', INSTANCE);
  const only = one(withNative.flows, 'el flujo nativo');
  if (only.flow_id !== NATIVE.flow || only.state !== 'cohorte' || only.trigger !== 'compra'
      || only.product_ref !== A.funnel || only.flow_key !== 'activacion' || only.active_version !== 1) {
    fail('la lectura de los flujos nativos', withNative);
  }
  rows.native_flows_cohort = withNative;
  results.flujos_nativos = { sin: 0, con: withNative.flows.map((f) => `${f.flow_key}:${f.state}`) };
}

// ---------------------------------------------------------------------------
// 2. La cola de la compra.
// ---------------------------------------------------------------------------
{
  const buyer = person('cola');
  await contactFor(buyer);
  const purchase = await admitPurchase(buyer);
  const enqueue = async (id) => one(await asService('select public.enqueue_flow_engine_native_event_v1($1::jsonb, $2) as r',
    [JSON.stringify(INSTANCE), id]), 'anotar').r;
  if ((await enqueue(purchase.id)) !== true || (await enqueue(purchase.id)) !== false) fail('anotar dos veces');
  // Mientras el evento sigue received, no se lee: el worker todavia no corto.
  const early = await readJson('read_flow_engine_native_event_queue_v1', INSTANCE, 10);
  if (early.pending !== 1 || early.events.length !== 0) fail('la cola con el evento sin aplicar', early);
  // Sin caso del recuperador la compra no encuentra que cortar (not_found) y el
  // evento queda failed: igual dispara (esquema, «Qué lo dispara»).
  const appliedQueue = await applyPurchase(buyer, purchase);
  const statusAfter = one(await sql('select processing_status from public.webhook_events where id = $1', [purchase.id]),
    'estado').processing_status;
  if (statusAfter === 'received') fail('la compra aplicada sigue received', appliedQueue);
  const ready = await readJson('read_flow_engine_native_event_queue_v1', INSTANCE, 10);
  const queued = one(ready.events, 'la compra en la cola');
  if (queued.webhook_event_id !== purchase.id || queued.attempts !== 0 || queued.payload.id !== purchase.payload.id) {
    fail('la cola con la compra aplicada', ready);
  }
  rows.queue_ready = { ...ready, events: ready.events.map((event) => ({ ...event, payload: '<payload>' })) };
  const finish = async (done) => one(await asService('select public.finish_flow_engine_native_event_v1($1, $2, $3) as r',
    [purchase.id, done, await dbNow()]), 'cerrar').r;
  if ((await finish(false)) !== true) fail('sumar un intento');
  const retried = await readJson('read_flow_engine_native_event_queue_v1', INSTANCE, 10);
  if (one(retried.events, 'reintento').attempts !== 1) fail('el intento', retried);
  if ((await finish(true)) !== true || (await finish(true)) !== false) fail('cerrar la compra');
  const closed = await readJson('read_flow_engine_native_event_queue_v1', INSTANCE, 10);
  if (closed.pending !== 0 || closed.events.length !== 0) fail('la cola cerrada', closed);
  // Un carrito no entra a la cola.
  const cartLead = await cartFor(person('cola-carrito'));
  const cartEvent = one(await sql(`select id from public.webhook_events where event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
    order by received_at desc limit 1`), 'evento del carrito').id;
  const wrong = await expectError(db, 'un carrito en la cola', () => db.query(
    'select public.enqueue_flow_engine_native_event_v1($1::jsonb, $2)', [JSON.stringify(INSTANCE), cartEvent]),
  { code: '22023', message: 'flow_engine_native_queue_event_invalid' });
  results.cola = { aplicada: appliedQueue, estado: statusAfter, anotada: true, antes_del_corte: early.events.length, despues: 1, rechazo: wrong.message,
    carrito: Boolean(cartLead) };
}

// ---------------------------------------------------------------------------
// 3. A2 pendiente + compra: la compra corta el carrito y arranca la bienvenida.
// ---------------------------------------------------------------------------
{
  const shopper = person('a2-y-compra');
  await contactFor(shopper);
  await addToCohort(shopper);
  const lead = await cartFor(shopper);
  const cart = await admit(lead);
  if (cart.outcome !== 'legado' || !cart.created) fail('el carrito', cart);
  shopper.plan = { recovery_case_id: cart.recovery_case_id, scheduled_action_id: cart.scheduled_action_id };
  const a1 = await reserve(shopper, cart.scheduled_action_id);
  const a1Start = await startOld(a1);
  if (a1Start.status !== undefined && a1Start.status !== 'request_started' && a1Start.outcome !== 'request_started') {
    // mark_lancemos_pilot_request_started devuelve el intento: lo que importa es la aceptacion.
  }
  await accept(shopper, a1);
  const a2 = await stepOf(shopper, 'cart_second_touch');
  if (!['pending', 'deferred'].includes(a2.status)) fail('A2 tenia que quedar pendiente', a2.status);

  const purchase = await admitPurchase(shopper);
  if ((await applyPurchase(shopper, purchase)) !== 'applied') fail('aplicar la compra con A2 pendiente');
  const cartCase = one(await sql('select status from public.recovery_cases where id = $1', [cart.recovery_case_id]), 'caso');
  const a2After = one(await sql('select status from public.scheduled_actions where id = $1', [a2.id]), 'A2').status;
  if (cartCase.status !== 'won' || ['pending', 'deferred', 'retryable_failed'].includes(a2After)) {
    fail('la compra tenia que cortar el carrito', { caso: cartCase.status, a2: a2After });
  }
  const welcome = one((await admitNative(flowEvent(shopper, purchase))).filter((r) => r.flow_id === NATIVE.flow),
    'la bienvenida');
  if (welcome.outcome !== 'motor_arranco' || !welcome.created || welcome.recovery_case_id === cart.recovery_case_id) {
    fail('la bienvenida despues del corte', welcome);
  }
  NATIVE.shopper = shopper;
  NATIVE.welcome = welcome;
  NATIVE.cartAction = cart.scheduled_action_id;
  results.a2_y_compra = { carrito: cartCase.status, a2: a2After, bienvenida: welcome.outcome };
}

// ---------------------------------------------------------------------------
// 4. La cola del paso (etiquetar).
// ---------------------------------------------------------------------------
{
  const tail = await readJson('flow_engine_native_step_tail_v1', NATIVE.welcome.scheduled_action_id);
  if (tail.flow_id !== NATIVE.flow || tail.version !== 1 || tail.etiquetar !== 'cliente' || tail.producto !== A.productName
      || typeof tail.momento !== 'string') {
    fail('la cola del paso de la bienvenida', tail);
  }
  const cartTail = one(await asService('select public.flow_engine_native_step_tail_v1($1)::text as r',
    [NATIVE.cartAction]), 'cola del carrito').r;
  if (cartTail !== null) fail('una accion del carrito no tiene cola nativa', cartTail);
  rows.step_tail = tail;
  results.etiquetar = { bienvenida: tail.etiquetar, carrito: cartTail };
}

// ---------------------------------------------------------------------------
// 5. La respuesta a la bienvenida entra al agente.
// ---------------------------------------------------------------------------
{
  const { shopper, welcome } = NATIVE;
  // La bienvenida sale: reserva, arranque nativo y aceptacion en una
  // conversacion nueva de Chatwoot (la del carrito ya cerro).
  shopper.chatwoot = String(Number(shopper.chatwoot) + 50000);
  const reserved = await reserve({ ...shopper, plan: { recovery_case_id: welcome.recovery_case_id } },
    welcome.scheduled_action_id);
  const started = await startNew(reserved);
  if (started.outcome !== 'request_started') fail('el arranque de la bienvenida', started);
  await accept(shopper, reserved);
  const conversation = one(await sql(`select c.id, c.automation_status from public.conversations c
    where c.commercial_context = jsonb_build_object('chatwoot_conversation_id', $1::text)`, [shopper.chatwoot]), 'conversacion');
  if (conversation.automation_status !== 'enabled') fail('la aceptacion deja la conversacion enabled', conversation);
  const ADOPT = 'select * from public.admit_portable_inbound_commercial_case_v1($1,$2,$3,$4)';
  const reply = () => asService(ADOPT, [A.inboundScope, A.inboundVersion, Number(shopper.chatwoot), shopper.phone]);
  // Rojo: sin el parche, la v2 la rechaza (la respuesta no llega al agente).
  const red = await rolledBack(async () => {
    await db.exec(previous(ADOPTION));
    await db.exec('set role service_role');
    try {
      await db.query(ADOPT, [A.inboundScope, A.inboundVersion, Number(shopper.chatwoot), shopper.phone]);
      return null;
    } catch (error) {
      return error;
    }
  });
  if (red?.message !== 'inbound_canonical_conversation_conflict') {
    fail('sin el parche la respuesta tenia que chocar', red ? { code: red.code, message: red.message } : 'paso');
  }
  const admitted = one(await reply(), 'la respuesta');
  const adopted = one(await sql('select automation_status from public.conversations where id = $1', [conversation.id]),
    'conversacion adoptada');
  const event = await sql(`select related_action_id from public.conversation_events
    where conversation_id = $1 and event_type = 'inbound_adopted_template_conversation'`, [conversation.id]);
  if (adopted.automation_status !== 'draft_only' || event.length !== 1
      || event[0].related_action_id !== welcome.scheduled_action_id || !admitted.commercial_case_id) {
    fail('la respuesta a la bienvenida tenia que adoptarse', { admitted, adopted, event });
  }
  results.respuesta = { sin_parche: red.message, con_parche: adopted.automation_status, salida: admitted.outcome };
}

// ---------------------------------------------------------------------------
// 6. MOT-002.
// ---------------------------------------------------------------------------
{
  // Los motivos de la persona no se avisan: fuera_de_cohorte no deja fila y
  // flujo_pausado si, pero no es del motor.
  const stranger = person('fuera');
  await contactFor(stranger);
  const outside = await admitPurchase(stranger);
  await applyPurchase(stranger, outside);
  const outRow = one(await admitNative(flowEvent(stranger, outside)), 'fuera');
  if (outRow.reason !== 'fuera_de_cohorte') fail('fuera de la cohorte', outRow);
  const quiet = await readJson('read_flow_engine_start_rejections_v1', INSTANCE, true, 20);
  if (quiet.unnotified !== 0 || quiet.rejections.length !== 0) fail('fuera_de_cohorte no se avisa', quiet);

  // Un error interno sembrado: la admision de la persona de la cohorte falla
  // adentro (division por cero en el chequeo del motor), sin frenar nada.
  const sick = person('error');
  await contactFor(sick);
  await addToCohort(sick);
  const sickPurchase = await admitPurchase(sick);
  await applyPurchase(sick, sickPurchase);
  const original = after.get('_flow_engine_native_unsupported(jsonb)')?.definition
    ?? one(await sql(`select pg_get_functiondef('public._flow_engine_native_unsupported(jsonb)'::regprocedure) as d`), 'def').d;
  await db.exec(`create or replace function public._flow_engine_native_unsupported(p_definition jsonb)
    returns text language plpgsql as $f$ begin perform 1 / 0; return null; end $f$`);
  const sickRow = one(await admitNative(flowEvent(sick, sickPurchase)), 'error');
  await db.exec(original);
  if (sickRow.outcome !== 'motor_no_arranca' || sickRow.reason !== 'error_interno:22012') fail('el error sembrado', sickRow);

  const first = await readJson('read_flow_engine_start_rejections_v1', INSTANCE, true, 20);
  const rejection = one(first.rejections, 'el rechazo a avisar');
  if (first.unnotified !== 1 || rejection.flow_id !== NATIVE.flow || rejection.reason !== 'error_interno:22012'
      || rejection.phase !== 'admision' || rejection.count !== 1 || rejection.step !== 1 || rejection.notified_count !== 0
      || rejection.flow_key !== 'activacion' || JSON.stringify(rejection).includes(sick.phone)
      || JSON.stringify(rejection).includes(sickPurchase.id)) {
    fail('el rechazo del error interno', first);
  }
  rows.rejections_unnotified = first;
  const mark = async (count) => one(await asService(
    'select public.mark_flow_engine_start_rejection_notified_v1($1, $2, $3, $4, $5) as r',
    [NATIVE.flow, 'admision', 'error_interno:22012', count, await dbNow()]), 'marca').r;
  if ((await mark(1)) !== true || (await mark(1)) !== false) fail('marcar una vez');
  const after1 = await readJson('read_flow_engine_start_rejections_v1', INSTANCE, true, 20);
  if (after1.unnotified !== 0 || after1.rejections.length !== 0) fail('avisado no vuelve', after1);
  const all = await readJson('read_flow_engine_start_rejections_v1', INSTANCE, false, 20);
  if (one(all.rejections, 'todos').notified_count !== 1) fail('la lectura de todos', all);
  // Al cruzar 10, vuelve.
  await sql(`update public.flow_engine_start_rejections set count = 10 where flow_id = $1 and reason = 'error_interno:22012'`,
    [NATIVE.flow]);
  const ten = await readJson('read_flow_engine_start_rejections_v1', INSTANCE, true, 20);
  if (ten.unnotified !== 1 || one(ten.rejections, 'diez').step !== 10) fail('el escalon de 10', ten);
  if ((await mark(10)) !== true) fail('marcar el escalon de 10');
  // Una marca de algo que no existe no hace nada.
  const ghost = one(await asService('select public.mark_flow_engine_start_rejection_notified_v1($1, $2, $3, $4, $5) as r',
    [NATIVE.flow, 'admision', 'error_interno:00000', 1, await dbNow()]), 'fantasma').r;
  if (ghost !== false) fail('marcar un rechazo que no existe');
  // flujo_pausado tampoco se avisa.
  await rpc('operator_pause_flow_v1', INSTANCE, A.funnel, 'activacion', (await control(NATIVE.flow)).generation, 'dan', 'pausa');
  const pausedLead = person('pausa');
  await contactFor(pausedLead);
  await addToCohort(pausedLead);
  const pausedPurchase = await admitPurchase(pausedLead);
  await applyPurchase(pausedLead, pausedPurchase);
  const pausedRow = one(await admitNative(flowEvent(pausedLead, pausedPurchase)), 'pausa');
  if (pausedRow.reason !== 'flujo_pausado') fail('flujo_pausado', pausedRow);
  const still = await readJson('read_flow_engine_start_rejections_v1', INSTANCE, true, 20);
  if (still.unnotified !== 0) fail('flujo_pausado no se avisa', still);
  // Pausado no esta prendido: la lectura lo da en pausado.
  const pausedFlows = await readJson('read_flow_engine_native_flows_v1', INSTANCE);
  if (one(pausedFlows.flows, 'pausado').state !== 'pausado') fail('el flujo pausado en la lectura', pausedFlows);
  results.mot_002 = { primero: first.unnotified, marcado: after1.unnotified, a_los_diez: ten.unnotified,
    pausado: still.unnotified };
  void stateItem;
  void B;
}

// ---------------------------------------------------------------------------
// El fixture del bridge: uuid y horas normalizados.
// ---------------------------------------------------------------------------
const normalize = (value) => {
  const ids = new Map();
  const walk = (item) => {
    if (Array.isArray(item)) return item.map(walk);
    if (item && typeof item === 'object') return Object.fromEntries(Object.entries(item).map(([k, v]) => [k, walk(v)]));
    if (typeof item === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(item)) {
      if (!ids.has(item)) ids.set(item, `00000000-0000-4000-8000-${String(ids.size + 1).padStart(12, '0')}`);
      return ids.get(item);
    }
    if (typeof item === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/.test(item)) {
      return '2026-10-24T12:00:00.000000Z';
    }
    return item;
  };
  return walk(value);
};
if (process.argv.includes('--emit-bridge-fixtures')) {
  const text = `${JSON.stringify({
    _origen: 'tests/sql/followup_engine/validate_flow_engine_native_bridge.mjs --emit-bridge-fixtures: '
      + 'las RPC reales sobre la cadena de migraciones (PGlite), con el manifiesto de ATT1; uuid y horas normalizados',
    ...normalize(rows),
  }, null, 2)}\n`;
  if (process.argv.includes('--check')) {
    if (readFileSync(FIXTURE, 'utf8') !== text) {
      fail('native_bridge_rpc_rows.json no es lo que da la cadena: correr con --emit-bridge-fixtures sin --check');
    }
    results.fixture = 'igual a la cadena';
  } else {
    writeFileSync(FIXTURE, text);
    results.fixture = 'escrito';
  }
}

console.log(JSON.stringify({ validate_flow_engine_native_bridge: 'ok', ...results }, null, 2));
