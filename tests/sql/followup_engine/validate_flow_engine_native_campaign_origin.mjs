// De que landing y de que oferta viene un recorrido nativo (migracion
// 20261103001700, E-321; contrato docs/contracts/campaign-entry-v1.md).
//
// Por que: el carrito de una campana se identifica por la landing que vio la
// persona (Dan, 03/11). El bridge pregunta, antes del primer envio de un
// recorrido nativo, su landing y su oferta, y si son de una [[campanas]]
// vigente le pone a la conversacion la etiqueta de la campana.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (la cadena
// entera): el formulario con consentimiento y el carrito capturado
// (hotmart_cart_abandonment_rejected_v1.json) pasan por las admisiones
// portables de hoy, que correlacionan el carrito con la intencion, como en
// produccion. El paso nativo nombra la plantilla de Maribel
// (tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json),
// como en validate_flow_engine_native_cart.mjs.
//
// Prueba:
//   0. la forma: suma exactamente una funcion, solo de service_role, no cambia
//      ninguna otra ni su ACL, y correrla dos veces aborta;
//   1. la accion de un carrito nativo dice carrito, la oferta del evento y la
//      landing de la intencion correlacionada (la de purchase_intents);
//   2. con una intencion que no es real (provider_observed falso) la landing
//      es nula y la oferta sigue;
//   3. una accion que no existe da nulo;
//   4. sin la migracion la funcion no existe (el rojo).
//
// Con --emit escribe tests/fixtures/flow_engine/native_campaign_origin_rows.json
// (lo que contesta la base, con los uuid normalizados) para
// tests/test_campaign_entry.py; con --check (el de npm test) lo compara.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261103001700_flow_engine_native_campaign_origin_v1.sql';
const SIGNATURE = 'flow_engine_native_campaign_origin_v1(uuid)';
const FIXTURE = join(root, 'tests/fixtures/flow_engine/native_campaign_origin_rows.json');
const EMIT = process.argv.includes('--emit');
const CHECK = process.argv.includes('--check');
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Lo que venga despues no puede redefinir la lectura ni la intencion del recorrido sin mirar esta prueba.
// Revisadas: el pago rechazado sin formulario (20261112210400, E-483, pieza PF) abre un recorrido sin
// intencion (audience_hotmart_phone); la lectura no cambia y da la oferta sin landing (lo afirma
// validate_e483_payment_failure_by_phone.mjs).
// La ficha nativa (20261113200000, E-528, C4) abre su recorrido con la intencion de la ficha, como el carrito
// (la restriccion audience_by_trigger la suma); la lectura no cambia.
const REVIEWED = new Set(['20261112210400_e483_payment_failure_by_phone_v1.sql',
  '20261113200000_e528_native_payment_slip_v1.sql',
    // El correlador que decide solo y su pregunta (20261114000100 y 20261114000150, E-536).
    '20261114000100_identity_link_decider_v1.sql', '20261114000150_identity_link_question_v1.sql']);
for (const name of names.slice(position + 1).filter((candidate) => !REVIEWED.has(candidate))) {
  const text = readFileSync(join(root, 'supabase/migrations', name), 'utf8');
  if (text.includes('flow_engine_native_campaign_origin') || text.includes('audience_purchase_intent_id')) {
    fail(`${name} toca la lectura o la intencion del recorrido: revisar esta prueba`);
  }
}
const maribel = JSON.parse(readFileSync(join(root,
  'tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json'), 'utf8')).message_templates[0];

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
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
  if (before.has(SIGNATURE)) fail('la lectura ya existe antes de la migracion');
  await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
  const after = await snapshot(db);
  const changed = [...before.keys()].filter((s) => after.has(s) && after.get(s).definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && ['acl', 'prosecdef', 'provolatile'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (changed.length !== 0 || !sameSet(added, [SIGNATURE]) || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  const fn = after.get(SIGNATURE);
  if (!fn.service_x || fn.api_x || !fn.prosecdef || fn.provolatile !== 's') {
    fail('la lectura es solo de service_role, security definer y stable', fn);
  }
  if (!fn.definition.includes('flow_engine_native_campaign_origin')) fail('la lectura sin su marca');
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'flow_engine_native_campaign_origin_already_applied') {
      fail('la segunda corrida aborto por otra cosa', error.message);
    }
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { nuevas: added.length, cambiadas: changed.length };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo: ATT1 (buildWorld, la cadena entera) con un carrito nativo.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, cartFor } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
await db.exec('alter role service_role bypassrls');
const asService = async (run) => {
  await db.exec('set role service_role');
  try {
    return await run();
  } finally {
    await db.exec('reset role');
  }
};
const rolledBack = async (run) => {
  await db.exec('begin');
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};
const origin = async (actionId) => asService(async () => one((await db.query(
  `select public.flow_engine_native_campaign_origin_v1($1)::text as r`, [actionId])).rows, 'origen').r);

const CART = {
  esquema: 'setter-flujo/v1', flujo: 'carrito_nativo', producto: A.funnel, estado: 'borrador', nombre: 'Carrito nativo',
  dispara: { evento: 'carrito' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [{ plantilla: maribel.name, idioma: maribel.language, variables: ['nombre', 'producto'] }],
  termina: { si: ['compra'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-carrito-nativo', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
const flowId = await register(A.funnel, 'carrito_nativo');
await cycle([versionItem(flowId, A.funnel, 'carrito_nativo', CART)], 'v1 de carrito_nativo');
const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
  [flowId]), 'propuesta').proposal_id;
const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
  [flowId, proposalId, A.timezone]), 'abrir').r);
