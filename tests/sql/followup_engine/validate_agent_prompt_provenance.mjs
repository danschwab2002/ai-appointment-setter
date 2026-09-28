// Valida que la procedencia del prompt quede registrada por turno, contra el
// esquema real: registrar un release -> registrar un turno -> comprobar que el
// turno quedo atado al release vigente -> comprobar que la confianza de esa
// atribucion se calcula y NO se afirma.
//
// El caso que reproduce es el medido el 2026-09-27: las cuatro decisiones de la
// revision diaria de ese dia salieron con release_id='release_lineage_unavailable'
// y release_version=0, literales hardcodeados en daily_feedback_export.py. Del
// feedback se sabia que se dijo y de que dia, no sobre que version del agente.
//
// El paso que importa es el de la MALA ATRIBUCION. El registrador corre por
// cron, no por turno, asi que entre dos observaciones el SOUL puede haber
// cambiado --- y de hecho se edita a mano en el VPS. Si el sistema se limitara a
// atribuir el release mas nuevo anterior al turno, mentiria en silencio. La
// confianza se calcula comparando el mtime de los artefactos del release
// SIGUIENTE contra el momento del turno: si ya estaban modificados antes del
// turno, la atribucion es 'misattributed' y se sabe.
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { PGlite } from '@electric-sql/pglite';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const stack = [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(join(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => join(root, 'supabase/migrations', name)),
];

const db = new PGlite();
await db.waitReady;
await db.exec('create role anon noinherit; create role authenticated noinherit; create role service_role noinherit bypassrls;');
for (const path of stack) {
  let sql = readFileSync(path, 'utf8');
  sql = sql.replace(/create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite');
  await db.exec(sql);
}

const fail = (mensaje) => {
  console.error(`AGENT_PROMPT_PROVENANCE_FAILED: ${mensaje}`);
  process.exit(1);
};
const igual = (real, esperado, que) => {
  if (String(real) !== String(esperado)) {
    fail(`${que}: se esperaba ${esperado} y vino ${real}`);
  }
};
const digest = (semilla) => createHash('sha256').update(semilla).digest('hex');

const TENANT = 'lancemos';
const SCOPE = 'provenance-probe';
const PERFIL = 'agente-comercial';

const ARTEFACTOS = (soul) => ({
  'SOUL.md': { sha256: digest(soul), bytes: soul.length, modified_at: null },
  'config.yaml': { sha256: digest('config'), bytes: 12, modified_at: null },
  '.skills_prompt_snapshot.json': { sha256: digest('skills'), bytes: 34, modified_at: null },
});

async function registrar({ soul, modificado, observado }) {
  const { rows } = await db.query(
    `select public.register_agent_prompt_release_v1(
       $1, $2, $3, $4, $5::jsonb, $6::timestamptz, $7, $8::timestamptz, $9
     ) as r`,
    [
      TENANT, SCOPE, PERFIL, digest(soul),
      JSON.stringify(ARTEFACTOS(soul)), modificado, soul, observado,
      'validator',
    ],
  );
  return rows[0].r;
}

async function turno({ conv, cuando, digestTurno, modeloContesto = 'glm-5.2', resultado = 'completed' }) {
  const contexto = { conversation_ref: String(conv), human_handoff_confirmed: false };
  const { rows } = await db.query(
    `select public.record_agent_turn_provenance_v1(
       $1, $2, $3::bigint, $4, $5::timestamptz, $6, $7, $8, $9, $10, $11, $12::jsonb
     ) as r`,
    [
      TENANT, SCOPE, conv, digest(digestTurno), cuando, resultado,
      'agente-comercial', modeloContesto, '246de1ba', 'shadow-context-v1',
      digest(`ctx-${digestTurno}`), JSON.stringify(contexto),
    ],
  );
  return rows[0].r;
}

// --- 1. Un release se registra una vez; observarlo de nuevo no es un evento ---

const r1 = await registrar({
  soul: 'SOUL version uno',
  modificado: '2026-09-20T09:00:00Z',
  observado: '2026-09-20T10:00:00Z',
});
igual(r1.outcome, 'registered', 'primer release');
igual(r1.release_ordinal, 1, 'ordinal del primer release');

const r1otra = await registrar({
  soul: 'SOUL version uno',
  modificado: '2026-09-20T09:00:00Z',
  observado: '2026-09-20T10:30:00Z',
});
igual(r1otra.outcome, 'unchanged', 'el mismo release observado de nuevo');
igual(r1otra.release_ordinal, 1, 'el ordinal no avanza al no cambiar nada');
igual(r1otra.release_digest, r1.release_digest, 'el digest es el mismo');

// --- 2. Un turno queda atado al release vigente en ese momento ----------------

const CONV_A = 9601;
const t1 = await turno({ conv: CONV_A, cuando: '2026-09-20T11:00:00Z', digestTurno: 't1' });
igual(t1.outcome, 'recorded', 'turno con release vigente');
igual(t1.release_ordinal, 1, 'el turno toma el release 1');

// --- 3. El mismo turno no se cuenta dos veces --------------------------------

const t1otra = await turno({ conv: CONV_A, cuando: '2026-09-20T11:00:00Z', digestTurno: 't1' });
igual(t1otra.outcome, 'replayed', 'el reintento del bridge no duplica el turno');

// --- 4. Un turno anterior a toda observacion se guarda SIN release -----------
// Un turno sin procedencia es un dato, no una falla: si el registrador nunca
// corrio, el turno tiene que quedar contable en vez de invisible.

const CONV_C = 9603;
const t0 = await turno({ conv: CONV_C, cuando: '2026-09-20T09:30:00Z', digestTurno: 't0' });
igual(t0.outcome, 'recorded_without_release', 'turno previo a toda observacion');
if (t0.release_digest !== null) fail('un turno sin release no puede traer digest');

// --- 5. La confianza de la atribucion ----------------------------------------
// Sin una observacion posterior no hay con que comparar: 'open', no 'verified'.

const confianza = async (ordinal, cuando) => {
  const { rows } = await db.query(
    `select public.agent_turn_release_confidence_v1($1, $2, $3::integer, $4::timestamptz) as c`,
    [TENANT, SCOPE, ordinal, cuando],
  );
  return rows[0].c;
};

igual(await confianza(1, '2026-09-20T11:00:00Z'), 'open', 'sin release posterior');
igual(await confianza(null, '2026-09-20T11:00:00Z'), 'no_release', 'turno sin release');

// El release 2 se observa a las 13:00 y sus artefactos cambiaron a las 12:00,
// o sea DESPUES del turno de las 11:00: ese turno si corrio el release 1.
const r2 = await registrar({
  soul: 'SOUL version dos',
  modificado: '2026-09-20T12:00:00Z',
  observado: '2026-09-20T13:00:00Z',
});
igual(r2.outcome, 'registered', 'segundo release');
igual(r2.release_ordinal, 2, 'ordinal del segundo release');

igual(await confianza(1, '2026-09-20T11:00:00Z'), 'verified', 'el cambio vino despues del turno');

// --- 6. La mala atribucion se detecta, no se tapa ----------------------------
// Un turno de las 12:30 se atribuye al release 1 (el mas nuevo observado antes),
// pero los artefactos ya habian cambiado a las 12:00: corrio algo mas nuevo.

const CONV_B = 9602;
const t3 = await turno({ conv: CONV_B, cuando: '2026-09-20T12:30:00Z', digestTurno: 't3' });
igual(t3.release_ordinal, 1, 'el turno de las 12:30 se atribuye al release 1');
igual(
  await confianza(1, '2026-09-20T12:30:00Z'),
  'misattributed',
  'los artefactos ya habian cambiado antes del turno',
);

// --- 7. La revision diaria lee el ULTIMO turno de cada conversacion ----------

const t2 = await turno({ conv: CONV_A, cuando: '2026-09-20T14:00:00Z', digestTurno: 't2' });
igual(t2.release_ordinal, 2, 'el turno de las 14:00 toma el release 2');

const { rows: leido } = await db.query(
  `select public.get_agent_turn_provenance_v1(
     $1, $2, $3::bigint[], $4::timestamptz, $5::timestamptz
   ) as r`,
  [TENANT, SCOPE, [CONV_A, CONV_B, CONV_C], '2026-09-20T00:00:00Z', '2026-09-21T00:00:00Z'],
);
const paquete = leido[0].r;

igual(paquete[String(CONV_A)].release_ordinal, 2, 'de CONV_A se lee el turno mas nuevo');
igual(paquete[String(CONV_A)].confidence, 'open', 'el release 2 no tiene posterior');
igual(paquete[String(CONV_A)].model_answered, 'glm-5.2', 'el modelo que contesto queda');
igual(paquete[String(CONV_A)].bridge_release, '246de1ba', 'el sha del bridge queda');
igual(paquete[String(CONV_B)].confidence, 'misattributed', 'CONV_B arrastra su mala atribucion');
igual(paquete[String(CONV_C)].confidence, 'no_release', 'CONV_C no tiene release');
if (paquete[String(CONV_C)].release_ordinal !== null) {
  fail('CONV_C no puede traer ordinal');
}

// Una conversacion fuera de la ventana no entra.
const { rows: fuera } = await db.query(
  `select public.get_agent_turn_provenance_v1(
     $1, $2, $3::bigint[], $4::timestamptz, $5::timestamptz
   ) as r`,
  [TENANT, SCOPE, [CONV_A], '2026-09-21T00:00:00Z', '2026-09-22T00:00:00Z'],
);
igual(Object.keys(fuera[0].r).length, 0, 'ventana sin turnos');

// --- 8. Un release observado es un hecho: no se corrige ----------------------

let inmutable = false;
try {
  await db.exec(`update public.agent_prompt_releases set soul_text = 'otro';`);
} catch (error) {
  inmutable = String(error.message).includes('agent_prompt_release_is_immutable');
}
if (!inmutable) fail('los releases tienen que ser inmutables');

let sinBorrar = false;
try {
  await db.exec(`delete from public.agent_prompt_releases;`);
} catch (error) {
  sinBorrar = String(error.message).includes('agent_prompt_release_is_immutable');
}
if (!sinBorrar) fail('los releases no se pueden borrar');

// --- 9. Lo invalido se rechaza ----------------------------------------------

let rechazo = false;
try {
  await db.query(
    `select public.register_agent_prompt_release_v1(
       $1, $2, $3, 'no-es-un-sha', '{}'::jsonb, now(), 'soul', now(), 'validator'
     )`,
    [TENANT, SCOPE, PERFIL],
  );
} catch (error) {
  rechazo = String(error.message).includes('invalid_agent_prompt_release');
}
if (!rechazo) fail('un digest que no es sha256 tiene que fallar');

let rechazoTurno = false;
try {
  await turno({ conv: 9604, cuando: '2026-09-20T15:00:00Z', digestTurno: 't9', resultado: 'raro' });
} catch (error) {
  rechazoTurno = String(error.message).includes('invalid_agent_turn_provenance');
}
if (!rechazoTurno) fail('un outcome que no existe tiene que fallar');

// --- 10. El SOUL completo queda guardado, no solo su hash -------------------

const { rows: guardado } = await db.query(
  `select soul_text, artifacts from public.agent_prompt_releases where release_ordinal = 1`,
);
igual(guardado[0].soul_text, 'SOUL version uno', 'el texto del SOUL queda entero');
if (Object.keys(guardado[0].artifacts).length !== 3) {
  fail('tienen que quedar los tres artefactos');
}

console.log('AGENT_PROMPT_PROVENANCE_OK');
await db.close();
