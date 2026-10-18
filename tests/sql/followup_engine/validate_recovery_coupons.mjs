// Valida el cupon personal del carrito abandonado (20261018000100) contra la
// cadena completa de migraciones, ejecutando las RPC: sembrar -> reclamar con
// lease -> settle created -> settle valid -> lectura del emisor de A1.
//
// Lo que importa y no se ve leyendo el SQL:
//   * la siembra se cuenta desde el due_at del first_contact_review menos
//     p_lead, y el vencimiento prometido (expires_at) desde ese mismo due_at,
//     no desde la hora en que se sembro ni desde el abandono;
//   * un settle repetido despues de aplicado devuelve applied = false, aunque
//     el lease ya se haya liberado; un settle distinto sin lease falla;
//   * la compra o la baja del contacto vencen (expired,
//     case_no_longer_eligible) la fila que no llego a crearse en Hotmart, en
//     vez de devolverla al worker;
//   * el tope de dos codigos por caso lo impone la tabla (ordinal 1 o 2,
//     unico por caso), no el worker;
//   * create_unknown con next_attempt_at futuro no se reclama antes de esa
//     hora, y un request_started cuyo lease vencio vuelve como create_unknown.
//
// Los casos se arman con filas minimas insertadas a mano (contacto, evento,
// caso, secuencia y accion), respetando los CHECK, en vez de pasar por la
// admision de Hotmart: lo que se prueba aca es la tabla y sus tres RPC.
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
console.log('recovery_coupons_migration_apply=OK');

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

const T0 = Date.parse('2030-01-01T00:00:00.000Z');
const at = (minutes) => new Date(T0 + minutes * 60_000).toISOString();

await db.exec(`
  insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
  ) values (
    'coupon-test', 1, 'published', 'cart_recovery', 'UTC',
    '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
    interval '60 minutes', interval '7 days', 3,
    '[{"step_key":"first_contact","mode":"freeform"}]'::jsonb,
    'operator-test', now(), now()
  );
`);

let seq = 0;
// Un carrito abandonado en T0 con su first_contact_review a dueMinutes.
const makeCart = async ({ dueMinutes = 60, product = '3526906' } = {}) => {
  seq += 1;
  const contact = (await db.query(`
    insert into public.contacts (full_name, email, phone)
    values ($1, $2, $3) returning id
  `, [`Buyer ${seq}`, `buyer${seq}@example.com`, `52155500000${String(seq).padStart(2, '0')}`])).rows[0].id;
  const event = (await db.query(`
    insert into public.webhook_events (
      source, external_event_id, event_type, payload, processing_status
    ) values ('hotmart', $1, 'PURCHASE_OUT_OF_SHOPPING_CART', '{}'::jsonb, 'processed')
    returning id
  `, [`coupon-cart-${seq}`])).rows[0].id;
  const recoveryCase = (await db.query(`
    insert into public.recovery_cases (
      contact_id, abandonment_event_id, external_product_id, product_name,
      offer_code, status, grace_expires_at, policy_key, policy_version
    ) values ($1, $2, $3, 'Product', 'offer-a', 'grace_period', $4::timestamptz,
              'coupon-test', 1)
    returning id
  `, [contact, event, product, at(dueMinutes)])).rows[0].id;
  const sequence = (await db.query(`
    insert into public.followup_sequences (
      recovery_case_id, reason, policy_key, policy_version, max_attempts
    ) values ($1, 'cart_abandonment', 'coupon-test', 1, 3)
    returning id
  `, [recoveryCase])).rows[0].id;
  const action = (await db.query(`
    insert into public.scheduled_actions (
      recovery_case_id, followup_sequence_id, action_type, status, due_at,
      expires_at, expected_case_version, idempotency_key, policy_key,
      policy_version, step_key, anchor_type, anchor_subject_internal_id,
      anchor_observed_at
    ) values (
      $1, $2, 'first_contact_review', 'pending', $3::timestamptz,
      $3::timestamptz + interval '7 days', 1, $4, 'coupon-test', 1,
      'first_contact', 'cart_abandonment', $5, $6::timestamptz
    ) returning id, due_at
  `, [recoveryCase, sequence, at(dueMinutes), `coupon-action-${seq}`, event, at(0)])).rows[0];
  return { contact, event, recoveryCase, sequence, action };
};

