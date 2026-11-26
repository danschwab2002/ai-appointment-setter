// Lo que le falta a la base del masivo para preparar y para los vetos de la base
// (migracion 20261125000300, encargo 3b del mapa «El masivo manejado por el
// Automation Expert»; la usan el #505, preparar, y el #506, los vetos).
//
// Datos: la instancia de prueba (ATT1) y la persona que entra por «Entrar con
// Slack», como en validate_bulk_send_store.mjs; filas de lista sinteticas con
// digitos repetidos. Las compras salen del PURCHASE_APPROVED capturado de Maribel
// (tests/fixtures/hotmart_purchase_approved_maribel_20261029.json, ya anonimizado),
// con el telefono, el mail y la transaccion sinteticos de cada persona.
//
// Prueba:
//   0. la forma: una tabla con RLS y sin acceso para nadie; las cuatro funciones
//      nuevas del bridge solo de service_role y la auxiliar de nadie; solo cambian
//      create_bulk_send_v1, _bulk_send_view y la purga, con la misma ACL; correrla
//      dos veces aborta;
//   1. el producto del envio: el borrador lo guarda y la vista lo muestra; vacio,
//      repetido o fuera de forma no entra;
//   2. preparar: la lista con cursor por posicion (sin excluidas, aunque ya esten
//      preparadas o reservadas); anotar solo con el envio en preparando, la persona
//      sin exclusion y la etiqueta del envio; lo anotado no se pisa; leer lo
//      anotado por posiciones; la tanda del motor no cambia por preparar;
//   3. la purga borra lo anotado del envio de la lista purgada;
//   4. los vetos de la base: baja por evento (en la otra forma del telefono) y por
//      autorizacion denegada vigente; compro por telefono y por la huella del mail;
//      no cuentan otro producto, una compra reembolsada ni una denegacion vencida;
//      el producto sale del envio, el parametro tiene que coincidir, sin producto
//      no se lee, y otra instancia no ve la lista;
//   5. sin la migracion la funcion no existe (el rojo).
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const fail = (message, detail) => {
  console.error(`validate_bulk_send_prepared_and_base_vetoes: ${message}`,
    detail === undefined ? '' : JSON.stringify(detail));
  process.exit(1);
};
const MIGRATION = '20261125000300_bulk_send_prepared_and_base_vetoes_v1.sql';
const BRIDGE = [
  'record_bulk_send_prepared_v1(uuid,integer,jsonb)',
  'read_bulk_send_prepared_v1(uuid,integer[])',
  'read_bulk_send_members_v1(uuid,integer,integer)',
  'read_bulk_list_base_vetoes_v1(jsonb,uuid,text[])',
];
const PRIVATE = ['_bulk_send_purchases(text)'];
const CHANGED = ['_bulk_send_view(uuid)', 'create_bulk_send_v1(jsonb,jsonb)',
  'purge_bulk_list_phones_v1(timestamp with time zone)'];
