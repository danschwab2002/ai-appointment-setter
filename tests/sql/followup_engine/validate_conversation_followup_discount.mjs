// Valida el seguimiento con cupon contra el esquema real, ejecutando las RPC:
// reservar -> emitir el link con la RPC del agente -> autorizar -> cerrar ->
// uno solo por conversacion -> reintento de un fallo -> barreras de compra,
// derivacion y opt-out.
//
// Lo que importa y no se ve leyendo el SQL:
//   * el link del seguimiento es una emision NUEVA anclada en nuestro ultimo
//     mensaje, con su propio ULID, aunque el lead ya tenga un link del agente;
//   * un fallo antes de autorizar reusa el mismo link, y un fallo despues de
//     autorizar no vuelve a mandar nada;
//   * la compra se detecta por mail y por telefono sin codigo de pais, no solo
//     por el telefono exacto como la guarda de la reserva.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const db = new PGlite();
await db.waitReady;
await db.exec(
  'create role anon noinherit; create role authenticated noinherit; create role service_role noinherit bypassrls;',
);
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
  insert into public.inbound_commercial_scope_versions (
    scope_key, version, status, tenant_key, chatwoot_account_id,
    chatwoot_inbox_id, external_product_id, offer_code,
    approved_by, approved_at, published_at
  ) values (
    'libre-de-ansiedad-inbound', 2, 'published', 'lancemos', 1, 9,
    'F106691755G', 'explicit-catalog-authority', 'schema-probe', now(), now()
  );
  insert into public.human_handoff_projection_policies (
    policy_key, policy_version, scope_key, scope_version,
    inbound_scope_key, inbound_scope_version, expected_team_id,
    note_template_key, note_template_version, private_note_body, active
  ) values (
    'followup-probe-handoff', 1, null, null,
    'libre-de-ansiedad-inbound', 2, 17,
    'handoff-note', 1, 'Human review required.', true
  );
`);

async function admit(conversation, phone) {
  const row = (await db.query(
    'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)',
    ['libre-de-ansiedad-inbound', 2, conversation, phone],
  )).rows[0];
  if (row?.outcome !== 'created') {
    throw new Error(`fixture admission failed: ${JSON.stringify(row)}`);
  }
  return row;
}

const CLAIM = `
  select * from public.claim_conversation_followup_v1(
    $1::bigint, 1::bigint, 9::bigint, $2, $3, $4, $5,
    'johanna_seguimiento_descuento_01', 'es_EC', 'JOHANNA10',
    $6::bigint, $7::bigint, 90000::integer, $8, now()
  )
`;
const claim = async ({
  conversation, phone, email = null, key, regime = 'link_sent_no_purchase',
  inbound = 2443, outbound = 2445, ulid,
}) => (await db.query(
  CLAIM, [conversation, phone, email, key, regime, inbound, outbound, ulid],
)).rows[0];
const settle = async (key, status, messageId, reason) => (await db.query(`
  select * from public.settle_conversation_followup_v1($1, $2, $3::bigint, $4, now())
`, [key, status, messageId, reason])).rows[0];

// --- R1: el lead ya tiene el link del agente --------------------------------

const PHONE = '12025550140';
const lead = await admit(9501, PHONE);

// El link que el agente mando cuando el lead lo pidio (mensaje 2443).
const agentLink = (await db.query(`
  select * from public.reserve_chatwoot_checkout_issuance_v2(
    $1::uuid, $2, 1, 9, 9501, '2443', '01K5ABCDEFX2VYB4M6X9CDPTA1', clock_timestamp()
  )
`, [lead.commercial_case_id, PHONE])).rows[0];
if (agentLink?.outcome !== 'reserved') {
  throw new Error(`agent link fixture failed: ${JSON.stringify(agentLink)}`);
}

const FOLLOWUP_ULID = '01K5ABCDEFX2VYB4M6X9CDPTF1';
const claimed = await claim({
  conversation: 9501, phone: PHONE, key: 'followup:9501:2445', ulid: FOLLOWUP_ULID,
});
const expectedUrl = 'https://pay.hotmart.com/F106691755G'
  + `?off=bxjge6zq&checkoutMode=10&src=hermes&sck=hermes~v1~${FOLLOWUP_ULID}`;
if (claimed?.outcome !== 'claimed'
    || !claimed.followup_event_id
    || !claimed.checkout_issuance_id
    || claimed.checkout_issuance_id === agentLink.issuance_id
    || claimed.checkout_url_final !== expectedUrl
    || claimed.sck_value !== `hermes~v1~${FOLLOWUP_ULID}`) {
  throw new Error(`followup did not issue its own link: ${JSON.stringify(claimed)}`);
}

// La emision queda anclada en NUESTRO ultimo mensaje, no en el del lead.
const issuance = (await db.query(`
  select trigger_external_message_id, status, checkout_url_final
  from public.checkout_link_issuances where id = $1::uuid
