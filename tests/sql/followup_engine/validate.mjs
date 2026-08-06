import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { PGlite } from '@electric-sql/pglite';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const baseline = (await readFile(`${root}/supabase/baseline/20260803_public_schema.sql`, 'utf8'))
  .replace('create extension if not exists pgcrypto;', '-- omitted in PGlite: extension unavailable');
const migration = await readFile(`${root}/supabase/migrations/20260803000100_followup_engine_v1.sql`, 'utf8');
const identityBindingMigration = await readFile(
  `${root}/supabase/migrations/20260804000200_followup_identity_binding.sql`,
  'utf8',
);
const identityAuditMigration = await readFile(
  `${root}/supabase/migrations/20260805000100_followup_identity_audit.sql`,
  'utf8',
);
const contactAuthorizationGrantMigration = await readFile(
  `${root}/supabase/migrations/20260805000200_followup_contact_authorization_grant.sql`,
  'utf8',
);
const perCaseAnchorMigration = await readFile(
  `${root}/supabase/migrations/20260805000300_per_case_conversation_anchor.sql`,
  'utf8',
);
const finalE2ePolicySeed = await readFile(
  `${root}/supabase/seeds/20260806000100_cart_recovery_e2e_final_v1.sql`,
  'utf8',
);
const db = new PGlite();
await db.waitReady;
await db.exec(baseline);
console.log('baseline_apply=OK');
await db.exec(migration);
console.log('migration_apply=OK');
await db.exec(identityBindingMigration);
console.log('identity_binding_migration_apply=OK');
await db.exec(identityAuditMigration);
console.log('identity_audit_migration_apply=OK');
await db.exec(contactAuthorizationGrantMigration);
console.log('contact_authorization_grant_migration_apply=OK');
await db.exec(perCaseAnchorMigration);
console.log('per_case_anchor_migration_apply=OK');
await db.exec(finalE2ePolicySeed);
const finalE2ePolicy = await db.query(`
  select grace_period = interval '0 seconds' as immediate_first_message,
         expires_after = interval '1 hour' as expires_in_one_hour,
         max_automatic_messages = 4 as allows_four_messages,
         approved_by = 'operator-e2e-final-policy' as approval_matches,
         steps = '[{"step_key":"first_contact","mode":"freeform"},{"step_key":"followup_1","delay":"2 minutes","mode":"freeform"},{"step_key":"followup_2","delay":"5 minutes","mode":"freeform"},{"step_key":"followup_3","delay":"10 minutes","mode":"freeform"}]'::jsonb as steps_match
  from public.followup_policy_versions
  where policy_key = 'cart-recovery-e2e-final'
    and version = 1
    and status = 'published'
`);
if (finalE2ePolicy.rows.length !== 1
    || !finalE2ePolicy.rows[0].immediate_first_message
    || !finalE2ePolicy.rows[0].expires_in_one_hour
    || !finalE2ePolicy.rows[0].allows_four_messages
    || !finalE2ePolicy.rows[0].approval_matches
    || !finalE2ePolicy.rows[0].steps_match) {
  throw new Error('final E2E policy does not match the approved 0/2/5/10 schedule');
}
console.log('final_e2e_policy=OK');

async function authorizeExecute(actionId, workerId, leaseGeneration = 1, caseVersion = 1, sequenceRevision = 1) {
  await db.query(`
    insert into public.contact_authorizations (
      contact_id, channel, purpose, authorization_status,
      authorization_source, valid_from
    )
    select rc.contact_id, 'whatsapp', 'cart_recovery', 'allowed',
           'system', now() - interval '1 minute'
    from public.scheduled_actions sa
    join public.recovery_cases rc on rc.id=sa.recovery_case_id
    where sa.id=$1
      and not exists (
        select 1 from public.contact_authorizations ca
        where ca.contact_id=rc.contact_id
          and ca.channel='whatsapp'
          and ca.purpose='cart_recovery'
          and ca.authorization_status='allowed'
          and ca.valid_from <= now()
          and (ca.valid_until is null or ca.valid_until > now())
      )
  `, [actionId]);
  await db.query(`
    insert into public.conversation_events (
      recovery_case_id, event_type, actor_type, related_action_id, data
    )
    select sa.recovery_case_id, 'followup_action_reevaluated', 'system', sa.id,
           jsonb_build_object(
             'decision', 'execute', 'reason_code', 'test_execute_authorization',
             'worker_id', $2::text, 'lease_generation', $3::bigint,
             'case_version', $4::bigint, 'sequence_revision', $5::bigint
           )
    from public.scheduled_actions sa
    where sa.id = $1
  `, [actionId, workerId, leaseGeneration, caseVersion, sequenceRevision]);
}
const tables = await db.query(`select count(*)::int as count from information_schema.tables where table_schema='public' and table_type='BASE TABLE'`);
const functions = await db.query(`select count(*)::int as count from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public'`);
const columns = await db.query(`select count(*)::int as count from information_schema.columns where table_schema='public' and table_name='scheduled_actions' and column_name in ('next_attempt_at','lease_owner','lease_generation','lease_expires_at','step_key')`);
console.log(`public_tables=${tables.rows[0].count}`);
console.log(`public_functions=${functions.rows[0].count}`);
console.log(`required_action_columns=${columns.rows[0].count}`);

await db.exec(`
  insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
  ) values (
    'cart-recovery-test', 1, 'published', 'cart_recovery', 'UTC',
    '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
    interval '1 hour', interval '7 days', 3,
    '[{"step_key":"first_contact","mode":"freeform"},{"step_key":"followup_1","delay":"24 hours","mode":"freeform"}]'::jsonb,
    'operator-test', now(), now()
  );
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values (
    '00000000-0000-0000-0000-000000000001', 'hotmart', 'schema-check-event',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
  insert into public.contacts (id, full_name) values (
    '00000000-0000-0000-0000-000000000002', 'Schema Check'
  );
  insert into public.channel_identities (
    id, contact_id, channel, account_id, external_user_id, identity_status
  ) values (
    '00000000-0000-0000-0000-000000000003',
    '00000000-0000-0000-0000-000000000002',
    'whatsapp', 'chatwoot:1', 'authorized-test-user', 'active'
  );
`);

const plan1 = await db.query(`
  select * from public.plan_cart_recovery(
    '00000000-0000-0000-0000-000000000001',
    '00000000-0000-0000-0000-000000000002',
    'product-test', 'Product Test', 'offer-test',
    'cart-recovery-test', 1, now() - interval '2 hours'
  )
`);
const plan2 = await db.query(`
  select * from public.plan_cart_recovery(
    '00000000-0000-0000-0000-000000000001',
    '00000000-0000-0000-0000-000000000002',
    'product-test', 'Product Test', 'offer-test',
    'cart-recovery-test', 1, now() - interval '2 hours'
  )
`);
if (plan1.rows.length !== 1 || plan1.rows[0].created !== true) throw new Error('first plan did not create');
if (plan2.rows.length !== 1 || plan2.rows[0].created !== false) throw new Error('second plan was not idempotent');
if (plan1.rows[0].recovery_case_id !== plan2.rows[0].recovery_case_id) throw new Error('case id changed');
await db.query(`
  update public.recovery_cases
  set selected_channel_identity_id='00000000-0000-0000-0000-000000000003',
      identity_resolution_status='resolved'
  where id=$1
`, [plan1.rows[0].recovery_case_id]);
console.log('plan_idempotency=OK');

await db.exec(`
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values (
    '00000000-0000-0000-0000-000000000011', 'hotmart',
    'identity-binding-event', 'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
  insert into public.contacts (id, full_name) values (
    '00000000-0000-0000-0000-000000000012', 'Identity Binding Check'
  );
`);
const identityPlan1 = await db.query(`
  select * from public.plan_cart_recovery_with_identity(
    '00000000-0000-0000-0000-000000000011',
    '00000000-0000-0000-0000-000000000012',
    'identity-product', 'Identity Product', 'identity-offer',
    'cart-recovery-test', 1, timestamptz '2099-01-01 00:00:00+00',
    1, 7, '5531999999999'
  )
`);
const identityPlan2 = await db.query(`
  select * from public.plan_cart_recovery_with_identity(
    '00000000-0000-0000-0000-000000000011',
    '00000000-0000-0000-0000-000000000012',
    'identity-product', 'Identity Product', 'identity-offer',
    'cart-recovery-test', 1, timestamptz '2099-01-01 00:00:00+00',
    1, 7, '5531999999999'
  )
`);
if (identityPlan1.rows.length !== 1 || identityPlan1.rows[0].created !== true) {
  throw new Error('identity plan did not create');
}
if (identityPlan2.rows.length !== 1 || identityPlan2.rows[0].created !== false) {
  throw new Error('identity plan replay was not idempotent');
}
const identityBinding = await db.query(`
  select rc.identity_resolution_status,
         rc.identity_resolution_attempt_count,
         rc.identity_resolution_last_attempt_at is not null as has_last_attempt,
         rc.selected_channel_identity_id,
         ci.contact_id,
         ci.account_id,
         ci.external_user_id,
         ci.metadata ->> 'inbox_id' as inbox_id,
         (select count(*)::int from public.channel_identities x
          where x.channel='whatsapp'
            and x.account_id='chatwoot:1'
            and x.external_user_id='5531999999999') as identity_count
  from public.recovery_cases rc
  join public.channel_identities ci on ci.id=rc.selected_channel_identity_id
  where rc.id=$1
`, [identityPlan1.rows[0].recovery_case_id]);
const identityAttempts = await db.query(`
  select status, strategy, matched_channel_identity_id,
         evidence ->> 'source' as evidence_source
  from public.identity_resolution_attempts
  where recovery_case_id=$1
`, [identityPlan1.rows[0].recovery_case_id]);
const bound = identityBinding.rows[0];
if (!bound
    || bound.identity_resolution_status !== 'resolved'
    || bound.identity_resolution_attempt_count !== 1
    || bound.has_last_attempt !== true
    || bound.contact_id !== '00000000-0000-0000-0000-000000000012'
    || bound.account_id !== 'chatwoot:1'
    || bound.external_user_id !== '5531999999999'
    || bound.inbox_id !== '7'
    || bound.identity_count !== 1) {
  throw new Error('identity binding invariant failed');
}
const identityAttempt = identityAttempts.rows[0];
if (identityAttempts.rows.length !== 1
    || identityAttempt.status !== 'matched'
    || identityAttempt.strategy !== 'other'
    || identityAttempt.matched_channel_identity_id !== bound.selected_channel_identity_id
    || identityAttempt.evidence_source !== 'selected_channel_identity_transition') {
  throw new Error('identity audit invariant failed');
}
console.log('identity_binding_atomic_replay=OK');
console.log('identity_audit_atomic_replay=OK');

// Cart abandonment IS the authorization: the atomic plan must have granted
// exactly one active 'allowed' whatsapp/cart_recovery authorization, sourced
// from hotmart with cart_abandonment evidence, and the plan replay above must
// NOT have produced a duplicate.
const grantRows = await db.query(`
  select authorization_status, authorization_source, purpose, channel,
         evidence ->> 'reason' as reason,
         evidence ->> 'webhook_event_id' as webhook_event_id
  from public.contact_authorizations
  where contact_id='00000000-0000-0000-0000-000000000012'
    and channel='whatsapp'
    and purpose='cart_recovery'
`);
if (grantRows.rows.length !== 1) {
  throw new Error('cart-abandonment authorization grant was not idempotent');
}
const grant = grantRows.rows[0];
if (grant.authorization_status !== 'allowed'
    || grant.authorization_source !== 'hotmart'
    || grant.reason !== 'cart_abandonment'
    || grant.webhook_event_id !== '00000000-0000-0000-0000-000000000011') {
  throw new Error('cart-abandonment authorization grant invariant failed');
}
console.log('contact_authorization_grant_atomic_replay=OK');

