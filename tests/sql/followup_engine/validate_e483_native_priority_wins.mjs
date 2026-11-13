// `prioridad.le_gana_a` en el motor nativo (migracion 20261112210200, E-483,
// C7 del mapa «el AE maneja todos los flujos», 2026-11-12).
//
// Por que: el nativo leia solo no_arranca_mientras. La regla de ATT1 desde el
// 18/10 (D4): el pago rechazado le gana a un carrito en curso, lo corta. Con
// varios toques (20261112210000) un carrito nativo en curso tiene A2 esperando.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (con el carrito
// capturado hotmart_cart_abandonment_rejected_v1.json), los rechazos capturados
// de Maribel (tests/fixtures/hotmart_purchase_canceled_maribel_20261028.json)
// por la admision portable de hoy, como validate_flow_engine_native_payment_failure.mjs,
// y la plantilla de Maribel
// (tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json).
//
// Prueba:
//   0. la forma: cambia solo admit_flow_engine_event_v1 con su marca, se suma
//      _flow_engine_native_priority_wins_v1 sin permisos para la API; correrla
//      dos veces aborta;
//   1. sin la clave del bridge (el flag apagado): Lucia con el carrito de tres
//      toques (A1 mandado, A2 esperando) y un pago rechazado: los dos siguen;
//   2. rojo: con la admision de antes y la clave, A2 sigue esperando;
//   3. con la clave: Maria con A2 esperando y un pago rechazado nativo con
//      le_gana_a: [carrito_tres] -> A2 cancelada (le_gana_otro_flujo), su caso y
//      su secuencia cerrados, Z no se agenda, el pago rechazado arranca en OTRO
//      caso (medido: son dos recovery_cases); Ana, con su carrito, no se toca;
//      la repeticion del evento no corta de nuevo;
//   4. un toque mandandose (Juana, A2 reservado) no se cancela: el caso sigue y
//      el del pago lo anota (mandandose: 1);
//   5. el legado: Elena con el carrito del legado por la ruta motor (un
//      recorrido con legacy_kind) y le_gana_a: [carrito] no se toca;
//   6. sin le_gana_a (la v2 del pago): Sofia con el carrito y el pago, los dos
//      siguen como hoy.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261112210200_e483_native_priority_wins_v1.sql';
const ADMIT_EVENT = 'admit_flow_engine_event_v1(jsonb,jsonb)';
const CHANGED = [ADMIT_EVENT];
const ADDED = ['_flow_engine_native_priority_wins_v1(uuid,uuid,timestamp with time zone)'];
const maribel = JSON.parse(readFileSync(join(root,
  'tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json'), 'utf8')).message_templates[0];
const REFUSALS = JSON.parse(readFileSync(join(root,
  'tests/fixtures/hotmart_purchase_canceled_maribel_20261028.json'), 'utf8')).eventos;
const results = {};
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues va el pago rechazado completo en el nativo (20261112210300, E-483, C3): no toca la admision.
// Y el pago rechazado sin formulario (20261112210400, E-483, pieza PF): toca admit_flow_engine_event_v1 en
// otro lugar (antes y despues del bucle de flujos), sin tocar el bloque de esta.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261112210300_e483_native_condition_after_send_v1.sql',
  '20261112210400_e483_payment_failure_by_phone_v1.sql',
  // El cupon propio por persona en el nativo (20261112210500, E-483, C2).
  '20261112210500_e483_native_personal_coupon_v1.sql',
  // El evento de un disparo por audiencia en el nativo (20261112210600, E-483, para E-484).
  '20261112210600_e483_native_audience_source_v1.sql'])) {
  fail(`despues de ${MIGRATION} van solo la de C3 y la de PF`, names.slice(position + 1));
}

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const previousDefinitions = {};
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
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.has(s) && after.get(s).definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && ['acl', 'prosecdef', 'provolatile'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, CHANGED) || !sameSet(added, ADDED) || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  for (const signature of CHANGED) {
    if (!after.get(signature).definition.includes('e483_native_priority_wins')) fail(`${signature}: sin la marca`);
    previousDefinitions[signature] = before.get(signature).definition;
  }
  const privileges = (await db.query(`select r.rolname, has_function_privilege(r.rolname, $1, 'EXECUTE') as can
    from pg_roles r where r.rolname in ('anon', 'authenticated', 'service_role') order by r.rolname`,
  [`public.${ADDED[0]}`])).rows;
  if (privileges.some((row) => row.can) || after.get(ADDED[0]).prosecdef) fail('la funcion nueva no es para la API', privileges);
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'e483_native_priority_wins_already_applied') fail('la segunda corrida aborto por otra cosa', error.message);
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { cambiadas: changed.length, agregadas: added.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, FLOWS, SCOPE, INSTANCE, register, cycle, versionItem, reserve, startNew, accept, cartFor, dbNow,
  admit, createRoute, route } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
