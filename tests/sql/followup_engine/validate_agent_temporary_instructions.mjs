// La instruccion temporal para todas las conversaciones (migracion 20261126200000,
// agent-temporary-instruction-v1, 5.3 de Z1), con las RPC reales sobre la cadena completa de migraciones.
// Contrato: docs/contracts/agent-temporary-instruction-v1.md.
//
// Prueba, con un rojo por pieza:
//   0. la forma: agrega exactamente sus funciones y sus dos tablas, no cambia ninguna otra funcion; cada
//      funcion publica la ejecuta solo su rol; las tablas con RLS y sin permisos para la API; correrla dos
//      veces aborta (55000);
//   1. el agente: proponer la deja validada con huella y 24 h; la forma del texto (600, controles); la
//      ventana (en el futuro, a lo sumo 7 dias); proponer otra descarta la que esperaba; el tope diario;
//      no puede publicar ni retirar (42501); no ve ningun ID de Slack;
//   2. la pagina: con la huella correcta queda publicada; otra huella, 22023; sin sesion, 28000; repetir es
//      replayed; la lectura viva la devuelve sin IDs; publicar otra retira la primera (una sola viva) y el
//      indice unico lo hace cumplir; con otra viva, la pagina lo ve en other_live;
//   3. retirar; descartar solo lo no publicado; el vencimiento a las 24 h; con el `hasta` pasado no se
//      publica y la lectura viva no la devuelve;
//   4. los disparadores: contenido congelado, recorrido invalido, permanente, eventos solo agregar; los
//      eventos no llevan el texto.
//
// Datos sinteticos: los de buildWorld (el manifiesto de ATT1) y textos ficticios.
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261126200000_agent_temporary_instructions_v1.sql';
const sha = (text) => createHash('sha256').update(text).digest('hex');

const AE_FUNCTIONS = [
  'propose_agent_temporary_instruction_v1(jsonb,text,timestampwithtimezone,text,jsonb,text)',
  'get_agent_temporary_instruction_v1(jsonb,uuid)',
  'list_agent_temporary_instructions_v1(jsonb,integer)',
];
const APPROVER_FUNCTIONS = [
  'approver_get_agent_temporary_instruction_v1(jsonb,text,uuid)',
  'approver_publish_agent_temporary_instruction_v1(jsonb,text,uuid,text)',
  'approver_retire_agent_temporary_instruction_v1(jsonb,text,uuid,text)',
  'approver_discard_agent_temporary_instruction_v1(jsonb,text,uuid,text)',
];
const SERVICE_FUNCTIONS = ['get_live_agent_temporary_instruction_v1(jsonb)'];
const INTERNAL_FUNCTIONS = [
  '_agent_temporary_instruction_text_is_valid(text)',
  'reject_agent_temporary_instruction_mutation()',
  'validate_agent_temporary_instruction_change()',
  '_agent_temporary_instruction_event(agent_temporary_instructions,text,text,text,text,jsonb)',
  '_agent_temporary_instruction_expire_if_due(agent_temporary_instructions)',
  '_agent_temporary_instruction_view(agent_temporary_instructions,boolean)',
  '_agent_temporary_instruction_live(text,text)',
];
const ALL_FUNCTIONS = [...AE_FUNCTIONS, ...APPROVER_FUNCTIONS, ...SERVICE_FUNCTIONS, ...INTERNAL_FUNCTIONS];
const TABLES = ['agent_temporary_instructions', 'agent_temporary_instruction_events'];

