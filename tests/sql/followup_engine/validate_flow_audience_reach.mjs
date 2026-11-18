// La audiencia de hoy de un flujo con disparo por audiencia, contada para el rol
// del agente (migracion 20261116000100, E-622; contratos flow-audience-trigger-v1
// y flow-change-cycle-v1).
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena entera):
// los formularios con consentimiento y los carritos pasan por las admisiones
// portables de hoy, y el flujo de audiencia se publica por el ciclo del operador,
// como en validate_flow_audience_triggers.mjs.
//
// Prueba:
//   0. la forma: una funcion nueva, security definer, solo del rol del agente
//      (ni la API ni service_role); no cambia ninguna otra ni su ACL; correrla
//      dos veces aborta;
//   1. el conteo cierra con la lista: lo que cuenta es lo que deja pasar
//      read_flow_audience_candidates_v1 sin la baja (prospecta) y con la compra
//      del producto (clienta); sin telefonos, contactos ni nombres en la respuesta;
//   2. el rol del agente la corre con su instancia; otra instancia, otra version
//      o un flujo que no es de audiencia no se leen;
//   3. sin la migracion la funcion no existe (el rojo).
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261116000100_flow_audience_reach_v1.sql';
const SIGNATURE = 'read_flow_audience_reach_v1(jsonb,uuid,integer)';
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// 20261117000600 (E-665) cambia solo la repeticion del armado de la lista, que esta lectura no mira.
// 20261117000700 (E-666) cambia solo _flow_audience_occurrence_rows (la lectura de las ocurrencias), no la audiencia
// contada ni read_flow_audience_candidates_v1.
const LATER_OK = new Set(['20261117000600_flow_audience_repeat_counts_started_v1.sql',
  '20261117000700_flow_audience_start_reasons_v1.sql']);
for (const name of names.slice(position + 1).filter((n) => !LATER_OK.has(n))) {
  if (readFileSync(join(root, 'supabase/migrations', name), 'utf8').includes('flow_audience_')) {
    fail(`${name} toca el disparo por audiencia: revisar esta prueba`);
  }
}

