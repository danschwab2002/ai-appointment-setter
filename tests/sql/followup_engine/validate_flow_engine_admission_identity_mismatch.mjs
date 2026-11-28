// El choque de identidad en la admision de un evento es un rechazo con nombre
// (migracion 20261125000100, claim claude-flow-admission-identity-mismatch-v1).
//
// Por que: la admision nativa aborta con 23514 channel_identity_contact_mismatch
// cuando la identidad de WhatsApp del destino es de otro contacto, y
// admit_flow_engine_event_v1 lo guardaba como error_interno:23514, sin el
// mensaje (Maribel, 23 y 24/11: 7 filas, las 7 con la misma forma).
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera, el formulario y el carrito por las admisiones portables), con el
// carrito nativo de un toque de validate_flow_engine_native_cart.mjs. El
// segundo contacto sale de la forma real de las filas de Maribel
// (tests/fixtures/flow_engine/admission_identity_mismatch_maribel_20261125.json):
// un contacto sin mail ni telefono, duenio de la identidad de WhatsApp activa
// del destino (cuenta chatwoot:<n>, metadata con inbox_id, solo digitos), y el
// evento de Hotmart del contacto del mail.
//
// Prueba:
//   0. la forma: cambia exactamente admit_flow_engine_event_v1, sin funciones
//      nuevas ni otros atributos ni permisos; correrla dos veces aborta;
//   1. rojo: sin la migracion, la persona con dos contactos da
//      error_interno:23514 (la fila de rechazo y la fila que devuelve);
//   2. verde: con la migracion da no_entra:identidad_de_otro_contacto, con su
//      fila de rechazo, y sigue sin arrancar: ningun caso, la identidad sigue
//      del otro contacto; el motivo nuevo no se avisa como falla del motor;
//   3. lo demas, igual: otro 23514 (la identidad del mismo contacto con otra
//      bandeja) sigue siendo error_interno:23514, y una persona sin choque
//      arranca.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261125000100_flow_engine_admission_identity_mismatch_v1.sql';
const ADMIT = 'admit_flow_engine_event_v1(jsonb,jsonb)';
const NAMED = 'no_entra:identidad_de_otro_contacto';
const shape = JSON.parse(readFileSync(join(root,
  'tests/fixtures/flow_engine/admission_identity_mismatch_maribel_20261125.json'), 'utf8'));
