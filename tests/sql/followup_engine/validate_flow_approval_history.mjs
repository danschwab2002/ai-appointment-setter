// El historial de propuestas de la pagina de aprobacion (migracion
// 20261103000600, E-295), con las RPC reales sobre la cadena completa de
// migraciones.
//
// Prueba, con un rojo por pieza:
//   0. la forma: antes de la migracion las dos lecturas no existen (el rojo de
//      todo lo demas); agrega exactamente sus tres funciones y no cambia
//      ninguna otra ni ningun permiso; flow_change_approver ejecuta las dos
//      lecturas y no la interna; ni anon, ni authenticated, ni service_role
//      ejecutan ninguna; correrla dos veces aborta con 55000;
//   1. la lista, como el rol: todas las propuestas (publicada, descartada,
//      vencida y validada), de la mas nueva a la mas vieja, por paginas con
//      cursor, sin repetir ni saltear ninguna; el estado efectivo vencida; quien
//      la pidio, la aprobo y la cerro; la version que genero la publicada; y
//      ni el «para que» ni el motivo del descarte. Rojos: p_limit 0, 51 y null
//      (22023), un cursor de otra instancia o inexistente (P0002), otra aliada
//      (22023) y sin sesion (28000);
//   2. el detalle: la linea de tiempo de la publicada, la mas vieja primero,
//      con el actor de cada evento; el motivo y quien descarto la descartada; y
//      los mismos rojos de sesion y de propuesta;
//   3. lo que nadie mas puede: la interna para el rol, y las dos lecturas para
//      anon y service_role, 42501.
//
// Datos sinteticos, los de buildWorld (el manifiesto de ATT1). Los IDs de Slack
// son de forma valida e inventados.
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261103000600_flow_approval_history_v1.sql';
const LIST = 'approver_list_flow_changes_v1(jsonb,text,uuid,integer)';
const RECORD = 'approver_read_flow_change_record_v1(jsonb,text,uuid)';
const INTERNAL = '_flow_approval_published_versions(uuid)';
const results = {};
const fixture = (path) => JSON.parse(readFileSync(join(root, 'tests/fixtures', path), 'utf8'));
const BRIDGE = fixture('flow_change_new_flow/ae_bienvenida_20261024.json');
const sha = (text) => createHash('sha256').update(text).digest('hex');

// ---------------------------------------------------------------------------
// 0. La forma de la migracion, en una base propia: antes y despues.
// ---------------------------------------------------------------------------
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature.replace(/^public\./, ''), row]));

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
// Despues va solo la de si la persona ya es cliente (20261103000700, E-297), que no toca la pagina.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261103000700_customer_status_v1.sql', '20261103000900_flow_engine_native_payment_failure_branches_v1.sql', '20261103001000_handoff_wait_alerts_v1.sql', '20261103001300_flow_engine_native_segment_label_v1.sql', '20261103001500_segment_entry_answers_v1.sql',
  // El origen de un recorrido nativo para la campana por landing (20261103001700, E-321): una lectura de service_role.
  '20261103001700_flow_engine_native_campaign_origin_v1.sql',
  // El cierre del caso por el cupon de una campana (20261106000100, E-339): una RPC de service_role.
  '20261106000100_recovery_case_segment_coupon_close_v1.sql',
  // El rechazo de la cuenta espera a que la cuenta vuelva (20261107000100, E-348): dos tablas y ocho RPC de service_role.
  '20261107000100_meta_account_hold_retry_v1.sql',
  // El almacen dice quien ejecuta cada flujo (20261108000100, E-353): una clave mas en read_flow_store_v1.
  '20261108000100_flow_store_route_executor_v1.sql',
  // La base no publica una version nativa que el motor no ejecuta (20261108004100, E-357).
  '20261108004100_flow_change_publish_native_version_guard_v1.sql',
  // Las campanas del Automation Expert como dato (20261108005000, E-355).
  '20261108005000_agent_campaigns_v1.sql',
  // El sondeo de Meta y el aviso en el hilo (20261109000100, E-361).
  '20261109000100_meta_template_request_notices_v1.sql',
  // La oferta de la campana en el link del agente (20261110000500, E-410).
  '20261110000500_campaign_offer_checkout_issuance_v1.sql',
  '20261111000100_close_recovery_case_without_pending_touch_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de si la persona ya es cliente`, names.slice(position + 1));
}
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
await applyFile(shapeDb, join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of names.slice(0, position)) await applyFile(shapeDb, join(root, 'supabase/migrations', name));
const before = await snapshot(shapeDb);
if (before.has(LIST) || before.has(RECORD)) fail('las lecturas ya existian antes de la migracion');
const approverBefore = (await shapeDb.query(`
  select p.oid::regprocedure::text as signature from pg_proc p
   where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
     and has_function_privilege('flow_change_approver', p.oid, 'execute')`)).rows
  .map((row) => row.signature.replace(/^public\./, ''));
