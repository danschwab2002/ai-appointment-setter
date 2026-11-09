// El hilo de Slack y los avisos de Meta (migracion 20261109000100, F-146 parte 4, E-361), con las RPC reales
// sobre la cadena completa. Contrato: docs/contracts/template-request-v1.md 1.3.0.
//
// Prueba:
//   0. la forma: cada funcion nueva la ejecuta solo su rol, la tabla sin permisos y con RLS, correrla dos veces
//      aborta; save v1 y la lectura del agente no cambian (la lectura no trae el hilo);
//   1. guardar con hilo; rehacer sin sesion conserva el que habia; rehacer desde otra charla lo mueve; entrada
//      invalida; un guardado rechazado no toca el hilo;
//   2. a quien sondea: en revision, aprobada_meta y sin_respuesta_de_meta con id, hasta 7 dias; borrada en Meta no;
//      enviada sin id solo si Meta pudo crearla y sin pisar un envio en curso; la cadencia de 5 y 30 minutos; una
//      entrega anota last_polled_at;
//   3. el aviso: aprobada, rechazada con su motivo, recategorizada; una sola vez (segundo reclamo vacio); un
//      aviso por transicion nueva; sin hilo cuenta y no vuelve; reclamo abandonado (reinicio) no se repite;
//      rechazo de Slack reintenta hasta 3 veces; token o canal malo espera sin gastar intentos; aprobada_meta a
//      en_catalogo no avisa dos veces; lo de otra instancia no se toca.
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { buildWorld, expectError, fail, one, root } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261109000100_meta_template_request_notices_v1.sql';
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
if (!names.includes(MIGRATION)) fail(`falta ${MIGRATION}`);
const fixture = (path) => JSON.parse(readFileSync(join(root, 'tests/fixtures', path), 'utf8'));
const BRIDGE = fixture('meta_template_requests/bridge_att1_bienvenida_02.json');
const sha = (text) => createHash('sha256').update(text).digest('hex');

const world = await buildWorld();
const { db, INSTANCE, rpc } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const AE = 'U0123456789';
const DAN = 'U0DANSCHWAB1';
const TEAM = 'T0LANCEMOS01';
const DM = 'D0123456789';
const THREAD = '1762700000.123456';
const results = {};
const as = async (role, run) => {
  await db.exec('select pg_sleep(0.01)');
  await db.exec(`set role ${role}`);
  const out = await run();
  await db.exec('reset role');
  return out;
};
const agent = (run) => as('automation_expert_change', run);
const approver = (run) => as('flow_change_approver', run);
const service = (run) => as('service_role', run);
const shift = async (text, params) => {
  await db.exec('set session_replication_role = replica');
  await sql(text, params);
  await db.exec('set session_replication_role = origin');
};
const row = async (id) => one(await sql('select * from public.meta_template_requests where id = $1', [id]), 'fila');
const inMinutes = async (minutes) => new Date((await world.dbNow()).getTime() + minutes * 60_000).toISOString();

const draftNamed = (name, category = BRIDGE.draft.categoria) => ({ ...structuredClone(BRIDGE.draft), nombre: name,
  categoria: category });
const metaFor = (name, category = BRIDGE.meta_request.category) => ({ ...structuredClone(BRIDGE.meta_request), name,
  category });
const saveV2 = (draft, channel, thread, extra = {}) => agent(() => rpc('save_meta_template_request_draft_v2', INSTANCE,
  extra.id ?? null, extra.revision ?? null, draft, BRIDGE.purpose, null, null, AE, channel, thread));

let serial = 0;
const state = sha('estado');
const session = sha('sesion');
await approver(async () => {
  serial += 1;
  await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha('nonce'), '/flow-approval/', await inMinutes(10));
  await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
  const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, TEAM, DAN,
    await inMinutes(8 * 60));
  if (!created.ok) fail('la sesion', created);
});
const page = (fn, ...params) => approver(() => rpc(fn, INSTANCE, session, ...params));

