// El pago rechazado de una oferta de campana abre el recorrido nativo sin
// formulario, con el telefono del evento de Hotmart (migracion 20261112210400,
// E-483, pieza PF, 2026-11-12).
//
// Por que: en ATT1 la oferta Black wmkq4ba9 tiene una campana del AE con
// codigo_oferta. Sus rechazos de quien no lleno el formulario quedaban en
// payment_failure_correlation_unresolved (el legado) o no_entra:permiso (el
// nativo): ni caso ni mensaje. La etiqueta de la campana (§4b de
// campaign-entry-v1) la pone el bridge en el before_send de un recorrido que ya
// existe; con esto el recorrido existe.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera). Los rechazos son los capturados de ATT1
// (tests/fixtures/hotmart_purchase_canceled_att1_20261019.json: uno generico
// «Transaction refused» y un pendiente de MERCADO_PAGO cancelado sin motivo),
// con la oferta cambiada a la de la campana (2uafw5bg en el mundo de prueba,
// declarada en la instancia), pasados por la admision portable de hoy, que los
// deja unmatched sin formulario. El flujo es el pago rechazado completo de C3
// (B1 por motivo, 24 h, B2 o Z).
//
// Prueba:
//   0. la forma: cambian exactamente tres funciones con su marca, nace una
//      privada, la restriccion del recorrido nombra audience_hotmart_phone;
//      correrla dos veces aborta; nada despues;
//   1. sin la lista (el bridge de hoy, el flag apagado): sin formulario,
//      no_entra:permiso; con la lista y la oferta de campana: arranca, el
//      recorrido sin intencion y con audience_hotmart_phone, la autorizacion
//      hotmart_payment_failure_phone, la identidad de WhatsApp creada con el
//      movil del evento, y el origen que lee §4b dice pago_fallido y la oferta;
//      la variable de la transaccion queda vacia al terminar; rojo: con la
//      admision de antes, no_entra:permiso;
//   2. el despacho: B1 de su rama (banco) arranca y se acepta, el segundo toque
//      queda agendado; rojo: con la puerta de antes, el arranque rechaza;
//   3. lo que no entra: una oferta sin campana (no_entra:permiso), un evento
//      sin checkout_phone con el destino que guardo otro momento
//      (no_entra:permiso), un pendiente cancelado (no_es_un_rechazo), el
//      contacto dado de baja (baja), y la baja entre la reevaluacion y el
//      arranque (la puerta: baja, sin autorizacion); el telefono del evento
//      distinto del destino, una correlacion ambigua y el binding de otra oferta
//      (no_entra:permiso);
//   4. con formulario, el permiso formulario de siempre (con intencion, sin la
//      marca), con o sin la lista.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, errorOf, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261112210400_e483_payment_failure_by_phone_v1.sql';
const TZ = 'timestamp with time zone';
const ADMIT_EVENT = 'admit_flow_engine_event_v1(jsonb,jsonb)';
const ADMIT_NATIVE = `_flow_engine_admit_native_flow(flow_controls,webhook_events,uuid,text,${TZ},bigint,bigint,text)`;
const TAIL = `_flow_engine_authorize_native_tail(uuid,uuid,flow_engine_runs,recovery_cases,flow_controls,${TZ})`;
const CHANGED = [ADMIT_EVENT, ADMIT_NATIVE, TAIL];
const ADDED = ['_flow_engine_native_payment_failure_phone_audience(uuid,uuid,text,text,text)'];
const MARKER = 'e483_payment_failure_by_phone';
const CAPTURE = JSON.parse(readFileSync(join(root, 'tests/fixtures/hotmart_purchase_canceled_att1_20261019.json'), 'utf8'));
const REFUSED = CAPTURE.eventos[0].payload;   // CREDIT_CARD, «Transaction refused»: rama banco.
const PENDING = CAPTURE.eventos[5].payload;   // MERCADO_PAGO cancelado sin motivo: no es un rechazo.
if (REFUSED.event !== 'PURCHASE_CANCELED' || REFUSED.data.purchase.payment.refusal_reason !== 'Transaction refused'
    || !REFUSED.data.buyer.checkout_phone || PENDING.data.purchase.payment.type !== 'MERCADO_PAGO'
    || PENDING.data.purchase.payment.refusal_reason) {
  fail('el fixture no son los rechazos de ATT1 que esta prueba espera');
}
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues va el cupon propio en el nativo (20261112210500, E-483, C2): no toca la admision ni el permiso.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261112210500_e483_native_personal_coupon_v1.sql',
  // El evento de un disparo por audiencia en el nativo (20261112210600, E-483, para E-484).
  '20261112210600_e483_native_audience_source_v1.sql', '20261112220000_flow_audience_triggers_v1.sql', '20261112230000_e483_native_person_cap_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de C2`, names.slice(position + 1));
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
    && ['acl', 'prosecdef', 'provolatile'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, CHANGED) || !sameSet(added, ADDED) || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  for (const signature of CHANGED) {
    if (!after.get(signature).definition.includes(MARKER)) fail(`${signature}: sin la marca`);
    previous[signature] = before.get(signature).definition;
  }
  for (const signature of ADDED) {
    const row = after.get(signature);
    if (row.service_x || row.api_x || row.prosecdef) fail(`${signature}: no es de nadie de la API`, row);
  }
  const check = one((await db.query(`select pg_get_constraintdef(oid) as d from pg_constraint
    where conrelid = 'public.flow_engine_native_runs'::regclass and conname = 'flow_engine_native_runs_audience_by_trigger'`)).rows,
  'restriccion').d;
  if (!check.includes('audience_hotmart_phone')) fail('la restriccion del recorrido', check);
  const error = await errorOf(db, () => applyFile(db, join(root, 'supabase/migrations', MIGRATION)));
  if (error?.message !== 'e483_payment_failure_by_phone_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  await db.exec('rollback').catch(() => {});
  if ((await snapshot(db)).get(ADMIT_NATIVE).definition !== after.get(ADMIT_NATIVE).definition) fail('la segunda corrida cambio algo');
  results.forma = { cambiadas: changed.length, nuevas: added.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 (buildWorld, la cadena entera) con el pago rechazado completo.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, dbNow, reserve, startNew, accept, person, admitForm } = world;
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

// La oferta de la campana y otra sin campana, las dos declaradas en la instancia.
const CAMPAIGN = '2uafw5bg';
const OTHER = 'bmaztyhg';
if (!A.offers.some((o) => o.offer_code === CAMPAIGN) || !A.offers.some((o) => o.offer_code === OTHER)) {
  fail('las ofertas del mundo de ATT1', A.offers.map((o) => o.offer_code));
}
const WITH_LIST = { ...INSTANCE, native_payment_failure_by_phone_offers: [CAMPAIGN] };

// El pago rechazado completo de C3: B1 por motivo (cupon fijo en dos ramas), 24 h, B2 o Z.
const B2 = 'att1_pago_rechazado_descuento_01';
const Z = 'att1_ultima_renovacion_01';
const COUPON = { link: 'checkout', cupon: 'PROTOCOLO10' };
const b1 = (branch, coupon) => ({ plantilla: `maribel_pago_rechazado_${branch}_01`, idioma: 'es_MX',
  variables: ['nombre', 'producto'], boton: coupon ? { ...COUPON } : { link: 'checkout' } });
const follow = (template) => ({ plantilla: template, idioma: 'es_MX', variables: ['nombre', 'producto'] });
const path = (branch, coupon) => [b1(branch, coupon), { esperar: '24h' }, follow(coupon ? Z : B2)];
const FLOW = {
  esquema: 'setter-flujo/v1', flujo: 'pago_rechazado_campana', producto: A.funnel, estado: 'borrador',
  nombre: 'Pago rechazado de la campana', dispara: { evento: 'pago_fallido' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ esperar: '15m' }, {
    condicion: { dato: 'descuento_previo', es: ['si'] }, entonces: path('cupon', true),
    si_no: [{ condicion: { dato: 'rechazos', es: ['varios'] }, entonces: path('dos_veces', false),
      si_no: [{ condicion: { dato: 'tipo_de_rechazo', es: ['sin_saldo'] }, entonces: path('saldo', true),
        si_no: path('banco', false) }] }] }],
  termina: { si: ['compra', 'contesta'], vence: '3d' }, horario: 'siempre',
  topes: { bolsa: 'att1-pago-campana', por_dia: 20, total: 1000, repeticion: { una_por: 'persona', otra_despues_de: '72h' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
const B1_BANCO = 'maribel_pago_rechazado_banco_01';
const flowId = await register(A.funnel, FLOW.flujo);
await cycle([versionItem(flowId, A.funnel, FLOW.flujo, FLOW)], 'v1 del pago rechazado de la campana');
{
  const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'propuesta').proposal_id;
  const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [flowId, proposalId, A.timezone]), 'abrir').r);
  if (!opened.ok || opened.state !== 'cohorte') fail('abrir el pago rechazado de la campana', opened);
}

