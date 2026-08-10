import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { PGlite } from '@electric-sql/pglite';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const read = (path) => readFile(`${root}/${path}`, 'utf8');
const baseline = (await read('supabase/baseline/20260803_public_schema.sql'))
  .replace('create extension if not exists pgcrypto;', '-- omitted in PGlite');
const migrations = await Promise.all([
  'supabase/migrations/20260803000100_followup_engine_v1.sql',
  'supabase/migrations/20260804000200_followup_identity_binding.sql',
  'supabase/migrations/20260805000100_followup_identity_audit.sql',
  'supabase/migrations/20260805000200_followup_contact_authorization_grant.sql',
  'supabase/migrations/20260810000200_hotmart_cart_abandonment_authoritative.sql',
].map(read));

const db = new PGlite();
await db.waitReady;
await db.exec(baseline);
for (const migration of migrations) await db.exec(migration);
console.log('cart_abandonment_migration_apply=OK');

await db.exec(`
  insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
  ) values (
    'cart-authoritative-test', 1, 'published', 'cart_recovery', 'UTC',
    '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
    interval '1 minute', interval '7 days', 3,
    '[{"step_key":"first_contact","mode":"freeform"}]'::jsonb,
    'operator-test', now(), now()
  );
`);

const abandonedAt = '2099-01-01T00:00:00.000Z';
const payload = {
  id: 'cart-event-001',
  creation_date: Date.parse(abandonedAt),
  event: 'PURCHASE_OUT_OF_SHOPPING_CART',
  version: '2.0.0',
  data: {
    buyer: {
      email: 'buyer@example.com',
      phone: 'invalid123',
      checkout_phone: '+55 (31) 99999-9999',
    },
    product: { id: 3526906, name: 'Product' },
    offer: { code: 'offer-a' },
  },
};

let malformedRejected = false;
try {
  await db.query(
    'select * from public.admit_hotmart_cart_abandonment($1, $2::jsonb)',
    [payload.id, JSON.stringify({ ...payload, data: {} })],
  );
} catch (error) {
  malformedRejected = String(error).includes('invalid_cart_abandonment_admission_input');
}
if (!malformedRejected) throw new Error('malformed admission did not fail closed');

const checkoutPhonePayload = structuredClone(payload);
checkoutPhonePayload.id = 'cart-checkout-phone-001';
delete checkoutPhonePayload.data.buyer.email;
checkoutPhonePayload.data.buyer.phone = 'invalid123';
checkoutPhonePayload.data.buyer.checkout_phone = '+55 (11) 98888-7777';
const checkoutPhoneCanonical = await db.query(`
  select
    public.hotmart_cart_abandonment_payload_is_processable($1, $2::jsonb)
      as processable,
    public.hotmart_cart_abandonment_semantic_tuple($2::jsonb) ->> 'buyer_phone'
      as semantic_phone
`, [checkoutPhonePayload.id, JSON.stringify(checkoutPhonePayload)]);
if (checkoutPhoneCanonical.rows[0]?.processable !== true
    || checkoutPhoneCanonical.rows[0]?.semantic_phone !== '5511988887777') {
  throw new Error('checkout_phone fallback diverged before admission');
}
const checkoutPhoneAdmitted = await db.query(
  'select * from public.admit_hotmart_cart_abandonment($1, $2::jsonb)',
  [checkoutPhonePayload.id, JSON.stringify(checkoutPhonePayload)],
);
const changedCheckoutPhonePayload = structuredClone(checkoutPhonePayload);
changedCheckoutPhonePayload.data.buyer.checkout_phone = '+55 (11) 97777-6666';
const checkoutPhoneConflict = await db.query(
  'select * from public.admit_hotmart_cart_abandonment($1, $2::jsonb)',
  [checkoutPhonePayload.id, JSON.stringify(changedCheckoutPhonePayload)],
);
if (checkoutPhoneAdmitted.rows[0]?.outcome !== 'inserted'
    || checkoutPhoneConflict.rows[0]?.outcome !== 'semantic_conflict') {
  throw new Error('checkout_phone admission or semantic replay was not canonical');
}
await db.query(`
  update public.hotmart_cart_abandonment_semantic_conflicts
  set resolved_at = now(), resolution = 'test cleanup'
  where incoming_external_event_id = $1
`, [checkoutPhonePayload.id]);
console.log('cart_abandonment_checkout_phone_canonical=OK');

