// Los frenos ante Meta (migracion 20261127100000, meta-enforcement-brakes-v1, F1 del 2026-11-27), con las RPC
// reales sobre la cadena completa de migraciones. Contrato: docs/contracts/meta-enforcement-brakes-v1.md.
//
// Prueba, con un rojo por pieza:
//   0. la forma: agrega exactamente sus funciones y sus siete tablas, no cambia ninguna otra funcion; las
//      publicas las ejecuta solo service_role; tablas con RLS y sin permisos; correrla dos veces aborta (55000);
//   1. el sondeo: la primera lectura es linea de base; UTILITY -> MARKETING retira y avisa; MARKETING ->
//      UTILITY no hace nada; el aviso previo (correct_category) avisa sin retirar, una vez;
//   2. el webhook: el mismo cambio que ya vio el sondeo es repetido; un hecho nuevo retira y deja lo visto;
//   3. el contador: dos seguidas dejan el aviso «contador_dos» una vez; una solicitud UTILITY aprobada como
//      UTILITY lo reinicia; una aprobada como MARKETING cuenta (sin retirar); liberarlo a mano lo pone en 0;
//   4. la cuenta: ACCOUNT_RESTRICTION congela y avisa; repetido no duplica; UNBAN avisa sin descongelar;
//      descongelar y congelar a mano dejan quien y por que;
//   5. liberar una plantilla; los avisos se reclaman, se cierran y no se repiten;
//   6. los disparadores: nada se borra.
//
// Datos sinteticos: buildWorld (el manifiesto de ATT1), un WABA y IDs de Slack inventados.
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261127100000_meta_enforcement_brakes_v1.sql';

const SERVICE_FUNCTIONS = [
  'record_meta_template_categories_v1(jsonb,text,jsonb,timestampwithtimezone)',
  'record_meta_template_category_webhook_v1(jsonb,text,jsonb)',
  'record_meta_account_event_v1(jsonb,text,jsonb)',
  'get_meta_enforcement_state_v1(jsonb,text)',
  'act_on_meta_enforcement_v1(jsonb,text,text,jsonb,text,text)',
  'claim_meta_enforcement_notices_v1(jsonb,integer)',
  'finish_meta_enforcement_notice_v1(jsonb,uuid,text,text)',
];
const INTERNAL_FUNCTIONS = [
  'reject_meta_enforcement_mutation()',
  '_meta_enforcement_waba(text)',
  '_meta_enforcement_streak(text,text,text)',
  '_meta_enforcement_notice(text,text,text,jsonb)',
  '_meta_enforcement_category_change(text,text,text,text,text,text,text,text,text,text,text,'
    + 'timestampwithtimezone,timestampwithtimezone)',
  '_meta_enforcement_sweep_approvals(text,text,text)',
  '_meta_enforcement_state(text,text,text)',
];
const TABLES = ['meta_template_category_seen', 'meta_template_category_events', 'meta_template_retirements',
  'meta_account_events', 'meta_account_freezes', 'meta_recategorization_streak_releases', 'meta_enforcement_notices'];
const ALL_FUNCTIONS = [...SERVICE_FUNCTIONS, ...INTERNAL_FUNCTIONS];
const sig = (text) => text.replace(/^public\./, '').replace(/ /g, '');

const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [sig(row.signature), row]));
const tables = async (db) => (await db.query(`select c.relname from pg_class c
  where c.relnamespace = 'public'::regnamespace and c.relkind = 'r' order by 1`)).rows.map((row) => row.relname);

// 0. La forma.
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
  if (!sameSet(newTables, TABLES)) fail('las tablas', newTables);
  const roles = ['automation_expert_change', 'flow_change_approver', 'service_role', 'anon', 'authenticated'];
  for (const signature of ALL_FUNCTIONS) {
    const allowed = [];
    for (const role of roles) {
      const can = one((await shapeDb.query(`select has_function_privilege($1, p.oid, 'execute') as x
        from pg_proc p where p.pronamespace = 'public'::regnamespace
         and replace(regexp_replace(p.oid::regprocedure::text, '^public\\.', ''), ' ', '') = $2`,
      [role, signature])).rows, signature).x;
      if (can) allowed.push(role);
    }
    const expected = SERVICE_FUNCTIONS.includes(signature) ? ['service_role'] : [];
    if (!sameSet(allowed, expected)) fail(`los permisos de ${signature}`, allowed);
  }
  const definers = (await shapeDb.query(`select p.oid::regprocedure::text sig, prosecdef,
      coalesce(array_to_string(proconfig, ','), '') config
    from pg_proc p where p.pronamespace = 'public'::regnamespace and p.proname like '%meta_enforcement%'
       or p.proname in ('record_meta_template_categories_v1', 'record_meta_template_category_webhook_v1',
                        'record_meta_account_event_v1')`)).rows;
  for (const row of definers) {
    const signature = sig(row.sig);
    const publicOne = SERVICE_FUNCTIONS.includes(signature);
    if (row.prosecdef !== publicOne) fail(`security definer solo las publicas: ${signature}`, row);
    if (!row.config.includes('search_path=pg_catalog')) fail(`search_path fijo: ${signature}`, row);
  }
  for (const table of TABLES) {
    const row = one((await shapeDb.query(`select c.relrowsecurity,
        has_table_privilege('anon', c.oid, 'select') a, has_table_privilege('authenticated', c.oid, 'select') u,
        has_table_privilege('service_role', c.oid, 'select') s
      from pg_class c where c.oid = $1::regclass`, [`public.${table}`])).rows, table);
    if (!row.relrowsecurity || row.a || row.u || row.s) fail(`la tabla ${table}, sin permisos y con RLS`, row);
  }
  await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'meta_enforcement_already_applied' });
}
await shapeDb.close();

