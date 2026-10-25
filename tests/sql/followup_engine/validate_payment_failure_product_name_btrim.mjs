// El pago rechazado portable compara el nombre del producto sin los espacios
// de los bordes (migracion 20261025000200, E-087).
//
// Datos: el pago rechazado real de ATT1 (fixture capturado el 2026-10-19,
// hotmart_purchase_canceled_att1_20261019.json, evento 1, anonimizado) y el
// bloque data.product real de Maribel (fixture capturado el 2026-10-25,
// hotmart_product_name_trailing_space_maribel_20261025.json): Hotmart manda
// "El Protocolo Biohacking + " (26 caracteres) en el carrito y el binding lo
// tiene sin el espacio. Sin la migracion, el pago de Maribel con el espacio
// termina en portable_hotmart_payment_failure_binding_mismatch.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION = '20261025000200_payment_failure_product_name_btrim.sql';
const migrations = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
if (!migrations.includes(MIGRATION)) {
  throw new Error(`falta la migracion ${MIGRATION}`);
}

const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
`);
for (const file of [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...migrations.map((name) => join(root, 'supabase/migrations', name)),
]) {
  await db.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}

const fixture = (name) => JSON.parse(
  readFileSync(join(root, 'tests/fixtures', name), 'utf8'),
);
const att1Event = fixture('hotmart_purchase_canceled_att1_20261019.json').eventos[0].payload;
const maribel = fixture('hotmart_product_name_trailing_space_maribel_20261025.json');
const maribelCartName = maribel.events
  .find((event) => event.event === 'PURCHASE_OUT_OF_SHOPPING_CART').product.name;
const maribelBindingName = maribel.binding.product_name;
if (maribelCartName !== 'El Protocolo Biohacking + '
    || maribelCartName.length !== 26
    || maribelBindingName !== maribelCartName.trimEnd()) {
  throw new Error('el fixture de Maribel no es el capturado');
}

const ATT1_NAME = att1Event.data.product.name;
const ATT1_PRODUCT = att1Event.data.product.id;
const ATT1_OFFER = att1Event.data.purchase.offer.code;
const MARIBEL_PRODUCT = maribel.binding.hotmart_product_id;
const MARIBEL_OFFER = maribel.binding.offer_code;
const EMAIL = att1Event.data.buyer.email.toLowerCase();
const PHONE = att1Event.data.buyer.checkout_phone.replace(/[^0-9]/g, '');

await db.query(`
  insert into public.commercial_ally_runtime_bindings
    (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
     lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
     product_name, product_price, currency, offer_code, consent_copy_version,
     hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
     inbound_scope_key, inbound_scope_version)
  values
    ('att1','att1-main',1,'active','att1','ATT1','att1-site','main',
     'att1.example','/offer','ATT1HOTLINK',$1,47,'USD',$2,
     'att1-whatsapp-v1',$3,42,24,'att1-inbound',1),
    ('maribel','maribel-main',1,'active','maribel','Maribel','maribel-site','main',
     'maribel.example','/offer','MARIBELHOTLINK',$4,47,'USD',$5,
     'maribel-whatsapp-v1',$6,43,25,'maribel-inbound',1)
`, [ATT1_NAME, ATT1_OFFER, ATT1_PRODUCT, maribelBindingName, MARIBEL_OFFER, MARIBEL_PRODUCT]);
await db.query(`
  insert into public.hotmart_purchase_intent_scopes
    (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
     offer_ref, max_lookback, active)
  values
    ('att1','att1-main',$1,'ATT1HOTLINK',$2,interval '2 hours',true),
    ('maribel','maribel-main',$3,'MARIBELHOTLINK',$4,interval '2 hours',true)
`, [String(ATT1_PRODUCT), ATT1_OFFER, String(MARIBEL_PRODUCT), MARIBEL_OFFER]);

let sequence = 0;
const paymentFailure = ({ productId, productName, offer }) => {
  sequence += 1;
  const payload = structuredClone(att1Event);
  payload.id = `00000000-0000-4000-8000-0000000e087${sequence}`;
  payload.data.product.id = productId;
  payload.data.product.name = productName;
  payload.data.purchase.offer.code = offer;
  payload.data.purchase.transaction = `HP00000E087${sequence}`;
  return payload;
};
const admit = async (tenant, payload) => (await db.query(`
  select outcome from public.admit_portable_hotmart_payment_failure(
    $1, $2, 1, $3, $4::jsonb, $5, $6
  )
