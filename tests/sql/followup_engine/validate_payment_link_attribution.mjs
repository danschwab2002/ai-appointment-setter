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

const submittedAt = '2026-08-29T15:00:00Z';
const dueAt = '2026-08-29T16:30:00Z';
const phone = '12025551021';
const email = 'precheckout-worker@example.test';
const canonical = {
  event_type: 'PRECHECKOUT_FORM_SUBMITTED', contract_version: '1.1.0',
  external_submission_id: '01K3F8QW7N2VYB4M6X9CDPTZR1',
  submitted_at: submittedAt,
  source: {
    tenant_ref: 'lancemos', funnel_ref: 'psicologajohanna', landing_ref: 'ads-a',
    page_url: 'https://example.test/precheckout', aliado: 'Psicologa Johanna',
  },
  identity: { email, phone, phone_valid: true, phone_country_iso: 'US' },
  lead: { full_name: 'Lead Worker' },
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
    whatsapp_contact_authorized, provisional, provider_observed,
    activation_authorized
  ) values (
    'lancemos', 'psicologajohanna', 'ads-a', 'F106691755G', 'bxjge6zq',
    $1, $2, $3::timestamptz, 'waiting_for_purchase', true, false, true, true
  ) returning id
`, [email, phone, submittedAt]);
const submission = await db.query(`
  insert into public.precheckout_submissions (
    external_submission_id, contract_version, raw_payload, canonical_payload,
    provisional, provider_observed, activation_authorized
  ) values ($1, '1.1.0', '{}'::jsonb, $2::jsonb, false, true, true)
  returning id
`, ['01K3F8QW7N2VYB4M6X9CDPTZR1', JSON.stringify(canonical)]);
await db.query(`
  insert into public.purchase_intent_submissions (
    purchase_intent_id, submission_id, ordinal
  ) values ($1::uuid, $2::uuid, 1)
`, [intent.rows[0].id, submission.rows[0].id]);
const latestCanonical = {
  ...canonical,
  external_submission_id: '01K3F8QW7N2VYB4M6X9CDPTZR2',
  submitted_at: '2026-08-29T15:30:00Z',
  lead: { full_name: 'Lead Worker Updated' },
};
const latestSubmission = await db.query(`
  insert into public.precheckout_submissions (
    external_submission_id, contract_version, raw_payload, canonical_payload,
    provisional, provider_observed, activation_authorized
  ) values ($1, '1.1.0', '{}'::jsonb, $2::jsonb, false, true, true)
  returning id
`, ['01K3F8QW7N2VYB4M6X9CDPTZR2', JSON.stringify(latestCanonical)]);
await db.query(`
  insert into public.purchase_intent_submissions (
    purchase_intent_id, submission_id, ordinal
  ) values ($1::uuid, $2::uuid, 2)
`, [intent.rows[0].id, latestSubmission.rows[0].id]);
const timer = await db.query(`
  insert into public.hotmart_abandonment_reevaluations (
    purchase_intent_id, source_kind, source_submission_id,
    source_webhook_event_id, source_scope_id,
    policy_binding_id, policy_binding_generation, policy_key, policy_version,
    delay_seconds_snapshot, observed_at, due_at, idempotency_key
  )
  select $1::uuid, 'precheckout_intent', $2::uuid, null, null,
         binding.id, binding.generation, binding.policy_key, binding.policy_version,
         event.delay_seconds, $3::timestamptz,
         $3::timestamptz + interval '60 minutes',
         'precheckout-first-touch:' || $2::text
  from public.hotmart_abandonment_timer_policy_bindings binding
  join public.hotmart_abandonment_timer_policy_binding_events event
    on event.binding_id = binding.id and event.generation = binding.generation
  where binding.funnel_ref = 'psicologajohanna'
    and lower(binding.product_ref) = lower('F106691755G')
    and binding.offer_ref = 'bxjge6zq'
  returning id
`, [intent.rows[0].id, latestSubmission.rows[0].id, '2026-08-29T15:30:00Z']);
const timerId = timer.rows[0].id;

const historicalDue = await db.query(`
  select * from public.list_due_hotmart_abandonment_reevaluations(
    $1::timestamptz, 10
  )