// ---------------------------------------------------------------------------
// El mundo: ATT1, con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, INSTANCE, rpc } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const WABA = '1000000000000001';
const DAN = 'U0DANSCHWAB1';
const as = async (role, run) => {
  await db.exec(`set role ${role}`);
  const out = await run();
  await db.exec('reset role');
  return out;
};
const service = (run) => as('service_role', run);
const now = async () => (await world.dbNow()).toISOString();
const tpl = (name, category, correct = null, id = '111') => ({ id, name, language: 'es_MX', category,
  correct_category: correct });
const poll = async (templates) => service(async () => rpc('record_meta_template_categories_v1', INSTANCE, WABA,
  templates, await now()));
const state = () => service(() => rpc('get_meta_enforcement_state_v1', INSTANCE, WABA));
const notices = async () => sql(`select kind, data, status from public.meta_enforcement_notices order by created_at`);
const act = (action, target = null, reason = 'Se vio el aviso en WhatsApp Manager') =>
  service(() => rpc('act_on_meta_enforcement_v1', INSTANCE, WABA, action, target, reason, DAN));
const later = async () => db.exec('select pg_sleep(0.01)');

// 1. El sondeo.
{
  const base = await poll([tpl('att1_bienvenida_02', 'UTILITY'), tpl('att1_carrito_01', 'MARKETING', null, '112'),
    tpl('att1_pago_01', 'UTILITY', null, '113')]);
  if (!base.ok || base.baseline !== true || base.state.retired.length !== 0 || base.state.streak !== 0) {
    fail('la primera lectura es linea de base', base);
  }
  if ((await notices()).length !== 0) fail('la linea de base no avisa', await notices());
  await expectError(db, 'waba invalido', () => service(async () => rpc('record_meta_template_categories_v1', INSTANCE,
    'x', [], await now())), { code: '22023', message: 'meta_enforcement_waba_invalid' });
  await expectError(db, 'otra instancia', () => service(async () => rpc('record_meta_template_categories_v1',
    { ...INSTANCE, ally_ref: 'nadie' }, WABA, [], await now())), { code: '22023' });
  await later();
  const moved = await poll([tpl('att1_bienvenida_02', 'MARKETING'), tpl('att1_carrito_01', 'UTILITY', null, '112'),
    tpl('att1_pago_01', 'UTILITY', 'MARKETING', '113'), { name: 'Mal' }]);
  if (moved.baseline || moved.counts.retirada !== 1 || moved.counts.ignorado !== 1 || moved.counts.invalida !== 1) {
    fail('el sondeo: una retirada, una ignorada (marketing a utilidad) y una invalida', moved);
  }
  if (!sameSet(moved.state.retired.map((r) => r.template_name), ['att1_bienvenida_02'])
      || moved.state.retired[0].to !== 'MARKETING' || moved.state.streak !== 1) fail('lo retirado', moved.state);
  const kinds = (await notices()).map((n) => n.kind);
  if (!sameSet(kinds, ['recategorizacion_hecha', 'recategorizacion_previa'])) fail('los avisos del sondeo', kinds);
  // El mismo aviso previo otra vez no avisa de nuevo.
  await poll([tpl('att1_pago_01', 'UTILITY', 'MARKETING', '113')]);
  if ((await notices()).length !== 2) fail('el aviso previo sale una vez', await notices());
}

