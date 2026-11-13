// Las solicitudes de plantilla de Meta (migracion 20261102000200, F-146 partes 1 y 2), con las RPC reales
// sobre la cadena completa de migraciones.
//
// Contrato: docs/contracts/template-request-v1.md.
//
// Prueba, con un rojo por pieza:
//   0. la forma: agrega exactamente sus 19 funciones y sus dos tablas, no cambia ninguna otra funcion ni
//      permiso; cada funcion publica la ejecuta solo su rol (el del agente, el de la pagina o service_role) y
//      ninguna anon ni authenticated; las tablas con RLS y sin permisos para la API; correrla dos veces aborta.
//      Antes de la migracion nada existe (el rojo de todo lo demas);
//   1. el agente: guardar (con la salida real del bridge, bridge_att1_bienvenida_02.json), rehacer con la
//      revision, la revision vieja, el nombre ocupado, el tope de 10 abiertas, el enlace a una propuesta suya
//      (y a una que no existe), validar con bloqueos (queda borrador), sin el pedido de Meta (22023), y sin
//      bloqueos (validada, con la huella de 24 h); ni aprobar ni mandar;
//   2. la pagina, con la sesion: releer, aprobar con la huella (otra huella: 22023; repetir: replayed),
//      empezar el envio (el tope por dia; en curso; reintento a los 2 minutos), registrar el error y lo que
//      contesto Meta (PENDING: en_revision); descartar solo lo que no llego a Meta; sin sesion, 28000;
//   3. service_role, el sondeo: PENDING sigue sin evento; APPROVED como MARKETING de una UTILITY:
//      aprobada_meta con recategorized; en el catalogo: en_catalogo y final; REJECTED con su motivo; a las
//      48 horas sin respuesta, sin_respuesta_de_meta;
//   4. el vencimiento a las 24 h (y el nombre vuelve a quedar libre), los disparadores (recorrido,
//      congelado, permanente, solo agregar) y que la lista y la relectura del agente no traen ningun ID de
//      Slack.
//
// Datos sinteticos, los de buildWorld (el manifiesto de ATT1). Los ID de Slack son de forma valida e
// inventados. --emit-fixture deja las respuestas, con uuid y horas normalizados, en
// tests/fixtures/meta_template_requests/ para las pruebas del bridge (tests/test_template_request_api.py);
// --check verifica que sigan iguales.
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261102000200_meta_template_requests_v1.sql';
const EMIT = process.argv.includes('--emit-fixture');
const CHECK = process.argv.includes('--check');
const results = {};
const fixture = (path) => JSON.parse(readFileSync(join(root, 'tests/fixtures', path), 'utf8'));
const BRIDGE = fixture('meta_template_requests/bridge_att1_bienvenida_02.json');
const EXTRA = fixture('meta_template_requests/bridge_extra_cases.json');
const FLOW_BRIDGE = fixture('flow_change_new_flow/ae_bienvenida_20261024.json');
const sha = (text) => createHash('sha256').update(text).digest('hex');
// jsonb ordena las claves a su manera: se compara la forma canonica.
const canon = (value) => JSON.stringify(value, (_, v) => (v && typeof v === 'object' && !Array.isArray(v)
  ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, v[k]])) : v));

const AE_FUNCTIONS = [
  'save_meta_template_request_draft_v1(jsonb,uuid,integer,jsonb,text,uuid,text,text)',
  'record_meta_template_request_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,jsonb,text)',
  'get_meta_template_request_v1(jsonb,uuid)',
  'list_meta_template_requests_v1(jsonb,boolean,integer)',
];
const APPROVER_FUNCTIONS = [
  'approver_get_meta_template_request_v1(jsonb,text,uuid)',
  'approver_approve_meta_template_request_v1(jsonb,text,uuid,text)',
  'approver_discard_meta_template_request_v1(jsonb,text,uuid,text)',
  'approver_begin_meta_template_send_v1(jsonb,text,uuid,text,integer)',
  'approver_record_meta_template_sent_v1(jsonb,text,uuid,text,text,text,text)',
];
const SERVICE_FUNCTIONS = ['record_meta_template_status_v1(jsonb,uuid,text,text,text,text,boolean)'];
const INTERNAL_FUNCTIONS = [
  '_meta_template_draft_is_valid(jsonb)',
  '_meta_template_meta_request_is_valid(jsonb,text,text,text)',
  'reject_meta_template_request_mutation()',
  'validate_meta_template_request_change()',
  '_meta_template_request_event(meta_template_requests,text,text,text,text,jsonb)',
  '_meta_template_request_fingerprint(uuid,integer,jsonb,jsonb,text,uuid,text)',
  '_meta_template_request_expire_if_due(meta_template_requests)',
  '_meta_template_request_view(meta_template_requests,boolean)',
  '_meta_template_requests_open(text,text)',
];
const ALL_FUNCTIONS = [...AE_FUNCTIONS, ...APPROVER_FUNCTIONS, ...SERVICE_FUNCTIONS, ...INTERNAL_FUNCTIONS];

