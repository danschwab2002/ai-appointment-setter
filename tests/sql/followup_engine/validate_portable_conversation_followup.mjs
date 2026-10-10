// La reserva del seguimiento con cupon en el runtime con manifiesto (migracion
// 20261010000100, claim_portable_conversation_followup_v1), con las RPC reales
// sobre la cadena completa de migraciones y como service_role, el rol del
// bridge.
//
// El barredor del cupon (src/bridge/followup_discount.py) reserva con el
// telefono del contacto de Chatwoot, que en Mexico es el wa_id (521 + 10). En
// ATT1 la identidad del caso puede estar guardada como 52 + 10: el carrito la
// toma del telefono de Hotmart, que llega con los digitos que escribio la
// persona (con el 1 o sin el), y el primer contacto, del formulario (52). La
// reserva compartida exige la identidad exacta. Con
// manifiesto el bridge resuelve la identidad como el entrante
// (resolve_inbound_external_user_id) y reserva con la RPC portable: el link
// sale de reserve_portable_checkout_issuance_v2, que busca la intencion, la
// compra y la baja por las dos formas del movil, y solo pasa una conversacion
// adoptada (la respuesta a una plantilla nuestra). Este validador prueba:
//   0. (f) que la migracion agrega exactamente una funcion y no cambia ninguna
//      otra, ni su definicion ni sus grants: la compartida queda identica
//      (pg_get_functiondef antes y despues). La nueva es la compartida con sus
//      tres cambios y nada mas, definer con el mismo search_path, y solo
//      service_role la ejecuta: anon y authenticated reciben 42501. El
//      inventario de esquema da fingerprint_present;
//   a. la conversacion 21 de ATT1 (F4): el carrito con la identidad en 52, la
//      plantilla aceptada, la respuesta desde 521 resuelta a 52 y adoptada, el
//      link del agente y el silencio. La portable con 52 da claimed: la
//      emision es lead_intent con la oferta del formulario (2uafw5bg),
//      atribucion full, anclada en nuestro ultimo mensaje, y queda una sola
//      intencion viva. Despues, el resto del camino del barredor: la
//      autorizacion con la identidad resuelta (con el 521 da blocked_identity),
//      el cierre, el replay y el limite de uno por conversacion;
//   b. el control, antes de a: la compartida con el 521 de Chatwoot (el
//      barredor sin resolvedor) da issuance_blocked_identity y no escribe
//      nada;
//   a2. (derivado) la identidad en 521, con Hotmart trayendo el 1: la
//      portable da lead_intent sin fabricar una intencion; la compartida,
//      dentro de una transaccion que se deshace, da la oferta por defecto y
//      una segunda intencion viva. Es la otra mitad del hallazgo de la spec;
//   c. una conversacion organica (la persona escribio por su cuenta, sin
//      plantilla): la portable da blocked_not_template_reply y no escribe
//      nada; la compartida reservaria en esa misma conversacion;
//   d. la compra aprobada por Hotmart con el 521: purchase_already_approved,
//      sin escribir nada;
//   e. una baja guardada bajo el 521 en otra conversacion: la portable da
//      issuance_blocked_opt_out sin escribir nada; la compartida, con la
//      misma identidad resuelta, no la ve y reservaria;
//   g. sin la reserva portable (drop dentro de una transaccion que se
//      deshace) la migracion avisa con un notice, no crea ni cambia nada y la
//      huella del inventario da fingerprint_absent, no partial (la base de
//      Johanna). Ademas, una segunda corrida deja todo identico y, con la
//      compartida cambiada, la migracion falla con 55000.
//
// Datos: binding, ofertas, landings, producto, Chatwoot y consentimiento de
// tests/fixtures/instances/att1/instancia.toml; politica y scope del piloto de
// tests/fixtures/instances/att1/politica-piloto.json, con la misma desviacion
// documentada de la ventana de envio que validate_att1_portable_chain.mjs
// (00:00 a 23:59: la puerta de arranque exige el reloj real). El scope
// entrante y el catalogo de las tres ofertas, como
// despliegue/base/aprovisionar-att1.sql de la instancia (el hotlink como
// external_product_id y la primera oferta por defecto), igual que
// validate_whatsapp_phone_equivalence.mjs. Formulario: el golden del traductor
// de GHL de la landing -d (Mexico, 52 + 10, oferta 2uafw5bg, con el sck y el
// fbclid del anuncio), con id, fecha y comprador sustituidos. Carrito: la
// captura tests/fixtures/hotmart_cart_abandonment_rejected_v1.json con
// producto, oferta, id, fecha y comprador sustituidos; su telefono (anonimizado)
// tiene la forma 52 + 10, la del caso a, y el caso a2 usa la otra (derivado).
// La conversacion del caso a sale de F4
// (tests/fixtures/chatwoot_followup_candidate_inbox_11_20261010.json, la 21 de
// ATT1 anonimizada): su id, sus mensajes (plantilla, respuesta, link del
// agente), el telefono sintetico 521 y el mail; el candidato se arma con la
// regla de evaluate_followup_candidate. La plantilla y su idioma salen de F1
// (tests/fixtures/chatwoot_inbox_11_message_templates_20261010.json); el cupon
// es el de la decision de Dan (TIROIDES10), porque F1 lo trae redactado. La
// compra aprobada no tiene captura: usa el precedente inline de
// validate_whatsapp_phone_equivalence.mjs. Los demas telefonos son sinteticos.
//
// Lo que se simula y por que: el resolvedor del bridge
// (resolve_inbound_external_user_id) es la misma lectura de channel_identities
// que hace por PostgREST, con su regla: si entre las formas del wa_id hay una
// sola identidad activa del inbox y no es la textual, se usa la guardada. La
// baja del caso e se guarda bajo el 521 llamando a la RPC real con ese id: con
// el bridge de hoy no hay un camino que la deje asi despues de la adopcion (el
// resolvedor la guarda bajo el 52 y la frena antes la barrera del contacto,
// blocked_contact), pero es lo que la reserva portable suma sobre la
// compartida, la misma defensa que prueban validate_whatsapp_phone_equivalence.mjs
// (10.e) y la adopcion (opt_out_unmatched_other_form). Los contrastes con la
// compartida corren dentro de una transaccion que se deshace.
import { PGlite } from '@electric-sql/pglite';
import { randomBytes } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const db = new PGlite();
await db.waitReady;
// Los roles de la API como en Supabase: usan el schema public y tienen el
// execute por defecto en toda funcion nueva. Sin su revoke, la funcion queda
// ejecutable por anon y authenticated, y el caso f lo ve (el 42501 tiene que
// ser el de la funcion, no el del schema).
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  grant usage on schema public to service_role, anon, authenticated;
  alter default privileges grant execute on functions to anon, authenticated;
`);

const one = (rows, label) => {
  if (rows.length !== 1) throw new Error(`${label}: expected one row, got ${rows.length}`);
  return rows[0];
};
const sameSet = (left, right) => JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
const countOf = (text, needle) => text.split(needle).length - 1;
const results = {};

// ---------------------------------------------------------------------------
// 0 / f. La migracion cambia exactamente lo que dice. Se saca una foto de todas
//    las funciones de public antes y despues de aplicarla.
// ---------------------------------------------------------------------------
const TARGET = '20261010000100_portable_conversation_followup_claim.sql';
const ARGS = 'bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamp with time zone';
const SHARED_SIGNATURE = `claim_conversation_followup_v1(${ARGS})`;
const PORTABLE_SIGNATURE = `claim_portable_conversation_followup_v1(${ARGS})`;
const PORTABLE_RESERVE_SIGNATURE = 'reserve_portable_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamp with time zone)';
const SHARED_CLAIM = 'claim_conversation_followup_v1';
const PORTABLE_CLAIM = 'claim_portable_conversation_followup_v1';

const migrationNames = readdirSync(join(root, 'supabase/migrations'))
  .filter((name) => name.endsWith('.sql'))
  .sort();
const targetIndex = migrationNames.indexOf(TARGET);
if (targetIndex < 0) throw new Error(`${TARGET} is missing from the canonical stack`);
const forPglite = (text) => text.replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
);
const apply = async (file) => db.exec(forPglite(readFileSync(file, 'utf8')));
const snapshot = async () => new Map((await db.query(`
  select p.oid::regprocedure::text as signature,
         pg_get_functiondef(p.oid) as definition,
         p.prosecdef as security_definer,
         p.proconfig as config,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x,
         has_function_privilege('public', p.oid, 'execute') as public_x
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));
const diff = (left, right) => ({
  added: [...right.keys()].filter((signature) => !left.has(signature)),
  removed: [...left.keys()].filter((signature) => !right.has(signature)),
  changed: [...left.keys()].filter((signature) => right.has(signature)
    && right.get(signature).definition !== left.get(signature).definition),
  acl: [...left.keys()].filter((signature) => right.has(signature)
    && ['service_x', 'api_x', 'public_x'].some(
      (key) => right.get(signature)[key] !== left.get(signature)[key],
    )),
});
const unchanged = (change) => Object.values(change).every((list) => list.length === 0);