// Una solicitud que paso por todo el camino real hasta Meta.
const sentRequest = async (name, { channel = DM, thread = THREAD, category = 'UTILITY', metaId } = {}) => {
  const saved = channel === null
    ? await agent(() => rpc('save_meta_template_request_draft_v1', INSTANCE, null, null, draftNamed(name, category),
      BRIDGE.purpose, null, null, AE))
    : await saveV2(draftNamed(name, category), channel, thread);
  if (!saved.ok) fail(`guardar ${name}`, saved);
  const recorded = await agent(() => rpc('record_meta_template_request_validation_v1', INSTANCE, saved.request_id,
    saved.revision, BRIDGE.validator, BRIDGE.result, metaFor(name, category), BRIDGE.preview, AE));
  if (recorded.status !== 'validada') fail(`validar ${name}`, recorded);
  await page('approver_approve_meta_template_request_v1', saved.request_id, recorded.fingerprint);
  const begun = await page('approver_begin_meta_template_send_v1', saved.request_id, recorded.fingerprint, 20);
  if (!begun.ok) fail(`empezar ${name}`, begun);
  if (metaId !== null) {
    const sent = await page('approver_record_meta_template_sent_v1', saved.request_id, metaId ?? String(1000 + serial++),
      'PENDING', category, null);
    if (!sent.ok) fail(`mandar ${name}`, sent);
  }
  return saved.request_id;
};
const status = (id, metaId, metaStatus, category, reason = null, inCatalog = false) => service(() => rpc(
  'record_meta_template_status_v1', INSTANCE, id, metaId, metaStatus, category, reason, inCatalog));
const poll = (limit = 50) => service(() => rpc('list_meta_template_requests_to_poll_v1', INSTANCE, limit));
const claim = (limit = 50) => service(() => rpc('claim_meta_template_notices_v1', INSTANCE, limit));
const finish = (id, kind, outcome, code = null) => service(() => rpc('finish_meta_template_notice_v1', INSTANCE, id, kind,
  outcome, code));
const ids = (out) => out.requests.map((r) => r.request_id);

// ---------------------------------------------------------------------------
// 0. La forma.
// ---------------------------------------------------------------------------
{
  const can = async (role, signature) => one(await sql(
    `select has_function_privilege($1, to_regprocedure('public.${signature}'), 'execute') as x`, [role]), role).x;
  const expected = {
    'save_meta_template_request_draft_v2(jsonb,uuid,integer,jsonb,text,uuid,text,text,text,text)': 'automation_expert_change',
    'list_meta_template_requests_to_poll_v1(jsonb,integer)': 'service_role',
    'claim_meta_template_notices_v1(jsonb,integer)': 'service_role',
    'finish_meta_template_notice_v1(jsonb,uuid,text,text,text)': 'service_role',
  };
  for (const [signature, owner] of Object.entries(expected)) {
    for (const role of ['automation_expert_change', 'flow_change_approver', 'service_role', 'anon', 'authenticated']) {
      if ((await can(role, signature)) !== (role === owner)) fail(`permisos de ${signature} para ${role}`);
    }
  }
  const table = one(await sql(`select c.relrowsecurity rls, has_table_privilege('service_role', c.oid, 'select') s,
    has_table_privilege('anon', c.oid, 'select') a from pg_class c
    where c.oid = 'public.meta_template_request_notices'::regclass`), 'tabla');
  if (!table.rls || table.s || table.a) fail('la tabla, sin permisos y con RLS', table);
  const again = await expectError(db, 'correrla dos veces', () => db.exec(readFileSync(join(root, 'supabase/migrations',
    MIGRATION), 'utf8')), { code: '55000', message: 'meta_template_request_notices_already_applied' });
  results.forma = again.message;
}

// ---------------------------------------------------------------------------
// 1. Guardar con el hilo.
// ---------------------------------------------------------------------------
{
  const saved = await saveV2(draftNamed('att1_hilo_01'), DM, THREAD);
  if (!saved.ok) fail('guardar con hilo', saved);
  let stored = await row(saved.request_id);
  if (stored.notify_slack_channel !== DM || stored.notify_slack_thread_ts !== THREAD) fail('el hilo guardado', stored);
  const kept = await saveV2(draftNamed('att1_hilo_01'), null, null, { id: saved.request_id, revision: 1 });
  stored = await row(saved.request_id);
  if (!kept.ok || stored.notify_slack_channel !== DM || stored.notify_slack_thread_ts !== THREAD) {
    fail('sin sesion, se queda el hilo', stored);
  }
  const moved = await saveV2(draftNamed('att1_hilo_01'), 'D9876543210', null, { id: saved.request_id, revision: 2 });
  stored = await row(saved.request_id);
  if (!moved.ok || stored.notify_slack_channel !== 'D9876543210' || stored.notify_slack_thread_ts !== null) {
    fail('desde otra charla, se mueve', stored);
  }
  const stale = await saveV2(draftNamed('att1_hilo_01'), DM, THREAD, { id: saved.request_id, revision: 1 });
  stored = await row(saved.request_id);
  if (stale.ok || stored.notify_slack_channel !== 'D9876543210') fail('un guardado rechazado no toca el hilo', stored);
  await expectError(db, 'un canal sin forma', () => saveV2(draftNamed('att1_hilo_02'), 'canal', null),
    { code: '22023', message: 'meta_template_slack_thread_invalid' });
  await expectError(db, 'hilo sin canal', () => saveV2(draftNamed('att1_hilo_02'), null, THREAD),
    { code: '22023', message: 'meta_template_slack_thread_invalid' });
  const read = await agent(() => rpc('get_meta_template_request_v1', INSTANCE, saved.request_id));
  if (JSON.stringify(read).includes('D9876543210')) fail('la lectura del agente no trae el hilo', read);
  await expectError(db, 'la pagina no guarda con hilo', () => approver(() => rpc('save_meta_template_request_draft_v2',
    INSTANCE, null, null, draftNamed('att1_hilo_02'), BRIDGE.purpose, null, null, AE, DM, THREAD)), { code: '42501' });
  results.hilo = 'ok';
}

