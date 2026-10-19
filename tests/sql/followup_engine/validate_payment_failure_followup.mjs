// El seguimiento del pago rechazado, B2 o Z (migracion 20261019000700), con
// las RPC reales sobre la cadena completa de migraciones.
//
// Prueba:
//   0. la migracion: cambia exactamente tres funciones (el arranque del pago
//      rechazado, su planificacion y el pedido de renovacion de la 0600), cada
//      una es su definicion anterior con
//      su texto reemplazado y nada mas; agrega tres (el registro, el contexto
//      y el trigger que hace inmutable la tabla) y la tabla; las dos entradas
//      son solo de service_role, el trigger y la tabla de nadie, y correrla
//      dos veces aborta con 55000 sin tocar nada;
//   a. B1 del banco (declined) con su registro: el registro es idempotente,
//      otro valor choca, una combinacion sin sentido es 22023 y la fila no se
//      reescribe; al aceptarse B1 el motor inserta payment_failure_followup a
//      la aceptacion + 24 h; la siembra crea el cupon 2; con la ventana
//      cerrada la reevaluacion da execute; el contexto dice second_touch; el
//      link se emite, el arranque pasa por el del pago rechazado contra su
//      scope, se acepta y la secuencia termina con dos toques
//      (policy_exhausted, sin tercer paso);
//   b. B1 del saldo (insufficient_funds) con su cupon registrado: last_touch;
//   c. B1 sin registro (cayo a la plantilla de hoy): second_touch;
//   d. el bug: un segundo rechazo con el seguimiento pendiente se suma al caso,
//      no le cambia el due_at y le corre el expected_case_version: la
//      reevaluacion no lo pausa. Con la version vieja (lo que pasaba antes) la
//      misma reevaluacion pausa con authoritative_state_changed;
//   e. contestar cancela el seguimiento (prospect_replied);
//   f. el boton «Quiero mi 10 %» de A2 en la misma conversacion
//      (request_recovery_renewal_v1) no adelanta el seguimiento: devuelve
//      no_pending_last_touch y el paso queda como estaba.
//
// Datos: los de validate_portable_payment_failure_variants.mjs (el manifiesto
// de ATT1, el formulario con consentimiento, los rechazos capturados de
// tests/fixtures/hotmart_purchase_canceled_att1_20261019.json con
// creation_date corrido al reloj de la base, el catalogo del link). La
// politica de dos toques no existe todavia en la instancia (la publica su SQL
// de publicacion): aca van los valores del contrato
// (att1-pago-rechazado-dos-toques, gracia 15 min, expira a los 3 dias, tope 2,
// el paso payment_failure_followup con delay de 24 h, approved_template y
// coupon_ordinal 2). DESVIACIONES DOCUMENTADAS: la ventana de envio es de 00:00
// a 23:59 en vez de 09:00-21:00 de Ciudad de Mexico (la puerta de arranque mira
// el reloj real), y cuando una prueba manda el seguimiento su due_at se corre a
// ahora con un update en vez de esperar las 24 h.
import { PGlite } from '@electric-sql/pglite';
import { randomBytes } from 'node:crypto';
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
  grant usage on schema public to service_role;
`);

const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};
const results = {};

// ---------------------------------------------------------------------------
// 0. La migracion.
// ---------------------------------------------------------------------------
const TARGET = '20261019000700_payment_failure_followup_v1.sql';
const migrationNames = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const targetIndex = migrationNames.indexOf(TARGET);
if (targetIndex < 0) throw new Error(`${TARGET} is missing from the canonical stack`);
const apply = async (file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
));
const snapshot = async () => new Map((await db.query(`
  select p.oid::regprocedure::text as signature,
         pg_get_functiondef(p.oid) as definition,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const tables = async () => (await db.query(`
  select c.relname from pg_class c
  where c.relnamespace = 'public'::regnamespace and c.relkind = 'r'
`)).rows.map((row) => row.relname);

await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of migrationNames.slice(0, targetIndex)) {
  await apply(join(root, 'supabase/migrations', name));
}
const before = await snapshot();
const tablesBefore = await tables();
await apply(join(root, 'supabase/migrations', TARGET));
const after = await snapshot();
const tablesAfter = await tables();
for (const name of migrationNames.slice(targetIndex + 1)) {
  await apply(join(root, 'supabase/migrations', name));
}

const TZ = 'timestamp with time zone';
const START = `mark_portable_payment_failure_request_started(uuid,uuid,text,bigint,${TZ})`;
const PLAN = `plan_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,${TZ})`;
const RENEWAL = `request_recovery_renewal_v1(bigint,bigint,bigint,bigint,text,${TZ})`;
const ENTRYPOINTS = [
  `record_payment_failure_first_touch_v1(uuid,uuid,text,bigint,text,text,${TZ})`,
  `get_payment_failure_followup_context_v1(uuid,text,bigint,${TZ})`,
];
const PRIVATE = ['protect_payment_failure_first_touch_record()'];
const sameSet = (left, right) => JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
const added = [...after.keys()].filter((signature) => !before.has(signature));
const removed = [...before.keys()].filter((signature) => !after.has(signature));
const changed = [...before.keys()].filter((signature) => after.has(signature)
  && (after.get(signature).definition !== before.get(signature).definition
      || after.get(signature).service_x !== before.get(signature).service_x
      || after.get(signature).api_x !== before.get(signature).api_x));
