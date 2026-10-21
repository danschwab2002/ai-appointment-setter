// automation-expert-change-v1 (20261021000100): el rol del Automation Expert
// para proponer cambios de flujo, sus cuatro envoltorios y aprobar con la
// simulacion congelada en la vista previa.
//
// Corre sobre la cadena completa, con los roles y los privilegios por defecto
// de Supabase, y con authenticator NOINHERIT LOGIN como en la imagen de ATT1.
// Los pasos:
//   1. el rol: NOLOGIN NOINHERIT, concedido a authenticator con set y sin
//      inherit; el bloque se vuelve a aplicar sin error y falla cerrado con
//      anon como miembro;
//   2. los permisos: el rol ejecuta exactamente sus cuatro funciones y ninguna
//      otra de public; anon, authenticated y service_role no ejecutan
//      ninguna de las cuatro; el inventario de permisos todo ok y la huella
//      de 20261021000100 presente;
//   3. el ciclo del agente como el rol: lee el almacen, guarda un borrador,
//      registra la validacion y relee la propuesta; las operator_*, el SELECT
//      directo y el INSERT directo dan permission denied;
//   4. aprobar registra la simulacion de la vista previa: built con el sha256,
//      failed con su codigo, y not_built sin simulacion o con una forma rota;
//   5. los topes del agente: 10 abiertas por instancia, 100 revisiones y 100
//      validaciones por propuesta; el operador sin tope;
//   6. como PostgREST: set session authorization authenticator y set role al
//      rol; set role postgres falla (va al final: en PGlite, set session
//      authorization no vuelve atras con reset).

import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION_FILE = '20261021000100_automation_expert_change_v1.sql';
const BASELINE = join(root, 'supabase/baseline/20260803_public_schema.sql');
const MIGRATIONS = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
if (!MIGRATIONS.includes(MIGRATION_FILE)) throw new Error(`${MIGRATION_FILE} is missing`);
const SCHEMA_INVENTORY = readFileSync(join(root, 'scripts/supabase_schema_inventory.sql'), 'utf8');
const ACL_INVENTORY = readFileSync(join(root, 'scripts/supabase_acl_inventory.sql'), 'utf8');
const MIGRATION_SQL = readFileSync(join(root, 'supabase/migrations', MIGRATION_FILE), 'utf8');
const ROLE_BLOCK = MIGRATION_SQL.slice(MIGRATION_SQL.indexOf('do $role$'), MIGRATION_SQL.indexOf('$role$;') + 7);

const WRAPPERS = [
  'read_flow_store_v1(jsonb)',
  'save_flow_change_draft_v1(jsonb,uuid,integer,jsonb,jsonb,text,text)',
  'record_flow_change_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,jsonb,text)',
  'get_flow_change_proposal_v1(jsonb,uuid)',
];
const INSTANCE = { tenant_ref: 'lancemos', ally_ref: 'att1' };
const AE = 'U0123456789';
const VALIDATOR_AE = {
  contract: 'setter-flujo/v1', rules_version: 1, phase: 'publish', actor: 'automation_expert',
  product_version: '1.15.5', git_sha: 'b163669',
};
const CLEAN = { blocks: [], warnings: [] };
const SHA = 'a'.repeat(64);

function require(condition, message) {
  if (!condition) throw new Error(message);
}

const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin bypassrls;
  create role authenticator noinherit login;
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
  alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
  alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
