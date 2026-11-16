// Publicar con siembra, pausar, reanudar, cerrar y descartar el producto del Automation Expert, y una campana
// viva por producto (migracion 20261115000200, product-card-v1 §7, E-575, MP-08 del plan de multiproducto), con
// las RPC reales sobre la cadena completa de migraciones. Contrato: docs/contracts/product-card-v1.md.
//
// Prueba, con un rojo por pieza:
//   0. la forma: agrega exactamente sus funciones y cambia solo las dos de campanas que reescribe (con sus
//      mismos permisos); cada publica la ejecuta solo su rol; correrla dos veces aborta (55000);
//   1. publicar: sin sesion 28000, el agente no publica (42501), otra huella 22023, un borrador no; con el si
//      siembra el binding, el catalogo, los scopes de intencion y la politica (lo de
//      provision_instance_product.py) y resolve_commercial_ally_runtime_binding lo ve; repetir no siembra de
//      nuevo; la lectura en vivo; una validada vencida no se publica;
//   2. las colisiones adentro de la transaccion: la clave de un binding del manifiesto, el producto de Hotmart
//      del principal, una oferta de otro producto, un principal que no es de la instancia;
//   3. la version nueva cierra la anterior y siembra solo la diferencia (binding + 1, una oferta mas);
//   4. una campana viva por producto: la de un producto del AE convive con la de la instancia, una segunda del
//      mismo producto se rechaza, reanudar no termina la viva de otro lugar, y sin producto publicado no sale;
//   5. pausar y cerrar pausan primero los flujos y las campanas del producto; reanudar vuelve solo el producto;
//   6. descartar: el agente y la pagina, solo lo no publicado; taken ya no lo cuenta.
//
// El minimo es el de la ficha sintetica de Forever Young (tests/fixtures/agent_products/), como en
// validate_agent_products.mjs.
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { buildWorld, expectError, fail, one, root, sameSet } from './validate_flow_engine_motor_v1.mjs';

export const MIGRATION = '20261115000200_agent_products_publish_v1.sql';
const sha = (text) => createHash('sha256').update(text).digest('hex');

const APPROVER_FUNCTIONS = [
  'approver_get_agent_product_v1(jsonb,text,uuid)',
  'approver_publish_agent_product_v1(jsonb,text,uuid,text,text)',
  'approver_pause_agent_product_v1(jsonb,text,uuid,text)',
  'approver_resume_agent_product_v1(jsonb,text,uuid)',
  'approver_close_agent_product_v1(jsonb,text,uuid,text)',
  'approver_discard_agent_product_v1(jsonb,text,uuid,text)',
];
const AE_FUNCTIONS = ['discard_agent_product_v1(jsonb,uuid,text,text)'];
const SERVICE_FUNCTIONS = ['get_live_agent_products_v1(jsonb)'];
const INTERNAL_FUNCTIONS = [
  '_agent_campaign_live_scope(text,text,jsonb)',
  '_agent_product_default_offer(jsonb)',
  '_agent_product_additional_codes(jsonb)',
  '_agent_product_seed_conflict(agent_products,commercial_ally_runtime_bindings)',
  '_agent_product_seed(agent_products,commercial_ally_runtime_bindings,text)',
  '_agent_product_pause_dependents(agent_products,text,text,text)',
  '_agent_product_discard(text,text,uuid,text,text,text)',
];
const NEW_FUNCTIONS = [...APPROVER_FUNCTIONS, ...AE_FUNCTIONS, ...SERVICE_FUNCTIONS, ...INTERNAL_FUNCTIONS];
const REWRITTEN = [
  'approver_publish_agent_campaign_v1(jsonb,text,uuid,text,text)',
  'approver_resume_agent_campaign_v1(jsonb,text,uuid)',
];

const snapshot = async (db) => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition,
         coalesce(p.proacl::text, '') as acl
  from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f'
