// La confirmacion de registro a un evento por el motor nativo (4.3, segunda
// parte; migracion 20261127500000; la primera parte es el PR #525).
//
// Por que: desde #525 un formulario propio con tipo = "registro" se admite sin
// el primer contacto comercial y un flujo puede decir
// dispara: {evento: intencion, registro: <clave>}, pero la base no sabia rutear
// el envio a ese flujo y no salia nada. Esta migracion lo rutea sin tocar el
// primer contacto comercial.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera), el primer contacto del legado y el formulario capturado de Maribel
// (tests/fixtures/ghl_form_maribel_20261031.json, anonimizado en el servidor),
// como validate_flow_engine_native_intent.mjs: un formulario propio de registro
// manda el mismo lead.precheckout (la forma del envio de prueba de ATT1 del
// 26/11, tests/fixtures/own_forms/att1_own_form_submission_shape_20261126.json:
// la clave del evento no viaja en el envio, la pasa el bridge). Las personas
// son sinteticas. La plantilla es la de Maribel (maribel_retomar_proceso_01).
//
// Prueba:
//   0. la forma: cambian exactamente las cuatro funciones del encargo, nace la
//      entrada (solo service_role, security definer) y es la ultima; correrla
//      dos veces aborta;
//   1. el disparador y la forma: {evento: intencion, registro: <clave>} es
//      intencion; una clave mal escrita, otro evento o una clave de mas, no; un
//      flujo de registro con dos plantillas es motor_nativo:forma;
//   2. el formulario comercial no toma un flujo de registro (rojo: con la
//      entrada de antes, lo tomaba);
//   3. el registro: abre el caso del flujo de ese evento (context con la clave,
//      el ancla propia, el permiso del formulario), sin plan del legado y sin
//      tocar el flujo comercial; el reenvio devuelve lo guardado; otro evento
//      es sin_flujo y solo admite;
//   4. el freno por salud de Meta: ningun caso, el motivo en la fila y en el
//      rechazo; la forma del freno y de la clave se validan;
//   5. el despacho del registro: el paso con la plantilla y el permiso
//      formulario, el arranque y la aceptacion;
//   6. la separacion (D21): una confirmacion enviada no frena el primer
//      contacto comercial ni al reves (rojo sin el reemplazo); un segundo
//      registro al mismo evento, ya_disparado.
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { buildWorld, errorOf, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261127500000_own_forms_registration_confirmation_v1.sql';
const TZ = 'timestamp with time zone';
const ADMIT_NATIVE = `_flow_engine_admit_native_flow(flow_controls,webhook_events,uuid,text,${TZ},bigint,bigint,text)`;
const ROUTE = 'admit_and_route_portable_lead_precheckout_v1(text,text,integer,text,jsonb,jsonb,text,integer)';
const REGISTRATION = 'admit_and_route_registration_lead_precheckout_v1(text,text,integer,text,jsonb,jsonb,text,text)';
const CHANGED = ['_flow_engine_native_trigger(jsonb)', '_flow_engine_native_unsupported(jsonb)', ROUTE, ADMIT_NATIVE];
const EVENT = 'clase-28-11';
const maribel = JSON.parse(readFileSync(join(root,
  'tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json'), 'utf8')).message_templates[0];
const CAPTURED = JSON.parse(readFileSync(join(root, 'tests/fixtures/ghl_form_maribel_20261031.json'), 'utf8')).envios;
const PILOT = JSON.parse(readFileSync(join(root, 'tests/fixtures/instances/att1/politica-piloto.json'), 'utf8'));
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
// Despues va solo el texto fijo tercero del seguimiento por silencio (20261128100000, U4), que toca solo
// _flow_engine_native_unsupported.
if (names.at(-2) !== MIGRATION || names.at(-1) !== '20261128100000_flow_engine_native_silence_literal_v1.sql') {
  fail(`despues de ${MIGRATION} va solo la del texto fijo del seguimiento`, names.slice(-3));
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
  for (const name of names.slice(0, names.indexOf(MIGRATION))) await applyFile(db, join(root, 'supabase/migrations', name));
  const before = await snapshot(db);
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.has(s) && after.get(s).definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && ['acl', 'prosecdef', 'provolatile', 'proowner'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, CHANGED) || !sameSet(added, [REGISTRATION]) || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  for (const signature of CHANGED) {
    if (!after.get(signature).definition.includes('own_forms_registration')) fail(`${signature}: sin la marca`);
    previous[signature] = before.get(signature).definition;
  }
  const entry = after.get(REGISTRATION);
  if (!entry.service_x || entry.api_x || !entry.prosecdef) fail('la entrada es solo de service_role', entry);
  let error = null;
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  } catch (caught) {
    error = caught;
  }
  if (error?.message !== 'own_forms_registration_confirmation_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { cambiadas: changed.length, nueva: added[0] };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 (buildWorld) con el primer contacto del legado.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, register, cycle, versionItem, dbNow, reserve, startNew, accept, person } = world;
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
const OPERATOR = world.OPERATOR;

// El scope del primer contacto del legado (el de la instancia con otra clave), como el validador de intencion.
const FC = { scope: 'att1-primer-contacto-z2', policy: 'att1-primer-contacto-formulario-z2' };
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
  await world.setState(FC.scope, 1, 'armed');
}

// El formulario capturado, con la persona, la instancia y la oferta del mundo.
let formSerial = 0;
const formOf = (lead, submittedAt) => {
  formSerial += 1;
  const { offer } = lead;
  const P = lead.product ?? A;
  const id = `z2-form-${formSerial}-${lead.email}`;
  const pageUrl = `https://${offer.page_host}${offer.page_path}`;
  const checkoutUrl = `https://pay.hotmart.com/${P.hotlink}?off=${offer.offer_code}&checkoutMode=10`;
  const raw = structuredClone(CAPTURED[0].raw);
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
  const canonical = structuredClone(CAPTURED[0].canonical);
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
  return { id, raw, canonical };
};
const ROUTE_SQL = 'select * from public.admit_and_route_portable_lead_precheckout_v1($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8)';
const REGISTRATION_SQL = 'select * from public.admit_and_route_registration_lead_precheckout_v1($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8)';
const formArgs = async (lead, { minutesAgo, form }) => {
  const at = new Date((await dbNow()).getTime() - minutesAgo * 60_000);
  const used = form ?? formOf(lead, at);
  lead.form = used;
  lead.submittedAt = at;
  return [A.tenant, A.funnel, A.bindingVersion, used.id, JSON.stringify(used.raw), JSON.stringify(used.canonical)];
};
// El formulario comercial, con el scope del legado (ATT1 hoy).
const submit = async (lead, { minutesAgo = 61, form = null } = {}) => one(await asService(async () => (await db.query(
  ROUTE_SQL, [...await formArgs(lead, { minutesAgo, form }), FC.scope, 1])).rows), `${lead.label} formulario`);
// El formulario de registro: lo que llama el bridge con la clave del evento y el freno por salud.
const register_ = async (lead, { event = EVENT, brake = null, minutesAgo = 1, form = null } = {}) => one(
  await asService(async () => (await db.query(REGISTRATION_SQL,
    [...await formArgs(lead, { minutesAgo, form }), event, brake])).rows), `${lead.label} registro`);
const seed = async (lead) => {
  lead.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
    values ($1,$2,$3,'MX') returning id`, [lead.name, lead.email, lead.phone]), 'contacto').id;
  await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1,'email',$2,$2,'system'), ($1,'phone',$3,$3,'system')`, [lead.contact, lead.email, lead.phone]);
  return lead;
};
const flowRow = (row, flowId) => one(row.flujos.filter((f) => f.flow_id === flowId), 'fila del flujo');
const casesOf = async (lead) => sql(`select id, status, source, hotmart_purchase_intent_id, context
  from public.recovery_cases where contact_id = $1 order by created_at`, [lead.contact]);
