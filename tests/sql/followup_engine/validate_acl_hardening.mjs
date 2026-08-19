import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
`);

const baseline = join(root, 'supabase/baseline/20260803_public_schema.sql');
const migrations = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort()
  .map((name) => join(root, 'supabase/migrations', name));
const hardeningIndex = migrations.findIndex(
  (file) => file.endsWith('20260812000100_supabase_function_acl_hardening.sql'),
);
if (hardeningIndex < 0) {
  throw new Error('ACL hardening migration is missing from the canonical stack');
}
const hardening = migrations[hardeningIndex];
const beforeHardening = [baseline, ...migrations.slice(0, hardeningIndex)];
const afterHardening = migrations.slice(hardeningIndex + 1);
for (const file of beforeHardening) {
  await db.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}
const before = await db.query(`
  select count(*)::integer leaks
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and (
      has_function_privilege('anon', p.oid, 'execute')
      or has_function_privilege('authenticated', p.oid, 'execute')
      or (p.prorettype = 'trigger'::regtype
          and has_function_privilege('service_role', p.oid, 'execute'))
    )
`);
if (before.rows[0]?.leaks <= 0) {
  throw new Error('positive control failed: Supabase-style defaults produced no leaks');
}
await db.exec(readFileSync(hardening, 'utf8'));
for (const file of afterHardening) {
  await db.exec(readFileSync(file, 'utf8'));
}

const rows = await db.query(`
  with expected(signature) as (
    values
      ('activate_lancemos_pilot_scope_version(text,integer,bigint,text,text)'),
      ('admit_hotmart_cart_abandonment(text,jsonb)'),
      ('admit_hotmart_purchase_approved(text,jsonb)'),
      ('admit_observed_lead_precheckout(text,jsonb,jsonb)'),
      ('admit_precheckout_form_submission(text,jsonb,jsonb)'),
      ('begin_precheckout_test_first_touch(text,uuid,text,bigint,bigint)'),
      ('admit_inbound_commercial_case(text,integer,bigint,text)'),
      ('apply_chatwoot_inbound_opt_out(bigint,bigint,bigint,bigint,text,timestamp with time zone,text)'),
      ('apply_hotmart_purchase_approved(uuid,text,text,text,text,text,timestamp with time zone)'),
      ('claim_chatwoot_opt_out_projections(text,timestamp with time zone,interval,integer)'),
      ('claim_due_followup_actions(text,timestamp with time zone,interval,integer)'),
      ('claim_human_handoff_projection_effects(text,integer,integer,timestamp with time zone)'),
      ('evaluate_lancemos_pilot_scope(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid)'),
      ('finish_precheckout_test_first_touch(uuid,text,bigint,bigint,text)'),
      ('finalize_chatwoot_opt_out_projection(uuid,text,bigint,boolean,text,integer,timestamp with time zone)'),
      ('finalize_followup_delivery_attempt(uuid,uuid,text,bigint,text,text,uuid,text,timestamp with time zone,timestamp with time zone,timestamp with time zone)'),
      ('finalize_human_handoff_projection_effect(uuid,text,bigint,text,text,timestamp with time zone,timestamp with time zone)'),
      ('get_followup_chatwoot_context(uuid,text,bigint,timestamp with time zone)'),
      ('get_followup_execution_context(uuid,text,bigint,timestamp with time zone)'),
      ('get_human_handoff_projection_status()'),
      ('get_lancemos_pilot_runtime_status(text,integer,text,text,text)'),
      ('has_chatwoot_opt_out_stop(bigint,bigint,bigint,text)'),
      ('mark_lancemos_pilot_request_started(uuid,uuid,text,bigint,timestamp with time zone)'),
      ('plan_lancemos_pilot_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamp with time zone,bigint,bigint,text,text,integer)'),
      ('reconcile_chatwoot_opt_out_stop(bigint,bigint,bigint,text)'),
      ('reconcile_followup_delivery_attempt(uuid,uuid,bigint,text,text,uuid,timestamp with time zone,text,timestamp with time zone)'),
      ('record_and_finalize_followup_acceptance(uuid,uuid,text,bigint,text,text,text,timestamp with time zone)'),
      ('reevaluate_followup_action(uuid,text,bigint,timestamp with time zone,boolean,text,text,timestamp with time zone,text,boolean,boolean,boolean,boolean,boolean)'),
      ('request_human_handoff(uuid,text,text,text,text,integer,uuid,uuid,text,bigint,timestamp with time zone)'),
      ('reserve_followup_delivery_attempt(uuid,text,bigint,bigint,bigint,text,text,timestamp with time zone)'),
      ('set_lancemos_pilot_cohort_member(text,integer,uuid,bigint,text,text,text)'),
      ('set_lancemos_pilot_runtime_state(text,integer,bigint,text,text,text)')
  ), functions as (
    select p.oid, p.prorettype::regtype::text result_type,
           p.oid::regprocedure::text signature,
           has_function_privilege('anon', p.oid, 'execute') anon_x,
           has_function_privilege('authenticated', p.oid, 'execute') auth_x,
           has_function_privilege('service_role', p.oid, 'execute') service_x
    from pg_proc p
    where p.pronamespace = 'public'::regnamespace
  )
  select
    count(*)::integer total,
    count(*) filter (where anon_x or auth_x)::integer api_leaks,
    count(*) filter (where result_type = 'trigger' and service_x)::integer trigger_leaks,
    count(*) filter (where service_x is distinct from (expected.signature is not null))::integer allowlist_mismatches,
    count(*) filter (where expected.signature is not null)::integer expected_count
  from functions
  left join expected using (signature)
