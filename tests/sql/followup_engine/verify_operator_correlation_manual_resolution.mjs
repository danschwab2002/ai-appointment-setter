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
insert into public.purchase_intents (
  id, tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
  normalized_email, normalized_phone, submitted_at, lifecycle_state,
  current_classification, whatsapp_contact_authorized, provisional,
  provider_observed, activation_authorized
) values
('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1', 'lancemos', 'psicologajohanna',
 'fixture', 'f106691755g', 'bxjge6zq', 'a@resolution.invalid', '593999999991',
 '2026-08-24T09:00:00Z', 'waiting_for_purchase', 'tracking_incomplete',
 false, true, false, false),
('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2', 'lancemos', 'psicologajohanna',
 'fixture', 'F106691755G', 'bxjge6zq', 'a@resolution.invalid', '593999999992',
 '2026-08-24T09:05:00Z', 'waiting_for_purchase', 'tracking_incomplete',
 false, true, false, false),
('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3', 'lancemos', 'psicologajohanna',
 'fixture', 'f106691755g', 'bxjge6zq', 'b@resolution.invalid', '593999999993',
 '2026-08-24T09:10:00Z', 'waiting_for_purchase', 'tracking_incomplete',
 false, true, false, false),
('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4', 'lancemos', 'psicologajohanna',
 'fixture', 'f106691755g', 'bxjge6zq', 'b@resolution.invalid', '593999999994',
 '2026-08-24T09:15:00Z', 'waiting_for_purchase', 'tracking_incomplete',
 false, true, false, false);

insert into public.webhook_events (
  id, source, external_event_id, event_type, payload, processing_status, received_at
) values
('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1', 'hotmart', 'resolution-link',
 'PURCHASE_APPROVED', '{}'::jsonb, 'received', '2026-08-24T10:00:00Z'),
('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2', 'hotmart', 'resolution-close',
 'PURCHASE_APPROVED', '{}'::jsonb, 'received', '2026-08-24T10:05:00Z'),
('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3', 'hotmart', 'resolution-stale',
 'PURCHASE_APPROVED', '{}'::jsonb, 'received', '2026-08-24T10:10:00Z');

insert into public.hotmart_purchase_intent_event_identities (
  webhook_event_id, normalized_email, normalized_phone
) values
('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1', 'a@resolution.invalid', '593999999999'),
('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2', 'none@resolution.invalid', '593988888888'),
('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3', 'b@resolution.invalid', '593999999998');

insert into public.hotmart_purchase_intent_correlations (
  webhook_event_id, scope_id, event_type, outcome, purchase_intent_id,
  matched_by, candidate_count, reason_code, manual_handoff_required, observed_at
) values
('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1',
 (select id from public.hotmart_purchase_intent_scopes where active limit 1),
 'PURCHASE_APPROVED', 'ambiguous', null, null, 2, 'multiple_candidates', true,
 '2026-08-24T10:00:00Z'),
('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2',
 (select id from public.hotmart_purchase_intent_scopes where active limit 1),
 'PURCHASE_APPROVED', 'unmatched', null, null, 0, 'identity_not_found', true,
 '2026-08-24T10:05:00Z'),
('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3',
 (select id from public.hotmart_purchase_intent_scopes where active limit 1),
 'PURCHASE_APPROVED', 'ambiguous', null, null, 2, 'multiple_candidates', true,
 '2026-08-24T10:10:00Z');