// ---------------------------------------------------------------------------
// 0. La forma de la migracion, en una base propia: antes y despues.
// ---------------------------------------------------------------------------
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
// Despues va solo el barrido del seguimiento nativo (20261102000300, E-269), que no toca lo de esta.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261102000300_flow_engine_native_silence_sweep_v1.sql',
  // El reciclado de un telefono de prueba (20261102000400, E-270).
  '20261102000400_identity_recycle_v1.sql',
  // Los rechazos de Meta (20261103000500, E-284).
  '20261103000500_meta_delivery_rejections_v1.sql',
  // El historial de la pagina de aprobacion (20261103000600, E-295): dos lecturas del rol de la pagina.
  '20261103000600_flow_approval_history_v1.sql',
  // Si la persona ya es cliente (20261103000700, E-297): una lectura de service_role.
  '20261103000700_customer_status_v1.sql',
  // Las cuatro ramas del pago rechazado nativo (20261103000900, E-306).
  '20261103000900_flow_engine_native_payment_failure_branches_v1.sql',
  // El aviso de derivaciones sin atender (20261103001000, E-309): una lectura de service_role.
  '20261103001000_handoff_wait_alerts_v1.sql',
  // La etiqueta de un segmento en un flujo nativo (20261103001300, E-316): un reemplazo contado en la forma nativa.
  '20261103001300_flow_engine_native_segment_label_v1.sql',
  // La entrada a una campana: lo que contesto cada conversacion a la pregunta (20261103001500, E-317).
  '20261103001500_segment_entry_answers_v1.sql',
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
  // El sondeo de Meta y el aviso en el hilo (20261109000100, E-361): dos columnas, una tabla, cuatro RPC.
  '20261109000100_meta_template_request_notices_v1.sql',
  // La oferta de la campana en el link del agente (20261110000500, E-410).
  '20261110000500_campaign_offer_checkout_issuance_v1.sql',
  '20261111000100_close_recovery_case_without_pending_touch_v1.sql',
  '20261111000200_pending_cancel_keeps_first_contact_v1.sql',
  // Los numeros del tablero (20261112200000, E-507): una lectura nueva.
  '20261112200000_read_results_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la del barrido del seguimiento nativo`, names.slice(position + 1));
}
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
await applyFile(shapeDb, join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of names.slice(0, position)) await applyFile(shapeDb, join(root, 'supabase/migrations', name));
const before = await snapshot(shapeDb);
const tablesBefore = await tables(shapeDb);
if (ALL_FUNCTIONS.some((signature) => before.has(signature))) fail('alguna funcion ya existe antes de la migracion');
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
  if (!sameSet(newTables, ['meta_template_requests', 'meta_template_request_events'])) fail('las tablas', newTables);
  const can = async (role, signature) => one((await shapeDb.query(
    `select has_function_privilege($1, to_regprocedure('public.${signature}'), 'execute') as x`, [role])).rows,
  role).x;
  const roles = ['automation_expert_change', 'flow_change_approver', 'service_role', 'anon', 'authenticated'];
  const grants = {};
  for (const signature of ALL_FUNCTIONS) {
    const allowed = [];
    for (const role of roles) if (await can(role, signature)) allowed.push(role);
    grants[signature] = allowed;
    const expected = AE_FUNCTIONS.includes(signature) ? ['automation_expert_change']
      : APPROVER_FUNCTIONS.includes(signature) ? ['flow_change_approver']
        : SERVICE_FUNCTIONS.includes(signature) ? ['service_role'] : [];
    if (!sameSet(allowed, expected)) fail(`los permisos de ${signature}`, allowed);
  }
  const definers = (await shapeDb.query(`select p.oid::regprocedure::text sig, prosecdef, provolatile,
      coalesce(array_to_string(proconfig, ','), '') config
    from pg_proc p where p.pronamespace = 'public'::regnamespace and p.proname like '%meta_template%'`)).rows;
  for (const row of definers) {
    const signature = row.sig.replace(/^public\./, '').replace(/ /g, '');
    const publicOne = !INTERNAL_FUNCTIONS.includes(signature);
    if (row.prosecdef !== publicOne) fail(`security definer solo las publicas: ${signature}`, row);
    if (!row.config.includes('search_path=pg_catalog')) fail(`search_path fijo: ${signature}`, row);
  }
  const stable = definers.filter((row) => row.provolatile === 's').map((row) => row.sig.replace(/^public\./, '')
    .replace(/ /g, ''));
  for (const signature of ['get_meta_template_request_v1(jsonb,uuid)',
    'list_meta_template_requests_v1(jsonb,boolean,integer)']) {
    if (!stable.includes(signature)) fail(`las lecturas son stable: ${signature}`, stable);
  }
  const tableAcl = (await shapeDb.query(`select c.relname, c.relrowsecurity,
      has_table_privilege('anon', c.oid, 'select') a, has_table_privilege('authenticated', c.oid, 'select') u,
      has_table_privilege('service_role', c.oid, 'select') s,
      has_table_privilege('automation_expert_change', c.oid, 'select') x
    from pg_class c where c.oid in ('public.meta_template_requests'::regclass,
      'public.meta_template_request_events'::regclass)`)).rows;
  for (const row of tableAcl) {
    if (!row.relrowsecurity || row.a || row.u || row.s || row.x) fail('las tablas, sin permisos y con RLS', row);
  }
  const error = await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'meta_template_requests_already_applied' });
  results.forma = { funciones: added.length, tablas: newTables, segunda_corrida: error.message };
}
await shapeDb.close();