await db.exec('alter role service_role bypassrls');
const asService = async (run) => {
  await db.exec('set role service_role');
  try {
    return await run();
  } finally {
    await db.exec('reset role');
  }
};
const send = (variables = ['nombre', 'producto']) => ({ plantilla: maribel.name, idioma: maribel.language, variables });
const CART = {
  esquema: 'setter-flujo/v1', flujo: 'carrito_tres', producto: A.funnel, estado: 'borrador', nombre: 'Carrito de tres toques',
  dispara: { evento: 'carrito' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [send(), { esperar: '1m' }, send(['nombre']), { esperar: '2m' }, send()],
  termina: { si: ['compra', 'contesta'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-carrito-tres', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
// El pago rechazado de Maribel (validate_flow_engine_native_payment_failure.mjs) con horario siempre, que le gana
// al carrito nativo y, para el punto 5, al carrito del legado por su clave.
const PAYMENT = {
  esquema: 'setter-flujo/v1', flujo: 'pago_rechazado', producto: A.funnel, estado: 'borrador', nombre: 'Pago rechazado',
  dispara: { evento: 'pago_fallido' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ esperar: '15m' }, send()],
  termina: { si: ['compra'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-pago-rechazado-nativo', por_dia: 20, total: 1000,
    repeticion: { una_por: 'persona', otra_despues_de: '72h' } },
  prioridad: { le_gana_a: ['carrito_tres', 'carrito'], no_arranca_mientras: [] },
};
const openNative = async (flowKey, definition) => {
  const flowId = await register(A.funnel, flowKey);
  await cycle([versionItem(flowId, A.funnel, flowKey, definition)], `v1 de ${flowKey}`);
  const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'propuesta').proposal_id;
  const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [flowId, proposalId, A.timezone]), 'abrir').r);
  if (!opened.ok || opened.state !== 'cohorte') fail(`abrir ${flowKey}`, opened);
  return flowId;
};
const cartFlow = await openNative('carrito_tres', CART);
const payFlow = await openNative('pago_rechazado', PAYMENT);
const WITH_KEY = { ...INSTANCE, native_priority_wins: true };

const admitEvent = async (instance, flowEvent, flowId, label) => one((await asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
  [JSON.stringify(instance), JSON.stringify(flowEvent)])).rows)).filter((row) => row.flow_id === flowId), label);
const eventOf = (lead, tipo, evento, momento) => ({
  fuente: 'hotmart', tipo, evento_id: evento, producto_ref: A.funnel, oferta: lead.offer.offer_code,
  contacto_id: lead.contact, momento, chatwoot: { account_id: A.accountId, inbox_id: A.inboxId }, destino: lead.phone,
});
const actionsOf = async (caseId) => sql(`select id, step_key, status, terminal_reason, lease_owner
  from public.scheduled_actions where recovery_case_id = $1 order by created_at`, [caseId]);
