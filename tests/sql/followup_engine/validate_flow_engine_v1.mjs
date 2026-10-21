// Las rutas, las lecturas y la guarda del almacen sobre el legado (migracion
// 20261021000200, la T2.3a del motor de flujos), con las RPC reales sobre la
// cadena completa de migraciones.
//
// Contrato: docs/contracts/flow-engine-v1.md. Especificacion: «T2.3: el motor
// en la base» (2026-10-21, en el vault), parte T2.3a.
//
// Prueba:
//   0. la migracion: agrega exactamente sus funciones y sus cinco tablas (con
//      RLS y de nadie de la API); cambia exactamente siete funciones, y cada
//      una es su definicion vigente con su bloque insertado y nada mas; solo
//      las dos lecturas las ejecutan service_role y automation_expert_change;
//      correrla dos veces aborta con 55000;
//   1. compile_cases.json contra _flow_engine_compile_policy_core: los mismos
//      casos y los mismos esperados que pytest contra flow_policy.compile_policy;
//      un caso que da distinto es rojo;
//   2. crear la ruta, con un rojo por cada reason_code de 3.1 (flow_not_found,
//      flow_route_needs_published_version, flow_route_kind_mismatch,
//      flow_route_legacy_scope_unknown, flow_route_already_exists), el evento
//      flow_route_created, y los disparadores (legacy_kind inmutable, la ruta
//      no se borra, nace en legado, el disparador de la version activa);
//   3. mover la ruta: flow_route_generation_stale, unchanged y
//      flow_route_kind_not_supported (el pago_fallido, y en la T2.3a tambien el
//      carrito: el rojo a proposito del contrato). Con el motor de la T2.3b
//      simulado adentro de una transaccion que se deshace (_flow_engine_route_kinds
//      = {carrito}): flow_route_revalidation_invalid, flow_route_person_cap_present,
//      el paso a motor con su evento flow_route_moved, la guarda con la ruta en
//      motor, flow_route_legacy_not_armed, la vuelta a legado y flow_route_flow_off;
//   4. la guarda: con el carrito pausado en el almacen, un carrito no se
//      planifica (pilot_scope_rejected, flujo_pausado) y un pago fallido del
//      MISMO scope si; un A2 del carrito que esperaba se rechaza al arrancar
//      (pilot_request_start_rejected, flujo_pausado) y sale al prender; con el
//      carrito apagado, flujo_apagado; sin ruta, todo como hoy;
//   5. read_flow_engine_v1: la forma del JSON Schema del contrato
//      (tests/fixtures/flow_engine/read_flow_engine_v1.schema.json), lo en
//      curso del legado por legacy_kind, la ruta nula, la pagina de 50 con
//      next, y los permisos (service_role y el rol de la T4 si, anon no);
//   6. read_flow_engine_history_v1: la ventana (8 dias, too_wide y
//      since_invalid), la misma captura que la consulta v3 sobre la misma base
//      (salvo la sal, el origen y la siembra), una sal por llamada, y
//      flow_engine_history_too_large con mas de 1 MB.
//
// Con --salida <ruta> escribe la historia a ese archivo, para correrle la
// prueba de forma de Python:
//   uv run python scripts/flow_engine_capture_fixture.py --forma v3 --check-privacy <ruta>
//
// Datos: el armado de validate_flow_engine_capture_v3.mjs (el manifiesto de
// ATT1, el binding, el formulario, el carrito capturado y el scope
// att1-recuperacion v6 y v7 de tres toques) y los rechazos capturados de
// tests/fixtures/hotmart_purchase_canceled_att1_20261019.json. Desviaciones de
// produccion: los delays de A2 y Z son de 1 y 2 segundos; la ventana es de
// 00:00 a 23:59; la gracia es 0; el scope del carrito acepta tambien el pago
// rechazado (asi era att1-recuperacion antes del 19/10), para probar que la
// guarda va por legacy_kind y no por scope.
import { PGlite } from '@electric-sql/pglite';
import { randomBytes } from 'node:crypto';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
let OUT = null;
if (args.length === 2 && args[0] === '--salida') {
  [, OUT] = args;
} else if (args.length !== 0) {
  console.error('uso: node validate_flow_engine_v1.mjs [--salida <ruta>]');
  process.exit(2);
}

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const fixture = (path) => JSON.parse(readFileSync(join(root, 'tests/fixtures', path), 'utf8'));
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

const results = {};
const fail = (message, detail) => {
  throw new Error(`${message}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`);
};
const one = (rows, label) => {
  if (rows.length !== 1) fail(`${label}: se esperaba una fila y hubo ${rows.length}`);
  return rows[0];
};
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
const json = (value) => (value === null || value === undefined ? null : JSON.stringify(value));
const sameSet = (left, right) => JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
// Un error esperado, dentro de una transaccion que se deshace.
const expectError = async (label, run, { code, message, detail } = {}) => {
  let error = null;
  await db.exec('begin');
  try {
    await run();
  } catch (caught) {
    error = caught;
  } finally {
    await db.exec('rollback');
  }
  if (error === null
      || (code !== undefined && error.code !== code)
      || (message !== undefined && error.message !== message)
      || (detail !== undefined && error.detail !== detail)) {
    fail(`${label}: se esperaba ${code} ${message} ${detail ?? ''}`,
      error ? { code: error.code, message: error.message, detail: error.detail } : 'paso');
  }
  return error;
};

// ---------------------------------------------------------------------------
// 0. La migracion.
// ---------------------------------------------------------------------------
const TARGET = '20261021000200_flow_engine_routes_v1.sql';
const migrationNames = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const targetIndex = migrationNames.indexOf(TARGET);
if (targetIndex < 0) fail(`${TARGET} no esta en la cadena`);
const apply = async (file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
));
const snapshot = async () => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x,
         coalesce(has_function_privilege(to_regrole('automation_expert_change'), p.oid, 'execute'), false) as agent_x
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const tables = async () => (await db.query(`
  select c.relname from pg_class c where c.relnamespace = 'public'::regnamespace and c.relkind = 'r'
`)).rows.map((row) => row.relname);

await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of migrationNames.slice(0, targetIndex)) await apply(join(root, 'supabase/migrations', name));
const before = await snapshot();
const tablesBefore = await tables();
// Otro duenio en una de las siete: la migracion aborta sin cambiar nada.
await db.exec('create role otro_duenio nologin');
{
  let error = null;
  await db.exec('begin');
  try {
    await db.exec(`alter function public.mark_portable_payment_slip_request_started(${'uuid,uuid,text,bigint,timestamptz'}) owner to otro_duenio`);
    await apply(join(root, 'supabase/migrations', TARGET));
  } catch (caught) {
    error = caught;
  } finally {
    await db.exec('rollback');
  }
  if (error === null || error.code !== '55000' || error.message !== 'flow_engine_guard_owner_mismatch') {
    fail('otro duenio en una funcion guardada', error ? { code: error.code, message: error.message } : 'paso');
  }
  if (await tables().then((names) => names.includes('flow_engine_routes'))) fail('el aborto por el duenio dejo tablas');
  results.duenio = `${error.code}:${error.message}`;
}
await apply(join(root, 'supabase/migrations', TARGET));
const after = await snapshot();
const tablesAfter = await tables();
for (const name of migrationNames.slice(targetIndex + 1)) await apply(join(root, 'supabase/migrations', name));

const TZ = 'timestamp with time zone';
const READS = ['read_flow_engine_v1(jsonb,text)', `read_flow_engine_history_v1(jsonb,text,${TZ})`];
const ADDED = [
  ...READS,
  '_flow_engine_kind_matches(text,jsonb)', '_flow_engine_route_kinds()',
  '_flow_engine_legacy_scope_exists(text,text)', '_flow_engine_route_json(flow_engine_routes)',
  '_flow_engine_sequence_reason(text)', '_flow_engine_legacy_in_flight(text,text)',
  '_flow_engine_legacy_steps(text)', '_flow_engine_revalidation_is_valid(jsonb,integer,text)',
  '_flow_engine_duration_minutes(jsonb)', '_flow_engine_hour_minutes(jsonb)',
  '_flow_engine_compile_policy_core(jsonb,text,integer,text,text)', '_flow_engine_fixed_zone(uuid)',
  '_flow_engine_compile_policy(jsonb,uuid,integer,text)', 'reject_flow_engine_mutation()',
  'validate_flow_engine_route_insert()', 'validate_flow_engine_route_update()',
  'record_flow_engine_route_change()', '_flow_engine_legacy_guard(text,text)',
  '_flow_engine_legacy_guard_action(uuid,uuid,text)',
  'operator_create_flow_route_v1(jsonb,text,text,text,text,text,text)',
  'operator_route_flow_v1(jsonb,text,text,text,bigint,text,text,jsonb)',
];
const PLAN_ARGS = `uuid,uuid,text,text,text,text,integer,${TZ},bigint,bigint,text,text,integer`;
const MARK_ARGS = `uuid,uuid,text,bigint,${TZ}`;
const GUARDED = {
  [`plan_lancemos_pilot_cart_recovery(${PLAN_ARGS})`]: 'carrito',
  [`plan_portable_payment_failure_recovery(${PLAN_ARGS})`]: 'pago_fallido',
  [`plan_portable_payment_slip_recovery(${PLAN_ARGS})`]: 'ficha',
  [`mark_lancemos_pilot_request_started(${MARK_ARGS})`]: 'carrito',
  [`mark_portable_payment_failure_request_started(${MARK_ARGS})`]: 'pago_fallido',
  [`mark_portable_payment_slip_request_started(${MARK_ARGS})`]: 'ficha',
};
const AUTHORIZE = `authorize_lancemos_pilot_request_start(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid,uuid,uuid,${TZ})`;
const added = [...after.keys()].filter((signature) => !before.has(signature));
const removed = [...before.keys()].filter((signature) => !after.has(signature));
const changed = [...before.keys()].filter((signature) => after.has(signature)
  && ['definition', 'service_x', 'api_x', 'agent_x'].some((key) => after.get(signature)[key] !== before.get(signature)[key]));
