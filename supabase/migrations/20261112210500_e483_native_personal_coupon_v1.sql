-- e483-native-personal-coupon-v1: el cupon propio por persona en el motor
-- nativo (E-483, C2 del mapa «el AE maneja todos los flujos», 2026-11-12; P8).
-- Contrato: docs/contracts/flow-engine-v2.md («El cupon propio por persona en el nativo»).
--
-- Por que. El nativo frenaba boton: {link: checkout, cupon: personal}: la forma
-- de sus pasos (_flow_engine_native_steps) no tenia ranura con cupon y el
-- compilador lo rechazaba (coupon_not_allowed). El motor v1 (la ruta motor del
-- carrito de ATT1) ya lo hace, con un sustrato que no mira el legado: el
-- trabajador del cupon (recovery_coupons.py, claim_due_recovery_coupons_v1 y
-- settle_recovery_coupon_v1) siembra y crea el codigo de un paso que la politica
-- declara con "coupon_ordinal", y reserve_recovery_checkout_issuance_v1 emite
-- nuestro link para ese paso. Aca el nativo se suma a ese sustrato; no se copia.
--
-- La regla (D3 del mapa: las de ATT1). El cupon propio va en cualquier toque del
-- carrito y del pago rechazado sin cadena; el primero de un camino es el
-- ordinal 1 del caso y el segundo el 2; un tercero no compila (dos por caso
-- como maximo, como la tabla). Vence a las 24 h de la hora en que el toque tiene
-- que salir (p_validity del trabajador, el mismo que A1 y Z), y el bridge lo dice
-- en la hora del pais. Con el cupon, el toque puede decir cuando (solo el primer
-- envio: su ancla es el evento), precio y vence, ademas de nombre y producto.
--
-- Que cambia, sobre las definiciones vivas (cada reemplazo contado, con su
-- marca; si el texto no esta exactamente una vez, aborta con 55000):
--   1. _flow_engine_native_steps: las ranuras del carrito y del pago rechazado
--      suman "cupon_personal": true y "coupon_variables_de" (las variables que
--      el toque puede mandar con el cupon). La compra y el formulario, no.
--   2. _flow_engine_compile_policy_core: un paso con cupon personal en una
--      ranura "cupon_personal" toma el ordinal que le toca en el camino (1 o 2;
--      un tercero es coupon_too_many), sus variables salen de
--      coupon_variables_de, y el paso compilado lleva "coupon_ordinal" (tambien
--      el primero: el ordinal 1 del primer envio nativo no lo gobierna el ancla
--      del carrito). Una ranura sin "cupon_personal" compila igual que antes.
--   3. _flow_engine_native_unsupported: el cupon personal, solo como
--      {link: checkout, cupon: personal}, solo en el carrito y en el pago
--      rechazado, a lo sumo dos; un toque siguiente puede llevar ese boton (y
--      ningun otro); y el pago rechazado sin cadena lo acepta. En la cadena del
--      pago rechazado sigue cerrado (_flow_engine_native_payment_failure_send_ok
--      no cambia: la politica de la cadena es una sola para todas las ramas).
--   4. _flow_engine_native_first_coupon_ordinal(uuid), nueva y privada: el
--      ordinal del cupon del primer envio de un recorrido nativo, o null.
--   5. claim_due_recovery_coupons_v1: siembra el cupon del primer envio nativo
--      que lo declara, con la misma anticipacion y vigencia que A1. Los toques
--      siguientes ya los siembra la rama de recovery_followup_steps (su
--      no_reply_review lleva coupon_ordinal en la politica).
--   6. reserve_recovery_checkout_issuance_v1: emite nuestro link tambien para el
--      primer envio nativo con su cupon (los siguientes ya pasaban por
--      coupon_ordinal).
--   7. get_flow_engine_native_step_coupon_v1(uuid), nueva, solo service_role:
--      la lectura del bridge, el ordinal del paso y su cupon (si ya esta).
--
-- Lo que NO cambia. Ningun flujo publicado tiene cupon personal en el nativo
-- (la base y el validador lo rechazaban): nada de lo que corre en ATT1 o
-- Maribel compila distinto. El legado y la ruta motor no tienen
-- "cupon_personal" en sus ranuras ni ancla flow_engine_event: su siembra, su
-- link y sus cupones salen igual (lo fija validate_e483_native_personal_coupon.mjs
-- con las definiciones de antes y las de ahora sobre el mismo carrito de ATT1).
-- El bridge arma el toque con cupon solo con FLOW_ENGINE_NATIVE_PERSONAL_COUPON_ENABLED
-- (apagado por defecto: cierra sin salir). No toca tablas.
--
-- Como: el metodo de 20261112210000, reemplazo contado sobre la definicion
-- VIGENTE, con la marca que no puede estar. Correrla dos veces aborta con 55000.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $guard$
begin
    if to_regprocedure('public._flow_engine_native_first_coupon_ordinal(uuid)') is not null
       or to_regprocedure('public.get_flow_engine_native_step_coupon_v1(uuid)') is not null then
        raise exception using errcode = '55000', message = 'e483_native_personal_coupon_already_applied';
    end if;
    if to_regprocedure('public._flow_engine_native_steps(text)') is null
       or to_regprocedure('public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)') is null
       or to_regprocedure('public._flow_engine_native_unsupported(jsonb)') is null
       or to_regprocedure('public._recovery_followup_step(uuid)') is null
       or to_regprocedure('public.claim_due_recovery_coupons_v1(timestamptz,text,integer,text,interval,interval,numeric,integer)') is null
       or to_regprocedure('public.reserve_recovery_checkout_issuance_v1(uuid,text,bigint,uuid,text,timestamptz)') is null
       or to_regclass('public.flow_engine_native_runs') is null
       or to_regclass('public.recovery_coupons') is null then
        raise exception using errcode = '55000', message = 'e483_native_personal_coupon_missing';
    end if;