const target = join(root, 'supabase/migrations', MIGRATION);
await applyFile(shapeDb, target);
const after = await snapshot(shapeDb);
{
  const added = [...after.keys()].filter((signature) => !before.has(signature));
  const removed = [...before.keys()].filter((signature) => !after.has(signature));
  const changed = [...before.keys()].filter((signature) => after.has(signature)
    && after.get(signature).definition !== before.get(signature).definition);
  const aclChanged = [...before.keys()].filter((signature) => after.has(signature)
    && ['service_x', 'api_x', 'acl'].some((key) => after.get(signature)[key] !== before.get(signature)[key]));
  if (!sameSet(added, [LIST, RECORD, INTERNAL]) || removed.length || changed.length || aclChanged.length) {
    fail('la migracion cambio otra cosa', { added, removed, changed, aclChanged });
  }
  for (const signature of added) {
    const row = after.get(signature);
    if (row.api_x || row.service_x) fail(`${signature}: la API la ejecuta`, row);
  }
  const approverAfter = (await shapeDb.query(`
    select p.oid::regprocedure::text as signature from pg_proc p
     where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
       and has_function_privilege('flow_change_approver', p.oid, 'execute')`)).rows
    .map((row) => row.signature.replace(/^public\./, ''));
  if (!sameSet(approverAfter, [...approverBefore, LIST, RECORD])) {
    fail('el rol ejecuta otra cosa que lo de antes y las dos lecturas', approverAfter);
  }
  const error = await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'flow_approval_history_already_applied' });
  results.forma = { agregadas: added.length, rol: approverAfter.length, segunda_corrida: error.message };
}
await shapeDb.close();

// ---------------------------------------------------------------------------
// El mundo: ATT1, con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, rpc } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const INSTANCE_ZONE = { ...INSTANCE, time_zone: A.timezone };
const OTHER = { ...INSTANCE, ally_ref: 'otra-aliada' };
const AE = 'U0123456789';
const DAN = 'U0DANSCHWAB1';
const BRAYAN = 'U0BRAYANGHL2';
const TEAM = 'T0LANCEMOS01';
const SHA = 'a'.repeat(64);
const CLEAN = { blocks: [], warnings: [] };
const DISCARD_REASON = 'no hace falta, motivo de prueba';
const as = async (role, run) => {
  await db.exec(`set role ${role}`);
  // Si falla, la transaccion de refused queda abortada: el rollback deshace tambien el set role.
  const out = await run();
  await db.exec('reset role');
  return out;
};
const approver = (run) => as('flow_change_approver', run);
const refused = async (role, run) => {
  await db.exec('begin');
  try {
    await as(role, run);
  } catch (error) {
    await db.exec('rollback');
    return error;
  }
  await db.exec('rollback');
  return null;
};
const inMinutes = async (minutes) => new Date((await world.dbNow()).getTime() + minutes * 60_000).toISOString();
let serial = 0;
const signIn = async (userId) => {
  serial += 1;
  const state = sha(`h-state-${serial}`);
  const session = sha(`h-session-${serial}`);
  await approver(async () => {
    await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha(`h-nonce-${serial}`), '/flow-approval/',
      await inMinutes(10));
    await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
    const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, TEAM, userId,
      await inMinutes(60));
    if (!created.ok) fail('entrar', created);
  });
  return session;
};
const instanceBase = async () => (await db.query('select public._flow_store_base($1, $2, $3::uuid[]) b',
  [A.tenant, A.ally, []])).rows[0].b;
