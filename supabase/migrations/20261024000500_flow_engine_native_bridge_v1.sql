-- flow-engine-native-bridge-v1: lo que el bridge del motor nativo necesita de
-- la base (el PR 3 del hito 4). Contrato: docs/contracts/flow-engine-v2.md,
-- secciones 4, 5 y 8. Diseno: «el hito 4» (2026-10-24, en el vault), piezas A,
-- B y F. Encargo E-065.
--
-- Nace sin efecto: sin un flujo nativo (en ATT1 hoy no hay ninguno), las
-- lecturas nuevas devuelven listas vacias, la cola queda vacia y la rama nueva
-- de la adopcion da falso. Lo que trae:
--
-- 1. Los flujos nativos de la instancia, para que el bridge sepa si un evento
--    tiene a quien ir sin llamar a la admision por cada evento:
--    read_flow_engine_native_flows_v1.
-- 2. La cola de las compras que van a la admision nativa
--    (flow_engine_native_event_queue). El worker anota la compra ANTES de
--    aplicarla: apply_hotmart_purchase_approved la marca processed (o failed
--    con not_found), y despues de eso el lote ya no la vuelve a ver. Si la
--    admision nativa no contesta, la compra sigue en la cola y el lote la
--    reintenta, solo cuando ya se aplico (el corte va siempre primero).
--    enqueue_flow_engine_native_event_v1, read_flow_engine_native_event_queue_v1
--    y finish_flow_engine_native_event_v1.
-- 3. MOT-002 (contrato, seccion 8): read_flow_engine_start_rejections_v1 y
--    mark_flow_engine_start_rejection_notified_v1, con lo avisado en
--    flow_engine_rejection_notices. flow_engine_start_rejections no cambia.
--    La lectura suma flow_id (la marca lo pide y la forma del contrato no lo
--    traia) y el escalon (1, 10, 100). Avisa los motivos en los que fallo el
--    motor: error_interno:*, flow_engine_pool_unknown, politica_compilada:* y
--    motor_nativo:* (una version publicada que el motor no ejecuta). Vale
--    para todos los flujos del motor, el carrito incluido.
-- 4. El paso etiquetar (D4): flow_engine_native_step_tail_v1 dice, para la
--    accion de un recorrido nativo, si su version fijada lleva etiquetar, y el
--    producto y el momento de la compra para la nota. La admision nativa lo
--    saca de la politica compilada (20261024000200): lo ejecuta el bridge
--    despues de que la plantilla quedo aceptada.
-- 5. La respuesta a una plantilla nativa: admit_portable_inbound_commercial_case_v1
--    adopta la conversacion solo si el caso de la plantilla tiene binding del
--    piloto. Un caso nativo no tiene, asi que la respuesta a la bienvenida no
--    llegaba al agente (la v2 la rechaza con
--    inbound_canonical_conversation_conflict). La rama nueva acepta tambien un
--    caso con su fila en flow_engine_native_runs; con cualquier otro caso es la
--    de hoy.
--
-- Los reemplazos van sobre la definicion VIGENTE, con el metodo de
-- 20261024000200: un texto exacto que aparece una sola vez, la marca que no
-- puede estar, el duenio que aplica y la marca presente despues. La vigente
-- de admit_portable_inbound_commercial_case_v1 es 20261001000400 +
-- 20261009000200 + 20261019000400 + 20261019000600.
--
-- No crea roles, no cambia el ACL de lo que existe y no toca filas. Correrla
-- dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; P0002 no existe; 55000 un estado que no lo
-- permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public.admit_flow_engine_event_v1(jsonb,jsonb)') is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_missing';
    end if;
    if to_regclass('public.flow_engine_native_event_queue') is not null then
        raise exception using errcode = '55000', message = 'flow_engine_native_bridge_already_applied';
    end if;
end;
$already$;

create function pg_temp.flow_engine_native_bridge_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
begin
    if to_regprocedure(p_signature) is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_bridge_function_missing',
            detail = p_signature;
    end if;
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_engine_native_bridge_owner_mismatch',
            detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor)
       or position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_bridge_definition',
            detail = p_signature;
    end if;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_bridge_definition_not_replaced',
            detail = p_signature;
    end if;
