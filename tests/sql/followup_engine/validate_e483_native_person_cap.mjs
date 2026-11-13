// El tope de mensajes proactivos por persona en el motor nativo (migracion
// 20261112230000, E-483, C6 del mapa «el AE maneja todos los flujos»; D5: 4 cada
// 5 dias, apagado por defecto).
//
// Por que: con varios toques y las difusiones de E-484 un numero nuevo se quema.
// El tope del legado (20261007000100) no convive con el motor y, prendido, deja
// la accion frenada viva, que frena la adopcion de la respuesta. Este cierra.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena entera,
// con el carrito capturado hotmart_cart_abandonment_rejected_v1.json) y la
// plantilla de Maribel (tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json),
// como validate_e483_native_multitouch.mjs.
//
// Prueba:
//   0. la forma: cambian la autorizacion nativa y la v1 del arranque, se suma la
//      v2 solo para service_role; correrla dos veces aborta;
//   1. un carrito nativo de tres toques con tope {2 envios, 120 h}: A1 y A2 salen
//      por la v2; Z llegaria al tercero;
//   2. lo que no cuenta o no frena (cada uno en una transaccion que se deshace):
//      la v1 sin tope; los envios fuera de la ventana; el seguimiento de una
//      conversacion (una autorizacion sin accion programada); dos autorizaciones
//      de la ruta motor del carrito del legado en la ventana; y rojo: con la
//      autorizacion de antes, la v2 deja pasar a Z;
//   3. la persona es el telefono: otro contacto con la otra forma del movil suma;
//   4. Z por la v2: terminado con tope_por_persona, el intento, la accion, la
//      secuencia y el caso cerrados con ese motivo, sin autorizacion ni bolsa, y
//      sin ninguna accion viva de la persona (la adopcion no se frena);
//   5. un tope invalido es 22023.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261112230000_e483_native_person_cap_v1.sql';
const TAIL = '_flow_engine_authorize_native_tail(uuid,uuid,flow_engine_runs,recovery_cases,flow_controls,timestamp with time zone)';
const MARK_V1 = 'mark_flow_engine_request_started_v1(uuid,uuid,text,bigint,timestamp with time zone)';
const MARK_V2 = 'mark_flow_engine_request_started_v2(uuid,uuid,text,bigint,timestamp with time zone,jsonb)';
const maribel = JSON.parse(readFileSync(join(root,
  'tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json'), 'utf8')).message_templates[0];
const results = {};
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify([])) {
  fail(`despues de ${MIGRATION} no va ninguna`, names.slice(position + 1));
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
  if (!sameSet(changed, [TAIL, MARK_V1]) || !sameSet(added, [MARK_V2]) || removed.length !== 0
      || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  for (const signature of [TAIL, MARK_V1, MARK_V2]) {
    if (!after.get(signature).definition.includes('e483_native_person_cap')) fail(`${signature}: sin la marca`);
  }
  for (const signature of [TAIL, MARK_V1]) previousDefinitions[signature] = before.get(signature).definition;
  if (after.get(MARK_V2).definition.includes('flujo_apagado')) fail('la v2 no decide motivos');
  if (after.get(MARK_V1).definition.includes("reason_code = 'flujo_apagado'")) fail('la v1 sigue con el literal');
  const grants = (await db.query(`select r.rolname, has_function_privilege(r.rolname, $1, 'EXECUTE') as can
    from pg_roles r where r.rolname in ('anon', 'authenticated', 'service_role') order by r.rolname`,
  [`public.${MARK_V2}`])).rows;
  if (JSON.stringify(grants.map((g) => [g.rolname, g.can])) !== JSON.stringify([['anon', false],
    ['authenticated', false], ['service_role', true]])) fail('los permisos de la v2', grants);
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'e483_native_person_cap_already_applied') fail('la segunda corrida aborto por otra cosa', error.message);
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { cambiadas: changed.length, nuevas: added, permisos: 'solo service_role' };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, reserve, accept, cartFor, dbNow, person, FLOWS, SCOPE, admit,
  createRoute, route } = world;
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
const send = (variables = ['nombre', 'producto']) => ({ plantilla: maribel.name, idioma: maribel.language, variables });
const CART = {
  esquema: 'setter-flujo/v1', flujo: 'carrito_tope', producto: A.funnel, estado: 'borrador', nombre: 'Carrito con tope',
  dispara: { evento: 'carrito' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [send(), { esperar: '1m' }, send(['nombre']), { esperar: '2m' }, send()],
  termina: { si: ['compra', 'contesta'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-carrito-tope', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
const CAP = { max_sends: 2, window_hours: 120 };
const MARK_CAP = 'select * from public.mark_flow_engine_request_started_v2($1,$2,$3,$4,$5,$6::jsonb)';
const MARK_PLAIN = 'select * from public.mark_flow_engine_request_started_v1($1,$2,$3,$4,$5)';
const start = async (reserved, cap = CAP) => one((await db.query(cap === null ? MARK_PLAIN : MARK_CAP,
  [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, await dbNow(),
    ...(cap === null ? [] : [JSON.stringify(cap)])])).rows, 'arranque');
// Cada variante en una transaccion que se deshace: el intento reservado de Z queda como estaba.
const tryInside = async (label, setup, cap = CAP) => {
  await db.exec('begin');
  try {
    await setup();
    return (await start(zReserved, cap)).outcome;
  } finally {
    await db.exec('rollback');
  }
};

// ---------------------------------------------------------------------------
// 1. A1 y A2 salen con el tope.
// ---------------------------------------------------------------------------
const flowId = await register(A.funnel, 'carrito_tope');
await cycle([versionItem(flowId, A.funnel, 'carrito_tope', CART)], 'v1 del carrito con tope');
const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
  [flowId]), 'propuesta').proposal_id;
const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
  [flowId, proposalId, A.timezone]), 'abrir').r);
