// La base del primer contacto nativo: el disparador intencion (migracion
// 20261101000100, E-247, P20a de M16).
//
// Por que: el primer contacto tras el formulario de Maribel corre por el
// legado (su scope maribel-primer-contacto) y el Automation Expert solo crea
// flujos nativos. ATT1 manda unos 125 primeros contactos por semana por el
// mismo legado: sin un flujo nativo de intencion, todo tiene que dar
// exactamente lo mismo que hoy.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera), mas un scope del primer contacto del legado (landing,
// PRECHECKOUT_FORM_SUBMITTED, gracia 60 min y vence a 1 dia, como el de
// Maribel y el de ATT1). El formulario es el capturado de Maribel
// (tests/fixtures/ghl_form_maribel_20261031.json, anonimizado en el servidor),
// con la persona, la instancia, la landing y la oferta del mundo sustituidas.
// La plantilla del paso es la de Maribel (maribel_retomar_proceso_01).
//
// Prueba:
//   0. la forma: cambian exactamente las funciones del encargo, nacen las
//      cuatro nuevas y la tabla, sin permisos de la API; ninguna funcion del
//      primer contacto del legado cambia; correrla dos veces aborta;
//   1. el flujo de Maribel (la definicion del diseno, 2.1) compila a la
//      politica de hoy: gracia 1 h, 00:00-23:59 CDMX, vence a 1 dia, un mensaje;
//   2. sin un flujo nativo de intencion, la entrada nueva es el legado: los
//      tres validadores del primer contacto del legado corren enteros con la
//      entrada nueva en lugar de la vieja (y el mismo renglon, aca);
//   3. en cohorte: el formulario solo abre el caso nativo (landing, la
//      intencion, el context del legado, el envio a los 60 min, el permiso, la
//      fila del resultado), sin plan del legado; el reenvio devuelve lo
//      guardado; fuera de la cohorte decide el legado y no se crea el contacto;
//      rojo: sin el disparador, el legado planifica;
//   4. los frenos antes de crear nada: el carrito ya clasificado da
//      superseded_by_provider_event sin caso ni plan; rojo sin el freno;
//   5. el despacho: el paso con la plantilla y el permiso formulario, el
//      arranque contra la bolsa y la aceptacion; con el carrito entre la
//      reevaluacion y el arranque, el arranque rechaza sin cupo;
//   6. la reevaluacion como service_role (sin 42501) corta con los motivos del
//      legado y cierra el caso (cancelled): el carrito, el pago rechazado, la
//      compra, la baja en la otra forma del telefono y la conversacion
//      derivada; rojo: con la reevaluacion de antes, sale;
//   7. la repeticion de D21 (y 7b: el toque nativo de 24 h, que el legado no ve, frena
//      tambien con el flujo pausado y en otro producto sin flujo; rojo sin el bloque): un segundo formulario tras un corte sin envio
//      arranca; tras un envio aceptado, ya_disparado; rojo: con la generica,
//      el corte sin envio frenaba;
//   8. un caso del legado abierto: caso_del_legado_en_curso y el legado dice
//      precheckout_contact_already_planned;
//   9. las dos formas del telefono: la identidad 521 de quien escribio antes y
//      el formulario 52: el nativo usa la identidad que ya existe;
//  10. un 40P01 en el nativo se relanza y no consume el formulario; un
//      trigger diferido que falla deja la admision y decide el legado;
//  11. prendido: el contacto se crea en la admision; sin consentimiento no;
//  12. la adopcion de la respuesta al carrito con un primer contacto nativo
//      ya condenado (bloque 11); rojo con la definicion de antes;
//  13. los conteos de la simulacion y la historia del AE.
import { PGlite } from '@electric-sql/pglite';
import { spawnSync } from 'node:child_process';
import { readFileSync, readdirSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildWorld, errorOf, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const MIGRATION = '20261101000100_flow_engine_native_intent_v1.sql';
const TZ = 'timestamp with time zone';
const ADMIT_NATIVE = `_flow_engine_admit_native_flow(flow_controls,webhook_events,uuid,text,${TZ},bigint,bigint,text)`;
const REEVALUATE = `reevaluate_followup_action(uuid,text,bigint,${TZ},boolean,text,text,${TZ},text,boolean,boolean,boolean,boolean,boolean)`;
const ADOPTION = 'admit_portable_inbound_commercial_case_v1(text,integer,bigint,text)';
const ROUTE = 'admit_and_route_portable_lead_precheckout_v1(text,text,integer,text,jsonb,jsonb,text,integer)';
const CHANGED = [
  '_flow_engine_native_trigger(jsonb)',
  '_flow_engine_native_steps(text)',
  ADMIT_NATIVE,
  `_flow_engine_authorize_native_tail(uuid,uuid,flow_engine_runs,recovery_cases,flow_controls,${TZ})`,
  REEVALUATE,
  `read_flow_engine_native_trigger_counts_v1(jsonb,text,text,${TZ})`,
  'read_automation_expert_native_history_v1(jsonb,uuid)',
  ADOPTION,
];
const ADDED = [
  '_flow_engine_native_intent_audience(uuid,uuid,uuid,text,text,text)',
  '_flow_engine_native_intent_unclaimed(text)',
  '_flow_engine_native_intent_stop_reason(uuid)',
  ROUTE,
];
// Lo que ninguna funcion nueva puede tocar: el primer contacto del legado (el camino de ATT1).
const LEGACY_FIRST_CONTACT = [
  'admit_and_plan_portable_lead_precheckout(text,text,integer,text,jsonb,jsonb,text,integer)',
  'admit_portable_observed_lead_precheckout(text,text,integer,text,jsonb,jsonb)',
  '_portable_precheckout_stop_reason(uuid,uuid)',
  '_find_portable_precheckout_contact(uuid)',
  '_ensure_portable_precheckout_contact(uuid,uuid)',
  '_plan_portable_precheckout_first_contact(uuid,uuid,uuid,text,integer)',
  `reevaluate_portable_precheckout_action(uuid,text,bigint,${TZ},boolean,text,text,${TZ},text,boolean,boolean,boolean,boolean,boolean)`,
  `mark_portable_precheckout_request_started(uuid,uuid,text,bigint,${TZ})`,
  '_portable_consented_intent_reason(uuid,uuid,text)',
  'admit_flow_engine_event_v1(jsonb,jsonb)',
  '_portable_case_purchase_stop_reason(uuid)',
];
// La politica del primer contacto de Maribel, medida en maribel_db (solo lectura) el 2026-10-31:
// maribel-primer-contacto-formulario v2, la del scope maribel-primer-contacto v4 (150 por dia, 3.000 en total).
const MARIBEL_POLICY = {
  timezone: 'America/Mexico_City',
  business_windows: [{ end: '23:59', days: [1, 2, 3, 4, 5, 6, 7], start: '00:00' }],
  grace_period: '01:00:00', expires_after: '1 day', max_automatic_messages: 1,
};
const maribel = JSON.parse(readFileSync(join(root,
  'tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json'), 'utf8')).message_templates[0];
const CAPTURED = JSON.parse(readFileSync(join(root, 'tests/fixtures/ghl_form_maribel_20261031.json'), 'utf8')).envios;
const PILOT = JSON.parse(readFileSync(join(root, 'tests/fixtures/instances/att1/politica-piloto.json'), 'utf8'));
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
if (names.at(-1) !== MIGRATION) fail(`${MIGRATION} tiene que ser la ultima`, names.slice(-3));
if (maribel.name !== 'maribel_retomar_proceso_01') fail('el fixture no es la plantilla de Maribel');
if (CAPTURED.length !== 2 || CAPTURED.some((envio) => envio.contract_version !== '1.1.0'
    || envio.canonical.event_type !== 'PRECHECKOUT_FORM_SUBMITTED' || envio.accion.minutos_hasta_el_envio !== 60)
    || CAPTURED[1].accion.terminal_reason !== 'superseded_by_provider_event') {
  fail('el fixture no son los dos formularios de Maribel');
}

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile, p.proowner,
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
  for (const name of names.slice(0, -1)) await applyFile(db, join(root, 'supabase/migrations', name));
  const before = await snapshot(db);
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
  for (const signature of LEGACY_FIRST_CONTACT) {
    if (!before.has(signature) || before.get(signature).definition !== after.get(signature).definition) {
      fail(`${signature}: el primer contacto del legado no se toca`);
    }
  }
  for (const signature of CHANGED) {
    if (!after.get(signature).definition.includes('flow_engine_native_intent')) fail(`${signature}: sin la marca`);
    previous[signature] = before.get(signature).definition;
  }
  // Solo service_role ejecuta la entrada y el wrapper; las otras dos, nadie de la API.
  const acl = Object.fromEntries(ADDED.map((s) => [s, { service: after.get(s).service_x, api: after.get(s).api_x,
    definer: after.get(s).prosecdef }]));
  if (JSON.stringify(acl) !== JSON.stringify({
    [ADDED[0]]: { service: false, api: false, definer: false },
    [ADDED[1]]: { service: false, api: false, definer: false },
    [ADDED[2]]: { service: true, api: false, definer: true },
    [ADDED[3]]: { service: true, api: false, definer: true },
  })) fail('los permisos de las funciones nuevas', acl);
  const table = one((await db.query(`select
      has_table_privilege('service_role', 'public.flow_engine_native_intent_admissions', 'select') as service,
      has_table_privilege('anon', 'public.flow_engine_native_intent_admissions', 'select')
        or has_table_privilege('authenticated', 'public.flow_engine_native_intent_admissions', 'select') as api,
      (select relrowsecurity from pg_class where oid = 'public.flow_engine_native_intent_admissions'::regclass) as rls`)).rows,
  'tabla');
  if (table.service || table.api || !table.rls) fail('la tabla del resultado no es de nadie de la API', table);
  const checks = (await db.query(`select conname, pg_get_constraintdef(oid) as d from pg_constraint
    where conrelid = 'public.flow_engine_native_runs'::regclass and contype = 'c'`)).rows;
  const trigger = checks.find((row) => row.conname === 'flow_engine_native_runs_trigger_event_check');
  const audience = checks.find((row) => row.conname === 'flow_engine_native_runs_audience_by_trigger');
  if (!trigger?.d.includes('intencion') || !audience?.d.includes('intencion') || !audience.d.includes('pago_fallido')) {
    fail('las restricciones del recorrido', checks);
  }
  const migrationText = readFileSync(join(root, 'supabase/migrations', MIGRATION), 'utf8');
  if (!/set local lock_timeout = '5s';/.test(migrationText)) fail('la migracion va con lock_timeout (R9)');
  let error = null;
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  } catch (caught) {
    error = caught;
  }
  if (error?.message !== 'flow_engine_native_intent_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  await db.exec('rollback').catch(() => {});
  if ((await snapshot(db)).get(ADMIT_NATIVE).definition !== after.get(ADMIT_NATIVE).definition) fail('la segunda corrida cambio algo');
  results.forma = { cambiadas: changed.length, nuevas: added.length, legado_intacto: LEGACY_FIRST_CONTACT.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// 2a. Sin un flujo nativo de intencion, la entrada nueva es el legado: los tres
//     validadores del primer contacto del legado, enteros, con la entrada nueva.
// ---------------------------------------------------------------------------
{
  const OLD = 'public.admit_and_plan_portable_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8)';
  const NEW = 'public.admit_and_route_portable_lead_precheckout_v1($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8)';
  const parity = {};
  for (const file of ['validate_portable_precheckout_first_contact.mjs', 'validate_att1_portable_chain.mjs',
    'validate_portable_inbound_template_adoption.mjs']) {
    const source = readFileSync(join(HERE, file), 'utf8');
    const calls = source.split(`select * from ${OLD}`).length - 1;
    if (calls < 1) fail(`${file}: no llama a la entrada del legado`);
    // Las mismas cinco columnas que la vieja (la nueva suma ruta y flujos).
    const swapped = source.replaceAll(`select * from ${OLD}`,
      `select outcome, submission_id, purchase_intent_id, plan_outcome, plan_reason from ${NEW}`);
    const temporary = join(HERE, `_paridad_primer_contacto_nativo_${file}`);
    writeFileSync(temporary, swapped);
    let run;
    try {
      run = spawnSync(process.execPath, [temporary], { cwd: HERE, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
    } finally {
      unlinkSync(temporary);
    }
    if (run.status !== 0) fail(`${file} con la entrada nueva`, (run.stderr || run.stdout).slice(-2000));
    parity[file] = calls;
  }
  results.paridad_sin_flujo = parity;
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 (buildWorld) con el primer contacto del legado.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, B, INSTANCE, pauseFlow, register, cycle, versionItem, stateItem, dbNow, reserve, startNew, startOld, accept, person,
  cartFor, planCart, refusalFor, rpc, setState, OPERATOR } = world;
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

// El scope del primer contacto del legado (consented_intent): el de la instancia con otra clave y sin cohorte.
const FC = { scope: 'att1-primer-contacto-e247', policy: 'att1-primer-contacto-formulario-e247' };
{
  const fc = PILOT.first_contact;
  const [defaultOffer, ...additional] = A.offers;
  await sql(`insert into public.followup_policy_versions
      (policy_key, version, status, purpose, timezone, business_windows, grace_period, expires_after,
       max_automatic_messages, steps, approved_by, approved_at, published_at)
    values ($1,1,'published','cart_recovery',$2,$3::jsonb,$4::interval,$5::interval,$6,$7::jsonb,$8,now(),now())`,
  [FC.policy, A.timezone, JSON.stringify([{ days: [1, 2, 3, 4, 5, 6, 7], start: '00:00', end: '23:59' }]),
    fc.policy.grace_period, fc.policy.expires_after, fc.policy.max_automatic_messages, JSON.stringify(fc.policy.steps),
    OPERATOR]);
  await sql(`insert into public.pilot_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
       channel, channel_provider, channel_account_ref, source, source_event_type,
       additional_source_event_types, external_product_id, offer_code,
       additional_offer_codes, purpose, policy_key, policy_version, timezone,
       max_cohort_contacts, max_outbound_request_starts_total,
       max_outbound_request_starts_per_day, audience_mode, approved_by, approved_at, published_at)
    values ($1,1,'published',$2,$3,$4,'whatsapp',$5,$6,'landing','PRECHECKOUT_FORM_SUBMITTED',array[]::text[],
            $7,$8,$9::text[],'cart_recovery',$10,1,$11,20,1000,500,'consented_intent',$12,now(),now())`,
  [FC.scope, A.tenant, A.accountId, A.inboxId, fc.pilot_scope.channel_provider,
    `${fc.pilot_scope.channel_account_ref_prefix}${A.inboxId}`, String(A.productId), defaultOffer.offer_code,
    additional.map((offer) => offer.offer_code), FC.policy, A.timezone, OPERATOR]);
  await sql(`insert into public.pilot_runtime_controls (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
    values ($1,1,'inactive',0,$2,'el primer contacto del legado')`, [FC.scope, OPERATOR]);
  await setState(FC.scope, 1, 'armed');
}

// El formulario capturado de Maribel, con la persona, la instancia y la oferta del mundo.
let formSerial = 0;
const formOf = (lead, submittedAt, { captured = CAPTURED[0], consent = true } = {}) => {
  formSerial += 1;
  const { offer } = lead;
  const P = lead.product ?? A;
  const id = `e247-form-${formSerial}-${lead.email}`;
  const pageUrl = `https://${offer.page_host}${offer.page_path}`;
  const checkoutUrl = `https://pay.hotmart.com/${P.hotlink}?off=${offer.offer_code}&checkoutMode=10`;
  const raw = structuredClone(captured.raw);
  raw.id = id;
  raw.created_at = submittedAt.toISOString();
  raw.dedupe_key = `${offer.site}:${offer.offer_code}:${lead.email}:${formSerial}`;
  raw.source = { ...raw.source, site: offer.site, aliado: P.brand, landing_id: offer.landing_id, page_url: pageUrl };
  raw.data.buyer = { name: lead.name, email: lead.email, phone: `+${lead.phone}`, phone_country_code: lead.country,
    phone_national: lead.phone.slice(lead.country.length) };
  raw.data.product = { ...raw.data.product, hotlink: P.hotlink, name: P.productName, price: Number(P.price), currency: P.currency };
  raw.data.offer = { code: offer.offer_code };
  raw.data.checkout_url = checkoutUrl;
  raw.data.checkout_country = { ...raw.data.checkout_country, iso: lead.iso };
  raw.data.consent = { ...raw.data.consent, copy_version: P.copyVersion };
  const canonical = structuredClone(captured.canonical);
  canonical.external_submission_id = id;
  canonical.submitted_at = raw.created_at;
  canonical.dedupe_key = raw.dedupe_key;
  canonical.source = { ...canonical.source, tenant_ref: P.tenant, funnel_ref: P.funnel, landing_ref: offer.landing_id,
    page_url: pageUrl, aliado: P.brand };
  canonical.identity = { ...canonical.identity, email: lead.email, phone: lead.phone, phone_country_iso: lead.iso };
  canonical.lead = { full_name: lead.name };
  canonical.commerce = { ...canonical.commerce, product_ref: P.hotlink, product_name: P.productName,
    offer_ref: offer.offer_code, price: String(P.price), currency: P.currency, checkout_url: checkoutUrl };
  canonical.consent = { ...canonical.consent, copy_version: P.copyVersion };
  if (!consent) {
    // El envio 1.0.0 sin opt-in (variante derivada, la de validate_portable_precheckout_first_contact.mjs).
    raw.version = '1.0.0';
    raw.data.consent = { marketing_optin: false, notice: 'Aviso de privacidad sin opt-in explicito.' };
    canonical.contract_version = '1.0.0';
    canonical.consent = { terms_accepted: false, privacy_accepted: false, marketing_optin: false,
      whatsapp_contact: false, copy_version: 'lead-precheckout-v1-no-explicit-optin' };
    canonical.assurance = { ...canonical.assurance, activation_authorized: false };
  }
  return { id, raw, canonical };
};
const ROUTE_SQL = 'select * from public.admit_and_route_portable_lead_precheckout_v1($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8)';
const LEGACY_SQL = 'select * from public.admit_and_plan_portable_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8)';
const submit = async (lead, { minutesAgo = 61, scope = true, form = null, legacy = false, consent = true } = {}) => {
  const at = new Date((await dbNow()).getTime() - minutesAgo * 60_000);
  const used = form ?? formOf(lead, at, { consent });
  const P = lead.product ?? A;
  const row = one(await asService(async () => (await db.query(legacy ? LEGACY_SQL : ROUTE_SQL,
    [P.tenant, P.funnel, P.bindingVersion, used.id, JSON.stringify(used.raw), JSON.stringify(used.canonical),
      scope ? FC.scope : null, scope ? 1 : null])).rows), `${lead.label} formulario`);
  lead.form = used;
  lead.submittedAt = at;
  return row;
};
// Una persona que ya existe (la de la cohorte: Dan con su telefono), con su contacto.
const seed = async (lead) => {
  lead.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
    values ($1,$2,$3,'MX') returning id`, [lead.name, lead.email, lead.phone]), 'contacto').id;
  await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1,'email',$2,$2,'system'), ($1,'phone',$3,$3,'system')`, [lead.contact, lead.email, lead.phone]);
  return lead;
};
const legacyPlanOf = async (submission) => (await sql(
  'select outcome, reason_code from public.portable_precheckout_first_contact_plans where submission_id = $1', [submission]))[0] ?? null;
const admissionsOf = async (submission) => sql(`select flow_id, ruta, outcome, reason, recovery_case_id
  from public.flow_engine_native_intent_admissions where submission_id = $1 order by flow_id`, [submission]);
const flowRow = (row, flowId) => one(row.flujos.filter((f) => f.flow_id === flowId), 'fila del flujo');
const casesOf = async (lead) => sql(`select id, status, source, offer_code, hotmart_purchase_intent_id, policy_key, context
  from public.recovery_cases where contact_id = $1 order by created_at`, [lead.contact]);
const actionOf = async (caseId) => one(await sql(`select * from public.scheduled_actions where recovery_case_id = $1`,
  [caseId]), 'accion');

// ---------------------------------------------------------------------------
// 1. El flujo de Maribel (2.1 del diseno) compila a la politica de hoy.
// ---------------------------------------------------------------------------
const MARIBEL_INTENT = {
  esquema: 'setter-flujo/v1', flujo: 'primer_contacto', producto: A.funnel, nombre: 'Primer contacto tras el formulario',
  dispara: { evento: 'intencion' }, entra: { permiso: 'formulario' },
  pasos: [{ esperar: '60m' }, { plantilla: maribel.name, idioma: 'es_MX', variables: ['nombre', 'producto'] }],
  termina: { si: ['compra'], vence: '1d' },
  horario: { zona: 'America/Mexico_City', desde: '00:00', hasta: '23:59' },
  topes: { bolsa: 'maribel-primer-contacto-nativo', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona', otra_despues_de: '24h' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
{
  const unsupported = one(await sql('select public._flow_engine_native_unsupported($1::jsonb) as r',
    [JSON.stringify(MARIBEL_INTENT)]), 'lo que no ejecuta').r;
  if (unsupported !== null) fail('el primer contacto de Maribel se ejecuta', unsupported);
  const compiled = one(await sql(`select timezone, business_windows,
      case when grace_period = interval '1 hour' then '01:00:00' else grace_period::text end as grace_period,
      case when expires_after = interval '1 day' then '1 day' else expires_after::text end as expires_after,
      max_automatic_messages, steps
    from public._flow_engine_compile_policy_core($1::jsonb, '00000000-0000-4000-8000-0000000e2471', 1, 'nativo:intencion', 'America/Mexico_City')`,
  [JSON.stringify(MARIBEL_INTENT)]), 'politica');
  const got = { timezone: compiled.timezone, business_windows: compiled.business_windows, grace_period: compiled.grace_period,
    expires_after: compiled.expires_after, max_automatic_messages: compiled.max_automatic_messages };
  if (JSON.stringify(got) !== JSON.stringify(MARIBEL_POLICY) || compiled.steps.length !== 1
      || compiled.steps[0].step_key !== 'first_contact') {
    fail('la politica de Maribel', { got, want: MARIBEL_POLICY, steps: compiled.steps });
  }
  results.politica = got;
}

// ---------------------------------------------------------------------------
// 2b. Sin flujo, en este mundo: el renglon del legado y la ruta legado.
// ---------------------------------------------------------------------------
{
  const lead = person('sin-flujo');
  const form = formOf(lead, new Date((await dbNow()).getTime() - 61 * 60_000));
  const strip = (row) => ({ outcome: row.outcome, plan_outcome: row.plan_outcome, plan_reason: row.plan_reason });
  const legacy = await rolledBack(() => submit(lead, { form, legacy: true }));
  const routed = await rolledBack(() => submit(lead, { form }));
  if (JSON.stringify(strip(legacy)) !== JSON.stringify(strip(routed)) || routed.ruta !== 'legado'
      || JSON.stringify(routed.flujos) !== '[]' || legacy.plan_reason !== 'first_contact_scheduled') {
    fail('sin flujo, la entrada nueva es el legado', { legacy, routed });
  }
  // Sin el scope del legado (el flag apagado): solo la admision, como hoy.
  const onlyAdmission = await rolledBack(() => submit(lead, { form, scope: false }));
  if (onlyAdmission.outcome !== 'inserted' || onlyAdmission.plan_outcome !== null || onlyAdmission.ruta !== 'legado') {
    fail('sin scope, solo la admision', onlyAdmission);
  }
  const invalid = await rolledBack(() => errorOf(db, () => db.query(ROUTE_SQL,
    [A.tenant, A.funnel, A.bindingVersion, form.id, JSON.stringify(form.raw), JSON.stringify(form.canonical), FC.scope, null])));
  if (invalid?.message !== 'invalid_pilot_plan_parameters') fail('el scope sin version', invalid?.message);
  results.sin_flujo = { legado: strip(legacy), nueva: strip(routed), ruta: routed.ruta };
}

// El flujo nativo del primer contacto, en cohorte (el AE con los valores de hoy; horario siempre en el mundo).
const PRIMER = { ...MARIBEL_INTENT, estado: 'borrador', entra: { permiso: 'formulario', contesto_a: [] },
  horario: 'siempre', topes: { ...MARIBEL_INTENT.topes, bolsa: 'att1-primer-contacto-nativo' } };
const flowId = await register(A.funnel, 'primer_contacto');
await cycle([versionItem(flowId, A.funnel, 'primer_contacto', PRIMER)], 'v1 del primer contacto');
{
  const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'propuesta').proposal_id;
  const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [flowId, proposalId, A.timezone]), 'abrir').r);
  if (!opened.ok || opened.state !== 'cohorte') fail('abrir el primer contacto', opened);
}
const cohort = async (contactId) => sql(
  `insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`, [flowId, contactId]);
const member = async (label) => {
  const lead = await seed(person(label));
  await cohort(lead.contact);
  return lead;
};

// ---------------------------------------------------------------------------
// 3. En cohorte: el formulario solo.
// ---------------------------------------------------------------------------
const maria = await member('intencion-maria');
const started = await submit(maria);
const startedRow = flowRow(started, flowId);
if (started.ruta !== 'nativo' || startedRow.outcome !== 'motor_arranco' || started.plan_outcome !== null) {
  fail('el formulario abre el recorrido nativo', started);
}
{
  const kase = one(await casesOf(maria), 'caso');
  const native = one(await sql('select * from public.flow_engine_native_runs where recovery_case_id = $1', [kase.id]), 'recorrido');
  const action = await actionOf(kase.id);
  const anchor = one(await sql('select * from public.webhook_events where id = $1', [action.anchor_subject_internal_id]), 'ancla');
  const trace = await sql('select event_role, webhook_event_id from public.recovery_case_events where recovery_case_id = $1', [kase.id]);
  if (kase.source !== 'landing' || kase.offer_code !== null || kase.hotmart_purchase_intent_id !== started.purchase_intent_id
      || kase.context.trigger_kind !== 'precheckout_intent' || kase.context.flow_engine_trigger !== 'intencion'
      || kase.context.flow_engine_offer !== maria.offer.offer_code || kase.context.purchase_intent_id !== started.purchase_intent_id
      || kase.context.precheckout_submission_id !== started.submission_id
      || native.trigger_event !== 'intencion' || native.audience_purchase_intent_id !== started.purchase_intent_id
      || native.audience_precheckout_submission_id !== started.submission_id || native.offer_code !== maria.offer.offer_code) {
    fail('el caso nativo del formulario', { kase, native });
  }
  if (anchor.source !== 'system' || anchor.event_type !== 'PRECHECKOUT_FORM_SUBMITTED' || anchor.processing_status !== 'processed'
      || anchor.external_event_id !== `flow-engine-intent:${started.submission_id}`
      || anchor.payload.creation_date !== maria.submittedAt.getTime()
      || Object.keys(anchor.payload).sort().join() !== 'creation_date,precheckout_submission_id,purchase_intent_id') {
    fail('el ancla del formulario, solo ids', anchor);
  }
  if (trace.length !== 1 || trace[0].event_role !== 'precheckout_intent' || trace[0].webhook_event_id !== anchor.id) {
    fail('el formulario queda en el caso', trace);
  }
  const wait = (new Date(action.due_at) - maria.submittedAt) / 60_000;
  const expires = (new Date(action.expires_at) - maria.submittedAt) / 60_000;
  if (Math.abs(wait - CAPTURED[0].accion.minutos_hasta_el_envio) > 0.01 || Math.abs(expires - 1440) > 0.01 || action.anchor_type !== 'flow_engine_event') {
    fail('el envio a los 60 min del formulario y vence al dia', { wait, expires });
  }
  const grant = one(await sql(`select * from public.contact_authorizations where contact_id = $1 and purpose = 'cart_recovery'`,
    [maria.contact]), 'autorizacion');
  if (grant.authorization_source !== 'system' || grant.evidence.reason !== 'precheckout_whatsapp_consent'
      || grant.evidence.precheckout_submission_id !== started.submission_id) fail('el permiso del formulario', grant);
  const admissions = await admissionsOf(started.submission_id);
  if (admissions.length !== 1 || admissions[0].ruta !== 'nativo' || admissions[0].recovery_case_id !== kase.id) {
    fail('la fila del resultado nativo', admissions);
  }
  if (await legacyPlanOf(started.submission_id) !== null) fail('el legado no planifica lo que tomo el nativo');
  // El reenvio de GHL (el mismo envio): lo guardado, sin otro caso.
  const replay = await submit(maria, { form: maria.form });
  if (replay.outcome !== 'duplicate' || replay.ruta !== 'nativo' || flowRow(replay, flowId).recovery_case_id !== kase.id
      || (await casesOf(maria)).length !== 1) fail('el reenvio devuelve lo guardado', replay);
  maria.caseId = kase.id;
  maria.actionId = action.id;
  results.admision = { ruta: started.ruta, salida: startedRow.outcome, espera_min: wait, reenvio: replay.outcome };
}
{
  // Fuera de la cohorte: decide el legado, y sin scope del legado no se crea el contacto.
  const outsider = person('intencion-afuera');
  const out = await rolledBack(() => submit(outsider));
  if (out.ruta !== 'legado' || flowRow(out, flowId).reason !== 'fuera_de_cohorte' || out.plan_reason !== 'first_contact_scheduled') {
    fail('fuera de la cohorte decide el legado', out);
  }
  const outOnly = await rolledBack(async () => {
    const row = await submit(outsider, { scope: false });
    const contacts = await sql('select 1 from public.contacts where email = $1', [outsider.email]);
    const admissions = await admissionsOf(row.submission_id);
    const rejections = (await sql('select count(*)::int as n from public.flow_engine_start_rejections where source_ref = $1',
      [row.submission_id])).at(0).n;
    return { row, contacts: contacts.length, admissions, rejections };
  });
  if (outOnly.row.ruta !== 'legado' || outOnly.contacts !== 0 || outOnly.admissions.length !== 1
      || outOnly.admissions[0].ruta !== 'legado' || outOnly.admissions[0].reason !== 'fuera_de_cohorte'
      || outOnly.rejections !== 0) {
    fail('fuera de la cohorte no se crea el contacto', outOnly);
  }
  // Rojo: sin el disparador intencion, el motor no lo ejecuta y planifica el legado (las fallas del motor van al legado).
  const lead = await member('intencion-sin-disparador');
  const red = await withPrevious(['_flow_engine_native_trigger(jsonb)'], () => submit(lead));
  if (red.ruta !== 'legado' || flowRow(red, flowId).reason !== 'motor_nativo:disparador' || red.plan_reason !== 'first_contact_scheduled') {
    fail('sin el disparador, el legado planifica', red);
  }
  results.cohorte = { afuera: out.ruta, sin_contacto: outOnly.contacts, rojo: flowRow(red, flowId).reason };
}

// ---------------------------------------------------------------------------
// 4. Los frenos antes de crear nada.
// ---------------------------------------------------------------------------
{
  const lead = await seed(person('intencion-carrito-antes'));
  // El formulario y el carrito antes de entrar a la cohorte: la intencion queda en confirmed_abandonment.
  await submit(lead, { scope: false, minutesAgo: 90 });
  await cartFor(lead, { form: false });
  await cohort(lead.contact);
  const caseCount = (await sql('select count(*)::int as n from public.recovery_cases')).at(0).n;
  const before = await submit(lead);
  if (before.ruta !== 'nativo' || flowRow(before, flowId).reason !== 'superseded_by_provider_event'
      || await legacyPlanOf(before.submission_id) !== null
      || (await sql('select count(*)::int as n from public.recovery_cases')).at(0).n !== caseCount) {
    fail('con el carrito ya clasificado no se crea nada', before);
  }
  // Rojo: sin el freno del legado, el nativo abria el caso.
  const red = await rolledBack(async () => {
    await db.exec(`create or replace function public._portable_precheckout_stop_reason(p_purchase_intent_id uuid, p_contact_id uuid)
      returns text language sql stable set search_path = pg_catalog, public, pg_temp as $$ select null::text $$`);
    return flowRow(await submit(lead), flowId);
  });
  if (red.outcome !== 'motor_arranco') fail('sin el freno, el nativo abria el caso', red);
  results.freno_al_admitir = { motivo: flowRow(before, flowId).reason, sin_el_freno: red.outcome };
}

// ---------------------------------------------------------------------------
// 5. El despacho.
// ---------------------------------------------------------------------------
{
  const lead = { label: 'intencion-maria', chatwoot: '771' };
  const reserved = await reserve(lead, maria.actionId);
  const step = one(await asService(async () => (await db.query('select * from public.flow_engine_execution_context_v1($1)',
    [maria.actionId])).rows), 'contexto del paso');
  if (step.template_name !== maribel.name || step.entry_permission !== 'formulario' || step.step_key !== 'first_contact'
      || JSON.stringify(step.template_variables) !== JSON.stringify(['nombre', 'producto'])) {
    fail('el contexto del paso', step);
  }
  // Rojo: con el carrito entre la reevaluacion y el arranque, el arranque rechaza y no consume cupo.
  const red = await rolledBack(async () => {
    await sql(`update public.purchase_intents set current_classification = 'confirmed_abandonment' where id = $1`,
      [started.purchase_intent_id]);
    const row = await startNew(reserved);
    const auth = (await sql('select count(*)::int as n from public.flow_engine_request_authorizations where pool = $1',
      [PRIMER.topes.bolsa])).at(0).n;
    return { outcome: row.outcome, reason: row.reason, auth };
  });
  if (red.outcome !== 'rechazado' || red.reason !== 'superseded_by_provider_event' || red.auth !== 0) {
    fail('el arranque vuelve a mirar los frenos', red);
  }
  // Y sin el bloque del arranque, salia.
  const redTail = await withPrevious([CHANGED[3]], async () => {
    await sql(`update public.purchase_intents set current_classification = 'confirmed_abandonment' where id = $1`,
      [started.purchase_intent_id]);
    return (await startNew(reserved)).outcome;
  });
  if (redTail !== 'request_started') fail('sin el bloque del arranque, salia', redTail);
  const startedSend = await startNew(reserved);
  if (startedSend.outcome !== 'request_started' || !startedSend.flow_authorization_id) fail('el arranque nativo', startedSend);
  const accepted = await accept(lead, reserved);
  const pool = one(await sql('select count(*)::int as n from public.flow_engine_request_authorizations where pool = $1',
    [PRIMER.topes.bolsa]), 'bolsa').n;
  if (pool !== 1) fail('la bolsa del primer contacto cuenta el envio', pool);
  maria.sent = true;
  results.despacho = { plantilla: step.template_name, permiso: step.entry_permission, rojo: red.reason,
    sin_el_bloque: redTail, aceptacion: accepted.status };
}

// ---------------------------------------------------------------------------
// 6. La reevaluacion como service_role: los motivos del legado.
// ---------------------------------------------------------------------------
// El despacho como lo hace el worker, pero con la reevaluacion como service_role.
const claimAs = async (label, actionId) => {
  await sql(`update public.scheduled_actions set due_at = least(now() + interval '1 day', expires_at - interval '1 minute')
     where id <> $1 and status in ('pending', 'deferred', 'retryable_failed') and due_at <= now() + interval '1 minute'`, [actionId]);
  await sql(`update public.scheduled_actions set due_at = least(due_at, now()), next_attempt_at = null where id = $1`, [actionId]);
  const worker = `e247-worker-${label}`;
  const now = await dbNow();
  const claimed = await sql("select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)", [worker, now]);
  if (claimed.length !== 1 || claimed[0].id !== actionId) fail(`${label}: el claim tomo otra cosa`, claimed.length);
  return one(await asService(async () => (await db.query('select * from public.reevaluate_followup_action($1,$2,$3,$4)',
    [actionId, worker, claimed[0].lease_generation, now])).rows), `${label} reevaluacion`);
};
const fresh = async (label, overrides = {}) => {
  const lead = await seed({ ...person(label), ...overrides });
  await cohort(lead.contact);
  const row = await submit(lead);
  const cell = flowRow(row, flowId);
  if (cell.outcome !== 'motor_arranco') fail(`${label}: arranca`, row);
  lead.caseId = cell.recovery_case_id;
  lead.actionId = (await actionOf(cell.recovery_case_id)).id;
  lead.intent = row.purchase_intent_id;
  return lead;
};
const cuts = {};
const expectCut = async (label, setup, reason, overrides = {}) => {
  const lead = await fresh(label, overrides);
  await setup(lead);
  const decision = await claimAs(label, lead.actionId);
  const kase = one(await sql('select status from public.recovery_cases where id = $1', [lead.caseId]), 'caso').status;
  const sequence = one(await sql('select status from public.followup_sequences where recovery_case_id = $1', [lead.caseId]), 'secuencia').status;
  if (decision.decision !== 'cancel' || decision.reason_code !== reason || kase !== 'cancelled' || sequence !== 'completed') {
    fail(`${label}: la reevaluacion corta`, { decision, kase, sequence });
  }
  cuts[label] = decision.reason_code;
  return lead;
};
{
  // El carrito despues del formulario (el 62 % de Maribel): el legado y el carrito nativo nacen igual de Hotmart.
  const cart = await expectCut('corte-carrito', async (lead) => { await cartFor(lead, { form: false }); },
    'superseded_by_provider_event');
  // Rojo: con la reevaluacion de antes, el primer contacto salia sobre el carrito.
  const red = await rolledBack(async () => {
    const lead = await fresh('corte-carrito-rojo');
    await cartFor(lead, { form: false });
    await db.exec(previous[REEVALUATE]);
    return (await claimAs('corte-carrito-rojo', lead.actionId)).decision;
  });
  if (red !== 'execute') fail('con la reevaluacion de antes, salia', red);
  cuts.rojo = red;
  // El pago rechazado: el legado del pago abre su caso de Hotmart.
  await expectCut('corte-pago', async (lead) => { await refusalFor(1, lead); }, 'superseded_by_provider_event');
  // La compra: la intencion comprada.
  await expectCut('corte-compra', async (lead) => {
    await sql(`update public.purchase_intents set lifecycle_state = 'purchased' where id = $1`, [lead.intent]);
  }, 'intent_purchased');
  // La baja por el camino real de Chatwoot desde la otra forma del movil (521), que no tiene identidad: queda
  // unmatched y el contacto conserva su permiso; el freno del legado la ve en las dos formas.
  await expectCut('corte-baja', async (lead) => {
    const optOut = one(await sql(`select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,$6,'unsubscribe')`,
      [A.accountId, A.inboxId, 977001, 997001, `521${lead.phone.slice(2)}`, await dbNow()]), 'baja');
    if (optOut.outcome !== 'recorded_unmatched') fail('la baja de la otra forma queda unmatched', optOut);
  }, 'precheckout_prior_opt_out', { phone: '525550002472', country: '52', iso: 'MX' });
  // La conversacion derivada en otra conversacion del contacto.
  await expectCut('corte-derivada', async (lead) => {
    const inbound = one(await sql('select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)',
      [A.inboundScope, A.inboundVersion, 978001, lead.phone]), 'entrante');
    await sql(`update public.conversations set human_takeover = true where id = $1`, [inbound.conversation_id]);
  }, 'precheckout_conversation_handoff');
  results.reevaluacion = cuts;
  cart.done = true;
}

// ---------------------------------------------------------------------------
// 7. La repeticion de D21.
// ---------------------------------------------------------------------------
{
  // Un corte sin envio (el carrito) no frena el formulario siguiente.
  const lead = await expectCut('repeticion-corte', async (l) => { await cartFor(l, { form: false }); },
    'superseded_by_provider_event');
  const repeatForm = (l) => {
    // El formulario siguiente, con la intencion sin la clasificacion del carrito (otra oferta: otra intencion).
    l.offer = A.offers[0];
    return submit(l, { minutesAgo: 5 });
  };
  const red = await rolledBack(async () => {
    const definition = one(await sql('select pg_get_functiondef($1::regprocedure) as d', [ADMIT_NATIVE]), 'definicion').d;
    const begin = definition.indexOf('    -- flow_engine_native_intent_repeat: begin');
    const end = definition.indexOf('    -- flow_engine_native_intent_repeat: end\n') + '    -- flow_engine_native_intent_repeat: end\n'.length;
    const generic = definition.slice(0, begin) + definition.slice(end).replace(
      "\n       and public._flow_engine_native_trigger(v_definition.definition) is distinct from 'intencion' then\n", ' then\n');
    await db.exec(generic);
    return flowRow(await repeatForm({ ...lead }), flowId).reason;
  });
  if (red !== 'ya_disparado') fail('con la repeticion generica, el corte sin envio frenaba', red);
  const again = await repeatForm(lead);
  if (flowRow(again, flowId).outcome !== 'motor_arranco') fail('tras un corte sin envio, arranca', again);
  // Tras un envio aceptado (Maria): ya_disparado.
  maria.offer = A.offers[0];
  const sentAgain = await submit(maria, { minutesAgo: 5 });
  if (sentAgain.ruta !== 'nativo' || flowRow(sentAgain, flowId).reason !== 'ya_disparado') {
    fail('tras un envio aceptado, ya_disparado', sentAgain);
  }
  results.repeticion = { tras_corte: flowRow(again, flowId).outcome, tras_envio: flowRow(sentAgain, flowId).reason, rojo: red };
}

// ---------------------------------------------------------------------------
// 7b. El toque nativo de 24 h que el legado no ve (D21): un formulario que iba a
//     quedar para el legado despues de un envio nativo es del nativo (ya_disparado).
// ---------------------------------------------------------------------------
{
  const guardOff = async () => {
    const definition = one(await sql('select pg_get_functiondef($1::regprocedure) as d', [ROUTE]), 'entrada').d;
    const anchor = '                if v_found_contact is not null then\n                    select run.flow_id, run.version';
    if (!definition.includes(anchor)) fail('el ancla del toque nativo en la entrada');
    await db.exec(definition.replace(anchor, '                if false then\n                    select run.flow_id, run.version'));
  };
  // a. El mismo producto con el flujo pausado (la vuelta atras, R4 del diseno).
  const paused = async (guard) => rolledBack(async () => {
    if (!guard) await guardOff();
    await pauseFlow(flowId, A.funnel, 'primer_contacto');
    const row = await submit({ ...maria }, { minutesAgo: 3 });
    return { ruta: row.ruta, reason: flowRow(row, flowId).reason, plan: row.plan_reason };
  });
  const pausedNow = await paused(true);
  const pausedRed = await paused(false);
  if (pausedNow.ruta !== 'nativo' || pausedNow.reason !== 'ya_disparado' || pausedNow.plan !== null) {
    fail('con el flujo pausado, el toque nativo de 24 h frena al legado', pausedNow);
  }
  if (pausedRed.ruta !== 'legado' || pausedRed.plan !== 'first_contact_scheduled') fail('sin el bloque, el legado mandaba otro', pausedRed);
  // b. Otro producto sin flujo nativo (B, del mismo binding): la misma persona.
  const other = async (guard) => rolledBack(async () => {
    if (!guard) await guardOff();
    const lead = { ...maria, label: 'intencion-otro-producto', product: B, offer: B.offers[0] };
    const row = await submit(lead, { minutesAgo: 3, scope: false });
    return { ruta: row.ruta, flujos: row.flujos };
  });
  const otherNow = await other(true);
  const otherRed = await other(false);
  if (otherNow.ruta !== 'nativo' || otherNow.flujos.length !== 1 || otherNow.flujos[0].reason !== 'ya_disparado'
      || otherNow.flujos[0].flow_id !== flowId) {
    fail('en otro producto, el toque nativo de 24 h es del nativo', otherNow);
  }
  if (otherRed.ruta !== 'legado') fail('sin el bloque, otro producto iba al legado', otherRed);
  results.toque_nativo = { pausado: pausedNow.reason, pausado_sin_el_bloque: pausedRed.plan,
    otro_producto: otherNow.flujos[0].reason, otro_producto_sin_el_bloque: otherRed.ruta };
}

// ---------------------------------------------------------------------------
// 8. Un caso del legado abierto: decide el legado.
// ---------------------------------------------------------------------------
{
  const lead = await seed(person('intencion-legado'));
  const legacy = await submit(lead, { legacy: true });
  if (legacy.plan_reason !== 'first_contact_scheduled') fail('el legado planifica primero', legacy);
  await cohort(lead.contact);
  lead.offer = A.offers[0];
  const second = await submit(lead, { minutesAgo: 5 });
  if (second.ruta !== 'legado' || flowRow(second, flowId).reason !== 'caso_del_legado_en_curso'
      || second.plan_reason !== 'precheckout_contact_already_planned') {
    fail('con un caso del legado abierto decide el legado', second);
  }
  const rejection = await sql(`select reason from public.flow_engine_start_rejections where flow_id = $1 and source_ref = $2`,
    [flowId, second.submission_id]);
  if (rejection.length !== 1 || rejection[0].reason !== 'caso_del_legado_en_curso') fail('el rechazo queda registrado', rejection);
  // El reenvio de un envio que tomo el legado: el renglon del legado, sin otra fila.
  const again = await submit(lead, { form: lead.form });
  if (again.outcome !== 'duplicate' || again.ruta !== 'legado' || again.plan_reason !== 'precheckout_contact_already_planned'
      || JSON.stringify(again.flujos) !== '[]' || (await admissionsOf(second.submission_id)).length !== 1) {
    fail('el reenvio de un envio del legado devuelve el renglon del legado', again);
  }
  // El otro brazo: el caso del legado ya cerrado con su toque aceptado hace menos de 24 h.
  // ESTADO SEMBRADO: el toque aceptado y el caso cerrado se escriben a mano (el despacho del legado no corre en este mundo).
  const closed = await seed(person('intencion-legado-cerrado'));
  const closedPlan = await submit(closed, { legacy: true });
  const closedCase = one(await sql(`select recovery_case_id from public.portable_precheckout_first_contact_plans
    where submission_id = $1`, [closedPlan.submission_id]), 'plan').recovery_case_id;
  await sql(`update public.scheduled_actions set status = 'accepted_by_chatwoot', terminal_reason = 'accepted_by_chatwoot',
    executed_at = clock_timestamp() - interval '2 hours' where recovery_case_id = $1`, [closedCase]);
  await sql(`update public.recovery_cases set status = 'sequence_exhausted', closed_at = clock_timestamp(), version = version + 1
    where id = $1`, [closedCase]);
  await cohort(closed.contact);
  closed.offer = A.offers[0];
  const touchArm = async (red) => rolledBack(async () => {
    if (red) {
      const definition = one(await sql('select pg_get_functiondef($1::regprocedure) as d', [ADMIT_NATIVE]), 'definicion').d;
      const arm = '            or exists (\n               select 1\n                 from public.scheduled_actions touched';
      if (!definition.includes(arm)) fail('el brazo del toque del legado');
      await db.exec(definition.replace(arm, '            or false and exists (\n               select 1\n                 from public.scheduled_actions touched'));
    }
    const row = await submit({ ...closed }, { minutesAgo: 5 });
    return { reason: flowRow(row, flowId).reason, plan: row.plan_reason };
  });
  const touched = await touchArm(false);
  const touchedRed = await touchArm(true);
  if (touched.reason !== 'caso_del_legado_en_curso' || touched.plan !== 'precheckout_contact_already_planned') {
    fail('con un toque del legado aceptado en 24 h decide el legado', touched);
  }
  if (touchedRed.reason !== 'motor_arranco' && touchedRed.reason !== null) fail('sin el brazo, el nativo arrancaba', touchedRed);
  results.legado_en_curso = { ruta: second.ruta, legado: second.plan_reason, reenvio: again.ruta,
    toque_cerrado: touched.reason, toque_cerrado_sin_el_brazo: touchedRed.reason ?? 'motor_arranco' };
}

// ---------------------------------------------------------------------------
// 9. Las dos formas del telefono.
// ---------------------------------------------------------------------------
{
  // Quien escribio antes por WhatsApp (521 + 10) y deja el formulario con 52 + 10.
  const lead = { ...person('intencion-dos-formas'), phone: '525550002471', country: '52', iso: 'MX' };
  await seed(lead);
  await cohort(lead.contact);
  await sql(`insert into public.channel_identities (contact_id, channel, account_id, external_user_id, identity_status, metadata)
    values ($1, 'whatsapp', $2, '5215550002471', 'active', $3::jsonb)`,
  [lead.contact, `chatwoot:${A.accountId}`, JSON.stringify({ inbox_id: A.inboxId })]);
  const row = await submit(lead);
  const kase = flowRow(row, flowId);
  const identity = one(await sql(`select i.external_user_id from public.recovery_cases rc
    join public.channel_identities i on i.id = rc.selected_channel_identity_id where rc.id = $1`, [kase.recovery_case_id]), 'identidad');
  const identities = (await sql('select count(*)::int as n from public.channel_identities where contact_id = $1', [lead.contact])).at(0).n;
  if (kase.outcome !== 'motor_arranco' || identity.external_user_id !== '5215550002471' || identities !== 1) {
    fail('el nativo usa la identidad que ya existe', { row, identity, identities });
  }
  results.dos_formas = { identidad: '521', identidades: identities };
}

// ---------------------------------------------------------------------------
// 10. Los errores: el transitorio se relanza; el trigger diferido no tumba la admision.
// ---------------------------------------------------------------------------
{
  const lead = await member('intencion-transitorio');
  // Un transitorio adentro del flujo se relanza; deshecho, el mismo envio vuelve a entrar como nuevo (no se consumio).
  const transient = async (code) => rolledBack(async () => {
    const form = formOf(lead, new Date((await dbNow()).getTime() - 61 * 60_000));
    const args = [A.tenant, A.funnel, A.bindingVersion, form.id, JSON.stringify(form.raw), JSON.stringify(form.canonical), FC.scope, 1];
    await db.exec('savepoint antes');
    const definition = one(await sql('select pg_get_functiondef($1::regprocedure) as d', [ADMIT_NATIVE]), 'definicion').d;
    await db.exec(definition.replace('    -- El control. Apagado',
      `    raise exception using errcode = '${code}', message = 'transitorio de prueba';\n    -- El control. Apagado`));
    const error = await errorOf(db, () => db.query(ROUTE_SQL, args));
    await db.exec('rollback to savepoint antes');
    const retry = one((await db.query(ROUTE_SQL, args)).rows, 'reintento');
    return { code: error?.code, retry: retry.outcome, ruta: retry.ruta };
  });
  const deadlock = await transient('40P01');
  const busy = await transient('55P03');
  for (const [code, got] of [['40P01', deadlock], ['55P03', busy]]) {
    if (got.code !== code || got.retry !== 'inserted' || got.ruta !== 'nativo') {
      fail(`un ${code} se relanza y el formulario no se consume`, got);
    }
  }
  // Una falla del nativo fuera de un flujo (el manejador de afuera): el legado se queda con el envio.
  const outer = await rolledBack(async () => {
    // La fila del resultado nativo falla al insertarse (despues del bucle por flujo).
    await db.exec(`
      create function pg_temp.e247_fail_native_row() returns trigger language plpgsql as $$
      begin
        if new.ruta = 'nativo' then raise exception using errcode = '22012', message = 'falla de prueba'; end if;
        return new;
      end $$;
      create trigger e247_fail_native_row before insert on public.flow_engine_native_intent_admissions
        for each row execute function pg_temp.e247_fail_native_row();`);
    return submit(lead);
  });
  if (outer.ruta !== 'legado' || flowRow(outer, flowId).reason !== 'error_interno:22012' || outer.plan_reason !== 'first_contact_scheduled') {
    fail('una falla del nativo fuera del flujo deja el envio al legado', outer);
  }
  // Un trigger diferido que falla sobre el caso nativo: se deshace el flujo, no la admision, y planifica el legado.
  const deferred = await rolledBack(async () => {
    await db.exec(`
      create function pg_temp.e247_fail_native_case() returns trigger language plpgsql as $$
      begin
        if new.context ->> 'flow_engine_trigger' = 'intencion' then
          raise exception using errcode = '23514', message = 'diferido de prueba';
        end if;
        return new;
      end $$;
      create constraint trigger e247_fail_native_case after insert on public.recovery_cases
        deferrable initially deferred for each row execute function pg_temp.e247_fail_native_case();`);
    const row = await submit(lead);
    return { row, admissions: await admissionsOf(row.submission_id) };
  });
  if (deferred.row.outcome !== 'inserted' || deferred.row.ruta !== 'legado'
      || flowRow(deferred.row, flowId).reason !== 'error_interno:23514' || deferred.row.plan_reason !== 'first_contact_scheduled'
      || deferred.admissions.length !== 1 || deferred.admissions[0].ruta !== 'legado') {
    fail('un trigger diferido que falla deja la admision y decide el legado', deferred);
  }
  results.errores = { transitorio: deadlock.code, ocupado: busy.code, reintento: deadlock.retry,
    afuera_del_flujo: flowRow(outer, flowId).reason, diferido: flowRow(deferred.row, flowId).reason };
}

// ---------------------------------------------------------------------------
// 10b. La ventana de D21, ya_en_curso, la audiencia perdida, pausado y vencido.
// ---------------------------------------------------------------------------
let noContact = null;
{
  // Pasadas las 24 h del envio de Maria arranca otra vez. RELOJ MOVIDO A MANO: executed_at del toque.
  const window = await rolledBack(async () => {
    await sql(`update public.scheduled_actions set executed_at = clock_timestamp() - interval '25 hours'
      where recovery_case_id = $1`, [maria.caseId]);
    return flowRow(await submit({ ...maria, offer: A.offers.at(-1) }, { minutesAgo: 5 }), flowId).outcome;
  });
  if (window !== 'motor_arranco') fail('pasadas las 24 h del envio, arranca', window);
  // Con el caso nativo abierto: ya_en_curso.
  const open = await fresh('intencion-en-curso');
  const openAgain = await submit({ ...open, offer: A.offers[0] }, { minutesAgo: 5 });
  if (openAgain.ruta !== 'nativo' || flowRow(openAgain, flowId).reason !== 'ya_en_curso') fail('con el caso abierto, ya_en_curso', openAgain);
  // La audiencia perdida: la intencion sin la autorizacion de WhatsApp, sin ningun freno del legado.
  const lost = await fresh('intencion-audiencia-perdida');
  const reevaluated = await rolledBack(async () => {
    await sql('update public.purchase_intents set whatsapp_contact_authorized = false where id = $1', [lost.intent]);
    const decision = await claimAs('audiencia-perdida', lost.actionId);
    const kase = one(await sql('select status from public.recovery_cases where id = $1', [lost.caseId]), 'caso').status;
    return { decision: decision.decision, reason: decision.reason_code, kase };
  });
  if (reevaluated.decision !== 'cancel' || reevaluated.reason !== 'precheckout_authorization_lost' || reevaluated.kase !== 'cancelled') {
    fail('la reevaluacion corta con la audiencia perdida', reevaluated);
  }
  const reservedLost = await reserve({ label: 'intencion-audiencia-perdida', chatwoot: '981001' }, lost.actionId);
  const startLost = await rolledBack(async () => {
    await sql('update public.purchase_intents set whatsapp_contact_authorized = false where id = $1', [lost.intent]);
    const row = await startNew(reservedLost);
    const auth = (await sql('select count(*)::int as n from public.flow_engine_request_authorizations where action_id = $1',
      [lost.actionId])).at(0).n;
    return { outcome: row.outcome, reason: row.reason, auth };
  });
  if (startLost.outcome !== 'rechazado' || startLost.reason !== 'precheckout_authorization_lost' || startLost.auth !== 0) {
    fail('el arranque rechaza con la audiencia perdida, sin cupo', startLost);
  }
  // Pausado (sin toque nativo): el legado, con la fila y el rechazo flujo_pausado.
  const pausedLead = await member('intencion-pausado');
  const pausedRow = await rolledBack(async () => {
    await pauseFlow(flowId, A.funnel, 'primer_contacto');
    const row = await submit(pausedLead);
    const rejection = await sql('select reason from public.flow_engine_start_rejections where source_ref = $1', [row.submission_id]);
    return { row, rejection: rejection.map((r) => r.reason) };
  });
  if (pausedRow.row.ruta !== 'legado' || flowRow(pausedRow.row, flowId).reason !== 'flujo_pausado'
      || pausedRow.row.plan_reason !== 'first_contact_scheduled' || JSON.stringify(pausedRow.rejection) !== '["flujo_pausado"]') {
    fail('con el flujo pausado decide el legado', pausedRow);
  }
  // Un envio vencido (mas viejo que termina.vence): no arranca, del nativo.
  const late = await member('intencion-vencido');
  const expired = await rolledBack(() => submit(late, { minutesAgo: 25 * 60 }));
  if (expired.ruta !== 'nativo' || flowRow(expired, flowId).reason !== 'precheckout_submission_expired' || expired.plan_outcome !== null) {
    fail('un envio vencido no arranca', expired);
  }
  // Para el freno sin contacto de 11: una persona sin contacto con la intencion ya clasificada por el carrito.
  // ESTADO SEMBRADO: la clasificacion (la pone la correlacion de Hotmart, que crearia el contacto).
  noContact = person('intencion-carrito-sin-contacto');
  const first = await submit(noContact, { scope: false, minutesAgo: 90 });
  await sql(`update public.purchase_intents set current_classification = 'confirmed_abandonment' where id = $1`,
    [first.purchase_intent_id]);
  if ((await sql('select 1 from public.contacts where email = $1', [noContact.email])).length !== 0) fail('sin contacto antes de prender');
  results.ventana_y_cortes = { pasadas_24h: window, en_curso: flowRow(openAgain, flowId).reason,
    audiencia_reevaluacion: reevaluated.reason, audiencia_arranque: startLost.reason,
    pausado: flowRow(pausedRow.row, flowId).reason, vencido: flowRow(expired, flowId).reason };
}

// ---------------------------------------------------------------------------
// 12. La adopcion de la respuesta al carrito con un primer contacto nativo ya condenado.
// ---------------------------------------------------------------------------
{
  const lead = await fresh('intencion-adopcion');
  lead.chatwoot = '979001';
  await cartFor(lead, { form: false });
  await planCart(lead);
  const reserved = await reserve(lead, lead.plan.scheduled_action_id);
  const startedCart = await startOld(reserved);
  if (startedCart.id !== reserved.attempt.id && startedCart.attempt_id !== reserved.attempt.id
      && !startedCart.request_started_at) fail('el arranque del carrito', startedCart);
  await accept(lead, reserved);
  const pending = await actionOf(lead.caseId);
  if (pending.status !== 'pending') fail('el primer contacto nativo sigue pendiente', pending.status);
  const adopt = async () => asService(async () => {
    await db.exec('savepoint adopcion');
    let rows = null;
    const error = await errorOf(db, async () => {
      rows = (await db.query('select * from public.admit_portable_inbound_commercial_case_v1($1,$2,$3,$4)',
        [A.inboundScope, A.inboundVersion, Number(lead.chatwoot), lead.phone])).rows;
    });
    if (error) await db.exec('rollback to savepoint adopcion');
    return error ? { error: error.message } : { outcome: rows[0].outcome };
  });
  const red = await withPrevious([ADOPTION], adopt);
  if (red.error !== 'inbound_canonical_conversation_conflict') fail('con la adopcion de antes, frenaba', red);
  // Sin la clasificacion del carrito, el primer contacto todavia va a salir y frena.
  const stillGoing = await rolledBack(async () => {
    await sql('update public.purchase_intents set current_classification = null where id = $1', [lead.intent]);
    return adopt();
  });
  if (stillGoing.error !== 'inbound_canonical_conversation_conflict') fail('el que todavia va a salir frena', stillGoing);
  const adopted = await rolledBack(adopt);
  if (!adopted.outcome || adopted.error) fail('la respuesta al carrito se adopta', adopted);
  results.adopcion = { ahora: adopted.outcome, antes: red.error, sin_clasificacion: stillGoing.error };
}

// ---------------------------------------------------------------------------
// 11. Prendido: el contacto se crea en la admision; sin consentimiento, no.
// ---------------------------------------------------------------------------
{
  await cycle([stateItem(flowId, A.funnel, 'primer_contacto', 'prender')], 'prender el primer contacto');
  const state = one(await sql('select state from public.flow_controls where flow_id = $1', [flowId]), 'control').state;
  if (state !== 'prendido') fail('prender el primer contacto', state);
  const lead = person('intencion-prendido');
  const row = await submit(lead, { scope: false });
  const contact = await sql('select id from public.contacts where email = $1', [lead.email]);
  if (row.ruta !== 'nativo' || flowRow(row, flowId).outcome !== 'motor_arranco' || contact.length !== 1) {
    fail('prendido, el contacto se crea en la admision', { row, contact });
  }
  const noConsent = person('intencion-sin-consentimiento');
  const refused = await submit(noConsent, { scope: false, consent: false });
  const none = await sql('select id from public.contacts where email = $1', [noConsent.email]);
  if (refused.ruta !== 'nativo' || flowRow(refused, flowId).reason !== 'precheckout_intent_not_authorized' || none.length !== 0) {
    fail('sin consentimiento no se crea el contacto', { refused, none });
  }
  // El freno sin contacto corta antes de crearlo (el carrito ya clasificado); rojo: sin esa llamada, se creaba.
  const stopNoContact = async (red) => rolledBack(async () => {
    if (red) {
      const definition = one(await sql('select pg_get_functiondef($1::regprocedure) as d', [ROUTE]), 'entrada').d;
      const call = 'v_reason := public._portable_precheckout_stop_reason(v_intent_id, null);';
      if (!definition.includes(call)) fail('la llamada sin contacto en la entrada');
      await db.exec(definition.replace(call, 'v_reason := null;'));
    }
    const row = await submit({ ...noContact }, { scope: false, minutesAgo: 5 });
    const contacts = (await sql('select count(*)::int as n from public.contacts where email = $1', [noContact.email])).at(0).n;
    return { reason: flowRow(row, flowId).reason, contacts };
  });
  const cut = await stopNoContact(false);
  const cutRed = await stopNoContact(true);
  if (cut.reason !== 'superseded_by_provider_event' || cut.contacts !== 0) fail('el freno corta antes de crear el contacto', cut);
  if (cutRed.contacts !== 1) fail('sin la llamada sin contacto, se creaba el contacto', cutRed);
  results.prendido = { contacto: contact.length, sin_consentimiento: flowRow(refused, flowId).reason,
    freno_sin_contacto: cut.reason, contactos: cut.contacts, sin_la_llamada: cutRed.contacts };
}

// ---------------------------------------------------------------------------
// 13. Los conteos de la simulacion y la historia del AE.
// ---------------------------------------------------------------------------
{
  const since = new Date((await dbNow()).getTime() - 60 * 60_000).toISOString();
  const counts = one(await asService(async () => (await db.query(
    'select public.read_flow_engine_native_trigger_counts_v1($1::jsonb, $2, $3, $4::timestamptz) as r',
    [JSON.stringify(INSTANCE), A.funnel, 'intencion', since])).rows), 'conteos').r;
  const want = one(await sql(`select count(*) filter (where s.contract_version = '1.1.0')::int as n,
      count(*)::int as todos,
      count(distinct lower(s.canonical_payload #>> '{identity,email}')) filter (where s.contract_version = '1.1.0')::int as personas
    from public.precheckout_submissions s
    where s.received_at >= $1 and s.canonical_payload #>> '{source,funnel_ref}' = $2`, [since, A.funnel]), 'formularios');
  // El envio 1.0.0 sin opt-in de 11 esta en la ventana y no cuenta.
  if (counts.events !== want.n || want.todos <= want.n || counts.people !== want.personas || counts.events < 10
      || counts.trigger !== 'intencion') {
    fail('los conteos cuentan los formularios con consentimiento', { counts, want });
  }
  const red = await withPrevious([CHANGED[5]], () => errorOf(db, () => db.query(
    'select public.read_flow_engine_native_trigger_counts_v1($1::jsonb, $2, $3, $4::timestamptz)',
    [JSON.stringify(INSTANCE), A.funnel, 'intencion', since])));
  if (red?.message !== 'flow_engine_native_trigger_invalid') fail('sin el bloque, los conteos no aceptan intencion', red?.message);
  // La marca de instancia de las lecturas del AE (la de validate_automation_expert_native_reads.mjs).
  const AE_INSTANCE = { ...INSTANCE, chatwoot_account_id: A.accountId, chatwoot_inbox_id: A.inboxId,
    hotmart_product_id: String(A.productId), inbound_scope_key: A.inboundScope, agent_scope_ref: 'att1-agente',
    time_zone: A.timezone, scopes: [] };
  const history = await asService(async () => (await db.query(
    'select public.read_automation_expert_native_history_v1($1::jsonb, $2)::text as r', [JSON.stringify(AE_INSTANCE), maria.contact])).rows);
  if (!history[0].r.includes('PRECHECKOUT_FORM_SUBMITTED')) fail('la historia del AE dice el formulario', history[0].r.slice(0, 400));
  results.simulacion = { eventos: counts.events, personas: counts.people, sin_el_bloque: red.message };
}

await db.close();
console.log(JSON.stringify(results, null, 2));