const addedTables = tablesAfter.filter((name) => !tablesBefore.includes(name));
const TABLES = ['flow_engine_routes', 'flow_engine_runs', 'flow_engine_request_authorizations',
  'flow_engine_start_rejections', 'flow_engine_divergences'];
if (!sameSet(added, ADDED) || removed.length !== 0 || !sameSet(changed, [...Object.keys(GUARDED), AUTHORIZE])
    || !sameSet(addedTables, TABLES)) {
  fail('la migracion cambio otra cosa', { added, removed, changed, addedTables });
}
for (const signature of added) {
  const row = after.get(signature);
  const isRead = READS.includes(signature);
  if (row.api_x || row.service_x !== isRead || row.agent_x !== isRead) fail(`${signature}: permisos`, row);
}
// Cada funcion guardada es su definicion vigente con el bloque de la guarda
// insertado despues de la validacion de sus parametros, y nada mas.
const GUARD_BLOCK = /\n {4}-- flow_engine_legacy_guard: begin\n[\s\S]*? {4}-- flow_engine_legacy_guard: end\n/;
for (const [signature, kind] of Object.entries(GUARDED)) {
  const previous = before.get(signature).definition;
  const current = after.get(signature).definition;
  const block = current.match(GUARD_BLOCK);
  const anchor = signature.startsWith('plan_') ? 'invalid_pilot_plan_parameters' : 'invalid_pilot_request_start_parameters';
  const helper = signature.startsWith('plan_')
    ? `public._flow_engine_legacy_guard(p_scope_key, '${kind}')`
    : `public._flow_engine_legacy_guard_action(p_action_id, p_attempt_id, '${kind}')`;
  if (block === null || current.replace(GUARD_BLOCK, '') !== previous
      || !block[0].includes(helper)
      || current.indexOf(block[0]) !== current.indexOf(`message = '${anchor}';\n    end if;\n`) + `message = '${anchor}';\n    end if;\n`.length
      || after.get(signature).service_x !== before.get(signature).service_x) {
    fail(`${signature} no es su definicion vigente con la guarda insertada`);
  }
}
{
  // La bolsa: el total y el dia, y nada mas.
  const previous = before.get(AUTHORIZE).definition;
  const current = after.get(AUTHORIZE).definition;
  const total = '    select count(*)::integer into v_total\n';
  const daily = '      and authrow.local_budget_date = v_local_date;\n';
  const begin = current.indexOf('    -- flow_engine_shared_pool: begin\n');
  const endMarker = '    -- flow_engine_shared_pool: end\n';
  const end = current.indexOf(endMarker) + endMarker.length;
  const oldStart = previous.indexOf(total);
  const oldEnd = previous.indexOf(daily) + daily.length;
  const middle = previous.slice(previous.indexOf('    if v_total >='), previous.indexOf('    select count(*)::integer into v_daily'));
  if (begin < 0 || oldStart < 0 || current.slice(0, begin) !== previous.slice(0, oldStart)
      || current.slice(end) !== previous.slice(oldEnd) || !current.slice(begin, end).includes(middle)
      || (current.slice(begin, end).match(/engine_row\.pool = p_scope_key/g) || []).length !== 2) {
    fail('authorize_lancemos_pilot_request_start no es su definicion vigente con la bolsa de los dos caminos');
  }
}
const tableAcl = (await db.query(`
  select c.relname, c.relrowsecurity as rls,
         bool_or(has_table_privilege(r, c.oid, 'SELECT') or has_table_privilege(r, c.oid, 'INSERT')
                 or has_table_privilege(r, c.oid, 'UPDATE') or has_table_privilege(r, c.oid, 'DELETE')) as open
  from pg_class c cross join unnest(array['anon', 'authenticated', 'service_role']) as r
  where c.relnamespace = 'public'::regnamespace and c.relname = any($1)
  group by c.relname, c.relrowsecurity
`, [TABLES])).rows;
if (tableAcl.length !== 5 || tableAcl.some((row) => !row.rls || row.open)) fail('las tablas no son de nadie de la API', tableAcl);
{
  const error = await expectError('correr la migracion dos veces', () => apply(join(root, 'supabase/migrations', TARGET)),
    { code: '55000', message: 'flow_engine_routes_already_applied' });
  results.migracion = { agregadas: added.length, cambiadas: changed.length, tablas: addedTables.length,
    segunda: `${error.code}:${error.message}` };
}

// ---------------------------------------------------------------------------
// 1. compile_cases.json contra la gemela de SQL.
// ---------------------------------------------------------------------------
const canonical = (value) => JSON.stringify(value, (key, child) => (
  child && typeof child === 'object' && !Array.isArray(child)
    ? Object.fromEntries(Object.entries(child).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
    : child));
const COMPILED = `
  select p.policy_key, p.version, p.purpose, p.timezone, p.business_windows,
         extract(epoch from p.grace_period)::bigint as grace_period_seconds,
         extract(epoch from p.expires_after)::bigint as expires_after_seconds,
         p.max_automatic_messages, p.steps`;
const compileCases = fixture('flow_engine/compile_cases.json').cases;
{
  const differ = [];
  for (const item of compileCases) {
    const input = item.input;
    let got;
    try {
      got = one((await db.query(`${COMPILED}
        from public._flow_engine_compile_policy_core($1::jsonb, $2, $3, $4, $5) p
      `, [json(input.definition), input.flow_id, input.version, input.legacy_kind, input.fixed_zone])).rows, item.id);
    } catch (error) {
      got = { error: error.message };
    }
    const want = item.error !== undefined ? { error: item.error } : item.expected;
    if (canonical(got) !== canonical(want)) differ.push({ id: item.id, sql: got, python: want });
  }
  if (differ.length !== 0) fail('compile_cases.json da distinto en SQL y en Python', differ);
  results.compile_cases = compileCases.length;
}

// ---------------------------------------------------------------------------
// El armado de ATT1 (copiado de validate_flow_engine_capture_v3.mjs).
// ---------------------------------------------------------------------------
const readManifest = (text) => {
  const manifest = {};
  const walk = (path) => path.reduce((node, key) => (node[key] ??= {}), manifest);
  let table = manifest;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    let match = line.match(/^\[\[([a-z_.]+)\]\]$/);
    if (match) {
      const path = match[1].split('.');
      const parent = walk(path.slice(0, -1));
      table = {};
      (parent[path.at(-1)] ??= []).push(table);
      continue;
    }
    match = line.match(/^\[([a-z_.]+)\]$/);
    if (match) {
      table = walk(match[1].split('.'));
      continue;
    }
    match = line.match(/^([a-z_]+)\s*=\s*(?:"([^"]*)"|(-?\d+)|(true|false))\s*(?:#.*)?$/);
    if (match) table[match[1]] = match[2] ?? (match[3] !== undefined ? Number(match[3]) : match[4] === 'true');
  }
  return manifest;
};
const required = (value, label) => {
  if (value === undefined || value === null || value === '') fail(`al fixture de ATT1 le falta ${label}`);
  return value;
};
const manifest = readManifest(readFileSync(join(root, 'tests/fixtures/instances/att1/instancia.toml'), 'utf8'));
const offers = required(manifest.hotmart?.ofertas, 'hotmart.ofertas').map((offer) => {
  const url = new URL(required(offer.url, 'ofertas.url'));
  return {
    offer_code: required(offer.codigo, 'ofertas.codigo'), site: required(offer.site, 'ofertas.site'),
    landing_id: required(offer.landing_id, 'ofertas.landing_id'), page_host: url.host, page_path: url.pathname,
  };
});
const ATT1 = {
  tenant: required(manifest.instancia?.tenant_ref, 'instancia.tenant_ref'),
  funnel: required(manifest.instancia?.funnel_ref, 'instancia.funnel_ref'),
  ally: required(manifest.instancia?.ally_ref, 'instancia.ally_ref'),
  bindingVersion: required(manifest.instancia?.binding_version, 'instancia.binding_version'),
  brand: required(manifest.instancia?.marca, 'instancia.marca'),
  timezone: required(manifest.instancia?.zona_horaria, 'instancia.zona_horaria'),
  productId: required(manifest.hotmart?.product_id, 'hotmart.product_id'),
  hotlink: required(manifest.hotmart?.hotlink, 'hotmart.hotlink'),
  productName: required(manifest.hotmart?.product_name, 'hotmart.product_name'),
  currency: required(manifest.hotmart?.moneda, 'hotmart.moneda'),
  price: required(manifest.hotmart?.precio, 'hotmart.precio'),
  accountId: required(manifest.chatwoot?.account_id, 'chatwoot.account_id'),
  inboxId: required(manifest.chatwoot?.inbox_id, 'chatwoot.inbox_id'),
  inboundScope: required(manifest.inbound?.scope_key, 'inbound.scope_key'),
  inboundVersion: required(manifest.inbound?.scope_version, 'inbound.scope_version'),
  copyVersion: required(manifest.consentimiento?.copy_version, 'consentimiento.copy_version'),
};
const INSTANCE = { tenant_ref: ATT1.tenant, ally_ref: ATT1.ally, funnel_ref: ATT1.funnel };
const PRODUCT = ATT1.funnel;
const [defaultOffer, ...additionalOffers] = offers;
const offerByCode = (code) => offers.find((offer) => offer.offer_code === code) ?? fail(`oferta ${code}`);
const PILOT = fixture('instances/att1/politica-piloto.json');
const CHANNEL_PROVIDER = required(PILOT.pilot_scope?.channel_provider, 'pilot_scope.channel_provider');
const CHANNEL_REF = `${required(PILOT.pilot_scope?.channel_account_ref_prefix, 'pilot_scope.channel_account_ref_prefix')}${ATT1.inboxId}`;
const ALL_DAY = required(PILOT.policy?.business_windows, 'policy.business_windows')
  .map((window) => ({ ...window, start: '00:00', end: '23:59' }));
