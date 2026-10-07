// El tope de mensajes proactivos por persona entre flujos (20261007000100).
//
// Sobre la cadena completa de migraciones, con privilegios por defecto como
// los de Supabase, llama a la puerta comun de los tres arranques
// (authorize_lancemos_pilot_request_start) con dos scopes del mismo tenant (el
// primer contacto y la recuperacion de una instancia) y uno de otro tenant.
// Cada scope tiene su cohorte y sus topes holgados: lo que se mide es el tope
// por persona, no el del scope.
//
// Casos:
//   1. sin renglon del tenant no hay tope: la misma persona se autoriza en los
//      dos scopes (lo de hoy, y lo de Johanna);
//   2. con el tope (1 en 24 h): la persona autorizada en un scope queda frenada
//      en el otro con pilot_contact_proactive_cap_reached, sin renglon en el
//      ledger, sin evento y sin consumir el cupo del scope;
//   3. el replay del arranque ya autorizado sigue devolviendo la misma
//      autorizacion (replayed), con el tope lleno;
//   4. otra persona en el mismo scope se autoriza: el tope es por persona;
//   5. otro contacto de la misma persona (el telefono en la otra forma, 52 y
//      521) tambien queda frenado;
//   6. otro tenant, sin renglon, no se cuenta ni se frena;
//   7. pasada la ventana (la autorizacion movida 25 h atras, solo en esta base)
//      el arranque frenado se autoriza;
//   8. con max_request_starts = 2 entran dos y el tercero se frena;
//   9. la tabla del tope no tiene privilegios para la API;
//  10. la definicion vigente de la funcion trae el bloque una sola vez.
//
// No cubre la carrera de dos scopes a la vez (PGlite es una sola conexion): la
// serializa el pg_advisory_xact_lock por tenant y telefono de la migracion.
import { readdir, readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { PGlite } from '@electric-sql/pglite';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const baseline = (await readFile(
  `${root}/supabase/baseline/20260803_public_schema.sql`,
  'utf8',
)).replace(
  'create extension if not exists pgcrypto;',
  '-- omitted in PGlite: extension unavailable',
);
const migrationDir = `${root}/supabase/migrations`;
const migrationNames = (await readdir(migrationDir))
  .filter((name) => name.endsWith('.sql'))
  .sort();

const db = new PGlite();
await db.waitReady;
await db.exec(baseline);
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  alter default privileges grant execute on functions to anon, authenticated;
  alter default privileges grant all on tables to service_role;
`);
for (const name of migrationNames) {
  await db.exec(await readFile(`${migrationDir}/${name}`, 'utf8'));
}

const TENANT = 'cap-tenant';
const OTHER_TENANT = 'cap-otro-tenant';
const FIRST_SCOPE = 'cap-primer-contacto';
const RECOVERY_SCOPE = 'cap-recuperacion';
const OTHER_SCOPE = 'cap-otro';
const SCOPES = [
  [FIRST_SCOPE, TENANT],
  [RECOVERY_SCOPE, TENANT],
  [OTHER_SCOPE, OTHER_TENANT],
];

const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};

await db.exec(`
  insert into public.followup_policy_versions (
    policy_key, version, status, purpose, timezone, business_windows,
    grace_period, expires_after, max_automatic_messages, steps,
    approved_by, approved_at, published_at
  ) values (
    'cap-policy', 1, 'published', 'cart_recovery', 'UTC',
    '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]',
    interval '0 seconds', interval '30 days', 4,
    '[{"step_key":"first_contact","mode":"freeform"}]',
    'operator-test', now(), now()
  );
`);
for (const [scopeKey, tenant] of SCOPES) {
  await db.query(`
    insert into public.pilot_scope_versions (
      scope_key, version, status, tenant_key,
      chatwoot_account_id, chatwoot_inbox_id,
      channel, channel_provider, channel_account_ref,
      source, source_event_type, external_product_id, offer_code, purpose,
      policy_key, policy_version, timezone,
      max_cohort_contacts, max_outbound_request_starts_total,
      max_outbound_request_starts_per_day,
      approved_by, approved_at, published_at
    ) values (
      $1, 1, 'published', $2,
      10, 20, 'whatsapp', 'waba', 'opaque-number-ref',
      'hotmart', 'PURCHASE_OUT_OF_SHOPPING_CART', '3526906', 'offer-1',
      'cart_recovery', 'cap-policy', 1, 'America/Mexico_City',
      20, 50, 50, 'operator-test', now(), now()
    )
  `, [scopeKey, tenant]);
  await db.query(`
    insert into public.pilot_runtime_controls (
      scope_key, scope_version, runtime_state, generation, changed_by, change_reason
    ) values ($1, 1, 'inactive', 0, 'migration-test', 'default-off')
  `, [scopeKey]);
  const armed = one((await db.query(`
    select * from public.set_lancemos_pilot_runtime_state($1, 1, 0, 'armed', 'operator-test', 'cap-test')
  `, [scopeKey])).rows, `${scopeKey} armed`);
  if (armed.runtime_state !== 'armed') throw new Error(`${scopeKey} did not arm`);
}

// Una persona: un contacto con su carrito admitido y planificado (el caso, la
// identidad de WhatsApp y la accion), inscripta en las cohortes de los tres
// scopes, y un intento reservado por scope.
let attemptNumber = 0;
const reserveAttempt = async (actionId) => {
  attemptNumber += 1;
  return one((await db.query(`
    insert into public.followup_delivery_attempts (
      action_id, idempotency_key, attempt_number, channel, mode,
      phase, started_at, lease_generation,
      expected_case_version, expected_sequence_revision
    ) values ($1, $2, $3, 'whatsapp', 'freeform', 'reserved', clock_timestamp(), $4, 1, 1)
    returning id
  `, [actionId, `cap-attempt-${attemptNumber}`, attemptNumber, attemptNumber])).rows, 'attempt').id;
};
const person = async (label, phone) => {
  const email = `cap-${label}@example.test`;
  const contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone) values ($1, $2, $3) returning id
  `, [`Cap ${label}`, email, phone])).rows, `${label} contact`).id;
  const eventId = `cap-event-${label}`;
  const abandonedAt = new Date(Date.now() - 60_000);
  const payload = {
    id: eventId,
    creation_date: abandonedAt.getTime(),
    event: 'PURCHASE_OUT_OF_SHOPPING_CART',
    version: '2.0.0',
    data: {
      buyer: { email, phone },
      product: { id: 3526906, name: 'Product One' },
      offer: { code: 'offer-1' },
    },
  };
  const admitted = one((await db.query(`
    select * from public.admit_and_correlate_hotmart_cart_abandonment($1, $2::jsonb, $3, $4)
  `, [eventId, JSON.stringify(payload), email, phone])).rows, `${label} admission`);
  await db.query(`
    insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id)
    values ($1,'email',$2,$2,'hotmart',$3), ($1,'phone',$4,$4,'hotmart',$3)
  `, [contact, email, admitted.webhook_event_id, phone]);
  const plan = one((await db.query(`
    select * from public.plan_cart_recovery_with_identity(
      $1, $2, '3526906', 'Product One', 'offer-1', 'cap-policy', 1,
      $4::timestamptz, 10, 20, $3
    )
  `, [admitted.webhook_event_id, contact, phone, abandonedAt.toISOString()])).rows, `${label} plan`);
  if (plan.scheduled_action_id == null) throw new Error(`${label}: the plan returned no action`);
  for (const [scopeKey] of SCOPES) {
    const generation = one((await db.query(`
      select generation from public.pilot_runtime_controls where scope_key = $1
    `, [scopeKey])).rows, 'generation').generation;
    const member = one((await db.query(`
      select * from public.set_lancemos_pilot_cohort_member($1, 1, $2, $3, 'active', 'operator-test', 'cap-test')
    `, [scopeKey, contact, generation])).rows, `${label} enrollment`);
    if (member.member_status !== 'active') throw new Error(`${label} was not enrolled in ${scopeKey}`);
  }
  const attempts = {};
  for (const [scopeKey] of SCOPES) attempts[scopeKey] = await reserveAttempt(plan.scheduled_action_id);
  return { label, contact, phone, actionId: plan.scheduled_action_id, attempts };
};

