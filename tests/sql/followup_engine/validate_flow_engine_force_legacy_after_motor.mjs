// P3 del plan de la T5 (E-020): p_force_legacy sobre un carrito que el motor ya
// arranco. El bridge pide la admision forzada al legado despues de tres
// admisiones sin respuesta (flow_engine_runtime.should_force_legacy). Si la
// primera de esas admisiones SI confirmo en la base (el motor arranco el caso y
// la respuesta se perdio en el camino), la forzada llega con el mismo evento.
// La pregunta: ¿el legado planifica otro caso para el mismo carrito? Si lo
// hace, son dos A1 para la misma persona: el caso doble que frena la T5.
//
// Sobre la cadena completa de migraciones y el mundo de
// validate_flow_engine_motor_v1.mjs (buildWorld), con la ruta del carrito de A
// en motor. Mide y afirma lo que encuentra:
//   1. la admision normal arranca el caso del motor (motor_arranco, created);
//   2. la forzada, con el mismo evento: el resultado, cuantos casos abiertos
//      tiene el contacto, cuantos A1 (first_contact) quedan planificados y si
//      el caso del motor sigue siendo uno;
//   3. el control: una forzada sobre un carrito que el motor NO arranco
//      planifica por el legado (la seccion 5c del motor), para que el verde de
//      la 2 no sea un legado que nunca planifica.
//
// Datos sinteticos, los de buildWorld: personas de example.test con
// telefonos 1202555xxxx y el carrito capturado de Hotmart.
import { pathToFileURL } from 'node:url';
import { buildWorld, fail, one } from './validate_flow_engine_motor_v1.mjs';

const main = async () => {
  const results = {};
  const world = await buildWorld();
  const { db, A, FLOWS, SCOPE, cartFor, admit, createRoute, route, runOf } = world;

  const created = await createRoute(A.funnel, 'carrito', 'carrito', SCOPE);
  if (!created.ok) fail('crear la ruta del carrito', created);
  const toMotor = await route(A.funnel, 'carrito', FLOWS.carrito, 'motor');
  if (!toMotor.ok || toMotor.route.executor !== 'motor') fail('el carrito a motor', toMotor);

  const sql1 = async (text, params, label) => one((await db.query(text, params)).rows, label);
  const footprint = async (lead) => sql1(`
    select (select count(*)::int from public.recovery_cases c
             where c.contact_id = $1 and c.status not in ('sequence_exhausted', 'closed', 'cancelled', 'purchased'))
             as open_cases,
           (select count(distinct c.id)::int from public.recovery_cases c where c.contact_id = $1) as cases,
           (select count(*)::int from public.scheduled_actions a
              join public.recovery_cases c on c.id = a.recovery_case_id
             where c.contact_id = $1 and a.step_key = 'first_contact') as first_contacts,
           (select count(*)::int from public.scheduled_actions a
              join public.recovery_cases c on c.id = a.recovery_case_id
             where c.contact_id = $1) as actions,
           (select count(*)::int from public.flow_engine_runs r
              join public.recovery_cases c on c.id = r.recovery_case_id
             where c.contact_id = $1) as engine_runs,
           (select count(*)::int from public.flow_engine_divergences d
             where d.last_source_ref = $2) as divergences
  `, [lead.contact, lead.event], 'huella');

  // 1 y 2. El motor arranca y la respuesta se pierde; la forzada llega con el mismo evento.
  {
    const lead = await cartFor('doble');
    const first = await admit(lead);
    if (first.outcome !== 'motor_arranco' || first.created !== true) fail('la admision por el motor', first);
    const motorCase = first.recovery_case_id;
    const before = await footprint(lead);
    const forced = await admit(lead, true);
    const after = await footprint(lead);
    const run = await runOf(motorCase);
    results.forzada_despues_del_motor = {
      outcome: forced.outcome, created: forced.created, reason: forced.reason,
      mismo_caso: forced.recovery_case_id === motorCase,
      antes: before, despues: after, recorrido_del_motor: run ? 'sigue' : 'no esta',
    };
    const doble = after.cases > 1 || after.first_contacts > 1 || after.open_cases > 1 || after.actions > before.actions;
    results.caso_doble = doble;
    if (doble) {
      fail('CASO DOBLE: la forzada planifico otro caso para el mismo carrito', results.forzada_despues_del_motor);
    }
    if (run === null || before.first_contacts !== 1) fail('el caso del motor no quedo como uno', results);
  }

  // 3. El control: sin caso del motor, la forzada si planifica por el legado.
  {
    const lead = await cartFor('forzada-sin-motor');
    const forced = await admit(lead, true);
    const after = await footprint(lead);
    if (forced.outcome !== 'legado_por_divergencia' || forced.created !== true || after.first_contacts !== 1
        || after.engine_runs !== 0) {
      fail('el control: la forzada sin motor no planifico por el legado', { forced, after });
    }
    results.control_forzada_sin_motor = { outcome: forced.outcome, first_contacts: after.first_contacts };
  }

  console.log(JSON.stringify(results));
};

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    await main();
  } catch (error) {
    console.error(error.stack?.split('\n').slice(0, 3).join('\n'),
      JSON.stringify({ code: error.code, detail: error.detail, where: error.where, query: error.query?.slice(0, 200) }));
    process.exit(1);
  }
}
