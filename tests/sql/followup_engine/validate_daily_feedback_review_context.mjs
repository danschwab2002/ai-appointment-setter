// Contexto identificado de la revision diaria (ADR-0018, migracion 20260927000100).
//
// Corre las tres migraciones del daily feedback mas la del contexto sobre PGlite,
// con tablas minimas que imitan las columnas que la RPC de contexto lee de las
// tablas de derivacion, reactivacion, reanudacion, opt-out y links de pago
// (esas tablas viven en otras migraciones con una cadena de dependencias larga;
// aca importa la forma de lo que devuelve la RPC, no su schema completo).
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
// Tablas minimas con las columnas que lee get_daily_feedback_conversation_context_v1.
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
  join(root, 'supabase/migrations/20260927000100_daily_feedback_review_context_v2.sql'),
  'utf8',
));

const V2 = {
  sanitizer: 'identity-preserving-redaction-v2',
  selection: 'chatwoot-daily-conversation-context-v2',
  renderer: 'daily-feedback-web-v2',
};
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

await db.exec('set role service_role');
const configured = (await db.query(`
  select public.configure_daily_feedback_scope_v2(
    '10000000-0000-4000-8000-000000000001', '${'1'.repeat(64)}',
    'lancemos', 'psicologajohanna-agent-bot-19', 'https://slack.com',
    'T12345678', $1::jsonb, 'joint-reviewer-accountability-v1',
    1, 2, 19, 'America/Bogota', '18:00:00', 72,
    '${V2.sanitizer}', '${V2.selection}', '${V2.renderer}', false
  ) as result
`, [reviewers])).rows[0].result;
expect(configured.status === 'configured' && configured.reviewer_count === 4, `scope v2 failed: ${JSON.stringify(configured)}`);

// La RPC de contexto exige un scope configurado con el mismo account/inbox.
await expectRejected(
  `select public.get_daily_feedback_conversation_context_v1('lancemos','psicologajohanna-agent-bot-19',1,99,array[186]::bigint[])`,
  [], 'daily_feedback_scope_not_configured',
);
await expectRejected(
  `select public.get_daily_feedback_conversation_context_v1('lancemos','psicologajohanna-agent-bot-19',1,2,null)`,
  [], 'invalid_daily_feedback_context_request',
);
const emptyContext = (await db.query(
  `select public.get_daily_feedback_conversation_context_v1('lancemos','psicologajohanna-agent-bot-19',1,2,array[]::bigint[]) as result`,
)).rows[0].result;
expect(JSON.stringify(emptyContext) === '{}', `empty ids should return {}: ${JSON.stringify(emptyContext)}`);

// Lote A (dia 1): un item v2 con contexto, un item v1 (forma vieja) y un item de un solo mensaje.
const claimA = (await db.query(`
  with captured as (select clock_timestamp() as now_at)
  select public.claim_daily_feedback_collection_v1(
    '20000000-0000-4000-8000-000000000001', '${'2'.repeat(64)}',
    'collector-1', 'lancemos', 'psicologajohanna-agent-bot-19', greatest(
      now_at,
      (((now_at at time zone 'America/Bogota')::date::timestamp + time '18:00:00') at time zone 'America/Bogota') + interval '1 second'
    ), true, 120
  ) as result from captured
`)).rows[0].result;
expect(claimA.status === 'claimed' && claimA.sanitizer_version === V2.sanitizer, `claim A: ${JSON.stringify(claimA)}`);
const t = (minutes) => new Date(new Date(claimA.window_start).getTime() + minutes * 60 * 1000).toISOString();

