// El seguimiento a 24 h con cupon nativo, en la base (migracion 20261101000200,
// E-251, P22-2 de M16; diseno: el seguimiento y la ficha nativos, 1.2 (b)).
//
// Por que: el seguimiento con cupon de ATT1 y de Maribel corre por el legado
// (el barrido de followup_discount.py y claim_portable_conversation_followup_v1),
// y el Automation Expert solo crea flujos nativos. P22-1 deja escribirlo como un
// silencio en que lo ultimo lo dijo el agente; esta migracion le da a la base
// el disparador, lo que ejecuta, la reserva nativa, la barrera del legado, la
// simulacion y las ventas.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera). Cada conversacion adoptada sale por los caminos de hoy: el
// formulario con consentimiento, el carrito capturado
// (tests/fixtures/hotmart_cart_abandonment_rejected_v1.json), la plantilla del
// carrito aceptada por Chatwoot y la respuesta por la admision portable, que
// deja el evento de adopcion. La plantilla del seguimiento y su idioma salen
// del catalogo capturado de la bandeja 11 de ATT1
// (tests/fixtures/chatwoot_inbox_11_message_templates_20261010.json); el cupon
// es TIROIDES10 (la decision de Dan: el catalogo lo trae redactado). La compra
// es la capturada de la bienvenida de ATT1
// (tests/fixtures/hotmart_purchase_approved_att1_bienvenida_20261026.json), con
// la persona, la transaccion y el sck de nuestro link sustituidos. Las
// definiciones del contraste de lo que ejecuta el motor son todas las
// setter-flujo/v1 de tests/fixtures. Los telefonos son sinteticos.
//
// Prueba:
//   0. la forma: cambian exactamente las cinco funciones del encargo y nace la
//      reserva nativa (security definer, solo service_role, el search_path de
//      la portable); conversation_followup_events suma las tres columnas con
//      su check y su indice; correrla dos veces aborta sin cambiar nada;
//   1. lo que ejecuta: el seguimiento de Maribel (los valores de hoy) se
//      ejecuta, y cada cosa que el barrido no hace se frena; para toda
//      definicion que no es el silencio del agente, el disparador y lo que no
//      ejecuta dan lo mismo que con las definiciones de antes;
//   2. sin un silencio nativo, la portable da LOS MISMOS RESULTADOS que la de
//      antes sobre los mismos datos (comportamiento, no md5): la conversacion
//      adoptada, la organica, la compra, la baja, el replay y el limite; el
//      resultado, el link, la fila y lo que escribe;
//   3. el silencio nativo abre en cohorte, con su bolsa y su ruta;
//   4. la cohorte: fuera de ella la reserva nativa da blocked_out_of_cohort y
//      el legado manda como hoy; adentro, el legado da blocked_native_flow sin
//      escribir nada y la nativa reserva con flow_id, version y sha256, gasta
//      una autorizacion de la bolsa y emite nuestro link;
//   5. el doble envio contra el indice: con una corrida de un camino, el otro
//      no reserva ni con el mismo command_key (blocked_followup_limit, no
//      replayed) ni con otro; el replay es solo del mismo camino;
//   6. un rechazo no deja filas ni gasta la bolsa: otra version, otro sha256,
//      otra plantilla, otro cupon, otro inbox, un flujo que no existe, la
//      bolsa en su tope (y con uno menos, reserva);
//   7. la compra y la baja frenan a la nativa;
//   8. pausado: la nativa no corre y el legado vuelve a mandar; prendido: el
//      legado no le manda a nadie de la instancia;
//   9. la venta se lee por flow_id: la compra con el link de la corrida
//      (purchase_matched) cuenta para el silencio (by_link) y los demas flujos
//      dan lo mismo que antes;
//  10. la simulacion cuenta los seguimientos que salieron, y las de los eventos
//      dan lo mismo que antes;
//  11. solo service_role ejecuta la reserva nativa.
import { PGlite } from '@electric-sql/pglite';
import { randomBytes } from 'node:crypto';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261101000200_flow_engine_native_silence_v1.sql';
const ARGS = 'bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamp with time zone';
const PORTABLE = `claim_portable_conversation_followup_v1(${ARGS})`;
const NATIVE = `claim_flow_engine_silence_v1(uuid,integer,text,${ARGS})`;
const TRIGGER = '_flow_engine_native_trigger(jsonb)';
const UNSUPPORTED = '_flow_engine_native_unsupported(jsonb)';
const COUNTS = 'read_flow_engine_native_trigger_counts_v1(jsonb,text,text,timestamp with time zone)';
const SALES = 'read_flow_sales_v1(jsonb,timestamp with time zone)';
const CHANGED = [TRIGGER, UNSUPPORTED, PORTABLE, COUNTS, SALES];
const ADDED = [NATIVE];
const results = {};
const fixture = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));

