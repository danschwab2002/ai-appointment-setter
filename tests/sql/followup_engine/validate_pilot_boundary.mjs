import { readdir, readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { PGlite } from '@electric-sql/pglite';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const baseline = (await readFile(
  `${root}/supabase/baseline/20260803_public_schema.sql`,
  'utf8',
)).replace(
  'create extension if not exists pgcrypto;',
  '-- omitted in PGlite: extension unavailable',
);
const migrationDir = `${root}/supabase/migrations`;
const migrationNames = (await readdir(migrationDir))
  .filter((name) => name.endsWith('.sql'))
  .sort();

const db = new PGlite();
await db.waitReady;
await db.exec(baseline);
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  alter default privileges grant execute on functions to anon, authenticated;
  alter default privileges grant all on tables to service_role;
`);
for (const name of migrationNames) {
  await db.exec(await readFile(`${migrationDir}/${name}`, 'utf8'));
}

const CONTACT_1 = '10000000-0000-0000-0000-000000000001';
const CONTACT_2 = '10000000-0000-0000-0000-000000000002';
const CONTACT_3 = '10000000-0000-0000-0000-000000000003';
let eventId = null;
let foreignEventId = null;
const ATTEMPT_1 = '40000000-0000-0000-0000-000000000001';
const ATTEMPT_2 = '40000000-0000-0000-0000-000000000002';
const ATTEMPT_3 = '40000000-0000-0000-0000-000000000003';
const ATTEMPT_4 = '40000000-0000-0000-0000-000000000004';
const FOREIGN_ATTEMPT = '40000000-0000-0000-0000-000000000005';
let actionId = null;
const NOW = new Date().toISOString();

function assertOne(rows, message) {
  if (rows.length !== 1) throw new Error(`${message}: expected one row`);
  return rows[0];
}

async function setRuntime(expectedGeneration, targetState, reason) {
  const result = await db.query(`
    select * from public.set_lancemos_pilot_runtime_state(
      'lancemos-cart-recovery', 1, $1, $2, 'operator-test', $3
    )
  `, [expectedGeneration, targetState, reason]);
  return assertOne(result.rows, 'runtime transition');
}

async function setMembership(contactId, expectedGeneration, targetStatus) {
  const result = await db.query(`
    select * from public.set_lancemos_pilot_cohort_member(
      'lancemos-cart-recovery', 1, $1, $2, $3,
      'operator-test', 'controlled-pilot'
    )
  `, [contactId, expectedGeneration, targetStatus]);
  return assertOne(result.rows, 'cohort transition');
}

async function activateScopeVersion(scopeKey, targetVersion, expectedGeneration) {
  const result = await db.query(`
    select * from public.activate_lancemos_pilot_scope_version(
      $1,$2,$3,'operator-test','version-rollout'
    )
  `, [scopeKey, targetVersion, expectedGeneration]);
  return assertOne(result.rows, 'scope version activation');
}

async function evaluate(overrides = {}) {
  const values = {
    tenant: 'lancemos',
    accountId: 10,
    inboxId: 20,
    provider: 'waba',
    channelAccountRef: 'opaque-number-ref',
    source: 'hotmart',
    eventType: 'PURCHASE_OUT_OF_SHOPPING_CART',
    productId: '3526906',
    offerCode: 'offer-1',
    contactId: CONTACT_1,
    ...overrides,
  };
  const result = await db.query(`
    select * from public.evaluate_lancemos_pilot_scope(
      'lancemos-cart-recovery', 1, $1, $2, $3, $4, $5,
      $6, $7, $8, $9, $10
    )
  `, [
    values.tenant,
    values.accountId,
    values.inboxId,
    values.provider,
    values.channelAccountRef,
    values.source,
    values.eventType,
    values.productId,
    values.offerCode,
    values.contactId,
  ]);
  return assertOne(result.rows, 'scope evaluation');
}

async function authorize(attemptId, now, overrides = {}) {
  const values = {
    tenant: 'lancemos',
    accountId: 10,
    inboxId: 20,
    provider: 'waba',
    channelAccountRef: 'opaque-number-ref',
    source: 'hotmart',
    eventType: 'PURCHASE_OUT_OF_SHOPPING_CART',
    productId: '3526906',
    offerCode: 'offer-1',
    contactId: CONTACT_1,
    actionId,
    ...overrides,
  };
  const result = await db.query(`
    select * from public.authorize_lancemos_pilot_request_start(
      'lancemos-cart-recovery', 1, $1, $2, $3, $4, $5,
      $6, $7, $8, $9, $10, $11, $12, $13
    )
  `, [
    values.tenant,
    values.accountId,
    values.inboxId,
    values.provider,
    values.channelAccountRef,
    values.source,
    values.eventType,
    values.productId,
    values.offerCode,
    values.contactId,
    values.actionId,
    attemptId,
    now,
  ]);
  return assertOne(result.rows, 'request authorization');
}

async function admitCartAbandonment(externalEventId, email, phone) {
  const payload = {
    id: externalEventId,
    creation_date: Date.parse('2026-08-10T10:00:00.000Z'),
    event: 'PURCHASE_OUT_OF_SHOPPING_CART',
    version: '2.0.0',
    data: {
      buyer: { email, phone },
      product: { id: 3526906, name: 'Product One' },
      offer: { code: 'offer-1' },
    },
  };
  const result = await db.query(
    `select * from public.admit_and_correlate_hotmart_cart_abandonment(
      $1, $2::jsonb, $3, $4
    )`,
    [externalEventId, JSON.stringify(payload), email, phone],
  );
  const admitted = assertOne(result.rows, 'cart abandonment admission');
  if (admitted.outcome !== 'inserted') throw new Error('cart abandonment was not inserted');
  return admitted.webhook_event_id;
}

await db.exec(`
  insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
  ) values (
    'cart-recovery-test', 1, 'published', 'cart_recovery', 'UTC',
    '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]',
    interval '0 seconds', interval '30 days', 4,
    '[{"step_key":"first_contact","mode":"freeform"}]',
    'operator-test', now(), now()
  ), (
    'foreign-policy-test', 1, 'published', 'cart_recovery', 'UTC',
    '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]',
    interval '0 seconds', interval '30 days', 1,
    '[{"step_key":"foreign","mode":"freeform"}]',
    'operator-test', now(), now()
  );

  insert into public.pilot_scope_versions (
    scope_key, version, status, tenant_key,
    chatwoot_account_id, chatwoot_inbox_id,
    channel, channel_provider, channel_account_ref,
    source, source_event_type, external_product_id, offer_code, purpose,
    policy_key, policy_version, timezone,
    max_cohort_contacts,
    max_outbound_request_starts_total,
    max_outbound_request_starts_per_day,
    approved_by, approved_at, published_at
  ) values (
    'lancemos-cart-recovery', 1, 'published', 'lancemos',
    10, 20, 'whatsapp', 'waba', 'opaque-number-ref',
    'hotmart', 'PURCHASE_OUT_OF_SHOPPING_CART', '3526906', 'offer-1',
    'cart_recovery', 'cart-recovery-test', 1, 'America/Argentina/Buenos_Aires',
    2, 2, 1, 'operator-test', now(), now()
  );

  insert into public.pilot_runtime_controls (
    scope_key, scope_version, runtime_state, generation,
    changed_by, change_reason
  ) values (
    'lancemos-cart-recovery', 1, 'inactive', 0,
    'migration-test', 'default-off'
  );

  insert into public.contacts (id, full_name, email, phone) values
    ('${CONTACT_1}', 'Pilot Contact One', 'pilot-one@example.com', '5491100000000'),
    ('${CONTACT_2}', 'Pilot Contact Two', 'pilot-two@example.com', '5491100000001'),
    ('${CONTACT_3}', 'Pilot Contact Three', null, null);
