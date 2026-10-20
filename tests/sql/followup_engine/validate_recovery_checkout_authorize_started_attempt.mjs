// Valida 20261020000100: la autorizacion del link de recuperacion
// (authorize_recovery_checkout_issuance_v1) acepta el intento ya arrancado.
//
// El bug (ATT1, 2026-10-20 02:07Z): el bridge autoriza el link DESPUES de
// marcar el inicio del pedido, con el intento en request_started, y la funcion
// de 20261018000200 exigia reserved: devolvia blocked_attempt siempre y el
// mensaje no salia.
//
// Prueba:
//   0. sin la migracion (la cadena hasta 20261019000800), un intento arrancado
//      da blocked_attempt: el bug, reproducido;
//   1. la migracion: cambia solo el cerco del intento de esa funcion (la
//      definicion nueva es la vieja con el reemplazo y nada mas), deja la
//      marca, conserva security definer, search_path y los permisos (solo
//      service_role), y correrla otra vez aborta con 55000 sin tocar nada;
//   2. con la migracion, el mismo intento arrancado da request_started y la
//      emision pasa a request_started; el reintento da request_started_replay;
//   3. un intento arrancado de un lease anterior sigue dando blocked_attempt;
//   4. un intento completed (delivery_unknown) sigue dando blocked_attempt;
//   5. con el intento todavia en reserved sigue autorizando (compatibilidad).
//
// El inicio del pedido aca se marca con un update del intento (phase y
// request_started_at, lo mismo que escribe el motor), porque estos casos se
// arman con filas minimas sin el piloto. El orden real del bridge con las RPC
// de arranque reales (reservar el link -> marcar el inicio -> autorizar) lo
// recorren B1 del pago rechazado (validate_portable_payment_failure_variants,
// mark_portable_payment_failure_request_started), B1 y su seguimiento
// (validate_payment_failure_followup) y Z del carrito
// (validate_recovery_followup_steps, mark_lancemos_pilot_request_started).
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const MIGRATION = '20261020000100_recovery_checkout_authorize_started_attempt_v1.sql';
const SIGNATURE = 'public.authorize_recovery_checkout_issuance_v1(uuid,uuid,text,bigint,timestamptz)';
const MARKER = 'recovery_checkout_authorize_started_attempt';
const OLD = `    if v_attempt.lease_generation <> p_lease_generation
       or v_attempt.phase <> 'reserved' then
        return query select 'blocked_attempt'::text, v_issuance.id, v_issuance.status;
`;

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const migrationsDir = join(root, 'supabase/migrations');
const migrationSql = readFileSync(join(migrationsDir, MIGRATION), 'utf8');
const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  grant usage on schema public to service_role, anon;
`);
const applyFile = async (file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
));
const migrations = readdirSync(migrationsDir).filter((name) => name.endsWith('.sql')).sort();
if (!migrations.includes(MIGRATION)) throw new Error(`${MIGRATION} is missing`);
await applyFile(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of migrations.filter((name) => name < MIGRATION)) {
  await applyFile(join(migrationsDir, name));
}
console.log('recovery_checkout_authorize_started_previous_chain_apply=OK');

const fail = (message, detail) => {
  throw new Error(`${message}: ${JSON.stringify(detail)}`);
};
const one = (rows, label) => {
  if (rows.length !== 1) fail(`${label} expected one row`, rows);
  return rows[0];
};
const definition = async () => one((await db.query(
  'select pg_get_functiondef($1::regprocedure) as def', [SIGNATURE],
)).rows, 'definition').def;
const occurrences = (text, needle) => text.split(needle).length - 1;

const ATT1 = {
  tenant: 'att1', funnel: 'att1-main', hotlink: 'ATT1HOTLINK', product: 123456,
  offer: 'att1offer', account: 42, inbox: 24, scope: 'att1-inbound', scopeVersion: 1,
};

// --- el binding, el scope del abandono, el catalogo y la politica ----------

await db.query(`
  insert into public.commercial_ally_runtime_bindings (
    tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
    lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
    product_name, product_price, currency, offer_code, consent_copy_version,
    hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
    inbound_scope_key, inbound_scope_version
  ) values (
    $1, $2, 1, 'active', 'ally-one', 'Ally One', 'ally-one-site', 'main',
    'ally-one.example', '/offer/main', $3, 'ATT1 Offer', '49', 'USD', $4,
    'att1-whatsapp-v1', $5, $6, $7, $8, $9
  )