const names = readdirSync(join(root, 'supabase/migrations')).filter((name) => name.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
if (names.slice(position + 1).length !== 0) fail(`${MIGRATION} es la ultima de la cadena`, names.slice(position + 1));

// La plantilla del seguimiento de ATT1, del catalogo capturado.
const CATALOG = fixture('chatwoot_inbox_11_message_templates_20261010.json');
const template = one((CATALOG.message_templates ?? []).filter((item) => item.name === 'att1_seguimiento_descuento_01'),
  'la plantilla del seguimiento');
const urlButtons = (template.components ?? []).filter((component) => component.type === 'BUTTONS')
  .flatMap((component) => component.buttons ?? []);
if (template.status !== 'APPROVED' || template.category !== 'MARKETING' || urlButtons.length !== 1
    || urlButtons[0].type !== 'URL' || urlButtons[0].url !== 'https://pay.hotmart.com/{{1}}') {
  fail('el catalogo no trae el seguimiento de ATT1 con un solo boton al checkout', { status: template.status, urlButtons });
}
const TEMPLATE = { name: template.name, language: template.language };
const COUPON = 'TIROIDES10';
// El primer barrido de ATT1 (cada 600 s) despues de las 24 h de silencio.
const AGE = 86_400 + 600;
const CAPTURED_PURCHASE = fixture('hotmart_purchase_approved_att1_bienvenida_20261026.json').payload;

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile, p.proowner, p.proconfig,
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
  // El freno del disparador: con una forma que no es la vigente (otra migracion lo reemplazo entero),
  // la migracion aborta con 55000 en vez de parchear a ciegas, y no cambia nada.
  {
    const inside = readFileSync(join(root, 'supabase/migrations', MIGRATION), 'utf8')
      .replace('\nbegin;\n', '\n-- begin: la del validador\n').replace(/\ncommit;\s*$/, '\n');
    const trigger = before.get(TRIGGER).definition;
    const altered = trigger.replace(/\n\s*when p_definition -> 'dispara' = '\{"evento": "pago_fallido"\}'::jsonb then 'pago_fallido'/, '');
    if (altered === trigger) fail('el freno: no se pudo armar el disparador cambiado');
    await db.exec('begin');
    let caught = null;
    try {
      await db.exec(altered);
      await db.exec(inside);
    } catch (error) {
      caught = error;
    }
    await db.exec('rollback');
    if (caught?.code !== '55000' || caught.message !== 'unexpected_flow_engine_native_trigger_definition') {
      fail('el freno del disparador cambiado', { code: caught?.code, message: caught?.message });
    }
    const untouched = await snapshot(db);
    if ([...before.keys()].some((sig) => untouched.get(sig)?.definition !== before.get(sig).definition)) {
      fail('el freno dejo algo cambiado');
    }
    results.freno_del_disparador = caught.message;
  }
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
  const native = after.get(NATIVE);
  const portable = after.get(PORTABLE);
  if (!native.prosecdef || !native.service_x || native.api_x
      || JSON.stringify(native.proconfig) !== JSON.stringify(portable.proconfig)) {
    fail('la reserva nativa no es un definer de solo service_role como la portable',
      { prosecdef: native.prosecdef, service_x: native.service_x, api_x: native.api_x, config: native.proconfig });
  }
  for (const signature of CHANGED) {
    if (!after.get(signature).definition.includes('flow_engine_native_silence')) fail(`${signature}: sin la marca`);
    previous[signature] = before.get(signature).definition;
  }
  // La nativa sale de la portable de antes: sacando sus bloques, queda la portable byte a byte salvo el
  // nombre y los argumentos del flujo, y la fila con su flujo y su autorizacion.
  const columns = (await db.query(`
    select a.attname, format_type(a.atttypid, a.atttypmod) as type, a.attnotnull
      from pg_attribute a where a.attrelid = 'public.conversation_followup_events'::regclass
       and a.attname in ('flow_id', 'flow_version', 'definition_sha256') and not a.attisdropped
     order by a.attnum`)).rows;
  const check = (await db.query(`select pg_get_constraintdef(oid) as d from pg_constraint
    where conrelid = 'public.conversation_followup_events'::regclass
      and conname = 'conversation_followup_events_native_run'`)).rows[0]?.d ?? '';
  const index = (await db.query(`select indexdef from pg_indexes
    where tablename = 'conversation_followup_events' and indexname = 'conversation_followup_events_flow_idx'`)).rows[0]?.indexdef ?? '';
  if (JSON.stringify(columns) !== JSON.stringify([
    { attname: 'flow_id', type: 'uuid', attnotnull: false },
    { attname: 'flow_version', type: 'integer', attnotnull: false },
    { attname: 'definition_sha256', type: 'text', attnotnull: false },
  ]) || !check.includes('(flow_id IS NULL) AND (flow_version IS NULL) AND (definition_sha256 IS NULL)')
      || !check.includes('flow_version > 0') || !check.includes("definition_sha256 ~ '^[0-9a-f]{64}$'::text")
      || !index.includes('WHERE (flow_id IS NOT NULL)')) {
    fail('las columnas de la corrida', { columns, check, index });
  }
  let error = null;
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  } catch (caught) {
    error = caught;
  }
  if (error?.message !== 'flow_engine_native_silence_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  await db.exec('rollback').catch(() => {});
  const again = await snapshot(db);
  if ([...after.keys()].some((s) => !again.has(s) || again.get(s).definition !== after.get(s).definition)
      || again.size !== after.size) {
    fail('la segunda corrida cambio algo');
  }
  results.forma = { cambiadas: changed.length, nuevas: added.length, segunda_corrida: error.message };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, stateItem, dbNow, person, admitForm, cartFor, planCart,
  reserve, startOld, accept, rpc, control, pauseFlow } = world;
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
const withPrevious = async (signatures, run) => rolledBack(async () => {
  for (const signature of signatures) await db.exec(previous[signature]);
  return run();
});
// Lo mismo adentro de una transaccion ya abierta (un savepoint que se deshace).
let savepointSerial = 0;
const withPreviousInside = async (signatures, run) => {
  savepointSerial += 1;
  const name = `antes_${savepointSerial}`;
  await db.exec(`savepoint ${name}`);
  try {
    for (const signature of signatures) await db.exec(previous[signature]);
    return await run();
  } finally {
    await db.exec(`rollback to savepoint ${name}`);
  }
};
// Abrir un flujo nativo como lo abre la publicacion: registrar, publicar la v1 y abrirlo en cohorte.
const openNative = async (flowKey, definition) => {
  const flowId = await register(A.funnel, flowKey);
  await cycle([versionItem(flowId, A.funnel, flowKey, { ...definition, producto: A.funnel })], `v1 de ${flowKey}`);
  const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'propuesta').proposal_id;
  const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [flowId, proposalId, A.timezone]), 'abrir').r);
  if (!opened.ok || opened.state !== 'cohorte') fail(`abrir ${flowKey}`, opened);
  return { flowId, opened };
};

// ULID como el del adaptador: 48 bits de milisegundos y 80 aleatorios.
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

// Una conversacion adoptada: el carrito con el formulario, la plantilla del carrito aceptada por Chatwoot
// en la conversacion de la persona y su respuesta por la admision portable (el evento de adopcion).
let messageSerial = 0;
const adopted = async (label) => {
  const lead = await cartFor(label);
  const plan = await planCart(lead);
  if (!plan.scheduled_action_id) fail(`${label}: el carrito no planifico`, plan);
  const reserved = await reserve(lead, plan.scheduled_action_id);
  const started = await startOld(reserved);
  if (started.phase !== 'request_started') fail(`${label}: la plantilla no arranco`, started);
  await accept(lead, reserved);
  return replied(lead);
};
const replied = async (lead) => {
  const reply = one(await asService(() => sql('select * from public.admit_portable_inbound_commercial_case_v1($1,$2,$3,$4)',
    [A.inboundScope, A.inboundVersion, Number(lead.chatwoot), lead.phone])), `${lead.label} respuesta`);
  const conversation = one(await sql(`select id, contact_id from public.conversations
    where commercial_context ->> 'chatwoot_conversation_id' = $1`, [lead.chatwoot]), `${lead.label} conversacion`);
  lead.adoptions = one(await sql(`select count(*)::integer as n from public.conversation_events
    where conversation_id = $1 and event_type = 'inbound_adopted_template_conversation'`, [conversation.id]), 'adopcion').n;
  lead.conversation = conversation.id;
  lead.conversationContact = conversation.contact_id;
  lead.caseId = reply.commercial_case_id;
  messageSerial += 1;
  lead.inbound = 770_000 + messageSerial * 10;
  lead.outbound = lead.inbound + 1;
  return lead;
};
const expectAdopted = (lead) => {
  if (lead.adoptions !== 1) fail(`${lead.label}: la conversacion no quedo adoptada`, lead.adoptions);
  return lead;
};