`);

const inactive = await evaluate();
if (inactive.allowed !== false || inactive.reason_code !== 'pilot_runtime_not_armed') {
  throw new Error('inactive runtime did not fail closed');
}
console.log('pilot_default_off=OK');

const armed = await setRuntime(0, 'armed', 'controlled-test');
if (armed.runtime_state !== 'armed' || armed.generation !== 1 || armed.changed !== true) {
  throw new Error('runtime did not arm with generation CAS');
}

const absentRemoval = await setMembership(CONTACT_3, 1, 'removed');
if (absentRemoval.member_status !== 'removed' || absentRemoval.generation !== 1
    || absentRemoval.active_member_count !== 0 || absentRemoval.changed !== false) {
  throw new Error('absent cohort removal was not an idempotent no-op');
}

const member1 = await setMembership(CONTACT_1, 1, 'active');
if (member1.member_status !== 'active' || member1.generation !== 2
    || member1.active_member_count !== 1 || member1.changed !== true) {
  throw new Error('first cohort enrollment failed');
}
const member1Replay = await setMembership(CONTACT_1, 2, 'active');
if (member1Replay.generation !== 2 || member1Replay.changed !== false) {
  throw new Error('cohort enrollment replay was not idempotent');
}
const member2 = await setMembership(CONTACT_2, 2, 'active');
if (member2.generation !== 3 || member2.active_member_count !== 2) {
  throw new Error('second cohort enrollment failed');
}
const member3 = await setMembership(CONTACT_3, 3, 'active');
if (member3.changed !== false || member3.reason_code !== 'pilot_cohort_limit_reached'
    || member3.generation !== 3 || member3.active_member_count !== 2) {
  throw new Error('cohort cap did not fail closed');
}
console.log('pilot_cohort_cap=OK');

const allowed = await evaluate();
if (allowed.allowed !== true || allowed.reason_code !== 'pilot_scope_allowed'
    || allowed.runtime_generation !== 3) {
  throw new Error('valid scope was not accepted');
}
const wrongAccount = await evaluate({ accountId: 11 });
if (wrongAccount.allowed !== false
    || wrongAccount.reason_code !== 'pilot_chatwoot_account_mismatch') {
  throw new Error('wrong account did not fail closed');
}
const wrongInbox = await evaluate({ inboxId: 21 });
if (wrongInbox.allowed !== false
    || wrongInbox.reason_code !== 'pilot_chatwoot_inbox_mismatch') {
  throw new Error('wrong inbox did not fail closed');
}
const wrongOffer = await evaluate({ offerCode: 'offer-2' });
if (wrongOffer.allowed !== false || wrongOffer.reason_code !== 'pilot_offer_mismatch') {
  throw new Error('wrong offer did not fail closed');
}
const outsideCohort = await evaluate({ contactId: CONTACT_3 });
if (outsideCohort.allowed !== false
    || outsideCohort.reason_code !== 'pilot_contact_not_in_cohort') {
  throw new Error('outside-cohort contact did not fail closed');
}
console.log('pilot_scope_conjunction=OK');

eventId = await admitCartAbandonment(
  'pilot-boundary-event',
  'pilot-one@example.com',
  '5491100000000',
);
await db.query(`
  insert into public.contact_points (
    contact_id,type,raw_value,normalized_value,source,source_event_id
  ) values
    ($1,'email','pilot-one@example.com','pilot-one@example.com','hotmart',$2),
    ($1,'phone','5491100000000','5491100000000','hotmart',$2)