if (!opened.ok || opened.state !== 'cohorte') fail('abrir el carrito nativo', opened);

// ---------------------------------------------------------------------------
// 1. La accion de un carrito nativo.
// ---------------------------------------------------------------------------
const maria = await cartFor('origen-maria');
await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
  [flowId, maria.contact]);
const flowEvent = {
  fuente: 'hotmart', tipo: 'carrito', evento_id: maria.event, producto_ref: A.funnel, oferta: maria.offer.offer_code,
  contacto_id: maria.contact, momento: maria.cartArgs[6], chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
  destino: maria.phone,
};
const started = one((await asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
  [JSON.stringify(INSTANCE), JSON.stringify(flowEvent)])).rows)).filter((row) => row.flow_id === flowId), 'arranque');
if (started.outcome !== 'motor_arranco') fail('el carrito abre el recorrido nativo', started);
const intent = one(await sql(`select pi.id, pi.landing_ref, pi.provider_observed
    from public.hotmart_purchase_intent_correlations corr
    join public.purchase_intents pi on pi.id = corr.purchase_intent_id
   where corr.webhook_event_id = $1 and corr.outcome = 'resolved'`, [maria.event]), 'intencion');
if (!intent.provider_observed || !intent.landing_ref) fail('la intencion del formulario es real y trae su landing', intent);
const cartOrigin = JSON.parse(await origin(started.scheduled_action_id));
if (cartOrigin.disparador !== 'carrito' || cartOrigin.oferta !== maria.offer.offer_code
    || cartOrigin.landing !== intent.landing_ref) {
  fail('el origen del carrito', { cartOrigin, intent, oferta: maria.offer.offer_code });
}
results.carrito = cartOrigin;

// ---------------------------------------------------------------------------
// 2. Una intencion que no es real no dice landing.
// ---------------------------------------------------------------------------
const fabricated = await rolledBack(async () => {
  await sql('update public.purchase_intents set provider_observed = false where id = $1', [intent.id]);
  return JSON.parse(await origin(started.scheduled_action_id));
});
if (fabricated.landing !== null || fabricated.oferta !== maria.offer.offer_code || fabricated.disparador !== 'carrito') {
  fail('una intencion fabricada no da landing', fabricated);
}
results.fabricada = fabricated;

// ---------------------------------------------------------------------------
// 3. Una accion que no es de un recorrido nativo.
// ---------------------------------------------------------------------------
const none = await origin('00000000-0000-4000-8000-000000000000');
if (none !== null) fail('una accion que no existe da nulo', none);
results.otra = none;

// ---------------------------------------------------------------------------
// 4. El rojo: sin la migracion no hay lectura.
// ---------------------------------------------------------------------------
const red = await rolledBack(async () => {
  await db.exec(`drop function public.${SIGNATURE}`);
  return one(await sql(`select case when to_regprocedure('public.${SIGNATURE}') is null
    then 'no_existe' else 'existe' end as r`), 'rojo').r;
});
if (one(await sql(`select to_regprocedure('public.${SIGNATURE}') is not null as r`), 'vuelve').r !== true) {
  fail('el rollback devuelve la lectura');
}
if (red !== 'no_existe') fail('sin la migracion la lectura no existe', red);
results.sin_la_migracion = red;

// ---------------------------------------------------------------------------
// El fixture para el bridge.
// ---------------------------------------------------------------------------
const emitted = {
  _capture: {
    origen: 'validate_flow_engine_native_campaign_origin.mjs: la cadena entera de migraciones (PGlite), el '
      + 'formulario y el carrito capturado por las admisiones portables, y el recorrido nativo del carrito',
    encargo: 'E-321',
    regenerar: 'cd tests/sql/followup_engine && node validate_flow_engine_native_campaign_origin.mjs --emit',
  },
  cart: { ...cartOrigin, oferta: '<OFERTA>' },
  cart_fabricated_intent: { ...fabricated, oferta: '<OFERTA>' },
  not_native: none,
};
if (EMIT) {
  writeFileSync(FIXTURE, `${JSON.stringify(emitted, null, 2)}\n`);
} else if (CHECK) {
  const stored = JSON.parse(readFileSync(FIXTURE, 'utf8'));
  if (JSON.stringify(stored) !== JSON.stringify(emitted)) {
    fail('native_campaign_origin_rows.json no es lo que da la cadena: correr con --emit', emitted);
  }
}
await db.close();
console.log(`validate_flow_engine_native_campaign_origin: ok ${JSON.stringify(results)}`);
