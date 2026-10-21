-- flow-engine-routes-v1: las rutas de los flujos, las lecturas del motor y la
-- guarda del almacen sobre el legado (la T2.3a del motor de flujos).
-- Contrato: docs/contracts/flow-engine-v1.md (manda sobre el diseno). Diseno:
-- «el motor de flujos» (2026-10-16), §2.2 a §2.9, principios 7 y 12.
-- Especificacion: «T2.3: el motor en la base» (2026-10-21), parte T2.3a.
--
-- Nace apagada: ninguna ruta existe y ninguna funcion de esta migracion
-- planifica ni manda nada. Sin rutas, el legado se comporta exactamente como
-- hoy. Lo que trae:
--
-- 1. Cinco tablas vacias, con RLS y sin ningun permiso para la API:
--    flow_engine_routes (la ruta de un flujo: legado o motor),
--    flow_engine_runs, flow_engine_request_authorizations,
--    flow_engine_start_rejections y flow_engine_divergences. Las cuatro
--    ultimas las escribe el camino del motor (la T2.3b); aca solo existen, y
--    la bolsa del legado ya cuenta la tercera (punto 6).
-- 2. Los dos eventos nuevos del almacen, flow_route_created y
--    flow_route_moved, en el CHECK de flow_change_events. Los deja un
--    disparador de flow_engine_routes, como el de flow_controls: todo cambio
--    de una ruta deja su evento, lo haga una funcion o un UPDATE del duenio.
-- 3. Las dos lecturas del contrato, security definer y STABLE, para
--    service_role y para automation_expert_change (el rol de la T4, detras de
--    to_regrole): read_flow_engine_v1 (que ejecuta el motor, con lo en curso
--    por version) y read_flow_engine_history_v1 (la captura v3 restringida a
--    la instancia, al producto y a p_since, con una sal por llamada). La
--    consulta de la historia NO se escribe a mano: la arma
--    scripts/flow_engine_history_sql.py desde
--    tests/fixtures/flow_engine/att1_recovery_capture_query_v3.sql, y pytest
--    verifica que la de abajo sea exactamente esa.
-- 4. Las dos escrituras del contrato, solo del duenio y security invoker,
--    como las siete del almacen: operator_create_flow_route_v1 (siempre en
--    legado, y en la v1 solo para carrito, pago_fallido y ficha, los que la
--    guarda frena: flow_route_kind_not_guarded) y operator_route_flow_v1. En esta version el motor no ejecuta
--    ningun legacy_kind (_flow_engine_route_kinds() es vacia): mover a motor
--    devuelve siempre flow_route_kind_not_supported. La T2.3b la reemplaza
--    por {carrito}.
-- 5. _flow_engine_compile_policy, la gemela de flow_policy.compile_policy (la
--    T2.1): N pasos, la forma de pasos de cada legacy_kind y los mismos
--    errores. La atan tests/fixtures/flow_engine/compile_cases.json (PGlite y
--    pytest, los mismos casos) y la forma de los pasos, que pytest compara con
--    flow_policy.STEPS_BY_LEGACY_KIND.
-- 6. La guarda del almacen sobre el legado (principio 7, la parte legado) y
--    la bolsa contada por los dos caminos (principio 12), insertadas sobre la
--    definicion VIGENTE de siete funciones (el metodo de 20261007000100: un
--    texto exacto que tiene que aparecer una sola vez, y la marca que no puede
--    estar). Las vigentes son las de la ultima migracion que las redefinio:
--      plan_lancemos_pilot_cart_recovery ............ 20260930000300
--      plan_portable_payment_failure_recovery ....... 20261019000200
--      plan_portable_payment_slip_recovery .......... 20261019000100
--      mark_lancemos_pilot_request_started .......... 20261001000100
--      mark_portable_payment_failure_request_started  20261019000700
--      mark_portable_payment_slip_request_started ... 20261019000400
--      authorize_lancemos_pilot_request_start ....... 20261007000100
--    La guarda va al principio, despues de la validacion de los parametros y
--    antes de cualquier lock: toma la ruta y despues el control del flujo
--    (FOR SHARE los dos), antes del control del scope (el orden de §2.4). Va
--    por legacy_kind: pausar el carrito no frena al pago fallido aunque
--    compartan el scope. Sin ruta, sigue como hoy. Con la ruta en legado y el
--    flujo pausado o apagado rechaza como hoy rechaza pilot_runtime_not_armed:
--    al planificar, pilot_scope_rejected (el evento queda failed); al
--    arrancar, pilot_request_start_rejected (el bridge reintenta). En los dos,
--    el motivo en detail: flujo_pausado o flujo_apagado. Con la ruta en motor,
--    que en esta version solo puede poner un UPDATE del duenio, rechaza con
--    flow_engine_not_available: la delegacion en el motor llega con la T2.3b.
--    En un arranque, un intento ya autorizado (un replay) pasa: la
--    autorizacion de hoy lo devuelve antes de mirar el runtime. La migracion
--    aborta con 55000 si alguna de las siete tiene otro duenio que quien la
--    aplica.
--
-- Lo que no hace: no crea roles ni toca su configuracion, no cambia el ACL del
-- esquema ni los privilegios por defecto, no agrega claves foraneas a tablas
-- del sustrato (las tablas nuevas referencian solo al almacen) y no toca
-- ninguna fila que exista. Correrla dos veces aborta con 55000 sin cambiar
-- nada.
--
-- Errores: 22023 entrada invalida; P0002 no existe o no es de esta instancia,
-- sin distinguir; 55000 un estado que no lo permite; 23514 un invariante. Los
-- resultados del contrato vuelven como {"ok": false, "reason_code": ...}.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.flow_engine_routes') is not null then
        raise exception using errcode = '55000', message = 'flow_engine_routes_already_applied';
    end if;
end;
$already$;

-- ---------------------------------------------------------------------------
-- 1. Las tablas.
-- ---------------------------------------------------------------------------

-- La ruta de un flujo: quien lo ejecuta. Nace en legado y la crea la migracion
-- de cada flujo (la T5) al registrarlo. legacy_kind y legacy_scope_key no
-- cambian nunca. El scope es nulo solo para el cupon y la reactivacion, que no
-- tienen scope del piloto. Con un scope nulo, el UNIQUE no compara (dos
-- productos pueden tener cada uno su cupon); la guarda no los mira.
create table public.flow_engine_routes (
    flow_id uuid primary key references public.flow_controls (flow_id) on delete restrict,
    executor text not null default 'legado' check (executor in ('legado', 'motor')),
    legacy_kind text not null check (legacy_kind in
        ('carrito', 'pago_fallido', 'ficha', 'precheckout', 'descuento', 'reactivacion')),
    legacy_scope_key text check (legacy_scope_key ~ '^[a-z0-9][a-z0-9_-]{0,127}$'),
    generation bigint not null default 0 check (generation >= 0),
    changed_at timestamptz not null default clock_timestamp(),
    changed_by_ref text not null check (public._flow_store_actor_is_valid('operador', changed_by_ref)),
    change_reason text not null check (public._flow_store_free_text_is_valid(change_reason)),
    created_at timestamptz not null default clock_timestamp(),
    constraint flow_engine_routes_scope_by_kind
        check ((legacy_kind in ('descuento', 'reactivacion')) = (legacy_scope_key is null)),
    constraint flow_engine_routes_legacy unique (legacy_scope_key, legacy_kind)
);

