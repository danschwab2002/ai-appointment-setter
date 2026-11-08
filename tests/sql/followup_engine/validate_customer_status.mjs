// Si la persona ya es cliente, y de que producto (migracion 20261103000700,
// E-297, P-11; diseno E-278, PR 1), contra la cadena completa de migraciones.
//
// Prueba:
//   0. que la migracion agrega exactamente una funcion, no cambia ninguna otra
//      definicion ni ACL, que es solo de service_role y que correrla dos veces
//      aborta con customer_status_already_applied;
//   1. telefono: una compra aprobada por la admision portable real
//      (admit_portable_hotmart_purchase_approved) con el checkout_phone 521 +
//      10 la ve por las dos formas del movil (52 + 10 y 521 + 10), como
//      'cliente' con source 'telefono'; y sigue siendo clienta con la compra de
//      hace 400 dias (D3: no vence, person_activity si corta a los 30);
//   2. mail: la compra con otro telefono y el mail del contacto de este
//      WhatsApp (contact_points de tipo email, con mayusculas y espacios) da
//      'mail'; y con el mail solo en contacts.email, tambien;
//   3. link: la emision purchase_matched de ESTA conversacion da 'link'; la
//      misma persona desde otra conversacion la ve por la identidad a la que
//      se mando el link ('telefono'); el link gana sobre el telefono;
//   4. reembolso y contracargo, por la transaccion de la compra: todas
//      reembolsadas da 'reembolsada'; un contracargo o una protesta da
//      'contracargo'; con una compra vigente sigue 'cliente' y las fechas son
//      solo las de la vigente;
//   5. lo que no entra: otro telefono, otro funnel, otro tenant, un pago
//      rechazado (no es compra) y la emision de otra conversacion sin la
//      identidad de esta persona; dos funnels pedidos dan solo el comprado;
//      una entrada invalida da 22023; y la fila no lleva nada personal.
//
// Datos: binding, ofertas y producto de tests/fixtures/instances/att1/
// instancia.toml, y el sobre de compra 2.0.0 inline, como en
// validate_portable_person_activity.mjs (la admision solo mira el sobre). La
// emision del link (3) se inserta con session_replication_role = replica, como
// en validate_handoff_link_note.mjs, para no armar la cadena de claves ajenas;
// sus CHECK siguen valiendo. El reembolso (4) va como lo guardaria el PR de los
// reembolsos (D4): un webhook_events de hotmart con el event_type de Hotmart y
// la misma data.purchase.transaction. No hay todavia un reembolso capturado:
// ese PR trae el fixture real y confirma la forma.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION = '20261103000700_customer_status_v1.sql';
const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  -- Los privilegios por defecto de Supabase: toda funcion nueva nace con EXECUTE
  -- para anon y authenticated, y la migracion tiene que revocarlos.
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
`);

const fail = (message, detail) => {
  throw new Error(detail === undefined ? message : `${message}: ${JSON.stringify(detail)}`);
};
const one = (rows, label) => {
  if (rows.length !== 1) fail(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};
const results = {};

// ---------------------------------------------------------------------------
// 0. La migracion agrega una funcion y nada mas.
// ---------------------------------------------------------------------------
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`${MIGRATION} no esta en la cadena`);
// Despues va solo la de las cuatro ramas del pago rechazado nativo (20261103000900, E-306).
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261103000900_flow_engine_native_payment_failure_branches_v1.sql', '20261103001000_handoff_wait_alerts_v1.sql', '20261103001300_flow_engine_native_segment_label_v1.sql', '20261103001500_segment_entry_answers_v1.sql',
  // El origen de un recorrido nativo para la campana por landing (20261103001700, E-321): una lectura de service_role.
  '20261103001700_flow_engine_native_campaign_origin_v1.sql',
  // El cierre del caso por el cupon de una campana (20261106000100, E-339): una RPC de service_role.
  '20261106000100_recovery_case_segment_coupon_close_v1.sql',
  // El rechazo de la cuenta espera a que la cuenta vuelva (20261107000100, E-348): dos tablas y ocho RPC de service_role.
  '20261107000100_meta_account_hold_retry_v1.sql',
  // El almacen dice quien ejecuta cada flujo (20261108000100, E-353): una clave mas en read_flow_store_v1.
  '20261108000100_flow_store_route_executor_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de las ramas del pago rechazado`, names.slice(position + 1));
}
const apply = async (file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
));
const snapshot = async () => new Map((await db.query(`
  select p.oid::regprocedure::text as signature,
         pg_get_functiondef(p.oid) as definition,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));

await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of names.slice(0, position)) {
  await apply(join(root, 'supabase/migrations', name));
}
const before = await snapshot();
await apply(join(root, 'supabase/migrations', MIGRATION));
const after = await snapshot();

const ENTRYPOINT = 'get_customer_status_v1(text,text[],bigint,bigint,bigint,text)';
const added = [...after.keys()].filter((signature) => !before.has(signature));
const removed = [...before.keys()].filter((signature) => !after.has(signature));
const changed = [...before.keys()].filter((signature) => after.has(signature)
  && (after.get(signature).definition !== before.get(signature).definition
      || after.get(signature).service_x !== before.get(signature).service_x
      || after.get(signature).api_x !== before.get(signature).api_x));
if (JSON.stringify(added) !== JSON.stringify([ENTRYPOINT]) || removed.length || changed.length) {
  fail('the migration changed something else', { added, removed, changed });
}
{
  const row = after.get(ENTRYPOINT);
  if (row.service_x !== true || row.api_x !== false) fail(`${ENTRYPOINT} is not service_role only`, row);
}
try {
  await apply(join(root, 'supabase/migrations', MIGRATION));
  fail('running the migration twice passed');
} catch (error) {
  if (!String(error.message).includes('customer_status_already_applied')) throw error;
  await db.exec('rollback;');
}
if ((await snapshot()).size !== after.size) fail('the second run left something');
results.migration = { added: 1, changed: 0, twice: 'customer_status_already_applied' };

// ---------------------------------------------------------------------------
// El binding de ATT1 (el mismo lector minimo que validate_portable_person_activity.mjs).
// ---------------------------------------------------------------------------
const readManifest = (text) => {
  const manifest = {};
  const walk = (path) => path.reduce((node, key) => (node[key] ??= {}), manifest);
  let table = manifest;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    let match = line.match(/^\[\[([a-z_.]+)\]\]$/);
    if (match) {
      const path = match[1].split('.');
      const parent = walk(path.slice(0, -1));
      table = {};
      (parent[path.at(-1)] ??= []).push(table);
      continue;
    }
    match = line.match(/^\[([a-z_.]+)\]$/);
    if (match) {
      table = walk(match[1].split('.'));
      continue;
    }
    match = line.match(/^([a-z_]+)\s*=\s*(?:"([^"]*)"|(-?\d+)|(true|false))\s*(?:#.*)?$/);
    if (match) {
      table[match[1]] = match[2] ?? (match[3] !== undefined ? Number(match[3]) : match[4] === 'true');
    }
  }
  return manifest;
};
const required = (value, label) => {
  if (value === undefined || value === null || value === '') fail(`ATT1 fixture without ${label}`);
  return value;
};
const manifest = readManifest(readFileSync(join(root, 'tests/fixtures/instances/att1/instancia.toml'), 'utf8'));
const offers = required(manifest.hotmart?.ofertas, 'hotmart.ofertas').map((offer) => {
  const url = new URL(required(offer.url, 'ofertas.url'));
  return {
    offer_code: required(offer.codigo, 'ofertas.codigo'),
    site: required(offer.site, 'ofertas.site'),
    landing_id: required(offer.landing_id, 'ofertas.landing_id'),
    page_host: url.host,
    page_path: url.pathname,
  };
});
const ATT1 = {
  tenant: required(manifest.instancia?.tenant_ref, 'instancia.tenant_ref'),
  funnel: required(manifest.instancia?.funnel_ref, 'instancia.funnel_ref'),
  ally: required(manifest.instancia?.ally_ref, 'instancia.ally_ref'),
  bindingVersion: required(manifest.instancia?.binding_version, 'instancia.binding_version'),
  brand: required(manifest.instancia?.marca, 'instancia.marca'),
  productId: required(manifest.hotmart?.product_id, 'hotmart.product_id'),
  hotlink: required(manifest.hotmart?.hotlink, 'hotmart.hotlink'),
  productName: required(manifest.hotmart?.product_name, 'hotmart.product_name'),
  currency: required(manifest.hotmart?.moneda, 'hotmart.moneda'),
  price: required(manifest.hotmart?.precio, 'hotmart.precio'),
  accountId: required(manifest.chatwoot?.account_id, 'chatwoot.account_id'),
  inboxId: required(manifest.chatwoot?.inbox_id, 'chatwoot.inbox_id'),
  inboundScope: required(manifest.inbound?.scope_key, 'inbound.scope_key'),
  inboundVersion: required(manifest.inbound?.scope_version, 'inbound.scope_version'),
  copyVersion: required(manifest.consentimiento?.copy_version, 'consentimiento.copy_version'),
};
const [defaultOffer, ...additionalOffers] = offers;
const PILOT = JSON.parse(readFileSync(join(root, 'tests/fixtures/instances/att1/politica-piloto.json'), 'utf8'));

await db.query(`
  insert into public.commercial_ally_runtime_bindings
    (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
     lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
     product_name, product_price, currency, offer_code, consent_copy_version,
     hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
     inbound_scope_key, inbound_scope_version, additional_offer_codes,
     additional_offer_landings)
  values ($1,$2,$3,'active',$4,$5,$6,$7,$8,$9,$10,$11,$12::numeric,$13,$14,$15,
          $16,$17,$18,$19,$20,$21::text[],$22::jsonb)
`, [
  ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, ATT1.ally, ATT1.brand,
  defaultOffer.site, defaultOffer.landing_id, defaultOffer.page_host,
  defaultOffer.page_path, ATT1.hotlink, ATT1.productName, ATT1.price,
  ATT1.currency, defaultOffer.offer_code, ATT1.copyVersion, ATT1.productId,
  ATT1.accountId, ATT1.inboxId, ATT1.inboundScope, ATT1.inboundVersion,
  additionalOffers.map((offer) => offer.offer_code),
  JSON.stringify(additionalOffers.map((offer) => ({
    offer_code: offer.offer_code, site: offer.site, landing_id: offer.landing_id,
    page_host: offer.page_host, page_path: offer.page_path,
  }))),
]);
for (const offer of offers) {
  await db.query(`
    insert into public.hotmart_purchase_intent_scopes
      (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
       offer_ref, max_lookback, active)
    values ($1,$2,$3,$4,$5,$6::interval,true)
  `, [ATT1.tenant, ATT1.funnel, String(ATT1.productId), ATT1.hotlink, offer.offer_code,
    required(PILOT.purchase_intent_scope?.max_lookback, 'purchase_intent_scope.max_lookback')]);
}
await db.query(`
  insert into public.commercial_ally_hotmart_purchase_policies
    (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
  values ($1,$2,$3,true,$4::interval)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion,
  required(PILOT.purchase_policy?.max_lookback, 'purchase_policy.max_lookback')]);

// ---------------------------------------------------------------------------
// Personas, sobres y lecturas.
// ---------------------------------------------------------------------------
let personIndex = 0;
const person = (label) => {
  personIndex += 1;
  const suffix = String(personIndex).padStart(2, '0');
  const national = `55555507${suffix}`;
  return {
    label,
    email: `att1-cliente-${suffix}@example.test`,
    name: `Persona cliente ${label}`,
    plain: `52${national}`,
    whatsapp: `521${national}`,
  };
};
let eventIndex = 0;
const envelope = (buyer, event, extra = {}) => {
  eventIndex += 1;
  return {
    id: `att1-cliente-${event.toLowerCase()}-${eventIndex}`,
    creation_date: Date.now(),
    event,
    version: '2.0.0',
    data: {
      buyer,
      product: { id: ATT1.productId, name: ATT1.productName },
      purchase: {
        transaction: `HPATT1CLI${String(eventIndex).padStart(3, '0')}`,
        offer: { code: defaultOffer.offer_code },
        ...extra,
      },
    },
  };
};
const admitPurchase = async (buyer, email, phone) => {
  const payload = envelope(buyer, 'PURCHASE_APPROVED', { status: 'APPROVED', approved_date: Date.now() });
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload),
    email, phone])).rows, `${payload.id} purchase`);
  if (admitted.webhook_event_id == null) fail(`${payload.id} purchase not admitted`, admitted);
  return { eventId: admitted.webhook_event_id, transaction: payload.data.purchase.transaction };
};
const admitFailure = async (lead) => {
  const payload = envelope(
    { name: lead.name, email: lead.email, checkout_phone: `+${lead.whatsapp}` },
    'PURCHASE_CANCELED',
    { status: 'CANCELED', payment: { type: 'CREDIT_CARD', refusal_reason: 'Transaction refused' } },
  );
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload),
    lead.email, lead.whatsapp])).rows, `${lead.label} failure`);
  if (admitted.outcome !== 'inserted') fail(`${lead.label} failure`, admitted);
};
// Como lo guardaria el PR de los reembolsos (D4): sin binding ni correlacion,
// por la transaccion de la compra.
const reverse = async (transaction, eventType) => {
  eventIndex += 1;
  await db.query(`
    insert into public.webhook_events (source, external_event_id, event_type, payload, processing_status)
    values ('hotmart', $1, $2, $3::jsonb, 'processed')
  `, [`att1-cliente-reversal-${eventIndex}`, eventType, JSON.stringify({
    id: `att1-cliente-reversal-${eventIndex}`, event: eventType, version: '2.0.0',
    data: { product: { id: ATT1.productId }, purchase: { transaction, status: eventType.slice(9) } },
  })]);
};