end;
$function$;

-- ===========================================================================
-- Las tablas. Sin permisos para la API, como las otras del motor.
-- ===========================================================================

-- Las compras que van a la admision nativa: una fila por evento. done_at es
-- que la admision contesto (arranco, no arranco o rechazo la entrada para
-- siempre); attempts cuenta las que no contestaron.
create table public.flow_engine_native_event_queue (
    webhook_event_id uuid primary key references public.webhook_events (id) on delete restrict,
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    enqueued_at timestamptz not null default clock_timestamp(),
    attempts integer not null default 0 check (attempts >= 0),
    last_attempt_at timestamptz,
    done_at timestamptz
);

create index flow_engine_native_event_queue_pending_idx
    on public.flow_engine_native_event_queue (tenant_ref, ally_ref, enqueued_at)
    where done_at is null;

-- Lo avisado de MOT-002: por flujo, fase y motivo, la cuenta del ultimo aviso.
create table public.flow_engine_rejection_notices (
    flow_id uuid not null references public.flow_controls (flow_id) on delete restrict,
    phase text not null check (phase in ('admision', 'despacho')),
    reason text not null check (reason ~ '^[a-z0-9_:]{1,80}$'),
    notified_count integer not null check (notified_count > 0),
    notified_at timestamptz not null,
    primary key (flow_id, phase, reason)
);

alter table public.flow_engine_native_event_queue enable row level security;
alter table public.flow_engine_rejection_notices enable row level security;

-- ===========================================================================
-- 1. Los flujos nativos de la instancia.
-- ===========================================================================

