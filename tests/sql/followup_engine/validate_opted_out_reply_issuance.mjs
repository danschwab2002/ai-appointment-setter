// F-036 (decisiones D9 y D10 de Dan, 2026-10-24): quien se dio de baja y
// escribe por su cuenta recibe el link si lo pide. Migracion 20261024000400.
//
// Este validador prueba:
//   0. que la migracion cambia exactamente lo que dice: un helper nuevo,
//      _chatwoot_reply_follows_opt_out_v1, que ningun rol de la API ejecuta, y
//      cuatro funciones reemplazadas (las tres reservas del link del agente y
//      su autorizacion) con solo dos inserciones cada una; ninguna otra
//      definicion ni grant cambia. En particular no cambian los reclamos del
//      seguimiento (compartido y portable) ni el de la reactivacion, que siguen
//      frenando a un contacto dado de baja antes de reservar;
//   1. la tabla de verdad del helper sobre contact_opt_out_events: sin baja,
//      falso; con una baja anterior al mensaje (por la conversacion, por la
//      otra forma del movil o por el contacto), verdadero; con una baja igual
//      o posterior al mensaje, falso; la baja de otra cuenta o de otro inbox no
//      cuenta; un id de mensaje que no es un numero, falso.
//
// El recorrido de punta a punta (formulario, baja aplicada al contacto,
// conversacion nueva, reserva y autorizacion, la conversacion de la baja
// frenada y una baja nueva que vuelve a frenar) esta en
// validate_whatsapp_phone_equivalence.mjs, seccion 10 e2, con el resto del
// enlace del entrante. Los telefonos son sinteticos.
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

const fail = (message, detail) => {
  throw new Error(`${message}: ${JSON.stringify(detail)}`);
};

