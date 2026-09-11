import { PGlite } from '@electric-sql/pglite';
import { readFileSync } from 'node:fs';
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
  alter default privileges in schema public grant all on tables to service_role;
`);
await db.exec(readFileSync(
  join(root, 'supabase/migrations/20260910000100_daily_feedback_production_v1.sql'),
  'utf8',
));
await db.exec('set role service_role');

const configured = (await db.query(`
  select public.configure_daily_feedback_scope_v1(
    '10000000-0000-4000-8000-000000000001', '${'1'.repeat(64)}',
 'lancemos', 'psicologajohanna-agent-bot-19', 'juan',
 'https://slack.com', 'https://slack.com/user_id/U12345678',
 'T12345678', 'U12345678', 1, 2, 19, 'UTC', '23:55:00', 72,
    'deterministic-redaction-v1', 'chatwoot-daily-agent-dialogues-v1',
    'daily-feedback-web-v1', true
  ) as result
`)).rows[0].result;
if (configured.status !== 'configured' || configured.enabled !== true) {
  throw new Error(`scope configuration failed: ${JSON.stringify(configured)}`);
}

await db.exec('reset role');
const initialBindingGeneration = (await db.query(`
  select binding_generation::int as generation
  from public.daily_feedback_reviewer_bindings
  where tenant_ref='lancemos' and scope_ref='psicologajohanna-agent-bot-19'
`)).rows[0].generation;
await db.exec('set role service_role');
await db.query(`
  select public.configure_daily_feedback_scope_v1(
    '10000000-0000-4000-8000-000000000099', '${'8'.repeat(64)}',
    'lancemos', 'psicologajohanna-agent-bot-19', 'juan',
    'https://slack.com', 'https://slack.com/user_id/U12345678',
    'T12345678', 'U12345678', 1, 2, 19, 'UTC', '23:55:00', 72,
    'deterministic-redaction-v1', 'chatwoot-daily-agent-dialogues-v1',
    'daily-feedback-web-v1', true
  )
`);
await db.exec('reset role');
const unchangedBindingGeneration = (await db.query(`
  select binding_generation::int as generation
  from public.daily_feedback_reviewer_bindings
  where tenant_ref='lancemos' and scope_ref='psicologajohanna-agent-bot-19'
`)).rows[0].generation;
await db.exec('set role service_role');
if (unchangedBindingGeneration !== initialBindingGeneration) {
  throw new Error('UNCHANGED_CONFIGURATION_INVALIDATED_REVIEWER');
}

const claimed = (await db.query(`
  select public.claim_daily_feedback_collection_v1(
    '20000000-0000-4000-8000-000000000001', '${'2'.repeat(64)}',
    'collector-1', 'lancemos', 'psicologajohanna-agent-bot-19', (
      ((clock_timestamp() at time zone 'UTC')::date::timestamp + time '23:55:00')
      at time zone 'UTC'
    ), true, 120
  ) as result
`)).rows[0].result;
if (claimed.status !== 'claimed' || claimed.lease_generation !== 1) {
  throw new Error(`collection claim failed: ${JSON.stringify(claimed)}`);
}
if (claimed.chatwoot_account_id !== 1 || claimed.chatwoot_inbox_id !== 2 || claimed.chatwoot_agent_bot_id !== 19) {
  throw new Error(`collection claim omitted canonical Chatwoot authority: ${JSON.stringify(claimed)}`);
}
if (new Date(claimed.window_end).getTime() - new Date(claimed.window_start).getTime() !== 24 * 60 * 60 * 1000) {
  throw new Error(`daily cutoff window had a gap or overlap: ${JSON.stringify(claimed)}`);
}

const prospectAt = new Date(new Date(claimed.window_start).getTime() + 60 * 60 * 1000).toISOString();
const agentAt = new Date(new Date(claimed.window_start).getTime() + 61 * 60 * 1000).toISOString();

const items = [{
  conversation_ref: 'conv_1234567890abcdef1234',
  display_label: 'Conversación 01',
  apparent_objective: 'Revisar la respuesta del agente',
  observed_outcome: 'El prospecto continuó la conversación',
  release_id: 'release_lineage_unavailable',
  release_version: 0,
  messages: [
    { actor: 'prospect', occurred_at: prospectAt, text: 'Quiero conocer el programa' },
    { actor: 'agent', occurred_at: agentAt, text: 'Claro, te explico cómo funciona' },
  ],
}];
const committed = (await db.query(`
  select public.commit_daily_feedback_batch_v1(
    '30000000-0000-4000-8000-000000000001', '${'3'.repeat(64)}',
    'collector-1', '${claimed.schedule_id}', ${claimed.lease_generation},
    '${'a'.repeat(64)}', 'release_lineage_unavailable', $1::jsonb
  ) as result
