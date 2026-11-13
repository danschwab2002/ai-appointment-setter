// El disparo por audiencia y su lista congelada (migracion 20261112220000, E-484;
// contrato docs/contracts/flow-audience-trigger-v1.md).
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena entera):
// los formularios con consentimiento y los carritos capturados pasan por las
// admisiones portables de hoy, que dejan el contacto y la intencion como en
// produccion. El flujo de audiencia se publica por el ciclo del operador; la
// persona que confirma entra por «Entrar con Slack» como en la pagina.
//
// Prueba:
//   0. la forma: dos tablas y sus funciones; las del bridge solo de service_role,
//      las de la pagina solo de flow_change_approver, las internas de nadie; no
//      cambia ninguna otra ni su ACL, y correrla dos veces aborta;
//   1. las lecturas del bridge: el flujo que corre y su audiencia posible (con el
//      formulario en la ventana, la baja marcada, sin quien no dejo formulario);
//   2. fecha: la lista nace confirmada, deja afuera la baja y a quien no dejo el
//      formulario con permiso, y la misma ocurrencia no se arma dos veces;
//   3. el ritmo: suelta a lo sumo por_hora en una hora, la hora siguiente el resto;
//      una liberada que el motor no asento vuelve a darse a los 10 minutos sin
//      contar otra vez; la ocurrencia termina cuando todas quedaron asentadas;
//   4. la repeticion: quien ya salio en otra ocurrencia del flujo hace menos de
//      otra_despues_de queda omitida como repetida;
//   5. manual (D7): nace esperando; la pagina la muestra con cuantas van a salir;
//      sin sesion no confirma, un conteo distinto o sin segundo paso tampoco, el
//      conteo exacto con el segundo paso si (y queda quien confirmo);
//   6. el freno (D8, y a mano desde la pagina): una frenada no se suelta;
//   7. un flujo apagado no materializa; la lectura no trae telefonos;
//   8. sin la migracion la funcion no existe (el rojo).
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261112220000_flow_audience_triggers_v1.sql';
const BRIDGE = [
  'read_flow_audience_flows_v1(jsonb)',
  'read_flow_audience_candidates_v1(jsonb,uuid,integer,integer)',
  'materialize_flow_audience_occurrence_v1(jsonb,uuid,integer,jsonb,jsonb)',
  'brake_flow_audience_occurrence_v1(uuid,text)',
  'release_flow_audience_members_v1(uuid,timestamp with time zone,integer)',
  'settle_flow_audience_member_v1(uuid,uuid,text)',
  'read_flow_audience_occurrences_v1(jsonb,integer)',
];
const PAGE = [
  'approver_read_flow_audience_v1(jsonb,text)',
  'approver_confirm_flow_audience_v1(jsonb,text,uuid,integer,boolean)',
  'approver_brake_flow_audience_v1(jsonb,text,uuid)',
];
const PRIVATE = [
  '_flow_audience_form(jsonb)',
  '_flow_audience_active(jsonb,uuid,integer)',
  '_flow_audience_purchases(text)',
  '_flow_audience_finish_if_done(uuid)',
  '_flow_audience_occurrence_rows(text,text,integer)',
  '_flow_audience_occurrence_of(text,text,uuid)',
];
const results = {};
const sha = (text) => createHash('sha256').update(text).digest('hex');

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
for (const name of names.slice(position + 1)) {
  if (readFileSync(join(root, 'supabase/migrations', name), 'utf8').includes('flow_audience_')) {
    fail(`${name} toca el disparo por audiencia: revisar esta prueba`);
  }
}

