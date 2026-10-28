// El caso de un flujo nativo lleva al despacho la oferta del evento que lo
// abrio (migracion 20261026000200, E-122).
//
// Por que: la compra de prueba de ATT1 del 2026-10-26 abrio el primer
// recorrido nativo (att1/bienvenida) y la plantilla no salio: el despachador
// pide que execution_context.offer_code sea una de las ofertas aceptadas, y
// get_followup_execution_context devolvia el offer_code del caso, que la
// admision nativa deja nulo a proposito.
//
// Datos: la compra es la capturada de esa prueba, anonimizada
// (tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json,
// att1-db webhook_events c1dd277a…, oferta gopi6lh7, producto 5071808). Solo
// se corren sus horas a «hace un minuto» (la accion vence al dia) y su id. La
// definicion del flujo es la de ATT1 (flow_definitions d2d66d13… v1, leida el
// 2026-10-26): att1_bienvenida_01 con ["producto"] y etiquetar cliente.
//
// Prueba, con la cadena entera y un rojo a proposito:
//   0. la migracion: cambia exactamente las dos funciones, con su marca, sin
//      tocar permisos ni atributos; correrla dos veces aborta;
//   1. la compra capturada, aplicada como la aplica el worker y admitida al
//      flujo nativo como la admite el bridge (como service_role), abre el
//      recorrido; el caso sigue con offer_code nulo (los planificadores no lo
//      ven) y lleva la oferta en el context;
//   2. el despacho: claim, reevaluacion, intento; get_followup_execution_context
//      (como service_role, como lo llama el bridge) da la oferta del evento;
//      rojo: con la definicion de antes, la da nula;
//   3. lo que sigue al envio: el arranque nativo autoriza, la aceptacion
//      cierra, y la cola del paso (etiquetar) dice cliente y el producto; ni
//      uno ni otro miran la oferta;
//   4. un carrito de la misma persona y oferta no cae en el caso nativo: la
//      busqueda del caso abierto por oferta (la de los planificadores) no lo
//      encuentra.
//
// Con --emit escribe tests/fixtures/flow_engine/native_purchase_execution_context_rows.json
// (lo que contesta la base, con uuid y horas normalizados) para
// tests/test_flow_engine_native_purchase_e2e.py; con --check (el de npm test)
// lo compara y falla si difiere.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261026000200_flow_engine_native_offer_in_context_v1.sql';
const FIXTURE = join(root, 'tests/fixtures/flow_engine/native_purchase_execution_context_rows.json');
const EMIT = process.argv.includes('--emit');
const CHECK = process.argv.includes('--check');
const TZ = 'timestamp with time zone';
const ADMIT_FN = '_flow_engine_admit_native_flow(flow_controls,webhook_events,uuid,text,timestamp with time zone,bigint,bigint,text)';
const CONTEXT_FN = `get_followup_execution_context(uuid,text,bigint,${TZ})`;
const MARK = 'flow_engine_native_offer_in_context';
const captured = JSON.parse(readFileSync(join(root, 'tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json'), 'utf8'));
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues va solo la de la pagina de aprobacion (A′ PR 2), que no toca la admision ni el despacho.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261028000100_flow_change_slack_approver_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de la pagina de aprobacion`, names.slice(position + 1));
}
if (captured.payload.event !== 'PURCHASE_APPROVED' || captured.payload.data.purchase.offer.code !== 'gopi6lh7'
    || captured.payload.data.product.id !== 5071808) {
  fail('el fixture no es la compra capturada de la bienvenida de ATT1');
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
  const changed = [...before.keys()].filter((s) => after.get(s)?.definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && ['acl', 'prosecdef', 'provolatile'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, [ADMIT_FN, CONTEXT_FN]) || added.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, attributes });
  }
  for (const signature of [ADMIT_FN, CONTEXT_FN]) {
    if (!after.get(signature).definition.includes(MARK)) fail(`${signature}: sin la marca`);
    previousDefinitions[signature] = before.get(signature).definition;
  }
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (!['unexpected_flow_engine_native_admission_definition'].includes(error.message)) {
      fail('la segunda corrida aborto por otra cosa', error.message);
    }
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { cambiadas: changed.length, iguales: before.size - changed.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 (buildWorld, la cadena entera) con la bienvenida nativa.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, dbNow, reserve, startNew, accept } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
// service_role en Supabase salta RLS (BYPASSRLS); los permisos de tabla y de
// funcion son los que dejo la cadena.
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

// La bienvenida de ATT1, como esta en su base (d2d66d13… v1).
const WELCOME = {
  esquema: 'setter-flujo/v1', flujo: 'bienvenida', producto: A.funnel, estado: 'borrador', nombre: 'Bienvenida att1',
  dispara: { evento: 'compra' }, entra: { permiso: 'compra', contesto_a: [] },
  pasos: [{ plantilla: 'att1_bienvenida_01', idioma: 'es_MX', variables: ['producto'] }, { etiquetar: 'cliente' }],
  termina: { si: [], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-bienvenida', por_dia: 'sin_tope', total: 'sin_tope',
    repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
const flowId = await register(A.funnel, 'bienvenida');
await cycle([{ ...versionItem(flowId, A.funnel, 'bienvenida', WELCOME),
  ok_del_cliente: { quien: 'dan', medio: 'whatsapp', fecha: '2026-10-26' } }], 'v1 de la bienvenida');
const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
  [flowId]), 'propuesta').proposal_id;
const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
  [flowId, proposalId, A.timezone]), 'abrir').r);
if (!opened.ok || opened.state !== 'cohorte' || opened.pool !== 'att1-bienvenida') fail('abrir la bienvenida', opened);

// ---------------------------------------------------------------------------
// 1. La compra capturada: aplicada y admitida al flujo nativo.
// ---------------------------------------------------------------------------
const payload = structuredClone(captured.payload);
const buyer = payload.data.buyer;
const phone = buyer.checkout_phone.replace(/\D/g, '');
const offer = payload.data.purchase.offer.code;
const now = await dbNow();
const shift = now.getTime() - 60_000 - payload.creation_date;
for (const key of ['approved_date', 'order_date']) payload.data.purchase[key] += shift;
payload.creation_date += shift;
const contactId = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
  values ($1, $2, $3, 'MX') returning id`, [buyer.name, buyer.email, phone]), 'contacto').id;
