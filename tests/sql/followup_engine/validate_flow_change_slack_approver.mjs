// La pagina de aprobacion en la base (migracion 20261028000100, A′ PR 2), con
// las RPC reales sobre la cadena completa de migraciones.
//
// Contrato: docs/contracts/flow-change-cycle-v1.md, «El sí por un link en el
// chat del Automation Expert (1.9.0)», «Lo que pide a la base».
//
// Prueba, con un rojo por pieza:
//   0. la forma: agrega exactamente sus quince funciones y sus dos tablas;
//      cambia solo operator_flow_cohort_v1, con su marca y sin tocar permisos;
//      el rol ejecuta exactamente sus trece y ninguna otra de public; ni anon,
//      ni authenticated, ni service_role ejecutan ninguna ni tocan las
//      tablas; correrla dos veces aborta;
//   1. la sesion: crear el state, consumirlo una sola vez (el hash del nonce
//      vuelve), crear la sesion con ese state (una sola), validarla, y
//      revocarla. Rojos: el state consumido dos veces, una sesion sin state
//      consumido, un state de otra instancia, un vencimiento de mas de 8 h, la
//      sesion revocada, vencida o de otra instancia, y el tope de 200 states
//      pendientes;
//   2. el ciclo de la pagina, como el rol: releer una propuesta del agente con
//      su vista previa, las otras abiertas del mismo flujo (sin su para que),
//      la lista, aprobar (approved_by slack con el ID de la sesion, no de un
//      parametro), publicar y crear el flujo nativo en cohorte, sumar un
//      telefono a la cohorte (added_by slack, sin telefono en la respuesta ni
//      en el evento), pausar con la generacion y descartar. Sin sesion, cada
//      envoltorio da 28000;
//   3. lo que el rol no puede: las operator_*, los envoltorios del agente, el
//      SELECT y el INSERT directos, todo 42501;
//   4. la cohorte del operador sigue igual: agrega como operador y saca.
//
// Datos sinteticos, los de buildWorld: personas de example.test con
// telefonos 1202555xxxx (rango de ficcion) y el manifiesto de ATT1. Los IDs de
// Slack son de forma valida e inventados: no hay captura de Slack en esta
// prueba (la base no habla con Slack; el OIDC es del bridge, A′ PR 3a).
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261028000100_flow_change_slack_approver_v1.sql';
const TZ = 'timestamp with time zone';
const results = {};
const fixture = (path) => JSON.parse(readFileSync(join(root, 'tests/fixtures', path), 'utf8'));
const BRIDGE = fixture('flow_change_new_flow/ae_bienvenida_20261024.json');
const sha = (text) => createHash('sha256').update(text).digest('hex');

