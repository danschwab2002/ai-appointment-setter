// El registro por persona de un envio masivo, para el reporte (migracion
// 20261125000400, D10 del PR #511; lo lee el adaptador BulkSendRecipientsReport
// de src/bridge/bulk_send_wiring.py y lo arma bulk_send_report.build_report).
//
// Datos: la instancia de prueba (ATT1) y la persona que entra por «Entrar con
// Slack», como en validate_bulk_send_prepared_and_base_vetoes.mjs; filas de lista
// sinteticas con digitos repetidos. Las compras salen del PURCHASE_APPROVED
// capturado de Maribel (tests/fixtures/hotmart_purchase_approved_maribel_20261029.json,
// ya anonimizado), con el telefono, el mail y la transaccion sinteticos.
//
// Prueba:
//   0. la forma: una funcion nueva del bridge solo de service_role y la auxiliar de
//      nadie; nada mas cambia; correrla dos veces aborta;
//   1. el registro: las personas sin exclusion, en orden, con su estado (enviado,
//      fallido, dudoso, vetado, reservado y pendiente sin registro), la conversacion,
//      el mensaje, el codigo de Meta y el cierre; la excluida por los vetos no
//      aparece; el cursor por posicion; los limites; un envio sin lista; uno que no
//      existe;
//   2. la compra: la primera desde el cierre, por el telefono en otra forma o por la
//      huella del mail, del producto del envio; no cuentan una anterior al cierre,
//      otro producto, una reembolsada ni la de quien no recibio;
//   3. purgada la lista, el registro sigue sin telefono;
//   4. sin la migracion la funcion no existe (el rojo).
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const fail = (message, detail) => {
  console.error(`validate_bulk_send_recipients_read: ${message}`, detail === undefined ? '' : JSON.stringify(detail));
  process.exit(1);
};
const MIGRATION = '20261125000400_bulk_send_recipients_read_v1.sql';
const BRIDGE = ['read_bulk_send_recipients_v1(uuid,integer,integer)'];
const PRIVATE = ['_bulk_send_purchase_times(text)'];
const results = {};
const sha = (text) => createHash('sha256').update(text).digest('hex');

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
for (const name of names.slice(position + 1)) {
  if (readFileSync(join(root, 'supabase/migrations', name), 'utf8').includes('bulk_')) {
    fail(`${name} toca el masivo: revisar esta prueba`);
  }
}

const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select replace(p.oid::regprocedure::text, 'public.', '') as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('flow_change_approver', p.oid, 'execute') as page_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));

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

// ---------------------------------------------------------------------------
// 0. La forma.
// ---------------------------------------------------------------------------
{
  const before = await snapshot(db);
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.has(s)
    && ['definition', 'acl', 'prosecdef', 'service_x', 'page_x', 'api_x'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  const added = [...after.keys()].filter((s) => !before.has(s)).sort();
  if (changed.length || JSON.stringify(added) !== JSON.stringify([...BRIDGE, ...PRIVATE].sort())) {
    fail('la migracion cambio otra cosa', { changed, added });
  }
  for (const signature of BRIDGE) {
    const fn = after.get(signature);
    if (!fn.service_x || fn.page_x || fn.api_x || !fn.prosecdef) fail(`${signature}: solo service_role`, fn);
  }
  for (const signature of PRIVATE) {
    const fn = after.get(signature);
    if (fn.service_x || fn.page_x || fn.api_x) fail(`${signature}: la auxiliar no se expone`, fn);
  }
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'bulk_send_recipients_read_already_applied') fail('la segunda corrida', error.message);
    await db.exec('rollback');
  }
  results.forma = { nuevas: added.length };
}

