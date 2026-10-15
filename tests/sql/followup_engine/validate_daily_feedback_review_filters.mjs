// Filtros de la revision diaria y cierre del lote (migracion 20261015000100).
//
// Corre las migraciones del daily feedback, la del contexto (con las tablas
// minimas que lee su RPC, como validate_daily_feedback_review_context.mjs) y la
// de los filtros sobre PGlite. Las etiquetas de cada item son las que arma el
// colector en context.summary.review_facets; las del lote A copian la mezcla
// medida en el lote de ATT1 del 2026-10-13 (opt-outs, derivaciones, respuestas
// del modelo y una conversacion que abrio el lead).
import { PGlite } from '@electric-sql/pglite';
import { existsSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
  alter default privileges in schema public grant all on tables to service_role;
`);
for (const name of [
  '20260910000100_daily_feedback_production_v1.sql',
  '20260911000100_daily_feedback_notification_fencing_v1.sql',
  '20260911000200_daily_feedback_multi_reviewer_ownership_v1.sql',
]) {
  const file = join(root, 'supabase/migrations', name);
  if (existsSync(file)) await db.exec(readFileSync(file, 'utf8'));
}
await db.exec(`
  create table public.human_handoff_requests (
    id uuid primary key default gen_random_uuid(),
    chatwoot_account_id bigint not null, chatwoot_inbox_id bigint not null,
    external_conversation_id bigint not null, primary_reason_code text not null,
    detail_reason_code text, requested_by text not null, status text not null,
    created_at timestamptz not null default clock_timestamp()
  );
  create table public.conversation_reactivation_events (
    id uuid primary key default gen_random_uuid(),
    external_conversation_id bigint not null, status text not null, template_name text not null,
    reason_code text not null, provider_message_id bigint, quiet_seconds integer,
    failure_reason text, created_at timestamptz not null default clock_timestamp()
  );
  create table public.conversation_resume_events (
    id uuid primary key default gen_random_uuid(),
    external_conversation_id bigint not null, reason_code text not null,
    quiet_seconds integer, created_at timestamptz not null default clock_timestamp()
  );
  create table public.contact_opt_out_events (
    id uuid primary key default gen_random_uuid(),
    canonical_account_id bigint not null, canonical_inbox_id bigint not null,
    canonical_conversation_id bigint not null, canonical_message_id bigint not null,
    occurred_at timestamptz not null default clock_timestamp()
  );
  create table public.checkout_offer_catalog (
    id uuid primary key default gen_random_uuid(), offer_code text not null, landing_ref text not null
  );
  create table public.checkout_link_issuances (
    id uuid primary key default gen_random_uuid(),
    chatwoot_account_id bigint not null, chatwoot_inbox_id bigint not null,
    chatwoot_conversation_id bigint not null, created_at timestamptz not null default clock_timestamp(),
    status text not null, source_kind text not null, chatwoot_message_id bigint,
    sck_value text not null, original_sck text, checkout_url_final text not null,
    purchased_at timestamptz, offer_catalog_id uuid references public.checkout_offer_catalog(id)
  );
`);
await db.exec(readFileSync(
  join(root, 'supabase/migrations/20260927000100_daily_feedback_review_context_v2.sql'), 'utf8',
));
const filtersSql = readFileSync(
  join(root, 'supabase/migrations/20261015000100_daily_feedback_review_filters_v1.sql'), 'utf8',
);
await db.exec(filtersSql);

const ACK = 'Apruebo que este reporte se cierre sin completar el feedback de todas las conversaciones.';
const TENANT = 'lancemos';
const SCOPE = 'att1-agent-bot-2';
const reviewers = JSON.stringify([
  { reviewer_ref: 'juan', slack_user_id: 'U12345678', deletion_accountable: true },
  { reviewer_ref: 'mariana', slack_user_id: 'U87654321', deletion_accountable: true },
  { reviewer_ref: 'dan', slack_user_id: 'U11111111', deletion_accountable: true },
  { reviewer_ref: 'marcela', slack_user_id: 'U22222222', deletion_accountable: true },
]);

function expect(condition, message) {
  if (!condition) throw new Error(message);
}
async function expectRejected(sql, params, marker) {
  let observed = '';
  try {
    await db.query(sql, params);
  } catch (error) {
    observed = String(error.message);
  }
  expect(observed.includes(marker), `expected rejection ${marker}, got: ${observed || 'accepted'}`);
}
let commandSeq = 0;
const command = () => {
  commandSeq += 1;
  return `50000000-0000-4000-8000-${String(commandSeq).padStart(12, '0')}`;
};
const fingerprint = (n) => String(n % 10).repeat(64);

await db.exec('set role service_role');
const configured = (await db.query(`
  select public.configure_daily_feedback_scope_v2(
    '10000000-0000-4000-8000-000000000001', '${'1'.repeat(64)}',
    '${TENANT}', '${SCOPE}', 'https://slack.com',
    'T12345678', $1::jsonb, 'joint-reviewer-accountability-v1',
    2, 11, 2, 'America/Mexico_City', '18:00:00', 72,
    'identity-preserving-redaction-v2', 'chatwoot-daily-conversation-context-v2', 'daily-feedback-web-v2', false
  ) as result
`, [reviewers])).rows[0].result;
expect(configured.status === 'configured', `scope: ${JSON.stringify(configured)}`);

async function claimAt(nowSql, seed) {
  const claim = (await db.query(`
    select public.claim_daily_feedback_collection_v1(
      '2${seed}000000-0000-4000-8000-000000000001', '${seed.repeat(64)}',
      'collector-1', '${TENANT}', '${SCOPE}', ${nowSql}, true, 120
    ) as result
  `)).rows[0].result;
  expect(claim.status === 'claimed', `claim ${seed}: ${JSON.stringify(claim)}`);
  return claim;
}
async function commit(claim, items, seed) {
  const committed = (await db.query(`
    select public.commit_daily_feedback_batch_v1(
      '3${seed}000000-0000-4000-8000-000000000001', '${seed.repeat(64)}',
      'collector-1', '${claim.schedule_id}', ${claim.lease_generation},
      '${seed.repeat(64)}', 'release_lineage_unavailable', $1::jsonb
    ) as result
  `, [JSON.stringify(items)])).rows[0].result;
  expect(committed.status === 'committed', `commit ${seed}: ${JSON.stringify(committed)}`);
  await db.query('set role postgres');
  await db.query(`update public.daily_feedback_batches set retention_expires_at=clock_timestamp()+interval '1 hour' where id='${committed.batch_id}'`);
  await db.query('set role service_role');
  return committed;
}
async function login(batch, seed) {
  const stateHash = `${seed}`.repeat(64);
  const sessionHash = `${seed}`.repeat(63) + 'f';
  await db.query(`select public.begin_daily_feedback_oidc_v1('${stateHash}', '${batch.public_ref}',
    '/daily-feedback/review/${batch.public_ref}', clock_timestamp()+interval '5 minutes')`);
  const done = (await db.query(`select public.complete_daily_feedback_oidc_v1(
    '${stateHash}', '${sessionHash}', 'https://slack.com', 'https://slack.com/user_id/U87654321',
    'T12345678', 'U87654321', clock_timestamp()+interval '30 minutes') as result`)).rows[0].result;
  expect(done.status === 'authenticated', `login ${seed}: ${JSON.stringify(done)}`);
  return sessionHash;
}
const pageV1 = async (session, batch) => (await db.query(
  `select public.get_daily_feedback_review_page_v1($1, $2) as result`, [session, batch.public_ref],
)).rows[0].result;
const pageV2 = async (session, batch, facets) => (await db.query(
  `select public.get_daily_feedback_review_page_v2($1, $2, $3::text[]) as result`,
  [session, batch.public_ref, facets],
)).rows[0].result;
const decide = async (session, batch, itemId, decision = 'correct', feedback = null) => (await db.query(
  `select public.record_daily_feedback_decision_v1($1, $2, $3, $4, $5, $6, $7) as result`,
  [command(), fingerprint(commandSeq), session, batch.public_ref, itemId, decision, feedback],
)).rows[0].result;
const close = async (session, batch, ack, commandId = command()) => (await db.query(
  `select public.close_daily_feedback_batch_v1($1, $2, $3, $4, $5) as result`,
  [commandId, fingerprint(commandSeq), session, batch.public_ref, ack],
)).rows[0].result;

// Lote A: cinco conversaciones con etiquetas del colector.
const claimA = await claimAt(`greatest(clock_timestamp(),
  ((((clock_timestamp() at time zone 'America/Mexico_City')::date::timestamp + time '18:00:00') at time zone 'America/Mexico_City') + interval '1 second'))`, '1');
const t = (minutes) => new Date(new Date(claimA.window_start).getTime() + minutes * 60 * 1000).toISOString();
const item = (index, facets, conversationId) => ({
  conversation_ref: `conv_${String(index).padStart(20, '0')}`,
  display_label: `Conversación ${String(index).padStart(2, '0')}`,
  apparent_objective: 'Conversación sin objetivo claro',
  observed_outcome: 'Esperando respuesta del prospecto',
  release_id: 'release_lineage_unavailable', release_version: 0,
  messages: [
    { actor: 'agent', kind: 'first_touch_template', occurred_at: t(index * 10), status: 'read', text: 'Hola, vimos que no terminaste tu compra', meta: {} },
    { actor: 'prospect', kind: 'prospect_message', occurred_at: t(index * 10 + 1), status: 'sent', text: 'Hola', meta: {} },
  ],
  context: {
    chatwoot_conversation_id: conversationId,
    conversation_url: `https://chatwoot.example.test/app/accounts/2/conversations/${conversationId}`,
    conversation: { status: 'open', opened_by: facets.includes('inbound') ? 'prospect' : 'agent' },
    summary: { prospect_messages: 1, review_facets: facets },
  },
});
const batchA = await commit(claimA, [
  item(1, ['opt_out'], 103),
  item(2, ['handoff', 'model_reply'], 120),
  item(3, [], 121),
  item(4, ['model_reply', 'inbound'], 163),
  item(5, ['opt_out'], 104),
], '1');
const sessionA = await login(batchA, 'a');

