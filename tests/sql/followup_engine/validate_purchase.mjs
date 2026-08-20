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
        'version', '2.0.0',
        'data', jsonb_build_object(
          'buyer', jsonb_build_object('email', '${email}', 'phone', '${phone}'),
          'product', jsonb_build_object('id', 123, 'name', 'Pilot Product'),
          'offer', jsonb_build_object('code', 'OFFER-1')
        )
      ),
      'received', '2026-08-08T00:00:01Z'
    );
    insert into public.contacts (id, full_name, email, phone)
    values ('${contactId}', 'Local Buyer', '${email}', '${phone}');
    insert into public.contact_points (
      contact_id, type, raw_value, normalized_value, source, source_event_id
    ) values
      ('${contactId}', 'email', '${email}', '${email}', 'hotmart', '${eventId}'),
      ('${contactId}', 'phone', '${phone}', '${phone}', 'hotmart', '${eventId}');
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
await db.exec('set role service_role');
const applied = await db.query(`
  select * from public.apply_hotmart_purchase_approved(
    'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1'::uuid,
    'buyer1@example.com', '5531999999999', '123', 'OFFER-1',
    'HP17715690036014', '2026-08-08T00:00:05Z'::timestamptz
  );
`);
await db.exec('reset role');
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
  select rc.status as case_status, sa.status as action_status,
         fda.phase as attempt_phase, fda.outcome as attempt_outcome,
         fda.reconciliation_deadline is not null as has_reconciliation_deadline
  from public.recovery_cases rc
  join public.scheduled_actions sa on sa.recovery_case_id = rc.id
  join public.followup_delivery_attempts fda on fda.action_id = sa.id
  where rc.id = '${plan3.rows[0].recovery_case_id}'::uuid;
`);
if (inFlight.rows[0]?.case_status !== 'won'
    || inFlight.rows[0]?.action_status !== 'delivery_unknown'
    || inFlight.rows[0]?.attempt_phase !== 'completed'
    || inFlight.rows[0]?.attempt_outcome !== 'delivery_unknown'
    || inFlight.rows[0]?.has_reconciliation_deadline !== true) {
  throw new Error(`unexpected in-flight state: ${JSON.stringify(inFlight.rows[0])}`);
}
console.log('IN_FLIGHT_DELIVERY_PRESERVED_AS_UNKNOWN_OK');

// Una reserva sin request iniciado debe cerrarse como failed_before_request.
await insertAbandonment({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa5',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc5',
  externalId: 'abandonment-5',
  email: 'buyer5@example.com',
  phone: '5531555555555',
});
const plan5 = await plan({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa5',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc5',
});
await db.exec(`
  update public.recovery_cases
  set created_at = '2026-08-08T00:00:02Z'
  where id = '${plan5.rows[0].recovery_case_id}'::uuid;
  insert into public.followup_delivery_attempts (
    action_id, idempotency_key, attempt_number, channel, mode,
    phase, started_at, lease_generation,
    expected_case_version, expected_sequence_revision
  ) values (
    '${plan5.rows[0].scheduled_action_id}'::uuid,
    'purchase-reserved-1', 1, 'whatsapp', 'freeform',
    'reserved', '2026-08-08T00:00:03Z', 1, 1, 1
  );
`);
await insertPurchase({
  eventId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb5',
  externalId: 'purchase-5',
  email: 'buyer5@example.com',
  phone: '5531555555555',
  transaction: 'HP17715690036018',
});
await db.query(`
  select * from public.apply_hotmart_purchase_approved(
    'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb5'::uuid,
    'buyer5@example.com', '5531555555555', '123', 'OFFER-1',
    'HP17715690036018', '2026-08-08T00:00:05Z'::timestamptz
  );
`);
const reserved = await db.query(`
  select sa.status as action_status, fda.phase as attempt_phase,
         fda.outcome as attempt_outcome
  from public.scheduled_actions sa
  join public.followup_delivery_attempts fda on fda.action_id = sa.id
  where sa.id = '${plan5.rows[0].scheduled_action_id}'::uuid;