// ---------------------------------------------------------------------------
// La instancia y la persona que entra por la pagina.
// ---------------------------------------------------------------------------
await db.exec(`
  set session_replication_role = replica;
  insert into public.commercial_ally_runtime_bindings (tenant_ref, funnel_ref, binding_version, status,
    ally_ref, lead_ally_name, lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
    product_name, product_price, currency, offer_code, consent_copy_version, hotmart_product_id,
    chatwoot_account_id, chatwoot_inbox_id, inbound_scope_key, inbound_scope_version)
  values ('lancemos', 'att1', 1, 'active', 'att1', 'Aliada de prueba', 'sitio', 'landing', 'prueba.example',
    '/landing', 'H00000000', 'Producto de prueba', 10, 'MXN', 'OFR11', 'copy-v1', 1011, 2, 11, 'att1-inbound', 1),
    ('lancemos', 'maribel', 1, 'active', 'maribel', 'Otra aliada', 'sitio', 'landing', 'otra.example',
    '/landing', 'H00000001', 'Otro producto', 10, 'USD', 'OFR12', 'copy-v1', 1012, 3, 13, 'maribel-inbound', 1);
  set session_replication_role = origin;
`);
const INSTANCE = { tenant_ref: 'lancemos', ally_ref: 'att1', funnel_ref: 'att1' };
const TEAM = 'T0LANCEMOS01';
const DAN = 'U0DANSCHWAB1';
const one = (rows, label) => {
  if (rows.length !== 1) fail(`${label}: una fila`, rows);
  return rows[0];
};
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const rpc = async (fn, ...args) => {
  const marks = args.map((_, i) => `$${i + 1}`).join(', ');
  const values = args.map((a) => (a !== null && typeof a === 'object' && !Array.isArray(a) ? JSON.stringify(a) : a));
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
const dbNow = async () => new Date(one(await sql('select clock_timestamp() as t'), 'ahora').t);
const inMinutes = async (minutes) => new Date((await dbNow()).getTime() + minutes * 60_000).toISOString();
const session = sha('reporte-session');
await as('flow_change_approver', async () => {
  const state = sha('reporte-state');
  await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha('reporte-nonce'), '/flow-approval/', await inMinutes(10));
  await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
  const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, TEAM, DAN, await inMinutes(60));
  if (!created.ok) fail('entrar', created);
});
const page = (fn, ...args) => as('flow_change_approver', () => rpc(fn, ...args));

const PHONES = ['5215511111111', '5215522222222', '5215533333333', '5215544444444', '5215555555555',
  '5215566666666', '5215577777777', '5215588888888'];
const EMAIL = (i) => `persona${i}@example.com`;
const rows = PHONES.map((phone, i) => ({ phone, first_name: `Persona${i}`, email: EMAIL(i) }));
const draft = async (extra = {}) => (await rpc('create_bulk_send_v1', INSTANCE, {
  template_name: 'att1_vivo_recordatorio_01', template_language: 'es_MX',
  template_variables: [{ de: 'nombre' }], campaign_label: 'vivo-29-11',
  scheduled_at: '2026-11-29T23:00:00Z', product_refs: ['att1'], ...extra })).send_id;
const sendId = await draft();
const listId = (await page('approver_create_bulk_list_v1', INSTANCE, session, sendId, {
  file_sha256: sha('lista.csv'), source_kind: 'inscritas_ghl', source_note: 'inscritas al vivo',
  notice_declared: true, rejected_counts: {}, rows })).list_id;
await rpc('apply_bulk_list_vetoes_v1', INSTANCE, listId, [{ position: 1, reason: 'baja' }]);
const confirmed = await page('approver_confirm_bulk_send_v1', INSTANCE, session, sendId, 7, true, 'verde', false);
if (!confirmed.ok) fail('confirmar', confirmed);
await rpc('transition_bulk_send_v1', INSTANCE, sendId, 'preparando', null);
await rpc('transition_bulk_send_v1', INSTANCE, sendId, 'enviando', null);

const close = async (pos, outcome) => {
  const reserved = await rpc('reserve_bulk_send_recipient_v1', sendId, pos, false);
  if (!reserved.ok) fail(`reservar ${pos}`, reserved);
  if (outcome) {
    const closed = await rpc('close_bulk_send_recipient_v1', sendId, pos, outcome);
    if (!closed.ok) fail(`cerrar ${pos}`, closed);
  }
};

// Las compras, del PURCHASE_APPROVED capturado.
const capture = JSON.parse(readFileSync(join(root, 'tests/fixtures/hotmart_purchase_approved_maribel_20261029.json'),
  'utf8')).payload;
