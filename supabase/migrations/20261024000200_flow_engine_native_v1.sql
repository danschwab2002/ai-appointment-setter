-- flow-engine-native-v1: el motor nativo en la base (el PR 2 del hito 4).
-- Contrato: docs/contracts/flow-engine-v2.md (manda sobre el diseno), secciones
-- 2, 3, 4 y 6, y lo que fija para el PR 2 en la 4.4 y la 11. Diseno: «el
-- hito 4: el motor general y el AE que crea flujos» (2026-10-24, en el
-- vault), pieza A. Decisiones D1, D2 y D6 de Dan (2026-10-24).
--
-- Nace apagada: no crea ningun flujo, ninguna bolsa ni ninguna ruta, y la
-- unica funcion que abre un flujo nativo (_flow_engine_open_native_flow) no
-- tiene permiso para nadie mas que el duenio. Sin un flujo nativo, toda rama
-- nueva da falso y el carrito, el legado y las lecturas se comportan como
-- despues de 20261024000100. Lo que trae, por bloques:
--
-- 1. Tres tablas vacias, con RLS y sin permisos para la API:
--    flow_engine_pools (la bolsa nativa: su lock y su zona),
--    flow_cohort_members (la cohorte de prueba de un flujo nativo) y
--    flow_engine_native_runs (lo que un recorrido nativo necesita y la v1 no
--    guarda: el disparador, la oferta y la cuenta y el inbox de Chatwoot).
--    La ruta nativa: legacy_kind deja de ser not null y un check nuevo exige
--    executor motor, generation 0 y sin scope. El estado cohorte en el
--    control.
-- 2. Las guardas: una bolsa nativa no se llama como un scope del legado y un
--    scope no se llama como una bolsa (los dos lados, con un advisory lock por
--    nombre); la ruta nativa solo la crea _flow_engine_open_native_flow y no
--    se mueve (flow_route_native_cannot_move, en el disparador y en
--    operator_route_flow_v1); las transiciones de cohorte y la pausa desde
--    cohorte en validate_flow_control_change; la forma de pasos de un flujo
--    nativo en el compilador de la politica.
-- 3. _flow_engine_open_native_flow: los pasos 4 a 6 de la publicacion de un
--    item nuevo (contrato 7.2): la bolsa, la ruta nativa y el control en
--    cohorte, atados a la propuesta que publico la v1. La publicacion del
--    item nuevo (los pasos 1 a 3 y 7) y el item en el ciclo son del PR 4.
-- 4. La admision nativa, admit_flow_engine_event_v1, al lado de la del
--    carrito, que no se toca. En esta version el motor nativo ejecuta el
--    disparador compra con permiso compra.
-- 5. El arranque: _flow_engine_authorize_start toma, para un recorrido
--    nativo, el lock de la bolsa nativa y cuenta el dia en su zona
--    (_flow_engine_authorize_native_tail); el contexto del paso saca la forma
--    de pasos del disparador. La compra que dispara un flujo nativo no lo
--    frena en la reevaluacion sin tocarla: _portable_case_purchase_stop_reason
--    busca el binding de Hotmart del evento del caso, y una compra aprobada
--    no tiene (lo mide la prueba).
-- 6. Las lecturas de la v1 (read_flow_engine_v1, read_flow_engine_routes_v1
--    y read_flow_engine_history_v1) excluyen toda ruta nativa y todo flujo
--    en cohorte (contrato, seccion 1).
--
-- Los reemplazos van sobre la definicion VIGENTE de cada funcion, con el
-- metodo de 20261021000200 y 20261022000100: un texto exacto que tiene que
-- aparecer una sola vez, la marca que no puede estar, el duenio que es quien
-- aplica, y la marca presente despues. Las vigentes:
--   validate_flow_control_change ............ 20261016000100
--   validate_flow_engine_route_insert ....... 20261021000200
--   validate_flow_engine_route_update ....... 20261021000200
--   operator_route_flow_v1 .................. 20261021000200
--   _flow_engine_compile_policy_core ........ 20261021000200
--   read_flow_engine_v1 ..................... 20261021000200
--   read_flow_engine_routes_v1 .............. 20261022000200
--   read_flow_engine_history_v1 ............. 20261021000200 + 20261021000300,
--                                             20261022000300
--   _flow_engine_authorize_start ............ 20261022000100
--   flow_engine_execution_context_v1 ........ 20261022000100
--
-- Lo que un caso nativo NO tiene: el binding del piloto
-- (pilot_recovery_case_bindings). Sus lectores y que hacen con un caso sin el
-- estan en el cuerpo del PR; los que deciden por su presencia (la adopcion de
-- la respuesta, la derivacion y las lecturas del AE) lo dejan afuera.
--
-- Lo que no hace: no crea roles, no cambia el ACL del esquema, no toca filas
-- que existan y no agrega claves foraneas a tablas del sustrato. Correrla dos
-- veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; P0002 no existe; 55000 un estado que no lo
-- permite; 23514 un invariante.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public.admit_flow_engine_hotmart_v1(uuid,uuid,text,text,text,timestamptz,bigint,bigint,text,text,integer,text,integer,boolean)') is null then
        raise exception using errcode = '55000', message = 'flow_engine_motor_missing';
    end if;
    if to_regclass('public.flow_engine_pools') is not null then
        raise exception using errcode = '55000', message = 'flow_engine_native_already_applied';
    end if;
end;
$already$;

-- Los reemplazos por ancla, con la misma verificacion para todos.
create function pg_temp.flow_engine_native_patch(
    p_signature text, p_anchor text, p_replacement text, p_marker text)
returns void
language plpgsql
as $function$
declare
    v_definition text;