await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of migrationNames.slice(0, targetIndex)) {
  await apply(join(root, 'supabase/migrations', name));
}
const before = await snapshot();
await apply(join(root, 'supabase/migrations', TARGET));
const after = await snapshot();
for (const name of migrationNames.slice(targetIndex + 1)) {
  await apply(join(root, 'supabase/migrations', name));
}

{
  const change = diff(before, after);
  if (!sameSet(change.added, [PORTABLE_SIGNATURE]) || change.removed.length !== 0
      || change.changed.length !== 0 || change.acl.length !== 0) {
    throw new Error(`the migration changed something else: ${JSON.stringify(change)}`);
  }
  const shared = after.get(SHARED_SIGNATURE);
  if (!before.has(SHARED_SIGNATURE)
      || shared.definition !== before.get(SHARED_SIGNATURE).definition
      || !before.has(PORTABLE_RESERVE_SIGNATURE)) {
    throw new Error('the shared claim changed or the portable reserve is missing');
  }
  const portable = after.get(PORTABLE_SIGNATURE);
  if (portable.service_x !== true || portable.api_x !== false || portable.public_x !== false
      || portable.security_definer !== true || shared.security_definer !== true
      || JSON.stringify(portable.config) !== JSON.stringify(shared.config)
      || JSON.stringify(portable.config) !== JSON.stringify(['search_path=pg_catalog, public, pg_temp'])) {
    throw new Error(`the portable claim is not a service_role only definer like the shared one: ${JSON.stringify({ ...portable, definition: undefined })}`);
  }

  // La nueva es la compartida con sus tres cambios y nada mas: sacando el
  // bloque de la barrera y volviendo el nombre y la reserva, queda la
  // compartida byte a byte. Y el bloque va justo despues de calcular
  // v_inbound_only y antes del conteo de casos.
  const BLOCK_BEGIN = '\n    -- portable_followup_template_reply: begin\n';
  const BLOCK_END = '    -- portable_followup_template_reply: end\n';
  const INBOUND_ONLY_END = "and adoption.event_type = 'inbound_adopted_template_conversation'\n    );\n";
  const text = portable.definition;
  const start = text.indexOf(BLOCK_BEGIN);
  const stop = text.indexOf(BLOCK_END);
  const block = start < 0 || stop < start ? '' : text.slice(start, stop + BLOCK_END.length);
  const restored = (text.slice(0, start) + text.slice(stop + BLOCK_END.length))
    .replace('public.claim_portable_conversation_followup_v1(', 'public.claim_conversation_followup_v1(')
    .replace('from public.reserve_portable_checkout_issuance_v2(', 'from public.reserve_chatwoot_checkout_issuance_v2(');
  const drift = {
    derived: restored === shared.definition,
    blocks: countOf(text, BLOCK_BEGIN),
    barrier: block.replace(/^\s*--.*$/gm, '').replace(/\s+/g, ' ').trim(),
    after_inbound_only: start > 0 && text.slice(0, start).endsWith(INBOUND_ONLY_END),
    before_case_count: stop > 0 && text.indexOf('select count(*) into v_case_count') > stop,
    names: countOf(text, 'public.claim_portable_conversation_followup_v1('),
    portable_reserve: countOf(text, 'from public.reserve_portable_checkout_issuance_v2('),
    shared_reserve: countOf(text, 'reserve_chatwoot_checkout_issuance_v2'),
  };
  if (drift.derived !== true || drift.blocks !== 1
      || drift.barrier !== "if not v_inbound_only then outcome := 'blocked_not_template_reply'; return next; return; end if;"
      || drift.after_inbound_only !== true || drift.before_case_count !== true
      || drift.names !== 1 || drift.portable_reserve !== 1 || drift.shared_reserve !== 0) {
    throw new Error(`the portable claim drifted from the shared one: ${JSON.stringify(drift)}`);
  }
  results.migration_scope = {
    added: change.added.length,
    changed: 0,
    shared_claim_identical: true,
    portable_is_shared_plus_barrier: drift.derived,
    functions_unchanged: before.size,
  };
}
// La foto con la cadena entera: al final del validador tiene que seguir igual
// (los contrastes y el caso g se deshacen).
const settled = await snapshot();

const INVENTORY_SQL = readFileSync(join(root, 'scripts/supabase_schema_inventory.sql'), 'utf8');
const fingerprintOf = async () => (await db.query(INVENTORY_SQL)).rows
  .find((row) => row.version === TARGET.slice(0, 14));
{
  const row = await fingerprintOf();
  if (row?.fingerprint_status !== 'fingerprint_present' || row.present_markers !== 4) {
    throw new Error(`schema fingerprint ${TARGET}: ${JSON.stringify(row)}`);
  }
  results.schema_fingerprint = `${row.fingerprint_status} ${row.present_markers}/${row.total_markers}`;
}

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
    default: offer.por_defecto === true,
  };
});
if (offers.length !== 3 || !offers[0].default || offers.filter((offer) => offer.default).length !== 1) {
  throw new Error(`ATT1 declares three offers and the first is the default: ${JSON.stringify(offers)}`);
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
  inboundScope: required(manifest.inbound?.scope_key, 'inbound.scope_key'),
  inboundVersion: required(manifest.inbound?.scope_version, 'inbound.scope_version'),
  copyVersion: required(manifest.consentimiento?.copy_version, 'consentimiento.copy_version'),
};
const [defaultOffer, ...additionalOffers] = offers;
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
if (CHANNEL_PROVIDER !== 'waba' || pilotPolicy.max_automatic_messages !== 1
    || required(pilotPolicy.steps, 'policy.steps')[0]?.step_key !== 'first_contact') {
  // Los casos de abajo asumen un toque por caso y el paso first_contact del
  // carrito. Si la instancia los cambia, hay que revisarlos.
  throw new Error(`politica-piloto.json changed shape: ${JSON.stringify(pilotPolicy)}`);
}

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
// DESVIACION DOCUMENTADA (la misma de la cadena de ATT1): la ventana de envio
// usa los dias del fixture de 00:00 a 23:59, porque la puerta de arranque exige
// p_now a +-5 minutos del reloj de la base.
await db.query(`
  insert into public.followup_policy_versions
    (policy_key, version, status, purpose, timezone, business_windows,
     grace_period, expires_after, max_automatic_messages, steps,
     approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5::jsonb,$6::interval,$7::interval,$8,$9::jsonb,
          'operator-test',now(),now())
`, [POLICY, POLICY_VERSION, required(pilotPolicy.purpose, 'policy.purpose'), ATT1.timezone,
  JSON.stringify(required(pilotPolicy.business_windows, 'policy.business_windows')
    .map((window) => ({ ...window, start: '00:00', end: '23:59' }))),
  required(pilotPolicy.grace_period, 'policy.grace_period'),
  required(pilotPolicy.expires_after, 'policy.expires_after'),
  pilotPolicy.max_automatic_messages,
  JSON.stringify(pilotPolicy.steps)]);
