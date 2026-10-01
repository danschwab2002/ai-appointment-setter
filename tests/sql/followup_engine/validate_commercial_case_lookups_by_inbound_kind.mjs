// En una conversacion adoptada por la admision portable, las cuatro RPC que
// buscan el caso de la conversacion miran solo el inbound_sales (H7, migracion
// 20261001000500).
//
// La adopcion (20261001000400) crea el inbound_sales de la respuesta en la
// conversacion de la plantilla, donde el trigger de sombra ya habia dejado el
// cart_recovery del caso de recuperacion. Hasta la 000500,
// mark_human_handoff_attended, claim_conversation_reactivation,
// resume_paused_conversation y claim_conversation_followup_v1 contaban los dos
// casos y abortaban con P0001 *_ambiguous_case.
//
// El validador arma DOS bases con la cadena completa de migraciones: una sin
// la 000500 (lo de hoy) y otra con ella. En las dos corre exactamente la misma
// secuencia, con las RPC reales de ATT1, y compara lo que devuelve cada una de
// las cuatro funciones en cinco estados de conversacion. Cada llamada corre
// como service_role (el rol del bridge) dentro de un savepoint que se deshace,
// asi que ninguna sonda cambia el estado de la siguiente.
//
//   A. adoptada, con el cart_recovery al lado del inbound_sales (la cadena del
//      dispatcher hasta la aceptacion del carrito y la respuesta por la
//      admision portable): sin la 000500 las cuatro dan ambiguous; con ella
//      ninguna, y las que devuelven el caso devuelven el inbound_sales;
//   B. adoptada, con dos inbound_sales (el operador publica una version nueva
//      del scope entrante y la misma conversacion se admite otra vez): siguen
//      dando ambiguous, en las dos bases;
//   C. NO adoptada, con un unico cart_recovery (la plantilla aceptada sin
//      respuesta: la forma de las 2 conversaciones medidas en Johanna): el
//      mismo resultado en las dos bases;
//   D. NO adoptada, con cart_recovery + inbound_sales (la persona escribe
//      mientras el envio esta en vuelo y la aceptacion cae en esa misma
//      conversacion, que ya estaba en draft_only): siguen dando ambiguous, igual
//      en las dos bases;
//   E. NO adoptada, solo el inbound_sales (la forma de casi todas las
//      conversaciones de Johanna): el mismo resultado en las dos bases.
//
// Despues, sobre A, el "Necesito ayuda" entero: la derivacion, la nota
// proyectada, la marca de atencion de la persona del equipo, la reanudacion
// por el macro y la readmision. Sin la 000500 la conversacion queda con el
// equipo para siempre (P0001); con ella vuelve al agente.
//
// Datos: binding, ofertas, landings, producto, Chatwoot, equipo de
// derivacion, plantilla y consentimiento de
// tests/fixtures/instances/att1/instancia.toml; politica y scope del piloto de
// tests/fixtures/instances/att1/politica-piloto.json, con la misma desviacion
// documentada de la ventana de envio que validate_att1_portable_chain.mjs
// (00:00 a 23:59). Carrito: la captura
// tests/fixtures/hotmart_cart_abandonment_rejected_v1.json con producto,
// oferta, id, fecha y comprador sustituidos. El formulario usa el precedente
// inline de validate_att1_portable_chain.mjs. Los telefonos son sinteticos y
// el texto aceptado es un marcador: la base guarda el que le pasa el bridge.
//
// Lo que se simula y por que: la nota de la derivacion pasa a 'projected' con
// un update, que es lo que deja el proyector de notas (igual que
// validate_handoff_attendance.mjs). Todo lo demas sale de las RPC.
import { PGlite } from '@electric-sql/pglite';
import { randomBytes } from 'node:crypto';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { isDeepStrictEqual } from 'node:util';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION = '20261001000500_commercial_case_lookups_by_inbound_kind.sql';
if (!existsSync(join(root, 'supabase/migrations', MIGRATION))) {
  throw new Error(`${MIGRATION} does not exist`);
}
const MIGRATIONS = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const ADOPTION_EVENT = 'inbound_adopted_template_conversation';
const RPCS = {
  mark_human_handoff_attended: 'public.mark_human_handoff_attended(bigint,timestamptz,timestamptz)',
  claim_conversation_reactivation: 'public.claim_conversation_reactivation(bigint,text,text,text,text,bigint,integer,integer,integer,timestamptz)',
  resume_paused_conversation: 'public.resume_paused_conversation(bigint,text,text,integer,integer,timestamptz)',
  claim_conversation_followup_v1: 'public.claim_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)',
};
const AMBIGUOUS = {
  mark_human_handoff_attended: 'P0001 mark_human_handoff_attended_ambiguous_case',
  claim_conversation_reactivation: 'P0001 claim_conversation_reactivation_ambiguous_case',
  resume_paused_conversation: 'P0001 resume_paused_conversation_ambiguous_case',
  claim_conversation_followup_v1: 'P0001 claim_conversation_followup_ambiguous_case',
};

