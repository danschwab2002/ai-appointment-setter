// Las ofertas huerfanas de Hotmart (migracion 20261114000300, E-565, MP-02 del
// plan de multiproducto), contra la cadena completa de migraciones. Contrato:
// docs/contracts/hotmart-unknown-offers-v1.md §4.
//
// Prueba:
//   0. que la migracion agrega exactamente dos funciones, las dos solo de
//      service_role y security definer, no cambia ninguna otra definicion ni
//      ACL, deja las dos tablas con RLS y sin privilegios para los roles de la
//      API, y que correrla dos veces aborta con
//      hotmart_unknown_offers_already_applied;
//   1. el upsert idempotente: el primer evento inserta (conteo 1), el mismo
//      evento repetido (el reintento de Hotmart) da duplicate y no suma, otro
//      evento suma; un evento de otra entrada suma la entrada una sola vez;
//   2. la clave: otra oferta, otro tipo de evento y otro tenant son filas
//      distintas; la oferta null es una sola fila;
//   3. el despacho sin tipo de evento: la base lo lee de webhook_events por el
//      id del evento que abrio el caso; un id que no existe da 22023;
//   4. resuelta: la lista de abiertas no la trae, la lista entera si; un evento
//      nuevo la vuelve a abrir;
//   5. una entrada invalida (tenant, oferta con un mail, motivo, entrada, hash,
//      producto) da 22023 y no deja nada, y las columnas no tienen nada personal.
//
// Los datos son los del catalogo de los fixtures capturados: el producto 8443991
// y la oferta u8ikyx78 (tests/fixtures/hotmart_purchase_approved_maribel_20261029.json),
// el 8104005 y la mgbqpp19 (tests/fixtures/hotmart_cart_abandonment_rejected_v1.json)
// y el 5071808 con la 83utgyow (tests/fixtures/hotmart_routing_att1_20261016.json).
// La base no ve el payload: solo el producto, la oferta, el tipo y un hash.
import { PGlite } from '@electric-sql/pglite';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..');
const MIGRATION = '20261114000300_hotmart_unknown_offers_v1.sql';
const RECORD = 'record_hotmart_unknown_offer_v1(text,bigint,text,text,text,text,text,uuid)';
const LIST = 'list_hotmart_unknown_offers_v1(text,boolean)';
const db = new PGlite();
await db.waitReady;
await db.exec(`
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  -- Los privilegios por defecto de Supabase: toda funcion y tabla nueva nace con
  -- privilegios para los roles de la API, y la migracion tiene que revocarlos.
  alter default privileges in schema public grant execute on functions to anon, authenticated;
  alter default privileges in schema public grant all on functions to service_role;
  alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
`);

const fail = (message, detail) => {
  throw new Error(detail === undefined ? message : `${message}: ${JSON.stringify(detail)}`);
};
const results = {};

