import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { PGlite } from '@electric-sql/pglite';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const paths = [
  'supabase/baseline/20260803_public_schema.sql',
  'supabase/migrations/20260803000100_followup_engine_v1.sql',
  'supabase/migrations/20260804000200_followup_identity_binding.sql',
  'supabase/migrations/20260805000100_followup_identity_audit.sql',
  'supabase/migrations/20260805000200_followup_contact_authorization_grant.sql',
  'supabase/migrations/20260805000300_per_case_conversation_anchor.sql',
  'supabase/migrations/20260813000100_absolute_followup_deadlines.sql',
];
const commercialCaseMigrationPath =
  'supabase/migrations/20260816000100_commercial_case_root.sql';

const db = new PGlite();
await db.waitReady;
for (const [index, path] of paths.entries()) {
  let sql = await readFile(`${root}/${path}`, 'utf8');
  if (index === 0) {
    sql = sql.replace(
      'create extension if not exists pgcrypto;',
      '-- omitted in PGlite: extension unavailable',
    );
  }
  await db.exec(sql);
}

await db.exec(`
  insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
  ) values (
    'commercial-root-probe', 1, 'published', 'cart_recovery', 'UTC',
    '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
    interval '1 hour', interval '7 days', 1,
    '[{"step_key":"first_contact","mode":"freeform"}]'::jsonb,
    'schema-probe', now(), now()
  );
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values (
    '10000000-0000-0000-0000-000000000001', 'hotmart',
    'commercial-root-event', 'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
  insert into public.contacts (id, full_name) values (
    '10000000-0000-0000-0000-000000000002', 'Commercial Root Probe'
  );
`);

const plan = await db.query(`
  select * from public.plan_cart_recovery(
    '10000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000002',
    'product-root', 'Product Root', 'offer-root',
    'commercial-root-probe', 1, now()
  )
`);
const recoveryCaseId = plan.rows[0]?.recovery_case_id;
if (!recoveryCaseId) throw new Error('recovery case was not created');

await db.exec(`
  create role anon noinherit;
  create role authenticated noinherit;
  create role service_role noinherit bypassrls;
  grant usage on schema public to service_role;
  grant select, update on public.recovery_cases to service_role;
`);
await db.exec(await readFile(`${root}/${commercialCaseMigrationPath}`, 'utf8'));
console.log('commercial_case_root_migration_apply=OK');

const rootRow = await db.query(`
  select rc.id = cc.id as shared_id,
         rc.commercial_case_id = cc.id as child_binding,
         cc.case_kind,
         cc.authority_mode,
         cc.status,
         cc.automation_status,
         cc.product_ref,
         cc.offer_ref
  from public.recovery_cases rc
  join public.commercial_cases cc on cc.id = rc.commercial_case_id
  where rc.id = $1
`, [recoveryCaseId]);
const row = rootRow.rows[0];
if (!row?.shared_id || !row?.child_binding
    || row.case_kind !== 'cart_recovery'
    || row.authority_mode !== 'shadow'
    || row.status !== 'active'
    || row.automation_status !== 'enabled'
    || row.product_ref !== 'product-root'
    || row.offer_ref !== 'offer-root') {
  throw new Error('existing recovery did not receive an exact shadow parent');
}
console.log('commercial_case_existing_recovery_backfill=OK');

await db.exec(`
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values (
    '10000000-0000-0000-0000-000000000004', 'hotmart',
    'commercial-root-new-event', 'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
`);
const newPlan = await db.query(`
  select * from public.plan_cart_recovery_with_identity(
    '10000000-0000-0000-0000-000000000004',
    '10000000-0000-0000-0000-000000000002',
    'product-root-new', 'Product Root New', 'offer-root-new',
    'commercial-root-probe', 1, now(),
    1, 7, '5531999999999'
  )
`);
const newRecoveryCaseId = newPlan.rows[0]?.recovery_case_id;
const newRoot = await db.query(`
  select rc.commercial_case_id = rc.id as child_binding,
         cc.case_kind, cc.product_ref, cc.offer_ref,
         cc.identity_resolution_status
  from public.recovery_cases rc
  join public.commercial_cases cc on cc.id = rc.commercial_case_id
  where rc.id = $1
`, [newRecoveryCaseId]);
if (!newRoot.rows[0]?.child_binding
    || newRoot.rows[0]?.case_kind !== 'cart_recovery'
    || newRoot.rows[0]?.product_ref !== 'product-root-new'
    || newRoot.rows[0]?.offer_ref !== 'offer-root-new'
    || newRoot.rows[0]?.identity_resolution_status !== 'resolved') {
  throw new Error('new recovery did not receive an exact shadow parent');
}
console.log('commercial_case_new_recovery_shadow=OK');

