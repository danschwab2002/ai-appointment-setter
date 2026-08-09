-- Semantic idempotency for Hotmart PURCHASE_APPROVED admission.
-- A transaction replay is a duplicate only when its processable normalized
-- business tuple is identical. A changed tuple becomes a durable global
-- outbound blocker at the request_started boundary.

begin;

create table public.hotmart_purchase_semantic_conflicts (
    id uuid primary key default gen_random_uuid(),
    transaction text not null check (transaction ~ '^HP[A-Z0-9]{6,62}$'),
    existing_event_id uuid not null
        references public.webhook_events(id) on delete restrict,
    incoming_external_event_id text not null,
    existing_semantic_tuple jsonb not null,
    incoming_semantic_tuple jsonb not null,
    incoming_payload jsonb not null,
    detected_at timestamptz not null default clock_timestamp(),
    resolved_at timestamptz,
    resolution text,
    unique (transaction, incoming_external_event_id),
    check (
        (resolved_at is null and resolution is null)
        or (resolved_at is not null and nullif(btrim(resolution), '') is not null)
    )
);

create index hotmart_purchase_semantic_conflicts_unresolved_idx
on public.hotmart_purchase_semantic_conflicts (detected_at, id)
where resolved_at is null;

alter table public.hotmart_purchase_semantic_conflicts enable row level security;

