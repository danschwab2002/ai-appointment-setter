// Al comprar, el bridge busca las conversaciones del comprador con el agente
// para pausarlas y pasarlas al equipo (E-100, la replica de «Ventas maribel»;
// migracion 20261025000400).
//
// read_purchase_agent_pause_conversations_v1 devuelve las conversaciones de la
// instancia (el entrante de su scope, la identidad atada a su inbox o un caso
// de recuperacion de un scope de su inbox) cuya persona coincide con el
// comprador por los ultimos 10 digitos del WhatsApp o por el mail. Se prueba:
// el telefono de Hotmart sin el 1 encuentra la conversacion del WhatsApp con
// el 1 (52 contra 521); el mail encuentra la conversacion del contacto del
// carrito; otra cuenta, otro inbox y otra persona no cuentan; dos numeros de
// WhatsApp distintos dan ambiguous sin conversaciones; la mas reciente va
// primero y salen hasta 5; sin datos del comprador, cero filas; argumentos
// invalidos dan 22023; y solo service_role la ejecuta.
//
// El telefono y el mail del comprador salen del evento real de ATT1
// (tests/fixtures/hotmart_cash_payment_approved_att1_20261012.json,
// anonimizado: checkout_phone sin el 1 de Mexico). Las filas se insertan con
// session_replication_role = replica para no armar la cadena entera de claves
// ajenas: los CHECK de las tablas siguen valiendo.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION = '20261025000400_purchase_agent_pause_v1.sql';
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

const captured = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/hotmart_cash_payment_approved_att1_20261012.json'), 'utf8',
));
const buyer = captured.eventos[0].payload.data.buyer;
const BUYER_PHONE = buyer.checkout_phone;
const BUYER_EMAIL = buyer.email;
if (!/^52\d{10}$/.test(BUYER_PHONE)) throw new Error('el telefono del fixture no es el capturado');
// WhatsApp de Mexico con el 1 movil: la otra forma del mismo numero.
const WA_WITH_ONE = `521${BUYER_PHONE.slice(2)}`;

const ACCOUNT = 2;
const INBOX = 11;
const SCOPE = 'att1-inbound';
const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;