const addedTables = tablesAfter.filter((name) => !tablesBefore.includes(name));
if (!sameSet(added, [...ENTRYPOINTS, ...PRIVATE]) || removed.length !== 0
    || !sameSet(changed, [START, PLAN, RENEWAL])
    || !sameSet(addedTables, ['payment_failure_first_touch_records'])) {
  throw new Error(`the migration changed something else: ${JSON.stringify({ added, removed, changed, addedTables })}`);
}
for (const signature of ENTRYPOINTS) {
  const row = after.get(signature);
  if (row.service_x !== true || row.api_x !== false) {
    throw new Error(`${signature} is not service_role only: ${JSON.stringify(row)}`);
  }
}
for (const signature of PRIVATE) {
  const row = after.get(signature);
  if (row.service_x !== false || row.api_x !== false) {
    throw new Error(`${signature} is executable by a role of the API`);
  }
}
// Cada funcion cambiada es su definicion anterior con su reemplazo y nada mas.
// Los textos se leen de la migracion misma, entre sus marcas $old$ y $new$.
const migrationText = readFileSync(join(root, 'supabase/migrations', TARGET), 'utf8');
const specs = [...migrationText.matchAll(
  /\(\d+,\s*'public\.([^']+)',\s*\$old\$([\s\S]*?)\$old\$,\s*\$new\$([\s\S]*?)\$new\$\)/g,
)].map((match) => ({
  signature: match[1].replace(/timestamptz/g, TZ),
  old: match[2],
  next: match[3],
}));
if (!sameSet(specs.map((spec) => spec.signature), [START, PLAN, RENEWAL])) {
  throw new Error(`the replacements are not the documented ones: ${JSON.stringify(specs.map((spec) => spec.signature))}`);
}
for (const spec of specs) {
  const previous = before.get(spec.signature).definition;
  if (previous.split(spec.old).length !== 2
      || previous.split(spec.old).join(spec.next) !== after.get(spec.signature).definition
      || after.get(spec.signature).service_x !== before.get(spec.signature).service_x
      || after.get(spec.signature).api_x !== before.get(spec.signature).api_x) {
    throw new Error(`${spec.signature} is not its previous definition with its text replaced`);
  }
}
const tableAcl = one((await db.query(`
  select c.relrowsecurity as rls,
         bool_or(has_table_privilege(r, c.oid, 'SELECT')
                 or has_table_privilege(r, c.oid, 'INSERT')) as readable
  from pg_class c
  cross join unnest(array['anon', 'authenticated', 'service_role']) as r
  where c.oid = 'public.payment_failure_first_touch_records'::regclass
  group by c.relrowsecurity
`)).rows, 'table acl');
if (!tableAcl.rls || tableAcl.readable) {
  throw new Error(`payment_failure_first_touch_records acl: ${JSON.stringify(tableAcl)}`);
}
let rerun = null;
await db.exec('begin');
try {
  await apply(join(root, 'supabase/migrations', TARGET));
} catch (error) {
  rerun = error;
} finally {
  await db.exec('rollback');
}
if (rerun === null || rerun.code !== '55000'
    || rerun.message !== 'payment_failure_followup_already_applied') {
  throw new Error(`a second run did not abort: ${rerun ? `${rerun.code} ${rerun.message}` : 'success'}`);
}
results.migration = {
  added: added.length, changed: changed.length, tables: addedTables.length,
  rerun: `${rerun.code}:${rerun.message}`,
};

// ---------------------------------------------------------------------------
// El manifiesto de ATT1 (el mismo lector minimo que la cadena de ATT1).
// ---------------------------------------------------------------------------
const readManifest = (text) => {
  const manifest = {};
  const walk = (path) => path.reduce((node, key) => (node[key] ??= {}), manifest);
  let table = manifest;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    let match = line.match(/^\[\[([a-z_.]+)\]\]$/);
    if (match) {
      const path = match[1].split('.');
      const parent = walk(path.slice(0, -1));
      table = {};
      (parent[path.at(-1)] ??= []).push(table);
      continue;
    }
    match = line.match(/^\[([a-z_.]+)\]$/);
    if (match) {
      table = walk(match[1].split('.'));
      continue;
    }
    match = line.match(/^([a-z_]+)\s*=\s*(?:"([^"]*)"|(-?\d+)|(true|false))\s*(?:#.*)?$/);
    if (match) {
      table[match[1]] = match[2] ?? (match[3] !== undefined ? Number(match[3]) : match[4] === 'true');
    }
  }
  return manifest;
};
const required = (value, label) => {
  if (value === undefined || value === null || value === '') {
    throw new Error(`ATT1 fixture without ${label}`);
  }
  return value;
};
const manifest = readManifest(readFileSync(
  join(root, 'tests/fixtures/instances/att1/instancia.toml'), 'utf8',
));
const offers = required(manifest.hotmart?.ofertas, 'hotmart.ofertas').map((offer) => {
  const url = new URL(required(offer.url, 'ofertas.url'));
  return {
    offer_code: required(offer.codigo, 'ofertas.codigo'),
    site: required(offer.site, 'ofertas.site'),
    landing_id: required(offer.landing_id, 'ofertas.landing_id'),
    page_host: url.host,
    page_path: url.pathname,
  };
});
const ATT1 = {
  tenant: required(manifest.instancia?.tenant_ref, 'instancia.tenant_ref'),
  funnel: required(manifest.instancia?.funnel_ref, 'instancia.funnel_ref'),
  ally: required(manifest.instancia?.ally_ref, 'instancia.ally_ref'),
  bindingVersion: required(manifest.instancia?.binding_version, 'instancia.binding_version'),
  brand: required(manifest.instancia?.marca, 'instancia.marca'),
  timezone: required(manifest.instancia?.zona_horaria, 'instancia.zona_horaria'),
  productId: required(manifest.hotmart?.product_id, 'hotmart.product_id'),
  hotlink: required(manifest.hotmart?.hotlink, 'hotmart.hotlink'),
  productName: required(manifest.hotmart?.product_name, 'hotmart.product_name'),
  currency: required(manifest.hotmart?.moneda, 'hotmart.moneda'),
  price: required(manifest.hotmart?.precio, 'hotmart.precio'),
  accountId: required(manifest.chatwoot?.account_id, 'chatwoot.account_id'),
  inboxId: required(manifest.chatwoot?.inbox_id, 'chatwoot.inbox_id'),
  inboundScope: required(manifest.inbound?.scope_key, 'inbound.scope_key'),
  inboundVersion: required(manifest.inbound?.scope_version, 'inbound.scope_version'),
  copyVersion: required(manifest.consentimiento?.copy_version, 'consentimiento.copy_version'),
};
const [defaultOffer, ...additionalOffers] = offers;
const offerByCode = (code) => {
  const offer = offers.find((candidate) => candidate.offer_code === code);
  if (!offer) throw new Error(`offer ${code} is not in the ATT1 manifest`);
  return offer;
};
const PILOT = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/instances/att1/politica-piloto.json'), 'utf8',
));
const cartScope = required(PILOT.pilot_scope, 'politica-piloto.pilot_scope');
const cartPolicy = required(PILOT.policy, 'politica-piloto.policy');
const CHANNEL_PROVIDER = required(cartScope.channel_provider, 'pilot_scope.channel_provider');
const CHANNEL_REF = `${required(cartScope.channel_account_ref_prefix, 'pilot_scope.channel_account_ref_prefix')}${ATT1.inboxId}`;
const ALL_DAY = required(cartPolicy.business_windows, 'policy.business_windows')
  .map((window) => ({ ...window, start: '00:00', end: '23:59' }));