// Opt-out is authoritative: an active denial recorded BEFORE planning must not
// be overwritten by the auto-grant, and no extra 'allowed' row may appear.
await db.exec(`
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values (
    '00000000-0000-0000-0000-000000000021', 'hotmart',
    'optout-respect-event', 'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
  insert into public.contacts (id, full_name) values (
    '00000000-0000-0000-0000-000000000022', 'Opt Out Respect Check'
  );
  insert into public.contact_authorizations (
    contact_id, channel, purpose, authorization_status,
    authorization_source, valid_from
  ) values (
    '00000000-0000-0000-0000-000000000022', 'whatsapp', 'cart_recovery',
    'denied', 'manual', now() - interval '1 minute'
  );
`);
await db.query(`
  select * from public.plan_cart_recovery_with_identity(
    '00000000-0000-0000-0000-000000000021',
    '00000000-0000-0000-0000-000000000022',
    'optout-product', 'Opt Out Product', 'optout-offer',
    'cart-recovery-test', 1, timestamptz '2099-01-01 00:00:00+00',
    1, 7, '5531988888888'
  )
`);
const optoutRows = await db.query(`
  select authorization_status, authorization_source
  from public.contact_authorizations
  where contact_id='00000000-0000-0000-0000-000000000022'
    and channel='whatsapp'
    and purpose='cart_recovery'
  order by valid_from
`);
if (optoutRows.rows.length !== 1
    || optoutRows.rows[0].authorization_status !== 'denied'
    || optoutRows.rows[0].authorization_source !== 'manual') {
  throw new Error('auto-grant overrode an active opt-out');
}
console.log('contact_authorization_optout_respected=OK');
await db.exec(`
  delete from public.scheduled_actions where recovery_case_id in (
    select id from public.recovery_cases
    where contact_id='00000000-0000-0000-0000-000000000012'
  );
  delete from public.followup_sequences where recovery_case_id in (
    select id from public.recovery_cases
    where contact_id='00000000-0000-0000-0000-000000000012'
  );
  delete from public.recovery_case_events where recovery_case_id in (
    select id from public.recovery_cases
    where contact_id='00000000-0000-0000-0000-000000000012'
  );
  delete from public.recovery_cases
  where contact_id='00000000-0000-0000-0000-000000000012';
  delete from public.channel_identities
  where contact_id='00000000-0000-0000-0000-000000000012';
  delete from public.contacts
  where id='00000000-0000-0000-0000-000000000012';
  delete from public.webhook_events
  where id='00000000-0000-0000-0000-000000000011';
  delete from public.scheduled_actions where recovery_case_id in (
    select id from public.recovery_cases
    where contact_id='00000000-0000-0000-0000-000000000022'
  );
  delete from public.followup_sequences where recovery_case_id in (
    select id from public.recovery_cases
    where contact_id='00000000-0000-0000-0000-000000000022'
  );
  delete from public.recovery_case_events where recovery_case_id in (
    select id from public.recovery_cases
    where contact_id='00000000-0000-0000-0000-000000000022'
  );
  delete from public.recovery_cases
  where contact_id='00000000-0000-0000-0000-000000000022';
  delete from public.channel_identities
  where contact_id='00000000-0000-0000-0000-000000000022';
  delete from public.contact_authorizations
  where contact_id='00000000-0000-0000-0000-000000000022';
  delete from public.contacts
  where id='00000000-0000-0000-0000-000000000022';
  delete from public.webhook_events
  where id='00000000-0000-0000-0000-000000000021';
`);
console.log('identity_binding_probe_cleanup=OK');

const claim = await db.query(`
  select * from public.claim_due_followup_actions(
    'schema-check-worker', now(), interval '5 minutes', 1
  )
`);
if (claim.rows.length !== 1 || claim.rows[0].lease_generation !== 1) throw new Error('claim failed');
const actionId = claim.rows[0].id;
const claimAudit = await db.query(`
  select data ->> 'worker_id' as worker_id,
         (data ->> 'lease_generation')::int as lease_generation
  from public.conversation_events
  where related_action_id=$1 and event_type='followup_action_claimed'
`, [actionId]);
if (claimAudit.rows.length !== 1 || claimAudit.rows[0].worker_id !== 'schema-check-worker' || claimAudit.rows[0].lease_generation !== 1) throw new Error('claim audit missing');
console.log('claim_lease=OK');

let directReserveBlocked = false;
try {
  await db.query(`
    select * from public.reserve_followup_delivery_attempt(
      $1, 'schema-check-worker', 1, 1, 1, 'whatsapp', 'freeform', now()
    )
  `, [actionId]);
} catch (error) {
  directReserveBlocked = String(error.message).includes('followup_execute_authorization_not_found');
}
if (!directReserveBlocked) throw new Error('direct reservation bypassed reevaluation authorization');
console.log('direct_reservation_without_execute_blocked=OK');
await authorizeExecute(actionId, 'schema-check-worker');
const reserve = await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1, 'schema-check-worker', 1, 1, 1, 'whatsapp', 'freeform', now()
  )
`, [actionId]);
if (reserve.rows.length !== 1 || reserve.rows[0].phase !== 'reserved') throw new Error('reserve failed');
const attemptId = reserve.rows[0].id;
let incompatibleReserveReplayBlocked = false;
try {
  await db.query(`
    select * from public.reserve_followup_delivery_attempt(
      $1, 'schema-check-worker', 1, 1, 1, 'email', 'approved_template', now()
    )
  `, [actionId]);
} catch (error) {
  incompatibleReserveReplayBlocked = String(error.message).includes('delivery_attempt_already_reserved_differently');
}
if (!incompatibleReserveReplayBlocked) throw new Error('incompatible reserve replay was accepted');
console.log('attempt_reservation=OK');

let nullWorkerRequestStartBlocked = false;
try {
  await db.query(`
    select * from public.mark_followup_request_started(
      $1, $2, null, 1, now()
    )
  `, [actionId, attemptId]);
} catch (error) {
  nullWorkerRequestStartBlocked = String(error.message).includes(
    'invalid_request_start_parameters'
  );
}
if (!nullWorkerRequestStartBlocked) throw new Error('null worker bypassed request-start fence');
console.log('null_request_start_fence=OK');

const started = await db.query(`
  select * from public.mark_followup_request_started(
    $1, $2, 'schema-check-worker', 1, now()
  )
`, [actionId, attemptId]);
if (started.rows[0].phase !== 'request_started') throw new Error('request start failed');
console.log('request_started=OK');

const finalized = await db.query(`
  select * from public.record_and_finalize_followup_acceptance(
    $1, $2, 'schema-check-worker', 1,
    '7001', '8001', 'Mensaje canónico de prueba', now()
  )
`, [actionId, attemptId]);
if (finalized.rows[0].status !== 'accepted_by_chatwoot') throw new Error('finalize failed');
const canonicalAcceptance = await db.query(`
  select rc.conversation_id as case_conversation_id,
         fs.conversation_id as sequence_conversation_id,
         fda.accepted_message_id,
         m.external_message_id,
         m.direction,
         m.delivery_status,
         m.semantic_metadata ->> 'attempt_id' as message_attempt_id,
         c.commercial_context ->> 'chatwoot_conversation_id' as external_conversation_id,
         ci.external_conversation_id as identity_external_conversation_id
  from public.followup_delivery_attempts fda
  join public.scheduled_actions sa on sa.id=fda.action_id
  join public.recovery_cases rc on rc.id=sa.recovery_case_id
  join public.followup_sequences fs on fs.id=sa.followup_sequence_id
  join public.messages m on m.id=fda.accepted_message_id
  join public.conversations c on c.id=m.conversation_id
  join public.channel_identities ci on ci.id=c.channel_identity_id
  where fda.id=$1
`, [attemptId]);
if (canonicalAcceptance.rows.length !== 1) throw new Error('canonical acceptance missing');
if (canonicalAcceptance.rows[0].case_conversation_id !== canonicalAcceptance.rows[0].sequence_conversation_id) throw new Error('conversation links differ');
if (canonicalAcceptance.rows[0].external_message_id !== '8001' || canonicalAcceptance.rows[0].external_conversation_id !== '7001') throw new Error('canonical external identity mismatch');
if (canonicalAcceptance.rows[0].identity_external_conversation_id !== '7001') throw new Error('channel identity was not linked to canonical conversation');
if (canonicalAcceptance.rows[0].message_attempt_id !== attemptId) throw new Error('canonical message correlation mismatch');
if (canonicalAcceptance.rows[0].direction !== 'outbound' || canonicalAcceptance.rows[0].delivery_status !== 'accepted') throw new Error('canonical message state mismatch');
const finalizedAgain = await db.query(`
  select * from public.record_and_finalize_followup_acceptance(
    $1, $2, 'schema-check-worker', 1,
    '7001', '8001', 'Mensaje canónico de prueba', now()
  )
`, [actionId, attemptId]);
if (finalizedAgain.rows[0].status !== 'accepted_by_chatwoot') throw new Error('idempotent finalize failed');
const canonicalCounts = await db.query(`
  select
    (select count(*)::int from public.conversations where commercial_context ->> 'chatwoot_conversation_id'='7001') as conversations,
    (select count(*)::int from public.messages where external_message_id='8001') as messages
`);
if (canonicalCounts.rows[0].conversations !== 1 || canonicalCounts.rows[0].messages !== 1) throw new Error('acceptance replay duplicated canonical rows');
let genericAcceptanceBlocked = false;
try {
  await db.query(`
    select * from public.finalize_followup_delivery_attempt(
      $1, $2, 'schema-check-worker', 1, 'accepted_by_chatwoot',
      '8001', $3, 'accepted_by_chatwoot', null, null, now()
    )
  `, [actionId, attemptId, canonicalAcceptance.rows[0].accepted_message_id]);
} catch (error) {
  genericAcceptanceBlocked = String(error.message).includes(
    'canonical_acceptance_required'
  );
}
if (!genericAcceptanceBlocked) throw new Error('generic finalizer bypassed canonical acceptance');
console.log('generic_acceptance_bypass_blocked=OK');
let changedFinalizeAnchorBlocked = false;
try {
  await db.query(`
    select * from public.record_and_finalize_followup_acceptance(
      $1, $2, 'schema-check-worker', 1,
      '7001', 'different-message', 'Mensaje canónico de prueba', now()
    )
  `, [actionId, attemptId]);
} catch (error) {
  changedFinalizeAnchorBlocked = String(error.message).includes('delivery_attempt_already_finalized_differently');
}
if (!changedFinalizeAnchorBlocked) throw new Error('finalize accepted a different durable anchor');
const state = await db.query(`
  select rc.status as case_status, fs.current_step,
         fs.automatic_messages_accepted, sa.status as action_status
  from public.recovery_cases rc
  join public.followup_sequences fs on fs.recovery_case_id = rc.id
  join public.scheduled_actions sa on sa.followup_sequence_id = fs.id
`);
if (state.rows[0].case_status !== 'active') throw new Error('case did not activate');
if (state.rows[0].current_step !== 1 || state.rows[0].automatic_messages_accepted !== 1) throw new Error('sequence did not advance');
const successor = await db.query(`
  select id, action_type, status, anchor_type, anchor_subject_internal_id,
         expected_case_version
  from public.scheduled_actions
  where followup_sequence_id = $1 and status = 'pending'
`, [plan1.rows[0].followup_sequence_id]);
if (successor.rows.length !== 1 || successor.rows[0].action_type !== 'no_reply_review') throw new Error('next review missing');
if (successor.rows[0].anchor_type !== 'accepted_outbound_message') throw new Error('next review anchor missing');

const successorFence = await db.query(`
  select revision from public.followup_sequences where id=$1
`, [plan1.rows[0].followup_sequence_id]);
const lateClaim = await db.query(`
  select * from public.claim_due_followup_actions(
    'late-optout-worker', now() + interval '25 hours', interval '5 minutes', 10
  )
`);
const lateAction = lateClaim.rows.find((row) => row.id === successor.rows[0].id);
if (!lateAction) throw new Error('late opt-out successor not claimed');
await authorizeExecute(
  lateAction.id,
  'late-optout-worker',
  lateAction.lease_generation,
  successor.rows[0].expected_case_version,
  successorFence.rows[0].revision,
);
const lateAttempt = await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1, 'late-optout-worker', $2, $3, $4,
    'whatsapp', 'freeform', now() + interval '25 hours'
  )
`, [
  lateAction.id,
  lateAction.lease_generation,
  successor.rows[0].expected_case_version,
  successorFence.rows[0].revision,
]);
await db.query(`
  select * from public.mark_followup_request_started(
    $1, $2, 'late-optout-worker', $3, now() + interval '25 hours'
  )
`, [lateAction.id, lateAttempt.rows[0].id, lateAction.lease_generation]);
await db.query(`
  update public.contacts set contact_permission='opted_out'
  where id='00000000-0000-0000-0000-000000000002'
`);
await db.query(`
  select * from public.record_and_finalize_followup_acceptance(
    $1, $2, 'late-optout-worker', $3,
    '7001', '8002', 'Mensaje aceptado durante opt-out',
    now() + interval '25 hours'
  )
`, [lateAction.id, lateAttempt.rows[0].id, lateAction.lease_generation]);
const lateOptoutState = await db.query(`
  select sa.status, sa.terminal_reason, fda.outcome,
         fs.automatic_messages_accepted,
         (select count(*)::int from public.scheduled_actions x
          where x.followup_sequence_id=fs.id) as action_count
  from public.scheduled_actions sa
  join public.followup_delivery_attempts fda on fda.action_id=sa.id
  join public.followup_sequences fs on fs.id=sa.followup_sequence_id
  where sa.id=$1
`, [lateAction.id]);
if (lateOptoutState.rows[0].status !== 'accepted_by_chatwoot' || lateOptoutState.rows[0].outcome !== 'accepted_by_chatwoot') throw new Error('late opt-out acceptance evidence was lost');
if (lateOptoutState.rows[0].automatic_messages_accepted !== 1 || lateOptoutState.rows[0].action_count !== 2) throw new Error('late opt-out acceptance created a successor');
if (!lateOptoutState.rows[0].terminal_reason.includes('authoritative_state_changed_after_reservation')) throw new Error('late opt-out suppression reason missing');
console.log('late_optout_acceptance_without_successor=OK');
console.log('finalize_idempotency=OK');
console.log('accepted_finalization=OK');