const caseRow = async (caseId) => one(await sql(`select status, next_contact_reason, context
  from public.recovery_cases where id = $1`, [caseId]), 'caso');

// Una persona con el carrito nativo de tres toques: A1 mandado y A2 esperando.
const withCart = async (label) => {
  const lead = await cartFor(label);
  for (const flowId of [cartFlow, payFlow]) {
    await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
      [flowId, lead.contact]);
  }
  const started = await admitEvent(INSTANCE, eventOf(lead, 'carrito', lead.event, lead.cartArgs[6]), cartFlow, `${label} carrito`);
  if (started.outcome !== 'motor_arranco') fail(`${label}: el carrito arranca`, started);
  lead.cartCase = started.recovery_case_id;
  const reserved = await reserve(lead, started.scheduled_action_id);
  if ((await startNew(reserved)).outcome !== 'request_started') fail(`${label}: el arranque de A1`);
  await accept(lead, reserved);
  const second = (await actionsOf(lead.cartCase)).find((row) => row.step_key === 'native_touch_2');
  if (!second || second.status !== 'pending') fail(`${label}: A2 agendado`, await actionsOf(lead.cartCase));
  lead.a2 = second.id;
  return lead;
};
let refusalSerial = 0;
// Un rechazo capturado de Maribel con la persona sembrada, por la admision portable de hoy.
const refusalOf = async (lead) => {
  refusalSerial += 1;
  const payload = structuredClone(REFUSALS[0].payload);
  const moment = new Date((await dbNow()).getTime() - 10 * 60_000);
  payload.id = `prioridad-rechazo-${lead.email}-${refusalSerial}`;
  payload.creation_date = moment.getTime();
  payload.data.buyer = { ...payload.data.buyer, name: lead.name, email: lead.email, checkout_phone: lead.phone };
  payload.data.product = { ...payload.data.product, id: A.productId, name: A.productName };
  payload.data.purchase.offer.code = lead.offer.offer_code;
  payload.data.purchase.transaction = `HPPRIORIDAD${String(refusalSerial).padStart(4, '0')}`;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone]), `${lead.label} rechazo`);
  if (admitted.outcome !== 'inserted') fail(`${lead.label} rechazo`, admitted);
  lead.refusal = admitted.webhook_event_id;
  lead.refusalAt = moment.toISOString();
  return lead;
};
const pay = async (lead, instance) => admitEvent(instance, eventOf(lead, 'pago_fallido', lead.refusal, lead.refusalAt),
  payFlow, `${lead.label} pago`);
const a2Of = async (lead) => (await actionsOf(lead.cartCase)).find((row) => row.id === lead.a2);
const untouched = async (lead, label) => {
  const a2 = await a2Of(lead);
  const kase = await caseRow(lead.cartCase);
  if (a2.status !== 'pending' || a2.terminal_reason !== null || kase.status !== 'active' || kase.context?.closed_by_priority) {
    fail(`${label}: el carrito sigue`, { a2, kase });
  }
  return { a2: a2.status, caso: kase.status };
};

// ---------------------------------------------------------------------------
// 1. Sin la clave del bridge (el flag apagado): los dos siguen.
// ---------------------------------------------------------------------------
{
  const lucia = await refusalOf(await withCart('prioridad-lucia'));
  const started = await pay(lucia, INSTANCE);
  if (started.outcome !== 'motor_arranco' || started.created !== true) fail('Lucia: el pago arranca', started);
  results.sin_clave = { pago: started.outcome, carrito: await untouched(lucia, 'Lucia sin la clave') };
}

