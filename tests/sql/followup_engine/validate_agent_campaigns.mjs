// Las campanas del Automation Expert (migracion 20261108005000, agent-campaigns-v1, E-355), con las RPC
// reales sobre la cadena completa de migraciones. Contrato: docs/contracts/agent-campaigns-v1.md.
//
// Prueba, con un rojo por pieza:
//   0. la forma: agrega exactamente sus funciones y sus dos tablas, no cambia ninguna otra funcion; cada
//      funcion publica la ejecuta solo su rol; las tablas con RLS y sin permisos para la API; correrla dos
//      veces aborta (55000);
//   1. el agente: guardar, rehacer, revision vieja, tope, validar con bloqueos, validar sin bloqueos; rehacer
//      una validada (preguntas del juez) le saca la huella; no puede publicar (42501);
//   2. la pagina: sin control de calidad no se publica, con un chequeo_sha que no es el de la fila tampoco,
//      con la huella correcta queda publicada; repetir es replayed; sin sesion, 28000;
//   3. dos vivas: la segunda se rechaza y el indice unico lo hace cumplir;
//      una viva vencida no retiene el lugar;
//   4. pausar y reanudar; descartar solo lo no publicado; el vencimiento a las 24 h;
//   5. el juez: resultado de otra huella se rechaza, resultado limpio habilita y extiende 24 h, un resultado
//      sucio no pisa a una publicada; la lectura del catalogo vivo devuelve solo la publicada;
//   6. los disparadores: contenido congelado desde validada, recorrido invalido, permanente, solo agregar;
//      el agente no ve ningun ID de Slack.
//
// Datos sinteticos: los de buildWorld (el manifiesto de ATT1) y una campana ficticia.
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261108005000_agent_campaigns_v1.sql';
const sha = (text) => createHash('sha256').update(text).digest('hex');

const AE_FUNCTIONS = [
  'save_agent_campaign_draft_v1(jsonb,uuid,integer,jsonb,text,text)',
  'record_agent_campaign_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,text)',
  'get_agent_campaign_v1(jsonb,uuid)',
  'list_agent_campaigns_v1(jsonb,boolean,integer)',
];
const APPROVER_FUNCTIONS = [
  'approver_get_agent_campaign_v1(jsonb,text,uuid)',
  'approver_publish_agent_campaign_v1(jsonb,text,uuid,text,text)',
  'approver_pause_agent_campaign_v1(jsonb,text,uuid,text)',
  'approver_resume_agent_campaign_v1(jsonb,text,uuid)',
  'approver_discard_agent_campaign_v1(jsonb,text,uuid,text)',
];
const SERVICE_FUNCTIONS = [
  'get_live_agent_campaigns_v1(jsonb)',
  'operator_get_agent_campaign_v1(jsonb,uuid)',
  'record_agent_campaign_check_v1(jsonb,uuid,text,jsonb,text)',
];
const INTERNAL_FUNCTIONS = [
  '_agent_campaign_draft_is_valid(jsonb)',
  'reject_agent_campaign_mutation()',
  'validate_agent_campaign_change()',
  '_agent_campaign_event(agent_campaigns,text,text,text,text,jsonb)',
  '_agent_campaign_fingerprint(uuid,integer,integer,jsonb,text)',
  '_agent_campaign_expire_if_due(agent_campaigns)',
  '_agent_campaign_view(agent_campaigns,boolean)',
  '_agent_campaigns_open(text,text)',
];
const ALL_FUNCTIONS = [...AE_FUNCTIONS, ...APPROVER_FUNCTIONS, ...SERVICE_FUNCTIONS, ...INTERNAL_FUNCTIONS];