// Las dos bases: la cadena completa, con y sin la 000500. Los roles de la API
// con el execute por defecto de Supabase, como validate_acl_hardening.mjs.
const openDatabase = async (files) => {
  const db = new PGlite();
  await db.waitReady;
  await db.exec(`
    create role anon nologin;
    create role authenticated nologin;
    create role service_role nologin;
    grant usage on schema public to service_role;
    alter default privileges grant execute on functions to anon, authenticated;
  `);
  for (const file of [
    join(root, 'supabase/baseline/20260803_public_schema.sql'),
    ...files.map((name) => join(root, 'supabase/migrations', name)),
  ]) {
    await db.exec(readFileSync(file, 'utf8').replace(
      /create extension if not exists pgcrypto;/gi,
      '-- pgcrypto is built into PGlite',
    ));
  }
  return db;
};

// ---------------------------------------------------------------------------
// El manifiesto de ATT1. Lector minimo del subconjunto de TOML que usan los
// campos de aca (el mismo de validate_att1_portable_chain.mjs).
// ---------------------------------------------------------------------------
const manifestText = readFileSync(join(root, 'tests/fixtures/instances/att1/instancia.toml'), 'utf8');
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
const manifest = readManifest(manifestText);
const required = (value, label) => {
  if (value === undefined || value === null || value === '') {
    throw new Error(`instancia.toml de ATT1 sin ${label}`);
  }
  return value;
};
const offers = required(manifest.hotmart?.ofertas, 'hotmart.ofertas').map((offer) => {
  const url = new URL(required(offer.url, 'ofertas.url'));
  return {
    offer_code: required(offer.codigo, 'ofertas.codigo'),
    site: required(offer.site, 'ofertas.site'),
    landing_id: required(offer.landing_id, 'ofertas.landing_id'),
    page_host: url.host,
    page_path: url.pathname,
    default: offer.por_defecto === true,
  };
});
if (offers.length !== 3 || !offers[0].default || offers.filter((offer) => offer.default).length !== 1) {
  throw new Error(`ATT1 declara tres ofertas y la primera es la por defecto: ${JSON.stringify(offers)}`);
}
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
  handoffTeam: required(manifest.chatwoot?.equipo_derivacion, 'chatwoot.equipo_derivacion'),
  inboundScope: required(manifest.inbound?.scope_key, 'inbound.scope_key'),
  inboundVersion: required(manifest.inbound?.scope_version, 'inbound.scope_version'),
  copyVersion: required(manifest.consentimiento?.copy_version, 'consentimiento.copy_version'),
};
// La plantilla del carrito es una tabla en linea, que el lector minimo no lee.
const cartTemplate = manifestText.match(
  /^carrito = \{ nombre = "([a-z0-9_]+)", idioma = "([A-Za-z_]+)" \}$/m,
);
if (!cartTemplate) throw new Error('instancia.toml de ATT1 sin plantillas.carrito');
const TEMPLATE = { name: cartTemplate[1], language: cartTemplate[2] };
const [defaultOffer, ...additionalOffers] = offers;
const landingOf = (offer) => ({
  offer_code: offer.offer_code, site: offer.site, landing_id: offer.landing_id,
  page_host: offer.page_host, page_path: offer.page_path,
});

const PILOT = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/instances/att1/politica-piloto.json'), 'utf8',
));
const pilotScope = required(PILOT.pilot_scope, 'politica-piloto.pilot_scope');
const pilotPolicy = required(PILOT.policy, 'politica-piloto.policy');
const SCOPE = required(pilotScope.scope_key, 'pilot_scope.scope_key');
const SCOPE_VERSION = required(pilotScope.version, 'pilot_scope.version');
const POLICY = required(pilotPolicy.policy_key, 'policy.policy_key');
const POLICY_VERSION = required(pilotPolicy.version, 'policy.version');
const CHANNEL_PROVIDER = required(pilotScope.channel_provider, 'pilot_scope.channel_provider');
const CHANNEL_REF = `${required(pilotScope.channel_account_ref_prefix, 'pilot_scope.channel_account_ref_prefix')}${ATT1.inboxId}`;
const STEPS = required(pilotPolicy.steps, 'policy.steps');
if (CHANNEL_PROVIDER !== 'waba'
    || pilotPolicy.max_automatic_messages !== 1
    || STEPS.map((step) => step.step_key).join(',') !== 'first_contact,payment_failure_first_contact') {
  // Los estados de abajo asumen un toque por caso. Si la instancia lo cambia,
  // hay que revisarlos.
  throw new Error(`politica-piloto.json changed shape: ${JSON.stringify(pilotPolicy)}`);
}
const businessWindows = (policy) => JSON.stringify(required(policy.business_windows, 'business_windows')
  .map((window) => ({ ...window, start: '00:00', end: '23:59' })));
const CAPTURED_CART = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/hotmart_cart_abandonment_rejected_v1.json'), 'utf8',
)).payload;
const HANDOFF = { policy: 'att1-derivacion-entrante', version: 1 };
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
const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};
const CHATWOOT = {
  adopted: 940001,
  adoptedTwoInbound: 940002,
  cartOnly: 940003,
  inboundDuringSend: 940004,
  inboundOnly: 940005,
};

