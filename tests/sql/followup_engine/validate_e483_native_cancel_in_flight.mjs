// `en_curso: cancelar` en la version de un flujo nativo (migracion
// 20261112210100, E-483, C8 del mapa «el AE maneja todos los flujos»,
// 2026-11-12).
//
// Por que: con varios toques (20261112210000), una version con un error deja
// gente a mitad de la secuencia (A1 mandado, A2 esperando con la plantilla
// vieja). Hasta aca la base aceptaba solo `en_curso: seguir`.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (con el carrito
// capturado hotmart_cart_abandonment_rejected_v1.json) y la plantilla de
// Maribel (tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json),
// como validate_e483_native_multitouch.mjs.
//
// Prueba:
//   0. la forma: cambian _flow_change_items_are_valid y _flow_change_publish con
//      su marca, se suma _flow_change_cancel_in_flight_v1 sin permisos para la
//      API; correrla dos veces aborta;
//   1. un carrito nativo de tres toques en cohorte: Maria con A1 mandado y A2
//      esperando; Juana con A1 mandado y A2 reservado (un intento empezado);
//   2. seguir: la v2 con en_curso: seguir deja A2 de Maria pendiente, en su
//      recorrido de la v1;
//   3. rojo: con la publicacion de antes, la v3 con cancelar publica y A2 sigue
//      pendiente; con la forma de antes, cancelar ni se guarda;
//   4. cancelar: la v3 con cancelar cancela A2 de Maria (cambio_de_version),
//      cierra su caso y su secuencia con ese motivo y Z no se agenda; Juana no
//      se toca; el resumen de la publicacion lo cuenta;
//   5. un recorrido nuevo arranca con la version activa (la v3);
//   6. el legado: cancelar en el carrito del legado no publica
//      (en_curso_not_supported) y migrar no se guarda.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261112210100_e483_native_cancel_in_flight_v1.sql';
const CHANGED = ['_flow_change_items_are_valid(jsonb)', '_flow_change_publish(text,text,uuid,text,jsonb)'];
const ADDED = ['_flow_change_cancel_in_flight_v1(uuid,integer,uuid,timestamp with time zone)'];
const maribel = JSON.parse(readFileSync(join(root,
  'tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json'), 'utf8')).message_templates[0];
const results = {};
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues va solo prioridad.le_gana_a en el nativo (20261112210200, E-483, C7): no toca la publicacion.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261112210200_e483_native_priority_wins_v1.sql',
  // El pago rechazado completo en el nativo (20261112210300, E-483, C3).
  '20261112210300_e483_native_condition_after_send_v1.sql',
  // El pago rechazado sin formulario de una oferta de campana (20261112210400, E-483, pieza PF).
  '20261112210400_e483_payment_failure_by_phone_v1.sql',
  // El cupon propio por persona en el nativo (20261112210500, E-483, C2).
  '20261112210500_e483_native_personal_coupon_v1.sql',
  // El evento de un disparo por audiencia en el nativo (20261112210600, E-483, para E-484).
  '20261112210600_e483_native_audience_source_v1.sql', '20261112220000_flow_audience_triggers_v1.sql', '20261112230000_e483_native_person_cap_v1.sql'])) {
  fail(`despues de ${MIGRATION} van solo las de C7 y C3`, names.slice(position + 1));
}

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const previousDefinitions = {};
{
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
  const before = await snapshot(db);
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.has(s) && after.get(s).definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && ['acl', 'prosecdef', 'provolatile'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, CHANGED) || !sameSet(added, ADDED) || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  for (const signature of CHANGED) {
    if (!after.get(signature).definition.includes('e483_native_cancel_in_flight')) fail(`${signature}: sin la marca`);
    previousDefinitions[signature] = before.get(signature).definition;
  }
  const privileges = (await db.query(`select r.rolname, has_function_privilege(r.rolname, $1, 'EXECUTE') as can
    from pg_roles r where r.rolname in ('anon', 'authenticated', 'service_role') order by r.rolname`,
  [`public.${ADDED[0]}`])).rows;
  if (privileges.some((row) => row.can)) fail('la funcion nueva no es para la API', privileges);
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'e483_native_cancel_in_flight_already_applied') fail('la segunda corrida aborto por otra cosa', error.message);
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { cambiadas: changed.length, agregadas: added.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, FLOWS, INSTANCE, rpc, register, cycle, versionItem, reserve, startNew, accept, cartFor, cartDefinition } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
await db.exec('alter role service_role bypassrls');
const asService = async (run) => {
  await db.exec('set role service_role');
  try {
    return await run();
  } finally {
    await db.exec('reset role');
  }
};
const send = (variables = ['nombre', 'producto'], extra = {}) => ({
  plantilla: maribel.name, idioma: maribel.language, variables, ...extra });