const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature.replace(/^public\./, '').replace(/ /g, ''), row]));
const tables = async (db) => (await db.query(`select c.relname from pg_class c
  where c.relnamespace = 'public'::regnamespace and c.relkind = 'r' order by 1`)).rows.map((row) => row.relname);

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
  if (!sameSet(newTables, ['agent_campaigns', 'agent_campaign_events'])) fail('las tablas', newTables);
  const can = async (role, signature) => one((await shapeDb.query(
    `select has_function_privilege($1, to_regprocedure('public.${signature}'), 'execute') as x`, [role])).rows,
  role).x;
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
    from pg_proc p where p.pronamespace = 'public'::regnamespace and p.proname like '%agent_campaign%'`)).rows;
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
    from pg_class c where c.oid in ('public.agent_campaigns'::regclass, 'public.agent_campaign_events'::regclass)`)).rows;
  for (const row of tableAcl) {
    if (!row.relrowsecurity || row.a || row.u || row.s || row.x) fail('las tablas, sin permisos y con RLS', row);
  }
  await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'agent_campaigns_already_applied' });
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
const row = async (id) => one(await sql('select * from public.agent_campaigns where id = $1', [id]), 'fila');

const DRAFT = (overrides = {}) => ({
  clave: 'black-november-2026', nombre: 'Black November', etiqueta: 'black-november', macro_etiqueta: 77,
  desde: '2026-11-20T00:00:00-06:00', hasta: '2099-12-01T00:00:00-06:00', asesoramiento: ['USD 37 en México.'],
  palabras_clave: ['Black November'], ...overrides,
});
const VALIDATOR = { contract: 'agent-campaigns-v1', rules_version: 1, product_version: 'v1.33.0', git_sha: 'abcdef0' };
const CLEAN = { blocks: [], warnings: [] };
const CHECK = 'c'.repeat(64);
const LIMPIO = { veredicto: 'limpio', abiertos: 0, items: [] };

const save = (draft, extra = {}) => agent(() => rpc('save_agent_campaign_draft_v1', INSTANCE, extra.id ?? null,
  extra.revision ?? null, draft, extra.purpose ?? 'Black November', AE));
const record = (id, revision, result = CLEAN) => agent(() => rpc('record_agent_campaign_validation_v1', INSTANCE, id,
  revision, VALIDATOR, result, { render: '# Segmento: Black November' }, AE));
const check = (id, fingerprint, report = LIMPIO, sha_ = CHECK) => service(() => rpc('record_agent_campaign_check_v1',
  INSTANCE, id, fingerprint, report, sha_));
const validated = async (key) => {
  const saved = await save(DRAFT({ clave: key, etiqueta: key }));
  if (!saved.ok) fail(`guardar ${key}`, saved);
  const recorded = await record(saved.campaign_id, saved.revision);
  if (recorded.status !== 'validada') fail(`validar ${key}`, recorded);
  return { id: saved.campaign_id, fingerprint: recorded.fingerprint, revision: saved.revision };
};
const cleanValidated = async (key) => {
  const v = await validated(key);
  const checked = await check(v.id, v.fingerprint);
  if (!checked.ok || !checked.clean) fail(`juez limpio ${key}`, checked);
  return v;
};

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
const publish = (id, fingerprint, sha_ = CHECK) => page('approver_publish_agent_campaign_v1', id, fingerprint, sha_);

