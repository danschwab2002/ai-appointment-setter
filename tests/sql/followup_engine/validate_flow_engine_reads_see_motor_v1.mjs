// Las tres lecturas que cuentan tambien lo que autoriza el motor (migracion
// 20261022000300): la historia de la simulacion (read_flow_engine_history_v1)
// y las del AE (read_automation_expert_flows_v1 y
// read_automation_expert_lead_history_v1), sobre la cadena completa de
// migraciones con el mundo de la T2.3b (validate_flow_engine_motor_v1.mjs).
//
// Contratos: docs/contracts/flow-engine-v1.md, seccion 2, y
// docs/contracts/automation-expert-read-v1.md.
//
// Prueba:
//   0. la migracion: las tres funciones conservan la firma, el duenio, los
//      permisos (solo service_role las ejecuta) y llevan la marca; correrla
//      otra vez aborta;
//   1. sin filas del motor (una autorizacion del legado), las tres dan lo
//      mismo que la version de antes;
//   2. con una autorizacion del legado y una del motor, las tres cuentan las
//      dos: la historia (lista, conteo y contacto), /flows (starts_today,
//      starts_total y, con las dos movidas a ayer, observed.starts del
//      carrito) y la historia del lead del motor (el momento start_authorized
//      con su evidencia);
//   3. el rojo: con la version de antes, la del motor no aparece en ninguna;
//   4. la historia del lead deduce los topes con las dos tablas: con el tope
//      del scope en 2, un arranque reservado sin autorizar se explica como
//      tope total agotado, y el A2 que espera, tambien;
//   5. con filas sinteticas del motor (en una transaccion que se deshace): un
//      intento autorizado por el motor no es un rechazo, un contacto solo del
//      motor entra en la historia, y otra bolsa u otro dia no cuentan.
//
// La version de antes no se copia: se arma deshaciendo, sobre la definicion
// vigente, los reemplazos que lee de la migracion (cada ancla aparecia una
// sola vez, lo verifica la migracion), adentro de una transaccion que se
// deshace.
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

import { buildWorld, expectError, fail, one, root } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261022000300_flow_engine_reads_see_motor_v1.sql';
const MARKER = 'flow_engine_reads_see_motor_v1';
const HISTORY = 'public.read_flow_engine_history_v1(jsonb,text,timestamp with time zone)';
const FLOWS = 'public.read_automation_expert_flows_v1(jsonb)';
const LEAD = 'public.read_automation_expert_lead_history_v1(jsonb,uuid)';
const MOTOR_TABLE = 'flow_engine_request_authorizations';
// Con --emit-fixture [--check]: la fila del motor en la historia y su momento
// en la historia del lead, como los da la cadena real (uuid y horas fijos por
// orden de aparicion). La lee tests/test_automation_expert_read.py.
const FIXTURE = join(root, 'tests/fixtures/flow_engine/motor_authorization_reads.json');
const normalizer = () => {
  const ids = new Map();
  const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g;
  const fixedId = (id) => {
    if (!ids.has(id)) ids.set(id, `00000000-0000-4000-8000-${String(ids.size + 1).padStart(12, '0')}`);
    return ids.get(id);
  };
  const value = (raw) => {
    if (typeof raw === 'string') {
      return raw.replace(UUID, fixedId)
        .replace(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d(:\d\d)?)$/, '2026-10-22T15:00:00+00:00')
        .replace(/^\d{4}-\d\d-\d\d$/, '2026-10-22');
    }
    if (Array.isArray(raw)) return raw.map(value);
    if (raw !== null && typeof raw === 'object') {
      return Object.fromEntries(Object.entries(raw).map(([key, item]) => [key, value(item)]));
    }
    return raw;
  };
  return value;
};
let emitted = null;

