-- Discount policy V2: explicit indefinite offers and atomic trigger releases.
-- Existing rows are classified conservatively; no policy is seeded or published.

begin;

do $constraints$
declare
    v_constraint record;
begin
    for v_constraint in
        select constraint_row.conname
        from pg_catalog.pg_constraint constraint_row
        where constraint_row.conrelid =
                'public.commercial_ally_discount_policy_versions'::regclass
          and constraint_row.contype = 'c'
          and pg_catalog.pg_get_constraintdef(constraint_row.oid)
                ilike '%offer_valid_for%'
    loop
        execute format(
            'alter table public.commercial_ally_discount_policy_versions drop constraint %I',
            v_constraint.conname
        );
    end loop;
end
$constraints$;

alter table public.commercial_ally_discount_policy_versions
    alter column offer_valid_for drop not null,
    add column offer_expiration_mode text not null default 'finite',
    add column requires_inbound_reply_after_initial_template boolean
        not null default false,
    add column coupon_delivery_mode text not null default 'literal',
    add column urgency_copy_allowed boolean not null default true,
    add column channel_provider text not null default 'evolution',
    add column delivery_mode text not null default 'freeform',
    add column template_language text not null default 'es',
    add column template_category text,
    add column coupon_template_component text,
    add column coupon_template_parameter_index integer,
    add column release_requires_exact_trigger_set boolean not null default false,
    add constraint commercial_ally_discount_offer_expiration_mode_valid
        check (offer_expiration_mode in ('finite', 'indefinite')),
    add constraint commercial_ally_discount_offer_duration_valid
        check (
            (offer_expiration_mode = 'finite'
                and offer_valid_for is not null
                and offer_valid_for > interval '0')
            or
            (offer_expiration_mode = 'indefinite'
                and offer_valid_for is null)
        ),
    add constraint commercial_ally_discount_coupon_delivery_mode_valid
        check (coupon_delivery_mode in ('literal', 'meta_template_variable')),
    add constraint commercial_ally_discount_channel_provider_valid
        check (channel_provider in ('evolution', 'waba')),
    add constraint commercial_ally_discount_delivery_mode_valid
        check (delivery_mode in ('freeform', 'approved_template')),
    add constraint commercial_ally_discount_template_language_valid
        check (template_language ~ '^[a-z]{2,3}([_-][A-Za-z]{2})?$'),
    add constraint commercial_ally_discount_template_category_valid
        check (
            template_category is null
            or template_category in ('authentication', 'marketing', 'utility')
        ),
    add constraint commercial_ally_discount_reply_stage_valid
        check (
            not requires_inbound_reply_after_initial_template
            or presentation_stage = 'later_step'
        ),
    add constraint commercial_ally_discount_coupon_transport_valid
        check (
            (coupon_delivery_mode = 'literal'
                and coupon_template_component is null
                and coupon_template_parameter_index is null)
            or
            (coupon_delivery_mode = 'meta_template_variable'
                and channel_provider = 'waba'
                and delivery_mode = 'approved_template'
                and coupon_template_component is not null
                and coupon_template_component in ('body', 'button')
                and coupon_template_parameter_index is not null
                and coupon_template_parameter_index > 0)
        ),
    add constraint commercial_ally_discount_strict_release_semantics
        check (
            not release_requires_exact_trigger_set
            or (
                offer_expiration_mode = 'indefinite'
                and requires_inbound_reply_after_initial_template
                and coupon_delivery_mode = 'meta_template_variable'
                and not urgency_copy_allowed
                and channel_provider = 'waba'
                and delivery_mode = 'approved_template'
                and presentation_stage = 'later_step'
                and template_category is not null
                and template_category = 'marketing'
                and coupon_template_component is not null
                and coupon_template_parameter_index is not null
            )
        );

alter table public.commercial_ally_discount_policy_versions
    drop constraint commercial_ally_discount_policy_versions_pkey,
    add primary key (
        tenant_ref,
        funnel_ref,
        binding_version,
        policy_key,
        policy_version,
        trigger_kind
    );

create function public.guard_commercial_ally_discount_policy_v2_content()
returns trigger
language plpgsql
set search_path = ''
as $function$
begin
    if old.status <> 'draft' and row(
        old.offer_expiration_mode,
        old.requires_inbound_reply_after_initial_template,
        old.coupon_delivery_mode,
        old.urgency_copy_allowed,
        old.channel_provider,
        old.delivery_mode,
        old.template_language,
        old.template_category,
        old.coupon_template_component,
        old.coupon_template_parameter_index,
        old.release_requires_exact_trigger_set
    ) is distinct from row(
        new.offer_expiration_mode,
        new.requires_inbound_reply_after_initial_template,
        new.coupon_delivery_mode,
        new.urgency_copy_allowed,
        new.channel_provider,
        new.delivery_mode,
        new.template_language,
        new.template_category,
        new.coupon_template_component,
        new.coupon_template_parameter_index,
        new.release_requires_exact_trigger_set
    ) then
        raise exception using
            errcode = '55000',
            message = 'commercial_ally_discount_policy_content_immutable';
    end if;
    return new;
