import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { PGlite } from '@electric-sql/pglite';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const baseline = (await readFile(`${root}/supabase/baseline/20260803_public_schema.sql`, 'utf8'))
  .replace('create extension if not exists pgcrypto;', '-- omitted in PGlite: extension unavailable');
const migration = await readFile(`${root}/supabase/migrations/20260803000100_followup_engine_v1.sql`, 'utf8');
const db = new PGlite();
await db.waitReady;
await db.exec(baseline);
console.log('baseline_apply=OK');
await db.exec(migration);
console.log('migration_apply=OK');
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
    '[{"days":[1,2,3,4,5,6],"start":"09:00","end":"19:00"}]'::jsonb,
    interval '1 hour', interval '7 days', 3,
    '[{"step_key":"first_contact"},{"step_key":"followup_1","delay":"24 hours"}]'::jsonb,
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
console.log('plan_idempotency=OK');

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

const started = await db.query(`
  select * from public.mark_followup_request_started(
    $1, $2, 'schema-check-worker', 1, now()
  )
`, [actionId, attemptId]);
if (started.rows[0].phase !== 'request_started') throw new Error('request start failed');
console.log('request_started=OK');

const finalized = await db.query(`
  select * from public.finalize_followup_delivery_attempt(
    $1, $2, 'schema-check-worker', 1, 'accepted_by_chatwoot',
    'remote-message-test', '00000000-0000-0000-0000-000000000005',
    'accepted', null, null, now()
  )
`, [actionId, attemptId]);
if (finalized.rows[0].status !== 'accepted_by_chatwoot') throw new Error('finalize failed');
const finalizedAgain = await db.query(`
  select * from public.finalize_followup_delivery_attempt(
    $1, $2, 'schema-check-worker', 1, 'accepted_by_chatwoot',
    'remote-message-test', '00000000-0000-0000-0000-000000000005',
    'accepted', null, null, now()
  )
`, [actionId, attemptId]);
if (finalizedAgain.rows[0].status !== 'accepted_by_chatwoot') throw new Error('idempotent finalize failed');
let changedFinalizeAnchorBlocked = false;
try {
  await db.query(`
    select * from public.finalize_followup_delivery_attempt(
      $1, $2, 'schema-check-worker', 1, 'accepted_by_chatwoot',
      'remote-message-test', '00000000-0000-0000-0000-000000000099',
      'accepted', null, null, now()
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
  select action_type, status, anchor_type, anchor_subject_internal_id
  from public.scheduled_actions
  where followup_sequence_id = $1 and status = 'pending'
`, [plan1.rows[0].followup_sequence_id]);
if (successor.rows.length !== 1 || successor.rows[0].action_type !== 'no_reply_review') throw new Error('next review missing');
if (successor.rows[0].anchor_type !== 'accepted_outbound_message') throw new Error('next review anchor missing');
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
  select * from public.finalize_followup_delivery_attempt(
    $1, $2, 'near-expiry-worker', 1, 'accepted_by_chatwoot', 'cw-near-expiry',
    '00000000-0000-0000-0000-000000000011', 'accepted', null, null, now()
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
const concurrentClaim = await db.query(`
  select * from public.claim_due_followup_actions('concurrent-worker', now(), interval '5 minutes', 10)
`);
const concurrentAction = concurrentClaim.rows.find((row) => row.id === concurrentPlan.rows[0].scheduled_action_id);
if (!concurrentAction) throw new Error('concurrent action not claimed');
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
  select * from public.finalize_followup_delivery_attempt(
    $1, $2, 'concurrent-worker', 1, 'accepted_by_chatwoot', 'cw-concurrent',
    '00000000-0000-0000-0000-000000000014', 'accepted', null, null, now()
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
const takeoverAttempt = await db.query(`
  select * from public.reserve_followup_delivery_attempt(
    $1, 'takeover-worker', 1, 1, 1, 'whatsapp', 'freeform', now()
  )
`, [takeoverAction.id]);
await db.query(`
  update public.recovery_cases
  set status='paused', version=version+1
  where id=$1
`, [takeoverPlan.rows[0].recovery_case_id]);
await db.query(`
  update public.followup_sequences
  set status='paused', revision=revision+1
  where id=$1
`, [takeoverPlan.rows[0].followup_sequence_id]);
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
if (!takeoverBlocked) throw new Error('authoritative change did not block request start');
const takeoverAttemptState = await db.query(`
  select phase from public.followup_delivery_attempts where id=$1
`, [takeoverAttempt.rows[0].id]);
if (takeoverAttemptState.rows[0].phase !== 'reserved') throw new Error('blocked request start mutated attempt phase');
console.log('authoritative_change_before_request_blocked=OK');

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
  const claim = await db.query(`
    select * from public.claim_due_followup_actions($1, now(), interval '5 minutes', 1)
  `, [worker]);
  const action = claim.rows[0];
  if (!action || action.recovery_case_id !== plan.rows[0].recovery_case_id) throw new Error(`reconciliation ${suffix} not claimed`);
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
    select * from public.reconcile_followup_delivery_attempt(
      $1, $2, 1, 'accepted_by_chatwoot', 'cw-too-late',
      '40000000-0000-0000-0000-000000000098', null,
      'message_found_too_late', now() + interval '2 hours'
    )
  `, [foundFixture.actionId, foundFixture.attemptId]);
} catch (error) {
  lateAcceptanceBlocked = String(error.message).includes('reconciliation_window_expired');
}
if (!lateAcceptanceBlocked) throw new Error('late acceptance bypassed reconciliation deadline');
await db.query(`
  select * from public.reconcile_followup_delivery_attempt(
    $1, $2, 1, 'accepted_by_chatwoot', 'cw-reconciled',
    '40000000-0000-0000-0000-000000000021', null, 'message_found', now()
  )
`, [foundFixture.actionId, foundFixture.attemptId]);
await db.query(`
  select * from public.reconcile_followup_delivery_attempt(
    $1, $2, 1, 'accepted_by_chatwoot', 'cw-reconciled',
    '40000000-0000-0000-0000-000000000021', null, 'message_found', now()
  )
`, [foundFixture.actionId, foundFixture.attemptId]);
let changedAcceptedAnchorBlocked = false;
try {
  await db.query(`
    select * from public.reconcile_followup_delivery_attempt(
      $1, $2, 1, 'accepted_by_chatwoot', 'cw-reconciled',
      '40000000-0000-0000-0000-000000000099', null, 'message_found', now()
    )
  `, [foundFixture.actionId, foundFixture.attemptId]);
} catch (error) {
  changedAcceptedAnchorBlocked = String(error.message).includes('delivery_attempt_already_reconciled_differently');
}
if (!changedAcceptedAnchorBlocked) throw new Error('reconciliation accepted a different durable anchor');
const foundState = await db.query(`
  select sa.status, fda.outcome, fda.reconciliation_resolution,
         (select count(*)::int from public.scheduled_actions x
          where x.followup_sequence_id=sa.followup_sequence_id and x.id<>sa.id) as successor_count,
         (select count(*)::int from public.conversation_events ce
          where ce.related_action_id=sa.id and ce.event_type='followup_delivery_reconciled') as audit_count
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
console.log('expiration_boundaries_fail_closed=OK');

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