`, [JSON.stringify(items)])).rows[0].result;
if (committed.status !== 'committed' || committed.item_count !== 1) {
  throw new Error(`batch commit failed: ${JSON.stringify(committed)}`);
}
const replay = (await db.query(`
  select public.commit_daily_feedback_batch_v1(
    '30000000-0000-4000-8000-000000000001', '${'3'.repeat(64)}',
    'collector-1', '${claimed.schedule_id}', ${claimed.lease_generation},
    '${'a'.repeat(64)}', 'release_lineage_unavailable', $1::jsonb
  ) as result
`, [JSON.stringify(items)])).rows[0].result;
if (replay.status !== 'replayed' || replay.batch_id !== committed.batch_id) {
  throw new Error(`exact replay failed: ${JSON.stringify(replay)}`);
}
const nextClaim = (await db.query(`select public.claim_daily_feedback_collection_v1(
  '21000000-0000-4000-8000-000000000001', '${'d'.repeat(64)}',
  'collector-2', 'lancemos', 'psicologajohanna-agent-bot-19',
  '${claimed.window_end}'::timestamptz + interval '1 day', true, 120
) as result`)).rows[0].result;
if (new Date(nextClaim.window_start).getTime() !== new Date(claimed.window_end).getTime()) {
  throw new Error(`successive collection windows were not contiguous: ${JSON.stringify({claimed, nextClaim})}`);
}

const notifyClaim = (await db.query(`select public.claim_daily_feedback_notification_v1(
  '31000000-0000-4000-8000-000000000001', '${'5'.repeat(64)}',
  'notifier-1', 'lancemos', 'psicologajohanna-agent-bot-19', clock_timestamp(), 120
) as result`)).rows[0].result;
await db.query("set role postgres");
await db.query(`update public.daily_feedback_batches
  set retention_expires_at=clock_timestamp()-interval '1 second'
  where id='${notifyClaim.batch_id}'`);
await db.query("set role service_role");
let expiredStartRejected = false;
try {
  await db.query(`select public.mark_daily_feedback_notification_started_v1(
    '31500000-0000-4000-8000-000000000001', '${'e'.repeat(64)}',
    'notifier-1', '${notifyClaim.batch_id}', ${notifyClaim.lease_generation}
  )`);
} catch (error) {
  expiredStartRejected = String(error.message).includes('notification_expired');
}
if (!expiredStartRejected) throw new Error('expired notification request-start was not fenced');
await db.query("set role postgres");
await db.query(`update public.daily_feedback_batches
  set retention_expires_at=clock_timestamp()+interval '72 hours'
  where id='${notifyClaim.batch_id}'`);
await db.query("set role service_role");
await db.query(`select public.mark_daily_feedback_notification_started_v1(
  '32000000-0000-4000-8000-000000000001', '${'6'.repeat(64)}',
  'notifier-1', '${notifyClaim.batch_id}', ${notifyClaim.lease_generation}
)`);
await db.query("set role postgres");
await db.query(`update public.daily_feedback_batches
  set retention_expires_at=clock_timestamp()-interval '1 second'
  where id='${notifyClaim.batch_id}'`);
await db.query("set role service_role");
let expiredRetryRejected = false;
try {
  await db.query(`select public.retry_daily_feedback_notification_v1(
    '32500000-0000-4000-8000-000000000001', '${'f'.repeat(64)}',
    'notifier-1', '${notifyClaim.batch_id}', ${notifyClaim.lease_generation},
    'admission_transport_uncertain', 60
  )`);
} catch (error) {
  expiredRetryRejected = String(error.message).includes('notification_expired');
}
if (!expiredRetryRejected) throw new Error('expired request-start retry was not fenced');
await db.query("set role postgres");
await db.query(`update public.daily_feedback_batches
  set retention_expires_at=clock_timestamp()+interval '72 hours'
  where id='${notifyClaim.batch_id}'`);
await db.query("set role service_role");
const uncertain = (await db.query(`select public.retry_daily_feedback_notification_v1(
  '33000000-0000-4000-8000-000000000001', '${'7'.repeat(64)}',
  'notifier-1', '${notifyClaim.batch_id}', ${notifyClaim.lease_generation},
  'admission_transport_uncertain', 60
) as result`)).rows[0].result;
if (uncertain.status !== 'delivery_unknown') {
  throw new Error(`request-start uncertainty was not preserved: ${JSON.stringify(uncertain)}`);
}
const notifyReclaim = (await db.query(`select public.claim_daily_feedback_notification_v1(
  '34000000-0000-4000-8000-000000000001', '${'8'.repeat(64)}',
  'notifier-1', 'lancemos', 'psicologajohanna-agent-bot-19',
  clock_timestamp()+interval '61 seconds', 120
) as result`)).rows[0].result;
if (typeof notifyClaim.notification_occurred_at !== 'string' ||
    notifyReclaim.notification_occurred_at !== notifyClaim.notification_occurred_at) {
  throw new Error(`notification occurrence time was not stable: ${JSON.stringify({notifyClaim, notifyReclaim})}`);
}
if (notifyReclaim.batch_id !== notifyClaim.batch_id) {
  throw new Error(`notification reconciliation changed identity: ${JSON.stringify(notifyReclaim)}`);
}
await db.query(`select public.mark_daily_feedback_notification_started_v1(
  '35000000-0000-4000-8000-000000000001', '${'9'.repeat(64)}',
  'notifier-1', '${notifyReclaim.batch_id}', ${notifyReclaim.lease_generation}
)`);
await db.query(`select public.complete_daily_feedback_notification_v1(
  '36000000-0000-4000-8000-000000000001', '${'0'.repeat(64)}',
  'notifier-1', '${notifyReclaim.batch_id}', ${notifyReclaim.lease_generation}
)`);

const stateHash = 'b'.repeat(64);
const sessionHash = 'c'.repeat(64);
await db.query(`select public.begin_daily_feedback_oidc_v1(
  '${stateHash}', '${committed.public_ref}',
  '/daily-feedback/review/${committed.public_ref}', clock_timestamp()+interval '5 minutes'
)`);
let mismatchedSubjectRejected = false;
try {
  await db.query(`select public.complete_daily_feedback_oidc_v1(
    '${stateHash}', '${'e'.repeat(64)}',
    'https://slack.com', 'https://slack.com/user_id/U87654321',
    'T12345678', 'U12345678', clock_timestamp()+interval '4 hours'
  )`);
} catch (error) {
  mismatchedSubjectRejected = String(error.message).includes('reviewer_not_authorized');
}
if (!mismatchedSubjectRejected) throw new Error('mismatched OIDC subject was authorized');
const login = (await db.query(`select public.complete_daily_feedback_oidc_v1(
  '${stateHash}', '${sessionHash}',
  'https://slack.com', 'https://slack.com/user_id/U12345678',
  'T12345678', 'U12345678',
  clock_timestamp()+interval '4 hours'
) as result`)).rows[0].result;
if (login.status !== 'authenticated') {
  throw new Error(`OIDC completion failed: ${JSON.stringify(login)}`);
}
const page = (await db.query(`select public.get_daily_feedback_review_page_v1(
  '${sessionHash}', '${committed.public_ref}'
) as result`)).rows[0].result;
if (page.status !== 'item' || page.item.position !== 1 || page.item.messages.length !== 2) {
  throw new Error(`review page invalid: ${JSON.stringify(page)}`);
}
const literalFeedback = 'Responder primero y conservar esta frase literalmente: ñ.\nSegunda línea\tcon tabulación.';
const decision = (await db.query(`select public.record_daily_feedback_decision_v1(
  '40000000-0000-4000-8000-000000000001', '${'4'.repeat(64)}',
  '${sessionHash}', '${committed.public_ref}', '${page.item.item_id}',
  'correct_with_feedback', $1
) as result`, [literalFeedback])).rows[0].result;
if (decision.status !== 'recorded' || decision.batch_complete !== true) {
  throw new Error(`decision failed: ${JSON.stringify(decision)}`);
}
let incompatibleDecisionReplayRejected = false;
try {
  await db.query(`select public.record_daily_feedback_decision_v1(
    '40000000-0000-4000-8000-000000000001', '${'4'.repeat(64)}',
    '${sessionHash}', '${committed.public_ref}', '${page.item.item_id}',
    'skip', null
  )`);
} catch (error) {
  incompatibleDecisionReplayRejected = String(error.message).includes('idempotency_conflict');
}
if (!incompatibleDecisionReplayRejected) {
  throw new Error('same command and fingerprint accepted a changed decision payload');
}

await db.exec('reset role');
const persisted = (await db.query(`select verbatim_feedback from public.daily_feedback_decisions`)).rows[0];
if (persisted.verbatim_feedback !== literalFeedback) {
  throw new Error('literal feedback changed');
}
await db.exec(`update public.daily_feedback_batches set retention_expires_at=clock_timestamp()-interval '1 second'`);
await db.exec('set role service_role');
let directDmlBlocked = false;
try {
  await db.exec(`delete from public.daily_feedback_items`);
} catch {
  directDmlBlocked = true;
}
if (!directDmlBlocked) {
  throw new Error('service_role direct content DML was not blocked');
}
const purged = (await db.query(`select public.purge_expired_daily_feedback_v1(
  clock_timestamp(), 'juan', 'lancemos', 'psicologajohanna-agent-bot-19', 20
) as result`)).rows[0].result;
if (purged.count !== 1) {
  throw new Error(`purge failed: ${JSON.stringify(purged)}`);
}
await db.exec('reset role');
const counts = (await db.query(`
  select
    (select count(*)::int from public.daily_feedback_items) as items,
    (select count(*)::int from public.daily_feedback_decisions) as decisions,
    (select count(*)::int from public.daily_feedback_sessions) as sessions,
    (select count(*)::int from public.daily_feedback_purge_tombstones) as tombstones
