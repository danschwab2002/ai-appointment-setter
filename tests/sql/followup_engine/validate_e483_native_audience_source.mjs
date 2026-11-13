// La admision nativa acepta el evento de un disparo por audiencia (migracion
// 20261112210600, E-483, para E-484: los disparadores fecha, cada y manual).
//
// Por que: E-484 arma la lista congelada de cada ocurrencia y le pasa cada
// persona a admit_flow_engine_event_v1 con fuente: audiencia, atada al flujo y
// a su version (src/bridge/flow_audience_opener.py, audience_event, en la rama
// de E-484). La admision solo sabia de eventos de Hotmart.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (el binding, el
// formulario con consentimiento por la admision portable de hoy), la plantilla
// A2 del carrito de ATT1 de ese mundo, y la compra capturada de ATT1
// (tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json) por la
// admision portable de hoy, como validate_flow_engine_native_cart.mjs.
//
// Prueba:
//   0. la forma: cambian solo las funciones de la lista, con su marca; las
//      cuatro nuevas no son de la API; correrla dos veces aborta;
//   1. sin la clave (el flag apagado): el evento de audiencia es
//      invalid_flow_engine_event, como hoy, y un evento de Hotmart da lo mismo
//      con la clave y sin ella;
//   2. rojo: con las definiciones de antes y la clave, el evento de audiencia
//      tampoco entra;
//   3. con la clave: dos personas de la lista de un flujo de audiencia
//      publicado -> dos recorridos atados a ese flujo y a la version 1, con la
//      ocurrencia en el caso; la primera plantilla sale (la reevaluacion la
//      ejecuta y la puerta la autoriza contra la bolsa) y el segundo toque queda
//      agendado; el mismo evento otra vez devuelve el mismo caso; otra
//      ocurrencia de la misma persona es ya_en_curso (la repeticion de la
//      definicion);
//   4. las guardas: la baja, sin formulario (no_entra:permiso), la conversacion
//      derivada y la viva no arrancan; la compra corta el toque (la salida
//      compra);
//   5. no se mezcla: otro flujo de audiencia del mismo producto no recibe nada;
//      un evento con otra version, otro tipo u otro flujo es sin_flujo;
//   6. clientas (permiso compra): quien compro entra y su compra de antes no la
//      corta; quien no compro, no_entra:permiso;
//   7. una sola definicion: reembolsada, compra de otro producto, formulario sin
//      permiso, viejo o de otro producto y formulario_en 366d no entran.
import { PGlite } from '@electric-sql/pglite';
import { randomUUID } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261112210600_e483_native_audience_source_v1.sql';
const ADMIT_EVENT = 'admit_flow_engine_event_v1(jsonb,jsonb)';
const CHANGED = [
  ADMIT_EVENT,
  '_flow_engine_admit_native_flow(flow_controls,webhook_events,uuid,text,timestamp with time zone,bigint,bigint,text)',
  '_flow_engine_native_steps(text)',
  '_flow_engine_native_trigger(jsonb)',
  '_flow_engine_native_unsupported(jsonb)',
  '_portable_case_purchase_stop_reason(uuid)',
];
const ADDED = [
  '_flow_engine_admit_native_audience_v1(jsonb,jsonb)',
  '_flow_engine_native_audience_reason(jsonb,text,text,uuid,text)',
  '_flow_engine_native_audience_purchased(text,text,uuid,text,timestamp with time zone)',
  '_flow_engine_native_audience_purchase_stop(uuid)',
];
const PURCHASE = JSON.parse(readFileSync(join(root,
  'tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json'), 'utf8')).payload;
