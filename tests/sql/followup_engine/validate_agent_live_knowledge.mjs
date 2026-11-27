// El conocimiento vivo editado por el Automation Expert (migracion 20261126600000,
// live-knowledge-v1, pieza 5.5; lo usan src/bridge/live_knowledge_api.py, la pagina
// src/bridge/knowledge_approval.py y la lectura viva de src/bridge/live_knowledge.py).
//
// Datos: la instancia de prueba (ATT1) y la persona que entra por «Entrar con Slack»,
// como en validate_bulk_send_recipients_read.mjs. El conocimiento es un objeto chico
// sintetico: la base mira la forma, las reglas las corre el bridge con su cargador.
//
// Prueba:
//   0. la forma: dos tablas con RLS y sin permisos de la API, cada funcion solo para su
//      rol, nada mas cambia, correrla dos veces aborta;
//   1. guardar: validada con huella y 24 h, numero viva 1, 2...; el tope de 3 abiertas;
//      la base de la propuesta tiene que ser lo publicado ahora;
//   2. publicar: la huella, una sola publicada, la anterior retirada, la base cambiada
//      frena; lo publicado se lee por service_role; el contenido es inmutable;
//   3. descartar y volver al archivo; sin sesion nada;
//   4. sin la migracion las funciones no existen (el rojo).
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const fail = (message, detail) => {
  console.error(`validate_agent_live_knowledge: ${message}`, detail === undefined ? '' : JSON.stringify(detail));
  process.exit(1);
};
const MIGRATION = '20261126600000_agent_live_knowledge_v1.sql';
const AE = ['save_agent_knowledge_version_v1(jsonb,integer,text,text,uuid,text,jsonb,jsonb,text,integer,jsonb,jsonb,text,text)',
  'get_agent_knowledge_version_v1(jsonb,uuid)', 'list_agent_knowledge_versions_v1(jsonb,boolean,integer)'];
const PAGE = ['approver_get_agent_knowledge_v1(jsonb,text,uuid)',
  'approver_publish_agent_knowledge_v1(jsonb,text,uuid,text,text)',
  'approver_discard_agent_knowledge_v1(jsonb,text,uuid,text)',
  'approver_return_to_file_agent_knowledge_v1(jsonb,text,text)'];
const SERVICE = ['get_live_agent_knowledge_v1(jsonb)'];
const PRIVATE = ['reject_agent_knowledge_mutation()', 'validate_agent_knowledge_change()',
  '_agent_knowledge_event(agent_knowledge_versions,text,text,text,text,jsonb)',
  '_agent_knowledge_fingerprint(uuid,integer,text,text,jsonb,jsonb,text,text)',
  '_agent_knowledge_expire_if_due(agent_knowledge_versions)', '_agent_knowledge_view(agent_knowledge_versions,boolean)'];
const results = {};
const sha = (text) => createHash('sha256').update(text).digest('hex');

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
for (const name of names.slice(position + 1)) {
  if (readFileSync(join(root, 'supabase/migrations', name), 'utf8').includes('agent_knowledge')) {
    fail(`${name} toca el conocimiento vivo: revisar esta prueba`);
  }
}

const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select replace(p.oid::regprocedure::text, 'public.', '') as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('flow_change_approver', p.oid, 'execute') as page_x,
         has_function_privilege('automation_expert_change', p.oid, 'execute') as ae_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));

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

// 4. El rojo: sin la migracion no hay nada.
{
  const before = await snapshot(db);
  if ([...AE, ...PAGE, ...SERVICE].some((s) => before.has(s))) fail('las funciones ya existian antes de la migracion');
  results.rojo = 'sin la migracion no existen';
}

// ---------------------------------------------------------------------------
// 0. La forma.
// ---------------------------------------------------------------------------
{
  const before = await snapshot(db);
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.has(s)
    && ['definition', 'acl', 'prosecdef', 'service_x', 'page_x', 'ae_x', 'api_x'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  const added = [...after.keys()].filter((s) => !before.has(s)).sort();
  if (changed.length || JSON.stringify(added) !== JSON.stringify([...AE, ...PAGE, ...SERVICE, ...PRIVATE].sort())) {
    fail('la migracion cambio otra cosa', { changed, added });
  }
  const only = (list, key) => {
    for (const signature of list) {
      const fn = after.get(signature);
      const flags = { service_x: false, page_x: false, ae_x: false, api_x: false, [key]: true };
      if (!fn.prosecdef || Object.entries(flags).some(([k, v]) => fn[k] !== v)) fail(`${signature}: solo ${key}`, fn);
    }
  };
  only(AE, 'ae_x');
  only(PAGE, 'page_x');
  only(SERVICE, 'service_x');
  for (const signature of PRIVATE) {
    const fn = after.get(signature);
    if (fn.service_x || fn.page_x || fn.ae_x || fn.api_x) fail(`${signature}: la interna no se expone`, fn);
  }
  const tables = (await db.query(`
    select c.relname, c.relrowsecurity rls,
           has_table_privilege('anon', c.oid, 'select') or has_table_privilege('authenticated', c.oid, 'select')
             or has_table_privilege('service_role', c.oid, 'select')
             or has_table_privilege('automation_expert_change', c.oid, 'select')
             or has_table_privilege('flow_change_approver', c.oid, 'select') x
    from pg_class c where c.oid in ('public.agent_knowledge_versions'::regclass, 'public.agent_knowledge_events'::regclass)
  `)).rows;
  if (tables.length !== 2 || tables.some((t) => !t.rls || t.x)) fail('las tablas: RLS y sin permisos', tables);
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'agent_live_knowledge_already_applied') fail('la segunda corrida', error.message);
    await db.exec('rollback');
  }
  results.forma = { nuevas: added.length };
}

