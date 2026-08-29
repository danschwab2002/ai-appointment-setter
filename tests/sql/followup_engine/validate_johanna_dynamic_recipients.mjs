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

async function fixture({
  suffix, email, phone, eventTime,
  eventEmail = email,
  expectedOutcome = 'resolved',
}) {
  const intent = await db.query(`
    insert into public.purchase_intents (
      tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
      normalized_email, normalized_phone, submitted_at, lifecycle_state,
      current_classification, whatsapp_contact_authorized, provisional,
      provider_observed, activation_authorized
    ) values (
      'lancemos', 'psicologajohanna', 'ads-a', 'F106691755G', 'bxjge6zq',
      $1, $2, $3::timestamptz - interval '30 minutes', 'waiting_for_purchase',
      null, true, false, true, true
    ) returning id
  `, [email, phone, eventTime]);
  const intentId = intent.rows[0].id;
  const submission = await db.query(`
    insert into public.precheckout_submissions (
      external_submission_id, contract_version, raw_payload, canonical_payload,
      provisional, provider_observed, activation_authorized
    ) values (
      $1, '1.1.0', '{}'::jsonb,
      jsonb_build_object(
        'lead', jsonb_build_object('full_name', 'Lead ' || $1::text),
        'identity', jsonb_build_object('email', $2::text, 'phone', $3::text),
        'commerce', jsonb_build_object(
          'offer_ref', 'bxjge6zq', 'product_name', 'Libre de Ansiedad'
        ),
        'consent', jsonb_build_object(
          'marketing_optin', true,
          'whatsapp_contact', true,
          'copy_version', 'johanna-precheckout-whatsapp-disclosure-v1'
        )
      ), false, true, true
    ) returning id
  `, [`dynamic-recipient-submission-${suffix}`, email, phone]);
  await db.query(`
    insert into public.purchase_intent_submissions (
      purchase_intent_id, submission_id, ordinal
    ) values ($1::uuid, $2::uuid, 1)
  `, [intentId, submission.rows[0].id]);

  const externalId = `dynamic-recipient-cart-${suffix}`;
  const payload = {
    id: externalId,
    creation_date: Date.parse(eventTime),
    event: 'PURCHASE_OUT_OF_SHOPPING_CART',
    version: '2.0.0',
    data: {
      buyer: { name: `Lead ${suffix}`, email: eventEmail, phone },
      product: { id: 8104005, name: 'Libre de Ansiedad' },
      offer: { code: 'bxjge6zq' },
    },
  };
  const admitted = await db.query(
    'select * from public.admit_hotmart_cart_abandonment($1, $2::jsonb)',
    [externalId, JSON.stringify(payload)],
  );
  const eventId = admitted.rows[0].webhook_event_id;
  const correlated = await db.query(
    'select * from public.correlate_hotmart_purchase_intent($1::uuid)',
    [eventId],
  );
  if (correlated.rows[0]?.outcome !== expectedOutcome
      || (expectedOutcome === 'resolved'
        && correlated.rows[0]?.purchase_intent_id !== intentId)) {
    throw new Error(`correlation failed: ${JSON.stringify(correlated.rows)}`);
  }
  return { intentId, eventId, phone };
}

async function begin(item, suffix) {
  return db.query(`
    select * from public.begin_johanna_abandonment_hotmart_auto_v2(
      $1, $2::uuid, $3::uuid, 1, 9,
      'johanna-abandonment-template-e2e', 2, 1
    )
  `, [`johanna-dynamic-recipient:${suffix}`, item.eventId, item.intentId]);
}

const first = await fixture({
  suffix: 'one',
  email: 'dynamic-one@example.test',
  phone: '15550001111',
  eventTime: '2026-08-25T20:30:00Z',
});
const second = await fixture({
  suffix: 'two',
  email: 'dynamic-two@example.test',
  phone: '15550002222',
  eventTime: '2026-08-25T20:31:00Z',
});
const firstStarted = await begin(first, 'one');
const secondStarted = await begin(second, 'two');
if (firstStarted.rows[0]?.outcome !== 'started'
    || firstStarted.rows[0]?.target_phone !== first.phone
    || secondStarted.rows[0]?.outcome !== 'started'
    || secondStarted.rows[0]?.target_phone !== second.phone) {
  throw new Error(`dynamic recipients failed: ${JSON.stringify({
    first: firstStarted.rows,
    second: secondStarted.rows,
  })}`);
}
const commands = await db.query(`
  select target_phone from public.johanna_abandonment_one_shot_commands
  where command_key like 'johanna-dynamic-recipient:%'
  order by target_phone
`);
if (JSON.stringify(commands.rows.map((row) => row.target_phone))
    !== JSON.stringify([first.phone, second.phone])) {
  throw new Error(`unexpected recipients: ${JSON.stringify(commands.rows)}`);
}

const operatorResolved = await fixture({
  suffix: 'operator',
  email: 'dynamic-operator-form@example.test',
  eventEmail: 'dynamic-operator-hotmart@example.test',
  phone: '15550003333',
  eventTime: '2026-08-25T20:32:00Z',
  expectedOutcome: 'conflict',
});
await db.exec('set role service_role');
const prepared = await db.query(`
  select command_data from public.prepare_operator_correlation_resolution(
    'lancemos', 'psicologajohanna', 'test-operator', $1::uuid,
    'resolve_with_candidate', $2::uuid, 'operator_source_record',
    '99999999-9999-4999-8999-999999999991'::uuid
  )
`, [operatorResolved.eventId, operatorResolved.intentId]);
const commandId = prepared.rows[0]?.command_data?.command_id;
await db.query(`
  select resolution_data from public.confirm_operator_correlation_resolution(
    'lancemos', 'psicologajohanna', 'test-operator', $1::uuid,
    'resolve_with_candidate', $2::uuid
  )
`, [commandId, operatorResolved.intentId]);
const operatorStarted = await begin(operatorResolved, 'operator');
await db.exec('reset role');
if (operatorStarted.rows[0]?.outcome !== 'started'
    || operatorStarted.rows[0]?.target_phone !== operatorResolved.phone) {
  throw new Error(`operator resolution did not authorize one-shot: ${JSON.stringify(
    operatorStarted.rows,
  )}`);
}
console.log('JOHANNA_DYNAMIC_RECIPIENTS_SQL_OK');