// La politica de dos toques del contrato, con la desviacion de la ventana.
const REFUSAL = {
  scope: 'att1-pago-rechazado',
  scopeVersion: 1,
  policy: 'att1-pago-rechazado-dos-toques',
  policyVersion: 1,
  graceMinutes: 15,
  steps: [
    { mode: 'freeform', step_key: 'payment_failure_first_contact' },
    {
      mode: 'freeform', step_key: 'payment_failure_followup', delay: '24 hours',
      approved_template: true, coupon_ordinal: 2,
    },
  ],
};
const FOLLOWUP_DELAY_MS = 24 * 3_600_000;

await db.query(`
  insert into public.commercial_ally_runtime_bindings
    (tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name,
     lead_site, lead_landing_id, lead_page_host, lead_page_path, product_hotlink,
     product_name, product_price, currency, offer_code, consent_copy_version,
     hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id,
     inbound_scope_key, inbound_scope_version, additional_offer_codes,
     additional_offer_landings)
  values ($1,$2,$3,'active',$4,$5,$6,$7,$8,$9,$10,$11,$12::numeric,$13,$14,$15,
          $16,$17,$18,$19,$20,$21::text[],$22::jsonb)
`, [
  ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, ATT1.ally, ATT1.brand,
  defaultOffer.site, defaultOffer.landing_id, defaultOffer.page_host,
  defaultOffer.page_path, ATT1.hotlink, ATT1.productName, ATT1.price,
  ATT1.currency, defaultOffer.offer_code, ATT1.copyVersion, ATT1.productId,
  ATT1.accountId, ATT1.inboxId, ATT1.inboundScope, ATT1.inboundVersion,
  additionalOffers.map((offer) => offer.offer_code),
  JSON.stringify(additionalOffers.map((offer) => ({
    offer_code: offer.offer_code, site: offer.site, landing_id: offer.landing_id,
    page_host: offer.page_host, page_path: offer.page_path,
  }))),
]);
for (const offer of offers) {
  await db.query(`
    insert into public.hotmart_purchase_intent_scopes
      (tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
       offer_ref, max_lookback, active)
    values ($1,$2,$3,$4,$5,$6::interval,true)
  `, [ATT1.tenant, ATT1.funnel, String(ATT1.productId), ATT1.hotlink, offer.offer_code,
    required(PILOT.purchase_intent_scope?.max_lookback, 'purchase_intent_scope.max_lookback')]);
  await db.query(`
    insert into public.checkout_offer_catalog (
      tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref,
      offer_code, checkout_base_url, checkout_mode, default_for_inbound, status,
      version, approved_by, approved_at
    ) values ($1,$2,$3,$4,$5,$6,$7,$8,10,$9,'active',1,'operator-test',now())
  `, [ATT1.tenant, ATT1.funnel, ATT1.inboundScope, ATT1.inboundVersion, ATT1.hotlink,
    offer.landing_id, offer.offer_code, `https://pay.hotmart.com/${ATT1.hotlink}`,
    offer === defaultOffer]);
}
await db.query(`
  insert into public.commercial_ally_hotmart_purchase_policies
    (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
  values ($1,$2,$3,true,$4::interval)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion,
  required(PILOT.purchase_policy?.max_lookback, 'purchase_policy.max_lookback')]);
await db.query(`
  insert into public.followup_policy_versions
    (policy_key, version, status, purpose, timezone, business_windows,
     grace_period, expires_after, max_automatic_messages, steps,
     approved_by, approved_at, published_at)
  values ($1,$2,'published','cart_recovery',$3,$4::jsonb,$5::interval,interval '3 days',2,
          $6::jsonb,'operator-test',now(),now())
`, [REFUSAL.policy, REFUSAL.policyVersion, ATT1.timezone, JSON.stringify(ALL_DAY),
  `${REFUSAL.graceMinutes} minutes`, JSON.stringify(REFUSAL.steps)]);
await db.query(`
  insert into public.pilot_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     channel, channel_provider, channel_account_ref, source, source_event_type,
     additional_source_event_types, external_product_id, offer_code,
     additional_offer_codes, purpose, policy_key, policy_version, timezone,
     max_cohort_contacts, max_outbound_request_starts_total,
     max_outbound_request_starts_per_day, audience_mode,
     approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5,'whatsapp',$6,$7,'hotmart','PURCHASE_CANCELED','{}'::text[],
          $8,$9,$10::text[],'cart_recovery',$11,$12,$13,20,20,20,'consented_intent',
          'operator-test',now(),now())