insert into public.hotmart_purchase_intent_correlation_candidates (
  webhook_event_id, purchase_intent_id, email_match, phone_match
) values
('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1',
 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1', true, false),
('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1',
 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2', true, false),
('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3',
 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3', true, false),
('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3',
 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4', true, false);
`);

const caseFoldedDetail = await db.query(`
  select case_data from public.get_operator_unresolved_correlation(
    'lancemos', 'psicologajohanna',
    'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1'::uuid
  )
`);
const caseFoldedCandidates = caseFoldedDetail.rows[0]?.case_data?.candidates;
if (!Array.isArray(caseFoldedCandidates)
    || caseFoldedCandidates.length !== 2
    || !caseFoldedCandidates.some((candidate) =>
      candidate.purchase_intent_id === 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2')) {
  throw new Error(`case-folded candidate missing: ${JSON.stringify(caseFoldedCandidates)}`);
}

const before = await db.query(`
  select id, lifecycle_state, current_classification, activation_authorized, updated_at
  from public.purchase_intents order by id
`);

await db.exec('set role service_role');
const preparedLink = await db.query(`
  select command_data from public.prepare_operator_correlation_resolution(
    'lancemos', 'psicologajohanna', 'juan-operator',
    'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1'::uuid,
    'resolve_with_candidate',
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2'::uuid,
    'operator_source_record',
    '77777777-7777-4777-8777-777777777771'::uuid
  )
`);
const preparedLinkReplay = await db.query(`
  select command_data from public.prepare_operator_correlation_resolution(
    'lancemos', 'psicologajohanna', 'juan-operator',
    'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1'::uuid,
    'resolve_with_candidate',
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2'::uuid,
    'operator_source_record',
    '77777777-7777-4777-8777-777777777771'::uuid
  )
`);
await db.exec('reset role');
let forgedCommandBlocked = false;
try {
  await db.exec(`
    insert into public.operator_correlation_resolution_commands (
      id, idempotency_key, request_fingerprint, webhook_event_id, scope_id,
      tenant_ref, funnel_ref, product_ref, offer_ref, actor_ref, action,
      selected_purchase_intent_id, verification_basis, deterministic_outcome,
      deterministic_reason_code, candidate_count, candidate_snapshot,
      prepared_at, expires_at
    )
    select
      '55555555-5555-4555-8555-555555555551'::uuid,
      '77777777-7777-4777-8777-777777777779'::uuid,
      jsonb_build_object(
        'tenant_ref', command.tenant_ref,
        'funnel_ref', command.funnel_ref,
        'actor_ref', command.actor_ref,
        'webhook_event_id', command.webhook_event_id,
        'action', command.action,
        'selected_purchase_intent_id',
          'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3'::uuid,
        'verification_basis', command.verification_basis
      ),
      command.webhook_event_id, command.scope_id, command.tenant_ref,
      command.funnel_ref, command.product_ref, command.offer_ref,
      command.actor_ref, command.action,
      'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3'::uuid,
      command.verification_basis, command.deterministic_outcome,
      command.deterministic_reason_code, command.candidate_count,
      command.candidate_snapshot, command.prepared_at, command.expires_at
    from public.operator_correlation_resolution_commands command
    where command.id = '${preparedLink.rows[0].command_data.command_id}'::uuid
  `);
} catch (error) {
  forgedCommandBlocked = String(error).includes(
    'operator_correlation_resolution_command_invalid',
  );
}
if (!forgedCommandBlocked) {
  throw new Error('owner-level forged candidate command was not blocked');
}
await db.exec('set role service_role');
let prepareConflictBlocked = false;
try {
  await db.query(`
    select command_data from public.prepare_operator_correlation_resolution(
      'lancemos', 'psicologajohanna', 'juan-operator',
      'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1'::uuid,
      'close_without_match', null, 'no_valid_candidate_after_review',
      '77777777-7777-4777-8777-777777777771'::uuid
    )
  `);
} catch (error) {
  prepareConflictBlocked = String(error).includes(
    'operator_correlation_idempotency_conflict',
  );
}
await db.exec('reset role');
const linkCommand = preparedLink.rows[0]?.command_data;
const replayedPrepare = preparedLinkReplay.rows[0]?.command_data;
if (!linkCommand?.requires_human_approval || !linkCommand?.automation_blocked) {
  throw new Error(`unsafe prepare response: ${JSON.stringify(linkCommand)}`);
}
if (replayedPrepare?.command_id !== linkCommand?.command_id
    || replayedPrepare?.idempotency_key !== linkCommand?.idempotency_key
    || !prepareConflictBlocked) {
  throw new Error(`prepare idempotency diverged: ${JSON.stringify({
    linkCommand, replayedPrepare, prepareConflictBlocked,
  })}`);
}
const afterPrepare = await db.query(`
  select
    (select count(*)::integer from public.operator_correlation_resolution_commands) commands,
    (select count(*)::integer from public.operator_correlation_resolutions) resolutions
`);
if (afterPrepare.rows[0]?.commands !== 1 || afterPrepare.rows[0]?.resolutions !== 0) {
  throw new Error(`prepare applied resolution: ${JSON.stringify(afterPrepare.rows)}`);
}

await db.exec('set role service_role');
const appliedLink = await db.query(`
  select resolution_data from public.confirm_operator_correlation_resolution(
    'lancemos', 'psicologajohanna', 'juan-operator',
    '${linkCommand.command_id}'::uuid,
    'resolve_with_candidate',
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2'::uuid
  )
`);
const replayedLink = await db.query(`
  select resolution_data from public.confirm_operator_correlation_resolution(
    'lancemos', 'psicologajohanna', 'juan-operator',
    '${linkCommand.command_id}'::uuid,
    'resolve_with_candidate',
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2'::uuid
  )
`);
await db.exec('reset role');
const applied = appliedLink.rows[0]?.resolution_data;
const replayed = replayedLink.rows[0]?.resolution_data;
if (applied?.resolution_outcome !== 'linked_candidate'
    || applied?.effective_purchase_intent_id !== 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2'
    || applied?.replayed !== false
    || replayed?.resolution_id !== applied?.resolution_id
    || replayed?.replayed !== true) {
  throw new Error(`link/replay diverged: ${JSON.stringify({ applied, replayed })}`);
}

await db.exec('set role service_role');
const preparedClose = await db.query(`
  select command_data from public.prepare_operator_correlation_resolution(
    'lancemos', 'psicologajohanna', 'juan-operator',
    'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2'::uuid,
    'close_without_match', null, 'no_valid_candidate_after_review',
    '77777777-7777-4777-8777-777777777772'::uuid
  )
`);
const closeCommand = preparedClose.rows[0]?.command_data;
const appliedClose = await db.query(`
  select resolution_data from public.confirm_operator_correlation_resolution(
    'lancemos', 'psicologajohanna', 'juan-operator',
    '${closeCommand.command_id}'::uuid, 'close_without_match', null
  )
`);
await db.exec('reset role');
if (appliedClose.rows[0]?.resolution_data?.resolution_outcome !== 'closed_without_match'
    || appliedClose.rows[0]?.resolution_data?.effective_purchase_intent_id !== null) {
  throw new Error(`close diverged: ${JSON.stringify(appliedClose.rows)}`);
}

await db.exec('set role service_role');
const preparedStale = await db.query(`
  select command_data from public.prepare_operator_correlation_resolution(
    'lancemos', 'psicologajohanna', 'juan-operator',
    'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3'::uuid,
    'resolve_with_candidate',
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3'::uuid,
    'external_transaction_reference',
    '77777777-7777-4777-8777-777777777773'::uuid
  )
`);
await db.exec('reset role');
const staleCommand = preparedStale.rows[0]?.command_data;
await db.exec(`
  update public.purchase_intents
  set updated_at = updated_at + interval '1 second'
  where id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3'::uuid
`);
let staleBlocked = false;
try {
  await db.exec('set role service_role');
  await db.query(`
    select resolution_data from public.confirm_operator_correlation_resolution(
      'lancemos', 'psicologajohanna', 'juan-operator',
      '${staleCommand.command_id}'::uuid,
      'resolve_with_candidate',
      'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3'::uuid
    )
  `);
} catch (error) {
  staleBlocked = String(error).includes('operator_correlation_stale_evidence');
} finally {
  await db.exec('reset role');
}
if (!staleBlocked) throw new Error('changed candidate snapshot was confirmed');

const pending = await db.query(`
  select case_data from public.list_operator_unresolved_correlations(
    'lancemos', 'psicologajohanna', 20, null
  )
`);
if (pending.rows.length !== 1
    || pending.rows[0]?.case_data?.webhook_event_id
      !== 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3') {
  throw new Error(`resolved cases remained pending: ${JSON.stringify(pending.rows)}`);
}

const originals = await db.query(`
  select webhook_event_id, outcome, purchase_intent_id, manual_handoff_required
  from public.hotmart_purchase_intent_correlations order by webhook_event_id
`);
if (originals.rows.some((row) => row.purchase_intent_id !== null
    || row.manual_handoff_required !== true)
    || originals.rows.map((row) => row.outcome).join(',') !== 'ambiguous,unmatched,ambiguous') {
  throw new Error(`deterministic evidence changed: ${JSON.stringify(originals.rows)}`);
}
const after = await db.query(`
  select id, lifecycle_state, current_classification, activation_authorized, updated_at
  from public.purchase_intents order by id
`);
for (let index = 0; index < 2; index += 1) {
  const beforeRow = before.rows[index];
  const afterRow = after.rows[index];
  if (JSON.stringify(beforeRow) !== JSON.stringify(afterRow)) {
    throw new Error(`resolved intent changed: ${JSON.stringify({ beforeRow, afterRow })}`);
  }
}
if (after.rows.some((row) => row.activation_authorized !== false)) {
  throw new Error('manual resolution authorized activation');
}

let directDmlBlocked = false;
try {
  await db.exec(`
    set role service_role;
    delete from public.operator_correlation_resolutions;
    reset role;
  `);
} catch (error) {
  directDmlBlocked = String(error).includes('permission denied');
  await db.exec('reset role');
}
if (!directDmlBlocked) throw new Error('service_role received direct resolution DML');

console.log('operator_correlation_manual_resolution=OK');
console.log('operator_correlation_manual_resolution_replay=OK');
console.log('operator_correlation_manual_resolution_stale_guard=OK');
console.log('operator_correlation_manual_resolution_owner_forgery_guard=OK');
console.log('operator_correlation_manual_resolution_zero_effects=OK');
await db.close();
