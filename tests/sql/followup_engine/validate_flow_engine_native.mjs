// El motor nativo en la base (migracion 20261024000200, el PR 2 del hito 4),
// con las RPC reales sobre la cadena completa de migraciones.
//
// Contrato: docs/contracts/flow-engine-v2.md (secciones 2, 3, 4, 6 y la 11,
// lo que fija para el PR 2). Diseno: «el hito 4» (2026-10-24, en el vault),
// pieza A.
//
// Prueba, con un rojo a proposito por pieza (el rojo se afirma):
//   0. la migracion: agrega exactamente sus doce funciones y sus tres tablas
//      (con RLS y sin permisos para la API); cambia exactamente diez, cada
//      una con su marca, sin tocar permisos; todas las demas definiciones y
//      su ACL quedan iguales; solo la admision es de service_role; correrla
//      dos veces aborta y otro duenio aborta;
//   1. abrir un flujo nativo: la bolsa, la ruta nativa y el control en
//      cohorte; rojos: una bolsa que se llama como un scope
//      (flow_engine_pool_is_legacy_scope), un scope que se llama como una
//      bolsa (pilot_scope_is_native_pool), la ruta nativa sin abrir
//      (flow_route_native_needs_publication), mover la ruta nativa
//      (flow_route_native_cannot_move, por la funcion y por un UPDATE),
//      cohorte sin ruta nativa (flow_control_cohort_needs_native_route), el
//      tope por persona con una ruta nativa, y sin el parche del control la
//      apertura no pasa;
//   2. un flujo nativo disparado por una compra, de punta a punta: la compra
//      capturada (tests/fixtures/hotmart_cash_payment_approved_att1_20261012.json)
//      admitida y aplicada, la admision nativa con un contacto de la cohorte
//      arranca y con otro da fuera_de_cohorte sin fila; la reevaluacion
//      ejecuta (la compra que lo disparo no lo frena: no tiene binding de
//      Hotmart, y se afirma); el arranque toma el lock de la bolsa
//      nativa y cuenta el dia en su zona (rojo: sin el parche del arranque,
//      flow_engine_pool_unknown); el contexto del paso da la plantilla de la
//      version fijada; la imagen de hoy lo rechaza con
//      flow_engine_needs_engine_image;
//   3. las otras salidas: la repeticion del evento (created = false),
//      ya_en_curso, ya_disparado, flujo_pausado con su fila (y la pausa desde
//      cohorte), sin_flujo, cohorte -> prendido, la entrada fuera de forma y
//      un error interno que deja error_interno:<sqlstate> sin frenar;
//   4. lo de hoy sin cambios: un carrito de la misma persona con el caso
//      nativo abierto arma su propio caso del carrito, y las lecturas de la v1
//      no muestran el flujo nativo (rojo: sin el parche, lo muestran).
//
// Datos sinteticos, los de buildWorld: personas de example.test con
// telefonos 1202555xxxx (rango de ficcion) y el manifiesto de ATT1.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261024000200_flow_engine_native_v1.sql';
const TZ = 'timestamp with time zone';
const results = {};
const fixture = (path) => JSON.parse(readFileSync(join(root, 'tests/fixtures', path), 'utf8'));

