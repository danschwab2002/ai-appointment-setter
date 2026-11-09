// Reciclar un telefono de prueba (migracion 20261102000400, F-147 PR 1), con las RPC reales sobre la
// cadena completa de migraciones.
//
// Contrato: docs/contracts/identity-recycle-v1.md.
//
// Prueba, con un rojo por pieza (los numeros son los de la seccion 8 del diseno E-257):
//   0. la forma: agrega exactamente sus funciones y sus tres tablas, cambia solo las dos funciones de
//      inmutabilidad (con su marca), ningun otro permiso; las del aprobador solo para flow_change_approver,
//      la purga solo para service_role, ninguna para el rol del Automation Expert, anon ni authenticated
//      (6); tablas con RLS y sin permisos; correrla dos veces aborta. Antes de la migracion no existe nada;
//   1. el inventario: los conteos por tabla, contados aparte con el criterio de E-253, y las guardas: casos
//      abiertos, sin huella de prueba (D2), una fila de otra persona (3) y la vista vencida (7), cada una
//      sin cambiar ninguna fila;
//   2. aplicar: los frenos llamados de verdad (el dueno del formulario, _portable_precheckout_stop_reason y
//      get_portable_person_activity_v1) frenan antes y dan contacto nuevo y null despues, con un mail nuevo
//      y con el mail viejo reciclado (D3); el mail compartido no se toca;
//   3. una persona real distinta (R) sigue frenada y sus filas quedan identicas;
//   4. una compra nueva con el mismo telefono despues del reciclado frena de nuevo;
//   5. la excepcion no se falsifica (set_config, un reciclado de otra transaccion, otra columna, DELETE),
//      con el control positivo de la misma escritura con el reciclado de la transaccion;
//   8. deshacer deja las siete tablas como antes (sin updated_at), no se puede con el telefono de vuelta,
//      ni despues de la purga;
//   9. la purga deja el diario sin ningun telefono ni mail de la persona;
//  10. test_activity_ids_v1 trae las ids de la prueba y la venta de prueba sale de la cuenta.
//
// Datos sinteticos: telefonos de Mexico inventados con 555-01xx (el rango de ficcion), mails
// @example.test y los ID de Slack de forma valida e inventados.
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261102000400_identity_recycle_v1.sql';
const results = {};
const sha = (text) => createHash('sha256').update(text).digest('hex');
const fixture = (path) => JSON.parse(readFileSync(join(root, 'tests/fixtures', path), 'utf8'));

const APPROVER_FUNCTIONS = [
  'approver_identity_recycle_preview_v1(jsonb,text,text,text,boolean)',
  'approver_identity_recycle_apply_v1(jsonb,text,text,text,boolean,text)',
  'approver_get_identity_recycle_v1(jsonb,text,uuid)',
  'approver_identity_recycle_undo_v1(jsonb,text,uuid)',
];
const SERVICE_FUNCTIONS = ['purge_identity_recycles_v1()'];
const OWNER_FUNCTIONS = ['test_activity_ids_v1()'];
const INTERNAL_FUNCTIONS = [
  'guard_identity_recycle_change()',
  'guard_identity_recycle_journal_change()',
  '_identity_recycle_allows(text,jsonb,jsonb)',
  '_identity_recycle_contacts(text[])',
  '_identity_recycle_targets(text[],text[],uuid[],text[],text[],text[])',
  '_identity_recycle_set_paths(jsonb,jsonb)',
  '_identity_recycle_residue(text[],jsonb)',
  '_identity_recycle_inventory(text,text,text,text,boolean)',
  '_identity_recycle_blocked_by(jsonb)',
  '_identity_recycle_checks(jsonb,text[])',
  '_identity_recycle_mask_email(text)',
  '_identity_recycle_view(jsonb)',
  '_identity_recycle_write(uuid,boolean)',
  '_identity_recycle_returned(uuid)',
  '_identity_recycle_apply(text,text,text,text,text,text,boolean,text)',
  '_identity_recycle_undo_blocked(identity_recycles)',
  '_identity_recycle_undo(text,text,text,text,uuid)',
  '_identity_recycle_read(identity_recycles)',
];
const ALL_FUNCTIONS = [...APPROVER_FUNCTIONS, ...SERVICE_FUNCTIONS, ...OWNER_FUNCTIONS, ...INTERNAL_FUNCTIONS];
const PATCHED = ['protect_hotmart_purchase_intent_correlation()', 'protect_hotmart_cart_source_event()'];
const TABLES = ['identity_recycles', 'identity_recycle_rows', 'identity_recycle_activity'];