// Los (firma, ancla, reemplazo) de la migracion, en su orden.
const pairs = (() => {
  const text = readFileSync(join(root, 'supabase/migrations', MIGRATION), 'utf8');
  const re = /v_signatures := v_signatures \|\| '([^']+)'::text;\s*v_anchors := v_anchors \|\| \$a\$([\s\S]*?)\$a\$::text;\s*v_replacements := v_replacements \|\| \$r\$([\s\S]*?)\$r\$::text;/g;
  const found = [...text.matchAll(re)].map(([, signature, anchor, replacement]) => ({ signature, anchor, replacement }));
  if (found.length !== 9) fail(`se esperaban 9 reemplazos en ${MIGRATION}`, found.length);
  return found;
})();

const world = await buildWorld();
const { db, A, SCOPE, FLOWS: FLOW_IDS, INSTANCE, cartFor, planCart, admit, reserve, startOld, startNew, accept,
  createRoute, route, dbNow } = world;

const AE_INSTANCE = {
  tenant_ref: A.tenant, ally_ref: A.ally, funnel_ref: A.funnel,
  chatwoot_account_id: A.accountId, chatwoot_inbox_id: A.inboxId,
  hotmart_product_id: String(A.productId), inbound_scope_key: A.inboundScope,
  agent_scope_ref: 'att1-agent-bot', time_zone: A.timezone,
  scopes: [{ flow_group: 'recuperacion', key: SCOPE, version: 1 }],
};

const definitionOf = async (signature) => one((await db.query(
  'select pg_get_functiondef($1::regprocedure) as d', [signature])).rows, signature).d;

// La version de antes de las tres, en la transaccion abierta.
const revert = async () => {
  for (const signature of [HISTORY, FLOWS, LEAD]) {
    let definition = await definitionOf(signature);
    for (const pair of pairs.filter((item) => item.signature === signature)) {
      if (definition.split(pair.replacement).length !== 2) fail(`el reemplazo no esta una vez en ${signature}`);
      definition = definition.split(pair.replacement).join(pair.anchor);
    }
    if (definition.includes(MARKER)) fail(`la version de antes de ${signature} conserva la marca`);
    await db.exec(definition);
  }
};

// Las tres lecturas, como las llama el bridge (service_role). La historia con
// un p_since fijo, para que las dos versiones lean la misma ventana.
const since = new Date((await dbNow()).getTime() - 2 * 86_400_000).toISOString();
const readAll = async (leadRef) => {
  await db.exec('set local role service_role');
  const history = one((await db.query(`select public.read_flow_engine_history_v1($1::jsonb, $2, $3::timestamptz) as r`,
    [JSON.stringify(INSTANCE), A.funnel, since])).rows, 'historia').r;
  const flows = one((await db.query('select public.read_automation_expert_flows_v1($1::jsonb) as r',
    [JSON.stringify(AE_INSTANCE)])).rows, 'flows').r;
  const lead = leadRef ? one((await db.query('select public.read_automation_expert_lead_history_v1($1::jsonb, $2::uuid) as r',
    [JSON.stringify(AE_INSTANCE), leadRef])).rows, 'historia del lead').r : null;
  await db.exec('reset role');
  return { history, flows, lead };
};

// Las dos versiones sobre los mismos datos: la vigente y la de antes. Si
// before corre algo (mover las fechas), corre adentro y se deshace.
const bothVersions = async (leadRef, before = null) => {
  await db.exec('begin');
  try {
    if (before) await before();
    const current = await readAll(leadRef);
    await revert();
    const previous = await readAll(leadRef);
    return { current, previous };
  } finally {
    await db.exec('rollback');
  }
};

// Lo que cambia entre dos llamadas sin que cambien los datos: la sal de la
// historia (las claves de persona), la hora de la captura y la de la base.
const stable = (value) => {
  if (typeof value === 'string') return /^[0-9a-f]{16}$/.test(value) ? 'K' : value;
  if (Array.isArray(value)) return value.map(stable);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value)
      .filter(([key]) => !['captured_at', 'database_at'].includes(key))
      .map(([key, item]) => [key, stable(item)]));
  }
  return value;
};
const same = (left, right) => JSON.stringify(stable(left)) === JSON.stringify(stable(right));

