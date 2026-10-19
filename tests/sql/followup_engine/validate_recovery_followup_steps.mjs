// Los pasos siguientes con plantilla aprobada y cupon (A2 y Z del carrito,
// migracion 20261019000600), con las RPC reales sobre la cadena completa.
//
// Prueba:
//   0. la migracion: cambia exactamente cinco funciones (la reevaluacion, la
//      adopcion de la respuesta, la siembra del cupon, la emision del link y
//      la planificacion del carrito), cada una es su definicion anterior con
//      sus textos reemplazados y nada mas; agrega tres (el paso de la
//      politica, el pedido de renovacion y su lectura), y correrla dos veces
//      aborta sin tocar nada;
//   1. A2 sale con la ventana cerrada: al aceptarse A1 el motor inserta
//      cart_second_touch como no_reply_review (due_at = la aceptacion de A1 +
//      su delay); con la evidencia de una plantilla sin respuesta
//      (can_reply = false) la reevaluacion da execute, abierta o resuelta, y
//      en espera (snoozed) pausa; arranca por mark_lancemos_pilot_request_started
//      y al aceptarse entra cart_last_touch. Control: la misma politica sin las
//      marcas pausa A2 (chatwoot_conversation_blocked), como antes;
//   2. la adopcion de la respuesta (admit_portable_inbound_commercial_case_v1)
//      no se traba con A2 esperando; sin las marcas, la frena 22000;
//   3. el cupon 2: con Z dentro de p_lead, claim_due_recovery_coupons_v1
//      siembra ordinal 2 con expires_at = due_at de Z + p_validity, una
//      segunda corrida no lo duplica, y A2 (sin coupon_ordinal) no siembra;
//   4. el link: reserve_recovery_checkout_issuance_v1 da reserved para Z con
//      el marcador hermes~v1~, y rechaza A2 con 22023; Z sale y la secuencia
//      termina con policy_exhausted (tres toques);
//   5. la renovacion: request_recovery_renewal_v1 adelanta Z (advanced, due_at
//      <= now()), el mismo mensaje u otro dan already_requested, sin Z (con A2
//      pendiente, u otra conversacion) da no_pending_last_touch, una entrada
//      invalida 22023, y la reevaluacion de Z con un entrante despues del
//      ancla da execute; sin el pedido, la misma evidencia cancela Z con
//      prospect_replied;
//   6. un carrito nuevo de la misma persona (plan_cart_recovery) no adelanta
//      A2; un primer contacto pendiente si se adelanta. Por la puerta del
//      piloto (plan_lancemos_pilot_cart_recovery) el segundo carrito aborta
//      antes con pilot_case_binding_conflict, y eso tambien se fija.
//
// Datos: el armado de validate_att1_portable_chain.mjs (el manifiesto de
// ATT1, el formulario con consentimiento, el carrito capturado, la cohorte
// del piloto) con una politica del carrito de tres pasos publicada en la
// prueba, la del contrato, y su gemela sin las marcas, cada una con su scope
// del piloto. Desviaciones de produccion: los delays son de 1 y 2 segundos
// (en produccion, 24 y 72 horas), para no esperar; la ventana es de 00:00 a
// 23:59 (la puerta de arranque mira el reloj real); la gracia es 0 (la de la
// politica de un toque de ATT1); los topes del scope son 20 contactos y 50
// arranques; y cuando una prueba necesita un paso que "espera su dia" (A2 o
// Z con due_at en el futuro: la adopcion, la renovacion, el carrito nuevo) su
// due_at se corre a manana con un update, porque con los delays cortos
// venceria en segundos. El claim toma todo lo vencido, asi que las pruebas
// dejan vencida una sola accion a la vez.
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

const results = {};

// ---------------------------------------------------------------------------
// 0. La migracion.
// ---------------------------------------------------------------------------
const TARGET = '20261019000600_recovery_followup_steps_v1.sql';
const migrationNames = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const targetIndex = migrationNames.indexOf(TARGET);
if (targetIndex < 0) throw new Error(`${TARGET} is missing from the canonical stack`);
const apply = async (file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
));
const definitions = async () => new Map((await db.query(`
  select p.oid::regprocedure::text as signature, pg_get_functiondef(p.oid) as definition
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
  where n.nspname = 'public'
`)).rows.map((row) => [row.signature, row.definition]));

await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of migrationNames.slice(0, targetIndex)) {
  await apply(join(root, 'supabase/migrations', name));
}
const before = await definitions();
await apply(join(root, 'supabase/migrations', TARGET));
const after = await definitions();
for (const name of migrationNames.slice(targetIndex + 1)) {
  await apply(join(root, 'supabase/migrations', name));
}