const admitted = await db.query(
  'select * from public.admit_hotmart_cart_abandonment($1, $2::jsonb)',
  [payload.id, JSON.stringify(payload)],
);
const duplicate = await db.query(
  'select * from public.admit_hotmart_cart_abandonment($1, $2::jsonb)',
  [payload.id, JSON.stringify(payload)],
);
if (admitted.rows[0]?.outcome !== 'inserted' || duplicate.rows[0]?.outcome !== 'duplicate') {
  throw new Error('semantic admission replay invariant failed');
}
console.log('cart_abandonment_exact_replay=OK');

const eventId = admitted.rows[0].webhook_event_id;
await db.query(`
  insert into public.contacts (id, full_name, email, phone)
  values ('00000000-0000-0000-0000-000000000101', 'Buyer', $1, $2)
`, ['buyer@example.com', '5531999999999']);
await db.query(`
  insert into public.contact_points (
    contact_id, type, raw_value, normalized_value, source, source_event_id
  ) values
    ('00000000-0000-0000-0000-000000000101', 'email', $1, $1, 'hotmart', $3),
    ('00000000-0000-0000-0000-000000000101', 'phone', $2, $2, 'hotmart', $3);
`, ['buyer@example.com', '5531999999999', eventId]);

await db.query(`
  delete from public.contact_points
  where contact_id = '00000000-0000-0000-0000-000000000101'
    and type = 'phone'
`);
let partialIdentityRejected = false;
try {
  await db.query(`
    select * from public.plan_cart_recovery(
      $1::uuid, '00000000-0000-0000-0000-000000000101'::uuid,
      '3526906', 'Product', 'offer-a',
      'cart-authoritative-test', 1, $2::timestamptz
    )
  `, [eventId, abandonedAt]);
} catch (error) {
  partialIdentityRejected = String(error).includes('cart_abandonment_contact_mismatch');
}
if (!partialIdentityRejected) throw new Error('partial identity binding was accepted');
await db.query(`
  insert into public.contact_points (
    contact_id, type, raw_value, normalized_value, source, source_event_id
  ) values (
    '00000000-0000-0000-0000-000000000101', 'phone',
    '5531999999999', '5531999999999', 'hotmart', $1::uuid
  )
`, [eventId]);
console.log('cart_abandonment_each_identifier_bound=OK');

const simulatorEventId = '00000000-0000-4000-8000-000000000202';
const simulatorPayload = structuredClone(payload);
simulatorPayload.id = 'simulator-cart-001';
await db.query(`
  insert into public.webhook_events (
    id, source, external_event_id, event_type, payload, processing_status
  ) values ($1::uuid, 'simulator', $2, 'PURCHASE_OUT_OF_SHOPPING_CART', $3::jsonb, 'received')
`, [simulatorEventId, simulatorPayload.id, JSON.stringify(simulatorPayload)]);
let simulatorRejected = false;
try {
  await db.query(`
    select * from public.plan_cart_recovery(
      $1::uuid, '00000000-0000-0000-0000-000000000101'::uuid,
      '3526906', 'Product', 'offer-a',
      'cart-authoritative-test', 1, $2::timestamptz
    )
  `, [simulatorEventId, abandonedAt]);
} catch (error) {
  simulatorRejected = String(error).includes('cart_abandonment_event_not_authoritative');
}
if (!simulatorRejected) throw new Error('simulator event granted Hotmart authority');
console.log('cart_abandonment_simulator_authority_reject=OK');

let mismatchRejected = false;
try {
  await db.query(`
    select * from public.plan_cart_recovery(
      $1, '00000000-0000-0000-0000-000000000101',
      '999', 'Wrong Product', 'offer-a',
      'cart-authoritative-test', 1, $2::timestamptz
    )
  `, [eventId, abandonedAt]);
} catch (error) {
  mismatchRejected = String(error).includes('cart_abandonment_product_mismatch');
}
if (!mismatchRejected) throw new Error('canonical product mismatch was accepted');
console.log('cart_abandonment_plan_binding_reject=OK');