// La reserva del barrido, como service_role, con los argumentos de _follow_up.
const marks = (from, count) => Array.from({ length: count }, (_, i) => `$${from + i}`).join(',');
const claimArgs = (lead, over = {}) => {
  const outbound = over.outbound ?? lead.outbound;
  return [Number(lead.chatwoot), over.accountId ?? A.accountId, over.inboxId ?? A.inboxId, over.userId ?? lead.phone, lead.email,
    over.key ?? `followup:${lead.chatwoot}:${outbound}`, 'link_sent_no_purchase', over.template ?? TEMPLATE.name,
    over.language ?? TEMPLATE.language, over.coupon ?? COUPON, lead.inbound, outbound, AGE, over.ulid ?? ulid(), null];
};
const claimLegacy = async (lead, over = {}) => one(await asService(() => sql(
  `select * from public.claim_portable_conversation_followup_v1(${marks(1, 15)})`, claimArgs(lead, over))),
`${lead.label} reserva del legado`);
let FLOW = null;
const claimNative = async (lead, over = {}) => one(await asService(() => sql(
  `select * from public.claim_flow_engine_silence_v1($1,$2,$3,${marks(4, 15)})`,
  [over.flowId ?? FLOW.flowId, over.version ?? FLOW.version, over.sha ?? FLOW.sha, ...claimArgs(lead, over)])),
`${lead.label} reserva nativa`);

// Lo que puede escribir una reserva, en toda la base.
const footprint = async () => one(await sql(`
  select (select count(*)::integer from public.conversation_followup_events) as followups,
         (select count(*)::integer from public.conversation_followup_events where flow_id is not null) as native_followups,
         (select count(*)::integer from public.checkout_link_issuances) as issuances,
         (select count(*)::integer from public.flow_engine_request_authorizations) as authorizations,
         (select count(*)::integer from public.purchase_intents where lifecycle_state = 'waiting_for_purchase') as live_intents
`), 'huella');
const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);
const eventOf = async (id) => one(await sql(`
  select status, regime, template_name, template_language, coupon_code, inbound_age_seconds,
         last_inbound_message_id::text as inbound, last_outbound_message_id::text as outbound, command_key,
         conversation_id, commercial_case_id, flow_id, flow_version, definition_sha256,
         (select row(i.status, i.source_kind, i.trigger_external_message_id, i.checkout_url_final, i.sck_value)::text
            from public.checkout_link_issuances i where i.id = checkout_issuance_id) as issuance
    from public.conversation_followup_events where id = $1`, [id]), 'la fila del seguimiento');
// El resultado de una reserva sin ids: el resultado, el link, la fila y la huella.
const behaviour = async (run) => {
  const start = await footprint();
  const row = await run();
  const event = row.followup_event_id ? await eventOf(row.followup_event_id) : null;
  return { outcome: row.outcome, url: row.checkout_url_final, sck: row.sck_value, event,
    delta: Object.fromEntries(Object.entries(await footprint()).map(([k, v]) => [k, v - start[k]])) };
};
const NO_DELTA = { followups: 0, native_followups: 0, issuances: 0, authorizations: 0, live_intents: 0 };

// ---------------------------------------------------------------------------
// 1. Lo que ejecuta el motor nativo.
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
const unsupportedOf = async (definition) => one(await sql(
  'select public._flow_engine_native_unsupported($1::jsonb) as r, public._flow_engine_native_trigger($1::jsonb) as t',
  [JSON.stringify(definition)]), 'lo que no ejecuta');
{
  const ok = await unsupportedOf(FOLLOWUP);
  if (ok.r !== null || ok.t !== 'silencio_agente') fail('el seguimiento de Maribel no se ejecuta', ok);
  // E-254: la 0200 parcha el disparador de la 0100 (P20a, intencion) por reemplazo contado, no una copia
  // vieja: despues de las dos, los cuatro eventos y el silencio siguen saliendo.
  const triggerEvents = {};
  for (const evento of ['compra', 'carrito', 'pago_fallido', 'intencion']) {
    triggerEvents[evento] = one(await sql('select public._flow_engine_native_trigger($1::jsonb) as t',
      [JSON.stringify({ dispara: { evento } })]), 'el disparador').t;
  }
  if (JSON.stringify(triggerEvents) !== JSON.stringify({ compra: 'compra', carrito: 'carrito', pago_fallido: 'pago_fallido',
    intencion: 'intencion' })) fail('la 0200 perdio un evento del disparador de la 0100', triggerEvents);
  const triggerDefinition = one(await sql(
    "select pg_get_functiondef('public._flow_engine_native_trigger(jsonb)'::regprocedure) as d"), 'la definicion').d;
  if (!triggerDefinition.includes('flow_engine_native_silence_trigger')
      || !triggerDefinition.includes(`'{"evento": "intencion"}'::jsonb then 'intencion'`)) {
    fail('el disparador no es el de la 0100 con el silencio');
  }
  const step = FOLLOWUP.pasos[0];
  const variants = {
    'silencio de la persona': [{ ...FOLLOWUP, dispara: { silencio: { ...FOLLOWUP.dispara.silencio, lo_ultimo: 'persona' } } }, 'motor_nativo:disparador'],
    'silencio y evento': [{ ...FOLLOWUP, dispara: { ...FOLLOWUP.dispara, evento: 'carrito' } }, 'motor_nativo:disparador'],
    'permiso formulario': [{ ...FOLLOWUP, entra: { ...FOLLOWUP.entra, permiso: 'formulario' } }, 'motor_nativo:permiso'],
    'contesto_a lista': [{ ...FOLLOWUP, entra: { ...FOLLOWUP.entra, contesto_a: [] } }, 'motor_nativo:permiso'],
    'una por persona': [{ ...FOLLOWUP, topes: { ...FOLLOWUP.topes, repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } } }, 'motor_nativo:repeticion'],
    'otra a las 24 h': [{ ...FOLLOWUP, topes: { ...FOLLOWUP.topes, repeticion: { una_por: 'conversacion', otra_despues_de: '24h' } } }, 'motor_nativo:repeticion'],
    'con espera': [{ ...FOLLOWUP, pasos: [{ esperar: '1h' }, step] }, 'motor_nativo:forma'],
    'dos envios': [{ ...FOLLOWUP, pasos: [step, step] }, 'motor_nativo:forma'],
    'cupon con +': [{ ...FOLLOWUP, pasos: [{ ...step, boton: { link: 'checkout', cupon: 'PROTOCOLO10+' } }] }, 'motor_nativo:forma'],
    'cupon personal': [{ ...FOLLOWUP, pasos: [{ ...step, boton: { link: 'checkout', cupon: 'personal' } }] }, 'motor_nativo:forma'],
    'sin boton': [{ ...FOLLOWUP, pasos: [{ ...step, boton: undefined }] }, 'motor_nativo:forma'],
    'otras variables': [{ ...FOLLOWUP, pasos: [{ ...step, variables: ['nombre'] }] }, 'motor_nativo:forma'],
    'sin contesta': [{ ...FOLLOWUP, termina: { si: ['compra'] } }, 'motor_nativo:forma'],
    etiquetar: [{ ...FOLLOWUP, pasos: [step, { etiquetar: 'clienta' }] }, 'motor_nativo:forma'],
  };
  const got = {};
  for (const [label, [definition, want]] of Object.entries(variants)) {
    got[label] = (await unsupportedOf(JSON.parse(JSON.stringify(definition)))).r;
    if (got[label] !== want) fail(`lo que no ejecuta: ${label}`, { got: got[label], want });
  }
  // Para toda definicion que no es el silencio del agente: lo mismo que antes. Las de tests/fixtures.
  const definitions = [];
  const walk = (node) => {
    if (Array.isArray(node)) node.forEach(walk);
    else if (node && typeof node === 'object') {
      if (node.esquema === 'setter-flujo/v1' && node.dispara !== undefined) definitions.push(node);
      Object.values(node).forEach(walk);
    }
  };
  const visit = (dir) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) visit(path);
      else if (name.endsWith('.json')) {
        try {
          walk(JSON.parse(readFileSync(path, 'utf8')));
        } catch {
          // un json que no se lee no trae definiciones
        }
      }
    }
  };
  visit(join(root, 'tests/fixtures'));
  const events = definitions.filter((d) => !(d.dispara && typeof d.dispara === 'object' && d.dispara.silencio?.lo_ultimo === 'agente'));
  const read = async () => {
    const out = [];
    for (const definition of events) {
      const row = await unsupportedOf(definition);
      out.push([row.t, row.r]);
    }
    return out;
  };
  const now = await rolledBack(read);
  const before = await withPrevious([TRIGGER, UNSUPPORTED], read);
  if (events.length < 30 || !same(now, before)) {
    fail('lo que ejecuta el motor cambio para una definicion que no es el silencio', {
      total: events.length,
      differ: events.map((d, i) => [d.flujo, now[i], before[i]]).filter(([, a, b]) => !same(a, b)),
    });
  }
  // Las ramas de los eventos fuera de lo que traen los fixtures: lo mismo que antes.
  const cart = world.cartDefinition(A.funnel);
  const synthetic = {
    'una por conversacion': { ...cart, topes: { ...cart.topes, repeticion: { una_por: 'conversacion', otra_despues_de: 'nunca' } } },
    'otra_despues_de invalido': { ...cart, topes: { ...cart.topes, repeticion: { una_por: 'persona', otra_despues_de: 'pronto' } } },
    'prioridad que no es lista': { ...cart, prioridad: { le_gana_a: [], no_arranca_mientras: 'pago_rechazado' } },
    'etiquetar con carrito': { ...cart, pasos: [cart.pasos[0], { etiquetar: 'clienta' }] },
    'compra con formulario': { ...cart, dispara: { evento: 'compra' }, entra: { permiso: 'formulario', contesto_a: [] } },
  };
  const readSynthetic = async () => {
    const out = {};
    for (const [label, definition] of Object.entries(synthetic)) out[label] = await unsupportedOf(definition);
    return out;
  };
  const syntheticNow = await rolledBack(readSynthetic);
  const syntheticBefore = await withPrevious([TRIGGER, UNSUPPORTED], readSynthetic);
  if (!same(syntheticNow, syntheticBefore) || Object.values(syntheticNow).some((row) => row.r === null)) {
    fail('las ramas de los eventos cambiaron', { syntheticNow, syntheticBefore });
  }
  // El seguimiento de ATT1 capturado (el legado traducido, contesto_a con la lista de sus flujos): la base no
  // lo abre como nativo; el AE lo escribe con cualquier_plantilla.
  const captured = one(fixture('flow_store/att1_definitions_20261016.json').definitions
    .filter((definition) => definition.flujo === 'descuento'), 'el descuento capturado de ATT1');
  const capturedRow = await unsupportedOf(captured);
  if (capturedRow.t !== 'silencio_agente' || capturedRow.r !== 'motor_nativo:permiso') {
    fail('el descuento capturado de ATT1', capturedRow);
  }
  // Rojo: con las de antes, el seguimiento no se ejecuta.
  const red = await withPrevious([TRIGGER, UNSUPPORTED], () => unsupportedOf(FOLLOWUP));
  if (red.r !== 'motor_nativo:disparador') fail('rojo: con las de antes el silencio no tendria que ejecutarse', red);
  results.lo_que_ejecuta = { maribel: 'se ejecuta', frenos: Object.keys(variants).length,
    ramas_de_eventos_sinteticas_iguales: Object.keys(synthetic).length, descuento_capturado_de_att1: capturedRow.r,
    definiciones_iguales_que_antes: events.length, rojo_de_antes: red.r };
}

