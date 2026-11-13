// La ficha de OXXO nativa (migracion 20261113200000, E-528, C4 del mapa «el AE
// maneja todos los flujos», 2026-11-13; P25 de M16).
//
// Por que: la ficha solo la ejecutaba el legado (att1-ficha), con su scope, su
// politica y la plantilla del manifiesto; el AE no podia crearla ni cambiarla.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera): el formulario con consentimiento y la ficha pasan por las admisiones
// portables de hoy (admit_portable_hotmart_payment_slip_v2 correlaciona la ficha
// con la intencion, como en produccion). Las fichas son las capturadas de ATT1
// (tests/fixtures/hotmart_payment_slip_printed_att1_20261018.json, 2026-10-18,
// anonimizadas adentro de la base): una de Mexico, CASHPAYMENT, con el link de
// OXXO y la referencia de 34 digitos, con la persona sembrada.
//
// Prueba:
//   0. la forma: cambian exactamente las funciones del encargo (ninguna de las
//      cuatro de la ficha del legado: get_portable_payment_slip_send_context,
//      mark_portable_payment_slip_request_started, reevaluate_followup_action y
//      admit_portable_inbound_commercial_case_v1) y nacen las cuatro nuevas: la
//      lectura solo de service_role, las otras de nadie de la API; correrla dos
//      veces aborta;
//   1. la definicion objetivo compila: gracia 60 min, dos envios, C3 a un minuto
//      de C1 (native_touch_2, el momento real lo pone el bridge), vence 4 d; lo
//      que no abre: contesta, una duracion entre C1 y C3, otra ancla, el ancla
//      antes de C1, tres envios, el boton del checkout; rojo: con las
//      definiciones de antes, la ficha es motor_nativo:disparador;
//   2. la admision: en cohorte arranca (la secuencia de una ficha, la ficha como
//      evento del caso, el recorrido ficha_impresa, la autorizacion con la
//      evidencia del formulario); la lectura del contexto da la ficha; sin
//      formulario no_entra:permiso; rojo: con el evento de antes,
//      invalid_flow_engine_event;
//   3. una segunda ficha de la misma persona no abre otro caso (ya_en_curso), se
//      suma al caso y la lectura da la mas reciente, sin mover el envio;
//   4. el doble caso: con una ficha del legado abierta, el nativo no arranca
//      (caso_del_legado_en_curso);
//   5. la puerta del envio vuelve a mirar la intencion de la ficha;
//   6. C1 sale y, al aceptarse, queda C3 (native_touch_2) en la misma secuencia de
//      la ficha: con una respuesta de la persona despues de C1, la reevaluacion lo
//      ejecuta igual (contestar no corta C3, 20261019000800); rojo: con la
//      secuencia de un nativo cualquiera (reason recovery), lo cancela con
//      prospect_replied.
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { buildWorld, errorOf, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261113200000_e528_native_payment_slip_v1.sql';
const TZ = 'timestamp with time zone';
const ADMIT_EVENT = 'admit_flow_engine_event_v1(jsonb,jsonb)';
const ADMIT_NATIVE = `_flow_engine_admit_native_flow(flow_controls,webhook_events,uuid,text,${TZ},bigint,bigint,text)`;
const TAIL = `_flow_engine_authorize_native_tail(uuid,uuid,flow_engine_runs,recovery_cases,flow_controls,${TZ})`;
const CONTEXT = `get_flow_engine_native_payment_slip_context_v1(uuid,text,bigint,${TZ})`;
const CHANGED = [
  ADMIT_EVENT, ADMIT_NATIVE, TAIL,
  '_flow_engine_native_trigger(jsonb)',
  '_flow_engine_native_steps(text)',
  '_flow_engine_native_unsupported(jsonb)',
  '_flow_engine_compile_policy_core(jsonb,text,integer,text,text)',
];
const ADDED = [
  '_flow_engine_native_payment_slip_due_day(jsonb)',
  '_flow_engine_native_payment_slip_shape(jsonb)',
  '_flow_engine_native_payment_slip_audience(uuid,uuid,text,text,text)',
  CONTEXT,
];
const LEGACY_SLIP = [
  `get_portable_payment_slip_send_context(uuid,text,bigint,${TZ})`,
  `mark_portable_payment_slip_request_started(uuid,uuid,text,bigint,${TZ})`,
  'admit_portable_inbound_commercial_case_v1(text,integer,bigint,text)',
];
const SLIPS = JSON.parse(readFileSync(join(root,
  'tests/fixtures/hotmart_payment_slip_printed_att1_20261018.json'), 'utf8')).eventos;
const OXXO = SLIPS.find((event) => event.payload.data.purchase.payment.type === 'CASHPAYMENT'
  && event.payload.data.purchase.checkout_country?.iso === 'MX'
  && String(event.payload.data.purchase.payment.billet_url).includes('/print/oxxo'));
if (!OXXO || SLIPS.length !== 13) fail('el fixture no son las 13 fichas capturadas de ATT1');
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify([])) {
  fail(`despues de ${MIGRATION} no va ninguna`, names.slice(position + 1));
}

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile, p.proowner,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
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
  if (!sameSet(changed, CHANGED) || !sameSet(added, ADDED) || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  for (const signature of LEGACY_SLIP) {
    if (!before.has(signature) || before.get(signature).definition !== after.get(signature).definition) {
      fail(`${signature}: la ficha del legado no cambia`);
    }
  }
  for (const signature of ADDED) {
    const row = after.get(signature);
    if (row.api_x || (signature === CONTEXT ? !row.service_x || !row.prosecdef : row.service_x || row.prosecdef)) {
      fail(`${signature}: los permisos`, row);
    }
  }
  for (const signature of CHANGED) {
    if (!after.get(signature).definition.includes('e528_native_payment_slip')) fail(`${signature}: sin la marca`);
    previous[signature] = before.get(signature).definition;
  }
  const trigger = one((await db.query(`select pg_get_constraintdef(oid) as d from pg_constraint
    where conname = 'flow_engine_native_runs_trigger_event_check'`)).rows, 'restriccion').d;
  if (!trigger.includes('ficha_impresa') || !trigger.includes('audiencia')) fail('la restriccion del recorrido', trigger);
  let error = null;
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  } catch (caught) {
    error = caught;
  }
  if (error?.message !== 'e528_native_payment_slip_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  await db.exec('rollback').catch(() => {});
  if ((await snapshot(db)).get(ADMIT_NATIVE).definition !== after.get(ADMIT_NATIVE).definition) fail('la segunda corrida cambio algo');
  results.forma = { cambiadas: changed.length, nuevas: added.length, legado_igual: LEGACY_SLIP.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 (buildWorld, la cadena entera) con la ficha nativa.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, dbNow, reserve, startNew, accept, person, admitForm, THREE_TOUCHES,
  claim, reevaluate, evidence } = world;
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
const withPrevious = async (signatures, run) => rolledBack(async () => {
  for (const signature of signatures) await db.exec(previous[signature]);
  return run();
});

const ANCHOR = { vencimiento: { dias_antes: 0, a_las: '09:00' } };
const C1 = { plantilla: 'att1_ficha_efectivo_01', idioma: 'es_MX',
  variables: ['nombre', 'producto', 'medio_de_pago', 'vencimiento'], boton: { link: 'ficha' } };
const C3 = { plantilla: 'att1_ficha_vence_hoy_01', idioma: 'es_MX',
  variables: ['nombre', 'medio_de_pago', 'producto'], boton: { link: 'ficha' } };
// La definicion objetivo del diseno (§2.2), con los nombres de las plantillas capturadas de ATT1.
const TARGET = {
  esquema: 'setter-flujo/v1', flujo: 'ficha_oxxo', producto: A.funnel, estado: 'borrador', nombre: 'Ficha de pago sin pagar',
  dispara: { evento: 'ficha_impresa' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ esperar: '60m' }, C1, { esperar: ANCHOR }, C3],
  termina: { si: ['compra', 'ficha_vencida'], vence: '4d' },
  horario: { zona: 'America/Mexico_City', desde: '09:00', hasta: '21:00' },
  topes: { bolsa: 'att1-ficha-nativa', por_dia: 20, total: 1000,
    repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
// En el mundo de prueba, con horario siempre: la admision no depende de la hora en que corre.
const SLIP_FLOW = { ...TARGET, horario: 'siempre' };
const unsupported = async (definition) => one(await sql('select public._flow_engine_native_unsupported($1::jsonb) as r',
  [JSON.stringify(definition)]), 'lo que no ejecuta').r;

// ---------------------------------------------------------------------------
// 1. La forma: la definicion objetivo compila y lo que no abre.
// ---------------------------------------------------------------------------
{
  if (await unsupported(TARGET) !== null) fail('la ficha objetivo se ejecuta', await unsupported(TARGET));
  const compiled = one(await sql(`select grace_period = interval '60 minutes' as grace, expires_after = interval '4 days' as vence,
      max_automatic_messages, steps
    from public._flow_engine_compile_policy_core($1::jsonb, '00000000-0000-4000-8000-0000000e5280', 1,
                                                 'nativo:ficha_impresa', 'America/Mexico_City')`,
  [JSON.stringify(TARGET)]), 'politica');
  const steps = compiled.steps;
  if (!compiled.grace || !compiled.vence || compiled.max_automatic_messages !== 2 || steps.length !== 2
      || steps[0].step_key !== 'first_contact' || steps[1].step_key !== 'native_touch_2'
      || steps[1].delay !== '1 minute' || steps[1].approved_template !== true) {
    fail('la politica de la ficha', compiled);
  }
  const wrong = {
    contesta: { termina: { si: ['compra', 'ficha_vencida', 'contesta'], vence: '4d' } },
    duracion: { pasos: [{ esperar: '60m' }, C1, { esperar: '3d' }, C3] },
    otra_hora: { pasos: [{ esperar: '60m' }, C1, { esperar: { vencimiento: { dias_antes: 0, a_las: '10:00' } } }, C3] },
    c2: { pasos: [{ esperar: '60m' }, C1, { esperar: { vencimiento: { dias_antes: 1, a_las: '10:00' } } }, C3] },
    ancla_antes: { pasos: [{ esperar: ANCHOR }, C1] },
    tres: { pasos: [{ esperar: '60m' }, C1, { esperar: ANCHOR }, C3, { esperar: ANCHOR }, C3] },
    checkout: { pasos: [{ esperar: '60m' }, { ...C1, boton: { link: 'checkout' } }] },
    sin_c3: { pasos: [{ esperar: '60m' }, C1, { esperar: ANCHOR }] },
    // La regla de 20261112210000 (al menos una espera entre dos envios y ninguna al final) sigue
    // valiendo para la ficha: su forma propia la cubre.
    sin_espera: { pasos: [{ esperar: '60m' }, C1, C3] },
    espera_al_final: { pasos: [{ esperar: '60m' }, C1, { esperar: '60m' }] },
    ancla_al_final: { pasos: [{ esperar: '60m' }, C1, { esperar: ANCHOR }, C3, { esperar: '60m' }] },
  };
  for (const [label, change] of Object.entries(wrong)) {
    const got = await unsupported({ ...TARGET, ...change });
    if (got !== 'motor_nativo:forma') fail(`la ficha ${label} no se abre`, got);
  }
  // Un solo toque (C1) si.
  if (await unsupported({ ...TARGET, pasos: [{ esperar: '60m' }, C1] }) !== null) fail('la ficha de un toque se abre');
  // Rojo: con las definiciones de antes, la ficha no es un disparador nativo y la espera no compila.
  const red = await withPrevious(['_flow_engine_native_trigger(jsonb)', '_flow_engine_native_unsupported(jsonb)'],
    () => unsupported(TARGET));
  if (red !== 'motor_nativo:disparador') fail('sin la migracion, la ficha no es nativa', red);
  const redCompile = await withPrevious(['_flow_engine_compile_policy_core(jsonb,text,integer,text,text)'],
    () => errorOf(db, () => db.query(`select * from public._flow_engine_compile_policy_core($1::jsonb,
      '00000000-0000-4000-8000-0000000e5280', 1, 'nativo:ficha_impresa', 'America/Mexico_City')`, [JSON.stringify(TARGET)])));
  if (redCompile?.message !== 'compile_policy_wait_invalid') fail('sin la migracion, la espera anclada no compila', redCompile?.message);
  results.politica = { pasos: steps.map((step) => step.step_key), c3_delay: steps[1].delay, rojos: [red, redCompile.message] };
}

const openNative = async (flowKey, definition, product = A.funnel) => {
  const flowId = await register(product, flowKey);
  await cycle([versionItem(flowId, product, flowKey, { ...definition, producto: product })], `v1 de ${flowKey}`);
  const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'propuesta').proposal_id;
  const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [flowId, proposalId, A.timezone]), 'abrir').r);
  if (!opened.ok || opened.state !== 'cohorte') fail(`abrir ${flowKey}`, opened);
  return { flowId, opened };
};
const slipFlow = await openNative('ficha_oxxo', SLIP_FLOW);
const cohort = async (flowId, contactId) => sql(
  `insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`, [flowId, contactId]);