// ---------------------------------------------------------------------------
// 0. La forma de la migracion, en una base propia: antes y despues.
// ---------------------------------------------------------------------------
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature.replace(/^public\./, '').replace(/ /g, ''), row]));
const tableNames = async (db) => (await db.query(`select c.relname from pg_class c
  where c.relnamespace = 'public'::regnamespace and c.relkind = 'r' order by 1`)).rows.map((row) => row.relname);
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`${MIGRATION} no esta en la cadena`);
// Despues va solo la de los rechazos de Meta (20261103000500, E-284), que no toca el reciclado.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261103000500_meta_delivery_rejections_v1.sql',
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
  '20261109000100_meta_template_request_notices_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de los rechazos de Meta`, names.slice(position + 1));
}
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
{
  const shapeDb = new PGlite();
  await shapeDb.waitReady;
  await shapeDb.exec(`
    create role anon nologin;
    create role authenticated nologin;
    create role service_role nologin;
    create role authenticator noinherit login;
    alter default privileges in schema public grant execute on functions to anon, authenticated;
    alter default privileges in schema public grant all on functions to service_role;
    alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
    grant usage on schema public to service_role, anon;
  `);
  await applyFile(shapeDb, join(root, 'supabase/baseline/20260803_public_schema.sql'));
  for (const name of names.slice(0, position)) await applyFile(shapeDb, join(root, 'supabase/migrations', name));
  const before = await snapshot(shapeDb);
  const tablesBefore = await tableNames(shapeDb);
  if (ALL_FUNCTIONS.some((signature) => before.has(signature))) fail('alguna funcion ya existe antes de la migracion');
  if (PATCHED.some((signature) => before.get(signature).definition.includes('identity_recycle_v1'))) {
    fail('las funciones de inmutabilidad ya tienen la marca antes de la migracion');
  }
  const target = join(root, 'supabase/migrations', MIGRATION);
  await applyFile(shapeDb, target);
  const after = await snapshot(shapeDb);
  const added = [...after.keys()].filter((signature) => !before.has(signature));
  const removed = [...before.keys()].filter((signature) => !after.has(signature));
  const changed = [...before.keys()].filter((signature) => after.has(signature)
    && (after.get(signature).definition !== before.get(signature).definition
        || after.get(signature).acl !== before.get(signature).acl));
  if (!sameSet(added, ALL_FUNCTIONS) || removed.length !== 0 || !sameSet(changed, PATCHED)) {
    fail('la migracion cambio otra cosa', { added, removed, changed });
  }
  for (const signature of PATCHED) {
    const definition = after.get(signature).definition;
    if (after.get(signature).acl !== before.get(signature).acl
        || (definition.match(/identity_recycle_v1: begin/g) ?? []).length !== 1
        || !definition.includes('_identity_recycle_allows(')) {
      fail(`la excepcion de ${signature}`, definition);
    }
    // Fuera de la excepcion, el texto es el de antes: el reemplazo contado no toco nada mas.
    const stripped = definition.replace(/\n {4}-- identity_recycle_v1: begin[\s\S]*?-- identity_recycle_v1: end\n(\n)?/, '\n');
    if (stripped !== before.get(signature).definition) fail(`${signature} cambio fuera de la excepcion`);
  }
  const newTables = (await tableNames(shapeDb)).filter((table) => !tablesBefore.includes(table));
  if (!sameSet(newTables, TABLES)) fail('las tablas', newTables);
  const roles = ['automation_expert_change', 'flow_change_approver', 'service_role', 'anon', 'authenticated'];
  const grants = {};
  for (const signature of ALL_FUNCTIONS) {
    const allowed = [];
    for (const role of roles) {
      const can = one((await shapeDb.query(
        `select has_function_privilege($1, to_regprocedure('public.${signature}'), 'execute') as x`, [role])).rows,
      role).x;
      if (can) allowed.push(role);
    }
    grants[signature] = allowed;
    const expected = APPROVER_FUNCTIONS.includes(signature) ? ['flow_change_approver']
      : SERVICE_FUNCTIONS.includes(signature) ? ['service_role'] : [];
    if (!sameSet(allowed, expected)) fail(`los permisos de ${signature}`, allowed);
  }
  const definers = (await shapeDb.query(`select p.oid::regprocedure::text sig, prosecdef,
      coalesce(array_to_string(proconfig, ','), '') config
    from pg_proc p where p.pronamespace = 'public'::regnamespace
      and (p.proname like '%identity_recycle%' or p.proname = 'test_activity_ids_v1')`)).rows;
  for (const row of definers) {
    const signature = row.sig.replace(/^public\./, '').replace(/ /g, '');
    if (row.prosecdef !== !INTERNAL_FUNCTIONS.includes(signature)) fail(`security definer solo las publicas: ${signature}`);
    if (!row.config.includes('search_path=pg_catalog')) fail(`search_path fijo: ${signature}`, row);
  }
  const tableAcl = (await shapeDb.query(`select c.relname, c.relrowsecurity,
      has_table_privilege('anon', c.oid, 'select') a, has_table_privilege('authenticated', c.oid, 'select') u,
      has_table_privilege('service_role', c.oid, 'select') s,
      has_table_privilege('automation_expert_change', c.oid, 'select') x,
      has_table_privilege('flow_change_approver', c.oid, 'select') f
    from pg_class c where c.relname = any($1) and c.relnamespace = 'public'::regnamespace`, [TABLES])).rows;
  if (tableAcl.length !== 3 || tableAcl.some((row) => !row.relrowsecurity || row.a || row.u || row.s || row.x || row.f)) {
    fail('las tablas, sin permisos y con RLS', tableAcl);
  }
  const error = await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'identity_recycle_already_applied' });
  results.forma = { funciones: added.length, cambiadas: changed, tablas: newTables, segunda_corrida: error.message,
    permisos: Object.fromEntries(Object.entries(grants).filter(([, value]) => value.length)) };
  await shapeDb.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 (A) y otro producto de la misma instancia (B), con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, B, INSTANCE, FLOWS, LEGACY, rpc, admitForm, planCart, dbNow } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const DAN = 'U0DANSCHWAB1';
const TEAM = 'T0LANCEMOS01';
// Si la llamada falla dentro de una transaccion, el rollback de quien la espera deshace el set role.
const as = async (role, run) => {
  await db.exec(`set role ${role}`);
  let out;
  try {
    out = await run();
  } catch (error) {
    await db.exec('reset role').catch(() => {});
    throw error;
  }
  await db.exec('reset role');
  return out;
};
const approver = (run) => as('flow_change_approver', run);
const inMinutes = async (minutes) => new Date((await dbNow()).getTime() + minutes * 60_000).toISOString();
const shift = async (text, params) => {
  await db.exec('set session_replication_role = replica');
  await sql(text, params);
  await db.exec('set session_replication_role = origin');
};
const signIn = async (userId) => {
  const state = sha(`state-${userId}`);
  const session = sha(`session-${userId}`);
  await approver(async () => {
    await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha(`nonce-${userId}`), '/flow-approval/',
      await inMinutes(10));
    await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
    const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, TEAM, userId,
      await inMinutes(8 * 60));
    if (!created.ok) fail('la sesion', created);
  });
  return session;
};
const S = await signIn(DAN);
const page = (fn, ...params) => approver(() => rpc(fn, INSTANCE, S, ...params));
const preview = (phone, email, recycleEmails = true) => page('approver_identity_recycle_preview_v1', phone, email,
  recycleEmails);
const applyRecycle = (phone, email, fingerprint, recycleEmails = true) => page('approver_identity_recycle_apply_v1',
  phone, email, recycleEmails, fingerprint);