// ---------------------------------------------------------------------------
// 2. Rojo: con la admision de antes, la clave no corta nada.
// ---------------------------------------------------------------------------
const rita = await refusalOf(await withCart('prioridad-rita'));
await db.exec('begin');
try {
  await db.exec(previousDefinitions[ADMIT_EVENT]);
  const started = await pay(rita, WITH_KEY);
  if (started.outcome !== 'motor_arranco') fail('Rita: el pago arranca con la admision de antes', started);
  results.rojo = { pago: started.outcome, carrito: await untouched(rita, 'Rita con la admision de antes') };
} finally {
  await db.exec('rollback');
}

// ---------------------------------------------------------------------------
// 3. Con la clave: el pago rechazado corta el carrito de Maria, no el de Ana.
// ---------------------------------------------------------------------------
{
  const maria = await refusalOf(await withCart('prioridad-maria'));
  const ana = await withCart('prioridad-ana');
  const started = await pay(maria, WITH_KEY);
  if (started.outcome !== 'motor_arranco' || started.created !== true) fail('Maria: el pago arranca', started);
  // El doble caso, medido: el pago rechazado nativo abre su propio caso; el del carrito es otro.
  if (started.recovery_case_id === maria.cartCase) fail('el pago y el carrito comparten el caso', started);
  const payCase = await caseRow(started.recovery_case_id);
  const a2 = await a2Of(maria);
  const kase = await caseRow(maria.cartCase);
  const actions = await actionsOf(maria.cartCase);
  const sequences = await sql('select status, cancel_reason from public.followup_sequences where recovery_case_id = $1',
    [maria.cartCase]);
  const closed = await sql(`select data from public.conversation_events
    where recovery_case_id = $1 and event_type = 'recovery_case_closed_by_priority'`, [maria.cartCase]);
  const won = await sql(`select data from public.conversation_events
    where recovery_case_id = $1 and event_type = 'flow_engine_native_priority_won'`, [started.recovery_case_id]);
  if (a2.status !== 'cancelled' || a2.terminal_reason !== 'le_gana_otro_flujo') fail('A2 de Maria cancelada', a2);
  if (kase.status !== 'cancelled' || kase.next_contact_reason !== 'le_gana_otro_flujo'
      || kase.context?.closed_by_priority?.winner_case_id !== started.recovery_case_id
      || kase.context?.closed_by_priority?.flow_key !== 'carrito_tres') fail('el caso del carrito de Maria', kase);
  if (sequences.length === 0 || sequences.some((row) => row.status !== 'cancelled' || row.cancel_reason !== 'le_gana_otro_flujo')) {
    fail('las secuencias del carrito de Maria', sequences);
  }
  if (actions.some((row) => row.step_key === 'native_touch_3')) fail('Z no se agenda', actions);
  if (actions.some((row) => ['pending', 'deferred', 'retryable_failed', 'delivery_unknown'].includes(row.status))) {
    fail('al carrito de Maria no le queda nada vivo', actions);
  }
  if (closed.length !== 1 || won.length !== 1 || won[0].data.cerrados !== 1 || won[0].data.mandandose !== 0) {
    fail('los eventos del corte', { closed, won });
  }
  if (!['grace_period', 'active'].includes(payCase.status)) fail('el caso del pago sigue', payCase);
  const anaNow = await untouched(ana, 'Ana, otra persona');
  // La repeticion del mismo evento devuelve su caso y no corta otra vez.
  const replay = await pay(maria, WITH_KEY);
  const wonAgain = await sql(`select 1 from public.conversation_events
    where recovery_case_id = $1 and event_type = 'flow_engine_native_priority_won'`, [started.recovery_case_id]);
  if (replay.created !== false || replay.recovery_case_id !== started.recovery_case_id || wonAgain.length !== 1) {
    fail('la repeticion del evento', { replay, wonAgain });
  }
  results.con_clave = { a2_maria: `${a2.status}:${a2.terminal_reason}`, caso_carrito: kase.status,
    caso_pago: payCase.status, casos_distintos: true, ana: anaNow };
}