const leadFor = async (label, { form = true } = {}) => {
  const lead = person(label);
  if (form) await admitForm(lead, new Date((await dbNow()).getTime() - 50 * 60_000));
  return lead;
};
let slipSerial = 0;
// Una ficha capturada de ATT1 con la persona sembrada, por la admision v2 de hoy.
const slipOf = async (lead, { at = null } = {}) => {
  slipSerial += 1;
  const payload = structuredClone(OXXO.payload);
  const moment = at ?? new Date((await dbNow()).getTime() - 10 * 60_000);
  payload.id = `nativo-ficha-${lead.email}-${slipSerial}`;
  payload.creation_date = moment.getTime();
  payload.data.purchase.order_date = moment.getTime();
  payload.data.buyer = { ...payload.data.buyer, name: lead.name, email: lead.email, checkout_phone: lead.phone };
  payload.data.product = { ...payload.data.product, id: A.productId, name: A.productName };
  payload.data.purchase.offer.code = lead.offer.offer_code;
  payload.data.purchase.transaction = `HPNATIVOFICHA${String(slipSerial).padStart(4, '0')}`;
  payload.data.purchase.payment.billet_url = `${payload.data.purchase.payment.billet_url.split('hash=')[0]}hash=${'a'.repeat(31)}${slipSerial}`;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_payment_slip_v2($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone]), `${lead.label} ficha`);
  if (admitted.outcome !== 'inserted') fail(`${lead.label} ficha`, admitted);
  if (!lead.contact) {
    lead.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
      values ($1,$2,$3,'MX') returning id`, [lead.name, lead.email, lead.phone]), 'contacto').id;
    await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id)
      values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)`,
    [lead.contact, lead.email, lead.phone, admitted.webhook_event_id]);
  }
  lead.event = admitted.webhook_event_id;
  lead.moment = moment.toISOString();
  lead.billet = payload.data.purchase.payment.billet_url;
  return lead;
};
const flowEventOf = (lead) => ({
  fuente: 'hotmart', tipo: 'ficha_impresa', evento_id: lead.event, producto_ref: A.funnel, oferta: lead.offer.offer_code,
  contacto_id: lead.contact, momento: lead.moment, chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
  destino: lead.phone,
});
const admitNative = async (lead) => asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)', [JSON.stringify(INSTANCE), JSON.stringify(flowEventOf(lead))])).rows);
let anaReserved = null;
const rowOf = (rows, flowId, label) => one(rows.filter((row) => row.flow_id === flowId), label);
const contextOf = async (reserved) => asService(async () => (await db.query(
  `select * from public.${CONTEXT.split('(')[0]}($1, $2, $3, $4)`,
  [reserved.actionId, reserved.worker, reserved.lease, reserved.now])).rows);