const results = {};
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues va la lista congelada de E-484 (20261112220000): tablas y funciones nuevas, sin tocar la admision.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261112220000_flow_audience_triggers_v1.sql', '20261112230000_e483_native_person_cap_v1.sql',
  // La ficha de OXXO nativa (20261113200000, E-528, C4).
  '20261113200000_e528_native_payment_slip_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de la lista congelada de E-484`, names.slice(position + 1));
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
    if (!after.get(signature).definition.includes('e483_native_audience')) fail(`${signature}: sin la marca`);
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
  if (!check.includes("'audiencia'")) fail('el recorrido admite audiencia', check);
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'e483_native_audience_already_applied') fail('la segunda corrida aborto por otra cosa', error.message);
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { cambiadas: changed.length, agregadas: added.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, A2, B, INSTANCE, person, admitForm, register, cycle, versionItem, reserve, startNew, accept, dbNow,
  cartFor } = world;
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
const WITH_KEY = { ...INSTANCE, native_audience: true };
// Un envio por fecha a prospectas con permiso (lo que escribe el agente en E-484, con los defaults), de dos toques.
const audienceDefinition = (flujo, changes = {}) => ({
  esquema: 'setter-flujo/v1', flujo, producto: A.funnel, estado: 'borrador', nombre: 'Recordatorio de Black',
  dispara: { fecha: { el: '2026-12-01T10:00', zona: 'America/Mexico_City' } },
  para: { es: 'prospecta', formulario_en: '30d' },
  entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ ...A2 }, { esperar: '1m' }, { ...A2 }],
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
const audFlow = await openNative('recordatorio_black', audienceDefinition('recordatorio_black'));
const otherFlow = await openNative('aviso_a_mano', audienceDefinition('aviso_a_mano', { dispara: { manual: {} } }));
const clientFlow = await openNative('novedad_clientas', audienceDefinition('novedad_clientas', {
  dispara: { cada: { dias: ['lun'], hora: '10:00', zona: 'America/Mexico_City' } },
  para: { es: 'clienta' }, entra: { permiso: 'compra', contesto_a: [] } }));
const FLOW_IDS = [audFlow, otherFlow, clientFlow];

// Una persona con contacto, con el formulario con consentimiento (o sin el), en la cohorte de los tres flujos.
const lead = async (label, { form = true } = {}) => {
  const p = person(label);
  if (form) await admitForm(p, new Date((await dbNow()).getTime() - 50 * 60_000));
  p.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
    values ($1, $2, $3, 'MX') returning id`, [p.name, p.email, p.phone]), `${label} contacto`).id;
  await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1, 'email', $2, $2, 'system'), ($1, 'phone', $3, $3, 'system')`, [p.contact, p.email, p.phone]);
  for (const flowId of FLOW_IDS) {
    await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
      [flowId, p.contact]);
  }
  return p;
};
const eventOf = async (p, flowId, occurrence, { version = 1, tipo = 'fecha' } = {}) => ({
  fuente: 'audiencia', tipo, evento_id: `audiencia:${occurrence}:${p.contact}`, flujo_id: flowId, version,
  ocurrencia_id: occurrence, producto_ref: A.funnel, contacto_id: p.contact, momento: (await dbNow()).toISOString(),
  chatwoot: { account_id: A.accountId, inbox_id: A.inboxId }, destino: p.phone,
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
const actionsOf = async (caseId) => sql(`select id, step_key, status, terminal_reason
  from public.scheduled_actions where recovery_case_id = $1 order by created_at`, [caseId]);
const buys = async (p) => {
  const payload = structuredClone(PURCHASE);
  const now = await dbNow();
  const shift = now.getTime() - 60_000 - payload.creation_date;
  for (const key of ['approved_date', 'order_date']) payload.data.purchase[key] += shift;
  payload.creation_date += shift;
  payload.id = `audiencia-compra-${p.email}`;
  payload.data.buyer = { ...payload.data.buyer, name: p.name, email: p.email, checkout_phone: p.phone };
  payload.data.product = { ...payload.data.product, id: A.productId };
  payload.data.purchase.offer.code = p.offer.offer_code;
  payload.data.purchase.transaction = `HPAUDIENCIA${p.contact.replace(/-/g, '').slice(0, 8).toUpperCase()}`;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), p.email, p.phone]), `${p.label} compra`);
  if (admitted.outcome !== 'inserted') fail(`${p.label}: admitir la compra`, admitted);
};
const OCCURRENCE = randomUUID();