const maribel = JSON.parse(readFileSync(join(root,
  'tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json'), 'utf8')).message_templates[0];
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues van solo las del masivo (20261125000200 y 20261125000300), que no tocan la admision.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261125000200_bulk_send_store_v1.sql',
  '20261125000300_bulk_send_prepared_and_base_vetoes_v1.sql',
    // El registro por persona del masivo (20261125000400).
    '20261125000400_bulk_send_recipients_read_v1.sql',
    // La pausa del agente (20261126100000, 5.4).
    '20261126100000_agent_global_pause_v1.sql',
    // La instruccion temporal (20261126200000, 5.3 de Z1): tablas y RPC propias, no toca lo que se prueba aca.
    '20261126200000_agent_temporary_instructions_v1.sql',
    // Exportar contactos (20261126300000).
    '20261126300000_contact_export_v1.sql',
    // Las reglas de palabra clave (20261126400000, Z1 3.9).
    '20261126400000_keyword_rules_v1.sql',
    // Las corridas del juez (20261126500000, 4.9).
    '20261126500000_agent_campaign_judge_runs_v1.sql',
    // El conocimiento vivo del AE (20261126600000, pieza 5.5).
    '20261126600000_agent_live_knowledge_v1.sql',
    // El pais y la hora del evento de la audiencia (20261126700000, piezas 4.4 y 4.5).
    '20261126700000_flow_audience_country_and_event_hour_v1.sql',
    // Las fases del lanzamiento (20261126800000).
    '20261126800000_agent_campaign_phases_v1.sql',
    // Los frenos ante Meta (20261127100000, F1).
    '20261127100000_meta_enforcement_brakes_v1.sql',
    // El texto fijo en las variables de un paso nativo (20261127300000, F4).
    '20261127300000_flow_engine_literal_variables_v1.sql',
    // El flujo de la palabra clave (20261127400000, Z2 3.9).
    '20261127400000_flow_engine_keyword_trigger_v1.sql',
    // La confirmacion de registro (20261127500000, 4.3).
    '20261127500000_own_forms_registration_confirmation_v1.sql',
    // El texto fijo tercero del seguimiento por silencio (20261128100000, U4).
    '20261128100000_flow_engine_native_silence_literal_v1.sql'])) {
  fail(`despues de ${MIGRATION} van solo las del masivo`, names.slice(position + 1));
}
if (shape.rechazos.reason !== 'error_interno:23514' || shape.identidad_del_destino.es_de_otro_contacto !== true
    || shape.identidad_del_destino.su_contacto.email !== null || shape.identidad_del_destino.su_contacto.phone !== null) {
  fail('el fixture no es la forma capturada', shape);
}

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile, p.proconfig::text as config
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
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
    && ['acl', 'prosecdef', 'provolatile', 'config'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, [ADMIT]) || added.length !== 0 || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  const definition = after.get(ADMIT).definition;
  if (!definition.includes('flow_engine_admission_identity_mismatch') || !definition.includes(`'${NAMED}'`)
      || !definition.includes("'error_interno:' || lower(v_sqlstate)")) {
    fail('la definicion nueva', definition);
  }
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'flow_engine_admission_identity_mismatch_already_applied') {
      fail('la segunda corrida aborto por otra cosa', error.message);
    }
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { cambiadas: changed, nuevas: added.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 sin la migracion, con el carrito nativo de un toque.
// ---------------------------------------------------------------------------
const world = await buildWorld({ skip: [MIGRATION] });
const { db, A, INSTANCE, register, cycle, versionItem, cartFor } = world;
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
const CART = {
  esquema: 'setter-flujo/v1', flujo: 'carrito_nativo', producto: A.funnel, estado: 'borrador', nombre: 'Carrito nativo',
  dispara: { evento: 'carrito' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ plantilla: maribel.name, idioma: maribel.language, variables: ['nombre', 'producto'] }],
  termina: { si: ['compra'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-carrito-nativo', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
const flowId = await register(A.funnel, 'carrito_nativo');
await cycle([versionItem(flowId, A.funnel, 'carrito_nativo', CART)], 'v1 de carrito_nativo');
const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
  [flowId]), 'propuesta').proposal_id;
const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
  [flowId, proposalId, A.timezone]), 'abrir').r);
if (!opened.ok || opened.state !== 'cohorte') fail('abrir el carrito nativo', opened);
const cohort = async (contactId) => sql(
  `insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`, [flowId, contactId]);
const flowEventOf = (lead) => ({
  fuente: 'hotmart', tipo: 'carrito', evento_id: lead.event, producto_ref: A.funnel, oferta: lead.offer.offer_code,
  contacto_id: lead.contact, momento: lead.cartArgs[6], chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
  destino: lead.phone,
});
const admitNative = async (lead) => one((await asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
  [JSON.stringify(INSTANCE), JSON.stringify(flowEventOf(lead))])).rows)).filter((row) => row.flow_id === flowId),
`admision de ${lead.label}`);
const rejectionsOf = async (lead) => sql(`select reason, phase, count from public.flow_engine_start_rejections
  where flow_id = $1 and source_ref = $2 order by reason`, [flowId, lead.event]);
const casesOf = async (contactId) => one(await sql(
  'select count(*)::int as n from public.recovery_cases where contact_id = $1 and policy_key = $2',
  [contactId, `flujo:${flowId}`]), 'casos').n;

// La persona con dos contactos: el del mail (el del evento) y el que escribio
// primero por WhatsApp, sin mail ni telefono, duenio de la identidad activa del
// destino, como en las filas de Maribel.
const account = shape.identidad_del_destino.account_id.replace('<n>', String(A.accountId));
const lead = await cartFor('identidad-otro-contacto');
await cohort(lead.contact);
const other = one(await sql(`insert into public.contacts (full_name, email, phone) values (null, null, null)
  returning id`), 'contacto de WhatsApp').id;
const existing = await sql(`select id from public.channel_identities
  where channel = 'whatsapp' and account_id = $1 and external_user_id = $2`, [account, lead.phone]);
