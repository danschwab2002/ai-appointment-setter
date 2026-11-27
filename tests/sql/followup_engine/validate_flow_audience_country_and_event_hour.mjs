// El pais y la hora del evento del disparo por audiencia (migracion 20261126700000, 4.5 y 4.4 de la lista
// definitiva, 2026-11-26). Contrato: docs/contracts/flow-audience-trigger-v1.md 0.8.0.
//
// Por que: el recordatorio de la clase del 22/11 de Maribel salio a 1.704 personas de 24 indicativos con una
// sola hora escrita (hora Colombia). El bridge ya filtra por pais (para.paises) y llena hora_evento; la base
// tiene que (1) dejar compilar un toque de la audiencia con hora_evento y (2) darle a la pagina cuantas
// personas de la lista hay por prefijo de cuatro digitos, sin telefonos.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs y el aviso a mano de
// validate_flow_audience_start_reasons.mjs (la forma de los avisos de prueba de Maribel), con un paso que manda
// nombre y hora_evento.
//
// Prueba:
//   0. la migracion: cambia solo _flow_engine_native_steps (con su marca, mismos permisos), suma solo la
//      lectura de la pagina (de flow_change_approver, de nadie mas); solo la forma nativo:audiencia cambia, y
//      en sus tres toques; correrla dos veces aborta con 55000 sin tocar nada;
//   1. rojo: sin la migracion, el aviso con hora_evento no se abre en el nativo (variables_differ);
//   2. con la migracion, se abre; la lista con personas de Mexico, Colombia y Estados Unidos (una omitida por
//      baja) se cuenta por prefijo, sin la omitida y sin telefonos, y solo con la sesion.
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261126700000_flow_audience_country_and_event_hour_v1.sql';
const STEPS_FN = '_flow_engine_native_steps(text)';
const READ_FN = 'approver_read_flow_audience_countries_v1(jsonb,text)';
const MARK = 'flow_audience_event_hour_steps';
const FORMS = ['nativo:compra', 'nativo:carrito', 'nativo:intencion', 'nativo:pago_fallido', 'nativo:silencio',
  'nativo:ficha_impresa', 'nativo:audiencia', 'otra'];
const results = {};
const sha = (text) => createHash('sha256').update(text).digest('hex');

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues van solo las fases del lanzamiento (20261126800000, 4.8), que no tocan la audiencia.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261126800000_agent_campaign_phases_v1.sql',
  // Los frenos ante Meta (20261127100000, F1).
  '20261127100000_meta_enforcement_brakes_v1.sql',
  // El texto fijo en las variables de un paso nativo (20261127300000, F4).
  '20261127300000_flow_engine_literal_variables_v1.sql',
  // El flujo de la palabra clave (20261127400000, Z2 3.9).
  '20261127400000_flow_engine_keyword_trigger_v1.sql'])) {
  fail(`despues de ${MIGRATION} van solo la de las fases del lanzamiento, la de los frenos ante Meta, la del texto fijo y la de la palabra clave`, names.slice(position + 1));
}

const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select replace(p.oid::regprocedure::text, 'public.', '') as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const shapes = async (db) => {
  const out = {};
  for (const form of FORMS) {
    out[form] = (await db.query('select public._flow_engine_native_steps($1)::text as s', [form])).rows[0].s;
  }
  return out;
};

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
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
  const shapesBefore = await shapes(db);
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const shapesAfter = await shapes(db);
  const changed = [...before.keys()].filter((s) => after.get(s)?.definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && ['acl', 'prosecdef', 'provolatile'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, [STEPS_FN]) || !sameSet(added, [READ_FN]) || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, attributes });
  }
  if (!after.get(STEPS_FN).definition.includes(MARK)) fail('la forma no tiene la marca');
  const changedForms = FORMS.filter((form) => shapesAfter[form] !== shapesBefore[form]);
  if (!sameSet(changedForms, ['nativo:audiencia'])) fail('cambio otra forma que la audiencia', changedForms);
  const slots = JSON.parse(shapesAfter['nativo:audiencia']);
  const slotsBefore = JSON.parse(shapesBefore['nativo:audiencia']);
  if (slots.length !== 3 || slots.some((slot, i) => JSON.stringify(slot.variables_de)
      !== JSON.stringify(['nombre', 'producto', 'hora_evento'])
      || JSON.stringify(slot.variables) !== JSON.stringify(slotsBefore[i].variables))) {
    fail('los tres toques de la audiencia admiten hora_evento, con las mismas variables por defecto', slots);
  }
  if (slotsBefore.some((slot) => slot.variables_de.includes('hora_evento'))) fail('rojo: antes ya estaba');
  for (const [role, expected] of [['anon', false], ['authenticated', false], ['service_role', false],
    ['flow_change_approver', true]]) {
    const can = one((await db.query(`select has_function_privilege($1, 'public.${READ_FN}', 'EXECUTE') as ok`,
      [role])).rows, role).ok;
    if (can !== expected) fail(`${role} ${expected ? 'no ' : ''}puede leer la lista por prefijo`);
  }
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('la segunda corrida tendria que abortar');
  } catch (error) {
    if (error.message !== 'flow_audience_country_and_event_hour_already_applied') {
      fail('la segunda corrida aborto por otra cosa', error.message);
    }
  }
  await db.exec('rollback').catch(() => {});
  const again = await snapshot(db);
  if ([...after.keys()].some((s) => JSON.stringify(again.get(s)) !== JSON.stringify(after.get(s)))) {
    fail('la segunda corrida cambio algo');
  }
  results.forma = { cambiadas: changed, nuevas: added, formas: changedForms, segunda_corrida: 'aborta' };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 sin esta migracion (ni la del texto fijo, que la necesita).
