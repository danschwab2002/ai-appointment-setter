// La etiqueta de un segmento en un flujo nativo, con cualquier disparador
// (migracion 20261103001300, E-316, PR-C de los segmentos; contrato
// docs/contracts/segment-labels-v1.md).
//
// Datos: la definicion del carrito de un toque es la de
// validate_flow_engine_native_cart.mjs (la plantilla de Maribel del catalogo
// reconstruido de la bandeja 13,
// tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json).
// La etiqueta black-november es un ejemplo: la base no conoce los segmentos de
// la instancia (los mira el validador del bridge).
//
// Lo que fija: sin la migracion, un carrito con etiquetar de un segmento es
// motor_nativo:forma (el rojo que la justifica); con ella se abre, y cliente,
// automation_* o una etiqueta que no es texto siguen cerrados con el carrito.
// La compra, el silencio y la cadena del pago rechazado dan lo mismo que antes,
// el compilador da la misma politica con y sin el etiquetar, el ACL no cambia
// y correrla dos veces aborta.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION = '20261103001300_flow_engine_native_segment_label_v1.sql';
const fail = (message, detail) => {
  console.error(`validate_flow_engine_native_segment_label: ${message}`);
  if (detail !== undefined) console.error(JSON.stringify(detail, null, 2));
  process.exit(1);
};
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Lo que venga despues no puede redefinir la forma nativa sin mirar esta prueba.
// La guarda de publicar (20261108004100, E-357) llama a _flow_engine_native_unsupported y no la cambia.
// El nativo de varios toques (20261112210000, E-483) cambia la forma solo con mas de un envio o con una
// condicion despues de un envio: revisada, la etiqueta de un toque y la cadena del pago rechazado dan lo mismo.
// El pago rechazado completo (20261112210300, E-483, C3) cambia solo la rama de la cadena del pago
// rechazado (varios envios por rama, con contesta): revisada, la etiqueta no pasa por ahi.
// El cupon propio en el nativo (20261112210500, E-483, C2) cambia solo lo que mira el boton con
// cupon: personal: revisada, la etiqueta no pasa por ahi.
// El disparo por audiencia (20261112210600, E-483, para E-484) suma el permiso y los toques solo para el
// disparador audiencia: revisada, la etiqueta de los eventos no pasa por ahi.
const ONLY_CALLS = new Set(['20261108004100_flow_change_publish_native_version_guard_v1.sql',
  '20261112210000_e483_native_multitouch_v1.sql', '20261112210300_e483_native_condition_after_send_v1.sql',
  '20261112210500_e483_native_personal_coupon_v1.sql', '20261112210600_e483_native_audience_source_v1.sql', '20261112220000_flow_audience_triggers_v1.sql']);
for (const name of names.slice(position + 1).filter((candidate) => !ONLY_CALLS.has(candidate))) {
  if (readFileSync(join(root, 'supabase/migrations', name), 'utf8').includes('_flow_engine_native_unsupported')) {
    fail(`${name} toca _flow_engine_native_unsupported: revisar esta prueba`);
  }
}

const templates = JSON.parse(readFileSync(join(root, 'tests/fixtures',
  'chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json'), 'utf8'));
const list = Array.isArray(templates) ? templates : (templates.message_templates ?? templates.templates ?? []);
const maribel = list.find((template) => template && template.name && template.language);
if (maribel === undefined) fail('el fixture de plantillas no trae ninguna');

