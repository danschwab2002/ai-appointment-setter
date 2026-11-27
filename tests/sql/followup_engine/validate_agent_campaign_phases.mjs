// Las fases del lanzamiento y el bloque minimo (migracion 20261126800000, agent-campaign-phases-v1, 4.8), con las
// RPC reales sobre la cadena completa de migraciones. Contrato: docs/contracts/agent-campaign-phases-v1.md.
//
// Prueba, con un rojo por pieza:
//   0. la forma: agrega solo get_agent_campaign_rests_v1 (solo service_role) y reescribe solo
//      _agent_campaign_draft_is_valid, con sus mismos permisos; correrla dos veces aborta (55000);
//   1. la forma del borrador: un borrador de hoy da lo mismo que antes; fases sin bloque_minimo, una fase sin
//      clave valida, siete fases o un bloque minimo sin asesoramiento no entran;
//   2. el recorrido: una campana con fases se guarda, se valida, pasa el juez y se publica como cualquiera; la
//      lectura del reposo no la trae publicada; pausada, la trae (sin IDs de Slack); una pausada sin bloque
//      minimo no; una terminada hace mas de 14 dias tampoco.
//
// Datos sinteticos: los de buildWorld (el manifiesto de ATT1) y una campana ficticia.
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261126800000_agent_campaign_phases_v1.sql';
const sha = (text) => createHash('sha256').update(text).digest('hex');
const NEW = 'get_agent_campaign_rests_v1(jsonb)';
const REWRITTEN = '_agent_campaign_draft_is_valid(jsonb)';

