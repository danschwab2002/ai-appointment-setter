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
  order by landing_ref
`);
// 2026-09-22 (migration 20260922000100): the catalog mirrors the six published
// landing/offer pairs; ads-a keeps the only default.
const publishedPairs = await db.query(`
  select landing_ref, offer_ref
  from public.johanna_precheckout_landing_offers
  order by landing_ref
`);
const defaults = catalog.rows.filter((row) => row.default_for_inbound);
if (catalog.rows.length !== 6
    || publishedPairs.rows.length !== 6
    || defaults.length !== 1
    || defaults[0]?.landing_ref !== 'ads-a'
    || defaults[0]?.offer_code !== 'bxjge6zq'
    || catalog.rows.some((row) => row.product_ref !== 'F106691755G'
        || row.status !== 'active'
        || row.checkout_mode !== 10
        || row.checkout_base_url !== 'https://pay.hotmart.com/F106691755G')
    || catalog.rows.some((row, index) => row.landing_ref !== publishedPairs.rows[index]?.landing_ref
        || row.offer_code !== publishedPairs.rows[index]?.offer_ref)) {
  throw new Error(`catalog authority diverged: ${JSON.stringify(catalog.rows)}`);
}

const catalogId = defaults[0].id;

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
// 2026-10-05 (migration 20261005000100): the marker is separated with "~", like
// the ad's sck, and travels literally in the URL ("~" is unreserved).
const expectedSck = `hermes~v1~${issuanceUlid}`;
const expectedUrl = 'https://pay.hotmart.com/F106691755G'
  + `?off=bxjge6zq&checkoutMode=10&src=hermes&sck=hermes~v1~${issuanceUlid}`;
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
    // The ad's own "|" (an old lineage) is still encoded; the marker's "~" is not.
    || !precheckoutDurable?.checkout_url_final.includes(
         'sck=meta%7Clegacy%7Cvalue~hermes~v1~01K5ABCDEFX2VYB4M6X9CDPTS1')
    || precheckoutDurable?.sck_value
       !== 'meta|legacy|value~hermes~v1~01K5ABCDEFX2VYB4M6X9CDPTS1') {
  throw new Error(`precheckout issuance diverged: ${JSON.stringify({
    prepared: precheckoutPrepared, durable: precheckoutDurable,
  })}`);
}

// 2026-09-22 (migration 20260922000100): the link carries the offer the lead saw.
const catalogByOffer = new Map(catalog.rows.map((row) => [row.offer_code, row.id]));
const resolutionOf = async (issuanceId) => (await db.query(`
  select offer_resolution, lead_offer_code, offer_catalog_id, purchase_intent_id,
         source_kind, checkout_url_final
  from public.checkout_link_issuances
  where id = $1::uuid
`, [issuanceId])).rows[0];
const offerUrl = (offer, ulid, originalSck = null, fbclid = null) => {
  // The marker goes with "~" (20261005000100); only an ad's own "|" is encoded.
  const sck = originalSck ? `${originalSck}~hermes~v1~${ulid}` : `hermes~v1~${ulid}`;
  const base = 'https://pay.hotmart.com/F106691755G'
    + `?off=${offer}&checkoutMode=10&src=hermes&sck=${sck.split('|').join('%7C')}`;
  return fbclid ? `${base}&fbclid=${fbclid}` : base;
};
const insertIntent = async (landing, offer, phone, state, observed, submittedAt) => (await db.query(`
  insert into public.purchase_intents (
    tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
    normalized_phone, submitted_at, lifecycle_state,
    whatsapp_contact_authorized, provisional, provider_observed,
    activation_authorized
  ) values (
    'lancemos', 'psicologajohanna', $1, 'F106691755G', $2,
    $3, $6::timestamptz, $4, true, false, $5, true
  ) returning id
`, [landing, offer, phone, state, observed, submittedAt])).rows[0];
const attachSubmission = async (intentId, externalId, landing, attribution = null) => {
  const submission = (await db.query(`
    insert into public.precheckout_submissions (
      external_submission_id, contract_version, raw_payload, canonical_payload,
      provisional, provider_observed, activation_authorized
    ) values (
      $1, '1.1.0', $2::jsonb, '{}'::jsonb, false, true, true
    ) returning id
  `, [externalId, JSON.stringify({
    data: { attribution: attribution ?? { sck: `meta|${landing}|value` } },
  })])).rows[0];
  await db.query(`
    insert into public.purchase_intent_submissions (
      purchase_intent_id, submission_id, ordinal
    ) values ($1::uuid, $2::uuid, 1)
  `, [intentId, submission.id]);
  return submission;
};

// No intent at all (the inbound case above): the default, and the row says so.
const inboundResolution = await resolutionOf(prepared.issuance_id);
if (inboundResolution?.offer_resolution !== 'default_no_intent'
    || inboundResolution?.lead_offer_code !== null
    || inboundResolution?.offer_catalog_id !== catalogId) {
  throw new Error(`inbound resolution diverged: ${JSON.stringify(inboundResolution)}`);
}

// A lead that came through ads-b gets the ads-b offer, never ads-a.
const adsBCase = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)',
  ['libre-de-ansiedad-inbound', 2, 9111, '12025550131'],
)).rows[0];
const adsBIntent = await insertIntent(
  'ads-b', 'mgbgpp19', '12025550131', 'waiting_for_purchase', true, '2026-09-20T10:00:00Z',
);
const adsBSubmission = await attachSubmission(adsBIntent.id, 'checkout-offer-ads-b-1', 'ads-b');
const adsBUlid = '01K5ABCDEFX2VYB4M6X9CDPTA1';
const adsBPrepared = (await db.query(`
  select * from public.reserve_chatwoot_checkout_issuance_v2(
    $1::uuid, '12025550131', 1, 9, 9111, '511', $2, clock_timestamp()
  )
