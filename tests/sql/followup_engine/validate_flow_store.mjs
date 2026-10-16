// flow-change-store-v1: el almacen de las definiciones de flujo (1b, pieza 1)
// sobre la cadena completa de migraciones (docs/contracts/flow-change-cycle-v1.md).
//
// Corre como lo haria Supabase: anon, authenticated y service_role (este con
// bypassrls) y los privilegios por defecto de Supabase cargados antes de la
// cadena. Los bindings se siembran con session_replication_role = replica.
//
// Los pasos, en este orden:
//   1. dos instancias y dos productos de la misma aliada: una instancia no ve
//      ni toca lo de otra (es la primera que corre);
//   2. nada de lo que existia cambia: la foto de funciones, disparadores,
//      relaciones, ACL del esquema, roles y membresias antes y despues de la
//      migracion; lo nuevo es exactamente la lista cerrada; la huella del
//      inventario de esquema ausente antes y presente despues; el inventario
//      de permisos todo ok; ningun privilegio de la API sobre las tablas;
//   3. el ciclo entero por las funciones del operador;
//   4. los casos negativos (las guardas con NULL, la base de toda la
//      instancia, la base de quien propone, el texto libre, lo inmutable, el
//      vencimiento, lo que publicar vuelve a mirar, los disparadores);
//   5. el actor: Automation Expert, operador y slack, llamando a los helpers
//      como duenio;
//   6. los casos compartidos con el validador (tests/fixtures/flow_store/
//      invariant_cases.json);
//   7. las definiciones de ATT1 del dato real (att1_definitions_20261016.json),
//      publicadas en una base limpia con el resultado del validador de
//      Python (att1_validation_result_20261016.json);
//   8. los permisos como los usaria PostgREST, y un EXECUTE concedido por
//      error en una segunda base;
//   9. la base de un mundo de prueba (tests/fixtures/flow_store/
//      base_example.json), que pytest usa para probar FlowWorld.base_for. Con
//      FLOW_STORE_FIXTURE_DIR la escribe; sin la variable, exige que la del
//      repo sea identica.
//
// Las horas son siempre relativas al reloj de la base; PGlite tiene
// resolucion de milisegundo y las comparaciones de hora usan margenes.

import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION_FILE = '20261016000100_flow_change_store_v1.sql';
const MIGRATION_VERSION = '20261016000100';
const FIXTURES = join(root, 'tests/fixtures/flow_store');
const BASELINE = join(root, 'supabase/baseline/20260803_public_schema.sql');
const MIGRATIONS = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const POSITION = MIGRATIONS.indexOf(MIGRATION_FILE);
if (POSITION < 0) throw new Error(`${MIGRATION_FILE} is missing from the canonical chain`);
const SCHEMA_INVENTORY = readFileSync(join(root, 'scripts/supabase_schema_inventory.sql'), 'utf8');
const ACL_INVENTORY = readFileSync(join(root, 'scripts/supabase_acl_inventory.sql'), 'utf8');

// Los privilegios por defecto de Supabase: las funciones, las tablas y las
// secuencias nuevas de public quedan concedidas a los tres roles de la API.
const SUPABASE_ROLES = `
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin bypassrls;
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
  alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
  alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
`;

