// Las derivaciones sin atender de una bandeja (migracion 20261103001000, E-309,
// P-20; contrato docs/contracts/handoff-wait-alerts-v1.md), contra la cadena
// completa de migraciones.
//
// Prueba:
//   0. que la migracion agrega exactamente una funcion, no cambia ninguna otra
//      definicion ni ACL, que es solo de service_role y que correrla dos veces
//      aborta con handoff_wait_alerts_already_applied;
//   1. la forma de Maribel medida el 2026-11-02 (E-308, esperas.sh): cuatro
//      derivaciones en la cuenta 3 / bandeja 13, una atendida (no sale) y tres
//      sin respuesta (#8, #39, #46), cada una con su motivo fino;
//   2. una conversacion derivada dos veces sin respuesta sale una vez, con la
//      derivacion mas nueva;
//   3. lo que no entra: otra cuenta o bandeja (ATT1, cuenta 2 / bandeja 11),
//      una derivacion en dead_letter, una anterior a p_since; una en
//      'requested' (todavia sin proyectar) si entra;
//   4. el limite, el orden (de la mas vieja a la mas nueva) y que una entrada
//      invalida da 22023; y que la fila no lleva la nota privada.
//
// Datos: las filas se siembran con session_replication_role = replica, como en
// validate_automation_expert_read.mjs, para no armar la cadena de claves
// ajenas (conversacion, caso); sus CHECK siguen valiendo. Los numeros de
// conversacion y los motivos son los de E-308; ids, horas y notas, sinteticos.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION = '20261103001000_handoff_wait_alerts_v1.sql';
const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
`);

const fail = (message, detail) => {
  throw new Error(detail === undefined ? message : `${message}: ${JSON.stringify(detail)}`);
};
const results = {};

// ---------------------------------------------------------------------------
// 0. La migracion agrega una funcion y nada mas.
// ---------------------------------------------------------------------------
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`${MIGRATION} no esta en la cadena`);
// Despues va solo la etiqueta de un segmento en un flujo nativo (20261103001300, E-316): un reemplazo
// contado en la forma nativa, que no toca las derivaciones. Esta prueba aplica la cadena hasta la suya.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261103001300_flow_engine_native_segment_label_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de la etiqueta de un segmento`, names.slice(position + 1));
}
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

await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of names.slice(0, position)) {
  await apply(join(root, 'supabase/migrations', name));
}
const before = await snapshot();
await apply(join(root, 'supabase/migrations', MIGRATION));
const after = await snapshot();

const ENTRYPOINT = 'list_unattended_human_handoffs_v1(bigint,bigint,timestamp with time zone,integer)';
const added = [...after.keys()].filter((signature) => !before.has(signature));
const removed = [...before.keys()].filter((signature) => !after.has(signature));
const changed = [...before.keys()].filter((signature) => after.has(signature)
  && (after.get(signature).definition !== before.get(signature).definition
      || after.get(signature).service_x !== before.get(signature).service_x
      || after.get(signature).api_x !== before.get(signature).api_x));
if (JSON.stringify(added) !== JSON.stringify([ENTRYPOINT]) || removed.length || changed.length) {
  fail('the migration changed something else', { added, removed, changed });
}
{
  const row = after.get(ENTRYPOINT);
  if (row.service_x !== true || row.api_x !== false) fail(`${ENTRYPOINT} is not service_role only`, row);
}
try {
  await apply(join(root, 'supabase/migrations', MIGRATION));
  fail('running the migration twice passed');
} catch (error) {
  if (!String(error.message).includes('handoff_wait_alerts_already_applied')) throw error;
  await db.exec('rollback;');
}
if ((await snapshot()).size !== after.size) fail('the second run left something');
results.migration = { added: 1, changed: 0, twice: 'handoff_wait_alerts_already_applied' };

