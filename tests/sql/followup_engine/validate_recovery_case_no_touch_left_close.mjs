// El caso se cierra cuando su ultimo toque termina sin salir (migracion
// 20261111000100, E-423; informe E-413 del programa etapa-1, decision de Dan del
// 2026-11-10).
//
// Por que: un toque que termina permanent_failed o expired dejaba la secuencia y
// el caso abiertos sin nada pendiente, y nada los cerraba: un carrito nuevo de
// la misma oferta reventaba en el planificador (23505), el motor contestaba
// ya_en_curso y el reciclado contaba casos_abiertos. Medido el 10/11: 18 casos
// asi en ATT1 y 10 en Maribel.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera): el carrito capturado, los toques reservados por los caminos de hoy
// y el pago rechazado capturado. Los motivos de los toques son los medidos en
// produccion el 10/11 (pre_request_failed, sender_rejected_before_delivery,
// template_parameters_missing).
//
// Prueba:
//   0. la forma: suma una funcion privada (ningun rol de la API la ejecuta),
//      redefine solo el envoltorio publico con su ACL intacta, y correrla dos
//      veces aborta;
//   1. un toque, failed_before_request sin reintento: caso unreachable,
//      secuencia completed, la marca en context y el evento; el mismo intento
//      otra vez no cambia nada;
//   2. un toque, rejected (el remitente lo rechazo): unreachable;
//   3. tres toques, A1 aceptado y A2 sin salir: sequence_exhausted, sin Z;
//   4. con reintento: el caso sigue abierto; y el reintento despues del
//      vencimiento (expired) lo cierra;
//   5. la persona contesto mientras el toque estaba en vuelo: el caso no se
//      cierra (Dan decidio esperar con esa clase);
//  5b. un caso pausado, uno con otro toque vivo y uno con la conversacion
//      tomada no se cierran; el origen destrabe (el del kit) cierra y marca;
//   6. close_recovery_case_segment_coupon_v1 sigue cerrando en cancelled;
//   7. despues del cierre: el carrito nuevo de la misma oferta abre otro caso,
//      el reciclado ya no cuenta casos_abiertos, el pago rechazado abre su caso
//      y el motor arranca otro recorrido.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, errorOf, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261111000100_close_recovery_case_without_pending_touch_v1.sql';
const HELPER = '_close_recovery_case_without_pending_touch_v1(uuid,text,timestamp with time zone)';
const WRAPPER = 'finalize_followup_delivery_attempt(uuid,uuid,text,bigint,text,text,uuid,text,timestamp with time zone,timestamp with time zone,timestamp with time zone)';
const REASON = 'delivery_failed_no_touch_left';
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
for (const name of names.slice(position + 1)) {
  const text = readFileSync(join(root, 'supabase/migrations', name), 'utf8');
  if (text.includes('finalize_followup_delivery_attempt(') || text.includes('_close_recovery_case_without_pending_touch')) {
    fail(`${name} toca la finalizacion o el cierre: revisar esta prueba`);
  }
}

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
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
  if (before.has(HELPER)) fail('el cierre ya existe antes de la migracion');
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.has(s) && after.get(s).definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && ['acl', 'prosecdef', 'provolatile'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, [WRAPPER]) || !sameSet(added, [HELPER]) || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  const helper = after.get(HELPER);
  if (helper.service_x || helper.api_x || helper.prosecdef) {
    fail('el cierre es privado y security invoker', helper);
  }
  const wrapper = after.get(WRAPPER);
  if (!wrapper.service_x || wrapper.api_x || !wrapper.prosecdef
      || !wrapper.definition.includes('_close_recovery_case_without_pending_touch_v1')) {
    fail('el envoltorio sigue siendo de service_role y llama al cierre', wrapper);
  }
  // El envoltorio nuevo, deshaciendo sus tres cambios (la variable, el loop en
  // vez de return query y la llamada final), es el texto de antes, entero.
  const strip = (text) => text.replace(/\s+/g, ' ').trim();
  const undone = strip(wrapper.definition)
    .replace(/ declare v_row public\.scheduled_actions%rowtype; begin /, ' begin ')
    .replace(/ for v_row in (select \* from public\._finalize_followup_delivery_attempt\([^;]*?\)) loop return next v_row; end loop;/,
      ' return query $1;')
    .replace(/ -- close_recovery_case_without_pending_touch:.*?end if; end; \$function\$$/, ' end; $function$');
  if (undone !== strip(before.get(WRAPPER).definition)) {
    fail('el envoltorio cambio algo mas que la llamada al cierre', { undone, before: strip(before.get(WRAPPER).definition) });
  }
  const error = await errorOf(db, () => applyFile(db, join(root, 'supabase/migrations', MIGRATION)));
  await db.exec('rollback').catch(() => {});
  if (error?.code !== '55000' || error.message !== 'close_recovery_case_without_pending_touch_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  results.forma = { nuevas: added.length, cambiadas: changed.length, segunda_corrida: error.message };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, B, FLOWS, cartFor, planCart, admit, refusalFor, reserve, startOld, startNew, accept, stepOf, stepOfId,
  caseOf, runOf, route, createRoute, dbNow, sleep } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
