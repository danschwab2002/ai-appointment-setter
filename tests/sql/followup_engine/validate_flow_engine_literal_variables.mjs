// El texto fijo en las variables de un paso nativo (migracion 20261127300000, F4 del mapa «Frenos ante Meta y
// calentamiento», D15, 2026-11-27). Contrato: docs/contracts/flow-engine-v2.md §36.
//
// Por que: los borradores de utilidad de Maribel (27/11) dejan lo que cambia de un flujo a otro en un marcador,
// y hoy el compilador de la politica solo acepta nombres de variables_de: {"texto": "…"} es variables_differ.
//
// Datos: las formas vivas de _flow_engine_native_steps (la cadena entera de migraciones) y los casos compartidos
// del compilador (tests/fixtures/flow_engine/compile_cases.json), que tienen que compilar igual. Los textos son
// de ficcion, con la forma de los borradores (sin datos de nadie).
//
// Prueba:
//   0. la forma: cambia solo _flow_engine_native_steps y el compilador, con sus marcas, mismos permisos; la ficha
//      no suma texto_fijo; correrla dos veces aborta con 55000 sin tocar nada;
//   1. rojo: sin la migracion, un paso con {"texto": …} no compila (variables_differ);
//   2. con la migracion compila en la compra, el formulario, el carrito (tambien con el cupon propio), el pago
//      rechazado y la audiencia; en la ficha no; un texto mal formado (vacio, 401 caracteres, salto de linea,
//      tab, cinco espacios, «{{», otra clave, no texto) sigue siendo variables_differ; los nombres siguen sin
//      repetirse y dos textos iguales si;
//   3. el contexto del envio: lo que lee el bridge (flow_engine_execution_context_v1, jsonb_array_elements_text)
//      es el texto JSON del objeto, que el bridge decodifica (tests/fixtures/flow_engine/literal_variables_context_20261127.json).
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION = '20261127300000_flow_engine_literal_variables_v1.sql';
const STEPS_FN = '_flow_engine_native_steps(text)';
const CORE_FN = '_flow_engine_compile_policy_core(jsonb,text,integer,text,text)';
const FORMS = ['nativo:compra', 'nativo:carrito', 'nativo:intencion', 'nativo:pago_fallido', 'nativo:audiencia',
  'nativo:ficha_impresa', 'nativo:silencio', 'otra'];
const WITH_LITERAL = ['nativo:compra', 'nativo:carrito', 'nativo:intencion', 'nativo:pago_fallido', 'nativo:audiencia'];
const fail = (message, detail) => {
  console.error(`validate_flow_engine_literal_variables: ${message}`);
  if (detail !== undefined) console.error(JSON.stringify(detail, null, 2));
  process.exit(1);
};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
if (names.slice(position + 1).length !== 0) fail(`despues de ${MIGRATION} no va ninguna`, names.slice(position + 1));

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
const shapes = async (db) => {
  const out = {};
  for (const form of FORMS) {
    out[form] = (await db.query('select public._flow_engine_native_steps($1) as s', [form])).rows[0].s;
  }
  return out;
};

const FLOW_ID = '00000000-0000-4000-8000-0000000f4001';
// Texto de ficcion con la forma de U3 de los borradores (sin datos de nadie, 217 caracteres).
const TEXT = 'Tu inscripción quedó a medio camino y todavía estás a tiempo: con el acceso empezás hoy mismo, ' +
  'a tu ritmo, con el acompañamiento del equipo y las clases grabadas para verlas cuando quieras, sin apuro ' +
  'ninguno.';
const definition = (steps, extra = {}) => ({
  esquema: 'setter-flujo/v1', flujo: 'prueba_texto', producto: 'el-protocolo', estado: 'borrador',
  nombre: 'Prueba del texto fijo', dispara: { evento: 'intencion' }, entra: { permiso: 'formulario' },
  pasos: steps, termina: { si: ['compra', 'contesta'], vence: '3d' }, horario: 'siempre',
  topes: { por_dia: 'sin_tope', total: 'sin_tope', repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] }, ...extra,
});
const send = (variables, extra = {}) => ({ plantilla: 'maribel_pago_no_completado_01', idioma: 'es_MX', variables, ...extra });
const compile = async (db, kind, steps) => {
  try {
    const row = (await db.query(`
      select p.steps from public._flow_engine_compile_policy_core($1::jsonb, $2, 1, $3, 'America/Mexico_City') p
    `, [JSON.stringify(definition(steps)), FLOW_ID, kind])).rows[0];
    return { ok: row.steps };
  } catch (error) {
    return { error: error.message };
  }
};
const DIFFER = 'flow_policy_not_compilable:variables_differ:first_contact';
const literal = (text) => ({ texto: text });

