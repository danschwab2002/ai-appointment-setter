// B1-cupon usa solo un cupon de otro caso que le llego a la persona (migracion
// 20261019000500), con las RPC reales sobre la cadena completa de migraciones.
//
// Prueba:
//   0. la migracion: agrega solo recovery_coupon_was_delivered(uuid), que nadie
//      de la API ejecuta; cambia solo payment_failure_wants_first_coupon y
//      get_portable_payment_failure_send_context_v1, y cada una es su
//      definicion anterior con el reemplazo documentado y nada mas (mismos
//      permisos); aplicarla otra vez aborta sin cambiar nada;
//   1. el cupon del carrito valid, sin A1: B1 no lo ve (el contexto de envio
//      no da otro cupon y el saldo pide el suyo);
//   2. el link de A1 reservado y autorizado, sin respuesta de Chatwoot: igual;
//   3. Chatwoot no confirmo (delivery_unknown): igual;
//   4. Chatwoot lo acepto: B1 ve el cupon (codigo y vencimiento) y el saldo
//      deja de pedir el suyo;
//   5. un link de A1 emitido antes de que el cupon quedara valid no prueba
//      que el cupon llego;
//   6. un cupon con un link aceptado, pero de un caso del pago rechazado (el
//      link de B1 sale con o sin cupon), no cuenta.
//
// Datos: los casos se arman con filas minimas insertadas a mano (contacto,
// identidad, evento, binding, catalogo, caso, secuencia, accion e intento),
// respetando los CHECK, como validate_recovery_checkout_issuance.mjs, de donde
// salen el binding, el catalogo y la forma del carrito. El link de A1 pasa por
// las RPC reales (reserva, autorizacion y cierre del entrante). El rechazo es
// un PURCHASE_CANCELED con el motivo y el medio tal como llegan en
// tests/fixtures/hotmart_purchase_canceled_att1_20261019.json ('Saldo
// insuficiente.', CREDIT_CARD).
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
  grant usage on schema public to service_role, anon;
`);

const fail = (message, detail) => {
  throw new Error(`${message}: ${JSON.stringify(detail)}`);
};
const one = (rows, label) => {
  if (rows.length !== 1) fail(`${label} expected one row`, rows);
  return rows[0];
};
const results = {};

// ---------------------------------------------------------------------------
// 0. La migracion.
// ---------------------------------------------------------------------------
const TARGET = '20261019000500_payment_failure_other_coupon_delivered_v1.sql';
const migrationNames = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const targetIndex = migrationNames.indexOf(TARGET);
if (targetIndex < 0) fail('target migration missing', TARGET);
const sqlOf = (file) => readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
);
const snapshot = async () => new Map((await db.query(`
  select p.oid::regprocedure::text as signature,
         pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));

await db.exec(sqlOf(join(root, 'supabase/baseline/20260803_public_schema.sql')));
for (const name of migrationNames.slice(0, targetIndex)) {
  await db.exec(sqlOf(join(root, 'supabase/migrations', name)));
}
const before = await snapshot();
await db.exec(sqlOf(join(root, 'supabase/migrations', TARGET)));
const after = await snapshot();
for (const name of migrationNames.slice(targetIndex + 1)) {
  await db.exec(sqlOf(join(root, 'supabase/migrations', name)));
}
// La cadena completa: la segunda corrida se compara contra esto, no contra
// `after`, porque una migracion posterior puede redefinir otras funciones.
const settled = await snapshot();

const HELPER = 'recovery_coupon_was_delivered(uuid)';
const PATCHED = [
  'payment_failure_wants_first_coupon(uuid)',
  'get_portable_payment_failure_send_context_v1(uuid,text,bigint,timestamp with time zone)',
];
const OLD = "and coupon.status = 'valid'\n";
const NEW = "and coupon.status = 'valid'\n"
  + '          and public.recovery_coupon_was_delivered(coupon.id)\n';