// Las personas. T es el telefono de prueba (dos contactos: el lead y uno dado de baja con la otra forma del
// movil, como en E-253); R es una compradora real con otro telefono y otro mail; D es el mismo Dan con otro
// telefono, que comparte un mail con T.
let serial = 0;
const lead = (label, phone, email, P = A) => {
  serial += 1;
  return { label, product: P, name: `Persona ${label}`, email, phone, country: '52', iso: 'MX',
    offer: P.offers.at(-1) ?? P.offers[0], chatwoot: String(970000 + serial) };
};
const T_PHONE = '525555010199';
const T_OTHER_FORM = '5215555010199';
const T_NATIONAL = T_PHONE.slice(-10);
const T_MAIL = 'reciclar-t@example.test';
const SHARED_MAIL = 'dan-compartido@example.test';
const R_PHONE = '525555010288';
const R_MAIL = 'compradora-real@example.test';
const D_PHONE = '525555010311';
const NEW_MAIL = 'prueba-nueva@example.test';
const T = lead('T', T_PHONE, T_MAIL);
const R = lead('R', R_PHONE, R_MAIL);

// El carrito de buildWorld lleva el mail en el id del evento (payload.id): el inventario lo veria como el
// mail en otra ruta, con razon. Aca va un id sin identidad, como los de Hotmart.
const CAPTURED_CART = fixture('hotmart_cart_abandonment_rejected_v1.json').payload;
let cartSerial = 0;
const cartFor = async (person) => {
  const P = person.product;
  const base = await dbNow();
  await admitForm(person, new Date(base.getTime() - 50 * 60_000));
  cartSerial += 1;
  const payload = structuredClone(CAPTURED_CART);
  payload.id = `reciclar-carrito-${cartSerial}`;
  payload.creation_date = base.getTime() - 30 * 60_000;
  payload.data.product = { id: P.productId, name: P.productName };
  payload.data.offer = { code: person.offer.offer_code };
  payload.data.buyer = { name: person.name, email: person.email, phone: person.phone };
  const admitted = one(await sql('select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [P.tenant, P.funnel, P.bindingVersion, payload.id, JSON.stringify(payload), person.email, person.phone]), 'carrito');
  if (admitted.outcome !== 'inserted') fail(`${person.label} carrito`, admitted.outcome);
  person.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
    values ($1, $2, $3, 'MX') returning id`, [person.name, person.email, person.phone]), 'contacto').id;
  await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id)
    values ($1, 'email', $2, $2, 'hotmart', $4), ($1, 'phone', $3, $3, 'hotmart', $4)`,
  [person.contact, person.email, person.phone, admitted.webhook_event_id]);
  person.cartArgs = [admitted.webhook_event_id, person.contact, String(P.productId), P.productName,
    person.offer.offer_code, LEGACY[P.scope], new Date(base.getTime() - 30 * 60_000).toISOString(), P.accountId,
    P.inboxId, person.phone, P.scope, 1];
  return person;
};

const CAPTURED_PURCHASE = fixture('hotmart_cash_payment_approved_att1_20261012.json').eventos[1].payload;
let purchaseSerial = 0;
const purchaseFor = async (person, { email = person.email, phone = person.phone } = {}) => {
  purchaseSerial += 1;
  const now = await dbNow();
  const payload = structuredClone(CAPTURED_PURCHASE);
  payload.id = `reciclar-compra-${purchaseSerial}`;
  payload.creation_date = now.getTime() - 60_000;
  Object.assign(payload.data.buyer, { name: person.name, first_name: person.name.split(' ')[0], email,
    checkout_phone: phone });
  payload.data.product.id = person.product.productId;
  payload.data.purchase.offer.code = person.offer.offer_code;
  payload.data.purchase.transaction = `HP${String(930000000 + purchaseSerial)}`;
  payload.data.purchase.approved_date = now.getTime() - 60_000;
  payload.data.purchase.order_date = now.getTime() - 120_000;
  const P = person.product;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [P.tenant, P.funnel, P.bindingVersion, payload.id, JSON.stringify(payload), email, phone]), 'compra');
  if (admitted.outcome !== 'inserted') fail('admitir la compra', admitted);
  one(await sql('select * from public.apply_hotmart_purchase_approved($1,$2,$3,$4,$5,$6,$7::timestamptz)',
    [admitted.webhook_event_id, email, phone, String(P.productId), person.offer.offer_code,
      payload.data.purchase.transaction, new Date(payload.data.purchase.approved_date).toISOString()]), 'aplicar');
  return admitted.webhook_event_id;
};
// Un formulario nuevo de alguien con este telefono y este mail, y lo que dicen los frenos de verdad.
const formAndStops = async (phone, email, contactId = null) => {
  const person = lead(`form-${serial}`, phone, email);
  await admitForm(person, await dbNow());
  const intent = one(await sql(`select id from public.purchase_intents where normalized_email = $1
    and lifecycle_state = 'waiting_for_purchase' order by created_at desc limit 1`, [email]), 'intencion').id;
  const owner = one(await sql('select * from public._find_portable_precheckout_contact($1)', [intent]), 'duenos');
  const stop = one(await sql('select public._portable_precheckout_stop_reason($1, $2) as r', [intent, contactId]),
    'freno').r;
  return { intent, owner: owner.reason_code, stop };
};
const activity = async (phone) => {
  let n = 0;
  for (const P of [A, B]) {
    for (const form of [phone, `521${phone.slice(2)}`]) {
      n += Number(one(await sql('select count(*)::int n from public.get_portable_person_activity_v1($1,$2,$3)',
        [P.tenant, P.funnel, form]), 'actividad').n);
    }
  }
  return n;
};
const inRollback = async (run) => {
  await db.exec('begin');
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};
// La huella de las siete tablas del inventario, sin updated_at (set_updated_at lo mueve en cada UPDATE).
const SEVEN = ['contact_points', 'contacts', 'channel_identities', 'purchase_intents',
  'hotmart_purchase_intent_event_identities', 'webhook_events', 'contact_opt_out_events'];