`, [adsBCase.commercial_case_id, adsBUlid])).rows[0];
const adsBDurable = await resolutionOf(adsBPrepared?.issuance_id);
if (adsBPrepared?.outcome !== 'reserved'
    || adsBPrepared?.purchase_intent_id !== adsBIntent.id
    || adsBPrepared?.source_kind !== 'precheckout_request'
    || adsBPrepared?.checkout_url_final !== offerUrl('mgbgpp19', adsBUlid, 'meta|ads-b|value')
    || adsBDurable?.offer_resolution !== 'lead_intent'
    || adsBDurable?.lead_offer_code !== 'mgbgpp19'
    || adsBDurable?.offer_catalog_id !== catalogByOffer.get('mgbgpp19')
    || adsBDurable?.source_kind !== 'precheckout_request') {
  throw new Error(`ads-b issuance diverged: ${JSON.stringify({
    prepared: adsBPrepared, durable: adsBDurable, submission: adsBSubmission,
  })}`);
}
// The replay of the same trigger keeps the ads-b URL.
const adsBReplay = (await db.query(`
  select * from public.reserve_chatwoot_checkout_issuance_v2(
    $1::uuid, '12025550131', 1, 9, 9111, '511', '01K5ABCDEFX2VYB4M6X9CDPTA9', clock_timestamp()
  )
`, [adsBCase.commercial_case_id])).rows[0];
if (adsBReplay?.issuance_id !== adsBPrepared.issuance_id
    || adsBReplay?.checkout_url_final !== offerUrl('mgbgpp19', adsBUlid, 'meta|ads-b|value')) {
  throw new Error(`ads-b replay diverged: ${JSON.stringify(adsBReplay)}`);
}

// A real precheckout intent wins over a newer intent fabricated for an inbound link.
const orgBCase = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)',
  ['libre-de-ansiedad-inbound', 2, 9112, '12025550132'],
)).rows[0];
await insertIntent(
  'ads-a', 'bxjge6zq', '12025550132', 'waiting_for_purchase', false, '2026-09-21T10:00:00Z',
);
const orgBIntent = await insertIntent(
  'org-b', 'ecyu87q0', '12025550132', 'waiting_for_purchase', true, '2026-09-19T10:00:00Z',
);
await attachSubmission(orgBIntent.id, 'checkout-offer-org-b-1', 'org-b');
const orgBUlid = '01K5ABCDEFX2VYB4M6X9CDPTA2';
const orgBPrepared = (await db.query(`
  select * from public.reserve_chatwoot_checkout_issuance_v2(
    $1::uuid, '12025550132', 1, 9, 9112, '512', $2, clock_timestamp()
  )
`, [orgBCase.commercial_case_id, orgBUlid])).rows[0];
const orgBDurable = await resolutionOf(orgBPrepared?.issuance_id);
if (orgBPrepared?.outcome !== 'reserved'
    || orgBPrepared?.purchase_intent_id !== orgBIntent.id
    || orgBPrepared?.checkout_url_final !== offerUrl('ecyu87q0', orgBUlid, 'meta|org-b|value')
    || orgBDurable?.offer_resolution !== 'lead_intent'
    || orgBDurable?.offer_catalog_id !== catalogByOffer.get('ecyu87q0')) {
  throw new Error(`org-b precedence diverged: ${JSON.stringify({
    prepared: orgBPrepared, durable: orgBDurable,
  })}`);
}

// An intent whose offer is not in the catalog falls back to the default and says so.
const unknownCase = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)',
  ['libre-de-ansiedad-inbound', 2, 9113, '12025550133'],
)).rows[0];
const unknownIntent = await insertIntent(
  'ads-z', 'zzzzzzzz', '12025550133', 'waiting_for_purchase', true, '2026-09-20T10:00:00Z',
);
await attachSubmission(unknownIntent.id, 'checkout-offer-ads-z-1', 'ads-z');
const unknownUlid = '01K5ABCDEFX2VYB4M6X9CDPTA3';
const unknownPrepared = (await db.query(`
  select * from public.reserve_chatwoot_checkout_issuance_v2(
    $1::uuid, '12025550133', 1, 9, 9113, '513', $2, clock_timestamp()
  )
`, [unknownCase.commercial_case_id, unknownUlid])).rows[0];
const unknownDurable = await resolutionOf(unknownPrepared?.issuance_id);
const unknownIntentBehind = (await db.query(`
  select landing_ref, offer_ref from public.purchase_intents where id = $1::uuid