// 2. El webhook.
{
  const same = await service(async () => rpc('record_meta_template_category_webhook_v1', INSTANCE, WABA, {
    meta_template_id: '111', name: 'att1_bienvenida_02', language: 'es_MX', kind: 'hecho',
    previous_category: 'UTILITY', new_category: 'MARKETING', occurred_at: await now() }));
  if (same.outcome !== 'repetido') fail('el webhook del mismo cambio es repetido', same);
  const fresh = await service(async () => rpc('record_meta_template_category_webhook_v1', INSTANCE, WABA, {
    meta_template_id: '113', name: 'att1_pago_01', language: 'es_MX', kind: 'hecho',
    previous_category: 'UTILITY', new_category: 'MARKETING', occurred_at: await now() }));
  if (fresh.outcome !== 'retirada' || fresh.state.retired.length !== 2 || fresh.state.streak !== 2) {
    fail('un hecho nuevo por webhook retira', fresh);
  }
  const seen = one(await sql(`select category from public.meta_template_category_seen
    where template_name = 'att1_pago_01'`), 'visto');
  if (seen.category !== 'MARKETING') fail('el webhook deja lo visto', seen);
  const again = await poll([tpl('att1_pago_01', 'MARKETING', null, '113')]);
  if (again.counts.retirada) fail('el sondeo siguiente no lo registra otra vez', again);
  await expectError(db, 'evento invalido', () => service(() => rpc('record_meta_template_category_webhook_v1',
    INSTANCE, WABA, { name: 'x_y', language: 'es_MX', kind: 'hecho', new_category: 'MARKETING' })),
  { code: '22023', message: 'meta_enforcement_event_invalid' });
}

// 3. El contador.
{
  const counted = (await notices()).filter((n) => n.kind === 'contador_dos');
  if (counted.length !== 1 || counted[0].data.streak !== 2) fail('contador_dos, una vez', counted);
  const insertRequest = async (name, requested, answered) => {
    await db.exec('set session_replication_role = replica');
    const draft = { nombre: name, idioma: 'es_MX', categoria: requested, cuerpo: 'Hola', variables: [], botones: [] };
    await sql(`insert into public.meta_template_requests (tenant_ref, ally_ref, status, revision, template_name,
        language, category_requested, draft, purpose, requested_by_kind, requested_by_ref, fingerprint, validation,
        validated_at, expires_at, approved_at, approved_by_kind, approved_by_ref, sent_at, send_started_at,
        send_attempts, meta_template_id, meta_status, meta_category, meta_answered_at)
      values ($1, $2, 'aprobada_meta', 1, $3, 'es_MX', $4, $5::jsonb, 'Para probar', 'automation_expert', $6,
        repeat('a', 64), '{}'::jsonb, now(), now() + interval '24 hours', now(), 'slack', $6, now(), now(), 1,
        $7, 'APPROVED', $8, now())`,
    [INSTANCE.tenant_ref, INSTANCE.ally_ref, name, requested, JSON.stringify(draft), DAN,
      String(Math.floor(Math.random() * 1e9)), answered]);
    await db.exec('set session_replication_role = origin');
  };
  await later();
  await insertRequest('att1_utilidad_ok', 'UTILITY', 'UTILITY');
  if ((await state()).streak !== 0) fail('una UTILITY aprobada como UTILITY reinicia', await state());
  await later();
  await insertRequest('att1_utilidad_mkt', 'UTILITY', 'MARKETING');
  const swept = await poll([]);
  if (swept.counts.aprobacion !== 1 || swept.state.streak !== 1 || swept.state.retired.length !== 2) {
    fail('la aprobada como marketing cuenta y no retira', swept);
  }
  if ((await poll([])).counts.aprobacion !== 0) fail('la de aprobar cuenta una vez', 0);
  const released = await act('liberar_contador');
  if (!released.ok || released.replayed || released.state.streak !== 0) fail('liberar el contador', released);
  if (!(await act('liberar_contador')).replayed) fail('liberar el contador en 0 es replayed', 0);
}

