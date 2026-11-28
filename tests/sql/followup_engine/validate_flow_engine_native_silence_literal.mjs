// El texto fijo como tercera variable del seguimiento por silencio nativo (migracion 20261128100000, U4 del mapa
// «Frenos ante Meta y calentamiento», D17). Contrato: docs/contracts/flow-engine-v2.md §37.
//
// Por que: la plantilla de utilidad del seguimiento de Maribel (maribel_seguimiento_consulta_01, borradores del
// 27/11) deja lo comercial en su {{3}}, y la base exige que el paso del silencio tenga exactamente
// variables = [nombre, producto] (flow_engine_native_silence_shape, 20261101000200): un silencio con
// [nombre, producto, {"texto": …}] es motor_nativo:forma al publicar y blocked_flow_not_running al reservar.
//
// Datos: la cadena entera de migraciones y el seguimiento de Maribel escrito como nativo (el FOLLOWUP de
// tests/test_flow_engine_native_silence.py). El texto es el de la variable de U4 de los borradores (sin datos de
// nadie).
//
// Prueba:
//   0. la forma: cambia solo _flow_engine_native_unsupported, con su marca, mismos permisos y atributos, sin
//      funciones nuevas; correrla dos veces aborta con 55000 sin tocar nada;
//   1. rojo: sin la migracion, el silencio con el texto fijo tercero es motor_nativo:forma;
//   2. con la migracion se abre; un texto mal formado (vacio, en blanco, 401 caracteres, salto de linea, retorno,
//      tab, cinco espacios, «{{», otra clave, no texto), otro orden, dos textos, el texto solo con el nombre o
//      cuatro variables siguen siendo motor_nativo:forma; 400 caracteres y cuatro espacios pasan;
//   3. lo de antes no cambia: el silencio con nombre y producto, los otros rojos del silencio y los disparadores
//      de eventos (con y sin texto fijo) dan lo mismo que antes;
//   4. la reserva (claim_flow_engine_silence_v1) y la publicacion (_flow_change_new_flow_blocker) siguen
//      llamando a esta funcion: el cambio les llega sin tocarlas.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION = '20261128100000_flow_engine_native_silence_literal_v1.sql';
const UNSUPPORTED = '_flow_engine_native_unsupported(jsonb)';
const fail = (message, detail) => {
  console.error(`validate_flow_engine_native_silence_literal: ${message}`);
  if (detail !== undefined) console.error(JSON.stringify(detail, null, 2));
  process.exit(1);
};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
if (position !== names.length - 1) fail(`${MIGRATION} es la ultima`, names.slice(position + 1));

const boot = async (files) => {
  const db = new PGlite();
  await db.waitReady;
  await db.exec(`
    create role anon nologin;
    create role authenticated nologin;
    create role service_role nologin;
    alter default privileges in schema public grant execute on functions to anon, authenticated;
    alter default privileges in schema public grant all on functions to service_role;
    grant usage on schema public to service_role, anon;
  `);
  for (const file of [join(root, 'supabase/baseline/20260803_public_schema.sql'),
    ...files.map((name) => join(root, 'supabase/migrations', name))]) {
    await db.exec(readFileSync(file, 'utf8').replace(
      /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
  }
  return db;
};
const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));

// El texto de la variable de U4 (230 caracteres, borradores del 27/11).
const TEXT = 'queríamos retomar nuestra conversación. Para ayudarte a dar ese paso, te reservamos un '
  + '*10 % de descuento*: toca el botón de abajo y queda activado al instante. Si todavía tienes alguna duda, '
  + 'responde a este mensaje y te ayudamos.';