let serial = 0;
// Un rechazo capturado de ATT1 con la persona sembrada y la oferta cambiada, por la admision portable
// de hoy. Sin formulario, como en produccion: el contacto lo crea el bridge desde Hotmart.
const refusalOf = async (lead, { offer = CAMPAIGN, payload: source = REFUSED, phone = true } = {}) => {
  serial += 1;
  const payload = structuredClone(source);
  const moment = new Date((await dbNow()).getTime() - 10 * 60_000);
  payload.id = `pf-rechazo-${lead.email}-${serial}`;
  payload.creation_date = moment.getTime();
  payload.data.buyer = { ...payload.data.buyer, name: lead.name, email: lead.email, checkout_phone: lead.phone };
  if (!phone) delete payload.data.buyer.checkout_phone;
  payload.data.product = { ...payload.data.product, id: A.productId, name: A.productName };
  payload.data.purchase.offer.code = offer;
  payload.data.purchase.transaction = `HPPF${String(serial).padStart(4, '0')}`;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.email, phone ? lead.phone : null]),
  `${lead.label} rechazo`);
  if (admitted.outcome !== 'inserted') fail(`${lead.label} rechazo`, admitted);
  if (!lead.contact) {
    const known = await sql(`select contact_id from public.contact_points where type = 'email' and normalized_value = $1`,
      [lead.email]);
    if (known.length > 0) {
      lead.contact = known[0].contact_id;
    } else {
      lead.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
        values ($1,$2,$3,'MX') returning id`, [lead.name, lead.email, lead.phone]), 'contacto').id;
      await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id)
        values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)`,
      [lead.contact, lead.email, lead.phone, admitted.webhook_event_id]);
    }
    await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
      [flowId, lead.contact]);
  }
  lead.event = admitted.webhook_event_id;
  lead.moment = moment.toISOString();
  lead.offerCode = offer;
  return lead;
};
const correlationOf = async (lead) => one(await sql(`select correlation_outcome from public.commercial_ally_payment_failure_details
  where webhook_event_id = $1`, [lead.event]), 'correlacion').correlation_outcome;
