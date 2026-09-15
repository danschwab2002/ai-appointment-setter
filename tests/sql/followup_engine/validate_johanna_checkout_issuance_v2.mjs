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

const catalog = await db.query(`
  select id, tenant_ref, funnel_ref, product_ref, landing_ref, offer_code,
         checkout_base_url, checkout_mode, default_for_inbound, status
  from public.checkout_offer_catalog
`);
if (catalog.rows.length !== 1
    || catalog.rows[0]?.product_ref !== 'F106691755G'
    || catalog.rows[0]?.landing_ref !== 'ads-a'
    || catalog.rows[0]?.offer_code !== 'bxjge6zq'
    || catalog.rows[0]?.default_for_inbound !== true
    || catalog.rows[0]?.status !== 'active') {
  throw new Error(`catalog authority diverged: ${JSON.stringify(catalog.rows)}`);
}

const catalogId = catalog.rows[0].id;

await db.exec(`
  insert into public.inbound_commercial_scope_versions (
    scope_key, version, status, tenant_key, chatwoot_account_id,
    chatwoot_inbox_id, external_product_id, offer_code,
    approved_by, approved_at, published_at
  ) values (
    'libre-de-ansiedad-inbound', 2, 'published', 'lancemos', 1, 9,
    'F106691755G', 'explicit-catalog-authority', 'schema-probe', now(), now()
  );
`);

const inbound = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)',
  ['libre-de-ansiedad-inbound', 2, 9101, '12025550123'],
)).rows[0];
if (inbound?.outcome !== 'created') {
  throw new Error(`inbound fixture was not admitted: ${JSON.stringify(inbound)}`);
}

const issuanceUlid = '01K5ABCDEFX2VYB4M6X9CDPTZR';
const prepared = (await db.query(`
  select * from public.reserve_chatwoot_checkout_issuance_v2(
    $1::uuid, '12025550123', 1, 9, 9101, '501', $2, clock_timestamp()
  )
`, [inbound.commercial_case_id, issuanceUlid])).rows[0];
const expectedSck = `hermes|v1|${issuanceUlid}`;
const expectedUrl = 'https://pay.hotmart.com/F106691755G'
  + `?off=bxjge6zq&checkoutMode=10&src=hermes&sck=hermes%7Cv1%7C${issuanceUlid}`;
if (prepared?.outcome !== 'reserved'
    || prepared?.source_kind !== 'inbound_request'
    || prepared?.sck_value !== expectedSck
    || prepared?.checkout_url_final !== expectedUrl) {
  throw new Error(`inbound issuance diverged: ${JSON.stringify(prepared)}`);
}
const replay = (await db.query(`
  select * from public.reserve_chatwoot_checkout_issuance_v2(
    $1::uuid, '12025550123', 1, 9, 9101, '501',
    '01K5ABCDEFX2VYB4M6X9CDPTS0', clock_timestamp()
  )
`, [inbound.commercial_case_id])).rows[0];
if (replay?.outcome !== 'reserved'
    || replay?.issuance_id !== prepared.issuance_id
    || replay?.issuance_ulid !== issuanceUlid
    || replay?.checkout_url_final !== expectedUrl) {
  throw new Error(`issuance replay diverged: ${JSON.stringify(replay)}`);
}

const authorized = (await db.query(`
  select * from public.authorize_chatwoot_checkout_issuance_v2(
    $1::uuid, '12025550123', 1, 9, 9101, '501', clock_timestamp()
  )
`, [prepared.issuance_id])).rows[0];
if (authorized?.outcome !== 'request_started'
    || authorized?.status !== 'request_started') {
  throw new Error(`issuance authorization diverged: ${JSON.stringify(authorized)}`);
}

const finalized = (await db.query(`
  select * from public.finalize_chatwoot_checkout_issuance_v2(
    $1::uuid, 'accepted_by_chatwoot', 7001, null, clock_timestamp()
  )
`, [prepared.issuance_id])).rows[0];
if (finalized?.outcome !== 'finalized'
    || finalized?.status !== 'accepted_by_chatwoot') {
  throw new Error(`issuance finalization diverged: ${JSON.stringify(finalized)}`);
}