// ---------------------------------------------------------------------------
// 0. La forma.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select replace(p.oid::regprocedure::text, 'public.', '') as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('flow_change_approver', p.oid, 'execute') as page_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
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
  await applyFile(db, join(root, 'supabase/baseline/20260803_public_schema.sql'));
  for (const name of names.slice(0, position)) await applyFile(db, join(root, 'supabase/migrations', name));
  const before = await snapshot(db);
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.has(s)
    && ['definition', 'acl', 'prosecdef'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  const added = [...after.keys()].filter((s) => !before.has(s)).sort();
  if (changed.length || JSON.stringify(added) !== JSON.stringify([...BRIDGE, ...PAGE, ...PRIVATE].sort())) {
    fail('la migracion cambio otra cosa', { changed, added });
  }
  for (const signature of BRIDGE) {
    const fn = after.get(signature);
    if (!fn.service_x || fn.page_x || fn.api_x || !fn.prosecdef) fail(`${signature}: solo service_role`, fn);
  }
  for (const signature of PAGE) {
    const fn = after.get(signature);
    if (fn.service_x || !fn.page_x || fn.api_x || !fn.prosecdef) fail(`${signature}: solo la pagina`, fn);
  }
  for (const signature of PRIVATE) {
    const fn = after.get(signature);
    if (fn.service_x || fn.page_x || fn.api_x) fail(`${signature}: la auxiliar no se expone`, fn);
  }
  const tables = (await db.query(`select c.relname,
      has_table_privilege('anon', c.oid, 'select') or has_table_privilege('service_role', c.oid, 'select')
        or has_table_privilege('flow_change_approver', c.oid, 'select') as readable,
      c.relrowsecurity from pg_class c
     where c.relname in ('flow_audience_occurrences', 'flow_audience_members')`)).rows;
  if (tables.length !== 2 || tables.some((t) => t.readable || !t.relrowsecurity)) fail('las tablas, cerradas', tables);
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'flow_audience_triggers_already_applied') fail('la segunda corrida', error.message);
  }
  results.forma = { funciones: added.length, cambiadas: changed.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 con personas reales de la cadena.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, stateItem, cartFor, pauseFlow } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const call = async (text, params) => JSON.parse(one(await sql(`select ${text}::text as r`, params), text).r);
const rolledBack = async (run) => {
  await db.exec('begin');
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};
const ana = await cartFor('audiencia-ana');
const bea = await cartFor('audiencia-bea');
const sinFormulario = await cartFor('audiencia-sin-form', { form: false });
const baja = await cartFor('audiencia-baja');
await sql(`insert into public.contact_authorizations (contact_id, channel, purpose, authorization_status,
    authorization_source, valid_from) values ($1, 'whatsapp', 'cart_recovery', 'denied', 'manual', now() - interval '1 day')`,
[baja.contact]);
const member = (lead) => ({ contact_id: lead.contact, phone: lead.phone });
const everyone = [member(ana), member(bea), member(sinFormulario), member(baja)];