const admitWith = async (instance, lead) => one((await asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
  [JSON.stringify(instance), JSON.stringify({
    fuente: 'hotmart', tipo: 'pago_fallido', evento_id: lead.event, producto_ref: A.funnel, oferta: lead.offerCode,
    contacto_id: lead.contact, momento: lead.moment, chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
    destino: lead.phone })])).rows)).filter((row) => row.flow_id === flowId), `${lead.label} admision`);
const contextOf = async (actionId) => (await asService(async () => (await db.query(
  'select * from public.flow_engine_execution_context_v1($1)', [actionId])).rows));
const dataOf = async (reserved) => one(await asService(async () => (await db.query(
  'select * from public.get_flow_engine_native_payment_failure_context_v1($1,$2,$3,$4)',
  [reserved.actionId, reserved.worker, reserved.lease, reserved.now])).rows), 'datos');
const recordOf = async (reserved, index) => one(await asService(async () => (await db.query(
  'select * from public.record_flow_engine_native_branch_v1($1,$2,$3,$4,$5,$6)',
  [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, index, reserved.now])).rows), 'registro');
const branchOf = (data) => (data.descuento_previo === 'si' ? 0 : data.rechazos === 'varios' ? 1
  : data.tipo_de_rechazo === 'sin_saldo' ? 2 : 3);

