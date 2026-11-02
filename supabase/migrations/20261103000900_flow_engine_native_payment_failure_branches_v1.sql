-- Migration: las cuatro ramas del pago rechazado en el motor nativo, y el
-- boton del checkout en su paso (P-16, E-306, fase A del plan de E-302: A4;
-- flow-engine-v2 2.15.0, seccion 27).
--
-- Por que. Dan decidio el 2026-11-02 que el pago rechazado de Maribel lleve
-- las cuatro versiones de ATT1 (D1, «Las 4»), por el motor nativo (D4), con el
-- boton de nuestro link (D5) y el cupon fijo PROTOCOLO10 en las cuatro (D10).
-- El esquema 1.4 (el PR anterior) ya deja escribir la cadena de condiciones
-- con los tres datos del pago rechazado (tipo_de_rechazo, rechazos,
-- descuento_previo); la base todavia la rechazaba: _flow_engine_native_unsupported
-- da motor_nativo:forma con una condicion, el compilador no compila una
-- condicion ni un cupon fijo (fixed_coupon), el contexto del paso solo ve las
-- plantillas de arriba de todo, y la reserva del link no acepta una accion
-- nativa.
--
-- Que cambia, sobre las definiciones vivas (cada reemplazo contado, con su
-- marcador; si el texto no esta exactamente una vez, aborta con 55000 y no
-- cambia nada):
--   1. _flow_engine_native_payment_failure_branches(jsonb) y
--      _flow_engine_native_payment_failure_send_ok(jsonb) (nuevas, de nadie de
--      la API): la forma que ejecuta la base. Solo con pago_fallido: antes de
--      la cadena solo esperas; la cadena es el ultimo paso, hasta tres niveles,
--      cada condicion con un dato del pago rechazado y valores de ese dato, y
--      cada rama exactamente una plantilla, con boton {link: checkout} y,
--      opcional, un cupon fijo (nunca personal). Devuelve las ramas en orden
--      ([{"cuando": {dato, es} | null, "paso": {...}}]), o null.
--   2. _flow_engine_native_unsupported: con pago_fallido, una cadena con otra
--      forma, o un boton que no es ese, es motor_nativo:forma; la cadena valida
--      se abre.
--   3. _flow_engine_native_steps('nativo:pago_fallido') suma "cupon_fijo": true,
--      y el compilador acepta un cupon fijo en una forma que lo trae (con el
--      link checkout); con la cadena, compila cada rama con el mismo camino y
--      devuelve la politica de una (un envio: la condicion elige uno).
--   4. _flow_engine_sends: con la cadena de un pago rechazado nativo, el envio
--      es uno, el de la ultima rama (el si_no final); flow_engine_execution_context_v1
--      se vuelve a crear con template_branches al final (las ramas, en un
--      recorrido nativo; null en todo lo demas).
--   5. _flow_engine_native_payment_failure_data (nueva, de nadie de la API): los
--      tres datos del recorrido. get_flow_engine_native_payment_failure_context_v1
--      (service_role) los da con el cerco de la lease.
--   6. flow_engine_native_branch_choices (solo suma, con RLS, sin roles de la
--      API) y record_flow_engine_native_branch_v1 (service_role): la eleccion
--      del bridge, antes del envio, contra los datos que la base vuelve a leer.
--   7. reserve_recovery_checkout_issuance_v1 acepta el primer envio de un pago
--      rechazado nativo (ancla flow_engine_event) y, como el caso nativo nace
--      con offer_code nulo, usa la oferta del rechazo (context.flow_engine_offer).
--   8. read_flow_sales_v1 suma by_branch: por plantilla de rama, los envios
--      aceptados en la ventana y las ventas del flujo (por caso o por
--      identidad) cuyo ultimo envio a la persona fue de esa rama.
--
-- Que NO cambia. Ningun flujo de hoy: sin un pago rechazado nativo con boton o
-- con la cadena, las formas, el compilador, el contexto, la reserva y las
-- ventas devuelven lo mismo (lo fija validate_flow_engine_native_payment_failure_branches.mjs
-- con compile_cases.json y los flujos nativos de antes). El legado de ATT1 y de
-- Maribel no pasa por ninguna funcion nueva.
--
-- Grants: create or replace conserva los privilegios; el contexto se vuelve a
-- crear y vuelve a ser solo de service_role. Correrla dos veces aborta con
-- 55000 sin cambiar nada.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $guard$
begin
    if to_regprocedure('public._flow_engine_native_payment_failure_branches(jsonb)') is not null
       or to_regclass('public.flow_engine_native_branch_choices') is not null then
        raise exception using errcode = '55000',
            message = 'flow_engine_native_payment_failure_branches_already_applied';
    end if;
    if to_regprocedure('public._flow_engine_native_trigger(jsonb)') is null
       or to_regprocedure('public.flow_engine_execution_context_v1(uuid)') is null
       or to_regprocedure('public.read_flow_sales_v1(jsonb,timestamptz)') is null
       or to_regprocedure('public.recovery_coupon_was_delivered(uuid)') is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_payment_failure_missing';
    end if;
end;
$guard$;

-- ===========================================================================
-- Bloque 1. La forma de la cadena.
-- ===========================================================================

