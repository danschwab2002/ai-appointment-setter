// Quien ejecuta cada flujo en read_flow_store_v1 (migracion 20261108000100, E-353;
// contrato docs/contracts/flow-change-cycle-v1.md 1.11.0).
//
// Por que: el validador tiene que saber si una version nueva de un flujo del
// legado cambia lo que sale. Lo sabe por la ruta: motor, legado o ninguna.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs.
//
// Prueba:
//   0. la forma: cambia solo read_flow_store_v1, sin cambiar sus permisos, y
//      correrla dos veces aborta;
//   1. el rojo: antes de la migracion la clave no esta;
//   2. despues, cada flujo dice su ruta: motor, legado, nativo (motor) y null
//      sin ruta, y native no cambia.
//
// Con --emit escribe tests/fixtures/flow_change/store_route_executor_e353.json
// (lo que contesta la base, con los uuid normalizados) para
// tests/test_flow_change_without_effect.py; con --check (el de npm test) lo compara.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261108000100_flow_store_route_executor_v1.sql';
const SIGNATURE = 'read_flow_store_v1(jsonb)';
const FIXTURE = join(root, 'tests/fixtures/flow_change/store_route_executor_e353.json');
const EMIT = process.argv.includes('--emit');
const CHECK = process.argv.includes('--check');
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
for (const name of names.slice(position + 1)) {
  if (readFileSync(join(root, 'supabase/migrations', name), 'utf8').includes('read_flow_store_v1')) {
    fail(`${name} toca read_flow_store_v1: revisar esta prueba`);
  }
}
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
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
  if (before.get(SIGNATURE).definition.includes('route_executor')) fail('la clave ya estaba antes de la migracion');
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.has(s) && after.get(s).definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && ['acl', 'prosecdef', 'provolatile'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, [SIGNATURE]) || added.length || removed.length || attributes.length) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  if (!after.get(SIGNATURE).definition.includes('flow_store_route_executor')) fail('la lectura sin su marca');
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'flow_store_route_executor_already_applied') {
      fail('la segunda corrida aborto por otra cosa', error.message);
    }
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { cambiadas: changed.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// 1 y 2. El mundo: ATT1 con el carrito en motor, el pago en legado, un flujo
// sin ruta y una ruta nativa.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, B, FLOWS, INSTANCE, register } = world;
{
  const cart = await world.createRoute(A.funnel, 'carrito', 'carrito', A.scope);
  if (!cart.ok) fail('la ruta del carrito', cart);
  const pay = await world.createRoute(A.funnel, 'pago_fallido', 'pago_fallido', A.scope);
  if (!pay.ok) fail('la ruta del pago', pay);
  const moved = await world.route(A.funnel, 'carrito', FLOWS.carrito, 'motor');
  if (!moved.ok) fail('el carrito a motor', moved);
}
// La ruta nativa nace como en produccion: publicar la v1 y abrir el flujo.
const welcome = await register(A.funnel, 'bienvenida');
await world.cycle([{ ...world.versionItem(welcome, A.funnel, 'bienvenida', {
  esquema: 'setter-flujo/v1', flujo: 'bienvenida', producto: A.funnel, estado: 'borrador', nombre: 'Bienvenida despues de la compra',
  dispara: { evento: 'compra' }, entra: { permiso: 'compra', contesto_a: [] },
  pasos: [{ plantilla: 'att1_bienvenida_01', idioma: 'es_MX', variables: ['nombre', 'producto'] }, { etiquetar: 'cliente' }],
  termina: { si: [], vence: '1d' },
  horario: 'siempre',
  topes: { por_dia: 'sin_tope', total: 'sin_tope', repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
}), ok_del_cliente: { quien: 'juan', medio: 'whatsapp', fecha: '2026-10-15' } }], 'v1 de bienvenida');
{
  const { proposal_id: proposal } = (await db.query(
    'select proposal_id from public.flow_definitions where flow_id = $1 and version = 1', [welcome])).rows[0];
  const opened = (await db.query('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [welcome, proposal, A.timezone])).rows[0].r;
  if (!JSON.parse(opened).ok) fail('abrir la bienvenida', opened);
}

const read = async () => (await db.query('select public.read_flow_store_v1($1::jsonb) as store', [JSON.stringify(INSTANCE)])).rows[0].store;
const store = await read();
const byKey = Object.fromEntries(store.flows.map((flow) => [`${flow.product_ref}/${flow.flow_key}`, flow]));
const expected = {
  [`${A.funnel}/carrito`]: ['motor', false],
  [`${A.funnel}/pago_fallido`]: ['legado', false],
  [`${A.funnel}/bienvenida`]: ['motor', true],
  [`${B.funnel}/carrito`]: [null, false],
};
for (const [key, [executor, native]] of Object.entries(expected)) {
  const flow = byKey[key];
  if (!flow || !('route_executor' in flow) || flow.route_executor !== executor || flow.native !== native) {
    fail(`${key}: la ruta`, flow && { route_executor: flow.route_executor, native: flow.native });
  }
}
results.rutas = Object.fromEntries(Object.entries(expected).map(([key]) => [key, byKey[key].route_executor]));

// ---------------------------------------------------------------------------
// El fixture: lo que contesta la base, con los uuid normalizados.
// ---------------------------------------------------------------------------
const ids = new Map(Object.keys(expected).map((key, index) => [byKey[key].flow_id,
  `0000000${index + 1}-0000-4000-8000-0000000e3530`]));
const normalized = {
  products: store.products,
  flows: Object.keys(expected).map((key) => {
    const flow = byKey[key];
    return {
      flow_id: ids.get(flow.flow_id), product_ref: flow.product_ref, flow_key: flow.flow_key, state: flow.state,
      active_version: flow.active_version, generation: Number(flow.generation), native: flow.native,
      route_executor: flow.route_executor, versions: flow.versions,
    };
  }),
};
const text = `${JSON.stringify(normalized, null, 2)}\n`;
if (EMIT) writeFileSync(FIXTURE, text);
if (CHECK && readFileSync(FIXTURE, 'utf8') !== text) fail(`${FIXTURE} no es lo que contesta la base`);

console.log(JSON.stringify({ validate_flow_store_route_executor: 'ok', ...results }, null, 2));