// ---------------------------------------------------------------------------
// 1. La admision sin formulario.
// ---------------------------------------------------------------------------
const ana = await refusalOf(person('pf-ana'));
if (await correlationOf(ana) !== 'unmatched') fail('sin formulario, la admision portable no correlaciona');
{
  const identitiesBefore = await sql(`select id from public.channel_identities where external_user_id = $1`, [ana.phone]);
  if (identitiesBefore.length !== 0) fail('la persona no tiene identidad de WhatsApp antes', identitiesBefore);
  // Sin la lista (el bridge de hoy o el flag apagado): lo de antes.
  const off = await admitWith(INSTANCE, ana);
  if (off.outcome !== 'motor_no_arranca' || off.reason !== 'no_entra:permiso') fail('sin la lista, no_entra:permiso', off);
  // Rojo: con la admision de antes, la lista no cambia nada.
  const red = await withPrevious([ADMIT_NATIVE], () => admitWith(WITH_LIST, ana));
  if (red.outcome !== 'motor_no_arranca' || red.reason !== 'no_entra:permiso') fail('sin la migracion, no_entra:permiso', red);
  // Con la lista y la oferta de la campana: arranca.
  ana.start = await admitWith(WITH_LIST, ana);
  if (ana.start.outcome !== 'motor_arranco' || ana.start.created !== true) fail('con la lista, el rechazo abre el recorrido', ana.start);
  const native = one(await sql('select * from public.flow_engine_native_runs where recovery_case_id = $1',
    [ana.start.recovery_case_id]), 'recorrido');
  if (native.trigger_event !== 'pago_fallido' || native.offer_code !== CAMPAIGN || native.audience_hotmart_phone !== true
      || native.audience_purchase_intent_id !== null || native.audience_precheckout_submission_id !== null) {
    fail('el recorrido sin intencion y con el telefono de Hotmart', native);
  }
  const grant = one(await sql(`select authorization_source, evidence from public.contact_authorizations
    where contact_id = $1 and purpose = 'cart_recovery'`, [ana.contact]), 'autorizacion');
  if (grant.authorization_source !== 'hotmart' || grant.evidence.reason !== 'hotmart_payment_failure_phone'
      || grant.evidence.webhook_event_id !== ana.event || grant.evidence.offer !== CAMPAIGN) {
    fail('la autorizacion del telefono de Hotmart', grant);
  }
  // La identidad de WhatsApp nace con el movil del evento (no estaba en Chatwoot).
  const identity = one(await sql(`select i.external_user_id, i.contact_id from public.channel_identities i
    join public.recovery_cases rc on rc.selected_channel_identity_id = i.id where rc.id = $1`, [ana.start.recovery_case_id]),
  'identidad');
  if (identity.external_user_id !== ana.phone || identity.contact_id !== ana.contact) fail('la identidad del movil del evento', identity);
  // Lo que lee el plan de etiquetas de §4b (campaign_entry_matched by=oferta).
  const origin = one(await asService(async () => (await db.query(
    'select public.flow_engine_native_campaign_origin_v1($1) as o', [ana.start.scheduled_action_id])).rows), 'origen').o;
  if (JSON.stringify(origin) !== JSON.stringify({ oferta: CAMPAIGN, landing: null, disparador: 'pago_fallido' })) {
    fail('el origen de la campana: pago_fallido y la oferta, sin landing', origin);
  }
  // La lista vive en la transaccion y queda vacia al terminar.
  const leftover = await rolledBack(async () => {
    await admitWith(WITH_LIST, ana);
    return one(await sql(`select current_setting('e483.payment_failure_by_phone_offers', true) as v`), 'variable').v;
  });
  if (leftover !== '') fail('la lista queda vacia al terminar la admision', leftover);
  results.admision = { sin_lista: off.reason, sin_la_migracion: red.reason, con_lista: ana.start.outcome,
    autorizacion: grant.evidence.reason, origen: origin };
}