const TZ = 'timestamp with time zone';
const CHANGED = {
  [`reevaluate_followup_action(uuid,text,bigint,${TZ},boolean,text,text,${TZ},text,boolean,boolean,boolean,boolean,boolean)`]: [
    "    elsif p_chatwoot_inbound_after_anchor then\n",
    "                     and slip_sequence.reason = 'payment_slip'\n               )\n           )\n       ) then\n",
  ],
  'admit_portable_inbound_commercial_case_v1(text,integer,bigint,text)': [
    `${' '.repeat(29)}-- portable_payment_slip_reminders: end\n`,
  ],
  [`claim_due_recovery_coupons_v1(${TZ},text,integer,text,interval,interval,numeric,integer)`]: [
    '    order by due.due_at\n    limit p_limit\n    on conflict do nothing;\n',
  ],
  [`reserve_recovery_checkout_issuance_v1(uuid,text,bigint,uuid,text,${TZ})`]: [
    "    if v_action.action_type <> 'first_contact_review'\n"
      + '       -- payment_failure_variants: tambien el primer toque del pago rechazado.\n'
      + '       or v_action.anchor_type is null\n'
      + "       or v_action.anchor_type not in ('cart_abandonment', 'payment_failure')\n",
  ],
  [`plan_cart_recovery(uuid,uuid,text,text,text,text,integer,${TZ})`]: [
    "                    when v_sequence_status = 'active' then least(due_at, now())\n",
    "                    when v_sequence_status = 'active' then null\n",
  ],
};
const ADDED = [
  '_recovery_followup_step(uuid)',
  '_recovery_renewal_requested(uuid)',
  `request_recovery_renewal_v1(bigint,bigint,bigint,bigint,text,${TZ})`,
];
const changed = [...after.keys()].filter((signature) => before.has(signature)
  && before.get(signature) !== after.get(signature)).sort();
const added = [...after.keys()].filter((signature) => !before.has(signature)).sort();
const removed = [...before.keys()].filter((signature) => !after.has(signature));
if (JSON.stringify(changed) !== JSON.stringify(Object.keys(CHANGED).sort())
    || JSON.stringify(added) !== JSON.stringify([...ADDED].sort())
    || removed.length !== 0) {
  throw new Error(`the migration changed other functions: ${JSON.stringify({ changed, added, removed })}`);
}
// Cada una conserva todo lo que habia fuera de los textos reemplazados: antes
// del primero y despues del ultimo, la definicion es la misma.
for (const [signature, olds] of Object.entries(CHANGED)) {
  const previous = before.get(signature);
  const current = after.get(signature);
  for (const old of olds) {
    if (previous.split(old).length !== 2) {
      throw new Error(`${signature}: the replaced text is not there once`);
    }
  }
  const head = previous.split(olds[0])[0];
  const tail = previous.split(olds[olds.length - 1])[1];
  if (!current.startsWith(head) || !current.endsWith(tail)
      || !current.includes('recovery_followup_steps')) {
    throw new Error(`${signature} is not the previous definition with its texts replaced`);
  }
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
if (rerun === null || rerun.code !== '42P13' && rerun.code !== '42723' && rerun.code !== '55000') {
  throw new Error(`a second run did not abort: ${rerun ? `${rerun.code} ${rerun.message}` : 'success'}`);
}
// Los permisos de lo nuevo: solo service_role ejecuta, y nadie lee la tabla.
const grants = (await db.query(`
  select
    bool_and(has_function_privilege('service_role', f, 'EXECUTE')) as service_role,
    bool_or(has_function_privilege('anon', f, 'EXECUTE')
            or has_function_privilege('authenticated', f, 'EXECUTE')) as public_roles,
    bool_or(has_table_privilege(r, 'public.recovery_renewal_requests', 'SELECT')) as table_read
  from unnest(array[
    'public._recovery_followup_step(uuid)',
    'public._recovery_renewal_requested(uuid)',
    'public.request_recovery_renewal_v1(bigint,bigint,bigint,bigint,text,timestamptz)'
  ]::regprocedure[]) as f
  cross join unnest(array['anon', 'authenticated', 'service_role']) as r
`)).rows[0];
if (!grants.service_role || grants.public_roles || grants.table_read) {
  throw new Error(`unexpected grants: ${JSON.stringify(grants)}`);
}
results.migration = {
  changed: changed.length, added: added.length, rerun: rerun.code,
};

// ---------------------------------------------------------------------------
// El armado de ATT1: el manifiesto, el binding, la intencion con
// consentimiento y el carrito (copiado de validate_att1_portable_chain.mjs).
// ---------------------------------------------------------------------------
const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
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
const PILOT = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/instances/att1/politica-piloto.json'), 'utf8',
));
const cartScope = required(PILOT.pilot_scope, 'politica-piloto.pilot_scope');
const cartPolicy = required(PILOT.policy, 'politica-piloto.policy');
const CHANNEL_PROVIDER = required(cartScope.channel_provider, 'pilot_scope.channel_provider');
const CHANNEL_REF = `${required(cartScope.channel_account_ref_prefix, 'pilot_scope.channel_account_ref_prefix')}${ATT1.inboxId}`;
const ALL_DAY = required(cartPolicy.business_windows, 'policy.business_windows')
  .map((window) => ({ ...window, start: '00:00', end: '23:59' }));

