// De punta a punta: una ocurrencia manual de E-484 (20261112220000) entra al
// motor nativo por la admision de la fuente audiencia de E-483 (20261112210600).
//
// Por que: las dos mitades se probaron por separado (validate_flow_audience_triggers.mjs
// la lista congelada; validate_e483_native_audience_source.mjs la admision con un
// evento armado a mano). Aca la persona que suelta la lista es la que entra al
// motor, con el evento que arma el bridge (src/bridge/flow_audience_opener.py,
// audience_event: exactamente esas claves) y la clave que pasa el opener con
// FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED (admit_flow_engine_event(..., audience=True)
// -> p_instance.native_audience). Es lo que hace el barredor por cada persona:
// soltar, abrir, asentar.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (el binding, el
// formulario con consentimiento por la admision portable de hoy, la plantilla
// A2 del carrito de ATT1); la persona que confirma entra por «Entrar con Slack»
// como en la pagina.
//
// Prueba:
//   1. un flujo dispara.manual a prospectas, publicado y abierto en el nativo
//      (cohorte); una persona arma la lista (materializada), la confirma con el
//      conteo exacto y el segundo paso; la base suelta a las dos;
//   2. rojo, sin el enganche: el mismo evento sin native_audience es 22023
//      invalid_flow_engine_event y no arranca nada (lo que pasaba con el opener
//      de E-484 tal como estaba);
//   3. con el enganche: dos recorridos nativos atados al flujo y a la version 1,
//      con la ocurrencia y la forma manual en el caso; se asienta arranco y la
//      ocurrencia termina; la primera plantilla de cada una sale (la
//      reevaluacion la ejecuta y la puerta la autoriza) y queda el segundo toque.
import { createHash } from 'node:crypto';
import { buildWorld, fail, one } from './validate_flow_engine_motor_v1.mjs';

const results = {};
const sha = (text) => createHash('sha256').update(text).digest('hex');
const world = await buildWorld();
const { db, A, A2, INSTANCE, person, admitForm, register, cycle, versionItem, reserve, startNew, accept, dbNow } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const call = async (text, params) => JSON.parse(one(await sql(`select ${text}::text as r`, params), text).r);
await db.exec('alter role service_role bypassrls');
const as = async (role, run) => {
  await db.exec(`set role ${role}`);
  try {
    return await run();
  } finally {
    await db.exec('reset role');
  }
};
const rpc = async (fn, ...args) => {
  const marks = args.map((_, i) => `$${i + 1}`).join(', ');
  const values = args.map((a) => (a !== null && typeof a === 'object' ? JSON.stringify(a) : a));
  return JSON.parse(one(await sql(`select public.${fn}(${marks})::text as r`, values), fn).r);
};

// ---------------------------------------------------------------------------
// 1. El flujo manual, la lista, la confirmacion y lo que suelta la base.
// ---------------------------------------------------------------------------
const FLOW_KEY = 'aviso_a_mano';
const definition = {
  esquema: 'setter-flujo/v1', flujo: FLOW_KEY, producto: A.funnel, estado: 'borrador', nombre: 'Aviso a mano',
  dispara: { manual: {} },
  para: { es: 'prospecta', formulario_en: '30d' },
  entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ ...A2 }, { esperar: '1m' }, { ...A2 }],
  termina: { si: ['compra', 'contesta'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-aviso-a-mano', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona', otra_despues_de: '7d' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  ritmo: { por_hora: 200, por_ocurrencia: 1000 },
};
const flowId = await register(A.funnel, FLOW_KEY);
await cycle([versionItem(flowId, A.funnel, FLOW_KEY, definition)], `v1 de ${FLOW_KEY}`);
const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
  [flowId]), 'propuesta').proposal_id;
const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
  [flowId, proposalId, A.timezone]), 'abrir').r);
if (!opened.ok || opened.state !== 'cohorte') fail('abrir el flujo manual en el nativo', opened);

const lead = async (label) => {
  const p = person(label);
  await admitForm(p, new Date((await dbNow()).getTime() - 50 * 60_000));
  p.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
    values ($1, $2, $3, 'MX') returning id`, [p.name, p.email, p.phone]), `${label} contacto`).id;
  await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1, 'email', $2, $2, 'system'), ($1, 'phone', $3, $3, 'system')`, [p.contact, p.email, p.phone]);
  await sql('insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, \'dan\')',
    [flowId, p.contact]);
  return p;
};
const ana = await lead('e2e-ana');
const bea = await lead('e2e-bea');
const byContact = new Map([[ana.contact, ana], [bea.contact, bea]]);

const flows = await call('public.read_flow_audience_flows_v1($1::jsonb)', [JSON.stringify(INSTANCE)]);
const running = flows.find((row) => row.flow_id === flowId);
if (!running || running.version !== 1 || running.definition.dispara.manual === undefined) {
  fail('el flujo manual corre para el bridge', flows);
}
const DAN = 'U0DANSCHWAB1';
const inMinutes = async (minutes) => new Date((await dbNow()).getTime() + minutes * 60_000).toISOString();
const session = sha('e2e-session');
await as('flow_change_approver', async () => {
  const state = sha('e2e-state');
  await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha('e2e-nonce'), '/flow-approval/', await inMinutes(10));
  await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
  const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, 'T0LANCEMOS01', DAN,
    await inMinutes(60));
  if (!created.ok) fail('entrar con Slack', created);
});
const occurrenceKey = 'manual:33333333-3333-4333-8333-333333333333';
const materialized = await call(
  'public.materialize_flow_audience_occurrence_v1($1::jsonb, $2::uuid, $3::integer, $4::jsonb, $5::jsonb)',
  [JSON.stringify(INSTANCE), flowId, 1,
    JSON.stringify({ key: occurrenceKey, scheduled_at: (await dbNow()).toISOString(), fingerprint: 'b'.repeat(64),
      materialized_by: `slack:${DAN}` }),
    JSON.stringify([ana, bea].map((p) => ({ contact_id: p.contact, phone: p.phone })))]);