const literal = (text) => ({ texto: text });
// El seguimiento de Maribel escrito como nativo (FOLLOWUP de tests/test_flow_engine_native_silence.py).
const followup = (variables, extra = {}) => ({
  esquema: 'setter-flujo/v1', flujo: 'seguimiento_cupon', producto: 'att1', estado: 'borrador',
  nombre: 'Seguimiento con cupón',
  dispara: { silencio: { lo_ultimo: 'agente', desde: '24h', hasta: '72h', conversaciones: ['abierta', 'resuelta'] } },
  entra: { permiso: 'conversacion', contesto_a: 'cualquier_plantilla' },
  pasos: [{ plantilla: 'maribel_seguimiento_consulta_01', idioma: 'es_MX', variables,
    boton: { link: 'checkout', cupon: 'PROTOCOLO10' } }],
  termina: { si: ['compra', 'contesta'] },
  horario: { zona: 'America/Mexico_City', desde: '09:00', hasta: '20:59' },
  topes: { bolsa: 'seguimiento-cupon', por_dia: 'sin_tope', total: 'sin_tope',
    repeticion: { una_por: 'conversacion', otra_despues_de: 'nunca' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] }, ...extra,
});
// Un disparo de evento (el formulario), para ver que fuera del silencio no cambia nada.
const intent = (variables) => ({
  esquema: 'setter-flujo/v1', flujo: 'primer_contacto', producto: 'att1', estado: 'borrador',
  nombre: 'Primer contacto', dispara: { evento: 'intencion' }, entra: { permiso: 'formulario' },
  pasos: [{ plantilla: 'maribel_intento_compra_01', idioma: 'es_MX', variables }],
  termina: { si: ['compra'], vence: '1d' }, horario: 'siempre',
  topes: { por_dia: 'sin_tope', total: 'sin_tope', repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] },
});
const unsupported = async (db, definition) => (await db.query(
  `select public.${UNSUPPORTED.replace('(jsonb)', '')}($1::jsonb) as r`, [JSON.stringify(definition)])).rows[0].r;
const FORM = 'motor_nativo:forma';

// Los casos que no cambian: deben dar lo mismo antes y despues.
const UNCHANGED = [
  ['nombre y producto', followup(['nombre', 'producto'])],
  ['solo el nombre', followup(['nombre'])],
  ['sin boton', (() => { const d = followup(['nombre', 'producto']); delete d.pasos[0].boton; return d; })()],
  ['cupon personal', (() => { const d = followup(['nombre', 'producto']); d.pasos[0].boton.cupon = 'personal'; return d; })()],
  ['sin contesta', followup(['nombre', 'producto'], { termina: { si: ['compra'] } })],
  ['otra repeticion', followup(['nombre', 'producto'], { topes: { por_dia: 'sin_tope', total: 'sin_tope',
    repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } } })],
  ['el formulario con nombre y producto', intent(['nombre', 'producto'])],
  ['el formulario con un texto fijo', intent(['nombre', literal(TEXT)])],
  ['el formulario con nombre, producto y un texto fijo', intent(['nombre', 'producto', literal(TEXT)])],
];
const BAD = [
  ['vacio', ['nombre', 'producto', literal('')]],
  ['en blanco', ['nombre', 'producto', literal('   ')]],
  ['401 caracteres', ['nombre', 'producto', literal('a'.repeat(401))]],
  ['salto de linea', ['nombre', 'producto', literal('hola\nchau')]],
  ['retorno', ['nombre', 'producto', literal('hola\rchau')]],
  ['tab', ['nombre', 'producto', literal('hola\tchau')]],
  ['cinco espacios', ['nombre', 'producto', literal('hola     chau')]],
  ['un marcador', ['nombre', 'producto', literal('hola {{1}}')]],
  ['otra clave', ['nombre', 'producto', { texto: 'hola', de: 'nombre' }]],
  ['sin texto', ['nombre', 'producto', { de: 'nombre' }]],
  ['no es texto', ['nombre', 'producto', { texto: 7 }]],
  ['un texto suelto', ['nombre', 'producto', TEXT]],
  ['otro orden', ['nombre', literal(TEXT), 'producto']],
  ['producto primero', ['producto', 'nombre', literal(TEXT)]],
  ['el texto con el nombre solo', ['nombre', literal(TEXT)]],
  ['dos textos', ['nombre', 'producto', literal(TEXT), literal(TEXT)]],
  ['cuatro variables', ['nombre', 'producto', literal(TEXT), 'precio']],
  ['el texto solo', [literal(TEXT)]],
];