// ---------------------------------------------------------------------------
// La misma secuencia, sobre una base. Devuelve lo que vio, normalizado para
// poder comparar las dos bases (los ids cambian; los papeles no).
// ---------------------------------------------------------------------------
const runOn = async (db) => {
  const asService = async (action) => {
    await db.exec('set role service_role');
    try {
      return await action();
    } finally {
      await db.exec('reset role');
    }
  };
  const tryRows = async (sql, params) => {
    try {
      return { ok: true, rows: (await db.query(sql, params)).rows };
    } catch (caught) {
      return { ok: false, code: caught.code, message: caught.message };
    }
  };
  const show = (result) => (result.ok
    ? `${result.rows[0].outcome}:${result.rows[0].automation_status}`
    : `${result.code} ${result.message}`);
  const dbNow = async () => new Date((await db.query('select clock_timestamp() as now')).rows[0].now);

  // La provision de la instancia, como en validate_portable_inbound_template_adoption.mjs.
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
    JSON.stringify(additionalOffers.map(landingOf)),
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
    insert into public.followup_policy_versions
      (policy_key, version, status, purpose, timezone, business_windows,
       grace_period, expires_after, max_automatic_messages, steps,
       approved_by, approved_at, published_at)
    values ($1,$2,'published',$3,$4,$5::jsonb,$6::interval,$7::interval,$8,$9::jsonb,
            'operator-test',now(),now())
  `, [POLICY, POLICY_VERSION, required(pilotPolicy.purpose, 'policy.purpose'),
    ATT1.timezone, businessWindows(pilotPolicy), required(pilotPolicy.grace_period, 'policy.grace_period'),
    required(pilotPolicy.expires_after, 'policy.expires_after'), pilotPolicy.max_automatic_messages,
    JSON.stringify(STEPS)]);
  await db.query(`
    insert into public.pilot_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
       channel, channel_provider, channel_account_ref, source, source_event_type,
       additional_source_event_types, external_product_id, offer_code,
       additional_offer_codes, purpose, policy_key, policy_version, timezone,
       max_cohort_contacts, max_outbound_request_starts_total,
       max_outbound_request_starts_per_day, audience_mode,
       approved_by, approved_at, published_at)
    values ($1,$2,'published',$3,$4,$5,'whatsapp',$6,$7,$8,$9,$10::text[],$11,$12,
            $13::text[],$14,$15,$16,$17,$18,$19,$20,$21,'operator-test',now(),now())
  `, [
    SCOPE, SCOPE_VERSION, ATT1.tenant, ATT1.accountId, ATT1.inboxId, CHANNEL_PROVIDER,
    CHANNEL_REF, required(pilotScope.source, 'pilot_scope.source'),
    required(pilotScope.source_event_type, 'pilot_scope.source_event_type'),
    required(pilotScope.additional_source_event_types, 'pilot_scope.additional_source_event_types'),
    String(ATT1.productId), defaultOffer.offer_code,
    additionalOffers.map((offer) => offer.offer_code),
    required(pilotScope.purpose, 'pilot_scope.purpose'), POLICY, POLICY_VERSION, ATT1.timezone,
    required(pilotScope.max_cohort_contacts, 'pilot_scope.max_cohort_contacts'),
    required(pilotScope.max_outbound_request_starts_total, 'pilot_scope.max_outbound_request_starts_total'),
    required(pilotScope.max_outbound_request_starts_per_day, 'pilot_scope.max_outbound_request_starts_per_day'),
    pilotScope.audience_mode ?? 'manual_cohort',
  ]);
  await db.query(`
    insert into public.pilot_runtime_controls
      (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
    values ($1,$2,'inactive',0,'operator-test','default-off')
  `, [SCOPE, SCOPE_VERSION]);
  await db.query(`
    insert into public.commercial_ally_hotmart_purchase_policies
      (tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
    values ($1,$2,$3,true,$4::interval)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion,
    required(PILOT.purchase_policy?.max_lookback, 'purchase_policy.max_lookback')]);
  // El scope entrante de la instancia, publicado como lo deja aprovisionar-att1.sql.
  const publishInboundScope = (version) => db.query(`
    insert into public.inbound_commercial_scope_versions
      (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
       external_product_id, offer_code, approved_by, approved_at, published_at)
    values ($1,$2,'published',$3,$4,$5,$6,$7,'operator-test',now(),now())
  `, [ATT1.inboundScope, version, ATT1.tenant, ATT1.accountId, ATT1.inboxId,
    String(ATT1.productId), defaultOffer.offer_code]);
  await publishInboundScope(ATT1.inboundVersion);
  // La politica de la nota de derivacion del entrante, con el equipo del
  // manifiesto (la misma de validate_portable_precheckout_first_contact.mjs).
  await db.query(`
    insert into public.human_handoff_projection_policies (
      policy_key, policy_version, scope_key, scope_version,
      inbound_scope_key, inbound_scope_version, expected_team_id,
      note_template_key, note_template_version, private_note_body, active
    ) values ($1,$2,null,null,$3,$4,$5,'att1-nota-derivacion',1,
      'Derivacion automatica de prueba.',true)
  `, [HANDOFF.policy, HANDOFF.version, ATT1.inboundScope, ATT1.inboundVersion, ATT1.handoffTeam]);
  const armed = one((await db.query(`
    select * from public.set_lancemos_pilot_runtime_state($1,$2,0,'armed','operator-test','controlled-test')
  `, [SCOPE, SCOPE_VERSION])).rows, 'arm the recovery scope');
  if (armed.runtime_state !== 'armed') throw new Error(`the recovery scope is not armed: ${JSON.stringify(armed)}`);

  const baseMs = Math.floor((await dbNow()).getTime() / 1000) * 1000;
  const SUBMITTED_AT = new Date(baseMs - 50 * 60_000);
  const CART_AT = new Date(baseMs - 30 * 60_000);

  let personIndex = 0;
  const person = (label) => {
    personIndex += 1;
    const suffix = String(personIndex).padStart(2, '0');
    return {
      label,
      name: `Compradora ATT1 ${label}`,
      email: `att1-lookup-${suffix}@example.test`,
      phone: `120255503${suffix}`,
    };
  };
  // Formulario de la landing de la oferta (precedente inline de
  // validate_att1_portable_chain.mjs), con consentimiento.
  const admitForm = async (lead, offer) => {
    const id = `att1-lookup-form-${lead.email}-${offer.offer_code}`;
    const pageUrl = `https://${offer.page_host}${offer.page_path}`;
    const checkoutUrl = `https://pay.hotmart.com/${ATT1.hotlink}?off=${offer.offer_code}&checkoutMode=10`;
    const raw = {
      id,
      event: 'lead.precheckout',
      version: '1.1.0',
      created_at: SUBMITTED_AT.toISOString(),
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
      submitted_at: raw.created_at,
      source: {
        tenant_ref: ATT1.tenant, funnel_ref: ATT1.funnel, landing_ref: offer.landing_id,
        page_url: pageUrl, aliado: ATT1.brand,
      },
      identity: {
        email: lead.email, phone: lead.phone, phone_valid: true, phone_country_iso: 'US',
      },
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
    if (admitted.outcome !== 'inserted') {
      throw new Error(`${lead.label}: the form was not admitted: ${JSON.stringify(admitted)}`);
    }
  };
  const admitCart = async (lead, offer) => {
    const payload = structuredClone(CAPTURED_CART);
    payload.id = `att1-lookup-cart-${lead.email}`;
    payload.creation_date = CART_AT.getTime();
    payload.data.product = { id: ATT1.productId, name: ATT1.productName };
    payload.data.offer = { code: offer.offer_code };
    payload.data.buyer = { name: lead.name, email: lead.email, phone: lead.phone };
    const admitted = one((await db.query(`
      select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
    `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
      JSON.stringify(payload), lead.email, lead.phone])).rows, `${lead.label} cart`);
    if (admitted.outcome !== 'inserted') {
      throw new Error(`${lead.label}: the cart was not admitted: ${JSON.stringify(admitted)}`);
    }
    return { eventId: admitted.webhook_event_id, abandonedAt: new Date(payload.creation_date) };
  };
  // Lo que deja resolve_event antes de planificar: contacto nuevo y sus puntos.
  const createContact = async (lead, eventId) => {
    lead.contact = one((await db.query(`
      insert into public.contacts (full_name, email, phone, country_iso)
      values ($1,$2,$3,'MX') returning id
    `, [lead.name, lead.email, lead.phone])).rows, `${lead.label} contact`).id;
    await db.query(`
      insert into public.contact_points
        (contact_id, type, raw_value, normalized_value, source, source_event_id)
      values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)
    `, [lead.contact, lead.email, lead.phone, eventId]);
  };
  const enroll = async (lead) => {
    const { generation } = one((await db.query(`
      select generation from public.pilot_runtime_controls where scope_key = $1
    `, [SCOPE])).rows, 'scope generation');
    const member = one((await db.query(`
      select * from public.set_lancemos_pilot_cohort_member($1,$2,$3,$4,'active','operator-test','controlled-test')
    `, [SCOPE, SCOPE_VERSION, lead.contact, generation])).rows, `${lead.label} enrollment`);
    if (member.member_status !== 'active') {
      throw new Error(`${lead.label} was not enrolled: ${JSON.stringify(member)}`);
    }
  };
  const planCart = async (lead, offer, cart) => one((await db.query(`
    select * from public.plan_lancemos_pilot_cart_recovery(
      $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
    )
  `, [cart.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
    offer.offer_code, POLICY, POLICY_VERSION, cart.abandonedAt.toISOString(), ATT1.accountId,
    ATT1.inboxId, lead.phone, SCOPE, SCOPE_VERSION])).rows, `${lead.label} cart plan`);

  // La cadena del dispatcher para un carrito (la que
  // validate_att1_portable_chain.mjs fija contra el bridge): claim,
  // reevaluacion real, reserva approved_template y arranque del piloto. La
  // aceptacion de Chatwoot va aparte, para poder meter un entrante en el medio.
  let messageNumber = 0;
  const startSend = async (lead, plan) => {
    const worker = `att1-lookup-${lead.label}`;
    const now = await dbNow();
    const claimed = (await db.query(`
      select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
    `, [worker, now])).rows;
    if (claimed.length !== 1 || claimed[0].id !== plan.scheduled_action_id) {
      throw new Error(`${lead.label}: claimed ${JSON.stringify(claimed.map((row) => row.id))}, expected ${plan.scheduled_action_id}`);
    }
    const lease = claimed[0].lease_generation;
    const decision = one((await db.query(`
      select * from public.reevaluate_followup_action($1,$2,$3,$4)
    `, [plan.scheduled_action_id, worker, lease, now])).rows, `${lead.label} reevaluation`);
    if (decision.decision !== 'execute') {
      throw new Error(`${lead.label}: the real reevaluation did not execute: ${JSON.stringify(decision)}`);
    }
    const attempt = one((await db.query(`
      select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
    `, [plan.scheduled_action_id, worker, lease, decision.case_version,
      decision.sequence_revision, now])).rows, `${lead.label} reservation`);
    const started = one((await db.query(`
      select * from public.mark_lancemos_pilot_request_started($1,$2,$3,$4,$5)
    `, [plan.scheduled_action_id, attempt.id, worker, lease, now])).rows, `${lead.label} request start`);
    if (started.phase !== 'request_started' || started.pilot_authorization_id == null) {
      throw new Error(`${lead.label}: the send did not start: ${JSON.stringify(started)}`);
    }
    return { actionId: plan.scheduled_action_id, attemptId: attempt.id, worker, lease };
  };
  const accept = async (lead, delivery, chatwoot) => {
    messageNumber += 1;
    const accepted = one((await db.query(`
      select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
    `, [delivery.actionId, delivery.attemptId, delivery.worker, delivery.lease, String(chatwoot),
      `att1-lookup-wamid-${messageNumber}`,
      `Plantilla aprobada de ATT1 para ${lead.name} (${ATT1.productName})`,
      await dbNow()])).rows, `${lead.label} acceptance`);
    if (accepted.status !== 'accepted_by_chatwoot') {
      throw new Error(`${lead.label}: acceptance did not finalize: ${JSON.stringify(accepted)}`);
    }
  };
  // El carrito de una persona hasta el arranque del envio.
  const cartInFlight = async (label) => {
    const lead = person(label);
    await admitForm(lead, defaultOffer);
    const cart = await admitCart(lead, defaultOffer);
    await createContact(lead, cart.eventId);
    await enroll(lead);
    const plan = await planCart(lead, defaultOffer, cart);
    return { lead, plan, delivery: await startSend(lead, plan) };
  };
  const PORTABLE_SQL = 'select * from public.admit_portable_inbound_commercial_case_v1($1,$2,$3,$4)';
  const reply = async (chatwoot, user, version = ATT1.inboundVersion) => asService(() => tryRows(
    PORTABLE_SQL, [ATT1.inboundScope, version, chatwoot, user],
  ));
  const expectReply = async (label, chatwoot, user, expected, version) => {
    const result = await reply(chatwoot, user, version);
    if (show(result) !== expected) {
      throw new Error(`${label}: the reply gave ${show(result)}, expected ${expected}`);
    }
    return result.rows[0];
  };

  // ---------------------------------------------------------------------------
  // Los cinco estados.
  // ---------------------------------------------------------------------------
  const states = {};
  // A. Adoptada: la plantilla del carrito aceptada y la respuesta por la
  //    admision portable.
  {
    const { lead, delivery } = await cartInFlight('adoptada');
    await accept(lead, delivery, CHATWOOT.adopted);
    const admitted = await expectReply('A', CHATWOOT.adopted, lead.phone, 'created:draft_only');
    states.adopted = { chatwoot: CHATWOOT.adopted, lead, inboundCase: admitted.commercial_case_id };
  }
  // B. Adoptada con dos inbound_sales: el operador publica la version
  //    siguiente del scope entrante (el bridge pasa a admitir con ella) y la
  //    misma conversacion, ya en draft_only, se admite otra vez.
  {
    const { lead, delivery } = await cartInFlight('adoptada-dos-inbound');
    await accept(lead, delivery, CHATWOOT.adoptedTwoInbound);
    await expectReply('B', CHATWOOT.adoptedTwoInbound, lead.phone, 'created:draft_only');
    await publishInboundScope(ATT1.inboundVersion + 1);
    await expectReply('B (next scope version)', CHATWOOT.adoptedTwoInbound, lead.phone,
      'created:draft_only', ATT1.inboundVersion + 1);
    states.adoptedTwoInbound = { chatwoot: CHATWOOT.adoptedTwoInbound, lead };
  }
  // C. NO adoptada, un unico cart_recovery: la plantilla aceptada y nadie
  //    contesto.
  {
    const { lead, delivery } = await cartInFlight('solo-carrito');
    await accept(lead, delivery, CHATWOOT.cartOnly);
    states.cartOnly = { chatwoot: CHATWOOT.cartOnly, lead };
  }
  // D. NO adoptada, cart_recovery + inbound_sales: la persona escribe mientras
  //    el envio esta en vuelo (la admision crea la conversacion en draft_only,
  //    sin nada que adoptar) y la aceptacion cae en esa misma conversacion.
  {
    const { lead, delivery } = await cartInFlight('entrante-en-vuelo');
    const admitted = await expectReply('D', CHATWOOT.inboundDuringSend, lead.phone, 'created:draft_only');
    if (admitted.contact_id !== lead.contact) {
      throw new Error('D: the inbound did not resolve to the contact of the cart');
    }
    await accept(lead, delivery, CHATWOOT.inboundDuringSend);
    states.inboundDuringSend = { chatwoot: CHATWOOT.inboundDuringSend, lead };
  }
  // E. NO adoptada, solo el inbound_sales: alguien escribe sin plantilla previa.
  {
    const lead = person('solo-entrante');
    await expectReply('E', CHATWOOT.inboundOnly, lead.phone, 'created:draft_only');
    states.inboundOnly = { chatwoot: CHATWOOT.inboundOnly, lead };
  }

  // La forma de cada conversacion: los casos por tipo y los eventos de adopcion.
  const shapeOf = async (chatwoot) => one((await db.query(`
    select conversation.id, conversation.status, conversation.automation_status,
           (select array_agg(commercial_case.case_kind order by commercial_case.case_kind)
              from public.commercial_cases commercial_case
             where commercial_case.conversation_id = conversation.id) as kinds,
           (select count(*)::integer from public.conversation_events event
             where event.conversation_id = conversation.id
               and event.event_type = $2) as adoption_events
    from public.conversations conversation
    where conversation.commercial_context = jsonb_build_object('chatwoot_conversation_id', $1::text)
  `, [String(chatwoot), ADOPTION_EVENT])).rows, `conversation ${chatwoot}`);
  const shapes = {};
  for (const [name, state] of Object.entries(states)) {
    const shape = await shapeOf(state.chatwoot);
    state.conversation = shape.id;
    shapes[name] = `${shape.kinds.join('+')} events=${shape.adoption_events} ${shape.status}/${shape.automation_status}`;
  }

  // Los ids de cada base se nombran por su papel, para comparar las dos.
  const caseKinds = new Map((await db.query(
    'select id, case_kind from public.commercial_cases',
  )).rows.map((row) => [row.id, row.case_kind]));
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const normalize = (row, conversationId) => Object.fromEntries(Object.entries(row).map(([key, value]) => {
    if (value === null) return [key, null];
    if (value === conversationId) return [key, 'conversation'];
    if (caseKinds.has(value)) return [key, `case:${caseKinds.get(value)}`];
    if (key === 'checkout_url_final' || key === 'sck_value') return [key, 'set'];
    if (typeof value === 'string' && UUID.test(value)) return [key, 'new-id'];
    return [key, value];
  }));

  // Las cuatro sondas, como service_role, en un savepoint que se deshace.
  const PROBES = {
    mark_human_handoff_attended: (state, now) => [
      'select * from public.mark_human_handoff_attended($1::bigint,$2::timestamptz,$2::timestamptz)',
      [state.chatwoot, now],
    ],
    claim_conversation_reactivation: (state, now) => [
      `select * from public.claim_conversation_reactivation(
         $1::bigint,$2,'operator_request',$3,$4,1,100000,null,1,$5::timestamptz)`,
      [state.chatwoot, `reactivation:lookup:${state.chatwoot}`, TEMPLATE.name, TEMPLATE.language, now],
    ],
    resume_paused_conversation: (state, now) => [
      `select * from public.resume_paused_conversation(
         $1::bigint,$2,'operator_request',null,3,$3::timestamptz)`,
      [state.chatwoot, `resume:lookup:${state.chatwoot}`, now],
    ],
    claim_conversation_followup_v1: (state, now) => [
      `select * from public.claim_conversation_followup_v1(
         $1::bigint,$2,$3,$4,$5,$6,'went_quiet',$7,$8,'TIROIDES10',1,2,100000,$9,$10::timestamptz)`,
      [state.chatwoot, ATT1.accountId, ATT1.inboxId, state.lead.phone, state.lead.email,
        `followup:lookup:${state.chatwoot}`, TEMPLATE.name, TEMPLATE.language,
        ulidAt(Date.now()), now],
    ],
  };
  const probe = async (name, state) => {
    const [sql, params] = PROBES[name](state, await dbNow());
    await db.exec('savepoint lookup_probe');
    let result;
    try {
      await db.exec('set local role service_role');
      const row = one((await db.query(sql, params)).rows, `${name} on ${state.chatwoot}`);
      await db.exec('reset role');
      result = normalize(row, state.conversation);
      // El seguimiento no devuelve el caso: se lee de su fila.
      if (row.followup_event_id) {
        const event = one((await db.query(`
          select commercial_case_id from public.conversation_followup_events where id = $1
        `, [row.followup_event_id])).rows, 'followup event');
        result.followup_commercial_case = `case:${caseKinds.get(event.commercial_case_id)}`;
      }
    } catch (caught) {
      result = `${caught.code} ${caught.message}`;
    }
    await db.exec('rollback to savepoint lookup_probe');
    return result;
  };
  const probes = {};
  await db.exec('begin');
  try {
    for (const [name, state] of Object.entries(states)) {
      probes[name] = {};
      for (const rpc of Object.keys(RPCS)) {
        probes[name][rpc] = await probe(rpc, state);
      }
    }
  } finally {
    await db.exec('rollback');
  }

  // ---------------------------------------------------------------------------
  // "Necesito ayuda" sobre A, en una transaccion que se deshace: cada paso en
  // su savepoint (si falla, el estado queda como antes del paso).
  // ---------------------------------------------------------------------------
  const step = async (action) => {
    await db.exec('savepoint help_step');
    try {
      const result = await action();
      await db.exec('release savepoint help_step');
      return result;
    } catch (caught) {
      await db.exec('rollback to savepoint help_step');
      return `${caught.code} ${caught.message}`;
    }
  };
  const asServiceLocal = async (sql, params) => {
    await db.exec('set local role service_role');
    const rows = (await db.query(sql, params)).rows;
    await db.exec('reset role');
    return one(rows, sql);
  };
  const adopted = states.adopted;
  const help = {};
  await db.exec('begin');
  try {
    help.handoff = await step(async () => (await asServiceLocal(`
      select * from public.request_inbound_human_handoff(
        $1::uuid, $2, 'explicit_human_request', $3, $4, clock_timestamp(), null)
    `, [adopted.inboundCase, `handoff:lookup:${adopted.chatwoot}`, HANDOFF.policy, HANDOFF.version])).outcome);
    // Lo que deja el proyector de notas: la nota llego a Chatwoot.
    await db.query(`
      update public.human_handoff_requests
      set status = 'projected', projected_at = clock_timestamp(), updated_at = clock_timestamp()
      where commercial_case_id = $1
    `, [adopted.inboundCase]);
    help.reply_while_handed_off = await step(async () => {
      const row = await asServiceLocal(PORTABLE_SQL,
        [ATT1.inboundScope, ATT1.inboundVersion, adopted.chatwoot, adopted.lead.phone]);
      return `${row.outcome}:${row.automation_status}`;
    });
    help.attended = await step(async () => {
      const row = await asServiceLocal(
        'select * from public.mark_human_handoff_attended($1::bigint, clock_timestamp(), clock_timestamp())',
        [adopted.chatwoot]);
      return `${row.outcome}:${row.attended_count}:${normalize(row, adopted.conversation).attended_commercial_case_id}`;
    });
    help.resumed_by_the_macro = await step(async () => {
      const row = await asServiceLocal(`
        select * from public.resume_paused_conversation(
          $1::bigint, $2, 'operator_request', null, 3, clock_timestamp())
      `, [adopted.chatwoot, `resume:lookup:help:${adopted.chatwoot}`]);
      return `${row.outcome}:${normalize(row, adopted.conversation).resumed_commercial_case_id}`;
    });
    help.reply_after = await step(async () => {
      const row = await asServiceLocal(PORTABLE_SQL,
        [ATT1.inboundScope, ATT1.inboundVersion, adopted.chatwoot, adopted.lead.phone]);
      return `${row.outcome}:${row.automation_status}`;
    });
    const conversation = await shapeOf(adopted.chatwoot);
    const takeover = one((await db.query(
      'select human_takeover from public.conversations where id = $1', [adopted.conversation],
    )).rows, 'takeover').human_takeover;
    help.conversation_after = `${conversation.status}/${conversation.automation_status}/takeover=${takeover}`;
  } finally {
    await db.exec('rollback');
  }

  // Si la base tiene el filtro (lo que distingue a las dos) y el ACL.
  const functions = {};
  for (const [name, signature] of Object.entries(RPCS)) {
    const row = one((await db.query(`
      select position($2 in p.prosrc) > 0 as filtered,
             p.prosecdef as definer,
             p.proconfig as config,
             has_function_privilege('service_role', p.oid, 'execute') as service_x,
             has_function_privilege('anon', p.oid, 'execute') as anon_x,
             has_function_privilege('authenticated', p.oid, 'execute') as auth_x
      from pg_proc p where p.oid = to_regprocedure($1)
    `, [signature, ADOPTION_EVENT])).rows, signature);
    functions[name] = row;
  }
  return { shapes, probes, help, functions };
};