end;
$guard$;

-- Un reemplazo contado: el texto exacto una sola vez y la marca ausente.
create function pg_temp.e483_native_personal_coupon_patch(p_signature text, p_old text, p_new text)
returns text
language plpgsql
as $function$
declare
    v_text text := pg_get_functiondef(p_signature::regprocedure);
begin
    if position('e483_native_personal_coupon' in v_text) > 0 then
        raise exception using errcode = '55000', message = 'e483_native_personal_coupon_already_applied',
            detail = p_signature;
    end if;
    if p_old = '' or length(v_text) - length(replace(v_text, p_old, '')) <> length(p_old) then
        raise exception using errcode = '55000',
            message = 'unexpected_' || regexp_replace(split_part(p_signature, '(', 1), '^public\._?', '') || '_definition',
            detail = p_signature;
    end if;
    return replace(v_text, p_old, p_new);
end;
$function$;

-- ---------------------------------------------------------------------------
-- 4. El ordinal del cupon del primer envio de un recorrido nativo.
-- ---------------------------------------------------------------------------
create function public._flow_engine_native_first_coupon_ordinal(p_action_id uuid)
returns smallint
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    -- e483_native_personal_coupon: el primer envio de un recorrido nativo cuya
    -- politica lo declara con su cupon propio (20261112210500). El de un caso del
    -- legado o de la ruta motor nunca: su ancla no es flow_engine_event.
    select (policy.steps -> 0 ->> 'coupon_ordinal')::smallint
      from public.scheduled_actions action
      join public.followup_policy_versions policy
        on policy.policy_key = action.policy_key
       and policy.version = action.policy_version
     where action.id = p_action_id
       and action.action_type = 'first_contact_review'
       and action.anchor_type = 'flow_engine_event'
       and policy.steps -> 0 ->> 'step_key' = action.step_key
       and policy.steps -> 0 ->> 'coupon_ordinal' in ('1', '2')
       and exists (select 1 from public.flow_engine_native_runs native
                    where native.recovery_case_id = action.recovery_case_id);
$function$;

-- ---------------------------------------------------------------------------
-- 7. La lectura del bridge: el ordinal del paso nativo y su cupon.
-- ---------------------------------------------------------------------------
create function public.get_flow_engine_native_step_coupon_v1(p_action_id uuid)
returns table (ordinal smallint, coupon_id uuid, code text, status text, expires_at timestamptz,
               verified_at timestamptz)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    -- e483_native_personal_coupon: el cupon que el paso nativo declara (20261112210500).
    -- Sin fila: el paso no lleva cupon propio (o no es de un recorrido nativo). Con
    -- la fila y coupon_id nulo: el trabajador todavia no lo sembro.
    with step as (
        select action.recovery_case_id,
               coalesce(public._flow_engine_native_first_coupon_ordinal(action.id),
                        case when public._recovery_followup_step(action.id) ->> 'coupon_ordinal' in ('1', '2')
                             then (public._recovery_followup_step(action.id) ->> 'coupon_ordinal')::smallint
                        end) as ordinal
          from public.scheduled_actions action
         where action.id = p_action_id
           and exists (select 1 from public.flow_engine_native_runs native
                        where native.recovery_case_id = action.recovery_case_id)
    )
    select step.ordinal, coupon.id, coupon.code, coupon.status, coupon.expires_at, coupon.verified_at
      from step
      left join public.recovery_coupons coupon
        on coupon.recovery_case_id = step.recovery_case_id
       and coupon.ordinal = step.ordinal
     where step.ordinal is not null;
