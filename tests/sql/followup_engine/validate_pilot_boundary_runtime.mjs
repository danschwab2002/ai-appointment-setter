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

const CONTACT = '71000000-0000-0000-0000-000000000001';
const NOW = new Date();
const ABANDONED_AT = NOW.toISOString();
const EVALUATION_AT = new Date(NOW.getTime() + 60_000).toISOString();
const payload = {
  id: 'pilot-runtime-event',
  creation_date: NOW.getTime(),
  event: 'PURCHASE_OUT_OF_SHOPPING_CART',
  version: '2.0.0',
  data: {
    buyer: { email: 'pilot-runtime@example.com', phone: '5491100000100' },
    product: { id: 3526906, name: 'Product One' },
    offer: { code: 'offer-1' },
  },
};

function one(rows, label) {
  if (rows.length !== 1) throw new Error(`${label}: expected one row`);
  return rows[0];
}

async function plan() {
  return db.query(`
    select * from public.plan_lancemos_pilot_cart_recovery(
      $1, '${CONTACT}', '3526906', 'Product One', 'offer-1',
      'cart-recovery-test', 1, $2,
      10, 20, '5491100000100',
      'lancemos-cart-recovery', 1
    )
  `, [eventId, ABANDONED_AT]);
}

await db.exec(`
  insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
  ) values (
    'cart-recovery-test', 1, 'published', 'cart_recovery', 'UTC',
    '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]',
    interval '0 seconds', interval '30 days', 1,
    '[{"step_key":"first_contact","mode":"approved_template"}]',
    'operator-test', now(), now()
  );
  insert into public.pilot_scope_versions (
    scope_key, version, status, tenant_key,
    chatwoot_account_id, chatwoot_inbox_id,
    channel, channel_provider, channel_account_ref,
    source, source_event_type, external_product_id, offer_code, purpose,
    policy_key, policy_version, timezone,
    max_cohort_contacts, max_outbound_request_starts_total,
    max_outbound_request_starts_per_day,
    approved_by, approved_at, published_at
  ) values (
    'lancemos-cart-recovery', 1, 'published', 'lancemos',
    10, 20, 'whatsapp', 'waba', 'opaque-number-ref',
    'hotmart', 'PURCHASE_OUT_OF_SHOPPING_CART', '3526906', 'offer-1',
    'cart_recovery', 'cart-recovery-test', 1, 'UTC',
    1, 5, 5, 'operator-test', now(), now()
  );
  insert into public.pilot_runtime_controls (
    scope_key, scope_version, runtime_state, generation, changed_by, change_reason
  ) values (
    'lancemos-cart-recovery', 1, 'inactive', 0, 'test', 'default-off'
  );
  insert into public.contacts (id, full_name, email, phone)
  values ('${CONTACT}', 'Pilot Runtime', 'pilot-runtime@example.com', '5491100000100');
`);

const admitted = one((await db.query(
  'select * from public.admit_hotmart_cart_abandonment($1,$2::jsonb)',
  [payload.id, JSON.stringify(payload)],
)).rows, 'admission');
const eventId = admitted.webhook_event_id;
await db.query(`
  insert into public.contact_points (
    contact_id, type, raw_value, normalized_value, source, source_event_id
  ) values
    ('${CONTACT}', 'email', 'pilot-runtime@example.com', 'pilot-runtime@example.com', 'hotmart', $1),
    ('${CONTACT}', 'phone', '5491100000100', '5491100000100', 'hotmart', $1)
`, [eventId]);

const inactiveStatus = one((await db.query(`
  select * from public.get_lancemos_pilot_runtime_status(
    'lancemos-cart-recovery',1,'lancemos','waba','opaque-number-ref'
  )
`)).rows, 'inactive status');
if (!inactiveStatus.configured
    || inactiveStatus.runtime_state !== 'inactive'
    || inactiveStatus.reason_code !== 'pilot_runtime_inactive') {
  throw new Error('inactive runtime status was not deployment-ready');
}
const mismatchedStatus = one((await db.query(`
  select * from public.get_lancemos_pilot_runtime_status(
    'lancemos-cart-recovery',1,'lancemos','waba','wrong-ref'
  )
`)).rows, 'mismatched status');
if (mismatchedStatus.configured
    || mismatchedStatus.reason_code !== 'pilot_scope_config_mismatch') {
  throw new Error('runtime status did not detect configuration mismatch');
}
console.log('pilot_runtime_readiness_status=OK');