const event = (await db.query(`
  insert into public.webhook_events (
    source, external_event_id, event_type, payload, processing_status
  ) values (
    'hotmart', 'checkout-issuance-purchase-1', 'PURCHASE_APPROVED',
    '{}'::jsonb, 'received'
  ) returning id
`)).rows[0];
const matched = (await db.query(`
  select * from public.correlate_hotmart_checkout_issuance_v2(
    $1::uuid, $2, clock_timestamp()
  )
`, [event.id, expectedSck])).rows[0];
if (matched?.outcome !== 'matched'
    || matched?.issuance_id !== prepared.issuance_id
    || matched?.purchase_intent_id !== prepared.purchase_intent_id) {
  throw new Error(`SCK correlation diverged: ${JSON.stringify(matched)}`);
}
const durable = (await db.query(`
  select issuance.status, issuance.original_sck, intent.lifecycle_state,
         offer.id as offer_catalog_id
  from public.checkout_link_issuances issuance
  join public.purchase_intents intent on intent.id = issuance.purchase_intent_id
  join public.checkout_offer_catalog offer on offer.id = issuance.offer_catalog_id
  where issuance.id = $1::uuid
`, [prepared.issuance_id])).rows[0];
if (durable?.status !== 'purchase_matched'
    || durable?.original_sck !== null
    || durable?.lifecycle_state !== 'purchased'
    || durable?.offer_catalog_id !== catalogId) {
  throw new Error(`durable association diverged: ${JSON.stringify(durable)}`);
}

const invalidEvent = (await db.query(`
  insert into public.webhook_events (
    source, external_event_id, event_type, payload, processing_status
  ) values ('hotmart', 'checkout-issuance-invalid-sck', 'PURCHASE_APPROVED',
            '{}'::jsonb, 'received') returning id
`)).rows[0];
const invalidSck = (await db.query(`
  select * from public.correlate_hotmart_checkout_issuance_v2(
    $1::uuid, 'hermes|v1|not-a-ulid', clock_timestamp()
  )
`, [invalidEvent.id])).rows[0];
const missingEvent = (await db.query(`
  insert into public.webhook_events (
    source, external_event_id, event_type, payload, processing_status
  ) values ('hotmart', 'checkout-issuance-missing-sck', 'PURCHASE_APPROVED',
            '{}'::jsonb, 'received') returning id
`)).rows[0];
const missingSck = (await db.query(`
  select * from public.correlate_hotmart_checkout_issuance_v2(
    $1::uuid, 'hermes|v1|01K5ABCDEFX2VYB4M6X9CDPTZZ', clock_timestamp()
  )
`, [missingEvent.id])).rows[0];
if (invalidSck?.outcome !== 'invalid_hermes_sck'
    || missingSck?.outcome !== 'not_found'
    || invalidSck?.issuance_id !== null
    || missingSck?.issuance_id !== null) {
  throw new Error(`invalid SCK fallback fence diverged: ${JSON.stringify({
    invalidSck, missingSck,
  })}`);
}

const malformedExternalId = 'checkout-issuance-malformed-wrapper-sck';
const malformedHermesSck = 'hermes|invalid';
const malformedHermesPayload = {
  id: malformedExternalId,
  creation_date: 1786147210000,
  event: 'PURCHASE_APPROVED',
  version: '2.0.0',
  data: {
    buyer: { email: 'schema@example.test', checkout_phone: '12025550123' },
    product: { id: 123, ucode: 'F106691755G' },
    purchase: {
      status: 'APPROVED',
      transaction: 'HPV2MALFORMED001',
      approved_date: 1786147205000,
      offer: { code: 'bxjge6zq' },
      origin: { sck: malformedHermesSck },
    },
  },
};
let malformedWrapperBlocked = false;
try {
  await db.query(`
    select * from public.admit_and_correlate_hotmart_checkout_issuance_v2(
      $1, $2::jsonb, $3, clock_timestamp()
    )
  `, [malformedExternalId, JSON.stringify(malformedHermesPayload), malformedHermesSck]);
} catch {
  malformedWrapperBlocked = true;
}
const malformedResidue = (await db.query(`
  select count(*)::integer as count
  from public.webhook_events
  where source = 'hotmart' and external_event_id = $1
`, [malformedExternalId])).rows[0]?.count;
if (!malformedWrapperBlocked || malformedResidue !== 0) {
  throw new Error(`malformed wrapper SCK was durably admitted: ${JSON.stringify({
    malformedWrapperBlocked, malformedResidue,
  })}`);
}

const precheckoutCase = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)',
  ['libre-de-ansiedad-inbound', 2, 9102, '12025550124'],
)).rows[0];

for (const [label, commercialCaseId, externalUserId] of [
  ['case', precheckoutCase.commercial_case_id, '12025550123'],
  ['identity', inbound.commercial_case_id, '12025550999'],
]) {
  const conflict = (await db.query(`
    select * from public.reserve_chatwoot_checkout_issuance_v2(
      $1::uuid, $2, 1, 9, 9101, '501',
      '01K5ABCDEFX2VYB4M6X9CDPTZZ', clock_timestamp()
    )
  `, [commercialCaseId, externalUserId])).rows[0];
  if (conflict?.outcome !== 'replay_conflict'
      || conflict?.issuance_id !== null
      || conflict?.checkout_url_final !== null
      || conflict?.sck_value !== null) {
    throw new Error(`${label} replay was accepted: ${JSON.stringify(conflict)}`);
  }
}

