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
for (const file of [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(join(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => join(root, 'supabase/migrations', name)),
]) {
  await db.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}

const bindingValues = [
  'att1', 'att1-main', 1, 'active', 'ally-one', 'Ally One',
  'ally-one-site', 'main', 'ally-one.example', '/offer/main', 'ATT1HOTLINK',
  'ATT1 Offer', '49', 'USD', 'att1offer', 'att1-whatsapp-v1', 123456,
  42, 24, 'att1-inbound', 1,
];
await db.query(`
  insert into public.commercial_ally_runtime_bindings
    (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
     lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
     product_name, product_price, currency, offer_code, consent_copy_version,
     hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
     inbound_scope_key, inbound_scope_version)
  values (${bindingValues.map((_, i) => `$${i + 1}`).join(',')})
`, bindingValues);

const cart = (id, overrides = {}) => ({
  id,
  creation_date: Date.parse('2026-09-03T02:00:00Z'),
  event: 'PURCHASE_OUT_OF_SHOPPING_CART',
  version: '2.0.0',
  data: {
    buyer: { email: 'buyer@example.test', phone: '+1 (202) 555-0123' },
    product: { id: 123456, name: 'ATT1 Offer' },
    offer: { code: 'att1offer' },
    checkout_country: { iso: 'MX', name: 'México' },
  },
  ...overrides,
});
const admit = (tenant, version, payload) => db.query(`
  select * from public.admit_portable_hotmart_cart_abandonment(
    $1, 'att1-main', $2, $3, $4::jsonb, 'buyer@example.test', '12025550123'
  )
`, [tenant, version, payload.id, JSON.stringify(payload)]);
const expectRejected = async (label, action) => {
  try {
    await action();
  } catch {
    return;
  }
  throw new Error(`${label} did not fail closed`);
};
const eventCount = async () => (await db.query(
  `select count(*)::integer count from public.webhook_events`,
)).rows[0]?.count;

const baselineEvents = await eventCount();
await expectRejected('missing binding', () => admit('missing', 1, cart('missing')));
await expectRejected('missing scope', () => admit('att1', 1, cart('missing-scope')));
const wrongProduct = cart('wrong-product');
wrongProduct.data.product.id = 999999;
await expectRejected('wrong product', () => admit('att1', 1, wrongProduct));
const wrongOffer = cart('wrong-offer');
wrongOffer.data.offer.code = 'other';
await expectRejected('wrong offer', () => admit('att1', 1, wrongOffer));
if (await eventCount() !== baselineEvents) {
  throw new Error('rejected portable cart created durable events');
}

await db.query(`
  insert into public.hotmart_purchase_intent_scopes
    (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
     offer_ref, max_lookback, active)
  values ('att1', 'att1-main', '123456', 'ATT1HOTLINK', 'att1offer',
          interval '2 hours', true)
`);
await db.query(`
  insert into public.hotmart_abandonment_timer_policy_bindings
    (tenant_ref, funnel_ref, product_ref, offer_ref, enabled,
     policy_key, policy_version)
  select 'att1', 'att1-main', 'ATT1HOTLINK', 'att1offer', true,
         policy_key, policy_version
  from public.hotmart_abandonment_timer_policy_bindings
  where tenant_ref='lancemos'
    and funnel_ref='psicologajohanna'
    and enabled
  limit 1
`);
const intent = (await db.query(`
  insert into public.purchase_intents
    (tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
     normalized_email, normalized_phone, submitted_at, lifecycle_state,
     whatsapp_contact_authorized, provisional, provider_observed,
     activation_authorized)
  values ('att1','att1-main','main','ATT1HOTLINK','att1offer',
          'buyer@example.test','12025550123','2026-09-03T01:30:00Z',
          'waiting_for_purchase',true,false,true,true)
  returning id
`)).rows[0].id;

const payload = cart('portable-cart-exact');
const inserted = await admit('att1', 1, payload);
const duplicate = await admit('att1', 1, payload);
const changed = structuredClone(payload);
changed.data.buyer.email = 'changed@example.test';
const conflict = await admit('att1', 1, changed);
const state = (await db.query(`
  select lifecycle_state from public.purchase_intents where id=$1
`, [intent])).rows[0]?.lifecycle_state;
const correlation = (await db.query(`
  select c.outcome, c.purchase_intent_id
  from public.hotmart_purchase_intent_correlations c
  join public.webhook_events e on e.id=c.webhook_event_id
  where e.external_event_id='portable-cart-exact'
`)).rows[0];
const scheduledTimers = (await db.query(`
  select count(*)::integer count
  from public.hotmart_abandonment_reevaluations
  where purchase_intent_id=$1
`, [intent])).rows[0]?.count;
if (inserted.rows[0]?.outcome !== 'inserted'
    || duplicate.rows[0]?.outcome !== 'duplicate'
    || conflict.rows[0]?.outcome !== 'semantic_conflict'
    || correlation?.outcome !== 'resolved'
    || correlation?.purchase_intent_id !== intent
    || state !== 'waiting_for_purchase'
    || scheduledTimers !== 0) {
  throw new Error('portable cart correlation/replay contract diverged');
}

const acl = (await db.query(`
  select
    has_function_privilege('service_role',
      'public.admit_portable_hotmart_cart_abandonment(text,text,integer,text,jsonb,text,text)',
      'execute') service_exec,
    has_function_privilege('anon',
      'public.admit_portable_hotmart_cart_abandonment(text,text,integer,text,jsonb,text,text)',
      'execute') anon_exec,
    has_function_privilege('authenticated',
      'public.admit_portable_hotmart_cart_abandonment(text,text,integer,text,jsonb,text,text)',
      'execute') authenticated_exec,
    has_function_privilege('service_role',
      'public.admit_and_correlate_hotmart_cart_abandonment(text,jsonb,text,text)',
      'execute') legacy_service_exec,
    coalesce(has_function_privilege('service_role',
      to_regprocedure('public.admit_johanna_hotmart_cart_abandonment(text,jsonb,text,text)'),
      'execute'), false) johanna_service_exec
`)).rows[0];
if (!acl?.service_exec || acl.anon_exec || acl.authenticated_exec
    || acl.legacy_service_exec || !acl.johanna_service_exec) {
  throw new Error(`portable cart ACL diverged: ${JSON.stringify(acl)}`);
}

const legacyCountBefore = Number((await db.query(
  `select count(*)::int as count from public.webhook_events`
)).rows[0].count);
let legacyScopeRejected = false;
try {
  await db.query(`
    select * from public.admit_johanna_hotmart_cart_abandonment(
      'legacy-wrong-scope-001',
      '{"id":"legacy-wrong-scope-001","event":"PURCHASE_OUT_OF_SHOPPING_CART","version":"2.0.0","creation_date":1788377100000,"data":{"product":{"id":9999999,"name":"Wrong product"},"offer":{"code":"wrong"},"buyer":{"email":"nobody@example.test","phone":"+529000000000"}}}'::jsonb,
      'nobody@example.test', '+529999999999'
    )
  `);
} catch (error) {
  legacyScopeRejected = String(error).includes('johanna_hotmart_cart_scope_mismatch');
}
if (!legacyScopeRejected) throw new Error('legacy wrapper accepted wrong scope');
const legacyCountAfterRejected = Number((await db.query(
  `select count(*)::int as count from public.webhook_events`
)).rows[0].count);
if (legacyCountAfterRejected !== legacyCountBefore) {
  throw new Error('legacy wrapper wrong scope left residue');
}

const legacyIntent = (await db.query(`
  insert into public.purchase_intents
    (tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
     normalized_email, normalized_phone, submitted_at, lifecycle_state,
     whatsapp_contact_authorized, provisional, provider_observed,
     activation_authorized)
  values ('lancemos','psicologajohanna','main','F106691755G','bxjge6zq',
          'legacy.wrapper@example.test','529111111111',
          to_timestamp(1788377100) - interval '30 minutes',
          'waiting_for_purchase',true,false,true,true)
  returning id
`)).rows[0].id;
const legacyPayload = JSON.stringify({
  id: 'legacy-wrapper-event-001',
  event: 'PURCHASE_OUT_OF_SHOPPING_CART',
  version: '2.0.0',
  creation_date: 1788377100000,
  data: {
    product: { id: 8104005, name: 'Libre de Ansiedad' },
    offer: { code: 'bxjge6zq' },
    buyer: { email: 'legacy.wrapper@example.test', phone: '+529111111111' },
  },
});
const legacyAdmission = (await db.query(
  `select * from public.admit_johanna_hotmart_cart_abandonment($1, $2::jsonb, $3, $4)`,
  ['legacy-wrapper-event-001', legacyPayload, 'legacy.wrapper@example.test', '+529111111111'],
)).rows[0];
if (!['inserted', 'duplicate'].includes(legacyAdmission.outcome)) {
  throw new Error(`legacy wrapper unexpected admission outcome: ${legacyAdmission.outcome}`);
}
const legacyTimerCount = Number((await db.query(`
  select count(*)::int as count
  from public.hotmart_abandonment_reevaluations
  where purchase_intent_id = $1
`, [legacyIntent])).rows[0].count);
if (legacyTimerCount !== 1) {
  throw new Error(`legacy wrapper failed to preserve timer scheduling: ${legacyTimerCount}`);
}

await db.close();
console.log('commercial ally portable recovery validation passed');