// ---------------------------------------------------------------------------
// 0. La forma de la migracion, en una base propia: antes y despues.
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
  'reject_flow_engine_native_mutation()': false,
  '_flow_engine_native_trigger(jsonb)': false,
  '_flow_engine_native_steps(text)': false,
  '_flow_engine_native_run_steps(uuid)': false,
  '_flow_engine_native_unsupported(jsonb)': false,
  '_flow_engine_native_pool(jsonb)': false,
  'validate_flow_engine_pool_insert()': false,
  'reject_pilot_scope_named_like_native_pool()': false,
  '_flow_engine_open_native_flow(uuid,uuid,text)': false,
  [`_flow_engine_admit_native_flow(flow_controls,webhook_events,uuid,text,${TZ},bigint,bigint,text)`]: false,
  'admit_flow_engine_event_v1(jsonb,jsonb)': true,
  [`_flow_engine_authorize_native_tail(uuid,uuid,flow_engine_runs,recovery_cases,flow_controls,${TZ})`]: false,
};
const PATCHED = {
  'validate_flow_engine_route_insert()': ['flow_engine_native_route: begin'],
  'validate_flow_engine_route_update()': ['flow_engine_native_route: begin'],
  'operator_route_flow_v1(jsonb,text,text,text,bigint,text,text,jsonb)': ['flow_engine_native_route: begin'],
  'validate_flow_control_change()': ['flow_engine_native_cohort: begin', 'flow_engine_native_cohort_pause',
    'flow_engine_native_cohort_route: begin'],
  '_flow_engine_compile_policy_core(jsonb,text,integer,text,text)': ['flow_engine_native_shape'],
  [`_flow_engine_authorize_start(uuid,uuid,${TZ},boolean)`]: ['flow_engine_native_start: begin'],
  'flow_engine_execution_context_v1(uuid)': ['flow_engine_native_steps'],
  'read_flow_engine_v1(jsonb,text)': ['flow_engine_native_excluded'],
  'read_flow_engine_routes_v1(jsonb)': ['flow_engine_native_excluded'],
  [`read_flow_engine_history_v1(jsonb,text,${TZ})`]: ['flow_engine_native_excluded', 'flow_engine_native_cases_excluded'],
};
const NEW_TABLES = ['flow_engine_pools', 'flow_cohort_members', 'flow_engine_native_runs'];

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
// Despues van F-036 (20261024000400), que parcha el link del agente, y el
// bridge del motor nativo (20261024000500), que suma lecturas y parcha la
// adopcion: ninguna redefine lo de esta. La foto de antes y despues se saca
// alrededor de esta.
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`${MIGRATION} no esta en la cadena`);
if (JSON.stringify(names.slice(position + 1))
    !== JSON.stringify(['20261024000400_opted_out_reply_checkout_issuance.sql',
      '20261024000500_flow_engine_native_bridge_v1.sql', '20261024000600_flow_change_new_flow_v1.sql',
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
  '20261026000200_flow_engine_native_offer_in_context_v1.sql',
  // La pagina de aprobacion (A′ PR 2): el rol del aprobador, sus sesiones, la cohorte con slack y la ruta
  // nativa firmada por slack. No toca lo que esta prueba mira.
  '20261028000100_flow_change_slack_approver_v1.sql',
  // El carrito nativo (E-161): parcha la admision y el arranque nativos y vuelve a crear el contexto del paso.
  '20261028000200_flow_engine_native_cart_v1.sql',
  // Sumar ofertas a un scope publicado (E-163): solo el trigger de pilot_scope_versions.
  '20261028000300_pilot_scope_offer_append_v1.sql',
  // La compra con nuestro link cuenta siempre (E-169): el helper del cierre por el link y el correlador.
  '20261028000400_purchase_by_our_link_counts_v1.sql',
  // El doble caso del carrito (E-204): la admision nativa y el planificador del carrito, por reemplazo contado.
  '20261030000100_flow_engine_native_legacy_double_case_v1.sql'])) {
  fail(`despues de ${MIGRATION} van solo la de F-036, la del bridge nativo, la del AE que crea flujos y la de sus lecturas`,
    names.slice(position + 1));
}
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
await applyFile(shapeDb, join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of names.slice(0, position)) await applyFile(shapeDb, join(root, 'supabase/migrations', name));
const before = await snapshot(shapeDb);
const tablesBefore = await tableAcl(shapeDb);
const target = join(root, 'supabase/migrations', MIGRATION);
{
  // Otro duenio en una de las funciones parchadas: aborta sin cambiar nada.
  await shapeDb.exec('create role otro_duenio nologin');
  const error = await expectError(shapeDb, 'otro duenio', async () => {
    await shapeDb.exec('alter function public.read_flow_engine_routes_v1(jsonb) owner to otro_duenio');
    await applyFile(shapeDb, target);
  }, { code: '55000', message: 'flow_engine_native_owner_mismatch' });
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
    const row = tablesAfter.get(name);
    if (!row.rls) fail(`${name}: sin RLS`);
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
    { code: '55000', message: 'flow_engine_native_already_applied' });
  results.segunda_corrida = `${error.code}:${error.message}`;
  results.forma = { agregadas: added.length, cambiadas: changed.length, iguales: before.size - changed.length };
}
await shapeDb.close();
// La definicion de antes de cada funcion parchada: para los rojos.
const previous = (signature) => before.get(signature).definition;

// ---------------------------------------------------------------------------
// El mundo: ATT1 (A), un producto B, el carrito y el pago fallido de A en el
// almacen con su ruta en legado (buildWorld).
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, B, FLOWS, INSTANCE, person, cartFor, admit, reserve, startNew, startOld, rpc, register, cycle,
  versionItem, stateItem, control, dbNow, claim, reevaluate } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
// La ruta del carrito de ATT1 en legado, como en produccion: la lee la
// historia de la simulacion y la admision del carrito.
{
  const created = await world.createRoute(A.funnel, 'carrito', 'carrito', A.scope);
  if (!created.ok) fail('la ruta del carrito', created);
}

