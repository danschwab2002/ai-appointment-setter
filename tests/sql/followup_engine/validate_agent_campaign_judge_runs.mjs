// Las corridas del juez de una campana que pide el Automation Expert (migracion 20261126500000,
// agent-campaign-judge-v1, 4.9, F-163), con las RPC reales sobre la cadena completa de migraciones.
// Contrato: docs/contracts/agent-campaign-judge-v1.md.
//
// Prueba, con un rojo por pieza:
//   0. la forma: agrega exactamente sus tres funciones y su tabla, no cambia ninguna otra funcion; las
//      tres solo para service_role; la tabla con RLS y sin permisos para la API; correrla dos veces
//      aborta (55000);
//   1. abrir: solo sobre una validada; con menos de 20 minutos, no; una sola corriendo por instancia;
//   2. cerrar: el resultado con su forma; una ya cerrada no se pisa;
//   3. el tope: 3 por clave por dia (desde p_day_start); lo de antes del dia no cuenta;
//   4. la abandonada: una corriendo hace mas de 30 minutos se ve como error y no frena la siguiente;
//   5. la lectura: la ultima corrida y cuantas lleva hoy; entradas invalidas, 22023.
//
// Datos sinteticos: los de buildWorld (el manifiesto de ATT1) y campanas ficticias.
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261126500000_agent_campaign_judge_runs_v1.sql';

const SERVICE_FUNCTIONS = [
  'start_agent_campaign_judge_run_v1(jsonb,uuid,text,timestampwithtimezone,integer)',
  'finish_agent_campaign_judge_run_v1(jsonb,uuid,text,jsonb,text)',
  'get_agent_campaign_judge_run_v1(jsonb,uuid,timestampwithtimezone)',
];

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
  if (!sameSet(added, SERVICE_FUNCTIONS) || removed.length !== 0 || changed.length !== 0) {
    fail('la migracion cambio otra cosa', { added, removed, changed });
  }
  const newTables = (await tables(shapeDb)).filter((table) => !tablesBefore.includes(table));
  if (!sameSet(newTables, ['agent_campaign_judge_runs'])) fail('la tabla', newTables);
  const can = async (role, signature) => one((await shapeDb.query(
    `select has_function_privilege($1, p.oid, 'execute') as x from pg_proc p
      where p.pronamespace = 'public'::regnamespace
        and replace(regexp_replace(p.oid::regprocedure::text, '^public[.]', ''), ' ', '') = $2`,
    [role, signature])).rows, signature).x;
  const roles = ['automation_expert_change', 'flow_change_approver', 'service_role', 'anon', 'authenticated'];
  for (const signature of SERVICE_FUNCTIONS) {
    const allowed = [];
    for (const role of roles) if (await can(role, signature)) allowed.push(role);
    if (!sameSet(allowed, ['service_role'])) fail(`los permisos de ${signature}`, allowed);
  }
  const definers = (await shapeDb.query(`select p.oid::regprocedure::text sig, prosecdef,
      coalesce(array_to_string(proconfig, ','), '') config
    from pg_proc p where p.pronamespace = 'public'::regnamespace and p.proname like '%agent_campaign_judge%'`)).rows;
  if (definers.length !== 3) fail('las tres funciones', definers);
  for (const row of definers) {
    if (!row.prosecdef || !row.config.includes('search_path=pg_catalog')) fail(`security definer y search_path: ${row.sig}`, row);
  }
  const tableAcl = one((await shapeDb.query(`select c.relrowsecurity,
      has_table_privilege('anon', c.oid, 'select') a, has_table_privilege('authenticated', c.oid, 'select') u,
      has_table_privilege('service_role', c.oid, 'select') s,
      has_table_privilege('automation_expert_change', c.oid, 'select') x
    from pg_class c where c.oid = 'public.agent_campaign_judge_runs'::regclass`)).rows, 'tabla');
  if (!tableAcl.relrowsecurity || tableAcl.a || tableAcl.u || tableAcl.s || tableAcl.x) {
    fail('la tabla, sin permisos y con RLS', tableAcl);
  }
  await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'agent_campaign_judge_runs_already_applied' });
}
await shapeDb.close();