const CLAIM = `
  select * from public.claim_due_recovery_coupons_v1(
    $1::timestamptz, $2, 120, 'ATT', interval '15 minutes', interval '24 hours',
    0.10, 50
  )
`;
const claim = async (minutes, worker = 'worker-a') => (
  await db.query(CLAIM, [at(minutes), worker])
).rows;
const SETTLE = `
  select * from public.settle_recovery_coupon_v1(
    $1::uuid, $2, $3, $4::bigint, $5::bigint, $6::bigint, $7, $8::timestamptz,
    $9::timestamptz
  )
`;
const settle = async ({
  id, worker = 'worker-a', status, hotmartId = null, startMs = null, endMs = null,
  failure = null, nextAttempt = null, minutes,
}) => (await db.query(SETTLE, [
  id, worker, status, hotmartId, startMs, endMs, failure, nextAttempt, at(minutes),
])).rows[0];
const coupon = async (id) => (await db.query(
  'select * from public.recovery_coupons where id = $1', [id],
)).rows[0];
const couponsOf = async (recoveryCase) => (await db.query(
  'select * from public.recovery_coupons where recovery_case_id = $1 order by ordinal',
  [recoveryCase],
)).rows;

// --- 1. la siembra respeta p_lead y el vencimiento sale del due_at ----------

const cart = await makeCart({ dueMinutes: 60 });
const early = await claim(40);
if (early.length !== 0 || (await couponsOf(cart.recoveryCase)).length !== 0) {
  fail('seeded before due_at - p_lead', early);
}
const first = await claim(46);
if (first.length !== 1
    || first[0].recovery_case_id !== cart.recoveryCase
    || first[0].status !== 'reserved'
    || first[0].attempt_count !== 1
    || first[0].external_product_id !== '3526906'
    || !/^ATT[0-9A-F]{12}$/.test(first[0].code)
    || new Date(first[0].expires_at).getTime()
      !== new Date(cart.action.due_at).getTime() + 24 * 3_600_000) {
  fail('first seed diverged', first);
}
const seeded = await coupon(first[0].coupon_id);
if (seeded.ordinal !== 1 || Number(seeded.discount) !== 0.1
    || seeded.lease_owner !== 'worker-a'
    || new Date(seeded.lease_expires_at).getTime() !== Date.parse(at(46)) + 120_000) {
  fail('seeded row diverged', seeded);
}
console.log('recovery_coupons_seed_window=OK');

// --- 2. otra llamada no siembra otro ni devuelve la fila con lease ----------

const again = await claim(47, 'worker-b');
if (again.length !== 0 || (await couponsOf(cart.recoveryCase)).length !== 1) {
  fail('second claim seeded again or stole a live lease', again);
}
console.log('recovery_coupons_single_seed_and_live_lease=OK');

// --- 3. otro worker no puede settlear ----------------------------------------

await expectError('foreign lease settle', () => settle({
  id: seeded.id, worker: 'worker-b', status: 'request_started', minutes: 47,
}), 'recovery_coupon_lease_not_held');

// --- 4. request_started -> created -> (reverificar) -> valid ----------------