// Un error esperado sin deshacer lo de antes.
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
// Algo dentro de una transaccion que se deshace (los rojos con la funcion de antes).
const rolledBack = async (run) => {
  await db.exec('begin');
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};

// La activacion (flujo 7 del esquema), con permiso compra, la plantilla de
// utilidad, la etiqueta y horario siempre.
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
const openNative = async (flowId, proposalId) => one(await sql(
  'select public._flow_engine_open_native_flow($1, $2, $3)::text as r', [flowId, proposalId, A.timezone]), 'abrir').r;

// ---------------------------------------------------------------------------
// 1. Abrir un flujo nativo y las guardas.
// ---------------------------------------------------------------------------
const NATIVE = {};
{
  NATIVE.flow = await register(A.funnel, 'activacion');
  const proposal = await publishV1(NATIVE.flow, activation());
  // Rojo: sin el parche del control, cohorte no es una transicion.
  const redControl = await rolledBack(async () => {
    await db.exec(previous('validate_flow_control_change()'));
    return errorOf(() => openNative(NATIVE.flow, proposal));
  });
  if (redControl?.message !== 'flow_control_transition_invalid') {
    fail('sin el parche del control la apertura tenia que fallar', redControl?.message ?? 'paso');
  }
  const opened = JSON.parse(await openNative(NATIVE.flow, proposal));
  if (!opened.ok || opened.state !== 'cohorte' || opened.pool !== 'att1-activacion' || opened.timezone !== A.timezone) {
    fail('abrir la activacion', opened);
  }
  const state = await control(NATIVE.flow);
  const route = one(await sql('select * from public.flow_engine_routes where flow_id = $1', [NATIVE.flow]), 'ruta');
  if (state.state !== 'cohorte' || route.executor !== 'motor' || route.legacy_kind !== null
      || route.legacy_scope_key !== null || Number(route.generation) !== 0) {
    fail('la ruta nativa y el control', { state, route });
  }
  const events = await sql(`select event_type, data ->> 'to_state' as to_state from public.flow_change_events
    where flow_id = $1 and event_type in ('flow_route_created', 'flow_control_changed') order by created_at`, [NATIVE.flow]);
  if (!events.some((e) => e.event_type === 'flow_route_created') || events.at(-1).to_state !== 'cohorte') {
    fail('los eventos de la apertura', events);
  }
  results.abrir = opened;
  NATIVE.pool = opened.pool;
  NATIVE.proposal = proposal;

  // Abrirlo dos veces no pasa.
  const twice = JSON.parse(await openNative(NATIVE.flow, proposal));
  if (twice.ok || twice.reason_code !== 'flow_engine_native_open_invalid') fail('abrir dos veces', twice);

  // ⭐ Rojo: una bolsa que se llama como un scope del legado.
  const scoped = await register(A.funnel, 'bolsa_del_scope');
  const scopedProposal = await publishV1(scoped, activation('bolsa_del_scope', {
    topes: { por_dia: 'sin_tope', total: 'sin_tope', bolsa: A.scope,
      repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } } }));
  const legacyName = await errorOf(() => openNative(scoped, scopedProposal));
  if (legacyName?.code !== '23514' || legacyName.message !== 'flow_engine_pool_is_legacy_scope') {
    fail('una bolsa con el nombre del scope', legacyName ? { code: legacyName.code, message: legacyName.message } : 'paso');
  }
  if ((await sql('select 1 from public.flow_engine_pools where pool = $1', [A.scope])).length !== 0) {
    fail('la bolsa con el nombre del scope quedo');
  }
  // Y el otro lado: un scope con el nombre de la bolsa nativa.
  const nativeName = await errorOf(() => sql(`
    insert into public.pilot_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id, channel, channel_provider,
       channel_account_ref, source, source_event_type, external_product_id, offer_code, purpose, policy_key,
       policy_version, timezone, max_cohort_contacts, max_outbound_request_starts_total,
       max_outbound_request_starts_per_day, audience_mode)
    select $1, 1, 'draft', s.tenant_key, s.chatwoot_account_id, s.chatwoot_inbox_id, s.channel, s.channel_provider,
           s.channel_account_ref, s.source, s.source_event_type, s.external_product_id, s.offer_code, s.purpose,
           s.policy_key, s.policy_version, s.timezone, s.max_cohort_contacts, s.max_outbound_request_starts_total,
           s.max_outbound_request_starts_per_day, s.audience_mode
      from public.pilot_scope_versions s where s.scope_key = $2 and s.version = 1`, [NATIVE.pool, A.scope]));
  if (nativeName?.code !== '23514' || nativeName.message !== 'pilot_scope_is_native_pool') {
    fail('un scope con el nombre de la bolsa', nativeName ? { code: nativeName.code, message: nativeName.message } : 'paso');
  }
  results.bolsas = [legacyName.message, nativeName.message];

  // La ruta nativa no nace sin abrir el flujo.
  const loose = await register(A.funnel, 'ruta_suelta');
  await publishV1(loose, activation('ruta_suelta'));
  const looseRoute = await errorOf(() => sql(`
    insert into public.flow_engine_routes (flow_id, executor, legacy_kind, legacy_scope_key, generation,
      changed_by_ref, change_reason) values ($1, 'motor', null, null, 0, 'dan', 'una ruta suelta')`, [loose]));
  if (looseRoute?.message !== 'flow_route_native_needs_publication') fail('la ruta nativa suelta', looseRoute?.message ?? 'paso');
  // cohorte sin ruta nativa.
  const cohortNoRoute = await errorOf(() => sql(`
    update public.flow_controls set state = 'cohorte', generation = generation + 1, change_kind = 'publicado',
      changed_by_proposal_id = (select proposal_id from public.flow_definitions where flow_id = $1 and version = 1)
     where flow_id = $1`, [loose]));
  if (cohortNoRoute?.message !== 'flow_control_cohort_needs_native_route') {
    fail('cohorte sin ruta nativa', cohortNoRoute?.message ?? 'paso');
  }
  // Mover la ruta nativa, por la funcion y por un UPDATE.
  const moved = await rpc('operator_route_flow_v1', INSTANCE, A.funnel, 'activacion', 'legado', 0, 'dan', 'volver al legado', null);
  if (moved.ok || moved.reason_code !== 'flow_route_native_cannot_move') fail('mover la ruta nativa', moved);
  const movedByHand = await errorOf(() => sql(`update public.flow_engine_routes set executor = 'legado',
    generation = generation + 1 where flow_id = $1`, [NATIVE.flow]));
  if (movedByHand?.message !== 'flow_route_native_cannot_move') fail('mover la ruta nativa a mano', movedByHand?.message ?? 'paso');
  // El tope por persona no se carga con una ruta nativa (contrato, seccion 2).
  const cap = await errorOf(() => sql(`
    insert into public.pilot_proactive_contact_caps (tenant_key, max_request_starts, request_window, approved_by)
    values ($1, 2, interval '24 hours', 'dan')`, [A.tenant]));
  if (cap?.code !== '55000' || cap.message !== 'pilot_proactive_contact_cap_with_engine_route') {
    fail('el tope por persona con una ruta nativa', cap ? { code: cap.code, message: cap.message } : 'paso');
  }
  results.guardas = { ruta_suelta: looseRoute.message, cohorte: cohortNoRoute.message, mover: moved.reason_code };
}

