// Valida el registro de reactivaciones contra el esquema real, ejecutando las
// RPC: reservar -> comprobar idempotencia -> cerrar como entregada -> comprobar
// el limite -> soltar una fallida y comprobar que se puede reintentar.
//
// El paso que importa es el ultimo. Un indice de idempotencia total dejaria una
// conversacion quemada para siempre ante un fallo transitorio de Chatwoot; uno
// parcial mal escrito permitiria mandar dos veces la misma plantilla al mismo
// lead. Leyendo el SQL las dos cosas se ven igual.
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join, resolve } from 'node:path';
import { PGlite } from '@electric-sql/pglite';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const stack = [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(join(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => join(root, 'supabase/migrations', name)),
];

const db = new PGlite();
await db.waitReady;
await db.exec('create role anon noinherit; create role authenticated noinherit; create role service_role noinherit bypassrls;');
for (const path of stack) {
  let sql = readFileSync(path, 'utf8');
  sql = sql.replace(/create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite');
  await db.exec(sql);
}

await db.exec(`
  insert into public.inbound_commercial_scope_versions (
    scope_key, version, status, tenant_key, chatwoot_account_id,
    chatwoot_inbox_id, external_product_id, offer_code,
    approved_by, approved_at, published_at
  ) values (
    'reactivation-probe', 1, 'published', 'tenant-probe', 7, 11,
    'product-probe', 'offer-probe', 'schema-probe', now(), now()
  );
`);

const CONV = 9401;
const args = ['reactivation-probe', 1, CONV, '5511999999902'];

const created = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)', args,
)).rows[0];
if (created?.outcome !== 'created') {
  throw new Error('fixture admission did not create the durable case');
}

const claimArgs = (commandKey, max = 1) => ([
  CONV, commandKey, 'outside_service_window',
  'johanna_reactivacion_01', 'es_EC', 2079, 101000, 100362, max,
]);
const CLAIM = `
  select * from public.claim_conversation_reactivation(
    $1::bigint, $2, $3, $4, $5, $6::bigint, $7::integer, $8::integer,
    $9::integer, now()
  )
`;

// --- la reserva -----------------------------------------------------------

const claimed = (await db.query(CLAIM, claimArgs('reactivate:9401:2079'))).rows[0];
if (claimed?.outcome !== 'claimed'
    || claimed.reactivated_commercial_case_id !== created.commercial_case_id
    || !claimed.reactivation_event_id) {
  throw new Error('reactivation claim did not reserve the send');
}

const reserved = (await db.query(`
  select status, reason_code, template_name, template_language,
         last_inbound_message_id, inbound_age_seconds, quiet_seconds,
         previous_conversation_status, previous_human_takeover, settled_at
  from public.conversation_reactivation_events
  where command_key = 'reactivate:9401:2079'
`)).rows[0];
if (reserved?.status !== 'claimed'
    || reserved.settled_at !== null
    || reserved.reason_code !== 'outside_service_window'
    || reserved.template_name !== 'johanna_reactivacion_01'
    || reserved.template_language !== 'es_EC'
    || Number(reserved.last_inbound_message_id) !== 2079
    || reserved.inbound_age_seconds !== 101000
    || reserved.quiet_seconds !== 100362
    || reserved.previous_conversation_status !== 'active'
    || reserved.previous_human_takeover !== false) {
  throw new Error('reactivation claim did not audit the send it reserved');
}

// Reservar no despausa nada: la pausa la levanta resume_paused_conversation
// cuando el lead contesta.
const untouched = (await db.query(
  'select status, automation_status, human_takeover, version from public.conversations where id = $1::uuid',
  [claimed.reactivated_conversation_id],
)).rows[0];
if (untouched?.human_takeover !== false || untouched.automation_status !== 'draft_only') {
  throw new Error('reactivation claim mutated the conversation state');
}

// Idempotencia: el mismo command_key no reserva dos veces.
const replayed = (await db.query(CLAIM, claimArgs('reactivate:9401:2079'))).rows[0];
if (replayed?.outcome !== 'replayed'
    || replayed.reactivation_event_id !== claimed.reactivation_event_id) {
  throw new Error('reactivation claim was not idempotent by command key');
}

// --- el cierre ------------------------------------------------------------

const settled = (await db.query(`
  select * from public.settle_conversation_reactivation(
    'reactivate:9401:2079', 'sent', 3100::bigint, null, now()
  )
`)).rows[0];
if (settled?.outcome !== 'settled'
    || settled.reactivation_event_id !== claimed.reactivation_event_id) {
  throw new Error('settlement did not close the live reservation');
}
const delivered = (await db.query(`
  select status, provider_message_id, failure_reason, settled_at
  from public.conversation_reactivation_events
  where command_key = 'reactivate:9401:2079'
`)).rows[0];
if (delivered?.status !== 'sent'
    || Number(delivered.provider_message_id) !== 3100
    || delivered.failure_reason !== null
    || delivered.settled_at === null) {
  throw new Error('settlement did not record the provider message');
}

// Cerrar dos veces no reescribe una fila terminal.
const resettled = (await db.query(`
  select * from public.settle_conversation_reactivation(
    'reactivate:9401:2079', 'failed', null, 'tarde', now()
  )
`)).rows[0];
if (resettled?.outcome !== 'not_found') {
  throw new Error('settlement rewrote a terminal row');
}
const stillDelivered = (await db.query(
  "select status from public.conversation_reactivation_events where command_key = 'reactivate:9401:2079'",
)).rows[0];
if (stillDelivered?.status !== 'sent') {
  throw new Error('a second settlement mutated a delivered row');
}