// El scope del carrito con los valores de la instancia (sin audience_mode
// declarado: la cohorte manual, como validate_portable_inbound_template_adoption.mjs).
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
const armed = one((await db.query(`
  select * from public.set_lancemos_pilot_runtime_state($1,$2,0,'armed','operator-test','controlled-test')
`, [SCOPE, SCOPE_VERSION])).rows, 'arm the recovery scope');
if (armed.runtime_state !== 'armed') throw new Error(`the recovery scope is not armed: ${JSON.stringify(armed)}`);
// El scope entrante y el catalogo de las tres ofertas, como
// aprovisionar-att1.sql: el hotlink como external_product_id y la primera
// oferta por defecto. Con el product_id numerico la reserva da
// missing_default_offer para cualquier telefono.
await db.query(`
  insert into public.inbound_commercial_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     external_product_id, offer_code, approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5,$6,$7,'operator-test',now(),now())
`, [ATT1.inboundScope, ATT1.inboundVersion, ATT1.tenant, ATT1.accountId, ATT1.inboxId,
  ATT1.hotlink, defaultOffer.offer_code]);
for (const offer of offers) {
  await db.query(`
    insert into public.checkout_offer_catalog
      (tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref,
       offer_code, checkout_base_url, checkout_mode, default_for_inbound, status,
       version, approved_by, approved_at)
    values ($1,$2,$3,$4,$5,$6,$7,$8,10,$9,'active',1,'operator-test',now())
  `, [ATT1.tenant, ATT1.funnel, ATT1.inboundScope, ATT1.inboundVersion, ATT1.hotlink,
    offer.landing_id, offer.offer_code, `https://pay.hotmart.com/${ATT1.hotlink}`,
    offer.default]);
}

// ---------------------------------------------------------------------------
// Los datos reales: F1 (la plantilla), F4 (la conversacion 21), el golden del
// formulario y la captura del carrito.
// ---------------------------------------------------------------------------
const F1 = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/chatwoot_inbox_11_message_templates_20261010.json'), 'utf8',
));
const TEMPLATE_NAME = 'att1_seguimiento_descuento_01';
const template = one((F1.message_templates ?? [])
  .filter((candidate) => candidate.name === TEMPLATE_NAME), `F1 ${TEMPLATE_NAME}`);
const button = (template.components ?? []).find((component) => component.type === 'BUTTONS')
  ?.buttons?.find((candidate) => candidate.type === 'URL');
if (F1.id !== ATT1.inboxId || template.status !== 'APPROVED'
    || button?.url !== 'https://pay.hotmart.com/{{1}}'
    || !button.example?.[0]?.startsWith(`https://pay.hotmart.com/${ATT1.hotlink}?`)) {
  throw new Error(`F1 is not the catalog of the ATT1 inbox with ${TEMPLATE_NAME} approved: ${JSON.stringify({ inbox: F1.id, status: template.status, button })}`);
}
const TEMPLATE = { name: template.name, language: template.language };
const BUTTON_BASE = button.url.replace('{{1}}', '');
// La decision de Dan (D8 de la spec): F1 trae el cupon redactado.
const COUPON = 'TIROIDES10';
// El primer barrido de ATT1 (cada 600 s) despues de las 24 h de silencio.
const INBOUND_AGE_SECONDS = 86_400 + 600;

const F4 = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/chatwoot_followup_candidate_inbox_11_20261010.json'), 'utf8',
));
const f4Details = F4.conversation?.conversation ?? {};
const f4Sender = f4Details.meta?.sender ?? {};
// El candidato con la regla de evaluate_followup_candidate: el ultimo mensaje
// publico conversacional (por id) es del agent_bot, el ultimo entrante del
// contacto va antes, y el regimen sale de los salientes (un link de Hotmart y
// ninguno de pago fallido: link_sent_no_purchase).
const f4Conversational = (F4.conversation?.messages ?? [])
  .filter((message) => message.private === false && [0, 1, 3].includes(message.message_type))
  .sort((left, right) => left.id - right.id);
const f4Inbound = f4Conversational
  .filter((message) => message.message_type === 0 && message.sender?.type === 'contact');
const f4Outgoing = f4Conversational.filter((message) => message.message_type !== 0);
const f4Last = f4Conversational.at(-1);
const f4LastInbound = f4Inbound.at(-1);
const f4Template = f4Outgoing[0];
const f4AgentLink = f4Outgoing.filter((message) => (message.content ?? '').includes('https://pay.hotmart.com/')).at(-1);
const f4LinkOffer = f4AgentLink?.content.match(/[?&]off=([A-Za-z0-9]+)/)?.[1];
if (f4Details.account_id !== ATT1.accountId || f4Details.inbox_id !== ATT1.inboxId
    || f4Details.status !== 'open'
    || !/^\+521[0-9]{10}$/.test(f4Sender.phone_number ?? '')
    || !/^[^@\s]+@[^@\s]+$/.test(f4Sender.email ?? '')
    || f4Last?.message_type !== 1 || f4Last.sender?.type !== 'agent_bot'
    || f4AgentLink?.id !== f4Last.id
    || !(f4Template?.id < f4LastInbound?.id && f4LastInbound.id < f4Last.id)
    || f4Outgoing.some((message) => (message.content ?? '').includes('no pudo completarse'))) {
  throw new Error(`F4 is not the adopted ATT1 candidate this validator expects: ${JSON.stringify({ id: f4Details.id, messages: f4Conversational.map((message) => [message.id, message.message_type, message.sender?.type]) })}`);
}
const F4_CASE = {
  chatwoot: f4Details.id,
  templateMessage: f4Template.id,
  inbound: f4LastInbound.id,
  outbound: f4Last.id,
  regime: 'link_sent_no_purchase',
  phone: f4Sender.phone_number.slice(1),
  email: f4Sender.email,
  name: f4Sender.name,
};

const GOLDEN = JSON.parse(readFileSync(join(root,
  'tests/fixtures/ghl/expected/ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json'), 'utf8'));
const FORM_OFFER = offers.find((offer) => offer.offer_code === GOLDEN.canonical_payload?.commerce?.offer_ref);
const FORM_SCK = GOLDEN.raw_payload?.data?.attribution?.sck;
const FORM_FBCLID = GOLDEN.raw_payload?.data?.attribution?.fbclid;
if (GOLDEN.raw_payload?.id !== GOLDEN.canonical_payload?.external_submission_id
    || GOLDEN.canonical_payload.identity.phone_country_iso !== 'MX'
    || GOLDEN.raw_payload.data.buyer.phone_country_code !== '52'
    || !/^52[0-9]{10}$/.test(GOLDEN.canonical_payload.identity.phone)
    || FORM_OFFER?.offer_code !== '2uafw5bg'
    || FORM_OFFER.default
    || GOLDEN.canonical_payload.consent.copy_version !== ATT1.copyVersion
    || GOLDEN.canonical_payload.consent.whatsapp_contact !== true
    || !FORM_SCK || !FORM_FBCLID
    || f4LinkOffer !== FORM_OFFER.offer_code) {
  // F4 es una conversacion de la landing -d: su link del agente salio con la
  // oferta del formulario. El caso a la reconstruye con ese golden.
  throw new Error(`the -d golden is not the MX form of F4: ${JSON.stringify({ offer: FORM_OFFER?.offer_code, f4LinkOffer })}`);
}
const CAPTURED_CART = JSON.parse(readFileSync(
  join(root, 'tests/fixtures/hotmart_cart_abandonment_rejected_v1.json'), 'utf8',
)).payload;
if (!/^52[0-9]{10}$/.test(CAPTURED_CART?.data?.buyer?.phone ?? '')) {
  // El caso a usa la forma del telefono de la captura (anonimizado): 52 + 10,
  // sin el 1. El caso a2 prueba la otra forma.
  throw new Error(`the captured cart no longer brings a 52 + 10 phone: ${CAPTURED_CART?.data?.buyer?.phone}`);
}

// ---------------------------------------------------------------------------
// Tiempos, personas y la cadena (formulario, carrito, plantilla, respuesta).
// ---------------------------------------------------------------------------
const dbNow = async () => new Date((await db.query('select clock_timestamp() as now')).rows[0].now);
const baseMs = Math.floor((await dbNow()).getTime() / 1000) * 1000;
const at = (minutes) => new Date(baseMs + minutes * 60_000);
const SUBMITTED_AT = at(-50);
const CART_AT = at(-30);
const PURCHASED_AT = at(-5);