// ---------------------------------------------------------------------------
// 2. Una compra de punta a punta.
// ---------------------------------------------------------------------------
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
// La compra capturada para la persona: admitida como la admite el bridge y
// aplicada como la aplica el worker (corta lo abierto).
const purchaseFor = async (lead, P = A) => {
  purchaseSerial += 1;
  const now = await dbNow();
  const payload = structuredClone(CAPTURED_PURCHASE);
  payload.id = `nativo-compra-${lead.email}-${purchaseSerial}`;
  payload.creation_date = now.getTime() - 60_000;
  Object.assign(payload.data.buyer, { name: lead.name, first_name: lead.name.split(' ')[0], email: lead.email,
    checkout_phone: lead.phone });
  payload.data.product.id = P.productId;
  payload.data.purchase.offer.code = lead.offer.offer_code;
  payload.data.purchase.transaction = `HP${String(900000000 + purchaseSerial)}`;
  payload.data.purchase.approved_date = now.getTime() - 60_000;
  payload.data.purchase.order_date = now.getTime() - 120_000;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [P.tenant, P.funnel, P.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone]), 'compra');
  if (admitted.outcome !== 'inserted') fail('admitir la compra', admitted);
  await contactFor(lead);
  const applied = one(await sql('select * from public.apply_hotmart_purchase_approved($1,$2,$3,$4,$5,$6,$7::timestamptz)',
    [admitted.webhook_event_id, lead.email, lead.phone, String(P.productId), lead.offer.offer_code,
      payload.data.purchase.transaction, new Date(payload.data.purchase.approved_date).toISOString()]), 'aplicar');
  return { id: admitted.webhook_event_id, moment: new Date(payload.creation_date).toISOString(), applied: applied.outcome };
};
const flowEvent = (lead, purchase, extra = {}) => ({
  fuente: 'hotmart', tipo: 'compra', evento_id: purchase.id, producto_ref: A.funnel, oferta: lead.offer.offer_code,
  contacto_id: lead.contact, momento: purchase.moment, chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
  destino: lead.phone, ...extra,
});
const ADMIT_NATIVE = 'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)';
const admitNative = async (event, instance = INSTANCE) => {
  await db.exec('set role service_role');
  let failed = false;
  try {
    return (await db.query(ADMIT_NATIVE, [JSON.stringify(instance), JSON.stringify(event)])).rows;
  } catch (error) {
    failed = true;
    throw error;
  } finally {
    // Con la transaccion abortada no se puede: el rollback de errorOf la deshace.
    if (!failed) await db.exec('reset role');
  }
};
const rowFor = (rows, flowId) => one(rows.filter((row) => row.flow_id === flowId), 'fila del flujo');
const rejections = async (flowId) => sql(`select reason, phase, count from public.flow_engine_start_rejections
  where flow_id = $1 order by reason`, [flowId]);
