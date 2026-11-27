// El flujo que arranca con la palabra clave de una regla (migracion 20261127400000,
// flow-engine-keyword-trigger-v1, Z2 3.9). Contrato: docs/contracts/keyword-rules-v1.md §5.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (el binding, la plantilla A2 del
// carrito de ATT1), la regla de soporte de validate_keyword_rules.mjs publicada por las RPC reales de
// la pagina, y la compra capturada de ATT1 (tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json)
// por la admision portable de hoy, como validate_e483_native_audience_source.mjs. Personas sinteticas.
//
// Prueba, con un rojo por pieza:
//   0. la forma: cambian solo las funciones de la lista, con su marca, sin tocar permisos ni atributos;
//      las tres nuevas no son de la API; el recorrido admite palabra_clave; correrla dos veces aborta;
//   1. sin la clave (el flag apagado): el evento es invalid_flow_engine_event, como hoy, y un evento de
//      Hotmart da lo mismo con la clave y sin ella;
//   2. rojo: con las definiciones de antes y la clave, el evento tampoco entra;
//   3. con la clave: la persona de la conversacion (su identidad de WhatsApp de la cuenta) arranca el
//      flujo de la regla; el caso con la regla y la conversacion; el evento sin telefono; la
//      autorizacion keyword_rule; el despacho ve el producto del recorrido y el numero de la identidad;
//      la primera plantilla sale por la puerta; el mismo mensaje otra vez es el mismo caso; otro mensaje
//      de la misma persona es ya_en_curso;
//   4. las guardas: sin identidad (no_entra:sin_contacto), la baja, la conversacion derivada, quien ya
//      compro; la regla pausada y otra clave son sin_flujo; la compra corta el toque;
//   5. la forma en la base: con contesto_a o con otro permiso, motor_nativo:permiso.
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261127400000_flow_engine_keyword_trigger_v1.sql';
const MARK = 'flow_engine_keyword_trigger';
const CHANGED = [
  'admit_flow_engine_event_v1(jsonb,jsonb)',
  '_flow_engine_admit_native_flow(flow_controls,webhook_events,uuid,text,timestamp with time zone,bigint,bigint,text)',
  '_flow_engine_native_steps(text)',
  '_flow_engine_native_trigger(jsonb)',
  '_flow_engine_native_unsupported(jsonb)',
  '_portable_case_purchase_stop_reason(uuid)',
  'get_followup_execution_context(uuid,text,bigint,timestamp with time zone)',
];
const ADDED = [
  '_flow_engine_admit_native_keyword_v1(jsonb,jsonb)',
  '_flow_engine_native_keyword_reason(jsonb,text,text,uuid,text)',
  '_flow_engine_native_keyword_purchase_stop(uuid)',
];
const PURCHASE = JSON.parse(readFileSync(join(root,
  'tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json'), 'utf8')).payload;
const sha = (text) => createHash('sha256').update(text).digest('hex');
const results = {};
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);

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
    if (!after.get(signature).definition.includes(MARK)) fail(`${signature}: sin la marca`);
    previousDefinitions[signature] = before.get(signature).definition;
  }
  for (const signature of ADDED) {
    const privileges = (await db.query(`select r.rolname, has_function_privilege(r.rolname, $1, 'EXECUTE') as can
      from pg_roles r where r.rolname in ('anon', 'authenticated', 'service_role') order by r.rolname`,
    [`public.${signature}`])).rows;
    if (privileges.some((row) => row.can) || after.get(signature).prosecdef) fail(`${signature} no es para la API`, privileges);
  }
  const check = (await db.query(`select pg_get_constraintdef(oid) as def from pg_constraint
    where conname = 'flow_engine_native_runs_trigger_event_check'`)).rows[0]?.def ?? '';
  if (!check.includes("'palabra_clave'") || !check.includes("'ficha_impresa'")) fail('el recorrido admite palabra_clave', check);
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'flow_engine_keyword_trigger_already_applied') fail('la segunda corrida aborto por otra cosa', error.message);
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { cambiadas: changed.length, agregadas: added.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo, la regla publicada y el flujo.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, A2, INSTANCE, person, register, cycle, versionItem, reserve, claim, startNew, accept, dbNow,
  cartFor, rpc } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
