// Los frenos del seguimiento con cupon despues de Z y con una ficha vigente, en
// la base (migracion 20261101000300, E-263, F-149 y F-150; diagnostico:
// documentacion/2026-11-01-tres-errores-de-mensajes-en-conversaciones-reales.md,
// errores 2 y 3).
//
// Por que: en ATT1 #491 la persona recibio Z (cart_last_touch, «no te voy a
// insistir mas»), contesto y al dia siguiente le llego el seguimiento con
// cupon; en #542 la persona tenia su ficha de OXXO vigente y le llego igual. Las
// dos reservas del seguimiento (la portable del legado y la nativa) suman
// blocked_after_last_touch y blocked_pending_payment despues de la compra.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera), y cada conversacion adoptada por los caminos de hoy, como en
// validate_flow_engine_native_silence.mjs: el formulario, el carrito capturado,
// la plantilla del carrito aceptada por Chatwoot y la respuesta por la admision
// portable. La plantilla del seguimiento y su idioma salen del catalogo
// capturado de la bandeja 11 (chatwoot_inbox_11_message_templates_20261010.json),
// con TIROIDES10. La ficha es la ficha 3 capturada de ATT1 (OXXO de Mexico,
// tests/fixtures/hotmart_payment_slip_printed_att1_20261018.json), con la
// persona, la transaccion, el producto y la oferta sustituidos, y entra por la
// admision real (admit_portable_hotmart_payment_slip_event_v1). La compra es la
// capturada de la bienvenida de ATT1.
//
// Lo que se deriva y por que:
//   * Z: sale por el camino real. La politica del carrito de A en el mundo es
//     la de tres toques (A1, A2 a 1 s y Z a 2 s, att1-carrito-tres-toques):
//     A1, A2 y Z se reservan, arrancan y los acepta Chatwoot como en el motor
//     de hoy, y despues la persona contesta (la adopcion).
//   * El vencimiento de la ficha: no hay captura de un PURCHASE_EXPIRED. Se
//     arma con la ficha capturada y event = PURCHASE_EXPIRED, la misma
//     transaccion, y entra por la misma admision real, que lo acepta.
//   * Las horas: received_at y accepted_at se corren hacia atras adentro de una
//     transaccion que se deshace.
//
// Prueba:
//   0. la forma: cambian exactamente las dos reservas, sin sumar ni sacar
//      funciones ni tocar dueno, security definer o permisos; correrla dos
//      veces aborta sin cambiar nada; con una portable cambiada (sin la
//      barrera de la compra) aborta con 55000 sin cambiar nada;
//   1. sin Z y sin ficha, las dos reservas dan LO MISMO que las de antes sobre
//      los mismos datos: el resultado, el link, la fila y lo que escriben
//      (la adoptada, la compra);
//   2. Z: el legado y la nativa dan blocked_after_last_touch sin escribir nada
//      ni gastar la bolsa; con las de antes daban claimed (rojo). Un Z de hace
//      31 dias ya no frena; el Z de otra persona no frena;
//   3. la ficha: el legado y la nativa dan blocked_pending_payment sin escribir
//      nada; por el telefono y por el mail; rojo con las de antes. Una ficha
//      vencida (PURCHASE_EXPIRED de la misma transaccion), una de hace 8 dias y
//      la de otra persona no frenan; con la compra aprobada gana la compra.
import { PGlite } from '@electric-sql/pglite';
import { randomBytes } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261101000300_followup_stops_after_last_touch_and_pending_payment_v1.sql';
const MARKER = 'followup_stops_after_last_touch_and_pending_payment';
const ARGS = 'bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamp with time zone';
const PORTABLE = `claim_portable_conversation_followup_v1(${ARGS})`;
const NATIVE = `claim_flow_engine_silence_v1(uuid,integer,text,${ARGS})`;
const CHANGED = [PORTABLE, NATIVE];
const results = {};
const fixture = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));
const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);