`);
if (reserved.rows[0]?.action_status !== 'cancelled'
    || reserved.rows[0]?.attempt_phase !== 'completed'
    || reserved.rows[0]?.attempt_outcome !== 'failed_before_request') {
  throw new Error(`unexpected reserved state: ${JSON.stringify(reserved.rows[0])}`);
}
console.log('RESERVED_ATTEMPT_CLOSED_BEFORE_REQUEST_OK');

// Una cancelación ajena a compras y sin terminal_reason no toca el ledger.
await insertAbandonment({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa6',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc6',
  externalId: 'abandonment-6',
  email: 'buyer6@example.com',
  phone: '5531222222222',
});
const plan6 = await plan({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa6',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc6',
});
await db.exec(`
  insert into public.followup_delivery_attempts (
    action_id, idempotency_key, attempt_number, channel, mode,
    phase, started_at, lease_generation,
    expected_case_version, expected_sequence_revision
  ) values (
    '${plan6.rows[0].scheduled_action_id}'::uuid,
    'non-purchase-reserved-1', 1, 'whatsapp', 'freeform',
    'reserved', '2026-08-08T00:00:03Z', 1, 1, 1
  );
  update public.scheduled_actions
  set status = 'cancelled'
  where id = '${plan6.rows[0].scheduled_action_id}'::uuid;
`);
const unrelatedCancellation = await db.query(`
  select phase, outcome, reason_code
  from public.followup_delivery_attempts
  where action_id = '${plan6.rows[0].scheduled_action_id}'::uuid;
`);
if (unrelatedCancellation.rows[0]?.phase !== 'reserved'
    || unrelatedCancellation.rows[0]?.outcome !== null
    || unrelatedCancellation.rows[0]?.reason_code !== null) {
  throw new Error(
    `purchase trigger touched unrelated attempt: ${JSON.stringify(unrelatedCancellation.rows[0])}`
  );
}
console.log('UNRELATED_NULL_REASON_CANCELLATION_PRESERVES_ATTEMPT_OK');

// Compra previa con email y teléfono apuntando a contactos distintos: fail closed.
await insertPurchase({
  eventId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb4',
  externalId: 'purchase-4',
  email: 'buyer4@example.com',
  phone: '5531444444444',
  transaction: 'HP17715690036017',
});
await insertAbandonment({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc4',
  externalId: 'abandonment-4',
  email: 'buyer4@example.com',
  phone: '5531666666666',
});
await db.exec(`
  insert into public.contacts (id, full_name, email, phone)
  values (
    'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
    'Other Buyer', 'other4@example.com', '5531444444444'
  );
  insert into public.contact_points (
    contact_id, type, raw_value, normalized_value, source
  ) values (
    'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
    'phone', '5531444444444', '5531444444444', 'hotmart'
  );
`);
const plan4 = await plan({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc4',
});
const ambiguous = await db.query(`
  select rc.status as case_status, fs.status as sequence_status,
         sa.status as action_status, sa.terminal_reason,
         we.processing_status as purchase_status
  from public.recovery_cases rc
  join public.followup_sequences fs on fs.recovery_case_id = rc.id
  join public.scheduled_actions sa on sa.recovery_case_id = rc.id
  join public.webhook_events we on we.external_event_id = 'purchase-4'
  where rc.id = '${plan4.rows[0].recovery_case_id}'::uuid;
