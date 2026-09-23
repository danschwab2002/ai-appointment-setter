// Valida la transicion inversa de la pausa contra el esquema real, ejecutando
// las RPC: admitir -> derivar -> comprobar que la admision queda bloqueada ->
// reactivar -> comprobar que la admision vuelve a pasar. El paso que importa es
// el ultimo: sin el, una reactivacion podria escribir los campos y aun asi no
// devolver la conversacion al agente.
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
    'resume-probe', 1, 'published', 'tenant-probe', 7, 11,
    'product-probe', 'offer-probe', 'schema-probe', now(), now()
  );
  insert into public.human_handoff_projection_policies (
    policy_key, policy_version, scope_key, scope_version,
    inbound_scope_key, inbound_scope_version, expected_team_id,
    note_template_key, note_template_version, private_note_body, active
  ) values (
    'resume-probe-handoff', 1, null, null,
    'resume-probe', 1, 17,
    'handoff-note', 1, 'Human review required.', true
  );
`);

const CONV = 9301;
const args = ['resume-probe', 1, CONV, '5511999999901'];

const created = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)', args,
)).rows[0];
if (created?.outcome !== 'created') {
  throw new Error('fixture admission did not create the durable case');
}

const handoff = (await db.query(`
  select * from public.request_inbound_human_handoff(
    $1::uuid, 'handoff:resume-probe', 'policy_requires_human',
    'resume-probe-handoff', 1, now()
  )
`, [created.commercial_case_id])).rows[0];
if (handoff?.outcome !== 'requested') {
  throw new Error('durable handoff did not pause the fixture');
}

const blocked = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)', args,
)).rows[0];
if (blocked?.outcome !== 'blocked') {
  throw new Error('paused conversation was expected to block admission');
}

// --- la vuelta ------------------------------------------------------------

// Mientras la derivacion no se proyecto, la reactivacion no se mete.
const pending = (await db.query(`
  select * from public.resume_paused_conversation(
    $1::bigint, 'resume:resume-probe:0', 'operator_request', null, 3, now()
  )
`, [CONV])).rows[0];
if (pending?.outcome !== 'blocked_pending_handoff') {
  throw new Error('resume ran over a handoff that was still being projected');
}

// El proyector publica la nota y cierra su trabajo (en produccion las seis
// filas medidas el 2026-09-23 estaban en 'projected').
await db.query(`
  update public.human_handoff_requests
  set status = 'projected', projected_at = now(), updated_at = now()
  where commercial_case_id = $1::uuid
`, [created.commercial_case_id]);

const resumed = (await db.query(`
  select * from public.resume_paused_conversation(
    $1::bigint, 'resume:resume-probe:1', 'inbound_after_quiet_period', 28800, 3, now()
  )
`, [CONV])).rows[0];
if (resumed?.outcome !== 'resumed'
    || resumed.resumed_commercial_case_id !== created.commercial_case_id
    || !resumed.resume_event_id) {
  throw new Error('resume did not report a durable transition');
}

const audit = (await db.query(`
  select reason_code, quiet_seconds, previous_conversation_status,
         previous_human_takeover, previous_case_status
  from public.conversation_resume_events where command_key = 'resume:resume-probe:1'
`)).rows[0];
if (audit?.previous_conversation_status !== 'paused_human'
    || audit.previous_human_takeover !== true
    || audit.previous_case_status !== 'paused'
    || audit.quiet_seconds !== 28800
    || audit.reason_code !== 'inbound_after_quiet_period') {
  throw new Error('resume audit row did not capture the previous state');
}

// El paso que prueba el efecto: la admision vuelve a pasar.
const readmitted = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)', args,
)).rows[0];
if (readmitted?.outcome === 'blocked'
    || readmitted?.automation_status !== 'draft_only') {
  throw new Error('resumed conversation still blocks the agent');
}

// Idempotencia: el mismo command_key no reactiva dos veces ni duplica auditoria.
const replayed = (await db.query(`
  select * from public.resume_paused_conversation(
    $1::bigint, 'resume:resume-probe:1', 'inbound_after_quiet_period', 28800, 3, now()
  )
`, [CONV])).rows[0];
if (replayed?.outcome !== 'replayed'
    || replayed.resume_event_id !== resumed.resume_event_id) {
  throw new Error('resume was not idempotent by command key');
}

// Una conversacion ya admisible no se toca.
const already = (await db.query(`
  select * from public.resume_paused_conversation(
    $1::bigint, 'resume:resume-probe:2', 'operator_request', null, 3, now()
  )
`, [CONV])).rows[0];
if (already?.outcome !== 'already_active') {
  throw new Error('resume of an active conversation was not a no-op');
}

// Quien pidio no ser contactado no se reactiva.
await db.query(`
  select * from public.request_inbound_human_handoff(
    $1::uuid, 'handoff:resume-probe:2', 'policy_requires_human',
    'resume-probe-handoff', 1, now()
  )
`, [created.commercial_case_id]);
const permisoPrevio = (await db.query(
  'select contact_permission from public.contacts where id = $1::uuid',
  [created.contact_id],
)).rows[0]?.contact_permission;
await db.query(
  "update public.contacts set contact_permission = 'opted_out' where id = $1::uuid",
  [created.contact_id],
);
const optedOut = (await db.query(`
  select * from public.resume_paused_conversation(
    $1::bigint, 'resume:resume-probe:3', 'operator_request', null, 3, now()
  )
`, [CONV])).rows[0];
if (optedOut?.outcome !== 'blocked_contact') {
  throw new Error('resume ignored the opt-out barrier');
}
const stillPaused = (await db.query(
  'select human_takeover from public.conversations where id = $1::uuid',
  [resumed.resumed_conversation_id],
)).rows[0];
if (stillPaused?.human_takeover !== true) {
  throw new Error('opted-out resume mutated the conversation anyway');
}

// Anti-loop: pasado el limite, la conversacion se queda con las personas.
await db.query(
  'update public.contacts set contact_permission = $2 where id = $1::uuid',
  [created.contact_id, permisoPrevio],
);
const limited = (await db.query(`
  select * from public.resume_paused_conversation(
    $1::bigint, 'resume:resume-probe:limit', 'operator_request', null, 1, now()
  )
`, [CONV])).rows[0];
if (limited?.outcome !== 'blocked_resume_limit') {
  throw new Error('resume ignored its own loop guard');
}

// Una conversacion inexistente no explota ni inventa filas.
const missing = (await db.query(`
  select * from public.resume_paused_conversation(
    999999::bigint, 'resume:resume-probe:4', 'operator_request', null, 3, now()
  )
`)).rows[0];
if (missing?.outcome !== 'not_found') {
  throw new Error('resume of an unknown conversation did not fail closed');
}

// ACL: la RPC la ejecuta el servicio, nunca anon ni authenticated.
const acl = (await db.query(`
  select
    not has_function_privilege('anon', 'public.resume_paused_conversation(bigint,text,text,integer,integer,timestamptz)', 'execute') as anon_denied,
    not has_function_privilege('authenticated', 'public.resume_paused_conversation(bigint,text,text,integer,integer,timestamptz)', 'execute') as authenticated_denied,
    has_function_privilege('service_role', 'public.resume_paused_conversation(bigint,text,text,integer,integer,timestamptz)', 'execute') as service_allowed
`)).rows[0];
if (!acl?.anon_denied || !acl.authenticated_denied || !acl.service_allowed) {
  throw new Error('resume RPC ACL is wrong');
}

console.log('RESUME_PAUSED_CONVERSATION_SQL_OK');