// ULID como el del adaptador (generate_issuance_ulid): 48 bits de milisegundos
// y 80 aleatorios, en base32 de Crockford.
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

const asService = async (action) => {
  await db.exec('set role service_role');
  try {
    return await action();
  } finally {
    // Dentro de una transaccion abortada el reset falla, y el rollback que
    // sigue devuelve el rol de la sesion.
    try {
      await db.exec('reset role');
    } catch {
      // nada
    }
  }
};
const rolledBack = async (action) => {
  await db.exec('begin');
  try {
    return await action();
  } finally {
    await db.exec('rollback');
  }
};

// Una persona con su movil en las dos formas: plain (52 + 10, la del
// formulario y la de Hotmart) y whatsapp (521 + 10, el wa_id de Chatwoot).
let personIndex = 0;
const person = (label, { national, email, name } = {}) => {
  personIndex += 1;
  const suffix = String(personIndex).padStart(2, '0');
  const digits = national ?? `55555561${suffix}`;
  return {
    label,
    name: name ?? `Compradora cupon ${label}`,
    email: email ?? `att1-cupon-${suffix}@example.test`,
    plain: `52${digits}`,
    whatsapp: `521${digits}`,
    offer: FORM_OFFER,
  };
};
const intentOf = async (id, label) => one((await db.query(`
  select normalized_phone, offer_ref, lifecycle_state, whatsapp_contact_authorized,
         activation_authorized
  from public.purchase_intents where id = $1
`, [id])).rows, label);

// El formulario: el golden de la landing -d con id, fecha y comprador
// sustituidos, por la admision real.
const submitForm = async (lead) => {
  const raw = structuredClone(GOLDEN.raw_payload);
  const canonical = structuredClone(GOLDEN.canonical_payload);
  const id = ulidAt(SUBMITTED_AT.getTime());
  const dedupeKey = `${raw.source.site}:${raw.data.offer.code}:${lead.email}`;
  raw.id = id;
  raw.created_at = SUBMITTED_AT.toISOString();
  raw.data.buyer.email = lead.email;
  raw.data.buyer.phone = `+${lead.plain}`;
  raw.data.buyer.phone_national = lead.plain.slice(2);
  raw.dedupe_key = dedupeKey;
  canonical.external_submission_id = id;
  canonical.submitted_at = SUBMITTED_AT.toISOString().replace('.000Z', 'Z');
  canonical.identity.email = lead.email;
  canonical.identity.phone = lead.plain;
  canonical.dedupe_key = dedupeKey;
  const admitted = one((await db.query(`
    select * from public.admit_portable_observed_lead_precheckout($1,$2,$3,$4,$5::jsonb,$6::jsonb)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, id, JSON.stringify(raw),
    JSON.stringify(canonical)])).rows, `${lead.label} form`);
  const intent = await intentOf(admitted.purchase_intent_id, `${lead.label} intent`);
  if (admitted.outcome !== 'inserted'
      || intent.normalized_phone !== lead.plain
      || intent.offer_ref !== lead.offer.offer_code
      || intent.whatsapp_contact_authorized !== true
      || intent.activation_authorized !== true) {
    throw new Error(`${lead.label}: unexpected form admission: ${JSON.stringify({ outcome: admitted.outcome, intent })}`);
  }
  lead.intent = admitted.purchase_intent_id;
};

// El carrito capturado, con el telefono que manda Hotmart.
const admitCart = async (lead, { phone }) => {
  const payload = structuredClone(CAPTURED_CART);
  payload.id = `att1-cupon-cart-${lead.email}`;
  payload.creation_date = CART_AT.getTime();
  payload.data.product = { id: ATT1.productId, name: ATT1.productName };
  payload.data.offer = { code: lead.offer.offer_code };
  payload.data.buyer = { name: lead.name, email: lead.email, phone };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id,
    JSON.stringify(payload), lead.email, phone])).rows, `${lead.label} cart`);
  const correlation = (await db.query(`
    select outcome, purchase_intent_id from public.hotmart_purchase_intent_correlations
    where webhook_event_id = $1
  `, [admitted.webhook_event_id])).rows[0] ?? null;
  if (admitted.outcome !== 'inserted' || correlation?.outcome !== 'resolved'
      || correlation.purchase_intent_id !== lead.intent) {
    throw new Error(`${lead.label}: the cart did not correlate with the form: ${JSON.stringify({ outcome: admitted.outcome, correlation })}`);
  }
  return { eventId: admitted.webhook_event_id, abandonedAt: new Date(payload.creation_date) };
};

// Lo que deja resolve_event antes de planificar: el contacto con el telefono de
// Hotmart en contacts.phone y en su contact_point.
const createContact = async (lead, eventId, { phone }) => {
  lead.contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone, country_iso)
    values ($1,$2,$3,'MX') returning id
  `, [lead.name, lead.email, phone])).rows, `${lead.label} contact`).id;
  await db.query(`
    insert into public.contact_points
      (contact_id, type, raw_value, normalized_value, source, source_event_id)
    values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)
  `, [lead.contact, lead.email, phone, eventId]);
  lead.destination = phone;
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
const planCart = async (lead, cart) => one((await db.query(`
  select * from public.plan_lancemos_pilot_cart_recovery(
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
  )
`, [cart.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  lead.offer.offer_code, POLICY, POLICY_VERSION, cart.abandonedAt.toISOString(),
  ATT1.accountId, ATT1.inboxId, lead.destination, SCOPE, SCOPE_VERSION])).rows,
`${lead.label} cart plan`);

// La cadena del dispatcher (la que validate_att1_portable_chain.mjs fija contra
// el bridge): claim, reevaluacion real, reserva approved_template, arranque del
// piloto y la aceptacion de Chatwoot de la plantilla en la conversacion.
const dispatchTemplate = async (lead, plan, { chatwoot, templateMessage }) => {
  const worker = `att1-cupon-${lead.label}`;
  const now = await dbNow();
  const claimed = (await db.query(`
    select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
  `, [worker, now])).rows;
  if (claimed.length !== 1 || claimed[0].id !== plan.scheduled_action_id
      || claimed[0].anchor_type !== 'cart_abandonment') {
    throw new Error(`${lead.label}: claimed ${JSON.stringify(claimed.map((row) => [row.id, row.anchor_type]))}, expected ${plan.scheduled_action_id}`);
  }
  const lease = claimed[0].lease_generation;
  const decision = one((await db.query(`
    select * from public.reevaluate_followup_action($1,$2,$3,$4)
  `, [plan.scheduled_action_id, worker, lease, now])).rows, `${lead.label} reevaluation`);
  if (decision.decision !== 'execute') {
    throw new Error(`${lead.label}: the real reevaluation did not execute: ${JSON.stringify(decision)}`);
  }
  const context = one((await db.query(`
    select * from public.get_followup_execution_context($1,$2,$3,$4)
  `, [plan.scheduled_action_id, worker, lease, now])).rows, `${lead.label} context`);
  if (context.step_key !== 'first_contact' || context.offer_code !== lead.offer.offer_code) {
    throw new Error(`${lead.label}: execution context diverged: ${JSON.stringify(context)}`);
  }
  const attempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
  `, [plan.scheduled_action_id, worker, lease, decision.case_version,
    decision.sequence_revision, now])).rows, `${lead.label} reservation`);
  const started = one((await db.query(`
    select * from public.mark_lancemos_pilot_request_started($1,$2,$3,$4,$5)
  `, [plan.scheduled_action_id, attempt.id, worker, lease, now])).rows,
  `${lead.label} request start`);
  if (started.phase !== 'request_started' || started.pilot_authorization_id == null) {
    throw new Error(`${lead.label}: the template did not start: ${JSON.stringify(started)}`);
  }
  const accepted = one((await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,$5,$6,$7,$8)
  `, [plan.scheduled_action_id, attempt.id, worker, lease, String(chatwoot),
    String(templateMessage), `Plantilla del carrito de ATT1 para ${lead.name} (${ATT1.productName})`,
    await dbNow()])).rows, `${lead.label} acceptance`);
  if (accepted.status !== 'accepted_by_chatwoot') {
    throw new Error(`${lead.label}: the acceptance did not finalize: ${JSON.stringify(accepted)}`);
  }
};

