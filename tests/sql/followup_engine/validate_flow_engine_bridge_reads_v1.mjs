// Lo que el bridge de la T2.4 lee y marca del motor de flujos (migracion
// 20261022000200), con las RPC reales sobre la cadena completa de migraciones.
//
// Contrato: docs/contracts/flow-engine-v1.md, seccion 7.4. Plan: «T2.4: el plan
// medido contra el codigo» (2026-10-22, en el vault), commit 1.
//
// Prueba:
//   0. la migracion: las tres funciones existen, solo service_role las
//      ejecuta (anon y authenticated dan 42501), y correrla otra vez aborta;
//   1. read_flow_engine_routes_v1: las rutas de la instancia con su forma, y
//      sin definition ni in_flight; otra instancia no ve nada;
//   2. read_flow_engine_divergences_v1: una divergencia transitoria (la de
//      p_force_legacy con la ruta en legado) sale con su forma, sin ids de
//      evento, y unnotified la cuenta; p_limit fuera de 1..50 es 22023;
//   3. mark_flow_engine_divergence_notified_v1: la primera marca da true, la
//      segunda false, y unnotified baja a 0; la fila sigue (no se borra).
//
// El rojo a proposito: sin la migracion, la primera llamada da 42883 (la
// funcion no existe).
//
// Con --emit-bridge-fixtures [--check], escribe (o compara)
// tests/fixtures/flow_engine/bridge_rpc_rows.json desde la cadena real: una
// fila por cada outcome de la admision y del arranque, y el contexto de A1, A2
// y Z. Lo lee tests/test_flow_engine_supabase_client.py: el doble del cliente
// del bridge no se escribe a mano. Los uuid y las horas se normalizan (por
// orden de aparicion) para que el archivo no cambie entre corridas.
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

import { buildWorld, expectError, fail, one, root } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261022000200_flow_engine_bridge_reads_v1.sql';
const FIXTURE = join(root, 'tests/fixtures/flow_engine/bridge_rpc_rows.json');
const READS = [
  'read_flow_engine_routes_v1(jsonb)',
  'read_flow_engine_divergences_v1(jsonb,boolean,integer)',
  'mark_flow_engine_divergence_notified_v1(uuid,timestamp with time zone)',
];

const canonical = (value) => JSON.stringify(Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))));

const asRole = async (db, role, run) => {
  await db.exec('begin');
  await db.exec(`set local role ${role}`);
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};

