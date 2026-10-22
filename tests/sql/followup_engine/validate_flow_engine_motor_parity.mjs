// La paridad de la ruta del motor contra el legado, para el carrito (la T2.3b
// del motor de flujos, principio 3 del diseno: lo que iba a medir la sombra).
//
// Dos comparaciones, con la cadena real y el mundo de
// validate_flow_engine_motor_v1.mjs (ATT1, el carrito de tres toques en el
// almacen, la ruta del carrito en motor):
//
//   1. Los casos del carrito de la T1 (tests/fixtures/flow_engine/
//      legacy_recovery_cases.json, que genera validate_flow_engine_legacy.mjs con
//      el legado) corridos por la ruta del motor, con la imagen nueva (la
//      admision y mark_flow_engine_request_started_v1) y con la vieja (el
//      envoltorio y mark_lancemos_pilot_request_started, que delegan). Se compara
//      la proyeccion que ve el bridge: cada plan (arranco y el motivo que deja en
//      processing_error), la primera reevaluacion de cada toma (decision y
//      motivo) y cada arranque (resultado, y el motivo si no arranco). Con la
//      imagen nueva, un no-arranque del motor se proyecta como
//      flow_engine_no_start:<motivo> (diseno, principio 1, punto 5). Los casos son
//      G1, G4, G6, G7, G9, G10, G20, G21, G22 y G23: los que dispara un carrito y
//      se reproducen en un mundo con un scope y una ventana de todo el dia. Las
//      ventanas (G2, G3, G25) quedan afuera: la reevaluacion es la misma
//      funcion, y la politica compilada la fijan compile_cases.json y la
//      relectura de la admision.
//   2. Los pasos siguientes (A2 y Z) y la renovacion, como
//      validate_recovery_followup_steps.mjs: el mismo caso por el legado (la ruta
//      en legado, la politica de tres toques del scope) y por el motor (las dos
//      imagenes), comparando la forma de cada paso (step_key, plantilla
//      aprobada, cupon), el cupon 2, la renovacion y el cierre. Las esperas no
//      se comparan: el legado de la prueba espera segundos y el esquema minutos.
//
// Toda diferencia tiene que estar con nombre en
// tests/fixtures/flow_engine/motor_parity_named_differences.json (caso, imagen,
// posicion y campo, los dos valores y el porque). Una diferencia sin nombre es
// rojo, y un nombre que ya no ocurre tambien (la lista no envejece en silencio).
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildWorld, errorOf, fail, one, root } from './validate_flow_engine_motor_v1.mjs';

const LEGACY = JSON.parse(readFileSync(join(root, 'tests/fixtures/flow_engine/legacy_recovery_cases.json'), 'utf8'));
const NAMED = JSON.parse(readFileSync(join(root, 'tests/fixtures/flow_engine/motor_parity_named_differences.json'), 'utf8'));
const CASES = ['G1', 'G4', 'G6', 'G7', 'G9', 'G10', 'G20', 'G21', 'G22', 'G23'];
// Desde que posicion de la proyeccion del legado se compara: en G7, el pago
// fallido abierto (su plan y su diferimiento) es la preparacion, y lo que se
// compara es el carrito que llega despues.
const FROM = { G7: 2 };

const w = await buildWorld();
const { db, A, FLOWS, SCOPE } = w;
const created = await w.createRoute(A.funnel, 'carrito', 'carrito', SCOPE);
if (!created.ok) fail('la ruta del carrito', created);

// La proyeccion de un caso del legado: plan, la primera reevaluacion de cada
// toma y el arranque (sin el motivo cuando arranco).
const project = (pasos) => {
  const out = [];
  let lastToma = null;
  for (const paso of pasos) {
    if (paso.paso === 'toma') lastToma = paso;
    if (paso.paso === 'plan') out.push({ paso: 'plan', arranco: paso.arranco, motivo: paso.motivo });
    if (paso.paso === 'reevaluacion' && lastToma) {
      out.push({ paso: 'reevaluacion', decision: paso.decision, motivo: paso.motivo });
      lastToma = null;
    }
    if (paso.paso === 'arranque') {
      out.push(paso.resultado === 'request_started'
        ? { paso: 'arranque', resultado: 'request_started' }
        : { paso: 'arranque', resultado: paso.resultado, motivo: paso.motivo });
    }
  }
  return out;
};