const results = {};
const sha = (text) => createHash('sha256').update(text).digest('hex');

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// El registro por persona para el reporte (D10 del #511): solo lee, no cambia nada de lo que se prueba aca.
const KNOWN_LATER = ['20261125000400_bulk_send_recipients_read_v1.sql', '20261126500000_agent_campaign_judge_runs_v1.sql'];
for (const name of names.slice(position + 1)) {
  if (!KNOWN_LATER.includes(name) && readFileSync(join(root, 'supabase/migrations', name), 'utf8').includes('bulk_')) {
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
    && ['definition', 'acl', 'prosecdef'].some((k) => after.get(s)[k] !== before.get(s)[k])).sort();
  const aclChanged = CHANGED.filter((s) => ['acl', 'prosecdef', 'service_x', 'page_x', 'api_x']
    .some((k) => after.get(s)[k] !== before.get(s)[k]));
  const added = [...after.keys()].filter((s) => !before.has(s)).sort();
  if (JSON.stringify(changed) !== JSON.stringify([...CHANGED].sort()) || aclChanged.length
      || JSON.stringify(added) !== JSON.stringify([...BRIDGE, ...PRIVATE].sort())) {
    fail('la migracion cambio otra cosa', { changed, aclChanged, added });
  }
  for (const signature of BRIDGE) {
    const fn = after.get(signature);
    if (!fn.service_x || fn.page_x || fn.api_x || !fn.prosecdef) fail(`${signature}: solo service_role`, fn);
  }
  for (const signature of PRIVATE) {
    const fn = after.get(signature);
    if (fn.service_x || fn.page_x || fn.api_x) fail(`${signature}: la auxiliar no se expone`, fn);
  }
  const table = (await db.query(`select c.relrowsecurity,
      (select bool_or(has_table_privilege(r, c.oid, p))
         from unnest(array['anon', 'authenticated', 'service_role', 'flow_change_approver']) r,
              unnest(array['select', 'insert', 'update', 'delete']) p) as reachable
      from pg_class c where c.relnamespace = 'public'::regnamespace and c.relname = 'bulk_send_prepared'`)).rows;
  if (table.length !== 1 || table[0].reachable || !table[0].relrowsecurity) fail('la tabla, cerrada', table);
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'bulk_send_prepared_already_applied') fail('la segunda corrida', error.message);
    await db.exec('rollback');
  }
  results.forma = { nuevas: added.length, redefinidas: changed.length };
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
const OTHER = { tenant_ref: 'lancemos', ally_ref: 'maribel', funnel_ref: 'maribel' };
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
const enter = async (instance, label) => {
  const session = sha(`${label}-session`);
  await as('flow_change_approver', async () => {
    const state = sha(`${label}-state`);
    await rpc('begin_flow_approval_oidc_v1', instance, state, sha(`${label}-nonce`), '/flow-approval/',
      await inMinutes(10));
    await rpc('consume_flow_approval_oidc_v1', instance, state);
    const created = await rpc('create_flow_approval_session_v1', instance, state, session, TEAM, DAN,
      await inMinutes(60));
    if (!created.ok) fail('entrar', created);
  });
  return session;
};
const session = await enter(INSTANCE, 'masivo');
const page = (fn, ...args) => as('flow_change_approver', () => rpc(fn, ...args));

const PHONES = ['5215511111111', '5215522222222', '5215533333333', '5215544444444', '5215555555555',
  '5215566666666', '5215577777777'];
const EMAIL = (i) => `persona${i}@example.com`;
const rows = PHONES.map((phone, i) => ({ phone, first_name: `Persona${i}`, email: i === 3 ? ` ${EMAIL(3).toUpperCase()} ` : null }));
const listFor = (extra = {}) => ({ file_sha256: sha('lista.csv'), source_kind: 'inscritas_ghl',
  source_note: 'inscritas al vivo', notice_declared: true, rejected_counts: {}, rows, ...extra });
const draft = async (extra = {}) => (await rpc('create_bulk_send_v1', INSTANCE, {
  template_name: 'att1_vivo_recordatorio_01', template_language: 'es_MX',
  template_variables: [{ de: 'nombre' }], campaign_label: 'vivo-29-11',
  scheduled_at: '2026-11-29T23:00:00Z', product_refs: ['att1'], ...extra })).send_id;

// ---------------------------------------------------------------------------
// 1. El producto del envio.
// ---------------------------------------------------------------------------
const sendId = await draft();
const badProducts = {};
for (const [label, value] of Object.entries({ vacio: [], repetido: ['att1', 'att1'], forma: ['Att 1'],
  numero: [5], texto: 'att1' })) {
  badProducts[label] = await errorOf(() => draft({ product_refs: value }));
}
const withoutProducts = await draft({ product_refs: undefined });
const view = await rpc('read_bulk_send_v1', INSTANCE, sendId);
const viewWithout = await rpc('read_bulk_send_v1', INSTANCE, withoutProducts);
if (Object.values(badProducts).some((e) => e !== 'bulk_send_invalid')
    || JSON.stringify(view.product_refs) !== '["att1"]' || viewWithout.product_refs !== null) {
  fail('el producto del envio', { badProducts, view: view.product_refs, viewWithout: viewWithout.product_refs });
}
results.producto = { guardado: view.product_refs, invalidos: Object.keys(badProducts).length };

// ---------------------------------------------------------------------------
// 2. Preparar.
// ---------------------------------------------------------------------------
const created = await page('approver_create_bulk_list_v1', INSTANCE, session, sendId, listFor());
const listId = created.list_id;
await rpc('apply_bulk_list_vetoes_v1', INSTANCE, listId, [{ position: 1, reason: 'baja' }]);
const ok = await page('approver_confirm_bulk_send_v1', INSTANCE, session, sendId, 6, true, 'verde', false);
const prepared = (pos, extra = {}) => ({ contact_id: 700 + pos, conversation_id: 9000 + pos,
  source_id: PHONES[pos], label: 'vivo-29-11', how: 'nuevo', ...extra });
