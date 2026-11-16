-- product-card-v1: publicar con siembra, pausar, reanudar, cerrar y descartar
-- el producto que sumo el Automation Expert; una campana viva por producto
-- (E-575, MP-08 del plan de multiproducto). Contrato:
-- docs/contracts/product-card-v1.md §7.
--
-- Por que. MP-07 (20261115000100) dejo el almacen: el AE propone y valida, y la
-- base congela 24 horas lo validado con su huella. Faltaba el si del duenio y
-- lo que hoy hace una persona con psql despues de un PR: sembrar el producto
-- (scripts/provision_instance_product.py). Esta migracion lo hace en la misma
-- transaccion del si, idempotente.
--
-- 1. Seis funciones de la pagina, solo para flow_change_approver
--    (20261028000100), todas con la sesion viva (el actor slack sale de la
--    sesion, nunca de un parametro): releer, publicar, pausar, reanudar, cerrar
--    y descartar.
--    - Publicar exige la huella de lo validado y la validacion sin vencer,
--      vuelve a mirar las colisiones adentro de la transaccion (D11 de E-569) y
--      siembra lo mismo que provision_instance_product.py: el binding (con el
--      precio y la moneda de la oferta por defecto), una fila del catalogo de
--      ofertas por oferta, un scope de intencion de compra por oferta y la
--      politica de compra. Una version nueva de una clave publicada cierra la
--      anterior (cerrada por sistema, nueva_version).
--    - Pausar y cerrar pausan primero los flujos prendidos del producto
--      (_flow_change_pause, como la pagina) y sus campanas publicadas. Reanudar
--      vuelve solo el producto: flujos y campanas quedan pausados.
-- 2. Una del Automation Expert, solo para automation_expert_change: descartar
--    sus borradores y validadas (D7 de E-569: MP-07 no lo tenia).
-- 3. Una para service_role: la lectura en vivo de los productos publicados y
--    pausados, con su binding activo (la usa MP-09).
-- 4. Una campana viva por producto (D6 de Dan, 2026-11-13). agent_campaigns
--    suma live_scope: '' para todas las de hoy y para cualquiera cuyo
--    oferta.producto no nombre un producto del AE; la clave de ese producto si
--    lo nombra. agent_campaigns_one_live pasa a (tenant_ref, ally_ref,
--    live_scope). Publicar y reanudar una campana se reescriben solo para eso:
--    sin productos del AE, todo live_scope es '' y el resultado es el de hoy.
--
-- Sin AGENT_PRODUCTS_ENABLED el bridge no llama a ninguna de estas funciones, y
-- sin productos del AE el cambio de agent_campaigns no cambia ningun resultado.
-- Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; 28000 sin sesion; P0002 no existe o no es de
-- esta instancia. Los resultados del ciclo vuelven como {"ok": false,
-- "reason_code"}.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.agent_products') is null
       or to_regclass('public.agent_campaigns') is null
       or to_regprocedure('public._flow_change_pause(text,text,text,text,bigint,text,text,text)') is null
       or to_regprocedure('public._flow_approval_session_actor(text,text,text)') is null
       or to_regprocedure('public._agent_product_view(public.agent_products)') is null then
        raise exception using errcode = '55000', message = 'agent_products_publish_prerequisite_missing';
    end if;
    if to_regprocedure('public.approver_publish_agent_product_v1(jsonb,text,uuid,text,text)') is not null then
        raise exception using errcode = '55000', message = 'agent_products_publish_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. El AE puede descartar lo suyo: closed_by_kind = automation_expert, solo en
--    un descartado.
-- ===========================================================================

alter table public.agent_products drop constraint agent_products_closed_by_kind_check;
alter table public.agent_products add constraint agent_products_closed_by_kind_check
    check (closed_by_kind in ('slack', 'sistema')
           or (closed_by_kind = 'automation_expert' and status = 'descartada'));

-- Una sola version viva (publicada o pausada) por clave: publicar una version
-- nueva cierra la anterior, y el indice lo hace cumplir.
create unique index agent_products_one_open_live_per_key
    on public.agent_products (tenant_ref, ally_ref, product_key) where status in ('publicada', 'pausada');

-- ===========================================================================
-- 2. Una campana viva por producto (D6).
-- ===========================================================================

alter table public.agent_campaigns add column live_scope text not null default ''
    check (live_scope = '' or (live_scope ~ '^[a-z0-9]+(-[a-z0-9]+)*$' and length(live_scope) <= 40));

drop index public.agent_campaigns_one_live;
-- **Una sola viva por producto.** '' es el lugar de siempre (la instancia):
-- el principal, los de [[productos]] y toda campana sin oferta.producto.
create unique index agent_campaigns_one_live
    on public.agent_campaigns (tenant_ref, ally_ref, live_scope) where status = 'publicada';

-- El lugar de una campana: la clave de un producto del AE si oferta.producto lo
-- nombra (cualquier version no descartada), '' si no. La base no conoce las
-- claves del manifiesto: el principal y [[productos]] siguen compartiendo el
-- lugar de la instancia, como hoy.
create function public._agent_campaign_live_scope(p_tenant_ref text, p_ally_ref text, p_draft jsonb)
returns text
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select coalesce((
        select p.product_key from public.agent_products p
         where p.tenant_ref = p_tenant_ref and p.ally_ref = p_ally_ref
           and p.product_key = p_draft -> 'oferta' ->> 'producto'
           and p.status <> 'descartada'
         limit 1), '')
