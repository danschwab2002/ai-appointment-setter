-- Versioned discount policy boundary for portable commercial ally runtimes.
-- This migration intentionally seeds no policy and grants runtime no table DML.

begin;

create table public.commercial_ally_discount_policy_versions (
    tenant_ref text not null,
    funnel_ref text not null,
    binding_version integer not null check (binding_version > 0),
    policy_key text not null check (
        policy_key ~ '^[a-z0-9][a-z0-9-]{0,127}$'
    ),
    policy_version integer not null check (policy_version > 0),
    trigger_kind text not null check (
        trigger_kind in (
            'precheckout_without_purchase_signal',
            'confirmed_cart_abandonment',
            'payment_failure'
        )
    ),
    status text not null default 'draft' check (
        status in ('draft', 'approved', 'published', 'retired')
    ),
    discount_kind text not null check (
        discount_kind in ('percentage', 'fixed_amount')
    ),
    discount_value numeric(14, 2) not null check (discount_value > 0),
    currency text,
    coupon_reference text not null check (btrim(coupon_reference) <> ''),
    offer_valid_for interval not null check (offer_valid_for > interval '0'),
    presentation_stage text not null check (
        presentation_stage in ('first_touch', 'later_step')
    ),
    template_key text not null check (btrim(template_key) <> ''),
    copy_version text not null check (btrim(copy_version) <> ''),
    valid_from timestamptz not null default statement_timestamp(),
    valid_until timestamptz,
    approved_by text,
    approved_at timestamptz,
    published_at timestamptz,
    created_at timestamptz not null default statement_timestamp(),
    updated_at timestamptz not null default statement_timestamp(),
    primary key (
        tenant_ref,
        funnel_ref,
        binding_version,
        policy_key,
        policy_version
    ),
    foreign key (tenant_ref, funnel_ref, binding_version)
        references public.commercial_ally_runtime_bindings (
            tenant_ref,
            funnel_ref,
            binding_version
        )
        on update restrict
        on delete restrict,
    check (valid_until is null or valid_until > valid_from),
    check (
        (discount_kind = 'percentage'
            and discount_value <= 100
            and currency is null)
        or
        (discount_kind = 'fixed_amount'
            and currency is not null
            and currency ~ '^[A-Z]{3}$')
    ),
    check (
        (status = 'draft'
            and approved_by is null
            and approved_at is null
            and published_at is null)
        or
        (status = 'approved'
            and nullif(btrim(approved_by), '') is not null
            and approved_at is not null
            and published_at is null)
        or
        (status = 'published'
            and nullif(btrim(approved_by), '') is not null
            and approved_at is not null
            and published_at is not null
            and published_at >= approved_at)
        or
        (status = 'retired'
            and nullif(btrim(approved_by), '') is not null
            and approved_at is not null)
    )
);

create unique index commercial_ally_discount_policy_one_published
    on public.commercial_ally_discount_policy_versions (
        tenant_ref,
        funnel_ref,
        binding_version,
        trigger_kind
    )
    where status = 'published';

create function public.guard_commercial_ally_discount_policy_version()
returns trigger
language plpgsql
set search_path = ''
as $function$
begin
    if tg_op = 'INSERT' then
        if new.status <> 'draft' then
            raise exception using
                errcode = '55000',
                message = 'commercial_ally_discount_policy_status_transition_invalid';
        end if;
        new.created_at := statement_timestamp();
        new.updated_at := statement_timestamp();
        return new;
    end if;

    if tg_op = 'DELETE' then
        if old.status <> 'draft' then
            raise exception using
                errcode = '55000',
                message = 'commercial_ally_discount_policy_content_immutable';
        end if;
        return old;
    end if;

    if new.created_at is distinct from old.created_at then
        raise exception using
            errcode = '55000',
            message = 'commercial_ally_discount_policy_approval_metadata_immutable';
    end if;

    if row(
        old.tenant_ref,
        old.funnel_ref,
        old.binding_version,
        old.policy_key,
        old.policy_version
    ) is distinct from row(
        new.tenant_ref,
        new.funnel_ref,
        new.binding_version,
        new.policy_key,
        new.policy_version
    ) then
        raise exception using
            errcode = '55000',
            message = 'commercial_ally_discount_policy_content_immutable';
    end if;

    if old.status <> 'draft' and row(
        old.trigger_kind,
        old.discount_kind,
        old.discount_value,
        old.currency,
        old.coupon_reference,
        old.offer_valid_for,
        old.presentation_stage,
        old.template_key,
        old.copy_version,
        old.valid_from,
        old.valid_until
    ) is distinct from row(
        new.trigger_kind,
        new.discount_kind,
        new.discount_value,
        new.currency,
        new.coupon_reference,
        new.offer_valid_for,
        new.presentation_stage,
        new.template_key,
        new.copy_version,
        new.valid_from,
        new.valid_until
    ) then
        raise exception using
            errcode = '55000',
            message = 'commercial_ally_discount_policy_content_immutable';
    end if;

    if old.status in ('approved', 'published', 'retired') and row(
        old.approved_by,
        old.approved_at
    ) is distinct from row(
        new.approved_by,
        new.approved_at
    ) then
        raise exception using
            errcode = '55000',
            message = 'commercial_ally_discount_policy_approval_metadata_immutable';
    end if;

    if old.published_at is distinct from new.published_at
        and not (old.status = 'approved' and new.status = 'published')
    then
        raise exception using
            errcode = '55000',
            message = 'commercial_ally_discount_policy_approval_metadata_immutable';
    end if;

    if not (
        new.status = old.status
        or (old.status = 'draft' and new.status = 'approved')
        or (old.status = 'approved' and new.status = 'published')
        or (old.status in ('approved', 'published')
            and new.status = 'retired')
    ) then
        raise exception using
            errcode = '55000',
            message = 'commercial_ally_discount_policy_status_transition_invalid';
    end if;

    if old.status = 'draft' and new.status = 'approved' then
        new.approved_at := statement_timestamp();
        new.published_at := null;
    elsif old.status = 'approved' and new.status = 'published' then
        new.published_at := statement_timestamp();
    end if;

    new.updated_at := statement_timestamp();
    return new;
