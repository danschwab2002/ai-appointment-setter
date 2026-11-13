-- e483-native-multitouch-v1: el motor nativo manda hasta tres toques con espera
-- (E-483, C1 y C5 del mapa «el AE maneja todos los flujos», 2026-11-12).
--
-- Por que. El esquema setter-flujo/v1 ya escribe varios toques; lo que ataba un
-- flujo nativo a un mensaje era la base: la forma de sus pasos
-- (_flow_engine_native_steps) era un literal de un solo envio por disparador y
-- _flow_engine_native_unsupported rechazaba cualquier otra forma con
-- motor_nativo:forma. Medido en PGlite el 2026-11-12 (P2 del mapa) con la forma
-- parcheada en la prueba: el sustrato del legado ya hace el resto sin cambios.
-- _flow_engine_compile_policy_core compila un paso por envio con su delay desde
-- el primer envio y approved_template; la finalizacion del intento (20260813000100)
-- agenda el no_reply_review del toque siguiente al aceptar el anterior;
-- flow_engine_execution_context_v1 encuentra su plantilla por el step_key; y
-- mark_flow_engine_request_started_v1 lo autoriza contra la bolsa nativa, que
-- cuenta cada envio (no cada caso). La reevaluacion corta el toque siguiente
-- con la compra y con la respuesta de la persona (prospect_replied).
--
-- Que cambia:
--   1. _flow_engine_native_steps: carrito, intencion y pago_fallido suman dos
--      ranuras, native_touch_2 y native_touch_3, con las variables de entre
--      nombre y producto (variables_de) y sin cupon. La compra (la bienvenida,
--      utilidad, regla 17) sigue de un solo envio. La ranura 1 no cambia: un
--      flujo de un toque compila exactamente igual que antes.
--   2. _flow_engine_native_unsupported: de uno a tres envios en el camino. Con
--      mas de uno: solo carrito, intencion y pago_fallido; la salida contesta
--      escrita (la respuesta corta el toque siguiente: el flujo lo tiene que
--      decir); al menos un minuto de espera entre dos envios y ninguna espera
--      despues del ultimo (la regla del validador); el boton solo en el primer
--      envio; y sin condicion (una condicion
--      despues de un envio es C3, todavia no). Un pago rechazado con una
--      plantilla antes de su cadena tampoco: hoy no compilaba (more_sends_than_
--      steps) y con la forma nueva compilaria sin que nadie lo haya probado.
--
-- Lo que no cambia: ningun flujo publicado tiene mas de un envio (la base y el
-- validador los rechazaban), asi que nada de lo que corre en ATT1 o Maribel
-- cambia al aplicarla. El envio de los toques siguientes en el bridge va
-- detras de FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED (apagado por defecto).
--
-- Como: el metodo de 20261103000900, sobre la definicion VIGENTE de cada
-- funcion, con un texto exacto que tiene que aparecer una sola vez y la marca
-- que no puede estar. Correrla dos veces aborta con 55000.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $e483$
declare
    c_steps constant text := 'public._flow_engine_native_steps(text)';
    c_unsupported constant text := 'public._flow_engine_native_unsupported(jsonb)';
    c_touches constant text :=
        ', {"step_key": "native_touch_2", "coupon_ordinal": null, "variables": ["nombre", "producto"], '
        || '"variables_de": ["nombre", "producto"], "coupon_variables": null}, '
        || '{"step_key": "native_touch_3", "coupon_ordinal": null, "variables": ["nombre", "producto"], '
        || '"variables_de": ["nombre", "producto"], "coupon_variables": null}]''::jsonb';
    c_first constant text :=
        '[{"step_key": "first_contact", "coupon_ordinal": null, "variables": ["nombre", "producto"], '
        || '"variables_de": ["nombre", "producto"], "coupon_variables": null';
    c_old_cart constant text :=
        E'        when ''nativo:carrito'' then\n            ''' || c_first || E'}]''::jsonb\n';
    c_new_cart constant text :=
        E'        -- e483_native_multitouch: hasta tres toques (20261112210000).\n'
        || E'        when ''nativo:carrito'' then\n            ''' || c_first || '}' || c_touches || E'\n';
    c_old_intent constant text :=
        E'        when ''nativo:intencion'' then\n            ''' || c_first || E'}]''::jsonb\n';
    c_new_intent constant text :=
        E'        -- e483_native_multitouch: hasta tres toques (20261112210000).\n'
        || E'        when ''nativo:intencion'' then\n            ''' || c_first || '}' || c_touches || E'\n';
    c_old_payment constant text :=
        E'            ''' || c_first || E', "cupon_fijo": true}]''::jsonb\n';
    c_new_payment constant text :=
        E'            -- e483_native_multitouch: hasta tres toques, el cupon fijo solo en el primero (20261112210000).\n'
        || E'            ''' || c_first || ', "cupon_fijo": true}' || c_touches || E'\n';
    c_old_shape constant text :=
        E'        -- flow_engine_native_payment_failure_branches: begin (20261103000900)\n';
    c_plantillas constant text :=
        E'(select count(*) from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array''\n'
        || E'                                                     then p_definition -> ''pasos'' else ''[]''::jsonb end) step(value)\n'
        || E'                  where jsonb_typeof(step.value) = ''object'' and step.value ? ''plantilla'')';
    c_new_shape constant text :=
        E'        -- e483_native_multitouch: begin (20261112210000)\n'
        || E'        -- Hasta tres envios. Con mas de uno: carrito, intencion o pago_fallido, la\n'
        || E'        -- salida contesta escrita, el boton solo en el primero, sin condicion y con\n'
        || E'        -- al menos un minuto entre dos envios y sin espera al final.\n'
        || E'        when ' || c_plantillas || E' > 3 then ''motor_nativo:forma''\n'
        || E'        when ' || c_plantillas || E' > 1\n'
        || E'             and (coalesce(public._flow_engine_native_trigger(p_definition), '''') not in (''carrito'', ''intencion'', ''pago_fallido'')\n'
        || E'                  or not coalesce(jsonb_typeof(p_definition #> ''{termina,si}'') = ''array''\n'
        || E'                                  and p_definition #> ''{termina,si}'' @> ''["contesta"]''::jsonb, false)\n'
        || E'                  or exists (select 1 from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array'' then p_definition -> ''pasos'' else ''[]''::jsonb end) with ordinality step(value, ordinal)\n'
        || E'                              where jsonb_typeof(step.value) = ''object''\n'
        || E'                                and (step.value ? ''condicion''\n'
        || E'                                     or (step.value ? ''boton'' and step.ordinal > (\n'
        || E'                                         select min(first.ordinal) from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array'' then p_definition -> ''pasos'' else ''[]''::jsonb end)\n'
        || E'                                                with ordinality first(value, ordinal)\n'
        || E'                                          where jsonb_typeof(first.value) = ''object'' and first.value ? ''plantilla'')))))\n'
        || E'            then ''motor_nativo:forma''\n'
        || E'        -- Con mas de un envio, la regla del validador (flow_engine.py, T2.1): al menos un minuto de\n'
        || E'        -- espera entre dos envios y ninguna espera despues del ultimo.\n'
        || E'        when ' || c_plantillas || E' > 1\n'
        || E'             and (exists (select 1 from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array'' then p_definition -> ''pasos'' else ''[]''::jsonb end) with ordinality send(value, ordinal)\n'
        || E'                           where jsonb_typeof(send.value) = ''object'' and send.value ? ''plantilla''\n'
        || E'                             and exists (select 1 from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array'' then p_definition -> ''pasos'' else ''[]''::jsonb end) with ordinality earlier(value, ordinal)\n'
        || E'                                          where earlier.ordinal < send.ordinal and jsonb_typeof(earlier.value) = ''object''\n'
        || E'                                            and earlier.value ? ''plantilla'')\n'
        || E'                             and not exists (select 1 from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array'' then p_definition -> ''pasos'' else ''[]''::jsonb end) with ordinality pause(value, ordinal)\n'
        || E'                                              where pause.ordinal < send.ordinal and jsonb_typeof(pause.value) = ''object''\n'
        || E'                                                and coalesce(pause.value ->> ''esperar'', '''') ~ ''^0*[1-9][0-9]*[mhd]$''\n'
        || E'                                                and pause.ordinal > (select max(previous.ordinal)\n'
        || E'                                                      from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array'' then p_definition -> ''pasos'' else ''[]''::jsonb end) with ordinality previous(value, ordinal)\n'
        || E'                                                     where previous.ordinal < send.ordinal and jsonb_typeof(previous.value) = ''object''\n'
        || E'                                                       and previous.value ? ''plantilla'')))\n'
        || E'                  or exists (select 1 from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array'' then p_definition -> ''pasos'' else ''[]''::jsonb end) with ordinality pause(value, ordinal)\n'
        || E'                              where jsonb_typeof(pause.value) = ''object'' and pause.value ? ''esperar''\n'
        || E'                                and pause.ordinal > (select max(last.ordinal)\n'
        || E'                                      from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array'' then p_definition -> ''pasos'' else ''[]''::jsonb end) with ordinality last(value, ordinal)\n'
        || E'                                     where jsonb_typeof(last.value) = ''object'' and last.value ? ''plantilla'')))\n'
        || E'            then ''motor_nativo:forma''\n'
        || E'        when public._flow_engine_native_trigger(p_definition) = ''pago_fallido''\n'
        || E'             and ' || c_plantillas || E' >= 1\n'
        || E'             and exists (select 1 from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array''\n'
        || E'                                                                 then p_definition -> ''pasos'' else ''[]''::jsonb end) step(value)\n'
        || E'                          where jsonb_typeof(step.value) = ''object'' and step.value ? ''condicion'')\n'
        || E'            then ''motor_nativo:forma''\n'
        || E'        -- e483_native_multitouch: end\n'
        || c_old_shape;
    c_old_count constant text := E'and step.value ? ''plantilla'') <> 1\n';
    c_new_count constant text := E'and step.value ? ''plantilla'') not between 1 and 3\n';
    v_text text;
    v_new text;
