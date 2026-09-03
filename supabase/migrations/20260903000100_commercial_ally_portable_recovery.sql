-- Binding-fenced portable cart-abandonment admission and correlation only.
-- No customer state is seeded and no delivery effect is created.

begin;

create table public.commercial_ally_hotmart_event_bindings (
    webhook_event_id uuid primary key
        references public.webhook_events(id) on delete restrict,
    scope_id uuid not null
        constraint commercial_ally_hotmart_event_bindings_scope_fk
        references public.hotmart_purchase_intent_scopes(id) on delete restrict,
    tenant_ref text not null,
    funnel_ref text not null,
    binding_version integer not null,
    hotmart_product_id text not null,
    purchase_intent_product_ref text not null,
    offer_ref text not null,
    created_at timestamptz not null default clock_timestamp(),
    foreign key (tenant_ref, funnel_ref, binding_version)
        references public.commercial_ally_runtime_bindings(
            tenant_ref, funnel_ref, binding_version
        ) on delete restrict,
    check (nullif(btrim(hotmart_product_id), '') is not null),
    check (nullif(btrim(purchase_intent_product_ref), '') is not null),
    check (nullif(btrim(offer_ref), '') is not null)
);

revoke all on table public.commercial_ally_hotmart_event_bindings from public;

create function public.protect_commercial_ally_hotmart_event_binding()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    raise exception using errcode = '23514',
        message = 'commercial_ally_hotmart_event_binding_immutable';
end;
$function$;

revoke all on function public.protect_commercial_ally_hotmart_event_binding()
    from public;

create trigger commercial_ally_hotmart_event_bindings_append_only
before update or delete on public.commercial_ally_hotmart_event_bindings
for each row execute function public.protect_commercial_ally_hotmart_event_binding();

