// 20260928000200: varias ofertas por binding en los eventos de Hotmart portables.
//
// Ejecuta las RPC reales sobre la cadena completa de migraciones:
// - la forma de `additional_offer_codes` la valida la base;
// - carrito y pago fallido admiten una oferta adicional del binding, con el scope
//   de ESA oferta, y rechazan una oferta ajena sin dejar eventos;
// - la compra aprobada entra con cualquier oferta del producto (frena igual).
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

const expectRejected = async (label, action) => {
  let rejected = false;
  await db.exec('begin');
  try {
    await action();
  } catch {
    rejected = true;
  } finally {
    await db.exec('rollback');
  }
  if (!rejected) throw new Error(`${label} did not fail closed`);
};
const eventCount = async () => (await db.query(
  `select count(*)::integer count from public.webhook_events`,
)).rows[0]?.count;

const bindingInsert = (version, additional) => db.query(`
  insert into public.commercial_ally_runtime_bindings
    (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
     lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
     product_name, product_price, currency, offer_code, consent_copy_version,
     hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
     inbound_scope_key, inbound_scope_version, additional_offer_codes)
  values ('att1','att1-main',$1,'draft','att1','ATT1','att1-site','main',
          'att1.example','/offer','ATT1HOTLINK','ATT1 Offer',47,'USD','att1offer',
          'att1-whatsapp-v1',123456,42,24,'att1-inbound',1,$2::text[])
`, [version, additional]);

// 1. La forma de las ofertas adicionales.
await expectRejected('additional repeats the default offer', () => bindingInsert(9, ['att1offer']));
await expectRejected('additional with a malformed code', () => bindingInsert(9, ['bad code']));
await expectRejected('additional with null', () => bindingInsert(9, ['att1org', null]));
await expectRejected('more than 16 additional offers', () => bindingInsert(
  9, Array.from({ length: 17 }, (_, index) => `offer${String(index).padStart(2, '0')}`),
));

await bindingInsert(1, ['att1org']);
await db.query(`update public.commercial_ally_runtime_bindings set status='active' where binding_version=1`);
const resolved = (await db.query(`
  select additional_offer_codes from public.resolve_commercial_ally_runtime_binding('att1','att1-main',1)
`)).rows[0];
if (JSON.stringify(resolved?.additional_offer_codes) !== JSON.stringify(['att1org'])) {
  throw new Error(`binding resolution does not carry additional offers: ${JSON.stringify(resolved)}`);
}

// 2. Carrito.
const cart = (id, offer) => ({
  id,
  creation_date: Date.parse('2026-09-28T02:00:00Z'),
  event: 'PURCHASE_OUT_OF_SHOPPING_CART',
  version: '2.0.0',
  data: {
    buyer: { email: 'buyer@example.test', phone: '+1 (202) 555-0123' },
    product: { id: 123456, name: 'ATT1 Offer' },
    offer: { code: offer },
    checkout_country: { iso: 'MX', name: 'México' },
  },
});
const admitCart = (payload) => db.query(`
  select * from public.admit_portable_hotmart_cart_abandonment(
    'att1','att1-main',1,$1,$2::jsonb,'buyer@example.test','12025550123'
  )
`, [payload.id, JSON.stringify(payload)]);

const baseline = await eventCount();
await expectRejected('additional offer without its own scope', () => admitCart(cart('cart-no-scope', 'att1org')));
await expectRejected('offer outside the binding', () => admitCart(cart('cart-foreign', 'ghloffer')));
if (await eventCount() !== baseline) throw new Error('rejected carts created durable events');

await db.exec(`
  insert into public.hotmart_purchase_intent_scopes
    (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
     offer_ref, max_lookback, active)
  values ('att1','att1-main','123456','ATT1HOTLINK','att1offer',interval '2 hours',true),
         ('att1','att1-main','123456','ATT1HOTLINK','att1org',interval '2 hours',true);
`);
const cartAdmitted = (await admitCart(cart('cart-additional', 'att1org'))).rows[0];
if (cartAdmitted?.outcome !== 'inserted') {
  throw new Error(`additional-offer cart was not admitted: ${JSON.stringify(cartAdmitted)}`);
}
const cartProvenance = (await db.query(`
  select b.offer_ref from public.commercial_ally_hotmart_event_bindings b
  where b.webhook_event_id=$1
`, [cartAdmitted.webhook_event_id])).rows[0];
if (cartProvenance?.offer_ref !== 'att1org') {
  throw new Error(`cart provenance does not keep the event offer: ${JSON.stringify(cartProvenance)}`);
}
const cartDefault = (await admitCart(cart('cart-default', 'att1offer'))).rows[0];
if (cartDefault?.outcome !== 'inserted') throw new Error('default-offer cart regressed');

