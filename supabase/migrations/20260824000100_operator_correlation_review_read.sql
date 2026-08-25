-- Read-only operator projection for deterministic correlation cases that require
-- human review. The projection masks identity inside PostgreSQL and creates no
-- workflow transition, notification, match, candidate selection, or effect.

begin;

create or replace function public.list_operator_unresolved_correlations(
    p_tenant_ref text,
    p_funnel_ref text,
    p_limit integer default 20,
    p_webhook_event_id uuid default null
)
returns table (case_data jsonb)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_tenant_ref is null or nullif(btrim(p_tenant_ref), '') is null
       or p_funnel_ref is null or nullif(btrim(p_funnel_ref), '') is null then
        raise exception using
            errcode = '22023',
            message = 'invalid_operator_correlation_scope';
    end if;
    if p_limit is null or p_limit < 1 or p_limit > 50 then
        raise exception using
            errcode = '22023',
            message = 'invalid_operator_correlation_limit';
    end if;

    return query
    select jsonb_build_object(
        'webhook_event_id', correlation.webhook_event_id,
        'scope_id', correlation.scope_id,
        'event_type', correlation.event_type,
        'outcome', correlation.outcome,
        'candidate_count', correlation.candidate_count,
        'reason_code', correlation.reason_code,
        'manual_handoff_required', correlation.manual_handoff_required,
        'observed_at', correlation.observed_at,
        'scope', case
            when scope.id is null then null
            else jsonb_build_object(
                'tenant_ref', scope.tenant_ref,
                'funnel_ref', scope.funnel_ref,
                'product_ref', scope.purchase_intent_product_ref,
                'offer_ref', scope.offer_ref
            )
        end,
        'identity', jsonb_build_object(
            'email_present', identity.normalized_email is not null,
            'phone_present', identity.normalized_phone is not null,
            'masked_email', case
                when identity.normalized_email is null then null
                when length(split_part(identity.normalized_email, '@', 1)) <= 2
                    then '***@'
                        || split_part(identity.normalized_email, '@', 2)
                else left(split_part(identity.normalized_email, '@', 1), 1)
                    || '***'
                    || right(split_part(identity.normalized_email, '@', 1), 1)
                    || '@'
                    || split_part(identity.normalized_email, '@', 2)
            end,
            'masked_phone', case
                when identity.normalized_phone is null then null
                else repeat('*', greatest(length(identity.normalized_phone) - 4, 0))
                    || right(identity.normalized_phone, 4)
            end
        ),
        'candidates', coalesce((
            select jsonb_agg(
                jsonb_build_object(
                    'purchase_intent_id', candidate.purchase_intent_id,
                    'email_match', candidate.email_match,
                    'phone_match', candidate.phone_match,
                    'submitted_at', intent.submitted_at,
                    'lifecycle_state', intent.lifecycle_state,
                    'masked_email', case
                        when intent.normalized_email is null then null
                        when length(split_part(intent.normalized_email, '@', 1)) <= 2
                            then '***@'
                                || split_part(intent.normalized_email, '@', 2)
                        else left(split_part(intent.normalized_email, '@', 1), 1)
                            || '***'
                            || right(split_part(intent.normalized_email, '@', 1), 1)
                            || '@'
                            || split_part(intent.normalized_email, '@', 2)
                    end,
                    'masked_phone', repeat(
                        '*', greatest(length(intent.normalized_phone) - 4, 0)
                    ) || right(intent.normalized_phone, 4)
                )
                order by candidate.purchase_intent_id
            )
            from public.hotmart_purchase_intent_correlation_candidates candidate
            join public.purchase_intents intent
              on intent.id = candidate.purchase_intent_id
             and intent.tenant_ref = scope.tenant_ref
             and intent.funnel_ref = scope.funnel_ref
             and intent.product_ref = scope.purchase_intent_product_ref
             and intent.offer_ref = scope.offer_ref
            where candidate.webhook_event_id = correlation.webhook_event_id
        ), '[]'::jsonb)
    )
    from public.hotmart_purchase_intent_correlations correlation
    left join public.hotmart_purchase_intent_scopes scope
      on scope.id = correlation.scope_id
    left join public.hotmart_purchase_intent_event_identities identity
      on identity.webhook_event_id = correlation.webhook_event_id
    where correlation.manual_handoff_required
      and correlation.purchase_intent_id is null
      and correlation.outcome in ('unmatched', 'ambiguous', 'conflict')
      and scope.tenant_ref = p_tenant_ref
      and scope.funnel_ref = p_funnel_ref
      and (
          p_webhook_event_id is null
          or correlation.webhook_event_id = p_webhook_event_id
      )
    order by correlation.observed_at desc, correlation.webhook_event_id asc
    limit p_limit;
end;
$function$;

create or replace function public.get_operator_unresolved_correlation(
    p_tenant_ref text,
    p_funnel_ref text,
    p_webhook_event_id uuid
)
returns table (case_data jsonb)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    select unresolved.case_data
    from public.list_operator_unresolved_correlations(
        p_tenant_ref,
        p_funnel_ref,
        1,
        p_webhook_event_id
    ) unresolved;
$function$;

revoke execute on function public.list_operator_unresolved_correlations(text, text, integer, uuid) from public;
revoke execute on function public.get_operator_unresolved_correlation(text, text, uuid)
from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke execute on function public.list_operator_unresolved_correlations(text, text, integer, uuid) from anon;
        revoke execute on function public.get_operator_unresolved_correlation(text, text, uuid)
        from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke execute on function public.list_operator_unresolved_correlations(text, text, integer, uuid) from authenticated;
        revoke execute on function public.get_operator_unresolved_correlation(text, text, uuid)
        from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke execute on function public.list_operator_unresolved_correlations(text, text, integer, uuid) from service_role;
        revoke execute on function public.get_operator_unresolved_correlation(text, text, uuid)
        from service_role;
        grant execute on function public.list_operator_unresolved_correlations(text, text, integer, uuid) to service_role;
        grant execute on function public.get_operator_unresolved_correlation(text, text, uuid)
        to service_role;
    end if;
end;
$roles$;

commit;