if (!materialized.ok || materialized.state !== 'materializada' || materialized.member_count !== 2) {
  fail('la lista manual espera a una persona', materialized);
}
const OCCURRENCE = materialized.occurrence_id;
const confirmed = await as('flow_change_approver',
  () => rpc('approver_confirm_flow_audience_v1', INSTANCE, session, OCCURRENCE, 2, true));
if (!confirmed.ok) fail('confirmar con el conteo exacto', confirmed);
const released = await call('public.release_flow_audience_members_v1($1::uuid, now(), 50)', [OCCURRENCE]);
if (released.released?.length !== 2 || released.released.some((row) => !byContact.has(row.contact_id))) {
  fail('la base suelta a las dos', released);
}
results.lista = { estado: materialized.state, confirmada: confirmed.ok, sueltas: released.released.length };

// El evento del opener (flow_audience_opener.audience_event), con lo que soltó la base y lo que leyó el bridge.
const eventOf = async (row) => ({
  fuente: 'audiencia', tipo: 'manual', evento_id: `audiencia:${OCCURRENCE}:${row.contact_id}`, flujo_id: flowId,
  version: running.version, ocurrencia_id: OCCURRENCE, producto_ref: running.product_ref,
  contacto_id: row.contact_id, momento: (await dbNow()).toISOString(),
  chatwoot: { account_id: A.accountId, inbox_id: A.inboxId }, destino: row.phone,
});
const admit = async (instance, flowEvent) => as('service_role', async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
  [JSON.stringify(instance), JSON.stringify(flowEvent)])).rows);
const runsOf = async () => sql(`select run.recovery_case_id, run.flow_id, run.version, native.trigger_event
  from public.flow_engine_runs run join public.flow_engine_native_runs native using (recovery_case_id)
  where run.flow_id = $1 order by run.started_at`, [flowId]);

// ---------------------------------------------------------------------------
// 2. Rojo: sin el enganche (sin native_audience) la base rechaza a cada persona.
// ---------------------------------------------------------------------------
{
  let refused = null;
  try {
    await admit(INSTANCE, await eventOf(released.released[0]));
  } catch (error) {
    refused = { code: error.code, message: error.message };
  }
  await db.exec('reset role');
  if (refused?.code !== '22023' || refused.message !== 'invalid_flow_engine_event') {
    fail('rojo: sin la clave del opener la persona no entra', refused);
  }
  if ((await runsOf()).length !== 0) fail('rojo: sin la clave no arranca ningun recorrido');
  results.rojo = refused.message;
}

// ---------------------------------------------------------------------------
// 3. Con el enganche: abrir, asentar y la primera plantilla.
// ---------------------------------------------------------------------------
const WITH_KEY = { ...INSTANCE, native_audience: true };
for (const row of released.released) {
  const p = byContact.get(row.contact_id);
  const rows = await admit(WITH_KEY, await eventOf(row));
  const mine = rows.filter((r) => r.flow_id === flowId);
  if (mine.length !== 1 || mine[0].outcome !== 'motor_arranco' || mine[0].created !== true) {
    fail(`${p.label}: el motor arranca`, rows);
  }
  p.caseId = mine[0].recovery_case_id;
  p.first = mine[0].scheduled_action_id;
  const settled = await call('public.settle_flow_audience_member_v1($1::uuid, $2::uuid, $3)',
    [OCCURRENCE, row.contact_id, 'arranco']);
  if (!settled.settled) fail(`${p.label}: asentar`, settled);
}
const runs = await runsOf();
if (runs.length !== 2 || runs.some((run) => run.version !== 1 || run.trigger_event !== 'audiencia')) {
  fail('dos recorridos nativos de audiencia en la version 1', runs);
}
for (const p of [ana, bea]) {
  const kase = one(await sql('select context, policy_key, policy_version from public.recovery_cases where id = $1',
    [p.caseId]), `${p.label} caso`);
  if (kase.context.flow_audience_occurrence_id !== OCCURRENCE || kase.context.flow_audience_form !== 'manual'
      || kase.policy_key !== `flujo:${flowId}` || kase.policy_version !== 1) fail(`${p.label}: el caso`, kase);
}
const finished = one(await sql('select state from public.flow_audience_occurrences where id = $1', [OCCURRENCE]),
  'ocurrencia').state;
if (finished !== 'terminada') fail('la ocurrencia termina con las dos asentadas', finished);
const firsts = [];
for (const p of [ana, bea]) {
  const reserved = await reserve(p, p.first);
  const mark = await startNew(reserved);
  if (mark.outcome !== 'request_started') fail(`${p.label}: la primera plantilla sale`, mark);
  await accept(p, reserved);
  const second = one(await sql(`select status from public.scheduled_actions
    where recovery_case_id = $1 and step_key = 'native_touch_2'`, [p.caseId]), `${p.label} segundo toque`);
  if (second.status !== 'pending') fail(`${p.label}: el segundo toque queda agendado`, second);
  firsts.push(mark.outcome);
}
results.con_enganche = { recorridos: runs.length, version: 1, ocurrencia: finished, primera: firsts };

console.log(`validate_flow_audience_native_e2e: ok ${JSON.stringify(results)}`);
await db.close();