$function$;

do $acl$
declare
    v_role text;
begin
    revoke all on function public._flow_engine_native_first_coupon_ordinal(uuid) from public;
    revoke all on function public.get_flow_engine_native_step_coupon_v1(uuid) from public;
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on function public._flow_engine_native_first_coupon_ordinal(uuid) from %I', v_role);
        execute format('revoke all on function public.get_flow_engine_native_step_coupon_v1(uuid) from %I', v_role);
    end loop;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'grant execute on function public.get_flow_engine_native_step_coupon_v1(uuid) to service_role';
    end if;
end;
$acl$;

-- ---------------------------------------------------------------------------
-- 1 a 3, 5 y 6. Los reemplazos sobre la definicion vigente.
-- ---------------------------------------------------------------------------
do $patch$
declare
    c_steps constant text := 'public._flow_engine_native_steps(text)';
    c_core constant text := 'public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)';
    c_unsupported constant text := 'public._flow_engine_native_unsupported(jsonb)';
    c_claim constant text :=
        'public.claim_due_recovery_coupons_v1(timestamptz,text,integer,text,interval,interval,numeric,integer)';
    c_reserve constant text := 'public.reserve_recovery_checkout_issuance_v1(uuid,text,bigint,uuid,text,timestamptz)';
    -- 1. Las ranuras, como las dejo 20261112210000.
    c_slot constant text := '"variables": ["nombre", "producto"], "variables_de": ["nombre", "producto"], "coupon_variables": null';
    c_first constant text := '{"step_key": "first_contact", "coupon_ordinal": null, ' || c_slot;
    c_second constant text := '{"step_key": "native_touch_2", "coupon_ordinal": null, ' || c_slot;
    c_third constant text := '{"step_key": "native_touch_3", "coupon_ordinal": null, ' || c_slot;
    c_coupon_first constant text :=
        ', "cupon_personal": true, "coupon_variables_de": ["nombre", "producto", "cuando", "precio", "vence"]';
    c_coupon_next constant text :=
        ', "cupon_personal": true, "coupon_variables_de": ["nombre", "producto", "precio", "vence"]';
    c_old_cart constant text :=
        E'        when ''nativo:carrito'' then\n            ''['
        || c_first || '}, ' || c_second || '}, ' || c_third || E'}]''::jsonb\n';
    c_new_cart constant text :=
        E'        -- e483_native_personal_coupon: el cupon propio en cualquier toque (20261112210500).\n'
        || E'        when ''nativo:carrito'' then\n            ''['
        || c_first || c_coupon_first || '}, ' || c_second || c_coupon_next || '}, '
        || c_third || c_coupon_next || E'}]''::jsonb\n';
    c_old_payment constant text :=
        E'            ''[' || c_first || ', "cupon_fijo": true}, ' || c_second || '}, ' || c_third || E'}]''::jsonb\n';
    c_new_payment constant text :=
        E'            -- e483_native_personal_coupon: el cupon propio en cualquier toque, sin la cadena (20261112210500).\n'
        || E'            ''[' || c_first || ', "cupon_fijo": true' || c_coupon_first || '}, '
        || c_second || c_coupon_next || '}, ' || c_third || c_coupon_next || E'}]''::jsonb\n';
    -- 2. El compilador.
    c_old_declare constant text := E'    v_send_coupons integer[] := array[]::integer[];\n';
    c_new_declare constant text := c_old_declare
        || E'    -- e483_native_personal_coupon: el ordinal del cupon propio del envio (20261112210500).\n'
        || E'    v_ordinal integer;\n';
    c_old_personal constant text :=
        E'        if v_personal then\n'
        || E'            if jsonb_typeof(v_slot -> ''coupon_ordinal'') is distinct from ''number'' then\n'
        || E'                raise exception using errcode = ''22023'', message = c_refuse || ''coupon_not_allowed:'' || v_slot_key;\n'
        || E'            end if;\n';
    c_new_personal constant text :=
        E'        v_ordinal := null;\n'
        || E'        if v_personal then\n'
        || E'            -- e483_native_personal_coupon: begin (20261112210500)\n'
        || E'            -- Una ranura nativa con cupon_personal: el ordinal que le toca en el camino,\n'
        || E'            -- 1 o 2 (dos por caso como maximo, como recovery_coupons).\n'
        || E'            if coalesce(v_slot -> ''cupon_personal'' = ''true''::jsonb, false) then\n'
        || E'                v_ordinal := 1 + (select count(*) from unnest(v_send_coupons) sent(ordinal)\n'
        || E'                                   where sent.ordinal is not null);\n'
        || E'                if v_ordinal > 2 then\n'
        || E'                    raise exception using errcode = ''22023'', message = c_refuse || ''coupon_too_many:'' || v_slot_key;\n'
        || E'                end if;\n'
        || E'            elsif jsonb_typeof(v_slot -> ''coupon_ordinal'') is distinct from ''number'' then\n'
        || E'                raise exception using errcode = ''22023'', message = c_refuse || ''coupon_not_allowed:'' || v_slot_key;\n'
        || E'            end if;\n'
        || E'            -- e483_native_personal_coupon: end\n';
    c_old_variables constant text :=
        E'            when jsonb_typeof(v_slot -> ''variables_de'') = ''array'' and not v_personal then\n';
    c_new_variables constant text :=
        E'            -- e483_native_personal_coupon: con el cupon, las de coupon_variables_de (20261112210500).\n'
        || E'            when jsonb_typeof(case when v_personal then v_slot -> ''coupon_variables_de''\n'
        || E'                                   else v_slot -> ''variables_de'' end) = ''array'' then\n';
    c_old_variables_in constant text :=
        E'or not (v_slot -> ''variables_de'') @> jsonb_build_array(item.value)) then true\n';
    c_new_variables_in constant text :=
        E'or not (case when v_personal then v_slot -> ''coupon_variables_de''\n'
        || E'                                                 else v_slot -> ''variables_de'' end) @> jsonb_build_array(item.value)) then true\n';
    c_old_sent constant text :=
        E'            || case when v_personal then (v_slot ->> ''coupon_ordinal'')::integer end;\n';
    c_new_sent constant text :=
        E'            || case when v_personal then coalesce(v_ordinal, (v_slot ->> ''coupon_ordinal'')::integer) end;\n';
    c_old_compiled constant text :=
        E'        v_compiled := jsonb_build_object(''mode'', ''freeform'', ''step_key'', v_shape -> (i - 1) ->> ''step_key'');\n';
    c_new_compiled constant text := c_old_compiled
        || E'        -- e483_native_personal_coupon: el envio nativo con su cupon propio lleva su ordinal,\n'
        || E'        -- tambien el primero (20261112210500).\n'
        || E'        if coalesce(v_shape -> (i - 1) -> ''cupon_personal'' = ''true''::jsonb, false)\n'
        || E'           and v_send_coupons[i] is not null then\n'
        || E'            v_compiled := v_compiled || jsonb_build_object(''coupon_ordinal'', v_send_coupons[i]);\n'
        || E'        end if;\n';
    -- 3. Lo que abre la base.
    c_pasos constant text :=
        'case when jsonb_typeof(p_definition -> ''pasos'') = ''array'' then p_definition -> ''pasos'' else ''[]''::jsonb end';
    c_personal_button constant text := '''{"link": "checkout", "cupon": "personal"}''::jsonb';
    c_old_open constant text := E'        -- e483_native_multitouch: begin (20261112210000)\n';
    c_new_open constant text :=
        E'        -- e483_native_personal_coupon: begin (20261112210500)\n'
        || E'        -- El cupon propio: solo {link: checkout, cupon: personal}, solo en el carrito y\n'
        || E'        -- en el pago rechazado, y a lo sumo dos por camino.\n'
        || E'        when exists (select 1 from jsonb_array_elements(' || c_pasos || E') step(value)\n'
        || E'                      where jsonb_typeof(step.value) = ''object''\n'
        || E'                        and jsonb_typeof(step.value -> ''boton'') = ''object''\n'
        || E'                        and step.value #> ''{boton,cupon}'' = ''"personal"''::jsonb\n'
        || E'                        and (step.value -> ''boton'' is distinct from ' || c_personal_button || E'\n'
        || E'                             or coalesce(public._flow_engine_native_trigger(p_definition), '''')\n'
        || E'                                not in (''carrito'', ''pago_fallido'')))\n'
        || E'            then ''motor_nativo:forma''\n'
        || E'        when (select count(*) from jsonb_array_elements(' || c_pasos || E') step(value)\n'
        || E'               where jsonb_typeof(step.value) = ''object''\n'
        || E'                 and step.value -> ''boton'' = ' || c_personal_button || E') > 2\n'
        || E'            then ''motor_nativo:forma''\n'
        || E'        -- e483_native_personal_coupon: end\n'
        || c_old_open;
    c_old_later_button constant text := E'or (step.value ? ''boton'' and step.ordinal > (\n';
    c_new_later_button constant text :=
        E'or (step.value ? ''boton''\n'
        || E'                                         -- e483_native_personal_coupon: menos el del cupon propio (20261112210500).\n'
        || E'                                         and step.value -> ''boton'' is distinct from ' || c_personal_button || E'\n'
        || E'                                         and step.ordinal > (\n';
    c_old_payment_button constant text :=
        E'and not public._flow_engine_native_payment_failure_send_ok(step.value))\n';
    c_new_payment_button constant text :=
        E'and not public._flow_engine_native_payment_failure_send_ok(step.value)\n'
        || E'                            -- e483_native_personal_coupon: o el del cupon propio, sin la cadena (20261112210500).\n'
        || E'                            and not (step.value -> ''boton'' = ' || c_personal_button || E'\n'
        || E'                                     and public._flow_engine_native_payment_failure_send_ok(step.value - ''boton'')))\n';
    -- 5. La siembra del primer envio nativo.
    c_old_seed constant text :=
        E'    -- recovery_followup_steps (20261019000600): el cupon de un paso siguiente\n';
    c_new_seed constant text :=
        E'    -- e483_native_personal_coupon: begin (20261112210500)\n'
        || E'    -- El cupon del primer envio de un recorrido nativo que lo declara, con la\n'
        || E'    -- misma anticipacion y la misma vigencia que el de A1.\n'
        || E'    insert into public.recovery_coupons as coupon (\n'
        || E'        recovery_case_id, ordinal, code, external_product_id, discount,\n'
        || E'        expires_at, status, created_at, updated_at\n'
        || E'    )\n'
        || E'    select native_due.case_id,\n'
        || E'           native_due.ordinal,\n'
        || E'           v_prefix || upper(substr(replace(gen_random_uuid()::text, ''-'', ''''), 1, 12)),\n'
        || E'           native_due.product_id,\n'
        || E'           p_discount,\n'
        || E'           native_due.due_at + p_validity,\n'
        || E'           ''reserved'',\n'
        || E'           p_now,\n'
        || E'           p_now\n'
        || E'    from (\n'
        || E'        select distinct on (rc.id)\n'
        || E'               rc.id as case_id,\n'
        || E'               rc.external_product_id as product_id,\n'
        || E'               sa.due_at,\n'
        || E'               public._flow_engine_native_first_coupon_ordinal(sa.id) as ordinal\n'
        || E'        from public.recovery_cases rc\n'
        || E'        join public.contacts ct on ct.id = rc.contact_id\n'
        || E'        join public.scheduled_actions sa on sa.recovery_case_id = rc.id\n'
        || E'        where rc.status in (''grace_period'', ''active'')\n'
        || E'          and rc.purchase_event_id is null\n'
        || E'          and ct.contact_permission not in (''opted_out'', ''blocked'', ''restricted'')\n'
        || E'          and ct.lifecycle_status <> ''do_not_contact''\n'
        || E'          and sa.action_type = ''first_contact_review''\n'
        || E'          and sa.anchor_type = ''flow_engine_event''\n'
        || E'          and sa.status in (''pending'', ''deferred'', ''retryable_failed'')\n'
        || E'          and sa.due_at - p_lead <= p_now\n'
        || E'          and sa.expires_at > p_now\n'
        || E'          and sa.due_at + p_validity > p_now\n'
        || E'          and public._flow_engine_native_first_coupon_ordinal(sa.id) is not null\n'
        || E'          and not exists (\n'
        || E'              select 1 from public.recovery_coupons existing\n'
        || E'              where existing.recovery_case_id = rc.id\n'
        || E'                and existing.ordinal = public._flow_engine_native_first_coupon_ordinal(sa.id)\n'
        || E'          )\n'
        || E'        order by rc.id, sa.due_at\n'
        || E'    ) native_due\n'
        || E'    order by native_due.due_at\n'
        || E'    limit p_limit\n'
        || E'    on conflict do nothing;\n'
        || E'    -- e483_native_personal_coupon: end\n\n'
        || c_old_seed;
    -- 6. El link del primer envio nativo con su cupon.
    c_old_reserve constant text :=
        E'       ) not in (''1'', ''2'')\n'
        || E'       or v_action.recovery_case_id is null then\n';
    c_new_reserve constant text :=
        E'       ) not in (''1'', ''2'')\n'
        || E'       -- e483_native_personal_coupon: y el primer envio de un recorrido nativo con su\n'
        || E'       -- cupon propio (20261112210500).\n'
        || E'       and public._flow_engine_native_first_coupon_ordinal(v_action.id) is null\n'
        || E'       or v_action.recovery_case_id is null then\n';
    v_text text;
begin
    -- 1. La forma.
    v_text := pg_temp.e483_native_personal_coupon_patch(c_steps, c_old_cart, c_new_cart);
    if length(v_text) - length(replace(v_text, c_old_payment, '')) <> length(c_old_payment) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_steps_definition',
            detail = c_steps;
    end if;
    execute replace(v_text, c_old_payment, c_new_payment);

    -- 2. El compilador.
    v_text := pg_temp.e483_native_personal_coupon_patch(c_core, c_old_declare, c_new_declare);
    if length(v_text) - length(replace(v_text, c_old_personal, '')) <> length(c_old_personal)
       or length(v_text) - length(replace(v_text, c_old_variables, '')) <> length(c_old_variables)
       or length(v_text) - length(replace(v_text, c_old_variables_in, '')) <> length(c_old_variables_in)
       or length(v_text) - length(replace(v_text, c_old_sent, '')) <> length(c_old_sent)
       or length(v_text) - length(replace(v_text, c_old_compiled, '')) <> length(c_old_compiled) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_compile_policy_core_definition',
            detail = c_core;
    end if;
    execute replace(replace(replace(replace(replace(v_text,
        c_old_personal, c_new_personal), c_old_variables, c_new_variables),
        c_old_variables_in, c_new_variables_in), c_old_sent, c_new_sent), c_old_compiled, c_new_compiled);

    -- 3. Lo que abre la base.
    v_text := pg_temp.e483_native_personal_coupon_patch(c_unsupported, c_old_open, c_new_open);
    if length(v_text) - length(replace(v_text, c_old_later_button, '')) <> length(c_old_later_button)
       or length(v_text) - length(replace(v_text, c_old_payment_button, '')) <> length(c_old_payment_button) then
        raise exception using errcode = '55000', message = 'unexpected_flow_engine_native_unsupported_definition',
            detail = c_unsupported;
    end if;
    execute replace(replace(v_text, c_old_later_button, c_new_later_button), c_old_payment_button, c_new_payment_button);

    -- 5. La siembra.
    execute pg_temp.e483_native_personal_coupon_patch(c_claim, c_old_seed, c_new_seed);

    -- 6. El link.
    execute pg_temp.e483_native_personal_coupon_patch(c_reserve, c_old_reserve, c_new_reserve);

    -- Quedo aplicada: las formas y las marcas.
    if public._flow_engine_native_steps('nativo:carrito') -> 2 ->> 'cupon_personal' is distinct from 'true'
       or public._flow_engine_native_steps('nativo:pago_fallido') -> 0 ->> 'cupon_fijo' is distinct from 'true'
       or public._flow_engine_native_steps('nativo:pago_fallido') -> 0 ->> 'cupon_personal' is distinct from 'true'
       or public._flow_engine_native_steps('nativo:intencion') -> 0 ? 'cupon_personal'
       or public._flow_engine_native_steps('nativo:compra') -> 0 ? 'cupon_personal'
       or jsonb_array_length(public._flow_engine_native_steps('nativo:carrito')) <> 3
       or position('e483_native_personal_coupon: end' in pg_get_functiondef(c_core::regprocedure)) = 0
       or position('e483_native_personal_coupon: end' in pg_get_functiondef(c_unsupported::regprocedure)) = 0
       or position('e483_native_personal_coupon: end' in pg_get_functiondef(c_claim::regprocedure)) = 0
       or position('e483_native_personal_coupon' in pg_get_functiondef(c_reserve::regprocedure)) = 0 then
        raise exception using errcode = '55000', message = 'e483_native_personal_coupon_not_applied';
    end if;
end;
$patch$;

drop function pg_temp.e483_native_personal_coupon_patch(text, text, text);

commit;
