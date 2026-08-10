import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const db = new PGlite();
await db.waitReady;
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
  'opt-out-test', 1, 'published', 'cart_recovery', 'UTC', '{}'::jsonb,
  interval '1 hour', interval '7 days', 3, '[]'::jsonb,
  'local-test', '2026-08-09T00:00:00Z', '2026-08-09T00:00:00Z'
);
insert into public.webhook_events (
  id, source, external_event_id, event_type, payload, processing_status, received_at
) values (
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1', 'hotmart', 'optout-abandonment-1',
  'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb, 'received', '2026-08-09T00:00:00Z'
);
insert into public.contacts (id, full_name, email, phone)
values (
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc1', 'Opt Out Fixture',
  'optout@example.test', '5531999999999'
);
`);

const plan = await db.query(`
select * from public.plan_cart_recovery_with_identity(
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1'::uuid,
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc1'::uuid,
  '123', 'Pilot Product', 'OFFER-1', 'opt-out-test', 1,
  '2026-08-09T00:00:00Z'::timestamptz,
  1, 7, '5531999999999'
);
`);
if (plan.rows.length !== 1) throw new Error('fixture plan was not created');
await db.exec(`
insert into public.followup_delivery_attempts (
  action_id, idempotency_key, attempt_number, channel, mode, phase,
  started_at, lease_generation, expected_case_version, expected_sequence_revision
)
select sa.id, sa.idempotency_key, 1, 'whatsapp', 'freeform', 'reserved',
       '2026-08-09T00:04:00Z', 1, rc.version, fs.revision
from public.scheduled_actions sa
join public.recovery_cases rc on rc.id = sa.recovery_case_id
join public.followup_sequences fs on fs.id = sa.followup_sequence_id
where sa.id = '${plan.rows[0].scheduled_action_id}'::uuid;
`);

const applied = await db.query(`
select * from public.apply_chatwoot_inbound_opt_out(
  1, 7, 42, 9001, '5531999999999',
  '2026-08-09T00:05:00Z'::timestamptz, 'unsubscribe'
);
`);
if (applied.rows[0]?.outcome !== 'applied') {
  throw new Error(`expected applied, got ${JSON.stringify(applied.rows)}`);
}
const exactState = await db.query(`
select
  c.contact_permission,
  c.lifecycle_status,
  rc.status as case_status,
  fs.status as sequence_status,
  sa.status as action_status,
  attempt.phase as attempt_phase,
  attempt.outcome as attempt_outcome,
  attempt.reason_code as attempt_reason,
  (select count(*)::int from public.contact_authorizations ca
    where ca.contact_id = c.id and ca.authorization_status = 'denied'
      and ca.valid_until is null) as active_denials,
  (select count(*)::int from public.contact_authorizations ca
    where ca.contact_id = c.id and ca.authorization_status = 'allowed'
      and ca.valid_until is null) as active_allowances
from public.contacts c
join public.recovery_cases rc on rc.contact_id = c.id
join public.followup_sequences fs on fs.recovery_case_id = rc.id
join public.scheduled_actions sa on sa.recovery_case_id = rc.id
join public.followup_delivery_attempts attempt on attempt.action_id = sa.id
where c.id = 'cccccccc-cccc-4ccc-8ccc-ccccccccccc1'::uuid;
`);
const state = exactState.rows[0];
if (state?.contact_permission !== 'opted_out'
    || state?.lifecycle_status !== 'do_not_contact'
    || state?.case_status !== 'cancelled'
    || state?.sequence_status !== 'cancelled'
    || state?.action_status !== 'cancelled'
    || state?.attempt_phase !== 'completed'
    || state?.attempt_outcome !== 'failed_before_request'
    || state?.attempt_reason !== 'contact_opted_out'
    || Number(state?.active_denials) !== 1
    || Number(state?.active_allowances) !== 0) {
  throw new Error(`unexpected exact opt-out state: ${JSON.stringify(state)}`);
}

const replay = await db.query(`
select * from public.apply_chatwoot_inbound_opt_out(
  1, 7, 42, 9001, '5531999999999',
  '2026-08-09T00:05:00Z'::timestamptz, 'unsubscribe'
);
`);
if (replay.rows[0]?.outcome !== 'already_applied') {
  throw new Error(`expected already_applied, got ${JSON.stringify(replay.rows)}`);
}

await db.exec(`
insert into public.webhook_events (
  id, source, external_event_id, event_type, payload, processing_status, received_at
) values (
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2', 'hotmart', 'optout-abandonment-2',
  'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb, 'received', '2026-08-09T00:10:00Z'
);
insert into public.contacts (id, full_name, email, phone)
values (
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc2', 'In Flight Opt Out Fixture',
  'optout2@example.test', '5531777777777'
);
`);
const inFlightPlan = await db.query(`
select * from public.plan_cart_recovery_with_identity(
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2'::uuid,
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc2'::uuid,
  '123', 'Pilot Product', 'OFFER-1', 'opt-out-test', 1,
  '2026-08-09T00:10:00Z'::timestamptz,
  1, 7, '5531777777777'
);
`);
await db.exec(`
insert into public.followup_delivery_attempts (
  action_id, idempotency_key, attempt_number, channel, mode, phase,
  started_at, request_started_at, lease_generation,
  expected_case_version, expected_sequence_revision
)
select sa.id, sa.idempotency_key, 1, 'whatsapp', 'freeform', 'request_started',
       '2026-08-09T00:14:00Z', '2026-08-09T00:14:30Z', 1,
       rc.version, fs.revision
