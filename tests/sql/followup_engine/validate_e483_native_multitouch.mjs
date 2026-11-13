// El motor nativo manda hasta tres toques con espera (migracion 20261112210000,
// E-483, C1 y C5 del mapa «el AE maneja todos los flujos», 2026-11-12).
//
// Por que: un flujo nativo era de un solo envio por un literal de la base
// (_flow_engine_native_steps) y la forma que abria _flow_engine_native_unsupported.
// El sustrato del legado (compilador, agenda del paso siguiente, contexto del
// paso, arranque y bolsa) ya sabe varios pasos: lo mide esta prueba.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera, con el carrito capturado hotmart_cart_abandonment_rejected_v1.json) y
// la plantilla de Maribel (tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json).
//
// Prueba:
//   0. la forma: cambian solo las dos funciones, con su marca; correrla dos
//      veces aborta; la compra sigue de un envio;
//   1. un carrito nativo de tres toques (A1, 1 minuto, A2, 2 minutos, Z, con
//      compra y contesta) se publica, se abre y su politica tiene tres pasos;
//   2. el despacho: A1 sale y, al aceptarse, queda agendado native_touch_2 a su
//      espera; su contexto trae su plantilla y sus variables, el arranque lo
//      autoriza y la bolsa cuenta dos envios; al aceptarlo queda native_touch_3;
//   3. la respuesta corta: con un mensaje de la persona despues de A2, Z se
//      cancela con prospect_replied y no sale;
//   4. lo que no abre: sin contesta, la compra con dos envios, cuatro envios, un
//      boton en el segundo envio, un pago rechazado con una plantilla y una
//      condicion; y lo de un toque, igual que antes;
//   5. rojo: con las definiciones de antes, el carrito de tres toques es
//      motor_nativo:forma.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261112210000_e483_native_multitouch_v1.sql';
const CHANGED = ['_flow_engine_native_steps(text)', '_flow_engine_native_unsupported(jsonb)'];
const maribel = JSON.parse(readFileSync(join(root,
  'tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json'), 'utf8')).message_templates[0];
const results = {};
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues van en_curso: cancelar en el nativo (20261112210100, E-483, C8) y prioridad.le_gana_a en el nativo
// (20261112210200, E-483, C7): ninguna toca la forma nativa.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261112210100_e483_native_cancel_in_flight_v1.sql',
  '20261112210200_e483_native_priority_wins_v1.sql',
  // El pago rechazado completo en el nativo (20261112210300, E-483, C3).
  '20261112210300_e483_native_condition_after_send_v1.sql',
  // El pago rechazado sin formulario de una oferta de campana (20261112210400, E-483, pieza PF).
  '20261112210400_e483_payment_failure_by_phone_v1.sql',
  // El cupon propio por persona en el nativo (20261112210500, E-483, C2).
  '20261112210500_e483_native_personal_coupon_v1.sql'])) {
  fail(`despues de ${MIGRATION} van solo las de C8 y C7`, names.slice(position + 1));
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
  if (!sameSet(changed, CHANGED) || added.length !== 0 || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  for (const signature of CHANGED) {
    if (!after.get(signature).definition.includes('e483_native_multitouch')) fail(`${signature}: sin la marca`);
    previousDefinitions[signature] = before.get(signature).definition;
  }
  const lengths = (await db.query(`select k, jsonb_array_length(public._flow_engine_native_steps('nativo:' || k)) n
    from unnest(array['compra', 'carrito', 'intencion', 'pago_fallido']) k order by k`)).rows;
  if (JSON.stringify(lengths.map((r) => [r.k, r.n])) !== JSON.stringify([['carrito', 3], ['compra', 1], ['intencion', 3], ['pago_fallido', 3]])) {
    fail('las formas', lengths);
  }
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'e483_native_multitouch_already_applied') fail('la segunda corrida aborto por otra cosa', error.message);
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { cambiadas: changed.length, formas: Object.fromEntries(lengths.map((r) => [r.k, r.n])) };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, reserve, startNew, accept, cartFor } = world;
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
const unsupported = async (definition) => one(await sql('select public._flow_engine_native_unsupported($1::jsonb) as r',
  [JSON.stringify(definition)]), 'unsupported').r;