// ---------------------------------------------------------------------------
// El mundo: ATT1, con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, INSTANCE, rpc } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const AE = 'U0123456789';
const as = async (role, run) => {
  await db.exec('select pg_sleep(0.01)');
  await db.exec(`set role ${role}`);
  const out = await run();
  await db.exec('reset role');
  return out;
};
const agent = (run) => as('automation_expert_change', run);
const service = (run) => as('service_role', run);
const shift = async (text, params) => {
  await db.exec('set session_replication_role = replica');
  await sql(text, params);
  await db.exec('set session_replication_role = origin');
};
const hoursAgo = async (hours) => new Date((await world.dbNow()).getTime() - hours * 3_600_000).toISOString();
const DRAFT = (key) => ({
  clave: key, nombre: 'Black November', etiqueta: key.slice(0, 40), macro_etiqueta: 77,
  desde: '2026-11-20T00:00:00-06:00', hasta: '2099-12-01T00:00:00-06:00', asesoramiento: ['USD 37 en México.'],
  palabras_clave: ['Black November'],
});
const VALIDATOR = { contract: 'agent-campaigns-v1', rules_version: 1, product_version: 'v1.42.0', git_sha: 'abcdef0' };
const saveDraft = async (key) => {
  const saved = await agent(() => rpc('save_agent_campaign_draft_v1', INSTANCE, null, null, DRAFT(key),
    'Black November', AE));
  if (!saved.ok) fail(`guardar ${key}`, saved);
  return saved;
};
const validated = async (key) => {
  const saved = await saveDraft(key);
  const recorded = await agent(() => rpc('record_agent_campaign_validation_v1', INSTANCE, saved.campaign_id,
    saved.revision, VALIDATOR, { blocks: [], warnings: [] }, { render: '# Segmento' }, AE));
  if (recorded.status !== 'validada') fail(`validar ${key}`, recorded);
  return saved.campaign_id;
};
let DAY = await hoursAgo(1);
const start = (id, day = DAY, limit = 3) => service(() => rpc('start_agent_campaign_judge_run_v1', INSTANCE, id, AE,
  day, limit));
const finish = (run, status, result, code = null) => service(() => rpc('finish_agent_campaign_judge_run_v1',
  INSTANCE, run, status, result, code));
const read = (id, day = DAY) => service(() => rpc('get_agent_campaign_judge_run_v1', INSTANCE, id, day));
const CLEAN = { veredicto: 'limpio', abiertos: 0, preguntas: [] };
const OPEN = { veredicto: 'abiertos', abiertos: 1,
  preguntas: [{ numero: 'C1', tipo: 'C', razon: 'promesa', campo: 'oferta.precio', pregunta: '¿Cuál es el precio?' }] };

// 1. Abrir.
const A = await validated('black-november-2026');
{
  const draft = await saveDraft('borrador-2026');
  const refused = await start(draft.campaign_id);
  if (refused.ok !== false || refused.reason_code !== 'agent_campaign_not_validated') fail('sobre un borrador', refused);
  await expectError(db, 'el agente no puede abrir', () => agent(() => rpc('start_agent_campaign_judge_run_v1',
    INSTANCE, A, AE, DAY, 3)), { code: '42501' });
  await expectError(db, 'una campana que no existe', () => start('00000000-0000-4000-8000-000000000000'),
    { code: 'P0002', message: 'agent_campaign_not_found' });
  await expectError(db, 'el dia en el futuro', () => start(A, new Date(Date.now() + 3_600_000).toISOString()),
    { code: '22023', message: 'agent_campaign_judge_day_start_invalid' });
  await expectError(db, 'un tope fuera de rango', () => start(A, DAY, 0),
    { code: '22023', message: 'agent_campaign_judge_daily_limit_invalid' });
  await expectError(db, 'quien pide no es un usuario de Slack', () => service(() => rpc(
    'start_agent_campaign_judge_run_v1', INSTANCE, A, 'dan', DAY, 3)),
  { code: '22023', message: 'agent_campaign_judge_requested_by_invalid' });

  const soon = await validated('vence-pronto-2026');
  await shift(`update public.agent_campaigns set expires_at = clock_timestamp() + interval '10 minutes'
    where id = $1`, [soon]);
  const late = await start(soon);
  if (late.reason_code !== 'agent_campaign_judge_expires_soon') fail('con menos de 20 minutos', late);
  if ((await sql('select count(*)::int n from public.agent_campaign_judge_runs'))[0].n !== 0) fail('no abrio nada');
}
// Sin corridas, para la lectura del final (el tope de abiertas por instancia es 5).
const C = await validated('sin-corridas-2026');
const first = await start(A);
if (!first.ok || first.runs_today !== 1 || first.daily_limit !== 3) fail('la primera', first);
{
  const busy = await start(C);
  if (busy.reason_code !== 'agent_campaign_judge_running') fail('una sola corriendo por instancia', busy);
  const again = await start(A);
  if (again.reason_code !== 'agent_campaign_judge_running') fail('la misma, dos veces', again);
  const shown = await read(A);
  if (shown.last_run?.status !== 'corriendo' || shown.runs_today !== 1) fail('la lectura corriendo', shown);
}

// 2. Cerrar.
await expectError(db, 'limpio sin resultado', () => finish(first.run_id, 'limpio', null),
  { code: '22023', message: 'agent_campaign_judge_result_invalid' });