const addToCohort = async (lead) => sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref)
  values ($1, $2, 'dan')`, [NATIVE.flow, await contactFor(lead)]);

{
  const buyer = person('compradora-1');
  await contactFor(buyer);
  await addToCohort(buyer);
  const purchase = await purchaseFor(buyer);
  const rows = await admitNative(flowEvent(buyer, purchase));
  const started = rowFor(rows, NATIVE.flow);
  if (started.outcome !== 'motor_arranco' || started.created !== true || !started.recovery_case_id) {
    fail('la compra de la cohorte arranca', rows);
  }
  buyer.plan = { recovery_case_id: started.recovery_case_id, scheduled_action_id: started.scheduled_action_id };
  const run = one(await sql('select * from public.flow_engine_runs where recovery_case_id = $1', [started.recovery_case_id]), 'recorrido');
  const native = one(await sql('select * from public.flow_engine_native_runs where recovery_case_id = $1',
    [started.recovery_case_id]), 'recorrido nativo');
  const kase = one(await sql('select * from public.recovery_cases where id = $1', [started.recovery_case_id]), 'caso');
  const sequence = one(await sql('select * from public.followup_sequences where recovery_case_id = $1',
    [started.recovery_case_id]), 'secuencia');
  const action = one(await sql('select * from public.scheduled_actions where id = $1', [started.scheduled_action_id]), 'accion');
  if (run.legacy_kind !== null || run.legacy_scope_key !== null || run.pool !== NATIVE.pool || run.legacy_dispatch_ok
      || native.trigger_event !== 'compra' || native.offer_code !== buyer.offer.offer_code
      || kase.offer_code !== null || kase.abandonment_event_id !== purchase.id || kase.policy_key !== `flujo:${NATIVE.flow}`
      || sequence.reason !== 'recovery' || action.step_key !== 'first_contact' || action.action_type !== 'first_contact_review') {
    fail('el recorrido nativo', { run, native, kase, sequence, action });
  }
  if ((await sql('select 1 from public.pilot_recovery_case_bindings where recovery_case_id = $1',
    [started.recovery_case_id])).length !== 0) fail('un caso nativo no tiene binding del piloto');
  const policy = one(await sql('select * from public.followup_policy_versions where policy_key = $1', [kase.policy_key]), 'politica');
  if (policy.timezone !== A.timezone || policy.max_automatic_messages !== 1 || policy.steps.length !== 1) {
    fail('la politica compilada nativa', policy);
  }
  results.compra = { compra_aplicada: purchase.applied, salida: started.outcome, bolsa: run.pool };

  // La repeticion del mismo evento devuelve el mismo caso.
  const replay = rowFor(await admitNative(flowEvent(buyer, purchase)), NATIVE.flow);
  if (replay.outcome !== 'motor_arranco' || replay.created !== false || replay.recovery_case_id !== started.recovery_case_id) {
    fail('la repeticion del evento', replay);
  }

  // Fuera de la cohorte: no arranca y no deja fila.
  const stranger = person('compradora-fuera');
  const strangerPurchase = await purchaseFor(stranger);
  const outside = rowFor(await admitNative(flowEvent(stranger, strangerPurchase)), NATIVE.flow);
  if (outside.outcome !== 'motor_no_arranca' || outside.reason !== 'fuera_de_cohorte') fail('fuera de la cohorte', outside);
  if ((await rejections(NATIVE.flow)).some((row) => row.reason === 'fuera_de_cohorte')) fail('fuera de la cohorte dejo fila');

  // La reevaluacion ejecuta: la compra que lo disparo no lo frena, porque
  // _portable_case_purchase_stop_reason busca el binding de Hotmart del evento
  // del caso y una compra aprobada no tiene. Medido, no supuesto: si algun dia
  // la compra lleva binding, esto da rojo.
  const stop = one(await sql('select public._portable_case_purchase_stop_reason($1) as r',
    [started.recovery_case_id]), 'freno').r;
  if (stop !== null || (await sql('select 1 from public.commercial_ally_hotmart_event_bindings where webhook_event_id = $1',
    [purchase.id])).length !== 0) {
    fail('la compra que dispara el flujo nativo lo frenaria', stop);
  }
  const reserved = await reserve(buyer, started.scheduled_action_id);
  if (reserved.attempt.mode !== 'approved_template') fail('el intento', reserved.attempt);

  // El arranque. Rojo: sin el parche, la bolsa nativa no existe para el legado.
  const redStart = await rolledBack(async () => {
    await db.exec(previous(`_flow_engine_authorize_start(uuid,uuid,${TZ},boolean)`));
    return startNew(reserved);
  });
  if (redStart.outcome !== 'rechazado' || redStart.reason !== 'flow_engine_pool_unknown') {
    fail('sin el parche el arranque tenia que rechazar', redStart);
  }
  // La imagen de hoy no despacha un flujo nativo.
  const oldImage = await errorOf(() => startOld(reserved));
  if (oldImage?.message !== 'pilot_request_start_rejected' || oldImage.detail !== 'flow_engine_needs_engine_image') {
    fail('la imagen de hoy', oldImage ? { message: oldImage.message, detail: oldImage.detail } : 'paso');
  }
  const startedSend = await startNew(reserved);
  if (startedSend.outcome !== 'request_started' || !startedSend.flow_authorization_id) fail('el arranque nativo', startedSend);
  const authorization = one(await sql('select * from public.flow_engine_request_authorizations where id = $1',
    [startedSend.flow_authorization_id]), 'autorizacion');
  const localDate = one(await sql(`select (clock_timestamp() at time zone $1)::date::text as d`, [A.timezone]), 'dia').d;
  if (authorization.pool !== NATIVE.pool || authorization.legacy_scope_key !== null
      || authorization.local_budget_date.toISOString?.().slice(0, 10) !== localDate
        && String(authorization.local_budget_date).slice(0, 10) !== localDate) {
    fail('la autorizacion nativa', { authorization, localDate });
  }
  const context = one(await sql('select * from public.flow_engine_execution_context_v1($1)', [started.scheduled_action_id]), 'contexto');
  if (context.template_name !== ACTIVATION_TEMPLATE || context.send_ordinal !== 1
      || JSON.stringify(context.template_variables) !== JSON.stringify(['nombre', 'producto'])) {
    fail('el contexto del paso', context);
  }
  results.arranque = { salida: startedSend.outcome, bolsa: authorization.pool, plantilla: context.template_name };
  NATIVE.buyer = buyer;
}

// ---------------------------------------------------------------------------
// 3. Las otras salidas.
// ---------------------------------------------------------------------------
{
  const { buyer } = NATIVE;
  // ya_en_curso: otra compra con el recorrido abierto.
  const second = await purchaseFor(buyer);
  const inFlight = rowFor(await admitNative(flowEvent(buyer, second)), NATIVE.flow);
  if (inFlight.outcome !== 'motor_no_arranca' || inFlight.reason !== 'ya_en_curso') fail('ya_en_curso', inFlight);
  // ya_disparado: con el recorrido cerrado, otra_despues_de nunca.
  await sql(`update public.recovery_cases set status = 'cancelled', closed_at = now(), version = version + 1 where id = $1`,
    [buyer.plan.recovery_case_id]);
  const third = await purchaseFor(buyer);
  const fired = rowFor(await admitNative(flowEvent(buyer, third)), NATIVE.flow);
  if (fired.outcome !== 'motor_no_arranca' || fired.reason !== 'ya_disparado') fail('ya_disparado', fired);

  // sin_flujo: la compra de un producto sin flujos nativos.
  const buyerB = person('compradora-b', B);
  const purchaseB = await purchaseFor(buyerB, B);
  const none = one(await admitNative(flowEvent(buyerB, purchaseB, { producto_ref: B.funnel })), 'sin flujo');
  if (none.outcome !== 'sin_flujo' || none.flow_id !== null) fail('sin_flujo', none);

  // La entrada fuera de forma.
  for (const [label, event] of [
    ['clave de mas', { ...flowEvent(buyer, third), extra: 1 }],
    ['sin destino', (({ destino, ...rest }) => rest)(flowEvent(buyer, third))],
    ['fuente', flowEvent(buyer, third, { fuente: 'ghl' })],
    ['chatwoot', flowEvent(buyer, third, { chatwoot: { account_id: '1', inbox_id: 2 } })],
    ['destino con mas', flowEvent(buyer, third, { destino: `+${buyer.phone}` })],
  ]) {
    const error = await errorOf(() => admitNative(event));
    if (error?.code !== '22023' || error.message !== 'invalid_flow_engine_event') {
      fail(`entrada fuera de forma: ${label}`, error ? { code: error.code, message: error.message } : 'paso');
    }
  }
  // El evento de otro producto no entra.
  const wrongProduct = await errorOf(() => admitNative(flowEvent(buyer, third, { producto_ref: B.funnel })));
  if (wrongProduct?.code !== '22023') fail('el evento de otro producto', wrongProduct?.message ?? 'paso');

  // Pausar desde cohorte, y la fila de flujo_pausado.
  const generation = (await control(NATIVE.flow)).generation;
  const paused = await rpc('operator_pause_flow_v1', INSTANCE, A.funnel, 'activacion', generation, 'dan', 'pausa desde cohorte');
  if (!paused.ok || !paused.changed || (await control(NATIVE.flow)).state !== 'pausado') fail('pausar desde cohorte', paused);
  const pausedBuyer = person('compradora-pausa');
  await addToCohort(pausedBuyer);
  const pausedPurchase = await purchaseFor(pausedBuyer);
  const pausedRow = rowFor(await admitNative(flowEvent(pausedBuyer, pausedPurchase)), NATIVE.flow);
  if (pausedRow.outcome !== 'motor_no_arranca' || pausedRow.reason !== 'flujo_pausado') fail('flujo_pausado', pausedRow);
  if (!(await rejections(NATIVE.flow)).some((row) => row.reason === 'flujo_pausado' && row.phase === 'admision')) {
    fail('flujo_pausado sin su fila');
  }
  // Prender (de pausado a prendido, con su propuesta): ahora entra cualquiera.
  await cycle([stateItem(NATIVE.flow, A.funnel, 'activacion', 'prender')], 'prender la activacion');
  if ((await control(NATIVE.flow)).state !== 'prendido') fail('prender la activacion');
  const anyone = person('compradora-todos');
  const anyonePurchase = await purchaseFor(anyone);
  const open = rowFor(await admitNative(flowEvent(anyone, anyonePurchase)), NATIVE.flow);
  if (open.outcome !== 'motor_arranco' || !open.created) fail('prendida para todos', open);
  NATIVE.anyone = anyone;
  anyone.plan = { recovery_case_id: open.recovery_case_id };

  // Un error interno de un flujo no frena a los otros: un segundo flujo
  // nativo cuya bolsa se borro por debajo (sin el disparador) da error_interno.
  const other = await register(A.funnel, 'segunda_bienvenida');
  const otherProposal = await publishV1(other, activation('segunda_bienvenida'));
  const otherOpened = JSON.parse(await openNative(other, otherProposal));
  if (!otherOpened.ok) fail('abrir el segundo flujo', otherOpened);
  const both = await rolledBack(async () => {
    await db.exec(`create or replace function public._flow_engine_native_unsupported(p_definition jsonb)
      returns text language plpgsql as $f$ begin
        if p_definition ->> 'flujo' = 'segunda_bienvenida' then perform 1 / 0; end if; return null; end $f$`);
    const lead = person('compradora-dos-flujos');
    const lp = await purchaseFor(lead);
    return admitNative(flowEvent(lead, lp));
  });
  const otherRow = rowFor(both, other);
  const mainRow = rowFor(both, NATIVE.flow);
  if (otherRow.outcome !== 'motor_no_arranca' || otherRow.reason !== 'error_interno:22012'
      || mainRow.outcome !== 'motor_arranco') {
    fail('un error interno de un flujo', both);
  }
  // Un evento abre un solo caso: con los dos flujos, el segundo no arranca.
  const pair = person('compradora-par');
  const pairPurchase = await purchaseFor(pair);
  await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
    [other, pair.contact]);
  const pairRows = await admitNative(flowEvent(pair, pairPurchase));
  const outcomes = pairRows.map((row) => `${row.outcome}:${row.reason ?? ''}`).sort();
  if (JSON.stringify(outcomes) !== JSON.stringify(['motor_arranco:', 'motor_no_arranca:evento_con_otro_flujo'])) {
    fail('un evento, un caso', pairRows);
  }
  results.salidas = { ya_en_curso: inFlight.reason, ya_disparado: fired.reason, sin_flujo: none.outcome,
    pausado: pausedRow.reason, error_interno: otherRow.reason, un_evento_un_caso: outcomes };
}

// ---------------------------------------------------------------------------
// 4. Lo de hoy sin cambios.
// ---------------------------------------------------------------------------
{
  // Un carrito de la persona con el caso nativo abierto arma su propio caso
  // del carrito (la oferta del caso nativo es nula a proposito).
  const { anyone } = NATIVE;
  const nativeCase = anyone.plan.recovery_case_id;
  const lead = await cartFor(anyone);
  const cart = await admit(lead);
  if (cart.outcome !== 'legado' || cart.created !== true || cart.recovery_case_id === nativeCase) {
    fail('el carrito con un caso nativo abierto', cart);
  }
  const cartCase = one(await sql('select * from public.recovery_cases where id = $1', [cart.recovery_case_id]), 'caso del carrito');
  if (cartCase.policy_key === `flujo:${NATIVE.flow}` || cartCase.abandonment_event_id === null) fail('el caso del carrito', cartCase);
  results.carrito_con_caso_nativo = cart.outcome;

  // El orden de la compra (choque 2 del diseno): primero la compra frena la
  // recuperacion y despues arranca el flujo nativo. Una persona con su
  // carrito abierto compra: el caso del carrito se gana y la bienvenida arranca.
  const shopper = person('compradora-con-carrito');
  await contactFor(shopper);
  const shopperCart = await admit(await cartFor(shopper));
  if (shopperCart.outcome !== 'legado' || !shopperCart.created) fail('el carrito de la compradora', shopperCart);
  const shopperPurchase = await purchaseFor(shopper);
  const cartAfter = one(await sql('select status from public.recovery_cases where id = $1',
    [shopperCart.recovery_case_id]), 'carrito despues de la compra');
  if (shopperPurchase.applied !== 'applied' || cartAfter.status !== 'won') {
    fail('la compra corta el carrito', { applied: shopperPurchase.applied, status: cartAfter.status });
  }
  const welcome = rowFor(await admitNative(flowEvent(shopper, shopperPurchase)), NATIVE.flow);
  if (welcome.outcome !== 'motor_arranco' || !welcome.created || welcome.recovery_case_id === shopperCart.recovery_case_id) {
    fail('la bienvenida despues de cortar el carrito', welcome);
  }
  results.compra_corta_y_arranca = { carrito: cartAfter.status, bienvenida: welcome.outcome };

  // Las lecturas de la v1 no muestran lo nativo. Rojo: sin el parche, si.
  const read = async (fn, ...args) => {
    await db.exec('set role service_role');
    try {
      const marks = args.map((_, i) => `$${i + 1}`).join(', ');
      return one((await db.query(`select public.${fn}(${marks})::text as r`,
        args.map((a) => (typeof a === 'object' ? JSON.stringify(a) : a)))).rows, fn).r;
    } finally {
      await db.exec('reset role');
    }
  };
  const flows = JSON.parse(await read('read_flow_engine_v1', INSTANCE));
  const routes = JSON.parse(await read('read_flow_engine_routes_v1', INSTANCE));
  const keys = flows.flows.map((flow) => flow.flow_key);
  if (keys.some((key) => ['activacion', 'segunda_bienvenida'].includes(key))
      || routes.routes.some((r) => r.legacy_kind === null || r.flow_state === 'cohorte')
      || flows.flows.some((flow) => flow.state === 'cohorte')) {
    fail('las lecturas de la v1 muestran lo nativo', { keys, routes });
  }
  for (const flowKey of ['carrito', 'pago_fallido']) {
    if (!keys.includes(flowKey)) fail(`read_flow_engine_v1 perdio ${flowKey}`, keys);
  }
  const since = new Date((await dbNow()).getTime() - 2 * 86_400_000).toISOString();
  const history = await read('read_flow_engine_history_v1', INSTANCE, A.funnel, since);
  if (history.includes(nativeCase)) fail('la historia muestra un caso nativo');
  const red = await rolledBack(async () => {
    await db.exec(previous('read_flow_engine_routes_v1(jsonb)'));
    await db.exec(previous(`read_flow_engine_history_v1(jsonb,text,${TZ})`));
    await db.exec(previous('read_flow_engine_v1(jsonb,text)'));
    return {
      routes: JSON.parse(await read('read_flow_engine_routes_v1', INSTANCE)),
      history: await read('read_flow_engine_history_v1', INSTANCE, A.funnel, since),
      flows: JSON.parse(await read('read_flow_engine_v1', INSTANCE)),
    };
  });
  if (!red.routes.routes.some((r) => r.legacy_kind === null) || !red.history.includes(nativeCase)
      || !red.flows.flows.some((flow) => flow.flow_key === 'activacion')) {
    fail('sin el parche las lecturas tenian que mostrar lo nativo');
  }
  results.lecturas = { flujos_v1: keys, rutas_v1: routes.routes.length };
}

console.log(JSON.stringify({ validate_flow_engine_native: 'ok', ...results }, null, 2));