// ---------------------------------------------------------------------------
// 2. A quien sondea.
// ---------------------------------------------------------------------------
{
  const inReview = await sentRequest('att1_sondeo_01', { metaId: '111111111' });
  const first = await poll();
  if (!ids(first).includes(inReview)) fail('en revision se sondea', first);
  if (first.requests.find((r) => r.request_id === inReview).meta_template_id !== '111111111') fail('con su id', first);
  if (!(await row(inReview)).last_polled_at) fail('entregarla anota last_polled_at');
  if (ids(await poll()).includes(inReview)) fail('recien mirada no vuelve');
  await shift(`update public.meta_template_requests set last_polled_at = clock_timestamp() - interval '6 minutes'
    where id = $1`, [inReview]);
  if (!ids(await poll()).includes(inReview)) fail('en la primera hora, a los 5 minutos');
  await shift(`update public.meta_template_requests set sent_at = clock_timestamp() - interval '2 hours',
    last_polled_at = clock_timestamp() - interval '6 minutes' where id = $1`, [inReview]);
  if (ids(await poll()).includes(inReview)) fail('despues de la hora, no a los 6 minutos');
  await shift(`update public.meta_template_requests set last_polled_at = clock_timestamp() - interval '31 minutes'
    where id = $1`, [inReview]);
  if (!ids(await poll()).includes(inReview)) fail('despues de la hora, a los 30 minutos');

  const midSend = await sentRequest('att1_sondeo_02', { metaId: null });
  if (ids(await poll()).includes(midSend)) fail('una enviada recien empezada no se pisa');
  await shift(`update public.meta_template_requests set send_started_at = clock_timestamp() - interval '6 minutes'
    where id = $1`, [midSend]);
  const orphan = (await poll()).requests.find((r) => r.request_id === midSend);
  if (!orphan || orphan.meta_template_id !== null || !Array.isArray(orphan.meta_components)) {
    fail('una enviada a medias se busca por nombre, con lo que se le pidio a Meta', orphan);
  }
  for (const uncertain of ['meta_http:503', 'meta:2', 'meta:1:99']) {
    await shift(`update public.meta_template_requests set last_send_error = $2, last_polled_at = null where id = $1`,
      [midSend, uncertain]);
    if (!ids(await poll()).includes(midSend)) fail(`con ${uncertain} Meta pudo crearla`);
  }
  await page('approver_record_meta_template_sent_v1', midSend, null, null, null, 'meta:100:2388040');
  await shift(`update public.meta_template_requests set last_polled_at = null where id = $1`, [midSend]);
  if (ids(await poll()).includes(midSend)) fail('lo que Meta dijo que no, no se busca');

  const due = async (id) => {
    await shift(`update public.meta_template_requests set last_polled_at = null where id = $1`, [id]);
    return ids(await poll()).includes(id);
  };
  const approvedMeta = await sentRequest('att1_sondeo_03', { metaId: '111111113' });
  await status(approvedMeta, '111111113', 'APPROVED', 'UTILITY', null, false);
  if ((await row(approvedMeta)).status !== 'aprobada_meta' || !(await due(approvedMeta))) fail('aprobada_meta se sondea');
  const noAnswer = await sentRequest('att1_sondeo_04', { metaId: '111111114' });
  await shift(`update public.meta_template_requests set sent_at = clock_timestamp() - interval '49 hours' where id = $1`,
    [noAnswer]);
  await status(noAnswer, '111111114', 'PENDING', 'UTILITY');
  if ((await row(noAnswer)).status !== 'sin_respuesta_de_meta' || !(await due(noAnswer))) {
    fail('sin_respuesta_de_meta se sondea');
  }
  for (const [id, label] of [[approvedMeta, 'aprobada_meta'], [noAnswer, 'sin_respuesta_de_meta']]) {
    await shift(`update public.meta_template_requests set sent_at = clock_timestamp() - interval '8 days' where id = $1`,
      [id]);
    if (await due(id)) fail(`${label}: el sondeo tiene fin`);
  }
  const deleted = await sentRequest('att1_sondeo_05', { metaId: '111111115' });
  const gone = await status(deleted, '111111115', 'DELETED', null);
  if (!gone.ok || (await row(deleted)).status !== 'en_revision') fail('borrada: se anota y no se mueve', gone);
  if (await due(deleted)) fail('borrada en Meta, no se sondea mas');
  for (const notice of (await claim()).notices) await finish(notice.request_id, notice.kind, 'failed', 'notice_invalid');
  results.sondeo = 'ok';
}