// ---------------------------------------------------------------------------
// 1. Se publica, se abre y compila a tres pasos.
// ---------------------------------------------------------------------------
const flowId = await register(A.funnel, 'carrito_tres');
await cycle([versionItem(flowId, A.funnel, 'carrito_tres', CART)], 'v1 del carrito de tres toques');
const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
  [flowId]), 'propuesta').proposal_id;
const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
  [flowId, proposalId, A.timezone]), 'abrir').r);
if (!opened.ok || opened.state !== 'cohorte') fail('abrir el carrito de tres toques', opened);
const maria = await cartFor('tres-maria');
await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
  [flowId, maria.contact]);
const flowEvent = {
  fuente: 'hotmart', tipo: 'carrito', evento_id: maria.event, producto_ref: A.funnel, oferta: maria.offer.offer_code,
  contacto_id: maria.contact, momento: maria.cartArgs[6], chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
  destino: maria.phone,
};
const started = one((await asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
  [JSON.stringify(INSTANCE), JSON.stringify(flowEvent)])).rows)).filter((row) => row.flow_id === flowId), 'arranque');
if (started.outcome !== 'motor_arranco') fail('el carrito de tres toques arranca', started);
const kase = one(await sql('select * from public.recovery_cases where id = $1', [started.recovery_case_id]), 'caso');
const policy = one(await sql('select * from public.followup_policy_versions where policy_key = $1', [kase.policy_key]), 'politica');
if (policy.max_automatic_messages !== 3
    || JSON.stringify(policy.steps.map((s) => [s.step_key, s.delay ?? null, s.approved_template ?? null]))
       !== JSON.stringify([['first_contact', null, null], ['native_touch_2', '1 minute', true], ['native_touch_3', '3 minutes', true]])) {
  fail('la politica de tres toques', policy.steps);
}
results.publicacion = { estado: opened.state, pasos: policy.steps.map((s) => s.step_key) };

// ---------------------------------------------------------------------------
// 2. El despacho de los tres toques.
// ---------------------------------------------------------------------------
const lead = { label: 'tres-maria', chatwoot: '540' };
const actions = async () => sql(`select id, step_key, action_type, status from public.scheduled_actions
  where recovery_case_id = $1 order by created_at`, [started.recovery_case_id]);
const dueNow = async (id) => sql("update public.scheduled_actions set due_at = now() - interval '1 second' where id = $1", [id]);
const first = await reserve(lead, started.scheduled_action_id);
if ((await startNew(first)).outcome !== 'request_started') fail('el arranque de A1');
await accept(lead, first);
const second = (await actions()).find((row) => row.step_key === 'native_touch_2');
if (!second || second.action_type !== 'no_reply_review' || second.status !== 'pending') fail('A2 agendado', await actions());
await dueNow(second.id);
const secondReserved = await reserve(lead, second.id);
const context = one(await asService(async () => (await db.query('select * from public.flow_engine_execution_context_v1($1)',
  [second.id])).rows), 'contexto de A2');
if (context.send_ordinal !== 2 || context.template_name !== maribel.name
    || JSON.stringify(context.template_variables) !== JSON.stringify(['nombre']) || context.template_button !== null) {
  fail('el contexto de A2', context);
}
const secondStart = await startNew(secondReserved);
if (secondStart.outcome !== 'request_started' || !secondStart.flow_authorization_id) fail('el arranque de A2', secondStart);
await accept(lead, secondReserved);
const pool = one(await sql('select count(*)::int as n from public.flow_engine_request_authorizations where pool = $1',
  ['att1-carrito-tres']), 'bolsa').n;
if (pool !== 2) fail('la bolsa cuenta cada envio', pool);
const third = (await actions()).find((row) => row.step_key === 'native_touch_3');
if (!third || third.status !== 'pending') fail('Z agendado', await actions());
results.despacho = { a2: secondStart.outcome, bolsa: pool, z: third.status };

