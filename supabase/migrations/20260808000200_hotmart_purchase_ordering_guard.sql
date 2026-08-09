-- Evita programar recuperación cuando la compra aprobada llegó primero.
-- El trigger diferido corre al commit del plan, cuando caso, secuencia, acción
-- y auditoría inicial ya existen. SKIP LOCKED evita invertir el orden de locks
-- frente al worker de compra concurrente; ese worker correlacionará al terminar
-- el transaction que está creando el caso.

begin;

create or replace function public.stop_cart_recovery_for_known_purchase()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
declare
    v_case public.recovery_cases%rowtype;
    v_purchase_event_id uuid;
    v_purchase_approved_at timestamptz;
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

    select we.id, parsed.approved_at
      into v_purchase_event_id, v_purchase_approved_at
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
      and (
          select count(distinct identity_match.contact_id) = 1
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
                          '[^0-9]',
                          '',
                          'g'
                      ),
                      ''
                  ) is not null
                  and regexp_replace(c.phone, '[^0-9]', '', 'g') =
                      regexp_replace(
                          we.payload #>> '{data,buyer,checkout_phone}',
                          '[^0-9]',
                          '',
                          'g'
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
                      '[^0-9]',
                      '',
                      'g'
                  )
              )
          ) identity_match
      )
      and (
          select count(*) = 1
          from public.recovery_cases candidate_case
          where candidate_case.contact_id = v_case.contact_id
            and candidate_case.source = v_case.source
            and candidate_case.external_product_id = v_case.external_product_id
            and candidate_case.offer_code is not distinct from v_case.offer_code
            and candidate_case.status in ('grace_period', 'active', 'paused')
      )
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
                    )
                    or (
                        nullif(
                            regexp_replace(
                                we.payload #>> '{data,buyer,checkout_phone}',
                                '[^0-9]',
                                '',
                                'g'
                            ),
                            ''
                        ) is not null
                        and regexp_replace(c.phone, '[^0-9]', '', 'g') =
                            regexp_replace(
                                we.payload #>> '{data,buyer,checkout_phone}',
                                '[^0-9]',
                                '',
                                'g'
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
                    )
                    or (
                        cp.type = 'phone'
                        and cp.normalized_value = regexp_replace(
                            we.payload #>> '{data,buyer,checkout_phone}',
                            '[^0-9]',
                            '',
                            'g'
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
    set status = 'cancelled',
        next_attempt_at = null,
        terminal_reason = 'purchase_detected',
        lease_owner = null,
        lease_expires_at = null
    where sa.id = new.id
      and sa.status in ('pending', 'deferred', 'retryable_failed');

    update public.followup_sequences fs
    set status = 'completed',
        completion_reason = 'purchase_detected',
        completed_at = coalesce(fs.completed_at, now()),
        revision = fs.revision + 1
    where fs.id = new.followup_sequence_id
      and fs.status in ('active', 'paused');

    update public.recovery_cases rc
    set status = 'won',
        lead_stage = 'won',
        purchase_event_id = v_purchase_event_id,
        won_at = coalesce(rc.won_at, v_purchase_approved_at),
        closed_at = coalesce(rc.closed_at, now()),
        next_contact_at = null,
        next_contact_reason = null,
        context = rc.context || jsonb_build_object(
            'purchase_transaction', (
                select purchase_event.payload #>> '{data,purchase,transaction}'
                from public.webhook_events purchase_event
                where purchase_event.id = v_purchase_event_id
            ),
            'purchase_approved_at', v_purchase_approved_at,
            'purchase_correlation', 'known_purchase_before_recovery_plan'
        ),
        version = rc.version + 1
    where rc.id = new.recovery_case_id
      and rc.status in ('grace_period', 'active', 'paused');

    insert into public.conversation_events (
        recovery_case_id,
        event_type,
        actor_type,
        related_action_id,
        data
    ) values (
        new.recovery_case_id,
        'purchase_detected',
        'integration',
        new.id,
        jsonb_build_object(
            'reason_code', 'known_purchase_before_recovery_plan',
            'webhook_event_id', v_purchase_event_id
        )
    );

    update public.webhook_events we
    set processing_status = 'processed',
        processed_at = now(),
        processing_error = null
    where we.id = v_purchase_event_id;

    return new;
end;
$function$;

revoke all on function public.stop_cart_recovery_for_known_purchase()
from public;

do $grants$
begin
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'grant execute on function public.stop_cart_recovery_for_known_purchase() to service_role';
    end if;
end;
$grants$;

drop trigger if exists scheduled_actions_stop_for_known_purchase
on public.scheduled_actions;

create constraint trigger scheduled_actions_stop_for_known_purchase
after insert on public.scheduled_actions
deferrable initially deferred
for each row
execute function public.stop_cart_recovery_for_known_purchase();

commit;