// La politica del carrito con A2 y Z (la del contrato, con los delays cortos
// de la cabecera) y su control: los mismos pasos sin las dos marcas. Cada una
// con su scope del piloto, porque el scope fija la politica.
const MARKED = {
  scope: 'att1-carrito-a2-z',
  policy: 'att1-carrito-tres-toques',
  steps: [
    { step_key: 'first_contact', mode: 'freeform' },
    { step_key: 'cart_second_touch', mode: 'freeform', delay: '1 second', approved_template: true },
    {
      step_key: 'cart_last_touch', mode: 'freeform', delay: '2 seconds',
      approved_template: true, coupon_ordinal: 2,
    },
  ],
};
const UNMARKED = {
  scope: 'att1-carrito-sin-marcas',
  policy: 'att1-carrito-tres-toques-sin-marcas',
  steps: MARKED.steps.map(({ approved_template: _a, coupon_ordinal: _c, ...step }) => step),
};

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
}
await db.query(`
  insert into public.commercial_ally_hotmart_purchase_policies
    (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
  values ($1,$2,$3,true,$4::interval)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion,
  required(PILOT.purchase_policy?.max_lookback, 'purchase_policy.max_lookback')]);
// El scope entrante (la adopcion) y su catalogo (el link), como los siembra
// aprovisionar-att1.sql.
await db.query(`
  insert into public.inbound_commercial_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     external_product_id, offer_code, approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5,$6,$7,'operator-test',now(),now())
`, [ATT1.inboundScope, ATT1.inboundVersion, ATT1.tenant, ATT1.accountId, ATT1.inboxId,
  ATT1.hotlink, defaultOffer.offer_code]);
for (const offer of offers) {
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
for (const flow of [MARKED, UNMARKED]) {
  await db.query(`
    insert into public.followup_policy_versions
      (policy_key, version, status, purpose, timezone, business_windows,
       grace_period, expires_after, max_automatic_messages, steps,
       approved_by, approved_at, published_at)
    values ($1,1,'published','cart_recovery',$2,$3::jsonb,interval '0 seconds',
            interval '5 days',3,$4::jsonb,'operator-test',now(),now())
  `, [flow.policy, ATT1.timezone, JSON.stringify(ALL_DAY), JSON.stringify(flow.steps)]);
  await db.query(`
    insert into public.pilot_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
       channel, channel_provider, channel_account_ref, source, source_event_type,
       additional_source_event_types, external_product_id, offer_code,
       additional_offer_codes, purpose, policy_key, policy_version, timezone,
       max_cohort_contacts, max_outbound_request_starts_total,
       max_outbound_request_starts_per_day, approved_by, approved_at, published_at)
    values ($1,1,'published',$2,$3,$4,'whatsapp',$5,$6,'hotmart',
            'PURCHASE_OUT_OF_SHOPPING_CART','{}'::text[],$7,$8,$9::text[],
            'cart_recovery',$10,1,$11,20,50,50,'operator-test',now(),now())
  `, [flow.scope, ATT1.tenant, ATT1.accountId, ATT1.inboxId, CHANNEL_PROVIDER, CHANNEL_REF,
    String(ATT1.productId), defaultOffer.offer_code,
    additionalOffers.map((offer) => offer.offer_code), flow.policy, ATT1.timezone]);
  await db.query(`
    insert into public.pilot_runtime_controls
      (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
    values ($1,1,'inactive',0,'operator-test','default-off')
  `, [flow.scope]);
  await db.query(`
    select * from public.set_lancemos_pilot_runtime_state($1,1,0,'armed','operator-test','controlled-test')
  `, [flow.scope]);
}

const dbNow = async () => new Date((await db.query('select clock_timestamp() as now')).rows[0].now);
const baseMs = Math.floor((await dbNow()).getTime() / 1000) * 1000;
const at = (minutes) => new Date(baseMs + minutes * 60_000);
const CAPTURED_CART = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/hotmart_cart_abandonment_rejected_v1.json'), 'utf8',
)).payload;
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
const expectError = async (label, run, { code, message, detail } = {}) => {
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
      || (message !== undefined && error.message !== message)
      || (detail !== undefined && error.detail !== detail)) {
    throw new Error(`${label}: expected ${code} ${message} ${detail ?? ''}, got ${error ? `${error.code} ${error.message} ${error.detail}` : 'success'}`);
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
const asService = async (run) => {
  await db.exec('set role service_role');
  try {
    return await run();
  } finally {
    await db.exec('reset role');
  }
};

let personIndex = 0;
const person = (label, flow) => {
  personIndex += 1;
  const suffix = String(personIndex).padStart(2, '0');
  return {
    label,
    flow,
    name: `Compradora A2 Z ${label}`,
    email: `att1-a2z-${suffix}@example.test`,
    phone: `120255502${suffix}`,
    offer: additionalOffers[0],
    chatwoot: String(920000 + personIndex),
  };
};
// El formulario 1.1.0 con consentimiento (precedente inline de la cadena).
const admitForm = async (lead) => {
  const { offer } = lead;
  const id = `att1-a2z-form-${lead.email}`;
  const submittedAt = at(-50).toISOString();
  const pageUrl = `https://${offer.page_host}${offer.page_path}`;
  const checkoutUrl = `https://pay.hotmart.com/${ATT1.hotlink}?off=${offer.offer_code}&checkoutMode=10`;
  const raw = {
    id,
    event: 'lead.precheckout',
    version: '1.1.0',
    created_at: submittedAt,
    source: {
      system: 'landing', site: offer.site, aliado: ATT1.brand,
      landing_id: offer.landing_id, page_url: pageUrl,
    },
    data: {
      buyer: {
        name: lead.name, email: lead.email, phone: `+${lead.phone}`,
        phone_country_code: '1', phone_national: lead.phone.slice(1),
      },
      product: {
        hotlink: ATT1.hotlink, id: null, name: ATT1.productName,
        price: Number(ATT1.price), currency: ATT1.currency,
      },
      offer: { code: offer.offer_code },
      checkout_url: checkoutUrl,
      checkout_country: { iso: 'US', source: 'phone_country_code' },
      consent: { marketing_optin: true, whatsapp_contact: true, copy_version: ATT1.copyVersion },
    },
    dedupe_key: `${offer.site}:${offer.offer_code}:${lead.email}`,
  };
  const canonical = {
    external_submission_id: id,
    event_type: 'PRECHECKOUT_FORM_SUBMITTED',
    contract_version: '1.1.0',
    submitted_at: submittedAt,
    source: {
      tenant_ref: ATT1.tenant, funnel_ref: ATT1.funnel, landing_ref: offer.landing_id,
      page_url: pageUrl, aliado: ATT1.brand,
    },
    identity: { email: lead.email, phone: lead.phone, phone_valid: true, phone_country_iso: 'US' },
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
    JSON.stringify(canonical)])).rows, `${lead.label} form`);
  if (admitted.outcome !== 'inserted') throw new Error(`${lead.label} form: ${admitted.outcome}`);
};
// El carrito capturado, con producto, oferta, id, fecha y comprador sustituidos.
const admitCart = async (lead, number, createdAt) => {
  const payload = structuredClone(CAPTURED_CART);
  payload.id = `att1-a2z-cart-${number}-${lead.email}`;
  payload.creation_date = createdAt.getTime();
  payload.data.product = { id: ATT1.productId, name: ATT1.productName };
  payload.data.offer = { code: lead.offer.offer_code };
  payload.data.buyer = { name: lead.name, email: lead.email, phone: lead.phone };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
    JSON.stringify(payload), lead.email, lead.phone])).rows, `${lead.label} cart ${number}`);
  if (admitted.outcome !== 'inserted') throw new Error(`${lead.label} cart ${number}: ${admitted.outcome}`);
  return { eventId: admitted.webhook_event_id, abandonedAt: createdAt };
};
const planCart = async (lead, cart) => one((await db.query(`
  select * from public.plan_lancemos_pilot_cart_recovery(
    $1,$2,$3,$4,$5,$6,1,$7,$8,$9,$10,$11,1
  )
`, [cart.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  lead.offer.offer_code, lead.flow.policy, cart.abandonedAt.toISOString(), ATT1.accountId,
  ATT1.inboxId, lead.phone, lead.flow.scope])).rows, `${lead.label} plan`);
