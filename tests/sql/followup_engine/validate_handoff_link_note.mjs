// Al derivar, el bridge lee el ultimo link que le mandamos a la persona para
// la nota privada (F-034, D8; migracion 20261025000300, E-091).
//
// read_handoff_note_checkout_link_v1 devuelve el ultimo link
// accepted_by_chatwoot del contacto del caso en esa cuenta e inbox, y, si es
// el del carrito (recovery_first_touch), el cupon vigente de ese caso. Se
// prueba: el ultimo aceptado gana; un link no aceptado o ya comprado no
// cuenta; otro inbox, otra cuenta y otro contacto no cuentan; el del carrito
// trae el cupon vigente de ordinal mas alto y no uno vencido ni sin crear;
// sin link, cero filas; y solo service_role la ejecuta.
//
// Las URL son la forma real del link de ATT1 capturado el 10/10
// (tests/fixtures/chatwoot_followup_candidate_inbox_11_20261010.json,
// anonimizado). Las filas se insertan con session_replication_role = replica
// para no armar la cadena entera de claves ajenas: los CHECK de la tabla
// (forma de la URL, del sck, del origen y del estado) siguen valiendo.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION = '20261025000300_handoff_link_note_v1.sql';
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

const captured = readFileSync(
  join(root, 'tests/fixtures/chatwoot_followup_candidate_inbox_11_20261010.json'), 'utf8',
);
const shape = captured.match(
  /https:\/\/pay\.hotmart\.com\/D98014973Y\?off=[A-Za-z0-9]+&checkoutMode=10&src=hermes&sck=[A-Za-z0-9_~%-]+~hermes~v1~[0-9A-Z]{26}/,
);
if (!shape) throw new Error('el fixture del link de ATT1 no es el capturado');
const base = shape[0].replace(/[0-9A-Z]{26}$/, '');

const ACCOUNT = 1;
const INBOX = 11;
const CASE_A = '00000000-0000-4000-8000-00000000a001';
const CASE_B = '00000000-0000-4000-8000-00000000b001';
const CART_A = '00000000-0000-4000-8000-00000000c001';
const CONTACT_A = '00000000-0000-4000-8000-0000000c0a01';
const CONTACT_B = '00000000-0000-4000-8000-0000000c0b01';
const NOW = '2026-10-25T18:00:00Z';

await db.exec(`set session_replication_role = replica;`);
await db.query(`
  insert into public.commercial_cases
    (id, case_kind, contact_id, status, inbound_scope_key, inbound_scope_version, tenant_ref)
  values ($1, 'inbound_sales', $2, 'active', 'att1-inbound', 1, 'att1'),
         ($3, 'inbound_sales', $4, 'active', 'att1-inbound', 1, 'att1')
`, [CASE_A, CONTACT_A, CASE_B, CONTACT_B]);

let sequence = 0;
const ulid = () => {
  sequence += 1;
  return `01M4GR1RSK8DJY4XVHMCY3${String(sequence).padStart(4, '0')}`;
};
const issue = async ({
  contact = CONTACT_A, account = ACCOUNT, inbox = INBOX, status = 'accepted_by_chatwoot',
  finalizedAt, kind = 'inbound_request', recoveryCase = null,
}) => {
  const id = ulid();
  const cart = kind === 'recovery_first_touch';
  const url = `${base}${id}`;
  const sck = url.split('&sck=')[1];
  const accepted = status === 'accepted_by_chatwoot';
  const matched = status === 'purchase_matched';
  await db.query(`
    insert into public.checkout_link_issuances
      (issuance_ulid, commercial_case_id, purchase_intent_id, contact_id,
       channel_identity_id, offer_catalog_id, chatwoot_account_id, chatwoot_inbox_id,
       chatwoot_conversation_id, trigger_external_message_id, source_kind,
       source_value, sck_format_version, sck_value, checkout_url_final, status,
       chatwoot_message_id, finalized_at, recovery_case_id, delivery_attempt_id,
       hotmart_webhook_event_id, purchased_at, created_at, failure_code)
    values ($1, $2, gen_random_uuid(), $3, gen_random_uuid(), gen_random_uuid(),
            $4, $5, $6, $7, $8, 'hermes', 'v1', $9, $10, $11, $12, $13, $14, $15,
            $16, $17, $13, $18)
  `, [
    id, cart ? recoveryCase : CASE_A, contact, account, inbox,
    cart ? null : 500 + sequence, cart ? null : String(9000 + sequence), kind,
    decodeURIComponent(sck), url, status,
    accepted || matched ? 3000 + sequence : null,
    finalizedAt,
    cart ? recoveryCase : null, cart ? '00000000-0000-4000-8000-0000000da001' : null,
    matched ? '00000000-0000-4000-8000-0000000e0001' : null,
    matched ? finalizedAt : null,
    status === 'delivery_unknown' ? 'chatwoot_timeout' : null,
  ]);
  return url;
};
const coupon = async ({ ordinal, code, status, expiresAt }) => {
  await db.query(`
    insert into public.recovery_coupons
      (recovery_case_id, ordinal, code, external_product_id, discount, expires_at,
       status, hotmart_start_ms, hotmart_end_ms, verified_at)
    values ($1, $2, $3, 'P1', 0.1, $4, $5, 1, 2, case when $5 = 'valid' then now() end)
  `, [CART_A, ordinal, code, expiresAt, status]);
};
const read = async (caseId = CASE_A, inbox = INBOX, account = ACCOUNT, now = NOW) => (await db.query(`
  select checkout_url_final, source_kind, accepted_at, coupon_code
  from public.read_handoff_note_checkout_link_v1($1, $2, $3, $4)
`, [caseId, account, inbox, now])).rows;
const expectOne = (label, rows, url, kind, couponCode) => {
  if (rows.length !== 1 || rows[0].checkout_url_final !== url
      || rows[0].source_kind !== kind || rows[0].coupon_code !== couponCode) {
    throw new Error(`${label}: ${JSON.stringify(rows)}`);
  }
};