await db.query(`
  update public.recovery_cases
  set status = 'paused', version = version + 1
  where id = $1
`, [newRecoveryCaseId]);
const paused = await db.query(`
  select status, automation_status, version
  from public.commercial_cases where id = $1
`, [newRecoveryCaseId]);
if (paused.rows[0]?.status !== 'paused'
    || paused.rows[0]?.automation_status !== 'paused'
    || paused.rows[0]?.version !== 2) {
  throw new Error('recovery update did not synchronize the shadow parent');
}
console.log('commercial_case_shadow_sync=OK');

let mismatchRejected = false;
try {
  await db.query(`
    update public.commercial_cases
    set product_ref = 'forged-product'
    where id = $1
  `, [newRecoveryCaseId]);
} catch (error) {
  mismatchRejected = String(error?.message ?? error).includes(
    'commercial_case_root_mismatch',
  );
}
if (!mismatchRejected) throw new Error('direct shadow mismatch was accepted');
console.log('commercial_case_shadow_mismatch_rejected=OK');

let inboundRejected = false;
try {
  await db.exec(`
    insert into public.commercial_cases (
      id, case_kind, contact_id, product_ref, status, automation_status
    ) values (
      '10000000-0000-0000-0000-000000000003',
      'inbound_sales',
      '10000000-0000-0000-0000-000000000002',
      'product-root',
      'active',
      'draft_only'
    )
  `);
} catch (error) {
  inboundRejected = String(error?.message ?? error).includes(
    'commercial_case_kind_not_enabled',
  );
}
if (!inboundRejected) throw new Error('Cut A admitted an inbound case directly');
console.log('commercial_case_inbound_default_off=OK');

let paymentFailureRejected = false;
try {
  await db.exec(`
    insert into public.commercial_cases (
      id, case_kind, contact_id, product_ref, status, automation_status
    ) values (
      '10000000-0000-0000-0000-000000000007',
      'payment_failure',
      '10000000-0000-0000-0000-000000000002',
      'product-root', 'active', 'draft_only'
    )
  `);
} catch (error) {
  paymentFailureRejected = String(error?.message ?? error).includes(
    'commercial_case_kind_not_enabled',
  );
}
if (!paymentFailureRejected) {
  throw new Error('Cut A admitted a payment-failure case directly');
}
console.log('commercial_case_payment_failure_default_off=OK');

for (const timestampColumn of ['created_at', 'updated_at']) {
  let timestampMutationRejected = false;
  try {
    await db.query(`
      update public.commercial_cases
      set ${timestampColumn} = ${timestampColumn} - interval '1 day'
      where id = $1
    `, [newRecoveryCaseId]);
  } catch (error) {
    timestampMutationRejected = String(error?.message ?? error).includes(
      'commercial_case_root_mismatch',
    );
  }
  if (!timestampMutationRejected) {
    throw new Error(`direct ${timestampColumn} divergence was accepted`);
  }
}
console.log('commercial_case_timestamp_divergence_rejected=OK');

let serviceRoleDirectShadowRejected = false;
await db.exec('set role service_role');
try {
  await db.query(`
    update public.commercial_cases set updated_at = updated_at where id = $1
  `, [newRecoveryCaseId]);
} catch (error) {
  serviceRoleDirectShadowRejected = String(error?.message ?? error).includes(
    'permission denied',
  );
} finally {
  await db.exec('reset role');
}
if (!serviceRoleDirectShadowRejected) {
  throw new Error('service_role could mutate the shadow directly');
}

await db.exec('set role service_role');
let serviceRoleRecoveryWriteError;
try {
  await db.query(`
    update public.recovery_cases
    set created_at = created_at - interval '1 second'
    where id = $1
  `, [newRecoveryCaseId]);
} catch (error) {
  serviceRoleRecoveryWriteError = error;
} finally {
  await db.exec('reset role');
}
if (serviceRoleRecoveryWriteError) {
  throw new Error(
    `service_role recovery write was broken: ${serviceRoleRecoveryWriteError.message}`,
  );
}
const serviceRoleProgress = await db.query(`
  select rc.created_at = cc.created_at as created_at_synced
  from public.recovery_cases rc
  join public.commercial_cases cc on cc.id = rc.commercial_case_id
  where rc.id = $1
`, [newRecoveryCaseId]);
if (!serviceRoleProgress.rows[0]?.created_at_synced) {
  throw new Error('service_role recovery write did not synchronize the shadow');
}
console.log('commercial_case_service_role_internal_progress=OK');

let directDeleteRejected = false;
try {
  await db.query(
    'delete from public.commercial_cases where id = $1',
    [newRecoveryCaseId],
  );
} catch (error) {
  directDeleteRejected = String(error?.message ?? error).includes(
    'commercial_case_delete_requires_recovery_delete',
  );
}
if (!directDeleteRejected) throw new Error('direct shadow delete was accepted');
console.log('commercial_case_direct_delete_rejected=OK');