const names = readdirSync(join(root, 'supabase/migrations')).filter((n) => n.endsWith('.sql')).sort();
const position = names.indexOf(MIGRATION);
if (position < 0) fail(`${MIGRATION} no esta en la cadena`);
// Despues van solo las del producto que suma el AE (20261115000100, E-569, y su publicacion 20261115000200, E-575),
// que no tocan este registro, y la audiencia de hoy de un flujo de audiencia (20261116000100, E-622), que tampoco.
if (JSON.stringify(names.slice(position + 1)) !== JSON.stringify(['20261115000100_agent_products_v1.sql',
  '20261115000200_agent_products_publish_v1.sql', '20261116000100_flow_audience_reach_v1.sql',
    // El registro de permiso de WhatsApp (20261116000200, E-627).
    '20261116000200_permission_registry_v1.sql',
    // El destinatario de un flujo nativo de audiencia (20261117000500, E-661).
    '20261117000500_flow_engine_native_audience_recipient_v1.sql',
    // La repeticion de un flujo de audiencia cuenta solo a quien arranco (20261117000600, E-665).
    '20261117000600_flow_audience_repeat_counts_started_v1.sql',
    // Los motivos de quien no arranco en un envio a una audiencia (20261117000700, E-666).
    '20261117000700_flow_audience_start_reasons_v1.sql',
    // El numero de conversacion de los avisos del correlador (20261118000300, E-656).
    '20261118000300_identity_link_conversation_number_account_v1.sql',
    // El choque de identidad en la admision es un rechazo con nombre (20261125000100).
    '20261125000100_flow_engine_admission_identity_mismatch_v1.sql',
    // El almacen del masivo (20261125000200).
    '20261125000200_bulk_send_store_v1.sql',
    // Preparar y los vetos del masivo (20261125000300).
    '20261125000300_bulk_send_prepared_and_base_vetoes_v1.sql',
    // El registro por persona del masivo (20261125000400).
    '20261125000400_bulk_send_recipients_read_v1.sql',
    // La pausa del agente (20261126100000, 5.4).
    '20261126100000_agent_global_pause_v1.sql',
    // La instruccion temporal (20261126200000, 5.3 de Z1): tablas y RPC propias, no toca lo que se prueba aca.
    '20261126200000_agent_temporary_instructions_v1.sql',
    // Exportar contactos (20261126300000).
    '20261126300000_contact_export_v1.sql',
    // Las reglas de palabra clave (20261126400000, Z1 3.9).
    '20261126400000_keyword_rules_v1.sql',
    // Las corridas del juez (20261126500000, 4.9).
    '20261126500000_agent_campaign_judge_runs_v1.sql',
    // El conocimiento vivo del AE (20261126600000, pieza 5.5).
    '20261126600000_agent_live_knowledge_v1.sql',
    // El pais y la hora del evento de la audiencia (20261126700000, piezas 4.4 y 4.5).
    '20261126700000_flow_audience_country_and_event_hour_v1.sql',
    // Las fases del lanzamiento (20261126800000).
    '20261126800000_agent_campaign_phases_v1.sql',
    // Los frenos ante Meta (20261127100000, F1).
    '20261127100000_meta_enforcement_brakes_v1.sql',
    // El texto fijo en las variables de un paso nativo (20261127300000, F4).
    '20261127300000_flow_engine_literal_variables_v1.sql',
    // El flujo de la palabra clave (20261127400000, Z2 3.9).
    '20261127400000_flow_engine_keyword_trigger_v1.sql',
    // La confirmacion de registro (20261127500000, 4.3).
    '20261127500000_own_forms_registration_confirmation_v1.sql',
    // El texto fijo tercero del seguimiento por silencio (20261128100000, U4).
    '20261128100000_flow_engine_native_silence_literal_v1.sql'])) fail(`despues de ${MIGRATION} van solo la de E-569, la de E-575 y la de E-622`, names.slice(position + 1));
const apply = async (file) => db.exec(readFileSync(file, 'utf8').replace(
  /create extension if not exists pgcrypto;/gi,
  '-- pgcrypto is built into PGlite',
));
const snapshot = async () => new Map((await db.query(`
  select p.oid::regprocedure::text as signature,
         pg_get_functiondef(p.oid) as definition,
         p.prosecdef as secdef,
         has_function_privilege('service_role', p.oid, 'execute') as service_x,
         has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute') as api_x
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and p.prokind = 'f'
`)).rows.map((row) => [row.signature, row]));

await apply(join(root, 'supabase/baseline/20260803_public_schema.sql'));
for (const name of names.slice(0, position)) {
  await apply(join(root, 'supabase/migrations', name));
}
const before = await snapshot();
await apply(join(root, 'supabase/migrations', MIGRATION));
const after = await snapshot();