`, [CONTACT_1, eventId]);
const plan = await db.query(`
  select * from public.plan_cart_recovery_with_identity(
    $1, '${CONTACT_1}',
    '3526906', 'Product One', 'offer-1',
    'cart-recovery-test', 1, timestamptz '2026-08-10 10:00:00+00',
    10, 20, '5491100000000'
  )
`, [eventId]);
const planned = assertOne(plan.rows, 'cart recovery plan');
if (planned.scheduled_action_id == null) throw new Error('plan did not return action');
actionId = planned.scheduled_action_id;
await db.query(`
  insert into public.followup_delivery_attempts (
    id, action_id, idempotency_key, attempt_number, channel, mode,
    phase, started_at, lease_generation,
    expected_case_version, expected_sequence_revision
  ) values
    ('${ATTEMPT_1}', $1, 'pilot-attempt-1', 1, 'whatsapp', 'freeform',
     'reserved', timestamptz '2026-08-10 10:01:00+00', 1, 1, 1),
    ('${ATTEMPT_2}', $1, 'pilot-attempt-2', 2, 'whatsapp', 'freeform',
     'reserved', timestamptz '2026-08-11 10:01:00+00', 2, 1, 1),
    ('${ATTEMPT_3}', $1, 'pilot-attempt-3', 3, 'whatsapp', 'freeform',
     'reserved', timestamptz '2026-08-12 10:01:00+00', 3, 1, 1),
    ('${ATTEMPT_4}', $1, 'pilot-attempt-4', 4, 'whatsapp', 'freeform',
     'reserved', timestamptz '2026-08-13 10:01:00+00', 4, 1, 1);
`, [actionId]);

foreignEventId = await admitCartAbandonment(
  'pilot-foreign-policy-event',
  'pilot-two@example.com',
  '5491100000001',
);
await db.query(`
  insert into public.contact_points (
    contact_id,type,raw_value,normalized_value,source,source_event_id
  ) values
    ($1,'email','pilot-two@example.com','pilot-two@example.com','hotmart',$2),
    ($1,'phone','5491100000001','5491100000001','hotmart',$2)