`, [claimed.checkout_issuance_id])).rows[0];
if (issuance?.trigger_external_message_id !== '2445' || issuance.status !== 'reserved') {
  throw new Error(`followup issuance anchor diverged: ${JSON.stringify(issuance)}`);
}

const audit = (await db.query(`
  select status, regime, template_name, template_language, coupon_code,
         last_inbound_message_id, last_outbound_message_id, inbound_age_seconds,
         settled_at
  from public.conversation_followup_events where id = $1::uuid
`, [claimed.followup_event_id])).rows[0];
if (audit?.status !== 'claimed'
    || audit.regime !== 'link_sent_no_purchase'
    || audit.template_name !== 'johanna_seguimiento_descuento_01'
    || audit.template_language !== 'es_EC'
    || audit.coupon_code !== 'JOHANNA10'
    || Number(audit.last_inbound_message_id) !== 2443
    || Number(audit.last_outbound_message_id) !== 2445
    || audit.inbound_age_seconds !== 90000
    || audit.settled_at !== null) {
  throw new Error(`followup audit diverged: ${JSON.stringify(audit)}`);
}

// Idempotencia: el mismo command_key devuelve la reserva y el link originales.
const replayed = await claim({
  conversation: 9501, phone: PHONE, key: 'followup:9501:2445',
  ulid: '01K5ABCDEFX2VYB4M6X9CDPTF2',
});
if (replayed?.outcome !== 'replayed'
    || replayed.followup_event_id !== claimed.followup_event_id
    || replayed.checkout_url_final !== expectedUrl) {
  throw new Error(`followup claim was not idempotent: ${JSON.stringify(replayed)}`);
}

// Uno solo por conversacion: otro mensaje nuestro sin contestar no abre otro.
const limited = await claim({
  conversation: 9501, phone: PHONE, key: 'followup:9501:2999',
  inbound: 2443, outbound: 2999, ulid: '01K5ABCDEFX2VYB4M6X9CDPTF3',
});
if (limited?.outcome !== 'blocked_followup_limit' || limited.followup_event_id !== null) {
  throw new Error(`followup ignored the one-per-conversation limit: ${JSON.stringify(limited)}`);
}

// El bridge autoriza la emision con el mismo ancla antes de mandar.
const authorized = (await db.query(`
  select * from public.authorize_chatwoot_checkout_issuance_v2(
    $1::uuid, $2, 1, 9, 9501, '2445', clock_timestamp()
  )
`, [claimed.checkout_issuance_id, PHONE])).rows[0];
if (authorized?.outcome !== 'request_started') {
  throw new Error(`followup issuance could not be authorized: ${JSON.stringify(authorized)}`);
}

const settled = await settle('followup:9501:2445', 'sent', 3100, null);
if (settled?.outcome !== 'settled' || settled.followup_event_id !== claimed.followup_event_id) {
  throw new Error('settlement did not close the live reservation');
}
const resettled = await settle('followup:9501:2445', 'failed', null, 'tarde');
if (resettled?.outcome !== 'not_found') {
  throw new Error('settlement rewrote a terminal row');
}
const afterSent = await claim({
  conversation: 9501, phone: PHONE, key: 'followup:9501:2445',
  ulid: '01K5ABCDEFX2VYB4M6X9CDPTF4',
});
if (afterSent?.outcome !== 'replayed') {
  throw new Error('a delivered followup was claimable again');
}

// --- el reintento de un fallo -------------------------------------------------

const PHONE_B = '12025550141';
await admit(9502, PHONE_B);
const firstTry = await claim({
  conversation: 9502, phone: PHONE_B, key: 'followup:9502:3001', regime: 'went_quiet',
  inbound: 3000, outbound: 3001, ulid: '01K5ABCDEFX2VYB4M6X9CDPTB1',
});
if (firstTry?.outcome !== 'claimed') {
  throw new Error(`R2 followup was not claimed: ${JSON.stringify(firstTry)}`);
}
// Chatwoot rechazo antes de autorizar: la fila se libera y el link se reusa.
await settle('followup:9502:3001', 'failed', null, 'ChatwootProtocolError');
const retry = await claim({
  conversation: 9502, phone: PHONE_B, key: 'followup:9502:3001', regime: 'went_quiet',
  inbound: 3000, outbound: 3001, ulid: '01K5ABCDEFX2VYB4M6X9CDPTB2',
});
if (retry?.outcome !== 'claimed'
    || retry.followup_event_id === firstTry.followup_event_id
    || retry.checkout_issuance_id !== firstTry.checkout_issuance_id
    || retry.checkout_url_final !== firstTry.checkout_url_final) {
  throw new Error(`a failed followup did not reuse its link on retry: ${JSON.stringify(retry)}`);
}
// Con el envio ya autorizado, un fallo no vuelve a mandar: el resultado es
// incierto y mandar dos veces es peor que no mandar.
await db.query(`
  select * from public.authorize_chatwoot_checkout_issuance_v2(
    $1::uuid, $2, 1, 9, 9502, '3001', clock_timestamp()
  )
