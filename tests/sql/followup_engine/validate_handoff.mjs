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
`);
for (const file of [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(join(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => join(root, 'supabase/migrations', name)),
]) {
  await db.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}

await db.exec(`
insert into public.followup_policy_versions (
  policy_key, version, status, purpose, timezone, business_windows,
  grace_period, expires_after, max_automatic_messages, steps,
  approved_by, approved_at, published_at
) values (
  'handoff-flow', 1, 'published', 'cart_recovery', 'UTC', '{}'::jsonb,
  interval '1 hour', interval '7 days', 3, '[]'::jsonb,
  'local-test', '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z'
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
  'handoff-pilot', 1, 'published', 'lancemos', 1, 7,
  'whatsapp', 'waba', 'test-account-ref',
  'hotmart', 'PURCHASE_OUT_OF_SHOPPING_CART', '123', 'OFFER-1',
  'cart_recovery', 'handoff-flow', 1, 'UTC',
  1, 2, 2, 'local-test',
  '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z'
), (
  'foreign-handoff-pilot', 1, 'published', 'lancemos', 99, 7,
  'whatsapp', 'waba', 'foreign-account-ref',
  'hotmart', 'PURCHASE_OUT_OF_SHOPPING_CART', '123', 'OFFER-1',
  'cart_recovery', 'handoff-flow', 1, 'UTC',
  1, 2, 2, 'local-test',
  '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z'
);
insert into public.human_handoff_projection_policies (
  policy_key, policy_version, scope_key, scope_version, expected_team_id,
  note_template_key, note_template_version, private_note_body, active
) values (
  'lancemos-handoff', 1, 'handoff-pilot', 1, 17, 'handoff-note', 1,
  'Se solicitó intervención humana. Revisá la conversación antes de responder.',
  true
), (
  'foreign-handoff', 1, 'foreign-handoff-pilot', 1, 18, 'handoff-note', 1,
  'Se solicitó intervención humana.', true
);
insert into public.webhook_events (
  id, source, external_event_id, event_type, payload, processing_status, received_at
) values (
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1', 'hotmart', 'handoff-abandonment-1',
  'PURCHASE_OUT_OF_SHOPPING_CART', jsonb_build_object(
    'id', 'handoff-abandonment-1', 'creation_date', 1786320000000,
    'event', 'PURCHASE_OUT_OF_SHOPPING_CART', 'version', '2.0.0',
    'data', jsonb_build_object(
      'buyer', jsonb_build_object(
        'email', 'handoff@example.test', 'phone', '5531999999999'
      ),
      'product', jsonb_build_object('id', 123, 'name', 'Pilot Product'),
      'offer', jsonb_build_object('code', 'OFFER-1')
    )
  ), 'received',
  '2026-08-10T00:00:00Z'
);
insert into public.contacts (id, full_name, email, phone)
values (
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc1', 'Handoff Fixture',
  'handoff@example.test', '5531999999999'
);
insert into public.contact_points (
  contact_id, type, raw_value, normalized_value, source, source_event_id
) values
  (
    'cccccccc-cccc-4ccc-8ccc-ccccccccccc1', 'email',
    'handoff@example.test', 'handoff@example.test', 'hotmart',
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1'
  ),
  (
    'cccccccc-cccc-4ccc-8ccc-ccccccccccc1', 'phone',
    '5531999999999', '5531999999999', 'hotmart',
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1'
  );
`);

const plan = await db.query(`
select * from public.plan_cart_recovery_with_identity(
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1'::uuid,
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc1'::uuid,
  '123', 'Pilot Product', 'OFFER-1', 'handoff-flow', 1,
  '2026-08-10T00:00:00Z'::timestamptz,
  1, 7, '5531999999999'
);
`);
if (plan.rows.length !== 1) throw new Error('handoff fixture plan missing');
const recoveryCaseId = plan.rows[0].recovery_case_id;
const actionId = plan.rows[0].scheduled_action_id;

await db.exec(`
insert into public.pilot_recovery_case_bindings (
  recovery_case_id, scope_key, scope_version, source_event_id
) values (
  '${recoveryCaseId}'::uuid, 'handoff-pilot', 1,
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1'::uuid
);
insert into public.conversations (
  id, contact_id, channel_identity_id, commercial_context
)
select
  'dddddddd-dddd-4ddd-8ddd-ddddddddddd1'::uuid,
  rc.contact_id,
  rc.selected_channel_identity_id,
  jsonb_build_object('chatwoot_conversation_id', 42)
