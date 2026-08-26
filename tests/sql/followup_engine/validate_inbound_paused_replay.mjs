import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join, resolve } from 'node:path';
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

await db.exec(`
  insert into public.inbound_commercial_scope_versions (
    scope_key, version, status, tenant_key, chatwoot_account_id,
    chatwoot_inbox_id, external_product_id, offer_code,
    approved_by, approved_at, published_at
  ) values (
    'paused-replay-probe', 1, 'published', 'tenant-probe', 7, 11,
    'product-probe', 'offer-probe', 'schema-probe', now(), now()
  );
  insert into public.human_handoff_projection_policies (
    policy_key, policy_version, scope_key, scope_version,
    inbound_scope_key, inbound_scope_version, expected_team_id,
    note_template_key, note_template_version, private_note_body, active
  ) values (
    'paused-replay-handoff', 1, null, null,
    'paused-replay-probe', 1, 17,
    'handoff-note', 1, 'Human review required.', true
  );
`);
const args = ['paused-replay-probe', 1, 9101, '5511999999999'];
const created = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)', args,
)).rows[0];
const activeReplay = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)', args,
)).rows[0];
if (created?.outcome !== 'created' || activeReplay?.outcome !== 'already_exists') {
  throw new Error('active inbound replay did not remain replyable');
}

const handoff = (await db.query(`
  select * from public.request_inbound_human_handoff(
    $1::uuid,
    'handoff:paused-replay-probe',
    'policy_requires_human',
    'paused-replay-handoff',
    1,
    now()
  )
`, [created.commercial_case_id])).rows[0];
if (handoff?.outcome !== 'requested') {
  throw new Error('durable inbound handoff did not pause the fixture');
}
const blocked = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)', args,
)).rows[0];
if (blocked?.outcome !== 'blocked'
    || blocked.commercial_case_id !== created.commercial_case_id
    || blocked.automation_status !== 'disabled') {
  throw new Error('paused durable aggregate replay was not blocked explicitly');
}
const legacy = (await db.query(
  'select * from public.admit_inbound_commercial_case($1,$2,$3,$4)', args,
)).rows[0];
if (legacy?.outcome !== 'evidence_conflict'
    || legacy.commercial_case_id !== created.commercial_case_id) {
  throw new Error('legacy rolling caller did not fail closed on paused replay');
}

await db.exec('grant usage on schema public to service_role; set role service_role;');
const serviceBlocked = (await db.query(
  'select * from public.admit_inbound_commercial_case_v2($1,$2,$3,$4)', args,
)).rows[0];
if (serviceBlocked?.outcome !== 'blocked') {
  throw new Error('service_role could not observe the durable blocked result');
}
const acl = (await db.query(`
  select
    not has_function_privilege('anon', 'public.admit_inbound_commercial_case_v2(text,integer,bigint,text)', 'execute') as anon_denied,
    not has_function_privilege('authenticated', 'public.admit_inbound_commercial_case_v2(text,integer,bigint,text)', 'execute') as authenticated_denied,
    has_function_privilege('service_role', 'public.admit_inbound_commercial_case_v2(text,integer,bigint,text)', 'execute') as service_allowed,
    not has_function_privilege('service_role', 'public.admit_inbound_commercial_case_base(text,integer,bigint,text)', 'execute') as base_denied
`)).rows[0];
await db.exec('reset role;');
if (!acl?.anon_denied || !acl.authenticated_denied || !acl.service_allowed || !acl.base_denied) {
  throw new Error('paused replay RPC ACL is wrong');
}

console.log('INBOUND_PAUSED_REPLAY_SQL_OK');