// Formulario, carrito, contacto (lo que deja resolve_event), cohorte y plan.
const start = async (label, flow) => {
  const lead = person(label, flow);
  await admitForm(lead);
  const cart = await admitCart(lead, 1, at(-30));
  lead.contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone, country_iso)
    values ($1,$2,$3,'MX') returning id
  `, [lead.name, lead.email, lead.phone])).rows, `${label} contact`).id;
  await db.query(`
    insert into public.contact_points
      (contact_id, type, raw_value, normalized_value, source, source_event_id)
    values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)
  `, [lead.contact, lead.email, lead.phone, cart.eventId]);
  const { generation } = one((await db.query(`
    select generation from public.pilot_runtime_controls where scope_key = $1
  `, [flow.scope])).rows, 'scope generation');
  const member = one((await db.query(`
    select * from public.set_lancemos_pilot_cohort_member($1,1,$2,$3,'active','operator-test','controlled-test')
  `, [flow.scope, lead.contact, generation])).rows, `${label} enrollment`);
  if (member.member_status !== 'active') throw new Error(`${label} enrollment: ${JSON.stringify(member)}`);
  lead.plan = await planCart(lead, cart);
  if (!lead.plan.created) throw new Error(`${label} plan: ${JSON.stringify(lead.plan)}`);
  return lead;
};

const actionOf = async (id) => one((await db.query(`
  select * from public.scheduled_actions where id = $1
`, [id])).rows, 'action');
const stepOf = async (lead, stepKey) => {
  const rows = (await db.query(`
    select id from public.scheduled_actions
    where recovery_case_id = $1 and step_key = $2
  `, [lead.plan.recovery_case_id, stepKey])).rows;
  return actionOf(one(rows, `${lead.label} ${stepKey}`).id);
};
// Lo que el dispatcher arma de Chatwoot antes de un no_reply_review: despues
// de una plantilla sin respuesta la ventana de 24 h esta cerrada
// (can_reply = false); una respuesta la abre.
const evidence = (lead, now, { replied = false, status = 'open' } = {}) => ({
  p_chatwoot_checked: true,
  p_chatwoot_conversation_id: lead.chatwoot,
  p_chatwoot_checkpoint_message_id: '1',
  p_chatwoot_checkpoint_at: now.toISOString(),
  p_chatwoot_status: status,
  p_chatwoot_can_reply: replied,
  p_chatwoot_anchor_found: true,
  p_chatwoot_automation_paused: false,
  p_chatwoot_inbound_after_anchor: replied,
  p_chatwoot_human_activity_after_anchor: false,
});
const reevaluate = async (actionId, worker, lease, now, chatwoot) => {
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
// El claim toma todo lo vencido: las pruebas dejan vencida una sola accion a
// la vez, y se exige que sea la esperada.
const claim = async (lead, actionId) => {
  const worker = `a2z-worker-${lead.label}`;
  const now = await dbNow();
  const claimed = (await db.query(`
    select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
  `, [worker, now])).rows;
  if (claimed.length !== 1 || claimed[0].id !== actionId) {
    throw new Error(`${lead.label}: claimed ${JSON.stringify(claimed.map((row) => [row.id, row.step_key]))}, expected ${actionId}`);
  }
  return { worker, now, lease: claimed[0].lease_generation };
};
const reserveAttempt = async (actionId, { worker, lease, now }, decision) => one((await db.query(`
  select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