const applyFile = async (target, path) => {
  await target.exec(readFileSync(path, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
};

const newDatabase = async ({ through }) => {
  const target = new PGlite();
  await target.waitReady;
  await target.exec(SUPABASE_ROLES);
  await applyFile(target, BASELINE);
  for (const name of MIGRATIONS.slice(0, through)) {
    await applyFile(target, join(root, 'supabase/migrations', name));
  }
  return target;
};

let checks = 0;
const failures = [];
const check = (condition, label, detail) => {
  checks += 1;
  if (!condition) {
    failures.push(label);
    console.log(`FAIL ${label}${detail === undefined ? '' : ` ${JSON.stringify(detail)}`}`);
  }
};
const sha256 = (text) => createHash('sha256').update(text, 'utf8').digest('hex');
// JSON con las claves ordenadas: jsonb devuelve las claves por largo y bytes.
const canonical = (value) => JSON.stringify(value, (key, inner) => (
  inner && typeof inner === 'object' && !Array.isArray(inner)
    ? Object.fromEntries(Object.entries(inner).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
    : inner));
const sameJson = (left, right) => canonical(left) === canonical(right);
const counts = (values) => values.reduce((acc, value) => ({ ...acc, [value]: (acc[value] ?? 0) + 1 }), {});

// ---------------------------------------------------------------------------
// La foto de lo que existe en public (y de los roles del cluster).
// ---------------------------------------------------------------------------
const PHOTO = `
  select 'fn:' || p.oid::regprocedure::text k,
         md5(pg_get_functiondef(p.oid)) || '|' || coalesce(p.proacl::text, '') v
    from pg_proc p
   where p.pronamespace = 'public'::regnamespace and p.prokind in ('f', 'p')
  union all
  select 'tg:' || c.relname || '.' || t.tgname, md5(pg_get_triggerdef(t.oid)) || '|' || t.tgenabled::text
    from pg_trigger t join pg_class c on c.oid = t.tgrelid
   where c.relnamespace = 'public'::regnamespace and not t.tgisinternal
  union all
  select 'rel:' || c.relname,
         c.relkind::text || '|' || coalesce(c.relacl::text, '') || '|' || c.relrowsecurity || '|' || c.relforcerowsecurity
    from pg_class c where c.relnamespace = 'public'::regnamespace
  union all
  select 'con:' || r.relname || '.' || c.conname,
         md5(pg_get_constraintdef(c.oid)) || '|' || case when c.contype = 'f' then c.confrelid::regclass::text else '' end
    from pg_constraint c join pg_class r on r.oid = c.conrelid
   where r.relnamespace = 'public'::regnamespace
  union all
  select 'pol:' || c.relname || '.' || p.polname,
         md5(coalesce(pg_get_expr(p.polqual, p.polrelid), '') || '|'
             || coalesce(pg_get_expr(p.polwithcheck, p.polrelid), '') || '|' || p.polcmd::text || '|' || p.polroles::text)
    from pg_policy p join pg_class c on c.oid = p.polrelid
   where c.relnamespace = 'public'::regnamespace
  union all
  select 'nsp:' || n.nspname, n.nspowner::regrole::text || '|' || coalesce(n.nspacl::text, '')
    from pg_namespace n
  union all
  select 'ext:' || e.extname, e.extversion from pg_extension e
  union all
  select 'role:' || r.rolname,
         concat_ws('|', r.rolsuper, r.rolinherit, r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
                   r.rolreplication, r.rolbypassrls, r.rolconnlimit, r.rolvaliduntil)
    from pg_roles r
  union all
  select 'member:' || m.roleid::regrole::text || '>' || m.member::regrole::text, m.admin_option::text
    from pg_auth_members m
  union all
  select 'setting:' || s.setdatabase || ':' || s.setrole, array_to_string(s.setconfig, ';')
    from pg_db_role_setting s
  union all
  select 'defacl:' || d.defaclrole::regrole::text || ':' || coalesce(d.defaclnamespace::regnamespace::text, '')
         || ':' || d.defaclobjtype::text, d.defaclacl::text
    from pg_default_acl d`;
const photo = async (target) => new Map((await target.query(PHOTO)).rows.map((row) => [row.k, row.v]));

// La lista cerrada de lo que agrega la migracion (diseno, secciones 2 y 3).
const NEW_FUNCTIONS = [
  '_flow_store_actor_is_valid(text,text)',
  '_flow_store_free_text_is_valid(text)',
  'reject_flow_store_mutation()',
  'validate_flow_control_change()',
  'record_flow_control_change()',
  'validate_flow_definition_insert()',
  'validate_flow_change_proposal_change()',
  '_flow_store_instance(jsonb)',
  '_flow_store_product_is_bound(text,text,text)',
  '_flow_store_base(text,text,uuid[])',
  '_flow_change_item_flow_ids(jsonb)',
  '_flow_change_items_are_valid(jsonb)',
  '_flow_change_findings_are_valid(jsonb)',
  '_flow_change_result_is_valid(jsonb)',
  '_flow_change_validator_is_valid(jsonb)',
  '_flow_change_fingerprint(uuid,integer,jsonb,jsonb,jsonb,text)',
  '_flow_change_event(flow_change_proposals,text,text,text,text,jsonb)',
  '_flow_change_expire_if_due(flow_change_proposals)',
  '_flow_change_save_draft(text,text,uuid,integer,jsonb,jsonb,text,text,text)',
  '_flow_change_record_validation(text,text,uuid,integer,jsonb,jsonb,jsonb,jsonb,text,text)',
  '_flow_change_approve(text,text,uuid,text,text,text)',
  '_flow_change_publish(text,text,uuid,text,jsonb)',
  '_flow_change_pause(text,text,text,text,bigint,text,text,text)',
  '_flow_change_discard(text,text,uuid,text,text,text)',
  'operator_register_flow_v1(jsonb,text,text,text,text)',
  'operator_save_flow_change_draft_v1(jsonb,uuid,integer,jsonb,jsonb,text,text)',
  'operator_record_flow_change_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,jsonb,text)',
  'operator_approve_flow_change_v1(jsonb,uuid,text,text)',
  'operator_publish_flow_change_v1(jsonb,uuid,text,jsonb)',
  'operator_pause_flow_v1(jsonb,text,text,bigint,text,text)',
  'operator_discard_flow_change_v1(jsonb,uuid,text,text)',
];
const OPERATOR_FUNCTIONS = NEW_FUNCTIONS.filter((signature) => signature.startsWith('operator_'));
const NEW_TRIGGERS = [
  'flow_controls.flow_controls_validate',
  'flow_controls.flow_controls_record_change',
  'flow_controls.flow_controls_permanent',
  'flow_controls.flow_controls_no_truncate',
  'flow_controls.flow_controls_set_updated_at',
  'flow_definitions.flow_definitions_validate',
  'flow_definitions.flow_definitions_immutable',
  'flow_definitions.flow_definitions_no_truncate',
  'flow_change_proposals.flow_change_proposals_validate',
  'flow_change_proposals.flow_change_proposals_permanent',
  'flow_change_proposals.flow_change_proposals_no_truncate',
  'flow_change_proposals.flow_change_proposals_set_updated_at',
  'flow_change_events.flow_change_events_append_only',
  'flow_change_events.flow_change_events_no_truncate',
];
const NEW_TABLES = ['flow_change_proposals', 'flow_controls', 'flow_definitions', 'flow_change_events'];
const NEW_RELATIONS = [
  ...NEW_TABLES,
  'flow_change_proposals_pkey', 'flow_change_proposals_instance_time_idx', 'flow_change_proposals_open_idx',
  'flow_controls_pkey', 'flow_controls_identity', 'flow_controls_instance_idx',
  'flow_definitions_pkey', 'flow_definitions_proposal_idx',
  'flow_change_events_pkey', 'flow_change_events_proposal_time_idx', 'flow_change_events_flow_time_idx',
  'flow_change_events_instance_time_idx',
];

// ---------------------------------------------------------------------------
// La base: la cadena hasta la migracion anterior, la foto, la migracion y la
// foto otra vez.
// ---------------------------------------------------------------------------
const db = await newDatabase({ through: POSITION });
const fingerprintOf = async (target) => (await target.query(SCHEMA_INVENTORY)).rows;
const inventoryBefore = await fingerprintOf(db);
const aclBefore = (await db.query(ACL_INVENTORY)).rows;
const photoBefore = await photo(db);
await applyFile(db, join(root, 'supabase/migrations', MIGRATION_FILE));
const photoAfter = await photo(db);
const inventoryAfter = await fingerprintOf(db);
const aclAfter = (await db.query(ACL_INVENTORY)).rows;
for (const name of MIGRATIONS.slice(POSITION + 1)) {
  await applyFile(db, join(root, 'supabase/migrations', name));
}

const query = async (sql, params, target = db) => (await target.query(sql, params)).rows;
const one = async (sql, params, target = db) => (await target.query(sql, params)).rows[0];
const attempt = async (sql, params, target = db) => {
  try {
    await target.query(sql, params);
    return null;
  } catch (error) {
    return { code: error.code, message: error.message };
  }
};
const asRole = async (role, fn, target = db) => {
  await target.exec(`set role ${role}`);
  try {
    return await fn();
  } finally {
    await target.exec('reset role');
  }
};
const replica = async (sql, params) => {
  await db.exec('set session_replication_role = replica');
  try {
    return await db.query(sql, params);
  } finally {
    await db.exec('set session_replication_role = origin');
  }
};

// ---------------------------------------------------------------------------
// Los bindings: ATT1 con dos productos (att1 y att2), otra instancia, y las
// del mundo de prueba del paso 9.
// ---------------------------------------------------------------------------
const BINDINGS = [
  ['lancemos', 'att1', 'att1', 11],
  ['lancemos', 'att2', 'att1', 12],
  ['lancemos', 'otra', 'otra', 13],
  ['lancemos', 'mundo', 'mundo', 21],
  ['lancemos', 'mundo-dos', 'mundo', 22],
  ['lancemos', 'vecina', 'vecina', 23],
];
const seedBinding = async (target, [tenant, funnel, ally, inbox]) => {
  await target.exec('set session_replication_role = replica');
  try {
    await target.query(`
      insert into public.commercial_ally_runtime_bindings (tenant_ref, funnel_ref, binding_version, status,
        ally_ref, lead_ally_name, lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
        product_name, product_price, currency, offer_code, consent_copy_version, hotmart_product_id,
        chatwoot_account_id, chatwoot_inbox_id, inbound_scope_key, inbound_scope_version)
      values ($1, $2, 1, 'active', $3, 'Aliada de prueba', 'sitio', 'landing', 'prueba.example', '/landing',
        'H00000000', 'Producto de prueba', 10, 'MXN', 'OFR' || $4, 'copy-v1', 1000 + $4::int, 2, $4::int,
        $2 || '-inbound', 1)`, [tenant, funnel, ally, inbox]);
  } finally {
    await target.exec('set session_replication_role = origin');
  }
};
for (const binding of BINDINGS) await seedBinding(db, binding);
const setBindingStatus = async (funnel, status) => replica(
  `update public.commercial_ally_runtime_bindings set status = $2 where tenant_ref = 'lancemos' and funnel_ref = $1`,
  [funnel, status],
);

// ---------------------------------------------------------------------------
// Las llamadas.
// ---------------------------------------------------------------------------
const ATT1 = { tenant_ref: 'lancemos', ally_ref: 'att1', funnel_ref: 'att1', chatwoot_inbox_id: 11 };
const OTRA = { tenant_ref: 'lancemos', ally_ref: 'otra' };
const MUNDO = { tenant_ref: 'lancemos', ally_ref: 'mundo' };
const VECINA = { tenant_ref: 'lancemos', ally_ref: 'vecina' };
const VALIDATOR = { contract: 'setter-flujo/v1', rules_version: 1, product_version: 'v1.9.0', git_sha: 'abcdef0' };
const CLEAN = { blocks: [], warnings: [] };
const PURPOSE = 'que el carrito salga a las 2 horas';
const json = (value) => (value === null || value === undefined ? null : JSON.stringify(value));

const register = async (instance, product, flowKey, target = db) => (await one(
  `select public.operator_register_flow_v1($1::jsonb, $2, $3, 'dan', 'alta del flujo') r`,
  [json(instance), product, flowKey], target)).r;
const draftBase = async (flowIds) => (await one(`
  select coalesce(jsonb_object_agg(flow_id::text, jsonb_build_object('active_version', active_version)), '{}'::jsonb) b
    from public.flow_controls where flow_id = any($1::uuid[])`, [flowIds])).b;
const instanceBase = async (instance, flowIds) => (await one(
  'select public._flow_store_base($1, $2, $3::uuid[]) b',
  [instance.tenant_ref, instance.ally_ref, flowIds])).b;
const saveDraft = async (instance, items, base, { proposalId = null, revision = null, purpose = PURPOSE } = {}) => (await one(
  `select public.operator_save_flow_change_draft_v1($1::jsonb, $2::uuid, $3::int, $4::jsonb, $5::jsonb, $6, 'dan') r`,
  [json(instance), proposalId, revision, json(items), json(base), purpose])).r;
const SAVE_SQL = `select public.operator_save_flow_change_draft_v1($1::jsonb, $2::uuid, $3::int, $4::jsonb, $5::jsonb, $6, 'dan')`;
const RECORD_SQL = `select public.operator_record_flow_change_validation_v1($1::jsonb, $2::uuid, $3::int, $4::jsonb, $5::jsonb, $6::jsonb, $7::jsonb, 'dan') r`;
const record = async (instance, proposalId, revision, validatedBase,
  { result = CLEAN, validator = VALIDATOR, preview = {} } = {}) => (await one(
  RECORD_SQL, [json(instance), proposalId, revision, json(validatedBase), json(validator), json(result), json(preview)])).r;
const approve = async (instance, proposalId, fingerprint) => (await one(
  `select public.operator_approve_flow_change_v1($1::jsonb, $2::uuid, $3, 'dan') r`,
  [json(instance), proposalId, fingerprint])).r;
const revalidation = (fingerprint, extra = {}) => ({
  fingerprint,
  validator: VALIDATOR,
  result: CLEAN,
  catalog_sha256: 'a'.repeat(64),
  validated_at: new Date().toISOString(),
  ...extra,
});
const PUBLISH_SQL = `select public.operator_publish_flow_change_v1($1::jsonb, $2::uuid, $3, $4::jsonb) r`;
const publish = async (instance, proposalId, fingerprint, reval) => (await one(
  PUBLISH_SQL, [json(instance), proposalId, fingerprint, json(reval)])).r;
const pause = async (instance, product, flowKey, generation, reason = 'pausa de prueba') => (await one(
  `select public.operator_pause_flow_v1($1::jsonb, $2, $3, $4::bigint, 'dan', $5) r`,
  [json(instance), product, flowKey, generation, reason])).r;
const discard = async (instance, proposalId, reason = 'no va') => (await one(
  `select public.operator_discard_flow_change_v1($1::jsonb, $2::uuid, 'dan', $3) r`,
  [json(instance), proposalId, reason])).r;
const control = async (flowId) => one(
  `select state, active_version, generation::int generation, change_kind, changed_by_kind, changed_by_ref
     from public.flow_controls where flow_id = $1`, [flowId]);
const proposal = async (proposalId) => one(
  `select status, revision, validation, base, fingerprint, closed_by_kind, closed_by_ref, approved_by_kind
     from public.flow_change_proposals where id = $1`, [proposalId]);
const eventsOf = async (proposalId) => query(
  `select event_type, reason_code, actor_kind, actor_ref, flow_id, generation::int generation, data
     from public.flow_change_events where proposal_id = $1`, [proposalId]);
const countRows = async (table, where = 'true', params = []) => (await one(
  `select count(*)::int n from public.${table} where ${where}`, params)).n;

// Las definiciones de prueba: el bloque del carrito del esquema
// (flow-definition-v1.md, bloque 1), completo y normalizado, como borrador.
const definition = (producto, flujo, overrides = {}) => ({
  esquema: 'setter-flujo/v1',
  flujo,
  producto,
  estado: 'borrador',
  nombre: 'Carrito abandonado',
  dispara: { evento: 'carrito' },
  entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ plantilla: 'att1_carrito_abandonado_01', idioma: 'es_MX', variables: ['nombre', 'producto'] }],
  termina: { si: ['compra'], vence: '1d' },
  horario: { zona: 'America/Mexico_City', desde: '09:00', hasta: '21:00' },
  topes: {
    bolsa: `${producto}-${flujo}`,
    por_dia: 80,
    total: 1000,
    repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' },
  },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  ...overrides,
});
const versionItem = (flowId, producto, flujo, overrides = {}) => ({
  producto, flujo, flow_id: flowId, tipo: 'version', definicion: definition(producto, flujo, overrides), en_curso: 'seguir',
});
const stateItem = (flowId, producto, flujo, tipo) => ({ producto, flujo, flow_id: flowId, tipo });
const backItem = (flowId, producto, flujo, version) => ({
  producto, flujo, flow_id: flowId, tipo: 'volver', version, en_curso: 'seguir',
});

// Guarda, valida, aprueba y publica, con la base que leeria quien propone y la
// que leeria el validador.
const prepare = async (instance, items, { approveIt = true } = {}) => {
  const flowIds = [...new Set(items.map((item) => item.flow_id))];
  const saved = await saveDraft(instance, items, await draftBase(flowIds));
  if (!saved?.ok) return { saved };
  const validated = await record(instance, saved.proposal_id, saved.revision, await instanceBase(instance, flowIds));
  const approved = approveIt && validated?.status === 'validada'
    ? await approve(instance, saved.proposal_id, validated.fingerprint)
    : null;
  return { id: saved.proposal_id, saved, validated, approved, fingerprint: validated?.fingerprint };
};
const cycle = async (instance, items, label) => {
  let prepared = {};
  let published = null;
  try {
    prepared = await prepare(instance, items);
    published = prepared.fingerprint
      ? await publish(instance, prepared.id, prepared.fingerprint, revalidation(prepared.fingerprint))
      : null;
  } catch (caught) {
    published = { error: { code: caught.code, message: caught.message } };
  }
  check(
    prepared.saved?.ok && prepared.validated?.status === 'validada' && prepared.approved?.ok
      && published?.ok && published.replayed === false,
    `ciclo: ${label}`,
    { ...prepared, published },
  );
  return { ...prepared, published };
};

// ===========================================================================
// 1. Dos instancias y dos productos de la misma aliada.
// ===========================================================================
const carrito = (await register(ATT1, 'att1', 'carrito')).flow_id;
const pagoFallido = (await register(ATT1, 'att1', 'pago_fallido')).flow_id;
const carritoAtt2 = (await register(ATT1, 'att2', 'carrito')).flow_id;
const carritoOtra = (await register(OTRA, 'otra', 'carrito')).flow_id;
check(Boolean(carrito && pagoFallido && carritoAtt2 && carritoOtra), '1: cuatro flujos, dos productos en ATT1');
let again = await register(ATT1, 'att1', 'carrito');
check(again.ok === false && again.reason_code === 'flow_already_registered' && again.flow_id === carrito,
  '1: registrar dos veces da flow_already_registered con el mismo flow_id', again);
let error = await attempt(`select public.operator_register_flow_v1($1::jsonb, 'otra', 'carrito', 'dan', 'alta')`, [json(ATT1)]);
check(error?.code === '22023' && error.message === 'flow_store_product_not_bound',
  '1: registrar en ATT1 un flujo del producto otra da flow_store_product_not_bound', error);
error = await attempt(`select public.operator_register_flow_v1($1::jsonb, 'att1', 'carrito', 'dan', 'alta')`, [json(OTRA)]);
check(error?.message === 'flow_store_product_not_bound', '1: la otra instancia no registra en un producto de ATT1', error);
check(await countRows('flow_change_events', `event_type = 'flow_control_changed' and reason_code = 'registrado'`) === 4,
  '1: el disparador deja un evento por cada alta');

const att1Base = await instanceBase(ATT1, [carrito]);
check(carrito in att1Base && pagoFallido in att1Base && carritoAtt2 in att1Base && !(carritoOtra in att1Base),
  '1: la base de ATT1 tiene los flujos de att1 y de att2, y no el de otra', att1Base);
check(sameJson(att1Base[carrito], { active_version: null, generation: 0 })
    && sameJson(att1Base[carritoAtt2], { active_version: null }),
  '1: la base lleva la generacion solo de los flujos de la propuesta', att1Base);
check(sameJson(await instanceBase(OTRA, []), { [carritoOtra]: { active_version: null } }),
  '1: la base de la otra instancia tiene solo su flujo');

const isolated = await saveDraft(ATT1, [versionItem(carrito, 'att1', 'carrito', { nombre: 'Aislada' })],
  await draftBase([carrito]));
check(isolated.ok && isolated.status === 'borrador' && isolated.revision === 1, '1: un borrador de ATT1', isolated);
for (const [label, sql, params] of [
  ['guardar un borrador sobre un flujo de ATT1', SAVE_SQL,
    [json(OTRA), null, null, json([versionItem(carrito, 'att1', 'carrito')]), json(await draftBase([carrito])), PURPOSE]],
  ['rehacer el borrador de ATT1', SAVE_SQL,
    [json(OTRA), isolated.proposal_id, 1, json([versionItem(carrito, 'att1', 'carrito')]), json(await draftBase([carrito])), PURPOSE]],
  ['validar', RECORD_SQL,
    [json(OTRA), isolated.proposal_id, 1, json(await instanceBase(ATT1, [carrito])), json(VALIDATOR), json(CLEAN), '{}']],
  ['aprobar', `select public.operator_approve_flow_change_v1($1::jsonb, $2::uuid, $3, 'dan')`,
    [json(OTRA), isolated.proposal_id, 'a'.repeat(64)]],
  ['publicar', PUBLISH_SQL, [json(OTRA), isolated.proposal_id, 'a'.repeat(64), json(revalidation('a'.repeat(64)))]],
  ['descartar', `select public.operator_discard_flow_change_v1($1::jsonb, $2::uuid, 'dan', 'no va')`,
    [json(OTRA), isolated.proposal_id]],
  ['pausar', `select public.operator_pause_flow_v1($1::jsonb, 'att1', 'carrito', 0, 'dan', 'pausa')`, [json(OTRA)]],
]) {
  error = await attempt(sql, params);
  check(error?.code === 'P0002', `1: la otra instancia no puede ${label} de ATT1: P0002`, error);
}
check((await proposal(isolated.proposal_id)).status === 'borrador', '1: el borrador de ATT1 sigue intacto');
check(await countRows('flow_change_proposals', 'tenant_ref = $1 and ally_ref = $2', ['lancemos', 'otra']) === 0,
  '1: la otra instancia no guardo nada');

// Un flujo de otra instancia con la misma clave no se filtra: si un producto
// pasa de una aliada a otra, registrar devuelve flow_already_registered sin el
// flow_id ajeno.
await setBindingStatus('att2', 'retired');
await replica(`
  insert into public.commercial_ally_runtime_bindings (tenant_ref, funnel_ref, binding_version, status, ally_ref,
    lead_ally_name, lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink, product_name,
    product_price, currency, offer_code, consent_copy_version, hotmart_product_id, chatwoot_account_id,
    chatwoot_inbox_id, inbound_scope_key, inbound_scope_version)
  values ('lancemos', 'att2', 2, 'active', 'otra', 'Aliada de prueba', 'sitio', 'landing', 'prueba.example',
    '/landing', 'H00000000', 'Producto de prueba', 10, 'MXN', 'OFR99', 'copy-v1', 1099, 2, 99, 'att2-inbound', 1)`);
again = await register(OTRA, 'att2', 'carrito');
check(again.ok === false && again.reason_code === 'flow_already_registered' && again.flow_id === null,
  '1: registrar una clave de otra instancia no devuelve su flow_id', again);
await replica(`delete from public.commercial_ally_runtime_bindings where tenant_ref = 'lancemos' and funnel_ref = 'att2' and ally_ref = 'otra'`);
await setBindingStatus('att2', 'active');

// La instancia: un objeto con tenant_ref y ally_ref de texto, y al menos un
// binding activo.
for (const [label, instance, message] of [
  ['NULL', null, 'flow_store_instance_invalid'],
  ['una lista', [], 'flow_store_instance_invalid'],
  ['un tenant que no es texto', { tenant_ref: 5, ally_ref: 'att1' }, 'flow_store_instance_invalid'],
  ['sin ally_ref', { tenant_ref: 'lancemos' }, 'flow_store_instance_invalid'],
  ['con mayusculas', { tenant_ref: 'Lancemos', ally_ref: 'att1' }, 'flow_store_instance_invalid'],
  ['sin binding activo', { tenant_ref: 'lancemos', ally_ref: 'nadie' }, 'flow_store_instance_not_bound'],
]) {
  error = await attempt(`select public.operator_pause_flow_v1($1::jsonb, 'att1', 'carrito', 0, 'dan', 'pausa')`,
    [json(instance)]);
  check(error?.code === '22023' && error.message === message, `1: la instancia ${label}: ${message}`, error);
}

// ===========================================================================
// 2. Nada de lo que existia cambia, y lo nuevo es la lista cerrada.
// ===========================================================================
const changed = [...photoBefore].filter(([key, value]) => photoAfter.get(key) !== value).map(([key]) => key);
check(changed.length === 0, '2: nada de lo que existia cambia (funciones, disparadores, relaciones, ACL, roles, membresias)', changed);
const added = [...photoAfter.keys()].filter((key) => !photoBefore.has(key));
const addedOf = (prefix) => added.filter((key) => key.startsWith(prefix)).map((key) => key.slice(prefix.length)).sort();
check(sameJson(addedOf('fn:'), [...NEW_FUNCTIONS].sort()), '2: las funciones nuevas son exactamente las 31', addedOf('fn:'));
check(sameJson(addedOf('tg:'), [...NEW_TRIGGERS].sort()), '2: los disparadores nuevos son exactamente los 14', addedOf('tg:'));
check(sameJson(addedOf('rel:'), [...NEW_RELATIONS].sort()), '2: las relaciones nuevas son las cuatro tablas y sus indices', addedOf('rel:'));
for (const prefix of ['pol:', 'nsp:', 'ext:', 'role:', 'member:', 'setting:', 'defacl:']) {
  check(addedOf(prefix).length === 0, `2: no agrega nada en ${prefix}`, addedOf(prefix));
}
const newConstraints = addedOf('con:');
check(newConstraints.length > 0 && newConstraints.every((key) => NEW_TABLES.includes(key.split('.')[0])),
  '2: las restricciones nuevas son de las cuatro tablas', newConstraints);
check(newConstraints.every((key) => {
  const referenced = photoAfter.get(`con:${key}`).split('|')[1];
  return referenced === '' || NEW_TABLES.includes(referenced.replace(/^public\./, ''));
}), '2: ninguna clave foranea apunta a una tabla que existia');
check(Object.keys(counts(added.map((key) => key.split(':')[0]))).every((kind) => ['fn', 'tg', 'rel', 'con'].includes(kind)),
  '2: no hay otra clase de objeto nuevo', counts(added.map((key) => key.split(':')[0])));

const rowOf = (rows) => rows.find((row) => row.version === MIGRATION_VERSION);
check(rowOf(inventoryBefore)?.fingerprint_status === 'fingerprint_absent', '2: la huella da fingerprint_absent antes de aplicar', rowOf(inventoryBefore));
check(rowOf(inventoryAfter)?.fingerprint_status === 'fingerprint_present' && rowOf(inventoryAfter)?.total_markers === 5,
  '2: la huella da fingerprint_present (5 de 5) despues', rowOf(inventoryAfter));
check(inventoryBefore.filter((row) => row.version !== MIGRATION_VERSION).every((row) => row.fingerprint_status === 'fingerprint_present'),
  '2: antes de aplicar, las demas huellas estan presentes');
check(inventoryAfter.every((row) => row.fingerprint_status === 'fingerprint_present'), '2: despues, todas las huellas estan presentes',
  inventoryAfter.filter((row) => row.fingerprint_status !== 'fingerprint_present').map((row) => row.version));
check(aclAfter.length === aclBefore.length + 31 && aclAfter.every((row) => row.acl_status === 'ok'),
  `2: el inventario de permisos: ${aclAfter.length} funciones, todas ok (31 nuevas)`,
  { before: aclBefore.length, after: aclAfter.length, bad: aclAfter.filter((row) => row.acl_status !== 'ok') });
const tablePrivileges = await one(`
  select count(*) filter (where has_table_privilege(r, c.oid, p))::int n, count(*)::int total
    from unnest(array['anon', 'authenticated', 'service_role']) r,
         unnest(array['select', 'insert', 'update', 'delete', 'truncate', 'references', 'trigger']) p,
         pg_class c
   where c.oid = any (array['public.flow_controls'::regclass, 'public.flow_definitions'::regclass,
                            'public.flow_change_proposals'::regclass, 'public.flow_change_events'::regclass])`);
check(tablePrivileges.n === 0 && tablePrivileges.total === 84,
  '2: ningun rol de la API tiene ninguno de los siete privilegios sobre las cuatro tablas', tablePrivileges);
const functionPrivileges = await one(`
  select count(*) filter (where has_function_privilege(r, p.oid, 'execute'))::int n, count(*)::int total
    from unnest(array['anon', 'authenticated', 'service_role']) r, pg_proc p
   where p.pronamespace = 'public'::regnamespace and p.oid::regprocedure::text = any ($1::text[])`, [NEW_FUNCTIONS]);
check(functionPrivileges.n === 0 && functionPrivileges.total === 93,
  '2: ningun rol de la API ejecuta ninguna de las 31 funciones nuevas', functionPrivileges);
const definer = await one(`
  select count(*) filter (where p.prosecdef)::int definer,
         count(*) filter (where not (p.proconfig @> array['search_path=pg_catalog, public, pg_temp']
                                     or p.proconfig @> array['search_path=pg_catalog, pg_temp']))::int loose
    from pg_proc p
   where p.pronamespace = 'public'::regnamespace and p.oid::regprocedure::text = any ($1::text[])`, [NEW_FUNCTIONS]);
check(definer.definer === 0 && definer.loose === 0, '2: las 31 son security invoker con el search_path fijo', definer);
const lockTimeouts = (await query(`
  select p.proname from pg_proc p
   where p.pronamespace = 'public'::regnamespace and p.proconfig @> array['lock_timeout=5s']
   order by 1`)).map((row) => row.proname);
check(sameJson(lockTimeouts, [
  '_flow_change_approve', '_flow_change_pause', '_flow_change_publish', '_flow_change_record_validation',
  'operator_approve_flow_change_v1', 'operator_pause_flow_v1', 'operator_publish_flow_change_v1',
  'operator_record_flow_change_validation_v1',
]), '2: lock_timeout de 5 s en las que esperan locks', lockTimeouts);

// ===========================================================================
// 3. El ciclo entero por las funciones del operador.
// ===========================================================================
const expectControl = async (flowId, expected, label) => {
  const row = await control(flowId);
  check(row.state === expected.state && row.active_version === expected.active_version
      && row.generation === expected.generation, label, { row, expected });
  return row;
};
const expectCycleEvents = async (prepared, flowCount, label) => {
  const events = await eventsOf(prepared.id);
  const types = counts(events.map((event) => event.event_type));
  check(sameJson(types, {
    flow_change_draft_saved: 1, flow_change_validated: 1, flow_change_approved: 1,
    flow_control_changed: flowCount, flow_change_published: 1,
  }), `${label}: sus eventos`, types);
  check((await proposal(prepared.id)).status === 'publicada', `${label}: la propuesta queda publicada`);
  const approved = events.find((event) => event.event_type === 'flow_change_approved');
  check(approved?.data?.simulation === 'not_built' && approved.data.fingerprint === prepared.fingerprint,
    `${label}: aprobar registra simulation not_built`, approved?.data);
  return events;
};

await expectControl(carrito, { state: 'apagado', active_version: null, generation: 0 }, '3: registrado: apagado, sin version, generacion 0');
let run = await cycle(ATT1, [versionItem(carrito, 'att1', 'carrito')], '3: la v1 del carrito, primera version del operador');
await expectCycleEvents(run, 1, '3: la v1');
check(sameJson(run.published.published, [{ state: 'apagado', flujo: 'carrito', version: 1, producto: 'att1', generation: 1 }]),
  '3: publicar devuelve lo publicado', run.published);
await expectControl(carrito, { state: 'apagado', active_version: 1, generation: 1 }, '3: publicar no toca el estado: apagado con la v1');
let controlEvent = (await eventsOf(run.id)).find((event) => event.event_type === 'flow_control_changed');
check(controlEvent?.reason_code === 'publicado' && controlEvent.generation === 1 && controlEvent.flow_id === carrito
    && controlEvent.data.from_version === null && controlEvent.data.to_version === 1
    && controlEvent.data.change_reason_sha256 === sha256(PURPOSE) && controlEvent.actor_ref === 'dan',
  '3: el disparador deja el cambio del control con la generacion, las versiones y el sha256 del para que', controlEvent);
const v1 = await one(`select definition, definition_sha256, approved_by_kind, approved_by_ref from public.flow_definitions where flow_id = $1 and version = 1`, [carrito]);
check(v1.definition.version === 1 && v1.definition.estado === 'publicada' && v1.approved_by_kind === 'operador'
    && v1.approved_by_ref === 'dan', '3: la v1 guardada con version y estado: publicada', v1);
const v1Text = (await one(`select definition::text t from public.flow_definitions where flow_id = $1 and version = 1`, [carrito])).t;
check(v1.definition_sha256 === sha256(v1Text), '3: definition_sha256 es el de la definicion guardada');
const published = (await eventsOf(run.id)).find((event) => event.event_type === 'flow_change_published');
check(sameJson(published?.data?.flows, [{
  flow_id: carrito, en_curso: 'seguir', to_state: 'apagado', from_state: 'apagado', to_version: 1, from_version: null,
}]) && published.data.revalidation.catalog_sha256 === 'a'.repeat(64), '3: flow_change_published lleva los flujos y la revalidacion', published?.data);

run = await cycle(ATT1, [stateItem(carrito, 'att1', 'carrito', 'prender')], '3: prender el carrito');
await expectCycleEvents(run, 1, '3: prender');
await expectControl(carrito, { state: 'prendido', active_version: 1, generation: 2 }, '3: prendido con la v1');
run = await cycle(ATT1, [versionItem(carrito, 'att1', 'carrito', {
  topes: { bolsa: 'att1-carrito', por_dia: 90, total: 1000, repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
})], '3: la v2 sobre la v1 con el flujo prendido');
await expectCycleEvents(run, 1, '3: la v2');
await expectControl(carrito, { state: 'prendido', active_version: 2, generation: 3 }, '3: prendido -> prendido no es una transicion: sigue prendido con la v2');
run = await cycle(ATT1, [backItem(carrito, 'att1', 'carrito', 1)], '3: volver a la v1');
await expectCycleEvents(run, 1, '3: volver');
await expectControl(carrito, { state: 'prendido', active_version: 1, generation: 4 }, '3: la activa es la v1 otra vez');
check(await countRows('flow_definitions', 'flow_id = $1', [carrito]) === 2, '3: volver no escribe una version nueva');

let paused = await pause(ATT1, 'att1', 'carrito', 4);
check(paused.ok && paused.changed && paused.generation === 5, '3: pausar', paused);
await expectControl(carrito, { state: 'pausado', active_version: 1, generation: 5 }, '3: pausado con la v1');
const pauseEvent = (await query(`select reason_code, actor_kind, proposal_id, data from public.flow_change_events
  where flow_id = $1 and generation = 5`, [carrito]))[0];
check(pauseEvent?.reason_code === 'pausado' && pauseEvent.proposal_id === null && pauseEvent.data.from_state === 'prendido'
    && pauseEvent.data.to_state === 'pausado', '3: la pausa deja su evento, sin propuesta', pauseEvent);
paused = await pause(ATT1, 'att1', 'carrito', 4);
check(paused.ok === false && paused.reason_code === 'flow_control_generation_mismatch' && paused.generation === 5,
  '3: pausar con la generacion vieja: flow_control_generation_mismatch', paused);
paused = await pause(ATT1, 'att1', 'carrito', 5);
check(paused.ok === true && paused.changed === false, '3: pausar algo pausado no cambia nada', paused);
run = await cycle(ATT1, [stateItem(carrito, 'att1', 'carrito', 'prender')], '3: sacar la pausa con prender');
await expectCycleEvents(run, 1, '3: sacar la pausa');
await expectControl(carrito, { state: 'prendido', active_version: 1, generation: 6 }, '3: prendido otra vez');

// ===========================================================================
// 4. Los casos negativos.
// ===========================================================================
// 4a. Las guardas con NULL al validar: la foto contra la que corrio el
// validador tiene que ser exactamente la de hoy.
let draft = await saveDraft(ATT1, [versionItem(carrito, 'att1', 'carrito', { nombre: 'Guardas' })], await draftBase([carrito]));
let good = await instanceBase(ATT1, [carrito]);
for (const [label, base] of [
  ['SQL NULL', null],
  ['{}', {}],
  ['sin un flujo de la instancia', { [carrito]: good[carrito] }],
  ['con un flujo de mas', { ...good, '00000000-0000-4000-8000-000000000000': { active_version: null } }],
  ['con una generacion nula', { ...good, [carrito]: { active_version: good[carrito].active_version, generation: null } }],
  ['sin la generacion del flujo de la propuesta', { ...good, [carrito]: { active_version: good[carrito].active_version } }],
  ['con la generacion de otro flujo', { ...good, [pagoFallido]: { ...good[pagoFallido], generation: 0 } }],
]) {
  const result = await record(ATT1, draft.proposal_id, draft.revision, base);
  check(result.ok === false && result.reason_code === 'flow_change_stale_base' && sameJson(result.base, good),
    `4a: validar con la base ${label}: stale_base`, result);
}
check(await countRows('flow_change_events', `proposal_id = $1 and event_type = 'flow_change_validation_rejected'`, [draft.proposal_id]) === 7,
  '4a: cada validacion rechazada deja su evento');
check((await proposal(draft.proposal_id)).status === 'borrador', '4a: la propuesta sigue en borrador');

// 4b. El resultado, el validador y la vista previa, con claves y tipos exactos.
const recordError = async (overrides) => attempt(RECORD_SQL, [json(ATT1), draft.proposal_id, draft.revision, json(good),
  json('validator' in overrides ? overrides.validator : VALIDATOR),
  json('result' in overrides ? overrides.result : CLEAN),
  json('preview' in overrides ? overrides.preview : {})]);
const finding = (extra = {}) => ({ rule: 1, code: 'unknown_key', kind: 'forma', path: '/topes/max', ...extra });
for (const [label, result] of [
  ['{}', {}],
  ['una clave de mas', { blocks: [], warnings: [], x: 1 }],
  ['blocks que no es lista', { blocks: {}, warnings: [] }],
  ['un hallazgo sin path', { blocks: [{ rule: 1, code: 'unknown_key', kind: 'forma' }], warnings: [] }],
  ['la regla 18', { blocks: [finding({ rule: 18 })], warnings: [] }],
  ['la regla como texto', { blocks: [finding({ rule: '1' })], warnings: [] }],
  ['un kind que no existe', { blocks: [finding({ kind: 'grave' })], warnings: [] }],
  ['un codigo con mayusculas', { blocks: [finding({ code: 'Unknown_key' })], warnings: [] }],
  ['un path que no es JSON Pointer', { blocks: [finding({ path: 'topes/max' })], warnings: [] }],
  ['un path con un escape invalido', { blocks: [finding({ path: '/topes/a~2b' })], warnings: [] }],
  ['un path de mas de 512', { blocks: [finding({ path: `/${'x'.repeat(512)}` })], warnings: [] }],
  ['una razon con RLO', { blocks: [finding({ reason: `a${String.fromCodePoint(0x202e)}b` })], warnings: [] }],
  ['una razon de mas de 300', { blocks: [finding({ reason: 'x'.repeat(301) })], warnings: [] }],
  ['una clave desconocida en un hallazgo', { blocks: [finding({ severity: 'alta' })], warnings: [] }],
  ['201 hallazgos', { blocks: Array.from({ length: 201 }, () => finding()), warnings: [] }],
]) {
  error = await recordError({ result });
  check(error?.code === '22023' && error.message === 'flow_change_result_invalid', `4b: validar con ${label}: flow_change_result_invalid`, error);
}
for (const [label, validator] of [
  ['NULL', null],
  ['otro contrato', { ...VALIDATOR, contract: 'setter-flujo/v2' }],
  ['rules_version como texto', { ...VALIDATOR, rules_version: '1' }],
  ['rules_version 0', { ...VALIDATOR, rules_version: 0 }],
  ['un git_sha corto', { ...VALIDATOR, git_sha: 'abc' }],
  ['una clave de mas', { ...VALIDATOR, extra: 1 }],
]) {
  error = await recordError({ validator });
  check(error?.code === '22023' && error.message === 'flow_change_validator_invalid', `4b: validar con el validador ${label}: flow_change_validator_invalid`, error);
}
for (const [label, preview] of [['NULL', null], ['una lista', []], ['de mas de 128 KiB', { x: 'x'.repeat(131072) }]]) {
  error = await recordError({ preview });
  check(error?.code === '22023' && error.message === 'flow_change_preview_invalid', `4b: una vista previa ${label}: flow_change_preview_invalid`, error);
}
check((await proposal(draft.proposal_id)).status === 'borrador', '4b: ninguna de esas validaciones se registro');
error = await attempt(RECORD_SQL, [json(ATT1), draft.proposal_id, null, json(good), json(VALIDATOR), json(CLEAN), '{}']);
check(error?.message === 'flow_change_revision_invalid', '4b: validar sin revision: 22023', error);
const legitFinding = finding({ path: '/pasos/0/a~1b~0c', reason: 'una razon', alternative: 'una alternativa', limit: 'un limite' });
let recorded = await record(ATT1, draft.proposal_id, draft.revision, good,
  { result: { blocks: [legitFinding, finding({ rule: null, code: 'flow_change_stale_base', kind: 'ciclo', path: '' })], warnings: [] } });
check(recorded.ok && recorded.status === 'borrador' && recorded.blocks === 2, '4b: con bloqueos, sigue en borrador', recorded);
let row = await proposal(draft.proposal_id);
check(row.status === 'borrador' && row.validation?.result?.blocks?.length === 2 && sameJson(row.validation.base, good),
  '4b: con bloqueos, guarda la validacion con la base', row);
let blockedEvent = (await eventsOf(draft.proposal_id)).find((event) => event.event_type === 'flow_change_validation_blocked');
check(sameJson(blockedEvent?.data?.codes, ['unknown_key', 'flow_change_stale_base']), '4b: el evento lleva los codigos', blockedEvent?.data);

// Rehacer: sube la revision y borra la validacion; la revision vieja no valida.
let redone = await saveDraft(ATT1, [versionItem(carrito, 'att1', 'carrito', { nombre: 'Guardas 2' })], await draftBase([carrito]),
  { proposalId: draft.proposal_id, revision: 1 });
check(redone.ok && redone.revision === 2 && (await proposal(draft.proposal_id)).validation === null,
  '4b: rehacer sube la revision y borra la validacion', redone);
let stale = await saveDraft(ATT1, [versionItem(carrito, 'att1', 'carrito')], await draftBase([carrito]),
  { proposalId: draft.proposal_id, revision: 1 });
check(stale.ok === false && stale.reason_code === 'flow_change_revision_mismatch' && stale.revision === 2,
  '4b: rehacer con la revision vieja: flow_change_revision_mismatch', stale);
recorded = await record(ATT1, draft.proposal_id, 1, good);
check(recorded.ok === false && recorded.reason_code === 'flow_change_revision_mismatch', '4b: validar la revision vieja: flow_change_revision_mismatch', recorded);
error = await attempt(SAVE_SQL, [json(ATT1), draft.proposal_id, null, json([versionItem(carrito, 'att1', 'carrito')]), json(await draftBase([carrito])), PURPOSE]);
check(error?.message === 'flow_change_redo_invalid', '4b: rehacer sin la revision: flow_change_redo_invalid', error);
error = await attempt(SAVE_SQL, [json(ATT1), null, null, json([versionItem(carrito, 'att1', 'carrito')]), '[]', PURPOSE]);
check(error?.message === 'flow_change_base_invalid', '4b: una base de quien propone que no es objeto: flow_change_base_invalid', error);
recorded = await record(ATT1, draft.proposal_id, 2, good);
check(recorded.ok && recorded.status === 'validada' && /^[0-9a-f]{64}$/.test(recorded.fingerprint), '4b: con la base exacta, congela', recorded);
row = await proposal(draft.proposal_id);
check(sameJson(row.base, good) && row.fingerprint === recorded.fingerprint && row.validation.base === undefined,
  '4b: la base congelada es la de la base', row);
const fingerprintText = (await one(`select public._flow_change_fingerprint(id, revision, items, draft_base, base, purpose) f
  from public.flow_change_proposals where id = $1`, [draft.proposal_id])).f;
check(fingerprintText === recorded.fingerprint, '4b: la huella es la de la funcion, sobre el contenido, las dos bases y el para que');
const validatedEvent = (await eventsOf(draft.proposal_id)).find((event) => event.event_type === 'flow_change_validated');
check(validatedEvent?.data?.fingerprint === recorded.fingerprint && sameJson(validatedEvent.data.warnings, [])
    && validatedEvent.data.revision === 2, '4b: flow_change_validated lleva la huella, la revision y las advertencias', validatedEvent?.data);
const validatedAt = await one(`select extract(epoch from (expires_at - validated_at))::int s from public.flow_change_proposals where id = $1`, [draft.proposal_id]);
check(validatedAt.s === 86400, '4b: vence a las 24 horas de validar', validatedAt);

// 4c. Rehacer una validada.
stale = await saveDraft(ATT1, [versionItem(carrito, 'att1', 'carrito')], await draftBase([carrito]),
  { proposalId: draft.proposal_id, revision: 2 });
check(stale.ok === false && stale.reason_code === 'flow_change_not_draft', '4c: rehacer una validada: flow_change_not_draft', stale);
recorded = await record(ATT1, draft.proposal_id, 2, good);
check(recorded.ok === false && recorded.reason_code === 'flow_change_not_draft', '4c: validar otra vez una validada: flow_change_not_draft', recorded);

// 4d. Aprobar: la huella viaja con el si.
error = await attempt(`select public.operator_approve_flow_change_v1($1::jsonb, $2::uuid, null, 'dan')`, [json(ATT1), draft.proposal_id]);
check(error?.code === '22023' && error.message === 'flow_change_fingerprint_invalid', '4d: aprobar con la huella NULL: 22023', error);
error = await attempt(`select public.operator_approve_flow_change_v1($1::jsonb, $2::uuid, $3, 'dan')`, [json(ATT1), draft.proposal_id, 'B'.repeat(64)]);
check(error?.message === 'flow_change_fingerprint_invalid', '4d: aprobar con una huella sin la forma: 22023', error);
error = await attempt(`select public.operator_approve_flow_change_v1($1::jsonb, $2::uuid, $3, 'dan')`, [json(ATT1), draft.proposal_id, 'b'.repeat(64)]);
check(error?.code === '22023' && error.message === 'flow_change_fingerprint_mismatch', '4d: aprobar con otra huella: 22023', error);
error = await attempt(`select public.operator_approve_flow_change_v1($1::jsonb, $2::uuid, $3, 'Dan')`, [json(ATT1), draft.proposal_id, recorded.fingerprint]);
check(error?.message === 'flow_store_actor_invalid', '4d: un aprobador sin la forma del operador: flow_store_actor_invalid', error);
const guards = { id: draft.proposal_id, fingerprint: (await proposal(draft.proposal_id)).fingerprint };
let approved = await approve(ATT1, guards.id, guards.fingerprint);
check(approved.ok && approved.replayed === false && approved.status === 'aprobada', '4d: aprobar', approved);
approved = await approve(ATT1, guards.id, guards.fingerprint);
check(approved.ok && approved.replayed === true && approved.approved_by_ref === 'dan', '4d: aprobar dos veces: replayed', approved);
error = await attempt(`select public.operator_approve_flow_change_v1($1::jsonb, $2::uuid, $3, 'dan')`, [json(ATT1), guards.id, 'c'.repeat(64)]);
check(error?.message === 'flow_change_fingerprint_mismatch', '4d: aprobar una aprobada con otra huella: 22023', error);

// 4e. Publicar exige una revalidacion fresca de esa misma huella.
const minutesAgo = (minutes) => new Date(Date.now() - minutes * 60_000).toISOString();
const badRevalidations = [
  ['SQL NULL', null],
  ['{}', {}],
  ['sin catalog_sha256', (() => { const value = revalidation(guards.fingerprint); delete value.catalog_sha256; return value; })()],
  ['con bloqueos', revalidation(guards.fingerprint, { result: { blocks: [finding({ rule: 13, code: 'template_not_approved', kind: 'proteccion', path: '/pasos/0' })], warnings: [] } })],
  ['de otra huella', revalidation('c'.repeat(64))],
  ['con rules_version 0', revalidation(guards.fingerprint, { validator: { ...VALIDATOR, rules_version: 0 } })],
  ['hecha hace 11 minutos', revalidation(guards.fingerprint, { validated_at: minutesAgo(11) })],
  ['del futuro', revalidation(guards.fingerprint, { validated_at: minutesAgo(-2) })],
  ['con una fecha basura', revalidation(guards.fingerprint, { validated_at: 'ayer' })],
  // Mas alla de las nueve del diseno: una palabra que Postgres lee como fecha,
  // una fecha que no es texto, una fecha sin zona y una clave de mas.
  ['con validated_at now', revalidation(guards.fingerprint, { validated_at: 'now' })],
  ['con validated_at numero', revalidation(guards.fingerprint, { validated_at: Date.now() })],
  ['con una fecha sin zona', revalidation(guards.fingerprint, { validated_at: minutesAgo(1).replace('Z', '') })],
  ['con una clave de mas', revalidation(guards.fingerprint, { preview: {} })],
  ['con un catalogo que no es hex', revalidation(guards.fingerprint, { catalog_sha256: 'z'.repeat(64) })],
];
for (const [label, reval] of badRevalidations) {
  const result = await publish(ATT1, guards.id, guards.fingerprint, reval);
  check(result.ok === false && result.reason_code === 'flow_change_revalidation_blocked', `4e: publicar con la revalidacion ${label}: revalidation_blocked`, result);
}
check(await countRows('flow_change_events', `proposal_id = $1 and event_type = 'flow_change_publish_rejected' and reason_code = 'flow_change_revalidation_blocked'`, [guards.id])
  === badRevalidations.length, '4e: cada rechazo deja su evento');
check((await proposal(guards.id)).status === 'aprobada' && (await control(carrito)).generation === 6,
  '4e: nada se publico');
error = await attempt(PUBLISH_SQL, [json(ATT1), guards.id, null, json(revalidation(guards.fingerprint))]);
check(error?.code === '22023' && error.message === 'flow_change_fingerprint_invalid', '4e: publicar con la huella NULL: 22023', error);
error = await attempt(PUBLISH_SQL, [json(ATT1), guards.id, 'd'.repeat(64), json(revalidation('d'.repeat(64)))]);
check(error?.message === 'flow_change_fingerprint_mismatch', '4e: publicar con otra huella: 22023', error);
let result = await publish(ATT1, guards.id, guards.fingerprint, revalidation(guards.fingerprint, { validated_at: minutesAgo(9) }));
check(result.ok && result.replayed === false && result.published[0].version === 3, '4e: con una revalidacion de hace 9 minutos, publica la v3', result);
result = await publish(ATT1, guards.id, guards.fingerprint, revalidation(guards.fingerprint));
check(result.ok && result.replayed === true, '4e: publicar dos veces: replayed, sin otra version', result);
check(await countRows('flow_definitions', 'flow_id = $1', [carrito]) === 3, '4e: una sola version nueva');
error = await attempt(PUBLISH_SQL, [json(ATT1), guards.id, 'e'.repeat(64), json(revalidation('e'.repeat(64)))]);
check(error?.message === 'flow_change_fingerprint_mismatch', '4e: publicar una publicada con otra huella: 22023', error);
await expectControl(carrito, { state: 'prendido', active_version: 3, generation: 7 }, '4e: el carrito sigue prendido con la v3');

// 4f. La base de toda la instancia: publicar otro flujo invalida, pausar otro
// flujo no, y pausar el mismo entre aprobar y publicar si.
let pending = await prepare(ATT1, [versionItem(carrito, 'att1', 'carrito', { nombre: 'Base A' })], { approveIt: false });
check(pending.validated?.status === 'validada', '4f: una propuesta del carrito validada', pending.validated);
await cycle(ATT1, [versionItem(pagoFallido, 'att1', 'pago_fallido', {
  nombre: 'Pago fallido', dispara: { evento: 'pago_fallido' },
  pasos: [{ plantilla: 'att1_compra_fallida_01', idioma: 'es_MX', variables: ['nombre', 'producto'] }],
})], '4f: la v1 del pago fallido mientras la del carrito esta validada');
approved = await approve(ATT1, pending.id, pending.fingerprint);
check(approved.ok === false && approved.reason_code === 'flow_change_stale_base', '4f: aprobar despues de que otro flujo cambio de version: stale_base', approved);
check((await proposal(pending.id)).status === 'validada'
    && (await eventsOf(pending.id)).some((event) => event.event_type === 'flow_change_approval_rejected'),
  '4f: el rechazo de la aprobacion queda registrado y la propuesta sigue validada');
await cycle(ATT1, [stateItem(pagoFallido, 'att1', 'pago_fallido', 'prender')], '4f: prender el pago fallido');
pending = await prepare(ATT1, [versionItem(carrito, 'att1', 'carrito', { nombre: 'Base B' })], { approveIt: false });
paused = await pause(ATT1, 'att1', 'pago_fallido', (await control(pagoFallido)).generation);
approved = await approve(ATT1, pending.id, pending.fingerprint);
check(paused.ok && paused.changed && approved.ok && approved.replayed === false,
  '4f: pausar otro flujo no invalida la propuesta del carrito', { paused, approved });
paused = await pause(ATT1, 'att1', 'carrito', (await control(carrito)).generation);
const beforeStale = await control(carrito);
result = await publish(ATT1, pending.id, pending.fingerprint, revalidation(pending.fingerprint));
check(paused.ok && result.ok === false && result.reason_code === 'flow_change_stale_base',
  '4f: una pausa del mismo flujo entre aprobar y publicar: stale_base', { paused, result });
check((await proposal(pending.id)).status === 'aprobada', '4f: la propuesta queda aprobada hasta que vence');
const staleEvent = (await eventsOf(pending.id)).find((event) => event.event_type === 'flow_change_publish_rejected');
check(staleEvent?.reason_code === 'flow_change_stale_base' && sameJson(staleEvent.data.flows, [carrito]),
  '4f: el rechazo de la publicacion lleva los flujos que cambiaron', staleEvent);
check(sameJson(await control(carrito), beforeStale) && await countRows('flow_definitions', 'flow_id = $1', [carrito]) === 3,
  '4f: no se escribio nada');

// 4g. La base que leyo quien propone.
const proposalsBefore = await countRows('flow_change_proposals');
const current = await draftBase([carrito]);
let saved = await saveDraft(ATT1, [versionItem(carrito, 'att1', 'carrito', { nombre: 'Vieja' })],
  { [carrito]: { active_version: current[carrito].active_version + 1 } });
check(saved.ok === false && saved.reason_code === 'flow_change_stale_base' && sameJson(saved.base, current),
  '4g: guardar sobre una version que no es la activa: stale_base con la de hoy', saved);
saved = await saveDraft(ATT1, [versionItem(carrito, 'att1', 'carrito', { nombre: 'Nula' })], { [carrito]: { active_version: null } });
check(saved.reason_code === 'flow_change_stale_base', '4g: una primera version sobre un flujo que ya tiene una: stale_base', saved);
saved = await saveDraft(ATT1, [versionItem(carrito, 'att1', 'carrito', { nombre: 'Con generacion' })],
  { [carrito]: { active_version: current[carrito].active_version, generation: 1 } });
check(saved.reason_code === 'flow_change_stale_base', '4g: una base de quien propone con otra forma: stale_base', saved);
check(await countRows('flow_change_proposals') === proposalsBefore, '4g: no se guardo nada');

// 4h. Los items, con su forma minima.
const carritoItem = versionItem(carrito, 'att1', 'carrito');
for (const [label, items] of [
  ['en_curso cancelar', [{ ...carritoItem, en_curso: 'cancelar' }]],
  ['en_curso migrar', [{ ...carritoItem, en_curso: 'migrar' }]],
  ['dos de contenido sobre el mismo flujo', [carritoItem, backItem(carrito, 'att1', 'carrito', 1)]],
  ['dos de estado sobre el mismo flujo', [stateItem(carrito, 'att1', 'carrito', 'prender'), stateItem(carrito, 'att1', 'carrito', 'apagar')]],
  ['una definicion con version', [{ ...carritoItem, definicion: { ...carritoItem.definicion, version: 4 } }]],
  ['una definicion publicada', [{ ...carritoItem, definicion: { ...carritoItem.definicion, estado: 'publicada' } }]],
  ['otro esquema', [{ ...carritoItem, definicion: { ...carritoItem.definicion, esquema: 'setter-flujo/v2' } }]],
  ['un flujo distinto del item', [{ ...carritoItem, definicion: { ...carritoItem.definicion, flujo: 'pago_fallido' } }]],
  ['un producto distinto del item', [{ ...carritoItem, definicion: { ...carritoItem.definicion, producto: 'att2' } }]],
  ['una clave de mas', [{ ...carritoItem, nota: 'x' }]],
  ['un prender con en_curso', [{ ...stateItem(carrito, 'att1', 'carrito', 'prender'), en_curso: 'seguir' }]],
  ['un volver con la version como texto', [{ ...backItem(carrito, 'att1', 'carrito', 1), version: '1' }]],
  ['un tipo que no existe', [{ ...stateItem(carrito, 'att1', 'carrito', 'pausar') }]],
  ['un flow_id con mayusculas', [{ ...carritoItem, flow_id: carrito.toUpperCase() }]],
  ['un producto que no es texto', [{ ...carritoItem, producto: true }]],
  ['once items', Array.from({ length: 11 }, () => stateItem(carrito, 'att1', 'carrito', 'prender'))],
  ['ninguno', []],
  ['una definicion de mas de 32 KiB', [{ ...carritoItem, definicion: { ...carritoItem.definicion, nombre: 'x'.repeat(33000) } }]],
  ['un ok_del_cliente con otra forma', [{ ...carritoItem, ok_del_cliente: { quien: 'Juan', medio: 'WhatsApp' } }]],
  ['un ok_del_cliente con una fecha imposible', [{ ...carritoItem, ok_del_cliente: { quien: 'Juan', medio: 'WhatsApp', fecha: '2026-13-01' } }]],
  ['un ok_del_cliente con RLO', [{ ...carritoItem, ok_del_cliente: { quien: `Ju${String.fromCodePoint(0x202e)}an`, medio: 'WhatsApp', fecha: '2026-10-15' } }]],
]) {
  error = await attempt(SAVE_SQL, [json(ATT1), null, null, json(items), json(await draftBase([carrito])), PURPOSE]);
  check(error?.code === '22023' && error.message === 'flow_change_items_invalid', `4h: ${label}: flow_change_items_invalid`, error);
}
saved = await saveDraft(ATT1, [{ ...carritoItem, ok_del_cliente: { quien: 'Juan', medio: 'WhatsApp', fecha: '2026-10-15' } },
  stateItem(carrito, 'att1', 'carrito', 'apagar')], await draftBase([carrito]));
check(saved.ok, '4h: una version con el OK del cliente y un apagar del mismo flujo se guardan', saved);
error = await attempt(SAVE_SQL, [json(ATT1), null, null, json([{ ...carritoItem, flujo: 'no_existe', definicion: { ...carritoItem.definicion, flujo: 'no_existe' } }]), '{}', PURPOSE]);
check(error?.code === 'P0002' && error.message === 'flow_not_found', '4h: un item cuyo flujo no es el del control: P0002 flow_not_found', error);

// 4i. El texto libre: una linea, sin controles ni bidi.
const codepoint = (value) => String.fromCodePoint(value);
for (const [label, text] of [
  ['ESC', `a${codepoint(0x1b)}[2Kb`],
  ['RLO', `a${codepoint(0x202e)}b`],
  ['ZWSP', `a${codepoint(0x200b)}b`],
  ['un salto de linea', 'a\nb'],
  ['NEL', `a${codepoint(0x85)}b`],
  ['BOM', `a${codepoint(0xfeff)}b`],
  ['guion suave', `a${codepoint(0xad)}b`],
  ['ALM', `a${codepoint(0x61c)}b`],
  ['LRI', `a${codepoint(0x2066)}b`],
  ['solo espacios', '   '],
  ['501 caracteres', 'x'.repeat(501)],
]) {
  error = await attempt(SAVE_SQL, [json(ATT1), null, null, json([carritoItem]), json(await draftBase([carrito])), text]);
  check(error?.code === '22023' && error.message === 'flow_store_text_invalid', `4i: purpose con ${label}: flow_store_text_invalid`, error);
}
const legitText = `que el carrito salga a las 2 horas, ${codepoint(0xf1)}and${codepoint(0xfa)}: ${codepoint(0xbf)}s${codepoint(0xed)}? ${codepoint(0x1f642)}`;
saved = await saveDraft(ATT1, [carritoItem], await draftBase([carrito]), { purpose: legitText });
check(saved.ok, '4i: un texto con tildes, enie, signos de apertura y un emoji pasa', saved);
const draftEvent = (await eventsOf(saved.proposal_id)).find((event) => event.event_type === 'flow_change_draft_saved');
const savedDefinitionText = (await one(
  `select (items -> 0 -> 'definicion')::text d from public.flow_change_proposals where id = $1`, [saved.proposal_id])).d;
check(draftEvent?.data?.purpose_sha256 === sha256(legitText)
    && draftEvent.data.items[0].definition_sha256 === sha256(savedDefinitionText)
    && draftEvent.data.revision === 1 && !JSON.stringify(draftEvent.data).includes('carrito salga'),
  '4i: el evento lleva el sha256 del para que y de la definicion, nunca el texto', draftEvent?.data);
// Un cambio que el disparador acepta (pausado -> prendido) con un RLO en el
// motivo: lo ataja el CHECK de la columna.
check((await control(pagoFallido)).state === 'pausado', '4i: (el pago fallido esta pausado)');
error = await attempt(`update public.flow_controls set change_reason = 'x' || chr(8238) || 'y', state = 'prendido',
  change_kind = 'publicado', changed_by_proposal_id = $2::uuid, generation = generation + 1 where flow_id = $1`,
[pagoFallido, guards.id]);
check(error?.code === '23514' && /violates check constraint "flow_controls_change_reason_check"/.test(error.message),
  '4i: el CHECK ataja el texto aunque no pase por una funcion', error);

// 4j. Lo que no se borra ni se modifica.
for (const [sql, message] of [
  ['update public.flow_definitions set published_at = now()', 'flow_definition_is_immutable'],
  ['delete from public.flow_definitions', 'flow_definition_is_immutable'],
  // Sin cascade lo frena antes la clave foranea de flow_controls (0A000).
  ['truncate public.flow_definitions cascade', 'flow_definition_is_immutable'],
  [`update public.flow_change_events set reason_code = 'x'`, 'flow_change_event_is_append_only'],
  ['delete from public.flow_change_events', 'flow_change_event_is_append_only'],
  ['truncate public.flow_change_events', 'flow_change_event_is_append_only'],
  ['delete from public.flow_controls', 'flow_control_is_permanent'],
  ['truncate public.flow_controls cascade', 'flow_control_is_permanent'],
  ['delete from public.flow_change_proposals', 'flow_change_proposal_is_permanent'],
  ['truncate public.flow_change_proposals cascade', 'flow_change_proposal_is_permanent'],
]) {
  error = await attempt(sql);
  check(error?.code === '55000' && error.message === message, `4j: ${sql}: ${message}`, error);
}
check(await countRows('flow_definitions') > 0 && await countRows('flow_change_events') > 0, '4j: las filas siguen ahi');

// 4k. Una publicada queda congelada entera.
error = await attempt(`update public.flow_change_proposals set approved_by_ref = 'otro' where id = $1`, [guards.id]);
check(error?.code === '55000' && error.message === 'flow_change_proposal_frozen', '4k: una publicada no cambia ni el aprobador', error);
error = await attempt(`update public.flow_change_proposals set status = status where id = $1`, [guards.id]);
check(error?.message === 'flow_change_proposal_frozen', '4k: una publicada no acepta ni un UPDATE sin cambios', error);
error = await attempt(`update public.flow_change_proposals set validation = '{}'::jsonb where id = $1`, [pending.id]);
check(error?.message === 'flow_change_proposal_frozen', '4k: una aprobada no cambia la validacion', error);
error = await attempt(`update public.flow_change_proposals set approved_by_ref = 'otro' where id = $1`, [pending.id]);
check(error?.message === 'flow_change_proposal_frozen', '4k: una aprobada no cambia el aprobador', error);

// 4l. Un UPDATE directo del duenio deja su evento, y las transiciones se miran
// solo si el estado cambia.
let eventsBefore = await countRows('flow_change_events', 'flow_id = $1', [pagoFallido]);
error = await attempt(`update public.flow_controls set state = 'prendido', generation = generation + 1, change_kind = 'publicado',
  changed_by_proposal_id = $2::uuid where flow_id = $1`, [pagoFallido, guards.id]);
check(error === null && await countRows('flow_change_events', 'flow_id = $1', [pagoFallido]) === eventsBefore + 1,
  '4l: un UPDATE directo del duenio a un control deja exactamente un evento', error);
error = await attempt(`update public.flow_controls set state = 'prendido', generation = generation + 1, change_kind = 'publicado',
  changed_by_proposal_id = $2::uuid where flow_id = $1`, [pagoFallido, guards.id]);
check(error === null, '4l: prendido -> prendido no es una transicion', error);
error = await attempt(`update public.flow_controls set state = 'apagado', generation = generation + 1, change_kind = 'publicado',
  changed_by_proposal_id = $2::uuid where flow_id = $1`, [carritoAtt2, guards.id]);
check(error === null, '4l: apagado -> apagado tampoco', error);

// Los demas codigos de los disparadores, por DML directo del duenio.
const directo = (await register(ATT1, 'att1', 'directo')).flow_id;
for (const [label, sql, params, code, message] of [
  ['un control que no nace apagado', `insert into public.flow_controls (tenant_ref, ally_ref, product_ref, flow_key, state, generation,
    change_kind, changed_by_kind, changed_by_ref, change_reason) values ('lancemos', 'att1', 'att1', 'nace_prendido', 'prendido', 0,
    'registrado', 'operador', 'dan', 'alta')`, [], '23514', 'flow_control_must_start_off'],
  ['un UPDATE que no sube la generacion', `update public.flow_controls set change_kind = 'publicado', changed_by_proposal_id = $2::uuid
    where flow_id = $1`, [pagoFallido, guards.id], '23514', 'flow_control_generation_must_increment'],
  ['un UPDATE que sube la generacion en 2', `update public.flow_controls set generation = generation + 2, change_kind = 'publicado',
    changed_by_proposal_id = $2::uuid where flow_id = $1`, [pagoFallido, guards.id], '23514', 'flow_control_generation_must_increment'],
  ['cambiar la clave', `update public.flow_controls set flow_key = 'otro', generation = generation + 1, change_kind = 'publicado',
    changed_by_proposal_id = $2::uuid where flow_id = $1`, [pagoFallido, guards.id], '55000', 'flow_control_identity_is_immutable'],
  ['cambiar la instancia', `update public.flow_controls set ally_ref = 'otra', generation = generation + 1, change_kind = 'publicado',
    changed_by_proposal_id = $2::uuid where flow_id = $1`, [pagoFallido, guards.id], '55000', 'flow_control_identity_is_immutable'],
  ['volver a registrado', `update public.flow_controls set generation = generation + 1, change_kind = 'registrado'
    where flow_id = $1`, [pagoFallido], '23514', 'flow_control_change_kind_invalid'],
  ['borrar la version activa', `update public.flow_controls set active_version = null, state = 'apagado', generation = generation + 1,
    change_kind = 'publicado', changed_by_proposal_id = $2::uuid where flow_id = $1`, [pagoFallido, guards.id], '23514',
    'flow_control_version_cannot_be_cleared'],
  ['de apagado a pausado', `update public.flow_controls set state = 'pausado', generation = generation + 1, change_kind = 'pausado'
    where flow_id = $1`, [carritoAtt2], '23514', 'flow_control_transition_invalid'],
  ['una pausa que no pausa', `update public.flow_controls set generation = generation + 1, change_kind = 'pausado',
    changed_by_proposal_id = null where flow_id = $1`, [pagoFallido], '23514', 'flow_control_change_kind_invalid'],
  ['una propuesta de borrador a publicada', `update public.flow_change_proposals set status = 'publicada' where id = $1`,
    [saved.proposal_id], '23514', 'flow_change_transition_invalid'],
  ['un borrador que cambia sin subir la revision', `update public.flow_change_proposals set purpose = 'otro para que' where id = $1`,
    [saved.proposal_id], '23514', 'flow_change_revision_must_increment'],
  ['cambiar quien la pidio', `update public.flow_change_proposals set requested_by_ref = 'otro' where id = $1`,
    [saved.proposal_id], '55000', 'flow_change_proposal_identity_is_immutable'],
  ['una version de un flujo que no existe', `insert into public.flow_definitions (flow_id, version, schema_ref, definition,
    definition_sha256, proposal_id, approved_by_kind, approved_by_ref) values (gen_random_uuid(), 1, 'setter-flujo/v1', '{}'::jsonb,
    repeat('0', 64), $1::uuid, 'operador', 'dan')`, [guards.id], '23503', 'flow_definition_flow_not_found'],
]) {
  error = await attempt(sql, params);
  check(error?.code === code && error.message === message, `4l: ${label}: ${message}`, error);
}
const directDefinition = (version, overrides = {}) => ({
  ...definition('att1', 'directo', overrides), estado: 'publicada', version,
});
const insertDefinition = async (flowId, version, value, sha = '0'.repeat(64)) => attempt(`
  insert into public.flow_definitions (flow_id, version, schema_ref, definition, definition_sha256, proposal_id,
    approved_by_kind, approved_by_ref) values ($1::uuid, $2::int, 'setter-flujo/v1', $3::jsonb, $4, $5::uuid, 'operador', 'dan')`,
[flowId, version, json(value), sha, guards.id]);
for (const [label, version, value] of [
  ['otro flujo', 1, { ...directDefinition(1), flujo: 'carrito' }],
  ['otro producto', 1, { ...directDefinition(1), producto: 'att2' }],
  ['un borrador', 1, { ...directDefinition(1), estado: 'borrador' }],
  ['la version como texto', 1, { ...directDefinition(1), version: '1' }],
  ['otra version adentro', 1, directDefinition(2)],
  ['otro esquema adentro', 1, { ...directDefinition(1), esquema: 'setter-flujo/v2' }],
]) {
  error = await insertDefinition(directo, version, value);
  check(error?.code === '23514' && error.message === 'flow_definition_identity_mismatch', `4l: una version con ${label}: flow_definition_identity_mismatch`, error);
}
error = await insertDefinition(directo, 2, directDefinition(2));
check(error?.code === '23514' && error.message === 'flow_definition_version_not_next', '4l: una version que no es la siguiente: flow_definition_version_not_next', error);
error = await insertDefinition(directo, 1, directDefinition(1), 'f'.repeat(64));
const stored = await one(`select definition_sha256 s, definition::text t from public.flow_definitions where flow_id = $1 and version = 1`, [directo]);
check(error === null && stored.s === sha256(stored.t) && stored.s !== 'f'.repeat(64),
  '4l: definition_sha256 lo calcula el disparador, no se confia en el que llega', { error, stored: stored?.s });
check((await control(directo)).active_version === null, '4l: una version escrita directo no mueve el control');

// 4m. El vencimiento, materializado de forma perezosa con los locks tomados.
const age = async (proposalId) => replica(`update public.flow_change_proposals
  set validated_at = validated_at - interval '25 hours', expires_at = expires_at - interval '25 hours' where id = $1`, [proposalId]);
const expectExpired = async (proposalId, fromStatus, label) => {
  const row2 = await proposal(proposalId);
  const expired = (await eventsOf(proposalId)).find((event) => event.event_type === 'flow_change_expired');
  check(row2.status === 'vencida' && row2.closed_by_kind === 'sistema' && row2.closed_by_ref === 'vencimiento'
      && expired?.actor_kind === 'sistema' && expired.reason_code === 'flow_change_proposal_expired'
      && expired.data.from_status === fromStatus, label, { row2, expired });
};
let expiring = await prepare(ATT1, [versionItem(carrito, 'att1', 'carrito', { nombre: 'Vence al aprobar' })], { approveIt: false });
await age(expiring.id);
result = await approve(ATT1, expiring.id, expiring.fingerprint);
check(result.ok === false && result.reason_code === 'flow_change_proposal_expired', '4m: aprobar una vencida: flow_change_proposal_expired', result);
await expectExpired(expiring.id, 'validada', '4m: aprobar una vencida la deja vencida con su evento');
result = await approve(ATT1, expiring.id, expiring.fingerprint);
check(result.ok === false && result.reason_code === 'flow_change_not_validated' && result.status === 'vencida',
  '4m: vencida es final', result);
expiring = await prepare(ATT1, [versionItem(carrito, 'att1', 'carrito', { nombre: 'Vence al publicar' })]);
check(expiring.approved?.ok, '4m: una aprobada', expiring.approved);
await age(expiring.id);
result = await publish(ATT1, expiring.id, expiring.fingerprint, revalidation(expiring.fingerprint));
check(result.ok === false && result.reason_code === 'flow_change_proposal_expired', '4m: publicar una vencida: flow_change_proposal_expired', result);
await expectExpired(expiring.id, 'aprobada', '4m: publicar una vencida la deja vencida con su evento');
check(await countRows('flow_definitions', 'flow_id = $1', [carrito]) === 3, '4m: nada se publico tarde');
expiring = await prepare(ATT1, [versionItem(carrito, 'att1', 'carrito', { nombre: 'Vence al descartar' })], { approveIt: false });
await age(expiring.id);
result = await discard(ATT1, expiring.id);
check(result.ok === false && result.reason_code === 'flow_change_final' && result.status === 'vencida',
  '4m: descartar una vencida: flow_change_final', result);
await expectExpired(expiring.id, 'validada', '4m: descartar una vencida la deja vencida con su evento');

// 4n. Lo que publicar vuelve a mirar, por si la base cambio por otro camino: un
// error de programa aborta la transaccion entera y no escribe nada.
const snapshot = async () => ({
  definitions: await countRows('flow_definitions'),
  events: await countRows('flow_change_events'),
  controls: (await query('select flow_id, generation::int g, state, active_version from public.flow_controls order by flow_id'))
    .map((item) => `${item.flow_id}:${item.g}:${item.state}:${item.active_version}`).join(','),
});
for (const [label, items, message, before] of [
  ['un volver a una version que no existe', [backItem(carrito, 'att1', 'carrito', 99)], 'flow_change_rollback_target_invalid'],
  ['un volver a la version activa', [backItem(carrito, 'att1', 'carrito', 3)], 'flow_change_rollback_target_invalid'],
  ['prender algo prendido', [stateItem(carrito, 'att1', 'carrito', 'prender')], 'flow_change_state_transition_invalid',
    async () => { await cycle(ATT1, [stateItem(carrito, 'att1', 'carrito', 'prender')], '4n: sacar la pausa del carrito'); }],
  ['prender un flujo sin version', [stateItem(carritoAtt2, 'att2', 'carrito', 'prender')], 'flow_change_flow_has_no_version'],
  ['apagar algo apagado', [stateItem(carritoAtt2, 'att2', 'carrito', 'apagar')], 'flow_change_state_transition_invalid'],
]) {
  if (before) await before();
  // La validacion la registra el duenio como limpia aunque el validador la
  // habria bloqueado: la base le cree, pero publicar vuelve a mirar.
  const seeded = await prepare(ATT1, items);
  check(seeded.approved?.ok, `4n: ${label}: sembrada y aprobada`, seeded);
  const beforeSnapshot = await snapshot();
  error = await attempt(PUBLISH_SQL, [json(ATT1), seeded.id, seeded.fingerprint, json(revalidation(seeded.fingerprint))]);
  check(error?.code === '55000' && error.message === message, `4n: ${label}: 55000 ${message}`, error);
  check(sameJson(await snapshot(), beforeSnapshot) && (await proposal(seeded.id)).status === 'aprobada',
    `4n: ${label}: no escribe nada`);
}

// 4o. Un producto que deja de estar ligado a la instancia.
await setBindingStatus('att2', 'retired');
error = await attempt(SAVE_SQL, [json(ATT1), null, null, json([versionItem(carritoAtt2, 'att2', 'carrito')]), json(await draftBase([carritoAtt2])), PURPOSE]);
check(error?.code === '55000' && error.message === 'flow_store_product_not_bound', '4o: guardar sobre un producto desligado: 55000', error);
error = await attempt(`select public.operator_register_flow_v1($1::jsonb, 'att2', 'otro', 'dan', 'alta')`, [json(ATT1)]);
check(error?.code === '22023' && error.message === 'flow_store_product_not_bound', '4o: registrar en un producto desligado: 22023', error);
await setBindingStatus('att2', 'active');
const unbound = await prepare(ATT1, [versionItem(carritoAtt2, 'att2', 'carrito')]);
check(unbound.approved?.ok, '4o: una primera version del carrito de att2 aprobada', unbound);
await setBindingStatus('att2', 'retired');
let beforeUnbound = await snapshot();
error = await attempt(PUBLISH_SQL, [json(ATT1), unbound.id, unbound.fingerprint, json(revalidation(unbound.fingerprint))]);
check(error?.code === '55000' && error.message === 'flow_store_product_not_bound' && sameJson(await snapshot(), beforeUnbound),
  '4o: publicar sobre un producto que se desligo despues del si: 55000 y no escribe nada', error);
await setBindingStatus('att2', 'active');
result = await publish(ATT1, unbound.id, unbound.fingerprint, revalidation(unbound.fingerprint));
check(result.ok && result.published[0].producto === 'att2', '4o: ligado otra vez, publica', result);

// 4p. Pausar y descartar.
paused = await pause(ATT1, 'att2', 'carrito', (await control(carritoAtt2)).generation);
check(paused.ok === false && paused.reason_code === 'flow_pause_requires_on', '4p: pausar algo apagado: flow_pause_requires_on', paused);
error = await attempt(`select public.operator_pause_flow_v1($1::jsonb, 'att1', 'carrito', null, 'dan', 'pausa')`, [json(ATT1)]);
check(error?.code === '22023' && error.message === 'flow_control_generation_invalid', '4p: pausar sin generacion: 22023', error);
error = await attempt(`select public.operator_pause_flow_v1($1::jsonb, 'att1', 'no_existe', 0, 'dan', 'pausa')`, [json(ATT1)]);
check(error?.code === 'P0002' && error.message === 'flow_not_found', '4p: pausar un flujo que no existe: P0002', error);
error = await attempt(`select public.operator_pause_flow_v1($1::jsonb, 'att1', 'carrito', 0, 'dan', $2)`, [json(ATT1), `a${codepoint(0x202e)}b`]);
check(error?.message === 'flow_store_text_invalid', '4p: pausar con RLO en el motivo: 22023', error);
const toDiscard = await saveDraft(ATT1, [carritoItem], await draftBase([carrito]));
result = await discard(ATT1, toDiscard.proposal_id, 'ya no hace falta');
row = await proposal(toDiscard.proposal_id);
const discardedEvent = (await eventsOf(toDiscard.proposal_id)).find((event) => event.event_type === 'flow_change_discarded');
check(result.ok && result.replayed === false && row.status === 'descartada' && row.closed_by_kind === 'operador'
    && discardedEvent?.data?.from_status === 'borrador' && discardedEvent.data.close_reason_sha256 === sha256('ya no hace falta'),
  '4p: descartar un borrador deja su evento con el sha256 del motivo', { result, row, discardedEvent });
result = await discard(ATT1, toDiscard.proposal_id);
check(result.ok && result.replayed === true, '4p: descartar dos veces: replayed', result);
result = await discard(ATT1, guards.id);
check(result.ok === false && result.reason_code === 'flow_change_final' && result.status === 'publicada', '4p: descartar una publicada: flow_change_final', result);
error = await attempt(`select public.operator_discard_flow_change_v1($1::jsonb, $2::uuid, 'dan', $3)`, [json(ATT1), pending.id, 'a\nb']);
check(error?.message === 'flow_store_text_invalid', '4p: descartar con un salto de linea en el motivo: 22023', error);
result = await discard(ATT1, pending.id, 'la base cambio');
check(result.ok && (await proposal(pending.id)).status === 'descartada', '4p: descartar una aprobada', result);

// ===========================================================================
// 5. El actor, llamando a los helpers como duenio.
// ===========================================================================
const AE = 'U0123ABCD9';
const nuevo = (await register(ATT1, 'att1', 'nuevo')).flow_id;
error = await attempt(`select public._flow_change_save_draft('lancemos', 'att1', null, null, $1::jsonb, $2::jsonb, 'p', 'automation_expert', $3)`,
  [json([versionItem(nuevo, 'att1', 'nuevo')]), json(await draftBase([nuevo])), AE]);
check(error?.code === '55000' && error.message === 'flow_creation_not_enabled', '5: el Automation Expert no crea flujos: flow_creation_not_enabled', error);
const operatorFirst = await saveDraft(ATT1, [versionItem(nuevo, 'att1', 'nuevo')], await draftBase([nuevo]));
check(operatorFirst.ok, '5: el operador si escribe la primera version', operatorFirst);
await discard(ATT1, operatorFirst.proposal_id);
const aeDraft = (await one(`select public._flow_change_save_draft('lancemos', 'att1', null, null, $1::jsonb, $2::jsonb,
  'subir el tope del carrito', 'automation_expert', $3) r`, [json([versionItem(carrito, 'att1', 'carrito', { nombre: 'Del agente' })]),
  json(await draftBase([carrito])), AE])).r;
check(aeDraft.ok, '5: el Automation Expert propone sobre un flujo con version', aeDraft);
const aeRow = await one(`select requested_by_kind, requested_by_ref from public.flow_change_proposals where id = $1`, [aeDraft.proposal_id]);
check(aeRow.requested_by_kind === 'automation_expert' && aeRow.requested_by_ref === AE, '5: la propuesta lleva la clase y el ID declarado', aeRow);
error = await attempt(SAVE_SQL, [json(ATT1), aeDraft.proposal_id, 1, json([carritoItem]), json(await draftBase([carrito])), PURPOSE]);
check(error?.code === 'P0002', '5: el operador no rehace una propuesta del Automation Expert: P0002', error);
error = await attempt(RECORD_SQL, [json(ATT1), aeDraft.proposal_id, 1, json(await instanceBase(ATT1, [carrito])), json(VALIDATOR), json(CLEAN), '{}']);
check(error?.code === 'P0002', '5: el operador no congela una propuesta del Automation Expert: P0002', error);
const aeValidated = (await one(`select public._flow_change_record_validation('lancemos', 'att1', $1::uuid, 1, $2::jsonb, $3::jsonb,
  $4::jsonb, '{}'::jsonb, 'automation_expert', $5) r`, [aeDraft.proposal_id, json(await instanceBase(ATT1, [carrito])),
  json(VALIDATOR), json(CLEAN), AE])).r;
check(aeValidated.status === 'validada', '5: el Automation Expert valida su propia propuesta', aeValidated);
error = await attempt(`select public._flow_change_approve('lancemos', 'att1', $1::uuid, $2, 'automation_expert', $3)`,
  [aeDraft.proposal_id, aeValidated.fingerprint, AE]);
check(error?.code === '22023' && error.message === 'flow_store_actor_invalid', '5: el Automation Expert no aprueba', error);
const opDraft = await saveDraft(ATT1, [versionItem(carrito, 'att1', 'carrito', { nombre: 'Del operador' })], await draftBase([carrito]));
error = await attempt(`select public._flow_change_discard('lancemos', 'att1', $1::uuid, 'automation_expert', $2, 'no va')`, [opDraft.proposal_id, AE]);
check(error?.code === 'P0002', '5: el Automation Expert no descarta una propuesta del operador: P0002', error);
result = (await one(`select public._flow_change_discard('lancemos', 'att1', $1::uuid, 'automation_expert', $2, 'me equivoque') r`,
  [aeDraft.proposal_id, AE])).r;
check(result.ok, '5: el Automation Expert descarta la suya', result);
const aeSecond = (await one(`select public._flow_change_save_draft('lancemos', 'att1', null, null, $1::jsonb, $2::jsonb,
  'otra del agente', 'automation_expert', $3) r`, [json([versionItem(carrito, 'att1', 'carrito', { nombre: 'Del agente 2' })]),
  json(await draftBase([carrito])), AE])).r;
result = await discard(ATT1, aeSecond.proposal_id, 'la descarta un aprobador');
check(result.ok && (await proposal(aeSecond.proposal_id)).closed_by_kind === 'operador',
  '5: el operador (aprobador) descarta una propuesta del Automation Expert', result);
error = await attempt(`insert into public.flow_change_events (tenant_ref, ally_ref, proposal_id, event_type, actor_kind, actor_ref, reason_code)
  values ('lancemos', 'att1', $1::uuid, 'flow_change_approved', 'slack', 'U0123ABCD9', 'x')`, [opDraft.proposal_id]);
check(error === null, '5: los CHECK aceptan la clase slack con un ID de Slack', error);
error = await attempt(`insert into public.flow_change_events (tenant_ref, ally_ref, proposal_id, event_type, actor_kind, actor_ref, reason_code)
  values ('lancemos', 'att1', $1::uuid, 'flow_change_approved', 'slack', 'dan', 'x')`, [opDraft.proposal_id]);
check(error?.code === '23514', '5: un nombre escrito no pasa como ID de Slack', error);
// Aprobar con slack, y la version publicada lo guarda (la tabla inmutable ya
// acepta slack: la pieza 4 no tiene que alterarla).
const slackValidated = await record(ATT1, opDraft.proposal_id, opDraft.revision, await instanceBase(ATT1, [carrito]));
const slackApproved = (await one(`select public._flow_change_approve('lancemos', 'att1', $1::uuid, $2, 'slack', 'U0123ABCD9') r`,
  [opDraft.proposal_id, slackValidated.fingerprint])).r;
result = await publish(ATT1, opDraft.proposal_id, slackValidated.fingerprint, revalidation(slackValidated.fingerprint));
const slackVersion = await one(`select approved_by_kind, approved_by_ref from public.flow_definitions where proposal_id = $1`, [opDraft.proposal_id]);
const slackControl = await control(carrito);
check(slackApproved.ok && result.ok && slackVersion?.approved_by_kind === 'slack' && slackVersion.approved_by_ref === 'U0123ABCD9'
    && slackControl.changed_by_kind === 'slack', '5: un aprobador de Slack queda en la version y en el control', { slackApproved, result, slackVersion, slackControl });
// La pausa del agente queda registrada con su clase.
paused = (await one(`select public._flow_change_pause('lancemos', 'att1', 'att1', 'carrito', $1::bigint, 'automation_expert', $2, 'pausa pedida en la charla') r`,
  [slackControl.generation, AE])).r;
const aePause = await control(carrito);
check(paused.ok && paused.changed && aePause.changed_by_kind === 'automation_expert' && aePause.changed_by_ref === AE,
  '5: la pausa del Automation Expert queda con su clase y su ID', { paused, aePause });
await cycle(ATT1, [stateItem(carrito, 'att1', 'carrito', 'prender')], '5: sacar la pausa del agente');

// ===========================================================================
// 6. Los casos compartidos con el validador (invariantes 3 y 4).
// ===========================================================================
const shared = JSON.parse(readFileSync(join(FIXTURES, 'invariant_cases.json'), 'utf8'));
check(shared.cases.length === 7 && shared.instance.tenant_ref === ATT1.tenant_ref && shared.instance.ally_ref === ATT1.ally_ref,
  '6: los siete casos compartidos, sobre ATT1');
const SHARED = { tenant_ref: shared.instance.tenant_ref, ally_ref: shared.instance.ally_ref };
for (const item of shared.cases) {
  const flowId = (await register(SHARED, item.definition.producto, item.flujo)).flow_id;
  for (const previous of item.previous_versions) {
    await cycle(SHARED, [{ producto: previous.producto, flujo: item.flujo, flow_id: flowId, tipo: 'version', definicion: previous, en_curso: 'seguir' }],
      `6: ${item.id}: version previa`);
  }
  const prepared = await prepare(SHARED, [{ producto: item.definition.producto, flujo: item.flujo, flow_id: flowId, tipo: 'version', definicion: item.definition, en_curso: 'seguir' }]);
  check(prepared.approved?.ok, `6: ${item.id}: el borrador se guarda, se valida y se aprueba`, prepared);
  const versionsBefore = await countRows('flow_definitions', 'flow_id = $1', [flowId]);
  error = await attempt(PUBLISH_SQL, [json(SHARED), prepared.id, prepared.fingerprint, json(revalidation(prepared.fingerprint))]);
  const versionsAfter = await countRows('flow_definitions', 'flow_id = $1', [flowId]);
  if (item.base === null) {
    check(error === null && versionsAfter === versionsBefore + 1, `6: ${item.id}: la base lo publica`, error);
  } else {
    check(error?.code === item.base.sqlstate && error.message === item.base.error && versionsAfter === versionsBefore,
      `6: ${item.id}: la base lo rechaza con ${item.base.error} y no escribe nada`, error);
  }
}

// ===========================================================================
// 7. Las definiciones de ATT1 (el dato real del 2026-10-16). pytest las
//    traduce y las valida (tests/test_flow_validator_att1.py) y escribe
//    att1_definitions_20261016.json y att1_validation_result_20261016.json.
//    Aca se publican en una base limpia, con solo el binding de ATT1 (en la
//    principal ATT1 ya tiene carrito y pago fallido publicados): los cinco
//    flujos como primeras versiones de una sola propuesta, validada con el
//    resultado del validador de Python. La reactivacion, con horario:
//    siempre, se publica. Despues, una v2 del carrito con otra zona hace
//    fallar la publicacion entera.
// ===========================================================================
const att1Definitions = JSON.parse(readFileSync(join(FIXTURES, 'att1_definitions_20261016.json'), 'utf8'));
const att1Validation = JSON.parse(readFileSync(join(FIXTURES, 'att1_validation_result_20261016.json'), 'utf8'));
check(att1Validation.validator.contract === VALIDATOR.contract && att1Validation.validator.rules_version === VALIDATOR.rules_version
    && att1Validation.result.blocks.length === 0 && att1Validation.result.warnings.length === 3,
  '7: el resultado del validador de Python, sin bloqueos y con las tres advertencias', att1Validation);
const real = await newDatabase({ through: MIGRATIONS.length });
await seedBinding(real, BINDINGS[0]);
const REAL = { tenant_ref: att1Definitions.instance.tenant_ref, ally_ref: att1Definitions.instance.ally_ref };
const realOne = async (sql, params = []) => one(sql, params, real);
const realIds = [];
for (const definitionOf of att1Definitions.definitions) {
  const registered = await register(REAL, definitionOf.producto, definitionOf.flujo, real);
  check(registered?.ok, `7: se registra ${definitionOf.producto}/${definitionOf.flujo}`, registered);
  realIds.push(registered?.flow_id);
}
const realItem = (index, definicion) => ({
  producto: definicion.producto, flujo: definicion.flujo, flow_id: realIds[index], tipo: 'version', definicion, en_curso: 'seguir',
});
const realPrepare = async (items, result) => {
  try {
    return await realPrepareOrThrow(items, result);
  } catch (caught) {
    return { error: { code: caught.code, message: caught.message } };
  }
};
const realPrepareOrThrow = async (items, result) => {
  const flowIds = [...new Set(items.map((item) => item.flow_id))];
  const draft = (await realOne(`
    select coalesce(jsonb_object_agg(flow_id::text, jsonb_build_object('active_version', active_version)), '{}'::jsonb) b
      from public.flow_controls where flow_id = any($1::uuid[])`, [flowIds])).b;
  const saved = (await realOne(
    `select public.operator_save_flow_change_draft_v1($1::jsonb, null::uuid, null::int, $2::jsonb, $3::jsonb, $4, 'dan') r`,
    [json(REAL), json(items), json(draft), 'las definiciones de ATT1 del 16/10'])).r;
  if (!saved?.ok) return { saved };
  const base = (await realOne('select public._flow_store_base($1, $2, $3::uuid[]) b', [REAL.tenant_ref, REAL.ally_ref, flowIds])).b;
  const validated = (await realOne(RECORD_SQL,
    [json(REAL), saved.proposal_id, saved.revision, json(base), json(VALIDATOR), json(result), '{}'])).r;
  const approved = validated?.status === 'validada'
    ? (await realOne(`select public.operator_approve_flow_change_v1($1::jsonb, $2::uuid, $3, 'dan') r`,
      [json(REAL), saved.proposal_id, validated.fingerprint])).r
    : null;
  return { id: saved.proposal_id, saved, validated, approved, fingerprint: validated?.fingerprint };
};
const realPrepared = await realPrepare(att1Definitions.definitions.map((definicion, index) => realItem(index, definicion)),
  att1Validation.result);
check(realPrepared.saved?.ok && realPrepared.validated?.status === 'validada' && realPrepared.approved?.ok,
  '7: la propuesta de las cinco primeras versiones se guarda, se valida con el resultado de Python y se aprueba', realPrepared);
let realPublished = null;
try {
  realPublished = (await realOne(PUBLISH_SQL, [json(REAL), realPrepared.id, realPrepared.fingerprint,
    json(revalidation(realPrepared.fingerprint, { result: att1Validation.result, catalog_sha256: att1Validation.catalog_sha256 }))])).r;
} catch (caught) {
  realPublished = { error: { code: caught.code, message: caught.message } };
}
check(realPublished?.ok && realPublished.replayed === false && realPublished.published?.length === 5,
  '7: la base publica las cinco', realPublished);
const realStored = await query(`
  select c.flow_key, c.state, c.active_version, d.version, d.definition
    from public.flow_controls c join public.flow_definitions d on d.flow_id = c.flow_id
   order by c.flow_key`, [], real);
check(realStored.length === 5 && realStored.every((row) => row.version === 1 && row.active_version === 1 && row.state === 'apagado'),
  '7: cinco v1 activas, y los flujos siguen apagados', realStored.map((row) => [row.flow_key, row.state, row.active_version, row.version]));
for (const definitionOf of att1Definitions.definitions) {
  const stored = realStored.find((row) => row.flow_key === definitionOf.flujo);
  check(stored && sameJson(stored.definition, { ...definitionOf, version: 1, estado: 'publicada' }),
    `7: ${definitionOf.flujo}: lo publicado es la definicion de pytest, con version 1 y estado publicada`, stored?.definition);
}
check(realStored.find((row) => row.flow_key === 'reactivacion')?.definition.horario === 'siempre',
  '7: la reactivacion, con horario: siempre, se publica (correccion 1)');
// Una v2 del carrito con otra zona, junto con una v2 del pago fallido: la base
// la rechaza entera aunque el resultado diga que no hay bloqueos.
const realIndex = (flowKey) => att1Definitions.definitions.findIndex((definitionOf) => definitionOf.flujo === flowKey);
const realCart = att1Definitions.definitions[realIndex('carrito')];
const realPayment = att1Definitions.definitions[realIndex('pago_fallido')];
const zonePrepared = await realPrepare([
  realItem(realIndex('carrito'), { ...realCart, horario: { ...realCart.horario, zona: 'America/Bogota' } }),
  realItem(realIndex('pago_fallido'), { ...realPayment, topes: { ...realPayment.topes, por_dia: 90 } }),
], CLEAN);
check(zonePrepared.approved?.ok, '7: la v2 del carrito con otra zona se guarda, se valida y se aprueba', zonePrepared);
const realControlsBefore = await query('select flow_key, active_version, generation::int generation from public.flow_controls order by flow_key', [], real);
error = await attempt(PUBLISH_SQL, [json(REAL), zonePrepared.id, zonePrepared.fingerprint, json(revalidation(zonePrepared.fingerprint))], real);
const realControlsAfter = await query('select flow_key, active_version, generation::int generation from public.flow_controls order by flow_key', [], real);
const realVersions = (await realOne('select count(*)::int n from public.flow_definitions')).n;
check(error?.code === '23514' && error.message === 'flow_definition_zone_must_remain_constant'
    && realVersions === 5 && sameJson(realControlsBefore, realControlsAfter),
  '7: la v2 del carrito con otra zona hace fallar la publicacion entera, y no queda ninguna version nueva',
  { error, realVersions });
await real.close();

// ===========================================================================
// 8. Los permisos, como los usaria PostgREST.
// ===========================================================================
const OPERATOR_CALLS = [
  [`select public.operator_register_flow_v1($1::jsonb, 'att1', 'otro', 'dan', 'alta')`, [json(ATT1)]],
  [SAVE_SQL, [json(ATT1), null, null, json([carritoItem]), json(await draftBase([carrito])), PURPOSE]],
  [RECORD_SQL, [json(ATT1), guards.id, 1, '{}', json(VALIDATOR), json(CLEAN), '{}']],
  [`select public.operator_approve_flow_change_v1($1::jsonb, $2::uuid, $3, 'dan')`, [json(ATT1), guards.id, guards.fingerprint]],
  [PUBLISH_SQL, [json(ATT1), guards.id, guards.fingerprint, json(revalidation(guards.fingerprint))]],
  [`select public.operator_pause_flow_v1($1::jsonb, 'att1', 'carrito', 0, 'dan', 'pausa')`, [json(ATT1)]],
  [`select public.operator_discard_flow_change_v1($1::jsonb, $2::uuid, 'dan', 'no va')`, [json(ATT1), guards.id]],
];
check(OPERATOR_CALLS.length === OPERATOR_FUNCTIONS.length, '8: una llamada por cada funcion del operador');
const tableWrites = [
  `insert into public.flow_controls (tenant_ref, ally_ref, product_ref, flow_key, state, generation, change_kind, changed_by_kind,
    changed_by_ref, change_reason) values ('lancemos', 'att1', 'att1', 'colado', 'apagado', 0, 'registrado', 'operador', 'dan', 'alta')`,
  `insert into public.flow_change_events (tenant_ref, ally_ref, flow_id, event_type, actor_kind, actor_ref, reason_code)
    values ('lancemos', 'att1', gen_random_uuid(), 'flow_control_changed', 'operador', 'dan', 'x')`,
  'select count(*) from public.flow_definitions',
  'select count(*) from public.flow_change_proposals',
];
for (const role of ['service_role', 'anon', 'authenticated']) {
  for (const [sql, params] of OPERATOR_CALLS) {
    error = await asRole(role, () => attempt(sql, params));
    check(error?.code === '42501' && /permission denied for function operator_/.test(error.message),
      `8: ${role} no ejecuta ${sql.match(/operator_\w+/)[0]}`, error);
  }
  for (const sql of tableWrites) {
    error = await asRole(role, () => attempt(sql));
    check(error?.code === '42501' && /permission denied for table flow_/.test(error.message),
      `8: ${role} no toca la tabla directo: ${sql.slice(0, 40).replace(/\s+/g, ' ')}`, error);
  }
  error = await asRole(role, () => attempt(`select public._flow_store_base('lancemos', 'att1', '{}'::uuid[])`));
  check(error?.code === '42501', `8: ${role} no ejecuta los helpers`, error);
}

// Un EXECUTE concedido por error sobre una funcion del operador y sus helpers
// no alcanza para escribir: son security invoker y el rol no tiene permisos
// sobre las tablas. En una segunda base, para no ensuciar la primera.
const second = await newDatabase({ through: MIGRATIONS.length });
await seedBinding(second, BINDINGS[0]);
await second.exec(`
  grant execute on function public.operator_register_flow_v1(jsonb,text,text,text,text) to service_role;
  grant execute on function public._flow_store_instance(jsonb) to service_role;
  grant execute on function public._flow_store_product_is_bound(text,text,text) to service_role;
  grant execute on function public._flow_store_actor_is_valid(text,text) to service_role;
  grant execute on function public._flow_store_free_text_is_valid(text) to service_role;
`);
error = await asRole('service_role', () => attempt(
  `select public.operator_register_flow_v1($1::jsonb, 'att1', 'carrito', 'dan', 'alta')`, [json(ATT1)], second), second);
check(error?.code === '42501' && error.message === 'permission denied for table flow_controls',
  '8: con un EXECUTE equivocado, la llamada da permission denied for table flow_controls', error);
check((await one('select count(*)::int n from public.flow_controls', [], second)).n === 0, '8: y no escribio nada');
const ownerCall = (await one(`select public.operator_register_flow_v1($1::jsonb, 'att1', 'carrito', 'dan', 'alta') r`, [json(ATT1)], second)).r;
check(ownerCall.ok, '8: el duenio si registra en la segunda base', ownerCall);
await second.close();

// ===========================================================================
// 9. La base de un mundo de prueba, para FlowWorld.base_for.
// ===========================================================================
const WORLD_FLOWS = [
  { instance: MUNDO, flow_id: '10000000-0000-4000-8000-000000000001', producto: 'mundo', flujo: 'carrito' },
  { instance: MUNDO, flow_id: '10000000-0000-4000-8000-000000000002', producto: 'mundo', flujo: 'pago_fallido' },
  { instance: MUNDO, flow_id: '10000000-0000-4000-8000-000000000003', producto: 'mundo', flujo: 'reactivacion' },
  { instance: MUNDO, flow_id: '10000000-0000-4000-8000-000000000004', producto: 'mundo-dos', flujo: 'carrito' },
  { instance: VECINA, flow_id: '20000000-0000-4000-8000-000000000001', producto: 'vecina', flujo: 'carrito' },
];
for (const flow of WORLD_FLOWS) {
  error = await attempt(`insert into public.flow_controls (flow_id, tenant_ref, ally_ref, product_ref, flow_key, state, generation,
    change_kind, changed_by_kind, changed_by_ref, change_reason)
    values ($1::uuid, $2, $3, $4, $5, 'apagado', 0, 'registrado', 'operador', 'dan', 'alta del mundo de prueba')`,
  [flow.flow_id, flow.instance.tenant_ref, flow.instance.ally_ref, flow.producto, flow.flujo]);
  check(error === null, `9: el control ${flow.producto}/${flow.flujo} con su flow_id fijo`, error);
}
const [mundoCarrito, mundoPago, mundoReactivacion, mundoDos, vecinaCarrito] = WORLD_FLOWS;
const worldItem = (flow, overrides) => versionItem(flow.flow_id, flow.producto, flow.flujo, overrides);
await cycle(MUNDO, [worldItem(mundoCarrito), stateItem(mundoCarrito.flow_id, 'mundo', 'carrito', 'prender')], '9: la v1 del carrito, prendido');
await cycle(MUNDO, [worldItem(mundoCarrito, { topes: { bolsa: 'mundo-carrito', por_dia: 90, total: 1000,
  repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } } })], '9: la v2 del carrito');
await cycle(MUNDO, [worldItem(mundoPago, { nombre: 'Pago fallido', dispara: { evento: 'pago_fallido' } })], '9: la v1 del pago fallido, apagado');
await cycle(MUNDO, [worldItem(mundoDos), stateItem(mundoDos.flow_id, 'mundo-dos', 'carrito', 'prender')], '9: la v1 del carrito del segundo producto');
paused = await pause(MUNDO, 'mundo-dos', 'carrito', (await control(mundoDos.flow_id)).generation);
check(paused.ok && paused.changed, '9: el carrito del segundo producto, pausado', paused);
await cycle(VECINA, [worldItem(vecinaCarrito)], '9: la v1 del carrito de la otra instancia');

const worldOf = async (instance) => (await one(`
  select coalesce(jsonb_agg(jsonb_build_object(
           'flow_id', c.flow_id, 'tenant_ref', c.tenant_ref, 'ally_ref', c.ally_ref,
           'product_ref', c.product_ref, 'flow_key', c.flow_key, 'state', c.state,
           'active_version', c.active_version, 'generation', c.generation,
           'versions', coalesce((select jsonb_agg(jsonb_build_object('version', d.version, 'definition', d.definition)
                                                  order by d.version)
                                   from public.flow_definitions d where d.flow_id = c.flow_id), '[]'::jsonb))
           order by c.flow_id), '[]'::jsonb) w
    from public.flow_controls c
   where c.tenant_ref = $1 and c.ally_ref = $2`, [instance.tenant_ref, instance.ally_ref])).w;
const baseCases = [
  { items: [mundoCarrito], label: 'una propuesta sobre el carrito' },
  { items: [mundoCarrito, mundoReactivacion], label: 'una propuesta sobre el carrito y un flujo sin version' },
  { items: [mundoDos], label: 'una propuesta sobre el carrito del segundo producto' },
];
const example = {
  _captured: {
    origin: 'tests/sql/followup_engine/validate_flow_store.mjs, paso 9: _flow_store_base sobre la cadena completa de migraciones',
    captured_at: new Date().toISOString().slice(0, 10),
  },
  instance: { tenant_ref: MUNDO.tenant_ref, ally_ref: MUNDO.ally_ref, products: ['mundo', 'mundo-dos'] },
  flows: await worldOf(MUNDO),
  flows_of_other_instances: await worldOf(VECINA),
  bases: [],
};
for (const baseCase of baseCases) {
  const itemFlowIds = baseCase.items.map((flow) => flow.flow_id);
  example.bases.push({ description: baseCase.label, item_flow_ids: itemFlowIds, base: await instanceBase(MUNDO, itemFlowIds) });
}
check(example.flows.length === 4 && example.flows_of_other_instances.length === 1, '9: el mundo tiene cuatro flujos y uno ajeno');
const expectedStates = example.flows.map((flow) => `${flow.product_ref}/${flow.flow_key}:${flow.state}:${flow.active_version}:${flow.generation}`);
check(sameJson(expectedStates, [
  'mundo/carrito:prendido:2:2', 'mundo/pago_fallido:apagado:1:1', 'mundo/reactivacion:apagado:null:0', 'mundo-dos/carrito:pausado:1:2',
]), '9: los controles del mundo', expectedStates);
check(sameJson(example.bases[0].base, {
  [mundoCarrito.flow_id]: { generation: 2, active_version: 2 },
  [mundoPago.flow_id]: { active_version: 1 },
  [mundoReactivacion.flow_id]: { active_version: null },
  [mundoDos.flow_id]: { active_version: 1 },
}), '9: la base de una propuesta sobre el carrito', example.bases[0].base);
check(example.bases.every((item) => !(vecinaCarrito.flow_id in item.base)), '9: ninguna base tiene el flujo de la otra instancia');

const examplePath = join(FIXTURES, 'base_example.json');
const withoutCapture = (value) => JSON.stringify({ ...value, _captured: undefined });
const fixtureDir = process.env.FLOW_STORE_FIXTURE_DIR;
if (fixtureDir) {
  writeFileSync(join(fixtureDir, 'base_example.json'), `${JSON.stringify(example, null, 1)}\n`);
  console.log(`flow_store_fixture_written=${join(fixtureDir, 'base_example.json')}`);
} else {
  const inRepo = JSON.parse(readFileSync(examplePath, 'utf8'));
  check(withoutCapture(inRepo) === withoutCapture(example),
    '9: tests/fixtures/flow_store/base_example.json es identico a la base de hoy (regenerarlo con FLOW_STORE_FIXTURE_DIR)');
}

await db.close();
console.log(JSON.stringify({
  checks,
  failures: failures.length,
  functions: NEW_FUNCTIONS.length,
  triggers: NEW_TRIGGERS.length,
  acl_inventory_functions: aclAfter.length,
  shared_cases: shared.cases.length,
  att1_definitions: att1Definitions.definitions.length,
}));
if (failures.length) {
  console.log(`FLOW_STORE=FAIL (${failures.length})`);
  process.exit(1);
}
console.log('FLOW_STORE=OK');