// 1. El agente.
{
  const saved = await save(DRAFT());
  if (!saved.ok || saved.status !== 'borrador' || saved.revision !== 1 || saved.segment_version !== 1) fail('guardar', saved);
  const redo = await save(DRAFT({ nombre: 'Otro nombre' }), { id: saved.campaign_id, revision: 1 });
  if (!redo.ok || redo.revision !== 2) fail('rehacer', redo);
  const stale = await save(DRAFT(), { id: saved.campaign_id, revision: 1 });
  if (stale.ok || stale.reason_code !== 'agent_campaign_revision_mismatch') fail('la revision vieja', stale);
  const key = await save(DRAFT({ clave: 'otra-clave', etiqueta: 'otra' }), { id: saved.campaign_id, revision: 2 });
  if (key.ok || key.reason_code !== 'agent_campaign_key_is_immutable') fail('la clave no cambia', key);
  await expectError(db, 'un borrador sin forma', () => save({ ...DRAFT(), extra: 1 }),
    { code: '22023', message: 'agent_campaign_draft_invalid' });
  await expectError(db, 'sin entrada', () => { const d = DRAFT(); delete d.palabras_clave; return save(d); },
    { code: '22023', message: 'agent_campaign_draft_invalid' });

  // Con bloqueos queda borrador y sin huella.
  const blocked = await record(saved.campaign_id, 2, { blocks: [{ code: 'campaign_macro_unreadable' }], warnings: [] });
  if (!blocked.ok || blocked.status !== 'borrador' || blocked.blocks !== 1) fail('con bloqueos', blocked);
  if ((await row(saved.campaign_id)).fingerprint !== null) fail('con bloqueos no hay huella');

  const recorded = await record(saved.campaign_id, 2);
  if (!recorded.ok || recorded.status !== 'validada' || !/^[0-9a-f]{64}$/.test(recorded.fingerprint)) fail('validar', recorded);
  const frozen = await row(saved.campaign_id);
  if ((new Date(frozen.expires_at) - new Date(frozen.validated_at)) / 3_600_000 !== 24) fail('24 h', frozen);
  const again = await record(saved.campaign_id, 2);
  if (again.ok || again.reason_code !== 'agent_campaign_not_draft') fail('validar dos veces', again);

  // El agente no publica ni pausa.
  await expectError(db, 'el agente no publica', () => agent(() => rpc('approver_publish_agent_campaign_v1', INSTANCE, S,
    saved.campaign_id, recorded.fingerprint, CHECK)), { code: '42501' });

  // Rehacer una validada (preguntas del juez) le saca la huella y el control.
  await check(saved.campaign_id, recorded.fingerprint, { veredicto: 'abiertos', abiertos: 1, items: [{ pregunta: 'x' }] }, null);
  const redone = await save(DRAFT({ nombre: 'Tercero' }), { id: saved.campaign_id, revision: 2 });
  if (!redone.ok || redone.status !== 'borrador' || redone.revision !== 3) fail('rehacer una validada', redone);
  const cleared = await row(saved.campaign_id);
  if (cleared.fingerprint !== null || cleared.check_report !== null || cleared.validated_at !== null) fail('sin huella', cleared);
  // Un tope.
  await save(DRAFT({ clave: 'c2', etiqueta: 'c2' }));
  await save(DRAFT({ clave: 'c3', etiqueta: 'c3' }));
  await save(DRAFT({ clave: 'c4', etiqueta: 'c4' }));
  await save(DRAFT({ clave: 'c5', etiqueta: 'c5' }));
  const limit = await save(DRAFT({ clave: 'c6', etiqueta: 'c6' }));
  if (limit.ok || limit.reason_code !== 'agent_campaign_open_limit') fail('el tope', limit);
  // Se liberan para lo que sigue.
  await sql(`update public.agent_campaigns set status = 'descartada', closed_at = now(), closed_by_kind = 'sistema',
    closed_by_ref = 'prueba', close_reason = 'prueba' where status = 'borrador'`);
  const list = await agent(() => rpc('list_agent_campaigns_v1', INSTANCE, false, 50));
  if (JSON.stringify(list).includes(DAN) || JSON.stringify(list).includes(AE)) fail('la lista del agente sin IDs', list);
}

