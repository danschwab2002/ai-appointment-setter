-- Migration: el link del producto que nombra el agente. Paso 2c-3a de varios
-- productos por aliada.
--
-- Por que. Con varios productos en una instancia (un funnel_ref por producto,
-- decision de Dan del 2026-10-16), el agente dice en `product` de que producto
-- es el link que pide (2b, 20261016000200). La reserva portable
-- (reserve_portable_checkout_issuance_v2, 20261001000100 con el separador de
-- 20261005000100) elige la oferta por defecto con el producto del caso
-- inbound_sales, que es uno solo por conversacion: el del scope entrante, el
-- del producto principal. Desde esa oferta sale todo lo demas: el funnel, la
-- compra que frena, la intencion viva, el sck y la URL. Un segundo caso
-- inbound_sales por producto no sirve: cuatro RPC de 20261001000500 abortan con
-- P0001 si una conversacion tiene dos.
--
-- Que crea. Una funcion nueva, ninguna tabla ni indice:
-- reserve_portable_checkout_issuance_for_funnel_v1, la reserva portable con un
-- parametro mas, p_funnel_ref. Se deriva de la definicion viva con reemplazos
-- contados, como 20261001000100 derivo la portable de la compartida: si alguien
-- cambio la reserva portable, la cuenta falla y hay que mirarlo, en vez de
-- derivar una copia a ciegas. Cambia cinco cosas:
-- - p_funnel_ref es obligatorio y es un slug;
-- - el funnel tiene que tener un binding activo de la aliada del caso en la
--   misma cuenta y el mismo inbox de Chatwoot; si no, blocked_funnel;
-- - la oferta por defecto es la del catalogo del scope entrante con ese funnel,
--   no la del producto del caso;
-- - la oferta de la intencion viva se busca tambien por ese funnel (dos
--   productos pueden compartir el producto de Hotmart);
-- - el replay exige que la emision anterior sea de ese mismo funnel.
-- El caso, el scope, el contacto, la baja, la conversacion y la identidad
-- quedan igual. La autorizacion y el cierre de la emision no cambian: miran el
-- caso y el scope, no la oferta.
--
-- Nombre nuevo y no un noveno parametro con default: el seguimiento portable
-- llama a la reserva por nombre con 8 argumentos (20261010000100) y una
-- sobrecarga lo dejaria ambiguo. Con un solo producto, el bridge sigue usando
-- la reserva de siempre.
--
-- Limite que se acepta: el catalogo admite un solo default activo por scope,
-- version y producto de Hotmart (20260914000100). Dos productos con el mismo
-- hotlink en el mismo scope entrante no pueden tener cada uno su default, y el
-- segundo da missing_default_offer. Es el caso de las ofertas viejas de ATT2
-- dentro del producto de ATT1, que espera la pregunta 6 a Juan.

begin;

do $reserve_for_funnel$
declare
    v_definition text;
    v_name constant text := 'reserve_portable_checkout_issuance_v2';
    v_head constant text := 'public.reserve_portable_checkout_issuance_v2(';
    v_funnel_head constant text := 'public.reserve_portable_checkout_issuance_for_funnel_v1(';
    v_params constant text :=
        'p_issuance_ulid text, p_now timestamp with time zone';
    v_funnel_params constant text :=
        'p_issuance_ulid text, p_funnel_ref text, p_now timestamp with time zone';
    v_invalid constant text :=
        E'       or p_now is null then\n';
    v_funnel_invalid constant text :=
        E'       or p_funnel_ref is null\n'
        || E'       or p_funnel_ref !~ ''^[a-z0-9][a-z0-9-]{0,127}$''\n'
        || E'       or p_now is null then\n';
    v_replay constant text :=
        E'        if v_issuance.commercial_case_id is distinct from p_commercial_case_id\n';
    v_funnel_replay constant text :=
        E'        if v_issuance.commercial_case_id is distinct from p_commercial_case_id\n'
        || E'           or not exists (\n'
        || E'                select 1\n'
        || E'                from public.checkout_offer_catalog replay_offer\n'
        || E'                where replay_offer.id = v_issuance.offer_catalog_id\n'
        || E'                  and replay_offer.funnel_ref = p_funnel_ref\n'
        || E'           )\n';
    v_default_select constant text :=
        E'    select offer.* into v_default_offer\n';
    v_funnel_binding constant text :=
        E'    -- for_funnel: el funnel del producto pedido es de la aliada del caso,\n'
        || E'    -- con un binding activo en la misma cuenta y el mismo inbox.\n'
        || E'    if not exists (\n'
        || E'        select 1\n'
        || E'        from public.commercial_ally_runtime_bindings binding\n'
        || E'        where binding.tenant_ref = v_case.tenant_ref\n'
        || E'          and binding.funnel_ref = p_funnel_ref\n'
        || E'          and binding.status = ''active''\n'
        || E'          and binding.chatwoot_account_id = p_chatwoot_account_id\n'
        || E'          and binding.chatwoot_inbox_id = p_chatwoot_inbox_id\n'
        || E'    ) then\n'
        || E'        return query select ''blocked_funnel''::text, null::uuid, null::text,\n'
        || E'            null::uuid, null::text, null::text, null::text, null::text;\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'\n'
        || E'    select offer.* into v_default_offer\n';
    v_default_product constant text :=
        E'      and lower(offer.product_ref) = lower(v_case.product_ref)\n';
    v_default_funnel constant text :=
        E'      and offer.funnel_ref = p_funnel_ref\n';
    v_lead_offer constant text :=
        E'          and lower(offer.product_ref) = lower(v_default_offer.product_ref)\n';
    v_lead_offer_funnel constant text :=
        E'          and lower(offer.product_ref) = lower(v_default_offer.product_ref)\n'
        || E'          and offer.funnel_ref = v_default_offer.funnel_ref\n';