const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature.replace(/^public\./, '').replace(/ /g, ''), row]));
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
  if (!sameSet(newTables, TABLES)) fail('las tablas', newTables);
  const can = async (role, signature) => one((await shapeDb.query(
    `select has_function_privilege($1, to_regprocedure('public.${signature.replace('timestampwithtimezone', 'timestamp with time zone')}'), 'execute') as x`,
    [role])).rows, role).x;
  const roles = ['automation_expert_change', 'flow_change_approver', 'service_role', 'anon', 'authenticated'];
  for (const signature of ALL_FUNCTIONS) {
    const allowed = [];
    for (const role of roles) if (await can(role, signature)) allowed.push(role);
    const expected = AE_FUNCTIONS.includes(signature) ? ['automation_expert_change']
      : APPROVER_FUNCTIONS.includes(signature) ? ['flow_change_approver']
        : SERVICE_FUNCTIONS.includes(signature) ? ['service_role'] : [];
    if (!sameSet(allowed, expected)) fail(`los permisos de ${signature}`, allowed);
  }
  const definers = (await shapeDb.query(`select p.oid::regprocedure::text sig, prosecdef,
      coalesce(array_to_string(proconfig, ','), '') config
    from pg_proc p where p.pronamespace = 'public'::regnamespace and p.proname like '%agent_temporary_instruction%'`)).rows;
  if (definers.length !== ALL_FUNCTIONS.length) fail('las funciones por nombre', definers.map((r) => r.sig));
  for (const row of definers) {
    const signature = row.sig.replace(/^public\./, '').replace(/ /g, '');
    const publicOne = !INTERNAL_FUNCTIONS.includes(signature);
    if (row.prosecdef !== publicOne) fail(`security definer solo las publicas: ${signature}`, row);
    if (!row.config.includes('search_path=pg_catalog')) fail(`search_path fijo: ${signature}`, row);
  }
  const tableAcl = (await shapeDb.query(`select c.relname, c.relrowsecurity,
      has_table_privilege('anon', c.oid, 'select') a, has_table_privilege('authenticated', c.oid, 'select') u,
      has_table_privilege('service_role', c.oid, 'select') s,
      has_table_privilege('automation_expert_change', c.oid, 'select') x
    from pg_class c where c.oid in ('public.agent_temporary_instructions'::regclass,
                                    'public.agent_temporary_instruction_events'::regclass)`)).rows;
  if (tableAcl.length !== 2) fail('las dos tablas', tableAcl);
  for (const row of tableAcl) {
    if (!row.relrowsecurity || row.a || row.u || row.s || row.x) fail('las tablas, sin permisos y con RLS', row);
  }
  await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'agent_temporary_instructions_already_applied' });
}
await shapeDb.close();

// ---------------------------------------------------------------------------
// El mundo: ATT1, con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, INSTANCE, rpc } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const AE = 'U0123456789';
const DAN = 'U0DANSCHWAB1';
const TEAM = 'T0LANCEMOS01';
const later = async () => db.exec('select pg_sleep(0.01)');
const as = async (role, run) => {
  await later();
  await db.exec(`set role ${role}`);
  const out = await run();
  await db.exec('reset role');
  return out;
};
const agent = (run) => as('automation_expert_change', run);
const approver = (run) => as('flow_change_approver', run);
const service = (run) => as('service_role', run);
const inMinutes = async (minutes) => new Date((await world.dbNow()).getTime() + minutes * 60_000).toISOString();
const shift = async (text, params) => {
  await db.exec('set session_replication_role = replica');
  await sql(text, params);
  await db.exec('set session_replication_role = origin');
};
const row = async (id) => one(await sql('select * from public.agent_temporary_instructions where id = $1', [id]),
  'fila');

const VALIDATOR = { contract: 'agent-temporary-instruction-v1', rules_version: 1, product_version: 'v1.42.0',
  git_sha: 'abcdef0' };
const TEXT = 'Hoy no ofrezcas cupón.\nSi preguntan por el vivo, se movió al jueves.';
const propose = async (text = TEXT, extra = {}) => agent(async () => rpc('propose_agent_temporary_instruction_v1',
  INSTANCE, text, extra.until ?? await inMinutes(48 * 60), extra.purpose ?? 'Black sin cupon',
  extra.validator ?? VALIDATOR, AE));

let serial = 0;
const signIn = async (userId) => {
  serial += 1;
  const state = sha(`state-${serial}`);
  const session = sha(`session-${serial}`);
  await approver(async () => {
    await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha(`nonce-${serial}`), '/flow-approval/',
      await inMinutes(10));
    await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
    const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, TEAM, userId,
      await inMinutes(8 * 60));
    if (!created.ok) fail('la sesion', created);
  });
  return session;
};
const S = await signIn(DAN);
const page = (fn, ...params) => approver(() => rpc(fn, INSTANCE, S, ...params));
const publish = (id, fingerprint) => page('approver_publish_agent_temporary_instruction_v1', id, fingerprint);
const live = () => service(() => rpc('get_live_agent_temporary_instruction_v1', INSTANCE));