await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
  values ($1, 'email', $2, $2, 'hotmart'), ($1, 'phone', $3, $3, 'hotmart')`, [contactId, buyer.email, phone]);
await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
  [flowId, contactId]);
const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
  [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), buyer.email, phone]), 'compra');
if (admitted.outcome !== 'inserted') fail('admitir la compra', admitted);
const applied = one(await sql('select * from public.apply_hotmart_purchase_approved($1,$2,$3,$4,$5,$6,$7::timestamptz)',
  [admitted.webhook_event_id, buyer.email, phone, String(payload.data.product.id), offer,
    payload.data.purchase.transaction, new Date(payload.data.purchase.approved_date).toISOString()]), 'aplicar');
// La forma exacta que arma admit_native_purchase (src/bridge/native_flows.py).
const flowEvent = {
  fuente: 'hotmart', tipo: 'compra', evento_id: admitted.webhook_event_id, producto_ref: A.funnel, oferta: offer,
  contacto_id: contactId, momento: new Date(payload.creation_date).toISOString(),
  chatwoot: { account_id: A.accountId, inbox_id: A.inboxId }, destino: phone,
};
const rows = await asService(async () => (await db.query('select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
  [JSON.stringify(INSTANCE), JSON.stringify(flowEvent)])).rows);
const started = one(rows.filter((row) => row.flow_id === flowId), 'fila de la bienvenida');
if (started.outcome !== 'motor_arranco' || started.created !== true) fail('la compra abre la bienvenida', rows);
const kase = one(await sql('select * from public.recovery_cases where id = $1', [started.recovery_case_id]), 'caso');
const native = one(await sql('select * from public.flow_engine_native_runs where recovery_case_id = $1',
  [started.recovery_case_id]), 'recorrido nativo');
if (kase.offer_code !== null || kase.policy_key !== `flujo:${flowId}` || native.offer_code !== offer
    || kase.context.flow_engine_offer !== offer || kase.product_name !== A.productName) {
  fail('el caso nativo', { offer_code: kase.offer_code, context: kase.context, native: native.offer_code });
}
results.admision = { compra_aplicada: applied.outcome, salida: started.outcome, oferta_en_el_context: kase.context.flow_engine_offer };

// ---------------------------------------------------------------------------
// 2. El despacho: el contexto que lee el chequeo del destinatario.
// ---------------------------------------------------------------------------
const lead = { label: 'bienvenida', chatwoot: '520' };
const reserved = await reserve(lead, started.scheduled_action_id);
if (reserved.attempt.mode !== 'approved_template') fail('el intento', reserved.attempt);
const CONTEXT_SQL = 'select * from public.get_followup_execution_context($1, $2, $3, $4)';
const readContext = () => asService(async () => one((await db.query(CONTEXT_SQL,
  [started.scheduled_action_id, reserved.worker, reserved.lease, reserved.now])).rows, 'contexto'));
const context = await readContext();
if (context.offer_code !== offer || context.product_name !== A.productName || context.buyer_phone !== phone) {
  fail('el contexto del despacho', context);
}
// Rojo: la definicion de antes da la oferta nula (el followup_recipient_not_allowlisted de produccion).
const red = await rolledBack(async () => {
  await db.exec(previousDefinitions[CONTEXT_FN]);
  return readContext();
});
if (red.offer_code !== null) fail('sin la migracion la oferta tendria que ser nula', red);
const step = one(await asService(async () => (await db.query('select * from public.flow_engine_execution_context_v1($1)',
  [started.scheduled_action_id])).rows), 'contexto del paso');
if (step.template_name !== 'att1_bienvenida_01' || JSON.stringify(step.template_variables) !== JSON.stringify(['producto'])) {
  fail('el contexto del paso', step);
}
results.contexto = { oferta: context.offer_code, sin_la_migracion: red.offer_code };

// ---------------------------------------------------------------------------
// 3. Lo que sigue al envio: el arranque, la aceptacion y la cola del paso.
// ---------------------------------------------------------------------------
const startedSend = await startNew(reserved);
if (startedSend.outcome !== 'request_started' || !startedSend.flow_authorization_id) fail('el arranque nativo', startedSend);
const accepted = await accept(lead, reserved);
const tail = one(await asService(async () => (await db.query('select public.flow_engine_native_step_tail_v1($1) as t',
  [started.scheduled_action_id])).rows), 'cola').t;
if (tail.etiquetar !== 'cliente' || tail.producto !== A.productName || Number(tail.version) !== 1) fail('la cola del paso', tail);
results.despues = { arranque: startedSend.outcome, aceptacion: accepted.status, etiquetar: tail.etiquetar };

// ---------------------------------------------------------------------------
// 4. Un carrito de la misma persona y oferta no ve el caso nativo.
// ---------------------------------------------------------------------------
{
  const open = await sql(`select id from public.recovery_cases where contact_id = $1 and source = 'hotmart'
    and external_product_id = $2 and offer_code is not distinct from $3`, [contactId, String(A.productId), offer]);
  if (open.length !== 0) fail('la busqueda por oferta de los planificadores encuentra el caso nativo', open);
}

// ---------------------------------------------------------------------------
// El fixture para el bridge.
// ---------------------------------------------------------------------------
const ids = new Map([[started.scheduled_action_id, 'ACTION'], [started.recovery_case_id, 'CASE'],
  [contactId, 'CONTACT'], [admitted.webhook_event_id, 'EVENT'], [flowId, 'FLOW']]);
const normalize = (value) => JSON.parse(JSON.stringify(value, (key, v) => {
  if (typeof v === 'string' && ids.has(v)) return `<${ids.get(v)}>`;
  if (key === 'momento') return '<MOMENTO>';
  return v;
}));
const contextRow = (row) => normalize({
  action_id: row.action_id, action_type: row.action_type, step_key: row.step_key, recovery_case_id: row.recovery_case_id,
  contact_id: row.contact_id, source_event_id: row.source_event_id, buyer_name: row.buyer_name,
  buyer_email: row.buyer_email, buyer_phone: row.buyer_phone, product_name: row.product_name,
  offer_code: row.offer_code, current_goal: row.current_goal, lead_stage: row.lead_stage,
});
const emitted = {
  _capture: {
    origen: 'validate_flow_engine_native_offer_in_context.mjs: la cadena entera de migraciones (PGlite) con la '
      + 'compra capturada tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json',
    encargo: 'E-122',
    regenerar: 'cd tests/sql/followup_engine && node validate_flow_engine_native_offer_in_context.mjs --emit',
  },
  flow_event: normalize(flowEvent),
  admission: normalize({ flow_id: started.flow_id, outcome: started.outcome, reason: started.reason, created: started.created }),
  execution_context: contextRow(context),
  execution_context_before_migration: contextRow(red),
  step_context: { template_name: step.template_name, template_language: step.template_language,
    template_variables: step.template_variables, send_ordinal: step.send_ordinal },
  step_tail: normalize({ etiquetar: tail.etiquetar, producto: tail.producto }),
};
if (EMIT) {
  writeFileSync(FIXTURE, `${JSON.stringify(emitted, null, 2)}\n`);
} else if (CHECK) {
  const stored = JSON.parse(readFileSync(FIXTURE, 'utf8'));
  if (JSON.stringify(stored) !== JSON.stringify(emitted)) {
    fail('native_purchase_execution_context_rows.json no es lo que da la cadena: correr con --emit', emitted);
  }
}
await db.close();
console.log(`validate_flow_engine_native_offer_in_context: ok ${JSON.stringify(results)}`);
