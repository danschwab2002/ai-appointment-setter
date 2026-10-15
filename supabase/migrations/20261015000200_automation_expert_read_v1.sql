-- automation-expert-read-v1: las tres lecturas del Automation Expert (1a).
-- Contrato: docs/contracts/automation-expert-read-v1.md.
--
-- Solo leen. Son STABLE SECURITY DEFINER, con search_path fijo, y solo las
-- ejecuta service_role: el bridge las llama por PostgREST con su cliente de
-- siempre. No suman tablas ni permisos sobre tablas.
--
-- La instancia la arma el bridge con su manifiesto y sus variables, nunca con
-- el pedido (p_instance). El tenant solo no separa instancias: cada fila entra
-- solo si se ata a la instancia por la bandeja de Chatwoot, el producto de
-- Hotmart, el scope del agente o el scope entrante.
--
-- Nunca salen el telefono, el mail, el nombre completo, el contenido de un
-- mensaje, la URL de un link, la nota privada de una derivacion ni un payload
-- crudo. Las respuestas se arman con claves nombradas (jsonb_build_object),
-- nunca con to_jsonb de una fila. Todo codigo de texto libre pasa por la forma
-- de los motivos (^[a-z][a-z0-9_:]{0,79}$) antes de salir; el que no la cumple
-- sale null, marcado.

begin;

create or replace function public.read_automation_expert_flows_v1(
    p_instance jsonb
)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_tenant text;
    v_inbox bigint;
    v_product text;
    v_inbound_scope text;
    v_tz text;
    v_today date;
    v_window_from timestamptz;
    v_window_to timestamptz;
    v_scopes jsonb;
    v_policies jsonb;
    v_cap jsonb;
    v_observed jsonb;
    v_without_flow jsonb;
begin
    if p_instance is null or jsonb_typeof(p_instance) <> 'object'
       or coalesce(p_instance ->> 'tenant_ref', '') !~ '^[a-z0-9][a-z0-9-]{0,127}$'
       or coalesce(p_instance ->> 'chatwoot_inbox_id', '') !~ '^[1-9][0-9]{0,17}$'
       or coalesce(p_instance ->> 'hotmart_product_id', '') !~ '^[1-9][0-9]{0,17}$'
       or coalesce(p_instance ->> 'inbound_scope_key', '') !~ '^[a-z0-9][a-z0-9-]{0,127}$'
       or coalesce(p_instance ->> 'time_zone', '') !~ '^[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}$' then
        raise exception using errcode = '22023', message = 'automation_expert_invalid_instance';
    end if;
    v_tenant := p_instance ->> 'tenant_ref';
    v_inbox := (p_instance ->> 'chatwoot_inbox_id')::bigint;
    v_product := p_instance ->> 'hotmart_product_id';
    v_inbound_scope := p_instance ->> 'inbound_scope_key';
    v_tz := p_instance ->> 'time_zone';

    -- Los 7 dias completos de la zona de la instancia, sin el de hoy.
    v_today := (now() at time zone v_tz)::date;
    v_window_to := (v_today::timestamp at time zone v_tz);
    v_window_from := ((v_today - 7)::timestamp at time zone v_tz);

    -- Los scopes que el bridge dice que corre, solo si su bandeja y su
    -- producto son los de la instancia.
    with configured as (
        select item.value ->> 'flow_group' as flow_group,
               item.value ->> 'key' as scope_key,
               (item.value ->> 'version')::integer as scope_version
          from jsonb_array_elements(coalesce(p_instance -> 'scopes', '[]'::jsonb)) item
         where jsonb_typeof(item.value) = 'object'
           and coalesce(item.value ->> 'key', '') ~ '^[a-z0-9][a-z0-9-]{0,127}$'
           and coalesce(item.value ->> 'version', '') ~ '^[1-9][0-9]{0,8}$'
           and coalesce(item.value ->> 'flow_group', '') in ('recuperacion', 'precheckout')
    )
    select coalesce(jsonb_agg(
        jsonb_build_object(
            'flow_group', configured.flow_group,
            'scope_key', configured.scope_key,
            'version', configured.scope_version,
            'bound_to_instance', scope.scope_key is not null,
            'status', scope.status,
            'policy_key', scope.policy_key,
            'policy_version', scope.policy_version,
            'timezone', scope.timezone,
            'audience_mode', scope.audience_mode,
            'source_event_type', scope.source_event_type,
            'additional_source_event_types', to_jsonb(coalesce(scope.additional_source_event_types, '{}'::text[])),
            'max_outbound_request_starts_per_day', scope.max_outbound_request_starts_per_day,
            'max_outbound_request_starts_total', scope.max_outbound_request_starts_total,
            'published_at', scope.published_at,
            'runtime_state', control.runtime_state,
            'runtime_changed_at', control.changed_at,
            'version_activated_at', (
                select max(event.created_at)
                  from public.pilot_control_events event
                 where scope.scope_key is not null
                   and event.scope_key = configured.scope_key
                   and event.event_type = 'pilot_scope_version_activated'
                   and (event.data ->> 'to_version') = configured.scope_version::text
            ),
            'local_budget_date', case when scope.scope_key is not null
                then (now() at time zone coalesce(scope.timezone, v_tz))::date end,
            'starts_today', (
                select count(*)
                  from public.pilot_outbound_request_authorizations auth
                 where scope.scope_key is not null
                   and auth.scope_key = configured.scope_key
                   and auth.local_budget_date = (now() at time zone coalesce(scope.timezone, v_tz))::date
            ),
            'starts_total', (
                select count(*)
                  from public.pilot_outbound_request_authorizations auth
                 where scope.scope_key is not null
                   and auth.scope_key = configured.scope_key
            )
        ) order by configured.flow_group), '[]'::jsonb)
      into v_scopes
      from configured
      left join public.pilot_scope_versions scope
        on scope.scope_key = configured.scope_key
       and scope.version = configured.scope_version
       and scope.chatwoot_inbox_id = v_inbox
       and scope.external_product_id = v_product
      left join public.pilot_runtime_controls control
        on control.scope_key = scope.scope_key
       and control.scope_version = scope.version;

    select coalesce(jsonb_agg(
        jsonb_build_object(
            'policy_key', policy.policy_key,
            'version', policy.version,
            'status', policy.status,
            'timezone', policy.timezone,
            'business_windows', policy.business_windows,
            'grace_period_seconds', extract(epoch from policy.grace_period)::bigint,
            'expires_after_seconds', extract(epoch from policy.expires_after)::bigint,
            'max_automatic_messages', policy.max_automatic_messages,
            'step_keys', (
                select coalesce(jsonb_agg(step.value ->> 'step_key' order by step.ordinality), '[]'::jsonb)
                  from jsonb_array_elements(policy.steps) with ordinality step
                 where (step.value ->> 'step_key') ~ '^[a-z][a-z0-9_]{0,79}$'
            )
        ) order by policy.policy_key, policy.version), '[]'::jsonb)
      into v_policies
      from public.followup_policy_versions policy
     where (policy.policy_key, policy.version) in (
        select (scope_item ->> 'policy_key'), (scope_item ->> 'policy_version')::integer
          from jsonb_array_elements(v_scopes) scope_item
         where (scope_item ->> 'bound_to_instance')::boolean
     );

    select jsonb_build_object(
        'configured', count(*) > 0,
        'max_request_starts', max(cap.max_request_starts),
        'request_window_seconds', max(extract(epoch from cap.request_window))::bigint
    )
      into v_cap
      from public.pilot_proactive_contact_caps cap
     where cap.tenant_key = v_tenant;

    -- Lo observado en la ventana, solo conteos y medianas.
    with instance_scopes as (
        select scope.scope_key, scope.version, scope.timezone
          from public.pilot_scope_versions scope
         where scope.chatwoot_inbox_id = v_inbox
           and scope.external_product_id = v_product
    ),
    cases as (
        select rc.id, rc.contact_id, rc.created_at,
               abandonment.event_type as trigger_event_type,
               abandonment.received_at as trigger_at,
               case abandonment.event_type
                    when 'PURCHASE_OUT_OF_SHOPPING_CART' then 'carrito'
                    when 'PURCHASE_CANCELED' then 'pago_fallido'
                    when 'PRECHECKOUT_FORM_SUBMITTED' then 'precheckout'
               end as flow_key
          from public.recovery_cases rc
          join public.pilot_recovery_case_bindings binding on binding.recovery_case_id = rc.id
          join instance_scopes scope
            on scope.scope_key = binding.scope_key and scope.version = binding.scope_version
          join public.webhook_events abandonment on abandonment.id = rc.abandonment_event_id
    ),
    hotmart as (
        select event.event_type, event.processing_status, event.processing_error, event.received_at
          from public.webhook_events event
         where event.received_at >= v_window_from and event.received_at < v_window_to
           and event.payload #>> '{data,product,id}' = v_product
    ),
    actions as (
        select action.id, action.anchor_type, action.status, action.terminal_reason, cases.flow_key,
               cases.trigger_at, cases.contact_id
          from public.scheduled_actions action
          join cases on cases.id = action.recovery_case_id
    ),
    accepted as (
        select actions.flow_key, attempt.accepted_at, actions.trigger_at
          from public.followup_delivery_attempts attempt
          join actions on actions.id = attempt.action_id
         where attempt.outcome = 'accepted_by_chatwoot'
           and attempt.accepted_at >= v_window_from and attempt.accepted_at < v_window_to
    ),
    authorized as (
        select actions.flow_key, count(*) as n
          from public.pilot_outbound_request_authorizations auth
          join actions on actions.id = auth.action_id
         where auth.authorized_at >= v_window_from and auth.authorized_at < v_window_to
         group by actions.flow_key
    ),
    deferred as (
        select actions.flow_key, count(distinct actions.id) as n
          from public.conversation_events event
          join actions on actions.id = event.related_action_id
         where event.event_type = 'followup_action_reevaluated'
           and event.data ->> 'decision' = 'defer'
           and event.created_at >= v_window_from and event.created_at < v_window_to
         group by actions.flow_key
    ),
    stopped as (
        select actions.flow_key,
               case when (event.data ->> 'reason_code') ~ '^[a-z][a-z0-9_:]{0,79}$'
                    then event.data ->> 'reason_code' else 'invalid_reason_code' end as reason_code,
               count(*) as n
          from public.conversation_events event
          join actions on actions.id = event.related_action_id
         where event.event_type = 'followup_action_reevaluated'
           and event.data ->> 'decision' in ('cancel', 'pause', 'escalate')
           and event.created_at >= v_window_from and event.created_at < v_window_to
         group by 1, 2
    ),
    rejected as (
        select case hotmart.event_type
                    when 'PURCHASE_OUT_OF_SHOPPING_CART' then 'carrito'
                    when 'PURCHASE_CANCELED' then 'pago_fallido'
               end as flow_key,
               case when hotmart.processing_error ~ '^[a-z][a-z0-9_:]{0,79}$'
                    then hotmart.processing_error else 'invalid_reason_code' end as reason_code,
               count(*) as n
          from hotmart
         where hotmart.processing_status = 'failed'
           and hotmart.event_type in ('PURCHASE_OUT_OF_SHOPPING_CART', 'PURCHASE_CANCELED')
         group by 1, 2
    ),
    plans as (
        select plan.outcome,
               case when plan.reason_code ~ '^[a-z][a-z0-9_:]{0,79}$'
                    then plan.reason_code else 'invalid_reason_code' end as reason_code,
               count(*) as n
          from public.portable_precheckout_first_contact_plans plan
          join instance_scopes scope
            on scope.scope_key = plan.scope_key and scope.version = plan.scope_version
         where plan.created_at >= v_window_from and plan.created_at < v_window_to
         group by 1, 2
    ),
    superseded as (
        select (
            select min(winner.trigger_at)
              from cases winner
             where winner.contact_id = actions.contact_id
               and winner.flow_key in ('carrito', 'pago_fallido')
               and winner.trigger_at >= actions.trigger_at
               and winner.trigger_at <= event.created_at
        ) - actions.trigger_at as form_to_winner
          from public.conversation_events event
          join actions on actions.id = event.related_action_id
         where actions.flow_key = 'precheckout'
           and event.event_type = 'followup_action_reevaluated'
           and event.data ->> 'reason_code' = 'superseded_by_provider_event'
           and event.created_at >= v_window_from and event.created_at < v_window_to
    ),
    coupon as (
        select followup.status, count(*) as n
          from public.conversation_followup_events followup
          join public.commercial_cases commercial on commercial.id = followup.commercial_case_id
         where commercial.inbound_scope_key = v_inbound_scope
           and followup.created_at >= v_window_from and followup.created_at < v_window_to
         group by followup.status
    ),
    reactivation as (
        select reactivation.status, count(*) as n
          from public.conversation_reactivation_events reactivation
          join public.commercial_cases commercial on commercial.id = reactivation.commercial_case_id
         where commercial.inbound_scope_key = v_inbound_scope
           and reactivation.created_at >= v_window_from and reactivation.created_at < v_window_to
         group by reactivation.status
    ),
    flow_keys(flow_key, trigger_type) as (
        values ('carrito', 'PURCHASE_OUT_OF_SHOPPING_CART'),
               ('pago_fallido', 'PURCHASE_CANCELED'),
               ('precheckout', 'PRECHECKOUT_FORM_SUBMITTED')
    )
    select jsonb_build_object(
        'window_from', v_window_from,
        'window_to', v_window_to,
        'window_days', 7,
        'flows', (
            select jsonb_object_agg(flow_keys.flow_key, jsonb_build_object(
                'triggers', case flow_keys.flow_key
                    when 'precheckout' then (select coalesce(sum(plans.n), 0) from plans)
                    else (select count(*) from hotmart where hotmart.event_type = flow_keys.trigger_type)
                end,
                'cases_opened', (
                    select count(*) from cases
                     where cases.flow_key = flow_keys.flow_key
                       and cases.created_at >= v_window_from and cases.created_at < v_window_to),
                'starts', coalesce((select authorized.n from authorized where authorized.flow_key = flow_keys.flow_key), 0),
                'accepted_by_chatwoot', (select count(*) from accepted where accepted.flow_key = flow_keys.flow_key),
                'deferred_by_hours', coalesce((select deferred.n from deferred where deferred.flow_key = flow_keys.flow_key), 0),
                'stopped', coalesce((
                    select jsonb_object_agg(stopped.reason_code, stopped.n)
                      from stopped where stopped.flow_key = flow_keys.flow_key), '{}'::jsonb),
                'rejected_on_entry', coalesce((
                    select jsonb_object_agg(rejected.reason_code, rejected.n)
                      from rejected where rejected.flow_key = flow_keys.flow_key), '{}'::jsonb),
                'median_trigger_to_accept_seconds', (
                    select round(percentile_cont(0.5) within group (
                        order by extract(epoch from accepted.accepted_at - accepted.trigger_at)))::bigint
                      from accepted where accepted.flow_key = flow_keys.flow_key),
                'first_contact_plans', case when flow_keys.flow_key = 'precheckout' then coalesce((
                    select jsonb_object_agg(plans.outcome || ':' || plans.reason_code, plans.n) from plans), '{}'::jsonb)
                    end,
                'superseded', case when flow_keys.flow_key = 'precheckout' then (select count(*) from superseded) end,
                'median_form_to_winning_event_seconds', case when flow_keys.flow_key = 'precheckout' then (
                    select round(percentile_cont(0.5) within group (
                        order by extract(epoch from superseded.form_to_winner)))::bigint
                      from superseded where superseded.form_to_winner is not null)
                    end
            ))
              from flow_keys
        ),
        'descuento', jsonb_build_object(
            'sent', coalesce((select coupon.n from coupon where coupon.status = 'sent'), 0),
            'failed', coalesce((select coupon.n from coupon where coupon.status = 'failed'), 0),
            'claimed', coalesce((select coupon.n from coupon where coupon.status = 'claimed'), 0)
        ),
        'reactivacion', jsonb_build_object(
            'sent', coalesce((select reactivation.n from reactivation where reactivation.status = 'sent'), 0),
            'failed', coalesce((select reactivation.n from reactivation where reactivation.status = 'failed'), 0),
            'claimed', coalesce((select reactivation.n from reactivation where reactivation.status = 'claimed'), 0)
        )
    )
      into v_observed;

    -- Los eventos de Hotmart del producto que llegan y no disparan nada.
    select coalesce(jsonb_agg(jsonb_build_object(
        'event_type', grouped.event_type,
        'processing_error', grouped.processing_error,
        'count_window', grouped.count_window,
        'last_received_at', grouped.last_received_at
    ) order by grouped.event_type), '[]'::jsonb)
      into v_without_flow
      from (
        select event.event_type,
               case when event.processing_error ~ '^[a-z][a-z0-9_:]{0,79}$'
                    then event.processing_error end as processing_error,
               count(*) filter (
                   where event.received_at >= v_window_from and event.received_at < v_window_to
               ) as count_window,
               max(event.received_at) as last_received_at
          from public.webhook_events event
         where event.processing_error = 'unsupported_persisted_event_type'
           and event.payload #>> '{data,product,id}' = v_product
           and event.event_type ~ '^[A-Z][A-Z0-9_]{0,79}$'
         group by 1, 2
      ) grouped;

    return jsonb_build_object(
        'database_at', now(),
        'local_date', v_today,
        'scopes', v_scopes,
        'policies', v_policies,
        'proactive_contact_cap', v_cap,
        'observed', v_observed,
        'events_without_flow', v_without_flow
    );