await db.exec('alter role service_role bypassrls');
const as = async (role, run) => {
  await db.exec(`set role ${role}`);
  try {
    return await run();
  } finally {
    await db.exec('reset role');
  }
};
const asService = (run) => as('service_role', run);
const WITH_KEY = { ...INSTANCE, native_keyword: true };

// La regla de soporte, por las RPC reales: la redacta el AE y la publica una persona en la pagina.
const AE = 'U0123456789';
const DAN = 'U0DANSCHWAB1';
const TEAM = 'T0LANCEMOS01';
const inMinutes = async (minutes) => new Date((await dbNow()).getTime() + minutes * 60_000).toISOString();
let serial = 0;
const signIn = async () => {
  serial += 1;
  const state = sha(`kw-state-${serial}`);
  const session = sha(`kw-session-${serial}`);
  await as('flow_change_approver', async () => {
    await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha(`kw-nonce-${serial}`), '/flow-approval/',
      await inMinutes(10));
    await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
    const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, TEAM, DAN,
      await inMinutes(8 * 60));
    if (!created.ok) fail('la sesion', created);
  });
  return session;
};
const SESSION = await signIn();
const publishRule = async (rule) => {
  const saved = await as('automation_expert_change', () => rpc('save_keyword_rule_draft_v1', INSTANCE, null, null,
    rule, 'El link de soporte', AE));
  if (!saved.ok) fail('guardar la regla', saved);
  const published = await as('flow_change_approver', () => rpc('approver_publish_keyword_rule_v1', INSTANCE, SESSION,
    saved.rule_id, saved.fingerprint));
  if (!published.ok) fail('publicar la regla', published);
  return saved.rule_id;
};
const SOPORTE = await publishRule({ clave: 'soporte', nombre: 'Link de soporte', palabra_clave: 'Hola, necesito soporte',
  palabra_normalizada: 'hola necesito soporte', etiqueta: 'soporte', macro_etiqueta: 77 });
const ALUMNOS = await publishRule({ clave: 'alumnos', nombre: 'Link de alumnas', palabra_clave: 'Soy alumna',
  palabra_normalizada: 'soy alumna', etiqueta: 'alumnas', macro_etiqueta: 78 });

// El flujo de la regla de soporte: dos toques, entra con conversacion, sin contesto_a.
const keywordDefinition = (flujo, changes = {}) => ({
  esquema: 'setter-flujo/v1', flujo, producto: A.funnel, estado: 'borrador', nombre: 'Bienvenida de soporte',
  dispara: { palabra_clave: 'soporte' },
  entra: { permiso: 'conversacion', contesto_a: [] },
  pasos: [{ esperar: '1m' }, { ...A2 }, { esperar: '1m' }, { ...A2 }],
  termina: { si: ['compra', 'contesta'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: `att1-${flujo.replace(/_/g, '-')}`, por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona', otra_despues_de: '7d' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  ...changes,
});
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
const soporteFlow = await openNative('soporte_bienvenida', keywordDefinition('soporte_bienvenida'));
const FLOW_IDS = [soporteFlow];

// La persona de una conversacion entrante: un contacto sin telefono ni mail con la identidad de WhatsApp de
// la cuenta (lo que deja admit_inbound_commercial_case_v2), en la cohorte del flujo.
let conversationNumber = 910000;
const lead = async (label, { identity = true } = {}) => {
  const p = person(label);
  conversationNumber += 1;
  p.conversation = conversationNumber;
  p.contact = one(await sql(`insert into public.contacts (metadata) values ('{"source": "chatwoot_inbound"}'::jsonb)
    returning id`), `${label} contacto`).id;
  if (identity) {
    p.identity = one(await sql(`insert into public.channel_identities
      (contact_id, channel, account_id, external_user_id, external_conversation_id, identity_status, metadata)
      values ($1, 'whatsapp', $2, $3, $4, 'active', $5::jsonb) returning id`,
    [p.contact, `chatwoot:${A.accountId}`, p.phone, String(p.conversation), JSON.stringify({ inbox_id: String(A.inboxId) })]),
    `${label} identidad`).id;
  }
  for (const flowId of FLOW_IDS) {
    await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
      [flowId, p.contact]);
  }
  return p;
};
const eventOf = async (p, { rule = SOPORTE, key = 'soporte', message = 1 } = {}) => ({
  fuente: 'palabra_clave', evento_id: `palabra_clave:${p.conversation}:${message}`, regla_id: rule, regla_clave: key,
  momento: (await dbNow()).toISOString(),
  chatwoot: { account_id: A.accountId, inbox_id: A.inboxId, conversation_id: p.conversation, message_id: message },
  destino: p.phone,
});
const admitRows = async (instance, flowEvent) => asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
  [JSON.stringify(instance), JSON.stringify(flowEvent)])).rows);
