import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const migrationDir = join(root, 'supabase/migrations');
const migrationNames = readdirSync(migrationDir)
  .filter((name) => name.endsWith('.sql'))
  .sort();
const targetMigration = '20260831000300_johanna_six_landing_precheckout.sql';
const predecessorMigration = '20260831000200_disable_johanna_funnel_dashboard_read.sql';
const prefixMigrationNames = migrationNames.filter((name) => name < targetMigration);

function sqlFor(file) {
  return readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  );
}

async function freshDb() {
  const instance = new PGlite();
  await instance.waitReady;
  await instance.exec(`
    create role anon nologin;
    create role authenticated nologin;
    create role service_role nologin;
  `);
  await instance.exec(sqlFor(join(root, 'supabase/baseline/20260803_public_schema.sql')));
  return instance;
}

async function applyMigrations(instance, names) {
  for (const name of names) {
    await instance.exec(sqlFor(join(migrationDir, name)));
  }
}

const missingPredecessorDb = await freshDb();
await applyMigrations(
  missingPredecessorDb,
  prefixMigrationNames.filter((name) => name !== predecessorMigration),
);
await missingPredecessorDb.exec(`
  create schema supabase_migrations;
  create table supabase_migrations.schema_migrations (version text primary key);
  insert into supabase_migrations.schema_migrations (version) values
    ('20260829000200'), ('20260829000300'), ('20260829000400'),
    ('20260829000500'), ('20260831000300');
`);
await applyMigrations(missingPredecessorDb, [targetMigration]);
const missingPredecessorReadiness = await missingPredecessorDb.query(
  'select migration_tracking_complete from public.get_precheckout_delayed_first_touch_readiness()',
);
if (missingPredecessorReadiness.rows[0]?.migration_tracking_complete !== false) {
  throw new Error('readiness accepted a missing required 20260831000200 predecessor');
}
await missingPredecessorDb.close();

const divergentScopeDb = await freshDb();
await applyMigrations(divergentScopeDb, prefixMigrationNames);
await divergentScopeDb.exec(`
  insert into public.hotmart_purchase_intent_scopes (
    tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
    offer_ref, max_lookback, active
  ) values (
    'wrong-tenant', 'wrong-funnel', '8104005', 'wrong-product',
    'mgbgpp19', interval '1 minute', true
  );
`);
try {
  await applyMigrations(divergentScopeDb, [targetMigration]);
  throw new Error('migration accepted a divergent active correlation scope');
} catch (error) {
  if (error?.message === 'migration accepted a divergent active correlation scope') throw error;
  if (!error?.message?.includes('johanna_existing_correlation_scope_mismatch')) throw error;
}
await divergentScopeDb.close();

const db = await freshDb();
await applyMigrations(db, migrationNames);
await db.exec(`
  create schema if not exists supabase_migrations;
  create table if not exists supabase_migrations.schema_migrations (
    version text primary key
  );
  insert into supabase_migrations.schema_migrations (version) values
    ('20260829000200'), ('20260829000300'), ('20260829000400'),
    ('20260829000500'), ('20260831000200'), ('20260831000300')
  on conflict (version) do nothing;
`);

