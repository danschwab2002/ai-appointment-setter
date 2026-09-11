import { PGlite } from '@electric-sql/pglite';
import { existsSync, readFileSync } from 'node:fs';
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
const fencingMigration = join(
  root,
  'supabase/migrations/20260911000100_daily_feedback_notification_fencing_v1.sql',
);
if (existsSync(fencingMigration)) {
  await db.exec(readFileSync(fencingMigration, 'utf8'));
}
await db.exec(readFileSync(
  join(root, 'supabase/migrations/20260911000200_daily_feedback_multi_reviewer_ownership_v1.sql'),
  'utf8',
));
await db.exec('set role service_role');

const reviewers = JSON.stringify([
  { reviewer_ref: 'juan', slack_user_id: 'U12345678', deletion_accountable: true },
  { reviewer_ref: 'mariana', slack_user_id: 'U87654321', deletion_accountable: true },
  { reviewer_ref: 'dan', slack_user_id: 'U11111111', deletion_accountable: true },
  { reviewer_ref: 'marcela', slack_user_id: 'U22222222', deletion_accountable: true },
]);
const invalidReviewerSets = [
  {
    reviewers: [
      { reviewer_ref: 'juan', slack_user_id: 'U12345678', deletion_accountable: true, extra: true },
      { reviewer_ref: 'mariana', slack_user_id: 'U87654321', deletion_accountable: true },
      { reviewer_ref: 'dan', slack_user_id: 'U11111111', deletion_accountable: true },
      { reviewer_ref: 'marcela', slack_user_id: 'U22222222', deletion_accountable: true },
    ],
    marker: 'unknown_reviewer_key',
  },
  {
    reviewers: [
      { reviewer_ref: 'juan', slack_user_id: 'U12345678', deletion_accountable: true },
      { reviewer_ref: 'juan', slack_user_id: 'U87654321', deletion_accountable: true },
      { reviewer_ref: 'dan', slack_user_id: 'U11111111', deletion_accountable: true },
      { reviewer_ref: 'marcela', slack_user_id: 'U22222222', deletion_accountable: true },
    ],
    marker: 'duplicate_reviewer_ref',
  },
  {
    reviewers: [
      { reviewer_ref: 'juan', slack_user_id: 'U12345678', deletion_accountable: false },
      { reviewer_ref: 'mariana', slack_user_id: 'U87654321', deletion_accountable: true },
      { reviewer_ref: 'dan', slack_user_id: 'U11111111', deletion_accountable: true },
      { reviewer_ref: 'marcela', slack_user_id: 'U22222222', deletion_accountable: true },
    ],
    marker: 'all_reviewers_must_be_deletion_accountable',
  },
  {
    reviewers: [{ reviewer_ref: 'juan', slack_user_id: 'U12345678', deletion_accountable: true }],
    marker: 'reviewer_set_must_have_four',
  },
];
for (const [index, invalid] of invalidReviewerSets.entries()) {
  let rejected = false;
  let observedError = '';
  try {
    await db.query(`select public.configure_daily_feedback_scope_v2(
      '00000000-0000-4000-8000-00000000000${index + 2}', '${String(index + 2).repeat(64)}',
      'lancemos', 'psicologajohanna-agent-bot-19', 'https://slack.com', 'T12345678',
      '${JSON.stringify(invalid.reviewers)}'::jsonb, 'joint-reviewer-accountability-v1',
      1, 9, 1, 'America/Bogota', '18:00:00', 72,
      'san-v1', 'sel-v1', 'render-v1', true
    )`);
  } catch (error) {
    observedError = String(error.message);
    rejected = observedError.includes(invalid.marker);
  }
  if (!rejected) throw new Error(`invalid reviewer set was not rejected: ${invalid.marker}; ${observedError}`);
}
const configured = (await db.query(`
  select public.configure_daily_feedback_scope_v2(
    '10000000-0000-4000-8000-000000000001', '${'1'.repeat(64)}',
    'lancemos', 'psicologajohanna-agent-bot-19', 'https://slack.com',
    'T12345678', $1::jsonb, 'joint-reviewer-accountability-v1',
    1, 2, 19, 'America/Bogota', '18:00:00', 72,
    'deterministic-redaction-v1', 'chatwoot-daily-agent-dialogues-v1',
    'daily-feedback-web-v1', false
  ) as result
`, [reviewers])).rows[0].result;
if (configured.status !== 'configured' || configured.enabled !== false || configured.reviewer_count !== 4) {
  throw new Error(`scope configuration failed: ${JSON.stringify(configured)}`);
}