await db.exec('alter role service_role bypassrls');
const asService = async (run) => {
  await db.exec('set role service_role');
  try {
    return await run();
  } finally {
    await db.exec('reset role').catch(() => {});
  }
};
// El bridge llama al envoltorio como service_role.
const finalize = async (reserved, outcome, reason, nextAttemptAt = null) => one(await asService(async () => sql(
  'select * from public.finalize_followup_delivery_attempt($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)',
  [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, outcome, null, null, reason,
    nextAttemptAt, null, await dbNow()])), `finalizar ${reason}`);
const sendTouch = async (lead, actionId, start = startOld) => {
  const reserved = await reserve(lead, actionId);
  const started = await start(reserved);
  if ((started.phase ?? started.outcome) !== 'request_started') fail(`${lead.label}: no arranco`, started);
  await accept(lead, reserved);
};
const sequencesOf = async (caseId) => sql(
  'select id, status, completion_reason, completed_at from public.followup_sequences where recovery_case_id = $1 order by created_at',
  [caseId]);
const closedEvents = async (caseId) => sql(`select data from public.conversation_events
  where recovery_case_id = $1 and event_type = 'recovery_case_closed_without_pending_touch'`, [caseId]);
const expectClosed = async (lead, actionId, { to, actionStatus, terminal }) => {
  const recovery = await caseOf(lead);
  const action = await stepOfId(actionId);
  const sequences = await sequencesOf(recovery.id);
  const mark = recovery.context?.closed_without_pending_touch;
  const events = await closedEvents(recovery.id);
  if (recovery.status !== to || recovery.closed_at === null || recovery.won_at !== null
      || recovery.next_contact_reason !== REASON
      || action.status !== actionStatus || action.terminal_reason !== terminal
      || sequences.length !== 1 || sequences[0].status !== 'completed' || sequences[0].completion_reason !== REASON
      || mark?.to_status !== to || mark?.origin !== 'finalize' || mark?.action_id !== actionId
      || mark?.terminal_reason !== terminal || !sameSet(mark?.sequence_ids ?? [], [sequences[0].id])
      || !['grace_period', 'active'].includes(mark?.from_status)
      || events.length !== 1 || events[0].data.to_status !== to) {
    fail(`${lead.label}: el cierre`, { recovery: recovery.status, mark, action: [action.status, action.terminal_reason],
      sequences, events: events.length });
  }
  return { recovery, mark };
};
const pending = (lead) => sql(`select step_key, status from public.scheduled_actions
  where recovery_case_id = $1 and status in ('pending','deferred','retryable_failed','delivery_unknown')`,
[lead.plan.recovery_case_id]);
const oneTouch = async (label) => {
  const lead = await cartFor(label, { P: B });
  const plan = await planCart(lead);
  if (!plan.scheduled_action_id) fail(`${label}: el carrito no planifico`, plan);
  return lead;
};
const atA2 = async (label) => {
  const lead = await cartFor(label);
  const plan = await planCart(lead);
  if (!plan.scheduled_action_id) fail(`${label}: el carrito no planifico`, plan);
  await sendTouch(lead, plan.scheduled_action_id);
  await sleep(1_100);
  return lead;
};

// ---------------------------------------------------------------------------
// 1. Un toque que no sale antes del pedido, sin reintento.
// ---------------------------------------------------------------------------
{
  const lead = await oneTouch('un-toque-pre-request');
  if ((await caseOf(lead)).policy_key !== 'att1-recuperacion-un-toque') fail('el caso no es de un toque');
  const reserved = await reserve(lead, lead.plan.scheduled_action_id);
  const row = await finalize(reserved, 'failed_before_request', 'pre_request_failed');
  if (row.status !== 'permanent_failed' || row.id !== reserved.actionId) fail('un toque: la fila devuelta', row);
  const { recovery, mark } = await expectClosed(lead, reserved.actionId,
    { to: 'unreachable', actionStatus: 'permanent_failed', terminal: 'pre_request_failed' });
  // El mismo intento otra vez: la privada lo da por cerrado igual y el cierre no hace nada.
  const replay = await finalize(reserved, 'failed_before_request', 'pre_request_failed');
  const again = await caseOf(lead);
  if (replay.status !== 'permanent_failed' || again.version !== recovery.version
      || (await closedEvents(recovery.id)).length !== 1) {
    fail('un toque: repetir cambio algo', { replay: replay.status, version: [recovery.version, again.version] });
  }
  results.un_toque = { caso: recovery.status, desde: mark.from_status, repetir: 'sin cambios' };
}