$function$;

-- Igual que en 20261108005000, salvo el lugar: la otra viva que frena es la del
-- mismo live_scope, y una campana de un producto del AE se publica solo con el
-- producto publicado.
create or replace function public.approver_publish_agent_campaign_v1(
    p_instance jsonb, p_session_hash text, p_campaign_id uuid, p_fingerprint text, p_check_sha text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_actor text;
    v_campaign public.agent_campaigns%rowtype;
    v_other public.agent_campaigns%rowtype;
    v_now timestamptz;
    v_scope text;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not coalesce(p_fingerprint ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'agent_campaign_fingerprint_invalid';
    end if;
    if not coalesce(p_check_sha ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'agent_campaign_check_sha_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_campaigns:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_campaign from public.agent_campaigns c
     where c.id = p_campaign_id and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_campaign_not_found';
    end if;
    if public._agent_campaign_expire_if_due(v_campaign) then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_expired');
    end if;
    if v_campaign.fingerprint is not null and p_fingerprint is distinct from v_campaign.fingerprint then
        raise exception using errcode = '22023', message = 'agent_campaign_fingerprint_mismatch';
    end if;
    if v_campaign.status in ('publicada', 'pausada', 'terminada') then
        return jsonb_build_object('ok', true, 'replayed', true, 'status', v_campaign.status);
    end if;
    if v_campaign.status is distinct from 'validada' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_not_validated',
            'status', v_campaign.status);
    end if;
    v_now := clock_timestamp();
    if v_campaign.check_sha is null then
        perform public._agent_campaign_event(v_campaign, 'agent_campaign_publish_refused', 'slack', v_actor,
            'agent_campaign_check_missing', '{}'::jsonb);
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_check_missing');
    end if;
    if p_check_sha is distinct from v_campaign.check_sha then
        perform public._agent_campaign_event(v_campaign, 'agent_campaign_publish_refused', 'slack', v_actor,
            'agent_campaign_check_stale', '{}'::jsonb);
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_check_stale');
    end if;
    if v_campaign.valid_until <= v_now then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_window_over');
    end if;
    v_scope := public._agent_campaign_live_scope(v_instance.o_tenant_ref, v_instance.o_ally_ref, v_campaign.draft);
    if v_scope <> '' and not exists (
        select 1 from public.agent_products p
         where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
           and p.product_key = v_scope and p.status = 'publicada') then
        perform public._agent_campaign_event(v_campaign, 'agent_campaign_publish_refused', 'slack', v_actor,
            'agent_campaign_product_not_live', jsonb_build_object('product_key', v_scope));
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_product_not_live');
    end if;
    -- Una viva cuya vigencia ya termino no retiene el lugar (de ningun producto).
    for v_other in
        select * from public.agent_campaigns c
         where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
           and c.status = 'publicada' and c.valid_until <= v_now and c.id <> v_campaign.id
         for update
    loop
        update public.agent_campaigns
           set status = 'terminada', closed_at = v_now, closed_by_kind = 'sistema', closed_by_ref = 'vigencia'
         where id = v_other.id;
        perform public._agent_campaign_event(v_other, 'agent_campaign_finished', 'sistema', 'vigencia',
            'agent_campaign_window_over', jsonb_build_object('valid_until', v_other.valid_until));
    end loop;
    if exists (select 1 from public.agent_campaigns c
                where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
                  and c.status = 'publicada' and c.live_scope = v_scope and c.id <> v_campaign.id) then
        perform public._agent_campaign_event(v_campaign, 'agent_campaign_publish_refused', 'slack', v_actor,
            'agent_campaign_live_exists', '{}'::jsonb);
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_live_exists');
    end if;
    update public.agent_campaigns
       set status = 'publicada', approved_at = v_now, approved_by_kind = 'slack', approved_by_ref = v_actor,
           live_scope = v_scope
     where id = v_campaign.id;
    perform public._agent_campaign_event(v_campaign, 'agent_campaign_published', 'slack', v_actor,
        'agent_campaign_published', jsonb_build_object('fingerprint', v_campaign.fingerprint,
            'check_sha', v_campaign.check_sha, 'segment_version', v_campaign.segment_version));
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'publicada');
end;
$function$;

-- Igual que en 20261108005000, salvo el lugar: la otra viva que frena es la del
-- mismo live_scope, y solo las vencidas de ese lugar pasan a terminada (antes
-- eran todas las de la instancia, que era el mismo lugar).
create or replace function public.approver_resume_agent_campaign_v1(
    p_instance jsonb, p_session_hash text, p_campaign_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_actor text;
    v_campaign public.agent_campaigns%rowtype;
    v_other public.agent_campaigns%rowtype;
    v_scope text;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_campaigns:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_campaign from public.agent_campaigns c
     where c.id = p_campaign_id and c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_campaign_not_found';
    end if;
    if v_campaign.status = 'publicada' then
        return jsonb_build_object('ok', true, 'replayed', true, 'status', 'publicada');
    end if;
    if v_campaign.status is distinct from 'pausada' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_not_paused',
            'status', v_campaign.status);
    end if;
    if v_campaign.valid_until <= clock_timestamp() then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_window_over');
    end if;
    v_scope := v_campaign.live_scope;
    if v_scope <> '' and not exists (
        select 1 from public.agent_products p
         where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
           and p.product_key = v_scope and p.status = 'publicada') then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_product_not_live');
    end if;
    if exists (select 1 from public.agent_campaigns c
                where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
                  and c.status = 'publicada' and c.live_scope = v_scope and c.id <> v_campaign.id
                  and c.valid_until > clock_timestamp()) then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_campaign_live_exists');
    end if;
    -- Una viva vencida del mismo lugar no lo retiene: el indice unico la frenaria igual.
    for v_other in
        select * from public.agent_campaigns c
         where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
           and c.status = 'publicada' and c.live_scope = v_scope and c.id <> v_campaign.id
         for update
    loop
        update public.agent_campaigns
           set status = 'terminada', closed_at = clock_timestamp(), closed_by_kind = 'sistema', closed_by_ref = 'vigencia'
         where id = v_other.id;
        perform public._agent_campaign_event(v_other, 'agent_campaign_finished', 'sistema', 'vigencia',
            'agent_campaign_window_over', jsonb_build_object('valid_until', v_other.valid_until));
    end loop;
    update public.agent_campaigns
       set status = 'publicada', paused_at = null, paused_by_ref = null, pause_reason = null
     where id = v_campaign.id;
    perform public._agent_campaign_event(v_campaign, 'agent_campaign_resumed', 'slack', v_actor,
        'agent_campaign_resumed', '{}'::jsonb);
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'publicada');
end;
$function$;

