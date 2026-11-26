// La pausa del agente comercial (migracion 20261126100000, agent-global-pause-v1, item 5.4), con las RPC
// reales sobre la cadena completa de migraciones. Contrato: docs/contracts/agent-global-pause-v1.md.
//
// Prueba, con un rojo por pieza:
//   0. la forma: agrega exactamente sus funciones y sus dos tablas, no cambia ninguna otra funcion; cada
//      funcion publica la ejecuta solo su rol; las tablas con RLS y sin permisos para la API; correrla dos
//      veces aborta (55000);
//   1. el agente: pausar todas, la misma otra vez es replayed, por etiqueta y por producto conviven; una
//      entrada invalida (alcance, motivo, hasta) es 22023; el tope de 20 vivas;
//   2. la lectura del turno (service_role) devuelve las vivas y las terminadas en 48 h con su fin (ends_at),
//      sin quien la pidio ni el motivo;
//   3. lo retenido: se anota una vez por conversacion y suma los mensajes; una pausa levantada no anota;
//   4. sacarla: queda quien y por que, repetir es replayed, otra instancia no la ve (P0002);
//   5. el vencimiento: una con hasta vencido se lee con su fin, la siguiente escritura la levanta por
//      vencimiento, y una que termino hace mas de 48 h ya no se lee;
//   6. los disparadores: la fila no se borra ni cambia salvo levantarla una vez.
//
// Datos sinteticos: los de buildWorld (el manifiesto de ATT1) y IDs de Slack inventados.
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261126100000_agent_global_pause_v1.sql';

const AE_FUNCTIONS = [
  'pause_agent_globally_v1(jsonb,text,text,text,timestampwithtimezone,text,text)',
  'resume_agent_globally_v1(jsonb,uuid,text,text)',
  'list_agent_global_pauses_v1(jsonb,integer)',
];
const SERVICE_FUNCTIONS = [
  'get_live_agent_global_pauses_v1(jsonb)',
  'record_agent_global_pause_hold_v1(jsonb,uuid,bigint)',
];
const INTERNAL_FUNCTIONS = [
  'reject_agent_global_pause_mutation()',
  '_agent_global_pause_view(agent_global_pauses,timestampwithtimezone)',
  '_agent_global_pauses_expire_due(text,text,timestampwithtimezone)',
];
const ALL_FUNCTIONS = [...AE_FUNCTIONS, ...SERVICE_FUNCTIONS, ...INTERNAL_FUNCTIONS];
const sig = (text) => text.replace(/^public\./, '').replace(/ /g, '');

const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [sig(row.signature), row]));
const tables = async (db) => (await db.query(`select c.relname from pg_class c
  where c.relnamespace = 'public'::regnamespace and c.relkind = 'r' order by 1`)).rows.map((row) => row.relname);

// 0. La forma.
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
  if (!sameSet(newTables, ['agent_global_pauses', 'agent_global_pause_holds'])) fail('las tablas', newTables);
  const roles = ['automation_expert_change', 'flow_change_approver', 'service_role', 'anon', 'authenticated'];
  for (const signature of ALL_FUNCTIONS) {
    const allowed = [];
    for (const role of roles) {
      const can = one((await shapeDb.query(`select has_function_privilege($1, p.oid, 'execute') as x
        from pg_proc p where p.pronamespace = 'public'::regnamespace
         and replace(regexp_replace(p.oid::regprocedure::text, '^public\\.', ''), ' ', '') = $2`,
      [role, signature])).rows, signature).x;
      if (can) allowed.push(role);
    }
    const expected = AE_FUNCTIONS.includes(signature) ? ['automation_expert_change']
      : SERVICE_FUNCTIONS.includes(signature) ? ['service_role'] : [];
    if (!sameSet(allowed, expected)) fail(`los permisos de ${signature}`, allowed);
  }
  const definers = (await shapeDb.query(`select p.oid::regprocedure::text sig, prosecdef,
      coalesce(array_to_string(proconfig, ','), '') config
    from pg_proc p where p.pronamespace = 'public'::regnamespace and p.proname like '%agent_global_pause%'`)).rows;
  for (const row of definers) {
    const signature = sig(row.sig);
    const publicOne = !INTERNAL_FUNCTIONS.includes(signature);
    if (row.prosecdef !== publicOne) fail(`security definer solo las publicas: ${signature}`, row);
    if (!row.config.includes('search_path=pg_catalog')) fail(`search_path fijo: ${signature}`, row);
  }
  const tableAcl = (await shapeDb.query(`select c.relname, c.relrowsecurity,
      has_table_privilege('anon', c.oid, 'select') a, has_table_privilege('authenticated', c.oid, 'select') u,
      has_table_privilege('service_role', c.oid, 'select') s,
      has_table_privilege('automation_expert_change', c.oid, 'select') x
    from pg_class c where c.oid in ('public.agent_global_pauses'::regclass,
                                    'public.agent_global_pause_holds'::regclass)`)).rows;
  for (const row of tableAcl) {
    if (!row.relrowsecurity || row.a || row.u || row.s || row.x) fail('las tablas, sin permisos y con RLS', row);
  }
  await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'agent_global_pause_already_applied' });
}
await shapeDb.close();