`, [CONTACT_2, foreignEventId]);
const foreignPlan = await db.query(`
  select * from public.plan_cart_recovery_with_identity(
    $1, '${CONTACT_2}',
    '3526906', 'Product One', 'offer-1',
    'foreign-policy-test', 1, timestamptz '2026-08-10 10:00:00+00',
    10, 20, '5491100000001'
  )
`, [foreignEventId]);
const foreignActionId = assertOne(
  foreignPlan.rows,
  'foreign-policy plan',
).scheduled_action_id;
await db.query(`
  insert into public.followup_delivery_attempts (
    id,action_id,idempotency_key,attempt_number,channel,mode,phase,
    started_at,lease_generation,expected_case_version,expected_sequence_revision
  ) values ($1,$2,'pilot-foreign-policy-attempt',1,'whatsapp','freeform',
            'reserved',clock_timestamp(),1,1,1)
`, [FOREIGN_ATTEMPT, foreignActionId]);
const wrongPolicy = await authorize(FOREIGN_ATTEMPT, NOW, {
  actionId: foreignActionId,
  contactId: CONTACT_2,
});
if (wrongPolicy.authorized !== false
    || wrongPolicy.reason_code !== 'pilot_attempt_mismatch') {
  throw new Error('action under a foreign policy crossed the pilot scope');
}

const forgedTime = await authorize(ATTEMPT_1, '2099-01-01 00:00:00+00');
if (forgedTime.authorized !== false
    || forgedTime.reason_code !== 'pilot_request_time_invalid') {
  throw new Error('caller-controlled time could bypass pilot budgets');
}
const authorization1 = await authorize(ATTEMPT_1, NOW);
if (authorization1.authorized !== true
    || authorization1.reason_code !== 'pilot_request_start_authorized'
    || authorization1.replayed !== false
    || authorization1.request_authorization_id == null) {
  throw new Error('first request-start authorization failed');
}
const authorization1Replay = await authorize(ATTEMPT_1, NOW);
if (authorization1Replay.authorized !== true
    || authorization1Replay.replayed !== true
    || authorization1Replay.request_authorization_id
       !== authorization1.request_authorization_id) {
  throw new Error('request authorization replay consumed another slot');
}
const replayWithWrongProvider = await authorize(
  ATTEMPT_1,
  NOW,
  { provider: 'evolution' },
);
if (replayWithWrongProvider.authorized !== false
    || replayWithWrongProvider.reason_code !== 'pilot_channel_account_mismatch') {
  throw new Error('request authorization replay bypassed scope conjunction');
}
const dailyBlocked = await authorize(ATTEMPT_2, NOW);
if (dailyBlocked.authorized !== false
    || dailyBlocked.reason_code !== 'pilot_daily_budget_exhausted') {
  throw new Error('daily budget did not fail closed');
}
await db.query(`
  insert into public.pilot_outbound_request_authorizations (
    scope_key,scope_version,action_id,attempt_id,contact_id,
    local_budget_date,runtime_generation,reason_code,authorized_at
  ) values (
    'lancemos-cart-recovery',1,$1,$2,$3,
    (clock_timestamp() at time zone 'America/Argentina/Buenos_Aires')::date - 1,
    3,'pilot_request_start_authorized',clock_timestamp() - interval '1 day'
  )
`, [actionId, ATTEMPT_2, CONTACT_1]);
const totalBlocked = await authorize(ATTEMPT_3, NOW);
if (totalBlocked.authorized !== false
    || totalBlocked.reason_code !== 'pilot_total_budget_exhausted') {
  throw new Error('total budget did not fail closed');
}
const ledgerCount = await db.query(`
  select count(*)::int as count
  from public.pilot_outbound_request_authorizations