from public.scheduled_actions sa
join public.recovery_cases rc on rc.id = sa.recovery_case_id
join public.followup_sequences fs on fs.id = sa.followup_sequence_id
where sa.id = '${inFlightPlan.rows[0].scheduled_action_id}'::uuid;
`);
const inFlightOptOut = await db.query(`
select * from public.apply_chatwoot_inbound_opt_out(
  1, 7, 44, 9003, '5531777777777',
  '2026-08-09T00:15:00Z'::timestamptz, 'stop_contacting'
);
`);
if (inFlightOptOut.rows[0]?.outcome !== 'applied') {
  throw new Error(`expected in-flight applied, got ${JSON.stringify(inFlightOptOut.rows)}`);
}
const inFlightState = await db.query(`
select sa.status as action_status, attempt.phase, attempt.outcome,
       attempt.reason_code, attempt.reconciliation_deadline
from public.scheduled_actions sa
join public.followup_delivery_attempts attempt on attempt.action_id = sa.id
where sa.id = '${inFlightPlan.rows[0].scheduled_action_id}'::uuid;
`);
const inFlight = inFlightState.rows[0];
if (inFlight?.action_status !== 'delivery_unknown'
    || inFlight?.phase !== 'completed'
    || inFlight?.outcome !== 'delivery_unknown'
    || inFlight?.reason_code !== 'contact_opted_out_after_request_started'
    || inFlight?.reconciliation_deadline == null) {
  throw new Error(`in-flight effect was not preserved as unknown: ${JSON.stringify(inFlight)}`);
}
await db.query(`
select * from public.finalize_followup_delivery_attempt(
  '${inFlightPlan.rows[0].scheduled_action_id}'::uuid,
  (select id from public.followup_delivery_attempts
   where action_id = '${inFlightPlan.rows[0].scheduled_action_id}'::uuid),
  'in-flight-worker', 1, 'rejected', null, null, 'provider_rejected',
  '2026-08-09T00:30:00Z'::timestamptz,
  null, '2026-08-09T00:16:00Z'::timestamptz
);
`);
const rejectedAfterStop = await db.query(`
select sa.status as action_status, sa.next_attempt_at,
       attempt.reconciliation_resolution,
       attempt.reconciliation_next_attempt_at,
       attempt.reason_code