await db.exec(`
  insert into public.contacts (id, full_name) values (
    '00000000-0000-0000-0000-000000000009', 'Near Expiry Check'
  );
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values (
    '00000000-0000-0000-0000-000000000010', 'hotmart', 'near-expiry',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
`);
const nearExpiryPlan = await db.query(`
  select * from public.plan_cart_recovery(
    '00000000-0000-0000-0000-000000000010',
    '00000000-0000-0000-0000-000000000009',
    'near-expiry-product', 'Near Expiry Product', null,
    'cart-recovery-test', 1, now() - interval '6 days 23 hours'
  )
`);
const nearExpiryClaim = await db.query(`
  select * from public.claim_due_followup_actions('near-expiry-worker', now(), interval '5 minutes', 1)
`);
const nearExpiryAction = nearExpiryClaim.rows.find((row) => row.id === nearExpiryPlan.rows[0].scheduled_action_id);
if (!nearExpiryAction) throw new Error('near-expiry action not claimed');
await authorizeExecute(nearExpiryAction.id, 'near-expiry-worker');
const nearExpiryAttempt = await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1, 'near-expiry-worker', 1, 1, 1, 'whatsapp', 'freeform', now()
  )
`, [nearExpiryAction.id]);
await db.query(`
  select * from public.mark_followup_request_started(
    $1, $2, 'near-expiry-worker', 1, now()
  )
`, [nearExpiryAction.id, nearExpiryAttempt.rows[0].id]);
await db.query(`
  insert into public.channel_identities (
    id, contact_id, channel, account_id, external_user_id, identity_status
  ) values (
    '00000000-0000-0000-0000-000000000090',
    '00000000-0000-0000-0000-000000000009',
    'whatsapp', 'chatwoot:1', 'near-expiry-user', 'active'
  )
`);
await db.query(`
  update public.recovery_cases
  set selected_channel_identity_id='00000000-0000-0000-0000-000000000090',
      identity_resolution_status='resolved'
  where id=$1
`, [nearExpiryPlan.rows[0].recovery_case_id]);
await db.query(`
  select * from public.record_and_finalize_followup_acceptance(
    $1, $2, 'near-expiry-worker', 1,
    'cw-near-expiry-conversation', 'cw-near-expiry-message',
    'Near expiry accepted message', now()
  )
`, [nearExpiryAction.id, nearExpiryAttempt.rows[0].id]);
const nearExpiryState = await db.query(`
  select sa.status, fs.status as sequence_status, fs.completion_reason,
         (select count(*)::int from public.scheduled_actions x where x.followup_sequence_id=fs.id) as action_count
  from public.scheduled_actions sa
  join public.followup_sequences fs on fs.id=sa.followup_sequence_id
  where sa.id=$1
`, [nearExpiryAction.id]);
if (nearExpiryState.rows[0].status !== 'accepted_by_chatwoot') throw new Error('near-expiry acceptance rolled back');
if (nearExpiryState.rows[0].sequence_status !== 'completed' || nearExpiryState.rows[0].completion_reason !== 'next_step_outside_expiration') throw new Error('near-expiry sequence not completed');
if (nearExpiryState.rows[0].action_count !== 1) throw new Error('near-expiry successor should not exist');
console.log('near_expiry_acceptance_preserved=OK');

await db.exec(`
  insert into public.contacts (id, full_name) values (
    '00000000-0000-0000-0000-000000000012', 'Concurrent State Check'
  );
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values
  (
    '00000000-0000-0000-0000-000000000013', 'hotmart', 'concurrent-state',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  ),
  (
    '00000000-0000-0000-0000-000000000015', 'hotmart', 'concurrent-purchase',
    'PURCHASE_APPROVED', '{}'::jsonb
  );
`);
const concurrentPlan = await db.query(`
  select * from public.plan_cart_recovery(
    '00000000-0000-0000-0000-000000000013',
    '00000000-0000-0000-0000-000000000012',
    'concurrent-product', 'Concurrent Product', null,
    'cart-recovery-test', 1, now() - interval '2 hours'
  )
`);
await db.query(`
  insert into public.channel_identities (
    id, contact_id, channel, account_id, external_user_id, identity_status
  ) values (
    '00000000-0000-0000-0000-000000000091',
    '00000000-0000-0000-0000-000000000012',
    'whatsapp', 'chatwoot:1', 'concurrent-user', 'active'
  )
`);
await db.query(`
  update public.recovery_cases
  set selected_channel_identity_id='00000000-0000-0000-0000-000000000091',
      identity_resolution_status='resolved'
  where id=$1
`, [concurrentPlan.rows[0].recovery_case_id]);
const concurrentClaim = await db.query(`
  select * from public.claim_due_followup_actions(
    'concurrent-worker', now(), interval '5 minutes', 10
  )
`);
const concurrentAction = concurrentClaim.rows.find((row) => row.id === concurrentPlan.rows[0].scheduled_action_id);
if (!concurrentAction) throw new Error('concurrent action not claimed');
await authorizeExecute(concurrentAction.id, 'concurrent-worker');
const concurrentAttempt = await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1, 'concurrent-worker', 1, 1, 1, 'whatsapp', 'freeform', now()
  )
`, [concurrentAction.id]);
await db.query(`
  select * from public.mark_followup_request_started(
    $1, $2, 'concurrent-worker', 1, now()
  )
`, [concurrentAction.id, concurrentAttempt.rows[0].id]);
await db.query(`
  update public.recovery_cases
  set status='won',
      purchase_event_id='00000000-0000-0000-0000-000000000015',
      won_at=now(),
      closed_at=now(),
      version=version+1
  where id=$1
`, [concurrentPlan.rows[0].recovery_case_id]);
await db.query(`
  update public.followup_sequences
  set status='completed', completion_reason='purchase', completed_at=now(), revision=revision+1
  where id=$1
`, [concurrentPlan.rows[0].followup_sequence_id]);
await db.query(`
  update public.scheduled_actions
  set status='cancelled', terminal_reason='purchase', lease_owner=null, lease_expires_at=null
  where id=$1
`, [concurrentAction.id]);
await db.query(`
  select * from public.record_and_finalize_followup_acceptance(
    $1, $2, 'concurrent-worker', 1,
    'cw-concurrent-conversation', 'cw-concurrent-message',
    'Concurrent accepted message', now()
  )
`, [concurrentAction.id, concurrentAttempt.rows[0].id]);
const concurrentState = await db.query(`
  select sa.status, sa.terminal_reason, fs.status as sequence_status,
         fs.automatic_messages_accepted, rc.status as case_status,
         fda.outcome,
         (select count(*)::int from public.conversation_events ce
          where ce.related_action_id=sa.id
            and ce.event_type='followup_delivery_finalized') as audit_count,
         (select ce.data ->> 'from_status' from public.conversation_events ce
          where ce.related_action_id=sa.id
            and ce.event_type='followup_delivery_finalized'
          limit 1) as audit_from_status,
         (select count(*)::int from public.scheduled_actions x
          where x.followup_sequence_id=fs.id and x.id<>sa.id) as successor_count
  from public.scheduled_actions sa
  join public.followup_sequences fs on fs.id=sa.followup_sequence_id
  join public.recovery_cases rc on rc.id=sa.recovery_case_id
  join public.followup_delivery_attempts fda on fda.action_id=sa.id
  where sa.id=$1
`, [concurrentAction.id]);
const concurrentRow = concurrentState.rows[0];
if (concurrentRow.status !== 'accepted_by_chatwoot' || concurrentRow.outcome !== 'accepted_by_chatwoot') throw new Error('concurrent acceptance not recorded');
if (concurrentRow.case_status !== 'won' || concurrentRow.sequence_status !== 'completed') throw new Error('authoritative terminal state overwritten');
if (concurrentRow.automatic_messages_accepted !== 0 || concurrentRow.successor_count !== 0) throw new Error('successor created after authoritative change');
if (!concurrentRow.terminal_reason.includes('authoritative_state_changed_after_reservation')) throw new Error('concurrent suppression reason missing');
if (concurrentRow.audit_count !== 1) throw new Error('concurrent acceptance audit missing');
if (concurrentRow.audit_from_status !== 'cancelled') throw new Error('concurrent acceptance audit lost prior status');
console.log('concurrent_state_change_suppresses_successor=OK');

await db.exec(`
  insert into public.contacts (id, full_name, email) values
    ('00000000-0000-0000-0000-000000000016', 'Takeover Race', 'takeover@example.com');
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values (
    '00000000-0000-0000-0000-000000000017', 'hotmart', 'evt-takeover',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
`);
const takeoverPlan = await db.query(`
  select * from public.plan_cart_recovery(
    '00000000-0000-0000-0000-000000000017',
    '00000000-0000-0000-0000-000000000016',
    'takeover-product', 'Takeover Product', null::text,
    'cart-recovery-test', 1, now() - interval '2 hours'
  )
`);
const takeoverClaim = await db.query(`
  select * from public.claim_due_followup_actions(
    'takeover-worker', now(), interval '5 minutes', 1
  )
`);
const takeoverAction = takeoverClaim.rows[0];
if (!takeoverAction || takeoverAction.recovery_case_id !== takeoverPlan.rows[0].recovery_case_id) throw new Error('takeover action not claimed');
await authorizeExecute(takeoverAction.id, 'takeover-worker');
const takeoverAttempt = await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1, 'takeover-worker', 1, 1, 1, 'whatsapp', 'freeform', now()
  )
`, [takeoverAction.id]);
await db.query(`
  update public.contacts
  set contact_permission='opted_out'
  where id='00000000-0000-0000-0000-000000000016'
`);
let takeoverBlocked = false;
try {
  await db.query(`
    select * from public.mark_followup_request_started(
      $1, $2, 'takeover-worker', 1, now()
    )
  `, [takeoverAction.id, takeoverAttempt.rows[0].id]);
} catch (error) {
  takeoverBlocked = String(error.message).includes('authoritative_state_changed_before_request');
}
if (!takeoverBlocked) throw new Error('contact opt-out did not block request start');
const takeoverAttemptState = await db.query(`
  select phase from public.followup_delivery_attempts where id=$1
`, [takeoverAttempt.rows[0].id]);
if (takeoverAttemptState.rows[0].phase !== 'reserved') throw new Error('blocked request start mutated attempt phase');
console.log('contact_opt_out_before_request_blocked=OK');

await db.query(`
  update public.contacts set contact_permission='unknown'
  where id='00000000-0000-0000-0000-000000000016'
`);
await db.query(`
  insert into public.contact_authorizations (
    contact_id, channel, purpose, authorization_status,
    authorization_source, valid_from
  ) values (
    '00000000-0000-0000-0000-000000000016',
    'whatsapp', 'cart_recovery', 'denied', 'manual', now() - interval '1 second'
  )
`);
let authorizationRaceBlocked = false;
try {
  await db.query(`
    select * from public.mark_followup_request_started(
      $1, $2, 'takeover-worker', 1, now()
    )
  `, [takeoverAction.id, takeoverAttempt.rows[0].id]);
} catch (error) {
  authorizationRaceBlocked = String(error.message).includes(
    'authoritative_state_changed_before_request'
  );
}
if (!authorizationRaceBlocked) throw new Error('concurrent channel denial did not block request start');
console.log('channel_denial_before_request_blocked=OK');

await db.exec(`
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values (
    '00000000-0000-0000-0000-000000000003', 'hotmart', 'schema-check-in-flight',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
  insert into public.contacts (id, full_name) values (
    '00000000-0000-0000-0000-000000000004', 'In Flight Check'
  );
  select * from public.plan_cart_recovery(
    '00000000-0000-0000-0000-000000000003',
    '00000000-0000-0000-0000-000000000004',
    'product-test-2', 'Product Test 2', 'offer-test-2',
    'cart-recovery-test', 1, now() - interval '2 hours'
  );
`);
const inFlightClaim = await db.query(`
  select * from public.claim_due_followup_actions(
    'in-flight-worker', now(), interval '5 minutes', 1
  )
`);
const inFlightActionId = inFlightClaim.rows[0].id;
await authorizeExecute(inFlightActionId, 'in-flight-worker');
const inFlightReserve = await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1, 'in-flight-worker', 1, 1, 1, 'whatsapp', 'freeform', now()
  )
`, [inFlightActionId]);
await db.query(`
  select * from public.mark_followup_request_started(
    $1, $2, 'in-flight-worker', 1, now()
  )
`, [inFlightActionId, inFlightReserve.rows[0].id]);
const reclaim = await db.query(`
  select * from public.claim_due_followup_actions(
    'replacement-worker', now() + interval '10 minutes', interval '5 minutes', 10
  )
`);
if (reclaim.rows.some((row) => row.id === inFlightActionId)) throw new Error('in-flight request was reclaimed');
console.log('in_flight_reclaim_blocked=OK');

