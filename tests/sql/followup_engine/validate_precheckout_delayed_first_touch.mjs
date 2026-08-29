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
  insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
  ) values
    (
      'precheckout-delayed-test', 1, 'published', 'cart_recovery', 'UTC',
      '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
      interval '60 minutes', interval '7 days', 1,
      '[{"step_key":"first_contact","mode":"template"}]'::jsonb,
      'operator-test', now(), now()
    ),
    (
      'precheckout-invalid-delay-test', 1, 'published', 'cart_recovery', 'UTC',
      '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
      interval '30 minutes', interval '7 days', 1,
      '[{"step_key":"first_contact","mode":"template"}]'::jsonb,
      'operator-test', now(), now()
    );

  insert into public.hotmart_abandonment_timer_policy_bindings (
    tenant_ref, funnel_ref, product_ref, offer_ref, enabled,
    precheckout_first_touch_enabled, policy_key, policy_version
  ) values (
    'lancemos', 'psicologajohanna', 'F106691755G', 'bxjge6zq', true,
    true, 'precheckout-delayed-test', 1
  );
`);

function payload(suffix, version = '1.1.0') {
  const authorized = version === '1.1.0';
  const phone = suffix === 'one' ? '12025550123'
    : suffix === 'two' ? '12025550124' : '12025550125';
  const email = `precheckout-${suffix}@example.test`;
  const id = suffix === 'one' ? '01K3F8QW7N2VYB4M6X9CDPTZR1'
    : suffix === 'two' ? '01K3F8QW7N2VYB4M6X9CDPTZR2'
      : '01K3F8QW7N2VYB4M6X9CDPTZR3';
  const createdAt = '2026-08-29T15:00:00Z';
  const raw = {
    id,
    event: 'lead.precheckout',
    version,
    created_at: createdAt,
    source: {
      system: 'landing', site: 'psicologajohanna', aliado: 'Psicologa Johanna',
      landing_id: 'ads-a', page_url: 'https://psicologajohanna.com/ldla/evg/vsl/ads-a',
    },
    data: {
      buyer: {
        name: `Lead ${suffix}`, email, phone: `+${phone}`,
        phone_country_code: '1', phone_national: phone.slice(1),
      },
      product: {
        hotlink: 'F106691755G', id: null, name: 'Liberate De La Ansiedad',
        price: 49, currency: 'USD',
      },
      offer: { code: 'bxjge6zq' },
      checkout_url: `https://pay.hotmart.com/F106691755G?off=bxjge6zq&email=${email}`,
      checkout_country: { iso: 'US', source: 'phone_country_code' },
      attribution: {
        utm_source: '', utm_medium: '', utm_campaign: '', utm_content: '',
        utm_term: '', sck: '', fbclid: '', referrer: '',
      },
      consent: authorized ? {
        marketing_optin: true,
        whatsapp_contact: true,
        copy_version: 'johanna-precheckout-whatsapp-disclosure-v1',
      } : {
        marketing_optin: false,
        notice: 'sin consentimiento explicito - dato entregado para completar una compra',
      },
    },
    dedupe_key: `psicologajohanna:bxjge6zq:${email}`,
  };
  const canonical = {
    event_type: 'PRECHECKOUT_FORM_SUBMITTED', contract_version: version,
    external_submission_id: id, submitted_at: createdAt,
    source: {
      tenant_ref: 'lancemos', funnel_ref: 'psicologajohanna', landing_ref: 'ads-a',
      page_url: raw.source.page_url, aliado: raw.source.aliado,
    },
    identity: { email, phone, phone_valid: true, phone_country_iso: 'US' },
    lead: { full_name: `Lead ${suffix}` },
    commerce: {
      product_ref: 'F106691755G', product_name: 'Liberate De La Ansiedad',
      price: '49', currency: 'USD', offer_ref: 'bxjge6zq',
      checkout_url: raw.data.checkout_url,
    },
    consent: {
      terms_accepted: false, privacy_accepted: false,
      whatsapp_contact: authorized, marketing_optin: authorized,
      copy_version: authorized
        ? 'johanna-precheckout-whatsapp-disclosure-v1'
        : 'lead-precheckout-v1-no-explicit-optin',
    },
    dedupe_key: raw.dedupe_key,
    assurance: {
      provisional: false, provider_observed: true,
      activation_authorized: authorized,
    },
  };
  return { id, raw, canonical };
}