`);
if (ledgerCount.rows[0].count !== 2) {
  throw new Error('budget failures or replay wrote extra ledger rows');
}
console.log('pilot_budget_and_replay=OK');

const paused = await setRuntime(3, 'paused', 'kill-switch-test');
if (paused.runtime_state !== 'paused' || paused.generation !== 4) {
  throw new Error('kill switch did not pause');
}
const pausedAuthorization = await authorize(ATTEMPT_4, NOW);
if (pausedAuthorization.authorized !== false
    || pausedAuthorization.reason_code !== 'pilot_runtime_not_armed') {
  throw new Error('paused runtime authorized a new request');
}
const pausedEvaluation = await evaluate();
if (pausedEvaluation.allowed !== false
    || pausedEvaluation.reason_code !== 'pilot_runtime_not_armed') {
  throw new Error('paused runtime passed early evaluation');
}
const pausedReplay = await authorize(ATTEMPT_1, NOW);
if (pausedReplay.authorized !== true
    || pausedReplay.reason_code !== 'pilot_request_start_authorized'
    || pausedReplay.replayed !== true) {
  throw new Error('kill switch rewrote a durable authorization replay');
}
console.log('pilot_kill_switch=OK');

let staleGenerationRejected = false;
try {
  await setRuntime(3, 'armed', 'stale-rearm');
} catch (error) {
  staleGenerationRejected = String(error).includes('pilot_runtime_generation_mismatch');
}
if (!staleGenerationRejected) throw new Error('stale runtime generation was accepted');
console.log('pilot_generation_fence=OK');

let publishedMutationRejected = false;
try {
  await db.exec(`
    update public.pilot_scope_versions
    set offer_code='changed-offer'
    where scope_key='lancemos-cart-recovery' and version=1
  `);
} catch (error) {
  publishedMutationRejected = String(error).includes('published_pilot_scope_is_immutable');
}
if (!publishedMutationRejected) throw new Error('published scope was mutable');

let ledgerMutationRejected = false;
try {
  await db.exec(`
    update public.pilot_outbound_request_authorizations
    set reason_code='rewritten'
    where attempt_id='${ATTEMPT_1}'
  `);
} catch (error) {
  ledgerMutationRejected = String(error).includes('pilot_request_authorization_is_append_only');
}
if (!ledgerMutationRejected) throw new Error('request authorization ledger was mutable');

await db.exec(`
  insert into public.pilot_scope_versions (
    scope_key,version,status,tenant_key,chatwoot_account_id,chatwoot_inbox_id,
    channel,channel_provider,channel_account_ref,source,source_event_type,
    external_product_id,offer_code,purpose,policy_key,policy_version,timezone,
    max_cohort_contacts,max_outbound_request_starts_total,
    max_outbound_request_starts_per_day,approved_by,approved_at,published_at
  )
  select 'lancemos-versioned',version,'published','lancemos',10,20,
         'whatsapp','waba','opaque-number-ref','hotmart',
         'PURCHASE_OUT_OF_SHOPPING_CART','3526906',
         'offer-' || version::text,'cart_recovery','cart-recovery-test',1,
         'America/Argentina/Buenos_Aires',2,2,1,
         'operator-test',clock_timestamp(),clock_timestamp()
  from (values (1),(2)) versions(version);
  insert into public.pilot_runtime_controls(
    scope_key,scope_version,runtime_state,generation,changed_by,change_reason
  ) values ('lancemos-versioned',1,'inactive',0,'operator-test','default-off');
`);
const activatedV2 = await activateScopeVersion('lancemos-versioned', 2, 0);
if (activatedV2.scope_version !== 2 || activatedV2.runtime_state !== 'inactive'
    || activatedV2.generation !== 1 || activatedV2.changed !== true) {
  throw new Error('published scope version did not activate default-off');
}
const activatedV2Replay = await activateScopeVersion('lancemos-versioned', 2, 1);
if (activatedV2Replay.generation !== 1 || activatedV2Replay.changed !== false) {
  throw new Error('scope version activation replay was not idempotent');
}
await db.query(`
  select * from public.set_lancemos_pilot_runtime_state(
    'lancemos-versioned',2,1,'armed','operator-test','version-test'
  )
`);
let armedVersionChangeRejected = false;
try {
  await activateScopeVersion('lancemos-versioned', 1, 2);
} catch (error) {
  armedVersionChangeRejected = String(error).includes(
    'pilot_scope_version_change_requires_pause',
  );
}
if (!armedVersionChangeRejected) throw new Error('armed scope version changed in place');
await db.query(`
  select * from public.set_lancemos_pilot_runtime_state(
    'lancemos-versioned',2,2,'paused','operator-test','version-test'
  )