const count = async (table) => Number(one((await db.query(`select count(*)::int as n from public.${table}`)).rows, table).n);
const results = {};

// ---------------------------------------------------------------------------
// 0. La migracion.
// ---------------------------------------------------------------------------
for (const signature of [HISTORY, FLOWS, LEAD]) {
  const row = one((await db.query(`
    select has_function_privilege('service_role', p.oid, 'execute') as service_x,
           has_function_privilege('anon', p.oid, 'execute') as anon_x,
           has_function_privilege('authenticated', p.oid, 'execute') as auth_x,
           has_function_privilege('public', p.oid, 'execute') as public_x,
           p.prosecdef as definer, coalesce(array_to_string(p.proconfig, ','), '') as config
      from pg_proc p where p.oid = to_regprocedure($1)`, [signature])).rows, signature);
  if (!row.service_x || row.anon_x || row.auth_x || row.public_x || !row.definer
      || !row.config.startsWith('search_path=')) {
    fail(`permisos de ${signature}`, row);
  }
  const definition = await definitionOf(signature);
  const expected = pairs.filter((item) => item.signature === signature);
  if (!expected.every((item) => definition.includes(item.replacement))) fail(`${signature} sin los reemplazos`);
}
await expectError(db, 'correr la migracion dos veces', () => db.exec(
  readFileSync(join(root, 'supabase/migrations', MIGRATION), 'utf8')),
{ code: '55000', message: 'unexpected_flow_engine_reads_definition' });

// ---------------------------------------------------------------------------
// 1. Sin filas del motor: lo mismo que antes.
// ---------------------------------------------------------------------------
if (!(await createRoute(A.funnel, 'carrito', 'carrito', SCOPE)).ok) fail('crear la ruta');
const legacyLead = await cartFor('lectura-legado');
await planCart(legacyLead);
{
  const reserved = await reserve(legacyLead, legacyLead.plan.scheduled_action_id);
  const started = await startOld(reserved);
  if (started.phase !== 'request_started') fail('A1 del legado', started);
  await accept(legacyLead, reserved);
}
if (await count('pilot_outbound_request_authorizations') !== 1 || await count(MOTOR_TABLE) !== 0) {
  fail('se esperaba una autorizacion del legado y ninguna del motor');
}
const legacyContact = one((await db.query('select contact_id from public.recovery_cases where id = $1',
  [legacyLead.plan.recovery_case_id])).rows, 'contacto del legado').contact_id;
{
  const { current, previous } = await bothVersions(legacyContact);
  for (const name of ['history', 'flows', 'lead']) {
    if (!same(current[name], previous[name])) {
      fail(`sin filas del motor, ${name} cambio`, { current: current[name], previous: previous[name] });
    }
  }
  if (current.history.autorizaciones.length !== 1 || current.flows.scopes[0].starts_total !== 1) {
    fail('la del legado no se cuenta', { history: current.history.autorizaciones, scopes: current.flows.scopes });
  }
  results.sin_motor = 'igual';
}

// ---------------------------------------------------------------------------
// 2. Una del legado y una del motor.
// ---------------------------------------------------------------------------
if (!(await route(A.funnel, 'carrito', FLOW_IDS.carrito, 'motor')).ok) fail('el carrito a motor');
const motorLead = await cartFor('lectura-motor');
const admitted = await admit(motorLead);
if (admitted.outcome !== 'motor_arranco') fail('la admision del motor', admitted);
{
  const reserved = await reserve(motorLead, motorLead.plan.scheduled_action_id);
  const started = await startNew(reserved);
  if (started.outcome !== 'request_started') fail('A1 del motor', started);
  await accept(motorLead, reserved);
}
if (await count('pilot_outbound_request_authorizations') !== 1 || await count(MOTOR_TABLE) !== 1) {
  fail('se esperaba una autorizacion de cada lado');
}
const engineRow = one((await db.query(`select * from public.${MOTOR_TABLE}`)).rows, 'la del motor');
if (engineRow.pool !== SCOPE) fail('la bolsa del motor no es el scope del legado', engineRow.pool);
const motorContact = engineRow.contact_id;