const OPERATOR = 'operador-motor-prueba';
const OPERATOR_REASON = 'razon-del-operador-del-motor';
const SCOPE = 'att1-recuperacion';
const ONE_TOUCH = { policy: 'att1-recuperacion-un-toque', steps: [{ step_key: 'first_contact', mode: 'freeform' }], max: 1 };
const THREE_TOUCHES = {
  policy: 'att1-carrito-tres-toques',
  steps: [
    { step_key: 'first_contact', mode: 'freeform' },
    { step_key: 'cart_second_touch', mode: 'freeform', delay: '1 second', approved_template: true },
    { step_key: 'cart_last_touch', mode: 'freeform', delay: '2 seconds', approved_template: true, coupon_ordinal: 2 },
  ],
  max: 3,
};
const FLOW = { version: 7, policy: THREE_TOUCHES.policy };

await db.query(`
  insert into public.commercial_ally_runtime_bindings
    (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
     lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
     product_name, product_price, currency, offer_code, consent_copy_version,
     hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
     inbound_scope_key, inbound_scope_version, additional_offer_codes,
     additional_offer_landings)
  values ($1,$2,$3,'active',$4,$5,$6,$7,$8,$9,$10,$11,$12::numeric,$13,$14,$15,
          $16,$17,$18,$19,$20,$21::text[],$22::jsonb)
`, [
  ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, ATT1.ally, ATT1.brand,
  defaultOffer.site, defaultOffer.landing_id, defaultOffer.page_host, defaultOffer.page_path,
  ATT1.hotlink, ATT1.productName, ATT1.price, ATT1.currency, defaultOffer.offer_code, ATT1.copyVersion,
  ATT1.productId, ATT1.accountId, ATT1.inboxId, ATT1.inboundScope, ATT1.inboundVersion,
  additionalOffers.map((offer) => offer.offer_code),
  JSON.stringify(additionalOffers.map((offer) => ({
    offer_code: offer.offer_code, site: offer.site, landing_id: offer.landing_id,
    page_host: offer.page_host, page_path: offer.page_path,
  }))),
]);
for (const offer of offers) {
  await db.query(`
    insert into public.hotmart_purchase_intent_scopes
      (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref, offer_ref, max_lookback, active)
    values ($1,$2,$3,$4,$5,$6::interval,true)
  `, [ATT1.tenant, ATT1.funnel, String(ATT1.productId), ATT1.hotlink, offer.offer_code,
    required(PILOT.purchase_intent_scope?.max_lookback, 'purchase_intent_scope.max_lookback')]);
  await db.query(`
    insert into public.checkout_offer_catalog (
      tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref, offer_code,
      checkout_base_url, checkout_mode, default_for_inbound, status, version, approved_by, approved_at
    ) values ($1,$2,$3,$4,$5,$6,$7,$8,10,$9,'active',1,$10,now())
  `, [ATT1.tenant, ATT1.funnel, ATT1.inboundScope, ATT1.inboundVersion, ATT1.hotlink,
    offer.landing_id, offer.offer_code, `https://pay.hotmart.com/${ATT1.hotlink}`, offer === defaultOffer, OPERATOR]);
}
await db.query(`
  insert into public.commercial_ally_hotmart_purchase_policies
    (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
  values ($1,$2,$3,true,$4::interval)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, required(PILOT.purchase_policy?.max_lookback, 'purchase_policy.max_lookback')]);
await db.query(`
  insert into public.inbound_commercial_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     external_product_id, offer_code, approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5,$6,$7,$8,now(),now())
`, [ATT1.inboundScope, ATT1.inboundVersion, ATT1.tenant, ATT1.accountId, ATT1.inboxId,
  ATT1.hotlink, defaultOffer.offer_code, OPERATOR]);
for (const policy of [ONE_TOUCH, THREE_TOUCHES]) {
  await db.query(`
    insert into public.followup_policy_versions
      (policy_key, version, status, purpose, timezone, business_windows, grace_period, expires_after,
       max_automatic_messages, steps, approved_by, approved_at, published_at)
    values ($1,1,'published','cart_recovery',$2,$3::jsonb,interval '0 seconds',interval '5 days',$4,$5::jsonb,$6,now(),now())
  `, [policy.policy, ATT1.timezone, JSON.stringify(ALL_DAY), policy.max, JSON.stringify(policy.steps), OPERATOR]);
}
for (const [version, policy] of [[6, ONE_TOUCH], [7, THREE_TOUCHES]]) {
  await db.query(`
    insert into public.pilot_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
       channel, channel_provider, channel_account_ref, source, source_event_type,
       additional_source_event_types, external_product_id, offer_code,
       additional_offer_codes, purpose, policy_key, policy_version, timezone,
       max_cohort_contacts, max_outbound_request_starts_total,
       max_outbound_request_starts_per_day, approved_by, approved_at, published_at)
    values ($1,$2,'published',$3,$4,$5,'whatsapp',$6,$7,'hotmart',
            'PURCHASE_OUT_OF_SHOPPING_CART',array['PURCHASE_CANCELED'],$8,$9,$10::text[],
            'cart_recovery',$11,1,$12,20,100,50,$13,now(),now())
  `, [SCOPE, version, ATT1.tenant, ATT1.accountId, ATT1.inboxId, CHANNEL_PROVIDER, CHANNEL_REF,
    String(ATT1.productId), defaultOffer.offer_code, additionalOffers.map((offer) => offer.offer_code),
    policy.policy, ATT1.timezone, OPERATOR]);
}
await db.query(`
  insert into public.pilot_runtime_controls (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
  values ($1,6,'inactive',0,$2,$3)
`, [SCOPE, OPERATOR, OPERATOR_REASON]);
const generationOf = async () => one((await db.query(
  'select generation from public.pilot_runtime_controls where scope_key = $1', [SCOPE])).rows, 'generacion').generation;
const setState = async (version, state) => db.query(
  'select * from public.set_lancemos_pilot_runtime_state($1,$2,$3,$4,$5,$6)',
  [SCOPE, version, await generationOf(), state, OPERATOR, OPERATOR_REASON]);
await setState(6, 'armed');
await setState(6, 'paused');
await db.query('select * from public.activate_lancemos_pilot_scope_version($1,7,$2,$3,$4)',
  [SCOPE, await generationOf(), OPERATOR, OPERATOR_REASON]);
await sleep(20);
await setState(7, 'armed');
const activation = one((await db.query(`
  select min(created_at) as at from public.pilot_control_events
  where scope_key = $1 and event_type = 'pilot_scope_version_activated' and scope_version >= 7
`, [SCOPE])).rows, 'activacion').at;

// Las personas, el formulario, el carrito y el plan.
const dbNow = async () => new Date((await db.query('select clock_timestamp() as now')).rows[0].now);
const baseMs = Math.floor((await dbNow()).getTime() / 1000) * 1000;
const at = (minutes) => new Date(baseMs + minutes * 60_000);
const digits = (value) => String(value ?? '').replace(/\D/g, '');
const CAPTURED_CART = fixture('hotmart_cart_abandonment_rejected_v1.json').payload;
const REFUSALS = fixture('hotmart_purchase_canceled_att1_20261019.json').eventos;
let personIndex = 0;
const person = (label) => {
  personIndex += 1;
  const suffix = String(personIndex).padStart(2, '0');
  return {
    label, name: `Compradora Motor ${label}`, email: `motor-${suffix}@example.test`,
    phone: `120255504${suffix}`, country: '1', iso: 'US', offer: additionalOffers[0],
    chatwoot: String(940000 + personIndex),
  };
};
const admitForm = async (lead, submittedAt) => {
  const { offer } = lead;
  const id = `motor-form-${lead.email}-${offer.offer_code}`;
  const pageUrl = `https://${offer.page_host}${offer.page_path}`;
  const checkoutUrl = `https://pay.hotmart.com/${ATT1.hotlink}?off=${offer.offer_code}&checkoutMode=10`;
  const raw = {
    id, event: 'lead.precheckout', version: '1.1.0', created_at: submittedAt.toISOString(),
    source: { system: 'landing', site: offer.site, aliado: ATT1.brand, landing_id: offer.landing_id, page_url: pageUrl },
    data: {
      buyer: {
        name: lead.name, email: lead.email, phone: `+${lead.phone}`,
        phone_country_code: lead.country, phone_national: lead.phone.slice(lead.country.length),
      },
      product: { hotlink: ATT1.hotlink, id: null, name: ATT1.productName, price: Number(ATT1.price), currency: ATT1.currency },
      offer: { code: offer.offer_code },
      checkout_url: checkoutUrl,
      checkout_country: { iso: lead.iso, source: 'phone_country_code' },
      consent: { marketing_optin: true, whatsapp_contact: true, copy_version: ATT1.copyVersion },
    },
    dedupe_key: `${offer.site}:${offer.offer_code}:${lead.email}`,
  };
  const canonicalForm = {
    external_submission_id: id, event_type: 'PRECHECKOUT_FORM_SUBMITTED', contract_version: '1.1.0',
    submitted_at: raw.created_at,
    source: { tenant_ref: ATT1.tenant, funnel_ref: ATT1.funnel, landing_ref: offer.landing_id, page_url: pageUrl, aliado: ATT1.brand },
    identity: { email: lead.email, phone: lead.phone, phone_valid: true, phone_country_iso: lead.iso },
    lead: { full_name: lead.name },
    commerce: {
      product_ref: ATT1.hotlink, product_name: ATT1.productName, offer_ref: offer.offer_code,
      price: String(ATT1.price), currency: ATT1.currency, checkout_url: checkoutUrl,
    },
    dedupe_key: raw.dedupe_key,
    consent: {
      terms_accepted: false, privacy_accepted: false, marketing_optin: true, whatsapp_contact: true,
      copy_version: ATT1.copyVersion,
    },
    assurance: { provisional: false, provider_observed: true, activation_authorized: true },
  };
  const admitted = one((await db.query(`
    select * from public.admit_portable_observed_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, id, JSON.stringify(raw), JSON.stringify(canonicalForm)])).rows,
  `${lead.label} formulario`);
  if (admitted.outcome !== 'inserted') fail(`${lead.label} formulario`, admitted.outcome);
};
const contactFor = async (lead, eventId) => {
  lead.contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone, country_iso) values ($1,$2,$3,'MX') returning id
  `, [lead.name, lead.email, lead.phone])).rows, `${lead.label} contacto`).id;
  await db.query(`
    insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id)
    values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)
  `, [lead.contact, lead.email, lead.phone, eventId]);
  const member = one((await db.query(`
    select * from public.set_lancemos_pilot_cohort_member($1,$2,$3,$4,'active',$5,$6)
  `, [SCOPE, FLOW.version, lead.contact, await generationOf(), OPERATOR, OPERATOR_REASON])).rows, `${lead.label} cohorte`);
  if (member.member_status !== 'active') fail(`${lead.label} cohorte`, member);
};
const PLAN_CART = 'select * from public.plan_lancemos_pilot_cart_recovery($1,$2,$3,$4,$5,$6,1,$7,$8,$9,$10,$11,$12)';
const cartFor = async (label) => {
  const lead = person(label);
  await admitForm(lead, at(-50));
  const payload = structuredClone(CAPTURED_CART);
  payload.id = `motor-cart-${lead.email}`;
  payload.creation_date = at(-30).getTime();
  payload.data.product = { id: ATT1.productId, name: ATT1.productName };
  payload.data.offer = { code: lead.offer.offer_code };
  payload.data.buyer = { name: lead.name, email: lead.email, phone: lead.phone };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone])).rows,
  `${label} carrito`);
  if (admitted.outcome !== 'inserted') fail(`${label} carrito`, admitted.outcome);
  await contactFor(lead, admitted.webhook_event_id);
  lead.cartArgs = [admitted.webhook_event_id, lead.contact, String(ATT1.productId), ATT1.productName,
    lead.offer.offer_code, FLOW.policy, at(-30).toISOString(), ATT1.accountId, ATT1.inboxId, lead.phone,
    SCOPE, FLOW.version];
  return lead;
};
const planCart = async (lead) => {
  lead.plan = one((await db.query(PLAN_CART, lead.cartArgs)).rows, `${lead.label} plan`);
  if (!lead.plan.created) fail(`${lead.label} plan`, lead.plan);
  return lead.plan;
};
// El pago rechazado capturado, en el mismo scope del carrito.
const refusalFor = async (personaNumber, label) => {
  const payload = structuredClone(REFUSALS.find((event) => event.persona === personaNumber).payload);
  const createdAt = at(-20);
  payload.creation_date = createdAt.getTime();
  const lead = {
    label, name: payload.data.buyer.name, email: payload.data.buyer.email.toLowerCase(),
    phone: digits(payload.data.buyer.checkout_phone), country: '52', iso: 'MX',
    offer: offerByCode(payload.data.purchase.offer.code), chatwoot: String(950000 + personaNumber),
  };
  await admitForm(lead, at(-40));
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone])).rows,
  `${label} rechazo`);
  if (admitted.outcome !== 'inserted') fail(`${label} rechazo`, admitted.outcome);
  await contactFor(lead, admitted.webhook_event_id);
  lead.refusalArgs = [admitted.webhook_event_id, lead.contact, String(ATT1.productId), ATT1.productName,
    payload.data.purchase.offer.code, FLOW.policy, 1, createdAt.toISOString(), ATT1.accountId, ATT1.inboxId,
    lead.phone, SCOPE, FLOW.version];
  return lead;
};
const PLAN_REFUSAL = 'select * from public.plan_portable_payment_failure_recovery($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)';