const names = readdirSync(join(root, 'supabase/migrations')).filter((name) => name.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
if (names.slice(position + 1).length !== 0) fail(`${MIGRATION} es la ultima de la cadena`, names.slice(position + 1));

const CATALOG = fixture('chatwoot_inbox_11_message_templates_20261010.json');
const template = one((CATALOG.message_templates ?? []).filter((item) => item.name === 'att1_seguimiento_descuento_01'),
  'la plantilla del seguimiento');
const TEMPLATE = { name: template.name, language: template.language };
const COUPON = 'TIROIDES10';
const AGE = 86_400 + 600;
const CAPTURED_PURCHASE = fixture('hotmart_purchase_approved_att1_bienvenida_20261026.json').payload;
// La primera ficha de OXXO de Mexico de la captura (de las cinco).
const CAPTURED_SLIP = fixture('hotmart_payment_slip_printed_att1_20261018.json').eventos
  .find((evento) => evento.payload.data.purchase.payment.type === 'CASHPAYMENT'
    && evento.payload.data.purchase.checkout_country.iso === 'MX')?.payload;
if (!CAPTURED_SLIP) fail('la captura no trae una ficha de OXXO de Mexico');

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
  const inside = readFileSync(join(root, 'supabase/migrations', MIGRATION), 'utf8')
    .replace('\nbegin;\n', '\n-- begin: la del validador\n').replace(/\ncommit;\s*$/, '\n');
  // El freno: una portable sin la barrera de la compra (otra migracion la cambio) aborta con 55000.
  {
    const portable = before.get(PORTABLE).definition;
    const altered = portable.replace("        outcome := 'purchase_already_approved';\n", "        outcome := 'purchase_seen';\n");
    if (altered === portable) fail('el freno: no se pudo armar la portable cambiada');
    await db.exec('begin');
    let caught = null;
    try {
      await db.exec(altered);
      await db.exec(inside);
    } catch (error) {
      caught = error;
    }
    await db.exec('rollback');
    if (caught?.code !== '55000' || caught.message !== 'unexpected_followup_claim_definition') {
      fail('el freno de la portable cambiada', { code: caught?.code, message: caught?.message });
    }
    const untouched = await snapshot(db);
    if ([...before.keys()].some((sig) => untouched.get(sig)?.definition !== before.get(sig).definition)) {
      fail('el freno dejo algo cambiado');
    }
    results.freno = caught.message;
  }
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.has(s) && after.get(s).definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && ['acl', 'prosecdef', 'provolatile', 'proowner', 'proconfig'].some((k) => JSON.stringify(after.get(s)[k])
      !== JSON.stringify(before.get(s)[k])));
  if (!sameSet(changed, CHANGED) || added.length !== 0 || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  // Cada una es la de antes con el bloque y nada mas, justo despues de la compra.
  for (const signature of CHANGED) {
    const now = after.get(signature).definition;
    const begin = now.indexOf(`    -- ${MARKER}: begin\n`);
    const endMark = `    -- ${MARKER}: end\n`;
    const end = now.indexOf(endMark);
    if (begin < 0 || end < begin) fail(`${signature}: sin el bloque`);
    const without = now.slice(0, begin - 1) + now.slice(end + endMark.length);
    if (without !== before.get(signature).definition) fail(`${signature}: cambio algo fuera del bloque`);
    const purchase = now.lastIndexOf("outcome := 'purchase_already_approved'", begin);
    if (purchase < 0 || now.indexOf('return;\n    end if;\n', purchase) > begin) {
      fail(`${signature}: el bloque no va justo despues de la compra`);
    }
    previous[signature] = before.get(signature).definition;
  }
  const native = after.get(NATIVE).definition;
  if (native.indexOf(`${MARKER}: end`) > native.indexOf('flow_engine_native_silence_pool: begin')) {
    fail('en la nativa el bloque va antes de la bolsa');
  }
  let error = null;
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  } catch (caught) {
    error = caught;
  }
  await db.exec('rollback').catch(() => {});
  if (error?.code !== '55000' || error.message !== 'followup_stops_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
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
const { db, A, register, cycle, versionItem, dbNow, cartFor, planCart, reserve, startOld, accept, stepOf, sleep } = world;
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
    lead.lastTouch = z.id;
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
  lead.conversation = conversation.id;
  lead.conversationContact = conversation.contact_id;
  lead.caseId = reply.commercial_case_id;
  messageSerial += 1;
  lead.inbound = 780_000 + messageSerial * 10;
  lead.outbound = lead.inbound + 1;
  return lead;
};

