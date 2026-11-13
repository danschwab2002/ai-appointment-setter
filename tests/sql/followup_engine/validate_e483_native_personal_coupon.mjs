// El cupon propio por persona en el motor nativo (migracion 20261112210500, E-483, C2 del mapa
// «el AE maneja todos los flujos», 2026-11-12; P8).
//
// Por que: el nativo frenaba boton: {link: checkout, cupon: personal}. El motor v1 (la ruta motor del
// carrito de ATT1) ya lo hace con un sustrato que no mira el legado: el trabajador del cupon siembra el
// codigo de un paso que la politica declara con coupon_ordinal y la reserva del link lo emite. Esta
// prueba mide que el nativo se sume a ese sustrato sin cambiar lo que factura ATT1.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs (el carrito capturado
// hotmart_cart_abandonment_rejected_v1.json, el formulario y la politica de tres toques del legado) y las
// plantillas de ATT1 del carrito (A1 con cupon, A2 y Z: tests/fixtures/chatwoot_message_templates_inbox_11_carrito_20261018.json).
//
// Prueba:
//   0. la forma: cambian solo las cinco funciones y se suman dos, con su marca y sus permisos; correrla
//      dos veces aborta; despues de ella no va ninguna;
//   1. lo que factura no cambia: sobre el mismo carrito del legado de ATT1, la siembra de A1 y de Z (el
//      cupon 1 y el 2: cantidad, ordinal, descuento, vencimiento y forma del codigo) y la reserva del link
//      de A1 dan lo mismo con las definiciones de antes y las de ahora; y la politica de la ruta motor
//      (A1, A2 y Z con cupon) compila igual;
//   2. un carrito nativo de tres toques con el cupon propio en A1 y Z (la forma de ATT1): la politica lleva
//      coupon_ordinal 1 y 2; el trabajador siembra el codigo de cada uno a su hora, que vence a las 24 h
//      del toque; el bridge lee el ordinal y el cupon del paso; el contexto trae las variables y el boton;
//      la reserva emite nuestro link; y el caso no tiene mas de dos codigos;
//   3. con el cupon en A2 y Z: los ordinales son 1 y 2 en los toques siguientes;
//   4. lo que no abre: tres cupones, el cupon en la compra, el formulario o el seguimiento, en la cadena
//      del pago rechazado, un boton de cupon propio que no es el del checkout, cuando en un toque siguiente
//      y precio o vence sin el cupon; y lo que si: el pago rechazado sin cadena;
//   5. rojo: con las definiciones de antes, el carrito con cupon propio no abre ni compila.
//
// Con --emit escribe tests/fixtures/flow_engine/native_personal_coupon_rows.json (las filas que lee el
// bridge: el contexto de cada toque con cupon y su cupon) para tests/test_e483_native_personal_coupon.py;
// con --check (el de npm test) lo compara y falla si difiere.
import { PGlite } from '@electric-sql/pglite';
import { randomBytes } from 'node:crypto';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261112210500_e483_native_personal_coupon_v1.sql';
const CHANGED = [
  '_flow_engine_native_steps(text)',
  '_flow_engine_compile_policy_core(jsonb,text,integer,text,text)',
  '_flow_engine_native_unsupported(jsonb)',
  'claim_due_recovery_coupons_v1(timestamp with time zone,text,integer,text,interval,interval,numeric,integer)',
  'reserve_recovery_checkout_issuance_v1(uuid,text,bigint,uuid,text,timestamp with time zone)',
];
const ADDED = ['_flow_engine_native_first_coupon_ordinal(uuid)', 'get_flow_engine_native_step_coupon_v1(uuid)'];
const FIXTURE = join(root, 'tests/fixtures/flow_engine/native_personal_coupon_rows.json');
const EMIT = process.argv.includes('--emit');
const CHECK = process.argv.includes('--check');
const results = {};
const emitted = {};
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues va el evento de un disparo por audiencia (20261112210600, E-483, para E-484): suma la ranura
// nativo:audiencia y el permiso de la audiencia, sin tocar las del carrito ni las del pago rechazado.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261112210600_e483_native_audience_source_v1.sql', '20261112220000_flow_audience_triggers_v1.sql', '20261112230000_e483_native_person_cap_v1.sql'])) {
  fail(`despues de ${MIGRATION} va solo la de la audiencia`, names.slice(position + 1));
}

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const previousDefinitions = {};
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
  const changed = [...before.keys()].filter((s) => after.has(s) && after.get(s).definition !== before.get(s).definition);
  const added = [...after.keys()].filter((s) => !before.has(s));
  const removed = [...before.keys()].filter((s) => !after.has(s));
  const attributes = [...before.keys()].filter((s) => after.has(s)
    && ['acl', 'prosecdef', 'provolatile'].some((k) => after.get(s)[k] !== before.get(s)[k]));
  if (!sameSet(changed, CHANGED) || !sameSet(added, ADDED) || removed.length !== 0 || attributes.length !== 0) {
    fail('la migracion cambio otra cosa', { changed, added, removed, attributes });
  }
  for (const signature of CHANGED) {
    if (!after.get(signature).definition.includes('e483_native_personal_coupon')) fail(`${signature}: sin la marca`);
    previousDefinitions[signature] = before.get(signature).definition;
  }
  // Lo nuevo: solo service_role ejecuta la lectura del bridge; el ordinal, nadie de la API.
  const grants = (await db.query(`
    select p.proname, r.rolname, has_function_privilege(r.rolname, p.oid, 'EXECUTE') as can
      from pg_proc p cross join pg_roles r
     where p.pronamespace = 'public'::regnamespace
       and p.proname in ('_flow_engine_native_first_coupon_ordinal', 'get_flow_engine_native_step_coupon_v1')
       and r.rolname in ('anon', 'authenticated', 'service_role') order by 1, 2`)).rows;
  const allowed = grants.filter((row) => row.can).map((row) => `${row.proname}:${row.rolname}`);
  if (JSON.stringify(allowed) !== JSON.stringify(['get_flow_engine_native_step_coupon_v1:service_role'])) {
    fail('los permisos de lo nuevo', allowed);
  }
  // La compra y el formulario siguen sin cupon propio.
  const slots = (await db.query(`select k, public._flow_engine_native_steps('nativo:' || k) s
    from unnest(array['compra', 'carrito', 'intencion', 'pago_fallido']) k order by k`)).rows;
  const personal = Object.fromEntries(slots.map((row) => [row.k, row.s.map((slot) => slot.cupon_personal === true)]));
  if (JSON.stringify(personal) !== JSON.stringify({ carrito: [true, true, true], compra: [false],
    intencion: [false, false, false], pago_fallido: [true, true, true] })) fail('las ranuras con cupon propio', personal);
  try {
    await applyFile(db, join(root, 'supabase/migrations', MIGRATION));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (error.message !== 'e483_native_personal_coupon_already_applied') {
      fail('la segunda corrida aborto por otra cosa', error.message);
    }
  }
  await db.exec('rollback').catch(() => {});
  results.forma = { cambiadas: changed.length, nuevas: added.length, ranuras: personal };
  await db.close();
}