// El despacho de un paso, como en la captura v3.
// El claim toma todo lo vencido: lo demas que vence se corre a manana.
const claim = async (label, actionId) => {
  await db.query(`
    update public.scheduled_actions set due_at = now() + interval '1 day'
     where id <> $1 and status = 'pending' and due_at <= now() + interval '1 minute'
  `, [actionId]);
  const worker = `motor-worker-${label}`;
  const now = await dbNow();
  const claimed = (await db.query(
    "select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)", [worker, now])).rows;
  if (claimed.length !== 1 || claimed[0].id !== actionId) fail(`${label}: el claim tomo otra cosa`, claimed.map((row) => row.step_key));
  return { worker, now, lease: claimed[0].lease_generation };
};
const evidence = (lead, now) => ({
  p_chatwoot_checked: true, p_chatwoot_conversation_id: lead.chatwoot, p_chatwoot_checkpoint_message_id: '1',
  p_chatwoot_checkpoint_at: now.toISOString(), p_chatwoot_status: 'open', p_chatwoot_can_reply: false,
  p_chatwoot_anchor_found: true, p_chatwoot_automation_paused: false, p_chatwoot_inbound_after_anchor: false,
  p_chatwoot_human_activity_after_anchor: false,
});
const reevaluate = async (actionId, fence, chatwoot) => {
  if (!chatwoot) {
    return one((await db.query('select * from public.reevaluate_followup_action($1,$2,$3,$4)',
      [actionId, fence.worker, fence.lease, fence.now])).rows, 'reevaluacion');
  }
  const names = Object.keys(chatwoot);
  const list = names.map((name, index) => `${name} => $${index + 5}`).join(', ');
  return one((await db.query(`
    select * from public.reevaluate_followup_action(p_action_id => $1, p_worker_id => $2, p_lease_generation => $3,
      p_now => $4, ${list})
  `, [actionId, fence.worker, fence.lease, fence.now, ...names.map((name) => chatwoot[name])])).rows, 'reevaluacion');
};
const MARK_CART = 'select * from public.mark_lancemos_pilot_request_started($1,$2,$3,$4,$5)';
// Hasta el arranque: claim, reevaluacion e intento reservado.
const reserve = async (lead, actionId, chatwoot) => {
  const fence = await claim(lead.label, actionId);
  const decision = await reevaluate(actionId, fence, chatwoot ? chatwoot(fence.now) : null);
  if (decision.decision !== 'execute') fail(`${lead.label} reevaluacion`, decision);
  const attempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
  `, [actionId, fence.worker, fence.lease, decision.case_version, decision.sequence_revision, fence.now])).rows, 'intento');
  return { ...fence, attempt, actionId };
};
let messageNumber = 0;
const startAndAccept = async (lead, reserved) => {
  const started = one((await db.query(MARK_CART,
    [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, await dbNow()])).rows, `${lead.label} arranque`);
  if (started.phase !== 'request_started') fail(`${lead.label} arranque`, started);
  messageNumber += 1;
  const accepted = one((await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
  `, [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, lead.chatwoot,
    `motor-wamid-${messageNumber}`, `Texto sembrado ${messageNumber}`, await dbNow()])).rows, `${lead.label} aceptacion`);
  if (accepted.status !== 'accepted_by_chatwoot') fail(`${lead.label} aceptacion`, accepted);
  return started;
};
const stepOf = async (lead, stepKey) => one((await db.query(
  'select * from public.scheduled_actions where recovery_case_id = $1 and step_key = $2',
  [lead.plan.recovery_case_id, stepKey])).rows, `${lead.label} ${stepKey}`);
const sleepUntil = async (date) => {
  const wait = new Date(date).getTime() - (await dbNow()).getTime() + 150;
  if (wait > 0) await sleep(wait);
};

// Sin rutas, el carrito de hoy: A1 aceptado y A2 esperando.
const espera = await cartFor('espera');
await planCart(espera);
await startAndAccept(espera, await reserve(espera, espera.plan.scheduled_action_id));
espera.a2 = await stepOf(espera, 'cart_second_touch');
if (espera.a2.status !== 'pending') fail('A2 de «espera»', espera.a2.status);

// ---------------------------------------------------------------------------
// El almacen: el carrito y el pago fallido de att1, publicados y prendidos, y
// el primer contacto registrado sin publicar.
// ---------------------------------------------------------------------------
const VALIDATOR = {
  contract: 'setter-flujo/v1', rules_version: 1, phase: 'publish', actor: 'operador',
  product_version: 'v1.9.0', git_sha: 'abcdef0',
};
const CLEAN = { blocks: [], warnings: [] };
const rpc = async (fn, ...params) => {
  const marks = params.map((_, i) => `$${i + 1}`).join(', ');
  return JSON.parse(one((await db.query(`select public.${fn}(${marks})::text as r`,
    params.map((p) => (p !== null && typeof p === 'object' ? JSON.stringify(p) : p)))).rows, fn).r);
};
const register = async (flowKey) => {
  const registered = await rpc('operator_register_flow_v1', INSTANCE, PRODUCT, flowKey, 'dan', 'alta del flujo');
  if (!registered.ok) fail(`registrar ${flowKey}`, registered);
  return registered.flow_id;
};
const draftBase = async (flowIds) => (await db.query(`
  select coalesce(jsonb_object_agg(flow_id::text, jsonb_build_object('active_version', active_version)), '{}'::jsonb) b
    from public.flow_controls where flow_id = any($1::uuid[])`, [flowIds])).rows[0].b;
const instanceBase = async (flowIds) => (await db.query('select public._flow_store_base($1, $2, $3::uuid[]) b',
  [ATT1.tenant, ATT1.ally, flowIds])).rows[0].b;