await db.exec(`
  insert into public.contacts (id, full_name) values (
    '00000000-0000-0000-0000-000000000006', 'Paused Case Check'
  );
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values
    ('00000000-0000-0000-0000-000000000007', 'hotmart', 'paused-initial',
     'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb),
    ('00000000-0000-0000-0000-000000000008', 'hotmart', 'paused-repeat',
     'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb);
`);
const pausedInitial = await db.query(`
  select * from public.plan_cart_recovery(
    '00000000-0000-0000-0000-000000000007',
    '00000000-0000-0000-0000-000000000006',
    'paused-product', 'Paused Product', 'paused-offer',
    'cart-recovery-test', 1, now()
  )
`);
await db.query(`update public.recovery_cases set status='paused' where id=$1`, [pausedInitial.rows[0].recovery_case_id]);
await db.query(`update public.followup_sequences set status='paused' where id=$1`, [pausedInitial.rows[0].followup_sequence_id]);
const pausedRepeat = await db.query(`
  select * from public.plan_cart_recovery(
    '00000000-0000-0000-0000-000000000008',
    '00000000-0000-0000-0000-000000000006',
    'paused-product', 'Paused Product', 'paused-offer',
    'cart-recovery-test', 1, now() + interval '1 minute'
  )
`);
if (pausedRepeat.rows[0].recovery_case_id !== pausedInitial.rows[0].recovery_case_id) throw new Error('paused case duplicated');
const pausedState = await db.query(`
  select rc.version, fs.status as sequence_status, sa.expected_case_version
  from public.recovery_cases rc
  join public.followup_sequences fs on fs.recovery_case_id=rc.id
  join public.scheduled_actions sa on sa.followup_sequence_id=fs.id
  where rc.id=$1
`, [pausedInitial.rows[0].recovery_case_id]);
if (pausedState.rows.length !== 1 || pausedState.rows[0].sequence_status !== 'paused') throw new Error('paused sequence resurrected');
if (pausedState.rows[0].expected_case_version !== pausedState.rows[0].version) throw new Error('paused action version stale');
console.log('repeated_abandonment_pause_preserved=OK');

async function createDeliveryUnknownFixture(suffix, worker) {
  const contactId = `20000000-0000-0000-0000-${suffix.padStart(12, '0')}`;
  const eventId = `30000000-0000-0000-0000-${suffix.padStart(12, '0')}`;
  await db.query(`insert into public.contacts (id, full_name) values ($1, $2)`, [contactId, `Reconciliation ${suffix}`]);
  await db.query(`
    insert into public.webhook_events (id, source, external_event_id, event_type, payload)
    values ($1, 'hotmart', $2, 'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb)
  `, [eventId, `reconciliation-${suffix}`]);
  const plan = await db.query(`
    select * from public.plan_cart_recovery(
      $1, $2, $3, $4, null::text, 'cart-recovery-test', 1,
      now() - interval '2 hours'
    )
  `, [eventId, contactId, `reconciliation-product-${suffix}`, `Reconciliation Product ${suffix}`]);
  const identityId = `25000000-0000-0000-0000-${suffix.padStart(12, '0')}`;
  await db.query(`
    insert into public.channel_identities (
      id, contact_id, channel, account_id, external_user_id, identity_status
    ) values ($1, $2, 'whatsapp', 'chatwoot:1', $3, 'active')
  `, [identityId, contactId, `reconciliation-user-${suffix}`]);
  await db.query(`
    update public.recovery_cases
    set selected_channel_identity_id=$1, identity_resolution_status='resolved'
    where id=$2
  `, [identityId, plan.rows[0].recovery_case_id]);
  const claim = await db.query(`
    select * from public.claim_due_followup_actions($1, now(), interval '5 minutes', 1)
  `, [worker]);
  const action = claim.rows[0];
  if (!action || action.recovery_case_id !== plan.rows[0].recovery_case_id) throw new Error(`reconciliation ${suffix} not claimed`);
  await authorizeExecute(action.id, worker);
  const reserve = await db.query(`
    select * from public.reserve_followup_delivery_attempt(
      $1, $2, 1, 1, 1, 'whatsapp', 'freeform', now()
    )
  `, [action.id, worker]);
  await db.query(`
    select * from public.mark_followup_request_started($1, $2, $3, 1, now())
  `, [action.id, reserve.rows[0].id, worker]);
  let nonFutureDeadlineBlocked = false;
  try {
    await db.query(`
      select * from public.finalize_followup_delivery_attempt(
        $1, $2, $3, 1, 'delivery_unknown', null, null, 'ambiguous_timeout',
        null, now(), now()
      )
    `, [action.id, reserve.rows[0].id, worker]);
  } catch (error) {
    nonFutureDeadlineBlocked = String(error.message).includes('future_reconciliation_deadline_required');
  }
  if (!nonFutureDeadlineBlocked) throw new Error('delivery_unknown accepted a non-future deadline');
  await db.query(`
    select * from public.finalize_followup_delivery_attempt(
      $1, $2, $3, 1, 'delivery_unknown', null, null, 'ambiguous_timeout',
      null, now() + interval '1 hour', now()
    )
  `, [action.id, reserve.rows[0].id, worker]);
  return { actionId: action.id, attemptId: reserve.rows[0].id, plan };
}

const foundFixture = await createDeliveryUnknownFixture('21', 'reconcile-found-worker');
let missingDeadlineBlocked = false;
try {
  await db.query(`
    update public.followup_delivery_attempts
    set reconciliation_deadline=null
    where id=$1
  `, [foundFixture.attemptId]);
} catch (error) {
  missingDeadlineBlocked = true;
}
if (!missingDeadlineBlocked) throw new Error('delivery_unknown accepted a missing deadline');
let lateAcceptanceBlocked = false;
try {
  await db.query(`
    select * from public.record_and_finalize_followup_acceptance(
      $1, $2, 'reconcile-found-worker', 1,
      'cw-reconciled-conversation', 'cw-too-late',
      'Reconciled accepted message', now() + interval '2 hours'
    )
  `, [foundFixture.actionId, foundFixture.attemptId]);
} catch (error) {
  lateAcceptanceBlocked = String(error.message).includes('reconciliation_window_expired');
}
if (!lateAcceptanceBlocked) throw new Error('late acceptance bypassed reconciliation deadline');
await db.query(`
  select * from public.record_and_finalize_followup_acceptance(
    $1, $2, 'reconcile-found-worker', 1,
    'cw-reconciled-conversation', 'cw-reconciled-message',
    'Reconciled accepted message', now()
  )
`, [foundFixture.actionId, foundFixture.attemptId]);
await db.query(`
  select * from public.record_and_finalize_followup_acceptance(
    $1, $2, 'reconcile-found-worker', 1,
    'cw-reconciled-conversation', 'cw-reconciled-message',
    'Reconciled accepted message', now()
  )
`, [foundFixture.actionId, foundFixture.attemptId]);
let changedAcceptedAnchorBlocked = false;
try {
  await db.query(`
    select * from public.record_and_finalize_followup_acceptance(
      $1, $2, 'reconcile-found-worker', 1,
      'cw-reconciled-conversation', 'cw-reconciled-different-message',
      'Reconciled accepted message', now()
    )
  `, [foundFixture.actionId, foundFixture.attemptId]);
} catch (error) {
  changedAcceptedAnchorBlocked = String(error.message).includes('delivery_attempt_already_finalized_differently');
}
if (!changedAcceptedAnchorBlocked) throw new Error('reconciliation accepted a different durable anchor');
const foundState = await db.query(`
  select sa.status, fda.outcome, fda.reconciliation_resolution,
         (select count(*)::int from public.scheduled_actions x
          where x.followup_sequence_id=sa.followup_sequence_id and x.id<>sa.id) as successor_count,
         (select count(*)::int from public.conversation_events ce
          where ce.related_action_id=sa.id
            and ce.event_type='followup_delivery_finalized'
            and ce.data ->> 'to_status'='accepted_by_chatwoot') as audit_count
         ,(select ce.data ->> 'from_status' from public.conversation_events ce
           where ce.related_action_id=sa.id
             and ce.event_type='followup_delivery_finalized'
             and ce.data ->> 'to_status'='accepted_by_chatwoot'
           limit 1) as finalized_from_status
  from public.scheduled_actions sa
  join public.followup_delivery_attempts fda on fda.action_id=sa.id
  where sa.id=$1
`, [foundFixture.actionId]);
if (foundState.rows[0].status !== 'accepted_by_chatwoot' || foundState.rows[0].outcome !== 'accepted_by_chatwoot') throw new Error('found message not accepted');
if (foundState.rows[0].reconciliation_resolution !== 'accepted_by_chatwoot') throw new Error('found message reconciliation missing');
if (foundState.rows[0].successor_count !== 1 || foundState.rows[0].audit_count !== 1) throw new Error('found message reconciliation not idempotent');
if (foundState.rows[0].finalized_from_status !== 'delivery_unknown') throw new Error('reconciled acceptance audit lost prior status');
console.log('delivery_unknown_found_message_reconciled=OK');

const notAppliedFixture = await createDeliveryUnknownFixture('22', 'reconcile-not-applied-worker');
let lateNotAppliedBlocked = false;
try {
  await db.query(`
    select * from public.reconcile_followup_delivery_attempt(
      $1, $2, 1, 'not_applied', null, null,
      now() + interval '3 hours', 'provider_proved_not_applied',
      now() + interval '2 hours'
    )
  `, [notAppliedFixture.actionId, notAppliedFixture.attemptId]);
} catch (error) {
  lateNotAppliedBlocked = String(error.message).includes('reconciliation_window_expired');
}
if (!lateNotAppliedBlocked) throw new Error('late not_applied bypassed reconciliation deadline');
let retryOutsideExpirationBlocked = false;
try {
  await db.query(`
    select * from public.reconcile_followup_delivery_attempt(
      $1, $2, 1, 'not_applied', null, null,
      now() + interval '8 days', 'provider_proved_not_applied', now()
    )
  `, [notAppliedFixture.actionId, notAppliedFixture.attemptId]);
} catch (error) {
  retryOutsideExpirationBlocked = String(error.message).includes('not_applied_retry_not_permitted');
}
if (!retryOutsideExpirationBlocked) throw new Error('not_applied escaped retryable_failed semantics');
await db.query(`update public.scheduled_actions set max_execution_retries=0 where id=$1`, [notAppliedFixture.actionId]);
let exhaustedRetryBlocked = false;
try {
  await db.query(`
    select * from public.reconcile_followup_delivery_attempt(
      $1, $2, 1, 'not_applied', null, null,
      now() + interval '10 minutes', 'provider_proved_not_applied', now()
    )
  `, [notAppliedFixture.actionId, notAppliedFixture.attemptId]);
} catch (error) {
  exhaustedRetryBlocked = String(error.message).includes('not_applied_retry_not_permitted');
}
if (!exhaustedRetryBlocked) throw new Error('not_applied ignored exhausted retry budget');
await db.query(`update public.scheduled_actions set max_execution_retries=3 where id=$1`, [notAppliedFixture.actionId]);
await db.query(`
  select * from public.reconcile_followup_delivery_attempt(
    $1, $2, 1, 'not_applied', null, null,
    now() + interval '10 minutes', 'provider_proved_not_applied', now()
  )
`, [notAppliedFixture.actionId, notAppliedFixture.attemptId]);
const notAppliedState = await db.query(`
  select sa.status, sa.next_attempt_at, fda.outcome, fda.reconciliation_resolution
  from public.scheduled_actions sa
  join public.followup_delivery_attempts fda on fda.action_id=sa.id
  where sa.id=$1
`, [notAppliedFixture.actionId]);
if (notAppliedState.rows[0].status !== 'retryable_failed' || !notAppliedState.rows[0].next_attempt_at) throw new Error('not applied result did not schedule controlled retry');
if (notAppliedState.rows[0].outcome !== 'rejected' || notAppliedState.rows[0].reconciliation_resolution !== 'not_applied') throw new Error('not applied reconciliation ledger mismatch');
console.log('delivery_unknown_not_applied_reconciled=OK');