// ---------------------------------------------------------------------------
// 1. Sin la clave del bridge (el flag apagado): como hoy.
// ---------------------------------------------------------------------------
{
  const zoe = await lead('audiencia-zoe');
  const flowEvent = await eventOf(zoe, audFlow, OCCURRENCE);
  const refused = await errorOf(() => admitRows(INSTANCE, flowEvent));
  if (refused?.code !== '22023' || refused.message !== 'invalid_flow_engine_event') fail('sin la clave, como hoy', refused);
  if ((await runsOf(audFlow)).length !== 0) fail('sin la clave no arranca nada');
  // Un evento de Hotmart, con la clave y sin ella, da lo mismo.
  const cart = await cartFor('audiencia-carrito');
  const hotmart = { fuente: 'hotmart', tipo: 'carrito', evento_id: cart.event, producto_ref: A.funnel,
    oferta: cart.offer.offer_code, contacto_id: cart.contact, momento: cart.cartArgs[6],
    chatwoot: { account_id: A.accountId, inbox_id: A.inboxId }, destino: cart.phone };
  const plain = await admitRows(INSTANCE, hotmart);
  const keyed = await admitRows(WITH_KEY, hotmart);
  if (JSON.stringify(plain) !== JSON.stringify(keyed)) fail('el evento de Hotmart cambia con la clave', { plain, keyed });
  results.sin_clave = { audiencia: refused.message, hotmart: plain.map((row) => row.outcome) };
}

// ---------------------------------------------------------------------------
// 2. Rojo: con las definiciones de antes y la clave, el evento no entra.
// ---------------------------------------------------------------------------
{
  const rita = await lead('audiencia-rita');
  const flowEvent = await eventOf(rita, audFlow, OCCURRENCE);
  await db.exec('begin');
  let refused;
  try {
    for (const definition of Object.values(previousDefinitions)) await db.exec(definition);
    // Sin cambiar de rol: el error aborta la transaccion y el reset role no correria.
    refused = await errorOf(() => db.query('select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
      [JSON.stringify(WITH_KEY), JSON.stringify(flowEvent)]));
  } finally {
    await db.exec('rollback');
  }
  if (refused?.message !== 'invalid_flow_engine_event') fail('rojo: la admision de antes no conoce la audiencia', refused);
  results.rojo = refused.message;
}