if (!opened.ok || opened.state !== 'cohorte') fail('abrir el carrito con tope', opened);
// Un movil de Mexico con el 1 despues del 52: tiene otra forma (_whatsapp_phone_variants, 52/521).
const maria = await cartFor({ ...person('tope-maria', A, '52155512340'), country: '52', iso: 'MX' });
await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
  [flowId, maria.contact]);
const started = one((await asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
  [JSON.stringify(INSTANCE), JSON.stringify({
    fuente: 'hotmart', tipo: 'carrito', evento_id: maria.event, producto_ref: A.funnel, oferta: maria.offer.offer_code,
    contacto_id: maria.contact, momento: maria.cartArgs[6], chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
    destino: maria.phone,
  })])).rows)).filter((row) => row.flow_id === flowId), 'arranque');
if (started.outcome !== 'motor_arranco') fail('el carrito con tope arranca', started);
const lead = { label: 'tope-maria', chatwoot: '541' };
const actions = async () => sql(`select id, step_key, status, terminal_reason from public.scheduled_actions
  where recovery_case_id = $1 order by created_at`, [started.recovery_case_id]);
const dueNow = async (id) => sql("update public.scheduled_actions set due_at = now() - interval '1 second' where id = $1", [id]);
const first = await reserve(lead, started.scheduled_action_id);
const firstStart = await start(first);
if (firstStart.outcome !== 'request_started') fail('A1 sale con el tope', firstStart);
await accept(lead, first);
const second = (await actions()).find((row) => row.step_key === 'native_touch_2');
await dueNow(second.id);
const secondReserved = await reserve(lead, second.id);
const secondStart = await start(secondReserved);
if (secondStart.outcome !== 'request_started') fail('A2 sale con el tope', secondStart);
await accept(lead, secondReserved);
const third = (await actions()).find((row) => row.step_key === 'native_touch_3');
if (!third || third.status !== 'pending') fail('Z agendado', await actions());
await dueNow(third.id);
const zReserved = await reserve(lead, third.id);
const authorizations = async () => sql(`select id, action_id, attempt_id, authorized_at from public.flow_engine_request_authorizations
  where pool = 'att1-carrito-tope' order by authorized_at`);
const sent = await authorizations();
if (sent.length !== 2) fail('A1 y A2 autorizados', sent);
results.antes = { a1: firstStart.outcome, a2: secondStart.outcome, autorizados: sent.length };

