-- Migration: la oferta de la campana en el link del agente (E-408 / E-410,
-- decision de Dan del 2026-11-10).
--
-- Por que. Una campana puede tener su propia oferta de Hotmart dentro del mismo
-- producto (Black de ATT1: wmkq4ba9, con el bono, contra gopi6lh7 por
-- defecto). Hasta aca el link del agente salia con la oferta por defecto del
-- funnel o con la de la intencion viva de la persona (lead_intent): quien
-- entraba a la campana por WhatsApp, sin formulario de esa oferta, recibia la
-- por defecto, sin el bono. El bridge, con AGENT_CAMPAIGN_OFFER_LINK_ENABLED y
-- un segmento vigente con [oferta].codigo_oferta, pide esta reserva.
--
-- Que crea.
-- 1. reserve_portable_checkout_issuance_for_offer_v1: la reserva por funnel
--    (20261016000400, con la baja de 20261024000400) con dos parametros mas,
--    p_offer_code y p_campaign_ref. Se deriva de la definicion VIVA de la de
--    funnel con reemplazos contados: si alguien la cambio, la cuenta falla y
--    hay que mirarlo. Cambia cuatro cosas:
--    - p_offer_code (forma de Hotmart, [A-Za-z0-9]{4,32}) y p_campaign_ref (la
--      clave de la campana, un slug) son obligatorios;
--    - la oferta es la fila activa del catalogo con el mismo tenant, scope,
--      version, producto y funnel que la por defecto, y ese offer_code (la de
--      version mas alta). Si no esta, blocked_campaign_offer y no se escribe
--      nada (D2: nunca la por defecto, que no trae lo que promete el bloque);
--    - la intencion viva de la persona no reemplaza la oferta de la campana
--      (D3), pero su formulario sigue aportando el sck y el fbclid; su oferta
--      queda en lead_offer_code;
--    - offer_resolution = 'campaign' y campaign_ref en la fila; el replay
--      exige la misma oferta y la misma campana (D12).
--    El caso, el scope, el binding del funnel, el contacto, la baja, la
--    conversacion, la identidad y la compra que frena (por producto, con
--    cualquier oferta) quedan igual. La autorizacion y el cierre de la emision
--    no cambian: miran el caso y el scope, no la oferta. La correlacion de la
--    compra por sck tampoco: el sck sigue siendo hermes~v1~<ULID>.
-- 2. checkout_link_issuances.campaign_ref (text null), con su forma y la regla
--    "no nula si y solo si offer_resolution = 'campaign'"; el check de
--    offer_resolution suma 'campaign'; el trigger de inmutabilidad suma la
--    columna.
--
-- Nombre nuevo y no un parametro con default: el seguimiento portable y la
-- reserva por funnel se llaman por nombre y una sobrecarga las dejaria
-- ambiguas. La portable y la de funnel no se tocan.

begin;

-- 1. La columna, sus checks y el valor nuevo de offer_resolution.
alter table public.checkout_link_issuances
    add column campaign_ref text;

alter table public.checkout_link_issuances
    drop constraint checkout_link_issuances_offer_resolution_check;
alter table public.checkout_link_issuances
    add constraint checkout_link_issuances_offer_resolution_check check (
        offer_resolution in (
            'lead_intent', 'default_no_intent', 'default_offer_not_in_catalog',
            'catalog_default', 'recovery_case_offer', 'campaign'
        )
    );
alter table public.checkout_link_issuances
    add constraint checkout_link_issuances_campaign_ref_shape check (
        campaign_ref is null or campaign_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'
    );
alter table public.checkout_link_issuances
    add constraint checkout_link_issuances_campaign_ref_iff_campaign check (
        (offer_resolution = 'campaign') = (campaign_ref is not null)
    );

-- 2. El trigger de inmutabilidad, con la columna nueva. Se deriva de la
--    definicion viva con un reemplazo contado.
do $protect_campaign_ref$
declare
    v_definition text;
    v_last constant text :=
        E'       or old.delivery_attempt_id is distinct from new.delivery_attempt_id\n';
    v_with_campaign constant text :=
        E'       or old.delivery_attempt_id is distinct from new.delivery_attempt_id\n'
        || E'       or old.campaign_ref is distinct from new.campaign_ref\n';