-- Los flujos con ruta nativa que no estan apagados, con el disparador de su
-- version activa. El bridge llama a la admision nativa solo para un evento de
-- un producto con un flujo prendido o en cohorte que dispara con ese evento.
create function public.read_flow_engine_native_flows_v1(p_instance jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_flows jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    select coalesce(jsonb_agg(jsonb_build_object(
               'flow_id', c.flow_id,
               'product_ref', c.product_ref,
               'flow_key', c.flow_key,
               'state', c.state,
               'active_version', c.active_version,
               'trigger', d.definition #>> '{dispara,evento}')
               order by c.product_ref, c.flow_key), '[]'::jsonb)
      into v_flows
      from public.flow_controls c
      join public.flow_engine_routes r on r.flow_id = c.flow_id and r.legacy_kind is null
      left join public.flow_definitions d on d.flow_id = c.flow_id and d.version = c.active_version
     where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
       and c.state <> 'apagado';
    return jsonb_build_object('flows', v_flows);
end;
$function$;

-- ===========================================================================
-- 2. La cola de las compras para la admision nativa.
-- ===========================================================================

-- Anota la compra antes de aplicarla. true si la anoto ahora, false si ya
-- estaba. Solo una compra aprobada de Hotmart.
create function public.enqueue_flow_engine_native_event_v1(p_instance jsonb, p_webhook_event_id uuid)
returns boolean
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_event public.webhook_events%rowtype;
    v_inserted integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_webhook_event_id is null then
        raise exception using errcode = '22023', message = 'flow_engine_native_queue_parameters_invalid';
    end if;
    select event_row.* into v_event from public.webhook_events event_row where event_row.id = p_webhook_event_id;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_engine_native_queue_event_not_found';
    end if;
    if v_event.source <> 'hotmart' or v_event.event_type is distinct from 'PURCHASE_APPROVED' then
        raise exception using errcode = '22023', message = 'flow_engine_native_queue_event_invalid';
    end if;
    insert into public.flow_engine_native_event_queue (webhook_event_id, tenant_ref, ally_ref)
    values (p_webhook_event_id, v_instance.o_tenant_ref, v_instance.o_ally_ref)
    on conflict (webhook_event_id) do nothing;
    get diagnostics v_inserted = row_count;
    return v_inserted = 1;
end;
$function$;

-- Las compras anotadas que la admision todavia no contesto, de a lo sumo
-- p_limit, solo las que el worker ya aplico (el evento dejo de estar
-- received: primero el corte, despues el flujo), de las ultimas 72 horas y
-- con menos de 20 intentos. Trae el payload, como fetch_pending_events.
create function public.read_flow_engine_native_event_queue_v1(p_instance jsonb, p_limit integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_pending integer;
    v_events jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_limit is null or p_limit < 1 or p_limit > 50 then
        raise exception using errcode = '22023', message = 'flow_engine_native_queue_parameters_invalid';
    end if;
    select count(*)::integer into v_pending
      from public.flow_engine_native_event_queue q
     where q.tenant_ref = v_instance.o_tenant_ref and q.ally_ref = v_instance.o_ally_ref
       and q.done_at is null;
    select coalesce(jsonb_agg(jsonb_build_object(
               'webhook_event_id', x.webhook_event_id,
               'attempts', x.attempts,
               'payload', x.payload) order by x.enqueued_at, x.webhook_event_id), '[]'::jsonb)
      into v_events
      from (
          select q.webhook_event_id, q.attempts, q.enqueued_at, e.payload
            from public.flow_engine_native_event_queue q
            join public.webhook_events e on e.id = q.webhook_event_id
           where q.tenant_ref = v_instance.o_tenant_ref and q.ally_ref = v_instance.o_ally_ref
             and q.done_at is null
             and e.processing_status <> 'received'
             and q.enqueued_at > clock_timestamp() - interval '72 hours'
             and q.attempts < 20
           order by q.enqueued_at, q.webhook_event_id
           limit p_limit) x;
    return jsonb_build_object('pending', v_pending, 'events', v_events);
end;
$function$;

-- Cierra la compra (p_done: la admision contesto) o suma un intento. true si
-- cambio algo; false con una compra que no esta o ya estaba cerrada.
create function public.finish_flow_engine_native_event_v1(
    p_webhook_event_id uuid, p_done boolean, p_now timestamptz)
returns boolean
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_updated integer;
begin
    if p_webhook_event_id is null or p_done is null or p_now is null then
        raise exception using errcode = '22023', message = 'flow_engine_native_queue_parameters_invalid';
    end if;
    update public.flow_engine_native_event_queue q
       set done_at = case when p_done then p_now else q.done_at end,
           attempts = q.attempts + case when p_done then 0 else 1 end,
           last_attempt_at = p_now
     where q.webhook_event_id = p_webhook_event_id and q.done_at is null;
    get diagnostics v_updated = row_count;
    return v_updated = 1;
end;
$function$;

-- ===========================================================================
-- 3. MOT-002: el motor no pudo arrancar un flujo.
-- ===========================================================================

-- Los motivos que avisa: los que dicen que fallo el motor, no la persona.
create function public._flow_engine_rejection_is_alertable(p_reason text)
returns boolean
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select coalesce(p_reason = 'flow_engine_pool_unknown'
                    or p_reason like 'error\_interno:%'
                    or p_reason like 'politica\_compilada:%'
                    or p_reason like 'motor\_nativo:%', false)
$function$;

-- El escalon de una cuenta: 1, 10, 100 o 0.
create function public._flow_engine_rejection_step(p_count integer)
returns integer
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select case when p_count >= 100 then 100 when p_count >= 10 then 10 when p_count >= 1 then 1 else 0 end
$function$;

create function public.read_flow_engine_start_rejections_v1(
    p_instance jsonb, p_only_unnotified boolean, p_limit integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_unnotified integer;
    v_rejections jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_only_unnotified is null or p_limit is null or p_limit < 1 or p_limit > 50 then
        raise exception using errcode = '22023', message = 'flow_engine_start_rejections_parameters_invalid';
    end if;
    with grouped as (
        select r.flow_id, c.flow_key, c.product_ref, r.phase, r.reason,
               max(r.version) as version, sum(r.count)::integer as count,
               min(r.first_at) as first_at, max(r.last_at) as last_at
          from public.flow_engine_start_rejections r
          join public.flow_controls c on c.flow_id = r.flow_id
         where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
           and public._flow_engine_rejection_is_alertable(r.reason)
         group by r.flow_id, c.flow_key, c.product_ref, r.phase, r.reason
    ), annotated as (
        select g.*, coalesce(n.notified_count, 0) as notified_count,
               public._flow_engine_rejection_step(g.count) as step,
               public._flow_engine_rejection_step(g.count)
                   > public._flow_engine_rejection_step(coalesce(n.notified_count, 0)) as unnotified
          from grouped g
          left join public.flow_engine_rejection_notices n
            on n.flow_id = g.flow_id and n.phase = g.phase and n.reason = g.reason
    )
    select (select count(*)::integer from annotated where unnotified),
           coalesce((select jsonb_agg(jsonb_build_object(
                   'flow_id', x.flow_id,
                   'flow_key', x.flow_key,
                   'product_ref', x.product_ref,
                   'version', x.version,
                   'phase', x.phase,
                   'reason', x.reason,
                   'count', x.count,
                   'step', x.step,
                   'notified_count', x.notified_count,
                   'first_at', to_char(x.first_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                   'last_at', to_char(x.last_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
                   order by x.first_at, x.flow_id, x.phase, x.reason)
              from (select a.* from annotated a
                     where not p_only_unnotified or a.unnotified
                     order by a.first_at, a.flow_id, a.phase, a.reason
                     limit p_limit) x), '[]'::jsonb)
      into v_unnotified, v_rejections;
    return jsonb_build_object('unnotified', v_unnotified, 'rejections', v_rejections);
end;
$function$;

-- La marca del aviso: guarda la cuenta avisada. true si subio el escalon con
-- esta llamada; false si ya estaba avisada en ese escalon o si el flujo, la
-- fase o el motivo no tienen rechazos.
create function public.mark_flow_engine_start_rejection_notified_v1(
    p_flow_id uuid, p_phase text, p_reason text, p_notified_count integer, p_notified_at timestamptz)
returns boolean
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_previous integer;
begin
    if p_flow_id is null or p_phase is null or p_reason is null or p_notified_count is null
       or p_notified_count < 1 or p_notified_at is null then
        raise exception using errcode = '22023', message = 'flow_engine_start_rejection_mark_parameters_invalid';
    end if;
    if not exists (select 1 from public.flow_engine_start_rejections r
                    where r.flow_id = p_flow_id and r.phase = p_phase and r.reason = p_reason) then
        return false;
    end if;
    select n.notified_count into v_previous from public.flow_engine_rejection_notices n
     where n.flow_id = p_flow_id and n.phase = p_phase and n.reason = p_reason
       for update;
    if found and public._flow_engine_rejection_step(v_previous)
                 >= public._flow_engine_rejection_step(p_notified_count) then
        return false;
    end if;
    insert into public.flow_engine_rejection_notices (flow_id, phase, reason, notified_count, notified_at)
    values (p_flow_id, p_phase, p_reason, p_notified_count, p_notified_at)
    on conflict (flow_id, phase, reason) do update
       set notified_count = excluded.notified_count, notified_at = excluded.notified_at;
    return true;
end;
$function$;

-- ===========================================================================
-- 4. El paso etiquetar.
-- ===========================================================================

-- Para la accion de un recorrido nativo: su flujo y version, la etiqueta del
-- paso etiquetar de la version fijada (o null), el producto y el momento del
-- disparo. Null para cualquier otra accion. Sin datos de la persona.
create function public.flow_engine_native_step_tail_v1(p_action_id uuid)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    select jsonb_build_object(
               'flow_id', run.flow_id,
               'version', run.version,
               'etiquetar', (select step.value ->> 'etiquetar'
                               from jsonb_array_elements(coalesce(definition_row.definition -> 'pasos', '[]'::jsonb))
                                    with ordinality as step(value, ordinality)
                              where jsonb_typeof(step.value) = 'object' and step.value ? 'etiquetar'
                              order by step.ordinality
                              limit 1),
               'producto', recovery.product_name,
               'momento', to_char(action_row.anchor_observed_at at time zone 'UTC',
                                  'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
      from public.scheduled_actions action_row
      join public.flow_engine_native_runs native on native.recovery_case_id = action_row.recovery_case_id
      join public.flow_engine_runs run on run.recovery_case_id = action_row.recovery_case_id
      join public.flow_definitions definition_row
        on definition_row.flow_id = run.flow_id and definition_row.version = run.version
      join public.recovery_cases recovery on recovery.id = action_row.recovery_case_id
     where action_row.id = p_action_id
$function$;

-- ===========================================================================
-- 5. La respuesta a una plantilla nativa entra al agente.
-- ===========================================================================

do $patch_adoption$
begin
    perform pg_temp.flow_engine_native_bridge_patch(
        'public.admit_portable_inbound_commercial_case_v1(text,integer,bigint,text)',
        E'                    join public.pilot_recovery_case_bindings binding\n'
        || E'                      on binding.recovery_case_id = recovery.id\n'
        || E'                    where message.conversation_id = v_conversation.id\n',
        E'                    -- flow_engine_native_adoption: begin\n'
        || E'                    -- Un caso nativo (20261024000500) no tiene binding del\n'
        || E'                    -- piloto: su fila en flow_engine_native_runs vale igual.\n'
        || E'                    left join public.pilot_recovery_case_bindings binding\n'
        || E'                      on binding.recovery_case_id = recovery.id\n'
        || E'                    where message.conversation_id = v_conversation.id\n'
        || E'                      and (binding.recovery_case_id is not null\n'
        || E'                           or exists (select 1 from public.flow_engine_native_runs native_run\n'
        || E'                                       where native_run.recovery_case_id = recovery.id))\n'
        || E'                    -- flow_engine_native_adoption: end\n',
        'flow_engine_native_adoption: begin');
end;
$patch_adoption$;

-- ===========================================================================
-- Los permisos. Las lecturas, la cola, la marca y la cola del paso son de
-- service_role; los helpers, de nadie. Las tablas, de nadie.
-- ===========================================================================

do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public.read_flow_engine_native_flows_v1(jsonb)',
        'public.enqueue_flow_engine_native_event_v1(jsonb,uuid)',
        'public.read_flow_engine_native_event_queue_v1(jsonb,integer)',
        'public.finish_flow_engine_native_event_v1(uuid,boolean,timestamptz)',
        'public._flow_engine_rejection_is_alertable(text)',
        'public._flow_engine_rejection_step(integer)',
        'public.read_flow_engine_start_rejections_v1(jsonb,boolean,integer)',
        'public.mark_flow_engine_start_rejection_notified_v1(uuid,text,text,integer,timestamptz)',
        'public.flow_engine_native_step_tail_v1(uuid)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role')
             order by r.rolname
        loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
    end loop;

    revoke all on table public.flow_engine_native_event_queue, public.flow_engine_rejection_notices from public;
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on table public.flow_engine_native_event_queue, '
            'public.flow_engine_rejection_notices from %I', v_role);
    end loop;

    if to_regrole('service_role') is not null then
        grant execute on function public.read_flow_engine_native_flows_v1(jsonb) to service_role;
        grant execute on function public.enqueue_flow_engine_native_event_v1(jsonb, uuid) to service_role;
        grant execute on function public.read_flow_engine_native_event_queue_v1(jsonb, integer) to service_role;
        grant execute on function public.finish_flow_engine_native_event_v1(uuid, boolean, timestamptz)
            to service_role;
        grant execute on function public.read_flow_engine_start_rejections_v1(jsonb, boolean, integer)
            to service_role;
        grant execute on function public.mark_flow_engine_start_rejection_notified_v1(
            uuid, text, text, integer, timestamptz) to service_role;
        grant execute on function public.flow_engine_native_step_tail_v1(uuid) to service_role;
    end if;
end;
$acl$;

commit;