// ---------------------------------------------------------------------------
// El mundo: ATT1, con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, INSTANCE, rpc } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const DAN = 'U0DANSCHWAB1';
const BRAYAN = 'U0BRAYANGHL2';
// Dentro de expectError el error deja la transaccion abortada: el rollback devuelve el rol.
const as = async (role, run) => {
  await db.exec(`set role ${role}`);
  const out = await run();
  await db.exec('reset role');
  return out;
};
const agent = (run) => as('automation_expert_change', run);
const service = (run) => as('service_role', run);
const inMinutes = async (minutes) => new Date((await world.dbNow()).getTime() + minutes * 60_000).toISOString();
const pause = (scope, value = null, label = null, until = null, reason = 'Vivo de las 19', who = DAN) =>
  agent(() => rpc('pause_agent_globally_v1', INSTANCE, scope, value, label, until, reason, who));
const live = () => service(() => rpc('get_live_agent_global_pauses_v1', INSTANCE));
const hold = (id, conversation) => service(() => rpc('record_agent_global_pause_hold_v1', INSTANCE, id, conversation));

// 1. El agente.
const all = await pause('todas');
if (!all.ok || all.replayed || all.pause.scope !== 'todas' || all.pause.status !== 'activa'
    || all.pause.requested_by !== DAN || all.pause.reason !== 'Vivo de las 19') fail('pausar todas', all);
const again = await pause('todas', null, null, null, 'otra vez', BRAYAN);
if (!again.ok || !again.replayed || again.pause.pause_id !== all.pause.pause_id || again.pause.requested_by !== DAN) {
  fail('la misma pausa otra vez es la que ya esta', again);
}
const label = await pause('etiqueta', 'black', 'black', await inMinutes(60));
const product = await pause('producto', 'protocolo', 'producto-el-protocolo');
if (!label.ok || label.replayed || !product.ok || product.replayed) fail('etiqueta y producto conviven', { label, product });
for (const [what, args, message] of [
  ['alcance', ['algunas'], 'agent_pause_scope_invalid'],
  ['todas con valor', ['todas', 'x', 'x'], 'agent_pause_scope_invalid'],
  ['etiqueta sin valor', ['etiqueta'], 'agent_pause_scope_invalid'],
  ['etiqueta que no es su valor', ['etiqueta', 'black', 'otra'], 'agent_pause_scope_invalid'],
  ['producto sin etiqueta', ['producto', 'protocolo'], 'agent_pause_scope_invalid'],
  ['mayusculas', ['etiqueta', 'Black', 'Black'], 'agent_pause_scope_invalid'],
]) {
  await expectError(db, what, () => pause(...args), { code: '22023', message });
}
await expectError(db, 'motivo vacio', () => pause('etiqueta', 'otra', 'otra', null, '  '),
  { code: '22023', message: 'agent_pause_reason_invalid' });
await expectError(db, 'hasta en el pasado', async () => pause('etiqueta', 'otra', 'otra', await inMinutes(-5)),
  { code: '22023', message: 'agent_pause_until_invalid' });