// ---------------------------------------------------------------------------
// 0. Lo que agrega la migracion, y nada mas.
// ---------------------------------------------------------------------------
{
  const added = [...after.keys()].filter((signature) => !before.has(signature)).sort();
  const removed = [...before.keys()].filter((signature) => !after.has(signature));
  const changed = [...before.keys()].filter((signature) => after.has(signature)
    && (after.get(signature).definition !== before.get(signature).definition
        || after.get(signature).service_x !== before.get(signature).service_x
        || after.get(signature).api_x !== before.get(signature).api_x));
  if (JSON.stringify(added) !== JSON.stringify([LIST, RECORD].sort()) || removed.length || changed.length) {
    fail('the migration changed something else', { added, removed, changed });
  }
  for (const signature of added) {
    const row = after.get(signature);
    if (row.service_x !== true || row.api_x !== false || row.secdef !== true) {
      fail(`${signature} is not a service_role-only security definer`, row);
    }
  }
  const tables = (await db.query(`
    select c.relname, c.relrowsecurity,
           has_table_privilege('anon', c.oid, 'select') or has_table_privilege('anon', c.oid, 'insert')
             or has_table_privilege('authenticated', c.oid, 'select')
             or has_table_privilege('authenticated', c.oid, 'insert')
             or has_table_privilege('service_role', c.oid, 'select')
             or has_table_privilege('service_role', c.oid, 'insert')
             or has_table_privilege('service_role', c.oid, 'update')
             or has_table_privilege('service_role', c.oid, 'delete') as api_dml
      from pg_class c
     where c.relnamespace = 'public'::regnamespace
       and c.relname in ('hotmart_unknown_offers', 'hotmart_unknown_offer_refs')
     order by c.relname
  `)).rows;
  if (tables.length !== 2 || tables.some((t) => t.relrowsecurity !== true || t.api_dml !== false)) {
    fail('the tables are not RLS without API privileges', tables);
  }
  try {
    await apply(join(root, 'supabase/migrations', MIGRATION));
    fail('running the migration twice passed');
  } catch (error) {
    if (!String(error.message).includes('hotmart_unknown_offers_already_applied')) throw error;
    await db.exec('rollback;');
  }
  if ((await snapshot()).size !== after.size) fail('the second run left something');
  results.migration = { added: 2, changed: 0, tables_rls: 2, twice: 'hotmart_unknown_offers_already_applied' };
}

// ---------------------------------------------------------------------------
// Ayudas.
// ---------------------------------------------------------------------------
const ref = (value) => createHash('sha256').update(`hotmart_event:${value}`).digest('hex');
const record = async ({
  tenant = 'lancemos', product = 8443991, offer = 'u8ikyx78', type = 'PURCHASE_APPROVED',
  motivo = 'product_not_declared', entrada = 'parser_descarta', event = 'e1', source = null,
} = {}) => (await db.query(
  'select * from public.record_hotmart_unknown_offer_v1($1, $2, $3, $4, $5, $6, $7, $8)',
  [tenant, product, offer, type, motivo, entrada, typeof event === 'string' && event.length === 64 ? event : ref(event), source],
)).rows;
const list = async (tenant = 'lancemos', onlyOpen = true) => (await db.query(
  'select * from public.list_hotmart_unknown_offers_v1($1, $2)', [tenant, onlyOpen],
)).rows;
const rejects = async (label, args, message) => {
  try {
    await record(args);
  } catch (error) {
    if (error.code !== '22023' || !String(error.message).includes(message)) {
      fail(`${label}: expected 22023 ${message}`, { code: error.code, message: error.message });
    }
    return;
  }
  fail(`${label}: passed`);
};

// ---------------------------------------------------------------------------
// 1. El upsert idempotente.
// ---------------------------------------------------------------------------
{
  const [first] = await record({ event: 'maribel-compra-1' });
  if (first.outcome !== 'inserted' || Number(first.conteo) !== 1) fail('first event', first);
  const [replay] = await record({ event: 'maribel-compra-1' });
  if (replay.outcome !== 'duplicate' || Number(replay.conteo) !== 1 || replay.offer_id !== first.offer_id) {
    fail('the replay of Hotmart counted', replay);
  }
  const [second] = await record({ event: 'maribel-compra-2' });
  if (second.outcome !== 'counted' || Number(second.conteo) !== 2 || second.offer_id !== first.offer_id) {
    fail('another event did not count', second);
  }
  await record({ event: 'maribel-compra-3', entrada: 'sin_ruta' });
  await record({ event: 'maribel-compra-4', entrada: 'sin_ruta' });
  const [row] = await list();
  if (JSON.stringify(row.entradas) !== JSON.stringify(['parser_descarta', 'sin_ruta']) || Number(row.conteo) !== 4) {
    fail('the entries were not accumulated once each', row);
  }
  if (!(new Date(row.ultima_vez) >= new Date(row.primera_vez)) || row.resuelta_en !== null) fail('the times', row);
  results.idempotent = { inserted: 1, duplicate: 1, counted: 3, entradas: row.entradas };
}