// El formulario, el carrito con el telefono de Hotmart y la plantilla aceptada
// en la conversacion de Chatwoot: lo que deja el piloto antes de la respuesta.
const cartTemplateSent = async (lead, { hotmartPhone, chatwoot, templateMessage }) => {
  await submitForm(lead);
  const cart = await admitCart(lead, { phone: hotmartPhone });
  await createContact(lead, cart.eventId, { phone: hotmartPhone });
  await enroll(lead);
  const plan = await planCart(lead, cart);
  await dispatchTemplate(lead, plan, { chatwoot, templateMessage });
  return plan;
};

const conversationState = async (chatwoot) => {
  const rows = (await db.query(`
    select conversation.id, conversation.automation_status,
           identity.external_user_id
    from public.conversations conversation
    join public.channel_identities identity on identity.id = conversation.channel_identity_id
    where conversation.commercial_context ->> 'chatwoot_conversation_id' = $1
  `, [String(chatwoot)])).rows;
  if (rows.length > 1) throw new Error(`more than one conversation for ${chatwoot}`);
  return rows[0] ?? null;
};
const adoptionEvents = async (conversationId) => one((await db.query(`
  select count(*)::integer as count from public.conversation_events
  where conversation_id = $1 and event_type = 'inbound_adopted_template_conversation'
`, [conversationId])).rows, 'adoption events').count;

// resolve_inbound_external_user_id del bridge: la misma lectura de
// channel_identities (find_active_whatsapp_identities) y la misma regla.
const resolveInbound = async (waId) => {
  const { forms } = one((await db.query(
    'select public._whatsapp_phone_variants($1) as forms', [waId],
  )).rows, 'forms');
  if (!forms || forms.length < 2) return waId;
  const stored = [...new Set((await db.query(`
    select external_user_id from public.channel_identities
    where channel = 'whatsapp' and account_id = $1
      and external_user_id = any($2::text[]) and identity_status = 'active'
      and metadata ->> 'inbox_id' = $3
  `, [`chatwoot:${ATT1.accountId}`, forms, String(ATT1.inboxId)])).rows
    .map((row) => row.external_user_id))].sort();
  return stored.length === 1 && stored[0] !== waId ? stored[0] : waId;
};

// La persona contesta desde su wa_id: el bridge resuelve la identidad y admite
// por la admision portable, como service_role.
const replyFromWhatsapp = async (lead, chatwoot) => {
  const userId = await resolveInbound(lead.whatsapp);
  const admitted = one((await asService(() => db.query(`
    select * from public.admit_portable_inbound_commercial_case_v1($1,$2,$3,$4)
  `, [ATT1.inboundScope, ATT1.inboundVersion, chatwoot, userId]))).rows,
  `${lead.label} reply`);
  const state = await conversationState(chatwoot);
  return {
    userId,
    outcome: `${admitted.outcome}:${admitted.automation_status}`,
    caseId: admitted.commercial_case_id,
    conversationId: state?.id,
    identity: state?.external_user_id,
    adopted: state === null ? 0 : await adoptionEvents(state.id),
  };
};

// El link del agente: la reserva portable sobre el mensaje del lead, la
// autorizacion y el cierre con nuestro mensaje.
const agentLink = async (lead, reply, chatwoot, { inbound, outbound }) => {
  const reserved = one((await asService(() => db.query(`
    select * from public.reserve_portable_checkout_issuance_v2($1,$2,$3,$4,$5,$6,$7,clock_timestamp())
  `, [reply.caseId, reply.userId, ATT1.accountId, ATT1.inboxId, chatwoot, String(inbound),
    ulidAt(Date.now())]))).rows, `${lead.label} agent link`);
  const authorized = one((await asService(() => db.query(`
    select * from public.authorize_chatwoot_checkout_issuance_v2($1,$2,$3,$4,$5,$6,clock_timestamp())
  `, [reserved.issuance_id, reply.userId, ATT1.accountId, ATT1.inboxId, chatwoot,
    String(inbound)]))).rows, `${lead.label} agent link authorization`);
  const finalized = one((await asService(() => db.query(`
    select * from public.finalize_chatwoot_checkout_issuance_v2($1,'accepted_by_chatwoot',$2,null,clock_timestamp())
  `, [reserved.issuance_id, outbound]))).rows, `${lead.label} agent link finalization`);
  const issuance = await issuanceOf(reserved.issuance_id);
  if (reserved.outcome !== 'reserved' || authorized.outcome !== 'request_started'
      || finalized.outcome !== 'finalized'
      || issuance.offer_resolution !== 'lead_intent'
      || issuance.offer_code !== lead.offer.offer_code
      || issuance.purchase_intent_id !== lead.intent) {
    throw new Error(`${lead.label}: the agent link is not the one of the form: ${JSON.stringify({ reserved: reserved.outcome, authorized: authorized.outcome, finalized: finalized.outcome, ...issuance })}`);
  }
  return issuance;
};

const issuanceOf = async (id) => one((await db.query(`
  select issuance.id, issuance.status, issuance.source_kind, issuance.offer_resolution,
         issuance.attribution_resolution, issuance.purchase_intent_id,
         issuance.checkout_url_final, issuance.sck_value,
         issuance.trigger_external_message_id, issuance.commercial_case_id,
         issuance.channel_identity_id, offer.offer_code
  from public.checkout_link_issuances issuance
  join public.checkout_offer_catalog offer on offer.id = issuance.offer_catalog_id
  where issuance.id = $1
`, [id])).rows, `issuance ${id}`);

// La reserva del barredor, como service_role: los argumentos que arma
// _follow_up (el command_key de FollowupCandidate, la plantilla, el cupon).
const claimFollowup = async (rpc, {
  chatwoot, userId, email = null, regime, inbound, outbound,
  key = `followup:${chatwoot}:${outbound}`, ulid = ulidAt(Date.now()),
}) => ({
  ...one((await asService(() => db.query(`
    select * from public.${rpc}($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
  `, [chatwoot, ATT1.accountId, ATT1.inboxId, userId, email, key, regime, TEMPLATE.name,
    TEMPLATE.language, COUPON, inbound, outbound, INBOUND_AGE_SECONDS, ulid, null]))).rows,
  `${rpc} ${chatwoot}`),
  key,
  ulid,
});

// Lo que puede escribir una reserva: la fila del seguimiento, la emision y una
// intencion fabricada.
const footprint = async (lead, chatwoot) => one((await db.query(`
  select
    (select count(*)::integer from public.conversation_followup_events
      where external_conversation_id = $1) as followups,
    (select count(*)::integer from public.checkout_link_issuances
      where chatwoot_account_id = $2 and chatwoot_inbox_id = $3
        and chatwoot_conversation_id = $1) as issuances,
    (select count(*)::integer from public.purchase_intents
      where normalized_phone = any($4::text[])) as intents,
    (select count(*)::integer from public.purchase_intents
      where normalized_phone = any($4::text[])
        and lifecycle_state = 'waiting_for_purchase') as live_intents
`, [chatwoot, ATT1.accountId, ATT1.inboxId, [lead.plain, lead.whatsapp]])).rows,
`${lead.label} footprint`);
const sameFootprint = (left, right) => JSON.stringify(left) === JSON.stringify(right);

// Una adopcion: la respuesta resuelta entra a la conversacion de la plantilla.
const expectAdopted = (label, lead, reply, identity) => {
  if (reply.outcome !== 'created:draft_only' || reply.adopted !== 1
      || reply.identity !== identity || reply.userId !== identity) {
    throw new Error(`${label}: the reply was not adopted with the identity ${identity}: ${JSON.stringify(reply)}`);
  }
};