// ---------------------------------------------------------------------------
// El mundo.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, A, INSTANCE, register, cycle, versionItem, reserve, startNew, startOld, accept, cartFor, planCart,
  stepOf, dbNow } = world;
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
const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
const ulid = () => {
  let value = (BigInt(Date.now()) << 80n) | BigInt(`0x${randomBytes(10).toString('hex')}`);
  let out = '';
  for (let i = 0; i < 26; i += 1) {
    out = CROCKFORD[Number(value & 31n)] + out;
    value >>= 5n;
  }
  return out;
};
const claimCoupons = async () => sql(`
  select * from public.claim_due_recovery_coupons_v1(
    $1::timestamptz,'cupon-nativo',60,'ATT','45 minutes'::interval,'24 hours'::interval,0.1::numeric,100)
`, [await dbNow()]);
// Los cupones de un caso, sin el codigo (que sale al azar): lo que un cliente ve de cada uno.
const couponsOf = async (caseId) => sql(`
  select coupon.ordinal, coupon.status, coupon.discount::text as discount,
         extract(epoch from coupon.expires_at - action.due_at)::int as validity_seconds,
         coupon.code ~ '^ATT[0-9A-F]{12}$' as code_shape, action.step_key
    from public.recovery_coupons coupon
    join lateral (select sa.due_at, sa.step_key from public.scheduled_actions sa
                   where sa.recovery_case_id = coupon.recovery_case_id
                     and (coalesce(public._recovery_followup_step(sa.id) ->> 'coupon_ordinal', '') = coupon.ordinal::text
                          or (coupon.ordinal = 1 and sa.action_type = 'first_contact_review'))
                   order by sa.created_at limit 1) action on true
   where coupon.recovery_case_id = $1 order by coupon.ordinal`, [caseId]);