const definitionFor = (dispara, extra = {}) => ({
  esquema: 'setter-flujo/v1', flujo: 'difusion', producto: A.funnel, estado: 'borrador', nombre: 'Recordatorio',
  dispara, para: { es: 'prospecta', formulario_en: '30d' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ plantilla: 'att1_carrito_dudas_01', idioma: 'es_MX', variables: ['nombre', 'producto'] }],
  termina: { si: ['compra'] }, horario: 'siempre',
  topes: { bolsa: 'att1-difusion', por_dia: 'sin_tope', total: 'sin_tope',
    repeticion: { una_por: 'persona', otra_despues_de: '7d' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  ritmo: { por_hora: 2, por_ocurrencia: 10 },
  ...extra,
});
const flowId = await register(A.funnel, 'difusion');
await cycle([versionItem(flowId, A.funnel, 'difusion',
  definitionFor({ fecha: { el: '2026-12-01T10:00', zona: 'America/Mexico_City' } })),
stateItem(flowId, A.funnel, 'difusion', 'prender')], 'difusion por fecha');
const FINGERPRINT = 'a'.repeat(64);
const materialize = (key, members, version = 1, by = undefined) => call(
  'public.materialize_flow_audience_occurrence_v1($1::jsonb, $2::uuid, $3::integer, $4::jsonb, $5::jsonb)',
  [JSON.stringify(INSTANCE), flowId, version,
    JSON.stringify({ key, scheduled_at: '2026-12-01T16:00:00Z', fingerprint: FINGERPRINT, materialized_by: by }),
    JSON.stringify(members)]);
const settle = (occurrence, lead, outcome = 'arranco') => call(
  'public.settle_flow_audience_member_v1($1::uuid, $2::uuid, $3)', [occurrence, lead.contact, outcome]);

// ---------------------------------------------------------------------------
// 1. Las lecturas del bridge.
// ---------------------------------------------------------------------------
const flows = await call('public.read_flow_audience_flows_v1($1::jsonb)', [JSON.stringify(INSTANCE)]);
if (flows.length !== 1 || flows[0].flow_id !== flowId || flows[0].version !== 1
    || flows[0].definition.dispara.fecha === undefined) {
  fail('los flujos de audiencia que corren', flows);
}
const candidates = await call('public.read_flow_audience_candidates_v1($1::jsonb, $2::uuid, 1, 100)',
  [JSON.stringify(INSTANCE), flowId]);
const byContact = Object.fromEntries(candidates.candidates.map((c) => [c.contact_id, c]));
if (!byContact[ana.contact]?.form_at || byContact[ana.contact].opted_out || !byContact[baja.contact]?.opted_out
    || byContact[sinFormulario.contact] || JSON.stringify(byContact[ana.contact].customer_products) !== '[]') {
  fail('la audiencia posible', candidates);
}
results.lecturas = { flujos: flows.length, candidatas: candidates.candidates.length,
  sin_contacto: candidates.sin_contacto };
// Una compra aprobada de ana (la admision portable de hoy) la vuelve clienta del producto.
const asCustomer = await rolledBack(async () => {
  const payload = { id: 'audiencia-compra-ana', creation_date: Date.now(), event: 'PURCHASE_APPROVED', version: '2.0.0',
    data: { buyer: { name: ana.name, email: ana.email, checkout_phone: `+${ana.phone}` },
      product: { id: A.productId, name: A.productName },
      purchase: { transaction: 'HPAUDIENCIA001', status: 'APPROVED', approved_date: Date.now(),
        offer: { code: ana.offer.offer_code } } } };
  await sql('select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [A.tenant, A.funnel, A.bindingVersion, payload.id, JSON.stringify(payload), ana.email, ana.phone]);
  const read = await call('public.read_flow_audience_candidates_v1($1::jsonb, $2::uuid, 1, 100)',
    [JSON.stringify(INSTANCE), flowId]);
  return read.candidates.find((c) => c.contact_id === ana.contact)?.customer_products;
});
if (JSON.stringify(asCustomer) !== JSON.stringify([A.funnel])) fail('la compra la vuelve clienta', asCustomer);
results.lecturas.clienta = asCustomer;

// ---------------------------------------------------------------------------
// 2. fecha: la lista congelada.
// ---------------------------------------------------------------------------
const first = await materialize('fecha:2026-12-01T16:00Z', everyone);
if (!first.ok || !first.created || first.state !== 'confirmada' || first.member_count !== 2 || first.omitted_count !== 2) {
  fail('la lista de una fecha', first);
}
const reasons = Object.fromEntries((await sql(`select m.contact_id::text, m.state, m.omit_reason
    from public.flow_audience_members m where m.occurrence_id = $1`, [first.occurrence_id]))
  .map((row) => [row.contact_id, row.omit_reason ?? row.state]));
if (reasons[ana.contact] !== 'pendiente' || reasons[bea.contact] !== 'pendiente'
    || reasons[sinFormulario.contact] !== 'sin_permiso' || reasons[baja.contact] !== 'baja') {
  fail('los motivos de la lista', reasons);
}
const again = await materialize('fecha:2026-12-01T16:00Z', [member(ana)]);
if (again.created || again.occurrence_id !== first.occurrence_id || again.member_count !== 2) {
  fail('la misma ocurrencia no se arma dos veces', again);
}
results.fecha = { lista: first.member_count, omitidas: first.omitted_count, idempotente: !again.created };

// ---------------------------------------------------------------------------
// 3. El ritmo (dos por hora), la re-entrega y el fin.
// ---------------------------------------------------------------------------
const cami = await cartFor('audiencia-cami');
const paced = await materialize('fecha:2026-12-02T16:00Z', [member(ana), member(bea), member(cami)]);
const release = (now) => call('public.release_flow_audience_members_v1($1::uuid, $2::timestamptz, 50)',
  [paced.occurrence_id, now]);
const ids = (rows, flag) => rows.filter((r) => r.again === flag).map((r) => r.contact_id).sort();
const hour1 = await release('2026-12-02T16:01:00Z');
await settle(paced.occurrence_id, ana);
const early = await release('2026-12-02T16:05:00Z');
const redelivered = await release('2026-12-02T16:30:00Z');
const hour2 = await release('2026-12-02T17:05:00Z');
if (ids(hour1.released, false).length !== 2 || early.released.length !== 0
    || JSON.stringify(ids(redelivered.released, true)) !== JSON.stringify([bea.contact])
    || ids(redelivered.released, false).length !== 0
    || JSON.stringify(ids(hour2.released, false)) !== JSON.stringify([cami.contact]) || hour2.pending !== 0
    || hour2.state !== 'enviando') {
  fail('el ritmo por hora y la re-entrega', { hour1, early, redelivered, hour2 });
}
await settle(paced.occurrence_id, bea);
const twiceSettled = await settle(paced.occurrence_id, bea);
const last = await settle(paced.occurrence_id, cami, 'no_arranca');
if (twiceSettled.settled || last.state !== 'terminada') fail('asentar y terminar', { twiceSettled, last });
results.ritmo = [hour1.released.length, early.released.length, redelivered.released.length, hour2.released.length];

// ---------------------------------------------------------------------------
// 4. La repeticion: otra ocurrencia del mismo flujo dentro de los 7 dias.
// ---------------------------------------------------------------------------
const dora = await cartFor('audiencia-dora');
const repeated = await rolledBack(async () => {
  const third = await materialize('fecha:2026-12-03T16:00Z', [member(ana), member(dora)]);
  const rows = await sql('select contact_id::text, omit_reason from public.flow_audience_members where occurrence_id = $1',
    [third.occurrence_id]);
  return Object.fromEntries(rows.map((r) => [r.contact_id, r.omit_reason ?? 'pendiente']));
});
if (repeated[ana.contact] !== 'repetida' || repeated[dora.contact] !== 'pendiente') fail('la repeticion', repeated);
results.repeticion = repeated[ana.contact];

// ---------------------------------------------------------------------------
// 5. manual: una persona confirma en la pagina con el conteo exacto y el segundo paso.
// ---------------------------------------------------------------------------
const TEAM = 'T0LANCEMOS01';
const DAN = 'U0DANSCHWAB1';
const as = async (role, run) => {
  await db.exec(`set role ${role}`);
  try {
    return await run();
  } finally {
    await db.exec('reset role');
  }
};
const rpc = async (fn, ...args) => {
  const marks = args.map((_, i) => `$${i + 1}`).join(', ');
  const values = args.map((a) => (a !== null && typeof a === 'object' ? JSON.stringify(a) : a));
  return JSON.parse(one(await sql(`select public.${fn}(${marks})::text as r`, values), fn).r);
};
const inMinutes = async (minutes) => new Date((await world.dbNow()).getTime() + minutes * 60_000).toISOString();
const session = sha('audiencia-session');
await as('flow_change_approver', async () => {
  const state = sha('audiencia-state');
  await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha('audiencia-nonce'), '/flow-approval/',
    await inMinutes(10));
  await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
  const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, TEAM, DAN, await inMinutes(60));
  if (!created.ok) fail('entrar', created);
});
const eva = await cartFor('audiencia-eva');
await cycle([versionItem(flowId, A.funnel, 'difusion', definitionFor({ manual: {} }))], 'difusion manual');
const manualKey = 'manual:11111111-1111-4111-8111-111111111111';
const manual = await materialize(manualKey, [member(dora), member(eva)], 2, `slack:${DAN}`);
if (manual.state !== 'materializada' || manual.member_count !== 2) fail('manual espera a una persona', manual);
const page = await as('flow_change_approver', () => rpc('approver_read_flow_audience_v1', INSTANCE, session));
const shown = page.occurrences.find((o) => o.occurrence_id === manual.occurrence_id);
if (page.flows.length !== 1 || page.flows[0].form !== 'manual' || shown?.member_count !== 2
    || shown.materialized_by !== `slack:${DAN}` || JSON.stringify(page).includes(dora.phone)) {
  fail('la pagina muestra cuantas van a salir', page);
}
const confirm = (count, second, hash = session) => as('flow_change_approver',
  () => rpc('approver_confirm_flow_audience_v1', INSTANCE, hash, manual.occurrence_id, count, second));