const marks = (from, count) => Array.from({ length: count }, (_, i) => `$${from + i}`).join(',');
const claimArgs = (lead, over = {}) => [Number(lead.chatwoot), A.accountId, A.inboxId, over.userId ?? lead.phone,
  over.email ?? lead.email, `followup:${lead.chatwoot}:${lead.outbound}`, 'link_sent_no_purchase', TEMPLATE.name,
  TEMPLATE.language, COUPON, lead.inbound, lead.outbound, AGE, over.ulid ?? ulid(), null];
const claimLegacy = async (lead, over = {}) => one(await asService(() => sql(
  `select * from public.claim_portable_conversation_followup_v1(${marks(1, 15)})`, claimArgs(lead, over))),
`${lead.label} reserva del legado`);
let FLOW = null;
const claimNative = async (lead, over = {}) => one(await asService(() => sql(
  `select * from public.claim_flow_engine_silence_v1($1,$2,$3,${marks(4, 15)})`,
  [FLOW.flowId, FLOW.version, FLOW.sha, ...claimArgs(lead, over)])), `${lead.label} reserva nativa`);
const footprint = async () => one(await sql(`
  select (select count(*)::integer from public.conversation_followup_events) as followups,
         (select count(*)::integer from public.checkout_link_issuances) as issuances,
         (select count(*)::integer from public.flow_engine_request_authorizations) as authorizations
`), 'huella');
const NO_DELTA = { followups: 0, issuances: 0, authorizations: 0 };
const behaviour = async (run) => {
  const start = await footprint();
  const row = await run();
  const event = row.followup_event_id ? one(await sql(`select status, template_name, coupon_code, conversation_id,
      commercial_case_id, flow_id, (select i.status from public.checkout_link_issuances i where i.id = checkout_issuance_id) as issuance
    from public.conversation_followup_events where id = $1`, [row.followup_event_id]), 'la fila') : null;
  return { outcome: row.outcome, url: row.checkout_url_final, event,
    delta: Object.fromEntries(Object.entries(await footprint()).map(([k, v]) => [k, v - start[k]])) };
};
// El mismo ulid en las dos corridas: el link sale igual si la reserva es la misma.
const contrast = async (label, run, signatures) => {
  const fixedUlid = ulid();
  const now = await rolledBack(() => behaviour(() => run(fixedUlid)));
  const before = await withPrevious(signatures, () => behaviour(() => run(fixedUlid)));
  if (!same(now, before)) fail(`${label}: cambio respecto de la de antes`, { now, before });
  return now;
};

