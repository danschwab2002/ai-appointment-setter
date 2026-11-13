// Lo que contesto cada conversacion a la pregunta de entrada de una campana
// (migracion 20261103001500, E-317; contrato docs/contracts/campaign-inbound-entry-v1.md;
// D4 del diseno E-319), contra la cadena completa de migraciones.
//
// Prueba:
//   0. que la migracion agrega exactamente una tabla y dos funciones, no cambia
//      ninguna otra definicion ni ACL, que las dos son solo de service_role, que
//      la tabla no tiene permisos para ningun rol de la API y que correrla dos
//      veces aborta con segment_entry_answers_already_applied;
//   1. registrar un "no" y un "si" (recorded), y que una segunda respuesta de la
//      misma conversacion y campana devuelve replayed con la primera y no la pisa;
//   2. la lectura por conversacion: solo la de esa cuenta, inbox y conversacion;
//   3. una entrada invalida da 22023 (respuesta fuera de si/no, clave que no es
//      un slug, ids no positivos);
//   4. anon no ejecuta ninguna de las dos.
//
// Datos sinteticos: la cuenta 2 / inbox 11 de ATT1 y la clave de ejemplo
// black-november-2026. Ningun dato de una persona.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION = '20261103001500_segment_entry_answers_v1.sql';
const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
  alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
`);

const fail = (message, detail) => {
  throw new Error(detail === undefined ? message : `${message}: ${JSON.stringify(detail)}`);
};
const results = {};

// ---------------------------------------------------------------------------
// 0. La migracion agrega una tabla y dos funciones, y nada mas.
// ---------------------------------------------------------------------------
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`${MIGRATION} no esta en la cadena`);
// El origen de un recorrido nativo para la campana por landing (20261103001700, E-321): una lectura de service_role.
// El cierre del caso por el cupon de una campana (20261106000100, E-339): una RPC de service_role.
// La oferta de la campana en el link del agente (20261110000500, E-410).
// Los numeros del tablero (20261112200000, E-507): una lectura nueva.
// prioridad.le_gana_a en el nativo (20261112210200, E-483, C7).
// El pago rechazado completo en el nativo (20261112210300, E-483, C3).
// El pago rechazado sin formulario de una oferta de campana (20261112210400, E-483, pieza PF).
// El cupon propio por persona en el nativo (20261112210500, E-483, C2).
// El evento de un disparo por audiencia en el nativo (20261112210600, E-483, para E-484).
if (names[names.length - 1] !== '20261112230000_e483_native_person_cap_v1.sql' || names[names.length - 2] !== '20261112220000_flow_audience_triggers_v1.sql' || names[names.length - 3] !== '20261112210600_e483_native_audience_source_v1.sql' || names[names.length - 4] !== '20261112210500_e483_native_personal_coupon_v1.sql' || names[names.length - 5] !== '20261112210400_e483_payment_failure_by_phone_v1.sql' || names[names.length - 6] !== '20261112210300_e483_native_condition_after_send_v1.sql' || names[names.length - 7] !== '20261112210200_e483_native_priority_wins_v1.sql' || names[names.length - 8] !== '20261112210100_e483_native_cancel_in_flight_v1.sql' || names[names.length - 9] !== '20261112210000_e483_native_multitouch_v1.sql' || names[names.length - 10] !== '20261112200000_read_results_v1.sql' || names[names.length - 11] !== '20261111000200_pending_cancel_keeps_first_contact_v1.sql' || names[names.length - 12] !== '20261111000100_close_recovery_case_without_pending_touch_v1.sql' || names[names.length - 13] !== '20261110000500_campaign_offer_checkout_issuance_v1.sql' || names[names.length - 14] !== '20261109000100_meta_template_request_notices_v1.sql' || names[names.length - 15] !== '20261108005000_agent_campaigns_v1.sql' || position !== names.length - 21 || names[names.length - 20] !== '20261103001700_flow_engine_native_campaign_origin_v1.sql' || names[names.length - 19] !== '20261106000100_recovery_case_segment_coupon_close_v1.sql' || names[names.length - 18] !== '20261107000100_meta_account_hold_retry_v1.sql' || names[names.length - 17] !== '20261108000100_flow_store_route_executor_v1.sql' || names[names.length - 16] !== '20261108004100_flow_change_publish_native_version_guard_v1.sql') fail(`${MIGRATION} tiene que ser la ultima`, names.slice(position + 1));
const apply = async (file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
));
const snapshot = async () => new Map((await db.query(`
  select p.oid::regprocedure::text as signature,
         pg_get_functiondef(p.oid) as definition,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const tables = async () => (await db.query(`
  select c.relname from pg_class c
  where c.relnamespace = 'public'::regnamespace and c.relkind = 'r'
  order by 1
`)).rows.map((row) => row.relname);

await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of names.slice(0, position)) {
  await apply(join(root, 'supabase/migrations', name));
}
const before = await snapshot();
const tablesBefore = await tables();
await apply(join(root, 'supabase/migrations', MIGRATION));
const after = await snapshot();
const tablesAfter = await tables();

const RECORD = 'record_segment_entry_answer_v1(bigint,bigint,bigint,text,text,timestamp with time zone)';
const READ = 'get_segment_entry_answers_v1(bigint,bigint,bigint)';
const added = [...after.keys()].filter((signature) => !before.has(signature)).sort();
const removed = [...before.keys()].filter((signature) => !after.has(signature));
const changed = [...before.keys()].filter((signature) => after.has(signature)
  && (after.get(signature).definition !== before.get(signature).definition
      || after.get(signature).service_x !== before.get(signature).service_x
      || after.get(signature).api_x !== before.get(signature).api_x));