// ---------------------------------------------------------------------------
// 2. El despacho: B1 de su rama.
// ---------------------------------------------------------------------------
{
  ana.chatwoot = '483401';
  const reserved = await reserve(ana, ana.start.scheduled_action_id);
  const [context] = await contextOf(reserved.actionId);
  if (!context || context.send_ordinal !== 1 || !Array.isArray(context.template_branches)
      || context.template_branches.length !== 4) {
    fail('el contexto de B1 trae las cuatro ramas', context);
  }
  const index = branchOf(await dataOf(reserved));
  if (index !== 3) fail('un rechazo generico es la rama banco', index);
  const recorded = await recordOf(reserved, index);
  if (recorded.outcome !== 'recorded' || recorded.template_name !== B1_BANCO) fail('el registro de la rama', recorded);
  // Rojo: con la puerta de antes, el recorrido sin intencion no arranca.
  const red = await withPrevious([TAIL], () => startNew(reserved));
  if (red.outcome === 'request_started') fail('sin la migracion, la puerta no deja salir el recorrido sin intencion', red);
  const started = await startNew(reserved);
  if (started.outcome !== 'request_started' || !started.flow_authorization_id) fail('el arranque de B1', started);
  await accept(ana, reserved);
  const touch2 = await sql(`select status, action_type from public.scheduled_actions where recovery_case_id = $1
    and step_key = 'native_touch_2'`, [ana.start.recovery_case_id]);
  if (touch2.length !== 1 || touch2[0].status !== 'pending' || touch2[0].action_type !== 'no_reply_review') {
    fail('el segundo toque, agendado', touch2);
  }
  results.despacho = { rama: recorded.template_name, arranque: started.outcome, sin_la_migracion: red.reason ?? red.outcome,
    segundo: touch2[0].status };
}