await expectError(db, 'error sin codigo', () => finish(first.run_id, 'error', null),
  { code: '22023', message: 'agent_campaign_judge_result_invalid' });
await expectError(db, 'un estado que no existe', () => finish(first.run_id, 'corriendo', CLEAN),
  { code: '22023', message: 'agent_campaign_judge_result_invalid' });
await expectError(db, 'un codigo con otra forma', () => finish(first.run_id, 'error', null, 'Raro!'),
  { code: '22023', message: 'agent_campaign_judge_result_invalid' });
{
  const closed = await finish(first.run_id, 'con_preguntas', OPEN);
  if (!closed.ok || closed.status !== 'con_preguntas') fail('cerrar', closed);
  const twice = await finish(first.run_id, 'limpio', CLEAN);
  if (twice.reason_code !== 'agent_campaign_judge_run_not_running') fail('una cerrada no se pisa', twice);
  const shown = await read(A);
  if (shown.last_run?.status !== 'con_preguntas' || shown.last_run?.result?.preguntas?.[0]?.razon !== 'promesa'
      || shown.runs_today !== 1) fail('la lectura cerrada', shown);
}

// 3. El tope: 3 por clave por dia.
{
  const second = await start(A);
  if (!second.ok || second.runs_today !== 2) fail('la segunda', second);
  await finish(second.run_id, 'error', null, 'agent_campaign_judge_model_unavailable');
  const third = await start(A);
  if (!third.ok || third.runs_today !== 3) fail('la tercera', third);
  await finish(third.run_id, 'limpio', CLEAN);
  const fourth = await start(A);
  if (fourth.reason_code !== 'agent_campaign_judge_daily_limit' || fourth.runs_today !== 3) fail('la cuarta', fourth);
  // Lo de antes del comienzo del dia no cuenta.
  await shift(`update public.agent_campaign_judge_runs set started_at = started_at - interval '2 hours',
    finished_at = finished_at - interval '2 hours' where campaign_id = $1`, [A]);
  const tomorrow = await start(A);
  if (!tomorrow.ok || tomorrow.runs_today !== 1) fail('al dia siguiente', tomorrow);
  await finish(tomorrow.run_id, 'limpio', CLEAN);
  const shown = await read(A);
  if (shown.last_run?.run_id !== tomorrow.run_id || shown.runs_today !== 1) fail('la ultima es la de hoy', shown);
}

// 4. La abandonada.
{
  const B = await validated('abandonada-2026');
  const run = await start(B);
  if (!run.ok) fail('abrir B', run);
  await shift(`update public.agent_campaign_judge_runs set started_at = clock_timestamp() - interval '31 minutes'
    where id = $1`, [run.run_id]);
  DAY = await hoursAgo(2);
  const shown = await read(B);
  if (shown.last_run?.status !== 'error' || shown.last_run?.error_code !== 'agent_campaign_judge_abandoned') {
    fail('se ve abandonada', shown);
  }
  const stored = one(await sql('select status from public.agent_campaign_judge_runs where id = $1', [run.run_id]), 'b');
  if (stored.status !== 'corriendo') fail('la lectura no escribe', stored);
  const next = await start(B);
  if (!next.ok || next.runs_today !== 2) fail('la siguiente, con la abandonada contada', next);
  const marked = one(await sql('select status, error_code from public.agent_campaign_judge_runs where id = $1',
    [run.run_id]), 'b');
  if (marked.status !== 'error' || marked.error_code !== 'agent_campaign_judge_abandoned') fail('quedo como error', marked);
  const late = await finish(run.run_id, 'limpio', CLEAN);
  if (late.reason_code !== 'agent_campaign_judge_run_not_running') fail('el resultado tardio no la pisa', late);
  await finish(next.run_id, 'limpio', CLEAN);
}

// 5. La lectura sin corridas y la tabla con su forma.
{
  const shown = await read(C);
  if (shown.last_run !== null || shown.runs_today !== 0) fail('sin corridas', shown);
  await expectError(db, 'la lectura con el dia invalido', () => read(C, null),
    { code: '22023', message: 'agent_campaign_judge_day_start_invalid' });
  await expectError(db, 'dos corriendo a mano', async () => {
    const run = await start(C);
    await sql(`insert into public.agent_campaign_judge_runs (tenant_ref, ally_ref, campaign_id, campaign_key,
      fingerprint, status, requested_by_kind, requested_by_ref)
      select tenant_ref, ally_ref, campaign_id, campaign_key, fingerprint, 'corriendo', 'automation_expert', $2
        from public.agent_campaign_judge_runs where id = $1`, [run.run_id, AE]);
  }, { code: '23505' });
}

console.log('validate_agent_campaign_judge_runs: ok');
