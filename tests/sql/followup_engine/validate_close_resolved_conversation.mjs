// Parte 3 de E-539 (migracion 20261128300000): resolver una conversacion en
// Chatwoot cierra la del setter (close_resolved_conversation_v1, la que llama
// el conversation_updated con CHATWOOT_RESOLVED_CLOSE_ENABLED).
//
// Rojo sin la migracion: la funcion no existe y la conversacion derivada sigue
// 'paused_human' para siempre (25 de 50 el 2026-11-13).
//
// Verde con la migracion:
//   A. una derivacion 'projected' sin atender: el cierre la pasa a 'closed'
//      con closed_at y el evento, y NO toca human_takeover, automation_status,
//      el caso ni la derivacion (D-P3.2 y D-P3.3); la misma clave da
//      'replayed', otra 'already_closed'. Medido y anotado como pregunta P-1:
//      la admision de una cerrada da 'evidence_conflict' (una 'paused_human'
//      daba 'blocked'). La reanudacion vigente la levanta igual que a una
//      pausada (frena con la derivacion sin atender y, atendida, reanuda), y
//      la decision vista (la etiqueta sacada) da 'not_paused' (D-P3.5).
//   B. el proyector trabajando ('requested', 'projection_failed') da
//      'blocked_pending_projection' sin escribir.
//   C. una conversacion que maneja el agente, una 'paused_human' sin
//      human_takeover (D-P3.4), una 'completed' y una 'blocked' dan
//      'not_paused' sin escribir.
//   D. inexistente, clave invalida y segunda corrida.
// Forma: la migracion agrega solo la funcion nueva, security definer, solo
// service_role, y no cambia ninguna vigente.

import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION = '20261128300000_close_resolved_conversation_v1.sql';
const NUEVA = 'close_resolved_conversation_v1(bigint,text,timestamptz)';
const names = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const fail = (error, detail) => {
  console.error(JSON.stringify({ ok: false, error, detail }, null, 2));
  process.exit(1);
};
if (!names.includes(MIGRATION)) fail(`falta ${MIGRATION}`);
if (names.at(-1) !== MIGRATION) fail(`${MIGRATION} tiene que ser la ultima`, names.slice(-3));

const migrationSql = (name) => readFileSync(join(root, 'supabase/migrations', name), 'utf8')
  .replace(/create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite');

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
      'close-probe', 1, 'published', 'tenant-probe', 3, 13,
      'product-probe', 'offer-probe', 'schema-probe', now(), now()
    );
    insert into public.human_handoff_projection_policies (
      policy_key, policy_version, scope_key, scope_version,
      inbound_scope_key, inbound_scope_version, expected_team_id,
      note_template_key, note_template_version, private_note_body, active
    ) values (
      'close-probe-handoff', 1, null, null,
      'close-probe', 1, 17,
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
  ['close-probe', 1, conv, `5210000000${conv}`],
)).rows[0];

// Admitida y derivada; con status distinto de 'requested', el estado que deja
// el proyector de notas.
const derive = async (db, conv, status, key) => {
  const created = await admit(db, conv);
  if (created?.outcome !== 'created') fail('la admision no creo el caso', { conv, created });
  // La guarda del caso inbound exige updated_at estrictamente mayor en la
  // pausa; el reloj de PGlite puede repetir el instante entre dos sentencias
  // seguidas (el intermitente conocido de validate_inbound_handoff_detail_reason).
  await new Promise((done) => setTimeout(done, 5));
  const handoff = (await db.query(`
    select * from public.request_inbound_human_handoff(
      $1::uuid, $2, 'commercial_exception', 'close-probe-handoff', 1, now()
    )
  `, [created.commercial_case_id, key])).rows[0];
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
  return { caseId: created.commercial_case_id, handoffId: handoff.handoff_request_id };
};

