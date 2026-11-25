// Donde se guardan la lista y cada envio del masivo manejado por el AE (migracion
// 20261125000200, encargo 3 del mapa «El masivo manejado por el Automation Expert»).
//
// Datos: una instancia de prueba (ATT1) con su binding y una persona que entra por
// «Entrar con Slack» como en la pagina; las filas de la lista son las que deja
// bulk_import.read_bulk_list (telefono en forma de envio, primer nombre, mail),
// sinteticas y con digitos repetidos.
//
// Prueba:
//   0. la forma: cuatro tablas con RLS y sin acceso para anon, authenticated ni
//      service_role; las funciones del bridge solo de service_role, las de la
//      pagina solo de flow_change_approver, las internas de nadie; no cambia
//      ninguna otra ni su ACL, y correrla dos veces aborta;
//   1. la lista: se crea desde filas normalizadas atada a un borrador, guarda la
//      huella del mail y no el mail, y un telefono repetido, sin el aviso
//      declarado o por encima del tope no entra;
//   2. la lectura: conteos y cinco ejemplos enmascarados, nunca un telefono;
//   3. la confirmacion: un aprobador que no esta (sin sesion, revocada, de otra
//      instancia) no puede; sin los vetos no se confirma; el numero equivocado, sin
//      «Confirmo», en amarillo sin la segunda marca o en rojo no confirman; el
//      numero exacto si, y queda quien confirmo;
//   4. el registro idempotente: solo se reserva con el envio en curso; lo
//      reservado, enviado, dudoso y vetado no se vuelve a reservar; un reservado
//      sin cierre pasa a dudoso y no se reintenta; un fallido solo con el
//      reintento pedido; no termina con reservas abiertas;
//   5. el freno a mano desde la pagina: un frenado no se reserva mas;
//   6. la purga: a los N dias quedan la huella y los conteos, sin telefono ni
//      nombre; espera a un envio en curso y frena al borrador de la lista purgada;
//   7. sin la migracion la funcion no existe (el rojo).
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const fail = (message, detail) => {
  console.error(`validate_bulk_send_store: ${message}`, detail === undefined ? '' : JSON.stringify(detail));
  process.exit(1);
};
const MIGRATION = '20261125000200_bulk_send_store_v1.sql';
const BRIDGE = [
  'create_bulk_send_v1(jsonb,jsonb)',
  'apply_bulk_list_vetoes_v1(jsonb,uuid,jsonb)',
  'transition_bulk_send_v1(jsonb,uuid,text,text)',
  'reserve_bulk_send_recipient_v1(uuid,integer,boolean)',
  'close_bulk_send_recipient_v1(uuid,integer,jsonb)',
  'expire_bulk_send_reservations_v1(uuid,integer)',
  'read_bulk_send_batch_v1(uuid,integer)',
  'read_bulk_send_v1(jsonb,uuid)',
  'read_bulk_sends_v1(jsonb,integer)',
  'purge_bulk_list_phones_v1(timestamp with time zone)',
];
const PAGE = [
  'approver_create_bulk_list_v1(jsonb,text,uuid,jsonb)',
  'approver_read_bulk_send_v1(jsonb,text,uuid)',
  'approver_confirm_bulk_send_v1(jsonb,text,uuid,integer,boolean,text,boolean)',
  'approver_brake_bulk_send_v1(jsonb,text,uuid)',
];
const PRIVATE = [
  '_bulk_send_fingerprint(text,text)',
  '_bulk_send_mask(text)',
  '_bulk_send_of(text,text,uuid)',
  '_bulk_send_view(uuid)',
];
const TABLES = ['bulk_sends', 'bulk_lists', 'bulk_list_members', 'bulk_send_recipients'];
const results = {};
const sha = (text) => createHash('sha256').update(text).digest('hex');

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// 20261125000300 (preparar y los vetos de la base) agrega sin cambiar lo que se prueba aca: la purga suma
// el borrado de lo preparado y el borrador acepta product_refs (validate_bulk_send_prepared_and_base_vetoes.mjs).
const KNOWN_LATER = ['20261125000300_bulk_send_prepared_and_base_vetoes_v1.sql'];
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
    && ['definition', 'acl', 'prosecdef'].some((k) => after.get(s)[k] !== before.get(s)[k]));
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
    if (fn.service_x || fn.page_x || fn.api_x) fail(`${signature}: la auxiliar no se expone`, fn);
  }
  const tables = (await db.query(`select c.relname, c.relrowsecurity,
      (select bool_or(has_table_privilege(r, c.oid, p))
         from unnest(array['anon', 'authenticated', 'service_role', 'flow_change_approver']) r,
              unnest(array['select', 'insert', 'update', 'delete']) p) as reachable
      from pg_class c where c.relnamespace = 'public'::regnamespace and c.relname = any($1)`, [TABLES])).rows;
  if (tables.length !== 4 || tables.some((t) => t.reachable || !t.relrowsecurity)) fail('las tablas, cerradas', tables);
  // RLS cerrada de verdad: anon y authenticated no leen ni escriben aunque lo intenten.
  for (const role of ['anon', 'authenticated']) {
    for (const statement of ['select count(*) from public.bulk_list_members',
      "insert into public.bulk_sends (tenant_ref, ally_ref, template_name, template_language, scheduled_at, created_by) values ('x', 'x', 'x', 'es', now(), 'automation_expert')"]) {
      await db.exec('begin');
      let outcome = 'paso';
      try {
        await db.exec(`set local role ${role}`);
        await db.query(statement);
      } catch (error) {
        outcome = error.message.includes('permission denied') ? 'denegado' : error.message;
      } finally {
        await db.exec('rollback');
      }
      if (outcome !== 'denegado') fail(`${role} llega a las tablas`, { statement, outcome });
    }
  }
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'bulk_send_store_already_applied') fail('la segunda corrida', error.message);
    // La migracion abre su propia transaccion y la deja abortada: se cierra para seguir en la misma base.
    await db.exec('rollback');
  }
  results.forma = { funciones: added.length, cambiadas: changed.length, tablas: tables.length };
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
const rolledBack = async (run) => {
  await db.exec('begin');
  try {
    return await run();
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
const otherSession = await enter(OTHER, 'masivo-otra');
const page = (fn, ...args) => as('flow_change_approver', () => rpc(fn, ...args));
// Un error con el rol puesto: el rollback devuelve el rol (un reset despues del error no correria).
const pageError = (fn, ...args) => errorOf(async () => {
  await db.exec('set role flow_change_approver');
  await rpc(fn, ...args);
});

const PHONES = ['5215511111111', '5215522222222', '5215533333333', '5215544444444', '5215555555555',
  '5215566666666', '5215577777777'];
const rows = PHONES.map((phone, i) => ({ phone, first_name: ['Ana', 'Bea', 'Caro', 'Dora', 'Eva', 'Fer', null][i],
  email: i === 0 ? '  Ana@Example.com ' : null }));
const listFor = (extra = {}) => ({ file_sha256: sha('lista.csv'), source_kind: 'inscritas_ghl',
  source_note: 'inscritas al vivo, con el aviso de GHL', notice_declared: true,
  rejected_counts: { sin_pais: 2, duplicado: 1 }, rows, ...extra });
const draft = async (extra = {}) => (await rpc('create_bulk_send_v1', INSTANCE, {
  template_name: 'maribel_vivo_recordatorio_01', template_language: 'es_MX',
  template_variables: [{ de: 'nombre' }, 'el vivo'], header_image_url: 'https://example.com/vivo.jpg',
  campaign_label: 'vivo-29-11', scheduled_at: '2026-11-29T23:00:00Z', ...extra })).send_id;

// ---------------------------------------------------------------------------
// 1. La lista.
// ---------------------------------------------------------------------------
const sendId = await draft();
const invalidDraft = await errorOf(() => rpc('create_bulk_send_v1', INSTANCE, {
  template_name: 'x', template_language: 'es_MX', scheduled_at: '2026-11-29T23:00:00Z', rate_millis: 9000 }));
const withoutNotice = await page('approver_create_bulk_list_v1', INSTANCE, session, sendId,
  listFor({ notice_declared: false }));
const duplicate = await pageError('approver_create_bulk_list_v1', INSTANCE, session, sendId,
  listFor({ rows: [rows[0], { ...rows[1], phone: rows[0].phone }] }));
const badPhone = await pageError('approver_create_bulk_list_v1', INSTANCE, session, sendId,
  listFor({ rows: [{ phone: '+52 55 1111 1111' }] }));
const overCap = await rolledBack(async () => {
  const small = await draft({ recipient_cap: 3 });
  return page('approver_create_bulk_list_v1', INSTANCE, session, small, listFor());
});
const created = await page('approver_create_bulk_list_v1', INSTANCE, session, sendId, listFor());
const again = await page('approver_create_bulk_list_v1', INSTANCE, session, sendId, listFor());
const stored = await sql(`select m.position, m.phone, m.first_name, m.phone_fingerprint, m.email_fingerprint,
    l.uploaded_by, l.member_count from public.bulk_list_members m join public.bulk_lists l on l.id = m.list_id
   order by m.position`);
const emailHash = sha('bulk-send:email:ana@example.com');
if (invalidDraft !== 'bulk_send_invalid' || withoutNotice.reason_code !== 'notice_not_declared'
    || duplicate !== 'bulk_list_duplicate_phone' || badPhone !== 'bulk_list_invalid'
    || overCap.reason_code !== 'over_cap' || !created.ok || created.member_count !== 7
    || again.reason_code !== 'list_already_attached' || stored.length !== 7
    || stored[0].email_fingerprint !== emailHash || stored[1].email_fingerprint !== null
    || stored[0].phone_fingerprint !== sha(`bulk-send:phone:${PHONES[0]}`)
    || stored[0].uploaded_by !== `slack:${DAN}`
    || JSON.stringify(await sql('select * from public.bulk_list_members')).toLowerCase().includes('example.com')) {
  fail('la lista', { invalidDraft, withoutNotice, duplicate, badPhone, overCap, created, again, stored });
}
results.lista = { personas: created.member_count, repetido: duplicate, sin_aviso: withoutNotice.reason_code,
  tope: overCap.reason_code };

// ---------------------------------------------------------------------------
// 2. La lectura: conteos y ejemplos enmascarados, sin telefonos.
// ---------------------------------------------------------------------------
const listId = created.list_id;
const beforeVetoes = await page('approver_confirm_bulk_send_v1', INSTANCE, session, sendId, 7, true, 'verde', false);
const vetoed = await rpc('apply_bulk_list_vetoes_v1', INSTANCE, listId,
  [{ position: 1, reason: 'baja' }, { position: 3, reason: 'compro' }]);
const unknownPosition = await errorOf(() => rpc('apply_bulk_list_vetoes_v1', INSTANCE, listId,
  [{ position: 99, reason: 'baja' }]));
const view = await page('approver_read_bulk_send_v1', INSTANCE, session, sendId);
const forAe = await rpc('read_bulk_send_v1', INSTANCE, sendId);
const listed = await rpc('read_bulk_sends_v1', INSTANCE, 10);
const shown = JSON.stringify([view, forAe, listed]);
if (beforeVetoes.reason_code !== 'list_not_vetted' || !vetoed.ok || vetoed.eligible !== 5
    || unknownPosition !== 'bulk_list_vetoes_unknown_position'
    || view.list.eligible_count !== 5 || view.list.excluded_by_reason.baja !== 1
    || view.list.excluded_by_reason.compro !== 1 || view.list.rejected_counts.sin_pais !== 2
    || view.list.examples.length !== 5 || view.list.examples[0].phone !== '…1111'
    || view.list.examples[0].initial !== 'A.' || view.list.examples.some((e) => e.phone === '…2222')
    || view.list.examples[4].initial !== null
    || PHONES.some((phone) => shown.includes(phone)) || shown.includes('Bea') || shown.includes('Ana')
    || JSON.stringify(forAe) !== JSON.stringify(view) || listed.length !== 1) {
  fail('la lectura', { beforeVetoes, vetoed, unknownPosition, view, listed });
}
results.lectura = { van: view.list.eligible_count, ejemplos: view.list.examples.map((e) => e.phone) };

// ---------------------------------------------------------------------------
// 3. La confirmacion.
// ---------------------------------------------------------------------------
const confirm = (count, confirmed = true, health = 'verde', ack = false, hash = session, instance = INSTANCE) =>
  as('flow_change_approver', () => rpc('approver_confirm_bulk_send_v1', instance, hash, sendId, count, confirmed,
    health, ack));
const confirmError = (hash, instance = INSTANCE) => pageError('approver_confirm_bulk_send_v1', instance, hash, sendId,
  5, true, 'verde', false);
const noSession = await confirmError(sha('no-es-una-sesion'));
const otherInstance = await confirmError(otherSession);
const otherInstanceSend = await confirmError(otherSession, OTHER);
const revoked = await errorOf(async () => {
  await sql('update public.flow_approval_sessions set revoked_at = clock_timestamp() where session_hash = $1',
    [session]);
  await db.exec('set role flow_change_approver');
  await rpc('approver_confirm_bulk_send_v1', INSTANCE, session, sendId, 5, true, 'verde', false);
});
const asService = await errorOf(async () => {
  await db.exec('set role service_role');
  await rpc('approver_confirm_bulk_send_v1', INSTANCE, session, sendId, 5, true, 'verde', false);
});
const wrongCount = await confirm(7);
const offByOne = await confirm(4);
const noMark = await confirm(5, false);
const red = await confirm(5, true, 'rojo', true);
const yellowNoAck = await confirm(5, true, 'amarillo', false);
const reserveTooEarly = await rpc('reserve_bulk_send_recipient_v1', sendId, 0, false);
const ok = await confirm(5, true, 'amarillo', true);
const twice = await confirm(5);
const vetoAfter = await rpc('apply_bulk_list_vetoes_v1', INSTANCE, listId, []);
const confirmed = one(await sql(`select state, confirmed_by, confirmed_count, confirmed_health from public.bulk_sends
   where id = $1`, [sendId]), 'confirmado');
if (noSession !== 'flow_approval_session_invalid' || otherInstance !== 'flow_approval_session_invalid'
    || otherInstanceSend !== 'bulk_send_not_found' || revoked !== 'flow_approval_session_invalid'
    || !asService.includes('permission denied')
    || wrongCount.reason_code !== 'count_mismatch' || wrongCount.eligible_count !== 5
    || offByOne.reason_code !== 'count_mismatch' || noMark.reason_code !== 'confirmation_missing'
    || red.reason_code !== 'health_blocks' || yellowNoAck.reason_code !== 'yellow_ack_missing'
    || reserveTooEarly.reason_code !== 'not_sending' || !ok.ok || ok.confirmed_count !== 5
    || twice.reason_code !== 'not_waiting_for_confirmation' || vetoAfter.reason_code !== 'not_a_draft'
    || confirmed.state !== 'confirmado' || confirmed.confirmed_by !== `slack:${DAN}`
    || confirmed.confirmed_count !== 5 || confirmed.confirmed_health !== 'amarillo') {
  fail('la confirmacion', { noSession, otherInstance, otherInstanceSend, revoked, asService, wrongCount, offByOne,
    noMark, red, yellowNoAck, reserveTooEarly, ok, twice, vetoAfter, confirmed });
}
results.confirmacion = { sin_sesion: noSession, revocada: revoked, numero_equivocado: wrongCount.reason_code,
  sin_marca: noMark.reason_code, rojo: red.reason_code, amarillo_sin_marca: yellowNoAck.reason_code, ok: ok.ok };

// ---------------------------------------------------------------------------
// 4. El registro idempotente.
// ---------------------------------------------------------------------------
const step = (to, reason = null) => rpc('transition_bulk_send_v1', INSTANCE, sendId, to, reason);
const skip = await step('terminado');
const preparing = await step('preparando');
const sending = await step('enviando');
const batch = await rpc('read_bulk_send_batch_v1', sendId, 10);
const excluded = await rpc('reserve_bulk_send_recipient_v1', sendId, 1, false);
const first = await rpc('reserve_bulk_send_recipient_v1', sendId, 0, false);
const firstAgain = await rpc('reserve_bulk_send_recipient_v1', sendId, 0, true);
const closeSent = await rpc('close_bulk_send_recipient_v1', sendId, 0,
  { state: 'enviado', chatwoot_conversation_id: 9001, chatwoot_message_id: 70001 });
const closeTwice = await rpc('close_bulk_send_recipient_v1', sendId, 0, { state: 'fallido', reason: 'api_500' });
const sentAgain = await rpc('reserve_bulk_send_recipient_v1', sendId, 0, true);
const sentWithoutMessage = await errorOf(async () => {
  await rpc('reserve_bulk_send_recipient_v1', sendId, 6, false);
  await rpc('close_bulk_send_recipient_v1', sendId, 6, { state: 'enviado' });
});
// La persona 2 queda reservada y el bridge se cae antes de cerrar.
await rpc('reserve_bulk_send_recipient_v1', sendId, 2, false);
const cannotFinish = await step('terminado');
const freshNotExpired = await rpc('expire_bulk_send_reservations_v1', sendId, 5);
await sql(`update public.bulk_send_recipients set reserved_at = reserved_at - interval '10 minutes'
  where send_id = $1 and position = 2`, [sendId]);
const expired = await rpc('expire_bulk_send_reservations_v1', sendId, 5);
const doubtfulAgain = await rpc('reserve_bulk_send_recipient_v1', sendId, 2, true);
const closeDoubtful = await rpc('close_bulk_send_recipient_v1', sendId, 2,
  { state: 'enviado', chatwoot_message_id: 70002 });
// La persona 4 falla antes de mandar: sin pedirlo no vuelve; con el reintento pedido, si.
await rpc('reserve_bulk_send_recipient_v1', sendId, 4, false);
await rpc('close_bulk_send_recipient_v1', sendId, 4, { state: 'fallido', reason: 'chatwoot_api_502' });
const failedNoRetry = await rpc('reserve_bulk_send_recipient_v1', sendId, 4, false);
const failedRetry = await rpc('reserve_bulk_send_recipient_v1', sendId, 4, true);
await rpc('close_bulk_send_recipient_v1', sendId, 4,
  { state: 'enviado', chatwoot_conversation_id: 9004, chatwoot_message_id: 70004, meta_error_code: null });
// La persona 5 escribio al equipo entre la vista previa y su tanda: vetada al mandar.
await rpc('reserve_bulk_send_recipient_v1', sendId, 5, false);
await rpc('close_bulk_send_recipient_v1', sendId, 5, { state: 'vetado', reason: 'con_equipo' });
const vetoedAgain = await rpc('reserve_bulk_send_recipient_v1', sendId, 5, true);
const neverReserved = await rpc('close_bulk_send_recipient_v1', sendId, 3, { state: 'enviado', chatwoot_message_id: 1 });
const batchAfter = await rpc('read_bulk_send_batch_v1', sendId, 10);
const ledger = Object.fromEntries((await sql(`select position, state, attempts, reason from public.bulk_send_recipients
  where send_id = $1 order by position`, [sendId])).map((r) => [r.position, `${r.state}/${r.attempts}/${r.reason ?? ''}`]));
if (skip.reason_code !== 'transition_not_allowed' || !preparing.ok || !sending.ok
    || JSON.stringify(batch.map((b) => b.position)) !== '[0,2,4,5,6]' || batch[0].phone !== PHONES[0]
    || excluded.reason_code !== 'member_excluded' || !first.ok || first.phone !== PHONES[0]
    || firstAgain.reason_code !== 'already_reservado' || !closeSent.ok
    || closeTwice.reason_code !== 'already_enviado' || sentAgain.reason_code !== 'already_enviado'
    || sentWithoutMessage !== 'bulk_send_close_invalid'
    || cannotFinish.reason_code !== 'reservations_open' || freshNotExpired.dudosos !== 0 || expired.dudosos !== 1
    || doubtfulAgain.reason_code !== 'already_dudoso' || closeDoubtful.reason_code !== 'already_dudoso'
    || failedNoRetry.reason_code !== 'already_fallido' || !failedRetry.ok || failedRetry.attempt !== 2
    || vetoedAgain.reason_code !== 'already_vetado' || neverReserved.reason_code !== 'not_reserved'
    || JSON.stringify(batchAfter.map((b) => b.position)) !== '[6]'
    || JSON.stringify(ledger) !== JSON.stringify({ 0: 'enviado/1/', 2: 'dudoso/1/reservado_sin_cierre',
      4: 'enviado/2/', 5: 'vetado/1/con_equipo' })) {
  fail('el registro idempotente', { skip, preparing, sending, batch, excluded, first, firstAgain, closeSent,
    closeTwice, sentAgain, sentWithoutMessage, cannotFinish, freshNotExpired, expired, doubtfulAgain,
    closeDoubtful, failedNoRetry, failedRetry, vetoedAgain, neverReserved, batchAfter, ledger });
}
results.registro = ledger;

// ---------------------------------------------------------------------------
// 5. El freno a mano desde la pagina.
// ---------------------------------------------------------------------------
const braked = await rolledBack(async () => {
  const brake = await page('approver_brake_bulk_send_v1', INSTANCE, session, sendId);
  const after = await rpc('reserve_bulk_send_recipient_v1', sendId, 6, false);
  const row = one(await sql('select state, brake_reason, braked_by from public.bulk_sends where id = $1', [sendId]), 'f');
  const again = await page('approver_brake_bulk_send_v1', INSTANCE, session, sendId);
  return { brake, after, row, again };
});
const byMotor = await rolledBack(async () => step('frenado', 'meta_131048'));
const noReason = await errorOf(() => step('frenado'));
if (!braked.brake.ok || braked.after.reason_code !== 'not_sending' || braked.row.brake_reason !== 'a_mano'
    || braked.row.braked_by !== `slack:${DAN}` || braked.again.reason_code !== 'not_brakeable'
    || !byMotor.ok || noReason !== 'bulk_send_transition_invalid') {
  fail('el freno', { braked, byMotor, noReason });
}
results.freno = { a_mano: braked.row.brake_reason, despues: braked.after.reason_code };

// ---------------------------------------------------------------------------
// 6. La purga: a los N dias quedan la huella y los conteos.
// ---------------------------------------------------------------------------
// Un segundo envio, en borrador, con su lista: se purga y queda frenado.
const second = await draft({ template_name: 'maribel_vivo_en_vivo_01' });
const secondList = await page('approver_create_bulk_list_v1', INSTANCE, session, second,
  listFor({ rows: rows.slice(0, 2), retention_days: 30 }));
const in29 = await inMinutes(29 * 24 * 60);
const in31 = await inMinutes(31 * 24 * 60);
const early = await rpc('purge_bulk_list_phones_v1', in29);
const waitsForSending = await rpc('purge_bulk_list_phones_v1', in31);
const firstStill = one(await sql('select count(*)::int as n from public.bulk_list_members where list_id = $1 and phone is not null',
  [listId]), 'n').n;
await rpc('reserve_bulk_send_recipient_v1', sendId, 6, false);
await rpc('close_bulk_send_recipient_v1', sendId, 6, { state: 'enviado', chatwoot_message_id: 70006 });
const finished = await step('terminado');
const purged = await rpc('purge_bulk_list_phones_v1', in31);
const purgedAgain = await rpc('purge_bulk_list_phones_v1', in31);
const leftovers = await sql(`select count(*) filter (where phone is not null or first_name is not null)::int as datos,
    count(*) filter (where phone_fingerprint is not null)::int as huellas,
    count(*) filter (where exclusion_reason is not null)::int as excluidas
  from public.bulk_list_members`);
const afterPurge = await page('approver_read_bulk_send_v1', INSTANCE, session, sendId);
const secondSend = one(await sql('select state, brake_reason, braked_by from public.bulk_sends where id = $1', [second]),
  'segundo');
const secondPage = await page('approver_read_bulk_send_v1', INSTANCE, session, second);
if (!secondList.ok || early.lists !== 0 || waitsForSending.lists !== 1 || firstStill !== 7 || !finished.ok
    || purged.lists !== 1 || purged.members !== 7 || purgedAgain.lists !== 0
    || leftovers[0].datos !== 0 || leftovers[0].huellas !== 9 || leftovers[0].excluidas !== 2
    || afterPurge.list.state !== 'purgada' || afterPurge.list.member_count !== 7
    || afterPurge.list.rejected_counts.duplicado !== 1 || afterPurge.list.examples.length !== 0
    || afterPurge.recipients_by_state.enviado !== 3 || afterPurge.recipients_by_state.dudoso !== 1
    || secondSend.state !== 'frenado' || secondSend.brake_reason !== 'lista_purgada' || secondSend.braked_by !== 'purga'
    || secondPage.list.state !== 'purgada') {
  fail('la purga', { secondList, early, waitsForSending, firstStill, finished, purged, purgedAgain, leftovers,
    afterPurge, secondSend });
}
results.purga = { listas: purged.lists + waitsForSending.lists, personas: leftovers[0].huellas,
  despues: afterPurge.recipients_by_state };

// ---------------------------------------------------------------------------
// 7. El rojo: sin la migracion no hay registro.
// ---------------------------------------------------------------------------
const missing = await rolledBack(async () => {
  await db.exec('drop function public.reserve_bulk_send_recipient_v1(uuid,integer,boolean)');
  return errorOf(() => rpc('reserve_bulk_send_recipient_v1', sendId, 0, false));
});
if (!missing.includes('does not exist')) fail('sin la migracion la funcion no existe', missing);
results.sin_la_migracion = 'no_existe';

await db.close();
console.log(`validate_bulk_send_store: ok ${JSON.stringify(results)}`);