const COMPILE_CASES = JSON.parse(readFileSync(join(root, 'tests/fixtures/flow_engine/compile_cases.json'), 'utf8')).cases;
const legacyCompiles = async (db) => {
  const out = [];
  for (const item of COMPILE_CASES) {
    const input = item.input;
    try {
      const row = (await db.query(`
        select p.steps, p.grace_period::text as grace, p.expires_after::text as expires
          from public._flow_engine_compile_policy_core($1::jsonb, $2, $3, $4, $5) p
      `, [JSON.stringify(input.definition), input.flow_id, input.version, input.legacy_kind,
        input.fixed_zone])).rows[0];
      out.push({ id: item.id, row });
    } catch (error) {
      out.push({ id: item.id, error: error.message });
    }
  }
  return out;
};

// ---------------------------------------------------------------------------
// 1. Antes: el rojo que la justifica.
// ---------------------------------------------------------------------------
const before = await boot(names.slice(0, position));
{
  const got = await compile(before, 'nativo:intencion', [send(['nombre', literal(TEXT)])]);
  if (got.error !== DIFFER) fail('sin la migracion, el texto fijo tendria que dar variables_differ', got);
}
const beforeFns = await snapshot(before);
const beforeShapes = await shapes(before);
const beforeLegacy = await legacyCompiles(before);
const beforeNames = await compile(before, 'nativo:intencion', [send(['nombre', 'producto'])]);

