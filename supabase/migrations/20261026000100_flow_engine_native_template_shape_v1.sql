-- Migration: el paso de un flujo nativo manda las variables que declara, de
-- entre nombre y producto (E-115, 2026-10-26).
--
-- Por que. La bienvenida de ATT1 (att1_bienvenida_01, APPROVED UTILITY es_MX,
-- leida del catalogo de la bandeja 11 el 2026-10-26:
-- tests/fixtures/chatwoot_message_templates_inbox_11_bienvenida_20261026.json)
-- tiene un solo {{1}}, el producto, y un boton de URL fija. La forma nativa de
-- la compra (_flow_engine_native_steps('nativo:compra'), 20261024000200) fija
-- las variables del unico envio en ["nombre", "producto"], y el compilador
-- (_flow_engine_compile_policy_core, 20261021000200) exige esa lista exacta:
-- un paso con ["producto"] termina en
-- flow_policy_not_compilable:variables_differ:first_contact, al abrir el flujo
-- y en cada admision. El bridge ya arma el envio con las variables que declara
-- el paso, en su orden (worker._compose_native_step_proposal): solo la base lo
-- frenaba. El boton de URL fija no toca la base: no lleva parametro, y lo
-- miran el validador y el envio contra el catalogo.
--
-- Que cambia, dos cosas, sobre las definiciones vivas:
--   1. _flow_engine_native_steps('nativo:compra') suma a su unico paso
--      "variables_de": ["nombre", "producto"], las que el bridge sabe llenar.
--      "variables" queda igual: es lo que leen los demas.
--   2. El compilador, con un paso de la forma que trae variables_de (y sin
--      cupon personal), acepta una lista no vacia, sin repetidas, de esas
--      variables, en cualquier orden. Sin variables_de (todo el legado) la
--      comparacion es la de hoy, exacta. Es un reemplazo contado de la
--      condicion, con su marcador.
-- Una lista vacia, con una repetida o con otra variable sigue siendo
-- variables_differ. Ningun flujo del legado cambia: sus formas no traen
-- variables_de (_flow_engine_legacy_steps no se toca).
--
-- Grants: create or replace (y el execute de la definicion parchada)
-- conservan los privilegios de las dos funciones, asi que el ACL no cambia.
-- Correrla dos veces aborta con 55000 sin cambiar nada.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $native_template_shape$
declare
    c_steps constant text := 'public._flow_engine_native_steps(text)';
    c_core constant text := 'public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)';
    c_old_shape constant text :=
        E'''[{"step_key": "first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto"], "coupon_variables": null}]''::jsonb';
    c_old_check constant text :=
        E'        if jsonb_typeof(v_step -> ''variables'') is distinct from ''array''\n'
        || E'           or jsonb_typeof(v_wanted) is distinct from ''array''\n'
        || E'           or v_step -> ''variables'' is distinct from v_wanted then\n';
    c_new_check constant text :=
        E'        -- flow_engine_native_variables: begin\n'
        || E'        -- Un paso de una forma con variables_de (la nativa, 20261026000100) manda\n'
        || E'        -- las que declara: al menos una, sin repetidas, de esa lista. Sin\n'
        || E'        -- variables_de (el legado), la lista exacta de su paso.\n'
        || E'        -- (entre parentesis: plpgsql corta la condicion del if en el primer then)\n'
        || E'        if (case\n'
        || E'            when jsonb_typeof(v_slot -> ''variables_de'') = ''array'' and not v_personal then\n'
        || E'                case\n'
        || E'                    when jsonb_typeof(v_step -> ''variables'') is distinct from ''array'' then true\n'
        || E'                    when jsonb_array_length(v_step -> ''variables'') = 0 then true\n'
        || E'                    when exists (select 1 from jsonb_array_elements(v_step -> ''variables'') item(value)\n'
        || E'                                  where jsonb_typeof(item.value) is distinct from ''string''\n'
        || E'                                     or not (v_slot -> ''variables_de'') @> jsonb_build_array(item.value)) then true\n'
        || E'                    else (select count(distinct item.value) from jsonb_array_elements(v_step -> ''variables'') item(value))\n'
        || E'                         <> jsonb_array_length(v_step -> ''variables'')\n'
        || E'                end\n'
        || E'            else\n'
        || E'                jsonb_typeof(v_step -> ''variables'') is distinct from ''array''\n'
        || E'                or jsonb_typeof(v_wanted) is distinct from ''array''\n'
        || E'                or v_step -> ''variables'' is distinct from v_wanted\n'
        || E'        end) then\n'
        || E'        -- flow_engine_native_variables: end\n';
    v_definition text;
begin
    if to_regprocedure(c_steps) is null or to_regprocedure(c_core) is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_missing';
    end if;
    v_definition := pg_get_functiondef(c_core::regprocedure);
    if position('flow_engine_native_variables' in v_definition) > 0
       or position('variables_de' in pg_get_functiondef(c_steps::regprocedure)) > 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_template_shape_already_applied';
    end if;
    if position(c_old_shape in pg_get_functiondef(c_steps::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_steps_definition',
            detail = c_steps;
    end if;
    if length(v_definition) - length(replace(v_definition, c_old_check, '')) <> length(c_old_check) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_compile_definition',
            detail = c_core;
    end if;

    -- 1. La forma nativa de la compra, con las variables que el bridge sabe llenar.
    create or replace function public._flow_engine_native_steps(p_kind text)
    returns jsonb
    language sql
    immutable
    security invoker
    set search_path = pg_catalog, pg_temp
    as $function$
        select case p_kind
            when 'nativo:compra' then
                '[{"step_key": "first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto"], "variables_de": ["nombre", "producto"], "coupon_variables": null}]'::jsonb
        end
    $function$;

    -- 2. El compilador acepta esas variables en un paso nativo.
    execute replace(v_definition, c_old_check, c_new_check);
    if position('flow_engine_native_variables: end' in pg_get_functiondef(c_core::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_variables_not_applied',
            detail = c_core;
    end if;
end;
$native_template_shape$;

commit;