`, [actionId, worker, lease, decision.case_version, decision.sequence_revision, now])).rows,
'attempt reservation');
// El arranque del carrito (el flujo de cada paso del carrito es
// cart_abandonment) y la aceptacion, en la conversacion de A1.
let messageNumber = 0;
const startAndAccept = async (lead, actionId, attempt, { worker, lease }) => {
  const started = one((await db.query(`
    select * from public.mark_lancemos_pilot_request_started($1,$2,$3,$4,$5)
  `, [actionId, attempt.id, worker, lease, await dbNow()])).rows, `${lead.label} start`);
  if (started.phase !== 'request_started' || started.pilot_authorization_id == null) {
    throw new Error(`${lead.label} start: ${JSON.stringify(started)}`);
  }
  messageNumber += 1;
  const accepted = one((await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
  `, [actionId, attempt.id, worker, lease, lead.chatwoot, `a2z-wamid-${messageNumber}`,
    `Plantilla aprobada ${messageNumber} del carrito`, await dbNow()])).rows,
  `${lead.label} acceptance`);
  if (accepted.status !== 'accepted_by_chatwoot') {
    throw new Error(`${lead.label} acceptance: ${JSON.stringify(accepted)}`);
  }
  return started;
};
// Un toque de punta a punta: claim, reevaluacion, intento, arranque y
// aceptacion. A1 sale sin evidencia de Chatwoot (no hay conversacion).
const send = async (lead, actionId, { chatwoot = null, beforeStart } = {}) => {
  const fence = await claim(lead, actionId);
  const decision = await reevaluate(actionId, fence.worker, fence.lease, fence.now,
    chatwoot ? chatwoot(fence.now) : null);
  if (decision.decision !== 'execute' || decision.reason_code !== 'eligible_for_execution') {
    throw new Error(`${lead.label} reevaluation: ${JSON.stringify(decision)}`);
  }
  const attempt = await reserveAttempt(actionId, fence, decision);
  if (beforeStart) await beforeStart({ ...fence, attempt });
  await startAndAccept(lead, actionId, attempt, fence);
  return { ...fence, attempt, decision };
};
const sendA1 = (lead) => send(lead, lead.plan.scheduled_action_id);
// El paso siguiente que inserta el motor al aceptarse un toque: un
// no_reply_review anclado al mensaje aceptado, con la conversacion del caso, el
// vencimiento de A1 y due_at = la aceptacion de A1 + el delay del paso.
const nextStep = async (lead, stepKey, delayMs) => {
  const action = await stepOf(lead, stepKey);
  const a1 = await actionOf(lead.plan.scheduled_action_id);
  const firstAccepted = one((await db.query(`
    select min(attempt.accepted_at) as at
    from public.followup_delivery_attempts attempt
    join public.scheduled_actions action on action.id = attempt.action_id
    where action.followup_sequence_id = $1
  `, [lead.plan.followup_sequence_id])).rows, 'first acceptance').at;
  if (action.action_type !== 'no_reply_review' || action.status !== 'pending'
      || action.anchor_type !== 'accepted_outbound_message' || action.conversation_id === null
      || action.expires_at.getTime() !== a1.expires_at.getTime()
      || action.due_at.getTime() !== firstAccepted.getTime() + delayMs) {
    throw new Error(`${lead.label} ${stepKey}: ${JSON.stringify({ action, firstAccepted })}`);
  }
  return action;
};
const sleepUntil = async (date) => {
  const wait = date.getTime() - (await dbNow()).getTime() + 150;
  if (wait > 0) await sleep(wait);
};
// Una accion que espera "su dia": se corre a manana, como A2 a las 24 h o Z a
// las 72 h de produccion (con los delays cortos venceria en segundos).
const postpone = (actionId) => db.query(`
  update public.scheduled_actions set due_at = now() + interval '1 day' where id = $1
`, [actionId]);
const externalUserOf = async (lead) => one((await db.query(`
  select identity.external_user_id
  from public.recovery_cases recovery
  join public.channel_identities identity on identity.id = recovery.selected_channel_identity_id
  where recovery.id = $1
`, [lead.plan.recovery_case_id])).rows, `${lead.label} identity`).external_user_id;
const ADMISSION = 'select * from public.admit_portable_inbound_commercial_case_v1($1,$2,$3,$4)';
// El segundo carrito de la misma persona, producto y oferta, por
// plan_cart_recovery (el planificador que cambia la migracion). Por la puerta
// del piloto de ATT1 (plan_lancemos_pilot_cart_recovery) no llega: el binding
// del caso guarda el primer evento y el segundo aborta con
// pilot_case_binding_conflict (se fija abajo).
const replanCart = async (lead, cart) => one((await db.query(`
  select * from public.plan_cart_recovery($1,$2,$3,$4,$5,$6,1,$7)
`, [cart.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  lead.offer.offer_code, lead.flow.policy, cart.abandonedAt.toISOString()])).rows,
`${lead.label} second plan`);