`, [unknownPrepared?.purchase_intent_id])).rows[0];
if (unknownPrepared?.outcome !== 'reserved'
    || unknownPrepared?.purchase_intent_id === unknownIntent.id
    || unknownPrepared?.checkout_url_final !== offerUrl('bxjge6zq', unknownUlid, 'meta|ads-z|value')
    || unknownDurable?.offer_resolution !== 'default_offer_not_in_catalog'
    || unknownDurable?.lead_offer_code !== 'zzzzzzzz'
    || unknownDurable?.offer_catalog_id !== catalogId
    || unknownIntentBehind?.landing_ref !== 'ads-a'
    || unknownIntentBehind?.offer_ref !== 'bxjge6zq') {
  throw new Error(`unknown-offer fallback diverged: ${JSON.stringify({
    prepared: unknownPrepared, durable: unknownDurable, intent: unknownIntentBehind,
  })}`);
}

// A purchase through any offer of the product blocks a new link.
const boughtCase = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)',
  ['libre-de-ansiedad-inbound', 2, 9114, '12025550134'],
)).rows[0];
await insertIntent(
  'ads-c', 's1qfxm7m', '12025550134', 'purchased', true, '2026-09-18T10:00:00Z',
);
await insertIntent(
  'ads-a', 'bxjge6zq', '12025550134', 'waiting_for_purchase', true, '2026-09-21T10:00:00Z',
);
const boughtPrepared = (await db.query(`
  select * from public.reserve_chatwoot_checkout_issuance_v2(
    $1::uuid, '12025550134', 1, 9, 9114, '514', '01K5ABCDEFX2VYB4M6X9CDPTA4', clock_timestamp()
  )
`, [boughtCase.commercial_case_id])).rows[0];
if (boughtPrepared?.outcome !== 'purchase_already_approved'
    || boughtPrepared?.issuance_id !== null
    || boughtPrepared?.checkout_url_final !== null) {
  throw new Error(`known purchase through another offer was not blocked: ${JSON.stringify(boughtPrepared)}`);
}

// 2026-09-22 (migration 20260922000200): the link carries the lead's fbclid and
// keeps the ad's sck in front of the hermes marker.
const attributionOf = async (issuanceId) => (await db.query(`
  select attribution_resolution, dropped_unsafe_fields, sck_value, checkout_url_final
  from public.checkout_link_issuances
  where id = $1::uuid
`, [issuanceId])).rows[0];

const issueFor = async (phone, conv, trigger, ulid) => {
  const admitted = (await db.query(
    'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)',
    ['libre-de-ansiedad-inbound', 2, conv, phone],
  )).rows[0];
  return (await db.query(`
    select * from public.reserve_chatwoot_checkout_issuance_v2(
      $1::uuid, $2, 1, 9, $3, $4, $5, clock_timestamp()
    )
  `, [admitted.commercial_case_id, phone, conv, trigger, ulid])).rows[0];
};

// Real Johanna shape, measured 2026-09-22: fb.paid.<18 digits> plus a fbclid.
const fullPhone = '12025550140';
const fullUlid = '01K5ABCDEFX2VYB4M6X9CDPTB1';
const fullSck = 'fb.paid.120210000000000000';
const fullFbclid = 'IwAR0abcDEF_ghi-JKL.mno123';
const fullIntent = await insertIntent(
  'ads-b', 'mgbgpp19', fullPhone, 'waiting_for_purchase', true, '2026-09-22T10:00:00Z',
);
await attachSubmission(fullIntent.id, 'attribution-full-1', 'ads-b', {
  sck: fullSck, fbclid: fullFbclid,
});
const fullPrepared = await issueFor(fullPhone, 9120, '520', fullUlid);
const fullDurable = await attributionOf(fullPrepared.issuance_id);
if (fullPrepared?.outcome !== 'reserved'
    || fullDurable?.attribution_resolution !== 'full'
    || fullDurable?.dropped_unsafe_fields !== null
    || fullDurable?.sck_value !== `${fullSck}~hermes~v1~${fullUlid}`
    || fullDurable?.checkout_url_final !== offerUrl('mgbgpp19', fullUlid, fullSck, fullFbclid)) {
  throw new Error(`full attribution diverged: ${JSON.stringify({
    prepared: fullPrepared, durable: fullDurable,
  })}`);
}

// A lead with a fbclid but no ad sck: only the marker travels in the sck.
const fbOnlyPhone = '12025550141';
const fbOnlyUlid = '01K5ABCDEFX2VYB4M6X9CDPTB2';
const fbOnlyIntent = await insertIntent(
  'ads-b', 'mgbgpp19', fbOnlyPhone, 'waiting_for_purchase', true, '2026-09-22T10:00:00Z',
);
await attachSubmission(fbOnlyIntent.id, 'attribution-fbclid-1', 'ads-b', {
  fbclid: 'IwAR1onlyclickid',
});
const fbOnlyPrepared = await issueFor(fbOnlyPhone, 9121, '521', fbOnlyUlid);
const fbOnlyDurable = await attributionOf(fbOnlyPrepared.issuance_id);
if (fbOnlyDurable?.attribution_resolution !== 'fbclid_only'
    || fbOnlyDurable?.sck_value !== `hermes~v1~${fbOnlyUlid}`
    || fbOnlyDurable?.checkout_url_final
       !== offerUrl('mgbgpp19', fbOnlyUlid, null, 'IwAR1onlyclickid')) {
  throw new Error(`fbclid-only attribution diverged: ${JSON.stringify(fbOnlyDurable)}`);
}

// An ad sck that would break the query string is dropped, and the row says so.
const unsafePhone = '12025550142';
const unsafeUlid = '01K5ABCDEFX2VYB4M6X9CDPTB3';
const unsafeIntent = await insertIntent(
  'ads-b', 'mgbgpp19', unsafePhone, 'waiting_for_purchase', true, '2026-09-22T10:00:00Z',
);
await attachSubmission(unsafeIntent.id, 'attribution-unsafe-1', 'ads-b', {
  sck: 'Instagram_Reels_Publico Guardado_L2.26 | Inmersion',
  fbclid: 'has spaces & ampersand',
});
const unsafePrepared = await issueFor(unsafePhone, 9122, '522', unsafeUlid);
const unsafeDurable = await attributionOf(unsafePrepared.issuance_id);
if (unsafeDurable?.attribution_resolution !== 'marker_only'
    || unsafeDurable?.dropped_unsafe_fields !== 'sck,fbclid'
    || unsafeDurable?.sck_value !== `hermes~v1~${unsafeUlid}`
    || unsafeDurable?.checkout_url_final !== offerUrl('mgbgpp19', unsafeUlid)
    || unsafeDurable?.checkout_url_final.includes(' ')) {
  throw new Error(`unsafe attribution was not dropped: ${JSON.stringify(unsafeDurable)}`);
}

// The purchase webhook still finds the issuance when the ad's sck travels in
// front of the marker: this is what lets the sale be counted as recovered.
const compositeEvent = (await db.query(`
  insert into public.webhook_events (
    source, external_event_id, event_type, payload, processing_status
  ) values (
    'hotmart', 'checkout-attribution-purchase-1', 'PURCHASE_APPROVED',
    '{}'::jsonb, 'received'
  ) returning id