// ---------------------------------------------------------------------------
// 2. El remitente lo rechazo antes de entregar.
// ---------------------------------------------------------------------------
{
  const lead = await oneTouch('un-toque-rechazado');
  const reserved = await reserve(lead, lead.plan.scheduled_action_id);
  const started = await startOld(reserved);
  if ((started.phase ?? started.outcome) !== 'request_started') fail('rechazado: no arranco', started);
  await finalize(reserved, 'rejected', 'sender_rejected_before_delivery');
  await expectClosed(lead, reserved.actionId,
    { to: 'unreachable', actionStatus: 'permanent_failed', terminal: 'sender_rejected_before_delivery' });
  results.rechazado = 'unreachable';
}

// ---------------------------------------------------------------------------
// 3. Tres toques: A1 salio y A2 no.
// ---------------------------------------------------------------------------
{
  const lead = await atA2('a2-sin-salir');
  const a2 = await stepOf(lead, 'cart_second_touch');
  const reserved = await reserve(lead, a2.id);
  await finalize(reserved, 'failed_before_request', 'template_parameters_missing');
  await expectClosed(lead, a2.id,
    { to: 'sequence_exhausted', actionStatus: 'permanent_failed', terminal: 'template_parameters_missing' });
  const z = await sql(`select id from public.scheduled_actions where recovery_case_id = $1 and step_key = 'cart_last_touch'`,
    [lead.plan.recovery_case_id]);
  if (z.length !== 0) fail('A2: quedo un Z planificado', z);
  results.a2_sin_salir = 'sequence_exhausted';
}

// ---------------------------------------------------------------------------
// 4. Con reintento el caso sigue abierto; el reintento vencido lo cierra.
// ---------------------------------------------------------------------------
{
  const lead = await oneTouch('reintento');
  const reserved = await reserve(lead, lead.plan.scheduled_action_id);
  const action = await stepOfId(reserved.actionId);
  const soon = new Date((await dbNow()).getTime() + 60_000);
  const row = await finalize(reserved, 'failed_before_request', 'pre_request_failed', soon);
  const open = await caseOf(lead);
  if (row.status !== 'retryable_failed' || !['grace_period', 'active'].includes(open.status)
      || open.context?.closed_without_pending_touch || (await pending(lead)).length !== 1) {
    fail('reintento: el caso tendria que seguir abierto', { row: row.status, recovery: open.status });
  }
  // El segundo intento falla y el proximo caeria despues del vencimiento.
  const second = await reserve(lead, reserved.actionId);
  const late = new Date(new Date(action.expires_at).getTime() + 60_000);
  const expired = await finalize(second, 'failed_before_request', 'pre_request_failed', late);
  if (expired.status !== 'expired' || expired.terminal_reason !== 'retry_beyond_expiration') {
    fail('reintento vencido: la accion', expired);
  }
  await expectClosed(lead, reserved.actionId,
    { to: 'unreachable', actionStatus: 'expired', terminal: 'retry_beyond_expiration' });
  results.reintento = { con_reintento: open.status, vencido: 'unreachable' };
}

// ---------------------------------------------------------------------------
// 5. La persona contesto con el toque en vuelo: no se cierra.
// ---------------------------------------------------------------------------
{
  const lead = await atA2('contesto-en-vuelo');
  const a2 = await stepOf(lead, 'cart_second_touch');
  const reserved = await reserve(lead, a2.id);
  const conversation = one(await sql(`select coalesce(rc.conversation_id, fs.conversation_id) as id
    from public.recovery_cases rc join public.followup_sequences fs on fs.recovery_case_id = rc.id
    where rc.id = $1`, [lead.plan.recovery_case_id]), 'conversacion').id;
  if (!conversation) fail('contesto: el caso sin conversacion');
  await sql("update public.conversations set last_inbound_at = now() where id = $1", [conversation]);
  const row = await finalize(reserved, 'failed_before_request', 'pre_request_failed');
  const recovery = await caseOf(lead);
  if (row.status !== 'permanent_failed' || recovery.status !== 'active'
      || recovery.context?.closed_without_pending_touch || (await closedEvents(recovery.id)).length !== 0) {
    fail('contesto: el caso no tendria que cerrarse', { row: row.status, recovery: recovery.status });
  }
  const direct = one(await sql('select public._close_recovery_case_without_pending_touch_v1($1, $2, now()) as r',
    [a2.id, 'destrabe']), 'cierre directo').r;
  if (direct !== 'conversation_in_progress') fail('contesto: el motivo', direct);
  results.contesto = direct;
}