const validateReads = async (world) => {
  const { db, A, SCOPE, INSTANCE, cartFor, admit, createRoute } = world;
  const results = {};
  const rpcJson = async (sql, params) => JSON.parse(one((await db.query(sql, params)).rows, sql).r);
  const ROUTES = 'select public.read_flow_engine_routes_v1($1::jsonb)::text as r';
  const DIVERGENCES = 'select public.read_flow_engine_divergences_v1($1::jsonb, $2, $3)::text as r';
  const MARK = 'select public.mark_flow_engine_divergence_notified_v1($1, $2) as r';

  // 0. La migracion y los permisos.
  for (const signature of READS) {
    const row = one((await db.query(`
      select has_function_privilege('service_role', p.oid, 'execute') as service_x,
             has_function_privilege('anon', p.oid, 'execute') as anon_x,
             has_function_privilege('authenticated', p.oid, 'execute') as auth_x,
             p.prosecdef as definer, coalesce(array_to_string(p.proconfig, ','), '') as config
        from pg_proc p where p.oid = to_regprocedure($1)`, [`public.${signature}`])).rows, signature);
    if (!row.service_x || row.anon_x || row.auth_x || !row.definer || !row.config.startsWith('search_path=')) {
      fail(`permisos de ${signature}`, row);
    }
  }
  for (const role of ['anon', 'authenticated']) {
    await expectError(db, `${role} no lee las rutas`, async () => {
      await db.exec(`set local role ${role}`);
      await db.query(ROUTES, [JSON.stringify(INSTANCE)]);
    }, { code: '42501' });
  }
  await expectError(db, 'correr la migracion dos veces', () => db.exec(
    readFileSync(join(root, 'supabase/migrations', MIGRATION), 'utf8')),
  { code: '55000', message: 'flow_engine_bridge_reads_already_applied' });
  results.migracion = 'tres funciones, solo service_role, la segunda corrida aborta';

  // 1. Las rutas.
  const empty = await asRole(db, 'service_role', () => rpcJson(ROUTES, [JSON.stringify(INSTANCE)]));
  if (JSON.stringify(empty) !== JSON.stringify({ routes: [] })) fail('sin rutas', empty);
  const created = await createRoute(A.funnel, 'carrito', 'carrito', SCOPE);
  if (!created.ok) fail('crear la ruta', created);
  const routes = await asRole(db, 'service_role', () => rpcJson(ROUTES, [JSON.stringify(INSTANCE)]));
  const expected = { product_ref: A.funnel, flow_key: 'carrito', executor: 'legado', legacy_kind: 'carrito',
    legacy_scope_key: SCOPE, generation: 0, flow_state: 'prendido' };
  if (routes.routes.length !== 1 || canonical(routes.routes[0]) !== canonical(expected)) {
    fail('la ruta del carrito', routes);
  }
  if (JSON.stringify(routes).includes('definition') || JSON.stringify(routes).includes('in_flight')) {
    fail('la lectura de rutas trae definiciones o en curso', routes);
  }
  await expectError(db, 'una instancia sin binding', () => db.query(ROUTES,
    [JSON.stringify({ tenant_ref: 'otro-tenant', ally_ref: A.ally })]),
  { code: '22023', message: 'flow_store_instance_not_bound' });
  results.rutas = expected;

  // 2. Las divergencias.
  const none = await asRole(db, 'service_role', () => rpcJson(DIVERGENCES, [JSON.stringify(INSTANCE), true, 20]));
  if (JSON.stringify(none) !== JSON.stringify({ unnotified: 0, divergences: [] })) fail('sin divergencias', none);
  const forced = await admit(await cartFor('lectura-del-bridge'), true);
  if (forced.outcome !== 'legado_por_divergencia' || forced.divergence_first !== true) fail('la divergencia', forced);
  const read = await asRole(db, 'service_role', () => rpcJson(DIVERGENCES, [JSON.stringify(INSTANCE), true, 20]));
  const divergence = read.divergences[0];
  const keys = ['id', 'kind', 'reason', 'flow_key', 'product_ref', 'version', 'count', 'first_at', 'last_at'];
  if (read.unnotified !== 1 || read.divergences.length !== 1 || divergence.id !== forced.divergence_id
      || JSON.stringify(Object.keys(divergence).sort()) !== JSON.stringify([...keys].sort())
      || divergence.kind !== 'transitorio' || divergence.reason !== 'transitorio:forzado_al_legado'
      || divergence.flow_key !== 'carrito' || divergence.product_ref !== A.funnel || divergence.count !== 1
      || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$/.test(divergence.first_at)) {
    fail('la forma de la divergencia', read);
  }
  for (const limit of [0, 51, null]) {
    await expectError(db, `p_limit ${limit}`, () => db.query(DIVERGENCES, [JSON.stringify(INSTANCE), true, limit]),
      { code: '22023', message: 'flow_engine_divergences_parameters_invalid' });
  }
  await expectError(db, 'p_only_unnotified nulo', () => db.query(DIVERGENCES, [JSON.stringify(INSTANCE), null, 20]),
    { code: '22023', message: 'flow_engine_divergences_parameters_invalid' });
  results.divergencias = { unnotified: 1, kind: divergence.kind, reason: divergence.reason };

  // 3. La marca. Se confirma (no se deshace) para ver el efecto despues.
  const now = new Date().toISOString();
  await db.exec('begin');
  await db.exec('set local role service_role');
  const first = one((await db.query(MARK, [divergence.id, now])).rows, 'primera marca').r;
  const second = one((await db.query(MARK, [divergence.id, now])).rows, 'segunda marca').r;
  const missing = one((await db.query(MARK, ['00000000-0000-4000-8000-000000000000', now])).rows, 'otra').r;
  await db.exec('commit');
  if (first !== true || second !== false || missing !== false) fail('la marca', { first, second, missing });
  await expectError(db, 'la marca sin fecha', () => db.query(MARK, [divergence.id, null]),
    { code: '22023', message: 'flow_engine_divergence_mark_parameters_invalid' });
  const afterMark = await asRole(db, 'service_role', () => rpcJson(DIVERGENCES, [JSON.stringify(INSTANCE), true, 20]));
  const all = await asRole(db, 'service_role', () => rpcJson(DIVERGENCES, [JSON.stringify(INSTANCE), false, 20]));
  if (afterMark.unnotified !== 0 || afterMark.divergences.length !== 0 || all.divergences.length !== 1) {
    fail('despues de la marca', { afterMark, all });
  }
  const stored = one((await db.query('select notified_at from public.flow_engine_divergences where id = $1',
    [divergence.id])).rows, 'la fila marcada');
  if (stored.notified_at === null) fail('la marca no quedo');
  results.marca = { primera: first, segunda: second, otra: missing };
  return results;
};

