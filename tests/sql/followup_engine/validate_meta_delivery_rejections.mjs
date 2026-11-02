// El rechazo de Meta registrado y el unico reintento del primer contacto con
// 131049 (migracion 20261103000500, E-284, P-12a). Contrato:
// docs/contracts/meta-delivery-rejections-v1.md.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera, el manifiesto y el carrito capturados). Cada A1 sale por el camino
// real (plan, reserva, arranque, aceptacion de Chatwoot) con la politica de
// tres toques: despues de A1 queda A2 pendiente. El id de Chatwoot que el
// bridge guarda en remote_message_id es un numero sintetico (en el bridge es
// str(result.message_id)). La compra es la capturada de la bienvenida de ATT1
// con la persona sustituida.
//
// Prueba:
//   0. la forma: agrega exactamente sus seis funciones, una tabla y una vista,
//      no cambia ninguna otra funcion; solo service_role las ejecuta; la tabla
//      y la vista sin permisos para la API; correrla dos veces aborta (55000).
//      Antes de la migracion nada existe (el rojo de todo lo demas);
//   1. registrar un 131049 del A1: queda atado al intento y al caso, el caso
//      y el intento no cambian; repetir da replayed; antes de 24 h list_due no
//      lo trae y claim da too_early sin escribir; a las 25 h list_due lo trae,
//      claim lo reserva una sola vez, settle lo cierra; el rechazo del
//      reintento se registra y nunca vuelve a list_due; la vista lo cuenta por
//      plantilla;
//   2. las barreras de la base: compra, demasiado tarde, caso cerrado y otro
//      codigo; un freno deja blocked con el motivo y no se vuelve a evaluar;
//   3. 131026 con el cierre prendido: el caso pasa a unreachable, A2 se
//      cancela y la secuencia tambien, y pide el aviso al equipo una vez; con
//      el cierre apagado solo se registra;
//   4. entradas invalidas: 22023.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261103000500_meta_delivery_rejections_v1.sql';
const RECORD = 'record_meta_delivery_rejection_v1(bigint,bigint,bigint,bigint,text,text,text,timestamp with time zone,bigint,boolean,timestamp with time zone)';
const FUNCTIONS = [
  RECORD,
  'list_due_meta_first_touch_retries_v1(bigint,integer,integer,timestamp with time zone)',
  'claim_meta_first_touch_retry_v1(bigint,bigint,text,text,integer,timestamp with time zone)',
  'settle_meta_first_touch_retry_v1(uuid,text,bigint,text,timestamp with time zone)',
  'mark_meta_delivery_rejection_notified_v1(uuid,timestamp with time zone)',
  'block_meta_first_touch_retry_v1(uuid,text,timestamp with time zone)',
];
const results = {};
const fixture = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));