// ---------------------------------------------------------------------------
// 2. La clave.
// ---------------------------------------------------------------------------
{
  await record({ product: 8104005, offer: 'mgbqpp19', type: 'PURCHASE_OUT_OF_SHOPPING_CART', motivo: 'offer_not_declared', event: 'carrito-1' });
  await record({ product: 8104005, offer: 'mgbqpp19', type: 'PURCHASE_APPROVED', motivo: 'offer_not_declared', event: 'carrito-1' });
  await record({ product: 8104005, offer: null, type: 'PURCHASE_APPROVED', event: 'sin-oferta-1' });
  const [nullAgain] = await record({ product: 8104005, offer: null, type: 'PURCHASE_APPROVED', event: 'sin-oferta-2' });
  if (nullAgain.outcome !== 'counted' || Number(nullAgain.conteo) !== 2) fail('the null offer is two rows', nullAgain);
  await record({ tenant: 'otra', event: 'maribel-compra-1' });
  const mine = await list();
  if (mine.length !== 4) fail('the key of the rows', mine.map((r) => [r.hotmart_product_id, r.offer_code, r.event_type]));
  if ((await list('otra')).length !== 1) fail('another tenant sees rows that are not its own');
  results.key = { rows: mine.length, other_tenant: 1 };
}

// ---------------------------------------------------------------------------
// 3. El despacho sin tipo de evento: la base lo lee del evento que abrio el caso.
// ---------------------------------------------------------------------------
{
  const [{ id: sourceId }] = (await db.query(`
    insert into public.webhook_events (source, external_event_id, event_type, payload)
    values ('hotmart', 'fixture-compra_83utgyow', 'PURCHASE_APPROVED', '{}'::jsonb)
    returning id
  `)).rows;
  const [dispatch] = await record({
    product: 5071808, offer: '83utgyow', type: null, motivo: 'offer_not_declared',
    entrada: 'despacho', event: `source-${sourceId}`, source: sourceId,
  });
  if (dispatch.outcome !== 'inserted') fail('the dispatch row', dispatch);
  const row = (await list()).find((r) => r.offer_code === '83utgyow');
  if (!row || row.event_type !== 'PURCHASE_APPROVED' || JSON.stringify(row.entradas) !== '["despacho"]') {
    fail('the dispatch did not read the event type', row);
  }
  await rejects('a source event that does not exist', {
    product: 5071808, offer: '83utgyow', type: null, motivo: 'offer_not_declared', entrada: 'despacho',
    event: 'otro', source: '00000000-0000-4000-8000-000000000000',
  }, 'hotmart_unknown_offer_invalid_event_type');
  // Un caso que abrio el formulario (no Hotmart) no deja fila.
  const [{ id: formId }] = (await db.query(`
    insert into public.webhook_events (source, external_event_id, event_type, payload)
    values ('system', 'fixture-formulario', 'PRECHECKOUT_FORM_SUBMITTED', '{}'::jsonb)
    returning id
  `)).rows;
  const before = (await list('lancemos', false)).length;
  const [form] = await record({
    product: 5071808, offer: '83utgyow', type: null, motivo: 'offer_not_declared',
    entrada: 'despacho', event: `source-${formId}`, source: formId,
  });
  if (form.outcome !== 'not_hotmart' || form.offer_id !== null || (await list('lancemos', false)).length !== before) {
    fail('a case opened by a form left a row', form);
  }
  await rejects('no type and no source', { type: null, event: 'x' }, 'hotmart_unknown_offer_invalid_event_type');
  results.dispatch = { event_type_from_webhook_events: row.event_type, form_case: 'not_hotmart' };
}

