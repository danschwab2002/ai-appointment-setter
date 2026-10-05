// 2026-10-05 (migration 20261005000100, ADR-0022): the deployment path of the
// first ally, end to end. Her base stopped at 20260928000400: the migrations of
// 2026-09-29 to 2026-10-01 (the portable chain of the second instance) were
// left out on purpose, and 20261005000100 is applied on top of that, alone. If
// some day the left-out ones are applied after it, 20261001000100 derives the
// portable reserve from the shared one, which already writes "~".
//
// What is pinned here:
//   1. before: the fingerprint of 20261005000100 is absent (not partial);
//   2. 20261005000100 applies on the partial chain, skips the portable reserve
//      (it does not exist there), and the shared reserve writes "~";
//   3. the left-out migrations still apply afterwards, the derived portable
//      reserve writes "~", and every fingerprint involved stays present;
//   4. the ACL inventory stays clean at each step.
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { PGlite } from '@electric-sql/pglite';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const LAST_APPLIED_ON_JOHANNA = '20260928000400';
const TILDE = '20261005000100';
const migrations = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const tilde = migrations.filter((name) => name.startsWith(TILDE));
const applied = migrations.filter((name) => name.split('_', 1)[0] <= LAST_APPLIED_ON_JOHANNA);
const leftOut = migrations.filter((name) => {
  const version = name.split('_', 1)[0];
  return version > LAST_APPLIED_ON_JOHANNA && version < TILDE;
});
if (tilde.length !== 1 || leftOut.length === 0
    || !leftOut.some((name) => name.startsWith('20261001000100'))) {
  throw new Error(`unexpected migration set: ${JSON.stringify({ tilde, leftOut })}`);
}

const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
`);
const apply = async (names) => {
  for (const name of names) {
    await db.exec(readFileSync(join(root, 'supabase/migrations', name), 'utf8').replace(
      /create extension if not exists pgcrypto;/gi,
      '-- pgcrypto is built into PGlite',
    ));
  }
};
await db.exec(readFileSync(join(root, 'supabase/baseline/20260803_public_schema.sql'), 'utf8')
  .replace(/create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
await apply(applied);

const schemaSql = readFileSync(join(root, 'scripts/supabase_schema_inventory.sql'), 'utf8');
const aclSql = readFileSync(join(root, 'scripts/supabase_acl_inventory.sql'), 'utf8');
const fingerprint = async (version) => (await db.query(schemaSql)).rows
  .find((row) => row.version === version);
const aclFailures = async () => (await db.query(aclSql)).rows
  .filter((row) => row.acl_status !== 'ok');
const writers = async () => (await db.query(`
  select p.proname,
         position('''~hermes~v1~''' in pg_get_functiondef(p.oid)) > 0 as tilde,
         position('''|hermes|v1|''' in pg_get_functiondef(p.oid)) > 0 as bar
  from pg_proc p
  join pg_namespace n on n.oid = p.pronamespace
  where n.nspname = 'public'
    and p.proname in ('reserve_chatwoot_checkout_issuance_v2', 'reserve_portable_checkout_issuance_v2')
  order by p.proname
`)).rows;

// 1. Before: absent, not partial.
const before = await fingerprint(TILDE);
const writersBefore = await writers();
if (before?.fingerprint_status !== 'fingerprint_absent'
    || writersBefore.length !== 1
    || writersBefore[0].proname !== 'reserve_chatwoot_checkout_issuance_v2'
    || writersBefore[0].tilde || !writersBefore[0].bar) {
  throw new Error(`state before the migration diverged: ${JSON.stringify({ before, writersBefore })}`);
}

// 2. The migration, alone, on the partial chain.
await apply(tilde);
const after = await fingerprint(TILDE);
const writersAfter = await writers();
const aclAfter = await aclFailures();
if (after?.fingerprint_status !== 'fingerprint_present'
    || writersAfter.length !== 1
    || !writersAfter[0].tilde || writersAfter[0].bar
    || aclAfter.length !== 0) {
  throw new Error(`the migration on the partial chain diverged: ${JSON.stringify({
    after, writersAfter, aclAfter,
  })}`);
}

// 3. The left-out migrations, applied afterwards (they would need
//    `supabase db push --include-all` on a real base).
await apply(leftOut);
const later = await fingerprint(TILDE);
const derivation = await fingerprint('20261001000100');
const writersLater = await writers();
const aclLater = await aclFailures();
if (later?.fingerprint_status !== 'fingerprint_present'
    || derivation?.fingerprint_status !== 'fingerprint_present'
    || writersLater.length !== 2
    || writersLater.some((row) => !row.tilde || row.bar)
    || aclLater.length !== 0) {
  throw new Error(`the left-out migrations applied later diverged: ${JSON.stringify({
    later, derivation, writersLater, aclLater,
  })}`);
}

console.log(JSON.stringify({
  sck_marker_on_johanna_chain: 'OK',
  applied_before: applied.length,
  left_out_applied_later: leftOut.length,
  fingerprint: { before: before.fingerprint_status, after: after.fingerprint_status, later: later.fingerprint_status },
}));
await db.close();