// 1. El agente.
let first;
{
  first = await propose();
  if (!first.ok || first.status !== 'validada' || first.version !== 1 || !/^[0-9a-f]{64}$/.test(first.fingerprint)) {
    fail('proponer', first);
  }
  const stored = await row(first.instruction_id);
  if ((new Date(stored.expires_at) - new Date(stored.created_at)) / 3_600_000 !== 24) fail('24 h', stored);
  if (stored.instruction_text !== TEXT) fail('el texto con su salto de linea', stored);
  await expectError(db, 'mas de 600', () => propose('a'.repeat(601)),
    { code: '22023', message: 'agent_temporary_instruction_text_invalid' });
  await expectError(db, 'un invisible', () => propose('a​b'),
    { code: '22023', message: 'agent_temporary_instruction_text_invalid' });
  await expectError(db, 'otro validador', () => propose(TEXT, { validator: { ...VALIDATOR, contract: 'x' } }),
    { code: '22023', message: 'agent_temporary_instruction_validator_invalid' });
  const past = await propose(TEXT, { until: await inMinutes(-1) });
  if (past.ok || past.reason_code !== 'agent_temporary_instruction_window_over') fail('en el pasado', past);
  const long = await propose(TEXT, { until: await inMinutes(7 * 24 * 60 + 5) });
  if (long.ok || long.reason_code !== 'agent_temporary_instruction_window_too_long') fail('mas de 7 dias', long);
  if ((await sql('select count(*)::int n from public.agent_temporary_instructions'))[0].n !== 1) {
    fail('lo rechazado no deja fila');
  }

  // Proponer otra descarta la que esperaba el si.
  const second = await propose('Otra cosa.');
  if (!second.ok || second.version !== 2) fail('la segunda', second);
  const replaced = await row(first.instruction_id);
  if (replaced.status !== 'descartada' || replaced.closed_by_kind !== 'sistema' || replaced.closed_by_ref !== 'reemplazada') {
    fail('la anterior queda descartada', replaced);
  }
  const stale = await publish(first.instruction_id, first.fingerprint);
  if (stale.ok || stale.reason_code !== 'agent_temporary_instruction_not_validated') fail('no se publica la reemplazada', stale);
  first = second;

  // El agente no publica ni retira, y no ve IDs.
  await expectError(db, 'el agente no publica', () => agent(() => rpc('approver_publish_agent_temporary_instruction_v1',
    INSTANCE, S, first.instruction_id, first.fingerprint)), { code: '42501' });
  await expectError(db, 'el agente no retira', () => agent(() => rpc('approver_retire_agent_temporary_instruction_v1',
    INSTANCE, S, first.instruction_id, 'x')), { code: '42501' });
  const shown = await agent(() => rpc('get_agent_temporary_instruction_v1', INSTANCE, first.instruction_id));
  if (JSON.stringify(shown).includes(AE) || 'approved_by_ref' in shown) fail('el agente no ve IDs', shown);
}

// 2. La pagina.
{
  await expectError(db, 'otra huella', () => publish(first.instruction_id, 'e'.repeat(64)),
    { code: '22023', message: 'agent_temporary_instruction_fingerprint_mismatch' });
  await expectError(db, 'sin sesion', () => approver(() => rpc('approver_publish_agent_temporary_instruction_v1',
    INSTANCE, sha('sesion-falsa'), first.instruction_id, first.fingerprint)), { code: '28000' });
  if ((await live()).instruction !== null) fail('sin publicar no hay viva');
  const published = await publish(first.instruction_id, first.fingerprint);
  if (!published.ok || published.status !== 'publicada' || published.replayed || published.replaced) {
    fail('publicar', published);
  }
  const again = await publish(first.instruction_id, first.fingerprint);
  if (!again.ok || !again.replayed) fail('repetir', again);
  const stored = await row(first.instruction_id);
  if (stored.approved_by_ref !== DAN || stored.approved_by_kind !== 'slack') fail('quien aprobo', stored);
  const served = await live();
  if (served.instruction?.instruction_id !== first.instruction_id || served.instruction.texto !== 'Otra cosa.'
      || served.instruction.version !== 2 || JSON.stringify(served).includes(DAN)) fail('la lectura viva', served);

  // Publicar otra retira la primera: una sola viva.
  const next = await propose('La tercera.');
  const seen = await page('approver_get_agent_temporary_instruction_v1', next.instruction_id);
  if (seen.other_live?.version !== 2) fail('la pagina ve la otra viva', seen);
  if (seen.approved_by_ref !== null && seen.approved_by_ref !== undefined) fail('sin aprobar', seen);
  const swapped = await publish(next.instruction_id, next.fingerprint);
  if (!swapped.ok || !swapped.replaced) fail('publicar la siguiente', swapped);
  const old = await row(first.instruction_id);
  if (old.status !== 'retirada' || old.closed_by_kind !== 'sistema' || old.closed_by_ref !== 'reemplazada') {
    fail('la anterior queda retirada', old);
  }
  if ((await live()).instruction?.instruction_id !== next.instruction_id) fail('la viva es la nueva');
  await db.exec('set session_replication_role = replica');
  try {
    await expectError(db, 'el indice unico', () => sql(`update public.agent_temporary_instructions
      set status = 'publicada', approved_at = now(), approved_by_kind = 'slack', approved_by_ref = $2,
          closed_at = null, closed_by_kind = null, closed_by_ref = null, close_reason = null where id = $1`,
    [first.instruction_id, DAN]), { code: '23505' });
  } finally {
    await db.exec('set session_replication_role = origin');
  }
  first = next;
}