// ---------------------------------------------------------------------------
// El texto fijo (20261127300000, F4) pide esta migracion aplicada: sin ella aborta, asi que tampoco va.
const world = await buildWorld({ skip: [MIGRATION, '20261127300000_flow_engine_literal_variables_v1.sql'] });
const { db, A, A2, INSTANCE, person, admitForm, register, cycle, versionItem, dbNow } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const call = async (text, params) => JSON.parse(one(await sql(`select ${text}::text as r`, params), text).r);
await db.exec('alter role service_role bypassrls');
const as = async (role, run) => {
  await db.exec(`set role ${role}`);
  try {
    return await run();
  } finally {
    await db.exec('reset role');
  }
};
const rpc = async (fn, ...args) => {
  const marks = args.map((_, i) => `$${i + 1}`).join(', ');
  const values = args.map((a) => (a !== null && typeof a === 'object' ? JSON.stringify(a) : a));
  return JSON.parse(one(await sql(`select public.${fn}(${marks})::text as r`, values), fn).r);
};

const FLOW_KEY = 'clase_en_vivo';
const definition = {
  esquema: 'setter-flujo/v1', flujo: FLOW_KEY, producto: A.funnel, estado: 'borrador', nombre: 'Recordatorio de la clase',
  dispara: { manual: {} },
  para: { es: 'prospecta', formulario_en: '30d', paises: ['MX', 'CO', 'US'] },
  evento: { el: '2026-12-01T20:00', zona: 'America/Mexico_City' },
  entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ ...A2, variables: ['nombre', 'hora_evento'] }],
  termina: { si: ['compra', 'contesta'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-clase-en-vivo', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona', otra_despues_de: '7d' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  ritmo: { por_hora: 200, por_ocurrencia: 1000 },
};
const flowId = await register(A.funnel, FLOW_KEY);
await cycle([versionItem(flowId, A.funnel, FLOW_KEY, definition)], `v1 de ${FLOW_KEY}`);
const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
  [flowId]), 'propuesta').proposal_id;
const compile = async () => {
  try {
    const row = one(await sql(`select p.steps::text as steps from public._flow_engine_compile_policy_core($1::jsonb, $2, 1,
      'nativo:audiencia', 'America/Mexico_City') p`, [JSON.stringify(definition), flowId]), 'compilar');
    return { ok: JSON.parse(row.steps) };
  } catch (error) {
    return { error: error.message };
  }
};

// 1. Rojo: sin la migracion el compilador no deja pasar hora_evento.
const red = await compile();
if (red.error !== 'flow_policy_not_compilable:variables_differ:first_contact') {
  fail('rojo: sin la migracion el toque con hora_evento no compila', red);
}
results.rojo = red.error;

// 2. Con la migracion, compila (un solo paso, first_contact, como validate_flow_engine_native_template_shape.mjs).
await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
const green = await compile();
if (green.error !== undefined || green.ok.length !== 1 || green.ok[0].step_key !== 'first_contact') {
  fail('con la migracion el toque con hora_evento compila', green);
}
const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
  [flowId, proposalId, A.timezone]), 'abrir').r);
if (!opened.ok || opened.state !== 'cohorte') fail('el aviso se abre en el nativo', opened);

