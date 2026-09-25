// El motivo fino de una derivacion inbound tiene que sobrevivir hasta la fila y
// hasta la nota privada que lee una persona en Chatwoot.
//
// El caso que lo origino: 2026-09-24, conversacion 158. El agente decidio
// send_payment_link, la emision devolvio blocked porque el contacto ya habia
// comprado, y el worker compuso payment_link_purchase_already_approved. Ese
// motivo no se persistia en ningun lado: la fila decia 'commercial_exception' y
// la nota decia el mismo texto fijo que reciben todas las derivaciones.
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
  await db.exec(readFileSync(path, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}

const NOTE = 'Derivacion inbound registrada por el bridge.';
await db.exec(`
  insert into public.inbound_commercial_scope_versions (
    scope_key, version, status, tenant_key, chatwoot_account_id,
    chatwoot_inbox_id, external_product_id, offer_code,
    approved_by, approved_at, published_at
  ) values (
    'detail-reason-probe', 1, 'published', 'tenant-probe', 7, 11,
    'product-probe', 'offer-probe', 'schema-probe', now(), now()
  );
  insert into public.human_handoff_projection_policies (
    policy_key, policy_version, scope_key, scope_version,
    inbound_scope_key, inbound_scope_version, expected_team_id,
    note_template_key, note_template_version, private_note_body, active
  ) values (
    'detail-reason-handoff', 1, null, null,
    'detail-reason-probe', 1, 17,
    'handoff-note', 1, '${NOTE}', true
  );
`);

let conversationSeq = 9200;
async function derive(detailReasonCode) {
  conversationSeq += 1;
  const admitted = (await db.query(
    'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)',
    ['detail-reason-probe', 1, conversationSeq, `551199999${conversationSeq}`],
  )).rows[0];
  if (admitted?.outcome !== 'created') {
    throw new Error(`fixture admission failed: ${JSON.stringify(admitted)}`);
  }
  const handoff = (await db.query(`
    select * from public.request_inbound_human_handoff(
      $1::uuid, $2, 'commercial_exception', 'detail-reason-handoff', 1, now(), $3
    )
  `, [
    admitted.commercial_case_id,
    `handoff:detail-probe:${conversationSeq}`,
    detailReasonCode,
  ])).rows[0];
  if (handoff?.outcome !== 'requested') {
    throw new Error(`handoff not requested: ${JSON.stringify(handoff)}`);
  }
  return (await db.query(
    'select * from public.human_handoff_requests where id = $1',
    [handoff.handoff_request_id],
  )).rows[0];
}

// 1. El motivo medido en produccion llega a la fila y a la nota, en castellano.
const known = await derive('payment_link_purchase_already_approved');
if (known.primary_reason_code !== 'commercial_exception') {
  throw new Error('the closed taxonomy must not change');
}
if (known.detail_reason_code !== 'payment_link_purchase_already_approved') {
  throw new Error(`detail lost: ${JSON.stringify(known.detail_reason_code)}`);
}
if (!known.private_note_body.startsWith(NOTE)
    || !known.private_note_body.includes('\n\nMotivo: el contacto ya compro este producto')
    || !known.private_note_body.endsWith('(payment_link_purchase_already_approved).')) {
  throw new Error(`note without a readable reason: ${JSON.stringify(known.private_note_body)}`);
}

// 2. Sin detalle, la nota queda exactamente como antes de esta migracion.
const silent = await derive(null);
if (silent.detail_reason_code !== null || silent.private_note_body !== NOTE) {
  throw new Error(`null detail changed the note: ${JSON.stringify(silent.private_note_body)}`);
}

// 3. Un motivo que todavia no esta en el mapa se muestra crudo. Un codigo feo en
//    la nota es mejor que una derivacion sin motivo.
const unmapped = await derive('payment_link_some_future_outcome');
if (unmapped.detail_reason_code !== 'payment_link_some_future_outcome'
    || !unmapped.private_note_body.endsWith('\n\nMotivo: payment_link_some_future_outcome.')) {
  throw new Error(`unmapped reason was swallowed: ${JSON.stringify(unmapped.private_note_body)}`);
}

// 4. Un motivo mal formado es un error de programacion, no algo que se guarda.
for (const bad of ['Payment_Link', 'payment link', '9_leading_digit', 'x'.repeat(101)]) {
  let rejected = false;
  try {
    await derive(bad);
  } catch (error) {
    rejected = /invalid_inbound_human_handoff_parameters/.test(String(error));
    if (!rejected) throw error;
  }
  if (!rejected) {
    throw new Error(`malformed detail reason accepted: ${bad}`);
  }
}

// 5. El detalle es parte del snapshot inmutable del request.
let immutable = false;
try {
  await db.query(
    'update public.human_handoff_requests set detail_reason_code = $1 where id = $2',
    ['payment_link_disabled', known.id],
  );
} catch (error) {
  immutable = /human_handoff_request_identity_is_immutable/.test(String(error));
  if (!immutable) throw error;
}
if (!immutable) {
  throw new Error('detail_reason_code must be immutable once written');
}

// 6. El control negativo del mapa: una frase inventada no aparece de la nada.
const sentence = (await db.query(
  'select public.inbound_handoff_reason_sentence($1) as phrase',
  ['payment_link_some_future_outcome'],
)).rows[0];
if (sentence.phrase !== null) {
  throw new Error('unknown codes must not resolve to a sentence');
}

// 7. Los motivos que realmente emite el agente tienen frase. Estos tres salen
//    del SOUL que corre en produccion, no de una lista inventada: son los
//    unicos validos para decision="handoff", y son los que mas aparecen.
for (const code of [
  'explicit_human_request',
  'commercial_exception',
  'policy_requires_human',
  'payment_link_requested',
  'direct_medication_guidance',
]) {
  const row = (await db.query(
    'select public.inbound_handoff_reason_sentence($1) as phrase',
    [code],
  )).rows[0];
  if (typeof row.phrase !== 'string' || row.phrase.length < 20) {
    throw new Error(`agent reason without a sentence: ${code}`);
  }
}

// 8. Y llegan enteros hasta la nota, igual que los del worker.
const agentReason = await derive('commercial_exception');
if (agentReason.detail_reason_code !== 'commercial_exception'
    || !agentReason.private_note_body.includes('\n\nMotivo: la persona pidio una condicion comercial')
    || !agentReason.private_note_body.endsWith('(commercial_exception).')) {
  throw new Error(`agent reason not readable in the note: ${JSON.stringify(agentReason.private_note_body)}`);
}

console.log(
  'inbound_handoff_detail_reason=OK',
  'mapped_reason=OK unmapped_reason=OK null_reason=OK',
  'malformed_rejected=4 immutable=OK agent_reasons=5',
);