`)).rows.map((row) => [row.signature.replace(/^public\./, '').replace(/ /g, ''), row]));

// ---------------------------------------------------------------------------
// 0. La forma.
// ---------------------------------------------------------------------------
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
const target = join(root, 'supabase/migrations', MIGRATION);
await applyFile(shapeDb, target);
const after = await snapshot(shapeDb);
{
  const added = [...after.keys()].filter((signature) => !before.has(signature));
  const removed = [...before.keys()].filter((signature) => !after.has(signature));
  const changed = [...before.keys()].filter((signature) => after.has(signature)
    && after.get(signature).definition !== before.get(signature).definition);
  const aclChanged = [...before.keys()].filter((signature) => after.has(signature)
    && after.get(signature).acl !== before.get(signature).acl);
  if (!sameSet(added, NEW_FUNCTIONS) || removed.length !== 0 || !sameSet(changed, REWRITTEN) || aclChanged.length !== 0) {
    fail('la migracion cambio otra cosa', { added, removed, changed, aclChanged });
  }
  const can = async (role, signature) => one((await shapeDb.query(
    `select has_function_privilege($1, to_regprocedure('public.${signature}'), 'execute') as x`, [role])).rows,
  role).x;
  const roles = ['automation_expert_change', 'flow_change_approver', 'service_role', 'anon', 'authenticated'];
  for (const signature of [...NEW_FUNCTIONS, ...REWRITTEN]) {
    const allowed = [];
    for (const role of roles) if (await can(role, signature)) allowed.push(role);
    const expected = APPROVER_FUNCTIONS.includes(signature) || REWRITTEN.includes(signature) ? ['flow_change_approver']
      : AE_FUNCTIONS.includes(signature) ? ['automation_expert_change']
        : SERVICE_FUNCTIONS.includes(signature) ? ['service_role'] : [];
    if (!sameSet(allowed, expected)) fail(`los permisos de ${signature}`, allowed);
  }
  const definers = (await shapeDb.query(`select p.oid::regprocedure::text sig, prosecdef,
      coalesce(array_to_string(proconfig, ','), '') config
    from pg_proc p where p.pronamespace = 'public'::regnamespace`)).rows
    .map((row) => ({ ...row, sig: row.sig.replace(/^public\./, '').replace(/ /g, '') }))
    .filter((row) => NEW_FUNCTIONS.includes(row.sig));
  if (definers.length !== NEW_FUNCTIONS.length) fail('las funciones nuevas', definers.map((row) => row.sig));
  for (const row of definers) {
    if (row.prosecdef !== !INTERNAL_FUNCTIONS.includes(row.sig)) fail(`security definer solo las publicas: ${row.sig}`, row);
    if (!row.config.includes('search_path=pg_catalog')) fail(`search_path fijo: ${row.sig}`, row);
  }
  const index = one((await shapeDb.query(`select indexdef from pg_indexes
    where schemaname = 'public' and indexname = 'agent_campaigns_one_live'`)).rows, 'el indice').indexdef;
  if (!index.includes('live_scope') || !index.includes("'publicada'")) fail('una viva por producto', index);
  await expectError(shapeDb, 'correr la migracion dos veces', () => applyFile(shapeDb, target),
    { code: '55000', message: 'agent_products_publish_already_applied' });
}
await shapeDb.close();

// ---------------------------------------------------------------------------
// El mundo: ATT1 (A, el principal att1) y el producto sintetico B (att9), con la cadena entera.
// ---------------------------------------------------------------------------
const world = await buildWorld();
const { db, INSTANCE, rpc } = world;
const sql = async (text, params = []) => (await db.query(text, params)).rows;
const AE = 'U0123456789';
const DAN = 'U0DANSCHWAB1';
const TEAM = 'T0LANCEMOS01';
const PRINCIPAL = INSTANCE.funnel_ref;
const later = async () => db.exec('select pg_sleep(0.01)');
const as = async (role, run) => {
  await later();
  // Sin finally: dentro de expectError el rollback deshace el set role.
  await db.exec(`set role ${role}`);
  const out = await run();
  await db.exec('reset role');
  return out;
};
const agent = (run) => as('automation_expert_change', run);
const approver = (run) => as('flow_change_approver', run);
const service = (run) => as('service_role', run);
const inMinutes = async (minutes) => new Date((await world.dbNow()).getTime() + minutes * 60_000).toISOString();
const shift = async (text, params) => {
  await db.exec('set session_replication_role = replica');
  await sql(text, params);
  await db.exec('set session_replication_role = origin');
};
const product = async (id) => one(await sql('select * from public.agent_products where id = $1', [id]), 'producto');
const count = async (text, params) => Number(one(await sql(text, params), text).n);

const FY = JSON.parse(readFileSync(join(root, 'tests/fixtures/agent_products/forever_young_card_sintetica_20261114.json'),
  'utf8')).card;
const { texto_libre: FREE_TEXT, ...FY_MINIMUM } = FY;
const MINIMUM = (overrides = {}) => ({ ...structuredClone(FY_MINIMUM), ...overrides });
const keyed = (clave, productId, offer, hotlink = 'X000PRUEBA') => MINIMUM({
  clave, nombre: `Producto ${clave}`, alias: [`alias ${clave}`],
  hotmart: { producto_id: productId, hotlink },
  ofertas: [{ codigo: offer, precio: '99', moneda: 'USD', por_defecto: true, otros_montos: [] }],
});
const VALIDATOR = { contract: 'product-card-v1', rules_version: 1, product_version: 'v1.39.0', git_sha: '57e9792' };
const CLEAN = { blocks: [], warnings: [] };
const save = (minimum) => agent(() => rpc('save_agent_product_draft_v1', INSTANCE, null, null, minimum, FREE_TEXT, [],
  'Sumar el producto', AE));
const validated = async (minimum) => {
  const saved = await save(minimum);
  if (!saved.ok) fail(`guardar ${minimum.clave}`, saved);
  const recorded = await agent(() => rpc('record_agent_product_validation_v1', INSTANCE, saved.product_id, saved.revision,
    VALIDATOR, CLEAN, { render: '# Producto' }, AE));
  if (recorded.status !== 'validada') fail(`validar ${minimum.clave}`, recorded);
  return { id: saved.product_id, fingerprint: recorded.fingerprint, version: saved.product_version };
};

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
const publish = (id, fingerprint, principal = PRINCIPAL) => page('approver_publish_agent_product_v1', id, fingerprint,
  principal);
const discardAll = async () => {
  for (const open of await sql(`select id from public.agent_products where status in ('borrador', 'validada')`)) {
    const out = await agent(() => rpc('discard_agent_product_v1', INSTANCE, open.id, 'limpieza de la prueba', AE));
    if (!out.ok) fail('descartar para seguir', out);
  }
};
const seeded = async (funnel) => ({
  bindings: await sql(`select * from public.commercial_ally_runtime_bindings where funnel_ref = $1
    order by binding_version`, [funnel]),
  catalog: await sql(`select * from public.checkout_offer_catalog where funnel_ref = $1 order by offer_code, version`,
    [funnel]),
  scopes: await sql(`select * from public.hotmart_purchase_intent_scopes where funnel_ref = $1 order by offer_ref,
    created_at`, [funnel]),
  policies: await sql(`select * from public.commercial_ally_hotmart_purchase_policies where funnel_ref = $1
    order by binding_version`, [funnel]),
});
const principalBinding = one(await sql(`select * from public.commercial_ally_runtime_bindings
  where funnel_ref = $1 and status = 'active'`, [PRINCIPAL]), 'el principal');

// 1. Publicar.
let fy;
{
  fy = await validated(MINIMUM());
  await expectError(db, 'sin sesion', () => approver(() => rpc('approver_publish_agent_product_v1', INSTANCE,
    sha('sesion-falsa'), fy.id, fy.fingerprint, PRINCIPAL)), { code: '28000' });
  await expectError(db, 'el agente no publica', () => agent(() => rpc('approver_publish_agent_product_v1', INSTANCE, S,
    fy.id, fy.fingerprint, PRINCIPAL)), { code: '42501' });
  await expectError(db, 'otra huella', () => publish(fy.id, 'e'.repeat(64)),
    { code: '22023', message: 'agent_product_fingerprint_mismatch' });
  await expectError(db, 'un principal que no es de la instancia', () => publish(fy.id, fy.fingerprint, 'otra-cosa'),
    { code: '22023', message: 'agent_product_principal_not_bound' });
  const draft = await save(keyed('borrador-suelto', 9200001, 'oferbs01'));
  const notValidated = await publish(draft.product_id, 'e'.repeat(64));
  if (notValidated.ok || notValidated.reason_code !== 'agent_product_not_validated') fail('un borrador', notValidated);
  if ((await seeded('forever-young')).bindings.length !== 0) fail('antes del si no hay nada sembrado');

  const published = await publish(fy.id, fy.fingerprint);
  if (!published.ok || published.status !== 'publicada' || published.replayed
      || published.seed.binding !== 'creado' || published.seed.binding_version !== 1
      || published.seed.catalog.inserted !== 1 || published.seed.intent_scopes.inserted !== 1) fail('publicar', published);
  const stored = await product(fy.id);
  if (stored.status !== 'publicada' || stored.approved_by_ref !== DAN || stored.approved_by_kind !== 'slack') {
    fail('quien aprobo', stored);
  }
  const seed = await seeded('forever-young');
  const binding = one(seed.bindings, 'el binding');
  const offer = FY_MINIMUM.ofertas[0];
  if (binding.status !== 'active' || binding.ally_ref !== INSTANCE.ally_ref
      || Number(binding.hotmart_product_id) !== FY_MINIMUM.hotmart.producto_id
      || binding.product_hotlink !== FY_MINIMUM.hotmart.hotlink || binding.product_name !== FY_MINIMUM.nombre
      || Number(binding.product_price) !== Number(offer.precio) || binding.currency !== offer.moneda
      || binding.offer_code !== offer.codigo || binding.additional_offer_codes.length !== 0
      || binding.lead_landing_id !== 'forever-young' || binding.lead_page_path !== '/forever-young'
      || binding.lead_site !== principalBinding.lead_site
      || Number(binding.chatwoot_inbox_id) !== Number(principalBinding.chatwoot_inbox_id)
      || binding.inbound_scope_key !== principalBinding.inbound_scope_key
      || binding.consent_copy_version !== principalBinding.consent_copy_version) fail('el binding sembrado', binding);
  const row = one(seed.catalog, 'el catalogo');
  if (row.status !== 'active' || !row.default_for_inbound || row.offer_code !== offer.codigo
      || row.checkout_base_url !== `https://pay.hotmart.com/${FY_MINIMUM.hotmart.hotlink}`
      || row.scope_key !== principalBinding.inbound_scope_key || row.approved_by !== DAN) fail('el catalogo', row);
  const scope = one(seed.scopes, 'el scope');
  if (!scope.active || scope.hotmart_product_id !== String(FY_MINIMUM.hotmart.producto_id)
      || scope.offer_ref !== offer.codigo) fail('el scope de intencion', scope);
  const policy = one(seed.policies, 'la politica');
  if (!policy.enabled || policy.binding_version !== 1) fail('la politica de compra', policy);
  if ((await sql(`select * from public.resolve_commercial_ally_runtime_binding($1, 'forever-young', 1)`,
    [INSTANCE.tenant_ref])).length !== 1) fail('resolve no ve el binding');

  // Repetir no siembra de nuevo.
  const again = await publish(fy.id, fy.fingerprint);
  if (!again.ok || !again.replayed) fail('repetir', again);
  const reseed = await seeded('forever-young');
  if (reseed.bindings.length !== 1 || reseed.catalog.length !== 1 || reseed.scopes.length !== 1) fail('repetir siembra', reseed);

  // La lectura en vivo.
  const live = await service(() => rpc('get_live_agent_products_v1', INSTANCE));
  const fyLive = one(live.products, 'el vivo');
  if (fyLive.product_id !== fy.id || fyLive.status !== 'publicada' || fyLive.binding_version !== 1
      || fyLive.funnel_ref !== 'forever-young' || fyLive.texto_libre !== FREE_TEXT || JSON.stringify(live).includes(DAN)
      || JSON.stringify(live).includes(AE)) fail('la lectura en vivo', live);
  await expectError(db, 'el agente no lee lo vivo', () => agent(() => rpc('get_live_agent_products_v1', INSTANCE)),
    { code: '42501' });
  const shown = await page('approver_get_agent_product_v1', fy.id);
  if (shown.status !== 'publicada' || JSON.stringify(shown).includes(DAN)) fail('la pagina relee', shown);

  // Una validada vencida no se publica.
  const old = await validated(keyed('vieja', 9200002, 'ofervj01'));
  await shift(`update public.agent_products set validated_at = now() - interval '2 days',
    expires_at = now() - interval '1 day' where id = $1`, [old.id]);
  const expired = await publish(old.id, old.fingerprint);
  if (expired.ok || expired.reason_code !== 'agent_product_expired' || (await product(old.id)).status !== 'vencida') {
    fail('vencida', expired);
  }
  await discardAll();
}