// La conversacion del link (3); las demas lecturas van desde otra.
const CONVERSATION = 4101;
const status = async (phone, {
  tenant = ATT1.tenant, funnels = [ATT1.funnel], account = ATT1.accountId,
  inbox = ATT1.inboxId, conversation = 4100,
} = {}) => (await db.query(`
  select * from public.get_customer_status_v1($1, $2::text[], $3, $4, $5, $6)
`, [tenant, funnels, account, inbox, conversation, phone])).rows;
const expectStatus = (label, rows, expected) => {
  const seen = rows.map((row) => `${row.funnel_ref}:${row.status}:${row.source}`);
  if (JSON.stringify(seen) !== JSON.stringify(expected)) fail(label, seen);
};
const expectError = async (label, code, run) => {
  try {
    await run();
  } catch (error) {
    if (error.code !== code) fail(`${label}: expected ${code}, got ${error.code} ${error.message}`);
    return;
  }
  fail(`${label}: expected ${code}, it passed`);
};
const OK = `${ATT1.funnel}:cliente`;

// ---------------------------------------------------------------------------
// 1. Telefono, por las dos formas del movil, y sin ventana.
// ---------------------------------------------------------------------------
const byPhone = person('telefono');
const phonePurchase = await admitPurchase(
  { name: byPhone.name, email: byPhone.email, checkout_phone: `+${byPhone.whatsapp}` },
  byPhone.email, byPhone.whatsapp,
);
for (const phone of [byPhone.plain, byPhone.whatsapp]) {
  const rows = await status(phone);
  expectStatus(`phone by ${phone.length} digits`, rows, [`${OK}:telefono`]);
  if (!(rows[0].first_purchase_at instanceof Date)
      || rows[0].first_purchase_at.getTime() !== rows[0].last_purchase_at.getTime()) {
    fail('one purchase: first and last are the same', rows[0]);
  }
  if (Object.keys(rows[0]).sort().join() !== 'first_purchase_at,funnel_ref,last_purchase_at,source,status') {
    fail('the row carries something else', Object.keys(rows[0]));
  }
}
await db.query(`update public.webhook_events set received_at = clock_timestamp() - interval '400 days' where id = $1`,
  [phonePurchase.eventId]);