await expectError(db, 'hasta en 8 dias', async () => pause('etiqueta', 'otra', 'otra', await inMinutes(8 * 24 * 60)),
  { code: '22023', message: 'agent_pause_until_invalid' });
await expectError(db, 'quien pide no es de Slack', () => pause('etiqueta', 'otra', 'otra', null, 'x', 'dan'),
  { code: '22023', message: 'flow_store_actor_invalid' });
await expectError(db, 'otra instancia', () => agent(() => rpc('pause_agent_globally_v1',
  { tenant_ref: 'lancemos', ally_ref: 'nadie' }, 'todas', null, null, null, 'x', DAN)),
{ code: '22023', message: 'flow_store_instance_not_bound' });
await expectError(db, 'service_role no pausa', () => service(() => rpc('pause_agent_globally_v1', INSTANCE, 'todas',
  null, null, null, 'x', DAN)), { code: '42501' });
await expectError(db, 'el agente no lee lo vivo del turno', () => agent(() => rpc('get_live_agent_global_pauses_v1',
  INSTANCE)), { code: '42501' });
{
  // El tope: 20 vivas (ya hay 3).
  await db.exec('begin');
  for (let index = 0; index < 17; index += 1) {
    const extra = await pause('etiqueta', `tope-${index}`, `tope-${index}`);
    if (!extra.ok) fail(`la viva ${index + 4}`, extra);
  }
  const full = await pause('etiqueta', 'tope-x', 'tope-x');
  await db.exec('rollback');
  if (full.ok !== false || full.reason_code !== 'agent_pause_open_limit') fail('el tope de 20', full);
}

// 2. La lectura del turno.
{
  const read = await live();
  const ids = read.pauses.map((p) => p.pause_id);
  if (!sameSet(ids, [all.pause.pause_id, label.pause.pause_id, product.pause.pause_id])) fail('las vivas', read);
  for (const row of read.pauses) {
    if (!sameSet(Object.keys(row), ['pause_id', 'scope', 'scope_value', 'match_label', 'created_at', 'ends_at'])) {
      fail('la lectura del turno no trae quien ni por que', row);
    }
  }
}

// 3. Lo retenido.
{
  const first = await hold(all.pause.pause_id, 200);
  const second = await hold(all.pause.pause_id, 200);
  const other = await hold(all.pause.pause_id, 201);
  if (first.outcome !== 'recorded' || second.outcome !== 'repeated' || other.outcome !== 'recorded') {
    fail('lo retenido', { first, second, other });
  }
  const listed = await agent(() => rpc('list_agent_global_pauses_v1', INSTANCE, 20));
  const view = listed.pauses.find((p) => p.pause_id === all.pause.pause_id);
  if (view.held_conversations !== 2 || view.held_messages !== 3) fail('los conteos de lo retenido', view);
  await expectError(db, 'conversacion invalida', () => hold(all.pause.pause_id, 0),
    { code: '22023', message: 'agent_pause_hold_invalid' });
  await expectError(db, 'lo retenido no se borra', () => sql('delete from public.agent_global_pause_holds'),
    { code: '55000', message: 'agent_global_pause_is_permanent' });
}