const CART = {
  esquema: 'setter-flujo/v1', flujo: 'carrito_tres', producto: A.funnel, estado: 'borrador', nombre: 'Carrito de tres toques',
  dispara: { evento: 'carrito' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [send(), { esperar: '1m' }, send(['nombre']), { esperar: '2m' }, send()],
  termina: { si: ['compra', 'contesta'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-carrito-tres', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
const withWait = (wait) => ({ ...CART, pasos: [send(), { esperar: wait }, send(['nombre']), { esperar: '2m' }, send()] });

// La publicacion paso a paso (como cycle del mundo), devolviendo cada respuesta.
const VALIDATOR = { contract: 'setter-flujo/v1', rules_version: 1, phase: 'publish', actor: 'operador',
  product_version: 'v1.9.0', git_sha: 'abcdef0' };
const CLEAN = { blocks: [], warnings: [] };
const publishSteps = async (items, purpose) => {
  const flowIds = [...new Set(items.map((item) => item.flow_id))];
  const draftBase = one(await sql(`select coalesce(jsonb_object_agg(flow_id::text, jsonb_build_object('active_version',
    active_version)), '{}'::jsonb) b from public.flow_controls where flow_id = any($1::uuid[])`, [flowIds]), 'base').b;
  const saved = await rpc('operator_save_flow_change_draft_v1', INSTANCE, null, null, items, draftBase, purpose, 'dan');
  if (!saved.ok) return { saved };
  const instanceBase = one(await sql('select public._flow_store_base($1, $2, $3::uuid[]) b',
    [A.tenant, A.ally, flowIds]), 'base de la instancia').b;
  const validated = await rpc('operator_record_flow_change_validation_v1', INSTANCE, saved.proposal_id, saved.revision,
    instanceBase, VALIDATOR, CLEAN, {}, 'dan');
  const approved = await rpc('operator_approve_flow_change_v1', INSTANCE, saved.proposal_id, validated.fingerprint, 'dan');
  const published = await rpc('operator_publish_flow_change_v1', INSTANCE, saved.proposal_id, validated.fingerprint, {
    fingerprint: validated.fingerprint, validator: VALIDATOR, result: CLEAN, catalog_sha256: 'a'.repeat(64),
    validated_at: new Date().toISOString(),
  });
  return { saved, validated, approved, published };
};
const cancelItem = (flowId, product, flujo, definicion) => ({ ...versionItem(flowId, product, flujo, definicion), en_curso: 'cancelar' });

// ---------------------------------------------------------------------------
// 1. El carrito de tres toques, con Maria esperando A2 y Juana mandandolo.
// ---------------------------------------------------------------------------
const flowId = await register(A.funnel, 'carrito_tres');
await cycle([versionItem(flowId, A.funnel, 'carrito_tres', CART)], 'v1 del carrito de tres toques');
const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
  [flowId]), 'propuesta').proposal_id;
const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
  [flowId, proposalId, A.timezone]), 'abrir').r);
if (!opened.ok || opened.state !== 'cohorte') fail('abrir el carrito de tres toques', opened);
const activeVersion = async () => one(await sql('select active_version from public.flow_controls where flow_id = $1',
  [flowId]), 'control').active_version;

const startRun = async (label, chatwoot) => {
  const lead = await cartFor(label);
  await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
    [flowId, lead.contact]);
  const flowEvent = {
    fuente: 'hotmart', tipo: 'carrito', evento_id: lead.event, producto_ref: A.funnel, oferta: lead.offer.offer_code,
    contacto_id: lead.contact, momento: lead.cartArgs[6], chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
    destino: lead.phone,
  };
  const started = one((await asService(async () => (await db.query(
    'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
    [JSON.stringify(INSTANCE), JSON.stringify(flowEvent)])).rows)).filter((row) => row.flow_id === flowId), `${label} arranque`);
  if (started.outcome !== 'motor_arranco') fail(`${label} arranca`, started);
  return { label, chatwoot, caseId: started.recovery_case_id, first: started.scheduled_action_id };
};
const actionsOf = async (caseId) => sql(`select id, step_key, status, terminal_reason, lease_owner, policy_version
  from public.scheduled_actions where recovery_case_id = $1 order by created_at`, [caseId]);
const caseRow = async (caseId) => one(await sql(`select status, next_contact_reason, context
  from public.recovery_cases where id = $1`, [caseId]), 'caso');
