// Valida que una compra con nuestro link cuenta como recuperada siempre
// (20261028000400, E-169), contra la cadena completa de migraciones.
//
// Regla de Dan (2026-10-28): una compra con nuestro sck (hermes~v1~<ulid>) gana
// el caso de su link aunque la secuencia haya terminado o el caso este
// cerrado, y la emision queda purchase_matched aunque su intencion ya no
// espere. Lo que se prueba:
//   * ROJO A PROPOSITO, con la cadena hasta la migracion anterior: el caso de
//     un solo toque (sequence_exhausted) que compra con su link queda sin
//     ganar, y la emision cuya intencion ya compro devuelve
//     purchase_already_approved y no queda comprada. Es lo que se midio en ATT1
//     (1 caso del carrito en sequence_exhausted con su link comprado);
//   * al aplicar la migracion, lo que ya paso se arregla: ese caso queda won
//     con su closed_at de antes y el estado previo en el contexto;
//   * despues: sequence_exhausted, cancelled y expired ganan con
//     'won_after_close'; abierto sigue ganando como hoy; un caso ya ganado por
//     otra compra no se toca; la compra de un link del agente (sin caso) solo
//     marca la emision; la intencion cancelada o ya comprada no frena la
//     emision y la intencion no cambia;
//   * correrla dos veces aborta, y el helper sigue sin permisos.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const MIGRATION = '20261028000400_purchase_by_our_link_counts_v1.sql';
const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  grant usage on schema public to service_role, anon;
`);
const migrations = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const position = migrations.indexOf(MIGRATION);
if (position < 0) throw new Error(`falta ${MIGRATION}`);
const apply = async (file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
));
await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of migrations.slice(0, position)) {
  await apply(join(root, 'supabase/migrations', name));
}
console.log('purchase_by_our_link_counts_chain_before=OK');

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

const insertCoupon = async (cart, code, status) => db.query(`
  insert into public.recovery_coupons (
    recovery_case_id, ordinal, code, external_product_id, discount, expires_at,
    status, hotmart_start_ms, hotmart_end_ms, hotmart_coupon_id, verified_at
  ) values ($1, 1, $2, $3, 0.10, now() + interval '1 day', $4, 1, 2, 77,
            case when $4 = 'valid' then now() end)
`, [cart.recoveryCase, code, String(ATT1.product), status]);

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

// --- armar un caso con su link entregado, y cerrarlo como lo deja el legado ----

let ulidSeq = 0;
const nextUlid = () => {
  ulidSeq += 1;
  return `01K7PAGAD0SNTS${String(ulidSeq).padStart(12, '0')}`;
};
const sentCart = async (options = {}) => {
  const cart = await makeCart(options);
  const reserved = await reserve(cart, nextUlid());
  await authorize(cart, reserved.issuance_id);
  one((await db.query(`
    select * from public.finalize_chatwoot_checkout_issuance_v2(
      $1::uuid, 'accepted_by_chatwoot', 9001, null, now()
    )
  `, [reserved.issuance_id])).rows, 'finalize');
  await db.query(`
    update public.scheduled_actions
    set status = 'accepted_by_chatwoot', lease_owner = null, lease_expires_at = null
    where id = $1
  `, [cart.action]);
  return { ...cart, reserved };
};
// Como lo cierra el legado: un solo toque que salio (sequence_exhausted), o
// cancelado / vencido, con la secuencia terminada y sin acciones vivas.
const closeCase = async (cart, status) => {
  await db.query(`
    update public.followup_sequences
    set status = 'completed', completion_reason = 'max_attempts_reached', completed_at = now()
    where id = $1
  `, [cart.sequence]);
  await db.query(`
    update public.recovery_cases
    set status = $2, closed_at = now() - interval '2 hours', version = version + 1
    where id = $1
  `, [cart.recoveryCase, status]);
  return one((await db.query(
    'select closed_at from public.recovery_cases where id = $1', [cart.recoveryCase],
  )).rows, 'closed').closed_at;
};
const caseRow = async (cart) => one((await db.query(`
  select rc.status, rc.purchase_event_id, rc.won_at, rc.closed_at, rc.lead_stage,
         rc.context ->> 'purchase_matched_by' matched_by,
         rc.context ->> 'purchase_case_status_before' status_before,
         (select status from public.commercial_cases cc where cc.id = rc.id) commercial_status,
         (select count(*)::int from public.conversation_events ce
           where ce.recovery_case_id = rc.id and ce.event_type = 'purchase_detected'
             and ce.data ->> 'matched_by' = 'checkout_link') events,
         (select ce.data ->> 'case_status_before' from public.conversation_events ce
           where ce.recovery_case_id = rc.id and ce.event_type = 'purchase_detected'
           order by ce.created_at desc limit 1) event_status_before
  from public.recovery_cases rc where rc.id = $1