expectStatus('a purchase of 400 days ago', await status(byPhone.whatsapp), [`${OK}:telefono`]);
results.phone = { both_forms: true, no_window_days: 400 };

// ---------------------------------------------------------------------------
// 2. Mail del contacto de este WhatsApp.
// ---------------------------------------------------------------------------
const addWhatsappContact = async (lead, { pointEmail = null, columnEmail = null } = {}) => {
  const contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone) values ($1, $2, $3) returning id
  `, [lead.name, columnEmail, lead.whatsapp])).rows, 'contact').id;
  await db.query(`
    insert into public.channel_identities (contact_id, channel, account_id, external_user_id, metadata)
    values ($1, 'whatsapp', $2, $3, jsonb_build_object('inbox_id', $4::text))
  `, [contact, `chatwoot:${ATT1.accountId}`, lead.whatsapp, String(ATT1.inboxId)]);
  if (pointEmail) {
    await db.query(`
      insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
      values ($1, 'email', $2, $2, 'hotmart')
    `, [contact, pointEmail]);
  }
  return contact;
};
const byMail = person('mail');
const otherPhone = person('otro telefono');
await addWhatsappContact(byMail, { pointEmail: ` ${byMail.email.toUpperCase()} ` });
await admitPurchase(
  { name: byMail.name, email: byMail.email, checkout_phone: `+${otherPhone.whatsapp}` },
  byMail.email, otherPhone.whatsapp,
);
expectStatus('mail from contact_points', await status(byMail.whatsapp), [`${OK}:mail`]);
const byColumn = person('mail en contacts');
const otherPhone2 = person('otro telefono 2');
await addWhatsappContact(byColumn, { columnEmail: byColumn.email });
await admitPurchase(
  { name: byColumn.name, email: byColumn.email.toUpperCase(), checkout_phone: `+${otherPhone2.whatsapp}` },
  byColumn.email, otherPhone2.whatsapp,
);
expectStatus('mail from contacts.email', await status(byColumn.plain), [`${OK}:mail`]);
results.mail = { contact_points: true, contacts_email: true };

// ---------------------------------------------------------------------------
// 3. El link de esta conversacion.
// ---------------------------------------------------------------------------
const byLink = person('link');
const linkEvent = one((await db.query(`
  insert into public.webhook_events (source, external_event_id, event_type, payload, processing_status)
  values ('hotmart', 'att1-cliente-link-1', 'PURCHASE_APPROVED', $1::jsonb, 'processed') returning id