const runVersion = async (caseId) => one(await sql('select version from public.flow_engine_runs where recovery_case_id = $1',
  [caseId]), 'recorrido').version;
const sendFirst = async (run) => {
  const reserved = await reserve(run, run.first);
  if ((await startNew(reserved)).outcome !== 'request_started') fail(`${run.label}: el arranque de A1`);
  await accept(run, reserved);
  const second = (await actionsOf(run.caseId)).find((row) => row.step_key === 'native_touch_2');
  if (!second || second.status !== 'pending') fail(`${run.label}: A2 agendado`, await actionsOf(run.caseId));
  return second;
};

const maria = await startRun('cancel-maria', '610');
const mariaA2 = await sendFirst(maria);
const juana = await startRun('cancel-juana', '611');
const juanaA2 = await sendFirst(juana);
await sql("update public.scheduled_actions set due_at = now() - interval '1 second' where id = $1", [juanaA2.id]);
const juanaReserved = await reserve(juana, juanaA2.id);
if (juanaReserved.attempt.phase !== 'reserved') fail('el intento de A2 de Juana', juanaReserved.attempt);
results.mundo = { maria_a2: mariaA2.status, juana_a2: 'reservado' };

// ---------------------------------------------------------------------------
// 2. seguir: A2 de Maria sigue con la v1.
// ---------------------------------------------------------------------------
await cycle([versionItem(flowId, A.funnel, 'carrito_tres', withWait('2m'))], 'v2 con seguir');
if (await activeVersion() !== 2) fail('la v2 activa', await activeVersion());
{
  const a2 = (await actionsOf(maria.caseId)).find((row) => row.id === mariaA2.id);
  const kase = await caseRow(maria.caseId);
  if (a2.status !== 'pending' || a2.terminal_reason !== null || kase.status !== 'active' || await runVersion(maria.caseId) !== 1) {
    fail('con seguir, A2 sigue con la v1', { a2, kase });
  }
  results.seguir = { a2: a2.status, caso: kase.status, recorrido_version: 1 };
}

// ---------------------------------------------------------------------------
// 3. Rojo: con las definiciones de antes.
// ---------------------------------------------------------------------------
await db.exec('begin');
try {
  await db.exec(previousDefinitions['_flow_change_publish(text,text,uuid,text,jsonb)']);
  const steps = await publishSteps([cancelItem(flowId, A.funnel, 'carrito_tres', withWait('3m'))], 'rojo de la publicacion');
  const a2 = (await actionsOf(maria.caseId)).find((row) => row.id === mariaA2.id);
  if (!steps.published?.ok || a2.status !== 'pending') fail('sin la publicacion nueva, cancelar no cancela nada', { steps, a2 });
  results.rojo = { publicacion_vieja: { publica: steps.published.ok, a2: a2.status } };
} finally {
  await db.exec('rollback');
}
await db.exec('begin');
try {
  await db.exec(previousDefinitions['_flow_change_items_are_valid(jsonb)']);
  let saved;
  try {
    saved = (await publishSteps([cancelItem(flowId, A.funnel, 'carrito_tres', withWait('3m'))], 'rojo de la forma')).saved;
  } catch (error) {
    saved = { ok: false, error: error.message };
  }
  if (saved.ok) fail('sin la forma nueva, cancelar no se guarda', saved);
  results.rojo.forma_vieja = saved.error ?? saved.reason_code ?? 'rechazado';
} finally {
  await db.exec('rollback');
}
if (await activeVersion() !== 2) fail('el rojo se deshizo', await activeVersion());