{
  const { current, previous } = await bothVersions(motorContact);
  // La historia: la lista, el conteo y el contacto.
  const motorRows = (h) => h.autorizaciones.filter((row) => row.action_id === engineRow.action_id);
  if (current.history.autorizaciones.length !== 2 || current.history._captured.conteos.autorizaciones !== 2) {
    fail('la historia no cuenta las dos', current.history._captured.conteos);
  }
  const [shown] = motorRows(current.history);
  if (!shown || shown.scope_key !== SCOPE || shown.scope_version !== null || shown.runtime_generation !== null
      || shown.contact_id !== motorContact || !shown.local_budget_date || !shown.authorized_at) {
    fail('la del motor en la historia', shown);
  }
  if (!current.history.contactos.some((row) => row.id === motorContact)) fail('el contacto del motor en la historia');
  if (motorRows(previous.history).length !== 0 || previous.history._captured.conteos.autorizaciones !== 1) {
    fail('el rojo: la historia de antes ya veia la del motor');
  }
  // /flows: el consumo del dia y el total.
  const [scopeNow] = current.flows.scopes;
  const [scopeBefore] = previous.flows.scopes;
  if (scopeNow.starts_today !== 2 || scopeNow.starts_total !== 2) fail('/flows no cuenta las dos', scopeNow);
  if (scopeBefore.starts_today !== 1 || scopeBefore.starts_total !== 1) fail('el rojo: /flows de antes', scopeBefore);
  // La historia del lead del motor: el momento autorizado, con su evidencia.
  const authorizedMoments = (lead) => lead.moments.filter((moment) => moment.kind === 'start_authorized');
  const engineMoments = authorizedMoments(current.lead).filter((moment) => moment.evidence?.table === MOTOR_TABLE);
  if (engineMoments.length !== 1 || engineMoments[0].evidence.id !== engineRow.id
      || engineMoments[0].reason_code !== 'flow_engine_request_start_authorized'
      || engineMoments[0].flow_key !== 'carrito'
      || engineMoments[0].extra?.local_budget_date !== shown.local_budget_date) {
    fail('el momento del motor en la historia del lead', authorizedMoments(current.lead));
  }
  if (authorizedMoments(previous.lead).some((moment) => moment.evidence?.table === MOTOR_TABLE)
      || authorizedMoments(previous.lead).length !== authorizedMoments(current.lead).length - 1) {
    fail('el rojo: la historia del lead de antes', authorizedMoments(previous.lead));
  }
  const norm = normalizer();
  emitted = {
    _origen: 'tests/sql/followup_engine/validate_flow_engine_reads_see_motor_v1.mjs --emit-fixture: las RPC reales '
      + 'sobre la cadena de migraciones (PGlite) con el mundo de la T2.3b; uuid, horas y fechas normalizados. '
      + 'En ATT1 no habia todavia ninguna fila del motor (22/10 23:21Z, att1-db en solo lectura)',
    historia_autorizacion_del_motor: norm(shown),
    momento_del_motor: norm(engineMoments[0]),
  };
  results.con_motor = {
    historia: current.history._captured.conteos.autorizaciones,
    flows_hoy: scopeNow.starts_today,
    flows_total: scopeNow.starts_total,
    momentos_del_motor: engineMoments.length,
  };
}