// El motor, como lo ve el bridge con cada imagen.
const planAs = async (image, lead) => {
  if (image === 'vieja') {
    const error = await errorOf(db, () => w.planCart(lead));
    if (error === null) return { paso: 'plan', arranco: true, motivo: null };
    return { paso: 'plan', arranco: false,
      motivo: error.detail ? `${error.message}:${error.detail}` : error.message };
  }
  const row = await w.admit(lead);
  if (['motor_arranco', 'legado', 'legado_por_divergencia'].includes(row.outcome)) {
    return { paso: 'plan', arranco: true, motivo: null };
  }
  return { paso: 'plan', arranco: false,
    motivo: row.outcome === 'motor_no_arranca' ? `flow_engine_no_start:${row.reason}` : row.reason };
};
const reserveAs = async (lead, actionId) => {
  const reserved = await w.reserve(lead, actionId);
  return { reserved, step: { paso: 'reevaluacion', decision: reserved.decision.decision, motivo: reserved.decision.reason_code } };
};
const startAs = async (image, reserved) => {
  if (image === 'vieja') {
    let started = null;
    const error = await errorOf(db, async () => { started = await w.startOld(reserved); });
    if (error === null && started.phase === 'request_started') return { paso: 'arranque', resultado: 'request_started' };
    return { paso: 'arranque', resultado: 'rechazado', motivo: error?.detail ?? error?.message ?? 'sin_fila' };
  }
  const row = await w.startNew(reserved);
  return row.outcome === 'request_started'
    ? { paso: 'arranque', resultado: 'request_started' }
    : { paso: 'arranque', resultado: row.outcome, motivo: row.reason };
};
// A1 entero: plan, toma, reevaluacion, reserva y arranque.
const a1As = async (image, lead, out) => {
  const plan = await planAs(image, lead);
  out.push(plan);
  if (!plan.arranco) return null;
  const { reserved, step } = await reserveAs(lead, lead.plan.scheduled_action_id);
  out.push(step);
  out.push(await startAs(image, reserved));
  return reserved;
};
const usedToday = async () => Number(one((await db.query(`
  select (select count(*) from public.pilot_outbound_request_authorizations where scope_key = $1
            and local_budget_date = (now() at time zone 'America/Mexico_City')::date)
       + (select count(*) from public.flow_engine_request_authorizations where pool = $1
            and local_budget_date = (now() at time zone 'America/Mexico_City')::date) as n`, [SCOPE])).rows, 'hoy').n);
const usedTotal = async () => Number(one((await db.query(`
  select (select count(*) from public.pilot_outbound_request_authorizations where scope_key = $1)
       + (select count(*) from public.flow_engine_request_authorizations where pool = $1) as n`, [SCOPE])).rows, 'total').n);
const inTransaction = async (run) => {
  await db.exec('begin');
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};
// La version del scope del legado cambiada por el operador (G21 y G22), en
// una transaccion: pausa, publica la v2, la activa y la arma.
const scopeV2 = async () => {
  await w.setState(SCOPE, 1, 'paused');
  await db.query(`
    insert into public.pilot_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id, channel, channel_provider,
       channel_account_ref, source, source_event_type, additional_source_event_types, external_product_id, offer_code,
       additional_offer_codes, purpose, policy_key, policy_version, timezone, max_cohort_contacts,
       max_outbound_request_starts_total, max_outbound_request_starts_per_day, audience_mode, approved_by, approved_at,
       published_at)
    select scope_key, 2, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id, channel, channel_provider,
           channel_account_ref, source, source_event_type, additional_source_event_types, external_product_id, offer_code,
           additional_offer_codes, purpose, policy_key, policy_version, timezone, max_cohort_contacts,
           max_outbound_request_starts_total, max_outbound_request_starts_per_day, audience_mode, approved_by, now(), now()
      from public.pilot_scope_versions where scope_key = $1 and version = 1`, [SCOPE]);
  await db.query('select * from public.activate_lancemos_pilot_scope_version($1,2,$2,$3,$4)',
    [SCOPE, await w.generationOf(SCOPE), w.OPERATOR, 'la v2 del scope']);
  await w.setState(SCOPE, 2, 'armed');
};