const bridgeItem = (flujo) => {
  const item = structuredClone(BRIDGE.items[0]);
  item.flujo = flujo;
  item.definicion.flujo = flujo;
  item.definicion.topes.bolsa = `${A.funnel}-${flujo}`.slice(0, 40);
  return item;
};
const agentProposes = async (flujo) => {
  const validatedBase = await instanceBase();
  return as('automation_expert_change', async () => {
    const saved = await rpc('save_flow_change_draft_v1', INSTANCE, null, null, [bridgeItem(flujo)], {},
      BRIDGE.purpose, AE);
    if (!saved.ok) fail('guardar', saved);
    const recorded = await rpc('record_flow_change_validation_v1', INSTANCE, saved.proposal_id, saved.revision,
      validatedBase, BRIDGE.validator, BRIDGE.result, BRIDGE.preview, AE);
    if (recorded.status !== 'validada') fail('validar', recorded);
    return { id: saved.proposal_id, fingerprint: recorded.fingerprint };
  });
};

const S = await signIn(DAN);
const B = await signIn(BRAYAN);
const NOT_A_SESSION = sha('no-es-una-sesion');
// Cuatro propuestas, en este orden: la que se publica, la que se descarta, la que vence y la que espera.
const published = await agentProposes('historial_uno');
const discarded = await agentProposes('historial_dos');
const expired = await agentProposes('historial_tres');
const waiting = await agentProposes('historial_cuatro');
{
  const approved = await approver(() => rpc('approver_approve_flow_change_v1', INSTANCE, B, published.id,
    published.fingerprint));
  if (!approved.ok) fail('aprobar', approved);
  const done = await approver(() => rpc('approver_publish_flow_change_v1', INSTANCE_ZONE, B, published.id,
    published.fingerprint, { fingerprint: published.fingerprint, validator: BRIDGE.validator, result: CLEAN,
      catalog_sha256: SHA, validated_at: new Date().toISOString() }));
  if (!done.ok) fail('publicar', done);
  const gone = await approver(() => rpc('approver_discard_flow_change_v1', INSTANCE, S, discarded.id, DISCARD_REASON));
  if (!gone.ok) fail('descartar', gone);
  // Vencer sin esperar 24 h: se corre la validacion un dia y un minuto atras, como duenio.
  // Sin los disparadores, el metodo de validate_flow_change_list.mjs.
  await db.exec('set session_replication_role = replica');
  await sql(`update public.flow_change_proposals set validated_at = validated_at - interval '1441 minutes',
    expires_at = expires_at - interval '1441 minutes' where id = $1`, [expired.id]);
  await db.exec('set session_replication_role = origin');
}
const ORDER = [waiting.id, expired.id, discarded.id, published.id];