await db.exec(`set session_replication_role = replica;`);
let counter = 100;
const conversation = async ({
  waId, number, account = ACCOUNT, inbox = INBOX, email = null, updatedAt,
  inbound = false, metadataInbox = true, recoveryInbox = null,
}) => {
  counter += 1;
  const contact = id(counter * 10 + 1);
  const identity = id(counter * 10 + 2);
  const conv = id(counter * 10 + 3);
  await db.query(`insert into public.contacts (id, full_name) values ($1, 'Persona Prueba')`, [contact]);
  if (email) {
    await db.query(`
      insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
      values ($1, 'email', $2, $3, 'hotmart')
    `, [contact, email, email.toLowerCase()]);
  }
  await db.query(`
    insert into public.channel_identities
      (id, contact_id, channel, account_id, external_user_id, external_conversation_id, identity_status, metadata)
    values ($1, $2, 'whatsapp', $3, $4, $5, 'active', $6)
  `, [identity, contact, `chatwoot:${account}`, waId, String(number),
      metadataInbox ? JSON.stringify({ inbox_id: String(inbox) }) : '{}']);
  await db.query(`
    insert into public.conversations
      (id, contact_id, channel_identity_id, status, automation_status, commercial_context, updated_at)
    values ($1, $2, $3, 'active', 'draft_only', $4, $5)
  `, [conv, contact, identity, JSON.stringify({ chatwoot_conversation_id: String(number) }), updatedAt]);
  if (inbound) {
    await db.query(`
      insert into public.commercial_cases
        (id, case_kind, contact_id, selected_channel_identity_id, conversation_id, status,
         inbound_scope_key, inbound_scope_version, tenant_ref)
      values (gen_random_uuid(), 'inbound_sales', $1, $4, $2, 'active', $3, 1, 'att1')
    `, [contact, conv, SCOPE, identity]);
  }
  if (recoveryInbox !== null) {
    const rc = id(counter * 10 + 4);
    await db.query(`
      insert into public.recovery_cases
        (id, contact_id, conversation_id, abandonment_event_id, external_product_id, product_name, grace_expires_at,
         policy_key, policy_version, commercial_case_id)
      values ($1, $2, $3, gen_random_uuid(), '5071808', 'Producto', now(), 'att1-recuperacion-un-toque', 1, $1)
    `, [rc, contact, conv]);
    await db.query(`
      insert into public.pilot_recovery_case_bindings (recovery_case_id, scope_key, scope_version, source_event_id)
      values ($1, $2, 1, gen_random_uuid())
    `, [rc, `prueba-inbox-${recoveryInbox}`]);
  }
};
for (const inbox of [INBOX, 12]) {
  await db.query(`
    insert into public.pilot_scope_versions (
      scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id, channel,
      channel_provider, channel_account_ref, source, source_event_type, external_product_id,
      offer_code, purpose, policy_key, policy_version, timezone, max_cohort_contacts,
      max_outbound_request_starts_total, max_outbound_request_starts_per_day, approved_by,
      approved_at, published_at, additional_source_event_types, audience_mode
    ) values ($1, 1, 'published', 'lancemos', $2, $3, 'whatsapp', 'chatwoot', $4,
      'hotmart', 'PURCHASE_OUT_OF_SHOPPING_CART', '5071808', '2uafw5bg', 'cart_recovery',
      'att1-recuperacion-un-toque', 1, 'America/Mexico_City', 3, 1000, 40, 'CANARY_APPROVER', now(), now(),
      '{}', 'consented_intent')
  `, [`prueba-inbox-${inbox}`, ACCOUNT, inbox, `chatwoot-inbox:${inbox}`]);
}

const read = async ({
  email = BUYER_EMAIL, phone = BUYER_PHONE, account = ACCOUNT, inbox = INBOX, scope = SCOPE,
} = {}) => (await db.query(`
  select outcome, chatwoot_conversation_id::int as number
  from public.read_purchase_agent_pause_conversations_v1($1, $2, $3, $4, $5)
`, [account, inbox, scope, email, phone])).rows;
const expect = (label, rows, outcome, numbers) => {
  const got = rows.map((row) => row.number);
  if ((numbers.length === 0 && rows.length !== (outcome ? 1 : 0))
      || rows.some((row) => row.outcome !== outcome)
      || JSON.stringify(got.filter((n) => n !== null)) !== JSON.stringify(numbers)) {
    throw new Error(`${label}: ${JSON.stringify(rows)}`);
  }
};

// 1. Sin ninguna conversacion: cero filas.
expect('sin conversaciones', await read(), null, []);

// 2. El telefono de Hotmart (52, sin el 1) encuentra el WhatsApp con el 1.
await conversation({ waId: WA_WITH_ONE, number: 40, inbound: true, updatedAt: '2026-10-20T10:00:00Z' });
expect('telefono 52 contra 521', await read({ email: null }), 'found', [40]);

// 3. Otra cuenta, otro inbox (sin entrante del scope) y otra persona no cuentan.
await conversation({ waId: WA_WITH_ONE, number: 41, account: 3, updatedAt: '2026-10-21T10:00:00Z' });
// El mismo numero con otra forma (00 adelante), atado al inbox 12 y sin entrante del scope.
await conversation({ waId: `00${BUYER_PHONE}`, number: 42, inbox: 12, updatedAt: '2026-10-21T11:00:00Z' });
await conversation({ waId: '5215599999999', number: 43, inbound: true, updatedAt: '2026-10-21T12:00:00Z' });
expect('solo su cuenta, su inbox y su persona', await read({ email: null }), 'found', [40]);

