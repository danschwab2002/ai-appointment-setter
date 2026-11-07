// Un rechazo de la cuenta no gasta el toque (migracion 20261107000100, E-348,
// P-33; decisiones de Dan del 2026-11-07). Contrato:
// docs/contracts/meta-delivery-rejections-v1.md, «Cuando la cuenta no entrega».
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs, como
// validate_meta_delivery_rejections.mjs: cada A1 sale por el camino real y
// Chatwoot lo acepta con un id sintetico. El codigo es el 131042 capturado en
// Maribel (tests/fixtures/chatwoot_db_messages_inbox_13_maribel_131042_20261107.json).
//
// Prueba (las secciones 0b, 2b, 2c y 2d son de la revision del PR #432):
//   0. la forma: agrega exactamente sus ocho funciones y dos tablas, no cambia
//      ninguna otra funcion; solo service_role las ejecuta; la tabla sin
//      permisos para la API; correrla dos veces aborta (55000);
//   1. la espera: un 131042 registrado queda waiting; sin senal, o con una
//      senal anterior al ultimo rechazo de la cuenta, no se libera y claim
//      contesta not_released; con una plantilla entregada despues se libera,
//      sale una vez, y si el reintento vuelve rechazado por la cuenta la fila
//      vuelve a esperar, hasta 3 intentos;
//   0b. la marca de prendido: sin marca no entra nada; lo anterior a la marca
//      no entra aunque se registre como recorded; un replayed posterior si;
//   2. las barreras de la base: compra y caso cerrado; 2b. la cuenta vuelve a
//      caer con filas liberadas (vuelven a esperar sin gastar intentos);
//      2c. la misma persona en dos conversaciones y un contacto posterior;
//      2d. conversacion tomada, derivacion sin atender, contacto sin permiso
//      y baja;
//   3. lo que no entra: el 131049, el 131026, un seguimiento, lo vencido; el
//      reintento del 131049 no ve las filas de la cuenta;
//   4. el freno del bridge y las entradas invalidas (22023).
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261107000100_meta_account_hold_retry_v1.sql';
const FUNCTIONS = [
  'mark_meta_account_hold_enabled_v1(bigint,timestamp with time zone,timestamp with time zone,integer)',
  'hold_meta_account_rejection_v1(uuid,integer,timestamp with time zone)',
  'release_meta_account_holds_v1(bigint,text[],timestamp with time zone,timestamp with time zone)',
  'list_due_meta_account_hold_retries_v1(bigint,integer,timestamp with time zone)',
  'claim_meta_account_hold_retry_v1(bigint,bigint,text,text,text[],timestamp with time zone)',
  'settle_meta_account_hold_retry_v1(uuid,text,bigint,text,timestamp with time zone)',
  'block_meta_account_hold_retry_v1(uuid,text,timestamp with time zone)',
  'requeue_meta_account_hold_v1(bigint,bigint,bigint,timestamp with time zone)',
];
const results = {};
const fixture = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));
const capture = fixture('chatwoot_db_messages_inbox_13_maribel_131042_20261107.json');
const CODE = capture.rechazados_131042[0].content_attributes.external_error.match(/^(\d+)/)[1];
if (CODE !== '131042') fail('el codigo capturado no es 131042', CODE);
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
  const tableBefore = one((await db.query(`select to_regclass('public.meta_account_holds')::text as t`)).rows, 'tabla').t;
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
  for (const relation of ['public.meta_account_holds', 'public.meta_account_hold_activations']) {
    const tableAcl = one((await db.query(`
      select bool_or(has_table_privilege(r, $1, 'SELECT')) as any_select
      from unnest(array['anon', 'authenticated', 'service_role']) r
    `, [relation])).rows, relation);
    if (tableAcl.any_select) fail(`${relation} tiene permisos para la API`);
  }
  let error = null;
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  } catch (caught) {
    error = caught;
  }
  await db.exec('rollback').catch(() => {});
  if (error?.code !== '55000' || error.message !== 'meta_account_holds_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  results.forma = { funciones: added.length, segunda_corrida: error.message };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, dbNow, cartFor, planCart, reserve, startOld, caseOf } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const hours = (base, h) => new Date(base.getTime() + h * 3_600_000);
let serial = 0;
const sentA1 = async (label) => {
  const lead = await cartFor(label);
  const plan = await planCart(lead);
  if (!plan.scheduled_action_id) fail(`${label}: el carrito no planifico`, plan);
  const reserved = await reserve(lead, plan.scheduled_action_id);
  const started = await startOld(reserved);
  if (started.phase !== 'request_started') fail(`${label}: A1 no arranco`, started);
  serial += 1;
  lead.messageId = 770_000 + serial;
  const accepted = one(await sql('select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)',
    [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, lead.chatwoot, String(lead.messageId),
      `Texto sembrado ${serial}`, await dbNow()]), `${label} aceptacion`);
  if (accepted.status !== 'accepted_by_chatwoot') fail(`${label} aceptacion`, accepted);
  lead.sentAt = await dbNow();
  return lead;
};
const record = async (lead, { code = CODE, kind = 'first_touch', messageId = lead.messageId, retryOf = null,
  at = lead.sentAt } = {}) => one(await sql(
  'select * from public.record_meta_delivery_rejection_v1($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)',
  [A.accountId, A.inboxId, Number(lead.chatwoot), messageId, kind, code, 'att1_carrito_abandonado_01', at, retryOf,
    false, at]), `${lead.label} registro`);
const hold = async (rejectionId, now, maxAge = 259_200) => one(await sql(
  'select * from public.hold_meta_account_rejection_v1($1,$2,$3)', [rejectionId, maxAge, now]), 'espera');
const release = async (okAt, now, codes = [CODE]) => one(await sql(
  'select * from public.release_meta_account_holds_v1($1,$2,$3,$4)', [A.inboxId, codes, okAt, now]), 'liberar');
const due = async (now) => sql('select * from public.list_due_meta_account_hold_retries_v1($1,$2,$3)',
  [A.inboxId, 50, now]);
const claim = async (lead, now, phone = lead.phone, codes = [CODE]) => one(await sql(
  'select * from public.claim_meta_account_hold_retry_v1($1,$2,$3,$4,$5,$6)',
  [A.inboxId, lead.messageId, phone, lead.email, codes, now]), `${lead.label} reserva`);
const settle = async (rejectionId, retryId, now) => one(await sql(
  'select * from public.settle_meta_account_hold_retry_v1($1,$2,$3,$4,$5)',
  [rejectionId, 'sent', retryId, null, now]), 'settle');
const requeue = async (lead, retryId, now) => one(await sql('select * from public.requeue_meta_account_hold_v1($1,$2,$3,$4)',
  [A.inboxId, lead.messageId, retryId, now]), 'volver a esperar');
const holdOf = async (lead) => one(await sql(
  'select * from public.meta_account_holds where chatwoot_inbox_id = $1 and chatwoot_message_id = $2',
  [A.inboxId, lead.messageId]), `${lead.label} espera`);

// ---------------------------------------------------------------------------
// 0b. La marca de prendido (H2 y H4 de la revision): sin marca no entra nada;
//     con la marca, solo lo que Meta rechazo despues, aunque se registre en el
//     primer barrido (que mira 72 h) o llegue como replayed.
// ---------------------------------------------------------------------------
// La marca y el latido: (desde cuando corre el proceso, ahora, hueco tolerado).
const mark = async (since, now, gap = 1_800) => one(await sql(
  'select * from public.mark_meta_account_hold_enabled_v1($1,$2,$3,$4)', [A.inboxId, since, now, gap]), 'marca');
{
  const early = await sentA1('cuenta-sin-marca');
  const early1 = await record(early);
  if ((await hold(early1.rejection_id, early.sentAt)).outcome !== 'not_enabled') fail('entro sin la marca');
  // Se prende ahora: lo que sigue en este archivo es posterior a la marca.
  const enabledAt = early.sentAt;
  const marked = await mark(enabledAt, enabledAt);
  // El latido del barrido siguiente (10 min) no mueve la marca.
  const again = await mark(enabledAt, hours(enabledAt, 1 / 6));
  if (marked.outcome !== 'marked' || again.outcome !== 'beat'
      || new Date(again.enabled_at).getTime() !== enabledAt.getTime()) fail('la marca', { marked, again });
  // El primer barrido registra (recorded) un 131042 de hace 2 h: retroactivo.
  const old = await sentA1('cuenta-anterior');
  const oldRow = await record(old, { at: hours(enabledAt, -2) });
  if (oldRow.outcome !== 'recorded') fail('registrar el viejo', oldRow);
  if ((await hold(oldRow.rejection_id, enabledAt)).outcome !== 'before_enabled') fail('entro uno anterior a la marca');
  if ((await sql('select 1 from public.meta_account_holds where rejection_id = $1', [oldRow.rejection_id])).length !== 0) {
    fail('quedo una fila anterior a la marca');
  }
  // Uno posterior a la marca que se registro y cuya espera fallo: el replayed entra.
  const late = await sentA1('cuenta-reintentada');
  await record(late);
  const replay = await record(late);
  if (replay.outcome !== 'replayed' || (await hold(replay.rejection_id, await dbNow())).outcome !== 'held') {
    fail('el replayed posterior no entro');
  }
  await sql(`update public.meta_account_holds set status = 'blocked', reason = 'test_cleanup', settled_at = now()
    where rejection_id = $1`, [replay.rejection_id]);
  results.marca = { sin_marca: 'not_enabled', anterior: 'before_enabled', replayed: 'held' };
}

// ---------------------------------------------------------------------------
// 1. La espera, la senal y los tres intentos.
// ---------------------------------------------------------------------------
{
  const lead = await sentA1('cuenta');
  const caseBefore = await caseOf(lead);
  const recorded = await record(lead);
  const held = await hold(recorded.rejection_id, hours(lead.sentAt, 0.1));
  if (held.outcome !== 'held') fail('poner en espera', held);
  if ((await hold(recorded.rejection_id, hours(lead.sentAt, 0.2))).outcome !== 'replayed') fail('repetir la espera');
  const row = await holdOf(lead);
  if (row.status !== 'waiting' || row.attempts !== 0 || row.meta_error_code !== CODE
      || new Date(row.expires_at).getTime() !== hours(lead.sentAt, 72).getTime()) fail('la fila en espera', row);
  // Sin senal: no se libera y no sale.
  const blind = await release(null, hours(lead.sentAt, 1));
  if (blind.released !== 0 || blind.waiting !== 1) fail('sin senal', blind);
  if ((await claim(lead, hours(lead.sentAt, 1))).outcome !== 'not_released') fail('salio sin senal');
  if ((await due(hours(lead.sentAt, 1))).length !== 0) fail('list_due trajo uno en espera');
  // Una plantilla entregada antes del ultimo rechazo de la cuenta no es la senal.
  const early = await release(hours(lead.sentAt, -0.5), hours(lead.sentAt, 1));
  if (early.released !== 0 || new Date(early.last_rejection_at).getTime() !== lead.sentAt.getTime()) {
    fail('senal anterior al rechazo', early);
  }
  // La plantilla entregada despues: se libera y sale una vez.
  const ok = hours(lead.sentAt, 26);
  const freed = await release(ok, hours(lead.sentAt, 26.2));
  if (freed.released !== 1 || freed.waiting !== 0) fail('liberar', freed);
  const listed = await due(hours(lead.sentAt, 26.2));
  if (listed.length !== 1 || Number(listed[0].chatwoot_message_id) !== lead.messageId) fail('list_due', listed);
  let claimed = await claim(lead, hours(lead.sentAt, 26.2));
  if (claimed.outcome !== 'claimed' || claimed.attempts !== 1) fail('reserva', claimed);
  if ((await claim(lead, hours(lead.sentAt, 26.2))).outcome !== 'not_released') fail('la segunda reserva');
  if ((await settle(claimed.rejection_id, 990_101, hours(lead.sentAt, 26.3))).outcome !== 'settled') fail('settle');
  // El caso no se toco.
  const caseAfter = await caseOf(lead);
  if (caseAfter.status !== caseBefore.status || caseAfter.version !== caseBefore.version) fail('el caso cambio');
  // El reintento vuelve rechazado por la cuenta: la fila vuelve a esperar.
  const requeuedAt = hours(lead.sentAt, 26.4);
  await record(lead, { kind: 'first_touch_retry', messageId: 990_101, retryOf: lead.messageId, at: requeuedAt });
  if ((await requeue(lead, 990_101, requeuedAt)).outcome !== 'requeued') fail('volver a esperar');
  if ((await requeue(lead, 990_101, requeuedAt)).outcome !== 'not_found') fail('volver a esperar dos veces');
  if ((await holdOf(lead)).status !== 'waiting') fail('la fila no volvio a esperar');
  // La senal vieja ya no alcanza: el ultimo rechazo de la cuenta es el del reintento.
  if ((await release(ok, hours(lead.sentAt, 27))).released !== 0) fail('la senal vieja libero');
  // Segundo y tercer intento, y el tercero rechazado agota la fila.
  for (const [attempt, retryId] of [[2, 990_102], [3, 990_103]]) {
    const signal = hours(lead.sentAt, 27 + attempt);
    if ((await release(signal, hours(lead.sentAt, 27.2 + attempt))).released !== 1) fail(`liberar ${attempt}`);
    claimed = await claim(lead, hours(lead.sentAt, 27.2 + attempt));
    if (claimed.outcome !== 'claimed' || claimed.attempts !== attempt) fail(`reserva ${attempt}`, claimed);
    await settle(claimed.rejection_id, retryId, hours(lead.sentAt, 27.3 + attempt));
    const back = hours(lead.sentAt, 27.4 + attempt);
    await record(lead, { kind: 'first_touch_retry', messageId: retryId, retryOf: lead.messageId, at: back });
    const outcome = (await requeue(lead, retryId, back)).outcome;
    if (outcome !== (attempt === 3 ? 'exhausted' : 'requeued')) fail(`intento ${attempt}`, outcome);
  }
  const exhausted = await holdOf(lead);
  if (exhausted.status !== 'blocked' || exhausted.reason !== 'hold_attempts_exhausted') fail('tres intentos', exhausted);
  results.espera = { espera: held.outcome, sin_senal: blind.released, liberadas: freed.released,
    reserva: 'claimed', intentos: exhausted.attempts, cierre: exhausted.reason };
}

// ---------------------------------------------------------------------------
// 2. Las barreras de la base.
// ---------------------------------------------------------------------------
{
  const blocked = {};
  const buyer = await sentA1('cuenta-compra');
  await hold((await record(buyer)).rejection_id, buyer.sentAt);
  const purchase = structuredClone(fixture('hotmart_purchase_approved_att1_bienvenida_20261026.json').payload);
  purchase.id = `meta-hold-purchase-${buyer.email}`;
  purchase.data.buyer = { ...purchase.data.buyer, email: buyer.email, checkout_phone: buyer.phone, phone: buyer.phone };
  await sql(`insert into public.webhook_events (source, external_event_id, event_type, payload, received_at)
    values ('hotmart', $1, 'PURCHASE_APPROVED', $2::jsonb, now())`, [purchase.id, JSON.stringify(purchase)]);
  const closed = await sentA1('cuenta-cerrado');
  await hold((await record(closed)).rejection_id, closed.sentAt);
  await sql(`update public.recovery_cases set status = 'cancelled', closed_at = now(), version = version + 1
    where id = $1`, [closed.plan.recovery_case_id]);
  // Despues de los reintentos rechazados de la seccion 1 (el ultimo rechazo de
  // la cuenta en la bandeja es de cualquier fila).
  const signal = hours(closed.sentAt, 40);
  const freed = await release(signal, hours(closed.sentAt, 40.1));
  if (freed.released !== 2) fail('liberar las dos', freed);
  blocked.compra = (await claim(buyer, hours(closed.sentAt, 40.1))).outcome;
  blocked.cerrado = (await claim(closed, hours(closed.sentAt, 40.1))).outcome;
  const buyerRow = await holdOf(buyer);
  if (blocked.compra !== 'purchase_already_approved' || buyerRow.status !== 'blocked'
      || buyerRow.reason !== 'purchase_already_approved') fail('la compra', { blocked, buyerRow });
  if (blocked.cerrado !== 'blocked_case_closed') fail('el caso cerrado', blocked);
  if ((await claim(buyer, hours(closed.sentAt, 41))).outcome !== 'not_released') fail('un freno se volvio a evaluar');
  results.barreras = blocked;
}

// ---------------------------------------------------------------------------
// 2b. La cuenta vuelve a caer con filas liberadas (H1 de la revision).
// ---------------------------------------------------------------------------
{
  const first = await sentA1('cuenta-recae-1');
  const second = await sentA1('cuenta-recae-2');
  const third = await sentA1('cuenta-recae-3');
  for (const lead of [first, second, third]) await hold((await record(lead)).rejection_id, lead.sentAt);
  // Despues de todo lo anterior (la seccion 2 libero con +40 h).
  const signal = hours(third.sentAt, 50);
  const freed = await release(signal, hours(third.sentAt, 50.1));
  if (freed.released !== 3 || freed.held_back !== 0) fail('liberar las tres', freed);
  const claimed = await claim(first, hours(third.sentAt, 50.1));
  if (claimed.outcome !== 'claimed') fail('la primera', claimed);
  await settle(claimed.rejection_id, 990_201, hours(third.sentAt, 50.2));
  // La primera vuelve con 131042: la cuenta cayo otra vez.
  const back = hours(third.sentAt, 50.3);
  await record(first, { kind: 'first_touch_retry', messageId: 990_201, retryOf: first.messageId, at: back });
  await requeue(first, 990_201, back);
  // Sin senal nueva: las otras dos vuelven a esperar y no salen.
  const after = await release(signal, hours(third.sentAt, 50.4));
  if (after.held_back !== 2 || after.released !== 0 || after.waiting !== 3) fail('volver a esperar las liberadas', after);
  if ((await claim(second, hours(third.sentAt, 50.4))).outcome !== 'not_released') fail('salio con la cuenta caida');
  if ((await holdOf(second)).attempts !== 0) fail('gasto un intento', await holdOf(second));
  // La guarda de la reserva: liberadas, y un rechazo de la cuenta entre la
  // liberacion y la reserva.
  const newSignal = hours(third.sentAt, 51);
  if ((await release(newSignal, hours(third.sentAt, 51.1))).released !== 3) fail('liberar otra vez');
  const other = await sentA1('cuenta-recae-otro');
  await record(other, { at: hours(third.sentAt, 51.2) });
  const guarded = await claim(third, hours(third.sentAt, 51.3));
  if (guarded.outcome !== 'account_down_again' || (await holdOf(third)).status !== 'waiting'
      || (await holdOf(third)).attempts !== 0) fail('la guarda de la reserva', guarded);
  // Las demas siguen liberadas hasta la proxima liberacion, que las devuelve.
  if ((await release(newSignal, hours(third.sentAt, 51.4))).held_back !== 2) fail('la liberacion siguiente');
  results.recae = { liberadas: freed.released, devueltas: after.held_back, reserva: guarded.outcome };
}

// ---------------------------------------------------------------------------
// 2c. La misma persona en dos conversaciones (H3 de la revision).
// ---------------------------------------------------------------------------
{
  const one1 = await sentA1('cuenta-misma-1');
  const one2 = await sentA1('cuenta-misma-2');
  for (const lead of [one1, one2]) await hold((await record(lead)).rejection_id, lead.sentAt);
  // Otra persona rechazada, y despues un primer contacto nuestro aceptado (y no
  // rechazado) al mismo telefono.
  const older = await sentA1('cuenta-despues-1');
  await hold((await record(older)).rejection_id, older.sentAt);
  const newer = await sentA1('cuenta-despues-2');
  await sql('update public.contacts set phone = $1 where id = $2', [`+${older.phone}`, newer.contact]);
  const signal = hours(newer.sentAt, 60);
  await release(signal, hours(newer.sentAt, 60.1));
  for (const lead of [one1, one2, older]) if ((await holdOf(lead)).status !== 'released') fail('liberar 2c', lead.label);
  const firstClaim = await claim(one1, hours(newer.sentAt, 60.1));
  if (firstClaim.outcome !== 'claimed') fail('la primera de la misma persona', firstClaim);
  // La segunda conversacion de la misma persona (Chatwoot da el mismo telefono).
  const same = await claim(one2, hours(newer.sentAt, 60.1), one1.phone);
  if (same.outcome !== 'blocked_same_person' || (await holdOf(one2)).reason !== 'blocked_same_person') {
    fail('dos envios a la misma persona', same);
  }
  // N4: mientras el contacto posterior tiene menos de 2 h, Meta todavia puede
  // rechazarlo: no es final y la fila sigue liberada.
  await sql('update public.followup_delivery_attempts set accepted_at = $1 where remote_message_id = $2',
    [hours(newer.sentAt, 59.5), String(newer.messageId)]);
  const pending = await claim(older, hours(newer.sentAt, 60.1));
  if (pending.outcome !== 'newer_contact_pending' || (await holdOf(older)).status !== 'released') {
    fail('un contacto posterior reciente quedo final', pending);
  }
  await sql('update public.followup_delivery_attempts set accepted_at = $1 where remote_message_id = $2',
    [hours(newer.sentAt, 57), String(newer.messageId)]);
  const later = await claim(older, hours(newer.sentAt, 60.1));
  if (later.outcome !== 'blocked_newer_contact') fail('un contacto posterior entregado', later);
  results.misma_persona = { misma: same.outcome, pendiente: pending.outcome, posterior: later.outcome };
}

// ---------------------------------------------------------------------------
// 2e. La misma persona fuera de la misma caida, y una reserva vieja trabada
//     (N2): no frenan.
// ---------------------------------------------------------------------------
{
  const past = await sentA1('cuenta-otra-caida-1');
  const stuck = await sentA1('cuenta-trabada-1');
  const now1 = await sentA1('cuenta-otra-caida-2');
  const now2 = await sentA1('cuenta-trabada-2');
  for (const lead of [past, stuck, now1, now2]) await hold((await record(lead)).rejection_id, lead.sentAt);
  const signal = hours(now2.sentAt, 61.5);
  await release(signal, hours(now2.sentAt, 61.6));
  // past: mandada en una caida anterior (sus fechas se corren 80 h atras).
  const pastClaim = await claim(past, hours(now2.sentAt, 61.6));
  await settle(pastClaim.rejection_id, 990_301, hours(now2.sentAt, 61.6));
  await sql(`update public.meta_account_holds set message_created_at = $1, expires_at = $2
    where rejection_id = $3`, [hours(now1.sentAt, -80), hours(now1.sentAt, -8), pastClaim.rejection_id]);
  // stuck: reservada hace 2 h y nunca cerrada.
  const stuckClaim = await claim(stuck, hours(now2.sentAt, 61.6));
  await sql('update public.meta_account_holds set claimed_at = $1 where rejection_id = $2',
    [hours(now2.sentAt, 59.5), stuckClaim.rejection_id]);
  const otherFall = await claim(now1, hours(now2.sentAt, 61.6), past.phone);
  const oldClaim = await claim(now2, hours(now2.sentAt, 61.6), stuck.phone);
  if (otherFall.outcome !== 'claimed' || oldClaim.outcome !== 'claimed') {
    fail('la misma persona fuera de la caida, o una reserva trabada, frenaron', { otherFall, oldClaim });
  }
  results.otra_caida = { otra_caida: otherFall.outcome, reserva_trabada: oldClaim.outcome };
}

// ---------------------------------------------------------------------------
// 2f. La guarda de la reserva con todos los codigos de la cuenta (N6).
// ---------------------------------------------------------------------------
{
  const lead = await sentA1('cuenta-otro-codigo');
  await hold((await record(lead)).rejection_id, lead.sentAt);
  const signal = hours(lead.sentAt, 62);
  await release(signal, hours(lead.sentAt, 62.1), [CODE, '131031']);
  if ((await holdOf(lead)).status !== 'released') fail('liberar 2f');
  // La cuenta cae con el otro codigo de la lista despues de la senal.
  const other = await sentA1('cuenta-131031');
  await record(other, { code: '131031', at: hours(lead.sentAt, 62.2) });
  const guarded = await claim(lead, hours(lead.sentAt, 62.3), lead.phone, [CODE, '131031']);
  if (guarded.outcome !== 'account_down_again') fail('la guarda con el otro codigo', guarded);
  results.otros_codigos = guarded.outcome;
}

// ---------------------------------------------------------------------------
// 2d. Las demas barreras copiadas del 131049: conversacion tomada, derivacion
//     sin atender, contacto sin permiso y baja.
// ---------------------------------------------------------------------------
{
  const convOf = async (lead) => {
    const context = JSON.stringify({ chatwoot_conversation_id: String(lead.chatwoot) });
    const found = await sql('select id from public.conversations where commercial_context = $1::jsonb', [context]);
    if (found.length) return found[0].id;
    const identity = one(await sql(`insert into public.channel_identities (contact_id, channel, account_id, external_user_id,
      external_conversation_id) values ($1, 'whatsapp', $2, $3, $4) returning id`,
    [lead.contact, `chatwoot:${A.accountId}`, lead.phone, String(lead.chatwoot)]), 'identidad').id;
    return one(await sql(`insert into public.conversations (contact_id, channel_identity_id, commercial_context)
      values ($1, $2, $3::jsonb) returning id`, [lead.contact, identity, context]), 'conversacion').id;
  };
  const taken = await sentA1('cuenta-tomada');
  const handoff = await sentA1('cuenta-derivada');
  const denied = await sentA1('cuenta-sin-permiso');
  const optedOut = await sentA1('cuenta-baja');
  for (const lead of [taken, handoff, denied, optedOut]) await hold((await record(lead)).rejection_id, lead.sentAt);
  await sql('update public.conversations set human_takeover = true where id = $1', [await convOf(taken)]);
  const handoffConversation = await convOf(handoff);
  const identity = one(await sql('select channel_identity_id from public.conversations where id = $1',
    [handoffConversation]), 'identidad').channel_identity_id;
  // El caso comercial inbound exige un alcance publicado (trigger de
  // 20260816000200); aca se prueba la barrera de la reserva, no la admision:
  // la fila se siembra con los triggers apagados en esta sesion.
  await sql(`set session_replication_role = replica`);
  const commercial = one(await sql(`insert into public.commercial_cases (id, case_kind, contact_id, selected_channel_identity_id,
      conversation_id, status, automation_status, authority_mode, inbound_scope_key, inbound_scope_version, tenant_ref)
    values (gen_random_uuid(), 'inbound_sales', $1, $2, $3, 'active', 'draft_only', 'shadow', 'att1-inbound', 1, 'lancemos') returning id`,
  [handoff.contact, identity, handoffConversation]), 'caso comercial').id;
  await sql(`insert into public.human_handoff_requests (id, conversation_id, command_key, primary_reason_code, requested_by,
      projection_policy_key, projection_policy_version, chatwoot_account_id, chatwoot_inbox_id, external_conversation_id,
      expected_team_id, note_template_key, note_template_version, private_note_body, status, commercial_case_id,
      inbound_scope_key, inbound_scope_version)
    values (gen_random_uuid(), $1, $2, 'policy_requires_human', 'agent', 'handoff-policy', 1, $3, $4, $5, 7, 'handoff-note', 1,
      'nota de prueba', 'requested', $6, 'att1-inbound', 1)`,
  [handoffConversation, `handoff:${handoff.chatwoot}`, A.accountId, A.inboxId, Number(handoff.chatwoot), commercial]);
  await sql(`set session_replication_role = origin`);
  await convOf(denied);
  await sql(`update public.contacts set contact_permission = 'opted_out' where id = $1`, [denied.contact]);
  await sql(`insert into public.contact_opt_out_events (contact_id, channel, purpose, source, canonical_account_id,
      canonical_inbox_id, canonical_conversation_id, canonical_message_id, external_user_id, occurred_at,
      normalized_rule_key, correlation_status, projection_status)
    values ($1, 'whatsapp', 'cart_recovery', 'chatwoot', $2, $3, $4, 4001, $5, now(), 'stop_receiving_messages',
      'applied', 'applied')`, [optedOut.contact, A.accountId, A.inboxId, Number(optedOut.chatwoot), optedOut.phone]);
  const signal = hours(optedOut.sentAt, 61);
  await release(signal, hours(optedOut.sentAt, 61.1));
  const barriers = {};
  for (const [name, lead, expected] of [['tomada', taken, 'blocked_conversation'],
    ['derivada', handoff, 'blocked_pending_handoff'], ['sin_permiso', denied, 'blocked_contact'],
    ['baja', optedOut, 'blocked_opt_out']]) {
    barriers[name] = (await claim(lead, hours(optedOut.sentAt, 61.1))).outcome;
    if (barriers[name] !== expected || (await holdOf(lead)).status !== 'blocked') fail(`la barrera ${name}`, barriers);
  }
  results.otras_barreras = barriers;
}

// ---------------------------------------------------------------------------
// 3. Lo que no entra.
// ---------------------------------------------------------------------------
{
  const outside = {};
  const marketing = await sentA1('cuenta-131049');
  const marketingRow = await record(marketing, { code: '131049' });
  outside.c131049 = (await hold(marketingRow.rejection_id, marketing.sentAt)).outcome;
  const unreachable = await sentA1('cuenta-131026');
  outside.c131026 = (await hold((await record(unreachable, { code: '131026' })).rejection_id, unreachable.sentAt)).outcome;
  const followup = await sentA1('cuenta-seguimiento');
  outside.seguimiento = (await hold((await record(followup, { kind: 'followup' })).rejection_id, followup.sentAt)).outcome;
  if (outside.c131049 !== 'not_eligible' || outside.c131026 !== 'not_eligible' || outside.seguimiento !== 'not_eligible') {
    fail('entraron codigos o toques que no son de la cuenta', outside);
  }
  // Ya vencido al registrarlo: no entra.
  const old = await sentA1('cuenta-vieja');
  outside.vieja = (await hold((await record(old)).rejection_id, hours(old.sentAt, 2), 3_600)).outcome;
  if (outside.vieja !== 'too_late') fail('una vieja entro', outside.vieja);
  // Vence en la espera: nunca sale.
  const expiring = await sentA1('cuenta-vence');
  await hold((await record(expiring)).rejection_id, expiring.sentAt, 3_600);
  const gone = await release(null, hours(expiring.sentAt, 2));
  if (gone.expired < 1 || (await holdOf(expiring)).status !== 'expired') fail('vencer', gone);
  // El reintento del 131049 no ve las filas de la cuenta.
  const marketingDue = await sql('select * from public.list_due_meta_first_touch_retries_v1($1,$2,$3,$4)',
    [A.inboxId, 259_200, 50, hours(expiring.sentAt, 25)]);
  const holdIds = new Set((await sql('select chatwoot_message_id from public.meta_account_holds'))
    .map((r) => Number(r.chatwoot_message_id)));
  if (holdIds.size === 0) fail('no hay filas de la cuenta para comparar');
  if (marketingDue.some((item) => holdIds.has(Number(item.chatwoot_message_id)))) fail('el 131049 vio una fila de la cuenta');
  results.fuera = outside;
}

// ---------------------------------------------------------------------------
// 4. El freno del bridge y las entradas invalidas.
// ---------------------------------------------------------------------------
{
  const wrote = await sentA1('cuenta-escribio');
  const row = await record(wrote);
  await hold(row.rejection_id, wrote.sentAt);
  const first = one(await sql('select * from public.block_meta_account_hold_retry_v1($1,$2,$3)',
    [row.rejection_id, 'bridge_lead_wrote', await dbNow()]), 'freno');
  const again = one(await sql('select * from public.block_meta_account_hold_retry_v1($1,$2,$3)',
    [row.rejection_id, 'bridge_lead_wrote', await dbNow()]), 'freno otra vez');
  if (first.outcome !== 'blocked' || again.outcome !== 'not_found' || (await holdOf(wrote)).reason !== 'bridge_lead_wrote') {
    fail('el freno del bridge', { first, again });
  }
  const now = await dbNow();
  await expectError(db, 'motivo del bridge', () => sql('select * from public.block_meta_account_hold_retry_v1($1,$2,$3)',
    [row.rejection_id, 'lead_wrote', now]), { code: '22023', message: 'block_meta_account_hold_retry_invalid_input' });
  await expectError(db, 'codigo del 131049', () => sql('select * from public.release_meta_account_holds_v1($1,$2,$3,$4)',
    [A.inboxId, ['131049'], now, now]), { code: '22023', message: 'release_meta_account_holds_invalid_input' });
  await expectError(db, 'sin codigos', () => sql('select * from public.release_meta_account_holds_v1($1,$2,$3,$4)',
    [A.inboxId, [], now, now]), { code: '22023', message: 'release_meta_account_holds_invalid_input' });
  await expectError(db, 'vencimiento', () => sql('select * from public.hold_meta_account_rejection_v1($1,$2,$3)',
    [row.rejection_id, 60, now]), { code: '22023', message: 'hold_meta_account_rejection_invalid_input' });
  await expectError(db, 'codigos de la reserva', () => sql('select * from public.claim_meta_account_hold_retry_v1($1,$2,$3,$4,$5,$6)',
    [A.inboxId, 1, '5215500000000', null, ['131049'], now]), { code: '22023', message: 'claim_meta_account_hold_retry_invalid_input' });
  await expectError(db, 'telefono', () => sql('select * from public.claim_meta_account_hold_retry_v1($1,$2,$3,$4,$5,$6)',
    [A.inboxId, 1, '+5215500000000', null, [CODE], now]), { code: '22023', message: 'claim_meta_account_hold_retry_invalid_input' });
  results.bridge = first.outcome;
  results.entradas = 'ok';
}

// ---------------------------------------------------------------------------
// 5. Apagar y volver a prender (N1): sin latido por mas que el hueco, la marca
//    pasa a la hora de arranque del proceso nuevo; nunca hacia atras.
// ---------------------------------------------------------------------------
{
  const base = await dbNow();
  const restart = hours(base, 100);
  const remarked = await mark(restart, restart);
  if (remarked.outcome !== 'remarked' || new Date(remarked.enabled_at).getTime() !== restart.getTime()) {
    fail('el latido viejo no movio la marca', remarked);
  }
  const off = await sentA1('cuenta-apagada');
  const offRow = await record(off, { at: hours(base, 99) });
  if ((await hold(offRow.rejection_id, restart)).outcome !== 'before_enabled') fail('entro lo rechazado con la espera apagada');
  const back = await mark(hours(base, 50), hours(base, 200));
  if (back.outcome !== 'remarked' || new Date(back.enabled_at).getTime() !== restart.getTime()) {
    fail('la marca fue hacia atras', back);
  }
  await expectError(db, 'hueco', () => sql('select * from public.mark_meta_account_hold_enabled_v1($1,$2,$3,$4)',
    [A.inboxId, restart, restart, 60]), { code: '22023', message: 'mark_meta_account_hold_enabled_invalid_input' });
  results.reprendido = { marca: remarked.outcome, apagada: 'before_enabled' };
}

console.log(`meta_account_hold_retry=${JSON.stringify(results)}`);