const context186 = {
  chatwoot_conversation_id: 186,
  conversation_url: 'https://chatwoot.example.test/app/accounts/1/conversations/186',
  contact: { id: 200, name: 'Gustavo Ejemplo', phone: '+5730000000186', email: 'gustavo@example.test' },
  conversation: { status: 'open', labels: ['automation_paused'], assignee: null, created_at: t(0), first_reply_at: null, last_activity_at: t(70), can_reply: true, unread_count: 0 },
  origin: 'inbound',
  events: [{ kind: 'handoff', occurred_at: t(70), primary_reason_code: 'commercial_exception', detail_reason_code: 'explicit_human_request', requested_by: 'agent', status: 'projected' }],
  payment_links: [],
  prior_reviews: [],
  summary: { agent_replied: true, handoff_count: 1 },
};
const v2Messages = [
  { actor: 'prospect', kind: 'prospect_message', occurred_at: t(60), status: 'sent', text: 'Pero acá dice que el costo es de 170 dólares?\nY en este enlace dice que 49?', meta: {} },
  { actor: 'agent', kind: 'agent_reply', occurred_at: t(61), status: 'read', text: 'El precio confirmado es USD 49. Mirá https://pay.hotmart.com/F106691755G?off=b', meta: { chatwoot_message_id: 2412, decision: 'answer', reason_code: 'johanna_e2e_response', part: '1/3' } },
  { actor: 'team', kind: 'handoff_note', occurred_at: t(70), status: 'sent', text: 'Derivación inbound registrada por el bridge. Tel +57 310 255 4012, mail lead@example.test', meta: { author: 'Bridge Service' } },
  { actor: 'system', kind: 'automation_paused', occurred_at: t(70), status: 'sent', text: 'Bridge Service added automation_paused', meta: { actor_name: 'Bridge Service', label: 'automation_paused' } },
];
const itemsA = [
  {
    conversation_ref: 'conv_1234567890abcdef1234', display_label: 'Conversación 01',
    apparent_objective: 'Consulta de precio o formas de pago',
    observed_outcome: 'Derivado a humano (explicit_human_request) · Automatización pausada',
    release_id: 'release_lineage_unavailable', release_version: 0,
    messages: v2Messages, context: context186,
  },
  {
    conversation_ref: 'conv_abcdef1234567890abcd', display_label: 'Conversación 02',
    apparent_objective: 'Revisar la respuesta del agente', observed_outcome: 'Esperando respuesta del prospecto',
    release_id: 'release_lineage_unavailable', release_version: 0,
    messages: [
      { actor: 'prospect', occurred_at: t(80), text: 'Quiero conocer el programa' },
      { actor: 'agent', occurred_at: t(81), text: 'Claro, te explico' },
    ],
  },
  {
    conversation_ref: 'conv_00000000000000000184', display_label: 'Conversación 03',
    apparent_objective: 'Conversación sin objetivo claro', observed_outcome: 'Sin respuesta del agente',
    release_id: 'release_lineage_unavailable', release_version: 0,
    messages: [
      { actor: 'prospect', kind: 'prospect_message', occurred_at: t(90), status: 'sent', text: 'Hola', meta: {} },
    ],
    context: { chatwoot_conversation_id: 184, conversation_url: 'https://chatwoot.example.test/app/accounts/1/conversations/184' },
  },
];
const committedA = (await db.query(`
  select public.commit_daily_feedback_batch_v1(
    '30000000-0000-4000-8000-000000000001', '${'3'.repeat(64)}',
    'collector-1', '${claimA.schedule_id}', ${claimA.lease_generation},
    '${'a'.repeat(64)}', 'release_lineage_unavailable', $1::jsonb
  ) as result
`, [JSON.stringify(itemsA)])).rows[0].result;
expect(committedA.status === 'committed' && committedA.item_count === 3, `commit A: ${JSON.stringify(committedA)}`);

