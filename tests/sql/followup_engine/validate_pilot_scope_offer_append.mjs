// 20261028000300 y scripts/add_instance_offers.py: sumar una oferta a una
// instancia que ya corre, sin version nueva de nada (E-163).
//
// Ejecuta las RPC reales sobre la cadena completa de migraciones, con un estado
// de la forma de Maribel el 2026-10-28: el binding v2 activo con una sola
// oferta (u8ikyx78), su scope de intencion, el catalogo del scope entrante v2,
// y tres scopes del piloto del producto (maribel-recuperacion v4 armado en
// manual_cohort, maribel-primer-contacto v4 de fuente landing y
// maribel-pago-rechazado v1), con sus controles. El SQL de escritura es el del
// script (SQL_APPLY, impreso por python3 desde scripts/add_instance_offers.py),
// con el pedido que arma el script para el manifiesto de Maribel con la oferta
// mmliigg5 de org-a. Lo que prueba, en orden:
//
// 1. el trigger: sin el pedido de la transaccion, para otra fila, cambiando
//    otro campo, sacando o reordenando ofertas, repitiendo una, o borrando, un
//    scope publicado sigue inmutable; solo agregar al final, pedido para esa
//    fila, pasa;
// 2. antes: un carrito de u8ikyx78 planificado en la v4 y todavia sin salir
//    (en vuelo), un carrito de u8ikyx78 guardado con el binding v2 sin
//    planificar, y un carrito de mmliigg5 rechazado por el binding;
// 3. en seco (rollback): no cambia ninguna tabla;
// 4. aplicado: la misma fila del binding con la oferta y su landing, el scope de
//    intencion nuevo, la fila del catalogo, y la oferta al final de los tres
//    scopes del piloto, cada uno en su version; ningun control cambia de
//    version, estado ni generacion;
// 5. despues: el caso en vuelo sale (claim, reevaluacion, reserva, arranque y
//    aceptacion reales), el evento guardado con el binding v2 se planifica, y un
//    carrito nuevo de mmliigg5 entra y se planifica en la v4;
// 6. correrlo otra vez no cambia nada.
import { PGlite } from '@electric-sql/pglite';
import { spawnSync } from 'node:child_process';
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

const fail = (label, detail) => {
  throw new Error(`${label}: ${JSON.stringify(detail)}`);
};
const one = (rows, label) => {
  if (rows.length !== 1) fail(`${label}: expected one row`, rows.length);
  return rows[0];
};
const expectError = async (label, action, { code, message }) => {
  let error = null;
  await db.exec('begin');
  try {
    await action();
  } catch (caught) {
    error = caught;
  } finally {
    await db.exec('rollback');
  }
  if (error?.code !== code || (message !== undefined && error?.message !== message)) {
    fail(label, { expected: [code, message], got: [error?.code, error?.message] });
  }
};

// El SQL del script, tal cual.
const printed = spawnSync('python3', ['-I', '-c', `
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('add_instance_offers', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
print(json.dumps({'apply': module.SQL_APPLY, 'read': module.SQL_READ}))
`, join(root, 'scripts/add_instance_offers.py')], { encoding: 'utf8' });
if (printed.status !== 0) fail('python3 did not print the SQL of the script', printed.stderr);
const SCRIPT = JSON.parse(printed.stdout);

// ---------------------------------------------------------------------------
// La instancia, de la forma de Maribel (instancia.toml de setter-instancia-maribel,
// origin/main 4b07f72, y los scopes que publicaron sus SQL).
// ---------------------------------------------------------------------------
const M = {
  tenant: 'lancemos', funnel: 'maribel', bindingVersion: 2, ally: 'maribel', brand: 'El Protocolo',
  timezone: 'America/Mexico_City', productId: 8443991, hotlink: 'F107441770M',
  productName: 'El Protocolo Biohacking +', currency: 'MXN', price: '1397', accountId: 3, inboxId: 13,
  inboundScope: 'maribel-inbound', inboundVersion: 2, copyVersion: 'maribel-whatsapp-contact-v1',
};
const ADS = { offer_code: 'u8ikyx78', site: 'elprotocolo', landing_id: 'epma-vsl-ads-a',
  page_host: 'www.elprotocolo.net', page_path: '/epma/evg/vsl/ads-a' };
