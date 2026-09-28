// 20260928000300: el primer nombre inferido de cada lead.
//
// Ejecuta las RPC reales sobre la cadena completa de migraciones:
// - la primera respuesta manda y un segundo registro no la pisa;
// - `uncertain` no guarda nombre y `confident` exige uno;
// - la clave tiene que ser un sha256 en hex, en las dos RPC;
// - solo `service_role` ejecuta las RPC y nadie lee la tabla directo.
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
`);
for (const file of [
  join(root, 'supabase/baseline/20260803_public_schema.sql'),
  ...readdirSync(join(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => join(root, 'supabase/migrations', name)),
]) {
  await db.exec(readFileSync(file, 'utf8').replace(
    /create extension if not exists pgcrypto;/gi,
    '-- pgcrypto is built into PGlite',
  ));
}

const expectRejected = async (label, action) => {
  let rejected = false;
  await db.exec('begin');
  try {
    await action();
  } catch {
    rejected = true;
  } finally {
    await db.exec('rollback');
  }
  if (!rejected) throw new Error(`${label} did not fail closed`);
};

const keyA = 'a'.repeat(64);
const keyB = 'b'.repeat(64);
const record = (key, result, firstName) => db.query(
  `select public.record_lead_first_name_inference_v1($1, $2, $3, 'agente-comercial', 'lead-first-name-v1') outcome`,
  [key, result, firstName],
);
const lookup = (key) => db.query(
  `select * from public.get_lead_first_name_inference_v1($1)`,
  [key],
);

// 1. La primera respuesta manda.
const first = (await record(keyA, 'confident', 'Juan Carlos')).rows[0]?.outcome;
const second = (await record(keyA, 'confident', 'Juan')).rows[0]?.outcome;
if (first !== 'inserted' || second !== 'existing') {
  throw new Error(`first answer must win: ${first}/${second}`);
}
const stored = (await lookup(keyA)).rows;
if (stored.length !== 1 || stored[0].first_name !== 'Juan Carlos' || stored[0].result !== 'confident') {
  throw new Error(`unexpected stored inference: ${JSON.stringify(stored)}`);
}

// 2. `uncertain` sin nombre; una clave sin fila no devuelve nada.
await record(keyB, 'uncertain', null);
const uncertain = (await lookup(keyB)).rows;
if (uncertain.length !== 1 || uncertain[0].first_name !== null) {
  throw new Error(`uncertain must not store a name: ${JSON.stringify(uncertain)}`);
}
if ((await lookup('c'.repeat(64))).rows.length !== 0) {
  throw new Error('a missing key must return no rows');
}

// 3. Entradas invalidas.
await expectRejected('confident without name', () => record('d'.repeat(64), 'confident', null));
await expectRejected('uncertain with name', () => record('d'.repeat(64), 'uncertain', 'Ana'));
await expectRejected('unknown result', () => record('d'.repeat(64), 'maybe', 'Ana'));
await expectRejected('name longer than 80', () => record('d'.repeat(64), 'confident', 'x'.repeat(81)));
await expectRejected('record with a non-hash key', () => record('Juan Carlos', 'confident', 'Juan'));
await expectRejected('lookup with a non-hash key', () => lookup('ABC'));

// 4. Permisos: solo service_role ejecuta, nadie lee la tabla.
const grants = (await db.query(`
  select
    has_function_privilege('anon', 'public.get_lead_first_name_inference_v1(text)', 'execute') anon_get,
    has_function_privilege('authenticated', 'public.record_lead_first_name_inference_v1(text,text,text,text,text)', 'execute') auth_record,
    has_function_privilege('service_role', 'public.get_lead_first_name_inference_v1(text)', 'execute') service_get,
    has_function_privilege('service_role', 'public.record_lead_first_name_inference_v1(text,text,text,text,text)', 'execute') service_record,
    has_table_privilege('service_role', 'public.lead_first_name_inferences', 'select') service_select,
    has_table_privilege('anon', 'public.lead_first_name_inferences', 'select') anon_select
`)).rows[0];
if (grants.anon_get || grants.auth_record || !grants.service_get || !grants.service_record
    || grants.service_select || grants.anon_select) {
  throw new Error(`unexpected grants: ${JSON.stringify(grants)}`);
}

console.log(JSON.stringify({
  migration: '20260928000300',
  first_answer_wins: second,
  uncertain_first_name: uncertain[0].first_name,
  grants,
}));