// ---------------------------------------------------------------------------
// 3. Con la clave: dos personas, dos recorridos atados al flujo y su version.
// ---------------------------------------------------------------------------
const ana = await lead('audiencia-ana');
const bea = await lead('audiencia-bea');
{
  const started = [];
  for (const p of [ana, bea]) {
    const rows = await admitRows(WITH_KEY, await eventOf(p, audFlow, OCCURRENCE));
    const row = one(rows, `${p.label} admision`);
    if (row.flow_id !== audFlow || row.outcome !== 'motor_arranco' || row.created !== true) fail(`${p.label} arranca`, rows);
    p.caseId = row.recovery_case_id;
    p.first = row.scheduled_action_id;
    started.push(row);
  }
  const runs = await runsOf(audFlow);
  if (runs.length !== 2 || runs.some((run) => run.version !== 1 || run.trigger_event !== 'audiencia')) {
    fail('dos recorridos de audiencia en la version 1', runs);
  }
  const kase = one(await sql(`select source, context, policy_key, policy_version from public.recovery_cases
    where id = $1`, [ana.caseId]), 'caso de Ana');
  if (kase.source !== 'landing' || kase.context.flow_engine_trigger !== 'audiencia'
      || kase.context.flow_audience_occurrence_id !== OCCURRENCE || kase.context.flow_audience_form !== 'fecha'
      || kase.policy_key !== `flujo:${audFlow}` || kase.policy_version !== 1) fail('el caso de Ana', kase);
  const event = one(await sql(`select e.source, e.event_type, e.external_event_id from public.webhook_events e
    join public.recovery_cases rc on rc.abandonment_event_id = e.id where rc.id = $1`, [ana.caseId]), 'evento');
  if (event.source !== 'system' || event.event_type !== 'FLOW_AUDIENCE_RELEASED'
      || event.external_event_id !== `flow_audience:${audFlow}:1:${OCCURRENCE}:${ana.contact}`) fail('el evento de Ana', event);
  const grant = one(await sql(`select authorization_source, evidence from public.contact_authorizations
    where contact_id = $1 and purpose = 'cart_recovery'`, [ana.contact]), 'autorizacion');
  if (grant.authorization_source !== 'system' || grant.evidence.reason !== 'flow_audience'
      || grant.evidence.flow_audience_occurrence_id !== OCCURRENCE || grant.evidence.permiso !== 'formulario') {
    fail('la autorizacion de Ana', grant);
  }
  // La primera plantilla sale: la reevaluacion la ejecuta, la puerta la autoriza, y queda el segundo toque.
  const reserved = await reserve(ana, ana.first);
  const mark = await startNew(reserved);
  if (mark.outcome !== 'request_started') fail('la primera plantilla de Ana', mark);
  await accept(ana, reserved);
  const actions = await actionsOf(ana.caseId);
  const second = actions.find((row) => row.step_key === 'native_touch_2');
  if (!second || second.status !== 'pending') fail('el segundo toque de Ana', actions);
  const spent = one(await sql(`select count(*)::int as n from public.flow_engine_request_authorizations
    where flow_id = $1`, [audFlow]), 'bolsa').n;
  if (spent !== 1) fail('la bolsa cuenta el envio', spent);
  // El mismo evento otra vez: el mismo caso, sin otro recorrido.
  const replay = await admitOne(await eventOf(ana, audFlow, OCCURRENCE), 'repeticion');
  if (replay.outcome !== 'motor_arranco' || replay.created !== false || replay.recovery_case_id !== ana.caseId) {
    fail('la repeticion del evento', replay);
  }
  // Otra ocurrencia de la misma persona: la repeticion de la definicion (7d, con el caso en curso).
  const again = await admitOne(await eventOf(ana, audFlow, randomUUID()), 'otra ocurrencia');
  if (again.outcome !== 'motor_no_arranca' || again.reason !== 'ya_en_curso') fail('otra ocurrencia de Ana', again);
  if ((await runsOf(audFlow)).length !== 2) fail('la repeticion no suma recorridos');
  results.con_clave = { recorridos: 2, version: 1, primera: mark.outcome, segundo: second.status,
    repetido: replay.created, otra_ocurrencia: again.reason };
}