`, [cart.recoveryCase])).rows, 'case row');
let transactionSeq = 0;
const buy = async (cart, sck, coupon = null) => {
  transactionSeq += 1;
  return admitPurchase(`link-counts-purchase-${transactionSeq}`,
    `HPLINKCOUNTS${String(transactionSeq).padStart(4, '0')}`, sck ?? cart.reserved.sck_value, coupon);
};
const closeHelper = async (issuanceId, eventId) => one((await db.query(`
  select public._close_recovery_case_for_checkout_purchase_v1($1::uuid, $2::uuid, now()) outcome
`, [issuanceId, eventId])).rows, 'helper').outcome;

// --- 1. ROJO A PROPOSITO, con la cadena hasta la migracion anterior -----------

const pastExhausted = await sentCart();
const pastClosedAt = await closeCase(pastExhausted, 'sequence_exhausted');
await insertCoupon(pastExhausted, 'LINKPAST00001', 'valid');
const pastPurchase = await buy(pastExhausted, null, 'linkpast00001');
const pastBefore = await caseRow(pastExhausted);
const pastIssuance = await issuanceRow(pastExhausted.reserved.issuance_id);
if (pastPurchase.correlation_outcome !== 'matched' || pastIssuance.status !== 'purchase_matched'
    || pastBefore.status !== 'sequence_exhausted' || pastBefore.purchase_event_id !== null) {
  fail('before the migration, the exhausted case should stay without the purchase',
    { pastPurchase, pastBefore, issuance: pastIssuance.status });
}

// Una emision cuya intencion ya compro (por otra emision que la comparte):
// la compra con su link, en otra transaccion, no quedaba comprada.
const pastIntentBought = await sentCart();
await db.query(`
  update public.purchase_intents set lifecycle_state = 'purchased' where id = (
    select purchase_intent_id from public.checkout_link_issuances where id = $1)