`);
if (ambiguous.rows[0]?.case_status !== 'paused'
    || ambiguous.rows[0]?.sequence_status !== 'paused'
    || ambiguous.rows[0]?.action_status !== 'cancelled'
    || ambiguous.rows[0]?.terminal_reason !== 'purchase_correlation_ambiguous'
    || ambiguous.rows[0]?.purchase_status !== 'failed') {
  throw new Error(`unexpected ambiguous inverse state: ${JSON.stringify(ambiguous.rows[0])}`);
}
console.log('AMBIGUOUS_INVERSE_PURCHASE_FAILS_CLOSED_OK');

// La admisión rechaza tipos que el worker no puede procesar, sin reservar la
// transacción. Una entrega corregida puede ingresar después.
let malformedAdmissionBlocked = false;
try {
  await db.query(`
    select * from public._admit_hotmart_purchase_approved_base(
      'purchase-semantic-malformed',
      jsonb_build_object(
        'id', 'purchase-semantic-malformed',
        'creation_date', 1786147200000,
        'event', 'PURCHASE_APPROVED',
        'version', '2.0.0',
        'data', jsonb_build_object(
          'buyer', jsonb_build_object('email', 'malformed@example.com'),
          'product', jsonb_build_object('id', '123'),
          'purchase', jsonb_build_object(
            'status', 'APPROVED',
            'transaction', 'HPSEMANTICBAD01',
            'approved_date', 1786147205000
          )
        )
      )
    );
  `);
} catch (error) {
  malformedAdmissionBlocked = String(error).includes('invalid_purchase_admission_input');
}
if (!malformedAdmissionBlocked) {
  throw new Error('unprocessable purchase was admitted');
}
const correctedAfterMalformed = await db.query(`
  select * from public._admit_hotmart_purchase_approved_base(
    'purchase-semantic-corrected-after-malformed',
    jsonb_build_object(
      'id', 'purchase-semantic-corrected-after-malformed',
      'creation_date', 1786147200000,
      'event', 'PURCHASE_APPROVED',
      'version', '2.0.0',
      'data', jsonb_build_object(
        'buyer', jsonb_build_object('email', 'malformed@example.com'),
        'product', jsonb_build_object('id', 123),
        'purchase', jsonb_build_object(
          'status', 'APPROVED',
          'transaction', 'HPSEMANTICBAD01',
          'approved_date', 1786147205000
        )
      )
    )
  );
`);
if (correctedAfterMalformed.rows[0]?.outcome !== 'inserted') {
  throw new Error(`corrected purchase was suppressed: ${JSON.stringify(correctedAfterMalformed.rows[0])}`);
}
console.log('UNPROCESSABLE_PURCHASE_DOES_NOT_RESERVE_TRANSACTION_OK');

// Una fila malformada creada por el bridge anterior nunca puede comparar como
// duplicate con una entrega corregida y procesable.
await db.exec(`
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload, processing_status
  ) values (
    'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb9',
    'hotmart', 'purchase-semantic-legacy-malformed', 'PURCHASE_APPROVED',
    jsonb_build_object(
      'id', 'purchase-semantic-legacy-malformed',
      'creation_date', 1786147200000,
      'event', 'PURCHASE_APPROVED',
      'version', '2.0.0',
      'data', jsonb_build_object(
        'buyer', jsonb_build_object('email', 'legacy@example.com'),
        'product', jsonb_build_object('id', '123'),
        'purchase', jsonb_build_object(
          'status', 'APPROVED',
          'transaction', 'HPSEMANTICLEG01',
          'approved_date', 1786147205000
        )
      )
    ),
    'failed'
  );
`);
const correctedLegacy = await db.query(`
  select * from public._admit_hotmart_purchase_approved_base(
    'purchase-semantic-legacy-corrected',
    jsonb_build_object(
      'id', 'purchase-semantic-legacy-corrected',
      'creation_date', 1786147201000,
      'event', 'PURCHASE_APPROVED',
      'version', '2.0.0',
      'data', jsonb_build_object(
        'buyer', jsonb_build_object('email', 'legacy@example.com'),
        'product', jsonb_build_object('id', 123),
        'purchase', jsonb_build_object(
          'status', 'APPROVED',
          'transaction', 'HPSEMANTICLEG01',
          'approved_date', 1786147205000
        )
      )
    )
  );
`);
if (correctedLegacy.rows[0]?.outcome !== 'semantic_conflict') {
  throw new Error(`legacy malformed row suppressed correction: ${JSON.stringify(correctedLegacy.rows[0])}`);
}
await db.exec(`
  update public.hotmart_purchase_semantic_conflicts
  set resolved_at = clock_timestamp(), resolution = 'legacy-probe-resolved'
  where incoming_external_event_id = 'purchase-semantic-legacy-corrected';