const WRAPPERS = [
  'begin_flow_approval_oidc_v1(jsonb,text,text,text,timestamp with time zone)',
  'consume_flow_approval_oidc_v1(jsonb,text)',
  'create_flow_approval_session_v1(jsonb,text,text,text,text,timestamp with time zone)',
  'validate_flow_approval_session_v1(jsonb,text)',
  'revoke_flow_approval_session_v1(jsonb,text)',
  'approver_get_flow_change_proposal_v1(jsonb,text,uuid)',
  'approver_read_flow_store_v1(jsonb,text)',
  'approver_read_open_flow_changes_v1(jsonb,text,uuid)',
  'approver_approve_flow_change_v1(jsonb,text,uuid,text)',
  'approver_publish_flow_change_v1(jsonb,text,uuid,text,jsonb)',
  'approver_pause_flow_v1(jsonb,text,text,text,bigint,text)',
  'approver_discard_flow_change_v1(jsonb,text,uuid,text)',
  'approver_flow_cohort_v1(jsonb,text,text,text,text,text)',
];
const INTERNAL = [
  '_flow_approval_session_actor(text,text,text)',
  '_flow_cohort_change(text,text,text,text,text,text,text,text,text)',
];

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
// Despues va solo la del carrito nativo (E-161), que no toca lo que esta prueba mira.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261028000200_flow_engine_native_cart_v1.sql',
  // Sumar ofertas a un scope publicado (E-163): solo el trigger de pilot_scope_versions.
  '20261028000300_pilot_scope_offer_append_v1.sql',
  // La compra con nuestro link cuenta siempre (E-169): el helper del cierre por el link y el correlador.
  '20261028000400_purchase_by_our_link_counts_v1.sql',
  // El doble caso del carrito (E-204): la admision nativa y el planificador del carrito, por reemplazo contado.
  '20261030000100_flow_engine_native_legacy_double_case_v1.sql',
  // El pago rechazado nativo (E-207): el disparador pago_fallido, por reemplazo contado.
  '20261030000200_flow_engine_native_payment_failure_v1.sql',
  // La lista de las propuestas del AE (E-208): una lectura nueva, sin tocar ninguna funcion.
  '20261030000300_flow_change_list_v1.sql',
  // Los frenos de la compra ven nuestro link (E-215): reemplazos contados en los dos frenos portables.
  '20261030000400_portable_purchase_stops_see_our_link_v1.sql',
  // El primer contacto nativo (E-247): el disparador intencion, por reemplazos contados.
  '20261101000100_flow_engine_native_intent_v1.sql',
  // El seguimiento a 24 h nativo (E-251): el silencio del agente, por reemplazos contados.
  '20261101000200_flow_engine_native_silence_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la del carrito nativo`, names.slice(position + 1));
}
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
    && after.get(signature).definition !== before.get(signature).definition);
  const aclChanged = [...before.keys()].filter((signature) => after.has(signature)
    && ['service_x', 'api_x', 'acl'].some((key) => after.get(signature)[key] !== before.get(signature)[key]));
  if (!sameSet(added, [...WRAPPERS, ...INTERNAL]) || removed.length !== 0
      || !sameSet(changed, ['operator_flow_cohort_v1(jsonb,text,text,text,text,text,text)', 'record_flow_engine_route_change()']) || aclChanged.length !== 0) {
    fail('la migracion cambio otra cosa', { added, removed, changed, aclChanged });
  }
  if (!after.get('operator_flow_cohort_v1(jsonb,text,text,text,text,text,text)').definition
    .includes('flow_change_slack_approver(20261028000100)')) {
    fail('operator_flow_cohort_v1 sin su marca');
  }
  for (const signature of [...WRAPPERS, ...INTERNAL]) {
    const row = after.get(signature);
    if (row.api_x || row.service_x) fail(`${signature}: la API la ejecuta`, row);
  }
  const approverExecutes = (await shapeDb.query(`
    select p.oid::regprocedure::text as signature from pg_proc p
     where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
       and has_function_privilege('flow_change_approver', p.oid, 'execute')`)).rows
    .map((row) => row.signature.replace(/^public\./, ''));
  if (!sameSet(approverExecutes, WRAPPERS)) fail('el rol ejecuta otra cosa que sus trece', approverExecutes);
  const role = one((await shapeDb.query(`select rolcanlogin, rolinherit, rolsuper, rolbypassrls, rolcreaterole
    from pg_roles where rolname = 'flow_change_approver'`)).rows, 'rol');
  if (role.rolcanlogin || role.rolinherit || role.rolsuper || role.rolbypassrls || role.rolcreaterole) {
    fail('los atributos del rol', role);
  }
  const membership = (await shapeDb.query(`select m.rolname member, a.inherit_option, a.set_option
    from pg_auth_members a join pg_roles m on m.oid = a.member
    where a.roleid = 'flow_change_approver'::regrole`)).rows;
  if (!membership.some((row) => row.member === 'authenticator' && row.set_option && !row.inherit_option)) {
    fail('authenticator asume el rol', membership);
  }
  for (const table of ['flow_approval_oidc_states', 'flow_approval_sessions']) {
    const grants = (await shapeDb.query(`select grantee, privilege_type from information_schema.role_table_grants
      where table_schema = 'public' and table_name = $1 and grantee in ('anon', 'authenticated', 'service_role',
      'flow_change_approver')`, [table])).rows;
    if (grants.length !== 0) fail(`${table}: permisos para la API o el rol`, grants);
  }
  const error = await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'flow_change_slack_approver_already_applied' });
  // El bloque del rol falla cerrado si alguien mas lo asume.
  const sql = readFileSync(target, 'utf8');
  const roleBlock = sql.slice(sql.indexOf('do $role$'), sql.indexOf('$role$;') + 7);
  await shapeDb.exec(roleBlock);
  await shapeDb.exec('grant flow_change_approver to anon');
  await expectError(shapeDb, 'el rol con anon como miembro', () => shapeDb.exec(roleBlock),
    { code: '55000', message: 'flow_change_approver_role_unsafe' });
  results.forma = { agregadas: added.length, cambiadas: changed.length, segunda_corrida: error.message };
}
await shapeDb.close();