`, [retry.checkout_issuance_id, PHONE_B]);
await settle('followup:9502:3001', 'failed', null, 'ChatwootReplyDeliveryUnknownError');
const afterStarted = await claim({
  conversation: 9502, phone: PHONE_B, key: 'followup:9502:3001', regime: 'went_quiet',
  inbound: 3000, outbound: 3001, ulid: '01K5ABCDEFX2VYB4M6X9CDPTB3',
});
if (afterStarted?.outcome !== 'issuance_request_started_replay'
    || afterStarted.followup_event_id !== null) {
  throw new Error(`an in-flight followup was sent again: ${JSON.stringify(afterStarted)}`);
}

// --- la compra, por los tres caminos ---------------------------------------------

async function purchasedIntent({ phone, email }) {
  await db.query(`
    insert into public.purchase_intents (
      tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
      normalized_email, normalized_phone, submitted_at, lifecycle_state,
      whatsapp_contact_authorized, provisional, provider_observed, activation_authorized
    ) values (
      'lancemos', 'psicologajohanna', 'ads-b', 'F106691755G', 'mgbgpp19',
      $1, $2, now(), 'purchased', true, false, true, true
    )
  `, [email, phone]);
}

// Por mail: compro con otro telefono.
const PHONE_C = '12025550142';
await admit(9503, PHONE_C);
await purchasedIntent({ phone: '12025559999', email: 'compradora@example.com' });
const byEmail = await claim({
  conversation: 9503, phone: PHONE_C, email: ' Compradora@Example.com ',
  key: 'followup:9503:4001', inbound: 4000, outbound: 4001,
  ulid: '01K5ABCDEFX2VYB4M6X9CDPTC1',
});
if (byEmail?.outcome !== 'purchase_already_approved') {
  throw new Error(`a buyer by email received a coupon: ${JSON.stringify(byEmail)}`);
}

// Por telefono sin codigo de pais: Hotmart lo guardo con 9 digitos.
const PHONE_D = '593987654321';
await admit(9504, PHONE_D);
await purchasedIntent({ phone: '987654321', email: 'otra@example.com' });
const byTail = await claim({
  conversation: 9504, phone: PHONE_D, key: 'followup:9504:5001',
  inbound: 5000, outbound: 5001, ulid: '01K5ABCDEFX2VYB4M6X9CDPTD1',
});
if (byTail?.outcome !== 'purchase_already_approved') {
  throw new Error(`a buyer without country code received a coupon: ${JSON.stringify(byTail)}`);
}

// Ninguna barrera escribio auditoria ni emitio un link.
const blockedRows = (await db.query(`
  select
    (select count(*)::int from public.conversation_followup_events
      where external_conversation_id in (9503, 9504)) as events,
    (select count(*)::int from public.checkout_link_issuances
      where chatwoot_conversation_id in (9503, 9504)) as issuances
