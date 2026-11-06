// Las cuatro ramas del pago rechazado en el motor nativo, y el boton del
// checkout en su paso (migracion 20261103000900, P-16, E-306, fase A del plan
// de E-302: A4; flow-engine-v2 §27).
//
// Por que: Dan decidio el 2026-11-02 que el pago rechazado de Maribel lleve las
// cuatro versiones de ATT1 (D1 «Las 4»), por el motor nativo (D4), con el boton
// de nuestro link (D5) y el cupon fijo PROTOCOLO10 (D10). La base no abria una
// condicion ni un cupon fijo en un flujo nativo, y la reserva del link no
// aceptaba una accion nativa.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera): el formulario con consentimiento y los rechazos pasan por las
// admisiones portables de hoy, que los correlacionan con la intencion, como en
// produccion. Los rechazos son los capturados de Maribel
// (tests/fixtures/hotmart_purchase_canceled_maribel_20261028.json): uno de saldo
// insuficiente y dos de la misma persona, negados por el banco, con 24 s de
// diferencia. Las plantillas de las ramas son las cuatro de Maribel,
// reconstruidas del pedido a Meta
// (chatwoot_message_templates_inbox_13_maribel_pago_rechazado_reconstruida_20261102.json).
//
// Prueba:
//   0. la forma: cambian exactamente las funciones del encargo (el contexto se
//      vuelve a crear con una columna mas) y nacen las nuevas, con los permisos
//      del contrato; correrla dos veces aborta;
//   1. la cadena de Maribel se abre y compila a la politica de hoy (gracia 15
//      min, 09:00-21:00 CDMX, vence a 1 dia, un mensaje); rojos: otra forma de
//      cadena es motor_nativo:forma, y sin la migracion la cadena no se abria
//      ni compilaba, y el cupon fijo tampoco; compile_cases.json compila igual;
//   2. los tres datos: saldo insuficiente da sin_saldo, dos rechazos dan varios,
//      un envio nuestro aceptado antes da descuento_previo si, y todo lo demas
//      banco; el contexto del paso trae las cuatro ramas (con el rojo: sin la
//      migracion, la cadena no tenia envio);
//   3. el registro de la eleccion: la rama que corresponde se registra antes del
//      envio y se repite igual; una rama que no corresponde (stale), otra rama
//      para el mismo intento (conflict), un indice que no existe, y el registro
//      inmutable;
//   4. el link: la reserva acepta el primer envio nativo y va a la oferta del
//      rechazo, con el rojo de la definicion de antes; el envio sale y las
//      ventas por rama lo cuentan;
//   5. lo de antes, igual: el pago rechazado nativo de un paso sin boton tiene
//      el mismo contexto (template_branches nulo) y las ventas de antes son las
//      mismas, con by_branch vacio.
//
// Con --emit escribe tests/fixtures/flow_engine/native_payment_failure_branches_rows.json
// (las filas del contexto, de los datos, del registro y de la reserva, como las
// contesta la base) para tests/test_flow_engine_native_payment_failure_branches.py;
// con --check (el de npm test) lo compara y falla si difiere.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, errorOf, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261103000900_flow_engine_native_payment_failure_branches_v1.sql';
const FIXTURE = join(root, 'tests/fixtures/flow_engine/native_payment_failure_branches_rows.json');
const EMIT = process.argv.includes('--emit');
const CHECK = process.argv.includes('--check');
const TZ = 'timestamp with time zone';
const UNSUPPORTED = '_flow_engine_native_unsupported(jsonb)';
const STEPS = '_flow_engine_native_steps(text)';
const CORE = '_flow_engine_compile_policy_core(jsonb,text,integer,text,text)';
const SENDS = '_flow_engine_sends(jsonb)';
const CONTEXT = 'flow_engine_execution_context_v1(uuid)';
const RESERVE = `reserve_recovery_checkout_issuance_v1(uuid,text,bigint,uuid,text,${TZ})`;
const SALES = `read_flow_sales_v1(jsonb,${TZ})`;
const CHANGED = [UNSUPPORTED, STEPS, CORE, SENDS, CONTEXT, RESERVE, SALES];
const DATA_RPC = `get_flow_engine_native_payment_failure_context_v1(uuid,text,bigint,${TZ})`;
const RECORD_RPC = `record_flow_engine_native_branch_v1(uuid,uuid,text,bigint,integer,${TZ})`;
const ENTRYPOINTS = [DATA_RPC, RECORD_RPC];
const PRIVATE = [
  '_flow_engine_native_payment_failure_send_ok(jsonb)',
  '_flow_engine_native_payment_failure_branches(jsonb)',
  `_flow_engine_native_payment_failure_data(uuid,${TZ},${TZ})`,
  `_flow_engine_native_payment_failure_action(uuid,text,bigint,${TZ})`,
  '_flow_engine_native_payment_failure_choice(jsonb,text,text,text)',
];
const MARIBEL_POLICY = {
  timezone: 'America/Mexico_City',
  business_windows: [{ end: '21:00', days: [1, 2, 3, 4, 5, 6, 7], start: '09:00' }],
  grace_period: '00:15:00', expires_after: '1 day', max_automatic_messages: 1,
};
const fixtureFile = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));
const TEMPLATES = fixtureFile('chatwoot_message_templates_inbox_13_maribel_pago_rechazado_reconstruida_20261102.json')
  .message_templates;