`);
const result = rows.rows[0];
if (result.api_leaks !== 0 || result.trigger_leaks !== 0
    || result.allowlist_mismatches !== 0 || result.expected_count !== 32) {
  throw new Error(`ACL hardening failed: ${JSON.stringify(result)}`);
}
const inventory = await db.query(readFileSync(
  join(root, 'scripts/supabase_acl_inventory.sql'),
  'utf8',
));
const inventoryExpected = inventory.rows.filter(
  (row) => row.expected_service_role_execute === true,
).length;
if (inventory.rows.length !== result.total
    || inventoryExpected !== result.expected_count
    || inventory.rows.some((row) => row.acl_status !== 'ok')) {
  throw new Error('checked-in ACL inventory disagrees with clean-stack probe');
}

const purchaseWorkerAclRepair = migrations.find(
  (file) => file.endsWith('20260814000100_hotmart_purchase_worker_table_acl.sql'),
);
if (purchaseWorkerAclRepair === undefined) {
  throw new Error('purchase worker table ACL repair migration is missing');
}
const purchaseWorkerAclSql = readFileSync(purchaseWorkerAclRepair, 'utf8');
if (!/alter\s+function\s+public\.apply_hotmart_purchase_approved[\s\S]*security\s+definer/iu.test(
  purchaseWorkerAclSql,
) || !/revoke[\s\S]*update[\s\S]*on\s+(table\s+)?public\.followup_delivery_attempts[\s\S]*from\s+service_role/iu.test(
  purchaseWorkerAclSql,
) || /grant\s+(all|update)/iu.test(purchaseWorkerAclSql)) {
  throw new Error('purchase worker ACL repair must use a definer RPC without direct UPDATE');
}
const purchaseWorkerSearchPathRepair = migrations.find(
  (file) => file.endsWith('20260814000150_hotmart_purchase_worker_search_path.sql'),
);
if (purchaseWorkerSearchPathRepair === undefined) {
  throw new Error('purchase worker search_path repair migration is missing');
}
const purchaseWorkerSearchPathSql = readFileSync(purchaseWorkerSearchPathRepair, 'utf8');
if (!/alter\s+function\s+public\.apply_hotmart_purchase_approved[\s\S]*set\s+search_path\s*=\s*pg_catalog\s*,\s*public\s*,\s*pg_temp/iu.test(
  purchaseWorkerSearchPathSql,
)) {
  throw new Error('purchase worker search_path repair must pin pg_catalog, public, pg_temp');
}
const purchaseWorkerBoundary = await db.query(`
  select
    has_table_privilege(
      'service_role',
      'public.followup_delivery_attempts',
      'update'
    ) direct_update,
    has_function_privilege(
      'service_role',
      'public.apply_hotmart_purchase_approved(
        uuid,text,text,text,text,text,timestamp with time zone
      )',
      'execute'
    ) rpc_execute,
    p.prosecdef security_definer,
    array_to_string(p.proconfig, ',') function_config
  from pg_proc p
  where p.oid = 'public.apply_hotmart_purchase_approved(
    uuid,text,text,text,text,text,timestamp with time zone
  )'::regprocedure
`);
const purchaseBoundary = purchaseWorkerBoundary.rows[0];
if (purchaseBoundary?.direct_update !== false
    || purchaseBoundary?.rpc_execute !== true
    || purchaseBoundary?.security_definer !== true
    || purchaseBoundary?.function_config !== 'search_path=pg_catalog, public, pg_temp') {
  throw new Error(`purchase worker ACL boundary is unsafe: ${JSON.stringify(purchaseBoundary)}`);
}
let directUpdateBlocked = false;
try {
  await db.exec(`
    set role service_role;
    update public.followup_delivery_attempts set updated_at = updated_at where false;
    reset role;
  `);
} catch (error) {
  directUpdateBlocked = String(error).includes('permission denied');
  await db.exec('reset role');
}
if (!directUpdateBlocked) throw new Error('service_role direct UPDATE was not blocked');
console.log(`acl_hardening=OK positive_control_leaks=${before.rows[0].leaks} public_functions=${result.total} service_entrypoints=${result.expected_count}`);
await db.close();