const pairs = [
  ['ads-a', 'bxjge6zq'], ['ads-b', 'mgbgpp19'], ['ads-c', 's1qfxm7m'],
  ['org-a', 'jtt6fcsm'], ['org-b', 'ecyu87q0'], ['org-c', 'ulhzpw9a'],
];
const configured = await db.query(`
  select pair.landing_ref, pair.offer_ref,
         scope.scope_key, runtime.runtime_state, runtime.generation,
         correlation.active, binding.enabled, binding.precheckout_first_touch_enabled
  from public.johanna_precheckout_landing_offers pair
  cross join public.pilot_scope_versions scope
  join public.pilot_runtime_controls runtime
    on runtime.scope_key = scope.scope_key and runtime.scope_version = scope.version
  join public.hotmart_purchase_intent_scopes correlation
    on correlation.offer_ref = pair.offer_ref and correlation.active
  join public.hotmart_abandonment_timer_policy_bindings binding
    on binding.offer_ref = pair.offer_ref
   and lower(binding.product_ref) = lower('F106691755G')
  where scope.scope_key = 'johanna-precheckout-delayed-first-touch-production'
    and scope.version = 1 and scope.status = 'published'
    and scope.offer_code = 'explicit-six-pair-authority'
    and scope.max_cohort_contacts = 1000000
    and scope.max_outbound_request_starts_total = 1000000
    and scope.max_outbound_request_starts_per_day = 10000
`);
if (configured.rows.length !== 6
    || configured.rows.some((row) => row.runtime_state !== 'inactive'
      || row.generation !== 0 || !row.active || !row.enabled
      || !row.precheckout_first_touch_enabled)) {
  throw new Error(`six-pair production authority mismatch: ${JSON.stringify(configured.rows)}`);
}
const readiness = await db.query(
  'select * from public.get_precheckout_delayed_first_touch_readiness()',
);
if (readiness.rows[0]?.scope_configured !== true
    || readiness.rows[0]?.runtime_state !== 'inactive'
    || readiness.rows[0]?.runtime_generation !== 0
    || readiness.rows[0]?.reason_code !== 'precheckout_first_touch_ready') {
  throw new Error(`application readiness contract changed: ${JSON.stringify(readiness.rows)}`);
}
await db.exec(`
  insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
  )
  select 'johanna-precheckout-review-alternate', 1, 'published', purpose,
         timezone, business_windows, interval '60 minutes', expires_after,
         max_automatic_messages, steps, approved_by, approved_at, published_at
  from public.followup_policy_versions
  where policy_key = 'johanna-precheckout-delayed-first-touch-timer'
    and version = 1;
  update public.hotmart_abandonment_timer_policy_bindings
  set policy_key = 'johanna-precheckout-review-alternate', policy_version = 1,
      generation = generation + 1, updated_at = clock_timestamp()
  where tenant_ref = 'lancemos' and funnel_ref = 'psicologajohanna'
    and offer_ref = 'mgbgpp19';
`);
const mismatchedPolicyReadiness = await db.query(
  'select * from public.get_precheckout_delayed_first_touch_readiness()',
);
if (mismatchedPolicyReadiness.rows[0]?.scope_configured !== true
    || mismatchedPolicyReadiness.rows[0]?.reason_code !== 'timer_binding_policy_mismatch') {
  throw new Error(`timer policy mismatch was misclassified: ${JSON.stringify(mismatchedPolicyReadiness.rows)}`);
}
await db.exec(`
  update public.hotmart_abandonment_timer_policy_bindings
  set policy_key = 'johanna-precheckout-delayed-first-touch-timer', policy_version = 1,
      generation = generation + 1, updated_at = clock_timestamp()
  where tenant_ref = 'lancemos' and funnel_ref = 'psicologajohanna'
    and offer_ref = 'mgbgpp19';
`);
try {
  await db.exec("update public.johanna_precheckout_landing_offers set offer_ref='changed' where landing_ref='ads-a'");
  throw new Error('published pair was mutable');
} catch (error) {
  if (error?.message === 'published pair was mutable') throw error;
  if (error?.code !== '55000') throw error;
}