const startMs = Date.parse(at(47));
const endMs = new Date(seeded.expires_at).getTime();
const started = await settle({ id: seeded.id, status: 'request_started', minutes: 47 });
if (started.applied !== true || (await coupon(seeded.id)).lease_owner !== 'worker-a') {
  fail('request_started did not keep the lease', started);
}
const created = await settle({
  id: seeded.id, status: 'created', hotmartId: 991, startMs, endMs,
  nextAttempt: at(53), minutes: 47,
});
const afterCreated = await coupon(seeded.id);
if (created.applied !== true || afterCreated.status !== 'created'
    || afterCreated.lease_owner !== null
    || Number(afterCreated.hotmart_coupon_id) !== 991
    || new Date(afterCreated.next_attempt_at).toISOString() !== at(53)) {
  fail('created settle diverged', { created, afterCreated });
}
// Reclamar para verificar: no antes de next_attempt_at.
if ((await claim(50)).length !== 0) fail('created claimed before next_attempt_at', null);
const verify = await claim(53);
if (verify.length !== 1 || verify[0].status !== 'created' || verify[0].attempt_count !== 2) {
  fail('created was not offered for verification', verify);
}
// Todavia no aplica: reverificar mas tarde.
const recheck = await settle({
  id: seeded.id, status: 'created', nextAttempt: at(55), minutes: 53,
});
if (recheck.applied !== true) fail('created recheck not applied', recheck);
const verify2 = await claim(55);
if (verify2.length !== 1) fail('created recheck not offered', verify2);
const valid = await settle({ id: seeded.id, status: 'valid', minutes: 55 });
const afterValid = await coupon(seeded.id);
if (valid.applied !== true || afterValid.status !== 'valid'
    || new Date(afterValid.verified_at).toISOString() !== at(55)
    || afterValid.lease_owner !== null || afterValid.next_attempt_at !== null) {
  fail('valid settle diverged', { valid, afterValid });
}
const repeat = await settle({ id: seeded.id, status: 'valid', minutes: 56 });
if (repeat.applied !== false || repeat.status !== 'valid'
    || new Date((await coupon(seeded.id)).verified_at).toISOString() !== at(55)) {
  fail('repeated settle changed something', repeat);
}
await expectError('settle without lease', () => settle({
  id: seeded.id, status: 'create_failed', failure: 'x', minutes: 56,
}), 'recovery_coupon_lease_not_held');
const read = (await db.query(
  'select * from public.get_recovery_coupon_for_case_v1($1::uuid, 1::smallint)',
  [cart.recoveryCase],
)).rows;
const readNone = (await db.query(
  'select * from public.get_recovery_coupon_for_case_v1($1::uuid, 2::smallint)',
  [cart.recoveryCase],
)).rows;
if (read.length !== 1 || read[0].code !== seeded.code || read[0].status !== 'valid'
    || readNone.length !== 0) {
  fail('read for the A1 sender diverged', { read, readNone });
}
console.log('recovery_coupons_settle_lifecycle=OK');

// --- 5. compra y baja: no siembra, y lo reservado vence ----------------------

const bought = await makeCart({ dueMinutes: 60 });
const optedOut = await makeCart({ dueMinutes: 60 });
const boughtLater = await makeCart({ dueMinutes: 60 });
const optedOutLater = await makeCart({ dueMinutes: 60 });
// Una fila reserved de cada caso "later", sin lease, antes de que deje de ser
// elegible.
for (const [index, item] of [boughtLater, optedOutLater].entries()) {
  await db.query(`
    insert into public.recovery_coupons (
      recovery_case_id, ordinal, code, external_product_id, discount, expires_at
    ) values ($1, 1, $2, '3526906', 0.10, $3::timestamptz)
  `, [item.recoveryCase, `ATTLATER0000${index}`, at(60 + 24 * 60)]);
}
const purchase = async (item, n) => {
  const purchaseEvent = (await db.query(`
    insert into public.webhook_events (
      source, external_event_id, event_type, payload, processing_status
    ) values ('hotmart', $1, 'PURCHASE_APPROVED', '{}'::jsonb, 'processed')
    returning id
  `, [`coupon-purchase-${n}`])).rows[0].id;
  await db.query(`
    update public.recovery_cases
    set status = 'won', won_at = $2::timestamptz, purchase_event_id = $3
    where id = $1
  `, [item.recoveryCase, at(30), purchaseEvent]);
};
const optOut = async (item) => db.query(`
  update public.contacts
  set contact_permission = 'opted_out', lifecycle_status = 'do_not_contact'
  where id = $1
`, [item.contact]);
await purchase(bought, 1);
await purchase(boughtLater, 2);
await optOut(optedOut);
await optOut(optedOutLater);
const afterStop = await claim(46);
const stopIds = new Set([bought, optedOut, boughtLater, optedOutLater].map((x) => x.recoveryCase));
if (afterStop.some((row) => stopIds.has(row.recovery_case_id))) {
  fail('an ineligible case was returned', afterStop);
}
for (const item of [bought, optedOut]) {
  if ((await couponsOf(item.recoveryCase)).length !== 0) {
    fail('an ineligible case was seeded', item.recoveryCase);
  }
}
for (const item of [boughtLater, optedOutLater]) {
  const rows = await couponsOf(item.recoveryCase);
  if (rows.length !== 1 || rows[0].status !== 'expired'
      || rows[0].failure_code !== 'case_no_longer_eligible'
      || rows[0].attempt_count !== 0) {
    fail('reserved row of an ineligible case was not expired', rows);
  }
}
console.log('recovery_coupons_purchase_and_opt_out_stop=OK');