const admitOne = async (flowEvent, label) => one(await admitRows(WITH_KEY, flowEvent), label);
const errorOf = async (run) => {
  try {
    await run();
  } catch (error) {
    return { code: error.code, message: error.message };
  }
  return null;
};
const runsOf = async (flowId) => sql(`select run.recovery_case_id, run.version, native.trigger_event
  from public.flow_engine_runs run join public.flow_engine_native_runs native using (recovery_case_id)
  where run.flow_id = $1 order by run.started_at`, [flowId]);
const buys = async (p) => {
  // La compra de la persona por su telefono (contact_points: la de un entrante no tiene mail).
  p.email = `keyword-${p.contact.slice(0, 8)}@example.test`;
  await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1, 'email', $2, $2, 'system')`, [p.contact, p.email]);
  const payload = structuredClone(PURCHASE);
  const now = await dbNow();
  const shift = now.getTime() - 60_000 - payload.creation_date;
  for (const field of ['approved_date', 'order_date']) payload.data.purchase[field] += shift;
  payload.creation_date += shift;
  payload.id = `palabra-clave-compra-${p.email}`;
  payload.data.buyer = { ...payload.data.buyer, name: p.name, email: p.email, checkout_phone: p.phone };
  payload.data.product = { ...payload.data.product, id: A.productId };
  payload.data.purchase.offer.code = p.offer.offer_code;
  payload.data.purchase.transaction = `HPKEYWORD${p.contact.replace(/-/g, '').slice(0, 8).toUpperCase()}`;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), p.email, p.phone]), `${p.label} compra`);
  if (admitted.outcome !== 'inserted') fail(`${p.label}: admitir la compra`, admitted);
};

// ---------------------------------------------------------------------------
// 1. Sin la clave del bridge (el flag apagado): como hoy.
// ---------------------------------------------------------------------------
{
  const zoe = await lead('keyword-zoe');
  const flowEvent = await eventOf(zoe);
  const plainRefused = await errorOf(() => admitRows(INSTANCE, flowEvent));
  if (plainRefused?.code !== '22023' || plainRefused.message !== 'invalid_flow_engine_event') {
    fail('sin la clave, como hoy', plainRefused);
  }
  if ((await runsOf(soporteFlow)).length !== 0) fail('sin la clave no arranca nada');
  const cart = await cartFor('keyword-carrito');
  const hotmart = { fuente: 'hotmart', tipo: 'carrito', evento_id: cart.event, producto_ref: A.funnel,
    oferta: cart.offer.offer_code, contacto_id: cart.contact, momento: cart.cartArgs[6],
    chatwoot: { account_id: A.accountId, inbox_id: A.inboxId }, destino: cart.phone };
  const plain = await admitRows(INSTANCE, hotmart);
  const keyed = await admitRows(WITH_KEY, hotmart);
  if (JSON.stringify(plain) !== JSON.stringify(keyed)) fail('el evento de Hotmart cambia con la clave', { plain, keyed });
  results.sin_clave = { palabra_clave: plainRefused.message, hotmart: plain.map((row) => row.outcome) };
}

// ---------------------------------------------------------------------------
// 2. Rojo: con las definiciones de antes y la clave, el evento no entra.
// ---------------------------------------------------------------------------
{
  const rita = await lead('keyword-rita');
  const flowEvent = await eventOf(rita);
  await db.exec('begin');
  let refused;
  try {
    for (const definition of Object.values(previousDefinitions)) await db.exec(definition);
    refused = await errorOf(() => db.query('select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
      [JSON.stringify(WITH_KEY), JSON.stringify(flowEvent)]));
  } finally {
    await db.exec('rollback');
  }
  if (refused?.message !== 'invalid_flow_engine_event') fail('rojo: la admision de antes no conoce la palabra clave', refused);
  results.rojo = refused.message;
}

// ---------------------------------------------------------------------------
// 3. Con la clave.
// ---------------------------------------------------------------------------
{
  const ana = await lead('keyword-ana');
  const row = await admitOne(await eventOf(ana), 'Ana');
  if (row.flow_id !== soporteFlow || row.outcome !== 'motor_arranco' || row.created !== true) fail('Ana arranca', row);
  const runs = await runsOf(soporteFlow);
  if (runs.length !== 1 || runs[0].version !== 1 || runs[0].trigger_event !== 'palabra_clave') fail('el recorrido', runs);
  const kase = one(await sql(`select contact_id, source, offer_code, context, policy_key, selected_channel_identity_id
    from public.recovery_cases where id = $1`, [row.recovery_case_id]), 'caso de Ana');
  if (kase.contact_id !== ana.contact || kase.source !== 'landing' || kase.offer_code !== null
      || kase.context.flow_engine_trigger !== 'palabra_clave' || kase.context.keyword_rule_id !== SOPORTE
      || kase.context.keyword_rule_key !== 'soporte' || kase.context.chatwoot_conversation_id !== ana.conversation
      || kase.policy_key !== `flujo:${soporteFlow}` || kase.selected_channel_identity_id !== ana.identity) {
    fail('el caso de Ana', kase);
  }
  const event = one(await sql(`select e.source, e.event_type, e.external_event_id, e.payload::text as payload
    from public.webhook_events e join public.recovery_cases rc on rc.abandonment_event_id = e.id where rc.id = $1`,
  [row.recovery_case_id]), 'evento');
  if (event.source !== 'system' || event.event_type !== 'FLOW_KEYWORD_MATCHED'
      || event.external_event_id !== `keyword:${SOPORTE}:${ana.conversation}:1` || event.payload.includes(ana.phone)) {
    fail('el evento de Ana (sin telefono)', event);
  }
  const grant = one(await sql(`select authorization_source, evidence from public.contact_authorizations
    where contact_id = $1 and purpose = 'cart_recovery'`, [ana.contact]), 'autorizacion');
  if (grant.authorization_source !== 'system' || grant.evidence.reason !== 'keyword_rule'
      || grant.evidence.keyword_rule_id !== SOPORTE || grant.evidence.chatwoot_message_id !== 1) {
    fail('la autorizacion de Ana', grant);
  }
  // El despacho: el producto del recorrido y el numero de la identidad (el contacto no tiene telefono).
  // Como service_role, el rol del despacho (que no lee flow_engine_runs): el producto sale del context.
  if (kase.context.flow_engine_product_ref !== A.funnel) fail('el producto en el context', kase.context);
  const fence = await claim('keyword-ana-contexto', row.scheduled_action_id);
  await db.exec('grant execute on function public.get_followup_execution_context(uuid,text,bigint,timestamptz) to service_role');
  const context = await asService(async () => one((await db.query(
    'select * from public.get_followup_execution_context($1, $2, $3, $4)',
    [row.scheduled_action_id, fence.worker, fence.lease, fence.now])).rows, 'contexto'));
  if (context.buyer_phone !== ana.phone || context.flow_engine_audience_product_ref !== A.funnel
      || context.offer_code !== null) fail('el contexto del despacho', context);
  // La primera plantilla sale por la puerta.
  const bea = await lead('keyword-bea');
  const beaRow = await admitOne(await eventOf(bea), 'Bea');
  if (beaRow.outcome !== 'motor_arranco') fail('Bea arranca', beaRow);
  const reserved = await reserve(bea, beaRow.scheduled_action_id);
  const mark = await startNew(reserved);
  if (mark.outcome !== 'request_started') fail('la primera plantilla de Bea', mark);
  await accept(bea, reserved);
  const second = (await sql(`select step_key, status from public.scheduled_actions where recovery_case_id = $1
    and step_key = 'native_touch_2'`, [beaRow.recovery_case_id]))[0];
  if (second?.status !== 'pending') fail('el segundo toque de Bea', second);
  // El mismo mensaje otra vez: el mismo caso. Otro mensaje de Ana: la repeticion de la definicion.
  const replay = await admitOne(await eventOf(ana), 'repeticion');
  if (replay.outcome !== 'motor_arranco' || replay.created !== false || replay.recovery_case_id !== row.recovery_case_id) {
    fail('la repeticion del mensaje', replay);
  }
  const again = await admitOne(await eventOf(ana, { message: 2 }), 'otro mensaje');
  if (again.outcome !== 'motor_no_arranca' || again.reason !== 'ya_en_curso') fail('otro mensaje de Ana', again);
  if ((await runsOf(soporteFlow)).length !== 2) fail('dos recorridos');
  results.con_clave = { arranca: row.outcome, telefono_del_despacho: 'identidad', producto: context.flow_engine_audience_product_ref,
    primera: mark.outcome, segundo: second.status, repetido: replay.created, otro_mensaje: again.reason };
}

// ---------------------------------------------------------------------------
// 4. Las guardas.
// ---------------------------------------------------------------------------
{
  const seen = {};
  const nadie = await lead('keyword-sin-identidad', { identity: false });
  const noContact = await admitOne(await eventOf(nadie), 'sin identidad');
  if (noContact.outcome !== 'motor_no_arranca' || noContact.reason !== 'no_entra:sin_contacto') fail('sin identidad', noContact);
  seen.sin_identidad = noContact.reason;
  const carla = await lead('keyword-carla');
  await sql("update public.contacts set contact_permission = 'opted_out' where id = $1", [carla.contact]);
  const optedOut = await admitOne(await eventOf(carla), 'Carla');
  if (optedOut.outcome !== 'motor_no_arranca' || optedOut.reason !== 'baja') fail('la baja', optedOut);
  seen.baja = optedOut.reason;
  const elsa = await lead('keyword-elsa');
  await sql(`insert into public.conversations (contact_id, channel_identity_id, human_takeover) values ($1, $2, true)`,
    [elsa.contact, elsa.identity]);
  const derived = await admitOne(await eventOf(elsa), 'Elsa');
  if (derived.outcome !== 'motor_no_arranca' || derived.reason !== 'conversacion_derivada') fail('la derivada', derived);
  seen.derivada = derived.reason;
  const hilda = await lead('keyword-hilda');
  await buys(hilda);
  const bought = await admitOne(await eventOf(hilda), 'Hilda');
  if (bought.outcome !== 'motor_no_arranca' || bought.reason !== 'compra') fail('Hilda ya compro', bought);
  seen.ya_compro = bought.reason;
  // Otra regla (sin flujo) y una regla que no existe: sin_flujo.
  const ines = await lead('keyword-ines');
  const otherRule = await admitOne(await eventOf(ines, { rule: ALUMNOS, key: 'alumnos' }), 'otra regla');
  const wrongKey = await admitOne(await eventOf(ines, { rule: SOPORTE, key: 'alumnos' }), 'otra clave');
  for (const [label, found] of Object.entries({ otherRule, wrongKey })) {
    if (found.flow_id !== null || found.outcome !== 'sin_flujo') fail(`${label}: sin_flujo`, found);
  }
  seen.otra_regla = otherRule.outcome;
  // La compra corta el toque: Gina entra, compra, y su primer toque se cancela.
  const gina = await lead('keyword-gina');
  const started = await admitOne(await eventOf(gina), 'Gina');
  if (started.outcome !== 'motor_arranco') fail('Gina arranca', started);
  await buys(gina);
  const cut = await reserve(gina, started.scheduled_action_id, { expect: 'cancel' });
  if (cut.decision.reason_code !== 'purchase_detected_any_offer') fail('la compra corta a Gina', cut.decision);
  seen.compra_corta = cut.decision.reason_code;
  // La regla pausada: sin_flujo, sin fila.
  const paused = await as('flow_change_approver', () => rpc('approver_pause_keyword_rule_v1', INSTANCE, SESSION, SOPORTE,
    'pausa de prueba'));
  if (!paused.ok) fail('pausar la regla', paused);
  const jose = await lead('keyword-jose');
  const pausedRow = await admitOne(await eventOf(jose), 'regla pausada');
  if (pausedRow.flow_id !== null || pausedRow.outcome !== 'sin_flujo') fail('la regla pausada', pausedRow);
  seen.regla_pausada = pausedRow.outcome;
  // Un evento mal armado es 22023: el evento_id que no es el de la conversacion y el mensaje.
  const bad = await errorOf(async () => admitRows(WITH_KEY, { ...(await eventOf(jose)), evento_id: 'palabra_clave:1:1' }));
  if (bad?.code !== '22023') fail('un evento mal armado', bad);
  results.guardas = seen;
}

// ---------------------------------------------------------------------------
// 5. La forma en la base.
// ---------------------------------------------------------------------------
{
  const unsupported = async (definition) => one(await sql('select public._flow_engine_native_unsupported($1::jsonb) as r',
    [JSON.stringify(definition)]), 'forma').r;
  const clean = await unsupported(keywordDefinition('x'));
  const repliedTo = await unsupported(keywordDefinition('x', { entra: { permiso: 'conversacion', contesto_a: ['otro'] } }));
  const form = await unsupported(keywordDefinition('x', { entra: { permiso: 'formulario', contesto_a: [] } }));
  const noReply = await unsupported(keywordDefinition('x', { termina: { si: ['compra'], vence: '1d' } }));
  if (clean !== null || repliedTo !== 'motor_nativo:permiso' || form !== 'motor_nativo:permiso'
      || noReply !== 'motor_nativo:forma') {
    fail('la forma de la palabra clave en la base', { clean, repliedTo, form, noReply });
  }
  results.forma_en_la_base = { limpia: clean, contesto_a: repliedTo, formulario: form, sin_contesta: noReply };
  // El texto fijo de F4 (20261127300000): la forma de la palabra clave lo lleva en sus tres toques, como las
  // demas formas nativas menos la ficha, y el compilador lo acepta en un flujo de palabra clave.
  const steps = one(await sql("select public._flow_engine_native_steps('nativo:palabra_clave') as s", []), 'pasos').s;
  if (steps.length !== 3 || !steps.every((slot) => slot.texto_fijo === true)) fail('la forma lleva texto_fijo', steps);
  const compileKeyword = async (variables) => {
    try {
      const definition = keywordDefinition('x', { pasos: [{ ...A2, variables }] });
      const row = one(await sql(`select p.steps from public._flow_engine_compile_policy_core($1::jsonb, $2, 1, $3,
        'America/Mexico_City') p`, [JSON.stringify(definition), soporteFlow, 'nativo:palabra_clave']), 'compilar');
      return { ok: row.steps };
    } catch (error) {
      return { error: error.message };
    }
  };
  const literalOk = await compileKeyword(['nombre', { texto: 'Te escribimos por el link de soporte.' }]);
  const literalBad = await compileKeyword(['nombre', { texto: 'dos\nlineas' }]);
  if (!literalOk.ok || literalBad.error !== 'flow_policy_not_compilable:variables_differ:first_contact') {
    fail('el texto fijo en un flujo de palabra clave', { literalOk, literalBad });
  }
  results.texto_fijo = { compila: true, dos_lineas: literalBad.error };
}

console.log(JSON.stringify(results, null, 2));
await db.close();
