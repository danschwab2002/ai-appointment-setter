import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const migrationDir = join(root, 'supabase/migrations');
const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
`);

const files = [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(migrationDir)
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => join(root, 'supabase/migrations', name)),
];
for (const file of files) {
  await db.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}

const functionName = 'public.get_precheckout_delayed_first_touch_readiness()';
const acl = await db.query(`
  select
    has_function_privilege('anon', $1, 'EXECUTE') as anon_execute,
    has_function_privilege('authenticated', $1, 'EXECUTE') as authenticated_execute,
    has_function_privilege('service_role', $1, 'EXECUTE') as service_execute
`, [functionName]);
if (acl.rows[0]?.anon_execute !== false
    || acl.rows[0]?.authenticated_execute !== false
    || acl.rows[0]?.service_execute !== true) {
  throw new Error(`readiness ACL mismatch: ${JSON.stringify(acl.rows)}`);
}

async function expectDenied(role) {
  await db.exec(`set role ${role}`);
  try {
    await db.query('select * from public.get_precheckout_delayed_first_touch_readiness()');
  } catch (error) {
    if (error?.code === '42501') return;
    throw error;
  } finally {
    await db.exec('reset role');
  }
  throw new Error(`${role} unexpectedly executed readiness RPC`);
}
await expectDenied('anon');
await expectDenied('authenticated');

await db.exec('set role service_role');
const initial = await db.query(
  'select * from public.get_precheckout_delayed_first_touch_readiness()',
);
await db.exec('reset role');
const initialRow = initial.rows[0];
if (initial.rows.length !== 1
    || initialRow?.migration_tracking_complete !== false
    || initialRow?.scope_configured !== true
    || initialRow?.runtime_state !== 'inactive'
    || initialRow?.runtime_generation !== 0
    || initialRow?.timer_binding_enabled !== true
    || initialRow?.first_touch_binding_enabled !== false
    || initialRow?.due_count !== 0
    || initialRow?.reserved_count !== 0
    || initialRow?.request_started_count !== 0
    || initialRow?.delivery_unknown_count !== 0
    || initialRow?.reason_code !== 'migration_tracking_incomplete') {
  throw new Error(`initial readiness mismatch: ${JSON.stringify(initial.rows)}`);
}

await db.exec(`
  create schema supabase_migrations;
  create table supabase_migrations.schema_migrations (
    version text primary key
  );
  insert into supabase_migrations.schema_migrations (version) values
    ('20260829000200'),
    ('20260829000300'),
    ('20260829000400'),
    ('20260829000500');
`);
await db.exec('set role service_role');
const tracked = await db.query(
  'select * from public.get_precheckout_delayed_first_touch_readiness()',
);
await db.exec('reset role');
if (tracked.rows[0]?.migration_tracking_complete !== true
    || tracked.rows[0]?.reason_code !== 'first_touch_binding_disabled') {
  throw new Error(`tracked default-off mismatch: ${JSON.stringify(tracked.rows)}`);
}

await db.exec(`
  update public.hotmart_abandonment_timer_policy_bindings
  set precheckout_first_touch_enabled = true,
      generation = generation + 1
  where tenant_ref = 'lancemos'
    and funnel_ref = 'psicologajohanna'
    and lower(product_ref) = lower('F106691755G')
    and offer_ref = 'bxjge6zq';
`);
await db.exec('set role service_role');
const ready = await db.query(
  'select * from public.get_precheckout_delayed_first_touch_readiness()',
);
await db.exec('reset role');
const readyRow = ready.rows[0];
if (readyRow?.reason_code !== 'precheckout_first_touch_ready'
    || readyRow?.timer_binding_generation !== 2
    || readyRow?.first_touch_binding_enabled !== true
    || readyRow?.due_count !== 0
    || readyRow?.reserved_count !== 0
    || readyRow?.request_started_count !== 0
    || readyRow?.delivery_unknown_count !== 0) {
  throw new Error(`activated readiness mismatch: ${JSON.stringify(ready.rows)}`);
}

await db.exec('begin');
await db.exec(`
  update public.pilot_runtime_controls
  set runtime_state = 'armed', generation = generation + 1
  where scope_key = 'johanna-precheckout-delayed-first-touch'
    and scope_version = 1