from public.scheduled_actions sa
join public.followup_delivery_attempts attempt on attempt.action_id = sa.id
where sa.id = '${inFlightPlan.rows[0].scheduled_action_id}'::uuid;
`);
if (rejectedAfterStop.rows[0]?.action_status !== 'cancelled'
    || rejectedAfterStop.rows[0]?.next_attempt_at != null
    || rejectedAfterStop.rows[0]?.reconciliation_resolution !== 'not_applied'
    || rejectedAfterStop.rows[0]?.reconciliation_next_attempt_at != null
    || rejectedAfterStop.rows[0]?.reason_code !== 'contact_opted_out_not_applied') {
  throw new Error(`late rejection under opt-out was not terminal: ${JSON.stringify(rejectedAfterStop.rows)}`);
}
await db.query(`
select * from public.reconcile_followup_delivery_attempt(
  '${inFlightPlan.rows[0].scheduled_action_id}'::uuid,
  (select id from public.followup_delivery_attempts
   where action_id = '${inFlightPlan.rows[0].scheduled_action_id}'::uuid),
  1, 'not_applied', null, null, null,
  'contact_opted_out_not_applied', '2026-08-09T00:16:01Z'::timestamptz
);
`);

const unmatched = await db.query(`
select * from public.apply_chatwoot_inbound_opt_out(
  1, 7, 43, 9002, '5531888888888',
  '2026-08-09T00:06:00Z'::timestamptz, 'do_not_contact_again'
);
`);
if (unmatched.rows[0]?.outcome !== 'recorded_unmatched') {
  throw new Error(`expected recorded_unmatched, got ${JSON.stringify(unmatched.rows)}`);
}
const stop = await db.query(`select public.has_chatwoot_opt_out_stop(1, 7, 43, '5531999999999') as stopped;`);
if (stop.rows[0]?.stopped !== true) throw new Error('unmatched stop fact was not enforced');
const crossConversationStop = await db.query(`select public.has_chatwoot_opt_out_stop(1, 7, 999, '5531999999999') as stopped;`);
if (crossConversationStop.rows[0]?.stopped !== true) throw new Error('global contact stop was not enforced across conversations');

await db.exec(`
insert into public.contacts (id, full_name)
values ('cccccccc-cccc-4ccc-8ccc-ccccccccccc3', 'Late Correlation Fixture');
insert into public.webhook_events (
  id, source, external_event_id, event_type, payload, processing_status, received_at
) values (
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3', 'hotmart', 'optout-abandonment-3',
  'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb, 'received', '2026-08-09T00:07:00Z'
);
insert into public.channel_identities (
  contact_id, channel, account_id, external_user_id, identity_status, metadata
) values (
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc3', 'whatsapp', 'chatwoot:1',
  '5531888888888', 'active', jsonb_build_object('inbox_id', 7)
);
`);
const reverseOrderPlan = await db.query(`
select * from public.plan_cart_recovery_with_identity(
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3'::uuid,
  'cccccccc-cccc-4ccc-8ccc-ccccccccccc3'::uuid,
  '123', 'Pilot Product', 'OFFER-1', 'opt-out-test', 1,
  '2026-08-09T00:07:00Z'::timestamptz,
  1, 7, '5531888888888'
);
`);
const reverseAttempt = await db.query(`
insert into public.followup_delivery_attempts (
  action_id, idempotency_key, attempt_number, channel, mode, phase,
  started_at, lease_generation, expected_case_version, expected_sequence_revision
)
select sa.id, sa.idempotency_key, 1, 'whatsapp', 'freeform', 'reserved',
       '2026-08-09T00:08:00Z', 1, rc.version, fs.revision
from public.scheduled_actions sa
join public.recovery_cases rc on rc.id = sa.recovery_case_id
join public.followup_sequences fs on fs.id = sa.followup_sequence_id
where sa.id = '${reverseOrderPlan.rows[0].scheduled_action_id}'::uuid
returning id;
`);
let pendingStopBlockedRequestStart = false;
try {
  await db.query(`
  select * from public.mark_followup_request_started(
    '${reverseOrderPlan.rows[0].scheduled_action_id}'::uuid,
    '${reverseAttempt.rows[0].id}'::uuid,
    'reverse-order-worker', 1, '2026-08-09T00:08:01Z'::timestamptz
  );
  `);
} catch (error) {
  pendingStopBlockedRequestStart = String(error).includes('pending_chatwoot_opt_out_stop');
}
if (!pendingStopBlockedRequestStart) {
  throw new Error('reverse-order unmatched stop did not block request-start');
}
const reconciled = await db.query(`
select * from public.reconcile_chatwoot_opt_out_stop(1, 7, 43, '5531999999999');
`);
if (reconciled.rows[0]?.outcome !== 'applied'
    || reconciled.rows[0]?.matched_contact_id !== 'cccccccc-cccc-4ccc-8ccc-ccccccccccc3') {
  throw new Error(`pending stop was not reconciled: ${JSON.stringify(reconciled.rows)}`);
}
const reconciledState = await db.query(`
select c.contact_permission, c.lifecycle_status,
       (select count(*)::int from public.contact_opt_out_events e
        where e.canonical_conversation_id = 43) as event_count,
       (select correlation_status from public.contact_opt_out_events e
        where e.canonical_conversation_id = 43) as correlation_status