const REFUSALS = fixtureFile('hotmart_purchase_canceled_maribel_20261028.json').eventos;
const COMPILE_CASES = fixtureFile('flow_engine/compile_cases.json').cases;
const PURCHASE = fixtureFile('hotmart_purchase_approved_att1_bienvenida_20261026.json').payload;
const results = {};
const emitted = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues va solo el aviso de derivaciones sin atender (20261103001000, E-309).
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261103001000_handoff_wait_alerts_v1.sql', '20261103001300_flow_engine_native_segment_label_v1.sql', '20261103001500_segment_entry_answers_v1.sql',
  // El origen de un recorrido nativo para la campana por landing (20261103001700, E-321): una lectura de service_role.
  '20261103001700_flow_engine_native_campaign_origin_v1.sql',
  // El cierre del caso por el cupon de una campana (20261106000100, E-339): una RPC de service_role.
  '20261106000100_recovery_case_segment_coupon_close_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la del aviso de derivaciones sin atender`, names.slice(position + 1));
}
if (TEMPLATES.length !== 4 || REFUSALS.length !== 3 || REFUSALS[1].persona !== REFUSALS[2].persona) {
  fail('los fixtures no son las cuatro plantillas y los tres rechazos de Maribel');
}

// La cadena de E-302 (§5.1), en el orden de ATT1, con el cupon fijo de Maribel (D10).
const BUTTON = { link: 'checkout', cupon: 'PROTOCOLO10' };
const send = (branch) => ({ plantilla: `maribel_pago_rechazado_${branch}_01`, idioma: 'es_MX',
  variables: ['nombre', 'producto'], boton: { ...BUTTON } });