// ---------------------------------------------------------------------------
// 5b. Lo que el cierre no toca, y el origen del destrabe. El caso trabado se
//     arma como quedaba antes de la migracion: la privada cierra la accion y el
//     caso queda abierto.
// ---------------------------------------------------------------------------
{
  const stuck = async (label) => {
    const lead = await oneTouch(label);
    const reserved = await reserve(lead, lead.plan.scheduled_action_id);
    await sql('select * from public._finalize_followup_delivery_attempt($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)',
      [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, 'failed_before_request', null, null,
        'pre_request_failed', null, null, await dbNow()]);
    if ((await caseOf(lead)).status !== 'grace_period') fail(`${label}: el trabado no quedo abierto`);
    return { lead, actionId: reserved.actionId };
  };
  const close = async (actionId, origin = 'destrabe') => one(await sql(
    'select public._close_recovery_case_without_pending_touch_v1($1, $2, now()) as r', [actionId, origin]), 'cierre').r;

  // Pausado: no se toca.
  const paused = await stuck('pausado');
  await sql("update public.recovery_cases set status = 'paused' where id = $1", [paused.lead.plan.recovery_case_id]);
  const pausedResult = await close(paused.actionId);
  if (pausedResult !== 'case_not_open' || (await caseOf(paused.lead)).status !== 'paused') fail('pausado', pausedResult);

  // Otro toque vivo en el caso: no se cierra.
  const pending = await stuck('con-otro-toque');
  await sql(`insert into public.scheduled_actions (followup_sequence_id, recovery_case_id, action_type, status, due_at,
      expires_at, expected_case_version, max_execution_retries, idempotency_key, policy_key, policy_version, step_key,
      conversation_id, anchor_type, anchor_subject_internal_id, anchor_observed_at, anchor_checkpoint)
    select followup_sequence_id, recovery_case_id, action_type, 'pending', now() + interval '1 hour',
      now() + interval '2 hours', expected_case_version, max_execution_retries, idempotency_key || ':otro',
      policy_key, policy_version, step_key,
      conversation_id, anchor_type, anchor_subject_internal_id, anchor_observed_at, anchor_checkpoint
    from public.scheduled_actions where id = $1`, [pending.actionId]);
  const pendingResult = await close(pending.actionId);
  if (pendingResult !== 'touch_pending' || (await caseOf(pending.lead)).status !== 'grace_period') {
    fail('con otro toque', pendingResult);
  }

  // Un toque anterior cancelado porque lo tomo alguien del equipo: no se cierra.
  const taken = await stuck('tomada');
  await sql(`insert into public.scheduled_actions (followup_sequence_id, recovery_case_id, action_type, status, due_at,
      expires_at, expected_case_version, max_execution_retries, idempotency_key, policy_key, policy_version, step_key,
      conversation_id, anchor_type, anchor_subject_internal_id, anchor_observed_at, anchor_checkpoint, terminal_reason)
    select followup_sequence_id, recovery_case_id, action_type, 'cancelled', due_at - interval '1 minute',
      expires_at, expected_case_version, max_execution_retries, idempotency_key || ':tomada',
      policy_key, policy_version, step_key,
      conversation_id, anchor_type, anchor_subject_internal_id, anchor_observed_at, anchor_checkpoint,
      'human_takeover_active'
    from public.scheduled_actions where id = $1`, [taken.actionId]);
  const takenResult = await close(taken.actionId);
  if (takenResult !== 'conversation_in_progress' || (await caseOf(taken.lead)).status !== 'grace_period') {
    fail('tomada', takenResult);
  }

  // El origen destrabe, como lo llama el kit.
  const kit = await stuck('destrabe');
  const kitResult = await close(kit.actionId);
  const kitCase = await caseOf(kit.lead);
  if (kitResult !== 'closed_unreachable' || kitCase.status !== 'unreachable'
      || kitCase.context?.closed_without_pending_touch?.origin !== 'destrabe'
      || kitCase.context?.closed_without_pending_touch?.from_status !== 'grace_period') {
    fail('destrabe', { kitResult, status: kitCase.status, mark: kitCase.context?.closed_without_pending_touch });
  }
  if (await close(kit.actionId) !== 'case_not_open') fail('destrabe: repetir');
  const badOrigin = await errorOf(db, () => close(kit.actionId, 'otro'));
  if (badOrigin?.message !== 'close_without_pending_touch_origin_invalid') fail('origen invalido', badOrigin?.message);
  results.no_toca = { pausado: pausedResult, con_otro_toque: pendingResult, tomada: takenResult,
    destrabe: kitResult };
}