// ---------------------------------------------------------------------------
// 3. Los avisos.
// ---------------------------------------------------------------------------
{
  const approved = await sentRequest('att1_aviso_01', { metaId: '222222221' });
  const rejected = await sentRequest('att1_aviso_02', { metaId: '222222222' });
  const recategorized = await sentRequest('att1_aviso_03', { metaId: '222222223' });
  const noThread = await sentRequest('att1_aviso_04', { channel: null, metaId: '222222224' });
  const emptyBefore = await claim();
  if (emptyBefore.notices.length) fail('en revision no se avisa', emptyBefore);

  await status(approved, '222222221', 'APPROVED', 'UTILITY', null, true);
  await status(rejected, '222222222', 'REJECTED', 'UTILITY', 'INCORRECT_CATEGORY');
  await status(recategorized, '222222223', 'APPROVED', 'MARKETING', null, false);
  await status(noThread, '222222224', 'APPROVED', 'UTILITY', null, true);

  const claimed = await claim();
  const of = (id) => claimed.notices.find((n) => n.request_id === id);
  if (claimed.notices.length !== 3) fail('tres avisos reclamados', claimed);
  if (of(approved).kind !== 'aprobada' || of(approved).status !== 'en_catalogo'
      || of(approved).channel !== DM || of(approved).thread_ts !== THREAD) fail('aprobada', of(approved));
  if (of(rejected).kind !== 'rechazada' || of(rejected).meta_rejected_reason !== 'INCORRECT_CATEGORY') {
    fail('rechazada con el motivo', of(rejected));
  }
  if (of(recategorized).kind !== 'recategorizada' || of(recategorized).meta_category !== 'MARKETING'
      || of(recategorized).category_requested !== 'UTILITY') fail('recategorizada', of(recategorized));
  if (claimed.counts.no_thread !== 1) fail('la de sin hilo queda contada', claimed.counts);
  if ((await claim()).notices.length !== 0) fail('un segundo reclamo no devuelve nada');
  await status(approved, '222222221', 'APPROVED', 'UTILITY', null, true); // final: no se mueve

  const done = await finish(approved, 'aprobada', 'sent');
  if (!done.ok || done.state !== 'sent') fail('cerrar enviado', done);
  const twice = await finish(approved, 'aprobada', 'sent');
  if (twice.ok) fail('cerrar dos veces', twice);
  const afterSent = await claim();
  if (afterSent.notices.length !== 0 || afterSent.counts.sent !== 1) fail('enviado, no vuelve', afterSent);

  // Slack lo rechazo: vuelve hasta 3 veces y despues falla.
  let retry = await finish(rejected, 'rechazada', 'retry', 'slack_rejected');
  if (retry.state !== 'retry') fail('reintento', retry);
  if ((await claim()).notices.some((n) => n.request_id === rejected)) fail('reintento, no enseguida');
  for (const attempt of [2, 3]) {
    await shift(`update public.meta_template_request_notices set next_attempt_at = clock_timestamp() - interval '1 second'
      where request_id = $1`, [rejected]);
    if (!(await claim()).notices.some((n) => n.request_id === rejected)) fail(`intento ${attempt}`);
    retry = await finish(rejected, 'rechazada', 'retry', 'slack_rejected');
  }
  if (retry.state !== 'failed') fail('al tercero, fallado', retry);

  // El token o el canal de Slack: espera sin gastar intentos, y sale cuando se corrige.
  const blocked = await sentRequest('att1_aviso_08', { metaId: '222222228' });
  await status(blocked, '222222228', 'APPROVED', 'UTILITY', null, true);
  if (!(await claim()).notices.some((n) => n.request_id === blocked)) fail('reclamar el bloqueado');
  const attemptsOf = async () => one(await sql(`select state, attempts, finished_at, next_attempt_at
    from public.meta_template_request_notices where request_id = $1`, [blocked]), 'aviso');
  for (let round = 0; round < 5; round += 1) {
    const held = await finish(blocked, 'aprobada', 'waiting', 'slack_blocked:token_revoked');
    const seen = await attemptsOf();
    if (held.state !== 'waiting' || seen.attempts !== 0 || seen.finished_at !== null) fail('esperando', { held, seen });
    const counted = await claim();
    if (counted.counts.waiting !== 1 || counted.notices.some((n) => n.request_id === blocked)) {
      fail('esperando, contado y no enseguida', counted);
    }
    await shift(`update public.meta_template_request_notices set next_attempt_at = clock_timestamp() - interval '1 second'
      where request_id = $1`, [blocked]);
    if (!(await claim()).notices.some((n) => n.request_id === blocked)) fail(`vuelve la vuelta ${round}`);
  }
  if ((await finish(blocked, 'aprobada', 'sent')).state !== 'sent') fail('corregido el token, sale');

  // De aprobada_meta a en_catalogo es la misma aprobacion: no hay segundo aviso.
  const reachedCatalog = await sentRequest('att1_aviso_07', { metaId: '222222227' });
  await status(reachedCatalog, '222222227', 'APPROVED', 'UTILITY', null, false);
  const approvedNotice = (await claim()).notices.find((n) => n.request_id === reachedCatalog);
  if (approvedNotice?.kind !== 'aprobada' || approvedNotice.status !== 'aprobada_meta') fail('aprobada', approvedNotice);
  await finish(reachedCatalog, 'aprobada', 'sent');
  await status(reachedCatalog, '222222227', 'APPROVED', 'UTILITY', null, true);
  if ((await row(reachedCatalog)).status !== 'en_catalogo') fail('llego al catalogo');
  if ((await claim()).notices.some((n) => n.request_id === reachedCatalog)) fail('en_catalogo no avisa dos veces');

  // Una rechazada entrega el id, para leer el motivo si no esta guardado.
  if (!of(rejected).meta_template_id) fail('la rechazada trae su id', of(rejected));

  // El reinicio: un reclamo que nadie cerro no se repite.
  await shift(`update public.meta_template_request_notices set claimed_at = clock_timestamp() - interval '6 minutes'
    where request_id = $1`, [recategorized]);
  const afterCrash = await claim();
  if (afterCrash.notices.length !== 0 || afterCrash.counts.unknown !== 1) fail('reclamo abandonado', afterCrash);
  if ((await finish(recategorized, 'recategorizada', 'sent')).ok) fail('cerrar uno abandonado');

  // Otra transicion de la misma solicitud sí avisa, una vez (aprobada, y Meta le cambia el tipo).
  const movedType = await sentRequest('att1_aviso_05', { metaId: '222222225' });
  await status(movedType, '222222225', 'APPROVED', 'UTILITY', null, false);
  const firstKind = (await claim()).notices.find((n) => n.request_id === movedType);
  if (firstKind.kind !== 'aprobada') fail('primero aprobada', firstKind);
  await finish(movedType, 'aprobada', 'sent');
  await status(movedType, '222222225', 'APPROVED', 'MARKETING', null, false);
  const secondKind = (await claim()).notices.find((n) => n.request_id === movedType);
  if (!secondKind || secondKind.kind !== 'recategorizada') fail('despues recategorizada', secondKind);

  // Aprobada al crearla, sin pasar por el sondeo: avisa igual.
  const instant = await sentRequest('att1_aviso_06', { metaId: null });
  await page('approver_record_meta_template_sent_v1', instant, '222222226', 'APPROVED', 'UTILITY', null);
  if (!(await claim()).notices.some((n) => n.request_id === instant && n.kind === 'aprobada')) {
    fail('la aprobada al crearla avisa');
  }

  // Otra instancia no ve ni reclama nada de esta.
  const other = { ...INSTANCE, ally_ref: 'otra-aliada' };
  const foreign = await expectError(db, 'otra instancia', () => service(() => rpc('claim_meta_template_notices_v1', other, 50)));
  results.avisos = { otra_instancia: foreign.code };
}

console.log(JSON.stringify(results));
