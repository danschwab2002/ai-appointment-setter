// El primer contacto de la ficha de pago (C1, migracion 20261019000100), con
// las RPC reales sobre la cadena completa de migraciones.
//
// Prueba:
//   0. la migracion: agrega exactamente las nueve funciones nuevas, cambia
//      solo las tres de la correlacion que parcha en el lugar (y cada una es
//      su definicion anterior con el reemplazo documentado, nada mas), las
//      cuatro derivadas son la definicion viva del pago fallido con los
//      reemplazos contados, y el md5 de pg_get_functiondef del pago fallido,
//      del carrito, del piloto y del correlador de Johanna es el mismo con y
//      sin la migracion; las cinco entradas son solo de service_role y lo
//      demas de nadie;
//   1. admit_portable_hotmart_payment_slip_v2 admite las 13 fichas capturadas:
//      las 5 en efectivo de Mexico, con la intencion de su formulario, quedan
//      con la correlacion resolved y los hechos crudos en
//      commercial_ally_payment_slip_details; la intencion sigue esperando la
//      compra y sin clasificar; las otras 8 se admiten sin intencion
//      (unmatched). Replay, replay cambiado, otra oferta, otro producto, la
//      identidad cambiada y la ficha que ya guardo la v1;
//   2. la v1 sigue igual para PURCHASE_EXPIRED;
//   3. el plan: caso, secuencia y accion con step_key
//      payment_slip_first_contact, anchor_type payment_slip y
//      due_at = creacion + gracia (60 min), el permiso de la intencion
//      consentida, el binding del scope de la ficha; con el scope de la
//      ficha inactivo, o contra el scope del carrito, no se planifica;
//   4. una segunda ficha de la misma persona se agrega al caso: no crea otro
//      mensaje ni adelanta la hora; el contexto de envio da la mas reciente;
//   5. el envio de punta a punta con la reevaluacion REAL: claim,
//      reevaluate_followup_action, contexto de ejecucion y de la ficha,
//      reserva approved_template, mark_portable_payment_slip_request_started
//      (autoriza contra el scope de la ficha con PURCHASE_BILLET_PRINTED; el
//      arranque del pago fallido la rechaza) y la aceptacion;
//   6. la baja en Chatwoot antes del envio lo frena sin consumir cupo;
//   7. una compra (admit_portable_hotmart_purchase_approved +
//      apply_hotmart_purchase_approved) cierra el caso de la ficha;
//   8. el estado del runtime del scope de la ficha;
//   9. H3, documentado y NO arreglado: un carrito posterior con un caso de la
//      ficha abierto, por el planificador del piloto del carrito.
//
// Datos:
//   - Binding, ofertas, producto, Chatwoot y consentimiento: tests/fixtures/
//     instances/att1/instancia.toml. Lookback de la intencion, politica de
//     compra y el scope del carrito (caso 9): politica-piloto.json, al lado.
//   - Las fichas: tests/fixtures/hotmart_payment_slip_printed_att1_20261018.json
//     (capturadas de la base de ATT1 el 2026-10-18, anonimizadas). DERIVACION
//     EXPLICITA: creation_date y order_date se corren juntos (la misma
//     diferencia entre los dos) para que la ficha caiga a una distancia fija
//     del reloj de la base; la gracia, el vencimiento y la puerta de arranque
//     (+-5 min del reloj) lo exigen. Los casos 4 y 9 re-identifican una ficha
//     capturada (id, comprador, transaccion) para que sea de la misma persona.
//     billet_url, billet_barcode, medio, pais, oferta y producto quedan tal
//     cual llegaron.
//   - La compra del caso 7: tests/fixtures/hotmart_cash_payment_approved_att1_
//     20261012.json (la compra real pagada en OXXO), re-identificada y con la
//     hora corrida. El carrito del caso 9: hotmart_cart_abandonment_rejected_
//     v1.json con producto, oferta, id, fecha y comprador sustituidos, como
//     validate_att1_portable_chain.mjs.
//   - El formulario: el precedente inline de validate_att1_portable_chain.mjs
//     (contrato lead.precheckout 1.1.0 con consentimiento), con el movil
//     mexicano de cada ficha.
//   - La politica y el scope de la ficha no existen todavia en la instancia
//     (los publica despliegue/base/publicar-ficha-v1.sql, segundo PR): aca van
//     los valores del diseno (att1-ficha-un-toque, gracia 60 min, un paso
//     payment_slip_first_contact; att1-ficha con PURCHASE_BILLET_PRINTED y
//     audiencia consented_intent). DESVIACION DOCUMENTADA, la misma de la
//     cadena de ATT1: la ventana de envio es de 00:00 a 23:59 en vez de
//     09:00-21:00 de Ciudad de Mexico, porque la puerta de arranque exige el
//     reloj real.
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
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
const TARGET = '20261019000100_portable_payment_slip_first_contact_v1.sql';
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
         md5(pg_get_functiondef(p.oid)) as md5,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));

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

const ENTRYPOINTS = [
  'admit_portable_hotmart_payment_slip_v2(text,text,integer,text,jsonb,text,text)',
  'plan_portable_payment_slip_recovery(uuid,uuid,text,text,text,text,integer,timestamp with time zone,bigint,bigint,text,text,integer)',
  'mark_portable_payment_slip_request_started(uuid,uuid,text,bigint,timestamp with time zone)',
  'get_portable_payment_slip_pilot_runtime_status(text,integer,text,text,text)',
  'get_portable_payment_slip_send_context(uuid,text,bigint,timestamp with time zone)',
];
const PRIVATE = [
  'plan_payment_slip_recovery(uuid,uuid,text,text,text,text,integer,timestamp with time zone)',
  'plan_payment_slip_recovery_with_identity(uuid,uuid,text,text,text,text,integer,timestamp with time zone,bigint,bigint,text)',
  'hotmart_payment_slip_payload_is_processable(text,jsonb)',
  'protect_commercial_ally_payment_slip_evidence()',
];
const PATCHED = [
  '_hotmart_purchase_intent_payload_identity(text,jsonb)',
  'hotmart_purchase_intent_payload_is_processable(uuid)',
  '_correlate_portable_hotmart_purchase_intent(uuid)',
];
// Las que no pueden cambiar (el pago fallido, el carrito, el piloto y el
// correlador de Johanna), ademas del chequeo general de abajo.
const UNCHANGED = [
  'plan_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamp with time zone)',
  'plan_payment_failure_recovery_with_identity(uuid,uuid,text,text,text,text,integer,timestamp with time zone,bigint,bigint,text)',
  'plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamp with time zone,bigint,bigint,text,text,integer)',
  'mark_portable_payment_failure_request_started(uuid,uuid,text,bigint,timestamp with time zone)',
  'admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text)',
  'mark_lancemos_pilot_request_started(uuid,uuid,text,bigint,timestamp with time zone)',
  'plan_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamp with time zone)',
  'reevaluate_followup_action(uuid,text,bigint,timestamp with time zone,boolean,text,text,timestamp with time zone,text,boolean,boolean,boolean,boolean,boolean)',
  'evaluate_lancemos_pilot_scope(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid)',
  'authorize_lancemos_pilot_request_start(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid,uuid,uuid,timestamp with time zone)',
  'get_lancemos_pilot_runtime_status(text,integer,text,text,text)',
  'correlate_hotmart_purchase_intent(uuid)',
  'admit_portable_hotmart_payment_slip_event_v1(text,text,integer,text,jsonb)',
];
const sameSet = (left, right) => JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
const added = [...after.keys()].filter((signature) => !before.has(signature));
const removed = [...before.keys()].filter((signature) => !after.has(signature));
const changed = [...before.keys()].filter((signature) => after.has(signature)
  && (after.get(signature).md5 !== before.get(signature).md5
      || after.get(signature).service_x !== before.get(signature).service_x
      || after.get(signature).api_x !== before.get(signature).api_x));