const cycle = async (items, label) => {
  const flowIds = [...new Set(items.map((item) => item.flow_id))];
  const saved = await rpc('operator_save_flow_change_draft_v1', INSTANCE, null, null, items, await draftBase(flowIds),
    'que el motor tenga su ruta', 'dan');
  const validated = await rpc('operator_record_flow_change_validation_v1', INSTANCE, saved.proposal_id, saved.revision,
    await instanceBase(flowIds), VALIDATOR, CLEAN, {}, 'dan');
  const approved = await rpc('operator_approve_flow_change_v1', INSTANCE, saved.proposal_id, validated.fingerprint, 'dan');
  const published = await rpc('operator_publish_flow_change_v1', INSTANCE, saved.proposal_id, validated.fingerprint, {
    fingerprint: validated.fingerprint, validator: VALIDATOR, result: CLEAN, catalog_sha256: 'a'.repeat(64),
    validated_at: new Date().toISOString(),
  });
  if (!saved.ok || validated.status !== 'validada' || !approved.ok || !published.ok) {
    fail(`ciclo ${label}`, { saved, validated, approved, published });
  }
};
const definitionOf = (flujo, overrides = {}) => ({
  esquema: 'setter-flujo/v1', flujo, producto: PRODUCT, estado: 'borrador', nombre: `Flujo ${flujo}`,
  dispara: { evento: 'carrito' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ plantilla: 'att1_carrito_abandonado_01', idioma: 'es_MX', variables: ['nombre', 'producto'] }],
  termina: { si: ['compra'], vence: '1d' },
  horario: { zona: 'America/Mexico_City', desde: '09:00', hasta: '21:00' },
  topes: { bolsa: SCOPE, por_dia: 50, total: 50, repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  ...overrides,
});
const versionItem = (flowId, flujo, overrides) => ({
  producto: PRODUCT, flujo, flow_id: flowId, tipo: 'version', definicion: definitionOf(flujo, overrides), en_curso: 'seguir',
});
const stateItem = (flowId, flujo, tipo) => ({ producto: PRODUCT, flujo, flow_id: flowId, tipo });
const FLOWS = {
  carrito: await register('carrito'),
  pago_fallido: await register('pago_fallido'),
  precheckout: await register('precheckout'),
};
await cycle([versionItem(FLOWS.carrito, 'carrito'), stateItem(FLOWS.carrito, 'carrito', 'prender'),
  versionItem(FLOWS.pago_fallido, 'pago_fallido', { dispara: { evento: 'pago_fallido' } }),
  stateItem(FLOWS.pago_fallido, 'pago_fallido', 'prender')], 'v1 del carrito y del pago fallido');
const control = async (flowId) => one((await db.query(
  'select state, active_version, generation::int as generation from public.flow_controls where flow_id = $1', [flowId])).rows, 'control');
if ((await control(FLOWS.carrito)).state !== 'prendido' || (await control(FLOWS.pago_fallido)).state !== 'prendido') {
  fail('los flujos no quedaron prendidos');
}
const pauseFlow = async (flowKey) => {
  const paused = await rpc('operator_pause_flow_v1', INSTANCE, PRODUCT, flowKey,
    (await control(FLOWS[flowKey])).generation, 'dan', 'pausa de prueba');
  if (!paused.ok || !paused.changed) fail(`pausar ${flowKey}`, paused);
};
const turnOn = async (flowKey, tipo = 'prender') => cycle([stateItem(FLOWS[flowKey], flowKey, tipo)], `${tipo} ${flowKey}`);

// La gemela con la zona fija del almacen: un siempre del carrito toma la de
// su v1 (CDMX); el primer contacto, sin versiones, toma UTC.
{
  const always = definitionOf('carrito', { horario: 'siempre' });
  const zone = async (flowId) => one((await db.query(
    'select p.timezone from public._flow_engine_compile_policy($1::jsonb, $2, 2, $3) p',
    [json(always), flowId, 'carrito'])).rows, 'zona fija').timezone;
  if (await zone(FLOWS.carrito) !== 'America/Mexico_City' || await zone(FLOWS.precheckout) !== 'UTC') {
    fail('la zona fija del almacen');
  }
}

// ---------------------------------------------------------------------------
// 2. Crear la ruta.
// ---------------------------------------------------------------------------
const createRoute = (flowKey, kind, scope) => rpc('operator_create_flow_route_v1', INSTANCE, PRODUCT, flowKey, kind, scope,
  'dan', 'la ruta del flujo');
const reasonOf = async (promise) => (await promise).reason_code;
const created = {};
{
  await expectError('flow_not_found', () => createRoute('no_existe', 'carrito', SCOPE), { code: 'P0002', message: 'flow_not_found' });
  await expectError('legacy_kind invalido', () => createRoute('carrito', 'compra', SCOPE), { code: '22023', message: 'flow_route_legacy_kind_invalid' });
  await expectError('cupon con scope', () => createRoute('carrito', 'descuento', SCOPE), { code: '22023', message: 'flow_route_legacy_scope_invalid' });
  await expectError('carrito sin scope', () => createRoute('carrito', 'carrito', null), { code: '22023', message: 'flow_route_legacy_scope_invalid' });
  const reds = {
    // En la v1, solo carrito, pago_fallido y ficha: los demas no tienen guarda.
    flow_route_kind_not_guarded: await reasonOf(createRoute('precheckout', 'precheckout', SCOPE)),
    flow_route_needs_published_version: await reasonOf(createRoute('precheckout', 'carrito', SCOPE)),
    flow_route_kind_mismatch: await reasonOf(createRoute('carrito', 'pago_fallido', SCOPE)),
    flow_route_legacy_scope_unknown: await reasonOf(createRoute('carrito', 'carrito', 'att1-no-existe')),
  };
  for (const [kind, scope] of [['descuento', null], ['reactivacion', null]]) {
    const got = await reasonOf(createRoute('carrito', kind, scope));
    if (got !== 'flow_route_kind_not_guarded') fail(`crear la ruta de ${kind}`, got);
  }
  created.carrito = await createRoute('carrito', 'carrito', SCOPE);
  reds.flow_route_already_exists = await reasonOf(createRoute('carrito', 'carrito', SCOPE));
  // Otro legacy_kind en el mismo scope: es otra ruta.
  created.pago_fallido = await createRoute('pago_fallido', 'pago_fallido', SCOPE);
  for (const [want, got] of Object.entries(reds)) if (want !== got) fail(`crear la ruta: ${want}`, got);
  const expected = { executor: 'legado', legacy_kind: 'carrito', legacy_scope_key: SCOPE, generation: 0 };
  if (!created.carrito.ok || canonical({ ...created.carrito.route, changed_at: undefined })
      !== canonical({ ...expected, changed_at: undefined }) || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$/.test(created.carrito.route.changed_at)
      || !created.pago_fallido.ok) {
    fail('crear la ruta', created);
  }
  const events = (await db.query(`
    select event_type, reason_code, actor_kind, actor_ref, generation::int as generation, data
      from public.flow_change_events where flow_id = $1 and event_type like 'flow_route%'
  `, [FLOWS.carrito])).rows;
  if (events.length !== 1 || events[0].event_type !== 'flow_route_created' || events[0].actor_ref !== 'dan'
      || events[0].generation !== 0 || events[0].data.to_executor !== 'legado' || events[0].data.legacy_kind !== 'carrito') {
    fail('el evento flow_route_created', events);
  }
  // Los disparadores: la ruta es permanente, su legado inmutable, nace en
  // legado y con el disparador de la version activa.
  await expectError('cambiar legacy_kind', () => db.query(
    "update public.flow_engine_routes set legacy_kind = 'ficha', generation = 1, executor = 'motor' where flow_id = $1",
    [FLOWS.carrito]), { code: '55000', message: 'flow_route_legacy_is_immutable' });
  await expectError('cambiar el scope', () => db.query(
    "update public.flow_engine_routes set legacy_scope_key = 'otro', generation = 1, executor = 'motor' where flow_id = $1",
    [FLOWS.carrito]), { code: '55000', message: 'flow_route_legacy_is_immutable' });
  await expectError('saltear la generacion', () => db.query(
    "update public.flow_engine_routes set generation = 5, executor = 'motor' where flow_id = $1",
    [FLOWS.carrito]), { code: '23514', message: 'flow_route_generation_must_increment' });
  await expectError('borrar la ruta', () => db.query('delete from public.flow_engine_routes where flow_id = $1',
    [FLOWS.carrito]), { code: '55000', message: 'flow_route_is_permanent' });
  await expectError('nacer en motor', () => db.query(`
    insert into public.flow_engine_routes (flow_id, executor, legacy_kind, legacy_scope_key, changed_by_ref, change_reason)
    values ($1, 'motor', 'precheckout', 'att1-otro', 'dan', 'directo')`, [FLOWS.precheckout]),
  { code: '23514', message: 'flow_route_must_start_in_legacy' });
  // Directo, sin la funcion: el disparador mira el disparador de la version
  // activa (un flujo del carrito no puede tener una ruta de pago_fallido).
  await expectError('una ruta directa con otro disparador', async () => {
    const otro = await register('otro_carrito');
    await cycle([versionItem(otro, 'otro_carrito')], 'v1 de otro_carrito');
    await db.query(`
      insert into public.flow_engine_routes (flow_id, legacy_kind, legacy_scope_key, changed_by_ref, change_reason)
      values ($1, 'pago_fallido', 'att1-otro', 'dan', 'directo')`, [otro]);
  }, { code: '23514', message: 'flow_route_kind_mismatch' });
  results.crear_la_ruta = Object.keys(reds).length + 1;
}