// ---------------------------------------------------------------------------
// a + b. La conversacion 21 de ATT1 (F4). El carrito llega con el telefono de
//    Hotmart sin el 1 (52, la forma de la captura), la plantilla sale y se
//    acepta, la persona contesta desde su
//    wa_id (521): el bridge lo resuelve a la identidad guardada (52) y la
//    admision portable adopta la conversacion. El agente le manda el link y la
//    persona se calla.
// ---------------------------------------------------------------------------
{
  const lead = person('f4', {
    national: F4_CASE.phone.slice(3), email: F4_CASE.email, name: F4_CASE.name,
  });
  if (lead.whatsapp !== F4_CASE.phone) throw new Error('the F4 phone is not a 521 + 10 mobile');
  await cartTemplateSent(lead, {
    hotmartPhone: lead.plain, chatwoot: F4_CASE.chatwoot, templateMessage: F4_CASE.templateMessage,
  });
  const reply = await replyFromWhatsapp(lead, F4_CASE.chatwoot);
  expectAdopted('F4', lead, reply, lead.plain);
  const link = await agentLink(lead, reply, F4_CASE.chatwoot,
    { inbound: F4_CASE.inbound, outbound: F4_CASE.outbound });
  const claimArgs = {
    chatwoot: F4_CASE.chatwoot, email: F4_CASE.email, regime: F4_CASE.regime,
    inbound: F4_CASE.inbound, outbound: F4_CASE.outbound,
  };
  const start = await footprint(lead, F4_CASE.chatwoot);

  // b. El barredor sin resolvedor reserva con el telefono de Chatwoot (521) y
  //    la compartida: la reserva exige la identidad del caso (52).
  const shared = await claimFollowup(SHARED_CLAIM, { ...claimArgs, userId: lead.whatsapp });
  const afterShared = await footprint(lead, F4_CASE.chatwoot);
  if (shared.outcome !== 'issuance_blocked_identity' || shared.followup_event_id !== null
      || shared.checkout_issuance_id !== null || !sameFootprint(start, afterShared)) {
    throw new Error(`b. the shared claim with the 521 of Chatwoot: ${JSON.stringify({ shared, start, afterShared })}`);
  }

  // a. Con manifiesto: la identidad resuelta (52) y la portable.
  const userId = await resolveInbound(lead.whatsapp);
  const claimed = await claimFollowup(PORTABLE_CLAIM, { ...claimArgs, userId });
  const issuance = claimed.checkout_issuance_id === null ? null
    : await issuanceOf(claimed.checkout_issuance_id);
  const event = claimed.followup_event_id === null ? null : one((await db.query(`
    select status, regime, template_name, template_language, coupon_code,
           last_inbound_message_id::text as last_inbound,
           last_outbound_message_id::text as last_outbound, inbound_age_seconds,
           checkout_issuance_id, conversation_id, commercial_case_id, command_key
    from public.conversation_followup_events where id = $1
  `, [claimed.followup_event_id])).rows, 'F4 followup event');
  const afterClaim = await footprint(lead, F4_CASE.chatwoot);
  const url = issuance?.checkout_url_final ?? '';
  if (userId !== lead.plain || claimed.outcome !== 'claimed'
      || issuance.status !== 'reserved'
      || issuance.id === link.id
      || issuance.trigger_external_message_id !== String(F4_CASE.outbound)
      || issuance.commercial_case_id !== reply.caseId
      || issuance.source_kind !== 'precheckout_request'
      || issuance.offer_resolution !== 'lead_intent'
      || issuance.offer_code !== FORM_OFFER.offer_code
      || issuance.attribution_resolution !== 'full'
      || issuance.purchase_intent_id !== lead.intent
      || claimed.checkout_url_final !== url
      || claimed.sck_value !== `${FORM_SCK}~hermes~v1~${claimed.ulid}`
      || !url.startsWith(`${BUTTON_BASE}${ATT1.hotlink}?off=${FORM_OFFER.offer_code}&`)
      || !url.includes(`&sck=${FORM_SCK}~hermes~v1~${claimed.ulid}`)
      || !url.endsWith(`&fbclid=${FORM_FBCLID}`)
      || event?.status !== 'claimed'
      || event.regime !== F4_CASE.regime
      || event.template_name !== TEMPLATE.name || event.template_language !== TEMPLATE.language
      || event.coupon_code !== COUPON
      || event.last_inbound !== String(F4_CASE.inbound)
      || event.last_outbound !== String(F4_CASE.outbound)
      || event.inbound_age_seconds !== INBOUND_AGE_SECONDS
      || event.checkout_issuance_id !== issuance.id
      || event.conversation_id !== reply.conversationId
      || event.commercial_case_id !== reply.caseId
      || event.command_key !== claimed.key
      || afterClaim.followups !== 1 || afterClaim.issuances !== start.issuances + 1
      || afterClaim.intents !== 1 || afterClaim.live_intents !== 1) {
    throw new Error(`a. the portable claim with the resolved identity: ${JSON.stringify({ userId, claimed, issuance, event, afterClaim })}`);
  }

  // El resto del camino del barredor. La autorizacion exige la misma identidad
  // que la reserva: con el 521 da blocked_identity (dentro de una transaccion
  // que se deshace), con la resuelta arranca. Despues el cierre, el replay del
  // mismo command_key y el limite de uno por conversacion.
  const authorize = (externalUserId) => asService(() => db.query(`
    select * from public.authorize_chatwoot_checkout_issuance_v2($1,$2,$3,$4,$5,$6,clock_timestamp())
  `, [issuance.id, externalUserId, ATT1.accountId, ATT1.inboxId, F4_CASE.chatwoot,
    String(F4_CASE.outbound)]));
  const authorizedWithWaId = one((await rolledBack(() => authorize(lead.whatsapp))).rows,
    'F4 authorization with the wa_id');
  const authorized = one((await authorize(userId)).rows, 'F4 authorization');
  const finalized = one((await asService(() => db.query(`
    select * from public.finalize_chatwoot_checkout_issuance_v2($1,'accepted_by_chatwoot',$2,null,clock_timestamp())
  `, [issuance.id, F4_CASE.outbound + 1]))).rows, 'F4 finalization');
  const settledEvent = one((await asService(() => db.query(`
    select * from public.settle_conversation_followup_v1($1,'sent',$2,null,clock_timestamp())
  `, [claimed.key, F4_CASE.outbound + 1]))).rows, 'F4 settlement');
  const replayed = await claimFollowup(PORTABLE_CLAIM, { ...claimArgs, userId });
  const limited = await claimFollowup(PORTABLE_CLAIM, {
    ...claimArgs, userId, outbound: F4_CASE.outbound + 1,
  });
  const afterAll = await footprint(lead, F4_CASE.chatwoot);
  if (authorizedWithWaId.outcome !== 'blocked_identity'
      || authorized.outcome !== 'request_started'
      || finalized.outcome !== 'finalized'
      || settledEvent.outcome !== 'settled'
      || settledEvent.followup_event_id !== claimed.followup_event_id
      || replayed.outcome !== 'replayed'
      || replayed.followup_event_id !== claimed.followup_event_id
      || replayed.checkout_url_final !== url
      || limited.outcome !== 'blocked_followup_limit'
      || !sameFootprint(afterAll, afterClaim)) {
    throw new Error(`a. the rest of the sweeper path: ${JSON.stringify({ authorizedWithWaId, authorized, finalized, settledEvent, replayed: replayed.outcome, limited: limited.outcome, afterAll })}`);
  }
  results.b_shared_with_the_chatwoot_phone = `${shared.outcome}, followups +0, issuances +0`;
  results.a_f4_portable_with_the_resolved_identity = {
    reply: `${reply.outcome}, identity 52 (resolved from 521), adopted`,
    agent_link: `${link.offer_resolution}:${link.offer_code}:${link.attribution_resolution}`,
    claim: `${claimed.outcome}:${issuance.source_kind}:${issuance.offer_resolution}:${issuance.offer_code}:${issuance.attribution_resolution}`,
    anchored_on_our_last_message: issuance.trigger_external_message_id,
    live_intents: afterClaim.live_intents,
    authorize_with_the_wa_id: authorizedWithWaId.outcome,
    sweeper_path: `${authorized.outcome}, ${finalized.outcome}, ${settledEvent.outcome}, replay ${replayed.outcome}, next ${limited.outcome}`,
  };
}