if (!sameSet(added, [...ENTRYPOINTS, ...PRIVATE]) || removed.length !== 0
    || !sameSet(changed, PATCHED)) {
  throw new Error(`the migration changed something else: ${JSON.stringify({ added, removed, changed })}`);
}
const md5Regression = {};
for (const signature of UNCHANGED) {
  const old = before.get(signature);
  const now = after.get(signature);
  if (!old || !now || old.md5 !== now.md5) {
    throw new Error(`${signature} changed: ${old?.md5} -> ${now?.md5}`);
  }
  md5Regression[signature.split('(')[0]] = now.md5;
}
for (const signature of ENTRYPOINTS) {
  const row = after.get(signature);
  if (row.service_x !== true || row.api_x !== false) {
    throw new Error(`${signature} is not service_role only: ${JSON.stringify({ service_x: row.service_x, api_x: row.api_x })}`);
  }
}
for (const signature of PRIVATE) {
  const row = after.get(signature);
  if (row.service_x !== false || row.api_x !== false) {
    throw new Error(`${signature} is executable by an API role: ${JSON.stringify({ service_x: row.service_x, api_x: row.api_x })}`);
  }
}
for (const signature of PATCHED) {
  // create or replace conserva el ACL: el parche no lo toca.
  if (after.get(signature).service_x !== before.get(signature).service_x
      || after.get(signature).api_x !== before.get(signature).api_x) {
    throw new Error(`${signature} changed its ACL`);
  }
}
const tableAcl = one((await db.query(`
  select c.relrowsecurity as rls,
         exists (
           select 1
           from unnest(array['anon', 'authenticated', 'service_role']) role_name
           cross join unnest(array[
             'select', 'insert', 'update', 'delete', 'truncate', 'references', 'trigger'
           ]) privilege_name
           where has_table_privilege(role_name, c.oid, privilege_name)
         ) as api_privilege
  from pg_class c
  where c.oid = to_regclass('public.commercial_ally_payment_slip_details')
`)).rows, 'slip details ACL');
if (tableAcl.rls !== true || tableAcl.api_privilege !== false) {
  throw new Error(`slip details table ACL: ${JSON.stringify(tableAcl)}`);
}

// Cada parche es la definicion anterior con su reemplazo y nada mas.
const count = (text, needle) => text.split(needle).length - 1;
const replaceExactly = (label, text, replacements) => {
  let out = text;
  const counts = [];
  for (const [old, next, expected] of replacements) {
    const found = count(text, old);
    counts.push(found);
    if (found !== expected) throw new Error(`${label}: "${old}" appears ${found} times, expected ${expected}`);
    out = out.split(old).join(next);
  }
  return { out, counts };
};
const patchProof = {
  '_hotmart_purchase_intent_payload_identity(text,jsonb)': [[
    "    elsif p_event_type = 'PURCHASE_CANCELED' then\n",
    "    elsif p_event_type in ('PURCHASE_CANCELED', 'PURCHASE_BILLET_PRINTED') then\n",
    1,
  ]],
  'hotmart_purchase_intent_payload_is_processable(uuid)': [[
    '    return false;\nend;',
    "    if v_event.event_type = 'PURCHASE_BILLET_PRINTED' then\n"
      + '        return coalesce(public.hotmart_payment_slip_payload_is_processable(\n'
      + '            v_event.external_event_id,\n'
      + '            v_event.payload\n'
      + '        ), false);\n'
      + '    end if;\n'
      + '    return false;\nend;',
    1,
  ]],
  '_correlate_portable_hotmart_purchase_intent(uuid)': [
    [
      "    elsif v_event_type = 'PURCHASE_CANCELED' then\n        v_offer_ref",
      "    elsif v_event_type in ('PURCHASE_CANCELED', 'PURCHASE_BILLET_PRINTED') then\n        v_offer_ref",
      1,
    ],
    [
      "        elsif v_event_type = 'PURCHASE_CANCELED' then\n            update public.purchase_intents",
      "        elsif v_event_type = 'PURCHASE_BILLET_PRINTED' then\n"
        + '            -- payment_slip_first_contact: begin\n'
        + '            -- La ficha no es un abandono ni un pago rechazado: la intencion\n'
        + '            -- sigue esperando la compra y su clasificacion no cambia.\n'
        + '            perform 1\n'
        + '            from public.purchase_intents\n'
        + '            where id = v_resolved_intent_id\n'
        + "              and lifecycle_state = 'waiting_for_purchase';\n"
        + '            -- payment_slip_first_contact: end\n'
        + "        elsif v_event_type = 'PURCHASE_CANCELED' then\n            update public.purchase_intents",
      1,
    ],
  ],
};
for (const [signature, replacements] of Object.entries(patchProof)) {
  const { out } = replaceExactly(signature, before.get(signature).definition, replacements);
  if (out !== after.get(signature).definition) {
    throw new Error(`${signature} is not its previous definition plus the documented patch`);
  }
}

// Las cuatro derivadas: la definicion viva del pago fallido, medida aca, con
// los reemplazos contados de la migracion, da exactamente la derivada.
const DERIVATIONS = [
  {
    source: UNCHANGED[0], target: PRIVATE[0],
    replacements: [
      ['public.plan_payment_failure_recovery(', 'public.plan_payment_slip_recovery(', 1],
      ["'PURCHASE_CANCELED'", "'PURCHASE_BILLET_PRINTED'", 1],
      ["'payment_failure'", "'payment_slip'", 10],
      ["'payment_failure_", "'payment_slip_", 4],
      ["'payment_failure:", "'payment_slip:", 2],
      ["'latest_payment_failure_", "'latest_payment_slip_", 4],
      ["'repeated_payment_failure'", "'repeated_payment_slip'", 1],
      ['then least(due_at, now())', 'then due_at', 1],
      ["when v_sequence_status = 'active' then null", "when v_sequence_status = 'active' then next_attempt_at", 1],
    ],
  },
  {
    source: UNCHANGED[1], target: PRIVATE[1],
    replacements: [
      ['public.plan_payment_failure_recovery_with_identity(', 'public.plan_payment_slip_recovery_with_identity(', 1],
      ['public.plan_payment_failure_recovery(', 'public.plan_payment_slip_recovery(', 1],
      ["'PURCHASE_CANCELED'", "'PURCHASE_BILLET_PRINTED'", 0],
      ["'payment_failure'", "'payment_slip'", 0],
    ],
  },
  {
    source: UNCHANGED[2], target: ENTRYPOINTS[1],
    replacements: [
      ['public.plan_portable_payment_failure_recovery(', 'public.plan_portable_payment_slip_recovery(', 1],
      ['public.plan_payment_failure_recovery_with_identity(', 'public.plan_payment_slip_recovery_with_identity(', 1],
      ['public.commercial_ally_payment_failure_details', 'public.commercial_ally_payment_slip_details', 2],
      ['v_payment_failure', 'v_payment_slip', 3],
      ["'PURCHASE_CANCELED'", "'PURCHASE_BILLET_PRINTED'", 3],
      ["'payment_failure'", "'payment_slip'", 1],
      ["'payment_failure_", "'payment_slip_", 7],
    ],
  },
  {
    source: UNCHANGED[3], target: ENTRYPOINTS[2],
    replacements: [
      ['public.mark_portable_payment_failure_request_started(', 'public.mark_portable_payment_slip_request_started(', 1],
      ["'PURCHASE_CANCELED'", "'PURCHASE_BILLET_PRINTED'", 1],
      ["'payment_failure'", "'payment_slip'", 1],
      ["'payment_failure_", "'payment_slip_", 1],
    ],
  },
];
const derivationCounts = {};
for (const { source, target, replacements } of DERIVATIONS) {
  const { out, counts } = replaceExactly(source, before.get(source).definition, replacements);
  if (out !== after.get(target).definition) {
    throw new Error(`${target} is not the live ${source} with the counted replacements`);
  }
  if (count(out, 'payment_failure') !== 0 || count(out, 'PURCHASE_CANCELED') !== 0) {
    throw new Error(`${target} still names the payment failure`);
  }
  derivationCounts[target.split('(')[0]] = Object.fromEntries(
    replacements.map(([old], index) => [old, counts[index]]),
  );
}
results.migration = {
  added: added.length, patched: changed.length, unchanged: before.size - changed.length,
  derivation_counts: derivationCounts,
};
results.md5_unchanged = md5Regression;

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
const ALL_DAY = (windows) => windows.map((window) => ({ ...window, start: '00:00', end: '23:59' }));

