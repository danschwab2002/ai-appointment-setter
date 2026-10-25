// El Automation Expert ve el motor nativo (migracion 20261025000100, el PR 5
// del hito 4), con las RPC reales sobre la cadena completa de migraciones.
//
// Contratos: docs/contracts/automation-expert-read-v1.md (1.2.0),
// docs/contracts/flow-engine-v2.md (seccion 15) y
// docs/contracts/flow-change-cycle-v1.md (1.8.0).
//
// Prueba, con un rojo a proposito por pieza (el rojo se afirma):
//   0. la migracion: agrega exactamente sus tres lecturas, solo para
//      service_role; cambia exactamente cuatro funciones, cada una con su
//      marca, sin tocar permisos; todas las demas quedan iguales; correrla dos
//      veces aborta y otro duenio aborta;
//   1. un flujo nativo de punta a punta (la bienvenida del PR 4, en cohorte):
//      la compra de la cohorte arranca el recorrido, sale el envio, y otra
//      compra de la misma persona la ata el recuperador al caso;
//   2. los flujos nativos: estado, version, bolsa, cohorte y lo observado, sin
//      nada de una persona;
//   3. la historia: los momentos del recorrido (arranco, programado,
//      autorizado, aceptado, compro); el anclaje deja ver a una persona que
//      solo tiene un recorrido nativo (rojo: sin el parche, la historia y la
//      busqueda no la encuentran);
//   4. las ventas por flujo: una compra despues del envio, en el toque que
//      salio, y la compra que disparo el recorrido no cuenta; la ventana de
//      mas da 22023;
//   5. prender con a: cohorte: de pausado vuelve a la cohorte (rojo: sin el
//      parche de los items no se guarda); prender sin a sigue llevando a
//      prendido.
//
// Datos sinteticos, los de buildWorld: personas de example.test con
// telefonos 1202555xxxx (rango de ficcion) y el manifiesto de ATT1.
import { PGlite } from '@electric-sql/pglite';
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261025000100_automation_expert_native_reads_v1.sql';
const TZ = 'timestamp with time zone';
const results = {};
// --emit-fixture: deja lo que devolvieron las tres lecturas en
// tests/fixtures/automation_expert_native/, para las pruebas del bridge.
const EMIT = process.argv.includes('--emit-fixture');
const emitted = {};
const fixture = (path) => JSON.parse(readFileSync(join(root, 'tests/fixtures', path), 'utf8'));
const BRIDGE = fixture('flow_change_new_flow/ae_bienvenida_20261024.json');

// ---------------------------------------------------------------------------
// 0. La forma de la migracion, en una base propia: antes y despues.
// ---------------------------------------------------------------------------
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('automation_expert_change', p.oid, 'execute') as agent_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));