const noSession = await (async () => {
  await db.exec('begin');
  try {
    await db.exec('set role flow_change_approver');
    await rpc('approver_confirm_flow_audience_v1', INSTANCE, sha('no-es-una-sesion'), manual.occurrence_id, 2, true);
    return 'confirmo';
  } catch (error) {
    return error.message;
  } finally {
    await db.exec('rollback');
  }
})();
const notReleased = await call('public.release_flow_audience_members_v1($1::uuid, now(), 50)', [manual.occurrence_id]);
const wrongCount = await confirm(3, true);
const noSecond = await confirm(2, false);
const ok = await confirm(2, true);
const twice = await confirm(2, true);
const confirmedBy = one(await sql('select confirmed_by from public.flow_audience_occurrences where id = $1',
  [manual.occurrence_id]), 'quien').confirmed_by;
if (noSession !== 'flow_approval_session_invalid' || notReleased.ok || wrongCount.reason_code !== 'count_mismatch'
    || noSecond.reason_code !== 'second_step_missing' || !ok.ok || twice.reason_code !== 'not_waiting_for_confirmation'
    || confirmedBy !== `slack:${DAN}`) {
  fail('la confirmacion manual', { noSession, notReleased, wrongCount, noSecond, ok, twice, confirmedBy });
}
results.manual = { sin_sesion: noSession, sin_confirmar: notReleased.reason_code,
  otro_conteo: wrongCount.reason_code, ok: ok.ok };