let inactiveBlocked = false;
try {
  await plan();
} catch (error) {
  inactiveBlocked = String(error.message).includes('pilot_scope_rejected');
}
if (!inactiveBlocked) throw new Error('inactive pilot planned work');
const inactiveCases = await db.query(
  'select count(*)::integer as count from public.recovery_cases where contact_id=$1',
  [CONTACT],
);
if (inactiveCases.rows[0].count !== 0) throw new Error('rejected plan left durable work');
console.log('pilot_runtime_atomic_plan_default_off=OK');

await db.query(`
  select * from public.set_lancemos_pilot_runtime_state(
    'lancemos-cart-recovery',1,0,'armed','operator-test','controlled-test'
  )
`);
await db.query(`
  select * from public.set_lancemos_pilot_cohort_member(
    'lancemos-cart-recovery',1,'${CONTACT}',1,'active',
    'operator-test','controlled-test'
  )
`);
const planned = one((await plan()).rows, 'pilot plan');
const actionId = planned.scheduled_action_id;
if (planned.created !== true) throw new Error('pilot plan was not created');
const binding = one((await db.query(`
  select scope_key,scope_version,source_event_id
  from public.pilot_recovery_case_bindings
  where recovery_case_id=$1
`, [planned.recovery_case_id])).rows, 'case scope binding');
if (binding.scope_key !== 'lancemos-cart-recovery'
    || binding.scope_version !== 1
    || binding.source_event_id !== eventId) {
  throw new Error('case was not durably bound to its admitted pilot scope');
}
console.log('pilot_runtime_atomic_plan_allowed=OK');

const claimed = one((await db.query(`
  select * from public.claim_due_followup_actions(
    'pilot-runtime-worker', $1, interval '5 minutes', 1
  )
`, [EVALUATION_AT])).rows, 'claim');
if (claimed.id !== actionId) throw new Error('claimed wrong action');

await db.query(`
  insert into public.conversation_events (
    recovery_case_id, event_type, actor_type, related_action_id, data
  ) values (
    $1, 'followup_action_reevaluated', 'system', $2,
    jsonb_build_object(
      'decision','execute', 'reason_code','eligible_for_execution',
      'worker_id','pilot-runtime-worker',
      'lease_generation',$3::bigint,
      'case_version',$4::bigint,
      'sequence_revision',$5::bigint
    )
  )
`, [
  claimed.recovery_case_id,
  actionId,
  claimed.lease_generation,
  claimed.expected_case_version,
  1,
]);
let freeformBlocked = false;
await db.exec('begin');
try {
  const wrongModeAttempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt(
      $1, 'pilot-runtime-worker', $2, $3, 1,
      'whatsapp', 'freeform', $4
    )
  `, [
    actionId,
    claimed.lease_generation,
    claimed.expected_case_version,
    EVALUATION_AT,
  ])).rows, 'wrong-mode reserve');
  try {
    await db.query(`
      select * from public.mark_lancemos_pilot_request_started(
        $1,$2,'pilot-runtime-worker',$3,$4
      )
    `, [
      actionId,
      wrongModeAttempt.id,
      claimed.lease_generation,
      EVALUATION_AT,
    ]);
  } catch (error) {
    freeformBlocked = (
      String(error.message).includes('pilot_request_start_rejected')
      && String(error.detail).includes('pilot_delivery_mode_mismatch')
    );
  }
} finally {
  await db.exec('rollback');
}
if (!freeformBlocked) throw new Error('WABA request-start accepted freeform mode');
console.log('pilot_runtime_waba_freeform_blocked=OK');

const attempt = one((await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1, 'pilot-runtime-worker', $2, $3, 1,
    'whatsapp', 'approved_template', $4
  )
`, [
  actionId,
  claimed.lease_generation,
  claimed.expected_case_version,
  EVALUATION_AT,
])).rows, 'reserve');