// ---------------------------------------------------------------------------
// 2. La admision.
// ---------------------------------------------------------------------------
const ana = await slipOf(await leadFor('ficha-ana'));
await cohort(slipFlow.flowId, ana.contact);
const started = rowOf(await admitNative(ana), slipFlow.flowId, 'ficha nativa');
if (started.outcome !== 'motor_arranco' || started.created !== true) fail('la ficha abre el recorrido nativo', started);
{
  const sequence = one(await sql('select reason from public.followup_sequences where id = $1',
    [started.followup_sequence_id]), 'secuencia');
  const events = await sql(`select webhook_event_id, event_role from public.recovery_case_events where recovery_case_id = $1`,
    [started.recovery_case_id]);
  const native = one(await sql('select * from public.flow_engine_native_runs where recovery_case_id = $1',
    [started.recovery_case_id]), 'recorrido nativo');
  const details = one(await sql(`select purchase_intent_id from public.commercial_ally_payment_slip_details
    where webhook_event_id = $1 and correlation_outcome = 'resolved'`, [ana.event]), 'correlacion');
  const action = one(await sql('select anchor_type, step_key, due_at from public.scheduled_actions where id = $1',
    [started.scheduled_action_id]), 'accion');
  const grant = one(await sql(`select evidence from public.contact_authorizations where contact_id = $1 and purpose = 'cart_recovery'`,
    [ana.contact]), 'autorizacion');
  const wait = (new Date(action.due_at) - new Date(ana.moment)) / 60_000;
  if (sequence.reason !== 'payment_slip' || native.trigger_event !== 'ficha_impresa'
      || native.audience_purchase_intent_id !== details.purchase_intent_id
      || !events.some((row) => row.webhook_event_id === ana.event && row.event_role === 'payment_slip')
      || action.anchor_type !== 'flow_engine_event' || action.step_key !== 'first_contact' || Math.abs(wait - 60) > 0.01
      || grant.evidence.reason !== 'precheckout_whatsapp_consent') {
    fail('el caso nativo de la ficha', { sequence, events, native, action, wait, grant });
  }
  results.admision = { secuencia: sequence.reason, recorrido: native.trigger_event, espera_min: wait };
}
{
  const noForm = await slipOf(await leadFor('ficha-sin-formulario', { form: false }));
  await cohort(slipFlow.flowId, noForm.contact);
  const refused = rowOf(await admitNative(noForm), slipFlow.flowId, 'sin formulario');
  if (refused.outcome !== 'motor_no_arranca' || refused.reason !== 'no_entra:permiso') fail('sin formulario', refused);
  const red = await withPrevious([ADMIT_EVENT], () => errorOf(db, () => db.query(
    'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)', [JSON.stringify(INSTANCE), JSON.stringify(flowEventOf(ana))])));
  if (red?.message !== 'invalid_flow_engine_event') fail('sin la migracion, la ficha no entra', red?.message);
  results.cortes = { sin_formulario: refused.reason, sin_la_migracion: red.message };
}

