import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const db = new PGlite();
await db.waitReady;

const schemaFiles = [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(join(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => join(root, 'supabase/migrations', name)),
];
for (const file of schemaFiles) {
  const sql = readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto omitted: gen_random_uuid is built into this PostgreSQL runtime',
  );
  await db.exec(sql);
}

await db.exec(`
insert into public.followup_policy_versions (
  policy_key, version, status, purpose, timezone, business_windows,
  grace_period, expires_after, max_automatic_messages, steps,
  approved_by, approved_at, published_at
) values (
  'purchase-test', 1, 'published', 'cart_recovery', 'UTC', '{}'::jsonb,
  interval '1 hour', interval '7 days', 3, '[]'::jsonb,
  'local-test', '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z'
);
`);

async function insertAbandonment({ eventId, contactId, externalId, email, phone }) {
  await db.exec(`
    insert into public.webhook_events (
      id, source, external_event_id, event_type, payload, processing_status, received_at
    ) values (
      '${eventId}', 'hotmart', '${externalId}', 'PURCHASE_OUT_OF_SHOPPING_CART',
      jsonb_build_object(
        'id', '${externalId}',
        'creation_date', 1786147200000,
        'event', 'PURCHASE_OUT_OF_SHOPPING_CART',
        'version', '2.0.0'
      ),
      'received', '2026-08-08T00:00:01Z'
    );
    insert into public.contacts (id, full_name, email, phone)
    values ('${contactId}', 'Local Buyer', '${email}', '${phone}');
    insert into public.contact_points (
      contact_id, type, raw_value, normalized_value, source, source_event_id
    ) values (
      '${contactId}', 'email', '${email}', '${email}', 'hotmart', '${eventId}'
    );
  `);
}

async function plan({ eventId, contactId }) {
  return db.query(`
    select * from public.plan_cart_recovery(
      '${eventId}'::uuid,
      '${contactId}'::uuid,
      '123',
      'Pilot Product',
      'OFFER-1',
      'purchase-test',
      1,
      '2026-08-08T00:00:00Z'::timestamptz
    );
  `);
}

async function insertPurchase({ eventId, externalId, email, phone, transaction, status = 'received' }) {
  const error = status === 'failed'
    ? "'purchase_correlation_contact_not_found'"
    : 'null';
  await db.exec(`
    insert into public.webhook_events (
      id, source, external_event_id, event_type, payload,
      processing_status, processing_error, received_at
    ) values (
      '${eventId}', 'hotmart', '${externalId}', 'PURCHASE_APPROVED',
      jsonb_build_object(
        'id', '${externalId}',
        'creation_date', 1786147210000,
        'event', 'PURCHASE_APPROVED',
        'version', '2.0.0',
        'data', jsonb_build_object(
          'buyer', jsonb_build_object('email', '${email}', 'checkout_phone', '${phone}'),
          'product', jsonb_build_object('id', 123),
          'purchase', jsonb_build_object(
            'status', 'APPROVED',
            'transaction', '${transaction}',
            'approved_date', 1786147205000,
            'offer', jsonb_build_object('code', 'OFFER-1')
          )
        )
      ),
      '${status}', ${error}, '2026-08-08T00:00:10Z'
    );
  `);
}

// Compra procesada después de que ya existe el caso.
await insertAbandonment({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc1',
  externalId: 'abandonment-1',
  email: 'buyer1@example.com',
  phone: '5531999999999',
});
const plan1 = await plan({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc1',
});
await db.exec(`update public.recovery_cases set created_at = '2026-08-08T00:00:02Z' where id = '${plan1.rows[0].recovery_case_id}'::uuid;`);
await insertPurchase({
  eventId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1',
  externalId: 'purchase-1',
  email: 'buyer1@example.com',
  phone: '5531999999999',
  transaction: 'HP17715690036014',
});
const applied = await db.query(`
  select * from public.apply_hotmart_purchase_approved(
    'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1'::uuid,
    'buyer1@example.com', '5531999999999', '123', 'OFFER-1',
    'HP17715690036014', '2026-08-08T00:00:05Z'::timestamptz
  );
`);
if (applied.rows[0]?.outcome !== 'applied') throw new Error('purchase RPC was not applied');
const state1 = await db.query(`
  select rc.status as case_status, fs.status as sequence_status, sa.status as action_status,
         we.processing_status as event_status
  from public.recovery_cases rc
  join public.followup_sequences fs on fs.recovery_case_id = rc.id
  join public.scheduled_actions sa on sa.recovery_case_id = rc.id
  join public.webhook_events we on we.id = rc.purchase_event_id
  where rc.id = '${plan1.rows[0].recovery_case_id}'::uuid;
`);
const expected = JSON.stringify({
  case_status: 'won', sequence_status: 'completed',
  action_status: 'cancelled', event_status: 'processed',
});
if (JSON.stringify(state1.rows[0]) !== expected) {
  throw new Error(`unexpected direct state: ${JSON.stringify(state1.rows[0])}`);
}
console.log('DIRECT_PURCHASE_APPLIED_OK');

