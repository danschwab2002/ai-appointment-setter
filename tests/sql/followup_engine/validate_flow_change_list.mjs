// La lista de las propuestas del Automation Expert (migracion 20261030000300,
// E-208, el paso P7 del mapa M16), con la RPC real sobre la cadena completa de
// migraciones.
//
// Contrato: docs/contracts/flow-change-cycle-v1.md, «La lista de las
// propuestas (1.10.0)».
//
// Prueba, con un rojo por pieza:
//   0. la forma: agrega exactamente list_flow_changes_v1 y no cambia ninguna
//      otra funcion ni permiso; la ejecuta solo automation_expert_change (ni
//      anon, ni authenticated, ni service_role); correrla dos veces aborta.
//      Antes de la migracion la funcion no existe (el rojo de todo lo demas);
//   1. la lista, como el rol del agente: con p_only_open, solo un borrador y
//      una validada sin vencer (no la vencida, ni la publicada, ni la
//      descartada), con open igual al tope de save_flow_change_draft_v1; sin
//      p_only_open, todas, de la mas nueva a la mas vieja, con effective_status
//      vencida en la que paso las 24 h; el para que solo en las del agente; los
//      flujos de cada item; el ultimo evento; p_limit corta; y nada del ID de
//      Slack de quien la pidio;
//   2. los rojos de la entrada: p_limit 0, 51 o null y p_only_open null dan
//      22023 flow_change_list_parameters_invalid; otra aliada, 22023
//      flow_store_instance_not_bound; anon, 42501.
//
// Datos sinteticos, los de buildWorld (el manifiesto de ATT1). El ID de Slack
// es de forma valida e inventado. --emit-fixture deja la respuesta, con uuid y
// horas normalizados, en tests/fixtures/flow_change_list/ para las pruebas del
// bridge (tests/test_flow_change_list.py); --check verifica que siga igual.
import { PGlite } from '@electric-sql/pglite';
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261030000300_flow_change_list_v1.sql';
const SIGNATURE = 'list_flow_changes_v1(jsonb,boolean,integer)';
const EMIT = process.argv.includes('--emit-fixture');
// --check: la salida de hoy tiene que ser exactamente el fixture guardado.
const CHECK = process.argv.includes('--check');
const results = {};
const fixture = (path) => JSON.parse(readFileSync(join(root, 'tests/fixtures', path), 'utf8'));
const BRIDGE = fixture('flow_change_new_flow/ae_bienvenida_20261024.json');

// ---------------------------------------------------------------------------
// 0. La forma de la migracion, en una base propia: antes y despues.
// ---------------------------------------------------------------------------
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature.replace(/^public\./, ''), row]));

const shapeDb = new PGlite();
await shapeDb.waitReady;
await shapeDb.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  create role authenticator noinherit login;
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
  alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
  grant usage on schema public to service_role, anon;
`);
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`${MIGRATION} no esta en la cadena`);
// Despues va solo la de los frenos de la compra con nuestro link (E-215), que no toca esta lectura.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261030000400_portable_purchase_stops_see_our_link_v1.sql', '20261101000100_flow_engine_native_intent_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de E-215`, names.slice(position + 1));
}
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
await applyFile(shapeDb, join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of names.slice(0, position)) await applyFile(shapeDb, join(root, 'supabase/migrations', name));
const before = await snapshot(shapeDb);
if (before.has(SIGNATURE)) fail('la lista ya existe antes de la migracion');
const target = join(root, 'supabase/migrations', MIGRATION);
await applyFile(shapeDb, target);
const after = await snapshot(shapeDb);
{
  const added = [...after.keys()].filter((signature) => !before.has(signature));
  const removed = [...before.keys()].filter((signature) => !after.has(signature));
  const changed = [...before.keys()].filter((signature) => after.has(signature)
    && (after.get(signature).definition !== before.get(signature).definition
        || after.get(signature).acl !== before.get(signature).acl));
  if (!sameSet(added, [SIGNATURE]) || removed.length !== 0 || changed.length !== 0) {
    fail('la migracion cambio otra cosa', { added, removed, changed });
  }
  const can = async (role) => one((await shapeDb.query(
    `select has_function_privilege($1, to_regprocedure('public.${SIGNATURE}'), 'execute') as x`, [role])).rows,
  role).x;
  const grants = {
    automation_expert_change: await can('automation_expert_change'), service_role: await can('service_role'),
    anon: await can('anon'), authenticated: await can('authenticated'),
  };
  if (!grants.automation_expert_change || grants.service_role || grants.anon || grants.authenticated) {
    fail('los permisos de la lista', grants);
  }
  const definer = one((await shapeDb.query(`select prosecdef, provolatile from pg_proc
    where oid = to_regprocedure('public.${SIGNATURE}')`)).rows, 'funcion');
  if (!definer.prosecdef || definer.provolatile !== 's') fail('security definer y stable', definer);
  const error = await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'flow_change_list_already_applied' });
  results.forma = { agregadas: added, permisos: grants, segunda_corrida: error.message };
}
await shapeDb.close();