// Sin etiquetas pedidas: igual que la v1, con los conteos de lo pendiente.
const all = await pageV2(sessionA, batchA, null);
expect(all.status === 'item' && all.item.position === 1, `v2 sin filtro: ${JSON.stringify(all)}`);
expect(all.queue.pending === 5 && all.queue.matching === 5 && all.queue.faceted === true && all.queue.filtering === false,
  `v2 sin filtro, cola: ${JSON.stringify(all.queue)}`);
expect(all.queue.facets.handoff === 1 && all.queue.facets.inbound === 1 && all.queue.facets.opt_out === 2
  && all.queue.facets.model_reply === 2 && Object.keys(all.queue.facets).length === 4,
  `conteos por etiqueta: ${JSON.stringify(all.queue.facets)}`);
expect(all.item.context.chatwoot_conversation_id === 103, 'v2 sin filtro tiene que devolver el contexto del item');
const empty = await pageV2(sessionA, batchA, []);
expect(empty.status === 'item' && empty.item.position === 1 && empty.queue.filtering === false, 'un arreglo vacio no filtra');
const v1 = await pageV1(sessionA, batchA);
expect(v1.status === 'item' && v1.item.position === 1 && v1.queue === undefined, 'la v1 sigue igual');

// Con el filtro por defecto de la pagina: derivacion, respuesta del modelo e inbound.
const facets = ['handoff', 'model_reply', 'inbound'];
const first = await pageV2(sessionA, batchA, facets);
expect(first.status === 'item' && first.item.position === 2, `filtro, primera: ${JSON.stringify(first)}`);
expect(first.queue.pending === 5 && first.queue.matching === 2 && first.queue.filtering === true, `filtro, cola: ${JSON.stringify(first.queue)}`);
expect(first.item.context.chatwoot_conversation_id === 120, 'filtro: el item tiene que traer su contexto');