// ---------------------------------------------------------------------------
// 1. Antes: el rojo que la justifica.
// ---------------------------------------------------------------------------
const db = await boot(names.slice(0, position));
if (await unsupported(db, followup(['nombre', 'producto', literal(TEXT)])) !== FORM) {
  fail('sin la migracion, el silencio con un texto fijo tendria que ser motor_nativo:forma');
}
if (await unsupported(db, followup(['nombre', 'producto'])) !== null) {
  fail('sin la migracion, el silencio con nombre y producto se abre');
}
const beforeFns = await snapshot(db);
const beforeUnchanged = [];
for (const [what, definition] of UNCHANGED) beforeUnchanged.push([what, await unsupported(db, definition)]);

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
await db.exec(readFileSync(join(root, 'supabase/migrations', MIGRATION), 'utf8'));
const after = await snapshot(db);
{
  const changed = [...beforeFns.keys()].filter((s) => after.get(s)?.definition !== beforeFns.get(s).definition);
  if (JSON.stringify(changed) !== JSON.stringify([UNSUPPORTED])) fail('la migracion cambia solo la funcion de la forma', changed);
  if (after.size !== beforeFns.size) fail('la migracion no suma funciones');
  const was = beforeFns.get(UNSUPPORTED);
  const now = after.get(UNSUPPORTED);
  if (was.acl !== now.acl || was.prosecdef !== now.prosecdef || was.provolatile !== now.provolatile) {
    fail('cambio el ACL o los atributos de la funcion', [was, now].map(({ acl, prosecdef, provolatile }) => ({ acl, prosecdef, provolatile })));
  }
  if (!now.definition.includes('flow_silence_literal: el texto fijo')) fail('falta la marca');
  try {
    await db.exec(readFileSync(join(root, 'supabase/migrations', MIGRATION), 'utf8'));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (!String(error.message).includes('flow_engine_native_silence_literal_already_applied')) {
      fail('la segunda corrida aborto por otra cosa', error.message);
    }
  }
  await db.exec('rollback').catch(() => {});
  const again = await snapshot(db);
  for (const [signature, row] of after) {
    if (again.get(signature)?.definition !== row.definition) fail('la segunda corrida cambio algo', signature);
  }
}

// ---------------------------------------------------------------------------
// 2. El silencio con el texto fijo tercero.
// ---------------------------------------------------------------------------
let opened = 0;
let refused = 0;
for (const [what, variables] of [
  ['el texto de U4', ['nombre', 'producto', literal(TEXT)]],
  ['400 caracteres', ['nombre', 'producto', literal('a'.repeat(400))]],
  ['cuatro espacios', ['nombre', 'producto', literal('hola    chau')]],
  ['un caracter', ['nombre', 'producto', literal('a')]],
]) {
  const got = await unsupported(db, followup(variables));
  if (got !== null) fail(`${what}: el silencio con el texto fijo tercero se abre`, got);
  opened += 1;
}
for (const [what, variables] of BAD) {
  const got = await unsupported(db, followup(variables));
  if (got !== FORM) fail(`${what}: tendria que seguir siendo ${FORM}`, got);
  refused += 1;
}
// La forma sigue mirando el resto del paso con el texto: sin boton o con el cupon personal, no.
{
  const noButton = followup(['nombre', 'producto', literal(TEXT)]);
  delete noButton.pasos[0].boton;
  if (await unsupported(db, noButton) !== FORM) fail('el texto fijo no salva un paso sin boton');
  const personal = followup(['nombre', 'producto', literal(TEXT)]);
  personal.pasos[0].boton.cupon = 'personal';
  if (await unsupported(db, personal) !== FORM) fail('el texto fijo no salva el cupon personal');
  refused += 2;
}

// ---------------------------------------------------------------------------
// 3. Lo de antes no cambia.
// ---------------------------------------------------------------------------
for (const [index, [what, definition]] of UNCHANGED.entries()) {
  const got = await unsupported(db, definition);
  if (got !== beforeUnchanged[index][1]) fail(`${what}: cambio lo que da la funcion`, [beforeUnchanged[index][1], got]);
}

// ---------------------------------------------------------------------------
// 4. La reserva y la publicacion leen esta funcion.
// ---------------------------------------------------------------------------
{
  const claim = [...after.entries()].find(([signature]) => signature.startsWith('claim_flow_engine_silence_v1('));
  if (claim === undefined || !claim[1].definition.includes('public._flow_engine_native_unsupported(v_definition.definition) is not null')) {
    fail('la reserva nativa del silencio ya no mira _flow_engine_native_unsupported');
  }
  const blocker = [...after.entries()].filter(([signature]) => signature.startsWith('_flow_change_new_flow_blocker('));
  if (blocker.length === 0 || !blocker.every(([, row]) => row.definition.includes('_flow_engine_native_unsupported('))) {
    fail('la publicacion de un flujo nuevo ya no mira _flow_engine_native_unsupported');
  }
}
await db.close();
console.log(`validate_flow_engine_native_silence_literal: ok (${opened} se abren, ${refused} no, ${UNCHANGED.length} iguales que antes)`);