// ---------------------------------------------------------------------------
// 4. cancelar: A2 de Maria se cancela, su caso se cierra y Z no se agenda.
// ---------------------------------------------------------------------------
{
  const steps = await publishSteps([cancelItem(flowId, A.funnel, 'carrito_tres', withWait('3m'))], 'v3 con cancelar');
  if (!steps.published?.ok) fail('la v3 con cancelar publica', steps);
  if (await activeVersion() !== 3) fail('la v3 activa', await activeVersion());
  const mariaActions = await actionsOf(maria.caseId);
  const a2 = mariaActions.find((row) => row.id === mariaA2.id);
  const kase = await caseRow(maria.caseId);
  const sequences = await sql('select status, cancel_reason from public.followup_sequences where recovery_case_id = $1',
    [maria.caseId]);
  const events = await sql(`select data from public.conversation_events
    where recovery_case_id = $1 and event_type = 'recovery_case_closed_by_version_change'`, [maria.caseId]);
  if (a2.status !== 'cancelled' || a2.terminal_reason !== 'cambio_de_version') fail('A2 de Maria cancelada', a2);
  if (kase.status !== 'cancelled' || kase.next_contact_reason !== 'cambio_de_version'
      || kase.context?.closed_by_version_change?.from_version !== 1
      || kase.context?.closed_by_version_change?.to_version !== 3) fail('el caso de Maria cerrado', kase);
  if (sequences.length === 0 || sequences.some((row) => row.status !== 'cancelled' || row.cancel_reason !== 'cambio_de_version')) {
    fail('las secuencias de Maria', sequences);
  }
  if (events.length !== 1 || events[0].data.reason_code !== 'cambio_de_version') fail('el evento del cierre', events);
  if (mariaActions.some((row) => row.step_key === 'native_touch_3')) fail('Z no se agenda', mariaActions);
  // Nada de lo de Maria vuelve a salir.
  const live = mariaActions.filter((row) => ['pending', 'deferred', 'retryable_failed', 'delivery_unknown'].includes(row.status));
  if (live.length !== 0) fail('a Maria no le queda nada vivo', live);

  // Juana: su A2 ya se esta mandando, no se toca.
  const juanaA2Now = (await actionsOf(juana.caseId)).find((row) => row.id === juanaA2.id);
  const juanaCase = await caseRow(juana.caseId);
  if (juanaA2Now.status !== 'pending' || juanaA2Now.lease_owner === null || juanaCase.status !== 'active'
      || juanaCase.context?.closed_by_version_change) fail('el intento empezado de Juana no se toca', { juanaA2Now, juanaCase });
  const attempt = one(await sql('select phase from public.followup_delivery_attempts where id = $1', [juanaReserved.attempt.id]), 'intento');
  if (attempt.phase !== 'reserved') fail('el intento de Juana sigue', attempt);

  // El resumen de la publicacion.
  const published = one(await sql(`select data from public.flow_change_events
    where proposal_id = $1 and event_type = 'flow_change_published'`, [steps.saved.proposal_id]), 'evento publicado').data;
  const summary = published.flows.find((row) => row.flow_id === flowId);
  if (summary.en_curso !== 'cancelar' || summary.cancelados?.cerrados !== 1 || summary.cancelados?.mandandose !== 1) {
    fail('el resumen de la publicacion', summary);
  }
  results.cancelar = { a2_maria: `${a2.status}:${a2.terminal_reason}`, caso_maria: kase.status,
    juana: { a2: juanaA2Now.status, caso: juanaCase.status }, resumen: summary.cancelados };
}

// ---------------------------------------------------------------------------
// 5. Un recorrido nuevo arranca con la v3.
// ---------------------------------------------------------------------------
{
  const ana = await startRun('cancel-ana', '612');
  const version = await runVersion(ana.caseId);
  const first = (await actionsOf(ana.caseId))[0];
  if (version !== 3 || first.status !== 'pending') fail('el recorrido nuevo arranca con la v3', { version, first });
  results.nuevo = { recorrido_version: version };
}

// ---------------------------------------------------------------------------
// 6. El legado: cancelar no publica y migrar no se guarda.
// ---------------------------------------------------------------------------
{
  const before = one(await sql('select active_version from public.flow_controls where flow_id = $1', [FLOWS.carrito]), 'legado').active_version;
  const steps = await publishSteps([cancelItem(FLOWS.carrito, A.funnel, 'carrito', cartDefinition(A.funnel, { a2Wait: '3m' }))],
    'cancelar en el legado');
  const after = one(await sql('select active_version from public.flow_controls where flow_id = $1', [FLOWS.carrito]), 'legado').active_version;
  if (steps.published?.ok !== false || steps.published?.reason_code !== 'en_curso_not_supported' || after !== before) {
    fail('cancelar en el legado no publica', { steps, before, after });
  }
  const rejected = one(await sql(`select reason_code from public.flow_change_events
    where proposal_id = $1 and event_type = 'flow_change_publish_rejected'`, [steps.saved.proposal_id]), 'rechazo').reason_code;
  if (rejected !== 'en_curso_not_supported') fail('el evento del rechazo', rejected);
  let migrate;
  try {
    migrate = (await publishSteps([{ ...versionItem(flowId, A.funnel, 'carrito_tres', withWait('4m')), en_curso: 'migrar' }],
      'migrar')).saved;
  } catch (error) {
    migrate = { ok: false, error: error.message };
  }
  if (migrate.ok) fail('migrar no se guarda', migrate);
  results.legado = { cancelar: steps.published.reason_code, migrar: migrate.error ?? migrate.reason_code ?? 'rechazado' };
}

console.log(JSON.stringify(results, null, 2));
await db.close();