`, [ATT1.tenant, ATT1.funnel, ATT1.hotlink, ATT1.offer, ATT1.product,
  ATT1.account, ATT1.inbox, ATT1.scope, ATT1.scopeVersion]);
const scopeId = one((await db.query(`
  insert into public.hotmart_purchase_intent_scopes (
    tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
    offer_ref, max_lookback, active
  ) values ($1, $2, $3, $4, $5, interval '7 days', true) returning id
`, [ATT1.tenant, ATT1.funnel, String(ATT1.product), ATT1.hotlink, ATT1.offer])).rows,
'scope').id;
await db.query(`
  insert into public.checkout_offer_catalog (
    tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref,
    offer_code, checkout_base_url, checkout_mode, default_for_inbound, status,
    version, approved_by, approved_at
  ) values ($1, $2, $3, $4, $5, 'main', $6, $7, 10, true, 'active', 1, 'operator-test', now())
`, [ATT1.tenant, ATT1.funnel, ATT1.scope, ATT1.scopeVersion, ATT1.hotlink, ATT1.offer,
  `https://pay.hotmart.com/${ATT1.hotlink}`]);
await db.exec(`
  insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
  ) values (
    'authorize-started-test', 1, 'published', 'cart_recovery', 'UTC',
    '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
    interval '60 minutes', interval '7 days', 3,
    '[{"step_key":"first_contact","mode":"approved_template"}]'::jsonb,
    'operator-test', now(), now()
  );
`);

// --- un carrito con su A1 tomado por el despachador y el link reservado ----