// ---------------------------------------------------------------------------
// a2. (Derivado) La identidad en 521: Hotmart trae el movil con el 1, el
//     contacto y la identidad del caso quedan en 521, y el formulario en 52.
//     La compartida busca la intencion con el id exacto: la oferta por
//     defecto, sin el formulario, y una intencion fabricada. La portable la
//     encuentra por las dos formas.
// ---------------------------------------------------------------------------
{
  const CHATWOOT = 9_600_201;
  const lead = person('identidad-521');
  await cartTemplateSent(lead, { hotmartPhone: lead.whatsapp, chatwoot: CHATWOOT, templateMessage: 960_201 });
  const reply = await replyFromWhatsapp(lead, CHATWOOT);
  expectAdopted('a2', lead, reply, lead.whatsapp);
  await agentLink(lead, reply, CHATWOOT, { inbound: 960_203, outbound: 960_204 });
  const claimArgs = {
    chatwoot: CHATWOOT, userId: reply.userId, email: lead.email,
    regime: 'link_sent_no_purchase', inbound: 960_203, outbound: 960_204,
  };
  const start = await footprint(lead, CHATWOOT);
  const shared = await rolledBack(async () => {
    const row = await claimFollowup(SHARED_CLAIM, claimArgs);
    return {
      outcome: row.outcome,
      issuance: row.checkout_issuance_id === null ? null : await issuanceOf(row.checkout_issuance_id),
      footprint: await footprint(lead, CHATWOOT),
    };
  });
  const claimed = await claimFollowup(PORTABLE_CLAIM, claimArgs);
  const issuance = claimed.checkout_issuance_id === null ? null
    : await issuanceOf(claimed.checkout_issuance_id);
  const afterClaim = await footprint(lead, CHATWOOT);
  if (shared.outcome !== 'claimed'
      || shared.issuance.offer_resolution !== 'default_no_intent'
      || shared.issuance.offer_code !== defaultOffer.offer_code
      || shared.issuance.purchase_intent_id === lead.intent
      || shared.footprint.live_intents !== 2
      || claimed.outcome !== 'claimed'
      || issuance.offer_resolution !== 'lead_intent'
      || issuance.offer_code !== FORM_OFFER.offer_code
      || issuance.attribution_resolution !== 'full'
      || issuance.purchase_intent_id !== lead.intent
      || start.live_intents !== 1 || afterClaim.intents !== 1 || afterClaim.live_intents !== 1) {
    throw new Error(`a2. the identity in 521: ${JSON.stringify({ shared, claimed: claimed.outcome, issuance, start, afterClaim })}`);
  }
  results.a2_identity_in_521_derived = {
    shared_rolled_back: `${shared.outcome}:${shared.issuance.offer_resolution}:${shared.issuance.offer_code}, live intents ${shared.footprint.live_intents}`,
    portable: `${claimed.outcome}:${issuance.offer_resolution}:${issuance.offer_code}:${issuance.attribution_resolution}, live intents ${afterClaim.live_intents}`,
  };
}

// ---------------------------------------------------------------------------
// c. Una conversacion organica: la persona dejo el formulario y escribio por
//    su cuenta, sin ninguna plantilla nuestra. La admision portable la crea en
//    draft_only, sin evento de adopcion. Con manifiesto el cupon no sale ahi
//    (D4, la politica later_step).
// ---------------------------------------------------------------------------
{
  const CHATWOOT = 9_600_301;
  const lead = person('organica');
  await submitForm(lead);
  const reply = await replyFromWhatsapp(lead, CHATWOOT);
  if (reply.outcome !== 'created:draft_only' || reply.adopted !== 0
      || reply.userId !== lead.whatsapp || reply.identity !== lead.whatsapp) {
    throw new Error(`c. the organic conversation: ${JSON.stringify(reply)}`);
  }
  const claimArgs = {
    chatwoot: CHATWOOT, userId: reply.userId, email: lead.email,
    regime: 'went_quiet', inbound: 960_302, outbound: 960_303,
  };
  const start = await footprint(lead, CHATWOOT);
  const claimed = await claimFollowup(PORTABLE_CLAIM, claimArgs);
  const afterClaim = await footprint(lead, CHATWOOT);
  const shared = await rolledBack(async () => (await claimFollowup(SHARED_CLAIM, claimArgs)).outcome);
  if (claimed.outcome !== 'blocked_not_template_reply'
      || claimed.followup_event_id !== null || claimed.checkout_issuance_id !== null
      || !sameFootprint(start, afterClaim) || afterClaim.followups !== 0
      || shared !== 'claimed') {
    throw new Error(`c. the organic conversation: ${JSON.stringify({ claimed, start, afterClaim, shared })}`);
  }
  results.c_organic_conversation = `${claimed.outcome}, nothing written (shared rolled back: ${shared})`;
}

