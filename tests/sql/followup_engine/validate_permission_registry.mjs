// El registro de permiso de WhatsApp y la segunda rama del permiso del carrito y
// del pago rechazado nativos (migracion 20261116000200, E-627, M1 de E-624).
//
// Por que: el combo de Maribel no tiene formulario. El permiso sale de un aviso
// en el checkout o en la landing, que un conector (M2) escribe en
// permission_grants. El nucleo solo pregunta si hay un permiso registrado; la
// rama del formulario va primero e intacta.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera). El carrito es el capturado (hotmart_cart_abandonment_rejected_v1.json,
// con telefono) y los rechazos, los capturados de ATT1
// (hotmart_purchase_canceled_att1_20261019.json), cada uno con la persona
// sembrada y pasado por las admisiones portables de hoy. El paso del carrito
// nombra la plantilla de Maribel (como validate_flow_engine_native_cart.mjs) y el
// pago rechazado es el completo de C3 (como validate_e483_payment_failure_by_phone.mjs).
//
// Prueba:
//   0. la forma: cambian exactamente la admision y la puerta, con su marca;
//      nacen las ocho funciones, las cuatro RPC solo de service_role y las
//      privadas de nadie; las dos tablas sin acceso directo de nadie de la API;
//      la restriccion del recorrido nombra el permiso; correrla dos veces aborta;
//      despues va solo la de E-656;
//   1. las RPC: el aviso entra, se repite sin cambios y otro contenido con el
//      mismo id aborta; el hash que no es del texto, la vigencia anterior a la
//      medicion y una clave de mas no entran; el permiso entra y el mismo acto
//      otra vez es duplicate; lo que el aviso no cubre es rejected con su motivo
//      (aviso desconocido, de otro conector, de otro producto, de otra version,
//      anterior a la vigencia, oferta y uso fuera del aviso, despues del cierre);
//      el cierre y la revocacion; nada se borra ni se reescribe;
//   2. el carrito sin formulario: sin permiso, no_entra:permiso (lo de hoy); con
//      el permiso, arranca, con el permiso en la fila del recorrido, sin
//      intencion, y la autorizacion registered_permission; rojo: con la
//      admision de antes, no_entra:permiso; el despacho autoriza y se acepta;
//      rojo: con la puerta de antes, el arranque rechaza; un permiso revocado
//      despues de admitir frena en la puerta con ya_no_entra, y la baja, con baja;
//   3. lo que no entra: un permiso de otra oferta, de otro telefono, posterior al
//      carrito, vencido, revocado, de otra instancia o de otro producto, solo de
//      difusion, o de otro contacto (no_entra:permiso); la baja de Chatwoot de
//      ese movil (baja);
//   4. con formulario, el permiso formulario de siempre, aunque haya un permiso
//      registrado (la intencion en la fila, sin el permiso);
//   5. el pago rechazado sin formulario: sin permiso, no_entra:permiso; con el
//      permiso, arranca con la marca; un pendiente cancelado, no_es_un_rechazo;
//      con la lista de campana y la oferta en ella, gana el telefono de Hotmart
//      (la rama de hoy) y no el registro.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, errorOf, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261116000200_permission_registry_v1.sql';
const TZ = 'timestamp with time zone';
const ADMIT_NATIVE = `_flow_engine_admit_native_flow(flow_controls,webhook_events,uuid,text,${TZ},bigint,bigint,text)`;
const TAIL = `_flow_engine_authorize_native_tail(uuid,uuid,flow_engine_runs,recovery_cases,flow_controls,${TZ})`;
const CHANGED = [ADMIT_NATIVE, TAIL];
const RPCS = [
  'register_permission_notice_v1(jsonb)',
  `close_permission_notice_v1(text,text,${TZ})`,
  'record_permission_grant_v1(jsonb)',
  'revoke_permission_grant_v1(uuid,text)',
];
const PRIVATE = [
  '_permission_registry_append_only()',
  `_registered_permission_reason(text,uuid,text,text,text,${TZ},text)`,
  `_flow_engine_native_registered_audience(uuid,uuid,text,text,text,text,${TZ})`,
  '_registered_permission_recheck(uuid,uuid,text,bigint)',
];
const MARKER = 'permission_registry';
const maribel = JSON.parse(readFileSync(join(root,
  'tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json'), 'utf8')).message_templates[0];