const ORG = { offer_code: 'mmliigg5', site: 'elprotocolo', landing_id: 'epma-vsl-org-a',
  page_host: 'www.elprotocolo.net', page_path: '/epma/evg/vsl/org-a' };
const CHANNEL_REF = `chatwoot-inbox:${M.inboxId}`;
const POLICY = 'maribel-recuperacion-un-toque';

await db.query(`
  insert into public.commercial_ally_runtime_bindings
    (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
     lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
     product_name, product_price, currency, offer_code, consent_copy_version,
     hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
     inbound_scope_key, inbound_scope_version)
  values ($1,$2,1,'retired',$3,$4,$5,$6,$7,$8,$9,$10,$11::numeric,$12,$13,$14,$15,$16,12,$17,1),
         ($1,$2,2,'active',$3,$4,$5,$6,$7,$8,$9,$10,$11::numeric,$12,$13,$14,$15,$16,$18,$17,$19)
`, [M.tenant, M.funnel, M.ally, M.brand, ADS.site, ADS.landing_id, ADS.page_host, ADS.page_path,
  M.hotlink, M.productName, M.price, M.currency, ADS.offer_code, M.copyVersion, M.productId,
  M.accountId, M.inboundScope, M.inboxId, M.inboundVersion]);
await db.query(`
  insert into public.hotmart_purchase_intent_scopes
    (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref, offer_ref, max_lookback, active)
  values ($1,$2,$3,$4,$5,'24 hours'::interval,true)
`, [M.tenant, M.funnel, String(M.productId), M.hotlink, ADS.offer_code]);
await db.query(`
  insert into public.commercial_ally_hotmart_purchase_policies
    (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
  values ($1,$2,1,true,'7 days'::interval), ($1,$2,2,true,'7 days'::interval)
`, [M.tenant, M.funnel]);
await db.query(`
  insert into public.inbound_commercial_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id, external_product_id,
     offer_code, approved_by, approved_at, published_at)
  values ($1,1,'published',$2,$3,12,$4,$5,'aprovisionar-maribel-v1',now(),now()),
         ($1,2,'published',$2,$3,$6,$4,$5,'inbox-binding:dan',now(),now())
`, [M.inboundScope, M.tenant, M.accountId, M.hotlink, ADS.offer_code, M.inboxId]);
await db.query(`
  insert into public.checkout_offer_catalog
    (tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref, offer_code,
     checkout_base_url, checkout_mode, default_for_inbound, status, version, approved_by, approved_at)
  values ($1,$2,$3,1,$4,$5,$6,$7,10,true,'active',1,'aprovisionar-maribel-v1',now()),
         ($1,$2,$3,2,$4,$5,$6,$7,10,true,'active',1,'inbox-binding:dan',now())
`, [M.tenant, M.funnel, M.inboundScope, M.hotlink, ADS.landing_id, ADS.offer_code,
  `https://pay.hotmart.com/${M.hotlink}`]);
// DESVIACION DOCUMENTADA (la de la cadena de ATT1): la ventana de envio va de 00:00 a 23:59, porque la
// puerta de arranque exige p_now a +-5 minutos del reloj de la base.
await db.query(`
  insert into public.followup_policy_versions
    (policy_key, version, status, purpose, timezone, business_windows, grace_period, expires_after,
     max_automatic_messages, steps, approved_by, approved_at, published_at)
  values ($1,1,'published','cart_recovery',$2,
          '[{"days":[1,2,3,4,5,6,7],"start":"00:00","end":"23:59"}]'::jsonb,
          '0 seconds'::interval,'1 day'::interval,1,
          '[{"mode":"freeform","step_key":"first_contact"},{"mode":"freeform","step_key":"payment_failure_first_contact"}]'::jsonb,
          'operator-test',now(),now())
`, [POLICY, M.timezone]);

