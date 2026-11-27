// El caso de un flujo nativo de audiencia lleva al despacho el producto del
// evento que lo abrio (migracion 20261117000500, E-661).
//
// Por que: en Maribel, el 2026-11-17 16:01-16:10Z, aviso_fecha_prueba y
// aviso_diario_prueba (cohorte) liberaron su ocurrencia, el motor arranco y
// cada envio termino en 4 intentos failed_before_request / pre_request_failed
// con followup_recipient_not_allowlisted en el log del bridge. El despachador
// pide que execution_context.offer_code sea una de las ofertas aceptadas y un
// caso de audiencia nace sin oferta (recovery_cases.offer_code y
// context.flow_engine_offer nulos, context.flow_engine_trigger = audiencia).
// validate_flow_audience_native_e2e.mjs no lo vio: la primera plantilla sale
// por la puerta de la base sin pasar por get_followup_execution_context.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs y el evento que
// arma el bridge (src/bridge/flow_audience_opener.py, audience_event: esas
// claves y ninguna otra), con la clave del opener (native_audience) como en
// validate_flow_audience_native_e2e.mjs. La forma del flujo es la de los avisos
// de prueba de Maribel (dispara.manual a prospectas, plantilla con nombre y
// producto).
//
// Prueba, con la cadena entera y un rojo a proposito en el mismo mundo:
//   0. la migracion: cambia solo get_followup_execution_context, con su marca,
//      sin tocar permisos ni atributos; correrla dos veces no hace nada;
//   1. rojo: con todo menos esta migracion, el contexto del despacho del caso
//      de audiencia no trae oferta ni producto (lo que vio produccion);
//   2. con la migracion: el mismo caso trae flow_engine_audience_product_ref =
//      el producto_ref del evento, y la oferta sigue nula; las trece columnas
//      de antes son las mismas;
//   (un caso que no es de audiencia trae la columna nula: lo afirma
//   validate_flow_engine_native_offer_in_context.mjs con la compra de ATT1.)
//
// Con --emit escribe tests/fixtures/flow_engine/native_audience_execution_context_rows.json
// (lo que contesta la base, con uuid y horas normalizados) para
// tests/test_flow_engine_native_audience_recipient.py; con --check (el de npm
// test) lo compara y falla si difiere.
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261117000500_flow_engine_native_audience_recipient_v1.sql';
const FIXTURE = join(root, 'tests/fixtures/flow_engine/native_audience_execution_context_rows.json');
const EMIT = process.argv.includes('--emit');
const CHECK = process.argv.includes('--check');
const CONTEXT_FN = 'get_followup_execution_context(uuid,text,bigint,timestamp with time zone)';
const MARK = 'flow_engine_native_audience_recipient';
const COLUMN = 'flow_engine_audience_product_ref';
const OLD_COLUMNS = ['action_id', 'action_type', 'step_key', 'recovery_case_id', 'contact_id', 'source_event_id',
  'buyer_name', 'buyer_email', 'buyer_phone', 'product_name', 'offer_code', 'current_goal', 'lead_stage'];