// La compra capturada de ATT1, de la persona y (si se da) con el sck de nuestro link.
let purchaseSerial = 0;
async function purchase(lead, { sck = null } = {}) {
  purchaseSerial += 1;
  const now = await dbNow();
  const payload = structuredClone(CAPTURED_PURCHASE);
  payload.id = `silencio-compra-${lead.email}-${purchaseSerial}`;
  payload.creation_date = now.getTime() - 60_000;
  Object.assign(payload.data.buyer, { name: lead.name, first_name: lead.name.split(' ')[0], email: lead.email,
    checkout_phone: lead.phone });
  payload.data.product.id = A.productId;
  payload.data.purchase.offer.code = lead.offer.offer_code;
  payload.data.purchase.transaction = `HP${String(950000000 + purchaseSerial)}`;
  payload.data.purchase.approved_date = now.getTime() - 60_000;
  payload.data.purchase.order_date = now.getTime() - 120_000;
  if (sck) {
    payload.data.purchase.origin = { ...(payload.data.purchase.origin ?? {}), sck };
    const admitted = one(await sql('select * from public.admit_and_correlate_hotmart_checkout_issuance_v2($1,$2::jsonb,$3,clock_timestamp())',
      [payload.id, JSON.stringify(payload), sck]), 'la compra con nuestro link');
    return admitted;
  }
  const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone]), 'la compra');
  if (admitted.outcome !== 'inserted') fail('admitir la compra', admitted);
  return admitted;
}
let optOutSerial = 0;
async function optOut(lead) {
  optOutSerial += 1;
  const result = one(await sql(`select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,$6::timestamptz,'unsubscribe')`,
    [A.accountId, A.inboxId, Number(lead.chatwoot), 990_000 + optOutSerial, lead.phone, (await dbNow()).toISOString()]),
  `${lead.label} baja`);
  if (result.outcome !== 'applied' || result.matched_contact_id === null) fail(`${lead.label} baja`, result);
  return result;
}