// --- 6. el tope de dos codigos por caso --------------------------------------

const insertCoupon = (recoveryCase, ordinal, code) => db.query(`
  insert into public.recovery_coupons (
    recovery_case_id, ordinal, code, external_product_id, discount, expires_at
  ) values ($1, $2, $3, '3526906', 0.10, $4::timestamptz)
`, [recoveryCase, ordinal, code, at(24 * 60)]);
await expectError('third ordinal', () => insertCoupon(cart.recoveryCase, 3, 'ATTORDINAL3X'),
  'recovery_coupons_ordinal_check');
await expectError('second ordinal 1', () => insertCoupon(cart.recoveryCase, 1, 'ATTORDINAL1X'),
  'recovery_coupons_recovery_case_id_ordinal_key');
await insertCoupon(cart.recoveryCase, 2, 'ATTRENEWAL01');
await expectError('duplicate code', () => insertCoupon(bought.recoveryCase, 2, 'ATTRENEWAL01'),
  'recovery_coupons_code_key');
await expectError('bad code shape', () => insertCoupon(bought.recoveryCase, 2, 'att-lower'),
  'recovery_coupons_code_check');
console.log('recovery_coupons_two_codes_cap=OK');

// --- 7. el trigger: identidad inmutable y estados terminales ----------------

await expectError('code change', () => db.query(
  `update public.recovery_coupons set code = 'ATTCHANGED01' where id = $1`, [seeded.id],
), 'recovery_coupon_identity_immutable');
await expectError('expires_at change', () => db.query(
  `update public.recovery_coupons set expires_at = expires_at + interval '1 hour' where id = $1`,
  [seeded.id],
), 'recovery_coupon_identity_immutable');
const expiredRow = (await couponsOf(boughtLater.recoveryCase))[0];
await expectError('expired back to reserved', () => db.query(
  `update public.recovery_coupons set status = 'reserved' where id = $1`, [expiredRow.id],
), 'recovery_coupon_terminal_status');
await expectError('delete', () => db.query(
  'delete from public.recovery_coupons where id = $1', [seeded.id],
), 'recovery_coupon_append_only');
console.log('recovery_coupons_trigger=OK');

// --- 8. create_unknown con next_attempt_at y request_started abandonado -----

