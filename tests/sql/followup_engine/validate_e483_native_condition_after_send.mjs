// El pago rechazado completo en el motor nativo: B1 por motivo y, a las 24 h,
// B2 o Z segun la rama (migracion 20261112210300, E-483, C3 del mapa «el AE
// maneja todos los flujos», 2026-11-12; H3/P9).
//
// Por que: el legado de ATT1 manda B1 en una de cuatro variantes y a las 24 h
// B2 si B1 salio sin cupon, Z si salio con uno. En el nativo cada rama de la
// cadena (20261103000900) mandaba una plantilla. Con la migracion cada rama es
// un camino (B1, esperar, B2 o Z): la rama sabe si su B1 lleva cupon fijo, asi
// que no hace falta un dato nuevo «el toque anterior llevo cupon».
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera): el formulario con consentimiento y los rechazos capturados de
// Maribel (tests/fixtures/hotmart_purchase_canceled_maribel_20261028.json: uno
// de saldo insuficiente y dos negados por el banco) pasan por las admisiones
// portables de hoy; la compra, la capturada de la bienvenida de ATT1. B1, las
// cuatro plantillas de Maribel (reconstruidas, 20261102); B2 y Z, las del
// seguimiento de ATT1 (chatwoot_message_templates_inbox_11_pago_rechazado_seguimiento_20261019.json).
//
// Prueba:
//   0. la forma: cambian exactamente cinco funciones, con su marca, y nacen dos
//      privadas; correrla dos veces aborta; nada despues; compile_cases.json
//      compila igual;
//   1. la cadena con su seguimiento (cada rama: B1, esperar 24h, B2 o Z) se
//      abre y compila a dos pasos (native_touch_2 a 24 h); rojos de la forma
//      (sin contesta, esperas distintas, boton en el segundo, espera al final,
//      cuatro envios, una plantilla antes de la cadena, una condicion adentro
//      de una rama) y rojo con las definiciones de antes;
//   2. lo publicado hoy (la cadena de Maribel de un envio por rama) da las
//      mismas ramas, la misma politica y los mismos envios que antes;
//   3. el despacho: un rechazo del banco recibe B1 banco y, a las 24 h, B2; uno
//      de saldo insuficiente recibe B1 saldo y Z, aunque un rechazo nuevo entre
//      los dos cambie el motivo; el rojo: con el contexto de antes, el segundo
//      toque no sale de la rama elegida;
//   4. la respuesta corta el segundo; la compra lo corta;
//   5. sin la eleccion del primer envio aceptado, el segundo no tiene envio.
//
// B2 y Z son los nombres de ATT1: la base no mira el catalogo. Tal como estan aprobadas llevan el boton del
// checkout con el cupon 2 y una variable mas que nombre y producto, asi que el validador no las deja en un
// toque siguiente nativo hasta C2 (lo fija tests/test_e483_native_condition_after_send.py).
//
// Con --emit escribe tests/fixtures/flow_engine/native_condition_after_send_rows.json (las filas del
// contexto del primer y del segundo toque, como las contesta la base) para
// tests/test_e483_native_condition_after_send.py; con --check (el de npm test) lo compara y falla si difiere.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, errorOf, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261112210300_e483_native_condition_after_send_v1.sql';
const TZ = 'timestamp with time zone';
const BRANCHES = '_flow_engine_native_payment_failure_branches(jsonb)';
const UNSUPPORTED = '_flow_engine_native_unsupported(jsonb)';
const CORE = '_flow_engine_compile_policy_core(jsonb,text,integer,text,text)';
const SENDS = '_flow_engine_sends(jsonb)';
const CONTEXT = 'flow_engine_execution_context_v1(uuid)';
const CHANGED = [BRANCHES, UNSUPPORTED, CORE, SENDS, CONTEXT];
const ADDED = ['_flow_engine_native_payment_failure_path_shape(jsonb)', '_flow_engine_native_payment_failure_paths(jsonb)'];
const fixtureFile = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));
const REFUSALS = fixtureFile('hotmart_purchase_canceled_maribel_20261028.json').eventos;
const PURCHASE = fixtureFile('hotmart_purchase_approved_att1_bienvenida_20261026.json').payload;
const COMPILE_CASES = fixtureFile('flow_engine/compile_cases.json').cases;
const FOLLOWUPS = fixtureFile('chatwoot_message_templates_inbox_11_pago_rechazado_seguimiento_20261019.json')
  .message_templates.map((template) => template.name);