await db.exec(`
  set session_replication_role = replica;
  insert into public.commercial_ally_runtime_bindings (tenant_ref, funnel_ref, binding_version, status,
    ally_ref, lead_ally_name, lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
    product_name, product_price, currency, offer_code, consent_copy_version, hotmart_product_id,
    chatwoot_account_id, chatwoot_inbox_id, inbound_scope_key, inbound_scope_version)
  values ('lancemos', 'att1', 1, 'active', 'att1', 'Aliada de prueba', 'sitio', 'landing', 'prueba.example',
    '/landing', 'H00000000', 'Producto de prueba', 10, 'MXN', 'OFR11', 'copy-v1', 1011, 2, 11, 'att1-inbound', 1);
  set session_replication_role = origin;
`);
const INSTANCE = { tenant_ref: 'lancemos', ally_ref: 'att1' };
const TEAM = 'T0LANCEMOS01';
const DAN = 'U0DANSCHWAB1';
const ASKER = 'U01ASKER00';
const FILE_SHA = sha('archivo-v15');
const one = (rows, label) => {
  if (rows.length !== 1) fail(`${label}: una fila`, rows);
  return rows[0];
};
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const rpc = async (fn, ...args) => {
  const marks = args.map((_, i) => `$${i + 1}`).join(', ');
  const values = args.map((a) => (a !== null && typeof a === 'object' && !Array.isArray(a) ? JSON.stringify(a) : a));
  return JSON.parse(one(await sql(`select public.${fn}(${marks})::text as r`, values), fn).r);
};
const as = async (role, run) => {
  await db.exec(`set role ${role}`);
  try {
    return await run();
  } finally {
    await db.exec('reset role');
  }
};
const errorOf = async (run) => {
  await db.exec('begin');
  try {
    await run();
    return 'paso';
  } catch (error) {
    return error.message;
  } finally {
    await db.exec('rollback');
  }
};
const dbNow = async () => new Date(one(await sql('select clock_timestamp() as t'), 'ahora').t);
const inMinutes = async (minutes) => new Date((await dbNow()).getTime() + minutes * 60_000).toISOString();
const session = sha('conocimiento-session');
await as('flow_change_approver', async () => {
  const state = sha('conocimiento-state');
  await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha('conocimiento-nonce'), '/flow-approval/', await inMinutes(10));
  await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
  const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, TEAM, DAN, await inMinutes(60));
  if (!created.ok) fail('entrar', created);
});
const page = (fn, ...args) => as('flow_change_approver', () => rpc(fn, ...args));
const agent = (fn, ...args) => as('automation_expert_change', () => rpc(fn, ...args));
const service = (fn, ...args) => as('service_role', () => rpc(fn, ...args));

const save = (n, base = { origin: 'archivo', id: null, sha: FILE_SHA }) => agent('save_agent_knowledge_version_v1',
  INSTANCE, 15, FILE_SHA, base.origin, base.id, base.sha, { cambios: { faq: [{ pregunta: `p${n}` }] } },
  { voz: { resumen: 'r' }, faq: [{ pregunta: `p${n}` }] }, sha(`render-${n}`), 29000 + n,
  { secciones: ['faq'] }, { contract: 'live-knowledge-v1', git_sha: 'abcdef0' }, `Cambio ${n}`, ASKER);

// ---------------------------------------------------------------------------
// 1. Guardar.
// ---------------------------------------------------------------------------
const first = await save(1);
if (!first.ok || first.status !== 'validada' || first.live_version !== 1 || !/^[0-9a-f]{64}$/.test(first.fingerprint)) {
  fail('guardar la primera', first);
}
const second = await save(2);
const third = await save(3);
if (second.live_version !== 2 || third.live_version !== 3) fail('los numeros', [second, third]);
const fourth = await save(4);
if (fourth.ok || fourth.reason_code !== 'agent_knowledge_open_limit') fail('el tope de abiertas', fourth);
const badBase = await save(5, { origin: 'vivo', id: first.knowledge_id, sha: sha('render-1') });
if (badBase.ok || badBase.reason_code !== 'agent_knowledge_base_changed') fail('base viva que no esta publicada', badBase);
const listed = await agent('list_agent_knowledge_versions_v1', INSTANCE, true, 10);
if (listed.versions.length !== 3 || JSON.stringify(listed.versions[0].sections) !== '["faq"]') fail('la lista', listed);
const read = await agent('get_agent_knowledge_version_v1', INSTANCE, first.knowledge_id);
if (read.approved_by_ref !== undefined || read.rendered_sha256 !== sha('render-1')) fail('la lectura del agente', read);
results.guardar = { abiertas: 3 };