`);
const rolledBackV1 = await activateScopeVersion('lancemos-versioned', 1, 3);
if (rolledBackV1.scope_version !== 1 || rolledBackV1.runtime_state !== 'inactive'
    || rolledBackV1.generation !== 4) {
  throw new Error('paused scope did not roll back default-off');
}
let timezoneRolloverRejected = false;
try {
  await db.exec(`
    insert into public.pilot_scope_versions (
      scope_key,version,status,tenant_key,chatwoot_account_id,chatwoot_inbox_id,
      channel,channel_provider,channel_account_ref,source,source_event_type,
      external_product_id,offer_code,purpose,policy_key,policy_version,timezone,
      max_cohort_contacts,max_outbound_request_starts_total,
      max_outbound_request_starts_per_day,approved_by,approved_at,published_at
    ) values (
      'lancemos-versioned',3,'published','lancemos',10,20,
      'whatsapp','waba','opaque-number-ref','hotmart',
      'PURCHASE_OUT_OF_SHOPPING_CART','3526906','offer-3','cart_recovery',
      'cart-recovery-test',1,'UTC',2,2,1,
      'operator-test',clock_timestamp(),clock_timestamp()
    )
  `);
} catch (error) {
  timezoneRolloverRejected = String(error).includes(
    'pilot_scope_timezone_must_remain_constant',
  );
}
if (!timezoneRolloverRejected) throw new Error('version change reset the daily timezone');
console.log('pilot_scope_version_activation=OK');

const functionPrivileges = await db.query(`
  select p.proname,
         has_function_privilege('anon', p.oid, 'EXECUTE') as anon_execute,
         has_function_privilege('authenticated', p.oid, 'EXECUTE') as authenticated_execute,
         has_function_privilege('service_role', p.oid, 'EXECUTE') as service_execute
  from pg_proc p
  join pg_namespace n on n.oid=p.pronamespace
  where n.nspname='public'
    and p.proname in (
      'validate_pilot_scope_version',
      'validate_pilot_runtime_control_transition',
      'reject_pilot_append_only_mutation',
      'activate_lancemos_pilot_scope_version',
      'set_lancemos_pilot_runtime_state',
      'set_lancemos_pilot_cohort_member',
      'evaluate_lancemos_pilot_scope',
      'authorize_lancemos_pilot_request_start'
    )
`);
if (functionPrivileges.rows.length !== 8) {
  throw new Error('pilot function privilege inventory is incomplete');
}
const serviceEntrypoints = new Set([
  'activate_lancemos_pilot_scope_version',
  'set_lancemos_pilot_runtime_state',
  'set_lancemos_pilot_cohort_member',
  'evaluate_lancemos_pilot_scope',
]);
for (const row of functionPrivileges.rows) {
  if (row.anon_execute || row.authenticated_execute
      || row.service_execute !== serviceEntrypoints.has(row.proname)) {
    throw new Error(`pilot function privilege leak: ${JSON.stringify(row)}`);
  }
}
const tablePrivileges = await db.query(`
  select table_name,
         has_table_privilege('anon', format('public.%I', table_name),
                             'SELECT,INSERT,UPDATE,DELETE') as anon_dml,
         has_table_privilege('authenticated', format('public.%I', table_name),
                             'SELECT,INSERT,UPDATE,DELETE') as authenticated_dml,
         has_table_privilege('service_role', format('public.%I', table_name),
                             'SELECT,INSERT,UPDATE,DELETE') as service_dml
  from information_schema.tables
  where table_schema='public'
    and table_name like 'pilot_%'
`);
if (tablePrivileges.rows.length !== 6
    || tablePrivileges.rows.some((row) => row.anon_dml
      || row.authenticated_dml || row.service_dml)) {
  throw new Error(`pilot table privilege leak: ${JSON.stringify(tablePrivileges.rows)}`);
}
console.log('pilot_effective_privileges=OK');

const closed = await setRuntime(4, 'closed', 'pilot-finished');
if (closed.runtime_state !== 'closed' || closed.generation !== 5) {
  throw new Error('runtime did not close irreversibly');
}
let closedRuntimeRejected = false;
try {
  await setRuntime(5, 'armed', 'unsafe-reopen');
} catch (error) {
  closedRuntimeRejected = String(error).includes('closed_pilot_runtime_is_terminal');
}
if (!closedRuntimeRejected) throw new Error('closed runtime was reopened');
console.log('pilot_closed_is_irreversible=OK');

const audit = await db.query(`
  select event_type, count(*)::int as count
  from public.pilot_control_events
  where scope_key='lancemos-cart-recovery'
  group by event_type
`);
const auditCounts = Object.fromEntries(audit.rows.map((row) => [row.event_type, row.count]));
if (auditCounts.pilot_runtime_state_changed !== 3
    || auditCounts.pilot_cohort_member_enrolled !== 2
    || auditCounts.pilot_outbound_request_authorized !== 1) {
  throw new Error(`unexpected pilot audit counts: ${JSON.stringify(auditCounts)}`);
}
console.log('pilot_immutability_and_audit=OK');
console.log('LANCEMOS_PILOT_BOUNDARY_OK');