// ---------------------------------------------------------------------------
// 3. Mover la ruta.
// ---------------------------------------------------------------------------
const route = (flowKey, executor, generation, revalidation = null) => rpc('operator_route_flow_v1', INSTANCE, PRODUCT,
  flowKey, executor, generation, 'dan', 'mover la ruta', revalidation);
const activeSha = async (flowId) => one((await db.query(`
  select d.definition_sha256 from public.flow_definitions d join public.flow_controls c
    on c.flow_id = d.flow_id and c.active_version = d.version where d.flow_id = $1`, [flowId])).rows, 'sha').definition_sha256;
const revalidationFor = async (flowId, overrides = {}) => ({
  version: (await control(flowId)).active_version, definition_sha256: await activeSha(flowId), rules_version: 2,
  capabilities: 'v1', blocks: [],
  validator: { ...VALIDATOR, rules_version: 2 },
  ...overrides,
});
{
  await expectError('flow_route_not_found', () => route('precheckout', 'motor', 0), { code: 'P0002', message: 'flow_route_not_found' });
  await expectError('ejecutor invalido', () => route('carrito', 'otro', 0), { code: '22023', message: 'flow_route_executor_invalid' });
  const reds = {
    flow_route_generation_stale: await reasonOf(route('carrito', 'motor', 3)),
    // El rojo a proposito del contrato: el pago fallido no va a motor en la v1.
    flow_route_kind_not_supported: await reasonOf(route('pago_fallido', 'motor', 0, await revalidationFor(FLOWS.pago_fallido))),
  };
  // En la T2.3a, tampoco el carrito: el camino del motor llega con la T2.3b.
  const cartToEngine = await route('carrito', 'motor', 0, await revalidationFor(FLOWS.carrito));
  const unchanged = await route('carrito', 'legado', 0);
  for (const [want, got] of Object.entries(reds)) if (want !== got) fail(`mover la ruta: ${want}`, got);
  if (cartToEngine.reason_code !== 'flow_route_kind_not_supported' || !unchanged.ok || unchanged.unchanged !== true) {
    fail('mover la ruta en la T2.3a', { cartToEngine, unchanged });
  }

  // El motor de la T2.3b, simulado adentro de una transaccion que se deshace.
  await db.exec('begin');
  try {
    await db.exec(`create or replace function public._flow_engine_route_kinds() returns text[] language sql immutable
      security invoker set search_path = pg_catalog, pg_temp as $f$ select array['carrito']::text[] $f$`);
    const good = await revalidationFor(FLOWS.carrito);
    const invalid = [
      ['sin revalidacion', null],
      ['con una clave de mas', { ...good, fingerprint: 'a'.repeat(64) }],
      ['sin blocks', (({ blocks, ...rest }) => rest)(good)],
      ['con un bloqueo', { ...good, blocks: [{ rule: 14, code: 'engine_not_supported' }] }],
      ['de otra version', { ...good, version: 2 }],
      ['de otra huella', { ...good, definition_sha256: 'b'.repeat(64) }],
      ['con las reglas viejas', { ...good, rules_version: 1 }],
      ['con otras capacidades', { ...good, capabilities: 'preview' }],
      ['con un validador sin git_sha', { ...good, validator: (({ git_sha, ...rest }) => rest)(good.validator) }],
    ];
    for (const [label, value] of invalid) {
      const got = await route('carrito', 'motor', 0, value);
      if (got.reason_code !== 'flow_route_revalidation_invalid') fail(`revalidacion ${label}`, got);
    }
    reds.flow_route_revalidation_invalid = 'flow_route_revalidation_invalid';
    await db.query(`insert into public.pilot_proactive_contact_caps (tenant_key, max_request_starts, request_window, approved_by)
      values ($1, 2, interval '1 day', 'dan')`, [ATT1.tenant]);
    reds.flow_route_person_cap_present = await reasonOf(route('carrito', 'motor', 0, good));
    await db.query('delete from public.pilot_proactive_contact_caps where tenant_key = $1', [ATT1.tenant]);
    const moved = await route('carrito', 'motor', 0, good);
    if (!moved.ok || moved.route.executor !== 'motor' || moved.route.generation !== 1) fail('a motor', moved);
    const event = one((await db.query(`
      select data, generation::int as generation from public.flow_change_events
       where flow_id = $1 and event_type = 'flow_route_moved'`, [FLOWS.carrito])).rows, 'flow_route_moved');
    if (event.generation !== 1 || event.data.from_executor !== 'legado' || event.data.to_executor !== 'motor'
        || event.data.from_generation !== 0 || event.data.to_generation !== 1) {
      fail('el evento flow_route_moved', event);
    }
    // Con la ruta en motor, la guarda no deja planificar por el legado: la
    // delegacion en el motor es de la T2.3b.
    const probe = await cartFor('sonda-motor');
    try {
      await db.query('savepoint sonda');
      await db.query(PLAN_CART, probe.cartArgs);
      fail('un carrito con la ruta en motor se planifico por el legado');
    } catch (error) {
      await db.query('rollback to savepoint sonda');
      if (error.message !== 'pilot_scope_rejected' || error.detail !== 'flow_engine_not_available') {
        fail('la guarda con la ruta en motor', { message: error.message, detail: error.detail });
      }
    }
    // Volver a legado exige el scope armado.
    await setState(7, 'paused');
    reds.flow_route_legacy_not_armed = await reasonOf(route('carrito', 'legado', 1));
    await setState(7, 'armed');
    const back = await route('carrito', 'legado', 1);
    if (!back.ok || back.route.executor !== 'legado' || back.route.generation !== 2) fail('de vuelta a legado', back);
    // A motor con el flujo apagado.
    await turnOn('carrito', 'apagar');
    reds.flow_route_flow_off = await reasonOf(route('carrito', 'motor', 2, await revalidationFor(FLOWS.carrito)));
  } finally {
    await db.exec('rollback');
  }
  for (const [want, got] of Object.entries(reds)) if (want !== got) fail(`mover la ruta: ${want}`, got);
  if (one((await db.query('select cardinality(public._flow_engine_route_kinds()) as n')).rows, 'capacidades').n !== 0) {
    fail('la transaccion de la T2.3b no se deshizo');
  }
  results.mover_la_ruta = Object.keys(reds).length;
}
const CONTRACT_CODES = [
  'flow_route_kind_not_guarded', 'flow_route_needs_published_version', 'flow_route_kind_mismatch', 'flow_route_legacy_scope_unknown',
  'flow_route_already_exists', 'flow_route_generation_stale', 'flow_route_kind_not_supported', 'flow_route_flow_off',
  'flow_route_revalidation_invalid', 'flow_route_person_cap_present', 'flow_route_legacy_not_armed',
];
if (results.crear_la_ruta - 1 + results.mover_la_ruta !== CONTRACT_CODES.length) fail('falta un reason_code del contrato');

// ---------------------------------------------------------------------------
// 4. La guarda: va por legacy_kind.
// ---------------------------------------------------------------------------
let pausado;
{
  // Con la ruta en legado y el carrito prendido, todo como hoy.
  const hoy = await cartFor('hoy');
  await planCart(hoy);
  await pauseFlow('carrito');
  // El carrito pausado no se planifica...
  pausado = await cartFor('pausado');
  await expectError('un carrito con el flujo pausado', () => db.query(PLAN_CART, pausado.cartArgs),
    { code: '55000', message: 'pilot_scope_rejected', detail: 'flujo_pausado' });
  // ... y un pago fallido del mismo scope si.
  const pago = await refusalFor(4, 'pago');
  const pagoPlan = one((await db.query(PLAN_REFUSAL, pago.refusalArgs)).rows, 'plan del pago fallido');
  if (!pagoPlan.created) fail('el pago fallido del mismo scope', pagoPlan);
  // El A2 que esperaba no arranca, y el intento queda reservado.
  await sleepUntil(espera.a2.due_at);
  const reserved = await reserve(espera, espera.a2.id, (now) => evidence(espera, now));
  await expectError('A2 con el flujo pausado', () => db.query(MARK_CART,
    [espera.a2.id, reserved.attempt.id, reserved.worker, reserved.lease, new Date()]),
  { code: '55000', message: 'pilot_request_start_rejected', detail: 'flujo_pausado' });
  const attempt = one((await db.query('select phase from public.followup_delivery_attempts where id = $1',
    [reserved.attempt.id])).rows, 'intento de A2');
  if (attempt.phase !== 'reserved') fail('el intento de A2 tenia que quedar reservado', attempt);
  // Al prender, sale.
  await turnOn('carrito');
  const started = await startAndAccept(espera, reserved);
  if (started.phase !== 'request_started') fail('A2 al prender', started);
  // Con el pago fallido pausado, el pago no se planifica y el carrito si.
  await pauseFlow('pago_fallido');
  const pago2 = await refusalFor(12, 'pago-pausado');
  await expectError('un pago fallido con su flujo pausado', () => db.query(PLAN_REFUSAL, pago2.refusalArgs),
    { code: '55000', message: 'pilot_scope_rejected', detail: 'flujo_pausado' });
  await planCart(pausado);
  await turnOn('pago_fallido');
  // Apagado.
  await turnOn('carrito', 'apagar');
  const apagado = await cartFor('apagado');
  await expectError('un carrito con el flujo apagado', () => db.query(PLAN_CART, apagado.cartArgs),
    { code: '55000', message: 'pilot_scope_rejected', detail: 'flujo_apagado' });
  await turnOn('carrito');
  await planCart(apagado);
  results.guarda = { carrito_pausado: 'flujo_pausado', pago_del_mismo_scope: 'planificado',
    a2: 'rechazado_y_sale_al_prender', apagado: 'flujo_apagado' };
}

