-- automation-expert-flows-speed-v1: read_automation_expert_flows_v1 (la
-- lectura de /flows del Automation Expert) responde en milisegundos en vez de
-- pasarse del statement_timeout de 8 s del bridge.
-- Contrato: docs/contracts/automation-expert-read-v1.md (la salida no cambia).
--
-- Por que: en ATT1 /flows fallaba con SQLSTATE 57014 (8,1 s, medido por la
-- coordinadora el 22/10 12:46Z desde el bridge v1.16.0). auto_explain sobre
-- att1-db en solo lectura (22/10 13:30Z): de 10,6 s, 10,5 s son la consulta de
-- lo observado, y adentro tres joins de conversation_events con el CTE actions
-- que el planificador estima en 1 fila: nested loop que lee la tabla entera
-- por cada accion (~970). superseded 2,1 s; deferred 1,2 s y stopped 1,6 s,
-- cada uno tres veces (una por flujo, porque estaban inlined).
--
-- Como: un CTE reevaluated materializado lee una sola vez las reevaluaciones
-- de la ventana (event_type y fechas, igual que antes), y deferred, stopped,
-- superseded y authorized pasan a materialized para correr una vez y no una
-- por flujo. Los filtros por decision y reason_code son los mismos, sobre las
-- mismas columnas. El resto de la funcion queda letra por letra.
--
-- Lo que hace al aplicarse: solo reemplaza esta funcion. create or replace
-- conserva el duenio y los permisos; la migracion verifica las dos cosas.

begin;

set local lock_timeout = '5s';

do $owner$
begin
    if (select p.proowner from pg_proc p
         where p.oid = 'public.read_automation_expert_flows_v1(jsonb)'::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'automation_expert_flows_owner_mismatch';
    end if;
end;
$owner$;

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
    -- Las reevaluaciones de la ventana, leidas una sola vez. Antes cada uno de
    -- deferred, stopped y superseded leia conversation_events entera por cada
    -- accion (nested loop con la tabla adentro), y deferred y stopped otra vez
    -- por cada flujo: 10,5 s en att1-db el 22/10 con ~970 acciones.
    reevaluated as materialized (
        select event.related_action_id,
               event.created_at,
               event.data ->> 'decision' as decision,
               event.data ->> 'reason_code' as reason_code
          from public.conversation_events event
         where event.event_type = 'followup_action_reevaluated'
           and event.created_at >= v_window_from and event.created_at < v_window_to
    ),
    authorized as materialized (
        select actions.flow_key, count(*) as n
          from public.pilot_outbound_request_authorizations auth
          join actions on actions.id = auth.action_id
         where auth.authorized_at >= v_window_from and auth.authorized_at < v_window_to
         group by actions.flow_key
    ),
    deferred as materialized (
        select actions.flow_key, count(distinct actions.id) as n
          from reevaluated event
          join actions on actions.id = event.related_action_id
         where event.decision = 'defer'
         group by actions.flow_key
    ),
    stopped as materialized (
        select actions.flow_key,
               case when event.reason_code ~ '^[a-z][a-z0-9_:]{0,79}$'
                    then event.reason_code else 'invalid_reason_code' end as reason_code,
               count(*) as n
          from reevaluated event
          join actions on actions.id = event.related_action_id
         where event.decision in ('cancel', 'pause', 'escalate')
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
    superseded as materialized (
        select (
            select min(winner.trigger_at)
              from cases winner
             where winner.contact_id = actions.contact_id
               and winner.flow_key in ('carrito', 'pago_fallido')
               and winner.trigger_at >= actions.trigger_at
               and winner.trigger_at <= event.created_at
        ) - actions.trigger_at as form_to_winner
          from reevaluated event
          join actions on actions.id = event.related_action_id
         where actions.flow_key = 'precheckout'
           and event.reason_code = 'superseded_by_provider_event'
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

do $acl$
begin
    if has_function_privilege('public', 'public.read_automation_expert_flows_v1(jsonb)', 'execute') then
        raise exception using errcode = '55000', message = 'automation_expert_flows_acl_changed';
    end if;
    if exists (select 1 from pg_roles where rolname = 'anon')
       and has_function_privilege('anon', 'public.read_automation_expert_flows_v1(jsonb)', 'execute') then
        raise exception using errcode = '55000', message = 'automation_expert_flows_acl_changed';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated')
       and has_function_privilege('authenticated', 'public.read_automation_expert_flows_v1(jsonb)', 'execute') then
        raise exception using errcode = '55000', message = 'automation_expert_flows_acl_changed';
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role')
       and not has_function_privilege('service_role', 'public.read_automation_expert_flows_v1(jsonb)', 'execute') then
        raise exception using errcode = '55000', message = 'automation_expert_flows_acl_changed';
    end if;
end;
$acl$;

commit;
