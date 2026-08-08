-- Correlaciona PURCHASE_APPROVED de Hotmart y detiene la recuperación activa.
-- La correlación es exacta y fail-closed: cualquier ambigüedad pausa todos los
-- casos candidatos antes de devolver control al worker.

begin;

create unique index webhook_events_hotmart_purchase_transaction_unique_idx
on public.webhook_events ((payload #>> '{data,purchase,transaction}'))
where source = 'hotmart'
  and event_type = 'PURCHASE_APPROVED'
  and nullif(payload #>> '{data,purchase,transaction}', '') is not null;

create or replace function public.apply_hotmart_purchase_approved(
    p_webhook_event_id uuid,
    p_buyer_email text,
    p_buyer_phone text,
    p_external_product_id text,
    p_offer_code text,
    p_transaction text,
    p_approved_at timestamptz
)
returns table (
    outcome text,
    recovery_case_id uuid,
    matched_by text
)
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
declare
    v_event public.webhook_events%rowtype;
    v_contact_ids uuid[];
    v_case_ids uuid[];
    v_contact_id uuid;
    v_case_id uuid;
    v_email_matched boolean := false;
    v_phone_matched boolean := false;
    v_matched_by text;
    v_payload_approved_at timestamptz;
    v_buyer_email text;
    v_buyer_phone text;
    v_payload_email text;
    v_payload_phone text;
begin
    v_buyer_email := nullif(lower(btrim(p_buyer_email)), '');
    v_buyer_phone := nullif(regexp_replace(
        coalesce(p_buyer_phone, ''), '[^0-9]', '', 'g'
    ), '');

    if p_webhook_event_id is null
       or p_external_product_id is null
       or btrim(p_external_product_id) = ''
       or p_transaction is null
       or p_transaction !~ '^HP[A-Z0-9]{6,62}$'
       or p_approved_at is null
       or (v_buyer_email is null and v_buyer_phone is null)
       or (nullif(btrim(p_buyer_phone), '') is not null and v_buyer_phone is null) then
        raise exception using
            errcode = '22023',
            message = 'invalid_purchase_correlation_input';
    end if;

    select we.* into v_event
    from public.webhook_events we
    where we.id = p_webhook_event_id
    for update;

    if not found then
        raise exception using errcode = 'P0002', message = 'webhook_event_not_found';
    end if;
    if v_event.source <> 'hotmart'
       or v_event.event_type <> 'PURCHASE_APPROVED' then
        raise exception using
            errcode = '22023',
            message = 'webhook_event_not_purchase_approved';
    end if;

    if v_event.processing_status = 'processed' then
        select rc.id into v_case_id
        from public.recovery_cases rc
        where rc.purchase_event_id = p_webhook_event_id;
        if v_case_id is null then
            raise exception using
                errcode = '55000',
                message = 'processed_purchase_without_recovery_case';
        end if;
        return query select 'already_applied'::text, v_case_id, null::text;
        return;
    end if;
    if v_event.processing_status <> 'received' then
        raise exception using
            errcode = '55000',
            message = 'purchase_event_not_processable';
    end if;

    -- Defense in depth: the RPC inputs must describe the locked event itself.
    begin
        v_payload_approved_at := to_timestamp(
            ((v_event.payload #>> '{data,purchase,approved_date}')::bigint)::double precision
            / 1000.0
        );
    exception when others then
        raise exception using
            errcode = '22023',
            message = 'purchase_event_invalid_approved_date';
    end;

    v_payload_email := nullif(lower(btrim(
        v_event.payload #>> '{data,buyer,email}'
    )), '');
    v_payload_phone := nullif(regexp_replace(
        coalesce(v_event.payload #>> '{data,buyer,checkout_phone}', ''),
        '[^0-9]',
        '',
        'g'
    ), '');

    if v_event.payload #>> '{event}' is distinct from 'PURCHASE_APPROVED'
       or v_event.payload #>> '{version}' is distinct from '2.0.0'
       or v_event.payload #>> '{data,purchase,status}' is distinct from 'APPROVED'
       or v_event.payload #>> '{data,purchase,transaction}' is distinct from p_transaction
       or v_event.payload #>> '{data,product,id}' is distinct from p_external_product_id
       or nullif(v_event.payload #>> '{data,purchase,offer,code}', '')
          is distinct from nullif(p_offer_code, '')
       or v_payload_email is distinct from v_buyer_email
       or v_payload_phone is distinct from v_buyer_phone
       or v_payload_approved_at is distinct from p_approved_at then
        raise exception using
            errcode = '22023',
            message = 'purchase_rpc_payload_mismatch';
    end if;

    if p_approved_at > v_event.received_at + interval '5 minutes' then
        raise exception using
            errcode = '22023',
            message = 'purchase_approved_at_in_future';
    end if;

    -- Keep identity resolution stable until this transaction commits.
    lock table public.contacts in share mode;
    lock table public.contact_points in share mode;

    with contact_matches as (
        select c.id as contact_id, 'email'::text as matched_kind
        from public.contacts c
        where v_buyer_email is not null
          and lower(btrim(c.email)) = v_buyer_email
        union all
        select c.id, 'phone'::text
        from public.contacts c
        where v_buyer_phone is not null
          and regexp_replace(coalesce(c.phone, ''), '[^0-9]', '', 'g') =
              v_buyer_phone
        union all
        select cp.contact_id, 'email'::text
        from public.contact_points cp
        where cp.type = 'email'
          and v_buyer_email is not null
          and lower(btrim(cp.normalized_value)) = v_buyer_email
        union all
        select cp.contact_id, 'phone'::text
        from public.contact_points cp
        where cp.type = 'phone'
          and v_buyer_phone is not null
          and regexp_replace(cp.normalized_value, '[^0-9]', '', 'g') =
              v_buyer_phone
    )
    select array_agg(distinct cm.contact_id order by cm.contact_id)
    into v_contact_ids
    from contact_matches cm;

    if coalesce(cardinality(v_contact_ids), 0) = 0 then
        update public.webhook_events
        set processing_status = 'failed',
            processing_error = 'purchase_correlation_contact_not_found',
            processed_at = now()
        where id = p_webhook_event_id;
        return query select 'not_found'::text, null::uuid, null::text;
        return;
    end if;

    -- Match the established lock order: event -> contact -> case -> sequence -> action.
    perform 1
    from public.contacts c
    where c.id = any(v_contact_ids)
    order by c.id
    for update;

    select array_agg(distinct rc.id order by rc.id)
    into v_case_ids
    from public.recovery_cases rc
    join public.followup_sequences candidate_sequence
      on candidate_sequence.recovery_case_id = rc.id
     and candidate_sequence.status in ('active', 'paused')
    join public.followup_policy_versions policy
      on policy.policy_key = candidate_sequence.policy_key
     and policy.version = candidate_sequence.policy_version
    where rc.contact_id = any(v_contact_ids)
      and rc.source = 'hotmart'
      and rc.external_product_id = p_external_product_id
      and rc.offer_code is not distinct from p_offer_code
      and rc.status in ('grace_period', 'active', 'paused')
      and rc.purchase_event_id is null
      and rc.created_at >= p_approved_at - policy.expires_after
      and rc.created_at <= p_approved_at + interval '5 minutes';

    if cardinality(v_contact_ids) = 1
       and coalesce(cardinality(v_case_ids), 0) = 0 then
        update public.webhook_events
        set processing_status = 'failed',
            processing_error = 'purchase_correlation_case_not_found',
            processed_at = now()
        where id = p_webhook_event_id;
        return query select 'not_found'::text, null::uuid, null::text;
        return;
    end if;

    if coalesce(cardinality(v_contact_ids), 0) <> 1
       or coalesce(cardinality(v_case_ids), 0) <> 1 then
        if coalesce(cardinality(v_case_ids), 0) > 0 then
            perform 1
            from public.recovery_cases rc
            where rc.id = any(v_case_ids)
            order by rc.id
            for update;
            perform 1
            from public.followup_sequences fs
            where fs.recovery_case_id = any(v_case_ids)
              and fs.status in ('active', 'paused')
            order by fs.id
            for update;
            perform 1
            from public.scheduled_actions sa
            where sa.recovery_case_id = any(v_case_ids)
              and sa.status in ('pending', 'deferred', 'retryable_failed')
            order by sa.id
            for update;
            perform 1
            from public.followup_delivery_attempts attempt
            join public.scheduled_actions sa on sa.id = attempt.action_id
            where sa.recovery_case_id = any(v_case_ids)
              and attempt.phase = 'request_started'
            order by attempt.id
            for update of attempt;

            update public.scheduled_actions sa
            set status = 'delivery_unknown',
                terminal_reason = 'purchase_correlation_ambiguous_request_in_flight',
                lease_owner = null,
                lease_expires_at = null,
                next_attempt_at = null
            where sa.recovery_case_id = any(v_case_ids)
              and sa.status in ('pending', 'deferred', 'retryable_failed')
              and exists (
                  select 1
                  from public.followup_delivery_attempts attempt
                  where attempt.action_id = sa.id
                    and attempt.phase = 'request_started'
              );

            update public.scheduled_actions sa
            set status = 'cancelled',
                terminal_reason = 'purchase_correlation_ambiguous',
                lease_owner = null,
                lease_expires_at = null,
                next_attempt_at = null
            where sa.recovery_case_id = any(v_case_ids)
              and sa.status in ('pending', 'deferred', 'retryable_failed')
              and not exists (
                  select 1
                  from public.followup_delivery_attempts attempt
                  where attempt.action_id = sa.id
                    and attempt.phase = 'request_started'
              );

            update public.followup_sequences fs
            set status = 'paused',
                revision = revision + 1
            where fs.recovery_case_id = any(v_case_ids)
              and fs.status = 'active';

            update public.recovery_cases
            set status = 'paused',
                next_contact_at = null,
                next_contact_reason = 'purchase_correlation_ambiguous',
                version = version + 1
            where id = any(v_case_ids)
              and status in ('grace_period', 'active');

            insert into public.conversation_events (
                recovery_case_id,
                event_type,
                actor_type,
                data
            )
            select
                candidate_id,
                'purchase_correlation_ambiguous',
                'integration',
                jsonb_build_object(
                    'webhook_event_id', p_webhook_event_id,
                    'candidate_count', cardinality(v_case_ids)
                )
            from unnest(v_case_ids) as candidate_id;
        end if;

        update public.webhook_events
        set processing_status = 'failed',
            processing_error = case
                when cardinality(v_contact_ids) <> 1
                    then 'purchase_correlation_contact_ambiguous'
                else 'purchase_correlation_case_ambiguous'
            end,
            processed_at = now()
        where id = p_webhook_event_id;
        return query select 'ambiguous'::text, null::uuid, null::text;
        return;
    end if;

    v_contact_id := v_contact_ids[1];
    v_case_id := v_case_ids[1];

    select
        exists (
            select 1 from public.contact_points cp
            where cp.contact_id = v_contact_id
              and cp.type = 'email'
              and v_buyer_email is not null
              and lower(btrim(cp.normalized_value)) = v_buyer_email
            union all
            select 1 from public.contacts c
            where c.id = v_contact_id
              and v_buyer_email is not null
              and lower(btrim(c.email)) = v_buyer_email
        ),
        exists (
            select 1 from public.contact_points cp
            where cp.contact_id = v_contact_id
              and cp.type = 'phone'
              and v_buyer_phone is not null
              and regexp_replace(cp.normalized_value, '[^0-9]', '', 'g') =
                  v_buyer_phone
            union all
            select 1 from public.contacts c
            where c.id = v_contact_id
              and v_buyer_phone is not null
              and regexp_replace(coalesce(c.phone, ''), '[^0-9]', '', 'g') =
                  v_buyer_phone
        )
    into v_email_matched, v_phone_matched;

    v_matched_by := case
        when v_email_matched and v_phone_matched then 'email_and_phone'
        when v_email_matched then 'email'
        when v_phone_matched then 'phone'
        else null
    end;

    if v_matched_by is null then
        raise exception using
            errcode = '40001',
            message = 'purchase_identity_changed_concurrently';
    end if;

    perform 1
    from public.recovery_cases rc
    where rc.id = v_case_id
    for update;
    perform 1
    from public.followup_sequences fs
    where fs.recovery_case_id = v_case_id
      and fs.status in ('active', 'paused')
    order by fs.id
    for update;
    perform 1
    from public.scheduled_actions sa
    where sa.recovery_case_id = v_case_id
      and sa.status in ('pending', 'deferred', 'retryable_failed')
    order by sa.id
    for update;
    perform 1
    from public.followup_delivery_attempts attempt
    join public.scheduled_actions sa on sa.id = attempt.action_id
    where sa.recovery_case_id = v_case_id
      and attempt.phase = 'request_started'
    order by attempt.id
    for update of attempt;

    update public.scheduled_actions sa
    set status = 'delivery_unknown',
        terminal_reason = 'purchase_detected_request_in_flight',
        lease_owner = null,
        lease_expires_at = null,
        next_attempt_at = null
    where sa.recovery_case_id = v_case_id
      and sa.status in ('pending', 'deferred', 'retryable_failed')
      and exists (
          select 1
          from public.followup_delivery_attempts attempt
          where attempt.action_id = sa.id
            and attempt.phase = 'request_started'
      );

    update public.scheduled_actions sa
    set status = 'cancelled',
        terminal_reason = 'purchase_detected',
        lease_owner = null,
        lease_expires_at = null,
        next_attempt_at = null
    where sa.recovery_case_id = v_case_id
      and sa.status in ('pending', 'deferred', 'retryable_failed')
      and not exists (
          select 1
          from public.followup_delivery_attempts attempt
          where attempt.action_id = sa.id
            and attempt.phase = 'request_started'
      );

    update public.followup_sequences fs
    set status = 'completed',
        completion_reason = 'purchase_detected',
        completed_at = coalesce(completed_at, now()),
        revision = revision + 1
    where fs.recovery_case_id = v_case_id
      and fs.status in ('active', 'paused');

    update public.recovery_cases
    set status = 'won',
        lead_stage = 'won',
        purchase_event_id = p_webhook_event_id,
        won_at = p_approved_at,
        closed_at = now(),
        next_contact_at = null,
        next_contact_reason = 'purchase_detected',
        context = context || jsonb_build_object(
            'purchase_transaction', p_transaction,
            'purchase_approved_at', p_approved_at,
            'purchase_matched_by', v_matched_by
        ),
        version = version + 1
    where id = v_case_id
      and status in ('grace_period', 'active', 'paused')
      and purchase_event_id is null;

    if not found then
        raise exception using
            errcode = '40001',
            message = 'purchase_case_changed_concurrently';
    end if;

    insert into public.conversation_events (
        recovery_case_id,
        event_type,
        actor_type,
        data
    ) values (
        v_case_id,
        'purchase_detected',
        'integration',
        jsonb_build_object(
            'webhook_event_id', p_webhook_event_id,
            'matched_by', v_matched_by
        )
    );

    update public.webhook_events
    set processing_status = 'processed',
        processing_error = null,
        processed_at = now()
    where id = p_webhook_event_id;

    return query select 'applied'::text, v_case_id, v_matched_by;
end;
$function$;

revoke all on function public.apply_hotmart_purchase_approved(
    uuid, text, text, text, text, text, timestamptz
) from public;

do $grants$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'revoke all on function public.apply_hotmart_purchase_approved(uuid, text, text, text, text, text, timestamptz) from anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'revoke all on function public.apply_hotmart_purchase_approved(uuid, text, text, text, text, text, timestamptz) from authenticated';
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'grant execute on function public.apply_hotmart_purchase_approved(uuid, text, text, text, text, text, timestamptz) to service_role';
    end if;
end;
$grants$;

commit;
