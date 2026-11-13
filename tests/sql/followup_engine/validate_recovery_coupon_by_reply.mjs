// La respuesta escrita a A2 reactiva el cupon personal como el boton, y el
// agente lee el cupon vigente de la persona (migracion 20261102000100, E-267,
// el arreglo de fondo de F-148; diagnostico:
// documentacion/2026-11-01-tres-errores-de-mensajes-en-conversaciones-reales.md,
// error 1).
//
// Por que: en ATT1 #471 y #517 la persona contesto A2 («Tu 10 % ya vencio, pero
// si me respondes te lo reactivo») escribiendo, y no con el boton «Quiero mi
// 10 %». La respuesta no adelantaba Z y la reevaluacion de Z la cancelaba por
// prospect_replied; el agente no sabia el cupon de la persona.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera, el carrito capturado, la politica de tres toques A1, A2 a 1 s y Z a
// 2 s). A1, A2 y Z salen por el camino real (reserva, arranque, aceptacion);
// los links de A1 y de Z, por reserve_recovery_checkout_issuance_v1. Los cupones
// se insertan ya valid, como validate_payment_failure_other_coupon_delivered.mjs
// (el alta en Hotmart es del worker, fuera de la base): la siembra no los pisa.
//
// Prueba:
//   0. la forma: agrega exactamente las dos funciones, definer, solo
//      service_role, sin tocar ninguna otra; correrla dos veces aborta con
//      55000; con una request_recovery_renewal_v1 cambiada (sin el limite a
//      cart_last_touch) aborta con 55000 sin crear nada;
//   1. la respuesta escrita despues de A2 adelanta Z (advanced), igual que el
//      boton, y la reevaluacion de Z con la respuesta ya no la cancela; sin el
//      pedido la cancela por prospect_replied (rojo). El mismo mensaje otra vez
//      y otro mensaje dan already_requested. Un mensaje anterior a A2 (Z creado
//      despues) da reply_before_last_touch sin escribir; otra persona,
//      no_pending_last_touch;
//   2. el cupon del turno: el 1 que salio en A1 (valid); nada si no salio (el
//      cupon sembrado sin A1); renewal_in_progress con el pedido y Z sin salir;
//      el 2 cuando Z salio con su link; nada con el cupon vencido, con la
//      compra, ni para otra persona; nunca un tercer codigo.
import { PGlite } from '@electric-sql/pglite';
import { randomBytes } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261102000100_recovery_coupon_by_reply_v1.sql';
const BY_REPLY = 'request_recovery_renewal_by_reply_v1(bigint,bigint,bigint,bigint,text,timestamp with time zone)';
const LOOKUP = 'get_recovery_coupon_for_conversation_v1(bigint,bigint,bigint,text)';
const RENEWAL = 'request_recovery_renewal_v1(bigint,bigint,bigint,bigint,text,timestamp with time zone)';
const results = {};
const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);