const results = {};
const sha = (text) => createHash('sha256').update(text).digest('hex');

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues va solo la repeticion de un flujo de audiencia (20261117000600, E-665): no toca el contexto del despacho.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261117000600_flow_audience_repeat_counts_started_v1.sql', '20261117000700_flow_audience_start_reasons_v1.sql',
  // El numero de conversacion de los avisos del correlador (20261118000300, E-656): no toca el contexto del despacho.
  '20261118000300_identity_link_conversation_number_account_v1.sql',
  // El choque de identidad en la admision es un rechazo con nombre (20261125000100).
  '20261125000100_flow_engine_admission_identity_mismatch_v1.sql',
  // El almacen del masivo (20261125000200).
  '20261125000200_bulk_send_store_v1.sql',
  // Preparar y los vetos del masivo (20261125000300).
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
    '20261127500000_own_forms_registration_confirmation_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de la repeticion de E-665`, names.slice(position + 1));
}

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile
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
  const changed = [...before.keys()].filter((s) => after.get(s)?.definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && ['acl', 'prosecdef', 'provolatile'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, [CONTEXT_FN]) || added.length !== 0 || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  if (!after.get(CONTEXT_FN).definition.includes(MARK)) fail('la funcion no tiene la marca');
  if (after.get(CONTEXT_FN).acl !== before.get(CONTEXT_FN).acl) fail('los permisos cambiaron');
  for (const role of ['anon', 'authenticated']) {
    const can = one((await db.query(`select has_function_privilege($1, 'public.${CONTEXT_FN}', 'EXECUTE') as ok`,
      [role])).rows, role).ok;
    if (can) fail(`${role} puede ejecutar el contexto del despacho`);
  }
  // Idempotente: la segunda corrida no cambia nada.
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const again = await snapshot(db);
  if ([...after.keys()].some((s) => JSON.stringify(again.get(s)) !== JSON.stringify(after.get(s)))) {
    fail('la segunda corrida cambio algo');
  }
  // Con otra definicion (alguien la cambio), aborta sin tocar.
  await db.exec(`create or replace function public.get_followup_execution_context(
      p_action_id uuid, p_worker_id text, p_lease_generation bigint, p_now timestamptz)
    returns table (action_id uuid, action_type text, step_key text, recovery_case_id uuid, contact_id uuid,
                   source_event_id uuid, buyer_name text, buyer_email text, buyer_phone text, product_name text,
                   offer_code text, current_goal text, lead_stage text, ${COLUMN} text)
    language plpgsql set search_path = public, pg_temp as $f$ begin return; end; $f$`);
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('con otra definicion tendria que abortar');
  } catch (error) {
    if (error.message !== 'unexpected_followup_execution_context_definition') {
      fail('con otra definicion aborto por otra cosa', error.message);
    }
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { cambiadas: changed.length, iguales: before.size - changed.length, segunda_corrida: 'nada' };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 (buildWorld, la cadena entera) sin esta migracion.
// ---------------------------------------------------------------------------
// La palabra clave (20261127400000, Z2 3.9) exige esta migracion: se salta y se aplica despues de ella.
const KEYWORD = '20261127400000_flow_engine_keyword_trigger_v1.sql';
const world = await buildWorld({ skip: [MIGRATION, KEYWORD] });
const { db, A, A2, INSTANCE, person, admitForm, register, cycle, versionItem, reserve, dbNow } = world;
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

// El aviso a mano, como los avisos de prueba de Maribel.
const FLOW_KEY = 'aviso_a_mano';
const definition = {
  esquema: 'setter-flujo/v1', flujo: FLOW_KEY, producto: A.funnel, estado: 'borrador', nombre: 'Aviso a mano',
  dispara: { manual: {} },
  para: { es: 'prospecta', formulario_en: '30d' },
  entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ ...A2 }],
  termina: { si: ['compra', 'contesta'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-aviso-a-mano', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona', otra_despues_de: '7d' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  ritmo: { por_hora: 200, por_ocurrencia: 1000 },
};
const flowId = await register(A.funnel, FLOW_KEY);
await cycle([versionItem(flowId, A.funnel, FLOW_KEY, definition)], `v1 de ${FLOW_KEY}`);
const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
  [flowId]), 'propuesta').proposal_id;
const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
  [flowId, proposalId, A.timezone]), 'abrir').r);
if (!opened.ok || opened.state !== 'cohorte') fail('abrir el aviso en el nativo', opened);

const ana = person('audiencia-ana');
await admitForm(ana, new Date((await dbNow()).getTime() - 50 * 60_000));
ana.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
  values ($1, $2, $3, 'MX') returning id`, [ana.name, ana.email, ana.phone]), 'contacto').id;
await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
  values ($1, 'email', $2, $2, 'system'), ($1, 'phone', $3, $3, 'system')`, [ana.contact, ana.email, ana.phone]);
await sql('insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, \'dan\')',
  [flowId, ana.contact]);

const flows = await call('public.read_flow_audience_flows_v1($1::jsonb)', [JSON.stringify(INSTANCE)]);
const running = flows.find((row) => row.flow_id === flowId);
if (!running || running.version !== 1 || running.product_ref !== A.funnel) fail('el aviso corre para el bridge', flows);
const DAN = 'U0DANSCHWAB1';
const inMinutes = async (minutes) => new Date((await dbNow()).getTime() + minutes * 60_000).toISOString();
const session = sha('e661-session');
await as('flow_change_approver', async () => {
  const state = sha('e661-state');
  await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha('e661-nonce'), '/flow-approval/', await inMinutes(10));
  await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
  const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, 'T0LANCEMOS01', DAN,
    await inMinutes(60));
  if (!created.ok) fail('entrar con Slack', created);
});
const materialized = await call(
  'public.materialize_flow_audience_occurrence_v1($1::jsonb, $2::uuid, $3::integer, $4::jsonb, $5::jsonb)',
  [JSON.stringify(INSTANCE), flowId, 1,
    JSON.stringify({ key: 'manual:66166166-1661-4661-8661-661661661661', scheduled_at: (await dbNow()).toISOString(),
      fingerprint: 'e'.repeat(64), materialized_by: `slack:${DAN}` }),
    JSON.stringify([{ contact_id: ana.contact, phone: ana.phone }])]);
if (!materialized.ok || materialized.member_count !== 1) fail('la lista', materialized);
const OCCURRENCE = materialized.occurrence_id;
const confirmed = await as('flow_change_approver',
  () => rpc('approver_confirm_flow_audience_v1', INSTANCE, session, OCCURRENCE, 1, true));
if (!confirmed.ok) fail('confirmar', confirmed);
const released = await call('public.release_flow_audience_members_v1($1::uuid, now(), 50)', [OCCURRENCE]);
const [member] = released.released ?? [];
if (released.released?.length !== 1 || member.contact_id !== ana.contact) fail('la base suelta a Ana', released);