// ---------------------------------------------------------------------------
// 3. Lo que no entra.
// ---------------------------------------------------------------------------
{
  const out = {};
  // Una oferta declarada sin campana.
  const otra = await refusalOf(person('pf-otra-oferta'), { offer: OTHER });
  out.oferta_sin_campana = (await admitWith(WITH_LIST, otra)).reason;
  if (out.oferta_sin_campana !== 'no_entra:permiso') fail('una oferta sin campana', out);
  // Sin checkout_phone (E-441: el bridge usa la identidad que guardo otro momento): el permiso es el telefono del evento.
  const sinTelefono = await refusalOf(person('pf-sin-telefono'), { phone: false });
  out.sin_telefono_en_el_evento = (await admitWith(WITH_LIST, sinTelefono)).reason;
  if (out.sin_telefono_en_el_evento !== 'no_entra:permiso') fail('un evento sin telefono', out);
  // Un pendiente cancelado no es un rechazo (el bridge: sin_reclamo, como hoy).
  const pendiente = await refusalOf(person('pf-pendiente'), { payload: PENDING });
  out.pendiente = (await admitWith(WITH_LIST, pendiente)).reason;
  if (out.pendiente !== 'no_es_un_rechazo') fail('un pendiente cancelado', out);
  // El contacto dado de baja.
  const baja = await refusalOf(person('pf-baja'));
  await sql(`update public.contacts set contact_permission = 'opted_out' where id = $1`, [baja.contact]);
  out.baja = (await admitWith(WITH_LIST, baja)).reason;
  if (out.baja !== 'baja') fail('un contacto dado de baja', out);
  // La baja entre la reevaluacion y el arranque: la puerta la mira.
  const tarde = await refusalOf(person('pf-baja-tarde'));
  tarde.chatwoot = '483402';
  tarde.start = await admitWith(WITH_LIST, tarde);
  if (tarde.start.outcome !== 'motor_arranco') fail('pf-baja-tarde arranca', tarde.start);
  const reserved = await reserve(tarde, tarde.start.scheduled_action_id);
  await recordOf(reserved, branchOf(await dataOf(reserved)));
  await sql(`update public.contacts set contact_permission = 'opted_out' where id = $1`, [tarde.contact]);
  const refused = await startNew(reserved);
  if (refused.outcome === 'request_started' || refused.reason !== 'baja') fail('la puerta frena la baja', refused);
  const authorizations = one(await sql(`select count(*)::int as n from public.flow_engine_request_authorizations
    where action_id = $1`, [reserved.actionId]), 'autorizaciones').n;
  if (authorizations !== 0) fail('la baja no consume cupo', authorizations);
  out.baja_en_la_puerta = refused.reason;
  // Correccion de la revision (2026-11-13): el telefono del evento distinto del destino, una correlacion
  // ambigua de la admision portable y el binding de otra oferta no entran.
  const otroDestino = await refusalOf(person('pf-otro-destino'));
  const elOtro = person('pf-otro-destino-telefono');
  out.destino_distinto = (await admitWith(WITH_LIST, { ...otroDestino, phone: elOtro.phone })).reason;
  if (out.destino_distinto !== 'no_entra:permiso') fail('el destino no es el telefono del evento', out);
  const ambigua = await refusalOf(person('pf-ambigua'));
  if (await correlationOf(ambigua) !== 'unmatched') fail('pf-ambigua: la admision portable la deja unmatched');
  await rolledBack(async () => {
    // La evidencia es inmutable (commercial_ally_payment_failure_evidence_immutable): el cambio vive solo aca.
    await db.exec('alter table public.commercial_ally_payment_failure_details disable trigger user');
    await sql(`update public.commercial_ally_payment_failure_details set correlation_outcome = 'ambiguous'
      where webhook_event_id = $1`, [ambigua.event]);
    out.correlacion_ambigua = (await admitWith(WITH_LIST, ambigua)).reason;
  });
  if (out.correlacion_ambigua !== 'no_entra:permiso') fail('una correlacion ambigua', out);
  const otroBinding = await refusalOf(person('pf-binding-otra-oferta'), { offer: OTHER });
  out.binding_de_otra_oferta = (await admitWith(WITH_LIST, { ...otroBinding, offerCode: CAMPAIGN })).reason;
  if (out.binding_de_otra_oferta !== 'no_entra:permiso') fail('el binding de otra oferta', out);
  results.no_entra = out;
}

// ---------------------------------------------------------------------------
// 4. Con formulario, el permiso de siempre.
// ---------------------------------------------------------------------------
{
  const conForm = person('pf-con-formulario');
  conForm.offer = A.offers.find((o) => o.offer_code === CAMPAIGN);
  await admitForm(conForm, new Date((await dbNow()).getTime() - 50 * 60_000));
  await refusalOf(conForm);
  if (await correlationOf(conForm) !== 'resolved') fail('con formulario, la admision portable correlaciona');
  const started = await admitWith(WITH_LIST, conForm);
  if (started.outcome !== 'motor_arranco') fail('con formulario arranca', started);
  const native = one(await sql('select * from public.flow_engine_native_runs where recovery_case_id = $1',
    [started.recovery_case_id]), 'recorrido');
  if (native.audience_hotmart_phone !== false || !native.audience_purchase_intent_id) {
    fail('con formulario, la intencion y sin la marca', native);
  }
  results.con_formulario = { arranca: started.outcome, intencion: Boolean(native.audience_purchase_intent_id) };
}

await db.close();
console.log(JSON.stringify({ ok: true, ...results }, null, 2));