const NOW = Date.now();
const iso = (deltaSeconds = 0) => new Date(NOW + deltaSeconds * 1000).toISOString();
let seq = 0;
const makeCart = async () => {
  seq += 1;
  const phone = `52155500001${String(seq).padStart(2, '0')}`;
  const contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone)
    values ($1, $2, $3) returning id
  `, [`Buyer ${seq}`, `buyer${seq}@example.com`, phone])).rows, 'contact').id;
  const identity = one((await db.query(`
    insert into public.channel_identities (
      contact_id, channel, account_id, external_user_id, metadata
    ) values ($1, 'whatsapp', $2, $3, jsonb_build_object('inbox_id', $4::text))
    returning id
  `, [contact, `chatwoot:${ATT1.account}`, phone, String(ATT1.inbox)])).rows,
  'identity').id;
  const event = one((await db.query(`
    insert into public.webhook_events (
      source, external_event_id, event_type, payload, processing_status
    ) values ('hotmart', $1, 'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb, 'processed')
    returning id
  `, [`authorize-started-cart-${seq}`])).rows, 'event').id;
  await db.query(`
    insert into public.commercial_ally_hotmart_event_bindings (
      webhook_event_id, scope_id, tenant_ref, funnel_ref, binding_version,
      hotmart_product_id, purchase_intent_product_ref, offer_ref
    ) values ($1, $2, $3, $4, 1, $5, $6, $7)
  `, [event, scopeId, ATT1.tenant, ATT1.funnel, String(ATT1.product), ATT1.hotlink,
    ATT1.offer]);
  const recoveryCase = one((await db.query(`
    insert into public.recovery_cases (
      contact_id, abandonment_event_id, external_product_id, product_name,
      offer_code, status, grace_expires_at, policy_key, policy_version,
      selected_channel_identity_id, identity_resolution_status
    ) values ($1, $2, $3, 'ATT1 Offer', $4, 'active', now(), 'authorize-started-test', 1,
              $5, 'resolved')
    returning id
  `, [contact, event, String(ATT1.product), ATT1.offer, identity])).rows, 'case').id;
  const sequence = one((await db.query(`
    insert into public.followup_sequences (
      recovery_case_id, reason, policy_key, policy_version, max_attempts
    ) values ($1, 'cart_abandonment', 'authorize-started-test', 1, 3)
    returning id
  `, [recoveryCase])).rows, 'sequence').id;
  const action = one((await db.query(`
    insert into public.scheduled_actions (
      recovery_case_id, followup_sequence_id, action_type, status, due_at,
      expires_at, expected_case_version, idempotency_key, policy_key,
      policy_version, step_key, anchor_type, anchor_subject_internal_id,
      anchor_observed_at, lease_owner, lease_generation, lease_expires_at
    ) values (
      $1, $2, 'first_contact_review', 'pending', $3::timestamptz,
      $3::timestamptz + interval '7 days', 1, $4, 'authorize-started-test', 1,
      'first_contact', 'cart_abandonment', $5, now(), 'worker-a', 1, $6::timestamptz
    ) returning id
  `, [recoveryCase, sequence, iso(-60), `authorize-started-${seq}`, event, iso(300)])).rows,
  'action').id;
  const attempt = one((await db.query(`
    insert into public.followup_delivery_attempts (
      action_id, idempotency_key, attempt_number, channel, mode, phase,
      started_at, lease_generation, expected_case_version, expected_sequence_revision
    ) values ($1, $2, 1, 'whatsapp', 'approved_template', 'reserved', now(), 1, 1, 1)
    returning id
  `, [action, `authorize-started-attempt-${seq}`])).rows, 'attempt').id;
  const reserved = one((await db.query(`
    select * from public.reserve_recovery_checkout_issuance_v1(
      $1::uuid, 'worker-a', 1::bigint, $2::uuid, $3, $4::timestamptz
    )
  `, [action, attempt, `01K7STARTED${String(seq).padStart(15, '0')}`, iso()])).rows, 'reserve');
  if (reserved.outcome !== 'reserved') fail('reserve', reserved);
  return { action, attempt, issuance: reserved.issuance_id };
};
// Lo que escribe el motor al marcar el inicio del pedido
// (mark_followup_request_started y sus variantes).
const markStarted = (cart) => db.query(`
  update public.followup_delivery_attempts
  set phase = 'request_started', request_started_at = clock_timestamp()
  where id = $1 and phase = 'reserved'
`, [cart.attempt]);
const authorize = async (cart, { generation = 1 } = {}) => one((await db.query(`
  select * from public.authorize_recovery_checkout_issuance_v1(
    $1::uuid, $2::uuid, 'worker-a', $3::bigint, $4::timestamptz
  )
`, [cart.issuance, cart.action, generation, iso()])).rows, 'authorize');
const issuanceStatus = async (cart) => one((await db.query(
  'select status from public.checkout_link_issuances where id = $1', [cart.issuance],
)).rows, 'issuance').status;
const results = {};

// --- 0. el bug, sin la migracion ----------------------------------------------

const started = await makeCart();
await markStarted(started);
const before = await authorize(started);
if (before.outcome !== 'blocked_attempt' || (await issuanceStatus(started)) !== 'reserved') {
  fail('without the migration a started attempt should be blocked (the bug)', before);
}
results.without_migration = before.outcome;
console.log('recovery_checkout_authorize_started_bug_reproduced=OK');

// --- 1. la migracion -------------------------------------------------------------

const oldDefinition = await definition();
if (occurrences(oldDefinition, OLD) !== 1 || oldDefinition.includes(MARKER)) {
  fail('the live definition before the migration diverged', { found: occurrences(oldDefinition, OLD) });
}
await db.exec(migrationSql);
const newDefinition = await definition();
const blockStart = migrationSql.indexOf('$new$') + '$new$'.length;
const expectedNew = migrationSql.slice(blockStart, migrationSql.indexOf('$new$', blockStart));
if (newDefinition !== oldDefinition.replace(OLD, expectedNew)
    || occurrences(newDefinition, OLD) !== 0
    || occurrences(newDefinition, MARKER) !== 1
    || !newDefinition.includes("or v_attempt.phase not in ('reserved', 'request_started') then")) {
  fail('the migration changed more than the attempt fence', { newDefinition });
}
const acl = one((await db.query(`
  select
    p.prosecdef security_definer,
    array_to_string(p.proconfig, ',') config,
    has_function_privilege('anon', p.oid, 'execute') anon_x,
    has_function_privilege('authenticated', p.oid, 'execute') auth_x,
    has_function_privilege('service_role', p.oid, 'execute') service_x
  from pg_proc p
  where p.oid = $1::regprocedure
`, [SIGNATURE])).rows, 'acl');
if (!acl.security_definer || acl.anon_x || acl.auth_x || !acl.service_x
    || acl.config !== 'search_path=pg_catalog, public, pg_temp') {
  fail('function ACL diverged', acl);
}
let rerun = null;
try {
  await db.exec(migrationSql);
} catch (error) {
  rerun = error;
}
if (rerun === null || rerun.code !== '55000'
    || !String(rerun.message).includes('recovery_checkout_authorize_started_already_applied')) {
  fail('running the migration twice did not abort with 55000', String(rerun?.message ?? rerun));
}
await db.exec('rollback').catch(() => {});
if ((await definition()) !== newDefinition) fail('the aborted rerun changed the function', {});
// Las migraciones que vengan despues, para que los casos corran sobre la cadena
// completa.
for (const name of migrations.filter((name) => name > MIGRATION)) {
  await applyFile(join(migrationsDir, name));
}
results.migration = { replaced: 1, marker: 1, rerun: `${rerun.code}:already_applied` };
console.log('recovery_checkout_authorize_started_migration=OK');

// --- 2. el intento arrancado autoriza ------------------------------------------

const after = await authorize(started);
const replay = await authorize(started);
if (after.outcome !== 'request_started' || after.status !== 'request_started'
    || (await issuanceStatus(started)) !== 'request_started'
    || replay.outcome !== 'request_started_replay') {
  fail('a started attempt did not authorize', { after, replay });
}
results.started_attempt = `${after.outcome}, replay ${replay.outcome}`;
console.log('recovery_checkout_authorize_started_attempt=OK');

// --- 3. un intento arrancado de un lease anterior --------------------------------

const staleLease = await makeCart();
await markStarted(staleLease);
// Otro worker tomo la accion con el lease 2; el intento sigue siendo del 1.
await db.query(`
  update public.scheduled_actions set lease_generation = 2 where id = $1
`, [staleLease.action]);
const stale = await authorize(staleLease, { generation: 2 });
if (stale.outcome !== 'blocked_attempt' || (await issuanceStatus(staleLease)) !== 'reserved') {
  fail('a started attempt of another lease authorized', stale);
}
results.other_lease = stale.outcome;
console.log('recovery_checkout_authorize_started_other_lease=OK');

// --- 4. un intento completed ----------------------------------------------------

const completed = await makeCart();
await db.query(`
  update public.followup_delivery_attempts
  set phase = 'completed', outcome = 'delivery_unknown',
      request_started_at = clock_timestamp(), reason_code = 'test_completed',
      reconciliation_deadline = clock_timestamp() + interval '15 minutes'
  where id = $1
`, [completed.attempt]);
const closed = await authorize(completed);
if (closed.outcome !== 'blocked_attempt' || (await issuanceStatus(completed)) !== 'reserved') {
  fail('a completed attempt authorized', closed);
}
results.completed_attempt = closed.outcome;
console.log('recovery_checkout_authorize_started_completed=OK');

// --- 5. compatibilidad: el intento todavia en reserved --------------------------

const reservedCart = await makeCart();
const early = await authorize(reservedCart);
if (early.outcome !== 'request_started' || (await issuanceStatus(reservedCart)) !== 'request_started') {
  fail('a reserved attempt no longer authorizes', early);
}
results.reserved_attempt = early.outcome;
console.log('recovery_checkout_authorize_started_reserved_compat=OK');

await db.close();
console.log(JSON.stringify(results));
console.log('recovery_checkout_authorize_started=OK');