// ---------------------------------------------------------------------------
// 4. Las guardas.
// ---------------------------------------------------------------------------
{
  const carla = await lead('audiencia-carla');
  await sql("update public.contacts set contact_permission = 'opted_out' where id = $1", [carla.contact]);
  const dora = await lead('audiencia-dora', { form: false });
  const identityOf = async (p) => one(await sql(`insert into public.channel_identities
    (contact_id, channel, account_id, external_user_id, identity_status, metadata)
    values ($1, 'whatsapp', $2, $3, 'active', $4::jsonb) returning id`,
  [p.contact, `chatwoot:${A.accountId}`, p.phone, JSON.stringify({ inbox_id: A.inboxId })]), 'identidad').id;
  const elsa = await lead('audiencia-elsa');
  await sql(`insert into public.conversations (contact_id, channel_identity_id, human_takeover) values ($1, $2, true)`,
    [elsa.contact, await identityOf(elsa)]);
  const flor = await lead('audiencia-flor');
  const florConversation = one(await sql(`insert into public.conversations (contact_id, channel_identity_id)
    values ($1, $2) returning id`, [flor.contact, await identityOf(flor)]), 'conversacion').id;
  await sql(`insert into public.messages (conversation_id, direction, actor_type, content, occurred_at)
    values ($1, 'inbound', 'prospect', 'Hola, una duda', now() - interval '2 hours')`, [florConversation]);
  // E-531: la tabla messages del setter guarda solo salientes (Maribel, 13/11: 264 salientes y 0 entrantes) y
  // conversations.last_inbound_at no se llena nunca. Lo que el setter registra de un entrante es el turno del
  // agente (agent_turn_provenance, por el numero de Chatwoot) y la adopcion (conversation_events inbound_*),
  // la senal de recuperador_estado.py (E-511). Hugo: un turno de hace 2 h en su conversacion de Chatwoot;
  // Ines: la adopcion de su conversacion de una plantilla hace 3 h. Ninguno tiene un entrante en messages.
  const hugo = await lead('audiencia-hugo');
  await sql(`insert into public.conversations (contact_id, channel_identity_id, commercial_context)
    values ($1, $2, '{"chatwoot_conversation_id": "483611"}'::jsonb)`, [hugo.contact, await identityOf(hugo)]);
  await sql(`insert into public.agent_turn_provenance (tenant_ref, scope_ref, external_conversation_id, turn_digest,
      occurred_at, outcome, model_requested, bridge_release, context_builder_version, context_digest, context_added)
    values ($1, 'audiencia', 483611, $2, now() - interval '2 hours', 'completed', 'modelo', 'v1.37.0', 'v1', $2, '{}'::jsonb)`,
  [A.tenant, 'a'.repeat(63) + '1']);
  const ines = await lead('audiencia-ines');
  const inesConversation = one(await sql(`insert into public.conversations (contact_id, channel_identity_id)
    values ($1, $2) returning id`, [ines.contact, await identityOf(ines)]), 'conversacion de Ines').id;
  await sql(`insert into public.conversation_events (conversation_id, event_type, actor_type, data, created_at)
    values ($1, 'inbound_adopted_template_conversation', 'integration', '{"chatwoot_conversation_id": "483612"}'::jsonb,
            now() - interval '3 hours')`, [inesConversation]);
  const expected = { [carla.label]: 'baja', [dora.label]: 'no_entra:permiso', [elsa.label]: 'conversacion_derivada',
    [flor.label]: 'conversacion_viva', [hugo.label]: 'conversacion_viva', [ines.label]: 'conversacion_viva' };
  const seen = {};
  for (const p of [carla, dora, elsa, flor, hugo, ines]) {
    const row = await admitOne(await eventOf(p, audFlow, OCCURRENCE), p.label);
    if (row.outcome !== 'motor_no_arranca' || row.reason !== expected[p.label]) fail(`${p.label} no entra`, row);
    seen[p.label.replace('audiencia-', '')] = row.reason;
  }
  // La baja de Chatwoot entre la reevaluacion y el arranque (la reevaluacion no la ve): la puerta la mira,
  // sin consumir cupo.
  const lia = await lead('audiencia-lia');
  const liaRow = await admitOne(await eventOf(lia, audFlow, OCCURRENCE), 'Lia');
  if (liaRow.outcome !== 'motor_arranco') fail('Lia arranca', liaRow);
  const liaReserved = await reserve(lia, liaRow.scheduled_action_id);
  await sql(`insert into public.contact_opt_out_events (channel, purpose, source, canonical_account_id,
      canonical_inbox_id, canonical_conversation_id, canonical_message_id, external_user_id, occurred_at,
      normalized_rule_key, correlation_status)
    values ('whatsapp', 'cart_recovery', 'chatwoot', $1, $2, 483601, 483601, $3, now(), 'baja', 'unmatched')`,
  [A.accountId, A.inboxId, lia.phone]);
  const gate = await startNew(liaReserved);
  if (gate.outcome === 'request_started' || gate.reason !== 'baja') fail('la puerta frena la baja de Lia', gate);
  const liaSpent = one(await sql(`select count(*)::int as n from public.flow_engine_request_authorizations
    where action_id = $1`, [liaReserved.actionId]), 'autorizaciones').n;
  if (liaSpent !== 0) fail('la baja no consume cupo', liaSpent);
  seen.baja_en_la_puerta = gate.reason;
  // La compra como salida: Gina entra, compra, y su primer toque se corta.
  const gina = await lead('audiencia-gina');
  const row = await admitOne(await eventOf(gina, audFlow, OCCURRENCE), 'Gina');
  if (row.outcome !== 'motor_arranco') fail('Gina arranca', row);
  await buys(gina);
  const cut = await reserve(gina, row.scheduled_action_id, { expect: 'cancel' });
  if (cut.decision.reason_code !== 'purchase_detected_any_offer') fail('la compra corta a Gina', cut.decision);
  const ginaCase = one(await sql('select status from public.recovery_cases where id = $1', [row.recovery_case_id]), 'caso');
  if (ginaCase.status !== 'cancelled') fail('el caso de Gina se cierra', ginaCase);
  // Y quien ya compro no entra a un envio a prospectas.
  const hilda = await lead('audiencia-hilda');
  await buys(hilda);
  const bought = await admitOne(await eventOf(hilda, audFlow, OCCURRENCE), 'Hilda');
  if (bought.outcome !== 'motor_no_arranca' || bought.reason !== 'compra') fail('Hilda ya compro', bought);
  results.guardas = { ...seen, compra_corta: cut.decision.reason_code, ya_compro: bought.reason };
}