const added = [...after.keys()].filter((signature) => !before.has(signature));
const removed = [...before.keys()].filter((signature) => !after.has(signature));
const changed = [...before.keys()].filter((signature) => after.has(signature)
  && after.get(signature).definition !== before.get(signature).definition);
if (JSON.stringify(added) !== JSON.stringify([HELPER]) || removed.length !== 0
    || JSON.stringify([...changed].sort()) !== JSON.stringify([...PATCHED].sort())) {
  fail('migration footprint', { added, removed, changed });
}
const helper = after.get(HELPER);
if (helper.service_x || helper.api_x) fail('helper is executable from the API', helper);
for (const signature of PATCHED) {
  const old = before.get(signature);
  const now = after.get(signature);
  if (old.definition.split(OLD).length - 1 !== 1
      || now.definition !== old.definition.replace(OLD, NEW)
      || now.acl !== old.acl || now.service_x !== old.service_x || now.api_x !== old.api_x) {
    fail(`patch of ${signature}`, { acl: [old.acl, now.acl] });
  }
}
// Aplicarla otra vez aborta y no cambia nada.
let rerun = null;
try {
  await db.exec(sqlOf(join(root, 'supabase/migrations', TARGET)));
} catch (error) {
  rerun = String(error.message ?? error);
}
await db.exec('rollback').catch(() => {});
const afterRerun = await snapshot();
if (rerun === null
    || [...settled.keys()].some((signature) => afterRerun.get(signature)?.definition
      !== settled.get(signature).definition)) {
  fail('rerun', rerun);
}
results.migration = {
  added: HELPER, patched: PATCHED.length, helper_api_execute: false, rerun_aborts: true,
};

// ---------------------------------------------------------------------------
// El binding, el scope del abandono, el catalogo y la politica (los de
// validate_recovery_checkout_issuance.mjs).
// ---------------------------------------------------------------------------
const ATT1 = {
  tenant: 'att1', funnel: 'att1-main', hotlink: 'ATT1HOTLINK', product: 123456,
  offer: 'att1offer', account: 42, inbox: 24, scope: 'att1-inbound', scopeVersion: 1,
};
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
    'coupon-delivered-test', 1, 'published', 'cart_recovery', 'UTC',
    '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
    interval '60 minutes', interval '7 days', 3,
    '[{"step_key":"first_contact","mode":"approved_template"}]'::jsonb,
    'operator-test', now(), now()
  );
`);

const NOW = Date.now();
const iso = (deltaSeconds = 0) => new Date(NOW + deltaSeconds * 1000).toISOString();
let seq = 0;

const makePerson = async () => {
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
  return { contact, identity };
};

// Un caso con su secuencia y su primera accion tomada por el despachador.
const makeCase = async (person, { anchor, eventType, payload = {}, context = {} }) => {
  seq += 1;
  const event = one((await db.query(`
    insert into public.webhook_events (
      source, external_event_id, event_type, payload, processing_status
    ) values ('hotmart', $1, $2, $3::jsonb, 'processed')
    returning id
  `, [`coupon-delivered-${seq}`, eventType, JSON.stringify(payload)])).rows, 'event').id;
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
      selected_channel_identity_id, identity_resolution_status, context
    ) values ($1, $2, $3, 'ATT1 Offer', $4, 'active', now(), 'coupon-delivered-test', 1,
              $5, 'resolved', $6::jsonb)
    returning id
  `, [person.contact, event, String(ATT1.product), ATT1.offer, person.identity,
    JSON.stringify({ trigger_kind: anchor, ...context })])).rows, 'case').id;
  const sequence = one((await db.query(`
    insert into public.followup_sequences (
      recovery_case_id, reason, policy_key, policy_version, max_attempts
    ) values ($1, $2, 'coupon-delivered-test', 1, 3)
    returning id
  `, [recoveryCase, anchor])).rows, 'sequence').id;
  const action = one((await db.query(`
    insert into public.scheduled_actions (
      recovery_case_id, followup_sequence_id, action_type, status, due_at,
      expires_at, expected_case_version, idempotency_key, policy_key,
      policy_version, step_key, anchor_type, anchor_subject_internal_id,
      anchor_observed_at, lease_owner, lease_generation, lease_expires_at
    ) values (
      $1, $2, 'first_contact_review', 'pending', $3::timestamptz,
      $3::timestamptz + interval '7 days', 1, $4, 'coupon-delivered-test', 1,
      'first_contact', $5, $6, now(), 'worker-a', 1, $7::timestamptz
    ) returning id
  `, [recoveryCase, sequence, iso(-60), `coupon-delivered-${seq}-first`, anchor, event,
    iso(300)])).rows, 'action').id;
  const attempt = one((await db.query(`
    insert into public.followup_delivery_attempts (
      action_id, idempotency_key, attempt_number, channel, mode, phase,
      started_at, lease_generation, expected_case_version, expected_sequence_revision
    ) values ($1, $2, 1, 'whatsapp', 'approved_template', 'reserved', now(), 1, 1, 1)
    returning id
  `, [action, `coupon-delivered-attempt-${seq}`])).rows, 'attempt').id;
  return { event, recoveryCase, action, attempt };
};