// 4. Sacarla.
{
  const resumed = await agent(() => rpc('resume_agent_globally_v1', INSTANCE, all.pause.pause_id, 'Termino el vivo', BRAYAN));
  if (!resumed.ok || resumed.replayed || resumed.pause.status !== 'levantada' || resumed.pause.lifted_by !== BRAYAN
      || resumed.pause.lift_reason !== 'Termino el vivo' || resumed.pause.held_conversations !== 2) {
    fail('sacar la pausa', resumed);
  }
  const twice = await agent(() => rpc('resume_agent_globally_v1', INSTANCE, all.pause.pause_id, null, DAN));
  if (!twice.ok || !twice.replayed || twice.pause.lifted_by !== BRAYAN) fail('sacarla dos veces', twice);
  const after = await hold(all.pause.pause_id, 202);
  if (after.outcome !== 'not_active') fail('una pausa levantada no anota', after);
  const read = await live();
  const ended = read.pauses.find((p) => p.pause_id === all.pause.pause_id);
  if (!ended || ended.ends_at === null) fail('la levantada se lee con su fin', read);
  await expectError(db, 'otra instancia no la ve', () => agent(() => rpc('resume_agent_globally_v1',
    { ...INSTANCE, ally_ref: 'nadie' }, label.pause.pause_id, null, DAN)), { code: '22023' });
  await expectError(db, 'una que no existe', () => agent(() => rpc('resume_agent_globally_v1', INSTANCE,
    '00000000-0000-4000-8000-000000000000', null, DAN)), { code: 'P0002', message: 'agent_pause_not_found' });
  // Despues de levantarla se puede volver a pausar todas.
  const fresh = await pause('todas');
  if (!fresh.ok || fresh.replayed || fresh.pause.pause_id === all.pause.pause_id) fail('pausar de nuevo', fresh);
  const listed = await agent(() => rpc('list_agent_global_pauses_v1', INSTANCE, 20));
  if (listed.pauses[listed.pauses.length - 1].pause_id !== all.pause.pause_id) fail('las vivas primero', listed);
  await expectError(db, 'limite de la lista', () => agent(() => rpc('list_agent_global_pauses_v1', INSTANCE, 51)),
    { code: '22023', message: 'agent_pause_limit_invalid' });
}

// 5. El vencimiento.
{
  await db.exec('set session_replication_role = replica');
  await sql(`update public.agent_global_pauses set created_at = now() - interval '2 hours',
             until = now() - interval '1 minute' where id = $1`, [label.pause.pause_id]);
  await db.exec('set session_replication_role = origin');
  const read = await live();
  const expired = read.pauses.find((p) => p.pause_id === label.pause.pause_id);
  if (!expired || expired.ends_at === null || new Date(expired.ends_at) > await world.dbNow()) {
    fail('una vencida se lee con su fin', read);
  }
  const listed = await agent(() => rpc('list_agent_global_pauses_v1', INSTANCE, 20));
  if (listed.pauses.find((p) => p.pause_id === label.pause.pause_id).status !== 'vencida') fail('se ve vencida', listed);
  // La siguiente escritura la levanta por vencimiento, y la misma etiqueta se puede volver a pausar.
  const relabel = await pause('etiqueta', 'black', 'black');
  if (!relabel.ok || relabel.replayed) fail('pausar la etiqueta otra vez', relabel);
  const row = one(await sql('select status, lifted_by_kind, lifted_by_ref from public.agent_global_pauses where id = $1',
    [label.pause.pause_id]), 'vencida');
  if (row.status !== 'levantada' || row.lifted_by_kind !== 'vencimiento' || row.lifted_by_ref !== null) {
    fail('levantada por vencimiento', row);
  }
  // Pasadas 48 horas del fin, ya no se lee.
  await db.exec('set session_replication_role = replica');
  await sql(`update public.agent_global_pauses set created_at = now() - interval '50 hours',
             until = now() - interval '49 hours', lifted_at = now() - interval '49 hours' where id = $1`,
  [label.pause.pause_id]);
  await db.exec('set session_replication_role = origin');
  if ((await live()).pauses.some((p) => p.pause_id === label.pause.pause_id)) fail('una de hace 49 h no se lee', 0);
}

// 6. Los disparadores.
{
  await expectError(db, 'no se borra', () => sql('delete from public.agent_global_pauses where id = $1',
    [product.pause.pause_id]), { code: '55000', message: 'agent_global_pause_is_permanent' });
  await expectError(db, 'el motivo no cambia', () => sql(`update public.agent_global_pauses set reason = 'otro'
    where id = $1`, [product.pause.pause_id]), { code: '55000', message: 'agent_global_pause_is_permanent' });
  await expectError(db, 'una levantada no vuelve', () => sql(`update public.agent_global_pauses
    set status = 'activa', lifted_at = null, lifted_by_kind = null, lifted_by_ref = null, lift_reason = null
    where id = $1`, [all.pause.pause_id]), { code: '55000', message: 'agent_global_pause_is_permanent' });
}

console.log('agent global pause: ok');