end;
$function$;

create trigger commercial_ally_discount_policy_v2_content_guard
before update on public.commercial_ally_discount_policy_versions
for each row execute function
    public.guard_commercial_ally_discount_policy_v2_content();

create function public.assert_commercial_ally_discount_release_complete(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
    p_policy_key text,
    p_policy_version integer
)
returns void
language plpgsql
set search_path = ''
as $function$
declare
    v_reference public.commercial_ally_discount_policy_versions%rowtype;
    v_total integer;
    v_published integer;
    v_triggers text[];
begin
    select release_row.* into v_reference
    from public.commercial_ally_discount_policy_versions release_row
    where release_row.tenant_ref = p_tenant_ref
      and release_row.funnel_ref = p_funnel_ref
      and release_row.binding_version = p_binding_version
      and release_row.policy_key = p_policy_key
      and release_row.policy_version = p_policy_version
      and release_row.status = 'published'
      and release_row.release_requires_exact_trigger_set
    order by release_row.trigger_kind
    limit 1;

    if not found then
        return;
    end if;

    select
        count(*)::integer,
        count(*) filter (where release_row.status = 'published')::integer,
        array_agg(release_row.trigger_kind order by release_row.trigger_kind)
    into v_total, v_published, v_triggers
    from public.commercial_ally_discount_policy_versions release_row
    where release_row.tenant_ref = p_tenant_ref
      and release_row.funnel_ref = p_funnel_ref
      and release_row.binding_version = p_binding_version
      and release_row.policy_key = p_policy_key
      and release_row.policy_version = p_policy_version;

    if v_total <> 3
       or v_published <> 3
       or v_triggers is distinct from array[
            'confirmed_cart_abandonment',
            'payment_failure',
            'precheckout_without_purchase_signal'
       ]::text[]
       or exists (
            select 1
            from public.commercial_ally_discount_policy_versions candidate
            where candidate.tenant_ref = p_tenant_ref
              and candidate.funnel_ref = p_funnel_ref
              and candidate.binding_version = p_binding_version
              and candidate.policy_key = p_policy_key
              and candidate.policy_version = p_policy_version
              and (
                  not candidate.release_requires_exact_trigger_set
                  or row(
                      candidate.discount_kind,
                      candidate.discount_value,
                      candidate.currency,
                      candidate.coupon_reference,
                      candidate.offer_valid_for,
                      candidate.offer_expiration_mode,
                      candidate.presentation_stage,
                      candidate.template_key,
                      candidate.copy_version,
                      candidate.valid_from,
                      candidate.valid_until,
                      candidate.approved_by,
                      candidate.requires_inbound_reply_after_initial_template,
                      candidate.coupon_delivery_mode,
                      candidate.urgency_copy_allowed,
                      candidate.channel_provider,
                      candidate.delivery_mode,
                      candidate.template_language,
                      candidate.template_category,
                      candidate.coupon_template_component,
                      candidate.coupon_template_parameter_index
                  ) is distinct from row(
                      v_reference.discount_kind,
                      v_reference.discount_value,
                      v_reference.currency,
                      v_reference.coupon_reference,
                      v_reference.offer_valid_for,
                      v_reference.offer_expiration_mode,
                      v_reference.presentation_stage,
                      v_reference.template_key,
                      v_reference.copy_version,
                      v_reference.valid_from,
                      v_reference.valid_until,
                      v_reference.approved_by,
                      v_reference.requires_inbound_reply_after_initial_template,
                      v_reference.coupon_delivery_mode,
                      v_reference.urgency_copy_allowed,
                      v_reference.channel_provider,
                      v_reference.delivery_mode,
                      v_reference.template_language,
                      v_reference.template_category,
                      v_reference.coupon_template_component,
                      v_reference.coupon_template_parameter_index
                  )
              )
       ) then
        raise exception using
            errcode = '55000',
            message = 'commercial_ally_discount_release_incomplete';
    end if;
end;
$function$;

