// automation-expert-read-v1: las tres lecturas del Automation Expert sobre la
// cadena completa de migraciones (docs/contracts/automation-expert-read-v1.md).
//
// Fija las pruebas del contrato que viven en la base:
//   1. una instancia no ve los flujos ni los leads de otra, con dos instancias
//      que comparten el tenant `lancemos` y tienen bandejas y productos
//      distintos (es la primera que corre);
//   2. las tres son STABLE SECURITY DEFINER con search_path fijo, solo las
//      ejecuta service_role y corren adentro de una transaccion de solo
//      lectura sin error, sin cambiar ninguna fila;
//   3. ninguna respuesta lleva telefono, mail, nombre completo, contenido, la
//      URL de un link ni un payload: cada columna de texto que leen lleva un
//      canario que no puede aparecer en la salida;
//   4. el bucle del lease sale como un solo momento, con su cuenta;
//   5. un reason_code con un telefono adentro sale null y marcado, y la
//      historia sale igual.
// Ademas: la regla de identidad de los eventos de Hotmart sin caso (decision
// de Dan del 2026-10-15), la busqueda por las tres llaves y el tope de cinco
// candidatos.
//
// Los datos se siembran con session_replication_role = replica (sin
// disparadores ni claves foraneas) y con la forma medida en att1-db el
// 2026-10-15: los mismos estados, motivos, claves de data y tipos de evento.

import { PGlite } from '@electric-sql/pglite';
import { createHash, randomUUID } from 'node:crypto';
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