let legacyConfigurationRejected = false;
try {
  await db.query(`select public.configure_daily_feedback_scope_v1(
    '10000000-0000-4000-8000-000000000002', '${'2'.repeat(64)}',
    'lancemos','psicologajohanna-agent-bot-19','juan','https://slack.com',
    'https://slack.com/user_id/U12345678','T12345678','U12345678',
    1,2,19,'America/Bogota','18:00:00',72,'deterministic-redaction-v1',
    'chatwoot-daily-agent-dialogues-v1','daily-feedback-web-v1',true
  )`);
} catch (error) {
  legacyConfigurationRejected = String(error.message).includes('daily_feedback_config_v1_disabled')
    || String(error.message).includes('permission denied');
}
if (!legacyConfigurationRejected) throw new Error('legacy configuration could collapse reviewer set');

await db.exec('reset role');
const initialBindingGeneration = (await db.query(`
  select binding_generation::int as generation
  from public.daily_feedback_reviewer_bindings
  where tenant_ref='lancemos' and scope_ref='psicologajohanna-agent-bot-19'
    and reviewer_ref='juan'
`)).rows[0].generation;
await db.exec('set role service_role');
await db.query(`
  select public.configure_daily_feedback_scope_v2(
    '10000000-0000-4000-8000-000000000099', '${'8'.repeat(64)}',
    'lancemos', 'psicologajohanna-agent-bot-19', 'https://slack.com',
    'T12345678', $1::jsonb, 'joint-reviewer-accountability-v1',
    1, 2, 19, 'America/Bogota', '18:00:00', 72,
    'deterministic-redaction-v1', 'chatwoot-daily-agent-dialogues-v1',
    'daily-feedback-web-v1', false
  )
`, [reviewers]);
await db.exec('reset role');
const unchangedBindingGeneration = (await db.query(`
  select binding_generation::int as generation
  from public.daily_feedback_reviewer_bindings
  where tenant_ref='lancemos' and scope_ref='psicologajohanna-agent-bot-19'
    and reviewer_ref='juan'
`)).rows[0].generation;
await db.exec('set role service_role');
if (unchangedBindingGeneration !== initialBindingGeneration) {
  throw new Error('UNCHANGED_CONFIGURATION_INVALIDATED_REVIEWER');
}

const disabledBackgroundClaim = (await db.query(`
  select public.claim_daily_feedback_collection_v1(
    '20000000-0000-4000-8000-000000000000', '${'0'.repeat(64)}',
    'collector-1', 'lancemos', 'psicologajohanna-agent-bot-19', (
      ((clock_timestamp() at time zone 'America/Bogota')::date::timestamp + time '18:00:00')
      at time zone 'America/Bogota'
    ), false, 120
  ) as result
`)).rows[0].result;
if (disabledBackgroundClaim.status !== 'idle') {
  throw new Error('disabled scheduler was claimable without manual force');
}