// 2. Las colisiones, adentro de la transaccion (D11 de E-569).
{
  const principalOffer = principalBinding.offer_code;
  for (const [label, minimum, reason] of [
    ['la clave de un binding del manifiesto', keyed('att9', 9200010, 'ofercl01'), 'agent_product_key_taken'],
    ['el producto de Hotmart del principal', keyed('choca-id', Number(principalBinding.hotmart_product_id), 'ofercl02'),
      'agent_product_hotmart_product_taken'],
    ['la oferta de otro producto', keyed('choca-oferta', 9200012, 'offb9001'), 'agent_product_offer_code_taken'],
    ['la oferta del principal', keyed('choca-oferta-a', 9200013, principalOffer), 'agent_product_offer_code_taken'],
    ['el hotlink de otro producto', keyed('choca-hotlink', 9200014, 'ofercl04', principalBinding.product_hotlink),
      'agent_product_hotlink_taken'],
    ['el producto de Hotmart de otro producto del AE', keyed('choca-ae', FY_MINIMUM.hotmart.producto_id, 'ofercl05'),
      'agent_product_hotmart_product_taken'],
  ]) {
    const v = await validated(minimum);
    const refused = await publish(v.id, v.fingerprint);
    if (refused.ok || refused.reason_code !== reason) fail(label, refused);
    if ((await product(v.id)).status !== 'validada') fail(`${label}: sigue validada`);
    if ((await seeded(minimum.clave)).catalog.length !== 0 && minimum.clave !== 'att9') fail(`${label}: no siembra`);
    const events = await sql(`select event_type, reason_code from public.agent_product_events where product_id = $1
      order by created_at desc limit 1`, [v.id]);
    if (events[0]?.event_type !== 'agent_product_publish_refused' || events[0]?.reason_code !== reason) {
      fail(`${label}: el evento`, events);
    }
    await discardAll();
  }
  // Un producto del AE no hace de principal.
  const asPrincipal = await validated(keyed('como-principal', 9200015, 'ofercl06'));
  await expectError(db, 'el principal no es un producto del AE', () => publish(asPrincipal.id, asPrincipal.fingerprint,
    'forever-young'), { code: '22023', message: 'agent_product_principal_not_bound' });
  // Lo que se publica es exactamente lo validado: la huella se recalcula.
  await shift(`update public.agent_products set minimum = jsonb_set(minimum, '{nombre}', '"Otro nombre"')
    where id = $1`, [asPrincipal.id]);
  await expectError(db, 'el contenido cambio por abajo', () => publish(asPrincipal.id, asPrincipal.fingerprint),
    { code: '22023', message: 'agent_product_fingerprint_mismatch' });
  await discardAll();
}