begin
    if to_regprocedure(p_signature) is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_function_missing', detail = p_signature;
    end if;
    if (select p.proowner from pg_proc p where p.oid = p_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_engine_native_owner_mismatch', detail = p_signature;
    end if;
    v_definition := pg_get_functiondef(p_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, p_anchor, '')) <> length(p_anchor)
       or position(p_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_definition',
            detail = p_signature;
    end if;
    execute replace(v_definition, p_anchor, p_replacement);
    if position(p_marker in pg_get_functiondef(p_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_definition_not_replaced',
            detail = p_signature;
    end if;
end;
$function$;

-- ===========================================================================
-- Bloque 1. Las tablas, la ruta nativa y el estado cohorte.
-- ===========================================================================

-- La bolsa nativa (contrato, seccion 3): su fila es el lock del conteo (FOR
-- UPDATE, como hoy la de pilot_runtime_controls) y su zona. La crea la
-- publicacion del primer flujo que la nombra; no cambia nunca.
create table public.flow_engine_pools (
    pool text primary key check (pool ~ '^[a-z][a-z0-9_-]{1,39}$'),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    timezone text not null,
    created_by_proposal_id uuid not null references public.flow_change_proposals (id) on delete restrict,
    created_at timestamptz not null default clock_timestamp()
);

-- La cohorte de prueba de un flujo nativo (contrato, seccion 6). El telefono
-- no se guarda: se guarda el contacto ya resuelto. Sin clave foranea al
-- contacto: el sustrato no se toca.
create table public.flow_cohort_members (
    flow_id uuid not null references public.flow_controls (flow_id) on delete restrict,
    contact_id uuid not null,
    added_by_ref text not null check (public._flow_store_actor_is_valid('operador', added_by_ref)),
    added_at timestamptz not null default clock_timestamp(),
    primary key (flow_id, contact_id)
);

-- Lo que un recorrido nativo necesita y flow_engine_runs no guarda. Una fila
-- por recorrido nativo, solo suma.
create table public.flow_engine_native_runs (
    recovery_case_id uuid primary key references public.flow_engine_runs (recovery_case_id) on delete restrict,
    trigger_event text not null check (trigger_event in ('compra')),
    offer_code text check (offer_code ~ '^[A-Za-z0-9_-]{1,64}$'),
    chatwoot_account_id bigint not null check (chatwoot_account_id > 0),
    chatwoot_inbox_id bigint not null check (chatwoot_inbox_id > 0),
    started_at timestamptz not null default clock_timestamp()
);

create index flow_engine_native_runs_trigger_idx on public.flow_engine_native_runs (trigger_event);

alter table public.flow_engine_pools enable row level security;
alter table public.flow_cohort_members enable row level security;
alter table public.flow_engine_native_runs enable row level security;

-- La ruta nativa (contrato, seccion 2): sin legacy_kind, en motor, generation
-- 0 y sin scope. El check de hoy (flow_engine_routes_scope_by_kind) sigue para
-- toda ruta con legacy_kind.
alter table public.flow_engine_routes alter column legacy_kind drop not null;
alter table public.flow_engine_routes
    add constraint flow_engine_routes_native_shape
    check (legacy_kind is not null
           or (executor = 'motor' and generation = 0 and legacy_scope_key is null));

-- El estado cohorte (contrato, seccion 6).
alter table public.flow_controls
    drop constraint flow_controls_state_check,
    add constraint flow_controls_state_check check (state in ('apagado', 'prendido', 'pausado', 'cohorte'));

-- Las tres tablas: la bolsa no cambia ni se borra, los recorridos solo suman y
-- un miembro de la cohorte se agrega o se saca, pero no se edita.
create function public.reject_flow_engine_native_mutation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    raise exception using errcode = '55000', message = case tg_table_name
        when 'flow_engine_pools' then 'flow_engine_pool_is_permanent'
        when 'flow_engine_native_runs' then 'flow_engine_native_run_is_append_only'
        when 'flow_cohort_members' then 'flow_cohort_member_is_immutable'
        else 'flow_engine_native_mutation_rejected' end;
end;
$function$;

-- ===========================================================================
-- Bloque 2. Las guardas.
-- ===========================================================================

-- El disparador de un flujo nativo que ejecuta esta version, o null.
create function public._flow_engine_native_trigger(p_definition jsonb)
returns text
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select case when p_definition -> 'dispara' = '{"evento": "compra"}'::jsonb then 'compra' end
$function$;

-- La forma de los pasos de un flujo nativo (contrato 4.4): sale del
-- disparador. compra es un solo envio, con nombre y producto (el nombre lo
-- extrae el bridge en el PR 3; hasta entonces la regla 14 lo frena al
-- publicar). Null para todo lo demas: el legado sigue con
-- _flow_engine_legacy_steps.
create function public._flow_engine_native_steps(p_kind text)
returns jsonb
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select case p_kind
        when 'nativo:compra' then
            '[{"step_key": "first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto"], "coupon_variables": null}]'::jsonb
    end
$function$;

-- La forma de pasos de un recorrido: la del disparador si es nativo, o null.
-- security definer: la llama flow_engine_execution_context_v1.
create function public._flow_engine_native_run_steps(p_recovery_case_id uuid)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    select public._flow_engine_native_steps('nativo:' || native.trigger_event)
      from public.flow_engine_native_runs native
     where native.recovery_case_id = p_recovery_case_id
$function$;

-- Por que el motor nativo de esta version no ejecuta una definicion, o null.
-- La ejecuta si dispara con la compra, entra con el permiso compra y repite
-- por persona o por persona y oferta.
create function public._flow_engine_native_unsupported(p_definition jsonb)
returns text
language sql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case
        when public._flow_engine_native_trigger(p_definition) is null then 'motor_nativo:disparador'
        when p_definition #> '{entra,permiso}' is distinct from '"compra"'::jsonb then 'motor_nativo:permiso'
        when coalesce(p_definition #>> '{topes,repeticion,una_por}', '') not in ('persona', 'persona_y_oferta')
            then 'motor_nativo:repeticion'
        when p_definition #> '{topes,repeticion,otra_despues_de}' is distinct from '"nunca"'::jsonb
             and public._flow_engine_duration_minutes(p_definition #> '{topes,repeticion,otra_despues_de}') is null
            then 'motor_nativo:repeticion'
        when p_definition -> 'prioridad' ? 'no_arranca_mientras'
             and jsonb_typeof(p_definition #> '{prioridad,no_arranca_mientras}') is distinct from 'array'
            then 'motor_nativo:prioridad'
    end
$function$;

-- La bolsa de una definicion nativa: topes.bolsa, o <producto>-<flujo>.
create function public._flow_engine_native_pool(p_definition jsonb)
returns text
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select coalesce(p_definition #>> '{topes,bolsa}',
                    (p_definition ->> 'producto') || '-' || (p_definition ->> 'flujo'))
$function$;

-- ⭐ Una bolsa nativa no se llama como un scope del legado (contrato,
-- seccion 3): los topes del legado se cuentan por scope_key, y una bolsa con
-- ese nombre sumaria sus envios sin que nadie lo vea. El advisory lock por
-- nombre serializa con el disparador del otro lado.
create function public.validate_flow_engine_pool_insert()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    perform pg_advisory_xact_lock(hashtextextended('flow-engine-pool-name:' || new.pool, 0));
    if exists (select 1 from public.pilot_runtime_controls runtime where runtime.scope_key = new.pool)
       or exists (select 1 from public.pilot_scope_versions scope where scope.scope_key = new.pool) then
        raise exception using errcode = '23514', message = 'flow_engine_pool_is_legacy_scope';
    end if;
    if not exists (select 1 from pg_timezone_names tz where tz.name = new.timezone) then
        raise exception using errcode = '22023', message = 'flow_engine_pool_zone_invalid';
    end if;
    return new;
end;
$function$;

-- El otro lado: un scope nuevo no se llama como una bolsa nativa. security
-- definer: el que inserta un scope no lee las tablas del motor.
create function public.reject_pilot_scope_named_like_native_pool()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    perform pg_advisory_xact_lock(hashtextextended('flow-engine-pool-name:' || new.scope_key, 0));
    if exists (select 1 from public.flow_engine_pools pool where pool.pool = new.scope_key) then
        raise exception using errcode = '23514', message = 'pilot_scope_is_native_pool';
    end if;
    return new;
end;
$function$;

create trigger flow_engine_pools_validate
before insert on public.flow_engine_pools
for each row execute function public.validate_flow_engine_pool_insert();
create trigger flow_engine_pools_permanent
before update or delete on public.flow_engine_pools
for each row execute function public.reject_flow_engine_native_mutation();
create trigger flow_engine_pools_no_truncate
before truncate on public.flow_engine_pools
for each statement execute function public.reject_flow_engine_native_mutation();
create trigger flow_engine_native_runs_append_only
before update or delete on public.flow_engine_native_runs
for each row execute function public.reject_flow_engine_native_mutation();
create trigger flow_engine_native_runs_no_truncate
before truncate on public.flow_engine_native_runs
for each statement execute function public.reject_flow_engine_native_mutation();
create trigger flow_cohort_members_immutable
before update on public.flow_cohort_members
for each row execute function public.reject_flow_engine_native_mutation();
create trigger pilot_scope_versions_not_native_pool
before insert on public.pilot_scope_versions
for each row execute function public.reject_pilot_scope_named_like_native_pool();

-- Los reemplazos del bloque 2, sobre la definicion vigente.
do $patch_guards$
begin
    -- La ruta nativa nace solo dentro de _flow_engine_open_native_flow (la
    -- marca local de la transaccion), en motor, con la version activa y un
    -- disparador que el motor nativo ejecuta. Con legacy_kind, como hoy.
    perform pg_temp.flow_engine_native_patch(
        'public.validate_flow_engine_route_insert()',
        E'    if not coalesce(new.executor = ''legado'' and new.generation = 0, false) then\n',
        E'    -- flow_engine_native_route: begin\n'
        || E'    -- El motor nativo (20261024000200): la ruta sin legacy_kind la crea\n'
        || E'    -- solo la apertura de un flujo nativo, en motor y sin scope.\n'
        || E'    if new.legacy_kind is null then\n'
        || E'        if coalesce(current_setting(''flow_engine.native_route_flow'', true), '''')\n'
        || E'           is distinct from new.flow_id::text then\n'
        || E'            raise exception using errcode = ''23514'', message = ''flow_route_native_needs_publication'';\n'
        || E'        end if;\n'
        || E'        select * into v_control from public.flow_controls c where c.flow_id = new.flow_id;\n'
        || E'        if not found or v_control.active_version is null then\n'
        || E'            raise exception using errcode = ''23514'', message = ''flow_route_needs_published_version'';\n'
        || E'        end if;\n'
        || E'        select d.definition into v_definition from public.flow_definitions d\n'
        || E'         where d.flow_id = new.flow_id and d.version = v_control.active_version;\n'
        || E'        if public._flow_engine_native_trigger(v_definition) is null then\n'
        || E'            raise exception using errcode = ''23514'', message = ''flow_route_kind_mismatch'';\n'
        || E'        end if;\n'
        || E'        return new;\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_route: end\n'
        || E'    if not coalesce(new.executor = ''legado'' and new.generation = 0, false) then\n',
        'flow_engine_native_route: begin');

    -- Una ruta nativa no se mueve: no hay legado al que volver.
    perform pg_temp.flow_engine_native_patch(
        'public.validate_flow_engine_route_update()',
        E'    if row(new.flow_id, new.legacy_kind, new.legacy_scope_key, new.created_at)\n',
        E'    -- flow_engine_native_route: begin\n'
        || E'    if old.legacy_kind is null then\n'
        || E'        raise exception using errcode = ''55000'', message = ''flow_route_native_cannot_move'';\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_route: end\n'
        || E'    if row(new.flow_id, new.legacy_kind, new.legacy_scope_key, new.created_at)\n',
        'flow_engine_native_route: begin');

    -- El mismo motivo como resultado del contrato, antes de mirar el destino.
    perform pg_temp.flow_engine_native_patch(
        'public.operator_route_flow_v1(jsonb,text,text,text,bigint,text,text,jsonb)',
        E'    select c.* into v_control from public.flow_controls c where c.flow_id = v_flow_id for share;\n',
        E'    select c.* into v_control from public.flow_controls c where c.flow_id = v_flow_id for share;\n'
        || E'    -- flow_engine_native_route: begin\n'
        || E'    if v_route.legacy_kind is null then\n'
        || E'        return jsonb_build_object(''ok'', false, ''reason_code'', ''flow_route_native_cannot_move'');\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_route: end\n',
        'flow_engine_native_route: begin');

    -- Las transiciones de cohorte (contrato, seccion 6) y la pausa desde
    -- cohorte. cohorte solo con una ruta nativa.
    perform pg_temp.flow_engine_native_patch(
        'public.validate_flow_control_change()',
        E'        or (old.state = ''pausado'' and new.state in (''prendido'', ''apagado''))\n',
        E'        or (old.state = ''pausado'' and new.state in (''prendido'', ''apagado''))\n'
        || E'        -- flow_engine_native_cohort: begin\n'
        || E'        or (old.state = ''apagado'' and new.state = ''cohorte'')\n'
        || E'        or (old.state = ''cohorte'' and new.state in (''prendido'', ''pausado'', ''apagado''))\n'
        || E'        or (old.state = ''pausado'' and new.state = ''cohorte'')\n'
        || E'        -- flow_engine_native_cohort: end\n',
        'flow_engine_native_cohort: begin');
    perform pg_temp.flow_engine_native_patch(
        'public.validate_flow_control_change()',
        E'        old.state = ''prendido'' and new.state = ''pausado''\n',
        E'        -- flow_engine_native_cohort_pause: la pausa tambien desde cohorte.\n'
        || E'        old.state in (''prendido'', ''cohorte'') and new.state = ''pausado''\n',
        'flow_engine_native_cohort_pause');
    perform pg_temp.flow_engine_native_patch(
        'public.validate_flow_control_change()',
        E'    if new.change_kind = ''pausado'' and not coalesce(\n',
        E'    -- flow_engine_native_cohort_route: begin\n'
        || E'    if new.state = ''cohorte'' and old.state is distinct from ''cohorte''\n'
        || E'       and not exists (select 1 from public.flow_engine_routes native_route\n'
        || E'                        where native_route.flow_id = new.flow_id\n'
        || E'                          and native_route.legacy_kind is null) then\n'
        || E'        raise exception using errcode = ''23514'', message = ''flow_control_cohort_needs_native_route'';\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_cohort_route: end\n'
        || E'    if new.change_kind = ''pausado'' and not coalesce(\n',
        'flow_engine_native_cohort_route: begin');

    -- La forma de pasos de un flujo nativo en el compilador. Con un
    -- legacy_kind del legado, _flow_engine_legacy_steps no es null y el
    -- coalesce devuelve lo mismo que hoy.
    perform pg_temp.flow_engine_native_patch(
        'public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)',
        E'    v_shape := public._flow_engine_legacy_steps(p_legacy_kind);\n',
        E'    -- flow_engine_native_shape: un flujo nativo saca la forma del disparador.\n'
        || E'    v_shape := coalesce(public._flow_engine_legacy_steps(p_legacy_kind),\n'
        || E'                        public._flow_engine_native_steps(p_legacy_kind));\n',
        'flow_engine_native_shape');
end;
$patch_guards$;

-- ===========================================================================
-- Bloque 3. Abrir un flujo nativo.
-- ===========================================================================

-- Los pasos 4 a 6 de la publicacion de un item nuevo (contrato 7.2), en la
-- misma transaccion que publico la v1: crea la bolsa si no existe (con su
-- guarda), la ruta nativa y pasa el control a cohorte. p_proposal_id es la
-- propuesta publicada que creo la v1 del flujo. p_timezone es la zona de la
-- instancia, para una definicion con horario siempre. Solo el duenio la
-- ejecuta: la llama la publicacion del item nuevo (el PR 4).
create function public._flow_engine_open_native_flow(p_flow_id uuid, p_proposal_id uuid, p_timezone text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_control public.flow_controls%rowtype;
    v_proposal public.flow_change_proposals%rowtype;
    v_definition public.flow_definitions%rowtype;
    v_pool public.flow_engine_pools%rowtype;
    v_pool_key text;
    v_zone text;
    v_unsupported text;
begin
    select * into v_control from public.flow_controls c where c.flow_id = p_flow_id for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_not_found';
    end if;
    select * into v_proposal from public.flow_change_proposals p where p.id = p_proposal_id;
    select * into v_definition from public.flow_definitions d
     where d.flow_id = p_flow_id and d.version = v_control.active_version;
    -- Un flujo nuevo: apagado, con su v1 recien publicada por esta propuesta y
    -- sin ruta.
    if v_proposal.id is null or v_proposal.status is distinct from 'publicada'
       or v_proposal.tenant_ref is distinct from v_control.tenant_ref
       or v_proposal.ally_ref is distinct from v_control.ally_ref
       or v_control.state is distinct from 'apagado' or v_control.active_version is distinct from 1
       or v_definition.proposal_id is distinct from p_proposal_id then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_engine_native_open_invalid');
    end if;
    if exists (select 1 from public.flow_engine_routes r where r.flow_id = p_flow_id) then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_route_already_exists');
    end if;
    v_unsupported := public._flow_engine_native_unsupported(v_definition.definition);
    if v_unsupported is not null then
        return jsonb_build_object('ok', false, 'reason_code', v_unsupported);
    end if;
    -- Una ruta nativa es una ruta en motor: con un renglon de tope por persona
    -- no se abre, el mismo codigo que mover a motor (contrato, seccion 2).
    if exists (select 1 from public.pilot_proactive_contact_caps k where k.tenant_key = v_control.tenant_ref) then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_route_person_cap_present');
    end if;

    -- 4. La bolsa: la de topes.bolsa, o <producto>-<flujo>, con la zona del
    -- horario o la de la instancia.
    v_pool_key := public._flow_engine_native_pool(v_definition.definition);
    v_zone := case when jsonb_typeof(v_definition.definition -> 'horario') = 'object'
                   then v_definition.definition #>> '{horario,zona}' else p_timezone end;
    select * into v_pool from public.flow_engine_pools pool where pool.pool = v_pool_key for update;
    if found then
        if v_pool.tenant_ref is distinct from v_control.tenant_ref
           or v_pool.ally_ref is distinct from v_control.ally_ref then
            return jsonb_build_object('ok', false, 'reason_code', 'flow_engine_pool_other_instance');
        end if;
        if v_pool.timezone is distinct from v_zone then
            return jsonb_build_object('ok', false, 'reason_code', 'flow_engine_pool_zone_mismatch');
        end if;
    else
        insert into public.flow_engine_pools (pool, tenant_ref, ally_ref, timezone, created_by_proposal_id)
        values (v_pool_key, v_control.tenant_ref, v_control.ally_ref, v_zone, p_proposal_id)
        returning * into v_pool;
    end if;

    -- 5. La ruta nativa. La marca local deja pasar al disparador de la ruta
    -- solo este flujo, solo en esta transaccion.
    perform set_config('flow_engine.native_route_flow', p_flow_id::text, true);
    insert into public.flow_engine_routes (flow_id, executor, legacy_kind, legacy_scope_key, generation,
        changed_by_ref, change_reason)
    values (p_flow_id, 'motor', null, null, 0, v_proposal.approved_by_ref, 'flujo nativo: ' || v_control.flow_key);
    perform set_config('flow_engine.native_route_flow', '', true);

    -- 6. El control en cohorte, con la v1 activa, atribuido a la propuesta.
    update public.flow_controls
       set state = 'cohorte', generation = generation + 1, change_kind = 'publicado',
           changed_by_kind = v_proposal.approved_by_kind, changed_by_ref = v_proposal.approved_by_ref,
           changed_by_proposal_id = p_proposal_id, change_reason = v_proposal.purpose,
           changed_at = clock_timestamp()
     where flow_id = p_flow_id;
    return jsonb_build_object('ok', true, 'pool', v_pool.pool, 'timezone', v_pool.timezone, 'state', 'cohorte',
        'generation', v_control.generation + 1);
end;
$function$;

-- ===========================================================================
-- Bloque 4. La admision nativa.
-- ===========================================================================

-- Un flujo nativo y un evento ya validado y bloqueado: arranca o no arranca.
-- Corre dentro de la subtransaccion de admit_flow_engine_event_v1: un error
-- lo convierte en error_interno:<sqlstate> sin frenar a los otros flujos.
create function public._flow_engine_admit_native_flow(
    p_control public.flow_controls, p_event public.webhook_events, p_contact_id uuid, p_offer text,
    p_moment timestamptz, p_account_id bigint, p_inbox_id bigint, p_destination text)
returns table (outcome text, reason text, recovery_case_id uuid, followup_sequence_id uuid,
               scheduled_action_id uuid, created boolean)
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_definition public.flow_definitions%rowtype;
    v_contact public.contacts%rowtype;
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_pool public.flow_engine_pools%rowtype;
    v_case_id uuid;
    v_sequence_id uuid;
    v_action_id uuid;
    v_reason text;
    v_message text;
    v_field text;
    v_after integer;
    v_blocking text;
    v_compiled public.followup_policy_versions%rowtype;
    v_stored public.followup_policy_versions%rowtype;
    v_policy_key text := 'flujo:' || p_control.flow_id::text;
    v_sendable jsonb;
    v_identity public.channel_identities%rowtype;
    v_account text := 'chatwoot:' || p_account_id::text;
    v_now timestamptz := clock_timestamp();
begin
    -- El control. Apagado (se apago entre la busqueda y el lock) no arranca y
    -- no deja fila; pausado deja la suya.
    if p_control.state = 'apagado' then
        return query select 'motor_no_arranca'::text, 'flujo_apagado'::text, null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;
    if p_control.state = 'pausado' then
        perform public._flow_engine_record_start_rejection(
            p_control.flow_id, p_control.active_version, p_event.id, 'admision', 'flujo_pausado');
        return query select 'motor_no_arranca'::text, 'flujo_pausado'::text, null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;
    select definition_row.* into strict v_definition
      from public.flow_definitions definition_row
     where definition_row.flow_id = p_control.flow_id and definition_row.version = p_control.active_version;

    -- La repeticion del mismo evento devuelve su caso.
    select run.recovery_case_id into v_case_id
      from public.flow_engine_runs run
     where run.flow_id = p_control.flow_id and run.trigger_source_ref = p_event.id;
    if v_case_id is not null then
        select sequence_row.id, action_row.id into v_sequence_id, v_action_id
          from public.followup_sequences sequence_row
          join public.scheduled_actions action_row on action_row.followup_sequence_id = sequence_row.id
         where sequence_row.recovery_case_id = v_case_id
         order by action_row.created_at asc
         limit 1;
        return query select 'motor_arranco'::text, null::text, v_case_id, v_sequence_id, v_action_id, false;
        return;
    end if;

    -- Lo que el motor nativo de esta version ejecuta: una falla del motor.
    v_reason := public._flow_engine_native_unsupported(v_definition.definition);
    if v_reason is not null then
        perform public._flow_engine_record_start_rejection(
            p_control.flow_id, v_definition.version, p_event.id, 'admision', v_reason);
        return query select 'motor_no_arranca'::text, v_reason, null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;

    -- La cohorte: fuera de ella no deja fila (contrato 4.3).
    if p_control.state = 'cohorte' and not exists (
        select 1 from public.flow_cohort_members member
         where member.flow_id = p_control.flow_id and member.contact_id = p_contact_id) then
        return query select 'motor_no_arranca'::text, 'fuera_de_cohorte'::text, null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;

    -- El permiso: con compra, la evidencia es este evento de compra.
    if public._flow_engine_native_trigger(v_definition.definition) is distinct from 'compra'
       or p_event.event_type is distinct from 'PURCHASE_APPROVED' then
        v_reason := 'no_entra:permiso';
    end if;

    -- La baja: la del contacto y la de Chatwoot de ese movil.
    if v_reason is null then
        select contact_row.* into strict v_contact from public.contacts contact_row where contact_row.id = p_contact_id;
        if v_contact.contact_permission in ('opted_out', 'blocked', 'restricted')
           or v_contact.lifecycle_status = 'do_not_contact'
           or public._portable_chatwoot_opt_out_stop(p_account_id, p_contact_id, p_destination) then
            v_reason := 'baja';
        end if;
    end if;

    -- La repeticion: un recorrido en curso de este flujo para la misma
    -- persona (y oferta) es ya_en_curso; uno anterior dentro de
    -- otra_despues_de, o cualquiera con nunca, es ya_disparado.
    if v_reason is null then
        v_after := case when v_definition.definition #> '{topes,repeticion,otra_despues_de}' = '"nunca"'::jsonb
                        then null
                        else public._flow_engine_duration_minutes(
                            v_definition.definition #> '{topes,repeticion,otra_despues_de}') end;
        select case when rc.status in ('grace_period', 'active', 'paused') then 'ya_en_curso' else 'ya_disparado' end
          into v_reason
          from public.flow_engine_runs run
          join public.recovery_cases rc on rc.id = run.recovery_case_id
          left join public.flow_engine_native_runs native on native.recovery_case_id = run.recovery_case_id
         where run.flow_id = p_control.flow_id and run.person_ref = p_contact_id
           and (v_definition.definition #>> '{topes,repeticion,una_por}' = 'persona'
                or native.offer_code is not distinct from p_offer)
           and (rc.status in ('grace_period', 'active', 'paused')
                or v_after is null
                or run.started_at > p_moment - v_after * interval '1 minute')
         order by (rc.status in ('grace_period', 'active', 'paused')) desc
         limit 1;
    end if;

    -- La prioridad entre flujos del motor (relations_between_engine_flows_only):
    -- un recorrido abierto de un flujo de no_arranca_mientras, del mismo
    -- producto y la misma persona.
    if v_reason is null then
        select run.flow_key into v_blocking
          from public.flow_engine_runs run
          join public.recovery_cases rc on rc.id = run.recovery_case_id
         where run.person_ref = p_contact_id and run.product_ref = p_control.product_ref
           and rc.status in ('grace_period', 'active', 'paused')
           and run.flow_key in (select jsonb_array_elements_text(
                   coalesce(v_definition.definition #> '{prioridad,no_arranca_mientras}', '[]'::jsonb)))
         order by run.flow_key
         limit 1;
        if v_blocking is not null then
            v_reason := 'no_arranca_mientras:' || v_blocking;
        end if;
    end if;

    -- Un evento abre un solo caso (recovery_cases.abandonment_event_id es
    -- unico): si ya abrio el de otro flujo, este no arranca.
    if v_reason is null and exists (
        select 1 from public.recovery_cases other where other.abandonment_event_id = p_event.id) then
        v_reason := 'evento_con_otro_flujo';
    end if;

    if v_reason is not null then
        perform public._flow_engine_record_start_rejection(
            p_control.flow_id, v_definition.version, p_event.id, 'admision', v_reason);
        return query select 'motor_no_arranca'::text, v_reason, null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;

    -- La bolsa: la de la version activa tiene que existir.
    select pool.* into v_pool from public.flow_engine_pools pool
     where pool.pool = public._flow_engine_native_pool(v_definition.definition);
    if not found then
        perform public._flow_engine_record_start_rejection(
            p_control.flow_id, v_definition.version, p_event.id, 'admision', 'flow_engine_pool_unknown');
        return query select 'motor_no_arranca'::text, 'flow_engine_pool_unknown'::text,
            null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;

    -- La politica compilada: la de la forma nativa, sin los pasos que no son
    -- envios (etiquetar lo ejecuta el bridge), en la zona de la bolsa. Se
    -- inserta publicada y la que quede tiene que ser esta.
    v_sendable := v_definition.definition || jsonb_build_object('pasos', coalesce((
        select jsonb_agg(step.value order by step.ordinality)
          from jsonb_array_elements(coalesce(v_definition.definition -> 'pasos', '[]'::jsonb))
               with ordinality as step(value, ordinality)
         where not (jsonb_typeof(step.value) = 'object' and step.value ? 'etiquetar')), '[]'::jsonb));
    begin
        v_compiled := public._flow_engine_compile_policy_core(
            v_sendable, p_control.flow_id::text, v_definition.version,
            'nativo:' || public._flow_engine_native_trigger(v_definition.definition), v_pool.timezone);
    exception when sqlstate '22023' then
        get stacked diagnostics v_message = message_text;
        v_reason := public._flow_engine_reason(left('politica_compilada:' || v_message, 80),
                                               'politica_compilada:no_compila');
        perform public._flow_engine_record_start_rejection(
            p_control.flow_id, v_definition.version, p_event.id, 'admision', v_reason);
        return query select 'motor_no_arranca'::text, v_reason, null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end;
    insert into public.followup_policy_versions (
        policy_key, version, status, purpose, timezone, business_windows, grace_period, expires_after,
        max_automatic_messages, steps, approved_by, approved_at, published_at)
    values (
        v_policy_key, v_definition.version, 'published', v_compiled.purpose, v_compiled.timezone,
        v_compiled.business_windows, v_compiled.grace_period, v_compiled.expires_after,
        v_compiled.max_automatic_messages, v_compiled.steps, v_definition.approved_by_ref,
        v_definition.published_at, v_definition.published_at)
    on conflict (policy_key, version) do nothing;
    select stored.* into strict v_stored
      from public.followup_policy_versions stored
     where stored.policy_key = v_policy_key and stored.version = v_definition.version;
    v_field := case
        when v_stored.status is distinct from 'published' then 'status'
        when v_stored.timezone is distinct from v_compiled.timezone then 'timezone'
        when v_stored.business_windows is distinct from v_compiled.business_windows then 'business_windows'
        when v_stored.grace_period is distinct from v_compiled.grace_period then 'grace_period'
        when v_stored.expires_after is distinct from v_compiled.expires_after then 'expires_after'
        when v_stored.max_automatic_messages is distinct from v_compiled.max_automatic_messages
            then 'max_automatic_messages'
        when v_stored.steps is distinct from v_compiled.steps then 'steps'
    end;
    if v_field is not null then
        v_reason := 'politica_compilada:' || v_field;
        perform public._flow_engine_record_start_rejection(
            p_control.flow_id, v_definition.version, p_event.id, 'admision', v_reason);
        return query select 'motor_no_arranca'::text, v_reason, null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;
    if p_moment + v_stored.expires_after <= p_moment + v_stored.grace_period then
        raise exception using errcode = '22023', message = 'flow_engine_native_window_invalid';
    end if;

    -- El producto de la instancia (para el caso: el id de Hotmart y el nombre).
    select binding.* into strict v_binding
      from public.commercial_ally_runtime_bindings binding
     where binding.tenant_ref = p_control.tenant_ref and binding.ally_ref = p_control.ally_ref
       and binding.funnel_ref = p_control.product_ref and binding.status = 'active'
     order by binding.binding_version desc
     limit 1;

    -- La identidad de WhatsApp, como el planificador de hoy
    -- (plan_cart_recovery_with_identity).
    select identity_row.* into v_identity
      from public.channel_identities identity_row
     where identity_row.channel = 'whatsapp' and identity_row.account_id = v_account
       and identity_row.external_user_id = p_destination
     for update;
    if v_identity.id is null then
        insert into public.channel_identities (contact_id, channel, account_id, external_user_id, identity_status, metadata)
        values (p_contact_id, 'whatsapp', v_account, p_destination, 'active', jsonb_build_object('inbox_id', p_inbox_id))
        returning * into v_identity;
    end if;
    if v_identity.contact_id <> p_contact_id then
        raise exception using errcode = '23514', message = 'channel_identity_contact_mismatch';
    end if;
    if v_identity.identity_status <> 'active' then
        raise exception using errcode = '23514', message = 'channel_identity_not_active';
    end if;
    if v_identity.metadata ? 'inbox_id' and v_identity.metadata ->> 'inbox_id' <> p_inbox_id::text then
        raise exception using errcode = '23514', message = 'channel_identity_inbox_mismatch';
    end if;
    update public.channel_identities
       set metadata = metadata || jsonb_build_object('inbox_id', p_inbox_id), updated_at = clock_timestamp()
     where id = v_identity.id;

    -- El caso, la secuencia y la accion sobre el sustrato de hoy. La oferta
    -- del caso va nula a proposito: el planificador del carrito (y el del
    -- motor) suman un carrito al caso abierto de la misma persona, producto y
    -- oferta, y un carrito nunca tiene que caer en un caso nativo. La oferta
    -- del evento queda en flow_engine_native_runs.
    insert into public.recovery_cases (
        contact_id, abandonment_event_id, source, external_product_id, product_name, offer_code,
        status, lead_stage, grace_expires_at, policy_key, policy_version, context)
    values (
        p_contact_id, p_event.id, p_event.source, v_binding.hotmart_product_id::text, v_binding.product_name, null,
        'grace_period', 'new', p_moment + v_stored.grace_period, v_policy_key, v_definition.version,
        jsonb_build_object('flow_engine_event_id', p_event.id, 'flow_engine_trigger',
                           public._flow_engine_native_trigger(v_definition.definition)))
    returning id into v_case_id;
    update public.recovery_cases
       set selected_channel_identity_id = v_identity.id, identity_resolution_status = 'resolved',
           identity_resolution_error = null, identity_resolution_last_attempt_at = clock_timestamp(),
           identity_resolution_attempt_count = identity_resolution_attempt_count + 1
     where id = v_case_id;
    insert into public.followup_sequences (
        recovery_case_id, status, reason, policy_key, policy_version, current_step, max_attempts)
    values (v_case_id, 'active', 'recovery', v_policy_key, v_definition.version, 0, v_stored.max_automatic_messages)
    returning id into v_sequence_id;
    insert into public.scheduled_actions (
        recovery_case_id, followup_sequence_id, action_type, status, due_at, expires_at, expected_case_version,
        idempotency_key, policy_key, policy_version, step_key, anchor_type, anchor_subject_internal_id,
        anchor_observed_at, anchor_checkpoint)
    values (
        v_case_id, v_sequence_id, 'first_contact_review', 'pending', p_moment + v_stored.grace_period,
        p_moment + v_stored.expires_after,
        (select rc.version from public.recovery_cases rc where rc.id = v_case_id),
        'flow_engine_native:first_contact:' || v_case_id::text, v_policy_key, v_definition.version,
        v_stored.steps -> 0 ->> 'step_key', 'flow_engine_event', p_event.id, p_moment,
        jsonb_build_object('webhook_event_id', p_event.id))
    returning id into v_action_id;
    insert into public.conversation_events (recovery_case_id, event_type, actor_type, related_action_id, data)
    values (v_case_id, 'flow_engine_native_planned', 'system', v_action_id,
        jsonb_build_object('policy_key', v_policy_key, 'policy_version', v_definition.version,
                           'flow_id', p_control.flow_id, 'trigger',
                           public._flow_engine_native_trigger(v_definition.definition)));

    -- La compra es la autorizacion (permiso compra), como el carrito lo es
    -- para el carrito: un renglon allowed solo si no hay ninguno vigente. Una
    -- denegacion vigente gana en la reevaluacion.
    if not exists (
        select 1 from public.contact_authorizations ca
         where ca.contact_id = p_contact_id and ca.channel = 'whatsapp' and ca.purpose = 'cart_recovery'
           and ca.valid_from <= v_now and (ca.valid_until is null or ca.valid_until > v_now)
    ) then
        insert into public.contact_authorizations (
            contact_id, channel, purpose, authorization_status, authorization_source, evidence, valid_from)
        values (p_contact_id, 'whatsapp', 'cart_recovery', 'allowed', 'hotmart',
            jsonb_build_object('reason', 'purchase_approved', 'webhook_event_id', p_event.id,
                               'recovery_case_id', v_case_id),
            v_now);
    end if;

    -- El recorrido, con la version fijada y sin legado.
    insert into public.flow_engine_runs (
        recovery_case_id, flow_id, version, definition_sha256, product_ref, flow_key, pool, legacy_kind,
        legacy_scope_key, person_ref, trigger_source_ref, legacy_dispatch_ok)
    values (
        v_case_id, p_control.flow_id, v_definition.version, v_definition.definition_sha256, p_control.product_ref,
        p_control.flow_key, v_pool.pool, null, null, p_contact_id, p_event.id, false);
    insert into public.flow_engine_native_runs (recovery_case_id, trigger_event, offer_code,
        chatwoot_account_id, chatwoot_inbox_id)
    values (v_case_id, public._flow_engine_native_trigger(v_definition.definition), p_offer,
        p_account_id, p_inbox_id);

    return query select 'motor_arranco'::text, null::text, v_case_id, v_sequence_id, v_action_id, true;
end;
$function$;

-- La admision nativa (contrato, seccion 4). p_flow_event es el evento ya
-- normalizado por el bridge (4.1), con estas claves exactas. Una fila por
-- flujo nativo que dispara el evento, o una sola con sin_flujo.
create function public.admit_flow_engine_event_v1(p_instance jsonb, p_flow_event jsonb)
returns table (flow_id uuid, outcome text, reason text, recovery_case_id uuid,
               followup_sequence_id uuid, scheduled_action_id uuid, created boolean)
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_instance record;
    v_type text;
    v_event_id uuid;
    v_contact_id uuid;
    v_moment timestamptz;
    v_offer text;
    v_account_id bigint;
    v_inbox_id bigint;
    v_destination text;
    v_product text;
    v_event public.webhook_events%rowtype;
    v_control public.flow_controls%rowtype;
    v_flow_ids uuid[];
    v_flow_id uuid;
    v_one record;
    v_sqlstate text;
    v_any boolean := false;
begin
    v_instance := public._flow_store_instance(p_instance);

    -- 4.1 La forma del evento, con las claves exactas.
    if jsonb_typeof(p_flow_event) is distinct from 'object'
       or (select array_agg(k order by k) from jsonb_object_keys(p_flow_event) k)
          is distinct from array['chatwoot', 'contacto_id', 'destino', 'evento_id', 'fuente', 'momento',
                                 'oferta', 'producto_ref', 'tipo']
       or p_flow_event -> 'fuente' is distinct from '"hotmart"'::jsonb
       or coalesce(p_flow_event ->> 'tipo', '') not in ('compra', 'carrito')
       or not coalesce(p_flow_event ->> 'evento_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', false)
       or not coalesce(p_flow_event ->> 'contacto_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', false)
       or not coalesce(p_flow_event ->> 'producto_ref' ~ '^[a-z0-9][a-z0-9-]{0,127}$', false)
       or not (p_flow_event -> 'oferta' = 'null'::jsonb
               or coalesce(p_flow_event ->> 'oferta' ~ '^[A-Za-z0-9_-]{1,64}$', false))
       or jsonb_typeof(p_flow_event -> 'momento') is distinct from 'string'
       or not coalesce(p_flow_event ->> 'momento' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$', false)
       or jsonb_typeof(p_flow_event -> 'chatwoot') is distinct from 'object'
       or (select array_agg(k order by k) from jsonb_object_keys(
               case when jsonb_typeof(p_flow_event -> 'chatwoot') = 'object'
                    then p_flow_event -> 'chatwoot' else '{}'::jsonb end) k)
          is distinct from array['account_id', 'inbox_id']
       or jsonb_typeof(p_flow_event #> '{chatwoot,account_id}') is distinct from 'number'
       or jsonb_typeof(p_flow_event #> '{chatwoot,inbox_id}') is distinct from 'number'
       or not coalesce(p_flow_event #>> '{chatwoot,account_id}' ~ '^[1-9][0-9]{0,17}$', false)
       or not coalesce(p_flow_event #>> '{chatwoot,inbox_id}' ~ '^[1-9][0-9]{0,17}$', false)
       or not coalesce(p_flow_event ->> 'destino' ~ '^[0-9]{6,20}$', false) then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_event';
    end if;
    v_type := p_flow_event ->> 'tipo';
    v_event_id := (p_flow_event ->> 'evento_id')::uuid;
    v_contact_id := (p_flow_event ->> 'contacto_id')::uuid;
    v_product := p_flow_event ->> 'producto_ref';
    v_offer := p_flow_event ->> 'oferta';
    v_account_id := (p_flow_event #>> '{chatwoot,account_id}')::bigint;
    v_inbox_id := (p_flow_event #>> '{chatwoot,inbox_id}')::bigint;
    v_destination := p_flow_event ->> 'destino';
    begin
        v_moment := (p_flow_event ->> 'momento')::timestamptz;
    exception when others then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_event';
    end;
    if v_moment > clock_timestamp() + interval '5 minutes' then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_event';
    end if;
    if not public._flow_store_product_is_bound(v_instance.o_tenant_ref, v_instance.o_ally_ref, v_product) then
        raise exception using errcode = '22023', message = 'flow_store_product_not_bound';
    end if;

    -- El evento persistido tiene que ser este: de Hotmart, del tipo y del
    -- producto (el id de Hotmart del binding de la instancia, nunca el nombre).
    -- Se mira antes de buscar flujos: un evento mal armado por el bridge es
    -- un error aunque no haya ningun flujo nativo.
    select event_row.* into v_event from public.webhook_events event_row where event_row.id = v_event_id;
    if not found
       or v_event.source <> 'hotmart'
       or v_event.event_type is distinct from (case v_type when 'compra' then 'PURCHASE_APPROVED'
                                                    else 'PURCHASE_OUT_OF_SHOPPING_CART' end)
       or not exists (
           select 1 from public.commercial_ally_runtime_bindings binding
            where binding.tenant_ref = v_instance.o_tenant_ref and binding.ally_ref = v_instance.o_ally_ref
              and binding.funnel_ref = v_product
              and binding.hotmart_product_id::text = v_event.payload #>> '{data,product,id}') then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_event';
    end if;
    -- Los flujos nativos de la instancia y del producto que dispara el evento:
    -- la ruta y el control FOR SHARE, en orden de flow_id, antes del evento y
    -- del contacto (el orden de §2.4). Apagado no arranca y no deja fila.
    select coalesce(array_agg(c.flow_id order by c.flow_id), array[]::uuid[]) into v_flow_ids
      from public.flow_controls c
      join public.flow_engine_routes r on r.flow_id = c.flow_id and r.legacy_kind is null
      join public.flow_definitions d on d.flow_id = c.flow_id and d.version = c.active_version
     where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
       and c.product_ref = v_product and c.state <> 'apagado'
       and d.definition #>> '{dispara,evento}' = v_type;
    if cardinality(v_flow_ids) = 0 then
        return query select null::uuid, 'sin_flujo'::text, null::text, null::uuid, null::uuid, null::uuid, null::boolean;
        return;
    end if;
    perform 1 from public.flow_engine_routes r where r.flow_id = any(v_flow_ids) order by r.flow_id for share;
    perform 1 from public.flow_controls c where c.flow_id = any(v_flow_ids) order by c.flow_id for share;

    -- El evento, ya validado arriba, FOR UPDATE: serializa las admisiones del
    -- mismo evento.
    select event_row.* into strict v_event from public.webhook_events event_row
     where event_row.id = v_event_id for update;
    perform 1 from public.contacts contact_row where contact_row.id = v_contact_id for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_engine_event_contact_not_found';
    end if;

    -- 4.2 Cada flujo en su subtransaccion: la falla de uno no frena a los otros.
    foreach v_flow_id in array v_flow_ids loop
        select c.* into strict v_control from public.flow_controls c where c.flow_id = v_flow_id;
        begin
            select one.* into v_one
              from public._flow_engine_admit_native_flow(
                  v_control, v_event, v_contact_id, v_offer, v_moment, v_account_id, v_inbox_id,
                  v_destination) one;
        exception when others then
            get stacked diagnostics v_sqlstate = returned_sqlstate;
            perform public._flow_engine_record_start_rejection(
                v_control.flow_id, v_control.active_version, v_event.id, 'admision',
                'error_interno:' || lower(v_sqlstate));
            select 'motor_no_arranca'::text as outcome, 'error_interno:' || lower(v_sqlstate) as reason,
                   null::uuid as recovery_case_id, null::uuid as followup_sequence_id,
                   null::uuid as scheduled_action_id, null::boolean as created
              into v_one;
        end;
        v_any := true;
        return query select v_control.flow_id, v_one.outcome, v_one.reason, v_one.recovery_case_id,
            v_one.followup_sequence_id, v_one.scheduled_action_id, v_one.created;
    end loop;
end;
$function$;

-- ===========================================================================
-- Bloque 5. El arranque y el contexto.
-- ===========================================================================

-- Lo que sigue a la pausa en la autorizacion de un recorrido nativo: el lock
-- de la bolsa nativa (FOR UPDATE), el intento reservado como plantilla
-- aprobada, la identidad con la cuenta y el inbox del recorrido, la cohorte
-- otra vez, y los topes de la definicion fijada contados sobre la bolsa por
-- los dos caminos, con el dia en la zona de la bolsa. Sin binding ni scope del
-- legado.
create function public._flow_engine_authorize_native_tail(
    p_action_id uuid, p_attempt_id uuid, p_run public.flow_engine_runs, p_case public.recovery_cases,
    p_control public.flow_controls, p_clock timestamptz)
returns table (outcome text, reason text, authorization_id uuid, flow_generation bigint, replayed boolean)
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_pool public.flow_engine_pools%rowtype;
    v_existing public.flow_engine_request_authorizations%rowtype;
    v_native public.flow_engine_native_runs%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_identity public.channel_identities%rowtype;
    v_definition jsonb;
    v_local_date date;
    v_total integer;
    v_daily integer;
    v_id uuid;
begin
    select pool.* into v_pool from public.flow_engine_pools pool where pool.pool = p_run.pool for update;
    if not found then
        return query select 'rechazado'::text, 'flow_engine_pool_unknown'::text, null::uuid, p_control.generation, false;
        return;
    end if;
    select engine_row.* into v_existing
      from public.flow_engine_request_authorizations engine_row where engine_row.attempt_id = p_attempt_id;
    if found then
        if v_existing.action_id <> p_action_id then
            return query select 'rechazado'::text, 'pilot_attempt_mismatch'::text, null::uuid, null::bigint, false;
        else
            return query select 'authorized'::text, null::text, v_existing.id, v_existing.flow_generation, true;
        end if;
        return;
    end if;

    select native.* into v_native from public.flow_engine_native_runs native
     where native.recovery_case_id = p_run.recovery_case_id;
    select attempt.* into v_attempt
      from public.followup_delivery_attempts attempt
     where attempt.id = p_attempt_id and attempt.action_id = p_action_id;
    if v_native.recovery_case_id is null or v_attempt.id is null or v_attempt.phase <> 'reserved'
       or v_attempt.outcome is not null then
        return query select 'rechazado'::text, 'pilot_attempt_mismatch'::text, null::uuid, p_control.generation, false;
        return;
    end if;
    if v_attempt.channel <> 'whatsapp' or v_attempt.mode <> 'approved_template' then
        return query select 'rechazado'::text, 'pilot_delivery_mode_mismatch'::text, null::uuid,
            p_control.generation, false;
        return;
    end if;
    select identity_row.* into v_identity
      from public.channel_identities identity_row
     where identity_row.id = p_case.selected_channel_identity_id
       and identity_row.contact_id = p_case.contact_id
       and identity_row.channel = 'whatsapp'
       and identity_row.identity_status = 'active';
    if not found
       or v_identity.account_id <> 'chatwoot:' || v_native.chatwoot_account_id::text
       or coalesce(v_identity.metadata ->> 'inbox_id', '') <> v_native.chatwoot_inbox_id::text then
        return query select 'rechazado'::text, 'pilot_attempt_mismatch'::text, null::uuid, p_control.generation, false;
        return;
    end if;
    if p_control.state = 'cohorte' and not exists (
        select 1 from public.flow_cohort_members member
         where member.flow_id = p_run.flow_id and member.contact_id = p_case.contact_id) then
        return query select 'rechazado'::text, 'fuera_de_cohorte'::text, null::uuid, p_control.generation, false;
        return;
    end if;

    select definition_row.definition into strict v_definition
      from public.flow_definitions definition_row
     where definition_row.flow_id = p_run.flow_id and definition_row.version = p_run.version;
    v_local_date := (p_clock at time zone v_pool.timezone)::date;
    select (select count(*)::integer from public.pilot_outbound_request_authorizations authrow
             where authrow.scope_key = p_run.pool)
           + (select count(*)::integer from public.flow_engine_request_authorizations engine_row
               where engine_row.pool = p_run.pool)
      into v_total;
    if jsonb_typeof(v_definition #> '{topes,total}') = 'number'
       and v_total >= (v_definition #>> '{topes,total}')::numeric then
        return query select 'rechazado'::text, 'tope_del_flujo'::text, null::uuid, p_control.generation, false;
        return;
    end if;
    select (select count(*)::integer from public.pilot_outbound_request_authorizations authrow
             where authrow.scope_key = p_run.pool and authrow.local_budget_date = v_local_date)
           + (select count(*)::integer from public.flow_engine_request_authorizations engine_row
               where engine_row.pool = p_run.pool and engine_row.local_budget_date = v_local_date)
      into v_daily;
    if jsonb_typeof(v_definition #> '{topes,por_dia}') = 'number'
       and v_daily >= (v_definition #>> '{topes,por_dia}')::numeric then
        return query select 'rechazado'::text, 'tope_del_flujo'::text, null::uuid, p_control.generation, false;
        return;
    end if;

    insert into public.flow_engine_request_authorizations (
        flow_id, version, pool, legacy_scope_key, action_id, attempt_id, contact_id, local_budget_date,
        flow_generation, authorized_at)
    values (
        p_run.flow_id, p_run.version, p_run.pool, null, p_action_id, p_attempt_id,
        p_case.contact_id, v_local_date, p_control.generation, p_clock)
    returning id into v_id;
    return query select 'authorized'::text, null::text, v_id, p_control.generation, false;
end;
$function$;

do $patch_start$
begin
    -- La autorizacion: un recorrido nativo (legacy_kind nulo) sigue por su
    -- cola desde la bolsa. El del carrito, como hoy.
    perform pg_temp.flow_engine_native_patch(
        'public._flow_engine_authorize_start(uuid,uuid,timestamptz,boolean)',
        E'    -- (3) La bolsa.\n',
        E'    -- flow_engine_native_start: begin\n'
        || E'    -- El motor nativo (20261024000200): la bolsa nativa y su zona.\n'
        || E'    if v_run.legacy_kind is null then\n'
        || E'        return query select native_tail.* from public._flow_engine_authorize_native_tail(\n'
        || E'            p_action_id, p_attempt_id, v_run, v_case, v_control, v_clock) native_tail;\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_start: end\n'
        || E'    -- (3) La bolsa.\n',
        'flow_engine_native_start: begin');

    -- El contexto del paso: la forma de pasos de un recorrido nativo sale del
    -- disparador. Con un legacy_kind, _flow_engine_legacy_steps no es null y
    -- el coalesce devuelve lo mismo que hoy.
    perform pg_temp.flow_engine_native_patch(
        'public.flow_engine_execution_context_v1(uuid)',
        E'            from jsonb_array_elements(public._flow_engine_legacy_steps(run.legacy_kind))\n',
        E'            -- flow_engine_native_steps: la forma del disparador en un recorrido nativo.\n'
        || E'            from jsonb_array_elements(coalesce(public._flow_engine_legacy_steps(run.legacy_kind),\n'
        || E'                                               public._flow_engine_native_run_steps(run.recovery_case_id)))\n',
        'flow_engine_native_steps');

end;
$patch_start$;

-- ===========================================================================
-- Bloque 6. Las lecturas de la v1 excluyen lo nativo (contrato, seccion 1).
-- ===========================================================================

do $patch_reads$
begin
    perform pg_temp.flow_engine_native_patch(
        'public.read_flow_engine_v1(jsonb,text)',
        E'           and (p_after is null or (c.product_ref, c.flow_key) > (v_after_product, v_after_key))\n',
        E'           and (p_after is null or (c.product_ref, c.flow_key) > (v_after_product, v_after_key))\n'
        || E'           -- flow_engine_native_excluded: ni ruta nativa ni cohorte (20261024000200).\n'
        || E'           and c.state <> ''cohorte''\n'
        || E'           and not exists (select 1 from public.flow_engine_routes native_route\n'
        || E'                            where native_route.flow_id = c.flow_id and native_route.legacy_kind is null)\n',
        'flow_engine_native_excluded');
    perform pg_temp.flow_engine_native_patch(
        'public.read_flow_engine_routes_v1(jsonb)',
        E'     where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref;\n',
        E'     where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref\n'
        || E'       -- flow_engine_native_excluded: ni ruta nativa ni cohorte (20261024000200).\n'
        || E'       and r.legacy_kind is not null and c.state <> ''cohorte'';\n',
        'flow_engine_native_excluded');
    perform pg_temp.flow_engine_native_patch(
        'public.read_flow_engine_history_v1(jsonb,text,timestamptz)',
        E'                           and c.product_ref = p_product_ref), array[]::text[])\n',
        E'                           and c.product_ref = p_product_ref\n'
        || E'                           -- flow_engine_native_excluded: sin los flujos nativos (20261024000200).\n'
        || E'                           and not exists (select 1 from public.flow_engine_routes native_route\n'
        || E'                                            where native_route.flow_id = c.flow_id\n'
        || E'                                              and native_route.legacy_kind is null)), array[]::text[])\n',
        'flow_engine_native_excluded');
    -- Los casos de Hotmart del producto (cs, «hechos de la persona»): sin los
    -- nativos. La compra de la persona sigue en la historia por su evento.
    perform pg_temp.flow_engine_native_patch(
        'public.read_flow_engine_history_v1(jsonb,text,timestamptz)',
        E'       and (rc.created_at >= v.siembra or rc.status in (''grace_period'', ''active'', ''paused''))\n',
        E'       and (rc.created_at >= v.siembra or rc.status in (''grace_period'', ''active'', ''paused''))\n'
        || E'       -- flow_engine_native_cases_excluded: sin los casos nativos (20261024000200).\n'
        || E'       and not exists (select 1 from public.flow_engine_native_runs native_run\n'
        || E'                        where native_run.recovery_case_id = rc.id)\n',
        'flow_engine_native_cases_excluded');
end;
$patch_reads$;

-- ===========================================================================
-- Los permisos. Ninguna tabla ni funcion para la API, salvo la admision:
-- service_role. Los privilegios por defecto de Supabase conceden directo a
-- cada rol, asi que se revoca a cada uno.
-- ===========================================================================

do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public.reject_flow_engine_native_mutation()',
        'public._flow_engine_native_trigger(jsonb)',
        'public._flow_engine_native_steps(text)',
        'public._flow_engine_native_run_steps(uuid)',
        'public._flow_engine_native_unsupported(jsonb)',
        'public._flow_engine_native_pool(jsonb)',
        'public.validate_flow_engine_pool_insert()',
        'public.reject_pilot_scope_named_like_native_pool()',
        'public._flow_engine_open_native_flow(uuid,uuid,text)',
        'public._flow_engine_admit_native_flow(public.flow_controls,public.webhook_events,uuid,text,timestamptz,bigint,bigint,text)',
        'public.admit_flow_engine_event_v1(jsonb,jsonb)',
        'public._flow_engine_authorize_native_tail(uuid,uuid,public.flow_engine_runs,public.recovery_cases,public.flow_controls,timestamptz)'
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

    revoke all on table public.flow_engine_pools, public.flow_cohort_members, public.flow_engine_native_runs
        from public;
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on table public.flow_engine_pools, public.flow_cohort_members, '
            'public.flow_engine_native_runs from %I', v_role);
    end loop;

    if to_regrole('service_role') is not null then
        grant execute on function public.admit_flow_engine_event_v1(jsonb, jsonb) to service_role;
    end if;
end;
$acl$;

commit;