// ---------------------------------------------------------------------------
// 0. La forma.
// ---------------------------------------------------------------------------
const names = readdirSync(join(root, 'supabase/migrations')).filter((name) => name.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
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
  const tableBefore = one((await db.query(`select to_regclass('public.meta_delivery_rejections')::text as t`)).rows, 'tabla').t;
  if (tableBefore !== null) fail('la tabla existe antes de la migracion');
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const added = [...after.keys()].filter((s) => !before.has(s)).map((s) => s.replace(/^public\./, ''));
  const changed = [...before.keys()].filter((s) => !after.has(s)
    || after.get(s).definition !== before.get(s).definition || after.get(s).acl !== before.get(s).acl);
  if (!sameSet(added, FUNCTIONS) || changed.length !== 0) fail('la migracion cambio otra cosa', { added, changed });
  for (const signature of FUNCTIONS) {
    const acl = one((await db.query(`
      select has_function_privilege('service_role', $1, 'EXECUTE') as service,
             has_function_privilege('anon', $1, 'EXECUTE') as anon,
             has_function_privilege('authenticated', $1, 'EXECUTE') as authenticated,
             p.prosecdef
      from pg_proc p where p.oid = $1::regprocedure
    `, [`public.${signature}`])).rows, signature);
    if (!acl.service || acl.anon || acl.authenticated || !acl.prosecdef) fail(`permisos de ${signature}`, acl);
  }
  for (const relation of ['public.meta_delivery_rejections', 'public.meta_delivery_rejection_counts_v1']) {
    const acl = one((await db.query(`
      select bool_or(has_table_privilege(r, $1, 'SELECT')) as any_select
      from unnest(array['anon', 'authenticated', 'service_role']) r
    `, [relation])).rows, relation);
    if (acl.any_select) fail(`${relation} tiene permisos para la API`);
  }
  let error = null;
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  } catch (caught) {
    error = caught;
  }
  await db.exec('rollback').catch(() => {});
  if (error?.code !== '55000' || error.message !== 'meta_delivery_rejections_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  results.forma = { funciones: added.length, segunda_corrida: error.message };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, dbNow, cartFor, planCart, reserve, startOld, stepOf, caseOf } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const hours = (base, h) => new Date(base.getTime() + h * 3_600_000);
let serial = 0;
// A1 por el camino real, aceptado por Chatwoot con el id de mensaje que guarda el bridge.
const sentA1 = async (label) => {
  const lead = await cartFor(label);
  const plan = await planCart(lead);
  if (!plan.scheduled_action_id) fail(`${label}: el carrito no planifico`, plan);
  const reserved = await reserve(lead, plan.scheduled_action_id);
  const started = await startOld(reserved);
  if (started.phase !== 'request_started') fail(`${label}: A1 no arranco`, started);
  serial += 1;
  lead.messageId = 880_000 + serial;
  const accepted = one(await sql('select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)',
    [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, lead.chatwoot, String(lead.messageId),
      `Texto sembrado ${serial}`, await dbNow()]), `${label} aceptacion`);
  if (accepted.status !== 'accepted_by_chatwoot') fail(`${label} aceptacion`, accepted);
  lead.attempt = reserved.attempt.id;
  lead.sentAt = await dbNow();
  return lead;
};
const record = async (lead, { code = '131049', kind = 'first_touch', template = 'att1_carrito_abandonado_01',
  messageId = lead.messageId, retryOf = null, close = false, now = null } = {}) => one(await sql(
  'select * from public.record_meta_delivery_rejection_v1($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)',
  [A.accountId, A.inboxId, Number(lead.chatwoot), messageId, kind, code, template, lead.sentAt, retryOf, close,
    now ?? await dbNow()]), `${lead.label} registro`);
const due = async (now, maxAge = 259_200) => sql(
  'select * from public.list_due_meta_first_touch_retries_v1($1,$2,$3,$4)', [A.inboxId, maxAge, 50, now]);
const claim = async (lead, now, { maxAge = 259_200, phone = lead.phone, email = lead.email } = {}) => one(await sql(
  'select * from public.claim_meta_first_touch_retry_v1($1,$2,$3,$4,$5,$6)',
  [A.inboxId, lead.messageId, phone, email, maxAge, now]), `${lead.label} reserva`);
const rowOf = async (lead, messageId = lead.messageId) => one(await sql(
  'select * from public.meta_delivery_rejections where chatwoot_inbox_id = $1 and chatwoot_message_id = $2',
  [A.inboxId, messageId]), `${lead.label} fila`);

// ---------------------------------------------------------------------------
// 1. Registrar y reintentar una vez.
// ---------------------------------------------------------------------------
{
  const lead = await sentA1('reintento');
  const caseBefore = await caseOf(lead);
  const attemptBefore = one(await sql('select outcome, phase from public.followup_delivery_attempts where id = $1',
    [lead.attempt]), 'intento');
  const first = await record(lead);
  if (first.outcome !== 'recorded' || first.retry_status !== 'none' || first.needs_team_notice) fail('registrar', first);
  const row = await rowOf(lead);
  if (row.delivery_attempt_id !== lead.attempt || row.recovery_case_id !== lead.plan.recovery_case_id
      || row.meta_error_code !== '131049' || row.template_name !== 'att1_carrito_abandonado_01') {
    fail('la fila no quedo atada al intento y al caso', row);
  }
  const caseAfter = await caseOf(lead);
  const attemptAfter = one(await sql('select outcome, phase from public.followup_delivery_attempts where id = $1',
    [lead.attempt]), 'intento');
  if (caseAfter.status !== caseBefore.status || caseAfter.version !== caseBefore.version
      || JSON.stringify(attemptAfter) !== JSON.stringify(attemptBefore)) fail('registrar cambio el caso o el intento');
  const replay = await record(lead, { template: null });
  if (replay.outcome !== 'replayed' || replay.rejection_id !== first.rejection_id) fail('repetir', replay);
  // Antes de 24 h.
  const early = hours(lead.sentAt, 23);
  if ((await due(early)).length !== 0) fail('list_due trajo uno de menos de 24 h');
  const tooEarly = await claim(lead, early);
  if (tooEarly.outcome !== 'too_early' || (await rowOf(lead)).retry_status !== 'none') fail('too_early', tooEarly);
  // A las 25 h: una sola vez.
  const later = hours(lead.sentAt, 25);
  const listed = await due(later);
  if (listed.length !== 1 || Number(listed[0].chatwoot_message_id) !== lead.messageId) fail('list_due a las 25 h', listed);
  const claimed = await claim(lead, later);
  if (claimed.outcome !== 'claimed') fail('reserva', claimed);
  const again = await claim(lead, later);
  if (again.outcome !== 'already_attempted') fail('la segunda reserva', again);
  if ((await due(later)).length !== 0) fail('list_due trajo uno reservado');
  const settled = one(await sql('select * from public.settle_meta_first_touch_retry_v1($1,$2,$3,$4,$5)',
    [claimed.rejection_id, 'sent', 990_001, null, later]), 'settle');
  if (settled.outcome !== 'settled' || (await rowOf(lead)).retry_status !== 'sent') fail('settle', settled);
  const resettle = one(await sql('select * from public.settle_meta_first_touch_retry_v1($1,$2,$3,$4,$5)',
    [claimed.rejection_id, 'failed', null, 'chatwoot_http_error', later]), 'settle otra vez');
  if (resettle.outcome !== 'not_found') fail('settle de una fila cerrada', resettle);
  // El reintento rechazado otra vez: se registra y nunca se reintenta.
  const retried = await record(lead, { kind: 'first_touch_retry', messageId: 990_001, retryOf: lead.messageId });
  if (retried.outcome !== 'recorded') fail('registrar el rechazo del reintento', retried);
  if ((await due(hours(lead.sentAt, 50))).some((item) => Number(item.chatwoot_message_id) === 990_001)) {
    fail('list_due trajo un reintento');
  }
  const counts = one(await sql(`select * from public.meta_delivery_rejection_counts_v1
    where template_name = 'att1_carrito_abandonado_01' and touch_kind = 'first_touch'`), 'vista');
  if (Number(counts.rejected) !== 1 || Number(counts.retried) !== 1 || Number(counts.retry_rejected_again) !== 1) {
    fail('la vista', counts);
  }
  results.reintento = { registro: first.outcome, temprano: tooEarly.outcome, reserva: claimed.outcome,
    segunda: again.outcome, cierre: settled.outcome };
}

// ---------------------------------------------------------------------------
// 2. Las barreras de la base.
// ---------------------------------------------------------------------------
{
  const blocked = {};
  // La compra: la capturada de la bienvenida de ATT1, con la persona sustituida.
  const buyer = await sentA1('compra');
  await record(buyer);
  const purchase = structuredClone(fixture('hotmart_purchase_approved_att1_bienvenida_20261026.json').payload);
  purchase.id = `meta-retry-purchase-${buyer.email}`;
  purchase.data.buyer = { ...purchase.data.buyer, email: buyer.email, checkout_phone: buyer.phone, phone: buyer.phone };
  await sql(`insert into public.webhook_events (source, external_event_id, event_type, payload, received_at)
    values ('hotmart', $1, 'PURCHASE_APPROVED', $2::jsonb, now())`, [purchase.id, JSON.stringify(purchase)]);
  blocked.compra = (await claim(buyer, hours(buyer.sentAt, 25))).outcome;
  const buyerRow = await rowOf(buyer);
  if (blocked.compra !== 'purchase_already_approved' || buyerRow.retry_status !== 'blocked'
      || buyerRow.retry_reason !== 'purchase_already_approved') fail('la compra', { blocked, buyerRow });
  if ((await claim(buyer, hours(buyer.sentAt, 26))).outcome !== 'already_attempted') fail('un freno se volvio a evaluar');
  // Demasiado tarde.
  const late = await sentA1('tarde');
  await record(late);
  blocked.tarde = (await claim(late, hours(late.sentAt, 80))).outcome;
  if (blocked.tarde !== 'blocked_too_late') fail('demasiado tarde', blocked.tarde);
  // El caso cerrado por otra cosa.
  const closed = await sentA1('cerrado');
  await record(closed);
  await sql(`update public.recovery_cases set status = 'cancelled', closed_at = now(), version = version + 1
    where id = $1`, [closed.plan.recovery_case_id]);
  blocked.cerrado = (await claim(closed, hours(closed.sentAt, 25))).outcome;
  if (blocked.cerrado !== 'blocked_case_closed') fail('el caso cerrado', blocked.cerrado);
  // Otro codigo: no se reintenta.
  const other = await sentA1('otro-codigo');
  await record(other, { code: '130472' });
  blocked.otro = (await claim(other, hours(other.sentAt, 25))).outcome;
  if (blocked.otro !== 'not_eligible' || (await rowOf(other)).retry_status !== 'none') fail('otro codigo', blocked.otro);
  if ((await due(hours(other.sentAt, 25))).some((item) => Number(item.chatwoot_message_id) === other.messageId)) {
    fail('list_due trajo otro codigo');
  }
  // Lo que vio el bridge en Chatwoot (la persona escribio): blocked con su motivo, una sola vez.
  const wrote = await sentA1('escribio');
  const wroteRow = await record(wrote);
  const bridgeBlock = one(await sql('select * from public.block_meta_first_touch_retry_v1($1,$2,$3)',
    [wroteRow.rejection_id, 'bridge_lead_wrote', await dbNow()]), 'freno del bridge');
  const bridgeAgain = one(await sql('select * from public.block_meta_first_touch_retry_v1($1,$2,$3)',
    [wroteRow.rejection_id, 'bridge_lead_wrote', await dbNow()]), 'freno del bridge otra vez');
  if (bridgeBlock.outcome !== 'blocked' || bridgeAgain.outcome !== 'not_found'
      || (await rowOf(wrote)).retry_reason !== 'bridge_lead_wrote'
      || (await due(hours(wrote.sentAt, 25))).some((item) => Number(item.chatwoot_message_id) === wrote.messageId)) {
    fail('el freno del bridge', { bridgeBlock, bridgeAgain });
  }
  await expectError(db, 'motivo del bridge', () => sql('select * from public.block_meta_first_touch_retry_v1($1,$2,$3)',
    [wroteRow.rejection_id, 'lead_wrote', null]), { code: '22023', message: 'block_meta_first_touch_retry_invalid_input' });
  blocked.bridge = bridgeBlock.outcome;
  results.barreras = blocked;
}

// ---------------------------------------------------------------------------
// 3. 131026: el numero no recibe WhatsApp.
// ---------------------------------------------------------------------------
{
  const lead = await sentA1('sin-whatsapp');
  const a2 = await stepOf(lead, 'cart_second_touch');
  if (a2.status !== 'pending') fail('A2 no quedo pendiente', a2.status);
  const recorded = await record(lead, { code: '131026', close: true });
  if (recorded.unreachable_outcome !== 'case_closed' || !recorded.needs_team_notice) fail('131026 con cierre', recorded);
  const closedCase = await caseOf(lead);
  const a2After = one(await sql('select status, terminal_reason from public.scheduled_actions where id = $1', [a2.id]), 'A2');
  const sequences = await sql('select status from public.followup_sequences where recovery_case_id = $1',
    [lead.plan.recovery_case_id]);
  if (closedCase.status !== 'unreachable' || a2After.status !== 'cancelled'
      || a2After.terminal_reason !== 'meta_131026_unreachable' || sequences.some((s) => s.status === 'active')) {
    fail('el caso no se cerro', { closedCase: closedCase.status, a2After, sequences });
  }
  if ((await claim(lead, hours(lead.sentAt, 25))).outcome !== 'not_eligible') fail('131026 se reservo');
  const marked = one(await sql('select * from public.mark_meta_delivery_rejection_notified_v1($1)',
    [recorded.rejection_id]), 'marca');
  const remarked = one(await sql('select * from public.mark_meta_delivery_rejection_notified_v1($1)',
    [recorded.rejection_id]), 'marca otra vez');
  const replay = await record(lead, { code: '131026', close: true });
  if (marked.outcome !== 'marked' || remarked.outcome !== 'replayed' || replay.needs_team_notice) {
    fail('el aviso', { marked, remarked, replay });
  }
  // Con el cierre apagado: solo se registra.
  const off = await sentA1('sin-whatsapp-apagado');
  const offRecorded = await record(off, { code: '131026', close: false });
  if (offRecorded.unreachable_outcome !== null || offRecorded.needs_team_notice
      || (await caseOf(off)).status === 'unreachable'
      || (await stepOf(off, 'cart_second_touch')).status !== 'pending') fail('131026 sin cierre', offRecorded);
  results.sin_whatsapp = { cierre: recorded.unreachable_outcome, aviso: marked.outcome, apagado: 'solo_registro' };
}

// ---------------------------------------------------------------------------
// 4. Entradas invalidas.
// ---------------------------------------------------------------------------
{
  const now = await dbNow();
  await expectError(db, 'tipo de toque', () => sql(
    'select * from public.record_meta_delivery_rejection_v1($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)',
    [A.accountId, A.inboxId, 1, 1, 'otro', '131049', null, now, null, false, now]),
  { code: '22023', message: 'record_meta_delivery_rejection_invalid_input' });
  await expectError(db, 'reintento sin original', () => sql(
    'select * from public.record_meta_delivery_rejection_v1($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)',
    [A.accountId, A.inboxId, 1, 2, 'first_touch_retry', '131049', null, now, null, false, now]),
  { code: '22023', message: 'record_meta_delivery_rejection_invalid_input' });
  await expectError(db, 'codigo con texto', () => sql(
    'select * from public.record_meta_delivery_rejection_v1($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)',
    [A.accountId, A.inboxId, 1, 3, 'first_touch', '131049: texto', null, now, null, false, now]),
  { code: '22023', message: 'record_meta_delivery_rejection_invalid_input' });
  await expectError(db, 'techo', () => sql('select * from public.list_due_meta_first_touch_retries_v1($1,$2,$3,$4)',
    [A.inboxId, 3_600, 10, now]), { code: '22023', message: 'list_due_meta_first_touch_retries_invalid_input' });
  await expectError(db, 'telefono', () => sql('select * from public.claim_meta_first_touch_retry_v1($1,$2,$3,$4,$5,$6)',
    [A.inboxId, 1, '+5215500000000', null, 259_200, now]),
  { code: '22023', message: 'claim_meta_first_touch_retry_invalid_input' });
  results.entradas = 'ok';
}

console.log(`meta_delivery_rejections=${JSON.stringify(results)}`);
