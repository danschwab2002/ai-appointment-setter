-- Forward-only safety fences for the already-applied Hotmart purchase vertical.
-- Keep this migration additive: earlier 20260808000100..00300 migrations were
-- applied manually to the pilot database and must not be rewritten in place.

begin;

create or replace function public.finalize_purchase_stopped_delivery_attempts()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    if new.terminal_reason is null
       or new.terminal_reason not in (
        'purchase_detected',
        'purchase_detected_request_in_flight',
        'purchase_correlation_ambiguous',
        'purchase_correlation_ambiguous_request_in_flight'
    ) or new.status not in ('cancelled', 'delivery_unknown') then
        return new;
    end if;

    update public.followup_delivery_attempts
    set phase = 'completed',
        outcome = 'failed_before_request',
        reason_code = new.terminal_reason,
        finalized_next_attempt_at = null,
        updated_at = clock_timestamp()
    where action_id = new.id
      and phase = 'reserved';

    update public.followup_delivery_attempts
    set phase = 'completed',
        outcome = 'delivery_unknown',
        reason_code = new.terminal_reason,
        reconciliation_deadline = coalesce(
            reconciliation_deadline,
            clock_timestamp() + interval '15 minutes'
        ),
        finalized_next_attempt_at = null,
        updated_at = clock_timestamp()
    where action_id = new.id
      and phase = 'request_started';

    return new;
end;
$function$;

drop trigger if exists scheduled_actions_finalize_purchase_attempts
on public.scheduled_actions;

create trigger scheduled_actions_finalize_purchase_attempts
before update of status, terminal_reason on public.scheduled_actions
for each row execute function public.finalize_purchase_stopped_delivery_attempts();

create or replace function public.fail_closed_ambiguous_known_purchase()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
declare
    v_case public.recovery_cases%rowtype;
    v_purchase_event_id uuid;
    v_identity_count integer;