const addCoupon = async (recoveryCase, code, expiresAt, verifiedAt) => one((await db.query(`
  insert into public.recovery_coupons
    (recovery_case_id, ordinal, code, external_product_id, discount, expires_at, status,
     hotmart_start_ms, hotmart_end_ms, verified_at)
  values ($1, 1, $2, $3, 0.10, $4, 'valid', $5, $6, $7)
  returning id
`, [recoveryCase, code, String(ATT1.product), expiresAt,
  NOW - 4 * 3_600_000, new Date(expiresAt).getTime() - 3 * 3_600_000, verifiedAt])).rows,
'coupon').id;

// El link del primer toque por las RPC reales: reserva, autorizacion y,
// opcionalmente, el cierre del entrante.
const issueLink = async (theCase, ulid) => {
  const reserved = one((await db.query(`
    select * from public.reserve_recovery_checkout_issuance_v1(
      $1::uuid, 'worker-a', 1::bigint, $2::uuid, $3, $4::timestamptz
    )
  `, [theCase.action, theCase.attempt, ulid, iso()])).rows, 'reserve');
  if (reserved.outcome !== 'reserved') fail('reserve', reserved);
  const authorized = one((await db.query(`
    select * from public.authorize_recovery_checkout_issuance_v1(
      $1::uuid, $2::uuid, 'worker-a', 1::bigint, $3::timestamptz
    )
  `, [reserved.issuance_id, theCase.action, iso()])).rows, 'authorize');
  if (authorized.outcome !== 'request_started') fail('authorize', authorized);
  return reserved.issuance_id;
};
// delivery_unknown lleva su motivo y ningun mensaje, como lo cierra el
// despachador cuando Chatwoot no contesto.
const finalizeLink = async (issuanceId, status, messageId) => {
  const finalized = one((await db.query(`
    select * from public.finalize_chatwoot_checkout_issuance_v2(
      $1::uuid, $2, $3, $4, now()
    )
  `, [issuanceId, status, messageId,
    status === 'delivery_unknown' ? 'chatwoot_send_unknown' : null])).rows, 'finalize');
  if (finalized.outcome !== 'finalized') fail(`finalize ${status}`, finalized);
};