`)).rows[0];
if (blockedRows?.events !== 0 || blockedRows.issuances !== 0) {
  throw new Error(`a blocked followup left rows behind: ${JSON.stringify(blockedRows)}`);
}

// --- derivacion y opt-out -----------------------------------------------------------

const PHONE_E = '12025550144';
const derived = await admit(9505, PHONE_E);
const handoff = (await db.query(`
  select * from public.request_inbound_human_handoff(
    $1::uuid, 'handoff:followup-probe', 'explicit_human_request',
    'followup-probe-handoff', 1, now()
  )
`, [derived.commercial_case_id])).rows[0];
if (handoff?.outcome !== 'requested') {
  throw new Error(`handoff fixture failed: ${JSON.stringify(handoff)}`);
}
const pendingHandoff = await claim({
  conversation: 9505, phone: PHONE_E, key: 'followup:9505:6001',
  inbound: 6000, outbound: 6001, ulid: '01K5ABCDEFX2VYB4M6X9CDPTE1',
});
if (pendingHandoff?.outcome !== 'blocked_pending_handoff') {
  throw new Error(`a pending handoff received a coupon: ${JSON.stringify(pendingHandoff)}`);
}

const PHONE_F = '12025550145';
const optedOut = await admit(9506, PHONE_F);
await db.query(
  "update public.contacts set contact_permission = 'opted_out' where id = $1::uuid",
  [optedOut.contact_id],
);
const optOutClaim = await claim({
  conversation: 9506, phone: PHONE_F, key: 'followup:9506:7001',
  inbound: 7000, outbound: 7001, ulid: '01K5ABCDEFX2VYB4M6X9CDPTG1',
});
if (optOutClaim?.outcome !== 'blocked_contact') {
  throw new Error(`an opted-out contact received a coupon: ${JSON.stringify(optOutClaim)}`);
}

const unknown = await claim({
  conversation: 999999, phone: '12025550146', key: 'followup:999999:1',
  inbound: 1, outbound: 2, ulid: '01K5ABCDEFX2VYB4M6X9CDPTH1',
});
if (unknown?.outcome !== 'not_found') {
  throw new Error('an unknown conversation did not fail closed');
}

// --- entradas invalidas ----------------------------------------------------------

for (const [label, overrides] of [
  ['regimen no declarado', { regime: 'porque_si' }],
  ['nuestro mensaje anterior al del lead', { inbound: 8001, outbound: 8000 }],
  ['ULID invalido', { ulid: 'no-es-un-ulid' }],
]) {
  let rejected = false;
  try {
    await claim({
      conversation: 9501, phone: PHONE, key: 'followup:9501:8001',
      inbound: 8000, outbound: 8001, ulid: '01K5ABCDEFX2VYB4M6X9CDPTJ1', ...overrides,
    });
  } catch (error) {
    rejected = String(error.message ?? error).includes('claim_conversation_followup_invalid_input');
  }
  if (!rejected) {
    throw new Error(`invalid input was accepted: ${label}`);
  }
}
let incoherent = false;
try {
  await settle('followup:9502:3001', 'sent', null, null);
} catch (error) {
  incoherent = String(error.message ?? error).includes('settle_conversation_followup_invalid_input');
}
if (!incoherent) {
  throw new Error('a sent settlement without provider message was accepted');
}

// --- los permisos --------------------------------------------------------------------

const SIGNATURES = [
  'public.claim_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)',
  'public.settle_conversation_followup_v1(text,text,bigint,text,timestamptz)',
];
for (const signature of SIGNATURES) {
  for (const role of ['anon', 'authenticated']) {
    const granted = (await db.query(
      'select has_function_privilege($1, $2, $3) as allowed', [role, signature, 'EXECUTE'],
    )).rows[0];
    if (granted?.allowed !== false) {
      throw new Error(`${role} can execute ${signature}`);
    }
  }
  const service = (await db.query(
    'select has_function_privilege($1, $2, $3) as allowed', ['service_role', signature, 'EXECUTE'],
  )).rows[0];
  if (service?.allowed !== true) {
    throw new Error(`service_role cannot execute ${signature}`);
  }
}
for (const role of ['anon', 'authenticated', 'service_role']) {
  const granted = (await db.query(
    "select has_table_privilege($1, 'public.conversation_followup_events', 'SELECT') as allowed",
    [role],
  )).rows[0];
  if (granted?.allowed !== false) {
    throw new Error(`${role} can read conversation_followup_events directly`);
  }
}

console.log('CONVERSATION_FOLLOWUP_DISCOUNT_SQL_OK');
await db.close();