async function admit(item) {
  return db.query(
    'select * from public.admit_observed_lead_precheckout($1, $2::jsonb, $3::jsonb)',
    [item.id, JSON.stringify(item.raw), JSON.stringify(item.canonical)],
  );
}

const first = payload('one');
const inserted = await admit(first);
const replay = await admit(first);
if (inserted.rows[0]?.outcome !== 'inserted'
    || replay.rows[0]?.outcome !== 'duplicate') {
  throw new Error(`admission replay diverged: ${JSON.stringify({
    inserted: inserted.rows, replay: replay.rows,
  })}`);
}
const timer = await db.query(`
  select id, source_kind, source_submission_id, source_webhook_event_id,
         source_scope_id, delay_seconds_snapshot,
         extract(epoch from due_at - observed_at)::integer as delay_seconds,
         status, idempotency_key
  from public.hotmart_abandonment_reevaluations
  where purchase_intent_id = $1::uuid
`, [inserted.rows[0].purchase_intent_id]);
const timerRow = timer.rows[0];
if (timer.rows.length !== 1
    || timerRow.source_kind !== 'precheckout_intent'
    || timerRow.source_submission_id !== inserted.rows[0].submission_id
    || timerRow.source_webhook_event_id !== null
    || timerRow.source_scope_id !== null
    || timerRow.delay_seconds_snapshot !== 3600
    || timerRow.delay_seconds !== 3600
    || timerRow.status !== 'scheduled'
    || !timerRow.idempotency_key.startsWith('precheckout-first-touch:')) {
  throw new Error(`precheckout timer diverged: ${JSON.stringify(timer.rows)}`);
}
const refreshed = payload('one');
refreshed.id = '01K3F8QW7N2VYB4M6X9CDPTZR4';
refreshed.raw.id = refreshed.id;
refreshed.raw.created_at = '2026-08-29T15:50:00Z';
refreshed.canonical.external_submission_id = refreshed.id;
refreshed.canonical.submitted_at = '2026-08-29T15:50:00Z';
const refreshedSubmission = await db.query(`
  insert into public.precheckout_submissions (
    external_submission_id, contract_version, raw_payload, canonical_payload,
    provisional, provider_observed, activation_authorized
  ) values ($1, '1.1.0', $2::jsonb, $3::jsonb, false, true, true)
  returning id
`, [refreshed.id, JSON.stringify(refreshed.raw), JSON.stringify(refreshed.canonical)]);
await db.query(`
  insert into public.purchase_intent_submissions (
    purchase_intent_id, submission_id, ordinal
  ) values ($1::uuid, $2::uuid, 2)
`, [inserted.rows[0].purchase_intent_id, refreshedSubmission.rows[0].id]);
const refreshedSchedule = await db.query(`
  select * from public.schedule_precheckout_first_touch_reevaluation(
    $1::uuid, $2::uuid
  )
`, [inserted.rows[0].purchase_intent_id, refreshedSubmission.rows[0].id]);
const refreshedTimer = await db.query(`
  select source_submission_id, observed_at, due_at
  from public.hotmart_abandonment_reevaluations
  where purchase_intent_id = $1::uuid and status = 'scheduled'
`, [inserted.rows[0].purchase_intent_id]);
if (refreshedSchedule.rows[0]?.outcome !== 'coalesced_existing_timer'
    || refreshedTimer.rows.length !== 1
    || refreshedTimer.rows[0].source_submission_id !== refreshedSubmission.rows[0].id
    || new Date(refreshedTimer.rows[0].observed_at).toISOString()
      !== '2026-08-29T15:50:00.000Z'
    || new Date(refreshedTimer.rows[0].due_at).toISOString()
      !== '2026-08-29T16:50:00.000Z') {
  throw new Error(`latest authorized submission did not reset timer: ${JSON.stringify({
    schedule: refreshedSchedule.rows, timer: refreshedTimer.rows,
  })}`);
}
const due = await db.query(`
  select * from public.list_due_hotmart_abandonment_reevaluations(
    '2026-08-29T17:00:00Z'::timestamptz, 100
  )
`);
if (due.rows.length !== 0) throw new Error('inert precheckout timer entered due list');
let inactiveRejected = false;
try {
  await db.query(`
    select * from public.reevaluate_hotmart_abandonment_timer(
      $1::uuid, '2026-08-29T17:00:00Z'::timestamptz
    )
  `, [timer.rows[0].source_submission_id ? timerRow.idempotency_key.split(':')[1] : null]);
} catch (error) {
  // The call above intentionally uses the source UUID, proving arbitrary IDs cannot act.
  inactiveRejected = String(error).includes('hotmart_abandonment_reevaluation_not_found');
}
if (!inactiveRejected) throw new Error('arbitrary source ID was accepted as reevaluation ID');
const timerId = timerRow.id;
const unpublishedScope = await db.query(`
  select * from public.reevaluate_hotmart_abandonment_timer(
    $1::uuid, '2026-08-29T17:00:00Z'::timestamptz
  )
`, [timerId]);
const unpublishedCommands = await db.query(`
  select count(*)::integer as count
  from public.johanna_abandonment_one_shot_commands
  where source_reevaluation_id = $1::uuid
`, [timerId]);
if (unpublishedScope.rows[0]?.reevaluation_outcome
      !== 'blocked_contact_binding_missing'
    || unpublishedCommands.rows[0]?.count !== 0) {
  throw new Error('unpublished scope did not keep precheckout reservation inert');
}
console.log('PRECHECKOUT_DELAYED_TIMER_SCHEDULE_REPLAY_INERT_OK');

