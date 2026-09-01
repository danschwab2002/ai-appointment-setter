-- Durable, non-secret binding for one commercial ally deployment.
-- No customer row is seeded here: provisioning must insert and explicitly
-- activate the reviewed binding for the isolated stack.

create table public.commercial_ally_runtime_bindings (
    tenant_ref text not null,
    funnel_ref text not null,
    binding_version integer not null check (binding_version > 0),
    status text not null default 'draft'
        check (status in ('draft', 'validated', 'approved', 'active', 'retired')),
    ally_ref text not null,
    lead_ally_name text not null,
    lead_site text not null,
    lead_landing_id text not null,
    lead_page_host text not null,
    lead_page_path text not null check (lead_page_path like '/%'),
    product_hotlink text not null,
    product_name text not null,
    product_price numeric(14, 2) not null check (product_price > 0),
    currency text not null check (currency ~ '^[A-Z]{3}$'),
    offer_code text not null,
    consent_copy_version text not null,
    hotmart_product_id bigint not null check (hotmart_product_id > 0),
    chatwoot_account_id bigint not null check (chatwoot_account_id > 0),
    chatwoot_inbox_id bigint not null check (chatwoot_inbox_id > 0),
    inbound_scope_key text not null,
    inbound_scope_version integer not null check (inbound_scope_version > 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (tenant_ref, funnel_ref, binding_version)
);

create unique index commercial_ally_runtime_bindings_one_active
    on public.commercial_ally_runtime_bindings (tenant_ref, funnel_ref)
    where status = 'active';

alter table public.commercial_ally_runtime_bindings enable row level security;

revoke all on table public.commercial_ally_runtime_bindings from public;

do $roles$
begin
    if to_regrole('anon') is not null then
        execute 'revoke all on table public.commercial_ally_runtime_bindings from anon';
    end if;
    if to_regrole('authenticated') is not null then
        execute 'revoke all on table public.commercial_ally_runtime_bindings from authenticated';
    end if;
    if to_regrole('service_role') is not null then
        execute 'revoke all on table public.commercial_ally_runtime_bindings from service_role';
        execute 'grant select on table public.commercial_ally_runtime_bindings to service_role';
    end if;
end
$roles$;

create function public.resolve_commercial_ally_runtime_binding(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer
)
returns setof public.commercial_ally_runtime_bindings
language sql
stable
security invoker
set search_path = ''
as $$
    select b.*
    from public.commercial_ally_runtime_bindings as b
    where b.tenant_ref = p_tenant_ref
      and b.funnel_ref = p_funnel_ref
      and b.binding_version = p_binding_version
      and b.status = 'active'
$$;

revoke all on function public.resolve_commercial_ally_runtime_binding(text, text, integer)
    from public;

do $roles$
begin
    if to_regrole('anon') is not null then
        execute 'revoke all on function public.resolve_commercial_ally_runtime_binding(text, text, integer) from anon';
    end if;
    if to_regrole('authenticated') is not null then
        execute 'revoke all on function public.resolve_commercial_ally_runtime_binding(text, text, integer) from authenticated';
    end if;
    if to_regrole('service_role') is not null then
        execute 'grant execute on function public.resolve_commercial_ally_runtime_binding(text, text, integer) to service_role';
    end if;
end
$roles$;