// 3. La version nueva: cierra la anterior y siembra solo la diferencia.
let fy2;
{
  const second = { codigo: 'fyprueba2', precio: '149', moneda: 'USD', por_defecto: false, otros_montos: [] };
  fy2 = await validated(MINIMUM({ ofertas: [...structuredClone(FY_MINIMUM.ofertas), second] }));
  if (fy2.version !== 2) fail('la version 2', fy2);
  const published = await publish(fy2.id, fy2.fingerprint);
  if (!published.ok || published.seed.binding !== 'nuevo' || published.seed.binding_version !== 2
      || published.seed.catalog.kept !== 1 || published.seed.catalog.inserted !== 1
      || published.seed.catalog.retired !== 0 || published.seed.intent_scopes.inserted !== 1) fail('la version 2', published);
  const v1 = await product(fy.id);
  if (v1.status !== 'cerrada' || v1.closed_by_kind !== 'sistema' || v1.closed_by_ref !== 'nueva_version') {
    fail('la version 1 queda cerrada', v1);
  }
  const seed = await seeded('forever-young');
  if (!sameSet(seed.bindings.map((b) => `${b.binding_version}:${b.status}`), ['1:retired', '2:active'])) {
    fail('el binding nuevo', seed.bindings);
  }
  const active = seed.bindings.find((b) => b.status === 'active');
  if (JSON.stringify(active.additional_offer_codes) !== JSON.stringify(['fyprueba2'])) fail('la oferta adicional', active);
  if (seed.catalog.filter((c) => c.status === 'active').length !== 2 || seed.scopes.filter((s) => s.active).length !== 2
      || seed.policies.length !== 2) fail('la siembra de la version 2', seed);
  const live = await service(() => rpc('get_live_agent_products_v1', INSTANCE));
  if (one(live.products, 'una viva por clave').product_id !== fy2.id) fail('solo la version 2 en vivo', live);
  // El binding viejo queda retirado, como un bump de binding_version en el manifiesto.
  if ((await sql(`select * from public.resolve_commercial_ally_runtime_binding($1, 'forever-young', 1)`,
    [INSTANCE.tenant_ref])).length !== 0) fail('el binding 1 queda retirado');

  // Una version 3 con el mismo minimo, publicada con la 2 pausada: la 2 se cierra y la siembra queda igual.
  const pausedV2 = await page('approver_pause_agent_product_v1', fy2.id, 'cambio de texto');
  if (!pausedV2.ok) fail('pausar la 2', pausedV2);
  const v2minimum = (await product(fy2.id)).minimum;
  const v3saved = await agent(() => rpc('save_agent_product_draft_v1', INSTANCE, null, null,
    v2minimum, `${FREE_TEXT}\n\nOtra linea.`, [], 'Otro texto', AE));
  const v3recorded = await agent(() => rpc('record_agent_product_validation_v1', INSTANCE, v3saved.product_id,
    v3saved.revision, VALIDATOR, CLEAN, { render: '# Producto' }, AE));
  await expectError(db, 'una pausada y una publicada de la misma clave', () => shift(`update public.agent_products
    set status = 'publicada', approved_at = now(), approved_by_kind = 'slack', approved_by_ref = $2 where id = $1`,
  [v3saved.product_id, DAN]), { code: '23505' });
  const v3 = await publish(v3saved.product_id, v3recorded.fingerprint);
  if (!v3.ok || v3.seed.binding !== 'igual' || v3.seed.binding_version !== 2 || v3.seed.catalog.kept !== 2
      || v3.seed.catalog.inserted !== 0 || v3.seed.intent_scopes.inserted !== 0) fail('la version 3, igual', v3);
  const closedV2 = await product(fy2.id);
  if (closedV2.status !== 'cerrada' || closedV2.closed_by_ref !== 'nueva_version') fail('la 2 pausada se cierra', closedV2);
  if ((await seeded('forever-young')).bindings.length !== 2) fail('sin binding nuevo');
  fy2 = { id: v3saved.product_id, fingerprint: v3recorded.fingerprint, version: 3 };
}

