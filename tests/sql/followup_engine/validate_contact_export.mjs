// Exportar contactos (migracion 20261126300000, contact-export-v1, funcion 1.3;
// lo usan src/bridge/contact_export.py y la pagina /flow-approval/exportar).
//
// Datos: la instancia de prueba (ATT1) y la persona que entra por «Entrar con
// Slack», como en validate_bulk_send_recipients_read.mjs; contactos, formularios
// y bajas sinteticos. Las compras salen del PURCHASE_APPROVED capturado de
// Maribel (tests/fixtures/hotmart_purchase_approved_maribel_20261029.json, ya
// anonimizado), con el telefono, el mail y la transaccion sinteticos. La forma de
// cada persona se compara con tests/fixtures/contact_export/people_page_shape_20261126.json
// (la medida en ATT1 y Maribel), que es la que parsea el bridge.
//
// Prueba:
//   0. la forma: tres funciones del bridge solo de service_role, tres de la pagina
//      solo de flow_change_approver, tres internas de nadie, la tabla de nadie;
//      nada mas cambia; correrla dos veces aborta;
//   1. las personas: una por telefono canonico (el contacto, el formulario y la
//      compra de la misma persona en sus dos formas se juntan), el nombre y el
//      mail en su orden, los formularios del tenant con su producto, las compras
//      sin reembolso, la baja por evento y por autorizacion denegada; otro tenant
//      y una compra reembolsada no entran; el cursor y los limites;
//   2. el pedido: crear (y lo que no cierra), vence a los 30 minutos, el tope de
//      20 por hora; la vista del bridge sin quien lo pidio;
//   3. la pagina: con la sesion, lee el pedido y las personas solo pendiente;
//      marcar una vez; ya bajado y vencido no leen personas ni marcan; otra
//      instancia no lo ve; sin sesion, 28000;
//   4. sin la migracion las funciones no existen (el rojo).
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const fail = (message, detail) => {
  console.error(`validate_contact_export: ${message}`, detail === undefined ? '' : JSON.stringify(detail));
  process.exit(1);
};
const MIGRATION = '20261126300000_contact_export_v1.sql';
const BRIDGE = ['create_contact_export_v1(jsonb,jsonb)', 'read_contact_export_v1(jsonb,uuid)',
  'read_contact_export_people_v1(jsonb,text,integer)'];
const PAGE = ['approver_read_contact_export_v1(jsonb,text,uuid)',
  'approver_read_contact_export_people_v1(jsonb,text,uuid,text,integer)',
  'approver_mark_contact_export_downloaded_v1(jsonb,text,uuid,integer)'];
const PRIVATE = ['_contact_export_people(text,text,integer)', '_contact_export_check_page(text,integer)',
  '_contact_export_view(contact_exports,boolean)'];
const SHAPE = JSON.parse(readFileSync(join(root, 'tests/fixtures/contact_export/people_page_shape_20261126.json'),
  'utf8')).page;