`)).rows[0];
const compositeMatch = (await db.query(`
  select * from public.correlate_hotmart_checkout_issuance_v2(
    $1::uuid, $2, clock_timestamp()
  )
`, [compositeEvent.id, `${fullSck}~hermes~v1~${fullUlid}`])).rows[0];
if (compositeMatch?.outcome !== 'matched'
    || compositeMatch?.issuance_id !== fullPrepared.issuance_id) {
  throw new Error(`composite sck correlation diverged: ${JSON.stringify(compositeMatch)}`);
}

// A sck that does not end in a hermes marker is still rejected up front.
const strayEvent = (await db.query(`
  insert into public.webhook_events (
    source, external_event_id, event_type, payload, processing_status
  ) values (
    'hotmart', 'checkout-attribution-purchase-2', 'PURCHASE_APPROVED',
    '{}'::jsonb, 'received'
  ) returning id
`)).rows[0];
// The two marker forms are never mixed, and nothing may follow the ULID.
for (const stray of [
  `hermes|v1|${fullUlid}|tail`, 'fb.paid.120210000000000000', 'hermes|v1|nope',
  `hermes~v1~${fullUlid}~tail`, 'hermes~v1~nope', `hermes|v1~${fullUlid}`,
  `hermes~v1|${fullUlid}`, `${fullSck}|hermes~v1~${fullUlid}`,
  `${fullSck}~hermes|v1|${fullUlid}`, `~hermes~v1~${fullUlid}`,
]) {
  const rejected = (await db.query(`
    select * from public.correlate_hotmart_checkout_issuance_v2(
      $1::uuid, $2, clock_timestamp()
    )
  `, [strayEvent.id, stray])).rows[0];
  if (rejected?.outcome !== 'invalid_hermes_sck') {
    throw new Error(`stray sck was not rejected: ${JSON.stringify({ stray, rejected })}`);
  }
}

// 2026-09-25 (migration 20260925000100): the Lancemos core v1.10.0 composes the
// ad's sck as utm_source~utm_term~utm_content~utm_medium~utm_campaign (E10/E13).
// "~" is unreserved, so it travels literally in the URL while the marker's "|"
// is still encoded. The cases are the core's own expected outputs, captured
// from its verifier into the fixture; the linages with "|" and "." above keep
// passing because the reader accepts every separator ever emitted.
const tildeFixture = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/lancemos_core_sck_tilde_20260925.json'), 'utf8',
));
if (!tildeFixture.casos.length || tildeFixture.casos.some((caso) => !caso.sck.includes('~'))) {
  throw new Error('tilde fixture has no tilde cases');
}
let tildeSeq = 0;
for (const caso of tildeFixture.casos) {
  tildeSeq += 1;
  const tildePhone = `120255501${String(50 + tildeSeq).padStart(2, '0')}`;
  const tildeUlid = `01K5ABCDEFX2VYB4M6X9CDPTC${tildeSeq}`;
  const tildeIntent = await insertIntent(
    'ads-b', 'mgbgpp19', tildePhone, 'waiting_for_purchase', true, '2026-09-25T10:00:00Z',
  );
  await attachSubmission(tildeIntent.id, `attribution-tilde-${tildeSeq}`, 'ads-b', {
    sck: caso.sck, fbclid: 'IwAR2tildeclickid',
  });
  const tildePrepared = await issueFor(tildePhone, 9130 + tildeSeq, String(530 + tildeSeq), tildeUlid);
  const tildeDurable = await attributionOf(tildePrepared.issuance_id);
  if (tildePrepared?.outcome !== 'reserved'
      || tildeDurable?.attribution_resolution !== 'full'
      || tildeDurable?.dropped_unsafe_fields !== null
      || tildeDurable?.sck_value !== `${caso.sck}~hermes~v1~${tildeUlid}`
      || tildeDurable?.checkout_url_final
         !== offerUrl('mgbgpp19', tildeUlid, caso.sck, 'IwAR2tildeclickid')
      || !tildeDurable?.checkout_url_final.includes(`&sck=${caso.sck}~hermes~v1~`)) {
    throw new Error(`tilde sck was not preserved (${caso.nombre}): ${JSON.stringify({
      prepared: tildePrepared, durable: tildeDurable,
    })}`);
  }
  // And the purchase webhook still finds the issuance behind the "~" prefix.
  const tildeEvent = (await db.query(`
    insert into public.webhook_events (
      source, external_event_id, event_type, payload, processing_status
    ) values (
      'hotmart', $1, 'PURCHASE_APPROVED', '{}'::jsonb, 'received'
    ) returning id
  `, [`checkout-attribution-tilde-${tildeSeq}`])).rows[0];
  const tildeMatch = (await db.query(`
    select * from public.correlate_hotmart_checkout_issuance_v2(
      $1::uuid, $2, clock_timestamp()
    )
  `, [tildeEvent.id, `${caso.sck}~hermes~v1~${tildeUlid}`])).rows[0];
  if (tildeMatch?.outcome !== 'matched' || tildeMatch?.issuance_id !== tildePrepared.issuance_id) {
    throw new Error(`tilde sck correlation diverged (${caso.nombre}): ${JSON.stringify(tildeMatch)}`);
  }
}

// Widening the alphabet to "~" lets nothing else through: the unsafe case above
// still drops, and an accent inside a "~" sck drops too.
const tildeUnsafePhone = '12025550159';
const tildeUnsafeUlid = '01K5ABCDEFX2VYB4M6X9CDPTC9';
const tildeUnsafeIntent = await insertIntent(
  'ads-b', 'mgbgpp19', tildeUnsafePhone, 'waiting_for_purchase', true, '2026-09-25T10:00:00Z',
);
await attachSubmission(tildeUnsafeIntent.id, 'attribution-tilde-unsafe', 'ads-b', {
  sck: 'meta-ads~~~~Niños 23/09',
});
const tildeUnsafePrepared = await issueFor(tildeUnsafePhone, 9139, '539', tildeUnsafeUlid);
const tildeUnsafeDurable = await attributionOf(tildeUnsafePrepared.issuance_id);
if (tildeUnsafeDurable?.attribution_resolution !== 'marker_only'
    || tildeUnsafeDurable?.dropped_unsafe_fields !== 'sck'
    || tildeUnsafeDurable?.sck_value !== `hermes~v1~${tildeUnsafeUlid}`) {
  throw new Error(`unsafe tilde sck was not dropped: ${JSON.stringify(tildeUnsafeDurable)}`);
}

// 2026-09-27 (migration 20260927000200): the Lancemos core v1.13.0 appends
// utm_id (Meta's 18-digit campaign id) as a sixth field, so an ad's sck can
// reach ~196 characters (longest measured on Hotmart sales 177, plus "~" and
// the id). The reserve guard's cap rises from 200 to 255. The first two cases
// are the core's output observed in production on draninagarza; the 196 one is
// the measured maximum brought to six fields; 255 and 256 pin the new edge.
const utmIdFixture = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/lancemos_core_sck_utm_id_20260927.json'), 'utf8',
));
if (utmIdFixture.casos.length !== 2
    || utmIdFixture.casos[0].sck.split('~').length !== 6
    || !/^[0-9]{18}$/.test(utmIdFixture.casos[0].sck.split('~')[5])
    || utmIdFixture.sintetico_196.sck.length !== 196) {
  throw new Error('utm_id fixture does not have the expected shape');
}
const lengthCases = [
  ...utmIdFixture.casos.map((caso) => ({ nombre: caso.nombre, sck: caso.sck, preserved: true })),
  { nombre: 'el maximo medido (177) mas ~ y el id: 196', sck: utmIdFixture.sintetico_196.sck, preserved: true },
  { nombre: 'exactamente 255 se preserva', sck: `${'a'.repeat(236)}~120210000000000001`, preserved: true },
  { nombre: '256 se descarta (como 201 antes)', sck: `${'a'.repeat(237)}~120210000000000001`, preserved: false },
];
if (lengthCases[3].sck.length !== 255 || lengthCases[4].sck.length !== 256) {
  throw new Error('length edge cases are not 255/256');
}
let lengthSeq = 0;
for (const caso of lengthCases) {
  lengthSeq += 1;
  const lengthPhone = `1202555031${lengthSeq}`;
  const lengthUlid = `01K5ABCDEFX2VYB4M6X9CDPTD${lengthSeq}`;
  const lengthIntent = await insertIntent(
    'ads-b', 'mgbgpp19', lengthPhone, 'waiting_for_purchase', true, '2026-09-27T10:00:00Z',
  );
  await attachSubmission(lengthIntent.id, `attribution-length-${lengthSeq}`, 'ads-b', {
    sck: caso.sck, fbclid: 'IwAR2lengthclickid',
  });
  const lengthPrepared = await issueFor(lengthPhone, 9180 + lengthSeq, String(580 + lengthSeq), lengthUlid);
  const lengthDurable = await attributionOf(lengthPrepared.issuance_id);
  if (caso.preserved) {
    if (lengthPrepared?.outcome !== 'reserved'
        || lengthDurable?.attribution_resolution !== 'full'
        || lengthDurable?.dropped_unsafe_fields !== null
        || lengthDurable?.sck_value !== `${caso.sck}~hermes~v1~${lengthUlid}`
        || lengthDurable?.checkout_url_final
           !== offerUrl('mgbgpp19', lengthUlid, caso.sck, 'IwAR2lengthclickid')) {
      throw new Error(`long sck was not preserved (${caso.nombre}): ${JSON.stringify({
        prepared: lengthPrepared, durable: lengthDurable,
      })}`);
    }
    // And the purchase webhook still finds the issuance behind the long prefix.
    const lengthEvent = (await db.query(`
      insert into public.webhook_events (
        source, external_event_id, event_type, payload, processing_status
      ) values (
        'hotmart', $1, 'PURCHASE_APPROVED', '{}'::jsonb, 'received'
      ) returning id
    `, [`checkout-attribution-length-${lengthSeq}`])).rows[0];
    const lengthMatch = (await db.query(`
      select * from public.correlate_hotmart_checkout_issuance_v2(
        $1::uuid, $2, clock_timestamp()
      )
    `, [lengthEvent.id, `${caso.sck}~hermes~v1~${lengthUlid}`])).rows[0];
    if (lengthMatch?.outcome !== 'matched' || lengthMatch?.issuance_id !== lengthPrepared.issuance_id) {
      throw new Error(`long sck correlation diverged (${caso.nombre}): ${JSON.stringify(lengthMatch)}`);
    }
  } else if (lengthPrepared?.outcome !== 'reserved'
      || lengthDurable?.attribution_resolution !== 'fbclid_only'
      || lengthDurable?.dropped_unsafe_fields !== 'sck'
      || lengthDurable?.sck_value !== `hermes~v1~${lengthUlid}`) {
    throw new Error(`over-long sck was not dropped (${caso.nombre}): ${JSON.stringify(lengthDurable)}`);
  }
}

// 2026-10-05 (migration 20261005000100): the purchase admission accepts what the
// recognizer accepts. Until now it took only the marker alone, so a purchase made
// with a link that carried the ad's sck raised 22023 and the bridge answered 503
// to Hotmart; no test had ever called it with a valid sck. The ad's sck below is
// one Hotmart really returned on a Johanna purchase (fixture of 2026-10-05).
const shapesFixture = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/hotmart_purchase_sck_shapes_20261005.json'), 'utf8',
));
const realAdSck = shapesFixture.compras.find((compra) => compra.sck?.includes('~'))?.sck;
const realLegacyMarkers = [
  shapesFixture.compras.find((compra) => compra.sck?.startsWith('hermes|'))?.sck,
  shapesFixture.emision_att1_20261005.sck_value,
];
if (!realAdSck || realAdSck.includes('hermes') || realLegacyMarkers.some((sck) => !sck)) {
  throw new Error('sck shapes fixture does not have the expected shapes');
}
const purchasePayload = (externalId, transaction, sck) => ({
  id: externalId,
  creation_date: 1786147210000,
  event: 'PURCHASE_APPROVED',
  version: '2.0.0',
  data: {
    buyer: { email: 'schema@example.test', checkout_phone: '12025550123' },
    product: { id: 123, ucode: 'F106691755G' },
    purchase: {
      status: 'APPROVED',
      transaction,
      approved_date: 1786147205000,
      offer: { code: 'mgbgpp19' },
      origin: { sck },
    },
  },
});
const admitPurchase = async (externalId, transaction, sck) => (await db.query(`
  select * from public.admit_and_correlate_hotmart_checkout_issuance_v2(
    $1, $2::jsonb, $3, clock_timestamp()
  )