// 4. Una campana viva por producto (D6).
const CHECK = 'c'.repeat(64);
const campaign = async (key, producto) => {
  const draft = { clave: key, nombre: key, etiqueta: key, macro_etiqueta: 77, desde: '2026-11-20T00:00:00-06:00',
    hasta: '2099-12-01T00:00:00-06:00', asesoramiento: ['Texto.'], palabras_clave: [key] };
  if (producto) draft.oferta = { producto };
  const saved = await agent(() => rpc('save_agent_campaign_draft_v1', INSTANCE, null, null, draft, key, AE));
  const recorded = await agent(() => rpc('record_agent_campaign_validation_v1', INSTANCE, saved.campaign_id,
    saved.revision, { contract: 'agent-campaigns-v1', rules_version: 1, product_version: 'v1.39.0', git_sha: 'abcdef0' },
    CLEAN, {}, AE));
  const checked = await service(() => rpc('record_agent_campaign_check_v1', INSTANCE, saved.campaign_id,
    recorded.fingerprint, { veredicto: 'limpio', abiertos: 0, items: [] }, CHECK));
  if (!saved.ok || recorded.status !== 'validada' || !checked.ok) fail(`campana ${key}`, { saved, recorded, checked });
  return { id: saved.campaign_id, fingerprint: recorded.fingerprint };
};
const publishCampaign = (c) => page('approver_publish_agent_campaign_v1', c.id, c.fingerprint, CHECK);
const campaignRow = async (id) => one(await sql('select * from public.agent_campaigns where id = $1', [id]), 'campana');
let instanceCampaign;
let fyCampaign;
{
  instanceCampaign = await campaign('black-instancia', null);
  const first = await publishCampaign(instanceCampaign);
  if (!first.ok || (await campaignRow(instanceCampaign.id)).live_scope !== '') fail('la de la instancia', first);
  fyCampaign = await campaign('black-forever', 'forever-young');
  const second = await publishCampaign(fyCampaign);
  if (!second.ok || second.status !== 'publicada') fail('la de un producto del AE convive', second);
  if ((await campaignRow(fyCampaign.id)).live_scope !== 'forever-young') fail('su lugar', await campaignRow(fyCampaign.id));
  const twin = await campaign('otra-forever', 'forever-young');
  const refused = await publishCampaign(twin);
  if (refused.ok || refused.reason_code !== 'agent_campaign_live_exists') fail('dos del mismo producto', refused);
  const other = await campaign('otra-instancia', null);
  const refusedInstance = await publishCampaign(other);
  if (refusedInstance.ok || refusedInstance.reason_code !== 'agent_campaign_live_exists') {
    fail('dos de la instancia, como siempre', refusedInstance);
  }
  // Un producto de [[productos]] (att9) no es del AE: comparte el lugar de la instancia, como hoy.
  const manifestProduct = await campaign('black-att9', 'att9');
  const refusedManifest = await publishCampaign(manifestProduct);
  if (refusedManifest.ok || refusedManifest.reason_code !== 'agent_campaign_live_exists') {
    fail('el de [[productos]] sigue en el lugar de la instancia', refusedManifest);
  }
  await expectError(db, 'el indice: dos del mismo producto', () => sql(`update public.agent_campaigns
    set status = 'publicada', approved_at = now(), approved_by_kind = 'slack', approved_by_ref = $2,
        live_scope = 'forever-young' where id = $1`, [twin.id, DAN]), { code: '23505' });
  // Reanudar no termina la viva de otro lugar.
  await page('approver_pause_agent_campaign_v1', fyCampaign.id, 'prueba de reanudar');
  const resumed = await page('approver_resume_agent_campaign_v1', fyCampaign.id);
  if (!resumed.ok || resumed.status !== 'publicada') fail('reanudar con la de la instancia viva', resumed);
  if ((await campaignRow(instanceCampaign.id)).status !== 'publicada') fail('la de la instancia sigue viva');
  // Sin el producto publicado, su campana no sale.
  const noProduct = await validated(keyed('sin-publicar', 9200030, 'ofersp01'));
  const orphan = await campaign('black-sin-publicar', 'sin-publicar');
  const refusedOrphan = await publishCampaign(orphan);
  if (refusedOrphan.ok || refusedOrphan.reason_code !== 'agent_campaign_product_not_live') fail('sin producto', refusedOrphan);
  await agent(() => rpc('discard_agent_product_v1', INSTANCE, noProduct.id, 'limpieza de la prueba', AE));
}

