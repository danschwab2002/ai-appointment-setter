// Los vetos de la lista del masivo contra la base real (encargo 4 del mapa «El masivo manejado
// por el Automation Expert»): lo que arma bridge/bulk_send_vetoes.py, aplicado a
// apply_bulk_list_vetoes_v1 de 20261125000200.
//
// Datos: tests/fixtures/bulk_send_vetoes/apply_payload_20261125.json, el mismo que compara
// tests/test_bulk_send_vetoes.py con lo que arma el bridge (16 personas sinteticas, 12 vetadas
// con los siete motivos). Si el armado del bridge o la base cambian, se rompe uno de los dos.
//
// Prueba:
//   1. la base acepta el armado del bridge tal cual: deja la lista revisada, con las exclusiones
//      y los conteos por motivo que dice el bridge, y la tanda del motor no trae a ningun vetado;
//   2. volver a aplicar reemplaza (no suma): una segunda revision con menos vetos deja solo esos;
//   3. un motivo fuera de los siete, una posicion repetida o una que no esta no entran, y la
//      lista queda como estaba;
//   4. con el envio confirmado ya no se revisa (not_a_draft).
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const fail = (message, detail) => {
  console.error(`validate_bulk_send_vetoes: ${message}`, detail === undefined ? '' : JSON.stringify(detail));
  process.exit(1);
};
const MIGRATION = '20261125000200_bulk_send_store_v1.sql';
const golden = JSON.parse(readFileSync(join(root, 'tests/fixtures/bulk_send_vetoes/apply_payload_20261125.json'), 'utf8'));
const sha = (text) => createHash('sha256').update(text).digest('hex');
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));

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
for (const name of names) await applyFile(db, join(root, 'supabase/migrations', name));

await db.exec(`
  set session_replication_role = replica;
  insert into public.commercial_ally_runtime_bindings (tenant_ref, funnel_ref, binding_version, status,
    ally_ref, lead_ally_name, lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
    product_name, product_price, currency, offer_code, consent_copy_version, hotmart_product_id,
    chatwoot_account_id, chatwoot_inbox_id, inbound_scope_key, inbound_scope_version)
  values ('lancemos', 'maribel', 1, 'active', 'maribel', 'Aliada de prueba', 'sitio', 'landing', 'prueba.example',
    '/landing', 'H00000001', 'Producto de prueba', 10, 'USD', 'OFR12', 'copy-v1', 1012, 3, 13, 'maribel-inbound', 1);
  set session_replication_role = origin;
`);
const INSTANCE = { tenant_ref: 'lancemos', ally_ref: 'maribel', funnel_ref: 'maribel' };
const TEAM = 'T0LANCEMOS01';
const DAN = 'U0DANSCHWAB1';
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const one = (rows, label) => {
  if (rows.length !== 1) fail(`${label}: una fila`, rows);
  return rows[0];
};
const rpc = async (fn, ...args) => {
  const marks = args.map((_, i) => `$${i + 1}`).join(', ');
  const values = args.map((a) => (a !== null && typeof a === 'object' ? JSON.stringify(a) : a));
  return JSON.parse(one(await sql(`select public.${fn}(${marks})::text as r`, values), fn).r);
};
const as = async (role, run) => {
  await db.exec(`set role ${role}`);
  try {
    return await run();
  } finally {
    await db.exec('reset role');
  }
};
const errorOf = async (run) => {
  await db.exec('begin');
  try {
    await run();
    return 'paso';
  } catch (error) {
    return error.message;
  } finally {
    await db.exec('rollback');
  }
};
const inMinutes = async (minutes) => new Date(
  new Date(one(await sql('select clock_timestamp() as t'), 'ahora').t).getTime() + minutes * 60_000).toISOString();

// La persona que sube la lista entra por «Entrar con Slack», como en la pagina.
const session = sha('vetos-session');
await as('flow_change_approver', async () => {
  const state = sha('vetos-state');
  await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha('vetos-nonce'), '/flow-approval/', await inMinutes(10));
  await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
  const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, TEAM, DAN, await inMinutes(60));
  if (!created.ok) fail('entrar', created);
});

const draft = async () => (await rpc('create_bulk_send_v1', INSTANCE, {
  template_name: 'maribel_vivo_recordatorio_01', template_language: 'es_MX', template_variables: [{ de: 'nombre' }],
  scheduled_at: '2026-11-29T23:00:00Z' })).send_id;