from public.contacts c
where c.id = 'cccccccc-cccc-4ccc-8ccc-ccccccccccc3'::uuid;
`);
if (reconciledState.rows[0]?.contact_permission !== 'opted_out'
    || reconciledState.rows[0]?.lifecycle_status !== 'do_not_contact'
    || Number(reconciledState.rows[0]?.event_count) !== 1
    || reconciledState.rows[0]?.correlation_status !== 'applied') {
  throw new Error(`reconciled stop state invalid: ${JSON.stringify(reconciledState.rows)}`);
}

const projectionClaim = await db.query(`
select * from public.claim_chatwoot_opt_out_projections(
  'projection-worker-1', '2026-08-09T00:20:00Z'::timestamptz,
  interval '1 minute', 1
);
`);
if (projectionClaim.rows.length !== 1
    || projectionClaim.rows[0]?.opt_out_event_id !== applied.rows[0]?.opt_out_event_id) {
  throw new Error(`unexpected projection claim: ${JSON.stringify(projectionClaim.rows)}`);
}
await db.query(`
select * from public.finalize_chatwoot_opt_out_projection(
  '${projectionClaim.rows[0].opt_out_event_id}'::uuid,
  'projection-worker-1', ${projectionClaim.rows[0].lease_generation},
  false, 'chatwoot_unavailable', 2, '2026-08-09T00:20:01Z'::timestamptz
);
`);
const retryState = await db.query(`
select projection_status, projection_attempt_count, projection_next_attempt_at,
       projection_error_code
from public.contact_opt_out_events
where id = '${projectionClaim.rows[0].opt_out_event_id}'::uuid;
`);
if (retryState.rows[0]?.projection_status !== 'retryable_failed'
    || Number(retryState.rows[0]?.projection_attempt_count) !== 1
    || retryState.rows[0]?.projection_next_attempt_at == null
    || retryState.rows[0]?.projection_error_code !== 'chatwoot_unavailable') {
  throw new Error(`projection retry was not durable: ${JSON.stringify(retryState.rows)}`);
}
let lostResponseReplayBlocked = false;
try {
  await db.query(`
    select * from public.finalize_chatwoot_opt_out_projection(
      '${projectionClaim.rows[0].opt_out_event_id}'::uuid,
      'projection-worker-1', ${projectionClaim.rows[0].lease_generation},
      false, 'chatwoot_unavailable', 2, '2026-08-09T00:20:02Z'::timestamptz
    );
  `);
} catch (error) {
  lostResponseReplayBlocked = String(error).includes('chatwoot_opt_out_projection_lease_not_found');
}
if (!lostResponseReplayBlocked) throw new Error('projection failure replay bypassed the cleared lease');
const replayState = await db.query(`
  select projection_status, projection_attempt_count
  from public.contact_opt_out_events
  where id = '${projectionClaim.rows[0].opt_out_event_id}'::uuid;
`);
if (replayState.rows[0]?.projection_status !== 'retryable_failed'
    || replayState.rows[0]?.projection_attempt_count !== 1) {
  throw new Error(`projection failure replay mutated state: ${JSON.stringify(replayState.rows)}`);
}
const retryClaim = await db.query(`
select * from public.claim_chatwoot_opt_out_projections(
  'projection-worker-1', '2026-08-09T00:21:00Z'::timestamptz,
  interval '1 minute', 1
);
`);
if (retryClaim.rows[0]?.opt_out_event_id !== applied.rows[0]?.opt_out_event_id) {
  throw new Error(`projection was not reclaimed: ${JSON.stringify(retryClaim.rows)}`);
}
await db.query(`
select * from public.finalize_chatwoot_opt_out_projection(
  '${retryClaim.rows[0].opt_out_event_id}'::uuid,
  'projection-worker-1', ${retryClaim.rows[0].lease_generation},
  true, null, 2, '2026-08-09T00:21:01Z'::timestamptz
);
`);
const appliedProjection = await db.query(`
select projection_status, projection_attempt_count
from public.contact_opt_out_events
where id = '${retryClaim.rows[0].opt_out_event_id}'::uuid;
`);
if (appliedProjection.rows[0]?.projection_status !== 'applied'
    || Number(appliedProjection.rows[0]?.projection_attempt_count) !== 2) {
  throw new Error(`projection was not finalized: ${JSON.stringify(appliedProjection.rows)}`);
}

let relaxationBlocked = false;
try {
  await db.exec(`
    update public.contacts
    set contact_permission = 'allowed', lifecycle_status = 'lead'
    where id = 'cccccccc-cccc-4ccc-8ccc-ccccccccccc1'::uuid;
  `);
} catch (error) {
  relaxationBlocked = String(error).includes('authoritative_opt_out_reauthorization_required');
}
if (!relaxationBlocked) throw new Error('authoritative contact opt-out could be relaxed directly');

console.log('INBOUND_OPT_OUT_DURABLE_OK');
