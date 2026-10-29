// El Automation Expert crea flujos (migracion 20261024000600, el PR 4 del hito 4),
// con las RPC reales sobre la cadena completa de migraciones.
//
// Contratos: docs/contracts/flow-change-cycle-v1.md (1.7.0) y
// docs/contracts/flow-engine-v2.md (secciones 6, 7 y 14).
//
// Prueba, con un rojo a proposito por pieza (el rojo se afirma):
//   0. la migracion: agrega exactamente sus cuatro funciones; cambia
//      exactamente seis, cada una con su marca, sin tocar permisos; todas las
//      demas definiciones y su ACL quedan iguales; la lectura de la simulacion
//      es de service_role y del rol del agente, y nada mas; correrla dos veces
//      aborta y otro duenio aborta;
//   1. la conversacion de prueba («crea la bienvenida despues de la compra»):
//      lo que guardo y valido el bridge contra el doble
//      (tests/fixtures/flow_change_new_flow/ae_bienvenida_20261024.json, lo
//      escribe tests/test_flow_change_new_flow.py) lo guarda y lo registra el
//      rol del agente; el operador aprueba y publica, y termina en un flujo
//      nativo publicado en cohorte: el control, la v1, la bolsa, la ruta
//      nativa y flow_created, en la misma transaccion. Rojos: sin el parche de
//      los items no se guarda (flow_change_items_invalid), sin el de publicar
//      no se crea nada, y sin la zona de la instancia no se publica
//      (flow_engine_native_zone_missing) ni se escribe nada;
//   2. lo que no se crea: la clave ya registrada (flow_already_registered),
//      prender distinto de cohorte (new_flow_must_start_in_cohort), un flujo
//      del legado (el carrito: motor_nativo:disparador), la plantilla a crear
//      (flow_definition_unresolved_value) y la primera version de un flujo
//      registrado sin version, que sigue siendo del operador
//      (flow_creation_not_enabled);
//   3. la cohorte del operador: agregar, otra vez sin cambiar, sacar, un
//      telefono que no es de nadie, uno con dos contactos, el tope de 5, un
//      flujo del legado, y que ni la respuesta ni el evento traen el telefono
//      o el contacto;
//   4. la admision: con el contacto en la cohorte, la compra arranca la
//      bienvenida; con otro, fuera_de_cohorte. Pasarla a todos es otra
//      propuesta (prender): desde ahi arranca para cualquiera;
//   5. la simulacion: la lectura cuenta las compras y las personas, como el
//      rol del agente; con otro disparador o una ventana de mas, 22023;
//   6. lo de hoy sin cambios: el almacen dice native false de los flujos del
//      legado, y un cambio de version del carrito se publica como antes.
//
// Datos sinteticos, los de buildWorld: personas de example.test con
// telefonos 1202555xxxx (rango de ficcion) y el manifiesto de ATT1.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261024000600_flow_change_new_flow_v1.sql';
const TZ = 'timestamp with time zone';
const results = {};
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

