import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { PGlite } from '@electric-sql/pglite';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const stack = [
  'supabase/baseline/20260803_public_schema.sql',
  'supabase/migrations/20260803000100_followup_engine_v1.sql',
  'supabase/migrations/20260804000200_followup_identity_binding.sql',
  'supabase/migrations/20260805000100_followup_identity_audit.sql',
  'supabase/migrations/20260805000200_followup_contact_authorization_grant.sql',
  'supabase/migrations/20260805000300_per_case_conversation_anchor.sql',
  'supabase/migrations/20260813000100_absolute_followup_deadlines.sql',
  'supabase/migrations/20260816000100_commercial_case_root.sql',
  'supabase/migrations/20260816000200_inbound_commercial_case_draft_only.sql',
];

const db = new PGlite();
await db.waitReady;
await db.exec('create role anon noinherit; create role authenticated noinherit; create role service_role noinherit bypassrls;');
for (const [index, path] of stack.entries()) {
  let sql = await readFile(`${root}/${path}`, 'utf8');
  if (index === 0) sql = sql.replace('create extension if not exists pgcrypto;', '-- omitted');
  await db.exec(sql);
}

await db.exec(`
  insert into public.inbound_commercial_scope_versions (
    scope_key, version, status, tenant_key, chatwoot_account_id,
    chatwoot_inbox_id, external_product_id, offer_code,
    approved_by, approved_at, published_at
  ) values (
    'scope-probe-a', 1, 'published', 'tenant-probe-a', 7, 11,
    'product-probe', 'offer-probe', 'schema-probe', now(), now()
  );
`);

const args = ['scope-probe-a', 1, 9001, '5511999999999'];
const first = await db.query(`select * from public.admit_inbound_commercial_case($1,$2,$3,$4)`, args);
const replay = await db.query(`select * from public.admit_inbound_commercial_case($1,$2,$3,$4)`, args);
const a = first.rows[0];
const b = replay.rows[0];
if (a?.outcome !== 'created' || b?.outcome !== 'already_exists' || a.commercial_case_id !== b.commercial_case_id) {
  throw new Error('exact inbound admission was not idempotent');
}
const row = await db.query(`
  select case_kind, contact_id, selected_channel_identity_id, conversation_id,
         inbound_scope_key, inbound_scope_version, tenant_ref,
         product_ref, offer_ref, status, automation_status, authority_mode
  from public.commercial_cases where id = $1
`, [a.commercial_case_id]);
if (row.rows[0]?.case_kind !== 'inbound_sales'
    || row.rows[0]?.contact_id !== a.contact_id
    || row.rows[0]?.selected_channel_identity_id !== a.channel_identity_id
    || row.rows[0]?.conversation_id !== a.conversation_id
    || row.rows[0]?.inbound_scope_key !== 'scope-probe-a'
    || row.rows[0]?.inbound_scope_version !== 1
    || row.rows[0]?.tenant_ref !== 'tenant-probe-a'
    || row.rows[0]?.product_ref !== 'product-probe'
    || row.rows[0]?.offer_ref !== 'offer-probe'
    || row.rows[0]?.status !== 'active'
    || row.rows[0]?.automation_status !== 'draft_only'
    || row.rows[0]?.authority_mode !== 'shadow') {
  throw new Error('inbound case is not exact draft-only canonical state');
}
const canonical = await db.query(`
  select
    (select count(*) from public.contacts)::int as contacts,
    (select count(*) from public.channel_identities)::int as identities,
    (select count(*) from public.conversations)::int as conversations,
    contact.full_name,
    contact.email,
    contact.phone,
    contact.contact_permission,
    identity.external_conversation_id,
    identity.metadata ->> 'inbox_id' as inbox_id,
    conversation.commercial_context ->> 'chatwoot_conversation_id' as anchor
  from public.contacts contact
  join public.channel_identities identity on identity.contact_id = contact.id
  join public.conversations conversation on conversation.channel_identity_id = identity.id
  where contact.id = $1 and identity.id = $2 and conversation.id = $3
`, [a.contact_id, a.channel_identity_id, a.conversation_id]);
const c = canonical.rows[0];
if (c?.contacts !== 1 || c?.identities !== 1 || c?.conversations !== 1
    || c.full_name !== null || c.email !== null || c.phone !== null
    || c.contact_permission !== 'unknown' || c.external_conversation_id !== '9001'
    || c.inbox_id !== '11' || c.anchor !== '9001') {
  throw new Error('minimal canonical Chatwoot aggregate was not created safely');
}
const effects = await db.query(`
  select
    (select count(*) from public.followup_sequences)::int as sequences,
    (select count(*) from public.scheduled_actions)::int as actions,
    (select count(*) from public.followup_delivery_attempts)::int as attempts
`);
if (effects.rows[0]?.sequences !== 0 || effects.rows[0]?.actions !== 0 || effects.rows[0]?.attempts !== 0) {
  throw new Error('draft-only admission created effect work');
}