// La decision acepta la conversacion que se ve aunque no sea la primera por indice.
const recorded = await decide(sessionA, batchA, first.item.item_id, 'correct_with_feedback', 'No mandar el link dos veces');
expect(recorded.status === 'recorded' && recorded.decided_count === 1 && recorded.batch_complete === false, `decision fuera de orden: ${JSON.stringify(recorded)}`);
await expectRejected(
  `select public.record_daily_feedback_decision_v1($1, $2, $3, $4, $5, 'correct', null)`,
  [command(), fingerprint(commandSeq), sessionA, batchA.public_ref, first.item.item_id],
  'review_item_not_current',
);
const second = await pageV2(sessionA, batchA, facets);
expect(second.status === 'item' && second.item.position === 4 && second.queue.matching === 1, `filtro, segunda: ${JSON.stringify(second)}`);
expect((await decide(sessionA, batchA, second.item.item_id)).status === 'recorded', 'segunda decision');

// Ninguna pendiente coincide: filtered_out, sin item y con la cola.
const out = await pageV2(sessionA, batchA, facets);
expect(out.status === 'filtered_out' && out.item === undefined, `filtered_out: ${JSON.stringify(out)}`);
expect(out.queue.pending === 3 && out.queue.matching === 0 && out.decided_count === 2 && out.item_count === 5,
  `filtered_out, cola: ${JSON.stringify(out)}`);