`);
console.log('LEGACY_MALFORMED_PURCHASE_CANNOT_SUPPRESS_CORRECTION_OK');

// La misma transacción sólo es duplicate cuando la tupla de negocio coincide.
await insertAbandonment({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa7',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc7',
  externalId: 'abandonment-7',
  email: 'buyer7@example.com',
  phone: '5599990000001',
});
const plan7 = await plan({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa7',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc7',
});
const firstAdmission = await db.query(`
  select * from public._admit_hotmart_purchase_approved_base(
    'purchase-semantic-1',
    jsonb_build_object(
      'id', 'purchase-semantic-1',
      'creation_date', 1786147200000,
      'event', 'PURCHASE_APPROVED',
      'version', '2.0.0',
      'data', jsonb_build_object(
        'buyer', jsonb_build_object(
          'email', 'buyer7@example.com',
          'checkout_phone', '5599990000001'
        ),
        'product', jsonb_build_object('id', 123),
        'purchase', jsonb_build_object(
          'status', 'APPROVED',
          'transaction', 'HPSEMANTIC00001',
          'approved_date', 1786147205000,
          'offer', jsonb_build_object('code', 'OFFER-1')
        )
      )
    )
  );
`);
if (firstAdmission.rows[0]?.outcome !== 'inserted') {
  throw new Error(`first semantic admission failed: ${JSON.stringify(firstAdmission.rows[0])}`);
}
const exactReplay = await db.query(`
  select * from public._admit_hotmart_purchase_approved_base(
    'purchase-semantic-exact-replay',
    jsonb_build_object(
      'id', 'purchase-semantic-exact-replay',
      'creation_date', 1786147201000,
      'event', 'PURCHASE_APPROVED',
      'version', '2.0.0',
      'data', jsonb_build_object(
        'buyer', jsonb_build_object(
          'email', ' BUYER7@example.com ',
          'checkout_phone', '5599990000001'
        ),
        'product', jsonb_build_object('id', 123),
        'purchase', jsonb_build_object(
          'status', 'APPROVED',
          'transaction', 'HPSEMANTIC00001',
          'approved_date', 1786147205000,
          'offer', jsonb_build_object('code', ' OFFER-1 ')
        )
      )
    )
  );
`);
const beforeConflict = await db.query(`
  select rc.status as case_status, sa.status as action_status,
         (select count(*)::integer
          from public.hotmart_purchase_semantic_conflicts
          where resolved_at is null) as conflicts
  from public.recovery_cases rc
  join public.scheduled_actions sa on sa.recovery_case_id = rc.id
  where rc.id = '${plan7.rows[0].recovery_case_id}'::uuid;
`);
if (exactReplay.rows[0]?.outcome !== 'duplicate'
    || beforeConflict.rows[0]?.case_status !== 'grace_period'
    || beforeConflict.rows[0]?.action_status !== 'pending'
    || beforeConflict.rows[0]?.conflicts !== 0) {
  throw new Error(`exact replay was not idempotent: ${JSON.stringify({
    replay: exactReplay.rows[0], state: beforeConflict.rows[0],
  })}`);
}
console.log('PURCHASE_SEMANTIC_EXACT_REPLAY_OK');

const semanticConflict = await db.query(`
  select * from public._admit_hotmart_purchase_approved_base(
    'purchase-semantic-corrected',
    jsonb_build_object(
      'id', 'purchase-semantic-corrected',
      'creation_date', 1786147200000,
      'event', 'PURCHASE_APPROVED',
      'version', '2.0.0',
      'data', jsonb_build_object(
        'buyer', jsonb_build_object(
          'email', 'corrected-buyer@example.com',
          'checkout_phone', '5531888888888'
        ),
        'product', jsonb_build_object('id', 123),
        'purchase', jsonb_build_object(
          'status', 'APPROVED',
          'transaction', 'HPSEMANTIC00001',
          'approved_date', 1786147205000,
          'offer', jsonb_build_object('code', 'OFFER-1')
        )
      )
    )
  );
`);
const conflictState = await db.query(`
  select count(*)::integer as unresolved_conflicts
  from public.hotmart_purchase_semantic_conflicts
  where resolved_at is null;