from public.recovery_cases rc
where rc.id = '${recoveryCaseId}'::uuid;

update public.recovery_cases
set conversation_id = 'dddddddd-dddd-4ddd-8ddd-ddddddddddd1'::uuid
where id = '${recoveryCaseId}'::uuid;
update public.followup_sequences
set conversation_id = 'dddddddd-dddd-4ddd-8ddd-ddddddddddd1'::uuid
where recovery_case_id = '${recoveryCaseId}'::uuid;
update public.scheduled_actions
set conversation_id = 'dddddddd-dddd-4ddd-8ddd-ddddddddddd1'::uuid
where recovery_case_id = '${recoveryCaseId}'::uuid;

insert into public.followup_delivery_attempts (
  action_id, idempotency_key, attempt_number, channel, mode, phase,
  started_at, lease_generation, expected_case_version,
  expected_sequence_revision
)
select
  sa.id, sa.idempotency_key, 1, 'whatsapp', 'freeform', 'reserved',
  '2026-08-10T00:04:00Z', 1, rc.version, fs.revision
from public.scheduled_actions sa
join public.recovery_cases rc on rc.id = sa.recovery_case_id
join public.followup_sequences fs on fs.id = sa.followup_sequence_id
where sa.id = '${actionId}'::uuid;
`);

let unfencedAgentRejected = false;
try {
  await db.query(`
    select * from public.request_human_handoff(
      '${recoveryCaseId}'::uuid,
      'handoff:fixture:unfenced-agent',
      'commercial_exception',
      'agent',
      'lancemos-handoff',
      1,
      null, null, null, null,
      '2026-08-10T00:03:59Z'::timestamptz
    );
  `);
} catch (error) {
  unfencedAgentRejected = String(error.message).includes(
    'invalid_human_handoff_parameters',
  );
}
if (!unfencedAgentRejected) throw new Error('unfenced agent handoff was admitted');

const requested = await db.query(`
select * from public.request_human_handoff(
  '${recoveryCaseId}'::uuid,
  'handoff:fixture:reserved',
  'explicit_human_request',
  'operator',
  'lancemos-handoff',
  1
);
`);
if (requested.rows[0]?.outcome !== 'requested') {
  throw new Error(`expected requested: ${JSON.stringify(requested.rows)}`);
}
const handoffRequestId = requested.rows[0].handoff_request_id;

const stopped = await db.query(`
select
  rc.status as case_status,
  rc.next_contact_reason,
  fs.status as sequence_status,
  fs.cancel_reason,
  sa.status as action_status,
  sa.terminal_reason,
  attempt.phase as attempt_phase,
  attempt.outcome as attempt_outcome,
  attempt.reason_code as attempt_reason,
  conversation.status as conversation_status,
  conversation.automation_status,
  request.expected_team_id,
  request.status as request_status,
  (select count(*)::int
   from public.human_handoff_projection_effects effect
   where effect.handoff_request_id = request.id) as effect_count