// 3. Retirar, descartar, vencer, el hasta.
{
  const retired = await page('approver_retire_agent_temporary_instruction_v1', first.instruction_id, 'ya paso el vivo');
  if (!retired.ok || retired.status !== 'retirada') fail('retirar', retired);
  const retiredRow = await row(first.instruction_id);
  if (retiredRow.closed_by_ref !== DAN || retiredRow.close_reason !== 'ya paso el vivo') fail('quien retiro', retiredRow);
  if ((await live()).instruction !== null) fail('retirada no se sirve');
  const twice = await page('approver_retire_agent_temporary_instruction_v1', first.instruction_id, 'x');
  if (!twice.replayed) fail('retirar dos veces', twice);

  const d = await propose('Para descartar.');
  const discarded = await page('approver_discard_agent_temporary_instruction_v1', d.instruction_id, 'no va');
  if (!discarded.ok) fail('descartar', discarded);
  const notLive = await page('approver_retire_agent_temporary_instruction_v1', d.instruction_id, 'x');
  if (notLive.ok || notLive.reason_code !== 'agent_temporary_instruction_not_live') fail('retirar lo no publicado', notLive);

  const e = await propose('Para vencer.');
  await shift(`update public.agent_temporary_instructions set created_at = now() - interval '25 hours',
    expires_at = now() - interval '1 minute' where id = $1`,
    [e.instruction_id]);
  const lapsed = await publish(e.instruction_id, e.fingerprint);
  if (lapsed.ok || lapsed.reason_code !== 'agent_temporary_instruction_expired') fail('vencida', lapsed);
  if ((await row(e.instruction_id)).status !== 'vencida') fail('quedo vencida');

  const over = await propose('Ya paso.');
  await shift(`update public.agent_temporary_instructions set created_at = now() - interval '2 hours',
    valid_until = now() - interval '1 minute' where id = $1`, [over.instruction_id]);
  const late = await publish(over.instruction_id, over.fingerprint);
  if (late.ok || late.reason_code !== 'agent_temporary_instruction_window_over') fail('publicar sin vigencia', late);

  // Una publicada cuyo hasta paso no se sirve aunque siga publicada.
  const soon = await propose('Vence ya.');
  await publish(soon.instruction_id, soon.fingerprint);
  await shift(`update public.agent_temporary_instructions set created_at = now() - interval '2 hours',
    valid_until = now() - interval '1 minute' where id = $1`, [soon.instruction_id]);
  if ((await live()).instruction !== null) fail('vencida no se sirve');
  const view = await agent(() => rpc('get_agent_temporary_instruction_v1', INSTANCE, soon.instruction_id));
  if (view.effective_status !== 'terminada') fail('terminada en la vista', view);
  const listed = await agent(() => rpc('list_agent_temporary_instructions_v1', INSTANCE, 50));
  if (listed.live_instruction_id !== null || listed.instructions.length < 5) fail('la lista', listed);
}

// 1b. El tope diario.
{
  const count = (await sql(`select count(*)::int n from public.agent_temporary_instructions
    where created_at > now() - interval '24 hours'`))[0].n;
  for (let i = count; i < 20; i += 1) {
    const made = await propose(`Relleno ${i}.`);
    if (!made.ok) fail('rellenar', made);
  }
  const limit = await propose('Una mas.');
  if (limit.ok || limit.reason_code !== 'agent_temporary_instruction_daily_limit') fail('el tope diario', limit);
}

// 4. Los disparadores.
{
  const id = first.instruction_id;
  await expectError(db, 'el contenido congelado', () => sql(
    `update public.agent_temporary_instructions set instruction_text = 'otra cosa' where id = $1`, [id]),
  { code: '55000', message: 'agent_temporary_instruction_frozen' });
  const open = (await sql(`select id from public.agent_temporary_instructions where status = 'validada' limit 1`))[0];
  await expectError(db, 'un recorrido invalido', () => sql(
    `update public.agent_temporary_instructions set status = 'retirada', closed_at = now(), closed_by_kind = 'sistema',
       closed_by_ref = 'prueba', close_reason = 'x' where id = $1`, [open.id]),
  { code: '23514' });
  await expectError(db, 'no se borra', () => sql('delete from public.agent_temporary_instructions where id = $1', [id]),
    { code: '55000', message: 'agent_temporary_instruction_is_permanent' });
  await expectError(db, 'los eventos, solo agregar', () => sql(
    `delete from public.agent_temporary_instruction_events where instruction_id = $1`, [id]),
  { code: '55000', message: 'agent_temporary_instruction_event_is_append_only' });
  const events = await sql('select data::text as d from public.agent_temporary_instruction_events');
  if (events.some((event) => event.d.includes('ofrezcas') || event.d.includes('La tercera'))) {
    fail('los eventos no llevan el texto', events);
  }
}

await db.close();
console.log('AGENT_TEMPORARY_INSTRUCTIONS_OK');