// 4. La cuenta.
{
  const restrict = async (violation, types = []) => service(async () => rpc('record_meta_account_event_v1',
    INSTANCE, WABA, { event: 'ACCOUNT_RESTRICTION', violation_type: violation, restriction_types: types,
      occurred_at: '2026-11-27T12:00:00Z' }));
  const frozen = await restrict('UTILITY_TEMPLATE_ABUSE', ['RESTRICTED_UTILITY_TEMPLATES']);
  if (frozen.action !== 'congela' || frozen.state.frozen?.source !== 'webhook'
      || frozen.state.frozen.violation_type !== 'UTILITY_TEMPLATE_ABUSE') fail('congela', frozen);
  const repeated = await restrict('UTILITY_TEMPLATE_ABUSE', ['RESTRICTED_UTILITY_TEMPLATES']);
  if (repeated.action !== 'repetido') fail('el mismo evento es repetido', repeated);
  const unban = await restrict('UTILITY_TEMPLATE_ABUSE_UNBAN');
  if (unban.action !== 'aviso' || unban.state.frozen === null) fail('el UNBAN avisa sin descongelar', unban);
  const kinds = (await notices()).map((n) => n.kind);
  if (kinds.filter((k) => k === 'congelamiento').length !== 1
      || kinds.filter((k) => k === 'restriccion_levantada_por_meta').length !== 1) fail('los avisos de la cuenta', kinds);
  await expectError(db, 'otro evento', () => service(() => rpc('record_meta_account_event_v1', INSTANCE, WABA,
    { event: 'PARTNER_ADDED', occurred_at: '2026-11-27T12:00:00Z' })),
  { code: '22023', message: 'meta_enforcement_event_invalid' });
  const lifted = await act('descongelar', null, 'Meta levanto la restriccion');
  if (!lifted.ok || lifted.replayed || lifted.state.frozen !== null) fail('descongelar', lifted);
  if (!(await act('descongelar')).replayed) fail('descongelar dos veces es replayed', 0);
  const manual = await act('congelar', null, 'Banner de restriccion en WhatsApp Manager');
  if (manual.state.frozen?.source !== 'manual' || manual.replayed) fail('congelar a mano', manual);
  if (!(await act('congelar')).replayed) fail('congelar dos veces es replayed', 0);
  const row = one(await sql(`select frozen_by_ref, reason from public.meta_account_freezes where status = 'congelada'`),
    'congelada');
  if (row.frozen_by_ref !== DAN) fail('quien congelo', row);
  await expectError(db, 'accion invalida', () => act('borrar'), { code: '22023', message: 'meta_enforcement_action_invalid' });
  await expectError(db, 'actor invalido', () => service(() => rpc('act_on_meta_enforcement_v1', INSTANCE, WABA,
    'congelar', null, 'x', 'dan')), { code: '22023', message: 'flow_store_actor_invalid' });
}

// 5. Liberar una plantilla y los avisos.
{
  const freed = await act('liberar_plantilla', { template_name: 'att1_bienvenida_02', language: 'es_MX' },
    'Meta aprobo la apelacion');
  if (freed.state.retired.some((r) => r.template_name === 'att1_bienvenida_02')) fail('liberar una plantilla', freed);
  await expectError(db, 'liberar una que no esta retirada', () => act('liberar_plantilla',
    { template_name: 'att1_bienvenida_02', language: 'es_MX' }),
  { code: 'P0002', message: 'meta_enforcement_template_not_retired' });
  const claimed = await service(() => rpc('claim_meta_enforcement_notices_v1', INSTANCE, 20));
  const total = (await notices()).length;
  if (claimed.notices.length !== total || claimed.counts.reclamado !== total) fail('reclamar todos', claimed);
  const [first, second, third, ...rest] = claimed.notices;
  const finish = (notice, outcome, code = null) => service(() => rpc('finish_meta_enforcement_notice_v1', INSTANCE,
    notice.notice_id, outcome, code));
  if ((await finish(first, 'sent')).status !== 'enviado') fail('sent', first);
  if ((await finish(second, 'retry', 'slack_rejected')).status !== 'pendiente') fail('retry', second);
  if ((await finish(third, 'waiting', 'slack_blocked:not_in_channel')).status !== 'esperando') fail('waiting', third);
  for (const notice of rest) await finish(notice, 'unknown', 'slack_delivery_unknown');
  const twice = await finish(first, 'sent');
  if (twice.ok !== false || twice.reason_code !== 'meta_enforcement_notice_not_claimed') fail('cerrar dos veces', twice);
  const next = await service(() => rpc('claim_meta_enforcement_notices_v1', INSTANCE, 20));
  if (!sameSet(next.notices.map((n) => n.notice_id), [second.notice_id, third.notice_id])) {
    fail('vuelven a salir solo el retry y el waiting', next);
  }
  if (next.notices.find((n) => n.notice_id === third.notice_id).attempts !== 1) fail('waiting no gasta intento', next);
}

// 6. Los disparadores.
for (const table of TABLES) {
  await expectError(db, `no se borra ${table}`, () => sql(`delete from public.${table}`),
    { code: '55000', message: 'meta_enforcement_is_permanent' });
}
await expectError(db, 'una retirada no vuelve', () => sql(`update public.meta_template_retirements
  set status = 'retirada', released_at = null, released_by_ref = null, release_reason = null
  where status = 'liberada'`), { code: '55000', message: 'meta_enforcement_is_permanent' });

console.log('meta enforcement brakes: ok');