function payload(index, landingRef, offerRef) {
  const phone = `12025551${String(index).padStart(3, '0')}`;
  const email = `six-${index}@example.test`;
  const id = `01K4F8QW7N2VYB4M6X9CDP${String(index).padStart(3, '0')}`;
  const createdAt = '2026-09-06T12:00:00Z';
  const pageUrl = `https://psicologajohanna.com/${landingRef}`;
  const checkoutUrl = `https://pay.hotmart.com/F106691755G?off=${offerRef}&email=${email}`;
  const dedupeKey = `psicologajohanna:${offerRef}:${email}`;
  const raw = {
    id, event: 'lead.precheckout', version: '1.1.0', created_at: createdAt,
    source: { system: 'landing', site: 'psicologajohanna', aliado: 'Psicologa Johanna', landing_id: landingRef, page_url: pageUrl },
    data: {
      buyer: { name: `Lead ${index}`, email, phone: `+${phone}`, phone_country_code: '1', phone_national: phone.slice(1) },
      product: { hotlink: 'F106691755G', id: null, name: 'Liberate De La Ansiedad', price: 49, currency: 'USD' },
      offer: { code: offerRef }, checkout_url: checkoutUrl,
      checkout_country: { iso: 'US', source: 'phone_country_code' },
      attribution: { utm_source: '', utm_medium: '', utm_campaign: '', utm_content: '', utm_term: '', sck: '', fbclid: '', referrer: '' },
      consent: { marketing_optin: true, whatsapp_contact: true, copy_version: 'johanna-precheckout-whatsapp-disclosure-v1' },
    }, dedupe_key: dedupeKey,
  };
  const canonical = {
    event_type: 'PRECHECKOUT_FORM_SUBMITTED', contract_version: '1.1.0',
    external_submission_id: id, submitted_at: createdAt,
    source: { tenant_ref: 'lancemos', funnel_ref: 'psicologajohanna', landing_ref: landingRef, page_url: pageUrl, aliado: 'Psicologa Johanna' },
    identity: { email, phone, phone_valid: true, phone_country_iso: 'US' },
    lead: { full_name: `Lead ${index}` },
    commerce: { product_ref: 'F106691755G', product_name: 'Liberate De La Ansiedad', price: '49', currency: 'USD', offer_ref: offerRef, checkout_url: checkoutUrl },
    consent: { terms_accepted: false, privacy_accepted: false, marketing_optin: true, whatsapp_contact: true, copy_version: 'johanna-precheckout-whatsapp-disclosure-v1' },
    dedupe_key: dedupeKey,
    assurance: { provisional: false, provider_observed: true, activation_authorized: true },
  };
  return { id, raw, canonical };
}

async function assertUnused(item) {
  const preflight = await db.query(`
    select
      (select count(*)::integer from public.precheckout_submissions
       where external_submission_id = $1) as submission_count,
      (select count(*)::integer from public.purchase_intents
       where normalized_email = $2 or normalized_phone = $3) as intent_count
  `, [item.id, item.canonical.identity.email, item.canonical.identity.phone ?? '__no_phone__']);
  if (preflight.rows[0]?.submission_count !== 0
      || preflight.rows[0]?.intent_count !== 0) {
    throw new Error(`fixture identity already exists: ${JSON.stringify(preflight.rows)}`);
  }
}

for (let index = 0; index < pairs.length; index += 1) {
  const [landingRef, offerRef] = pairs[index];
  const item = payload(index + 1, landingRef, offerRef);
  await assertUnused(item);
  const admitted = await db.query(
    'select * from public.admit_observed_lead_precheckout($1,$2::jsonb,$3::jsonb)',
    [item.id, JSON.stringify(item.raw), JSON.stringify(item.canonical)],
  );
  if (admitted.rows[0]?.outcome !== 'inserted') {
    throw new Error(`admission failed for ${landingRef}/${offerRef}: ${JSON.stringify(admitted.rows)}`);
  }
  const intent = await db.query('select landing_ref, offer_ref from public.purchase_intents where id=$1', [admitted.rows[0].purchase_intent_id]);
  if (intent.rows[0]?.landing_ref !== landingRef || intent.rows[0]?.offer_ref !== offerRef) {
    throw new Error(`intent authority mismatch for ${landingRef}/${offerRef}: ${JSON.stringify(intent.rows)}`);
  }
  const timers = await db.query(`select id, status from public.hotmart_abandonment_reevaluations where purchase_intent_id=$1`, [admitted.rows[0].purchase_intent_id]);
  if (timers.rows.length !== 1 || timers.rows[0].status !== 'scheduled') {
    throw new Error(`timer missing for ${landingRef}/${offerRef}: ${JSON.stringify(timers.rows)}`);
  }
  const reevaluated = await db.query(
    'select * from public.reevaluate_hotmart_abandonment_timer($1,$2::timestamptz)',
    [timers.rows[0].id, '2026-09-06T14:00:00Z'],
  );
  if (reevaluated.rows[0]?.reevaluation_outcome !== 'command_reserved') {
    throw new Error(`reevaluation failed for ${landingRef}/${offerRef}: ${JSON.stringify(reevaluated.rows)}`);
  }
  const command = await db.query('select * from public.get_precheckout_delayed_one_shot_command($1)', [timers.rows[0].id]);
  if (command.rows[0]?.send_authorized !== true || command.rows[0]?.command_status !== 'request_started') {
    throw new Error(`command authorization failed for ${landingRef}/${offerRef}: ${JSON.stringify(command.rows)}`);
  }
}

