// El paso de un flujo nativo compila con las variables que declara, de entre
// nombre y producto (migracion 20261026000100, E-115).
//
// Datos: la bienvenida real de ATT1 (att1_bienvenida_01, catalogo de la
// bandeja 11 leido el 2026-10-26,
// tests/fixtures/chatwoot_message_templates_inbox_11_bienvenida_20261026.json)
// tiene un solo {{1}}: el producto. Sin la migracion, el paso con
// ["producto"] no compila (variables_differ:first_contact); con ella, compila,
// y el legado compila igual que antes (compile_cases.json entero).
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION = '20261026000100_flow_engine_native_template_shape_v1.sql';
const fail = (message, detail) => {
  console.error(`validate_flow_engine_native_template_shape: ${message}`);
  if (detail !== undefined) console.error(JSON.stringify(detail, null, 2));
  process.exit(1);
};
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
// Despues van la de la oferta del evento en el caso nativo (E-122), la de la pagina de aprobacion
// (A′ PR 2) y la del carrito nativo (E-161, que reescribe la forma nativa con la de la compra igual):
// ninguna toca el compilador.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261026000200_flow_engine_native_offer_in_context_v1.sql',
  // La pagina de aprobacion (A′ PR 2): el rol del aprobador, sus sesiones, la cohorte con slack y la ruta
  // nativa firmada por slack. No toca lo que esta prueba mira.
  '20261028000100_flow_change_slack_approver_v1.sql',
  '20261028000200_flow_engine_native_cart_v1.sql',
  // Sumar ofertas a un scope publicado (E-163): solo el trigger de pilot_scope_versions.
  '20261028000300_pilot_scope_offer_append_v1.sql',
  // La compra con nuestro link cuenta siempre (E-169): el helper del cierre por el link y el correlador.
  '20261028000400_purchase_by_our_link_counts_v1.sql'])) {
  fail(`despues de ${MIGRATION} van solo la de la oferta en el caso nativo, la de la pagina de aprobacion y la del carrito nativo`, names.slice(position + 1));
}

const fixture = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));
const welcome = fixture('chatwoot_message_templates_inbox_11_bienvenida_20261026.json').message_templates
  .find((template) => template.name === 'att1_bienvenida_01');
const welcomeBody = welcome.components.find((component) => component.type === 'BODY').text;
if (JSON.stringify(welcomeBody.match(/\{\{\d+\}\}/g)) !== JSON.stringify(['{{1}}'])) {
  fail('el fixture de la bienvenida no es el capturado (un solo {{1}})');
}

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

const FLOW_ID = '00000000-0000-4000-8000-0000000e1150';
const definition = (variables) => {
  const step = { plantilla: welcome.name, idioma: welcome.language };
  if (variables !== undefined) step.variables = variables;
  return {
    esquema: 'setter-flujo/v1', flujo: 'bienvenida', producto: 'att1', estado: 'borrador',
    nombre: 'Bienvenida despues de la compra', dispara: { evento: 'compra' },
    entra: { permiso: 'compra', contesto_a: [] }, pasos: [step],
    termina: { si: [], vence: '1d' }, horario: 'siempre',
    topes: { por_dia: 'sin_tope', total: 'sin_tope', repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } },
    prioridad: { le_gana_a: [], no_arranca_mientras: [] },
  };
};
const compileNative = async (db, variables) => {
  try {
    const row = (await db.query(`
      select p.steps from public._flow_engine_compile_policy_core($1::jsonb, $2, 1, 'nativo:compra',
                                                                  'America/Mexico_City') p
    `, [JSON.stringify(definition(variables)), FLOW_ID])).rows[0];
    return { ok: row.steps };
  } catch (error) {
    return { error: error.message };
  }
};
const DIFFER = 'flow_policy_not_compilable:variables_differ:first_contact';
const ACCEPTED = [['producto'], ['nombre'], ['nombre', 'producto'], ['producto', 'nombre']];
const REFUSED = [[], ['nombre', 'nombre'], ['precio'], ['producto', 'precio'], [1], ['nombre', 'producto', 'nombre']];
const acl = async (db) => (await db.query(`
  select p.oid::regprocedure::text as signature, coalesce(p.proacl::text, '') as acl, p.prosecdef, p.provolatile
    from pg_proc p
   where p.oid in ('public._flow_engine_native_steps(text)'::regprocedure,
                   'public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)'::regprocedure)
   order by 1
`)).rows;
const COMPILE_CASES = fixture('flow_engine/compile_cases.json').cases;
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