let productNameRejected = false;
try {
  await db.query(`
    select * from public.plan_cart_recovery(
      $1::uuid, '00000000-0000-0000-0000-000000000101'::uuid,
      '3526906', 'Wrong Product', 'offer-a',
      'cart-authoritative-test', 1, $2::timestamptz
    )
  `, [eventId, abandonedAt]);
} catch (error) {
  productNameRejected = String(error).includes('cart_abandonment_product_name_mismatch');
}
if (!productNameRejected) throw new Error('canonical product name mismatch was accepted');
console.log('cart_abandonment_product_name_binding_reject=OK');

const plan = await db.query(`
  select * from public.plan_cart_recovery_with_identity(
    $1, '00000000-0000-0000-0000-000000000101',
    '3526906', 'Product', 'offer-a',
    'cart-authoritative-test', 1, $2::timestamptz,
    1, 7, '5531999999999'
  )
`, [eventId, abandonedAt]);
if (plan.rows.length !== 1 || plan.rows[0].created !== true) {
  throw new Error('canonical plan was not created');
}
const grant = await db.query(`
  select count(*)::int as count
  from public.contact_authorizations
  where contact_id='00000000-0000-0000-0000-000000000101'
    and channel='whatsapp'
    and purpose='cart_recovery'
    and authorization_status='allowed'
    and authorization_source='hotmart'
`);
if (grant.rows[0].count !== 1) throw new Error('atomic authorization grant missing');
console.log('cart_abandonment_plan_authorized=OK');

let updateBypassRejected = false;
try {
  await db.query(`
    update public.recovery_case_events
    set observed_at = observed_at + interval '1 second'
  `);
} catch (error) {
  updateBypassRejected = String(error).includes('cart_abandonment_binding_immutable');
}
if (!updateBypassRejected) throw new Error('binding update bypass was not rejected');
console.log('cart_abandonment_binding_update_bypass_reject=OK');

let metadataUpdateRejected = false;
try {
  await db.query(`
    update public.recovery_case_events
    set created_at = created_at + interval '1 second'
    where recovery_case_id = $1::uuid
      and event_role = 'cart_abandonment'
  `, [plan.rows[0].recovery_case_id]);
} catch (error) {
  metadataUpdateRejected = String(error).includes('cart_abandonment_binding_immutable');
}
if (!metadataUpdateRejected) throw new Error('binding metadata update was not rejected');
console.log('cart_abandonment_binding_any_update_reject=OK');

let deleteBypassRejected = false;
try {
  await db.query(`
    delete from public.recovery_case_events
    where recovery_case_id = $1::uuid
      and event_role = 'cart_abandonment'
  `, [plan.rows[0].recovery_case_id]);
} catch (error) {
  deleteBypassRejected = String(error).includes('cart_abandonment_binding_immutable');
}
if (!deleteBypassRejected) throw new Error('binding delete bypass was not rejected');
console.log('cart_abandonment_binding_delete_bypass_reject=OK');

let caseMutationRejected = false;
try {
  await db.query(`
    update public.recovery_cases
    set product_name = 'Mutated Product'
    where id = $1::uuid
  `, [plan.rows[0].recovery_case_id]);
} catch (error) {
  caseMutationRejected = String(error).includes('cart_abandonment_binding_immutable');
}
if (!caseMutationRejected) throw new Error('case binding mutation was not rejected');
console.log('cart_abandonment_case_binding_immutable=OK');

let sourceMutationRejected = false;
try {
  await db.query(`
    update public.webhook_events
    set payload = jsonb_set(payload, '{data,offer,code}', '"mutated-offer"')
    where id = $1::uuid
  `, [eventId]);
} catch (error) {
  sourceMutationRejected = String(error).includes('cart_abandonment_source_immutable');
}
if (!sourceMutationRejected) throw new Error('canonical source event mutation was accepted');
console.log('cart_abandonment_source_event_immutable=OK');

const conflicting = structuredClone(payload);
conflicting.data.offer.code = 'offer-b';
const conflict = await db.query(
  'select * from public.admit_hotmart_cart_abandonment($1, $2::jsonb)',
  [payload.id, JSON.stringify(conflicting)],
);
if (conflict.rows[0]?.outcome !== 'semantic_conflict') {
  throw new Error('changed business tuple was not a semantic conflict');
}
const conflictCount = await db.query(`
  select count(*)::int as count
  from public.hotmart_cart_abandonment_semantic_conflicts
  where resolved_at is null
`);
if (conflictCount.rows[0].count !== 1) throw new Error('durable conflict evidence missing');
console.log('cart_abandonment_semantic_conflict=OK');
