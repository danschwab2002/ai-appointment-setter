// El producto que suma el Automation Expert (migracion 20261115000100, product-card-v1, E-569, MP-07 del plan
// de multiproducto), con las RPC reales sobre la cadena completa de migraciones.
// Contrato: docs/contracts/product-card-v1.md.
//
// Prueba, con un rojo por pieza:
//   0. la forma: agrega exactamente sus funciones y sus dos tablas, no cambia ninguna otra funcion; las cuatro
//      publicas las ejecuta solo automation_expert_change (ni service_role); las tablas con RLS y sin permisos
//      para la API; correrla dos veces aborta (55000);
//   1. proponer: guardar, rehacer, revision vieja, clave inmutable, un minimo sin la forma (22023), el tope de
//      cinco abiertos, la version siguiente de la misma clave;
//   2. validar: con bloqueos queda borrador y sin huella; sin bloqueos, validada 24 h con las advertencias que dio
//      la validacion; validar dos veces; rehacer una validada le saca la huella; el vencimiento a las 24 h;
//   3. listar y obtener: taken trae la identidad de todos salvo los descartados (product-card-v1 §2.1), el
//      agente no ve ningun ID de Slack, P0002 de otra instancia;
//   4. los disparadores: contenido congelado desde validada, recorrido invalido, permanente, solo agregar.
//
// El minimo es el de la ficha sintetica de Forever Young (tests/fixtures/agent_products/, la misma que valida
// tests/test_agent_products.py): lo que guarda la fila es lo que produce parse_product_card.
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261115000100_agent_products_v1.sql';

const AE_FUNCTIONS = [
  'save_agent_product_draft_v1(jsonb,uuid,integer,jsonb,text,jsonb,text,text)',
  'record_agent_product_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,text)',
  'get_agent_product_v1(jsonb,uuid)',
  'list_agent_products_v1(jsonb,boolean,integer)',
];
const INTERNAL_FUNCTIONS = [
  '_agent_product_minimum_is_valid(jsonb)',
  '_agent_product_findings_are_valid(jsonb)',
  'reject_agent_product_mutation()',
  'validate_agent_product_change()',
  '_agent_product_event(agent_products,text,text,text,text,jsonb)',
  '_agent_product_fingerprint(uuid,integer,integer,jsonb,text,text)',
  '_agent_product_expire_if_due(agent_products)',
  '_agent_product_view(agent_products)',
  '_agent_products_open(text,text)',
];
const ALL_FUNCTIONS = [...AE_FUNCTIONS, ...INTERNAL_FUNCTIONS];

const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature.replace(/^public\./, '').replace(/ /g, ''), row]));
const tables = async (db) => (await db.query(`select c.relname from pg_class c
  where c.relnamespace = 'public'::regnamespace and c.relkind = 'r' order by 1`)).rows.map((row) => row.relname);

// ---------------------------------------------------------------------------
// 0. La forma.
// ---------------------------------------------------------------------------
const shapeDb = new PGlite();
await shapeDb.waitReady;
await shapeDb.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  create role authenticator noinherit login;
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
  alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
  grant usage on schema public to service_role, anon;