// ---------------------------------------------------------------------------
// 2. Publicar.
// ---------------------------------------------------------------------------
const wrong = await page('approver_publish_agent_knowledge_v1', INSTANCE, session, first.knowledge_id, 'a'.repeat(64), sha('render-1'));
if (wrong.ok || wrong.reason_code !== 'agent_knowledge_fingerprint_mismatch') fail('la huella', wrong);
if ((await service('get_live_agent_knowledge_v1', INSTANCE)).knowledge !== null) fail('nada publicado todavia');
const published = await page('approver_publish_agent_knowledge_v1', INSTANCE, session, first.knowledge_id,
  first.fingerprint, sha('render-1'));
if (!published.ok || published.status !== 'publicada') fail('publicar', published);
const live = (await service('get_live_agent_knowledge_v1', INSTANCE)).knowledge;
if (live.knowledge_id !== first.knowledge_id || live.file_sha256 !== FILE_SHA || live.approved_by_ref !== DAN
    || live.rendered_sha256 !== sha('render-1')) fail('la lectura viva', live);
// La segunda se armo sobre el archivo: ya no es lo vigente.
const stale = await page('approver_publish_agent_knowledge_v1', INSTANCE, session, second.knowledge_id,
  second.fingerprint, sha('render-2'));
if (stale.ok || stale.reason_code !== 'agent_knowledge_base_changed') fail('base cambiada', stale);
// Una armada sobre la viva si se publica, y retira la anterior.
await page('approver_discard_agent_knowledge_v1', INSTANCE, session, third.knowledge_id, 'no va');
const next = await save(7, { origin: 'vivo', id: first.knowledge_id, sha: sha('render-1') });
if (!next.ok) fail('guardar sobre la viva', next);
const replaced = await page('approver_publish_agent_knowledge_v1', INSTANCE, session, next.knowledge_id,
  next.fingerprint, sha('render-7'));
if (!replaced.ok) fail('publicar sobre la viva', replaced);
const old = await page('approver_get_agent_knowledge_v1', INSTANCE, session, first.knowledge_id);
if (old.status !== 'retirada' || old.live.knowledge_id !== next.knowledge_id) fail('la anterior retirada', old);
const replay = await page('approver_publish_agent_knowledge_v1', INSTANCE, session, next.knowledge_id,
  next.fingerprint, sha('render-7'));
if (!replay.ok || !replay.replayed) fail('publicar dos veces', replay);
const frozen = await errorOf(() => db.exec(`update public.agent_knowledge_versions set purpose = 'otro' where id = '${next.knowledge_id}'`));
if (frozen !== 'agent_knowledge_frozen') fail('el contenido es inmutable', frozen);
const permanent = await errorOf(() => db.exec(`delete from public.agent_knowledge_versions where id = '${first.knowledge_id}'`));
if (permanent !== 'agent_knowledge_is_permanent') fail('no se borra', permanent);
const twoLive = await errorOf(() => db.exec(`update public.agent_knowledge_versions set status = 'publicada',
  approved_at = clock_timestamp(), approved_by_kind = 'slack', approved_by_ref = '${DAN}' where id = '${second.knowledge_id}'`));
if (!/agent_knowledge_versions_one_live|duplicate key/.test(twoLive)) fail('una sola publicada', twoLive);
results.publicar = { retirada: 1 };

// ---------------------------------------------------------------------------
// 3. Descartar, volver al archivo y sin sesion.
// ---------------------------------------------------------------------------
const discardLive = await page('approver_discard_agent_knowledge_v1', INSTANCE, session, next.knowledge_id, 'x');
if (discardLive.ok || discardLive.reason_code !== 'agent_knowledge_final') fail('no se descarta la publicada', discardLive);
const noSession = await errorOf(async () => {
  await db.exec('set local role flow_change_approver');
  await rpc('approver_return_to_file_agent_knowledge_v1', INSTANCE, sha('otra'), 'x');
});
if (noSession !== 'flow_approval_session_invalid') fail('sin sesion', noSession);
const back = await page('approver_return_to_file_agent_knowledge_v1', INSTANCE, session, 'volver al archivo');
if (!back.ok || back.retired_live_version !== next.live_version) fail('volver al archivo', back);
if ((await service('get_live_agent_knowledge_v1', INSTANCE)).knowledge !== null) fail('despues de volver, nada vivo');
const again = await page('approver_return_to_file_agent_knowledge_v1', INSTANCE, session, 'otra vez');
if (again.ok || again.reason_code !== 'agent_knowledge_no_live') fail('volver sin viva', again);
const events = one(await sql(`select count(*)::int n from public.agent_knowledge_events`), 'eventos').n;
if (events < 8) fail('los eventos', events);
results.cierre = { eventos: events };

console.log(JSON.stringify({ validate_agent_live_knowledge: 'ok', ...results }));