// ---------------------------------------------------------------------------
// 1. A2 sale con la ventana cerrada, y Z entra al aceptarse A2.
// ---------------------------------------------------------------------------
const main = await start('principal', MARKED);
{
  await sendA1(main);
  const sequence = one((await db.query(`
    select status, reason, current_step from public.followup_sequences where id = $1
  `, [main.plan.followup_sequence_id])).rows, 'main sequence after A1');
  if (sequence.status !== 'active' || sequence.reason !== 'cart_abandonment'
      || sequence.current_step !== 1) {
    throw new Error(`main sequence after A1: ${JSON.stringify(sequence)}`);
  }
  main.a2 = await nextStep(main, 'cart_second_touch', 1000);
}
// 3 (antes de que A2 salga): un paso sin coupon_ordinal no siembra el cupon 2.
const COUPON_ARGS = (now) => [now, 'a2z-coupons', 60, 'ATT', '45 minutes', '24 hours', 0.1, 100];
const CLAIM_COUPONS = `
  select * from public.claim_due_recovery_coupons_v1(
    $1::timestamptz,$2,$3,$4,$5::interval,$6::interval,$7::numeric,$8
  )
`;
const couponsOf = async (lead) => (await db.query(`
  select ordinal, expires_at, status from public.recovery_coupons
  where recovery_case_id = $1 order by ordinal
`, [lead.plan.recovery_case_id])).rows;
const a2Coupons = await rolledBack(async () => {
  await db.query(CLAIM_COUPONS, COUPON_ARGS(await dbNow()));
  return couponsOf(main);
});
if (a2Coupons.some((row) => row.ordinal === 2)) {
  throw new Error(`a step without coupon_ordinal seeded coupon 2: ${JSON.stringify(a2Coupons)}`);
}
let a2Checkout = null;
{
  await sleepUntil(main.a2.due_at);
  const fence = await claim(main, main.a2.id);
  const variants = {};
  for (const [label, options] of [
    ['resolved', { status: 'resolved' }],
    ['snoozed', { status: 'snoozed' }],
  ]) {
    variants[label] = await rolledBack(() => reevaluate(main.a2.id, fence.worker, fence.lease,
      fence.now, evidence(main, fence.now, options)));
  }
  if (variants.resolved.decision !== 'execute'
      || variants.snoozed.decision !== 'pause'
      || variants.snoozed.reason_code !== 'chatwoot_conversation_blocked') {
    throw new Error(`A2 variants: ${JSON.stringify(variants)}`);
  }
  const decision = await reevaluate(main.a2.id, fence.worker, fence.lease, fence.now,
    evidence(main, fence.now));
  if (decision.decision !== 'execute' || decision.reason_code !== 'eligible_for_execution') {
    throw new Error(`A2 with the window closed: ${JSON.stringify(decision)}`);
  }
  const attempt = await reserveAttempt(main.a2.id, fence, decision);
  // 4 (A2 reclamado y con su intento): sin coupon_ordinal, no hay link.
  a2Checkout = await expectError('checkout link for A2', () => db.query(`
    select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)
  `, [main.a2.id, fence.worker, fence.lease, attempt.id, ulidAt(Date.now()), fence.now]),
  { code: '22023', message: 'recovery_checkout_issuance_requires_cart_first_contact' });
  await startAndAccept(main, main.a2.id, attempt, fence);
  main.z = await nextStep(main, 'cart_last_touch', 2000);
  results.a2_window_closed = {
    open_can_reply_false: decision.decision,
    resolved: variants.resolved.decision,
    snoozed: `${variants.snoozed.decision}:${variants.snoozed.reason_code}`,
    start: 'mark_lancemos_pilot_request_started',
    next: `${main.z.action_type}:${main.z.step_key}`,
  };
}

// ---------------------------------------------------------------------------
// 3. El cupon 2 de Z: dentro de p_lead se siembra con ordinal 2 y vence a
//    due_at de Z + p_validity; una segunda corrida no lo duplica.
// ---------------------------------------------------------------------------
{
  const first = await db.query(CLAIM_COUPONS, COUPON_ARGS(await dbNow()));
  const second = await db.query(CLAIM_COUPONS, COUPON_ARGS(await dbNow()));
  const coupons = (await couponsOf(main)).filter((row) => row.ordinal === 2);
  const coupon = one(coupons, 'coupon 2');
  const expected = main.z.due_at.getTime() + 24 * 3_600_000;
  if (coupon.expires_at.getTime() !== expected
      || !first.rows.some((row) => row.recovery_case_id === main.plan.recovery_case_id)) {
    throw new Error(`coupon 2: ${JSON.stringify({ coupon, expected: new Date(expected), first: first.rows })}`);
  }
  results.coupon_2 = {
    ordinal: 2,
    expires_at: 'z_due_at_plus_validity',
    second_run_rows: coupons.length,
    without_coupon_ordinal: a2Coupons.filter((row) => row.ordinal === 2).length,
    claimed_again: second.rows.filter((row) => row.recovery_case_id === main.plan.recovery_case_id).length,
  };
}