// Lo que la restriccion rechaza: clave desconocida en context, mensaje v2 sin meta,
// actor fuera del alfabeto, URL http en conversation_url, control chars.
const claimRejects = async (items, marker, seed) => {
  const claim = (await db.query(`select public.claim_daily_feedback_collection_v1(
    '2${seed}000000-0000-4000-8000-000000000001', '${seed.repeat(64)}',
    'collector-x', 'lancemos', 'psicologajohanna-agent-bot-19',
    '${claimA.window_end}'::timestamptz + interval '${seed} day', true, 120
  ) as result`)).rows[0].result;
  expect(claim.status === 'claimed', `reject-claim ${seed}: ${JSON.stringify(claim)}`);
  await expectRejected(`select public.commit_daily_feedback_batch_v1(
    '3${seed}000000-0000-4000-8000-000000000001', '${seed.repeat(64)}',
    'collector-x', '${claim.schedule_id}', ${claim.lease_generation},
    '${seed.repeat(64)}', 'release_lineage_unavailable', $1::jsonb
  )`, [JSON.stringify(items)], marker);
  await db.query(`select public.fail_daily_feedback_collection_v1(
    '4${seed}000000-0000-4000-8000-000000000001', '${seed.repeat(64)}',
    'collector-x', '${claim.schedule_id}', ${claim.lease_generation}, 'collection_failed', 60
  )`);
  await db.query('set role postgres');
  await db.query(`update public.daily_feedback_schedules set lease_owner=null, lease_expires_at=null,
    pending_local_date=null, pending_window_start=null, pending_window_end=null, next_attempt_at=clock_timestamp()`);
  await db.query('set role service_role');
};
const base = (overrides) => [{ ...itemsA[0], ...overrides }];
await claimRejects(base({ context: { ...context186, unknown_key: 1 } }), 'invalid_daily_feedback_item_context', '4');
await claimRejects(base({ context: { ...context186, conversation_url: 'http://insecure.example' } }), 'invalid_daily_feedback_item_context', '5');
await claimRejects(base({ messages: [{ actor: 'prospect', kind: 'prospect_message', occurred_at: t(60), status: 'sent', text: 'sin meta' }] }), 'invalid_daily_feedback_item', '6');
await claimRejects(base({ messages: [{ ...v2Messages[0], actor: 'robot' }] }), 'invalid_daily_feedback_item', '7');
await claimRejects(base({ messages: [{ ...v2Messages[0], text: 'con control  char' }] }), 'invalid_daily_feedback_item', '8');

// La pagina devuelve el contexto y los mensajes v2 tal cual.
await db.query('set role postgres');
await db.query(`update public.daily_feedback_batches set retention_expires_at=clock_timestamp()+interval '1 hour' where id='${committedA.batch_id}'`);
await db.query('set role service_role');
const stateHash = 'b'.repeat(64);
const sessionHash = 'c'.repeat(64);
await db.query(`select public.begin_daily_feedback_oidc_v1('${stateHash}', '${committedA.public_ref}',
  '/daily-feedback/review/${committedA.public_ref}', clock_timestamp()+interval '5 minutes')`);
const login = (await db.query(`select public.complete_daily_feedback_oidc_v1(
  '${stateHash}', '${sessionHash}', 'https://slack.com', 'https://slack.com/user_id/U12345678',
  'T12345678', 'U12345678', clock_timestamp()+interval '30 minutes') as result`)).rows[0].result;
expect(login.status === 'authenticated', `login: ${JSON.stringify(login)}`);
const page = (await db.query(`select public.get_daily_feedback_review_page_v1('${sessionHash}', '${committedA.public_ref}') as result`)).rows[0].result;
expect(page.status === 'item' && page.item.position === 1, `page: ${JSON.stringify(page)}`);
expect(page.item.context.chatwoot_conversation_id === 186, 'page lost the conversation id');
expect(page.item.context.contact.phone === '+5730000000186', 'page lost the contact phone');
expect(page.item.messages.length === 4 && page.item.messages[1].meta.decision === 'answer', 'page lost v2 message meta');
expect(page.item.messages[1].text.includes('https://pay.hotmart.com'), 'v2 text was redacted');
const decision = (await db.query(`select public.record_daily_feedback_decision_v1(
  '40000000-0000-4000-8000-000000000001', '${'4'.repeat(64)}',
  '${sessionHash}', '${committedA.public_ref}', '${page.item.item_id}',
  'correct_with_feedback', $1) as result`, ['No repetir el precio dos veces'])).rows[0].result;
expect(decision.status === 'recorded', `decision: ${JSON.stringify(decision)}`);

