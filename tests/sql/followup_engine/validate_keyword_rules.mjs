// Las reglas de palabra clave (migracion 20261126400000, keyword-rules-v1, Z1 3.9), con las RPC reales sobre
// la cadena completa de migraciones. Contrato: docs/contracts/keyword-rules-v1.md.
//
// Prueba, con un rojo por pieza:
//   0. la forma: agrega exactamente sus funciones y sus dos tablas, no cambia ninguna otra funcion; cada
//      funcion publica la ejecuta solo su rol; las tablas con RLS y sin permisos para la API; correrla dos
//      veces aborta (55000);
//   1. el agente: guardar, rehacer (sube la revision y cambia la huella), revision vieja, clave inmutable,
//      clave tomada, tope de borradores, una regla sin forma o con un flujo (22023); no puede publicar (42501);
//   2. la pagina: con otra huella no se publica (22023), sin sesion 28000, con la huella queda publicada y
//      quien la aprobo es el de la sesion; repetir es replayed; la lectura viva trae solo la publicada y sin IDs;
//   3. la misma palabra no esta en dos reglas vivas o pausadas (respuesta y indice unico); el tope de 20 vivas;
//   4. pausar, reanudar y descartar (una viva no se descarta sin pausarla);
//   5. los disparadores: contenido congelado desde publicada, recorrido invalido, permanente, solo agregar;
//      el agente no ve ningun ID de Slack.
//
// Datos sinteticos: los de buildWorld (el manifiesto de ATT1) y reglas ficticias.
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261126400000_keyword_rules_v1.sql';
const sha = (text) => createHash('sha256').update(text).digest('hex');

const AE_FUNCTIONS = [
  'save_keyword_rule_draft_v1(jsonb,uuid,integer,jsonb,text,text)',
  'get_keyword_rule_v1(jsonb,uuid)',
  'list_keyword_rules_v1(jsonb,boolean,integer)',
];
const APPROVER_FUNCTIONS = [
  'approver_get_keyword_rule_v1(jsonb,text,uuid)',
  'approver_publish_keyword_rule_v1(jsonb,text,uuid,text)',
  'approver_pause_keyword_rule_v1(jsonb,text,uuid,text)',
  'approver_resume_keyword_rule_v1(jsonb,text,uuid)',
  'approver_discard_keyword_rule_v1(jsonb,text,uuid,text)',
];
const SERVICE_FUNCTIONS = ['get_live_keyword_rules_v1(jsonb)'];
const INTERNAL_FUNCTIONS = [
  '_keyword_rule_is_valid(jsonb)',
  'reject_keyword_rule_mutation()',
  'validate_keyword_rule_change()',
  '_keyword_rule_event(keyword_rules,text,text,text,text,jsonb)',
  '_keyword_rule_fingerprint(uuid,integer,jsonb,text)',
  '_keyword_rule_view(keyword_rules,boolean)',
];
const ALL_FUNCTIONS = [...AE_FUNCTIONS, ...APPROVER_FUNCTIONS, ...SERVICE_FUNCTIONS, ...INTERNAL_FUNCTIONS];

const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature.replace(/^public\./, '').replace(/ /g, ''), row]));
const tables = async (db) => (await db.query(`select c.relname from pg_class c
  where c.relnamespace = 'public'::regnamespace and c.relkind = 'r' order by 1`)).rows.map((row) => row.relname);

const shapeDb = new PGlite();
await shapeDb.waitReady;
await shapeDb.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  create role authenticator noinherit login;
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
  alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
  grant usage on schema public to service_role, anon;