const crossScopeCase = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)',
  ['libre-de-ansiedad-inbound', 2, 9103, '12025550125'],
)).rows[0];
const foreignIntents = (await db.query(`
  insert into public.purchase_intents (
    tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
    normalized_phone, submitted_at, lifecycle_state,
    whatsapp_contact_authorized, provisional, provider_observed,
    activation_authorized
  ) values
    ('lancemos', 'foreign-funnel', 'foreign-a', 'F106691755G', 'foreign-offer-a',
     '12025550125', '2026-09-13T00:00:00Z', 'purchased', true, false, true, true),
    ('lancemos', 'foreign-funnel', 'foreign-b', 'F106691755G', 'foreign-offer-b',
     '12025550125', '2026-09-14T00:00:00Z', 'waiting_for_purchase', true, false, true, true)
  returning id
`)).rows;
const crossScopeReservation = (await db.query(`
  select * from public.reserve_chatwoot_checkout_issuance_v2(
    $1::uuid, '12025550125', 1, 9, 9103, '503',
    '01K5ABCDEFX2VYB4M6X9CDPTS2', clock_timestamp()
  )
`, [crossScopeCase.commercial_case_id])).rows[0];
const crossScopeIntent = (await db.query(`
  select intent.funnel_ref, intent.landing_ref, intent.offer_ref
  from public.purchase_intents intent
  where intent.id = $1::uuid
`, [crossScopeReservation.purchase_intent_id])).rows[0];
if (crossScopeReservation?.outcome !== 'reserved'
    || foreignIntents.some((intent) => intent.id === crossScopeReservation.purchase_intent_id)
    || crossScopeIntent?.funnel_ref !== 'psicologajohanna'
    || crossScopeIntent?.landing_ref !== 'ads-a'
    || crossScopeIntent?.offer_ref !== 'bxjge6zq') {
  throw new Error(`cross-scope intent leaked: ${JSON.stringify({
    crossScopeReservation, crossScopeIntent, foreignIntents,
  })}`);
}

const precheckoutIntent = (await db.query(`
  insert into public.purchase_intents (
    tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
    normalized_phone, submitted_at, lifecycle_state,
    whatsapp_contact_authorized, provisional, provider_observed,
    activation_authorized
  ) values (
    'lancemos', 'psicologajohanna', 'ads-a', 'F106691755G', 'bxjge6zq',
    '12025550124', clock_timestamp(), 'waiting_for_purchase',
    true, false, true, true
  ) returning id
`)).rows[0];
const precheckoutSubmission = (await db.query(`
  insert into public.precheckout_submissions (
    external_submission_id, contract_version, raw_payload, canonical_payload,
    provisional, provider_observed, activation_authorized
  ) values (
    'checkout-issuance-precheckout-1', '1.1.0',
    '{"data":{"attribution":{"sck":"meta|legacy|value"}}}'::jsonb,
    '{}'::jsonb,
    false, true, true
  ) returning id
`)).rows[0];
await db.query(`
  insert into public.purchase_intent_submissions (
    purchase_intent_id, submission_id, ordinal
  ) values ($1::uuid, $2::uuid, 1)
`, [precheckoutIntent.id, precheckoutSubmission.id]);
const precheckoutPrepared = (await db.query(`
  select * from public.reserve_chatwoot_checkout_issuance_v2(
    $1::uuid, '12025550124', 1, 9, 9102, '502',
    '01K5ABCDEFX2VYB4M6X9CDPTS1', clock_timestamp()
  )
`, [precheckoutCase.commercial_case_id])).rows[0];
const precheckoutDurable = (await db.query(`
  select source_kind, source_submission_id, original_sck,
         checkout_url_final, sck_value
  from public.checkout_link_issuances
  where id = $1::uuid
`, [precheckoutPrepared.issuance_id])).rows[0];
if (precheckoutPrepared?.outcome !== 'reserved'
    || precheckoutDurable?.source_kind !== 'precheckout_request'
    || precheckoutDurable?.source_submission_id !== precheckoutSubmission.id
    || precheckoutDurable?.original_sck !== 'meta|legacy|value'
    || precheckoutDurable?.checkout_url_final.includes('meta%7Clegacy')
    || precheckoutDurable?.sck_value !== 'hermes|v1|01K5ABCDEFX2VYB4M6X9CDPTS1') {
  throw new Error(`precheckout issuance diverged: ${JSON.stringify({
    prepared: precheckoutPrepared, durable: precheckoutDurable,
  })}`);
}