`, [REFUSAL.scope, REFUSAL.scopeVersion, ATT1.tenant, ATT1.accountId, ATT1.inboxId,
  CHANNEL_PROVIDER, CHANNEL_REF, String(ATT1.productId), defaultOffer.offer_code,
  additionalOffers.map((offer) => offer.offer_code), REFUSAL.policy, REFUSAL.policyVersion,
  ATT1.timezone]);
await db.query(`
  insert into public.pilot_runtime_controls
    (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
  values ($1,$2,'inactive',0,'operator-test','default-off')
`, [REFUSAL.scope, REFUSAL.scopeVersion]);
await db.query(`
  select * from public.set_lancemos_pilot_runtime_state($1,$2,0,'armed','operator-test','controlled-test')
`, [REFUSAL.scope, REFUSAL.scopeVersion]);

// ---------------------------------------------------------------------------
// Las capturas, las derivaciones y los pasos de cada toque.
// ---------------------------------------------------------------------------
const fixture = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));
const CAPTURE = fixture('hotmart_purchase_canceled_att1_20261019.json').eventos;
const dbNow = async () => new Date((await db.query('select clock_timestamp() as now')).rows[0].now);
const baseMs = Math.floor((await dbNow()).getTime() / 1000) * 1000;
const at = (minutes) => new Date(baseMs + minutes * 60_000);
const digits = (value) => String(value ?? '').replace(/\D/g, '');
const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
const ulidAt = (ms) => {
  let value = (BigInt(ms) << 80n) | BigInt(`0x${randomBytes(10).toString('hex')}`);
  let out = '';
  for (let i = 0; i < 26; i += 1) {
    out = CROCKFORD[Number(value & 31n)] + out;
    value >>= 5n;
  }
  return out;
};
const shifted = (payload, when, suffix = '') => {
  const copy = structuredClone(payload);
  copy.creation_date = when.getTime();
  if (suffix) {
    copy.id = `${payload.id}-${suffix}`;
    copy.data.purchase.transaction = `${payload.data.purchase.transaction}${suffix.toUpperCase().replace(/[^A-Z0-9]/g, '')}`;
  }
  return copy;
};
const byPerson = (persona) => CAPTURE.filter((event) => event.persona === persona)
  .map((event) => event.payload);
const expectError = async (label, run, { code, message } = {}) => {
  let error = null;
  await db.exec('begin');
  try {
    await run();
  } catch (caught) {
    error = caught;
  } finally {
    await db.exec('rollback');
  }
  if (error === null
      || (code !== undefined && error.code !== code)
      || (message !== undefined && error.message !== message)) {
    throw new Error(`${label}: expected ${code} ${message}, got ${error ? `${error.code} ${error.message}` : 'success'}`);
  }
  return error;
};
const rolledBack = async (run) => {
  await db.exec('begin');
  try {
    return await run();
  } finally {
    await db.exec('rollback');
  }
};

// El formulario con consentimiento con el movil mexicano de la persona (el
// precedente inline de validate_att1_portable_chain.mjs).
const admitForm = async (lead, submittedAt) => {
  const offer = lead.offer;
  const id = `followup-form-${lead.email}-${offer.offer_code}`;
  const pageUrl = `https://${offer.page_host}${offer.page_path}`;
  const checkoutUrl = `https://pay.hotmart.com/${ATT1.hotlink}?off=${offer.offer_code}&checkoutMode=10`;
  const raw = {
    id,
    event: 'lead.precheckout',
    version: '1.1.0',
    created_at: submittedAt.toISOString(),
    source: {
      system: 'landing', site: offer.site, aliado: ATT1.brand,
      landing_id: offer.landing_id, page_url: pageUrl,
    },
    data: {
      buyer: {
        name: lead.name, email: lead.email, phone: `+${lead.phone}`,
        phone_country_code: '52', phone_national: lead.phone.slice(2),
      },
      product: {
        hotlink: ATT1.hotlink, id: null, name: ATT1.productName,
        price: Number(ATT1.price), currency: ATT1.currency,
      },
      offer: { code: offer.offer_code },
      checkout_url: checkoutUrl,
      checkout_country: { iso: 'MX', source: 'phone_country_code' },
      consent: { marketing_optin: true, whatsapp_contact: true, copy_version: ATT1.copyVersion },
    },
    dedupe_key: `${offer.site}:${offer.offer_code}:${lead.email}`,
  };
  const canonical = {
    external_submission_id: id,
    event_type: 'PRECHECKOUT_FORM_SUBMITTED',
    contract_version: '1.1.0',
    submitted_at: raw.created_at,
    source: {
      tenant_ref: ATT1.tenant, funnel_ref: ATT1.funnel, landing_ref: offer.landing_id,
      page_url: pageUrl, aliado: ATT1.brand,
    },
    identity: { email: lead.email, phone: lead.phone, phone_valid: true, phone_country_iso: 'MX' },
    lead: { full_name: lead.name },
    commerce: {
      product_ref: ATT1.hotlink, product_name: ATT1.productName, offer_ref: offer.offer_code,
      price: String(ATT1.price), currency: ATT1.currency, checkout_url: checkoutUrl,
    },
    dedupe_key: raw.dedupe_key,
    consent: {
      terms_accepted: false, privacy_accepted: false, marketing_optin: true,
      whatsapp_contact: true, copy_version: ATT1.copyVersion,
    },
    assurance: { provisional: false, provider_observed: true, activation_authorized: true },
  };
  const admitted = one((await db.query(`
    select * from public.admit_portable_observed_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, id, JSON.stringify(raw),
    JSON.stringify(canonical)])).rows, `form ${lead.email}`);
  if (admitted.outcome !== 'inserted') throw new Error(`form ${lead.email}: ${admitted.outcome}`);
};
const admitRefusal = async (payload) => one((await db.query(`
  select * from public.admit_portable_hotmart_payment_failure($1,$2,$3,$4,$5::jsonb,$6,$7)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload),
  payload.data.buyer.email.toLowerCase(), digits(payload.data.buyer.checkout_phone)])).rows,
