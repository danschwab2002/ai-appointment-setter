// Valida el link de pago del primer mensaje del carrito (20261018000200) contra
// la cadena completa de migraciones, ejecutando las RPC: reservar con el lease
// del despachador -> autorizar -> cerrar con el finalize del entrante -> la
// compra con ese sck cierra el caso y canjea el cupon.
//
// Lo que importa y no se ve leyendo el SQL:
//   * la fila recovery_first_touch no tiene conversacion ni disparador, pero
//     si cuenta, inbox e identidad: salen de la identidad del caso y tienen
//     que coincidir con el binding de la aliada del abandono;
//   * el link es uno por intento de envio: el reintento con el mismo intento
//     devuelve la misma fila aunque traiga otro ULID;
//   * la oferta es la del carrito (recovery_cases.offer_code) en el catalogo
//     del scope entrante del binding; la intencion, la correlacionada al
//     abandono, y con ella el sck del anuncio de su ficha;
//   * la compra entra por admit_and_correlate_hotmart_checkout_issuance_v2 (la
//     ruta del link, que no pasa por la admision por identidad): cierra el
//     caso ganado, cancela los toques pendientes y deja el cupon redeemed solo
//     si el coupon_code del pago es uno de ese caso;
//   * la autorizacion del entrante ya no acepta una fila sin conversacion por
//     la comparacion con NULL.
//
// Los casos se arman con filas minimas insertadas a mano (contacto, identidad,
// evento, binding, catalogo, caso, secuencia, acciones e intento), respetando
// los CHECK, en vez de pasar por la admision de Hotmart: lo que se prueba aca
// es la emision, su autorizacion y el cierre por la compra.
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
console.log('recovery_checkout_issuance_migration_apply=OK');

const fail = (message, detail) => {
  throw new Error(`${message}: ${JSON.stringify(detail)}`);
};
const expectError = async (label, fn, needle) => {
  try {
    await fn();
  } catch (error) {
    if (String(error.message ?? error).includes(needle)) return;
    fail(`${label} failed with another error`, String(error.message ?? error));
  }
  fail(`${label} did not fail`, needle);
};
const one = (rows, label) => {
  if (rows.length !== 1) fail(`${label} expected one row`, rows);
  return rows[0];
};

const ATT1 = {
  tenant: 'att1', funnel: 'att1-main', hotlink: 'ATT1HOTLINK', product: 123456,
  offer: 'att1offer', account: 42, inbox: 24, scope: 'att1-inbound', scopeVersion: 1,
};
const URL_SHAPE = new RegExp(
  '^https://pay[.]hotmart[.]com/ATT1HOTLINK[?]off=[A-Za-z0-9_-]+&checkoutMode=10'
  + '&src=hermes&sck=([A-Za-z0-9._%~-]+~)?hermes~v1~[0-7][0-9A-HJKMNP-TV-Z]{25}'
  + '(&fbclid=[A-Za-z0-9._-]+)?$',
);

// --- el binding, el scope del abandono y el catalogo -------------------------

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
for (const [landing, offerCode, isDefault] of [
  ['main', 'att1offer', true],
  ['promo', 'att1promo', false],
]) {
  await db.query(`
    insert into public.checkout_offer_catalog (
      tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref,
      offer_code, checkout_base_url, checkout_mode, default_for_inbound, status,
      version, approved_by, approved_at
    ) values ($1, $2, $3, $4, $5, $6, $7, $8, 10, $9, 'active', 1, 'operator-test', now())
  `, [ATT1.tenant, ATT1.funnel, ATT1.scope, ATT1.scopeVersion, ATT1.hotlink,
    landing, offerCode, `https://pay.hotmart.com/${ATT1.hotlink}`, isDefault]);
}
await db.exec(`
  insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
  ) values (
    'recovery-link-test', 1, 'published', 'cart_recovery', 'UTC',
    '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
    interval '60 minutes', interval '7 days', 3,
    '[{"step_key":"first_contact","mode":"approved_template"}]'::jsonb,
    'operator-test', now(), now()
  );
`);

