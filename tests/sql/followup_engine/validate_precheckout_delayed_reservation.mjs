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
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
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

await db.exec(`
  update public.hotmart_abandonment_timer_policy_bindings
  set precheckout_first_touch_enabled = true,
      generation = generation + 1
  where tenant_ref = 'lancemos'
    and funnel_ref = 'psicologajohanna'
    and lower(product_ref) = lower('F106691755G')
    and offer_ref = 'bxjge6zq';
`);

const dueAt = '2026-08-29T16:00:00Z';
let fixtureOrdinal = 0;
async function fixture({ phone, email, classification = null, lifecycle = 'waiting_for_purchase' }) {
  fixtureOrdinal += 1;
  const suffix = String(fixtureOrdinal).padStart(3, '0');
  const submittedAt = '2026-08-29T15:00:00Z';
  const canonical = {
    event_type: 'PRECHECKOUT_FORM_SUBMITTED', contract_version: '1.1.0',
    external_submission_id: `reservation-submission-${suffix}`,
    submitted_at: submittedAt,
    source: {
      tenant_ref: 'lancemos', funnel_ref: 'psicologajohanna', landing_ref: 'ads-a',
      page_url: 'https://example.test/precheckout', aliado: 'Psicologa Johanna',
    },
    identity: { email, phone, phone_valid: true, phone_country_iso: 'US' },
    lead: { full_name: `Lead ${suffix}` },
    commerce: {
      product_ref: 'F106691755G', product_name: 'Libre de Ansiedad',
      price: '49', currency: 'USD', offer_ref: 'bxjge6zq',
      checkout_url: 'https://pay.hotmart.com/F106691755G?off=bxjge6zq',
    },
    consent: {
      terms_accepted: false, privacy_accepted: false,
      marketing_optin: true, whatsapp_contact: true,
      copy_version: 'johanna-precheckout-whatsapp-disclosure-v1',
    },
    dedupe_key: `psicologajohanna:bxjge6zq:${email}`,
    assurance: {
      provisional: false, provider_observed: true, activation_authorized: true,
    },
  };
  const intent = await db.query(`
    insert into public.purchase_intents (
      tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
      normalized_email, normalized_phone, submitted_at, lifecycle_state,
      current_classification, whatsapp_contact_authorized, provisional,
      provider_observed, activation_authorized
    ) values (
      'lancemos', 'psicologajohanna', 'ads-a', 'F106691755G', 'bxjge6zq',
      $1, $2, $3::timestamptz, $4, $5, true, false, true, true
    ) returning id
  `, [email, phone, submittedAt, lifecycle, classification]);
  const submission = await db.query(`
    insert into public.precheckout_submissions (
      external_submission_id, contract_version, raw_payload, canonical_payload,
      provisional, provider_observed, activation_authorized
    ) values ($1, '1.1.0', '{}'::jsonb, $2::jsonb, false, true, true)
    returning id
  `, [`reservation-submission-${suffix}`, JSON.stringify(canonical)]);
  await db.query(`
    insert into public.purchase_intent_submissions (
      purchase_intent_id, submission_id, ordinal
    ) values ($1::uuid, $2::uuid, 1)
  `, [intent.rows[0].id, submission.rows[0].id]);
  const timer = await db.query(`
    insert into public.hotmart_abandonment_reevaluations (
      purchase_intent_id, source_kind, source_submission_id,
      source_webhook_event_id, source_scope_id,
      policy_binding_id, policy_binding_generation, policy_key, policy_version,
      delay_seconds_snapshot, observed_at, due_at, idempotency_key
    )
    select
      $1::uuid, 'precheckout_intent', $2::uuid, null, null,
      binding.id, binding.generation, binding.policy_key, binding.policy_version,
      event.delay_seconds, $3::timestamptz, $3::timestamptz + interval '60 minutes',
      'precheckout-first-touch:' || $2::text
    from public.hotmart_abandonment_timer_policy_bindings binding
    join public.hotmart_abandonment_timer_policy_binding_events event
      on event.binding_id = binding.id and event.generation = binding.generation
    where binding.tenant_ref = 'lancemos'
      and binding.funnel_ref = 'psicologajohanna'
      and binding.precheckout_first_touch_enabled
    returning id
  `, [intent.rows[0].id, submission.rows[0].id, submittedAt]);
  return {
    intentId: intent.rows[0].id,
    submissionId: submission.rows[0].id,
    timerId: timer.rows[0].id,
    phone,
  };
}

async function reevaluate(item) {
  return db.query(`
    select * from public.reevaluate_hotmart_abandonment_timer(
      $1::uuid, $2::timestamptz
    )
  `, [item.timerId, dueAt]);
}