const close = async (db, conv, key) => (await db.query(
  'select * from public.close_resolved_conversation_v1($1::bigint, $2, now())',
  [conv, key],
)).rows[0];
const conversation = async (db, conv) => (await db.query(`
  select id, status, automation_status, human_takeover, closed_at, version
  from public.conversations
  where commercial_context = jsonb_build_object('chatwoot_conversation_id', $1::text)
`, [String(conv)])).rows[0];
const commercialCase = async (db, id) => (await db.query(
  'select status, automation_status, version from public.commercial_cases where id = $1::uuid', [id],
)).rows[0];
const handoffRow = async (db, id) => (await db.query(
  'select status, attended_at, updated_at from public.human_handoff_requests where id = $1::uuid', [id],
)).rows[0];
const closeEvents = async (db, conversationId) => (await db.query(`
  select id, actor_type, data from public.conversation_events
  where conversation_id = $1::uuid and event_type = 'chatwoot_resolved_conversation_closed'
`, [conversationId])).rows;
// La clave del camino de la resolucion: close:<conversacion>:resolved:<32 hex>.
const closeKey = (conv, hex) => `close:${conv}:resolved:${hex.repeat(32).slice(0, 32)}`;
const vigente = async (db, conv, key, reason, quiet) => (await db.query(`
  select * from public.resume_paused_conversation(
    $1::bigint, $2, $3, $4::integer, 3, now()
  )
`, [conv, key, reason, quiet])).rows[0];
const byOperator = async (db, conv, key) => (await db.query(
  'select * from public.resume_paused_conversation_by_operator_v1($1::bigint, $2, 3, now())',
  [conv, key],
)).rows[0];