// El replay: un arranque ya autorizado que se repite con el flujo pausado
// devuelve la fila de hoy (replayed), como la autorizacion; uno nuevo no.
{
  const replay = await cartFor('replay');
  await planCart(replay);
  const reserved = await reserve(replay, replay.plan.scheduled_action_id);
  const first = one((await db.query(MARK_CART,
    [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, await dbNow()])).rows, 'replay: arranque');
  if (first.phase !== 'request_started' || first.pilot_authorization_replayed) fail('replay: el primer arranque', first);
  await pauseFlow('carrito');
  const again = one((await db.query(MARK_CART,
    [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, await dbNow()])).rows, 'replay: otra vez');
  if (again.phase !== 'request_started' || again.pilot_authorization_replayed !== true
      || again.pilot_authorization_id !== first.pilot_authorization_id) {
    fail('replay: con el flujo pausado, el arranque ya autorizado tiene que volver como replay', again);
  }
  // Otro intento del mismo arranque, sin autorizar, si se frena.
  await expectError('un intento sin autorizar con el flujo pausado', () => db.query(MARK_CART,
    [reserved.actionId, '00000000-0000-4000-8000-0000000000aa', reserved.worker, reserved.lease, new Date()]),
  { code: '55000', message: 'pilot_request_start_rejected', detail: 'flujo_pausado' });
  await turnOn('carrito');
  results.replay = 'replayed_con_el_flujo_pausado';
}

// La bolsa contada por los dos caminos (principio 12): las autorizaciones del
// motor con pool = scope_key cuentan en el total y en el dia del legado. El
// scope tiene 100 en total y 50 por dia. Cada prueba, en una transaccion que
// se deshace.
{
  const bolsa = await cartFor('bolsa');
  await planCart(bolsa);
  const reserved = await reserve(bolsa, bolsa.plan.scheduled_action_id);
  const legacy = one((await db.query(`
    select count(*)::int as total,
           count(*) filter (where local_budget_date = (now() at time zone $2)::date)::int as hoy
      from public.pilot_outbound_request_authorizations where scope_key = $1
  `, [SCOPE, ATT1.timezone])).rows, 'autorizaciones del legado');
  const engineRows = (count, daysAgo) => db.query(`
    insert into public.flow_engine_request_authorizations
      (flow_id, version, pool, legacy_scope_key, action_id, attempt_id, contact_id, local_budget_date, flow_generation)
    select $1, 1, $2, $2, gen_random_uuid(), gen_random_uuid(), gen_random_uuid(),
           (now() at time zone $3)::date - $4::integer, 0
      from generate_series(1, $5)
  `, [FLOWS.carrito, SCOPE, ATT1.timezone, daysAgo, count]);
  const startWith = async (count, daysAgo) => {
    await db.exec('begin');
    try {
      await engineRows(count, daysAgo);
      const started = one((await db.query(MARK_CART,
        [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, await dbNow()])).rows, 'bolsa');
      return started.phase;
    } catch (error) {
      return error.detail ?? error.message;
    } finally {
      await db.exec('rollback');
    }
  };
  const outcomes = {
    // Hoy, hasta el tope del dia: el dia salta y el total no.
    dia: await startWith(50 - legacy.hoy, 0),
    // Ayer, hasta el total: el dia no salta y el total si.
    total: await startWith(100 - legacy.total, 1),
    // Ayer, hasta el tope del dia: no salta nada (el dia es el local).
    otro_dia: await startWith(50 - legacy.hoy, 1),
  };
  if (outcomes.dia !== 'pilot_daily_budget_exhausted' || outcomes.total !== 'pilot_total_budget_exhausted'
      || outcomes.otro_dia !== 'request_started') {
    fail('la bolsa de los dos caminos', { outcomes, legacy });
  }
  results.bolsa = outcomes;
}

// ---------------------------------------------------------------------------
// 5. read_flow_engine_v1.
// ---------------------------------------------------------------------------
// El JSON Schema del contrato, con las palabras que usa el archivo: type,
// const, enum, pattern, minimum, maxItems, required, properties,
// additionalProperties false, items y $ref a $defs.
const SCHEMA = fixture('flow_engine/read_flow_engine_v1.schema.json');
const schemaErrors = (value, schema, path = '$', out = []) => {
  if (schema.$ref) return schemaErrors(value, SCHEMA.$defs[schema.$ref.replace('#/$defs/', '')], path, out);
  const types = Array.isArray(schema.type) ? schema.type : schema.type ? [schema.type] : null;
  const typeOf = (v) => (v === null ? 'null' : Array.isArray(v) ? 'array'
    : Number.isInteger(v) ? 'integer' : typeof v === 'number' ? 'number' : typeof v);
  if (types && !types.some((t) => t === typeOf(value) || (t === 'number' && typeOf(value) === 'integer'))) {
    out.push(`${path}: tipo ${typeOf(value)}`);
    return out;
  }
  if ('const' in schema && JSON.stringify(value) !== JSON.stringify(schema.const)) out.push(`${path}: const`);
  if (schema.enum && !schema.enum.some((option) => JSON.stringify(option) === JSON.stringify(value))) out.push(`${path}: enum`);
  if (schema.pattern && typeof value === 'string' && !new RegExp(schema.pattern, 'u').test(value)) out.push(`${path}: pattern`);
  if (schema.minimum !== undefined && typeof value === 'number' && value < schema.minimum) out.push(`${path}: minimum`);
  if (schema.maxItems !== undefined && Array.isArray(value) && value.length > schema.maxItems) out.push(`${path}: maxItems`);
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    for (const key of schema.required ?? []) if (!(key in value)) out.push(`${path}.${key}: falta`);
    for (const [key, child] of Object.entries(value)) {
      if (schema.properties?.[key]) schemaErrors(child, schema.properties[key], `${path}.${key}`, out);
      else if (schema.additionalProperties === false) out.push(`${path}.${key}: de mas`);
    }
  }
  if (Array.isArray(value) && schema.items) value.forEach((item, i) => schemaErrors(item, schema.items, `${path}[${i}]`, out));
  return out;
};
const readEngine = (after = null) => rpc('read_flow_engine_v1', INSTANCE, after);
{
  const read = await readEngine();
  const errors = schemaErrors(read, SCHEMA);
  if (errors.length !== 0) fail('read_flow_engine_v1 no cumple el JSON Schema', errors);
  // El rojo del esquema: una clave de mas y un dato personal no pasan.
  if (schemaErrors({ ...read, telefono: 'x' }, SCHEMA).length === 0
      || schemaErrors({ ...read, flows: [{ ...read.flows[0], contact_id: 'x' }] }, SCHEMA).length === 0) {
    fail('el JSON Schema deja pasar una clave de mas');
  }
  const byKey = Object.fromEntries(read.flows.map((flow) => [flow.flow_key, flow]));
  const openCases = async (reason) => Number(one((await db.query(`
    select count(distinct rc.id) as n from public.recovery_cases rc
      join public.pilot_recovery_case_bindings b on b.recovery_case_id = rc.id and b.scope_key = $1
     where rc.status in ('grace_period', 'active', 'paused')
       and exists (select 1 from public.followup_sequences s where s.recovery_case_id = rc.id and s.reason = $2)
  `, [SCOPE, reason])).rows, 'abiertos').n);
  const cart = byKey.carrito;
  const pay = byKey.pago_fallido;
  const expectedCart = await openCases('cart_abandonment');
  const expectedPay = await openCases('payment_failure');
  if (read.contract !== 'flow-engine-v1' || read.next !== null || read.flows.length !== 3
      || cart.route.executor !== 'legado' || cart.versions.length !== 1
      || cart.versions[0].version !== cart.active_version || cart.versions[0].pool !== SCOPE
      || cart.versions[0].in_flight !== expectedCart || expectedCart < 4
      || pay.versions[0].in_flight !== expectedPay || expectedPay !== 1
      || byKey.precheckout.route !== null || byKey.precheckout.versions.length !== 0
      || cart.versions[0].definition_sha256 !== await activeSha(FLOWS.carrito)) {
    fail('read_flow_engine_v1', { cart, pay, precheckout: byKey.precheckout, expectedCart, expectedPay });
  }
  // Ninguna persona en la salida.
  const text = JSON.stringify(read);
  for (const lead of [espera, pausado]) if (text.includes(lead.phone) || text.includes(lead.email)) fail('datos personales');
  await expectError('p_after invalido', () => readEngine('att1'), { code: '22023', message: 'flow_engine_after_invalid' });
  await expectError('instancia sin binding', () => rpc('read_flow_engine_v1', { tenant_ref: 'otra', ally_ref: 'otra' }, null),
    { code: '22023', message: 'flow_store_instance_not_bound' });
  // Los permisos: service_role y el rol de la T4 la leen; anon no.
  for (const role of ['service_role', 'automation_expert_change']) {
    await db.exec(`set role ${role}`);
    try {
      const viaRole = await readEngine();
      if (viaRole.flows.length !== 3) fail(`${role} lee`, viaRole.flows.length);
    } finally {
      await db.exec('reset role');
    }
  }
  await db.exec('set role anon');
  try {
    await readEngine();
    fail('anon ejecuta read_flow_engine_v1');
  } catch (error) {
    if (error.code !== '42501') fail('anon', { code: error.code, message: error.message });
  } finally {
    await db.exec('reset role');
  }
  results.lectura = { flujos: read.flows.length, en_curso_carrito: expectedCart, en_curso_pago: expectedPay };
}