-- Un recorrido del motor: el caso que abrio, con la version fijada. Solo suma.
-- El caso y el contacto no llevan clave foranea: el sustrato no se toca.
create table public.flow_engine_runs (
    recovery_case_id uuid primary key,
    flow_id uuid not null,
    version integer not null check (version > 0),
    definition_sha256 text not null check (definition_sha256 ~ '^[0-9a-f]{64}$'),
    product_ref text not null check (product_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    flow_key text not null check (flow_key ~ '^[a-z][a-z0-9_]{1,39}$'),
    pool text not null check (pool ~ '^[a-z0-9][a-z0-9_-]{0,127}$'),
    legacy_kind text check (legacy_kind in
        ('carrito', 'pago_fallido', 'ficha', 'precheckout', 'descuento', 'reactivacion')),
    legacy_scope_key text check (legacy_scope_key ~ '^[a-z0-9][a-z0-9_-]{0,127}$'),
    person_ref uuid not null,
    trigger_source_ref uuid not null,
    legacy_dispatch_ok boolean not null,
    started_at timestamptz not null default clock_timestamp(),
    foreign key (flow_id, version) references public.flow_definitions (flow_id, version) on delete restrict
);

-- Una autorizacion de arranque del motor: la bolsa la cuentan los dos caminos.
create table public.flow_engine_request_authorizations (
    id uuid primary key default gen_random_uuid(),
    flow_id uuid not null,
    version integer not null check (version > 0),
    pool text not null check (pool ~ '^[a-z0-9][a-z0-9_-]{0,127}$'),
    legacy_scope_key text check (legacy_scope_key ~ '^[a-z0-9][a-z0-9_-]{0,127}$'),
    action_id uuid not null,
    attempt_id uuid not null unique,
    contact_id uuid not null,
    local_budget_date date not null,
    flow_generation bigint not null check (flow_generation >= 0),
    authorized_at timestamptz not null default clock_timestamp(),
    foreign key (flow_id, version) references public.flow_definitions (flow_id, version) on delete restrict
);

-- Los disparos que el motor no arranco y los arranques rechazados del
-- despacho, con un upsert que suma count.
create table public.flow_engine_start_rejections (
    flow_id uuid not null references public.flow_controls (flow_id) on delete restrict,
    version integer check (version > 0),
    source_ref uuid not null,
    phase text not null check (phase in ('admision', 'despacho')),
    reason text not null check (reason ~ '^[a-z0-9_:]{1,80}$'),
    first_at timestamptz not null default clock_timestamp(),
    last_at timestamptz not null default clock_timestamp(),
    count integer not null default 1 check (count > 0),
    primary key (flow_id, source_ref, reason)
);

-- Cada vez que el motor no pudo admitir y cayo al legado: una fila por flujo,
-- clase y motivo.
create table public.flow_engine_divergences (
    id uuid primary key default gen_random_uuid(),
    kind text not null check (kind in
        ('admision_al_legado', 'politica_compilada', 'producto', 'ruta', 'transitorio')),
    flow_id uuid not null references public.flow_controls (flow_id) on delete restrict,
    version integer check (version > 0),
    reason text not null check (reason ~ '^[a-z0-9_:]{1,80}$'),
    sqlstate text check (sqlstate ~ '^[0-9A-Z]{5}$'),
    first_source_ref uuid,
    last_source_ref uuid,
    first_at timestamptz not null default clock_timestamp(),
    last_at timestamptz not null default clock_timestamp(),
    count integer not null default 1 check (count > 0),
    notified_at timestamptz,
    constraint flow_engine_divergences_one_row unique (flow_id, kind, reason)
);

create index flow_engine_runs_flow_version_idx on public.flow_engine_runs (flow_id, version);
create index flow_engine_request_authorizations_pool_idx
    on public.flow_engine_request_authorizations (pool, local_budget_date);

alter table public.flow_engine_routes enable row level security;
alter table public.flow_engine_runs enable row level security;
alter table public.flow_engine_request_authorizations enable row level security;
alter table public.flow_engine_start_rejections enable row level security;
alter table public.flow_engine_divergences enable row level security;

-- ---------------------------------------------------------------------------
-- 2. Los eventos de la ruta en el almacen.
-- ---------------------------------------------------------------------------

alter table public.flow_change_events
    drop constraint flow_change_events_event_type_check,
    add constraint flow_change_events_event_type_check check (event_type in (
        'flow_change_draft_saved', 'flow_change_validation_blocked',
        'flow_change_validation_rejected', 'flow_change_validated',
        'flow_change_approved', 'flow_change_approval_rejected',
        'flow_change_publish_rejected', 'flow_change_published',
        'flow_control_changed', 'flow_change_discarded', 'flow_change_expired',
        'flow_route_created', 'flow_route_moved'));

-- ---------------------------------------------------------------------------
-- 3. Los helpers de forma.
-- ---------------------------------------------------------------------------

-- El legacy_kind y el disparador de una definicion (contrato, seccion 3):
-- carrito con dispara.evento carrito, pago_fallido con pago_fallido, ficha con
-- ficha_impresa, precheckout con intencion, y el cupon y la reactivacion con
-- su silencio (lo_ultimo agente y persona: flow-definition-v1.md, bloques 4 y
-- 5).
create function public._flow_engine_kind_matches(p_legacy_kind text, p_definition jsonb)
returns boolean
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select coalesce(case p_legacy_kind
        when 'carrito' then p_definition -> 'dispara' = '{"evento": "carrito"}'::jsonb
        when 'pago_fallido' then p_definition -> 'dispara' = '{"evento": "pago_fallido"}'::jsonb
        when 'ficha' then p_definition -> 'dispara' = '{"evento": "ficha_impresa"}'::jsonb
        when 'precheckout' then p_definition -> 'dispara' = '{"evento": "intencion"}'::jsonb
        when 'descuento' then jsonb_typeof(p_definition #> '{dispara,silencio}') = 'object'
            and not (p_definition -> 'dispara' ? 'evento')
            and p_definition #> '{dispara,silencio,lo_ultimo}' = '"agente"'::jsonb
        when 'reactivacion' then jsonb_typeof(p_definition #> '{dispara,silencio}') = 'object'
            and not (p_definition -> 'dispara' ? 'evento')
            and p_definition #> '{dispara,silencio,lo_ultimo}' = '"persona"'::jsonb
    end, false)
$function$;

-- Los legacy_kind que el motor de esta version ejecuta (la regla 1 de mover a
-- motor). Vacia en la T2.3a: el camino del motor llega con la T2.3b, que la
-- reemplaza por {carrito}.
create function public._flow_engine_route_kinds()
returns text[]
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select array[]::text[]
$function$;

-- El scope del legado existe si tiene una version publicada del tenant.
create function public._flow_engine_legacy_scope_exists(p_tenant_ref text, p_scope_key text)
returns boolean
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select exists (
        select 1 from public.pilot_scope_versions s
         where s.scope_key = p_scope_key and s.tenant_key = p_tenant_ref and s.status = 'published')
$function$;

-- La forma de route del contrato (seccion 1). Las horas en UTC.
create function public._flow_engine_route_json(p_route public.flow_engine_routes)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case when p_route.flow_id is null then null else jsonb_build_object(
        'executor', p_route.executor,
        'legacy_kind', p_route.legacy_kind,
        'legacy_scope_key', p_route.legacy_scope_key,
        'generation', p_route.generation,
        'changed_at', to_char(p_route.changed_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')) end
$function$;

-- La secuencia del legado de cada legacy_kind (followup_sequences.reason).
create function public._flow_engine_sequence_reason(p_legacy_kind text)
returns text
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select case p_legacy_kind
        when 'carrito' then 'cart_abandonment'
        when 'pago_fallido' then 'payment_failure'
        when 'ficha' then 'payment_slip'
        when 'precheckout' then 'precheckout_intent'
    end
$function$;

-- Lo en curso del legado (contrato, seccion 1): los casos abiertos atados al
-- scope con una secuencia de su legacy_kind, sin los que arranco el motor.
-- Abierto es grace_period, active o paused, como el caso que elegiria el
-- planificador de hoy.
create function public._flow_engine_legacy_in_flight(p_scope_key text, p_legacy_kind text)
returns integer
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select count(distinct rc.id)::integer
      from public.recovery_cases rc
      join public.pilot_recovery_case_bindings b on b.recovery_case_id = rc.id
     where b.scope_key = p_scope_key
       and rc.status in ('grace_period', 'active', 'paused')
       and exists (select 1 from public.followup_sequences s
                    where s.recovery_case_id = rc.id
                      and s.reason = public._flow_engine_sequence_reason(p_legacy_kind))
       and not exists (select 1 from public.flow_engine_runs r where r.recovery_case_id = rc.id)
$function$;

-- La forma de los pasos de cada flujo del legado: la gemela de
-- flow_policy.STEPS_BY_LEGACY_KIND. pytest compara este literal con la de
-- Python, paso por paso.
create function public._flow_engine_legacy_steps(p_legacy_kind text)
returns jsonb
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    -- flow_engine_legacy_steps: begin
    select ($steps${"carrito": [{"step_key": "first_contact", "coupon_ordinal": 1, "variables": ["nombre", "producto"], "coupon_variables": ["nombre", "producto", "cuando", "precio", "vence"]}, {"step_key": "cart_second_touch", "coupon_ordinal": null, "variables": ["nombre", "producto"], "coupon_variables": null}, {"step_key": "cart_last_touch", "coupon_ordinal": 2, "variables": null, "coupon_variables": ["nombre", "producto", "vence"]}], "precheckout": [{"step_key": "first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto"], "coupon_variables": null}], "pago_fallido": [{"step_key": "payment_failure_first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto"], "coupon_variables": null}, {"step_key": "payment_failure_followup", "coupon_ordinal": 2, "variables": null, "coupon_variables": ["nombre", "producto", "vence"]}]}$steps$::jsonb) -> p_legacy_kind
    -- flow_engine_legacy_steps: end
$function$;

-- La revalidacion de mover a motor (contrato, 3.2, regla 3): las seis claves
-- exactas, la version y la huella de la activa, rules_version 2 o mas,
-- capabilities v1, sin bloqueos y el validador con sus seis claves.
create function public._flow_engine_revalidation_is_valid(
    p_revalidation jsonb, p_version integer, p_definition_sha256 text)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if jsonb_typeof(p_revalidation) is distinct from 'object' then
        return false;
    end if;
    if (select array_agg(k order by k) from jsonb_object_keys(p_revalidation) k)
       is distinct from array['blocks', 'capabilities', 'definition_sha256', 'rules_version', 'validator', 'version'] then
        return false;
    end if;
    return coalesce(
        jsonb_typeof(p_revalidation -> 'version') = 'number'
        and p_revalidation ->> 'version' = p_version::text
        and p_revalidation -> 'definition_sha256' = to_jsonb(p_definition_sha256)
        and jsonb_typeof(p_revalidation -> 'rules_version') = 'number'
        and p_revalidation ->> 'rules_version' ~ '^[1-9][0-9]{0,3}$'
        and (p_revalidation ->> 'rules_version')::integer >= 2
        and p_revalidation -> 'capabilities' = '"v1"'::jsonb
        and p_revalidation -> 'blocks' = '[]'::jsonb
        and public._flow_change_validator_is_valid(p_revalidation -> 'validator'), false);
end;
$function$;

-- ---------------------------------------------------------------------------
-- 4. La politica compilada: la gemela de flow_policy.compile_policy.
-- ---------------------------------------------------------------------------

-- Una duracion del esquema (0m, 60m, 24h, 1d) en minutos, o null.
create function public._flow_engine_duration_minutes(p_value jsonb)
returns integer
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select case when jsonb_typeof(p_value) = 'string' and p_value #>> '{}' ~ '^(0|[1-9][0-9]{0,4})(m|h|d)$'
        then substring(p_value #>> '{}' from '^([0-9]+)')::integer
             * case right(p_value #>> '{}', 1) when 'm' then 1 when 'h' then 60 else 1440 end
    end
$function$;

-- Una hora HH:MM del esquema en minutos desde la medianoche, o null.
create function public._flow_engine_hour_minutes(p_value jsonb)
returns integer
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select case when jsonb_typeof(p_value) = 'string' and p_value #>> '{}' ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'
        then substring(p_value #>> '{}' from 1 for 2)::integer * 60
             + substring(p_value #>> '{}' from 4 for 2)::integer
    end
$function$;

-- Lo que hace flow_policy.compile_policy, en el mismo orden y con los mismos
-- codigos (el message del raise, con 22023): el flow_id, la version, el
-- legacy_kind, el horario, los pasos y el vence. p_flow_id es texto, como en
-- Python, para que un id que no es un uuid de una el mismo error.
-- p_fixed_zone es la zona fija del flujo (la que toma un siempre).
create function public._flow_engine_compile_policy_core(
    p_definition jsonb, p_flow_id text, p_version integer, p_legacy_kind text, p_fixed_zone text)
returns public.followup_policy_versions
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    c_refuse constant text := 'flow_policy_not_compilable:';
    v_shape jsonb;
    v_horario jsonb;
    v_zone text;
    v_start integer;
    v_end integer;
    v_step jsonb;
    v_kind text;
    -- Las esperas se suman en bigint: 16 esperas de 99999d pasan el integer, y
    -- Python no tiene tope (los casos compartidos lo fijan).
    v_minutes bigint;
    v_waited bigint := 0;
    v_since_first bigint := 0;
    v_waited_after_send boolean := false;
    v_sends integer := 0;
    v_send_minutes bigint[] := array[]::bigint[];
    v_send_coupons integer[] := array[]::integer[];
    v_last_minutes bigint;
    v_slot jsonb;
    v_slot_key text;
    v_coupon jsonb;
    v_personal boolean;
    v_wanted jsonb;
    v_deadline integer;
    v_steps jsonb := '[]'::jsonb;
    v_compiled jsonb;
    v_hours bigint;
    v_row public.followup_policy_versions%rowtype;
begin
    if not coalesce(p_flow_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', false) then
        raise exception using errcode = '22023', message = 'compile_policy_flow_id_invalid';
    end if;
    if p_version is null or p_version < 1 then
        raise exception using errcode = '22023', message = 'compile_policy_version_invalid';
    end if;
    v_shape := public._flow_engine_legacy_steps(p_legacy_kind);
    if p_legacy_kind is null or jsonb_typeof(v_shape) is distinct from 'array' then
        raise exception using errcode = '22023', message = 'compile_policy_legacy_kind_without_step';
    end if;

    -- El horario (flow_schedule.franja_of).
    if jsonb_typeof(p_definition) is distinct from 'object'
       or (p_fixed_zone is not null
           and not exists (select 1 from pg_timezone_names tz where tz.name = p_fixed_zone)) then
        raise exception using errcode = '22023', message = 'compile_policy_horario_invalid';
    end if;
    v_horario := p_definition -> 'horario';
    if v_horario = '"siempre"'::jsonb then
        v_zone := p_fixed_zone;
        v_start := 0;
        v_end := 1439;
    else
        if jsonb_typeof(v_horario) is distinct from 'object'
           or (select array_agg(k order by k) from jsonb_object_keys(v_horario) k)
              is distinct from array['desde', 'hasta', 'zona'] then
            raise exception using errcode = '22023', message = 'compile_policy_horario_invalid';
        end if;
        v_start := public._flow_engine_hour_minutes(v_horario -> 'desde');
        v_end := public._flow_engine_hour_minutes(v_horario -> 'hasta');
        if jsonb_typeof(v_horario -> 'zona') is distinct from 'string'
           or not exists (select 1 from pg_timezone_names tz where tz.name = v_horario ->> 'zona')
           or v_start is null or v_end is null
           or (p_fixed_zone is not null and p_fixed_zone is distinct from v_horario ->> 'zona') then
            raise exception using errcode = '22023', message = 'compile_policy_horario_invalid';
        end if;
        v_zone := v_horario ->> 'zona';
    end if;

    -- Los pasos del unico camino que ejecuta la v1 (flow_policy._path).
    if jsonb_typeof(p_definition -> 'pasos') is distinct from 'array' then
        raise exception using errcode = '22023', message = 'compile_policy_without_send';
    end if;
    for v_step in select value from jsonb_array_elements(p_definition -> 'pasos') loop
        v_kind := null;
        if jsonb_typeof(v_step) = 'object' then
            select min(t.kind) into v_kind
              from unnest(array['esperar', 'plantilla', 'conversar', 'condicion', 'derivar']) t(kind)
             where v_step ? t.kind
            having count(*) = 1;
        end if;
        if v_kind = 'esperar' then
            v_minutes := public._flow_engine_duration_minutes(v_step -> 'esperar');
            if v_minutes is null then
                raise exception using errcode = '22023', message = 'compile_policy_wait_invalid';
            end if;
            if v_sends > 0 then
                v_since_first := v_since_first + v_minutes;
            else
                v_waited := v_waited + v_minutes;
            end if;
            v_waited_after_send := v_sends > 0;
            continue;
        end if;
        if v_kind is distinct from 'plantilla' then
            raise exception using errcode = '22023',
                message = 'compile_policy_step_not_supported:' || coalesce(v_kind, 'desconocido');
        end if;
        v_waited_after_send := false;
        if v_sends >= jsonb_array_length(v_shape) then
            raise exception using errcode = '22023', message = c_refuse || 'more_sends_than_steps';
        end if;
        v_slot := v_shape -> v_sends;
        v_slot_key := v_slot ->> 'step_key';
        -- Entre dos envios, al menos un minuto.
        v_last_minutes := case when v_sends > 1 then v_send_minutes[v_sends] else 0 end;
        if v_sends > 0 and v_since_first <= v_last_minutes then
            raise exception using errcode = '22023', message = c_refuse || 'espera_entre_envios:' || v_slot_key;
        end if;
        v_coupon := case when jsonb_typeof(v_step -> 'boton') = 'object' then v_step #> '{boton,cupon}' end;
        if v_coupon = 'null'::jsonb then
            v_coupon := null;
        end if;
        v_personal := coalesce(v_coupon = '"personal"'::jsonb, false);
        if v_personal then
            if jsonb_typeof(v_slot -> 'coupon_ordinal') is distinct from 'number' then
                raise exception using errcode = '22023', message = c_refuse || 'coupon_not_allowed:' || v_slot_key;
            end if;
        elsif v_coupon is not null then
            raise exception using errcode = '22023', message = c_refuse || 'fixed_coupon:' || v_slot_key;
        elsif jsonb_typeof(v_slot -> 'coupon_ordinal') = 'number' and (v_slot ->> 'coupon_ordinal')::integer > 1 then
            raise exception using errcode = '22023', message = c_refuse || 'coupon_missing:' || v_slot_key;
        end if;
        v_wanted := case when v_personal then v_slot -> 'coupon_variables' else v_slot -> 'variables' end;
        if jsonb_typeof(v_step -> 'variables') is distinct from 'array'
           or jsonb_typeof(v_wanted) is distinct from 'array'
           or v_step -> 'variables' is distinct from v_wanted then
            raise exception using errcode = '22023', message = c_refuse || 'variables_differ:' || v_slot_key;
        end if;
        v_sends := v_sends + 1;
        v_send_minutes := v_send_minutes || case when v_sends > 1 then v_since_first else v_waited end;
        v_send_coupons := v_send_coupons
            || case when v_personal then (v_slot ->> 'coupon_ordinal')::integer end;
    end loop;
    if v_sends = 0 then
        raise exception using errcode = '22023', message = 'compile_policy_without_send';
    end if;
    if v_waited_after_send then
        raise exception using errcode = '22023', message = 'compile_policy_step_after_send';
    end if;

    v_deadline := case when jsonb_typeof(p_definition -> 'termina') = 'object'
        then public._flow_engine_duration_minutes(p_definition #> '{termina,vence}') end;
    if v_deadline is null or v_deadline < 1 then
        raise exception using errcode = '22023', message = 'compile_policy_vence_invalid';
    end if;

    -- Un paso por envio; desde el segundo, el delay desde el primer envio (el
    -- texto del legado), approved_template y el cupon de ordinal 2 o mas.
    for i in 1 .. v_sends loop
        v_compiled := jsonb_build_object('mode', 'freeform', 'step_key', v_shape -> (i - 1) ->> 'step_key');
        if i > 1 then
            v_minutes := v_send_minutes[i];
            v_hours := v_minutes / 60;
            v_compiled := v_compiled || jsonb_build_object(
                'delay', case
                    when v_minutes % 60 = 0 then v_hours::text || case when v_hours = 1 then ' hour' else ' hours' end
                    else v_minutes::text || case when v_minutes = 1 then ' minute' else ' minutes' end end,
                'approved_template', true);
            if v_send_coupons[i] is not null and v_send_coupons[i] > 1 then
                v_compiled := v_compiled || jsonb_build_object('coupon_ordinal', v_send_coupons[i]);
            end if;
        end if;
        v_steps := v_steps || jsonb_build_array(v_compiled);
    end loop;

    v_row.policy_key := 'flujo:' || p_flow_id;
    v_row.version := p_version;
    -- La admision (T2.3b) la inserta publicada, con su aprobacion.
    v_row.status := 'published';
    v_row.purpose := 'cart_recovery';
    v_row.timezone := coalesce(v_zone, 'UTC');
    v_row.business_windows := jsonb_build_array(jsonb_build_object(
        'days', jsonb_build_array(1, 2, 3, 4, 5, 6, 7),
        'start', lpad((v_start / 60)::text, 2, '0') || ':' || lpad((v_start % 60)::text, 2, '0'),
        'end', lpad((v_end / 60)::text, 2, '0') || ':' || lpad((v_end % 60)::text, 2, '0')));
    v_row.grace_period := v_send_minutes[1] * interval '1 minute';
    v_row.expires_after := v_deadline * interval '1 minute';
    v_row.max_automatic_messages := v_sends;
    v_row.steps := v_steps;
    return v_row;
end;
$function$;

-- La zona fija de un flujo: la del primer horario objeto que tuvo
-- (invariante 4 del almacen), o null.
create function public._flow_engine_fixed_zone(p_flow_id uuid)
returns text
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select d.definition #>> '{horario,zona}'
      from public.flow_definitions d
     where d.flow_id = p_flow_id and jsonb_typeof(d.definition -> 'horario') = 'object'
     order by d.version
     limit 1
$function$;

-- La firma del diseno (§2.2): la zona fija sale del almacen para ese flujo.
create function public._flow_engine_compile_policy(
    p_definition jsonb, p_flow_id uuid, p_version integer, p_legacy_kind text)
returns public.followup_policy_versions
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select public._flow_engine_compile_policy_core(
        p_definition, p_flow_id::text, p_version, p_legacy_kind, public._flow_engine_fixed_zone(p_flow_id))
$function$;

-- ---------------------------------------------------------------------------
-- 5. Los disparadores de las tablas.
-- ---------------------------------------------------------------------------

create function public.reject_flow_engine_mutation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    raise exception using errcode = '55000', message = case tg_table_name
        when 'flow_engine_routes' then 'flow_route_is_permanent'
        when 'flow_engine_runs' then 'flow_engine_run_is_append_only'
        when 'flow_engine_request_authorizations' then 'flow_engine_authorization_is_append_only'
        else 'flow_engine_record_is_permanent'
    end;
end;
$function$;

-- Una ruta nace en legado, con generacion 0, sobre un flujo con version
-- activa cuyo disparador corresponde al legacy_kind, y con el scope del
-- legado existente.
create function public.validate_flow_engine_route_insert()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_control public.flow_controls%rowtype;
    v_definition jsonb;
begin
    if not coalesce(new.executor = 'legado' and new.generation = 0, false) then
        raise exception using errcode = '23514', message = 'flow_route_must_start_in_legacy';
    end if;
    select * into v_control from public.flow_controls c where c.flow_id = new.flow_id;
    if not found or v_control.active_version is null then
        raise exception using errcode = '23514', message = 'flow_route_needs_published_version';
    end if;
    select d.definition into v_definition from public.flow_definitions d
     where d.flow_id = new.flow_id and d.version = v_control.active_version;
    if not public._flow_engine_kind_matches(new.legacy_kind, v_definition) then
        raise exception using errcode = '23514', message = 'flow_route_kind_mismatch';
    end if;
    if new.legacy_scope_key is not null
       and not public._flow_engine_legacy_scope_exists(v_control.tenant_ref, new.legacy_scope_key) then
        raise exception using errcode = '23514', message = 'flow_route_legacy_scope_unknown';
    end if;
    return new;
end;
$function$;

-- legacy_kind y legacy_scope_key no cambian nunca; todo cambio mueve el
-- ejecutor y sube la generacion en 1.
create function public.validate_flow_engine_route_update()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if row(new.flow_id, new.legacy_kind, new.legacy_scope_key, new.created_at)
       is distinct from row(old.flow_id, old.legacy_kind, old.legacy_scope_key, old.created_at) then
        raise exception using errcode = '55000', message = 'flow_route_legacy_is_immutable';
    end if;
    if new.generation is distinct from old.generation + 1 then
        raise exception using errcode = '23514', message = 'flow_route_generation_must_increment';
    end if;
    if new.executor is not distinct from old.executor then
        raise exception using errcode = '23514', message = 'flow_route_executor_unchanged';
    end if;
    return new;
end;
$function$;

-- Todo cambio de una ruta deja su evento en el almacen.
create function public.record_flow_engine_route_change()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_control public.flow_controls%rowtype;
    v_event text := case when tg_op = 'INSERT' then 'flow_route_created' else 'flow_route_moved' end;
begin
    select * into strict v_control from public.flow_controls c where c.flow_id = new.flow_id;
    insert into public.flow_change_events (
        tenant_ref, ally_ref, flow_id, event_type, actor_kind, actor_ref, reason_code, generation, data)
    values (
        v_control.tenant_ref, v_control.ally_ref, new.flow_id, v_event, 'operador', new.changed_by_ref,
        v_event, new.generation,
        jsonb_build_object(
            'legacy_kind', new.legacy_kind,
            'legacy_scope_key', new.legacy_scope_key,
            'from_executor', case when tg_op = 'UPDATE' then old.executor end,
            'to_executor', new.executor,
            'from_generation', case when tg_op = 'UPDATE' then old.generation end,
            'to_generation', new.generation,
            'change_reason_sha256', encode(sha256(convert_to(new.change_reason, 'UTF8')), 'hex')));
    return null;
end;
$function$;

create trigger flow_engine_routes_validate
before insert on public.flow_engine_routes
for each row execute function public.validate_flow_engine_route_insert();
create trigger flow_engine_routes_immutable
before update on public.flow_engine_routes
for each row execute function public.validate_flow_engine_route_update();
create trigger flow_engine_routes_record_change
after insert or update on public.flow_engine_routes
for each row execute function public.record_flow_engine_route_change();
create trigger flow_engine_routes_permanent
before delete on public.flow_engine_routes
for each row execute function public.reject_flow_engine_mutation();
create trigger flow_engine_routes_no_truncate
before truncate on public.flow_engine_routes
for each statement execute function public.reject_flow_engine_mutation();

create trigger flow_engine_runs_append_only
before update or delete on public.flow_engine_runs
for each row execute function public.reject_flow_engine_mutation();
create trigger flow_engine_runs_no_truncate
before truncate on public.flow_engine_runs
for each statement execute function public.reject_flow_engine_mutation();
create trigger flow_engine_request_authorizations_append_only
before update or delete on public.flow_engine_request_authorizations
for each row execute function public.reject_flow_engine_mutation();
create trigger flow_engine_request_authorizations_no_truncate
before truncate on public.flow_engine_request_authorizations
for each statement execute function public.reject_flow_engine_mutation();
create trigger flow_engine_start_rejections_permanent
before delete on public.flow_engine_start_rejections
for each row execute function public.reject_flow_engine_mutation();
create trigger flow_engine_start_rejections_no_truncate
before truncate on public.flow_engine_start_rejections
for each statement execute function public.reject_flow_engine_mutation();
create trigger flow_engine_divergences_permanent
before delete on public.flow_engine_divergences
for each row execute function public.reject_flow_engine_mutation();
create trigger flow_engine_divergences_no_truncate
before truncate on public.flow_engine_divergences
for each statement execute function public.reject_flow_engine_mutation();

-- ---------------------------------------------------------------------------
-- 6. La guarda del almacen sobre el legado (principio 7, la parte legado).
-- ---------------------------------------------------------------------------

-- Devuelve null para seguir como hoy, o el motivo del rechazo. Toma la ruta y
-- despues el control del flujo, FOR SHARE los dos (el orden de §2.4): un
-- cambio de ruta o una pausa que esta en curso espera a que termine el
-- arranque, y al reves.
create function public._flow_engine_legacy_guard(p_scope_key text, p_legacy_kind text)
returns text
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_route public.flow_engine_routes%rowtype;
    v_state text;
begin
    if p_scope_key is null or p_legacy_kind is null then
        return null;
    end if;
    select r.* into v_route from public.flow_engine_routes r
     where r.legacy_scope_key = p_scope_key and r.legacy_kind = p_legacy_kind
     for share;
    if not found then
        return null;
    end if;
    select c.state into v_state from public.flow_controls c where c.flow_id = v_route.flow_id for share;
    if v_route.executor = 'motor' then
        return 'flow_engine_not_available';
    end if;
    return case v_state
        when 'pausado' then 'flujo_pausado'
        when 'apagado' then 'flujo_apagado'
    end;
end;
$function$;

-- La misma guarda para un arranque: el scope sale del binding del caso de la
-- accion. Sin binding, sigue como hoy (el arranque lo rechaza el mismo). Un
-- intento ya autorizado (su fila en pilot_outbound_request_authorizations, la
-- que escribe authorize_lancemos_pilot_request_start para los tres arranques)
-- es un replay: la autorizacion lo devuelve con replayed = true antes de mirar
-- el runtime, asi que la guarda tampoco lo frena. El envio ya cruzo.
create function public._flow_engine_legacy_guard_action(
    p_action_id uuid, p_attempt_id uuid, p_legacy_kind text)
returns text
language sql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case
        when exists (
            select 1 from public.pilot_outbound_request_authorizations authrow
             where authrow.attempt_id = p_attempt_id and authrow.action_id = p_action_id)
        then null
        else public._flow_engine_legacy_guard((
            select b.scope_key
              from public.scheduled_actions a
              join public.pilot_recovery_case_bindings b on b.recovery_case_id = a.recovery_case_id
             where a.id = p_action_id), p_legacy_kind)
    end
$function$;

-- La insercion, sobre la definicion vigente de cada funcion.
do $guard$
declare
    c_plan_anchor constant text :=
        E'            message = ''invalid_pilot_plan_parameters'';\n    end if;\n';
    c_mark_anchor constant text :=
        E'            message = ''invalid_pilot_request_start_parameters'';\n    end if;\n';
    c_total_anchor constant text :=
        E'    select count(*)::integer into v_total\n'
        || E'    from public.pilot_outbound_request_authorizations authrow\n'
        || E'    where authrow.scope_key = p_scope_key;\n';
    c_daily_anchor constant text :=
        E'    select count(*)::integer into v_daily\n'
        || E'    from public.pilot_outbound_request_authorizations authrow\n'
        || E'    where authrow.scope_key = p_scope_key\n'
        || E'      and authrow.local_budget_date = v_local_date;\n';
    v_signatures text[];
    v_anchors text[];
    v_replacements text[];
    v_markers text[];
    v_definition text;
    v_signature text;
    i integer;
begin
    v_signatures := array[
        'public.plan_lancemos_pilot_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)',
        'public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)',
        'public.plan_portable_payment_slip_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)',
        'public.mark_lancemos_pilot_request_started(uuid,uuid,text,bigint,timestamptz)',
        'public.mark_portable_payment_failure_request_started(uuid,uuid,text,bigint,timestamptz)',
        'public.mark_portable_payment_slip_request_started(uuid,uuid,text,bigint,timestamptz)',
        'public.authorize_lancemos_pilot_request_start(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid,uuid,uuid,timestamptz)'
    ];
    v_anchors := array[c_plan_anchor, c_plan_anchor, c_plan_anchor,
                       c_mark_anchor, c_mark_anchor, c_mark_anchor, c_total_anchor];
    v_markers := array[
        'flow_engine_legacy_guard: begin', 'flow_engine_legacy_guard: begin', 'flow_engine_legacy_guard: begin',
        'flow_engine_legacy_guard: begin', 'flow_engine_legacy_guard: begin', 'flow_engine_legacy_guard: begin',
        'flow_engine_shared_pool: begin'];
    v_replacements := array[]::text[];
    -- Los tres planificadores: rechazan como pilot_runtime_not_armed al planificar.
    for i in 1 .. 3 loop
        v_replacements := v_replacements || (v_anchors[i]
            || E'\n'
            || E'    -- flow_engine_legacy_guard: begin\n'
            || E'    -- La guarda del almacen (20261021000200): con la ruta de este flujo\n'
            || E'    -- en legado y el flujo pausado o apagado, no planifica. Va por\n'
            || E'    -- legacy_kind y antes del control del scope (el orden de los locks).\n'
            || E'    declare\n'
            || E'        v_flow_engine_stop text;\n'
            || E'    begin\n'
            || E'        v_flow_engine_stop := public._flow_engine_legacy_guard(p_scope_key, '
            || quote_literal((array['carrito', 'pago_fallido', 'ficha'])[i]) || E');\n'
            || E'        if v_flow_engine_stop is not null then\n'
            || E'            raise exception using\n'
            || E'                errcode = ''55000'',\n'
            || E'                message = ''pilot_scope_rejected'',\n'
            || E'                detail = v_flow_engine_stop;\n'
            || E'        end if;\n'
            || E'    end;\n'
            || E'    -- flow_engine_legacy_guard: end\n');
    end loop;
    -- Los tres arranques: rechazan y el bridge reintenta en el lease siguiente.
    for i in 1 .. 3 loop
        v_replacements := v_replacements || (v_anchors[i + 3]
            || E'\n'
            || E'    -- flow_engine_legacy_guard: begin\n'
            || E'    -- La guarda del almacen (20261021000200): con la ruta de este flujo\n'
            || E'    -- en legado y el flujo pausado o apagado, no arranca (se reintenta).\n'
            || E'    -- Va por legacy_kind y antes del control del scope.\n'
            || E'    declare\n'
            || E'        v_flow_engine_stop text;\n'
            || E'    begin\n'
            || E'        v_flow_engine_stop := public._flow_engine_legacy_guard_action(p_action_id, p_attempt_id, '
            || quote_literal((array['carrito', 'pago_fallido', 'ficha'])[i]) || E');\n'
            || E'        if v_flow_engine_stop is not null then\n'
            || E'            raise exception using\n'
            || E'                errcode = ''55000'',\n'
            || E'                message = ''pilot_request_start_rejected'',\n'
            || E'                detail = v_flow_engine_stop;\n'
            || E'        end if;\n'
            || E'    end;\n'
            || E'    -- flow_engine_legacy_guard: end\n');
    end loop;
    -- La bolsa contada por los dos caminos (principio 12): el total y el dia
    -- suman las autorizaciones del motor con pool = scope_key. Hoy no hay
    -- ninguna, asi que no cambia nada.
    v_replacements := v_replacements || (
        E'    -- flow_engine_shared_pool: begin\n'
        || E'    -- La bolsa la cuentan los dos caminos (20261021000200): el total y el\n'
        || E'    -- dia suman las autorizaciones del motor con pool = scope_key.\n'
        || E'    select (select count(*)::integer\n'
        || E'              from public.pilot_outbound_request_authorizations authrow\n'
        || E'             where authrow.scope_key = p_scope_key)\n'
        || E'           + (select count(*)::integer\n'
        || E'                from public.flow_engine_request_authorizations engine_row\n'
        || E'               where engine_row.pool = p_scope_key)\n'
        || E'      into v_total;\n');

    for i in 1 .. array_length(v_signatures, 1) loop
        v_signature := v_signatures[i];
        if to_regprocedure(v_signature) is null then
            raise exception using errcode = '55000', message = 'flow_engine_guard_function_missing',
                detail = v_signature;
        end if;
        -- El duenio: create or replace no cambia de duenio, y otro duenio
        -- haria que la funcion corriera con otros permisos (o no se pudiera
        -- reemplazar). La migracion la aplica el duenio de las siete.
        if (select p.proowner from pg_proc p where p.oid = v_signature::regprocedure)
           is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
            raise exception using errcode = '55000', message = 'flow_engine_guard_owner_mismatch',
                detail = v_signature;
        end if;
        v_definition := pg_get_functiondef(v_signature::regprocedure);
        if length(v_definition) - length(replace(v_definition, v_anchors[i], '')) <> length(v_anchors[i])
           or position(v_markers[i] in v_definition) > 0 then
            raise exception using errcode = '55000', message = 'unexpected_flow_engine_guard_definition',
                detail = v_signature;
        end if;
        v_definition := replace(v_definition, v_anchors[i], v_replacements[i]);
        -- La bolsa: tambien el dia, con su propia ancla.
        if v_markers[i] = 'flow_engine_shared_pool: begin' then
            if length(v_definition) - length(replace(v_definition, c_daily_anchor, '')) <> length(c_daily_anchor) then
                raise exception using errcode = '55000', message = 'unexpected_flow_engine_guard_definition',
                    detail = v_signature;
            end if;
            v_definition := replace(v_definition, c_daily_anchor,
                E'    select (select count(*)::integer\n'
                || E'              from public.pilot_outbound_request_authorizations authrow\n'
                || E'             where authrow.scope_key = p_scope_key\n'
                || E'               and authrow.local_budget_date = v_local_date)\n'
                || E'           + (select count(*)::integer\n'
                || E'                from public.flow_engine_request_authorizations engine_row\n'
                || E'               where engine_row.pool = p_scope_key\n'
                || E'                 and engine_row.local_budget_date = v_local_date)\n'
                || E'      into v_daily;\n'
                || E'    -- flow_engine_shared_pool: end\n');
        end if;
        execute v_definition;
        if position(v_markers[i] in pg_get_functiondef(v_signature::regprocedure)) = 0 then
            raise exception using errcode = '55000', message = 'flow_engine_guard_definition_not_replaced',
                detail = v_signature;
        end if;
    end loop;
end;
$guard$;

-- ---------------------------------------------------------------------------
-- 7. Las lecturas del contrato.
-- ---------------------------------------------------------------------------

-- Que ejecuta el motor (contrato, seccion 1), de a 50 flujos por
-- (product_ref, flow_key). p_after es el next de la pagina anterior:
-- '<product_ref>/<flow_key>' del ultimo flujo que devolvio.
create function public.read_flow_engine_v1(p_instance jsonb, p_after text default null)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    c_page constant integer := 50;
    v_instance record;
    v_after_product text;
    v_after_key text;
    v_flows jsonb := '[]'::jsonb;
    v_count integer := 0;
    v_next text;
    v_control public.flow_controls%rowtype;
    v_route public.flow_engine_routes%rowtype;
    v_versions jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_after is not null then
        if not coalesce(p_after ~ '^[a-z0-9][a-z0-9-]{0,127}/[a-z][a-z0-9_]{1,39}$', false) then
            raise exception using errcode = '22023', message = 'flow_engine_after_invalid';
        end if;
        v_after_product := split_part(p_after, '/', 1);
        v_after_key := split_part(p_after, '/', 2);
    end if;
    for v_control in
        select c.* from public.flow_controls c
         where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
           and (p_after is null or (c.product_ref, c.flow_key) > (v_after_product, v_after_key))
         order by c.product_ref, c.flow_key
         limit c_page + 1
    loop
        v_count := v_count + 1;
        if v_count > c_page then
            exit;
        end if;
        v_next := v_control.product_ref || '/' || v_control.flow_key;
        v_route := null;
        select r.* into v_route from public.flow_engine_routes r where r.flow_id = v_control.flow_id;
        -- La activa y toda version con recorridos del motor abiertos. Con la
        -- ruta en legado, lo en curso del legado va a la activa.
        select coalesce(jsonb_agg(jsonb_build_object(
                   'version', d.version,
                   'definition', d.definition,
                   'definition_sha256', d.definition_sha256,
                   'published_at', to_char(d.published_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                   'pool', coalesce(d.definition #>> '{topes,bolsa}', v_route.legacy_scope_key),
                   'in_flight', x.motor + case
                       when v_route.executor = 'legado' and d.version = v_control.active_version
                       then public._flow_engine_legacy_in_flight(v_route.legacy_scope_key, v_route.legacy_kind)
                       else 0 end)
                   order by d.version), '[]'::jsonb)
          into v_versions
          from public.flow_definitions d
          cross join lateral (
              select count(*)::integer as motor
                from public.flow_engine_runs r
                join public.recovery_cases rc on rc.id = r.recovery_case_id
               where r.flow_id = d.flow_id and r.version = d.version
                 and rc.status in ('grace_period', 'active', 'paused')) x
         where d.flow_id = v_control.flow_id
           and (d.version = v_control.active_version or x.motor > 0);
        v_flows := v_flows || jsonb_build_array(jsonb_build_object(
            'flow_id', v_control.flow_id,
            'product_ref', v_control.product_ref,
            'flow_key', v_control.flow_key,
            'state', v_control.state,
            'active_version', v_control.active_version,
            'generation', v_control.generation,
            'route', public._flow_engine_route_json(v_route),
            'versions', v_versions));
    end loop;
    return jsonb_build_object(
        'contract', 'flow-engine-v1',
        'contract_version', '1.0.0',
        'database_at', to_char(now() at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'engine', jsonb_build_object('capabilities', 'v1', 'rules_version', 2),
        'flows', v_flows,
        'next', case when v_count > c_page then to_jsonb(v_next) else 'null'::jsonb end);
end;
$function$;

-- La historia para simular (contrato, seccion 2): la captura v3 restringida a
-- la instancia, al producto y a p_since. La siembra es p_since y la ventana
-- empieza un dia despues; p_since no puede ser anterior a 8 dias. El scope del
-- carrito es el de la ruta carrito del producto. Las horas salen en UTC (la
-- forma las pide asi), y la sal de las claves de persona es aleatoria por
-- llamada y no sale.
create function public.read_flow_engine_history_v1(p_instance jsonb, p_product_ref text, p_since timestamptz)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
set timezone = 'UTC'
as $function$
declare
    c_max_bytes constant integer := 1000000;
    v_instance record;
    v_tenant_ref text;
    v_cart_scope_key text;
    v_product_ids text[];
    v_hotlinks text[];
    v_scope_keys text[];
    v_policy_keys text[];
    v_capture jsonb;
begin
    -- La captura lee tablas grandes: un tope propio, local a la transaccion.
    perform set_config('statement_timeout', '20s', true);
    v_instance := public._flow_store_instance(p_instance);
    v_tenant_ref := v_instance.o_tenant_ref;
    if not coalesce(p_product_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$', false) then
        raise exception using errcode = '22023', message = 'flow_store_flow_invalid';
    end if;
    if not public._flow_store_product_is_bound(v_tenant_ref, v_instance.o_ally_ref, p_product_ref) then
        raise exception using errcode = '22023', message = 'flow_store_product_not_bound';
    end if;
    if p_since is null or p_since > clock_timestamp() then
        raise exception using errcode = '22023', message = 'flow_engine_history_since_invalid';
    end if;
    if p_since < clock_timestamp() - interval '8 days' then
        raise exception using errcode = '22023', message = 'flow_engine_history_window_too_wide';
    end if;
    select r.legacy_scope_key into v_cart_scope_key
      from public.flow_engine_routes r
      join public.flow_controls c on c.flow_id = r.flow_id
     where c.tenant_ref = v_tenant_ref and c.ally_ref = v_instance.o_ally_ref
       and c.product_ref = p_product_ref and r.legacy_kind = 'carrito';
    if v_cart_scope_key is null then
        raise exception using errcode = 'P0002', message = 'flow_engine_history_cart_route_not_found';
    end if;
    select coalesce(array_agg(distinct b.hotmart_product_id::text), array[]::text[]),
           coalesce(array_agg(distinct b.product_hotlink), array[]::text[])
      into v_product_ids, v_hotlinks
      from public.commercial_ally_runtime_bindings b
     where b.tenant_ref = v_tenant_ref and b.ally_ref = v_instance.o_ally_ref and b.funnel_ref = p_product_ref;
    select coalesce(array_agg(distinct s.scope_key), array[]::text[]) || array[v_cart_scope_key]
      into v_scope_keys
      from public.pilot_scope_versions s
     where s.tenant_key = v_tenant_ref
       and (s.external_product_id = any(v_product_ids) or s.external_product_id = any(v_hotlinks));
    select coalesce(array_agg(distinct s.policy_key), array[]::text[])
           || coalesce((select array_agg('flujo:' || c.flow_id::text)
                          from public.flow_controls c
                         where c.tenant_ref = v_tenant_ref and c.ally_ref = v_instance.o_ally_ref
                           and c.product_ref = p_product_ref), array[]::text[])
      into v_policy_keys
      from public.pilot_scope_versions s
     where s.scope_key = any(v_scope_keys);

    v_capture := (
    -- flow_engine_history_query: begin
with v as (
    -- read_flow_engine_history_v1: los parametros de la funcion en lugar de
    -- los CTE p, w y v de la consulta v3. La siembra es p_since y la ventana
    -- empieza un dia despues; la sal es aleatoria por llamada y no sale.
    select clock_timestamp() as captured_at,
           gen_random_uuid()::text as sal,
           v_cart_scope_key as scope_key,
           p_since + interval '1 day' as desde,
           p_since as siembra,
           v_product_ids as product_ids,
           v_scope_keys as scope_keys,
           v_policy_keys as policy_keys,
           v_tenant_ref as tenant_ref
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
       and e.payload #>> '{data,product,id}' = any(v.product_ids)
),
ev as (
    select ev0.*, public._whatsapp_phone_canonical(ev0.phone_raw) as phone_c from ev0
),
-- Todos los casos de Hotmart de la ventana o en curso: los del carrito y, como
-- hechos de la persona, los del pago rechazado.
cs as (
    select rc.* from public.recovery_cases rc, v
     where rc.source = 'hotmart'
       and rc.external_product_id = any(v.product_ids)
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
       and x.scope_key = any(v.scope_keys)
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
                    union select a.contact_id from public.pilot_outbound_request_authorizations a
                           where a.scope_key = any((select v.scope_keys from v)::text[]))
)
select json_build_object(
  '_captured', json_build_object(
      'origin', 'read_flow_engine_history_v1 (flow-engine-v1)',
      'query', 'captura-paridad-recuperacion-v3',
      'captured_at', (select captured_at from v),
      'desde', (select desde from v),
      'desde_origen', (select 'read_flow_engine_history_v1:' || scope_key || ':p_since+1d' from v),
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
          'autorizaciones', (select count(*) from public.pilot_outbound_request_authorizations a
                              where a.scope_key = any((select v.scope_keys from v)::text[])),
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
          order by s.scope_key, s.version), '[]'::json) from public.pilot_scope_versions s
         where s.scope_key = any((select v.scope_keys from v)::text[])),
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
          order by f.policy_key, f.version), '[]'::json) from public.followup_policy_versions f
         where f.policy_key = any((select v.policy_keys from v)::text[])),
      'pilot_runtime_controls', (select coalesce(json_agg(json_build_object(
              'scope_key', c.scope_key, 'scope_version', c.scope_version,
              'runtime_state', c.runtime_state, 'generation', c.generation,
              'changed_at', c.changed_at)
          order by c.scope_key), '[]'::json) from public.pilot_runtime_controls c
         where c.scope_key = any((select v.scope_keys from v)::text[])),
      'pilot_proactive_contact_caps', (select coalesce(json_agg(json_build_object(
              'tenant_key', k.tenant_key, 'max_request_starts', k.max_request_starts,
              'request_window', k.request_window)), '[]'::json)
          from public.pilot_proactive_contact_caps k
         where k.tenant_key = (select v.tenant_ref from v)),
      'pilot_control_events_control_level', (select coalesce(json_agg(json_build_object(
              'scope_key', e.scope_key, 'scope_version', e.scope_version,
              'event_type', e.event_type, 'runtime_generation', e.runtime_generation,
              'reason_code', case when e.reason_code ~ '^[a-z0-9_:]{1,80}$' then e.reason_code else 'otro' end,
              'to_state', case when (e.data ->> 'to_state') ~ '^[a-z][a-z_]{0,31}$'
                               then e.data ->> 'to_state' end,
              'created_at', e.created_at)
          order by e.created_at, e.runtime_generation, e.id), '[]'::json)
          from public.pilot_control_events e
         where e.contact_id is null and e.action_id is null and e.attempt_id is null
           and e.scope_key = any((select v.scope_keys from v)::text[]))),
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
      from public.hotmart_purchase_semantic_conflicts k
     where exists (select 1 from public.webhook_events we
                    where we.id = k.existing_event_id
                      and we.payload #>> '{data,product,id}' = any((select v.product_ids from v)::text[]))),
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
      ) order by a.authorized_at, a.id), '[]'::json) from public.pilot_outbound_request_authorizations a
     where a.scope_key = any((select v.scope_keys from v)::text[])),
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
)
    -- flow_engine_history_query: end
    )::jsonb;
    if octet_length(v_capture::text) > c_max_bytes then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_engine_history_too_large');
    end if;
    return v_capture;
end;
$function$;

-- ---------------------------------------------------------------------------
-- 8. Las escrituras del contrato: solo del duenio, security invoker.
-- ---------------------------------------------------------------------------

-- Crear la ruta, siempre en legado (contrato, 3.1). La corre la migracion de
-- cada flujo al registrarlo y publicar su v1. El evento flow_route_created lo
-- deja el disparador.
create function public.operator_create_flow_route_v1(
    p_instance jsonb, p_product_ref text, p_flow_key text, p_legacy_kind text,
    p_legacy_scope_key text, p_actor_name text, p_reason text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_control public.flow_controls%rowtype;
    v_definition jsonb;
    v_route public.flow_engine_routes%rowtype;
begin
    -- El lock_timeout va con set_config local y no en la firma, como en
    -- 20261021000100: la lista de las funciones del almacen con lock_timeout
    -- en la firma la fija validate_flow_store.mjs.
    perform set_config('lock_timeout', '5s', true);
    v_instance := public._flow_store_instance(p_instance);
    if not coalesce(p_product_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'
                    and p_flow_key ~ '^[a-z][a-z0-9_]{1,39}$', false) then
        raise exception using errcode = '22023', message = 'flow_store_flow_invalid';
    end if;
    if not coalesce(p_legacy_kind in ('carrito', 'pago_fallido', 'ficha', 'precheckout', 'descuento', 'reactivacion'),
                    false) then
        raise exception using errcode = '22023', message = 'flow_route_legacy_kind_invalid';
    end if;
    if not coalesce(case when p_legacy_kind in ('descuento', 'reactivacion') then p_legacy_scope_key is null
                         else p_legacy_scope_key ~ '^[a-z0-9][a-z0-9_-]{0,127}$' end, false) then
        raise exception using errcode = '22023', message = 'flow_route_legacy_scope_invalid';
    end if;
    if not coalesce(public._flow_store_actor_is_valid('operador', p_actor_name), false) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    select * into v_control from public.flow_controls c
     where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
       and c.product_ref = p_product_ref and c.flow_key = p_flow_key
     for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_not_found';
    end if;
    -- En la v1, solo los legacy_kind que la guarda frena en el legado: una
    -- ruta de otro haria creer que pausarlo en el almacen lo frena.
    if p_legacy_kind not in ('carrito', 'pago_fallido', 'ficha') then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_route_kind_not_guarded');
    end if;
    if v_control.active_version is null then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_route_needs_published_version');
    end if;
    select d.definition into v_definition from public.flow_definitions d
     where d.flow_id = v_control.flow_id and d.version = v_control.active_version;
    if not public._flow_engine_kind_matches(p_legacy_kind, v_definition) then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_route_kind_mismatch');
    end if;
    if p_legacy_scope_key is not null
       and not public._flow_engine_legacy_scope_exists(v_instance.o_tenant_ref, p_legacy_scope_key) then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_route_legacy_scope_unknown');
    end if;
    insert into public.flow_engine_routes (flow_id, executor, legacy_kind, legacy_scope_key, generation,
        changed_by_ref, change_reason)
    values (v_control.flow_id, 'legado', p_legacy_kind, p_legacy_scope_key, 0, p_actor_name, p_reason)
    on conflict do nothing
    returning * into v_route;
    if v_route.flow_id is null then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_route_already_exists');
    end if;
    return jsonb_build_object('ok', true, 'route', public._flow_engine_route_json(v_route));
end;
$function$;

-- Mover la ruta (contrato, 3.2). Toma la ruta FOR UPDATE y despues el control
-- FOR SHARE. La generacion esperada es el CAS. Lo en curso no se mueve. El
-- evento flow_route_moved lo deja el disparador.
create function public.operator_route_flow_v1(
    p_instance jsonb, p_product_ref text, p_flow_key text, p_executor text,
    p_expected_generation bigint, p_actor_name text, p_reason text, p_revalidation jsonb)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_flow_id uuid;
    v_route public.flow_engine_routes%rowtype;
    v_control public.flow_controls%rowtype;
    v_sha text;
begin
    -- El lock_timeout va con set_config local y no en la firma, como en
    -- 20261021000100: la lista de las funciones del almacen con lock_timeout
    -- en la firma la fija validate_flow_store.mjs.
    perform set_config('lock_timeout', '5s', true);
    v_instance := public._flow_store_instance(p_instance);
    if not coalesce(p_product_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'
                    and p_flow_key ~ '^[a-z][a-z0-9_]{1,39}$', false) then
        raise exception using errcode = '22023', message = 'flow_store_flow_invalid';
    end if;
    if not coalesce(p_executor in ('legado', 'motor'), false) then
        raise exception using errcode = '22023', message = 'flow_route_executor_invalid';
    end if;
    if p_expected_generation is null then
        raise exception using errcode = '22023', message = 'flow_route_generation_invalid';
    end if;
    if not coalesce(public._flow_store_actor_is_valid('operador', p_actor_name), false) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    select c.flow_id into v_flow_id from public.flow_controls c
     where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
       and c.product_ref = p_product_ref and c.flow_key = p_flow_key;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_not_found';
    end if;
    -- 1. La ruta, 2. el control (§2.4).
    select r.* into v_route from public.flow_engine_routes r where r.flow_id = v_flow_id for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_route_not_found';
    end if;
    select c.* into v_control from public.flow_controls c where c.flow_id = v_flow_id for share;
    if v_route.generation is distinct from p_expected_generation then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_route_generation_stale',
            'generation', v_route.generation);
    end if;
    if v_route.executor = p_executor then
        return jsonb_build_object('ok', true, 'unchanged', true);
    end if;
    if p_executor = 'motor' then
        if not coalesce(v_route.legacy_kind = any(public._flow_engine_route_kinds()), false) then
            return jsonb_build_object('ok', false, 'reason_code', 'flow_route_kind_not_supported');
        end if;
        if v_control.state = 'apagado' then
            return jsonb_build_object('ok', false, 'reason_code', 'flow_route_flow_off');
        end if;
        select d.definition_sha256 into v_sha from public.flow_definitions d
         where d.flow_id = v_flow_id and d.version = v_control.active_version;
        if not public._flow_engine_revalidation_is_valid(p_revalidation, v_control.active_version, v_sha) then
            return jsonb_build_object('ok', false, 'reason_code', 'flow_route_revalidation_invalid');
        end if;
        if exists (select 1 from public.pilot_proactive_contact_caps k
                    where k.tenant_key = v_instance.o_tenant_ref) then
            return jsonb_build_object('ok', false, 'reason_code', 'flow_route_person_cap_present');
        end if;
    elsif not exists (
        select 1 from public.pilot_runtime_controls rc
         where rc.scope_key = v_route.legacy_scope_key and rc.runtime_state = 'armed'
    ) then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_route_legacy_not_armed');
    end if;
    update public.flow_engine_routes
       set executor = p_executor, generation = generation + 1, changed_at = clock_timestamp(),
           changed_by_ref = p_actor_name, change_reason = p_reason
     where flow_id = v_flow_id
    returning * into v_route;
    return jsonb_build_object('ok', true, 'route', public._flow_engine_route_json(v_route));
end;
$function$;

-- ---------------------------------------------------------------------------
-- 9. Los permisos. Ninguna tabla ni funcion para la API, salvo las dos
--    lecturas: service_role y, si existe, el rol de la T4. Los privilegios por
--    defecto de Supabase conceden directo a cada rol, asi que se revoca a cada
--    uno; revocarle a public no se los saca.
-- ---------------------------------------------------------------------------

do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public._flow_engine_kind_matches(text,jsonb)',
        'public._flow_engine_route_kinds()',
        'public._flow_engine_legacy_scope_exists(text,text)',
        'public._flow_engine_route_json(public.flow_engine_routes)',
        'public._flow_engine_sequence_reason(text)',
        'public._flow_engine_legacy_in_flight(text,text)',
        'public._flow_engine_legacy_steps(text)',
        'public._flow_engine_revalidation_is_valid(jsonb,integer,text)',
        'public._flow_engine_duration_minutes(jsonb)',
        'public._flow_engine_hour_minutes(jsonb)',
        'public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)',
        'public._flow_engine_fixed_zone(uuid)',
        'public._flow_engine_compile_policy(jsonb,uuid,integer,text)',
        'public.reject_flow_engine_mutation()',
        'public.validate_flow_engine_route_insert()',
        'public.validate_flow_engine_route_update()',
        'public.record_flow_engine_route_change()',
        'public._flow_engine_legacy_guard(text,text)',
        'public._flow_engine_legacy_guard_action(uuid,uuid,text)',
        'public.read_flow_engine_v1(jsonb,text)',
        'public.read_flow_engine_history_v1(jsonb,text,timestamptz)',
        'public.operator_create_flow_route_v1(jsonb,text,text,text,text,text,text)',
        'public.operator_route_flow_v1(jsonb,text,text,text,bigint,text,text,jsonb)'
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

    revoke all on table public.flow_engine_routes, public.flow_engine_runs,
        public.flow_engine_request_authorizations, public.flow_engine_start_rejections,
        public.flow_engine_divergences from public;
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on table public.flow_engine_routes, public.flow_engine_runs, '
            'public.flow_engine_request_authorizations, public.flow_engine_start_rejections, '
            'public.flow_engine_divergences from %I', v_role);
    end loop;

    foreach v_function in array array[
        'public.read_flow_engine_v1(jsonb,text)',
        'public.read_flow_engine_history_v1(jsonb,text,timestamptz)'
    ]::regprocedure[] loop
        if to_regrole('service_role') is not null then
            execute format('grant execute on function %s to service_role', v_function);
        end if;
        -- El rol de la T4 lo crea 20261021000100; una base sin el no lo nombra.
        if to_regrole('automation_expert_change') is not null then
            execute format('grant execute on function %s to automation_expert_change', v_function);
        end if;
    end loop;
end;
$acl$;

commit;