end;
$function$;

create trigger commercial_ally_discount_policy_guard
before insert or update or delete
on public.commercial_ally_discount_policy_versions
for each row execute function public.guard_commercial_ally_discount_policy_version();

alter table public.commercial_ally_discount_policy_versions enable row level security;

revoke all on table public.commercial_ally_discount_policy_versions from public;
revoke all on function public.guard_commercial_ally_discount_policy_version()
    from public;

do $roles$
begin
    if to_regrole('anon') is not null then
        execute 'revoke all on table public.commercial_ally_discount_policy_versions from anon';
        execute 'revoke all on function public.guard_commercial_ally_discount_policy_version() from anon';
    end if;
    if to_regrole('authenticated') is not null then
        execute 'revoke all on table public.commercial_ally_discount_policy_versions from authenticated';
        execute 'revoke all on function public.guard_commercial_ally_discount_policy_version() from authenticated';
    end if;
    if to_regrole('service_role') is not null then
        execute 'revoke all on table public.commercial_ally_discount_policy_versions from service_role';
        execute 'revoke all on function public.guard_commercial_ally_discount_policy_version() from service_role';
    end if;
end
$roles$;

create function public.resolve_commercial_ally_discount_policy(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
    p_trigger_kind text
)
returns table (
    policy_key text,
    policy_version integer,
    trigger_kind text,
    discount_kind text,
    discount_value numeric,
    currency text,
    coupon_reference text,
    offer_valid_for interval,
    presentation_stage text,
    template_key text,
    copy_version text,
    valid_from timestamptz,
    valid_until timestamptz
)
language sql
stable
security definer
set search_path = ''
as $function$
    select
        p.policy_key,
        p.policy_version,
        p.trigger_kind,
        p.discount_kind,
        p.discount_value,
        p.currency,
        p.coupon_reference,
        p.offer_valid_for,
        p.presentation_stage,
        p.template_key,
        p.copy_version,
        p.valid_from,
        p.valid_until
    from public.commercial_ally_runtime_bindings b
    join public.commercial_ally_discount_policy_versions p
      on p.tenant_ref = b.tenant_ref
     and p.funnel_ref = b.funnel_ref
     and p.binding_version = b.binding_version
    where b.tenant_ref = p_tenant_ref
      and b.funnel_ref = p_funnel_ref
      and b.binding_version = p_binding_version
      and b.status = 'active'
      and p.trigger_kind = p_trigger_kind
      and p.status = 'published'
      and p.valid_from <= statement_timestamp()
      and (p.valid_until is null or p.valid_until > statement_timestamp())
$function$;

revoke all on function public.resolve_commercial_ally_discount_policy(
    text, text, integer, text
) from public;

do $roles$
begin
    if to_regrole('anon') is not null then
        execute 'revoke all on function public.resolve_commercial_ally_discount_policy(text,text,integer,text) from anon';
    end if;
    if to_regrole('authenticated') is not null then
        execute 'revoke all on function public.resolve_commercial_ally_discount_policy(text,text,integer,text) from authenticated';
    end if;
    if to_regrole('service_role') is not null then
        execute 'grant execute on function public.resolve_commercial_ally_discount_policy(text,text,integer,text) to service_role';
    end if;
end
$roles$;

commit;