await db.exec(`
  create table public.commercial_case_nested_delete_probe (case_id uuid not null);
  create function public.run_commercial_case_nested_delete_probe()
  returns trigger
  language plpgsql
  set search_path = public, pg_temp
  as $probe$
  begin
    delete from public.commercial_cases where id = new.case_id;
    return new;
  end;
  $probe$;
  create trigger commercial_case_nested_delete_probe_run
  after insert on public.commercial_case_nested_delete_probe
  for each row execute function public.run_commercial_case_nested_delete_probe();
`);
let nestedDeleteRejected = false;
try {
  await db.query(
    'insert into public.commercial_case_nested_delete_probe(case_id) values ($1)',
    [newRecoveryCaseId],
  );
} catch (error) {
  nestedDeleteRejected = String(error?.message ?? error).includes(
    'commercial_case_delete_requires_recovery_delete',
  );
}
if (!nestedDeleteRejected) throw new Error('nested shadow delete was accepted');
const nestedDeleteState = await db.query(`
  select
    (select count(*)::int from public.recovery_cases where id = $1) as recovery_count,
    (select count(*)::int from public.commercial_cases where id = $1) as root_count
`, [newRecoveryCaseId]);
if (nestedDeleteState.rows[0]?.recovery_count !== 1
    || nestedDeleteState.rows[0]?.root_count !== 1) {
  throw new Error('nested delete left a recovery/root orphan');
}
await db.exec(`
  drop table public.commercial_case_nested_delete_probe;
  drop function public.run_commercial_case_nested_delete_probe();
`);
console.log('commercial_case_nested_delete_rejected=OK');

const selectedIdentity = await db.query(`
  select selected_channel_identity_id as id
  from public.recovery_cases where id = $1
`, [newRecoveryCaseId]);
const selectedIdentityId = selectedIdentity.rows[0]?.id;
if (!selectedIdentityId) throw new Error('missing identity for conversation-delete probe');
const conversation = await db.query(`
  insert into public.conversations (contact_id, channel_identity_id)
  values ('10000000-0000-0000-0000-000000000002', $1)
  returning id
`, [selectedIdentityId]);
const conversationId = conversation.rows[0]?.id;
await db.query(`
  update public.recovery_cases set conversation_id = $1 where id = $2
`, [conversationId, newRecoveryCaseId]);
await db.query('delete from public.conversations where id = $1', [conversationId]);
const conversationDeleteState = await db.query(`
  select rc.conversation_id as recovery_conversation_id,
         cc.conversation_id as root_conversation_id
  from public.recovery_cases rc
  join public.commercial_cases cc on cc.id = rc.commercial_case_id
  where rc.id = $1
`, [newRecoveryCaseId]);
if (conversationDeleteState.rows[0]?.recovery_conversation_id !== null
    || conversationDeleteState.rows[0]?.root_conversation_id !== null) {
  throw new Error('conversation delete did not preserve SET NULL semantics');
}
console.log('commercial_case_conversation_delete_set_null=OK');

await db.query(`
  update public.recovery_cases
  set identity_resolution_status = 'pending'
  where id = $1
`, [newRecoveryCaseId]);
await db.query(
  'delete from public.identity_resolution_attempts where matched_channel_identity_id = $1',
  [selectedIdentityId],
);
await db.query(
  'delete from public.channel_identities where id = $1',
  [selectedIdentityId],
);
const identityDeleteState = await db.query(`
  select rc.selected_channel_identity_id as recovery_identity_id,
         cc.selected_channel_identity_id as root_identity_id
  from public.recovery_cases rc
  join public.commercial_cases cc on cc.id = rc.commercial_case_id
  where rc.id = $1
`, [newRecoveryCaseId]);
if (identityDeleteState.rows[0]?.recovery_identity_id !== null
    || identityDeleteState.rows[0]?.root_identity_id !== null) {
  throw new Error('identity delete did not preserve SET NULL semantics');
}
console.log('commercial_case_identity_delete_set_null=OK');

await db.exec(`
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values
    ('10000000-0000-0000-0000-000000000005', 'hotmart',
     'commercial-root-immediate', 'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb),
    ('10000000-0000-0000-0000-000000000006', 'hotmart',
     'commercial-root-insert-delete', 'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb);
`);

await db.exec(`
  begin;
  set constraints all immediate;
  insert into public.recovery_cases (
    contact_id, abandonment_event_id, source,
    external_product_id, product_name, offer_code,
    status, grace_expires_at, policy_key, policy_version
  ) values (
    '10000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000005', 'hotmart',
    'product-immediate', 'Product Immediate', 'offer-immediate',
    'grace_period', now() + interval '1 hour',
    'commercial-root-probe', 1
  );
  commit;
`);
const immediate = await db.query(`
  select cc.id
  from public.commercial_cases cc
  join public.recovery_cases rc on rc.commercial_case_id = cc.id
  where rc.abandonment_event_id = '10000000-0000-0000-0000-000000000005'
`);
const immediateCaseId = immediate.rows[0]?.id;
if (!immediateCaseId || immediate.rows.length !== 1) {
  throw new Error('immediate constraints prevented exact shadow creation');
}
console.log('commercial_case_immediate_constraints=OK');

