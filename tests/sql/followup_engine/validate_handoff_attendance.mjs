// Valida que una derivacion sin atender frene la automatizacion, contra el
// esquema real: derivar -> proyectar la nota -> comprobar que la reactivacion
// NO reserva -> marcar la atencion de una persona -> comprobar que recien ahi
// reserva -> derivar de nuevo y comprobar que la atencion vieja no cuenta.
//
// El caso que reproduce es el medido el 2026-09-27 en produccion: de los diez
// envios de reactivacion, nueve salieron sobre conversaciones con una
// derivacion en 'projected' que ninguna persona habia contestado. Leyendo el
// SQL viejo eso se veia sano, porque 'projected' significa que la nota llego a
// Chatwoot; lo que faltaba era distinguirlo de que alguien la hubiera leido.
//
// El paso que importa es el ultimo. Marcar la atencion por caso, sin comparar
// contra la fecha de cada derivacion, dejaria una conversacion abierta para
// siempre: la primera respuesta del equipo marcaria como atendidas tambien las
// derivaciones que todavia no existen.
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
    'attendance-probe', 1, 'published', 'tenant-probe', 7, 11,
    'product-probe', 'offer-probe', 'schema-probe', now(), now()
  );
  insert into public.human_handoff_projection_policies (
    policy_key, policy_version, scope_key, scope_version,
    inbound_scope_key, inbound_scope_version, expected_team_id,
    note_template_key, note_template_version, private_note_body, active
  ) values (
    'attendance-probe-handoff', 1, null, null,
    'attendance-probe', 1, 17,
    'handoff-note', 1, 'Human review required.', true
  );
`);

const CONV = 9501;
const args = ['attendance-probe', 1, CONV, '5511999999903'];

const created = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)', args,
)).rows[0];
if (created?.outcome !== 'created') {
  throw new Error('fixture admission did not create the durable case');
}

// La conversacion 186: el lead pidio hablar con alguien y el bridge derivo.
const handoff = (await db.query(`
  select * from public.request_inbound_human_handoff(
    $1::uuid, 'handoff:attendance-probe', 'explicit_human_request',
    'attendance-probe-handoff', 1, now()
  )
`, [created.commercial_case_id])).rows[0];
if (handoff?.outcome !== 'requested') {
  throw new Error('durable handoff did not pause the fixture');
}

// El proyector publica la nota privada. En produccion las 33 filas medidas el
// 2026-09-27 estaban todas aca, y ninguna tenia forma de decir si alguien la
// habia contestado.
await db.query(`
  update public.human_handoff_requests
  set status = 'projected', projected_at = now(), updated_at = now()
  where commercial_case_id = $1::uuid
`, [created.commercial_case_id]);

const claimArgs = (commandKey, max = 1) => ([
  CONV, commandKey, 'outside_service_window',
  'johanna_reactivacion_01', 'es_EC', 3079, 101000, null, max,
]);
const CLAIM = `
  select * from public.claim_conversation_reactivation(
    $1::bigint, $2, $3, $4, $5, $6::bigint, $7::integer, $8::integer,
    $9::integer, now()
  )
`;

// --- el bug medido --------------------------------------------------------

const blocked = (await db.query(
  CLAIM, claimArgs('reactivation:attendance-probe:1'),
)).rows[0];
if (blocked?.outcome !== 'blocked_pending_handoff') {
  throw new Error(
    'la reactivacion reservo un envio sobre una derivacion que nadie atendio',
  );
}

const nothingClaimed = (await db.query(`
  select count(*)::int as total from public.conversation_reactivation_events
`)).rows[0];
if (nothingClaimed?.total !== 0) {
  throw new Error('un envio bloqueado no puede dejar fila de reserva');
}

// La respuesta del lead tampoco levanta la pausa mientras nadie lo atienda.
const resumeBlocked = (await db.query(`
  select * from public.resume_paused_conversation(
    $1::bigint, 'resume:attendance-probe:1', 'inbound_after_quiet_period',
    28800, 3, now()
  )