// ---------------------------------------------------------------------------
// 6. read_flow_engine_history_v1.
// ---------------------------------------------------------------------------
const history = (since, product = PRODUCT) => rpc('read_flow_engine_history_v1', INSTANCE, product, since);
const QUERY_V3 = readFileSync(join(root, 'tests/fixtures/flow_engine/att1_recovery_capture_query_v3.sql'), 'utf8');
// Las claves de persona dependen de la sal: se cambian por los contactos que
// las tienen, que no dependen de la sal.
const desalted = (capture) => {
  const owners = new Map();
  for (const contact of capture.contactos) {
    for (const field of ['person_keys', 'digits_keys', 'email_keys']) {
      for (const key of contact[field] ?? []) {
        owners.set(key, [...(owners.get(key) ?? []), `${field}:${contact.id}`].sort());
      }
    }
  }
  const label = (key) => (owners.get(key) ?? ['sin_contacto']).join('|');
  const walk = (value) => {
    if (Array.isArray(value)) return value.map(walk);
    if (value && typeof value === 'object') {
      return Object.fromEntries(Object.entries(value).map(([key, child]) => {
        if (['person_key', 'digits_key', 'email_key'].includes(key) && typeof child === 'string') return [key, label(child)];
        if (['person_keys', 'digits_keys', 'email_keys'].includes(key) && Array.isArray(child)) {
          return [key, child.map(label).sort()];
        }
        return [key, walk(child)];
      }));
    }
    return value;
  };
  const out = walk(capture);
  for (const key of ['captured_at', 'origin', 'desde_origen', 'siembra']) delete out._captured[key];
  return out;
};
let historyText = null;
// Lo que no puede salir: un cupon con su codigo y un link con su URL, su sck y
// su ulid, sembrados por las RPC de hoy.
const SECRETS = new Set();
{
  const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
  const ulidAt = (ms) => {
    let value = (BigInt(ms) << 80n) | BigInt(`0x${randomBytes(10).toString('hex')}`);
    let out = '';
    for (let i = 0; i < 26; i += 1) {
      out = CROCKFORD[Number(value & 31n)] + out;
      value >>= 5n;
    }
    return out;
  };
  const secreto = await cartFor('secreto');
  await planCart(secreto);
  await db.query(`
    select * from public.claim_due_recovery_coupons_v1(
      $1::timestamptz,'motor-cupones',60,'ATT','45 minutes'::interval,'24 hours'::interval,0.1::numeric,100)
  `, [await dbNow()]);
  const reserved = await reserve(secreto, secreto.plan.scheduled_action_id);
  const ulid = ulidAt(Date.now());
  const issuance = one((await db.query('select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)',
    [reserved.actionId, reserved.worker, reserved.lease, reserved.attempt.id, ulid, reserved.now])).rows, 'link');
  if (issuance.outcome !== 'reserved') fail('el link sembrado', issuance.outcome);
  const coupons = (await db.query('select code from public.recovery_coupons where code is not null')).rows.map((row) => row.code);
  if (coupons.length === 0) fail('no hay cupon sembrado con codigo');
  for (const value of [ulid, issuance.sck_value, issuance.checkout_url_final, ...coupons]) {
    if (!value) fail('un secreto sembrado vacio');
    SECRETS.add(String(value));
  }
}
{
  await expectError('ventana de mas de 8 dias', () => history(new Date(Date.now() - 9 * 86_400_000).toISOString()),
    { code: '22023', message: 'flow_engine_history_window_too_wide' });
  await expectError('sin p_since', () => history(null), { code: '22023', message: 'flow_engine_history_since_invalid' });
  await expectError('p_since en el futuro', () => history(new Date(Date.now() + 86_400_000).toISOString()),
    { code: '22023', message: 'flow_engine_history_since_invalid' });
  await expectError('otro producto', () => history(new Date(Date.now() - 86_400_000).toISOString(), 'att9'),
    { code: '22023', message: 'flow_store_product_not_bound' });
  // p_since como lo pasa la vista previa (ahora - 8 dias) no da too_wide.
  const sinceActivation = new Date(new Date(activation).getTime() - 86_400_000);
  const firstRead = await rpc('read_flow_engine_history_v1', INSTANCE, PRODUCT,
    (await db.query("select ($1::timestamptz - interval '1 day')::text as t", [activation])).rows[0].t);
  const secondRead = await history(sinceActivation.toISOString());
  await db.exec("set timezone to 'UTC'");
  const v3 = Object.values(one((await db.query(QUERY_V3)).rows, 'la consulta v3'))[0];
  await db.exec('reset timezone');
  if (firstRead._captured.query !== 'captura-paridad-recuperacion-v3'
      || firstRead._captured.desde_origen !== `read_flow_engine_history_v1:${SCOPE}:p_since+1d`
      || new Date(firstRead._captured.desde).getTime() !== new Date(v3._captured.desde).getTime()) {
    fail('la ventana de la historia', firstRead._captured);
  }
  // La v3 lee toda la base; la funcion, solo el producto. La configuracion de
  // la cadena trae los scopes de Johanna, que no son del producto: se sacan
  // de la v3 con la misma regla (el tenant y el producto de Hotmart del scope).
  const productScopes = new Set(v3.config.pilot_scope_versions
    .filter((row) => row.tenant_key === ATT1.tenant && row.external_product_id === String(ATT1.productId))
    .map((row) => row.scope_key));
  const productPolicies = new Set(v3.config.pilot_scope_versions
    .filter((row) => productScopes.has(row.scope_key)).map((row) => row.policy_key));
  const v3OfProduct = {
    ...v3,
    config: {
      ...v3.config,
      pilot_scope_versions: v3.config.pilot_scope_versions.filter((row) => productScopes.has(row.scope_key)),
      followup_policy_versions: v3.config.followup_policy_versions.filter((row) => productPolicies.has(row.policy_key)),
      pilot_runtime_controls: v3.config.pilot_runtime_controls.filter((row) => productScopes.has(row.scope_key)),
      pilot_control_events_control_level: v3.config.pilot_control_events_control_level
        .filter((row) => productScopes.has(row.scope_key)),
      pilot_proactive_contact_caps: v3.config.pilot_proactive_contact_caps.filter((row) => row.tenant_key === ATT1.tenant),
    },
  };
  if (v3OfProduct.config.pilot_scope_versions.length === v3.config.pilot_scope_versions.length) {
    fail('la base de la prueba no tiene scopes de otro producto: la restriccion no se prueba');
  }
  if (canonical(desalted(firstRead)) !== canonical(desalted(v3OfProduct))) {
    const left = desalted(firstRead);
    const right = desalted(v3OfProduct);
    const differ = Object.keys(right).filter((key) => canonical(left[key]) !== canonical(right[key]))
      .flatMap((key) => (right[key] && typeof right[key] === 'object' && !Array.isArray(right[key])
        ? Object.keys(right[key]).filter((sub) => canonical(left[key][sub]) !== canonical(right[key][sub])).map((sub) => `${key}.${sub}`)
        : [key]));
    fail('la historia no es la captura v3 de la misma base', differ);
  }
  // Una sal por llamada: la misma persona da otra clave en otra llamada.
  const keysOf = (capture) => capture.contactos.flatMap((contact) => contact.person_keys ?? []).sort();
  if (keysOf(firstRead).length === 0 || keysOf(firstRead).some((key) => keysOf(secondRead).includes(key))) {
    fail('la sal no es por llamada');
  }
  historyText = JSON.stringify(firstRead);
  if (firstRead.cupones.length === 0 || firstRead.links.length === 0) fail('la historia no trae el cupon ni el link sembrados');
  const leaked = [...SECRETS].filter((secret) => historyText.includes(secret)).length;
  if (leaked !== 0) fail('la historia trae un codigo de cupon, una URL, un sck o un ulid sembrado', { cantidad: leaked });
  for (const lead of [espera, pausado]) {
    if (historyText.includes(lead.phone) || historyText.includes(lead.email) || historyText.includes(lead.name)) {
      fail('la historia trae datos sembrados');
    }
  }
  if (/@|https?:\/\//i.test(historyText)) fail('la historia trae una arroba o una URL');
  // Mas de 1 MB: corta.
  await db.exec('begin');
  try {
    await db.query(`
      insert into public.webhook_events (source, external_event_id, event_type, payload, received_at, processing_status)
      select 'hotmart', 'motor-relleno-' || n, 'PURCHASE_OUT_OF_SHOPPING_CART',
             jsonb_build_object('creation_date', 1760000000000 + n, 'data', jsonb_build_object('product',
               jsonb_build_object('id', $1::bigint))), clock_timestamp(), 'processed'
        from generate_series(1, 4000) n
    `, [ATT1.productId]);
    const tooLarge = await history(sinceActivation.toISOString());
    if (canonical(tooLarge) !== canonical({ ok: false, reason_code: 'flow_engine_history_too_large' })) {
      fail('mas de 1 MB', Object.keys(tooLarge));
    }
  } finally {
    await db.exec('rollback');
  }
  results.historia = { bytes: Buffer.byteLength(historyText), conteos: firstRead._captured.conteos };
}
if (OUT !== null) writeFileSync(OUT, `${historyText}\n`);

// ---------------------------------------------------------------------------
// 7. La pagina de 50 de read_flow_engine_v1, con 53 flujos.
// ---------------------------------------------------------------------------
{
  for (let i = 0; i < 50; i += 1) await register(`extra_${String(i).padStart(2, '0')}`);
  const first = await readEngine();
  const second = await readEngine(first.next);
  const keys = [...first.flows, ...second.flows].map((flow) => `${flow.product_ref}/${flow.flow_key}`);
  const all = (await db.query(`
    select product_ref || '/' || flow_key as k from public.flow_controls
     where tenant_ref = $1 and ally_ref = $2 order by product_ref, flow_key`, [ATT1.tenant, ATT1.ally])).rows.map((row) => row.k);
  if (first.flows.length !== 50 || first.next !== keys[49] || second.flows.length !== 3 || second.next !== null
      || JSON.stringify(keys) !== JSON.stringify(all) || schemaErrors(first, SCHEMA).length !== 0
      || schemaErrors(second, SCHEMA).length !== 0) {
    fail('la pagina de 50', { primera: first.flows.length, next: first.next, segunda: second.flows.length });
  }
  results.paginas = [first.flows.length, second.flows.length];
}

console.log(JSON.stringify(results));