const ADDED = [
  'read_automation_expert_native_flows_v1(jsonb)',
  'read_automation_expert_native_history_v1(jsonb,uuid)',
  `read_flow_sales_v1(jsonb,${TZ})`,
];
const PATCHED = {
  'read_automation_expert_lead_history_v1(jsonb,uuid)': 'automation_expert_native_anchor_v1',
  'find_automation_expert_lead_v1(jsonb,bigint,text,text)': 'automation_expert_native_anchor_v1',
  '_flow_change_items_are_valid(jsonb)': 'flow_change_prender_a_cohorte_v1',
  '_flow_change_publish(text,text,uuid,text,jsonb)': 'flow_change_prender_a_cohorte_v1',
};

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
// Despues va solo el btrim del nombre del producto en el pago rechazado (E-087), que solo parcha esa admision.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261025000200_payment_failure_product_name_btrim.sql'])) {
  fail(`despues de ${MIGRATION} va solo la del btrim del nombre del producto`, names.slice(position + 1));
}
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
await applyFile(shapeDb, join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of names.slice(0, position)) await applyFile(shapeDb, join(root, 'supabase/migrations', name));
const before = await snapshot(shapeDb);
const target = join(root, 'supabase/migrations', MIGRATION);
{
  await shapeDb.exec('create role otro_duenio nologin');
  const error = await expectError(shapeDb, 'otro duenio', async () => {
    await shapeDb.exec('alter function public.find_automation_expert_lead_v1(jsonb,bigint,text,text) owner to otro_duenio');
    await applyFile(shapeDb, target);
  }, { code: '55000', message: 'automation_expert_native_owner_mismatch' });
  results.duenio = `${error.code}:${error.message}`;
}
await applyFile(shapeDb, target);
const after = await snapshot(shapeDb);
{
  const added = [...after.keys()].filter((signature) => !before.has(signature));
  const removed = [...before.keys()].filter((signature) => !after.has(signature));
  const changed = [...before.keys()].filter((signature) => after.has(signature)
    && after.get(signature).definition !== before.get(signature).definition);
  const aclChanged = [...before.keys()].filter((signature) => after.has(signature)
    && ['service_x', 'agent_x', 'api_x', 'acl'].some((key) => after.get(signature)[key] !== before.get(signature)[key]));
  if (!sameSet(added, ADDED) || removed.length !== 0 || !sameSet(changed, Object.keys(PATCHED))
      || aclChanged.length !== 0) {
    fail('la migracion cambio otra cosa', { added, removed, changed, aclChanged });
  }
  for (const signature of ADDED) {
    const row = after.get(signature);
    if (row.api_x || !row.service_x || row.agent_x) fail(`${signature}: permisos`, row);
  }
  for (const [signature, marker] of Object.entries(PATCHED)) {
    if (!after.get(signature).definition.includes(marker)) fail(`${signature}: falta ${marker}`);
    if (before.get(signature).definition.includes(marker)) fail(`${signature}: ${marker} ya estaba`);
  }
  const error = await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'automation_expert_native_reads_already_applied' });
  results.segunda_corrida = `${error.code}:${error.message}`;
  results.forma = { agregadas: added.length, cambiadas: changed.length, iguales: before.size - changed.length };
}
await shapeDb.close();
const previous = (signature) => before.get(signature).definition;

// ---------------------------------------------------------------------------
// El mundo: ATT1 (A) con el carrito en el almacen (buildWorld).
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, person, rpc, cycle, stateItem, control, pauseFlow, dbNow, reserve, startNew, accept } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const INSTANCE_ZONE = { ...INSTANCE, time_zone: A.timezone };
// La marca de instancia de las lecturas del AE (InstanceMark.to_rpc del bridge).
const AE_INSTANCE = {
  ...INSTANCE, chatwoot_account_id: A.accountId, chatwoot_inbox_id: A.inboxId, hotmart_product_id: String(A.productId),
  inbound_scope_key: 'att1-inbound', agent_scope_ref: 'att1-agente', time_zone: A.timezone, scopes: [],
};
const AE = 'U0123456789';
const SHA = 'a'.repeat(64);
const CLEAN = { blocks: [], warnings: [] };
const rolledBack = async (run) => {
  await db.exec('begin');
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};
// Con la transaccion abortada no se puede resetear: el rollback de expectError
// deshace tambien el set role.
const asRole = async (role, run) => {
  await db.exec(`set role ${role}`);
  const value = await run();
  await db.exec('reset role');
  return value;
};
const read = async (fn, ...params) => asRole('service_role', () => rpc(fn, ...params));

// ---------------------------------------------------------------------------
// 1. La bienvenida del PR 4, publicada en cohorte, con un recorrido que vende.
// ---------------------------------------------------------------------------
const instanceBase = async () => (await db.query('select public._flow_store_base($1, $2, $3::uuid[]) b',
  [A.tenant, A.ally, []])).rows[0].b;