// 2. La pagina: sin el si, nada; con el si, publicada.
{
  const v = await validated('pagina-uno');
  const noCheck = await publish(v.id, v.fingerprint);
  if (noCheck.ok || noCheck.reason_code !== 'agent_campaign_check_missing') fail('sin control de calidad', noCheck);
  if ((await row(v.id)).status !== 'validada') fail('sigue validada');
  await check(v.id, v.fingerprint);
  const wrongSha = await publish(v.id, v.fingerprint, 'd'.repeat(64));
  if (wrongSha.ok || wrongSha.reason_code !== 'agent_campaign_check_stale') fail('chequeo_sha viejo', wrongSha);
  await expectError(db, 'otra huella', () => publish(v.id, 'e'.repeat(64)),
    { code: '22023', message: 'agent_campaign_fingerprint_mismatch' });
  await expectError(db, 'sin sesion', () => approver(() => rpc('approver_publish_agent_campaign_v1', INSTANCE,
    sha('sesion-falsa'), v.id, v.fingerprint, CHECK)), { code: '28000' });
  const published = await publish(v.id, v.fingerprint);
  if (!published.ok || published.status !== 'publicada' || published.replayed) fail('publicar', published);
  const again = await publish(v.id, v.fingerprint);
  if (!again.ok || !again.replayed) fail('repetir', again);
  const stored = await row(v.id);
  if (stored.approved_by_ref !== DAN || stored.approved_by_kind !== 'slack') fail('quien aprobo', stored);

  // 5. La lectura del catalogo vivo.
  const live = await service(() => rpc('get_live_agent_campaigns_v1', INSTANCE));
  if (live.campaigns.length !== 1 || live.campaigns[0].campaign_id !== v.id
      || JSON.stringify(live).includes(DAN)) fail('el catalogo vivo', live);

  // 3. Dos vivas: la segunda se rechaza.
  const second = await cleanValidated('pagina-dos');
  const refused = await publish(second.id, second.fingerprint);
  if (refused.ok || refused.reason_code !== 'agent_campaign_live_exists') fail('dos vivas', refused);
  if ((await row(second.id)).status !== 'validada') fail('la segunda sigue validada');
  await expectError(db, 'el indice unico', () => sql(`update public.agent_campaigns set status = 'publicada',
    approved_at = now(), approved_by_kind = 'slack', approved_by_ref = $2 where id = $1`, [second.id, DAN]),
  { code: '23505' });

  // 4. Pausar libera el lugar; reanudar respeta a la que entro; una vencida no retiene.
  const paused = await page('approver_pause_agent_campaign_v1', v.id, 'hasta revisar el precio');
  if (!paused.ok || paused.status !== 'pausada') fail('pausar', paused);
  const pausedRow = await row(v.id);
  if (pausedRow.status !== 'pausada' || pausedRow.paused_by_ref !== DAN) fail('quien pauso', pausedRow);
  const liveAfter = await service(() => rpc('get_live_agent_campaigns_v1', INSTANCE));
  if (liveAfter.campaigns.length !== 0) fail('pausada no se sirve', liveAfter);
  const nowTwo = await publish(second.id, second.fingerprint);
  if (!nowTwo.ok || nowTwo.status !== 'publicada') fail('publicar la segunda', nowTwo);
  const blockedResume = await page('approver_resume_agent_campaign_v1', v.id);
  if (blockedResume.ok || blockedResume.reason_code !== 'agent_campaign_live_exists') fail('reanudar con otra viva', blockedResume);
  await shift(`update public.agent_campaigns set valid_from = now() - interval '2 hours', valid_until = now() - interval '1 hour' where id = $1`, [second.id]);
  const resumed = await page('approver_resume_agent_campaign_v1', v.id);
  if (!resumed.ok || resumed.status !== 'publicada') fail('reanudar con la otra vencida', resumed);
  if ((await row(second.id)).status !== 'terminada') fail('la vencida quedo terminada');
  const finished = await sql(`select actor_kind, actor_ref, reason_code from public.agent_campaign_events
    where campaign_id = $1 and event_type = 'agent_campaign_finished'`, [second.id]);
  if (finished.length !== 1 || finished[0].reason_code !== 'agent_campaign_window_over'
      || finished[0].actor_kind !== 'sistema') fail('reanudar deja el fin de la vencida en el historial', finished);

  // El juez sobre una publicada: sucio no pisa, limpio re-certifica; publicar otra con la viva vencida libera el lugar.
  const keptReport = (await row(v.id)).check_report;
  const dirty = await check(v.id, v.fingerprint, { veredicto: 'abiertos', abiertos: 1,
    items: [{ numero: 1, tipo: 'hueco', pregunta: '¿Hasta cuándo vale?' }] }, null);
  if (dirty.ok || dirty.reason_code !== 'agent_campaign_check_not_clean') fail('sucio no pisa', dirty);
  if (JSON.stringify((await row(v.id)).check_report) !== JSON.stringify(keptReport)) fail('el control limpio sigue en la fila');
  const keptQuestions = await sql(`select data from public.agent_campaign_events where campaign_id = $1
    and event_type = 'agent_campaign_checked' and reason_code = 'agent_campaign_check_not_clean'`, [v.id]);
  if (keptQuestions.length !== 1 || keptQuestions[0].data.items[0].pregunta !== '¿Hasta cuándo vale?') {
    fail('las preguntas de un resultado sucio quedan en el historial', keptQuestions);
  }
  const recert = await check(v.id, v.fingerprint, LIMPIO, 'a'.repeat(64));
  if (!recert.ok) fail('re-certificar', recert);
  if ((await row(v.id)).check_sha !== 'a'.repeat(64)) fail('nuevo sha');
  await shift(`update public.agent_campaigns set valid_from = now() - interval '2 hours', valid_until = now() - interval '1 hour' where id = $1`, [v.id]);
  const third = await cleanValidated('pagina-tres');
  const freed = await publish(third.id, third.fingerprint);
  if (!freed.ok || (await row(v.id)).status !== 'terminada') fail('una vencida no retiene el lugar', freed);
  await page('approver_pause_agent_campaign_v1', third.id, 'fin de la prueba');
  await sql(`update public.agent_campaigns set status = 'terminada', closed_at = now(), closed_by_kind = 'sistema',
    closed_by_ref = 'prueba' where id = $1`, [third.id]);
}