if (JSON.stringify(added) !== JSON.stringify([READ, RECORD].sort()) || removed.length || changed.length) {
  fail('the migration changed something else', { added, removed, changed });
}
const newTables = tablesAfter.filter((name) => !tablesBefore.includes(name));
if (JSON.stringify(newTables) !== JSON.stringify(['segment_entry_answers'])) fail('tables', newTables);
for (const signature of [RECORD, READ]) {
  const row = after.get(signature);
  if (row.service_x !== true || row.api_x !== false) fail(`${signature} is not service_role only`, row);
}
const tableAcl = (await db.query(`
  select r.rolname, has_table_privilege(r.rolname, 'public.segment_entry_answers', 'select') as can_select,
         has_table_privilege(r.rolname, 'public.segment_entry_answers', 'insert') as can_insert
  from pg_roles r where r.rolname in ('anon', 'authenticated', 'service_role') order by 1
`)).rows;
if (tableAcl.some((row) => row.can_select || row.can_insert)) fail('the table has API grants', tableAcl);
const rls = (await db.query(`select relrowsecurity from pg_class where oid = 'public.segment_entry_answers'::regclass`)).rows[0];
if (rls.relrowsecurity !== true) fail('row level security is off');
try {
  await apply(join(root, 'supabase/migrations', MIGRATION));
  fail('running the migration twice passed');
} catch (error) {
  if (!String(error.message).includes('segment_entry_answers_already_applied')) throw error;
  await db.exec('rollback;');
}
if ((await snapshot()).size !== after.size) fail('the second run left something');
results.migration = { functions: 2, tables: 1, changed: 0, twice: 'segment_entry_answers_already_applied' };

// ---------------------------------------------------------------------------
// 1 y 2. Registrar y leer.
// ---------------------------------------------------------------------------
await db.query('set role service_role');
const KEY = 'black-november-2026';
const record = async (account, inbox, conversation, key, answer) => (await db.query(
  `select * from public.record_segment_entry_answer_v1($1, $2, $3, $4, $5, '2026-11-20T15:00:00Z'::timestamptz)`,
  [account, inbox, conversation, key, answer],
)).rows;
const read = async (account, inbox, conversation) => (await db.query(
  `select * from public.get_segment_entry_answers_v1($1, $2, $3)`,
  [account, inbox, conversation],
)).rows;

const no = await record(2, 11, 701, KEY, 'no');
if (JSON.stringify(no) !== JSON.stringify([{ outcome: 'recorded', answer: 'no' }])) fail('record no', no);
const si = await record(2, 11, 702, KEY, 'si');
if (JSON.stringify(si) !== JSON.stringify([{ outcome: 'recorded', answer: 'si' }])) fail('record si', si);
// La primera respuesta queda.
const again = await record(2, 11, 701, KEY, 'si');
if (JSON.stringify(again) !== JSON.stringify([{ outcome: 'replayed', answer: 'no' }])) fail('replay', again);
// Otra campana de la misma conversacion es otra fila.
await record(2, 11, 701, 'lanzamiento-2027', 'si');
const conv701 = await read(2, 11, 701);
if (JSON.stringify(conv701) !== JSON.stringify([
  { segment_key: KEY, answer: 'no' },
  { segment_key: 'lanzamiento-2027', answer: 'si' },
])) fail('read 701', conv701);
// Otra cuenta, otro inbox u otra conversacion no ven nada.
for (const args of [[3, 11, 701], [2, 13, 701], [2, 11, 999]]) {
  const rows = await read(...args);
  if (rows.length) fail('scope', { args, rows });
}
results.answers = { no: no[0].outcome, si: si[0].outcome, again: again[0].outcome, conv701: conv701.length };

// ---------------------------------------------------------------------------
// 3. Entradas invalidas.
// ---------------------------------------------------------------------------
for (const args of [
  [2, 11, 703, KEY, 'tal vez'],
  [2, 11, 703, 'Black November', 'si'],
  [0, 11, 703, KEY, 'si'],
  [2, 11, 0, KEY, 'no'],
]) {
  try {
    await record(...args);
    fail('invalid answer passed', args);
  } catch (error) {
    if (!String(error.message).includes('invalid_segment_entry_answer')) throw error;
  }
}
try {
  await read(2, 0, 701);
  fail('invalid query passed');
} catch (error) {
  if (!String(error.message).includes('invalid_segment_entry_answers_query')) throw error;
}
results.invalid_input = '22023';

// ---------------------------------------------------------------------------
// 4. anon no ejecuta ninguna.
// ---------------------------------------------------------------------------
await db.query('reset role');
await db.query('set role anon');
for (const sql of [
  `select * from public.get_segment_entry_answers_v1(2, 11, 701)`,
  `select * from public.record_segment_entry_answer_v1(2, 11, 704, '${KEY}', 'no')`,
  `select * from public.segment_entry_answers`,
]) {
  try {
    await db.query(sql);
    fail('anon can reach the answers', sql);
  } catch (error) {
    if (!/permission denied/i.test(String(error.message))) throw error;
  }
}
await db.query('reset role');

console.log(`segment_entry_answers=OK ${JSON.stringify(results)}`);