// ---------------------------------------------------------------------------
// 5. No se mezcla.
// ---------------------------------------------------------------------------
{
  if ((await runsOf(otherFlow)).length !== 0) fail('el otro flujo del producto no recibio nada');
  const before = (await runsOf(audFlow)).length;
  const ines = await lead('audiencia-ines');
  const wrongVersion = await admitOne(await eventOf(ines, audFlow, OCCURRENCE, { version: 2 }), 'otra version');
  const wrongType = await admitOne(await eventOf(ines, audFlow, OCCURRENCE, { tipo: 'manual' }), 'otro tipo');
  const unknown = await admitOne(await eventOf(ines, randomUUID(), OCCURRENCE), 'otro flujo');
  for (const [label, row] of Object.entries({ wrongVersion, wrongType, unknown })) {
    if (row.flow_id !== null || row.outcome !== 'sin_flujo') fail(`${label}: sin_flujo`, row);
  }
  // El otro flujo, con su evento, arranca el suyo: el de Ana en recordatorio_black no lo frena.
  const own = await admitOne(await eventOf(ana, otherFlow, randomUUID(), { tipo: 'manual' }), 'Ana en el otro flujo');
  if (own.flow_id !== otherFlow || own.outcome !== 'motor_arranco' || own.recovery_case_id === ana.caseId) {
    fail('el otro flujo arranca el suyo', own);
  }
  if ((await runsOf(audFlow)).length !== before || (await runsOf(otherFlow)).length !== 1) fail('cada flujo con lo suyo');
  results.no_se_mezcla = { otra_version: wrongVersion.outcome, otro_tipo: wrongType.outcome, otro_flujo: unknown.outcome,
    el_otro: own.outcome };
}

// ---------------------------------------------------------------------------
// 6. Clientas: permiso compra.
// ---------------------------------------------------------------------------
{
  const julia = await lead('audiencia-julia', { form: false });
  await buys(julia);
  const kim = await lead('audiencia-kim', { form: false });
  const started = await admitOne(await eventOf(julia, clientFlow, OCCURRENCE, { tipo: 'cada' }), 'Julia');
  if (started.outcome !== 'motor_arranco') fail('Julia, clienta, entra', started);
  const refused = await admitOne(await eventOf(kim, clientFlow, OCCURRENCE, { tipo: 'cada' }), 'Kim');
  if (refused.outcome !== 'motor_no_arranca' || refused.reason !== 'no_entra:permiso') fail('Kim no compro', refused);
  // Su compra de antes no corta su envio: con permiso compra solo cuenta una posterior.
  const reserved = await reserve(julia, started.scheduled_action_id);
  const mark = await startNew(reserved);
  if (mark.outcome !== 'request_started') fail('el envio de Julia', mark);
  results.clientas = { compro: started.outcome, no_compro: refused.reason, envio: mark.outcome };
}