// ---------------------------------------------------------------------------
// El mundo: ATT1, con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, rpc } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const OTHER = { ...INSTANCE, ally_ref: 'otra-aliada' };
const AE = 'U0123456789';
const DAN = 'U0DANSCHWAB1';
const TEAM = 'T0LANCEMOS01';
const later = async () => db.exec('select pg_sleep(0.01)');
// Cada llamada, un poco despues de la anterior: PGlite guarda clock_timestamp con milisegundos, y dos
// eventos o dos filas que empatan cambian el orden de la lista (y el fixture).
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
// Mover las fechas como duenio y sin disparadores (el metodo de validate_flow_change_list.mjs).
const shift = async (text, params) => {
  await db.exec('set session_replication_role = replica');
  await sql(text, params);
  await db.exec('set session_replication_role = origin');
};
const row = async (id) => one(await sql('select * from public.meta_template_requests where id = $1', [id]), 'fila');
const events = async (id) => (await sql(`select event_type, actor_kind, reason_code, data
  from public.meta_template_request_events where request_id = $1 order by created_at, id`, [id]));

const draftNamed = (name, category = BRIDGE.draft.categoria) => ({ ...structuredClone(BRIDGE.draft), nombre: name,
  categoria: category });
const metaFor = (name, category = BRIDGE.meta_request.category) => ({ ...structuredClone(BRIDGE.meta_request), name,
  category });
const save = (draft, extra = {}) => agent(() => rpc('save_meta_template_request_draft_v1', INSTANCE,
  extra.id ?? null, extra.revision ?? null, draft, extra.purpose ?? BRIDGE.purpose, extra.proposal ?? null,
  extra.step ?? null, AE));
const record = (id, revision, { result = BRIDGE.result, meta = BRIDGE.meta_request } = {}) => agent(() => rpc(
  'record_meta_template_request_validation_v1', INSTANCE, id, revision, BRIDGE.validator, result, meta,
  BRIDGE.preview, AE));