const ADDED = {
  '_flow_change_new_flow_zone(jsonb)': [false, false],
  '_flow_change_new_flow_blocker(text,text,jsonb)': [false, false],
  'operator_flow_cohort_v1(jsonb,text,text,text,text,text,text)': [false, false],
  [`read_flow_engine_native_trigger_counts_v1(jsonb,text,text,${TZ})`]: [true, true],
};
const PATCHED = {
  '_flow_change_items_are_valid(jsonb)': ['flow_change_new_flow: begin'],
  '_flow_change_item_flow_ids(jsonb)': ['flow_change_new_flow: un item nuevo'],
  '_flow_change_save_draft(text,text,uuid,integer,jsonb,jsonb,text,text,text)': ['flow_change_new_flow: begin'],
  '_flow_change_publish(text,text,uuid,text,jsonb)': ['flow_change_new_flow: las variables',
    'flow_change_new_flow: begin (los chequeos)', 'flow_change_new_flow: begin (registrar y abrir)'],
  'operator_publish_flow_change_v1(jsonb,uuid,text,jsonb)': ['flow_change_new_flow: la zona'],
  'read_flow_store_v1(jsonb)': ['flow_change_new_flow: native'],
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
// Despues va solo la de las lecturas nativas del AE (20261025000100): suma lecturas y parcha prender
// (a: cohorte) sin tocar lo que esta prueba mira.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261025000100_automation_expert_native_reads_v1.sql',
  // El btrim del nombre del producto en el pago rechazado (E-087): solo parcha esa admision.
  '20261025000200_payment_failure_product_name_btrim.sql',
  // La lectura del link para la nota privada al derivar (F-034 D8): una funcion nueva.
  '20261025000300_handoff_link_note_v1.sql',
  // Las conversaciones del comprador para pausar al agente (E-100): una funcion nueva.
  '20261025000400_purchase_agent_pause_v1.sql',
  // La forma nativa con las variables del paso (E-115): parcha la forma nativa y el compilador.
  '20261026000100_flow_engine_native_template_shape_v1.sql',
  // La oferta del evento en el caso nativo (E-122): parcha la admision nativa y el contexto del despacho.
  '20261026000200_flow_engine_native_offer_in_context_v1.sql',
  // La pagina de aprobacion (A′ PR 2): el rol del aprobador, sus sesiones, la cohorte con slack y la ruta
  // nativa firmada por slack. No toca lo que esta prueba mira.
  '20261028000100_flow_change_slack_approver_v1.sql',
  // El carrito nativo (E-161): parcha la admision y el arranque nativos y vuelve a crear el contexto del paso.
  '20261028000200_flow_engine_native_cart_v1.sql',
  // Sumar ofertas a un scope publicado (E-163): solo el trigger de pilot_scope_versions.
  '20261028000300_pilot_scope_offer_append_v1.sql',
  // La compra con nuestro link cuenta siempre (E-169): el helper del cierre por el link y el correlador.
  '20261028000400_purchase_by_our_link_counts_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de las lecturas nativas del AE`, names.slice(position + 1));
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
    await shapeDb.exec('alter function public.read_flow_store_v1(jsonb) owner to otro_duenio');
    await applyFile(shapeDb, target);
  }, { code: '55000', message: 'flow_change_new_flow_owner_mismatch' });
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
  if (!sameSet(added, Object.keys(ADDED)) || removed.length !== 0 || !sameSet(changed, Object.keys(PATCHED))
      || aclChanged.length !== 0) {
    fail('la migracion cambio otra cosa', { added, removed, changed, aclChanged });
  }
  for (const [signature, [service, agent]] of Object.entries(ADDED)) {
    const row = after.get(signature);
    if (row.api_x || row.service_x !== service || row.agent_x !== agent) fail(`${signature}: permisos`, row);
  }
  for (const [signature, markers] of Object.entries(PATCHED)) {
    for (const marker of markers) {
      if (!after.get(signature).definition.includes(marker)) fail(`${signature}: falta ${marker}`);
      if (before.get(signature).definition.includes(marker)) fail(`${signature}: ${marker} ya estaba`);
    }
  }
  const error = await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'flow_change_new_flow_already_applied' });
  results.segunda_corrida = `${error.code}:${error.message}`;
  results.forma = { agregadas: added.length, cambiadas: changed.length, iguales: before.size - changed.length };
}
await shapeDb.close();
const previous = (signature) => before.get(signature).definition;

// ---------------------------------------------------------------------------
// El mundo: ATT1 (A), el carrito y el pago fallido de A en el almacen
// (buildWorld), con la ruta del carrito en legado.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, FLOWS, INSTANCE, person, rpc, register, cycle, cartDefinition, versionItem, stateItem, control,
  dbNow } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
{
  const created = await world.createRoute(A.funnel, 'carrito', 'carrito', A.scope);
  if (!created.ok) fail('la ruta del carrito', created);
}
const INSTANCE_ZONE = { ...INSTANCE, time_zone: A.timezone };
const AE = 'U0123456789';
const SHA = 'a'.repeat(64);
const CLEAN = { blocks: [], warnings: [] };
const VALIDATOR_AE = { contract: 'setter-flujo/v1', rules_version: 1, phase: 'publish', actor: 'automation_expert',
  product_version: 'v1.17.1', git_sha: 'fc62393' };
