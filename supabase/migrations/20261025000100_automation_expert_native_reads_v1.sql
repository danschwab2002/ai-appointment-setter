-- automation-expert-native-reads-v1: el PR 5 del hito 4. El Automation
-- Expert ve los flujos y los recorridos del motor nativo, cuenta las ventas
-- por flujo y puede volver un flujo nativo de pausado a la cohorte.
-- Contratos: docs/contracts/automation-expert-read-v1.md (1.2.0, «Lo que suma
-- el motor nativo (1.2.0)»), docs/contracts/flow-engine-v2.md (2.3.0, seccion
-- 15) y docs/contracts/flow-change-cycle-v1.md (1.8.0).
--
-- Lo que trae, por bloques:
--
-- 1. Tres lecturas nuevas, de solo lectura, que ejecuta solo service_role
--    (como las tres del AE de 20261015000200):
--    * read_automation_expert_native_flows_v1(p_instance): los flujos con
--      ruta nativa de la instancia, apagados incluidos, con su estado, su
--      version activa y su definicion, su bolsa, cuantos tiene la cohorte y
--      lo que paso en los ultimos 7 dias corridos;
--    * read_automation_expert_native_history_v1(p_instance, p_lead_ref): los
--      momentos de los recorridos nativos de una persona, en la forma de los
--      momentos de read_automation_expert_lead_history_v1, y de que cohortes
--      es;
--    * read_flow_sales_v1(p_instance, p_since): por flujo del motor (el
--      carrito en motor y los nativos), las compras que siguieron a un envio
--      aceptado: las que el recuperador ya ato a un caso del flujo, y las que
--      no ato nadie, por la persona (el mail), en los 7 dias despues del
--      envio. Cada compra cuenta una sola vez.
-- 2. El anclaje de una persona a la instancia en
--    find_automation_expert_lead_v1 y read_automation_expert_lead_history_v1
--    suma un camino: un recorrido nativo de esa bandeja
--    (flow_engine_native_runs.chatwoot_inbox_id). Sin recorridos nativos,
--    las dos dan exactamente lo de antes.
-- 3. prender con a: cohorte (ciclo 1.8.0): _flow_change_items_are_valid
--    acepta la clave a, solo en un prender y solo con el valor cohorte, y
--    _flow_change_publish lleva el control a cohorte en vez de a prendido.
--    La transicion pausado -> cohorte ya la deja validate_flow_control_change
--    (20261024000200), que tambien frena cohorte sobre una ruta del legado.
--    Sin la clave a, prender hace lo de antes.
--
-- Los reemplazos van sobre la definicion VIGENTE, con el metodo de
-- 20261024000200: un texto exacto que aparece las veces esperadas, la marca
-- que no puede estar, el duenio que aplica, la marca presente despues y el
-- ACL igual. Las vigentes:
--   find_automation_expert_lead_v1 ............ 20261015000200
--   read_automation_expert_lead_history_v1 .... 20261015000200 + 20261022000300
--   _flow_change_items_are_valid .............. 20261016000100 + 20261024000600
--   _flow_change_publish ...................... 20261016000100 + 20261024000600
--
-- Lo que no hace: no crea tablas, roles ni filas; no toca el despacho, la
-- admision ni la reevaluacion; no cambia lo que se manda. Correrla dos veces
-- aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; 55000 un estado que no lo permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public.operator_flow_cohort_v1(jsonb,text,text,text,text,text,text)') is null then
        raise exception using errcode = '55000', message = 'flow_change_new_flow_missing';
    end if;
    if to_regprocedure('public.read_automation_expert_native_flows_v1(jsonb)') is not null then
        raise exception using errcode = '55000', message = 'automation_expert_native_reads_already_applied';
    end if;
end;
$already$;

-- Los reemplazos por ancla. p_expected: cuantas veces tiene que aparecer el
-- ancla (todas se reemplazan).
create function pg_temp.automation_expert_native_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text, p_expected integer)
returns void
language plpgsql
as $function$
declare
    v_definition text;
    v_acl_before text;