`);
const pglite = (sql) => sql.replace(/create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite');
await db.exec(pglite(readFileSync(BASELINE, 'utf8')));
for (const name of MIGRATIONS) {
  await db.exec(pglite(readFileSync(join(root, 'supabase/migrations', name), 'utf8')));
}

const one = async (sql, params = []) => (await db.query(sql, params)).rows[0];
const call = async (fn, ...params) => {
  const marks = params.map((_, i) => `$${i + 1}`).join(', ');
  return (await one(`select public.${fn}(${marks})::text as r`, params.map(
    (p) => (p !== null && typeof p === 'object' ? JSON.stringify(p) : p)))).r;
};
const json = async (fn, ...params) => JSON.parse(await call(fn, ...params));
const denied = async (sql, label) => {
  try {
    await db.query(sql);
  } catch (error) {
    require(error.code === '42501', `${label}: expected 42501, got ${error.code} ${error.message}`);
    return;
  }
  throw new Error(`${label}: expected permission denied`);
};
const asAgent = async (fn) => {
  await db.exec('set role automation_expert_change');
  try {
    return await fn();
  } finally {
    await db.exec('reset role');
  }
};

// 1. El rol.
const role = await one(`select rolcanlogin, rolinherit, rolsuper, rolbypassrls, rolcreaterole
  from pg_roles where rolname = 'automation_expert_change'`);
require(role && !role.rolcanlogin && !role.rolinherit && !role.rolsuper && !role.rolbypassrls
  && !role.rolcreaterole, `role attributes: ${JSON.stringify(role)}`);
const membership = (await db.query(`select m.rolname member, a.inherit_option, a.set_option
  from pg_auth_members a join pg_roles m on m.oid = a.member
  where a.roleid = 'automation_expert_change'::regrole order by 1`)).rows;
const viaAuthenticator = membership.filter((row) => row.member === 'authenticator');
require(viaAuthenticator.length === 1 && viaAuthenticator[0].set_option && !viaAuthenticator[0].inherit_option,
  `membership: ${JSON.stringify(membership)}`);
require(membership.every((row) => row.member === 'authenticator' || (!row.inherit_option && !row.set_option)),
  `unexpected member: ${JSON.stringify(membership)}`);
await db.exec(ROLE_BLOCK);  // segunda pasada, como en otra base del mismo cluster
await db.exec('grant automation_expert_change to anon');
try {
  await db.exec(ROLE_BLOCK);
  throw new Error('the role block did not fail closed with anon as member');
} catch (error) {
  require(String(error.message).includes('automation_expert_change_role_unsafe'), `role block: ${error.message}`);
}
await db.exec('revoke automation_expert_change from anon');
console.log('automation_expert_change_role=OK');

// 2. Los permisos.
const executable = (await db.query(`select p.proname || '(' || oidvectortypes(p.proargtypes) || ')' sig
  from pg_proc p where p.pronamespace = 'public'::regnamespace
   and has_function_privilege('automation_expert_change', p.oid, 'execute') order by 1`)).rows.map((r) => r.sig);
require(JSON.stringify(executable) === JSON.stringify([
  'get_flow_change_proposal_v1(jsonb, uuid)',
  'read_flow_store_v1(jsonb)',
  'record_flow_change_validation_v1(jsonb, uuid, integer, jsonb, jsonb, jsonb, jsonb, text)',
  'save_flow_change_draft_v1(jsonb, uuid, integer, jsonb, jsonb, text, text)',
]), `the role executes: ${JSON.stringify(executable)}`);
for (const signature of WRAPPERS) {
  const row = await one(`select has_function_privilege('anon', $1::regprocedure, 'execute') a,
    has_function_privilege('authenticated', $1::regprocedure, 'execute') u,
    has_function_privilege('service_role', $1::regprocedure, 'execute') s`, [`public.${signature}`]);
  require(!row.a && !row.u && !row.s, `${signature} is executable by an API role`);
}
const acl = (await db.query(ACL_INVENTORY)).rows;
require(acl.length > 0 && acl.every((row) => row.acl_status === 'ok'),
  `acl inventory: ${JSON.stringify(acl.filter((row) => row.acl_status !== 'ok'))}`);
const [fingerprint] = (await db.query(SCHEMA_INVENTORY)).rows.filter((row) => row.version === '20261021000100');
require(fingerprint?.fingerprint_status === 'fingerprint_present', `fingerprint: ${JSON.stringify(fingerprint)}`);
console.log('automation_expert_change_acl=OK');

// La instancia, sus flujos y una v1 del carrito, publicada por el operador.
await db.exec(`
  set session_replication_role = replica;
  insert into public.commercial_ally_runtime_bindings (tenant_ref, funnel_ref, binding_version, status,
    ally_ref, lead_ally_name, lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
    product_name, product_price, currency, offer_code, consent_copy_version, hotmart_product_id,
    chatwoot_account_id, chatwoot_inbox_id, inbound_scope_key, inbound_scope_version)
  values ('lancemos', 'att1', 1, 'active', 'att1', 'Aliada de prueba', 'sitio', 'landing', 'prueba.example',
    '/landing', 'H00000000', 'Producto de prueba', 10, 'MXN', 'OFR11', 'copy-v1', 1011, 2, 11,
    'att1-inbound', 1);
  set session_replication_role = origin;
