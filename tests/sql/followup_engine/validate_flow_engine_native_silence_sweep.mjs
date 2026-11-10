// Lo que lee el barrido nativo del seguimiento a 24 h con cupon, en la base
// (migracion 20261102000300, E-269, P22-3 de M16; diseno: el seguimiento y la
// ficha nativos, 1.2 (c)).
//
// Por que: P22-2 (20261101000200) dejo publicar el seguimiento como un silencio
// nativo y le dio su reserva, pero nadie la llamaba, y la barrera del legado le
// saca la conversacion al barrido de hoy (la advertencia de E-251: P22-2 sin
// P22-3 deja sin cupon a la cohorte). El barrido nativo del bridge lee los
// silencios que corren con read_flow_engine_native_silences_v1 y reserva con
// claim_flow_engine_silence_v1 usando lo que leyo.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera), con las conversaciones adoptadas por los caminos de hoy, como en
// validate_followup_stops.mjs: el formulario, el carrito capturado
// (tests/fixtures/hotmart_cart_abandonment_rejected_v1.json), A1 (y, con Z, A2 y
// Z por la politica de tres toques del mundo) aceptados por Chatwoot, y la
// respuesta por la admision portable. La plantilla del seguimiento y su idioma
// salen del catalogo capturado de la bandeja 11
// (tests/fixtures/chatwoot_inbox_11_message_templates_20261010.json), con
// TIROIDES10. Los telefonos son sinteticos.
//
// Prueba:
//   0. la forma: suma exactamente una funcion, no cambia ninguna (ni su dueno,
//      security definer, volatilidad, search_path o permisos); la nueva es
//      stable, security definer, con el search_path de las del motor, y solo
//      service_role la ejecuta; correrla dos veces aborta sin cambiar nada; con
//      un disparador sin el silencio (otra migracion lo reemplazo entero) aborta
//      con 55000 sin crear nada;
//   1. sin un silencio nativo la lectura da una lista vacia, tambien con un
//      flujo nativo de evento prendido (la bienvenida);
//   2. en cohorte: el silencio con su version, su sha256, la definicion activa
//      tal cual y las conversaciones de Chatwoot de los contactos de la cohorte
//      (vacia sin miembros; la de cada miembro, y solo esas: no la de un miembro de otro flujo);
//   3. lo que lee es lo que pide la reserva: con el flujo, la version y el
//      sha256 leidos, la nativa reserva para un miembro de la cohorte; y para
//      un miembro al que le salio Z del carrito (lo que E-267 hace mas
//      frecuente: la respuesta escrita a A2 adelanta Z) da
//      blocked_after_last_touch sin escribir nada ni gastar la bolsa;
//   4. pausado no aparece; prendido aparece con las conversaciones en null; con una ruta del legado
//      no aparece;
//   5. una version nueva cambia la version, el sha256 y la definicion leidas;
//   6. otra instancia no se lee (22023 de _flow_store_instance);
//   7. si el disparador pierde el silencio, la lectura falla con 55000, como la
//      reserva nativa;
//   8. solo service_role la ejecuta.
import { PGlite } from '@electric-sql/pglite';
import { randomBytes } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261102000300_flow_engine_native_silence_sweep_v1.sql';
const READ = 'read_flow_engine_native_silences_v1(jsonb)';
const NATIVE = 'claim_flow_engine_silence_v1(uuid,integer,text,bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamp with time zone)';
const TRIGGER = '_flow_engine_native_trigger(jsonb)';
const results = {};
const fixture = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));
const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);

const names = readdirSync(join(root, 'supabase/migrations')).filter((name) => name.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues va solo el reciclado de un telefono de prueba (20261102000400, E-270).
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261102000400_identity_recycle_v1.sql', '20261103000500_meta_delivery_rejections_v1.sql',
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
  '20261111000200_pending_cancel_keeps_first_contact_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo el reciclado`, names.slice(position + 1));
}

const CATALOG = fixture('chatwoot_inbox_11_message_templates_20261010.json');
const template = one((CATALOG.message_templates ?? []).filter((item) => item.name === 'att1_seguimiento_descuento_01'),
  'la plantilla del seguimiento');
