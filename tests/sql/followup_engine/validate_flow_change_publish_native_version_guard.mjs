// La base no publica una version de un flujo nativo que el motor no ejecuta
// (migracion 20261108004100, E-357; contrato docs/contracts/flow-change-cycle-v1.md 1.12.0).
//
// Por que: desde P28 el Automation Expert cambia la estructura de un nativo. El validador
// mira la regla 14, pero si difiere de la base la version se publicaria y despues ninguna
// admision arrancaria. Es el mismo chequeo que la base ya hace con un item nuevo.
//
// Datos: el mundo de ATT1 de validate_flow_engine_motor_v1.mjs.
//
// Prueba:
//   1. el rojo: antes de la migracion, una version que el motor no ejecuta se publica;
//   2. despues, se rechaza con el motivo de _flow_engine_native_unsupported, deja su evento
//      y no escribe la version ni toca la generacion;
//   3. una version que el motor si ejecuta (repeticion y prioridad nuevas) se publica igual;
//   4. un volver a una version que el motor no ejecuta se rechaza;
//   5. una version del legado con la ruta del motor (el carrito de ATT1, tres toques) se publica como antes;
//   6. correrla dos veces aborta.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, fail, root } from './validate_flow_engine_motor_v1.mjs';

const MIGRATION = '20261108004100_flow_change_publish_native_version_guard_v1.sql';
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`falta la migracion ${MIGRATION}`);
for (const name of names.slice(position + 1)) {
  if (readFileSync(join(root, 'supabase/migrations', name), 'utf8').includes('_flow_change_publish')) {
    fail(`${name} toca _flow_change_publish: revisar esta prueba`);
  }
}
const sql = readFileSync(join(root, 'supabase/migrations', MIGRATION), 'utf8');
const results = {};

const welcomeDefinition = (extra = {}) => ({
  esquema: 'setter-flujo/v1', flujo: 'bienvenida', producto: 'att1', estado: 'borrador', nombre: 'Bienvenida despues de la compra',
  dispara: { evento: 'compra' }, entra: { permiso: 'compra', contesto_a: [] },
  pasos: [{ plantilla: 'att1_bienvenida_01', idioma: 'es_MX', variables: ['nombre', 'producto'] }, { etiquetar: 'cliente' }],
  termina: { si: [], vence: '1d' }, horario: 'siempre',
  topes: { por_dia: 'sin_tope', total: 'sin_tope', repeticion: { una_por: 'persona', otra_despues_de: 'nunca' } },
  prioridad: { le_gana_a: [], no_arranca_mientras: [] }, ...extra,
});
const TWO_SENDS = { pasos: [{ plantilla: 'att1_bienvenida_01', idioma: 'es_MX', variables: ['nombre', 'producto'] },
  { esperar: '1h' }, { plantilla: 'att1_bienvenida_01', idioma: 'es_MX', variables: ['nombre', 'producto'] }] };

const build = async () => {
  const world = await buildWorld();
  const { db, A, INSTANCE, register, rpc } = world;
  const welcome = await register(A.funnel, 'bienvenida');
  await world.cycle([{ ...world.versionItem(welcome, A.funnel, 'bienvenida', welcomeDefinition({ producto: A.funnel })),
    ok_del_cliente: { quien: 'juan', medio: 'whatsapp', fecha: '2026-10-15' } }], 'v1 de bienvenida');
  const { proposal_id: proposal } = (await db.query(
    'select proposal_id from public.flow_definitions where flow_id = $1 and version = 1', [welcome])).rows[0];
  const opened = JSON.parse((await db.query('select public._flow_engine_open_native_flow($1, $2, $3)::text as r',
    [welcome, proposal, A.timezone])).rows[0].r);
  if (!opened.ok) fail('abrir la bienvenida', opened);

  const VALIDATOR = { contract: 'setter-flujo/v1', rules_version: 2, phase: 'publish', actor: 'operador',
    product_version: 'v1.9.0', git_sha: 'abcdef0' };
  const CLEAN = { blocks: [], warnings: [] };
  const attempt = async (items, label) => {
    const flowIds = [...new Set(items.map((item) => item.flow_id))];
    const draftBase = (await db.query(`select coalesce(jsonb_object_agg(flow_id::text, jsonb_build_object('active_version', active_version)), '{}'::jsonb) b
      from public.flow_controls where flow_id = any($1::uuid[])`, [flowIds])).rows[0].b;
    const base = (await db.query('select public._flow_store_base($1, $2, $3::uuid[]) b', [A.tenant, A.ally, flowIds])).rows[0].b;
    const saved = await rpc('operator_save_flow_change_draft_v1', INSTANCE, null, null, items, draftBase, label, 'dan');
    if (!saved.ok) fail(`guardar ${label}`, saved);
    const validated = await rpc('operator_record_flow_change_validation_v1', INSTANCE, saved.proposal_id, saved.revision, base, VALIDATOR, CLEAN, {}, 'dan');
    if (validated.status !== 'validada') fail(`validar ${label}`, validated);
    const approved = await rpc('operator_approve_flow_change_v1', INSTANCE, saved.proposal_id, validated.fingerprint, 'dan');
    if (!approved.ok) fail(`aprobar ${label}`, approved);
    return { proposal: saved.proposal_id, published: await rpc('operator_publish_flow_change_v1', INSTANCE, saved.proposal_id, validated.fingerprint, {
      fingerprint: validated.fingerprint, validator: VALIDATOR, result: CLEAN, catalog_sha256: 'a'.repeat(64), validated_at: new Date().toISOString() }) };
  };
  const item = (extra) => ({ ...world.versionItem(welcome, A.funnel, 'bienvenida', welcomeDefinition({ producto: A.funnel, ...extra })),
    ok_del_cliente: { quien: 'juan', medio: 'whatsapp', fecha: '2026-10-15' } });
  return { world, db, A, welcome, attempt, item };
};