const validated = async (name, category) => {
  const saved = await save(draftNamed(name, category));
  if (!saved.ok) fail(`guardar ${name}`, saved);
  const recorded = await record(saved.request_id, saved.revision, { meta: metaFor(name, category) });
  if (recorded.status !== 'validada') fail(`validar ${name}`, recorded);
  return { id: saved.request_id, fingerprint: recorded.fingerprint };
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

// Una propuesta de flujo del agente, para el enlace.
const flowItem = structuredClone(FLOW_BRIDGE.items[0]);
const flowSaved = await agent(() => rpc('save_flow_change_draft_v1', INSTANCE, null, null, [flowItem], {},
  'la bienvenida que espera su plantilla', AE));
if (!flowSaved.ok) fail('la propuesta de flujo', flowSaved);

// ---------------------------------------------------------------------------
// 1. El agente.
// ---------------------------------------------------------------------------
const outputs = {};
{
  const saved = await save(BRIDGE.draft, { proposal: flowSaved.proposal_id, step: '/items/0/definicion/pasos/0' });
  if (!saved.ok || saved.status !== 'borrador' || saved.revision !== 1) fail('guardar', saved);
  outputs.saved = saved;
  const stored = await row(saved.request_id);
  if (stored.template_name !== 'att1_bienvenida_02' || stored.language !== 'es_MX'
      || stored.category_requested !== 'UTILITY' || stored.proposal_id !== flowSaved.proposal_id) {
    fail('la fila guardada', stored);
  }
  const redo = await save({ ...BRIDGE.draft, encabezado: 'Aviso' }, { id: saved.request_id, revision: 1,
    proposal: flowSaved.proposal_id, step: '/items/0/definicion/pasos/0' });
  if (!redo.ok || redo.revision !== 2) fail('rehacer', redo);
  const stale = await save(BRIDGE.draft, { id: saved.request_id, revision: 1, proposal: flowSaved.proposal_id,
    step: '/items/0/definicion/pasos/0' });
  if (stale.ok || stale.reason_code !== 'meta_template_revision_mismatch') fail('la revision vieja', stale);
  const taken = await save(BRIDGE.draft);
  if (taken.ok || taken.reason_code !== 'meta_template_name_taken') fail('el nombre ocupado', taken);
  const noProposal = await expectError(db, 'una propuesta que no existe', () => save(draftNamed('att1_otra_01'), {
    proposal: '00000000-0000-4000-8000-000000000999', step: '/items/0' }), { code: 'P0002',
    message: 'flow_change_not_found' });
  const halfLink = await expectError(db, 'el enlace a medias', () => save(draftNamed('att1_otra_01'), {
    proposal: flowSaved.proposal_id }), { code: '22023', message: 'meta_template_flow_link_invalid' });
  const badDraft = await expectError(db, 'un borrador sin forma', () => save({ ...BRIDGE.draft, extra: 1 }),
    { code: '22023', message: 'meta_template_draft_invalid' });
  const other = await expectError(db, 'otra aliada', () => agent(() => rpc('save_meta_template_request_draft_v1',
    OTHER, null, null, BRIDGE.draft, 'x', null, null, AE)), { code: '22023', message: 'flow_store_instance_not_bound' });

  // Con bloqueos queda borrador y sin huella.
  const blocked = await record(saved.request_id, 2, {
    result: { blocks: [{ code: 'template_name_pattern', path: '/nombre', reason: 'x' }], warnings: [] },
    meta: null });
  if (!blocked.ok || blocked.status !== 'borrador' || blocked.blocks !== 1) fail('con bloqueos', blocked);
  if ((await row(saved.request_id)).fingerprint !== null) fail('con bloqueos no hay huella');
  const noMeta = await expectError(db, 'sin el pedido de Meta', () => record(saved.request_id, 2, { meta: null }),
    { code: '22023', message: 'meta_template_meta_request_invalid' });
  const wrongMeta = await expectError(db, 'el pedido de otro nombre', () => record(saved.request_id, 2, {
    meta: metaFor('att1_otro_99') }), { code: '22023', message: 'meta_template_meta_request_invalid' });
  // La revision 2 lleva el encabezado 'Aviso': se rehace con el de la salida del bridge y se valida.
  const third = await save(BRIDGE.draft, { id: saved.request_id, revision: 2, proposal: flowSaved.proposal_id,
    step: '/items/0/definicion/pasos/0' });
  const recorded = await record(saved.request_id, third.revision);
  if (!recorded.ok || recorded.status !== 'validada' || !/^[0-9a-f]{64}$/.test(recorded.fingerprint)) {
    fail('validar', recorded);
  }
  outputs.recorded = recorded;
  const frozen = await row(saved.request_id);
  const hours = (new Date(frozen.expires_at) - new Date(frozen.validated_at)) / 3_600_000;
  if (frozen.status !== 'validada' || hours !== 24
      || canon(frozen.validation.meta_request) !== canon(BRIDGE.meta_request)) {
    fail('la validada congela el pedido 24 h', frozen);
  }
  const again = await record(saved.request_id, third.revision);
  if (again.ok || again.reason_code !== 'meta_template_not_draft') fail('validar dos veces', again);
  const notDraft = await save(BRIDGE.draft, { id: saved.request_id, revision: third.revision,
    proposal: flowSaved.proposal_id, step: '/items/0/definicion/pasos/0' });
  if (notDraft.ok || notDraft.reason_code !== 'meta_template_not_draft') fail('rehacer una validada', notDraft);
  const agentApproves = await expectError(db, 'el agente no aprueba', () => agent(() => rpc(
    'approver_approve_meta_template_request_v1', INSTANCE, S, saved.request_id, recorded.fingerprint)),
  { code: '42501' });
  const agentStatus = await expectError(db, 'el agente no registra estados', () => agent(() => rpc(
    'record_meta_template_status_v1', INSTANCE, saved.request_id, '1', 'APPROVED', 'UTILITY', null, false)),
  { code: '42501' });
  const anon = await expectError(db, 'anon', () => as('anon', () => rpc('list_meta_template_requests_v1', INSTANCE,
    true, 10)), { code: '42501' });
  outputs.got = await agent(() => rpc('get_meta_template_request_v1', INSTANCE, saved.request_id));
  results.agente = { revision_vieja: stale.reason_code, nombre_ocupado: taken.reason_code,
    sin_propuesta: noProposal.message, enlace_a_medias: halfLink.message, sin_forma: badDraft.message,
    otra_aliada: other.message, sin_pedido: noMeta.message, pedido_ajeno: wrongMeta.message,
    no_aprueba: agentApproves.code, no_registra: agentStatus.code, anon: anon.code };
  results.request_id = saved.request_id;
}

// El tope de 10 abiertas del agente.
{
  const open = one(await sql(`select count(*)::integer n from public.meta_template_requests
    where status = 'borrador' or (status in ('validada', 'aprobada') and clock_timestamp() < expires_at)`), 'n').n;
  const extra = [];
  for (let index = open; index < 10; index += 1) {
    const saved = await save(draftNamed(`att1_tope_${String(index).padStart(2, '0')}`));
    if (!saved.ok) fail('llenar el tope', saved);
    extra.push(saved.request_id);
  }
  const over = await save(draftNamed('att1_tope_99'));
  if (over.ok || over.reason_code !== 'meta_template_open_limit' || over.open !== 10) fail('el tope', over);
  for (const id of extra) {
    const discarded = await page('approver_discard_meta_template_request_v1', id, 'del tope');
    if (!discarded.ok) fail('descartar del tope', discarded);
  }
  const freed = await save(draftNamed('att1_tope_00'));
  if (!freed.ok) fail('el nombre de una descartada queda libre', freed);
  await page('approver_discard_meta_template_request_v1', freed.request_id, 'listo');
  results.tope = { abiertas: 10, codigo: over.reason_code };
}

// ---------------------------------------------------------------------------
// 2. La pagina, con la sesion.
// ---------------------------------------------------------------------------
{
  const id = results.request_id;
  const { fingerprint } = outputs.recorded;
  const noSession = await expectError(db, 'sin sesion', () => approver(() => rpc(
    'approver_approve_meta_template_request_v1', INSTANCE, sha('nadie'), id, fingerprint)),
  { code: '28000', message: 'flow_approval_session_invalid' });
  const wrong = await expectError(db, 'otra huella', () => page('approver_approve_meta_template_request_v1', id,
    sha('otra')), { code: '22023', message: 'meta_template_fingerprint_mismatch' });
  const sendEarly = await page('approver_begin_meta_template_send_v1', id, fingerprint, 5);
  if (sendEarly.ok || sendEarly.reason_code !== 'meta_template_not_approved') fail('mandar sin aprobar', sendEarly);
  const approved = await page('approver_approve_meta_template_request_v1', id, fingerprint);
  if (!approved.ok || approved.replayed || approved.approved_by_ref !== DAN || approved.status !== 'aprobada') {
    fail('aprobar', approved);
  }
  const replay = await page('approver_approve_meta_template_request_v1', id, fingerprint);
  if (!replay.ok || !replay.replayed) fail('aprobar dos veces', replay);
  outputs.approverGot = await page('approver_get_meta_template_request_v1', id);
  if (outputs.approverGot.approved_by_ref !== DAN) fail('la pagina ve quien aprobo', outputs.approverGot);
  const badCap = await expectError(db, 'el tope fuera de rango', () => page('approver_begin_meta_template_send_v1',
    id, fingerprint, 0), { code: '22023', message: 'meta_template_daily_cap_invalid' });
  const begun = await page('approver_begin_meta_template_send_v1', id, fingerprint, 5);
  if (!begun.ok || begun.attempt !== 1 || canon(begun.meta_request) !== canon(BRIDGE.meta_request)) {
    fail('empezar el envio devuelve el pedido congelado', begun);
  }
  outputs.begun = begun;
  const inProgress = await page('approver_begin_meta_template_send_v1', id, fingerprint, 5);
  if (inProgress.ok || inProgress.reason_code !== 'meta_template_send_in_progress') fail('en curso', inProgress);
  const discardSent = await page('approver_discard_meta_template_request_v1', id, 'ya no');
  if (discardSent.ok || discardSent.reason_code !== 'meta_template_request_final') fail('descartar enviada', discardSent);
  const failed = await page('approver_record_meta_template_sent_v1', id, null, null, null, 'meta_graph_unavailable');
  if (!failed.ok || !failed.send_failed || (await row(id)).last_send_error !== 'meta_graph_unavailable') {
    fail('el error del envio', failed);
  }
  await shift(`update public.meta_template_requests set send_started_at = send_started_at - interval '3 minutes'
    where id = $1`, [id]);
  const retried = await page('approver_begin_meta_template_send_v1', id, fingerprint, 5);
  if (!retried.ok || retried.attempt !== 2) fail('el reintento a los 2 minutos', retried);
  const badResult = await expectError(db, 'un estado de Meta que no es del alta', () => page(
    'approver_record_meta_template_sent_v1', id, '1234567890', 'PAUSED', 'UTILITY', null),
  { code: '22023', message: 'meta_template_send_result_invalid' });
  const sent = await page('approver_record_meta_template_sent_v1', id, '1234567890123456', 'PENDING', 'UTILITY', null);
  if (!sent.ok || sent.status !== 'en_revision' || sent.recategorized) fail('lo que contesto Meta', sent);
  outputs.sent = sent;
  const sentAgain = await page('approver_record_meta_template_sent_v1', id, '1234567890123456', 'PENDING', 'UTILITY',
    null);
  if (!sentAgain.ok || !sentAgain.replayed) fail('registrar dos veces', sentAgain);
  results.pagina = { sin_sesion: noSession.message, otra_huella: wrong.message, tope_invalido: badCap.message,
    en_curso: inProgress.reason_code, descartar_enviada: discardSent.reason_code, intentos: retried.attempt,
    estado: sent.status, estado_invalido: badResult.message };
}

// El tope por dia: con 1, la segunda del dia no sale.
{
  const second = await validated('att1_segunda_01', 'UTILITY');
  await page('approver_approve_meta_template_request_v1', second.id, second.fingerprint);
  const capped = await page('approver_begin_meta_template_send_v1', second.id, second.fingerprint, 1);
  if (capped.ok || capped.reason_code !== 'meta_template_daily_cap' || capped.sent_last_24h !== 1) {
    fail('el tope por dia', capped);
  }
  const lastEvent = (await events(second.id)).at(-1);
  if (lastEvent.event_type !== 'meta_template_send_refused' || lastEvent.reason_code !== 'meta_template_daily_cap') {
    fail('el tope deja su evento', lastEvent);
  }
  const begun = await page('approver_begin_meta_template_send_v1', second.id, second.fingerprint, 5);
  if (!begun.ok) fail('con 5, sale', begun);
  results.tope_por_dia = capped.reason_code;
  results.second = second;
}

// ---------------------------------------------------------------------------
// 3. service_role: el sondeo.
// ---------------------------------------------------------------------------
{
  const id = results.request_id;
  const pending = await service(() => rpc('record_meta_template_status_v1', INSTANCE, id, '1234567890123456',
    'PENDING', 'UTILITY', null, false));
  if (!pending.ok || pending.status !== 'en_revision') fail('PENDING sigue', pending);
  const eventsBefore = (await events(id)).length;
  await service(() => rpc('record_meta_template_status_v1', INSTANCE, id, '1234567890123456', 'PENDING', 'UTILITY',
    null, false));
  if ((await events(id)).length !== eventsBefore) fail('el mismo estado no deja evento');
  const wrongId = await expectError(db, 'otro id de Meta', () => service(() => rpc('record_meta_template_status_v1',
    INSTANCE, id, '999', 'APPROVED', 'UTILITY', null, false)), { code: '22023', message: 'meta_template_id_mismatch' });
  const inconsistent = await expectError(db, 'en el catalogo sin aprobar', () => service(() => rpc(
    'record_meta_template_status_v1', INSTANCE, id, '1234567890123456', 'PENDING', 'UTILITY', null, true)),
  { code: '22023', message: 'meta_template_status_invalid' });
  // Meta la aprueba como marketing: la pedida era utilidad.
  const recat = await service(() => rpc('record_meta_template_status_v1', INSTANCE, id, '1234567890123456',
    'APPROVED', 'MARKETING', null, false));
  if (!recat.ok || recat.status !== 'aprobada_meta' || recat.recategorized !== true) fail('recategorizada', recat);
  outputs.listedApproved = await agent(() => rpc('list_meta_template_requests_v1', INSTANCE, true, 50));
  const inCatalog = await service(() => rpc('record_meta_template_status_v1', INSTANCE, id, '1234567890123456',
    'APPROVED', 'MARKETING', null, true));
  if (!inCatalog.ok || inCatalog.status !== 'en_catalogo') fail('en el catalogo', inCatalog);
  const final = await service(() => rpc('record_meta_template_status_v1', INSTANCE, id, '1234567890123456',
    'APPROVED', 'MARKETING', null, true));
  if (final.ok || final.reason_code !== 'meta_template_request_final') fail('en_catalogo es final', final);
  const types = (await events(id)).map((event) => event.event_type);
  for (const type of ['meta_template_draft_saved', 'meta_template_validation_blocked', 'meta_template_validated',
    'meta_template_approved', 'meta_template_send_started', 'meta_template_send_failed', 'meta_template_sent',
    'meta_template_approved_by_meta', 'meta_template_in_catalog']) {
    if (!types.includes(type)) fail(`falta el evento ${type}`, types);
  }

  // La segunda: enviada sin id (el envio quedo a medias); el sondeo la encuentra por nombre y Meta la rechaza.
  const second = results.second;
  const rejected = await service(() => rpc('record_meta_template_status_v1', INSTANCE, second.id, '2222222222',
    'REJECTED', 'UTILITY', 'INVALID_FORMAT', false));
  if (!rejected.ok || rejected.status !== 'rechazada') fail('rechazada', rejected);
  const rejectedRow = await row(second.id);
  if (rejectedRow.meta_rejected_reason !== 'INVALID_FORMAT' || rejectedRow.closed_by_ref !== 'meta') {
    fail('el motivo del rechazo', rejectedRow);
  }
  // Una rechazada no libera el nombre: la correccion es otra con el numero siguiente (D5).
  const sameName = await save(draftNamed('att1_segunda_01'));
  if (sameName.ok || sameName.reason_code !== 'meta_template_name_taken') fail('el nombre de una rechazada', sameName);

  // La tercera: a las 48 horas sin respuesta.
  const third = await validated('att1_tercera_01', 'UTILITY');
  await page('approver_approve_meta_template_request_v1', third.id, third.fingerprint);
  await page('approver_begin_meta_template_send_v1', third.id, third.fingerprint, 5);
  await page('approver_record_meta_template_sent_v1', third.id, '3333333333', 'PENDING', 'UTILITY', null);
  await shift(`update public.meta_template_requests set sent_at = sent_at - interval '49 hours' where id = $1`,
    [third.id]);
  const silent = await service(() => rpc('record_meta_template_status_v1', INSTANCE, third.id, '3333333333',
    'PENDING', null, null, false));
  if (!silent.ok || silent.status !== 'sin_respuesta_de_meta') fail('sin respuesta', silent);
  // La respuesta tarde, ya en el catalogo: pasa por aprobada_meta y deja los dos eventos. Se deshace (los
  // dos eventos empatan en milisegundos en PGlite y el fixture de la lista cambiaria de orden).
  await db.exec('begin');
  const late = await service(() => rpc('record_meta_template_status_v1', INSTANCE, third.id, '3333333333',
    'APPROVED', 'UTILITY', null, true));
  const lateTypes = (await events(third.id)).map((event) => event.event_type).slice(-2);
  await db.exec('rollback');
  if (!late.ok || late.status !== 'en_catalogo' || late.recategorized
      || !sameSet(lateTypes, ['meta_template_approved_by_meta', 'meta_template_in_catalog'])) {
    fail('la respuesta tarde', { late, lateTypes });
  }
  results.sondeo = { recategorizada: recat.recategorized, catalogo: inCatalog.status, rechazada: rejected.status,
    sin_respuesta: silent.status, otro_id: wrongId.message, inconsistente: inconsistent.message };
}

// ---------------------------------------------------------------------------
// 4. El vencimiento, los disparadores y lo que no sale.
// ---------------------------------------------------------------------------
{
  const expiring = await validated('att1_vence_01', 'MARKETING');
  await shift(`update public.meta_template_requests set validated_at = validated_at - interval '25 hours',
    expires_at = expires_at - interval '25 hours' where id = $1`, [expiring.id]);
  const listed = await agent(() => rpc('list_meta_template_requests_v1', INSTANCE, false, 50));
  const listedRow = listed.requests.find((r) => r.request_id === expiring.id);
  if (listedRow.status !== 'validada' || listedRow.effective_status !== 'vencida') fail('la vencida, efectiva', listedRow);
  const late = await page('approver_approve_meta_template_request_v1', expiring.id, expiring.fingerprint);
  if (late.ok || late.reason_code !== 'meta_template_request_expired') fail('aprobar vencida', late);
  if ((await row(expiring.id)).status !== 'vencida') fail('queda vencida');
  const reuse = await save(draftNamed('att1_vence_01', 'MARKETING'));
  if (!reuse.ok) fail('una vencida libera el nombre', reuse);

  const transition = await expectError(db, 'el recorrido', () => sql(`update public.meta_template_requests
    set status = 'en_catalogo' where id = $1`, [reuse.request_id]), { code: '23514',
    message: 'meta_template_request_transition_invalid' });
  const frozenEdit = await expectError(db, 'congelada', () => sql(`update public.meta_template_requests
    set purpose = 'otro' where id = $1`, [results.request_id]), { code: '55000', message: 'meta_template_request_frozen' });
  const permanent = await expectError(db, 'permanente', () => sql('delete from public.meta_template_requests where id = $1',
    [reuse.request_id]), { code: '55000', message: 'meta_template_request_is_permanent' });
  const appendOnly = await expectError(db, 'solo agregar', () => sql(`update public.meta_template_request_events
    set reason_code = 'x' where request_id = $1`, [reuse.request_id]), { code: '55000',
    message: 'meta_template_request_event_is_append_only' });
  const incoherent = await expectError(db, 'el CHECK del estado', () => sql(`update public.meta_template_requests
    set status = 'validada' where id = $1`, [reuse.request_id]), { code: '23514' });
  outputs.listedAll = await agent(() => rpc('list_meta_template_requests_v1', INSTANCE, false, 50));
  outputs.listedOpen = await agent(() => rpc('list_meta_template_requests_v1', INSTANCE, true, 50));
  for (const listing of [outputs.listedAll, outputs.listedOpen, outputs.got]) {
    const text = JSON.stringify(listing);
    if (text.includes(AE) || text.includes(DAN)) fail('lo del agente trae un ID de Slack', listing);
  }
  if (outputs.listedOpen.requests.some((r) => ['en_catalogo', 'rechazada', 'descartada', 'vencida']
    .includes(r.effective_status))) {
    fail('las abiertas no traen las cerradas', outputs.listedOpen);
  }
  const bad = await expectError(db, 'p_limit 51', () => agent(() => rpc('list_meta_template_requests_v1', INSTANCE,
    true, 51)), { code: '22023', message: 'meta_template_list_parameters_invalid' });
  results.cierre = { vencida: 'vencida', recorrido: transition.message, congelada: frozenEdit.message,
    permanente: permanent.message, solo_agregar: appendOnly.message, check: incoherent.code, limite: bad.message };
}

// ---------------------------------------------------------------------------
// 5. Los bordes de la revision (E-268, tres lentes), en una transaccion que se deshace: no entran al fixture.
// ---------------------------------------------------------------------------
{
  await db.exec('begin');
  // Un envio a medias (enviada sin id) que el sondeo encuentra PAUSED: pasa a la revision con su id.
  const paused = await validated('att1_pausada_01', 'UTILITY');
  await page('approver_approve_meta_template_request_v1', paused.id, paused.fingerprint);
  await page('approver_begin_meta_template_send_v1', paused.id, paused.fingerprint, 20);
  const pausedOut = await service(() => rpc('record_meta_template_status_v1', INSTANCE, paused.id, '4444444444',
    'PAUSED', 'UTILITY', null, false));
  if (!pausedOut.ok || pausedOut.status !== 'en_revision') fail('enviada sin id y PAUSED', pausedOut);
  // Otra a medias, PENDING a las 49 h: sin respuesta.
  const late = await validated('att1_tarde_01', 'UTILITY');
  await page('approver_approve_meta_template_request_v1', late.id, late.fingerprint);
  await page('approver_begin_meta_template_send_v1', late.id, late.fingerprint, 20);
  await shift(`update public.meta_template_requests set sent_at = sent_at - interval '49 hours' where id = $1`,
    [late.id]);
  const lateOut = await service(() => rpc('record_meta_template_status_v1', INSTANCE, late.id, '5555555555',
    'PENDING', null, null, false));
  if (!lateOut.ok || lateOut.status !== 'sin_respuesta_de_meta') fail('enviada sin id a las 49 h', lateOut);
  // Un id de Meta es de una sola solicitud.
  const twin = await validated('att1_gemela_01', 'UTILITY');
  await page('approver_approve_meta_template_request_v1', twin.id, twin.fingerprint);
  await page('approver_begin_meta_template_send_v1', twin.id, twin.fingerprint, 20);
  await db.exec('savepoint gemela');
  let duplicated = null;
  try {
    await page('approver_record_meta_template_sent_v1', twin.id, '4444444444', 'PENDING', 'UTILITY', null);
  } catch (error) {
    duplicated = error;
  }
  // Volver al savepoint deshace tambien el set role de la llamada que fallo.
  await db.exec('rollback to savepoint gemela');
  if (duplicated?.code !== '23505') fail('dos solicitudes con el mismo id de Meta', duplicated?.code);
  // Una validada que vencio sin que nadie la tocara no retiene el nombre.
  const stale = await validated('att1_olvidada_01', 'UTILITY');
  await shift(`update public.meta_template_requests set validated_at = validated_at - interval '25 hours',
    expires_at = expires_at - interval '25 hours' where id = $1`, [stale.id]);
  const reused = await save(draftNamed('att1_olvidada_01'));
  if (!reused.ok || (await row(stale.id)).status !== 'vencida') fail('el nombre de una vencida sin marcar', reused);
  // El pedido de Meta tiene que ser el del borrador: otro cuerpo no se congela.
  const forged = structuredClone(BRIDGE.meta_request);
  forged.components.find((c) => c.type === 'BODY').text = 'Entra a https://otro.example ya.';
  await db.exec('savepoint pedido');
  let forgedError = null;
  try {
    await record(reused.request_id, reused.revision, { meta: { ...forged, name: 'att1_olvidada_01' } });
  } catch (error) {
    forgedError = error;
  }
  await db.exec('rollback to savepoint pedido');
  if (forgedError?.message !== 'meta_template_meta_request_invalid') fail('un pedido ajeno al borrador', forgedError?.message);
  // Las otras formas que arma el bridge (checkout de marketing con la baja y pie; la ficha con su medio)
  // pasan las formas de la base y se congelan.
  const extra = [];
  for (const item of EXTRA.cases) {
    const saved = await save(item.draft);
    if (!saved.ok) fail(`guardar ${item.draft.nombre}`, saved);
    const recorded = await record(saved.request_id, saved.revision, { result: item.result, meta: item.meta_request });
    if (recorded.status !== 'validada') fail(`validar ${item.draft.nombre}`, recorded);
    extra.push(item.draft.nombre);
  }
  await db.exec('rollback');
  results.bordes = { otras_formas: extra, pausada: pausedOut.status, tarde: lateOut.status, mismo_id: duplicated.code,
    vencida_sin_marcar: 'libera', pedido_ajeno: forgedError.message };
}

if (EMIT || CHECK) {
  const ids = new Map();
  const normalize = (value) => JSON.parse(JSON.stringify(value)
    .replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g, (id) => {
      if (!ids.has(id)) ids.set(id, `00000000-0000-4000-8000-${String(ids.size + 1).padStart(12, '0')}`);
      return ids.get(id);
    })
    .replace(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)/g, '2026-11-02T12:00:00.000000+00:00')
    .replace(/"fingerprint":"[0-9a-f]{64}"/g, `"fingerprint":"${'f'.repeat(64)}"`));
  const dir = join(root, 'tests/fixtures/meta_template_requests');
  const text = `${JSON.stringify({
    _capture: {
      source: 'tests/sql/followup_engine/validate_meta_template_requests.mjs --emit-fixture',
      migration: MIGRATION,
      note: 'la salida real de las RPC del agente sobre la cadena de migraciones en PGlite, datos sinteticos; '
        + 'uuid, horas y huella normalizados',
    },
    saved: normalize(outputs.saved),
    recorded: normalize(outputs.recorded),
    got: normalize(outputs.got),
    listed_open: normalize(outputs.listedOpen),
    listed_all: normalize(outputs.listedAll),
  }, null, 2)}\n`;
  if (CHECK && readFileSync(join(dir, 'rpc_outputs.json'), 'utf8') !== text) {
    fail('tests/fixtures/meta_template_requests/rpc_outputs.json no es la salida de hoy: regenerarlo con --emit-fixture');
  }
  if (EMIT) {
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, 'rpc_outputs.json'), text);
  }
}

console.log(JSON.stringify(results, null, 2));
console.log('validate_meta_template_requests: OK');