`refusal ${payload.id}`);
// Los mismos argumentos que va a armar resolution.resolve_event.
const planRefusal = async (lead, refusal) => one((await db.query(`
  select * from public.plan_portable_payment_failure_recovery(
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
  )
`, [refusal.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  refusal.offer, REFUSAL.policy, REFUSAL.policyVersion, refusal.createdAt.toISOString(),
  ATT1.accountId, ATT1.inboxId, lead.phone, REFUSAL.scope, REFUSAL.scopeVersion])).rows,
`plan ${refusal.eventId}`);
let chatwootConversation = 930000;
const refusalFor = async (payload, createdAt, { suffix = '', lead = null } = {}) => {
  let person = lead;
  if (person === null) {
    chatwootConversation += 1;
    person = {
      email: payload.data.buyer.email,
      phone: digits(payload.data.buyer.checkout_phone),
      name: payload.data.buyer.name,
      offer: offerByCode(payload.data.purchase.offer.code),
      chatwoot: String(chatwootConversation),
    };
    await admitForm(person, new Date(createdAt.getTime() - 30 * 60_000));
  }
  const body = shifted(payload, createdAt, suffix);
  const admitted = await admitRefusal(body);
  if (admitted.outcome !== 'inserted') throw new Error(`admission ${body.id}: ${admitted.outcome}`);
  if (person.contact === undefined) {
    person.contact = one((await db.query(`
      insert into public.contacts (full_name, email, phone, country_iso)
      values ($1,$2,$3,'MX') returning id
    `, [person.name, person.email, person.phone])).rows, `contact ${person.email}`).id;
    await db.query(`
      insert into public.contact_points
        (contact_id, type, raw_value, normalized_value, source, source_event_id)
      values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)
    `, [person.contact, person.email, person.phone, admitted.webhook_event_id]);
  }
  return {
    lead: person, eventId: admitted.webhook_event_id, createdAt,
    offer: payload.data.purchase.offer.code,
  };
};
const actionOf = async (id) => one((await db.query(`
  select * from public.scheduled_actions where id = $1
`, [id])).rows, 'action');
const caseOf = async (caseId) => one((await db.query(`
  select status, version, (context ->> 'payment_failure_count')::integer as count
  from public.recovery_cases where id = $1
`, [caseId])).rows, 'case');
// Lo que el dispatcher arma de Chatwoot antes de un no_reply_review: despues
// de una plantilla sin respuesta la ventana de 24 h esta cerrada
// (can_reply = false); una respuesta la abre.
const evidence = (lead, now, { replied = false } = {}) => ({
  p_chatwoot_checked: true,
  p_chatwoot_conversation_id: lead.chatwoot,
  p_chatwoot_checkpoint_message_id: '1',
  p_chatwoot_checkpoint_at: now.toISOString(),
  p_chatwoot_status: 'open',
  p_chatwoot_can_reply: replied,
  p_chatwoot_anchor_found: true,
  p_chatwoot_automation_paused: false,
  p_chatwoot_inbound_after_anchor: replied,
  p_chatwoot_human_activity_after_anchor: false,
});
const reevaluate = async (actionId, { worker, lease, now }, chatwoot) => {
  if (!chatwoot) {
    return one((await db.query(`
      select * from public.reevaluate_followup_action($1,$2,$3,$4)
    `, [actionId, worker, lease, now])).rows, 'reevaluation');
  }
  const names = Object.keys(chatwoot);
  const args = names.map((name, index) => `${name} => $${index + 5}`).join(', ');
  return one((await db.query(`
    select * from public.reevaluate_followup_action(
      p_action_id => $1, p_worker_id => $2, p_lease_generation => $3, p_now => $4, ${args}
    )
  `, [actionId, worker, lease, now, ...names.map((name) => chatwoot[name])])).rows, 'reevaluation');
};
// El claim toma todo lo vencido: cada prueba deja vencida una sola accion.
const claim = async (label, actionId) => {
  const worker = `followup-worker-${label}`;
  const now = await dbNow();
  const claimed = (await db.query(`
    select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
  `, [worker, now])).rows;
  if (claimed.length !== 1 || claimed[0].id !== actionId) {
    throw new Error(`${label}: claimed ${JSON.stringify(claimed.map((row) => [row.id, row.step_key]))}, expected ${actionId}`);
  }
  return { worker, now, lease: claimed[0].lease_generation };
};
const reserveAttempt = async (actionId, { worker, lease, now }, decision) => one((await db.query(`
  select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
`, [actionId, worker, lease, decision.case_version, decision.sequence_revision, now])).rows,
'attempt reservation');
const RECORD = 'select * from public.record_payment_failure_first_touch_v1($1,$2,$3,$4,$5,$6,$7)';
const CONTEXT = 'select * from public.get_payment_failure_followup_context_v1($1,$2,$3,$4)';
let messageNumber = 0;
// Un toque del pago rechazado de punta a punta: claim, reevaluacion, intento,
// lo que haga la prueba antes del arranque (el registro, el link), el arranque
// del pago rechazado contra su scope y la aceptacion en la conversacion.
const send = async (label, lead, actionId, { chatwoot = null, beforeStart } = {}) => {
  const fence = await claim(label, actionId);
  const decision = await reevaluate(actionId, fence, chatwoot ? chatwoot(fence.now) : null);
  if (decision.decision !== 'execute' || decision.reason_code !== 'eligible_for_execution') {
    throw new Error(`${label} reevaluation: ${JSON.stringify(decision)}`);
  }
  const attempt = await reserveAttempt(actionId, fence, decision);
  const extra = beforeStart ? await beforeStart({ ...fence, attempt }) : null;
  const started = one((await db.query(`
    select * from public.mark_portable_payment_failure_request_started($1,$2,$3,$4,$5)
  `, [actionId, attempt.id, fence.worker, fence.lease, await dbNow()])).rows, `${label} start`);
  const authorization = one((await db.query(`
    select scope_key from public.pilot_outbound_request_authorizations where id = $1
  `, [started.pilot_authorization_id])).rows, `${label} authorization`);
  messageNumber += 1;
  const accepted = one((await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
  `, [actionId, attempt.id, fence.worker, fence.lease, lead.chatwoot,
    `followup-wamid-${messageNumber}`, `Plantilla aprobada ${messageNumber} del pago rechazado`,
    await dbNow()])).rows, `${label} acceptance`);
  if (started.phase !== 'request_started' || authorization.scope_key !== REFUSAL.scope
      || accepted.status !== 'accepted_by_chatwoot') {
    throw new Error(`${label} dispatch: ${JSON.stringify({ started, authorization, accepted })}`);
  }
  return { ...fence, attempt, decision, extra };
};
// El seguimiento que inserta el motor al aceptarse B1: un no_reply_review
// anclado al mensaje aceptado, con el vencimiento de B1 y due_at = la
// aceptacion de B1 + 24 h.
const followupOf = async (label, plan) => {
  const rows = (await db.query(`
    select id from public.scheduled_actions
    where recovery_case_id = $1 and step_key = 'payment_failure_followup'
  `, [plan.recovery_case_id])).rows;
  const action = await actionOf(one(rows, `${label} followup`).id);
  const b1 = await actionOf(plan.scheduled_action_id);
  const accepted = one((await db.query(`
    select min(attempt.accepted_at) as at
    from public.followup_delivery_attempts attempt
    join public.scheduled_actions action on action.id = attempt.action_id
    where action.followup_sequence_id = $1
  `, [plan.followup_sequence_id])).rows, 'first acceptance').at;
  if (action.action_type !== 'no_reply_review' || action.status !== 'pending'
      || action.anchor_type !== 'accepted_outbound_message'
      || action.expires_at.getTime() !== b1.expires_at.getTime()
      || action.due_at.getTime() !== accepted.getTime() + FOLLOWUP_DELAY_MS) {
    throw new Error(`${label} followup: ${JSON.stringify({ action, accepted })}`);
  }
  return action;
};
// En produccion el seguimiento espera 24 h: la prueba lo corre a ahora.
const dueNow = (actionId) => db.query(`
  update public.scheduled_actions set due_at = clock_timestamp() - interval '1 second'
  where id = $1
`, [actionId]);
const COUPONS = `
  select * from public.claim_due_recovery_coupons_v1(
    $1::timestamptz,'followup-coupons',60,'ATT1',interval '15 minutes',interval '24 hours',0.10,100
  )
`;
const couponOf = async (caseId, ordinal) => (await db.query(`
  select code, expires_at from public.recovery_coupons
  where recovery_case_id = $1 and ordinal = $2
`, [caseId, ordinal])).rows[0] ?? null;

const [B_PAYLOAD] = byPerson(4);
const [S_PAYLOAD] = byPerson(12);
const [F_PAYLOAD] = byPerson(1);
if (B_PAYLOAD.data.purchase.payment.refusal_reason !== 'Transação negada pelo banco emissor'
    || S_PAYLOAD.data.purchase.payment.refusal_reason !== 'Saldo insuficiente.'
    || F_PAYLOAD.data.purchase.payment.refusal_reason !== 'Transaction refused'
    || [B_PAYLOAD, S_PAYLOAD, F_PAYLOAD]
      .some((payload) => payload.data.purchase.checkout_country.iso !== 'MX')) {
  throw new Error('the capture no longer has the cases this validator uses');
}

// ---------------------------------------------------------------------------
// a. B1 del banco con su registro, y el seguimiento de punta a punta.
// ---------------------------------------------------------------------------
const B = await refusalFor(B_PAYLOAD, at(-16));
const bPlan = await planRefusal(B.lead, B);
{
  const record = {};
  await send('b1-banco', B.lead, bPlan.scheduled_action_id, {
    beforeStart: async ({ worker, lease, now, attempt }) => {
      const args = [bPlan.scheduled_action_id, attempt.id, worker, lease];
      record.first = one((await db.query(RECORD, [...args, 'declined', null, now])).rows,
        'record').outcome;
      record.again = one((await db.query(RECORD, [...args, 'declined', null, now])).rows,
        'record again').outcome;
      record.other = (await expectError('another value for the same attempt',
        () => db.query(RECORD, [...args, 'two_refusals', null, now]),
        { code: '55000', message: 'payment_failure_first_touch_record_conflict' })).message;
      record.nonsense = (await expectError('a declined with a coupon',
        () => db.query(RECORD, [...args, 'declined', 'ATT1ABCDEF123456', now]),
        { code: '22023', message: 'invalid_payment_failure_first_touch_record' })).message;
      record.lease = (await expectError('the record with another lease',
        () => db.query(RECORD, [bPlan.scheduled_action_id, attempt.id, worker, lease + 1,
          'declined', null, now]),
        { code: '55000', message: 'current_action_lease_not_found' })).message;
      record.immutable = (await expectError('the record rewritten',
        () => db.query(`
          update public.payment_failure_first_touch_records set variant = 'two_refusals'
          where delivery_attempt_id = $1
        `, [attempt.id]),
        { code: '55000', message: 'payment_failure_first_touch_record_immutable' })).message;
      // B1 emite su link (20261019000200), como en produccion.
      const link = one((await db.query(`
        select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)
      `, [bPlan.scheduled_action_id, worker, lease, attempt.id, ulidAt(Date.now()), now])).rows,
      'B1 link');
      if (link.outcome !== 'reserved') throw new Error(`B1 link: ${JSON.stringify(link)}`);
    },
  });
  if (record.first !== 'recorded' || record.again !== 'already_recorded') {
    throw new Error(`the record: ${JSON.stringify(record)}`);
  }
  results.record = record;
}
const bFollowup = await followupOf('banco', bPlan);
{
  const sequence = one((await db.query(`
    select status, current_step from public.followup_sequences where id = $1
  `, [bPlan.followup_sequence_id])).rows, 'sequence after B1');
  const state = await caseOf(bPlan.recovery_case_id);
  if (sequence.status !== 'active' || sequence.current_step !== 1 || state.status !== 'active'
      || Number(bFollowup.expected_case_version) !== Number(state.version)) {
    throw new Error(`after B1: ${JSON.stringify({ sequence, state, followup: bFollowup })}`);
  }
}

// ---------------------------------------------------------------------------
// d. Un segundo rechazo con el seguimiento pendiente.
// ---------------------------------------------------------------------------
{
  const caseBefore = await caseOf(bPlan.recovery_case_id);
  const again = await refusalFor(B_PAYLOAD, at(-2), { suffix: 'con-seguimiento', lead: B.lead });
  const plan = await planRefusal(B.lead, again);
  const caseAfter = await caseOf(bPlan.recovery_case_id);
  const followup = await actionOf(bFollowup.id);
  const actions = one((await db.query(`
    select count(*)::integer as count from public.scheduled_actions where recovery_case_id = $1
  `, [bPlan.recovery_case_id])).rows, 'actions').count;
  if (plan.created || plan.recovery_case_id !== bPlan.recovery_case_id
      || caseAfter.count !== 2 || Number(caseAfter.version) !== Number(caseBefore.version) + 1
      || followup.status !== 'pending'
      || followup.due_at.getTime() !== bFollowup.due_at.getTime()
      || Number(followup.expected_case_version) !== Number(caseAfter.version)
      || actions !== 2) {
    throw new Error(`second refusal with the followup pending: ${JSON.stringify({ plan, caseBefore, caseAfter, followup, actions })}`);
  }
  results.second_refusal = {
    aggregated: true, due_at: 'unchanged',
    expected_case_version: `${caseBefore.version}->${caseAfter.version}`,
  };
}

// a (sigue). El cupon 2, el contexto, el link, el arranque y el cierre.
{
  await dueNow(bFollowup.id);
  const seeded = await db.query(COUPONS, [await dbNow()]);
  const coupon = await couponOf(bPlan.recovery_case_id, 2);
  const due = (await actionOf(bFollowup.id)).due_at;
  if (coupon === null || !seeded.rows.some((row) => row.recovery_case_id === bPlan.recovery_case_id)
      || coupon.expires_at.getTime() !== due.getTime() + 24 * 3_600_000
      || (await couponOf(bPlan.recovery_case_id, 1)) !== null) {
    throw new Error(`coupon 2 of the followup: ${JSON.stringify({ coupon, seeded: seeded.rows })}`);
  }
  // d (control): con la version vieja, lo que pasaba antes, se pausa.
  const stale = await rolledBack(async () => {
    await db.query(`
      update public.scheduled_actions set expected_case_version = expected_case_version - 1
      where id = $1
    `, [bFollowup.id]);
    const fence = await claim('version-vieja', bFollowup.id);
    return reevaluate(bFollowup.id, fence, evidence(B.lead, fence.now));
  });
  if (stale.decision !== 'pause' || stale.reason_code !== 'authoritative_state_changed') {
    throw new Error(`the stale version did not pause: ${JSON.stringify(stale)}`);
  }
  results.second_refusal.control_stale_version = `${stale.decision}:${stale.reason_code}`;

  let context = null;
  let link = null;
  const sent = await send('seguimiento-banco', B.lead, bFollowup.id, {
    chatwoot: (now) => evidence(B.lead, now),
    beforeStart: async ({ worker, lease, now, attempt }) => {
      context = one((await db.query(CONTEXT, [bFollowup.id, worker, lease, now])).rows,
        'followup context');
      link = one((await db.query(`
        select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)
      `, [bFollowup.id, worker, lease, attempt.id, ulidAt(Date.now()), now])).rows,
      'followup link');
      // Ni el registro ni el contexto valen para la accion equivocada.
      await expectError('a record on the followup', () => db.query(RECORD,
        [bFollowup.id, attempt.id, worker, lease, 'declined', null, now]),
      { code: '55000', message: 'payment_failure_action_required' });
    },
  });
  if (context.followup_kind !== 'second_touch' || context.first_touch_variant !== 'declined'
      || context.payment_failure_count !== 2) {
    throw new Error(`followup context: ${JSON.stringify(context)}`);
  }
  if (link.outcome !== 'reserved'
      || !link.checkout_url_final?.startsWith(`https://pay.hotmart.com/${ATT1.hotlink}`)
      || !link.checkout_url_final.includes(`off=${B.offer}`)) {
    throw new Error(`followup link: ${JSON.stringify(link)}`);
  }
  const sequence = one((await db.query(`
    select status, completion_reason, automatic_messages_accepted
    from public.followup_sequences where id = $1
  `, [bPlan.followup_sequence_id])).rows, 'sequence after the followup');
  const state = await caseOf(bPlan.recovery_case_id);
  const actions = one((await db.query(`
    select count(*)::integer as count from public.scheduled_actions where recovery_case_id = $1
  `, [bPlan.recovery_case_id])).rows, 'actions after the followup').count;
  if (sequence.status !== 'completed' || sequence.completion_reason !== 'policy_exhausted'
      || sequence.automatic_messages_accepted !== 2 || state.status !== 'sequence_exhausted'
      || actions !== 2 || sent.decision.decision !== 'execute') {
    throw new Error(`after the followup: ${JSON.stringify({ sequence, state, actions })}`);
  }
  results.declined = {
    window_closed: sent.decision.decision,
    context: `${context.followup_kind}/${context.first_touch_variant}/${context.payment_failure_count}`,
    coupon_2: 'due_at + 24 h',
    link: link.outcome,
    start: 'mark_portable_payment_failure_request_started',
    sequence: `${sequence.completion_reason}:${sequence.automatic_messages_accepted}`,
    case: state.status,
  };
}