// ---------------------------------------------------------------------------
// 2. Sin un silencio nativo, la portable da lo mismo que la de antes.
// ---------------------------------------------------------------------------
const contrast = async (label, lead, over = {}) => {
  const key = over.key ?? `followup:${lead.chatwoot}:${over.outbound ?? lead.outbound}`;
  const fixed = { ...over, key, ulid: over.ulid ?? ulid() };
  const now = await rolledBack(() => behaviour(() => claimLegacy(lead, fixed)));
  const before = await withPrevious([PORTABLE], () => behaviour(() => claimLegacy(lead, fixed)));
  if (!same(now, before)) fail(`sin nativos, ${label}: la portable cambio`, { now, before });
  return now;
};
// Un flujo nativo de evento (la compra) prendido en la instancia: la barrera del legado no lo confunde con un
// silencio.
const COMPRA = {
  esquema: 'setter-flujo/v1', flujo: 'bienvenida_nativa', producto: A.funnel, estado: 'borrador', nombre: 'Bienvenida',
  dispara: { evento: 'compra' }, entra: { permiso: 'compra', contesto_a: [] },
  pasos: [{ plantilla: 'att1_bienvenida_01', idioma: 'es_MX', variables: ['nombre', 'producto'] }],
  termina: { si: [] }, horario: 'siempre',
  topes: { bolsa: 'att1-bienvenida-nativa', por_dia: 'sin_tope', total: 'sin_tope',
    repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
const compraNativa = await openNative('bienvenida_nativa', COMPRA);
await cycle([stateItem(compraNativa.flowId, A.funnel, 'bienvenida_nativa', 'prender')], 'prender la bienvenida nativa');
if ((await control(compraNativa.flowId)).state !== 'prendido') fail('la bienvenida nativa no quedo prendida');
{
  const sinNativos = {};
  const conversa = expectAdopted(await adopted('sin-nativos'));
  const claimed = await contrast('la conversacion adoptada', conversa);
  if (claimed.outcome !== 'claimed' || claimed.event.flow_id !== null || claimed.delta.followups !== 1
      || claimed.delta.issuances !== 1 || claimed.delta.authorizations !== 0) {
    fail('sin nativos: la adoptada', claimed);
  }
  sinNativos.adoptada = claimed.outcome;

  // La organica: el formulario y la persona que escribe sin plantilla.
  const organica = person('organica');
  await admitForm(organica, new Date((await dbNow()).getTime() - 50 * 60_000));
  await replied(organica);
  if (organica.adoptions !== 0) fail('la organica quedo adoptada');
  sinNativos.organica = (await contrast('la organica', organica)).outcome;

  // La compra aprobada de la persona.
  const compro = expectAdopted(await adopted('compro'));
  await purchase(compro);
  sinNativos.compra = (await contrast('la compra', compro)).outcome;

  // La baja por Chatwoot.
  const baja = expectAdopted(await adopted('baja'));
  await optOut(baja);
  sinNativos.baja = (await contrast('la baja', baja)).outcome;

  // El replay y el limite, despues de una reserva de verdad.
  const replay = expectAdopted(await adopted('replay'));
  const first = await claimLegacy(replay, { ulid: ulid() });
  if (first.outcome !== 'claimed') fail('la reserva del replay', first);
  sinNativos.replay = (await contrast('el replay', replay)).outcome;
  sinNativos.limite = (await contrast('el limite', replay, { outbound: replay.outbound + 5 })).outcome;
  const want = { adoptada: 'claimed', organica: 'blocked_not_template_reply', compra: 'purchase_already_approved',
    baja: 'blocked_conversation', replay: 'replayed', limite: 'blocked_followup_limit' };
  if (!same(sinNativos, want)) fail('sin nativos: los resultados', { sinNativos, want });
  results.sin_nativos_igual_que_antes = { ...sinNativos, con_un_nativo_de_evento_prendido: 'bienvenida_nativa' };
}

// ---------------------------------------------------------------------------
// 3. El silencio nativo abre en cohorte.
// ---------------------------------------------------------------------------
const TEST_FOLLOWUP = { ...FOLLOWUP, horario: 'siempre', topes: { ...FOLLOWUP.topes, por_dia: 5 } };
{
  const flowId = await register(A.funnel, 'seguimiento_cupon');
  await cycle([versionItem(flowId, A.funnel, 'seguimiento_cupon', TEST_FOLLOWUP)], 'v1 del seguimiento');
  const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'propuesta').proposal_id;
  const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [flowId, proposalId, A.timezone]), 'abrir').r);
  if (!opened.ok || opened.state !== 'cohorte' || opened.pool !== 'att1-seguimiento-cupon') fail('abrir el seguimiento', opened);
  const sha = one(await sql('select definition_sha256 from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'sha').definition_sha256;
  FLOW = { flowId, version: 1, sha, pool: opened.pool };
  // Rojo: con las de antes, la base no lo abria.
  const red = await withPrevious([TRIGGER, UNSUPPORTED], async () => one(await sql(
    'select public._flow_engine_native_unsupported(definition) as r from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'rojo').r);
  if (red !== 'motor_nativo:disparador') fail('rojo: con las de antes se abria', red);
  results.abre = { estado: opened.state, bolsa: opened.pool, rojo_de_antes: red };
}
const addToCohort = async (lead) => sql(
  `insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
  [FLOW.flowId, lead.conversationContact]);

// ---------------------------------------------------------------------------
// 4. La cohorte.
// ---------------------------------------------------------------------------
const uno = expectAdopted(await adopted('cohorte-1'));
let nativeClaim = null;
{
  const outside = await behaviour(() => claimNative(uno));
  const legacyOutside = await rolledBack(() => behaviour(() => claimLegacy(uno)));
  if (outside.outcome !== 'blocked_out_of_cohort' || !same(outside.delta, NO_DELTA)
      || legacyOutside.outcome !== 'claimed' || legacyOutside.event.flow_id !== null) {
    fail('fuera de la cohorte', { outside, legacyOutside });
  }
  await addToCohort(uno);
  const legacyInside = await behaviour(() => claimLegacy(uno));
  if (legacyInside.outcome !== 'blocked_native_flow' || !same(legacyInside.delta, NO_DELTA)) {
    fail('el legado con la persona en la cohorte', legacyInside);
  }
  // Rojo: con la portable de antes, el legado le mandaba igual.
  const red = await withPrevious([PORTABLE], async () => (await claimLegacy(uno)).outcome);
  if (red !== 'claimed') fail('rojo: con la portable de antes el legado no mandaba', red);

  const key = `followup:${uno.chatwoot}:${uno.outbound}`;
  const ulidValue = ulid();
  const claimed = await behaviour(() => claimNative(uno, { ulid: ulidValue }));
  nativeClaim = one(await sql('select id, checkout_issuance_id from public.conversation_followup_events where command_key = $1',
    [key]), 'la corrida');
  const authorization = one(await sql('select * from public.flow_engine_request_authorizations where attempt_id = $1',
    [nativeClaim.id]), 'la autorizacion');
  if (claimed.outcome !== 'claimed' || claimed.event.flow_id !== FLOW.flowId || claimed.event.flow_version !== 1
      || claimed.event.definition_sha256 !== FLOW.sha || claimed.event.status !== 'claimed'
      || claimed.event.template_name !== TEMPLATE.name || claimed.event.coupon_code !== COUPON
      || claimed.event.conversation_id !== uno.conversation || claimed.event.commercial_case_id !== uno.caseId
      || !claimed.event.issuance.includes('reserved') || !claimed.event.issuance.includes(`sck=hermes~v1~${ulidValue}`)
      || !claimed.sck?.endsWith(`hermes~v1~${ulidValue}`) || !claimed.url?.startsWith(`https://pay.hotmart.com/${A.hotlink}?`)
      || !same(claimed.delta, { followups: 1, native_followups: 1, issuances: 1, authorizations: 1, live_intents: 0 })
      || authorization.flow_id !== FLOW.flowId || authorization.version !== 1 || authorization.pool !== FLOW.pool
      || authorization.action_id !== nativeClaim.id || authorization.contact_id !== uno.conversationContact
      || authorization.legacy_scope_key !== null) {
    fail('la reserva nativa en la cohorte', { claimed, authorization });
  }
  results.cohorte = { fuera: outside.outcome, legado_fuera: legacyOutside.outcome, legado_adentro: legacyInside.outcome,
    rojo_portable_de_antes: red, nativa_adentro: claimed.outcome, autorizacion: authorization.pool };
}

// ---------------------------------------------------------------------------
// 5. El doble envio contra el indice.
// ---------------------------------------------------------------------------
{
  const replay = await behaviour(() => claimNative(uno));
  const legacySameKey = await behaviour(() => claimLegacy(uno));
  const legacyOtherKey = await behaviour(() => claimLegacy(uno, { outbound: uno.outbound + 3 }));
  const nativeOtherKey = await behaviour(() => claimNative(uno, { outbound: uno.outbound + 3 }));
  if (replay.outcome !== 'replayed' || !same(replay.delta, NO_DELTA)
      || legacySameKey.outcome !== 'blocked_followup_limit' || !same(legacySameKey.delta, NO_DELTA)
      || legacyOtherKey.outcome !== 'blocked_followup_limit' || !same(legacyOtherKey.delta, NO_DELTA)
      || nativeOtherKey.outcome !== 'blocked_followup_limit' || !same(nativeOtherKey.delta, NO_DELTA)) {
    fail('despues de la nativa', { replay, legacySameKey, legacyOtherKey, nativeOtherKey });
  }
  // Rojo: con la portable de antes, el legado le devolvia la corrida nativa (replayed) y la mandaba otra vez.
  const red = await withPrevious([PORTABLE], async () => (await claimLegacy(uno)).outcome);
  if (red !== 'replayed') fail('rojo: con la portable de antes el legado no hacia replay de la nativa', red);

  // El legado primero: la persona fuera de la cohorte, reserva el legado; despues entra a la cohorte.
  const dos = expectAdopted(await adopted('legado-primero'));
  const legacy = await claimLegacy(dos);
  if (legacy.outcome !== 'claimed') fail('el legado primero', legacy);
  await addToCohort(dos);
  const sameKey = await behaviour(() => claimNative(dos));
  const otherKey = await behaviour(() => claimNative(dos, { outbound: dos.outbound + 3 }));
  const legacyReplay = await behaviour(() => claimLegacy(dos));
  if (sameKey.outcome !== 'blocked_followup_limit' || !same(sameKey.delta, NO_DELTA)
      || otherKey.outcome !== 'blocked_followup_limit' || !same(otherKey.delta, NO_DELTA)
      || legacyReplay.outcome !== 'replayed') {
    fail('el legado primero, la nativa despues', { sameKey, otherKey, legacyReplay });
  }
  // El indice: una segunda fila viva en la conversacion no entra, venga del camino que venga.
  const live = one(await sql(`select count(*)::integer as n from public.conversation_followup_events
    where conversation_id = any($1::uuid[]) and status <> 'failed'`, [[uno.conversation, dos.conversation]]), 'vivas').n;
  if (live !== 2) fail('una fila viva por conversacion', live);
  results.doble_envio = { nativa_replay: replay.outcome, legado_mismo_key: legacySameKey.outcome,
    legado_otro_key: legacyOtherKey.outcome, nativa_otro_key: nativeOtherKey.outcome, rojo_portable_de_antes: red,
    legado_primero: `${sameKey.outcome} / ${otherKey.outcome}, replay del legado ${legacyReplay.outcome}` };
}

// ---------------------------------------------------------------------------
// 6. Un rechazo no deja filas ni gasta la bolsa.
// ---------------------------------------------------------------------------
const tres = expectAdopted(await adopted('rechazos'));
await addToCohort(tres);
{
  const cases = {
    otra_version: [{ version: 2 }, 'blocked_flow_not_running'],
    otro_sha: [{ sha: 'b'.repeat(64) }, 'blocked_flow_version'],
    otra_plantilla: [{ template: 'att1_carrito_dudas_01' }, 'blocked_flow_version'],
    otro_idioma: [{ language: 'es' }, 'blocked_flow_version'],
    otro_cupon: [{ coupon: 'OTRO10' }, 'blocked_flow_version'],
    otro_inbox: [{ inboxId: A.inboxId + 1000 }, 'blocked_flow_not_running'],
    flujo_que_no_existe: [{ flowId: '00000000-0000-4000-8000-00000000e251' }, 'blocked_flow_not_running'],
  };
  const got = {};
  for (const [label, [over, want]] of Object.entries(cases)) {
    const row = await behaviour(() => claimNative(tres, over));
    got[label] = row.outcome;
    if (row.outcome !== want || !same(row.delta, NO_DELTA)) fail(`rechazo ${label}`, { row, want });
  }
  // La bolsa en su tope (por_dia 5, contado por los dos caminos): con 5 de hoy rechaza sin escribir; con 4, reserva.
  const fill = async (count) => {
    const { timezone } = one(await sql('select timezone from public.flow_engine_pools where pool = $1', [FLOW.pool]), 'zona');
    const today = one(await sql(`select (clock_timestamp() at time zone $1)::date::text as d`, [timezone]), 'hoy').d;
    const used = one(await sql(`select count(*)::integer as n from public.flow_engine_request_authorizations
      where pool = $1 and local_budget_date = $2::date`, [FLOW.pool, today]), 'usadas').n;
    for (let i = used; i < count; i += 1) {
      await sql(`insert into public.flow_engine_request_authorizations (flow_id, version, pool, legacy_scope_key, action_id,
          attempt_id, contact_id, local_budget_date, flow_generation)
        values ($1, 1, $2, null, gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), $3::date, 0)`,
      [FLOW.flowId, FLOW.pool, today]);
    }
  };
  const capped = await rolledBack(async () => {
    await fill(5);
    return behaviour(() => claimNative(tres));
  });
  const underCap = await rolledBack(async () => {
    await fill(4);
    return behaviour(() => claimNative(tres));
  });
  if (capped.outcome !== 'blocked_flow_cap' || !same(capped.delta, NO_DELTA)
      || underCap.outcome !== 'claimed' || underCap.delta.authorizations !== 1) {
    fail('la bolsa', { capped, underCap });
  }
  got.bolsa_en_su_tope = capped.outcome;
  got.bolsa_con_uno_menos = underCap.outcome;
  // Otra version activa: el barrido con la version vieja no reserva; con la nueva, si. Y el tope total de
  // la version nueva (las autorizaciones de la bolsa que ya hay) frena sin escribir nada.
  const versions = await rolledBack(async () => {
    const used = one(await sql('select count(*)::integer as n from public.flow_engine_request_authorizations where pool = $1',
      [FLOW.pool]), 'usadas').n;
    const publish = async (total) => {
      await cycle([versionItem(FLOW.flowId, A.funnel, 'seguimiento_cupon',
        { ...TEST_FOLLOWUP, topes: { ...TEST_FOLLOWUP.topes, total } })], `v nueva con total ${total}`);
      const version = (await control(FLOW.flowId)).active_version;
      const sha = one(await sql('select definition_sha256 from public.flow_definitions where flow_id = $1 and version = $2',
        [FLOW.flowId, version]), 'sha').definition_sha256;
      return { version, sha };
    };
    const v2 = await publish(used);
    const old = await behaviour(() => claimNative(tres));
    const atTotal = await behaviour(() => claimNative(tres, v2));
    const v3 = await publish(used + 1);
    const underTotal = await behaviour(() => claimNative(tres, v3));
    return { v2: v2.version, old, atTotal, v3: v3.version, underTotal };
  });
  if (versions.v2 !== 2 || versions.old.outcome !== 'blocked_flow_version' || !same(versions.old.delta, NO_DELTA)
      || versions.atTotal.outcome !== 'blocked_flow_cap' || !same(versions.atTotal.delta, NO_DELTA)
      || versions.underTotal.outcome !== 'claimed' || versions.underTotal.event.flow_version !== versions.v3) {
    fail('la version activa y el tope total', versions);
  }
  got.version_vieja = versions.old.outcome;
  got.tope_total = versions.atTotal.outcome;
  results.rechazos_sin_filas = got;
}

// ---------------------------------------------------------------------------
// 7. La compra y la baja frenan a la nativa.
// ---------------------------------------------------------------------------
{
  const compro = expectAdopted(await adopted('nativa-compro'));
  await addToCohort(compro);
  await purchase(compro);
  const bought = await behaviour(() => claimNative(compro));
  const baja = expectAdopted(await adopted('nativa-baja'));
  await addToCohort(baja);
  await optOut(baja);
  const optedOut = await behaviour(() => claimNative(baja));
  if (bought.outcome !== 'purchase_already_approved' || !same(bought.delta, NO_DELTA)
      || optedOut.outcome !== 'blocked_conversation' || !same(optedOut.delta, NO_DELTA)) {
    fail('la compra y la baja', { bought, optedOut });
  }
  results.frenos = { compra: bought.outcome, baja: optedOut.outcome };
}

// ---------------------------------------------------------------------------
// 8. Pausado y prendido.
// ---------------------------------------------------------------------------
{
  const paused = await rolledBack(async () => {
    await pauseFlow(FLOW.flowId, A.funnel, 'seguimiento_cupon');
    const native = await behaviour(() => claimNative(tres));
    const legacy = await behaviour(() => claimLegacy(tres));
    return { native, legacy, state: (await control(FLOW.flowId)).state };
  });
  if (paused.state !== 'pausado' || paused.native.outcome !== 'blocked_flow_not_running'
      || !same(paused.native.delta, NO_DELTA) || paused.legacy.outcome !== 'claimed') {
    fail('pausado', paused);
  }
  const fuera = expectAdopted(await adopted('prendido'));
  const on = await rolledBack(async () => {
    await cycle([stateItem(FLOW.flowId, A.funnel, 'seguimiento_cupon', 'prender')], 'prender el seguimiento');
    const legacy = await behaviour(() => claimLegacy(fuera));
    // Otra instancia (otro inbox, sin binding): la barrera no la toca, la portable da lo de antes.
    const otherNow = await behaviour(() => claimLegacy(fuera, { inboxId: A.inboxId + 1000, ulid: '01M6AD7D9VZBHDC7FHDPT2CJFB' }));
    const otherBefore = await withPreviousInside([PORTABLE], () => behaviour(() => claimLegacy(fuera,
      { inboxId: A.inboxId + 1000, ulid: '01M6AD7D9VZBHDC7FHDPT2CJFB' })));
    // Un rechazo de la emision dentro de la nativa (otra identidad): nada escrito, la bolsa sin gastar.
    const issuance = await behaviour(() => claimNative(fuera, { userId: '12025550999' }));
    const native = await behaviour(() => claimNative(fuera));
    return { legacy, otherNow, otherBefore, issuance, native, state: (await control(FLOW.flowId)).state };
  });
  if (on.otherNow.outcome === 'blocked_native_flow' || !same(on.otherNow, on.otherBefore)
      || !on.issuance.outcome.startsWith('issuance_') || !same(on.issuance.delta, NO_DELTA)) {
    fail('prendido: otra instancia y la emision', on);
  }
  if (on.state !== 'prendido' || on.legacy.outcome !== 'blocked_native_flow' || !same(on.legacy.delta, NO_DELTA)
      || on.native.outcome !== 'claimed' || on.native.delta.native_followups !== 1) {
    fail('prendido', on);
  }
  results.estados = { pausado: `nativa ${paused.native.outcome}, legado ${paused.legacy.outcome}`,
    prendido: `legado ${on.legacy.outcome}, nativa ${on.native.outcome} (fuera de la cohorte)`,
    otra_instancia: `${on.otherNow.outcome}, igual que antes`, rechazo_de_la_emision: `${on.issuance.outcome}, nada escrito` };
}

// ---------------------------------------------------------------------------
// 9. La venta se lee por flow_id.
// ---------------------------------------------------------------------------
{
  // La corrida de la cohorte sale: la emision se autoriza y se acepta, y el seguimiento se cierra sent.
  const authorized = one(await asService(() => sql(
    'select * from public.authorize_chatwoot_checkout_issuance_v2($1,$2,$3,$4,$5,$6,clock_timestamp())',
    [nativeClaim.checkout_issuance_id, uno.phone, A.accountId, A.inboxId, Number(uno.chatwoot), String(uno.outbound)])),
  'autorizar');
  const finalized = one(await asService(() => sql(
    "select * from public.finalize_chatwoot_checkout_issuance_v2($1,'accepted_by_chatwoot',$2,null,clock_timestamp())",
    [nativeClaim.checkout_issuance_id, uno.outbound + 1])), 'aceptar');
  const settled = one(await asService(() => sql(
    "select * from public.settle_conversation_followup_v1($1,'sent',$2,null,clock_timestamp())",
    [`followup:${uno.chatwoot}:${uno.outbound}`, uno.outbound + 1])), 'cerrar');
  if (authorized.outcome !== 'request_started' || finalized.outcome !== 'finalized' || settled.outcome !== 'settled') {
    fail('la salida de la corrida', { authorized, finalized, settled });
  }
  const { sck_value: sck } = one(await sql('select sck_value from public.checkout_link_issuances where id = $1',
    [nativeClaim.checkout_issuance_id]), 'el sck');
  const bought = await purchase(uno, { sck });
  const issuance = one(await sql('select status from public.checkout_link_issuances where id = $1',
    [nativeClaim.checkout_issuance_id]), 'la emision').status;
  if (bought.correlation_outcome !== 'matched' || issuance !== 'purchase_matched') {
    fail('la compra con el link de la corrida', { bought, issuance });
  }
  const since = new Date((await dbNow()).getTime() - 2 * 86_400_000).toISOString();
  const read = async () => asService(() => rpc('read_flow_sales_v1', INSTANCE, since));
  const now = await rolledBack(read);
  const before = await withPrevious([SALES], read);
  const silence = now.flows.find((flow) => flow.flow_key === 'seguimiento_cupon');
  const others = (sales) => sales.flows.filter((flow) => flow.flow_key !== 'seguimiento_cupon')
    .map(({ by_link: _ignored, ...rest }) => rest);
  if (!silence || silence.by_link !== 1 || silence.purchases_after_send !== 1 || silence.people_after_send !== 1
      || silence.runs_started < 1 || silence.by_case !== 0 || silence.by_identity !== 0
      || !same(silence.by_step, { seguimiento: 1 })
      || before.flows.some((flow) => flow.flow_key === 'seguimiento_cupon')
      || !same(others(now), others(before))
      || now.flows.some((flow) => flow.flow_key !== 'seguimiento_cupon' && flow.by_link !== 0)) {
    fail('las ventas por flujo', { silence, now: now.flows, before: before.flows });
  }
  // Una sola vez: la misma compra con un recorrido del motor de la persona (el carrito, con su envio
  // aceptado) cuenta por el link y no por identidad; atada a ese caso, cuenta por el caso y no por el link.
  const runOfCart = async () => {
    const flowId = world.FLOWS.carrito;
    const def = one(await sql(`select d.version, d.definition_sha256 from public.flow_definitions d
      join public.flow_controls c on c.flow_id = d.flow_id and c.active_version = d.version where d.flow_id = $1`,
    [flowId]), 'el carrito');
    await sql(`insert into public.flow_engine_runs (recovery_case_id, flow_id, version, definition_sha256, product_ref,
        flow_key, pool, legacy_kind, legacy_scope_key, person_ref, trigger_source_ref, legacy_dispatch_ok)
      values ($1, $2, $3, $4, $5, 'carrito', $6, 'carrito', $6, $7, $8, true)`,
    [uno.plan.recovery_case_id, flowId, def.version, def.definition_sha256, A.funnel, A.scope, uno.contact, uno.event]);
  };
  const byFlow = (sales) => Object.fromEntries(sales.flows.map((flow) => [flow.flow_key,
    { link: flow.by_link, kase: flow.by_case, identity: flow.by_identity }]));
  const once = await rolledBack(async () => {
    await runOfCart();
    const withRun = byFlow(await read());
    await sql("update public.recovery_cases set purchase_event_id = $1, status = 'won', won_at = coalesce(won_at, now()) where id = $2",
      [bought.webhook_event_id, uno.plan.recovery_case_id]);
    const withCase = byFlow(await read());
    return { withRun, withCase };
  });
  if (once.withRun.seguimiento_cupon?.link !== 1 || once.withRun.carrito?.identity !== 0
      || once.withCase.seguimiento_cupon?.link !== 0 || once.withCase.carrito?.kase !== 1) {
    fail('una compra cuenta una sola vez', once);
  }
  results.ventas_una_sola_vez = { con_recorrido_del_carrito: 'por el link', atada_al_caso: 'por el caso' };
  results.ventas = { seguimiento: `by_link ${silence.by_link}, purchases_after_send ${silence.purchases_after_send}, corridas ${silence.runs_started}`,
    los_demas_flujos: 'iguales que antes', antes: 'sin el seguimiento' };
}

// ---------------------------------------------------------------------------
// 10. La simulacion.
// ---------------------------------------------------------------------------
{
  const since = new Date((await dbNow()).getTime() - 7 * 86_400_000).toISOString();
  const counts = await asService(() => rpc('read_flow_engine_native_trigger_counts_v1', INSTANCE, A.funnel, 'silencio', since));
  const want = one(await sql(`select count(*)::integer as events,
      count(distinct i.contact_id)::integer as people
    from public.conversation_followup_events f join public.checkout_link_issuances i on i.id = f.checkout_issuance_id
    where f.status = 'sent' and f.created_at >= $1`, [since]), 'los enviados');
  if (counts.trigger !== 'silencio' || counts.events !== want.events || counts.people !== want.people || want.events < 1) {
    fail('la simulacion del silencio', { counts, want });
  }
  const red = await withPrevious([COUNTS], async () => {
    await db.exec('savepoint antes');
    try {
      await asService(() => rpc('read_flow_engine_native_trigger_counts_v1', INSTANCE, A.funnel, 'silencio', since));
      return 'paso';
    } catch (error) {
      await db.exec('rollback to savepoint antes');
      return error.message;
    }
  });
  const eventsNow = {};
  const eventsBefore = {};
  for (const trigger of ['compra', 'carrito', 'pago_fallido']) {
    const strip = ({ until: _until, ...rest }) => rest;
    eventsNow[trigger] = strip(await rolledBack(() => asService(() => rpc('read_flow_engine_native_trigger_counts_v1',
      INSTANCE, A.funnel, trigger, since))));
    eventsBefore[trigger] = strip(await withPrevious([COUNTS], () => asService(() => rpc('read_flow_engine_native_trigger_counts_v1',
      INSTANCE, A.funnel, trigger, since))));
  }
  if (red !== 'flow_engine_native_trigger_invalid' || !same(eventsNow, eventsBefore)) {
    fail('la simulacion de los eventos', { red, eventsNow, eventsBefore });
  }
  const outside = await rolledBack(async () => {
    await sql("update public.conversation_followup_events set created_at = $1::timestamptz - interval '1 day' where status = 'sent'",
      [since]);
    return asService(() => rpc('read_flow_engine_native_trigger_counts_v1', INSTANCE, A.funnel, 'silencio', since));
  });
  if (outside.events !== 0 || outside.people !== 0) fail('la simulacion conto fuera de la ventana', outside);
  results.simulacion = { fuera_de_la_ventana: outside.events, silencio: `${counts.events} seguimientos, ${counts.people} personas`,
    eventos_iguales_que_antes: Object.keys(eventsNow), rojo_de_antes: red };
}

// ---------------------------------------------------------------------------
// 10b. Si otra migracion reemplaza entero el disparador sin el silencio (cualquier orden de aplicacion),
//      la reserva nativa falla fuerte (55000) en vez de dar blocked_flow_not_running para siempre, y el
//      legado vuelve a mandar (el lado seguro).
// ---------------------------------------------------------------------------
{
  const wiped = await rolledBack(async () => {
    await db.exec(previous[TRIGGER]);
    await db.exec('savepoint borrado');
    let caught = null;
    try {
      await claimNative(tres);
    } catch (error) {
      caught = error;
    }
    await db.exec('rollback to savepoint borrado');
    const legacy = await behaviour(() => claimLegacy(tres));
    return { code: caught?.code, message: caught?.message, legacy: legacy.outcome };
  });
  if (wiped.code !== '55000' || wiped.message !== 'flow_engine_native_silence_trigger_missing'
      || wiped.legacy !== 'claimed') {
    fail('el disparador borrado', wiped);
  }
  results.disparador_borrado = `${wiped.code} ${wiped.message}, legado ${wiped.legacy}`;
}

// ---------------------------------------------------------------------------
// 11. Solo service_role ejecuta la reserva nativa.
// ---------------------------------------------------------------------------
{
  const denied = {};
  for (const role of ['anon', 'authenticated']) {
    await db.exec(`set role ${role}`);
    let caught = null;
    try {
      await sql(`select * from public.claim_flow_engine_silence_v1($1,$2,$3,${marks(4, 15)})`,
        [FLOW.flowId, 1, FLOW.sha, ...claimArgs(tres)]);
    } catch (error) {
      caught = error;
    } finally {
      await db.exec('reset role');
    }
    if (caught?.code !== '42501') fail(`${role} ejecuto la reserva nativa`, caught?.message);
    denied[role] = caught.code;
  }
  results.acl = denied;
}

console.log(JSON.stringify({ flow_engine_native_silence: 'OK', ...results }));
await db.close();