const unknownCart = await makeCart({ dueMinutes: 120 });
const unknownClaim = (await claim(106)).filter((row) => row.recovery_case_id === unknownCart.recoveryCase);
if (unknownClaim.length !== 1) fail('unknown cart not seeded', unknownClaim);
const unknownId = unknownClaim[0].coupon_id;
const unknown = await settle({
  id: unknownId, status: 'create_unknown', failure: 'hotmart_timeout',
  nextAttempt: at(116), minutes: 106,
});
if (unknown.applied !== true) fail('create_unknown not applied', unknown);
if ((await claim(110)).some((row) => row.coupon_id === unknownId)) {
  fail('create_unknown claimed before next_attempt_at', null);
}
const retry = (await claim(116)).filter((row) => row.coupon_id === unknownId);
if (retry.length !== 1 || retry[0].status !== 'create_unknown' || retry[0].attempt_count !== 2) {
  fail('create_unknown not retried at next_attempt_at', retry);
}
await settle({ id: unknownId, status: 'request_started', minutes: 116 });
// El worker muere: el lease (120 s) vence y vuelve como create_unknown.
if ((await claim(117)).some((row) => row.coupon_id === unknownId)) {
  fail('request_started with a live lease was offered', null);
}
const recovered = (await claim(119)).filter((row) => row.coupon_id === unknownId);
const recoveredRow = await coupon(unknownId);
if (recovered.length !== 1 || recovered[0].status !== 'create_unknown'
    || recoveredRow.failure_code !== 'lease_expired_after_request_started') {
  fail('abandoned request_started did not come back as create_unknown', { recovered, recoveredRow });
}
await expectError('invalid transition', () => settle({
  id: unknownId, status: 'valid', startMs: 1, endMs: 2, minutes: 119,
}), 'recovery_coupon_invalid_transition');
const failed = await settle({
  id: unknownId, status: 'create_failed', failure: 'hotmart_rejected', minutes: 119,
});
if (failed.applied !== true || (await coupon(unknownId)).status !== 'create_failed') {
  fail('create_failed not applied', failed);
}
console.log('recovery_coupons_retry_schedule=OK');

// --- 9. entrada invalida ------------------------------------------------------

await expectError('bad prefix', () => db.query(`
  select * from public.claim_due_recovery_coupons_v1(
    $1::timestamptz, 'w', 120, 'A', interval '15 minutes', interval '24 hours', 0.10, 5
  )
`, [at(0)]), 'invalid_recovery_coupon_claim_input');
await expectError('bad discount', () => db.query(`
  select * from public.claim_due_recovery_coupons_v1(
    $1::timestamptz, 'w', 120, 'ATT', interval '15 minutes', interval '24 hours', 0.99, 5
  )
`, [at(0)]), 'invalid_recovery_coupon_claim_input');
console.log('recovery_coupons_input_validation=OK');

// --- 10. grants ---------------------------------------------------------------

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
    to_regprocedure('public.claim_due_recovery_coupons_v1(timestamptz,text,integer,text,interval,interval,numeric,integer)'),
    to_regprocedure('public.settle_recovery_coupon_v1(uuid,text,text,bigint,bigint,bigint,text,timestamptz,timestamptz)'),
    to_regprocedure('public.get_recovery_coupon_for_case_v1(uuid,smallint)'),
    to_regprocedure('public.protect_recovery_coupon()')
  )
  order by 1
`)).rows;
const rpcs = acl.filter((row) => !row.signature.startsWith('protect_'));
const trigger = acl.find((row) => row.signature.startsWith('protect_'));
if (acl.length !== 4
    || rpcs.some((row) => !row.security_definer || row.anon_x || row.auth_x || !row.service_x
      || row.config !== 'search_path=pg_catalog, public, pg_temp')
    || trigger.anon_x || trigger.auth_x || trigger.service_x) {
  fail('function ACL diverged', acl);
}
const table = (await db.query(`
  select c.relrowsecurity rls,
    exists (
      select 1
      from unnest(array['anon', 'authenticated', 'service_role']) role_name
      cross join unnest(array['select', 'insert', 'update', 'delete']) privilege_name
      where has_table_privilege(role_name, c.oid, privilege_name)
    ) api_privilege
  from pg_class c where c.oid = to_regclass('public.recovery_coupons')
`)).rows[0];
if (table?.rls !== true || table.api_privilege !== false) fail('table ACL diverged', table);
await expectError('anon claim', async () => {
  try {
    await db.exec(`
      set role anon;
      select * from public.claim_due_recovery_coupons_v1(
        now(), 'w', 120, 'ATT', interval '15 minutes', interval '24 hours', 0.10, 5
      );
    `);
  } finally {
    await db.exec('reset role');
  }
}, 'permission denied');
await expectError('anon read', async () => {
  try {
    await db.exec(`
      set role anon;
      select * from public.get_recovery_coupon_for_case_v1(gen_random_uuid(), 1::smallint);
    `);
  } finally {
    await db.exec('reset role');
  }
}, 'permission denied');
console.log('recovery_coupons_grants=OK');

await db.close();
console.log('recovery_coupons=OK');