// La lista: dos de Mexico, una de Colombia, una de Estados Unidos (Houston) y una de Mexico con baja.
const lead = async (label, prefix, country, iso) => {
  const p = person(label, A, prefix);
  p.country = country;
  p.iso = iso;
  await admitForm(p, new Date((await dbNow()).getTime() - 50 * 60_000));
  p.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
    values ($1, $2, $3, $4) returning id`, [p.name, p.email, p.phone, iso]), 'contacto').id;
  await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1, 'email', $2, $2, 'system'), ($1, 'phone', $3, $3, 'system')`, [p.contact, p.email, p.phone]);
  await sql('insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, \'dan\')',
    [flowId, p.contact]);
  return p;
};
const people = [
  await lead('clase-mx-1', '5255111100', '52', 'MX'),
  await lead('clase-mx-2', '5233111100', '52', 'MX'),
  await lead('clase-co', '5730011100', '57', 'CO'),
  await lead('clase-us', '1713555100', '1', 'US'),
  await lead('clase-mx-baja', '5281111100', '52', 'MX'),
];
const optedOut = people[4];
// La baja: la de validate_flow_audience_triggers.mjs.
await sql(`insert into public.contact_authorizations (contact_id, channel, purpose, authorization_status,
    authorization_source, valid_from) values ($1, 'whatsapp', 'cart_recovery', 'denied', 'manual', now() - interval '1 day')`,
[optedOut.contact]);
const DAN = 'U0DANSCHWAB1';
const inMinutes = async (minutes) => new Date((await dbNow()).getTime() + minutes * 60_000).toISOString();
const session = sha('z1-pais-session');
await as('flow_change_approver', async () => {
  const state = sha('z1-pais-state');
  await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha('z1-pais-nonce'), '/flow-approval/', await inMinutes(10));
  await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
  const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, 'T0LANCEMOS01', DAN,
    await inMinutes(60));
  if (!created.ok) fail('entrar con Slack', created);
});
const materialized = await call(
  'public.materialize_flow_audience_occurrence_v1($1::jsonb, $2::uuid, $3::integer, $4::jsonb, $5::jsonb)',
  [JSON.stringify(INSTANCE), flowId, 1,
    JSON.stringify({ key: 'manual:77777777-7777-4777-8777-777777777777', scheduled_at: (await dbNow()).toISOString(),
      fingerprint: 'f'.repeat(64), materialized_by: `slack:${DAN}` }),
    JSON.stringify(people.map((p) => ({ contact_id: p.contact, phone: p.phone })))]);
if (!materialized.ok) fail('la lista', materialized);
const OCCURRENCE = materialized.occurrence_id;
const omitted = Number(one(await sql(`select count(*) as n from public.flow_audience_members
  where occurrence_id = $1 and state = 'omitida'`, [OCCURRENCE]), 'omitidas').n);

const read = await as('flow_change_approver', () => rpc('approver_read_flow_audience_countries_v1', INSTANCE, session));
if (read.contract !== 'flow-audience-countries-v1') fail('el contrato de la lectura', read);
const counts = read.occurrences?.[OCCURRENCE];
if (omitted !== 1) fail('la de la baja queda omitida', { omitted, materialized });
const expected = { 5255: 1, 5233: 1, 5730: 1, 1713: 1 };
if (JSON.stringify(Object.fromEntries(Object.entries(counts ?? {}).sort()))
    !== JSON.stringify(Object.fromEntries(Object.entries(expected).map(([k, v]) => [String(k), v]).sort()))) {
  fail('la lista por prefijo, sin las omitidas', { counts, omitted });
}
if (Object.values(counts).reduce((a, b) => a + b, 0) !== materialized.member_count) {
  fail('la suma por prefijo es la de la lista', { counts, materialized });
}
const text = JSON.stringify(read);
for (const p of people) {
  if (text.includes(p.phone) || text.includes(p.contact) || text.includes(p.email)) fail('la lectura lleva datos', p.label);
}
let denied;
try {
  await as('flow_change_approver', () => rpc('approver_read_flow_audience_countries_v1', INSTANCE, sha('otra')));
} catch (error) {
  denied = error.message;
}
if (denied !== 'flow_approval_session_invalid') fail('sin sesion no se lee', denied);
results.pagina = { por_prefijo: counts, omitidas: omitted, sin_sesion: denied };

await db.close();
console.log(`validate_flow_audience_country_and_event_hour: ok ${JSON.stringify(results)}`);
