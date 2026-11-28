-- flow-engine-native-silence-literal-v1: el seguimiento por silencio nativo puede
-- mandar un texto fijo como tercera variable, [nombre, producto, {"texto": "…"}]
-- (U4 del mapa «Frenos ante Meta y calentamiento», D17, 2026-11-28). Contrato:
-- docs/contracts/flow-engine-v2.md (§37).
--
-- Por que. La plantilla de utilidad del seguimiento de Maribel
-- (maribel_seguimiento_consulta_01, borradores del 27/11) deja lo comercial en su
-- {{3}}. El texto fijo de F4 (20261127300000) quedo afuera del silencio a
-- proposito (D17): la base exige en _flow_engine_native_unsupported que el paso
-- del silencio tenga exactamente variables = [nombre, producto]
-- (flow_engine_native_silence_shape, 20261101000200), y esa funcion la miran la
-- publicacion (_flow_change_new_flow_blocker) y la reserva del barrido nativo
-- (claim_flow_engine_silence_v1, flow_engine_native_silence_gate).
--
-- Que cambia: un reemplazo contado de esa sola condicion. Ademas de
-- [nombre, producto], acepta [nombre, producto, {"texto": t}] con t de 1 a 400
-- caracteres, no en blanco, sin salto de linea, retorno, tab, cinco espacios
-- seguidos ni «{{» (las mismas reglas que el compilador de F4), y el objeto con
-- la clave texto sola. El orden es fijo: el barrido manda siempre el nombre en
-- {{1}} y el producto en {{2}} (FollowupTemplate.params), y el texto en {{3}}.
--
-- Lo que NO cambia. Ninguna tabla, ningun permiso y ninguna otra funcion. Un
-- silencio sin texto fijo se abre y se reserva igual que antes. Los telefonos,
-- los mails y los links del texto los frena el validador del bridge, no la base.
-- Nadie lo escribe con FLOW_LITERAL_VARIABLES_ENABLED apagado: el validador
-- rechaza {"texto": …} y el barrido nativo no arma un silencio con texto (D-U4.1).
--
-- Como: el metodo de 20261127300000 (reemplazo contado sobre la definicion
-- VIGENTE, con su marca, conservando duenio y permisos). Correrla dos veces
-- aborta con 55000 sin cambiar nada.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $silence_literal$
declare
    c_signature constant text := 'public._flow_engine_native_unsupported(jsonb)';
    c_old constant text := $old$and p_definition #> '{pasos,0,variables}' = '["nombre", "producto"]'::jsonb$old$;
    c_new constant text := $new$and (p_definition #> '{pasos,0,variables}' = '["nombre", "producto"]'::jsonb
                      -- flow_silence_literal: el texto fijo como tercera variable del seguimiento, de 1 a
                      -- 400 caracteres, en una linea y sin «{{» (U4, 20261128100000).
                      or (jsonb_typeof(p_definition #> '{pasos,0,variables}') = 'array'
                          and jsonb_array_length(p_definition #> '{pasos,0,variables}') = 3
                          and p_definition #> '{pasos,0,variables,0}' = '"nombre"'::jsonb
                          and p_definition #> '{pasos,0,variables,1}' = '"producto"'::jsonb
                          and jsonb_typeof(p_definition #> '{pasos,0,variables,2}') = 'object'
                          and (p_definition #> '{pasos,0,variables,2}') - 'texto' = '{}'::jsonb
                          and jsonb_typeof(p_definition #> '{pasos,0,variables,2,texto}') = 'string'
                          and char_length(p_definition #>> '{pasos,0,variables,2,texto}') between 1 and 400
                          and btrim(p_definition #>> '{pasos,0,variables,2,texto}') <> ''
                          and strpos(p_definition #>> '{pasos,0,variables,2,texto}', chr(10)) = 0
                          and strpos(p_definition #>> '{pasos,0,variables,2,texto}', chr(13)) = 0
                          and strpos(p_definition #>> '{pasos,0,variables,2,texto}', chr(9)) = 0
                          and strpos(p_definition #>> '{pasos,0,variables,2,texto}', '     ') = 0
                          and strpos(p_definition #>> '{pasos,0,variables,2,texto}', '{{') = 0))$new$;
    v_definition text;
    v_acl text;
begin
    if to_regprocedure(c_signature) is null
       or position('flow_engine_native_silence_shape' in pg_get_functiondef(c_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_literal_missing';
    end if;
    v_definition := pg_get_functiondef(c_signature::regprocedure);
    if position('flow_silence_literal' in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_literal_already_applied';
    end if;
    if (select p.proowner from pg_proc p where p.oid = c_signature::regprocedure)
       is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_literal_owner_mismatch';
    end if;
    if length(v_definition) - length(replace(v_definition, c_old, '')) <> length(c_old) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_silence_literal_definition';
    end if;
    select coalesce(p.proacl::text, '') into v_acl from pg_proc p where p.oid = c_signature::regprocedure;
    execute replace(v_definition, c_old, c_new);
    if position('flow_silence_literal: el texto fijo' in pg_get_functiondef(c_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_literal_not_replaced';
    end if;
    if (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = c_signature::regprocedure)
       is distinct from v_acl then
        raise exception using errcode = '55000', message = 'flow_engine_native_silence_literal_acl_changed';
    end if;
end;
$silence_literal$;

commit;