// ---------------------------------------------------------------------------
// Las filas: Maribel (cuenta 3, bandeja 13) y ruido de ATT1 (cuenta 2, bandeja 11).
// ---------------------------------------------------------------------------
const NOW = new Date('2026-11-02T21:45:00Z');
const hoursAgo = (hours) => new Date(NOW.getTime() - hours * 3600 * 1000).toISOString();
let serial = 0;
const uuid = () => {
  serial += 1;
  return `00000000-0000-4000-8000-${String(serial).padStart(12, '0')}`;
};
const rows = [];
const handoff = (account, inbox, conversation, primary, detail, status, createdHoursAgo, attendedHoursAgo = null) => {
  const id = uuid();
  rows.push({ id, conversation, createdHoursAgo, detail, account });
  const projected = status === 'projected' ? `'${hoursAgo(createdHoursAgo)}'` : 'null';
  const attended = attendedHoursAgo === null ? 'null' : `'${hoursAgo(attendedHoursAgo)}'`;
  return `('${id}', '${uuid()}', 'handoff:${account}:${conversation}:${serial}', '${primary}', 'agent', 'handoff-policy', 1,
           ${account}, ${inbox}, ${conversation}, 3, 'handoff-note', 1, 'CANARY_NOTE nota privada', '${status}',
           '${hoursAgo(createdHoursAgo)}', ${projected}, '${uuid()}', 'inbound', 1,
           ${detail === null ? 'null' : `'${detail}'`}, ${attended})`;
};
const values = [
  // 1. Maribel: la atendida en 2 minutos (no sale) y las tres sin respuesta.
  handoff(3, 13, 20, 'commercial_exception', 'explicit_human_request', 'projected', 140, 139.9),
  handoff(3, 13, 8, 'commercial_exception', 'payment_link_purchase_already_approved', 'projected', 125.7),
  handoff(3, 13, 39, 'commercial_exception', 'agent_turn_failed', 'projected', 76.5),
  // 2. La 46 derivada dos veces: sale la mas nueva.
  handoff(3, 13, 46, 'commercial_exception', 'explicit_human_request', 'projected', 70),
  handoff(3, 13, 46, 'commercial_exception', 'policy_requires_human', 'projected', 66.1),
  // 3. Lo que no entra, y la que si (requested).
  handoff(2, 11, 655, 'commercial_exception', 'payment_link_conversation_advanced', 'projected', 5.5),
  handoff(3, 9, 77, 'commercial_exception', null, 'projected', 10),
  handoff(3, 13, 90, 'commercial_exception', null, 'dead_letter', 12),
  handoff(3, 13, 91, 'explicit_human_request', null, 'projected', 200),
  handoff(3, 13, 92, 'policy_requires_human', null, 'requested', 1),
];
await db.exec(`
  set session_replication_role = replica;
  insert into public.human_handoff_requests (id, conversation_id, command_key, primary_reason_code, requested_by,
    projection_policy_key, projection_policy_version, chatwoot_account_id, chatwoot_inbox_id, external_conversation_id,
    expected_team_id, note_template_key, note_template_version, private_note_body, status, created_at, projected_at,
    commercial_case_id, inbound_scope_key, inbound_scope_version, detail_reason_code, attended_at)
  values ${values.join(',\n')};
  set session_replication_role = origin;
`);

await db.query('set role service_role');
const list = async (account, inbox, sinceHours, limit = 200) => (await db.query(
  `select * from public.list_unattended_human_handoffs_v1($1, $2, $3::timestamptz, $4)`,
  [account, inbox, hoursAgo(sinceHours), limit],
)).rows;

const maribel = await list(3, 13, 168);
const expected = [
  [8, 'payment_link_purchase_already_approved'],
  [39, 'agent_turn_failed'],
  [46, 'policy_requires_human'],
  [92, null],
];
const got = maribel.map((row) => [Number(row.external_conversation_id), row.detail_reason_code]);
if (JSON.stringify(got) !== JSON.stringify(expected)) fail('maribel unattended', got);
const newest46 = rows.filter((row) => row.conversation === 46 && row.account === 3).at(-1).id;
if (maribel[2].handoff_request_id !== newest46) fail('46 must come with its newest handoff', maribel[2]);
const keys = Object.keys(maribel[0]).sort().join(',');
if (keys !== 'created_at,detail_reason_code,external_conversation_id,handoff_request_id,primary_reason_code') {
  fail('row shape', keys);
}
if (JSON.stringify(maribel).includes('CANARY_NOTE')) fail('the private note leaked');
results.maribel = got;

// La ventana deja afuera la 91 (200 h) y, con 100 h, tambien la 8 (125,7 h).
const shorter = (await list(3, 13, 100)).map((row) => Number(row.external_conversation_id));
if (JSON.stringify(shorter) !== JSON.stringify([39, 46, 92])) fail('since window', shorter);
const withOld = (await list(3, 13, 240)).map((row) => Number(row.external_conversation_id));
if (JSON.stringify(withOld) !== JSON.stringify([91, 8, 39, 46, 92])) fail('older ceiling', withOld);
// El limite corta por la mas vieja primero.
const limited = (await list(3, 13, 168, 2)).map((row) => Number(row.external_conversation_id));
if (JSON.stringify(limited) !== JSON.stringify([8, 39])) fail('limit', limited);
// ATT1 ve solo lo suyo.
const att1 = (await list(2, 11, 168)).map((row) => Number(row.external_conversation_id));
if (JSON.stringify(att1) !== JSON.stringify([655])) fail('att1 scope', att1);
results.scope = { shorter, withOld, limited, att1 };

for (const args of [[0, 13, 168, 10], [3, null, 168, 10], [3, 13, 168, 0], [3, 13, 168, 501]]) {
  try {
    await db.query(
      `select * from public.list_unattended_human_handoffs_v1($1, $2, $3::timestamptz, $4)`,
      [args[0], args[1], hoursAgo(args[2]), args[3]],
    );
    fail('invalid input passed', args);
  } catch (error) {
    if (!String(error.message).includes('list_unattended_human_handoffs_invalid_input')) throw error;
  }
}
results.invalid_input = '22023';

await db.query('reset role');
await db.query('set role anon');
try {
  await db.query(`select * from public.list_unattended_human_handoffs_v1(3, 13, now(), 10)`);
  fail('anon can read the unattended handoffs');
} catch (error) {
  if (!/permission denied/i.test(String(error.message))) throw error;
}
await db.query('reset role');

console.log(`handoff_wait_alerts=OK ${JSON.stringify(results)}`);