const REFUSALS = JSON.parse(readFileSync(join(root, 'tests/fixtures/hotmart_purchase_canceled_att1_20261019.json'), 'utf8'));
const REFUSED = REFUSALS.eventos[0].payload;   // CREDIT_CARD, «Transaction refused»: rama banco.
const PENDING = REFUSALS.eventos[5].payload;   // MERCADO_PAGO cancelado sin motivo: no es un rechazo.
if (maribel.name !== 'maribel_retomar_proceso_01' || REFUSED.data.purchase.payment.refusal_reason !== 'Transaction refused'
    || PENDING.data.purchase.payment.type !== 'MERCADO_PAGO' || PENDING.data.purchase.payment.refusal_reason) {
  fail('los fixtures no son los que esta prueba espera');
}
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues va solo el destinatario de un flujo nativo de audiencia (20261117000500, E-661): cambia el tipo de
// get_followup_execution_context, que esta no toca.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261117000500_flow_engine_native_audience_recipient_v1.sql',
  // La repeticion de un flujo de audiencia cuenta solo a quien arranco (20261117000600, E-665).
  '20261117000600_flow_audience_repeat_counts_started_v1.sql',
  // Los motivos de quien no arranco en un envio a una audiencia (20261117000700, E-666).
  '20261117000700_flow_audience_start_reasons_v1.sql',
  // El numero de conversacion de los avisos del correlador (20261118000300, E-656), que no toca el permiso.
  '20261118000300_identity_link_conversation_number_account_v1.sql',
  // El choque de identidad en la admision es un rechazo con nombre (20261125000100).
  '20261125000100_flow_engine_admission_identity_mismatch_v1.sql',
  // El almacen del masivo (20261125000200).
  '20261125000200_bulk_send_store_v1.sql'])) {
  fail(`despues de ${MIGRATION} van solo la de E-661, la de E-665, la de E-666 y la de E-656`, names.slice(position + 1));
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
  if (!sameSet(changed, CHANGED) || !sameSet(added, [...RPCS, ...PRIVATE]) || removed.length !== 0
      || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  for (const signature of CHANGED) {
    if (!after.get(signature).definition.includes(MARKER)) fail(`${signature}: sin la marca`);
    previous[signature] = before.get(signature).definition;
  }
  for (const signature of RPCS) {
    const row = after.get(signature);
    if (!row.service_x || row.api_x || !row.prosecdef) fail(`${signature}: solo de service_role`, row);
  }
  for (const signature of PRIVATE) {
    const row = after.get(signature);
    if (row.service_x || row.api_x || row.prosecdef) fail(`${signature}: no es de nadie de la API`, row);
  }
  for (const table of ['permission_notices', 'permission_grants']) {
    const acl = one((await db.query(`select c.relrowsecurity as rls,
        has_table_privilege('service_role', c.oid, 'select') as service_select,
        has_table_privilege('anon', c.oid, 'select') or has_table_privilege('authenticated', c.oid, 'select') as api_select
      from pg_class c where c.oid = $1::regclass`, [`public.${table}`])).rows, table);
    if (!acl.rls || acl.service_select || acl.api_select) fail(`${table}: sin acceso directo de nadie de la API`, acl);
  }
  const check = one((await db.query(`select pg_get_constraintdef(oid) as d from pg_constraint
    where conrelid = 'public.flow_engine_native_runs'::regclass and conname = 'flow_engine_native_runs_audience_by_trigger'`)).rows,
  'restriccion').d;
  if (!check.includes('audience_permission_grant_id') || !check.includes('audience_hotmart_phone')) {
    fail('la restriccion del recorrido', check);
  }
  const error = await errorOf(db, () => applyFile(db, join(root, 'supabase/migrations', MIGRATION)));
  if (error?.message !== 'permission_registry_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  await db.exec('rollback').catch(() => {});
  if ((await snapshot(db)).get(ADMIT_NATIVE).definition !== after.get(ADMIT_NATIVE).definition) fail('la segunda corrida cambio algo');
  results.forma = { cambiadas: changed.length, nuevas: added.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 (buildWorld, la cadena entera).
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, dbNow, reserve, startNew, accept, person, cartFor, admitForm } = world;
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
const rpcError = async (text, params) => {
  try {
    await asService(async () => db.query(text, params));
    return null;
  } catch (error) {
    return error.message;
  }
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
const cohort = async (flowId, contactId) => sql(
  `insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')
   on conflict do nothing`, [flowId, contactId]);

// El aviso y el permiso, como los escribiria el conector de M2.
const NOW = await dbNow();
const iso = (date) => date.toISOString();
const ago = (minutes) => new Date(NOW.getTime() - minutes * 60_000);
const TEXT = 'Al continuar, aceptas que El Protocolo te contacte por WhatsApp al numero que ingresaste.';
const sha = one(await sql(`select encode(sha256(convert_to($1, 'UTF8')), 'hex') as h`, [TEXT]), 'hash').h;
const OFFER = A.offers.at(-1).offer_code;
const OTHER_OFFER = A.offers.find((o) => o.offer_code !== OFFER).offer_code;
const NOTICE = {
  tenant_ref: A.tenant, notice_ref: 'att1-checkout-prueba-v1', connector: 'hotmart_checkout', product_ref: A.funnel,
  offer_refs: [OFFER, OTHER_OFFER], uses: ['recuperacion', 'difusion'], surface_url: 'https://pay.hotmart.com/prueba',
  copy_version: 'att1-whatsapp-checkout-v1', notice_text: TEXT, notice_text_sha256: sha, checkbox: 'ninguna',
  measured_at: iso(ago(24 * 60)), measured_by: 'E-627 validador', valid_from: iso(ago(24 * 60)),
};
const registerNotice = async (notice) => one(await asService(async () => (await db.query(
  'select * from public.register_permission_notice_v1($1::jsonb)', [JSON.stringify(notice)])).rows), 'aviso');
let evidenceSerial = 0;
const evidence = () => {
  evidenceSerial += 1;
  return `00000000-0000-4000-8000-${String(evidenceSerial).padStart(12, '0')}`;
};
const grantOf = (lead, extra = {}) => ({
  tenant_ref: A.tenant, phone: lead.phone, product_ref: A.funnel, offer_ref: lead.offer?.offer_code ?? OFFER,
  uses: ['recuperacion'], connector: 'hotmart_checkout', connector_version: 'm1-prueba', evidence_kind: 'aviso_en_el_paso',
  evidence_ref: lead.event ?? evidence(), notice_ref: NOTICE.notice_ref, copy_version: NOTICE.copy_version,
  granted_at: iso(ago(60)), expires_at: iso(new Date(NOW.getTime() + 7 * 24 * 60 * 60_000)), ...extra,
});
const recordGrant = async (grant) => one(await asService(async () => (await db.query(
  'select * from public.record_permission_grant_v1($1::jsonb)', [JSON.stringify(grant)])).rows), 'permiso');

// ---------------------------------------------------------------------------
// 1. Las RPC.
// ---------------------------------------------------------------------------
{
  const out = {};
  const inserted = await registerNotice(NOTICE);
  if (inserted.outcome !== 'inserted' || !inserted.notice_id) fail('el aviso entra', inserted);
  const again = await registerNotice(NOTICE);
  if (again.outcome !== 'unchanged' || again.notice_id !== inserted.notice_id) fail('el mismo aviso otra vez', again);
  out.conflicto = await rpcError('select * from public.register_permission_notice_v1($1::jsonb)',
    [JSON.stringify({ ...NOTICE, offer_refs: [OFFER] })]);
  out.hash_ajeno = await rpcError('select * from public.register_permission_notice_v1($1::jsonb)',
    [JSON.stringify({ ...NOTICE, notice_ref: 'otro-v1', notice_text_sha256: '0'.repeat(64) })]);
  out.antes_de_medir = await rpcError('select * from public.register_permission_notice_v1($1::jsonb)',
    [JSON.stringify({ ...NOTICE, notice_ref: 'otro-v2', valid_from: iso(ago(25 * 60)) })]);
  out.clave_de_mas = await rpcError('select * from public.register_permission_notice_v1($1::jsonb)',
    [JSON.stringify({ ...NOTICE, notice_ref: 'otro-v3', telefono: '1' })]);
  if (out.conflicto !== 'permission_notice_conflict' || out.hash_ajeno !== 'permission_notice_invalid_arguments'
      || out.antes_de_medir !== 'permission_notice_invalid_arguments'
      || out.clave_de_mas !== 'permission_notice_invalid_arguments') {
    fail('los avisos que no entran', out);
  }
  // Un aviso de otro conector, para los rechazos.
  await registerNotice({ ...NOTICE, notice_ref: 'att1-landing-prueba-v1', connector: 'landing' });

  const lead = { phone: '12025559901' };
  const first = await recordGrant(grantOf(lead, { evidence_ref: evidence() }));
  if (first.outcome !== 'inserted' || !first.grant_id) fail('el permiso entra', first);
  const sameAct = grantOf(lead, { evidence_ref: evidence() });
  const a = await recordGrant(sameAct);
  const b = await recordGrant(sameAct);
  if (a.outcome !== 'inserted' || b.outcome !== 'duplicate' || b.grant_id !== a.grant_id) fail('el mismo acto otra vez', { a, b });
  const rejected = {};
  for (const [label, extra] of Object.entries({
    aviso_desconocido: { notice_ref: 'no-existe-v1' },
    aviso_de_otro_conector: { connector: 'landing' },
    aviso_de_otro_producto: { product_ref: 'otro-producto' },
    aviso_de_otra_version: { copy_version: 'otra-v9' },
    aviso_no_vigente: { granted_at: iso(ago(25 * 60)) },
    oferta_fuera_del_aviso: { offer_ref: 'zzzz9999' },
    oferta_nula_con_aviso_por_oferta: { offer_ref: null },
    uso_fuera_del_aviso: { notice_ref: 'att1-landing-prueba-v1', connector: 'landing', uses: ['recuperacion', 'otro'] },
  })) {
    const row = await recordGrant(grantOf(lead, { evidence_ref: evidence(), ...extra }));
    rejected[label] = row.reason;
    if (row.outcome !== 'rejected' || row.grant_id !== null) fail(`${label}: rejected`, row);
  }
  if (rejected.oferta_nula_con_aviso_por_oferta !== 'oferta_fuera_del_aviso'
      || rejected.uso_fuera_del_aviso !== 'uso_fuera_del_aviso'
      || Object.entries(rejected).some(([k, v]) => !k.startsWith('oferta_nula') && k !== v)) {
    fail('los motivos del rechazo', rejected);
  }
  out.rechazos = rejected;
  out.telefono_invalido = await rpcError('select * from public.record_permission_grant_v1($1::jsonb)',
    [JSON.stringify(grantOf(lead, { evidence_ref: evidence(), phone: 'sin numero' }))]);
  out.vence_antes = await rpcError('select * from public.record_permission_grant_v1($1::jsonb)',
    [JSON.stringify(grantOf(lead, { evidence_ref: evidence(), expires_at: iso(ago(120)) }))]);
  if (out.telefono_invalido !== 'permission_grant_invalid_arguments'
      || out.vence_antes !== 'permission_grant_invalid_arguments') {
    fail('los permisos invalidos', out);
  }
  // El cierre: despues de valid_until no nacen permisos; el que ya estaba sigue.
  await registerNotice({ ...NOTICE, notice_ref: 'att1-checkout-cerrado-v1' });
  const closed = one(await asService(async () => (await db.query(
    'select * from public.close_permission_notice_v1($1,$2,$3)', [A.tenant, 'att1-checkout-cerrado-v1', iso(ago(30))])).rows),
  'cierre');
  const closedAgain = one(await asService(async () => (await db.query(
    'select * from public.close_permission_notice_v1($1,$2,$3)', [A.tenant, 'att1-checkout-cerrado-v1', iso(ago(10))])).rows),
  'cierre otra vez');
  const afterClose = await recordGrant(grantOf(lead, { evidence_ref: evidence(), notice_ref: 'att1-checkout-cerrado-v1',
    granted_at: iso(ago(20)) }));
  const beforeClose = await recordGrant(grantOf(lead, { evidence_ref: evidence(), notice_ref: 'att1-checkout-cerrado-v1',
    granted_at: iso(ago(40)) }));
  if (closed.outcome !== 'closed' || closedAgain.outcome !== 'already_closed' || afterClose.reason !== 'aviso_no_vigente'
      || beforeClose.outcome !== 'inserted') {
    fail('el cierre del aviso', { closed, closedAgain, afterClose, beforeClose });
  }
  // La revocacion.
  const revoked = one(await asService(async () => (await db.query(
    'select * from public.revoke_permission_grant_v1($1,$2)', [first.grant_id, 'dan'])).rows), 'revocacion');
  const revokedAgain = one(await asService(async () => (await db.query(
    'select * from public.revoke_permission_grant_v1($1,$2)', [first.grant_id, 'dan'])).rows), 'revocacion otra vez');
  if (revoked.outcome !== 'revoked' || revokedAgain.outcome !== 'already_revoked') fail('la revocacion', { revoked, revokedAgain });
  // Nada se borra ni se reescribe.
  out.borrar_permiso = (await errorOf(db, () => sql('delete from public.permission_grants where id = $1', [a.grant_id])))?.message;
  out.reescribir_permiso = (await errorOf(db, () => sql(`update public.permission_grants set offer_ref = 'zzzz' where id = $1`,
    [a.grant_id])))?.message;
  out.desrevocar = (await errorOf(db, () => sql('update public.permission_grants set revoked_at = null, revoked_by = null where id = $1',
    [first.grant_id])))?.message;
  out.borrar_aviso = (await errorOf(db, () => sql('delete from public.permission_notices where notice_ref = $1',
    ['att1-landing-prueba-v1'])))?.message;
  out.reescribir_aviso = (await errorOf(db, () => sql(`update public.permission_notices set notice_text = 'otro' where notice_ref = $1`,
    ['att1-landing-prueba-v1'])))?.message;
  for (const key of ['borrar_permiso', 'reescribir_permiso', 'desrevocar', 'borrar_aviso', 'reescribir_aviso']) {
    if (out[key] !== 'permission_registry_append_only') fail(`${key}: solo se agrega`, out);
  }
  // Nadie de la API llama las RPC.
  await db.exec('set role anon');
  const anon = await errorOf(db, () => db.query('select * from public.record_permission_grant_v1($1::jsonb)',
    [JSON.stringify(grantOf(lead))]));
  await db.exec('reset role');
  if (!anon?.message?.includes('permission denied')) fail('anon no llama la RPC', anon?.message);
  results.rpc = out;
}

// ---------------------------------------------------------------------------
// 2. El carrito sin formulario.
// ---------------------------------------------------------------------------
const CART = {
  esquema: 'setter-flujo/v1', flujo: 'carrito_permiso', producto: A.funnel, estado: 'borrador', nombre: 'Carrito con permiso',
  dispara: { evento: 'carrito' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ plantilla: maribel.name, idioma: maribel.language, variables: ['nombre', 'producto'] }],
  termina: { si: ['compra'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-carrito-permiso', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
const cartFlow = await openNative('carrito_permiso', CART);
const cartEventOf = (lead) => ({
  fuente: 'hotmart', tipo: 'carrito', evento_id: lead.event, producto_ref: A.funnel, oferta: lead.offer.offer_code,
  contacto_id: lead.contact, momento: lead.cartArgs[6], chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
  destino: lead.phone,
});
const admitWith = async (instance, flowEvent, flowId) => one((await asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
  [JSON.stringify(instance), JSON.stringify(flowEvent)])).rows)).filter((row) => row.flow_id === flowId), 'admision');
const admitCart = async (lead) => admitWith(INSTANCE, cartEventOf(lead), cartFlow);
const nativeOf = async (caseId) => one(await sql('select * from public.flow_engine_native_runs where recovery_case_id = $1',
  [caseId]), 'recorrido');
const noFormCart = async (label) => {
  const lead = await cartFor(label, { form: false });
  await cohort(cartFlow, lead.contact);
  return lead;
};
const grantForCart = async (lead, extra = {}) => recordGrant(grantOf(lead, {
  granted_at: iso(new Date(new Date(lead.cartArgs[6]).getTime() - 60_000)), ...extra }));

{
  const ana = await noFormCart('permiso-ana');
  const without = await admitCart(ana);
  if (without.outcome !== 'motor_no_arranca' || without.reason !== 'no_entra:permiso') fail('sin permiso, lo de hoy', without);
  const grant = await grantForCart(ana);
  if (grant.outcome !== 'inserted') fail('el permiso del carrito de ana', grant);
  const red = await withPrevious([ADMIT_NATIVE], () => admitCart(ana));
  if (red.outcome !== 'motor_no_arranca' || red.reason !== 'no_entra:permiso') fail('sin la migracion, no_entra:permiso', red);
  ana.start = await admitCart(ana);
  if (ana.start.outcome !== 'motor_arranco' || ana.start.created !== true) fail('con el permiso, el carrito arranca', ana.start);
  const native = await nativeOf(ana.start.recovery_case_id);
  if (native.audience_permission_grant_id !== grant.grant_id || native.audience_purchase_intent_id !== null
      || native.audience_precheckout_submission_id !== null || native.audience_hotmart_phone !== false
      || native.trigger_event !== 'carrito') {
    fail('el recorrido con el permiso y sin intencion', native);
  }
  const authorization = one(await sql(`select authorization_status, evidence from public.contact_authorizations
    where contact_id = $1 and purpose = 'cart_recovery'`, [ana.contact]), 'autorizacion');
  if (authorization.authorization_status !== 'allowed' || authorization.evidence.reason !== 'registered_permission'
      || authorization.evidence.permission_grant_id !== grant.grant_id || authorization.evidence.webhook_event_id !== ana.event) {
    fail('la autorizacion del permiso registrado', authorization);
  }
  // El despacho.
  const lead = { label: 'permiso-ana', chatwoot: '627001' };
  const reserved = await reserve(lead, ana.start.scheduled_action_id);
  const redTail = await withPrevious([TAIL], () => startNew(reserved));
  if (redTail.outcome === 'request_started') fail('sin la migracion, la puerta no deja salir el carrito sin intencion', redTail);
  const started = await startNew(reserved);
  if (started.outcome !== 'request_started' || !started.flow_authorization_id) fail('el arranque del carrito', started);
  const accepted = await accept(lead, reserved);
  results.carrito = { sin_permiso: without.reason, sin_la_migracion: red.reason, con_permiso: ana.start.outcome,
    autorizacion: authorization.evidence.reason, puerta_de_antes: redTail.reason ?? redTail.outcome,
    arranque: started.outcome, aceptacion: accepted.status };

  // La puerta: un permiso revocado despues de admitir, y la baja.
  const beto = await noFormCart('permiso-beto');
  const betoGrant = await grantForCart(beto);
  beto.start = await admitCart(beto);
  if (beto.start.outcome !== 'motor_arranco') fail('beto arranca', beto.start);
  const betoReserved = await reserve({ label: 'permiso-beto', chatwoot: '627002' }, beto.start.scheduled_action_id);
  const revokedGate = await rolledBack(async () => {
    await asService(async () => db.query('select * from public.revoke_permission_grant_v1($1,$2)', [betoGrant.grant_id, 'prueba']));
    return startNew(betoReserved);
  });
  if (revokedGate.outcome === 'request_started' || revokedGate.reason !== 'ya_no_entra') fail('la puerta frena el revocado', revokedGate);
  const bajaGate = await rolledBack(async () => {
    await sql(`update public.contacts set contact_permission = 'opted_out' where id = $1`, [beto.contact]);
    return startNew(betoReserved);
  });
  if (bajaGate.outcome === 'request_started' || bajaGate.reason !== 'baja') fail('la puerta frena la baja', bajaGate);
  const authorizations = one(await sql(`select count(*)::int as n from public.flow_engine_request_authorizations
    where action_id = $1`, [betoReserved.actionId]), 'autorizaciones').n;
  if (authorizations !== 0) fail('el freno no consume cupo', authorizations);
  results.puerta = { revocado: revokedGate.reason, baja: bajaGate.reason };
}

// ---------------------------------------------------------------------------
// 3. Lo que no entra.
// ---------------------------------------------------------------------------
{
  const out = {};
  const refusedWith = async (label, extra, setup) => {
    const lead = await noFormCart(label);
    const grant = await grantForCart(lead, typeof extra === 'function' ? extra(lead) : extra);
    if (grant.outcome !== 'inserted') fail(`${label}: el permiso entra`, grant);
    if (setup) await setup(lead, grant);
    out[label] = (await admitCart(lead)).reason;
    if (out[label] !== 'no_entra:permiso') fail(`${label}: no_entra:permiso`, out);
  };
  await refusedWith('otra-oferta', { offer_ref: OTHER_OFFER });
  await refusedWith('otro-telefono', { phone: '12025559977' });
  await refusedWith('posterior-al-carrito', (lead) => ({ granted_at: iso(new Date(new Date(lead.cartArgs[6]).getTime() + 60_000)) }));
  await refusedWith('vencido', (lead) => ({ granted_at: iso(new Date(new Date(lead.cartArgs[6]).getTime() - 3 * 60_000)),
    expires_at: iso(new Date(new Date(lead.cartArgs[6]).getTime() - 60_000)) }));
  await refusedWith('revocado', {}, async (_lead, grant) => {
    await asService(async () => db.query('select * from public.revoke_permission_grant_v1($1,$2)', [grant.grant_id, 'prueba']));
  });
  await refusedWith('solo-difusion', { uses: ['difusion'] });
  await refusedWith('de-otro-contacto', (lead) => ({ contact_id: null }), async (lead, grant) => {
    const other = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
      values ('Otra', 'otra-permiso@example.test', $1, 'MX') returning id`, [lead.phone]), 'otro contacto').id;
    await sql('update public.permission_grants set contact_id = $1 where id = $2', [other, grant.grant_id]);
  });
  // De otra instancia o de otro producto: el permiso existe, pero no es de este flujo.
  {
    const lead = await noFormCart('otra-instancia');
    await registerNotice({ ...NOTICE, tenant_ref: 'otra-instancia', notice_ref: 'otra-checkout-v1' });
    const grant = await recordGrant(grantOf(lead, { tenant_ref: 'otra-instancia', notice_ref: 'otra-checkout-v1',
      granted_at: iso(new Date(new Date(lead.cartArgs[6]).getTime() - 60_000)) }));
    await registerNotice({ ...NOTICE, notice_ref: 'otro-producto-v1', product_ref: 'otro-producto' });
    const grant2 = await recordGrant(grantOf(lead, { product_ref: 'otro-producto', notice_ref: 'otro-producto-v1',
      evidence_ref: evidence(), granted_at: iso(new Date(new Date(lead.cartArgs[6]).getTime() - 60_000)) }));
    if (grant.outcome !== 'inserted' || grant2.outcome !== 'inserted') fail('los permisos ajenos entran', { grant, grant2 });
    out['otra-instancia-u-otro-producto'] = (await admitCart(lead)).reason;
    if (out['otra-instancia-u-otro-producto'] !== 'no_entra:permiso') fail('otra instancia u otro producto', out);
  }
  // La baja de Chatwoot de ese movil.
  {
    const lead = await noFormCart('baja-chatwoot');
    await grantForCart(lead);
    await sql(`insert into public.contact_opt_out_events (contact_id, channel, purpose, source, canonical_account_id,
        canonical_inbox_id, canonical_conversation_id, canonical_message_id, external_user_id, occurred_at,
        normalized_rule_key, correlation_status, projection_status)
      values ($1, 'whatsapp', 'cart_recovery', 'chatwoot', $2, $3, 627099, 627099, $4, clock_timestamp(),
        'stop_receiving_messages', 'applied', 'applied')`, [lead.contact, A.accountId, A.inboxId, lead.phone]);
    out['baja-chatwoot'] = (await admitCart(lead)).reason;
    if (out['baja-chatwoot'] !== 'baja') fail('la baja de Chatwoot', out);
  }
  results.no_entra = out;
}

// ---------------------------------------------------------------------------
// 4. Con formulario, el permiso de siempre.
// ---------------------------------------------------------------------------
{
  const lead = await cartFor('permiso-con-formulario');
  await cohort(cartFlow, lead.contact);
  await grantForCart(lead);
  const started = await admitCart(lead);
  if (started.outcome !== 'motor_arranco') fail('con formulario arranca', started);
  const native = await nativeOf(started.recovery_case_id);
  if (!native.audience_purchase_intent_id || native.audience_permission_grant_id !== null) {
    fail('con formulario, la intencion y sin el permiso registrado', native);
  }
  const authorization = one(await sql(`select evidence from public.contact_authorizations
    where contact_id = $1 and purpose = 'cart_recovery'`, [lead.contact]), 'autorizacion');
  if (authorization.evidence.reason !== 'precheckout_whatsapp_consent') fail('con formulario, la evidencia de siempre', authorization);
  results.con_formulario = { arranca: started.outcome, evidencia: authorization.evidence.reason };
}

// ---------------------------------------------------------------------------
// 5. El pago rechazado sin formulario.
// ---------------------------------------------------------------------------
{
  const B2 = 'att1_pago_rechazado_descuento_01';
  const Z = 'att1_ultima_renovacion_01';
  const COUPON = { link: 'checkout', cupon: 'PROTOCOLO10' };
  const b1 = (branch, coupon) => ({ plantilla: `maribel_pago_rechazado_${branch}_01`, idioma: 'es_MX',
    variables: ['nombre', 'producto'], boton: coupon ? { ...COUPON } : { link: 'checkout' } });
  const follow = (template) => ({ plantilla: template, idioma: 'es_MX', variables: ['nombre', 'producto'] });
  const path = (branch, coupon) => [b1(branch, coupon), { esperar: '24h' }, follow(coupon ? Z : B2)];
  const FLOW = {
    esquema: 'setter-flujo/v1', flujo: 'pago_rechazado_permiso', producto: A.funnel, estado: 'borrador',
    nombre: 'Pago rechazado con permiso', dispara: { evento: 'pago_fallido' }, entra: { permiso: 'formulario', contesto_a: [] },
    pasos: [{ esperar: '15m' }, {
      condicion: { dato: 'descuento_previo', es: ['si'] }, entonces: path('cupon', true),
      si_no: [{ condicion: { dato: 'rechazos', es: ['varios'] }, entonces: path('dos_veces', false),
        si_no: [{ condicion: { dato: 'tipo_de_rechazo', es: ['sin_saldo'] }, entonces: path('saldo', true),
          si_no: path('banco', false) }] }] }],
    termina: { si: ['compra', 'contesta'], vence: '3d' }, horario: 'siempre',
    topes: { bolsa: 'att1-pago-permiso', por_dia: 20, total: 1000, repeticion: { una_por: 'persona', otra_despues_de: '72h' } },
    prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  };
  const pfFlow = await openNative('pago_rechazado_permiso', FLOW);
  let serial = 0;
  const refusalOf = async (label, source = REFUSED) => {
    serial += 1;
    const lead = person(label);
    const payload = structuredClone(source);
    const moment = new Date((await dbNow()).getTime() - 10 * 60_000);
    payload.id = `permiso-rechazo-${lead.email}-${serial}`;
    payload.creation_date = moment.getTime();
    payload.data.buyer = { ...payload.data.buyer, name: lead.name, email: lead.email, checkout_phone: lead.phone };
    payload.data.product = { ...payload.data.product, id: A.productId, name: A.productName };
    payload.data.purchase.offer.code = OFFER;
    payload.data.purchase.transaction = `HPPR${String(serial).padStart(4, '0')}`;
    const admitted = one(await sql('select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)',
      [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone]), `${label} rechazo`);
    if (admitted.outcome !== 'inserted') fail(`${label} rechazo`, admitted);
    lead.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
      values ($1,$2,$3,'MX') returning id`, [lead.name, lead.email, lead.phone]), 'contacto').id;
    await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id)
      values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)`,
    [lead.contact, lead.email, lead.phone, admitted.webhook_event_id]);
    await cohort(pfFlow, lead.contact);
    lead.event = admitted.webhook_event_id;
    lead.moment = moment.toISOString();
    lead.offer = { offer_code: OFFER };
    return lead;
  };
  const pfEventOf = (lead) => ({
    fuente: 'hotmart', tipo: 'pago_fallido', evento_id: lead.event, producto_ref: A.funnel, oferta: OFFER,
    contacto_id: lead.contact, momento: lead.moment, chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
    destino: lead.phone,
  });
  const pfGrant = async (lead) => recordGrant(grantOf(lead, { granted_at: iso(new Date(new Date(lead.moment).getTime() - 60_000)) }));
  const out = {};
  const carla = await refusalOf('permiso-pf-carla');
  out.sin_permiso = (await admitWith(INSTANCE, pfEventOf(carla), pfFlow)).reason;
  if (out.sin_permiso !== 'no_entra:permiso') fail('el rechazo sin permiso', out);
  const grant = await pfGrant(carla);
  const started = await admitWith(INSTANCE, pfEventOf(carla), pfFlow);
  if (started.outcome !== 'motor_arranco') fail('el rechazo con permiso arranca', started);
  const native = await nativeOf(started.recovery_case_id);
  if (native.audience_permission_grant_id !== grant.grant_id || native.audience_hotmart_phone !== false
      || native.audience_purchase_intent_id !== null || native.trigger_event !== 'pago_fallido') {
    fail('el recorrido del rechazo con permiso', native);
  }
  out.con_permiso = started.outcome;
  const pending = await refusalOf('permiso-pf-pendiente', PENDING);
  await pfGrant(pending);
  out.pendiente = (await admitWith(INSTANCE, pfEventOf(pending), pfFlow)).reason;
  if (out.pendiente !== 'no_es_un_rechazo') fail('un pendiente cancelado', out);
  // Con la lista de campana y la oferta en ella, gana la rama de hoy (el telefono de Hotmart).
  const dora = await refusalOf('permiso-pf-dora');
  await pfGrant(dora);
  const byPhone = await admitWith({ ...INSTANCE, native_payment_failure_by_phone_offers: [OFFER] }, pfEventOf(dora), pfFlow);
  if (byPhone.outcome !== 'motor_arranco') fail('con la lista arranca', byPhone);
  const doraNative = await nativeOf(byPhone.recovery_case_id);
  if (doraNative.audience_hotmart_phone !== true || doraNative.audience_permission_grant_id !== null) {
    fail('con la lista gana el telefono de Hotmart', doraNative);
  }
  out.con_la_lista = 'telefono_de_hotmart';
  results.pago_rechazado = out;
}

await db.close();
console.log(JSON.stringify({ ok: true, ...results }, null, 2));