// 5. Pausar y cerrar: primero los flujos y las campanas del producto.
{
  const registered = await rpc('operator_register_flow_v1', INSTANCE, 'forever-young', 'carrito', 'dan', 'alta del flujo');
  if (!registered.ok) fail('registrar el flujo del producto', registered);
  await shift(`update public.flow_controls set state = 'prendido', active_version = 1, generation = 1
    where flow_id = $1`, [registered.flow_id]);
  await expectError(db, 'pausar sin motivo', () => page('approver_pause_agent_product_v1', fy2.id, ''),
    { code: '22023', message: 'flow_store_text_invalid' });
  const paused = await page('approver_pause_agent_product_v1', fy2.id, 'hasta revisar el precio');
  if (!paused.ok || paused.status !== 'pausada' || paused.flows_paused !== 1 || paused.campaigns_paused !== 1) {
    fail('pausar', paused);
  }
  const flow = one(await sql('select * from public.flow_controls where flow_id = $1', [registered.flow_id]), 'flujo');
  if (flow.state !== 'pausado' || Number(flow.generation) !== 2 || flow.changed_by_ref !== DAN) fail('el flujo pausado', flow);
  if ((await campaignRow(fyCampaign.id)).status !== 'pausada') fail('la campana del producto pausada');
  if ((await campaignRow(instanceCampaign.id)).status !== 'publicada') fail('la de la instancia no se toca');
  if ((await product(fy2.id)).paused_by_ref !== DAN) fail('quien pauso');
  const twice = await page('approver_pause_agent_product_v1', fy2.id, 'otra vez');
  if (!twice.ok || !twice.replayed) fail('pausar dos veces', twice);
  const live = await service(() => rpc('get_live_agent_products_v1', INSTANCE));
  if (one(live.products, 'pausado').status !== 'pausada') fail('lo vivo dice pausada', live);
  const blockedCampaign = await page('approver_resume_agent_campaign_v1', fyCampaign.id);
  if (blockedCampaign.ok || blockedCampaign.reason_code !== 'agent_campaign_product_not_live') {
    fail('la campana no vuelve con el producto pausado', blockedCampaign);
  }

  const resumed = await page('approver_resume_agent_product_v1', fy2.id);
  if (!resumed.ok || resumed.status !== 'publicada') fail('reanudar', resumed);
  const after = one(await sql('select state from public.flow_controls where flow_id = $1', [registered.flow_id]), 'flujo');
  if (after.state !== 'pausado' || (await campaignRow(fyCampaign.id)).status !== 'pausada') {
    fail('reanudar vuelve solo el producto', after);
  }

  // Cerrar, con la campana otra vez publicada y el flujo prendido.
  const back = await page('approver_resume_agent_campaign_v1', fyCampaign.id);
  if (!back.ok) fail('la campana vuelve', back);
  await shift(`update public.flow_controls set state = 'prendido', generation = generation + 1 where flow_id = $1`,
    [registered.flow_id]);
  const closed = await page('approver_close_agent_product_v1', fy2.id, 'termino el lanzamiento');
  if (!closed.ok || closed.status !== 'cerrada' || closed.flows_paused !== 1 || closed.campaigns_paused !== 1) {
    fail('cerrar', closed);
  }
  const closedRow = await product(fy2.id);
  if (closedRow.closed_by_kind !== 'slack' || closedRow.closed_by_ref !== DAN) fail('quien cerro', closedRow);
  const again = await page('approver_close_agent_product_v1', fy2.id, 'otra vez');
  if (!again.ok || !again.replayed) fail('cerrar dos veces', again);
  const notLive = await page('approver_pause_agent_product_v1', fy2.id, 'tarde');
  if (notLive.ok || notLive.reason_code !== 'agent_product_not_live') fail('pausar una cerrada', notLive);
  if ((await service(() => rpc('get_live_agent_products_v1', INSTANCE))).products.length !== 0) fail('nada vivo');
  // La siembra queda: los casos en vuelo siguen resolviendo su binding.
  if ((await sql(`select * from public.resolve_commercial_ally_runtime_binding($1, 'forever-young', 2)`,
    [INSTANCE.tenant_ref])).length !== 1) fail('cerrar no retira el binding');
}