// ---------------------------------------------------------------------------
// b. B1 del saldo con su cupon registrado: Z.
// ---------------------------------------------------------------------------
{
  const S = await refusalFor(S_PAYLOAD, at(-16));
  const plan = await planRefusal(S.lead, S);
  await db.query(COUPONS, [await dbNow()]);
  const own = await couponOf(plan.recovery_case_id, 1);
  if (own === null) throw new Error('B1-saldo has no coupon of its own');
  await send('b1-saldo', S.lead, plan.scheduled_action_id, {
    beforeStart: async ({ worker, lease, now, attempt }) => {
      const recorded = one((await db.query(RECORD, [plan.scheduled_action_id, attempt.id, worker,
        lease, 'insufficient_funds', own.code, now])).rows, 'saldo record').outcome;
      if (recorded !== 'recorded') throw new Error(`saldo record: ${recorded}`);
    },
  });
  const followup = await followupOf('saldo', plan);
  await dueNow(followup.id);
  const fence = await claim('seguimiento-saldo', followup.id);
  const context = one((await db.query(CONTEXT, [followup.id, fence.worker, fence.lease, fence.now]))
    .rows, 'saldo context');
  if (context.followup_kind !== 'last_touch' || context.first_touch_variant !== 'insufficient_funds'
      || context.payment_failure_count !== 1) {
    throw new Error(`saldo context: ${JSON.stringify(context)}`);
  }
  // Sin la lease de la accion no hay contexto.
  await expectError('the followup context without its lease', () => db.query(CONTEXT,
    [plan.scheduled_action_id, fence.worker, fence.lease, fence.now]),
  { code: '55000', message: 'current_action_lease_not_found' });
  // Z queda para manana: la prueba siguiente no la reclama.
  await db.query(`
    update public.scheduled_actions set due_at = now() + interval '1 day' where id = $1
  `, [followup.id]);
  results.insufficient_funds = `${context.followup_kind}/${context.first_touch_variant}`;
}