// ---------------------------------------------------------------------------
// 2. Lo que no cuenta o no frena.
// ---------------------------------------------------------------------------
// La tabla es de solo agregar (reject_flow_engine_mutation): la prueba la envejece sin sus disparadores, adentro
// de la transaccion que se deshace.
const age = async (ids) => {
  await db.exec("set local session_replication_role = replica");
  await sql(`update public.flow_engine_request_authorizations
    set authorized_at = authorized_at - interval '121 hours' where id = any($1::uuid[])`, [ids]);
  await db.exec("set local session_replication_role = origin");
};
const passes = {
  sin_tope: await tryInside('v1', async () => {}, null),
  fuera_de_ventana: await tryInside('ventana', () => age(sent.map((row) => row.id))),
  // El seguimiento de una conversacion (claim_flow_engine_silence_v1) deja su autorizacion con la fila de
  // conversation_followup_events como accion: no es una accion programada.
  seguimiento_no_cuenta: await tryInside('seguimiento', async () => {
    await age([sent[0].id]);
    await sql(`insert into public.flow_engine_request_authorizations (flow_id, version, pool, legacy_scope_key,
        action_id, attempt_id, contact_id, local_budget_date, flow_generation, authorized_at)
      select $1, 1, 'att1-carrito-tope', null, x.id, x.id, $2, current_date, 0, now()
        from (select gen_random_uuid() as id) x`, [flowId, maria.contact]);
  }),
  // Correccion de la revision (2026-11-13): una autorizacion de la ruta motor del carrito del legado (un
  // recorrido con legacy_kind, sin flow_engine_native_runs) de la misma persona, dentro de la ventana, no
  // cuenta: el tope es del nativo.
  ruta_motor_no_cuenta: await tryInside('ruta motor', async () => {
    await age(sent.map((row) => row.id));
    const created = await createRoute(A.funnel, 'carrito', 'carrito', SCOPE);
    if (!created.ok) fail('la ruta del carrito del legado', created);
    const moved = await route(A.funnel, 'carrito', FLOWS.carrito, 'motor');
    if (!moved.ok) fail('la ruta del carrito del legado a motor', moved);
    const elena = await cartFor('tope-ruta-motor');
    const row = await admit(elena);
    if (row.outcome !== 'motor_arranco') fail('el carrito del legado por el motor', row);
    const legacyKind = one(await sql('select legacy_kind from public.flow_engine_runs where recovery_case_id = $1',
      [row.recovery_case_id]), 'recorrido').legacy_kind;
    if (legacyKind !== 'carrito') fail('el recorrido es del legado', legacyKind);
    for (let i = 0; i < 2; i += 1) {
      await sql(`insert into public.flow_engine_request_authorizations (flow_id, version, pool, legacy_scope_key,
          action_id, attempt_id, contact_id, local_budget_date, flow_generation, authorized_at)
        values ($1, 1, 'att1-carrito-tope', null, $2, gen_random_uuid(), $3, current_date, 0, now())`,
      [flowId, row.scheduled_action_id, maria.contact]);
    }
  }),
  rojo_sin_la_migracion: await tryInside('rojo', async () => {
    await db.exec(previousDefinitions[TAIL]);
  }),
};
for (const [label, outcome] of Object.entries(passes)) {
  if (outcome !== 'request_started') fail(`${label}: Z sale`, outcome);
}
results.no_frena = passes;

// ---------------------------------------------------------------------------
// 3. La persona es el telefono: otro contacto con la otra forma del movil suma.
// ---------------------------------------------------------------------------
const identity = one(await sql(`select identity_row.account_id, identity_row.external_user_id,
    public._whatsapp_phone_variants(identity_row.external_user_id) as variants
  from public.recovery_cases case_row join public.channel_identities identity_row
    on identity_row.id = case_row.selected_channel_identity_id where case_row.id = $1`,
[started.recovery_case_id]), 'identidad');
const otherForm = (identity.variants ?? []).find((value) => value !== identity.external_user_id);
const twin = async () => {
  await age([sent[0].id]);
  const contact = one(await sql(`insert into public.contacts (full_name, phone, country_iso)
    values ('Otra forma', $1, 'MX') returning id`, [otherForm]), 'contacto gemelo').id;
  await sql(`insert into public.channel_identities (contact_id, channel, account_id, external_user_id, identity_status)
    values ($1, 'whatsapp', $2, $3, 'active')`, [contact, identity.account_id, otherForm]);
  await sql(`insert into public.flow_engine_request_authorizations (flow_id, version, pool, legacy_scope_key,
      action_id, attempt_id, contact_id, local_budget_date, flow_generation, authorized_at)
    values ($1, 1, 'att1-carrito-tope', null, $2, gen_random_uuid(), $3, current_date, 0, now())`,
  [flowId, sent[0].action_id, contact]);
};
if (!otherForm) fail('el movil de la prueba tiene otra forma', identity);
{
  const withTwin = await tryInside('gemelo', twin);
  const alone = await tryInside('solo', () => age([sent[0].id]));
  if (withTwin !== 'terminado' || alone !== 'request_started') fail('la otra forma del movil suma', { withTwin, alone });
  results.telefono = { con_la_otra_forma: withTwin, sin_ella: alone };
}