// ---------------------------------------------------------------------------
// 3. La respuesta corta el toque siguiente.
// ---------------------------------------------------------------------------
await dueNow(third.id);
{
  const claimed = await (async () => {
    const now = (await sql('select now() as n'))[0].n;
    const rows = await sql("select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)", ['w-tres', now]);
    return { now, row: one(rows.filter((r) => r.id === third.id), 'claim de Z') };
  })();
  const decision = one(await sql(`select * from public.reevaluate_followup_action(p_action_id => $1, p_worker_id => $2,
    p_lease_generation => $3, p_now => $4, p_chatwoot_checked => true, p_chatwoot_conversation_id => $5,
    p_chatwoot_checkpoint_message_id => '9', p_chatwoot_checkpoint_at => $4, p_chatwoot_status => 'open',
    p_chatwoot_can_reply => true, p_chatwoot_anchor_found => true, p_chatwoot_automation_paused => false,
    p_chatwoot_inbound_after_anchor => true, p_chatwoot_human_activity_after_anchor => false)`,
  [third.id, 'w-tres', claimed.row.lease_generation, claimed.now, lead.chatwoot]), 'reevaluacion de Z');
  if (decision.decision !== 'cancel' || decision.reason_code !== 'prospect_replied') fail('la respuesta corta Z', decision);
  results.respuesta = { decision: decision.decision, motivo: decision.reason_code };
}

// ---------------------------------------------------------------------------
// 4. Lo que no abre, y lo de un toque igual.
// ---------------------------------------------------------------------------
const PAY_CHAIN = { condicion: { dato: 'tipo_de_rechazo', es: ['sin_saldo'] }, entonces: [send()], si_no: [send()] };
const cases = {
  sin_contesta: { ...CART, termina: { si: ['compra'], vence: '1d' } },
  compra_dos: { ...CART, dispara: { evento: 'compra' }, entra: { permiso: 'compra', contesto_a: [] },
    pasos: [send(), { esperar: '1m' }, send()] },
  cuatro: { ...CART, pasos: [...CART.pasos, { esperar: '3m' }, send()] },
  boton_segundo: { ...CART, dispara: { evento: 'pago_fallido' },
    pasos: [send(), { esperar: '1m' }, send(['nombre', 'producto'], { boton: { link: 'checkout' } })] },
  pago_plantilla_y_condicion: { ...CART, dispara: { evento: 'pago_fallido' }, pasos: [send(), { esperar: '1m' }, PAY_CHAIN] },
};
const refusals = {};
for (const [label, definition] of Object.entries(cases)) {
  refusals[label] = await unsupported(definition);
  if (refusals[label] !== 'motor_nativo:forma') fail(`${label} no se abre`, refusals[label]);
}
const ONE = { ...CART, pasos: [send()], termina: { si: ['compra'], vence: '1d' } };
const oneTouch = {
  carrito: await unsupported(ONE),
  boton_primero: await unsupported({ ...CART, dispara: { evento: 'pago_fallido' },
    pasos: [send(['nombre', 'producto'], { boton: { link: 'checkout' } }), { esperar: '1m' }, send()] }),
  intencion_tres: await unsupported({ ...CART, dispara: { evento: 'intencion' } }),
  pago_cadena: await unsupported({ ...ONE, dispara: { evento: 'pago_fallido' }, pasos: [PAY_CHAIN] }),
};
for (const [label, reason] of Object.entries(oneTouch)) {
  if (reason !== null) fail(`${label} se abre`, reason);
}
results.no_abre = refusals;

// ---------------------------------------------------------------------------
// 5. Rojo: con las definiciones de antes.
// ---------------------------------------------------------------------------
await db.exec('begin');
let red;
try {
  for (const signature of CHANGED) await db.exec(previousDefinitions[signature]);
  red = await unsupported(CART);
} finally {
  await db.exec('rollback');
}
if (red !== 'motor_nativo:forma') fail('sin la migracion, tres toques no se abren', red);
results.rojo = red;

console.log(JSON.stringify(results, null, 2));
await db.close();