const listFor = async (sendId) => {
  const created = await as('flow_change_approver', () => rpc('approver_create_bulk_list_v1', INSTANCE, session, sendId, {
    file_sha256: sha('lista-vetos.csv'), source_kind: 'inscritas_ghl', notice_declared: true, rows: golden.rows }));
  if (!created.ok || created.member_count !== golden.members) fail('la lista', created);
  return created.list_id;
};
const view = async (sendId) => rpc('read_bulk_send_v1', INSTANCE, sendId);
const reasons = async (listId) => Object.fromEntries((await sql(
  'select position, exclusion_reason from public.bulk_list_members where list_id = $1 and exclusion_reason is not null order by position',
  [listId])).map((r) => [r.position, r.exclusion_reason]));

// ---------------------------------------------------------------------------
// 1. El armado del bridge, tal cual.
// ---------------------------------------------------------------------------
const sendId = await draft();
const listId = await listFor(sendId);
{
  const applied = await rpc('apply_bulk_list_vetoes_v1', INSTANCE, listId, golden.p_exclusions);
  if (!applied.ok || applied.excluded !== golden.p_exclusions.length || applied.eligible !== golden.eligible) {
    fail('aplicar el armado del bridge', applied);
  }
  const v = await view(sendId);
  if (v.list.state !== 'revisada' || v.list.eligible_count !== golden.eligible
      || JSON.stringify(Object.fromEntries(Object.entries(v.list.excluded_by_reason).sort()))
         !== JSON.stringify(golden.excluded_by_reason)) {
    fail('la vista despues de los vetos', v.list);
  }
  const expected = Object.fromEntries(golden.p_exclusions.map((e) => [e.position, e.reason]));
  if (JSON.stringify(await reasons(listId)) !== JSON.stringify(expected)) fail('las exclusiones', await reasons(listId));
  if (new Set(Object.values(expected)).size !== 7) fail('el armado tiene que traer los siete motivos', expected);
  results.armado = { excluidas: applied.excluded, quedan: applied.eligible, motivos: v.list.excluded_by_reason };
}

// ---------------------------------------------------------------------------
// 2. Volver a aplicar reemplaza.
// ---------------------------------------------------------------------------
{
  const fewer = golden.p_exclusions.slice(0, 2);
  const applied = await rpc('apply_bulk_list_vetoes_v1', INSTANCE, listId, fewer);
  if (!applied.ok || applied.excluded !== 2 || applied.eligible !== golden.members - 2) fail('reaplicar', applied);
  const now = await reasons(listId);
  if (Object.keys(now).length !== 2) fail('reaplicar reemplaza', now);
  const again = await rpc('apply_bulk_list_vetoes_v1', INSTANCE, listId, golden.p_exclusions);
  if (!again.ok || again.excluded !== golden.p_exclusions.length) fail('volver al armado', again);
  results.reemplaza = true;
}

// ---------------------------------------------------------------------------
// 3. Lo que no entra, y la lista queda como estaba.
// ---------------------------------------------------------------------------
{
  const before = JSON.stringify(await reasons(listId));
  const bad = {
    motivo: [{ position: 0, reason: 'activo_6h' }],
    repetida: [{ position: 0, reason: 'baja' }, { position: 0, reason: 'compro' }],
    fuera: [{ position: golden.members, reason: 'baja' }],
  };
  const got = {};
  for (const [label, exclusions] of Object.entries(bad)) {
    got[label] = await errorOf(() => rpc('apply_bulk_list_vetoes_v1', INSTANCE, listId, exclusions));
  }
  if (got.motivo !== 'bulk_list_vetoes_invalid' || got.repetida !== 'bulk_list_vetoes_invalid'
      || got.fuera !== 'bulk_list_vetoes_unknown_position') {
    fail('lo que no entra', got);
  }
  if (JSON.stringify(await reasons(listId)) !== before) fail('la lista cambio con un rechazo');
  results.rechazos = got;
}

// ---------------------------------------------------------------------------
// 4. Confirmado, ya no se revisa.
// ---------------------------------------------------------------------------
{
  const confirmed = await as('flow_change_approver', () => rpc('approver_confirm_bulk_send_v1', INSTANCE, session, sendId,
    golden.eligible, true, 'verde', false));
  if (!confirmed.ok) fail('confirmar', confirmed);
  const late = await rpc('apply_bulk_list_vetoes_v1', INSTANCE, listId, []);
  if (late.ok !== false || late.reason_code !== 'not_a_draft') fail('confirmado no se revisa', late);
  const batch = await rpc('read_bulk_send_batch_v1', sendId, 100);
  const vetoed = new Set(golden.p_exclusions.map((e) => e.position));
  if (batch.length !== golden.eligible || batch.some((m) => vetoed.has(m.position))) fail('la tanda trae vetados', batch);
  results.confirmado = { tanda: batch.length };
}

console.log(JSON.stringify({ validate_bulk_send_vetoes: 'ok', ...results }));