const tooEarly = await rpc('record_bulk_send_prepared_v1', sendId, 0, prepared(0));
const toPreparing = await rpc('transition_bulk_send_v1', INSTANCE, sendId, 'preparando', null);
const pageOne = await rpc('read_bulk_send_members_v1', sendId, -1, 3);
const pageTwo = await rpc('read_bulk_send_members_v1', sendId, pageOne[pageOne.length - 1].position, 3);
const pageEnd = await rpc('read_bulk_send_members_v1', sendId, 6, 3);
const badCursor = await errorOf(() => rpc('read_bulk_send_members_v1', sendId, -2, 3));
const badLimit = await errorOf(() => rpc('read_bulk_send_members_v1', sendId, -1, 501));
const first = await rpc('record_bulk_send_prepared_v1', sendId, 0, prepared(0));
const firstAgain = await rpc('record_bulk_send_prepared_v1', sendId, 0, prepared(0, { conversation_id: 1 }));
await rpc('record_bulk_send_prepared_v1', sendId, 2, prepared(2, { how: 'reusa' }));
const excluded = await rpc('record_bulk_send_prepared_v1', sendId, 1, prepared(1));
const wrongLabel = await errorOf(() => rpc('record_bulk_send_prepared_v1', sendId, 3, prepared(3, { label: 'otra' })));
const noLabel = await errorOf(() => rpc('record_bulk_send_prepared_v1', sendId, 3, prepared(3, { label: null })));
const badHow = await errorOf(() => rpc('record_bulk_send_prepared_v1', sendId, 3, prepared(3, { how: 'magia' })));
const badSource = await errorOf(() => rpc('record_bulk_send_prepared_v1', sendId, 3,
  prepared(3, { source_id: '+52 55 4444 4444' })));
const badContact = await errorOf(() => rpc('record_bulk_send_prepared_v1', sendId, 3, prepared(3, { contact_id: '7' })));
const unknownPosition = await errorOf(() => rpc('record_bulk_send_prepared_v1', sendId, 99, prepared(3)));
const read = await rpc('read_bulk_send_prepared_v1', sendId, [0, 1, 2, 3]);
const readNone = await rpc('read_bulk_send_prepared_v1', sendId, []);
const readNull = await errorOf(() => rpc('read_bulk_send_prepared_v1', sendId, [0, null]));
const readUnknown = await errorOf(() => rpc('read_bulk_send_prepared_v1', '00000000-0000-4000-8000-000000000000', [0]));
const toSending = await rpc('transition_bulk_send_v1', INSTANCE, sendId, 'enviando', null);
const lateRecord = await rpc('record_bulk_send_prepared_v1', sendId, 3, prepared(3));
const batch = await rpc('read_bulk_send_batch_v1', sendId, 10);
await rpc('reserve_bulk_send_recipient_v1', sendId, 0, false);
const cursorAfterReserve = await rpc('read_bulk_send_members_v1', sendId, -1, 2);
const positions = (list) => JSON.stringify(list.map((m) => m.position));
if (!created.ok || !ok.ok || tooEarly.reason_code !== 'not_preparing' || !toPreparing.ok
    || positions(pageOne) !== '[0,2,3]' || pageOne[0].phone !== PHONES[0] || pageOne[0].first_name !== 'Persona0'
    || positions(pageTwo) !== '[4,5,6]' || pageEnd.length !== 0
    || badCursor !== 'bulk_send_read_invalid' || badLimit !== 'bulk_send_read_invalid'
    || !first.ok || firstAgain.reason_code !== 'already_prepared' || firstAgain.conversation_id !== 9000
    || excluded.reason_code !== 'member_excluded' || wrongLabel !== 'bulk_send_prepared_label_mismatch'
    || noLabel !== 'bulk_send_prepared_label_mismatch' || badHow !== 'bulk_send_prepared_invalid'
    || badSource !== 'bulk_send_prepared_invalid' || badContact !== 'bulk_send_prepared_invalid'
    || unknownPosition !== 'bulk_list_member_not_found'
    || positions(read) !== '[0,2]' || read[0].contact_id !== 700 || read[0].conversation_id !== 9000
    || read[0].source_id !== PHONES[0] || read[0].label !== 'vivo-29-11' || read[0].how !== 'nuevo'
    || read[1].how !== 'reusa' || !read[0].prepared_at || readNone.length !== 0
    || readNull !== 'bulk_send_read_invalid' || readUnknown !== 'bulk_send_not_found'
    || !toSending.ok || lateRecord.reason_code !== 'not_preparing'
    || positions(batch) !== '[0,2,3,4,5,6]' || positions(cursorAfterReserve) !== '[0,2]') {
  fail('preparar', { created, ok, tooEarly, toPreparing, pageOne, pageTwo, pageEnd, badCursor, badLimit, first,
    firstAgain, excluded, wrongLabel, noLabel, badHow, badSource, badContact, unknownPosition, read, readNone,
    readNull, readUnknown, toSending, lateRecord, batch, cursorAfterReserve });
}
results.preparar = { tandas: [positions(pageOne), positions(pageTwo)], anotadas: read.length,
  repetida: firstAgain.reason_code, excluida: excluded.reason_code };