// ---------------------------------------------------------------------------
// 1. La lista.
// ---------------------------------------------------------------------------
{
  // Pagina por pagina de a 3, hasta que no haya mas: el mundo deja tambien sus propuestas.
  const pages = [];
  let cursor = null;
  for (let guard = 0; guard < 50; guard += 1) {
    const page = await approver(() => rpc('approver_list_flow_changes_v1', INSTANCE, S, cursor, 3));
    pages.push(page);
    if (!page.has_more) break;
    cursor = page.next_before;
  }
  const first = pages[0];
  const last = pages.at(-1);
  const ids = pages.flatMap((page) => page.proposals.map((row) => row.proposal_id));
  if (first.proposals.length !== 3 || first.has_more !== true || first.next_before !== first.proposals[2].proposal_id
      || JSON.stringify(ids.slice(0, 4)) !== JSON.stringify(ORDER) || new Set(ids).size !== ids.length
      || first.total !== ids.length || last.has_more !== false || last.next_before !== null
      || pages.slice(0, -1).some((page) => page.proposals.length !== 3) || pages.length < 2) {
    fail('la lista por paginas', pages.map((page) => ({ n: page.proposals.length, more: page.has_more })));
  }
  const all = await approver(() => rpc('approver_list_flow_changes_v1', INSTANCE, S, null, 50));
  if (JSON.stringify(all.proposals.map((row) => row.proposal_id)) !== JSON.stringify(ids)) {
    fail('una pagina de 50 es la misma lista', all);
  }
  const byId = Object.fromEntries(all.proposals.map((row) => [row.proposal_id, row]));
  const p = byId[published.id];
  const d = byId[discarded.id];
  const e = byId[expired.id];
  const w = byId[waiting.id];
  const states = [p, d, e, w].map((row) => `${row.status}/${row.effective_status}`);
  if (JSON.stringify(states) !== JSON.stringify(['publicada/publicada', 'descartada/descartada', 'validada/vencida',
    'validada/validada'])) {
    fail('los estados', states);
  }
  if (p.requested_by_kind !== 'automation_expert' || p.requested_by_ref !== AE || p.approved_by_kind !== 'slack'
      || p.approved_by_ref !== BRAYAN || p.published_at === null || p.versions.length !== 1
      || p.versions[0].flujo !== 'historial_uno' || p.versions[0].version !== 1
      || p.flows[0].flujo !== 'historial_uno' || p.flows[0].tipo !== 'nuevo') {
    fail('la publicada en la lista', p);
  }
  if (d.closed_by_kind !== 'slack' || d.closed_by_ref !== DAN || d.closed_at === null || d.versions.length !== 0
      || d.last_event?.event_type !== 'flow_change_discarded' || d.last_event?.actor_ref !== DAN) {
    fail('la descartada en la lista', d);
  }
  const text = JSON.stringify(all);
  if (text.includes(BRIDGE.purpose) || text.includes(DISCARD_REASON) || 'purpose' in p || 'close_reason' in d
      || 'items' in p || 'validation' in p) {
    fail('la lista lleva el para que, el motivo o la vista previa', Object.keys(p));
  }
  const errors = {
    cero: () => rpc('approver_list_flow_changes_v1', INSTANCE, S, null, 0),
    cincuenta_y_uno: () => rpc('approver_list_flow_changes_v1', INSTANCE, S, null, 51),
    sin_tope: () => rpc('approver_list_flow_changes_v1', INSTANCE, S, null, null),
    cursor_inexistente: () => rpc('approver_list_flow_changes_v1', INSTANCE, S, '00000000-0000-4000-8000-000000000999', 5),
    otra_aliada: () => rpc('approver_list_flow_changes_v1', OTHER, S, null, 5),
    sin_sesion: () => rpc('approver_list_flow_changes_v1', INSTANCE, NOT_A_SESSION, null, 5),
  };
  const expected = {
    cero: ['22023', 'flow_approval_history_parameters_invalid'],
    cincuenta_y_uno: ['22023', 'flow_approval_history_parameters_invalid'],
    sin_tope: ['22023', 'flow_approval_history_parameters_invalid'],
    cursor_inexistente: ['P0002', 'flow_change_not_found'],
    otra_aliada: ['22023', 'flow_store_instance_not_bound'],
    sin_sesion: ['28000', 'flow_approval_session_invalid'],
  };
  for (const [key, run] of Object.entries(errors)) {
    const error = await refused('flow_change_approver', run);
    if (error?.code !== expected[key][0] || error.message !== expected[key][1]) {
      fail(`la lista: ${key}`, error ? { code: error.code, message: error.message } : 'paso');
    }
  }
  results.lista = { total: all.total, paginas: pages.map((page) => page.proposals.length), estados: states,
    rojos: Object.keys(errors) };
}