const control = async (db, flow) => (await db.query('select active_version, generation, state from public.flow_controls where flow_id = $1', [flow])).rows[0];
const versions = async (db, flow) => Number((await db.query('select count(*) c from public.flow_definitions where flow_id = $1', [flow])).rows[0].c);

// 1. El rojo: sobre la definicion anterior a la migracion, se publica.
{
  const { db, welcome, attempt, item } = await build();
  const definition = (await db.query(`select pg_get_functiondef('public._flow_change_publish(text,text,uuid,text,jsonb)'::regprocedure) d`)).rows[0].d;
  if (!definition.includes('flow_change_publish_native_version_guard')) fail('la migracion no esta en la cadena de la prueba');
  await db.exec(`do $x$ begin execute replace(pg_get_functiondef('public._flow_change_publish(text,text,uuid,text,jsonb)'::regprocedure),
    'where item ->> ''tipo'' in (''version'', ''volver'')' || chr(10) || '                   and exists (select 1 from public.flow_engine_routes r',
    'where false' || chr(10) || '                   and exists (select 1 from public.flow_engine_routes r'); end $x$;`);
  const { published } = await attempt([item(TWO_SENDS)], 'sin la guarda');
  if (!published.ok || await versions(db, welcome) !== 2) fail('sin la guarda se publicaba: el rojo', published);
  results.rojo = 'sin la guarda se publica';
}

// 2 a 5.
{
  const { world, db, A, welcome, attempt, item } = await build();
  const before = await control(db, welcome);
  const { proposal, published } = await attempt([item(TWO_SENDS)], 'dos envios');
  if (published.ok || published.reason_code !== 'motor_nativo:forma') fail('dos envios', published);
  const after = await control(db, welcome);
  if (after.active_version !== before.active_version || Number(after.generation) !== Number(before.generation)
      || await versions(db, welcome) !== 1) {
    fail('el rechazo escribio algo', { before, after });
  }
  const events = (await db.query(`select reason_code from public.flow_change_events where proposal_id = $1 and event_type = 'flow_change_publish_rejected'`, [proposal])).rows;
  if (events.length !== 1 || events[0].reason_code !== 'motor_nativo:forma') fail('el evento del rechazo', events);
  results.rechaza = published.reason_code;

  const changed = await attempt([item({ topes: { por_dia: 'sin_tope', total: 'sin_tope', repeticion: { una_por: 'persona_y_oferta', otra_despues_de: '30d' } },
    prioridad: { le_gana_a: [], no_arranca_mientras: ['carrito'] } })], 'repeticion y prioridad');
  if (!changed.published.ok || (await control(db, welcome)).active_version !== 2) fail('una version que el motor ejecuta', changed.published);
  results.publica = 2;

  // Un destino de volver que el motor no ejecuta: se escribe a mano, no se puede publicar por el ciclo.
  await db.query(`insert into public.flow_definitions (flow_id, version, schema_ref, definition, definition_sha256, proposal_id, approved_by_kind, approved_by_ref)
    values ($1, 3, 'setter-flujo/v1', $2::jsonb, repeat('0', 64), $3, 'operador', 'dan')`,
    [welcome, JSON.stringify({ ...welcomeDefinition({ producto: A.funnel, ...TWO_SENDS }), estado: 'publicada', version: 3 }), proposal]);
  const back = await attempt([{ producto: A.funnel, flujo: 'bienvenida', flow_id: welcome, tipo: 'volver', version: 3, en_curso: 'seguir' }], 'volver');
  if (back.published.ok || back.published.reason_code !== 'motor_nativo:forma') fail('volver a lo que el motor no ejecuta', back.published);
  results.volver = back.published.reason_code;

  // El carrito de ATT1: ruta del motor con legacy_kind y tres toques, que el nativo no ejecuta.
  const route = await world.createRoute(A.funnel, 'carrito', 'carrito', A.scope);
  if (!route.ok) fail('la ruta del carrito', route);
  await world.publishCart({ a2Wait: '2m' }, 'una version del carrito con la ruta del motor');
  results.legado = 'publica como antes';
}

// 6. Dos corridas.
{
  const { db } = await build();
  try {
    await db.exec(sql);
    fail('la segunda corrida no aborto');
  } catch (error) {
    if (!String(error.message).includes('flow_change_publish_native_version_guard_already_applied')) {
      fail('la segunda corrida aborto por otra cosa', error.message);
    }
  }
  await db.exec('rollback').catch(() => {});
  results.segunda_corrida = 'aborta';
}

console.log(JSON.stringify({ validate_flow_change_publish_native_version_guard: 'ok', ...results }, null, 2));