const authorize = async (who, scopeKey, { attemptId = who.attempts[scopeKey] } = {}) => {
  const tenant = SCOPES.find(([key]) => key === scopeKey)[1];
  return one((await db.query(`
    select * from public.authorize_lancemos_pilot_request_start(
      $1, 1, $2, 10, 20, 'waba', 'opaque-number-ref', 'hotmart',
      'PURCHASE_OUT_OF_SHOPPING_CART', '3526906', 'offer-1', $3, $4, $5, clock_timestamp()
    )
  `, [scopeKey, tenant, who.contact, who.actionId, attemptId])).rows, `${who.label} ${scopeKey}`);
};
const expectAuthorized = (label, row) => {
  if (row.authorized !== true || row.reason_code !== 'pilot_request_start_authorized'
      || row.replayed !== false || row.request_authorization_id == null) {
    throw new Error(`${label}: expected a new authorization, got ${JSON.stringify(row)}`);
  }
};
const expectCapped = (label, row) => {
  if (row.authorized !== false || row.reason_code !== 'pilot_contact_proactive_cap_reached'
      || row.request_authorization_id !== null || row.replayed !== false) {
    throw new Error(`${label}: expected pilot_contact_proactive_cap_reached, got ${JSON.stringify(row)}`);
  }
};
const ledger = async () => one((await db.query(`
  select (select count(*)::integer from public.pilot_outbound_request_authorizations) as authorizations,
         (select count(*)::integer from public.pilot_control_events
          where event_type = 'pilot_outbound_request_authorized') as events
`)).rows, 'ledger');
const setCap = (max, window = '24 hours') => db.query(`
  insert into public.pilot_proactive_contact_caps (tenant_key, max_request_starts, request_window, approved_by)
  values ($1, $2, $3::interval, 'operator-test')
  on conflict (tenant_key) do update
     set max_request_starts = excluded.max_request_starts,
         request_window = excluded.request_window,
         updated_at = clock_timestamp()
`, [TENANT, max, window]);