// ---------------------------------------------------------------------------
// 0. La forma de la migracion.
// ---------------------------------------------------------------------------
await before.exec(readFileSync(join(root, 'supabase/migrations', MIGRATION), 'utf8'));
const db = before;
{
  const after = await snapshot(db);
  const changed = [...beforeFns.keys()].filter((s) => after.get(s)?.definition !== beforeFns.get(s).definition);
  if (JSON.stringify(changed.sort()) !== JSON.stringify([CORE_FN, STEPS_FN].sort())) {
    fail('la migracion cambia solo las formas y el compilador', changed);
  }
  if ([...after.keys()].length !== [...beforeFns.keys()].length) fail('la migracion no suma funciones');
  for (const signature of changed) {
    const was = beforeFns.get(signature);
    const now = after.get(signature);
    if (was.acl !== now.acl || was.prosecdef !== now.prosecdef || was.provolatile !== now.provolatile) {
      fail(`cambio el ACL o los atributos de ${signature}`, [was.acl, now.acl]);
    }
  }
  if (!after.get(STEPS_FN).definition.includes('flow_literal_variables_steps')) fail('falta la marca de las formas');
  if (!after.get(CORE_FN).definition.includes('flow_literal_variables: el texto fijo')) fail('falta la marca del compilador');
  const shapesAfter = await shapes(db);
  for (const form of FORMS) {
    const was = beforeShapes[form];
    const now = shapesAfter[form];
    if (WITH_LITERAL.includes(form)) {
      if (!Array.isArray(now) || now.length !== was.length) fail(`${form}: la forma cambia de largo`, now);
      now.forEach((slot, index) => {
        const { texto_fijo: flag, ...rest } = slot;
        if (flag !== true || JSON.stringify(rest) !== JSON.stringify(was[index])) {
          fail(`${form}: cada toque suma solo texto_fijo`, [was[index], slot]);
        }
      });
    } else if (JSON.stringify(now) !== JSON.stringify(was)) {
      fail(`${form}: la forma no cambia`, [was, now]);
    }
  }
  try {
    await db.exec(readFileSync(join(root, 'supabase/migrations', MIGRATION), 'utf8'));
    fail('correrla dos veces tendria que abortar');
  } catch (error) {
    if (!String(error.message).includes('flow_engine_literal_variables_already_applied')) {
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
// 2. El compilador.
// ---------------------------------------------------------------------------
let accepted = 0;
let refused = 0;
const expectOk = async (kind, steps, what) => {
  const got = await compile(db, kind, steps);
  if (got.error !== undefined) fail(`${what} tendria que compilar`, got);
  accepted += 1;
  return got.ok;
};
const expectDiffer = async (kind, steps, what, code = DIFFER) => {
  const got = await compile(db, kind, steps);
  if (got.error !== code) fail(`${what} tendria que dar ${code}`, got);
  refused += 1;
};
for (const kind of ['nativo:compra', 'nativo:intencion', 'nativo:carrito', 'nativo:pago_fallido', 'nativo:audiencia']) {
  const steps = await expectOk(kind, [send(['nombre', literal(TEXT)])], `${kind} con nombre y un texto fijo`);
  if (steps.length !== 1 || steps[0].step_key !== 'first_contact') fail(`${kind}: un solo paso first_contact`, steps);
  await expectOk(kind, [send([literal(TEXT)])], `${kind} solo con un texto fijo`);
  await expectOk(kind, [send([literal(TEXT), 'producto', literal(TEXT)])], `${kind} con dos textos iguales`);
}
// Los tres toques del formulario, el texto fijo en el segundo y el tercero.
{
  const steps = await expectOk('nativo:intencion', [
    send(['nombre', 'producto']), { esperar: '15m' }, send(['nombre', literal(TEXT)]), { esperar: '20m' },
    send([literal('Te guardamos el lugar hasta mañana.')])], 'los tres toques del formulario');
  if (JSON.stringify(steps.map((step) => step.step_key)) !== JSON.stringify(['first_contact', 'native_touch_2', 'native_touch_3'])) {
    fail('los tres toques', steps);
  }
}
// El carrito con el cupon propio: el texto fijo junto a las variables del cupon.
await expectOk('nativo:carrito', [send(['nombre', 'precio', literal(TEXT)], { boton: { link: 'checkout', cupon: 'personal' } })],
  'el carrito con el cupon propio y un texto fijo');
// La audiencia con la hora del evento.
await expectOk('nativo:audiencia', [send(['nombre', 'hora_evento', literal(TEXT)])], 'la audiencia con hora_evento');
// La ficha arma sus parametros por nombre (D-F4.3): no.
await expectDiffer('nativo:ficha_impresa', [send(['nombre', literal(TEXT)])], 'la ficha con un texto fijo');
// Los nombres siguen sin repetirse, y un nombre que no es de la forma sigue afuera.
await expectDiffer('nativo:intencion', [send(['nombre', 'nombre', literal(TEXT)])], 'un nombre repetido');
await expectDiffer('nativo:intencion', [send(['precio', literal(TEXT)])], 'precio sin cupon');
await expectDiffer('nativo:intencion', [send([])], 'una lista vacia');
const BAD = [
  ['vacio', literal('')], ['en blanco', literal('   ')], ['401 caracteres', literal('a'.repeat(401))],
  ['salto de linea', literal('hola\nchau')], ['retorno', literal('hola\rchau')], ['tab', literal('hola\tchau')],
  ['cinco espacios', literal('hola     chau')], ['un marcador', literal('hola {{1}}')],
  ['otra clave', { texto: 'hola', de: 'nombre' }], ['sin texto', { de: 'nombre' }], ['no es texto', { texto: 7 }],
  ['un numero', 7], ['un arreglo', ['hola']],
];
for (const [what, value] of BAD) {
  await expectDiffer('nativo:intencion', [send(['nombre', value])], `un texto fijo ${what}`);
}
await expectOk('nativo:intencion', [send([literal('a'.repeat(400))])], '400 caracteres');
await expectOk('nativo:intencion', [send([literal('hola    chau')])], 'cuatro espacios');
// La forma de antes compila igual, y los casos del legado tambien.
if (JSON.stringify(await compile(db, 'nativo:intencion', [send(['nombre', 'producto'])])) !== JSON.stringify(beforeNames)) {
  fail('nombre y producto compilan distinto que antes');
}
{
  const afterLegacy = await legacyCompiles(db);
  const differ = afterLegacy.filter((item, index) => JSON.stringify(item) !== JSON.stringify(beforeLegacy[index]));
  if (differ.length !== 0) fail('compile_cases.json compila distinto despues de la migracion', differ);
}

// ---------------------------------------------------------------------------
// 3. Lo que lee el bridge: el texto JSON del objeto, en su posicion.
// ---------------------------------------------------------------------------
{
  const fixture = JSON.parse(readFileSync(join(root, 'tests/fixtures/flow_engine/literal_variables_context_20261127.json'), 'utf8'));
  const row = (await db.query(`
    select array(select jsonb_array_elements_text(coalesce(send.variables, '[]'::jsonb))) as template_variables
      from public._flow_engine_sends($1::jsonb) send where send.ordinal = 1
  `, [JSON.stringify(definition([send(fixture.definition_variables)]))])).rows[0];
  if (JSON.stringify(row.template_variables) !== JSON.stringify(fixture.template_variables)) {
    fail('el contexto del envio no es el del fixture', [row.template_variables, fixture.template_variables]);
  }
  const decoded = row.template_variables.map((item) => (item.startsWith('{') ? JSON.parse(item) : item));
  if (JSON.stringify(decoded) !== JSON.stringify(fixture.definition_variables)) fail('el texto no se decodifica', decoded);
}
await db.close();
console.log(`validate_flow_engine_literal_variables: ok (${accepted} compilan, ${refused} no, ${COMPILE_CASES.length} casos del legado iguales)`);