from public.recovery_cases rc
join public.followup_sequences fs on fs.recovery_case_id = rc.id
join public.scheduled_actions sa on sa.recovery_case_id = rc.id
join public.followup_delivery_attempts attempt on attempt.action_id = sa.id
join public.conversations conversation on conversation.id = rc.conversation_id
join public.human_handoff_requests request on request.recovery_case_id = rc.id
where rc.id = '${recoveryCaseId}'::uuid;
`);
const state = stopped.rows[0];
if (state?.case_status !== 'paused'
    || state?.next_contact_reason !== 'human_handoff_requested'
    || state?.sequence_status !== 'paused'
    || state?.cancel_reason !== 'human_handoff_requested'
    || state?.action_status !== 'cancelled'
    || state?.terminal_reason !== 'human_handoff_requested'
    || state?.attempt_phase !== 'completed'
    || state?.attempt_outcome !== 'failed_before_request'
    || state?.attempt_reason !== 'human_handoff_requested'
    || state?.conversation_status !== 'paused_human'
    || state?.automation_status !== 'paused'
    || Number(state?.expected_team_id) !== 17
    || state?.request_status !== 'requested'
    || Number(state?.effect_count) !== 2) {
  throw new Error(`unexpected durable handoff state: ${JSON.stringify(state)}`);
}

const replay = await db.query(`
select * from public.request_human_handoff(
  '${recoveryCaseId}'::uuid,
  'handoff:fixture:reserved',
  'explicit_human_request',
  'operator',
  'lancemos-handoff',
  1
);
`);
if (replay.rows[0]?.outcome !== 'already_requested'
    || replay.rows[0]?.handoff_request_id !== handoffRequestId) {
  throw new Error(`handoff replay was not idempotent: ${JSON.stringify(replay.rows)}`);
}

const evidence = await db.query(`
select * from public.request_human_handoff(
  '${recoveryCaseId}'::uuid,
  'handoff:fixture:second-reason',
  'commercial_exception',
  'operator',
  'lancemos-handoff',
  1
);
`);
if (evidence.rows[0]?.outcome !== 'evidence_appended'
    || evidence.rows[0]?.handoff_request_id !== handoffRequestId) {
  throw new Error(`second reason did not reuse request: ${JSON.stringify(evidence.rows)}`);
}
const cardinality = await db.query(`
select
  (select count(*)::int from public.human_handoff_requests
   where recovery_case_id = '${recoveryCaseId}'::uuid) as request_count,
  (select count(*)::int from public.human_handoff_request_evidence
   where handoff_request_id = '${handoffRequestId}'::uuid) as evidence_count,
  (select count(*)::int from public.conversation_events
   where recovery_case_id = '${recoveryCaseId}'::uuid
     and event_type = 'human_handoff_requested') as event_count;
`);
if (Number(cardinality.rows[0]?.request_count) !== 1
    || Number(cardinality.rows[0]?.evidence_count) !== 1
    || Number(cardinality.rows[0]?.event_count) !== 2) {
  throw new Error(`unexpected handoff cardinality: ${JSON.stringify(cardinality.rows[0])}`);
}

let conflictRejected = false;
try {
  await db.query(`
    select * from public.request_human_handoff(
      '${recoveryCaseId}'::uuid,
      'handoff:fixture:reserved',
      'commercial_exception',
      'operator',
      'lancemos-handoff',
      1
    );
  `);
} catch (error) {
  conflictRejected = String(error?.message ?? error).includes(
    'human_handoff_command_conflict',
  );
}
if (!conflictRejected) throw new Error('conflicting command replay was accepted');

let evidenceConflictRejected = false;
try {
  await db.query(`
    select * from public.request_human_handoff(
      '${recoveryCaseId}'::uuid,
      'handoff:fixture:second-reason',
      'policy_requires_human',
      'operator',
      'lancemos-handoff',
      1
    );
  `);
} catch (error) {
  evidenceConflictRejected = String(error?.message ?? error).includes(
    'human_handoff_command_conflict',
  );
}
if (!evidenceConflictRejected) {
  throw new Error('conflicting evidence replay was accepted');
}

await db.exec(`
  insert into public.human_handoff_projection_policies (
    policy_key, policy_version, scope_key, scope_version, expected_team_id,
    note_template_key, note_template_version, private_note_body, active
  ) values (
    'lancemos-handoff', 2, 'handoff-pilot', 1, 17,
    'handoff-note', 2, 'Nueva plantilla que no debe cambiar el request.', true
  );