let serial = 0;
const fresh = (label) => {
  serial += 1;
  return `${label}-${serial}`;
};
const RUNNERS = {
  G1: async (image, out) => {
    await a1As(image, await w.cartFor(fresh('g1')), out);
  },
  // Los topes son los de la definicion fijada, no los del scope: una version
  // con el tope del dia (o el total) justo para una persona mas.
  G4: async (image, out) => inTransaction(async () => {
    await w.publishCart({ topes: { por_dia: (await usedToday()) + 1 } }, 'tope del dia para uno');
    await a1As(image, await w.cartFor(fresh('g4a')), out);
    await a1As(image, await w.cartFor(fresh('g4b')), out);
  }),
  G6: async (image, out) => inTransaction(async () => {
    await w.publishCart({ topes: { total: (await usedTotal()) + 1 } }, 'tope total para uno');
    await a1As(image, await w.cartFor(fresh('g6a')), out);
    await a1As(image, await w.cartFor(fresh('g6b')), out);
  }),
  G7: async (image, out) => {
    const lead = w.person(fresh('g7'));
    await w.admitForm(lead, new Date(Date.now() - 40 * 60_000));
    await w.refusalFor(4, lead);
    await w.cartFor(lead, { form: false });
    out.push(await planAs(image, lead));
  },
  G9: async (image, out) => {
    const lead = await w.cartFor(fresh('g9'));
    out.push(await planAs(image, lead));
    await w.cartFor(lead, { form: false });
    out.push(await planAs(image, lead));
  },
  G10: async (image, out) => {
    const lead = await w.cartFor(fresh('g10'));
    const reserved = await a1As(image, lead, out);
    await w.accept(lead, reserved);
    await w.cartFor(lead, { form: false });
    out.push(await planAs(image, lead));
  },
  G20: async (image, out) => {
    const lead = w.person(fresh('g20'));
    serial += 1;
    await db.query(`select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,$6::timestamptz,'unsubscribe')`,
      [A.accountId, A.inboxId, 890000 + serial, 9890000 + serial, lead.phone, new Date(Date.now() - 90 * 60_000).toISOString()]);
    await w.cartFor(lead);
    out.push(await planAs(image, lead));
  },
  G21: async (image, out) => inTransaction(async () => {
    const lead = await w.cartFor(fresh('g21'));
    out.push(await planAs(image, lead));
    await scopeV2();
    const { reserved, step } = await reserveAs(lead, lead.plan.scheduled_action_id);
    out.push(step);
    out.push(await startAs(image, reserved));
  }),
  G22: async (image, out) => inTransaction(async () => {
    await scopeV2();
    out.push(await planAs(image, await w.cartFor(fresh('g22'))));
  }),
  G23: async (image, out) => {
    const lead = await w.cartFor(fresh('g23'));
    out.push(await planAs(image, lead));
    await w.setState(SCOPE, 1, 'paused');
    try {
      const { reserved, step } = await reserveAs(lead, lead.plan.scheduled_action_id);
      out.push(step);
      out.push(await startAs(image, reserved));
    } finally {
      await w.setState(SCOPE, 1, 'armed');
    }
  },
};