// --- un carrito de ATT1 con su A1 tomado por el despachador ------------------

const NOW = Date.now();
const iso = (deltaSeconds = 0) => new Date(NOW + deltaSeconds * 1000).toISOString();
let seq = 0;
const makeCart = async ({ offerCode = 'att1promo', binding = true, form = null } = {}) => {
  seq += 1;
  const phone = `52155500000${String(seq).padStart(2, '0')}`;
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
  `, [`recovery-link-cart-${seq}`])).rows, 'event').id;
  if (binding) {
    await db.query(`
      insert into public.commercial_ally_hotmart_event_bindings (
        webhook_event_id, scope_id, tenant_ref, funnel_ref, binding_version,
        hotmart_product_id, purchase_intent_product_ref, offer_ref
      ) values ($1, $2, $3, $4, 1, $5, $6, $7)
    `, [event, scopeId, ATT1.tenant, ATT1.funnel, String(ATT1.product), ATT1.hotlink,
      ATT1.offer]);
  }
  const recoveryCase = one((await db.query(`
    insert into public.recovery_cases (
      contact_id, abandonment_event_id, external_product_id, product_name,
      offer_code, status, grace_expires_at, policy_key, policy_version,
      selected_channel_identity_id, identity_resolution_status
    ) values ($1, $2, $3, 'ATT1 Offer', $4, 'active', now(), 'recovery-link-test', 1,
              $5, 'resolved')
    returning id
  `, [contact, event, String(ATT1.product), offerCode, identity])).rows, 'case').id;
  let intent = null;
  if (form) {
    intent = one((await db.query(`
      insert into public.purchase_intents (
        tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
        normalized_phone, submitted_at, lifecycle_state,
        whatsapp_contact_authorized, provisional, provider_observed,
        activation_authorized
      ) values ($1, $2, 'promo', $3, 'att1promo', $4, now(), 'waiting_for_purchase',
                true, false, true, true)
      returning id
    `, [ATT1.tenant, ATT1.funnel, ATT1.hotlink, phone.replace(/^521/, '52')])).rows,
    'intent').id;
    const submission = one((await db.query(`
      insert into public.precheckout_submissions (
        external_submission_id, contract_version, raw_payload, canonical_payload,
        provisional, provider_observed, activation_authorized
      ) values ($1, '1.1.0', $2::jsonb, '{}'::jsonb, false, true, true)
      returning id
    `, [`recovery-link-form-${seq}`, JSON.stringify({ data: { attribution: form } })])).rows,
    'submission').id;
    await db.query(`
      insert into public.purchase_intent_submissions (purchase_intent_id, submission_id, ordinal)
      values ($1, $2, 1)
    `, [intent, submission]);
    await db.query(`
      insert into public.hotmart_purchase_intent_correlations (
        webhook_event_id, scope_id, event_type, outcome, purchase_intent_id,
        matched_by, candidate_count, reason_code, manual_handoff_required, observed_at
      ) values ($1, $2, 'PURCHASE_OUT_OF_SHOPPING_CART', 'resolved', $3, 'phone', 1,
                'exact_phone', false, now())
    `, [event, scopeId, intent]);
  }
  const sequence = one((await db.query(`
    insert into public.followup_sequences (
      recovery_case_id, reason, policy_key, policy_version, max_attempts
    ) values ($1, 'cart_abandonment', 'recovery-link-test', 1, 3)
    returning id
  `, [recoveryCase])).rows, 'sequence').id;
  const insertAction = async (stepKey, actionType, dueSeconds, lease) => one((await db.query(`
    insert into public.scheduled_actions (
      recovery_case_id, followup_sequence_id, action_type, status, due_at,
      expires_at, expected_case_version, idempotency_key, policy_key,
      policy_version, step_key, anchor_type, anchor_subject_internal_id,
      anchor_observed_at, lease_owner, lease_generation, lease_expires_at
    ) values (
      $1, $2, $3, 'pending', $4::timestamptz,
      $4::timestamptz + interval '7 days', 1, $5, 'recovery-link-test', 1,
      $6, 'cart_abandonment', $7, now(), $8, $9, $10::timestamptz
    ) returning id
  `, [recoveryCase, sequence, actionType, iso(dueSeconds), `recovery-link-${seq}-${stepKey}`,
    stepKey, event, lease ? 'worker-a' : null, lease ? 1 : 0,
    lease ? iso(300) : null])).rows, `action ${stepKey}`).id;
  const action = await insertAction('first_contact', 'first_contact_review', -60, true);
  const attempt = one((await db.query(`
    insert into public.followup_delivery_attempts (
      action_id, idempotency_key, attempt_number, channel, mode, phase,
      started_at, lease_generation, expected_case_version, expected_sequence_revision
    ) values ($1, $2, 1, 'whatsapp', 'approved_template', 'reserved', now(), 1, 1, 1)
    returning id
  `, [action, `recovery-link-attempt-${seq}`])).rows, 'attempt').id;
  // Una accion viva por secuencia: el toque siguiente se agrega cuando A1 ya
  // salio (addSecondTouch). Mientras tanto, el pendiente es A1.
  return {
    contact, identity, event, recoveryCase, sequence, action, second: action, attempt,
    intent, phone, insertAction,
  };
};

const RESERVE = `
  select * from public.reserve_recovery_checkout_issuance_v1(
    $1::uuid, $2, $3::bigint, $4::uuid, $5, $6::timestamptz
  )
`;
const reserve = async (cart, ulid, { worker = 'worker-a', generation = 1, at = iso() } = {}) => one(
  (await db.query(RESERVE, [cart.action, worker, generation, cart.attempt, ulid, at])).rows,
  'reserve',
);
const AUTHORIZE = `
  select * from public.authorize_recovery_checkout_issuance_v1(
    $1::uuid, $2::uuid, $3, $4::bigint, $5::timestamptz
  )
`;
const authorize = async (cart, issuanceId, { worker = 'worker-a', at = iso() } = {}) => one(
  (await db.query(AUTHORIZE, [issuanceId, cart.action, worker, 1, at])).rows,
  'authorize',
);
const issuanceRow = async (id) => one((await db.query(
  'select * from public.checkout_link_issuances where id = $1', [id],
)).rows, 'issuance');

// --- 1. la emision del carrito ------------------------------------------------

const ULID_A = '01K7REC0VERYAAAAAAAAAAAAAA';
const cartA = await makeCart({
  offerCode: 'att1promo',
  form: { sck: 'fb~tiroides~ad3~cpc~E02~120210000000000001', fbclid: 'IwAR0abc_DEF-1' },
});
const reservedA = await reserve(cartA, ULID_A);
const rowA = await issuanceRow(reservedA.issuance_id);
const offerA = one((await db.query(
  'select offer_code from public.checkout_offer_catalog where id = $1', [rowA.offer_catalog_id],
)).rows, 'offer');
if (reservedA.outcome !== 'reserved'
    || reservedA.sck_value !== `fb~tiroides~ad3~cpc~E02~120210000000000001~hermes~v1~${ULID_A}`
    || reservedA.checkout_url_final !== rowA.checkout_url_final
    || !URL_SHAPE.test(rowA.checkout_url_final)
    || !rowA.checkout_url_final.endsWith(`~hermes~v1~${ULID_A}&fbclid=IwAR0abc_DEF-1`)
    || rowA.source_kind !== 'recovery_first_touch'
    || rowA.recovery_case_id !== cartA.recoveryCase
    || rowA.commercial_case_id !== cartA.recoveryCase
    || rowA.delivery_attempt_id !== cartA.attempt
    || rowA.channel_identity_id !== cartA.identity
    || Number(rowA.chatwoot_account_id) !== ATT1.account
    || Number(rowA.chatwoot_inbox_id) !== ATT1.inbox
    || rowA.chatwoot_conversation_id !== null
    || rowA.trigger_external_message_id !== null
    || rowA.purchase_intent_id !== cartA.intent
    || rowA.source_submission_id === null
    || rowA.offer_resolution !== 'recovery_case_offer'
    || rowA.lead_offer_code !== 'att1promo'
    || offerA.offer_code !== 'att1promo'
    || rowA.attribution_resolution !== 'full'
    || rowA.status !== 'reserved') {
  fail('recovery issuance diverged', { reservedA, rowA, offerA });
}
console.log('recovery_checkout_issuance_reserved=OK');

// Sin ficha y con una oferta que no esta en el catalogo: la default, el
// marcador solo y una intencion fabricada para el telefono de la identidad.
const ULID_B = '01K7REC0VERYBBBBBBBBBBBBBB';
const cartB = await makeCart({ offerCode: 'not-in-catalog' });
const reservedB = await reserve(cartB, ULID_B);
const rowB = await issuanceRow(reservedB.issuance_id);
const intentB = one((await db.query(
  'select * from public.purchase_intents where id = $1', [rowB.purchase_intent_id],
)).rows, 'intent B');
if (reservedB.outcome !== 'reserved'
    || reservedB.sck_value !== `hermes~v1~${ULID_B}`
    || !URL_SHAPE.test(rowB.checkout_url_final)
    || !rowB.checkout_url_final.includes('?off=att1offer&')
    || rowB.offer_resolution !== 'default_offer_not_in_catalog'
    || rowB.attribution_resolution !== 'marker_only'
    || rowB.source_submission_id !== null
    || intentB.normalized_phone !== cartB.phone
    || intentB.offer_ref !== 'att1offer'
    || intentB.provider_observed !== false
    || intentB.lifecycle_state !== 'waiting_for_purchase') {
  fail('default recovery issuance diverged', { reservedB, rowB, intentB });
}
console.log('recovery_checkout_issuance_default_offer=OK');

// --- 2. idempotente por intento -------------------------------------------------

const replayA = await reserve(cartA, '01K7REC0VERYZZZZZZZZZZZZZZ');
const countA = Number(one((await db.query(
  'select count(*) n from public.checkout_link_issuances where recovery_case_id = $1',
  [cartA.recoveryCase],
)).rows, 'count').n);
if (replayA.outcome !== 'reserved' || replayA.issuance_id !== reservedA.issuance_id
    || replayA.checkout_url_final !== reservedA.checkout_url_final || countA !== 1) {
  fail('replay with the same attempt diverged', { replayA, countA });
}
console.log('recovery_checkout_issuance_idempotent=OK');

// --- 3. el cerco del lease ------------------------------------------------------

await expectError('foreign lease', () => reserve(cartA, ULID_A, { worker: 'worker-b' }),
  'current_action_lease_not_found');
await expectError('stale generation', () => reserve(cartA, ULID_A, { generation: 2 }),
  'current_action_lease_not_found');
await expectError('expired lease', () => reserve(cartA, ULID_A, { at: iso(600) }),
  'current_action_lease_not_found');
await expectError('authorize with a foreign lease',
  () => authorize(cartA, reservedA.issuance_id, { worker: 'worker-b' }),
  'current_action_lease_not_found');
await expectError('bad ulid', () => reserve(cartA, 'not-a-ulid'),
  'invalid_recovery_checkout_issuance_input');
console.log('recovery_checkout_issuance_lease_fence=OK');

// --- 4. lo que no se emite ni se autoriza --------------------------------------

const noBinding = await makeCart({ binding: false });
const noBindingResult = await reserve(noBinding, '01K7REC0VERYCCCCCCCCCCCCCC');
if (noBindingResult.outcome !== 'blocked_binding' || noBindingResult.issuance_id !== null) {
  fail('a cart without the ally binding got a link', noBindingResult);
}

// Comprado despues de reservar: no autoriza.
const bought = await makeCart();
const boughtReserved = await reserve(bought, '01K7REC0VERYDDDDDDDDDDDDDD');
const boughtPurchase = one((await db.query(`
  insert into public.webhook_events (source, external_event_id, event_type, payload, processing_status)
  values ('hotmart', 'recovery-link-other-purchase', 'PURCHASE_APPROVED', '{}'::jsonb, 'processed')
  returning id
`)).rows, 'other purchase').id;
await db.query(`
  update public.recovery_cases
  set status = 'won', won_at = now(), purchase_event_id = $2
  where id = $1
`, [bought.recoveryCase, boughtPurchase]);
const boughtAuth = await authorize(bought, boughtReserved.issuance_id);
if (boughtAuth.outcome !== 'blocked_case' || boughtAuth.status !== 'reserved') {
  fail('a bought case was authorized', boughtAuth);
}

// Dado de baja despues de reservar: no autoriza.
const optedOut = await makeCart();
const optedOutReserved = await reserve(optedOut, '01K7REC0VERYEEEEEEEEEEEEEE');
await db.query(`
  update public.contacts
  set contact_permission = 'opted_out', lifecycle_status = 'do_not_contact'
  where id = $1
`, [optedOut.contact]);
const optedOutAuth = await authorize(optedOut, optedOutReserved.issuance_id);
if (optedOutAuth.outcome !== 'blocked_contact' || optedOutAuth.status !== 'reserved') {
  fail('an opted-out contact was authorized', optedOutAuth);
}
// Y una reserva nueva para un contacto dado de baja tampoco sale.
const optedOutAgain = await makeCart();
await db.query(`
  update public.contacts set contact_permission = 'opted_out' where id = $1
`, [optedOutAgain.contact]);
const optedOutAgainResult = await reserve(optedOutAgain, '01K7REC0VERYFFFFFFFFFFFFFF');
if (optedOutAgainResult.outcome !== 'blocked_contact') {
  fail('an opted-out contact got a link', optedOutAgainResult);
}

// La autorizacion del entrante no acepta la fila del carrito.
const inboundAuth = one((await db.query(`
  select * from public.authorize_chatwoot_checkout_issuance_v2(
    $1::uuid, $2, $3, $4, 1, '1', now()
  )
`, [reservedA.issuance_id, cartA.phone, ATT1.account, ATT1.inbox])).rows, 'inbound authorize');
if (inboundAuth.outcome !== 'invalid_request'
    || (await issuanceRow(reservedA.issuance_id)).status !== 'reserved') {
  fail('the inbound authorization accepted a cart issuance', inboundAuth);
}
console.log('recovery_checkout_issuance_blocked=OK');

// --- 5. autorizar y cerrar con el finalize del entrante ------------------------

const authA = await authorize(cartA, reservedA.issuance_id);
const authAgain = await authorize(cartA, reservedA.issuance_id);
if (authA.outcome !== 'request_started' || authA.status !== 'request_started'
    || authAgain.outcome !== 'request_started_replay') {
  fail('authorization diverged', { authA, authAgain });
}
const finalized = one((await db.query(`
  select * from public.finalize_chatwoot_checkout_issuance_v2(
    $1::uuid, 'accepted_by_chatwoot', 9001, null, now()
  )
`, [reservedA.issuance_id])).rows, 'finalize');
const afterFinalize = await issuanceRow(reservedA.issuance_id);
if (finalized.outcome !== 'finalized' || afterFinalize.status !== 'accepted_by_chatwoot'
    || Number(afterFinalize.chatwoot_message_id) !== 9001) {
  fail('finalize diverged', { finalized, afterFinalize });
}
const replayAfterSend = await reserve(cartA, ULID_A);
if (replayAfterSend.outcome !== 'already_accepted'
    || replayAfterSend.issuance_id !== reservedA.issuance_id) {
  fail('replay after the send diverged', replayAfterSend);
}
// A1 salio: su accion queda aceptada y el toque siguiente (A2) pendiente.
await db.query(`
  update public.scheduled_actions
  set status = 'accepted_by_chatwoot', lease_owner = null, lease_expires_at = null
  where id = $1
`, [cartA.action]);
cartA.second = await cartA.insertAction('second_touch', 'no_reply_review', 86_400, false);
await expectError('identity change', () => db.query(`
  update public.checkout_link_issuances set delivery_attempt_id = $2 where id = $1
`, [reservedA.issuance_id, cartB.attempt]), 'checkout_link_issuance_immutable');
console.log('recovery_checkout_issuance_authorize_finalize=OK');

// --- 6. la compra con ese sck -----------------------------------------------------

const insertCoupon = async (cart, code, status) => db.query(`
  insert into public.recovery_coupons (
    recovery_case_id, ordinal, code, external_product_id, discount, expires_at,
    status, hotmart_start_ms, hotmart_end_ms, hotmart_coupon_id, verified_at
  ) values ($1, 1, $2, $3, 0.10, now() + interval '1 day', $4, 1, 2, 77,
            case when $4 = 'valid' then now() end)
`, [cart.recoveryCase, code, String(ATT1.product), status]);
await insertCoupon(cartA, 'ATTCOUPONAAAA1', 'valid');
await insertCoupon(cartB, 'ATTCOUPONBBBB1', 'valid');

const purchasePayload = (externalId, transaction, sck, couponCode) => ({
  id: externalId,
  creation_date: 1786147210000,
  event: 'PURCHASE_APPROVED',
  version: '2.0.0',
  data: {
    buyer: { email: 'recovery-link@example.test', checkout_phone: '5215550000001' },
    product: { id: ATT1.product, ucode: ATT1.hotlink },
    purchase: {
      status: 'APPROVED',
      transaction,
      approved_date: 1786147205000,
      offer: { code: 'att1promo', coupon_code: couponCode },
      origin: { sck },
    },
  },
});
const admitPurchase = async (externalId, transaction, sck, couponCode) => one((await db.query(`
  select * from public.admit_and_correlate_hotmart_checkout_issuance_v2(
    $1, $2::jsonb, $3, clock_timestamp()
  )
`, [externalId, JSON.stringify(purchasePayload(externalId, transaction, sck, couponCode)), sck])).rows,
'purchase admission');

const caseState = async (cart) => one((await db.query(`
  select rc.status, rc.purchase_event_id, rc.won_at, rc.version,
    (select status from public.followup_sequences where id = $2) sequence_status,
    (select status from public.scheduled_actions where id = $3) second_status,
    (select terminal_reason from public.scheduled_actions where id = $3) second_reason,
    (select status from public.recovery_coupons where recovery_case_id = rc.id) coupon_status,
    (select redeemed_webhook_event_id from public.recovery_coupons where recovery_case_id = rc.id) coupon_event,
    (select status from public.commercial_cases where id = rc.id) commercial_status
  from public.recovery_cases rc where rc.id = $1
`, [cart.recoveryCase, cart.sequence, cart.second])).rows, 'case state');

const beforeB = await caseState(cartB);
const purchaseA = await admitPurchase(
  'recovery-link-purchase-a', 'HPRECOVERYA0001', reservedA.sck_value, 'attcouponaaaa1',
);
const stateA = await caseState(cartA);
const intentA = one((await db.query(
  'select lifecycle_state from public.purchase_intents where id = $1', [cartA.intent],
)).rows, 'intent A');
const issuanceA = await issuanceRow(reservedA.issuance_id);
if (purchaseA.admission_outcome !== 'inserted'
    || purchaseA.correlation_outcome !== 'matched'
    || purchaseA.issuance_id !== reservedA.issuance_id
    || purchaseA.commercial_case_id !== cartA.recoveryCase
    || issuanceA.status !== 'purchase_matched'
    || intentA.lifecycle_state !== 'purchased'
    || stateA.status !== 'won'
    || stateA.purchase_event_id !== purchaseA.webhook_event_id
    || new Date(stateA.won_at).getTime() !== 1786147205000
    || stateA.sequence_status !== 'completed'
    || stateA.second_status !== 'cancelled'
    || stateA.second_reason !== 'purchase_detected'
    || stateA.coupon_status !== 'redeemed'
    || stateA.coupon_event !== purchaseA.webhook_event_id
    || stateA.commercial_status !== 'completed') {
  fail('the purchase with the cart link did not close the case', { purchaseA, stateA, intentA, issuanceA });
}
const eventsA = Number(one((await db.query(`
  select count(*) n from public.conversation_events
  where recovery_case_id = $1 and event_type = 'purchase_detected'
    and data ->> 'matched_by' = 'checkout_link'
`, [cartA.recoveryCase])).rows, 'events').n);
if (eventsA !== 1) fail('the purchase event was not recorded once', eventsA);

// La compra con el sck de A no toca a B.
const afterB = await caseState(cartB);
if (JSON.stringify(afterB) !== JSON.stringify(beforeB) || afterB.status !== 'active'
    || afterB.second_status !== 'pending' || afterB.coupon_status !== 'valid') {
  fail('the purchase of another case touched this one', { beforeB, afterB });
}

// El replay de la misma compra no cambia nada.
const replayPurchase = await admitPurchase(
  'recovery-link-purchase-a', 'HPRECOVERYA0001', reservedA.sck_value, 'attcouponaaaa1',
);
const stateAReplay = await caseState(cartA);
if (replayPurchase.admission_outcome !== 'duplicate'
    || replayPurchase.correlation_outcome !== 'replay'
    || stateAReplay.version !== stateA.version) {
  fail('the purchase replay changed the case', { replayPurchase, stateAReplay });
}

// B compra con su link (aunque nunca se autorizo) y un cupon que no es suyo:
// el caso se gana y su cupon queda como estaba.
const purchaseB = await admitPurchase(
  'recovery-link-purchase-b', 'HPRECOVERYB0001', reservedB.sck_value, 'ATTCOUPONAAAA1',
);
const stateB = await caseState(cartB);
if (purchaseB.correlation_outcome !== 'matched' || stateB.status !== 'won'
    || stateB.second_status !== 'cancelled' || stateB.coupon_status !== 'valid') {
  fail('the purchase with a foreign coupon diverged', { purchaseB, stateB });
}
console.log('recovery_checkout_issuance_purchase_closes_case=OK');

// --- 7. grants ----------------------------------------------------------------------

const acl = (await db.query(`
  select
    p.oid::regprocedure::text signature,
    p.prosecdef security_definer,
    array_to_string(p.proconfig, ',') config,
    has_function_privilege('anon', p.oid, 'execute') anon_x,
    has_function_privilege('authenticated', p.oid, 'execute') auth_x,
    has_function_privilege('service_role', p.oid, 'execute') service_x
  from pg_proc p
  where p.oid in (
    to_regprocedure('public.reserve_recovery_checkout_issuance_v1(uuid,text,bigint,uuid,text,timestamptz)'),
    to_regprocedure('public.authorize_recovery_checkout_issuance_v1(uuid,uuid,text,bigint,timestamptz)'),
    to_regprocedure('public._close_recovery_case_for_checkout_purchase_v1(uuid,uuid,timestamptz)')
  )
  order by 1
`)).rows;
const rpcs = acl.filter((row) => !row.signature.startsWith('_'));
const helper = acl.find((row) => row.signature.startsWith('_'));
if (acl.length !== 3
    || rpcs.some((row) => !row.security_definer || row.anon_x || row.auth_x || !row.service_x
      || row.config !== 'search_path=pg_catalog, public, pg_temp')
    || helper.security_definer || helper.anon_x || helper.auth_x || helper.service_x) {
  fail('function ACL diverged', acl);
}
console.log('recovery_checkout_issuance_grants=OK');

await db.close();
console.log('recovery_checkout_issuance=OK');