// ---------------------------------------------------------------------------
// El mundo: ATT1 (A), con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, person, rpc, control } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const INSTANCE_ZONE = { ...INSTANCE, time_zone: A.timezone };
const OTHER = { ...INSTANCE, ally_ref: 'otra-aliada' };
const AE = 'U0123456789';
const DAN = 'U0DANSCHWAB1';
const BRAYAN = 'U0BRAYANGHL2';
const TEAM = 'T0LANCEMOS01';
const SHA = 'a'.repeat(64);
const CLEAN = { blocks: [], warnings: [] };
const as = async (role, run) => {
  await db.exec(`set role ${role}`);
  let failed = false;
  try {
    return await run();
  } catch (error) {
    failed = true;
    throw error;
  } finally {
    if (!failed) await db.exec('reset role');
  }
};
const approver = (run) => as('flow_change_approver', run);
// Un error esperado como el rol, sin deshacer lo de antes.
const refused = async (run) => {
  await db.exec('begin');
  try {
    await approver(run);
  } catch (error) {
    await db.exec('rollback');
    return error;
  }
  await db.exec('rollback');
  return null;
};
const inMinutes = async (minutes) => new Date((await world.dbNow()).getTime() + minutes * 60_000).toISOString();

// Entrar como lo hace el bridge: state y nonce, consumir, sesion.
let serial = 0;
const signIn = async (userId, { instance = INSTANCE, returnPath = '/flow-approval/', hours = 8 } = {}) => {
  serial += 1;
  const state = sha(`state-${serial}`);
  const nonce = sha(`nonce-${serial}`);
  const session = sha(`session-${serial}`);
  return approver(async () => {
    const begun = await rpc('begin_flow_approval_oidc_v1', instance, state, nonce, returnPath, await inMinutes(10));
    const consumed = await rpc('consume_flow_approval_oidc_v1', instance, state);
    const created = await rpc('create_flow_approval_session_v1', instance, state, session, TEAM, userId,
      await inMinutes(hours * 60));
    return { state, nonce, session, begun, consumed, created };
  });
};