`, [JSON.stringify(envelope({ name: 'Otra', email: 'otra-persona@example.test',
  checkout_phone: '+5215555079999' }, 'PURCHASE_APPROVED', { status: 'APPROVED' }))])).rows, 'link event').id;
await db.exec('set session_replication_role = replica;');
const linkIntent = one((await db.query(`
  insert into public.purchase_intents (tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
    normalized_phone, submitted_at, lifecycle_state, whatsapp_contact_authorized, provisional,
    provider_observed, activation_authorized)
  values ($1, $2, $3, $4, $5, $6, now(), 'purchased', true, false, true, true) returning id
`, [ATT1.tenant, ATT1.funnel, defaultOffer.landing_id, ATT1.hotlink, defaultOffer.offer_code,
  byLink.plain])).rows, 'intent').id;
const linkContact = one((await db.query(`
  insert into public.contacts (full_name, phone) values ($1, $2) returning id
`, [byLink.name, byLink.whatsapp])).rows, 'link contact').id;
const linkIdentity = one((await db.query(`
  insert into public.channel_identities (contact_id, channel, account_id, external_user_id, metadata)
  values ($1, 'whatsapp', $2, $3, '{}'::jsonb) returning id
`, [linkContact, `chatwoot:${ATT1.accountId}`, byLink.whatsapp])).rows, 'link identity').id;
const ULID = '01M4GR1RSK8DJY4XVHMCY30001';
const url = `https://pay.hotmart.com/${ATT1.hotlink}?off=${defaultOffer.offer_code}&checkoutMode=10`
  + `&src=hermes&sck=hermes~v1~${ULID}`;
