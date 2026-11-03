-- flow-engine-native-segment-label-v1: la etiqueta de un segmento en un flujo
-- nativo, con cualquier disparador (E-316, PR-C de los segmentos; diseno:
-- documentacion/2026-11-03-e310-diseno-segmentos-conocimiento-por-etiqueta.md,
-- §2.7; contrato: docs/contracts/segment-labels-v1.md).
--
-- Por que. Un segmento (segment-knowledge-v1, PR-A) le suma al agente un
-- conocimiento mientras la conversacion tiene su etiqueta de Chatwoot, y Dan
-- pidio el 2026-11-03 que la etiqueta este desde el primer turno del agente:
-- que la ponga el flujo que le escribe a la persona (por ejemplo, el carrito de
-- la oferta de Black November), antes de su primer envio. El esquema ya tiene
-- el paso etiquetar, pero la base lo deja solo con la compra (la regla 5,
-- tambien en _flow_engine_native_unsupported: etiquetar marca clienta), asi que
-- un carrito con etiquetar da motor_nativo:forma al publicar.
--
-- Que cambia. Un solo reemplazo contado en _flow_engine_native_unsupported
-- (la definicion VIGENTE, que es la de 20261101000200 con el bloque de
-- 20261103000900 adelante): con un disparador que no es la compra, etiquetar
-- sigue siendo motor_nativo:forma solo si la etiqueta es cliente, de la
-- automatizacion (automation_*) o no es un texto. La etiqueta de un segmento se
-- abre. Que esa etiqueta sea de un segmento declarado de la instancia (con su
-- macro y dentro de su vigencia) lo mira el validador del bridge
-- (etiquetar_segment_not_before_template y segment_label_outside_window): la
-- base no tiene el manifiesto. Una etiqueta que no es de ningun segmento, si
-- llegara, no la pone nadie: el bridge solo etiqueta con un macro declarado.
--
-- Que NO cambia. La compra, el silencio (un solo paso) y la cadena del pago
-- rechazado (antes de la cadena solo esperas) quedan igual. El compilador ya
-- saltea etiquetar (lo ejecuta el bridge, 20261024000200) y la cola del paso
-- (flow_engine_native_step_tail_v1) ya devuelve el primer etiquetar de la
-- version fijada, sea cual sea. Ninguna tabla, ningun permiso, ninguna fila.
-- create or replace conserva los privilegios. Correrla dos veces aborta con
-- 55000 sin cambiar nada.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $segment_label$
declare
    c_unsupported constant text := 'public._flow_engine_native_unsupported(jsonb)';
    c_old constant text :=
        E'             -- etiquetar marca clienta: solo con la compra (la regla 5 del esquema, tambien en la base).\n'
        || E'             or (public._flow_engine_native_trigger(p_definition) is distinct from ''compra''\n'
        || E'                 and exists (select 1 from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array''\n'
        || E'                                                                     then p_definition -> ''pasos'' else ''[]''::jsonb end) step(value)\n'
        || E'                              where jsonb_typeof(step.value) = ''object'' and step.value ? ''etiquetar''))\n';
    c_new constant text :=
        E'             -- etiquetar marca clienta: solo con la compra (la regla 5 del esquema, tambien en la base).\n'
        || E'             -- flow_engine_native_segment_label: la etiqueta de un segmento va con cualquier\n'
        || E'             -- disparador; cliente, la de la automatizacion o una que no es texto, solo con la\n'
        || E'             -- compra (20261103001300).\n'
        || E'             or (public._flow_engine_native_trigger(p_definition) is distinct from ''compra''\n'
        || E'                 and exists (select 1 from jsonb_array_elements(case when jsonb_typeof(p_definition -> ''pasos'') = ''array''\n'
        || E'                                                                     then p_definition -> ''pasos'' else ''[]''::jsonb end) step(value)\n'
        || E'                              where jsonb_typeof(step.value) = ''object'' and step.value ? ''etiquetar''\n'
        || E'                                and (jsonb_typeof(step.value -> ''etiquetar'') is distinct from ''string''\n'
        || E'                                     or step.value ->> ''etiquetar'' = ''cliente''\n'
        || E'                                     or step.value ->> ''etiquetar'' like ''automation\\_%'')))\n';
    v_text text;
begin
    if to_regprocedure(c_unsupported) is null
       or to_regprocedure('public._flow_engine_native_trigger(jsonb)') is null
       or to_regprocedure('public.flow_engine_native_step_tail_v1(uuid)') is null then
        raise exception using errcode = '55000', message = 'flow_engine_native_segment_label_missing';
    end if;
    v_text := pg_get_functiondef(c_unsupported::regprocedure);
    if position('flow_engine_native_segment_label' in v_text) > 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_segment_label_already_applied';
    end if;
    if length(v_text) - length(replace(v_text, c_old, '')) <> length(c_old) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_unsupported_definition',
            detail = c_unsupported;
    end if;
    execute replace(v_text, c_old, c_new);
    if position('flow_engine_native_segment_label' in pg_get_functiondef(c_unsupported::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'flow_engine_native_segment_label_not_replaced',
            detail = c_unsupported;
    end if;
end;
$segment_label$;

commit;