begin
    if to_regprocedure(c_steps) is null or to_regprocedure(c_unsupported) is null then
        raise exception using errcode = '55000', message = 'e483_native_multitouch_missing';
    end if;

    -- 1. La forma: dos ranuras mas en carrito, intencion y pago_fallido.
    v_text := pg_get_functiondef(c_steps::regprocedure);
    if position('native_touch_2' in v_text) > 0 then
        raise exception using errcode = '55000', message = 'e483_native_multitouch_already_applied';
    end if;
    if length(v_text) - length(replace(v_text, c_old_cart, '')) <> length(c_old_cart)
       or length(v_text) - length(replace(v_text, c_old_intent, '')) <> length(c_old_intent)
       or length(v_text) - length(replace(v_text, c_old_payment, '')) <> length(c_old_payment) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_steps_definition',
            detail = c_steps;
    end if;
    v_new := replace(replace(replace(v_text, c_old_cart, c_new_cart), c_old_intent, c_new_intent),
                     c_old_payment, c_new_payment);
    execute v_new;

    -- 2. Lo que abre la base: de uno a tres envios, con las condiciones de arriba.
    v_text := pg_get_functiondef(c_unsupported::regprocedure);
    if position('e483_native_multitouch' in v_text) > 0
       or length(v_text) - length(replace(v_text, c_old_shape, '')) <> length(c_old_shape)
       or length(v_text) - length(replace(v_text, c_old_count, '')) <> length(c_old_count) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_unsupported_definition',
            detail = c_unsupported;
    end if;
    execute replace(replace(v_text, c_old_shape, c_new_shape), c_old_count, c_new_count);

    if jsonb_array_length(public._flow_engine_native_steps('nativo:carrito')) <> 3
       or jsonb_array_length(public._flow_engine_native_steps('nativo:intencion')) <> 3
       or jsonb_array_length(public._flow_engine_native_steps('nativo:pago_fallido')) <> 3
       or jsonb_array_length(public._flow_engine_native_steps('nativo:compra')) <> 1
       or position('e483_native_multitouch: end' in pg_get_functiondef(c_unsupported::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'e483_native_multitouch_not_applied';
    end if;
end;
$e483$;

commit;