const WELCOME = {};
{
  const item = structuredClone(BRIDGE.items[0]);
  const validatedBase = await instanceBase();
  await db.exec('set role automation_expert_change');
  const saved = await rpc('save_flow_change_draft_v1', INSTANCE, null, null, [item], {}, BRIDGE.purpose, AE);
  const recorded = await rpc('record_flow_change_validation_v1', INSTANCE, saved.proposal_id, saved.revision,
    validatedBase, BRIDGE.validator, BRIDGE.result, BRIDGE.preview, AE);
  await db.exec('reset role');
  const approved = await rpc('operator_approve_flow_change_v1', INSTANCE, saved.proposal_id, recorded.fingerprint, 'dan');
  const published = await rpc('operator_publish_flow_change_v1', INSTANCE_ZONE, saved.proposal_id, recorded.fingerprint, {
    fingerprint: recorded.fingerprint, validator: BRIDGE.validator, result: CLEAN, catalog_sha256: SHA,
    validated_at: new Date().toISOString(),
  });
  if (!saved.ok || !approved.ok || !published.ok || published.published[0].state !== 'cohorte') {
    fail('publicar la bienvenida', { saved, recorded, approved, published });
  }
  WELCOME.flow = published.published[0].flow_id;
  WELCOME.pool = published.published[0].pool;
}
const contactFor = async (lead) => {
  if (lead.contact) return lead.contact;
  lead.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
    values ($1, $2, $3, 'MX') returning id`, [lead.name, lead.email, lead.phone]), 'contacto').id;
  await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1, 'email', $2, $2, 'hotmart'), ($1, 'phone', $3, $3, 'hotmart')`, [lead.contact, lead.email, lead.phone]);
  return lead.contact;
};
const CAPTURED_PURCHASE = fixture('hotmart_cash_payment_approved_att1_20261012.json').eventos[1].payload;
let purchaseSerial = 0;
const purchaseFor = async (lead) => {
  purchaseSerial += 1;
  const now = await dbNow();
  const payload = structuredClone(CAPTURED_PURCHASE);
  payload.id = `lecturas-compra-${lead.email}-${purchaseSerial}`;
  payload.creation_date = now.getTime() - 60_000;
  Object.assign(payload.data.buyer, { name: lead.name, first_name: lead.name.split(' ')[0], email: lead.email,
    checkout_phone: lead.phone });
  payload.data.product.id = A.productId;
  payload.data.purchase.offer.code = lead.offer.offer_code;
  payload.data.purchase.transaction = `HP${String(920000000 + purchaseSerial)}`;
  payload.data.purchase.approved_date = now.getTime() - 60_000;
  payload.data.purchase.order_date = now.getTime() - 120_000;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone]), 'compra');
  if (admitted.outcome !== 'inserted') fail('admitir la compra', admitted);
  await contactFor(lead);
  one(await sql('select * from public.apply_hotmart_purchase_approved($1,$2,$3,$4,$5,$6,$7::timestamptz)',
    [admitted.webhook_event_id, lead.email, lead.phone, String(A.productId), lead.offer.offer_code,
      payload.data.purchase.transaction, new Date(payload.data.purchase.approved_date).toISOString()]), 'aplicar');
  return { id: admitted.webhook_event_id, moment: new Date(payload.creation_date).toISOString() };
};
const admitNative = async (lead, purchase) => asRole('service_role', async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)', [
    JSON.stringify(INSTANCE), JSON.stringify({
      fuente: 'hotmart', tipo: 'compra', evento_id: purchase.id, producto_ref: A.funnel, oferta: lead.offer.offer_code,
      contacto_id: lead.contact, momento: purchase.moment, chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
      destino: lead.phone,
    })])).rows);
const buyer = person('lecturas-cohorte');
{
  await contactFor(buyer);
  const added = await rpc('operator_flow_cohort_v1', INSTANCE, A.funnel, 'bienvenida', 'agregar', buyer.phone, 'dan',
    'el telefono de prueba');
  if (!added.ok) fail('la cohorte', added);
  const trigger = await purchaseFor(buyer);
  const started = one((await admitNative(buyer, trigger)).filter((row) => row.flow_id === WELCOME.flow), 'arranque');
  if (started.outcome !== 'motor_arranco') fail('la compra de la cohorte', started);
  buyer.plan = { recovery_case_id: started.recovery_case_id };
  const reserved = await reserve(buyer, started.scheduled_action_id);
  const send = await startNew(reserved);
  if (send.outcome !== 'request_started') fail('el arranque nativo', send);
  await accept(buyer, reserved);
  WELCOME.trigger = trigger.id;
  WELCOME.case = started.recovery_case_id;
  WELCOME.action = started.scheduled_action_id;
  // Otra compra de la misma persona, con el caso abierto: la ata el recuperador.
  const repeat = await purchaseFor(buyer);
  const stored = one(await sql('select status, purchase_event_id from public.recovery_cases where id = $1',
    [WELCOME.case]), 'caso');
  WELCOME.repeat = repeat.id;
  WELCOME.attached = stored.purchase_event_id === repeat.id;
  results.recorrido = { caso: stored.status, compra_atada: WELCOME.attached };
}