let purchaseNumber = 0;
const purchase = async ({ phone, email, funnel = 'att1', type = 'PURCHASE_APPROVED', transaction, at = null }) => {
  purchaseNumber += 1;
  const payload = structuredClone(capture);
  payload.event = type;
  payload.data.buyer.checkout_phone = phone;
  payload.data.buyer.email = email;
  payload.data.purchase.transaction = transaction ?? `HP${String(purchaseNumber).padStart(10, '0')}`;
  const event = one(await sql(`insert into public.webhook_events (source, external_event_id, event_type, payload,
      received_at) values ('hotmart', $1, $2, $3::jsonb, coalesce($4::timestamptz, clock_timestamp())) returning id`,
  [`evento-${purchaseNumber}`, type, JSON.stringify(payload), at]), 'evento').id;
  if (type === 'PURCHASE_APPROVED') {
    await sql(`insert into public.portable_hotmart_purchase_correlations (webhook_event_id, tenant_ref, funnel_ref,
        binding_version, policy_max_lookback, outcome, candidate_count, reason_code, observed_at)
      values ($1, 'lancemos', $2, 1, interval '30 days', 'unmatched', 0, 'sin_intencion', now())`, [event, funnel]);
  }
  return payload.data.purchase.transaction;
};

// 0: enviado; compro antes del envio (no cuenta) y despues por la otra forma del telefono (cuenta).
await purchase({ phone: '525511111111', email: 'otra0@example.com', at: '2026-01-01T00:00:00Z' });
await close(0, { state: 'enviado', chatwoot_conversation_id: 9000, chatwoot_message_id: 70000 });
// 2: fallido antes de tocar Chatwoot (G3); compro, pero no recibio.
await close(2, { state: 'fallido', reason: 'g3_escribio_hace_poco' });
// 3: dudoso.
await close(3, { state: 'dudoso', reason: 'reservado_sin_cierre' });
// 4: vetado al revalidar.
await close(4, { state: 'vetado', reason: 'baja' });
// 5: reservado sin cierre (en curso).
await close(5, null);
// 6: sin registro: pendiente.
// 7: enviado con codigo de Meta; compro otro producto y una reembolsada (no cuentan); despues, por el mail.
await close(7, { state: 'enviado', chatwoot_conversation_id: 9007, chatwoot_message_id: 70007,
  meta_error_code: '131049' });
await purchase({ phone: '525533333333', email: 'otra2@example.com' });
await purchase({ phone: '525588888888', email: 'otra7@example.com', funnel: 'maribel' });
const refunded = await purchase({ phone: '525588888888', email: 'otra7@example.com' });
await purchase({ phone: '525588888888', email: 'otra7@example.com', type: 'PURCHASE_REFUNDED', transaction: refunded });
await purchase({ phone: '525599999999', email: EMAIL(7) });
await purchase({ phone: '525511111111', email: 'otra0@example.com' });

const asService = (fn, ...args) => as('service_role', () => rpc(fn, ...args));
const all = await asService('read_bulk_send_recipients_v1', sendId, -1, 500);
const pageOne = await asService('read_bulk_send_recipients_v1', sendId, -1, 3);
const pageTwo = await asService('read_bulk_send_recipients_v1', sendId, pageOne[pageOne.length - 1].position, 3);
const pageThree = await asService('read_bulk_send_recipients_v1', sendId, pageTwo[pageTwo.length - 1].position, 3);
const pageEnd = await asService('read_bulk_send_recipients_v1', sendId, 7, 3);
const invalid = {};
for (const [label, args] of Object.entries({ cursor: [-2, 3], cero: [-1, 0], tope: [-1, 501], nulo: [null, 3] })) {
  invalid[label] = await errorOf(() => rpc('read_bulk_send_recipients_v1', sendId, ...args));
}
const unknown = await errorOf(() => rpc('read_bulk_send_recipients_v1', '00000000-0000-4000-8000-000000000000', -1, 3));
const noList = await rpc('read_bulk_send_recipients_v1', await draft({ template_name: 'att1_sin_lista_01' }), -1, 3);
const byPosition = Object.fromEntries(all.map((r) => [r.position, r]));
const positions = (list) => JSON.stringify(list.map((r) => r.position));
const states = all.map((r) => `${r.position}:${r.state}`).join(',');
const p0 = byPosition[0];
const p7 = byPosition[7];
if (positions(all) !== '[0,2,3,4,5,6,7]'
    || states !== '0:enviado,2:fallido,3:dudoso,4:vetado,5:reservado,6:pendiente,7:enviado'
    || p0.phone !== PHONES[0] || p0.chatwoot_conversation_id !== 9000 || p0.chatwoot_message_id !== 70000
    || !p0.closed_at || p0.meta_error_code !== null
    || !p0.purchased_at || new Date(p0.purchased_at) < new Date(p0.closed_at)
    || p7.meta_error_code !== '131049' || !p7.purchased_at
    || byPosition[2].purchased_at !== null || byPosition[2].chatwoot_message_id !== null
    || byPosition[5].closed_at !== null || byPosition[6].closed_at !== null
    || positions(pageOne) !== '[0,2,3]' || positions(pageTwo) !== '[4,5,6]' || positions(pageThree) !== '[7]'
    || pageEnd.length !== 0 || Object.values(invalid).some((e) => e !== 'bulk_send_read_invalid')
    || unknown !== 'bulk_send_not_found' || noList.length !== 0) {
  fail('el registro', { all, pageOne, pageTwo, pageThree, pageEnd, invalid, unknown, noList });
}
// La compra de 0 es la de despues del envio, no la de enero; la de 7 es la del mail, no la reembolsada.
const lastEvents = await sql(`select e.received_at, e.payload #>> '{data,buyer,email}' as email
  from public.webhook_events e where e.event_type = 'PURCHASE_APPROVED' order by e.received_at`);