`, [dueAt]);
const disabledDue = await db.query(`
  select * from public.list_due_hotmart_abandonment_reevaluations_v2(
    $1::timestamptz, 10, false
  )
`, [dueAt]);
const enabledDue = await db.query(`
  select * from public.list_due_hotmart_abandonment_reevaluations_v2(
    $1::timestamptz, 10, true
  )
`, [dueAt]);
if (historicalDue.rows.length !== 0 || disabledDue.rows.length !== 0
    || enabledDue.rows.length !== 1
    || enabledDue.rows[0].reevaluation_id !== timerId) {
  throw new Error(`due-list gate diverged: ${JSON.stringify({
    historical: historicalDue.rows, disabled: disabledDue.rows,
    enabled: enabledDue.rows,
  })}`);
}

const reserved = await db.query(`
  select * from public.reevaluate_hotmart_abandonment_timer(
    $1::uuid, $2::timestamptz
  )
`, [timerId, dueAt]);
if (reserved.rows[0]?.reevaluation_outcome !== 'command_reserved'
    || reserved.rows[0]?.replayed !== false) {
  throw new Error(`command was not reserved: ${JSON.stringify(reserved.rows)}`);
}
const rawReserved = await db.query(`
  select status from public.johanna_abandonment_one_shot_commands
  where source_reevaluation_id = $1::uuid
`, [timerId]);
if (rawReserved.rows[0]?.status !== 'reserved') {
  throw new Error(`request-start happened before final authority: ${JSON.stringify(rawReserved.rows)}`);
}
const reservedRecoveryDue = await db.query(`
  select * from public.list_due_hotmart_abandonment_reevaluations_v2(
    $1::timestamptz, 10, true
  )
`, [dueAt]);
if (reservedRecoveryDue.rows.length !== 1
    || reservedRecoveryDue.rows[0].reevaluation_id !== timerId) {
  throw new Error(`reserved command was not recoverable: ${JSON.stringify(reservedRecoveryDue.rows)}`);
}

await db.exec('begin');
await db.query(`
  update public.purchase_intents
  set lifecycle_state = 'purchased'
  where id = $1::uuid
`, [intent.rows[0].id]);
const stoppedProjection = await db.query(`
  select * from public.get_precheckout_delayed_one_shot_command($1::uuid)
`, [timerId]);
if (stoppedProjection.rows[0]?.command_status !== 'delivery_unknown'
    || stoppedProjection.rows[0]?.send_authorized !== false
    || stoppedProjection.rows[0]?.authorization_reason !== 'cancelled_purchased'
    || stoppedProjection.rows[0]?.buyer_name !== null
    || stoppedProjection.rows[0]?.buyer_email !== null) {
  throw new Error(`pre-send purchase stop diverged: ${JSON.stringify(stoppedProjection.rows)}`);
}
await db.exec('rollback');

const projected = await db.query(`
  select * from public.get_precheckout_delayed_one_shot_command($1::uuid)
`, [timerId]);
const command = projected.rows[0];
if (projected.rows.length !== 1
    || command.command_status !== 'request_started'
    || command.target_phone !== phone
    || command.buyer_name !== 'Lead Worker Updated'
    || command.buyer_email !== email
    || command.product_name !== 'Libre de Ansiedad'
    || command.template_name !== 'johanna_interes_precheckout_01'
    || command.template_language !== 'es_EC'
    || command.template_category !== 'MARKETING'
    || command.copy_version !== 'johanna-precheckout-delayed-first-touch-v1') {
  throw new Error(`sender projection diverged: ${JSON.stringify(projected.rows)}`);
}
const inflightRecoveryDue = await db.query(`
  select * from public.list_due_hotmart_abandonment_reevaluations_v2(
    $1::timestamptz, 10, true
  )
`, [dueAt]);
if (inflightRecoveryDue.rows.length !== 1
    || inflightRecoveryDue.rows[0].reevaluation_id !== timerId) {
  throw new Error(`request_started command was not recoverable: ${JSON.stringify(inflightRecoveryDue.rows)}`);
}

await db.exec('begin');
const recovered = await db.query(`
  select * from public.get_precheckout_delayed_one_shot_command($1::uuid)