`, [CONV])).rows[0];
if (resumeBlocked?.outcome !== 'blocked_pending_handoff') {
  throw new Error('la reanudacion devolvio al agente una derivacion sin atender');
}

// --- una persona contesta -------------------------------------------------

const attended = (await db.query(
  'select * from public.mark_human_handoff_attended($1::bigint, now(), now())',
  [CONV],
)).rows[0];
if (attended?.outcome !== 'attended'
    || attended.attended_count !== 1
    || attended.attended_commercial_case_id !== created.commercial_case_id) {
  throw new Error('la atencion del equipo no quedo registrada');
}

// Idempotente: el equipo escribe cinco mensajes seguidos y la derivacion se
// atiende una sola vez.
const again = (await db.query(
  'select * from public.mark_human_handoff_attended($1::bigint, now(), now())',
  [CONV],
)).rows[0];
if (again?.outcome !== 'noop' || again.attended_count !== 0) {
  throw new Error('marcar dos veces la atencion tiene que ser inocuo');
}

const claimed = (await db.query(
  CLAIM, claimArgs('reactivation:attendance-probe:2'),
)).rows[0];
if (claimed?.outcome !== 'claimed' || !claimed.reactivation_event_id) {
  throw new Error(
    'con la derivacion atendida la reactivacion tiene que poder reservar',
  );
}

// --- el paso que importa --------------------------------------------------
//
// El lead vuelve a escribir y el agente lo deriva de nuevo. La atencion de la
// derivacion anterior no puede tapar a esta: es posterior.

// Atendida y reactivada, la conversacion puede volver al agente. Es el unico
// camino que la despausa sin intervencion manual, y deja el caso en
// 'active'/'draft_only', que es lo que exige una derivacion nueva.
const resumed = (await db.query(`
  select * from public.resume_paused_conversation(
    $1::bigint, 'resume:attendance-probe:2', 'inbound_after_quiet_period',
    28800, 3, now()
  )
`, [CONV])).rows[0];
if (resumed?.outcome !== 'resumed') {
  throw new Error('una derivacion atendida tiene que poder reanudarse');
}

const second = (await db.query(`
  select * from public.request_inbound_human_handoff(
    $1::uuid, 'handoff:attendance-probe:2', 'explicit_human_request',
    'attendance-probe-handoff', 1, now()
  )
`, [created.commercial_case_id])).rows[0];
if (second?.outcome !== 'requested') {
  throw new Error('la segunda derivacion no se registro');
}

const stillUnattended = (await db.query(`
  select count(*)::int as total
  from public.human_handoff_requests
  where commercial_case_id = $1::uuid and attended_at is null
`, [created.commercial_case_id])).rows[0];
if (stillUnattended?.total !== 1) {
  throw new Error('la derivacion nueva quedo marcada como ya atendida');
}

const blockedAgain = (await db.query(
  CLAIM, claimArgs('reactivation:attendance-probe:3', 5),
)).rows[0];
if (blockedAgain?.outcome !== 'blocked_pending_handoff') {
  throw new Error('la derivacion nueva tiene que volver a frenar la reactivacion');
}

// Una atencion anterior a la derivacion no la cierra: el equipo escribio antes
// de que el agente derivara, asi que no contesto esto.
const stale = (await db.query(`
  select * from public.mark_human_handoff_attended(
    $1::bigint, now() - interval '1 hour', now()
  )
`, [CONV])).rows[0];
if (stale?.outcome !== 'noop' || stale.attended_count !== 0) {
  throw new Error('un mensaje anterior a la derivacion no la atiende');
}

// La cola de pendientes, que es la metrica que antes no existia.
const queue = (await db.query(`
  select count(*)::int as total
  from public.human_handoff_requests
  where attended_at is null
    and status in ('requested', 'projected', 'projection_failed')
`)).rows[0];
if (queue?.total !== 1) {
  throw new Error('la cola de derivaciones sin atender no cuadra');
}

console.log('handoff attendance gates automation: OK');
await db.close();