const names = readdirSync(join(root, 'supabase/migrations')).filter((name) => name.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues van la de las solicitudes de plantilla de Meta (20261102000200, E-268) y la del barrido del
// seguimiento nativo (20261102000300, E-269): tablas y funciones nuevas.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261102000200_meta_template_requests_v1.sql', '20261102000300_flow_engine_native_silence_sweep_v1.sql',
  // El reciclado de un telefono de prueba (20261102000400, E-270).
  '20261102000400_identity_recycle_v1.sql',
  // Los rechazos de Meta (20261103000500, E-284).
  '20261103000500_meta_delivery_rejections_v1.sql',
  // El historial de la pagina de aprobacion (20261103000600, E-295): dos lecturas del rol de la pagina.
  '20261103000600_flow_approval_history_v1.sql',
  // Si la persona ya es cliente (20261103000700, E-297): una lectura de service_role.
  '20261103000700_customer_status_v1.sql',
  // Las cuatro ramas del pago rechazado nativo (20261103000900, E-306).
  '20261103000900_flow_engine_native_payment_failure_branches_v1.sql',
  // El aviso de derivaciones sin atender (20261103001000, E-309): una lectura de service_role.
  '20261103001000_handoff_wait_alerts_v1.sql',
  // La etiqueta de un segmento en un flujo nativo (20261103001300, E-316): un reemplazo contado en la forma nativa.
  '20261103001300_flow_engine_native_segment_label_v1.sql',
  // La entrada a una campana: lo que contesto cada conversacion a la pregunta (20261103001500, E-317).
  '20261103001500_segment_entry_answers_v1.sql',
  // El origen de un recorrido nativo para la campana por landing (20261103001700, E-321): una lectura de service_role.
  '20261103001700_flow_engine_native_campaign_origin_v1.sql',
  // El cierre del caso por el cupon de una campana (20261106000100, E-339): una RPC de service_role.
  '20261106000100_recovery_case_segment_coupon_close_v1.sql',
  // El rechazo de la cuenta espera a que la cuenta vuelva (20261107000100, E-348): dos tablas y ocho RPC de service_role.
  '20261107000100_meta_account_hold_retry_v1.sql',
  // El almacen dice quien ejecuta cada flujo (20261108000100, E-353): una clave mas en read_flow_store_v1.
  '20261108000100_flow_store_route_executor_v1.sql',
  // La base no publica una version nativa que el motor no ejecuta (20261108004100, E-357).
  '20261108004100_flow_change_publish_native_version_guard_v1.sql',
  // Las campanas del Automation Expert como dato (20261108005000, E-355).
  '20261108005000_agent_campaigns_v1.sql',
  // El sondeo de Meta y el aviso en el hilo (20261109000100, E-361).
  '20261109000100_meta_template_request_notices_v1.sql',
  // La oferta de la campana en el link del agente (20261110000500, E-410).
  '20261110000500_campaign_offer_checkout_issuance_v1.sql',
  '20261111000100_close_recovery_case_without_pending_touch_v1.sql',
  '20261111000200_pending_cancel_keeps_first_contact_v1.sql',
  // Los numeros del tablero (20261112200000, E-507): una lectura nueva.
  '20261112200000_read_results_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de las solicitudes de plantilla`, names.slice(position + 1));
}

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile, p.proowner, p.proconfig
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
  const inside = readFileSync(join(root, 'supabase/migrations', MIGRATION), 'utf8')
    .replace('\nbegin;\n', '\n-- begin: la del validador\n').replace(/\ncommit;\s*$/, '\n');
  // El freno: una renovacion sin el limite a cart_last_touch aborta con 55000.
  {
    const renewal = before.get(RENEWAL).definition;
    const altered = renewal.replace("      and action.step_key = 'cart_last_touch'\n", '');
    if (altered === renewal) fail('el freno: no se pudo armar la renovacion cambiada');
    await db.exec('begin');
    let caught = null;
    try {
      await db.exec(altered);
      await db.exec(inside);
    } catch (error) {
      caught = error;
    }
    await db.exec('rollback');
    if (caught?.code !== '55000' || caught.message !== 'unexpected_request_recovery_renewal_definition') {
      fail('el freno de la renovacion cambiada', { code: caught?.code, message: caught?.message });
    }
    const untouched = await snapshot(db);
    if (untouched.size !== before.size
        || [...before.keys()].some((sig) => untouched.get(sig)?.definition !== before.get(sig).definition)) {
      fail('el freno dejo algo cambiado');
    }
    results.freno = caught.message;
  }
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.has(s) && (after.get(s).definition !== before.get(s).definition
    || ['acl', 'prosecdef', 'provolatile', 'proowner', 'proconfig'].some((k) => JSON.stringify(after.get(s)[k])
      !== JSON.stringify(before.get(s)[k]))));
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  if (changed.length !== 0 || removed.length !== 0 || !sameSet(added, [BY_REPLY, LOOKUP])) {
    fail('la migracion cambio otra cosa', { changed, added, removed });
  }
  const acl = (await db.query(`
    select p.oid::regprocedure::text as signature, p.prosecdef as definer, p.provolatile as volatility,
           has_function_privilege('anon', p.oid, 'EXECUTE') as anon_x,
           has_function_privilege('authenticated', p.oid, 'EXECUTE') as auth_x,
           has_function_privilege('service_role', p.oid, 'EXECUTE') as service_x,
           array_to_string(p.proconfig, ',') as config
    from pg_proc p where p.oid in (to_regprocedure($1), to_regprocedure($2))
  `, [`public.${BY_REPLY}`, `public.${LOOKUP}`])).rows;
  if (acl.length !== 2 || acl.some((row) => !row.definer || row.anon_x || row.auth_x || !row.service_x
      || row.config !== 'search_path=pg_catalog, public, pg_temp')) {
    fail('los permisos de las nuevas', acl);
  }
  if (acl.find((row) => row.signature === LOOKUP).volatility !== 's') fail('la lectura tiene que ser stable');
  let error = null;
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  } catch (caught) {
    error = caught;
  }
  await db.exec('rollback').catch(() => {});
  if (error?.code !== '55000' || error.message !== 'recovery_coupon_by_reply_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  const again = await snapshot(db);
  if (again.size !== after.size || [...after.keys()].some((s) => again.get(s)?.definition !== after.get(s).definition)) {
    fail('la segunda corrida cambio algo');
  }
  results.forma = { nuevas: added.length, cambiadas: 0, solo_service_role: true, segunda_corrida: error.message };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, dbNow, cartFor, planCart, reserve, startOld, accept, stepOf, stepOfId, evidence, sleep } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const rolledBack = async (run) => {
  await db.exec('begin');
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};
const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
const ulid = () => {
  let value = (BigInt(Date.now()) << 80n) | BigInt(`0x${randomBytes(10).toString('hex')}`);
  let out = '';
  for (let i = 0; i < 26; i += 1) {
    out = CROCKFORD[Number(value & 31n)] + out;
    value >>= 5n;
  }
  return out;
};
let codeSerial = 0;
// Un cupon ya valid (el alta en Hotmart es del worker), verificado hace un minuto.
const validCoupon = async (lead, ordinal, { seconds = 86_400 } = {}) => {
  codeSerial += 1;
  const now = await dbNow();
  const code = `ATT1RESP${String(codeSerial).padStart(4, '0')}${ordinal}`;
  await sql(`insert into public.recovery_coupons
      (recovery_case_id, ordinal, code, external_product_id, discount, expires_at, status,
       hotmart_start_ms, hotmart_end_ms, verified_at)
    values ($1, $2, $3, $4, 0.10, $5, 'valid', $6, $7, $8)`,
  [lead.plan.recovery_case_id, ordinal, code, String(A.productId), new Date(now.getTime() + seconds * 1_000),
    now.getTime() - 3_600_000, now.getTime() + seconds * 1_000, new Date(now.getTime() - 60_000)]);
  return code;
};
let messageSerial = 0;
const messageId = () => {
  messageSerial += 1;
  return 880_000 + messageSerial;
};
// Un toque por el camino real; con link, nuestro link de recuperacion antes del arranque.
const sendTouch = async (lead, actionId, label, { link = false } = {}) => {
  const reserved = await reserve(lead, actionId);
  let issuance = null;
  if (link) {
    const issued = one(await sql('select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)',
      [actionId, reserved.worker, reserved.lease, reserved.attempt.id, ulid(), reserved.now]), `${label} link`);
    if (issued.outcome !== 'reserved') fail(`${lead.label}: el link de ${label}`, issued);
    issuance = issued.issuance_id;
    const authorized = one(await sql('select * from public.authorize_recovery_checkout_issuance_v1($1,$2,$3,$4,$5)',
      [issuance, actionId, reserved.worker, reserved.lease, await dbNow()]), `${label} autorizacion del link`);
    if (authorized.outcome !== 'request_started') fail(`${lead.label}: autorizar el link de ${label}`, authorized);
  }
  const started = await startOld(reserved);
  if (started.phase !== 'request_started') fail(`${lead.label}: ${label} no arranco`, started);
  await accept(lead, reserved);
  if (issuance) {
    const finalized = one(await sql("select * from public.finalize_chatwoot_checkout_issuance_v2($1,'accepted_by_chatwoot',$2,null,now())",
      [issuance, messageId()]), `${label} cierre del link`);
    if (finalized.outcome !== 'finalized') fail(`${lead.label}: cerrar el link de ${label}`, finalized);
  }
  return reserved;
};
// Una persona con A1 (con su cupon 1 y el link) y A2 aceptados: Z queda pendiente.
const afterA2 = async (label, { coupon1 = true, seconds1 = 86_400 } = {}) => {
  const lead = await cartFor(label);
  const plan = await planCart(lead);
  if (!plan.scheduled_action_id) fail(`${label}: el carrito no planifico`, plan);
  if (coupon1) lead.code1 = await validCoupon(lead, 1, { seconds: seconds1 });
  await sendTouch(lead, plan.scheduled_action_id, 'A1', { link: coupon1 });
  await sleep(1_100);
  await sendTouch(lead, (await stepOf(lead, 'cart_second_touch')).id, 'A2');
  lead.z = (await stepOf(lead, 'cart_last_touch')).id;
  if ((await stepOfId(lead.z)).status !== 'pending') fail(`${label}: Z no quedo pendiente`);
  return lead;
};
const byReply = async (lead, { id = messageId(), at = null, phone = lead.phone } = {}) => one(await sql(
  `select * from public.${BY_REPLY.replace(/\(.*/, '')}($1,$2,$3,$4,$5,$6)`,
  [A.accountId, A.inboxId, Number(lead.chatwoot), id, phone, (at ?? await dbNow()).toISOString()]), 'por respuesta').outcome;