const errorOf = async (run) => {
  let inTransaction = true;
  try {
    await db.exec('savepoint esperado');
  } catch (error) {
    if (error.code !== '25P01') throw error;
    inTransaction = false;
    await db.exec('begin');
  }
  try {
    await run();
    await db.exec(inTransaction ? 'release savepoint esperado' : 'commit');
    return null;
  } catch (error) {
    await db.exec(inTransaction ? 'rollback to savepoint esperado' : 'rollback');
    if (inTransaction) await db.exec('release savepoint esperado');
    return error;
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
// Como el rol del agente. Con la transaccion abortada no se puede resetear: el rollback de errorOf
// (al savepoint) deshace tambien el set role.
const asAgent = async (run) => {
  await db.exec('set role automation_expert_change');
  let failed = false;
  try {
    return await run();
  } catch (error) {
    failed = true;
    throw error;
  } finally {
    if (!failed) await db.exec('reset role');
  }
};
const instanceBase = async () => (await db.query('select public._flow_store_base($1, $2, $3::uuid[]) b',
  [A.tenant, A.ally, []])).rows[0].b;
const controlByKey = async (flowKey) => (await sql(`select flow_id, state, active_version, generation::int as generation
  from public.flow_controls where tenant_ref = $1 and product_ref = $2 and flow_key = $3`, [A.tenant, A.funnel, flowKey]))[0];

// El item del bridge, con otra clave (un flujo nuevo por prueba).
const bridgeItem = (flujo = 'bienvenida', change = null) => {
  const item = structuredClone(BRIDGE.items[0]);
  item.flujo = flujo;
  item.definicion.flujo = flujo;
  item.definicion.topes.bolsa = `${A.funnel}-${flujo}`.slice(0, 40);
  if (change) change(item);
  return item;
};
// Lo que hace el agente con sus herramientas, ya pasado por el bridge: guardar y registrar la validacion.
const agentProposes = async (items, { result = BRIDGE.result, preview = BRIDGE.preview } = {}) => {
  const validatedBase = await instanceBase();
  return asAgent(async () => {
    const saved = await rpc('save_flow_change_draft_v1', INSTANCE, null, null, items, {}, BRIDGE.purpose, AE);
    if (!saved.ok) return { saved };
    const recorded = await rpc('record_flow_change_validation_v1', INSTANCE, saved.proposal_id, saved.revision,
      validatedBase, BRIDGE.validator, result, preview, AE);
    return { saved, recorded };
  });
};
const publish = async (proposal, instance = INSTANCE_ZONE) => {
  const approved = await rpc('operator_approve_flow_change_v1', INSTANCE, proposal.saved.proposal_id,
    proposal.recorded.fingerprint, 'dan');
  if (!approved.ok) fail('aprobar', approved);
  return rpc('operator_publish_flow_change_v1', instance, proposal.saved.proposal_id, proposal.recorded.fingerprint, {
    fingerprint: proposal.recorded.fingerprint, validator: BRIDGE.validator, result: CLEAN, catalog_sha256: SHA,
    validated_at: new Date().toISOString(),
  });
};

// ---------------------------------------------------------------------------
// 1. La conversacion de prueba, de punta a punta.
// ---------------------------------------------------------------------------
const WELCOME = {};
{
  if (BRIDGE.items.length !== 1 || BRIDGE.items[0].tipo !== 'nuevo' || 'flow_id' in BRIDGE.items[0]
      || BRIDGE.items[0].prender !== 'cohorte' || BRIDGE.result.blocks.length !== 0
      || BRIDGE.preview.simulacion.status !== 'built') {
    fail('el puente del bridge no es el de la conversacion de prueba', BRIDGE.items);
  }
  // Rojo: sin el parche de los items, el item nuevo no se guarda.
  const redItems = await rolledBack(async () => {
    await db.exec(previous('_flow_change_items_are_valid(jsonb)'));
    return errorOf(() => agentProposes([bridgeItem('bienvenida_roja')]));
  });
  if (redItems?.message !== 'flow_change_items_invalid') fail('sin el parche de los items', redItems?.message ?? 'paso');

  // Rojo: sin la zona de la instancia (horario: siempre), no se publica y no se escribe nada.
  const noZone = await agentProposes([bridgeItem('bienvenida_sin_zona')]);
  const refusedZone = await publish(noZone, INSTANCE);
  if (refusedZone.ok || refusedZone.reason_code !== 'flow_engine_native_zone_missing'
      || await controlByKey('bienvenida_sin_zona')) {
    fail('sin la zona de la instancia', refusedZone);
  }

  // Rojo: sin el parche de publicar, la propuesta pasa a publicada y no crea el flujo.
  const redPublish = await rolledBack(async () => {
    const proposal = await agentProposes([bridgeItem('bienvenida_sin_parche')]);
    await db.exec(previous('_flow_change_publish(text,text,uuid,text,jsonb)'));
    const published = await publish(proposal);
    return { published, control: await controlByKey('bienvenida_sin_parche') };
  });
  if (!redPublish.published.ok || redPublish.control) fail('sin el parche de publicar no se crea nada', redPublish);

  // La de verdad.
  const proposal = await agentProposes([bridgeItem()]);
  if (!proposal.saved.ok || proposal.recorded.status !== 'validada') fail('el agente propone la bienvenida', proposal);
  const stored = one(await sql(`select requested_by_kind, draft_base, items, validation -> 'preview' -> 'simulacion' as sim
    from public.flow_change_proposals where id = $1`, [proposal.saved.proposal_id]), 'propuesta');
  if (stored.requested_by_kind !== 'automation_expert' || JSON.stringify(stored.draft_base) !== '{}'
      || stored.sim.status !== 'built') {
    fail('la propuesta del agente', stored);
  }
  const published = await publish(proposal);
  const [entry] = published.published ?? [];
  if (!published.ok || published.published.length !== 1 || entry.state !== 'cohorte' || entry.version !== 1
      || entry.pool !== 'att1-bienvenida') {
    fail('publicar la bienvenida', published);
  }
  WELCOME.flow = entry.flow_id;
  const state = await control(WELCOME.flow);
  const definition = one(await sql(`select version, proposal_id, definition from public.flow_definitions
    where flow_id = $1`, [WELCOME.flow]), 'v1');
  const route = one(await sql('select * from public.flow_engine_routes where flow_id = $1', [WELCOME.flow]), 'ruta');
  const pool = one(await sql('select * from public.flow_engine_pools where pool = $1', [entry.pool]), 'bolsa');
  const created = await sql(`select event_type, reason_code, data from public.flow_change_events
    where flow_id = $1 and event_type = 'flow_created'`, [WELCOME.flow]);
  if (state.state !== 'cohorte' || state.active_version !== 1 || definition.proposal_id !== proposal.saved.proposal_id
      || definition.definition.estado !== 'publicada' || route.executor !== 'motor' || route.legacy_kind !== null
      || pool.timezone !== A.timezone || created.length !== 1 || created[0].data.state !== 'cohorte') {
    fail('el flujo nativo en cohorte', { state, definition, route, pool, created });
  }
  // Publicarla otra vez es la repeticion de la misma huella: no crea otro.
  const replay = await rpc('operator_publish_flow_change_v1', INSTANCE_ZONE, proposal.saved.proposal_id,
    proposal.recorded.fingerprint, null);
  if (!replay.ok || replay.replayed !== true) fail('la repeticion', replay);
  results.conversacion = { estado: state.state, version: state.active_version, bolsa: pool.pool,
    zona: pool.timezone, ruta: route.executor, evento: created[0].reason_code };
}

// ---------------------------------------------------------------------------
// 2. Lo que no se crea.
// ---------------------------------------------------------------------------
{
  const taken = await asAgent(() => rpc('save_flow_change_draft_v1', INSTANCE, null, null, [bridgeItem()], {},
    BRIDGE.purpose, AE));
  if (taken.ok || taken.reason_code !== 'flow_already_registered') fail('la clave ya registrada', taken);
  const everyone = await errorOf(() => asAgent(() => rpc('save_flow_change_draft_v1', INSTANCE, null, null,
    [bridgeItem('bienvenida_todos', (item) => { item.prender = 'prendido'; })], {}, BRIDGE.purpose, AE)));
  if (everyone?.code !== '22023' || everyone.message !== 'new_flow_must_start_in_cohort') {
    fail('prender distinto de cohorte', everyone?.message ?? 'paso');
  }
  // Un flujo del legado: el carrito de tres toques (A1, A2 y Z) no lo ejecuta el motor nativo, que manda un
  // solo envio (desde 20261028000200 el disparador y el permiso pasan: lo frena la forma). Con un validador
  // que mintiera, la base igual no lo abre.
  const cart = cartDefinition(A.funnel);
  delete cart.topes.bolsa;
  const legacy = await agentProposes([bridgeItem('carrito_nuevo', (item) => {
    item.definicion = { ...cart, flujo: 'carrito_nuevo', topes: { ...cart.topes, bolsa: 'att1-carrito-nuevo' } };
    delete item.ok_del_cliente;
  })], { result: CLEAN });
  const legacyPublished = await publish(legacy);
  if (legacyPublished.ok || legacyPublished.reason_code !== 'motor_nativo:forma'
      || await controlByKey('carrito_nuevo')) {
    fail('un flujo del legado no se crea', legacyPublished);
  }
  // La plantilla a crear deja guardar pero no publicar.
  const toCreate = await agentProposes([bridgeItem('bienvenida_a_crear', (item) => {
    item.definicion.pasos[0].plantilla = { a_crear: 'att1_bienvenida_01' };
  })], { result: CLEAN });
  if (!toCreate.saved.ok) fail('guardar con la plantilla a crear', toCreate);
  const toCreatePublished = await publish(toCreate);
  if (toCreatePublished.ok || toCreatePublished.reason_code !== 'flow_definition_unresolved_value') {
    fail('la plantilla inexistente no publica', toCreatePublished);
  }
  // La primera version de un flujo registrado sin version sigue siendo del operador.
  const registered = await register(A.funnel, 'ficha');
  const first = await errorOf(() => asAgent(() => rpc('save_flow_change_draft_v1', INSTANCE, null, null,
    [versionItem(registered, A.funnel, 'ficha', { ...BRIDGE.items[0].definicion, flujo: 'ficha' })],
    { [registered]: { active_version: null } }, BRIDGE.purpose, AE)));
  if (first?.code !== '55000' || first.message !== 'flow_creation_not_enabled') {
    fail('la v1 de un flujo registrado', first?.message ?? 'paso');
  }
  results.no_se_crea = [taken.reason_code, everyone.message, legacyPublished.reason_code,
    toCreatePublished.reason_code, first.message];
}

// ---------------------------------------------------------------------------
// 3. La cohorte del operador.
// ---------------------------------------------------------------------------
let contactSerial = 0;
const contactFor = async (lead) => {
  if (lead.contact) return lead.contact;
  contactSerial += 1;
  lead.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
    values ($1, $2, $3, 'MX') returning id`, [lead.name, lead.email, lead.phone]), 'contacto').id;
  await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1, 'email', $2, $2, 'hotmart'), ($1, 'phone', $3, $3, 'hotmart')`, [lead.contact, lead.email, lead.phone]);
  return lead.contact;
};
const cohort = (action, phone, flowKey = 'bienvenida') => rpc('operator_flow_cohort_v1', INSTANCE, A.funnel, flowKey,
  action, phone, 'dan', 'el telefono de prueba');
const tester = person('cohorte-1');
{
  await contactFor(tester);
  const added = await cohort('agregar', `+${tester.phone}`);
  const again = await cohort('agregar', tester.phone);
  const removed = await cohort('sacar', tester.phone);
  const readded = await cohort('agregar', tester.phone);
  if (!added.ok || !added.changed || added.members !== 1 || !again.ok || again.changed || again.members !== 1
      || !removed.ok || removed.members !== 0 || !readded.ok || readded.members !== 1) {
    fail('agregar y sacar de la cohorte', { added, again, removed, readded });
  }
  const text = JSON.stringify([added, again, removed, readded]);
  const events = await sql(`select data from public.flow_change_events where flow_id = $1
    and event_type = 'flow_cohort_changed' order by created_at`, [WELCOME.flow]);
  const eventText = JSON.stringify(events);
  if (events.length !== 3 || text.includes(tester.phone) || eventText.includes(tester.phone)
      || text.includes(tester.contact) || eventText.includes(tester.contact)) {
    fail('la cohorte no devuelve ni guarda el telefono ni el contacto', { text, eventText });
  }
  const nobody = await cohort('agregar', '+12025550999');
  if (nobody.ok || nobody.reason_code !== 'flow_cohort_contact_not_found') fail('un telefono de nadie', nobody);
  const twin = person('cohorte-gemela');
  await contactFor(twin);
  await sql(`insert into public.contacts (full_name, email, phone, country_iso) values ('Otra', 'otra@example.test', $1, 'MX')`,
    [twin.phone]);
  const ambiguous = await cohort('agregar', twin.phone);
  if (ambiguous.ok || ambiguous.reason_code !== 'flow_cohort_contact_ambiguous') fail('dos contactos', ambiguous);
  for (let index = 2; index <= 5; index += 1) {
    const lead = person(`cohorte-${index}`);
    await contactFor(lead);
    const ok = await cohort('agregar', lead.phone);
    if (!ok.ok || ok.members !== index) fail(`agregar el ${index}`, ok);
  }
  const sixth = person('cohorte-6');
  await contactFor(sixth);
  const full = await cohort('agregar', sixth.phone);
  if (full.ok || full.reason_code !== 'flow_cohort_full') fail('el tope de 5', full);
  const legacyFlow = await cohort('agregar', tester.phone, 'carrito');
  if (legacyFlow.ok || legacyFlow.reason_code !== 'flow_cohort_needs_native_flow') fail('un flujo del legado', legacyFlow);
  const badPhone = await errorOf(() => cohort('agregar', '123'));
  if (badPhone?.message !== 'flow_cohort_phone_invalid' || String(badPhone.message).includes('123')) {
    fail('un telefono sin forma', badPhone?.message ?? 'paso');
  }
  results.cohorte = { miembros: 5, rechazos: [nobody.reason_code, ambiguous.reason_code, full.reason_code,
    legacyFlow.reason_code, badPhone.message] };
}

// ---------------------------------------------------------------------------
// 4. La admision: la cohorte, y despues para todos.
// ---------------------------------------------------------------------------
const CAPTURED_PURCHASE = fixture('hotmart_cash_payment_approved_att1_20261012.json').eventos[1].payload;
let purchaseSerial = 0;
const purchaseFor = async (lead) => {
  purchaseSerial += 1;
  const now = await dbNow();
  const payload = structuredClone(CAPTURED_PURCHASE);
  payload.id = `nuevo-compra-${lead.email}-${purchaseSerial}`;
  payload.creation_date = now.getTime() - 60_000;
  Object.assign(payload.data.buyer, { name: lead.name, first_name: lead.name.split(' ')[0], email: lead.email,
    checkout_phone: lead.phone });
  payload.data.product.id = A.productId;
  payload.data.purchase.offer.code = lead.offer.offer_code;
  payload.data.purchase.transaction = `HP${String(910000000 + purchaseSerial)}`;
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
const admitNative = async (lead, purchase) => {
  await db.exec('set role service_role');
  let failed = false;
  try {
    return (await db.query('select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)', [
      JSON.stringify(INSTANCE), JSON.stringify({
        fuente: 'hotmart', tipo: 'compra', evento_id: purchase.id, producto_ref: A.funnel, oferta: lead.offer.offer_code,
        contacto_id: lead.contact, momento: purchase.moment, chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
        destino: lead.phone,
      })])).rows;
  } catch (error) {
    failed = true;
    throw error;
  } finally {
    if (!failed) await db.exec('reset role');
  }
};
const outcomeFor = (rows) => one(rows.filter((row) => row.flow_id === WELCOME.flow), 'fila de la bienvenida');
{
  const inCohort = outcomeFor(await admitNative(tester, await purchaseFor(tester)));
  if (inCohort.outcome !== 'motor_arranco' || inCohort.created !== true) fail('la compra de la cohorte', inCohort);
  const stranger = person('fuera-1');
  const outside = outcomeFor(await admitNative(stranger, await purchaseFor(stranger)));
  if (outside.outcome !== 'motor_no_arranca' || outside.reason !== 'fuera_de_cohorte') fail('fuera de la cohorte', outside);
  // Pasarla a todos es otra propuesta, con su si.
  await cycle([stateItem(WELCOME.flow, A.funnel, 'bienvenida', 'prender')], 'la bienvenida para todos');
  const everyone = await control(WELCOME.flow);
  const later = person('fuera-2');
  const afterOn = outcomeFor(await admitNative(later, await purchaseFor(later)));
  if (everyone.state !== 'prendido' || afterOn.outcome !== 'motor_arranco') fail('para todos', { everyone, afterOn });
  results.admision = { cohorte: inCohort.outcome, otro: outside.reason, para_todos: afterOn.outcome };
}

// ---------------------------------------------------------------------------
// 5. La simulacion de un flujo nuevo.
// ---------------------------------------------------------------------------
{
  const counts = await asAgent(async () => JSON.parse(one(await sql(`select public.read_flow_engine_native_trigger_counts_v1(
    $1::jsonb, $2, 'compra', now() - interval '7 days')::text as r`, [JSON.stringify(INSTANCE), A.funnel]), 'conteo').r));
  if (counts.contract !== 'flow-engine-v2' || counts.events !== 3 || counts.people !== 3
      || JSON.stringify(Object.keys(counts).sort())
         !== JSON.stringify(['contract', 'events', 'people', 'product_ref', 'since', 'trigger', 'until'])) {
    fail('el conteo de la simulacion', counts);
  }
  // El carrito cuenta desde 20261028000200 (E-161); el pago rechazado sigue sin disparador nativo.
  const otherTrigger = await errorOf(() => sql(`select public.read_flow_engine_native_trigger_counts_v1($1::jsonb, $2,
    'pago_fallido', now() - interval '1 day')`, [JSON.stringify(INSTANCE), A.funnel]));
  const tooWide = await errorOf(() => sql(`select public.read_flow_engine_native_trigger_counts_v1($1::jsonb, $2,
    'compra', now() - interval '9 days')`, [JSON.stringify(INSTANCE), A.funnel]));
  if (otherTrigger?.message !== 'flow_engine_native_trigger_invalid' || tooWide?.message !== 'flow_engine_history_window_too_wide') {
    fail('las entradas de la simulacion', { otro: otherTrigger?.message, ancha: tooWide?.message });
  }
  results.simulacion = { compras: counts.events, personas: counts.people };
}

// ---------------------------------------------------------------------------
// 6. Lo de hoy sin cambios.
// ---------------------------------------------------------------------------
{
  const store = await asAgent(() => rpc('read_flow_store_v1', INSTANCE));
  const byKey = Object.fromEntries(store.flows.map((flow) => [flow.flow_key, flow]));
  if (byKey.carrito.native !== false || byKey.pago_fallido.native !== false || byKey.bienvenida.native !== true) {
    fail('el almacen dice que flujo es nativo', store.flows.map((flow) => [flow.flow_key, flow.native]));
  }
  const cartBefore = await control(FLOWS.carrito);
  await cycle([versionItem(FLOWS.carrito, A.funnel, 'carrito', cartDefinition(A.funnel, { a2Wait: '3m' }))],
    'una version del carrito');
  const cartAfter = await control(FLOWS.carrito);
  if (cartAfter.active_version !== cartBefore.active_version + 1 || cartAfter.state !== cartBefore.state) {
    fail('una version del carrito se publica como antes', { cartBefore, cartAfter });
  }
  results.hoy = { carrito: `${cartBefore.active_version} -> ${cartAfter.active_version}, ${cartAfter.state}` };
}

console.log(JSON.stringify({ validate_flow_change_new_flow: 'ok', ...results }, null, 2));