// ---------------------------------------------------------------------------
// c. B1 sin registro (la plantilla de hoy), y e. contestar lo cancela.
// ---------------------------------------------------------------------------
{
  const F = await refusalFor(F_PAYLOAD, at(-16));
  const plan = await planRefusal(F.lead, F);
  await send('b1-sin-registro', F.lead, plan.scheduled_action_id);
  const records = one((await db.query(`
    select count(*)::integer as count from public.payment_failure_first_touch_records
    where recovery_case_id = $1
  `, [plan.recovery_case_id])).rows, 'records').count;
  const followup = await followupOf('sin-registro', plan);
  await dueNow(followup.id);
  const fence = await claim('seguimiento-sin-registro', followup.id);
  const context = one((await db.query(CONTEXT, [followup.id, fence.worker, fence.lease, fence.now]))
    .rows, 'context without a record');
  if (records !== 0 || context.followup_kind !== 'second_touch'
      || context.first_touch_variant !== null) {
    throw new Error(`without a record: ${JSON.stringify({ records, context })}`);
  }
  // f. «Quiero mi 10 %» en la misma conversacion. Primero, que el pedido llega
  // al seguimiento: la conversacion del caso es la de la persona (sin esto, el
  // no_pending_last_touch de abajo no probaria nada).
  const sameConversation = one((await db.query(`
    select count(*)::integer as count
    from public.scheduled_actions action
    join public.recovery_cases recovery on recovery.id = action.recovery_case_id
    join public.conversations conversation on conversation.id = recovery.conversation_id
    where action.id = $1
      and conversation.commercial_context ->> 'chatwoot_conversation_id' = $2
  `, [followup.id, String(F.lead.chatwoot)])).rows, 'same conversation').count;
  const beforeRenewal = await actionOf(followup.id);
  const renewal = one((await db.query(`
    select * from public.request_recovery_renewal_v1($1,$2,$3,$4,$5,$6)
  `, [ATT1.accountId, ATT1.inboxId, F.lead.chatwoot, 990001, F.lead.phone, fence.now])).rows,
  'renewal');
  const afterRenewal = await actionOf(followup.id);
  const renewals = one((await db.query(`
    select count(*)::integer as count from public.recovery_renewal_requests where action_id = $1
  `, [followup.id])).rows, 'renewals').count;
  if (sameConversation !== 1 || renewal.outcome !== 'no_pending_last_touch' || renewals !== 0
      || afterRenewal.due_at.getTime() !== beforeRenewal.due_at.getTime()) {
    throw new Error(`the cart renewal reached the follow-up: ${JSON.stringify({ sameConversation, renewal, renewals })}`);
  }
  results.cart_renewal = `${renewal.outcome}; the follow-up is untouched`;
  const replied = await reevaluate(followup.id, fence, evidence(F.lead, fence.now, { replied: true }));
  const cancelled = await actionOf(followup.id);
  if (replied.decision !== 'cancel' || replied.reason_code !== 'prospect_replied'
      || cancelled.status !== 'cancelled') {
    throw new Error(`a reply to B1: ${JSON.stringify({ replied, status: cancelled.status })}`);
  }
  results.without_record = `${context.followup_kind}/${context.first_touch_variant}`;
  results.replied = `${replied.decision}:${replied.reason_code}`;
}

console.log(JSON.stringify(results, null, 2));