// ---------------------------------------------------------------------------
// 1. La sesion.
// ---------------------------------------------------------------------------
const DAN_SESSION = {};
{
  const entered = await signIn(DAN, { returnPath: '/flow-approval/p/00000000-0000-4000-8000-000000000001' });
  if (!entered.begun.ok || entered.consumed.nonce_hash !== entered.nonce
      || entered.consumed.return_path !== '/flow-approval/p/00000000-0000-4000-8000-000000000001'
      || !entered.created.ok) {
    fail('entrar', entered);
  }
  Object.assign(DAN_SESSION, entered);
  const valid = await approver(() => rpc('validate_flow_approval_session_v1', INSTANCE, entered.session));
  if (!valid.ok || valid.slack_user_id !== DAN || valid.slack_team_id !== TEAM) fail('validar la sesion', valid);
  const stored = one(await sql('select * from public.flow_approval_sessions where session_hash = $1', [entered.session]),
    'sesion');
  const storedText = JSON.stringify(stored);
  if (storedText.includes(`state-`) || stored.tenant_ref !== A.tenant || stored.revoked_at !== null) {
    fail('la fila de la sesion', stored);
  }

  const twice = await refused(() => rpc('consume_flow_approval_oidc_v1', INSTANCE, entered.state));
  const secondSession = await refused(() => rpc('create_flow_approval_session_v1', INSTANCE, entered.state,
    sha('otra-sesion'), TEAM, DAN, new Date(Date.now() + 3600_000).toISOString()));
  const unconsumedState = sha('sin-consumir');
  const unconsumed = await refused(async () => {
    await rpc('begin_flow_approval_oidc_v1', INSTANCE, unconsumedState, sha('n-sin-consumir'), '/flow-approval/',
      await inMinutes(5));
    return rpc('create_flow_approval_session_v1', INSTANCE, unconsumedState, sha('s-sin-consumir'), TEAM, DAN,
      await inMinutes(60));
  });
  const otherInstanceState = sha('otra-instancia');
  const otherInstance = await refused(async () => {
    await rpc('begin_flow_approval_oidc_v1', INSTANCE, otherInstanceState, sha('n-otra'), '/flow-approval/',
      await inMinutes(5));
    return rpc('consume_flow_approval_oidc_v1', OTHER, otherInstanceState);
  });
  const tooLong = await refused(async () => {
    const state = sha('largo');
    await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha('n-largo'), '/flow-approval/', await inMinutes(5));
    await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
    return rpc('create_flow_approval_session_v1', INSTANCE, state, sha('s-largo'), TEAM, DAN, await inMinutes(9 * 60));
  });
  const stateTooLong = await refused(async () => rpc('begin_flow_approval_oidc_v1', INSTANCE, sha('st-largo'),
    sha('n-st-largo'), '/flow-approval/', await inMinutes(11)));
  const badPath = await refused(async () => rpc('begin_flow_approval_oidc_v1', INSTANCE, sha('ruta'), sha('n-ruta'),
    'https://example.test/', await inMinutes(5)));
  const otherInstanceSession = await refused(() => rpc('validate_flow_approval_session_v1', OTHER, entered.session));
  const codes = { twice, secondSession, unconsumed, otherInstance, otherInstanceSession, tooLong, stateTooLong, badPath };
  const expected = {
    twice: ['28000', 'flow_approval_oidc_state_invalid'],
    secondSession: ['28000', 'flow_approval_oidc_state_invalid'],
    unconsumed: ['28000', 'flow_approval_oidc_state_invalid'],
    // Una aliada que la base no tiene ni llega a mirar el state o la sesion.
    otherInstance: ['22023', 'flow_store_instance_not_bound'],
    otherInstanceSession: ['22023', 'flow_store_instance_not_bound'],
    tooLong: ['22023', 'flow_approval_session_expiry_invalid'],
    stateTooLong: ['22023', 'flow_approval_oidc_expiry_invalid'],
    badPath: ['22023', 'flow_approval_return_path_invalid'],
  };
  for (const [key, [code, message]] of Object.entries(expected)) {
    if (codes[key]?.code !== code || codes[key]?.message !== message) {
      fail(`la sesion: ${key}`, codes[key] ? { code: codes[key].code, message: codes[key].message } : 'paso');
    }
  }

  // Revocar: la siguiente validacion falla, y revocar otra vez contesta igual.
  const leaving = await signIn(DAN);
  const revoked = await approver(() => rpc('revoke_flow_approval_session_v1', INSTANCE, leaving.session));
  const again = await approver(() => rpc('revoke_flow_approval_session_v1', INSTANCE, leaving.session));
  const afterRevoke = await refused(() => rpc('validate_flow_approval_session_v1', INSTANCE, leaving.session));
  if (!revoked.ok || !again.ok || afterRevoke?.message !== 'flow_approval_session_invalid') {
    fail('revocar', { revoked, again, afterRevoke: afterRevoke?.message });
  }
  // Vencida: se corre el vencimiento a mano, como duenio.
  const expiring = await signIn(DAN);
  await sql(`update public.flow_approval_sessions set issued_at = issued_at - interval '9 hours',
    expires_at = issued_at - interval '9 hours' + interval '8 hours' where session_hash = $1`, [expiring.session]);
  const expired = await refused(() => rpc('validate_flow_approval_session_v1', INSTANCE, expiring.session));
  if (expired?.message !== 'flow_approval_session_invalid') fail('una sesion vencida', expired?.message ?? 'paso');

  // El tope de 200 states pendientes por instancia.
  await sql(`insert into public.flow_approval_oidc_states (state_hash, tenant_ref, ally_ref, nonce_hash, return_path,
    expires_at) select encode(sha256(convert_to('tope-' || i, 'UTF8')), 'hex'), $1, $2,
    encode(sha256(convert_to('n-tope-' || i, 'UTF8')), 'hex'), '/flow-approval/', clock_timestamp() + interval '5 minutes'
    from generate_series(1, 200) i`, [A.tenant, A.ally]);
  const busy = await approver(async () => rpc('begin_flow_approval_oidc_v1', INSTANCE, sha('el-201'), sha('n-201'),
    '/flow-approval/', await inMinutes(5)));
  if (busy.ok || busy.reason_code !== 'flow_approval_oidc_busy') fail('el tope de states', busy);
  await sql(`delete from public.flow_approval_oidc_states where state_hash in (select encode(sha256(convert_to('tope-' || i,
    'UTF8')), 'hex') from generate_series(1, 200) i)`);
  results.sesion = Object.fromEntries(Object.entries(codes).map(([key, error]) => [key, error.message]));
  results.sesion.tope = busy.reason_code;
}