// ---------------------------------------------------------------------------
// 4. Resuelta y reabierta.
// ---------------------------------------------------------------------------
{
  await db.exec(`update public.hotmart_unknown_offers set resuelta_en = clock_timestamp()
                  where tenant_ref = 'lancemos' and offer_code = 'u8ikyx78'`);
  const open = await list();
  const all = await list('lancemos', false);
  if (open.some((r) => r.offer_code === 'u8ikyx78') || !all.some((r) => r.offer_code === 'u8ikyx78')) {
    fail('the resolved row in the lists', { open: open.length, all: all.length });
  }
  const [again] = await record({ event: 'maribel-compra-5' });
  if (again.outcome !== 'counted') fail('the new event of a resolved row', again);
  const reopened = (await list()).find((r) => r.offer_code === 'u8ikyx78');
  if (!reopened || reopened.resuelta_en !== null || Number(reopened.conteo) !== 5) fail('the row was not reopened', reopened);
  // Un duplicado de un evento ya contado no la reabre.
  await db.exec(`update public.hotmart_unknown_offers set resuelta_en = clock_timestamp()
                  where tenant_ref = 'lancemos' and offer_code = 'u8ikyx78'`);
  const [dup] = await record({ event: 'maribel-compra-5' });
  if (dup.outcome !== 'duplicate' || (await list()).some((r) => r.offer_code === 'u8ikyx78')) {
    fail('a replay reopened the row', dup);
  }
  results.resolved = { hidden_while_resolved: true, reopened_by_new_event: true, replay_keeps_it_closed: true };
}

// ---------------------------------------------------------------------------
// 5. Entradas invalidas y nada personal.
// ---------------------------------------------------------------------------
{
  const count = async () => Number((await db.query('select count(*) as n from public.hotmart_unknown_offers')).rows[0].n);
  const rows = await count();
  await rejects('a bad tenant', { tenant: 'Lancemos SA', event: 'y' }, 'hotmart_unknown_offer_invalid_arguments');
  await rejects('an offer with an email', { offer: 'persona@example.test', event: 'y' }, 'hotmart_unknown_offer_invalid_arguments');
  await rejects('a bad reason', { motivo: 'otro', event: 'y' }, 'hotmart_unknown_offer_invalid_arguments');
  await rejects('a bad entry', { entrada: 'webhook', event: 'y' }, 'hotmart_unknown_offer_invalid_arguments');
  await rejects('a bad hash', { event: 'z'.repeat(64) }, 'hotmart_unknown_offer_invalid_arguments');
  await rejects('a bad product', { product: 0, event: 'y' }, 'hotmart_unknown_offer_invalid_arguments');
  await rejects('a bad event type', { type: 'purchase approved', event: 'y' }, 'hotmart_unknown_offer_invalid_event_type');
  if ((await count()) !== rows) fail('a rejected call left a row');
  try {
    await db.query('select * from public.list_hotmart_unknown_offers_v1($1, true)', ['']);
    fail('a bad tenant listed');
  } catch (error) {
    if (error.code !== '22023') throw error;
  }
  const columns = (await db.query(`
    select table_name, column_name from information_schema.columns
     where table_schema = 'public' and table_name in ('hotmart_unknown_offers', 'hotmart_unknown_offer_refs')
     order by table_name, ordinal_position
  `)).rows.map((r) => `${r.table_name}.${r.column_name}`);
  const expected = [
    'hotmart_unknown_offer_refs.offer_id', 'hotmart_unknown_offer_refs.event_ref_sha256',
    'hotmart_unknown_offer_refs.entrada', 'hotmart_unknown_offer_refs.created_at',
    'hotmart_unknown_offers.id', 'hotmart_unknown_offers.tenant_ref', 'hotmart_unknown_offers.hotmart_product_id',
    'hotmart_unknown_offers.offer_code', 'hotmart_unknown_offers.event_type', 'hotmart_unknown_offers.motivo',
    'hotmart_unknown_offers.entradas', 'hotmart_unknown_offers.conteo', 'hotmart_unknown_offers.primera_vez',
    'hotmart_unknown_offers.ultima_vez', 'hotmart_unknown_offers.resuelta_en',
  ];
  if (JSON.stringify(columns) !== JSON.stringify(expected)) fail('the columns (nothing personal, no payload)', columns);
  results.invalid = { rejected: 8, columns: columns.length };
}

console.log(JSON.stringify({ ok: true, results }, null, 2));