`);
const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`${MIGRATION} no esta en la cadena`);
const applyFile = async (db, file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi, '-- pgcrypto is built into PGlite'));
await applyFile(shapeDb, join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of names.slice(0, position)) await applyFile(shapeDb, join(root, 'supabase/migrations', name));
const before = await snapshot(shapeDb);
const tablesBefore = await tables(shapeDb);
const target = join(root, 'supabase/migrations', MIGRATION);
await applyFile(shapeDb, target);
const after = await snapshot(shapeDb);
{
  const added = [...after.keys()].filter((signature) => !before.has(signature));
  const removed = [...before.keys()].filter((signature) => !after.has(signature));
  const changed = [...before.keys()].filter((signature) => after.has(signature)
    && (after.get(signature).definition !== before.get(signature).definition
        || after.get(signature).acl !== before.get(signature).acl));
  if (!sameSet(added, ALL_FUNCTIONS) || removed.length !== 0 || changed.length !== 0) {
    fail('la migracion cambio otra cosa', { added, removed, changed });
  }
  const newTables = (await tables(shapeDb)).filter((table) => !tablesBefore.includes(table));
  if (!sameSet(newTables, ['keyword_rules', 'keyword_rule_events'])) fail('las tablas', newTables);
  const can = async (role, signature) => one((await shapeDb.query(
    `select has_function_privilege($1, to_regprocedure('public.${signature}'), 'execute') as x`, [role])).rows,
  role).x;
  const roles = ['automation_expert_change', 'flow_change_approver', 'service_role', 'anon', 'authenticated'];
  for (const signature of ALL_FUNCTIONS) {
    const allowed = [];
    for (const role of roles) if (await can(role, signature)) allowed.push(role);
    const expected = AE_FUNCTIONS.includes(signature) ? ['automation_expert_change']
      : APPROVER_FUNCTIONS.includes(signature) ? ['flow_change_approver']
        : SERVICE_FUNCTIONS.includes(signature) ? ['service_role'] : [];
    if (!sameSet(allowed, expected)) fail(`los permisos de ${signature}`, allowed);
  }
  const definers = (await shapeDb.query(`select p.oid::regprocedure::text sig, prosecdef,
      coalesce(array_to_string(proconfig, ','), '') config
    from pg_proc p where p.pronamespace = 'public'::regnamespace and p.proname like '%keyword_rule%'`)).rows;
  for (const row of definers) {
    const signature = row.sig.replace(/^public\./, '').replace(/ /g, '');
    const publicOne = !INTERNAL_FUNCTIONS.includes(signature);
    if (row.prosecdef !== publicOne) fail(`security definer solo las publicas: ${signature}`, row);
    if (!row.config.includes('search_path=pg_catalog')) fail(`search_path fijo: ${signature}`, row);
  }
  const tableAcl = (await shapeDb.query(`select c.relname, c.relrowsecurity,
      has_table_privilege('anon', c.oid, 'select') a, has_table_privilege('authenticated', c.oid, 'select') u,
      has_table_privilege('service_role', c.oid, 'select') s,
      has_table_privilege('automation_expert_change', c.oid, 'select') x
    from pg_class c where c.oid in ('public.keyword_rules'::regclass, 'public.keyword_rule_events'::regclass)`)).rows;
  for (const row of tableAcl) {
    if (!row.relrowsecurity || row.a || row.u || row.s || row.x) fail('las tablas, sin permisos y con RLS', row);
  }
  await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'keyword_rules_already_applied' });
}
await shapeDb.close();

// ---------------------------------------------------------------------------
// El mundo: ATT1, con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, INSTANCE, rpc } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const AE = 'U0123456789';
const DAN = 'U0DANSCHWAB1';
const TEAM = 'T0LANCEMOS01';
const later = async () => db.exec('select pg_sleep(0.01)');
const as = async (role, run) => {
  await later();
  await db.exec(`set role ${role}`);
  const out = await run();
  await db.exec('reset role');
  return out;
};
const agent = (run) => as('automation_expert_change', run);
const approver = (run) => as('flow_change_approver', run);
const service = (run) => as('service_role', run);
const inMinutes = async (minutes) => new Date((await world.dbNow()).getTime() + minutes * 60_000).toISOString();
const row = async (id) => one(await sql('select * from public.keyword_rules where id = $1', [id]), 'fila');

// La regla como la normaliza keyword_rules.parse_rule.
const RULE = (overrides = {}) => ({
  clave: 'soporte', nombre: 'Link de soporte', palabra_clave: 'Hola, necesito soporte',
  palabra_normalizada: 'hola necesito soporte', etiqueta: 'soporte', macro_etiqueta: 77, ...overrides,
});
const save = (rule, extra = {}) => agent(() => rpc('save_keyword_rule_draft_v1', INSTANCE, extra.id ?? null,
  extra.revision ?? null, rule, extra.purpose ?? 'El link de soporte', AE));

let serial = 0;
const signIn = async (userId) => {
  serial += 1;
  const state = sha(`state-${serial}`);
  const session = sha(`session-${serial}`);
  await approver(async () => {
    await rpc('begin_flow_approval_oidc_v1', INSTANCE, state, sha(`nonce-${serial}`), '/flow-approval/',
      await inMinutes(10));
    await rpc('consume_flow_approval_oidc_v1', INSTANCE, state);
    const created = await rpc('create_flow_approval_session_v1', INSTANCE, state, session, TEAM, userId,
      await inMinutes(8 * 60));
    if (!created.ok) fail('la sesion', created);
  });
  return session;
};
const S = await signIn(DAN);
const page = (fn, ...params) => approver(() => rpc(fn, INSTANCE, S, ...params));
const publish = (id, fingerprint) => page('approver_publish_keyword_rule_v1', id, fingerprint);

// 1. El agente.
let main;
{
  const saved = await save(RULE());
  if (!saved.ok || saved.status !== 'borrador' || saved.revision !== 1 || !/^[0-9a-f]{64}$/.test(saved.fingerprint)) {
    fail('guardar', saved);
  }
  const redo = await save(RULE({ nombre: 'Otro nombre' }), { id: saved.rule_id, revision: 1 });
  if (!redo.ok || redo.revision !== 2 || redo.fingerprint === saved.fingerprint) fail('rehacer cambia la huella', redo);
  const stale = await save(RULE(), { id: saved.rule_id, revision: 1 });
  if (stale.ok || stale.reason_code !== 'keyword_rule_revision_mismatch') fail('la revision vieja', stale);
  const key = await save(RULE({ clave: 'otra' }), { id: saved.rule_id, revision: 2 });
  if (key.ok || key.reason_code !== 'keyword_rule_key_is_immutable') fail('la clave no cambia', key);
  const taken = await save(RULE({ palabra_clave: 'otra cosa', palabra_normalizada: 'otra cosa' }));
  if (taken.ok || taken.reason_code !== 'keyword_rule_key_taken') fail('la clave tomada', taken);
  await expectError(db, 'una regla sin forma', () => save({ ...RULE(), extra: 1 }),
    { code: '22023', message: 'keyword_rule_invalid' });
  await expectError(db, 'una regla con flujo', () => save({ ...RULE(), flujo: { producto: 'p', flujo: 'f' } }),
    { code: '22023', message: 'keyword_rule_invalid' });
  await expectError(db, 'una palabra sin normalizar', () => save(RULE({ palabra_normalizada: 'Hola, Soporte' })),
    { code: '22023', message: 'keyword_rule_invalid' });
  await expectError(db, 'el agente no publica', () => agent(() => rpc('approver_publish_keyword_rule_v1', INSTANCE, S,
    saved.rule_id, redo.fingerprint)), { code: '42501' });
  // Un tope de diez borradores.
  for (let index = 2; index <= 10; index += 1) {
    const extra = await save(RULE({ clave: `r${index}`, palabra_clave: `palabra ${index}`,
      palabra_normalizada: `palabra ${index}` }));
    if (!extra.ok) fail(`borrador ${index}`, extra);
  }
  const limit = await save(RULE({ clave: 'r11', palabra_clave: 'palabra once', palabra_normalizada: 'palabra once' }));
  if (limit.ok || limit.reason_code !== 'keyword_rule_open_limit') fail('el tope', limit);
  const list = await agent(() => rpc('list_keyword_rules_v1', INSTANCE, true, 50));
  if (list.rules.length !== 10 || JSON.stringify(list).includes(DAN) || JSON.stringify(list).includes(AE)) {
    fail('la lista del agente, sin IDs', list);
  }
  main = { id: saved.rule_id, fingerprint: redo.fingerprint };
}

// 2. La pagina: con la huella, publicada.
{
  await expectError(db, 'otra huella', () => publish(main.id, 'e'.repeat(64)),
    { code: '22023', message: 'keyword_rule_fingerprint_mismatch' });
  await expectError(db, 'sin sesion', () => approver(() => rpc('approver_publish_keyword_rule_v1', INSTANCE,
    sha('sesion-falsa'), main.id, main.fingerprint)), { code: '28000' });
  const published = await publish(main.id, main.fingerprint);
  if (!published.ok || published.status !== 'publicada' || published.replayed) fail('publicar', published);
  const again = await publish(main.id, main.fingerprint);
  if (!again.ok || !again.replayed) fail('repetir', again);
  const stored = await row(main.id);
  if (stored.approved_by_ref !== DAN || stored.approved_by_kind !== 'slack') fail('quien aprobo', stored);
  const live = await service(() => rpc('get_live_keyword_rules_v1', INSTANCE));
  if (live.rules.length !== 1 || live.rules[0].rule_id !== main.id || live.rules[0].rule.etiqueta !== 'soporte'
      || JSON.stringify(live).includes(DAN) || JSON.stringify(live).includes(AE)) fail('la lectura viva', live);
}

// 3. La misma palabra en una sola regla viva o pausada.
{
  const twin = await save(RULE({ clave: 'gemela', etiqueta: 'gemela', nombre: 'Gemela' }));
  // El tope: se libera un lugar descartando un borrador.
  if (!twin.ok) {
    const [open] = await sql(`select id from public.keyword_rules where status = 'borrador' order by created_at limit 1`);
    await page('approver_discard_keyword_rule_v1', open.id, 'lugar para la prueba');
  }
  const created = twin.ok ? twin : await save(RULE({ clave: 'gemela', etiqueta: 'gemela', nombre: 'Gemela' }));
  if (!created.ok) fail('la gemela', created);
  const refused = await publish(created.rule_id, created.fingerprint);
  if (refused.ok || refused.reason_code !== 'keyword_rule_word_taken') fail('la misma palabra', refused);
  if ((await row(created.rule_id)).status !== 'borrador') fail('la gemela sigue borrador');
  await expectError(db, 'el indice unico', () => sql(`update public.keyword_rules set status = 'publicada',
    approved_at = now(), approved_by_kind = 'slack', approved_by_ref = $2 where id = $1`, [created.rule_id, DAN]),
  { code: '23505' });
  await page('approver_discard_keyword_rule_v1', created.rule_id, 'fin de la prueba');
}

// 4. Pausar, reanudar y descartar.
{
  const viva = await page('approver_discard_keyword_rule_v1', main.id, 'no va');
  if (viva.ok || viva.reason_code !== 'keyword_rule_live_pause_first') fail('una viva no se descarta', viva);
  const paused = await page('approver_pause_keyword_rule_v1', main.id, 'hasta revisar el link');
  if (!paused.ok) fail('pausar', paused);
  if ((await row(main.id)).paused_by_ref !== DAN) fail('quien pauso');
  if ((await service(() => rpc('get_live_keyword_rules_v1', INSTANCE))).rules.length !== 0) fail('pausada no se sirve');
  const resumed = await page('approver_resume_keyword_rule_v1', main.id);
  if (!resumed.ok || (await row(main.id)).status !== 'publicada') fail('reanudar', resumed);
  const notPaused = await page('approver_resume_keyword_rule_v1', main.id);
  if (!notPaused.ok || !notPaused.replayed) fail('reanudar una viva', notPaused);
  await page('approver_pause_keyword_rule_v1', main.id, 'para descartar');
  const discarded = await page('approver_discard_keyword_rule_v1', main.id, 'ya no va');
  if (!discarded.ok || (await row(main.id)).status !== 'descartada') fail('descartar una pausada', discarded);
  const twice = await page('approver_discard_keyword_rule_v1', main.id, 'ya no va');
  if (!twice.replayed) fail('descartar dos veces', twice);
  // Descartada, la palabra queda libre.
  const reborn = await save(RULE({ clave: 'soporte-2' }));
  if (!reborn.ok) {
    const [open] = await sql(`select id from public.keyword_rules where status = 'borrador' order by created_at limit 1`);
    await page('approver_discard_keyword_rule_v1', open.id, 'lugar');
  }
  const free = reborn.ok ? reborn : await save(RULE({ clave: 'soporte-2' }));
  const published = await publish(free.rule_id, free.fingerprint);
  if (!published.ok) fail('la palabra se libera al descartar', published);
  main = { id: free.rule_id, fingerprint: free.fingerprint };
}

// 3b. El tope de 20 vivas (con filas sembradas sin el disparador de forma).
{
  await db.exec('set session_replication_role = replica');
  for (let index = 1; index <= 19; index += 1) {
    await sql(`insert into public.keyword_rules (tenant_ref, ally_ref, status, revision, rule_key, keyword, label,
        label_macro_id, rule, purpose, fingerprint, requested_by_kind, requested_by_ref, approved_at,
        approved_by_kind, approved_by_ref)
      values ($1, $2, 'publicada', 1, $3, $4, 'x', 1, $5::jsonb, 'semilla', $6, 'automation_expert', $7, now(),
        'slack', $8)`, [INSTANCE.tenant_ref, INSTANCE.ally_ref, `viva-${index}`, `viva ${index}`,
      JSON.stringify(RULE({ clave: `viva-${index}`, palabra_clave: `viva ${index}`, palabra_normalizada: `viva ${index}`,
        etiqueta: 'x', macro_etiqueta: 1 })), sha(`viva-${index}`), AE, DAN]);
  }
  await db.exec('set session_replication_role = origin');
  const [open] = await sql(`select id from public.keyword_rules where status = 'borrador' order by created_at limit 1`);
  const fingerprint = (await row(open.id)).fingerprint;
  const full = await publish(open.id, fingerprint);
  if (full.ok || full.reason_code !== 'keyword_rule_live_limit') fail('el tope de vivas', full);
}

// 5. Los disparadores.
{
  await expectError(db, 'el contenido congelado', () => sql(
    `update public.keyword_rules set purpose = 'otra cosa' where id = $1`, [main.id]),
  { code: '55000', message: 'keyword_rule_frozen' });
  await expectError(db, 'un recorrido invalido', () => sql(
    `update public.keyword_rules set status = 'borrador' where id = $1`, [main.id]),
  { code: '23514', message: 'keyword_rule_transition_invalid' });
  await expectError(db, 'no se borra', () => sql('delete from public.keyword_rules where id = $1', [main.id]),
    { code: '55000', message: 'keyword_rule_is_permanent' });
  await expectError(db, 'los eventos, solo agregar', () => sql(
    `delete from public.keyword_rule_events where rule_id = $1`, [main.id]),
  { code: '55000', message: 'keyword_rule_event_is_append_only' });
  const shown = await agent(() => rpc('get_keyword_rule_v1', INSTANCE, main.id));
  const text = JSON.stringify(shown);
  if (text.includes(DAN) || text.includes(AE) || 'approved_by_ref' in shown) fail('el agente no ve IDs', shown);
  const forPage = await page('approver_get_keyword_rule_v1', main.id);
  if (forPage.approved_by_ref !== DAN) fail('la pagina ve quien aprobo', forPage);
}

await db.close();
console.log('KEYWORD_RULES_OK');