const actionOf = async (caseId) => one(await sql('select * from public.scheduled_actions where recovery_case_id = $1',
  [caseId]), 'accion');
const legacyPlanOf = async (submission) => (await sql(
  'select outcome, reason_code from public.portable_precheckout_first_contact_plans where submission_id = $1', [submission]))[0] ?? null;

// ---------------------------------------------------------------------------
// Los dos flujos: el primer contacto comercial y la confirmacion del registro.
// ---------------------------------------------------------------------------
const COMMERCIAL = {
  esquema: 'setter-flujo/v1', flujo: 'primer_contacto', producto: A.funnel, nombre: 'Primer contacto tras el formulario',
  estado: 'borrador', dispara: { evento: 'intencion' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ esperar: '60m' }, { plantilla: maribel.name, idioma: 'es_MX', variables: ['nombre', 'producto'] }],
  termina: { si: ['compra'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-primer-contacto-nativo-z2', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona', otra_despues_de: '24h' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
const CONFIRMATION = {
  ...COMMERCIAL, flujo: 'confirmacion_registro', nombre: 'Confirmacion del registro a la clase',
  dispara: { evento: 'intencion', registro: EVENT },
  // La confirmacion sale apenas llega el formulario: sin espera y un solo mensaje.
  pasos: [{ plantilla: maribel.name, idioma: 'es_MX', variables: ['nombre', 'producto'] }],
  topes: { bolsa: 'att1-registro-nativo-z2', por_dia: 500, total: 5000, repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } },
};

// ---------------------------------------------------------------------------
// 1. El disparador y la forma.
// ---------------------------------------------------------------------------
{
  const trigger = async (dispara) => one(await sql('select public._flow_engine_native_trigger($1::jsonb) as t',
    [JSON.stringify({ dispara })]), 'disparador').t;
  const unsupported = async (definition) => one(await sql('select public._flow_engine_native_unsupported($1::jsonb) as r',
    [JSON.stringify(definition)]), 'forma').r;
  const shapes = {
    registro: await trigger({ evento: 'intencion', registro: EVENT }),
    comercial: await trigger({ evento: 'intencion' }),
    clave_mal: await trigger({ evento: 'intencion', registro: 'Clase 28' }),
    clave_corta: await trigger({ evento: 'intencion', registro: 'ab' }),
    clave_numero: await trigger({ evento: 'intencion', registro: 28 }),
    otro_evento: await trigger({ evento: 'carrito', registro: EVENT }),
    clave_de_mas: await trigger({ evento: 'intencion', registro: EVENT, otra: 'x' }),
  };
  if (JSON.stringify(shapes) !== JSON.stringify({ registro: 'intencion', comercial: 'intencion', clave_mal: null,
    clave_corta: null, clave_numero: null, otro_evento: null, clave_de_mas: null })) fail('el disparador del registro', shapes);
  const two = { ...CONFIRMATION, termina: { si: ['compra', 'contesta'], vence: '1d' },
    pasos: [CONFIRMATION.pasos[0], { esperar: '1d' }, CONFIRMATION.pasos[0]] };
  const forms = {
    confirmacion: await unsupported(CONFIRMATION),
    dos_mensajes: await unsupported(two),
    // El comercial de varios toques sigue igual.
    comercial_dos: await unsupported({ ...two, dispara: { evento: 'intencion' } }),
  };
  if (forms.confirmacion !== null || forms.dos_mensajes !== 'motor_nativo:forma' || forms.comercial_dos !== null) {
    fail('la forma de la confirmacion', forms);
  }
  const red = await withPrevious([CHANGED[1]], () => unsupported(two));
  if (red !== null) fail('sin el reemplazo, la base abria una confirmacion de dos mensajes', red);
  results.disparador = { ...shapes, ...forms };
}

const openFlow = async (flowKey, definition) => {
  const flowId = await register(A.funnel, flowKey);
  await cycle([versionItem(flowId, A.funnel, flowKey, definition)], `v1 de ${flowKey}`);
  const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'propuesta').proposal_id;
  const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [flowId, proposalId, A.timezone]), 'abrir').r);
  if (!opened.ok || opened.state !== 'cohorte') fail(`abrir ${flowKey}`, opened);
  return flowId;
};
const commercialId = await openFlow('primer_contacto', COMMERCIAL);
const confirmationId = await openFlow('confirmacion_registro', CONFIRMATION);
const member = async (label) => {
  const lead = await seed(person(label));
  await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $3, 'dan'), ($2, $3, 'dan')`,
    [commercialId, confirmationId, lead.contact]);
  return lead;
};

// ---------------------------------------------------------------------------
// 2. El formulario comercial no toma un flujo de registro.
// ---------------------------------------------------------------------------
{
  const lead = await member('z2-comercial');
  const row = await rolledBack(() => submit(lead));
  const ids = row.flujos.map((f) => f.flow_id);
  if (row.ruta !== 'nativo' || !sameSet(ids, [commercialId]) || flowRow(row, commercialId).outcome !== 'motor_arranco') {
    fail('el formulario comercial va solo al primer contacto comercial', row);
  }
  const red = await withPrevious([ROUTE], () => submit({ ...lead }));
  if (!red.flujos.some((f) => f.flow_id === confirmationId)) fail('con la entrada de antes, tomaba el registro', red.flujos);
  results.comercial = { flujos: ids.length, rojo_tomaba_el_registro: true };
}

// ---------------------------------------------------------------------------
// 3. El registro.
// ---------------------------------------------------------------------------
const ana = await member('z2-registro-ana');
{
  const row = await register_(ana);
  const cell = flowRow(row, confirmationId);
  if (row.ruta !== 'nativo' || row.flujos.length !== 1 || cell.outcome !== 'motor_arranco' || row.plan_outcome !== null) {
    fail('el registro abre la confirmacion', row);
  }
  const kase = one(await casesOf(ana), 'caso');
  const action = await actionOf(kase.id);
  const anchor = one(await sql('select * from public.webhook_events where id = $1', [action.anchor_subject_internal_id]), 'ancla');
  const native = one(await sql('select * from public.flow_engine_native_runs where recovery_case_id = $1', [kase.id]), 'recorrido');
  if (kase.context.registration_event !== EVENT || kase.context.flow_engine_trigger !== 'intencion'
      || kase.source !== 'landing' || kase.hotmart_purchase_intent_id !== row.purchase_intent_id
      || native.trigger_event !== 'intencion' || native.audience_precheckout_submission_id !== row.submission_id) {
    fail('el caso de la confirmacion', { kase, native });
  }
  if (anchor.external_event_id !== `flow-engine-registration:${row.submission_id}` || anchor.source !== 'system'
      || anchor.event_type !== 'PRECHECKOUT_FORM_SUBMITTED') fail('el ancla del registro', anchor);
  // Sin espera: la confirmacion sale apenas llega el formulario (la gracia de la politica es 0).
  if (Math.abs((new Date(action.due_at) - ana.submittedAt) / 60_000) > 0.01 || action.anchor_type !== 'flow_engine_event') {
    fail('la confirmacion sale apenas llega el formulario', action);
  }
  if (await legacyPlanOf(row.submission_id) !== null) fail('el registro nunca planifica el legado');
  const admissions = await sql(`select flow_id, ruta, outcome from public.flow_engine_native_intent_admissions
    where submission_id = $1`, [row.submission_id]);
  if (admissions.length !== 1 || admissions[0].ruta !== 'nativo' || admissions[0].flow_id !== confirmationId) {
    fail('la fila del resultado', admissions);
  }
  const replay = await register_(ana, { form: ana.form });
  if (replay.outcome !== 'duplicate' || replay.ruta !== 'nativo' || flowRow(replay, confirmationId).recovery_case_id !== kase.id
      || (await casesOf(ana)).length !== 1) fail('el reenvio devuelve lo guardado', replay);
  ana.caseId = kase.id;
  ana.actionId = action.id;
  // Otro evento: ningun flujo lo confirma; solo la admision, como hoy.
  const other = await seed(person('z2-otro-evento'));
  const none = await register_(other, { event: 'vivo-30-11' });
  if (none.outcome !== 'inserted' || none.ruta !== 'sin_flujo' || none.flujos.length !== 0
      || (await casesOf(other)).length !== 0 || await legacyPlanOf(none.submission_id) !== null) {
    fail('otro evento: solo la admision', none);
  }
  results.registro = { ruta: row.ruta, salida: cell.outcome, reenvio: replay.outcome, otro_evento: none.ruta };
}

// ---------------------------------------------------------------------------
// 4. El freno por salud de Meta y la forma de la entrada.
// ---------------------------------------------------------------------------
{
  const lead = await member('z2-freno');
  const row = await register_(lead, { brake: 'meta_salud:red' });
  const cell = flowRow(row, confirmationId);
  const rejection = await sql(`select reason from public.flow_engine_start_rejections
    where flow_id = $1 and source_ref = $2`, [confirmationId, row.submission_id]);
  if (row.ruta !== 'nativo' || cell.outcome !== 'motor_no_arranca' || cell.reason !== 'meta_salud:red'
      || (await casesOf(lead)).length !== 0 || rejection.length !== 1 || rejection[0].reason !== 'meta_salud:red') {
    fail('el freno por salud no abre nada', { row, rejection });
  }
  const form = formOf(person('z2-forma'), await dbNow());
  const args = [A.tenant, A.funnel, A.bindingVersion, form.id, JSON.stringify(form.raw), JSON.stringify(form.canonical)];
  const badEvent = await rolledBack(() => errorOf(db, () => db.query(REGISTRATION_SQL, [...args, 'Clase 28', null])));
  const badBrake = await rolledBack(() => errorOf(db, () => db.query(REGISTRATION_SQL, [...args, EVENT, 'roja'])));
  if (badEvent?.message !== 'invalid_registration_event' || badBrake?.message !== 'invalid_health_brake') {
    fail('la forma de la entrada', { badEvent: badEvent?.message, badBrake: badBrake?.message });
  }
  results.freno = { motivo: cell.reason, casos: 0, clave_mal: badEvent.message, freno_mal: badBrake.message };
}

// ---------------------------------------------------------------------------
// 5. El despacho de la confirmacion.
// ---------------------------------------------------------------------------
{
  const reserved = await reserve({ label: 'z2-registro-ana', chatwoot: '881' }, ana.actionId);
  const step = one(await asService(async () => (await db.query('select * from public.flow_engine_execution_context_v1($1)',
    [ana.actionId])).rows), 'contexto del paso');
  if (step.template_name !== maribel.name || step.entry_permission !== 'formulario' || step.step_key !== 'first_contact'
      || JSON.stringify(step.template_variables) !== JSON.stringify(['nombre', 'producto'])) {
    fail('el contexto del paso de la confirmacion', step);
  }
  const started = await startNew(reserved);
  if (started.outcome !== 'request_started') fail('el arranque de la confirmacion', started);
  const accepted = await accept({ label: 'z2-registro-ana', chatwoot: '881' }, reserved);
  results.despacho = { plantilla: step.template_name, permiso: step.entry_permission, aceptacion: accepted.status };
}

// ---------------------------------------------------------------------------
// 6. La separacion entre la confirmacion y el primer contacto comercial (D21).
// ---------------------------------------------------------------------------
{
  // Ana recibio la confirmacion: su formulario comercial arranca el primer contacto (no ya_disparado).
  const commercial = await rolledBack(() => submit({ ...ana }, { minutesAgo: 1 }));
  const cell = flowRow(commercial, commercialId);
  if (commercial.ruta !== 'nativo' || cell.outcome !== 'motor_arranco') {
    fail('una confirmacion enviada no frena el primer contacto comercial', commercial);
  }
  const red = await withPrevious([ADMIT_NATIVE], () => submit({ ...ana }, { minutesAgo: 1 }));
  if (flowRow(red, commercialId).reason !== 'ya_disparado') fail('sin el reemplazo, la confirmacion frenaba', red.flujos);
  // Un segundo registro de Ana al mismo evento: ya_disparado (una confirmacion por persona y evento).
  const again = await register_({ ...ana });
  if (flowRow(again, confirmationId).reason !== 'ya_disparado') fail('un segundo registro al mismo evento', again);
  // Al reves: con el primer contacto comercial enviado, la confirmacion sale igual.
  const bea = await member('z2-comercial-bea');
  const sold = await submit(bea);
  const soldCase = flowRow(sold, commercialId).recovery_case_id;
  const soldAction = (await actionOf(soldCase)).id;
  const reserved = await reserve({ label: 'z2-comercial-bea', chatwoot: '882' }, soldAction);
  await startNew(reserved);
  await accept({ label: 'z2-comercial-bea', chatwoot: '882' }, reserved);
  const confirmation = await register_({ ...bea });
  if (flowRow(confirmation, confirmationId).outcome !== 'motor_arranco') {
    fail('el primer contacto comercial enviado no frena la confirmacion', confirmation);
  }
  results.separacion = { comercial_tras_confirmacion: cell.outcome, rojo: 'ya_disparado',
    segundo_registro: flowRow(again, confirmationId).reason,
    confirmacion_tras_comercial: flowRow(confirmation, confirmationId).outcome };
}

await db.close();
console.log(JSON.stringify(results, null, 2));
