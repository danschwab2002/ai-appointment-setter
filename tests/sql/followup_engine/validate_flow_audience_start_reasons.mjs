// Cada envio a una audiencia dice cuantas personas arrancaron, cuantas no y por
// que (migracion 20261117000700, E-666).
//
// Por que: en Maribel, el 2026-11-16 17:09Z, la ocurrencia 8ed2f5be... de
// aviso_manual_prueba termino con su unica persona liberada y
// open_outcome = no_arranca; el motivo (conversacion_viva) quedo en
// flow_engine_start_rejections, atado al evento sintetico de esa persona, y la
// pagina de envios no lo mostraba: ninguna lectura de la pagina lo devolvia.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs, el aviso a mano
// de validate_flow_engine_native_audience_recipient.mjs (la forma de los avisos
// de prueba de Maribel), el evento que arma el bridge
// (src/bridge/flow_audience_opener.py, audience_event) y la conversacion viva
// de validate_e483_native_audience_source.mjs (un entrante hace 2 h).
//
// Prueba, con la cadena entera y un rojo a proposito en el mismo mundo:
//   0. la migracion: cambia solo _flow_audience_occurrence_rows, con su marca,
//      sin tocar permisos ni atributos; la definicion de la cadena es la de
//      produccion (md5 del cuerpo, maribel_db y att1-db el 17/11); correrla dos
//      veces no hace nada; con otra definicion aborta;
//   1. rojo: sin la migracion, la lectura de la pagina
//      (approver_read_flow_audience_v1, con la sesion) no dice por que no
//      arranco nadie (lo que vio produccion);
//   2. con la migracion, la misma lectura: Ana arranco; Bea no (conversacion
//      viva, el rechazo de la admision); Caro no y sin rechazo registrado
//      (sin_registro); Dora sin flujo; Eva todavia sin asentar. Las claves de
//      antes son las mismas, y la lectura del bridge
//      (read_flow_audience_occurrences_v1) trae lo mismo. Sin telefonos.
//
// Con --emit escribe tests/fixtures/flow_audience/occurrence_start_reasons_rows.json
// (la ocurrencia que contesta la base, con uuid y horas normalizados) para
// tests/test_flow_audience_start_reasons.py; con --check (el de npm test) lo
// compara y falla si difiere.
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261117000700_flow_audience_start_reasons_v1.sql';
const FIXTURE = join(root, 'tests/fixtures/flow_audience/occurrence_start_reasons_rows.json');
const EMIT = process.argv.includes('--emit');
const CHECK = process.argv.includes('--check');
const ROWS_FN = '_flow_audience_occurrence_rows(text,text,integer)';
const MARK = 'flow_audience_start_reasons';
// El cuerpo de 20261112220000 en produccion (maribel_db y att1-db, 2026-11-17).
const PRODUCTION_MD5 = 'fbddc394830be11898b98dc144b8a6d6';
const NEW_KEYS = ['not_started_count', 'no_flow_count', 'unsettled_count', 'not_started_by_reason'];
const results = {};
const sha = (text) => createHash('sha256').update(text).digest('hex');
// jsonb no guarda el orden de las claves: se comparan ordenadas.
const canon = (value) => JSON.stringify(value, (_, v) => (v && typeof v === 'object' && !Array.isArray(v)
  ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, v[k]])) : v));

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues va solo el numero de conversacion de los avisos del correlador (20261118000300, E-656), que no toca
// las ocurrencias.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261118000300_identity_link_conversation_number_account_v1.sql', '20261125000100_flow_engine_admission_identity_mismatch_v1.sql', '20261125000200_bulk_send_store_v1.sql', '20261125000300_bulk_send_prepared_and_base_vetoes_v1.sql', '20261125000400_bulk_send_recipients_read_v1.sql', '20261126100000_agent_global_pause_v1.sql',
    // La instruccion temporal (20261126200000, 5.3 de Z1): tablas y RPC propias, no toca lo que se prueba aca.
    '20261126200000_agent_temporary_instructions_v1.sql',
    // Exportar contactos (20261126300000).
    '20261126300000_contact_export_v1.sql',
    // Las reglas de palabra clave (20261126400000, Z1 3.9).
    '20261126400000_keyword_rules_v1.sql',
    // Las corridas del juez (20261126500000, 4.9).
    '20261126500000_agent_campaign_judge_runs_v1.sql',
    // El conocimiento vivo del AE (20261126600000, pieza 5.5).
    '20261126600000_agent_live_knowledge_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de E-656`, names.slice(position + 1));
}

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile, md5(p.prosrc) as md5
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
  if (before.get(ROWS_FN)?.md5 !== PRODUCTION_MD5) {
    fail('la definicion de la cadena no es la de produccion', before.get(ROWS_FN)?.md5);
  }
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.get(s)?.definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && ['acl', 'prosecdef', 'provolatile'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, [ROWS_FN]) || added.length !== 0 || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  if (!after.get(ROWS_FN).definition.includes(MARK)) fail('la funcion no tiene la marca');
  for (const role of ['anon', 'authenticated', 'service_role', 'flow_change_approver']) {
    const can = one((await db.query(`select has_function_privilege($1, 'public.${ROWS_FN}', 'EXECUTE') as ok`,
      [role])).rows, role).ok;
    if (can) fail(`${role} puede ejecutar la lectura privada de las ocurrencias`);
  }
  // Idempotente: la segunda corrida no cambia nada.
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const again = await snapshot(db);
  if ([...after.keys()].some((s) => JSON.stringify(again.get(s)) !== JSON.stringify(after.get(s)))) {
    fail('la segunda corrida cambio algo');
  }
  // Con otra definicion (alguien la cambio), aborta sin tocar.
  await db.exec(`create or replace function public._flow_audience_occurrence_rows(
      p_tenant_ref text, p_ally_ref text, p_limit integer)
    returns jsonb language sql stable set search_path = pg_catalog, public, pg_temp as $f$ select '[]'::jsonb $f$`);
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('con otra definicion tendria que abortar');
  } catch (error) {
    if (error.message !== 'unexpected_flow_audience_occurrence_rows_definition') {
      fail('con otra definicion aborto por otra cosa', error.message);
    }
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { cambiadas: changed.length, md5_produccion: 'igual', segunda_corrida: 'nada' };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 (buildWorld, la cadena entera) sin esta migracion.
// ---------------------------------------------------------------------------
const world = await buildWorld({ skip: [MIGRATION] });
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

// Cinco prospectas con formulario, en la cohorte.
const lead = async (label) => {
  const p = person(label);
  await admitForm(p, new Date((await dbNow()).getTime() - 50 * 60_000));
  p.contact = one(await sql(`insert into public.contacts (full_name, email, phone, country_iso)
    values ($1, $2, $3, 'MX') returning id`, [p.name, p.email, p.phone]), 'contacto').id;
  await sql(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1, 'email', $2, $2, 'system'), ($1, 'phone', $3, $3, 'system')`, [p.contact, p.email, p.phone]);
  await sql('insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, \'dan\')',
    [flowId, p.contact]);
  return p;
};
const ana = await lead('aviso-ana');
const bea = await lead('aviso-bea');
const caro = await lead('aviso-caro');
const dora = await lead('aviso-dora');
const eva = await lead('aviso-eva');
// Bea nos escribio hace 2 h (la conversacion viva de validate_e483_native_audience_source.mjs).
const beaIdentity = one(await sql(`insert into public.channel_identities
    (contact_id, channel, account_id, external_user_id, identity_status, metadata)
    values ($1, 'whatsapp', $2, $3, 'active', $4::jsonb) returning id`,
[bea.contact, `chatwoot:${A.accountId}`, bea.phone, JSON.stringify({ inbox_id: A.inboxId })]), 'identidad').id;
const beaConversation = one(await sql(`insert into public.conversations (contact_id, channel_identity_id)
    values ($1, $2) returning id`, [bea.contact, beaIdentity]), 'conversacion').id;
await sql(`insert into public.messages (conversation_id, direction, actor_type, content, occurred_at)
    values ($1, 'inbound', 'prospect', 'Hola, una duda', now() - interval '2 hours')`, [beaConversation]);

const flows = await call('public.read_flow_audience_flows_v1($1::jsonb)', [JSON.stringify(INSTANCE)]);
const running = flows.find((row) => row.flow_id === flowId);
if (!running || running.version !== 1) fail('el aviso corre para el bridge', flows);
const DAN = 'U0DANSCHWAB1';
const inMinutes = async (minutes) => new Date((await dbNow()).getTime() + minutes * 60_000).toISOString();
const session = sha('e666-session');
await as('flow_change_approver', async () => {
  const state = sha('e666-state');
  await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha('e666-nonce'), '/flow-approval/', await inMinutes(10));
  await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
  const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, 'T0LANCEMOS01', DAN,
    await inMinutes(60));
  if (!created.ok) fail('entrar con Slack', created);
});
const people = [ana, bea, caro, dora, eva];
const materialized = await call(
  'public.materialize_flow_audience_occurrence_v1($1::jsonb, $2::uuid, $3::integer, $4::jsonb, $5::jsonb)',
  [JSON.stringify(INSTANCE), flowId, 1,
    JSON.stringify({ key: 'manual:66666666-6666-4666-8666-666666666666', scheduled_at: (await dbNow()).toISOString(),
      fingerprint: 'e'.repeat(64), materialized_by: `slack:${DAN}` }),
    JSON.stringify(people.map((p) => ({ contact_id: p.contact, phone: p.phone })))]);
if (!materialized.ok || materialized.member_count !== 5) fail('la lista', materialized);
const OCCURRENCE = materialized.occurrence_id;
const confirmed = await as('flow_change_approver',
  () => rpc('approver_confirm_flow_audience_v1', INSTANCE, session, OCCURRENCE, 5, true));
if (!confirmed.ok) fail('confirmar', confirmed);
const released = await call('public.release_flow_audience_members_v1($1::uuid, now(), 50)', [OCCURRENCE]);
if (released.released?.length !== 5) fail('la base suelta a las cinco', released);
const phoneOf = new Map(released.released.map((m) => [m.contact_id, m.phone]));

// El opener del bridge: Ana y Bea pasan por la admision nativa; lo que contesta se asienta.
const admit = async (p) => {
  const flowEvent = {
    fuente: 'audiencia', tipo: 'manual', evento_id: `audiencia:${OCCURRENCE}:${p.contact}`, flujo_id: flowId,
    version: running.version, ocurrencia_id: OCCURRENCE, producto_ref: running.product_ref,
    contacto_id: p.contact, momento: (await dbNow()).toISOString(),
    chatwoot: { account_id: A.accountId, inbox_id: A.inboxId }, destino: phoneOf.get(p.contact),
  };
  const rows = await as('service_role', async () => (await db.query(
    'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
    [JSON.stringify({ ...INSTANCE, native_audience: true }), JSON.stringify(flowEvent)])).rows);
  return one(rows.filter((row) => row.flow_id === flowId), `fila del aviso de ${p.label}`);
};
const settle = (p, outcome) => as('service_role', () => rpc('settle_flow_audience_member_v1', OCCURRENCE, p.contact,
  outcome));
const anaRow = await admit(ana);
if (anaRow.outcome !== 'motor_arranco') fail('Ana arranca', anaRow);
await settle(ana, 'arranco');
const beaRow = await admit(bea);
if (beaRow.outcome !== 'motor_no_arranca' || beaRow.reason !== 'conversacion_viva') fail('Bea no arranca', beaRow);
await settle(bea, 'no_arranca');
const rejections = await sql('select reason, phase from public.flow_engine_start_rejections where flow_id = $1',
  [flowId]);
if (rejections.length !== 1 || rejections[0].reason !== 'conversacion_viva') {
  fail('el rechazo de Bea queda registrado (como 1515e3a7... de Maribel)', rejections);
}
// Caro: la admision contesto con un error permanente (el opener asienta no_arranca sin rechazo en la base).
await settle(caro, 'no_arranca');
// Dora: el flujo cambio de version entre soltar y abrir (sin_flujo).
await settle(dora, 'sin_flujo');
// Eva: todavia sin asentar.
results.arranque = { ana: anaRow.outcome, bea: `${beaRow.outcome}:${beaRow.reason}`, caro: 'no_arranca',
  dora: 'sin_flujo', eva: 'sin asentar' };

// ---------------------------------------------------------------------------
// 1 y 2. La lectura de la pagina, sin la migracion y con ella.
// ---------------------------------------------------------------------------
const pageRow = async () => {
  const read = await as('flow_change_approver', () => rpc('approver_read_flow_audience_v1', INSTANCE, session));
  return one(read.occurrences.filter((row) => row.occurrence_id === OCCURRENCE), 'la ocurrencia en la pagina');
};
const red = await pageRow();
if (NEW_KEYS.some((key) => key in red) || red.released_count !== 5 || red.started_count !== 1) {
  fail('rojo: sin la migracion la pagina no dice por que no arrancaron', red);
}
await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
const row = await pageRow();
const expected = { not_started_count: 2, no_flow_count: 1, unsettled_count: 1,
  not_started_by_reason: { conversacion_viva: 1, sin_registro: 1 } };
for (const [key, value] of Object.entries(expected)) {
  if (canon(row[key]) !== canon(value)) fail(`con la migracion: ${key}`, row);
}
for (const key of Object.keys(red)) {
  if (canon(row[key]) !== canon(red[key])) fail(`la clave ${key} cambio`, { red, row });
}
const bridgeRows = await as('service_role',
  () => call('public.read_flow_audience_occurrences_v1($1::jsonb, 10)', [JSON.stringify(INSTANCE)]));
const bridgeRow = one(bridgeRows.filter((r) => r.occurrence_id === OCCURRENCE), 'la ocurrencia del bridge');
if (canon(bridgeRow) !== canon(row)) fail('la lectura del bridge trae lo mismo', bridgeRow);
const text = JSON.stringify(row);
for (const p of people) {
  if (text.includes(p.phone) || text.includes(p.contact) || text.includes(p.email)) fail('la fila lleva datos', p.label);
}
results.pagina = { rojo: 'sin motivos', con_la_migracion: expected };

// ---------------------------------------------------------------------------
// El fixture para el bridge.
// ---------------------------------------------------------------------------
const ids = new Map([[flowId, '<FLOW>'], [OCCURRENCE, '<OCCURRENCE>']]);
const normalize = (value) => JSON.parse(canon(JSON.parse(JSON.stringify(value, (key, v) => {
  if (typeof v === 'string') {
    if (ids.has(v)) return ids.get(v);
    if (/_at$/.test(key)) return `<${key.toUpperCase()}>`;
  }
  return v;
}))));
const emitted = {
  _capture: {
    origen: 'validate_flow_audience_start_reasons.mjs: la cadena entera de migraciones (PGlite), la lectura de la '
      + 'pagina (approver_read_flow_audience_v1) de una ocurrencia manual con cinco personas: una arranco, una no '
      + 'por conversacion viva (el caso 8ed2f5be... de Maribel, 2026-11-16 17:09Z), una no sin rechazo registrado, '
      + 'una sin flujo y una sin asentar',
    encargo: 'E-666',
    regenerar: 'cd tests/sql/followup_engine && node validate_flow_audience_start_reasons.mjs --emit',
  },
  occurrence: normalize(row),
  occurrence_before_migration: normalize(red),
};
if (EMIT) {
  mkdirSync(dirname(FIXTURE), { recursive: true });
  writeFileSync(FIXTURE, `${JSON.stringify(emitted, null, 2)}\n`);
} else if (CHECK) {
  const stored = JSON.parse(readFileSync(FIXTURE, 'utf8'));
  if (canon(stored) !== canon(emitted)) {
    fail('occurrence_start_reasons_rows.json no es lo que da la cadena: correr con --emit', emitted);
  }
}
await db.close();
console.log(`validate_flow_audience_start_reasons: ok ${JSON.stringify(results)}`);