// El silencio nativo de ATT1 en cohorte, como lo abre la publicacion.
const FOLLOWUP = {
  esquema: 'setter-flujo/v1', flujo: 'seguimiento_cupon', producto: A.funnel, estado: 'borrador',
  nombre: 'Seguimiento con cupón',
  dispara: { silencio: { lo_ultimo: 'agente', desde: '24h', hasta: '72h', conversaciones: ['abierta', 'resuelta'] } },
  entra: { permiso: 'conversacion', contesto_a: 'cualquier_plantilla' },
  pasos: [{ plantilla: TEMPLATE.name, idioma: TEMPLATE.language, variables: ['nombre', 'producto'],
    boton: { link: 'checkout', cupon: COUPON } }],
  termina: { si: ['compra', 'contesta'] },
  horario: 'siempre',
  topes: { bolsa: 'att1-seguimiento-cupon', por_dia: 'sin_tope', total: 'sin_tope',
    repeticion: { una_por: 'conversacion', otra_despues_de: 'nunca' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
{
  const flowId = await register(A.funnel, 'seguimiento_cupon');
  await cycle([versionItem(flowId, A.funnel, 'seguimiento_cupon', FOLLOWUP)], 'v1 del seguimiento');
  const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'propuesta').proposal_id;
  const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [flowId, proposalId, A.timezone]), 'abrir').r);
  if (!opened.ok || opened.state !== 'cohorte') fail('abrir el seguimiento', opened);
  const sha = one(await sql('select definition_sha256 from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'sha').definition_sha256;
  FLOW = { flowId, version: 1, sha };
}
const addToCohort = async (lead) => sql(
  `insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
  [FLOW.flowId, lead.conversationContact]);

// La compra aprobada de la persona, la capturada de ATT1.
let purchaseSerial = 0;
const purchase = async (lead) => {
  purchaseSerial += 1;
  const now = await dbNow();
  const payload = structuredClone(CAPTURED_PURCHASE);
  payload.id = `frenos-compra-${lead.email}-${purchaseSerial}`;
  payload.creation_date = now.getTime() - 60_000;
  Object.assign(payload.data.buyer, { name: lead.name, first_name: lead.name.split(' ')[0], email: lead.email,
    checkout_phone: lead.phone });
  payload.data.product.id = A.productId;
  payload.data.purchase.offer.code = lead.offer.offer_code;
  payload.data.purchase.transaction = `HP${String(960000000 + purchaseSerial)}`;
  payload.data.purchase.approved_date = now.getTime() - 60_000;
  payload.data.purchase.order_date = now.getTime() - 120_000;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone]), 'la compra');
  if (admitted.outcome !== 'inserted') fail('admitir la compra', admitted);
};
// La ficha capturada, de la persona, por la admision real. event PURCHASE_EXPIRED arma su vencimiento.
let slipSerial = 0;
const slip = async (lead, { event = 'PURCHASE_BILLET_PRINTED', transaction = null, phone = lead.phone,
  email = lead.email } = {}) => {
  slipSerial += 1;
  const now = await dbNow();
  const payload = structuredClone(CAPTURED_SLIP);
  payload.id = `frenos-ficha-${slipSerial}`;
  payload.event = event;
  payload.creation_date = now.getTime() - 60_000;
  Object.assign(payload.data.buyer, { name: lead.name, first_name: lead.name.split(' ')[0], email,
    checkout_phone: phone });
  payload.data.product.id = A.productId;
  payload.data.purchase.offer.code = lead.offer.offer_code;
  payload.data.purchase.transaction = transaction ?? `HP${String(970000000 + slipSerial)}`;
  payload.data.purchase.order_date = now.getTime() - 120_000;
  if (event === 'PURCHASE_EXPIRED') payload.data.purchase.status = 'EXPIRED';
  const admitted = one(await asService(() => sql(
    'select * from public.admit_portable_hotmart_payment_slip_event_v1($1,$2,$3,$4,$5::jsonb)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload)])), 'la ficha');
  if (admitted.outcome !== 'inserted') fail(`admitir ${event}`, admitted);
  return { transaction: payload.data.purchase.transaction, eventId: admitted.webhook_event_id };
};

// ---------------------------------------------------------------------------
// 1. Sin Z y sin ficha, lo mismo que antes.
// ---------------------------------------------------------------------------
{
  const got = {};
  const conversa = await adopted('sin-frenos');
  got.legado = (await contrast('el legado sin frenos', (u) => claimLegacy(conversa, { ulid: u }), [PORTABLE])).outcome;
  const compro = await adopted('sin-frenos-compro');
  await purchase(compro);
  got.legado_compra = (await contrast('el legado con la compra', (u) => claimLegacy(compro, { ulid: u }), [PORTABLE])).outcome;
  const nativa = await adopted('sin-frenos-nativa');
  await addToCohort(nativa);
  got.nativa = (await contrast('la nativa sin frenos', (u) => claimNative(nativa, { ulid: u }), [NATIVE])).outcome;
  if (!same(got, { legado: 'claimed', legado_compra: 'purchase_already_approved', nativa: 'claimed' })) {
    fail('sin frenos', got);
  }
  results.sin_z_ni_ficha_igual_que_antes = got;
}

// ---------------------------------------------------------------------------
// 2. Z: 30 dias sin seguimiento.
// ---------------------------------------------------------------------------
{
  const got = {};
  const zeta = await adopted('z-legado', { withLastTouch: true });
  // Otra persona, con su A1 y sin Z.
  const otra = await adopted('z-otra-persona');
  const legacy = await rolledBack(() => behaviour(() => claimLegacy(zeta)));
  const legacyRed = await withPrevious([PORTABLE], async () => (await claimLegacy(zeta)).outcome);
  const old = await rolledBack(async () => {
    await sql(`update public.followup_delivery_attempts set accepted_at = accepted_at - interval '31 days'
      where action_id = $1`, [zeta.lastTouch]);
    return (await claimLegacy(zeta)).outcome;
  });
  const other = await rolledBack(async () => (await claimLegacy(otra)).outcome);
  if (legacy.outcome !== 'blocked_after_last_touch' || !same(legacy.delta, NO_DELTA)
      || legacyRed !== 'claimed' || old !== 'claimed' || other !== 'claimed') {
    fail('Z en el legado', { legacy, legacyRed, old, other });
  }
  Object.assign(got, { legado: legacy.outcome, rojo_legado_de_antes: legacyRed, z_de_hace_31_dias: old,
    z_de_otra_persona: other });

  const zetaNativa = await adopted('z-nativa', { withLastTouch: true });
  await addToCohort(zetaNativa);
  const native = await rolledBack(() => behaviour(() => claimNative(zetaNativa)));
  const nativeRed = await withPrevious([NATIVE], async () => (await claimNative(zetaNativa)).outcome);
  if (native.outcome !== 'blocked_after_last_touch' || !same(native.delta, NO_DELTA) || nativeRed !== 'claimed') {
    fail('Z en la nativa', { native, nativeRed });
  }
  Object.assign(got, { nativa: native.outcome, rojo_nativa_de_antes: nativeRed, bolsa_sin_gastar: native.delta.authorizations });
  results.despues_de_z = got;
}

// ---------------------------------------------------------------------------
// 3. La ficha vigente.
// ---------------------------------------------------------------------------
{
  const got = {};
  const ficha = await adopted('ficha-legado');
  const { transaction } = await slip(ficha);
  const legacy = await rolledBack(() => behaviour(() => claimLegacy(ficha)));
  const legacyRed = await withPrevious([PORTABLE], async () => (await claimLegacy(ficha)).outcome);
  // Solo por el mail: la ficha con otro telefono.
  const byMail = await adopted('ficha-por-mail');
  await slip(byMail, { phone: '5215500000999' });
  const mail = await rolledBack(async () => (await claimLegacy(byMail)).outcome);
  const vencida = await rolledBack(async () => {
    await slip(ficha, { event: 'PURCHASE_EXPIRED', transaction });
    return (await claimLegacy(ficha)).outcome;
  });
  const vieja = await rolledBack(async () => {
    await sql(`update public.webhook_events set received_at = received_at - interval '8 days'
      where event_type = 'PURCHASE_BILLET_PRINTED' and payload #>> '{data,purchase,transaction}' = $1`, [transaction]);
    return (await claimLegacy(ficha)).outcome;
  });
  const otra = await adopted('ficha-otra-persona');
  const other = await rolledBack(async () => (await claimLegacy(otra)).outcome);
  const compro = await rolledBack(async () => {
    await purchase(ficha);
    return (await claimLegacy(ficha)).outcome;
  });
  if (legacy.outcome !== 'blocked_pending_payment' || !same(legacy.delta, NO_DELTA) || legacyRed !== 'claimed'
      || mail !== 'blocked_pending_payment' || vencida !== 'claimed' || vieja !== 'claimed' || other !== 'claimed'
      || compro !== 'purchase_already_approved') {
    fail('la ficha en el legado', { legacy, legacyRed, mail, vencida, vieja, other, compro });
  }
  Object.assign(got, { legado: legacy.outcome, rojo_legado_de_antes: legacyRed, solo_por_mail: mail,
    vencida_por_hotmart: vencida, de_hace_8_dias: vieja, de_otra_persona: other, con_la_compra: compro });

  const fichaNativa = await adopted('ficha-nativa');
  await addToCohort(fichaNativa);
  await slip(fichaNativa);
  const native = await rolledBack(() => behaviour(() => claimNative(fichaNativa)));
  const nativeRed = await withPrevious([NATIVE], async () => (await claimNative(fichaNativa)).outcome);
  if (native.outcome !== 'blocked_pending_payment' || !same(native.delta, NO_DELTA) || nativeRed !== 'claimed') {
    fail('la ficha en la nativa', { native, nativeRed });
  }
  Object.assign(got, { nativa: native.outcome, rojo_nativa_de_antes: nativeRed });
  results.ficha_vigente = got;
}

console.log(JSON.stringify({ ok: true, migration: MIGRATION, ...results }, null, 2));