const escalatedFixture = await createDeliveryUnknownFixture('23', 'reconcile-escalated-worker');
let earlyEscalationBlocked = false;
try {
  await db.query(`
    select * from public.reconcile_followup_delivery_attempt(
      $1, $2, 1, 'escalated', null, null, null, 'operator_review_required', now()
    )
  `, [escalatedFixture.actionId, escalatedFixture.attemptId]);
} catch (error) {
  earlyEscalationBlocked = String(error.message).includes('reconciliation_window_not_expired');
}
if (!earlyEscalationBlocked) throw new Error('early delivery_unknown escalation was allowed');
await db.query(`
  select * from public.reconcile_followup_delivery_attempt(
    $1, $2, 1, 'escalated', null, null, null,
    'operator_review_required', now() + interval '2 hours'
  )
`, [escalatedFixture.actionId, escalatedFixture.attemptId]);
const escalatedState = await db.query(`
  select sa.status, fs.status as sequence_status, rc.status as case_status,
         fda.outcome, fda.reconciliation_resolution
  from public.scheduled_actions sa
  join public.followup_sequences fs on fs.id=sa.followup_sequence_id
  join public.recovery_cases rc on rc.id=sa.recovery_case_id
  join public.followup_delivery_attempts fda on fda.action_id=sa.id
  where sa.id=$1
`, [escalatedFixture.actionId]);
if (escalatedState.rows[0].status !== 'delivery_unknown' || escalatedState.rows[0].outcome !== 'delivery_unknown') throw new Error('escalation lost uncertain delivery evidence');
if (escalatedState.rows[0].sequence_status !== 'paused' || escalatedState.rows[0].case_status !== 'paused') throw new Error('inconclusive reconciliation did not pause');
if (escalatedState.rows[0].reconciliation_resolution !== 'escalated') throw new Error('escalation reconciliation missing');
console.log('delivery_unknown_inconclusive_escalated=OK');

await db.exec(`
  insert into public.contacts (id, full_name)
  values ('50000000-0000-0000-0000-000000000024', 'Late Rejection Check');
  insert into public.webhook_events (id, source, external_event_id, event_type, payload)
  values (
    '60000000-0000-0000-0000-000000000024', 'hotmart', 'late-rejection-24',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
`);
const lateRejectPlan = await db.query(`
  select * from public.plan_cart_recovery(
    '60000000-0000-0000-0000-000000000024',
    '50000000-0000-0000-0000-000000000024',
    'late-rejection-product', 'Late Rejection Product', null,
    'cart-recovery-test', 1, now() - interval '2 hours'
  )
`);
const lateRejectClaim = await db.query(`
  select * from public.claim_due_followup_actions(
    'late-rejection-worker', now(), interval '5 minutes', 1
  )
`);
const lateRejectAction = lateRejectClaim.rows[0];
if (!lateRejectAction || lateRejectAction.recovery_case_id !== lateRejectPlan.rows[0].recovery_case_id) throw new Error('late rejection action not claimed');
await authorizeExecute(lateRejectAction.id, 'late-rejection-worker');
const lateRejectReserve = await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1, 'late-rejection-worker', 1, 1, 1, 'whatsapp', 'freeform', now()
  )
`, [lateRejectAction.id]);
await db.query(`
  select * from public.mark_followup_request_started(
    $1, $2, 'late-rejection-worker', 1, now()
  )
`, [lateRejectAction.id, lateRejectReserve.rows[0].id]);
await db.query(`update public.recovery_cases set status='paused', version=version+1 where id=$1`, [lateRejectAction.recovery_case_id]);
await db.query(`update public.followup_sequences set status='paused', revision=revision+1 where id=$1`, [lateRejectAction.followup_sequence_id]);
await db.query(`
  update public.scheduled_actions
  set status='cancelled', terminal_reason='human_takeover', lease_owner=null, lease_expires_at=null
  where id=$1
`, [lateRejectAction.id]);
await db.query(`
  select * from public.finalize_followup_delivery_attempt(
    $1, $2, 'late-rejection-worker', 1, 'rejected', null, null,
    'provider_rejected', null, null, now()
  )
`, [lateRejectAction.id, lateRejectReserve.rows[0].id]);
const lateRejectState = await db.query(`
  select sa.status, sa.lease_owner, fda.outcome,
         (select ce.data ->> 'from_status' from public.conversation_events ce
          where ce.related_action_id=sa.id
            and ce.event_type='followup_delivery_finalized'
          limit 1) as audit_from_status,
         (select ce.data ->> 'to_status' from public.conversation_events ce
          where ce.related_action_id=sa.id
            and ce.event_type='followup_delivery_finalized'
          limit 1) as audit_to_status
  from public.scheduled_actions sa
  join public.followup_delivery_attempts fda on fda.action_id=sa.id
  where sa.id=$1
`, [lateRejectAction.id]);
if (lateRejectState.rows[0].status !== 'cancelled' || lateRejectState.rows[0].lease_owner !== null) throw new Error('late rejection resurrected cancelled action');
if (lateRejectState.rows[0].outcome !== 'rejected') throw new Error('late rejection ledger missing');
if (lateRejectState.rows[0].audit_from_status !== 'cancelled' || lateRejectState.rows[0].audit_to_status !== 'cancelled') throw new Error('late rejection audit mismatch');
const lateRejectReclaim = await db.query(`
  select * from public.claim_due_followup_actions(
    'late-rejection-reclaimer', now() + interval '1 day', interval '5 minutes', 100
  )
`);
if (lateRejectReclaim.rows.some((row) => row.id === lateRejectAction.id)) throw new Error('late rejected action became reclaimable');
console.log('late_rejection_preserves_terminal_action=OK');

await db.exec(`
  insert into public.contacts (id, full_name)
  values ('50000000-0000-0000-0000-000000000027', 'Late Unknown Check');
  insert into public.webhook_events (id, source, external_event_id, event_type, payload)
  values (
    '60000000-0000-0000-0000-000000000027', 'hotmart', 'late-unknown-27',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
`);
const lateUnknownPlan = await db.query(`
  select * from public.plan_cart_recovery(
    '60000000-0000-0000-0000-000000000027',
    '50000000-0000-0000-0000-000000000027',
    'late-unknown-product', 'Late Unknown Product', null,
    'cart-recovery-test', 1, now() - interval '2 hours'
  )
`);
const lateUnknownClaim = await db.query(`
  select * from public.claim_due_followup_actions('late-unknown-worker', now(), interval '5 minutes', 1)
`);
const lateUnknownAction = lateUnknownClaim.rows[0];
if (!lateUnknownAction || lateUnknownAction.recovery_case_id !== lateUnknownPlan.rows[0].recovery_case_id) throw new Error('late unknown action not claimed');
await authorizeExecute(lateUnknownAction.id, 'late-unknown-worker');
const lateUnknownReserve = await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1, 'late-unknown-worker', 1, 1, 1, 'whatsapp', 'freeform', now()
  )
`, [lateUnknownAction.id]);
await db.query(`
  select * from public.mark_followup_request_started(
    $1, $2, 'late-unknown-worker', 1, now()
  )
`, [lateUnknownAction.id, lateUnknownReserve.rows[0].id]);
await db.query(`update public.recovery_cases set status='paused', version=version+1 where id=$1`, [lateUnknownAction.recovery_case_id]);
await db.query(`update public.followup_sequences set status='paused', revision=revision+1 where id=$1`, [lateUnknownAction.followup_sequence_id]);
await db.query(`
  update public.scheduled_actions
  set status='cancelled', terminal_reason='human_takeover', lease_owner=null, lease_expires_at=null
  where id=$1
`, [lateUnknownAction.id]);
await db.query(`
  select * from public.finalize_followup_delivery_attempt(
    $1, $2, 'late-unknown-worker', 1, 'delivery_unknown', null, null,
    'ambiguous_timeout', null, now() + interval '1 hour', now()
  )
`, [lateUnknownAction.id, lateUnknownReserve.rows[0].id]);
const lateUnknownState = await db.query(`
  select sa.status, sa.terminal_reason, fda.outcome
  from public.scheduled_actions sa
  join public.followup_delivery_attempts fda on fda.action_id=sa.id
  where sa.id=$1
`, [lateUnknownAction.id]);
if (lateUnknownState.rows[0].status !== 'cancelled' || lateUnknownState.rows[0].outcome !== 'delivery_unknown') throw new Error('late uncertainty replaced terminal action state');
if (!lateUnknownState.rows[0].terminal_reason.includes('human_takeover')) throw new Error('late uncertainty lost terminal reason');
const lateUnknownReclaim = await db.query(`
  select * from public.claim_due_followup_actions('late-unknown-reclaimer', now(), interval '5 minutes', 100)
`);
if (lateUnknownReclaim.rows.some((row) => row.id === lateUnknownAction.id)) throw new Error('late unknown action became reclaimable');
await db.query(`
  select * from public.reconcile_followup_delivery_attempt(
    $1, $2, 1, 'escalated', null, null, null,
    'operator_review_required', now() + interval '2 hours'
  )
`, [lateUnknownAction.id, lateUnknownReserve.rows[0].id]);
const lateUnknownEscalated = await db.query(`
  select sa.status, sa.terminal_reason, fda.reconciliation_resolution
  from public.scheduled_actions sa
  join public.followup_delivery_attempts fda on fda.action_id=sa.id
  where sa.id=$1
`, [lateUnknownAction.id]);
if (lateUnknownEscalated.rows[0].status !== 'cancelled' || lateUnknownEscalated.rows[0].reconciliation_resolution !== 'escalated') throw new Error('late uncertainty escalation changed terminal action');
if (!lateUnknownEscalated.rows[0].terminal_reason.includes('human_takeover')) throw new Error('late escalation lost terminal reason');
console.log('late_delivery_unknown_preserves_terminal_action=OK');

await db.exec(`
  insert into public.contacts (id, full_name)
  values ('50000000-0000-0000-0000-000000000025', 'Permanent Failure Check');
  insert into public.webhook_events (id, source, external_event_id, event_type, payload)
  values (
    '60000000-0000-0000-0000-000000000025', 'hotmart', 'permanent-failure-25',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
`);
const permanentPlan = await db.query(`
  select * from public.plan_cart_recovery(
    '60000000-0000-0000-0000-000000000025',
    '50000000-0000-0000-0000-000000000025',
    'permanent-product', 'Permanent Product', null,
    'cart-recovery-test', 1, now() - interval '2 hours'
  )
`);
const permanentClaim = await db.query(`
  select * from public.claim_due_followup_actions('permanent-worker', now(), interval '5 minutes', 1)
`);
const permanentAction = permanentClaim.rows[0];
if (!permanentAction || permanentAction.recovery_case_id !== permanentPlan.rows[0].recovery_case_id) throw new Error('permanent failure action not claimed');
await authorizeExecute(permanentAction.id, 'permanent-worker');
const permanentReserve = await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1, 'permanent-worker', 1, 1, 1, 'whatsapp', 'freeform', now()
  )
`, [permanentAction.id]);
await db.query(`
  select * from public.finalize_followup_delivery_attempt(
    $1, $2, 'permanent-worker', 1, 'failed_before_request', null, null,
    'clear_local_failure', null, null, now()
  )
`, [permanentAction.id, permanentReserve.rows[0].id]);
const permanentState = await db.query(`
  select sa.status, fda.outcome, fda.finalized_next_attempt_at
  from public.scheduled_actions sa
  join public.followup_delivery_attempts fda on fda.action_id=sa.id
  where sa.id=$1
`, [permanentAction.id]);
if (permanentState.rows[0].status !== 'permanent_failed' || permanentState.rows[0].outcome !== 'failed_before_request') throw new Error('permanent failure did not finalize');
if (permanentState.rows[0].finalized_next_attempt_at !== null) throw new Error('permanent failure invented retry time');
console.log('permanent_failure_without_retry=OK');

await db.exec(`
  insert into public.contacts (id, full_name)
  values ('50000000-0000-0000-0000-000000000026', 'Expiration Boundary Check');
  insert into public.webhook_events (id, source, external_event_id, event_type, payload)
  values (
    '60000000-0000-0000-0000-000000000026', 'hotmart', 'expiration-boundary-26',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
`);
const expirationPlan = await db.query(`
  select * from public.plan_cart_recovery(
    '60000000-0000-0000-0000-000000000026',
    '50000000-0000-0000-0000-000000000026',
    'expiration-product', 'Expiration Product', null,
    'cart-recovery-test', 1, now() - interval '2 hours'
  )
`);
const expirationClaim = await db.query(`
  select * from public.claim_due_followup_actions('expiration-worker', now(), interval '5 minutes', 1)
`);
const expirationAction = expirationClaim.rows[0];
if (!expirationAction || expirationAction.recovery_case_id !== expirationPlan.rows[0].recovery_case_id) throw new Error('expiration action not claimed');
await authorizeExecute(expirationAction.id, 'expiration-worker');
await db.query(`update public.scheduled_actions set expires_at=now()-interval '1 second' where id=$1`, [expirationAction.id]);
let expiredReserveBlocked = false;
try {
  await db.query(`
    select * from public.reserve_followup_delivery_attempt(
      $1, 'expiration-worker', 1, 1, 1, 'whatsapp', 'freeform', now()
    )
  `, [expirationAction.id]);
} catch (error) {
  expiredReserveBlocked = String(error.message).includes('current_action_authorization_not_found');
}
if (!expiredReserveBlocked) throw new Error('expired action was reservable');
await db.query(`update public.scheduled_actions set expires_at=now()+interval '1 day' where id=$1`, [expirationAction.id]);
const expirationReserve = await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1, 'expiration-worker', 1, 1, 1, 'whatsapp', 'freeform', now()
  )
`, [expirationAction.id]);
await db.query(`update public.scheduled_actions set expires_at=now()-interval '1 second' where id=$1`, [expirationAction.id]);
let expiredRequestStartBlocked = false;
try {
  await db.query(`
    select * from public.mark_followup_request_started(
      $1, $2, 'expiration-worker', 1, now()
    )
  `, [expirationAction.id, expirationReserve.rows[0].id]);
} catch (error) {
  expiredRequestStartBlocked = String(error.message).includes('current_action_lease_not_found');
}
if (!expiredRequestStartBlocked) throw new Error('expired action reached request_started');
await db.query(`update public.scheduled_actions set lease_owner=null, lease_expires_at=null where id=$1`, [expirationAction.id]);
const expiredClaim = await db.query(`
  select * from public.claim_due_followup_actions('expired-reclaimer', now(), interval '5 minutes', 100)
`);
if (expiredClaim.rows.some((row) => row.id === expirationAction.id)) throw new Error('expired action was claimable');
const expiredCrashState = await db.query(`
  select sa.status as action_status, fs.status as sequence_status, rc.status as case_status
  from public.scheduled_actions sa
  join public.followup_sequences fs on fs.id=sa.followup_sequence_id
  join public.recovery_cases rc on rc.id=sa.recovery_case_id
  where sa.id=$1
`, [expirationAction.id]);
if (expiredCrashState.rows[0].action_status !== 'expired' || expiredCrashState.rows[0].sequence_status !== 'completed' || expiredCrashState.rows[0].case_status !== 'expired') throw new Error('crash-expired action remained stranded');
console.log('expiration_boundaries_fail_closed=OK');