`, [timerId]);
if (recovered.rows[0]?.command_status !== 'delivery_unknown'
    || recovered.rows[0]?.send_authorized !== false
    || recovered.rows[0]?.authorization_reason !== 'precheckout_inflight_recovered'
    || recovered.rows[0]?.buyer_name !== null
    || recovered.rows[0]?.buyer_email !== null) {
  throw new Error(`inflight recovery diverged: ${JSON.stringify(recovered.rows)}`);
}
await db.exec('rollback');

await db.query(`
  select * from public.finish_johanna_abandonment_one_shot(
    $1::uuid, 'accepted_by_chatwoot', 701, 801, null
  )
`, [command.command_id]);
const terminal = await db.query(`
  select * from public.get_precheckout_delayed_one_shot_command($1::uuid)
`, [timerId]);
const replay = await db.query(`
  select * from public.reevaluate_hotmart_abandonment_timer(
    $1::uuid, $2::timestamptz
  )
`, [timerId, dueAt]);
const commandCount = await db.query(`
  select count(*)::integer as count
  from public.johanna_abandonment_one_shot_commands
  where source_reevaluation_id = $1::uuid
`, [timerId]);
if (terminal.rows[0]?.command_status !== 'accepted_by_chatwoot'
    || replay.rows[0]?.replayed !== true
    || commandCount.rows[0].count !== 1
    || (await db.query(`
      select * from public.list_due_hotmart_abandonment_reevaluations_v2(
        $1::timestamptz, 10, true
      )
    `, [dueAt])).rows.length !== 0) {
  throw new Error(`terminal replay diverged: ${JSON.stringify({
    terminal: terminal.rows, replay: replay.rows, count: commandCount.rows,
  })}`);
}


await db.exec(`
  insert into public.inbound_commercial_scope_versions (
    scope_key, version, status, tenant_key, chatwoot_account_id,
    chatwoot_inbox_id, external_product_id, offer_code,
    approved_by, approved_at, published_at
  ) values (
    'payment-link-test', 1, 'published', 'lancemos', 1, 9,
    'F106691755G', 'bxjge6zq', 'operator-test', now(), now()
  )
`);
const inbound = await db.query(`
  select * from public.admit_inbound_commercial_case(
    'payment-link-test', 1, 701, $1
  )
`, [phone]);
const candidate = await db.query(`
  select * from public.get_chatwoot_payment_link_candidate(
    $1::uuid, $2, 1, 9, 701, 604800, $3::timestamptz
  )
`, [inbound.rows[0].commercial_case_id, phone, dueAt]);
if (candidate.rows[0]?.outcome !== 'available'
    || candidate.rows[0]?.source_reevaluation_id !== timerId
    || candidate.rows[0]?.source_submission_id !== latestSubmission.rows[0].id
    || candidate.rows[0]?.sequence_origin_event_ulid !== '01K3F8QW7N2VYB4M6X9CDPTZR2'
    || candidate.rows[0]?.canonical_checkout_url !== canonical.commerce.checkout_url) {
  throw new Error(`payment-link candidate diverged: ${JSON.stringify({
    inbound: inbound.rows, candidate: candidate.rows,
  })}`);
}
const staleCandidate = await db.query(`
  select * from public.get_chatwoot_payment_link_candidate(
    $1::uuid, $2, 1, 9, 701, 604800,
    '2026-09-05T15:30:00.001Z'::timestamptz
  )
`, [inbound.rows[0].commercial_case_id, phone]);
if (staleCandidate.rows[0]?.outcome !== 'checkout_url_stale') {
  throw new Error(`stale checkout remained available: ${JSON.stringify(staleCandidate.rows)}`);
}
await db.exec('begin');
await db.query(`update public.purchase_intents set lifecycle_state = 'purchased'
  where id = $1::uuid`, [intent.rows[0].id]);
const purchasedCandidate = await db.query(`
  select * from public.get_chatwoot_payment_link_candidate(
    $1::uuid, $2, 1, 9, 701, 604800, $3::timestamptz
  )