await db.exec(`
  update public.conversations
  set automation_status = 'paused', human_takeover = true
  where id = '${a.conversation_id}'
`);
const pausedReplay = await db.query(`
  select * from public.admit_inbound_commercial_case($1,$2,$3,$4)
`, args);
if (pausedReplay.rows[0]?.outcome !== 'already_exists'
    || pausedReplay.rows[0]?.commercial_case_id !== a.commercial_case_id) {
  throw new Error('exact replay was invalidated by later conversation pause');
}
await db.exec(`
  update public.conversations
  set automation_status = 'draft_only', human_takeover = false
  where id = '${a.conversation_id}'
`);

const conflict = await db.query(`
  select * from public.admit_inbound_commercial_case($1,$2,$3,$4)
`, ['scope-probe-a', 1, 9001, '5511888888888']);
if (conflict.rows[0]?.outcome !== 'evidence_conflict'
    || conflict.rows[0]?.commercial_case_id !== a.commercial_case_id) {
  throw new Error('semantic replay did not return durable conflict');
}
const conflictState = await db.query(`
  select
    (select count(*) from public.inbound_commercial_case_conflicts)::int as conflicts,
    (select count(*) from public.commercial_cases where case_kind = 'inbound_sales')::int as cases
`);
if (conflictState.rows[0]?.conflicts !== 1 || conflictState.rows[0]?.cases !== 1) {
  throw new Error('semantic conflict evidence or case cardinality is wrong');
}

await db.exec('grant usage on schema public to service_role; set role service_role;');
const serviceReplay = await db.query(`
  select * from public.admit_inbound_commercial_case($1,$2,$3,$4)
`, args);
if (serviceReplay.rows[0]?.outcome !== 'already_exists') {
  throw new Error('service_role cannot use the bounded admission RPC');
}
let directDmlDenied = false;
try {
  await db.exec(`insert into public.inbound_commercial_case_admissions (
    scope_key, scope_version, external_conversation_id, external_user_id,
    commercial_case_id, contact_id, channel_identity_id, conversation_id
  ) values (
    'scope-probe-a', 1, 9999, '5511777777777',
    '20000000-0000-0000-0000-000000000099',
    '${a.contact_id}', '${a.channel_identity_id}', '${a.conversation_id}'
  )`);
} catch (error) {
  directDmlDenied = String(error?.message ?? error).includes('permission denied');
}
await db.exec('reset role;');
if (!directDmlDenied) throw new Error('service_role direct admission DML was accepted');

const acl = await db.query(`
  select
    not has_function_privilege('anon', 'public.admit_inbound_commercial_case(text,integer,bigint,text)', 'execute') as anon_denied,
    not has_function_privilege('authenticated', 'public.admit_inbound_commercial_case(text,integer,bigint,text)', 'execute') as authenticated_denied,
    has_function_privilege('service_role', 'public.admit_inbound_commercial_case(text,integer,bigint,text)', 'execute') as service_allowed
`);
if (!acl.rows[0]?.anon_denied || !acl.rows[0]?.authenticated_denied || !acl.rows[0]?.service_allowed) {
  throw new Error('inbound admission RPC ACL is wrong');
}

for (const statement of [
  `update public.commercial_cases set offer_ref = 'forged' where id = '${a.commercial_case_id}'`,
  `delete from public.commercial_cases where id = '${a.commercial_case_id}'`,
]) {
  let rejected = false;
  try { await db.exec(statement); } catch (error) {
    rejected = String(error?.message ?? error).includes('inbound_commercial_case_is_immutable');
  }
  if (!rejected) throw new Error('direct inbound case mutation was accepted');
}