create function public.admit_portable_hotmart_cart_abandonment(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
    p_external_event_id text,
    p_payload jsonb,
    p_normalized_email text,
    p_normalized_phone text
)
returns table (
    outcome text,
    webhook_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_scope public.hotmart_purchase_intent_scopes%rowtype;
    v_provenance public.commercial_ally_hotmart_event_bindings%rowtype;
    v_admission_outcome text;
    v_event_id uuid;
    v_correlation_outcome text;
begin
    if p_tenant_ref is null or nullif(btrim(p_tenant_ref), '') is null
       or p_funnel_ref is null or nullif(btrim(p_funnel_ref), '') is null
       or p_binding_version is null or p_binding_version < 1
       or p_external_event_id is null or nullif(btrim(p_external_event_id), '') is null
       or p_payload is null or jsonb_typeof(p_payload) <> 'object' then
        raise exception using errcode = '22023',
            message = 'invalid_portable_hotmart_cart_input';
    end if;

    select b.* into v_binding
    from public.commercial_ally_runtime_bindings b
    where b.tenant_ref = p_tenant_ref
      and b.funnel_ref = p_funnel_ref
      and b.binding_version = p_binding_version
      and b.status = 'active'
    for update;
    if not found then
        raise exception using errcode = '22023',
            message = 'commercial_ally_binding_unavailable';
    end if;

    if p_payload #>> '{id}' is distinct from p_external_event_id
       or p_payload #>> '{event}' is distinct from 'PURCHASE_OUT_OF_SHOPPING_CART'
       or p_payload #>> '{version}' is distinct from '2.0.0'
       or jsonb_typeof(p_payload #> '{data,product,id}') is distinct from 'number'
       or (p_payload #>> '{data,product,id}')::numeric
            is distinct from v_binding.hotmart_product_id::numeric
       or p_payload #>> '{data,offer,code}'
            is distinct from v_binding.offer_code then
        raise exception using errcode = '22023',
            message = 'portable_hotmart_cart_binding_mismatch';
    end if;

    select scope.* into v_scope
    from public.hotmart_purchase_intent_scopes scope
    where scope.tenant_ref = v_binding.tenant_ref
      and scope.funnel_ref = v_binding.funnel_ref
      and scope.hotmart_product_id = v_binding.hotmart_product_id::text
      and scope.purchase_intent_product_ref = v_binding.product_hotlink
      and scope.offer_ref = v_binding.offer_code
      and scope.active
    for update;
    if not found then
        raise exception using errcode = '22023',
            message = 'portable_hotmart_cart_scope_unavailable';
    end if;

    select admission.outcome, admission.webhook_event_id
    into strict v_admission_outcome, v_event_id
    from public._admit_hotmart_cart_abandonment_base(
        p_external_event_id,
        p_payload
    ) admission;

    if v_admission_outcome = 'inserted' then
        insert into public.commercial_ally_hotmart_event_bindings (
            webhook_event_id,
            scope_id,
            tenant_ref,
            funnel_ref,
            binding_version,
            hotmart_product_id,
            purchase_intent_product_ref,
            offer_ref
        ) values (
            v_event_id,
            v_scope.id,
            v_binding.tenant_ref,
            v_binding.funnel_ref,
            v_binding.binding_version,
            v_scope.hotmart_product_id,
            v_scope.purchase_intent_product_ref,
            v_scope.offer_ref
        );
    end if;

    select provenance.* into v_provenance
    from public.commercial_ally_hotmart_event_bindings provenance
    where provenance.webhook_event_id = v_event_id;
    if not found
       or v_provenance.scope_id is distinct from v_scope.id
       or v_provenance.tenant_ref is distinct from v_binding.tenant_ref
       or v_provenance.funnel_ref is distinct from v_binding.funnel_ref
       or v_provenance.binding_version is distinct from v_binding.binding_version
       or v_provenance.hotmart_product_id is distinct from v_scope.hotmart_product_id
       or v_provenance.purchase_intent_product_ref
            is distinct from v_scope.purchase_intent_product_ref
       or v_provenance.offer_ref is distinct from v_scope.offer_ref then
        raise exception using errcode = '22023',
            message = 'portable_hotmart_cart_replay_binding_mismatch';
    end if;

    if v_admission_outcome <> 'semantic_conflict' then
        perform public._admit_hotmart_purchase_intent_identity(
            v_event_id,
            p_normalized_email,
            p_normalized_phone
        );
        select correlation.outcome into strict v_correlation_outcome
        from public.correlate_hotmart_purchase_intent(v_event_id) correlation;
    end if;

    return query select v_admission_outcome, v_event_id;
end;
$function$;

create function public.admit_johanna_hotmart_cart_abandonment(
    p_external_event_id text,
    p_payload jsonb,
    p_normalized_email text,
    p_normalized_phone text
)
returns table (
    outcome text,
    webhook_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if jsonb_typeof(p_payload) is distinct from 'object'
       or p_payload ->> 'event' is distinct from 'PURCHASE_OUT_OF_SHOPPING_CART'
       or p_payload #>> '{data,product,id}' is distinct from '8104005'
       or p_payload #>> '{data,offer,code}' is distinct from 'bxjge6zq' then
        raise exception using errcode = '22023',
            message = 'johanna_hotmart_cart_scope_mismatch';
    end if;

    return query
    select admitted.outcome, admitted.webhook_event_id
    from public.admit_and_correlate_hotmart_cart_abandonment(
        p_external_event_id,
        p_payload,
        p_normalized_email,
        p_normalized_phone
    ) admitted;
end;
$function$;

revoke all on function public.admit_portable_hotmart_cart_abandonment(
    text, text, integer, text, jsonb, text, text
) from public;
revoke all on function public.admit_johanna_hotmart_cart_abandonment(
    text, jsonb, text, text
) from public;
revoke all on function public.admit_and_correlate_hotmart_cart_abandonment(
    text, jsonb, text, text
) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on table public.commercial_ally_hotmart_event_bindings from anon;
        revoke all on function public.protect_commercial_ally_hotmart_event_binding()
            from anon;
        revoke all on function public.admit_portable_hotmart_cart_abandonment(
            text, text, integer, text, jsonb, text, text
        ) from anon;
        revoke all on function public.admit_johanna_hotmart_cart_abandonment(
            text, jsonb, text, text
        ) from anon;
        revoke all on function public.admit_and_correlate_hotmart_cart_abandonment(
            text, jsonb, text, text
        ) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on table public.commercial_ally_hotmart_event_bindings
            from authenticated;
        revoke all on function public.protect_commercial_ally_hotmart_event_binding()
            from authenticated;
        revoke all on function public.admit_portable_hotmart_cart_abandonment(
            text, text, integer, text, jsonb, text, text
        ) from authenticated;
        revoke all on function public.admit_johanna_hotmart_cart_abandonment(
            text, jsonb, text, text
        ) from authenticated;
        revoke all on function public.admit_and_correlate_hotmart_cart_abandonment(
            text, jsonb, text, text
        ) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on table public.commercial_ally_hotmart_event_bindings
            from service_role;
        revoke all on function public.protect_commercial_ally_hotmart_event_binding()
            from service_role;
        revoke all on function public.admit_and_correlate_hotmart_cart_abandonment(
            text, jsonb, text, text
        ) from service_role;
        grant execute on function public.admit_portable_hotmart_cart_abandonment(
            text, text, integer, text, jsonb, text, text
        ) to service_role;
        grant execute on function public.admit_johanna_hotmart_cart_abandonment(
            text, jsonb, text, text
        ) to service_role;
    end if;
end;
$roles$;

commit;