// ---------------------------------------------------------------------------
// 3. La segunda ficha se suma al caso; la lectura da la mas reciente.
// ---------------------------------------------------------------------------
{
  const lead = { label: 'ficha-ana', chatwoot: '640' };
  const due1 = one(await sql('select due_at from public.scheduled_actions where id = $1', [started.scheduled_action_id]), 'accion').due_at;
  const firstBillet = ana.billet;
  await slipOf(ana);
  const second = rowOf(await admitNative(ana), slipFlow.flowId, 'la segunda ficha');
  if (second.outcome !== 'motor_no_arranca' || second.reason !== 'ya_en_curso' || second.recovery_case_id !== null) {
    fail('la segunda ficha no abre otro caso', second);
  }
  const trace = await sql(`select event_role from public.recovery_case_events where recovery_case_id = $1 and webhook_event_id = $2`,
    [started.recovery_case_id, ana.event]);
  const due2 = one(await sql('select due_at from public.scheduled_actions where id = $1', [started.scheduled_action_id]), 'accion').due_at;
  if (trace.length !== 1 || trace[0].event_role !== 'payment_slip' || String(due1) !== String(due2)) {
    fail('la segunda ficha se suma al caso sin mover el envio', { trace, due1, due2 });
  }
  const reserved = await reserve(lead, started.scheduled_action_id);
  anaReserved = reserved;
  const [context] = await contextOf(reserved);
  if (context?.billet_url !== ana.billet || context.billet_url === firstBillet || context.payment_type !== 'CASHPAYMENT'
      || context.checkout_country_iso !== 'MX' || !/^[0-9]{34}$/.test(context.billet_barcode ?? '')) {
    fail('la lectura de la ficha da la mas reciente', { context, firstBillet });
  }
  // 5. La puerta del envio vuelve a mirar la intencion de la ficha.
  const intentId = one(await sql('select audience_purchase_intent_id as i from public.flow_engine_native_runs where recovery_case_id = $1',
    [started.recovery_case_id]), 'intencion').i;
  const gate = await rolledBack(async () => {
    await db.query('update public.purchase_intents set whatsapp_contact_authorized = false where id = $1', [intentId]);
    return startNew(reserved);
  });
  if (gate.outcome !== 'rechazado' || gate.reason !== 'no_entra:permiso') fail('el arranque vuelve a mirar la intencion de la ficha', gate);
  const redGate = await withPrevious([TAIL], () => rolledBack(async () => {
    await db.query('update public.purchase_intents set whatsapp_contact_authorized = false where id = $1', [intentId]);
    return startNew(reserved);
  }));
  if (redGate.outcome === 'rechazado' && redGate.reason === 'no_entra:permiso') fail('sin la migracion, la puerta no miraba la ficha', redGate);
  results.segunda_ficha = { motivo: second.reason, eventos_de_ficha: 2, lectura: 'la mas reciente', puerta: gate.reason };
}