const chain = (last = [send('banco')]) => ({
  condicion: { dato: 'descuento_previo', es: ['si'] }, entonces: [send('cupon')],
  si_no: [{ condicion: { dato: 'rechazos', es: ['varios'] }, entonces: [send('dos_veces')],
    si_no: [{ condicion: { dato: 'tipo_de_rechazo', es: ['sin_saldo'] }, entonces: [send('saldo')], si_no: last }] }],
});
const BRANCH_TEMPLATES = ['cupon', 'dos_veces', 'saldo', 'banco'].map((b) => `maribel_pago_rechazado_${b}_01`);
for (const template of TEMPLATES) {
  if (!BRANCH_TEMPLATES.includes(template.name) || template.status !== 'APPROVED') fail('las plantillas del fixture', template.name);
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
  for (const name of names.slice(0, position)) await applyFile(db, join(root, 'supabase/migrations', name));
  const before = await snapshot(db);
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.has(s) && after.get(s).definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  // El contexto se vuelve a crear (una columna mas): su dueno y sus permisos se miran aparte.
  const attributes = [...before.keys()].filter((s) => s !== CONTEXT && after.has(s)
    && ['acl', 'prosecdef', 'provolatile', 'proowner'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, CHANGED) || !sameSet(added, [...ENTRYPOINTS, ...PRIVATE]) || removed.length !== 0
      || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  for (const signature of [CONTEXT, ...ENTRYPOINTS]) {
    const row = after.get(signature);
    if (!row.prosecdef || !row.service_x || row.api_x) fail(`${signature}: solo de service_role`, row);
  }
  if (after.get(CONTEXT).prosecdef !== before.get(CONTEXT).prosecdef
      || after.get(CONTEXT).provolatile !== before.get(CONTEXT).provolatile) {
    fail('el contexto vuelve a crearse con los mismos atributos');
  }
  for (const signature of PRIVATE) {
    const row = after.get(signature);
    if (row.service_x || row.api_x) fail(`${signature}: no es de nadie de la API`, row);
  }
  for (const signature of CHANGED) {
    if (!after.get(signature).definition.includes('flow_engine_native_payment_failure')) fail(`${signature}: sin la marca`);
    previous[signature] = before.get(signature).definition;
  }
  const table = one((await db.query(`select relrowsecurity as rls,
      has_table_privilege('service_role', 'public.flow_engine_native_branch_choices', 'select') as service_select,
      has_table_privilege('anon', 'public.flow_engine_native_branch_choices', 'select')
        or has_table_privilege('authenticated', 'public.flow_engine_native_branch_choices', 'select') as api_select
    from pg_class where oid = 'public.flow_engine_native_branch_choices'::regclass`)).rows, 'tabla');
  if (!table.rls || table.service_select || table.api_select) fail('la tabla de las elecciones, con RLS y sin roles', table);
  let error = null;
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  } catch (caught) {
    error = caught;
  }
  if (error?.message !== 'flow_engine_native_payment_failure_branches_already_applied') {
    fail('correrla dos veces tendria que abortar', error?.message ?? 'paso');
  }
  await db.exec('rollback').catch(() => {});
  if ((await snapshot(db)).get(CORE).definition !== after.get(CORE).definition) fail('la segunda corrida cambio algo');
  // compile_cases.json compila igual antes y despues.
  const compileAll = async (core) => {
    const out = [];
    await db.exec('begin');
    try {
      if (core) await db.exec(core);
      for (const item of COMPILE_CASES) {
        const input = item.input;
        await db.exec('savepoint c');
        try {
          const row = (await db.query(`select p.steps, p.grace_period::text as grace, p.expires_after::text as expires,
              p.max_automatic_messages as sends, p.timezone, p.business_windows
            from public._flow_engine_compile_policy_core($1::jsonb, $2, $3, $4, $5) p`,
          [JSON.stringify(input.definition), input.flow_id, input.version, input.legacy_kind, input.fixed_zone])).rows[0];
          out.push({ id: item.id, row });
          await db.exec('release savepoint c');
        } catch (caught) {
          out.push({ id: item.id, error: caught.message });
          await db.exec('rollback to savepoint c');
        }
      }
    } finally {
      await db.exec('rollback');
    }
    return out;
  };
  const now = await compileAll(null);
  const then = await compileAll(previous[CORE]);
  if (JSON.stringify(now) !== JSON.stringify(then)) fail('compile_cases.json compila distinto despues de la migracion');
  results.forma = { cambiadas: changed.length, nuevas: added.length, casos_de_compilacion: now.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 (buildWorld, la cadena entera).
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, dbNow, reserve, startNew, startOld, accept, person, admitForm,
  cartFor, planCart } = world;
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

const MARIBEL = {
  esquema: 'setter-flujo/v1', flujo: 'pago_rechazado', producto: A.funnel, estado: 'borrador', nombre: 'Pago rechazado',
  dispara: { evento: 'pago_fallido' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ esperar: '15m' }, chain()],
  termina: { si: ['compra'], vence: '1d' },
  horario: { zona: 'America/Mexico_City', desde: '09:00', hasta: '21:00' },
  topes: { bolsa: 'att1-pago-rechazado-ramas', por_dia: 20, total: 1000,
    repeticion: { una_por: 'persona', otra_despues_de: '72h' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
// En el mundo de prueba, con horario siempre: el despacho no depende de la hora en que corre.
const PAYMENT = { ...MARIBEL, horario: 'siempre' };
const unsupportedOf = async (definition) => one(await sql('select public._flow_engine_native_unsupported($1::jsonb) as r',
  [JSON.stringify(definition)]), 'lo que no ejecuta').r;
const compileOf = async (definition) => one(await sql(`select timezone, business_windows,
    case when grace_period = interval '15 minutes' then '00:15:00' else grace_period::text end as grace_period,
    case when expires_after = interval '1 day' then '1 day' else expires_after::text end as expires_after,
    max_automatic_messages, steps
  from public._flow_engine_compile_policy_core($1::jsonb, '00000000-0000-4000-8000-0000000e3061', 1, 'nativo:pago_fallido',
    'America/Mexico_City')`, [JSON.stringify(definition)]), 'politica');

// ---------------------------------------------------------------------------
// 1. La cadena se abre y compila a la politica de hoy.
// ---------------------------------------------------------------------------
{
  if (await unsupportedOf(MARIBEL) !== null) fail('la cadena de Maribel se abre', await unsupportedOf(MARIBEL));
  const compiled = await compileOf(MARIBEL);
  const got = { timezone: compiled.timezone, business_windows: compiled.business_windows, grace_period: compiled.grace_period,
    expires_after: compiled.expires_after, max_automatic_messages: compiled.max_automatic_messages };
  if (JSON.stringify(got) !== JSON.stringify(MARIBEL_POLICY) || compiled.steps.length !== 1
      || compiled.steps[0].step_key !== 'first_contact') {
    fail('la politica de la cadena de Maribel', { got, steps: compiled.steps });
  }
  // Un paso con el boton y el cupon fijo, sin la cadena, tambien (D5).
  const single = { ...MARIBEL, pasos: [{ esperar: '15m' }, send('banco')] };
  if (await unsupportedOf(single) !== null || (await compileOf(single)).max_automatic_messages !== 1) {
    fail('un paso con el boton del checkout se abre y compila');
  }
  const branches = one(await sql('select public._flow_engine_native_payment_failure_branches($1::jsonb) as b',
    [JSON.stringify(MARIBEL)]), 'ramas').b;
  if (JSON.stringify(branches.map((b) => b.paso.plantilla)) !== JSON.stringify(BRANCH_TEMPLATES)
      || branches[3].cuando !== null || branches[0].cuando.dato !== 'descuento_previo') {
    fail('las ramas, en el orden de ATT1', branches);
  }
  // Rojos de la forma, uno por uno.
  const reds = {
    ventana: { ...MARIBEL, pasos: [{ esperar: '15m' }, { ...chain(), condicion: { dato: 'ventana', es: ['abierta'] } }] },
    valor: { ...MARIBEL, pasos: [{ esperar: '15m' }, { ...chain(), condicion: { dato: 'rechazos', es: ['dos'] } }] },
    cuatro_niveles: { ...MARIBEL, pasos: [{ esperar: '15m' }, chain([{ condicion: { dato: 'rechazos', es: ['uno'] },
      entonces: [send('banco')], si_no: [send('banco')] }])] },
    espera_en_rama: { ...MARIBEL, pasos: [{ esperar: '15m' }, { ...chain(), entonces: [{ esperar: '1h' }, send('cupon')] }] },
    rama_vacia: { ...MARIBEL, pasos: [{ esperar: '15m' }, { ...chain(), entonces: [] }] },
    cupon_personal: { ...MARIBEL, pasos: [{ esperar: '15m' }, { ...chain(), entonces: [{ ...send('cupon'),
      boton: { link: 'checkout', cupon: 'personal' } }] }] },
    link_ficha: { ...MARIBEL, pasos: [{ esperar: '15m' }, { ...send('banco'), boton: { link: 'ficha' } }] },
    cadena_no_al_final: { ...MARIBEL, pasos: [chain(), { esperar: '15m' }] },
    otro_disparador: { ...MARIBEL, dispara: { evento: 'carrito' } },
  };
  const shapes = {};
  for (const [label, definition] of Object.entries(reds)) {
    shapes[label] = await unsupportedOf(definition);
    if (shapes[label] !== 'motor_nativo:forma') fail(`la cadena ${label} es motor_nativo:forma`, shapes[label]);
  }
  const compileRed = await errorOf(db, () => compileOf(reds.espera_en_rama));
  if (compileRed?.message !== 'flow_policy_not_compilable:ramas_invalidas') fail('el compilador frena otra cadena', compileRed?.message);
  // Rojos: sin la migracion, la cadena no se abria, no compilaba, y el cupon fijo tampoco.
  const before = await withPrevious([UNSUPPORTED], () => unsupportedOf(MARIBEL));
  const beforeCompile = await withPrevious([CORE], () => errorOf(db, () => compileOf(MARIBEL)));
  const beforeCoupon = await withPrevious([CORE, STEPS], () => errorOf(db, () => compileOf(single)));
  if (before !== 'motor_nativo:forma' || beforeCompile?.message !== 'compile_policy_step_not_supported:condicion'
      || beforeCoupon?.message !== 'flow_policy_not_compilable:fixed_coupon:first_contact') {
    fail('sin la migracion, la cadena y el cupon fijo no se abrian', { before, beforeCompile, beforeCoupon });
  }
  results.politica = { ...got, rojos: Object.keys(shapes).length, sin_la_migracion: before };
}

// ---------------------------------------------------------------------------
// El flujo, la cohorte y las personas.
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
const flowId = await openNative('pago_rechazado', PAYMENT);
const cohort = async (contactId) => sql(
  `insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`, [flowId, contactId]);
const leadFor = async (label) => {
  const lead = person(label);
  await admitForm(lead, new Date((await dbNow()).getTime() - 50 * 60_000));
  return lead;
};
let serial = 0;
const refusalOf = async (lead, { index = 0, at = null } = {}) => {
  serial += 1;
  const payload = structuredClone(REFUSALS[index].payload);
  const moment = at ?? new Date((await dbNow()).getTime() - 10 * 60_000);
  payload.id = `ramas-rechazo-${lead.email}-${serial}`;
  payload.creation_date = moment.getTime();
  payload.data.buyer = { ...payload.data.buyer, name: lead.name, email: lead.email, checkout_phone: lead.phone };
  payload.data.product = { ...payload.data.product, id: A.productId, name: A.productName };
  payload.data.purchase.offer.code = lead.offer.offer_code;
  payload.data.purchase.transaction = `HPRAMAS${String(serial).padStart(4, '0')}`;
  const admitted = one(await sql('select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone]), `${lead.label} rechazo`);
  if (admitted.outcome !== 'inserted') fail(`${lead.label} rechazo`, admitted);
  if (!lead.contact) {
    const known = await sql(`select contact_id from public.contact_points where type = 'email' and normalized_value = $1`,
      [lead.email]);
    if (known.length > 0) {
      lead.contact = known[0].contact_id;
    } else {
      lead.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
        values ($1,$2,$3,'MX') returning id`, [lead.name, lead.email, lead.phone]), 'contacto').id;
      await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id)
        values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)`,
      [lead.contact, lead.email, lead.phone, admitted.webhook_event_id]);
    }
  }
  lead.event = admitted.webhook_event_id;
  lead.moment = moment.toISOString();
  return lead;
};
const admitNative = async (lead) => {
  const rows = await asService(async () => (await db.query('select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
    [JSON.stringify(INSTANCE), JSON.stringify({
      fuente: 'hotmart', tipo: 'pago_fallido', evento_id: lead.event, producto_ref: A.funnel, oferta: lead.offer.offer_code,
      contacto_id: lead.contact, momento: lead.moment, chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
      destino: lead.phone })])).rows);
  return one(rows.filter((row) => row.flow_id === flowId), `${lead.label} admision`);
};
const contextOf = async (actionId) => one(await asService(async () => (await db.query(
  'select * from public.flow_engine_execution_context_v1($1)', [actionId])).rows), 'contexto del paso');
const dataOf = async (reserved) => one(await asService(async () => (await db.query(
  'select * from public.get_flow_engine_native_payment_failure_context_v1($1,$2,$3,$4)',
  [reserved.actionId, reserved.worker, reserved.lease, reserved.now])).rows), 'datos');
const recordOf = async (reserved, index) => one(await asService(async () => (await db.query(
  'select * from public.record_flow_engine_native_branch_v1($1,$2,$3,$4,$5,$6)',
  [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, index, reserved.now])).rows), 'registro');
const choiceOf = (data) => {
  const value = { tipo_de_rechazo: data.tipo_de_rechazo, rechazos: data.rechazos, descuento_previo: data.descuento_previo };
  const branches = MARIBEL.pasos[1];
  let node = branches;
  for (let index = 0; index < 3; index += 1) {
    if (node.condicion.es.includes(value[node.condicion.dato])) return index;
    node = node.si_no[0];
    if (!node.condicion) return index + 1;
  }
  return 3;
};

// ---------------------------------------------------------------------------
// 2. Los tres datos, y el contexto del paso con sus ramas.
// ---------------------------------------------------------------------------
const people = {};
{
  // Saldo insuficiente, un rechazo, sin descuento antes: la rama del saldo.
  people.saldo = await refusalOf(await leadFor('ramas-saldo'), { index: 0 });
  // Negado por el banco, un rechazo: todo lo demas.
  people.banco = await refusalOf(await leadFor('ramas-banco'), { index: 1 });
  // Dos rechazos de la misma persona con 24 s de diferencia: varios.
  const gap = REFUSALS[2].payload.creation_date - REFUSALS[1].payload.creation_date;
  const firstAt = new Date((await dbNow()).getTime() - 10 * 60_000);
  people.dos = await refusalOf(await leadFor('ramas-dos'), { index: 1, at: firstAt });
  // Un envio nuestro aceptado antes del rechazo (el A1 del carrito del legado): descuento_previo si.
  const previo = person('ramas-previo');
  await cartFor(previo, { form: true });
  await planCart(previo);
  const a1 = await reserve(previo, previo.plan.scheduled_action_id);
  await startOld(a1);
  await accept(previo, a1);
  await sql(`update public.followup_delivery_attempts set accepted_at = accepted_at - interval '2 hours' where id = $1`,
    [a1.attempt.id]);
  people.previo = await refusalOf(previo, { index: 1 });
  for (const lead of Object.values(people)) await cohort(lead.contact);
  for (const lead of Object.values(people)) {
    lead.start = await admitNative(lead);
    if (lead.start.outcome !== 'motor_arranco') fail(`${lead.label} arranca`, lead.start);
  }
  await refusalOf(people.dos, { index: 2, at: new Date(firstAt.getTime() + gap) });
  const second = await admitNative(people.dos);
  if (second.reason !== 'ya_en_curso') fail('el segundo rechazo se suma al recorrido', second);

  const context = await contextOf(people.banco.start.scheduled_action_id);
  if (context.template_name !== 'maribel_pago_rechazado_banco_01' || context.entry_permission !== 'formulario'
      || JSON.stringify(context.template_button) !== JSON.stringify(BUTTON)
      || JSON.stringify((context.template_branches ?? []).map((b) => b.paso.plantilla)) !== JSON.stringify(BRANCH_TEMPLATES)) {
    fail('el contexto del paso con sus ramas', context);
  }
  emitted.step_context = context;
  // Rojo: sin la migracion, la cadena no tenia envio y el contexto no contestaba.
  const red = await withPrevious([SENDS], async () => (await asService(async () => (await db.query(
    'select * from public.flow_engine_execution_context_v1($1)', [people.banco.start.scheduled_action_id])).rows)).length);
  if (red !== 0) fail('sin la migracion, la cadena no tenia envio', red);

  const expected = {
    saldo: { tipo_de_rechazo: 'sin_saldo', rechazos: 'uno', descuento_previo: 'no', refusal_count: 1 },
    banco: { tipo_de_rechazo: 'banco', rechazos: 'uno', descuento_previo: 'no', refusal_count: 1 },
    dos: { tipo_de_rechazo: 'banco', rechazos: 'varios', descuento_previo: 'no', refusal_count: 2 },
    previo: { tipo_de_rechazo: 'banco', rechazos: 'uno', descuento_previo: 'si', refusal_count: 1 },
  };
  const branchOf = { saldo: 2, banco: 3, dos: 1, previo: 0 };
  results.datos = {};
  for (const [key, lead] of Object.entries(people)) {
    lead.reserved = await reserve({ label: lead.label, chatwoot: String(951000 + serial++) }, lead.start.scheduled_action_id);
    const data = await dataOf(lead.reserved);
    const got = { tipo_de_rechazo: data.tipo_de_rechazo, rechazos: data.rechazos, descuento_previo: data.descuento_previo,
      refusal_count: data.refusal_count };
    if (JSON.stringify(got) !== JSON.stringify(expected[key]) || choiceOf(data) !== branchOf[key]) {
      fail(`los datos de ${key}`, { got, want: expected[key], rama: choiceOf(data) });
    }
    if (new Date(data.latest_failed_at).getTime() > Date.now() + 60_000) fail('el ultimo rechazo, en el pasado', data);
    results.datos[key] = `${data.tipo_de_rechazo}/${data.rechazos}/${data.descuento_previo} -> ${BRANCH_TEMPLATES[branchOf[key]]}`;
    emitted[`data_${key}`] = { ...got };
  }
  // Rojo: sin el envio aceptado antes, descuento_previo es no.
  const noPrevious = await rolledBack(async () => {
    await sql('update public.followup_delivery_attempts set accepted_at = now() + interval \'1 hour\' where id = $1',
      [a1.attempt.id]);
    return (await dataOf(people.previo.reserved)).descuento_previo;
  });
  if (noPrevious !== 'no') fail('un envio despues del rechazo no es un descuento previo', noPrevious);
  // El dato pide una accion nativa del pago rechazado.
  const legacy = await errorOf(db, () => asService(async () => db.query(
    'select * from public.get_flow_engine_native_payment_failure_context_v1($1,$2,$3,$4)',
    [a1.actionId, a1.worker, a1.lease, a1.now])));
  if (!['flow_engine_native_payment_failure_action_required', 'current_action_lease_not_found'].includes(legacy?.message)) {
    fail('los datos solo de una accion nativa del pago rechazado', legacy?.message);
  }
}

// ---------------------------------------------------------------------------
// 3. El registro de la eleccion, antes del envio.
// ---------------------------------------------------------------------------
{
  const lead = people.banco;
  const stale = await errorOf(db, () => recordOf(lead.reserved, 2));
  if (stale?.message !== 'flow_engine_native_branch_stale') fail('una rama que no corresponde', stale?.message);
  const unknown = await errorOf(db, () => recordOf(lead.reserved, 4));
  if (unknown?.message !== 'invalid_flow_engine_native_branch') fail('un indice que no existe', unknown?.message);
  const recorded = await recordOf(lead.reserved, 3);
  if (recorded.outcome !== 'recorded' || recorded.template_name !== 'maribel_pago_rechazado_banco_01') {
    fail('la eleccion se registra', recorded);
  }
  const replay = await recordOf(lead.reserved, 3);
  if (replay.outcome !== 'already_recorded') fail('la eleccion se repite igual', replay);
  const conflict = await errorOf(db, () => recordOf(lead.reserved, 2));
  if (conflict?.message !== 'flow_engine_native_branch_conflict') fail('otra rama para el mismo intento', conflict?.message);
  const row = one(await sql('select * from public.flow_engine_native_branch_choices where delivery_attempt_id = $1',
    [lead.reserved.attempt.id]), 'eleccion');
  if (row.flow_id !== flowId || row.branch_index !== 3 || row.tipo_de_rechazo !== 'banco' || row.rechazos !== 'uno'
      || row.descuento_previo !== 'no') {
    fail('la fila de la eleccion', row);
  }
  const immutable = await errorOf(db, () => db.query(
    `update public.flow_engine_native_branch_choices set branch_index = 2 where delivery_attempt_id = $1`,
    [lead.reserved.attempt.id]));
  if (immutable?.message !== 'flow_engine_native_mutation_rejected') fail('la eleccion no se reescribe', immutable?.message);
  for (const [key, index] of [['saldo', 2], ['dos', 1], ['previo', 0]]) {
    const other = await recordOf(people[key].reserved, index);
    if (other.outcome !== 'recorded' || other.template_name !== BRANCH_TEMPLATES[index]) fail(`la eleccion de ${key}`, other);
  }
  emitted.record = { outcome: recorded.outcome, template_name: recorded.template_name };
  emitted.record_replay = { outcome: replay.outcome, template_name: replay.template_name };
  emitted.record_stale = { code: stale.code, message: stale.message };
  results.registro = { registrada: recorded.template_name, repetida: replay.outcome, vieja: stale.message,
    conflicto: conflict.message };
}

// ---------------------------------------------------------------------------
// 4. El link, el envio y las ventas por rama.
// ---------------------------------------------------------------------------
{
  const lead = people.banco;
  const reserveLink = async (reserved, ulid) => one(await asService(async () => (await db.query(
    'select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)',
    [reserved.actionId, reserved.worker, reserved.lease, reserved.attempt.id, ulid, reserved.now])).rows), 'link');
  // (como el dueno: el rojo corre adentro de una transaccion que se deshace, sin cambiar de rol)
  const red = await withPrevious([RESERVE], () => errorOf(db, () => db.query(
    'select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)',
    [lead.reserved.actionId, lead.reserved.worker, lead.reserved.lease, lead.reserved.attempt.id,
      '01JBRAMAS0000000000000000A', lead.reserved.now])));
  if (red?.message !== 'recovery_checkout_issuance_requires_cart_first_contact') fail('sin la migracion, la reserva no aceptaba el nativo', red?.message);
  const link = await reserveLink(lead.reserved, '01JBRAMAS0000000000000000B');
  const issuance = one(await sql('select * from public.checkout_link_issuances where id = $1', [link.issuance_id]), 'emision');
  if (link.outcome !== 'reserved' || !link.checkout_url_final.includes(`off=${lead.offer.offer_code}`)
      || issuance.offer_resolution !== 'recovery_case_offer' || issuance.lead_offer_code !== lead.offer.offer_code
      || issuance.recovery_case_id !== lead.start.recovery_case_id || issuance.source_kind !== 'recovery_first_touch') {
    fail('el link del pago rechazado nativo va a la oferta del rechazo', { link, issuance });
  }
  const again = await reserveLink(lead.reserved, '01JBRAMAS0000000000000000C');
  if (again.outcome !== 'reserved' || again.issuance_id !== link.issuance_id) fail('la reserva es idempotente por intento', again);
  emitted.reservation = { outcome: link.outcome, offer_resolution: issuance.offer_resolution,
    url_shape: link.checkout_url_final.replace(/sck=[^&]+/, 'sck=<sck>') };
  const started = await startNew(lead.reserved);
  if (started.outcome !== 'request_started') fail('el envio nativo arranca', started);
  // El link se autoriza justo antes del envio y se cierra con la aceptacion, como el de B1.
  const authorized = one(await asService(async () => (await db.query(
    'select * from public.authorize_recovery_checkout_issuance_v1($1,$2,$3,$4,$5)',
    [link.issuance_id, lead.reserved.actionId, lead.reserved.worker, lead.reserved.lease, await dbNow()])).rows), 'autorizar');
  if (authorized.outcome !== 'request_started') fail('el link del envio nativo se autoriza', authorized);
  const acceptedSend = await accept({ label: lead.label, chatwoot: '952001' }, lead.reserved);
  const finalized = one(await asService(async () => (await db.query(
    'select * from public.finalize_chatwoot_checkout_issuance_v2($1,$2,$3,null,$4)',
    [link.issuance_id, 'accepted_by_chatwoot', acceptedSend.accepted_message_id ?? 777001, await dbNow()])).rows), 'cerrar link');
  if (finalized.outcome !== 'finalized') fail('el link se cierra aceptado', finalized);
  emitted.authorization = { outcome: authorized.outcome };
  // La compra despues del envio (la capturada de la bienvenida de ATT1, con la persona sembrada).
  {
    const payload = structuredClone(PURCHASE);
    const now = await dbNow();
    const shift = now.getTime() - 30_000 - payload.creation_date;
    for (const key of ['approved_date', 'order_date']) payload.data.purchase[key] += shift;
    payload.creation_date += shift;
    payload.id = `ramas-compra-${lead.email}`;
    payload.data.buyer = { ...payload.data.buyer, name: lead.name, email: lead.email, checkout_phone: lead.phone };
    payload.data.product = { ...payload.data.product, id: A.productId };
    payload.data.purchase.offer.code = lead.offer.offer_code;
    payload.data.purchase.transaction = 'HPRAMASCOMPRA';
    const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
      [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone]), 'compra');
    if (admitted.outcome !== 'inserted') fail('admitir la compra', admitted);
    await sql('select * from public.apply_hotmart_purchase_approved($1,$2,$3,$4,$5,$6,$7::timestamptz)',
      [admitted.webhook_event_id, lead.email, lead.phone, String(A.productId), lead.offer.offer_code,
        payload.data.purchase.transaction, new Date(payload.data.purchase.approved_date).toISOString()]);
    lead.purchase = admitted.webhook_event_id;
  }
  const since = new Date((await dbNow()).getTime() - 86_400_000).toISOString();
  const sales = await asService(async () => JSON.parse(one(await sql('select public.read_flow_sales_v1($1::jsonb, $2::timestamptz)::text as r',
    [JSON.stringify(INSTANCE), since]), 'ventas').r));
  const flow = one(sales.flows.filter((item) => item.flow_id === flowId), 'ventas del flujo');
  const won = one(await sql('select purchase_event_id from public.recovery_cases where id = $1', [lead.start.recovery_case_id]), 'caso');
  if (JSON.stringify(flow.by_branch) !== JSON.stringify({ maribel_pago_rechazado_banco_01: { sends: 1, purchases: 1 } })) {
    fail('las ventas por rama cuentan el envio y la compra', { by_branch: flow.by_branch, won });
  }
  // Lo de antes de las ventas, igual: la definicion de antes da lo mismo sin by_branch.
  const beforeSales = await withPrevious([SALES], async () => asService(async () => JSON.parse(one(await sql(
    'select public.read_flow_sales_v1($1::jsonb, $2::timestamptz)::text as r', [JSON.stringify(INSTANCE), since]), 'ventas').r)));
  const strip = (doc) => JSON.stringify(doc.flows.map(({ by_branch, ...rest }) => rest));
  if (strip(sales) !== strip(beforeSales) || sales.flows.some((item) => item.flow_id !== flowId
      && JSON.stringify(item.by_branch) !== '{}')) {
    fail('las ventas de antes, iguales');
  }
  emitted.sales_by_branch = flow.by_branch;
  results.envio = { link: link.outcome, oferta: issuance.offer_resolution, ventas_por_rama: flow.by_branch };
}

// ---------------------------------------------------------------------------
// 5. Lo de antes, igual: el pago rechazado nativo de un paso sin boton.
// ---------------------------------------------------------------------------
{
  const plain = { ...PAYMENT, flujo: 'pago_rechazado_simple', topes: { ...PAYMENT.topes, bolsa: 'att1-pago-simple' },
    pasos: [{ esperar: '15m' }, { plantilla: 'maribel_pago_rechazado_banco_01', idioma: 'es_MX',
      variables: ['nombre', 'producto'] }] };
  const plainId = await openNative('pago_rechazado_simple', plain);
  const lead = await refusalOf(await leadFor('ramas-simple'), { index: 1 });
  await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
    [plainId, lead.contact]);
  const rows = await asService(async () => (await db.query('select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
    [JSON.stringify(INSTANCE), JSON.stringify({
      fuente: 'hotmart', tipo: 'pago_fallido', evento_id: lead.event, producto_ref: A.funnel, oferta: lead.offer.offer_code,
      contacto_id: lead.contact, momento: lead.moment, chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
      destino: lead.phone })])).rows);
  const start = one(rows.filter((row) => row.flow_id === plainId && row.outcome === 'motor_arranco'), 'arranque simple');
  const now = await contextOf(start.scheduled_action_id);
  const before = await withPrevious([SENDS], () => contextOf(start.scheduled_action_id));
  const branches = now.template_branches;
  if (branches !== null || now.template_button !== null || JSON.stringify(now) !== JSON.stringify(before)) {
    fail('el contexto de un paso sin boton, igual', { now, before });
  }
  emitted.plain_step_context = now;
  results.de_antes = { ramas: branches, boton: now.template_button };
}

// ---------------------------------------------------------------------------
// El fixture para el bridge.
// ---------------------------------------------------------------------------
const stable = (value) => JSON.parse(JSON.stringify(value, (key, item) => (
  ['flow_id'].includes(key) ? '<flow_id>' : item)));
const fixture = {
  _capture: {
    origen: 'validate_flow_engine_native_payment_failure_branches.mjs: la cadena entera de migraciones (PGlite), el '
      + 'formulario y los rechazos capturados de Maribel por las admisiones portables, y las cuatro plantillas '
      + 'reconstruidas de Maribel en las ramas',
    encargo: 'E-306',
    regenerar: 'cd tests/sql/followup_engine && node validate_flow_engine_native_payment_failure_branches.mjs --emit',
  },
  ...stable(emitted),
};
if (EMIT) {
  writeFileSync(FIXTURE, `${JSON.stringify(fixture, null, 2)}\n`);
} else if (CHECK) {
  const stored = JSON.parse(readFileSync(FIXTURE, 'utf8'));
  if (JSON.stringify(stored) !== JSON.stringify(fixture)) fail('el fixture del bridge no es el de hoy', { stored, fixture });
}
await db.close();
console.log(JSON.stringify({ ok: true, ...results }));