// Antes: la cadena sin esta migracion. El rojo que la justifica.
const before = await boot(names.slice(0, position));
const beforeWelcome = await compileNative(before, ['producto']);
if (beforeWelcome.error !== DIFFER) fail('sin la migracion, ["producto"] tendria que dar variables_differ', beforeWelcome);
const beforeAcl = await acl(before);
const beforeLegacy = await legacyCompiles(before);
const beforeExact = await compileNative(before, ['nombre', 'producto']);
await before.close();

// Despues: la cadena entera.
const db = await boot(names);
for (const variables of ACCEPTED) {
  const got = await compileNative(db, variables);
  if (got.error !== undefined) fail(`${JSON.stringify(variables)} tendria que compilar`, got);
  if (got.ok.length !== 1 || got.ok[0].step_key !== 'first_contact') fail('un solo paso first_contact', got.ok);
}
for (const variables of REFUSED) {
  const got = await compileNative(db, variables);
  if (got.error !== DIFFER) fail(`${JSON.stringify(variables)} tendria que dar variables_differ`, got);
}
{
  const got = await compileNative(db, undefined);
  if (got.error !== DIFFER) fail('un paso sin variables tendria que dar variables_differ', got);
}
// La forma de antes compila igual.
if (JSON.stringify(await compileNative(db, ['nombre', 'producto'])) !== JSON.stringify(beforeExact)) {
  fail('["nombre", "producto"] compila distinto que antes');
}
// El legado compila igual que antes, caso por caso (los variables_differ incluidos).
{
  const afterLegacy = await legacyCompiles(db);
  const differ = afterLegacy.filter((item, index) => JSON.stringify(item) !== JSON.stringify(beforeLegacy[index]));
  if (differ.length !== 0) fail('compile_cases.json compila distinto despues de la migracion', differ);
}
// La forma nativa suma variables_de y deja variables como estaba.
{
  const shape = (await db.query(`select public._flow_engine_native_steps('nativo:compra') as s`)).rows[0].s;
  if (JSON.stringify(shape[0].variables) !== JSON.stringify(['nombre', 'producto'])
      || JSON.stringify(shape[0].variables_de) !== JSON.stringify(['nombre', 'producto'])) {
    fail('la forma nativa no es la esperada', shape);
  }
  const legacy = (await db.query(`select public._flow_engine_legacy_steps('carrito')::text as s`)).rows[0].s;
  if (legacy.includes('variables_de')) fail('la forma del legado no tiene variables_de', legacy);
}
// El ACL y los atributos de las dos funciones no cambian.
if (JSON.stringify(await acl(db)) !== JSON.stringify(beforeAcl)) fail('cambio el ACL', [beforeAcl, await acl(db)]);
// Correrla dos veces aborta sin cambiar nada.
try {
  await db.exec(readFileSync(join(root, 'supabase/migrations', MIGRATION), 'utf8'));
  fail('correrla dos veces tendria que abortar');
} catch (error) {
  if (!String(error.message).includes('flow_engine_native_template_shape_already_applied')) {
    fail('la segunda corrida aborto por otra cosa', error.message);
  }
}
await db.exec('rollback').catch(() => {});
if ((await compileNative(db, ['producto'])).error !== undefined) fail('despues de la segunda corrida ya no compila');
await db.close();
console.log(`validate_flow_engine_native_template_shape: ok (${ACCEPTED.length} aceptadas, ${REFUSED.length + 1} rechazadas, ${COMPILE_CASES.length} casos del legado iguales)`);