const lookup = async (lead, { phone = lead.phone } = {}) => sql(
  `select state, code, discount::text as discount, ordinal from public.${LOOKUP.replace(/\(.*/, '')}($1,$2,$3,$4)`,
  [A.accountId, A.inboxId, Number(lead.chatwoot), phone]);
const requests = async (lead) => one(await sql(
  'select count(*)::integer as n from public.recovery_renewal_requests where recovery_case_id = $1',
  [lead.plan.recovery_case_id]), 'pedidos').n;
// La reevaluacion de Z con la respuesta de la persona despues del ancla.
const reevaluateWithReply = async (lead) => {
  const fence = { worker: `por-respuesta-${lead.label}`, now: await dbNow() };
  await sql(`update public.scheduled_actions set due_at = least(due_at, now()), next_attempt_at = null where id = $1`, [lead.z]);
  const claimed = (await sql("select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)",
    [fence.worker, fence.now])).filter((row) => row.id === lead.z);
  if (claimed.length !== 1) fail(`${lead.label}: el claim no tomo Z`);
  const chatwoot = { ...evidence(lead, fence.now), p_chatwoot_inbound_after_anchor: true };
  const keys = Object.keys(chatwoot);
  return one(await sql(`select * from public.reevaluate_followup_action(p_action_id => $1, p_worker_id => $2,
      p_lease_generation => $3, p_now => $4, ${keys.map((k, i) => `${k} => $${i + 5}`).join(', ')})`,
  [lead.z, fence.worker, claimed[0].lease_generation, fence.now, ...keys.map((k) => chatwoot[k])]), 'reevaluacion');
};