// ---------------------------------------------------------------------------
// 6. El freno: por la salud y a mano.
// ---------------------------------------------------------------------------
const braked = await rolledBack(async () => {
  const brake = await call('public.brake_flow_audience_occurrence_v1($1::uuid, $2)', [manual.occurrence_id, 'meta_salud:red']);
  const after = await call('public.release_flow_audience_members_v1($1::uuid, now(), 50)', [manual.occurrence_id]);
  return { brake, after };
});
const byHand = await rolledBack(async () => {
  const brake = await as('flow_change_approver',
    () => rpc('approver_brake_flow_audience_v1', INSTANCE, session, manual.occurrence_id));
  const reason = one(await sql('select brake_reason from public.flow_audience_occurrences where id = $1',
    [manual.occurrence_id]), 'motivo').brake_reason;
  return { brake, reason };
});
if (!braked.brake.ok || braked.after.reason_code !== 'not_releasable' || !byHand.brake.ok || byHand.reason !== 'a_mano') {
  fail('una frenada no se suelta', { braked, byHand });
}
results.freno = [braked.after.reason_code, byHand.reason];

// ---------------------------------------------------------------------------
// 7. Un flujo apagado no materializa; la lectura no trae telefonos.
// ---------------------------------------------------------------------------
const off = await rolledBack(async () => {
  await pauseFlow(flowId, A.funnel, 'difusion');
  return materialize('manual:22222222-2222-4222-8222-222222222222', [member(ana)], 2);
});
if (off.ok || off.reason_code !== 'flow_not_running') fail('un flujo pausado no materializa', off);
const read = await call('public.read_flow_audience_occurrences_v1($1::jsonb, 10)', [JSON.stringify(INSTANCE)]);
if (read.length !== 3 || JSON.stringify(read).includes(ana.phone)) fail('la lectura', read);
results.lectura = read.map((row) => `${row.form}:${row.state}`);

// ---------------------------------------------------------------------------
// 8. El rojo: sin la migracion no hay lista.
// ---------------------------------------------------------------------------
const red = await rolledBack(async () => {
  await db.exec('drop function public.materialize_flow_audience_occurrence_v1(jsonb,uuid,integer,jsonb,jsonb)');
  try {
    await materialize('fecha:2026-12-03T16:00Z', [member(ana)]);
    return 'existe';
  } catch (error) {
    return error.message.includes('does not exist') ? 'no_existe' : error.message;
  }
});
if (red !== 'no_existe') fail('sin la migracion la funcion no existe', red);
results.sin_la_migracion = red;

await db.close();
console.log(`validate_flow_audience_triggers: ok ${JSON.stringify(results)}`);