// ---------------------------------------------------------------------------
// 4. Un toque mandandose no se cancela.
// ---------------------------------------------------------------------------
{
  const juana = await refusalOf(await withCart('prioridad-juana'));
  await sql("update public.scheduled_actions set due_at = now() - interval '1 second' where id = $1", [juana.a2]);
  const reserved = await reserve(juana, juana.a2);
  if (reserved.attempt.phase !== 'reserved') fail('el intento de A2 de Juana', reserved.attempt);
  const started = await pay(juana, WITH_KEY);
  if (started.outcome !== 'motor_arranco') fail('Juana: el pago arranca', started);
  const a2 = await a2Of(juana);
  const kase = await caseRow(juana.cartCase);
  if (a2.status !== 'pending' || a2.lease_owner === null || kase.status !== 'active' || kase.context?.closed_by_priority) {
    fail('el envio en curso de Juana no se toca', { a2, kase });
  }
  const won = one(await sql(`select data from public.conversation_events
    where recovery_case_id = $1 and event_type = 'flow_engine_native_priority_won'`, [started.recovery_case_id]), 'anotado').data;
  if (won.cerrados !== 0 || won.mandandose !== 1) fail('el pago anota el envio en curso', won);
  results.mandandose = { a2: a2.status, caso: kase.status, anotado: { cerrados: won.cerrados, mandandose: won.mandandose } };
}

// ---------------------------------------------------------------------------
// 5. El legado: el carrito del legado por la ruta motor no se toca.
// ---------------------------------------------------------------------------
{
  const created = await createRoute(A.funnel, 'carrito', 'carrito', SCOPE);
  if (!created.ok) fail('la ruta del carrito del legado', created);
  const moved = await route(A.funnel, 'carrito', FLOWS.carrito, 'motor');
  if (!moved.ok) fail('la ruta del carrito del legado a motor', moved);
  const elena = await cartFor('prioridad-elena');
  await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
    [payFlow, elena.contact]);
  const row = await admit(elena);
  if (row.outcome !== 'motor_arranco') fail('Elena: el carrito del legado por el motor', row);
  const legacyRun = one(await sql('select legacy_kind from public.flow_engine_runs where recovery_case_id = $1',
    [row.recovery_case_id]), 'recorrido del legado');
  if (legacyRun.legacy_kind !== 'carrito') fail('el recorrido es del legado', legacyRun);
  await refusalOf(elena);
  const started = await pay(elena, WITH_KEY);
  if (started.outcome !== 'motor_arranco') fail('Elena: el pago arranca', started);
  const legacyCase = await caseRow(row.recovery_case_id);
  const live = (await actionsOf(row.recovery_case_id)).filter((action) => action.status === 'pending');
  if (!['grace_period', 'active'].includes(legacyCase.status) || legacyCase.context?.closed_by_priority || live.length === 0) {
    fail('el carrito del legado no se toca', { legacyCase, live });
  }
  results.legado = { caso: legacyCase.status, pendientes: live.length };
}

// ---------------------------------------------------------------------------
// 6. Sin le_gana_a: los dos siguen como hoy.
// ---------------------------------------------------------------------------
{
  await cycle([versionItem(payFlow, A.funnel, 'pago_rechazado',
    { ...PAYMENT, prioridad: { le_gana_a: [], no_arranca_mientras: [] } })], 'v2 del pago sin le_gana_a');
  const sofia = await refusalOf(await withCart('prioridad-sofia'));
  const started = await pay(sofia, WITH_KEY);
  if (started.outcome !== 'motor_arranco') fail('Sofia: el pago arranca', started);
  const version = one(await sql('select version from public.flow_engine_runs where recovery_case_id = $1',
    [started.recovery_case_id]), 'recorrido').version;
  if (version !== 2) fail('Sofia corre la v2', version);
  results.sin_le_gana_a = { pago: started.outcome, carrito: await untouched(sofia, 'Sofia sin le_gana_a') };
}

console.log(JSON.stringify(results, null, 2));
await db.close();