const TARGET = '20261024000400_opted_out_reply_checkout_issuance.sql';
const migrationNames = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const targetIndex = migrationNames.indexOf(TARGET);
if (targetIndex < 0) throw new Error(`${TARGET} is missing from the canonical stack`);
const apply = async (file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
));
const snapshot = async () => new Map((await db.query(`
  select p.oid::regprocedure::text as signature,
         pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));

await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of migrationNames.slice(0, targetIndex)) {
  await apply(join(root, 'supabase/migrations', name));
}
const before = await snapshot();
await apply(join(root, 'supabase/migrations', TARGET));
const after = await snapshot();
for (const name of migrationNames.slice(targetIndex + 1)) {
  await apply(join(root, 'supabase/migrations', name));
}

// ---------------------------------------------------------------------------
// 0. El alcance de la migracion.
// ---------------------------------------------------------------------------
const HELPER = '_chatwoot_reply_follows_opt_out_v1(uuid,bigint,bigint,bigint,text,text)';
const REPLACED = [
  'reserve_chatwoot_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamp with time zone)',
  'reserve_portable_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamp with time zone)',
  'reserve_portable_checkout_issuance_for_funnel_v1(uuid,text,bigint,bigint,bigint,text,text,text,timestamp with time zone)',
  'authorize_chatwoot_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,timestamp with time zone)',
];
const MUST_NOT_CHANGE = [
  'claim_conversation_followup_v1',
  'claim_portable_conversation_followup_v1',
  'claim_conversation_reactivation',
  'has_chatwoot_opt_out_stop(bigint,bigint,bigint,text)',
  'apply_chatwoot_inbound_opt_out(bigint,bigint,bigint,bigint,text,timestamp with time zone,text)',
  'reconcile_chatwoot_opt_out_stop(bigint,bigint,bigint,text)',
];

const added = [...after.keys()].filter((signature) => !before.has(signature));
const removed = [...before.keys()].filter((signature) => !after.has(signature));
const changed = [...after.keys()].filter((signature) => before.has(signature)
  && (before.get(signature).definition !== after.get(signature).definition
    || before.get(signature).acl !== after.get(signature).acl));
if (JSON.stringify(added) !== JSON.stringify([HELPER]) || removed.length !== 0
    || JSON.stringify([...changed].sort()) !== JSON.stringify([...REPLACED].sort())) {
  fail('the migration changed something else', { added, removed, changed });
}
for (const name of MUST_NOT_CHANGE) {
  const matches = [...after.keys()].filter((signature) => signature === name
    || signature.startsWith(`${name}(`));
  if (matches.length === 0) fail('a guarded function is missing', name);
  for (const signature of matches) {
    if (changed.includes(signature)
        || after.get(signature).definition.includes('_chatwoot_reply_follows_opt_out_v1')) {
      fail('a proactive path learned the opted-out reply', signature);
    }
  }
}
for (const name of ['claim_conversation_followup_v1', 'claim_portable_conversation_followup_v1',
  'claim_conversation_reactivation']) {
  const [signature] = [...after.keys()].filter((key) => key.startsWith(`${name}(`));
  if (!/contact_permission in \(\s*'opted_out', 'blocked', 'restricted'\s*\)/
    .test(after.get(signature).definition)) {
    fail('a proactive claim no longer stops an opted-out contact', signature);
  }
}

// Cada reemplazo es la definicion anterior con dos inserciones y nada mas.
const CONTACT_OLD = "       or v_contact.contact_permission in ('opted_out', 'blocked', 'restricted')\n"
  + "       or v_contact.lifecycle_status = 'do_not_contact' then\n";
const CONTACT_NEW = "       or v_contact.contact_permission in ('blocked', 'restricted')\n"
  + '       or (\n'
  + "           (v_contact.contact_permission = 'opted_out'\n"
  + "            or v_contact.lifecycle_status = 'do_not_contact')\n"
  + '           -- F-036: la baja no frena el link que pide quien escribio despues.\n'
  + '           and not (\n'
  + "               v_contact.contact_permission = 'opted_out'\n"
  + '               and public._chatwoot_reply_follows_opt_out_v1(\n'
  + '                   v_contact.id, p_chatwoot_account_id, p_chatwoot_inbox_id,\n'
  + '                   p_chatwoot_conversation_id, p_external_user_id,\n'
  + '                   p_trigger_external_message_id\n'
  + '               )\n'
  + '           )\n'
  + '       ) then\n';
const GUARD = '    if not public._chatwoot_reply_follows_opt_out_v1(\n'
  + '        v_contact.id, p_chatwoot_account_id, p_chatwoot_inbox_id,\n'
  + '        p_chatwoot_conversation_id, p_external_user_id,\n'
  + '        p_trigger_external_message_id\n'
  + '    ) and ';
for (const signature of REPLACED) {
  const old = before.get(signature).definition;
  const now = after.get(signature).definition;
  const restored = now.replace(CONTACT_NEW, CONTACT_OLD).replace(GUARD, '    if ');
  if (restored !== old || now.split('_chatwoot_reply_follows_opt_out_v1(').length !== 3) {
    fail('a replacement is not the old definition plus the two insertions', signature);
  }
}

// El helper es interno: lo llaman funciones security definer.
const helperAcl = (await db.query(`
  select rolname, has_function_privilege(rolname, $1, 'EXECUTE') as can
  from pg_roles where rolname in ('anon', 'authenticated', 'service_role')
`, [`public.${HELPER}`])).rows;
if (helperAcl.length !== 3 || helperAcl.some((row) => row.can)) {
  fail('an API role can execute the helper', helperAcl);
}

// ---------------------------------------------------------------------------
// 1. La tabla de verdad del helper.
// ---------------------------------------------------------------------------
const ACCOUNT = 2;
const INBOX = 11;
let message = 70000;
const optOut = async ({
  conversation, userId, messageId, account = ACCOUNT, inbox = INBOX,
}) => {
  message += 1;
  await db.query(`
    insert into public.contact_opt_out_events (
      contact_id, channel, purpose, source, canonical_account_id,
      canonical_inbox_id, canonical_conversation_id, canonical_message_id,
      external_user_id, occurred_at, normalized_rule_key, correlation_status
    ) values (null, 'whatsapp', 'cart_recovery', 'chatwoot', $1, $2, $3, $4, $5,
      now(), 'unsubscribe', 'unmatched')
  `, [account, inbox, conversation, messageId ?? message, userId]);
};
const follows = async (conversation, userId, trigger, contactId = null) => (await db.query(`
  select public._chatwoot_reply_follows_opt_out_v1($1::uuid, $2, $3, $4, $5, $6) as value
`, [contactId, ACCOUNT, INBOX, conversation, userId, trigger])).rows[0].value;

const cases = [];
const expect = async (label, value, expected) => {
  if (value !== expected) fail(`helper: ${label}`, { value, expected });
  cases.push(`${label}: ${value}`);
};

// Sin ninguna baja no hay nada que dejar pasar: la reserva sigue como siempre.
await expect('no opt-out', await follows(500, '5215500000501', '80000'), false);
// La baja anterior al mensaje, en otra conversacion, bajo el wa_id (521…).
await optOut({ conversation: 501, userId: '5215500000502', messageId: 1000 });
await expect('older opt-out, same wa_id', await follows(502, '5215500000502', '2000'), true);
// La misma baja, buscada con la forma del formulario (52…).
await expect('older opt-out, other form', await follows(503, '525500000502', '2000'), true);
// El mensaje no es posterior: igual id o anterior.
await expect('same message id', await follows(502, '5215500000502', '1000'), false);
await expect('earlier message', await follows(502, '5215500000502', '999'), false);
// Una baja nueva, posterior al mensaje, vuelve a frenar.
await optOut({ conversation: 504, userId: '5215500000502', messageId: 3000 });
await expect('a newer opt-out stops', await follows(502, '5215500000502', '2000'), false);
await expect('after the newest opt-out', await follows(502, '5215500000502', '3001'), true);
// La baja de esta conversacion, con otro telefono, tambien cuenta.
await optOut({ conversation: 510, userId: '5215500000599', messageId: 5000 });
await expect('opt-out of this conversation, newer', await follows(510, '5215500000510', '4000'), false);
await expect('opt-out of this conversation, older', await follows(510, '5215500000510', '6000'), true);
// Otra cuenta u otro inbox no cuentan: sin baja propia, falso.
await optOut({ conversation: 520, userId: '5215500000520', messageId: 100, account: 9 });
await optOut({ conversation: 521, userId: '5215500000520', messageId: 101, inbox: 99 });
await expect('only other account or inbox', await follows(522, '5215500000520', '200'), false);
// Un id que no es un numero, o demasiado largo, no deja pasar nada.
await expect('trigger not numeric', await follows(502, '5215500000502', 'abc'), false);
await expect('trigger too long', await follows(502, '5215500000502', '1'.repeat(19)), false);
await expect('trigger null', await follows(502, '5215500000502', null), false);

console.log(JSON.stringify({
  opted_out_reply_issuance: 'OK',
  migration_scope: { added: added.length, replaced: changed.length },
  helper: cases,
}));
await db.close();