`, [inbound.rows[0].commercial_case_id, phone, dueAt]);
if (purchasedCandidate.rows[0]?.outcome !== 'purchase_not_open') {
  throw new Error('purchased intent did not stop payment link');
}
await db.exec('rollback');
await db.exec('begin');
await db.query(`
  insert into public.contact_opt_out_events (
    channel, purpose, source, canonical_account_id, canonical_inbox_id,
    canonical_conversation_id, canonical_message_id, external_user_id,
    occurred_at, normalized_rule_key, correlation_status
  ) values (
    'whatsapp', 'cart_recovery', 'chatwoot', 1, 9, 701, 903, $1,
    $2::timestamptz, 'no_more_messages', 'unmatched'
  )
`, [phone, dueAt]);
const optedOutCandidate = await db.query(`
  select * from public.get_chatwoot_payment_link_candidate(
    $1::uuid, $2, 1, 9, 701, 604800, $3::timestamptz
  )
`, [inbound.rows[0].commercial_case_id, phone, dueAt]);
if (optedOutCandidate.rows[0]?.outcome !== 'blocked_opt_out') {
  throw new Error('durable opt-out did not stop payment link');
}
await db.exec('rollback');
await db.exec('begin');
await db.query(`
  update public.conversations
  set human_takeover = true, status = 'paused_human', automation_status = 'paused'
  where id = $1::uuid
`, [inbound.rows[0].conversation_id]);
const handoffCandidate = await db.query(`
  select * from public.get_chatwoot_payment_link_candidate(
    $1::uuid, $2, 1, 9, 701, 604800, $3::timestamptz
  )
`, [inbound.rows[0].commercial_case_id, phone, dueAt]);
if (handoffCandidate.rows[0]?.outcome !== 'blocked_conversation') {
  throw new Error('human takeover did not stop payment link');
}
await db.exec('rollback');
const finalUrl = `${canonical.commerce.checkout_url}&src=hermes-01K3F8QW7N2VYB4M6X9CDPTZR2`;
await db.exec('begin');
const skippedPrimaryField = await db.query(`
  select * from public.prepare_chatwoot_payment_link_send(
    $1::uuid, $2, 1, 9, 701, '901', 604800,
    $3::uuid, $4::uuid, $5, $6, 'xcod',
    'hermes-01K3F8QW7N2VYB4M6X9CDPTZR2', 'hermes-', $7::timestamptz
  )
`, [
  inbound.rows[0].commercial_case_id, phone, timerId,
  latestSubmission.rows[0].id, canonical.commerce.checkout_url,
  `${canonical.commerce.checkout_url}&xcod=hermes-01K3F8QW7N2VYB4M6X9CDPTZR2`, dueAt,
]);
if (skippedPrimaryField.rows[0]?.outcome !== 'candidate_changed') {
  throw new Error('payment-link preparation skipped available src field');
}
await db.exec('rollback');
const prepared = await db.query(`
  select * from public.prepare_chatwoot_payment_link_send(
    $1::uuid, $2, 1, 9, 701, '902', 604800,
    $3::uuid, $4::uuid, $5, $6, 'src',
    'hermes-01K3F8QW7N2VYB4M6X9CDPTZR2', 'hermes-', $7::timestamptz
  )
`, [
  inbound.rows[0].commercial_case_id, phone, timerId,
  latestSubmission.rows[0].id, canonical.commerce.checkout_url, finalUrl, dueAt,
]);
if (prepared.rows[0]?.outcome !== 'request_started'
    || prepared.rows[0]?.checkout_url_final !== finalUrl
    || prepared.rows[0]?.tracking_field !== 'src'
    || prepared.rows[0]?.tracking_value !== 'hermes-01K3F8QW7N2VYB4M6X9CDPTZR2') {
  throw new Error(`payment-link prepare diverged: ${JSON.stringify(prepared.rows)}`);
}
const persistedBinding = await db.query(`
  select source_reevaluation_id, source_submission_id,
         checkout_url_original, checkout_url_final, tracking_field, tracking_value
  from public.payment_link_bindings
  where id = $1::uuid
