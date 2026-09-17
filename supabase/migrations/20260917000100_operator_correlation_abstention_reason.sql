-- Persist bounded, non-PII reasons for terminal AI pre-resolution abstentions.

begin;

alter table public.operator_correlation_preresolutions
    add column decision_reason_code text;

update public.operator_correlation_preresolutions
set decision_reason_code = 'legacy_unclassified'
where status = 'abstained';

alter table public.operator_correlation_preresolutions
    add constraint operator_correlation_preresolutions_decision_reason_check
    check (
        (status = 'abstained'
         and decision_reason_code is not null
         and decision_reason_code in (
            'legacy_unclassified',
            'model_abstained',
            'model_abstained_missing_information',
            'proposal_keys_invalid',
            'proposal_shape_invalid',
            'abstention_payload_invalid',
            'confidence_below_high',
            'contradicting_evidence_present',
            'candidate_not_allowed',
            'supporting_evidence_missing',
            'supporting_evidence_unknown',
            'supporting_evidence_candidate_mismatch',
            'independent_discriminating_evidence_missing',
            'independent_discriminating_candidate_not_unique'
        ))
        or (status <> 'abstained' and decision_reason_code is null)
    );

create or replace function public.complete_operator_correlation_preresolution(
    p_webhook_event_id uuid,
    p_claim_token uuid,
    p_lease_generation bigint,
    p_disposition text,
    p_recommended_purchase_intent_id uuid,
    p_supporting_evidence jsonb,
    p_model_name text,
    p_prompt_version text,
    p_decision_reason_code text
)
returns table (
    recommendation_ref uuid,
    status text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_pre public.operator_correlation_preresolutions%rowtype;
    v_now timestamptz := clock_timestamp();
    v_recommendation_ref uuid;
    v_item jsonb;
begin
    select pre.* into v_pre
    from public.operator_correlation_preresolutions pre
    where pre.webhook_event_id = p_webhook_event_id
      and pre.preresolution_version = 1
    for update;

    if not found
       or v_pre.status <> 'leased'
       or v_pre.claim_token is distinct from p_claim_token
       or v_pre.lease_generation is distinct from p_lease_generation
       or v_pre.lease_expires_at <= v_now then
        raise exception using errcode = '55000',
            message = 'operator_correlation_preresolution_lease_lost';
    end if;

    if p_disposition not in ('recommended', 'abstained', 'suppressed_unmatched')
       or jsonb_typeof(coalesce(p_supporting_evidence, 'null'::jsonb)) <> 'array'
       or jsonb_array_length(p_supporting_evidence) > 5 then
        raise exception using errcode = '22023',
            message = 'invalid_operator_correlation_preresolution_completion';
    end if;

    if p_disposition = 'suppressed_unmatched' then
        if v_pre.outcome <> 'unmatched'
           or p_recommended_purchase_intent_id is not null
           or p_supporting_evidence <> '[]'::jsonb
           or p_decision_reason_code is not null then
            raise exception using errcode = '22023',
                message = 'invalid_operator_correlation_preresolution_suppression';
        end if;
    elsif p_disposition = 'abstained' then
        if v_pre.outcome not in ('ambiguous', 'conflict')
           or p_recommended_purchase_intent_id is not null
           or p_supporting_evidence <> '[]'::jsonb
           or p_model_name is null or nullif(btrim(p_model_name), '') is null
           or p_prompt_version is null or nullif(btrim(p_prompt_version), '') is null
           or p_decision_reason_code is null
           or p_decision_reason_code not in (
               'legacy_unclassified',
               'model_abstained',
               'model_abstained_missing_information',
               'proposal_keys_invalid',
               'proposal_shape_invalid',
               'abstention_payload_invalid',
               'confidence_below_high',
               'contradicting_evidence_present',
               'candidate_not_allowed',
               'supporting_evidence_missing',
               'supporting_evidence_unknown',
               'supporting_evidence_candidate_mismatch',
               'independent_discriminating_evidence_missing',
               'independent_discriminating_candidate_not_unique'
           ) then
            raise exception using errcode = '22023',
                message = 'invalid_operator_correlation_preresolution_abstention';
        end if;
    else
        if v_pre.outcome not in ('ambiguous', 'conflict')
           or v_pre.evidence_snapshot is null
           or v_pre.evidence_fingerprint is null
           or p_recommended_purchase_intent_id is null
           or jsonb_array_length(p_supporting_evidence) < 1
           or p_model_name is null or nullif(btrim(p_model_name), '') is null
           or p_prompt_version is null or nullif(btrim(p_prompt_version), '') is null
           or p_decision_reason_code is not null
           or char_length(p_model_name) > 200
           or char_length(p_prompt_version) > 200
           or not exists (
               select 1
               from public.hotmart_purchase_intent_correlation_candidates candidate
               where candidate.webhook_event_id = p_webhook_event_id
                 and candidate.purchase_intent_id = p_recommended_purchase_intent_id
           ) then
            raise exception using errcode = '22023',
                message = 'invalid_operator_correlation_preresolution_recommendation';
        end if;

        for v_item in select value from jsonb_array_elements(p_supporting_evidence)
        loop
            if jsonb_typeof(v_item) <> 'object'
               or (v_item - array['kind', 'value']) <> '{}'::jsonb
               or v_item ->> 'kind' not in (
                   'precheckout_time_proximity_minutes',
                   'nearest_precheckout_by_at_least_5m',
                   'same_product_offer',
                   'chatwoot_phone_exact_match',
                   'chatwoot_email_exact_match',
                   'customer_confirmed_identity',
                   'prior_verified_identity',
                   'event_email_exact_match',
                   'event_phone_exact_match'
               )
               or not exists (
                   select 1
                   from jsonb_array_elements(
                       v_pre.evidence_snapshot -> 'facts'
                   ) fact(value)
                   where fact.value ->> 'candidate_id' =
                           p_recommended_purchase_intent_id::text
                     and fact.value ->> 'kind' = v_item ->> 'kind'
                     and (fact.value -> 'value') is not distinct from
                         (v_item -> 'value')
               ) then
                raise exception using errcode = '22023',
                    message = 'invalid_operator_correlation_preresolution_evidence';
            end if;
        end loop;

        if not exists (
            select 1
            from jsonb_array_elements(p_supporting_evidence) supplied(value)
            join jsonb_array_elements(v_pre.evidence_snapshot -> 'facts') fact(value)
              on fact.value ->> 'candidate_id' =
                    p_recommended_purchase_intent_id::text
             and fact.value ->> 'kind' = supplied.value ->> 'kind'
             and (fact.value -> 'value') is not distinct from
                 (supplied.value -> 'value')
            where (fact.value ->> 'independent')::boolean
              and (fact.value ->> 'discriminating')::boolean
        ) then
            raise exception using errcode = '22023',
                message = 'operator_correlation_preresolution_evidence_not_discriminating';
        end if;

        if (
            select count(distinct fact.value ->> 'candidate_id')
            from jsonb_array_elements(v_pre.evidence_snapshot -> 'facts') fact(value)
            where (fact.value ->> 'independent')::boolean
              and (fact.value ->> 'discriminating')::boolean
        ) <> 1 then
            raise exception using errcode = '22023',
                message = 'operator_correlation_preresolution_discriminator_not_unique';
        end if;

        v_recommendation_ref := gen_random_uuid();
    end if;

    update public.operator_correlation_preresolutions pre
    set status = p_disposition,
        recommendation_ref = v_recommendation_ref,
        recommended_purchase_intent_id = case
            when p_disposition = 'recommended'
            then p_recommended_purchase_intent_id
            else null
        end,
        supporting_evidence = case
            when p_disposition = 'recommended' then p_supporting_evidence
            else '[]'::jsonb
        end,
        model_name = case
            when p_disposition in ('recommended', 'abstained') then p_model_name
            else null
        end,
        prompt_version = case
            when p_disposition in ('recommended', 'abstained') then p_prompt_version
            else null
        end,
        decision_reason_code = case
            when p_disposition = 'abstained' then p_decision_reason_code
            else null
        end,
        decided_at = v_now,
        lease_owner = null,
        claim_token = null,
        lease_expires_at = null,
        updated_at = v_now
    where pre.webhook_event_id = p_webhook_event_id
      and pre.preresolution_version = 1;

    if p_disposition = 'recommended' then
        insert into public.slack_correlation_notification_projection (
            source_event_id,
            tenant_ref,
            funnel_ref,
            outcome,
            reason_code,
            candidate_count,
            occurred_at,
            projection_status,
            notification_contract_version
        )
        select
            pre.webhook_event_id,
            pre.tenant_ref,
            pre.funnel_ref,
            pre.outcome,
            pre.reason_code,
            pre.candidate_count,
            correlation.observed_at,
            'pending',
            3
        from public.operator_correlation_preresolutions pre
        join public.hotmart_purchase_intent_correlations correlation
          on correlation.webhook_event_id = pre.webhook_event_id
        where pre.webhook_event_id = p_webhook_event_id
          and pre.preresolution_version = 1
        on conflict (source_event_id) do update
        set projection_status = 'pending',
            notification_contract_version = 3,
            next_attempt_at = null,
            lease_owner = null,
            claim_token = null,
            lease_expires_at = null,
            last_failure_code = null,
            updated_at = v_now
        where slack_correlation_notification_projection.attempt_count = 0
          and slack_correlation_notification_projection.notification_id is null
          and slack_correlation_notification_projection.notification_contract_version is null
          and slack_correlation_notification_projection.projection_status = 'suppressed';
    end if;

    return query select v_recommendation_ref, p_disposition;
end;
$function$;

-- Rolling compatibility: replicas on the previous bridge contract keep the
-- eight-argument RPC and record a bounded legacy reason until they drain.
create or replace function public.complete_operator_correlation_preresolution(
    p_webhook_event_id uuid,
    p_claim_token uuid,
    p_lease_generation bigint,
    p_disposition text,
    p_recommended_purchase_intent_id uuid default null,
    p_supporting_evidence jsonb default '[]'::jsonb,
    p_model_name text default null,
    p_prompt_version text default null
)
returns table (
    recommendation_ref uuid,
    status text
)
language sql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    select completed.recommendation_ref, completed.status
    from public.complete_operator_correlation_preresolution(
        p_webhook_event_id,
        p_claim_token,
        p_lease_generation,
        p_disposition,
        p_recommended_purchase_intent_id,
        p_supporting_evidence,
        p_model_name,
        p_prompt_version,
        case
            when p_disposition = 'abstained' then 'legacy_unclassified'
            else null
        end
    ) completed;
$function$;

revoke all on function public.complete_operator_correlation_preresolution(
    uuid, uuid, bigint, text, uuid, jsonb, text, text, text
) from public;
revoke all on function public.complete_operator_correlation_preresolution(
    uuid, uuid, bigint, text, uuid, jsonb, text, text
) from public;

do $roles$
begin
    if to_regrole('anon') is not null then
        revoke execute on function public.complete_operator_correlation_preresolution(
            uuid, uuid, bigint, text, uuid, jsonb, text, text, text
        ) from anon;
        revoke execute on function public.complete_operator_correlation_preresolution(
            uuid, uuid, bigint, text, uuid, jsonb, text, text
        ) from anon;
    end if;
    if to_regrole('authenticated') is not null then
        revoke execute on function public.complete_operator_correlation_preresolution(
            uuid, uuid, bigint, text, uuid, jsonb, text, text, text
        ) from authenticated;
        revoke execute on function public.complete_operator_correlation_preresolution(
            uuid, uuid, bigint, text, uuid, jsonb, text, text
        ) from authenticated;
    end if;
    if to_regrole('service_role') is not null then
        grant execute on function public.complete_operator_correlation_preresolution(
            uuid, uuid, bigint, text, uuid, jsonb, text, text, text
        ) to service_role;
        grant execute on function public.complete_operator_correlation_preresolution(
            uuid, uuid, bigint, text, uuid, jsonb, text, text
        ) to service_role;
    end if;
end;
$roles$;

commit;