-- Una plantilla de una rama: solo plantilla (un nombre), idioma, variables y
-- boton; el boton, si esta, es el link checkout con, opcional, un codigo fijo
-- (la forma de followup_discount.COUPON_CODE_RE, nunca personal).
create function public._flow_engine_native_payment_failure_send_ok(p_step jsonb)
returns boolean
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select coalesce(
        jsonb_typeof(p_step) = 'object'
        and jsonb_typeof(p_step -> 'plantilla') = 'string'
        and p_step - 'plantilla' - 'idioma' - 'variables' - 'boton' = '{}'::jsonb
        and (not p_step ? 'boton'
             or (jsonb_typeof(p_step -> 'boton') = 'object'
                 and (p_step -> 'boton') - 'link' - 'cupon' = '{}'::jsonb
                 and p_step #> '{boton,link}' = '"checkout"'::jsonb
                 and (not (p_step -> 'boton') ? 'cupon'
                      or (jsonb_typeof(p_step #> '{boton,cupon}') = 'string'
                          and p_step #>> '{boton,cupon}' ~ '^[A-Za-z0-9_-]{1,64}$'
                          and p_step #>> '{boton,cupon}' <> 'personal')))),
        false)
$function$;

-- Las ramas de la cadena de un pago rechazado nativo, en el orden en que se
-- eligen, o null si la definicion no tiene esa forma (o no es un pago
-- rechazado). La primera rama cuya condicion se cumple gana; la ultima, con
-- "cuando": null, es el si_no final.
create function public._flow_engine_native_payment_failure_branches(p_definition jsonb)
returns jsonb
language plpgsql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    c_values constant jsonb :=
        '{"tipo_de_rechazo": ["sin_saldo", "banco"], "rechazos": ["uno", "varios"], "descuento_previo": ["si", "no"]}';
    v_steps jsonb;
    v_node jsonb;
    v_condition jsonb;
    v_rest jsonb;
    v_branches jsonb := '[]'::jsonb;
    v_level integer := 0;
begin
    if public._flow_engine_native_trigger(p_definition) is distinct from 'pago_fallido'
       or jsonb_typeof(p_definition -> 'pasos') is distinct from 'array'
       or jsonb_array_length(p_definition -> 'pasos') = 0 then
        return null;
    end if;
    v_steps := p_definition -> 'pasos';
    v_node := v_steps -> -1;
    if jsonb_typeof(v_node) is distinct from 'object' or not v_node ? 'condicion' then
        return null;
    end if;
    -- Antes de la cadena, solo esperas: la gracia es una sola, antes de elegir.
    if exists (select 1 from jsonb_array_elements(v_steps - (jsonb_array_length(v_steps) - 1)) step(value)
                where jsonb_typeof(step.value) is distinct from 'object'
                   or step.value - 'esperar' <> '{}'::jsonb
                   or not step.value ? 'esperar') then
        return null;
    end if;
    loop
        v_level := v_level + 1;
        v_condition := v_node -> 'condicion';
        if v_node - 'condicion' - 'entonces' - 'si_no' <> '{}'::jsonb
           or jsonb_typeof(v_condition) is distinct from 'object'
           or v_condition - 'dato' - 'es' <> '{}'::jsonb
           or jsonb_typeof(v_condition -> 'dato') is distinct from 'string'
           or not c_values ? (v_condition ->> 'dato')
           or jsonb_typeof(v_condition -> 'es') is distinct from 'array'
           or jsonb_array_length(v_condition -> 'es') = 0
           or exists (select 1 from jsonb_array_elements(v_condition -> 'es') value(item)
                       where not (c_values -> (v_condition ->> 'dato')) @> jsonb_build_array(value.item))
           or jsonb_typeof(v_node -> 'entonces') is distinct from 'array'
           or jsonb_array_length(v_node -> 'entonces') <> 1
           or not public._flow_engine_native_payment_failure_send_ok(v_node -> 'entonces' -> 0) then
            return null;
        end if;
        v_branches := v_branches || jsonb_build_array(
            jsonb_build_object('cuando', v_condition, 'paso', v_node -> 'entonces' -> 0));
        v_rest := coalesce(v_node -> 'si_no', '[]'::jsonb);
        if jsonb_typeof(v_rest) is distinct from 'array' or jsonb_array_length(v_rest) <> 1 then
            return null;
        end if;
        if v_level < 3 and jsonb_typeof(v_rest -> 0) = 'object' and (v_rest -> 0) ? 'condicion' then
            v_node := v_rest -> 0;
            continue;
        end if;
        if not public._flow_engine_native_payment_failure_send_ok(v_rest -> 0) then
            return null;
        end if;
        return v_branches || jsonb_build_array(jsonb_build_object('cuando', null, 'paso', v_rest -> 0));
    end loop;
end;
$function$;

-- ===========================================================================
-- Bloque 2. Lo que abre la base, la forma y el compilador.
-- ===========================================================================

do $native_shape$
declare
    c_unsupported constant text := 'public._flow_engine_native_unsupported(jsonb)';
    c_steps constant text := 'public._flow_engine_native_steps(text)';
    c_core constant text := 'public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)';
    c_old_unsupported constant text :=
        E'        -- flow_engine_native_cart_shape: un solo envio, y solo esperar, plantilla y\n';
    c_new_unsupported constant text :=
        E'        -- flow_engine_native_payment_failure_branches: begin (20261103000900)\n'
        || E'        -- El pago rechazado con su cadena de condiciones al final, una plantilla\n'
        || E'        -- por rama (_flow_engine_native_payment_failure_branches), se abre; con\n'
        || E'        -- otra forma de cadena, motor_nativo:forma. Y el boton de su plantilla, si\n'
        || E'        -- esta, es el link checkout sin cupon o con un codigo fijo.\n'
        || E'        when public._flow_engine_native_trigger(p_definition) = ''pago_fallido''\n'
        || E'             and exists (select 1 from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array''\n'
        || E'                                                                 then p_definition -> ''pasos'' else ''[]''::jsonb end) step(value)\n'
        || E'                          where jsonb_typeof(step.value) = ''object'' and step.value ? ''condicion'')\n'
        || E'            then case when public._flow_engine_native_payment_failure_branches(p_definition) is null\n'
        || E'                      then ''motor_nativo:forma'' end\n'
        || E'        when public._flow_engine_native_trigger(p_definition) = ''pago_fallido''\n'
        || E'             and exists (select 1 from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array''\n'
        || E'                                                                 then p_definition -> ''pasos'' else ''[]''::jsonb end) step(value)\n'
        || E'                          where jsonb_typeof(step.value) = ''object'' and step.value ? ''plantilla''\n'
        || E'                            and step.value ? ''boton''\n'
        || E'                            and not public._flow_engine_native_payment_failure_send_ok(step.value))\n'
        || E'            then ''motor_nativo:forma''\n'
        || E'        -- flow_engine_native_payment_failure_branches: end\n'
        || c_old_unsupported;
    c_old_shape constant text :=
        E'        when ''nativo:pago_fallido'' then\n'
        || E'            ''[{"step_key": "first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto"], "variables_de": ["nombre", "producto"], "coupon_variables": null}]''::jsonb\n';
    c_new_shape constant text :=
        E'        when ''nativo:pago_fallido'' then\n'
        || E'            -- flow_engine_native_payment_failure_fixed_coupon: y el boton del checkout con un\n'
        || E'            -- codigo fijo (20261103000900).\n'
        || E'            ''[{"step_key": "first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto"], "variables_de": ["nombre", "producto"], "coupon_variables": null, "cupon_fijo": true}]''::jsonb\n';
    c_old_declare constant text :=
        E'    v_row public.followup_policy_versions%rowtype;\nbegin\n';
    c_new_declare constant text :=
        E'    v_row public.followup_policy_versions%rowtype;\n'
        || E'    -- flow_engine_native_payment_failure_branches (20261103000900).\n'
        || E'    v_branches jsonb;\n'
        || E'    v_branch jsonb;\n'
        || E'begin\n';
    c_old_path constant text :=
        E'    -- Los pasos del unico camino que ejecuta la v1 (flow_policy._path).\n';
    c_new_path constant text :=
        E'    -- flow_engine_native_payment_failure_branches: begin (20261103000900)\n'
        || E'    -- La cadena de un pago rechazado nativo: cada rama, con las esperas de antes,\n'
        || E'    -- compila a la misma politica (un envio); se devuelve la de una. Una cadena\n'
        || E'    -- con otra forma no compila.\n'
        || E'    if p_legacy_kind = ''nativo:pago_fallido'' and jsonb_typeof(p_definition -> ''pasos'') = ''array''\n'
        || E'       and jsonb_array_length(p_definition -> ''pasos'') > 0\n'
        || E'       and jsonb_typeof(p_definition -> ''pasos'' -> -1) = ''object''\n'
        || E'       and (p_definition -> ''pasos'' -> -1) ? ''condicion'' then\n'
        || E'        v_branches := public._flow_engine_native_payment_failure_branches(p_definition);\n'
        || E'        if v_branches is null then\n'
        || E'            raise exception using errcode = ''22023'', message = c_refuse || ''ramas_invalidas'';\n'
        || E'        end if;\n'
        || E'        for v_branch in select value from jsonb_array_elements(v_branches) loop\n'
        || E'            v_row := public._flow_engine_compile_policy_core(\n'
        || E'                jsonb_set(p_definition, ''{pasos}'',\n'
        || E'                          ((p_definition -> ''pasos'') - (jsonb_array_length(p_definition -> ''pasos'') - 1))\n'
        || E'                          || jsonb_build_array(v_branch -> ''paso'')),\n'
        || E'                p_flow_id, p_version, p_legacy_kind, p_fixed_zone);\n'
        || E'        end loop;\n'
        || E'        return v_row;\n'
        || E'    end if;\n'
        || E'    -- flow_engine_native_payment_failure_branches: end\n'
        || E'\n'
        || c_old_path;
    c_old_coupon constant text :=
        E'        elsif v_coupon is not null then\n'
        || E'            raise exception using errcode = ''22023'', message = c_refuse || ''fixed_coupon:'' || v_slot_key;\n';
    c_new_coupon constant text :=
        E'        -- flow_engine_native_payment_failure_fixed_coupon: una forma con cupon_fijo (el pago\n'
        || E'        -- rechazado nativo, 20261103000900) acepta un codigo fijo en el link checkout.\n'
        || E'        elsif v_coupon is not null and not (coalesce(v_slot -> ''cupon_fijo'' = ''true''::jsonb, false)\n'
        || E'                                            and jsonb_typeof(v_coupon) = ''string''\n'
        || E'                                            and v_coupon #>> ''{}'' ~ ''^[A-Za-z0-9_-]{1,64}$''\n'
        || E'                                            and v_step #>> ''{boton,link}'' = ''checkout'') then\n'
        || E'            raise exception using errcode = ''22023'', message = c_refuse || ''fixed_coupon:'' || v_slot_key;\n';
    v_text text;
begin
    if to_regprocedure(c_unsupported) is null or to_regprocedure(c_steps) is null or to_regprocedure(c_core) is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_payment_failure_missing';
    end if;

    -- 1. Lo que abre la base.
    v_text := pg_get_functiondef(c_unsupported::regprocedure);
    if position('flow_engine_native_payment_failure_branches' in v_text) > 0
       or length(v_text) - length(replace(v_text, c_old_unsupported, '')) <> length(c_old_unsupported) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_unsupported_definition',
            detail = c_unsupported;
    end if;
    execute replace(v_text, c_old_unsupported, c_new_unsupported);

    -- 2. La forma del pago rechazado, con el cupon fijo.
    v_text := pg_get_functiondef(c_steps::regprocedure);
    if position('cupon_fijo' in v_text) > 0
       or length(v_text) - length(replace(v_text, c_old_shape, '')) <> length(c_old_shape) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_steps_definition',
            detail = c_steps;
    end if;
    execute replace(v_text, c_old_shape, c_new_shape);

    -- 3. El compilador: la cadena y el cupon fijo.
    v_text := pg_get_functiondef(c_core::regprocedure);
    if position('flow_engine_native_payment_failure' in v_text) > 0
       or length(v_text) - length(replace(v_text, c_old_declare, '')) <> length(c_old_declare)
       or length(v_text) - length(replace(v_text, c_old_path, '')) <> length(c_old_path)
       or length(v_text) - length(replace(v_text, c_old_coupon, '')) <> length(c_old_coupon) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_compile_definition',
            detail = c_core;
    end if;
    execute replace(replace(replace(v_text, c_old_declare, c_new_declare), c_old_path, c_new_path),
                    c_old_coupon, c_new_coupon);

    if position('flow_engine_native_payment_failure_branches: end' in pg_get_functiondef(c_unsupported::regprocedure)) = 0
       or position('"cupon_fijo": true' in pg_get_functiondef(c_steps::regprocedure)) = 0
       or position('flow_engine_native_payment_failure_branches: end' in pg_get_functiondef(c_core::regprocedure)) = 0
       or position('flow_engine_native_payment_failure_fixed_coupon' in pg_get_functiondef(c_core::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_payment_failure_shape_not_applied';
    end if;
end;
$native_shape$;

-- ===========================================================================
-- Bloque 3. El envio del paso y sus ramas.
-- ===========================================================================

do $sends$
declare
    c_sends constant text := 'public._flow_engine_sends(jsonb)';
    c_old constant text :=
        E'     where jsonb_typeof(step.value) = ''object'' and step.value ? ''plantilla''\n'
        || E'     order by step.step_index\n';
    c_new constant text :=
        E'     where jsonb_typeof(step.value) = ''object'' and step.value ? ''plantilla''\n'
        || E'       -- flow_engine_native_payment_failure_branches: sin la cadena (20261103000900).\n'
        || E'       and public._flow_engine_native_payment_failure_branches(p_definition) is null\n'
        || E'    -- Con la cadena de un pago rechazado nativo, un envio: el de la ultima rama (el\n'
        || E'    -- si_no final). La rama que sale la elige el bridge con\n'
        || E'    -- flow_engine_execution_context_v1.template_branches.\n'
        || E'    union all\n'
        || E'    select 1, branch.paso ->> ''plantilla'', branch.paso ->> ''idioma'', branch.paso -> ''variables'',\n'
        || E'           branch.paso -> ''boton''\n'
        || E'      from (select public._flow_engine_native_payment_failure_branches(p_definition) -> -1 -> ''paso'' as paso) branch\n'
        || E'     where branch.paso is not null\n'
        || E'     order by 1\n';
    v_text text;
begin
    v_text := pg_get_functiondef(c_sends::regprocedure);
    if position('flow_engine_native_payment_failure' in v_text) > 0
       or length(v_text) - length(replace(v_text, c_old, '')) <> length(c_old) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_sends_definition', detail = c_sends;
    end if;
    execute replace(v_text, c_old, c_new);
end;
$sends$;

do $context_shape$
declare
    v_definition text := pg_get_functiondef('public.flow_engine_execution_context_v1(uuid)'::regprocedure);
begin
    -- La definicion vigente es la de 20261028000200: sin ella, otra migracion la
    -- cambio y esto no la conoce.
    if position('public._flow_engine_native_run_steps(run.recovery_case_id)' in v_definition) = 0
       or position('send.boton, run.legacy_dispatch_ok' in v_definition) = 0
       or position('flow_engine_native_cart_permission' in v_definition) = 0
       or position('template_branches' in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_execution_context_definition';
    end if;
end;
$context_shape$;

drop function public.flow_engine_execution_context_v1(uuid);

-- La de 20261028000200, con template_branches al final: las ramas de la cadena
-- de un pago rechazado nativo (las que elige el bridge), en un recorrido nativo.
create function public.flow_engine_execution_context_v1(p_action_id uuid)
returns table (flow_id uuid, version integer, product_ref text, flow_key text, step_key text,
               send_ordinal integer, template_name text, template_language text, template_variables text[],
               template_button jsonb, legacy_dispatch_ok boolean, entry_permission text,
               template_branches jsonb)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    select run.flow_id, run.version, run.product_ref, run.flow_key, action_row.step_key,
           position_row.ordinal, send.plantilla, send.idioma,
           array(select jsonb_array_elements_text(coalesce(send.variables, '[]'::jsonb))),
           send.boton, run.legacy_dispatch_ok,
           -- flow_engine_native_cart_permission: el permiso de la version fijada (20261028000200).
           definition_row.definition #>> '{entra,permiso}',
           -- flow_engine_native_payment_failure_branches: las ramas, solo en un recorrido nativo (20261103000900).
           case when exists (select 1 from public.flow_engine_native_runs native
                              where native.recovery_case_id = action_row.recovery_case_id)
                then public._flow_engine_native_payment_failure_branches(definition_row.definition) end
      from public.scheduled_actions action_row
      join public.flow_engine_runs run on run.recovery_case_id = action_row.recovery_case_id
      join public.flow_definitions definition_row
        on definition_row.flow_id = run.flow_id and definition_row.version = run.version
      cross join lateral (
          select shape.step_index::integer as ordinal
            -- flow_engine_native_steps: la forma del disparador en un recorrido nativo.
            from jsonb_array_elements(coalesce(public._flow_engine_legacy_steps(run.legacy_kind),
                                               public._flow_engine_native_run_steps(run.recovery_case_id)))
                 with ordinality as shape(step, step_index)
           where shape.step ->> 'step_key' = action_row.step_key
      ) position_row
      join lateral public._flow_engine_sends(definition_row.definition) send
        on send.ordinal = position_row.ordinal
     where action_row.id = p_action_id
$function$;

-- ===========================================================================
-- Bloque 4. Los tres datos del recorrido.
-- ===========================================================================

-- Los datos de un caso de un pago rechazado nativo, con la regla del contrato
-- (flow-engine-v2 §27):
--   - rechazos: el que abrio el caso (abandonment_event_id) y los que se le
--     sumaron (recovery_case_events con payment_failure); uno o varios;
--   - tipo_de_rechazo: payment_failure_refusal_kind del ultimo, por su
--     creation_date (insufficient_funds es sin_saldo; todo lo demas, banco,
--     como B1-banco del legado);
--   - descuento_previo: si la persona ya recibio de nosotros un descuento antes
--     del primer rechazo del caso: un envio de recuperacion aceptado por
--     Chatwoot en otro caso suyo del mismo producto (no la bienvenida de la
--     compra), un seguimiento con cupon mandado en una conversacion suya, o un
--     cupon personal vigente de otro caso que le llego (la regla de B1-cupon
--     del legado, recovery_coupon_was_delivered, a mas de una hora de vencer).
create function public._flow_engine_native_payment_failure_data(p_recovery_case_id uuid, p_first_at timestamptz,
                                                                 p_now timestamptz)
returns table (tipo_de_rechazo text, rechazos text, descuento_previo text, refusal_count integer,
               latest_failed_at timestamptz)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    with case_row as (
        select rc.* from public.recovery_cases rc where rc.id = p_recovery_case_id
    ),
    refusals as (
        select event.id,
               public.payment_failure_refusal_kind(
                   coalesce(details.refusal_reason, event.payload #>> '{data,purchase,payment,refusal_reason}'),
                   event.payload #>> '{data,purchase,payment,type}') as kind,
               case when event.payload ->> 'creation_date' ~ '^[0-9]{10,16}$'
                    then to_timestamp((event.payload ->> 'creation_date')::double precision / 1000.0)
                    else event.received_at end as failed_at
          from public.webhook_events event
          left join public.commercial_ally_payment_failure_details details on details.webhook_event_id = event.id
         where event.id = (select case_row.abandonment_event_id from case_row)
            or event.id in (select e.webhook_event_id from public.recovery_case_events e
                             where e.recovery_case_id = p_recovery_case_id and e.event_role = 'payment_failure')
    ),
    latest as (
        select refusals.kind, refusals.failed_at from refusals order by refusals.failed_at desc, refusals.id desc limit 1
    )
    select case (select latest.kind from latest) when 'insufficient_funds' then 'sin_saldo' else 'banco' end,
           case when (select count(*) from refusals) >= 2 then 'varios' else 'uno' end,
           case when exists (
                    select 1
                      from public.followup_delivery_attempts attempt
                      join public.scheduled_actions other_action on other_action.id = attempt.action_id
                      join public.recovery_cases other_case on other_case.id = other_action.recovery_case_id
                     where other_case.contact_id = (select case_row.contact_id from case_row)
                       and other_case.id <> p_recovery_case_id
                       and other_case.external_product_id = (select case_row.external_product_id from case_row)
                       and coalesce(other_case.context ->> 'flow_engine_trigger', '') <> 'compra'
                       and attempt.outcome = 'accepted_by_chatwoot'
                       and attempt.accepted_at < p_first_at)
                  or exists (
                    select 1
                      from public.conversation_followup_events followup
                      join public.conversations conversation on conversation.id = followup.conversation_id
                     where conversation.contact_id = (select case_row.contact_id from case_row)
                       and followup.status = 'sent'
                       and followup.settled_at < p_first_at)
                  or exists (
                    select 1
                      from public.recovery_coupons coupon
                      join public.recovery_cases other_case on other_case.id = coupon.recovery_case_id
                     where other_case.contact_id = (select case_row.contact_id from case_row)
                       and other_case.id <> p_recovery_case_id
                       and coupon.external_product_id = (select case_row.external_product_id from case_row)
                       and coupon.status = 'valid'
                       and coupon.expires_at > p_now + interval '1 hour'
                       and public.recovery_coupon_was_delivered(coupon.id))
                then 'si' else 'no' end,
           (select count(*)::integer from refusals),
           (select latest.failed_at from latest)
      from case_row
     where exists (select 1 from refusals)
$function$;

-- La accion nativa del pago rechazado con la lease vigente (el cerco de
-- get_portable_payment_failure_send_context_v1), o 55000.
create function public._flow_engine_native_payment_failure_action(
    p_action_id uuid, p_worker_id text, p_lease_generation bigint, p_now timestamptz)
returns public.scheduled_actions
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
begin
    if p_action_id is null
       or p_worker_id is null or btrim(p_worker_id) = ''
       or p_lease_generation is null or p_lease_generation < 1
       or p_now is null then
        raise exception using errcode = '22023', message = 'invalid_followup_fence';
    end if;
    select action.* into v_action
      from public.scheduled_actions action
     where action.id = p_action_id;
    if not found
       or not (
           v_action.lease_owner = p_worker_id
           and v_action.lease_generation = p_lease_generation
           and v_action.lease_expires_at > p_now
           and v_action.expires_at > p_now
           and v_action.status in ('pending', 'deferred', 'retryable_failed')
       ) then
        raise exception using errcode = '55000', message = 'current_action_lease_not_found';
    end if;
    if v_action.action_type <> 'first_contact_review'
       or v_action.anchor_type is distinct from 'flow_engine_event'
       or v_action.recovery_case_id is null
       or not exists (select 1 from public.flow_engine_native_runs native
                       where native.recovery_case_id = v_action.recovery_case_id
                         and native.trigger_event = 'pago_fallido') then
        raise exception using errcode = '55000', message = 'flow_engine_native_payment_failure_action_required';
    end if;
    return v_action;
end;
$function$;

create function public.get_flow_engine_native_payment_failure_context_v1(
    p_action_id uuid, p_worker_id text, p_lease_generation bigint, p_now timestamptz)
returns table (tipo_de_rechazo text, rechazos text, descuento_previo text, refusal_count integer,
               latest_failed_at timestamptz)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
begin
    v_action := public._flow_engine_native_payment_failure_action(p_action_id, p_worker_id, p_lease_generation, p_now);
    return query
    select data.* from public._flow_engine_native_payment_failure_data(
        v_action.recovery_case_id, v_action.anchor_observed_at, p_now) data;
    if not found then
        raise exception using errcode = '55000', message = 'payment_failure_details_missing';
    end if;
end;
$function$;

-- ===========================================================================
-- Bloque 5. La eleccion de la rama, antes del envio.
-- ===========================================================================

-- Una fila por intento: la rama que eligio el bridge, con los datos que la
-- base volvio a leer. Solo ids y codigos. Solo suma.
create table public.flow_engine_native_branch_choices (
    delivery_attempt_id uuid primary key references public.followup_delivery_attempts (id) on delete restrict,
    action_id uuid not null references public.scheduled_actions (id) on delete restrict,
    recovery_case_id uuid not null references public.recovery_cases (id) on delete restrict,
    flow_id uuid not null references public.flow_controls (flow_id) on delete restrict,
    version integer not null check (version > 0),
    branch_index integer not null check (branch_index between 0 and 3),
    template_name text not null check (template_name ~ '^[a-z0-9_]+$' and length(template_name) <= 512),
    tipo_de_rechazo text not null check (tipo_de_rechazo in ('sin_saldo', 'banco')),
    rechazos text not null check (rechazos in ('uno', 'varios')),
    descuento_previo text not null check (descuento_previo in ('si', 'no')),
    created_at timestamptz not null default clock_timestamp()
);

create index flow_engine_native_branch_choices_flow_idx
on public.flow_engine_native_branch_choices (flow_id, template_name);
create index flow_engine_native_branch_choices_case_idx
on public.flow_engine_native_branch_choices (recovery_case_id);

alter table public.flow_engine_native_branch_choices enable row level security;

create trigger flow_engine_native_branch_choices_append_only
before update or delete on public.flow_engine_native_branch_choices
for each row execute function public.reject_flow_engine_native_mutation();
create trigger flow_engine_native_branch_choices_no_truncate
before truncate on public.flow_engine_native_branch_choices
for each statement execute function public.reject_flow_engine_native_mutation();

-- La primera rama cuya condicion se cumple con esos datos (la ultima, sin
-- condicion, siempre).
create function public._flow_engine_native_payment_failure_choice(
    p_branches jsonb, p_tipo_de_rechazo text, p_rechazos text, p_descuento_previo text)
returns integer
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select (min(branch.ordinality) - 1)::integer
      from jsonb_array_elements(p_branches) with ordinality as branch(value, ordinality)
     where branch.value -> 'cuando' = 'null'::jsonb
        or (branch.value -> 'cuando' -> 'es') @> jsonb_build_array(
               case branch.value #>> '{cuando,dato}'
                   when 'tipo_de_rechazo' then p_tipo_de_rechazo
                   when 'rechazos' then p_rechazos
                   when 'descuento_previo' then p_descuento_previo end)
$function$;

create function public.record_flow_engine_native_branch_v1(
    p_action_id uuid, p_attempt_id uuid, p_worker_id text, p_lease_generation bigint, p_branch_index integer,
    p_now timestamptz)
returns table (outcome text, template_name text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_action public.scheduled_actions%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_run public.flow_engine_runs%rowtype;
    v_branches jsonb;
    v_data record;
    v_choice integer;
    v_template text;
    v_record public.flow_engine_native_branch_choices%rowtype;
begin
    if p_attempt_id is null or p_branch_index is null or p_branch_index < 0 or p_branch_index > 3 then
        raise exception using errcode = '22023', message = 'invalid_flow_engine_native_branch';
    end if;
    v_action := public._flow_engine_native_payment_failure_action(p_action_id, p_worker_id, p_lease_generation, p_now);

    select attempt.* into v_attempt
      from public.followup_delivery_attempts attempt
     where attempt.id = p_attempt_id
       and attempt.action_id = p_action_id
       and attempt.lease_generation = p_lease_generation;
    if not found then
        raise exception using errcode = '55000', message = 'flow_engine_native_branch_attempt_not_found';
    end if;

    select run.* into v_run from public.flow_engine_runs run where run.recovery_case_id = v_action.recovery_case_id;
    select public._flow_engine_native_payment_failure_branches(definition_row.definition) into v_branches
      from public.flow_definitions definition_row
     where definition_row.flow_id = v_run.flow_id and definition_row.version = v_run.version;
    if v_branches is null or p_branch_index >= jsonb_array_length(v_branches) then
        raise exception using errcode = '55000', message = 'flow_engine_native_branch_unknown';
    end if;
    v_template := v_branches -> p_branch_index -> 'paso' ->> 'plantilla';

    select choice.* into v_record
      from public.flow_engine_native_branch_choices choice
     where choice.delivery_attempt_id = p_attempt_id;
    if found then
        if v_record.branch_index is distinct from p_branch_index then
            raise exception using errcode = '55000', message = 'flow_engine_native_branch_conflict';
        end if;
        return query select 'already_recorded'::text, v_record.template_name;
        return;
    end if;

    -- Se registra antes del envio: un intento que ya arranco no se registra.
    if v_attempt.phase <> 'reserved' then
        raise exception using errcode = '55000', message = 'flow_engine_native_branch_attempt_not_reserved';
    end if;

    -- Los datos de ahora: si cambiaron desde que el bridge los leyo (un rechazo
    -- nuevo), la rama elegida ya no es la que corresponde.
    select data.* into v_data
      from public._flow_engine_native_payment_failure_data(
          v_action.recovery_case_id, v_action.anchor_observed_at, p_now) data;
    if not found then
        raise exception using errcode = '55000', message = 'payment_failure_details_missing';
    end if;
    v_choice := public._flow_engine_native_payment_failure_choice(
        v_branches, v_data.tipo_de_rechazo, v_data.rechazos, v_data.descuento_previo);
    if v_choice is distinct from p_branch_index then
        raise exception using errcode = '55000', message = 'flow_engine_native_branch_stale';
    end if;

    insert into public.flow_engine_native_branch_choices (
        delivery_attempt_id, action_id, recovery_case_id, flow_id, version, branch_index, template_name,
        tipo_de_rechazo, rechazos, descuento_previo, created_at)
    values (
        p_attempt_id, p_action_id, v_action.recovery_case_id, v_run.flow_id, v_run.version, p_branch_index,
        v_template, v_data.tipo_de_rechazo, v_data.rechazos, v_data.descuento_previo, p_now);

    return query select 'recorded'::text, v_template;
end;
$function$;

-- ===========================================================================
-- Bloque 6. El link del primer envio nativo del pago rechazado.
-- ===========================================================================

do $issuance$
declare
    c_reserve constant text := 'public.reserve_recovery_checkout_issuance_v1(uuid,text,bigint,uuid,text,timestamptz)';
    c_old_anchor constant text :=
        E'           or v_action.anchor_type not in (''cart_abandonment'', ''payment_failure'')\n';
    c_new_anchor constant text :=
        E'           or (v_action.anchor_type not in (''cart_abandonment'', ''payment_failure'')\n'
        || E'               -- flow_engine_native_payment_failure_branches: y el primer envio de un pago\n'
        || E'               -- rechazado nativo (20261103000900).\n'
        || E'               and not (v_action.anchor_type = ''flow_engine_event''\n'
        || E'                        and exists (select 1 from public.flow_engine_native_runs native\n'
        || E'                                     where native.recovery_case_id = v_action.recovery_case_id\n'
        || E'                                       and native.trigger_event = ''pago_fallido'')))\n';
    c_old_case constant text :=
        E'        return query select ''blocked_case''::text, null::uuid, null::text, null::text;\n'
        || E'        return;\n'
        || E'    end if;\n';
    c_new_case constant text := c_old_case
        || E'    -- flow_engine_native_payment_failure_offer: el caso nativo nace con offer_code\n'
        || E'    -- nulo (la oferta del rechazo va en context.flow_engine_offer, 20261026000200):\n'
        || E'    -- el link va a la oferta que la persona intento pagar (20261103000900).\n'
        || E'    if v_action.anchor_type = ''flow_engine_event'' and nullif(btrim(v_case.offer_code), '''') is null then\n'
        || E'        v_case.offer_code := nullif(btrim(v_case.context ->> ''flow_engine_offer''), '''');\n'
        || E'    end if;\n';
    v_text text;
begin
    v_text := pg_get_functiondef(c_reserve::regprocedure);
    if position('flow_engine_native_payment_failure' in v_text) > 0
       or length(v_text) - length(replace(v_text, c_old_anchor, '')) <> length(c_old_anchor)
       or length(v_text) - length(replace(v_text, c_old_case, '')) <> length(c_old_case) then
        raise exception using errcode = '55000', message = 'unexpected_recovery_checkout_issuance_definition',
            detail = c_reserve;
    end if;
    execute replace(replace(v_text, c_old_anchor, c_new_anchor), c_old_case, c_new_case);
    v_text := pg_get_functiondef(c_reserve::regprocedure);
    if position('flow_engine_native_payment_failure_branches' in v_text) = 0
       or position('flow_engine_native_payment_failure_offer' in v_text) = 0 then
        raise exception using errcode = '55000', message = 'recovery_checkout_issuance_not_patched';
    end if;
end;
$issuance$;

-- ===========================================================================
-- Bloque 7. Lo que ve el AE: las ventas por rama.
-- ===========================================================================

do $sales$
declare
    c_sales constant text := 'public.read_flow_sales_v1(jsonb,timestamptz)';
    c_old_cte constant text :=
        E'        select by_link.*, ''link''::text from by_link\n'
        || E'    )\n';
    c_new_cte constant text := c_old_cte
        || E'    -- flow_engine_native_payment_failure_branch_sales: begin (20261103000900)\n'
        || E'    -- Las ramas del pago rechazado nativo: cada eleccion con su envio aceptado, y cada\n'
        || E'    -- venta del flujo despues de un envio (la regla de arriba, por caso o por identidad)\n'
        || E'    -- en la rama del ultimo envio de esa persona antes de la compra.\n'
        || E'    , branch_sends as (\n'
        || E'        select choice.flow_id, choice.template_name, rc.contact_id, attempt.accepted_at\n'
        || E'          from public.flow_engine_native_branch_choices choice\n'
        || E'          join flows on flows.flow_id = choice.flow_id\n'
        || E'          join public.recovery_cases rc on rc.id = choice.recovery_case_id\n'
        || E'          join public.followup_delivery_attempts attempt on attempt.id = choice.delivery_attempt_id\n'
        || E'         where attempt.outcome = ''accepted_by_chatwoot'' and attempt.accepted_at is not null\n'
        || E'    ),\n'
        || E'    branch_sales as (\n'
        || E'        select branch.flow_id, branch.template_name, count(distinct sales.id)::integer as n\n'
        || E'          from sales\n'
        || E'          join purchases on purchases.id = sales.id\n'
        || E'          join lateral (\n'
        || E'              select branch_sends.flow_id, branch_sends.template_name\n'
        || E'                from branch_sends\n'
        || E'               where branch_sends.flow_id = sales.flow_id and branch_sends.contact_id = sales.person_ref\n'
        || E'                 and branch_sends.accepted_at <= purchases.purchased_at\n'
        || E'               order by branch_sends.accepted_at desc\n'
        || E'               limit 1) branch on true\n'
        || E'         where sales.step_key is not null\n'
        || E'         group by branch.flow_id, branch.template_name\n'
        || E'    )\n'
        || E'    -- flow_engine_native_payment_failure_branch_sales: end\n';
    c_old_key constant text :=
        E'               ''by_step'', coalesce((select jsonb_object_agg(step.step_key, step.n)\n';
    c_new_key constant text :=
        E'               -- flow_engine_native_payment_failure_branch_sales: por plantilla de rama.\n'
        || E'               ''by_branch'', coalesce((select jsonb_object_agg(branch.template_name, jsonb_build_object(\n'
        || E'                                          ''sends'', branch.sends, ''purchases'', coalesce((\n'
        || E'                                              select branch_sales.n from branch_sales\n'
        || E'                                               where branch_sales.flow_id = flows.flow_id\n'
        || E'                                                 and branch_sales.template_name = branch.template_name), 0)))\n'
        || E'                                      from (select branch_sends.template_name,\n'
        || E'                                                   count(*) filter (where branch_sends.accepted_at >= p_since\n'
        || E'                                                                     and branch_sends.accepted_at < v_now)::integer as sends\n'
        || E'                                              from branch_sends\n'
        || E'                                             where branch_sends.flow_id = flows.flow_id\n'
        || E'                                             group by branch_sends.template_name) branch), ''{}''::jsonb),\n'
        || c_old_key;
    v_text text;
begin
    v_text := pg_get_functiondef(c_sales::regprocedure);
    if position('flow_engine_native_payment_failure' in v_text) > 0
       or length(v_text) - length(replace(v_text, c_old_cte, '')) <> length(c_old_cte)
       or length(v_text) - length(replace(v_text, c_old_key, '')) <> length(c_old_key) then
        raise exception using errcode = '55000', message = 'unexpected_flow_sales_definition', detail = c_sales;
    end if;
    execute replace(replace(v_text, c_old_cte, c_new_cte), c_old_key, c_new_key);
end;
$sales$;

-- ===========================================================================
-- Los permisos. Las dos entradas del bridge y el contexto, solo de
-- service_role; las demas nuevas, de nadie de la API.
-- ===========================================================================

do $acl$
declare
    v_function regprocedure;
    v_role text;
    v_entrypoints constant regprocedure[] := array[
        'public.flow_engine_execution_context_v1(uuid)',
        'public.get_flow_engine_native_payment_failure_context_v1(uuid,text,bigint,timestamptz)',
        'public.record_flow_engine_native_branch_v1(uuid,uuid,text,bigint,integer,timestamptz)'
    ]::regprocedure[];
    v_private constant regprocedure[] := array[
        'public._flow_engine_native_payment_failure_send_ok(jsonb)',
        'public._flow_engine_native_payment_failure_branches(jsonb)',
        'public._flow_engine_native_payment_failure_data(uuid,timestamptz,timestamptz)',
        'public._flow_engine_native_payment_failure_action(uuid,text,bigint,timestamptz)',
        'public._flow_engine_native_payment_failure_choice(jsonb,text,text,text)'
    ]::regprocedure[];
begin
    foreach v_function in array v_entrypoints || v_private loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role')
             order by r.rolname
        loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
    end loop;
    if to_regrole('service_role') is not null then
        foreach v_function in array v_entrypoints loop
            execute format('grant execute on function %s to service_role', v_function);
        end loop;
    end if;
    -- La tabla, de nadie de la API (la escribe la funcion security definer).
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on table public.flow_engine_native_branch_choices from %I', v_role);
    end loop;
end;
$acl$;

commit;