const results = {};
const sha = (text) => createHash('sha256').update(text).digest('hex');

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
for (const name of names.slice(position + 1)) {
  if (readFileSync(join(root, 'supabase/migrations', name), 'utf8').includes('contact_export')) {
    fail(`${name} toca la exportacion: revisar esta prueba`);
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
  if (changed.length || JSON.stringify(added) !== JSON.stringify([...BRIDGE, ...PAGE, ...PRIVATE].sort())) {
    fail('la migracion cambio otra cosa', { changed, added });
  }
  for (const signature of BRIDGE) {
    const fn = after.get(signature);
    if (!fn.service_x || fn.page_x || fn.api_x || !fn.prosecdef) fail(`${signature}: solo service_role`, fn);
  }
  for (const signature of PAGE) {
    const fn = after.get(signature);
    if (fn.service_x || !fn.page_x || fn.api_x || !fn.prosecdef) fail(`${signature}: solo la pagina`, fn);
  }
  for (const signature of PRIVATE) {
    const fn = after.get(signature);
    if (fn.service_x || fn.page_x || fn.api_x) fail(`${signature}: la interna no se expone`, fn);
  }
  const table = (await db.query(`select
      has_table_privilege('service_role', 'public.contact_exports', 'select') as s,
      has_table_privilege('anon', 'public.contact_exports', 'select') as a,
      has_table_privilege('flow_change_approver', 'public.contact_exports', 'select') as f,
      (select relrowsecurity from pg_class where oid = 'public.contact_exports'::regclass) as rls`)).rows[0];
  if (table.s || table.a || table.f || !table.rls) fail('la tabla es de nadie', table);
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'contact_export_already_applied') fail('la segunda corrida', error.message);
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
const INSTANCE = { tenant_ref: 'lancemos', ally_ref: 'att1' };
const OTHER = { tenant_ref: 'lancemos', ally_ref: 'maribel' };
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
// Lo mismo con un rol: el set role va adentro de la transaccion, asi el rollback lo deshace.
const errorAs = async (role, run) => {
  await db.exec('begin');
  try {
    await db.exec(`set local role ${role}`);
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
const session = sha('exportar-session');
await as('flow_change_approver', async () => {
  const state = sha('exportar-state');
  await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha('exportar-nonce'), '/flow-approval/', await inMinutes(10));
  await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
  const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, TEAM, DAN, await inMinutes(60));
  if (!created.ok) fail('entrar', created);
});
const service = (fn, ...args) => as('service_role', () => rpc(fn, ...args));
const page = (fn, ...args) => as('flow_change_approver', () => rpc(fn, ...args));

// ---------------------------------------------------------------------------
// 1. Las personas.
// ---------------------------------------------------------------------------
const contact = async ({ name = null, email = null, phone = null, points = [], whatsapp = null }) => {
  const id = one(await sql(`insert into public.contacts (full_name, email, phone) values ($1, $2, $3) returning id`,
    [name, email, phone]), 'contacto').id;
  for (const value of points) {
    await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
      values ($1, 'phone', $2, $2, 'system')`, [id, value]);
  }
  if (whatsapp) {
    await sql(`insert into public.channel_identities (contact_id, channel, account_id, external_user_id)
      values ($1, 'whatsapp', 'cuenta', $2)`, [id, whatsapp]);
  }
  return id;
};
let formNumber = 0;
const form = async ({ phone, email = null, name = null, at, tenant = 'lancemos', funnel = 'att1' }) => {
  formNumber += 1;
  const intent = one(await sql(`insert into public.purchase_intents (tenant_ref, funnel_ref, landing_ref, product_ref,
      offer_ref, normalized_email, normalized_phone, submitted_at, lifecycle_state, whatsapp_contact_authorized,
      provisional, provider_observed, activation_authorized)
    values ($1, $2, 'landing', 'H00000000', $3, $4, $5, $6, 'cancelled', true, false, true, true) returning id`,
  [tenant, funnel, `OFR${formNumber}`, email, phone, at]), 'formulario').id;
  const submission = one(await sql(`insert into public.precheckout_submissions (external_submission_id,
      contract_version, raw_payload, canonical_payload, provisional, provider_observed, activation_authorized)
    values ($1, '1.1.0', '{}'::jsonb, jsonb_build_object('lead', jsonb_build_object('full_name', $2::text)),
      false, true, true) returning id`, [`envio-${formNumber}`, name]), 'envio').id;
  await sql(`insert into public.purchase_intent_submissions (purchase_intent_id, submission_id, ordinal)
    values ($1, $2, 1)`, [intent, submission]);
};
const capture = JSON.parse(readFileSync(join(root, 'tests/fixtures/hotmart_purchase_approved_maribel_20261029.json'),
  'utf8')).payload;
let purchaseNumber = 0;
const purchase = async ({ phone, email, name, funnel = 'att1', type = 'PURCHASE_APPROVED', transaction,
  tenant = 'lancemos' }) => {
  purchaseNumber += 1;
  const payload = structuredClone(capture);
  payload.event = type;
  payload.data.buyer.checkout_phone = phone;
  payload.data.buyer.email = email;
  payload.data.buyer.name = name;
  payload.data.purchase.transaction = transaction ?? `HP${String(purchaseNumber).padStart(10, '0')}`;
  const event = one(await sql(`insert into public.webhook_events (source, external_event_id, event_type, payload)
      values ('hotmart', $1, $2, $3::jsonb) returning id`,
  [`evento-${purchaseNumber}`, type, JSON.stringify(payload)]), 'evento').id;
  if (type === 'PURCHASE_APPROVED') {
    await sql(`insert into public.portable_hotmart_purchase_correlations (webhook_event_id, tenant_ref, funnel_ref,
        binding_version, policy_max_lookback, outcome, candidate_count, reason_code, observed_at)
      values ($1, $2, $3, 1, interval '30 days', 'unmatched', 0, 'sin_intencion', now())`, [event, tenant, funnel]);
  }
  return payload.data.purchase.transaction;
};

// Ana: el contacto (con el telefono en la forma de WhatsApp en la identidad) y un formulario. Nombre y mail del
// contacto, en minusculas.
const ana = await contact({ name: ' Ana Contacto ', email: ' ANA@Example.com ', phone: '525511111111',
  whatsapp: '5215511111111' });
await form({ phone: '525511111111', email: 'ana.form@example.com', name: 'Ana Form', at: '2026-11-20T15:00:00Z' });
// Bea: solo dos formularios (sin contacto): nombre y mail del mas nuevo.
await form({ phone: '525522222222', email: 'bea.vieja@example.com', name: 'Bea Vieja', at: '2026-10-01T10:00:00Z' });
await form({ phone: '525522222222', email: 'bea@example.com', name: 'Bea Nueva', at: '2026-11-21T10:00:00Z' });
// Caro: solo una compra, con el telefono en la forma de WhatsApp.
await purchase({ phone: '5215533333333', email: 'Caro@Example.com', name: 'Caro Compra' });
// Una compra reembolsada sola: no entra.
const refunded = await purchase({ phone: '525544444444', email: 'reembolso@example.com', name: 'Reembolso' });
await purchase({ phone: '525544444444', email: 'reembolso@example.com', name: 'Reembolso', type: 'PURCHASE_REFUNDED',
  transaction: refunded });
// Dani: la baja por evento de Chatwoot (en la otra forma del telefono).
await contact({ name: 'Dani Baja', phone: '525555555555' });
await sql(`insert into public.contact_opt_out_events (contact_id, channel, purpose, source, canonical_account_id,
    canonical_inbox_id, canonical_conversation_id, canonical_message_id, external_user_id, occurred_at,
    normalized_rule_key, correlation_status)
  values (null, 'whatsapp', 'cart_recovery', 'chatwoot', 2, 11, 501, 601, '5215555555555', now(), 'baja', 'unmatched')`);
// Eli: la baja por autorizacion denegada vigente, con el telefono en contact_points.
const eli = await contact({ name: 'Eli Denegada', points: ['+52 55 6666 6666'] });
await sql(`insert into public.contact_authorizations (contact_id, channel, purpose, authorization_status,
    authorization_source, valid_from) values ($1, 'whatsapp', 'cart_recovery', 'denied', 'manual',
    now() - interval '1 day')`, [eli]);
// Otro tenant: no entra. Ana compro tambien otro producto del tenant.
await form({ phone: '525577777777', email: 'otro@example.com', name: 'Otro Tenant', at: '2026-11-20T10:00:00Z',
  tenant: 'otro' });
await purchase({ phone: '525511111111', email: 'ana.compra@example.com', name: 'Ana Compra', funnel: 'maribel' });

const read = async (after, limit) => service('read_contact_export_people_v1', INSTANCE, after, limit);
const all = await read(null, 1000);
const phones = all.people.map((p) => p.phone);
const byPhone = Object.fromEntries(all.people.map((p) => [p.phone, p]));
const keys = (o) => JSON.stringify(Object.keys(o).sort());
const shapeOk = keys(all) === keys(SHAPE)
  && all.people.every((p) => keys(p) === keys(SHAPE.people[0]))
  && all.people.every((p) => p.forms.every((f) => keys(f) === keys(SHAPE.people[0].forms[0])));
const A = byPhone['525511111111'];
const B = byPhone['525522222222'];
const C = byPhone['525533333333'];
if (!shapeOk
    || JSON.stringify(phones) !== JSON.stringify(['525511111111', '525522222222', '525533333333', '525555555555',
      '525566666666'])
    || all.next_after !== null
    || A.name !== 'Ana Contacto' || A.email !== 'ana@example.com' || A.opted_out
    || JSON.stringify(A.purchased_products) !== '["maribel"]' || A.forms.length !== 1 || A.forms[0].product !== 'att1'
    || B.name !== 'Bea Nueva' || B.email !== 'bea@example.com' || B.forms.length !== 2
    || new Date(B.forms[0].at) <= new Date(B.forms[1].at)
    || C.name !== 'Caro Compra' || C.email !== 'caro@example.com' || JSON.stringify(C.purchased_products) !== '["att1"]'
    || C.forms.length !== 0
    || !byPhone['525555555555'].opted_out || !byPhone['525566666666'].opted_out
    || byPhone['525566666666'].name !== 'Eli Denegada') {
  fail('las personas', all);
}
const p1 = await read(null, 2);
const p2 = await read(p1.next_after, 2);
const p3 = await read(p2.next_after, 2);
if (p1.next_after !== '525522222222' || p2.next_after !== '525555555555' || p3.next_after !== null
    || JSON.stringify([...p1.people, ...p2.people, ...p3.people].map((p) => p.phone)) !== JSON.stringify(phones)) {
  fail('el cursor', { p1, p2, p3 });
}
const invalid = {};
for (const [label, args] of Object.entries({ cero: [null, 0], tope: [null, 1001], cursor: ['abc', 2], nulo: [null, null] })) {
  invalid[label] = await errorOf(() => rpc('read_contact_export_people_v1', INSTANCE, ...args));
}
if (Object.values(invalid).some((e) => e !== 'contact_export_page_invalid')) fail('los limites', invalid);
results.personas = { personas: phones.length, bajas: all.people.filter((p) => p.opted_out).length, paginas: 3 };

// ---------------------------------------------------------------------------
// 2. El pedido.
// ---------------------------------------------------------------------------
const request = { filters: { compro: false }, columns: ['nombre', 'telefono', 'email'], matched_count: 2,
  requested_by: 'U0PIDEALAE1' };
const created = await service('create_contact_export_v1', INSTANCE, request);
if (!created.ok || !created.export_id) fail('crear', created);
const stored = one(await sql(`select extract(epoch from expires_at - created_at)::integer as span, requested_by, columns from public.contact_exports
  where id = $1`, [created.export_id]), 'pedido');
if (stored.span !== 1800 || stored.requested_by !== 'slack:U0PIDEALAE1'
    || JSON.stringify(stored.columns) !== '["nombre","telefono","email"]') {
  fail('el pedido guardado', stored);
}
const wrong = {};
for (const [label, value] of Object.entries({
  columna: { ...request, columns: ['direccion'] },
  repetida: { ...request, columns: ['telefono', 'telefono'] },
  clave: { ...request, phones: ['525511111111'] },
  conteo: { ...request, matched_count: 20001 },
  quien: { ...request, requested_by: 'nadie' },
  filtros: { ...request, filters: ['compro'] },
})) {
  wrong[label] = await errorOf(() => rpc('create_contact_export_v1', INSTANCE, value));
}
if (Object.values(wrong).some((e) => e !== 'contact_export_invalid')) fail('lo que no cierra', wrong);
const view = await service('read_contact_export_v1', INSTANCE, created.export_id);
if (view.state !== 'pendiente' || view.matched_count !== 2 || 'requested_by' in view || 'downloaded_by' in view) {
  fail('la vista del bridge', view);
}
const otherInstance = await errorOf(() => rpc('read_contact_export_v1', OTHER, created.export_id));
if (otherInstance !== 'contact_export_not_found') fail('otra instancia', otherInstance);
// El tope: 20 por instancia por hora (ya hay uno).
for (let i = 0; i < 19; i += 1) {
  const more = await service('create_contact_export_v1', INSTANCE, request);
  if (!more.ok) fail(`el pedido ${i + 2}`, more);
}
const limited = await service('create_contact_export_v1', INSTANCE, request);
const otherOk = await service('create_contact_export_v1', OTHER, request);
if (limited.ok || limited.reason_code !== 'contact_export_rate_limited' || !otherOk.ok) {
  fail('el tope por hora', { limited, otherOk });
}
results.pedido = { vence_en_minutos: 30, tope: 20 };

// ---------------------------------------------------------------------------
// 3. La pagina.
// ---------------------------------------------------------------------------
const id = created.export_id;
const pageView = await page('approver_read_contact_export_v1', INSTANCE, session, id);
const pagePeople = await page('approver_read_contact_export_people_v1', INSTANCE, session, id, null, 1000);
if (pageView.requested_by !== 'slack:U0PIDEALAE1' || pageView.state !== 'pendiente'
    || !pagePeople.ok || pagePeople.people.length !== 5) {
  fail('la pagina lee', { pageView, pagePeople });
}
const noSession = await errorAs('flow_change_approver',
  () => rpc('approver_read_contact_export_v1', INSTANCE, sha('otra'), id));
const crossed = await errorAs('flow_change_approver',
  () => rpc('approver_read_contact_export_v1', INSTANCE, session, otherOk.export_id));
if (noSession !== 'flow_approval_session_invalid' || crossed !== 'contact_export_not_found') {
  fail('sesion e instancia', { noSession, crossed });
}
const marked = await page('approver_mark_contact_export_downloaded_v1', INSTANCE, session, id, 2);
const again = await page('approver_mark_contact_export_downloaded_v1', INSTANCE, session, id, 2);
const afterPeople = await page('approver_read_contact_export_people_v1', INSTANCE, session, id, null, 1000);
const downloaded = await service('read_contact_export_v1', INSTANCE, id);
const by = one(await sql('select downloaded_by from public.contact_exports where id = $1', [id]), 'bajado');
if (!marked.ok || again.ok || again.reason_code !== 'contact_export_already_downloaded'
    || afterPeople.ok || afterPeople.reason_code !== 'contact_export_already_downloaded' || 'people' in afterPeople
    || downloaded.state !== 'bajado' || downloaded.downloaded_count !== 2 || by.downloaded_by !== `slack:${DAN}`) {
  fail('se baja una vez', { marked, again, afterPeople, downloaded, by });
}
// Vencido: otro pedido, corrido al pasado.
const old = (await service('create_contact_export_v1', OTHER, request)).export_id;
const mine = one(await sql(`insert into public.contact_exports (tenant_ref, ally_ref, filters, columns, matched_count,
    requested_by, created_at, expires_at) values ('lancemos', 'att1', '{}', array['telefono'], 0, 'slack:U0PIDEALAE1',
    now() - interval '1 hour', now() - interval '31 minutes') returning id`), 'vencido').id;
const expiredPeople = await page('approver_read_contact_export_people_v1', INSTANCE, session, mine, null, 10);
const expiredMark = await page('approver_mark_contact_export_downloaded_v1', INSTANCE, session, mine, 0);
const expiredView = await service('read_contact_export_v1', INSTANCE, mine);
const badCount = await errorAs('flow_change_approver',
  () => rpc('approver_mark_contact_export_downloaded_v1', INSTANCE, session, mine, -1));
const badExpiry = await errorOf(() => sql(`insert into public.contact_exports (tenant_ref, ally_ref, filters, columns,
    matched_count, requested_by, expires_at) values ('lancemos', 'att1', '{}', array['telefono'], 0,
    'slack:U0PIDEALAE1', now() + interval '31 minutes')`));
if (expiredPeople.ok || expiredPeople.reason_code !== 'contact_export_expired'
    || expiredMark.ok || expiredMark.reason_code !== 'contact_export_expired' || expiredView.state !== 'vencido'
    || badCount !== 'contact_export_count_invalid' || !badExpiry.includes('check constraint') || !old) {
  fail('vencido', { expiredPeople, expiredMark, expiredView, badCount, badExpiry });
}
results.pagina = { una_vez: true, vencido: true };

// ---------------------------------------------------------------------------
// 4. El rojo: sin la migracion no hay nada.
// ---------------------------------------------------------------------------
const missing = await errorOf(async () => {
  await db.exec('drop function public.read_contact_export_people_v1(jsonb,text,integer)');
  await rpc('read_contact_export_people_v1', INSTANCE, null, 10);
});
if (!missing.includes('does not exist')) fail('sin la migracion la funcion no existe', missing);
results.sin_la_migracion = 'no_existe';

await db.close();
console.log(`validate_contact_export: ok ${JSON.stringify(results)}`);