`, [pastIntentBought.reserved.issuance_id]);
const pastIntentPurchase = await buy(pastIntentBought);
if (pastIntentPurchase.correlation_outcome !== 'purchase_already_approved'
    || (await issuanceRow(pastIntentBought.reserved.issuance_id)).status === 'purchase_matched') {
  fail('before the migration, a link whose intent already bought should not count',
    { pastIntentPurchase });
}
console.log('purchase_by_our_link_counts_red_before=OK');

// --- 2. la migracion, y lo que ya paso ------------------------------------------

await apply(join(root, 'supabase/migrations', MIGRATION));
for (const name of migrations.slice(position + 1)) {
  await apply(join(root, 'supabase/migrations', name));
}
const pastAfter = await caseRow(pastExhausted);
const pastCoupon = one((await db.query(
  'select status, redeemed_webhook_event_id from public.recovery_coupons where recovery_case_id = $1',
  [pastExhausted.recoveryCase],
)).rows, 'past coupon');
if (pastAfter.status !== 'won' || pastAfter.lead_stage !== 'won'
    || pastAfter.purchase_event_id !== pastPurchase.webhook_event_id
    || new Date(pastAfter.won_at).getTime() !== 1786147205000
    || new Date(pastAfter.closed_at).getTime() !== new Date(pastClosedAt).getTime()
    || pastAfter.matched_by !== 'checkout_link'
    || pastAfter.status_before !== 'sequence_exhausted'
    || pastAfter.event_status_before !== 'sequence_exhausted'
    || pastAfter.events !== 1
    || pastAfter.commercial_status !== 'completed'
    || pastCoupon.status !== 'redeemed'
    || pastCoupon.redeemed_webhook_event_id !== pastPurchase.webhook_event_id) {
  fail('the migration did not count the past purchase', { pastAfter, pastCoupon });
}
// La que nunca se marco (purchase_already_approved) no se reconstruye: no hay
// emision comprada de donde partir. Se mide aparte (0 en ATT1 y en Maribel).
console.log('purchase_by_our_link_counts_backfill=OK');

// --- 3. cada camino, despues ------------------------------------------------------

for (const status of ['sequence_exhausted', 'cancelled', 'expired', 'escalated']) {
  const cart = await sentCart();
  const closedAt = await closeCase(cart, status);
  const purchase = await buy(cart);
  const row = await caseRow(cart);
  if (purchase.correlation_outcome !== 'matched'
      || row.status !== 'won' || row.purchase_event_id !== purchase.webhook_event_id
      || new Date(row.closed_at).getTime() !== new Date(closedAt).getTime()
      || row.status_before !== status || row.event_status_before !== status || row.events !== 1) {
    fail(`a purchase with the link of a ${status} case did not count`, { purchase, row });
  }
}
// El helper dice que gano despues de cerrado.
const outcomeCart = await sentCart();
await closeCase(outcomeCart, 'sequence_exhausted');
const outcomeEvent = one((await db.query(`
  insert into public.webhook_events (source, external_event_id, event_type, payload, processing_status)
  values ('hotmart', 'link-counts-direct', 'PURCHASE_APPROVED', '{}'::jsonb, 'received') returning id
`)).rows, 'direct event').id;
const directOutcome = await closeHelper(outcomeCart.reserved.issuance_id, outcomeEvent);
if (directOutcome !== 'won_after_close') fail('the helper outcome diverged', directOutcome);

// Abierto: como hasta hoy, outcome 'won' y los toques que faltan cancelados.
const openCart = await sentCart();
const openSecond = await openCart.insertAction('second_touch', 'no_reply_review', 86_400, false);
const openPurchase = await buy(openCart);
const openRow = await caseRow(openCart);
const openSecondStatus = one((await db.query(
  'select status, terminal_reason from public.scheduled_actions where id = $1', [openSecond],
)).rows, 'open second');
if (openPurchase.correlation_outcome !== 'matched' || openRow.status !== 'won'
    || openRow.status_before !== 'active'
    || openSecondStatus.status !== 'cancelled' || openSecondStatus.terminal_reason !== 'purchase_detected') {
  fail('the open case path changed', { openPurchase, openRow, openSecondStatus });
}

// Ya ganado por otra compra: no se toca.
const wonCart = await sentCart();
const otherPurchase = one((await db.query(`
  insert into public.webhook_events (source, external_event_id, event_type, payload, processing_status)
  values ('hotmart', 'link-counts-other', 'PURCHASE_APPROVED', '{}'::jsonb, 'processed') returning id
`)).rows, 'other').id;
await db.query(`
  update public.recovery_cases
  set status = 'won', won_at = now(), purchase_event_id = $2, closed_at = now()
  where id = $1