const beforeInvalid = await db.query('select count(*)::integer as count from public.purchase_intents');
const invalid = payload(7, 'ads-a', 'mgbgpp19');
await assertUnused(invalid);
try {
  await db.query(
    'select * from public.admit_observed_lead_precheckout($1,$2::jsonb,$3::jsonb)',
    [invalid.id, JSON.stringify(invalid.raw), JSON.stringify(invalid.canonical)],
  );
  throw new Error('cross-paired landing/offer was admitted');
} catch (error) {
  if (error?.message === 'cross-paired landing/offer was admitted') throw error;
  if (error?.code !== '22023') throw error;
}
const afterInvalid = await db.query('select count(*)::integer as count from public.purchase_intents');
if (afterInvalid.rows[0]?.count !== beforeInvalid.rows[0]?.count) {
  throw new Error('failed admission left historical effects');
}

const invalidPhone = payload(8, 'org-c', 'ulhzpw9a');
delete invalidPhone.canonical.identity.phone;
invalidPhone.canonical.identity.phone_valid = false;
invalidPhone.canonical.consent.whatsapp_contact = false;
invalidPhone.canonical.assurance.activation_authorized = false;
await assertUnused(invalidPhone);
const invalidPhoneAdmission = await db.query(
  'select * from public.admit_observed_lead_precheckout($1,$2::jsonb,$3::jsonb)',
  [invalidPhone.id, JSON.stringify(invalidPhone.raw), JSON.stringify(invalidPhone.canonical)],
);
const invalidPhoneIntent = await db.query(`
  select normalized_phone, whatsapp_contact_authorized, activation_authorized,
         (select count(*)::integer from public.hotmart_abandonment_reevaluations timer
          where timer.purchase_intent_id = intent.id) as timer_count
  from public.purchase_intents intent where intent.id=$1
`, [invalidPhoneAdmission.rows[0]?.purchase_intent_id]);
if (invalidPhoneAdmission.rows[0]?.outcome !== 'inserted'
    || invalidPhoneIntent.rows[0]?.normalized_phone !== null
    || invalidPhoneIntent.rows[0]?.whatsapp_contact_authorized !== false
    || invalidPhoneIntent.rows[0]?.activation_authorized !== false
    || invalidPhoneIntent.rows[0]?.timer_count !== 0
    || invalidPhone.raw.data.consent.whatsapp_contact !== true) {
  throw new Error(`invalid phone did not fail closed durably: ${JSON.stringify({
    admission: invalidPhoneAdmission.rows, intent: invalidPhoneIntent.rows,
  })}`);
}

for (const [landingRef, offerRef] of pairs) {
  const result = await db.query('select public.is_johanna_precheckout_pair($1,$2) as allowed', [landingRef, offerRef]);
  if (result.rows[0]?.allowed !== true) throw new Error(`helper rejected ${landingRef}/${offerRef}`);
}
const denied = await db.query("select public.is_johanna_precheckout_pair('ads-a','*') as wildcard, public.is_johanna_precheckout_pair('ads-a','mgbgpp19') as crossed");
if (denied.rows[0]?.wildcard !== false || denied.rows[0]?.crossed !== false) {
  throw new Error(`pair helper failed closed: ${JSON.stringify(denied.rows)}`);
}

console.log('JOHANNA_SIX_LANDING_PRECHECKOUT_SQL_OK');
await db.close();