expect(out.queue.facets.opt_out === 2 && out.queue.facets.handoff === undefined, `filtered_out, conteos: ${JSON.stringify(out.queue.facets)}`);
const optOuts = await pageV2(sessionA, batchA, ['opt_out']);
expect(optOuts.status === 'item' && optOuts.item.position === 1, 'el filtro de opt-out muestra la primera con opt-out');

// Etiquetas con forma rara: se rechazan sin leer nada.
await expectRejected(`select public.get_daily_feedback_review_page_v2($1, $2, array['Handoff']::text[])`, [sessionA, batchA.public_ref], 'invalid_review_facets');
await expectRejected(`select public.get_daily_feedback_review_page_v2($1, $2, array[null]::text[])`, [sessionA, batchA.public_ref], 'invalid_review_facets');
await expectRejected(`select public.get_daily_feedback_review_page_v2($1, $2, array['a','b','c','d','e','f','g','h','i']::text[])`, [sessionA, batchA.public_ref], 'invalid_review_facets');
await expectRejected(`select public.get_daily_feedback_review_page_v2($1, $2, null)`, ['0'.repeat(64), batchA.public_ref], 'review_session_invalid');

// El cierre exige la frase exacta.
await expectRejected(
  `select public.close_daily_feedback_batch_v1($1, $2, $3, $4, $5)`,
  [command(), fingerprint(commandSeq), sessionA, batchA.public_ref, 'Apruebo'],
  'review_closure_not_acknowledged',
);
await expectRejected(
  `select public.close_daily_feedback_batch_v1($1, $2, $3, $4, null)`,
  [command(), fingerprint(commandSeq), sessionA, batchA.public_ref],
  'review_closure_not_acknowledged',
);
const closeCommand = command();
const closed = await close(sessionA, batchA, ACK, closeCommand);
expect(closed.status === 'closed' && closed.decided_count === 2 && closed.unreviewed_count === 3 && closed.item_count === 5,
  `cierre: ${JSON.stringify(closed)}`);
const replayed = (await db.query(
  `select public.close_daily_feedback_batch_v1($1, $2, $3, $4, $5) as result`,
  [closeCommand, fingerprint(commandSeq), sessionA, batchA.public_ref, ACK],
)).rows[0].result;
expect(replayed.status === 'replayed' && replayed.unreviewed_count === 3, `el mismo comando se repite sin efecto: ${JSON.stringify(replayed)}`);

// Cerrado: las dos paginas dan complete con lo que dejo el cierre.
const doneV1 = await pageV1(sessionA, batchA);
const doneV2 = await pageV2(sessionA, batchA, facets);
for (const done of [doneV1, doneV2]) {
  expect(done.status === 'complete' && done.decided_count === 2 && done.closed_unreviewed_count === 3 && done.item === undefined,
    `lote cerrado: ${JSON.stringify(done)}`);
}
await expectRejected(
  `select public.close_daily_feedback_batch_v1($1, $2, $3, $4, $5)`,
  [command(), fingerprint(commandSeq), sessionA, batchA.public_ref, ACK],
  'review_batch_not_open',
);
await db.query('set role postgres');
const remainingId = (await db.query(
  `select i.id from public.daily_feedback_items i where i.batch_id=$1
     and not exists(select 1 from public.daily_feedback_decisions d where d.item_id=i.id) order by i.item_index limit 1`,
  [batchA.batch_id],
)).rows[0].id;
const stored = (await db.query(
  `select b.state, b.completed_at is not null as completed, c.unreviewed_count, c.decided_count, c.acknowledgement,
          (select count(*)::int from public.daily_feedback_decisions d where d.batch_id=b.id) as decisions,
          (select reviewer_ref from public.daily_feedback_reviewer_bindings rb where rb.id=c.reviewer_binding_id) as reviewer
     from public.daily_feedback_batches b join public.daily_feedback_batch_closures c on c.batch_id=b.id
    where b.id=$1`, [batchA.batch_id],
)).rows[0];
expect(stored.state === 'completed' && stored.completed && stored.unreviewed_count === 3 && stored.decided_count === 2
  && stored.decisions === 2 && stored.acknowledgement === ACK && stored.reviewer === 'mariana',
  `lo guardado del cierre: ${JSON.stringify(stored)}`);