// 4. La conversacion de un caso de recuperacion de su inbox cuenta aunque la
// identidad no diga el inbox; una de otro inbox, no.
await conversation({
  waId: BUYER_PHONE, number: 44, metadataInbox: false, recoveryInbox: INBOX,
  updatedAt: '2026-10-22T10:00:00Z',
});
await conversation({
  waId: `+${BUYER_PHONE}`, number: 45, metadataInbox: false, recoveryInbox: 12,
  updatedAt: '2026-10-22T11:00:00Z',
});
expect('la recuperacion de su inbox, la mas reciente primero', await read({ email: null }), 'found', [44, 40]);

// 5. Por mail: la conversacion de un contacto con ese mail (sin telefono).
await conversation({ waId: null, number: 46, email: BUYER_EMAIL.toUpperCase(), updatedAt: '2026-10-23T10:00:00Z' });
expect('por mail', await read({ phone: null }), 'found', [46]);

// 6. Hasta 5, la mas reciente primero (conversaciones por mail, sin numero).
for (const [n, at] of [[50, '2026-10-24T01:00:00Z'], [51, '2026-10-24T02:00:00Z'],
  [52, '2026-10-24T03:00:00Z'], [53, '2026-10-24T04:00:00Z'], [54, '2026-10-24T05:00:00Z']]) {
  await conversation({ waId: null, number: n, email: BUYER_EMAIL, updatedAt: at });
}
expect('hasta 5', await read({ phone: null }), 'found', [54, 53, 52, 51, 50]);

// 7. Telefono y mail juntos: las conversaciones sin numero (la del carrito)
// suman, no hacen ambiguo; un mail que cae en otro numero, si.
expect('numero y conversaciones sin numero', await read(), 'found', [54, 53, 52, 51, 50]);
await conversation({ waId: '5215588888888', number: 47, email: BUYER_EMAIL, updatedAt: '2026-10-23T11:00:00Z' });
expect('dos numeros', await read(), 'ambiguous', []);

// 8. Sin mail ni telefono util: cero filas. Argumentos invalidos: 22023.
expect('sin datos del comprador', await read({ email: '  ', phone: '123' }), null, []);
for (const [label, args] of [
  ['inbox 0', [ACCOUNT, 0, SCOPE, BUYER_EMAIL, BUYER_PHONE]],
  ['cuenta nula', [null, INBOX, SCOPE, BUYER_EMAIL, BUYER_PHONE]],
  ['scope invalido', [ACCOUNT, INBOX, 'Scope Malo', BUYER_EMAIL, BUYER_PHONE]],
]) {
  try {
    await db.query('select * from public.read_purchase_agent_pause_conversations_v1($1, $2, $3, $4, $5)', args);
  } catch (error) {
    if (!String(error.message).includes('invalid_purchase_agent_pause_read')) {
      throw new Error(`${label}: fallo por otra cosa: ${error.message}`);
    }
    continue;
  }
  throw new Error(`${label}: tenia que rechazarse`);
}

// 9. Solo service_role la ejecuta, y es security definer de solo lectura.
const signature = 'public.read_purchase_agent_pause_conversations_v1(bigint,bigint,text,text,text)';
const acl = (await db.query(`
  select r.rolname, has_function_privilege(r.rolname, $1, 'EXECUTE') as can
  from pg_roles r where r.rolname in ('anon','authenticated','service_role')
`, [signature])).rows;
const canExecute = Object.fromEntries(acl.map((row) => [row.rolname, row.can]));
if (canExecute.anon || canExecute.authenticated || !canExecute.service_role) {
  throw new Error(`grants inesperados: ${JSON.stringify(canExecute)}`);
}
const meta = (await db.query(`
  select prosecdef, provolatile from pg_proc where oid = $1::regprocedure
`, [signature])).rows[0];
if (!meta.prosecdef || meta.provolatile !== 's') {
  throw new Error(`la funcion tenia que ser security definer y stable: ${JSON.stringify(meta)}`);
}

console.log('purchase agent pause: ok');