const before = await openDatabase(MIGRATIONS.filter((name) => name !== MIGRATION));
const without = await runOn(before);
await before.close();
const after = await openDatabase(MIGRATIONS);
const withIt = await runOn(after);
await after.close();

const fail = (label, detail) => {
  throw new Error(`${label}: ${JSON.stringify(detail)}`);
};
// Las dos bases arman los mismos estados.
const EXPECTED_SHAPES = {
  adopted: 'cart_recovery+inbound_sales events=1 active/draft_only',
  adoptedTwoInbound: 'cart_recovery+inbound_sales+inbound_sales events=1 active/draft_only',
  cartOnly: 'cart_recovery events=0 active/enabled',
  inboundDuringSend: 'cart_recovery+inbound_sales events=0 active/draft_only',
  inboundOnly: 'inbound_sales events=0 active/draft_only',
};
for (const shapes of [without.shapes, withIt.shapes]) {
  if (!isDeepStrictEqual(shapes, EXPECTED_SHAPES)) fail('the states', { without: without.shapes, with: withIt.shapes });
}
// La base sin la 000500 es la de hoy y la otra tiene el filtro; el ACL no cambia.
for (const [name, row] of Object.entries(withIt.functions)) {
  const old = without.functions[name];
  if (old.filtered !== false || row.filtered !== true
      || row.definer !== true
      || JSON.stringify(row.config) !== JSON.stringify(['search_path=pg_catalog, public, pg_temp'])
      || row.service_x !== true || row.anon_x !== false || row.auth_x !== false
      || old.service_x !== row.service_x || old.anon_x !== row.anon_x || old.auth_x !== row.auth_x) {
    fail(`the ${name} function`, { without: old, with: row });
  }
}
const ambiguousEverywhere = (probes) => Object.keys(RPCS).every((rpc) => probes[rpc] === AMBIGUOUS[rpc]);