const footprint = async (filter = null) => one(await sql(`select md5(coalesce(string_agg(x, '|' order by x), '')) h from (
  ${SEVEN.map((table) => `select '${table}:' || (to_jsonb(t) - 'updated_at')::text x from public.${table} t`).join(' union all ')}
) y ${filter ? 'where strpos(x, $1) > 0 or strpos(x, $2) > 0' : ''}`, filter ?? []), 'huella').h;
const rFootprint = () => footprint([R_MAIL, R_PHONE.slice(-10)]);

// --- T: en la cohorte del carrito, formulario, carrito, caso y compra; otro contacto dado de baja con la
// otra forma del movil (y su baja de Chatwoot); un segundo mail que es tambien de D.
await cartFor(T);
await planCart(T);
await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref, added_by_kind)
  values ($1, $2, 'dan', 'operador')`, [FLOWS.carrito, T.contact]);
await sql(`insert into public.channel_identities (contact_id, channel, account_id, external_user_id)
  values ($1, 'whatsapp', $2, $3) on conflict do nothing`, [T.contact, `chatwoot:${A.accountId}`, T_PHONE]);
const T2 = one(await sql(`insert into public.contacts (full_name, contact_permission, lifecycle_status)
  values ('Persona T baja', 'opted_out', 'do_not_contact') returning id`), 'baja').id;
await sql(`insert into public.channel_identities (contact_id, channel, account_id, external_user_id)
  values ($1, 'whatsapp', $2, $3)`, [T2, `chatwoot:${A.accountId}`, T_OTHER_FORM]);
await sql(`insert into public.contact_opt_out_events (contact_id, channel, purpose, source, canonical_account_id,
    canonical_inbox_id, canonical_conversation_id, canonical_message_id, external_user_id, occurred_at,
    normalized_rule_key, correlation_status)
  values ($1, 'whatsapp', 'cart_recovery', 'chatwoot', $2, $3, 2, 9001, $4, now() - interval '20 days',
    'baja_de_prueba', 'applied')`, [T2, A.accountId, A.inboxId, T_OTHER_FORM]);
const D = one(await sql(`insert into public.contacts (full_name, email, phone) values ('Persona D', $1, $2)
  returning id`, [SHARED_MAIL, D_PHONE]), 'D').id;
await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
  values ($1, 'email', $2, $2, 'manual'), ($1, 'phone', $3, $3, 'manual')`, [D, SHARED_MAIL, D_PHONE]);