// Con las definiciones de antes, dentro de una transaccion que se deshace.
const withPrevious = async (signatures, run) => {
  await db.exec('begin');
  try {
    for (const signature of signatures) await db.exec(previousDefinitions[signature]);
    return await run();
  } finally {
    await db.exec('rollback');
  }
};
const inRollback = async (run) => {
  await db.exec('begin');
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};
const CLAIM = CHANGED[3];
const RESERVE = CHANGED[4];
const compile = async (definition, kind) => one(await sql(
  "select (public._flow_engine_compile_policy_core($1::jsonb, '0b3c5f00-0e30-4000-8000-0000000e3060', 1, $2, null)).steps as s",
  [JSON.stringify(definition), kind]), 'compilar').s;
const compileError = async (definition, kind) => {
  try {
    await inRollback(() => compile(definition, kind));
  } catch (error) {
    return error.message;
  }
  return null;
};
const unsupported = async (definition) => one(await sql('select public._flow_engine_native_unsupported($1::jsonb) as r',
  [JSON.stringify(definition)]), 'unsupported').r;

// ---------------------------------------------------------------------------
// 1. Lo que factura no cambia: el carrito del legado de ATT1, antes y ahora.
// ---------------------------------------------------------------------------
{
  const lead = await cartFor('cupon-legado');
  lead.chatwoot = '610';
  const plan = await planCart(lead);
  if (!plan.created) fail('el plan del legado', plan);
  await sql("update public.scheduled_actions set due_at = now() + interval '5 minutes' where id = $1",
    [plan.scheduled_action_id]);
  const a1Before = await withPrevious([CLAIM], async () => {
    await claimCoupons();
    return couponsOf(plan.recovery_case_id);
  });
  await claimCoupons();
  const a1After = await couponsOf(plan.recovery_case_id);
  if (a1After.length !== 1 || JSON.stringify(a1Before) !== JSON.stringify(a1After)) {
    fail('el cupon de A1 del legado cambio', { a1Before, a1After });
  }
  // La reserva del link de A1, antes y ahora: el mismo resultado.
  const a1 = await reserve(lead, plan.scheduled_action_id);
  const link = async () => {
    const row = one(await sql('select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)',
      [a1.actionId, a1.worker, a1.lease, a1.attempt.id, ulid(), a1.now]), 'link de A1');
    return { outcome: row.outcome, url: (row.checkout_url_final ?? '').replace(/sck=[^&]+/, 'sck=<sck>') };
  };
  const linkBefore = await withPrevious([RESERVE], link);
  const linkAfter = await inRollback(link);
  if (linkBefore.outcome !== 'reserved' || JSON.stringify(linkBefore) !== JSON.stringify(linkAfter)) {
    fail('el link de A1 del legado cambio', { linkBefore, linkAfter });
  }
  // A1 y A2 salen por el camino del legado; Z (el cupon 2) se siembra igual antes y ahora.
  if ((await startOld(a1)).phase !== 'request_started') fail('A1 del legado');
  await accept(lead, a1);
  const a2 = await stepOf(lead, 'cart_second_touch');
  const a2r = await reserve(lead, a2.id);
  if ((await startOld(a2r)).phase !== 'request_started') fail('A2 del legado');
  await accept(lead, a2r);
  const z = await stepOf(lead, 'cart_last_touch');
  const zBefore = await withPrevious([CLAIM], async () => {
    await claimCoupons();
    return couponsOf(plan.recovery_case_id);
  });
  await claimCoupons();
  const zAfter = await couponsOf(plan.recovery_case_id);
  if (zAfter.length !== 2 || JSON.stringify(zBefore) !== JSON.stringify(zAfter)) {
    fail('el cupon de Z del legado cambio', { zBefore, zAfter });
  }
  // (A1 ya salio: el claim del despacho le movio la hora; Z vence a las 24 h de la suya.)
  if (zAfter[1].validity_seconds !== 24 * 3600 || zAfter.some((row) => !row.code_shape)) fail('los cupones del legado', zAfter);
  // La politica de la ruta motor del carrito de ATT1 (A1, A2 y Z con cupon) compila igual.
  const motorDefinition = world.cartDefinition(A.funnel);
  const motorBefore = await withPrevious([CHANGED[1]], () => compile(motorDefinition, 'carrito'));
  const motorAfter = await compile(motorDefinition, 'carrito');
  if (JSON.stringify(motorBefore) !== JSON.stringify(motorAfter) || motorAfter[2].coupon_ordinal !== 2
      || 'coupon_ordinal' in motorAfter[0]) {
    fail('la politica de la ruta motor cambio', { motorBefore, motorAfter });
  }
  results.att1_igual = { a1: a1After, z: zAfter.map((row) => row.ordinal), link: linkAfter.outcome,
    z_paso: z.step_key, motor: motorAfter.map((step) => [step.step_key, step.coupon_ordinal ?? null]) };
}