// ---------------------------------------------------------------------------
// 1. La respuesta escrita adelanta Z como el boton.
// ---------------------------------------------------------------------------
{
  const lead = await afterA2('responde-a-a2');
  const zBefore = await stepOfId(lead.z);
  // Rojo: sin el pedido, la reevaluacion de Z con la respuesta la cancela.
  const red = await rolledBack(() => reevaluateWithReply(lead));
  if (red.decision !== 'cancel' || red.reason_code !== 'prospect_replied') fail('el rojo: sin pedido Z se cancela', red);
  // Antes de A2 no hay Z: un mensaje con fecha anterior a Z no adelanta nada.
  const early = await rolledBack(async () => {
    const outcome = await byReply(lead, { at: new Date(new Date(zBefore.created_at).getTime() - 60_000) });
    return { outcome, requests: await requests(lead), due: (await stepOfId(lead.z)).due_at };
  });
  if (early.outcome !== 'reply_before_last_touch' || early.requests !== 0
      || new Date(early.due).getTime() !== new Date(zBefore.due_at).getTime()) {
    fail('un mensaje anterior a Z', early);
  }
  const other = await rolledBack(() => byReply(lead, { phone: '5215500000777' }));
  if (other !== 'no_pending_last_touch') fail('otra persona', other);
  const first = messageId();
  const advanced = await byReply(lead, { id: first });
  const zAfter = await stepOfId(lead.z);
  if (advanced !== 'advanced' || new Date(zAfter.due_at) > await dbNow() || await requests(lead) !== 1) {
    fail('la respuesta adelanta Z', { advanced, zAfter });
  }
  const repeated = await byReply(lead, { id: first });
  const another = await byReply(lead);
  if (repeated !== 'already_requested' || another !== 'already_requested' || await requests(lead) !== 1) {
    fail('repetir no escribe otra vez', { repeated, another });
  }
  // Verde: con el pedido, la misma reevaluacion ejecuta Z.
  const green = await rolledBack(() => reevaluateWithReply(lead));
  if (green.decision !== 'execute') fail('con el pedido Z sale', green);
  results.respuesta_a_a2 = { rojo_sin_pedido: red.reason_code, mensaje_anterior_a_z: early.outcome, otra_persona: other,
    respuesta: advanced, mismo_mensaje: repeated, otro_mensaje: another, reevaluacion_con_pedido: green.decision };
}