const claimed = (await db.query(`
  with captured as (
    select clock_timestamp() as now_at
  )
  select public.claim_daily_feedback_collection_v1(
    '20000000-0000-4000-8000-000000000001', '${'2'.repeat(64)}',
    'collector-1', 'lancemos', 'psicologajohanna-agent-bot-19', greatest(
      now_at,
      (
        ((now_at at time zone 'America/Bogota')::date::timestamp + time '18:00:00')
        at time zone 'America/Bogota'
      ) + interval '1 second'
    ), true, 120
  ) as result
  from captured
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
if (committed.status !== 'committed' || committed.item_count !== 1 || committed.reviewer_count !== 4) {
  throw new Error(`batch commit failed: ${JSON.stringify(committed)}`);
}
await db.exec('reset role');
const batchReviewerCount = (await db.query(`
  select count(*)::int as count
  from public.daily_feedback_batch_reviewer_bindings
  where batch_id='${committed.batch_id}'
`)).rows[0].count;
if (batchReviewerCount !== 4) throw new Error('batch did not snapshot all four reviewers');
await db.exec('set role service_role');
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
const prematurePurge = (await db.query(`select public.purge_expired_daily_feedback_v2(
  clock_timestamp()+interval '10 days', 'daily-feedback-worker-1',
  'lancemos', 'psicologajohanna-agent-bot-19', 20
) as result`)).rows[0].result;
if (prematurePurge.count !== 0) {
  throw new Error(`caller-supplied purge clock bypassed retention: ${JSON.stringify(prematurePurge)}`);
}
let nullPurgeLimitRejected = false;
try {
  await db.query(`select public.purge_expired_daily_feedback_v2(
    clock_timestamp(), 'daily-feedback-worker-1',
    'lancemos', 'psicologajohanna-agent-bot-19', null
  )`);
} catch (error) {
  nullPurgeLimitRejected = String(error.message).includes('invalid_purge_request');
}
if (!nullPurgeLimitRejected) throw new Error('null purge limit was not rejected');
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
for (const [commandId, errorCode, retrySeconds] of [
  ['31100000-0000-4000-8000-000000000001', 'UPPERCASE', 60],
  ['31200000-0000-4000-8000-000000000001', 'transport_error', 0],
  ['31300000-0000-4000-8000-000000000001', 'transport_error', null],
  ['31400000-0000-4000-8000-000000000001', 'transport_error', 901],
]) {
  let invalidRetryRejected = false;
  try {
    await db.query(`select public.retry_daily_feedback_notification_v1(
      '${commandId}', '${'4'.repeat(64)}', 'notifier-1',
      '${notifyClaim.batch_id}', ${notifyClaim.lease_generation},
      '${errorCode}', ${retrySeconds === null ? 'null' : retrySeconds}
    )`);
  } catch (error) {
    invalidRetryRejected = String(error.message).includes('invalid_notification_retry');
  }
  if (!invalidRetryRejected) throw new Error(`invalid notification retry accepted: ${errorCode}/${retrySeconds}`);
}
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
if (typeof notifyClaim.notification_occurred_at !== 'string') {
  throw new Error(`notification occurrence time missing: ${JSON.stringify(notifyClaim)}`);
}
if (notifyReclaim.status !== 'idle') {
  throw new Error(`delivery-unknown notification was claimable again: ${JSON.stringify(notifyReclaim)}`);
}
const unhealthyReadiness = (await db.query(`select public.get_daily_feedback_readiness_v1(
  'lancemos','psicologajohanna-agent-bot-19',clock_timestamp()
) as result`)).rows[0].result;
if (unhealthyReadiness.delivery_unknown_count !== 1) {
  throw new Error(`delivery-unknown readiness was not fail-closed: ${JSON.stringify(unhealthyReadiness)}`);
}

const reviewersWithFutureAddition = JSON.stringify([
  { reviewer_ref: 'juan', slack_user_id: 'U12345678', deletion_accountable: true },
  { reviewer_ref: 'mariana', slack_user_id: 'U87654321', deletion_accountable: true },
  { reviewer_ref: 'dan', slack_user_id: 'U11111111', deletion_accountable: true },
  { reviewer_ref: 'future-reviewer', slack_user_id: 'U77777777', deletion_accountable: true },
]);
await db.query(`select public.configure_daily_feedback_scope_v2(
  '36000000-0000-4000-8000-000000000002', '${'a'.repeat(64)}',
  'lancemos','psicologajohanna-agent-bot-19','https://slack.com','T12345678',
  $1::jsonb,'joint-reviewer-accountability-v1',1,2,19,'America/Bogota','18:00:00',72,
  'deterministic-redaction-v1','chatwoot-daily-agent-dialogues-v1','daily-feedback-web-v1',false
)`, [reviewersWithFutureAddition]);

const mutableIdentityStateHash = 'a'.repeat(64);
await db.query("set role postgres");
await db.query(`update public.daily_feedback_reviewer_bindings
  set oidc_subject='https://slack.com/user_id/U99999999',slack_user_id='U99999999'
  where tenant_ref='lancemos' and scope_ref='psicologajohanna-agent-bot-19'
    and reviewer_ref='juan'`);
await db.query("set role service_role");
await db.query(`select public.begin_daily_feedback_oidc_v1(
  '${mutableIdentityStateHash}', '${committed.public_ref}',
  '/daily-feedback/review/${committed.public_ref}', clock_timestamp()+interval '5 minutes'
)`);
let mutableIdentityBypassRejected = false;
try {
  await db.query(`select public.complete_daily_feedback_oidc_v1(
    '${mutableIdentityStateHash}', '${'9'.repeat(64)}',
    'https://slack.com', 'https://slack.com/user_id/U99999999',
    'T12345678', 'U99999999', clock_timestamp()+interval '30 minutes'
  )`);
} catch (error) {
  mutableIdentityBypassRejected = String(error.message).includes('reviewer_not_authorized');
}
if (!mutableIdentityBypassRejected) {
  throw new Error('mutable live identity bypassed immutable batch authority');
}
await db.query("set role postgres");
await db.query(`update public.daily_feedback_reviewer_bindings
  set oidc_subject='https://slack.com/user_id/U12345678',slack_user_id='U12345678'
  where tenant_ref='lancemos' and scope_ref='psicologajohanna-agent-bot-19'
    and reviewer_ref='juan'`);
await db.query("set role service_role");

const stateHash = 'b'.repeat(64);
const sessionHash = 'c'.repeat(64);
await db.query("set role postgres");
await db.query(`update public.daily_feedback_batches
  set retention_expires_at=clock_timestamp()+interval '1 hour'
  where id='${committed.batch_id}'`);
await db.query("set role service_role");
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
let futureReviewerRetroactiveAccessRejected = false;
try {
  await db.query(`select public.complete_daily_feedback_oidc_v1(
    '${stateHash}', '${'f'.repeat(64)}',
    'https://slack.com', 'https://slack.com/user_id/U77777777',
    'T12345678', 'U77777777', clock_timestamp()+interval '30 minutes'
  )`);
} catch (error) {
  futureReviewerRetroactiveAccessRejected = String(error.message).includes('reviewer_not_authorized');
}
if (!futureReviewerRetroactiveAccessRejected) {
  throw new Error('new reviewer received retroactive batch access');
}
let sessionBeyondRetentionRejected = false;
try {
  await db.query(`select public.complete_daily_feedback_oidc_v1(
    '${stateHash}', '${sessionHash}',
    'https://slack.com', 'https://slack.com/user_id/U12345678',
    'T12345678', 'U12345678',
    clock_timestamp()+interval '4 hours'
  )`);
} catch (error) {
  sessionBeyondRetentionRejected = String(error.message).includes('invalid_session_expiry');
}
if (!sessionBeyondRetentionRejected) {
  throw new Error('OIDC session expiry exceeded batch retention');
}
const login = (await db.query(`select public.complete_daily_feedback_oidc_v1(
  '${stateHash}', '${sessionHash}',
  'https://slack.com', 'https://slack.com/user_id/U12345678',
  'T12345678', 'U12345678',
  clock_timestamp()+interval '30 minutes'
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
const marianaStateHash = 'd'.repeat(64);
const marianaSessionHash = 'e'.repeat(64);
await db.query(`select public.begin_daily_feedback_oidc_v1(
  '${marianaStateHash}', '${committed.public_ref}',
  '/daily-feedback/review/${committed.public_ref}', clock_timestamp()+interval '5 minutes'
)`);
const marianaLogin = (await db.query(`select public.complete_daily_feedback_oidc_v1(
  '${marianaStateHash}', '${marianaSessionHash}',
  'https://slack.com', 'https://slack.com/user_id/U87654321',
  'T12345678', 'U87654321', clock_timestamp()+interval '30 minutes'
) as result`)).rows[0].result;
const marianaPage = (await db.query(`select public.get_daily_feedback_review_page_v1(
  '${marianaSessionHash}', '${committed.public_ref}'
) as result`)).rows[0].result;
if (marianaLogin.status !== 'authenticated' || marianaPage.item.item_id !== page.item.item_id) {
  throw new Error('second snapshotted reviewer could not access collaborative batch');
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

const reviewersWithoutMariana = JSON.stringify([
  { reviewer_ref: 'juan', slack_user_id: 'U12345678', deletion_accountable: true },
  { reviewer_ref: 'dan', slack_user_id: 'U11111111', deletion_accountable: true },
  { reviewer_ref: 'future-reviewer', slack_user_id: 'U77777777', deletion_accountable: true },
  { reviewer_ref: 'replacement-two', slack_user_id: 'U66666666', deletion_accountable: true },
]);
await db.query(`select public.configure_daily_feedback_scope_v2(
  '41000000-0000-4000-8000-000000000001', '${'5'.repeat(64)}',
  'lancemos','psicologajohanna-agent-bot-19','https://slack.com','T12345678',
  $1::jsonb,'joint-reviewer-accountability-v1',1,2,19,'America/Bogota','18:00:00',72,
  'deterministic-redaction-v1','chatwoot-daily-agent-dialogues-v1','daily-feedback-web-v1',false
)`, [reviewersWithoutMariana]);
const juanStillAuthorized = (await db.query(`select public.get_daily_feedback_review_page_v1(
  '${sessionHash}', '${committed.public_ref}'
) as result`)).rows[0].result;
if (juanStillAuthorized.status !== 'complete') {
  throw new Error('revoking one reviewer invalidated another reviewer');
}
let marianaRevoked = false;
try {
  await db.query(`select public.get_daily_feedback_review_page_v1(
    '${marianaSessionHash}', '${committed.public_ref}'
  )`);
} catch (error) {
  marianaRevoked = String(error.message).includes('review_session_invalid')
    || String(error.message).includes('reviewer_not_authorized');
}
if (!marianaRevoked) throw new Error('removed reviewer session remained authorized');

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
const purged = (await db.query(`select public.purge_expired_daily_feedback_v2(
  clock_timestamp(), 'daily-feedback-worker-1', 'lancemos', 'psicologajohanna-agent-bot-19', 20
) as result`)).rows[0].result;
if (purged.count !== 1) {
  throw new Error(`purge failed: ${JSON.stringify(purged)}`);
}
const healthyReadiness = (await db.query(`select public.get_daily_feedback_readiness_v1(
  'lancemos','psicologajohanna-agent-bot-19',clock_timestamp()
) as result`)).rows[0].result;
if (healthyReadiness.delivery_unknown_count !== 0) {
  throw new Error(`purge did not restore readiness: ${JSON.stringify(healthyReadiness)}`);
}
await db.exec('reset role');
const counts = (await db.query(`
  select
    (select count(*)::int from public.daily_feedback_items) as items,
    (select count(*)::int from public.daily_feedback_decisions) as decisions,
    (select count(*)::int from public.daily_feedback_sessions) as sessions,
    (select count(*)::int from public.daily_feedback_batch_reviewer_bindings) as batch_reviewers,
    (select count(*)::int from public.daily_feedback_purge_tombstones) as tombstones
`)).rows[0];
if (counts.items !== 0 || counts.decisions !== 0 || counts.sessions !== 0
    || counts.batch_reviewers !== 0 || counts.tombstones !== 1) {
  throw new Error(`purge counts invalid: ${JSON.stringify(counts)}`);
}
const tombstone = (await db.query(`
  select accountable_reviewer_refs,accountable_reviewers,purge_actor_ref,deletion_owner
  from public.daily_feedback_purge_tombstones
`)).rows[0];
if (JSON.stringify(tombstone.accountable_reviewer_refs) !== JSON.stringify(['dan','juan','marcela','mariana'])
    || tombstone.accountable_reviewers.length !== 4
    || tombstone.accountable_reviewers.some((reviewer) => !reviewer.reviewer_binding_generation || !reviewer.slack_user_id)
    || tombstone.purge_actor_ref !== 'daily-feedback-worker-1'
    || tombstone.deletion_owner !== 'joint-reviewer-accountability-v1') {
  throw new Error(`purge accountability invalid: ${JSON.stringify(tombstone)}`);
}
await db.exec('set role postgres');
let tombstoneMutationRejected = false;
try {
  await db.query(`update public.daily_feedback_purge_tombstones
    set purge_actor_ref='tampered-owner-write'`);
} catch (error) {
  tombstoneMutationRejected = String(error.message).includes('daily_feedback_tombstone_immutable');
}
if (!tombstoneMutationRejected) throw new Error('owner-level tombstone mutation was not rejected');
await db.exec('set role service_role');

const replacementReviewers = JSON.stringify([
  { reviewer_ref: 'replacement-reviewer', slack_user_id: 'U99999999', deletion_accountable: true },
  { reviewer_ref: 'replacement-three', slack_user_id: 'U33333333', deletion_accountable: true },
  { reviewer_ref: 'replacement-four', slack_user_id: 'U44444444', deletion_accountable: true },
  { reviewer_ref: 'replacement-five', slack_user_id: 'U55555555', deletion_accountable: true },
]);
await db.query(`
  select public.configure_daily_feedback_scope_v2(
    '10000000-0000-4000-8000-000000000098', '${'9'.repeat(64)}',
    'lancemos', 'psicologajohanna-agent-bot-19', 'https://slack.com',
    'T12345678', $1::jsonb, 'joint-reviewer-accountability-v1',
    1, 2, 19, 'America/Bogota', '18:00:00', 72,
    'deterministic-redaction-v1', 'chatwoot-daily-agent-dialogues-v1',
    'daily-feedback-web-v1', false
  )
`, [replacementReviewers]);
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

await db.exec('reset role');
await db.exec(`
  insert into public.daily_feedback_batches(
    schedule_id, tenant_ref, scope_ref, local_date, window_start, window_end,
    sanitizer_version, selection_version, renderer_version, release_lineage,
    package_fingerprint, item_count, reviewer_binding_id,
    reviewer_binding_generation, notification_next_attempt_at, retention_expires_at,
    reviewer_set_hash,deletion_policy_ref
  )
  select s.id, s.tenant_ref, s.scope_ref, current_date + 100,
    clock_timestamp() - interval '2 hours', clock_timestamp() - interval '1 hour',
    s.sanitizer_version, s.selection_version, s.renderer_version,
    'release_lineage_unavailable', '${'c'.repeat(64)}', 0,
    s.reviewer_binding_id, s.reviewer_binding_generation,
    clock_timestamp() - interval '1 minute', clock_timestamp() + interval '72 hours',
    s.reviewer_set_hash,s.deletion_policy_ref
  from public.daily_feedback_schedules s
  where s.tenant_ref='lancemos' and s.scope_ref='psicologajohanna-agent-bot-19'
`);
await db.exec(`
  insert into public.daily_feedback_batch_reviewer_bindings(
    batch_id,reviewer_binding_id,reviewer_ref,reviewer_binding_generation,
    oidc_issuer,oidc_subject,slack_team_id,slack_user_id,deletion_accountable
  )
  select b.id,rb.id,rb.reviewer_ref,rb.binding_generation,
    rb.oidc_issuer,rb.oidc_subject,rb.slack_team_id,rb.slack_user_id,rb.deletion_accountable
  from public.daily_feedback_batches b
  join public.daily_feedback_reviewer_bindings rb
    on rb.tenant_ref=b.tenant_ref and rb.scope_ref=b.scope_ref and rb.active
  where b.package_fingerprint='${'c'.repeat(64)}'
`);
await db.exec('set role service_role');
const retainedClaim = (await db.query(`select public.claim_daily_feedback_notification_v1(
  '10000000-0000-4000-8000-000000000199', '${'d'.repeat(64)}',
  'worker-retention', 'lancemos', 'psicologajohanna-agent-bot-19',
  clock_timestamp(), 120
) as result`)).rows[0].result;
if (retainedClaim.status !== 'claimed' || typeof retainedClaim.retention_expires_at !== 'string') {
  throw new Error(`NOTIFICATION_CLAIM_OMITTED_RETENTION: ${JSON.stringify(retainedClaim)}`);
}
await db.exec('reset role');
await db.query(`
  update public.daily_feedback_batches
  set notification_lease_expires_at=clock_timestamp()-interval '1 second'
  where id='${retainedClaim.batch_id}'
`);
await db.exec('set role service_role');
let expiredLeaseRetryRejected = false;
try {
  await db.query(`select public.retry_daily_feedback_notification_v1(
    '10000000-0000-4000-8000-000000000200', '${'e'.repeat(64)}',
    'worker-retention', '${retainedClaim.batch_id}', ${retainedClaim.lease_generation},
    'connector_unavailable', 60
  )`);
} catch (error) {
  expiredLeaseRetryRejected = String(error.message).includes('stale_notification_lease');
}
if (!expiredLeaseRetryRejected) {
  throw new Error('EXPIRED_NOTIFICATION_LEASE_RETRY_ACCEPTED');
}

console.log('DAILY_FEEDBACK_PRODUCTION_SQL_OK');
