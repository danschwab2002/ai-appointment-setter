// Parte 3 de E-539 (migracion 20261128300000): resolver una conversacion en
// Chatwoot cierra la del setter (close_resolved_conversation_v1, la que llama
// el conversation_updated con CHATWOOT_RESOLVED_CLOSE_ENABLED).
//
// Rojo sin la migracion: la funcion no existe y la conversacion derivada sigue
// 'paused_human' para siempre (25 de 50 el 2026-11-13).
//
// Verde con la migracion:
//   A. una derivacion 'projected' sin atender: el cierre LIBERA al contacto
//      (Dan, 28/11): 'completed' con closed_at, sin human_takeover, draft_only,
//      la derivacion atendida y el evento; el caso no cambia. La parada del
//      primer contacto y el freno de la palabra clave, llamados de verdad,
//      frenan antes y dejan pasar despues (con 'closed' el primer contacto
//      seguiria frenado). La misma clave da 'replayed', otra 'already_closed'.
//      En la misma conversacion la admision da 'evidence_conflict' (P-1: por
//      eso el bridge solo cierra en bandejas sin lock).
//   B. el proyector trabajando ('requested', 'projection_failed') da
//      'blocked_pending_projection' sin escribir; 'dead_letter' y una ya
//      atendida cierran.
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
// A. La resuelta con la derivacion sin atender se cierra y LIBERA al contacto.
// ---------------------------------------------------------------------------
// Las lecturas por contacto, llamadas de verdad: la parada del primer contacto
// (_portable_precheckout_stop_reason, con un intent vivo y su binding) y el
// freno del disparador nativo por palabra clave (el de la audiencia es el
// mismo criterio, sin 'closed').
const readers = async (db, contactId, intentId) => ({
  primer_contacto: (await db.query(
    'select public._portable_precheckout_stop_reason($1::uuid, $2::uuid) as r', [intentId, contactId],
  )).rows[0].r,
  palabra_clave: (await db.query(
    `select public._flow_engine_native_keyword_reason('{}'::jsonb, 'tenant-probe', 'product-probe', $1::uuid, '5210000000260') as r`,
    [contactId],
  )).rows[0].r,
});
const seedIntent = async (db) => {
  await db.query(`
    insert into public.commercial_ally_runtime_bindings (
      tenant_ref, funnel_ref, binding_version, ally_ref, lead_ally_name, lead_site,
      lead_landing_id, lead_page_host, lead_page_path, product_hotlink, product_name,
      product_price, currency, offer_code, consent_copy_version, hotmart_product_id,
      chatwoot_account_id, chatwoot_inbox_id, inbound_scope_key, inbound_scope_version, status
    ) values (
      'tenant-probe', 'funnel-probe', 1, 'ally-probe', 'Aliada', 'site-probe',
      'landing-probe', 'example.test', '/probe', 'https://pay.example.test/x', 'Producto',
      10, 'USD', 'offerprobe1', 'v1', 1234, 3, 13, 'close-probe', 1, 'active'
    )`);
  return (await db.query(`
    insert into public.purchase_intents (
      tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref, submitted_at,
      whatsapp_contact_authorized, provisional, provider_observed, activation_authorized,
      lifecycle_state, normalized_phone
    ) values (
      'tenant-probe', 'funnel-probe', 'landing-probe', 'product-probe', 'offerprobe1', now(),
      true, false, true, true, 'waiting_for_purchase', '5210000000260'
    ) returning id`)).rows[0].id;
};
{
  const { caseId, handoffId } = await derive(after, 260, 'projected', 'handoff:close-probe:260');
  const intentId = await seedIntent(after);
  const [convBefore, caseBefore] = [await conversation(after, 260), await commercialCase(after, caseId)];
  const contactId = (await after.query(
    'select contact_id from public.conversations where id = $1::uuid', [convBefore.id],
  )).rows[0].contact_id;
  const held = await readers(after, contactId, intentId);
  if (held.primer_contacto !== 'precheckout_conversation_handoff' || held.palabra_clave !== 'conversacion_derivada') {
    fail('rojo: la derivada tenia que frenar el primer contacto y la palabra clave', held);
  }

  const closed = await close(after, 260, closeKey(260, 'a'));
  if (closed?.outcome !== 'closed' || !closed.close_event_id
      || closed.closed_conversation_id !== convBefore.id) {
    fail('la resuelta derivada tenia que cerrarse', closed);
  }
  const convAfter = await conversation(after, 260);
  if (convAfter.status !== 'completed' || convAfter.closed_at === null
      || convAfter.human_takeover !== false || convAfter.automation_status !== 'draft_only'
      || Number(convAfter.version) !== Number(convBefore.version) + 1) {
    fail('liberar: completed, sin human_takeover, draft_only, closed_at y version + 1', convAfter);
  }
  if ((await handoffRow(after, handoffId)).attended_at === null) {
    fail('liberar: la derivacion projected sin atender queda atendida');
  }
  const caseAfter = await commercialCase(after, caseId);
  if (JSON.stringify(caseAfter) !== JSON.stringify(caseBefore)) {
    fail('el cierre no toca el caso', { caseBefore, caseAfter });
  }
  const freed = await readers(after, contactId, intentId);
  if (freed.primer_contacto !== null || freed.palabra_clave !== null) {
    fail('liberado, el contacto vuelve al primer contacto y a los disparadores nativos', freed);
  }
  // Por que 'completed' y no 'closed': con 'closed' el primer contacto sigue
  // frenado (la parada vigente cuenta 'closed' en cualquier conversacion del
  // contacto). Se prueba y se deshace.
  await after.exec('begin');
  await after.query(`update public.conversations set status = 'closed' where id = $1::uuid`, [convBefore.id]);
  const withClosed = await readers(after, contactId, intentId);
  await after.exec('rollback');
  if (withClosed.primer_contacto !== 'precheckout_conversation_handoff') {
    fail('con closed el primer contacto tenia que seguir frenado', withClosed);
  }
  const [event] = await closeEvents(after, convBefore.id);
  if (event?.id !== closed.close_event_id || event.actor_type !== 'human_agent'
      || event.data.command_key !== closeKey(260, 'a')
      || event.data.external_conversation_id !== 260
      || event.data.previous_status !== 'paused_human'
      || event.data.previous_automation_status !== convBefore.automation_status
      || event.data.previous_human_takeover !== true
      || event.data.handoffs_marked_attended !== 1) {
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

  // P-1 (Dan, 28/11: c): la admision base no acepta una 'completed' en la
  // MISMA conversacion de Chatwoot y da 'evidence_conflict'; por eso el bridge
  // solo cierra en bandejas sin lock_to_single_conversation. Reabrir a mano
  // queda como deuda (b). Se fija lo medido.
  const sameConversation = await admit(after, 260);
  if (sameConversation?.outcome !== 'evidence_conflict') {
    fail('medido: la cerrada da evidence_conflict en la misma conversacion (P-1)', sameConversation);
  }
  // La decision vista no la levanta (D-P3.5). La reanudacion vigente por
  // mensaje entrante (que no mira el estado) SI revive una 'completed' a
  // 'active': es parte de la deuda (b), reabrir a mano; se fija lo medido.
  const seen = await byOperator(after, 260, 'resume:260:label:' + 'c'.repeat(32));
  if (seen?.outcome !== 'not_paused') fail('la decision vista sobre una cerrada tenia que dar not_paused', seen);
  const revived = await vigente(after, 260, 'resume:260:9002', 'inbound_after_quiet_period', 30000);
  if (revived?.outcome !== 'resumed' || (await conversation(after, 260)).status !== 'active') {
    fail('medido: la vigente revive la cerrada a active (deuda b)', revived);
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
// rindio) o ya atendida cierra, y ninguna de las dos se vuelve a marcar.
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
  if (result?.outcome !== 'closed' || row.status !== 'completed' || row.human_takeover !== false
      || event?.data.handoffs_marked_attended !== unattended) {
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