// ---------------------------------------------------------------------------
// 2. El detalle.
// ---------------------------------------------------------------------------
{
  const record = await approver(() => rpc('approver_read_flow_change_record_v1', INSTANCE, S, published.id));
  const types = record.events.map((event) => event.event_type);
  const times = record.events.map((event) => event.created_at);
  if (record.effective_status !== 'publicada' || record.approved_by_ref !== BRAYAN || record.versions.length !== 1
      || record.events_total !== record.events.length || types[0] !== 'flow_change_draft_saved'
      || !types.includes('flow_change_validated') || !types.includes('flow_change_published')
      || JSON.stringify([...times].sort()) !== JSON.stringify(times)
      || record.events.find((event) => event.event_type === 'flow_change_approved')?.actor_ref !== BRAYAN
      || record.events.some((event) => 'data' in event)) {
    fail('la linea de tiempo de la publicada', record);
  }
  const gone = await approver(() => rpc('approver_read_flow_change_record_v1', INSTANCE, S, discarded.id));
  if (gone.close_reason !== DISCARD_REASON || gone.closed_by_kind !== 'slack' || gone.closed_by_ref !== DAN
      || gone.versions.length !== 0 || gone.events.at(-1)?.event_type !== 'flow_change_discarded') {
    fail('la descartada', gone);
  }
  const late = await approver(() => rpc('approver_read_flow_change_record_v1', INSTANCE, S, expired.id));
  if (late.effective_status !== 'vencida' || late.status !== 'validada') fail('la vencida', late);
  const errors = {
    inexistente: () => rpc('approver_read_flow_change_record_v1', INSTANCE, S, '00000000-0000-4000-8000-000000000999'),
    otra_aliada: () => rpc('approver_read_flow_change_record_v1', OTHER, S, published.id),
    sin_sesion: () => rpc('approver_read_flow_change_record_v1', INSTANCE, NOT_A_SESSION, published.id),
  };
  const expected = {
    inexistente: ['P0002', 'flow_change_not_found'],
    otra_aliada: ['22023', 'flow_store_instance_not_bound'],
    sin_sesion: ['28000', 'flow_approval_session_invalid'],
  };
  for (const [key, run] of Object.entries(errors)) {
    const error = await refused('flow_change_approver', run);
    if (error?.code !== expected[key][0] || error.message !== expected[key][1]) {
      fail(`el detalle: ${key}`, error ? { code: error.code, message: error.message } : 'paso');
    }
  }
  results.detalle = { eventos: types, descartada_por: gone.closed_by_kind, vencida: late.effective_status };
}

// ---------------------------------------------------------------------------
// 3. Lo que nadie mas puede.
// ---------------------------------------------------------------------------
{
  const denied = {
    interna_como_rol: ['flow_change_approver', () => db.query(`select public._flow_approval_published_versions($1)`,
      [published.id])],
    lista_como_anon: ['anon', () => rpc('approver_list_flow_changes_v1', INSTANCE, S, null, 5)],
    detalle_como_anon: ['anon', () => rpc('approver_read_flow_change_record_v1', INSTANCE, S, published.id)],
    lista_como_service_role: ['service_role', () => rpc('approver_list_flow_changes_v1', INSTANCE, S, null, 5)],
    detalle_como_service_role: ['service_role', () => rpc('approver_read_flow_change_record_v1', INSTANCE, S,
      published.id)],
  };
  for (const [label, [role, run]] of Object.entries(denied)) {
    const error = await refused(role, run);
    if (error?.code !== '42501') fail(`no puede: ${label}`, error ? { code: error.code, message: error.message } : 'paso');
  }
  results.nadie_mas = Object.keys(denied);
}

console.log(JSON.stringify({ validate_flow_approval_history: 'ok', ...results }, null, 2));