// El evento del opener (flow_audience_opener.audience_event): exactamente estas claves.
const flowEvent = {
  fuente: 'audiencia', tipo: 'manual', evento_id: `audiencia:${OCCURRENCE}:${member.contact_id}`, flujo_id: flowId,
  version: running.version, ocurrencia_id: OCCURRENCE, producto_ref: running.product_ref,
  contacto_id: member.contact_id, momento: (await dbNow()).toISOString(),
  chatwoot: { account_id: A.accountId, inbox_id: A.inboxId }, destino: member.phone,
};
const rows = await as('service_role', async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
  [JSON.stringify({ ...INSTANCE, native_audience: true }), JSON.stringify(flowEvent)])).rows);
const started = one(rows.filter((row) => row.flow_id === flowId), 'fila del aviso');
if (started.outcome !== 'motor_arranco' || started.created !== true) fail('el motor arranca', rows);
const kase = one(await sql('select * from public.recovery_cases where id = $1', [started.recovery_case_id]), 'caso');
if (kase.offer_code !== null || kase.context.flow_engine_offer !== null
    || kase.context.flow_engine_trigger !== 'audiencia' || kase.policy_key !== `flujo:${flowId}`
    || kase.product_name !== A.productName) {
  fail('el caso de audiencia (como el 083dc040... de Maribel)', { offer: kase.offer_code, context: kase.context });
}
results.admision = { salida: started.outcome, oferta: kase.offer_code, disparo: kase.context.flow_engine_trigger };

// ---------------------------------------------------------------------------
// 1 y 2. El contexto del despacho, sin la migracion y con ella.
// ---------------------------------------------------------------------------
const reserved = await reserve(ana, started.scheduled_action_id);
if (reserved.attempt.mode !== 'approved_template') fail('el intento', reserved.attempt);
const readContext = () => as('service_role', async () => one((await db.query(
  'select * from public.get_followup_execution_context($1, $2, $3, $4)',
  [started.scheduled_action_id, reserved.worker, reserved.lease, reserved.now])).rows, 'contexto'));
const red = await readContext();
if (red.offer_code !== null || COLUMN in red) fail('rojo: sin la migracion no hay oferta ni producto', red);
await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
await applyFile(db, join(root, 'supabase/migrations', KEYWORD));
const context = await readContext();
if (context[COLUMN] !== A.funnel || context.offer_code !== null || context.product_name !== A.productName
    || context.buyer_phone !== ana.phone) {
  fail('con la migracion: el producto del evento', context);
}
for (const column of OLD_COLUMNS) {
  if (JSON.stringify(context[column]) !== JSON.stringify(red[column])) fail(`la columna ${column} cambio`, column);
}
results.contexto = { producto: context[COLUMN], oferta: context.offer_code, sin_la_migracion: red[COLUMN] ?? null };

// ---------------------------------------------------------------------------
// El fixture para el bridge.
// ---------------------------------------------------------------------------
const ids = new Map([[started.scheduled_action_id, 'ACTION'], [started.recovery_case_id, 'CASE'],
  [ana.contact, 'CONTACT'], [kase.abandonment_event_id, 'EVENT'], [flowId, 'FLOW'], [OCCURRENCE, 'OCCURRENCE']]);
const normalize = (value) => JSON.parse(JSON.stringify(value, (key, v) => {
  if (typeof v === 'string') {
    if (ids.has(v)) return `<${ids.get(v)}>`;
    if (key === 'evento_id') return `audiencia:<OCCURRENCE>:<CONTACT>`;
  }
  if (key === 'momento') return '<MOMENTO>';
  return v;
}));
const contextRow = (row) => normalize(Object.fromEntries(
  [...OLD_COLUMNS, ...(COLUMN in row ? [COLUMN] : [])].map((column) => [column, row[column]])));
const emitted = {
  _capture: {
    origen: 'validate_flow_engine_native_audience_recipient.mjs: la cadena entera de migraciones (PGlite) con el '
      + 'evento de flow_audience_opener.audience_event; el caso replica 083dc040.../602c59d3... de Maribel '
      + '(2026-11-17 16:01-16:10Z): offer_code y context.flow_engine_offer nulos, flow_engine_trigger audiencia',
    encargo: 'E-661',
    regenerar: 'cd tests/sql/followup_engine && node validate_flow_engine_native_audience_recipient.mjs --emit',
  },
  instance: { tenant_ref: A.tenant, funnel_ref: A.funnel, product_name: A.productName },
  flow_event: normalize(flowEvent),
  admission: normalize({ flow_id: started.flow_id, outcome: started.outcome, created: started.created }),
  case_context: { flow_engine_trigger: kase.context.flow_engine_trigger, flow_engine_offer: kase.context.flow_engine_offer ?? null,
    offer_code: kase.offer_code },
  execution_context: contextRow(context),
  execution_context_before_migration: contextRow(red),
};
if (EMIT) {
  writeFileSync(FIXTURE, `${JSON.stringify(emitted, null, 2)}\n`);
} else if (CHECK) {
  const stored = JSON.parse(readFileSync(FIXTURE, 'utf8'));
  if (JSON.stringify(stored) !== JSON.stringify(emitted)) {
    fail('native_audience_execution_context_rows.json no es lo que da la cadena: correr con --emit', emitted);
  }
}
await db.close();
console.log(`validate_flow_engine_native_audience_recipient: ok ${JSON.stringify(results)}`);