// 1. Sin ningun link: cero filas (D-091-1, no hay nota).
if ((await read()).length !== 0) throw new Error('sin link tenia que dar cero filas');

// 2. El ultimo aceptado gana.
await issue({ finalizedAt: '2026-10-20T10:00:00Z' });
const latest = await issue({ finalizedAt: '2026-10-21T10:00:00Z' });
expectOne('el ultimo aceptado', await read(), latest, 'inbound_request', null);

// 3. Uno mas nuevo que no se acepto, o ya comprado, no cuenta.
await issue({ status: 'delivery_unknown', finalizedAt: '2026-10-22T10:00:00Z' });
await issue({ status: 'purchase_matched', finalizedAt: '2026-10-23T10:00:00Z' });
expectOne('ignora lo no aceptado y lo comprado', await read(), latest, 'inbound_request', null);

// 4. Otro inbox, otra cuenta y otro contacto no cuentan.
await issue({ inbox: 12, finalizedAt: '2026-10-24T10:00:00Z' });
await issue({ account: 2, finalizedAt: '2026-10-24T11:00:00Z' });
await issue({ contact: CONTACT_B, finalizedAt: '2026-10-24T12:00:00Z' });
expectOne('solo su contacto, cuenta e inbox', await read(), latest, 'inbound_request', null);
if ((await read(CASE_A, 12)).length !== 1) throw new Error('el inbox 12 tiene su propio link');

// 5. Un link aceptado despues de p_now no se ve.
expectOne('antes de p_now', await read(CASE_A, INBOX, ACCOUNT, '2026-10-20T12:00:00Z'),
  (await db.query(`select checkout_url_final from public.checkout_link_issuances
    where finalized_at = '2026-10-20T10:00:00Z'`)).rows[0].checkout_url_final,
  'inbound_request', null);

// 6. El del carrito (va atado al caso del carrito, sin conversacion) gana si es
// el mas nuevo y trae el cupon vigente de ordinal mas alto.
const cart = await issue({
  kind: 'recovery_first_touch', recoveryCase: CART_A, finalizedAt: '2026-10-25T09:00:00Z',
});
if ((await read()).length !== 1 || (await read())[0].coupon_code !== null) {
  throw new Error(`carrito sin cupon: ${JSON.stringify(await read())}`);
}
await coupon({ ordinal: 1, code: 'ATT1A1K7M2Q9', status: 'valid', expiresAt: '2026-10-26T09:00:00Z' });
expectOne('carrito con su cupon', await read(), cart, 'recovery_first_touch', 'ATT1A1K7M2Q9');
await coupon({ ordinal: 2, code: 'ATT1A2R4T8W3', status: 'created', expiresAt: '2026-10-27T09:00:00Z' });
expectOne('la renovacion gana', await read(), cart, 'recovery_first_touch', 'ATT1A2R4T8W3');
expectOne('el cupon vencido no va', await read(CASE_A, INBOX, ACCOUNT, '2026-10-28T00:00:00Z'),
  cart, 'recovery_first_touch', null);

// 7. Un caso que no existe: cero filas. Argumentos invalidos: 22023.
if ((await read('00000000-0000-4000-8000-000000000999')).length !== 0) {
  throw new Error('un caso que no existe tenia que dar cero filas');
}
for (const [label, args] of [
  ['inbox 0', [CASE_A, ACCOUNT, 0, NOW]],
  ['cuenta nula', [CASE_A, null, INBOX, NOW]],
  ['sin ahora', [CASE_A, ACCOUNT, INBOX, null]],
]) {
  try {
    await db.query('select * from public.read_handoff_note_checkout_link_v1($1, $2, $3, $4)', args);
  } catch (error) {
    if (!String(error.message).includes('invalid_handoff_link_note_read')) {
      throw new Error(`${label}: fallo por otra cosa: ${error.message}`);
    }
    continue;
  }
  throw new Error(`${label}: tenia que rechazarse`);
}

// 8. Solo service_role la ejecuta, y es security definer de solo lectura.
const acl = (await db.query(`
  select r.rolname, has_function_privilege(r.rolname,
    'public.read_handoff_note_checkout_link_v1(uuid,bigint,bigint,timestamptz)', 'EXECUTE') as can
  from pg_roles r where r.rolname in ('anon','authenticated','service_role')
`)).rows;
const canExecute = Object.fromEntries(acl.map((row) => [row.rolname, row.can]));
if (canExecute.anon || canExecute.authenticated || !canExecute.service_role) {
  throw new Error(`grants inesperados: ${JSON.stringify(canExecute)}`);
}
const meta = (await db.query(`
  select prosecdef, provolatile from pg_proc
  where oid = 'public.read_handoff_note_checkout_link_v1(uuid,bigint,bigint,timestamptz)'::regprocedure
`)).rows[0];
if (!meta.prosecdef || meta.provolatile !== 's') {
  throw new Error(`la funcion tenia que ser security definer y stable: ${JSON.stringify(meta)}`);
}

console.log('handoff link note: ok');