// ---------------------------------------------------------------------------
// 3. La purga borra lo anotado.
// ---------------------------------------------------------------------------
await rpc('close_bulk_send_recipient_v1', sendId, 0, { state: 'enviado', chatwoot_message_id: 70000 });
await rpc('transition_bulk_send_v1', INSTANCE, sendId, 'frenado', 'prueba');
const purged = await rpc('purge_bulk_list_phones_v1', await inMinutes(31 * 24 * 60));
const leftPrepared = one(await sql('select count(*)::int as n from public.bulk_send_prepared'), 'n').n;
if (purged.lists !== 1 || purged.prepared !== 2 || purged.members !== 7 || leftPrepared !== 0) {
  fail('la purga', { purged, leftPrepared });
}
results.purga = purged;

// ---------------------------------------------------------------------------
// 4. Los vetos de la base.
// ---------------------------------------------------------------------------
const capture = JSON.parse(readFileSync(join(root, 'tests/fixtures/hotmart_purchase_approved_maribel_20261029.json'),
  'utf8')).payload;
let purchaseNumber = 0;
const purchase = async ({ phone, email, funnel = 'att1', type = 'PURCHASE_APPROVED', transaction }) => {
  purchaseNumber += 1;
  const payload = structuredClone(capture);
  payload.event = type;
  payload.data.buyer.checkout_phone = phone;
  payload.data.buyer.email = email;
  payload.data.purchase.transaction = transaction ?? `HP${String(purchaseNumber).padStart(10, '0')}`;
  const event = one(await sql(`insert into public.webhook_events (source, external_event_id, event_type, payload)
    values ('hotmart', $1, $2, $3::jsonb) returning id`, [`evento-${purchaseNumber}`, type, JSON.stringify(payload)]),
  'evento').id;
  if (type === 'PURCHASE_APPROVED') {
    await sql(`insert into public.portable_hotmart_purchase_correlations (webhook_event_id, tenant_ref, funnel_ref,
        binding_version, policy_max_lookback, outcome, candidate_count, reason_code, observed_at)
      values ($1, 'lancemos', $2, 1, interval '30 days', 'unmatched', 0, 'sin_intencion', now())`, [event, funnel]);
  }
  return payload.data.purchase.transaction;
};
const contactWith = async (phone) => {
  const contact = one(await sql(`insert into public.contacts (full_name) values ('Persona de prueba') returning id`),
    'contacto').id;
  await sql(`insert into public.channel_identities (contact_id, channel, account_id, external_user_id)
    values ($1, 'whatsapp', 'cuenta', $2)`, [contact, phone]);
  return contact;
};
const deny = async (contact, until = null) => sql(`insert into public.contact_authorizations (contact_id, channel,
    purpose, authorization_status, authorization_source, valid_from, valid_until)
  values ($1, 'whatsapp', 'cart_recovery', 'denied', 'manual', now() - interval '2 days', $2)`, [contact, until]);
// 0: pidio la baja por Chatwoot; el evento trae la forma sin el 1 de Mexico. Ademas compro.
await sql(`insert into public.contact_opt_out_events (contact_id, channel, purpose, source, canonical_account_id,
    canonical_inbox_id, canonical_conversation_id, canonical_message_id, external_user_id, occurred_at,
    normalized_rule_key, correlation_status)
  values (null, 'whatsapp', 'cart_recovery', 'chatwoot', 2, 11, 501, 601, '525511111111', now(), 'baja', 'unmatched')`);