`)).rows[0];
if (counts.items !== 0 || counts.decisions !== 0 || counts.sessions !== 0 || counts.tombstones !== 1) {
  throw new Error(`purge counts invalid: ${JSON.stringify(counts)}`);
}

await db.exec('set role service_role');
await db.query(`
  select public.configure_daily_feedback_scope_v1(
    '10000000-0000-4000-8000-000000000098', '${'9'.repeat(64)}',
    'lancemos', 'psicologajohanna-agent-bot-19', 'replacement-reviewer',
    'https://slack.com', 'https://slack.com/user_id/U87654321',
    'T12345678', 'U87654321', 1, 2, 19, 'UTC', '23:55:00', 72,
    'deterministic-redaction-v1', 'chatwoot-daily-agent-dialogues-v1',
    'daily-feedback-web-v1', true
  )
`);
await db.exec('reset role');
const oldReviewer = (await db.query(`
  select active, binding_generation::int as generation
  from public.daily_feedback_reviewer_bindings
  where tenant_ref='lancemos' and scope_ref='psicologajohanna-agent-bot-19'
    and reviewer_ref='juan'
`)).rows[0];
if (oldReviewer.active !== false || oldReviewer.generation !== initialBindingGeneration + 1) {
  throw new Error(`REPLACED_REVIEWER_REMAINED_AUTHORIZED: ${JSON.stringify(oldReviewer)}`);
}

const commandsBeforeIdlePolls = (await db.query(`
  select count(*)::int as count from public.daily_feedback_workflow_commands