await db.query(`
  insert into public.checkout_link_issuances
    (issuance_ulid, commercial_case_id, purchase_intent_id, contact_id, channel_identity_id,
     offer_catalog_id, chatwoot_account_id, chatwoot_inbox_id, chatwoot_conversation_id,
     trigger_external_message_id, source_kind, source_value, sck_format_version, sck_value,
     checkout_url_final, status, chatwoot_message_id, finalized_at, hotmart_webhook_event_id,
     purchased_at)
  values ($1, gen_random_uuid(), $2, $3, $4, gen_random_uuid(), $5, $6, $7, '9001',
          'inbound_request', 'hermes', 'v1', $8, $9, 'purchase_matched', 3001, now(), $10, now())
`, [ULID, linkIntent, linkContact, linkIdentity, ATT1.accountId, ATT1.inboxId, CONVERSATION,
  `hermes~v1~${ULID}`, url, linkEvent]);
await db.exec('set session_replication_role = origin;');
expectStatus('the link of this conversation', await status(byLink.whatsapp, { conversation: CONVERSATION }),
  [`${OK}:link`]);
expectStatus('the same person from another conversation', await status(byLink.plain, { conversation: 4102 }),
  [`${OK}:telefono`]);
expectStatus('a stranger in the conversation of the link',
  await status(person('extrana').whatsapp, { conversation: CONVERSATION }),
  [`${OK}:link`]);
