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
const stack = [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(join(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => join(root, 'supabase/migrations', name)),
];
for (const file of stack) {
  await db.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}

const binding = {
  tenant_ref: 'att1', funnel_ref: 'att1-main', binding_version: 1,
  status: 'active', ally_ref: 'ally-one', lead_ally_name: 'Ally One',
  lead_site: 'ally-one-site', lead_landing_id: 'main',
  lead_page_host: 'ally-one.example', lead_page_path: '/offer/main',
  product_hotlink: 'ATT1HOTLINK', product_name: 'ATT1 Offer',
  product_price: '49', currency: 'USD', offer_code: 'att1offer',
  consent_copy_version: 'att1-whatsapp-v1', hotmart_product_id: 123456,
  chatwoot_account_id: 42, chatwoot_inbox_id: 24,
  inbound_scope_key: 'att1-inbound', inbound_scope_version: 1,
};
const columns = Object.keys(binding);
await db.query(
  `insert into public.commercial_ally_runtime_bindings (${columns.join(',')})
   values (${columns.map((_, i) => `$${i + 1}`).join(',')})`,
  Object.values(binding),
);
await db.query(`
  insert into public.commercial_ally_runtime_bindings
    (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
     lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
     product_name, product_price, currency, offer_code, consent_copy_version,
     hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
     inbound_scope_key, inbound_scope_version)
  select tenant_ref, funnel_ref, 2, 'draft', ally_ref, lead_ally_name,
         lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
         product_name, product_price, currency, offer_code, consent_copy_version,
         hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
         inbound_scope_key, inbound_scope_version
  from public.commercial_ally_runtime_bindings where binding_version = 1
`);

const purchase = (id, email, phone, overrides = {}) => ({
  id,
  creation_date: Date.parse('2026-09-01T12:00:01Z'),
  event: 'PURCHASE_APPROVED',
  version: '2.0.0',
  data: {
    product: { id: 123456, ucode: 'ATT1-UCODE' },
    buyer: {
      ...(email === null ? {} : { email }),
      ...(phone === null ? {} : { checkout_phone: phone }),
    },
    purchase: {
      approved_date: Date.parse('2026-09-01T12:00:00Z'),
      status: 'APPROVED',
      transaction: `HP${id.replaceAll('-', '').toUpperCase()}`.slice(0, 30),
      offer: { code: 'att1offer' },
    },
  },
  ...overrides,
});
const admit = (tenant, funnel, version, payload, email, phone) => db.query(
  `select * from public.admit_portable_hotmart_purchase_approved(
     $1, $2, $3, $4, $5::jsonb, $6, $7
   )`,
  [tenant, funnel, version, payload.id, JSON.stringify(payload), email, phone],
);
const expectRejected = async (label, action) => {
  let rejected = false;
  try { await action(); } catch { rejected = true; }
  if (!rejected) throw new Error(`${label} did not fail closed`);
};
const eventCount = async () => (await db.query(
  `select count(*)::integer count from public.webhook_events`,
)).rows[0]?.count;
const addIntent = async (email, phone, submittedAt = '2026-09-01T11:30:00Z', scope = {}) => (
  await db.query(
    `insert into public.purchase_intents
      (tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
       normalized_email, normalized_phone, submitted_at, lifecycle_state,
       whatsapp_contact_authorized, provisional, provider_observed,
       activation_authorized)
     values ($1,$2,'main',$3,$4,$5,$6,$7,'waiting_for_purchase',
             true,false,true,true)
     returning id`,
    [scope.tenant ?? 'att1', scope.funnel ?? 'att1-main',
      scope.product ?? 'ATT1HOTLINK', scope.offer ?? 'att1offer',
      email, phone, submittedAt],
  )
).rows[0].id;
const correlation = async (externalId) => (await db.query(
  `select c.* from public.portable_hotmart_purchase_correlations c
   join public.webhook_events e on e.id = c.webhook_event_id
   where e.external_event_id = $1`, [externalId],
)).rows[0];

const contract = (await db.query(`
  select
    to_regclass('public.commercial_ally_hotmart_purchase_policies') is not null policy_table,
    to_regclass('public.portable_hotmart_purchase_correlations') is not null correlation_table,
    to_regprocedure('public.admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text)') is not null rpc,
    (select count(*)::integer from public.commercial_ally_hotmart_purchase_policies) policy_rows
`)).rows[0];
if (!contract?.policy_table || !contract?.correlation_table || !contract?.rpc
    || contract.policy_rows !== 0) {
  throw new Error(`portable purchase-stop contract/default diverged: ${JSON.stringify(contract)}`);
}

const baselineEvents = await eventCount();
await expectRejected('missing binding', () => admit(
  'missing', 'att1-main', 1, purchase('missing-binding', 'missing@example.test', '+12025550001'),
  'missing@example.test', '12025550001',
));
await expectRejected('inactive binding', () => admit(
  'att1', 'att1-main', 2, purchase('inactive-binding', 'inactive@example.test', '+12025550002'),
  'inactive@example.test', '12025550002',
));
await expectRejected('missing temporal policy', () => admit(
  'att1', 'att1-main', 1, purchase('missing-policy', 'policy@example.test', '+12025550003'),
  'policy@example.test', '12025550003',
));
await db.query(`update public.commercial_ally_runtime_bindings
                set hotmart_product_id = 654321 where binding_version = 1`);
await expectRejected('drifted binding', () => admit(
  'att1', 'att1-main', 1, purchase('drifted-binding', 'drift@example.test', '+12025550004'),
  'drift@example.test', '12025550004',
));
await db.query(`update public.commercial_ally_runtime_bindings
                set hotmart_product_id = 123456 where binding_version = 1`);
const wrongProduct = purchase('wrong-product', 'wrong-product@example.test', '+12025550005');
wrongProduct.data.product.id = 999999;
await expectRejected('wrong product', () => admit(
  'att1', 'att1-main', 1, wrongProduct,
  'wrong-product@example.test', '12025550005',
));
const wrongOffer = purchase('wrong-offer', 'wrong-offer@example.test', '+12025550006');
wrongOffer.data.purchase.offer.code = 'other';
await expectRejected('wrong offer', () => admit(
  'att1', 'att1-main', 1, wrongOffer,
  'wrong-offer@example.test', '12025550006',
));
if (await eventCount() !== baselineEvents) {
  throw new Error('binding/product/offer/policy rejection admitted durable events');
}

await db.query(`
  insert into public.commercial_ally_hotmart_purchase_policies
    (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
  values ('att1', 'att1-main', 1, true, interval '2 hours')
`);

const unmatchedPayload = purchase(
  'portable-unmatched', 'unmatched@example.test', '+12025550100',
);
await admit('att1', 'att1-main', 1, unmatchedPayload,
  'unmatched@example.test', '12025550100');
if ((await correlation('portable-unmatched'))?.outcome !== 'unmatched') {
  throw new Error('unmatched purchase did not remain unmatched');
}

const exactIntent = await addIntent('exact@example.test', '12025550101');
const exactPayload = purchase('portable-exact', 'exact@example.test', '+12025550101');
await admit('att1', 'att1-main', 1, exactPayload, 'exact@example.test', '12025550101');
const exactCorrelation = await correlation('portable-exact');
const exactState = (await db.query(
  `select lifecycle_state, activation_authorized from public.purchase_intents where id=$1`,
  [exactIntent],
)).rows[0];
if (exactCorrelation?.outcome !== 'resolved'
    || exactCorrelation?.purchase_intent_id !== exactIntent
    || exactState?.lifecycle_state !== 'purchased'
    || exactState?.activation_authorized !== false) {
  throw new Error('exact purchase did not atomically stop the exact intent');
}

// Canonical live-identity indexes normally prevent this topology. Drop the
// email uniqueness fence only long enough to exercise fail-closed behavior
// against legacy/drifted duplicate data, then restore it below.
await db.exec(`drop index public.purchase_intents_one_observed_email_idx`);
const ambiguousA = await addIntent('ambiguous@example.test', '12025550102');
const ambiguousB = await addIntent('ambiguous@example.test', '12025550103');
const ambiguousPayload = purchase('portable-ambiguous', 'ambiguous@example.test', null);
await admit('att1', 'att1-main', 1, ambiguousPayload, 'ambiguous@example.test', null);
const ambiguousCorrelation = await correlation('portable-ambiguous');
const ambiguousStates = (await db.query(
  `select lifecycle_state from public.purchase_intents where id = any($1::uuid[]) order by id`,
  [[ambiguousA, ambiguousB]],
)).rows;
if (ambiguousCorrelation?.outcome !== 'ambiguous'
    || ambiguousCorrelation?.candidate_count !== 2
    || ambiguousStates.some((row) => row.lifecycle_state !== 'waiting_for_purchase')) {
  throw new Error('ambiguous purchase did not fail closed');
}
await db.query(
  `update public.purchase_intents set lifecycle_state='cancelled' where id=$1`,
  [ambiguousB],
);
await db.exec(`
  create unique index purchase_intents_one_observed_email_idx
  on public.purchase_intents
    (tenant_ref, funnel_ref, product_ref, offer_ref, normalized_email)
  where lifecycle_state='waiting_for_purchase'
    and provider_observed and normalized_email is not null
`);

const conflictEmail = await addIntent('conflict@example.test', '12025550104');
const conflictPhone = await addIntent('other@example.test', '12025550105');
const conflictPayload = purchase('portable-conflict', 'conflict@example.test', '+12025550105');
await admit('att1', 'att1-main', 1, conflictPayload, 'conflict@example.test', '12025550105');
const conflictCorrelation = await correlation('portable-conflict');
const conflictStates = (await db.query(
  `select lifecycle_state from public.purchase_intents where id = any($1::uuid[]) order by id`,
  [[conflictEmail, conflictPhone]],
)).rows;
if (conflictCorrelation?.outcome !== 'conflict'
    || conflictCorrelation?.candidate_count !== 2
    || conflictStates.some((row) => row.lifecycle_state !== 'waiting_for_purchase')) {
  throw new Error('conflicting purchase did not fail closed');
}

const replayIntent = await addIntent('replay@example.test', '12025550106');
const replayPayload = purchase('portable-replay', 'replay@example.test', '+12025550106');
const inserted = await admit('att1', 'att1-main', 1, replayPayload,
  'replay@example.test', '12025550106');
const duplicate = await admit('att1', 'att1-main', 1, replayPayload,
  'replay@example.test', '12025550106');
const changed = structuredClone(replayPayload);
changed.data.purchase.transaction = 'HPCHANGED123456';
const semanticConflict = await admit('att1', 'att1-main', 1, changed,
  'replay@example.test', '12025550106');
const replayCounts = (await db.query(`
  select
    (select count(*)::integer from public.webhook_events e
      where e.external_event_id='portable-replay') events,
    (select count(*)::integer from public.portable_hotmart_purchase_correlations c
      join public.webhook_events e on e.id=c.webhook_event_id
      where e.external_event_id='portable-replay') correlations,
    (select count(*)::integer from public.hotmart_purchase_semantic_conflicts c
      where c.incoming_external_event_id='portable-replay') conflicts
`)).rows[0];
if (inserted.rows[0]?.outcome !== 'inserted'
    || duplicate.rows[0]?.outcome !== 'duplicate'
    || semanticConflict.rows[0]?.outcome !== 'semantic_conflict'
    || replayCounts?.events !== 1 || replayCounts?.correlations !== 1
    || replayCounts?.conflicts !== 1) {
  throw new Error(`replay/conflict semantics diverged: ${JSON.stringify(replayCounts)}`);
}

const timerFixture = async (label, completed) => {
  const intentId = await addIntent(`${label}@example.test`, completed ? '12025550108' : '12025550107');
  const source = (await db.query(
    `insert into public.webhook_events
       (source, external_event_id, event_type, payload, processing_status)
     values ('hotmart',$1,'PURCHASE_OUT_OF_SHOPPING_CART','{}'::jsonb,'processed')
     returning id`, [`${label}-abandonment`],
  )).rows[0].id;
  const scopeId = (await db.query(
    `select id from public.hotmart_purchase_intent_scopes order by created_at limit 1`,
  )).rows[0].id;
  await db.query(
    `insert into public.hotmart_purchase_intent_correlations
       (webhook_event_id, scope_id, event_type, outcome, purchase_intent_id,
        matched_by, candidate_count, reason_code, manual_handoff_required, observed_at)
     values ($1,$2,'PURCHASE_OUT_OF_SHOPPING_CART','resolved',$3,
             'email',1,'fixture',false,'2026-09-01T11:00:00Z')`,
    [source, scopeId, intentId],
  );
  const policyEvent = (await db.query(`
    select * from public.hotmart_abandonment_timer_policy_binding_events
    order by recorded_at limit 1
  `)).rows[0];
  const status = completed ? 'completed' : 'scheduled';
  const outcome = completed ? 'blocked_not_authorized' : null;
  const completedAt = completed ? '2026-09-01T11:10:00Z' : null;
  const reevaluation = (await db.query(
    `insert into public.hotmart_abandonment_reevaluations
       (purchase_intent_id, source_webhook_event_id, source_scope_id,
        policy_binding_id, policy_binding_generation, policy_key, policy_version,
        delay_seconds_snapshot, observed_at, due_at, status, outcome,
        idempotency_key, completed_at)
     values ($1,$2,$3,$4,$5,$6,$7,$8::integer,'2026-09-01T11:00:00Z',
             '2026-09-01T11:00:00Z'::timestamptz + make_interval(secs=>$8::integer),
             $9,$10,$11,$12)
     returning id`,
    [intentId, source, scopeId, policyEvent.binding_id, policyEvent.generation,
      policyEvent.policy_key, policyEvent.policy_version, policyEvent.delay_seconds,
      status, outcome, `${label}-timer`, completedAt],
  )).rows[0].id;
  return { intentId, reevaluation };
};
for (const [label, completed, phone] of [
  ['purchase-before-timer', false, '12025550107'],
  ['purchase-after-timer', true, '12025550108'],
]) {
  const fixture = await timerFixture(label, completed);
  const payload = purchase(label, `${label}@example.test`, `+${phone}`);
  await admit('att1', 'att1-main', 1, payload, `${label}@example.test`, phone);
  const timer = (await db.query(
    `select status, outcome from public.hotmart_abandonment_reevaluations where id=$1`,
    [fixture.reevaluation],
  )).rows[0];
  if (timer?.status !== 'completed' || timer?.outcome !== 'cancelled_purchased') {
    throw new Error(`${label} did not atomically cancel/supersede timer`);
  }
}

const effectTables = (await db.query(`
  select tablename from pg_tables
  where schemaname='public'
    and (tablename='scheduled_actions'
      or tablename='recovery_cases'
      or tablename ~ '(command|message|delivery|outbound)')
`)).rows.map((row) => row.tablename);
for (const table of effectTables) {
  const count = (await db.query(
    `select count(*)::integer count from public.${table}`,
  )).rows[0]?.count;
  if (count !== 0) throw new Error(`unexpected effect row in ${table}: ${count}`);
}

const acl = (await db.query(`
  select
    has_function_privilege('service_role',
      'public.admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text)',
      'execute') service_execute,
    has_function_privilege('anon',
      'public.admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text)',
      'execute') anon_execute,
    has_function_privilege('authenticated',
      'public.admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text)',
      'execute') authenticated_execute
`)).rows[0];
if (!acl?.service_execute || acl?.anon_execute || acl?.authenticated_execute) {
  throw new Error(`portable purchase RPC ACL mismatch: ${JSON.stringify(acl)}`);
}

console.log('portable_purchase_stop_binding_and_scope_fences=OK');
console.log('portable_purchase_stop_outcomes_and_replay=OK');
console.log('portable_purchase_stop_timer_orderings=OK');
console.log(`portable_purchase_stop_effect_tables_zero=${effectTables.length}`);