// ---------------------------------------------------------------------------
// 2. Los flujos nativos.
// ---------------------------------------------------------------------------
{
  const native = await read('read_automation_expert_native_flows_v1', AE_INSTANCE);
  emitted.rpc_native_flows = native;
  const flow = one(native.flows.filter((row) => row.flow_key === 'bienvenida'), 'bienvenida');
  if (native.flows.some((row) => row.flow_key === 'carrito')) fail('el carrito del legado no es nativo', native.flows);
  if (flow.state !== 'cohorte' || flow.active_version !== 1 || flow.pool !== WELCOME.pool || flow.pool_timezone !== A.timezone
      || flow.cohort_members !== 1 || flow.observed.runs_started !== 1 || flow.observed.accepted_by_chatwoot !== 1
      || flow.starts_today !== 1 || flow.definition?.dispara?.evento !== 'compra') {
    fail('el flujo nativo', flow);
  }
  const text = JSON.stringify(native);
  if (text.includes(buyer.phone) || text.includes(buyer.email) || text.includes(buyer.contact)) {
    fail('la lectura de los flujos trae datos de una persona');
  }
  const denied = await expectError(db, 'el rol del agente no lee', () => asRole('automation_expert_change',
    () => rpc('read_automation_expert_native_flows_v1', AE_INSTANCE)), { code: '42501' });
  results.flujos = { estado: flow.state, cohorte: flow.cohort_members, observado: flow.observed, rol_agente: denied.code };
}

// ---------------------------------------------------------------------------
// 3. La historia de la persona.
// ---------------------------------------------------------------------------
{
  const native = await read('read_automation_expert_native_history_v1', AE_INSTANCE, buyer.contact);
  emitted.rpc_native_history = native;
  const kinds = native.moments.map((moment) => moment.kind);
  for (const kind of ['entered_flow', 'scheduled', 'start_authorized', 'accepted_by_chatwoot']) {
    if (!kinds.includes(kind)) fail(`la historia nativa no trae ${kind}`, kinds);
  }
  if (WELCOME.attached && !kinds.includes('purchased')) fail('la historia nativa no trae la compra atada', kinds);
  if (native.moments.some((moment) => moment.flow_key !== 'bienvenida' || moment.flow_version.definition !== 1)
      || JSON.stringify(native.cohort_of) !== JSON.stringify(['bienvenida'])) {
    fail('los momentos nativos', native);
  }
  const text = JSON.stringify(native);
  if (text.includes(buyer.phone) || text.includes(buyer.email)) fail('la historia nativa trae el telefono o el mail');
  const stranger = person('lecturas-nadie');
  await contactFor(stranger);
  const nobody = await read('read_automation_expert_native_history_v1', AE_INSTANCE, stranger.contact);
  if (nobody !== null) fail('una persona sin recorridos nativos', nobody);
  // El anclaje: la persona solo tiene el recorrido nativo.
  const history = await read('read_automation_expert_lead_history_v1', AE_INSTANCE, buyer.contact);
  const found = await read('find_automation_expert_lead_v1', AE_INSTANCE, null, null, buyer.name.split(' ')[0]);
  const redAnchor = await rolledBack(async () => {
    await db.exec(previous('read_automation_expert_lead_history_v1(jsonb,uuid)'));
    await db.exec(previous('find_automation_expert_lead_v1(jsonb,bigint,text,text)'));
    return {
      history: await read('read_automation_expert_lead_history_v1', AE_INSTANCE, buyer.contact),
      found: await read('find_automation_expert_lead_v1', AE_INSTANCE, null, null, buyer.name.split(' ')[0]),
    };
  });
  const foundText = JSON.stringify(found);
  if (history === null || !foundText.includes(buyer.contact) || redAnchor.history !== null
      || JSON.stringify(redAnchor.found ?? null).includes(buyer.contact)) {
    fail('el anclaje de la persona con un recorrido nativo', { history: history !== null, found, redAnchor });
  }
  results.historia = { momentos: kinds, cohorte_de: native.cohort_of, sin_el_parche: 'no la encuentra' };
}