const insertScope = (key, version, { source = 'hotmart', eventType = 'PURCHASE_OUT_OF_SHOPPING_CART',
  extraEvents = [], mode = 'manual_cohort' } = {}) => db.query(`
  insert into public.pilot_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     channel, channel_provider, channel_account_ref, source, source_event_type,
     additional_source_event_types, external_product_id, offer_code,
     additional_offer_codes, purpose, policy_key, policy_version, timezone,
     max_cohort_contacts, max_outbound_request_starts_total,
     max_outbound_request_starts_per_day, audience_mode,
     approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5,'whatsapp','waba',$6,$7,$8,$9::text[],$10,$11,
          '{}'::text[],'cart_recovery',$12,1,$13,20,150,150,$14,'operator-test',now(),now())
`, [key, version, M.tenant, M.accountId, M.inboxId, CHANNEL_REF, source, eventType, extraEvents,
  String(M.productId), ADS.offer_code, POLICY, M.timezone, mode]);
const setState = async (key, version, state) => {
  const { generation } = one((await db.query(
    'select generation from public.pilot_runtime_controls where scope_key = $1', [key])).rows, key);
  return one((await db.query(`
    select * from public.set_lancemos_pilot_runtime_state($1,$2,$3,$4,'operator-test','controlled-test')
  `, [key, version, generation, state])).rows, `${key} ${state}`);
};
const control = (key, version) => db.query(`
  insert into public.pilot_runtime_controls (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
  values ($1,$2,'inactive',0,'operator-test','default-off')
`, [key, version]);
const REC = { key: 'maribel-recuperacion', version: 4 };
const FIRST = { key: 'maribel-primer-contacto', version: 4 };
const FAILURE = { key: 'maribel-pago-rechazado', version: 1 };
for (let version = 1; version <= 4; version += 1) await insertScope(REC.key, version);
for (let version = 1; version <= 4; version += 1) {
  await insertScope(FIRST.key, version, { source: 'landing', eventType: 'PRECHECKOUT_FORM_SUBMITTED',
    mode: 'consented_intent_in_cohort' });
}
await insertScope(FAILURE.key, 1, { eventType: 'PURCHASE_CANCELED' });
await control(REC.key, REC.version);
await control(FIRST.key, FIRST.version);
await control(FAILURE.key, FAILURE.version);
await setState(REC.key, REC.version, 'armed');
await setState(FAILURE.key, FAILURE.version, 'armed');

// ---------------------------------------------------------------------------
// 1. El trigger.
// ---------------------------------------------------------------------------
const append = (key, version, codes, marker = `${key}/${version}`) => async () => {
  await db.query(`select set_config('pilot_scope.offer_append', $1, true)`, [marker]);
  await db.query(`update public.pilot_scope_versions set additional_offer_codes = $3::text[]
                  where scope_key = $1 and version = $2`, [key, version, codes]);
};
const immutable = { code: '55000', message: 'published_pilot_scope_is_immutable' };
await expectError('an append without the request of the transaction', async () => {
  await db.query(`update public.pilot_scope_versions set additional_offer_codes = '{mmliigg5}'
                  where scope_key = $1 and version = $2`, [REC.key, REC.version]);
}, immutable);
await expectError('an append requested for another row', append(REC.key, REC.version, ['mmliigg5'],
  `${REC.key}/3`), immutable);
await expectError('an append that also changes the caps', async () => {
  await db.query(`select set_config('pilot_scope.offer_append', $1, true)`, [`${REC.key}/${REC.version}`]);
  await db.query(`update public.pilot_scope_versions set additional_offer_codes = '{mmliigg5}',
                    max_outbound_request_starts_total = 9999
                  where scope_key = $1 and version = $2`, [REC.key, REC.version]);
}, immutable);
await expectError('an append that repeats a code', append(REC.key, REC.version, ['mmliigg5', 'mmliigg5']),
  immutable);