const first = await fixture({
  phone: '12025551001', email: 'reservation-one@example.test',
});
const reserved = await reevaluate(first);
const replay = await reevaluate(first);
if (reserved.rows[0]?.reevaluation_outcome !== 'command_reserved'
    || reserved.rows[0]?.replayed !== false
    || replay.rows[0]?.reevaluation_outcome !== 'command_reserved'
    || replay.rows[0]?.replayed !== true) {
  throw new Error(`reservation replay diverged: ${JSON.stringify({
    reserved: reserved.rows, replay: replay.rows,
  })}`);
}
const command = await db.query(`
  select rollout_scope, source_reevaluation_id, hotmart_webhook_event_id,
         payment_failure_case_id, target_phone, template_name,
         template_language, template_category, copy_version,
         max_messages, followups_allowed, status
  from public.johanna_abandonment_one_shot_commands
  where source_reevaluation_id = $1::uuid
`, [first.timerId]);
const commandRow = command.rows[0];
if (command.rows.length !== 1
    || commandRow.rollout_scope !== 'johanna-precheckout-delayed-first-touch-v1'
    || commandRow.source_reevaluation_id !== first.timerId
    || commandRow.hotmart_webhook_event_id !== null
    || commandRow.payment_failure_case_id !== null
    || commandRow.target_phone !== first.phone
    || commandRow.template_name !== 'johanna_interes_precheckout_01'
    || commandRow.template_language !== 'es_EC'
    || commandRow.template_category !== 'MARKETING'
    || commandRow.copy_version !== 'johanna-precheckout-delayed-first-touch-v1'
    || commandRow.max_messages !== 1
    || commandRow.followups_allowed !== 0
    || commandRow.status !== 'reserved') {
  throw new Error(`reserved command diverged: ${JSON.stringify(command.rows)}`);
}
console.log('PRECHECKOUT_DELAYED_RESERVATION_COMMAND_REPLAY_OK');

await db.query(`
  update public.purchase_intents set lifecycle_state = 'purchased'
  where id = $1::uuid
`, [first.intentId]);
const budget = await fixture({
  phone: first.phone, email: 'reservation-budget@example.test',
});
const budgetResult = await reevaluate(budget);
if (budgetResult.rows[0]?.reevaluation_outcome !== 'budget_consumed') {
  throw new Error(`shared budget failed: ${JSON.stringify(budgetResult.rows)}`);
}
const phoneCommands = await db.query(`
  select count(*)::integer as count
  from public.johanna_abandonment_one_shot_commands where target_phone = $1
`, [first.phone]);
if (phoneCommands.rows[0].count !== 1) throw new Error('shared phone budget duplicated');
console.log('PRECHECKOUT_DELAYED_RESERVATION_SHARED_BUDGET_OK');

const purchased = await fixture({
  phone: '12025551003', email: 'reservation-purchased@example.test',
  lifecycle: 'purchased',
});
const purchasedResult = await reevaluate(purchased);
if (purchasedResult.rows[0]?.reevaluation_outcome !== 'cancelled_purchased') {
  throw new Error(`purchase did not cancel: ${JSON.stringify(purchasedResult.rows)}`);
}

const provider = await fixture({
  phone: '12025551004', email: 'reservation-provider@example.test',
  classification: 'confirmed_abandonment',
});
const providerResult = await reevaluate(provider);
if (providerResult.rows[0]?.reevaluation_outcome !== 'superseded_by_provider_event') {
  throw new Error(`provider did not supersede: ${JSON.stringify(providerResult.rows)}`);
}
console.log('PRECHECKOUT_DELAYED_RESERVATION_PURCHASE_PROVIDER_OK');

const stopped = await fixture({
  phone: '12025551005', email: 'reservation-stopped@example.test',
});
await db.query(`
  insert into public.contact_opt_out_events (
    channel, purpose, source, canonical_account_id, canonical_inbox_id,
    canonical_conversation_id, canonical_message_id, external_user_id,
    occurred_at, normalized_rule_key, correlation_status
  ) values (
    'whatsapp', 'cart_recovery', 'chatwoot', 1, 9, 9505, 9605, $1,
    '2026-08-29T15:30:00Z', 'no_more_messages', 'unmatched'
  )
`, [stopped.phone]);
const stoppedResult = await reevaluate(stopped);
if (stoppedResult.rows[0]?.reevaluation_outcome !== 'blocked_contact') {
  throw new Error(`opt-out did not block: ${JSON.stringify(stoppedResult.rows)}`);
}

const handoff = await fixture({
  phone: '12025551006', email: 'reservation-handoff@example.test',
});
const contactId = '81000000-0000-4000-8000-000000000006';
const identityId = '82000000-0000-4000-8000-000000000006';
await db.query(`
  insert into public.contacts (id, contact_permission, lifecycle_status)
  values ($1::uuid, 'unknown', 'lead')
`, [contactId]);
await db.query(`
  insert into public.channel_identities (
    id, contact_id, channel, account_id, external_user_id,
    external_conversation_id, identity_status, metadata
  ) values (
    $1::uuid, $2::uuid, 'whatsapp', 'chatwoot:1', $3, '9706', 'active',
    jsonb_build_object('inbox_id', '9')
  )
`, [identityId, contactId, handoff.phone]);
await db.query(`
  insert into public.conversations (
    contact_id, channel_identity_id, status, automation_status,
    human_takeover, commercial_context
  ) values (
    $1::uuid, $2::uuid, 'paused_human', 'paused', true,
    jsonb_build_object('chatwoot_conversation_id', '9706')
  )
`, [contactId, identityId]);
const handoffResult = await reevaluate(handoff);
if (handoffResult.rows[0]?.reevaluation_outcome !== 'blocked_handoff') {
  throw new Error(`handoff did not block: ${JSON.stringify(handoffResult.rows)}`);
}
const effects = await db.query(`
  select
    (select count(*)::integer from public.johanna_abandonment_one_shot_commands)
      as commands,
    (select count(*)::integer from public.messages) as messages,
    (select count(*)::integer from public.followup_delivery_attempts) as attempts
`);
if (effects.rows[0].commands !== 1
    || effects.rows[0].messages !== 0
    || effects.rows[0].attempts !== 0) {
  throw new Error(`unexpected effects: ${JSON.stringify(effects.rows[0])}`);
}
console.log('PRECHECKOUT_DELAYED_RESERVATION_STOPS_ZERO_EFFECT_OK');