const secondConversation = await db.query(
  `select * from public.admit_inbound_commercial_case($1,$2,$3,$4)`,
  ['scope-probe-a', 1, 9002, '5511999999999'],
);
const second = secondConversation.rows[0];
if (second?.outcome !== 'created' || second.contact_id !== a.contact_id
    || second.channel_identity_id !== a.channel_identity_id
    || second.conversation_id === a.conversation_id) {
  throw new Error('second Chatwoot conversation did not reuse only the stable identity');
}
const identityPointer = await db.query(`
  select external_conversation_id from public.channel_identities where id = $1
`, [a.channel_identity_id]);
if (identityPointer.rows[0]?.external_conversation_id !== '9002') {
  throw new Error('identity denormalized conversation pointer did not advance');
}
const firstAfterSecond = await db.query(
  `select * from public.admit_inbound_commercial_case($1,$2,$3,$4)`, args,
);
if (firstAfterSecond.rows[0]?.outcome !== 'already_exists') {
  throw new Error('last-write-wins identity pointer invalidated an exact replay');
}

await db.exec(`
  insert into public.inbound_commercial_scope_versions (
    scope_key, version, status, tenant_key, chatwoot_account_id,
    chatwoot_inbox_id, external_product_id, offer_code,
    approved_by, approved_at, published_at
  ) values (
    'scope-probe-rival', 1, 'published', 'tenant-probe-rival', 7, 11,
    'product-probe', 'offer-probe', 'schema-probe', now(), now()
  )
`);
let rivalRejected = false;
try {
  await db.query(`select * from public.admit_inbound_commercial_case($1,$2,$3,$4)`,
    ['scope-probe-rival', 1, 9001, '5511888888888']);
} catch (error) {
  rivalRejected = String(error?.message ?? error).includes(
    'inbound_external_conversation_owned_by_another_identity',
  );
}
if (!rivalRejected) throw new Error('rival identity reclaimed a historical conversation anchor');
const anchorOwners = await db.query(`
  select count(*)::int as conversations,
         count(distinct conversation.channel_identity_id)::int as owners
  from public.conversations conversation
  join public.channel_identities identity on identity.id = conversation.channel_identity_id
  where identity.account_id = 'chatwoot:7'
    and conversation.commercial_context = jsonb_build_object('chatwoot_conversation_id', '9001')
`);
if (anchorOwners.rows[0]?.conversations !== 1 || anchorOwners.rows[0]?.owners !== 1) {
  throw new Error('historical conversation anchor has multiple owners');
}

await db.exec(`update public.channel_identities set identity_status = 'blocked' where id = '${a.channel_identity_id}'`);
const blockedReplay = await db.query(
  `select * from public.admit_inbound_commercial_case($1,$2,$3,$4)`, args,
);
if (blockedReplay.rows[0]?.outcome !== 'evidence_conflict') {
  throw new Error('blocked identity replay did not fail closed durably');
}
await db.exec(`update public.channel_identities set identity_status = 'active' where id = '${a.channel_identity_id}'`);

await db.exec(`
  insert into public.inbound_commercial_scope_versions (
    scope_key, version, status, tenant_key, chatwoot_account_id,
    chatwoot_inbox_id, external_product_id, offer_code,
    approved_by, approved_at, published_at
  ) values (
    'scope-probe-b', 2, 'published', 'tenant-probe-b', 7, 11,
    'product-probe', 'offer-probe', 'schema-probe', now(), now()
  )
`);
const secondScope = await db.query(
  `select * from public.admit_inbound_commercial_case($1,$2,$3,$4)`,
  ['scope-probe-b', 2, 9001, '5511999999999'],
);
if (secondScope.rows[0]?.outcome !== 'created'
    || secondScope.rows[0]?.commercial_case_id === a.commercial_case_id) {
  throw new Error('physical case uniqueness does not match the command scope');
}