// ---------------------------------------------------------------------------
// 7. «Compro» y el formulario con la definicion de la lista (correccion de la
//    revision, 2026-11-13): una compra reembolsada o de otro producto no es
//    permiso compra; formulario_en fuera de 1d a 365d no es una ventana; el
//    formulario sin permiso de WhatsApp, viejo o de otro producto no entra.
// ---------------------------------------------------------------------------
{
  const reversed = async (transaction) => sql(`insert into public.webhook_events
      (source, external_event_id, event_type, payload, processing_status) values ('hotmart', $1, 'PURCHASE_REFUNDED', $2::jsonb, 'processed')`,
  [`audiencia-reembolso-${transaction}`, JSON.stringify({ id: `audiencia-reembolso-${transaction}`, event: 'PURCHASE_REFUNDED',
    version: '2.0.0', data: { product: { id: A.productId }, purchase: { transaction, status: 'REFUNDED' } } })]);
  const lia = await lead('audiencia-lia', { form: false });
  await buys(lia);
  await reversed(`HPAUDIENCIA${lia.contact.replace(/-/g, '').slice(0, 8).toUpperCase()}`);
  const refunded = await admitOne(await eventOf(lia, clientFlow, OCCURRENCE, { tipo: 'cada' }), 'Lia');
  if (refunded.outcome !== 'motor_no_arranca' || refunded.reason !== 'no_entra:permiso') fail('Lia, reembolsada', refunded);

  // Otro producto: la compra de B (att9, el otro producto de la instancia) no es una compra de A.
  const mora = await lead('audiencia-mora', { form: false });
  const payload = structuredClone(PURCHASE);
  payload.id = `audiencia-compra-b-${mora.email}`;
  payload.data.buyer = { ...payload.data.buyer, name: mora.name, email: mora.email, checkout_phone: mora.phone };
  payload.data.product = { ...payload.data.product, id: B.productId, name: B.productName };
  payload.data.purchase.offer.code = B.offers[0].offer_code;
  payload.data.purchase.transaction = 'HPAUDIENCIAB0001';
  const admittedB = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [B.tenant, B.funnel, B.bindingVersion, payload.id, JSON.stringify(payload), mora.email, mora.phone]), 'Mora compra B');
  if (admittedB.outcome !== 'inserted') fail('Mora: la compra de B', admittedB);
  const other = await admitOne(await eventOf(mora, clientFlow, OCCURRENCE, { tipo: 'cada' }), 'Mora');
  if (other.outcome !== 'motor_no_arranca' || other.reason !== 'no_entra:permiso') fail('Mora compro otro producto', other);

  // El formulario: sin permiso de WhatsApp, de hace mas de 30 dias, de otro producto.
  const variantsUpdate = async (p, set) => sql(`update public.purchase_intents set ${set}
    where tenant_ref = $1 and normalized_phone = any(public._whatsapp_phone_variants($2))`, [A.tenant, p.phone]);
  const noWa = await lead('audiencia-sin-wa');
  await variantsUpdate(noWa, 'whatsapp_contact_authorized = false');
  const old = await lead('audiencia-vieja');
  await variantsUpdate(old, "submitted_at = now() - interval '31 days'");
  const otherForm = await lead('audiencia-otro-form');
  await variantsUpdate(otherForm, `funnel_ref = '${B.funnel}'`);
  const forms = {};
  for (const p of [noWa, old, otherForm]) {
    const row = await admitOne(await eventOf(p, audFlow, randomUUID()), p.label);
    forms[p.label] = row.reason;
  }
  if (Object.values(forms).some((reason) => reason !== 'no_entra:permiso')) fail('el formulario que no entra', forms);

  // formulario_en: 366d no es una ventana (antes entraba con 366 dias).
  const nora = await lead('audiencia-nora');
  await db.exec('begin');
  let window366;
  try {
    // flow_definitions es inmutable (reject_flow_store_mutation): el cambio vive solo en esta transaccion.
    await db.exec('alter table public.flow_definitions disable trigger user');
    await sql(`update public.flow_definitions set definition = jsonb_set(definition, '{para,formulario_en}', '"366d"'::jsonb)
      where flow_id = $1 and version = 1`, [audFlow]);
    window366 = await admitOne(await eventOf(nora, audFlow, randomUUID()), 'Nora');
  } finally {
    await db.exec('rollback');
  }
  if (window366.outcome !== 'motor_no_arranca' || window366.reason !== 'no_entra:permiso') fail('formulario_en 366d', window366);
  results.una_definicion = { reembolsada: refunded.reason, otro_producto: other.reason, formularios: Object.values(forms),
    formulario_en_366d: window366.reason };
}

console.log(JSON.stringify(results, null, 2));
await db.close();