`);
if (semanticConflict.rows[0]?.outcome !== 'semantic_conflict'
    || conflictState.rows[0]?.unresolved_conflicts !== 1) {
  throw new Error(`semantic conflict did not fail closed: ${JSON.stringify({
    admission: semanticConflict.rows[0], state: conflictState.rows[0],
  })}`);
}
console.log('PURCHASE_SEMANTIC_CONFLICT_FAILS_CLOSED_OK');

// Un conflicto durable serializa y bloquea la frontera request_started, incluso
// si un plan futuro sigue visible como pendiente.
await insertAbandonment({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa8',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc8',
  externalId: 'abandonment-8',
  email: 'buyer8@example.com',
  phone: '5531888888889',
});
const plan8 = await plan({
  eventId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa8',
  contactId: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc8',
});
await db.exec(`
  insert into public.followup_delivery_attempts (
    action_id, idempotency_key, attempt_number, channel, mode,
    phase, started_at, lease_generation,
    expected_case_version, expected_sequence_revision
  ) values (
    '${plan8.rows[0].scheduled_action_id}'::uuid,
    'semantic-conflict-gated-1', 1, 'whatsapp', 'freeform',
    'reserved', '2026-08-08T00:00:03Z', 1, 1, 1
  );
`);
let requestStartBlocked = false;
try {
  await db.exec(`
    update public.followup_delivery_attempts
    set phase = 'request_started',
        request_started_at = '2026-08-08T00:00:04Z'
    where action_id = '${plan8.rows[0].scheduled_action_id}'::uuid;
  `);
} catch (error) {
  requestStartBlocked = String(error).includes('unresolved_purchase_semantic_conflict');
}
const gatedAttempt = await db.query(`
  select phase
  from public.followup_delivery_attempts
  where action_id = '${plan8.rows[0].scheduled_action_id}'::uuid;
`);
if (!requestStartBlocked || gatedAttempt.rows[0]?.phase !== 'reserved') {
  throw new Error(`request start escaped semantic conflict: ${JSON.stringify(gatedAttempt.rows[0])}`);
}
console.log('UNRESOLVED_PURCHASE_SEMANTIC_CONFLICT_BLOCKS_REQUEST_START_OK');

// El mismo delivery conflictivo conserva su outcome después de resolución y no
// reabre el incidente. La frontera se libera sólo al quedar resuelto.
await db.exec(`
  update public.hotmart_purchase_semantic_conflicts
  set resolved_at = clock_timestamp(), resolution = 'local-test-resolution'
  where incoming_external_event_id = 'purchase-semantic-corrected';
`);
const replayAfterResolution = await db.query(`
  select * from public._admit_hotmart_purchase_approved_base(
    'purchase-semantic-corrected',
    jsonb_build_object(
      'id', 'purchase-semantic-corrected',
      'creation_date', 1786147200000,
      'event', 'PURCHASE_APPROVED',
      'version', '2.0.0',
      'data', jsonb_build_object(
        'buyer', jsonb_build_object(
          'email', 'corrected-buyer@example.com',
          'checkout_phone', '5531888888888'
        ),
        'product', jsonb_build_object('id', 123),
        'purchase', jsonb_build_object(
          'status', 'APPROVED',
          'transaction', 'HPSEMANTIC00001',
          'approved_date', 1786147205000,
          'offer', jsonb_build_object('code', 'OFFER-1')
        )
      )
    )
  );
`);
await db.exec(`
  update public.followup_delivery_attempts
  set phase = 'request_started',
      request_started_at = '2026-08-08T00:00:04Z'
  where action_id = '${plan8.rows[0].scheduled_action_id}'::uuid;
`);
const releasedAttempt = await db.query(`
  select phase,
         (select count(*)::integer
          from public.hotmart_purchase_semantic_conflicts
          where resolved_at is null) as unresolved_conflicts
  from public.followup_delivery_attempts
  where action_id = '${plan8.rows[0].scheduled_action_id}'::uuid;
`);
if (replayAfterResolution.rows[0]?.outcome !== 'semantic_conflict'
    || releasedAttempt.rows[0]?.phase !== 'request_started'
    || releasedAttempt.rows[0]?.unresolved_conflicts !== 0) {
  throw new Error(`resolved conflict replay was not idempotent: ${JSON.stringify({
    replay: replayAfterResolution.rows[0], attempt: releasedAttempt.rows[0],
  })}`);
}
console.log('RESOLVED_PURCHASE_SEMANTIC_CONFLICT_REPLAY_OK');

await db.close();