create function public.enforce_commercial_ally_discount_release_complete()
returns trigger
language plpgsql
set search_path = ''
as $function$
begin
    perform public.assert_commercial_ally_discount_release_complete(
        coalesce(new.tenant_ref, old.tenant_ref),
        coalesce(new.funnel_ref, old.funnel_ref),
        coalesce(new.binding_version, old.binding_version),
        coalesce(new.policy_key, old.policy_key),
        coalesce(new.policy_version, old.policy_version)
    );
    return coalesce(new, old);
end;
$function$;

create constraint trigger commercial_ally_discount_release_complete
after insert or update or delete
on public.commercial_ally_discount_policy_versions
deferrable initially deferred
for each row execute function
    public.enforce_commercial_ally_discount_release_complete();

revoke all on function public.guard_commercial_ally_discount_policy_v2_content()
    from public;
revoke all on function public.assert_commercial_ally_discount_release_complete(
    text, text, integer, text, integer
) from public;
revoke all on function public.enforce_commercial_ally_discount_release_complete()
    from public;

drop function public.resolve_commercial_ally_discount_policy(
    text, text, integer, text
);

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
    offer_valid_for_seconds bigint,
    offer_expiration_mode text,
    presentation_stage text,
    template_key text,
    copy_version text,
    valid_from timestamptz,
    valid_until timestamptz,
    requires_inbound_reply_after_initial_template boolean,
    coupon_delivery_mode text,
    urgency_copy_allowed boolean,
    channel_provider text,
    delivery_mode text,
    template_language text,
    template_category text,
    coupon_template_component text,
    coupon_template_parameter_index integer,
    release_requires_exact_trigger_set boolean
)
language sql
stable
security definer
set search_path = ''
as $function$
    select
        policy.policy_key,
        policy.policy_version,
        policy.trigger_kind,
        policy.discount_kind,
        policy.discount_value,
        policy.currency,
        policy.coupon_reference,
        case
            when policy.offer_valid_for is null then null
            else extract(epoch from policy.offer_valid_for)::bigint
        end as offer_valid_for_seconds,
        policy.offer_expiration_mode,
        policy.presentation_stage,
        policy.template_key,
        policy.copy_version,
        policy.valid_from,
        policy.valid_until,
        policy.requires_inbound_reply_after_initial_template,
        policy.coupon_delivery_mode,
        policy.urgency_copy_allowed,
        policy.channel_provider,
        policy.delivery_mode,
        policy.template_language,
        policy.template_category,
        policy.coupon_template_component,
        policy.coupon_template_parameter_index,
        policy.release_requires_exact_trigger_set
    from public.commercial_ally_runtime_bindings binding
    join public.commercial_ally_discount_policy_versions policy
      on policy.tenant_ref = binding.tenant_ref
     and policy.funnel_ref = binding.funnel_ref
     and policy.binding_version = binding.binding_version
    where binding.tenant_ref = p_tenant_ref
      and binding.funnel_ref = p_funnel_ref
      and binding.binding_version = p_binding_version
      and binding.status = 'active'
      and policy.trigger_kind = p_trigger_kind
      and policy.status = 'published'
      and policy.valid_from <= statement_timestamp()
      and (policy.valid_until is null
        or policy.valid_until > statement_timestamp())
$function$;

revoke all on function public.resolve_commercial_ally_discount_policy(
    text, text, integer, text
) from public;

do $roles$
begin
    if to_regrole('anon') is not null then
        execute 'revoke all on function public.guard_commercial_ally_discount_policy_v2_content() from anon';
        execute 'revoke all on function public.assert_commercial_ally_discount_release_complete(text,text,integer,text,integer) from anon';
        execute 'revoke all on function public.enforce_commercial_ally_discount_release_complete() from anon';
        execute 'revoke all on function public.resolve_commercial_ally_discount_policy(text,text,integer,text) from anon';
    end if;
    if to_regrole('authenticated') is not null then
        execute 'revoke all on function public.guard_commercial_ally_discount_policy_v2_content() from authenticated';
        execute 'revoke all on function public.assert_commercial_ally_discount_release_complete(text,text,integer,text,integer) from authenticated';
        execute 'revoke all on function public.enforce_commercial_ally_discount_release_complete() from authenticated';
        execute 'revoke all on function public.resolve_commercial_ally_discount_policy(text,text,integer,text) from authenticated';
    end if;
    if to_regrole('service_role') is not null then
        execute 'revoke all on function public.guard_commercial_ally_discount_policy_v2_content() from service_role';
        execute 'revoke all on function public.assert_commercial_ally_discount_release_complete(text,text,integer,text,integer) from service_role';
        execute 'revoke all on function public.enforce_commercial_ally_discount_release_complete() from service_role';
        execute 'grant execute on function public.resolve_commercial_ally_discount_policy(text,text,integer,text) to service_role';
    end if;
end
$roles$;

commit;