const TEMPLATE = { name: template.name, language: template.language };
const COUPON = 'TIROIDES10';
// El primer barrido de ATT1 (cada 600 s) despues de las 24 h de silencio.
const AGE = 86_400 + 600;

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
  // El freno: un disparador sin el silencio (otra migracion lo reemplazo entero) aborta sin crear nada.
  {
    const trigger = before.get(TRIGGER).definition;
    const altered = trigger.replaceAll('flow_engine_native_silence_trigger', 'otro_disparador');
    if (altered === trigger) fail('el freno: no se pudo armar el disparador sin el silencio');
    await db.exec('begin');
    let caught = null;
    try {
      await db.exec(altered);
      await db.exec(inside);
    } catch (error) {
      caught = error;
    }
    await db.exec('rollback');
    if (caught?.code !== '55000' || caught.message !== 'flow_engine_native_silence_sweep_missing') {
      fail('el freno del disparador sin el silencio', { code: caught?.code, message: caught?.message });
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
  const changed = [...before.keys()].filter((s) => after.has(s)
    && ['definition', 'acl', 'prosecdef', 'provolatile', 'proowner', 'proconfig'].some((k) =>
      JSON.stringify(after.get(s)[k]) !== JSON.stringify(before.get(s)[k])));
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  if (changed.length !== 0 || removed.length !== 0 || !same(added, [READ])) {
    fail('la migracion cambio otra cosa', { changed, added, removed });
  }
  const read = after.get(READ);
  const native = after.get(NATIVE);
  if (read.prosecdef !== true || read.provolatile !== 's' || read.proowner !== native.proowner
      || !same(read.proconfig, ['search_path=pg_catalog, public, pg_temp'])) {
    fail('la lectura no tiene la forma de las del motor', read);
  }
  const acl = one((await db.query(`select
      has_function_privilege('service_role', $1::regprocedure, 'execute') as service,
      has_function_privilege('anon', $1::regprocedure, 'execute') as anon,
      has_function_privilege('authenticated', $1::regprocedure, 'execute') as authenticated`,
  [`public.${READ}`])).rows, 'acl');
  if (!acl.service || acl.anon || acl.authenticated) fail('el acl de la lectura', acl);
  let error = null;
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  } catch (caught) {
    error = caught;
  }
  await db.exec('rollback').catch(() => {});
  if (error?.code !== '55000' || error.message !== 'flow_engine_native_silence_sweep_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  const again = await snapshot(db);
  if (again.size !== after.size
      || [...after.keys()].some((s) => !again.has(s) || again.get(s).definition !== after.get(s).definition)) {
    fail('la segunda corrida cambio algo');
  }
  results.forma = { cambiadas: changed.length, nuevas: added.length, segunda_corrida: error.message };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, stateItem, cartFor, planCart, reserve, startOld, accept, stepOf,
  sleep, control, pauseFlow } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
await db.exec('alter role service_role bypassrls');
const asService = async (run) => {
  await db.exec('set role service_role');
  try {
    return await run();
  } finally {
    try {
      await db.exec('reset role');
    } catch {
      // dentro de una transaccion abortada; el rollback devuelve el rol
    }
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
const readSilences = async (instance = INSTANCE) => JSON.parse(one(await asService(() => sql(
  'select public.read_flow_engine_native_silences_v1($1::jsonb)::text as r', [JSON.stringify(instance)])), 'lectura').r);

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

let messageSerial = 0;
const sendTouch = async (lead, actionId, label) => {
  const reserved = await reserve(lead, actionId);
  const started = await startOld(reserved);
  if (started.phase !== 'request_started') fail(`${lead.label}: ${label} no arranco`, started);
  await accept(lead, reserved);
  return reserved;
};
// La conversacion adoptada. Con withLastTouch, antes de contestar le salen A2 y Z.
const adopted = async (label, { withLastTouch = false } = {}) => {
  const lead = await cartFor(label);
  const plan = await planCart(lead);
  if (!plan.scheduled_action_id) fail(`${label}: el carrito no planifico`, plan);
  await sendTouch(lead, plan.scheduled_action_id, 'A1');
  if (withLastTouch) {
    await sleep(1_100);
    await sendTouch(lead, (await stepOf(lead, 'cart_second_touch')).id, 'A2');
    await sleep(1_100);
    const z = await stepOf(lead, 'cart_last_touch');
    await sendTouch(lead, z.id, 'Z');
    const accepted = one(await sql(`select count(*)::integer as n from public.followup_delivery_attempts
      where action_id = $1 and outcome = 'accepted_by_chatwoot' and accepted_at is not null`, [z.id]), 'Z aceptado').n;
    if (accepted !== 1) fail(`${label}: Z no quedo aceptado`, accepted);
  }
  const reply = one(await asService(() => sql('select * from public.admit_portable_inbound_commercial_case_v1($1,$2,$3,$4)',
    [A.inboundScope, A.inboundVersion, Number(lead.chatwoot), lead.phone])), `${label} respuesta`);
  const conversation = one(await sql(`select id, contact_id from public.conversations
    where commercial_context ->> 'chatwoot_conversation_id' = $1`, [lead.chatwoot]), `${label} conversacion`);
  const adoptions = one(await sql(`select count(*)::integer as n from public.conversation_events
    where conversation_id = $1 and event_type = 'inbound_adopted_template_conversation'`, [conversation.id]), 'adopcion').n;
  if (adoptions !== 1) fail(`${label}: la conversacion no quedo adoptada`, adoptions);
  lead.conversationContact = conversation.contact_id;
  lead.caseId = reply.commercial_case_id;
  messageSerial += 1;
  lead.inbound = 790_000 + messageSerial * 10;
  lead.outbound = lead.inbound + 1;
  return lead;
};

// La reserva nativa con lo que leyo el barrido (el flujo, la version y el sha256), como _follow_up.
const marks = (from, count) => Array.from({ length: count }, (_, i) => `$${from + i}`).join(',');
const claimWith = async (flow, lead) => one(await asService(() => sql(
  `select * from public.claim_flow_engine_silence_v1($1,$2,$3,${marks(4, 15)})`,
  [flow.flow_id, flow.active_version, flow.definition_sha256, Number(lead.chatwoot), A.accountId, A.inboxId, lead.phone,
    lead.email, `followup:${lead.chatwoot}:${lead.outbound}`, 'link_sent_no_purchase',
    flow.definition.pasos[0].plantilla, flow.definition.pasos[0].idioma, flow.definition.pasos[0].boton.cupon,
    lead.inbound, lead.outbound, AGE, ulid(), null])), `${lead.label} reserva nativa`);
const footprint = async () => one(await sql(`
  select (select count(*)::integer from public.conversation_followup_events) as followups,
         (select count(*)::integer from public.checkout_link_issuances) as issuances,
         (select count(*)::integer from public.flow_engine_request_authorizations) as authorizations
`), 'huella');
const NO_DELTA = { followups: 0, issuances: 0, authorizations: 0 };
const behaviour = async (run) => {
  const start = await footprint();
  const row = await run();
  const flowOf = row.followup_event_id ? one(await sql(`select flow_id, flow_version, definition_sha256
    from public.conversation_followup_events where id = $1`, [row.followup_event_id]), 'la fila') : null;
  return { outcome: row.outcome, flowOf,
    delta: Object.fromEntries(Object.entries(await footprint()).map(([k, v]) => [k, v - start[k]])) };
};

// ---------------------------------------------------------------------------
// 1. Sin un silencio nativo: vacia, tambien con un flujo nativo de evento prendido.
// ---------------------------------------------------------------------------
const openNative = async (flowKey, definition) => {
  const flowId = await register(A.funnel, flowKey);
  await cycle([versionItem(flowId, A.funnel, flowKey, { ...definition, producto: A.funnel })], `v1 de ${flowKey}`);
  const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'propuesta').proposal_id;
  const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [flowId, proposalId, A.timezone]), 'abrir').r);
  if (!opened.ok || opened.state !== 'cohorte') fail(`abrir ${flowKey}`, opened);
  return flowId;
};
const COMPRA = {
  esquema: 'setter-flujo/v1', flujo: 'bienvenida_nativa', producto: A.funnel, estado: 'borrador', nombre: 'Bienvenida',
  dispara: { evento: 'compra' }, entra: { permiso: 'compra', contesto_a: [] },
  pasos: [{ plantilla: 'att1_bienvenida_01', idioma: 'es_MX', variables: ['nombre', 'producto'] }],
  termina: { si: [] }, horario: 'siempre',
  topes: { bolsa: 'att1-bienvenida-nativa', por_dia: 'sin_tope', total: 'sin_tope',
    repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
let welcomeFlow = null;
{
  const empty = await readSilences();
  const welcome = await openNative('bienvenida_nativa', COMPRA);
  welcomeFlow = welcome;
  await cycle([stateItem(welcome, A.funnel, 'bienvenida_nativa', 'prender')], 'prender la bienvenida nativa');
  if ((await control(welcome)).state !== 'prendido') fail('la bienvenida nativa no quedo prendida');
  const withWelcome = await readSilences();
  if (empty.contract !== 'flow-engine-v2' || !same(empty.flows, []) || withWelcome.contract !== 'flow-engine-v2'
      || !same(withWelcome.flows, [])) {
    fail('sin silencio la lectura no da vacia', { empty, withWelcome });
  }
  results.sin_silencio = { flujos: 0, con_la_bienvenida_prendida: 0 };
}

// ---------------------------------------------------------------------------
// 2. En cohorte, con su version, su sha256, la definicion y las conversaciones de la cohorte.
// ---------------------------------------------------------------------------
const FOLLOWUP = {
  esquema: 'setter-flujo/v1', flujo: 'seguimiento_cupon', producto: A.funnel, estado: 'borrador',
  nombre: 'Seguimiento con cupón',
  dispara: { silencio: { lo_ultimo: 'agente', desde: '24h', hasta: '72h', conversaciones: ['abierta', 'resuelta'] } },
  entra: { permiso: 'conversacion', contesto_a: 'cualquier_plantilla' },
  pasos: [{ plantilla: TEMPLATE.name, idioma: TEMPLATE.language, variables: ['nombre', 'producto'],
    boton: { link: 'checkout', cupon: COUPON } }],
  termina: { si: ['compra', 'contesta'] },
  horario: { zona: 'America/Mexico_City', desde: '09:00', hasta: '20:59' },
  topes: { bolsa: 'att1-seguimiento-cupon', por_dia: 'sin_tope', total: 'sin_tope',
    repeticion: { una_por: 'conversacion', otra_despues_de: 'nunca' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
const flowId = await openNative('seguimiento_cupon', FOLLOWUP);
const addToCohort = async (lead) => sql(
  `insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
  [flowId, lead.conversationContact]);
const theSilence = (payload) => one(payload.flows, 'el silencio leido');
const uno = await adopted('barrido-uno');
const zeta = await adopted('barrido-z', { withLastTouch: true });
const afuera = await adopted('barrido-afuera');
{
  const stored = one(await sql(`select definition, definition_sha256 from public.flow_definitions
    where flow_id = $1 and version = 1`, [flowId]), 'la version guardada');
  const noMembers = theSilence(await readSilences());
  if (noMembers.flow_id !== flowId || noMembers.product_ref !== A.funnel || noMembers.flow_key !== 'seguimiento_cupon'
      || noMembers.state !== 'cohorte' || noMembers.active_version !== 1
      || noMembers.definition_sha256 !== stored.definition_sha256 || !same(noMembers.definition, stored.definition)
      || !same(noMembers.cohort_conversation_ids, [])) {
    fail('el silencio en cohorte sin miembros', noMembers);
  }
  await addToCohort(uno);
  await addToCohort(zeta);
  // El de afuera es miembro de la cohorte de otro flujo (la bienvenida): no es de esta.
  await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
    [welcomeFlow, afuera.conversationContact]);
  const withMembers = theSilence(await readSilences());
  const expected = [Number(uno.chatwoot), Number(zeta.chatwoot)].sort((a, b) => a - b);
  if (!same([...withMembers.cohort_conversation_ids].sort((a, b) => a - b), expected)
      || withMembers.cohort_conversation_ids.includes(Number(afuera.chatwoot))) {
    fail('las conversaciones de la cohorte', { got: withMembers.cohort_conversation_ids, expected });
  }
  results.cohorte = { sin_miembros: noMembers.cohort_conversation_ids.length,
    con_dos_miembros: withMembers.cohort_conversation_ids.length, el_de_afuera_no: true };
}

// ---------------------------------------------------------------------------
// 3. Lo que lee es lo que pide la reserva, y Z frena.
// ---------------------------------------------------------------------------
{
  const flow = theSilence(await readSilences());
  const claimed = await rolledBack(() => behaviour(() => claimWith(flow, uno)));
  if (claimed.outcome !== 'claimed' || claimed.flowOf?.flow_id !== flowId || claimed.flowOf.flow_version !== 1
      || claimed.flowOf.definition_sha256 !== flow.definition_sha256 || claimed.delta.authorizations !== 1) {
    fail('la reserva con lo leido', claimed);
  }
  const afterZ = await rolledBack(() => behaviour(() => claimWith(flow, zeta)));
  if (afterZ.outcome !== 'blocked_after_last_touch' || !same(afterZ.delta, NO_DELTA)) {
    fail('Z en la reserva con lo leido', afterZ);
  }
  const outside = await rolledBack(() => behaviour(() => claimWith(flow, afuera)));
  if (outside.outcome !== 'blocked_out_of_cohort' || !same(outside.delta, NO_DELTA)) {
    fail('el de afuera de la cohorte', outside);
  }
  results.reserva = { miembro: claimed.outcome, miembro_con_z: afterZ.outcome, de_afuera: outside.outcome,
    bolsa_sin_gastar_con_z: afterZ.delta.authorizations };
}

// ---------------------------------------------------------------------------
// 4. Pausado no aparece; prendido, con las conversaciones en null.
// ---------------------------------------------------------------------------
{
  const paused = await rolledBack(async () => {
    await pauseFlow(flowId, A.funnel, 'seguimiento_cupon');
    return { state: (await control(flowId)).state, read: await readSilences() };
  });
  if (paused.state !== 'pausado' || paused.read.flows.length !== 0) fail('pausado', paused);
  const on = await rolledBack(async () => {
    await cycle([stateItem(flowId, A.funnel, 'seguimiento_cupon', 'prender')], 'prender el seguimiento');
    return { state: (await control(flowId)).state, flow: theSilence(await readSilences()) };
  });
  if (on.state !== 'prendido' || on.flow.state !== 'prendido' || on.flow.cohort_conversation_ids !== null) {
    fail('prendido', on);
  }
  // Con una ruta del legado (no nativa) no es del barrido nativo.
  const legacyRoute = await rolledBack(async () => {
    // Una ruta nativa no se mueve (flow_route_native_cannot_move): el trigger se apaga solo adentro de esta
    // transaccion que se deshace, para ver que la lectura mira la ruta y no solo el control.
    await db.exec("set local session_replication_role = replica");
    await sql(`update public.flow_engine_routes set legacy_kind = 'descuento' where flow_id = $1`, [flowId]);
    return (await readSilences()).flows.length;
  });
  if (legacyRoute !== 0) fail('con la ruta del legado aparece', legacyRoute);
  results.estados = { pausado: 0, prendido: 'todas las conversaciones (null)', ruta_del_legado: 0 };
}

// ---------------------------------------------------------------------------
// 5. Una version nueva.
// ---------------------------------------------------------------------------
{
  const v2 = await rolledBack(async () => {
    await cycle([versionItem(flowId, A.funnel, 'seguimiento_cupon', { ...FOLLOWUP, topes: { ...FOLLOWUP.topes, por_dia: 7 } })],
      'v2 del seguimiento');
    const stored = one(await sql(`select definition, definition_sha256 from public.flow_definitions
      where flow_id = $1 and version = 2`, [flowId]), 'la v2');
    return { stored, control: await control(flowId), flow: theSilence(await readSilences()) };
  });
  if (v2.control.active_version !== 2 || v2.flow.active_version !== 2
      || v2.flow.definition_sha256 !== v2.stored.definition_sha256 || v2.flow.definition.topes.por_dia !== 7) {
    fail('la version nueva', { control: v2.control, flow: v2.flow });
  }
  results.version_nueva = { version: v2.flow.active_version, por_dia: v2.flow.definition.topes.por_dia };
}

// ---------------------------------------------------------------------------
// 6. Otra instancia no se lee.
// ---------------------------------------------------------------------------
{
  let caught = null;
  try {
    await readSilences({ tenant_ref: INSTANCE.tenant_ref, ally_ref: 'otra-aliada' });
  } catch (error) {
    caught = error;
  }
  if (caught?.code !== '22023' || caught.message !== 'flow_store_instance_not_bound') {
    fail('otra instancia', { code: caught?.code, message: caught?.message });
  }
  results.otra_instancia = caught.message;
}

// ---------------------------------------------------------------------------
// 7. Si el disparador pierde el silencio, la lectura falla fuerte.
// ---------------------------------------------------------------------------
{
  const wiped = await rolledBack(async () => {
    const trigger = one(await sql(`select pg_get_functiondef('public.${TRIGGER}'::regprocedure) as d`), 'disparador').d;
    await db.exec(trigger.replaceAll('flow_engine_native_silence_trigger', 'otro_disparador'));
    try {
      await readSilences();
      return null;
    } catch (error) {
      return { code: error.code, message: error.message };
    }
  });
  if (wiped?.code !== '55000' || wiped.message !== 'flow_engine_native_silence_trigger_missing') {
    fail('el disparador sin el silencio', wiped);
  }
  results.disparador_sin_silencio = wiped.message;
}

// ---------------------------------------------------------------------------
// 8. Solo service_role la ejecuta.
// ---------------------------------------------------------------------------
{
  const denied = {};
  for (const role of ['anon', 'authenticated']) {
    await db.exec(`set role ${role}`);
    let caught = null;
    try {
      await sql('select public.read_flow_engine_native_silences_v1($1::jsonb)', [JSON.stringify(INSTANCE)]);
    } catch (error) {
      caught = error;
    } finally {
      await db.exec('reset role');
    }
    if (caught?.code !== '42501') fail(`${role} ejecuto la lectura`, caught?.message);
    denied[role] = caught.code;
  }
  results.acl = denied;
}

console.log(JSON.stringify({ flow_engine_native_silence_sweep: 'OK', ...results }));
await db.close();