// ---------------------------------------------------------------------------
// d. La compra: la conversacion del carrito adoptada y despues la compra
//    aprobada por Hotmart con el movil con el 1 (precedente inline, sin
//    captura). La intencion del formulario (52) queda purchased.
// ---------------------------------------------------------------------------
{
  const CHATWOOT = 9_600_401;
  const lead = person('compro');
  await cartTemplateSent(lead, { hotmartPhone: lead.plain, chatwoot: CHATWOOT, templateMessage: 960_401 });
  const reply = await replyFromWhatsapp(lead, CHATWOOT);
  expectAdopted('d', lead, reply, lead.plain);
  const payload = {
    id: `att1-cupon-purchase-${lead.email}`,
    creation_date: PURCHASED_AT.getTime(),
    event: 'PURCHASE_APPROVED',
    version: '2.0.0',
    data: {
      product: { id: ATT1.productId, ucode: 'ATT1-CUPON-UCODE' },
      buyer: { email: lead.email, checkout_phone: `+${lead.whatsapp}` },
      purchase: {
        approved_date: PURCHASED_AT.getTime(),
        status: 'APPROVED',
        transaction: 'HPATT1CUPON001',
        offer: { code: lead.offer.offer_code },
      },
    },
  };
  const purchase = one((await db.query(`
    select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload),
    lead.email, lead.whatsapp])).rows, 'd purchase');
  const correlation = one((await db.query(`
    select outcome, purchase_intent_id from public.portable_hotmart_purchase_correlations
    where webhook_event_id = $1
  `, [purchase.webhook_event_id])).rows, 'd purchase correlation');
  const intent = await intentOf(lead.intent, 'd intent after the purchase');
  const start = await footprint(lead, CHATWOOT);
  const claimed = await claimFollowup(PORTABLE_CLAIM, {
    chatwoot: CHATWOOT, userId: await resolveInbound(lead.whatsapp), email: lead.email,
    regime: 'went_quiet', inbound: 960_402, outbound: 960_403,
  });
  const afterClaim = await footprint(lead, CHATWOOT);
  if (purchase.outcome !== 'inserted' || correlation.outcome !== 'resolved'
      || correlation.purchase_intent_id !== lead.intent || intent.lifecycle_state !== 'purchased'
      || claimed.outcome !== 'purchase_already_approved'
      || claimed.followup_event_id !== null || claimed.checkout_issuance_id !== null
      || !sameFootprint(start, afterClaim) || afterClaim.live_intents !== 0) {
    throw new Error(`d. a purchase with the 521: ${JSON.stringify({ purchase: purchase.outcome, correlation, intent: intent.lifecycle_state, claimed, start, afterClaim })}`);
  }
  results.d_purchase_with_the_521 = `${correlation.outcome}:${intent.lifecycle_state}, ${claimed.outcome}, nothing written`;
}

// ---------------------------------------------------------------------------
// e. La baja guardada bajo el 521 en otra conversacion de Chatwoot, despues de
//    la adopcion: la RPC real la deja unmatched (no hay identidad 521) y el
//    contacto conserva su permiso. La compartida mira solo esta conversacion y
//    el id exacto; la portable, las dos formas.
// ---------------------------------------------------------------------------
{
  const CHATWOOT = 9_600_501;
  const lead = person('baja');
  await cartTemplateSent(lead, { hotmartPhone: lead.plain, chatwoot: CHATWOOT, templateMessage: 960_501 });
  const reply = await replyFromWhatsapp(lead, CHATWOOT);
  expectAdopted('e', lead, reply, lead.plain);
  const optOut = one((await db.query(`
    select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,$6,'stop_receiving_messages')
  `, [ATT1.accountId, ATT1.inboxId, CHATWOOT + 1, 960_599, lead.whatsapp, await dbNow()])).rows,
  'e opt-out');
  const contact = one((await db.query(`
    select contact_permission, lifecycle_status from public.contacts where id = $1
  `, [lead.contact])).rows, 'e contact');
  const claimArgs = {
    chatwoot: CHATWOOT, userId: await resolveInbound(lead.whatsapp), email: lead.email,
    regime: 'went_quiet', inbound: 960_502, outbound: 960_503,
  };
  const start = await footprint(lead, CHATWOOT);
  const claimed = await claimFollowup(PORTABLE_CLAIM, claimArgs);
  const afterClaim = await footprint(lead, CHATWOOT);
  const shared = await rolledBack(async () => (await claimFollowup(SHARED_CLAIM, claimArgs)).outcome);
  if (optOut.outcome !== 'recorded_unmatched' || optOut.matched_contact_id !== null
      || ['opted_out', 'blocked', 'restricted'].includes(contact.contact_permission)
      || contact.lifecycle_status === 'do_not_contact'
      || claimArgs.userId !== lead.plain
      || claimed.outcome !== 'issuance_blocked_opt_out'
      || claimed.followup_event_id !== null || claimed.checkout_issuance_id !== null
      || !sameFootprint(start, afterClaim)
      || shared !== 'claimed') {
    throw new Error(`e. an opt-out under the 521 in another conversation: ${JSON.stringify({ optOut: optOut.outcome, contact, claimed, start, afterClaim, shared })}`);
  }
  results.e_opt_out_under_the_521_elsewhere = `${optOut.outcome}, ${claimed.outcome}, nothing written (shared rolled back: ${shared})`;
}

// ---------------------------------------------------------------------------
// f. Solo service_role ejecuta la portable (la foto del paso 0 ya lo mira por
//    has_function_privilege): anon y authenticated reciben 42501 al llamarla.
// ---------------------------------------------------------------------------
{
  const denied = {};
  for (const role of ['anon', 'authenticated']) {
    await db.exec(`set role ${role}`);
    let caught = null;
    try {
      await db.query(`
        select * from public.${PORTABLE_CLAIM}($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
      `, [9_600_601, ATT1.accountId, ATT1.inboxId, '5215555556199', null, 'followup:9600601:2',
        'went_quiet', TEMPLATE.name, TEMPLATE.language, COUPON, 1, 2, INBOUND_AGE_SECONDS,
        ulidAt(Date.now()), null]);
    } catch (error) {
      caught = error;
    } finally {
      await db.exec('reset role');
    }
    if (caught?.code !== '42501'
        || caught.message !== `permission denied for function ${PORTABLE_CLAIM}`) {
      throw new Error(`${role} executed the portable claim: ${caught?.code} ${caught?.message}`);
    }
    denied[role] = caught.code;
  }
  results.f_acl = { service_role: 'execute', ...denied, shared_claim_definition: 'identical' };
}

// ---------------------------------------------------------------------------
// g. La migracion otra vez, dentro de una transaccion que se deshace: sin la
//    reserva portable (la base de Johanna) avisa y no crea nada; una segunda
//    corrida deja todo identico; con la compartida cambiada falla con 55000.
//    El begin y el commit de la migracion se sacan para correrla adentro.
// ---------------------------------------------------------------------------
{
  const migration = readFileSync(join(root, 'supabase/migrations', TARGET), 'utf8');
  if (countOf(migration, '\nbegin;\n') !== 1 || !migration.trimEnd().endsWith('\ncommit;')) {
    throw new Error(`${TARGET} does not run in its own transaction any more`);
  }
  const inside = forPglite(migration)
    .replace('\nbegin;\n', '\n-- begin: la transaccion es la del validador\n')
    .replace(/\ncommit;\s*$/, '\n-- commit: lo decide el validador\n');
  const runMigration = async () => {
    const notices = [];
    await db.exec(inside, { onNotice: (notice) => notices.push(notice.message) });
    return notices;
  };
  const DROP_PORTABLE_CLAIM = `drop function public.${PORTABLE_SIGNATURE};`;
  const portableExists = async () => (await db.query(
    'select to_regprocedure($1) is not null as present', [`public.${PORTABLE_SIGNATURE}`],
  )).rows[0].present;

  // Sin la reserva portable.
  const withoutReserve = await rolledBack(async () => {
    await db.exec(`${DROP_PORTABLE_CLAIM} drop function public.${PORTABLE_RESERVE_SIGNATURE};`);
    const dropped = await snapshot();
    const notices = await runMigration();
    const change = diff(dropped, await snapshot());
    const row = await fingerprintOf();
    return { notices, change, created: await portableExists(), fingerprint: row?.fingerprint_status, markers: row?.present_markers };
  });
  if (withoutReserve.notices.length !== 1
      || !withoutReserve.notices[0].startsWith('portable_conversation_followup_claim: ')
      || !withoutReserve.notices[0].endsWith('no se creo nada')
      || !unchanged(withoutReserve.change) || withoutReserve.created !== false
      || withoutReserve.fingerprint !== 'fingerprint_absent' || withoutReserve.markers !== 0) {
    throw new Error(`g. without the portable reserve: ${JSON.stringify(withoutReserve)}`);
  }

  // Una segunda corrida: la compartida no cambio, asi que recrea la misma
  // funcion con los mismos permisos, sin avisos.
  const rerun = await rolledBack(async () => {
    const notices = await runMigration();
    return { notices, change: diff(settled, await snapshot()) };
  });
  if (rerun.notices.length !== 0 || !unchanged(rerun.change)) {
    throw new Error(`g. a second run changed something: ${JSON.stringify(rerun)}`);
  }

  // Con la compartida cambiada (alguien ya la apunto a la reserva portable),
  // la migracion falla con 55000. El error aborta la sentencia que iba a
  // crear la funcion: si no falla, se anota si la creo, para el diagnostico.
  const altered = await rolledBack(async () => {
    await db.exec(DROP_PORTABLE_CLAIM);
    const shared = one((await db.query(
      'select pg_get_functiondef(to_regprocedure($1)) as definition', [`public.${SHARED_SIGNATURE}`],
    )).rows, 'shared definition').definition;
    await db.exec(shared.replace(
      'from public.reserve_chatwoot_checkout_issuance_v2(',
      'from public.reserve_portable_checkout_issuance_v2(',
    ));
    await db.exec('savepoint altered_shared');
    let caught = null;
    let createdWithoutError = null;
    try {
      await runMigration();
      createdWithoutError = await portableExists();
    } catch (error) {
      caught = error;
    }
    await db.exec('rollback to savepoint altered_shared');
    return { code: caught?.code, message: caught?.message, createdWithoutError };
  });
  if (altered.code !== '55000' || altered.message !== 'unexpected_conversation_followup_claim_definition') {
    throw new Error(`g. with the shared claim altered: ${JSON.stringify(altered)}`);
  }
  results.g_migration_runs = {
    without_portable_reserve: `notice, nothing created, ${withoutReserve.fingerprint}`,
    second_run: 'identical',
    altered_shared: `${altered.code} ${altered.message}`,
  };
}

// Lo que se deshizo quedo deshecho: las funciones, sus definiciones y sus
// permisos son los de la cadena entera.
{
  const change = diff(settled, await snapshot());
  if (!unchanged(change)) {
    throw new Error(`the validator left the functions changed: ${JSON.stringify(change)}`);
  }
}

console.log(JSON.stringify({ portable_conversation_followup: 'OK', ...results }));
await db.close();