// ---------------------------------------------------------------------------
// 2. El cupon del turno del agente.
// ---------------------------------------------------------------------------
{
  const got = {};
  // A1 salio con el cupon 1 (que vence en 6 s): el agente lo ve.
  const lead = await afterA2('cupon-del-turno', { seconds1: 6 });
  const a1 = await lookup(lead);
  if (!same(a1, [{ state: 'valid', code: lead.code1, discount: '0.100', ordinal: 1 }])) fail('el cupon 1 de A1', a1);
  got.cupon_1_entregado = a1[0].state;
  // Otra persona en la misma conversacion no lo ve.
  if ((await lookup(lead, { phone: '5215500000777' })).length !== 0) fail('otra persona ve el cupon');
  // El cupon 1 vencido: nada (A2 dice que vencio).
  await sleep(6_500);
  const expired = await lookup(lead);
  if (expired.length !== 0) fail('el cupon vencido', expired);
  got.cupon_1_vencido = 'sin_filas';
  // El cupon 2 sembrado y valid antes de Z, sin pedido: no salio, no cuenta.
  const code2 = await validCoupon(lead, 2, { seconds: 8 });
  if ((await lookup(lead)).length !== 0) fail('el cupon 2 sin Z cuenta');
  got.cupon_2_sin_salir = 'sin_filas';
  // La persona contesta: la renovacion esta en camino.
  if (await byReply(lead) !== 'advanced') fail('la respuesta no adelanto Z');
  const inProgress = await lookup(lead);
  if (!same(inProgress, [{ state: 'renewal_in_progress', code: null, discount: null, ordinal: 2 }])) {
    fail('la renovacion en camino', inProgress);
  }
  got.con_el_pedido = inProgress[0].state;
  // Z sale con su link: el agente ve el cupon 2.
  await sendTouch(lead, lead.z, 'Z', { link: true });
  const z = await lookup(lead);
  if (!same(z, [{ state: 'valid', code: code2, discount: '0.100', ordinal: 2 }])) fail('el cupon 2 de Z', z);
  got.cupon_2_entregado = z[0].state;
  // Con la compra (el caso con purchase_event_id), nada.
  const bought = await rolledBack(async () => {
    // El evento del carrito de la persona hace de compra: solo importa que el caso quede ganado.
    await sql("update public.recovery_cases set purchase_event_id = $2, status = 'won', won_at = now() where id = $1",
      [lead.plan.recovery_case_id, lead.event]);
    return lookup(lead);
  });
  if (bought.length !== 0) fail('con la compra', bought);
  got.con_la_compra = 'sin_filas';
  // Nunca un tercero: con los dos vencidos, nada, y la renovacion no encuentra otro Z.
  await sleep(8_500);
  const after = { rows: await lookup(lead), renewal: await byReply(lead),
    coupons: one(await sql('select count(*)::integer as n from public.recovery_coupons where recovery_case_id = $1',
      [lead.plan.recovery_case_id]), 'cupones').n };
  if (after.rows.length !== 0 || after.renewal !== 'no_pending_last_touch' || after.coupons !== 2) {
    fail('despues de Z no hay un tercer codigo', after);
  }
  got.sin_tercer_codigo = { filas: after.rows.length, renovacion: after.renewal, cupones_del_caso: after.coupons };

  // A1 sin cupon (cayo a la plantilla de hoy) y un cupon 1 valid despues: no salio.
  const noA1 = await afterA2('sin-cupon-en-a1', { coupon1: false });
  await validCoupon(noA1, 1);
  if ((await lookup(noA1)).length !== 0) fail('un cupon que A1 no llevo');
  got.cupon_que_a1_no_llevo = 'sin_filas';
  results.cupon_del_turno = got;
}

console.log(JSON.stringify({ ok: true, migration: MIGRATION, ...results }, null, 2));