`, [externalId, JSON.stringify(purchasePayload(externalId, transaction, sck)), sck])).rows[0];

// The new marker alone ("hermes~v1~<ulid>"), through the admission.
const tildeOnlyAdmission = await admitPurchase(
  'checkout-tilde-marker-purchase', 'HPTILDEMARKER001', `hermes~v1~${fbOnlyUlid}`,
);
if (tildeOnlyAdmission?.admission_outcome !== 'inserted'
    || tildeOnlyAdmission?.correlation_outcome !== 'matched'
    || tildeOnlyAdmission?.issuance_id !== fbOnlyPrepared.issuance_id) {
  throw new Error(`tilde marker purchase was not admitted: ${JSON.stringify(tildeOnlyAdmission)}`);
}

// A real ad's sck in front of the new marker: written with "~" and literal in the
// URL, and the purchase that brings it back is admitted and matched.
const realPhone = '12025550171';
const realUlid = '01K5ABCDEFX2VYB4M6X9CDPTE1';
const realIntent = await insertIntent(
  'ads-b', 'mgbgpp19', realPhone, 'waiting_for_purchase', true, '2026-10-05T10:00:00Z',
);
await attachSubmission(realIntent.id, 'attribution-real-tilde', 'ads-b', {
  sck: realAdSck, fbclid: 'IwAR3realclickid',
});
const realPrepared = await issueFor(realPhone, 9171, '571', realUlid);
const realDurable = await attributionOf(realPrepared.issuance_id);
if (realPrepared?.outcome !== 'reserved'
    || realDurable?.attribution_resolution !== 'full'
    || realDurable?.sck_value !== `${realAdSck}~hermes~v1~${realUlid}`
    || realDurable?.checkout_url_final
       !== offerUrl('mgbgpp19', realUlid, realAdSck, 'IwAR3realclickid')
    || !realDurable?.checkout_url_final.includes(`&sck=${realAdSck}~hermes~v1~${realUlid}&`)
    || realDurable?.checkout_url_final.includes('%7C')) {
  throw new Error(`real ad sck with the tilde marker diverged: ${JSON.stringify(realDurable)}`);
}
const realAdmission = await admitPurchase(
  'checkout-tilde-composite-purchase', 'HPTILDECOMPOSITE1', realDurable.sck_value,
);
if (realAdmission?.admission_outcome !== 'inserted'
    || realAdmission?.correlation_outcome !== 'matched'
    || realAdmission?.issuance_id !== realPrepared.issuance_id) {
  throw new Error(`composite tilde purchase was not admitted: ${JSON.stringify(realAdmission)}`);
}

// A link reserved before 20261005000100 keeps its "|": one is inserted as it was
// written then (the table has no insert trigger), with the ad's sck in front.
// Both CHECKs keep accepting it, and its purchase, which used to raise 22023,
// is admitted and matched.
const legacyPhone = '12025550172';
const legacyUlid = '01K5ABCDEFX2VYB4M6X9CDPTE3';
const legacyIntent = await insertIntent(
  'ads-b', 'mgbgpp19', legacyPhone, 'waiting_for_purchase', true, '2026-09-26T10:00:00Z',
);
await attachSubmission(legacyIntent.id, 'attribution-legacy-bar', 'ads-b', {
  sck: realAdSck, fbclid: 'IwAR3legacyclickid',
});
const legacyTemplate = await issueFor(legacyPhone, 9172, '572', '01K5ABCDEFX2VYB4M6X9CDPTE2');
const insertSibling = (templateId, ulid, trigger, sck, url) => db.query(`
  insert into public.checkout_link_issuances (
    issuance_ulid, commercial_case_id, purchase_intent_id, contact_id,
    channel_identity_id, offer_catalog_id, chatwoot_account_id, chatwoot_inbox_id,
    chatwoot_conversation_id, trigger_external_message_id, source_kind,
    source_submission_id, original_sck, source_value, sck_format_version,
    sck_value, checkout_url_final, offer_resolution, lead_offer_code,
    attribution_resolution, dropped_unsafe_fields
  )
  select $2, commercial_case_id, purchase_intent_id, contact_id,
         channel_identity_id, offer_catalog_id, chatwoot_account_id, chatwoot_inbox_id,
         chatwoot_conversation_id, $3, source_kind,
         source_submission_id, original_sck, source_value, sck_format_version,
         $4, $5, offer_resolution, lead_offer_code,
         attribution_resolution, dropped_unsafe_fields
  from public.checkout_link_issuances
  where id = $1::uuid
  returning id
