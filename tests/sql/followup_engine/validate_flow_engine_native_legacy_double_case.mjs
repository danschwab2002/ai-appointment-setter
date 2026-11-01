// Un carrito nunca abre un caso nativo y uno del legado para la misma persona
// y el mismo producto (migracion 20261030000100, E-204, M1a, P12 del mapa de
// M16).
//
// Por que: el caso de un carrito nativo nace con offer_code nulo, la admision
// nativa miraba solo flow_engine_runs y el planificador del legado busca por
// oferta. Con un caso abierto de un lado y un carrito nuevo de la misma
// persona, el otro lado abria un segundo caso y mandaba un segundo mensaje.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera, el formulario y el carrito por las admisiones portables, el
// planificador del piloto del legado como en produccion), con el carrito
// nativo de un toque de validate_flow_engine_native_cart.mjs y la plantilla de
// Maribel. ATT1 tiene dos ofertas en su manifiesto: el carrito de otra oferta
// es real.
//
// Prueba:
//   0. la forma: cambian exactamente _flow_engine_admit_native_flow y
//      plan_cart_recovery, sin funciones nuevas ni otros atributos; correrla
//      dos veces aborta;
//   1. (a) un caso del legado abierto (grace_period, y paused) y un carrito
//      nuevo de la misma persona, de la misma oferta y de otra: el nativo no
//      arranca (caso_del_legado_en_curso, con su fila de rechazo) y el
//      planificador del legado hace exactamente lo de hoy (la misma oferta
//      aborta con pilot_case_binding_conflict, la otra planifica su caso);
//      rojo: con la admision de antes, el nativo abria el segundo caso. Con el
//      caso del legado cerrado, el nativo arranca;
//   2. (b) un caso nativo del carrito abierto y el nativo pausado (la vuelta
//      atras): el legado no abre otro caso, de ninguna oferta
//      (pilot_scope_rejected / flujo_nativo_en_curso, y nada escrito); rojo:
//      con el planificador de antes abria el segundo caso. Con el caso nativo
//      cerrado, el legado planifica;
//   3. ATT1 de hoy no cambia: con la bienvenida nativa (compra) abierta, el
//      carrito del legado de esa persona planifica igual que antes; y tres
//      recorridos del legado (un carrito, el segundo de la misma oferta, la
//      repeticion del mismo evento) dan lo mismo con las dos definiciones;
//   3b. ATT1 como factura hoy, con el carrito por la ruta motor (E-206): sus
//      casos son flujo:<id> sin flow_engine_trigger, y un segundo carrito de la
//      misma persona (misma oferta y otra) da lo mismo con las dos
//      definiciones; si (b) mirara cualquier caso flujo:% que no sea compra,
//      el de otra oferta caeria en flujo_nativo_en_curso (rojo verificado);
//   4. el primer contacto del legado lo corta el caso nativo (C5):
//      _portable_precheckout_stop_reason, el freno que usa su reevaluacion, da
//      superseded_by_provider_event; con la clasificacion de la intencion
//      borrada lo sigue dando por el caso nativo, y con el caso nativo cerrado
//      deja de darlo (rojo);
//   5. la baja corta un recorrido nativo con la accion pendiente: el contacto
//      dado de baja cancela en la reevaluacion (contact_blocked), y la baja de
//      Chatwoot despues de reservar rechaza el arranque (baja). La compra la
//      prueba validate_flow_engine_native_cart.mjs (seccion 3); la toma
//      humana, la derivacion y la pausa por compra de un primer toque se ven
//      en Chatwoot y las corta el envio (tests/test_flow_engine_native_send_cuts.py).
//
// Con --emit escribe tests/fixtures/flow_engine/native_legacy_double_case_rows.json
// (la fila de la admision y el error del planificador, como los contesta la
// base) para tests/test_flow_engine_native_legacy_double_case.py; con --check
// (el de npm test) lo compara y falla si difiere.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, errorOf, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261030000100_flow_engine_native_legacy_double_case_v1.sql';
const FIXTURE = join(root, 'tests/fixtures/flow_engine/native_legacy_double_case_rows.json');
const EMIT = process.argv.includes('--emit');
const CHECK = process.argv.includes('--check');
const ADMIT_NATIVE = '_flow_engine_admit_native_flow(flow_controls,webhook_events,uuid,text,timestamp with time zone,bigint,bigint,text)';
const PLAN_BASE = 'plan_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamp with time zone)';
const CHANGED = [ADMIT_NATIVE, PLAN_BASE];
const maribel = JSON.parse(readFileSync(join(root,
  'tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json'), 'utf8')).message_templates[0];