const fail = (message) => {
  throw new Error(message);
};
const one = (rows, label) => {
  if (rows.length !== 1) fail(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};

// ---------------------------------------------------------------------------
// Las dos instancias. Comparten el tenant: lo que las separa es la bandeja, el
// producto, el scope entrante y el scope del agente.
// ---------------------------------------------------------------------------
const ATT1 = {
  tenant_ref: 'lancemos',
  ally_ref: 'att1',
  funnel_ref: 'att1',
  chatwoot_account_id: 2,
  chatwoot_inbox_id: 11,
  hotmart_product_id: '5071808',
  inbound_scope_key: 'att1-inbound',
  agent_scope_ref: 'att1-agent-bot',
  time_zone: 'America/Mexico_City',
  scopes: [
    { flow_group: 'recuperacion', key: 'att1-recuperacion', version: 5 },
    { flow_group: 'precheckout', key: 'att1-primer-contacto', version: 2 },
  ],
};
const OTRA = {
  ...ATT1,
  ally_ref: 'att2',
  funnel_ref: 'att2',
  chatwoot_inbox_id: 12,
  hotmart_product_id: '8437428',
  inbound_scope_key: 'att2-inbound',
  agent_scope_ref: 'att2-agent-bot',
  scopes: [{ flow_group: 'recuperacion', key: 'att2-recuperacion', version: 1 }],
};

const id = () => randomUUID();
const ids = new Proxy({}, { get: (cache, key) => (cache[key] ??= id()) });
const now = Date.now();
const at = (minutes) => new Date(now + minutes * 60_000).toISOString();
// T0: el formulario del lead A, tres dias atras (adentro de la ventana de 7
// dias completos de los flujos).
const T0 = -3 * 24 * 60;
const t = (minutes) => at(T0 + minutes);
const nameKey = (fullName) => createHash('sha256')
  .update(fullName.split(/\s+/).filter(Boolean).join(' ').toLowerCase(), 'utf8')
  .digest('hex');
const hex64 = (seed) => createHash('sha256').update(seed).digest('hex');

// Los canarios: valores que nunca pueden salir de la base.
const CANARIES = [
  'CANARY', 'canary', 'example.test', 'pay.hotmart.com', 'hermes~v1~',
  '5512345678', '5598765432', '5511112222', '5544443333',
  'CANARYSURNAME', 'CANARYB', 'CANARYC', 'CANARYX', 'CANARYNBSP', 'CANARYF', 'CANARYY',
];

const cart = (productId, extra = '') => JSON.stringify({
  data: { product: { id: Number(productId), name: 'CANARY_PRODUCT' },
          buyer: { email: 'buyer.canary@example.test', phone: '5512345678', name: 'CANARY_BUYER' } },
  extra,
});

await db.exec(`
set session_replication_role = replica;

-- Las politicas y los scopes de las dos instancias.
insert into public.followup_policy_versions (
  policy_key, version, status, purpose, timezone, business_windows, grace_period,
  expires_after, max_automatic_messages, steps, approved_by, approved_at, published_at
) values
  ('att1-recuperacion-un-toque', 1, 'published', 'cart_recovery', 'America/Mexico_City',
   '[{"end": "21:00", "days": [1,2,3,4,5,6,7], "start": "09:00"}]', '0 minutes', '1 day', 1,
   '[{"mode": "freeform", "step_key": "first_contact"}, {"mode": "freeform", "step_key": "payment_failure_first_contact"}]',
   'CANARY_APPROVER', now(), now()),
  ('att1-primer-contacto-formulario', 1, 'published', 'cart_recovery', 'America/Mexico_City',
   '[{"end": "21:00", "days": [1,2,3,4,5,6,7], "start": "09:00"}]', '1 hour', '1 day', 1,
   '[{"mode": "freeform", "step_key": "first_contact"}]', 'CANARY_APPROVER', now(), now()),
  ('att2-un-toque', 1, 'published', 'cart_recovery', 'America/Mexico_City',
   '[{"end": "21:00", "days": [1,2,3,4,5,6,7], "start": "09:00"}]', '0 minutes', '1 day', 1,
   '[{"mode": "freeform", "step_key": "first_contact"}]', 'CANARY_APPROVER', now(), now());

insert into public.pilot_scope_versions (
  scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id, channel,
  channel_provider, channel_account_ref, source, source_event_type, external_product_id,
  offer_code, purpose, policy_key, policy_version, timezone, max_cohort_contacts,
  max_outbound_request_starts_total, max_outbound_request_starts_per_day, approved_by,
  approved_at, published_at, additional_source_event_types, audience_mode
) values
  ('att1-recuperacion', 4, 'published', 'lancemos', 2, 11, 'whatsapp', 'chatwoot', 'chatwoot:2',
   'hotmart', 'PURCHASE_OUT_OF_SHOPPING_CART', '5071808', '2uafw5bg', 'cart_recovery',
   'att1-recuperacion-un-toque', 1, 'America/Mexico_City', 3, 1000, 40, 'CANARY_APPROVER', now(), now(),
   '{PURCHASE_CANCELED}', 'consented_intent'),
  ('att1-recuperacion', 5, 'published', 'lancemos', 2, 11, 'whatsapp', 'chatwoot', 'chatwoot:2',
   'hotmart', 'PURCHASE_OUT_OF_SHOPPING_CART', '5071808', '2uafw5bg', 'cart_recovery',
   'att1-recuperacion-un-toque', 1, 'America/Mexico_City', 3, 1000, 80, 'CANARY_APPROVER', now(), now(),
   '{PURCHASE_CANCELED}', 'consented_intent'),
  ('att1-primer-contacto', 2, 'published', 'lancemos', 2, 11, 'whatsapp', 'chatwoot', 'chatwoot:2',
   'landing', 'PRECHECKOUT_FORM_SUBMITTED', '5071808', '2uafw5bg', 'cart_recovery',
   'att1-primer-contacto-formulario', 1, 'America/Mexico_City', 3, 150, 10, 'CANARY_APPROVER', now(), now(),
   '{}', 'consented_intent'),
  ('att2-recuperacion', 1, 'published', 'lancemos', 2, 12, 'whatsapp', 'chatwoot', 'chatwoot:2',
   'hotmart', 'PURCHASE_OUT_OF_SHOPPING_CART', '8437428', 'att2offer', 'cart_recovery',
   'att2-un-toque', 1, 'America/Mexico_City', 3, 500, 50, 'CANARY_APPROVER', now(), now(),
   '{}', 'consented_intent');

insert into public.pilot_runtime_controls (scope_key, scope_version, runtime_state, changed_by, change_reason, changed_at)
values ('att1-recuperacion', 5, 'armed', 'CANARY_ACTOR', 'CANARY_REASON', '${t(-5 * 24 * 60)}'),
       ('att1-primer-contacto', 2, 'armed', 'CANARY_ACTOR', 'CANARY_REASON', '${t(-6 * 24 * 60)}'),
       ('att2-recuperacion', 1, 'armed', 'CANARY_ACTOR', 'CANARY_REASON', '${t(-6 * 24 * 60)}');

insert into public.pilot_control_events (scope_key, scope_version, event_type, runtime_generation, actor, reason_code, data, created_at)
values ('att1-recuperacion', 4, 'pilot_runtime_state_changed', 1, 'CANARY_ACTOR', 'pilot_runtime_state_changed',
        '{"to_state": "paused"}', '${t(-2 * 24 * 60 - 10)}'),
       ('att1-recuperacion', 5, 'pilot_scope_version_activated', 1, 'CANARY_ACTOR', 'pilot_scope_version_activated',
        '{"from_version": 4, "to_version": 5, "runtime_state": "inactive"}', '${t(-2 * 24 * 60)}'),
       ('att1-recuperacion', 5, 'pilot_runtime_state_changed', 2, 'CANARY_ACTOR', 'pilot_runtime_state_changed',
        '{"to_state": "armed"}', '${t(-2 * 24 * 60 + 1)}'),
       ('att1-recuperacion', 5, 'pilot_runtime_state_changed', 3, 'CANARY_ACTOR', 'pilot_runtime_state_changed',
        '{"to_state": "paused"}', '${t(5)}'),
       ('att1-recuperacion', 5, 'pilot_runtime_state_changed', 4, 'CANARY_ACTOR', 'pilot_runtime_state_changed',
        '{"to_state": "armed"}', '${t(15)}');

-- Lead A (ATT1): formulario, carrito que le gana al primer contacto, respuesta
-- adoptada, turnos del agente, link, cupon, derivacion atendida y compra.
insert into public.contacts (id, full_name, email, phone, metadata)
values ('${ids.contactA}', 'Ana  CANARYSURNAME Perez', 'ana.canary@example.test', '+5215512345678',
        '{"note": "CANARY_CONTACT_META"}');
insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, metadata)
values ('${ids.contactA}', 'email', 'Ana.Canary@example.test', 'ana.canary@example.test', 'system', '{"c": "CANARY"}'),
       ('${ids.contactA}', 'phone', '+52 1 55 1234 5678', '+5215512345678', 'system', '{"c": "CANARY"}');
insert into public.channel_identities (id, contact_id, channel, account_id, external_user_id, external_conversation_id, username, identity_status, metadata)
values ('${ids.identityA}', '${ids.contactA}', 'whatsapp', 'chatwoot:2', '5215512345678', '501', 'CANARY_USERNAME', 'active', '{"c": "CANARY"}');
insert into public.conversations (id, contact_id, channel_identity_id, status, automation_status, human_takeover, commercial_context)
values ('${ids.convA}', '${ids.contactA}', '${ids.identityA}', 'active', 'draft_only', false,
        '{"chatwoot_conversation_id": "501"}');
insert into public.purchase_intents (id, tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref, normalized_email, normalized_phone,
  submitted_at, lifecycle_state, whatsapp_contact_authorized, provisional, provider_observed, activation_authorized)
values ('${ids.intentA}', 'lancemos', 'att1', 'alimenta-tu-tiroides-d', '5071808', '2uafw5bg', 'ana.canary@example.test',
        '5215512345678', '${t(0)}', 'purchased', true, false, true, false);
insert into public.webhook_events (id, source, external_event_id, event_type, payload, processing_status, received_at)
values ('${ids.formA}', 'system', 'form-a', 'PRECHECKOUT_FORM_SUBMITTED', '{"c": "CANARY_FORM"}', 'processed', '${t(0)}'),
       ('${ids.cartA}', 'hotmart', 'cart-a', 'PURCHASE_OUT_OF_SHOPPING_CART', '${cart('5071808')}', 'processed', '${t(41)}'),
       ('${ids.buyA}', 'hotmart', 'buy-a', 'PURCHASE_APPROVED', '${cart('5071808')}', 'failed', '${t(2 * 24 * 60 + 300)}');
update public.webhook_events set processing_error = 'purchase_correlation_case_not_found' where id = '${ids.buyA}';
insert into public.hotmart_purchase_intent_event_identities (webhook_event_id, normalized_email, normalized_phone)
values ('${ids.cartA}', 'ana.canary@example.test', '5215512345678'),
       ('${ids.buyA}', 'ana.canary@example.test', '5215512345678');
insert into public.hotmart_purchase_intent_correlations (webhook_event_id, event_type, outcome, purchase_intent_id, matched_by,
  candidate_count, reason_code, manual_handoff_required, observed_at)
values ('${ids.cartA}', 'PURCHASE_OUT_OF_SHOPPING_CART', 'resolved', '${ids.intentA}', 'email_and_phone', 1,
        'exact_email_and_phone', false, '${t(41)}');

insert into public.recovery_cases (id, contact_id, conversation_id, abandonment_event_id, source, external_product_id, product_name,
  status, grace_expires_at, context, policy_key, policy_version, commercial_case_id, hotmart_purchase_intent_id, created_at, closed_at)
values ('${ids.caseFormA}', '${ids.contactA}', null, '${ids.formA}', 'landing', '5071808', 'CANARY_PRODUCT_NAME',
        'cancelled', '${t(60)}', '{"c": "CANARY_CTX"}', 'att1-primer-contacto-formulario', 1, '${ids.caseFormA}', '${ids.intentA}',
        '${t(0.1)}', null),
       ('${ids.caseCartA}', '${ids.contactA}', '${ids.convA}', '${ids.cartA}', 'hotmart', '5071808', 'CANARY_PRODUCT_NAME',
        'sequence_exhausted', '${t(41)}', '{"c": "CANARY_CTX"}', 'att1-recuperacion-un-toque', 1, '${ids.caseCartA}', '${ids.intentA}',
        '${t(41.5)}', '${t(46)}');
insert into public.pilot_recovery_case_bindings (recovery_case_id, scope_key, scope_version, source_event_id, bound_at, audience_mode,
  audience_purchase_intent_id, audience_precheckout_submission_id)
values ('${ids.caseFormA}', 'att1-primer-contacto', 2, '${ids.formA}', '${t(0.1)}', 'consented_intent', '${ids.intentA}', '${ids.submissionA}'),
       ('${ids.caseCartA}', 'att1-recuperacion', 5, '${ids.cartA}', '${t(41.5)}', 'consented_intent', '${ids.intentA}', '${ids.submissionA}');
insert into public.recovery_case_events (recovery_case_id, webhook_event_id, event_role, observed_at)
values ('${ids.caseFormA}', '${ids.formA}', 'precheckout_intent', '${t(0)}'),
       ('${ids.caseCartA}', '${ids.cartA}', 'cart_abandonment', '${t(41)}');
insert into public.portable_precheckout_first_contact_plans (submission_id, purchase_intent_id, contact_id, recovery_case_id, scope_key,
  scope_version, outcome, reason_code, created_at)
values ('${ids.submissionA}', '${ids.intentA}', '${ids.contactA}', '${ids.caseFormA}', 'att1-primer-contacto', 2, 'planned',
        'first_contact_scheduled', '${t(0.1)}');

insert into public.scheduled_actions (id, recovery_case_id, followup_sequence_id, conversation_id, action_type, status, due_at, expires_at,
  expected_case_version, idempotency_key, metadata, error_message, policy_key, policy_version, step_key, anchor_type,
  anchor_subject_internal_id, anchor_observed_at, terminal_reason, created_at, updated_at)
values ('${ids.actionFormA}', '${ids.caseFormA}', '${ids.seqFormA}', null, 'first_contact_review', 'cancelled', '${t(60)}', '${t(24 * 60)}',
        1, 'idem-form-a', '{"c": "CANARY_META"}', 'CANARY_ERROR', 'att1-primer-contacto-formulario', 1, 'first_contact',
        'precheckout_intent', '${ids.intentA}', '${t(0)}', 'superseded_by_provider_event', '${t(0.1)}', '${t(60)}'),
       ('${ids.actionCartA}', '${ids.caseCartA}', '${ids.seqCartA}', '${ids.convA}', 'first_contact_review', 'accepted_by_chatwoot',
        '${t(41.5)}', '${t(24 * 60)}', 1, 'idem-cart-a', '{"c": "CANARY_META"}', null, 'att1-recuperacion-un-toque', 1,
        'first_contact', 'cart_abandonment', '${ids.cartA}', '${t(41)}', 'accepted_by_chatwoot', '${t(41.5)}', '${t(46)}');
insert into public.conversation_events (recovery_case_id, related_action_id, event_type, actor_type, data, created_at)
values ('${ids.caseCartA}', '${ids.actionCartA}', 'followup_action_claimed', 'system', '{"worker_id": "CANARY_WORKER"}', '${t(41.6)}'),
       ('${ids.caseCartA}', '${ids.actionCartA}', 'followup_action_reevaluated', 'system',
        '{"decision": "defer", "reason_code": "business_window_closed", "detail": "CANARY_DETAIL"}', '${t(41.7)}'),
       ('${ids.caseCartA}', '${ids.actionCartA}', 'followup_action_reevaluated', 'system',
        '{"decision": "defer", "reason_code": "business_window_closed"}', '${t(43)}'),
       ('${ids.caseCartA}', '${ids.actionCartA}', 'followup_action_reevaluated', 'system',
        '{"decision": "execute", "reason_code": "eligible_for_execution"}', '${t(45.9)}'),
       ('${ids.caseFormA}', '${ids.actionFormA}', 'followup_action_reevaluated', 'system',
        '{"decision": "cancel", "reason_code": "superseded_by_provider_event", "detail": null}', '${t(60)}'),
       ('${ids.caseCartA}', null, 'inbound_adopted_template_conversation', 'integration',
        '{"chatwoot_conversation_id": "501", "scope_key": "att1-recuperacion"}', '${t(92)}');
insert into public.messages (id, conversation_id, external_message_id, direction, actor_type, message_type, content, delivery_status,
  is_followup, semantic_metadata, occurred_at)
values ('${ids.messageA}', '${ids.convA}', '3001', 'outbound', 'ai_agent', 'followup', 'CANARY_CONTENT hola Ana', 'accepted', false,
        '{"c": "CANARY"}', '${t(46)}');
insert into public.followup_delivery_attempts (id, action_id, idempotency_key, attempt_number, channel, mode, phase, outcome,
  remote_message_id, accepted_message_id, started_at, request_started_at, accepted_at, reason_code, lease_generation,
  expected_case_version, expected_sequence_revision, created_at)
values ('${ids.attemptA}', '${ids.actionCartA}', 'idem-attempt-a', 1, 'whatsapp', 'approved_template', 'completed',
        'accepted_by_chatwoot', 'CANARY_WAMID', '${ids.messageA}', '${t(46)}', '${t(46)}', '${t(46)}', 'accepted_by_chatwoot', 1, 1, 1, '${t(46)}');
insert into public.pilot_outbound_request_authorizations (scope_key, scope_version, action_id, attempt_id, contact_id,
  local_budget_date, runtime_generation, reason_code, authorized_at)
values ('att1-recuperacion', 5, '${ids.actionCartA}', '${ids.attemptA}', '${ids.contactA}',
        ('${t(46)}'::timestamptz at time zone 'America/Mexico_City')::date, 2, 'pilot_request_start_authorized', '${t(46)}');

insert into public.commercial_cases (id, case_kind, contact_id, selected_channel_identity_id, conversation_id, status, automation_status,
  authority_mode, inbound_scope_key, inbound_scope_version, tenant_ref)
values ('${ids.commercialA}', 'inbound_sales', '${ids.contactA}', '${ids.identityA}', '${ids.convA}', 'active', 'draft_only', 'shadow',
        'att1-inbound', 1, 'lancemos');
insert into public.agent_turn_provenance (tenant_ref, scope_ref, external_conversation_id, turn_digest, occurred_at, outcome,
  model_requested, model_answered, bridge_release, context_builder_version, context_digest, context_added)
values ('lancemos', 'att1-agent-bot', 501, '${hex64('turn-a1')}', '${t(92.5)}', 'completed', 'att1-agente-comercial', null,
        'abcdef1234567', 'agent-context-v1', '${hex64('ctx-a1')}', '{"known": "CANARY_KNOWN"}'),
       ('lancemos', 'att1-agent-bot', 501, '${hex64('turn-a2')}', '${t(24 * 60 + 90)}', 'completed', 'att1-agente-comercial', null,
        'abcdef1234567', 'agent-context-v1', '${hex64('ctx-a2')}', '{"known": "CANARY_KNOWN"}'),
       ('lancemos', 'att2-agent-bot', 501, '${hex64('turn-a3')}', '${t(93)}', 'completed', 'otro-agente', null,
        'abcdef1234567', 'agent-context-v1', '${hex64('ctx-a3')}', '{}');
insert into public.checkout_link_issuances (id, issuance_ulid, commercial_case_id, purchase_intent_id, contact_id, channel_identity_id,
  offer_catalog_id, chatwoot_account_id, chatwoot_inbox_id, chatwoot_conversation_id, trigger_external_message_id, source_kind,
  original_sck, source_value, sck_format_version, sck_value, checkout_url_final, status, chatwoot_message_id,
  hotmart_webhook_event_id, created_at, finalized_at, purchased_at, offer_resolution, attribution_resolution)
values ('${ids.linkA}', '01JABCDEFGHJKMNPQRSTVWXYZ0', '${ids.commercialA}', '${ids.intentA}', '${ids.contactA}', '${ids.identityA}',
        '${ids.offer}', 2, 11, 501, '3040', 'inbound_request', 'CANARY_SCK', 'hermes', 'v1',
        'CANARYSCKPREFIX~hermes~v1~01JABCDEFGHJKMNPQRSTVWXYZ0',
        'https://pay.hotmart.com/CANARYHOTLINK?off=2uafw5bg&checkoutMode=10&src=hermes&sck=CANARYSCKPREFIX~hermes~v1~01JABCDEFGHJKMNPQRSTVWXYZ0&fbclid=CANARYFBCLID',
        'purchase_matched', 3050, '${ids.buyA}', '${t(24 * 60 + 98)}', '${t(24 * 60 + 98)}',
        '${t(2 * 24 * 60 + 300)}', 'lead_intent', 'full');
insert into public.conversation_followup_events (id, conversation_id, commercial_case_id, external_conversation_id, command_key, regime,
  status, template_name, template_language, coupon_code, last_inbound_message_id, last_outbound_message_id, inbound_age_seconds,
  checkout_issuance_id, provider_message_id, failure_reason, created_at, settled_at)
values ('${ids.couponA}', '${ids.convA}', '${ids.commercialA}', 501, 'coupon:501', 'link_sent_no_purchase', 'sent',
        'att1_seguimiento_descuento_01', 'es_MX', 'CANARYCOUPON', 3010, 3020, 90000, '${ids.linkA}', 3101, null,
        '${t(24 * 60 + 98)}', '${t(24 * 60 + 98)}');
insert into public.human_handoff_requests (id, conversation_id, command_key, primary_reason_code, requested_by, projection_policy_key,
  projection_policy_version, chatwoot_account_id, chatwoot_inbox_id, external_conversation_id, expected_team_id, note_template_key,
  note_template_version, private_note_body, status, created_at, projected_at, commercial_case_id, inbound_scope_key,
  inbound_scope_version, detail_reason_code, attended_at)
values ('${ids.handoffA}', '${ids.convA}', 'handoff:501', 'policy_requires_human', 'agent', 'handoff-policy', 1, 2, 11, 501, 7,
        'handoff-note', 1, 'CANARY_NOTE la persona pidio hablar con alguien', 'projected', '${t(24 * 60 + 100)}',
        '${t(24 * 60 + 100)}', '${ids.commercialA}', 'att1-inbound', 1, 'pricing_question', '${t(24 * 60 + 220)}');
insert into public.lead_first_name_inferences (name_key, result, first_name, model_name, prompt_version)
values ('${nameKey('Ana  CANARYSURNAME Perez')}', 'confident', 'Ana', 'CANARY_MODEL', 'lead-first-name-v2');

-- Lead B (ATT1): un primer contacto vivo y un carrito de la v4 que el piloto
-- rechazo en cada vuelta del lease despues de que se activo la v5.
insert into public.contacts (id, full_name, email, phone)
values ('${ids.contactB}', 'Beto CANARYB Lopez', 'beto.canary@example.test', '+5215544443333');
insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
values ('${ids.contactB}', 'phone', '5215544443333', '+5215544443333', 'system');
insert into public.channel_identities (id, contact_id, channel, account_id, external_user_id, external_conversation_id, identity_status)
values ('${ids.identityB}', '${ids.contactB}', 'whatsapp', 'chatwoot:2', '5215544443333', '502', 'active');
insert into public.conversations (id, contact_id, channel_identity_id, status, automation_status, human_takeover, commercial_context)
values ('${ids.convB}', '${ids.contactB}', '${ids.identityB}', 'active', 'enabled', false, '{"chatwoot_conversation_id": "502"}');
insert into public.purchase_intents (id, tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref, normalized_phone,
  submitted_at, lifecycle_state, whatsapp_contact_authorized, provisional, provider_observed, activation_authorized)
values ('${ids.intentB}', 'lancemos', 'att1', 'alimenta-tu-tiroides', '5071808', 'bmaztyhg', '5215544443333',
        '${at(-20)}', 'waiting_for_purchase', true, false, true, false);
insert into public.webhook_events (id, source, external_event_id, event_type, payload, processing_status, received_at)
values ('${ids.formB}', 'system', 'form-b', 'PRECHECKOUT_FORM_SUBMITTED', '{"c": "CANARY_FORM"}', 'processed', '${at(-20)}'),
       ('${ids.cartB4}', 'hotmart', 'cart-b4', 'PURCHASE_OUT_OF_SHOPPING_CART', '${cart('5071808')}', 'processed', '${t(-3 * 24 * 60)}');
insert into public.recovery_cases (id, contact_id, conversation_id, abandonment_event_id, source, external_product_id, product_name,
  status, grace_expires_at, context, policy_key, policy_version, commercial_case_id, hotmart_purchase_intent_id, created_at)
values ('${ids.caseFormB}', '${ids.contactB}', null, '${ids.formB}', 'landing', '5071808', 'x', 'active', '${at(40)}', '{}',
        'att1-primer-contacto-formulario', 1, '${ids.caseFormB}', '${ids.intentB}', '${at(-20)}'),
       ('${ids.caseCartB4}', '${ids.contactB}', '${ids.convB}', '${ids.cartB4}', 'hotmart', '5071808', 'x', 'expired',
        '${t(-3 * 24 * 60)}', '{}', 'att1-recuperacion-un-toque', 1, '${ids.caseCartB4}', '${ids.intentB}', '${t(-3 * 24 * 60)}');
insert into public.pilot_recovery_case_bindings (recovery_case_id, scope_key, scope_version, source_event_id, bound_at, audience_mode,
  audience_purchase_intent_id, audience_precheckout_submission_id)
values ('${ids.caseFormB}', 'att1-primer-contacto', 2, '${ids.formB}', '${at(-20)}', 'consented_intent', '${ids.intentB}', '${ids.submissionB}'),
       ('${ids.caseCartB4}', 'att1-recuperacion', 4, '${ids.cartB4}', '${t(-3 * 24 * 60)}', 'consented_intent', '${ids.intentB}', '${ids.submissionB}');
insert into public.recovery_case_events (recovery_case_id, webhook_event_id, event_role, observed_at)
values ('${ids.caseCartB4}', '${ids.cartB4}', 'cart_abandonment', '${t(-3 * 24 * 60)}');
insert into public.portable_precheckout_first_contact_plans (submission_id, purchase_intent_id, contact_id, recovery_case_id, scope_key,
  scope_version, outcome, reason_code, created_at)
values ('${ids.submissionB}', '${ids.intentB}', '${ids.contactB}', '${ids.caseFormB}', 'att1-primer-contacto', 2, 'planned',
        'first_contact_scheduled', '${at(-20)}');
insert into public.scheduled_actions (id, recovery_case_id, followup_sequence_id, action_type, status, due_at, expires_at,
  expected_case_version, idempotency_key, policy_key, policy_version, step_key, anchor_type, anchor_subject_internal_id,
  anchor_observed_at, terminal_reason, created_at)
values ('${ids.actionFormB}', '${ids.caseFormB}', '${ids.seqFormB}', 'first_contact_review', 'pending', '${at(40)}', '${at(24 * 60)}',
        1, 'idem-form-b', 'att1-primer-contacto-formulario', 1, 'first_contact', 'precheckout_intent', '${ids.intentB}',
        '${at(-20)}', null, '${at(-20)}'),
       ('${ids.actionCartB4}', '${ids.caseCartB4}', '${ids.seqCartB4}', 'first_contact_review', 'expired', '${t(-3 * 24 * 60)}',
        '${t(-2 * 24 * 60)}', 1, 'idem-cart-b4', 'att1-recuperacion-un-toque', 1, 'first_contact', 'cart_abandonment',
        '${ids.cartB4}', '${t(-3 * 24 * 60)}', 'expired_after_worker_crash', '${t(-3 * 24 * 60)}');
insert into public.followup_delivery_attempts (action_id, idempotency_key, attempt_number, channel, mode, phase, started_at,
  lease_generation, expected_case_version, expected_sequence_revision, created_at)
values ('${ids.actionCartB4}', 'idem-b4-1', 1, 'whatsapp', 'approved_template', 'reserved', '${t(-2 * 24 * 60 + 10)}', 1, 1, 1, '${t(-2 * 24 * 60 + 10)}'),
       ('${ids.actionCartB4}', 'idem-b4-2', 2, 'whatsapp', 'approved_template', 'reserved', '${t(-2 * 24 * 60 + 15)}', 2, 1, 1, '${t(-2 * 24 * 60 + 15)}'),
       ('${ids.actionCartB4}', 'idem-b4-3', 3, 'whatsapp', 'approved_template', 'reserved', '${t(-2 * 24 * 60 + 20)}', 3, 1, 1, '${t(-2 * 24 * 60 + 20)}');
insert into public.conversation_events (recovery_case_id, related_action_id, event_type, actor_type, data, created_at)
values ('${ids.caseCartB4}', '${ids.actionCartB4}', 'followup_action_claimed', 'system', '{}', '${t(-2 * 24 * 60 + 10)}'),
       ('${ids.caseCartB4}', '${ids.actionCartB4}', 'followup_action_reevaluated', 'system',
        '{"decision": "execute", "reason_code": "eligible_for_execution"}', '${t(-2 * 24 * 60 + 10)}'),
       ('${ids.caseCartB4}', '${ids.actionCartB4}', 'followup_action_reevaluated', 'system',
        '{"decision": "execute", "reason_code": "eligible_for_execution"}', '${t(-2 * 24 * 60 + 15)}'),
       ('${ids.caseCartB4}', '${ids.actionCartB4}', 'followup_action_expired', 'system',
        '{"reason_code": "expired_after_worker_crash"}', '${t(-2 * 24 * 60 + 30)}');

-- Lead C (ATT1): atado a la instancia solo por la baja. Sus eventos de Hotmart
-- no abrieron caso y se atan por identidad.
insert into public.contacts (id, full_name, email, phone, contact_permission, lifecycle_status)
values ('${ids.contactC}', 'Ceci CANARYC Diaz', 'ceci.canary@example.test', '+5215598765432', 'opted_out', 'do_not_contact');
insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
values ('${ids.contactC}', 'email', 'ceci.canary@example.test', 'ceci.canary@example.test', 'hotmart'),
       ('${ids.contactC}', 'phone', '+52 55 9876 5432', '+525598765432', 'hotmart');
insert into public.contact_opt_out_events (contact_id, channel, purpose, source, canonical_account_id, canonical_inbox_id,
  canonical_conversation_id, canonical_message_id, external_user_id, occurred_at, normalized_rule_key, correlation_status,
  projection_status)
values ('${ids.contactC}', 'whatsapp', 'cart_recovery', 'chatwoot', 2, 11, 503, 4001, '5215598765432', '${t(30)}',
        'stop_receiving_messages', 'applied', 'applied');
-- Un contacto suelto, sin ancla en ninguna instancia, que comparte un telefono.
insert into public.contacts (id, full_name) values ('${ids.contactD}', 'Dario CANARY Suelto');
insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
values ('${ids.contactD}', 'phone', '5215511112222', '+5215511112222', 'hotmart');
insert into public.webhook_events (id, source, external_event_id, event_type, payload, processing_status, processing_error, received_at)
values ('${ids.rejectC}', 'hotmart', 'rej-c', 'PURCHASE_OUT_OF_SHOPPING_CART', '${cart('5071808')}', 'failed',
        'pilot_scope_rejected:pilot_runtime_not_armed', '${t(10)}'),
       ('${ids.badC}', 'hotmart', 'bad-c', 'PURCHASE_OUT_OF_SHOPPING_CART', '${cart('5071808')}', 'failed',
        'Error con el telefono 5215511112222', '${t(11)}'),
       ('${ids.buyC}', 'hotmart', 'buy-c', 'PURCHASE_APPROVED', '${cart('5071808')}', 'failed',
        'purchase_correlation_case_not_found', '${t(20)}'),
       ('${ids.ambiguousC}', 'hotmart', 'amb-c', 'PURCHASE_OUT_OF_SHOPPING_CART', '${cart('5071808')}', 'failed',
        'pilot_scope_rejected:pilot_runtime_not_armed', '${t(12)}'),
       ('${ids.otherProductC}', 'hotmart', 'other-c', 'PURCHASE_OUT_OF_SHOPPING_CART', '${cart('8437428')}', 'failed',
        'pilot_scope_rejected:pilot_runtime_not_armed', '${t(13)}'),
       ('${ids.slipA}', 'hotmart', 'slip-1', 'PURCHASE_BILLET_PRINTED', '${cart('5071808')}', 'failed',
        'unsupported_persisted_event_type', '${t(14)}'),
       ('${ids.slipOther}', 'hotmart', 'slip-2', 'PURCHASE_BILLET_PRINTED', '${cart('8437428')}', 'failed',
        'unsupported_persisted_event_type', '${t(14)}');
insert into public.hotmart_purchase_intent_event_identities (webhook_event_id, normalized_email, normalized_phone)
values ('${ids.rejectC}', 'ceci.canary@example.test', null),
       ('${ids.badC}', null, '5215598765432'),
       ('${ids.buyC}', null, '5215598765432'),
       ('${ids.ambiguousC}', 'ceci.canary@example.test', '5215511112222'),
       ('${ids.otherProductC}', 'ceci.canary@example.test', null);

-- Lead X (la otra instancia): mismo tenant, se llama casi igual que A.
insert into public.contacts (id, full_name) values ('${ids.contactX}', 'Ana CANARYX Perez');
insert into public.channel_identities (id, contact_id, channel, account_id, external_user_id, external_conversation_id, identity_status)
values ('${ids.identityX}', '${ids.contactX}', 'whatsapp', 'chatwoot:2', '5215500000099', '601', 'active');
insert into public.conversations (id, contact_id, channel_identity_id, status, automation_status, human_takeover, commercial_context)
values ('${ids.convX}', '${ids.contactX}', '${ids.identityX}', 'active', 'enabled', false, '{"chatwoot_conversation_id": "601"}');
insert into public.webhook_events (id, source, external_event_id, event_type, payload, processing_status, received_at)
values ('${ids.cartX}', 'hotmart', 'cart-x', 'PURCHASE_OUT_OF_SHOPPING_CART', '${cart('8437428')}', 'processed', '${t(5)}');
insert into public.recovery_cases (id, contact_id, conversation_id, abandonment_event_id, source, external_product_id, product_name,
  status, grace_expires_at, context, policy_key, policy_version, commercial_case_id, created_at)
values ('${ids.caseX}', '${ids.contactX}', '${ids.convX}', '${ids.cartX}', 'hotmart', '8437428', 'x', 'sequence_exhausted',
        '${t(5)}', '{}', 'att2-un-toque', 1, '${ids.caseX}', '${t(5)}');
insert into public.pilot_recovery_case_bindings (recovery_case_id, scope_key, scope_version, source_event_id, bound_at, audience_mode,
  audience_purchase_intent_id, audience_precheckout_submission_id)
values ('${ids.caseX}', 'att2-recuperacion', 1, '${ids.cartX}', '${t(5)}', 'consented_intent', '${ids.intentX}', '${ids.submissionX}');
insert into public.recovery_case_events (recovery_case_id, webhook_event_id, event_role, observed_at)
values ('${ids.caseX}', '${ids.cartX}', 'cart_abandonment', '${t(5)}');

-- Seis Zoe atadas a ATT1 por una baja: la busqueda por nombre pasa el tope.
${[1, 2, 3, 4, 5, 6].map((n) => `
insert into public.contacts (id, full_name) values ('${ids[`zoe${n}`]}', 'Zoe Numero${n}');
insert into public.contact_opt_out_events (contact_id, channel, purpose, source, canonical_account_id, canonical_inbox_id,
  canonical_conversation_id, canonical_message_id, external_user_id, occurred_at, normalized_rule_key, correlation_status,
  projection_status)
values ('${ids[`zoe${n}`]}', 'whatsapp', 'cart_recovery', 'chatwoot', 2, 11, ${700 + n}, ${5000 + n}, '1', '${t(1)}',
        'stop_receiving_messages', 'applied', 'applied');`).join('\n')}

-- Una conversacion de A en la misma cuenta de Chatwoot que nada ata a ATT1.
insert into public.channel_identities (id, contact_id, channel, account_id, external_user_id, external_conversation_id, identity_status)
values ('${ids.identityA2}', '${ids.contactA}', 'whatsapp', 'chatwoot:2', null, '777', 'active');
insert into public.conversations (id, contact_id, channel_identity_id, status, automation_status, human_takeover, commercial_context)
values ('${ids.convA2}', '${ids.contactA}', '${ids.identityA2}', 'active', 'enabled', false, '{"chatwoot_conversation_id": "777"}');
-- Un nombre con espacios duros, atado a ATT1 por una baja.
insert into public.contacts (id, full_name) values ('${ids.contactN}', E'Nora\u00a0CANARYNBSP\u00a0Lopez');
insert into public.contact_opt_out_events (contact_id, channel, purpose, source, canonical_account_id, canonical_inbox_id,
  canonical_conversation_id, canonical_message_id, external_user_id, occurred_at, normalized_rule_key, correlation_status,
  projection_status)
values ('${ids.contactN}', 'whatsapp', 'cart_recovery', 'chatwoot', 2, 11, 708, 5008, '1', '${t(1)}',
        'stop_receiving_messages', 'applied', 'applied');

-- El binding comercial activo de ATT1: sin el, el primer contacto no sabe si
-- otro flujo le gana (_portable_precheckout_stop_reason corta antes).
insert into public.commercial_ally_runtime_bindings (tenant_ref, funnel_ref, binding_version, status, ally_ref,
  lead_ally_name, lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink, product_name,
  product_price, currency, offer_code, consent_copy_version, hotmart_product_id, chatwoot_account_id,
  chatwoot_inbox_id, inbound_scope_key, inbound_scope_version, additional_offer_codes, additional_offer_landings)
values ('lancemos', 'att1', 1, 'active', 'att1', 'CANARY_ALLY', 'CANARY_SITE', 'ads-a', 'canary.example.test', '/',
        'CANARYHOT', 'CANARY_PRODUCT', 1, 'MXN', '2uafw5bg', 'v1', 5071808, 2, 11, 'att1-inbound', 1, '{}', '[]');

-- El caso B del contrato: el carrito de B llega despues de su formulario y
-- abre un caso, con el primer contacto todavia esperando.
insert into public.webhook_events (id, source, external_event_id, event_type, payload, processing_status, received_at)
values ('${ids.cartB5}', 'hotmart', 'cart-b5', 'PURCHASE_OUT_OF_SHOPPING_CART', '${cart('5071808')}', 'processed', '${at(-10)}');
insert into public.recovery_cases (id, contact_id, conversation_id, abandonment_event_id, source, external_product_id, product_name,
  status, grace_expires_at, context, policy_key, policy_version, commercial_case_id, hotmart_purchase_intent_id, created_at)
values ('${ids.caseCartB5}', '${ids.contactB}', '${ids.convB}', '${ids.cartB5}', 'hotmart', '5071808', 'x', 'active',
        '${at(-10)}', '{}', 'att1-recuperacion-un-toque', 1, '${ids.caseCartB5}', '${ids.intentB}', '${at(-9)}');
insert into public.pilot_recovery_case_bindings (recovery_case_id, scope_key, scope_version, source_event_id, bound_at, audience_mode,
  audience_purchase_intent_id, audience_precheckout_submission_id)
values ('${ids.caseCartB5}', 'att1-recuperacion', 5, '${ids.cartB5}', '${at(-9)}', 'consented_intent', '${ids.intentB}', '${ids.submissionB}');
insert into public.recovery_case_events (recovery_case_id, webhook_event_id, event_role, observed_at)
values ('${ids.caseCartB5}', '${ids.cartB5}', 'cart_abandonment', '${at(-10)}');

-- Un carrito de C rechazado entre la activacion de la v5 y su arranque.
insert into public.webhook_events (id, source, external_event_id, event_type, payload, processing_status, processing_error, received_at)
values ('${ids.rejectC2}', 'hotmart', 'rej-c2', 'PURCHASE_OUT_OF_SHOPPING_CART', '${cart('5071808')}', 'failed',
        'pilot_scope_rejected:pilot_runtime_not_armed', '${t(-2 * 24 * 60 + 0.5)}');
insert into public.hotmart_purchase_intent_event_identities (webhook_event_id, normalized_email, normalized_phone)
values ('${ids.rejectC2}', 'ceci.canary@example.test', null);

-- Lead F (ATT1): las cuatro ramas del rechazo al arrancar, la toma humana y las
-- ramas que el resto de los leads no tiene.
insert into public.pilot_scope_versions (
  scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id, channel,
  channel_provider, channel_account_ref, source, source_event_type, external_product_id,
  offer_code, purpose, policy_key, policy_version, timezone, max_cohort_contacts,
  max_outbound_request_starts_total, max_outbound_request_starts_per_day, approved_by,
  approved_at, published_at, additional_source_event_types, audience_mode
) values
  ('att1-prueba-total', 1, 'published', 'lancemos', 2, 11, 'whatsapp', 'chatwoot', 'chatwoot:2', 'hotmart',
   'PURCHASE_OUT_OF_SHOPPING_CART', '5071808', '2uafw5bg', 'cart_recovery', 'att1-recuperacion-un-toque', 1,
   'America/Mexico_City', 3, 2, 2, 'CANARY_APPROVER', now(), now(), '{}', 'consented_intent'),
  ('att1-prueba-dia', 1, 'published', 'lancemos', 2, 11, 'whatsapp', 'chatwoot', 'chatwoot:2', 'hotmart',
   'PURCHASE_OUT_OF_SHOPPING_CART', '5071808', '2uafw5bg', 'cart_recovery', 'att1-recuperacion-un-toque', 1,
   'America/Mexico_City', 3, 10, 1, 'CANARY_APPROVER', now(), now(), '{}', 'consented_intent');
insert into public.contacts (id, full_name, email) values ('${ids.contactF}', 'Fede CANARYF Ruiz', 'fede.canary@example.test');
insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
values ('${ids.contactF}', 'email', 'fede.canary@example.test', 'fede.canary@example.test', 'system');
insert into public.contact_authorizations (contact_id, channel, purpose, authorization_status, authorization_source, evidence,
  valid_from, recorded_at)
values ('${ids.contactF}', 'whatsapp', 'cart_recovery', 'allowed', 'system', '{"c": "CANARY_EVIDENCE"}', '${t(-12 * 24 * 60)}',
        '${t(-12 * 24 * 60)}');
insert into public.channel_identities (id, contact_id, channel, account_id, external_user_id, external_conversation_id, identity_status)
values ('${ids.identityF}', '${ids.contactF}', 'whatsapp', 'chatwoot:2', null, '505', 'active');
insert into public.conversations (id, contact_id, channel_identity_id, status, automation_status, human_takeover, commercial_context)
values ('${ids.convF}', '${ids.contactF}', '${ids.identityF}', 'paused_human', 'paused', true, '{"chatwoot_conversation_id": "505"}');
insert into public.webhook_events (id, source, external_event_id, event_type, payload, processing_status, received_at)
values ${['Paused', 'Total', 'Day', 'None', 'Live', 'Optout'].map((name, index) =>
  `('${ids[`cartF${name}`]}', 'hotmart', 'cart-f-${name}', 'PURCHASE_OUT_OF_SHOPPING_CART', '${cart('5071808')}', 'processed', '${t(-12 * 24 * 60 + index)}')`).join(',\n       ')};
insert into public.recovery_cases (id, contact_id, conversation_id, abandonment_event_id, source, external_product_id, product_name,
  status, grace_expires_at, context, policy_key, policy_version, commercial_case_id, created_at)
values ${['Paused', 'Total', 'Day', 'None', 'Live', 'Optout'].map((name, index) =>
  `('${ids[`caseF${name}`]}', '${ids.contactF}', '${ids.convF}', '${ids[`cartF${name}`]}', 'hotmart', '5071808', 'x', 'active', '${t(-12 * 24 * 60 + index)}', '{}', 'att1-recuperacion-un-toque', 1, '${ids[`caseF${name}`]}', '${t(-12 * 24 * 60 + index)}')`).join(',\n       ')};
insert into public.pilot_recovery_case_bindings (recovery_case_id, scope_key, scope_version, source_event_id, bound_at, audience_mode)
values ('${ids.caseFPaused}', 'att1-recuperacion', 5, '${ids.cartFPaused}', '${t(-12 * 24 * 60)}', 'manual_cohort'),
       ('${ids.caseFTotal}', 'att1-prueba-total', 1, '${ids.cartFTotal}', '${t(-12 * 24 * 60)}', 'manual_cohort'),
       ('${ids.caseFDay}', 'att1-prueba-dia', 1, '${ids.cartFDay}', '${t(-12 * 24 * 60)}', 'manual_cohort'),
       ('${ids.caseFNone}', 'att1-recuperacion', 5, '${ids.cartFNone}', '${t(-12 * 24 * 60)}', 'manual_cohort'),
       ('${ids.caseFLive}', 'att1-recuperacion', 5, '${ids.cartFLive}', '${t(-12 * 24 * 60)}', 'manual_cohort'),
       ('${ids.caseFOptout}', 'att1-recuperacion', 5, '${ids.cartFOptout}', '${t(-12 * 24 * 60)}', 'manual_cohort');
insert into public.scheduled_actions (id, recovery_case_id, followup_sequence_id, conversation_id, action_type, status, due_at, expires_at,
  expected_case_version, idempotency_key, policy_key, policy_version, step_key, anchor_type, anchor_subject_internal_id,
  anchor_observed_at, terminal_reason, created_at, updated_at)
values ${[
  ['Paused', 'expired', 'expired_after_worker_crash'],
  ['Total', 'expired', 'expired_after_worker_crash'],
  ['Day', 'expired', 'expired_after_worker_crash'],
  ['None', 'expired', 'expired_after_worker_crash'],
  ['Optout', 'cancelled', 'contact_opted_out'],
].map(([name, status, terminal], index) =>
  `('${ids[`actionF${name}`]}', '${ids[`caseF${name}`]}', '${ids[`seqF${name}`]}', '${ids.convF}', 'first_contact_review', '${status}', '${t(-12 * 24 * 60)}', '${t(30 * 24 * 60)}', 1, 'CANARY-idem-f-${index}', 'att1-recuperacion-un-toque', 1, 'first_contact', 'cart_abandonment', '${ids[`cartF${name}`]}', '${t(-12 * 24 * 60)}', '${terminal}', '${t(-12 * 24 * 60)}', '${t(-12 * 24 * 60 + 1)}')`).join(',\n       ')},
       ('${ids.actionFLive}', '${ids.caseFLive}', '${ids.seqFLive}', '${ids.convF}', 'first_contact_review', 'pending', '${at(60)}', '${at(24 * 60)}',
        1, 'CANARY-idem-f-live', 'att1-recuperacion-un-toque', 1, 'first_contact', 'cart_abandonment', '${ids.cartFLive}',
        '${t(-12 * 24 * 60)}', null, '${t(-12 * 24 * 60)}', '${t(-12 * 24 * 60)}');
-- Los topes de los dos scopes de prueba, gastados antes de los intentos.
insert into public.pilot_outbound_request_authorizations (scope_key, scope_version, action_id, attempt_id, contact_id,
  local_budget_date, runtime_generation, reason_code, authorized_at)
values ('att1-prueba-total', 1, '${ids.otherActionT1}', '${ids.otherAttemptT1}', '${ids.contactD}',
        ('${t(-12 * 24 * 60)}'::timestamptz at time zone 'America/Mexico_City')::date, 1, 'pilot_request_start_authorized', '${t(-12 * 24 * 60)}'),
       ('att1-prueba-total', 1, '${ids.otherActionT2}', '${ids.otherAttemptT2}', '${ids.contactD}',
        ('${t(-12 * 24 * 60)}'::timestamptz at time zone 'America/Mexico_City')::date, 1, 'pilot_request_start_authorized', '${t(-12 * 24 * 60)}'),
       ('att1-prueba-dia', 1, '${ids.otherActionD1}', '${ids.otherAttemptD1}', '${ids.contactD}',
        ('${t(-12 * 24 * 60)}'::timestamptz at time zone 'America/Mexico_City')::date, 1, 'pilot_request_start_authorized', '${t(-12 * 24 * 60)}');
insert into public.followup_delivery_attempts (action_id, idempotency_key, attempt_number, channel, mode, phase, started_at,
  request_started_at, lease_generation, expected_case_version, expected_sequence_revision, created_at)
values ('${ids.actionFPaused}', 'CANARY-f-p1', 1, 'whatsapp', 'approved_template', 'reserved', '${t(6)}', null, 1, 1, 1, '${t(6)}'),
       ('${ids.actionFPaused}', 'CANARY-f-p2', 2, 'whatsapp', 'approved_template', 'reserved', '${t(7)}', null, 2, 1, 1, '${t(7)}'),
       ('${ids.actionFTotal}', 'CANARY-f-t1', 1, 'whatsapp', 'approved_template', 'reserved', '${t(-12 * 24 * 60 + 1)}', null, 1, 1, 1, '${t(-12 * 24 * 60 + 1)}'),
       ('${ids.actionFDay}', 'CANARY-f-d1', 1, 'whatsapp', 'approved_template', 'reserved', '${t(-12 * 24 * 60 + 1)}', null, 1, 1, 1, '${t(-12 * 24 * 60 + 1)}'),
       ('${ids.actionFNone}', 'CANARY-f-n1', 1, 'whatsapp', 'approved_template', 'reserved', '${t(16)}', null, 1, 1, 1, '${t(16)}'),
       ('${ids.actionFNone}', 'CANARY-f-n2', 2, 'whatsapp', 'approved_template', 'request_started', '${t(17)}', '${t(17)}', 2, 1, 1, '${t(17)}');
insert into public.conversation_events (recovery_case_id, related_action_id, event_type, actor_type, data, created_at)
values ('${ids.caseFPaused}', null, 'payment_failure_aggregated', 'integration',
        '{"reason_code": "repeated_payment_failure", "policy_key": "CANARY_POLICY"}', '${t(8)}');
insert into public.commercial_cases (id, case_kind, contact_id, selected_channel_identity_id, conversation_id, status, automation_status,
  authority_mode, inbound_scope_key, inbound_scope_version, tenant_ref, product_ref, offer_ref)
values ('${ids.commercialF}', 'inbound_sales', '${ids.contactF}', '${ids.identityF}', '${ids.convF}', 'paused', 'paused', 'shadow',
        'att1-inbound', 1, 'lancemos', 'CANARYREF', 'CANARYOFFER');
insert into public.conversation_reactivation_events (conversation_id, commercial_case_id, external_conversation_id, command_key,
  reason_code, status, template_name, template_language, last_inbound_message_id, inbound_age_seconds, previous_conversation_status,
  previous_conversation_automation_status, previous_human_takeover, provider_message_id, failure_reason, created_at, settled_at)
values ('${ids.convF}', '${ids.commercialF}', 505, 'reactivation:505', 'outside_service_window', 'sent', 'CANARY_TEMPLATE', 'es_MX',
        900, 90000, 'active', 'enabled', false, 3201, null, '${t(9)}', '${t(9)}');
insert into public.conversation_resume_events (conversation_id, commercial_case_id, external_conversation_id, command_key, reason_code,
  previous_conversation_status, previous_conversation_automation_status, previous_human_takeover, previous_case_status,
  previous_case_automation_status, created_at)
values ('${ids.convF}', '${ids.commercialF}', 505, 'resume:505', 'operator_request', 'paused_human', 'paused', true, 'paused',
        'paused', '${t(10)}');
-- Un formulario de F sin contacto (no se planifico antes de crearlo): se ata por identidad.
insert into public.purchase_intents (id, tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref, normalized_email,
  submitted_at, lifecycle_state, whatsapp_contact_authorized, provisional, provider_observed, activation_authorized)
values ('${ids.intentF}', 'lancemos', 'att1', 'l-5215512345678', '5071808', '2uafw5bg', 'fede.canary@example.test',
        '${t(-13 * 24 * 60)}', 'waiting_for_purchase', true, false, true, false);
insert into public.portable_precheckout_first_contact_plans (submission_id, purchase_intent_id, contact_id, recovery_case_id, scope_key,
  scope_version, outcome, reason_code, created_at)
values ('${ids.submissionF}', '${ids.intentF}', null, null, 'att1-primer-contacto', 2, 'not_planned', 'pilot_runtime_not_armed',
        '${t(-13 * 24 * 60)}');

-- A en la otra instancia: un link, una derivacion y un cupon de la bandeja 12
-- que la historia de ATT1 no puede mostrar.
insert into public.commercial_cases (id, case_kind, contact_id, selected_channel_identity_id, conversation_id, status, automation_status,
  authority_mode, inbound_scope_key, inbound_scope_version, tenant_ref)
values ('${ids.commercialA12}', 'inbound_sales', '${ids.contactA}', '${ids.identityA2}', '${ids.convA2}', 'active', 'draft_only', 'shadow',
        'att2-inbound', 1, 'lancemos');
insert into public.human_handoff_requests (id, conversation_id, command_key, primary_reason_code, requested_by, projection_policy_key,
  projection_policy_version, chatwoot_account_id, chatwoot_inbox_id, external_conversation_id, expected_team_id, note_template_key,
  note_template_version, private_note_body, status, created_at, projected_at, commercial_case_id, inbound_scope_key,
  inbound_scope_version)
values ('${ids.handoffA12}', '${ids.convA2}', 'handoff:777', 'explicit_human_request', 'agent', 'handoff-policy', 1, 2, 12, 777, 7,
        'handoff-note', 1, 'CANARY_NOTE otra bandeja', 'projected', '${t(200)}', '${t(200)}', '${ids.commercialA12}',
        'att2-inbound', 1);
insert into public.conversation_followup_events (id, conversation_id, commercial_case_id, external_conversation_id, command_key, regime,
  status, template_name, template_language, coupon_code, last_inbound_message_id, last_outbound_message_id, inbound_age_seconds,
  checkout_issuance_id, provider_message_id, failure_reason, created_at, settled_at)
values ('${ids.couponA12}', '${ids.convA2}', '${ids.commercialA12}', 777, 'coupon:777', 'went_quiet', 'sent', 'att2_cupon', 'es_MX',
        'CANARYCOUPON2', 10, 20, 90000, '${ids.linkA}', 3999, null, '${t(300)}', '${t(300)}');

-- Una persona atada solo a la otra instancia, por una baja de la bandeja 12.
insert into public.contacts (id, full_name) values ('${ids.contactY}', 'Yago CANARYY Soto');
insert into public.contact_opt_out_events (contact_id, channel, purpose, source, canonical_account_id, canonical_inbox_id,
  canonical_conversation_id, canonical_message_id, external_user_id, occurred_at, normalized_rule_key, correlation_status,
  projection_status)
values ('${ids.contactY}', 'whatsapp', 'cart_recovery', 'chatwoot', 2, 12, 901, 9001, '1', '${t(1)}',
        'stop_receiving_messages', 'applied', 'applied');

-- Un contacto cuyo nombre es su telefono, como guarda WhatsApp a veces.
insert into public.contacts (id, full_name) values ('${ids.contactZ}', '+52 1 55 1234 5678');
insert into public.contact_opt_out_events (contact_id, channel, purpose, source, canonical_account_id, canonical_inbox_id,
  canonical_conversation_id, canonical_message_id, external_user_id, occurred_at, normalized_rule_key, correlation_status,
  projection_status)
values ('${ids.contactZ}', 'whatsapp', 'cart_recovery', 'chatwoot', 2, 11, 902, 9002, '1', '${t(1)}',
        'stop_receiving_messages', 'applied', 'applied');

set session_replication_role = origin;
`);

const asRole = async (role, action) => {
  await db.exec(`set role ${role}`);
  try {
    return await action();
  } finally {
    await db.exec('reset role');
  }
};
const readOnly = async (action) => {
  await db.exec('begin transaction read only');
  try {
    return await action();
  } finally {
    await db.exec('rollback');
  }
};
const call = (sql, params) => asRole('service_role', () => readOnly(async () => (
  one((await db.query(sql, params)).rows, sql).result
)));
const flows = (instance) => call(
  'select public.read_automation_expert_flows_v1($1::jsonb) as result', [JSON.stringify(instance)],
);
const find = (instance, conversation, issuance, name) => call(
  'select public.find_automation_expert_lead_v1($1::jsonb, $2, $3, $4) as result',
  [JSON.stringify(instance), conversation, issuance, name],
);
const history = (instance, leadRef) => call(
  'select public.read_automation_expert_lead_history_v1($1::jsonb, $2::uuid) as result',
  [JSON.stringify(instance), leadRef],
);

const counts = async () => one((await db.query(`
  select (select count(*) from public.webhook_events) as events,
         (select count(*) from public.recovery_cases) as cases,
         (select count(*) from public.scheduled_actions) as actions,
         (select count(*) from public.conversation_events) as conversation_events,
         (select count(*) from public.followup_delivery_attempts) as attempts,
         (select count(*) from public.pilot_outbound_request_authorizations) as authorizations,
         (select count(*) from public.checkout_link_issuances) as links,
         (select count(*) from public.contacts) as contacts
`)).rows, 'counts');
const before = await counts();
const outputs = [];

// ---------------------------------------------------------------------------
// 1. Una instancia no ve los leads ni los flujos de otra.
// ---------------------------------------------------------------------------
if ((await history(ATT1, ids.contactX)) !== null) fail('ATT1 read the history of a lead of the other instance');
// A esta en las dos instancias: cada una ve solo lo suyo de esa persona.
const historyAOther = await history(OTRA, ids.contactA);
outputs.push(historyAOther);
const otherIds = new Set(historyAOther.moments.map((moment) => moment.evidence.id));
for (const id of [ids.linkA, ids.couponA, ids.handoffA, ids.caseCartA, ids.caseFormA, ids.attemptA]) {
  if (otherIds.has(id)) fail(`the other instance showed an ATT1 item of A: ${id}`);
}
for (const id of [ids.handoffA12, ids.couponA12]) {
  if (!otherIds.has(id)) fail(`the other instance lost its own item of A: ${id}`);
}
if (JSON.stringify(historyAOther.lead.conversations) !== '[777]') {
  fail(`conversations of A in the other instance: ${JSON.stringify(historyAOther.lead.conversations)}`);
}
const historyX = await history(OTRA, ids.contactX);
if (historyX === null) fail('the other instance could not read its own lead');
outputs.push(historyX);
const lookupOther = await find(ATT1, 601, null, null);
if (lookupOther.result !== 'not_found') fail(`ATT1 found the conversation of the other instance: ${JSON.stringify(lookupOther)}`);
const byNameAna = await find(ATT1, null, null, 'ana');
outputs.push(byNameAna);
if (byNameAna.result !== 'found' || byNameAna.candidates.length !== 1
    || byNameAna.candidates[0].lead_ref !== ids.contactA) {
  fail(`the name lookup crossed instances: ${JSON.stringify(byNameAna)}`);
}
const byNameOther = await find(OTRA, null, null, 'ana');
// En la otra instancia hay dos Anas: X, y A con su conversacion de esa bandeja.
const anasOther = Object.fromEntries(byNameOther.candidates.map((candidate) => [candidate.lead_ref, candidate]));
if (byNameOther.result !== 'ambiguous' || !anasOther[ids.contactX]
    || JSON.stringify(anasOther[ids.contactA]?.conversations) !== '[777]') {
  fail(`the other instance and its two Anas: ${JSON.stringify(byNameOther)}`);
}
const flowsAtt1 = await flows(ATT1);
const flowsOther = await flows(OTRA);
outputs.push(flowsAtt1, flowsOther);
const boundAtt1 = flowsAtt1.scopes.filter((scope) => scope.bound_to_instance).map((scope) => scope.scope_key).sort();
if (JSON.stringify(boundAtt1) !== JSON.stringify(['att1-primer-contacto', 'att1-recuperacion'])) {
  fail(`ATT1 scopes: ${JSON.stringify(flowsAtt1.scopes)}`);
}
const crossed = await flows({ ...OTRA, scopes: [{ flow_group: 'recuperacion', key: 'att1-recuperacion', version: 5 }] });
const crossedScope = crossed.scopes[0];
if (crossedScope.bound_to_instance !== false || crossedScope.status !== null || crossedScope.starts_total !== 0
    || crossedScope.starts_today !== 0 || crossedScope.version_activated_at !== null
    || crossedScope.local_budget_date !== null || crossedScope.runtime_state !== null) {
  fail(`the other instance read an ATT1 scope: ${JSON.stringify(crossedScope)}`);
}
// Cada instancia cuenta su propio cupon: A tiene uno en cada una.
if (flowsOther.observed.descuento.sent !== 1 || flowsAtt1.observed.descuento.sent !== 1
    || flowsOther.observed.flows.precheckout.triggers !== 0) {
  fail(`the other instance observed ATT1: ${JSON.stringify(flowsOther.observed)}`);
}
if ((await history(ATT1, ids.contactY)) !== null) fail('ATT1 read a lead anchored only by an opt-out of inbox 12');
if ((await find(ATT1, null, null, 'yago')).result !== 'not_found') fail('ATT1 found a lead of inbox 12 by name');
if (flowsAtt1.events_without_flow.length !== 1 || flowsAtt1.events_without_flow[0].count_window !== 1
    || flowsAtt1.events_without_flow[0].event_type !== 'PURCHASE_BILLET_PRINTED') {
  fail(`ATT1 events without flow: ${JSON.stringify(flowsAtt1.events_without_flow)}`);
}

// ---------------------------------------------------------------------------
// Los flujos: el consumo y lo observado.
// ---------------------------------------------------------------------------
const recovery = flowsAtt1.scopes.find((scope) => scope.scope_key === 'att1-recuperacion');
if (recovery.version !== 5 || recovery.runtime_state !== 'armed' || recovery.max_outbound_request_starts_per_day !== 80
    || recovery.starts_total !== 1 || recovery.policy_key !== 'att1-recuperacion-un-toque') {
  fail(`recovery scope: ${JSON.stringify(recovery)}`);
}
const observedCart = flowsAtt1.observed.flows.carrito;
if (observedCart.accepted_by_chatwoot !== 1 || observedCart.starts !== 1 || observedCart.deferred_by_hours !== 1
    || observedCart.median_trigger_to_accept_seconds !== 300
    || observedCart.rejected_on_entry['pilot_scope_rejected:pilot_runtime_not_armed'] !== 3
    || observedCart.rejected_on_entry.invalid_reason_code !== 1) {
  fail(`observed cart: ${JSON.stringify(observedCart)}`);
}
if (flowsAtt1.observed.flows.precheckout.superseded !== 1
    || flowsAtt1.observed.flows.precheckout.median_form_to_winning_event_seconds !== 41 * 60) {
  fail(`observed precheckout: ${JSON.stringify(flowsAtt1.observed.flows.precheckout)}`);
}
if (flowsAtt1.observed.descuento.sent !== 1) fail(`observed coupon: ${JSON.stringify(flowsAtt1.observed.descuento)}`);
if (flowsAtt1.proactive_contact_cap.configured !== false) fail('ATT1 has no proactive contact cap row');

// ---------------------------------------------------------------------------
// La busqueda por las tres llaves y el tope de candidatos.
// ---------------------------------------------------------------------------
const byConversation = await find(ATT1, 501, null, null);
const byIssuance = await find(ATT1, null, '01JABCDEFGHJKMNPQRSTVWXYZ0', null);
const byFullName = await find(ATT1, null, null, 'Ána  canarysurname pérez');
outputs.push(byConversation, byIssuance, byFullName);
for (const [label, lookup] of [['conversation', byConversation], ['issuance', byIssuance], ['full name', byFullName]]) {
  if (lookup.result !== 'found' || lookup.candidates[0].lead_ref !== ids.contactA) {
    fail(`lookup by ${label}: ${JSON.stringify(lookup)}`);
  }
}
const candidateA = byConversation.candidates[0];
if (candidateA.first_name_inferred !== 'Ana' || candidateA.first_name_fallback !== 'Ana'
    || JSON.stringify(candidateA.conversations) !== '[501]') {
  fail(`candidate A: ${JSON.stringify(candidateA)}`);
}
// Un prefijo recorre solo la primera palabra: el apellido se pide entero.
const partialSurname = await find(ATT1, null, null, 'ana canary');
if (partialSurname.result !== 'not_found') fail(`a surname prefix found a lead: ${JSON.stringify(partialSurname)}`);
const wholeWords = await find(ATT1, null, null, 'ana canarysurname');
if (wholeWords.result !== 'found' || wholeWords.candidates[0].lead_ref !== ids.contactA) {
  fail(`whole words from the start: ${JSON.stringify(wholeWords)}`);
}
// Una conversacion que nada ata a la instancia es como una que no existe,
// aunque su contacto si este atado.
const unbound = await find(ATT1, 777, null, null);
if (unbound.result !== 'not_found') fail(`an unbound conversation found its contact: ${JSON.stringify(unbound)}`);
if (JSON.stringify(byConversation.candidates[0].conversations) !== '[501]') {
  fail(`the unbound conversation leaked into the list: ${JSON.stringify(byConversation.candidates[0])}`);
}
// El espacio duro cuenta como espacio: el respaldo es la primera palabra.
const nora = await find(ATT1, null, null, 'nora');
outputs.push(nora);
if (nora.result !== 'found' || nora.candidates[0].first_name_fallback !== 'Nora') {
  fail(`a name with hard spaces: ${JSON.stringify(nora)}`);
}
const zoes = await find(ATT1, null, null, 'zoe');
if (zoes.result !== 'too_many_candidates' || zoes.candidates.length !== 0) {
  fail(`six candidates must not be listed: ${JSON.stringify(zoes)}`);
}
for (const [label, args] of [
  ['two keys', [501, null, 'ana']],
  ['no key', [null, null, null]],
  ['short name', [null, null, 'an']],
  ['a percent sign', [null, null, 'an%']],
  ['an underscore', [null, null, 'a_a']],
  ['a url', [null, 'https://pay.example/x', null]],
  ['a huge conversation', [10_000_000, null, null]],
]) {
  let caught = null;
  try {
    await find(ATT1, ...args);
  } catch (error) {
    caught = error;
  }
  if (caught?.code !== '22023' || !/automation_expert_invalid_lookup/.test(caught.message)) {
    fail(`lookup with ${label}: expected invalid_lookup, got ${caught?.code} ${caught?.message}`);
  }
}

// ---------------------------------------------------------------------------
// La historia del lead A: el caso A del contrato, con las horas medidas.
// ---------------------------------------------------------------------------
const historyA = await history(ATT1, ids.contactA);
outputs.push(historyA);
const kinds = (h) => h.moments.map((moment) => moment.kind);
const kindsA = kinds(historyA);
for (const kind of [
  'form_received', 'first_contact_planned', 'entered_flow', 'hotmart_event', 'scheduled', 'deferred_by_hours',
  'start_authorized', 'accepted_by_chatwoot', 'stopped', 'replied_and_adopted', 'agent_replied', 'link_issued',
  'coupon_sent', 'handed_off', 'handoff_attended', 'purchased', 'closed',
]) {
  if (!kindsA.includes(kind)) fail(`history A lacks ${kind}: ${kindsA.join(',')}`);
}
const stoppedAll = historyA.moments.filter((moment) => moment.kind === 'stopped');
if (stoppedAll.length !== 1) fail(`one stop for the first contact of A: ${JSON.stringify(stoppedAll)}`);
const stoppedA = stoppedAll[0];
for (const id of [ids.handoffA12, ids.couponA12]) {
  if (historyA.moments.some((moment) => moment.evidence.id === id)) fail(`ATT1 showed an item of inbox 12: ${id}`);
}
if (stoppedA.reason_code !== 'superseded_by_provider_event' || stoppedA.decision !== 'cancel'
    || stoppedA.flow_key !== 'precheckout' || stoppedA.extra.winner_flow_key !== 'carrito') {
  fail(`the first contact lost to the cart: ${JSON.stringify(stoppedA)}`);
}
const deferredA = historyA.moments.filter((moment) => moment.kind === 'deferred_by_hours');
if (deferredA.length !== 1 || deferredA[0].extra.count !== 2 || deferredA[0].reason_code !== 'business_window_closed') {
  fail(`two deferrals of one step are one moment: ${JSON.stringify(deferredA)}`);
}
const acceptedA = historyA.moments.find((moment) => moment.kind === 'accepted_by_chatwoot');
if (acceptedA.extra.chatwoot_message_id !== 3001
    || acceptedA.flow_version.scope !== 'att1-recuperacion v5'
    || acceptedA.flow_version.policy !== 'att1-recuperacion-un-toque v1') {
  fail(`the accepted template: ${JSON.stringify(acceptedA)}`);
}
const couponA = historyA.moments.find((moment) => moment.kind === 'coupon_sent');
if (couponA.extra.chatwoot_message_id !== 3101 || couponA.reason_code !== 'link_sent_no_purchase') {
  fail(`the coupon: ${JSON.stringify(couponA)}`);
}
const purchasesA = historyA.moments.filter((moment) => moment.kind === 'purchased');
if (purchasesA.length !== 1 || purchasesA[0].extra.link_source !== 'coupon' || purchasesA[0].certainty !== 'stored') {
  fail(`one purchase, with the coupon link (the identity duplicate is dropped): ${JSON.stringify(purchasesA)}`);
}
const turnsA = historyA.moments.filter((moment) => moment.kind === 'agent_replied');
if (turnsA.length !== 2) fail(`the other agent's turn on the same number leaked in: ${turnsA.length}`);
if (historyA.gap_counts.agent_release_not_persisted !== 2) fail(`agent release gap: ${JSON.stringify(historyA.gap_counts)}`);
if (kindsA.filter((kind) => kind === 'followup_action_claimed').length) fail('the claimed lease events leaked');
if (historyA.now.purchased !== true || historyA.now.pending_handoff !== false || historyA.now.live_actions !== 0) {
  fail(`now A: ${JSON.stringify(historyA.now)}`);
}
if (historyA.lead.first_name_inferred !== 'Ana' || JSON.stringify(historyA.lead.conversations) !== '[501]'
    || historyA.lead.identity.has_phone !== true || historyA.lead.identity.has_email !== true) {
  fail(`lead A: ${JSON.stringify(historyA.lead)}`);
}
if (historyA.adopted_conversations.length !== 1 || historyA.adopted_conversations[0].coupon_sent !== true) {
  fail(`adopted conversations A: ${JSON.stringify(historyA.adopted_conversations)}`);
}
for (let index = 1; index < historyA.moments.length; index += 1) {
  if (historyA.moments[index - 1].at > historyA.moments[index].at) fail('the history is not in order');
}

// ---------------------------------------------------------------------------
// 4. El bucle del lease, un momento; y lo que todavia puede pasar.
// ---------------------------------------------------------------------------
const historyB = await history(ATT1, ids.contactB);
outputs.push(historyB);
const rejectedB = historyB.moments.filter((moment) => moment.kind === 'start_rejected');
if (rejectedB.length !== 1 || rejectedB[0].extra.count !== 3
    || rejectedB[0].reason_code !== 'pilot_scope_version_mismatch'
    || rejectedB[0].certainty !== 'inferred'
    || rejectedB[0].flow_version.scope !== 'att1-recuperacion v4') {
  fail(`the lease loop: ${JSON.stringify(rejectedB)}`);
}
if (historyB.moments.some((moment) => moment.kind === 'stopped' && moment.decision === 'execute')) {
  fail('an execute reevaluation leaked');
}
if (!historyB.moments.some((moment) => moment.kind === 'expired' && moment.reason_code === 'expired_after_worker_crash')) {
  fail(`the expiry of B: ${kinds(historyB).join(',')}`);
}
// El caso B: mientras el primer contacto espera, ya se sabe que el carrito le gana.
const expectedB = historyB.upcoming.find((item) => item.flow_key === 'precheckout');
if (!expectedB || expectedB.expected.decision !== 'cancel'
    || expectedB.expected.reason_code !== 'superseded_by_provider_event'
    || expectedB.expected.winner_flow_key !== 'carrito') {
  fail(`upcoming B: ${JSON.stringify(historyB.upcoming)}`);
}
if (historyB.now.live_actions !== 1) fail(`now B: ${JSON.stringify(historyB.now)}`);

// ---------------------------------------------------------------------------
// 5. Identidad: lo que se ata, lo que es hueco, y el codigo que no cumple la forma.
// ---------------------------------------------------------------------------
const historyC = await history(ATT1, ids.contactC);
outputs.push(historyC);
// Entre la activacion de una version (inactive) y su arranque: no es una pausa.
const inactiveC = historyC.moments.find((moment) => moment.evidence.id === ids.rejectC2);
if (!inactiveC || inactiveC.extra.control_state !== 'inactive') {
  fail(`a rejection after a version activation: ${JSON.stringify(inactiveC)}`);
}
const rejectedC = historyC.moments.filter((moment) => moment.kind === 'rejected_on_entry'
  && moment.evidence.id !== ids.rejectC2);
if (rejectedC.length !== 1 || rejectedC[0].reason_code !== 'pilot_runtime_not_armed'
    || rejectedC[0].attached_by !== 'identity' || rejectedC[0].certainty !== 'inferred'
    || rejectedC[0].flow_key !== 'carrito' || rejectedC[0].extra.control_state !== 'paused') {
  fail(`the rejected cart attached by identity: ${JSON.stringify(rejectedC)}`);
}
const badC = historyC.moments.find((moment) => moment.evidence.id === ids.badC);
if (!badC || badC.reason_code !== null || badC.reason_code_rejected !== true) {
  fail(`a reason code with a phone inside: ${JSON.stringify(badC)}`);
}
const purchaseC = historyC.moments.find((moment) => moment.kind === 'purchased');
if (!purchaseC || purchaseC.certainty !== 'inferred' || purchaseC.attached_by !== 'identity') {
  fail(`the purchase without our link: ${JSON.stringify(purchaseC)}`);
}
if (historyC.gap_counts.unattached_hotmart_event !== 1 || historyC.gap_counts.purchase_channel_not_persisted !== 1) {
  fail(`gaps C: ${JSON.stringify(historyC.gap_counts)}`);
}
if (historyC.moments.some((moment) => moment.evidence.id === ids.ambiguousC || moment.evidence.id === ids.otherProductC)) {
  fail('an ambiguous or foreign-product event was attached');
}
if (historyC.now.opted_out !== true || !historyC.moments.some((moment) => moment.kind === 'opted_out')) {
  fail(`the opt-out of C: ${JSON.stringify(historyC.now)}`);
}
if (historyC.lead.identity.opted_out !== true) fail('identity of C must say opted out');

// ---------------------------------------------------------------------------
// Lead F: las ramas del rechazo al arrancar, la toma humana y el resto.
// ---------------------------------------------------------------------------
const historyF = await history(ATT1, ids.contactF);
outputs.push(historyF);
const rejectedF = Object.fromEntries(historyF.moments
  .filter((moment) => moment.kind === 'start_rejected')
  .map((moment) => [moment.evidence.id, moment]));
for (const [action, code, state] of [
  [ids.actionFPaused, 'pilot_runtime_not_armed', 'paused'],
  [ids.actionFTotal, 'pilot_total_budget_exhausted', null],
  [ids.actionFDay, 'pilot_daily_budget_exhausted', null],
  [ids.actionFNone, null, 'armed'],
]) {
  const moment = rejectedF[action];
  if (!moment || moment.reason_code !== code || (state !== null && moment.extra.control_state !== state)) {
    fail(`start rejected ${code}: ${JSON.stringify(moment)}`);
  }
}
if (historyF.gap_counts.start_rejection_not_persisted !== 1) fail(`F gaps: ${JSON.stringify(historyF.gap_counts)}`);
const kindsF = kinds(historyF);
for (const kind of ['merged_into_open_case', 'reactivation', 'resumed', 'delivery_uncertain_or_failed',
                    'first_contact_not_planned', 'form_received']) {
  if (!kindsF.includes(kind)) fail(`history F lacks ${kind}: ${kindsF.join(',')}`);
}
const optoutStop = historyF.moments.find((moment) => moment.kind === 'stopped' && moment.evidence.id === ids.actionFOptout);
if (!optoutStop || optoutStop.reason_code !== 'contact_opted_out') fail(`a cancelled step without a reevaluation: ${JSON.stringify(optoutStop)}`);
const uncertainF = historyF.moments.find((moment) => moment.kind === 'delivery_uncertain_or_failed');
if (uncertainF.reason_code !== 'delivery_unknown') fail(`a stuck send: ${JSON.stringify(uncertainF)}`);
const formF = historyF.moments.find((moment) => moment.kind === 'form_received');
if (formF.certainty !== 'inferred' || formF.attached_by !== 'identity' || formF.extra.landing_ref !== null) {
  fail(`a form attached by identity, with a phone in its landing: ${JSON.stringify(formF)}`);
}
const liveF = historyF.upcoming.find((item) => item.flow_key === 'carrito');
if (!liveF || liveF.expected.decision !== 'pause' || liveF.expected.reason_code !== 'human_takeover_active') {
  fail(`a live cart with the team holding the conversation: ${JSON.stringify(historyF.upcoming)}`);
}
if (historyF.lead.identity.whatsapp_authorized !== true || historyF.now.human_takeover !== true) {
  fail(`now F: ${JSON.stringify(historyF)}`);
}
const historyZ = await history(ATT1, ids.contactZ);
outputs.push(historyZ);
if (historyZ.lead.first_name_fallback !== null) fail(`a name that is a phone: ${JSON.stringify(historyZ.lead)}`);

// ---------------------------------------------------------------------------
// 3. Ningun canario sale de la base.
// ---------------------------------------------------------------------------
const serialized = JSON.stringify(outputs);
for (const canary of CANARIES) {
  if (serialized.includes(canary)) fail(`a private value left the database: ${canary}`);
}

// ---------------------------------------------------------------------------
// 2. Solo lectura, STABLE, y solo service_role.
// ---------------------------------------------------------------------------
const after = await counts();
if (JSON.stringify(after) !== JSON.stringify(before)) {
  fail(`a read changed the database: ${JSON.stringify({ before, after })}`);
}
const signatures = [
  'public.read_automation_expert_flows_v1(jsonb)',
  'public.find_automation_expert_lead_v1(jsonb, bigint, text, text)',
  'public.read_automation_expert_lead_history_v1(jsonb, uuid)',
];
for (const signature of signatures) {
  const shape = one((await db.query(`
    select p.prosecdef as security_definer, p.provolatile as volatility,
           array_to_string(p.proconfig, ',') as config,
           has_function_privilege('service_role', p.oid, 'execute') as service_x,
           has_function_privilege('anon', p.oid, 'execute') as anon_x,
           has_function_privilege('authenticated', p.oid, 'execute') as auth_x
      from pg_proc p where p.oid = to_regprocedure($1)
  `, [signature])).rows, signature);
  if (!shape.security_definer || shape.volatility !== 's'
      || shape.config !== 'search_path=pg_catalog, public, pg_temp'
      || !shape.service_x || shape.anon_x || shape.auth_x) {
    fail(`${signature}: ${JSON.stringify(shape)}`);
  }
}
for (const role of ['anon', 'authenticated']) {
  let denied = null;
  try {
    await asRole(role, () => db.query(
      'select public.read_automation_expert_lead_history_v1($1::jsonb, $2::uuid)',
      [JSON.stringify(ATT1), ids.contactA],
    ));
  } catch (caught) {
    denied = caught;
  }
  if (denied?.code !== '42501') fail(`${role} executed the history: ${denied?.code} ${denied?.message}`);
}
let tableDenied = null;
try {
  await asRole('service_role', () => db.query('select count(*) from public.checkout_link_issuances'));
} catch (caught) {
  tableDenied = caught;
}
if (tableDenied?.code !== '42501') fail('service_role must not read checkout_link_issuances directly');

let invalidInstance = null;
try {
  await flows({ ...ATT1, chatwoot_inbox_id: 'once' });
} catch (caught) {
  invalidInstance = caught;
}
if (invalidInstance?.code !== '22023' || !/automation_expert_invalid_instance/.test(invalidInstance.message)) {
  fail(`an invalid instance must fail closed: ${invalidInstance?.message}`);
}

// Con AUTOMATION_EXPERT_FIXTURE_DIR, guarda la salida real de las funciones:
// son los fixtures con los que se prueba el bridge
// (tests/fixtures/automation_expert/rpc_*.json). Ya pasaron el filtro de los
// canarios de arriba.
if (process.env.AUTOMATION_EXPERT_FIXTURE_DIR) {
  const { writeFileSync } = await import('node:fs');
  const dir = process.env.AUTOMATION_EXPERT_FIXTURE_DIR;
  const captured = {
    origin: 'tests/sql/followup_engine/validate_automation_expert_read.mjs, sobre la cadena completa de migraciones',
    captured_at: new Date(now).toISOString(),
  };
  for (const [name, value] of [
    ['rpc_flows_att1.json', flowsAtt1],
    ['rpc_lookup_conversation_501.json', byConversation],
    ['rpc_lookup_too_many.json', zoes],
    ['rpc_history_lead_a.json', historyA],
    ['rpc_history_lead_b.json', historyB],
    ['rpc_history_lead_c.json', historyC],
    ['rpc_history_lead_f.json', historyF],
  ]) {
    writeFileSync(join(dir, name), `${JSON.stringify({ _captured: captured, result: value }, null, 1)}\n`);
  }
}

console.log(JSON.stringify({
  history_a_moments: historyA.moments.length,
  history_b_moments: historyB.moments.length,
  history_c_moments: historyC.moments.length,
  canaries: CANARIES.length,
}));
console.log('AUTOMATION_EXPERT_READ=OK');