// Compra conocida antes de que llegue/se procese el abandono.
await insertPurchase({
  eventId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2',
  externalId: 'purchase-2',
  email: 'buyer2@example.com',
  phone: '5531888888888',
  transaction: 'HP17715690036015',
  status: 'failed',
});
await insertAbandonment({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc2',
  externalId: 'abandonment-2',
  email: 'buyer2@example.com',
  phone: '5531888888888',
});
const plan2 = await plan({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc2',
});
const state2 = await db.query(`
  select rc.status as case_status, fs.status as sequence_status, sa.status as action_status,
         we.processing_status as event_status
  from public.recovery_cases rc
  join public.followup_sequences fs on fs.recovery_case_id = rc.id
  join public.scheduled_actions sa on sa.recovery_case_id = rc.id
  join public.webhook_events we on we.id = rc.purchase_event_id
  where rc.id = '${plan2.rows[0].recovery_case_id}'::uuid;
`);
if (JSON.stringify(state2.rows[0]) !== expected) {
  throw new Error(`unexpected ordering-guard state: ${JSON.stringify(state2.rows[0])}`);
}
console.log('PURCHASE_BEFORE_ABANDONMENT_GUARD_OK');

// Una solicitud externa ya iniciada no puede declararse engañosamente cancelada.
await insertAbandonment({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc3',
  externalId: 'abandonment-3',
  email: 'buyer3@example.com',
  phone: '5531777777777',
});
const plan3 = await plan({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc3',
});
await db.exec(`
  update public.recovery_cases
  set created_at = '2026-08-08T00:00:02Z'
  where id = '${plan3.rows[0].recovery_case_id}'::uuid;
  insert into public.followup_delivery_attempts (
    action_id, idempotency_key, attempt_number, channel, mode,
    phase, started_at, request_started_at,
    lease_generation, expected_case_version, expected_sequence_revision
  ) values (
    '${plan3.rows[0].scheduled_action_id}'::uuid,
    'purchase-in-flight-1', 1, 'whatsapp', 'freeform',
    'request_started', '2026-08-08T00:00:03Z', '2026-08-08T00:00:04Z',
    1, 1, 1
  );
`);
await insertPurchase({
  eventId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3',
  externalId: 'purchase-3',
  email: 'buyer3@example.com',
  phone: '5531777777777',
  transaction: 'HP17715690036016',
});
await db.query(`
  select * from public.apply_hotmart_purchase_approved(
    'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3'::uuid,
    'buyer3@example.com', '5531777777777', '123', 'OFFER-1',
    'HP17715690036016', '2026-08-08T00:00:05Z'::timestamptz
  );
`);
const inFlight = await db.query(`
  select rc.status as case_status, sa.status as action_status
  from public.recovery_cases rc
  join public.scheduled_actions sa on sa.recovery_case_id = rc.id
  where rc.id = '${plan3.rows[0].recovery_case_id}'::uuid;
`);
if (inFlight.rows[0]?.case_status !== 'won'
    || inFlight.rows[0]?.action_status !== 'delivery_unknown') {
  throw new Error(`unexpected in-flight state: ${JSON.stringify(inFlight.rows[0])}`);
}
console.log('IN_FLIGHT_DELIVERY_PRESERVED_AS_UNKNOWN_OK');

await db.close();