`);
let policySwitchRejected = false;
try {
  await db.query(`
    select * from public.request_human_handoff(
      '${recoveryCaseId}'::uuid,
      'handoff:fixture:policy-switch',
      'commercial_exception',
      'operator',
      'lancemos-handoff',
      2
    );
  `);
} catch (error) {
  policySwitchRejected = String(error?.message ?? error).includes(
    'human_handoff_command_conflict',
  );
}
if (!policySwitchRejected) {
  throw new Error('existing handoff accepted a different policy snapshot');
}

let scopeMismatchRejected = false;
try {
  await db.query(`
    select * from public.request_human_handoff(
      '${recoveryCaseId}'::uuid,
      'handoff:fixture:foreign-scope',
      'commercial_exception',
      'operator',
      'foreign-handoff',
      1
    );
  `);
} catch (error) {
  scopeMismatchRejected = String(error?.message ?? error).includes(
    'handoff_pilot_scope_mismatch',
  );
}
if (!scopeMismatchRejected) throw new Error('foreign pilot scope was accepted');

for (const [statement, expectedError] of [
  [
    `update public.human_handoff_projection_policies
     set expected_team_id = 99 where policy_key = 'lancemos-handoff'`,
    'human_handoff_policy_version_is_immutable',
  ],
  [
    `update public.human_handoff_requests set expected_team_id = 99
     where id = '${handoffRequestId}'::uuid`,
    'human_handoff_request_identity_is_immutable',
  ],
  [
    `delete from public.human_handoff_request_evidence
     where handoff_request_id = '${handoffRequestId}'::uuid`,
    'human_handoff_evidence_is_append_only',
  ],
  [
    `update public.human_handoff_projection_effects set effect_kind = 'assignment'
     where handoff_request_id = '${handoffRequestId}'::uuid
       and effect_kind = 'private_note'`,
    'human_handoff_projection_effect_identity_is_immutable',
  ],
]) {
  let mutationRejected = false;
  try {
    await db.exec(statement);
  } catch (error) {
    mutationRejected = String(error?.message ?? error).includes(expectedError);
  }
  if (!mutationRejected) {
    throw new Error(`immutable handoff row was mutable: ${expectedError}`);
  }
}

await db.exec(`
  update public.human_handoff_projection_policies
  set active = false
  where policy_key = 'lancemos-handoff' and policy_version = 1;