await db.exec(`
  insert into public.contacts (
    id, full_name, contact_permission
  ) values (
    '00000000-0000-0000-0000-000000000071', 'Eligible Recheck', 'allowed'
  );
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values (
    '00000000-0000-0000-0000-000000000072', 'hotmart', 'eligible-recheck',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
  insert into public.channel_identities (
    id, contact_id, channel, account_id, external_user_id, identity_status
  ) values (
    '00000000-0000-0000-0000-000000000073',
    '00000000-0000-0000-0000-000000000071',
    'whatsapp', 'test-account', 'test-user-71', 'active'
  );
  insert into public.contact_authorizations (
    contact_id, channel, purpose, authorization_status,
    authorization_source, valid_from
  ) values (
    '00000000-0000-0000-0000-000000000071',
    'whatsapp', 'cart_recovery', 'allowed', 'manual', now() - interval '1 day'
  );
`);
const eligiblePlan = await db.query(`
  select * from public.plan_cart_recovery(
    '00000000-0000-0000-0000-000000000072',
    '00000000-0000-0000-0000-000000000071',
    'eligible-product', 'Eligible Product', null,
    'cart-recovery-test', 1, now() - interval '2 hours'
  )
`);
await db.query(`
  update public.recovery_cases
  set selected_channel_identity_id='00000000-0000-0000-0000-000000000073',
      identity_resolution_status='resolved'
  where id=$1
`, [eligiblePlan.rows[0].recovery_case_id]);
const eligibleClaims = await db.query(`
  select * from public.claim_due_followup_actions(
    'reevaluation-worker', now(), interval '5 minutes', 100
  )
`);
const eligibleAction = eligibleClaims.rows.find((row) => row.id === eligiblePlan.rows[0].scheduled_action_id);
if (!eligibleAction) throw new Error('eligible reevaluation action not claimed');
let nullFenceBlocked = false;
try {
  await db.query(`
    select * from public.get_followup_execution_context(
      $1, null, null, now()
    )
  `, [eligibleAction.id]);
} catch (error) {
  nullFenceBlocked = String(error.message).includes('invalid_followup_fence');
}
if (!nullFenceBlocked) throw new Error('null lease fence exposed execution context');
console.log('null_lease_fence_blocked=OK');
const eligibleDecision = await db.query(`
  select * from public.reevaluate_followup_action(
    $1, 'reevaluation-worker', $2, now()
  )
`, [eligibleAction.id, eligibleAction.lease_generation]);
if (eligibleDecision.rows[0].decision !== 'execute' || eligibleDecision.rows[0].reason_code !== 'eligible_for_execution') throw new Error('eligible first contact did not execute');
const eligibleState = await db.query(`
  select status, lease_owner from public.scheduled_actions where id=$1
`, [eligibleAction.id]);
if (eligibleState.rows[0].status !== 'pending' || eligibleState.rows[0].lease_owner !== 'reevaluation-worker') throw new Error('execute did not preserve action lease');
console.log('authoritative_first_contact_execute=OK');
await db.exec(`
  update public.contacts
  set contact_permission='opted_out'
  where id='00000000-0000-0000-0000-000000000071'
`);
const blockedAfterExecute = await db.query(`
  select * from public.reevaluate_followup_action(
    $1, 'reevaluation-worker', $2, now()
  )
`, [eligibleAction.id, eligibleAction.lease_generation]);
if (blockedAfterExecute.rows[0].decision !== 'cancel' || blockedAfterExecute.rows[0].reason_code !== 'contact_blocked') throw new Error('prior execute hid a newer contact block');
console.log('execute_rechecks_new_blocker=OK');

await db.exec(`
  insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
  ) values (
    'cart-recovery-late-window', 1, 'published', 'cart_recovery', 'UTC',
    '[{"days":[1,2,3,4,5,6,7],"start":"23:00","end":"23:59"}]'::jsonb,
    interval '1 hour', interval '7 days', 3,
    '[{"step_key":"first_contact","mode":"freeform"},{"step_key":"followup_1","mode":"freeform"}]'::jsonb,
    'operator-test', now(), now()
  );
  insert into public.contacts (
    id, full_name, contact_permission
  ) values (
    '00000000-0000-0000-0000-000000000081', 'Deferred Recheck', 'allowed'
  );
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values (
    '00000000-0000-0000-0000-000000000082', 'hotmart', 'deferred-recheck',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
  insert into public.channel_identities (
    id, contact_id, channel, account_id, external_user_id, identity_status
  ) values (
    '00000000-0000-0000-0000-000000000083',
    '00000000-0000-0000-0000-000000000081',
    'whatsapp', 'test-account', 'test-user-81', 'active'
  );
  insert into public.contact_authorizations (
    contact_id, channel, purpose, authorization_status,
    authorization_source, valid_from
  ) values (
    '00000000-0000-0000-0000-000000000081',
    'whatsapp', 'cart_recovery', 'allowed', 'manual',
    date_trunc('day', now()) - interval '1 day'
  );
`);
const deferAt = await db.query(`select date_trunc('day', now()) + interval '8 hours' as value`);
const deferredPlan = await db.query(`
  select * from public.plan_cart_recovery(
    '00000000-0000-0000-0000-000000000082',
    '00000000-0000-0000-0000-000000000081',
    'deferred-product', 'Deferred Product', null,
    'cart-recovery-late-window', 1, $1::timestamptz - interval '2 hours'
  )
`, [deferAt.rows[0].value]);
await db.query(`
  update public.recovery_cases
  set selected_channel_identity_id='00000000-0000-0000-0000-000000000083',
      identity_resolution_status='resolved'
  where id=$1
`, [deferredPlan.rows[0].recovery_case_id]);
const deferredClaims = await db.query(`
  select * from public.claim_due_followup_actions(
    'defer-worker', $1, interval '5 minutes', 100
  )
`, [deferAt.rows[0].value]);
const deferredAction = deferredClaims.rows.find((row) => row.id === deferredPlan.rows[0].scheduled_action_id);
if (!deferredAction) throw new Error('outside-window action not claimed');
const deferredDecision = await db.query(`
  select * from public.reevaluate_followup_action(
    $1, 'defer-worker', $2, $3
  )
`, [deferredAction.id, deferredAction.lease_generation, deferAt.rows[0].value]);
if (deferredDecision.rows[0].decision !== 'defer' || deferredDecision.rows[0].reason_code !== 'business_window_closed') throw new Error('outside-window action was not deferred');
const deferredState = await db.query(`
  select sa.status as action_status, sa.due_at, sa.lease_owner,
         fs.status as sequence_status, rc.status as case_status
  from public.scheduled_actions sa
  join public.followup_sequences fs on fs.id=sa.followup_sequence_id
  join public.recovery_cases rc on rc.id=sa.recovery_case_id
  where sa.id=$1
`, [deferredAction.id]);
if (deferredState.rows[0].action_status !== 'deferred' || deferredState.rows[0].lease_owner !== null) throw new Error('deferred action retained terminal state or lease');
if (deferredState.rows[0].sequence_status !== 'active' || deferredState.rows[0].case_status !== 'grace_period') throw new Error('defer changed aggregate business state');
const expectedDeferredDue = await db.query(`select date_trunc('day', $1::timestamptz) + interval '23 hours' as value`, [deferAt.rows[0].value]);
if (new Date(deferredState.rows[0].due_at).getTime() !== new Date(expectedDeferredDue.rows[0].value).getTime()) throw new Error('defer did not select next business window');
console.log('business_window_defer=OK');

await db.query(`
  update public.scheduled_actions
  set status='cancelled', terminal_reason='test_replaced'
  where id=$1
`, [deferredAction.id]);
await db.query(`
  insert into public.scheduled_actions (
    recovery_case_id, followup_sequence_id, action_type, status, due_at,
    expires_at, expected_case_version, idempotency_key, policy_key,
    policy_version, step_key, anchor_type, anchor_subject_internal_id,
    anchor_observed_at
  )
  select rc.id, fs.id, 'no_reply_review', 'pending', $1,
         rc.created_at + interval '7 days', rc.version,
         'cart_recovery:no_reply:test:' || rc.id::text,
         rc.policy_key, rc.policy_version, 'followup_1', 'message',
         rc.abandonment_event_id, $1
  from public.recovery_cases rc
  join public.followup_sequences fs on fs.recovery_case_id=rc.id
  where rc.id=$2
`, [deferAt.rows[0].value, deferredPlan.rows[0].recovery_case_id]);
const noReplyAt = await db.query(`select date_trunc('day', $1::timestamptz) + interval '23 hours 30 minutes' as value`, [deferAt.rows[0].value]);
const noReplyClaims = await db.query(`
  select * from public.claim_due_followup_actions(
    'no-reply-worker', $1, interval '5 minutes', 100
  )
`, [noReplyAt.rows[0].value]);
const noReplyAction = noReplyClaims.rows.find((row) => row.action_type === 'no_reply_review' && row.recovery_case_id === deferredPlan.rows[0].recovery_case_id);
if (!noReplyAction) throw new Error('no-reply action not claimed');
const noReplyDecision = await db.query(`
  select * from public.reevaluate_followup_action(
    $1, 'no-reply-worker', $2, $3
  )
`, [noReplyAction.id, noReplyAction.lease_generation, noReplyAt.rows[0].value]);
if (noReplyDecision.rows[0].decision !== 'escalate' || noReplyDecision.rows[0].reason_code !== 'no_reply_anchor_invalid') throw new Error('no-reply action accepted a non-message anchor');
console.log('no_reply_requires_chatwoot=OK');

