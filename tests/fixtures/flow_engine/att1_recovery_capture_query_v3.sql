-- captura-paridad-recuperacion-v3: el dato real para la T2.2 del motor de flujos,
-- el carrito de tres toques de ATT1 (A1, A2 y Z).
--
-- Especificacion: «T2.2: la captura v3 y la paridad del carrito de tres
-- toques» (2026-10-21, en el vault). Parte de captura-paridad-recuperacion-v2
-- (att1_recovery_capture_query_v2.sql), que no cambia, con las mismas reglas.
--
-- Solo lectura. Devuelve una sola fila con un solo JSON. Se corre con
-- default_transaction_read_only y el SQL por la entrada estandar.
--
-- Que NO sale nunca: payloads, telefonos, mails, nombres, textos libres, notas,
-- refusal_reason, external_user_id, transacciones de Hotmart, nombres de
-- operador (approved_by, changed_by, change_reason, actor, requested_by), el
-- codigo de un cupon, el id del cupon en Hotmart, la URL de un link, su sck,
-- su ulid, los ids de Chatwoot ni el texto de una respuesta.
--   * Las personas salen como sha256 truncado a 16 hex, con una sal aleatoria
--     por corrida que no sale de la consulta, y un prefijo por tipo de clave
--     (p: telefono canonico, d: digitos tal cual, e: mail).
--   * config sale con una lista cerrada de claves, armada campo por campo.
--   * Todo motivo sale solo si cumple ^[a-z0-9_:]{1,80}$; si no, sale 'otro'.
--
-- La ventana: arranca con la activacion de la v7 del scope del carrito
-- (att1-recuperacion, pilot_scope_version_activated con scope_version >= 7),
-- la de A1, A2 y Z. La siembra son los 7 dias previos. Si esa activacion no
-- existe, la consulta falla entera y no devuelve nada a medias.
--
-- Lo que cambia contra la v2:
--   * solo el carrito: las secuencias, las acciones, sus intentos, las
--     reevaluaciones, los cupones, los links y las renovaciones son de los
--     casos atados a att1-recuperacion. El pago rechazado (att1-pago-rechazado)
--     no se migra en la Etapa 1: sus eventos y sus casos entran solo como
--     hechos de la persona (un pago fallido abierto frena al carrito);
--   * los pasos de la politica traen delay, approved_template y
--     coupon_ordinal (20261019000600);
--   * las secuencias, con current_step y el cierre (completion_reason);
--   * los cupones por caso (recovery_coupons), sin el codigo;
--   * el link de cada intento (checkout_link_issuances con
--     source_kind = 'recovery_first_touch'), sin URL ni sck;
--   * las renovaciones (recovery_renewal_requests), con su efecto;
--   * por cada accion con envio, la primera respuesta despues del envio, solo
--     como hora y de tres fuentes de la base: la adopcion de la respuesta
--     (inbound_adopted_template_conversation), una reevaluacion que vio un
--     entrante despues del ancla, y el pedido de renovacion.
with p as (
    select clock_timestamp() as captured_at,
           gen_random_uuid()::text as sal,
           'att1-recuperacion'::text as scope_key,
           7 as desde_version
),
w as (
    select p.captured_at, p.sal, p.scope_key, p.desde_version,
           coalesce(
               a.desde,
               -- Falla en voz alta: un texto que no es una fecha.
               ('captura_sin_activacion:' || p.scope_key || ':v' || p.desde_version::text
                || ':' || clock_timestamp()::text)::timestamptz
           ) as desde
      from p
      cross join lateral (
          select min(e.created_at) as desde
            from public.pilot_control_events e
           where e.scope_key = p.scope_key
             and e.event_type = 'pilot_scope_version_activated'
             and e.scope_version >= p.desde_version
      ) a
),
v as (
    select w.*, w.desde - interval '7 days' as siembra from w
),
ev0 as (
    select e.id, e.event_type, e.received_at, e.processed_at, e.processing_status,
           case when e.processing_error is null then null
                when e.processing_error ~ '^[a-z0-9_:]{1,80}$' then e.processing_error
                else 'otro' end as processing_error,
           case when e.payload ->> 'creation_date' ~ '^[0-9]{1,16}$'
                then (e.payload ->> 'creation_date')::bigint end as creation_date_ms,
           case when e.event_type = 'PURCHASE_APPROVED'
                 and e.payload #>> '{data,purchase,approved_date}' ~ '^[0-9]{10,16}$'
                then (e.payload #>> '{data,purchase,approved_date}')::bigint end as approved_date_ms,
           -- La forma que exige el trigger de compra conocida (20260808000200:63-66).
           case when e.event_type = 'PURCHASE_APPROVED' then coalesce(
                    e.payload #>> '{version}' = '2.0.0'
                and e.payload #>> '{event}' = 'PURCHASE_APPROVED'
                and e.payload #>> '{data,purchase,status}' = 'APPROVED'
                and e.payload #>> '{data,purchase,transaction}' ~ '^HP[A-Z0-9]{6,62}$', false)
           end as purchase_shape_ok,
           case when e.payload #>> '{data,product,id}' ~ '^[0-9]{1,12}$'
                then e.payload #>> '{data,product,id}' end as product_id,
           case when coalesce(e.payload #>> '{data,purchase,offer,code}',
                              e.payload #>> '{data,offer,code}') ~ '^[A-Za-z0-9]{1,32}$'
                then coalesce(e.payload #>> '{data,purchase,offer,code}',
                              e.payload #>> '{data,offer,code}') end as offer_code,
           case when e.payload #>> '{data,purchase,status}' ~ '^[A-Z_]{1,40}$'
                then e.payload #>> '{data,purchase,status}' end as purchase_status,
           -- El telefono como lo toma el parser de cada tipo (hotmart.py:179-181,
           -- :242 y :293-295), como en la v2.
           case e.event_type
                when 'PURCHASE_OUT_OF_SHOPPING_CART'
                    then coalesce(nullif(btrim(e.payload #>> '{data,buyer,phone}'), ''),
                                  nullif(btrim(e.payload #>> '{data,buyer,checkout_phone}'), ''))
                when 'PURCHASE_APPROVED'
                    then nullif(btrim(e.payload #>> '{data,buyer,checkout_phone}'), '')
                else coalesce(nullif(btrim(e.payload #>> '{data,buyer,checkout_phone}'), ''),
                              nullif(btrim(e.payload #>> '{data,buyer,phone}'), ''))
           end as phone_raw,
           case when e.event_type = 'PURCHASE_APPROVED'
                then nullif(regexp_replace(coalesce(e.payload #>> '{data,buyer,checkout_phone}', ''),
                                           '[^0-9]', '', 'g'), '') end as purchase_digits,
           case when e.event_type = 'PURCHASE_APPROVED'
                then nullif(lower(btrim(e.payload #>> '{data,buyer,email}')), '') end as purchase_email
      from public.webhook_events e, v
     where e.source = 'hotmart'
       and e.event_type in ('PURCHASE_OUT_OF_SHOPPING_CART', 'PURCHASE_CANCELED', 'PURCHASE_APPROVED')
       and e.received_at >= v.siembra
),
ev as (
    select ev0.*, public._whatsapp_phone_canonical(ev0.phone_raw) as phone_c from ev0
),
-- Todos los casos de Hotmart de la ventana o en curso: los del carrito y, como
-- hechos de la persona, los del pago rechazado.
cs as (
    select rc.* from public.recovery_cases rc, v
     where rc.source = 'hotmart'
       and (rc.created_at >= v.siembra or rc.status in ('grace_period', 'active', 'paused'))
),
-- Los del carrito: atados al scope del carrito.
cc as (
    select cs.* from cs, v
     where exists (select 1 from public.pilot_recovery_case_bindings b
                    where b.recovery_case_id = cs.id and b.scope_key = v.scope_key)
),
sq as (
    select s.* from public.followup_sequences s where s.recovery_case_id in (select id from cc)
),
ac as (
    select a.* from public.scheduled_actions a where a.recovery_case_id in (select id from cc)
),
-- Los intentos de cada accion, en tramos de intentos iguales consecutivos.
at0 as (
    select t.action_id, t.attempt_number,
           concat_ws('|', t.mode, t.phase, coalesce(t.outcome, '-'),
                     coalesce(t.reason_code, '-'), coalesce(t.reconciliation_resolution, '-')) as clave,
           json_build_object(
               'attempt_number', t.attempt_number, 'mode', t.mode, 'phase', t.phase,
               'outcome', t.outcome, 'started_at', t.started_at,
               'request_started_at', t.request_started_at, 'accepted_at', t.accepted_at,
               'reason_code', case when t.reason_code is null then null
                                   when t.reason_code ~ '^[a-z0-9_:]{1,80}$' then t.reason_code
                                   else 'otro' end,
               'finalized_next_attempt_at', t.finalized_next_attempt_at,
               'reconciliation_resolution', t.reconciliation_resolution,
               'reconciled_at', t.reconciled_at) as obj
      from public.followup_delivery_attempts t
     where t.action_id in (select id from ac)
),
at1 as (
    select at0.*,
           case when lag(at0.clave) over (partition by at0.action_id order by at0.attempt_number)
                     is distinct from at0.clave then 1 else 0 end as corte
      from at0
),
at2 as (
    select at1.*, sum(at1.corte) over (partition by at1.action_id order by at1.attempt_number) as tramo
      from at1
),
atr as (
    select at2.action_id, at2.tramo, count(*) as n, min(at2.attempt_number) as desde_numero,
           (array_agg(at2.obj order by at2.attempt_number asc))[1] as primero,
           (array_agg(at2.obj order by at2.attempt_number desc))[1] as ultimo
      from at2
     group by at2.action_id, at2.tramo
),
-- El primer pedido de cada accion y el del envio siguiente del mismo caso: la
-- respuesta de un envio es la primera que llega entre los dos.
envio0 as (
    select t.action_id, a.recovery_case_id, min(t.request_started_at) as request_started_at
      from public.followup_delivery_attempts t
      join ac a on a.id = t.action_id
     where t.request_started_at is not null
     group by t.action_id, a.recovery_case_id
),
envio as (
    select envio0.*,
           lead(envio0.request_started_at) over (
               partition by envio0.recovery_case_id
               order by envio0.request_started_at, envio0.action_id) as siguiente_at
      from envio0
),
-- Los eventos del caso que la paridad lee, en tramos por tipo, como en la v2.
re0 as (
    select e.id, e.recovery_case_id, e.related_action_id, e.event_type, e.actor_type, e.created_at,
           jsonb_strip_nulls(jsonb_build_object(
               'decision', e.data -> 'decision',
               'reason_code', case when e.data ->> 'reason_code' ~ '^[a-z0-9_:]{1,80}$' then e.data -> 'reason_code'
                                   when e.data ? 'reason_code' then '"otro"'::jsonb end,
               'policy_key', case when e.data ->> 'policy_key' ~ '^[a-z0-9_:-]{1,80}$' then e.data -> 'policy_key' end,
               'policy_version', case when jsonb_typeof(e.data -> 'policy_version') = 'number'
                                      then e.data -> 'policy_version' end,
               'chatwoot_checked', e.data -> 'chatwoot_checked',
               'chatwoot_inbound_after_anchor', e.data -> 'chatwoot_inbound_after_anchor',
               'chatwoot_human_activity_after_anchor', e.data -> 'chatwoot_human_activity_after_anchor',
               'chatwoot_automation_paused', e.data -> 'chatwoot_automation_paused')) as d
      from public.conversation_events e
     where e.recovery_case_id in (select id from cc)
       and e.event_type in ('followup_action_reevaluated', 'purchase_detected',
                            'followup_action_expired', 'payment_failure_aggregated')
),
re1 as (
    select re0.*,
           case when lag(re0.actor_type || '|' || re0.d::text) over (
                         partition by re0.recovery_case_id, re0.related_action_id, re0.event_type
                         order by re0.created_at, re0.id)
                     is distinct from (re0.actor_type || '|' || re0.d::text) then 1 else 0 end as corte
      from re0
),
re2 as (
    select re1.*,
           sum(re1.corte) over (partition by re1.recovery_case_id, re1.related_action_id, re1.event_type
                                order by re1.created_at, re1.id) as tramo
      from re1
),
rer as (
    select re2.recovery_case_id, re2.related_action_id, re2.event_type, re2.tramo,
           min(re2.actor_type) as actor_type, (array_agg(re2.d))[1] as d,
           count(*) as n, min(re2.created_at) as primero_at, max(re2.created_at) as ultimo_at
      from re2
     group by re2.recovery_case_id, re2.related_action_id, re2.event_type, re2.tramo
),
cu as (
    select c.* from public.recovery_coupons c where c.recovery_case_id in (select id from cc)
),
li as (
    select i.* from public.checkout_link_issuances i
     where i.source_kind = 'recovery_first_touch'
       and i.recovery_case_id in (select id from cc)
),
rn as (
    select r.* from public.recovery_renewal_requests r where r.recovery_case_id in (select id from cc)
),
pl as (
    select x.* from public.portable_precheckout_first_contact_plans x, v where x.created_at >= v.siembra
),
it as (
    select i.* from public.purchase_intents i
     where i.id in (select purchase_intent_id from pl where purchase_intent_id is not null)
        or i.id in (select c.purchase_intent_id from public.hotmart_purchase_intent_correlations c
                     where c.webhook_event_id in (select id from ev))
        or i.id in (select d.purchase_intent_id from public.commercial_ally_payment_failure_details d
                     where d.webhook_event_id in (select id from ev))
        or i.id in (select b.audience_purchase_intent_id from public.pilot_recovery_case_bindings b
                     where b.recovery_case_id in (select id from cs))
),
ct as (
    select c.* from public.contacts c
     where c.id in (select contact_id from cs
                    union select contact_id from pl where contact_id is not null
                    union select a.contact_id from public.pilot_outbound_request_authorizations a)
)
select json_build_object(
  '_captured', json_build_object(
      'origin', 'att1-db (stack setter-att1), psql con default_transaction_read_only',
      'query', 'captura-paridad-recuperacion-v3',
      'captured_at', (select captured_at from v),
      'desde', (select desde from v),
      'desde_origen', (select 'pilot_scope_version_activated:' || scope_key || ':v>=' || desde_version from v),
      'siembra', (select siembra from v),
      'redactions', json_build_array(
          'personas: sha256 truncado a 16 hex con una sal aleatoria por corrida que no sale de la consulta; prefijos p: (telefono canonico), d: (digitos tal cual) y e: (mail)',
          'sin payload, telefono, email, nombre, texto, nota, refusal_reason, external_user_id, transaccion ni nombre de operador',
          'config: lista cerrada de claves, sin approved_by, changed_by, change_reason ni actor',
          'processing_error, reason_code y to_state fuera de su regex salen como otro o null',
          'reevaluaciones e intentos repetidos: en tramos (cantidad, primero y ultimo)',
          'cupones sin codigo ni id de Hotmart; links sin URL, sck, ulid ni ids de Chatwoot; respuestas solo como hora'),
      'conteos', json_build_object(
          'eventos', (select count(*) from ev),
          'disparos_desde', (select count(*) from ev, v
                               where ev.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
                                 and ev.received_at >= v.desde),
          'casos', (select count(*) from cs),
          'casos_del_carrito', (select count(*) from cc),
          'secuencias', (select count(*) from sq),
          'acciones', (select count(*) from ac),
          'intentos', (select count(*) from at0),
          'intentos_tramos', (select count(*) from atr),
          'envios', (select count(*) from at0 where (at0.obj ->> 'request_started_at') is not null),
          'reevaluaciones', (select count(*) from re0),
          'reevaluaciones_tramos', (select count(*) from rer),
          'cupones', (select count(*) from cu),
          'links', (select count(*) from li),
          'renovaciones', (select count(*) from rn),
          'autorizaciones', (select count(*) from public.pilot_outbound_request_authorizations),
          'primer_contacto', (select count(*) from pl),
          'intenciones', (select count(*) from it),
          'contactos', (select count(*) from ct)),
      -- Para conciliar el denominador: todos los eventos, sin ventana.
      'censo_webhook_events', (select coalesce(json_agg(json_build_object(
              'source', x.source, 'event_type', x.event_type, 'n', x.n,
              'primero', x.primero, 'ultimo', x.ultimo) order by x.source, x.event_type), '[]'::json)
          from (select we.source, we.event_type, count(*) as n,
                       min(we.received_at) as primero, max(we.received_at) as ultimo
                  from public.webhook_events we
                 where we.source ~ '^[a-z_]{1,40}$' and we.event_type ~ '^[A-Za-z0-9_.]{1,80}$'
                 group by we.source, we.event_type) x)),
  'config', json_build_object(
      'pilot_scope_versions', (select coalesce(json_agg(json_build_object(
              'scope_key', s.scope_key, 'version', s.version, 'status', s.status,
              'tenant_key', s.tenant_key, 'source', s.source,
              'source_event_type', s.source_event_type,
              'additional_source_event_types', s.additional_source_event_types,
              'purpose', s.purpose, 'timezone', s.timezone,
              'external_product_id', s.external_product_id, 'offer_code', s.offer_code,
              'additional_offer_codes', s.additional_offer_codes,
              'policy_key', s.policy_key, 'policy_version', s.policy_version,
              'audience_mode', s.audience_mode,
              'max_outbound_request_starts_per_day', s.max_outbound_request_starts_per_day,
              'max_outbound_request_starts_total', s.max_outbound_request_starts_total,
              'published_at', s.published_at)
          order by s.scope_key, s.version), '[]'::json) from public.pilot_scope_versions s),
      'followup_policy_versions', (select coalesce(json_agg(json_build_object(
              'policy_key', f.policy_key, 'version', f.version, 'status', f.status,
              'purpose', f.purpose, 'timezone', f.timezone,
              'business_windows', (select coalesce(json_agg(json_build_object(
                      'days', bw -> 'days', 'start', bw ->> 'start', 'end', bw ->> 'end')), '[]'::json)
                  from jsonb_array_elements(f.business_windows) bw),
              'grace_period', f.grace_period, 'expires_after', f.expires_after,
              'max_automatic_messages', f.max_automatic_messages,
              -- Cada paso con sus marcas (20261019000600). El delay sale como
              -- intervalo de Postgres ('24 hours' -> '24:00:00'), solo si tiene
              -- la forma del legado; si no, 'no_se_lee', que no pasa la forma
              -- y frena la captura en voz alta.
              'steps', (select coalesce(json_agg(json_build_object(
                      'step_key', st ->> 'step_key', 'mode', st ->> 'mode',
                      'delay', case when st ->> 'delay' is null then null
                                    when st ->> 'delay' ~ '^[0-9]{1,6} (second|minute|hour|day)s?$'
                                        then ((st ->> 'delay')::interval)::text
                                    else 'no_se_lee' end,
                      'approved_template', case when jsonb_typeof(st -> 'approved_template') = 'boolean'
                                                then (st -> 'approved_template')::text::boolean end,
                      'coupon_ordinal', case when (st ->> 'coupon_ordinal') ~ '^[12]$'
                                             then (st ->> 'coupon_ordinal')::integer end)
                      order by ord), '[]'::json)
                  from jsonb_array_elements(f.steps) with ordinality as x(st, ord)),
              'published_at', f.published_at)
          order by f.policy_key, f.version), '[]'::json) from public.followup_policy_versions f),
      'pilot_runtime_controls', (select coalesce(json_agg(json_build_object(
              'scope_key', c.scope_key, 'scope_version', c.scope_version,
              'runtime_state', c.runtime_state, 'generation', c.generation,
              'changed_at', c.changed_at)
          order by c.scope_key), '[]'::json) from public.pilot_runtime_controls c),
      'pilot_proactive_contact_caps', (select coalesce(json_agg(json_build_object(
              'tenant_key', k.tenant_key, 'max_request_starts', k.max_request_starts,
              'request_window', k.request_window)), '[]'::json)
          from public.pilot_proactive_contact_caps k),
      'pilot_control_events_control_level', (select coalesce(json_agg(json_build_object(
              'scope_key', e.scope_key, 'scope_version', e.scope_version,
              'event_type', e.event_type, 'runtime_generation', e.runtime_generation,
              'reason_code', case when e.reason_code ~ '^[a-z0-9_:]{1,80}$' then e.reason_code else 'otro' end,
              'to_state', case when (e.data ->> 'to_state') ~ '^[a-z][a-z_]{0,31}$'
                               then e.data ->> 'to_state' end,
              'created_at', e.created_at)
          order by e.created_at, e.runtime_generation, e.id), '[]'::json)
          from public.pilot_control_events e
         where e.contact_id is null and e.action_id is null and e.attempt_id is null)),
  'eventos', (select coalesce(json_agg(json_build_object(
        'id', ev.id, 'event_type', ev.event_type, 'received_at', ev.received_at,
        'processed_at', ev.processed_at, 'processing_status', ev.processing_status,
        'processing_error', ev.processing_error, 'creation_date_ms', ev.creation_date_ms,
        'approved_date_ms', ev.approved_date_ms, 'purchase_shape_ok', ev.purchase_shape_ok,
        'product_id', ev.product_id, 'offer_code', ev.offer_code, 'purchase_status', ev.purchase_status,
        'person_key', case when ev.phone_c is null then null
                           else left(encode(sha256(convert_to(v.sal || ':p:' || ev.phone_c, 'UTF8')), 'hex'), 16) end,
        'digits_key', case when ev.purchase_digits is null then null
                           else left(encode(sha256(convert_to(v.sal || ':d:' || ev.purchase_digits, 'UTF8')), 'hex'), 16) end,
        'email_key', case when ev.purchase_email is null then null
                          else left(encode(sha256(convert_to(v.sal || ':e:' || ev.purchase_email, 'UTF8')), 'hex'), 16) end,
        'funnel_ref', case when b.funnel_ref ~ '^[a-z0-9_-]{1,80}$' then b.funnel_ref end,
        'cart_correlation', case when hc.webhook_event_id is null then null else json_build_object(
            'outcome', hc.outcome, 'purchase_intent_id', hc.purchase_intent_id,
            'reason_code', case when hc.reason_code ~ '^[a-z0-9_:]{1,80}$' then hc.reason_code else 'otro' end,
            'observed_at', hc.observed_at) end,
        'payment_failure_correlation', case when pf.webhook_event_id is null then null else json_build_object(
            'outcome', pf.correlation_outcome, 'purchase_intent_id', pf.purchase_intent_id) end
      ) order by ev.received_at, ev.id), '[]'::json)
      from ev cross join v
      left join public.commercial_ally_hotmart_event_bindings b on b.webhook_event_id = ev.id
      left join public.hotmart_purchase_intent_correlations hc on hc.webhook_event_id = ev.id
      left join public.commercial_ally_payment_failure_details pf on pf.webhook_event_id = ev.id),
  'conflictos_de_compra', (select coalesce(json_agg(json_build_object(
        'id', k.id, 'existing_event_id', k.existing_event_id,
        'detected_at', k.detected_at, 'resolved_at', k.resolved_at)
      order by k.detected_at, k.id), '[]'::json)
      from public.hotmart_purchase_semantic_conflicts k),
  'intenciones', (select coalesce(json_agg(json_build_object(
        'id', it.id, 'funnel_ref', it.funnel_ref, 'product_ref', it.product_ref, 'offer_ref', it.offer_ref,
        'submitted_at', it.submitted_at, 'lifecycle_state', it.lifecycle_state,
        'current_classification', it.current_classification,
        'whatsapp_contact_authorized', it.whatsapp_contact_authorized, 'provisional', it.provisional,
        'provider_observed', it.provider_observed, 'activation_authorized', it.activation_authorized,
        'updated_at', it.updated_at,
        'person_key', case when public._whatsapp_phone_canonical(it.normalized_phone) is null then null
                           else left(encode(sha256(convert_to(v.sal || ':p:'
                                || public._whatsapp_phone_canonical(it.normalized_phone), 'UTF8')), 'hex'), 16) end
      ) order by it.submitted_at, it.id), '[]'::json) from it cross join v),
  'contactos', (select coalesce(json_agg(json_build_object(
        'id', ct.id,
        'person_keys', (select coalesce(json_agg(distinct left(encode(sha256(convert_to(v.sal || ':p:' || k.c, 'UTF8')), 'hex'), 16)), '[]'::json)
                          from (select public._whatsapp_phone_canonical(ct.phone) as c
                                union select public._whatsapp_phone_canonical(cp.normalized_value)
                                  from public.contact_points cp where cp.contact_id = ct.id and cp.type = 'phone') k
                         where k.c is not null),
        'digits_keys', (select coalesce(json_agg(distinct left(encode(sha256(convert_to(v.sal || ':d:' || k.c, 'UTF8')), 'hex'), 16)), '[]'::json)
                          from (select nullif(regexp_replace(coalesce(ct.phone, ''), '[^0-9]', '', 'g'), '') as c
                                union select nullif(cp.normalized_value, '')
                                  from public.contact_points cp where cp.contact_id = ct.id and cp.type = 'phone') k
                         where k.c is not null),
        'email_keys', (select coalesce(json_agg(distinct left(encode(sha256(convert_to(v.sal || ':e:' || k.m, 'UTF8')), 'hex'), 16)), '[]'::json)
                         from (select nullif(lower(btrim(ct.email)), '') as m
                               union select nullif(cp.normalized_value, '')
                                 from public.contact_points cp where cp.contact_id = ct.id and cp.type = 'email') k
                        where k.m is not null)
      ) order by ct.id), '[]'::json) from ct cross join v),
  'casos', (select coalesce(json_agg(json_build_object(
        'id', cs.id, 'contact_id', cs.contact_id, 'abandonment_event_id', cs.abandonment_event_id,
        'purchase_event_id', cs.purchase_event_id, 'external_product_id', cs.external_product_id,
        'offer_code', cs.offer_code, 'status', cs.status, 'grace_expires_at', cs.grace_expires_at,
        'created_at', cs.created_at, 'closed_at', cs.closed_at, 'won_at', cs.won_at,
        'policy_key', cs.policy_key, 'policy_version', cs.policy_version,
        'trigger_kind', cs.context ->> 'trigger_kind',
        'purchase_correlation', case when cs.context ->> 'purchase_correlation' ~ '^[a-z0-9_:]{1,80}$'
                                     then cs.context ->> 'purchase_correlation' end,
        'has_conversation', cs.conversation_id is not null,
        'binding', (select json_build_object('scope_key', b.scope_key, 'scope_version', b.scope_version,
                        'source_event_id', b.source_event_id, 'audience_mode', b.audience_mode,
                        'audience_purchase_intent_id', b.audience_purchase_intent_id, 'bound_at', b.bound_at)
                      from public.pilot_recovery_case_bindings b where b.recovery_case_id = cs.id),
        'eventos', (select coalesce(json_agg(json_build_object('webhook_event_id', r.webhook_event_id,
                        'event_role', r.event_role, 'observed_at', r.observed_at)
                        order by r.observed_at, r.webhook_event_id), '[]'::json)
                      from public.recovery_case_events r where r.recovery_case_id = cs.id)
      ) order by cs.created_at, cs.id), '[]'::json) from cs),
  'secuencias', (select coalesce(json_agg(json_build_object(
        'id', sq.id, 'recovery_case_id', sq.recovery_case_id, 'status', sq.status,
        'reason', sq.reason, 'policy_key', sq.policy_key, 'policy_version', sq.policy_version,
        'current_step', sq.current_step, 'max_attempts', sq.max_attempts,
        'automatic_messages_accepted', sq.automatic_messages_accepted,
        'started_at', sq.started_at, 'completed_at', sq.completed_at, 'cancelled_at', sq.cancelled_at,
        'completion_reason', case when sq.completion_reason is null then null
                                  when sq.completion_reason ~ '^[a-z0-9_:]{1,80}$' then sq.completion_reason
                                  else 'otro' end,
        'cancel_reason', case when sq.cancel_reason is null then null
                              when sq.cancel_reason ~ '^[a-z0-9_:]{1,80}$' then sq.cancel_reason
                              else 'otro' end
      ) order by sq.started_at, sq.id), '[]'::json) from sq),
  'acciones', (select coalesce(json_agg(json_build_object(
        'id', ac.id, 'recovery_case_id', ac.recovery_case_id,
        'followup_sequence_id', ac.followup_sequence_id, 'action_type', ac.action_type,
        'status', ac.status, 'due_at', ac.due_at, 'expires_at', ac.expires_at,
        'step_key', ac.step_key, 'anchor_type', ac.anchor_type,
        'anchor_observed_at', ac.anchor_observed_at, 'next_attempt_at', ac.next_attempt_at,
        'execution_attempt_count', ac.execution_attempt_count,
        'terminal_reason', case when ac.terminal_reason is null then null
                                when ac.terminal_reason ~ '^[a-z0-9_:]{1,80}$' then ac.terminal_reason else 'otro' end,
        'executed_at', ac.executed_at, 'created_at', ac.created_at, 'updated_at', ac.updated_at,
        'intentos', (select coalesce(json_agg(json_build_object(
                        'n', r.n, 'primero', r.primero,
                        'ultimo', case when r.n > 1 then r.ultimo end)
                        order by r.desde_numero), '[]'::json)
                       from atr r where r.action_id = ac.id),
        -- La primera respuesta despues del primer pedido de esta accion y hasta
        -- el pedido siguiente del caso, solo como hora, de cada fuente. Sin
        -- pedido, o sin respuesta, no sale.
        'respuesta', (select case when x.adopcion_at is null and x.contesto_at is null
                                       and x.renovacion_at is null then null
                                  else json_build_object('adopcion_at', x.adopcion_at,
                                                         'contesto_at', x.contesto_at,
                                                         'renovacion_at', x.renovacion_at) end
                        from envio e
                        cross join lateral (select
                            (select min(ce.created_at) from public.conversation_events ce
                              where ce.recovery_case_id = ac.recovery_case_id
                                and ce.event_type = 'inbound_adopted_template_conversation'
                                and ce.created_at > e.request_started_at
                                and (e.siguiente_at is null or ce.created_at <= e.siguiente_at)) as adopcion_at,
                            (select min(ce.created_at) from public.conversation_events ce
                              where ce.recovery_case_id = ac.recovery_case_id
                                and ce.event_type = 'followup_action_reevaluated'
                                and ce.data ->> 'chatwoot_inbound_after_anchor' = 'true'
                                and ce.created_at > e.request_started_at
                                and (e.siguiente_at is null or ce.created_at <= e.siguiente_at)) as contesto_at,
                            (select min(r.occurred_at) from rn r
                              where r.recovery_case_id = ac.recovery_case_id
                                and r.occurred_at > e.request_started_at
                                and (e.siguiente_at is null or r.occurred_at <= e.siguiente_at)) as renovacion_at) x
                       where e.action_id = ac.id)
      ) order by ac.created_at, ac.id), '[]'::json) from ac),
  'cupones', (select coalesce(json_agg(json_build_object(
        'id', cu.id, 'recovery_case_id', cu.recovery_case_id, 'ordinal', cu.ordinal,
        'status', cu.status, 'expires_at', cu.expires_at,
        'failure_code', case when cu.failure_code is null then null
                             when cu.failure_code ~ '^[a-z0-9_:]{1,80}$' then cu.failure_code else 'otro' end,
        'attempt_count', cu.attempt_count, 'next_attempt_at', cu.next_attempt_at,
        'verified_at', cu.verified_at, 'redeemed_webhook_event_id', cu.redeemed_webhook_event_id,
        'created_at', cu.created_at, 'updated_at', cu.updated_at
      ) order by cu.created_at, cu.id), '[]'::json) from cu),
  'links', (select coalesce(json_agg(json_build_object(
        'id', li.id, 'recovery_case_id', li.recovery_case_id,
        'delivery_attempt_id', li.delivery_attempt_id,
        'action_id', (select t.action_id from public.followup_delivery_attempts t
                       where t.id = li.delivery_attempt_id),
        'attempt_number', (select t.attempt_number from public.followup_delivery_attempts t
                            where t.id = li.delivery_attempt_id),
        'status', li.status,
        'offer_resolution', case when li.offer_resolution ~ '^[a-z][a-z0-9_]{0,79}$'
                                 then li.offer_resolution else 'otro' end,
        'attribution_resolution', case when li.attribution_resolution ~ '^[a-z][a-z0-9_]{0,79}$'
                                       then li.attribution_resolution else 'otro' end,
        'failure_code', case when li.failure_code is null then null
                             when li.failure_code ~ '^[a-z0-9_:]{1,80}$' then li.failure_code else 'otro' end,
        'comprado', li.status = 'purchase_matched',
        'hotmart_webhook_event_id', li.hotmart_webhook_event_id,
        'created_at', li.created_at, 'finalized_at', li.finalized_at, 'purchased_at', li.purchased_at
      ) order by li.created_at, li.id), '[]'::json) from li),
  -- El efecto del pedido sobre su paso: request_recovery_renewal_v1 escribe
  -- due_at = least(due_at, now()) y el pedido con created_at = now(), en la
  -- misma transaccion (20261019000600:321-334). Igual: lo adelanto; antes: ya
  -- vencia; despues: otra cosa lo movio mas tarde (un diferido por horario).
  'renovaciones', (select coalesce(json_agg(json_build_object(
        'action_id', rn.action_id, 'recovery_case_id', rn.recovery_case_id,
        'occurred_at', rn.occurred_at, 'created_at', rn.created_at,
        'efecto', case when a.due_at = rn.created_at then 'adelanto'
                       when a.due_at < rn.created_at then 'ya_vencia'
                       else 'movido_despues' end
      ) order by rn.created_at, rn.action_id), '[]'::json)
      from rn join public.scheduled_actions a on a.id = rn.action_id),
  'autorizaciones', (select coalesce(json_agg(json_build_object(
        'scope_key', a.scope_key, 'scope_version', a.scope_version, 'action_id', a.action_id,
        'contact_id', a.contact_id, 'local_budget_date', a.local_budget_date,
        'runtime_generation', a.runtime_generation, 'authorized_at', a.authorized_at
      ) order by a.authorized_at, a.id), '[]'::json) from public.pilot_outbound_request_authorizations a),
  'reevaluaciones', (select coalesce(json_agg(json_build_object(
        'recovery_case_id', r.recovery_case_id, 'related_action_id', r.related_action_id,
        'event_type', r.event_type, 'actor_type', r.actor_type, 'data', r.d,
        'n', r.n, 'primero_at', r.primero_at, 'ultimo_at', r.ultimo_at
      ) order by r.primero_at, r.recovery_case_id, r.event_type), '[]'::json) from rer r),
  'primer_contacto', (select coalesce(json_agg(json_build_object(
        'purchase_intent_id', pl.purchase_intent_id, 'contact_id', pl.contact_id,
        'recovery_case_id', pl.recovery_case_id, 'scope_key', pl.scope_key,
        'scope_version', pl.scope_version, 'outcome', pl.outcome,
        'reason_code', case when pl.reason_code is null then null
                            when pl.reason_code ~ '^[a-z0-9_:]{1,80}$' then pl.reason_code else 'otro' end,
        'created_at', pl.created_at,
        'caso_status', rc.status,
        'accion_status', a.status,
        'accion_terminal_reason', case when a.terminal_reason is null then null
                                       when a.terminal_reason ~ '^[a-z0-9_:]{1,80}$' then a.terminal_reason
                                       else 'otro' end,
        'accion_due_at', a.due_at, 'accion_expires_at', a.expires_at,
        'accion_cerrada_at', case when a.status in ('pending', 'deferred', 'retryable_failed') then null
                                  else a.updated_at end,
        'request_started_at', (select min(t.request_started_at) from public.followup_delivery_attempts t
                                where t.action_id = a.id)
      ) order by pl.created_at, pl.submission_id), '[]'::json)
      from pl
      left join public.recovery_cases rc on rc.id = pl.recovery_case_id
      left join lateral (select x.* from public.scheduled_actions x
                          where x.recovery_case_id = pl.recovery_case_id
                          order by x.created_at, x.id limit 1) a on true)
);