await expectError('an update that leaves the list as it was', append(REC.key, REC.version, []), immutable);
// Las dos primeras pasan; lo que viene despues, en la misma transaccion, no.
const twoFirst = () => append(REC.key, REC.version, ['mmliigg5', 's4y32uzp'])();
await expectError('removing an offer', async () => {
  await twoFirst();
  await append(REC.key, REC.version, ['mmliigg5'])();
}, immutable);
await expectError('reordering the offers', async () => {
  await twoFirst();
  await append(REC.key, REC.version, ['s4y32uzp', 'mmliigg5', 'o11h7tn3'])();
}, immutable);
await expectError('deleting a published scope', async () => {
  await db.query(`select set_config('pilot_scope.offer_append', $1, true)`, [`${REC.key}/${REC.version}`]);
  await db.query('delete from public.pilot_scope_versions where scope_key = $1 and version = $2',
    [REC.key, REC.version]);
}, immutable);
await db.exec('begin');
await append(REC.key, REC.version, ['mmliigg5'])();
const appended = one((await db.query(`select additional_offer_codes, current_setting('pilot_scope.offer_append', true) m
  from public.pilot_scope_versions where scope_key = $1 and version = $2`, [REC.key, REC.version])).rows, 'append');
if (JSON.stringify(appended.additional_offer_codes) !== '["mmliigg5"]') fail('a valid append did not land', appended);
await db.exec('rollback');
const marker = one((await db.query(`select coalesce(current_setting('pilot_scope.offer_append', true), '') m`)).rows,
  'marker').m;
if (marker !== '') fail('the request of the transaction outlived it', marker);