-- ===========================================================================
-- 3. Los helpers de la siembra y de la pausa (security invoker, de nadie).
-- ===========================================================================

-- La oferta por defecto del minimo (el CHECK de la tabla garantiza una).
create function public._agent_product_default_offer(p_minimum jsonb)
returns jsonb
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select o from jsonb_array_elements(p_minimum -> 'ofertas') o where (o ->> 'por_defecto')::boolean limit 1
$function$;

-- Los codigos de las ofertas que no son la por defecto, en el orden del minimo.
create function public._agent_product_additional_codes(p_minimum jsonb)
returns text[]
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select coalesce(array_agg(o.value ->> 'codigo' order by o.ordinality), '{}'::text[])
      from jsonb_array_elements(p_minimum -> 'ofertas') with ordinality o
     where not (o.value ->> 'por_defecto')::boolean
$function$;

-- Lo que impide sembrar, o null. Corre con el lock de la instancia tomado, asi
-- que ve lo mismo que va a escribir. Lo de los alias lo vuelve a mirar el
-- bridge antes de llamar (la normalizacion es suya).
create function public._agent_product_seed_conflict(
    p_product public.agent_products, p_principal public.commercial_ally_runtime_bindings)
returns text
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_key text := p_product.product_key;
    v_codes text[];
    v_ours boolean;
begin
    select coalesce(array_agg(o ->> 'codigo'), '{}'::text[]) into v_codes
      from jsonb_array_elements(p_product.minimum -> 'ofertas') o;
    -- El binding lleva hasta 16 ofertas ademas de la por defecto, sin repetir.
    if cardinality(v_codes) > 17 or cardinality(v_codes) <> (select count(distinct c) from unnest(v_codes) c) then
        return 'agent_product_offers_do_not_fit';
    end if;
    -- Un binding con la clave como funnel_ref es nuestro solo si ya se publico
    -- una version de esta clave; si no, es del manifiesto (o del principal).
    v_ours := exists (select 1 from public.agent_products p
                       where p.tenant_ref = p_product.tenant_ref and p.ally_ref = p_product.ally_ref
                         and p.product_key = v_key and p.id <> p_product.id
                         and p.status in ('publicada', 'pausada', 'cerrada'));
    if v_key = p_principal.funnel_ref or (not v_ours and exists (
        select 1 from public.commercial_ally_runtime_bindings b
         where b.tenant_ref = p_product.tenant_ref and b.funnel_ref = v_key)) then
        return 'agent_product_key_taken';
    end if;
    if exists (select 1 from public.commercial_ally_runtime_bindings b
                where b.tenant_ref = p_product.tenant_ref and b.status = 'active' and b.funnel_ref <> v_key
                  and b.hotmart_product_id = p_product.hotmart_product_id)
       or exists (select 1 from public.agent_products p
                   where p.tenant_ref = p_product.tenant_ref and p.ally_ref = p_product.ally_ref
                     and p.product_key <> v_key and p.status <> 'descartada'
                     and p.hotmart_product_id = p_product.hotmart_product_id)
       or exists (select 1 from public.hotmart_purchase_intent_scopes s
                   where s.active and not (s.tenant_ref = p_product.tenant_ref and s.funnel_ref = v_key)
                     and s.hotmart_product_id = p_product.hotmart_product_id::text
                     and s.offer_ref = any(v_codes)) then
        return 'agent_product_hotmart_product_taken';
    end if;
    if exists (select 1 from public.commercial_ally_runtime_bindings b
                where b.tenant_ref = p_product.tenant_ref and b.status = 'active' and b.funnel_ref <> v_key
                  and (b.offer_code = any(v_codes) or b.additional_offer_codes && v_codes))
       or exists (select 1 from public.checkout_offer_catalog c
                   where c.tenant_ref = p_product.tenant_ref and c.status = 'active' and c.funnel_ref <> v_key
                     and c.offer_code = any(v_codes))
       or exists (select 1 from public.agent_products p, jsonb_array_elements(p.minimum -> 'ofertas') o
                   where p.tenant_ref = p_product.tenant_ref and p.ally_ref = p_product.ally_ref
                     and p.product_key <> v_key and p.status <> 'descartada'
                     and o ->> 'codigo' = any(v_codes)) then
        return 'agent_product_offer_code_taken';
    end if;
    if exists (select 1 from public.checkout_offer_catalog c
                where c.tenant_ref = p_product.tenant_ref and c.status = 'active' and c.funnel_ref <> v_key
                  and c.scope_key = p_principal.inbound_scope_key
                  and c.scope_version = p_principal.inbound_scope_version
                  and lower(c.product_ref) = lower(p_product.minimum -> 'hotmart' ->> 'hotlink')) then
        return 'agent_product_hotlink_taken';
    end if;
    return null;