begin
    select pg_get_functiondef(
        'public.reserve_portable_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamptz)'::regprocedure
    ) into v_definition;
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_name, ''))
          <> length(v_name)
       or length(v_definition) - length(replace(v_definition, v_head, ''))
          <> length(v_head)
       or length(v_definition) - length(replace(v_definition, v_params, ''))
          <> length(v_params)
       or length(v_definition) - length(replace(v_definition, v_invalid, ''))
          <> length(v_invalid)
       or length(v_definition) - length(replace(v_definition, v_replay, ''))
          <> length(v_replay)
       or length(v_definition) - length(replace(v_definition, v_default_select, ''))
          <> length(v_default_select)
       or length(v_definition) - length(replace(v_definition, v_default_product, ''))
          <> length(v_default_product)
       or length(v_definition) - length(replace(v_definition, v_lead_offer, ''))
          <> length(v_lead_offer)
       or position('p_funnel_ref' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_portable_checkout_issuance_reserve_definition';
    end if;
    v_definition := replace(v_definition, v_head, v_funnel_head);
    v_definition := replace(v_definition, v_params, v_funnel_params);
    v_definition := replace(v_definition, v_invalid, v_funnel_invalid);
    v_definition := replace(v_definition, v_replay, v_funnel_replay);
    v_definition := replace(v_definition, v_default_select, v_funnel_binding);
    v_definition := replace(v_definition, v_default_product, v_default_funnel);
    v_definition := replace(v_definition, v_lead_offer, v_lead_offer_funnel);
    -- La portable no se toca: es otra funcion, con otro nombre.
    v_definition := replace(
        v_definition,
        'CREATE OR REPLACE FUNCTION public.reserve_portable_checkout_issuance_for_funnel_v1(',
        'CREATE FUNCTION public.reserve_portable_checkout_issuance_for_funnel_v1('
    );
    execute v_definition;
    if to_regprocedure(
        'public.reserve_portable_checkout_issuance_for_funnel_v1(uuid,text,bigint,bigint,bigint,text,text,text,timestamptz)'
    ) is null then
        raise exception using errcode = '55000',
            message = 'portable_checkout_issuance_for_funnel_reserve_not_created';
    end if;
end;
$reserve_for_funnel$;

-- Supabase le da execute por defecto a toda funcion nueva: se revoca a todos y
-- queda solo para service_role, el rol del bridge.
do $roles$
declare
    v_signature text :=
        'public.reserve_portable_checkout_issuance_for_funnel_v1(uuid,text,bigint,bigint,bigint,text,text,text,timestamptz)';
begin
    execute format('revoke all on function %s from public', v_signature);
    if to_regrole('anon') is not null then
        execute format('revoke all on function %s from anon', v_signature);
    end if;
    if to_regrole('authenticated') is not null then
        execute format('revoke all on function %s from authenticated', v_signature);
    end if;
    if to_regrole('service_role') is not null then
        execute format('revoke all on function %s from service_role', v_signature);
        execute format('grant execute on function %s to service_role', v_signature);
    end if;

    -- Por presencia: service_role la ejecuta y ningun rol de la API.
    if exists (
           select 1 from pg_roles
           where rolname = 'service_role'
             and not has_function_privilege(rolname, v_signature, 'EXECUTE')
       )
       or exists (
           select 1 from pg_roles
           where rolname in ('anon', 'authenticated')
             and has_function_privilege(rolname, v_signature, 'EXECUTE')
       ) then
        raise exception using errcode = '55000',
            message = 'portable_checkout_issuance_for_funnel_acl_unexpected',
            detail = format('solo service_role ejecuta %s', v_signature);
    end if;
end
$roles$;

commit;
