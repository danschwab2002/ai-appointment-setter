-- flow-engine-literal-variables-v1: un paso nativo con plantilla puede mandar un
-- texto fijo en un marcador, {"texto": "…"} en variables (F4 del mapa «Frenos ante
-- Meta y calentamiento», D15, 2026-11-27). Contrato: docs/contracts/flow-engine-v2.md
-- (§36).
--
-- Por que. Las plantillas de utilidad de Maribel (borradores del 27/11) dejan lo
-- que cambia de un flujo a otro en un marcador, y hoy un paso solo nombra
-- variables de una lista cerrada que llena el bridge (nombre, producto, ...): el
-- compilador de la politica rechaza cualquier otra cosa con
-- flow_policy_not_compilable:variables_differ. El unico camino que acepta un
-- texto literal es el masivo (las variables del masivo en flow_change_api), que no
-- es un flujo.
--
-- Que cambia:
--   1. _flow_engine_native_steps: cada toque de las formas nativo:compra,
--      nativo:carrito, nativo:pago_fallido, nativo:intencion y nativo:audiencia
--      suma "texto_fijo": true (la ficha no: su envio arma sus parametros por
--      nombre, D-F4.3; el silencio no pasa por estas formas y sigue exigiendo
--      nombre y producto, D17). variables y variables_de no cambian.
--   2. El compilador (_flow_engine_compile_policy_core), en un paso de una forma
--      con texto_fijo, acepta ademas de los nombres de variables_de (o de
--      coupon_variables_de, con el cupon propio) un objeto {"texto": t} con t de
--      1 a 400 caracteres, no en blanco, sin salto de linea, retorno, tab, cinco
--      espacios seguidos ni «{{». Los nombres siguen sin repetirse; dos textos
--      iguales si pueden ir. Un reemplazo contado de la condicion, con su marca.
--
-- Lo que NO cambia. Ninguna tabla, ningun permiso y ninguna otra funcion. La
-- definicion guarda el paso como venga (flow_definitions.definition es jsonb), y
-- flow_engine_execution_context_v1 le pasa al bridge el texto JSON del objeto en
-- template_variables (jsonb_array_elements_text), que el bridge decodifica
-- (supabase.get_flow_engine_step_context, D-F4.4). Los telefonos, los mails y los
-- links del texto los frena el validador del bridge, no la base. Un flujo sin
-- texto fijo compila igual que antes. Nadie lo escribe con
-- FLOW_LITERAL_VARIABLES_ENABLED apagado: la base lo acepta siempre (D15).
--
-- Como: el metodo de 20261126700000 (reemplazo contado sobre la definicion
-- VIGENTE, con su marca, conservando duenio y permisos). Correrla dos veces
-- aborta con 55000 sin cambiar nada.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_engine_native_steps(text)') is null
       or to_regprocedure('public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)') is null
       or position('flow_audience_event_hour_steps' in pg_get_functiondef(
              'public._flow_engine_native_steps(text)'::regprocedure)) = 0
       or position('flow_engine_native_variables: end' in pg_get_functiondef(
              'public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)'::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_literal_variables_missing';
    end if;
    if position('flow_literal_variables' in pg_get_functiondef(
              'public._flow_engine_native_steps(text)'::regprocedure)) > 0
       or position('flow_literal_variables' in pg_get_functiondef(
              'public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)'::regprocedure)) > 0 then
        raise exception using errcode = '55000', message = 'flow_engine_literal_variables_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. texto_fijo en los toques de las formas nativas, menos la ficha.
-- ===========================================================================
do $steps$
declare
    c_signature constant text := 'public._flow_engine_native_steps(text)';
    c_start constant text := 'select case p_kind';
    -- Los diez toques con nombre y producto (compra 1, carrito 3, pago_fallido 3, intencion 3) y los tres de
    -- la audiencia (con hora_evento). Los de la ficha nombran medio_de_pago: no coinciden.
    c_old_plain constant text := '"variables_de": ["nombre", "producto"]';
    c_new_plain constant text := '"texto_fijo": true, "variables_de": ["nombre", "producto"]';
    c_old_hour constant text := '"variables_de": ["nombre", "producto", "hora_evento"]';
    c_new_hour constant text := '"texto_fijo": true, "variables_de": ["nombre", "producto", "hora_evento"]';
    v_definition text;
    v_acl text;
begin
    if (select p.proowner from pg_proc p where p.oid = c_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_literal_variables_owner_mismatch';
    end if;
    v_definition := pg_get_functiondef(c_signature::regprocedure);
    if (length(v_definition) - length(replace(v_definition, c_old_plain, ''))) / length(c_old_plain) <> 10
       or (length(v_definition) - length(replace(v_definition, c_old_hour, ''))) / length(c_old_hour) <> 3
       or (length(v_definition) - length(replace(v_definition, c_start, ''))) / length(c_start) <> 1 then
        raise exception using errcode = '55000', message = 'unexpected_flow_literal_variables_steps_definition';
    end if;
    v_definition := replace(replace(v_definition, c_old_plain, c_new_plain), c_old_hour, c_new_hour);
    v_definition := replace(v_definition, c_start,
        c_start || chr(10)
        || '        -- flow_literal_variables_steps: texto_fijo en los toques nativos, menos la ficha (20261127300000).');
    select coalesce(p.proacl::text, '') into v_acl from pg_proc p where p.oid = c_signature::regprocedure;
    execute v_definition;
    if position('flow_literal_variables_steps' in pg_get_functiondef(c_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_literal_variables_steps_not_replaced';
    end if;
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = c_signature::regprocedure)
       is distinct from v_acl then
        raise exception using errcode = '55000', message = 'flow_literal_variables_steps_acl_changed';
    end if;
end;
$steps$;

-- ===========================================================================
-- 2. El compilador acepta el texto fijo en un paso de una forma con texto_fijo.
-- ===========================================================================
do $compile$
declare
    c_signature constant text := 'public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)';
    c_old constant text := $old$
                                  where jsonb_typeof(item.value) is distinct from 'string'
                                     or not (case when v_personal then v_slot -> 'coupon_variables_de'
                                                 else v_slot -> 'variables_de' end) @> jsonb_build_array(item.value)) then true
                    else (select count(distinct item.value) from jsonb_array_elements(v_step -> 'variables') item(value))
                         <> jsonb_array_length(v_step -> 'variables')
$old$;
    c_new constant text := $new$
                                  where not (
                                      (jsonb_typeof(item.value) = 'string'
                                       and (case when v_personal then v_slot -> 'coupon_variables_de'
                                                 else v_slot -> 'variables_de' end) @> jsonb_build_array(item.value))
                                      -- flow_literal_variables: el texto fijo {"texto": t} en una forma con
                                      -- texto_fijo, de 1 a 400 caracteres, en una linea y sin «{{» (20261127300000).
                                      or (coalesce(v_slot -> 'texto_fijo' = 'true'::jsonb, false)
                                          and jsonb_typeof(item.value) = 'object'
                                          and item.value - 'texto' = '{}'::jsonb
                                          and jsonb_typeof(item.value -> 'texto') = 'string'
                                          and char_length(item.value ->> 'texto') between 1 and 400
                                          and btrim(item.value ->> 'texto') <> ''
                                          and strpos(item.value ->> 'texto', chr(10)) = 0
                                          and strpos(item.value ->> 'texto', chr(13)) = 0
                                          and strpos(item.value ->> 'texto', chr(9)) = 0
                                          and strpos(item.value ->> 'texto', '     ') = 0
                                          and strpos(item.value ->> 'texto', '{{') = 0))) then true
                    -- flow_literal_variables: los nombres sin repetir; los textos fijos pueden repetirse.
                    else (select count(distinct item.value) from jsonb_array_elements(v_step -> 'variables') item(value)
                           where jsonb_typeof(item.value) = 'string')
                         <> (select count(*) from jsonb_array_elements(v_step -> 'variables') item(value)
                              where jsonb_typeof(item.value) = 'string')
$new$;
    v_definition text;
    v_acl text;
begin
    if (select p.proowner from pg_proc p where p.oid = c_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_literal_variables_owner_mismatch';
    end if;
    v_definition := pg_get_functiondef(c_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, c_old, '')) <> length(c_old) then
        raise exception using errcode = '55000', message = 'unexpected_flow_literal_variables_compile_definition';
    end if;
    select coalesce(p.proacl::text, '') into v_acl from pg_proc p where p.oid = c_signature::regprocedure;
    execute replace(v_definition, c_old, c_new);
    if position('flow_literal_variables: el texto fijo' in pg_get_functiondef(c_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_literal_variables_compile_not_replaced';
    end if;
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = c_signature::regprocedure)
       is distinct from v_acl then
        raise exception using errcode = '55000', message = 'flow_literal_variables_compile_acl_changed';
    end if;
end;
$compile$;

commit;