await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
  values ($1, 'email', $2, $2, 'manual')`, [T.contact, SHARED_MAIL]);

// Con el caso de T abierto, la vista frena.
{
  const view = await preview(T_PHONE, NEW_MAIL);
  if (view.can_recycle || view.blocked_by !== 'casos_abiertos') fail('un caso abierto frena', view);
  results.guardas = { caso_abierto: view.blocked_by };
}
const T_PURCHASE = await purchaseFor(T);
// Una compra de prueba con el mail compartido (el de D) y el telefono de T: es de T (el mail es de sus
// contactos), se le reescribe el telefono y el mail queda.
const T_PURCHASE_SHARED = await purchaseFor(T, { email: SHARED_MAIL });
if ((await sql(`select 1 from public.recovery_cases where contact_id = $1
    and status in ('grace_period', 'active', 'paused')`, [T.contact])).length) {
  fail('la compra de T no cerro su caso');
}

// --- R: carrito, caso, compra, y un formulario vivo despues de comprar.
await cartFor(R);
await planCart(R);
const R_CASE = R.plan.recovery_case_id;
await purchaseFor(R);
await admitForm(R, await dbNow());
const R_LIVE = one(await sql(`select id from public.purchase_intents where normalized_email = $1
  and lifecycle_state = 'waiting_for_purchase'`, [R_MAIL]), 'intencion viva de R').id;
const rStops = async () => ({
  caso: one(await sql('select public._portable_case_purchase_stop_reason($1) r', [R_CASE]), 'caso R').r,
  formulario: one(await sql('select public._portable_precheckout_stop_reason($1, $2) r', [R_LIVE, R.contact]),
    'form R').r,
});
const rBefore = await rStops();
if (rBefore.caso !== 'purchase_detected_any_offer' || rBefore.formulario !== 'purchase_by_identity') {
  fail('R frena antes', rBefore);
}
const rPrint = await rFootprint();

// ---------------------------------------------------------------------------
// 1. El inventario y las guardas.
// ---------------------------------------------------------------------------
// D2: el telefono de R no esta en ninguna cohorte.
{
  const view = await preview(R_PHONE, null);
  if (view.can_recycle || view.blocked_by !== 'sin_huella_de_prueba') fail('sin huella de prueba', view);
  results.guardas.sin_huella = view.blocked_by;
}
// Los conteos, contados aparte con el criterio de E-253 (todas las formas del telefono, por digitos).
const forms = [T_PHONE, T_OTHER_FORM];
const expectedRows = {
  contact_points: Number(one(await sql(`select count(*)::int n from public.contact_points
    where (type = 'phone' and regexp_replace(normalized_value, '\\D', '', 'g') = any($1))
       or (type = 'email' and contact_id = $2 and normalized_value = $3)`, [forms, T.contact, T_MAIL]), 'cp').n),
  contacts: 1,
  channel_identities: 2,
  purchase_intents: Number(one(await sql(`select count(*)::int n from public.purchase_intents
    where normalized_phone = any($1) or normalized_email = $2`, [forms, T_MAIL]), 'pi').n),
  hotmart_purchase_intent_event_identities: Number(one(await sql(`select count(*)::int n
    from public.hotmart_purchase_intent_event_identities where normalized_phone = any($1) or normalized_email = $2`,
  [forms, T_MAIL]), 'hi').n),
  webhook_events: Number(one(await sql(`select count(*)::int n from public.webhook_events
    where regexp_replace(coalesce(payload #>> '{data,buyer,phone}', payload #>> '{data,buyer,checkout_phone}', ''),
      '\\D', '', 'g') = any($1) or lower(payload #>> '{data,buyer,email}') = $2`, [forms, T_MAIL]), 'we').n),
  contact_opt_out_events: 1,
};
// 1 punto de telefono y 1 de mail (el compartido no); 2 intenciones (formulario y la del carrito no:
// la del formulario), los eventos (carrito y dos compras).
if (expectedRows.contact_points !== 2 || expectedRows.webhook_events !== 3) fail('el mundo de T', expectedRows);

const view = await preview(T_PHONE, NEW_MAIL);
{
  const text = JSON.stringify(view);
  if (!view.can_recycle || view.blocked_by !== null) fail('la vista de T', view);
  if (text.includes(T_NATIONAL) || text.includes(T_MAIL) || text.includes(SHARED_MAIL) || text.includes(NEW_MAIL)) {
    fail('la vista trae identidad en claro', view);
  }
  if (view.last2 !== '99' || view.people.contacts !== 2 || view.people.opted_out !== 1
      || view.history.purchases !== 2 || view.history.carts !== 1 || view.opt_outs.length !== 1
      || JSON.stringify(view.emails.recycled) !== JSON.stringify(['r···@e···.test'])
      || JSON.stringify(view.emails.shared) !== JSON.stringify(['d···@e···.test'])
      || view.cohorts.length !== 1 || view.cohorts[0].name !== `${A.funnel}/carrito`) {
    fail('lo que ve el aprobador', view);
  }
  for (const [table, n] of Object.entries(expectedRows)) {
    if (view.rows[table] !== n) fail(`el inventario de ${table}`, { vista: view.rows[table], esperado: n });
  }
  results.inventario = { filas: view.rows, celdas: view.cells, guardas: view.guards };
  // D3 apagado (la casilla sin marcar): los mails no se tocan y la vista lo dice.
  const noMails = await preview(T_PHONE, NEW_MAIL, false);
  if (!noMails.can_recycle || noMails.emails.recycle !== false || noMails.emails.recycled.length !== 0
      || JSON.stringify(noMails.emails.not_recycled) !== JSON.stringify(['r···@e···.test'])
      || noMails.rows.contact_points !== 1 || noMails.cells >= view.cells || noMails.fingerprint === view.fingerprint) {
    fail('sin reciclar los mails', noMails);
  }
  results.inventario.sin_mails = { celdas: noMails.cells };
}

// 3c. Una compra con el telefono de prueba y el mail de R: el reciclado entero no corre.
await inRollback(async () => {
  await purchaseFor(T, { email: R_MAIL });
  const before = await footprint();
  const blocked = await preview(T_PHONE, NEW_MAIL);
  const out = await applyRecycle(T_PHONE, NEW_MAIL, blocked.fingerprint);
  if (out.ok || out.reason_code !== 'fila_de_otra_persona' || (await footprint()) !== before
      || (await sql('select 1 from public.identity_recycles')).length) {
    fail('una fila de otra persona aborta todo', out);
  }
  results.guardas.otra_persona = out.reason_code;
});
// 3c'. Lo mismo con un formulario: el telefono de prueba y el mail de otra persona en una intencion.
await inRollback(async () => {
  await admitForm(lead('T-con-mail-ajeno', T_PHONE, 'otra-persona@example.test'), await dbNow());
  const blocked = await preview(T_PHONE, NEW_MAIL);
  if (blocked.blocked_by !== 'fila_de_otra_persona') fail('una intencion de otra persona', blocked);
});
// 7. Vista vencida: un carrito nuevo del telefono entre la vista y el si.
await inRollback(async () => {
  const cart = structuredClone(fixture('hotmart_cart_abandonment_rejected_v1.json').payload);
  cart.id = 'reciclar-carrito-entre-medio';
  cart.creation_date = Date.now() - 60_000;
  cart.data.product = { id: A.productId, name: A.productName };
  cart.data.offer = { code: T.offer.offer_code };
  cart.data.buyer = { name: T.name, email: T_MAIL, phone: T_PHONE };
  await sql('select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, cart.id, JSON.stringify(cart), T_MAIL, T_PHONE]);
  const before = await footprint();
  const out = await applyRecycle(T_PHONE, NEW_MAIL, view.fingerprint);
  if (out.ok || out.reason_code !== 'vista_vencida' || (await footprint()) !== before) fail('la vista vencida', out);
  results.guardas.vista_vencida = out.reason_code;
});

// ---------------------------------------------------------------------------
// 2. Antes de reciclar, los frenos de verdad frenan a una persona nueva con el telefono.
// ---------------------------------------------------------------------------
const stopsBefore = await inRollback(() => formAndStops(T_PHONE, NEW_MAIL));
const stopsBeforeOldMail = await inRollback(() => formAndStops(T_PHONE, T_MAIL));
const activityBefore = await activity(T_PHONE);
if (stopsBefore.owner !== 'precheckout_contact_ambiguous' || stopsBefore.stop !== 'purchase_by_identity'
    || stopsBeforeOldMail.stop !== 'purchase_by_identity' || activityBefore === 0) {
  fail('antes de reciclar frena', { stopsBefore, stopsBeforeOldMail, activityBefore });
}
const sales = async () => Number(one(await sql(`select count(*)::int n from public.portable_hotmart_purchase_correlations c
  join public.webhook_events w on w.id = c.webhook_event_id
  where c.tenant_ref = $1 and w.event_type = 'PURCHASE_APPROVED'
    and not exists (select 1 from public.test_activity_ids_v1() t where t.kind = 'webhook_event' and t.id = w.id)`,
[A.tenant]), 'ventas').n);
const salesBefore = await sales();
const printBefore = await footprint();

// ---------------------------------------------------------------------------
// 2. Aplicar.
// ---------------------------------------------------------------------------
const applied = await applyRecycle(T_PHONE, NEW_MAIL, view.fingerprint);
if (!applied.ok || applied.counts.checks !== 17 || applied.last2 !== '99') fail('aplicar', applied);
for (const [table, n] of Object.entries(expectedRows)) {
  if (applied.counts.rows[table] !== n) fail(`aplicar cambio ${table}`, applied.counts);
}
const recycle = one(await sql('select * from public.identity_recycles where id = $1', [applied.recycle_id]), 'reciclado');
if (recycle.status !== 'aplicado' || recycle.actor_kind !== 'slack' || recycle.actor_ref !== DAN) fail('la fila', recycle);
{
  const marker = `999${String(recycle.recycle_number).padStart(9, '0')}`;
  const markers = await sql('select distinct marker from public.identity_recycle_rows where recycle_id = $1 and kind = $2',
    [recycle.id, 'phone']);
  if (!markers.some((row) => row.marker === marker)) fail('el marcador de digitos', markers);
  // El mail compartido no se toco: sigue en D, en el punto de T y en la compra que lo lleva.
  const shared = Number(one(await sql(`select count(*)::int n from public.contact_points where normalized_value = $1`,
    [SHARED_MAIL]), 'compartido').n);
  const sharedEvent = one(await sql(`select payload #>> '{data,buyer,email}' e, payload #>> '{data,buyer,checkout_phone}' p
    from public.webhook_events where id = $1`, [T_PURCHASE_SHARED]), 'compra con el compartido');
  if (shared !== 2 || sharedEvent.e !== SHARED_MAIL || sharedEvent.p !== marker) fail('el mail compartido', { shared, sharedEvent });
}
const stopsAfter = await inRollback(() => formAndStops(T_PHONE, NEW_MAIL));
const stopsAfterOldMail = await inRollback(() => formAndStops(T_PHONE, T_MAIL));
const activityAfter = await activity(T_PHONE);
if (stopsAfter.owner !== 'precheckout_contact_not_found' || stopsAfter.stop !== null
    || stopsAfterOldMail.owner !== 'precheckout_contact_not_found' || stopsAfterOldMail.stop !== null
    || activityAfter !== 0) {
  fail('despues de reciclar no frena', { stopsAfter, stopsAfterOldMail, activityAfter });
}
// La baja del contacto viejo sigue siendo verdad.
const oldOptOut = one(await sql('select contact_permission, lifecycle_status from public.contacts where id = $1', [T2]), 'T2');
if (oldOptOut.contact_permission !== 'opted_out' || oldOptOut.lifecycle_status !== 'do_not_contact') fail('la baja vieja', oldOptOut);
results.aplicar = { filas: applied.counts.rows, verificaciones: applied.counts.checks,
  antes: { duenos: stopsBefore.owner, freno: stopsBefore.stop, actividad: activityBefore },
  despues: { duenos: stopsAfter.owner, freno: stopsAfter.stop, mail_viejo: stopsAfterOldMail.stop, actividad: activityAfter } };

// 3. R sigue frenada y sus filas no cambiaron.
{
  const after = await rStops();
  if (JSON.stringify(after) !== JSON.stringify(rBefore) || (await rFootprint()) !== rPrint) fail('R cambio', after);
  results.persona_real = after;
}
// 4. No es un pase permanente: una compra nueva con el telefono frena de nuevo.
await inRollback(async () => {
  await purchaseFor(lead('T-otra-vez', T_PHONE, 'otra-vez@example.test'));
  const again = await formAndStops(T_PHONE, NEW_MAIL);
  if (again.stop !== 'purchase_by_identity') fail('una compra nueva frena de nuevo', again);
  results.pase_permanente = again.stop;
});

// 10. Las ids de prueba y la venta de prueba fuera de la cuenta.
{
  const ids = await sql('select kind, id from public.test_activity_ids_v1()');
  const has = (kind, id) => ids.some((row) => row.kind === kind && row.id === id);
  const tIntents = (await sql('select id from public.purchase_intents where id in (select row_id from public.identity_recycle_rows where table_name = $1)',
    ['purchase_intents'])).map((row) => row.id);
  if (!has('contact', T.contact) || !has('contact', T2) || !has('webhook_event', T_PURCHASE)
      || !has('webhook_event', T_PURCHASE_SHARED) || !has('recovery_case', T.plan.recovery_case_id)
      || tIntents.length === 0 || !tIntents.every((id) => has('purchase_intent', id))
      || has('contact', R.contact) || has('recovery_case', R_CASE) || has('contact', D)) {
    fail('las ids de prueba', ids);
  }
  const salesAfter = await sales();
  if (salesBefore !== 3 || salesAfter !== 1) fail('la venta de prueba sale de la cuenta', { salesBefore, salesAfter });
  results.metricas = { ids: ids.length, ventas_antes: salesBefore, ventas_despues: salesAfter };
}

// ---------------------------------------------------------------------------
// 8. Deshacer.
// ---------------------------------------------------------------------------
{
  const read = await page('approver_get_identity_recycle_v1', applied.recycle_id);
  if (!read.can_undo || read.status !== 'aplicado' || JSON.stringify(read).includes(T_NATIONAL)) fail('releer', read);
  // Con el telefono de vuelta (un contacto nuevo), no se puede y no cambia nada.
  await inRollback(async () => {
    const fresh = one(await sql(`insert into public.contacts (full_name) values ('Persona nueva') returning id`), 'nueva').id;
    await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
      values ($1, 'phone', $2, $2, 'hotmart')`, [fresh, T_PHONE]);
    const before = await footprint();
    const out = await page('approver_identity_recycle_undo_v1', applied.recycle_id);
    if (out.ok || out.reason_code !== 'identity_recycle_phone_returned' || (await footprint()) !== before) {
      fail('deshacer con el telefono de vuelta', out);
    }
  });
  const undone = await page('approver_identity_recycle_undo_v1', applied.recycle_id);
  if (!undone.ok || (await footprint()) !== printBefore) fail('deshacer deja todo como antes', undone);
  const row = one(await sql('select * from public.identity_recycles where id = $1', [applied.recycle_id]), 'deshecho');
  const leftovers = Number(one(await sql('select count(*)::int n from public.identity_recycle_rows where recycle_id = $1 and old_value is not null',
    [applied.recycle_id]), 'diario').n);
  if (row.status !== 'deshecho' || row.undone_by_ref !== DAN || leftovers !== 0) fail('el deshecho', { row, leftovers });
  if ((await sales()) !== salesBefore) fail('un deshecho no cuenta como prueba');
  const again = await page('approver_identity_recycle_undo_v1', applied.recycle_id);
  if (again.ok || again.reason_code !== 'identity_recycle_purged') fail('deshacer dos veces', again);
  results.deshacer = { huella: 'igual', telefono_de_vuelta: 'identity_recycle_phone_returned', otra_vez: again.reason_code };
}

// ---------------------------------------------------------------------------
// 9. Otra vez, y la purga a las 72 h.
// ---------------------------------------------------------------------------
{
  const second = await preview(T_PHONE, NEW_MAIL);
  const out = await applyRecycle(T_PHONE, NEW_MAIL, second.fingerprint);
  if (!out.ok) fail('reciclar otra vez', out);
  const early = await as('service_role', () => rpc('purge_identity_recycles_v1'));
  if (!early.ok || early.purged !== 0) fail('la purga antes de tiempo', early);
  await shift(`update public.identity_recycles set applied_at = applied_at - interval '73 hours' where id = $1`, [out.recycle_id]);
  const window = await page('approver_get_identity_recycle_v1', out.recycle_id);
  if (window.can_undo || window.undo_blocked_by !== 'identity_recycle_undo_window_closed') fail('la ventana', window);
  const purged = await as('service_role', () => rpc('purge_identity_recycles_v1'));
  if (!purged.ok || purged.purged !== 1) fail('la purga', purged);
  const undo = await page('approver_identity_recycle_undo_v1', out.recycle_id);
  if (undo.ok || undo.reason_code !== 'identity_recycle_purged') fail('deshacer despues de la purga', undo);
  // Sin los ids, la huella y la transaccion (hex y digitos al azar): lo que queda es lo que guarda de la persona.
  // Los marcadores tienen su forma fija (el CHECK de la tabla): reciclado:<hex8>:<hex8>, 999 + 9 cifras o
  // reciclado-N-M@reciclado.invalid, y ninguno lleva un dato de la persona.
  const markers = (await sql('select distinct marker from public.identity_recycle_rows')).map((row) => row.marker);
  if (markers.some((m) => m.includes(T_NATIONAL) || !/^(reciclado:[0-9a-f]{8}:[0-9a-f]{8}|999[0-9]{9}|reciclado-[0-9]+-[0-9]+@reciclado\.invalid)$/.test(m))) {
    fail('los marcadores', markers);
  }
  const journal = (await sql(`select (to_jsonb(j) - 'recycle_id' - 'row_id' - 'created_at' - 'marker')::text t
      from public.identity_recycle_rows j
    union all select (to_jsonb(r) - 'id' - 'fingerprint' - 'txid' - 'created_at' - 'applied_at' - 'undone_at'
      - 'purged_at')::text from public.identity_recycles r`)).map((row) => row.t).join('\n');
  if ((await sql('select 1 from public.identity_recycle_rows where old_value is not null')).length) {
    fail('quedan valores viejos en el diario');
  }
  for (const secret of [T_NATIONAL, T_MAIL, SHARED_MAIL, NEW_MAIL, R_MAIL]) {
    if (journal.includes(secret)) fail('el diario purgado todavia tiene identidad');
  }
  const shapes = [...journal.matchAll(/[0-9]{8,}/g)].map((m) => m[0]).filter((n) => !/^999[0-9]{9}$/.test(n));
  const mails = [...journal.matchAll(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+/g)].map((m) => m[0])
    .filter((m) => !m.endsWith('@reciclado.invalid'));
  if (shapes.length || mails.length) fail('formas de telefono o mail en el diario purgado', { shapes, mails });
  const ids = await sql('select kind, id from public.test_activity_ids_v1()');
  if (!ids.some((r) => r.kind === 'webhook_event' && r.id === T_PURCHASE) || (await sales()) !== 1) {
    fail('las ids siguen despues de la purga');
  }
  results.purga = { antes_de_tiempo: early.purged, a_las_72h: purged.purged, deshacer: undo.reason_code };
}

// ---------------------------------------------------------------------------
// 6. Permisos de llamada y sesion.
// ---------------------------------------------------------------------------
{
  const noSession = await expectError(db, 'sin sesion', () => approver(() => rpc('approver_identity_recycle_preview_v1',
    INSTANCE, sha('nadie'), T_PHONE, null, true)), { code: '28000', message: 'flow_approval_session_invalid' });
  for (const role of ['automation_expert_change', 'service_role', 'anon', 'authenticated']) {
    await expectError(db, `${role} no recicla`, () => as(role, () => rpc('approver_identity_recycle_apply_v1',
      INSTANCE, S, T_PHONE, null, true, 'f'.repeat(64))), { code: '42501' });
    await expectError(db, `${role} no llama a la interna`, () => as(role, () => sql(
      `select public._identity_recycle_apply('lancemos','att1','slack','${DAN}','${T_PHONE}',null,true,'${'f'.repeat(64)}')`)),
    { code: '42501' });
  }
  for (const role of ['automation_expert_change', 'anon', 'authenticated']) {
    await expectError(db, `${role} no purga`, () => as(role, () => rpc('purge_identity_recycles_v1')), { code: '42501' });
    await expectError(db, `${role} no lee las ids`, () => as(role, () => sql('select * from public.test_activity_ids_v1()')),
      { code: '42501' });
  }
  results.permisos = { sin_sesion: noSession.code };
}

// ---------------------------------------------------------------------------
// 5. La excepcion no se falsifica. Cada caso escribe como duenio (superusuario), que es mas de lo que
//    puede cualquier rol de la API. Un evento de identidad y un carrito de R.
// ---------------------------------------------------------------------------
{
  const rEvent = one(await sql(`select webhook_event_id from public.hotmart_purchase_intent_event_identities
    where normalized_email = $1 limit 1`, [R_MAIL]), 'identidad de R').webhook_event_id;
  const rCart = one(await sql(`select id from public.webhook_events where event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
    and payload #>> '{data,buyer,email}' = $1`, [R_MAIL]), 'carrito de R').id;
  const forged = '999999999999';
  const updateIdentity = `update public.hotmart_purchase_intent_event_identities set normalized_phone = '${forged}'
    where webhook_event_id = '${rEvent}'`;
  const updateCart = `update public.webhook_events set payload = jsonb_set(payload, '{data,buyer,phone}', '"${forged}"')
    where id = '${rCart}'`;
  // a. Un set_config y ningun reciclado.
  await expectError(db, 'set_config', async () => {
    await sql("select set_config('identity_recycle.allow', 'on', true)");
    await sql(updateIdentity);
  }, { code: '23514', message: 'hotmart_purchase_intent_correlation_immutable' });
  await expectError(db, 'set_config en el carrito', async () => {
    await sql("select set_config('identity_recycle.allow', 'on', true)");
    await sql(updateCart);
  }, { code: '23514', message: 'cart_abandonment_source_immutable' });
  // Un reciclado de esta transaccion con su diario, como lo deja la funcion.
  const seed = async (rows) => {
    const r = one(await sql(`insert into public.identity_recycles (tenant_ref, ally_ref, actor_kind, actor_ref, status,
        txid, fingerprint, phone_last2, recycle_emails)
      values ($1, $2, 'slack', $3, 'aplicando', txid_current(), $4, '88', false) returning id`,
    [A.tenant, A.ally, DAN, 'a'.repeat(64)]), 'reciclado sembrado').id;
    for (const [table, rowId, column, oldValue] of rows) {
      await sql(`insert into public.identity_recycle_rows (recycle_id, table_name, row_id, column_name, kind, old_value, marker)
        values ($1, $2, $3, $4, 'phone', $5, $6)`, [r, table, rowId, column, oldValue, forged]);
    }
    return r;
  };
  const rPhone = one(await sql('select normalized_phone p from public.hotmart_purchase_intent_event_identities where webhook_event_id = $1',
    [rEvent]), 'telefono de R').p;
  const rCartPhone = one(await sql(`select payload #>> '{data,buyer,phone}' p from public.webhook_events where id = $1`,
    [rCart]), 'telefono del carrito').p;
  // Control positivo: con el reciclado de la transaccion y su diario, la misma escritura pasa.
  await inRollback(async () => {
    await seed([['hotmart_purchase_intent_event_identities', rEvent, 'normalized_phone', rPhone],
      ['webhook_events', rCart, 'payload.data.buyer.phone', rCartPhone]]);
    await sql(updateIdentity);
    await sql(updateCart);
  });
  // c. Otra columna ademas de la identidad.
  await expectError(db, 'otra columna', async () => {
    await seed([['hotmart_purchase_intent_event_identities', rEvent, 'normalized_phone', rPhone]]);
    await sql(`update public.hotmart_purchase_intent_event_identities set normalized_phone = '${forged}',
      normalized_email = 'otro@example.test' where webhook_event_id = '${rEvent}'`);
  }, { code: '23514', message: 'hotmart_purchase_intent_correlation_immutable' });
  await expectError(db, 'otra ruta del carrito', async () => {
    await seed([['webhook_events', rCart, 'payload.data.buyer.phone', rCartPhone]]);
    await sql(`update public.webhook_events set payload = jsonb_set(jsonb_set(payload, '{data,buyer,phone}', '"${forged}"'),
      '{data,buyer,name}', '"Otra"') where id = '${rCart}'`);
  }, { code: '23514', message: 'cart_abandonment_source_immutable' });
  await expectError(db, 'otro valor que el marcador', async () => {
    await seed([['hotmart_purchase_intent_event_identities', rEvent, 'normalized_phone', rPhone]]);
    await sql(`update public.hotmart_purchase_intent_event_identities set normalized_phone = '525555019999'
      where webhook_event_id = '${rEvent}'`);
  }, { code: '23514', message: 'hotmart_purchase_intent_correlation_immutable' });
  // d. Un DELETE, siempre.
  await expectError(db, 'delete de la identidad', async () => {
    await seed([['hotmart_purchase_intent_event_identities', rEvent, 'normalized_phone', rPhone]]);
    await sql(`delete from public.hotmart_purchase_intent_event_identities where webhook_event_id = '${rEvent}'`);
  }, { code: '23514' });
  await expectError(db, 'delete del carrito', async () => {
    await seed([['webhook_events', rCart, 'payload.data.buyer.phone', rCartPhone]]);
    await sql(`delete from public.webhook_events where id = '${rCart}'`);
  }, { code: '23514' });
  // El diario no se agrega sin un reciclado de la transaccion, y el reciclado no nace en otro estado.
  await expectError(db, 'diario sin reciclado', () => sql(`insert into public.identity_recycle_rows
    (recycle_id, table_name, row_id, column_name, kind, old_value, marker)
    values ($1, 'webhook_events', $2, 'payload.data.buyer.phone', 'phone', 'x', $3)`, [applied.recycle_id, rCart, forged]),
  { code: '23514', message: 'identity_recycle_journal_closed' });
  await expectError(db, 'nace aplicado', () => sql(`insert into public.identity_recycles (tenant_ref, ally_ref, actor_kind,
      actor_ref, status, txid, fingerprint, phone_last2, recycle_emails, applied_at)
    values ($1, $2, 'slack', $3, 'aplicado', txid_current(), $4, '88', false, now())`, [A.tenant, A.ally, DAN, 'a'.repeat(64)]),
  { code: '23514', message: 'identity_recycle_transition_invalid' });
  // b. Un reciclado de OTRA transaccion: se siembra y se confirma, y la escritura va en la siguiente.
  await db.exec('begin');
  await seed([['hotmart_purchase_intent_event_identities', rEvent, 'normalized_phone', rPhone]]);
  await db.exec('commit');
  await expectError(db, 'un reciclado de otra transaccion', () => sql(updateIdentity),
    { code: '23514', message: 'hotmart_purchase_intent_correlation_immutable' });
  if (one(await sql('select normalized_phone p from public.hotmart_purchase_intent_event_identities where webhook_event_id = $1',
    [rEvent]), 'R intacta').p !== rPhone) fail('R cambio por una falsificacion');
  results.excepcion = { set_config: '23514', otra_transaccion: '23514', otra_columna: '23514', delete: '23514',
    control_positivo: 'pasa' };
}

console.log(JSON.stringify(results, null, 2));
console.log('validate_identity_recycle: OK');