// La ficha: valores del diseno (C1), con la desviacion de la ventana.
const SLIP = {
  scope: 'att1-ficha',
  scopeVersion: 1,
  policy: 'att1-ficha-un-toque',
  policyVersion: 1,
  graceMinutes: 60,
  expiresAfter: '1 day',
  step: 'payment_slip_first_contact',
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

const insertPolicy = (key, version, grace, expires, steps, windows) => db.query(`
  insert into public.followup_policy_versions
    (policy_key, version, status, purpose, timezone, business_windows,
     grace_period, expires_after, max_automatic_messages, steps,
     approved_by, approved_at, published_at)
  values ($1,$2,'published','cart_recovery',$3,$4::jsonb,$5::interval,$6::interval,1,$7::jsonb,
          'operator-test',now(),now())
`, [key, version, ATT1.timezone, JSON.stringify(windows), grace, expires, JSON.stringify(steps)]);
const insertScope = (key, version, eventType, additionalTypes, policy, policyVersion, audience) => db.query(`
  insert into public.pilot_scope_versions
    (scope_key, version, status, tenant_key, chatwoot_account_id, chatwoot_inbox_id,
     channel, channel_provider, channel_account_ref, source, source_event_type,
     additional_source_event_types, external_product_id, offer_code,
     additional_offer_codes, purpose, policy_key, policy_version, timezone,
     max_cohort_contacts, max_outbound_request_starts_total,
     max_outbound_request_starts_per_day, audience_mode,
     approved_by, approved_at, published_at)
  values ($1,$2,'published',$3,$4,$5,'whatsapp',$6,$7,'hotmart',$8,$9::text[],$10,$11,
          $12::text[],'cart_recovery',$13,$14,$15,20,20,20,$16,'operator-test',now(),now())
`, [key, version, ATT1.tenant, ATT1.accountId, ATT1.inboxId, CHANNEL_PROVIDER, CHANNEL_REF,
  eventType, additionalTypes, String(ATT1.productId), defaultOffer.offer_code,
  additionalOffers.map((offer) => offer.offer_code), policy, policyVersion, ATT1.timezone,
  audience]);
const insertControl = (key, version) => db.query(`
  insert into public.pilot_runtime_controls
    (scope_key, scope_version, runtime_state, generation, changed_by, change_reason)
  values ($1,$2,'inactive',0,'operator-test','default-off')
`, [key, version]);

await insertPolicy(SLIP.policy, SLIP.policyVersion, `${SLIP.graceMinutes} minutes`,
  // El modo del paso es freeform como en la politica de la instancia
  // (politica-piloto.json): reevaluate_followup_action escala con
  // channel_mode_unsupported un paso approved_template, y el bridge reserva en
  // approved_template porque el scope es waba.
  SLIP.expiresAfter, [{ step_key: SLIP.step, mode: 'freeform' }],
  ALL_DAY(cartPolicy.business_windows));
await insertScope(SLIP.scope, SLIP.scopeVersion, 'PURCHASE_BILLET_PRINTED', [],
  SLIP.policy, SLIP.policyVersion, 'consented_intent');
await insertControl(SLIP.scope, SLIP.scopeVersion);
// El scope del carrito de la instancia (caso 3, contra el scope equivocado, y
// caso 9), con su politica de un toque.
await insertPolicy(cartPolicy.policy_key, cartPolicy.version, cartPolicy.grace_period,
  cartPolicy.expires_after, cartPolicy.steps, ALL_DAY(cartPolicy.business_windows));
await insertScope(cartScope.scope_key, cartScope.version, cartScope.source_event_type,
  cartScope.additional_source_event_types, cartPolicy.policy_key, cartPolicy.version,
  'consented_intent');
await insertControl(cartScope.scope_key, cartScope.version);

// ---------------------------------------------------------------------------
// Las capturas y las derivaciones.
// ---------------------------------------------------------------------------
const fixture = (name) => JSON.parse(readFileSync(join(root, 'tests/fixtures', name), 'utf8'));
const SLIPS = fixture('hotmart_payment_slip_printed_att1_20261018.json').eventos.map((event) => event.payload);
const MX_CASH = SLIPS.filter((payload) => payload.data.purchase.payment.type === 'CASHPAYMENT'
  && payload.data.purchase.checkout_country.iso === 'MX');
const CASH_APPROVED = fixture('hotmart_cash_payment_approved_att1_20261012.json').eventos[0].payload;
const CAPTURED_CART = fixture('hotmart_cart_abandonment_rejected_v1.json').payload;
if (SLIPS.length !== 13 || MX_CASH.length !== 5
    || MX_CASH.some((payload) => !payload.data.purchase.payment.billet_url
      .startsWith('https://customer-local-latam.ebanx.com/print/oxxo?hash=')
      || payload.data.purchase.payment.billet_barcode.length !== 34
      || payload.data.product.id !== ATT1.productId)) {
  throw new Error(`the captures changed: ${SLIPS.length} slips, ${MX_CASH.length} MX cash`);
}

const dbNow = async () => new Date((await db.query('select clock_timestamp() as now')).rows[0].now);
const baseMs = Math.floor((await dbNow()).getTime() / 1000) * 1000;
const at = (minutes) => new Date(baseMs + minutes * 60_000);
const digits = (value) => String(value ?? '').replace(/\D/g, '');

// La ficha corrida en el tiempo: creation_date a la hora pedida y order_date a
// la misma distancia que tenia de creation_date.
const shifted = (payload, when) => {
  const copy = structuredClone(payload);
  const gap = payload.creation_date - payload.data.purchase.order_date;
  copy.creation_date = when.getTime();
  copy.data.purchase.order_date = when.getTime() - gap;
  return copy;
};
// La misma ficha capturada, de otra persona (id, comprador y transaccion).
const reidentified = (payload, lead, suffix) => {
  const copy = structuredClone(payload);
  copy.id = `${payload.id}-${suffix}`;
  copy.data.buyer = { ...copy.data.buyer, email: lead.email, checkout_phone: lead.phone };
  copy.data.purchase.transaction = `${payload.data.purchase.transaction}${suffix.toUpperCase().replace(/[^A-Z0-9]/g, '')}`;
  return copy;
};
const leadOf = (payload) => ({
  email: payload.data.buyer.email,
  phone: digits(payload.data.buyer.checkout_phone),
  name: payload.data.buyer.name,
  offer: offerByCode(payload.data.purchase.offer.code),
});

const admitSlip = async (payload, { email, phone } = {}) => one((await db.query(`
  select * from public.admit_portable_hotmart_payment_slip_v2($1,$2,$3,$4,$5::jsonb,$6,$7)
`, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload),
  email ?? payload.data.buyer.email.toLowerCase(),
  phone ?? digits(payload.data.buyer.checkout_phone)])).rows, `slip ${payload.id}`);
