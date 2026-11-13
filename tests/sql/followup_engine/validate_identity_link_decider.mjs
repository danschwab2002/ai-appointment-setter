// El correlador que decide solo (migraciones 20261114000100 y 20261114000150,
// E-536; contrato docs/contracts/identity-link-decider-v1.md).
//
// Por que: un conflicto de carrito (conflict / email_phone_conflict) dejaba la
// intencion en identity_conflict y el carrito no le escribia a nadie. La pieza
// nueva ata, pregunta o no hace nada, y las cuatro puertas del carrito (el
// planificador del legado, el motor v1, el nativo y el chequeo del permiso)
// aceptan la atadura vigente y nada mas.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (binding,
// formulario 1.1.0 con consentimiento por la admision portable de hoy) y el
// carrito capturado (tests/fixtures/hotmart_cart_abandonment_rejected_v1.json)
// con el mail o el telefono del checkout cambiados: las formas de conflicto
// medidas el 13/11 en Maribel y ATT1 (tests/fixtures/identity_link/
// conflictos-2026-11.json, sin datos personales). Mails y telefonos sinteticos.
//
// Prueba:
//   0. la forma: cambian solo las cuatro funciones, con su marca; las nuevas son
//      de la API solo las de la lista; correrla dos veces aborta; la B cambia
//      solo la lectura de avisos;
//   1. rojo: sin la migracion, el carrito en conflicto lo rechaza el
//      planificador (pilot_audience_intent_unresolved);
//   2. la llave de la base y D8: sin renglon el claim no da nada; un conflicto
//      anterior a enabled_at no entra a la cola;
//   3. ata en vivo (R1): la evidencia trae solo rasgos (barrido sin mails,
//      telefonos ni nombres); el evento failed vuelve a received; el legado
//      planifica, y el motor v1 y el nativo dan la intencion con permiso; el
//      arranque autoriza;
//   4. ata en sombra: decision registrada, evento intacto, el planificador
//      sigue rechazando;
//   5. el destino (R2): el contacto del checkout queda con el telefono del
//      formulario; el plan al telefono del checkout se rechaza
//      (consented_intent_phone_mismatch); al del formulario, planifica;
//   6. revocar: el arranque siguiente corta con el motivo del permiso;
//   7. sin atadura identity_conflict sigue cortando (el chequeo compartido por
//      el carrito, el pago rechazado y el primer contacto);
//   8. una compra posterior con los datos del checkout corta la atadura;
//   9. cada freno con su motivo; la base re-verifica la regla, el rasgo fuerte
//      del modelo y el destino (el +1 nunca es destino; con D3, el del checkout);
//  10. inmutabilidad y ACL de las tablas nuevas; la carrera del reencolado
//      (idempotente, nunca en bucle);
//  11. los avisos al canal (sin datos personales) y su marca;
//  12. la pregunta (bloque B): planificada, la puerta antes de enviar, el envio,
//      la «Sí» ata con decided_by persona, la baja se anota, sin respuesta a las 72 h.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, errorOf, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const A_MIGRATION = '20261114000100_identity_link_decider_v1.sql';
const B_MIGRATION = '20261114000150_identity_link_question_v1.sql';
const CHANGED_A = [
  'plan_lancemos_pilot_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamp with time zone,bigint,bigint,text,text,integer)',
  '_portable_cart_audience_checks(text,integer,uuid,uuid,text,text)',
  '_flow_engine_native_cart_audience(uuid,uuid,text,text,text)',
  '_portable_consented_intent_reason(uuid,uuid,text)',
];
const API_A = [
  'claim_identity_link_cases_v1(text,text,text,integer)',
  'release_identity_link_case_v1(uuid,uuid,bigint,text)',
  'complete_identity_link_decision_v1(uuid,uuid,bigint,text,text,text,text,text,text,text[],text,text)',
  'requeue_identity_linked_events_v1(text,text)',
  'get_identity_link_destination_v1(uuid)',
  'bind_identity_link_contact_v1(uuid,uuid)',
  'read_identity_link_notices_v1(text,text,integer)',
  'mark_identity_link_notice_v1(text,text,text)',
];
const API_B = [
  'expire_identity_link_questions_v1(text,text)',
  'claim_identity_link_question_v1(text,text,text,integer)',
  'start_identity_link_question_v1(uuid,uuid,bigint)',
  'finish_identity_link_question_v1(uuid,uuid,bigint,text,bigint,bigint,text)',
  'record_identity_link_answer_v1(text,text,bigint,text,bigint)',
];
const TABLES = ['identity_link_controls', 'identity_link_cases', 'identity_link_decisions', 'identity_link_effects',
  'identity_link_revocations', 'identity_link_notices', 'identity_link_contact_rebinds', 'identity_link_questions',
  'identity_link_answers'];

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(A_MIGRATION);
if (position < 0 || names[position + 1] !== B_MIGRATION) fail(`faltan las migraciones ${A_MIGRATION} y ${B_MIGRATION}`);
if (names.length !== position + 2) fail('despues de la pregunta no va ninguna otra', names.slice(position + 2));
const readMigration = (name) => readFileSync(join(root, 'supabase/migrations', name), 'utf8')
  .replace(/create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite');

// ---------------------------------------------------------------------------
// 0. La forma de las migraciones.
// ---------------------------------------------------------------------------
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const canExecute = async (db, signature, role) => (await db.query(
  `select has_function_privilege($1, $2, 'EXECUTE') as can`, [role, `public.${signature}`])).rows[0].can;
{
  const db = new PGlite();
  await db.waitReady;
  await db.exec(`
    create role anon nologin;
    create role authenticated nologin;
    create role service_role nologin;
    alter default privileges in schema public grant execute on functions to anon, authenticated;
    alter default privileges in schema public grant all on functions to service_role;
    alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
    grant usage on schema public to service_role, anon;
  `);
  await db.exec(readFileSync(join(root, 'supabase/baseline/20260803_public_schema.sql'), 'utf8')
    .replace(/create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
  for (const name of names.slice(0, position)) await db.exec(readMigration(name));
  const before = await snapshot(db);
  await db.exec(readMigration(A_MIGRATION));
  const afterA = await snapshot(db);
  const changed = [...before.keys()].filter((s) => afterA.has(s) && afterA.get(s).definition !== before.get(s).definition);
  const removed = [...before.keys()].filter((s) => !afterA.has(s));
  const attributes = [...before.keys()].filter((s) => afterA.has(s)
    && ['acl', 'prosecdef', 'provolatile'].some((k) => afterA.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, CHANGED_A) || removed.length || attributes.length) {
    fail('la migracion A cambio otra cosa', { changed, removed, attributes });
  }
  for (const signature of CHANGED_A) {
    if (!afterA.get(signature).definition.includes('identity_link_decider')) fail(`${signature}: sin la marca`);
  }
  const addedA = [...afterA.keys()].filter((s) => !before.has(s));
  for (const signature of addedA) {
    for (const role of ['anon', 'authenticated']) {
      if (await canExecute(db, signature, role)) fail(`${signature}: ${role} no puede ejecutarla`);
    }
    if ((await canExecute(db, signature, 'service_role')) !== API_A.includes(signature)) {
      fail(`${signature}: service_role ${API_A.includes(signature) ? 'tiene' : 'no tiene'} que poder`);
    }
  }
  for (const signature of API_A) if (!addedA.includes(signature)) fail(`falta ${signature}`);
  const again = await errorOf(db, () => db.exec(readMigration(A_MIGRATION)));
  if (again?.code !== '55000') fail('correrla dos veces tiene que abortar', again?.message);
  await db.exec('rollback').catch(() => {});
  await db.exec(readMigration(B_MIGRATION));
  const afterB = await snapshot(db);
  const changedB = [...afterA.keys()].filter((s) => afterB.has(s) && afterB.get(s).definition !== afterA.get(s).definition);
  if (!sameSet(changedB, ['read_identity_link_notices_v1(text,text,integer)'])) fail('la migracion B cambio otra cosa', changedB);
  const addedB = [...afterB.keys()].filter((s) => !afterA.has(s));
  for (const signature of addedB) {
    for (const role of ['anon', 'authenticated']) {
      if (await canExecute(db, signature, role)) fail(`${signature}: ${role} no puede ejecutarla`);
    }
    if ((await canExecute(db, signature, 'service_role')) !== API_B.includes(signature)) {
      fail(`${signature}: service_role ${API_B.includes(signature) ? 'tiene' : 'no tiene'} que poder`);
    }
  }
  for (const table of TABLES) {
    for (const role of ['anon', 'authenticated', 'service_role']) {
      const can = (await db.query(`select has_table_privilege($1, $2, 'SELECT') or has_table_privilege($1, $2, 'INSERT')
        or has_table_privilege($1, $2, 'UPDATE') or has_table_privilege($1, $2, 'DELETE') as can`,
      [role, `public.${table}`])).rows[0].can;
      if (can) fail(`${table}: ${role} no tiene DML directo`);
    }
    const rls = one((await db.query("select relrowsecurity from pg_class where oid = $1::regclass", [`public.${table}`])).rows, table);
    if (!rls.relrowsecurity) fail(`${table}: sin RLS`);
  }
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo, sin las dos migraciones (la prueba en rojo), y los helpers.
// ---------------------------------------------------------------------------
const world = await buildWorld({ skip: [A_MIGRATION, B_MIGRATION] });
const { db, A, PLAN_CART, LEGACY, admitForm, dbNow } = world;
const CART = JSON.parse(readFileSync(join(root, 'tests/fixtures/hotmart_cart_abandonment_rejected_v1.json'), 'utf8')).payload;
const OFFER = A.offers.at(-1) ?? A.offers[0];
const at = (base, minutes) => new Date(base.getTime() + minutes * 60_000);
const q = async (sql, params = []) => (await db.query(sql, params)).rows;
const q1 = async (sql, params, label) => one(await q(sql, params), label);
let serial = 0;
// Un telefono de Mexico (52 + 10) o del formulario con el +1 (1 + los mismos 10).
const mxPhone = () => `52556${String(1000000 + serial * 37).slice(-7)}`;

// La persona: el formulario con sus datos, el carrito con los del checkout.
const conflictLead = async (label, { form = {}, cart = {}, minutesAgo = 30, consent = true } = {}) => {
  serial += 1;
  const phone = form.phone ?? mxPhone();
  const lead = {
    label, product: A, offer: OFFER,
    name: form.name ?? `Compradora Identidad ${label}`,
    email: form.email ?? `identidad-${serial}@example.test`,
    phone, country: phone.startsWith('52') ? '52' : '1', iso: phone.startsWith('52') ? 'MX' : 'US',
  };
  const base = await dbNow();
  await admitForm(lead, at(base, -minutesAgo - 45));
  if (!consent) {
    await q(`update public.precheckout_submissions set canonical_payload = jsonb_set(canonical_payload,
      '{consent,whatsapp_contact}', 'false'::jsonb) where canonical_payload #>> '{identity,email}' = $1`, [lead.email]);
  }
  lead.cartEmail = cart.email ?? lead.email;
  lead.cartPhone = cart.phone ?? lead.phone;
  lead.cartName = cart.name ?? lead.name;
  const payload = structuredClone(CART);
  payload.id = `identidad-cart-${serial}`;
  payload.creation_date = at(base, -minutesAgo).getTime();
  payload.data.product = { id: A.productId, name: A.productName };
  payload.data.offer = { code: OFFER.offer_code };
  payload.data.buyer = { name: lead.cartName, email: lead.cartEmail, phone: lead.cartPhone };
  const admitted = await q1(`select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)`,
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), lead.cartEmail, lead.cartPhone], `${label} carrito`);
  if (admitted.outcome !== 'inserted') fail(`${label} carrito`, admitted.outcome);
  lead.event = admitted.webhook_event_id;
  lead.abandonedAt = at(base, -minutesAgo).toISOString();
  lead.intent = (await q(`select purchase_intent_id from public.hotmart_purchase_intent_correlation_candidates
    where webhook_event_id = $1`, [lead.event]))[0]?.purchase_intent_id ?? null;
  // Lo que hace resolution.py: el contacto por mail o por telefono, o uno nuevo con los datos del checkout.
  const found = await q(`select distinct contact_id from public.contact_points
    where (type = 'email' and normalized_value = $1) or (type = 'phone' and normalized_value = any(public._whatsapp_phone_variants($2)))`,
  [lead.cartEmail, lead.cartPhone]);
  if (found.length > 1) fail(`${label}: contacto ambiguo en la prueba`);
  lead.contact = found[0]?.contact_id ?? (await q1(`insert into public.contacts (full_name, email, phone, country_iso)
    values ($1,$2,$3,'MX') returning id`, [lead.cartName, lead.cartEmail, lead.cartPhone], `${label} contacto`)).id;
  await q(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id)
    values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4) on conflict do nothing`,
  [lead.contact, lead.cartEmail, lead.cartPhone, lead.event]);
  return lead;
};
const correlationOf = async (lead) => q1(`select outcome, reason_code, candidate_count from public.hotmart_purchase_intent_correlations
  where webhook_event_id = $1`, [lead.event], `${lead.label} correlacion`);
const planArgs = (lead, phone = lead.cartPhone) => [lead.event, lead.contact, String(A.productId), A.productName,
  OFFER.offer_code, LEGACY[A.scope], lead.abandonedAt, A.accountId, A.inboxId, phone, A.scope, 1];
const plan = async (lead, phone) => q1(PLAN_CART, planArgs(lead, phone), `${lead.label} plan`);
const planError = async (lead, phone) => errorOf(db, () => db.query(PLAN_CART, planArgs(lead, phone)));
// Lo que hace el bridge hoy con el rechazo: el evento queda failed con el motivo.
const failEvent = async (lead, error = 'pilot_scope_rejected:pilot_audience_intent_unresolved') => q(
  `update public.webhook_events set processing_status = 'failed', processing_error = $2 where id = $1`, [lead.event, error]);
const eventOf = async (lead) => q1('select processing_status, processing_error from public.webhook_events where id = $1',
  [lead.event], `${lead.label} evento`);
const consentOf = async (lead, phone = lead.cartPhone) => (await q1(
  'select reason_code from public._portable_consented_intent_reason($1,$2,$3)', [lead.intent, lead.contact, phone],
  `${lead.label} permiso`)).reason_code;
const motorV1Of = async (lead, phone = lead.cartPhone) => q1(
  'select * from public._portable_cart_audience_checks($1,1,$2,$3,$4,$5)',
  [A.scope, lead.event, lead.contact, OFFER.offer_code, phone], `${lead.label} motor v1`);
const nativeOf = async (lead, phone = lead.cartPhone) => q1(
  'select * from public._flow_engine_native_cart_audience($1,$2,$3,$4,$5)',
  [lead.event, lead.contact, OFFER.offer_code, phone, A.tenant], `${lead.label} nativo`);
const results = {};

// ---------------------------------------------------------------------------
// 1. Rojo: sin la migracion, el carrito en conflicto no planifica.
// ---------------------------------------------------------------------------
const red = await conflictLead('rojo', { cart: { email: 'otra-rojo@example.test' } });
if ((await correlationOf(red)).outcome !== 'conflict') fail('el rojo no quedo en conflicto', await correlationOf(red));
{
  const error = await planError(red);
  if (error?.detail !== 'pilot_audience_intent_unresolved') fail('rojo: el planificador tenia que rechazar', error?.detail);
}
await db.exec(readMigration(A_MIGRATION));
await db.exec(readMigration(B_MIGRATION));
results.rojo_sin_migracion = 'pilot_audience_intent_unresolved';

// ---------------------------------------------------------------------------
// 2. La llave de la base y D8.
// ---------------------------------------------------------------------------
const CLAIM = 'select * from public.claim_identity_link_cases_v1($1,$2,$3,60)';
const claim = async (label) => (await q(CLAIM, [A.tenant, A.funnel, `worker-${label}`]))[0] ?? null;
if (await claim('sin-llave')) fail('sin la llave el claim no devuelve nada');
await q('select public.enable_identity_link_v1($1,$2,$3,$4)', [A.tenant, A.funnel, 'dan', 'prueba del correlador']);
if (await claim('d8')) fail('D8: un conflicto anterior a la llave no entra a la cola');
results.llave_y_d8 = 'ok';

const COMPLETE = 'select public.complete_identity_link_decision_v1($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::text[],$11,$12) as r';
const complete = async (claimed, { mode = 'vivo', decidedBy, branch, guard = null, modelDecision = null,
  confidence = null, codes = [], prompt = null, destination = null }) => (await q1(COMPLETE, [claimed.webhook_event_id,
  claimed.claim_token, claimed.lease_generation, mode, decidedBy, branch, guard, modelDecision, confidence, codes, prompt,
  destination], 'decision')).r;
const claimFor = async (lead) => {
  const claimed = await claim(lead.label);
  if (!claimed || claimed.webhook_event_id !== lead.event) fail(`${lead.label}: el claim tomo otra cosa`, claimed);
  return claimed;
};
// Ningun mail, telefono ni nombre en la evidencia.
const assertNoPersonalData = (lead, evidence) => {
  const text = JSON.stringify(evidence);
  for (const value of [lead.email, lead.cartEmail, lead.phone, lead.cartPhone, lead.phone.slice(-10), lead.cartPhone.slice(-10),
    lead.name, lead.cartName, ...lead.name.split(' ')]) {
    if (value && value.length > 3 && text.includes(value)) fail(`${lead.label}: la evidencia trae un dato personal`);
  }
  if (/@|\d{7,}/.test(text)) fail(`${lead.label}: la evidencia trae algo con forma de mail o telefono`, text);
};

// ---------------------------------------------------------------------------
// 3. Ata en vivo (R1, el mismo telefono y otro mail).
// ---------------------------------------------------------------------------
const r1 = await conflictLead('r1', { cart: { email: 'checkout-r1@example.test' } });
if ((await planError(r1))?.detail !== 'pilot_audience_intent_unresolved') fail('r1: antes de atar se rechaza');
await failEvent(r1);
{
  const claimed = await claimFor(r1);
  assertNoPersonalData(r1, claimed.evidence);
  const traits = claimed.evidence.rasgos;
  if (claimed.evidence.guard_reason !== null || traits.coincide_por !== 'telefono' || !traits.telefono_canonico_igual
      || traits.mail_igual || traits.pais_formulario !== 'MX' || traits.pais_checkout !== 'MX'
      || traits.parecido_nombre !== 100 || !traits.misma_oferta || traits.selector_pais_sospechoso) {
    fail('r1: los rasgos', claimed.evidence);
  }
  if (claimed.evidence_sha256.length !== 64) fail('r1: la huella');
  const decided = await complete(claimed, { decidedBy: 'regla:R1', branch: 'ata', destination: 'intent_phone' });
  if (decided.branch !== 'ata' || decided.effect !== 'requeued') fail('r1: la decision', decided);
}
if ((await eventOf(r1)).processing_status !== 'received' || (await eventOf(r1)).processing_error !== 'identity_link_requeued') {
  fail('r1: el evento vuelve a la cola', await eventOf(r1));
}
if ((await q1('select public._cart_event_intent_v1($1) as i', [r1.event], 'intencion')).i !== r1.intent) fail('r1: la intencion del carrito');
const r1Destination = (await q1('select public.bind_identity_link_contact_v1($1,$2) as d', [r1.event, r1.contact], 'bind')).d;
if (r1Destination !== r1.phone) fail('r1: el destino es el telefono del permiso', r1Destination);
if ((await motorV1Of(r1)).reason_code !== 'pilot_audience_allowed') fail('r1: el motor v1', await motorV1Of(r1));
{
  const native = await nativeOf(r1);
  if (native.reason !== null || native.purchase_intent_id !== r1.intent) fail('r1: el nativo', native);
}
r1.plan = await plan(r1);
{
  const binding = await q1(`select audience_purchase_intent_id from public.pilot_recovery_case_bindings where recovery_case_id = $1`,
    [r1.plan.recovery_case_id], 'binding');
  if (binding.audience_purchase_intent_id !== r1.intent) fail('r1: el caso guarda la intencion de la audiencia', binding);
}
results.ata_vivo_r1 = 'el legado planifica, el motor v1 y el nativo aceptan';

// ---------------------------------------------------------------------------
// 4. Ata en sombra: se registra y no cambia nada.
// ---------------------------------------------------------------------------
const shadow = await conflictLead('sombra', { cart: { email: 'checkout-sombra@example.test' } });
await failEvent(shadow);
{
  const decided = await complete(await claimFor(shadow), { mode: 'sombra', decidedBy: 'regla:R1', branch: 'ata',
    destination: 'intent_phone' });
  if (decided.effect !== 'sombra') fail('sombra: el efecto', decided);
}
if ((await eventOf(shadow)).processing_status !== 'failed') fail('sombra: el evento queda como estaba');
if ((await planError(shadow))?.detail !== 'pilot_audience_intent_unresolved') fail('sombra: el planificador sigue rechazando');
if ((await consentOf(shadow)) !== 'consented_intent_not_live') fail('sombra: el permiso sigue cortando');
results.ata_sombra = 'registrada, sin efecto';

// ---------------------------------------------------------------------------
// 5. El destino (R2: el mismo mail y otro telefono, el nombre igual).
// ---------------------------------------------------------------------------
const r2 = await conflictLead('r2', { cart: { phone: '525598765432' } });
await failEvent(r2);
{
  const claimed = await claimFor(r2);
  const traits = claimed.evidence.rasgos;
  if (traits.coincide_por !== 'mail' || !traits.mail_igual || traits.telefono_canonico_igual || traits.parecido_nombre < 90) {
    fail('r2: los rasgos', traits);
  }
  const wrong = await errorOf(db, () => db.query(COMPLETE, [claimed.webhook_event_id, claimed.claim_token,
    claimed.lease_generation, 'vivo', 'regla:R1', 'ata', null, null, null, [], null, 'intent_phone']));
  if (wrong?.message !== 'identity_link_rule_not_met') fail('r2: R1 sin el telefono igual se rechaza', wrong?.message);
  const decided = await complete(claimed, { decidedBy: 'regla:R2', branch: 'ata', destination: 'intent_phone' });
  if (decided.effect !== 'requeued') fail('r2: la decision', decided);
}
{
  const destination = (await q1('select public.bind_identity_link_contact_v1($1,$2) as d', [r2.event, r2.contact], 'bind')).d;
  if (destination !== r2.phone) fail('r2: el destino es el del formulario', destination);
  const contactPhone = (await q1('select phone from public.contacts where id = $1', [r2.contact], 'contacto')).phone;
  if (contactPhone !== r2.phone) fail('r2: el contacto queda con el telefono del permiso');
  const error = await planError(r2, r2.cartPhone);
  if (error?.detail !== 'pilot_audience_consented_intent_phone_mismatch') fail('r2: al telefono del checkout no', error?.detail);
  r2.plan = await plan(r2, r2.phone);
}
results.destino_r2 = 'al del formulario si, al del checkout consented_intent_phone_mismatch';
{
  // Si alguien cambio el telefono del contacto despues de atar, revocar no lo pisa.
  const changed = await conflictLead('r2-cambiado', { cart: { phone: '525587654321' } });
  await failEvent(changed);
  await complete(await claimFor(changed), { decidedBy: 'regla:R2', branch: 'ata', destination: 'intent_phone' });
  await q('select public.bind_identity_link_contact_v1($1,$2)', [changed.event, changed.contact]);
  await q(`update public.contacts set phone = '525500000001' where id = $1`, [changed.contact]);
  const decision = await q1('select decision_id from public.identity_link_decisions where webhook_event_id = $1',
    [changed.event], 'decision');
  await q('select public.revoke_identity_link_v1($1,$2,$3)', [decision.decision_id, 'auditora', 'prueba']);
  if ((await q1('select phone from public.contacts where id = $1', [changed.contact], 'contacto')).phone !== '525500000001') {
    fail('revocar no pisa un telefono que cambio despues');
  }
  const effects = (await q('select effect from public.identity_link_effects where decision_id = $1', [decision.decision_id]))
    .map((row) => row.effect);
  if (!effects.includes('contacto_sin_restaurar')) fail('el efecto contacto_sin_restaurar', effects);
  results.restaurar = 'restaura el telefono anterior; no pisa uno que cambio despues';
}

// ---------------------------------------------------------------------------
// 6. Revocar: el arranque siguiente corta.
// ---------------------------------------------------------------------------
{
  const decision = await q1(`select decision_id from public.identity_link_decisions where webhook_event_id = $1`, [r2.event], 'decision');
  if (!(await q1('select public.revoke_identity_link_v1($1,$2,$3) as r', [decision.decision_id, 'auditora', 'prueba'], 'revocar')).r) {
    fail('revocar');
  }
  if ((await consentOf(r2, r2.phone)) !== 'consented_intent_not_live') fail('revocada: el permiso vuelve a cortar');
  // El contacto vuelve a su telefono anterior, guardado en el registro de la decision.
  const rebind = await q1(`select previous_phone, bound_phone from public.identity_link_contact_rebinds
    where decision_id = $1`, [decision.decision_id], 'reatado');
  if (rebind.previous_phone !== r2.cartPhone || rebind.bound_phone !== r2.phone) fail('revocada: el registro del telefono anterior');
  if ((await q1('select phone from public.contacts where id = $1', [r2.contact], 'contacto')).phone !== r2.cartPhone) {
    fail('revocada: el contacto vuelve a su telefono anterior');
  }
  const effects = (await q(`select effect from public.identity_link_effects where decision_id = $1`, [decision.decision_id]))
    .map((row) => row.effect);
  if (!effects.includes('contacto_restaurado')) fail('revocada: el efecto contacto_restaurado', effects);
  if ((await q1('select public.revoke_identity_link_v1($1,$2,$3) as r', [decision.decision_id, 'auditora', 'otra vez'], 'x')).r) {
    fail('revocar dos veces no hace nada');
  }
  const reserved = await errorOf(db, async () => {
    const r = await world.reserve(r2, r2.plan.scheduled_action_id);
    await world.startOld(r);
  });
  if (!reserved || !/not_live|cancel|audience/.test(`${reserved.message} ${reserved.detail}`)) {
    fail('revocada: el arranque tenia que cortar', reserved && { message: reserved.message, detail: reserved.detail });
  }
  results.revocar = `${reserved.message}:${reserved.detail}`;
}

// ---------------------------------------------------------------------------
// 7. Sin atadura identity_conflict sigue cortando; 8. la compra corta la atadura.
// ---------------------------------------------------------------------------
if ((await consentOf(red)) !== 'consented_intent_not_live') fail('sin atadura el conflicto sigue cortando', await consentOf(red));
if ((await planError(red))?.detail !== 'pilot_audience_intent_unresolved') fail('sin atadura el planificador sigue rechazando');
results.sin_atadura = 'consented_intent_not_live';
{
  if ((await consentOf(r1, r1.phone)) !== 'consented_intent_ok') fail('r1: antes de la compra el permiso pasa');
  const now = Date.now();
  await q(`insert into public.webhook_events (source, external_event_id, event_type, payload)
    values ('hotmart', $1, 'PURCHASE_APPROVED', $2::jsonb)`, [`identidad-compra-${now}`, JSON.stringify({
    data: { product: { id: A.productId }, purchase: { approved_date: now }, buyer: { email: r1.cartEmail } } })]);
  if ((await consentOf(r1, r1.phone)) !== 'consented_intent_not_live') fail('r1: la compra con el mail del checkout corta');
  results.compra_corta = 'consented_intent_not_live';
}

// ---------------------------------------------------------------------------
// 9. Los frenos y lo que la base re-verifica.
// ---------------------------------------------------------------------------
const guardOf = async (lead) => (await q1('select public.get_identity_link_evidence_v1($1) as e', [lead.event], 'evidencia')).e.guard_reason;
{
  const noConsent = await conflictLead('sin-permiso', { cart: { email: 'checkout-sp@example.test' }, consent: false });
  if ((await guardOf(noConsent)) !== 'sin_permiso') fail('freno sin_permiso', await guardOf(noConsent));
  const old = await conflictLead('viejo', { cart: { email: 'checkout-viejo@example.test' }, minutesAgo: 25 * 60 });
  if ((await guardOf(old)) !== 'carrito_viejo') fail('freno carrito_viejo', await guardOf(old));
  const bought = await conflictLead('compro', { cart: { email: 'checkout-compro@example.test' } });
  await q(`insert into public.webhook_events (source, external_event_id, event_type, payload)
    values ('hotmart', $1, 'PURCHASE_APPROVED', $2::jsonb)`, [`identidad-compra-previa`, JSON.stringify({
    data: { product: { id: A.productId }, purchase: { approved_date: Date.now() }, buyer: { email: bought.email } } })]);
  if ((await guardOf(bought)) !== 'ya_compro') fail('freno ya_compro', await guardOf(bought));
  const optedOut = await conflictLead('baja', { cart: { email: 'checkout-baja@example.test' } });
  await q(`insert into public.contact_opt_out_events (channel, purpose, source, canonical_account_id, canonical_inbox_id,
    canonical_conversation_id, canonical_message_id, external_user_id, occurred_at, normalized_rule_key, correlation_status)
    values ('whatsapp','cart_recovery','chatwoot',$1,$2,99001,99001,$3,now(),'stop_receiving_messages','unmatched')`,
  [A.accountId, A.inboxId, `521${optedOut.phone.slice(2)}`]);
  if ((await guardOf(optedOut)) !== 'baja_previa') fail('freno baja_previa', await guardOf(optedOut));
  const notLive = await conflictLead('no-viva', { cart: { email: 'checkout-nv@example.test' } });
  await q("update public.purchase_intents set current_classification = 'tracking_incomplete' where id = $1", [notLive.intent]);
  if ((await guardOf(notLive)) !== 'intencion_no_viva') fail('freno intencion_no_viva', await guardOf(notLive));
  const ambiguous = await conflictLead('ambiguo', { cart: { email: 'checkout-amb@example.test' } });
  const other = (await q1(`insert into public.contacts (full_name, email, phone) values ('Otra', 'x', null) returning id`, [], 'otra')).id;
  await q(`insert into public.contact_points (contact_id, type, raw_value, normalized_value, source)
    values ($1,'email',$2,$2,'manual')`, [other, ambiguous.cartEmail]);
  if ((await guardOf(ambiguous)) !== 'contacto_ambiguo') fail('freno contacto_ambiguo', await guardOf(ambiguous));
  const resolved = await world.cartFor('resuelto');
  if ((await guardOf(resolved)) !== 'fuera_de_alcance') fail('freno fuera_de_alcance', await guardOf(resolved));
  results.frenos = 'sin_permiso, carrito_viejo, ya_compro, baja_previa, intencion_no_viva, contacto_ambiguo, fuera_de_alcance';
  // Un freno en vivo gana sobre la regla: la decision queda «nada» con su motivo.
  for (const lead of [noConsent, old, bought, optedOut, notLive, ambiguous]) {
    const decided = await complete(await claimFor(lead), { decidedBy: 'regla:R1', branch: 'ata', destination: 'intent_phone' });
    if (decided.branch !== 'nada' || !decided.guard_reason) fail(`${lead.label}: el freno gana`, decided);
  }
}
{
  // El +1 del selector del formulario (D3): el +1 nunca es destino; con D3, el del checkout.
  const ten = `556${String(2000000 + serial).slice(-7)}`;
  const plusOne = await conflictLead('mas-uno', { form: { phone: `1${ten}` }, cart: { phone: `52${ten}` } });
  const claimed = await claimFor(plusOne);
  const traits = claimed.evidence.rasgos;
  if (!traits.selector_pais_sospechoso || traits.pais_formulario !== 'NANP' || traits.pais_checkout !== 'MX'
      || !traits.telefono_10_igual || !traits.mail_igual) fail('+1: los rasgos', traits);
  const toPlusOne = await errorOf(db, () => db.query(COMPLETE, [claimed.webhook_event_id, claimed.claim_token,
    claimed.lease_generation, 'vivo', 'regla:R2', 'ata', null, null, null, [], null, 'intent_phone']));
  if (toPlusOne?.message !== 'identity_link_destination_without_permission') fail('+1: nunca al +1', toPlusOne?.message);
  // El modelo sin rasgo fuerte nunca ata.
  const weak = await errorOf(db, () => db.query(COMPLETE, [claimed.webhook_event_id, claimed.claim_token,
    claimed.lease_generation, 'vivo', 'modelo:prueba', 'ata', null, 'same_person', 'high', ['name_equal'], 'identity-link-v1',
    'intent_national_digits']));
  if (weak?.message !== 'identity_link_model_without_strong_trait') fail('modelo sin rasgo fuerte', weak?.message);
  const decided = await complete(claimed, { decidedBy: 'regla:R2', branch: 'ata', destination: 'intent_national_digits' });
  if (decided.branch !== 'ata') fail('+1 con D3', decided);
  const destination = (await q1('select public.bind_identity_link_contact_v1($1,$2) as d', [plusOne.event, plusOne.contact], 'bind')).d;
  if (destination !== `52${ten}`) fail('+1 con D3: el destino es el del checkout', destination);
  if ((await consentOf(plusOne, `52${ten}`)) !== 'consented_intent_ok') fail('+1 con D3: el permiso pasa al del checkout');
  if ((await consentOf(plusOne, `1${ten}`)) === 'consented_intent_ok') fail('+1 con D3: al +1 no');
  results.mas_uno = 'sin D3 rechazado; con D3 al telefono del checkout';
}

// ---------------------------------------------------------------------------
// 10. Inmutabilidad y la carrera del reencolado.
// ---------------------------------------------------------------------------
{
  const mutate = await errorOf(db, () => db.query("update public.identity_link_decisions set branch = 'nada'"));
  if (mutate?.message !== 'identity_link_append_only') fail('las decisiones son append-only', mutate?.message);
  const remove = await errorOf(db, () => db.query('delete from public.identity_link_effects'));
  if (remove?.message !== 'identity_link_append_only') fail('los efectos son append-only', remove?.message);
  const race = await conflictLead('carrera', { cart: { email: 'checkout-carrera@example.test' } });
  const decided = await complete(await claimFor(race), { decidedBy: 'regla:R1', branch: 'ata', destination: 'intent_phone' });
  if (decided.effect !== 'ya_estaba_en_cola') fail('carrera: el evento seguia en la cola', decided);
  await failEvent(race, 'flow_engine_no_start:no_entra:permiso');
  const REQUEUE = 'select public.requeue_identity_linked_events_v1($1,$2) as n';
  if ((await q1(REQUEUE, [A.tenant, A.funnel], 'requeue')).n !== 1) fail('carrera: se reencola');
  if ((await eventOf(race)).processing_status !== 'received') fail('carrera: el evento vuelve a received');
  await failEvent(race, 'flow_engine_no_start:no_entra:permiso');
  if ((await q1(REQUEUE, [A.tenant, A.funnel], 'requeue')).n !== 0) fail('carrera: nunca en bucle');
  results.carrera = 'una vez';
}

// ---------------------------------------------------------------------------
// 11. Los avisos.
// ---------------------------------------------------------------------------
const NOTICES = 'select * from public.read_identity_link_notices_v1($1,$2,100)';
{
  const notices = await q(NOTICES, [A.tenant, A.funnel]);
  const decisions = notices.filter((row) => row.kind === 'decision');
  const vivoAta = await q(`select decision_id from public.identity_link_decisions where mode = 'vivo' and branch in ('ata','pregunta')`);
  if (decisions.length !== vivoAta.length) fail('avisos: uno por decision vivo que ata o pregunta', { decisions, vivoAta });
  if (notices.some((row) => JSON.stringify({ ...row, notice_key: null, occurred_at: null }).match(/@|\d{7,}/))) {
    fail('avisos: sin datos personales', notices);
  }
  const first = notices[0];
  if (!(await q1('select public.mark_identity_link_notice_v1($1,$2,$3) as m', [A.tenant, A.funnel, first.notice_key], 'marca')).m) {
    fail('avisos: la marca');
  }
  if ((await q(NOTICES, [A.tenant, A.funnel])).some((row) => row.notice_key === first.notice_key)) fail('avisos: lo marcado no vuelve');
  for (const row of await q(NOTICES, [A.tenant, A.funnel])) {
    await q('select public.mark_identity_link_notice_v1($1,$2,$3)', [A.tenant, A.funnel, row.notice_key]);
  }
  results.avisos = `${decisions.length} decisiones`;
}

// ---------------------------------------------------------------------------
// 12. La pregunta (bloque B).
// ---------------------------------------------------------------------------
{
  const asked = await conflictLead('pregunta', { cart: { email: 'checkout-pregunta@example.test' } });
  await failEvent(asked);
  const decided = await complete(await claimFor(asked), { decidedBy: 'modelo:prueba', branch: 'pregunta',
    modelDecision: 'unsure', confidence: 'medium', codes: ['phone_canonical_equal'], prompt: 'identity-link-v1',
    destination: 'intent_phone' });
  if (decided.effect !== 'pregunta_planificada') fail('pregunta: planificada', decided);
  if ((await eventOf(asked)).processing_status !== 'failed') fail('pregunta: el carrito no se encola');
  const QCLAIM = 'select * from public.claim_identity_link_question_v1($1,$2,$3,60)';
  const question = await q1(QCLAIM, [A.tenant, A.funnel, 'worker-pregunta'], 'pregunta');
  if (question.phone !== asked.phone || question.full_name !== asked.name || question.product_name !== A.productName) {
    fail('pregunta: el telefono del permiso, el nombre del formulario y el producto', question);
  }
  const started = (await q1('select public.start_identity_link_question_v1($1,$2,$3) as s',
    [question.decision_id, question.claim_token, question.lease_generation], 'arranque')).s;
  if (started !== 'ok') fail('pregunta: la puerta', started);
  await q('select public.finish_identity_link_question_v1($1,$2,$3,$4,$5,$6,$7)',
    [question.decision_id, question.claim_token, question.lease_generation, 'sent', 777001, 88001, null]);
  const sendNotice = (await q(NOTICES, [A.tenant, A.funnel])).find((row) => row.kind === 'envio');
  if (!sendNotice || sendNotice.outcome !== 'sale' || Number(sendNotice.conversation_id) !== 777001) {
    fail('pregunta: el aviso del envio con el numero de conversacion', sendNotice);
  }
  const ANSWER = 'select public.record_identity_link_answer_v1($1,$2,$3,$4,$5) as r';
  const otherConversation = (await q1(ANSWER, [A.tenant, A.funnel, 777999, 'si', 1], 'otra')).r;
  if (otherConversation.recorded) fail('pregunta: la respuesta de otra conversacion no cuenta');
  const yes = (await q1(ANSWER, [A.tenant, A.funnel, 777001, 'si', 88002], 'si')).r;
  if (!yes.recorded || !yes.person_decision_id) fail('pregunta: la «Sí» ata', yes);
  if ((await q1('select public._cart_event_intent_v1($1) as i', [asked.event], 'intencion')).i !== asked.intent) {
    fail('pregunta: con la «Sí» la intencion queda atada');
  }
  if ((await q1(ANSWER, [A.tenant, A.funnel, 777001, 'baja', 88003], 'dos')).r.recorded) fail('pregunta: una sola respuesta');
  // Otra pregunta: la baja se anota; otra sin respuesta a las 72 h.
  const optOut = await conflictLead('pregunta-baja', { cart: { email: 'checkout-pbaja@example.test' } });
  const silent = await conflictLead('pregunta-silencio', { cart: { email: 'checkout-psil@example.test' } });
  for (const [lead, conversation] of [[optOut, 777002], [silent, 777003]]) {
    await complete(await claimFor(lead), { decidedBy: 'modelo:prueba', branch: 'pregunta', modelDecision: 'unsure',
      confidence: 'medium', codes: [], prompt: 'identity-link-v1', destination: 'intent_phone' });
    const claimed = await q1(QCLAIM, [A.tenant, A.funnel, 'worker-pregunta'], 'pregunta');
    await q('select public.start_identity_link_question_v1($1,$2,$3)', [claimed.decision_id, claimed.claim_token, claimed.lease_generation]);
    await q('select public.finish_identity_link_question_v1($1,$2,$3,$4,$5,$6,$7)',
      [claimed.decision_id, claimed.claim_token, claimed.lease_generation, 'sent', conversation, null, null]);
  }
  const baja = (await q1(ANSWER, [A.tenant, A.funnel, 777002, 'baja', 88004], 'baja')).r;
  if (!baja.recorded || baja.person_decision_id) fail('pregunta: la baja se anota y no ata', baja);
  await q(`update public.identity_link_questions set finished_at = now() - interval '73 hours' where chatwoot_conversation_id = 777003`);
  await q('select public.expire_identity_link_questions_v1($1,$2)', [A.tenant, A.funnel]);
  const answers = await q(`select answer from public.identity_link_answers order by answer`);
  if (JSON.stringify(answers.map((row) => row.answer)) !== JSON.stringify(['baja', 'si', 'sin_respuesta'])) fail('pregunta: las respuestas', answers);
  // La puerta: un freno en vivo cancela la pregunta antes de enviar.
  const late = await conflictLead('pregunta-freno', { cart: { email: 'checkout-pfreno@example.test' } });
  await complete(await claimFor(late), { decidedBy: 'modelo:prueba', branch: 'pregunta', modelDecision: 'unsure',
    confidence: 'medium', codes: [], prompt: 'identity-link-v1', destination: 'intent_phone' });
  const lateClaim = await q1(QCLAIM, [A.tenant, A.funnel, 'worker-pregunta'], 'pregunta');
  await q("update public.purchase_intents set lifecycle_state = 'purchased' where id = $1", [late.intent]);
  const gate = (await q1('select public.start_identity_link_question_v1($1,$2,$3) as s',
    [lateClaim.decision_id, lateClaim.claim_token, lateClaim.lease_generation], 'puerta')).s;
  if (gate !== 'identity_link_intencion_no_viva') fail('pregunta: la puerta corta con un freno vivo', gate);
  results.pregunta = 'planificada, puerta, envio, «Sí» ata, baja anotada, sin_respuesta a las 72 h';
}

console.log(JSON.stringify({ ok: true, results }, null, 2));