// 3. Pago fallido.
const failure = (id, offer) => ({
  id,
  creation_date: Date.parse('2026-09-28T03:00:00Z'),
  event: 'PURCHASE_CANCELED',
  version: '2.0.0',
  data: {
    buyer: { name: 'Buyer', email: 'buyer@example.test', checkout_phone: '+1 (202) 555-0123' },
    product: { id: 123456, name: 'ATT1 Offer' },
    purchase: {
      transaction: `HP${id.replaceAll('-', '').toUpperCase()}`.slice(0, 30),
      status: 'CANCELED',
      offer: { code: offer },
      payment: { refusal_reason: 'insufficient_funds' },
    },
    checkout_country: { iso: 'MX', name: 'México' },
  },
});
const admitFailure = (payload) => db.query(`
  select * from public.admit_portable_hotmart_payment_failure(
    'att1','att1-main',1,$1,$2::jsonb,'buyer@example.test','12025550123'
  )
`, [payload.id, JSON.stringify(payload)]);
await expectRejected('payment failure outside the binding', () => admitFailure(failure('pf-foreign', 'ghloffer')));
const failureAdmitted = (await admitFailure(failure('pf-additional', 'att1org'))).rows[0];
if (failureAdmitted?.outcome !== 'inserted') {
  throw new Error(`additional-offer payment failure was not admitted: ${JSON.stringify(failureAdmitted)}`);
}
const failureProvenance = (await db.query(`
  select b.offer_ref from public.commercial_ally_hotmart_event_bindings b
  where b.webhook_event_id=$1
`, [failureAdmitted.webhook_event_id])).rows[0];
if (failureProvenance?.offer_ref !== 'att1org') {
  throw new Error(`payment failure provenance does not keep the event offer: ${JSON.stringify(failureProvenance)}`);
}

// 4. Compra aprobada: cualquier oferta del producto.
await db.exec(`
  insert into public.commercial_ally_hotmart_purchase_policies
    (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
  values ('att1','att1-main',1,true,interval '2 hours');
`);
const purchase = (id, offer, productId = 123456) => ({
  id,
  creation_date: Date.parse('2026-09-28T04:00:01Z'),
  event: 'PURCHASE_APPROVED',
  version: '2.0.0',
  data: {
    product: { id: productId, ucode: 'ATT1-UCODE' },
    buyer: { email: 'buyer@example.test', checkout_phone: '+1 (202) 555-0123' },
    purchase: {
      approved_date: Date.parse('2026-09-28T04:00:00Z'),
      status: 'APPROVED',
      transaction: `HP${id.replaceAll('-', '').toUpperCase()}`.slice(0, 30),
      offer: { code: offer },
    },
  },
});
const admitPurchase = (payload) => db.query(`
  select * from public.admit_portable_hotmart_purchase_approved(
    'att1','att1-main',1,$1,$2::jsonb,'buyer@example.test','12025550123'
  )
`, [payload.id, JSON.stringify(payload)]);
await expectRejected('purchase of another product', () => admitPurchase(purchase('buy-other-product', 'att1offer', 999999)));
await expectRejected('purchase without offer', () => admitPurchase(purchase('buy-no-offer', '')));
const foreignPurchase = (await admitPurchase(purchase('buy-foreign-offer', 'ghloffer'))).rows[0];
if (foreignPurchase?.outcome !== 'inserted') {
  throw new Error(`a purchase through an offer outside the binding must still stop recovery: ${JSON.stringify(foreignPurchase)}`);
}

// 5. Ninguna RPC portable compara ya contra la oferta del binding a secas.
for (const signature of [
  'public.admit_portable_hotmart_cart_abandonment(text,text,integer,text,jsonb,text,text)',
  'public.admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text)',
  'public.admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text)',
]) {
  const definition = (await db.query(
    `select pg_get_functiondef(to_regprocedure($1)) def`, [signature],
  )).rows[0]?.def ?? '';
  if (/is distinct from v_binding\.offer_code|offer_ref\s*=\s*v_binding\.offer_code/.test(definition)) {
    throw new Error(`${signature} still requires the single binding offer`);
  }
}

console.log(JSON.stringify({
  migration: '20260928000200',
  binding_additional_offers: resolved.additional_offer_codes,
  cart_additional_offer: cartAdmitted.outcome,
  payment_failure_additional_offer: failureAdmitted.outcome,
  purchase_foreign_offer: foreignPurchase.outcome,
}));