end;
$function$;

-- La siembra: lo que imprime provision_instance_product.py para un producto de
-- [[productos]], con los datos del minimo y lo de la instancia copiado del
-- binding del principal. Idempotente: lo que ya esta igual no se toca; lo que
-- cambio en una version nueva retira la fila vieja y agrega la nueva (el
-- binding con binding_version + 1, como subir binding_version en el
-- manifiesto: el binding viejo queda retired y un caso en vuelo atado a el ya
-- no lo resuelve, igual que hoy con un bump del manifiesto). Devuelve que hizo.
create function public._agent_product_seed(
    p_product public.agent_products, p_principal public.commercial_ally_runtime_bindings, p_actor text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_key text := p_product.product_key;
    v_minimum jsonb := p_product.minimum;
    v_default jsonb := public._agent_product_default_offer(p_product.minimum);
    v_extra text[] := public._agent_product_additional_codes(p_product.minimum);
    v_hotlink text := p_product.minimum -> 'hotmart' ->> 'hotlink';
    v_active public.commercial_ally_runtime_bindings%rowtype;
    v_version integer;
    v_binding text;
    v_offer jsonb;
    v_is_default boolean;
    v_inserted integer := 0;
    v_kept integer := 0;
    v_retired integer;
    v_scopes_inserted integer := 0;
    v_scopes_off integer;
    v_now timestamptz := clock_timestamp();
begin
    -- El binding.
    select * into v_active from public.commercial_ally_runtime_bindings b
     where b.tenant_ref = p_product.tenant_ref and b.funnel_ref = v_key and b.status = 'active'
     for update;
    if found and v_active.ally_ref = p_product.ally_ref
       and v_active.lead_ally_name = p_principal.lead_ally_name
       and v_active.lead_site = p_principal.lead_site and v_active.lead_landing_id = v_key
       and v_active.lead_page_host = p_principal.lead_page_host and v_active.lead_page_path = '/' || v_key
       and v_active.product_hotlink = v_hotlink and v_active.product_name = v_minimum ->> 'nombre'
       and v_active.product_price = (v_default ->> 'precio')::numeric and v_active.currency = v_default ->> 'moneda'
       and v_active.offer_code = v_default ->> 'codigo'
       and v_active.consent_copy_version = p_principal.consent_copy_version
       and v_active.hotmart_product_id = p_product.hotmart_product_id
       and v_active.chatwoot_account_id = p_principal.chatwoot_account_id
       and v_active.chatwoot_inbox_id = p_principal.chatwoot_inbox_id
       and v_active.inbound_scope_key = p_principal.inbound_scope_key
       and v_active.inbound_scope_version = p_principal.inbound_scope_version
       and v_active.additional_offer_codes = v_extra
       and v_active.additional_offer_landings = '[]'::jsonb then
        v_version := v_active.binding_version;
        v_binding := 'igual';
    else
        if found then
            update public.commercial_ally_runtime_bindings set status = 'retired', updated_at = v_now
             where tenant_ref = v_active.tenant_ref and funnel_ref = v_active.funnel_ref
               and binding_version = v_active.binding_version;
            v_binding := 'nuevo';
        else
            v_binding := 'creado';
        end if;
        select coalesce(max(b.binding_version), 0) + 1 into v_version from public.commercial_ally_runtime_bindings b
         where b.tenant_ref = p_product.tenant_ref and b.funnel_ref = v_key;
        -- Sin landing propia (el minimo no la trae, D8): una nominal con la clave,
        -- en el sitio del principal. Ningun formulario la usa.
        insert into public.commercial_ally_runtime_bindings (
            tenant_ref, funnel_ref, binding_version, status, ally_ref, lead_ally_name, lead_site, lead_landing_id,
            lead_page_host, lead_page_path, product_hotlink, product_name, product_price, currency, offer_code,
            consent_copy_version, hotmart_product_id, chatwoot_account_id, chatwoot_inbox_id, inbound_scope_key,
            inbound_scope_version, additional_offer_codes, additional_offer_landings)
        values (
            p_product.tenant_ref, v_key, v_version, 'active', p_product.ally_ref, p_principal.lead_ally_name,
            p_principal.lead_site, v_key, p_principal.lead_page_host, '/' || v_key, v_hotlink,
            v_minimum ->> 'nombre', (v_default ->> 'precio')::numeric, v_default ->> 'moneda', v_default ->> 'codigo',
            p_principal.consent_copy_version, p_product.hotmart_product_id, p_principal.chatwoot_account_id,
            p_principal.chatwoot_inbox_id, p_principal.inbound_scope_key, p_principal.inbound_scope_version,
            v_extra, '[]'::jsonb);
    end if;

    -- El catalogo: una fila activa por oferta. Primero se retira lo que ya no es
    -- (libera el indice de la por defecto), despues se agrega lo que falta.
    with retired as (
        update public.checkout_offer_catalog c set status = 'retired'
         where c.tenant_ref = p_product.tenant_ref and c.funnel_ref = v_key and c.status = 'active'
           and not exists (
               select 1 from jsonb_array_elements(v_minimum -> 'ofertas') o
                where o ->> 'codigo' = c.offer_code
                  and c.scope_key = p_principal.inbound_scope_key
                  and c.scope_version = p_principal.inbound_scope_version
                  and c.product_ref = v_hotlink and c.landing_ref = v_key
                  and c.checkout_mode = 10
                  and c.default_for_inbound = (o ->> 'por_defecto')::boolean)
        returning 1)
    select count(*) into v_retired from retired;
    for v_offer in select o from jsonb_array_elements(v_minimum -> 'ofertas') o loop
        v_is_default := (v_offer ->> 'por_defecto')::boolean;
        if exists (select 1 from public.checkout_offer_catalog c
                    where c.tenant_ref = p_product.tenant_ref and c.funnel_ref = v_key and c.status = 'active'
                      and c.offer_code = v_offer ->> 'codigo') then
            v_kept := v_kept + 1;
            continue;
        end if;
        insert into public.checkout_offer_catalog (
            tenant_ref, funnel_ref, scope_key, scope_version, product_ref, landing_ref, offer_code,
            checkout_base_url, checkout_mode, default_for_inbound, status, version, approved_by, approved_at)
        select p_product.tenant_ref, v_key, p_principal.inbound_scope_key, p_principal.inbound_scope_version,
               v_hotlink, v_key, v_offer ->> 'codigo', 'https://pay.hotmart.com/' || v_hotlink, 10, v_is_default,
               'active', coalesce(max(c.version), 0) + 1, p_actor, v_now
          from public.checkout_offer_catalog c
         where c.tenant_ref = p_product.tenant_ref and c.scope_key = p_principal.inbound_scope_key
           and c.scope_version = p_principal.inbound_scope_version and c.product_ref = v_hotlink
           and c.offer_code = v_offer ->> 'codigo';
        v_inserted := v_inserted + 1;
    end loop;

    -- Los scopes de intencion de compra: uno activo por oferta.
    with off as (
        update public.hotmart_purchase_intent_scopes s set active = false, updated_at = v_now
         where s.tenant_ref = p_product.tenant_ref and s.funnel_ref = v_key and s.active
           and not exists (
               select 1 from jsonb_array_elements(v_minimum -> 'ofertas') o
                where o ->> 'codigo' = s.offer_ref
                  and s.hotmart_product_id = p_product.hotmart_product_id::text
                  and s.purchase_intent_product_ref = v_hotlink)
        returning 1)
    select count(*) into v_scopes_off from off;
    for v_offer in select o from jsonb_array_elements(v_minimum -> 'ofertas') o loop
        if exists (select 1 from public.hotmart_purchase_intent_scopes s
                    where s.tenant_ref = p_product.tenant_ref and s.funnel_ref = v_key and s.active
                      and s.offer_ref = v_offer ->> 'codigo') then
            continue;
        end if;
        insert into public.hotmart_purchase_intent_scopes (
            tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref, offer_ref, max_lookback, active)
        values (p_product.tenant_ref, v_key, p_product.hotmart_product_id::text, v_hotlink, v_offer ->> 'codigo',
                interval '24 hours', true);
        v_scopes_inserted := v_scopes_inserted + 1;
    end loop;

    -- La politica de compra del binding vigente.
    insert into public.commercial_ally_hotmart_purchase_policies (
        tenant_ref, funnel_ref, binding_version, enabled, max_lookback)
    values (p_product.tenant_ref, v_key, v_version, true, interval '7 days')
    on conflict do nothing;

    return jsonb_build_object('funnel_ref', v_key, 'binding_version', v_version, 'binding', v_binding,
        'catalog', jsonb_build_object('inserted', v_inserted, 'kept', v_kept, 'retired', v_retired),
        'intent_scopes', jsonb_build_object('inserted', v_scopes_inserted, 'deactivated', v_scopes_off));
end;
$function$;

-- Antes de pausar o cerrar un producto: sus flujos prendidos pasan a pausado
-- (como la pagina, con la generacion de la fila) y sus campanas publicadas a
-- pausada. Devuelve cuantos.
create function public._agent_product_pause_dependents(
    p_product public.agent_products, p_actor text, p_reason text, p_reason_code text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_flow public.flow_controls%rowtype;
    v_campaign public.agent_campaigns%rowtype;
    v_result jsonb;
    v_flows integer := 0;
    v_campaigns integer := 0;
begin
    for v_flow in
        select * from public.flow_controls c
         where c.tenant_ref = p_product.tenant_ref and c.ally_ref = p_product.ally_ref
           and c.product_ref = p_product.product_key and c.state = 'prendido'
         order by c.flow_key
         for update
    loop
        v_result := public._flow_change_pause(v_flow.tenant_ref, v_flow.ally_ref, v_flow.product_ref,
            v_flow.flow_key, v_flow.generation, 'slack', p_actor, p_reason);
        if coalesce((v_result ->> 'changed')::boolean, false) then
            v_flows := v_flows + 1;
        end if;
    end loop;
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_campaigns:' || p_product.tenant_ref || ':' || p_product.ally_ref, 0));
    for v_campaign in
        select * from public.agent_campaigns c
         where c.tenant_ref = p_product.tenant_ref and c.ally_ref = p_product.ally_ref
           and c.status = 'publicada' and c.live_scope = p_product.product_key
         for update
    loop
        update public.agent_campaigns
           set status = 'pausada', paused_at = clock_timestamp(), paused_by_ref = p_actor, pause_reason = p_reason
         where id = v_campaign.id;
        perform public._agent_campaign_event(v_campaign, 'agent_campaign_paused', 'slack', p_actor,
            p_reason_code, jsonb_build_object('product_id', p_product.id,
                'reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
        v_campaigns := v_campaigns + 1;
    end loop;
    return jsonb_build_object('flows_paused', v_flows, 'campaigns_paused', v_campaigns);
end;
$function$;

-- ===========================================================================
-- 4. Las seis de la pagina, con la sesion viva.
-- ===========================================================================

create function public.approver_get_agent_product_v1(p_instance jsonb, p_session_hash text, p_product_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_product public.agent_products%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    select * into v_product from public.agent_products p
     where p.id = p_product_id and p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_product_not_found';
    end if;
    return public._agent_product_view(v_product);
end;
$function$;

-- Publicar: el si del duenio, con la huella que mostro la pagina y el
-- funnel_ref del principal (de donde se copian la aliada, Chatwoot, el scope
-- entrante y el consentimiento). Siembra en la misma transaccion.
create function public.approver_publish_agent_product_v1(
    p_instance jsonb, p_session_hash text, p_product_id uuid, p_fingerprint text, p_principal_ref text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_actor text;
    v_product public.agent_products%rowtype;
    v_other public.agent_products%rowtype;
    v_principal public.commercial_ally_runtime_bindings%rowtype;
    v_conflict text;
    v_seed jsonb;
    v_now timestamptz;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not coalesce(p_fingerprint ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'agent_product_fingerprint_invalid';
    end if;
    if not coalesce(p_principal_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$', false) then
        raise exception using errcode = '22023', message = 'agent_product_principal_invalid';
    end if;
    -- El mismo lock que proponer: nadie guarda ni publica otro producto en el medio.
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_products:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_product from public.agent_products p
     where p.id = p_product_id and p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_product_not_found';
    end if;
    if public._agent_product_expire_if_due(v_product) then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_product_expired');
    end if;
    if v_product.fingerprint is not null and p_fingerprint is distinct from v_product.fingerprint then
        raise exception using errcode = '22023', message = 'agent_product_fingerprint_mismatch';
    end if;
    if v_product.status in ('publicada', 'pausada', 'cerrada') then
        return jsonb_build_object('ok', true, 'replayed', true, 'status', v_product.status);
    end if;
    if v_product.status is distinct from 'validada' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_product_not_validated',
            'status', v_product.status);
    end if;
    -- La huella se recalcula: lo que se publica es exactamente lo validado.
    if v_product.fingerprint is distinct from public._agent_product_fingerprint(v_product.id, v_product.revision,
            v_product.product_version, v_product.minimum, v_product.free_text, v_product.purpose) then
        raise exception using errcode = '22023', message = 'agent_product_fingerprint_mismatch';
    end if;
    select * into v_principal from public.commercial_ally_runtime_bindings b
     where b.tenant_ref = v_instance.o_tenant_ref and b.ally_ref = v_instance.o_ally_ref
       and b.funnel_ref = p_principal_ref and b.status = 'active';
    if not found or exists (select 1 from public.agent_products p
                             where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
                               and p.product_key = p_principal_ref) then
        raise exception using errcode = '22023', message = 'agent_product_principal_not_bound';
    end if;
    v_conflict := public._agent_product_seed_conflict(v_product, v_principal);
    if v_conflict is not null then
        perform public._agent_product_event(v_product, 'agent_product_publish_refused', 'slack', v_actor,
            v_conflict, jsonb_build_object('product_version', v_product.product_version));
        return jsonb_build_object('ok', false, 'reason_code', v_conflict);
    end if;
    v_now := clock_timestamp();
    -- Una version nueva reemplaza a la publicada (o pausada) de la misma clave.
    for v_other in
        select * from public.agent_products p
         where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
           and p.product_key = v_product.product_key and p.id <> v_product.id
           and p.status in ('publicada', 'pausada')
         for update
    loop
        update public.agent_products
           set status = 'cerrada', closed_at = v_now, closed_by_kind = 'sistema', closed_by_ref = 'nueva_version',
               close_reason = 'Reemplazada por la version ' || v_product.product_version
         where id = v_other.id;
        perform public._agent_product_event(v_other, 'agent_product_closed', 'sistema', 'nueva_version',
            'agent_product_superseded', jsonb_build_object('by_product_id', v_product.id,
                'by_product_version', v_product.product_version));
    end loop;
    v_seed := public._agent_product_seed(v_product, v_principal, v_actor);
    update public.agent_products
       set status = 'publicada', approved_at = v_now, approved_by_kind = 'slack', approved_by_ref = v_actor
     where id = v_product.id;
    perform public._agent_product_event(v_product, 'agent_product_published', 'slack', v_actor,
        'agent_product_published', jsonb_build_object('fingerprint', v_product.fingerprint,
            'product_version', v_product.product_version, 'seed', v_seed));
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'publicada', 'seed', v_seed);
end;
$function$;

-- Pausar: primero los flujos y las campanas del producto, despues el producto.
-- La siembra queda: el binding sigue activo para que los flujos se puedan
-- volver a prender. El ruteo vivo (MP-09) sirve solo los publicados.
create function public.approver_pause_agent_product_v1(
    p_instance jsonb, p_session_hash text, p_product_id uuid, p_reason text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_actor text;
    v_product public.agent_products%rowtype;
    v_dependents jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_products:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_product from public.agent_products p
     where p.id = p_product_id and p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_product_not_found';
    end if;
    if v_product.status = 'pausada' then
        return jsonb_build_object('ok', true, 'replayed', true, 'status', 'pausada');
    end if;
    if v_product.status is distinct from 'publicada' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_product_not_live', 'status', v_product.status);
    end if;
    v_dependents := public._agent_product_pause_dependents(v_product, v_actor, p_reason, 'agent_product_paused');
    update public.agent_products
       set status = 'pausada', paused_at = clock_timestamp(), paused_by_ref = v_actor, pause_reason = p_reason
     where id = v_product.id;
    perform public._agent_product_event(v_product, 'agent_product_paused', 'slack', v_actor,
        'agent_product_paused', v_dependents || jsonb_build_object(
            'reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'pausada') || v_dependents;
end;
$function$;

-- Reanudar: vuelve el producto, con la misma aprobacion. Sus flujos y campanas
-- quedan pausados: se vuelven a prender de a uno, cada uno por su camino.
create function public.approver_resume_agent_product_v1(p_instance jsonb, p_session_hash text, p_product_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_actor text;
    v_product public.agent_products%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_products:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_product from public.agent_products p
     where p.id = p_product_id and p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_product_not_found';
    end if;
    if v_product.status = 'publicada' then
        return jsonb_build_object('ok', true, 'replayed', true, 'status', 'publicada');
    end if;
    if v_product.status is distinct from 'pausada' then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_product_not_paused', 'status', v_product.status);
    end if;
    update public.agent_products
       set status = 'publicada', paused_at = null, paused_by_ref = null, pause_reason = null
     where id = v_product.id;
    perform public._agent_product_event(v_product, 'agent_product_resumed', 'slack', v_actor,
        'agent_product_resumed', '{}'::jsonb);
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'publicada');
end;
$function$;

-- Cerrar: como pausar, pero final. La siembra queda (los casos en vuelo del
-- producto siguen resolviendo su binding vigente); el ruteo vivo ya no lo
-- sirve. Sus campanas quedan pausadas (reanudarlas contesta
-- agent_campaign_product_not_live).
create function public.approver_close_agent_product_v1(
    p_instance jsonb, p_session_hash text, p_product_id uuid, p_reason text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_actor text;
    v_product public.agent_products%rowtype;
    v_dependents jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'agent_products:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select * into v_product from public.agent_products p
     where p.id = p_product_id and p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_product_not_found';
    end if;
    if v_product.status = 'cerrada' then
        return jsonb_build_object('ok', true, 'replayed', true, 'status', 'cerrada');
    end if;
    if v_product.status not in ('publicada', 'pausada') then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_product_not_live', 'status', v_product.status);
    end if;
    v_dependents := public._agent_product_pause_dependents(v_product, v_actor, p_reason, 'agent_product_closed');
    update public.agent_products
       set status = 'cerrada', closed_at = clock_timestamp(), closed_by_kind = 'slack', closed_by_ref = v_actor,
           close_reason = p_reason
     where id = v_product.id;
    perform public._agent_product_event(v_product, 'agent_product_closed', 'slack', v_actor,
        'agent_product_closed', v_dependents || jsonb_build_object('from_status', v_product.status,
            'reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'cerrada') || v_dependents;
end;
$function$;

-- Descartar lo que nunca se publico, con un motivo de una linea. Lo comparten
-- la pagina y el agente.
create function public._agent_product_discard(
    p_tenant_ref text, p_ally_ref text, p_product_id uuid, p_actor_kind text, p_actor_ref text, p_reason text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_product public.agent_products%rowtype;
begin
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    perform set_config('lock_timeout', '5s', true);
    perform pg_advisory_xact_lock(hashtextextended('agent_products:' || p_tenant_ref || ':' || p_ally_ref, 0));
    select * into v_product from public.agent_products p
     where p.id = p_product_id and p.tenant_ref = p_tenant_ref and p.ally_ref = p_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'agent_product_not_found';
    end if;
    if public._agent_product_expire_if_due(v_product) then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_product_final', 'status', 'vencida');
    end if;
    if v_product.status = 'descartada' then
        return jsonb_build_object('ok', true, 'replayed', true);
    end if;
    if v_product.status not in ('borrador', 'validada') then
        return jsonb_build_object('ok', false, 'reason_code', 'agent_product_final', 'status', v_product.status);
    end if;
    update public.agent_products
       set status = 'descartada', closed_at = clock_timestamp(), closed_by_kind = p_actor_kind,
           closed_by_ref = p_actor_ref, close_reason = p_reason
     where id = v_product.id;
    perform public._agent_product_event(v_product, 'agent_product_discarded', p_actor_kind, p_actor_ref,
        'agent_product_discarded', jsonb_build_object('from_status', v_product.status,
            'close_reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'replayed', false);
end;
$function$;

create function public.approver_discard_agent_product_v1(
    p_instance jsonb, p_session_hash text, p_product_id uuid, p_reason text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_actor text;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    return public._agent_product_discard(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_product_id,
        'slack', v_actor, p_reason);
end;
$function$;

-- ===========================================================================
-- 5. La del Automation Expert: descartar sus borradores y validadas.
-- ===========================================================================

create function public.discard_agent_product_v1(
    p_instance jsonb, p_product_id uuid, p_reason text, p_requested_by_ref text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not public._flow_store_actor_is_valid('automation_expert', p_requested_by_ref) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    return public._agent_product_discard(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_product_id,
        'automation_expert', p_requested_by_ref, p_reason);
end;
$function$;

-- ===========================================================================
-- 6. La de service_role: los productos vivos.
-- ===========================================================================

-- Lo que lee el bridge para rutear (MP-09): los publicados y los pausados (el
-- bridge sirve solo los publicados), cada uno con su binding activo. Sin IDs
-- de Slack.
create function public.get_live_agent_products_v1(p_instance jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_rows jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    select coalesce(jsonb_agg(jsonb_build_object(
               'product_id', p.id,
               'status', p.status,
               'product_key', p.product_key,
               'product_version', p.product_version,
               'minimo', p.minimum,
               'texto_libre', p.free_text,
               'fingerprint', p.fingerprint,
               'approved_at', p.approved_at,
               'paused_at', p.paused_at,
               'funnel_ref', b.funnel_ref,
               'binding_version', b.binding_version) order by p.product_key, p.id), '[]'::jsonb)
      into v_rows
      from public.agent_products p
      left join public.commercial_ally_runtime_bindings b
        on b.tenant_ref = p.tenant_ref and b.funnel_ref = p.product_key and b.status = 'active'
       and b.ally_ref = p.ally_ref
     where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
       and p.status in ('publicada', 'pausada');
    return jsonb_build_object('products', v_rows, 'database_at', clock_timestamp());
end;
$function$;

-- ===========================================================================
-- 7. Los permisos. Las internas, para nadie. Cada publica, solo para su rol.
--    Las dos de campanas que se reescriben conservan sus permisos (create or
--    replace no los toca).
-- ===========================================================================

do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public._agent_campaign_live_scope(text,text,jsonb)',
        'public._agent_product_default_offer(jsonb)',
        'public._agent_product_additional_codes(jsonb)',
        'public._agent_product_seed_conflict(public.agent_products,public.commercial_ally_runtime_bindings)',
        'public._agent_product_seed(public.agent_products,public.commercial_ally_runtime_bindings,text)',
        'public._agent_product_pause_dependents(public.agent_products,text,text,text)',
        'public._agent_product_discard(text,text,uuid,text,text,text)',
        'public.approver_get_agent_product_v1(jsonb,text,uuid)',
        'public.approver_publish_agent_product_v1(jsonb,text,uuid,text,text)',
        'public.approver_pause_agent_product_v1(jsonb,text,uuid,text)',
        'public.approver_resume_agent_product_v1(jsonb,text,uuid)',
        'public.approver_close_agent_product_v1(jsonb,text,uuid,text)',
        'public.approver_discard_agent_product_v1(jsonb,text,uuid,text)',
        'public.discard_agent_product_v1(jsonb,uuid,text,text)',
        'public.get_live_agent_products_v1(jsonb)'
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
    -- Los roles los crean 20261021000100 y 20261028000100; una base sin ellos no los nombra.
    if to_regrole('automation_expert_change') is not null then
        grant execute on function public.discard_agent_product_v1(jsonb, uuid, text, text) to automation_expert_change;
    end if;
    if to_regrole('flow_change_approver') is not null then
        foreach v_function in array array[
            'public.approver_get_agent_product_v1(jsonb,text,uuid)',
            'public.approver_publish_agent_product_v1(jsonb,text,uuid,text,text)',
            'public.approver_pause_agent_product_v1(jsonb,text,uuid,text)',
            'public.approver_resume_agent_product_v1(jsonb,text,uuid)',
            'public.approver_close_agent_product_v1(jsonb,text,uuid,text)',
            'public.approver_discard_agent_product_v1(jsonb,text,uuid,text)'
        ]::regprocedure[] loop
            execute format('grant execute on function %s to flow_change_approver', v_function);
        end loop;
    end if;
    if to_regrole('service_role') is not null then
        grant execute on function public.get_live_agent_products_v1(jsonb) to service_role;
    end if;
end;
$acl$;

commit;