`, [wonCart.recoveryCase, otherPurchase]);
const wonPurchase = await buy(wonCart);
const wonRow = await caseRow(wonCart);
if (wonPurchase.correlation_outcome !== 'matched'
    || (await issuanceRow(wonCart.reserved.issuance_id)).status !== 'purchase_matched'
    || wonRow.purchase_event_id !== otherPurchase || wonRow.events !== 0) {
  fail('a case already won by another purchase was touched', { wonPurchase, wonRow });
}

// La intencion ya comprada o cancelada no frena la emision; la intencion no cambia.
for (const state of ['purchased', 'cancelled']) {
  const cart = await sentCart();
  const intentId = (await issuanceRow(cart.reserved.issuance_id)).purchase_intent_id;
  await db.query('update public.purchase_intents set lifecycle_state = $2 where id = $1',
    [intentId, state]);
  const purchase = await buy(cart);
  const issuance = await issuanceRow(cart.reserved.issuance_id);
  const intent = one((await db.query(
    'select lifecycle_state from public.purchase_intents where id = $1', [intentId],
  )).rows, 'intent');
  const row = await caseRow(cart);
  if (purchase.correlation_outcome !== 'matched' || issuance.status !== 'purchase_matched'
      || issuance.hotmart_webhook_event_id !== purchase.webhook_event_id
      || intent.lifecycle_state !== state || row.status !== 'won') {
    fail(`a link whose intent is ${state} did not count`, { purchase, issuance, intent, row });
  }
}

// El replay sigue siendo replay, y otra compra con el mismo link, conflict.
const replayCart = await sentCart();
await closeCase(replayCart, 'sequence_exhausted');
transactionSeq += 1;
const replayId = `link-counts-purchase-${transactionSeq}`;
const replayTx = `HPLINKCOUNTS${String(transactionSeq).padStart(4, '0')}`;
const firstBuy = await admitPurchase(replayId, replayTx, replayCart.reserved.sck_value, null);
const versionAfterFirst = one((await db.query(
  'select version from public.recovery_cases where id = $1', [replayCart.recoveryCase],
)).rows, 'version').version;
const replayBuy = await admitPurchase(replayId, replayTx, replayCart.reserved.sck_value, null);
const secondBuy = await buy(replayCart);
const versionAfterReplay = one((await db.query(
  'select version from public.recovery_cases where id = $1', [replayCart.recoveryCase],
)).rows, 'version').version;
if (firstBuy.correlation_outcome !== 'matched' || replayBuy.correlation_outcome !== 'replay'
    || secondBuy.correlation_outcome !== 'conflict' || versionAfterReplay !== versionAfterFirst) {
  fail('replay or a second purchase with the same link diverged', { firstBuy, replayBuy, secondBuy });
}
console.log('purchase_by_our_link_counts_paths=OK');

// --- 4. una sola vez, y los permisos ------------------------------------------------

await expectError('second run', () => apply(join(root, 'supabase/migrations', MIGRATION)),
  'purchase_by_our_link_counts_already_applied');
// La migracion abre su transaccion: la que abortó se cierra antes de seguir.
await db.exec('rollback');
const helperAcl = one((await db.query(`
  select p.prosecdef,
         has_function_privilege('anon', p.oid, 'execute') anon_x,
         has_function_privilege('authenticated', p.oid, 'execute') auth_x,
         has_function_privilege('service_role', p.oid, 'execute') service_x,
         array_to_string(p.proconfig, ',') config
  from pg_proc p
  where p.oid = to_regprocedure('public._close_recovery_case_for_checkout_purchase_v1(uuid,uuid,timestamptz)')
`)).rows, 'helper acl');
const correlateAcl = one((await db.query(`
  select p.prosecdef,
         has_function_privilege('anon', p.oid, 'execute') anon_x,
         has_function_privilege('service_role', p.oid, 'execute') service_x
  from pg_proc p
  where p.oid = to_regprocedure('public.correlate_hotmart_checkout_issuance_v2(uuid,text,timestamptz)')
`)).rows, 'correlate acl');
if (helperAcl.prosecdef || helperAcl.anon_x || helperAcl.auth_x || helperAcl.service_x
    || helperAcl.config !== 'search_path=pg_catalog, public, pg_temp'
    || !correlateAcl.prosecdef || correlateAcl.anon_x) {
  fail('function ACL diverged', { helperAcl, correlateAcl });
}
console.log('purchase_by_our_link_counts_grants=OK');

await db.close();
console.log('purchase_by_our_link_counts=OK');
