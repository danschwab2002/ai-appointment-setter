-- captura-paridad-recuperacion-v2: el dato real para la tajada 1 del motor de flujos
-- (diseño del 2026-10-16, revisado después de tres verificadores adversariales).
--
-- Solo lectura. Devuelve una sola fila con un solo JSON. Se corre con
-- default_transaction_read_only y el SQL por la entrada estándar.
--
-- Qué NO sale nunca: payloads, teléfonos, mails, nombres, textos libres, notas,
-- refusal_reason, external_user_id, transacciones de Hotmart ni nombres de
-- operador (approved_by, changed_by, change_reason, actor, requested_by).
--   * Las personas salen como sha256 truncado a 16 hex, con una sal aleatoria
--     por corrida que no sale de la consulta, y un prefijo por tipo de clave
--     (p: teléfono canónico, d: dígitos tal cual, e: mail).
--   * config sale con una lista cerrada de claves, armada campo por campo.
--   * Todo motivo sale solo si cumple ^[a-z0-9_:]{1,80}$; si no, sale 'otro'.
--
-- La ventana: arranca con la activación de la v3 del scope del carrito
-- (att1-recuperacion, pilot_scope_version_activated con scope_version >= 3).
-- La v2 tenía cohorte y no vuelve a correr. La siembra son los 7 días previos.
-- Si esa activación no existe, la consulta falla entera y no devuelve nada a medias.
--
-- Lo que la tajada 1 no usa no se captura: no hay secuencias, bajas,
-- derivaciones ni la foto de las conversaciones. Las reevaluaciones repetidas y
-- los intentos repetidos de una misma acción salen como tramos: la cantidad,
-- el primero y el último.
with p as (
    select clock_timestamp() as captured_at,
           gen_random_uuid()::text as sal,
           'att1-recuperacion'::text as scope_key,
           3 as desde_version
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
           -- El teléfono como lo toma el parser de cada tipo: el carrito, phone y
           -- después checkout_phone (hotmart.py:179-181); la compra, solo
           -- checkout_phone (:242); el pago fallido y los demás, checkout_phone y
           -- después phone (:293-295).
           case e.event_type
                when 'PURCHASE_OUT_OF_SHOPPING_CART'
                    then coalesce(nullif(btrim(e.payload #>> '{data,buyer,phone}'), ''),
                                  nullif(btrim(e.payload #>> '{data,buyer,checkout_phone}'), ''))
                when 'PURCHASE_APPROVED'
                    then nullif(btrim(e.payload #>> '{data,buyer,checkout_phone}'), '')
                else coalesce(nullif(btrim(e.payload #>> '{data,buyer,checkout_phone}'), ''),
                              nullif(btrim(e.payload #>> '{data,buyer,phone}'), ''))
           end as phone_raw,
           -- La compra se identifica también por los dígitos tal cual y por el mail
           -- (20260808000200:80-124): solo para PURCHASE_APPROVED.
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
cs as (
    select rc.* from public.recovery_cases rc, v
     where rc.source = 'hotmart'
       and (rc.created_at >= v.siembra or rc.status in ('grace_period', 'active', 'paused'))
),
ac as (
    select a.* from public.scheduled_actions a where a.recovery_case_id in (select id from cs)
),
-- Los intentos de cada acción, en tramos de intentos iguales consecutivos.
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
-- Los eventos del caso que la tajada 1 lee, en tramos por tipo: una
-- reevaluación repetida igual sale una vez, con la cantidad, la primera y la última.
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
     where e.recovery_case_id in (select id from cs)
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
      'query', 'captura-paridad-recuperacion-v2',
      'captured_at', (select captured_at from v),
      'desde', (select desde from v),
      'desde_origen', (select 'pilot_scope_version_activated:' || scope_key || ':v>=' || desde_version from v),
      'siembra', (select siembra from v),
      'redactions', json_build_array(
          'personas: sha256 truncado a 16 hex con una sal aleatoria por corrida que no sale de la consulta; prefijos p: (telefono canonico), d: (digitos tal cual) y e: (mail)',
          'sin payload, telefono, email, nombre, texto, nota, refusal_reason, external_user_id, transaccion ni nombre de operador',
          'config: lista cerrada de claves, sin approved_by, changed_by, change_reason ni actor',
          'processing_error, reason_code y to_state fuera de su regex salen como otro o null',
          'reevaluaciones e intentos repetidos: en tramos (cantidad, primero y ultimo)'),
      'conteos', json_build_object(
          'eventos', (select count(*) from ev),
          'disparos_desde', (select count(*) from ev, v
                               where ev.event_type in ('PURCHASE_OUT_OF_SHOPPING_CART', 'PURCHASE_CANCELED')
                                 and ev.received_at >= v.desde),
          'casos', (select count(*) from cs),
          'acciones', (select count(*) from ac),
          'intentos', (select count(*) from at0),
          'intentos_tramos', (select count(*) from atr),
          'envios', (select count(*) from at0 where (at0.obj ->> 'request_started_at') is not null),
          'reevaluaciones', (select count(*) from re0),
          'reevaluaciones_tramos', (select count(*) from rer),
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
              'steps', (select coalesce(json_agg(json_build_object(
                      'step_key', st ->> 'step_key', 'mode', st ->> 'mode')), '[]'::json)
                  from jsonb_array_elements(f.steps) st),
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
              -- El estado destino (20260810000100:516-518), con la regex de la 1a
              -- (20261015000200:1016). La activación de una versión deja el
              -- control en inactive y no trae to_state.
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
        -- Las dos formas con que la compra conocida busca al contacto
        -- (20260808000200:80-124): los dígitos tal cual y el mail.
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
  'acciones', (select coalesce(json_agg(json_build_object(
        'id', ac.id, 'recovery_case_id', ac.recovery_case_id, 'action_type', ac.action_type,
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
                       from atr r where r.action_id = ac.id)
      ) order by ac.created_at, ac.id), '[]'::json) from ac),
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
  -- El primer contacto, plegado: el plan y lo que pasó con su acción. Alcanza
  -- para P6 (quién le gana a quién) sin traer sus casos ni sus reevaluaciones.
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