// 6. Descartar.
{
  const draft = await save(keyed('para-descartar', 9200040, 'oferpd01'));
  await expectError(db, 'descartar sin motivo', () => agent(() => rpc('discard_agent_product_v1', INSTANCE,
    draft.product_id, '', AE)), { code: '22023', message: 'flow_store_text_invalid' });
  await expectError(db, 'descartar con un actor invalido', () => agent(() => rpc('discard_agent_product_v1', INSTANCE,
    draft.product_id, 'me equivoque', 'nadie')), { code: '22023', message: 'flow_store_actor_invalid' });
  const discarded = await agent(() => rpc('discard_agent_product_v1', INSTANCE, draft.product_id, 'me equivoque', AE));
  if (!discarded.ok || discarded.replayed) fail('el agente descarta', discarded);
  const row = await product(draft.product_id);
  if (row.status !== 'descartada' || row.closed_by_kind !== 'automation_expert' || row.closed_by_ref !== AE) {
    fail('descartada por el agente', row);
  }
  const twice = await agent(() => rpc('discard_agent_product_v1', INSTANCE, draft.product_id, 'me equivoque', AE));
  if (!twice.ok || !twice.replayed) fail('descartar dos veces', twice);
  const final = await agent(() => rpc('discard_agent_product_v1', INSTANCE, fy2.id, 'no', AE));
  if (final.ok || final.reason_code !== 'agent_product_final') fail('no se descarta lo publicado', final);
  const v = await validated(keyed('validada-a-descartar', 9200041, 'oferpd02'));
  const byPage = await page('approver_discard_agent_product_v1', v.id, 'no va');
  if (!byPage.ok || (await product(v.id)).closed_by_kind !== 'slack') fail('la pagina descarta', byPage);
  const listed = await agent(() => rpc('list_agent_products_v1', INSTANCE, false, 50));
  const taken = listed.taken.map((t) => t.product_key);
  if (taken.includes('para-descartar') || taken.includes('validada-a-descartar') || !taken.includes('forever-young')) {
    fail('taken sin los descartados', taken);
  }
  await expectError(db, 'el agente descarta solo lo suyo de esta instancia', () => agent(() => rpc(
    'discard_agent_product_v1', { ...INSTANCE, ally_ref: 'otra-aliada' }, v.id, 'no', AE)),
  { code: '22023', message: 'flow_store_instance_not_bound' });
  await expectError(db, 'automation_expert solo en un descartado', () => shift(`update public.agent_products
    set closed_by_kind = 'automation_expert', closed_by_ref = $2 where id = $1`, [fy2.id, AE]), { code: '23514' });
}

await db.close();
console.log('AGENT_PRODUCTS_PUBLISH_OK');