// A. Sin la 000500, las cuatro ambiguas; con ella, ninguna, y eligen el
//    inbound_sales.
if (!ambiguousEverywhere(without.probes.adopted)) fail('A without the migration', without.probes.adopted);
const A = withIt.probes.adopted;
if (Object.values(A).some((result) => typeof result === 'string')
    || A.mark_human_handoff_attended.outcome !== 'noop'
    || A.mark_human_handoff_attended.attended_commercial_case_id !== 'case:inbound_sales'
    || A.claim_conversation_reactivation.outcome !== 'claimed'
    || A.claim_conversation_reactivation.reactivated_commercial_case_id !== 'case:inbound_sales'
    // already_active exige el caso en active/draft_only: solo el inbound_sales lo esta.
    || A.resume_paused_conversation.outcome !== 'already_active'
    || A.resume_paused_conversation.resumed_commercial_case_id !== 'case:inbound_sales'
    // El seguimiento pasa sus barreras y llega a la reserva del link, que solo
    // acepta un inbound_sales activo (con el cart_recovery daria
    // issuance_blocked_case). Ahi frena porque ATT1 no tiene catalogo de
    // ofertas de Johanna: su link sale por la reserva portable, y el
    // seguimiento con cupon todavia no la usa.
    || A.claim_conversation_followup_v1.outcome !== 'issuance_missing_default_offer'
    || A.claim_conversation_followup_v1.followup_event_id !== null) {
  fail('A with the migration', A);
}
// B. Dos inbound_sales: la guarda de ambiguedad sigue, en las dos bases.
if (!ambiguousEverywhere(without.probes.adoptedTwoInbound)
    || !ambiguousEverywhere(withIt.probes.adoptedTwoInbound)) {
  fail('B', { without: without.probes.adoptedTwoInbound, with: withIt.probes.adoptedTwoInbound });
}
// C, D y E: sin el evento de adopcion, exactamente lo de antes.
for (const name of ['cartOnly', 'inboundDuringSend', 'inboundOnly']) {
  if (!isDeepStrictEqual(without.probes[name], withIt.probes[name])) {
    fail(`${name} changed`, { without: without.probes[name], with: withIt.probes[name] });
  }
}
if (!ambiguousEverywhere(withIt.probes.inboundDuringSend)) fail('D', withIt.probes.inboundDuringSend);
for (const name of ['cartOnly', 'inboundOnly']) {
  if (Object.keys(RPCS).some((rpc) => withIt.probes[name][rpc] === AMBIGUOUS[rpc])) {
    fail(`${name} is a single case`, withIt.probes[name]);
  }
}
// El "Necesito ayuda": sin la 000500 queda con el equipo; con ella vuelve.
const EXPECTED_HELP = {
  without: {
    handoff: 'requested',
    reply_while_handed_off: 'blocked:disabled',
    attended: AMBIGUOUS.mark_human_handoff_attended,
    resumed_by_the_macro: AMBIGUOUS.resume_paused_conversation,
    reply_after: 'blocked:disabled',
    conversation_after: 'paused_human/paused/takeover=true',
  },
  with: {
    handoff: 'requested',
    reply_while_handed_off: 'blocked:disabled',
    attended: 'attended:1:case:inbound_sales',
    resumed_by_the_macro: 'resumed:case:inbound_sales',
    reply_after: 'already_exists:draft_only',
    conversation_after: 'active/draft_only/takeover=false',
  },
};
if (!isDeepStrictEqual({ without: without.help, with: withIt.help }, EXPECTED_HELP)) {
  fail('the help request on an adopted conversation', { without: without.help, with: withIt.help });
}

const summarize = (probes) => Object.fromEntries(Object.entries(probes).map(([rpc, result]) => [
  rpc, typeof result === 'string' ? result : result.outcome,
]));
console.log(JSON.stringify({
  commercial_case_lookups_by_inbound_kind: 'OK',
  states: withIt.shapes,
  adopted: { without: summarize(without.probes.adopted), with: summarize(A) },
  adopted_two_inbound: summarize(withIt.probes.adoptedTwoInbound),
  not_adopted_unchanged: {
    cart_only: summarize(withIt.probes.cartOnly),
    inbound_during_send: summarize(withIt.probes.inboundDuringSend),
    inbound_only: summarize(withIt.probes.inboundOnly),
  },
  help_request: { without: without.help, with: withIt.help },
}));