if (existing.length > 0) {
  await sql('update public.channel_identities set contact_id = $1 where id = $2', [other, existing[0].id]);
} else {
  await sql(`insert into public.channel_identities (contact_id, channel, account_id, external_user_id, identity_status, metadata)
    values ($1, $2, $3, $4, $5, jsonb_build_object('inbox_id', $6::bigint))`,
  [other, shape.identidad_del_destino.channel, account, lead.phone, shape.identidad_del_destino.identity_status, A.inboxId]);
}
const identityOwner = async () => one(await sql(`select contact_id from public.channel_identities
  where channel = 'whatsapp' and account_id = $1 and external_user_id = $2`, [account, lead.phone]), 'identidad').contact_id;
if (await identityOwner() !== other || /\D/.test(lead.phone)) fail('la identidad del destino es del otro contacto');

// ---------------------------------------------------------------------------
// 1. Rojo: sin la migracion.
// ---------------------------------------------------------------------------
const red = await admitNative(lead);
const redRows = await rejectionsOf(lead);
if (red.outcome !== 'motor_no_arranca' || red.reason !== 'error_interno:23514'
    || redRows.length !== 1 || redRows[0].reason !== 'error_interno:23514') {
  fail('sin la migracion se esperaba error_interno:23514', { red, redRows });
}
results.sin_la_migracion = red.reason;

// ---------------------------------------------------------------------------
// 2. Verde: con la migracion.
// ---------------------------------------------------------------------------
await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
const again = await cartFor(lead);
const green = await admitNative(again);
const greenRows = await rejectionsOf(again);
if (green.outcome !== 'motor_no_arranca' || green.reason !== NAMED || green.recovery_case_id !== null
    || green.scheduled_action_id !== null || green.created !== null) {
  fail('con la migracion se esperaba el rechazo con nombre', green);
}
if (greenRows.length !== 1 || greenRows[0].reason !== NAMED || greenRows[0].phase !== 'admision'
    || greenRows[0].count !== 1) {
  fail('la fila de rechazo con nombre', greenRows);
}
if (await casesOf(lead.contact) !== 0 || await casesOf(other) !== 0) fail('no tiene que abrir ningun caso');
if (await identityOwner() !== other) fail('la identidad sigue del otro contacto');
const alertable = one(await sql(`select public._flow_engine_rejection_is_alertable($1) as nuevo,
  public._flow_engine_rejection_is_alertable('error_interno:23514') as viejo`, [NAMED]), 'avisos');
if (alertable.nuevo !== false || alertable.viejo !== true) fail('el motivo nuevo no avisa como falla del motor', alertable);
results.con_la_migracion = { motivo: green.reason, fila: greenRows[0].reason, casos: 0, avisa: alertable.nuevo };

// ---------------------------------------------------------------------------
// 3. Lo demas, igual.
// ---------------------------------------------------------------------------
// Otro 23514: la identidad del mismo contacto, pero de otra bandeja.
const inbox = await cartFor('identidad-otra-bandeja');
await cohort(inbox.contact);
const inboxIdentity = await sql(`select id from public.channel_identities
  where channel = 'whatsapp' and account_id = $1 and external_user_id = $2`, [account, inbox.phone]);
if (inboxIdentity.length > 0) {
  await sql(`update public.channel_identities set contact_id = $1, metadata = jsonb_build_object('inbox_id', $2::bigint)
    where id = $3`, [inbox.contact, A.inboxId + 1000, inboxIdentity[0].id]);
} else {
  await sql(`insert into public.channel_identities (contact_id, channel, account_id, external_user_id, identity_status, metadata)
    values ($1, 'whatsapp', $2, $3, 'active', jsonb_build_object('inbox_id', $4::bigint))`,
  [inbox.contact, account, inbox.phone, A.inboxId + 1000]);
}
const otherCheck = await admitNative(inbox);
if (otherCheck.outcome !== 'motor_no_arranca' || otherCheck.reason !== 'error_interno:23514') {
  fail('otro 23514 sigue siendo error_interno:23514', otherCheck);
}
// Una persona sin choque arranca.
const clean = await cartFor('identidad-sin-choque');
await cohort(clean.contact);
const started = await admitNative(clean);
if (started.outcome !== 'motor_arranco' || started.created !== true) fail('sin choque arranca', started);
results.lo_demas = { otra_bandeja: otherCheck.reason, sin_choque: started.outcome };

await db.close();
console.log(JSON.stringify({ ok: true, ...results }));