`);
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`${MIGRATION} no esta en la cadena`);
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
await applyFile(shapeDb, join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of names.slice(0, position)) await applyFile(shapeDb, join(root, 'supabase/migrations', name));
const before = await snapshot(shapeDb);
const tablesBefore = await tables(shapeDb);
const target = join(root, 'supabase/migrations', MIGRATION);
await applyFile(shapeDb, target);
const after = await snapshot(shapeDb);
{
  const added = [...after.keys()].filter((signature) => !before.has(signature));
  const removed = [...before.keys()].filter((signature) => !after.has(signature));
  const changed = [...before.keys()].filter((signature) => after.has(signature)
    && (after.get(signature).definition !== before.get(signature).definition
        || after.get(signature).acl !== before.get(signature).acl));
  if (!sameSet(added, ALL_FUNCTIONS) || removed.length !== 0 || changed.length !== 0) {
    fail('la migracion cambio otra cosa', { added, removed, changed });
  }
  const newTables = (await tables(shapeDb)).filter((table) => !tablesBefore.includes(table));
  if (!sameSet(newTables, ['agent_products', 'agent_product_events'])) fail('las tablas', newTables);
  const can = async (role, signature) => one((await shapeDb.query(
    `select has_function_privilege($1, to_regprocedure('public.${signature}'), 'execute') as x`, [role])).rows,
  role).x;
  const roles = ['automation_expert_change', 'flow_change_approver', 'service_role', 'anon', 'authenticated'];
  for (const signature of ALL_FUNCTIONS) {
    const allowed = [];
    for (const role of roles) if (await can(role, signature)) allowed.push(role);
    const expected = AE_FUNCTIONS.includes(signature) ? ['automation_expert_change'] : [];
    if (!sameSet(allowed, expected)) fail(`los permisos de ${signature}`, allowed);
  }
  const definers = (await shapeDb.query(`select p.oid::regprocedure::text sig, prosecdef,
      coalesce(array_to_string(proconfig, ','), '') config
    from pg_proc p where p.pronamespace = 'public'::regnamespace
      and (p.proname like '%agent_product%')`)).rows;
  if (definers.length !== ALL_FUNCTIONS.length) fail('las funciones de productos', definers.map((row) => row.sig));
  for (const row of definers) {
    const signature = row.sig.replace(/^public\./, '').replace(/ /g, '');
    const publicOne = AE_FUNCTIONS.includes(signature);
    if (row.prosecdef !== publicOne) fail(`security definer solo las publicas: ${signature}`, row);
    if (!row.config.includes('search_path=pg_catalog')) fail(`search_path fijo: ${signature}`, row);
  }
  const tableAcl = (await shapeDb.query(`select c.relname, c.relrowsecurity,
      has_table_privilege('anon', c.oid, 'select') a, has_table_privilege('authenticated', c.oid, 'select') u,
      has_table_privilege('service_role', c.oid, 'select') s,
      has_table_privilege('automation_expert_change', c.oid, 'select') x
    from pg_class c where c.oid in ('public.agent_products'::regclass, 'public.agent_product_events'::regclass)`)).rows;
  if (tableAcl.length !== 2) fail('las dos tablas', tableAcl);
  for (const row of tableAcl) {
    if (!row.relrowsecurity || row.a || row.u || row.s || row.x) fail('las tablas, sin permisos y con RLS', row);
  }
  await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'agent_products_already_applied' });
}
await shapeDb.close();

// ---------------------------------------------------------------------------
// El mundo: ATT1, con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, INSTANCE, rpc } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const AE = 'U0123456789';
const later = async () => db.exec('select pg_sleep(0.01)');
const agent = async (run) => {
  await later();
  // Sin finally, como las campanas: dentro de expectError el rollback deshace el set role.
  await db.exec('set role automation_expert_change');
  const out = await run();
  await db.exec('reset role');
  return out;
};
const shift = async (text, params) => {
  await db.exec('set session_replication_role = replica');
  await sql(text, params);
  await db.exec('set session_replication_role = origin');
};
const row = async (id) => one(await sql('select * from public.agent_products where id = $1', [id]), 'fila');

const FY = JSON.parse(readFileSync(join(root, 'tests/fixtures/agent_products/forever_young_card_sintetica_20261114.json'),
  'utf8')).card;
const { texto_libre: FREE_TEXT, ...FY_MINIMUM } = FY;
const MINIMUM = (overrides = {}) => ({ ...structuredClone(FY_MINIMUM), ...overrides });
const keyed = (clave, productId, offer) => MINIMUM({
  clave, nombre: `Producto ${clave}`, alias: [`alias ${clave}`],
  hotmart: { producto_id: productId, hotlink: 'X000PRUEBA' },
  ofertas: [{ codigo: offer, precio: '99', moneda: 'USD', por_defecto: true, otros_montos: [] }],
});
const VALIDATOR = { contract: 'product-card-v1', rules_version: 1, product_version: 'v1.39.0', git_sha: '57e9792' };
const CLEAN = { blocks: [], warnings: [] };
const WARNED = { blocks: [], warnings: [{ code: 'skeleton_section_missing', field: 'texto_libre', detail: 'garantia' }] };

const canonical = (value) => JSON.stringify(value, (_, v) => (v && typeof v === 'object' && !Array.isArray(v)
  ? Object.fromEntries(Object.entries(v).sort(([a], [b]) => (a < b ? -1 : 1))) : v));
const save = (minimum, extra = {}) => agent(() => rpc('save_agent_product_draft_v1', INSTANCE, extra.id ?? null,
  extra.revision ?? null, minimum, extra.freeText ?? FREE_TEXT, extra.warnings ?? [], extra.purpose ?? 'Sumar el producto',
  AE));
const record = (id, revision, result = CLEAN) => agent(() => rpc('record_agent_product_validation_v1', INSTANCE, id,
  revision, VALIDATOR, result, { render: '# Producto: Black Forever Young' }, AE));
const list = (onlyOpen = true, limit = 50) => agent(() => rpc('list_agent_products_v1', INSTANCE, onlyOpen, limit));

// 1. Proponer.
let fyId;
{
  const saved = await save(MINIMUM());
  if (!saved.ok || saved.status !== 'borrador' || saved.revision !== 1 || saved.product_version !== 1) fail('guardar', saved);
  fyId = saved.product_id;
  const stored = await row(fyId);
  if (canonical(stored.minimum) !== canonical(MINIMUM()) || stored.free_text !== FREE_TEXT
      || Number(stored.hotmart_product_id) !== 9000019) fail('lo que guarda la fila', stored);
  const redo = await save(MINIMUM({ alias: ['forever young', 'black de ana lucia'] }), { id: fyId, revision: 1 });
  if (!redo.ok || redo.revision !== 2) fail('rehacer', redo);
  const stale = await save(MINIMUM(), { id: fyId, revision: 1 });
  if (stale.ok || stale.reason_code !== 'agent_product_revision_mismatch') fail('la revision vieja', stale);
  const key = await save(MINIMUM({ clave: 'otra-clave' }), { id: fyId, revision: 2 });
  if (key.ok || key.reason_code !== 'agent_product_key_is_immutable') fail('la clave no cambia', key);
  for (const [label, minimum] of [
    ['una clave de mas', { ...MINIMUM(), texto_libre: 'x' }],
    ['sin alias', MINIMUM({ alias: [] })],
    ['un alias de dos letras', MINIMUM({ alias: ['fy'] })],
    ['dos ofertas por defecto', MINIMUM({ ofertas: [...FY_MINIMUM.ofertas, { ...FY_MINIMUM.ofertas[0], codigo: 'fyprueba2' }] })],
    ['una clave reservada', MINIMUM({ clave: 'principal' })],
    ['un precio como numero', MINIMUM({ ofertas: [{ ...FY_MINIMUM.ofertas[0], precio: 199 }] })],
    ['relaciones sin la forma', MINIMUM({ relaciones: { incluye_productos: [] } })],
  ]) {
    await expectError(db, label, () => save(minimum), { code: '22023', message: 'agent_product_minimum_invalid' });
  }
  await expectError(db, 'un texto libre vacio', () => save(MINIMUM(), { freeText: '' }),
    { code: '22023', message: 'agent_product_free_text_invalid' });
  await expectError(db, 'un texto libre de mas', () => save(MINIMUM(), { freeText: 'x'.repeat(5501) }),
    { code: '22023', message: 'agent_product_free_text_invalid' });
  await expectError(db, 'advertencias sin la forma', () => save(MINIMUM(), { warnings: [{ code: 'Mal Codigo' }] }),
    { code: '22023', message: 'agent_product_warnings_invalid' });
  await expectError(db, 'el id sin la revision', () => save(MINIMUM(), { id: fyId }),
    { code: '22023', message: 'agent_product_redo_invalid' });

  // El tope: cinco abiertos.
  for (const [n, clave] of ['p2', 'p3', 'p4', 'p5'].entries()) {
    const other = await save(keyed(clave, 9100000 + n, `ofer${clave}x`));
    if (!other.ok) fail(`guardar ${clave}`, other);
  }
  const limit = await save(keyed('p6', 9100009, 'oferp6xx'));
  if (limit.ok || limit.reason_code !== 'agent_product_open_limit' || limit.open !== 5) fail('el tope', limit);
}

// 2. Validar.
let fyFingerprint;
{
  const blocked = await record(fyId, 2, { blocks: [{ code: 'alias_taken', field: 'alias[0]' }], warnings: [] });
  if (!blocked.ok || blocked.status !== 'borrador' || blocked.blocks !== 1) fail('con bloqueos', blocked);
  if ((await row(fyId)).fingerprint !== null) fail('con bloqueos no hay huella');
  await expectError(db, 'un resultado sin la forma', () => record(fyId, 2, { blocks: [{ code: 'free_text_amount_not_declared:149 USD' }], warnings: [] }),
    { code: '22023', message: 'agent_product_result_invalid' });
  await expectError(db, 'otro contrato', () => agent(() => rpc('record_agent_product_validation_v1', INSTANCE, fyId, 2,
    { ...VALIDATOR, contract: 'agent-campaigns-v1' }, CLEAN, {}, AE)),
  { code: '22023', message: 'agent_product_validator_invalid' });

  const recorded = await record(fyId, 2, WARNED);
  if (!recorded.ok || recorded.status !== 'validada' || !/^[0-9a-f]{64}$/.test(recorded.fingerprint)) fail('validar', recorded);
  fyFingerprint = recorded.fingerprint;
  const frozen = await row(fyId);
  if ((new Date(frozen.expires_at) - new Date(frozen.validated_at)) / 3_600_000 !== 24) fail('24 h', frozen);
  if (JSON.stringify(frozen.warnings) !== JSON.stringify(WARNED.warnings)) fail('las advertencias de la validacion', frozen);
  const again = await record(fyId, 2);
  if (again.ok || again.reason_code !== 'agent_product_not_draft') fail('validar dos veces', again);

  // Rehacer una validada le saca la huella.
  const p2 = (await list(true)).products.find((product) => product.product_key === 'p2');
  const v2 = await record(p2.product_id, 1);
  if (v2.status !== 'validada') fail('validar p2', v2);
  const redone = await save(keyed('p2', 9100000, 'oferp2x'), { id: p2.product_id, revision: 1, purpose: 'Otra vuelta' });
  if (!redone.ok || redone.status !== 'borrador' || redone.revision !== 2) fail('rehacer una validada', redone);
  const cleared = await row(p2.product_id);
  if (cleared.fingerprint !== null || cleared.validated_at !== null || cleared.validation !== null) fail('sin huella', cleared);

  // El vencimiento: una validada vieja pasa a vencida al tocarla, con su evento.
  const p3 = (await list(true)).products.find((product) => product.product_key === 'p3');
  await record(p3.product_id, 1);
  await shift(`update public.agent_products set validated_at = now() - interval '2 days',
               expires_at = now() - interval '1 day' where id = $1`, [p3.product_id]);
  const expired = await save(keyed('p3', 9100001, 'oferp3x'), { id: p3.product_id, revision: 1 });
  if (expired.ok || expired.reason_code !== 'agent_product_expired') fail('vencida', expired);
  if ((await row(p3.product_id)).status !== 'vencida') fail('queda vencida');
  const events = await sql(`select event_type from public.agent_product_events where product_id = $1 order by created_at`,
    [p3.product_id]);
  if (events.at(-1)?.event_type !== 'agent_product_expired') fail('el evento del vencimiento', events);
}

// 3. Listar y obtener.
{
  // Un descartado (la pagina, MP-08) no retiene nada: se descarta a mano para la prueba.
  const p4 = (await list(true)).products.find((product) => product.product_key === 'p4');
  await shift(`update public.agent_products set status = 'descartada', closed_at = now(), closed_by_kind = 'sistema',
               closed_by_ref = 'prueba', close_reason = 'prueba' where id = $1`, [p4.product_id]);

  const listed = await list(true);
  const keys = listed.products.map((product) => product.product_key).sort();
  if (!sameSet(keys, ['forever-young', 'p2', 'p5'])) fail('las abiertas', keys);
  if (listed.open !== 3 || listed.open_limit !== 5) fail('el conteo de abiertas', listed);
  const takenKeys = listed.taken.map((product) => product.product_key).sort();
  // Taken: todos menos los descartados (product-card-v1 §2.1): el vencido p3 sigue reteniendo su identidad.
  if (!sameSet(takenKeys, ['forever-young', 'p2', 'p3', 'p5'])) fail('taken: todos menos los descartados', takenKeys);
  const fyTaken = listed.taken.find((product) => product.product_key === 'forever-young');
  if (JSON.stringify(fyTaken.alias) !== JSON.stringify(['forever young', 'black de ana lucia'])
      || fyTaken.hotmart_product_id !== 9000019 || JSON.stringify(fyTaken.offer_codes) !== JSON.stringify(['fyprueba1'])
      || JSON.stringify(fyTaken.incluye_productos) !== '[]' || fyTaken.nombre !== 'Black Forever Young') {
    fail('taken: la identidad de un producto', fyTaken);
  }
  const all = await list(false, 2);
  if (all.products.length !== 2 || all.taken.length !== 4) fail('el tope del listado no corta taken', all);
  await expectError(db, 'un tope fuera de rango', () => list(true, 51),
    { code: '22023', message: 'agent_product_list_parameters_invalid' });

  // La version siguiente de la misma clave: otra fila, version 2.
  const v2 = await save(MINIMUM());
  if (!v2.ok || v2.product_version !== 2) fail('la version siguiente', v2);

  const shown = await agent(() => rpc('get_agent_product_v1', INSTANCE, fyId));
  if (shown.status !== 'validada' || shown.fingerprint !== fyFingerprint || shown.texto_libre !== FREE_TEXT
      || JSON.stringify(shown.minimo.alias) !== JSON.stringify(['forever young', 'black de ana lucia'])) fail('obtener', shown);
  if (JSON.stringify(shown).includes(AE) || 'requested_by_ref' in shown || 'approved_by_ref' in shown) {
    fail('el agente no ve IDs de Slack', shown);
  }
  await expectError(db, 'uno que no existe', () => agent(() => rpc('get_agent_product_v1', INSTANCE,
    '00000000-0000-4000-8000-000000000000')), { code: 'P0002', message: 'agent_product_not_found' });
  await expectError(db, 'una instancia sin binding', () => agent(() => rpc('get_agent_product_v1',
    { ...INSTANCE, ally_ref: 'otra-aliada' }, fyId)), { code: '22023', message: 'flow_store_instance_not_bound' });
  await expectError(db, 'el agente no lee la tabla', () => agent(() => sql('select * from public.agent_products')),
    { code: '42501' });
}

// 4. Los disparadores.
{
  await expectError(db, 'el contenido congelado', () => sql(
    `update public.agent_products set purpose = 'otra cosa' where id = $1`, [fyId]),
  { code: '55000', message: 'agent_product_frozen' });
  await expectError(db, 'un recorrido invalido', () => sql(
    `update public.agent_products set status = 'cerrada' where id = $1`, [fyId]),
  { code: '23514', message: 'agent_product_transition_invalid' });
  await expectError(db, 'la identidad', () => sql(
    `update public.agent_products set product_key = 'otra' where id = $1`, [fyId]),
  { code: '55000', message: 'agent_product_identity_is_immutable' });
  await expectError(db, 'no se borra', () => sql('delete from public.agent_products where id = $1', [fyId]),
    { code: '55000', message: 'agent_product_is_permanent' });
  await expectError(db, 'los eventos, solo agregar', () => sql(
    `delete from public.agent_product_events where product_id = $1`, [fyId]),
  { code: '55000', message: 'agent_product_event_is_append_only' });
  const vencida = (await sql(`select id from public.agent_products where status = 'vencida'`))[0].id;
  await expectError(db, 'un final no se toca', () => sql(
    `update public.agent_products set status = 'borrador' where id = $1`, [vencida]),
  { code: '55000', message: 'agent_product_frozen' });
}

await db.close();
console.log('AGENT_PRODUCTS_OK');