// 4. Descartar y vencer. 5. El juez con otra huella.
{
  const v = await validated('descarte');
  const otherHash = await check(v.id, 'f'.repeat(64));
  if (otherHash.ok || otherHash.reason_code !== 'agent_campaign_check_of_other_content') fail('otra huella', otherHash);
  const discarded = await page('approver_discard_agent_campaign_v1', v.id, 'no va');
  if (!discarded.ok) fail('descartar', discarded);
  const twice = await page('approver_discard_agent_campaign_v1', v.id, 'no va');
  if (!twice.replayed) fail('descartar dos veces', twice);

  const e = await validated('vence');
  await shift(`update public.agent_campaigns set validated_at = now() - interval '25 hours', expires_at = now() - interval '1 minute' where id = $1`, [e.id]);
  const lapsed = await publish(e.id, e.fingerprint);
  if (lapsed.ok || lapsed.reason_code !== 'agent_campaign_expired') fail('vencida', lapsed);
  if ((await row(e.id)).status !== 'vencida') fail('quedo vencida');

  // Con todo en regla, una vigencia que ya termino no se publica.
  const over = await cleanValidated('vigencia-ida');
  await shift(`update public.agent_campaigns set valid_from = now() - interval '2 hours',
    valid_until = now() - interval '1 hour' where id = $1`, [over.id]);
  const late = await publish(over.id, over.fingerprint);
  if (late.ok || late.reason_code !== 'agent_campaign_window_over') fail('publicar sin vigencia', late);
  if ((await row(over.id)).status !== 'validada') fail('sin vigencia sigue validada');
}

// 5. El juez limpio da otras 24 horas para aprobar.
{
  const v = await validated('juez-reloj');
  await shift(`update public.agent_campaigns set expires_at = now() + interval '1 minute' where id = $1`, [v.id]);
  await check(v.id, v.fingerprint);
  const after_ = await row(v.id);
  const hours = (new Date(after_.expires_at) - Date.now()) / 3_600_000;
  if (hours < 23.9) fail('el juez limpio extiende', after_);
}

// 6. Los disparadores.
{
  const v = await cleanValidated('disparadores');
  await expectError(db, 'el contenido congelado', () => sql(
    `update public.agent_campaigns set purpose = 'otra cosa' where id = $1`, [v.id]),
  { code: '55000', message: 'agent_campaign_frozen' });
  await expectError(db, 'un recorrido invalido', () => sql(
    `update public.agent_campaigns set status = 'terminada' where id = $1`, [v.id]),
  { code: '23514', message: 'agent_campaign_transition_invalid' });
  await expectError(db, 'no se borra', () => sql('delete from public.agent_campaigns where id = $1', [v.id]),
    { code: '55000', message: 'agent_campaign_is_permanent' });
  await expectError(db, 'los eventos, solo agregar', () => sql(
    `delete from public.agent_campaign_events where campaign_id = $1`, [v.id]),
  { code: '55000', message: 'agent_campaign_event_is_append_only' });
  const shown = await agent(() => rpc('get_agent_campaign_v1', INSTANCE, v.id));
  const text = JSON.stringify(shown);
  if (text.includes(DAN) || text.includes(AE) || 'approved_by_ref' in shown) fail('el agente no ve IDs', shown);
}

await db.close();
console.log('AGENT_CAMPAIGNS_OK');