// Rojo del cierre: con la v1 de antes, el toque frenado cerraria como flujo_apagado.
{
  await db.exec('begin');
  let closedAs;
  try {
    await db.exec(previousDefinitions[MARK_V1]);
    await start(zReserved);
    closedAs = one(await sql('select reason_code from public.followup_delivery_attempts where id = $1',
      [zReserved.attempt.id]), 'intento').reason_code;
  } finally {
    await db.exec('rollback');
  }
  if (closedAs !== 'flujo_apagado') fail('sin el cierre nuevo, el motivo seria el literal', closedAs);
  results.rojo_cierre = closedAs;
}

// ---------------------------------------------------------------------------
// 4. Z por la v2: cierra con tope_por_persona.
// ---------------------------------------------------------------------------
const capped = await start(zReserved);
if (capped.outcome !== 'terminado' || capped.reason !== 'tope_por_persona') fail('Z llega al tope', capped);
const attempt = one(await sql('select phase, outcome, reason_code from public.followup_delivery_attempts where id = $1',
  [zReserved.attempt.id]), 'intento de Z');
const zAction = one(await sql('select status, terminal_reason, followup_sequence_id from public.scheduled_actions where id = $1',
  [third.id]), 'accion de Z');
const sequence = one(await sql('select status, completion_reason from public.followup_sequences where id = $1',
  [zAction.followup_sequence_id]), 'secuencia');
const kase = one(await sql('select status from public.recovery_cases where id = $1', [started.recovery_case_id]), 'caso');
const event = one(await sql(`select data ->> 'reason_code' as reason from public.conversation_events
  where recovery_case_id = $1 and related_action_id = $2 and event_type = 'followup_action_reevaluated'
    and data ->> 'decision' = 'cancel'`, [started.recovery_case_id, third.id]), 'evento');
const live = one(await sql(`select count(*)::int as n from public.scheduled_actions action_row
  join public.recovery_cases case_row on case_row.id = action_row.recovery_case_id
  where case_row.contact_id = $1 and action_row.status in ('pending', 'deferred', 'retryable_failed', 'delivery_unknown')`,
[maria.contact]), 'vivas').n;
const after = await authorizations();
if (attempt.phase !== 'completed' || attempt.outcome !== 'failed_before_request' || attempt.reason_code !== 'tope_por_persona'
    || zAction.status !== 'cancelled' || zAction.terminal_reason !== 'tope_por_persona'
    || sequence.status !== 'completed' || sequence.completion_reason !== 'tope_por_persona'
    || kase.status !== 'cancelled' || event.reason !== 'tope_por_persona' || live !== 0 || after.length !== 2) {
  fail('el cierre por el tope', { attempt, zAction, sequence, kase, event, live, autorizados: after.length });
}
results.tope = { z: `${capped.outcome}:${capped.reason}`, caso: kase.status, acciones_vivas: live, autorizados: after.length };

// ---------------------------------------------------------------------------
// 5. Un tope invalido.
// ---------------------------------------------------------------------------
const invalid = {};
for (const [label, cap] of Object.entries({
  cero: { max_sends: 0, window_hours: 120 }, ventana_larga: { max_sends: 4, window_hours: 721 },
  decimal: { max_sends: 4.5, window_hours: 120 }, clave_de_mas: { ...CAP, otra: 1 }, texto: { max_sends: '4', window_hours: 120 },
})) {
  try {
    await db.query(MARK_CAP, [third.id, zReserved.attempt.id, zReserved.worker, zReserved.lease, await dbNow(), JSON.stringify(cap)]);
    fail(`${label}: tendria que ser 22023`);
  } catch (error) {
    if (error.code !== '22023' || error.message !== 'invalid_native_person_cap') fail(`${label}: otro error`, error.message);
    invalid[label] = error.code;
  }
}
results.invalido = invalid;

console.log(JSON.stringify(results, null, 2));
await db.close();