// ---------------------------------------------------------------------------
// 4. El link de Z: reserved con el marcador hermes~v1~; A2 no tiene link.
// ---------------------------------------------------------------------------
{
  await sleepUntil(main.z.due_at);
  const ulid = ulidAt(Date.now());
  let issuance = null;
  await send(main, main.z.id, {
    chatwoot: (now) => evidence(main, now),
    beforeStart: async ({ worker, lease, now, attempt }) => {
      issuance = one((await db.query(`
        select * from public.reserve_recovery_checkout_issuance_v1($1,$2,$3,$4,$5,$6)
      `, [main.z.id, worker, lease, attempt.id, ulid, now])).rows, 'Z checkout link');
    },
  });
  if (issuance.outcome !== 'reserved'
      || !issuance.checkout_url_final.startsWith(`https://pay.hotmart.com/${ATT1.hotlink}?off=${main.offer.offer_code}&`)
      || !issuance.checkout_url_final.includes(`sck=hermes~v1~${ulid}`)
      || issuance.sck_value !== `hermes~v1~${ulid}`) {
    throw new Error(`Z checkout link: ${JSON.stringify(issuance)}`);
  }
  const sequence = one((await db.query(`
    select status, completion_reason, automatic_messages_accepted
    from public.followup_sequences where id = $1
  `, [main.plan.followup_sequence_id])).rows, 'main sequence');
  if (sequence.status !== 'completed' || sequence.completion_reason !== 'policy_exhausted'
      || sequence.automatic_messages_accepted !== 3) {
    throw new Error(`main sequence: ${JSON.stringify(sequence)}`);
  }
  results.z_checkout = {
    z: issuance.outcome,
    sck: 'hermes~v1~',
    a2: `${a2Checkout.code}:${a2Checkout.message}`,
    sequence: `${sequence.completion_reason}:${sequence.automatic_messages_accepted}`,
  };
}

// ---------------------------------------------------------------------------
// 1 y 2, control: la misma politica sin las marcas. Con A2 esperando, la
// adopcion se frena (22000); con la ventana cerrada, A2 se pausa. Como antes.
// ---------------------------------------------------------------------------
const plain = await start('sin-marcas', UNMARKED);
{
  await sendA1(plain);
  const a2 = await nextStep(plain, 'cart_second_touch', 1000);
  const userId = await externalUserOf(plain);
  await expectError('adoption with an unmarked A2 waiting', async () => {
    // set local: el rollback de expectError devuelve el rol.
    await db.exec('set local role service_role');
    return db.query(ADMISSION, [ATT1.inboundScope, ATT1.inboundVersion, Number(plain.chatwoot), userId]);
  }, { code: '22000' });
  await sleepUntil(a2.due_at);
  const fence = await claim(plain, a2.id);
  const decision = await reevaluate(a2.id, fence.worker, fence.lease, fence.now,
    evidence(plain, fence.now));
  if (decision.decision !== 'pause' || decision.reason_code !== 'chatwoot_conversation_blocked') {
    throw new Error(`unmarked A2 with the window closed: ${JSON.stringify(decision)}`);
  }
  results.a2_window_closed.control_unmarked = `${decision.decision}:${decision.reason_code}`;
  results.adoption = { control_unmarked: '22000' };
}

// ---------------------------------------------------------------------------
// 2. La adopcion no se traba con A2 esperando su dia.
// ---------------------------------------------------------------------------
{
  const replier = await start('contesta-a1', MARKED);
  await sendA1(replier);
  const a2 = await nextStep(replier, 'cart_second_touch', 1000);
  await postpone(a2.id);
  const userId = await externalUserOf(replier);
  const adopted = one((await asService(() => db.query(ADMISSION,
    [ATT1.inboundScope, ATT1.inboundVersion, Number(replier.chatwoot), userId]))).rows,
  'adoption with A2 waiting');
  if (adopted.outcome !== 'created' || adopted.automation_status !== 'draft_only') {
    throw new Error(`adoption with A2 waiting: ${JSON.stringify(adopted)}`);
  }
  const stillPending = await actionOf(a2.id);
  if (stillPending.status !== 'pending') {
    throw new Error(`the adoption touched A2: ${JSON.stringify(stillPending)}`);
  }
  results.adoption.a2_pending = `${adopted.outcome}:${adopted.automation_status}`;
}