await db.exec(`
  update public.hotmart_abandonment_timer_policy_bindings
  set precheckout_first_touch_enabled = false,
      generation = generation + 1
  where tenant_ref = 'lancemos' and funnel_ref = 'psicologajohanna';
`);
const second = await admit(payload('two'));
const disabledCount = await db.query(`
  select count(*)::integer as count
  from public.hotmart_abandonment_reevaluations
  where purchase_intent_id = $1::uuid
`, [second.rows[0].purchase_intent_id]);
if (disabledCount.rows[0].count !== 0) throw new Error('default-off gate scheduled timer');
console.log('PRECHECKOUT_DELAYED_TIMER_DEFAULT_OFF_OK');

await db.exec(`
  update public.hotmart_abandonment_timer_policy_bindings
  set precheckout_first_touch_enabled = true,
      policy_key = 'precheckout-invalid-delay-test',
      policy_version = 1,
      generation = generation + 1
  where tenant_ref = 'lancemos' and funnel_ref = 'psicologajohanna';
`);
const third = payload('three');
let invalidDelayRejected = false;
try {
  await admit(third);
} catch (error) {
  invalidDelayRejected = String(error).includes(
    'precheckout_first_touch_delay_must_be_60_minutes',
  );
}
if (!invalidDelayRejected) throw new Error('invalid delay did not fail admission');
const rollback = await db.query(`
  select
    (select count(*)::integer from public.precheckout_submissions
     where external_submission_id = $1) as submissions,
    (select count(*)::integer from public.purchase_intents
     where normalized_email = $2) as intents
`, [third.id, third.canonical.identity.email]);
if (rollback.rows[0].submissions !== 0 || rollback.rows[0].intents !== 0) {
  throw new Error(`admission was not atomic: ${JSON.stringify(rollback.rows[0])}`);
}
console.log('PRECHECKOUT_DELAYED_TIMER_ATOMIC_ROLLBACK_OK');