const afterFor = (email) => lastEvents.filter((e) => e.email === email).at(-1).received_at;
if (new Date(p0.purchased_at).getTime() !== new Date(afterFor('otra0@example.com')).getTime()
    || new Date(p7.purchased_at).getTime() !== new Date(afterFor(EMAIL(7))).getTime()) {
  fail('la compra que cuenta', { p0: p0.purchased_at, p7: p7.purchased_at, lastEvents });
}
results.registro = { estados: states, paginas: [positions(pageOne), positions(pageTwo), positions(pageThree)] };

// Un envio sin producto no ve compras.
{
  const bare = await draft({ product_refs: undefined, template_name: 'att1_sin_producto_01' });
  const bareList = (await page('approver_create_bulk_list_v1', INSTANCE, session, bare, {
    file_sha256: sha('otra.csv'), source_kind: 'inscritas_ghl', source_note: 'otra', notice_declared: true,
    rejected_counts: {}, rows: [rows[0]] })).list_id;
  await rpc('apply_bulk_list_vetoes_v1', INSTANCE, bareList, []);
  await page('approver_confirm_bulk_send_v1', INSTANCE, session, bare, 1, true, 'verde', false);
  await rpc('transition_bulk_send_v1', INSTANCE, bare, 'preparando', null);
  await rpc('transition_bulk_send_v1', INSTANCE, bare, 'enviando', null);
  await rpc('reserve_bulk_send_recipient_v1', bare, 0, false);
  await rpc('close_bulk_send_recipient_v1', bare, 0, { state: 'enviado', chatwoot_conversation_id: 9100,
    chatwoot_message_id: 71000 });
  await purchase({ phone: PHONES[0], email: EMAIL(0) });
  const bareRead = await rpc('read_bulk_send_recipients_v1', bare, -1, 10);
  if (bareRead.length !== 1 || bareRead[0].purchased_at !== null) fail('sin producto no hay compra', bareRead);
  await rpc('transition_bulk_send_v1', INSTANCE, bare, 'frenado', 'prueba');
}

// ---------------------------------------------------------------------------
// 3. Purgada la lista, el registro sigue sin telefono.
// ---------------------------------------------------------------------------
await rpc('transition_bulk_send_v1', INSTANCE, sendId, 'frenado', 'prueba');
await rpc('purge_bulk_list_phones_v1', await inMinutes(31 * 24 * 60));
const purged = await rpc('read_bulk_send_recipients_v1', sendId, -1, 500);
if (positions(purged) !== '[0,2,3,4,5,6,7]' || purged.some((r) => r.phone !== null)
    || purged.map((r) => r.state).join(',') !== all.map((r) => r.state).join(',')
    || purged[purged.length - 1].purchased_at === null) {
  fail('purgada', purged);
}
results.purgada = { personas: purged.length, sin_telefono: true };

// ---------------------------------------------------------------------------
// 4. El rojo: sin la migracion no hay lectura.
// ---------------------------------------------------------------------------
const missing = await errorOf(async () => {
  await db.exec('drop function public.read_bulk_send_recipients_v1(uuid,integer,integer)');
  await rpc('read_bulk_send_recipients_v1', sendId, -1, 3);
});
if (!missing.includes('does not exist')) fail('sin la migracion la funcion no existe', missing);
results.sin_la_migracion = 'no_existe';

await db.close();
console.log(`validate_bulk_send_recipients_read: ok ${JSON.stringify(results)}`);