const detailOf = async (eventId) => one((await db.query(`
  select * from public.commercial_ally_payment_slip_details where webhook_event_id = $1
`, [eventId])).rows, 'slip detail');
const intentOf = async (id) => one((await db.query(`
  select lifecycle_state, current_classification, normalized_phone
  from public.purchase_intents where id = $1
`, [id])).rows, 'intent');
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

// El formulario con consentimiento (precedente inline de la cadena de ATT1)
// con el movil mexicano de la ficha: deja la intencion en 52 + 10.
const admitForm = async (lead, submittedAt) => {
  const offer = lead.offer;
  const id = `slip-form-${lead.email}-${offer.offer_code}`;
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
  return admitted.purchase_intent_id;
};
// Lo que deja resolve_event antes de planificar: el contacto y sus puntos.
const createContact = async (lead, eventId) => {
  lead.contact = one((await db.query(`
    insert into public.contacts (full_name, email, phone, country_iso)
    values ($1,$2,$3,'MX') returning id
  `, [lead.name, lead.email, lead.phone])).rows, `contact ${lead.email}`).id;
  await db.query(`
    insert into public.contact_points
      (contact_id, type, raw_value, normalized_value, source, source_event_id)
    values ($1,'email',$2,$2,'hotmart',$4), ($1,'phone',$3,$3,'hotmart',$4)
  `, [lead.contact, lead.email, lead.phone, eventId]);
};
// Los mismos argumentos que va a armar resolution.resolve_event.
const planSlip = (lead, slip, { scope = SLIP.scope, version = SLIP.scopeVersion } = {}) => db.query(`
  select * from public.plan_portable_payment_slip_recovery(
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
  )
`, [slip.eventId, lead.contact, String(ATT1.productId), ATT1.productName,
  lead.offer.offer_code, SLIP.policy, SLIP.policyVersion, slip.createdAt.toISOString(),
  ATT1.accountId, ATT1.inboxId, lead.phone, scope, version]);
const actionOf = async (id) => one((await db.query(`
  select status, step_key, anchor_type, due_at, expires_at, next_attempt_at,
         anchor_subject_internal_id, terminal_reason
  from public.scheduled_actions where id = $1
`, [id])).rows, 'action');
const caseActions = async (caseId) => (await db.query(`
  select id from public.scheduled_actions where recovery_case_id = $1
`, [caseId])).rows;
const starts = async (scope) => one((await db.query(`
  select count(*)::integer as count from public.pilot_outbound_request_authorizations
  where scope_key = $1
`, [scope])).rows, 'starts').count;
const openActions = async () => (await db.query(`
  select id from public.scheduled_actions where status in ('pending','deferred','retryable_failed')
`)).rows;

// Una persona por ficha capturada en efectivo de Mexico: su formulario, su
// ficha corrida a la hora pedida.
const people = new Map();
const slipFor = async (payload, createdAt, { form = true } = {}) => {
  const lead = people.get(payload.data.buyer.email) ?? leadOf(payload);
  people.set(lead.email, lead);
  if (form && lead.intent === undefined) {
    lead.intent = await admitForm(lead, new Date(createdAt.getTime() - 30 * 60_000));
  }
  const body = shifted(payload, createdAt);
  const admitted = await admitSlip(body);
  return { lead, body, admitted, eventId: admitted.webhook_event_id, createdAt };
};

// ---------------------------------------------------------------------------
// 1. Las 13 fichas por la admision v2.
// ---------------------------------------------------------------------------
// Las 5 de Mexico, a distintas distancias del reloj (los casos de abajo las
// usan): A se envia, B recibe una segunda ficha, C se da de baja, D compra y E
// es el carrito del H3. B es una de las cuatro de 2uafw5bg: su segunda ficha
// es otra captura de la misma oferta.
const [A_SLIP, C_SLIP, B_SLIP, D_SLIP, E_SLIP] = MX_CASH;
const mx = {
  A: await slipFor(A_SLIP, at(-70)),
  B: await slipFor(B_SLIP, at(-10)),
  C: await slipFor(C_SLIP, at(-10)),
  D: await slipFor(D_SLIP, at(-10)),
  E: await slipFor(E_SLIP, at(-10)),
};
for (const [label, slip] of Object.entries(mx)) {
  const detail = await detailOf(slip.eventId);
  const payment = slip.body.data.purchase.payment;
  const intent = await intentOf(slip.lead.intent);
  if (slip.admitted.outcome !== 'inserted'
      || detail.trigger_kind !== 'payment_slip'
      || detail.correlation_outcome !== 'resolved'
      || detail.purchase_intent_id !== slip.lead.intent
      || detail.payment_type !== 'CASHPAYMENT'
      || detail.checkout_country_iso !== 'MX'
      || detail.billet_url !== payment.billet_url
      || detail.billet_barcode !== payment.billet_barcode
      || detail.transaction_ref !== slip.body.data.purchase.transaction
      || detail.generated_at.getTime() !== slip.body.data.purchase.order_date
      || intent.lifecycle_state !== 'waiting_for_purchase'
      || intent.current_classification !== null) {
    throw new Error(`MX slip ${label}: ${JSON.stringify({ outcome: slip.admitted.outcome, detail, intent })}`);
  }
}
const correlation = one((await db.query(`
  select event_type, outcome, matched_by, purchase_intent_id
  from public.hotmart_purchase_intent_correlations where webhook_event_id = $1
`, [mx.A.eventId])).rows, 'A correlation');
if (correlation.event_type !== 'PURCHASE_BILLET_PRINTED' || correlation.outcome !== 'resolved'
    || correlation.matched_by !== 'email_and_phone' || correlation.purchase_intent_id !== mx.A.lead.intent) {
  throw new Error(`A correlation: ${JSON.stringify(correlation)}`);
}
// Las otras 8 (Mercado Pago, Nequi, Yape y la de Chile): se admiten y se
// cruzan igual (el filtro por medio y pais va en el worker); sin formulario
// quedan unmatched.
const others = [];
for (const payload of SLIPS.filter((candidate) => !MX_CASH.includes(candidate))) {
  const slip = await slipFor(payload, at(-15), { form: false });
  const detail = await detailOf(slip.eventId);
  if (slip.admitted.outcome !== 'inserted'
      || detail.correlation_outcome !== 'unmatched'
      || detail.purchase_intent_id !== null
      || detail.payment_type !== payload.data.purchase.payment.type
      || detail.checkout_country_iso !== payload.data.purchase.checkout_country.iso
      || (detail.billet_url !== null) !== Boolean(payload.data.purchase.payment.billet_url)) {
    throw new Error(`other slip ${payload.id}: ${JSON.stringify(detail)}`);
  }
  others.push(`${detail.payment_type}/${detail.checkout_country_iso}`);
}
{
  const replay = await admitSlip(mx.A.body);
  const changedReplay = structuredClone(mx.A.body);
  changedReplay.data.purchase.payment.billet_barcode = '0'.repeat(34);
  const conflict = await admitSlip(changedReplay);
  const otherOffer = structuredClone(mx.A.body);
  otherOffer.id = `${otherOffer.id}-other-offer`;
  otherOffer.data.purchase.offer.code = 'zzzzzzzz';
  const otherProduct = structuredClone(mx.A.body);
  otherProduct.id = `${otherProduct.id}-other-product`;
  otherProduct.data.product.id = 1;
  const ignoredOffer = await admitSlip(otherOffer);
  const ignoredProduct = await admitSlip(otherProduct);
  if (replay.outcome !== 'duplicate' || replay.webhook_event_id !== mx.A.eventId
      || conflict.outcome !== 'semantic_conflict' || conflict.webhook_event_id !== mx.A.eventId
      || ignoredOffer.outcome !== 'ignored_binding_mismatch' || ignoredOffer.webhook_event_id !== null
      || ignoredProduct.outcome !== 'ignored_binding_mismatch') {
    throw new Error(`slip admission edges: ${JSON.stringify({ replay, conflict, ignoredOffer, ignoredProduct })}`);
  }
  await expectError('identity that is not the payload one', () => admitSlip(
    { ...structuredClone(mx.A.body), id: `${mx.A.body.id}-identity` },
    { email: 'otra@example.test' },
  ), { code: '22023', message: 'portable_hotmart_payment_slip_identity_mismatch' });
  const expired = structuredClone(mx.A.body);
  expired.event = 'PURCHASE_EXPIRED';
  expired.data.purchase.status = 'EXPIRED';
  expired.id = `${expired.id}-expired-v2`;
  await expectError('an expired slip through the v2', () => admitSlip(expired),
    { code: '22023', message: 'invalid_portable_hotmart_payment_slip_input' });
  results.admission = {
    mexico_cash_resolved: Object.keys(mx).length,
    others_unmatched: others.length,
    replay: replay.outcome, changed_replay: conflict.outcome,
    other_offer_or_product: ignoredOffer.outcome, expired_in_v2: '22023',
  };
}