// ---------------------------------------------------------------------------
// 0. La forma, sobre el mundo sin la migracion.
// ---------------------------------------------------------------------------
const snapshot = async (db) => new Map((await db.query(`
  select replace(p.oid::regprocedure::text, 'public.', '') as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('automation_expert_change', p.oid, 'execute') as agent_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));

const world = await buildWorld({ skip: [MIGRATION] });
const { db, A, INSTANCE, register, cycle, versionItem, stateItem, cartFor } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const call = async (text, params) => JSON.parse(one(await sql(`select ${text}::text as r`, params), text).r);
const errorOf = async (run) => {
  await db.exec('savepoint intento');
  try {
    await run();
    return null;
  } catch (error) {
    return error.message;
  } finally {
    await db.exec('rollback to savepoint intento');
  }
};

// 3. El rojo: sin la migracion, el agente no tiene con que contar la audiencia.
await db.exec('begin');
const red = await errorOf(() => sql(`select public.read_flow_audience_reach_v1('{}'::jsonb, gen_random_uuid(), 1)`));
await db.exec('rollback');
if (!/read_flow_audience_reach_v1.*does not exist/.test(red ?? '')) fail('sin la migracion no existe', red);
results.rojo = red;

const before = await snapshot(db);
await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
const after = await snapshot(db);
const changed = [...before.keys()].filter((s) => after.has(s)
  && ['definition', 'acl', 'prosecdef'].some((k) => after.get(s)[k] !== before.get(s)[k]));
const added = [...after.keys()].filter((s) => !before.has(s));
if (changed.length || JSON.stringify(added) !== JSON.stringify([SIGNATURE])) {
  fail('la migracion cambio otra cosa', { changed, added });
}
const fn = after.get(SIGNATURE);
if (!fn.prosecdef || !fn.agent_x || fn.service_x || fn.api_x) fail('solo el rol del agente', fn);
try {
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  fail('correrla dos veces tendria que abortar');
} catch (error) {
  // La migracion abre su transaccion con begin: el abort la deja abierta.
  await db.exec('rollback');
  if (error.message !== 'flow_audience_reach_already_applied') fail('la segunda corrida', error.message);
}
results.forma = { agregada: SIGNATURE, cambiadas: changed.length };

// ---------------------------------------------------------------------------
// 1. El conteo cierra con la lista.
// ---------------------------------------------------------------------------
const ana = await cartFor('alcance-ana');
await cartFor('alcance-bea');
await cartFor('alcance-sin-form', { form: false });
const baja = await cartFor('alcance-baja');
await sql(`insert into public.contact_authorizations (contact_id, channel, purpose, authorization_status,
    authorization_source, valid_from) values ($1, 'whatsapp', 'cart_recovery', 'denied', 'manual', now() - interval '1 day')`,
[baja.contact]);

const definitionFor = (dispara, extra = {}) => ({
  esquema: 'setter-flujo/v1', flujo: 'difusion', producto: A.funnel, estado: 'borrador', nombre: 'Recordatorio',
  dispara, para: { es: 'prospecta', formulario_en: '30d' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ plantilla: 'att1_carrito_dudas_01', idioma: 'es_MX', variables: ['nombre', 'producto'] }],
  termina: { si: ['compra'] }, horario: 'siempre',
  topes: { bolsa: 'att1-difusion', por_dia: 'sin_tope', total: 'sin_tope',
    repeticion: { una_por: 'persona', otra_despues_de: '7d' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  ritmo: { por_hora: 2, por_ocurrencia: 10 },
  ...extra,
});
const flowId = await register(A.funnel, 'difusion');
await cycle([versionItem(flowId, A.funnel, 'difusion', definitionFor({ manual: {} })),
  stateItem(flowId, A.funnel, 'difusion', 'prender')], 'difusion manual');

const asAgent = async (run) => {
  await db.exec('begin');
  await db.exec('set local role automation_expert_change');
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};
const reachOf = (instance = INSTANCE, flow = flowId, version = 1) => asAgent(() => call(
  'public.read_flow_audience_reach_v1($1::jsonb, $2::uuid, $3::integer)', [JSON.stringify(instance), flow, version]));
// La lista del bridge, leida como service_role la lee el barredor (postgres aca), para comparar.
const listed = async () => call('public.read_flow_audience_candidates_v1($1::jsonb, $2::uuid, 1, 20000)',
  [JSON.stringify(INSTANCE), flowId]);

const prospects = await listed();
const expected = prospects.candidates.filter((c) => !c.opted_out && !c.customer_products.includes(A.funnel)).length;
const reach = await reachOf();
const KEYS = ['already_customer', 'ambiguous', 'candidates', 'contract', 'flow_id', 'matching', 'measured_at',
  'opted_out', 'product_ref', 'read_capped', 'state', 'status', 'version', 'without_contact', 'without_contact'];
if (JSON.stringify(Object.keys(reach).sort()) !== JSON.stringify([...new Set(KEYS)].sort())) {
  fail('la forma de la respuesta', reach);
}
if (reach.contract !== 'flow-audience-reach-v1' || reach.flow_id !== flowId || reach.version !== 1
    || reach.product_ref !== A.funnel || reach.status !== 'prospecta' || reach.read_capped !== false
    || reach.candidates !== prospects.candidates.length || reach.matching !== expected
    || reach.opted_out !== prospects.candidates.filter((c) => c.opted_out).length
    || reach.without_contact !== prospects.sin_contacto || reach.ambiguous !== prospects.ambiguas
    || reach.matching < 2 || reach.opted_out < 1) {
  fail('el conteo de prospectas cierra con la lista', { reach, expected, lista: prospects.candidates.length });
}
const text = JSON.stringify(reach);
for (const secret of [ana.phone, ana.contact, ana.email, baja.phone, baja.contact]) {
  if (secret && text.includes(String(secret))) fail('la respuesta no trae datos de una persona', secret);
}
results.prospecta = { candidatas: reach.candidates, cumplen: reach.matching, con_baja: reach.opted_out };

// clienta: la misma version leida como una difusion a clientas, solo en esta transaccion.
results.clienta = await (async () => {
  await db.exec('begin');
  try {
    await db.exec('alter table public.flow_definitions disable trigger user');
    await sql(`update public.flow_definitions set definition = jsonb_set(jsonb_set(definition, '{para}',
      '{"es": "clienta"}'::jsonb), '{entra,permiso}', '"compra"'::jsonb) where flow_id = $1 and version = 1`, [flowId]);
    const payload = { id: 'alcance-compra-ana', creation_date: Date.now(), event: 'PURCHASE_APPROVED', version: '2.0.0',
      data: { buyer: { name: ana.name, email: ana.email, checkout_phone: `+${ana.phone}` },
        product: { id: A.productId, name: A.productName },
        purchase: { transaction: 'HPALCANCE001', status: 'APPROVED', approved_date: Date.now(),
          offer: { code: ana.offer.offer_code } } } };
    await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
      [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), ana.email, ana.phone]);
    const customers = await listed();
    await db.exec('set local role automation_expert_change');
    const counted = await call('public.read_flow_audience_reach_v1($1::jsonb, $2::uuid, 1)',
      [JSON.stringify(INSTANCE), flowId]);
    const want = customers.candidates.filter((c) => !c.opted_out && c.customer_products.includes(A.funnel)).length;
    if (counted.status !== 'clienta' || counted.matching !== want || counted.matching < 1
        || counted.already_customer !== 0) {
      fail('el conteo de clientas cierra con la lista', { counted, want });
    }
    return { candidatas: counted.candidates, cumplen: counted.matching };
  } finally {
    await db.exec('rollback');
  }
})();

// ---------------------------------------------------------------------------
// 2. Lo que no se lee.
// ---------------------------------------------------------------------------
const refused = async (label, run, message) => {
  await db.exec('begin');
  await db.exec('set local role automation_expert_change');
  const got = await errorOf(run);
  await db.exec('rollback');
  if (got !== message) fail(label, { got, message });
  return got;
};
const reachCall = (instance, flow, version) => sql(
  'select public.read_flow_audience_reach_v1($1::jsonb, $2::uuid, $3::integer)', [JSON.stringify(instance), flow, version]);
results.rechazos = {
  otra_version: await refused('otra version', () => reachCall(INSTANCE, flowId, 2), 'flow_audience_flow_not_found'),
  otro_flujo: await refused('un flujo que no existe', () => reachCall(INSTANCE, '00000000-0000-4000-8000-000000000622', 1),
    'flow_audience_flow_not_found'),
  sin_version: await refused('sin version', () => reachCall(INSTANCE, flowId, null), 'flow_audience_read_invalid'),
};
// Un flujo con evento no es de audiencia: la lectura no lo cuenta.
const cartFlow = one(await sql(`select flow_id::text from public.flow_controls
  where tenant_ref = $1 and product_ref = $2 and flow_id <> $3 and active_version is not null limit 1`,
[A.tenant, A.funnel, flowId]), 'un flujo con evento').flow_id;
const cartVersion = one(await sql('select active_version from public.flow_controls where flow_id = $1', [cartFlow]),
  'su version').active_version;
results.rechazos.con_evento = await refused('un flujo con evento',
  () => reachCall(INSTANCE, cartFlow, cartVersion), 'flow_audience_not_an_audience');
// La API no la corre.
await db.exec('begin');
await db.exec('set local role anon');
const api = await errorOf(() => reachCall(INSTANCE, flowId, 1));
await db.exec('rollback');
if (!/permission denied/.test(api ?? '')) fail('la API no la corre', api);
results.rechazos.api = 'permission denied';

console.log(JSON.stringify({ validate_flow_audience_reach: 'OK', ...results }));
await db.close();