begin
    select pg_get_functiondef('public.protect_checkout_link_issuance()'::regprocedure)
    into v_definition;
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_last, ''))
          <> length(v_last)
       or position('campaign_ref' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_protect_checkout_link_issuance_definition';
    end if;
    execute replace(v_definition, v_last, v_with_campaign);
end;
$protect_campaign_ref$;

-- 3. La reserva con la oferta de la campana.
do $reserve_for_offer$
declare
    v_definition text;
    v_name constant text := 'reserve_portable_checkout_issuance_for_funnel_v1';
    v_head constant text := 'public.reserve_portable_checkout_issuance_for_funnel_v1(';
    v_offer_head constant text := 'public.reserve_portable_checkout_issuance_for_offer_v1(';
    v_params constant text :=
        'p_issuance_ulid text, p_funnel_ref text, p_now timestamp with time zone';
    v_offer_params constant text :=
        'p_issuance_ulid text, p_funnel_ref text, p_offer_code text, '
        || 'p_campaign_ref text, p_now timestamp with time zone';
    v_invalid constant text :=
        E'       or p_funnel_ref !~ ''^[a-z0-9][a-z0-9-]{0,127}$''\n'
        || E'       or p_now is null then\n';
    v_offer_invalid constant text :=
        E'       or p_funnel_ref !~ ''^[a-z0-9][a-z0-9-]{0,127}$''\n'
        || E'       or p_offer_code is null\n'
        || E'       or p_offer_code !~ ''^[A-Za-z0-9]{4,32}$''\n'
        || E'       or p_campaign_ref is null\n'
        || E'       or p_campaign_ref !~ ''^[a-z0-9][a-z0-9-]{0,127}$''\n'
        || E'       or p_now is null then\n';
    v_replay constant text :=
        E'                  and replay_offer.funnel_ref = p_funnel_ref\n'
        || E'           )\n';
    v_offer_replay constant text :=
        E'                  and replay_offer.funnel_ref = p_funnel_ref\n'
        || E'                  and replay_offer.offer_code = p_offer_code\n'
        || E'           )\n'
        || E'           -- for_offer: el mismo mensaje, reservado con otra oferta o\n'
        || E'           -- para otra campana, no se reusa (D12 de E-408).\n'
        || E'           or v_issuance.offer_resolution is distinct from ''campaign''\n'
        || E'           or v_issuance.campaign_ref is distinct from p_campaign_ref\n';
    -- La resolucion de la oferta de la de funnel, entera: desde que encontro
    -- (o no) la intencion viva hasta que eligio la oferta.
    v_resolution constant text := $old$    if found then
        v_lead_offer_code := v_lead_intent.offer_ref;
        select offer.* into v_offer
        from public.checkout_offer_catalog offer
        where offer.tenant_ref = v_default_offer.tenant_ref
          and offer.scope_key = v_default_offer.scope_key
          and offer.scope_version = v_default_offer.scope_version
          and lower(offer.product_ref) = lower(v_default_offer.product_ref)
          and offer.funnel_ref = v_default_offer.funnel_ref
          and offer.landing_ref = v_lead_intent.landing_ref
          and offer.offer_code = v_lead_intent.offer_ref
          and offer.status = 'active'
        order by offer.version desc
        limit 1
        for share;
        if found then
            v_offer_resolution := 'lead_intent';
        else
            v_offer := v_default_offer;
            v_offer_resolution := 'default_offer_not_in_catalog';
        end if;
    else
        v_offer := v_default_offer;
        v_offer_resolution := 'default_no_intent';
    end if;
$old$;
    v_offer_resolution_block constant text := $new$    -- for_offer: la oferta es la de la campana (D3 de E-408). La intencion
    -- viva de la persona no la reemplaza: queda su oferta en lead_offer_code y
    -- su formulario sigue aportando el sck y el fbclid. Sin la oferta activa en
    -- el catalogo del funnel no se escribe nada y el bridge deriva (D2).
    v_lead_offer_code := v_lead_intent.offer_ref;
    select offer.* into v_offer
    from public.checkout_offer_catalog offer
    where offer.tenant_ref = v_default_offer.tenant_ref
      and offer.scope_key = v_default_offer.scope_key
      and offer.scope_version = v_default_offer.scope_version
      and lower(offer.product_ref) = lower(v_default_offer.product_ref)
      and offer.funnel_ref = v_default_offer.funnel_ref
      and offer.offer_code = p_offer_code
      and offer.status = 'active'
    order by offer.version desc
    limit 1
    for share;
    if not found then
        return query select 'blocked_campaign_offer'::text, null::uuid, null::text,
            null::uuid, null::text, null::text, null::text, null::text;
        return;
    end if;
    v_offer_resolution := 'campaign';
$new$;
    v_insert_columns constant text :=
        E'        attribution_resolution, dropped_unsafe_fields\n    ) values (\n';
    v_offer_insert_columns constant text :=
        E'        attribution_resolution, dropped_unsafe_fields, campaign_ref\n    ) values (\n';
    v_insert_values constant text :=
        E'        v_attribution_resolution, v_dropped_unsafe\n    ) on conflict (\n';
    v_offer_insert_values constant text :=
        E'        v_attribution_resolution, v_dropped_unsafe, p_campaign_ref\n    ) on conflict (\n';
begin
    select pg_get_functiondef(
        'public.reserve_portable_checkout_issuance_for_funnel_v1(uuid,text,bigint,bigint,bigint,text,text,text,timestamptz)'::regprocedure
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
       or length(v_definition) - length(replace(v_definition, v_resolution, ''))
          <> length(v_resolution)
       or length(v_definition) - length(replace(v_definition, v_insert_columns, ''))
          <> length(v_insert_columns)
       or length(v_definition) - length(replace(v_definition, v_insert_values, ''))
          <> length(v_insert_values)
       or position('p_offer_code' in v_definition) > 0
       or position('campaign_ref' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_portable_checkout_issuance_for_funnel_definition';
    end if;
    v_definition := replace(v_definition, v_head, v_offer_head);
    v_definition := replace(v_definition, v_params, v_offer_params);
    v_definition := replace(v_definition, v_invalid, v_offer_invalid);
    v_definition := replace(v_definition, v_replay, v_offer_replay);
    v_definition := replace(v_definition, v_resolution, v_offer_resolution_block);
    v_definition := replace(v_definition, v_insert_columns, v_offer_insert_columns);
    v_definition := replace(v_definition, v_insert_values, v_offer_insert_values);
    -- La de funnel no se toca: es otra funcion, con otro nombre.
    v_definition := replace(
        v_definition,
        'CREATE OR REPLACE FUNCTION public.reserve_portable_checkout_issuance_for_offer_v1(',
        'CREATE FUNCTION public.reserve_portable_checkout_issuance_for_offer_v1('
    );
    execute v_definition;
    if to_regprocedure(
        'public.reserve_portable_checkout_issuance_for_offer_v1(uuid,text,bigint,bigint,bigint,text,text,text,text,text,timestamptz)'
    ) is null then
        raise exception using errcode = '55000',
            message = 'portable_checkout_issuance_for_offer_reserve_not_created';
    end if;
end;
$reserve_for_offer$;

-- Supabase le da execute por defecto a toda funcion nueva: se revoca a todos y
-- queda solo para service_role, el rol del bridge.
do $roles$
declare
    v_signature text :=
        'public.reserve_portable_checkout_issuance_for_offer_v1(uuid,text,bigint,bigint,bigint,text,text,text,text,text,timestamptz)';
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
            message = 'portable_checkout_issuance_for_offer_acl_unexpected',
            detail = format('solo service_role ejecuta %s', v_signature);
    end if;
end
$roles$;

commit;