`, [tenant, `${tenant}-main`, payload.id, JSON.stringify(payload), EMAIL, PHONE])).rows[0].outcome;
const rejected = async (label, tenant, payload) => {
  try {
    await admit(tenant, payload);
  } catch (error) {
    if (!String(error.message).includes('portable_hotmart_payment_failure_binding_mismatch')) {
      throw new Error(`${label}: fallo por otra cosa: ${error.message}`);
    }
    return;
  }
  throw new Error(`${label}: se admitio y tenia que rechazarse`);
};
const admitted = async (label, tenant, payload) => {
  let outcome;
  try {
    outcome = await admit(tenant, payload);
  } catch (error) {
    throw new Error(`${label}: se rechazo (${error.message})`);
  }
  if (outcome !== 'inserted') throw new Error(`${label}: outcome ${outcome}`);
};

// 1. Maribel: el nombre tal como lo manda Hotmart, con el espacio al final.
await admitted('maribel con el espacio', 'maribel', paymentFailure({
  productId: MARIBEL_PRODUCT, productName: maribelCartName, offer: MARIBEL_OFFER,
}));
// 2. Maribel: sin el espacio (la forma de la compra aprobada), igual.
await admitted('maribel sin el espacio', 'maribel', paymentFailure({
  productId: MARIBEL_PRODUCT, productName: maribelBindingName, offer: MARIBEL_OFFER,
}));
// 3. Un espacio adelante tampoco cambia nada.
await admitted('maribel con espacio adelante', 'maribel', paymentFailure({
  productId: MARIBEL_PRODUCT, productName: ` ${maribelBindingName}`, offer: MARIBEL_OFFER,
}));
// 4. ATT1 no cambia: su nombre capturado rutea igual que antes.
await admitted('att1 con su nombre', 'att1', paymentFailure({
  productId: ATT1_PRODUCT, productName: ATT1_NAME, offer: ATT1_OFFER,
}));
// 5. Un nombre distinto, mas alla de los bordes, se sigue rechazando.
await rejected('att1 con otro nombre', 'att1', paymentFailure({
  productId: ATT1_PRODUCT, productName: 'Alimenta tu  Tiroides', offer: ATT1_OFFER,
}));
await rejected('maribel sin el +', 'maribel', paymentFailure({
  productId: MARIBEL_PRODUCT, productName: 'El Protocolo Biohacking', offer: MARIBEL_OFFER,
}));
// 6. El id del producto se sigue comparando igual.
await rejected('maribel con el id de ATT1', 'maribel', paymentFailure({
  productId: ATT1_PRODUCT, productName: maribelCartName, offer: MARIBEL_OFFER,
}));

// 7. La captura listen-only usa la misma admision: el pago de Maribel con el
// espacio queda guardado como listen_only_capture.
const listenOnly = paymentFailure({
  productId: MARIBEL_PRODUCT, productName: maribelCartName, offer: MARIBEL_OFFER,
});
const capture = (await db.query(`
  select * from public.admit_portable_hotmart_listen_only_capture_v1(
    'maribel', 'maribel-main', 1, $1, $2::jsonb, $3, $4
  )
`, [listenOnly.id, JSON.stringify(listenOnly), EMAIL, PHONE])).rows;
const captured = (await db.query(`
  select processing_status, processing_error from public.webhook_events
  where source = 'hotmart' and external_event_id = $1
`, [listenOnly.id])).rows;
if (capture.length !== 1 || captured.length !== 1
    || captured[0].processing_status !== 'ignored'
    || captured[0].processing_error !== 'listen_only_capture') {
  throw new Error(`captura listen-only: ${JSON.stringify({ capture, captured })}`);
}

// 8. Los grants de la funcion no cambiaron con el reemplazo.
const acl = (await db.query(`
  select r.rolname, has_function_privilege(r.rolname,
    'public.admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text)',
    'EXECUTE') as can
  from pg_roles r where r.rolname in ('anon','authenticated','service_role')
  order by r.rolname
`)).rows;
const canExecute = Object.fromEntries(acl.map((row) => [row.rolname, row.can]));
if (canExecute.anon || canExecute.authenticated || !canExecute.service_role) {
  throw new Error(`grants inesperados: ${JSON.stringify(canExecute)}`);
}

console.log('payment failure product name btrim: ok');