const B2 = 'att1_pago_rechazado_descuento_01';
const Z = 'att1_ultima_renovacion_01';
if (!FOLLOWUPS.includes(B2) || !FOLLOWUPS.includes(Z)) fail('el fixture del seguimiento de ATT1 trae B2 y Z', FOLLOWUPS);
const results = {};
const emitted = {};
const FIXTURE = join(root, 'tests/fixtures/flow_engine/native_condition_after_send_rows.json');
const EMIT = process.argv.includes('--emit');
const CHECK = process.argv.includes('--check');
// jsonb ordena las claves: se compara con las claves ordenadas.
const canonical = (value) => JSON.stringify(value, (key, item) => (item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.keys(item).sort().map((k) => [k, item[k]])) : item));

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues va el pago rechazado sin formulario (20261112210400, E-483, pieza PF): no toca nada de esta.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261112210400_e483_payment_failure_by_phone_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de PF`, names.slice(position + 1));
}

// La cadena de ATT1 (el orden de choose_variant). B1 con cupon fijo (la variante
// del cupon y la del saldo) sigue con Z; sin cupon (dos rechazos y el banco), con B2.
const COUPON = { link: 'checkout', cupon: 'PROTOCOLO10' };
const b1 = (branch, coupon) => ({ plantilla: `maribel_pago_rechazado_${branch}_01`, idioma: 'es_MX',
  variables: ['nombre', 'producto'], boton: coupon ? { ...COUPON } : { link: 'checkout' } });
const follow = (template) => ({ plantilla: template, idioma: 'es_MX', variables: ['nombre', 'producto'] });
const path = (branch, coupon, wait = '24h') => [b1(branch, coupon), { esperar: wait }, follow(coupon ? Z : B2)];
const chainOf = (paths) => ({
  condicion: { dato: 'descuento_previo', es: ['si'] }, entonces: paths[0],
  si_no: [{ condicion: { dato: 'rechazos', es: ['varios'] }, entonces: paths[1],
    si_no: [{ condicion: { dato: 'tipo_de_rechazo', es: ['sin_saldo'] }, entonces: paths[2], si_no: paths[3] }] }],
});
const FULL_PATHS = [path('cupon', true), path('dos_veces', false), path('saldo', true), path('banco', false)];
const B1_TEMPLATES = ['cupon', 'dos_veces', 'saldo', 'banco'].map((b) => `maribel_pago_rechazado_${b}_01`);
const BASE = (A) => ({
  esquema: 'setter-flujo/v1', flujo: 'pago_rechazado_completo', producto: A.funnel, estado: 'borrador',
  nombre: 'Pago rechazado completo', dispara: { evento: 'pago_fallido' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ esperar: '15m' }, chainOf(FULL_PATHS)],
  termina: { si: ['compra', 'contesta'], vence: '3d' }, horario: 'siempre',
  topes: { bolsa: 'att1-pago-completo', por_dia: 20, total: 1000, repeticion: { una_por: 'persona', otra_despues_de: '72h' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
});
// Lo publicado hoy: la cadena de Maribel, un envio por rama con PROTOCOLO10 (20261103000900).
const TODAY_PATHS = ['cupon', 'dos_veces', 'saldo', 'banco'].map((b) => [b1(b, true)]);

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
  if (!sameSet(changed, CHANGED) || !sameSet(added, ADDED) || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  for (const signature of CHANGED) {
    if (!after.get(signature).definition.includes('e483_native_condition_after_send')) fail(`${signature}: sin la marca`);
    previous[signature] = before.get(signature).definition;
  }
  for (const signature of ADDED) {
    const row = after.get(signature);
    if (row.service_x || row.api_x) fail(`${signature}: no es de nadie de la API`, row);
  }
  const error = await errorOf(db, () => applyFile(db, join(root, 'supabase/migrations', MIGRATION)));
  if (error?.message !== 'e483_native_condition_after_send_already_applied') {
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
const { db, A, INSTANCE, register, cycle, versionItem, dbNow, reserve, startNew, accept, person, admitForm } = world;
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
const FULL = BASE(A);
const TODAY = { ...FULL, flujo: 'pago_rechazado', pasos: [{ esperar: '15m' }, chainOf(TODAY_PATHS)],
  termina: { si: ['compra'], vence: '1d' }, topes: { ...FULL.topes, bolsa: 'att1-pago-hoy' } };
const unsupportedOf = async (definition) => one(await sql('select public._flow_engine_native_unsupported($1::jsonb) as r',
  [JSON.stringify(definition)]), 'lo que no ejecuta').r;
const compileOf = async (definition) => one(await sql(`select grace_period::text as grace_period,
    expires_after::text as expires_after, max_automatic_messages, steps
  from public._flow_engine_compile_policy_core($1::jsonb, '00000000-0000-4000-8000-0000000e4833', 1, 'nativo:pago_fallido',
    'America/Mexico_City')`, [JSON.stringify(definition)]), 'politica');
const readsOf = async (definition) => ({
  ramas: one(await sql('select public._flow_engine_native_payment_failure_branches($1::jsonb) as b',
    [JSON.stringify(definition)]), 'ramas').b,
  envios: await sql('select * from public._flow_engine_sends($1::jsonb)', [JSON.stringify(definition)]),
  politica: await compileOf(definition),
  abre: await unsupportedOf(definition),
});

// ---------------------------------------------------------------------------
// 1. La cadena con su seguimiento se abre y compila a dos pasos.
// ---------------------------------------------------------------------------
{
  if (await unsupportedOf(FULL) !== null) fail('la cadena con su seguimiento se abre', await unsupportedOf(FULL));
  const compiled = await compileOf(FULL);
  if (compiled.max_automatic_messages !== 2 || compiled.grace_period !== '00:15:00'
      || JSON.stringify(compiled.steps.map((s) => [s.step_key, s.delay ?? null, s.approved_template ?? null]))
         !== JSON.stringify([['first_contact', null, null], ['native_touch_2', '24 hours', true]])) {
    fail('la politica de la cadena con su seguimiento', compiled);
  }
  const reads = await readsOf(FULL);
  if (JSON.stringify(reads.ramas.map((b) => b.paso.plantilla)) !== JSON.stringify(B1_TEMPLATES)
      || reads.ramas.some((b) => Object.keys(b).sort().join() !== 'cuando,paso')) {
    fail('las ramas siguen siendo el primer envio de cada una', reads.ramas);
  }
  const paths = one(await sql('select public._flow_engine_native_payment_failure_paths($1::jsonb) as p',
    [JSON.stringify(FULL)]), 'caminos').p;
  if (canonical(paths) !== canonical(FULL_PATHS)) fail('los caminos de las ramas', paths);
  if (JSON.stringify(reads.envios.map((s) => [s.ordinal, s.plantilla])) !== JSON.stringify([[1, B1_TEMPLATES[3]], [2, B2]])) {
    fail('los envios: el camino del si_no final', reads.envios);
  }
  const chainWith = (changes) => ({ ...FULL, pasos: [{ esperar: '15m' }, chainOf(FULL_PATHS.map((p, i) => changes[i] ?? p))] });
  const reds = {
    sin_contesta: { ...FULL, termina: { si: ['compra'], vence: '3d' } },
    esperas_distintas: chainWith({ 1: path('dos_veces', false, '48h') }),
    boton_en_el_segundo: chainWith({ 3: [b1('banco', false), { esperar: '24h' }, { ...follow(B2), boton: { link: 'checkout' } }] }),
    espera_al_final: chainWith({ 0: [...path('cupon', true), { esperar: '1h' }] }),
    cuatro_envios: chainWith({ 2: [...path('saldo', true), { esperar: '1h' }, follow(Z), { esperar: '1h' }, follow(Z)] }),
    sin_espera: chainWith({ 0: [b1('cupon', true), follow(Z)] }),
    espera_de_cero: chainWith({ 0: [b1('cupon', true), { esperar: '0m' }, follow(Z)] }),
    espera_antes_de_b1: chainWith({ 0: [{ esperar: '1h' }, ...path('cupon', true)] }),
    plantilla_antes_de_la_cadena: { ...FULL, pasos: [b1('banco', false), { esperar: '24h' }, chainOf(FULL_PATHS)] },
    condicion_en_la_rama: chainWith({ 3: [b1('banco', false), { esperar: '24h' },
      { condicion: { dato: 'rechazos', es: ['varios'] }, entonces: [follow(B2)], si_no: [follow(Z)] }] }),
    una_rama_de_un_envio: chainWith({ 3: [b1('banco', false)] }),
  };
  const shapes = {};
  for (const [label, definition] of Object.entries(reds)) {
    shapes[label] = await unsupportedOf(definition);
    if (shapes[label] !== 'motor_nativo:forma') fail(`${label} es motor_nativo:forma`, shapes[label]);
  }
  const compileRed = await errorOf(db, () => compileOf(reds.esperas_distintas));
  if (compileRed?.message !== 'flow_policy_not_compilable:ramas_invalidas') fail('el compilador frena esperas distintas', compileRed?.message);
  // Rojo: con las definiciones de antes, la cadena con su seguimiento no se abria ni compilaba.
  const before = await withPrevious(CHANGED, async () => ({
    abre: await unsupportedOf(FULL), compila: (await errorOf(db, () => compileOf(FULL)))?.message }));
  if (before.abre !== 'motor_nativo:forma' || before.compila !== 'flow_policy_not_compilable:ramas_invalidas') {
    fail('sin la migracion, la cadena con su seguimiento no se abria', before);
  }
  results.forma_del_flujo = { politica: compiled.steps.map((s) => s.step_key), rojos: Object.keys(shapes).length,
    sin_la_migracion: before };
}

// ---------------------------------------------------------------------------
// 2. Lo publicado hoy, igual.
// ---------------------------------------------------------------------------
{
  const now = await readsOf(TODAY);
  const then = await withPrevious(CHANGED, () => readsOf(TODAY));
  if (JSON.stringify(now) !== JSON.stringify(then) || now.abre !== null || now.politica.max_automatic_messages !== 1) {
    fail('la cadena de Maribel de un envio por rama, igual que antes', { now, then });
  }
  results.de_hoy = { abre: now.abre, envios: now.envios.length, politica: now.politica.steps.map((s) => s.step_key) };
}

// ---------------------------------------------------------------------------
// 3. El despacho: B1 de su rama y, a las 24 h, B2 o Z.
// ---------------------------------------------------------------------------
const flowId = await register(A.funnel, FULL.flujo);
await cycle([versionItem(flowId, A.funnel, FULL.flujo, FULL)], 'v1 del pago rechazado completo');
{
  const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
    [flowId]), 'propuesta').proposal_id;
  const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [flowId, proposalId, A.timezone]), 'abrir').r);
  if (!opened.ok || opened.state !== 'cohorte') fail('abrir el pago rechazado completo', opened);
}
let serial = 0;
const leadFor = async (label) => {
  const lead = person(label);
  await admitForm(lead, new Date((await dbNow()).getTime() - 50 * 60_000));
  lead.chatwoot = String(483300 + serial);
  return lead;
};
const refusalOf = async (lead, { index = 0, at = null } = {}) => {
  serial += 1;
  const payload = structuredClone(REFUSALS[index].payload);
  const moment = at ?? new Date((await dbNow()).getTime() - 10 * 60_000);
  payload.id = `c3-rechazo-${lead.email}-${serial}`;
  payload.creation_date = moment.getTime();
  payload.data.buyer = { ...payload.data.buyer, name: lead.name, email: lead.email, checkout_phone: lead.phone };
  payload.data.product = { ...payload.data.product, id: A.productId, name: A.productName };
  payload.data.purchase.offer.code = lead.offer.offer_code;
  payload.data.purchase.transaction = `HPC3${String(serial).padStart(4, '0')}`;
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
const admitNative = async (lead) => one((await asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
  [JSON.stringify(INSTANCE), JSON.stringify({
    fuente: 'hotmart', tipo: 'pago_fallido', evento_id: lead.event, producto_ref: A.funnel, oferta: lead.offer.offer_code,
    contacto_id: lead.contact, momento: lead.moment, chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
    destino: lead.phone })])).rows)).filter((row) => row.flow_id === flowId), `${lead.label} admision`);
const contextOf = async (actionId) => (await asService(async () => (await db.query(
  'select * from public.flow_engine_execution_context_v1($1)', [actionId])).rows));
const dataOf = async (reserved) => one(await asService(async () => (await db.query(
  'select * from public.get_flow_engine_native_payment_failure_context_v1($1,$2,$3,$4)',
  [reserved.actionId, reserved.worker, reserved.lease, reserved.now])).rows), 'datos');
const recordOf = async (reserved, index) => one(await asService(async () => (await db.query(
  'select * from public.record_flow_engine_native_branch_v1($1,$2,$3,$4,$5,$6)',
  [reserved.actionId, reserved.attempt.id, reserved.worker, reserved.lease, index, reserved.now])).rows), 'registro');
const branchOf = (data) => (data.descuento_previo === 'si' ? 0 : data.rechazos === 'varios' ? 1
  : data.tipo_de_rechazo === 'sin_saldo' ? 2 : 3);
const touch2Of = async (lead) => sql(`select * from public.scheduled_actions where recovery_case_id = $1
  and step_key = 'native_touch_2' order by created_at`, [lead.start.recovery_case_id]);
const dueNow = async (id) => sql("update public.scheduled_actions set due_at = now() - interval '1 second' where id = $1", [id]);

// B1: la base da las cuatro ramas, el bridge elige y registra, sale y se acepta.
const sendB1 = async (lead, want) => {
  const reserved = await reserve(lead, lead.start.scheduled_action_id);
  const [context] = await contextOf(reserved.actionId);
  if (!context || context.send_ordinal !== 1
      || JSON.stringify((context.template_branches ?? []).map((b) => b.paso.plantilla)) !== JSON.stringify(B1_TEMPLATES)) {
    fail(`${lead.label}: el contexto de B1 trae las cuatro ramas`, context);
  }
  if (lead.label === 'c3-banco') emitted.touch1_context_banco = context;
  const index = branchOf(await dataOf(reserved));
  if (index !== want) fail(`${lead.label}: la rama de B1`, { index, want });
  const recorded = await recordOf(reserved, index);
  if (recorded.outcome !== 'recorded' || recorded.template_name !== B1_TEMPLATES[want]) fail(`${lead.label}: registro`, recorded);
  if ((await startNew(reserved)).outcome !== 'request_started') fail(`${lead.label}: el arranque de B1`);
  const accepted = await accept(lead, reserved);
  const [touch2] = await touch2Of(lead);
  if (!touch2 || touch2.action_type !== 'no_reply_review' || touch2.status !== 'pending') {
    fail(`${lead.label}: el segundo toque, agendado`, await touch2Of(lead));
  }
  const acceptedAt = one(await sql('select accepted_at from public.followup_delivery_attempts where id = $1',
    [reserved.attempt.id]), 'aceptado').accepted_at;
  const hours = (new Date(touch2.due_at).getTime() - new Date(acceptedAt).getTime()) / 3_600_000;
  if (Math.abs(hours - 24) > 0.01) fail(`${lead.label}: el segundo toque, a las 24 h del primero`, { hours });
  return { reserved, accepted, touch2 };
};

const people = {};
{
  // Negado por el banco, un rechazo: B1 banco (sin cupon) y B2.
  people.banco = await refusalOf(await leadFor('c3-banco'), { index: 1 });
  // Saldo insuficiente: B1 saldo (con cupon) y Z.
  people.saldo = await refusalOf(await leadFor('c3-saldo'), { index: 0 });
  people.contesta = await refusalOf(await leadFor('c3-contesta'), { index: 1 });
  people.compra = await refusalOf(await leadFor('c3-compra'), { index: 1 });
  for (const lead of Object.values(people)) {
    await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
      [flowId, lead.contact]);
    lead.start = await admitNative(lead);
    if (lead.start.outcome !== 'motor_arranco') fail(`${lead.label} arranca`, lead.start);
  }
  const policy = one(await sql(`select p.* from public.followup_policy_versions p join public.recovery_cases c
    on c.policy_key = p.policy_key and c.policy_version = p.version where c.id = $1`, [people.banco.start.recovery_case_id]),
  'politica del caso');
  if (policy.max_automatic_messages !== 2) fail('la politica del caso, dos envios', policy);

  const sent = {};
  for (const [key, want] of [['banco', 3], ['saldo', 2], ['contesta', 3], ['compra', 3]]) {
    sent[key] = await sendB1(people[key], want);
  }
  // Un rechazo nuevo entre B1 y B2 (otro del banco): ahora son varios, pero la rama ya esta elegida.
  await refusalOf(people.saldo, { index: 2 });
  const more = await admitNative(people.saldo);
  if (more.reason !== 'ya_en_curso') fail('el rechazo nuevo se suma al recorrido', more);

  results.segundo = {};
  for (const [key, want] of [['banco', B2], ['saldo', Z]]) {
    const lead = people[key];
    const { touch2 } = sent[key];
    await dueNow(touch2.id);
    const reserved = await reserve(lead, touch2.id);
    const rows = await contextOf(touch2.id);
    const context = one(rows, `${key}: contexto del segundo`);
    if (context.send_ordinal !== 2 || context.step_key !== 'native_touch_2' || context.template_name !== want
        || context.template_branches !== null || context.template_button !== null
        || JSON.stringify(context.template_variables) !== JSON.stringify(['nombre', 'producto'])) {
      fail(`${key}: el segundo toque sale de su rama`, context);
    }
    emitted[`touch2_context_${key}`] = context;
    // Rojo: con el contexto de antes el segundo toque no sale de la rama elegida (el
    // del si_no final para todos, o ninguno).
    const red = await withPrevious([CONTEXT], () => contextOf(touch2.id));
    if (key === 'saldo' && red.length === 1 && red[0].template_name === want) fail('sin la migracion, Z saldria igual', red);
    const started = await startNew(reserved);
    if (started.outcome !== 'request_started' || !started.flow_authorization_id) fail(`${key}: el arranque del segundo`, started);
    await accept(lead, reserved);
    results.segundo[key] = { rama: B1_TEMPLATES[key === 'banco' ? 3 : 2], segundo: context.template_name,
      sin_la_migracion: red.map((row) => row.template_name) };
  }
  const pool = one(await sql('select count(*)::int as n from public.flow_engine_request_authorizations where pool = $1',
    [FULL.topes.bolsa]), 'bolsa').n;
  if (pool !== 6) fail('la bolsa cuenta cada envio (cuatro B1 y dos segundos)', pool);
  results.bolsa = pool;

  // -------------------------------------------------------------------------
  // 4. La respuesta corta el segundo; la compra lo corta.
  // -------------------------------------------------------------------------
  {
    const lead = people.contesta;
    const { touch2 } = sent.contesta;
    await dueNow(touch2.id);
    const now = (await sql('select now() as n'))[0].n;
    const claimed = one((await sql("select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)",
      ['w-c3', now])).filter((row) => row.id === touch2.id), 'claim del segundo');
    const decision = one(await sql(`select * from public.reevaluate_followup_action(p_action_id => $1, p_worker_id => $2,
      p_lease_generation => $3, p_now => $4, p_chatwoot_checked => true, p_chatwoot_conversation_id => $5,
      p_chatwoot_checkpoint_message_id => '9', p_chatwoot_checkpoint_at => $4, p_chatwoot_status => 'open',
      p_chatwoot_can_reply => true, p_chatwoot_anchor_found => true, p_chatwoot_automation_paused => false,
      p_chatwoot_inbound_after_anchor => true, p_chatwoot_human_activity_after_anchor => false)`,
    [touch2.id, 'w-c3', claimed.lease_generation, now, lead.chatwoot]), 'reevaluacion');
    if (decision.decision !== 'cancel' || decision.reason_code !== 'prospect_replied') fail('la respuesta corta el segundo', decision);
    results.respuesta = decision.reason_code;
  }
  {
    const lead = people.compra;
    const payload = structuredClone(PURCHASE);
    const now = await dbNow();
    const shift = now.getTime() - 30_000 - payload.creation_date;
    for (const key of ['approved_date', 'order_date']) payload.data.purchase[key] += shift;
    payload.creation_date += shift;
    payload.id = `c3-compra-${lead.email}`;
    payload.data.buyer = { ...payload.data.buyer, name: lead.name, email: lead.email, checkout_phone: lead.phone };
    payload.data.product = { ...payload.data.product, id: A.productId };
    payload.data.purchase.offer.code = lead.offer.offer_code;
    payload.data.purchase.transaction = 'HPC3COMPRA';
    const admitted = one(await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
      [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone]), 'compra');
    if (admitted.outcome !== 'inserted') fail('admitir la compra', admitted);
    await sql('select * from public.apply_hotmart_purchase_approved($1,$2,$3,$4,$5,$6,$7::timestamptz)',
      [admitted.webhook_event_id, lead.email, lead.phone, String(A.productId), lead.offer.offer_code,
        payload.data.purchase.transaction, new Date(payload.data.purchase.approved_date).toISOString()]);
    const [touch2] = await touch2Of(lead);
    let reason;
    if (touch2.status === 'pending') {
      await dueNow(touch2.id);
      reason = (await reserve(lead, touch2.id, { expect: 'cancel' })).decision.reason_code;
    } else {
      reason = `${touch2.status}:${touch2.terminal_reason ?? ''}`;
    }
    if (!(reason.startsWith('purchase_') || reason.startsWith('cancelled:purchase'))) fail('la compra corta el segundo', { reason, touch2 });
    results.compra = reason;
  }

  // -------------------------------------------------------------------------
  // 5. Sin la eleccion del primer envio aceptado, el segundo no tiene envio.
  // -------------------------------------------------------------------------
  {
    const { touch2 } = sent.banco;
    const without = await rolledBack(async () => {
      // (la tabla es de solo suma: adentro de una transaccion que se deshace)
      await db.exec('alter table public.flow_engine_native_branch_choices disable trigger flow_engine_native_branch_choices_append_only');
      await sql('delete from public.flow_engine_native_branch_choices where delivery_attempt_id = $1',
        [sent.banco.reserved.attempt.id]);
      return contextOf(touch2.id);
    });
    if (without.length !== 0) fail('sin un primer envio aceptado con su rama, el segundo no adivina', without);
    results.sin_eleccion = without.length;
  }
}

// ---------------------------------------------------------------------------
// El fixture para el bridge.
// ---------------------------------------------------------------------------
const stable = (value) => JSON.parse(JSON.stringify(value, (key, item) => (key === 'flow_id' ? '<flow_id>' : item)));
const fixture = {
  _capture: {
    origen: 'validate_e483_native_condition_after_send.mjs: la cadena entera de migraciones (PGlite), el formulario '
      + 'y los rechazos capturados de Maribel por las admisiones portables; B1 con las plantillas de Maribel y B2 y Z '
      + 'con los nombres del seguimiento de ATT1',
    encargo: 'E-483 C3',
    regenerar: 'cd tests/sql/followup_engine && node validate_e483_native_condition_after_send.mjs --emit',
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
console.log(JSON.stringify({ ok: true, ...results }, null, 2));