const boot = async (files) => {
  const db = new PGlite();
  await db.waitReady;
  await db.exec(`
    create role anon nologin;
    create role authenticated nologin;
    create role service_role nologin;
  `);
  for (const file of [join(root, 'supabase/baseline/20260803_public_schema.sql'),
    ...files.map((name) => join(root, 'supabase/migrations', name))]) {
    await db.exec(readFileSync(file, 'utf8').replace(
      /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
  }
  return db;
};

const SEND = { plantilla: maribel.name, idioma: maribel.language, variables: ['nombre', 'producto'] };
const CART = {
  esquema: 'setter-flujo/v1', flujo: 'carrito_bn', producto: 'att1', estado: 'borrador', nombre: 'Carrito BN',
  dispara: { evento: 'carrito' }, entra: { permiso: 'formulario', contesto_a: [] },
  pasos: [SEND],
  termina: { si: ['compra'], vence: '1d' }, horario: 'siempre',
  topes: { bolsa: 'att1-carrito-nativo', por_dia: 150, total: 3000,
    repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '0m' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
};
const PURCHASE = {
  ...CART, flujo: 'bienvenida', dispara: { evento: 'compra' }, entra: { permiso: 'compra', contesto_a: [] },
  termina: { si: [], vence: '1d' }, topes: { por_dia: 'sin_tope', total: 'sin_tope',
    repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } },
};
const SILENCE = {
  ...CART, flujo: 'seguimiento', dispara: { silencio: { lo_ultimo: 'agente', desde: '24h', hasta: '72h' } },
  entra: { permiso: 'conversacion', contesto_a: 'cualquier_plantilla' },
  termina: { si: ['compra', 'contesta'] },
  topes: { por_dia: 'sin_tope', total: 'sin_tope', repeticion: { una_por: 'conversacion', otra_despues_de: 'nunca' } },
};
const withSteps = (definition, pasos) => ({ ...definition, pasos });
const CASES = {
  carrito_solo: CART,
  carrito_segmento_primero: withSteps(CART, [{ etiquetar: 'black-november' }, SEND]),
  carrito_segmento_despues: withSteps(CART, [SEND, { etiquetar: 'black-november' }]),
  carrito_espera_segmento: withSteps(CART, [{ esperar: '15m' }, { etiquetar: 'black-november' }, SEND]),
  carrito_cliente: withSteps(CART, [SEND, { etiquetar: 'cliente' }]),
  carrito_pausa: withSteps(CART, [{ etiquetar: 'automation_paused' }, SEND]),
  carrito_no_texto: withSteps(CART, [{ etiquetar: 7 }, SEND]),
  carrito_cliente_y_segmento: withSteps(CART, [{ etiquetar: 'black-november' }, SEND, { etiquetar: 'cliente' }]),
  pago_fallido_segmento: { ...withSteps(CART, [{ etiquetar: 'black-november' }, SEND]), dispara: { evento: 'pago_fallido' } },
  intencion_segmento: { ...withSteps(CART, [{ etiquetar: 'black-november' }, SEND]), dispara: { evento: 'intencion' } },
  compra_cliente: withSteps(PURCHASE, [SEND, { etiquetar: 'cliente' }]),
  compra_segmento: withSteps(PURCHASE, [{ etiquetar: 'black-november' }, SEND]),
  silencio_segmento: withSteps(SILENCE, [{ etiquetar: 'black-november' }, SEND]),
};
const unsupported = async (db) => {
  const out = {};
  for (const [name, definition] of Object.entries(CASES)) {
    out[name] = (await db.query('select public._flow_engine_native_unsupported($1::jsonb) as r',
      [JSON.stringify(definition)])).rows[0].r;
  }
  return out;
};
const compile = async (db, definition) => {
  try {
    const row = (await db.query(`
      select p.steps, p.grace_period::text as grace, p.expires_after::text as expires
        from public._flow_engine_compile_policy_core($1::jsonb, $2, 1, 'nativo:carrito', 'America/Mexico_City') p
    `, [JSON.stringify(definition), '00000000-0000-4000-8000-0000000e3160'])).rows[0];
    return { ok: row };
  } catch (error) {
    return { error: error.message };
  }
};
const acl = async (db) => (await db.query(`
  select p.oid::regprocedure::text as signature, coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile
    from pg_proc p
   where p.oid in ('public._flow_engine_native_unsupported(jsonb)'::regprocedure,
                   'public.flow_engine_native_step_tail_v1(uuid)'::regprocedure)
   order by 1
`)).rows;

// Antes: la cadena sin esta migracion.
const before = await boot(names.slice(0, position));
const beforeShapes = await unsupported(before);
const beforeAcl = await acl(before);
await before.close();
for (const name of ['carrito_segmento_primero', 'pago_fallido_segmento', 'intencion_segmento']) {
  if (beforeShapes[name] !== 'motor_nativo:forma') fail(`sin la migracion, ${name} tendria que ser motor_nativo:forma`, beforeShapes);
}

// Despues: la cadena entera.
const db = await boot(names);
const afterShapes = await unsupported(db);
const OPENED = ['carrito_segmento_primero', 'carrito_segmento_despues', 'carrito_espera_segmento',
  'pago_fallido_segmento', 'intencion_segmento'];
for (const name of OPENED) {
  if (afterShapes[name] !== null) fail(`${name} tendria que abrirse`, afterShapes);
}
for (const name of ['carrito_cliente', 'carrito_pausa', 'carrito_no_texto', 'carrito_cliente_y_segmento']) {
  if (afterShapes[name] !== 'motor_nativo:forma') fail(`${name} sigue cerrado con el carrito`, afterShapes);
}
// Lo que no es la etiqueta de un segmento con otro disparador da lo mismo que antes.
for (const name of Object.keys(CASES).filter((n) => !OPENED.includes(n))) {
  if (afterShapes[name] !== beforeShapes[name]) fail(`${name} cambio`, { before: beforeShapes[name], after: afterShapes[name] });
}
// El compilador no ve etiquetar: la unica funcion que compila un flujo nativo es la admision, y le saca los
// pasos etiquetar antes (20261024000200, "lo ejecuta el bridge"). Con esa misma resta, la politica del
// carrito con el segmento antes, o despues de la espera, es la del carrito sin el paso.
{
  const compilers = (await db.query(`
    select p.proname, p.prosrc from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public' and p.prokind = 'f' and p.proname <> '_flow_engine_compile_policy_core'
       and p.prosrc like '%_flow_engine_compile_policy_core(%' and p.prosrc like '%''nativo:''%'`)).rows;
  if (compilers.length !== 1 || compilers[0].proname !== '_flow_engine_admit_native_flow'
      || !compilers[0].prosrc.includes("where not (jsonb_typeof(step.value) = 'object' and step.value ? 'etiquetar')")) {
    fail('la admision nativa ya no le saca etiquetar a la definicion antes de compilar', compilers.map((r) => r.proname));
  }
  const sendable = (definition) => withSteps(definition, definition.pasos.filter((step) => !('etiquetar' in step)));
  const plain = await compile(db, CART);
  if (plain.error !== undefined) fail('el carrito de un toque compila', plain);
  for (const name of ['carrito_segmento_primero', 'carrito_segmento_despues']) {
    const got = await compile(db, sendable(CASES[name]));
    if (JSON.stringify(got) !== JSON.stringify(plain)) fail(`${name} compila distinto`, { plain, got });
  }
  const waited = await compile(db, withSteps(CART, [{ esperar: '15m' }, SEND]));
  if (waited.error !== undefined
      || JSON.stringify(await compile(db, sendable(CASES.carrito_espera_segmento))) !== JSON.stringify(waited)) {
    fail('el segmento despues de la espera compila distinto', waited);
  }
}
if (JSON.stringify(await acl(db)) !== JSON.stringify(beforeAcl)) fail('cambio el ACL', [beforeAcl, await acl(db)]);
// Correrla dos veces aborta sin cambiar nada.
try {
  await db.exec(readFileSync(join(root, 'supabase/migrations', MIGRATION), 'utf8'));
  fail('correrla dos veces tendria que abortar');
} catch (error) {
  if (!String(error.message).includes('flow_engine_native_segment_label_already_applied')) {
    fail('la segunda corrida aborto por otra cosa', error.message);
  }
}
await db.exec('rollback').catch(() => {});
if (JSON.stringify(await unsupported(db)) !== JSON.stringify(afterShapes)) fail('la segunda corrida cambio la forma');
await db.close();
console.log(`validate_flow_engine_native_segment_label: ok (${Object.keys(CASES).length} formas)`);