const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature.replace(/^public\./, '').replace(/ /g, ''), row]));

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
const target = join(root, 'supabase/migrations', MIGRATION);
await applyFile(shapeDb, target);
const after = await snapshot(shapeDb);
{
  const added = [...after.keys()].filter((signature) => !before.has(signature));
  const removed = [...before.keys()].filter((signature) => !after.has(signature));
  const changed = [...before.keys()].filter((signature) => after.has(signature)
    && (after.get(signature).definition !== before.get(signature).definition
        || after.get(signature).acl !== before.get(signature).acl));
  if (!sameSet(added, [NEW]) || removed.length !== 0 || !sameSet(changed, [REWRITTEN])) {
    fail('la migracion cambio otra cosa', { added, removed, changed });
  }
  if (after.get(REWRITTEN).acl !== before.get(REWRITTEN).acl) fail('los permisos de la forma cambiaron');
  const can = async (role, signature) => one((await shapeDb.query(
    `select has_function_privilege($1, to_regprocedure('public.${signature}'), 'execute') as x`, [role])).rows,
  role).x;
  const allowed = [];
  for (const role of ['automation_expert_change', 'flow_change_approver', 'service_role', 'anon', 'authenticated']) {
    if (await can(role, NEW)) allowed.push(role);
  }
  if (!sameSet(allowed, ['service_role'])) fail('la lectura del reposo la ejecuta solo service_role', allowed);
  await expectError(shapeDb, 'correrla dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'agent_campaign_phases_already_applied' });
}

// ---------------------------------------------------------------------------
// 1. La forma del borrador.
// ---------------------------------------------------------------------------
const PHASES = [
  { clave: 'preventa', nombre: 'Preventa', desde: '2026-11-14T09:00:00-06:00', hasta: '2026-11-19T10:00:00-06:00' },
  { clave: 'carrito', nombre: 'Carrito abierto', desde: '2026-11-19T10:00:00-06:00', hasta: '2099-11-30T00:00:00-06:00',
    asesoramiento: ['USD 37 en Mexico.'] },
];
const MINIMAL = { instruccion: 'Ahora no hay ninguna fase abierta.', asesoramiento: ['Es un programa digital.'] };
const DRAFT = (overrides = {}) => ({
  clave: 'black-november-2026', nombre: 'Black November', etiqueta: 'black-november', macro_etiqueta: 77,
  desde: '2026-11-14T09:00:00-06:00', hasta: '2099-12-01T00:00:00-06:00', asesoramiento: ['USD 37 en Mexico.'],
  palabras_clave: ['Black November'], ...overrides,
});
{
  const valid = async (draft) => one((await shapeDb.query(
    'select public._agent_campaign_draft_is_valid($1::jsonb) as ok', [JSON.stringify(draft)])).rows, 'forma').ok;
  const old = { ...DRAFT(), oferta: { precio: '37' } };
  if (await valid(old) !== true) fail('un borrador de hoy sigue entrando');
  if (await valid({ ...old, extra: 1 }) !== false) fail('una clave de mas sigue sin entrar');
  if (await valid(DRAFT({ fases: PHASES, bloque_minimo: MINIMAL })) !== true) fail('las fases con el bloque minimo');
  if (await valid(DRAFT({ bloque_minimo: MINIMAL })) !== true) fail('el bloque minimo solo');
  if (await valid(DRAFT({ fases: PHASES })) !== false) fail('fases sin bloque minimo');
  if (await valid(DRAFT({ fases: [{ ...PHASES[0], clave: 'Con-Mayus' }], bloque_minimo: MINIMAL })) !== false) {
    fail('una fase con una clave que no es un slug');
  }
  if (await valid(DRAFT({ fases: [{ ...PHASES[0], clave: 'x'.repeat(25) }], bloque_minimo: MINIMAL })) !== false) {
    fail('una fase con una clave de mas de 24');
  }
  if (await valid(DRAFT({ fases: [{ ...PHASES[0], desde: '2026-11-14' }], bloque_minimo: MINIMAL })) !== false) {
    fail('una fase sin fecha con zona');
  }
  const seven = Array.from({ length: 7 }, (_, index) => ({ ...PHASES[0], clave: `f${index}` }));
  if (await valid(DRAFT({ fases: seven, bloque_minimo: MINIMAL })) !== false) fail('siete fases');
  if (await valid(DRAFT({ fases: PHASES, bloque_minimo: { instruccion: 'x' } })) !== false) {
    fail('un bloque minimo sin asesoramiento');
  }
  if (await valid(DRAFT({ fases: [], bloque_minimo: MINIMAL })) !== false) fail('fases vacias');
}

// ---------------------------------------------------------------------------
// 2. El recorrido, en el mundo de ATT1.
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
const VALIDATOR = { contract: 'agent-campaigns-v1', rules_version: 1, product_version: 'v1.42.0', git_sha: 'abcdef0' };
const CHECK = 'c'.repeat(64);
const LIMPIO = { veredicto: 'limpio', abiertos: 0, items: [] };

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
const published = async (draft) => {
  const saved = await agent(() => rpc('save_agent_campaign_draft_v1', INSTANCE, null, null, draft, 'Black November', AE));
  if (!saved.ok) fail(`guardar ${draft.clave}`, saved);
  const recorded = await agent(() => rpc('record_agent_campaign_validation_v1', INSTANCE, saved.campaign_id,
    saved.revision, VALIDATOR, { blocks: [], warnings: [] }, { render: '# Segmento', piezas: [] }, AE));
  if (recorded.status !== 'validada') fail(`validar ${draft.clave}`, recorded);
  const checked = await service(() => rpc('record_agent_campaign_check_v1', INSTANCE, saved.campaign_id,
    recorded.fingerprint, LIMPIO, CHECK));
  if (!checked.ok) fail(`juez ${draft.clave}`, checked);
  const out = await page('approver_publish_agent_campaign_v1', saved.campaign_id, recorded.fingerprint, CHECK);
  if (!out.ok || out.status !== 'publicada') fail(`publicar ${draft.clave}`, out);
  return saved.campaign_id;
};
const rests = async () => (await service(() => rpc('get_agent_campaign_rests_v1', INSTANCE))).campaigns;

{
  const phased = await published(DRAFT({ fases: PHASES, bloque_minimo: MINIMAL }));
  const live = await service(() => rpc('get_live_agent_campaigns_v1', INSTANCE));
  if (live.campaigns.length !== 1 || live.campaigns[0].campaign_id !== phased
      || live.campaigns[0].draft.fases.map((phase) => phase.clave).join() !== 'preventa,carrito') {
    fail('la viva con fases', live);
  }
  if ((await rests()).length !== 0) fail('una publicada no esta en reposo');

  const paused = await page('approver_pause_agent_campaign_v1', phased, 'cambio de fase a mano');
  if (!paused.ok || paused.status !== 'pausada') fail('pausar', paused);
  const resting = await rests();
  if (resting.length !== 1 || resting[0].campaign_id !== phased || resting[0].status !== 'pausada'
      || resting[0].check_sha !== CHECK || !resting[0].draft.bloque_minimo) fail('la pausada en reposo', resting);
  if (JSON.stringify(resting).includes(DAN)) fail('el reposo no lleva IDs de Slack');

  // Una pausada sin bloque minimo no esta en reposo: se sirve nada, como hoy.
  const plain = await published(DRAFT({ clave: 'sin-bloque', etiqueta: 'sin-bloque' }));
  await page('approver_pause_agent_campaign_v1', plain, 'fin de la prueba');
  if ((await rests()).some((row) => row.campaign_id === plain)) fail('una pausada sin bloque minimo');

  // Una terminada hace mas de 14 dias ya no.
  await shift(`update public.agent_campaigns set status = 'terminada', paused_at = null, paused_by_ref = null,
      pause_reason = null, closed_at = now(), closed_by_kind = 'sistema', closed_by_ref = 'sistema',
      valid_from = now() - interval '30 days', valid_until = now() - interval '15 days' where id = $1`, [phased]);
  if ((await rests()).length !== 0) fail('una terminada hace mas de 14 dias');
  await shift(`update public.agent_campaigns set valid_until = now() - interval '13 days' where id = $1`, [phased]);
  const recent = await rests();
  if (recent.length !== 1 || recent[0].status !== 'terminada') fail('una terminada hace menos de 14 dias', recent);
}

console.log('agent-campaign-phases-v1: forma, borrador y reposo OK');