// observed.starts cuenta los 7 dias completos sin el de hoy: las dos se
// mueven a ayer, adentro de la transaccion que se deshace.
{
  const yesterday = async () => {
    for (const table of ['pilot_outbound_request_authorizations', MOTOR_TABLE]) {
      await db.exec(`alter table public.${table} disable trigger user`);
      await db.exec(`update public.${table} set authorized_at = authorized_at - interval '1 day'`);
      await db.exec(`alter table public.${table} enable trigger user`);
    }
  };
  const { current, previous } = await bothVersions(null, yesterday);
  const startsNow = current.flows.observed.flows.carrito.starts;
  const startsBefore = previous.flows.observed.flows.carrito.starts;
  if (startsNow !== 2) fail('observed.starts no cuenta las dos', current.flows.observed.flows.carrito);
  if (startsBefore !== 1) fail('el rojo: observed.starts de antes', previous.flows.observed.flows.carrito);
  results.con_motor.flows_observado = startsNow;
}

// La historia del lead deduce los topes con las dos tablas: el tope del
// scope en 2 (adentro de la transaccion que se deshace) se agota con una del
// legado y una del motor. Un arranque que quedo reservado sin autorizar (un
// tercer carrito del motor) se explica como tope total agotado, y el paso que
// espera del legado (su A2) se espera vencido por el mismo tope.
{
  const third = await cartFor('lectura-reservado');
  if ((await admit(third)).outcome !== 'motor_arranco') fail('la admision del tercero');
  await reserve(third, third.plan.scheduled_action_id);
  const thirdContact = one((await db.query('select contact_id from public.recovery_cases where id = $1',
    [third.plan.recovery_case_id])).rows, 'contacto del tercero').contact_id;
  const capAtTwo = async () => {
    await db.exec('alter table public.pilot_scope_versions disable trigger user');
    await db.query(`update public.pilot_scope_versions
                       set max_outbound_request_starts_total = 2, max_outbound_request_starts_per_day = 2
                     where scope_key = $1`, [SCOPE]);
    await db.exec('alter table public.pilot_scope_versions enable trigger user');
  };
  const rejectedReason = (lead) => lead.moments.filter((moment) => moment.kind === 'start_rejected')
    .map((moment) => moment.reason_code);
  const rejected = await bothVersions(thirdContact, capAtTwo);
  if (JSON.stringify(rejectedReason(rejected.current.lead)) !== '["pilot_total_budget_exhausted"]') {
    fail('el rechazo deducido no cuenta la del motor', rejectedReason(rejected.current.lead));
  }
  if (rejectedReason(rejected.previous.lead).includes('pilot_total_budget_exhausted')) {
    fail('el rojo: el rechazo deducido de antes ya contaba la del motor', rejectedReason(rejected.previous.lead));
  }
  const expectedOf = (lead) => lead.upcoming.filter((step) => step.step_key === 'cart_second_touch')
    .map((step) => step.expected.reason_code);
  const waiting = await bothVersions(legacyContact, capAtTwo);
  if (JSON.stringify(expectedOf(waiting.current.lead)) !== '["pilot_total_budget_exhausted"]') {
    fail('el paso que espera no cuenta la del motor', expectedOf(waiting.current.lead));
  }
  if (expectedOf(waiting.previous.lead).includes('pilot_total_budget_exhausted')
      || expectedOf(waiting.previous.lead).length !== 1) {
    fail('el rojo: el paso que espera de antes', expectedOf(waiting.previous.lead));
  }
  results.con_motor.tope_deducido = { rechazo: rejectedReason(rejected.current.lead)[0],
    espera: expectedOf(waiting.current.lead)[0] };

  // Filas sinteticas del motor, adentro de la transaccion que se deshace (la
  // tabla no tiene FK a la accion ni al contacto, y es solo de insercion):
  //   * una con el intento reservado del tercero: ese intento deja de ser un
  //     arranque rechazado;
  //   * una de un contacto que no aparece por ningun otro lado: entra en los
  //     contactos de la historia;
  //   * una de otra bolsa y otra de ayer: no cuentan en la bolsa ni en el dia.
  const attemptOfThird = one((await db.query(`select attempt.id from public.followup_delivery_attempts attempt
      where attempt.action_id = $1 and attempt.phase = 'reserved'`, [third.plan.scheduled_action_id])).rows,
  'el intento reservado del tercero').id;
  const loneContact = '00000000-0000-4000-8000-00000000c0de';
  const synthetic = async () => {
    await db.query("insert into public.contacts (id, lifecycle_status) values ($1, 'lead')", [loneContact]);
    const insert = (pool, actionId, attemptId, contactId, day) => db.query(`
      insert into public.flow_engine_request_authorizations
        (flow_id, version, pool, legacy_scope_key, action_id, attempt_id, contact_id, local_budget_date,
         flow_generation, authorized_at)
      values ($1, $2, $3, $3, $4, $5, $6, $7::date, $8, clock_timestamp())`,
    [engineRow.flow_id, engineRow.version, pool, actionId, attemptId, contactId, day, engineRow.flow_generation]);
    const today = engineRow.local_budget_date instanceof Date
      ? engineRow.local_budget_date.toISOString().slice(0, 10) : String(engineRow.local_budget_date).slice(0, 10);
    const yesterday = new Date(Date.parse(`${today}T12:00:00Z`) - 86_400_000).toISOString().slice(0, 10);
    await insert(SCOPE, third.plan.scheduled_action_id, attemptOfThird, thirdContact, today);
    await insert(SCOPE, '00000000-0000-4000-8000-00000000a001', '00000000-0000-4000-8000-00000000b001',
      loneContact, yesterday);
    await insert('otra-bolsa', '00000000-0000-4000-8000-00000000a002', '00000000-0000-4000-8000-00000000b002',
      loneContact, today);
  };
  const seen = await bothVersions(thirdContact, synthetic);
  // Rechazo: el intento autorizado por el motor ya no es un rechazo.
  if (rejectedReason(seen.current.lead).length !== 0) fail('un intento autorizado por el motor sale rechazado',
    rejectedReason(seen.current.lead));
  if (rejectedReason(seen.previous.lead).length !== 1) fail('el rojo: el rechazo de antes', rejectedReason(seen.previous.lead));
  // Contactos: el contacto solo del motor entra (por su bolsa, no por la otra).
  const hasLone = (h) => h.contactos.some((row) => row.id === loneContact);
  if (!hasLone(seen.current.history) || hasLone(seen.previous.history)) fail('el contacto solo del motor');
  // Bolsa y dia: 1 legado + 1 motor + 2 sinteticas de la bolsa (una hoy, una ayer); la de otra bolsa no.
  const [scopeSeen] = seen.current.flows.scopes;
  if (scopeSeen.starts_total !== 4 || scopeSeen.starts_today !== 3) fail('los filtros de pool y dia de /flows', scopeSeen);
  if (seen.current.history._captured.conteos.autorizaciones !== 4
      || seen.current.history.autorizaciones.some((row) => row.scope_key === 'otra-bolsa')) {
    fail('el filtro de pool de la historia', seen.current.history._captured.conteos);
  }
  results.con_motor.sinteticas = { starts_total: scopeSeen.starts_total, starts_today: scopeSeen.starts_today };
}

if (process.argv.includes('--emit-fixture')) {
  const text = `${JSON.stringify(emitted, null, 2)}\n`;
  if (process.argv.includes('--check')) {
    if (readFileSync(FIXTURE, 'utf8') !== text) {
      fail('motor_authorization_reads.json no es lo que da la cadena: correr con --emit-fixture sin --check');
    }
    results.fixture = 'igual a la cadena';
  } else {
    writeFileSync(FIXTURE, text);
    results.fixture = 'escrito';
  }
}
console.log(JSON.stringify(results));
console.log('FLOW_ENGINE_READS_SEE_MOTOR=OK');