// Filas durables que la RPC de contexto tiene que devolver por conversacion.
await db.query('set role postgres');
await db.exec(`
  insert into public.human_handoff_requests(chatwoot_account_id, chatwoot_inbox_id, external_conversation_id, primary_reason_code, detail_reason_code, requested_by, status)
  values (1, 2, 186, 'commercial_exception', 'explicit_human_request', 'agent', 'projected'),
         (1, 2, 186, 'policy_requires_human', null, 'agent', 'projected'),
         (1, 3, 186, 'commercial_exception', null, 'agent', 'projected'),
         (1, 2, 999, 'commercial_exception', null, 'agent', 'projected');
  insert into public.conversation_reactivation_events(external_conversation_id, status, template_name, reason_code, provider_message_id, quiet_seconds)
  values (186, 'sent', 'johanna_reactivacion_01', 'outside_service_window', 2375, 28800);
  insert into public.conversation_resume_events(external_conversation_id, reason_code, quiet_seconds)
  values (186, 'inbound_after_quiet_period', 30000);
  insert into public.contact_opt_out_events(canonical_account_id, canonical_inbox_id, canonical_conversation_id, canonical_message_id)
  values (1, 2, 184, 2390);
  insert into public.checkout_offer_catalog(id, offer_code, landing_ref)
  values ('99999999-9999-4999-8999-999999999999', 'bxjge6zq', 'libre-de-ansiedad');
  insert into public.checkout_link_issuances(chatwoot_account_id, chatwoot_inbox_id, chatwoot_conversation_id, status, source_kind, chatwoot_message_id, sck_value, checkout_url_final, purchased_at, offer_catalog_id)
  values (1, 2, 184, 'purchase_matched', 'inbound_request', 2391, 'hermes|v1|01M3DSJ9CR4TQ5VMJNJMKA41TJ', 'https://pay.hotmart.com/F106691755G?off=bxjge6zq', clock_timestamp(), '99999999-9999-4999-8999-999999999999');
`);
await db.query('set role service_role');
const ctx = (await db.query(
  `select public.get_daily_feedback_conversation_context_v1('lancemos','psicologajohanna-agent-bot-19',1,2,array[186,184,555]::bigint[]) as result`,
)).rows[0].result;
expect(Object.keys(ctx).sort().join(',') === '184,186,555', `context keys: ${Object.keys(ctx)}`);
expect(ctx['186'].handoffs.length === 2, `handoffs must be scoped by account/inbox/conversation: ${JSON.stringify(ctx['186'].handoffs)}`);
expect(ctx['186'].handoffs[0].detail_reason_code === 'explicit_human_request', 'handoff detail lost');
expect(ctx['186'].reactivations.length === 1 && ctx['186'].reactivations[0].provider_message_id === 2375, 'reactivation lost');
expect(ctx['186'].resumes.length === 1 && ctx['186'].resumes[0].reason_code === 'inbound_after_quiet_period', 'resume lost');
expect(ctx['186'].opt_outs.length === 0 && ctx['184'].opt_outs.length === 1, 'opt-out scoping wrong');
expect(ctx['184'].payment_links.length === 1 && ctx['184'].payment_links[0].offer_code === 'bxjge6zq' && ctx['184'].payment_links[0].purchased_at !== null, `payment link lost: ${JSON.stringify(ctx['184'].payment_links)}`);
expect(ctx['186'].prior_reviews.length === 1 && ctx['186'].prior_reviews[0].decision === 'correct_with_feedback'
  && ctx['186'].prior_reviews[0].verbatim_feedback === 'No repetir el precio dos veces', `prior reviews: ${JSON.stringify(ctx['186'].prior_reviews)}`);
expect(ctx['184'].prior_reviews.length === 0, 'undecided item must not appear as prior review');
expect(ctx['555'].handoffs.length === 0 && ctx['555'].prior_reviews.length === 0, 'unknown conversation must be empty, not missing');

// Ni anon ni authenticated pueden llamar a la RPC nueva.
for (const role of ['anon', 'authenticated']) {
  await db.query(`set role ${role}`);
  await expectRejected(
    `select public.get_daily_feedback_conversation_context_v1('lancemos','psicologajohanna-agent-bot-19',1,2,array[186]::bigint[])`,
    [], 'permission denied',
  );
}
await db.query('set role postgres');

// Purga: el contexto se va con el item; el tombstone no lo conserva.
await db.query(`update public.daily_feedback_batches set retention_expires_at=clock_timestamp()-interval '1 second' where id='${committedA.batch_id}'`);
await db.query('set role service_role');
const purged = (await db.query(`select public.purge_expired_daily_feedback_v2(
  clock_timestamp(), 'daily-feedback-worker-1', 'lancemos', 'psicologajohanna-agent-bot-19', 20) as result`)).rows[0].result;
expect(purged.count === 1, `purge: ${JSON.stringify(purged)}`);
await db.query('reset role');
const leftovers = (await db.query(`select count(*)::int as n from public.daily_feedback_items where batch_id='${committedA.batch_id}'`)).rows[0].n;
expect(leftovers === 0, 'purge left identified items behind');

console.log('daily feedback review context (v2) validation passed');