// ---------------------------------------------------------------------------
// 4. El doble caso: una ficha del legado abierta gana.
// ---------------------------------------------------------------------------
{
  const eva = await slipOf(await leadFor('ficha-legado'));
  await cohort(slipFlow.flowId, eva.contact);
  // Un caso abierto de la ficha del legado de la persona (lo que deja plan_payment_slip_recovery), de otra ficha.
  const legacyEvent = eva.event;
  await slipOf(eva);
  await sql(`insert into public.recovery_cases (contact_id, abandonment_event_id, source, external_product_id, product_name,
      offer_code, status, lead_stage, grace_expires_at, policy_key, policy_version, context)
    values ($1, $2, 'hotmart', $3, $4, $5, 'grace_period', 'new', clock_timestamp() + interval '1 hour', $6, 1,
      '{"trigger_kind": "payment_slip"}'::jsonb)`,
  [eva.contact, legacyEvent, String(A.productId), A.productName, eva.offer.offer_code, THREE_TOUCHES.policy]);
  // Rojo: sin el bloque del doble caso, el nativo abria un segundo caso.
  const red = await rolledBack(async () => {
    const definition = one(await sql('select pg_get_functiondef($1::regprocedure) as d', [ADMIT_NATIVE]), 'definicion').d;
    const begin = definition.indexOf('    -- e528_native_payment_slip_double_case: begin');
    const end = definition.indexOf('    -- e528_native_payment_slip_double_case: end\n') + '    -- e528_native_payment_slip_double_case: end\n'.length;
    if (begin < 0 || end < begin) fail('el bloque del doble caso');
    await db.exec(definition.slice(0, begin) + definition.slice(end));
    return rowOf(await admitNative(eva), slipFlow.flowId, 'doble caso sin el bloque').outcome;
  });
  if (red !== 'motor_arranco') fail('sin el bloque, el nativo abria el segundo caso', red);
  const yielded = rowOf(await admitNative(eva), slipFlow.flowId, 'doble caso');
  if (yielded.outcome !== 'motor_no_arranca' || yielded.reason !== 'caso_del_legado_en_curso') fail('el doble caso', yielded);
  results.doble_caso = { motivo: yielded.reason, sin_el_bloque: red };
}