// ---------------------------------------------------------------------------
// El mundo: ATT1 (A), con la cadena entera. Los flujos del motor ya dejaron
// una propuesta del operador publicada ('el motor del carrito').
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, rpc } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const OTHER = { ...INSTANCE, ally_ref: 'otra-aliada' };
const AE = 'U0123456789';
// Si falla adentro de expectError, el rollback de la transaccion deshace el set role.
const as = async (role, run) => {
  await db.exec(`set role ${role}`);
  const out = await run();
  await db.exec('reset role');
  return out;
};
const agent = (run) => as('automation_expert_change', run);
const instanceBase = async () => (await db.query('select public._flow_store_base($1, $2, $3::uuid[]) b',
  [A.tenant, A.ally, []])).rows[0].b;
const bridgeItem = (flujo) => {
  const item = structuredClone(BRIDGE.items[0]);
  item.flujo = flujo;
  item.definicion.flujo = flujo;
  item.definicion.topes.bolsa = `${A.funnel}-${flujo}`.slice(0, 40);
  return item;
};
const agentDraft = async (flujo, purpose) => agent(async () => {
  const saved = await rpc('save_flow_change_draft_v1', INSTANCE, null, null, [bridgeItem(flujo)], {}, purpose, AE);
  if (!saved.ok) fail('guardar', saved);
  return saved;
});
const agentValidated = async (flujo, purpose) => {
  const saved = await agentDraft(flujo, purpose);
  const validatedBase = await instanceBase();
  return agent(async () => {
    const recorded = await rpc('record_flow_change_validation_v1', INSTANCE, saved.proposal_id, saved.revision,
      validatedBase, BRIDGE.validator, BRIDGE.result, BRIDGE.preview, AE);
    if (recorded.status !== 'validada') fail('validar', recorded);
    return { id: saved.proposal_id, fingerprint: recorded.fingerprint };
  });
};
const later = async () => db.exec("select pg_sleep(0.01)");

// ---------------------------------------------------------------------------
// 1. La lista, como el rol del agente.
// ---------------------------------------------------------------------------
const operatorPublished = (await sql(`select id from public.flow_change_proposals
  where requested_by_kind = 'operador' and status = 'publicada'`)).map((row) => row.id);
if (operatorPublished.length < 1) fail('el mundo no tiene una propuesta publicada del operador');
await later();
const expiring = await agentValidated('vencida', 'la que se vence');
// Vencida: se corre la validacion 25 h atras, como duenio y sin los disparadores (el metodo de
// validate_flow_store.mjs, 4m). Queda validada con expires_at en el pasado, como en la base antes
// de que algo la materialice.
await db.exec('set session_replication_role = replica');
await sql(`update public.flow_change_proposals set validated_at = validated_at - interval '25 hours',
  expires_at = expires_at - interval '25 hours' where id = $1`, [expiring.id]);
await db.exec('set session_replication_role = origin');
await later();
const discarded = await agentDraft('descartada', 'la que se descarta');
// PGlite guarda clock_timestamp con milisegundos: sin la pausa, el borrador y el descarte empatan.
await later();
const discard = await rpc('operator_discard_flow_change_v1', INSTANCE, discarded.proposal_id, 'dan',
  'no va mas');
if (!discard.ok) fail('descartar', discard);
await later();
const validated = await agentValidated('bienvenida', BRIDGE.purpose);
await later();
const draft = await agentDraft('borrador_vivo', 'el borrador que sigue abierto');