// ---------------------------------------------------------------------------
// 6. El freno del cupon de la campana sigue cerrando en cancelled.
// ---------------------------------------------------------------------------
{
  const lead = await atA2('cupon-de-campana');
  const a2 = await stepOf(lead, 'cart_second_touch');
  const reserved = await reserve(lead, a2.id);
  const outcome = one(await asService(async () => sql(
    'select outcome from public.close_recovery_case_segment_coupon_v1($1,$2,$3,$4,$5)',
    [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, await dbNow()])), 'cupon').outcome;
  const recovery = await caseOf(lead);
  if (outcome !== 'closed' || recovery.status !== 'cancelled' || recovery.context?.closed_without_pending_touch) {
    fail('cupon: el cierre de la campana', { outcome, recovery: recovery.status });
  }
  results.cupon_de_campana = { outcome, caso: recovery.status };
}

// ---------------------------------------------------------------------------
// 7. Lo que el caso abierto frenaba.
// ---------------------------------------------------------------------------
{
  // El carrito nuevo de la misma oferta (sin el cierre: 23505 en el planificador).
  const lead = await oneTouch('carrito-de-nuevo');
  const reserved = await reserve(lead, lead.plan.scheduled_action_id);
  await finalize(reserved, 'failed_before_request', 'pre_request_failed');
  const closed = lead.plan.recovery_case_id;
  if ((await caseOf(lead)).status !== 'unreachable') fail('carrito de nuevo: el cierre');
  const recycle = async () => Number(one(await sql(
    "select public._identity_recycle_inventory($1, $2, $3, null, false) -> 'guards' ->> 'casos_abiertos' as n",
    [B.tenant, B.ally, lead.phone]), 'reciclado').n);
  if (await recycle() !== 0) fail('carrito de nuevo: el reciclado cuenta el caso cerrado');
  await cartFor(lead, { P: B });
  const plan = await planCart(lead);
  if (plan.recovery_case_id === closed || !plan.scheduled_action_id || plan.created !== true) {
    fail('carrito de nuevo: no arranco limpio', plan);
  }
  results.carrito_de_nuevo = { caso_nuevo: plan.created, casos_abiertos_antes: 0 };

  // El pago rechazado de la persona (sin el cierre se suma al caso abierto sin mensaje).
  const payer = await cartFor('rechazo-despues');
  const payerPlan = await planCart(payer);
  const a1 = await reserve(payer, payerPlan.scheduled_action_id);
  await finalize(a1, 'failed_before_request', 'pre_request_failed');
  if ((await caseOf(payer)).status !== 'unreachable') fail('rechazo despues: el cierre');
  const refusal = await refusalFor(1, payer);
  if (refusal.recovery_case_id === payerPlan.recovery_case_id) fail('rechazo despues: se sumo al caso cerrado', refusal);
  results.rechazo_despues = { caso_nuevo: refusal.created };
}
{
  // El motor: cerrado el caso, el carrito nuevo arranca otro recorrido.
  const created = await createRoute(A.funnel, 'carrito', 'carrito', A.scope);
  if (!created.ok) fail('la ruta del carrito', created);
  const toMotor = await route(A.funnel, 'carrito', FLOWS.carrito, 'motor');
  if (!toMotor.ok) fail('el carrito a motor', toMotor);
  const lead = await cartFor('motor');
  const first = await admit(lead);
  if (first.outcome !== 'motor_arranco') fail('motor: la admision', first);
  const reserved = await reserve(lead, lead.plan.scheduled_action_id);
  await finalize(reserved, 'failed_before_request', 'pre_request_failed');
  const closed = lead.plan.recovery_case_id;
  if ((await caseOf(lead)).status !== 'unreachable') fail('motor: el cierre', await caseOf(lead));
  await cartFor(lead);
  const row = await admit(lead);
  const run = row.recovery_case_id ? await runOf(row.recovery_case_id) : null;
  if (row.outcome !== 'motor_arranco' || row.recovery_case_id === closed || run?.flow_id !== FLOWS.carrito) {
    fail('motor: el carrito nuevo', { row, run });
  }
  results.motor = row.outcome;
}

console.log(JSON.stringify(results));
await db.close();