end;
$function$;

create or replace function public.find_automation_expert_lead_v1(
    p_instance jsonb,
    p_conversation bigint,
    p_issuance_ulid text,
    p_name text
)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_account bigint;
    v_inbox bigint;
    v_product text;
    v_inbound_scope text;
    v_keys integer;
    v_name text;
    v_pattern text;
    v_candidates uuid[];
    v_count integer;
    v_result jsonb;
begin
    if p_instance is null or jsonb_typeof(p_instance) <> 'object'
       or coalesce(p_instance ->> 'chatwoot_account_id', '') !~ '^[1-9][0-9]{0,17}$'
       or coalesce(p_instance ->> 'chatwoot_inbox_id', '') !~ '^[1-9][0-9]{0,17}$'
       or coalesce(p_instance ->> 'hotmart_product_id', '') !~ '^[1-9][0-9]{0,17}$'
       or coalesce(p_instance ->> 'inbound_scope_key', '') !~ '^[a-z0-9][a-z0-9-]{0,127}$' then
        raise exception using errcode = '22023', message = 'automation_expert_invalid_instance';
    end if;
    v_account := (p_instance ->> 'chatwoot_account_id')::bigint;
    v_inbox := (p_instance ->> 'chatwoot_inbox_id')::bigint;
    v_product := p_instance ->> 'hotmart_product_id';
    v_inbound_scope := p_instance ->> 'inbound_scope_key';

    v_keys := (p_conversation is not null)::integer
        + (p_issuance_ulid is not null)::integer
        + (p_name is not null)::integer;
    if v_keys <> 1
       or (p_conversation is not null and (p_conversation < 1 or p_conversation > 999999))
       or (p_issuance_ulid is not null and p_issuance_ulid !~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$') then
        raise exception using errcode = '22023', message = 'automation_expert_invalid_lookup';
    end if;

    if p_name is not null then
        -- La misma normalizacion que la busqueda: minusculas, sin tildes, el
        -- espacio duro como espacio y los espacios colapsados. Solo letras,
        -- espacios, apostrofos y guiones.
        v_name := btrim(regexp_replace(
            translate(lower(translate(p_name, chr(160), ' ')),
                      'áéíóúüñàèìòùâêîôûäëïöÿç', 'aeiouunaeiouaeiouaeioyc'),
            '\s+', ' ', 'g'));
        if char_length(v_name) < 3 or char_length(v_name) > 80
           or v_name !~ '^[[:alpha:]'' -]+$' then
            raise exception using errcode = '22023', message = 'automation_expert_invalid_lookup';
        end if;
        v_pattern := replace(replace(replace(v_name, '\', '\\'), '%', '\%'), '_', '\_');
    end if;

    with
    -- Los contactos atados a la instancia, por alguno de los caminos con marca.
    anchored_contacts as (
        select rc.contact_id
          from public.recovery_cases rc
          join public.pilot_recovery_case_bindings binding on binding.recovery_case_id = rc.id
          join public.pilot_scope_versions scope
            on scope.scope_key = binding.scope_key and scope.version = binding.scope_version
         where scope.chatwoot_inbox_id = v_inbox and scope.external_product_id = v_product
        union
        select commercial.contact_id from public.commercial_cases commercial
         where commercial.inbound_scope_key = v_inbound_scope
        union
        select issuance.contact_id from public.checkout_link_issuances issuance
         where issuance.chatwoot_inbox_id = v_inbox
        union
        select plan.contact_id
          from public.portable_precheckout_first_contact_plans plan
          join public.pilot_scope_versions scope
            on scope.scope_key = plan.scope_key and scope.version = plan.scope_version
         where plan.contact_id is not null
           and scope.chatwoot_inbox_id = v_inbox and scope.external_product_id = v_product
        union
        select conversation.contact_id
          from public.human_handoff_requests handoff
          join public.conversations conversation on conversation.id = handoff.conversation_id
         where handoff.chatwoot_inbox_id = v_inbox
        union
        select opt_out.contact_id from public.contact_opt_out_events opt_out
         where opt_out.canonical_inbox_id = v_inbox and opt_out.contact_id is not null
    ),
    -- Las conversaciones atadas a la instancia, con su contacto y su numero.
    -- Un numero de conversacion entra a la busqueda solo por aca.
    instance_conversations as (
        select issuance.contact_id, issuance.chatwoot_conversation_id as number,
               issuance.created_at as activity_at
          from public.checkout_link_issuances issuance
         where issuance.chatwoot_inbox_id = v_inbox
        union all
        select conversation.contact_id, handoff.external_conversation_id, handoff.created_at
          from public.human_handoff_requests handoff
          join public.conversations conversation on conversation.id = handoff.conversation_id
         where handoff.chatwoot_inbox_id = v_inbox
        union all
        select opt_out.contact_id, opt_out.canonical_conversation_id, opt_out.occurred_at
          from public.contact_opt_out_events opt_out
         where opt_out.canonical_inbox_id = v_inbox and opt_out.contact_id is not null
        union all
        select conversation.contact_id,
               case when identity.external_conversation_id ~ '^[1-9][0-9]{0,17}$'
                    then identity.external_conversation_id::bigint end,
               conversation.updated_at
          from public.conversations conversation
          join public.channel_identities identity on identity.id = conversation.channel_identity_id
         where identity.account_id = 'chatwoot:' || v_account::text
           and identity.external_conversation_id ~ '^[1-9][0-9]{0,17}$'
           and (
            exists (
                select 1
                  from public.recovery_cases rc
                  join public.pilot_recovery_case_bindings binding on binding.recovery_case_id = rc.id
                  join public.pilot_scope_versions scope
                    on scope.scope_key = binding.scope_key and scope.version = binding.scope_version
                 where rc.conversation_id = conversation.id
                   and scope.chatwoot_inbox_id = v_inbox
                   and scope.external_product_id = v_product)
            or exists (
                select 1 from public.commercial_cases commercial
                 where commercial.conversation_id = conversation.id
                   and commercial.inbound_scope_key = v_inbound_scope)
           )
    ),
    by_key as (
        select instance_conversations.contact_id
          from instance_conversations
         where p_conversation is not null
           and instance_conversations.number = p_conversation
        union
        select issuance.contact_id
          from public.checkout_link_issuances issuance
         where p_issuance_ulid is not null
           and issuance.issuance_ulid = p_issuance_ulid
           and issuance.chatwoot_inbox_id = v_inbox
        union
        -- Por nombre: igualdad, o un prefijo de tres letras o mas sobre la
        -- primera palabra, o palabras enteras desde el principio. Un prefijo
        -- nunca recorre el apellido.
        select normalized.id
          from (
            select contact.id,
                   btrim(regexp_replace(
                       translate(lower(translate(contact.full_name, chr(160), ' ')),
                                 'áéíóúüñàèìòùâêîôûäëïöÿç', 'aeiouunaeiouaeiouaeioyc'),
                       '\s+', ' ', 'g')) as full_name
              from public.contacts contact
             where v_name is not null
               and contact.full_name is not null
          ) normalized
         where normalized.full_name = v_name
            or (position(' ' in v_name) = 0
                and split_part(normalized.full_name, ' ', 1) like v_pattern || '%' escape '\')
            or (position(' ' in v_name) > 0
                and normalized.full_name like v_pattern || ' %' escape '\')
    ),
    candidates as (
        select by_key.contact_id
          from by_key
          join anchored_contacts on anchored_contacts.contact_id = by_key.contact_id
         where by_key.contact_id is not null
    )
    select array_agg(distinct candidates.contact_id), count(distinct candidates.contact_id)
      into v_candidates, v_count
      from candidates;

    if v_count = 0 then
        return jsonb_build_object('result', 'not_found', 'candidates', '[]'::jsonb);
    end if;
    if v_count > 5 then
        return jsonb_build_object('result', 'too_many_candidates', 'candidates', '[]'::jsonb);
    end if;

    with instance_conversations as (
        select issuance.contact_id, issuance.chatwoot_conversation_id as number,
               issuance.created_at as activity_at
          from public.checkout_link_issuances issuance
         where issuance.chatwoot_inbox_id = v_inbox
           and issuance.contact_id = any(v_candidates)
        union all
        select conversation.contact_id, handoff.external_conversation_id, handoff.created_at
          from public.human_handoff_requests handoff
          join public.conversations conversation on conversation.id = handoff.conversation_id
         where handoff.chatwoot_inbox_id = v_inbox
           and conversation.contact_id = any(v_candidates)
        union all
        select opt_out.contact_id, opt_out.canonical_conversation_id, opt_out.occurred_at
          from public.contact_opt_out_events opt_out
         where opt_out.canonical_inbox_id = v_inbox
           and opt_out.contact_id = any(v_candidates)
        union all
        select conversation.contact_id,
               case when identity.external_conversation_id ~ '^[1-9][0-9]{0,17}$'
                    then identity.external_conversation_id::bigint end,
               conversation.updated_at
          from public.conversations conversation
          join public.channel_identities identity on identity.id = conversation.channel_identity_id
         where conversation.contact_id = any(v_candidates)
           and identity.account_id = 'chatwoot:' || v_account::text
           and identity.external_conversation_id ~ '^[1-9][0-9]{0,17}$'
           and (
            exists (
                select 1
                  from public.recovery_cases rc
                  join public.pilot_recovery_case_bindings binding on binding.recovery_case_id = rc.id
                  join public.pilot_scope_versions scope
                    on scope.scope_key = binding.scope_key and scope.version = binding.scope_version
                 where rc.conversation_id = conversation.id
                   and scope.chatwoot_inbox_id = v_inbox
                   and scope.external_product_id = v_product)
            or exists (
                select 1 from public.commercial_cases commercial
                 where commercial.conversation_id = conversation.id
                   and commercial.inbound_scope_key = v_inbound_scope)
           )
    ),
    bound_cases as (
        select rc.contact_id, rc.updated_at
          from public.recovery_cases rc
          join public.pilot_recovery_case_bindings binding on binding.recovery_case_id = rc.id
          join public.pilot_scope_versions scope
            on scope.scope_key = binding.scope_key and scope.version = binding.scope_version
         where rc.contact_id = any(v_candidates)
           and scope.chatwoot_inbox_id = v_inbox
           and scope.external_product_id = v_product
    )
    select jsonb_agg(candidate order by candidate ->> 'last_activity_at' desc nulls last, candidate ->> 'lead_ref')
      into v_result
      from (
        select jsonb_build_object(
            'lead_ref', contact.id,
            'first_name_inferred', (
                select inference.first_name
                  from public.lead_first_name_inferences inference
                 where inference.result = 'confident'
                   and inference.name_key = encode(sha256(convert_to(lower(regexp_replace(
                        btrim(translate(contact.full_name, chr(160), ' ')), '\s+', ' ', 'g')), 'UTF8')), 'hex')
                 order by inference.created_at desc
                 limit 1),
            'first_name_fallback', nullif(regexp_replace(split_part(regexp_replace(
                btrim(translate(coalesce(contact.full_name, ''), chr(160), ' ')), '\s+', ' ', 'g'), ' ', 1),
                '^.*[0-9].*$', ''), ''),
            'conversations', coalesce((
                select jsonb_agg(distinct instance_conversations.number order by instance_conversations.number)
                  from instance_conversations
                 where instance_conversations.contact_id = contact.id
                   and instance_conversations.number is not null), '[]'::jsonb),
            'last_activity_at', greatest(
                (select max(instance_conversations.activity_at) from instance_conversations
                  where instance_conversations.contact_id = contact.id),
                (select max(bound_cases.updated_at) from bound_cases
                  where bound_cases.contact_id = contact.id)
            )
        ) as candidate
          from public.contacts contact
         where contact.id = any(v_candidates)
      ) rows;

    return jsonb_build_object(
        'result', case when v_count = 1 then 'found' else 'ambiguous' end,
        'candidates', v_result
    );
end;
$function$;

create or replace function public.read_automation_expert_lead_history_v1(
    p_instance jsonb,
    p_lead_ref uuid
)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_tenant text;
    v_funnel text;
    v_account bigint;
    v_inbox bigint;
    v_product text;
    v_inbound_scope text;
    v_agent_scope text;
    v_recovery_scope text;
    v_anchored boolean;
    v_result jsonb;
begin
    if p_instance is null or jsonb_typeof(p_instance) <> 'object'
       or coalesce(p_instance ->> 'tenant_ref', '') !~ '^[a-z0-9][a-z0-9-]{0,127}$'
       or coalesce(p_instance ->> 'funnel_ref', '') !~ '^[a-z0-9][a-z0-9-]{0,127}$'
       or coalesce(p_instance ->> 'chatwoot_account_id', '') !~ '^[1-9][0-9]{0,17}$'
       or coalesce(p_instance ->> 'chatwoot_inbox_id', '') !~ '^[1-9][0-9]{0,17}$'
       or coalesce(p_instance ->> 'hotmart_product_id', '') !~ '^[1-9][0-9]{0,17}$'
       or coalesce(p_instance ->> 'inbound_scope_key', '') !~ '^[a-z0-9][a-z0-9-]{0,127}$'
       or coalesce(p_instance ->> 'agent_scope_ref', '') !~ '^[a-z0-9][a-z0-9-]{0,127}$' then
        raise exception using errcode = '22023', message = 'automation_expert_invalid_instance';
    end if;
    if p_lead_ref is null then
        raise exception using errcode = '22023', message = 'automation_expert_invalid_lead_ref';
    end if;
    v_tenant := p_instance ->> 'tenant_ref';
    v_funnel := p_instance ->> 'funnel_ref';
    v_account := (p_instance ->> 'chatwoot_account_id')::bigint;
    v_inbox := (p_instance ->> 'chatwoot_inbox_id')::bigint;
    v_product := p_instance ->> 'hotmart_product_id';
    v_inbound_scope := p_instance ->> 'inbound_scope_key';
    v_agent_scope := p_instance ->> 'agent_scope_ref';
    -- El scope de la recuperacion que corre el bridge: con el se deduce en que
    -- estado estaba el control cuando un carrito se rechazo al entrar.
    select item.value ->> 'key'
      into v_recovery_scope
      from jsonb_array_elements(coalesce(p_instance -> 'scopes', '[]'::jsonb)) item
     where jsonb_typeof(item.value) = 'object'
       and item.value ->> 'flow_group' = 'recuperacion'
     limit 1;

    -- Un lead que no se ata a esta instancia es como uno que no existe.
    select (
        exists (
            select 1
              from public.recovery_cases rc
              join public.pilot_recovery_case_bindings binding on binding.recovery_case_id = rc.id
              join public.pilot_scope_versions scope
                on scope.scope_key = binding.scope_key and scope.version = binding.scope_version
             where rc.contact_id = p_lead_ref
               and scope.chatwoot_inbox_id = v_inbox
               and scope.external_product_id = v_product)
        or exists (
            select 1 from public.commercial_cases commercial
             where commercial.contact_id = p_lead_ref
               and commercial.inbound_scope_key = v_inbound_scope)
        or exists (
            select 1 from public.checkout_link_issuances issuance
             where issuance.contact_id = p_lead_ref
               and issuance.chatwoot_inbox_id = v_inbox)
        or exists (
            select 1
              from public.portable_precheckout_first_contact_plans plan
              join public.pilot_scope_versions scope
                on scope.scope_key = plan.scope_key and scope.version = plan.scope_version
             where plan.contact_id = p_lead_ref
               and scope.chatwoot_inbox_id = v_inbox
               and scope.external_product_id = v_product)
        or exists (
            select 1
              from public.human_handoff_requests handoff
              join public.conversations conversation on conversation.id = handoff.conversation_id
             where conversation.contact_id = p_lead_ref
               and handoff.chatwoot_inbox_id = v_inbox)
        or exists (
            select 1 from public.contact_opt_out_events opt_out
             where opt_out.contact_id = p_lead_ref
               and opt_out.canonical_inbox_id = v_inbox)
    ) into v_anchored;

    if not v_anchored or not exists (select 1 from public.contacts where id = p_lead_ref) then
        return null;
    end if;

    -- Una sola sentencia: una sola foto de la base. Cada fuente entra solo si
    -- se ata a la persona y a la instancia (contrato, «Como se ata cada fuente
    -- a la persona y a la instancia»). Los momentos salen con su codigo crudo;
    -- el motivo de la lista cerrada y su frase los pone el bridge.
    with
    scopes as (
        select scope.scope_key, scope.version, scope.timezone,
               scope.max_outbound_request_starts_per_day as per_day,
               scope.max_outbound_request_starts_total as total_cap
          from public.pilot_scope_versions scope
         where scope.chatwoot_inbox_id = v_inbox
           and scope.external_product_id = v_product
    ),
    cases as (
        select rc.id, rc.conversation_id, rc.status, rc.created_at, rc.closed_at,
               rc.policy_key, rc.policy_version, rc.hotmart_purchase_intent_id,
               binding.scope_key, binding.scope_version, binding.audience_mode,
               binding.audience_purchase_intent_id,
               scopes.timezone as scope_tz, scopes.per_day, scopes.total_cap,
               abandonment.received_at as trigger_at,
               case abandonment.event_type
                    when 'PURCHASE_OUT_OF_SHOPPING_CART' then 'carrito'
                    when 'PURCHASE_CANCELED' then 'pago_fallido'
                    when 'PRECHECKOUT_FORM_SUBMITTED' then 'precheckout'
               end as flow_key,
               jsonb_build_object(
                   'scope', binding.scope_key || ' v' || binding.scope_version,
                   'policy', rc.policy_key || ' v' || rc.policy_version
               ) as flow_version
          from public.recovery_cases rc
          join public.pilot_recovery_case_bindings binding on binding.recovery_case_id = rc.id
          join scopes on scopes.scope_key = binding.scope_key and scopes.version = binding.scope_version
          join public.webhook_events abandonment on abandonment.id = rc.abandonment_event_id
         where rc.contact_id = p_lead_ref
    ),
    actions as (
        select action.id, action.recovery_case_id, action.created_at, action.updated_at,
               action.status, action.due_at, action.expires_at, action.step_key,
               action.anchor_type, action.terminal_reason, action.policy_key, action.policy_version,
               cases.flow_key, cases.flow_version, cases.scope_key, cases.scope_version,
               cases.scope_tz, cases.per_day, cases.total_cap,
               cases.conversation_id as case_conversation_id,
               cases.trigger_at as case_trigger_at,
               coalesce(cases.hotmart_purchase_intent_id, cases.audience_purchase_intent_id) as intent_id
          from public.scheduled_actions action
          join cases on cases.id = action.recovery_case_id
    ),
    lead_conversations as (
        select conversation.id as conversation_id, conversation.human_takeover,
               (conversation.human_takeover
                or conversation.status = 'paused_human'
                or conversation.automation_status in ('paused', 'disabled', 'restricted')) as held,
               case when identity.account_id = 'chatwoot:' || v_account::text
                     and identity.external_conversation_id ~ '^[1-9][0-9]{0,17}$'
                    then identity.external_conversation_id::bigint end as number
          from public.conversations conversation
          join public.channel_identities identity on identity.id = conversation.channel_identity_id
         where conversation.contact_id = p_lead_ref
           and (
            exists (select 1 from cases where cases.conversation_id = conversation.id)
            or exists (
                select 1 from public.commercial_cases commercial
                 where commercial.conversation_id = conversation.id
                   and commercial.inbound_scope_key = v_inbound_scope)
            or exists (
                select 1 from public.human_handoff_requests handoff
                 where handoff.conversation_id = conversation.id
                   and handoff.chatwoot_inbox_id = v_inbox)
           )
    ),
    links as (
        select issuance.id, issuance.created_at, issuance.purchased_at, issuance.status,
               issuance.source_kind, issuance.offer_resolution, issuance.attribution_resolution,
               issuance.chatwoot_conversation_id, issuance.hotmart_webhook_event_id,
               issuance.purchase_intent_id,
               exists (
                   select 1 from public.conversation_followup_events followup
                    where followup.checkout_issuance_id = issuance.id
               ) as from_coupon
          from public.checkout_link_issuances issuance
         where issuance.contact_id = p_lead_ref
           and issuance.chatwoot_inbox_id = v_inbox
    ),
    handoffs as (
        select handoff.id, handoff.created_at, handoff.attended_at, handoff.status,
               handoff.primary_reason_code, handoff.detail_reason_code,
               handoff.external_conversation_id
          from public.human_handoff_requests handoff
          join public.conversations conversation on conversation.id = handoff.conversation_id
         where conversation.contact_id = p_lead_ref
           and handoff.chatwoot_inbox_id = v_inbox
    ),
    opt_outs as (
        select opt_out.id, opt_out.occurred_at, opt_out.normalized_rule_key,
               opt_out.canonical_conversation_id
          from public.contact_opt_out_events opt_out
         where opt_out.contact_id = p_lead_ref
           and opt_out.canonical_inbox_id = v_inbox
    ),
    lead_numbers as (
        select lead_conversations.number from lead_conversations where lead_conversations.number is not null
        union select links.chatwoot_conversation_id from links
        union select handoffs.external_conversation_id from handoffs
        union select opt_outs.canonical_conversation_id from opt_outs
    ),
    commercial as (
        select commercial.id, commercial.conversation_id, commercial.case_kind
          from public.commercial_cases commercial
         where commercial.contact_id = p_lead_ref
           and commercial.inbound_scope_key = v_inbound_scope
    ),
    coupons as (
        select followup.id, coalesce(followup.settled_at, followup.created_at) as created_at,
               followup.status, followup.regime,
               followup.inbound_age_seconds, followup.external_conversation_id,
               followup.checkout_issuance_id, followup.provider_message_id, followup.conversation_id
          from public.conversation_followup_events followup
          join commercial on commercial.id = followup.commercial_case_id
    ),
    reactivations as (
        select reactivation.id, coalesce(reactivation.settled_at, reactivation.created_at) as created_at,
               reactivation.status,
               reactivation.reason_code, reactivation.external_conversation_id,
               reactivation.provider_message_id
          from public.conversation_reactivation_events reactivation
          join commercial on commercial.id = reactivation.commercial_case_id
    ),
    turns as (
        select turn.id, turn.occurred_at, turn.outcome, turn.model_answered, turn.model_requested,
               turn.bridge_release, turn.release_id, turn.external_conversation_id
          from public.agent_turn_provenance turn
         where turn.tenant_ref = v_tenant
           and turn.scope_ref = v_agent_scope
           and turn.external_conversation_id in (select lead_numbers.number from lead_numbers)
    ),
    lead_points as (
        select lower(btrim(point.normalized_value)) as email, null::text as phone10
          from public.contact_points point
         where point.contact_id = p_lead_ref and point.type = 'email'
        union all
        select null::text, right(regexp_replace(point.normalized_value, '\D', '', 'g'), 10)
          from public.contact_points point
         where point.contact_id = p_lead_ref and point.type = 'phone'
           and length(regexp_replace(point.normalized_value, '\D', '', 'g')) >= 10
    ),
    identity_intents as (
        -- Un formulario sin contacto se ata por identidad, con la misma regla
        -- que los eventos de Hotmart: sus datos llevan a un solo contacto.
        select intent.id as intent_id
          from public.purchase_intents intent
         where intent.tenant_ref = v_tenant
           and intent.funnel_ref = v_funnel
           and (lower(btrim(intent.normalized_email)) in (
                    select lead_points.email from lead_points where lead_points.email is not null)
                or right(intent.normalized_phone, 10) in (
                    select lead_points.phone10 from lead_points where lead_points.phone10 is not null))
           and (
            select count(distinct point.contact_id)
              from public.contact_points point
             where (point.type = 'email' and intent.normalized_email is not null
                    and lower(btrim(point.normalized_value)) = lower(btrim(intent.normalized_email)))
                or (point.type = 'phone' and intent.normalized_phone is not null
                    and length(regexp_replace(point.normalized_value, '\D', '', 'g')) >= 10
                    and right(regexp_replace(point.normalized_value, '\D', '', 'g'), 10)
                        = right(intent.normalized_phone, 10))
           ) = 1
    ),
    intent_paths as (
        select plan.purchase_intent_id as intent_id, 'first_contact_plan' as path
          from public.portable_precheckout_first_contact_plans plan
          join scopes on scopes.scope_key = plan.scope_key and scopes.version = plan.scope_version
         where plan.contact_id = p_lead_ref
        union all select cases.audience_purchase_intent_id, 'case' from cases
        union all select cases.hotmart_purchase_intent_id, 'case' from cases
        union all select links.purchase_intent_id, 'link' from links
        union all select identity_intents.intent_id, 'identity' from identity_intents
    ),
    intents as (
        select intent.id, intent.submitted_at, intent.landing_ref, intent.offer_ref,
               intent.lifecycle_state, intent.whatsapp_contact_authorized,
               coalesce(min(intent_paths.path) filter (where intent_paths.path <> 'identity'),
                        'identity') as path
          from intent_paths
          join public.purchase_intents intent on intent.id = intent_paths.intent_id
         where intent.tenant_ref = v_tenant
           and intent.funnel_ref = v_funnel
         group by intent.id
    ),
    identity_candidates as (
        select event.id, event.event_type, event.received_at, event.processing_status,
               event.processing_error,
               lower(btrim(identity.normalized_email)) as email,
               case when length(regexp_replace(coalesce(identity.normalized_phone, ''), '\D', '', 'g')) >= 10
                    then right(regexp_replace(identity.normalized_phone, '\D', '', 'g'), 10)
               end as phone10
          from public.hotmart_purchase_intent_event_identities identity
          join public.webhook_events event on event.id = identity.webhook_event_id
         where event.event_type in ('PURCHASE_OUT_OF_SHOPPING_CART', 'PURCHASE_CANCELED', 'PURCHASE_APPROVED')
           and event.payload #>> '{data,product,id}' = v_product
           and not exists (
               select 1 from public.recovery_case_events link where link.webhook_event_id = event.id
           )
    ),
    identity_events as (
        -- Se ata cuando los datos que cruzan llevan a un solo contacto (decision
        -- de Dan del 2026-10-15). Si llevan a dos, el evento es un hueco.
        select candidate.*,
               (
                select count(distinct point.contact_id)
                  from public.contact_points point
                 where (point.type = 'email' and candidate.email is not null
                        and lower(btrim(point.normalized_value)) = candidate.email)
                    or (point.type = 'phone' and candidate.phone10 is not null
                        and length(regexp_replace(point.normalized_value, '\D', '', 'g')) >= 10
                        and right(regexp_replace(point.normalized_value, '\D', '', 'g'), 10) = candidate.phone10)
               ) as contacts_matched
          from identity_candidates candidate
         where candidate.email in (select lead_points.email from lead_points where lead_points.email is not null)
            or candidate.phone10 in (select lead_points.phone10 from lead_points where lead_points.phone10 is not null)
    ),
    attached_events as (
        select event.*
          from identity_events event
         where event.contacts_matched = 1
           and not (
               event.event_type = 'PURCHASE_APPROVED'
               and exists (select 1 from links where links.hotmart_webhook_event_id = event.id)
           )
    ),
    winners as (
        select cases.flow_key, cases.trigger_at
          from cases
         where cases.flow_key in ('carrito', 'pago_fallido')
        union all
        select case attached.event_type
                    when 'PURCHASE_OUT_OF_SHOPPING_CART' then 'carrito'
                    when 'PURCHASE_CANCELED' then 'pago_fallido' end,
               attached.received_at
          from attached_events attached
         where attached.event_type in ('PURCHASE_OUT_OF_SHOPPING_CART', 'PURCHASE_CANCELED')
    ),
    rejected_starts as (
        select action.id as action_id, action.flow_key, action.flow_version, action.scope_key,
               action.scope_version, action.scope_tz, action.per_day, action.total_cap,
               count(*) as attempts, min(attempt.created_at) as first_at, max(attempt.created_at) as last_at
          from public.followup_delivery_attempts attempt
          join actions action on action.id = attempt.action_id
         where attempt.phase = 'reserved'
           and attempt.outcome is null
           and not exists (
               select 1 from public.pilot_outbound_request_authorizations auth
                where auth.attempt_id = attempt.id
           )
         group by action.id, action.flow_key, action.flow_version, action.scope_key,
                  action.scope_version, action.scope_tz, action.per_day, action.total_cap
    ),
    rejected_deduced as (
        select rejected.*,
               (
                select event.data ->> 'to_version'
                  from public.pilot_control_events event
                 where event.scope_key = rejected.scope_key
                   and event.event_type = 'pilot_scope_version_activated'
                   and event.created_at <= rejected.first_at
                 order by event.created_at desc, event.id desc
                 limit 1
               ) as active_version,
               (
                select case when control.event_type = 'pilot_scope_version_activated' then 'inactive'
                            when (control.data ->> 'to_state') ~ '^[a-z][a-z_]{0,31}$'
                                 then control.data ->> 'to_state' end
                  from public.pilot_control_events control
                 where control.scope_key = rejected.scope_key
                   and control.event_type in ('pilot_runtime_state_changed', 'pilot_scope_version_activated')
                   and control.created_at <= rejected.first_at
                 order by control.created_at desc, control.id desc
                 limit 1
               ) as control_state,
               (
                select count(*)
                  from public.pilot_outbound_request_authorizations auth
                 where auth.scope_key = rejected.scope_key
                   and auth.authorized_at <= rejected.first_at
               ) as total_before,
               (
                select count(*)
                  from public.pilot_outbound_request_authorizations auth
                 where auth.scope_key = rejected.scope_key
                   and auth.local_budget_date = (rejected.first_at at time zone rejected.scope_tz)::date
                   and auth.authorized_at <= rejected.first_at
               ) as day_before
          from rejected_starts rejected
    ),
    moments(at, kind, flow_key, flow_version, reason_code, decision, certainty, attached_by, ev_table, ev_id, extra) as (
        -- 1. El formulario.
        select intents.submitted_at, 'form_received', 'precheckout', null::jsonb, null::text, null::text,
               case when intents.path = 'identity' then 'inferred' else 'stored' end,
               intents.path, 'purchase_intents', intents.id::text,
               jsonb_build_object(
                   'landing_ref', case when intents.landing_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'
                                            and intents.landing_ref !~ '[0-9]{7}' then intents.landing_ref end,
                   'offer_ref', case when intents.offer_ref ~ '^[A-Za-z0-9]{4,32}$'
                                          and intents.offer_ref !~ '[0-9]{7}' then intents.offer_ref end,
                   'whatsapp_contact_authorized', intents.whatsapp_contact_authorized,
                   'lifecycle_state', case when intents.lifecycle_state ~ '^[a-z][a-z0-9_]{0,63}$' then intents.lifecycle_state end)
          from intents
        -- 2. El plan del primer contacto.
        union all
        select plan.created_at,
               case when plan.outcome = 'planned' then 'first_contact_planned' else 'first_contact_not_planned' end,
               'precheckout',
               jsonb_build_object(
                   'scope', plan.scope_key || ' v' || plan.scope_version,
                   'policy', scope.policy_key || ' v' || scope.policy_version),
               plan.reason_code, null,
               case when plan.contact_id = p_lead_ref then 'stored' else 'inferred' end,
               case when plan.contact_id = p_lead_ref then 'first_contact_plan' else 'identity' end,
               'portable_precheckout_first_contact_plans', plan.submission_id::text,
               jsonb_build_object('winner_flow_key', winner.flow_key, 'winner_at', winner.trigger_at)
          from public.portable_precheckout_first_contact_plans plan
          join public.pilot_scope_versions scope
            on scope.scope_key = plan.scope_key and scope.version = plan.scope_version
          left join lateral (
              select winners.flow_key, winners.trigger_at
                from winners
               where plan.reason_code = 'superseded_by_provider_event'
                 and winners.trigger_at <= plan.created_at
               order by winners.trigger_at desc
               limit 1
          ) winner on true
         where (plan.contact_id = p_lead_ref
                or (plan.contact_id is null
                    and plan.purchase_intent_id in (
                        select intents.id from intents where intents.path = 'identity')))
           and scope.chatwoot_inbox_id = v_inbox
           and scope.external_product_id = v_product
        -- 3. Los eventos de Hotmart que abrieron o se sumaron a un caso.
        union all
        select event.received_at, 'hotmart_event', cases.flow_key, cases.flow_version, null, null,
               'stored', 'case', 'webhook_events', event.id::text,
               jsonb_build_object(
                   'event_type', case when event.event_type ~ '^[A-Z][A-Z0-9_]{0,79}$' then event.event_type end,
                   'event_role', case when link.event_role ~ '^[a-z][a-z0-9_]{0,79}$' then link.event_role end,
                   'processing_status', case when event.processing_status ~ '^[a-z][a-z0-9_]{0,79}$' then event.processing_status end,
                   'correlation_outcome', case when correlation.outcome ~ '^[a-z][a-z0-9_]{0,79}$' then correlation.outcome end,
                   'matched_by', case when correlation.matched_by ~ '^[a-z][a-z0-9_]{0,79}$' then correlation.matched_by end)
          from public.recovery_case_events link
          join cases on cases.id = link.recovery_case_id
          join public.webhook_events event on event.id = link.webhook_event_id
          left join public.hotmart_purchase_intent_correlations correlation
            on correlation.webhook_event_id = event.id
         where event.event_type <> 'PRECHECKOUT_FORM_SUBMITTED'
        -- 3 y 4. Los eventos de Hotmart sin caso, atados solo por identidad.
        union all
        select event.received_at,
               case when event.event_type = 'PURCHASE_APPROVED' then 'purchased'
                    when event.processing_error like 'pilot\_scope\_rejected:%' escape '\' then 'rejected_on_entry'
                    else 'hotmart_event' end,
               case event.event_type
                    when 'PURCHASE_OUT_OF_SHOPPING_CART' then 'carrito'
                    when 'PURCHASE_CANCELED' then 'pago_fallido' end,
               null,
               case when event.event_type = 'PURCHASE_APPROVED' then null
                    when event.processing_error like 'pilot\_scope\_rejected:%' escape '\'
                         then substr(event.processing_error, 22)
                    else event.processing_error end,
               null, 'inferred', 'identity', 'webhook_events', event.id::text,
               jsonb_build_object(
                   'event_type', event.event_type,
                   'processing_status', case when event.processing_status ~ '^[a-z][a-z0-9_]{0,79}$' then event.processing_status end,
                   'link_source', null,
                   'control_state', case
                       when event.processing_error = 'pilot_scope_rejected:pilot_runtime_not_armed' then
                       (
                select case when control.event_type = 'pilot_scope_version_activated' then 'inactive'
                            when (control.data ->> 'to_state') ~ '^[a-z][a-z_]{0,31}$'
                                 then control.data ->> 'to_state' end
                  from public.pilot_control_events control
                 where control.scope_key = v_recovery_scope
                   and control.event_type in ('pilot_runtime_state_changed', 'pilot_scope_version_activated')
                   and control.created_at <= event.received_at
                 order by control.created_at desc, control.id desc
                 limit 1
               ) end)
          from attached_events event
        -- 5. El caso.
        union all
        select cases.created_at, 'entered_flow', cases.flow_key, cases.flow_version, null, null,
               'stored', 'case', 'recovery_cases', cases.id::text,
               jsonb_build_object(
                   'audience_mode', case when cases.audience_mode ~ '^[a-z][a-z0-9_]{0,63}$' then cases.audience_mode end)
          from cases
        -- 6. El evento que se sumo a un caso abierto.
        union all
        select event.created_at, 'merged_into_open_case', cases.flow_key, cases.flow_version,
               event.data ->> 'reason_code', null, 'stored', 'case', 'conversation_events', event.id::text, '{}'::jsonb
          from public.conversation_events event
          join cases on cases.id = event.recovery_case_id
         where event.event_type like '%\_aggregated' escape '\'
        -- 7. El paso programado.
        union all
        select action.created_at, 'scheduled', action.flow_key, action.flow_version, null, null,
               'stored', 'case', 'scheduled_actions', action.id::text,
               jsonb_build_object(
                   'step_key', case when action.step_key ~ '^[a-z][a-z0-9_]{0,79}$' then action.step_key end,
                   'anchor_type', case when action.anchor_type ~ '^[a-z][a-z0-9_]{0,79}$' then action.anchor_type end,
                   'due_at', action.due_at,
                   'expires_at', action.expires_at,
                   'status', action.status)
          from actions action
        -- 8. Las esperas por el horario, una por paso.
        union all
        select min(event.created_at), 'deferred_by_hours', action.flow_key, action.flow_version,
               min(event.data ->> 'reason_code'), 'defer', 'stored', 'case', 'scheduled_actions', action.id::text,
               jsonb_build_object('count', count(*), 'first_at', min(event.created_at), 'last_at', max(event.created_at))
          from public.conversation_events event
          join actions action on action.id = event.related_action_id
         where event.event_type = 'followup_action_reevaluated'
           and event.data ->> 'decision' = 'defer'
         group by action.id, action.flow_key, action.flow_version
        -- 9. Los frenos de la reevaluacion.
        union all
        select event.created_at, 'stopped', action.flow_key, action.flow_version,
               event.data ->> 'reason_code', event.data ->> 'decision', 'stored', 'case',
               'conversation_events', event.id::text,
               jsonb_build_object('winner_flow_key', winner.flow_key, 'winner_at', winner.trigger_at)
          from public.conversation_events event
          join actions action on action.id = event.related_action_id
          left join lateral (
              select winners.flow_key, winners.trigger_at
                from winners
               where event.data ->> 'reason_code' = 'superseded_by_provider_event'
                 and winners.trigger_at >= action.case_trigger_at
                 and winners.trigger_at <= event.created_at
               order by winners.trigger_at
               limit 1
          ) winner on true
         where event.event_type = 'followup_action_reevaluated'
           and event.data ->> 'decision' in ('cancel', 'pause', 'escalate')
        -- 9. Los pasos terminados sin reevaluacion (por ejemplo, una baja).
        union all
        select action.updated_at, 'stopped', action.flow_key, action.flow_version,
               action.terminal_reason, 'cancel', 'stored', 'case', 'scheduled_actions', action.id::text, '{}'::jsonb
          from actions action
         where action.status in ('cancelled', 'skipped', 'superseded', 'permanent_failed')
           and not exists (
               select 1 from public.conversation_events event
                where event.related_action_id = action.id
                  and event.event_type = 'followup_action_reevaluated'
                  and event.data ->> 'decision' in ('cancel', 'pause', 'escalate'))
        union all
        select event.created_at, 'expired', action.flow_key, action.flow_version,
               event.data ->> 'reason_code', null, 'stored', 'case', 'conversation_events', event.id::text, '{}'::jsonb
          from public.conversation_events event
          join actions action on action.id = event.related_action_id
         where event.event_type = 'followup_action_reevaluated'
           and event.data ->> 'decision' = 'expire'
        union all
        select action.updated_at, 'expired', action.flow_key, action.flow_version,
               action.terminal_reason, null, 'stored', 'case', 'scheduled_actions', action.id::text, '{}'::jsonb
          from actions action
         where action.status = 'expired'
           and not exists (
               select 1 from public.conversation_events event
                where event.related_action_id = action.id
                  and (event.event_type = 'followup_action_expired'
                       or (event.event_type = 'followup_action_reevaluated'
                           and event.data ->> 'decision' = 'expire')))
        -- 25. Los vencimientos.
        union all
        select event.created_at, 'expired', action.flow_key, action.flow_version,
               event.data ->> 'reason_code', null, 'stored', 'case', 'conversation_events', event.id::text, '{}'::jsonb
          from public.conversation_events event
          join actions action on action.id = event.related_action_id
         where event.event_type = 'followup_action_expired'
        -- 10. Los arranques autorizados.
        union all
        select auth.authorized_at, 'start_authorized', action.flow_key, action.flow_version,
               auth.reason_code, null, 'stored', 'case', 'pilot_outbound_request_authorizations', auth.id::text,
               jsonb_build_object('local_budget_date', auth.local_budget_date)
          from public.pilot_outbound_request_authorizations auth
          join actions action on action.id = auth.action_id
        -- 11. Los arranques rechazados: un bucle, un momento, con el motivo deducido.
        union all
        select rejected.first_at, 'start_rejected', rejected.flow_key, rejected.flow_version,
               case when rejected.active_version is not null
                         and rejected.active_version <> rejected.scope_version::text
                         then 'pilot_scope_version_mismatch'
                    when rejected.control_state is not null and rejected.control_state <> 'armed'
                         then 'pilot_runtime_not_armed'
                    when rejected.total_before >= rejected.total_cap then 'pilot_total_budget_exhausted'
                    when rejected.day_before >= rejected.per_day then 'pilot_daily_budget_exhausted'
               end,
               null, 'inferred', 'case', 'scheduled_actions', rejected.action_id::text,
               jsonb_build_object(
                   'count', rejected.attempts,
                   'first_at', rejected.first_at,
                   'last_at', rejected.last_at,
                   'control_state', case when rejected.control_state ~ '^[a-z][a-z_]{0,31}$' then rejected.control_state end)
          from rejected_deduced rejected
        -- 12. La plantilla que acepto Chatwoot, con el id de su mensaje para la entrega de Meta.
        union all
        select attempt.accepted_at, 'accepted_by_chatwoot', action.flow_key, action.flow_version,
               attempt.outcome, null, 'stored', 'case', 'followup_delivery_attempts', attempt.id::text,
               jsonb_build_object(
                   'chatwoot_message_id', case when message.external_message_id ~ '^[1-9][0-9]{0,17}$'
                                               then message.external_message_id::bigint end)
          from public.followup_delivery_attempts attempt
          join actions action on action.id = attempt.action_id
          left join public.messages message on message.id = attempt.accepted_message_id
         where attempt.outcome = 'accepted_by_chatwoot'
           and attempt.accepted_at is not null
        -- 13. El envio incierto o fallido.
        union all
        select coalesce(attempt.reconciled_at, attempt.updated_at), 'delivery_uncertain_or_failed',
               action.flow_key, action.flow_version,
               case when attempt.outcome is null then 'delivery_unknown' else attempt.outcome end, null,
               'stored', 'case', 'followup_delivery_attempts', attempt.id::text,
               jsonb_build_object(
                   'outcome', case when attempt.outcome ~ '^[a-z][a-z0-9_]{0,63}$' then attempt.outcome end,
                   'detail_reason_code', attempt.reason_code)
          from public.followup_delivery_attempts attempt
          join actions action on action.id = attempt.action_id
         where (attempt.outcome is not null and attempt.outcome <> 'accepted_by_chatwoot')
            or (attempt.phase = 'request_started' and attempt.outcome is null
                and attempt.request_started_at < now() - interval '15 minutes')
        -- 15. La respuesta a una plantilla, adoptada por el agente.
        union all
        select event.created_at, 'replied_and_adopted', cases.flow_key, cases.flow_version, null, null,
               'stored', 'case', 'conversation_events', event.id::text,
               jsonb_build_object(
                   'conversation', case when (event.data ->> 'chatwoot_conversation_id') ~ '^[1-9][0-9]{0,17}$'
                                        then (event.data ->> 'chatwoot_conversation_id')::bigint end)
          from public.conversation_events event
          join cases on cases.id = event.recovery_case_id
         where event.event_type = 'inbound_adopted_template_conversation'
        -- 16. Los turnos del agente, sin su contenido.
        union all
        select turns.occurred_at, 'agent_replied', null, null, null, null,
               'stored', 'conversation', 'agent_turn_provenance', turns.id::text,
               jsonb_build_object(
                   'outcome', case when turns.outcome ~ '^[a-z][a-z0-9_]{0,31}$' then turns.outcome end,
                   'model', case when coalesce(turns.model_answered, turns.model_requested) ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$'
                                      and coalesce(turns.model_answered, turns.model_requested) !~ '[0-9]{7}'
                                 then coalesce(turns.model_answered, turns.model_requested) end,
                   'bridge_release', case when turns.bridge_release ~ '^[0-9a-f]{7,40}$' then turns.bridge_release end,
                   'release_recorded', turns.release_id is not null,
                   'conversation', turns.external_conversation_id)
          from turns
        -- 18. Los links de pago, sin su URL.
        union all
        select links.created_at, 'link_issued', case when links.from_coupon then 'descuento' end, null, null, null,
               'stored', 'link', 'checkout_link_issuances', links.id::text,
               jsonb_build_object(
                   'source_kind', case when links.source_kind ~ '^[a-z][a-z0-9_]{0,63}$' then links.source_kind end,
                   'offer_resolution', case when links.offer_resolution ~ '^[a-z][a-z0-9_]{0,63}$' then links.offer_resolution end,
                   'attribution_resolution', case when links.attribution_resolution ~ '^[a-z][a-z0-9_]{0,63}$' then links.attribution_resolution end,
                   'status', case when links.status ~ '^[a-z][a-z0-9_]{0,63}$' then links.status end,
                   'conversation', links.chatwoot_conversation_id)
          from links
        -- 19. El cupon, sin su codigo.
        union all
        select coupons.created_at,
               case when coupons.status = 'sent' then 'coupon_sent' else 'delivery_uncertain_or_failed' end,
               'descuento', null,
               case coupons.status when 'sent' then coupons.regime
                                   when 'failed' then 'coupon_send_failed'
                                   else 'delivery_unknown' end,
               null, 'stored', 'conversation',
               'conversation_followup_events', coupons.id::text,
               jsonb_build_object(
                   'status', coupons.status,
                   'inbound_age_seconds', coupons.inbound_age_seconds,
                   'conversation', coupons.external_conversation_id,
                   'with_checkout_link', coupons.checkout_issuance_id is not null,
                   'chatwoot_message_id', coupons.provider_message_id)
          from coupons
        -- 21. La reactivacion.
        union all
        select reactivations.created_at,
               case when reactivations.status = 'sent' then 'reactivation' else 'delivery_uncertain_or_failed' end,
               'reactivacion', null,
               case reactivations.status when 'sent' then reactivations.reason_code
                                         when 'failed' then 'reactivation_send_failed'
                                         else 'delivery_unknown' end,
               null, 'stored', 'conversation',
               'conversation_reactivation_events', reactivations.id::text,
               jsonb_build_object(
                   'status', reactivations.status,
                   'conversation', reactivations.external_conversation_id,
                   'chatwoot_message_id', reactivations.provider_message_id)
          from reactivations
        -- 22. La derivacion, sin su nota privada.
        union all
        select handoffs.created_at, 'handed_off', null, null, handoffs.primary_reason_code, null,
               'stored', 'conversation', 'human_handoff_requests', handoffs.id::text,
               jsonb_build_object(
                   'detail_reason_code', case when handoffs.detail_reason_code ~ '^[a-z][a-z0-9_:]{0,79}$' then handoffs.detail_reason_code end,
                   'status', case when handoffs.status ~ '^[a-z][a-z0-9_]{0,31}$' then handoffs.status end,
                   'conversation', handoffs.external_conversation_id)
          from handoffs
        union all
        select handoffs.attended_at, 'handoff_attended', null, null, null, null,
               'stored', 'conversation', 'human_handoff_requests', handoffs.id::text,
               jsonb_build_object('conversation', handoffs.external_conversation_id)
          from handoffs
         where handoffs.attended_at is not null
        union all
        select resume.created_at, 'resumed', null, null, resume.reason_code, null,
               'stored', 'conversation', 'conversation_resume_events', resume.id::text,
               jsonb_build_object('conversation', resume.external_conversation_id)
          from public.conversation_resume_events resume
          join commercial on commercial.id = resume.commercial_case_id
        -- 23. La baja.
        union all
        select opt_outs.occurred_at, 'opted_out', null, null, opt_outs.normalized_rule_key, null,
               'stored', 'opt_out', 'contact_opt_out_events', opt_outs.id::text,
               jsonb_build_object('conversation', opt_outs.canonical_conversation_id)
          from opt_outs
        -- 24. La compra con un link nuestro.
        union all
        select links.purchased_at, 'purchased', case when links.from_coupon then 'descuento' end, null, null, null,
               'stored', 'link', 'checkout_link_issuances', links.id::text,
               jsonb_build_object('link_source', case when links.from_coupon then 'coupon' else 'agent' end)
          from links
         where links.purchased_at is not null
        -- 25. El caso cerrado.
        union all
        select cases.closed_at, 'closed', cases.flow_key, cases.flow_version, null, null,
               'stored', 'case', 'recovery_cases', cases.id::text,
               jsonb_build_object('case_status', case when cases.status ~ '^[a-z][a-z0-9_]{0,31}$' then cases.status end)
          from cases
         where cases.closed_at is not null
    ),
    live as (
        select action.id, action.due_at, action.expires_at, action.step_key, action.flow_key,
               action.flow_version, action.status, action.policy_key, action.policy_version,
               action.case_conversation_id, action.case_trigger_at,
               case when action.flow_key = 'precheckout' and action.intent_id is not null
                    then public._portable_precheckout_stop_reason(action.intent_id, p_lead_ref)
               end as precheckout_stop,
               (
                select control.data ->> 'to_version'
                  from public.pilot_control_events control
                 where control.scope_key = action.scope_key
                   and control.event_type = 'pilot_scope_version_activated'
                 order by control.created_at desc, control.id desc
                 limit 1
               ) as active_version,
               (
                select case when control.event_type = 'pilot_scope_version_activated' then 'inactive'
                            when (control.data ->> 'to_state') ~ '^[a-z][a-z_]{0,31}$'
                                 then control.data ->> 'to_state' end
                  from public.pilot_control_events control
                 where control.scope_key = action.scope_key
                   and control.event_type in ('pilot_runtime_state_changed', 'pilot_scope_version_activated')
                   and control.created_at <= now()
                 order by control.created_at desc, control.id desc
                 limit 1
               ) as control_state,
               (
                select count(*) from public.pilot_outbound_request_authorizations auth
                 where auth.scope_key = action.scope_key
               ) as total_now,
               (
                select count(*) from public.pilot_outbound_request_authorizations auth
                 where auth.scope_key = action.scope_key
                   and auth.local_budget_date = (now() at time zone action.scope_tz)::date
               ) as day_now,
               action.scope_version, action.per_day, action.total_cap,
               exists (
                   select 1
                     from public.followup_policy_versions policy
                     cross join lateral jsonb_array_elements(policy.business_windows) business_window
                    where policy.policy_key = action.policy_key
                      and policy.version = action.policy_version
                      and extract(isodow from (greatest(action.due_at, now()) at time zone policy.timezone))::integer
                          in (select day.value::integer
                                from jsonb_array_elements_text(business_window.value -> 'days') day)
                      and to_char(greatest(action.due_at, now()) at time zone policy.timezone, 'HH24:MI')
                          between (business_window.value ->> 'start')
                              and (business_window.value ->> 'end')
               ) as inside_window
          from actions action
         where action.status in ('pending', 'deferred', 'retryable_failed', 'delivery_unknown')
    ),
    now_state as (
        select
            exists (select 1 from links where links.purchased_at is not null)
                or exists (select 1 from attached_events where attached_events.event_type = 'PURCHASE_APPROVED')
                or exists (select 1 from intents where intents.lifecycle_state = 'purchased') as purchased,
            exists (select 1 from opt_outs)
                or exists (
                    select 1 from public.contacts contact
                     where contact.id = p_lead_ref
                       and (contact.contact_permission = 'opted_out' or contact.lifecycle_status = 'do_not_contact')
                ) as opted_out,
            exists (
                select 1 from handoffs
                 where handoffs.attended_at is null
                   and handoffs.status in ('requested', 'projected', 'projection_failed')
            ) as pending_handoff,
            exists (select 1 from lead_conversations where lead_conversations.human_takeover) as human_takeover
    )
    select jsonb_build_object(
        'database_at', now(),
        'moment_count', (select count(*) from moments where moments.at is not null),
        'lead', (
            select jsonb_build_object(
                'lead_ref', contact.id,
                'first_name_inferred', (
                    select inference.first_name
                      from public.lead_first_name_inferences inference
                     where inference.result = 'confident'
                       and inference.name_key = encode(sha256(convert_to(lower(regexp_replace(
                            btrim(translate(contact.full_name, chr(160), ' ')), '\s+', ' ', 'g')), 'UTF8')), 'hex')
                     order by inference.created_at desc
                     limit 1),
                'first_name_fallback', nullif(regexp_replace(split_part(regexp_replace(
                    btrim(translate(coalesce(contact.full_name, ''), chr(160), ' ')), '\s+', ' ', 'g'), ' ', 1),
                    '^.*[0-9].*$', ''), ''),
                'conversations', coalesce((
                    select jsonb_agg(lead_numbers.number order by lead_numbers.number)
                      from lead_numbers where lead_numbers.number is not null), '[]'::jsonb),
                'identity', jsonb_build_object(
                    'has_phone', exists (
                        select 1 from public.contact_points point
                         where point.contact_id = contact.id and point.type = 'phone'),
                    'has_email', exists (
                        select 1 from public.contact_points point
                         where point.contact_id = contact.id and point.type = 'email'),
                    'whatsapp_authorized', coalesce((
                        select authorization_row.authorization_status = 'allowed'
                               and (authorization_row.valid_until is null or authorization_row.valid_until > now())
                          from public.contact_authorizations authorization_row
                         where authorization_row.contact_id = contact.id
                           and authorization_row.channel = 'whatsapp'
                         order by authorization_row.recorded_at desc, authorization_row.id desc
                         limit 1), false),
                    'opted_out', (select now_state.opted_out from now_state))
            )
              from public.contacts contact
             where contact.id = p_lead_ref
        ),
        'moments', coalesce((
            select jsonb_agg(jsonb_build_object(
                'at', moments.at,
                'kind', moments.kind,
                'flow_key', moments.flow_key,
                'flow_version', moments.flow_version,
                'reason_code', case when moments.reason_code ~ '^[a-z][a-z0-9_:]{0,79}$'
                                         and moments.reason_code !~ '[0-9]{7}'
                                    then moments.reason_code end,
                'reason_code_rejected', moments.reason_code is not null
                                        and (moments.reason_code !~ '^[a-z][a-z0-9_:]{0,79}$'
                                             or moments.reason_code ~ '[0-9]{7}'),
                'decision', case when moments.decision in ('cancel', 'pause', 'escalate', 'defer') then moments.decision end,
                'certainty', moments.certainty,
                'attached_by', moments.attached_by,
                'evidence', jsonb_build_object('table', moments.ev_table, 'id', moments.ev_id),
                'extra', moments.extra
            ) order by moments.at, moments.kind, moments.ev_id)
              from moments
             where moments.at is not null), '[]'::jsonb),
        'upcoming', coalesce((
            select jsonb_agg(jsonb_build_object(
                'action_ref', live.id,
                'flow_key', live.flow_key,
                'flow_version', live.flow_version,
                'step_key', case when live.step_key ~ '^[a-z][a-z0-9_]{0,79}$' then live.step_key end,
                'status', live.status,
                'due_at', live.due_at,
                'expires_at', live.expires_at,
                'expected', case
                    when live.expires_at is not null and now() >= live.expires_at
                        then jsonb_build_object('decision', 'expire', 'reason_code', 'expired')
                    when live.precheckout_stop is not null
                        then jsonb_build_object('decision', 'cancel', 'reason_code',
                            case when live.precheckout_stop ~ '^[a-z][a-z0-9_:]{0,79}$' then live.precheckout_stop end,
                            'winner_flow_key', (
                                select winners.flow_key from winners
                                 where live.precheckout_stop = 'superseded_by_provider_event'
                                   and winners.trigger_at >= live.case_trigger_at
                                 order by winners.trigger_at
                                 limit 1))
                    when (select now_state.opted_out from now_state)
                        then jsonb_build_object('decision', 'cancel', 'reason_code', 'contact_opted_out')
                    when (select now_state.purchased from now_state)
                        then jsonb_build_object('decision', 'cancel', 'reason_code', 'intent_purchased')
                    when live.flow_key <> 'precheckout' and exists (
                        select 1 from lead_conversations
                         where lead_conversations.conversation_id = live.case_conversation_id
                           and lead_conversations.held)
                        then jsonb_build_object('decision', 'pause', 'reason_code', 'human_takeover_active')
                    when live.active_version is not null
                         and live.active_version <> live.scope_version::text
                        then jsonb_build_object('decision', 'expire', 'reason_code', 'pilot_scope_version_mismatch')
                    when live.control_state is not null and live.control_state <> 'armed'
                        then jsonb_build_object('decision', 'defer', 'reason_code', 'pilot_runtime_not_armed',
                                                'control_state', live.control_state)
                    when live.total_now >= live.total_cap
                        then jsonb_build_object('decision', 'expire', 'reason_code', 'pilot_total_budget_exhausted')
                    when live.day_now >= live.per_day
                        then jsonb_build_object('decision', 'defer', 'reason_code', 'pilot_daily_budget_exhausted')
                    when not live.inside_window
                        then jsonb_build_object('decision', 'defer', 'reason_code', 'business_window_closed')
                    else jsonb_build_object('decision', 'execute', 'reason_code', 'eligible_for_execution')
                end
            ) order by live.due_at, live.id)
              from live), '[]'::jsonb),
        'now', (
            select jsonb_build_object(
                'purchased', now_state.purchased,
                'opted_out', now_state.opted_out,
                'pending_handoff', now_state.pending_handoff,
                'human_takeover', now_state.human_takeover,
                'live_actions', (select count(*) from live))
              from now_state
        ),
        'adopted_conversations', coalesce((
            select jsonb_agg(jsonb_build_object(
                'conversation', lead_conversations.number,
                'coupon_sent', exists (
                    select 1 from coupons
                     where coupons.conversation_id = lead_conversations.conversation_id
                       and coupons.status in ('sent', 'claimed')),
                'last_agent_turn_at', (
                    select max(turns.occurred_at) from turns
                     where turns.external_conversation_id = lead_conversations.number)
            ) order by lead_conversations.number)
              from lead_conversations
             where lead_conversations.number is not null
               and exists (
                   select 1 from commercial
                    where commercial.conversation_id = lead_conversations.conversation_id
                      and commercial.case_kind = 'inbound_sales')), '[]'::jsonb),
        'gap_counts', jsonb_build_object(
            'unattached_hotmart_event', (
                select count(*) from identity_events where identity_events.contacts_matched > 1),
            'case_creation_failure_not_persisted', (
                select count(*) from attached_events
                 where attached_events.processing_error = 'create_recovery_case_failed'),
            'purchase_channel_not_persisted', (
                select count(*) from attached_events where attached_events.event_type = 'PURCHASE_APPROVED'),
            'agent_release_not_persisted', (
                select count(*) from turns where turns.release_id is null),
            'start_rejection_not_persisted', (
                select count(*) from rejected_deduced rejected
                 where not (
                    (rejected.active_version is not null and rejected.active_version <> rejected.scope_version::text)
                    or (rejected.control_state is not null and rejected.control_state <> 'armed')
                    or rejected.total_before >= rejected.total_cap
                    or rejected.day_before >= rejected.per_day)))
    )
      into v_result;

    if (v_result ->> 'moment_count')::integer > 2000 then
        raise exception using errcode = '54000', message = 'automation_expert_response_too_large';
    end if;

    return v_result;
end;
$function$;

-- Solo service_role ejecuta las tres. El REVOKE de public tiene que ser
-- explicito: los privilegios por defecto le dan EXECUTE a anon y a
-- authenticated sobre toda funcion nueva.
revoke execute on function public.read_automation_expert_flows_v1(jsonb) from public;
revoke execute on function public.find_automation_expert_lead_v1(jsonb, bigint, text, text) from public;
revoke execute on function public.read_automation_expert_lead_history_v1(jsonb, uuid) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke execute on function public.read_automation_expert_flows_v1(jsonb) from anon;
        revoke execute on function public.find_automation_expert_lead_v1(jsonb, bigint, text, text) from anon;
        revoke execute on function public.read_automation_expert_lead_history_v1(jsonb, uuid) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke execute on function public.read_automation_expert_flows_v1(jsonb) from authenticated;
        revoke execute on function public.find_automation_expert_lead_v1(jsonb, bigint, text, text) from authenticated;
        revoke execute on function public.read_automation_expert_lead_history_v1(jsonb, uuid) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke execute on function public.read_automation_expert_flows_v1(jsonb) from service_role;
        revoke execute on function public.find_automation_expert_lead_v1(jsonb, bigint, text, text) from service_role;
        revoke execute on function public.read_automation_expert_lead_history_v1(jsonb, uuid) from service_role;
        grant execute on function public.read_automation_expert_flows_v1(jsonb) to service_role;
        grant execute on function public.find_automation_expert_lead_v1(jsonb, bigint, text, text) to service_role;
        grant execute on function public.read_automation_expert_lead_history_v1(jsonb, uuid) to service_role;
    end if;
end;
$roles$;

commit;