const open = await agent(() => rpc('list_flow_changes_v1', INSTANCE, true, 50));
const all = await agent(() => rpc('list_flow_changes_v1', INSTANCE, false, 50));
const firstOnly = await agent(() => rpc('list_flow_changes_v1', INSTANCE, false, 1));
{
  const ids = (listing) => listing.proposals.map((row) => row.proposal_id);
  if (JSON.stringify(ids(open)) !== JSON.stringify([draft.proposal_id, validated.id])) {
    fail('las abiertas: el borrador y la validada sin vencer, la mas nueva primero', ids(open));
  }
  const tope = one(await sql(`select count(*)::integer n from public.flow_change_proposals p
    where p.requested_by_kind = 'automation_expert' and (p.status = 'borrador'
      or (p.status in ('validada', 'aprobada') and clock_timestamp() < p.expires_at))`), 'tope').n;
  if (open.open !== tope || open.open !== 2 || open.open_limit !== 10) fail('open y su tope', open);
  const expected = [draft.proposal_id, validated.id, discarded.proposal_id, expiring.id, ...operatorPublished];
  if (!sameSet(ids(all), expected) || ids(all).slice(0, 4).join() !== expected.slice(0, 4).join()) {
    fail('todas, de la mas nueva a la mas vieja', ids(all));
  }
  const byId = Object.fromEntries(all.proposals.map((row) => [row.proposal_id, row]));
  const vencida = byId[expiring.id];
  if (vencida.status !== 'validada' || vencida.effective_status !== 'vencida') fail('la vencida', vencida);
  if (byId[discarded.proposal_id].status !== 'descartada' || byId[discarded.proposal_id].closed_by_kind !== 'operador'
      || byId[discarded.proposal_id].last_event?.event_type !== 'flow_change_discarded') {
    fail('la descartada, con su ultimo evento', byId[discarded.proposal_id]);
  }
  const mine = byId[validated.id];
  if (mine.purpose !== BRIDGE.purpose || mine.requested_by_kind !== 'automation_expert'
      || mine.flows.length !== 1 || mine.flows[0].producto !== A.funnel || mine.flows[0].flujo !== 'bienvenida'
      || mine.flows[0].tipo !== 'nuevo'
      || mine.last_event?.event_type !== 'flow_change_validated' || mine.revision !== 1
      || mine.validated_at === null || mine.expires_at === null) {
    fail('la validada del agente', mine);
  }
  const operator = byId[operatorPublished[0]];
  if (operator.purpose !== null || operator.requested_by_kind !== 'operador' || operator.status !== 'publicada'
      || operator.flows.length < 1) {
    fail('la del operador: sin el para que', operator);
  }
  if (JSON.stringify(all).includes(AE) || JSON.stringify(all).includes('"dan"')) {
    fail('la lista lleva el ID de quien pidio o aprobo', all);
  }
  for (const row of all.proposals) {
    for (const key of ['requested_by_ref', 'approved_by_ref', 'items', 'validation', 'draft_base']) {
      if (key in row) fail(`la fila trae ${key}`, row);
    }
  }
  if (firstOnly.proposals.length !== 1 || firstOnly.proposals[0].proposal_id !== draft.proposal_id) {
    fail('p_limit corta', firstOnly);
  }
  results.lista = { abiertas: open.proposals.length, todas: all.proposals.length, open: open.open };
}

// ---------------------------------------------------------------------------
// 2. Los rojos de la entrada.
// ---------------------------------------------------------------------------
{
  const bad = {};
  for (const [label, onlyOpen, limit] of [['cero', true, 0], ['51', true, 51], ['limite_nulo', true, null],
    ['estado_nulo', null, 10]]) {
    const error = await expectError(db, `p_limit ${label}`,
      () => agent(() => rpc('list_flow_changes_v1', INSTANCE, onlyOpen, limit)),
      { code: '22023', message: 'flow_change_list_parameters_invalid' });
    bad[label] = error.message;
  }
  const other = await expectError(db, 'otra aliada', () => agent(() => rpc('list_flow_changes_v1', OTHER, true, 10)),
    { code: '22023', message: 'flow_store_instance_not_bound' });
  const anon = await expectError(db, 'anon', () => as('anon', () => rpc('list_flow_changes_v1', INSTANCE, true, 10)),
    { code: '42501' });
  results.rojos = { ...bad, otra_aliada: other.message, anon: anon.code };
}

if (EMIT || CHECK) {
  // uuid y horas normalizados: el bridge solo mira la forma.
  const ids = new Map();
  const normalize = (value) => JSON.parse(JSON.stringify(value)
    .replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g, (id) => {
      if (!ids.has(id)) ids.set(id, `00000000-0000-4000-8000-${String(ids.size + 1).padStart(12, '0')}`);
      return ids.get(id);
    })
    .replace(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)/g, '2026-10-30T12:00:00.000000+00:00'));
  const dir = join(root, 'tests/fixtures/flow_change_list');
  const text = `${JSON.stringify({
    _capture: {
      source: 'tests/sql/followup_engine/validate_flow_change_list.mjs --emit-fixture',
      migration: MIGRATION,
      note: 'la salida real de list_flow_changes_v1 sobre la cadena de migraciones en PGlite, datos sinteticos; '
        + 'uuid y horas normalizados',
    },
    open: normalize(open),
    all: normalize(all),
  }, null, 2)}\n`;
  if (CHECK && readFileSync(join(dir, 'list_flow_changes_v1.json'), 'utf8') !== text) {
    fail('tests/fixtures/flow_change_list/list_flow_changes_v1.json no es la salida de hoy: regenerarlo con --emit-fixture');
  }
  if (EMIT) {
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, 'list_flow_changes_v1.json'), text);
  }
}

await world.db.close();
console.log(JSON.stringify({ ok: true, migration: MIGRATION, results }, null, 2));