// ---------------------------------------------------------------------------
// 4. Las ventas por flujo.
// ---------------------------------------------------------------------------
{
  const sales = await read('read_flow_sales_v1', AE_INSTANCE, new Date(Date.now() - 7 * 86_400_000).toISOString());
  emitted.rpc_flow_sales = sales;
  const welcome = one(sales.flows.filter((row) => row.flow_key === 'bienvenida'), 'ventas de la bienvenida');
  // La bienvenida es de un solo envio: al salir, el caso cierra (sequence_exhausted) y la otra compra no
  // la ata ningun caso. Cuenta por la persona (by_identity), en el toque que salio. La compra que disparo
  // el recorrido no cuenta.
  if (WELCOME.attached) fail('la otra compra quedo atada al caso: la prueba esperaba el caso cerrado');
  if (sales.rule !== 'purchase_after_accepted_send_v1' || welcome.native !== true || welcome.runs_started !== 1
      || welcome.purchases_after_send !== 1 || welcome.by_identity !== 1 || welcome.by_case !== 0
      || welcome.people_after_send !== 1 || welcome.purchases_before_send !== 0
      || JSON.stringify(Object.values(welcome.by_step)) !== '[1]') {
    fail('las ventas de la bienvenida', welcome);
  }
  // Rojo: sin el envio aceptado, la misma compra no cuenta.
  const unsent = await rolledBack(async () => {
    await sql("update public.followup_delivery_attempts set accepted_at = now() + interval '1 hour' where action_id = $1",
      [WELCOME.action]);
    return read('read_flow_sales_v1', AE_INSTANCE, new Date(Date.now() - 7 * 86_400_000).toISOString());
  });
  if (one(unsent.flows.filter((row) => row.flow_key === 'bienvenida'), 'sin envio').purchases_after_send !== 0) {
    fail('una compra antes del envio conto como venta', unsent);
  }
  const text = JSON.stringify(sales);
  if (text.includes(buyer.contact) || text.includes(buyer.email) || text.includes(buyer.phone)) {
    fail('las ventas traen datos de una persona');
  }
  const tooWide = await expectError(db, 'la ventana de mas', () => read('read_flow_sales_v1', AE_INSTANCE,
    new Date(Date.now() - 40 * 86_400_000).toISOString()), { code: '22023', message: 'flow_sales_window_too_wide' });
  results.ventas = { bienvenida: welcome, ventana: tooWide.message };
}

// ---------------------------------------------------------------------------
// 5. prender con a: cohorte.
// ---------------------------------------------------------------------------
{
  await pauseFlow(WELCOME.flow, A.funnel, 'bienvenida');
  const toCohort = { ...stateItem(WELCOME.flow, A.funnel, 'bienvenida', 'prender'), a: 'cohorte' };
  // Rojo: sin el parche de los items, prender con a no se guarda.
  const red = await rolledBack(async () => {
    await db.exec(previous('_flow_change_items_are_valid(jsonb)'));
    await db.exec('savepoint rojo');
    try {
      await rpc('operator_save_flow_change_draft_v1', INSTANCE, null, null, [toCohort],
        { [WELCOME.flow]: { active_version: 1 } }, 'volver a la cohorte', 'dan');
      return 'paso';
    } catch (error) {
      await db.exec('rollback to savepoint rojo');
      return error.message;
    }
  });
  if (red !== 'flow_change_items_invalid') fail('sin el parche de los items', red);
  const wrong = await expectError(db, 'a distinto de cohorte', () => rpc('operator_save_flow_change_draft_v1', INSTANCE,
    null, null, [{ ...toCohort, a: 'prendido' }], { [WELCOME.flow]: { active_version: 1 } }, 'x', 'dan'),
    { message: 'flow_change_items_invalid' });
  await cycle([toCohort], 'volver a la cohorte');
  const back = await control(WELCOME.flow);
  if (back.state !== 'cohorte') fail('de pausado a la cohorte', back);
  await cycle([stateItem(WELCOME.flow, A.funnel, 'bienvenida', 'prender')], 'para todos');
  const everyone = await control(WELCOME.flow);
  if (everyone.state !== 'prendido') fail('prender sin a sigue llevando a prendido', everyone);
  results.prender_a_cohorte = { pausado: 'cohorte', sin_a: everyone.state, a_otro: wrong.message };
}

if (EMIT) {
  const dir = join(root, 'tests/fixtures/automation_expert_native');
  mkdirSync(dir, { recursive: true });
  for (const [name, value] of Object.entries(emitted)) {
    writeFileSync(join(dir, `${name}.json`), `${JSON.stringify({
      _capture: { source: 'tests/sql/followup_engine/validate_automation_expert_native_reads.mjs --emit-fixture',
        migration: MIGRATION, note: 'la salida real de la funcion sobre la cadena de migraciones en PGlite, datos sinteticos' },
      value }, null, 2)}\n`);
  }
}
console.log(JSON.stringify({ validate_automation_expert_native_reads: 'ok', ...results }, null, 2));