// Los pasos siguientes, la renovacion y el cierre de un caso de tres toques.
const followups = async (image) => {
  const lead = await w.cartFor(fresh(`pasos-${image}`));
  const out = [];
  if (image === 'legado') {
    if (!(await w.planCart(lead)).created) fail('el plan del legado');
  } else if (!(await planAs(image, lead)).arranco) {
    fail(`el plan de ${image}`);
  }
  const start = image === 'legado' ? 'vieja' : image;
  const shape = async (stepKey) => {
    const action = await w.stepOf(lead, stepKey);
    const step = one((await db.query('select public._recovery_followup_step($1) as s', [action.id])).rows, 'paso').s;
    return { action, step: { paso: stepKey, tipo: action.action_type, ancla: action.anchor_type,
      plantilla_aprobada: step?.approved_template ?? null, cupon: step?.coupon_ordinal ?? null } };
  };
  const send = async (actionId) => {
    const { reserved } = await reserveAs(lead, actionId);
    const started = await startAs(start, reserved);
    if (started.resultado !== 'request_started') fail(`${image}: el arranque`, started);
    await w.accept(lead, reserved);
  };
  await send(lead.plan.scheduled_action_id);
  const a2 = await shape('cart_second_touch');
  out.push(a2.step);
  await send(a2.action.id);
  const z = await shape('cart_last_touch');
  out.push(z.step);
  await db.query(`select * from public.claim_due_recovery_coupons_v1(
      $1::timestamptz,'paridad-cupones',60,'ATT','45 minutes'::interval,'24 hours'::interval,0.1::numeric,100)`,
  [await w.dbNow()]);
  out.push({ paso: 'cupon_2', hay: (await db.query(
    'select 1 from public.recovery_coupons where recovery_case_id = $1 and ordinal = 2', [lead.plan.recovery_case_id])).rows.length === 1 });
  serial += 1;
  const renewal = one((await db.query('select * from public.request_recovery_renewal_v1($1,$2,$3,$4,$5,$6)',
    [A.accountId, A.inboxId, Number(lead.chatwoot), 660000 + serial, lead.phone, (await w.dbNow()).toISOString()])).rows, 'renovacion');
  out.push({ paso: 'renovacion', resultado: renewal.outcome });
  await send(z.action.id);
  const sequence = one((await db.query(`select status, completion_reason from public.followup_sequences
    where recovery_case_id = $1`, [lead.plan.recovery_case_id])).rows, 'secuencia');
  out.push({ paso: 'cierre', estado: sequence.status, motivo: sequence.completion_reason,
    caso: (await w.caseOf(lead)).status });
  return out;
};

// Las diferencias de un lado contra el otro, posicion por posicion.
const differences = (caso, image, legacy, motor) => {
  const out = [];
  for (let i = 0; i < Math.max(legacy.length, motor.length); i += 1) {
    const left = legacy[i] ?? null;
    const right = motor[i] ?? null;
    if (left === null || right === null || left.paso !== right.paso) {
      out.push({ caso, imagen: image, posicion: i, campo: 'paso', legado: left, motor: right });
      continue;
    }
    for (const key of new Set([...Object.keys(left), ...Object.keys(right)])) {
      if (JSON.stringify(left[key] ?? null) !== JSON.stringify(right[key] ?? null)) {
        out.push({ caso, imagen: image, posicion: i, campo: key, legado: left[key] ?? null, motor: right[key] ?? null });
      }
    }
  }
  return out;
};

const found = [];
const legacyCases = Object.fromEntries(LEGACY.casos.map((item) => [item.id, item]));
// La comparacion 2 va primero, con la ruta todavia en legado.
const followupsLegacy = await followups('legado');
const moved = await w.route(A.funnel, 'carrito', FLOWS.carrito, 'motor');
if (!moved.ok) fail('el carrito a motor', moved);
const counts = {};
for (const image of ['nueva', 'vieja']) {
  for (const caso of CASES) {
    const legacy = project(legacyCases[caso].pasos).slice(FROM[caso] ?? 0);
    if (legacy.length === 0) fail(`${caso}: la proyeccion del legado vacia`);
    const motor = [];
    await RUNNERS[caso](image, motor);
    found.push(...differences(caso, image, legacy, motor));
    counts[`${caso}:${image}`] = motor.length;
  }
  found.push(...differences('pasos_siguientes', image, followupsLegacy, await followups(image)));
}

const key = (item) => JSON.stringify([item.caso, item.imagen, item.posicion, item.campo, item.legado, item.motor]);
const named = new Map(NAMED.diferencias.map((item) => [key(item), item]));
for (const item of NAMED.diferencias) {
  if (typeof item.por_que !== 'string' || item.por_que.length < 20) fail('una diferencia con nombre sin su porque', item);
}
const unnamed = found.filter((item) => !named.has(key(item)));
const stale = NAMED.diferencias.filter((item) => !found.some((other) => key(other) === key(item)));
if (unnamed.length !== 0 || stale.length !== 0) {
  fail('la paridad del motor tiene diferencias sin nombre o nombres que ya no ocurren', { unnamed, stale });
}
// El rojo: sacar una diferencia con nombre la vuelve una diferencia sin nombre.
{
  const withoutOne = new Set(NAMED.diferencias.slice(1).map(key));
  if (found.every((item) => withoutOne.has(key(item)))) fail('el rojo de la paridad no es rojo');
}
console.log(JSON.stringify({ casos: CASES.length, imagenes: 2, pasos: counts, diferencias_con_nombre: found.length,
  pasos_siguientes: followupsLegacy.length }));