// ---------------------------------------------------------------------------
// 2. La v1 sigue igual para PURCHASE_EXPIRED, y una ficha que guardo la v1 no
//    se reprocesa por la v2.
// ---------------------------------------------------------------------------
{
  const admitV1 = async (payload) => one((await db.query(`
    select * from public.admit_portable_hotmart_payment_slip_event_v1($1,$2,$3,$4,$5::jsonb)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, payload.id, JSON.stringify(payload)])).rows,
  `v1 ${payload.id}`);
  const expired = shifted(structuredClone(D_SLIP), at(-5));
  expired.id = `${D_SLIP.id}-expired`;
  expired.event = 'PURCHASE_EXPIRED';
  expired.data.purchase.status = 'EXPIRED';
  const first = await admitV1(expired);
  const again = await admitV1(expired);
  const stored = one((await db.query(`
    select event.event_type, event.processing_status,
           exists (select 1 from public.commercial_ally_hotmart_event_bindings provenance
                   where provenance.webhook_event_id = event.id) as provenance,
           exists (select 1 from public.commercial_ally_payment_slip_details details
                   where details.webhook_event_id = event.id) as detail,
           exists (select 1 from public.hotmart_purchase_intent_correlations correlation
                   where correlation.webhook_event_id = event.id) as correlated
    from public.webhook_events event where event.id = $1
  `, [first.webhook_event_id])).rows, 'v1 expired');
  if (first.outcome !== 'inserted' || again.outcome !== 'duplicate'
      || stored.event_type !== 'PURCHASE_EXPIRED' || stored.processing_status !== 'received'
      || !stored.provenance || stored.detail || stored.correlated) {
    throw new Error(`the v1 changed for PURCHASE_EXPIRED: ${JSON.stringify({ first, again, stored })}`);
  }
  const legacy = shifted(structuredClone(OTHERS_FIRST()), at(-20));
  legacy.id = `${legacy.id}-legacy-v1`;
  const legacyV1 = await admitV1(legacy);
  const legacyV2 = await admitSlip(legacy);
  if (legacyV1.outcome !== 'inserted' || legacyV2.outcome !== 'ignored_legacy_slip'
      || legacyV2.webhook_event_id !== legacyV1.webhook_event_id) {
    throw new Error(`a v1 slip replayed by the v2: ${JSON.stringify({ legacyV1, legacyV2 })}`);
  }
  results.v1 = { expired: `${first.outcome}/${again.outcome}`, legacy_slip_in_v2: legacyV2.outcome };
}
function OTHERS_FIRST() {
  return SLIPS.find((candidate) => !MX_CASH.includes(candidate));
}

// ---------------------------------------------------------------------------
// 3. El plan de la ficha.
// ---------------------------------------------------------------------------
for (const slip of Object.values(mx)) await createContact(slip.lead, slip.eventId);
await expectError('plan with the slip scope inactive', () => planSlip(mx.A.lead, mx.A),
  { code: '55000', message: 'pilot_scope_rejected' });
const armed = one((await db.query(`
  select * from public.set_lancemos_pilot_runtime_state($1,$2,0,'armed','operator-test','controlled-test')
`, [SLIP.scope, SLIP.scopeVersion])).rows, 'arm slip scope');
await db.query(`
  select * from public.set_lancemos_pilot_runtime_state($1,$2,0,'armed','operator-test','controlled-test')
`, [cartScope.scope_key, cartScope.version]);
// Contra el scope del carrito (que no lleva PURCHASE_BILLET_PRINTED) no se
// planifica.
const wrongScope = await expectError('plan against the cart scope',
  () => planSlip(mx.A.lead, mx.A, { scope: cartScope.scope_key, version: cartScope.version }),
  { code: '55000', message: 'pilot_scope_rejected' });
// Ni con la hora que no es la de la ficha.
await expectError('plan with another timestamp', () => db.query(`
  select * from public.plan_portable_payment_slip_recovery(
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
  )
`, [mx.A.eventId, mx.A.lead.contact, String(ATT1.productId), ATT1.productName,
  mx.A.lead.offer.offer_code, SLIP.policy, SLIP.policyVersion, at(-69).toISOString(),
  ATT1.accountId, ATT1.inboxId, mx.A.lead.phone, SLIP.scope, SLIP.scopeVersion]),
{ code: '55000', message: 'payment_slip_timestamp_mismatch' });
// Una ficha sin intencion no se planifica.
await expectError('plan of an unmatched slip', async () => {
  const slip = people.get(OTHERS_FIRST().data.buyer.email);
  const event = one((await db.query(`
    select id from public.webhook_events where external_event_id = $1
  `, [OTHERS_FIRST().id])).rows, 'unmatched event').id;
  await createContact(slip, event);
  return db.query(`
    select * from public.plan_portable_payment_slip_recovery(
      $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
    )
  `, [event, slip.contact, String(ATT1.productId), ATT1.productName,
    OTHERS_FIRST().data.purchase.offer.code, SLIP.policy, SLIP.policyVersion,
    at(-15).toISOString(), ATT1.accountId, ATT1.inboxId, slip.phone, SLIP.scope, SLIP.scopeVersion]);
}, { code: '55000', message: 'payment_slip_correlation_unresolved' });

const plans = {};
for (const [label, slip] of Object.entries(mx)) {
  plans[label] = one((await planSlip(slip.lead, slip)).rows, `${label} plan`);
}
{
  const plan = plans.A;
  const action = await actionOf(plan.scheduled_action_id);
  const state = one((await db.query(`
    select rc.status, rc.context ->> 'trigger_kind' as trigger_kind,
           rc.context ->> 'latest_payment_slip_event_id' as latest_event,
           rc.hotmart_purchase_intent_id, rc.selected_channel_identity_id,
           fs.reason, fs.max_attempts,
           (select string_agg(event_role, ',') from public.recovery_case_events
             where recovery_case_id = rc.id) as roles,
           binding.scope_key, binding.audience_mode, binding.audience_purchase_intent_id
    from public.recovery_cases rc
    join public.followup_sequences fs on fs.id = $2
    join public.pilot_recovery_case_bindings binding on binding.recovery_case_id = rc.id
    where rc.id = $1
  `, [plan.recovery_case_id, plan.followup_sequence_id])).rows, 'A case');
  const grants = (await db.query(`
    select authorization_status, evidence ->> 'reason' as reason,
           evidence ->> 'purchase_intent_id' as intent
    from public.contact_authorizations where contact_id = $1
  `, [mx.A.lead.contact])).rows;
  const expectedDue = mx.A.createdAt.getTime() + SLIP.graceMinutes * 60_000;
  if (!plan.created
      || action.step_key !== SLIP.step
      || action.anchor_type !== 'payment_slip'
      || action.status !== 'pending'
      || action.due_at.getTime() !== expectedDue
      || action.expires_at.getTime() !== mx.A.createdAt.getTime() + 24 * 3_600_000
      || action.anchor_subject_internal_id !== mx.A.eventId
      || state.status !== 'grace_period'
      || state.trigger_kind !== 'payment_slip'
      || state.latest_event !== mx.A.eventId
      || state.hotmart_purchase_intent_id !== mx.A.lead.intent
      || state.selected_channel_identity_id === null
      || state.reason !== 'payment_slip'
      || state.roles !== 'payment_slip'
      || state.scope_key !== SLIP.scope
      || state.audience_mode !== 'consented_intent'
      || state.audience_purchase_intent_id !== mx.A.lead.intent
      || grants.length !== 1 || grants[0].authorization_status !== 'allowed'
      || grants[0].reason !== 'precheckout_whatsapp_consent' || grants[0].intent !== mx.A.lead.intent) {
    throw new Error(`A plan: ${JSON.stringify({ plan, action, state, grants })}`);
  }
  for (const label of ['B', 'C', 'D', 'E']) {
    const other = await actionOf(plans[label].scheduled_action_id);
    if (!plans[label].created
        || other.due_at.getTime() !== mx[label].createdAt.getTime() + SLIP.graceMinutes * 60_000) {
      throw new Error(`${label} plan: ${JSON.stringify(other)}`);
    }
  }
  const replay = one((await planSlip(mx.A.lead, mx.A)).rows, 'A plan replay');
  if (replay.created || replay.scheduled_action_id !== plan.scheduled_action_id) {
    throw new Error(`A plan replay: ${JSON.stringify(replay)}`);
  }
  results.plan = {
    step_key: action.step_key, anchor_type: action.anchor_type,
    due_after_creation_minutes: (action.due_at.getTime() - mx.A.createdAt.getTime()) / 60_000,
    inactive_scope: 'pilot_scope_rejected', cart_scope: `pilot_scope_rejected/${wrongScope.detail}`,
    other_timestamp: 'payment_slip_timestamp_mismatch',
    unmatched: 'payment_slip_correlation_unresolved', replay_created: replay.created,
  };
}

// ---------------------------------------------------------------------------
// 4. La segunda ficha de B (otra ficha capturada de Mexico re-identificada a
//    B, cinco minutos despues): se agrega al caso, no crea otro mensaje ni
//    adelanta la hora.
// ---------------------------------------------------------------------------
{
  const before2 = await actionOf(plans.B.scheduled_action_id);
  const secondSource = MX_CASH.find((payload) => payload !== B_SLIP
    && payload.data.purchase.offer.code === B_SLIP.data.purchase.offer.code
    && payload.data.purchase.payment.billet_barcode !== B_SLIP.data.purchase.payment.billet_barcode);
  const second = shifted(reidentified(secondSource, mx.B.lead, 'b2'), at(-5));
  const admitted = await admitSlip(second);
  const detail = await detailOf(admitted.webhook_event_id);
  const plan = one((await planSlip(mx.B.lead, {
    eventId: admitted.webhook_event_id, createdAt: at(-5),
  })).rows, 'B second plan');
  const action = await actionOf(plans.B.scheduled_action_id);
  const counts = one((await db.query(`
    select
      (select count(*)::integer from public.scheduled_actions where recovery_case_id = $1) as actions,
      (select count(*)::integer from public.followup_sequences where recovery_case_id = $1) as sequences,
      (select count(*)::integer from public.recovery_case_events
        where recovery_case_id = $1 and event_role = 'payment_slip') as slips,
      (select count(*)::integer from public.conversation_events
        where recovery_case_id = $1 and event_type = 'payment_slip_aggregated') as aggregated,
      (select context ->> 'latest_payment_slip_event_id' from public.recovery_cases where id = $1) as latest
  `, [plans.B.recovery_case_id])).rows, 'B counts');
  if (admitted.outcome !== 'inserted' || detail.correlation_outcome !== 'resolved'
      || detail.purchase_intent_id !== mx.B.lead.intent
      || plan.created || plan.recovery_case_id !== plans.B.recovery_case_id
      || plan.scheduled_action_id !== plans.B.scheduled_action_id
      || action.due_at.getTime() !== before2.due_at.getTime()
      || action.next_attempt_at !== before2.next_attempt_at
      || action.due_at.getTime() <= (await dbNow()).getTime()
      || counts.actions !== 1 || counts.sequences !== 1 || counts.slips !== 2
      || counts.aggregated !== 1 || counts.latest !== admitted.webhook_event_id) {
    throw new Error(`B second slip: ${JSON.stringify({ admitted, plan, before2, action, counts })}`);
  }
  // El contexto de envio da la ficha mas reciente. Se reclama a la hora del
  // envio, dentro de una transaccion que se deshace.
  await db.exec('begin');
  try {
    const dueAt = new Date(action.due_at.getTime() + 60_000);
    const claimed = (await db.query(`
      select * from public.claim_due_followup_actions('slip-b',$1,interval '5 minutes',100)
    `, [dueAt])).rows.find((row) => row.id === plans.B.scheduled_action_id);
    if (!claimed) throw new Error('B action was not claimable at its due time');
    const context = one((await db.query(`
      select * from public.get_portable_payment_slip_send_context($1,'slip-b',$2,$3)
    `, [claimed.id, claimed.lease_generation, dueAt])).rows, 'B send context');
    if (context.webhook_event_id !== admitted.webhook_event_id
        || context.billet_barcode !== second.data.purchase.payment.billet_barcode
        || context.billet_url !== second.data.purchase.payment.billet_url
        || context.payment_type !== 'CASHPAYMENT' || context.checkout_country_iso !== 'MX'
        || context.generated_at.getTime() !== second.data.purchase.order_date) {
      throw new Error(`B send context is not the latest slip: ${JSON.stringify(context)}`);
    }
  } finally {
    await db.exec('rollback');
  }
  results.second_slip = {
    created: plan.created, actions: counts.actions, due_moved: false,
    send_context: 'latest slip',
  };
}

// ---------------------------------------------------------------------------
// 5. A de punta a punta, con la reevaluacion real.
// ---------------------------------------------------------------------------
{
  const worker = 'slip-worker-a';
  const now = await dbNow();
  const claimed = (await db.query(`
    select * from public.claim_due_followup_actions($1,$2,interval '5 minutes',100)
  `, [worker, now])).rows;
  if (claimed.length !== 1 || claimed[0].id !== plans.A.scheduled_action_id
      || claimed[0].anchor_type !== 'payment_slip') {
    throw new Error(`A claim: ${JSON.stringify(claimed.map((row) => [row.id, row.anchor_type]))}`);
  }
  const lease = claimed[0].lease_generation;
  const decision = one((await db.query(`
    select * from public.reevaluate_followup_action($1,$2,$3,$4)
  `, [plans.A.scheduled_action_id, worker, lease, now])).rows, 'A reevaluation');
  if (decision.decision !== 'execute' || decision.reason_code !== 'eligible_for_execution') {
    throw new Error(`A real reevaluation: ${JSON.stringify(decision)}`);
  }
  const context = one((await db.query(`
    select * from public.get_followup_execution_context($1,$2,$3,$4)
  `, [plans.A.scheduled_action_id, worker, lease, now])).rows, 'A execution context');
  const slipContext = one((await db.query(`
    select * from public.get_portable_payment_slip_send_context($1,$2,$3,$4)
  `, [plans.A.scheduled_action_id, worker, lease, now])).rows, 'A slip context');
  if (context.step_key !== SLIP.step || context.offer_code !== mx.A.lead.offer.offer_code
      || context.buyer_phone !== mx.A.lead.phone
      || slipContext.webhook_event_id !== mx.A.eventId
      || slipContext.billet_url !== mx.A.body.data.purchase.payment.billet_url
      || slipContext.billet_barcode !== mx.A.body.data.purchase.payment.billet_barcode) {
    throw new Error(`A contexts: ${JSON.stringify({ context, slipContext })}`);
  }
  await expectError('slip context with another lease', () => db.query(`
    select * from public.get_portable_payment_slip_send_context($1,$2,$3,$4)
  `, [plans.A.scheduled_action_id, worker, lease + 1, now]),
  { code: '55000', message: 'current_action_lease_not_found' });
  const attempt = one((await db.query(`
    select * from public.reserve_followup_delivery_attempt($1,$2,$3,$4,$5,'whatsapp','approved_template',$6)
  `, [plans.A.scheduled_action_id, worker, lease, decision.case_version,
    decision.sequence_revision, now])).rows, 'A reservation');
  const startedAt = await dbNow();
  // El arranque del pago fallido no arranca una ficha.
  await expectError('payment failure start on a slip action', () => db.query(`
    select * from public.mark_portable_payment_failure_request_started($1,$2,$3,$4,$5)
  `, [plans.A.scheduled_action_id, attempt.id, worker, lease, startedAt]),
  { code: '55000', message: 'pilot_request_start_rejected', detail: 'payment_failure_action_required' });
  const started = one((await db.query(`
    select * from public.mark_portable_payment_slip_request_started($1,$2,$3,$4,$5)
  `, [plans.A.scheduled_action_id, attempt.id, worker, lease, startedAt])).rows, 'A start');
  const authorization = one((await db.query(`
    select scope_key, scope_version, contact_id, action_id
    from public.pilot_outbound_request_authorizations where id = $1
  `, [started.pilot_authorization_id])).rows, 'A authorization');
  if (started.phase !== 'request_started' || started.mode !== 'approved_template'
      || started.pilot_authorization_replayed !== false
      // El scope de la ficha solo acepta PURCHASE_BILLET_PRINTED (sin tipos
      // adicionales): la puerta autorizo con ese tipo o no habria autorizado.
      || authorization.scope_key !== SLIP.scope
      || authorization.scope_version !== SLIP.scopeVersion
      || authorization.action_id !== plans.A.scheduled_action_id
      || authorization.contact_id !== mx.A.lead.contact) {
    throw new Error(`A start: ${JSON.stringify({ started, authorization })}`);
  }
  const accepted = one((await db.query(`
    select * from public.record_and_finalize_followup_acceptance($1,$2,$3,$4,'910001','slip-wamid-1',$5,$6)
  `, [plans.A.scheduled_action_id, attempt.id, worker, lease,
    `Plantilla aprobada de la ficha para ${mx.A.lead.name}`, await dbNow()])).rows, 'A acceptance');
  const sequence = one((await db.query(`
    select status, completion_reason from public.followup_sequences where id = $1
  `, [plans.A.followup_sequence_id])).rows, 'A sequence');
  if (accepted.status !== 'accepted_by_chatwoot' || sequence.status !== 'completed'
      || sequence.completion_reason !== 'policy_exhausted'
      || (await caseActions(plans.A.recovery_case_id)).length !== 1) {
    throw new Error(`A acceptance: ${JSON.stringify({ accepted: accepted.status, sequence })}`);
  }
  results.dispatch = {
    reevaluation: decision.reason_code, start_scope: authorization.scope_key,
    start_event_type: 'PURCHASE_BILLET_PRINTED', failure_start: 'payment_failure_action_required',
    acceptance: accepted.status, sequence: sequence.completion_reason,
  };
}

// ---------------------------------------------------------------------------
// 6. C se da de baja en Chatwoot antes del envio.
// ---------------------------------------------------------------------------
{
  const startsBefore = await starts(SLIP.scope);
  const optOut = one((await db.query(`
    select * from public.apply_chatwoot_inbound_opt_out($1,$2,$3,$4,$5,$6,'unsubscribe')
  `, [ATT1.accountId, ATT1.inboxId, 8901, 98901, mx.C.lead.phone, await dbNow()])).rows, 'C opt-out');
  const action = await actionOf(plans.C.scheduled_action_id);
  const active = one((await db.query(`
    select count(*)::integer as count from public.contact_authorizations
    where contact_id = $1 and authorization_status = 'allowed'
      and valid_from <= clock_timestamp()
      and (valid_until is null or valid_until > clock_timestamp())
  `, [mx.C.lead.contact])).rows, 'C active permissions').count;
  const later = (await db.query(`
    select id from public.claim_due_followup_actions('slip-c',$1,interval '5 minutes',100)
  `, [new Date(action.due_at.getTime() + 60_000)])).rows
    .filter((row) => row.id === plans.C.scheduled_action_id);
  if (optOut.outcome !== 'applied' || optOut.matched_contact_id !== mx.C.lead.contact
      || action.status !== 'cancelled' || active !== 0 || later.length !== 0
      || (await starts(SLIP.scope)) !== startsBefore) {
    throw new Error(`C opt-out: ${JSON.stringify({ optOut, action, active, later })}`);
  }
  results.opt_out = { outcome: optOut.outcome, action: action.status, active_permissions: active };
}

// ---------------------------------------------------------------------------
// 7. D compra (la compra real pagada en OXXO, re-identificada a D) antes del
//    envio: el caso de la ficha se cierra.
// ---------------------------------------------------------------------------
{
  const purchase = structuredClone(CASH_APPROVED);
  const approvedAt = at(-1).getTime();
  purchase.id = `${CASH_APPROVED.id}-slip-d`;
  purchase.creation_date = approvedAt;
  purchase.data.purchase.approved_date = approvedAt;
  purchase.data.purchase.order_date = mx.D.body.data.purchase.order_date;
  purchase.data.purchase.transaction = mx.D.body.data.purchase.transaction;
  purchase.data.purchase.offer = { ...purchase.data.purchase.offer, code: mx.D.lead.offer.offer_code };
  purchase.data.buyer = { ...purchase.data.buyer, email: mx.D.lead.email, checkout_phone: mx.D.lead.phone };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_purchase_approved($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, purchase.id, JSON.stringify(purchase),
    mx.D.lead.email, mx.D.lead.phone])).rows, 'D purchase');
  const applied = one((await db.query(`
    select * from public.apply_hotmart_purchase_approved($1,$2,$3,$4,$5,$6,$7)
  `, [admitted.webhook_event_id, mx.D.lead.email, mx.D.lead.phone, String(ATT1.productId),
    mx.D.lead.offer.offer_code, purchase.data.purchase.transaction,
    new Date(approvedAt).toISOString()])).rows, 'D apply');
  const state = one((await db.query(`
    select rc.status, rc.purchase_event_id,
           (select status from public.scheduled_actions where id = $2) as action_status,
           (select lifecycle_state from public.purchase_intents where id = $3) as intent
    from public.recovery_cases rc where rc.id = $1
  `, [plans.D.recovery_case_id, plans.D.scheduled_action_id, mx.D.lead.intent])).rows, 'D state');
  if (admitted.outcome !== 'inserted' || applied.outcome !== 'applied'
      || applied.recovery_case_id !== plans.D.recovery_case_id
      || state.status !== 'won' || state.purchase_event_id !== admitted.webhook_event_id
      || state.action_status !== 'cancelled' || state.intent !== 'purchased') {
    throw new Error(`D purchase: ${JSON.stringify({ admitted, applied, state })}`);
  }
  results.purchase = { applied: applied.outcome, case: state.status, action: state.action_status };
}

// ---------------------------------------------------------------------------
// 8. El estado del runtime del scope de la ficha.
// ---------------------------------------------------------------------------
{
  const status = (fn, scope, version) => db.query(`
    select * from public.${fn}($1,$2,$3,$4,$5)
  `, [scope, version, ATT1.tenant, CHANNEL_PROVIDER, CHANNEL_REF]);
  const slip = one((await status('get_portable_payment_slip_pilot_runtime_status',
    SLIP.scope, SLIP.scopeVersion)).rows, 'slip status');
  const cartOnSlip = one((await status('get_portable_payment_slip_pilot_runtime_status',
    cartScope.scope_key, cartScope.version)).rows, 'cart scope on slip status');
  const slipOnCart = one((await status('get_lancemos_pilot_runtime_status',
    SLIP.scope, SLIP.scopeVersion)).rows, 'slip scope on cart status');
  const invalid = one((await db.query(`
    select * from public.get_portable_payment_slip_pilot_runtime_status($1,$2,$3,$4,$5)
  `, [SLIP.scope, 0, ATT1.tenant, CHANNEL_PROVIDER, CHANNEL_REF])).rows, 'invalid status');
  if (armed.runtime_state !== 'armed'
      || slip.configured !== true || slip.runtime_state !== 'armed'
      || slip.reason_code !== 'pilot_runtime_armed'
      || cartOnSlip.configured !== false || cartOnSlip.reason_code !== 'pilot_scope_config_mismatch'
      || slipOnCart.configured !== false || slipOnCart.reason_code !== 'pilot_scope_config_mismatch'
      || invalid.reason_code !== 'pilot_runtime_config_invalid') {
    throw new Error(`runtime status: ${JSON.stringify({ slip, cartOnSlip, slipOnCart, invalid })}`);
  }
  results.runtime_status = {
    slip_scope: slip.reason_code, cart_scope: cartOnSlip.reason_code,
    shared_status_on_slip_scope: slipOnCart.reason_code,
  };
}

// ---------------------------------------------------------------------------
// 9. H3, documentado y NO arreglado: E tiene el caso de la ficha abierto
//    (pendiente, en la gracia) y deja un carrito de la misma oferta.
//    plan_cart_recovery toma cualquier caso Hotmart abierto del contacto, el
//    producto y la oferta sin mirar el disparador. Se fija lo que pasa hoy;
//    si alguien lo arregla, este caso tiene que cambiar a proposito.
// ---------------------------------------------------------------------------
{
  const cart = structuredClone(CAPTURED_CART);
  cart.id = `slip-h3-cart-${mx.E.lead.email}`;
  cart.creation_date = at(-2).getTime();
  cart.data.product = { id: ATT1.productId, name: ATT1.productName };
  cart.data.offer = { code: mx.E.lead.offer.offer_code };
  cart.data.buyer = { name: mx.E.lead.name, email: mx.E.lead.email, phone: mx.E.lead.phone };
  const admitted = one((await db.query(`
    select * from public.admit_portable_hotmart_cart_abandonment($1,$2,$3,$4,$5::jsonb,$6,$7)
  `, [ATT1.tenant, ATT1.funnel, ATT1.bindingVersion, cart.id, JSON.stringify(cart),
    mx.E.lead.email, mx.E.lead.phone])).rows, 'E cart');
  const slipBefore = await actionOf(plans.E.scheduled_action_id);
  let outcome;
  let after3;
  await db.exec('begin');
  try {
    try {
      const plan = one((await db.query(`
        select * from public.plan_lancemos_pilot_cart_recovery(
          $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
        )
      `, [admitted.webhook_event_id, mx.E.lead.contact, String(ATT1.productId), ATT1.productName,
        mx.E.lead.offer.offer_code, cartPolicy.policy_key, cartPolicy.version,
        new Date(cart.creation_date).toISOString(), ATT1.accountId, ATT1.inboxId,
        mx.E.lead.phone, cartScope.scope_key, cartScope.version])).rows, 'E cart plan');
      outcome = { planned: true, same_case: plan.recovery_case_id === plans.E.recovery_case_id };
    } catch (error) {
      outcome = { planned: false, code: error.code, message: error.message, detail: error.detail ?? null };
    }
  } finally {
    await db.exec('rollback');
  }
  after3 = await actionOf(plans.E.scheduled_action_id);
  // Lo que pasa hoy (medido el 2026-10-18): plan_cart_recovery agrega el
  // carrito al caso de la ficha y le adelanta la hora, y despues el
  // planificador del piloto del carrito encuentra el binding del caso en el
  // scope de la ficha y aborta con pilot_case_binding_conflict. La
  // transaccion se deshace entera: ni carrito ni ficha cambian. Con el flag de
  // la ficha prendido, ese carrito no recibe su mensaje mientras el caso de la
  // ficha siga abierto.
  if (admitted.outcome !== 'inserted'
      || outcome.planned !== false || outcome.code !== '55000'
      || outcome.message !== 'pilot_case_binding_conflict'
      || after3.status !== 'pending'
      || after3.due_at.getTime() !== slipBefore.due_at.getTime()
      || (await caseActions(plans.E.recovery_case_id)).length !== 1) {
    throw new Error(`H3 changed: ${JSON.stringify({ admitted: admitted.outcome, outcome, slipBefore, after3 })}`);
  }
  results.h3_cart_over_open_slip_case = outcome;
}

if ((await openActions()).some((row) => ![plans.B, plans.E]
  .some((plan) => plan.scheduled_action_id === row.id))) {
  throw new Error(`unexpected open actions: ${JSON.stringify(await openActions())}`);
}
console.log(`portable_payment_slip_recovery=OK ${JSON.stringify(results)}`);