// El cierre no se edita ni se borra.
await expectRejected(`update public.daily_feedback_batch_closures set unreviewed_count=1 where batch_id=$1`, [batchA.batch_id], 'daily_feedback_content_immutable');
await expectRejected(`delete from public.daily_feedback_batch_closures where batch_id=$1`, [batchA.batch_id], 'daily_feedback_content_immutable');
await db.query('set role service_role');
// Despues del cierre no entra ninguna decision.
await expectRejected(
  `select public.record_daily_feedback_decision_v1($1, $2, $3, $4, $5, 'correct', null)`,
  [command(), fingerprint(commandSeq), sessionA, batchA.public_ref, remainingId],
  'review_item_not_current',
);
// Las conversaciones cerradas sin revisar no aparecen como revision previa.
const priorContext = (await db.query(
  `select public.get_daily_feedback_conversation_context_v1($1, $2, 2, 11, array[120,103]::bigint[]) as result`,
  [TENANT, SCOPE],
)).rows[0].result;
expect(priorContext['120'].prior_reviews.length === 1 && priorContext['103'].prior_reviews.length === 0,
  `revisiones previas despues del cierre: ${JSON.stringify(priorContext)}`);

// Lote B, armado antes de los filtros (sin review_facets): el filtro no se aplica.
const claimB = await claimAt(`'${claimA.window_end}'::timestamptz + interval '1 day'`, '2');
const legacy = (index, conversationId) => {
  const base = item(index, [], conversationId);
  return { ...base, context: { ...base.context, summary: { prospect_messages: 1 } } };
};
const batchB = await commit(claimB, [legacy(1, 201), legacy(2, 202)], '2');
const sessionB = await login(batchB, 'b');
const legacyPage = await pageV2(sessionB, batchB, facets);
expect(legacyPage.status === 'item' && legacyPage.item.position === 1, `lote viejo: ${JSON.stringify(legacyPage)}`);
expect(legacyPage.queue.faceted === false && legacyPage.queue.filtering === false && legacyPage.queue.matching === 2,
  `lote viejo, cola: ${JSON.stringify(legacyPage.queue)}`);
// Un lote cerrado con todas sus conversaciones decididas no admite el cierre.
expect((await decide(sessionB, batchB, legacyPage.item.item_id)).status === 'recorded', 'lote B, primera');
const lastB = await pageV2(sessionB, batchB, null);
const finished = await decide(sessionB, batchB, lastB.item.item_id);
expect(finished.batch_complete === true, `lote B completo: ${JSON.stringify(finished)}`);
await expectRejected(
  `select public.close_daily_feedback_batch_v1($1, $2, $3, $4, $5)`,
  [command(), fingerprint(commandSeq), sessionB, batchB.public_ref, ACK],
  'review_batch_not_open',
);

// Permisos: ni anon ni authenticated llaman a las funciones nuevas, y nadie de
// la API lee la tabla del cierre.
for (const role of ['anon', 'authenticated']) {
  await db.query(`set role ${role}`);
  await expectRejected(`select public.get_daily_feedback_review_page_v2($1, $2, null)`, [sessionA, batchA.public_ref], 'permission denied');
  await expectRejected(`select public.close_daily_feedback_batch_v1($1, $2, $3, $4, $5)`, [command(), fingerprint(commandSeq), sessionA, batchA.public_ref, ACK], 'permission denied');
}
for (const role of ['anon', 'authenticated', 'service_role']) {
  await db.query(`set role ${role}`);
  await expectRejected('select * from public.daily_feedback_batch_closures', [], 'permission denied');
}

// La purga se lleva items y decisiones; el lote y su cierre quedan.
await db.query('set role postgres');
await db.query(`update public.daily_feedback_batches set retention_expires_at=clock_timestamp()-interval '1 second' where id=$1`, [batchA.batch_id]);
await db.query('set role service_role');
const purged = (await db.query(`select public.purge_expired_daily_feedback_v2(
  clock_timestamp(), 'daily-feedback-worker-1', $1, $2, 20) as result`, [TENANT, SCOPE])).rows[0].result;
expect(purged.count === 1, `purga: ${JSON.stringify(purged)}`);
await db.query('reset role');
const afterPurge = (await db.query(
  `select (select count(*)::int from public.daily_feedback_items where batch_id=$1) as items,
          (select count(*)::int from public.daily_feedback_batch_closures where batch_id=$1) as closures,
          (select state from public.daily_feedback_batches where id=$1) as state`, [batchA.batch_id],
)).rows[0];
expect(afterPurge.items === 0 && afterPurge.closures === 1 && afterPurge.state === 'purged', `despues de la purga: ${JSON.stringify(afterPurge)}`);

// La migracion corre entera o no corre.
expect(filtersSql.trimStart().startsWith('--') && /\bbegin;/.test(filtersSql) && /\bcommit;\s*$/.test(filtersSql), 'la migracion va en una transaccion');

console.log('daily feedback review filters validation passed');