begin
    if new.action_type <> 'first_contact_review'
       or new.status not in ('pending', 'deferred', 'retryable_failed') then
        return new;
    end if;

    select rc.* into v_case
    from public.recovery_cases rc
    where rc.id = new.recovery_case_id
      and rc.status in ('grace_period', 'active', 'paused')
    for update;

    if not found then
        return new;
    end if;

    select we.id, identity_matches.match_count
      into v_purchase_event_id, v_identity_count
    from public.webhook_events we
    join public.webhook_events abandonment
      on abandonment.id = v_case.abandonment_event_id
    cross join lateral (
        select case
            when we.payload #>> '{data,purchase,approved_date}'
                 ~ '^[0-9]{10,16}$'
            then to_timestamp(
                (we.payload #>> '{data,purchase,approved_date}')::double precision
                / 1000.0
            )
            else null
        end as approved_at
    ) parsed
    cross join lateral (
        select count(distinct candidate.contact_id)::integer as match_count
        from (
            select c.id as contact_id
            from public.contacts c
            where (
                nullif(we.payload #>> '{data,buyer,email}', '') is not null
                and lower(btrim(c.email)) = lower(btrim(
                    we.payload #>> '{data,buyer,email}'
                ))
            ) or (
                nullif(
                    regexp_replace(
                        we.payload #>> '{data,buyer,checkout_phone}',
                        '[^0-9]', '', 'g'
                    ),
                    ''
                ) is not null
                and regexp_replace(c.phone, '[^0-9]', '', 'g') =
                    regexp_replace(
                        we.payload #>> '{data,buyer,checkout_phone}',
                        '[^0-9]', '', 'g'
                    )
            )
            union all
            select cp.contact_id
            from public.contact_points cp
            where (
                cp.type = 'email'
                and cp.normalized_value = lower(btrim(
                    we.payload #>> '{data,buyer,email}'
                ))
            ) or (
                cp.type = 'phone'
                and cp.normalized_value = regexp_replace(
                    we.payload #>> '{data,buyer,checkout_phone}',
                    '[^0-9]', '', 'g'
                )
            )
        ) candidate
    ) identity_matches
    where we.source = 'hotmart'
      and we.event_type = 'PURCHASE_APPROVED'
      and (
          we.processing_status = 'received'
          or (
              we.processing_status = 'failed'
              and we.processing_error in (
                  'purchase_correlation_contact_not_found',
                  'purchase_correlation_case_not_found'
              )
          )
      )
      and we.payload #>> '{version}' = '2.0.0'
      and we.payload #>> '{event}' = 'PURCHASE_APPROVED'
      and we.payload #>> '{data,purchase,status}' = 'APPROVED'
      and we.payload #>> '{data,purchase,transaction}' ~ '^HP[A-Z0-9]{6,62}$'
      and we.payload #>> '{data,product,id}' = v_case.external_product_id
      and nullif(we.payload #>> '{data,purchase,offer,code}', '')
          is not distinct from v_case.offer_code
      and parsed.approved_at is not null
      and parsed.approved_at >= case
          when abandonment.payload #>> '{creation_date}' ~ '^[0-9]{10,16}$'
          then to_timestamp(
              (abandonment.payload #>> '{creation_date}')::double precision
              / 1000.0
          ) - interval '5 minutes'
          else abandonment.received_at - interval '5 minutes'
      end
      and identity_matches.match_count > 1
      and (
          exists (
              select 1
              from public.contacts c
              where c.id = v_case.contact_id
                and (
                    (
                        nullif(we.payload #>> '{data,buyer,email}', '') is not null
                        and lower(btrim(c.email)) = lower(btrim(
                            we.payload #>> '{data,buyer,email}'
                        ))
                    ) or (
                        nullif(
                            regexp_replace(
                                we.payload #>> '{data,buyer,checkout_phone}',
                                '[^0-9]', '', 'g'
                            ),
                            ''
                        ) is not null
                        and regexp_replace(c.phone, '[^0-9]', '', 'g') =
                            regexp_replace(
                                we.payload #>> '{data,buyer,checkout_phone}',
                                '[^0-9]', '', 'g'
                            )
                    )
                )
          )
          or exists (
              select 1
              from public.contact_points cp
              where cp.contact_id = v_case.contact_id
                and (
                    (
                        cp.type = 'email'
                        and cp.normalized_value = lower(btrim(
                            we.payload #>> '{data,buyer,email}'
                        ))
                    ) or (
                        cp.type = 'phone'
                        and cp.normalized_value = regexp_replace(
                            we.payload #>> '{data,buyer,checkout_phone}',
                            '[^0-9]', '', 'g'
                        )
                    )
                )
          )
      )
    order by parsed.approved_at asc, we.received_at asc, we.id::text asc
    limit 1
    for update of we skip locked;

    if v_purchase_event_id is null then
        return new;
    end if;

    perform 1
    from public.followup_sequences fs
    where fs.id = new.followup_sequence_id
    for update;

    update public.scheduled_actions sa
    set status = 'delivery_unknown',
        terminal_reason = 'purchase_correlation_ambiguous_request_in_flight',
        lease_owner = null,
        lease_expires_at = null,
        next_attempt_at = null
    where sa.id = new.id
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
    where sa.id = new.id
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
    where fs.id = new.followup_sequence_id
      and fs.status = 'active';

    update public.recovery_cases rc
    set status = 'paused',
        next_contact_at = null,
        next_contact_reason = 'purchase_correlation_ambiguous',
        version = version + 1
    where rc.id = new.recovery_case_id
      and rc.status in ('grace_period', 'active');

    insert into public.conversation_events (
        conversation_id,
        recovery_case_id,
        event_type,
        actor_type,
        data
    ) values (
        v_case.conversation_id,
        v_case.id,
        'purchase_correlation_ambiguous',
        'integration',
        jsonb_build_object(
            'webhook_event_id', v_purchase_event_id,
            'identity_candidate_count', v_identity_count,
            'ordering', 'purchase_before_abandonment'
        )
    );

    update public.webhook_events
    set processing_status = 'failed',
        processing_error = 'purchase_correlation_contact_ambiguous',
        processed_at = now()
    where id = v_purchase_event_id;

    return new;
end;
$function$;

drop trigger if exists scheduled_actions_fail_closed_known_purchase_ambiguity
on public.scheduled_actions;

create constraint trigger scheduled_actions_fail_closed_known_purchase_ambiguity
after insert on public.scheduled_actions
deferrable initially deferred
for each row execute function public.fail_closed_ambiguous_known_purchase();

revoke execute on function public.finalize_purchase_stopped_delivery_attempts()
from public;
revoke execute on function public.fail_closed_ambiguous_known_purchase()
from public;

do $privileges$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'revoke execute on function public.finalize_purchase_stopped_delivery_attempts() from anon';
        execute 'revoke execute on function public.fail_closed_ambiguous_known_purchase() from anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'revoke execute on function public.finalize_purchase_stopped_delivery_attempts() from authenticated';
        execute 'revoke execute on function public.fail_closed_ambiguous_known_purchase() from authenticated';
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'revoke execute on function public.finalize_purchase_stopped_delivery_attempts() from service_role';
        execute 'revoke execute on function public.fail_closed_ambiguous_known_purchase() from service_role';
    end if;
end;
$privileges$;

commit;