// 1. Sin renglon del tenant no hay tope.
const free = await person('sin-tope', '5215500000101');
expectAuthorized('no cap, first contact scope', await authorize(free, FIRST_SCOPE));
expectAuthorized('no cap, recovery scope', await authorize(free, RECOVERY_SCOPE));
console.log('proactive_cap_absent_keeps_today=OK');

// 2. Con el tope (1 en 24 h), entre los dos scopes del tenant.
await setCap(1);
const capped = await person('con-tope', '5215500000102');
expectAuthorized('cap, first contact', await authorize(capped, FIRST_SCOPE));
const before = await ledger();
const blocked = await authorize(capped, RECOVERY_SCOPE);
expectCapped('cap, the recovery after the first contact', blocked);
const after = await ledger();
const recoveryStarts = one((await db.query(`
  select count(*)::integer as count from public.pilot_outbound_request_authorizations where scope_key = $1
`, [RECOVERY_SCOPE])).rows, 'recovery starts').count;
if (after.authorizations !== before.authorizations || after.events !== before.events
    || recoveryStarts !== 1) {
  throw new Error(`the capped start wrote to the ledger or consumed budget: ${JSON.stringify({ before, after, recoveryStarts })}`);
}
console.log('proactive_cap_blocks_across_scopes=OK');

// 3. El replay del arranque ya autorizado no se frena.
const replay = await authorize(capped, FIRST_SCOPE);
if (replay.authorized !== true || replay.replayed !== true) {
  throw new Error(`the replay was capped: ${JSON.stringify(replay)}`);
}
console.log('proactive_cap_keeps_replay=OK');