results.link = { this_conversation: 'link', other_conversation: 'telefono' };

// ---------------------------------------------------------------------------
// 4. Reembolso y contracargo.
// ---------------------------------------------------------------------------
const buyer = (lead) => [{ name: lead.name, email: lead.email, checkout_phone: `+${lead.whatsapp}` },
  lead.email, lead.whatsapp];
const refunded = person('reembolsada');
await reverse((await admitPurchase(...buyer(refunded))).transaction, 'PURCHASE_REFUNDED');
expectStatus('refunded', await status(refunded.whatsapp), [`${ATT1.funnel}:reembolsada:telefono`]);
const chargeback = person('contracargo');
await reverse((await admitPurchase(...buyer(chargeback))).transaction, 'PURCHASE_CHARGEBACK');
expectStatus('chargeback', await status(chargeback.whatsapp), [`${ATT1.funnel}:contracargo:telefono`]);
const protest = person('protesta');
const protested = await admitPurchase(...buyer(protest));
const protestedTwice = await admitPurchase(...buyer(protest));
await reverse(protested.transaction, 'PURCHASE_REFUNDED');
await reverse(protestedTwice.transaction, 'PURCHASE_PROTEST');
expectStatus('a refund and a protest', await status(protest.whatsapp), [`${ATT1.funnel}:contracargo:telefono`]);
const again = person('compro otra vez');
const first = await admitPurchase(...buyer(again));
const second = await admitPurchase(...buyer(again));
await reverse(first.transaction, 'PURCHASE_REFUNDED');
await db.query(`update public.webhook_events set received_at = clock_timestamp() - interval '10 days' where id = $1`,
  [first.eventId]);
{
  const rows = await status(again.whatsapp);
  expectStatus('one live purchase left', rows, [`${OK}:telefono`]);
  const live = one((await db.query('select received_at from public.webhook_events where id = $1',
    [second.eventId])).rows, 'live').received_at.getTime();
  if (rows[0].first_purchase_at.getTime() !== live || rows[0].last_purchase_at.getTime() !== live) {
    fail('the dates are the live purchase ones', rows[0]);
  }
}
results.reversals = { refunded: true, chargeback: true, protest: true, live_purchase_wins: true };

// ---------------------------------------------------------------------------
// 5. Lo que no entra.
// ---------------------------------------------------------------------------
{
  expectStatus('a stranger', await status(person('sin nada').whatsapp), []);
  expectStatus('another funnel', await status(byPhone.whatsapp, { funnels: ['att2'] }), []);
  expectStatus('another tenant', await status(byPhone.whatsapp, { tenant: 'otra-aliada' }), []);
  expectStatus('two funnels, one bought', await status(byPhone.whatsapp, { funnels: ['att2', ATT1.funnel] }),
    [`${OK}:telefono`]);
  const rejected = person('pago rechazado');
  await admitFailure(rejected);
  expectStatus('a rejected payment is not a purchase', await status(rejected.whatsapp), []);
  expectStatus('the link of another inbox', await status(person('otro inbox').whatsapp,
    { inbox: ATT1.inboxId + 1, conversation: CONVERSATION }), []);
  await expectError('a short phone', '22023', () => status('5255'));
  await expectError('a phone with a plus', '22023', () => status(`+${byPhone.whatsapp}`));
  await expectError('an empty tenant', '22023', () => status(byPhone.whatsapp, { tenant: ' ' }));
  await expectError('no funnels', '22023', () => status(byPhone.whatsapp, { funnels: [] }));
  await expectError('an empty funnel', '22023', () => status(byPhone.whatsapp, { funnels: [''] }));
  await expectError('conversation zero', '22023', () => status(byPhone.whatsapp, { conversation: 0 }));
  results.excluded = {
    other_phone: true, other_funnel: true, other_tenant: true, payment_rejected: true,
    other_inbox: true, invalid_input: 6,
  };
}

console.log(JSON.stringify(results));