await purchase({ phone: '525511111111', email: 'otra0@example.com' });
// 1: autorizacion de WhatsApp denegada vigente.
await deny(await contactWith('5215522222222'));
// 2: compro el producto por el telefono.
await purchase({ phone: '525533333333', email: 'otra2@example.com' });
// 3: compro con otro telefono; la misma persona por el mail (la lista lo tiene en mayusculas y con espacios).
await purchase({ phone: '525599999999', email: EMAIL(3) });
// 4: compro otro producto.
await purchase({ phone: '525555555555', email: 'otra4@example.com', funnel: 'maribel' });
// 5: compro y le reembolsaron.
const refunded = await purchase({ phone: '525566666666', email: 'otra5@example.com' });
await purchase({ phone: '525566666666', email: 'otra5@example.com', type: 'PURCHASE_REFUNDED', transaction: refunded });
// 6: una denegacion que ya vencio.
await deny(await contactWith('525577777777'), new Date(Date.now() - 86_400_000).toISOString());

const vetoSend = await draft();
const vetoList = (await page('approver_create_bulk_list_v1', INSTANCE, session, vetoSend, listFor())).list_id;
const asService = (fn, ...args) => as('service_role', () => rpc(fn, ...args));
const fromColumn = await asService('read_bulk_list_base_vetoes_v1', INSTANCE, vetoList, null);
const fromEmpty = await asService('read_bulk_list_base_vetoes_v1', INSTANCE, vetoList, []);
const sameParam = await asService('read_bulk_list_base_vetoes_v1', INSTANCE, vetoList, ['att1']);
const twoArgs = JSON.parse(one(await sql(`select public.read_bulk_list_base_vetoes_v1($1::jsonb, $2)::text as r`,
  [JSON.stringify(INSTANCE), vetoList]), 'dos').r);
const mismatch = await errorOf(() => rpc('read_bulk_list_base_vetoes_v1', INSTANCE, vetoList, ['att1', 'maribel']));
const badParam = await errorOf(() => rpc('read_bulk_list_base_vetoes_v1', INSTANCE, vetoList, ['Att 1']));
const otherInstance = await errorOf(() => rpc('read_bulk_list_base_vetoes_v1', OTHER, vetoList, ['att1']));
// Un envio sin producto: sin el parametro no se lee; con el parametro, se lee con ese producto.
const bare = await draft({ product_refs: undefined, template_name: 'att1_vivo_sin_producto_01' });
const bareList = (await page('approver_create_bulk_list_v1', INSTANCE, session, bare, listFor())).list_id;
const bareMissing = await errorOf(() => rpc('read_bulk_list_base_vetoes_v1', INSTANCE, bareList, null));
const bareOther = await rpc('read_bulk_list_base_vetoes_v1', INSTANCE, bareList, ['maribel']);
const flat = (list) => list.map((v) => `${v.position}:${v.reason}`).join(',');
const expected = '0:baja,0:compro,1:baja,2:compro,3:compro';
const shown = JSON.stringify(fromColumn);
if (flat(fromColumn) !== expected || flat(fromEmpty) !== expected
    || flat(sameParam) !== expected || flat(twoArgs) !== expected
    || mismatch !== 'bulk_send_products_mismatch' || badParam !== 'bulk_list_vetoes_invalid'
    || otherInstance !== 'bulk_list_not_found' || bareMissing !== 'bulk_send_products_missing'
    || flat(bareOther) !== '0:baja,1:baja,4:compro'
    || PHONES.some((phone) => shown.includes(phone)) || shown.includes('example.com')) {
  fail('los vetos de la base', { fromColumn, fromEmpty, sameParam, twoArgs, mismatch, badParam, otherInstance,
    bareMissing, bareOther });
}
// Purgada la lista, ya no hay a quien vetar.
await rpc('transition_bulk_send_v1', INSTANCE, vetoSend, 'frenado', 'prueba');
await rpc('purge_bulk_list_phones_v1', await inMinutes(31 * 24 * 60));
const afterPurge = await rpc('read_bulk_list_base_vetoes_v1', INSTANCE, vetoList, null);
if (afterPurge.length !== 0) fail('purgada no veta', afterPurge);
results.vetos = flat(fromColumn);

// ---------------------------------------------------------------------------
// 5. El rojo: sin la migracion no hay lectura.
// ---------------------------------------------------------------------------
const missing = await errorOf(async () => {
  await db.exec('drop function public.read_bulk_list_base_vetoes_v1(jsonb,uuid,text[])');
  await rpc('read_bulk_list_base_vetoes_v1', INSTANCE, vetoList, null);
});
if (!missing.includes('does not exist')) fail('sin la migracion la funcion no existe', missing);
results.sin_la_migracion = 'no_existe';

await db.close();
console.log(`validate_bulk_send_prepared_and_base_vetoes: ok ${JSON.stringify(results)}`);