`)).rows[0].count;
await db.exec('set role service_role');
const idleCollection = (await db.query(`select public.claim_daily_feedback_collection_v1(
  '10000000-0000-4000-8000-000000000197', '${'a'.repeat(64)}',
  'worker-a', 'lancemos', 'psicologajohanna-agent-bot-19',
  clock_timestamp(), false, 120
) as result`)).rows[0].result;
const idleNotification = (await db.query(`select public.claim_daily_feedback_notification_v1(
  '10000000-0000-4000-8000-000000000198', '${'b'.repeat(64)}',
  'worker-a', 'lancemos', 'psicologajohanna-agent-bot-19',
  clock_timestamp(), 120
) as result`)).rows[0].result;
await db.exec('reset role');
const commandsAfterIdlePolls = (await db.query(`
  select count(*)::int as count from public.daily_feedback_workflow_commands
`)).rows[0].count;
if (idleCollection.status !== 'idle' || idleNotification.status !== 'idle'
    || commandsAfterIdlePolls !== commandsBeforeIdlePolls) {
  throw new Error(`IDLE_POLLS_PERSISTED_COMMANDS: ${JSON.stringify({
    idleCollection, idleNotification, commandsBeforeIdlePolls, commandsAfterIdlePolls,
  })}`);
}

console.log('DAILY_FEEDBACK_PRODUCTION_SQL_OK');