begin
    if to_regprocedure(p_signature) is null then
        raise exception using errcode = '55000', message = 'automation_expert_native_function_missing',
            detail = p_signature;
    end if;
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'automation_expert_native_owner_mismatch',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if (length(v_definition) - length(replace(v_definition, p_anchor, ''))) <> length(p_anchor) * p_expected
       or position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_automation_expert_native_definition',
            detail = p_signature;
    end if;
    select coalesce(p.proacl::text, '') into v_acl_before from pg_proc p where p.oid = p_signature::regprocedure;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'automation_expert_native_definition_not_replaced',
            detail = p_signature;
    end if;
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from v_acl_before then
        raise exception using errcode = '55000', message = 'automation_expert_native_acl_changed',
            detail = p_signature;
    end if;
end;
$function$;

-- ===========================================================================
-- Bloque 1. Las tres lecturas.
-- ===========================================================================

-- Los flujos con ruta nativa de la instancia (todos, apagados incluidos: el AE
-- muestra lo que hay). La ventana de lo observado son los ultimos 7 dias
-- corridos hasta now(). Nada de una persona: solo conteos.
create function public.read_automation_expert_native_flows_v1(p_instance jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_now timestamptz := now();
    v_from timestamptz := now() - interval '7 days';
    v_flows jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    with
    flows as (
        select c.flow_id, c.product_ref, c.flow_key, c.state, c.active_version, c.changed_at,
               d.definition
          from public.flow_controls c
          join public.flow_engine_routes r on r.flow_id = c.flow_id and r.legacy_kind is null
          left join public.flow_definitions d on d.flow_id = c.flow_id and d.version = c.active_version
         where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
    ),
    runs as (
        select run.recovery_case_id, run.flow_id, run.pool, run.started_at
          from public.flow_engine_runs run
          join flows on flows.flow_id = run.flow_id
    ),
    actions as (
        select action.id, action.status, runs.flow_id
          from public.scheduled_actions action
          join runs on runs.recovery_case_id = action.recovery_case_id
    ),
    accepted as (
        select actions.flow_id, count(*) as n
          from public.followup_delivery_attempts attempt
          join actions on actions.id = attempt.action_id
         where attempt.outcome = 'accepted_by_chatwoot'
           and attempt.accepted_at >= v_from and attempt.accepted_at < v_now
         group by actions.flow_id
    ),
    rejections as (
        select rejection.flow_id, jsonb_object_agg(rejection.reason, rejection.n) as by_reason
          from (select sr.flow_id, sr.reason, sum(sr.count)::integer as n
                  from public.flow_engine_start_rejections sr
                  join flows on flows.flow_id = sr.flow_id
                 where sr.last_at >= v_from
                 group by sr.flow_id, sr.reason) rejection
         group by rejection.flow_id
    )
    select coalesce(jsonb_agg(jsonb_build_object(
               'flow_id', flows.flow_id,
               'product_ref', flows.product_ref,
               'flow_key', flows.flow_key,
               'state', flows.state,
               'state_since', flows.changed_at,
               'active_version', flows.active_version,
               'definition', flows.definition,
               'pool', pool.pool,
               'pool_timezone', pool.timezone,
               'cohort_members', (select count(*)::integer from public.flow_cohort_members member
                                   where member.flow_id = flows.flow_id),
               'starts_today', (select count(*)::integer from public.flow_engine_request_authorizations engine_row
                                 where engine_row.flow_id = flows.flow_id
                                   and engine_row.local_budget_date
                                       = (v_now at time zone coalesce(pool.timezone, 'UTC'))::date),
               'observed', jsonb_build_object(
                   'window_days', 7,
                   'runs_started', (select count(*)::integer from runs
                                     where runs.flow_id = flows.flow_id
                                       and runs.started_at >= v_from and runs.started_at < v_now),
                   'accepted_by_chatwoot', coalesce((select accepted.n from accepted
                                                      where accepted.flow_id = flows.flow_id), 0),
                   'in_flight', (select count(*)::integer from actions
                                  where actions.flow_id = flows.flow_id
                                    and actions.status in ('pending', 'deferred', 'retryable_failed',
                                                           'delivery_unknown')),
                   'start_rejections', coalesce((select rejections.by_reason from rejections
                                                  where rejections.flow_id = flows.flow_id), '{}'::jsonb)))
               order by flows.product_ref, flows.flow_key), '[]'::jsonb)
      into v_flows
      from flows
      left join public.flow_engine_pools pool
        on flows.definition is not null and pool.pool = public._flow_engine_native_pool(flows.definition);
    return jsonb_build_object('flows', v_flows, 'window_from', v_from, 'database_at', v_now);
end;
$function$;

-- Los momentos de los recorridos nativos de una persona, atados a la bandeja
-- de la instancia. La misma forma que los de
-- read_automation_expert_lead_history_v1 (kind, at, flow_key, flow_version,
-- reason_code, certainty, attached_by, evidence y extra), con flow_version
-- {"definition": N}. null si la persona no tiene ningun recorrido nativo ni es
-- de ninguna cohorte de la instancia.
create function public.read_automation_expert_native_history_v1(p_instance jsonb, p_lead_ref uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_inbox bigint;
    v_moments jsonb;
    v_cohorts jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if coalesce(p_instance ->> 'chatwoot_inbox_id', '') !~ '^[1-9][0-9]{0,17}$' then
        raise exception using errcode = '22023', message = 'automation_expert_invalid_instance';
    end if;
    if p_lead_ref is null then
        raise exception using errcode = '22023', message = 'automation_expert_invalid_lead_ref';
    end if;
    v_inbox := (p_instance ->> 'chatwoot_inbox_id')::bigint;

    with
    runs as (
        select run.recovery_case_id, run.flow_id, run.flow_key, run.version, run.started_at,
               run.trigger_source_ref, native.trigger_event
          from public.flow_engine_runs run
          join public.flow_engine_native_runs native on native.recovery_case_id = run.recovery_case_id
          join public.flow_controls c on c.flow_id = run.flow_id
         where run.person_ref = p_lead_ref
           and native.chatwoot_inbox_id = v_inbox
           and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
    ),
    actions as (
        select action.id, action.status, action.terminal_reason, action.step_key, action.due_at,
               action.expires_at, action.updated_at, runs.flow_key, runs.version, runs.started_at
          from public.scheduled_actions action
          join runs on runs.recovery_case_id = action.recovery_case_id
    ),
    moments as (
        -- El recorrido arranco.
        select runs.started_at as at, 'entered_flow'::text as kind, runs.flow_key, runs.version,
               'flow_engine_native_started'::text as reason_code, null::text as decision,
               'flow_engine_runs'::text as evidence_table, runs.recovery_case_id as evidence_id,
               jsonb_build_object('event_type', case runs.trigger_event when 'compra' then 'PURCHASE_APPROVED' end)
                   as extra
          from runs
        union all
        -- El paso quedo programado.
        select actions.started_at, 'scheduled', actions.flow_key, actions.version,
               'flow_engine_native_step_scheduled', null, 'scheduled_actions', actions.id,
               jsonb_build_object('step_key', actions.step_key, 'due_at', actions.due_at,
                                  'expires_at', actions.expires_at, 'status', actions.status)
          from actions
        union all
        -- El motor autorizo el arranque.
        select auth.authorized_at, 'start_authorized', actions.flow_key, actions.version,
               'flow_engine_request_start_authorized', null, 'flow_engine_request_authorizations', auth.id,
               jsonb_build_object('local_budget_date', auth.local_budget_date)
          from public.flow_engine_request_authorizations auth
          join actions on actions.id = auth.action_id
        union all
        -- Lo que contesto Chatwoot a cada intento.
        select coalesce(attempt.accepted_at, attempt.request_started_at, attempt.started_at),
               case when attempt.outcome = 'accepted_by_chatwoot' then 'accepted_by_chatwoot'
                    else 'delivery_uncertain_or_failed' end,
               actions.flow_key, actions.version,
               case when attempt.outcome = 'accepted_by_chatwoot' then 'accepted_by_chatwoot'
                    else coalesce(attempt.reason_code, attempt.outcome) end,
               null, 'followup_delivery_attempts', attempt.id,
               jsonb_build_object('outcome', attempt.outcome)
          from public.followup_delivery_attempts attempt
          join actions on actions.id = attempt.action_id
         where attempt.outcome is not null
        union all
        -- El paso termino sin salir.
        select actions.updated_at,
               case when actions.status = 'expired' then 'expired' else 'stopped' end,
               actions.flow_key, actions.version,
               coalesce(actions.terminal_reason, actions.status),
               case when actions.status = 'expired' then null else 'cancel' end,
               'scheduled_actions', actions.id,
               jsonb_build_object('step_key', actions.step_key, 'status', actions.status)
          from actions
         where actions.status in ('cancelled', 'skipped', 'expired', 'permanent_failed', 'superseded')
        union all
        -- Una compra que el recuperador ato al caso del recorrido (no la que lo disparo).
        select coalesce(rc.won_at, event_row.received_at), 'purchased', runs.flow_key, runs.version,
               'purchase_detected', null, 'webhook_events', event_row.id,
               jsonb_build_object('event_type', event_row.event_type)
          from runs
          join public.recovery_cases rc on rc.id = runs.recovery_case_id
          join public.webhook_events event_row on event_row.id = rc.purchase_event_id
         where rc.purchase_event_id is distinct from runs.trigger_source_ref
    )
    select coalesce(jsonb_agg(jsonb_build_object(
               'at', moments.at,
               'kind', moments.kind,
               'flow_key', moments.flow_key,
               'flow_version', jsonb_build_object('definition', moments.version),
               'reason_code', moments.reason_code,
               'decision', moments.decision,
               'certainty', 'stored',
               'attached_by', 'contact',
               'evidence', jsonb_build_object('table', moments.evidence_table, 'id', moments.evidence_id),
               'extra', jsonb_strip_nulls(moments.extra))
               order by moments.at, moments.evidence_table, moments.evidence_id), '[]'::jsonb)
      into v_moments
      from moments
     where moments.at is not null;

    select coalesce(jsonb_agg(c.flow_key order by c.flow_key), '[]'::jsonb)
      into v_cohorts
      from public.flow_cohort_members member
      join public.flow_controls c on c.flow_id = member.flow_id
     where member.contact_id = p_lead_ref
       and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref;

    if v_moments = '[]'::jsonb and v_cohorts = '[]'::jsonb then
        return null;
    end if;
    return jsonb_build_object('moments', v_moments, 'cohort_of', v_cohorts, 'database_at', now());
end;
$function$;

-- Las ventas por flujo del motor (el carrito en motor y los nativos). La
-- regla, purchase_after_accepted_send_v1 (contrato de lectura, 1.2.0, «Las
-- ventas por flujo»; la decide Dan, D-080-1):
--   * una venta es una compra aprobada de Hotmart de un producto de la
--     instancia, contada una vez por transaccion;
--   * va al recorrido al que el recuperador ya la ato (by_case:
--     recovery_cases.purchase_event_id, que la correlacion de la compra
--     escribe al cerrar el caso abierto; es unique);
--   * si no la ato ningun caso, va por la persona (by_identity): al recorrido
--     del motor de la misma persona (su mail en contact_points) con el ultimo
--     envio aceptado por Chatwoot en los 7 dias antes de la compra. Asi cuenta
--     la compra que llega despues de que el recorrido ya termino (una
--     bienvenida de un solo envio cierra el caso al salir);
--   * la compra que disparo el recorrido no cuenta para ese recorrido;
--   * el toque es el ultimo paso aceptado antes de la compra. Una compra atada
--     a un caso sin ningun envio antes va aparte (purchases_before_send).
-- No es causalidad: es «compro despues de recibir el mensaje». p_since: desde
-- cuando (a lo sumo 31 dias atras). Nada de una persona sale.
create function public.read_flow_sales_v1(p_instance jsonb, p_since timestamptz)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_now timestamptz := now();
    v_flows jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_since is null or p_since > v_now then
        raise exception using errcode = '22023', message = 'flow_sales_since_invalid';
    end if;
    if p_since < v_now - interval '31 days' then
        raise exception using errcode = '22023', message = 'flow_sales_window_too_wide';
    end if;
    with
    flows as (
        select c.flow_id, c.product_ref, c.flow_key,
               exists (select 1 from public.flow_engine_routes r
                        where r.flow_id = c.flow_id and r.legacy_kind is null) as native
          from public.flow_controls c
         where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
           and exists (select 1 from public.flow_engine_runs run where run.flow_id = c.flow_id)
    ),
    runs as (
        select run.recovery_case_id, run.flow_id, run.person_ref, run.started_at, run.trigger_source_ref
          from public.flow_engine_runs run
          join flows on flows.flow_id = run.flow_id
    ),
    sends as materialized (
        select runs.recovery_case_id, runs.flow_id, runs.person_ref, runs.trigger_source_ref,
               action.step_key, attempt.accepted_at
          from runs
          join public.scheduled_actions action on action.recovery_case_id = runs.recovery_case_id
          join public.followup_delivery_attempts attempt on attempt.action_id = action.id
         where attempt.outcome = 'accepted_by_chatwoot' and attempt.accepted_at is not null
    ),
    products as (
        select distinct b.hotmart_product_id::text as product_id
          from public.commercial_ally_runtime_bindings b
         where b.tenant_ref = v_instance.o_tenant_ref and b.ally_ref = v_instance.o_ally_ref
           and b.hotmart_product_id is not null
    ),
    purchases as materialized (
        select distinct on (coalesce(e.payload #>> '{data,purchase,transaction}', e.id::text))
               e.id, e.received_at as purchased_at,
               nullif(lower(btrim(e.payload #>> '{data,buyer,email}')), '') as email
          from public.webhook_events e
          join products on products.product_id = e.payload #>> '{data,product,id}'
         where e.source = 'hotmart' and e.event_type = 'PURCHASE_APPROVED'
           and e.received_at >= p_since and e.received_at < v_now
         order by coalesce(e.payload #>> '{data,purchase,transaction}', e.id::text), e.received_at
    ),
    by_case as (
        select purchases.id, runs.flow_id, runs.person_ref,
               (select s.step_key from sends s
                 where s.recovery_case_id = runs.recovery_case_id and s.accepted_at <= purchases.purchased_at
                 order by s.accepted_at desc limit 1) as step_key
          from purchases
          join public.recovery_cases rc on rc.purchase_event_id = purchases.id
          join runs on runs.recovery_case_id = rc.id
         where runs.trigger_source_ref is distinct from purchases.id
    ),
    by_identity as (
        select purchases.id, touch.flow_id, touch.person_ref, touch.step_key
          from purchases
          join lateral (
              select s.flow_id, s.person_ref, s.step_key
                from sends s
               where purchases.email is not null
                 and s.trigger_source_ref is distinct from purchases.id
                 and s.accepted_at <= purchases.purchased_at
                 and s.accepted_at > purchases.purchased_at - interval '7 days'
                 and exists (select 1 from public.contact_points cp
                              where cp.contact_id = s.person_ref and cp.type = 'email'
                                and lower(btrim(cp.normalized_value)) = purchases.email)
               order by s.accepted_at desc
               limit 1) touch on true
         where not exists (select 1 from public.recovery_cases rc where rc.purchase_event_id = purchases.id)
    ),
    sales as (
        select by_case.*, 'case'::text as attached_by from by_case
        union all
        select by_identity.*, 'identity'::text from by_identity
    )
    select coalesce(jsonb_agg(jsonb_build_object(
               'flow_id', flows.flow_id,
               'product_ref', flows.product_ref,
               'flow_key', flows.flow_key,
               'native', flows.native,
               'runs_started', (select count(*)::integer from runs
                                 where runs.flow_id = flows.flow_id
                                   and runs.started_at >= p_since and runs.started_at < v_now),
               'purchases_after_send', (select count(*)::integer from sales
                                         where sales.flow_id = flows.flow_id and sales.step_key is not null),
               'by_case', (select count(*)::integer from sales
                            where sales.flow_id = flows.flow_id and sales.step_key is not null
                              and sales.attached_by = 'case'),
               'by_identity', (select count(*)::integer from sales
                                where sales.flow_id = flows.flow_id and sales.attached_by = 'identity'),
               'people_after_send', (select count(distinct sales.person_ref)::integer from sales
                                      where sales.flow_id = flows.flow_id and sales.step_key is not null),
               'purchases_before_send', (select count(*)::integer from sales
                                          where sales.flow_id = flows.flow_id and sales.step_key is null),
               'by_step', coalesce((select jsonb_object_agg(step.step_key, step.n)
                                      from (select sales.step_key, count(*)::integer as n from sales
                                             where sales.flow_id = flows.flow_id and sales.step_key is not null
                                             group by sales.step_key) step), '{}'::jsonb))
               order by flows.product_ref, flows.flow_key), '[]'::jsonb)
      into v_flows
      from flows;
    return jsonb_build_object('since', p_since, 'until', v_now, 'flows', v_flows,
                              'rule', 'purchase_after_accepted_send_v1', 'identity_window_days', 7);
end;
$function$;

-- ===========================================================================
-- Bloque 2. El anclaje de una persona con un recorrido nativo.
-- ===========================================================================

select pg_temp.automation_expert_native_patch(
    'public.read_automation_expert_lead_history_v1(jsonb,uuid)',
    $a$        or exists (
            select 1 from public.contact_opt_out_events opt_out
             where opt_out.contact_id = p_lead_ref
               and opt_out.canonical_inbox_id = v_inbox)
    ) into v_anchored;$a$,
    $r$        or exists (
            select 1 from public.contact_opt_out_events opt_out
             where opt_out.contact_id = p_lead_ref
               and opt_out.canonical_inbox_id = v_inbox)
        -- automation_expert_native_anchor_v1: un recorrido nativo de esta bandeja.
        or exists (
            select 1
              from public.flow_engine_runs run
              join public.flow_engine_native_runs native on native.recovery_case_id = run.recovery_case_id
             where run.person_ref = p_lead_ref
               and native.chatwoot_inbox_id = v_inbox)
    ) into v_anchored;$r$,
    'automation_expert_native_anchor_v1', 1);

select pg_temp.automation_expert_native_patch(
    'public.find_automation_expert_lead_v1(jsonb,bigint,text,text)',
    $a$        select opt_out.contact_id from public.contact_opt_out_events opt_out
         where opt_out.canonical_inbox_id = v_inbox and opt_out.contact_id is not null
    ),$a$,
    $r$        select opt_out.contact_id from public.contact_opt_out_events opt_out
         where opt_out.canonical_inbox_id = v_inbox and opt_out.contact_id is not null
        union
        -- automation_expert_native_anchor_v1: un recorrido nativo de esta bandeja.
        select run.person_ref
          from public.flow_engine_runs run
          join public.flow_engine_native_runs native on native.recovery_case_id = run.recovery_case_id
         where native.chatwoot_inbox_id = v_inbox
    ),$r$,
    'automation_expert_native_anchor_v1', 1);

-- ===========================================================================
-- Bloque 3. prender con a: cohorte.
-- ===========================================================================

select pg_temp.automation_expert_native_patch(
    'public._flow_change_items_are_valid(jsonb)',
    $a$        elsif v_item ->> 'tipo' in ('prender', 'apagar') then
            if v_keys is distinct from array['flow_id', 'flujo', 'producto', 'tipo'] then
                return false;
            end if;$a$,
    $r$        elsif v_item ->> 'tipo' in ('prender', 'apagar') then
            -- flow_change_prender_a_cohorte_v1: prender puede llevar a: cohorte.
            if v_keys is distinct from array['flow_id', 'flujo', 'producto', 'tipo']
               and not (v_item ->> 'tipo' = 'prender'
                        and v_keys = array['a', 'flow_id', 'flujo', 'producto', 'tipo']
                        and v_item -> 'a' = '"cohorte"'::jsonb) then
                return false;
            end if;$r$,
    'flow_change_prender_a_cohorte_v1', 1);

select pg_temp.automation_expert_native_patch(
    'public._flow_change_publish(text,text,uuid,text,jsonb)',
    $a$            when 'prender' then 'prendido'$a$,
    $r$            -- flow_change_prender_a_cohorte_v1: prender con a: cohorte vuelve a la cohorte.
            when 'prender' then coalesce(v_state_item ->> 'a', 'prendido')$r$,
    'flow_change_prender_a_cohorte_v1', 2);

-- ===========================================================================
-- Los permisos: solo service_role ejecuta las tres lecturas.
-- ===========================================================================

revoke all on function public.read_automation_expert_native_flows_v1(jsonb) from public;
revoke all on function public.read_automation_expert_native_history_v1(jsonb, uuid) from public;
revoke all on function public.read_flow_sales_v1(jsonb, timestamptz) from public;

do $roles$
declare
    v_role text;
begin
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format('revoke all on function public.read_automation_expert_native_flows_v1(jsonb) from %I', v_role);
            execute format('revoke all on function public.read_automation_expert_native_history_v1(jsonb, uuid) from %I', v_role);
            execute format('revoke all on function public.read_flow_sales_v1(jsonb, timestamptz) from %I', v_role);
        end if;
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.read_automation_expert_native_flows_v1(jsonb) to service_role;
        grant execute on function public.read_automation_expert_native_history_v1(jsonb, uuid) to service_role;
        grant execute on function public.read_flow_sales_v1(jsonb, timestamptz) to service_role;
    end if;
end;
$roles$;

commit;