`);
await db.exec('set local role service_role');
const wrongRuntime = await db.query(
  'select * from public.get_precheckout_delayed_first_touch_readiness()',
);
await db.exec('rollback');
if (wrongRuntime.rows[0]?.reason_code !== 'precheckout_runtime_not_inactive') {
  throw new Error(`runtime mismatch was not closed: ${JSON.stringify(wrongRuntime.rows)}`);
}

console.log('PRECHECKOUT_PRODUCTION_READINESS_SQL_OK');
await db.close();

const cumulativeDb = new PGlite();
await cumulativeDb.waitReady;
await cumulativeDb.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
`);
const cumulativeFiles = [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(migrationDir)
    .filter((name) => name.endsWith('.sql') && name < '20260829000500')
    .sort()
    .map((name) => join(migrationDir, name)),
];
for (const file of cumulativeFiles) {
  await cumulativeDb.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}
await cumulativeDb.exec(`
  insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
  )
  select
    'lancemos-johanna-abandonment-reevaluation', 1, 'published',
    purpose, timezone, business_windows, interval '5 minutes',
    expires_after, max_automatic_messages, steps,
    'fixture', clock_timestamp(), clock_timestamp()
  from public.followup_policy_versions
  where policy_key = 'johanna-abandonment-single-touch-e2e'
    and version = 2;

  insert into public.hotmart_abandonment_timer_policy_bindings (
    tenant_ref, funnel_ref, product_ref, offer_ref, enabled,
    precheckout_first_touch_enabled, policy_key, policy_version
  ) values (
    'lancemos', 'psicologajohanna', 'F106691755G', 'bxjge6zq', true,
    false, 'lancemos-johanna-abandonment-reevaluation', 1
  );
`);
await cumulativeDb.exec(readFileSync(
  join(migrationDir, '20260829000500_precheckout_production_readiness.sql'),
  'utf8',
));
const migratedBinding = await cumulativeDb.query(`
  select binding.policy_key, binding.policy_version, binding.generation,
         binding.precheckout_first_touch_enabled, policy.grace_period::text as delay
  from public.hotmart_abandonment_timer_policy_bindings binding
  join public.followup_policy_versions policy
    on policy.policy_key = binding.policy_key
   and policy.version = binding.policy_version
  where binding.tenant_ref = 'lancemos'
    and binding.funnel_ref = 'psicologajohanna'
    and lower(binding.product_ref) = lower('F106691755G')
    and binding.offer_ref = 'bxjge6zq'
`);
const migratedBindingRow = migratedBinding.rows[0];
if (migratedBindingRow?.policy_key !== 'johanna-precheckout-delayed-first-touch-timer'
    || migratedBindingRow?.policy_version !== 1
    || migratedBindingRow?.generation !== 2
    || migratedBindingRow?.precheckout_first_touch_enabled !== false
    || migratedBindingRow?.delay !== '01:00:00') {
  throw new Error(`legacy binding was not migrated: ${JSON.stringify(migratedBinding.rows)}`);
}

await cumulativeDb.exec(`
  update public.hotmart_abandonment_timer_policy_bindings
  set policy_key = 'lancemos-johanna-abandonment-reevaluation',
      policy_version = 1,
      precheckout_first_touch_enabled = true,
      generation = generation + 1
  where tenant_ref = 'lancemos'
    and funnel_ref = 'psicologajohanna'
    and lower(product_ref) = lower('F106691755G')
    and offer_ref = 'bxjge6zq';

  create schema supabase_migrations;
  create table supabase_migrations.schema_migrations (version text primary key);
  insert into supabase_migrations.schema_migrations (version) values
    ('20260829000200'), ('20260829000300'),
    ('20260829000400'), ('20260829000500');
`);
await cumulativeDb.exec('set role service_role');
const wrongPolicy = await cumulativeDb.query(
  'select * from public.get_precheckout_delayed_first_touch_readiness()',
);
await cumulativeDb.exec('reset role');
if (wrongPolicy.rows[0]?.reason_code !== 'timer_binding_policy_mismatch') {
  throw new Error(`wrong timer policy was not closed: ${JSON.stringify(wrongPolicy.rows)}`);
}
console.log('PRECHECKOUT_PRODUCTION_READINESS_LEGACY_BINDING_OK');
await cumulativeDb.close();