await db.exec('begin');
await db.query(`
  update public.purchase_intents
  set lifecycle_state = 'cancelled', updated_at = '2026-09-14T00:00:00Z'
  where id = $1::uuid
`, [precheckoutPrepared.purchase_intent_id]);
const cancelledAuthorization = (await db.query(`
  select * from public.authorize_chatwoot_checkout_issuance_v2(
    $1::uuid, '12025550124', 1, 9, 9102, '502',
    '2026-09-14T00:00:01Z'
  )
`, [precheckoutPrepared.issuance_id])).rows[0];
await db.exec('rollback');
if (cancelledAuthorization?.outcome !== 'blocked_intent'
    || cancelledAuthorization?.status !== 'reserved') {
  throw new Error(`cancelled intent was authorized: ${JSON.stringify(cancelledAuthorization)}`);
}

await db.query(`
  update public.checkout_offer_catalog
  set status = 'retired'
  where id = $1::uuid
`, [catalogId]);
const retiredOfferAuthorization = (await db.query(`
  select * from public.authorize_chatwoot_checkout_issuance_v2(
    $1::uuid, '12025550124', 1, 9, 9102, '502', clock_timestamp()
  )
`, [precheckoutPrepared.issuance_id])).rows[0];
if (retiredOfferAuthorization?.outcome !== 'request_started') {
  throw new Error(`reserved snapshot was invalidated: ${JSON.stringify(retiredOfferAuthorization)}`);
}
const unknown = (await db.query(`
  select * from public.finalize_chatwoot_checkout_issuance_v2(
    $1::uuid, 'delivery_unknown', null, 'chatwoot_timeout', clock_timestamp()
  )
`, [precheckoutPrepared.issuance_id])).rows[0];
const reconciled = (await db.query(`
  select * from public.finalize_chatwoot_checkout_issuance_v2(
    $1::uuid, 'accepted_by_chatwoot', 7002, null, clock_timestamp()
  )
`, [precheckoutPrepared.issuance_id])).rows[0];
if (unknown?.status !== 'delivery_unknown'
    || reconciled?.status !== 'accepted_by_chatwoot') {
  throw new Error(`delivery reconciliation diverged: ${JSON.stringify({ unknown, reconciled })}`);
}

await db.exec('set role service_role');
let directReadBlocked = false;
try {
  await db.query('select * from public.checkout_link_issuances');
} catch {
  directReadBlocked = true;
}
let directCorrelationBlocked = false;
try {
  await db.query(`
    select * from public.correlate_hotmart_checkout_issuance_v2(
      gen_random_uuid(), 'hermes|v1|01K5ABCDEFX2VYB4M6X9CDPTZR',
      clock_timestamp()
    )
  `);
} catch {
  directCorrelationBlocked = true;
}
await db.exec('reset role');
if (!directReadBlocked) throw new Error('service_role has direct issuance access');
if (!directCorrelationBlocked) {
  throw new Error('service_role can bypass payload-bound correlation wrapper');
}

await db.exec('set role anon');
let anonExecuteBlocked = false;
try {
  await db.query(`
    select * from public.reserve_chatwoot_checkout_issuance_v2(
      gen_random_uuid(), '12025550123', 1, 9, 10, '11',
      '01K5ABCDEFX2VYB4M6X9CDPTZR', clock_timestamp()
    )
  `);
} catch {
  anonExecuteBlocked = true;
}
await db.exec('reset role');
if (!anonExecuteBlocked) throw new Error('anon can execute issuance RPC');

const aclSql = readFileSync(join(root, 'scripts/supabase_acl_inventory.sql'), 'utf8');
const acl = await db.query(aclSql);
const badAcl = acl.rows.filter((row) => row.acl_status !== 'ok');
if (badAcl.length !== 0) {
  throw new Error(`ACL inventory failures: ${JSON.stringify(badAcl)}`);
}
const schemaSql = readFileSync(join(root, 'scripts/supabase_schema_inventory.sql'), 'utf8');
const schema = await db.query(schemaSql);
const v2 = schema.rows.find((row) => row.version === '20260914000100');
if (v2?.fingerprint_status !== 'fingerprint_present') {
  throw new Error(`V2 schema fingerprint mismatch: ${JSON.stringify(v2)}`);
}

console.log('JOHANNA_CHECKOUT_ISSUANCE_V2_SQL_OK');
await db.close();