// ---------------------------------------------------------------------------
// 2. Antes: un caso en vuelo, un evento guardado y una oferta que no entra.
// ---------------------------------------------------------------------------
const dbNow = async () => new Date((await db.query('select clock_timestamp() as now')).rows[0].now);
const CART_AT = new Date((await dbNow()).getTime() - 30 * 60_000);
const CAPTURED_CART = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/hotmart_cart_abandonment_rejected_v1.json'), 'utf8')).payload;
let personIndex = 0;
const person = (label) => {
  personIndex += 1;
  const suffix = String(personIndex).padStart(2, '0');
  return { label, name: `Compradora ${label}`, email: `maribel-offer-${suffix}@example.test`,
    phone: `521555000${suffix}` };
};
const cartPayload = (lead, offer) => {
  const payload = structuredClone(CAPTURED_CART);
  payload.id = `maribel-offer-cart-${lead.email}-${offer.offer_code}`;
  payload.creation_date = CART_AT.getTime();
  payload.data.product = { id: M.productId, name: M.productName };
  payload.data.offer = { code: offer.offer_code };
  payload.data.buyer = { name: lead.name, email: lead.email, phone: lead.phone };
  return payload;
};
const admitCart = async (lead, offer) => {
  const payload = cartPayload(lead, offer);
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [M.tenant, M.funnel, M.bindingVersion, payload.id, JSON.stringify(payload), lead.email, lead.phone])).rows,
  `${lead.label} cart`);
  if (admitted.outcome !== 'inserted') fail(`${lead.label}: cart not admitted`, admitted);
  return { eventId: admitted.webhook_event_id, abandonedAt: new Date(payload.creation_date) };
};
const createContact = async (lead, eventId) => {
  lead.contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone, country_iso) values ($1,$2,$3,'MX') returning id
  `, [lead.name, lead.email, lead.phone])).rows, `${lead.label} contact`).id;
  await db.query(`
    insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id)
    values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)
  `, [lead.contact, lead.email, lead.phone, eventId]);
};
const enroll = async (lead) => {
  const { generation } = one((await db.query(
    'select generation from public.pilot_runtime_controls where scope_key = $1', [REC.key])).rows, 'gen');
  await db.query(`select * from public.set_lancemos_pilot_cohort_member($1,$2,$3,$4,'active','operator-test','controlled-test')`,
    [REC.key, REC.version, lead.contact, generation]);
};
const planCart = async (lead, offer, cart) => one((await db.query(`
  select * from public.plan_lancemos_pilot_cart_recovery($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
`, [cart.eventId, lead.contact, String(M.productId), M.productName, offer.offer_code, POLICY, 1,
  cart.abandonedAt.toISOString(), M.accountId, M.inboxId, lead.phone, REC.key, REC.version])).rows,
`${lead.label} plan`);
const dispatch = async (lead, plan) => {
  const worker = `maribel-offer-${lead.label}`;
  const now = await dbNow();
  const claimed = (await db.query(`select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)`,
    [worker, now])).rows.filter((row) => row.id === plan.scheduled_action_id);
  if (claimed.length !== 1) fail(`${lead.label}: the action was not claimed`, claimed);
  const lease = claimed[0].lease_generation;
  const decision = one((await db.query('select * from public.reevaluate_followup_action($1,$2,$3,$4)',
    [plan.scheduled_action_id, worker, lease, now])).rows, `${lead.label} reevaluation`);
  if (decision.decision !== 'execute') fail(`${lead.label}: reevaluation`, decision);
  const attempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
  `, [plan.scheduled_action_id, worker, lease, decision.case_version, decision.sequence_revision, now])).rows,
  `${lead.label} reservation`);
  const started = one((await db.query('select * from public.mark_lancemos_pilot_request_started($1,$2,$3,$4,$5)',
    [plan.scheduled_action_id, attempt.id, worker, lease, await dbNow()])).rows, `${lead.label} start`);
  if (started.phase !== 'request_started' || started.pilot_authorization_id == null) {
    fail(`${lead.label}: did not start`, started);
  }
  const accepted = one((await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
  `, [plan.scheduled_action_id, attempt.id, worker, lease, `9101${personIndex}`, `maribel-wamid-${lead.label}`,
    `Plantilla aprobada para ${lead.name}`, await dbNow()])).rows, `${lead.label} acceptance`);
  if (accepted.status !== 'accepted_by_chatwoot') fail(`${lead.label}: acceptance`, accepted);
};

const inFlight = person('en-vuelo');
const inFlightCart = await admitCart(inFlight, ADS);
await createContact(inFlight, inFlightCart.eventId);
await enroll(inFlight);
const inFlightPlan = await planCart(inFlight, ADS, inFlightCart);
const stored = person('guardado');
const storedCart = await admitCart(stored, ADS);
const organic = person('organica');
await expectError('a cart of mmliigg5 before the offer is added', async () => {
  const payload = cartPayload(organic, ORG);
  await db.query('select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)',
    [M.tenant, M.funnel, M.bindingVersion, payload.id, JSON.stringify(payload), organic.email, organic.phone]);
}, { code: '22023', message: 'portable_hotmart_cart_binding_mismatch' });

// ---------------------------------------------------------------------------
// 3 y 4. El SQL del script: en seco y aplicado.
// ---------------------------------------------------------------------------
const PLAN = [{
  tenant_ref: M.tenant, funnel_ref: M.funnel, binding_version: M.bindingVersion,
  new_codes: [ORG.offer_code], new_landings: [{ offer_code: ORG.offer_code, site: ORG.site,
    landing_id: ORG.landing_id, page_host: ORG.page_host, page_path: ORG.page_path }],
  offers: [{ offer_code: ORG.offer_code, landing_id: ORG.landing_id }],
  old_codes: [], old_landings: [],
}];
const runScript = async (commit) => {
  await db.exec('drop table if exists pg_temp._args');
  await db.exec('create temp table _args (k text primary key, v text)');
  await db.query("insert into pg_temp._args values ('plan', $1), ('actor', 'dan')", [JSON.stringify(PLAN)]);
  const results = await db.exec(SCRIPT.apply + (commit ? 'commit;' : 'rollback;'));
  const rows = results.filter((result) => result.rows?.length === 1 && 'plan' in result.rows[0]);
  if (rows.length !== 1) fail('the script did not return its plan', results.length);
  return JSON.parse(rows[0].rows[0].plan);
};
const TABLES = ['commercial_ally_runtime_bindings', 'hotmart_purchase_intent_scopes', 'checkout_offer_catalog',
  'pilot_scope_versions', 'pilot_runtime_controls', 'pilot_control_events', 'recovery_cases', 'scheduled_actions'];
const snapshot = async () => {
  const out = {};
  for (const table of TABLES) {
    out[table] = one((await db.query(`select count(*) || ':' || coalesce(md5(string_agg(t::text, '|' order by t::text)), '') s
      from public.${table} t`)).rows, table).s;
  }
  return out;
};
const controls = async () => (await db.query(`select scope_key, scope_version, runtime_state, generation
  from public.pilot_runtime_controls order by scope_key`)).rows;

const before = await snapshot();
const controlsBefore = await controls();
const dry = await runScript(false);
if (JSON.stringify(await snapshot()) !== JSON.stringify(before)) fail('the dry run changed something', dry);
if (dry.bindings.length !== 1 || dry.pilot_scopes.length !== 3 || dry.intent_scopes.length !== 1
    || dry.catalog_rows.length !== 1) fail('the dry run plan', dry);

const applied = await runScript(true);
const binding = one((await db.query(`select status, additional_offer_codes, additional_offer_landings
  from public.resolve_commercial_ally_runtime_binding($1,$2,$3)`, [M.tenant, M.funnel, M.bindingVersion])).rows,
'binding after');
if (JSON.stringify(binding.additional_offer_codes) !== '["mmliigg5"]'
    || binding.additional_offer_landings[0]?.page_path !== ORG.page_path) fail('the binding row', binding);
const bindings = (await db.query(`select binding_version || ':' || status v from public.commercial_ally_runtime_bindings
  order by binding_version`)).rows.map((row) => row.v).join(',');
if (bindings !== '1:retired,2:active') fail('no binding version was published or retired', bindings);
const scopes = (await db.query(`select scope_key || '/' || version || ':' || array_to_string(additional_offer_codes, ',') v
  from public.pilot_scope_versions where additional_offer_codes <> '{}' order by 1`)).rows.map((row) => row.v);
if (JSON.stringify(scopes) !== JSON.stringify([
  'maribel-pago-rechazado/1:mmliigg5', 'maribel-primer-contacto/4:mmliigg5', 'maribel-recuperacion/4:mmliigg5',
])) fail('the offer on the pilot scopes, same versions only', scopes);
if (JSON.stringify(await controls()) !== JSON.stringify(controlsBefore)) {
  fail('a control changed', { before: controlsBefore, after: await controls() });
}
const intent = one((await db.query(`select tenant_ref, funnel_ref, max_lookback::text lookback from public.hotmart_purchase_intent_scopes
  where active and offer_ref = 'mmliigg5'`)).rows, 'intent scope');
if (intent.lookback !== '24:00:00' || intent.funnel_ref !== M.funnel) fail('the intent scope', intent);
const catalog = one((await db.query(`select landing_ref, default_for_inbound, scope_version, approved_by from public.checkout_offer_catalog
  where offer_code = 'mmliigg5' and status = 'active'`)).rows, 'catalog');
if (catalog.landing_ref !== ORG.landing_id || catalog.default_for_inbound || catalog.scope_version !== M.inboundVersion
    || catalog.approved_by !== 'add-offers:dan') fail('the catalog row', catalog);

// ---------------------------------------------------------------------------
// 5. Despues: lo que estaba en vuelo sale, lo guardado se planifica, lo nuevo entra.
// ---------------------------------------------------------------------------
await dispatch(inFlight, inFlightPlan);
await createContact(stored, storedCart.eventId);
await enroll(stored);
const storedPlan = await planCart(stored, ADS, storedCart);
if (!storedPlan.recovery_case_id) fail('the event stored with binding v2 did not plan', storedPlan);
const organicCart = await admitCart(organic, ORG);
await createContact(organic, organicCart.eventId);
await enroll(organic);
const organicPlan = await planCart(organic, ORG, organicCart);
const organicBinding = one((await db.query(`select scope_key, scope_version from public.pilot_recovery_case_bindings
  where recovery_case_id = $1`, [organicPlan.recovery_case_id])).rows, 'organic case binding');
if (organicBinding.scope_version !== REC.version) fail('the organic cart planned on another version', organicBinding);
const provenance = one((await db.query(`select binding_version, offer_ref from public.commercial_ally_hotmart_event_bindings
  where webhook_event_id = $1`, [organicCart.eventId])).rows, 'organic provenance');
if (provenance.binding_version !== M.bindingVersion || provenance.offer_ref !== ORG.offer_code) {
  fail('the organic cart provenance', provenance);
}

// El formulario de org-a (el 422 ghl_landing_unknown de hoy, del lado de la base): entra con su oferta.
const formLead = person('formulario');
const formId = `maribel-offer-form-${formLead.email}`;
const pageUrl = `https://${ORG.page_host}${ORG.page_path}`;
const checkoutUrl = `https://pay.hotmart.com/${M.hotlink}?off=${ORG.offer_code}&checkoutMode=10`;
const form = one((await db.query(`
  select * from public.admit_portable_observed_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb)
`, [M.tenant, M.funnel, M.bindingVersion, formId, JSON.stringify({
  id: formId, event: 'lead.precheckout', version: '1.1.0', created_at: CART_AT.toISOString(),
  source: { system: 'landing', site: ORG.site, aliado: M.brand, landing_id: ORG.landing_id, page_url: pageUrl },
  data: {
    buyer: { name: formLead.name, email: formLead.email, phone: `+${formLead.phone}`,
      phone_country_code: '52', phone_national: formLead.phone.slice(2) },
    product: { hotlink: M.hotlink, id: null, name: M.productName, price: Number(M.price), currency: M.currency },
    offer: { code: ORG.offer_code }, checkout_url: checkoutUrl,
    checkout_country: { iso: 'MX', source: 'phone_country_code' },
    consent: { marketing_optin: true, whatsapp_contact: true, copy_version: M.copyVersion },
  },
  dedupe_key: `${ORG.site}:${ORG.offer_code}:${formLead.email}`,
}), JSON.stringify({
  external_submission_id: formId, event_type: 'PRECHECKOUT_FORM_SUBMITTED', contract_version: '1.1.0',
  submitted_at: CART_AT.toISOString(),
  source: { tenant_ref: M.tenant, funnel_ref: M.funnel, landing_ref: ORG.landing_id, page_url: pageUrl,
    aliado: M.brand },
  identity: { email: formLead.email, phone: formLead.phone, phone_valid: true, phone_country_iso: 'MX' },
  lead: { full_name: formLead.name },
  commerce: { product_ref: M.hotlink, product_name: M.productName, offer_ref: ORG.offer_code,
    price: M.price, currency: M.currency, checkout_url: checkoutUrl },
  dedupe_key: `${ORG.site}:${ORG.offer_code}:${formLead.email}`,
  consent: { terms_accepted: false, privacy_accepted: false, marketing_optin: true, whatsapp_contact: true,
    copy_version: M.copyVersion },
  assurance: { provisional: false, provider_observed: true, activation_authorized: true },
})])).rows, 'org-a form');
const formIntent = one((await db.query('select offer_ref from public.purchase_intents where id = $1',
  [form.purchase_intent_id])).rows, 'org-a intent');
if (form.outcome !== 'inserted' || formIntent.offer_ref !== ORG.offer_code) fail('the org-a form', { form, formIntent });

// ---------------------------------------------------------------------------
// 6. Otra vez: nada que hacer.
// ---------------------------------------------------------------------------
PLAN[0].old_codes = [ORG.offer_code];
PLAN[0].old_landings = PLAN[0].new_landings;
const again = await runScript(true);
if (again.bindings.length || again.intent_scopes.length || again.catalog_rows.length || again.pilot_scopes.length
    || again.pilot_scopes_already.length !== 3) fail('a second run changed something', again);

console.log(JSON.stringify({
  pilot_scope_offer_append: 'OK',
  dry_run_wrote_nothing: true,
  applied: { bindings: applied.bindings.length, pilot_scopes: applied.pilot_scopes.length },
  in_flight_case_sent: true,
  stored_event_planned: true,
  organic_cart_planned_on: `${organicBinding.scope_key}/${organicBinding.scope_version}`,
}));