// ---------------------------------------------------------------------------
// 6. C1 sale, queda C3, y contestar no lo corta.
// ---------------------------------------------------------------------------
{
  const lead = { label: 'ficha-ana-c3', chatwoot: '640' };
  const reserved = anaReserved;
  const sent = await startNew(reserved);
  if (sent.outcome !== 'request_started') fail('C1 arranca contra la bolsa de la ficha', sent);
  await accept(lead, reserved);
  const c3 = one(await sql(`select * from public.scheduled_actions where recovery_case_id = $1 and step_key = 'native_touch_2'`,
      [started.recovery_case_id]), 'C3');
  if (c3.action_type !== 'no_reply_review' || c3.followup_sequence_id !== started.followup_sequence_id) fail('C3 queda agendado', c3);
  // El contexto de C3: la plantilla y las variables de la version fijada (native_touch_2), con su boton.
  const c3Step = one(await asService(async () => (await db.query('select * from public.flow_engine_execution_context_v1($1)',
    [c3.id])).rows), 'contexto de C3');
  if (c3Step.template_name !== C3.plantilla || c3Step.step_key !== 'native_touch_2'
      || JSON.stringify(c3Step.template_variables) !== JSON.stringify(C3.variables)
      || JSON.stringify(c3Step.template_button) !== JSON.stringify({ link: 'ficha' })) {
    fail('el contexto de C3', c3Step);
  }
  const replied = { ...evidence(lead, await dbNow()), p_chatwoot_inbound_after_anchor: true };
  const decide = async () => rolledBack(async () => {
    const fence = await claim('ficha-c3', c3.id);
    return reevaluate(c3.id, { ...fence }, { ...replied, p_chatwoot_checkpoint_at: fence.now.toISOString() });
  });
  const survives = await decide();
  if (survives.decision !== 'execute') fail('contestar no corta C3 de la ficha', survives);
  const red = await rolledBack(async () => {
    await db.query("update public.followup_sequences set reason = 'recovery' where id = $1", [started.followup_sequence_id]);
    const fence = await claim('ficha-c3-rojo', c3.id);
    return reevaluate(c3.id, fence, { ...replied, p_chatwoot_checkpoint_at: fence.now.toISOString() });
  });
  if (red.decision !== 'cancel' || red.reason_code !== 'prospect_replied') fail('con otra secuencia, la respuesta lo cortaba', red);
  results.c3 = { plantilla: c3Step.template_name, con_respuesta: survives.decision, rojo: red.reason_code };
}

console.log(JSON.stringify(results, null, 2));
