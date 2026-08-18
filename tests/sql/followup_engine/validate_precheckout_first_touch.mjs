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
const files = [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(join(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => join(root, 'supabase/migrations', name)),
];
for (const file of files) {
  await db.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}

const phoneA = ['120', '2555', '0123'].join('');
const phoneB = ['120', '2555', '0124'].join('');
async function fixture({ suffix, phone, blocked = false }) {
  const contact = `10000000-0000-4000-8000-0000000000${suffix}`;
  const identity = `20000000-0000-4000-8000-0000000000${suffix}`;
  const conversation = `30000000-0000-4000-8000-0000000000${suffix}`;
  const submission = `40000000-0000-4000-8000-0000000000${suffix}`;
  const intent = `50000000-0000-4000-8000-0000000000${suffix}`;
  const externalConversation = String(300 + Number(suffix));
  const canonical = {
    lead: { full_name: 'Lead de Prueba' },
  };
  await db.query(`insert into public.contacts (id, contact_permission)
    values ($1::uuid, $2)`, [contact, blocked ? 'opted_out' : 'unknown']);
  await db.query(`insert into public.channel_identities (
      id, contact_id, channel, account_id, external_user_id,
      external_conversation_id, identity_status, metadata
    ) values (
      $1::uuid, $2::uuid, 'whatsapp', 'chatwoot:1', $3, $4, 'active',
      jsonb_build_object('inbox_id', '2')
    )`,
  [identity, contact, phone, externalConversation]);
  await db.query(`insert into public.conversations (
      id, contact_id, channel_identity_id, status, automation_status,
      human_takeover, commercial_context
    ) values (
      $1::uuid, $2::uuid, $3::uuid, 'active', 'draft_only', false,
      jsonb_build_object('chatwoot_conversation_id', $4::text)
    )`, [conversation, contact, identity, externalConversation]);
  await db.query(`insert into public.precheckout_submissions (
      id, external_submission_id, contract_version, raw_payload,
      canonical_payload, provisional, provider_observed, activation_authorized
    ) values ($1::uuid, $2, '1.0.0-emulated', '{}'::jsonb, $3::jsonb, true, false, false)`,
  [submission, `submission-${suffix}`, JSON.stringify(canonical)]);
  await db.query(`insert into public.purchase_intents (
      id, tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
      normalized_phone, submitted_at, lifecycle_state,
      whatsapp_contact_authorized, provisional, provider_observed, activation_authorized
    ) values (
      $1::uuid, 'joana', 'libre-de-ansiedad', 'bcl-main',
      'F106691755G', 'bxjge6zq', $2, now(), 'waiting_for_purchase',
      false, true, false, false
    )`, [intent, phone]);
  await db.query(`insert into public.purchase_intent_submissions (
      purchase_intent_id, submission_id, ordinal
    ) values ($1::uuid, $2::uuid, 1)`, [intent, submission]);
  return { intent, phone, externalConversation };
}

async function intentOnlyFixture({ suffix, phone }) {
  const submission = `60000000-0000-4000-8000-0000000000${suffix}`;
  const intent = `70000000-0000-4000-8000-0000000000${suffix}`;
  const canonical = { lead: { full_name: 'Lead de Prueba' } };
  await db.query(`insert into public.precheckout_submissions (
      id, external_submission_id, contract_version, raw_payload,
      canonical_payload, provisional, provider_observed, activation_authorized
    ) values ($1::uuid, $2, '1.0.0-emulated', '{}'::jsonb, $3::jsonb, true, false, false)`,
  [submission, `submission-${suffix}`, JSON.stringify(canonical)]);
  await db.query(`insert into public.purchase_intents (
      id, tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
      normalized_phone, submitted_at, lifecycle_state,
      whatsapp_contact_authorized, provisional, provider_observed, activation_authorized
    ) values (
      $1::uuid, 'joana', 'libre-de-ansiedad', 'bcl-main',
      'F106691755G', 'bxjge6zq', $2, now(), 'waiting_for_purchase',
      false, true, false, false
    )`, [intent, phone]);
  await db.query(`insert into public.purchase_intent_submissions (
      purchase_intent_id, submission_id, ordinal
    ) values ($1::uuid, $2::uuid, 1)`, [intent, submission]);
  return { intent, phone };
}

const blocked = await fixture({ suffix: '02', phone: phoneB, blocked: true });
let optOutBlocked = false;
try {
  await db.query(`select * from public.begin_precheckout_test_first_touch($1, $2::uuid, $3, 1, 2)`,
    ['controlled-first-touch-blocked', blocked.intent, blocked.phone]);
} catch (error) {
  optOutBlocked = String(error).includes('precheckout_first_touch_identity_not_unique');
}
if (!optOutBlocked) throw new Error('opted-out target was not blocked');

const eligible = await fixture({ suffix: '01', phone: phoneA });
const first = await db.query(`
  select * from public.begin_precheckout_test_first_touch($1, $2::uuid, $3, 1, 2)
`, ['controlled-first-touch-001', eligible.intent, eligible.phone]);
if (first.rows[0]?.outcome !== 'started'
    || first.rows[0]?.command_status !== 'request_started'
    || first.rows[0]?.chatwoot_conversation_id !== Number(eligible.externalConversation)) {
  throw new Error(`first-touch start mismatch: ${JSON.stringify(first.rows)}`);
}
const commandId = first.rows[0].command_id;
const replay = await db.query(`
  select * from public.begin_precheckout_test_first_touch($1, $2::uuid, $3, 1, 2)
`, ['controlled-first-touch-001', eligible.intent, eligible.phone]);
if (replay.rows[0]?.outcome !== 'replay' || replay.rows[0]?.command_id !== commandId) {
  throw new Error('first-touch replay mismatch');
}
let secondKeyBlocked = false;
try {
  await db.query(`select * from public.begin_precheckout_test_first_touch($1, $2::uuid, $3, 1, 2)`,
    ['controlled-first-touch-002', eligible.intent, eligible.phone]);
} catch (error) {
  secondKeyBlocked = String(error).includes('precheckout_first_touch_already_exists');
}
if (!secondKeyBlocked) throw new Error('second command key was not blocked');

await db.exec('begin');
const failed = await db.query(`
  select * from public.finish_precheckout_test_first_touch(
    $1::uuid, 'failed', null, null, 'target_not_allowed'
  )
`, [commandId]);
if (failed.rows[0]?.command_status !== 'failed') {
  throw new Error('failed was not preserved exactly');
}
await db.exec('rollback');

await db.exec('begin');
const unknown = await db.query(`
  select * from public.finish_precheckout_test_first_touch(
    $1::uuid, 'delivery_unknown', null, null, 'chatwoot_http_error'
  )
`, [commandId]);
if (unknown.rows[0]?.command_status !== 'delivery_unknown') {
  throw new Error('delivery_unknown was not preserved exactly');
}
await db.exec('rollback');

const finished = await db.query(`
  select * from public.finish_precheckout_test_first_touch(
    $1::uuid, 'accepted_by_chatwoot', $2::bigint, 654::bigint, null
  )
`, [commandId, Number(eligible.externalConversation)]);
if (finished.rows[0]?.command_status !== 'accepted_by_chatwoot') {
  throw new Error('first-touch finish mismatch');
}
const finishedReplay = await db.query(`
  select * from public.finish_precheckout_test_first_touch(
    $1::uuid, 'accepted_by_chatwoot', $2::bigint, 654::bigint, null
  )
`, [commandId, Number(eligible.externalConversation)]);
if (finishedReplay.rows[0]?.command_status !== 'accepted_by_chatwoot') {
  throw new Error('first-touch finish replay mismatch');
}
const commandRows = await db.query(`
  select count(*)::integer count,
         bool_and(test_only and not generalizable) assurance,
         bool_and(max_messages = 1 and followups_allowed = 0) budget
  from public.precheckout_test_first_touch_commands
`);
if (commandRows.rows[0]?.count !== 1
    || commandRows.rows[0]?.assurance !== true
    || commandRows.rows[0]?.budget !== true) {
  throw new Error('first-touch durable budget mismatch');
}

await db.query(`update public.purchase_intents set lifecycle_state = 'purchased'
  where id = $1::uuid`, [eligible.intent]);
const successor = await intentOnlyFixture({ suffix: '03', phone: phoneA });
let successorBlocked = false;
try {
  await db.query(`select * from public.begin_precheckout_test_first_touch($1, $2::uuid, $3, 1, 2)`,
    ['controlled-first-touch-successor', successor.intent, successor.phone]);
} catch (error) {
  successorBlocked = String(error).includes('precheckout_first_touch_rollout_consumed');
}
if (!successorBlocked) throw new Error('sequential successor consumed a second rollout send');

console.log('precheckout_first_touch_start_replay_finish=OK');
console.log('precheckout_first_touch_budget_optout_and_ambiguity=OK');