await db.exec(`
  insert into public.contacts (id) values
    ('30000000-0000-0000-0000-000000000001'),
    ('30000000-0000-0000-0000-000000000011'),
    ('30000000-0000-0000-0000-000000000031');
  insert into public.channel_identities (
    id, contact_id, channel, account_id, external_user_id,
    external_conversation_id, identity_status, metadata
  ) values
    ('30000000-0000-0000-0000-000000000002',
     '30000000-0000-0000-0000-000000000001', 'whatsapp', 'chatwoot:999',
     '5511777777701', '9901', 'active', '{"inbox_id":"999"}'),
    ('30000000-0000-0000-0000-000000000012',
     '30000000-0000-0000-0000-000000000011', 'whatsapp', 'chatwoot:7',
     '5511777777702', '9902', 'active', '{"inbox_id":"11"}'),
    ('30000000-0000-0000-0000-000000000032',
     '30000000-0000-0000-0000-000000000031', 'whatsapp', 'chatwoot:7',
     '5511777777703', '9903', 'active', '{"inbox_id":"11"}');
  insert into public.conversations (
    id, contact_id, channel_identity_id, status, automation_status, commercial_context
  ) values
    ('30000000-0000-0000-0000-000000000003',
     '30000000-0000-0000-0000-000000000001',
     '30000000-0000-0000-0000-000000000002', 'active', 'draft_only',
     '{"chatwoot_conversation_id":"9901"}'),
    ('30000000-0000-0000-0000-000000000013',
     '30000000-0000-0000-0000-000000000011',
     '30000000-0000-0000-0000-000000000012', 'active', 'draft_only',
     '{"chatwoot_conversation_id":"9902","extra_untrusted_context":"present"}'),
    ('30000000-0000-0000-0000-000000000033',
     '30000000-0000-0000-0000-000000000031',
     '30000000-0000-0000-0000-000000000032', 'active', 'draft_only',
     '{"chatwoot_conversation_id":null}');
`);
for (const [id, contactId, identityId, conversationId, message] of [
  ['30000000-0000-0000-0000-000000000004', '30000000-0000-0000-0000-000000000001',
   '30000000-0000-0000-0000-000000000002', '30000000-0000-0000-0000-000000000003',
   'scope routing mismatch'],
  ['30000000-0000-0000-0000-000000000014', '30000000-0000-0000-0000-000000000011',
   '30000000-0000-0000-0000-000000000012', '30000000-0000-0000-0000-000000000013',
   'non-minimal conversation context'],
  ['30000000-0000-0000-0000-000000000034', '30000000-0000-0000-0000-000000000031',
   '30000000-0000-0000-0000-000000000032', '30000000-0000-0000-0000-000000000033',
   'null conversation anchor'],
]) {
  let rejected = false;
  try {
    await db.exec(`insert into public.commercial_cases (
      id, case_kind, contact_id, selected_channel_identity_id, conversation_id,
      inbound_scope_key, inbound_scope_version, tenant_ref, product_ref, offer_ref,
      status, automation_status, identity_resolution_status, authority_mode, version
    ) values (
      '${id}', 'inbound_sales', '${contactId}', '${identityId}', '${conversationId}',
      'scope-probe-a', 1, 'tenant-probe-a', 'product-probe', 'offer-probe',
      'active', 'draft_only', 'resolved', 'shadow', 1
    )`);
  } catch (error) {
    rejected = String(error?.message ?? error).includes('inbound_commercial_case_canonical_mismatch');
  }
  if (!rejected) throw new Error(`${message} bypassed physical case guard`);
}

await db.exec(`
  insert into public.inbound_commercial_scope_versions (
    scope_key, version, status, tenant_key, chatwoot_account_id,
    chatwoot_inbox_id, external_product_id, offer_code,
    approved_by, approved_at, published_at
  ) values (
    'scope-probe-guard', 1, 'published', 'tenant-probe-guard', 7, 11,
    'product-probe', 'offer-probe', 'schema-probe', now(), now()
  );
  insert into public.commercial_cases (
    id, case_kind, contact_id, selected_channel_identity_id, conversation_id,
    inbound_scope_key, inbound_scope_version, tenant_ref, product_ref, offer_ref,
    status, automation_status, identity_resolution_status, authority_mode, version
  ) values (
    '30000000-0000-0000-0000-000000000021', 'inbound_sales',
    '${a.contact_id}', '${a.channel_identity_id}', '${a.conversation_id}',
    'scope-probe-guard', 1, 'tenant-probe-guard', 'product-probe', 'offer-probe',
    'active', 'draft_only', 'resolved', 'shadow', 1
  );
`);
let forgedAdmissionRejected = false;
try {
  await db.exec(`insert into public.inbound_commercial_case_admissions (
    scope_key, scope_version, external_conversation_id, external_user_id,
    commercial_case_id, contact_id, channel_identity_id, conversation_id
  ) values (
    'scope-probe-guard', 1, 9001, '5511000000099',
    '30000000-0000-0000-0000-000000000021',
    '${a.contact_id}', '${a.channel_identity_id}', '${a.conversation_id}'
  )`);
} catch (error) {
  forgedAdmissionRejected = String(error?.message ?? error).includes(
    'inbound_commercial_case_admission_mismatch',
  );
}
if (!forgedAdmissionRejected) throw new Error('forged admission bypassed physical ledger guard');
console.log('inbound_commercial_case_draft_only=OK');
await db.close();