`, [templateId, ulid, trigger, sck, url]);
const legacySck = `${realAdSck}|hermes|v1|${legacyUlid}`;
const legacyRow = (await insertSibling(
  legacyTemplate.issuance_id, legacyUlid, '573', legacySck,
  'https://pay.hotmart.com/F106691755G?off=mgbgpp19&checkoutMode=10&src=hermes'
    + `&sck=${realAdSck}%7Chermes%7Cv1%7C${legacyUlid}&fbclid=IwAR3legacyclickid`,
)).rows[0];
const legacyAdmission = await admitPurchase(
  'checkout-legacy-composite-purchase', 'HPLEGACYCOMPOSITE', legacySck,
);
if (legacyAdmission?.admission_outcome !== 'inserted'
    || legacyAdmission?.correlation_outcome !== 'matched'
    || legacyAdmission?.issuance_id !== legacyRow.id) {
  throw new Error(`legacy composite purchase was not admitted: ${JSON.stringify(legacyAdmission)}`);
}

// The CHECKs refuse what the reserve never writes, each one on its own: a mixed
// marker with a valid URL can only be refused by the sck CHECK, and a valid
// marker with a raw "|" in the URL only by the URL CHECK.
for (const [label, ulid, trigger, sck, url, constraint] of [
  ['mixed marker', '01K5ABCDEFX2VYB4M6X9CDPTE4', '574', 'hermes|v1~01K5ABCDEFX2VYB4M6X9CDPTE4',
    'https://pay.hotmart.com/F106691755G?off=mgbgpp19&checkoutMode=10&src=hermes'
      + '&sck=hermes~v1~01K5ABCDEFX2VYB4M6X9CDPTE4',
    'checkout_link_issuances_sck_value_shape'],
  ['tilde marker after a bar', '01K5ABCDEFX2VYB4M6X9CDPTE6', '576', `${realAdSck}|hermes~v1~01K5ABCDEFX2VYB4M6X9CDPTE6`,
    'https://pay.hotmart.com/F106691755G?off=mgbgpp19&checkoutMode=10&src=hermes'
      + `&sck=${realAdSck}~hermes~v1~01K5ABCDEFX2VYB4M6X9CDPTE6`,
    'checkout_link_issuances_sck_value_shape'],
  ['raw bar in the URL', '01K5ABCDEFX2VYB4M6X9CDPTE5', '575', 'hermes|v1|01K5ABCDEFX2VYB4M6X9CDPTE5',
    'https://pay.hotmart.com/F106691755G?off=mgbgpp19&checkoutMode=10&src=hermes'
      + '&sck=hermes|v1|01K5ABCDEFX2VYB4M6X9CDPTE5',
    'checkout_link_issuances_url_shape'],
]) {
  let refusedBy = null;
  await db.exec('begin');
  try {
    await insertSibling(legacyTemplate.issuance_id, ulid, trigger, sck, url);
  } catch (error) {
    refusedBy = error?.constraint ?? `not a check violation: ${error?.message}`;
  }
  await db.exec('rollback');
  if (refusedBy !== constraint) {
    throw new Error(`issuance CHECKs on a ${label}: expected ${constraint}, got ${refusedBy}`);
  }
}

// A mixed sck is refused by the admission before anything is stored, like any
// malformed one.
for (const [index, mixed] of [
  `hermes|v1~${realUlid}`, `${realAdSck}|hermes~v1~${realUlid}`, `${realAdSck}~hermes|v1|${realUlid}`,
].entries()) {
  const externalId = `checkout-mixed-purchase-${index}`;
  let blocked = false;
  try {
    await admitPurchase(externalId, `HPMIXED00${index}`, mixed);
  } catch {
    blocked = true;
  }
  const residue = (await db.query(`
    select count(*)::integer as count
    from public.webhook_events
    where source = 'hotmart' and external_event_id = $1
  `, [externalId])).rows[0]?.count;
  if (!blocked || residue !== 0) {
    throw new Error(`mixed sck was admitted: ${JSON.stringify({ mixed, blocked, residue })}`);
  }
}

// The real legacy markers (the 2026-10-03 Johanna sale and the 2026-10-05 ATT1
// emission) still read as ours; a real ad's sck alone does not.
const shapesEvent = (await db.query(`
  insert into public.webhook_events (
    source, external_event_id, event_type, payload, processing_status
  ) values (
    'hotmart', 'checkout-real-shapes', 'PURCHASE_APPROVED', '{}'::jsonb, 'received'
  ) returning id