// Una fila como la da PostgREST: las horas en texto, y todo uuid y toda hora
// reemplazados por un valor fijo por orden de aparicion.
const normalizer = () => {
  const ids = new Map();
  const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g;
  const fixedId = (id) => {
    if (!ids.has(id)) ids.set(id, `00000000-0000-4000-8000-${String(ids.size + 1).padStart(12, '0')}`);
    return ids.get(id);
  };
  const value = (raw) => {
    if (raw instanceof Date) return '2026-10-22T15:00:00.000000+00:00';
    if (typeof raw === 'bigint') return Number(raw);
    if (typeof raw === 'string') {
      return raw.replace(UUID, fixedId).replace(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$/, '2026-10-22T15:00:00.000000Z');
    }
    if (Array.isArray(raw)) return raw.map(value);
    if (raw !== null && typeof raw === 'object') {
      return Object.fromEntries(Object.entries(raw).map(([key, item]) => [key, value(item)]));
    }
    return raw;
  };
  return value;
};

export const emitBridgeFixtures = async (world) => {
  const { db, A, SCOPE, FLOWS, cartFor, admit, createRoute, route, reserve, startNew, accept, stepOf,
    pauseFlow, setFlow, setState, dbNow } = world;
  const rows = { admission: {}, request_start: {}, context: {}, reads: {} };
  const norm = normalizer();
  const claimCoupons = async () => db.query(`
    select * from public.claim_due_recovery_coupons_v1(
      $1::timestamptz,'fixture-cupones',60,'ATT','45 minutes'::interval,'24 hours'::interval,0.1::numeric,100)
  `, [await dbNow()]);
  const contextOf = async (actionId, label) => one((await db.query(
    'select * from public.flow_engine_execution_context_v1($1)', [actionId])).rows, label);
  const keep = (group, key, row) => {
    if (rows[group][key] !== undefined) fail(`dos filas para ${group}.${key}`);
    rows[group][key] = norm(row);
  };
  const motorLead = async (label) => {
    const lead = await cartFor(label);
    const row = await admit(lead);
    if (row.outcome !== 'motor_arranco') fail(`${label}: la admision`, row);
    return { lead, row };
  };

  // La admision con la ruta en legado: legado, y forzada, legado_por_divergencia.
  if (!(await createRoute(A.funnel, 'carrito', 'carrito', SCOPE)).ok) fail('crear la ruta');
  const legacy = await admit(await cartFor('fixture-legado'));
  if (legacy.outcome !== 'legado') fail('legado', legacy);
  keep('admission', 'legado', legacy);
  const diverged = await admit(await cartFor('fixture-divergencia'), true);
  if (diverged.outcome !== 'legado_por_divergencia') fail('legado_por_divergencia', diverged);
  keep('admission', 'legado_por_divergencia', diverged);
  const readJson = async (sql, params, label) => JSON.parse(one((await db.query(sql, params)).rows, label).r);
  keep('reads', 'routes', await readJson('select public.read_flow_engine_routes_v1($1::jsonb)::text as r',
    [JSON.stringify(world.INSTANCE)], 'rutas'));
  keep('reads', 'divergences', await readJson(
    'select public.read_flow_engine_divergences_v1($1::jsonb, true, 20)::text as r',
    [JSON.stringify(world.INSTANCE)], 'divergencias'));
  // Con la ruta en motor: motor_arranco, motor_no_arranca y legado_rechazo.
  if (!(await route(A.funnel, 'carrito', FLOWS.carrito, 'motor')).ok) fail('el carrito a motor');
  const started = await motorLead('fixture-motor');
  keep('admission', 'motor_arranco', started.row);
  const noForm = await admit(await cartFor('fixture-sin-formulario', { form: false }));
  if (noForm.outcome !== 'motor_no_arranca' || noForm.reason !== 'no_entra:permiso') fail('motor_no_arranca', noForm);
  keep('admission', 'motor_no_arranca', noForm);
  await setState(SCOPE, 1, 'paused');
  const refused = await admit(await cartFor('fixture-rechazo'), true);
  if (refused.outcome !== 'legado_rechazo') fail('legado_rechazo', refused);
  keep('admission', 'legado_rechazo', refused);
  await setState(SCOPE, 1, 'armed');

  // A1, A2 y Z del caso del motor: el contexto, y el arranque request_started.
  await claimCoupons();
  const { lead } = started;
  const a1 = await reserve(lead, lead.plan.scheduled_action_id);
  keep('context', 'first_contact', await contextOf(a1.actionId, 'contexto de A1'));
  const a1Start = one((await db.query(world.MARK_ENGINE,
    [a1.actionId, a1.attempt.id, a1.worker, a1.lease, await dbNow()])).rows, 'arranque de A1');
  if (a1Start.outcome !== 'request_started') fail('request_started', a1Start);
  keep('request_start', 'request_started', {
    ...a1Start, _call: { action_id: a1.actionId, attempt_id: a1.attempt.id, lease_generation: a1.lease } });
  await accept(lead, a1);
  const a2 = await stepOf(lead, 'cart_second_touch');
  keep('context', 'cart_second_touch', await contextOf(a2.id, 'contexto de A2'));
  const a2r = await reserve(lead, a2.id);
  if ((await startNew(a2r)).outcome !== 'request_started') fail('A2 request_started');
  await accept(lead, a2r);
  const z = await stepOf(lead, 'cart_last_touch');
  keep('context', 'cart_last_touch', await contextOf(z.id, 'contexto de Z'));

  // rechazado (el flujo pausado despues de reservar) y terminado (apagado).
  const paused = await motorLead('fixture-pausado');
  const pausedReserved = await reserve(paused.lead, paused.lead.plan.scheduled_action_id);
  await pauseFlow(FLOWS.carrito, A.funnel, 'carrito');
  const rejected = await startNew(pausedReserved);
  if (rejected.outcome !== 'rechazado' || rejected.reason !== 'flujo_pausado') fail('rechazado', rejected);
  keep('request_start', 'rechazado', rejected);
  await setFlow(FLOWS.carrito, A.funnel, 'carrito', 'prender');
  const ending = await motorLead('fixture-terminado');
  const endReserved = await reserve(ending.lead, ending.lead.plan.scheduled_action_id);
  await setFlow(FLOWS.carrito, A.funnel, 'carrito', 'apagar');
  const ended = await startNew(endReserved);
  if (ended.outcome !== 'terminado' || ended.reason !== 'flujo_apagado') fail('terminado', ended);
  keep('request_start', 'terminado', ended);
  return {
    _origen: 'tests/sql/followup_engine/validate_flow_engine_bridge_reads_v1.mjs --emit-bridge-fixtures: '
      + 'las RPC reales sobre la cadena de migraciones (PGlite), con el manifiesto de ATT1 de tests/fixtures/instances/att1/; '
      + 'uuid y horas normalizados',
    ...rows,
  };
};

const main = async () => {
  const world = await buildWorld();
  if (process.argv.includes('--emit-bridge-fixtures')) {
    const text = `${JSON.stringify(await emitBridgeFixtures(world), null, 2)}\n`;
    if (process.argv.includes('--check')) {
      if (readFileSync(FIXTURE, 'utf8') !== text) {
        fail('bridge_rpc_rows.json no es lo que da la cadena: correr con --emit-bridge-fixtures sin --check');
      }
      console.log(JSON.stringify({ fixture: 'igual a la cadena' }));
    } else {
      writeFileSync(FIXTURE, text);
      console.log(JSON.stringify({ fixture: 'escrito' }));
    }
    return;
  }
  console.log(JSON.stringify(await validateReads(world)));
};

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