`);
const cart = (await json('operator_register_flow_v1', INSTANCE, 'att1', 'carrito', 'dan', 'alta')).flow_id;
const definition = (overrides = {}) => ({
  esquema: 'setter-flujo/v1', flujo: 'carrito', producto: 'att1', estado: 'borrador',
  nombre: 'Carrito abandonado', dispara: { evento: 'carrito' },
  entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ plantilla: 'att1_carrito_abandonado_01', idioma: 'es_MX', variables: ['nombre', 'producto'] }],
  termina: { si: ['compra'], vence: '1d' },
  horario: { zona: 'America/Mexico_City', desde: '09:00', hasta: '21:00' },
  topes: { bolsa: 'att1-carrito', por_dia: 80, total: 1000,
    repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  ...overrides,
});
const item = (overrides) => ({ producto: 'att1', flujo: 'carrito', flow_id: cart, tipo: 'version',
  definicion: definition(overrides), en_curso: 'seguir' });
const base = async () => JSON.parse((await one(
  `select public._flow_store_base('lancemos', 'att1', array[$1]::uuid[])::text b`, [cart])).b);
const draftBase = async () => ({ [cart]: { active_version: (await base())[cart].active_version } });
{
  const saved = await json('operator_save_flow_change_draft_v1', INSTANCE, null, null, [item()],
    { [cart]: { active_version: null } }, 'la v1', 'dan');
  const validated = await json('operator_record_flow_change_validation_v1', INSTANCE, saved.proposal_id,
    saved.revision, await base(), { ...VALIDATOR_AE, actor: 'operador' }, CLEAN, {}, 'dan');
  await json('operator_approve_flow_change_v1', INSTANCE, saved.proposal_id, validated.fingerprint, 'dan');
  const published = await json('operator_publish_flow_change_v1', INSTANCE, saved.proposal_id,
    validated.fingerprint, { fingerprint: validated.fingerprint, validator: { ...VALIDATOR_AE, actor: 'operador' },
      result: CLEAN, catalog_sha256: SHA, validated_at: new Date().toISOString() });
  require(published.ok === true, `v1: ${JSON.stringify(published)}`);
}

// 3. El ciclo del agente, como el rol.
// La base la lee el duenio (_flow_store_base no la ejecuta el rol), antes del ciclo del agente.
const store = await asAgent(() => json('read_flow_store_v1', INSTANCE));
require(JSON.stringify(store.products) === '["att1"]' && store.flows.length === 1
  && store.flows[0].active_version === 1 && store.flows[0].versions['1'].version === 1,
  `read_flow_store_v1: ${JSON.stringify(store).slice(0, 400)}`);
const baseNow = await base();
const draftNow = await draftBase();
const agentCycle = async (simulacion) => {
  await db.exec('set role automation_expert_change');
  try {
    const saved = await json('save_flow_change_draft_v1', INSTANCE, null, null,
      [item({ pasos: [{ esperar: '2h' }, ...definition().pasos] })], draftNow,
      'que el carrito espere 2 horas', AE);
    require(saved.ok === true, `agent save: ${JSON.stringify(saved)}`);
    const recorded = await json('record_flow_change_validation_v1', INSTANCE, saved.proposal_id, saved.revision,
      baseNow, VALIDATOR_AE, CLEAN, simulacion === undefined ? {} : { simulacion }, AE);
    require(recorded.status === 'validada', `agent record: ${JSON.stringify(recorded)}`);
    return { id: saved.proposal_id, fingerprint: recorded.fingerprint };
  } finally {
    await db.exec('reset role');
  }
};
const built = await agentCycle({ status: 'built', summary_sha256: 'b'.repeat(64), summary: { cambios: 3 } });
const reread = await asAgent(() => json('get_flow_change_proposal_v1', INSTANCE, built.id));
require(reread.effective_status === 'validada' && reread.requested_by_kind === 'automation_expert'
  && reread.requested_by_ref === AE && reread.events.length === 2
  && reread.validation.preview.simulacion.status === 'built', `get: ${JSON.stringify(reread).slice(0, 400)}`);
await db.exec('set role automation_expert_change');
await denied(`select public.operator_approve_flow_change_v1('{"tenant_ref":"lancemos","ally_ref":"att1"}'::jsonb,
  '${built.id}'::uuid, '${built.fingerprint}', 'dan')`, 'approve as the agent');
await denied(`select public.operator_pause_flow_v1('{"tenant_ref":"lancemos","ally_ref":"att1"}'::jsonb,
  'att1', 'carrito', 1, 'dan', 'x')`, 'pause as the agent');
await denied('select * from public.flow_controls', 'select controls as the agent');
await denied(`insert into public.flow_change_events (tenant_ref, ally_ref, event_type, actor_kind, actor_ref,
  reason_code) values ('lancemos', 'att1', 'flow_change_expired', 'sistema', 'x', 'x')`, 'insert as the agent');
await denied(`select public._flow_change_approve('lancemos', 'att1', '${built.id}'::uuid, '${built.fingerprint}',
  'operador', 'dan')`, 'the private approve as the agent');
await db.exec('reset role');
console.log('automation_expert_change_agent_cycle=OK');

// 4. Aprobar registra la simulacion de la vista previa.
const approvedEvent = async (proposal) => {
  const approved = await json('operator_approve_flow_change_v1', INSTANCE, proposal.id, proposal.fingerprint, 'dan');
  require(approved.ok === true, `approve: ${JSON.stringify(approved)}`);
  const row = await one(`select data::text d from public.flow_change_events
    where proposal_id = $1 and event_type = 'flow_change_approved'`, [proposal.id]);
  return { approved, data: JSON.parse(row.d) };
};
{
  const { approved, data } = await approvedEvent(built);
  require(data.simulation === 'built' && data.summary_sha256 === 'b'.repeat(64)
    && approved.simulation === 'built', `built: ${JSON.stringify(data)}`);
  // Aprobar invalida las otras validadas? No: aprobar no cambia la base. Se descarta para seguir.
  await json('operator_discard_flow_change_v1', INSTANCE, built.id, 'dan', 'prueba');
}
for (const [simulacion, expected] of [
  [{ status: 'failed', reason_code: 'flow_engine_history_too_large' },
    { simulation: 'failed', simulation_reason_code: 'flow_engine_history_too_large' }],
  [undefined, { simulation: 'not_built' }],
  [{ status: 'built' }, { simulation: 'not_built' }],
  [{ status: 'built', summary_sha256: 'NO-ES-HEX' }, { simulation: 'not_built' }],
  [{ status: 'failed', reason_code: 'Con Mayusculas' }, { simulation: 'not_built' }],
]) {
  const proposal = await agentCycle(simulacion);
  const { data } = await approvedEvent(proposal);
  for (const [key, value] of Object.entries(expected)) {
    require(data[key] === value, `${JSON.stringify(simulacion)}: ${JSON.stringify(data)}`);
  }
  require(expected.simulation === 'built' || !('summary_sha256' in data), `sha leaked: ${JSON.stringify(data)}`);
  await json('operator_discard_flow_change_v1', INSTANCE, proposal.id, 'dan', 'prueba');
}
console.log('automation_expert_change_approve_simulation=OK');

// 5. Los topes del agente.
const open = [];
for (let index = 0; index < 10; index += 1) {
  open.push(await asAgent(() => json('save_flow_change_draft_v1', INSTANCE, null, null, [item()], draftNow,
    `borrador ${index}`, AE)));
  require(open.at(-1).ok === true, `open ${index}: ${JSON.stringify(open.at(-1))}`);
}
const eleventh = await asAgent(() => json('save_flow_change_draft_v1', INSTANCE, null, null, [item()], draftNow,
  'el undecimo', AE));
require(eleventh.ok === false && eleventh.reason_code === 'flow_change_open_limit' && eleventh.open === 10,
  `open limit: ${JSON.stringify(eleventh)}`);
const operatorDraft = await json('operator_save_flow_change_draft_v1', INSTANCE, null, null, [item()], draftNow,
  'el operador no tiene tope', 'dan');
require(operatorDraft.ok === true, `operator draft: ${JSON.stringify(operatorDraft)}`);
// Rehacer: con la revision en 100 ya no se rehace.
await db.exec(`set session_replication_role = replica;
  update public.flow_change_proposals set revision = 100 where id = '${open[0].proposal_id}';
  set session_replication_role = origin;`);
const redo = await asAgent(() => json('save_flow_change_draft_v1', INSTANCE, open[0].proposal_id, 100, [item()],
  draftNow, 'rehacer', AE));
require(redo.ok === false && redo.reason_code === 'flow_change_revision_limit', `revision limit: ${JSON.stringify(redo)}`);
// Validar: con 100 validaciones registradas ya no se registra otra.
await db.exec(`insert into public.flow_change_events (tenant_ref, ally_ref, proposal_id, event_type, actor_kind,
  actor_ref, reason_code)
  select 'lancemos', 'att1', '${open[1].proposal_id}', 'flow_change_validation_blocked', 'automation_expert',
         '${AE}', 'flow_change_validation_blocked' from generate_series(1, 100)`);
const hundredFirst = await asAgent(() => json('record_flow_change_validation_v1', INSTANCE, open[1].proposal_id,
  open[1].revision, baseNow, VALIDATOR_AE, CLEAN, {}, AE));
require(hundredFirst.ok === false && hundredFirst.reason_code === 'flow_change_validation_limit',
  `validation limit: ${JSON.stringify(hundredFirst)}`);
// Una vencida o descartada no cuenta: al descartar una, entra otra.
await json('operator_discard_flow_change_v1', INSTANCE, open[2].proposal_id, 'dan', 'libera un lugar');
const again = await asAgent(() => json('save_flow_change_draft_v1', INSTANCE, null, null, [item()], draftNow,
  'entra otra', AE));
require(again.ok === true, `after discard: ${JSON.stringify(again)}`);
console.log('automation_expert_change_limits=OK');

// 6. Como PostgREST: authenticator asume el rol y no puede pasar a postgres.
await db.exec('set session authorization authenticator');
await db.exec('set role automation_expert_change');
const viaRest = await json('read_flow_store_v1', INSTANCE);
require(viaRest.flows.length === 1, 'read as authenticator -> automation_expert_change');
try {
  await db.exec('set role postgres');
  throw new Error('authenticator could set role postgres');
} catch (error) {
  require(error.code === '42501', `set role postgres: ${error.code} ${error.message}`);
}
console.log('automation_expert_change_postgrest=OK');
console.log('validate_automation_expert_change=OK');