// ---------------------------------------------------------------------------
// Rojo: sin la migracion no hay cierre y la derivada sigue 'paused_human'.
// ---------------------------------------------------------------------------
const before = await open(names.filter((name) => name !== MIGRATION));
{
  const absent = (await before.query(
    `select to_regprocedure('public.${NUEVA}') is null as absent`,
  )).rows[0].absent;
  if (absent !== true) fail('la funcion nueva existe sin la migracion');
  await derive(before, 260, 'projected', 'handoff:close-probe:260');
  const row = await conversation(before, 260);
  if (row?.status !== 'paused_human' || row.human_takeover !== true) {
    fail('rojo: sin la migracion la derivada tenia que quedar paused_human', row);
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
// A. La resuelta con la derivacion sin atender se cierra, y nada mas cambia.
// ---------------------------------------------------------------------------
{
  const { caseId, handoffId } = await derive(after, 260, 'projected', 'handoff:close-probe:260');
  const [convBefore, caseBefore, handoffBefore] = [
    await conversation(after, 260), await commercialCase(after, caseId), await handoffRow(after, handoffId),
  ];
  const closed = await close(after, 260, closeKey(260, 'a'));
  if (closed?.outcome !== 'closed' || !closed.close_event_id
      || closed.closed_conversation_id !== convBefore.id) {
    fail('la resuelta derivada tenia que cerrarse', closed);
  }
  const convAfter = await conversation(after, 260);
  if (convAfter.status !== 'closed' || convAfter.closed_at === null
      || Number(convAfter.version) !== Number(convBefore.version) + 1) {
    fail('la conversacion tenia que quedar closed, con closed_at y version + 1', convAfter);
  }
  // D-P3.2: human_takeover y automation_status quedan como estaban.
  if (convAfter.human_takeover !== true || convAfter.automation_status !== convBefore.automation_status) {
    fail('el cierre no toca human_takeover ni automation_status', { convBefore, convAfter });
  }
  // D-P3.3: el caso y la derivacion no cambian.
  const [caseAfter, handoffAfter] = [await commercialCase(after, caseId), await handoffRow(after, handoffId)];
  if (JSON.stringify(caseAfter) !== JSON.stringify(caseBefore)) {
    fail('el cierre no toca el caso', { caseBefore, caseAfter });
  }
  if (JSON.stringify(handoffAfter) !== JSON.stringify(handoffBefore)) {
    fail('el cierre no toca la derivacion', { handoffBefore, handoffAfter });
  }
  const [event] = await closeEvents(after, convBefore.id);
  if (event?.id !== closed.close_event_id || event.actor_type !== 'human_agent'
      || event.data.command_key !== closeKey(260, 'a')
      || event.data.external_conversation_id !== 260
      || event.data.previous_status !== 'paused_human'
      || event.data.previous_automation_status !== convBefore.automation_status
      || event.data.human_takeover !== true
      || event.data.unattended_projected_handoffs !== 1) {
    fail('el evento del cierre no dice lo que paso', event);
  }

  const replayed = await close(after, 260, closeKey(260, 'a'));
  if (replayed?.outcome !== 'replayed' || replayed.close_event_id !== closed.close_event_id) {
    fail('la misma entrega no puede cerrar dos veces', replayed);
  }
  const again = await close(after, 260, closeKey(260, 'b'));
  if (again?.outcome !== 'already_closed' || (await closeEvents(after, convBefore.id)).length !== 1) {
    fail('otra entrega sobre una cerrada tenia que dar already_closed sin escribir', again);
  }

  // PREGUNTA P-1 (para Dan, por eso el PR queda en draft): si la MISMA
  // conversacion de Chatwoot vuelve a recibir un mensaje (una bandeja con
  // lock_to_single_conversation = true la reabre, o una persona la reabre a
  // mano), la admision base no acepta una 'closed' y da 'evidence_conflict'
  // con un conflicto 'canonical_drift', donde una 'paused_human' daba
  // 'blocked'. Con 'evidence_conflict' el bridge no pasa
  // durable_pause_recorded y no vuelve a pedir la admision: el lead no
  // recibe respuesta a ese mensaje, y si la etiqueta ya no esta, a ninguno.
  // Este chequeo fija el comportamiento medido para que cambie solo a
  // conciencia.
  const blocked = await admit(after, 260);
  if (blocked?.outcome !== 'evidence_conflict') {
    fail('medido: una cerrada da evidence_conflict en la admision (P-1)', blocked);
  }
  const conflicts = (await after.query(`
    select count(*)::int as total from public.inbound_commercial_case_conflicts
    where reason = 'canonical_drift'
  `)).rows[0].total;
  if (conflicts < 1) fail('medido: la admision de una cerrada deja un conflicto canonical_drift (P-1)');
  // La decision vista no la levanta (D-P3.5) ...
  const seen = await byOperator(after, 260, 'resume:260:label:' + 'c'.repeat(32));
  if (seen?.outcome !== 'not_paused' || (await handoffRow(after, handoffId)).attended_at !== null) {
    fail('la decision vista sobre una cerrada tenia que dar not_paused sin marcar', seen);
  }
  // ... y la reanudacion por mensaje entrante la trata como a una pausada:
  // frena con la derivacion sin atender y, atendida, reanuda.
  const waiting = await vigente(after, 260, 'resume:260:9001', 'inbound_after_quiet_period', 30000);
  if (waiting?.outcome !== 'blocked_pending_handoff') {
    fail('la vigente sobre la cerrada tenia que frenar con la derivacion sin atender', waiting);
  }
  await after.query('select * from public.mark_human_handoff_attended(260::bigint, now(), now())');
  const resumed = await vigente(after, 260, 'resume:260:9002', 'inbound_after_quiet_period', 30000);
  if (resumed?.outcome !== 'resumed') fail('atendida, la vigente tenia que reanudar la cerrada', resumed);
  const readmitted = await admit(after, 260);
  if (readmitted?.outcome === 'blocked' || readmitted?.outcome === 'evidence_conflict') {
    fail('reanudada, la admision tenia que volver', readmitted);
  }
}

// ---------------------------------------------------------------------------
// B. El proyector trabajando: blocked_pending_projection sin escribir.
// ---------------------------------------------------------------------------
for (const [conv, status] of [[262, 'requested'], [263, 'projection_failed']]) {
  await derive(after, conv, status, `handoff:close-probe:${conv}`);
  const before = await conversation(after, conv);
  const result = await close(after, conv, closeKey(conv, 'a'));
  const row = await conversation(after, conv);
  if (result?.outcome !== 'blocked_pending_projection' || row.status !== before.status
      || Number(row.version) !== Number(before.version) || (await closeEvents(after, row.id)).length) {
    fail(`'${status}' tenia que dar blocked_pending_projection sin escribir`, { result, row });
  }
}

// ---------------------------------------------------------------------------
// B2. Lo que no frena: una derivacion en dead_letter (el proyector ya se
// rindio) o ya atendida cierra, y la atendida no cuenta como sin atender.
// ---------------------------------------------------------------------------
for (const [conv, status, unattended] of [[268, 'dead_letter', 0], [269, 'attended', 0]]) {
  const { handoffId } = await derive(after, conv, status === 'attended' ? 'projected' : status,
    `handoff:close-probe:${conv}`);
  if (status === 'attended') {
    await after.query(`select * from public.mark_human_handoff_attended(${conv}::bigint, now(), now())`);
    if ((await handoffRow(after, handoffId)).attended_at === null) fail('la 269 no quedo atendida');
  }
  const result = await close(after, conv, closeKey(conv, 'a'));
  const row = await conversation(after, conv);
  const [event] = await closeEvents(after, row.id);
  if (result?.outcome !== 'closed' || row.status !== 'closed'
      || event?.data.unattended_projected_handoffs !== unattended) {
    fail(`la ${conv} (${status}) tenia que cerrarse`, { result, row, event });
  }
}

// ---------------------------------------------------------------------------
// C. Lo que no tomo el equipo: not_paused sin escribir.
// ---------------------------------------------------------------------------
{
  const created = await admit(after, 264);
  if (created?.outcome !== 'created') fail('la admision de la 264 no creo el caso', created);

  await derive(after, 265, 'projected', 'handoff:close-probe:265');
  // D-P3.4: una 'paused_human' sin human_takeover no se cierra.
  await after.query(`
    update public.conversations set human_takeover = false
    where commercial_context = jsonb_build_object('chatwoot_conversation_id', '265')
  `);
  await derive(after, 266, 'projected', 'handoff:close-probe:266');
  await derive(after, 267, 'projected', 'handoff:close-probe:267');
  for (const [conv, status] of [[266, 'completed'], [267, 'blocked']]) {
    await after.query(`
      update public.conversations set status = $2
      where commercial_context = jsonb_build_object('chatwoot_conversation_id', $1::text)
    `, [String(conv), status]);
  }
  for (const conv of [264, 265, 266, 267]) {
    const before = await conversation(after, conv);
    const result = await close(after, conv, closeKey(conv, 'a'));
    const row = await conversation(after, conv);
    if (result?.outcome !== 'not_paused' || row.status !== before.status
        || Number(row.version) !== Number(before.version) || (await closeEvents(after, row.id)).length) {
      fail(`la ${conv} tenia que dar not_paused sin escribir`, { result, before, row });
    }
  }
}

// ---------------------------------------------------------------------------
// D. Inexistente, entrada invalida y segunda corrida.
// ---------------------------------------------------------------------------
{
  const missing = await close(after, 999999, closeKey(999999, 'a'));
  if (missing?.outcome !== 'not_found') fail('una conversacion inexistente tenia que dar not_found', missing);

  for (const [conv, key] of [[260, 'Clave Invalida'], [0, closeKey(0, 'a')], [260, null]]) {
    let invalid = null;
    try { await close(after, conv, key); } catch (error) { invalid = error; }
    if (invalid?.code !== '22023') fail('una entrada invalida tenia que dar 22023', { conv, key, error: invalid?.message });
  }

  let again = null;
  try { await after.exec(migrationSql(MIGRATION)); } catch (error) { again = error; }
  if (again?.code !== '55000' || !String(again.message).includes('close_resolved_conversation_already_applied')) {
    fail('la segunda corrida tenia que abortar con 55000', again?.message);
  }
}

console.log(JSON.stringify({ ok: true, validator: 'close_resolved_conversation' }));