// --- el limite ------------------------------------------------------------

// Otro inbound del mismo lead da otro command_key, pero el limite sigue.
const limited = (await db.query(CLAIM, claimArgs('reactivate:9401:2999', 1))).rows[0];
if (limited?.outcome !== 'blocked_reactivation_limit'
    || limited.reactivation_event_id !== null) {
  throw new Error('reactivation ignored its own loop guard');
}

// Con el limite en dos, el segundo envio si entra.
const second = (await db.query(CLAIM, claimArgs('reactivate:9401:2999', 2))).rows[0];
if (second?.outcome !== 'claimed') {
  throw new Error('a higher limit did not allow the second reactivation');
}

// --- el reintento de un fallo --------------------------------------------

const failed = (await db.query(`
  select * from public.settle_conversation_reactivation(
    'reactivate:9401:2999', 'failed', null, 'ChatwootProtocolError', now()
  )
`)).rows[0];
if (failed?.outcome !== 'settled') {
  throw new Error('a failed send could not be settled');
}

// El mismo command_key vuelve a reservarse: una fila 'failed' no bloquea, y
// tampoco cuenta contra el limite (por eso este claim entra con max = 2).
const retried = (await db.query(CLAIM, claimArgs('reactivate:9401:2999', 2))).rows[0];
if (retried?.outcome !== 'claimed'
    || retried.reactivation_event_id === second.reactivation_event_id) {
  throw new Error('a failed send could not be retried');
}

// Y una reserva viva sigue bloqueando el reintento: dos barridos simultaneos no
// le mandan la plantilla dos veces al mismo lead.
const blockedWhileLive = (await db.query(CLAIM, claimArgs('reactivate:9401:2999', 9))).rows[0];
if (blockedWhileLive?.outcome !== 'replayed') {
  throw new Error('a live reservation did not block a concurrent send');
}

// --- las barreras ---------------------------------------------------------

const permisoPrevio = (await db.query(
  'select contact_permission from public.contacts where id = $1::uuid',
  [created.contact_id],
)).rows[0]?.contact_permission;
await db.query(
  "update public.contacts set contact_permission = 'opted_out' where id = $1::uuid",
  [created.contact_id],
);
const optedOut = (await db.query(CLAIM, claimArgs('reactivate:9401:3999', 9))).rows[0];
if (optedOut?.outcome !== 'blocked_contact') {
  throw new Error('reactivation ignored the opt-out barrier');
}
const noRow = (await db.query(
  "select count(*)::int as n from public.conversation_reactivation_events where command_key = 'reactivate:9401:3999'",
)).rows[0];
if (noRow?.n !== 0) {
  throw new Error('a blocked reactivation still wrote an audit row');
}
await db.query(
  'update public.contacts set contact_permission = $2 where id = $1::uuid',
  [created.contact_id, permisoPrevio],
);

// Una conversacion que Chatwoot conoce y Supabase no, no manda nada.
const missing = (await db.query(`
  select * from public.claim_conversation_reactivation(
    999999::bigint, 'reactivate:999999:1', 'outside_service_window',
    'johanna_reactivacion_01', 'es_EC', 1::bigint, 90000::integer, null,
    1::integer, now()
  )
`)).rows[0];
if (missing?.outcome !== 'not_found' || missing.reactivation_event_id !== null) {
  throw new Error('an unknown conversation did not fail closed');
}

// Entrada invalida: un reason_code que nadie declaro no se guarda.
let rejected = false;
try {
  await db.query(`
    select * from public.claim_conversation_reactivation(
      $1::bigint, 'reactivate:9401:4999', 'porque si',
      'johanna_reactivacion_01', 'es_EC', 1::bigint, 90000::integer, null,
      1::integer, now()
    )
  `, [CONV]);
} catch (error) {
  rejected = String(error.message ?? error).includes('claim_conversation_reactivation_invalid_input');
}
if (!rejected) {
  throw new Error('an undeclared reason code was accepted');
}

// Un cierre que dice 'sent' y a la vez trae motivo de falla es incoherente.
let incoherent = false;
try {
  await db.query(`
    select * from public.settle_conversation_reactivation(
      'reactivate:9401:2999', 'failed', 7::bigint, null, now()
    )
  `);
} catch (error) {
  incoherent = String(error.message ?? error).includes('settle_conversation_reactivation_invalid_input');
}
if (!incoherent) {
  throw new Error('an incoherent settlement was accepted');
}

// --- los permisos ---------------------------------------------------------

for (const role of ['anon', 'authenticated']) {
  for (const signature of [
    'public.claim_conversation_reactivation(bigint,text,text,text,text,bigint,integer,integer,integer,timestamptz)',
    'public.settle_conversation_reactivation(text,text,bigint,text,timestamptz)',
  ]) {
    const granted = (await db.query(
      'select has_function_privilege($1, $2, $3) as allowed',
      [role, signature, 'EXECUTE'],
    )).rows[0];
    if (granted?.allowed !== false) {
      throw new Error(`${role} can execute ${signature}`);
    }
  }
}
for (const signature of [
  'public.claim_conversation_reactivation(bigint,text,text,text,text,bigint,integer,integer,integer,timestamptz)',
  'public.settle_conversation_reactivation(text,text,bigint,text,timestamptz)',
]) {
  const granted = (await db.query(
    'select has_function_privilege($1, $2, $3) as allowed',
    ['service_role', signature, 'EXECUTE'],
  )).rows[0];
  if (granted?.allowed !== true) {
    throw new Error(`service_role cannot execute ${signature}`);
  }
}

console.log('CONVERSATION_REACTIVATION_SQL_OK');
await db.close();