// ---------------------------------------------------------------------------
// 2. Un carrito nativo de tres toques con el cupon propio en A1 y Z.
// ---------------------------------------------------------------------------
const { A1, A2, Z } = world;
const CART = {
  esquema: 'setter-flujo/v1', flujo: 'carrito_cupon', producto: A.funnel, estado: 'borrador',
  nombre: 'Carrito con cupon propio', dispara: { evento: 'carrito' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [A1, { esperar: '1m' }, A2, { esperar: '2m' }, Z],
  termina: { si: ['compra', 'contesta'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-carrito-cupon', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
if ((await unsupported(CART)) !== null) fail('el carrito con cupon propio abre', await unsupported(CART));
const flowId = await register(A.funnel, 'carrito_cupon');
await cycle([versionItem(flowId, A.funnel, 'carrito_cupon', CART)], 'v1 del carrito con cupon propio');
const proposalId = one(await sql('select proposal_id from public.flow_definitions where flow_id = $1 and version = 1',
  [flowId]), 'propuesta').proposal_id;
const opened = JSON.parse(one(await sql('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
  [flowId, proposalId, A.timezone]), 'abrir').r);
if (!opened.ok || opened.state !== 'cohorte') fail('abrir el carrito con cupon propio', opened);
const maria = await cartFor('cupon-maria');
await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
  [flowId, maria.contact]);
const started = one((await asService(async () => (await db.query(
  'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
  [JSON.stringify(INSTANCE), JSON.stringify({
    fuente: 'hotmart', tipo: 'carrito', evento_id: maria.event, producto_ref: A.funnel, oferta: maria.offer.offer_code,
    contacto_id: maria.contact, momento: maria.cartArgs[6], chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
    destino: maria.phone,
  })])).rows)).filter((row) => row.flow_id === flowId), 'arranque');
if (started.outcome !== 'motor_arranco') fail('el carrito con cupon propio arranca', started);
const caseId = started.recovery_case_id;
const kase = one(await sql('select * from public.recovery_cases where id = $1', [caseId]), 'caso');
const policy = one(await sql('select * from public.followup_policy_versions where policy_key = $1', [kase.policy_key]), 'politica');
const policyShape = policy.steps.map((s) => [s.step_key, s.coupon_ordinal ?? null]);
if (JSON.stringify(policyShape) !== JSON.stringify([['first_contact', 1], ['native_touch_2', null], ['native_touch_3', 2]])) {
  fail('la politica con los cupones', policy.steps);
}
const lead = { label: 'cupon-maria', chatwoot: '620' };
const actions = async () => sql(`select id, step_key, action_type, status, due_at from public.scheduled_actions
  where recovery_case_id = $1 order by created_at`, [caseId]);
const stepCoupon = async (actionId) => asService(async () => (await db.query(
  'select * from public.get_flow_engine_native_step_coupon_v1($1)', [actionId])).rows);
const contextOf = async (actionId) => one(await asService(async () => (await db.query(
  'select * from public.flow_engine_execution_context_v1($1)', [actionId])).rows), 'contexto');
const stable = (row) => JSON.parse(JSON.stringify(row, (key, item) => {
  if (key === 'flow_id') return '<flow_id>';
  if (key === 'coupon_id') return item === null ? null : '<coupon_id>';
  if (key === 'code') return item === null ? null : '<code>';
  if (key === 'expires_at' || key === 'verified_at') return item === null ? null : `<${key}>`;
  return item;
}));

// A1: antes de la siembra, el paso dice su ordinal sin cupon; despues, el codigo vence a las 24 h de A1.
const firstId = started.scheduled_action_id;
const [unseeded] = await stepCoupon(firstId);
if (!unseeded || unseeded.ordinal !== 1 || unseeded.coupon_id !== null) fail('A1 sin sembrar', unseeded);
// Rojo de la siembra: con el trabajador de antes, el primer envio nativo no tiene cupon.
const seededBefore = await withPrevious([CLAIM], async () => {
  await claimCoupons();
  return (await couponsOf(caseId)).length;
});
if (seededBefore !== 0) fail('sin la migracion, A1 nativo no se siembra', seededBefore);
await claimCoupons();
let coupons = await couponsOf(caseId);
if (JSON.stringify(coupons.map((c) => [c.ordinal, c.status, c.discount, c.validity_seconds, c.code_shape]))
    !== JSON.stringify([[1, 'reserved', '0.100', 24 * 3600, true]])) fail('el cupon 1 de A1', coupons);
const [a1Coupon] = await stepCoupon(firstId);
if (a1Coupon.ordinal !== 1 || !a1Coupon.code || a1Coupon.status !== 'reserved') fail('la lectura del cupon de A1', a1Coupon);
const a1Context = await contextOf(firstId);
if (JSON.stringify(a1Context.template_variables) !== JSON.stringify(A1.variables)
    || JSON.stringify(a1Context.template_button) !== JSON.stringify({ link: 'checkout', cupon: 'personal' })) {
  fail('el contexto de A1', a1Context);
}
emitted.a1_context = a1Context;
emitted.a1_coupon = a1Coupon;
const first = await reserve(lead, firstId);
// Rojo del link: con la reserva de antes, el primer envio nativo del carrito no tiene link.
const linkBefore = await withPrevious([RESERVE], async () => {
  try {
    await sql('select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)',
      [first.actionId, first.worker, first.lease, first.attempt.id, ulid(), first.now]);
  } catch (error) {
    return error.message;
  }
  return null;
});
if (linkBefore !== 'recovery_checkout_issuance_requires_cart_first_contact') fail('sin la migracion, A1 nativo sin link', linkBefore);
const a1Link = one(await sql('select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)',
  [first.actionId, first.worker, first.lease, first.attempt.id, ulid(), first.now]), 'link de A1 nativo');
if (a1Link.outcome !== 'reserved' || !a1Link.checkout_url_final.includes('sck=')) fail('el link de A1 nativo', a1Link);
if ((await startNew(first)).outcome !== 'request_started') fail('el arranque de A1 nativo');
await accept(lead, first);

// A2: sin cupon; la lectura no da fila y el link no se emite.
const second = (await actions()).find((row) => row.step_key === 'native_touch_2');
if (!second || second.status !== 'pending') fail('A2 agendado', await actions());
if ((await stepCoupon(second.id)).length !== 0) fail('A2 no lleva cupon');
await sql("update public.scheduled_actions set due_at = now() - interval '1 second' where id = $1", [second.id]);
const secondReserved = await reserve(lead, second.id);
if ((await startNew(secondReserved)).outcome !== 'request_started') fail('el arranque de A2 nativo');
await accept(lead, secondReserved);

// Z: el cupon 2, sembrado a la hora de Z por la rama de los pasos siguientes.
const third = (await actions()).find((row) => row.step_key === 'native_touch_3');
if (!third || third.status !== 'pending') fail('Z agendado', await actions());
await claimCoupons();
coupons = await couponsOf(caseId);
if (JSON.stringify(coupons.map((c) => [c.ordinal, c.validity_seconds, c.step_key]))
    !== JSON.stringify([[1, 24 * 3600, 'first_contact'], [2, 24 * 3600, 'native_touch_3']])) fail('el cupon 2 de Z', coupons);
const [zCoupon] = await stepCoupon(third.id);
if (zCoupon.ordinal !== 2 || !zCoupon.code || zCoupon.code === a1Coupon.code) fail('la lectura del cupon de Z', zCoupon);
const zContext = await contextOf(third.id);
if (zContext.send_ordinal !== 3 || JSON.stringify(zContext.template_variables) !== JSON.stringify(Z.variables)
    || JSON.stringify(zContext.template_button) !== JSON.stringify({ link: 'checkout', cupon: 'personal' })) {
  fail('el contexto de Z', zContext);
}
emitted.z_context = zContext;
emitted.z_coupon = zCoupon;
await sql("update public.scheduled_actions set due_at = now() - interval '1 second' where id = $1", [third.id]);
const thirdReserved = await reserve(lead, third.id);
const zLink = one(await sql('select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)',
  [thirdReserved.actionId, thirdReserved.worker, thirdReserved.lease, thirdReserved.attempt.id, ulid(), thirdReserved.now]),
'link de Z nativo');
if (zLink.outcome !== 'reserved') fail('el link de Z nativo', zLink);
// Nunca mas de dos: otra corrida del trabajador no siembra nada nuevo.
await claimCoupons();
const total = one(await sql('select count(*)::int as n from public.recovery_coupons where recovery_case_id = $1', [caseId]),
  'cupones').n;
if (total !== 2) fail('dos cupones por caso como maximo', total);
results.nativo_a1_y_z = { rojo: { siembra: seededBefore, link: linkBefore }, politica: policyShape, cupones: coupons.map((c) => [c.ordinal, c.step_key, c.validity_seconds]),
  links: [a1Link.outcome, zLink.outcome], total };

// ---------------------------------------------------------------------------
// 2b. Las guardas de la siembra del primer envio nativo (correccion de la
//     revision, 2026-11-13): la baja, el caso comprado, la accion cancelada,
//     fuera de la anticipacion y la accion vencida no siembran; el mismo caso
//     sin nada de eso, si.
// ---------------------------------------------------------------------------
{
  let serial = 0;
  const freshCase = async () => {
    serial += 1;
    const person = await cartFor(`cupon-guarda-${serial}`);
    await sql(`insert into public.flow_cohort_members (flow_id, contact_id, added_by_ref) values ($1, $2, 'dan')`,
      [flowId, person.contact]);
    const row = one((await asService(async () => (await db.query(
      'select * from public.admit_flow_engine_event_v1($1::jsonb, $2::jsonb)',
      [JSON.stringify(INSTANCE), JSON.stringify({
        fuente: 'hotmart', tipo: 'carrito', evento_id: person.event, producto_ref: A.funnel, oferta: person.offer.offer_code,
        contacto_id: person.contact, momento: person.cartArgs[6], chatwoot: { account_id: A.accountId, inbox_id: A.inboxId },
        destino: person.phone,
      })])).rows)).filter((r) => r.flow_id === flowId), `guarda ${serial}`);
    if (row.outcome !== 'motor_arranco') fail(`la guarda ${serial} arranca`, row);
    return { caseId: row.recovery_case_id, actionId: row.scheduled_action_id, contact: person.contact, event: person.event };
  };
  const guards = {
    baja: (c) => sql("update public.contacts set contact_permission = 'opted_out' where id = $1", [c.contact]),
    // El caso ganado por la compra (recovery_cases_check1: purchase_event_id solo con won).
    caso_comprado: async (c) => {
      const purchase = one(await sql(`insert into public.webhook_events (source, external_event_id, event_type, payload,
        processing_status) values ('hotmart', $1, 'PURCHASE_APPROVED', '{}'::jsonb, 'processed') returning id`,
      [`cupon-guarda-compra-${c.caseId}`]), 'compra').id;
      await sql(`update public.recovery_cases set status = 'won', won_at = now(), closed_at = now(), purchase_event_id = $2
        where id = $1`, [c.caseId, purchase]);
    },
    accion_cancelada: (c) => sql("update public.scheduled_actions set status = 'cancelled', terminal_reason = 'prueba' where id = $1",
      [c.actionId]),
    fuera_de_la_anticipacion: (c) => sql(`update public.scheduled_actions set due_at = now() + interval '2 hours',
      expires_at = now() + interval '30 hours' where id = $1`, [c.actionId]),
    accion_vencida: (c) => sql(`update public.scheduled_actions set due_at = now() - interval '2 hours',
      expires_at = now() - interval '1 minute' where id = $1`, [c.actionId]),
    cupon_que_ya_venceria: (c) => sql(`update public.scheduled_actions set due_at = now() - interval '25 hours',
      expires_at = now() + interval '1 hour' where id = $1`, [c.actionId]),
  };
  const seeded = {};
  for (const [label, apply] of Object.entries(guards)) {
    seeded[label] = await inRollback(async () => {
      const c = await freshCase();
      await apply(c);
      await claimCoupons();
      return (await couponsOf(c.caseId)).length;
    });
  }
  seeded.sin_guarda = await inRollback(async () => {
    const c = await freshCase();
    await claimCoupons();
    return (await couponsOf(c.caseId)).length;
  });
  const expected = { baja: 0, caso_comprado: 0, accion_cancelada: 0, fuera_de_la_anticipacion: 0, accion_vencida: 0,
    cupon_que_ya_venceria: 0, sin_guarda: 1 };
  if (JSON.stringify(seeded) !== JSON.stringify(expected)) fail('las guardas de la siembra nativa', { seeded, expected });
  results.guardas_de_la_siembra = seeded;
}

// ---------------------------------------------------------------------------
// 3. El cupon en A2 y Z: ordinales 1 y 2 en los toques siguientes.
// ---------------------------------------------------------------------------
const PLAIN = { plantilla: 'att1_carrito_abandonado_01', idioma: 'es_MX', variables: ['nombre', 'producto'] };
const LATER = { ...CART, pasos: [PLAIN, { esperar: '24h' }, Z, { esperar: '48h' }, Z] };
if ((await unsupported(LATER)) !== null) fail('el cupon en A2 y Z abre', await unsupported(LATER));
const later = (await compile(LATER, 'nativo:carrito')).map((s) => [s.step_key, s.coupon_ordinal ?? null]);
if (JSON.stringify(later) !== JSON.stringify([['first_contact', null], ['native_touch_2', 1], ['native_touch_3', 2]])) {
  fail('los ordinales en A2 y Z', later);
}
results.nativo_a2_y_z = later;

// ---------------------------------------------------------------------------
// 4. Lo que no abre, y el pago rechazado sin cadena si.
// ---------------------------------------------------------------------------
const PERSONAL = { link: 'checkout', cupon: 'personal' };
const withButton = (step, boton) => ({ ...step, boton });
const refusals = {
  tres_cupones: await unsupported({ ...CART, pasos: [A1, { esperar: '1m' }, Z, { esperar: '2m' }, Z] }),
  en_la_compra: await unsupported({ ...CART, dispara: { evento: 'compra' }, entra: { permiso: 'compra', contesto_a: [] },
    pasos: [A1], termina: { si: ['compra'], vence: '1d' } }),
  en_el_formulario: await unsupported({ ...CART, dispara: { evento: 'intencion' } }),
  en_la_cadena: await unsupported({ ...CART, dispara: { evento: 'pago_fallido' }, pasos: [{ condicion:
    { dato: 'tipo_de_rechazo', es: ['sin_saldo'] }, entonces: [Z], si_no: [PLAIN] }] }),
  sin_link_checkout: await unsupported({ ...CART, pasos: [withButton(A1, { cupon: 'personal' })] }),
  boton_de_mas: await unsupported({ ...CART, pasos: [withButton(A1, { ...PERSONAL, texto: 'x' })] }),
};
for (const [label, reason] of Object.entries(refusals)) {
  if (reason !== 'motor_nativo:forma') fail(`${label} no se abre`, reason);
}
const compileRefusals = {
  tres_cupones: await compileError({ ...CART, pasos: [A1, { esperar: '1m' }, Z, { esperar: '2m' }, Z] }, 'nativo:carrito'),
  cuando_despues: await compileError({ ...CART, pasos: [PLAIN, { esperar: '1m' }, A1] }, 'nativo:carrito'),
  vence_sin_cupon: await compileError({ ...CART, pasos: [{ ...PLAIN, variables: ['nombre', 'vence'] }] }, 'nativo:carrito'),
  formulario: await compileError({ ...CART, pasos: [A1] }, 'nativo:intencion'),
};
const wanted = {
  tres_cupones: 'flow_policy_not_compilable:coupon_too_many:native_touch_3',
  cuando_despues: 'flow_policy_not_compilable:variables_differ:native_touch_2',
  vence_sin_cupon: 'flow_policy_not_compilable:variables_differ:first_contact',
  formulario: 'flow_policy_not_compilable:coupon_not_allowed:first_contact',
};
if (JSON.stringify(compileRefusals) !== JSON.stringify(wanted)) fail('lo que no compila', compileRefusals);
const PAY = { ...CART, dispara: { evento: 'pago_fallido' }, pasos: [PLAIN, { esperar: '24h' }, Z] };
const payOpens = await unsupported(PAY);
const pay = (await compile(PAY, 'nativo:pago_fallido')).map((s) => [s.step_key, s.coupon_ordinal ?? null]);
if (payOpens !== null || JSON.stringify(pay) !== JSON.stringify([['first_contact', null], ['native_touch_2', 1]])) {
  fail('el pago rechazado sin cadena con cupon propio', { payOpens, pay });
}
results.no_abre = { base: refusals, compilador: compileRefusals, pago_sin_cadena: pay };

// ---------------------------------------------------------------------------
// 5. Rojo: con las definiciones de antes.
// ---------------------------------------------------------------------------
const red = await withPrevious(CHANGED.slice(0, 3), async () => ({
  base: await unsupported({ ...CART, dispara: { evento: 'pago_fallido' }, pasos: [PLAIN, { esperar: '24h' }, Z] }),
  compilador: await (async () => {
    try {
      await compile(CART, 'nativo:carrito');
    } catch (error) {
      return error.message;
    }
    return null;
  })(),
}));
if (red.base !== 'motor_nativo:forma' || red.compilador !== 'flow_policy_not_compilable:coupon_not_allowed:first_contact') {
  fail('sin la migracion, el cupon propio no abre ni compila', red);
}
results.rojo = red;

// ---------------------------------------------------------------------------
// El fixture para el bridge.
// ---------------------------------------------------------------------------
const fixture = {
  _capture: {
    origen: 'validate_e483_native_personal_coupon.mjs: la cadena entera de migraciones (PGlite), el formulario y el '
      + 'carrito capturados de ATT1 por las admisiones portables; un carrito nativo con A1 y Z de ATT1 con cupon propio',
    encargo: 'E-483 C2',
    regenerar: 'cd tests/sql/followup_engine && node validate_e483_native_personal_coupon.mjs --emit',
  },
  ...stable(emitted),
};
if (EMIT) {
  writeFileSync(FIXTURE, `${JSON.stringify(fixture, null, 2)}\n`);
} else if (CHECK) {
  const stored = JSON.parse(readFileSync(FIXTURE, 'utf8'));
  if (JSON.stringify(stored) !== JSON.stringify(fixture)) fail('el fixture del bridge no es el de hoy', { stored, fixture });
}
console.log(JSON.stringify(results, null, 2));
await db.close();