`, [prepared.rows[0].binding_id]);
if (persistedBinding.rows.length !== 1
    || persistedBinding.rows[0].source_reevaluation_id !== timerId
    || persistedBinding.rows[0].source_submission_id !== latestSubmission.rows[0].id
    || persistedBinding.rows[0].checkout_url_original !== canonical.commerce.checkout_url
    || persistedBinding.rows[0].checkout_url_final !== finalUrl
    || persistedBinding.rows[0].tracking_field !== 'src'
    || persistedBinding.rows[0].tracking_value !== 'hermes-01K3F8QW7N2VYB4M6X9CDPTZR2') {
  throw new Error(`payment-link binding diverged: ${JSON.stringify(persistedBinding.rows)}`);
}
const preparedReplay = await db.query(`
  select * from public.prepare_chatwoot_payment_link_send(
    $1::uuid, $2, 1, 9, 701, '902', 604800,
    $3::uuid, $4::uuid, $5, $6, 'src',
    'hermes-01K3F8QW7N2VYB4M6X9CDPTZR2', 'hermes-', $7::timestamptz
  )
`, [
  inbound.rows[0].commercial_case_id, phone, timerId,
  latestSubmission.rows[0].id, canonical.commerce.checkout_url, finalUrl, dueAt,
]);
const paymentCounts = await db.query(`
  select
    (select count(*)::integer from public.payment_link_bindings) as bindings,
    (select count(*)::integer from public.payment_link_send_commands) as commands
`);
if (preparedReplay.rows[0]?.outcome !== 'delivery_unknown'
    || paymentCounts.rows[0]?.bindings !== 1
    || paymentCounts.rows[0]?.commands !== 1) {
  throw new Error(`payment-link replay duplicated state: ${JSON.stringify({
    replay: preparedReplay.rows, counts: paymentCounts.rows,
  })}`);
}
for (const statement of [
  `update public.payment_link_bindings set tracking_field = 'xcod'
   where id = '${prepared.rows[0].binding_id}'::uuid`,
  `delete from public.payment_link_bindings
   where id = '${prepared.rows[0].binding_id}'::uuid`,
]) {
  let immutable = false;
  try {
    await db.exec(statement);
  } catch (error) {
    immutable = String(error).includes('payment_link_binding_immutable');
  }
  if (!immutable) throw new Error('payment-link binding accepted mutation');
}

const unknownFinalization = await db.query(`
  select * from public.finalize_chatwoot_payment_link_send(
    $1::uuid, 'delivery_unknown', null, 'chatwoot_send_unconfirmed', $2::timestamptz
  )
`, [prepared.rows[0].send_command_id, dueAt]);
if (unknownFinalization.rows[0]?.outcome !== 'finalized'
    || unknownFinalization.rows[0]?.status !== 'delivery_unknown') {
  throw new Error(`payment-link unknown finalization diverged: ${JSON.stringify(
    unknownFinalization.rows,
  )}`);
}
const unknownReplay = await db.query(`
  select * from public.prepare_chatwoot_payment_link_send(
    $1::uuid, $2, 1, 9, 701, '902', 604800,
    $3::uuid, $4::uuid, $5, $6, 'src',
    'hermes-01K3F8QW7N2VYB4M6X9CDPTZR2', 'hermes-', $7::timestamptz
  )
`, [
  inbound.rows[0].commercial_case_id, phone, timerId,
  latestSubmission.rows[0].id, canonical.commerce.checkout_url, finalUrl, dueAt,
]);
if (unknownReplay.rows[0]?.outcome !== 'delivery_unknown') {
  throw new Error(`payment-link unknown replay diverged: ${JSON.stringify(unknownReplay.rows)}`);
}
const reconciledFinalization = await db.query(`
  select * from public.finalize_chatwoot_payment_link_send(
    $1::uuid, 'accepted_by_chatwoot', 990, null, $2::timestamptz
  )
`, [prepared.rows[0].send_command_id, dueAt]);
const reconciledCommand = await db.query(`
  select status, chatwoot_message_id, failure_code
  from public.payment_link_send_commands
  where id = $1::uuid