`)).rows[0];
for (const [sck, expected] of [
  ...realLegacyMarkers.map((marker) => [marker, 'not_found']),
  [realAdSck, 'invalid_hermes_sck'],
]) {
  const read = (await db.query(`
    select * from public.correlate_hotmart_checkout_issuance_v2(
      $1::uuid, $2, clock_timestamp()
    )
  `, [shapesEvent.id, sck])).rows[0];
  if (read?.outcome !== expected) {
    throw new Error(`real sck shape misread: ${JSON.stringify({ sck, expected, read })}`);
  }
}

// The attribution columns are immutable too.
await db.exec('begin');
let attributionLocked = false;
try {
  await db.query(`
    update public.checkout_link_issuances
    set attribution_resolution = 'marker_only'
    where id = $1::uuid
  `, [fullPrepared.issuance_id]);
} catch {
  attributionLocked = true;
}
await db.exec('rollback');
if (!attributionLocked) throw new Error('attribution_resolution is mutable');

// The resolution columns are immutable like the rest of the row.
await db.exec('begin');
let resolutionLocked = false;
try {
  await db.query(`
    update public.checkout_link_issuances
    set offer_resolution = 'default_no_intent'
    where id = $1::uuid
  `, [adsBPrepared.issuance_id]);
} catch {
  resolutionLocked = true;
}
await db.exec('rollback');
if (!resolutionLocked) throw new Error('offer_resolution is mutable');

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
const tildeMarker = schema.rows.find((row) => row.version === '20261005000100');
if (tildeMarker?.fingerprint_status !== 'fingerprint_present') {
  throw new Error(`tilde marker schema fingerprint mismatch: ${JSON.stringify(tildeMarker)}`);
}

console.log('JOHANNA_CHECKOUT_ISSUANCE_V2_SQL_OK');
await db.close();