await db.exec(`
  begin;
  update public.recovery_cases
  set status = 'paused', version = version + 1
  where id = '${immediateCaseId}';
  delete from public.recovery_cases
  where id = '${immediateCaseId}';
  commit;
`);
const updateDelete = await db.query(`
  select
    (select count(*)::int from public.recovery_cases
     where id = '${immediateCaseId}') as recovery_count,
    (select count(*)::int from public.commercial_cases
     where id = '${immediateCaseId}') as root_count
`);
if (updateDelete.rows[0]?.recovery_count !== 0
    || updateDelete.rows[0]?.root_count !== 0) {
  throw new Error('update-delete did not remove recovery and shadow atomically');
}
console.log('commercial_case_update_delete_same_transaction=OK');

await db.exec(`
  begin;
  insert into public.recovery_cases (
    contact_id, abandonment_event_id, source,
    external_product_id, product_name, offer_code,
    status, grace_expires_at, policy_key, policy_version
  ) values (
    '10000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000006', 'hotmart',
    'product-insert-delete', 'Product Insert Delete', 'offer-insert-delete',
    'grace_period', now() + interval '1 hour',
    'commercial-root-probe', 1
  );
  delete from public.recovery_cases
  where abandonment_event_id = '10000000-0000-0000-0000-000000000006';
  commit;
`);
const insertDelete = await db.query(`
  select
    (select count(*)::int from public.recovery_cases
     where abandonment_event_id = '10000000-0000-0000-0000-000000000006') as recovery_count,
    (select count(*)::int from public.commercial_cases
     where product_ref = 'product-insert-delete') as root_count
`);
if (insertDelete.rows[0]?.recovery_count !== 0
    || insertDelete.rows[0]?.root_count !== 0) {
  throw new Error('insert-delete did not leave zero durable rows');
}
console.log('commercial_case_insert_delete_same_transaction=OK');

const globalOrphans = await db.query(`
  select
    (select count(*)::int
     from public.recovery_cases rc
     left join public.commercial_cases cc on cc.id = rc.commercial_case_id
     where cc.id is null) as recoveries_without_root,
    (select count(*)::int
     from public.commercial_cases cc
     left join public.recovery_cases rc on rc.id = cc.recovery_case_id
     where cc.case_kind = 'cart_recovery' and rc.id is null) as roots_without_recovery
`);
if (globalOrphans.rows[0]?.recoveries_without_root !== 0
    || globalOrphans.rows[0]?.roots_without_recovery !== 0) {
  throw new Error('commercial-case global orphan check failed');
}
console.log('commercial_case_global_orphans=OK');

const hardenedFunctions = await db.query(`
  select
    count(*) filter (
      where p.proname in (
        'sync_recovery_commercial_case',
        'validate_recovery_commercial_case_shadow'
      )
        and p.prosecdef
        and array_to_string(p.proconfig, ',') =
            'search_path=pg_catalog, public, pg_temp'
    )::int as definer_count,
    count(*) filter (
      where p.proname in (
        'bind_recovery_commercial_case_id',
        'protect_commercial_case_shadow'
      )
        and not p.prosecdef
    )::int as invoker_count
  from pg_proc p
  join pg_namespace n on n.oid = p.pronamespace
  where n.nspname = 'public'
    and p.proname in (
      'bind_recovery_commercial_case_id',
      'sync_recovery_commercial_case',
      'protect_commercial_case_shadow',
      'validate_recovery_commercial_case_shadow'
    )
`);
if (hardenedFunctions.rows[0]?.definer_count !== 2
    || hardenedFunctions.rows[0]?.invoker_count !== 2) {
  throw new Error('commercial-case definer catalog hardening failed');
}

const functionAclLeaks = await db.query(`
  select count(*)::int as count
  from pg_proc p
  join pg_namespace n on n.oid = p.pronamespace
  cross join unnest(array['anon', 'authenticated', 'service_role']) as role_name
  where n.nspname = 'public'
    and p.proname in (
      'bind_recovery_commercial_case_id',
      'sync_recovery_commercial_case',
      'protect_commercial_case_shadow',
      'validate_recovery_commercial_case_shadow'
    )
    and has_function_privilege(role_name, p.oid, 'execute')
`);
if (functionAclLeaks.rows[0]?.count !== 0) {
  throw new Error('commercial-case trigger function EXECUTE ACL leaked');
}
console.log('commercial_case_definer_catalog=OK');