const purchaseCaptured = JSON.parse(readFileSync(join(root,
  'tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json'), 'utf8')).payload;
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues va el pago rechazado nativo (E-207), que toca las mismas dos funciones por reemplazo contado. Y despues, la lista de las propuestas (E-208) y los frenos de la compra con nuestro link (E-215).
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261030000200_flow_engine_native_payment_failure_v1.sql', '20261030000300_flow_change_list_v1.sql', '20261030000400_portable_purchase_stops_see_our_link_v1.sql', '20261101000100_flow_engine_native_intent_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la del pago rechazado nativo`, names.slice(position + 1));
}

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile, p.proowner
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const previous = {};
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
    && ['acl', 'prosecdef', 'provolatile', 'proowner'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, CHANGED) || added.length !== 0 || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  for (const signature of CHANGED) {
    const definition = after.get(signature).definition;
    if ((definition.match(/flow_engine_native_legacy_double_case: begin/g) ?? []).length !== 1) fail(`${signature}: sin la marca`);
    // Lo de antes queda entero: el reemplazo solo agrega el bloque.
    const block = definition.slice(definition.indexOf('    -- flow_engine_native_legacy_double_case: begin'),
      definition.indexOf('    -- flow_engine_native_legacy_double_case: end') + '    -- flow_engine_native_legacy_double_case: end\n\n'.length);
    if (definition.replace(block, '') !== before.get(signature).definition) fail(`${signature}: cambio algo mas que el bloque`);
    previous[signature] = before.get(signature).definition;
  }
  let error = null;
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  } catch (caught) {
    error = caught;
  }
  if (error?.message !== 'flow_engine_native_legacy_double_case_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  await db.exec('rollback').catch(() => {});
  if ((await snapshot(db)).get(PLAN_BASE).definition !== after.get(PLAN_BASE).definition) fail('la segunda corrida cambio algo');
  results.forma = { cambiadas: changed.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 con un carrito nativo en cohorte.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, stateItem, pauseFlow, dbNow, reserve, startNew, cartFor, planCart,
  PLAN_CART } = world;
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
const rolledBack = async (run) => {
  await db.exec('begin');
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};
// Con las definiciones de antes de esta migracion, adentro de una transaccion que se deshace.
const withPrevious = async (signatures, run) => rolledBack(async () => {
  for (const signature of signatures) await db.exec(previous[signature]);
  return run();
});

const CART = {
  esquema: 'setter-flujo/v1', flujo: 'carrito_nativo', producto: A.funnel, estado: 'borrador', nombre: 'Carrito nativo',
  dispara: { evento: 'carrito' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ plantilla: maribel.name, idioma: maribel.language, variables: ['nombre', 'producto'] }],
  termina: { si: ['compra'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-carrito-nativo', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
const openNative = async (flowKey, definition, extra = {}) => {
  const flowId = await register(A.funnel, flowKey);
  await cycle([{ ...versionItem(flowId, A.funnel, flowKey, definition), ...extra }], `v1 de ${flowKey}`);
  const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'propuesta').proposal_id;
  const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [flowId, proposalId, A.timezone]), 'abrir').r);
  if (!opened.ok) fail(`abrir ${flowKey}`, opened);
  return { flowId, opened };
};
const cart = await openNative('carrito_nativo', CART);
const cohort = async (flowId, contactId) => sql(
  `insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`, [flowId, contactId]);
const flowEventOf = (lead) => ({
  fuente: 'hotmart', tipo: 'carrito', evento_id: lead.event, producto_ref: A.funnel, oferta: lead.offer.offer_code,
  contacto_id: lead.contact, momento: lead.cartArgs[6], chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
  destino: lead.phone,
});
const admitNative = async (lead) => asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)', [JSON.stringify(INSTANCE), JSON.stringify(flowEventOf(lead))])).rows);
const cartRow = (rows, label) => one(rows.filter((row) => row.flow_id === cart.flowId), label);
const casesOf = async (lead) => sql(`select id, status, offer_code, policy_key from public.recovery_cases
  where contact_id = $1 order by created_at`, [lead.contact]);
const rejectionsOf = async (lead) => sql(`select reason from public.flow_engine_start_rejections
  where flow_id = $1 and source_ref = $2`, [cart.flowId, lead.event]);
// El error del planificador, como lo ve PostgREST (code, message, details).
const planError = async (lead) => {
  const error = await errorOf(db, () => db.query(PLAN_CART, lead.cartArgs));
  return error === null ? null : { code: error.code, message: error.message, details: error.detail ?? null };
};
// Una sola llamada al planificador: el caso que planifica, o el error.
const planOutcome = async (lead) => {
  let plan = null;
  const error = await errorOf(db, async () => {
    plan = one((await db.query(PLAN_CART, lead.cartArgs)).rows, `${lead.label} plan`);
  });
  return error === null ? { created: plan.created } : { error: { code: error.code, message: error.message } };
};
const otherOffer = A.offers.find((offer) => offer.offer_code !== A.offers.at(-1).offer_code);
if (!otherOffer) fail('ATT1 tiene una sola oferta en el manifiesto');
const emitted = {};

// ---------------------------------------------------------------------------
// 1. (a) Un caso del legado abierto: el nativo no arranca y el legado hace lo de hoy.
// ---------------------------------------------------------------------------
{
  const ana = await cartFor('doble-legado');
  await cohort(cart.flowId, ana.contact);
  // El primer carrito lo planifica el legado (el nativo todavia no existia para ella: se simula
  // con el planificador directo, como cuando el carrito llego antes de la cohorte).
  const first = await planCart(ana);
  if (first.created !== true) fail('el legado planifica el primer carrito', first);
  const legacyCase = first.recovery_case_id;
  const status = one(await sql('select status from public.recovery_cases where id = $1', [legacyCase]), 'caso').status;
  if (status !== 'grace_period') fail('el caso del legado nace en grace_period', status);

  // Un segundo carrito, de la misma oferta.
  await cartFor(ana);
  const same = cartRow(await admitNative(ana), 'misma oferta');
  if (same.outcome !== 'motor_no_arranca' || same.reason !== 'caso_del_legado_en_curso' || same.recovery_case_id !== null) {
    fail('el nativo no arranca sobre el caso del legado', same);
  }
  const sameRejections = await rejectionsOf(ana);
  if (sameRejections.length !== 1 || sameRejections[0].reason !== 'caso_del_legado_en_curso') fail('la fila de rechazo', sameRejections);
  // El legado, como hoy: la misma oferta suma al caso abierto, y el piloto aborta con el binding.
  const sameToday = await withPrevious([ADMIT_NATIVE, PLAN_BASE], () => planError(ana));
  const sameNow = await planError(ana);
  if (JSON.stringify(sameNow) !== JSON.stringify(sameToday) || sameNow?.message !== 'pilot_case_binding_conflict') {
    fail('el legado con la misma oferta, como hoy', { sameNow, sameToday });
  }
  // Rojo: con la admision de antes, el nativo abre un segundo caso.
  const red = await withPrevious([ADMIT_NATIVE], async () => {
    const row = cartRow(await admitNative(ana), 'admision de antes');
    return { row, cases: (await casesOf(ana)).length };
  });
  if (red.row.outcome !== 'motor_arranco' || red.cases !== 2) fail('sin la migracion, el doble caso', red);
  emitted.admission = { outcome: same.outcome, reason: same.reason, created: same.created };
  emitted.legacy_same_offer = sameNow;

  // Un tercer carrito, de otra oferta: el nativo tampoco (ignora la oferta) y el legado planifica su caso como hoy.
  ana.offer = otherOffer;
  await cartFor(ana);
  const other = cartRow(await admitNative(ana), 'otra oferta');
  if (other.outcome !== 'motor_no_arranca' || other.reason !== 'caso_del_legado_en_curso') fail('otra oferta', other);
  const otherToday = await withPrevious([ADMIT_NATIVE, PLAN_BASE], async () => {
    const plan = one((await db.query(PLAN_CART, ana.cartArgs)).rows, 'plan de antes');
    return { created: plan.created, offers: (await casesOf(ana)).map((row) => row.offer_code) };
  });
  const otherPlan = one((await db.query(PLAN_CART, ana.cartArgs)).rows, 'plan');
  const otherNow = { created: otherPlan.created, offers: (await casesOf(ana)).map((row) => row.offer_code) };
  if (JSON.stringify(otherNow) !== JSON.stringify(otherToday) || otherNow.created !== true
      || otherNow.offers.some((offer) => offer === null)) {
    fail('el legado con otra oferta, como hoy', { otherNow, otherToday });
  }

  // paused tambien es abierto (una derivacion del caso): el nativo no arranca.
  const bea = await cartFor('doble-legado-pausado');
  await cohort(cart.flowId, bea.contact);
  const beaFirst = await planCart(bea);
  await sql(`update public.recovery_cases set status = 'paused' where id = $1`, [beaFirst.recovery_case_id]);
  await cartFor(bea);
  const paused = cartRow(await admitNative(bea), 'legado pausado');
  if (paused.reason !== 'caso_del_legado_en_curso') fail('con el caso del legado pausado', paused);

  // Cerrado, no: el nativo arranca.
  await sql(`update public.recovery_cases set status = 'cancelled', closed_at = now() where id = $1`, [beaFirst.recovery_case_id]);
  const closed = cartRow(await admitNative(bea), 'legado cerrado');
  if (closed.outcome !== 'motor_arranco') fail('con el caso del legado cerrado, el nativo arranca', closed);
  results.a = { misma_oferta: same.reason, legado_misma_oferta: sameNow.message, otra_oferta: other.reason,
    legado_otra_oferta: otherNow.created, pausado: paused.reason, cerrado: closed.outcome, sin_la_migracion: red.row.outcome };
}

// ---------------------------------------------------------------------------
// 2. (b) Un caso nativo abierto y el nativo pausado: el legado no abre otro.
// ---------------------------------------------------------------------------
let nativeCaseOfCarla = null;
let carla = null;
{
  carla = await cartFor('doble-nativo');
  await cohort(cart.flowId, carla.contact);
  const started = cartRow(await admitNative(carla), 'el carrito nativo');
  if (started.outcome !== 'motor_arranco') fail('el carrito nativo arranca', started);
  nativeCaseOfCarla = started.recovery_case_id;
  carla.nativeAction = started.scheduled_action_id;
  await pauseFlow(cart.flowId, A.funnel, 'carrito_nativo');
  // Un segundo carrito de la misma oferta: el nativo pausado no lo toma (el bridge lo manda al legado).
  await cartFor(carla);
  const refused = cartRow(await admitNative(carla), 'nativo pausado');
  if (refused.reason !== 'flujo_pausado') fail('el nativo pausado', refused);
  const before = (await casesOf(carla)).length;
  const sameOffer = await planError(carla);
  if (sameOffer?.code !== '55000' || sameOffer.message !== 'pilot_scope_rejected' || sameOffer.details !== 'flujo_nativo_en_curso') {
    fail('el legado ve el caso nativo', sameOffer);
  }
  if ((await casesOf(carla)).length !== before) fail('el legado no escribio nada');
  // Rojo: con el planificador de antes, el legado abre el segundo caso.
  const red = await withPrevious([PLAN_BASE], async () => {
    const plan = one((await db.query(PLAN_CART, carla.cartArgs)).rows, 'plan de antes');
    return { created: plan.created, cases: (await casesOf(carla)).length };
  });
  if (red.created !== true || red.cases !== before + 1) fail('sin la migracion, el doble caso', red);
  // Otra oferta, lo mismo.
  carla.offer = otherOffer;
  await cartFor(carla);
  const otherOfferError = await planError(carla);
  if (otherOfferError?.details !== 'flujo_nativo_en_curso') fail('el legado ve el caso nativo de otra oferta', otherOfferError);
  emitted.legacy_over_native = sameOffer;
  results.b = { misma_oferta: sameOffer.details, otra_oferta: otherOfferError.details, sin_la_migracion: red.created };
  // De vuelta a correr (prendido): lo que sigue usa el carrito nativo.
  await cycle([stateItem(cart.flowId, A.funnel, 'carrito_nativo', 'prender')], 'prender el carrito nativo');
}

// ---------------------------------------------------------------------------
// 3. ATT1 de hoy no cambia.
// ---------------------------------------------------------------------------
{
  // La bienvenida nativa de la compra (la que ATT1 tiene): su caso abierto no frena el carrito del legado.
  const WELCOME = {
    esquema: 'setter-flujo/v1', flujo: 'bienvenida', producto: A.funnel, estado: 'borrador', nombre: 'Bienvenida att1',
    dispara: { evento: 'compra' }, entra: { permiso: 'compra', contesto_a: [] },
    pasos: [{ plantilla: 'att1_bienvenida_01', idioma: 'es_MX', variables: ['producto'] }, { etiquetar: 'cliente' }],
    termina: { si: [], vence: '1d' }, horario: 'siempre',
    topes: { bolsa: 'att1-bienvenida', por_dia: 'sin_tope', total: 'sin_tope',
      repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } },
    prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  };
  const welcome = await openNative('bienvenida', WELCOME, { ok_del_cliente: { quien: 'dan', medio: 'whatsapp', fecha: '2026-10-26' } });
  const payload = structuredClone(purchaseCaptured);
  const now = await dbNow();
  const shift = now.getTime() - 60_000 - payload.creation_date;
  for (const key of ['approved_date', 'order_date']) payload.data.purchase[key] += shift;
  payload.creation_date += shift;
  const phone = payload.data.buyer.checkout_phone.replace(/\D/g, '');
  const contactId = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
    values ($1, $2, $3, 'MX') returning id`, [payload.data.buyer.name, payload.data.buyer.email, phone]), 'contacto').id;
  await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1, 'email', $2, $2, 'hotmart'), ($1, 'phone', $3, $3, 'hotmart')`, [contactId, payload.data.buyer.email, phone]);
  await cohort(welcome.flowId, contactId);
  const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), payload.data.buyer.email, phone]), 'compra');
  await sql('select * from public.apply_hotmart_purchase_approved($1,$2,$3,$4,$5,$6,$7::timestamptz)',
    [admitted.webhook_event_id, payload.data.buyer.email, phone, String(payload.data.product.id),
      payload.data.purchase.offer.code, payload.data.purchase.transaction,
      new Date(payload.data.purchase.approved_date).toISOString()]);
  const rows = await asService(async () => (await db.query('select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
    [JSON.stringify(INSTANCE), JSON.stringify({
      fuente: 'hotmart', tipo: 'compra', evento_id: admitted.webhook_event_id, producto_ref: A.funnel,
      oferta: payload.data.purchase.offer.code, contacto_id: contactId, momento: new Date(payload.creation_date).toISOString(),
      chatwoot: { account_id: A.accountId, inbox_id: A.inboxId }, destino: phone,
    })])).rows);
  const welcomeRow = one(rows.filter((row) => row.flow_id === welcome.flowId), 'bienvenida');
  if (welcomeRow.outcome !== 'motor_arranco') fail('la bienvenida arranca', welcomeRow);
  // Despues, un carrito de esa persona del mismo producto, por el legado.
  const dora = { label: 'bienvenida-y-carrito', product: A, name: payload.data.buyer.name, email: payload.data.buyer.email,
    phone, country: '1', iso: 'US', offer: A.offers.at(-1), chatwoot: '950001', contact: contactId };
  await cartFor(dora);
  const today = await withPrevious([ADMIT_NATIVE, PLAN_BASE], () => planOutcome(dora));
  const nowPlan = await planOutcome(dora);
  const doraCases = await sql(`select id = $2 as welcome, status from public.recovery_cases where contact_id = $1
    order by created_at`, [contactId, welcomeRow.recovery_case_id]);
  if (doraCases.length !== 2 || !doraCases[0].welcome || doraCases[0].status !== 'grace_period') {
    fail('la bienvenida sigue abierta y el carrito tiene su caso', doraCases);
  }
  if (JSON.stringify(nowPlan) !== JSON.stringify(today) || nowPlan.created !== true) fail('el carrito del legado con la bienvenida abierta, como hoy', { nowPlan, today });

  // Tres recorridos del legado, con las dos definiciones: un carrito, el segundo de la misma oferta, la repeticion.
  const legacyRun = async (label) => {
    const lead = await cartFor(label);
    const first = one((await db.query(PLAN_CART, lead.cartArgs)).rows, `${label} primero`);
    const replay = one((await db.query(PLAN_CART, lead.cartArgs)).rows, `${label} repeticion`);
    await cartFor(lead);
    const second = await planError(lead);
    return { first: first.created, replay: replay.created, same_case: replay.recovery_case_id === first.recovery_case_id,
      second };
  };
  const before = await withPrevious([ADMIT_NATIVE, PLAN_BASE], () => legacyRun('att1-hoy-antes'));
  const after = await legacyRun('att1-hoy-despues');
  if (JSON.stringify(before) !== JSON.stringify(after) || before.first !== true || before.replay !== false
      || before.second?.message !== 'pilot_case_binding_conflict') {
    fail('el legado de ATT1, igual con las dos definiciones', { before, after });
  }
  results.att1 = { bienvenida_y_carrito: nowPlan, legado: after };
}

// ---------------------------------------------------------------------------
// 4. El primer contacto del legado lo corta el caso nativo (C5).
// ---------------------------------------------------------------------------
{
  const intentId = one(await sql(`select purchase_intent_id from public.hotmart_purchase_intent_correlations
    where webhook_event_id = $1 and outcome = 'resolved'`, [carla.cartArgs[0]]), 'correlacion').purchase_intent_id;
  const stop = async () => one(await sql('select public._portable_precheckout_stop_reason($1, $2) as r',
    [intentId, carla.contact]), 'freno').r;
  const live = await stop();
  if (live !== 'superseded_by_provider_event') fail('el caso nativo le gana al primer contacto', live);
  const isolated = await rolledBack(async () => {
    // Sin la clasificacion que deja la correlacion del carrito: queda solo el caso nativo.
    await sql('update public.purchase_intents set current_classification = null where id = $1', [intentId]);
    const onlyNative = await stop();
    // Los casos del legado de carla los cierra para que el que quede abierto sea el nativo.
    await sql(`update public.recovery_cases set status = 'cancelled', closed_at = now()
      where contact_id = $1 and id <> $2`, [carla.contact, nativeCaseOfCarla]);
    const stillNative = await stop();
    await sql(`update public.recovery_cases set status = 'cancelled', closed_at = now() where id = $1`, [nativeCaseOfCarla]);
    const withoutNative = await stop();
    return { onlyNative, stillNative, withoutNative };
  });
  if (isolated.onlyNative !== 'superseded_by_provider_event' || isolated.stillNative !== 'superseded_by_provider_event'
      || isolated.withoutNative === 'superseded_by_provider_event') {
    fail('el freno sale del caso nativo', isolated);
  }
  results.c5 = { freno: live, sin_caso_nativo: isolated.withoutNative };
}

// ---------------------------------------------------------------------------
// 5. La baja corta un recorrido nativo con la accion pendiente.
// ---------------------------------------------------------------------------
{
  // El contacto dado de baja: la reevaluacion cancela.
  const eva = await cartFor('doble-baja-contacto');
  await cohort(cart.flowId, eva.contact);
  const evaRow = cartRow(await admitNative(eva), 'carrito nativo de eva');
  if (evaRow.outcome !== 'motor_arranco') fail('el carrito nativo de eva', evaRow);
  await sql(`update public.contacts set contact_permission = 'opted_out' where id = $1`, [eva.contact]);
  const cut = await reserve({ label: 'doble-baja-contacto', chatwoot: '950010' }, evaRow.scheduled_action_id, { expect: 'cancel' });
  if (cut.decision.reason_code !== 'contact_blocked') fail('la baja del contacto', cut.decision);

  // La baja de Chatwoot despues de reservar: el arranque nativo rechaza.
  const fran = await cartFor('doble-baja-chatwoot');
  await cohort(cart.flowId, fran.contact);
  const franRow = cartRow(await admitNative(fran), 'carrito nativo de fran');
  if (franRow.outcome !== 'motor_arranco') fail('el carrito nativo de fran', franRow);
  const reserved = await reserve({ label: 'doble-baja-chatwoot', chatwoot: '950011' }, franRow.scheduled_action_id);
  await sql(`insert into public.contact_opt_out_events (channel, purpose, source, canonical_account_id, canonical_inbox_id,
      canonical_conversation_id, canonical_message_id, external_user_id, occurred_at, normalized_rule_key, correlation_status)
    values ('whatsapp', 'cart_recovery', 'chatwoot', $1, $2, 884001, 9884001, $3, clock_timestamp(), 'unsubscribe', 'unmatched')`,
  [Number(A.accountId), Number(A.inboxId), fran.phone]);
  const rejected = await startNew(reserved);
  if (rejected.outcome !== 'rechazado' || rejected.reason !== 'baja') fail('la baja de Chatwoot en el arranque', rejected);
  results.baja = { contacto: cut.decision.reason_code, chatwoot: rejected.reason };
}

// ---------------------------------------------------------------------------
// 3b. ATT1 como corre hoy: el carrito por la ruta motor (E-206). Sus casos son
//     policy_key flujo:<id> sin flow_engine_trigger en el context: (b) no los
//     toma por un carrito nativo. Un segundo carrito de la misma persona, de la
//     misma oferta y de otra, da lo mismo con las dos definiciones.
// ---------------------------------------------------------------------------
{
  const { FLOWS, SCOPE, createRoute, route, admit } = world;
  const created = await createRoute(A.funnel, 'carrito', 'carrito', SCOPE);
  if (!created.ok) fail('E-206: crear la ruta del carrito', created);
  const toMotor = await route(A.funnel, 'carrito', FLOWS.carrito, 'motor');
  if (!toMotor.ok || toMotor.route.executor !== 'motor') fail('E-206: el carrito a motor', toMotor);
  const lead = await cartFor('att1-ruta-motor');
  const first = await admit(lead);
  if (first.outcome !== 'motor_arranco' || first.created !== true) fail('E-206: A1 por el motor', first);
  const kase = one(await sql(`select policy_key, context ? 'flow_engine_trigger' as has_trigger, offer_code
    from public.recovery_cases where id = $1`, [first.recovery_case_id]), 'E-206 caso');
  if (!kase.policy_key.startsWith('flujo:') || kase.has_trigger) fail('E-206: el caso del motor', kase);
  const shape = (row) => ({ outcome: row.outcome, reason: row.reason, created: row.created,
    same_case: row.recovery_case_id === first.recovery_case_id });
  const second = {};
  for (const [label, offer] of [['misma', lead.offer], ['otra', otherOffer]]) {
    lead.offer = offer;
    await cartFor(lead);
    const before = await withPrevious([ADMIT_NATIVE, PLAN_BASE], async () => shape(await admit(lead)));
    const after = shape(await admit(lead));
    if (JSON.stringify(before) !== JSON.stringify(after)) fail(`E-206: segundo carrito (${label} oferta) por el motor`, { before, after });
    second[label] = after;
  }
  results.att1_ruta_motor = { primero: first.outcome, segundo: second };
}

// ---------------------------------------------------------------------------
// El fixture para el bridge.
// ---------------------------------------------------------------------------
const stored = {
  _capture: {
    origen: 'validate_flow_engine_native_legacy_double_case.mjs: la cadena entera de migraciones (PGlite), el mundo '
      + 'de ATT1 con el carrito nativo de un toque y el planificador del piloto del legado',
    encargo: 'E-204',
    regenerar: 'cd tests/sql/followup_engine && node validate_flow_engine_native_legacy_double_case.mjs --emit',
  },
  ...emitted,
};
if (EMIT) {
  writeFileSync(FIXTURE, `${JSON.stringify(stored, null, 2)}\n`);
} else if (CHECK) {
  const onDisk = JSON.parse(readFileSync(FIXTURE, 'utf8'));
  if (JSON.stringify(onDisk) !== JSON.stringify(stored)) {
    fail('native_legacy_double_case_rows.json no es lo que da la cadena: correr con --emit', stored);
  }
}
await db.close();
console.log(`validate_flow_engine_native_legacy_double_case: ok ${JSON.stringify(results)}`);