`);

const claimed = await db.query(`
select * from public.claim_human_handoff_projection_effects(
  'handoff-worker-1', 10, 60, '2026-08-10T00:10:00Z'::timestamptz
);
`);
if (claimed.rows.length !== 2
    || claimed.rows[0]?.effect_kind !== 'assignment'
    || claimed.rows[1]?.effect_kind !== 'private_note'
    || Number(claimed.rows[0]?.expected_team_id) !== 17
    || Number(claimed.rows[0]?.chatwoot_account_id) !== 1
    || Number(claimed.rows[0]?.chatwoot_inbox_id) !== 7
    || Number(claimed.rows[0]?.chatwoot_conversation_id) !== 42
    || !claimed.rows[1]?.idempotency_marker?.includes(handoffRequestId)
    || !claimed.rows[1]?.private_note_body?.includes('intervención humana')) {
  throw new Error(`unexpected handoff claims: ${JSON.stringify(claimed.rows)}`);
}

let staleLeaseRejected = false;
try {
  await db.query(`
    select * from public.finalize_human_handoff_projection_effect(
      '${claimed.rows[0].effect_id}'::uuid,
      'handoff-worker-1',
      ${Number(claimed.rows[0].lease_generation) + 1},
      'applied', null, null,
      '2026-08-10T00:10:01Z'::timestamptz
    );
  `);
} catch (error) {
  staleLeaseRejected = String(error?.message ?? error).includes(
    'handoff_projection_lease_fence_rejected',
  );
}
if (!staleLeaseRejected) throw new Error('stale projection lease was accepted');

const assignmentFinalized = await db.query(`
select * from public.finalize_human_handoff_projection_effect(
  '${claimed.rows[0].effect_id}'::uuid,
  'handoff-worker-1',
  ${claimed.rows[0].lease_generation},
  'applied', null, null,
  '2026-08-10T00:10:02Z'::timestamptz
);
`);
if (assignmentFinalized.rows[0]?.effect_status !== 'applied'
    || assignmentFinalized.rows[0]?.handoff_status !== 'projection_failed') {
  throw new Error(
    `assignment finalization mismatch: ${JSON.stringify(assignmentFinalized.rows)}`,
  );
}

const noteFinalized = await db.query(`
select * from public.finalize_human_handoff_projection_effect(
  '${claimed.rows[1].effect_id}'::uuid,
  'handoff-worker-1',
  ${claimed.rows[1].lease_generation},
  'applied', null, null,
  '2026-08-10T00:10:03Z'::timestamptz
);
`);
if (noteFinalized.rows[0]?.effect_status !== 'applied'
    || noteFinalized.rows[0]?.handoff_status !== 'projected') {
  throw new Error(`note finalization mismatch: ${JSON.stringify(noteFinalized.rows)}`);
}

const noDuplicateClaims = await db.query(`
select * from public.claim_human_handoff_projection_effects(
  'handoff-worker-2', 10, 60, '2026-08-10T00:11:00Z'::timestamptz
);
`);
if (noDuplicateClaims.rows.length !== 0) {
  throw new Error(`applied effects were reclaimed: ${JSON.stringify(noDuplicateClaims.rows)}`);
}

await db.exec(`
  update public.human_handoff_projection_effects
  set effect_status = case
        when effect_kind = 'assignment' then 'dead_letter'
        else 'retryable_failed'
      end,
      last_error_code = case
        when effect_kind = 'assignment' then 'assignment_terminal'
        else 'note_retry'
      end,
      applied_at = null,
      next_attempt_at = null,
      lease_owner = null,
      lease_expires_at = null
  where handoff_request_id = '${handoffRequestId}'::uuid;
  update public.human_handoff_requests
  set status = 'dead_letter',
      projected_at = null,
      last_error_code = 'assignment_terminal'
  where id = '${handoffRequestId}'::uuid;
`);
const deadSiblingDrain = await db.query(`
  select * from public.claim_human_handoff_projection_effects(
    'handoff-worker-dead-sibling', 10, 60, clock_timestamp()
  );
`);
if (deadSiblingDrain.rows.length !== 1
    || deadSiblingDrain.rows[0]?.effect_kind !== 'private_note') {
  throw new Error(
    `dead-letter sibling blocked projection drain: ${JSON.stringify(deadSiblingDrain.rows)}`,
  );
}

const privileges = await db.query(`
select
  has_table_privilege('anon', 'public.human_handoff_requests', 'select')
    as anon_table,
  has_table_privilege(
    'authenticated', 'public.human_handoff_request_evidence', 'insert'
  ) as authenticated_table,
  has_table_privilege(
    'service_role', 'public.human_handoff_projection_effects', 'update'
  ) as service_table,
  has_function_privilege(
    'anon',
    'public.request_human_handoff(uuid,text,text,text,text,integer,uuid,uuid,text,bigint,timestamptz)',
    'execute'
  ) as anon_request,
  has_function_privilege(
    'service_role',
    'public.request_human_handoff(uuid,text,text,text,text,integer,uuid,uuid,text,bigint,timestamptz)',
    'execute'
  ) as service_request,
  has_function_privilege(
    'service_role',
    'public.protect_human_handoff_request_identity()',
    'execute'
  ) as service_helper;
`);
const acl = privileges.rows[0];
if (acl?.anon_table !== false
    || acl?.authenticated_table !== false
    || acl?.service_table !== false
    || acl?.anon_request !== false
    || acl?.service_request !== true
    || acl?.service_helper !== false) {
  throw new Error(`unexpected handoff privileges: ${JSON.stringify(acl)}`);
}

console.log('HANDOFF_DURABLE_STOP_OK');
console.log('HANDOFF_REPLAY_AND_EVIDENCE_OK');
console.log('HANDOFF_PROJECTION_LEASES_OK');
console.log('HANDOFF_EFFECTIVE_ACL_OK');
await db.close();