// El rechazo por saldo de la misma persona, con su accion tomada.
const makeRefusal = async (person) => {
  const createdAt = NOW - 20 * 60_000;
  const refusal = await makeCase(person, {
    anchor: 'payment_failure',
    eventType: 'PURCHASE_CANCELED',
    payload: {
      creation_date: createdAt,
      data: { purchase: { payment: { type: 'CREDIT_CARD' } } },
    },
  });
  await db.query(`
    insert into public.commercial_ally_payment_failure_details (
      webhook_event_id, transaction_ref, refusal_reason
    ) values ($1, $2, 'Saldo insuficiente.')
  `, [refusal.event, `HP-coupon-delivered-${seq}`]);
  await db.query(`
    update public.recovery_cases
    set context = context || jsonb_build_object(
      'latest_payment_failure_event_id', $2::text, 'payment_failure_count', 1)
    where id = $1
  `, [refusal.recoveryCase, refusal.event]);
  return refusal;
};
const b1View = async (refusal) => {
  const context = one((await db.query(`
    select * from public.get_portable_payment_failure_send_context_v1(
      $1::uuid, 'worker-a', 1::bigint, $2::timestamptz
    )
  `, [refusal.action, iso()])).rows, 'send context');
  const wants = one((await db.query(
    'select public.payment_failure_wants_first_coupon($1) as wants', [refusal.recoveryCase],
  )).rows, 'wants').wants;
  if (context.refusal_kind !== 'insufficient_funds' || context.payment_failure_count !== 1) {
    fail('send context of the refusal', context);
  }
  return { code: context.other_coupon_code, expires: context.other_coupon_expires_at, wants };
};
const expectHidden = async (refusal, label) => {
  const view = await b1View(refusal);
  if (view.code !== null || view.expires !== null || view.wants !== true) fail(label, view);
  return 'hidden; the refusal asks for its own coupon';
};

// ---------------------------------------------------------------------------
// 1 a 4. El cupon del carrito, paso a paso hasta que Chatwoot acepta A1.
// ---------------------------------------------------------------------------
{
  const person = await makePerson();
  const cart = await makeCase(person, {
    anchor: 'cart_abandonment', eventType: 'PURCHASE_OUT_OF_SHOPPING_CART',
  });
  const expiresAt = iso(20 * 3600);
  await addCoupon(cart.recoveryCase, 'ATT1CARRITO0001', expiresAt, iso(-120));
  const refusal = await makeRefusal(person);

  results.valid_without_a1 = await expectHidden(refusal, 'coupon without A1');
  const issuance = await issueLink(cart, '01K7C0VP0NAAAAAAAAAAAAAAAA');
  results.link_request_started = await expectHidden(refusal, 'link request_started');
  await finalizeLink(issuance, 'delivery_unknown', null);
  results.delivery_unknown = await expectHidden(refusal, 'delivery_unknown');
  await finalizeLink(issuance, 'accepted_by_chatwoot', 9001);
  const view = await b1View(refusal);
  if (view.code !== 'ATT1CARRITO0001' || view.expires.getTime() !== new Date(expiresAt).getTime()
      || view.wants !== false) {
    fail('accepted A1', view);
  }
  results.accepted_by_chatwoot = 'B1 sees the coupon; the refusal does not ask for its own';
}

// ---------------------------------------------------------------------------
// 5. Un link de A1 anterior a que el cupon quedara valid.
// ---------------------------------------------------------------------------
{
  const person = await makePerson();
  const cart = await makeCase(person, {
    anchor: 'cart_abandonment', eventType: 'PURCHASE_OUT_OF_SHOPPING_CART',
  });
  const issuance = await issueLink(cart, '01K7C0VP0NBBBBBBBBBBBBBBBB');
  await finalizeLink(issuance, 'accepted_by_chatwoot', 9002);
  await addCoupon(cart.recoveryCase, 'ATT1CARRITO0002', iso(20 * 3600), iso(60));
  const refusal = await makeRefusal(person);
  results.link_before_valid = await expectHidden(refusal, 'link before the coupon was valid');
}

// ---------------------------------------------------------------------------
// 6. Un cupon con su link aceptado, de un caso del pago rechazado.
// ---------------------------------------------------------------------------
{
  const person = await makePerson();
  const earlier = await makeRefusal(person);
  await addCoupon(earlier.recoveryCase, 'ATT1SALDO00001', iso(20 * 3600), iso(-120));
  const issuance = await issueLink(earlier, '01K7C0VP0NCCCCCCCCCCCCCCCC');
  await finalizeLink(issuance, 'accepted_by_chatwoot', 9003);
  const refusal = await makeRefusal(person);
  results.payment_failure_coupon = await expectHidden(refusal, 'coupon of a refusal case');
}

console.log(JSON.stringify(results, null, 2));