// ---------------------------------------------------------------------------
// 5. La renovacion: «Quiero mi 10 %» adelanta Z y la respuesta no la cancela.
// ---------------------------------------------------------------------------
{
  const renew = await start('renueva', MARKED);
  await sendA1(renew);
  const a2 = await nextStep(renew, 'cart_second_touch', 1000);
  const userId = await externalUserOf(renew);
  const RENEW = 'select * from public.request_recovery_renewal_v1($1,$2,$3,$4,$5,$6)';
  const request = async (messageId, { conversation = renew.chatwoot } = {}) => one((await asService(
    () => db.query(RENEW, [ATT1.accountId, ATT1.inboxId, Number(conversation), messageId, userId,
      (new Date()).toISOString()]),
  )).rows, 'renewal').outcome;
  // Con A2 pendiente (sin cupon) no hay nada que adelantar.
  const withA2 = await rolledBack(() => request(7001));
  if (withA2 !== 'no_pending_last_touch') throw new Error(`renewal with A2 pending: ${withA2}`);
  await sleepUntil(a2.due_at);
  await send(renew, a2.id, { chatwoot: (now) => evidence(renew, now) });
  const z = await nextStep(renew, 'cart_last_touch', 2000);
  await postpone(z.id);
  const postponed = await actionOf(z.id);
  if (postponed.due_at <= (await dbNow())) throw new Error('Z was not postponed');
  const advanced = await request(7002);
  const zAfter = await actionOf(z.id);
  const dbClock = await dbNow();
  if (advanced !== 'advanced' || zAfter.due_at > dbClock || zAfter.status !== 'pending') {
    throw new Error(`renewal: ${JSON.stringify({ advanced, zAfter })}`);
  }
  const repeated = await request(7002);
  const anotherMessage = await request(7003);
  if (repeated !== 'already_requested' || anotherMessage !== 'already_requested') {
    throw new Error(`renewal twice: ${JSON.stringify({ repeated, anotherMessage })}`);
  }
  const rows = one((await db.query(`
    select count(*)::integer as count from public.recovery_renewal_requests where action_id = $1
  `, [z.id])).rows, 'renewal rows').count;
  if (rows !== 1) throw new Error(`renewal rows: ${rows}`);
  // Otra conversacion de la misma persona: nada que adelantar.
  const otherConversation = await rolledBack(() => request(7004, { conversation: '999999' }));
  if (otherConversation !== 'no_pending_last_touch') {
    throw new Error(`renewal from another conversation: ${otherConversation}`);
  }
  const invalid = await expectError('invalid renewal input', async () => {
    await db.exec('set local role service_role');
    return db.query(RENEW, [ATT1.accountId, ATT1.inboxId, Number(renew.chatwoot), 7005,
      '+52 no es un numero', (new Date()).toISOString()]);
  }, { code: '22023', message: 'invalid_recovery_renewal_input' });
  // Z sale aunque la persona contesto: su respuesta fue el pedido.
  await send(renew, z.id, { chatwoot: (now) => evidence(renew, now, { replied: true }) });
  const zSent = await actionOf(z.id);
  if (zSent.status !== 'accepted_by_chatwoot') throw new Error(`renewed Z: ${JSON.stringify(zSent)}`);

  // Control: sin el pedido, la misma respuesta cancela Z.
  const control = await start('contesta-sin-pedido', MARKED);
  await sendA1(control);
  const controlA2 = await nextStep(control, 'cart_second_touch', 1000);
  await sleepUntil(controlA2.due_at);
  await send(control, controlA2.id, { chatwoot: (now) => evidence(control, now) });
  const controlZ = await nextStep(control, 'cart_last_touch', 2000);
  await sleepUntil(controlZ.due_at);
  const fence = await claim(control, controlZ.id);
  const cancelled = await reevaluate(controlZ.id, fence.worker, fence.lease, fence.now,
    evidence(control, fence.now, { replied: true }));
  if (cancelled.decision !== 'cancel' || cancelled.reason_code !== 'prospect_replied') {
    throw new Error(`Z replied without a renewal: ${JSON.stringify(cancelled)}`);
  }
  results.renewal = {
    first: advanced,
    z_due_now: true,
    same_message: repeated,
    another_message: anotherMessage,
    replied_after_renewal: 'execute',
    z_sent: zSent.status,
    control_without_request: `${cancelled.decision}:${cancelled.reason_code}`,
    with_a2_pending: withA2,
    other_conversation: otherConversation,
    invalid: `${invalid.code}:${invalid.message}`,
  };
}

// ---------------------------------------------------------------------------
// 6. Un carrito nuevo de la misma persona no adelanta A2; un primer contacto
//    pendiente si se adelanta, como hasta ahora.
// ---------------------------------------------------------------------------
{
  const again = await start('carrito-otra-vez', MARKED);
  await sendA1(again);
  const a2 = await nextStep(again, 'cart_second_touch', 1000);
  await postpone(a2.id);
  const before = await actionOf(a2.id);
  const secondCart = await admitCart(again, 2, at(-5));
  const pilotRoute = await expectError('second cart through the pilot plan',
    () => planCart(again, secondCart), { code: '55000', message: 'pilot_case_binding_conflict' });
  const replan = await replanCart(again, secondCart);
  const after = await actionOf(a2.id);
  if (replan.created || replan.recovery_case_id !== again.plan.recovery_case_id
      || replan.scheduled_action_id !== a2.id
      || after.due_at.getTime() !== before.due_at.getTime() || after.status !== 'pending') {
    throw new Error(`a new cart moved A2: ${JSON.stringify({ replan, before: before.due_at, after: after.due_at })}`);
  }

  const control = await start('primer-contacto-otra-vez', MARKED);
  const firstContact = control.plan.scheduled_action_id;
  await postpone(firstContact);
  const controlBefore = await actionOf(firstContact);
  const controlReplan = await replanCart(control, await admitCart(control, 2, at(-5)));
  const controlAfter = await actionOf(firstContact);
  const clock = await dbNow();
  if (controlReplan.created || controlReplan.scheduled_action_id !== firstContact
      || controlAfter.due_at > clock || controlAfter.due_at >= controlBefore.due_at) {
    throw new Error(`a new cart did not move the first contact: ${JSON.stringify({ controlReplan, controlBefore: controlBefore.due_at, controlAfter: controlAfter.due_at })}`);
  }
  results.new_cart = {
    a2: 'unchanged',
    control_first_contact: 'advanced',
    pilot_route: pilotRoute.message,
  };
}

console.log(JSON.stringify(results));