// ---------------------------------------------------------------------------
// 2. El ciclo de la pagina, como el rol.
// ---------------------------------------------------------------------------
const instanceBase = async () => (await db.query('select public._flow_store_base($1, $2, $3::uuid[]) b',
  [A.tenant, A.ally, []])).rows[0].b;
const bridgeItem = (flujo) => {
  const item = structuredClone(BRIDGE.items[0]);
  item.flujo = flujo;
  item.definicion.flujo = flujo;
  item.definicion.topes.bolsa = `${A.funnel}-${flujo}`.slice(0, 40);
  return item;
};
const agentProposes = async (items) => {
  const validatedBase = await instanceBase();
  return as('automation_expert_change', async () => {
    const saved = await rpc('save_flow_change_draft_v1', INSTANCE, null, null, items, {}, BRIDGE.purpose, AE);
    if (!saved.ok) fail('guardar', saved);
    const recorded = await rpc('record_flow_change_validation_v1', INSTANCE, saved.proposal_id, saved.revision,
      validatedBase, BRIDGE.validator, BRIDGE.result, BRIDGE.preview, AE);
    if (recorded.status !== 'validada') fail('validar', recorded);
    return { id: saved.proposal_id, fingerprint: recorded.fingerprint };
  });
};
const S = DAN_SESSION.session;
const NOT_A_SESSION = sha('no-es-una-sesion');
let welcomeFlow;
{
  const welcome = await agentProposes([bridgeItem('bienvenida')]);
  const rival = await agentProposes([bridgeItem('bienvenida')]);
  const unrelated = await agentProposes([bridgeItem('otra_cosa')]);

  const shown = await approver(() => rpc('approver_get_flow_change_proposal_v1', INSTANCE, S, welcome.id));
  if (shown.status !== 'validada' || shown.fingerprint !== welcome.fingerprint
      || shown.validation?.preview?.simulacion?.status !== 'built') {
    fail('releer la propuesta con su vista previa', shown);
  }
  const others = await approver(() => rpc('approver_read_open_flow_changes_v1', INSTANCE, S, welcome.id));
  const otherIds = others.proposals.map((row) => row.proposal_id);
  if (!sameSet(otherIds, [rival.id]) || JSON.stringify(others).includes(BRIDGE.purpose)
      || 'purpose' in others.proposals[0]) {
    fail('las otras abiertas del mismo flujo, sin su para que', others);
  }
  const listing = await approver(() => rpc('approver_read_open_flow_changes_v1', INSTANCE, S, null));
  if (!sameSet(listing.proposals.map((row) => row.proposal_id), [welcome.id, rival.id, unrelated.id])) {
    fail('la lista de la pagina', listing);
  }
  const store = await approver(() => rpc('approver_read_flow_store_v1', INSTANCE, S));
  if (!Array.isArray(store.flows)) fail('leer el almacen', store);

  // Aprobar: el ID es el de la sesion de Brayan, aunque Dan haya entrado antes.
  const brayan = await signIn(BRAYAN);
  const approved = await approver(() => rpc('approver_approve_flow_change_v1', INSTANCE, brayan.session, welcome.id,
    welcome.fingerprint));
  if (!approved.ok || approved.approved_by_kind !== 'slack' || approved.approved_by_ref !== BRAYAN
      || approved.simulation !== 'built') {
    fail('aprobar con el ID de la sesion', approved);
  }
  const replay = await approver(() => rpc('approver_approve_flow_change_v1', INSTANCE, S, welcome.id,
    welcome.fingerprint));
  if (!replay.ok || replay.replayed !== true || replay.approved_by_ref !== BRAYAN) fail('aprobar otra vez', replay);
  const published = await approver(async () => rpc('approver_publish_flow_change_v1', INSTANCE_ZONE, S, welcome.id,
    welcome.fingerprint, { fingerprint: welcome.fingerprint, validator: BRIDGE.validator, result: CLEAN,
      catalog_sha256: SHA, validated_at: new Date().toISOString() }));
  const [entry] = published.published ?? [];
  if (!published.ok || entry?.state !== 'cohorte') fail('publicar el flujo nuevo', published);
  welcomeFlow = entry.flow_id;
  const definition = one(await sql(`select approved_by_kind, approved_by_ref from public.flow_definitions
    where flow_id = $1 and version = 1`, [welcomeFlow]), 'v1');
  if (definition.approved_by_kind !== 'slack' || definition.approved_by_ref !== BRAYAN) fail('la v1 con el aprobador', definition);
  // La ruta nativa la firma Brayan, y su evento dice slack (antes de la migracion: 23514 al publicar).
  const routeEvent = one(await sql(`select actor_kind, actor_ref from public.flow_change_events
    where flow_id = $1 and event_type = 'flow_route_created'`, [welcomeFlow]), 'evento de la ruta');
  if (routeEvent.actor_kind !== 'slack' || routeEvent.actor_ref !== BRAYAN) fail('el evento de la ruta nativa', routeEvent);
  // La rival quedo vieja: la base ya no la deja aprobar.
  const stale = await approver(() => rpc('approver_approve_flow_change_v1', INSTANCE, S, rival.id, rival.fingerprint));
  if (stale.ok) fail('la rival se aprueba sobre una base vieja', stale);

  // La cohorte desde la pagina.
  const tester = person('pagina-1');
  tester.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
    values ($1, $2, $3, 'MX') returning id`, [tester.name, tester.email, tester.phone]), 'contacto').id;
  const added = await approver(() => rpc('approver_flow_cohort_v1', INSTANCE, S, A.funnel, 'bienvenida', tester.phone,
    'el telefono de prueba de la pagina'));
  const member = one(await sql('select added_by_kind, added_by_ref from public.flow_cohort_members where flow_id = $1',
    [welcomeFlow]), 'miembro');
  const event = one(await sql(`select actor_kind, actor_ref, data from public.flow_change_events
    where flow_id = $1 and event_type = 'flow_cohort_changed'`, [welcomeFlow]), 'evento');
  const text = JSON.stringify([added, event]);
  if (!added.ok || !added.changed || added.members !== 1 || member.added_by_kind !== 'slack' || member.added_by_ref !== DAN
      || event.actor_kind !== 'slack' || event.actor_ref !== DAN || text.includes(tester.phone)
      || text.includes(tester.contact)) {
    fail('sumar un telefono desde la pagina', { added, member, event });
  }

  // Pausar con la generacion que mostro la pagina; con otra, no pausa.
  const generation = (await control(welcomeFlow)).generation;
  const wrongGeneration = await approver(() => rpc('approver_pause_flow_v1', INSTANCE, S, A.funnel, 'bienvenida',
    generation - 1, 'pausa desde la pagina'));
  const paused = await approver(() => rpc('approver_pause_flow_v1', INSTANCE, S, A.funnel, 'bienvenida', generation,
    'pausa desde la pagina'));
  const pauseRow = one(await sql(`select changed_by_kind, changed_by_ref, state from public.flow_controls
    where flow_id = $1`, [welcomeFlow]), 'control');
  if (wrongGeneration.ok || wrongGeneration.reason_code !== 'flow_control_generation_mismatch' || !paused.ok
      || !paused.changed || pauseRow.state !== 'pausado' || pauseRow.changed_by_kind !== 'slack'
      || pauseRow.changed_by_ref !== DAN) {
    fail('pausar desde la pagina', { wrongGeneration, paused, pauseRow });
  }

  // Descartar.
  const discarded = await approver(() => rpc('approver_discard_flow_change_v1', INSTANCE, S, unrelated.id,
    'no hace falta'));
  const discardEvent = one(await sql(`select actor_kind, actor_ref from public.flow_change_events
    where proposal_id = $1 and event_type = 'flow_change_discarded'`, [unrelated.id]), 'descarte');
  if (!discarded.ok || discardEvent.actor_kind !== 'slack' || discardEvent.actor_ref !== DAN) {
    fail('descartar desde la pagina', { discarded, discardEvent });
  }

  // Sin sesion, cada envoltorio da 28000 y no escribe.
  const calls = {
    get: () => rpc('approver_get_flow_change_proposal_v1', INSTANCE, NOT_A_SESSION, welcome.id),
    store: () => rpc('approver_read_flow_store_v1', INSTANCE, NOT_A_SESSION),
    open: () => rpc('approver_read_open_flow_changes_v1', INSTANCE, NOT_A_SESSION, null),
    approve: () => rpc('approver_approve_flow_change_v1', INSTANCE, NOT_A_SESSION, rival.id, rival.fingerprint),
    publish: () => rpc('approver_publish_flow_change_v1', INSTANCE_ZONE, NOT_A_SESSION, rival.id, rival.fingerprint, null),
    pause: () => rpc('approver_pause_flow_v1', INSTANCE, NOT_A_SESSION, A.funnel, 'bienvenida', 0, 'x'),
    discard: () => rpc('approver_discard_flow_change_v1', INSTANCE, NOT_A_SESSION, rival.id, 'x'),
    cohort: () => rpc('approver_flow_cohort_v1', INSTANCE, NOT_A_SESSION, A.funnel, 'bienvenida', tester.phone, 'x'),
  };
  for (const [label, run] of Object.entries(calls)) {
    const error = await refused(run);
    if (error?.code !== '28000' || error.message !== 'flow_approval_session_invalid') {
      fail(`sin sesion: ${label}`, error ? { code: error.code, message: error.message } : 'paso');
    }
  }
  results.ciclo = { aprobada_por: approved.approved_by_kind, publicada: entry.state, cohorte: added.members,
    pausa: pauseRow.state, otras_abiertas: otherIds.length, sin_sesion: Object.keys(calls).length };
}

// ---------------------------------------------------------------------------
// 3. Lo que el rol no puede.
// ---------------------------------------------------------------------------
{
  const denied = {
    operator_approve: `select public.operator_approve_flow_change_v1('${JSON.stringify(INSTANCE)}'::jsonb,
      gen_random_uuid(), '${SHA}', 'dan')`,
    operator_cohort: `select public.operator_flow_cohort_v1('${JSON.stringify(INSTANCE)}'::jsonb, 'att1', 'bienvenida',
      'agregar', '12025550100', 'dan', 'x')`,
    agent_get: `select public.get_flow_change_proposal_v1('${JSON.stringify(INSTANCE)}'::jsonb, gen_random_uuid())`,
    internal_actor: `select public._flow_approval_session_actor('a', 'b', '${SHA}')`,
    internal_cohort: `select public._flow_cohort_change('a','b','c','d','agregar','1','slack','${DAN}','x')`,
    select_sessions: 'select * from public.flow_approval_sessions',
    select_states: 'select * from public.flow_approval_oidc_states',
    insert_session: `insert into public.flow_approval_sessions (session_hash, tenant_ref, ally_ref, slack_team_id,
      slack_user_id, expires_at) values ('${SHA}', 'a', 'b', '${TEAM}', '${DAN}', now() + interval '1 hour')`,
    select_proposals: 'select * from public.flow_change_proposals',
  };
  for (const [label, text] of Object.entries(denied)) {
    const error = await refused(() => db.query(text));
    if (error?.code !== '42501') fail(`el rol no puede: ${label}`, error ? { code: error.code, message: error.message } : 'paso');
  }
  results.el_rol_no_puede = Object.keys(denied);
}

// ---------------------------------------------------------------------------
// 4. La cohorte del operador sigue igual.
// ---------------------------------------------------------------------------
{
  const lead = person('operador-1');
  await sql(`insert into public.contacts (full_name, email, phone, country_iso) values ($1, $2, $3, 'MX')`,
    [lead.name, lead.email, lead.phone]);
  const added = await rpc('operator_flow_cohort_v1', INSTANCE, A.funnel, 'bienvenida', 'agregar', lead.phone, 'dan',
    'el telefono de prueba');
  const kinds = (await sql(`select added_by_kind, added_by_ref from public.flow_cohort_members where flow_id = $1
    order by added_at`, [welcomeFlow])).map((row) => `${row.added_by_kind}:${row.added_by_ref}`);
  const removed = await rpc('operator_flow_cohort_v1', INSTANCE, A.funnel, 'bienvenida', 'sacar', lead.phone, 'dan',
    'el telefono de prueba');
  if (!added.ok || added.members !== 2 || JSON.stringify(kinds) !== JSON.stringify([`slack:${DAN}`, 'operador:dan'])
      || !removed.ok || removed.members !== 1 || JSON.stringify(Object.keys(added).sort())
         !== JSON.stringify(['action', 'changed', 'max_members', 'members', 'ok'])) {
    fail('la cohorte del operador', { added, kinds, removed });
  }
  const operatorEvent = (await sql(`select actor_kind from public.flow_change_events where flow_id = $1
    and event_type = 'flow_cohort_changed' order by created_at`, [welcomeFlow])).map((row) => row.actor_kind);
  if (JSON.stringify(operatorEvent) !== JSON.stringify(['slack', 'operador', 'operador'])) {
    fail('los eventos de la cohorte', operatorEvent);
  }
  // La pagina no saca: la funcion de la pagina siempre agrega, y la interna rechaza sacar con slack.
  const slackRemove = await expectError(db, 'sacar con slack', () => db.query(`select public._flow_cohort_change($1, $2,
    $3, 'bienvenida', 'sacar', $4, 'slack', $5, 'x')`, [A.tenant, A.ally, A.funnel, lead.phone, DAN]),
  { code: '22023', message: 'flow_store_actor_invalid' });
  results.operador = { cohorte: kinds, sacar_con_slack: slackRemove.message };
}

console.log(JSON.stringify({ validate_flow_change_slack_approver: 'ok', ...results }, null, 2));
