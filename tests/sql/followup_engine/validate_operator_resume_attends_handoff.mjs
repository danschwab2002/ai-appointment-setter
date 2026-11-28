// E-577, parte 1 de E-539 (migracion 20261128200000): sacar la etiqueta de una
// derivacion que nadie contesto devuelve la conversacion al agente, solo por la
// via de la decision VISTA (resume_paused_conversation_by_operator_v1, la que
// llama el conversation_updated de E-571). La vigente resume_paused_conversation
// no cambia: el camino del mensaje entrante deduce la decision por la ausencia
// de la etiqueta y ahi una derivacion sin atender sigue bloqueando.
//
// Arma dos bases con la cadena completa: sin la migracion (rojo: la unica via
// es la vigente y da blocked_pending_handoff) y con ella (verde). Compara las
// funciones de las dos: la migracion agrega exactamente una y no cambia
// ninguna definicion ni ACL. Despues, en la base con la migracion:
//   A. la decision vista reanuda, marca attended_at, deja la fila
//      operator_request sin silencio medido y la admision vuelve a pasar;
//      la misma clave da replayed; el eco del propio bridge da not_paused;
//      una derivacion nueva con el tope alcanzado da blocked_resume_limit y
//      la marca se deshace;
//   B. la vigente con operator_request (la ausencia de la etiqueta) sigue en
//      blocked_pending_handoff sin marcar;
//   C. 'requested' y 'projection_failed' bloquean sin marcar;
//   D. sin derivacion: not_paused sin escribir; inexistente: not_found; clave
//      invalida: 22023; la segunda corrida de la migracion: 55000.
//
// Datos: sinteticos (cuenta 3 y bandeja 13 como Maribel; telefonos y textos de
// prueba). La nota de la derivacion pasa a 'projected' con un update, que es
// lo que deja el proyector (igual que validate_handoff_attendance.mjs).
//
// La entrega real de Maribel (conversacion 260, 2026-11-12, 7afec902-...) no
// esta en el repo: es una deuda anotada en el contrato, y la forma del evento
// la prueban los tests de Python con el precedente capturado de la 173.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join, resolve } from 'node:path';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION = '20261128200000_operator_resume_attends_handoff_v1.sql';
const NUEVA = 'resume_paused_conversation_by_operator_v1(bigint,text,integer,timestamptz)';
const names = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const fail = (error, detail) => {
  console.error(JSON.stringify({ ok: false, error, detail }, null, 2));
  process.exit(1);
};
if (!names.includes(MIGRATION)) fail(`falta ${MIGRATION}`);
// Despues va solo el cierre de la conversacion que Chatwoot resolvio (20261128300000, parte 3 de E-539).
if (JSON.stringify(names.slice(names.indexOf(MIGRATION) + 1))
    !== JSON.stringify(['20261128300000_close_resolved_conversation_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de la parte 3 de E-539`, names.slice(names.indexOf(MIGRATION) + 1));
}

const migrationSql = (name) => readFileSync(join(root, 'supabase/migrations', name), 'utf8')
  .replace(/create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite');

// Los roles de la API con el execute por defecto de Supabase, como
// validate_acl_hardening.mjs: asi el revoke de la nueva se prueba de verdad.
const open = async (files) => {
  const db = new PGlite();
  await db.waitReady;
  await db.exec(`
    create role anon nologin;
    create role authenticated nologin;
    create role service_role nologin;
    grant usage on schema public to service_role;
    alter default privileges grant execute on functions to anon, authenticated;
  `);
  await db.exec(readFileSync(join(root, 'supabase/baseline/20260803_public_schema.sql'), 'utf8')
    .replace(/create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
  for (const name of files) await db.exec(migrationSql(name));
  await db.exec(`
    insert into public.inbound_commercial_scope_versions (
      scope_key, version, status, tenant_key, chatwoot_account_id,
      chatwoot_inbox_id, external_product_id, offer_code,
      approved_by, approved_at, published_at
    ) values (
      'operator-probe', 1, 'published', 'tenant-probe', 3, 13,
      'product-probe', 'offer-probe', 'schema-probe', now(), now()
    );
    insert into public.human_handoff_projection_policies (
      policy_key, policy_version, scope_key, scope_version,
      inbound_scope_key, inbound_scope_version, expected_team_id,
      note_template_key, note_template_version, private_note_body, active
    ) values (
      'operator-probe-handoff', 1, null, null,
      'operator-probe', 1, 17,
      'handoff-note', 1, 'Human review required.', true
    );
  `);
  return db;
};

const functions = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature,
         pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));

const admit = async (db, conv) => (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)',
  ['operator-probe', 1, conv, `5210000000${conv}`],
)).rows[0];

// Admitida (si hace falta) y derivada; con status distinto de 'requested', el
// estado que deja el proyector de notas.
const derive = async (db, conv, status, key) => {
  let caseId = (await db.query(`
    select commercial_case.id
    from public.commercial_cases commercial_case
    join public.conversations conversation on conversation.id = commercial_case.conversation_id
    where conversation.commercial_context = jsonb_build_object('chatwoot_conversation_id', $1::text)
  `, [String(conv)])).rows[0]?.id;
  if (!caseId) {
    const created = await admit(db, conv);
    if (created?.outcome !== 'created') fail('la admision no creo el caso', { conv, created });
    caseId = created.commercial_case_id;
  }
  const handoff = (await db.query(`
    select * from public.request_inbound_human_handoff(
      $1::uuid, $2, 'commercial_exception', 'operator-probe-handoff', 1, now()
    )
  `, [caseId, key])).rows[0];
  if (handoff?.outcome !== 'requested') fail('la derivacion no se registro', { conv, handoff });
  if (status !== 'requested') {
    await db.query(`
      update public.human_handoff_requests
      set status = $2,
          projected_at = case when $2 = 'projected' then now() end,
          updated_at = now()
      where id = $1::uuid
    `, [handoff.handoff_request_id, status]);
  }
  return { caseId, handoffId: handoff.handoff_request_id };
};

const byOperator = async (db, conv, key, max = 3) => (await db.query(
  'select * from public.resume_paused_conversation_by_operator_v1($1::bigint, $2, $3::integer, now())',
  [conv, key, max],
)).rows[0];
const vigente = async (db, conv, key) => (await db.query(`
  select * from public.resume_paused_conversation(
    $1::bigint, $2, 'operator_request', null, 3, now()
  )
`, [conv, key])).rows[0];
const attendedAt = async (db, id) => (await db.query(
  'select attended_at from public.human_handoff_requests where id = $1::uuid', [id],
)).rows[0]?.attended_at ?? null;
// La clave del camino por etiqueta: resume:<conversacion>:label:<32 hex>.
const labelKey = (conv, hex) => `resume:${conv}:label:${hex.repeat(32).slice(0, 32)}`;

// ---------------------------------------------------------------------------
// Rojo: sin la migracion la unica via es la vigente, y no reanuda.
// ---------------------------------------------------------------------------
const before = await open(names.filter((name) => name !== MIGRATION));
{
  const absent = (await before.query(
    `select to_regprocedure('public.${NUEVA}') is null as absent`,
  )).rows[0].absent;
  if (absent !== true) fail('la funcion nueva existe sin la migracion');
  const { handoffId } = await derive(before, 260, 'projected', 'handoff:operator-probe:260');
  const result = await vigente(before, 260, labelKey(260, 'a'));
  if (result?.outcome !== 'blocked_pending_handoff' || await attendedAt(before, handoffId) !== null) {
    fail('rojo: sin la migracion la etiqueta sacada tenia que seguir en blocked_pending_handoff', result);
  }
}

// ---------------------------------------------------------------------------
// La forma: la migracion agrega exactamente la nueva y no cambia nada mas.
// ---------------------------------------------------------------------------
const after = await open(names);
{
  const [previous, current] = [await functions(before), await functions(after)];
  const added = [...current.keys()].filter((signature) => !previous.has(signature));
  const changed = [...previous.keys()].filter((signature) => !current.has(signature)
    || current.get(signature).definition !== previous.get(signature).definition
    || current.get(signature).acl !== previous.get(signature).acl);
  if (added.length !== 1 || added[0].replace(/^public\./, '').replace(/\s+/g, '')
      !== NUEVA.replace('timestamptz', 'timestampwithtimezone')) {
    fail('la migracion tiene que agregar solo la funcion nueva', added);
  }
  if (changed.length) fail('ninguna funcion vigente cambia', changed);
  const shape = (await after.query(`
    select p.prosecdef,
           array_to_string(p.proconfig, ',') as config,
           has_function_privilege('service_role', p.oid, 'execute') as service_allowed,
           not has_function_privilege('anon', p.oid, 'execute') as anon_denied,
           not has_function_privilege('authenticated', p.oid, 'execute') as authenticated_denied
    from pg_proc p where p.oid = to_regprocedure('public.${NUEVA}')
  `)).rows[0];
  if (!shape?.prosecdef || shape.config !== 'search_path=pg_catalog, public, pg_temp'
      || !shape.service_allowed || !shape.anon_denied || !shape.authenticated_denied) {
    fail('la funcion nueva: security definer, search_path fijo y solo service_role', shape);
  }
}

// ---------------------------------------------------------------------------
// A. La decision vista reanuda y marca la atencion.
// ---------------------------------------------------------------------------
{
  const { handoffId } = await derive(after, 260, 'projected', 'handoff:operator-probe:260');
  const blocked = await admit(after, 260);
  if (blocked?.outcome !== 'blocked') fail('la derivacion tenia que bloquear la admision', blocked);

  const resumed = await byOperator(after, 260, labelKey(260, 'a'));
  if (resumed?.outcome !== 'resumed' || !resumed.resume_event_id || !resumed.resumed_commercial_case_id) {
    fail('sacar la etiqueta de una derivacion sin atender tenia que reanudar', resumed);
  }
  if (await attendedAt(after, handoffId) === null) fail('la derivacion no quedo atendida');
  const audit = (await after.query(`
    select reason_code, quiet_seconds, previous_conversation_status, previous_human_takeover
    from public.conversation_resume_events where id = $1::uuid
  `, [resumed.resume_event_id])).rows[0];
  if (audit?.reason_code !== 'operator_request' || audit.quiet_seconds !== null
      || audit.previous_conversation_status !== 'paused_human' || audit.previous_human_takeover !== true) {
    fail('la fila de la reanudacion no dice que la decidio una persona', audit);
  }
  const readmitted = await admit(after, 260);
  if (readmitted?.outcome === 'blocked' || readmitted?.automation_status !== 'draft_only') {
    fail('la admision sigue bloqueada despues de reanudar', readmitted);
  }

  const replayed = await byOperator(after, 260, labelKey(260, 'a'));
  if (replayed?.outcome !== 'replayed' || replayed.resume_event_id !== resumed.resume_event_id) {
    fail('la misma entrega no puede reanudar dos veces', replayed);
  }
  // El eco de la remocion que hace el propio bridge al reanudar.
  const echo = await byOperator(after, 260, labelKey(260, 'b'));
  if (echo?.outcome !== 'not_paused') fail('el eco sobre una conversacion activa tenia que dar not_paused', echo);

  // Tope anti-loop: se deriva otra vez, el limite no deja reanudar y la marca
  // se deshace.
  const second = await derive(after, 260, 'projected', 'handoff:operator-probe:260:2');
  const limited = await byOperator(after, 260, labelKey(260, 'c'), 1);
  if (limited?.outcome !== 'blocked_resume_limit') fail('el tope tenia que frenar', limited);
  if (await attendedAt(after, second.handoffId) !== null) fail('el tope tenia que deshacer la marca');
  const still = await admit(after, 260);
  if (still?.outcome !== 'blocked') fail('con el tope la conversacion se queda con las personas', still);
}

// ---------------------------------------------------------------------------
// B. La vigente, por ausencia de la etiqueta, sigue bloqueando.
// ---------------------------------------------------------------------------
{
  const { handoffId } = await derive(after, 261, 'projected', 'handoff:operator-probe:261');
  const result = await vigente(after, 261, 'resume:261:9001');
  if (result?.outcome !== 'blocked_pending_handoff' || await attendedAt(after, handoffId) !== null) {
    fail('la vigente no cambia: operator_request por el entrante sigue bloqueando', result);
  }
}

// ---------------------------------------------------------------------------
// C. El proyector trabajando sigue bloqueando, sin marcar.
// ---------------------------------------------------------------------------
for (const [conv, status] of [[262, 'requested'], [263, 'projection_failed']]) {
  const { handoffId } = await derive(after, conv, status, `handoff:operator-probe:${conv}`);
  const result = await byOperator(after, conv, labelKey(conv, 'a'));
  if (result?.outcome !== 'blocked_pending_handoff' || await attendedAt(after, handoffId) !== null) {
    fail(`'${status}' tenia que bloquear sin marcar`, result);
  }
}

// ---------------------------------------------------------------------------
// D. Sin pausa, inexistente, entrada invalida y segunda corrida.
// ---------------------------------------------------------------------------
{
  const created = await admit(after, 264);
  if (created?.outcome !== 'created') fail('la admision de la 264 no creo el caso', created);
  const notPaused = await byOperator(after, 264, labelKey(264, 'a'));
  if (notPaused?.outcome !== 'not_paused') fail('sin derivacion tenia que dar not_paused', notPaused);
  const written = (await after.query(
    'select count(*)::int as total from public.conversation_resume_events where external_conversation_id = 264',
  )).rows[0].total;
  if (written !== 0) fail('not_paused no escribe');

  const missing = await byOperator(after, 999999, labelKey(999999, 'a'));
  if (missing?.outcome !== 'not_found') fail('una conversacion inexistente tenia que dar not_found', missing);

  let invalid = null;
  try { await byOperator(after, 260, 'Clave Invalida'); } catch (error) { invalid = error; }
  if (invalid?.code !== '22023') fail('una clave invalida tenia que dar 22023', invalid?.message);

  let again = null;
  try { await after.exec(migrationSql(MIGRATION)); } catch (error) { again = error; }
  if (again?.code !== '55000' || !String(again.message).includes('operator_resume_attends_handoff_already_applied')) {
    fail('la segunda corrida tenia que abortar con 55000', again?.message);
  }
}

console.log(JSON.stringify({ ok: true, validator: 'operator_resume_attends_handoff' }));
