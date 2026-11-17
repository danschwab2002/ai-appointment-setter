// La repeticion de un flujo de audiencia cuenta solo a quien arranco un recorrido
// (migracion 20261117000600, E-665; contrato docs/contracts/flow-audience-trigger-v1.md).
//
// Dato real: tests/fixtures/flow_engine/audience_repeat_not_started_maribel_20261117.json
// (maribel_db, 17/11, solo lectura). En aviso_manual_prueba la persona de prueba quedo
// liberada con open_outcome = no_arranca (conversacion_viva) en la ocurrencia del 16/11
// y omitida como repetida en la del 17/11, sin haber recibido nada.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena entera) sin
// esta migracion; se aplica en el medio, sobre el mismo mundo.
//
// Prueba:
//   1. el rojo: sin la migracion, quien quedo no_arranca (la secuencia del fixture) o
//      sin_flujo en la ocurrencia anterior queda omitida como repetida, igual que en
//      Maribel;
//   2. la forma: cambia solo la definicion de materialize_flow_audience_occurrence_v1,
//      con los mismos atributos y permisos; la segunda corrida no hace nada;
//   3. con la migracion: no_arranca y sin_flujo entran a la ocurrencia siguiente;
//      arranco y la liberada sin asentar siguen repetidas; arranco fuera de la ventana
//      entra; la baja sigue siendo baja.
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261117000600_flow_audience_repeat_counts_started_v1.sql';
const FIXTURE = join(root, 'tests/fixtures/flow_engine/audience_repeat_not_started_maribel_20261117.json');
const SIGNATURE = 'materialize_flow_audience_occurrence_v1(jsonb,uuid,integer,jsonb,jsonb)';
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
if (!names.includes(MIGRATION)) fail(`falta la migracion ${MIGRATION}`);
const real = JSON.parse(readFileSync(FIXTURE, 'utf8'));
const [realFirst, realSecond] = real.ocurrencias;
if (realFirst.miembro.open_outcome !== 'no_arranca' || realSecond.miembro.omit_reason !== 'repetida'
    || real.flujo.topes.repeticion.otra_despues_de !== '7d') {
  fail('el fixture de Maribel no es el caso del encargo', real);
}

const world = await buildWorld({ skip: [MIGRATION] });
const { db, A, INSTANCE, register, cycle, versionItem, stateItem, cartFor } = world;
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
const applyFile = async (file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));

const flowId = await register(A.funnel, 'aviso');
await cycle([versionItem(flowId, A.funnel, 'aviso', {
  esquema: 'setter-flujo/v1', flujo: 'aviso', producto: A.funnel, estado: 'borrador', nombre: 'Aviso',
  dispara: { fecha: { el: '2026-12-01T10:00', zona: 'America/Mexico_City' } },
  para: { es: 'prospecta', formulario_en: '30d' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ plantilla: 'att1_carrito_dudas_01', idioma: 'es_MX', variables: ['nombre', 'producto'] }],
  termina: { si: ['compra'] }, horario: 'siempre',
  topes: { bolsa: 'att1-aviso', por_dia: 'sin_tope', total: 'sin_tope', repeticion: real.flujo.topes.repeticion },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  ritmo: { por_hora: 50, por_ocurrencia: 50 },
}), stateItem(flowId, A.funnel, 'aviso', 'prender')], 'aviso por fecha');

const FINGERPRINT = 'b'.repeat(64);
const member = (lead) => ({ contact_id: lead.contact, phone: lead.phone });
const materialize = (key, members) => call(
  'public.materialize_flow_audience_occurrence_v1($1::jsonb, $2::uuid, 1, $3::jsonb, $4::jsonb)',
  [JSON.stringify(INSTANCE), flowId,
    JSON.stringify({ key, scheduled_at: '2026-12-01T16:00:00Z', fingerprint: FINGERPRINT }), JSON.stringify(members)]);
const settle = (occurrence, lead, outcome) => call(
  'public.settle_flow_audience_member_v1($1::uuid, $2::uuid, $3)', [occurrence, lead.contact, outcome]);
const reasonsOf = async (occurrence) => Object.fromEntries((await sql(`select contact_id::text, state, omit_reason
    from public.flow_audience_members where occurrence_id = $1`, [occurrence]))
  .map((r) => [r.contact_id, r.omit_reason ?? r.state]));

// La ocurrencia anterior: cada persona termina con un asiento distinto.
const arranco = await cartFor('repeticion-arranco');
const noArranca = await cartFor('repeticion-no-arranca');   // la secuencia del fixture de Maribel
const sinFlujo = await cartFor('repeticion-sin-flujo');
const sinAsentar = await cartFor('repeticion-sin-asentar');
const vieja = await cartFor('repeticion-arranco-hace-8-dias');
const baja = await cartFor('repeticion-baja');
const nueva = await cartFor('repeticion-nueva');
const everyone = [arranco, noArranca, sinFlujo, sinAsentar, vieja, baja];
const first = await materialize('fecha:2026-12-01T16:00Z', everyone.map(member));
if (!first.ok || first.member_count !== everyone.length) fail('la primera ocurrencia', first);
const released = await call('public.release_flow_audience_members_v1($1::uuid, now(), 50)', [first.occurrence_id]);
if (released.released.length !== everyone.length) fail('soltar la primera ocurrencia', released);
await settle(first.occurrence_id, arranco, 'arranco');
await settle(first.occurrence_id, noArranca, realFirst.miembro.open_outcome);
await settle(first.occurrence_id, sinFlujo, 'sin_flujo');
await settle(first.occurrence_id, vieja, 'arranco');
await settle(first.occurrence_id, baja, 'no_arranca');
// La liberacion de hace 8 dias: fuera de otra_despues_de (7d).
await sql(`update public.flow_audience_members set released_at = now() - interval '8 days', opened_at = now() - interval '8 days'
  where occurrence_id = $1 and contact_id = $2`, [first.occurrence_id, vieja.contact]);