`, [prepared.rows[0].send_command_id]);
if (reconciledFinalization.rows[0]?.outcome !== 'reconciled'
    || reconciledFinalization.rows[0]?.status !== 'accepted_by_chatwoot'
    || reconciledCommand.rows[0]?.status !== 'accepted_by_chatwoot'
    || reconciledCommand.rows[0]?.chatwoot_message_id !== 990
    || reconciledCommand.rows[0]?.failure_code !== 'chatwoot_send_unconfirmed') {
  throw new Error(`payment-link unknown reconciliation diverged: ${JSON.stringify({
    finalization: reconciledFinalization.rows,
    command: reconciledCommand.rows,
  })}`);
}

const newestCanonical = {
  ...canonical,
  external_submission_id: '01K3F8QW7N2VYB4M6X9CDPTZR3',
  submitted_at: '2026-08-29T16:00:00Z',
};
const newestSubmission = await db.query(`
  insert into public.precheckout_submissions (
    external_submission_id, contract_version, raw_payload, canonical_payload,
    provisional, provider_observed, activation_authorized
  ) values ($1, '1.1.0', '{}'::jsonb, $2::jsonb, false, true, true)
  returning id
`, ['01K3F8QW7N2VYB4M6X9CDPTZR3', JSON.stringify(newestCanonical)]);
await db.query(`
  insert into public.purchase_intent_submissions (
    purchase_intent_id, submission_id, ordinal
  ) values ($1::uuid, $2::uuid, 3)
`, [intent.rows[0].id, newestSubmission.rows[0].id]);
await db.query(`
  select * from public.schedule_precheckout_first_touch_reevaluation(
    $1::uuid, $2::uuid
  )
`, [intent.rows[0].id, newestSubmission.rows[0].id]);
const staleCommandCandidate = await db.query(`
  select * from public.get_chatwoot_payment_link_candidate(
    $1::uuid, $2, 1, 9, 701, 604800, '2026-08-29T16:30:00Z'::timestamptz
  )
`, [inbound.rows[0].commercial_case_id, phone]);
if (staleCommandCandidate.rows[0]?.outcome !== 'precheckout_sequence_not_latest') {
  throw new Error(`stale recovery sequence authorized a newer checkout: ${JSON.stringify(
    staleCommandCandidate.rows,
  )}`);
}
console.log('PAYMENT_LINK_ATTRIBUTION_SQL_OK');

const acl = await db.query(`
  select
    has_function_privilege('anon',
      'public.list_due_hotmart_abandonment_reevaluations_v2(timestamptz,integer,boolean)',
      'EXECUTE') as anon_due,
    has_function_privilege('authenticated',
      'public.get_precheckout_delayed_one_shot_command(uuid)',
      'EXECUTE') as authenticated_projection,
    has_function_privilege('service_role',
      'public.list_due_hotmart_abandonment_reevaluations_v2(timestamptz,integer,boolean)',
      'EXECUTE') as service_due,
    has_function_privilege('service_role',
      'public.get_precheckout_delayed_one_shot_command(uuid)',
      'EXECUTE') as service_projection,
    has_function_privilege('anon',
      'public.prepare_chatwoot_payment_link_send(uuid,text,bigint,bigint,bigint,text,integer,uuid,uuid,text,text,text,text,text,timestamptz)',
      'EXECUTE') as anon_payment_prepare,
    has_function_privilege('authenticated',
      'public.get_chatwoot_payment_link_candidate(uuid,text,bigint,bigint,bigint,integer,timestamptz)',
      'EXECUTE') as authenticated_payment_candidate,
    has_function_privilege('service_role',
      'public.prepare_chatwoot_payment_link_send(uuid,text,bigint,bigint,bigint,text,integer,uuid,uuid,text,text,text,text,text,timestamptz)',
      'EXECUTE') as service_payment_prepare,
    has_table_privilege('service_role', 'public.payment_link_bindings', 'INSERT')
      as service_binding_insert
`);
if (acl.rows[0].anon_due !== false
    || acl.rows[0].authenticated_projection !== false
    || acl.rows[0].service_due !== true
    || acl.rows[0].service_projection !== true
    || acl.rows[0].anon_payment_prepare !== false
    || acl.rows[0].authenticated_payment_candidate !== false
    || acl.rows[0].service_payment_prepare !== true
    || acl.rows[0].service_binding_insert !== false) {
  throw new Error(`worker RPC ACL diverged: ${JSON.stringify(acl.rows[0])}`);
}

console.log('PRECHECKOUT_DELAYED_WORKER_SENDER_SQL_OK');