create or replace function public.hotmart_purchase_semantic_tuple(p_payload jsonb)
returns jsonb
language sql
immutable
strict
security invoker
set search_path = public, pg_temp
as $function$
    select jsonb_build_object(
        'event', p_payload #>> '{event}',
        'version', p_payload #>> '{version}',
        'status', p_payload #>> '{data,purchase,status}',
        'transaction', p_payload #>> '{data,purchase,transaction}',
        'product_id', p_payload #>> '{data,product,id}',
        'offer_code', case
            when jsonb_typeof(
                p_payload #> '{data,purchase,offer,code}'
            ) = 'string'
            then nullif(btrim(
                p_payload #>> '{data,purchase,offer,code}'
            ), '')
            else null
        end,
        'buyer_email', case
            when jsonb_typeof(p_payload #> '{data,buyer,email}') = 'string'
            then nullif(lower(btrim(
                p_payload #>> '{data,buyer,email}'
            )), '')
            else null
        end,
        'buyer_phone', case
            when jsonb_typeof(
                p_payload #> '{data,buyer,checkout_phone}'
            ) = 'string'
            and p_payload #>> '{data,buyer,checkout_phone}'
                ~ '^\+?[0-9 ()-]+$'
            then nullif(regexp_replace(
                p_payload #>> '{data,buyer,checkout_phone}',
                '[^0-9]', '', 'g'
            ), '')
            else null
        end,
        'approved_date', p_payload #>> '{data,purchase,approved_date}'
    );
$function$;

create or replace function public.hotmart_purchase_payload_is_processable(
    p_external_event_id text,
    p_payload jsonb
)
returns boolean
language sql
immutable
strict
security invoker
set search_path = public, pg_temp
as $function$
    select
        jsonb_typeof(p_payload) = 'object'
        and jsonb_typeof(p_payload -> 'id') = 'string'
        and nullif(btrim(p_payload ->> 'id'), '') is not null
        and p_payload ->> 'id' = p_external_event_id
        and p_payload ->> 'event' = 'PURCHASE_APPROVED'
        and p_payload ->> 'version' = '2.0.0'
        and jsonb_typeof(p_payload -> 'creation_date') = 'number'
        and p_payload ->> 'creation_date' ~ '^[0-9]+$'
        and case
            when p_payload ->> 'creation_date' ~ '^[0-9]+$'
            then (p_payload ->> 'creation_date')::numeric
                <= 253402300799999::numeric
            else false
        end
        and jsonb_typeof(p_payload -> 'data') = 'object'
        and jsonb_typeof(p_payload #> '{data,buyer}') = 'object'
        and jsonb_typeof(p_payload #> '{data,product}') = 'object'
        and jsonb_typeof(p_payload #> '{data,purchase}') = 'object'
        and p_payload #>> '{data,purchase,status}' = 'APPROVED'
        and jsonb_typeof(
            p_payload #> '{data,purchase,transaction}'
        ) = 'string'
        and p_payload #>> '{data,purchase,transaction}'
            ~ '^HP[A-Z0-9]{6,62}$'
        and jsonb_typeof(p_payload #> '{data,product,id}') = 'number'
        and p_payload #>> '{data,product,id}' ~ '^-?[0-9]+$'
        and jsonb_typeof(
            p_payload #> '{data,purchase,approved_date}'
        ) = 'number'
        and p_payload #>> '{data,purchase,approved_date}' ~ '^[0-9]+$'
        and case
            when p_payload #>> '{data,purchase,approved_date}' ~ '^[0-9]+$'
            then (p_payload #>> '{data,purchase,approved_date}')::numeric
                <= 253402300799999::numeric
            else false
        end
        and (
            (
                jsonb_typeof(p_payload #> '{data,buyer,email}') = 'string'
                and nullif(btrim(
                    p_payload #>> '{data,buyer,email}'
                ), '') is not null
            )
            or (
                jsonb_typeof(
                    p_payload #> '{data,buyer,checkout_phone}'
                ) = 'string'
                and p_payload #>> '{data,buyer,checkout_phone}'
                    ~ '^\+?[0-9 ()-]+$'
                and nullif(regexp_replace(
                    p_payload #>> '{data,buyer,checkout_phone}',
                    '[^0-9]', '', 'g'
                ), '') is not null
            )
        );
$function$;

create or replace function public.admit_hotmart_purchase_approved(
    p_external_event_id text,
    p_payload jsonb
)
returns table (
    outcome text,
    webhook_event_id uuid
)
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_transaction text;
    v_existing public.webhook_events%rowtype;
    v_existing_tuple jsonb;
    v_incoming_tuple jsonb;
    v_conflict public.hotmart_purchase_semantic_conflicts%rowtype;
begin
    -- Serialize conflict admission with the absolute request-start gate. If
    -- request_started wins, it commits before this admission observes attempts;
    -- if admission wins, request start observes the committed blocker.
    perform pg_advisory_xact_lock(7275726368617365);

    if p_external_event_id is null
       or btrim(p_external_event_id) = ''
       or p_payload is null
       or not coalesce(
           public.hotmart_purchase_payload_is_processable(
               p_external_event_id,
               p_payload
           ),
           false
       ) then
        raise exception using
            errcode = '22023',
            message = 'invalid_purchase_admission_input';
    end if;

    v_transaction := p_payload #>> '{data,purchase,transaction}';

    -- Serialize lookup+insert with legacy bridge versions that may still use a
    -- direct webhook_events insert during a rolling deploy.
    lock table public.webhook_events in share row exclusive mode;
    lock table public.hotmart_purchase_semantic_conflicts
        in share row exclusive mode;

    select event.* into v_existing
    from public.webhook_events event
    where event.source = 'hotmart'
      and (
          event.external_event_id = p_external_event_id
          or (
              event.event_type = 'PURCHASE_APPROVED'
              and event.payload #>> '{data,purchase,transaction}' = v_transaction
          )
      )
    order by
        (event.external_event_id = p_external_event_id) desc,
        event.received_at asc,
        event.id::text asc
    limit 1
    for update;

    if not found then
        insert into public.webhook_events (
            source,
            external_event_id,
            event_type,
            payload,
            processing_status
        ) values (
            'hotmart',
            p_external_event_id,
            'PURCHASE_APPROVED',
            p_payload,
            'received'
        )
        returning id into webhook_event_id;

        outcome := 'inserted';
        return next;
        return;
    end if;

    v_existing_tuple := public.hotmart_purchase_semantic_tuple(v_existing.payload);
    v_incoming_tuple := public.hotmart_purchase_semantic_tuple(p_payload);

    if v_existing.source = 'hotmart'
       and v_existing.event_type = 'PURCHASE_APPROVED'
       and coalesce(
           public.hotmart_purchase_payload_is_processable(
               v_existing.external_event_id,
               v_existing.payload
           ),
           false
       )
       and v_existing_tuple = v_incoming_tuple then
        outcome := 'duplicate';
        webhook_event_id := v_existing.id;
        return next;
        return;
    end if;

    insert into public.hotmart_purchase_semantic_conflicts (
        transaction,
        existing_event_id,
        incoming_external_event_id,
        existing_semantic_tuple,
        incoming_semantic_tuple,
        incoming_payload
    ) values (
        v_transaction,
        v_existing.id,
        p_external_event_id,
        v_existing_tuple,
        v_incoming_tuple,
        p_payload
    )
    on conflict (transaction, incoming_external_event_id) do nothing
    returning * into v_conflict;

    if not found then
        select conflict.* into strict v_conflict
        from public.hotmart_purchase_semantic_conflicts conflict
        where conflict.transaction = v_transaction
          and conflict.incoming_external_event_id = p_external_event_id
        for update;

        if v_conflict.existing_event_id is distinct from v_existing.id
           or v_conflict.existing_semantic_tuple is distinct from v_existing_tuple
           or v_conflict.incoming_semantic_tuple is distinct from v_incoming_tuple
           or v_conflict.incoming_payload is distinct from p_payload then
            raise exception using
                errcode = '22000',
                message = 'purchase_semantic_conflict_replayed_differently';
        end if;
    end if;

    update public.webhook_events
    set processing_status = 'failed',
        processing_error = 'purchase_semantic_conflict',
        processed_at = clock_timestamp()
    where id = v_existing.id
      and processing_status in ('received', 'processing', 'failed');

    -- A replay remains idempotently semantic_conflict even after an operator
    -- resolves the original incident. It never reopens a resolved blocker.
    outcome := 'semantic_conflict';
    webhook_event_id := v_existing.id;
    return next;
end;
$function$;

create or replace function public.guard_purchase_semantic_conflict_request_start()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
begin
    if new.phase <> 'request_started'
       or (tg_op = 'UPDATE' and old.phase = 'request_started') then
        return new;
    end if;

    perform pg_advisory_xact_lock(7275726368617365);

    if exists (
        select 1
        from public.hotmart_purchase_semantic_conflicts conflict
        where conflict.resolved_at is null
    ) then
        raise exception using
            errcode = 'P0001',
            message = 'unresolved_purchase_semantic_conflict';
    end if;

    return new;
end;
$function$;

drop trigger if exists followup_attempts_guard_purchase_semantic_conflict
on public.followup_delivery_attempts;

create trigger followup_attempts_guard_purchase_semantic_conflict
before insert or update of phase on public.followup_delivery_attempts
for each row execute function public.guard_purchase_semantic_conflict_request_start();

revoke all on public.hotmart_purchase_semantic_conflicts from public;
revoke execute on function public.hotmart_purchase_semantic_tuple(jsonb) from public;
revoke execute on function public.hotmart_purchase_payload_is_processable(text, jsonb)
from public;
revoke execute on function public.admit_hotmart_purchase_approved(text, jsonb)
from public;
revoke execute on function public.guard_purchase_semantic_conflict_request_start()
from public;

do $privileges$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'revoke all on public.hotmart_purchase_semantic_conflicts from anon';
        execute 'revoke execute on function public.hotmart_purchase_semantic_tuple(jsonb) from anon';
        execute 'revoke execute on function public.hotmart_purchase_payload_is_processable(text, jsonb) from anon';
        execute 'revoke execute on function public.admit_hotmart_purchase_approved(text, jsonb) from anon';
        execute 'revoke execute on function public.guard_purchase_semantic_conflict_request_start() from anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'revoke all on public.hotmart_purchase_semantic_conflicts from authenticated';
        execute 'revoke execute on function public.hotmart_purchase_semantic_tuple(jsonb) from authenticated';
        execute 'revoke execute on function public.hotmart_purchase_payload_is_processable(text, jsonb) from authenticated';
        execute 'revoke execute on function public.admit_hotmart_purchase_approved(text, jsonb) from authenticated';
        execute 'revoke execute on function public.guard_purchase_semantic_conflict_request_start() from authenticated';
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'revoke all on public.hotmart_purchase_semantic_conflicts from service_role';
        execute 'grant select on public.hotmart_purchase_semantic_conflicts to service_role';
        execute 'revoke execute on function public.hotmart_purchase_semantic_tuple(jsonb) from service_role';
        execute 'revoke execute on function public.hotmart_purchase_payload_is_processable(text, jsonb) from service_role';
        execute 'revoke execute on function public.guard_purchase_semantic_conflict_request_start() from service_role';
        execute 'grant execute on function public.admit_hotmart_purchase_approved(text, jsonb) to service_role';
    end if;
end;
$privileges$;

commit;