await db.exec(`
  insert into public.contacts (
    id, full_name, contact_permission
  ) values (
    '00000000-0000-0000-0000-000000000076', 'Canonical Chatwoot', 'allowed'
  );
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values (
    '00000000-0000-0000-0000-000000000077', 'hotmart', 'canonical-chatwoot',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
  insert into public.channel_identities (
    id, contact_id, channel, account_id, external_user_id,
    external_conversation_id, identity_status, metadata
  ) values (
    '00000000-0000-0000-0000-000000000078',
    '00000000-0000-0000-0000-000000000076',
    'whatsapp', '1', 'chatwoot-contact-76', '22', 'active',
    '{"inbox_id": 7}'::jsonb
  );
  insert into public.contact_authorizations (
    contact_id, channel, purpose, authorization_status,
    authorization_source, valid_from
  ) values (
    '00000000-0000-0000-0000-000000000076',
    'whatsapp', 'cart_recovery', 'allowed', 'manual', now() - interval '1 day'
  );
`);
const canonicalPlan = await db.query(`
  select * from public.plan_cart_recovery(
    '00000000-0000-0000-0000-000000000077',
    '00000000-0000-0000-0000-000000000076',
    'canonical-product', 'Canonical Product', null,
    'cart-recovery-test', 1, now() - interval '2 hours'
  )
`);
await db.query(`
  update public.recovery_cases
  set selected_channel_identity_id='00000000-0000-0000-0000-000000000078',
      identity_resolution_status='resolved'
  where id=$1
`, [canonicalPlan.rows[0].recovery_case_id]);
// ADR-0008: reply authority is the case's own conversation. Give this case a
// Chatwoot conversation "22" (linked to the identity) so the no-reply check
// reads it from the case, not from the identity anchor.
const canonicalConversation = await db.query(`
  insert into public.conversations (
    contact_id, channel_identity_id, status, automation_status,
    human_takeover, commercial_context
  ) values (
    '00000000-0000-0000-0000-000000000076',
    '00000000-0000-0000-0000-000000000078',
    'active', 'enabled', false,
    jsonb_build_object('chatwoot_conversation_id', '22')
  )
  returning id
`);
await db.query(`
  update public.recovery_cases set conversation_id=$2 where id=$1
`, [canonicalPlan.rows[0].recovery_case_id, canonicalConversation.rows[0].id]);
const canonicalClaims = await db.query(`
  select * from public.claim_due_followup_actions(
    'canonical-worker', now(), interval '5 minutes', 100
  )
`);
const canonicalAction = canonicalClaims.rows.find((row) => row.id === canonicalPlan.rows[0].scheduled_action_id);
if (!canonicalAction) throw new Error('canonical Chatwoot action not claimed');
const canonicalContext = await db.query(`
  select * from public.get_followup_chatwoot_context(
    $1, 'canonical-worker', $2, now()
  )
`, [canonicalAction.id, canonicalAction.lease_generation]);
if (canonicalContext.rows[0].external_conversation_id !== '22' || Number(canonicalContext.rows[0].expected_inbox_id) !== 7) throw new Error('canonical Chatwoot context was not fenced');
const canonicalDecision = await db.query(`
  select * from public.reevaluate_followup_action(
    $1, 'canonical-worker', $2, now(),
    true, '22', '41', now(), 'open', true, true, false, true, false
  )
`, [canonicalAction.id, canonicalAction.lease_generation]);
if (canonicalDecision.rows[0].decision !== 'cancel' || canonicalDecision.rows[0].reason_code !== 'prospect_replied') throw new Error('canonical inbound did not stop automation');
const canonicalState = await db.query(`
  select sa.status as action_status, fs.status as sequence_status, rc.status as case_status
  from public.scheduled_actions sa
  join public.followup_sequences fs on fs.id=sa.followup_sequence_id
  join public.recovery_cases rc on rc.id=sa.recovery_case_id
  where sa.id=$1
`, [canonicalAction.id]);
if (canonicalState.rows[0].action_status !== 'cancelled' || canonicalState.rows[0].sequence_status !== 'completed' || canonicalState.rows[0].case_status !== 'grace_period') throw new Error('canonical inbound transition violated aggregate contract');
console.log('canonical_chatwoot_inbound_guard=OK');

await db.exec(`
  insert into public.contacts (id, full_name) values (
    '00000000-0000-0000-0000-000000000074', 'Unknown Authorization'
  );
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values (
    '00000000-0000-0000-0000-000000000075', 'hotmart', 'unknown-authorization',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
`);
const unknownPlan = await db.query(`
  select * from public.plan_cart_recovery(
    '00000000-0000-0000-0000-000000000075',
    '00000000-0000-0000-0000-000000000074',
    'unknown-auth-product', 'Unknown Auth Product', null,
    'cart-recovery-test', 1, now() - interval '2 hours'
  )
`);
const unknownClaims = await db.query(`
  select * from public.claim_due_followup_actions(
    'unknown-auth-worker', now(), interval '5 minutes', 100
  )
`);
const unknownAction = unknownClaims.rows.find((row) => row.id === unknownPlan.rows[0].scheduled_action_id);
if (!unknownAction) throw new Error('unknown authorization action not claimed');
const unknownDecision = await db.query(`
  select * from public.reevaluate_followup_action(
    $1, 'unknown-auth-worker', $2, now()
  )
`, [unknownAction.id, unknownAction.lease_generation]);
if (unknownDecision.rows[0].decision !== 'escalate' || unknownDecision.rows[0].reason_code !== 'identity_not_authorized') throw new Error('missing identity did not fail closed');
console.log('authoritative_reevaluation_fail_closed=OK');

let mutableIdentityBlocked = false;
try {
  await db.query(`
    update public.scheduled_actions set policy_key='tampered-policy' where id=$1
  `, [foundFixture.actionId]);
} catch (error) {
  mutableIdentityBlocked = String(error.message).includes('scheduled_action_identity_is_immutable');
}
if (!mutableIdentityBlocked) throw new Error('scheduled action identity was mutable');
let nullExpirationBlocked = false;
try {
  await db.query(`update public.scheduled_actions set expires_at=null where id=$1`, [foundFixture.actionId]);
} catch (error) {
  nullExpirationBlocked = true;
}
if (!nullExpirationBlocked) throw new Error('scheduled action accepted null expires_at');
console.log('ledger_and_action_identity_constraints=OK');

// --- ADR-0008: per-case conversation authority (recurring buyer) ---
// One buyer has two LIVE cases for different products. Chatwoot opens
// conversation "26" for case A and "27" for case B. Finalizing B must not
// change the reply-authority conversation read by A.
async function driveFirstContactToAccept(caseId, externalConversationId, remoteMessageId, content) {
  const claimed = await db.query(`
    select * from public.claim_due_followup_actions(
      'adr8-worker', now(), interval '5 minutes', 1
    )
  `);
  const row = claimed.rows.find((r) => r.recovery_case_id === caseId);
  if (!row) throw new Error(`ADR-0008: no due action claimed for case ${caseId}`);
  const aId = row.id;
  await authorizeExecute(aId, 'adr8-worker');
  const reserved = await db.query(`
    select * from public.reserve_followup_delivery_attempt(
      $1, 'adr8-worker', 1, 1, 1, 'whatsapp', 'freeform', now()
    )
  `, [aId]);
  const atId = reserved.rows[0].id;
  await db.query(`
    select * from public.mark_followup_request_started($1, $2, 'adr8-worker', 1, now())
  `, [aId, atId]);
  const fin = await db.query(`
    select * from public.record_and_finalize_followup_acceptance(
      $1, $2, 'adr8-worker', 1, $3, $4, $5, now()
    )
  `, [aId, atId, externalConversationId, remoteMessageId, content]);
  return { actionId: aId, attemptId: atId, status: fin.rows[0].status };
}

await db.exec(`
  insert into public.webhook_events (id, source, external_event_id, event_type, payload)
  values ('00000000-0000-0000-0000-0000000ad801', 'hotmart', 'adr8-event-1',
          'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb);
  insert into public.contacts (id, full_name)
  values ('00000000-0000-0000-0000-0000000ad80c', 'Recurring Buyer');
`);

// Case #1 -> Chatwoot conversation "26"
const adr8Plan1 = await db.query(`
  select * from public.plan_cart_recovery_with_identity(
    '00000000-0000-0000-0000-0000000ad801',
    '00000000-0000-0000-0000-0000000ad80c',
    'adr8-product', 'ADR8 Product', 'adr8-offer',
    'cart-recovery-test', 1, now() - interval '2 hours',
    1, 7, '5599888800026'
  )
`);
const adr8Case1 = adr8Plan1.rows[0].recovery_case_id;
const adr8Accept1 = await driveFirstContactToAccept(adr8Case1, '26', 'adr8-msg-26', 'Primer contacto caso 1');
if (adr8Accept1.status !== 'accepted_by_chatwoot') throw new Error('ADR-0008: case #1 did not accept');
const adr8AnchorAfter1 = await db.query(`
  select ci.external_conversation_id
  from public.recovery_cases rc
  join public.channel_identities ci on ci.id = rc.selected_channel_identity_id
  where rc.id = $1
`, [adr8Case1]);
if (adr8AnchorAfter1.rows[0].external_conversation_id !== '26') throw new Error('ADR-0008: identity not anchored to 26');
console.log('adr8_case_a_bound_to_26=OK');

// Keep case A live. A different product creates a concurrent case B that
// resolves to the same WhatsApp identity.
await db.exec(`
  insert into public.webhook_events (id, source, external_event_id, event_type, payload)
  values ('00000000-0000-0000-0000-0000000ad802', 'hotmart', 'adr8-event-2',
          'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb);
`);
const adr8Plan2 = await db.query(`
  select * from public.plan_cart_recovery_with_identity(
    '00000000-0000-0000-0000-0000000ad802',
    '00000000-0000-0000-0000-0000000ad80c',
    'adr8-product-b', 'ADR8 Product B', 'adr8-offer-b',
    'cart-recovery-test', 1, now() - interval '2 hours',
    1, 7, '5599888800026'
  )
`);
const adr8Case2 = adr8Plan2.rows[0].recovery_case_id;
if (adr8Plan2.rows[0].created !== true || adr8Case2 === adr8Case1) throw new Error('ADR-0008: case #2 was not a new case');
const adr8LiveCases = await db.query(`
  select count(*)::int as count,
         count(distinct selected_channel_identity_id)::int as identities
  from public.recovery_cases
  where id in ($1, $2)
    and status in ('grace_period', 'active', 'paused')
`, [adr8Case1, adr8Case2]);
if (adr8LiveCases.rows[0].count !== 2 || adr8LiveCases.rows[0].identities !== 1) {
  throw new Error('ADR-0008: expected two live cases sharing one identity');
}
// The denormalized identity pointer still carries "26" before B finalizes.
const adr8StaleAnchor = await db.query(`
  select ci.external_conversation_id
  from public.recovery_cases rc
  join public.channel_identities ci on ci.id = rc.selected_channel_identity_id
  where rc.id = $1
`, [adr8Case2]);
if (adr8StaleAnchor.rows[0].external_conversation_id !== '26') throw new Error('ADR-0008: precondition — case #2 should inherit stale 26 anchor');

// Case #2 -> Chatwoot conversation "27". Must accept by advancing the anchor.
const adr8Accept2 = await driveFirstContactToAccept(adr8Case2, '27', 'adr8-msg-27', 'Primer contacto caso 2');
if (adr8Accept2.status !== 'accepted_by_chatwoot') throw new Error('ADR-0008: case #2 finalize did not accept (regression: mismatch guard rejected)');
const adr8AnchorAfter2 = await db.query(`
  select ci.external_conversation_id
  from public.recovery_cases rc
  join public.channel_identities ci on ci.id = rc.selected_channel_identity_id
  where rc.id = $1
`, [adr8Case2]);
if (adr8AnchorAfter2.rows[0].external_conversation_id !== '27') throw new Error('ADR-0008: anchor did not advance to 27');
const adr8Case2Conv = await db.query(`
  select c.commercial_context ->> 'chatwoot_conversation_id' as ext
  from public.recovery_cases rc
  join public.conversations c on c.id = rc.conversation_id
  where rc.id = $1
`, [adr8Case2]);
if (adr8Case2Conv.rows[0].ext !== '27') throw new Error('ADR-0008: case #2 not linked to conversation 27');
console.log('adr8_concurrent_case_b_bound_to_27=OK');

// Make case A's successor due and claim it. Its authoritative context must stay
// on conversation "26" even though case B last wrote identity pointer "27".
await db.query(`
  update public.scheduled_actions
  set due_at=now() - interval '1 minute', next_attempt_at=null
  where recovery_case_id=$1
    and status in ('pending', 'deferred')
`, [adr8Case1]);
const adr8CaseAClaims = await db.query(`
  select * from public.claim_due_followup_actions(
    'adr8-authority-worker', now(), interval '5 minutes', 100
  )
`);
const adr8CaseAReview = adr8CaseAClaims.rows.find(
  (row) => row.recovery_case_id === adr8Case1 && row.action_type === 'no_reply_review'
);
if (!adr8CaseAReview) throw new Error('ADR-0008: case A no-reply review was not claimed');
const adr8CaseAContext = await db.query(`
  select * from public.get_followup_chatwoot_context(
    $1, 'adr8-authority-worker', $2, now()
  )
`, [adr8CaseAReview.id, adr8CaseAReview.lease_generation]);
if (adr8CaseAContext.rows[0].external_conversation_id !== '26') {
  throw new Error('ADR-0008: case A authority was hijacked by case B conversation');
}
console.log('adr8_concurrent_cases_keep_independent_authority=OK');

// Human takeover belongs to the conversation/case that owns it. Pausing case B's
// conversation must not pause the concurrently-live case A.
await db.query(`
  update public.conversations
  set human_takeover=true
  where id=(select conversation_id from public.recovery_cases where id=$1)
`, [adr8Case2]);
const adr8CaseAAfterBHumanTakeover = await db.query(`
  select * from public.reevaluate_followup_action(
    $1, 'adr8-authority-worker', $2, now(),
    true, '26', $3, now(),
    'open', true, true, false, false, false
  )
`, [
  adr8CaseAReview.id,
  adr8CaseAReview.lease_generation,
  adr8CaseAContext.rows[0].anchor_external_message_id,
]);
if (
  adr8CaseAAfterBHumanTakeover.rows[0].decision !== 'execute' ||
  adr8CaseAAfterBHumanTakeover.rows[0].reason_code !== 'eligible_for_execution'
) {
  throw new Error('ADR-0008: case B human takeover paused case A');
}
console.log('adr8_human_takeover_is_scoped_to_case_conversation=OK');