await sql(`insert into public.contact_authorizations (contact_id, channel, purpose, authorization_status,
    authorization_source, valid_from) values ($1, 'whatsapp', 'cart_recovery', 'denied', 'manual', now() - interval '1 minute')`,
[baja.contact]);

const nextOccurrence = () => rolledBack(async () => {
  const second = await materialize('fecha:2026-12-02T16:00Z', [...everyone, nueva].map(member));
  const r = await reasonsOf(second.occurrence_id);
  return { arranco: r[arranco.contact], no_arranca: r[noArranca.contact], sin_flujo: r[sinFlujo.contact],
    sin_asentar: r[sinAsentar.contact], arranco_hace_8_dias: r[vieja.contact], baja: r[baja.contact],
    nueva: r[nueva.contact], member_count: second.member_count };
});

// ---------------------------------------------------------------------------
// 1. El rojo: sin la migracion, el no_arranca consume la repeticion (Maribel).
// ---------------------------------------------------------------------------
const before = await nextOccurrence();
if (before.no_arranca !== realSecond.miembro.omit_reason || before.sin_flujo !== 'repetida'
    || before.arranco !== 'repetida' || before.nueva !== 'pendiente') {
  fail('sin la migracion, el caso de Maribel tendria que reproducirse', before);
}
results.sin_la_migracion = before;

// ---------------------------------------------------------------------------
// 2. La forma.
// ---------------------------------------------------------------------------
const snapshot = async () => new Map((await sql(`
  select p.oid::regprocedure::text as signature, md5(pg_get_functiondef(p.oid)) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile, coalesce(p.proconfig::text, '') as config
    from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'`))
  .map((row) => [row.signature.replace(/^public\./, ''), row]));
const pre = await snapshot();
await applyFile(join(root, 'supabase/migrations', MIGRATION));
const post = await snapshot();
const changed = [...pre.keys()].filter((s) => JSON.stringify(pre.get(s)) !== JSON.stringify(post.get(s)));
const added = [...post.keys()].filter((s) => !pre.has(s));
if (JSON.stringify(changed) !== JSON.stringify([SIGNATURE]) || added.length) {
  fail('la migracion cambia solo la definicion del armado', { changed, added });
}
const [a, b] = [pre.get(SIGNATURE), post.get(SIGNATURE)];
if (a.acl !== b.acl || !b.prosecdef || b.provolatile !== 'v' || a.config !== b.config) {
  fail('mismos atributos y permisos', { a, b });
}
const grants = one(await sql(`select has_function_privilege('service_role', $1::regprocedure, 'execute') as service,
    has_function_privilege('anon', $1::regprocedure, 'execute') or has_function_privilege('authenticated', $1::regprocedure,
      'execute') as api`, [`public.${SIGNATURE}`]), 'permisos');
if (!grants.service || grants.api) fail('solo service_role ejecuta el armado', grants);
await applyFile(join(root, 'supabase/migrations', MIGRATION));
const twice = await snapshot();
if (JSON.stringify(twice.get(SIGNATURE)) !== JSON.stringify(b)) fail('la segunda corrida no hace nada');
results.forma = { cambiadas: changed, segunda_corrida: 'sin cambios' };

// ---------------------------------------------------------------------------
// 3. Con la migracion.
// ---------------------------------------------------------------------------
const after = await nextOccurrence();
const expected = { arranco: 'repetida', no_arranca: 'pendiente', sin_flujo: 'pendiente', sin_asentar: 'repetida',
  arranco_hace_8_dias: 'pendiente', baja: 'baja', nueva: 'pendiente', member_count: 4 };
if (JSON.stringify(after) !== JSON.stringify(expected)) fail('la repeticion cuenta solo a quien arranco', after);
results.con_la_migracion = after;

// Una definicion desconocida aborta sin tocar nada.
const unknown = await rolledBack(async () => {
  await db.exec(`create or replace function public.materialize_flow_audience_occurrence_v1(
      p_instance jsonb, p_flow_id uuid, p_version integer, p_occurrence jsonb, p_members jsonb)
    returns jsonb language plpgsql volatile security definer set search_path = pg_catalog, public, pg_temp
    as $f$ begin return '{}'::jsonb; end; $f$`);
  try {
    await applyFile(join(root, 'supabase/migrations', MIGRATION));
    return 'aplico';
  } catch (error) {
    return error.message;
  }
});
if (unknown !== 'unexpected_flow_audience_materialize_definition') fail('una definicion desconocida aborta', unknown);
results.definicion_desconocida = unknown;

await db.close();
console.log(`validate_flow_audience_repeat_started: ok ${JSON.stringify(results)}`);
