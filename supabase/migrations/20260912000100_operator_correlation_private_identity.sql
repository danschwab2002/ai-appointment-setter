-- Exact-case private review projection. List responses remain masked; only the
-- service-role exact lookup receives complete normalized identity for an
-- authenticated, tenant/funnel-bound Slack modal.

begin;

create or replace function public.get_operator_unresolved_correlation(
    p_tenant_ref text,
    p_funnel_ref text,
    p_webhook_event_id uuid
)
returns table (case_data jsonb)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_case jsonb;
begin
    select unresolved.case_data
      into v_case
      from public.list_operator_unresolved_correlations(
          p_tenant_ref,
          p_funnel_ref,
          1,
          p_webhook_event_id
      ) unresolved;

    if v_case is null then
        return;
    end if;

    select jsonb_set(
        jsonb_set(
            v_case,
            '{identity}',
            jsonb_build_object(
                'normalized_email', identity.normalized_email,
                'normalized_phone', identity.normalized_phone
            ),
            true
        ),
        '{candidates}',
        coalesce((
            select jsonb_agg(
                jsonb_build_object(
                    'purchase_intent_id', candidate.purchase_intent_id,
                    'email_match', candidate.email_match,
                    'phone_match', candidate.phone_match,
                    'submitted_at', intent.submitted_at,
                    'lifecycle_state', intent.lifecycle_state,
                    'normalized_email', intent.normalized_email,
                    'normalized_phone', intent.normalized_phone
                )
                order by candidate.purchase_intent_id
            )
            from public.hotmart_purchase_intent_correlation_candidates candidate
            join public.hotmart_purchase_intent_correlations correlation
              on correlation.webhook_event_id = candidate.webhook_event_id
            join public.hotmart_purchase_intent_scopes scope
              on scope.id = correlation.scope_id
             and scope.tenant_ref = p_tenant_ref
             and scope.funnel_ref = p_funnel_ref
            join public.purchase_intents intent
              on intent.id = candidate.purchase_intent_id
             and intent.tenant_ref = scope.tenant_ref
             and intent.funnel_ref = scope.funnel_ref
             and lower(intent.product_ref) = lower(scope.purchase_intent_product_ref)
             and intent.offer_ref = scope.offer_ref
            where candidate.webhook_event_id = p_webhook_event_id
        ), '[]'::jsonb),
        true
    )
      into v_case
      from public.hotmart_purchase_intent_event_identities identity
     where identity.webhook_event_id = p_webhook_event_id;

    if v_case is null then
        raise exception using
            errcode = '55000',
            message = 'operator_correlation_private_identity_missing';
    end if;

    return query select v_case;
end;
$function$;

revoke execute on function public.get_operator_unresolved_correlation(text, text, uuid)
from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke execute on function public.get_operator_unresolved_correlation(text, text, uuid)
        from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke execute on function public.get_operator_unresolved_correlation(text, text, uuid)
        from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke execute on function public.get_operator_unresolved_correlation(text, text, uuid)
        from service_role;
        grant execute on function public.get_operator_unresolved_correlation(text, text, uuid)
        to service_role;
    end if;
end;
$roles$;

commit;