// Same contact but a different WhatsApp identity must also fail closed. This
// isolates the identity predicate from the contact predicate.
const adr8CaseAContact = await db.query(`
  select contact_id from public.recovery_cases where id=$1
`, [adr8Case1]);
await db.query(`
  insert into public.channel_identities (
    id, contact_id, channel, account_id, external_user_id, identity_status
  ) values (
    '00000000-0000-0000-0000-0000000ad829', $1,
    'whatsapp', 'chatwoot:adr8-other-identity', '5599888800029', 'active'
  )
`, [adr8CaseAContact.rows[0].contact_id]);
const adr8SameContactOtherIdentityConversation = await db.query(`
  insert into public.conversations (
    contact_id, channel_identity_id, status, automation_status,
    human_takeover, commercial_context
  ) values (
    $1, '00000000-0000-0000-0000-0000000ad829',
    'active', 'enabled', false,
    jsonb_build_object('chatwoot_conversation_id', '29')
  ) returning id
`, [adr8CaseAContact.rows[0].contact_id]);
await db.exec('begin');
await db.query(`
  update public.recovery_cases set conversation_id=$2 where id=$1
`, [adr8Case1, adr8SameContactOtherIdentityConversation.rows[0].id]);
const adr8WrongIdentityContext = await db.query(`
  select * from public.get_followup_chatwoot_context(
    $1, 'adr8-authority-worker', $2, now()
  )
`, [adr8CaseAReview.id, adr8CaseAReview.lease_generation]);
let adr8WrongIdentityEvidenceBlocked = false;
try {
  await db.query(`
    select * from public.reevaluate_followup_action(
      $1, 'adr8-authority-worker', $2, now(),
      true, '29', 'wrong-identity-anchor', now(),
      'open', true, true, false, false, false
    )
  `, [adr8CaseAReview.id, adr8CaseAReview.lease_generation]);
} catch (error) {
  adr8WrongIdentityEvidenceBlocked = String(error.message).includes(
    'invalid_chatwoot_authority_evidence'
  );
}
await db.exec('rollback');
if (
  adr8WrongIdentityContext.rows[0].external_conversation_id !== null ||
  !adr8WrongIdentityEvidenceBlocked
) {
  throw new Error('ADR-0008: same-contact foreign identity bypassed authority fence');
}
console.log('adr8_same_contact_foreign_identity_fails_closed=OK');

// A conversation owned by another contact and identity must fail closed. The
// authoritative context must not expose that foreign conversation merely because its UUID was stored on the case.
await db.exec('begin');
await db.query(`
  update public.recovery_cases set conversation_id=$2 where id=$1
`, [adr8Case1, canonicalConversation.rows[0].id]);
const adr8ForeignContext = await db.query(`
  select * from public.get_followup_chatwoot_context(
    $1, 'adr8-authority-worker', $2, now()
  )
`, [adr8CaseAReview.id, adr8CaseAReview.lease_generation]);
let adr8ForeignEvidenceBlocked = false;
try {
  await db.query(`
    select * from public.reevaluate_followup_action(
      $1, 'adr8-authority-worker', $2, now(),
      true, '22', 'foreign-anchor', now(),
      'open', true, true, false, false, false
    )
  `, [adr8CaseAReview.id, adr8CaseAReview.lease_generation]);
} catch (error) {
  adr8ForeignEvidenceBlocked = String(error.message).includes(
    'invalid_chatwoot_authority_evidence'
  );
}
await db.exec('rollback');
if (adr8ForeignContext.rows[0].external_conversation_id !== null) {
  throw new Error('ADR-0008: foreign conversation leaked into case authority');
}
if (!adr8ForeignEvidenceBlocked) {
  throw new Error('ADR-0008: foreign conversation evidence bypassed reevaluation fence');
}
console.log('adr8_foreign_conversation_authority_fails_closed=OK');

// Regression: a fresh successor attempt inside case A cannot finalize against
// conversation "28" because case A is already canonically bound to "26".
const adr8CaseASequence = await db.query(`
  select revision from public.followup_sequences where id=$1
`, [adr8CaseAReview.followup_sequence_id]);
const adr8CaseASequenceRevision = adr8CaseASequence.rows[0].revision;
await authorizeExecute(
  adr8CaseAReview.id,
  'adr8-authority-worker',
  adr8CaseAReview.lease_generation,
  adr8CaseAReview.expected_case_version,
  adr8CaseASequenceRevision,
);
const adr8CaseAAttempt = await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1, 'adr8-authority-worker', $2, $3, $4,
    'whatsapp', 'freeform', now()
  )
`, [
  adr8CaseAReview.id,
  adr8CaseAReview.lease_generation,
  adr8CaseAReview.expected_case_version,
  adr8CaseASequenceRevision,
]);
await db.query(`
  select * from public.mark_followup_request_started(
    $1, $2, 'adr8-authority-worker', $3, now()
  )
`, [
  adr8CaseAReview.id,
  adr8CaseAAttempt.rows[0].id,
  adr8CaseAReview.lease_generation,
]);
let adr8MidCaseMismatchBlocked = false;
try {
  await db.query(`
    select * from public.record_and_finalize_followup_acceptance(
      $1, $2, 'adr8-authority-worker', $3,
      '28', 'adr8-msg-28', 'salto indebido', now()
    )
  `, [
    adr8CaseAReview.id,
    adr8CaseAAttempt.rows[0].id,
    adr8CaseAReview.lease_generation,
  ]);
} catch (error) {
  adr8MidCaseMismatchBlocked = String(error.message).includes('case_conversation_mismatch');
}
if (!adr8MidCaseMismatchBlocked) throw new Error('ADR-0008: case_conversation_mismatch did not block mid-case jump');
console.log('adr8_mid_case_conversation_jump_blocked=OK');

await db.exec(`
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values (
    '50000000-0000-0000-0000-000000000001',
    'hotmart', 'final-e2e-four-message-event',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
  insert into public.contacts (id, full_name) values (
    '50000000-0000-0000-0000-000000000002', 'Final E2E Chain Check'
  );
`);
const finalE2ePlan = await db.query(`
  select * from public.plan_cart_recovery_with_identity(
    '50000000-0000-0000-0000-000000000001',
    '50000000-0000-0000-0000-000000000002',
    'final-e2e-product', 'Final E2E Product', 'final-e2e-offer',
    'cart-recovery-e2e-final', 1,
    timestamptz '2099-02-01 00:00:00+00',
    1, 7, '5531888888888'
  )
`);
if (finalE2ePlan.rows.length !== 1 || finalE2ePlan.rows[0].created !== true) {
  throw new Error('final E2E four-message plan was not created');
}
const finalE2eAcceptedAt = [
  '2099-02-01T00:00:00.000Z',
  '2099-02-01T00:02:00.000Z',
  '2099-02-01T00:07:00.000Z',
  '2099-02-01T00:17:00.000Z',
];
for (let index = 0; index < finalE2eAcceptedAt.length; index += 1) {
  const pending = await db.query(`
    select id, due_at = $2::timestamptz as due_at_matches
    from public.scheduled_actions
    where recovery_case_id=$1 and status='pending'
  `, [finalE2ePlan.rows[0].recovery_case_id, finalE2eAcceptedAt[index]]);
  if (pending.rows.length !== 1) {
    throw new Error(`final E2E expected one pending action before message ${index + 1}`);
  }
  if (!pending.rows[0].due_at_matches) {
    throw new Error(`final E2E message ${index + 1} was not due at the exact relative delay`);
  }
  const claimed = await db.query(`
    select * from public.claim_due_followup_actions(
      'final-e2e-worker', $1::timestamptz, interval '1 minute', 100
    )
  `, [finalE2eAcceptedAt[index]]);
  const action = claimed.rows.find((row) => row.id === pending.rows[0].id);
  if (!action) throw new Error(`final E2E message ${index + 1} action was not due`);
  const sequence = await db.query(`
    select revision from public.followup_sequences where id=$1
  `, [action.followup_sequence_id]);
  await authorizeExecute(
    action.id,
    'final-e2e-worker',
    action.lease_generation,
    action.expected_case_version,
    sequence.rows[0].revision,
  );
  const attempt = await db.query(`
    select * from public.reserve_followup_delivery_attempt(
      $1, 'final-e2e-worker', $2, $3, $4,
      'whatsapp', 'freeform', $5::timestamptz
    )
  `, [
    action.id,
    action.lease_generation,
    action.expected_case_version,
    sequence.rows[0].revision,
    finalE2eAcceptedAt[index],
  ]);
  await db.query(`
    select * from public.mark_followup_request_started(
      $1, $2, 'final-e2e-worker', $3, $4::timestamptz
    )
  `, [
    action.id,
    attempt.rows[0].id,
    action.lease_generation,
    finalE2eAcceptedAt[index],
  ]);
  await db.query(`
    select * from public.record_and_finalize_followup_acceptance(
      $1, $2, 'final-e2e-worker', $3,
      '9901', $4, $5, $6::timestamptz
    )
  `, [
    action.id,
    attempt.rows[0].id,
    action.lease_generation,
    `final-e2e-message-${index + 1}`,
    `Mensaje E2E final ${index + 1}`,
    finalE2eAcceptedAt[index],
  ]);
}
const finalE2eState = await db.query(`
  select rc.status as case_status,
         fs.status as sequence_status,
         fs.automatic_messages_accepted,
         count(sa.id) filter (where sa.status='pending')::int as pending_actions,
         count(sa.id)::int as total_actions
  from public.recovery_cases rc
  join public.followup_sequences fs on fs.recovery_case_id=rc.id
  join public.scheduled_actions sa on sa.followup_sequence_id=fs.id
  where rc.id=$1
  group by rc.status, fs.status, fs.automatic_messages_accepted
`, [finalE2ePlan.rows[0].recovery_case_id]);
if (finalE2eState.rows.length !== 1
    || finalE2eState.rows[0].automatic_messages_accepted !== 4
    || finalE2eState.rows[0].pending_actions !== 0
    || finalE2eState.rows[0].total_actions !== 4
    || finalE2eState.rows[0].sequence_status !== 'completed') {
  throw new Error('final E2E policy did not complete exactly four accepted messages');
}
console.log('final_e2e_four_message_chain=OK');

await db.close();

const dirtyDb = new PGlite();
await dirtyDb.waitReady;
await dirtyDb.exec(baseline);
await dirtyDb.exec(`
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload
  ) values (
    '10000000-0000-0000-0000-000000000001', 'hotmart', 'legacy-event',
    'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb
  );
  insert into public.contacts (id, full_name) values (
    '10000000-0000-0000-0000-000000000002', 'Legacy Check'
  );
  insert into public.recovery_cases (
    id, contact_id, abandonment_event_id, source, external_product_id,
    product_name, status, grace_expires_at
  ) values (
    '10000000-0000-0000-0000-000000000003',
    '10000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000001',
    'hotmart', 'legacy-product', 'Legacy Product', 'grace_period', now()
  );
  insert into public.followup_sequences (
    recovery_case_id, reason, policy_key, policy_version, max_attempts
  ) values (
    '10000000-0000-0000-0000-000000000003',
    'manual', 'legacy-policy', 1, 3
  );
`);
let preflightBlocked = false;
try {
  await dirtyDb.exec(migration);
} catch (error) {
  preflightBlocked = String(error.message).includes('followup_engine_requires_empty_legacy_scheduler_tables');
}
if (!preflightBlocked) throw new Error('legacy scheduler preflight did not abort');
console.log('legacy_preflight_abort=OK');
await dirtyDb.close();

const conflictingPolicyDb = new PGlite();
await conflictingPolicyDb.waitReady;
await conflictingPolicyDb.exec(baseline);
await conflictingPolicyDb.exec(migration);
await conflictingPolicyDb.exec(identityBindingMigration);
await conflictingPolicyDb.exec(identityAuditMigration);
await conflictingPolicyDb.exec(contactAuthorizationGrantMigration);
await conflictingPolicyDb.exec(perCaseAnchorMigration);
await conflictingPolicyDb.exec(
  finalE2ePolicySeed.replaceAll(
    'operator-e2e-final-policy',
    'unexpected-approver',
  ),
);
let conflictingPolicyApprovalBlocked = false;
try {
  await conflictingPolicyDb.exec(finalE2ePolicySeed);
} catch (error) {
  conflictingPolicyApprovalBlocked = String(error.message).includes(
    'final_e2e_policy_v1_mismatch',
  );
}
if (!conflictingPolicyApprovalBlocked) {
  throw new Error('final E2E seed accepted conflicting approval identity');
}
console.log('final_e2e_conflicting_approval_blocked=OK');
await conflictingPolicyDb.close();