let legacyBlocked = false;
try {
  await db.query(`
    select * from public.mark_followup_request_started(
      $1,$2,'pilot-runtime-worker',$3,$4
    )
  `, [actionId, attempt.id, claimed.lease_generation, EVALUATION_AT]);
} catch (error) {
  legacyBlocked = String(error.message).includes(
    'pilot_request_authorization_required',
  );
}
if (!legacyBlocked) throw new Error('legacy request-start bypassed pilot authorization');
console.log('pilot_runtime_legacy_request_start_blocked=OK');

const started = one((await db.query(`
  select * from public.mark_lancemos_pilot_request_started(
    $1,$2,'pilot-runtime-worker',$3,$4
  )
`, [actionId, attempt.id, claimed.lease_generation, EVALUATION_AT])).rows, 'start');
if (started.phase !== 'request_started'
    || started.pilot_authorization_replayed !== false
    || started.pilot_authorization_id == null) {
  throw new Error('atomic pilot request-start did not commit both facts');
}
const authorizationCount = await db.query(`
  select count(*)::integer as count
  from public.pilot_outbound_request_authorizations
  where attempt_id=$1
`, [attempt.id]);
if (authorizationCount.rows[0].count !== 1) {
  throw new Error('request-start authorization cardinality mismatch');
}

const replay = one((await db.query(`
  select * from public.mark_lancemos_pilot_request_started(
    $1,$2,'pilot-runtime-worker',$3,$4
  )
`, [actionId, attempt.id, claimed.lease_generation, EVALUATION_AT])).rows, 'replay');
if (replay.phase !== 'request_started'
    || replay.pilot_authorization_replayed !== true
    || replay.pilot_authorization_id !== started.pilot_authorization_id) {
  throw new Error('atomic request-start replay changed durable identity');
}
console.log('pilot_runtime_atomic_request_start_and_replay=OK');

const privileges = await db.query(`
  select
    has_function_privilege(
      'service_role',
      'public.get_lancemos_pilot_runtime_status(text,integer,text,text,text)',
      'execute'
    ) as service_status,
    has_function_privilege(
      'service_role',
      'public.mark_lancemos_pilot_request_started(uuid,uuid,text,bigint,timestamptz)',
      'execute'
    ) as service_atomic,
    has_function_privilege(
      'service_role',
      'public.authorize_lancemos_pilot_request_start(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid,uuid,uuid,timestamptz)',
      'execute'
    ) as service_standalone,
    has_function_privilege(
      'anon',
      'public.get_lancemos_pilot_runtime_status(text,integer,text,text,text)',
      'execute'
    ) as anon_status,
    has_function_privilege(
      'anon',
      'public.mark_lancemos_pilot_request_started(uuid,uuid,text,bigint,timestamptz)',
      'execute'
    ) as anon_atomic,
    has_function_privilege(
      'authenticated',
      'public.mark_lancemos_pilot_request_started(uuid,uuid,text,bigint,timestamptz)',
      'execute'
    ) as authenticated_atomic,
    has_table_privilege(
      'service_role','public.pilot_recovery_case_bindings',
      'select,insert,update,delete'
    ) as service_binding_dml,
    has_function_privilege(
      'service_role',
      'public.mark_followup_request_started(uuid,uuid,text,bigint,timestamptz)',
      'execute'
    ) as service_legacy_start
`);
const acl = one(privileges.rows, 'privileges');
if (!acl.service_status || !acl.service_atomic || acl.service_standalone
    || acl.anon_status || acl.anon_atomic || acl.authenticated_atomic
    || acl.service_binding_dml || acl.service_legacy_start) {
  throw new Error('pilot runtime effective privileges are unsafe');
}
console.log('pilot_runtime_effective_privileges=OK');

await db.close();