// 4. Otra persona en el mismo scope.
const other = await person('otra-persona', '5215500000103');
expectAuthorized('cap, another person', await authorize(other, RECOVERY_SCOPE));
console.log('proactive_cap_is_per_person=OK');

// 5. Otro contacto de la misma persona, con el telefono en la otra forma.
const twin = await person('mismo-telefono', '525500000102');
expectCapped('cap, another contact with the other form of the phone', await authorize(twin, RECOVERY_SCOPE));
console.log('proactive_cap_follows_phone_forms=OK');

// 6. Otro tenant, sin renglon: no cuenta lo del tenant con tope ni se frena.
expectAuthorized('cap, another tenant', await authorize(capped, OTHER_SCOPE));
console.log('proactive_cap_is_per_tenant=OK');

// 7. Pasada la ventana. Solo en esta base descartable: el ledger es
//    append-only, asi que se apaga su trigger para mover la autorizacion.
await db.exec('alter table public.pilot_outbound_request_authorizations disable trigger pilot_outbound_authorizations_append_only');
await db.query(`
  update public.pilot_outbound_request_authorizations
     set authorized_at = authorized_at - interval '25 hours'
   where contact_id = $1 and scope_key = $2
`, [capped.contact, FIRST_SCOPE]);
await db.exec('alter table public.pilot_outbound_request_authorizations enable trigger pilot_outbound_authorizations_append_only');
expectAuthorized('cap, after the window', await authorize(capped, RECOVERY_SCOPE));
console.log('proactive_cap_window_expires=OK');

// 8. Con max 2: entran dos y el tercero se frena.
await setCap(2);
const two = await person('dos-mensajes', '5215500000104');
expectAuthorized('cap 2, first', await authorize(two, FIRST_SCOPE));
expectAuthorized('cap 2, second', await authorize(two, RECOVERY_SCOPE));
const third = await reserveAttempt(two.actionId);
expectCapped('cap 2, third', await authorize(two, FIRST_SCOPE, { attemptId: third }));
console.log('proactive_cap_max_two=OK');

// 9. La tabla del tope, para nadie de la API.
const privileges = one((await db.query(`
  select has_table_privilege('anon', 'public.pilot_proactive_contact_caps', 'SELECT,INSERT,UPDATE,DELETE') as anon,
         has_table_privilege('authenticated', 'public.pilot_proactive_contact_caps', 'SELECT,INSERT,UPDATE,DELETE') as authenticated,
         has_table_privilege('service_role', 'public.pilot_proactive_contact_caps', 'SELECT,INSERT,UPDATE,DELETE') as service_role,
         (select relrowsecurity from pg_class where oid = 'public.pilot_proactive_contact_caps'::regclass) as rls
`)).rows, 'cap table privileges');
if (privileges.anon || privileges.authenticated || privileges.service_role || !privileges.rls) {
  throw new Error(`the cap table is reachable from the API: ${JSON.stringify(privileges)}`);
}
console.log('proactive_cap_table_closed=OK');

// 10. El bloque, una vez, en la definicion vigente.
const definition = one((await db.query(`
  select pg_get_functiondef('public.authorize_lancemos_pilot_request_start(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid,uuid,uuid,timestamptz)'::regprocedure) as body
`)).rows, 'definition').body;
const blocks = definition.split('pilot_proactive_contact_cap: begin').length - 1;
const capPosition = definition.indexOf('pilot_proactive_contact_cap: begin');
const totalPosition = definition.indexOf('pilot_total_budget_exhausted');
if (blocks !== 1 || capPosition < 0 || totalPosition < capPosition) {
  throw new Error(`the cap block is not once and before the scope budgets: ${JSON.stringify({ blocks, capPosition, totalPosition })}`);
}
console.log('proactive_cap_block_once=OK');

console.log('PILOT_PROACTIVE_CONTACT_CAP_OK');
await db.close();
