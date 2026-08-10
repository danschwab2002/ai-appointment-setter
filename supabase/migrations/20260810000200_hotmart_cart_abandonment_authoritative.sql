-- Authoritative semantic admission and canonical planning binding for
-- Hotmart PURCHASE_OUT_OF_SHOPPING_CART v2.0.0.

begin;

create table public.hotmart_cart_abandonment_semantic_conflicts (
    id uuid primary key default gen_random_uuid(),
    existing_event_id uuid not null
        references public.webhook_events(id) on delete restrict,
    incoming_external_event_id text not null,
    existing_semantic_tuple jsonb not null,
    incoming_semantic_tuple jsonb not null,
    incoming_payload jsonb not null,
    detected_at timestamptz not null default clock_timestamp(),
    resolved_at timestamptz,
    resolution text,
    unique (existing_event_id, incoming_external_event_id),
    check (
        (resolved_at is null and resolution is null)
        or (resolved_at is not null and nullif(btrim(resolution), '') is not null)
    )
);

create index hotmart_cart_abandonment_conflicts_unresolved_idx
on public.hotmart_cart_abandonment_semantic_conflicts (detected_at, id)
where resolved_at is null;

alter table public.hotmart_cart_abandonment_semantic_conflicts enable row level security;

create or replace function public.hotmart_cart_abandonment_payload_is_processable(
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
        and nullif(btrim(p_payload ->> 'id'), '') = p_external_event_id
        and p_payload ->> 'event' = 'PURCHASE_OUT_OF_SHOPPING_CART'
        and p_payload ->> 'version' = '2.0.0'
        and jsonb_typeof(p_payload -> 'creation_date') = 'number'
        and p_payload ->> 'creation_date' ~ '^[0-9]+$'
        and case
            when p_payload ->> 'creation_date' ~ '^[0-9]+$'
            then (p_payload ->> 'creation_date')::numeric > 0
                and (p_payload ->> 'creation_date')::numeric
                    <= 253402300799999::numeric
            else false
        end
        and jsonb_typeof(p_payload -> 'data') = 'object'
        and jsonb_typeof(p_payload #> '{data,buyer}') = 'object'
        and jsonb_typeof(p_payload #> '{data,product}') = 'object'
        and jsonb_typeof(p_payload #> '{data,offer}') = 'object'
        and jsonb_typeof(p_payload #> '{data,product,id}') = 'number'
        and p_payload #>> '{data,product,id}' ~ '^[0-9]+$'
        and (p_payload #>> '{data,product,id}')::numeric > 0
        and jsonb_typeof(p_payload #> '{data,product,name}') = 'string'
        and nullif(btrim(p_payload #>> '{data,product,name}'), '') is not null
        and jsonb_typeof(p_payload #> '{data,offer,code}') = 'string'
        and nullif(btrim(p_payload #>> '{data,offer,code}'), '') is not null
        and (
            (
                jsonb_typeof(p_payload #> '{data,buyer,email}') = 'string'
                and nullif(btrim(p_payload #>> '{data,buyer,email}'), '') is not null
            )
            or (
                jsonb_typeof(p_payload #> '{data,buyer,phone}') = 'string'
                and p_payload #>> '{data,buyer,phone}' ~ '^\+?[0-9 ()-]+$'
                and nullif(regexp_replace(
                    p_payload #>> '{data,buyer,phone}', '[^0-9]', '', 'g'
                ), '') is not null
            )
            or (
                jsonb_typeof(p_payload #> '{data,buyer,checkout_phone}') = 'string'
                and p_payload #>> '{data,buyer,checkout_phone}' ~ '^\+?[0-9 ()-]+$'
                and nullif(regexp_replace(
                    p_payload #>> '{data,buyer,checkout_phone}',
                    '[^0-9]', '', 'g'
                ), '') is not null
            )
        );
$function$;

create or replace function public.hotmart_cart_abandonment_semantic_tuple(
    p_payload jsonb
)
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
        'abandoned_at_ms', p_payload #>> '{creation_date}',
        'buyer_email', case
            when jsonb_typeof(p_payload #> '{data,buyer,email}') = 'string'
            then nullif(lower(btrim(p_payload #>> '{data,buyer,email}')), '')
            else null
        end,
        'buyer_phone', coalesce(
            case
                when jsonb_typeof(p_payload #> '{data,buyer,phone}') = 'string'
                 and p_payload #>> '{data,buyer,phone}' ~ '^\+?[0-9 ()-]+$'
                then nullif(regexp_replace(
                    p_payload #>> '{data,buyer,phone}', '[^0-9]', '', 'g'
                ), '')
                else null
            end,
            case
                when jsonb_typeof(p_payload #> '{data,buyer,checkout_phone}') = 'string'
                 and p_payload #>> '{data,buyer,checkout_phone}' ~ '^\+?[0-9 ()-]+$'
                then nullif(regexp_replace(
                    p_payload #>> '{data,buyer,checkout_phone}',
                    '[^0-9]', '', 'g'
                ), '')
                else null
            end
        ),
        'product_id', p_payload #>> '{data,product,id}',
        'product_name', nullif(btrim(p_payload #>> '{data,product,name}'), ''),
        'offer_code', nullif(btrim(p_payload #>> '{data,offer,code}'), '')
    );
$function$;

create or replace function public.admit_hotmart_cart_abandonment(
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
    v_existing public.webhook_events%rowtype;
    v_existing_tuple jsonb;
    v_incoming_tuple jsonb;
    v_conflict public.hotmart_cart_abandonment_semantic_conflicts%rowtype;
begin
    perform pg_advisory_xact_lock(7275726368617365);

    if p_external_event_id is null
       or btrim(p_external_event_id) = ''
       or p_payload is null
       or not coalesce(
           public.hotmart_cart_abandonment_payload_is_processable(
               p_external_event_id,
               p_payload
           ),
           false
       ) then
        raise exception using
            errcode = '22023',
            message = 'invalid_cart_abandonment_admission_input';
    end if;

    lock table public.webhook_events in share row exclusive mode;
    lock table public.hotmart_cart_abandonment_semantic_conflicts
        in share row exclusive mode;

    select event.* into v_existing
    from public.webhook_events event
    where event.source = 'hotmart'
      and event.external_event_id = p_external_event_id
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
            'PURCHASE_OUT_OF_SHOPPING_CART',
            p_payload,
            'received'
        )
        returning id into webhook_event_id;

        outcome := 'inserted';
        return next;
        return;
    end if;

    v_existing_tuple := public.hotmart_cart_abandonment_semantic_tuple(
        v_existing.payload
    );
    v_incoming_tuple := public.hotmart_cart_abandonment_semantic_tuple(p_payload);

    if v_existing.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
       and coalesce(
           public.hotmart_cart_abandonment_payload_is_processable(
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

    insert into public.hotmart_cart_abandonment_semantic_conflicts (
        existing_event_id,
        incoming_external_event_id,
        existing_semantic_tuple,
        incoming_semantic_tuple,
        incoming_payload
    ) values (
        v_existing.id,
        p_external_event_id,
        v_existing_tuple,
        v_incoming_tuple,
        p_payload
    )
    on conflict (existing_event_id, incoming_external_event_id) do nothing
    returning * into v_conflict;

    if not found then
        select conflict.* into strict v_conflict
        from public.hotmart_cart_abandonment_semantic_conflicts conflict
        where conflict.existing_event_id = v_existing.id
          and conflict.incoming_external_event_id = p_external_event_id
        for update;

        if v_conflict.existing_semantic_tuple is distinct from v_existing_tuple
           or v_conflict.incoming_semantic_tuple is distinct from v_incoming_tuple
           or v_conflict.incoming_payload is distinct from p_payload then
            raise exception using
                errcode = '22000',
                message = 'cart_abandonment_semantic_conflict_replayed_differently';
        end if;
    end if;

    update public.webhook_events
    set processing_status = 'failed',
        processing_error = 'cart_abandonment_semantic_conflict',
        processed_at = clock_timestamp()
    where id = v_existing.id
      and processing_status in ('received', 'processing', 'failed');

    outcome := 'semantic_conflict';
    webhook_event_id := v_existing.id;
    return next;
end;
$function$;

create or replace function public.validate_hotmart_cart_recovery_binding()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_event public.webhook_events%rowtype;
    v_case public.recovery_cases%rowtype;
    v_email_contact_count integer;
    v_email_case_matches boolean;
    v_phone_contact_count integer;
    v_phone_case_matches boolean;
    v_expected_phone text;
    v_expected_timestamp timestamptz;
begin
    if new.event_role <> 'cart_abandonment' then
        return new;
    end if;

    select * into strict v_event
    from public.webhook_events event
    where event.id = new.webhook_event_id
    for key share;

    select * into strict v_case
    from public.recovery_cases recovery_case
    where recovery_case.id = new.recovery_case_id
    for key share;

    if v_event.source <> 'hotmart'
       or v_event.event_type <> 'PURCHASE_OUT_OF_SHOPPING_CART' then
        raise exception using
            errcode = '23514',
            message = 'cart_abandonment_event_not_authoritative';
    end if;

    if not coalesce(
        public.hotmart_cart_abandonment_payload_is_processable(
            v_event.external_event_id,
            v_event.payload
        ),
        false
    ) then
        raise exception using
            errcode = '23514',
            message = 'cart_abandonment_event_not_processable';
    end if;

    v_expected_phone := coalesce(
        case
            when jsonb_typeof(v_event.payload #> '{data,buyer,phone}') = 'string'
             and v_event.payload #>> '{data,buyer,phone}' ~ '^\+?[0-9 ()-]+$'
            then nullif(regexp_replace(
                v_event.payload #>> '{data,buyer,phone}',
                '[^0-9]', '', 'g'
            ), '')
            else null
        end,
        case
            when jsonb_typeof(v_event.payload #> '{data,buyer,checkout_phone}') = 'string'
             and v_event.payload #>> '{data,buyer,checkout_phone}' ~ '^\+?[0-9 ()-]+$'
            then nullif(regexp_replace(
                v_event.payload #>> '{data,buyer,checkout_phone}',
                '[^0-9]', '', 'g'
            ), '')
            else null
        end
    );

    if v_case.external_product_id
       is distinct from v_event.payload #>> '{data,product,id}' then
        raise exception using
            errcode = '23514',
            message = 'cart_abandonment_product_mismatch';
    end if;

    if lower(btrim(v_case.product_name)) is distinct from lower(btrim(
        v_event.payload #>> '{data,product,name}'
    )) then
        raise exception using
            errcode = '23514',
            message = 'cart_abandonment_product_name_mismatch';
    end if;

    if v_case.offer_code is distinct from nullif(
        btrim(v_event.payload #>> '{data,offer,code}'),
        ''
    ) then
        raise exception using
            errcode = '23514',
            message = 'cart_abandonment_offer_mismatch';
    end if;

    v_expected_timestamp := to_timestamp(
        (v_event.payload ->> 'creation_date')::numeric / 1000
    );
    if new.observed_at is distinct from v_expected_timestamp then
        raise exception using
            errcode = '23514',
            message = 'cart_abandonment_timestamp_mismatch';
    end if;

    if nullif(lower(btrim(v_event.payload #>> '{data,buyer,email}')), '')
       is not null then
        select count(distinct point.contact_id)::integer,
               coalesce(bool_or(point.contact_id = v_case.contact_id), false)
          into v_email_contact_count, v_email_case_matches
        from public.contact_points point
        where point.type = 'email'
          and point.normalized_value = lower(btrim(
              v_event.payload #>> '{data,buyer,email}'
          ));

        if v_email_contact_count <> 1 or not v_email_case_matches then
            raise exception using
                errcode = '23514',
                message = 'cart_abandonment_contact_mismatch';
        end if;
    end if;

    if v_expected_phone is not null then
        select count(distinct point.contact_id)::integer,
               coalesce(bool_or(point.contact_id = v_case.contact_id), false)
          into v_phone_contact_count, v_phone_case_matches
        from public.contact_points point
        where point.type = 'phone'
          and point.normalized_value = v_expected_phone;

        if v_phone_contact_count <> 1 or not v_phone_case_matches then
            raise exception using
                errcode = '23514',
                message = 'cart_abandonment_contact_mismatch';
        end if;
    end if;

    return new;
end;
$function$;

drop trigger if exists recovery_case_events_validate_hotmart_abandonment
on public.recovery_case_events;

create trigger recovery_case_events_validate_hotmart_abandonment
before insert or update of recovery_case_id, webhook_event_id, event_role, observed_at
on public.recovery_case_events
for each row execute function public.validate_hotmart_cart_recovery_binding();

create or replace function public.protect_hotmart_cart_recovery_event()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
begin
    if old.event_role = 'cart_abandonment' then
        raise exception using
            errcode = '23514',
            message = 'cart_abandonment_binding_immutable';
    end if;
    if tg_op = 'DELETE' then
        return old;
    end if;
    return new;
end;
$function$;

drop trigger if exists recovery_case_events_protect_hotmart_abandonment_mutation
on public.recovery_case_events;

create trigger recovery_case_events_protect_hotmart_abandonment_mutation
before update or delete on public.recovery_case_events
for each row execute function public.protect_hotmart_cart_recovery_event();

create or replace function public.protect_hotmart_cart_recovery_binding()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
begin
    if old.abandonment_event_id is not null and (
        new.contact_id is distinct from old.contact_id
        or new.abandonment_event_id is distinct from old.abandonment_event_id
        or new.source is distinct from old.source
        or new.external_product_id is distinct from old.external_product_id
        or new.product_name is distinct from old.product_name
        or new.offer_code is distinct from old.offer_code
    ) then
        raise exception using
            errcode = '23514',
            message = 'cart_abandonment_binding_immutable';
    end if;
    return new;
end;
$function$;

drop trigger if exists recovery_cases_protect_hotmart_cart_binding
on public.recovery_cases;

create trigger recovery_cases_protect_hotmart_cart_binding
before update of contact_id, abandonment_event_id, source,
                 external_product_id, product_name, offer_code
on public.recovery_cases
for each row execute function public.protect_hotmart_cart_recovery_binding();

create or replace function public.protect_hotmart_cart_source_event()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
begin
    if tg_op = 'DELETE' then
        if old.source = 'hotmart'
           and old.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART' then
            raise exception using
                errcode = '23514',
                message = 'cart_abandonment_source_immutable';
        end if;
        return old;
    end if;

    if (
        (old.source = 'hotmart'
         and old.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART')
        or (new.source = 'hotmart'
            and new.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART')
    ) and (
        new.source is distinct from old.source
        or new.external_event_id is distinct from old.external_event_id
        or new.event_type is distinct from old.event_type
        or new.payload is distinct from old.payload
    ) then
        raise exception using
            errcode = '23514',
            message = 'cart_abandonment_source_immutable';
    end if;
    return new;
end;
$function$;

drop trigger if exists webhook_events_protect_hotmart_cart_source_update
on public.webhook_events;
create trigger webhook_events_protect_hotmart_cart_source_update
before update of source, external_event_id, event_type, payload
on public.webhook_events
for each row execute function public.protect_hotmart_cart_source_event();

drop trigger if exists webhook_events_protect_hotmart_cart_source_delete
on public.webhook_events;
create trigger webhook_events_protect_hotmart_cart_source_delete
before delete on public.webhook_events
for each row execute function public.protect_hotmart_cart_source_event();

create or replace function public.guard_cart_abandonment_semantic_conflict_request_start()
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
        from public.hotmart_cart_abandonment_semantic_conflicts conflict
        where conflict.resolved_at is null
    ) then
        raise exception using
            errcode = 'P0001',
            message = 'unresolved_cart_abandonment_semantic_conflict';
    end if;

    return new;
end;
$function$;

drop trigger if exists followup_attempts_guard_cart_abandonment_conflict
on public.followup_delivery_attempts;

create trigger followup_attempts_guard_cart_abandonment_conflict
before insert or update of phase on public.followup_delivery_attempts
for each row execute function public.guard_cart_abandonment_semantic_conflict_request_start();

revoke all on public.hotmart_cart_abandonment_semantic_conflicts from public;
revoke execute on function public.hotmart_cart_abandonment_payload_is_processable(text, jsonb)
from public;
revoke execute on function public.hotmart_cart_abandonment_semantic_tuple(jsonb)
from public;
revoke execute on function public.admit_hotmart_cart_abandonment(text, jsonb)
from public;
revoke execute on function public.validate_hotmart_cart_recovery_binding()
from public;
revoke execute on function public.guard_cart_abandonment_semantic_conflict_request_start()
from public;
revoke execute on function public.protect_hotmart_cart_recovery_binding()
from public;
revoke execute on function public.protect_hotmart_cart_recovery_event()
from public;
revoke execute on function public.protect_hotmart_cart_source_event()
from public;

do $privileges$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'revoke all on public.hotmart_cart_abandonment_semantic_conflicts from anon';
        execute 'revoke execute on function public.hotmart_cart_abandonment_payload_is_processable(text, jsonb) from anon';
        execute 'revoke execute on function public.hotmart_cart_abandonment_semantic_tuple(jsonb) from anon';
        execute 'revoke execute on function public.admit_hotmart_cart_abandonment(text, jsonb) from anon';
        execute 'revoke execute on function public.validate_hotmart_cart_recovery_binding() from anon';
        execute 'revoke execute on function public.guard_cart_abandonment_semantic_conflict_request_start() from anon';
        execute 'revoke execute on function public.protect_hotmart_cart_recovery_binding() from anon';
        execute 'revoke execute on function public.protect_hotmart_cart_recovery_event() from anon';
        execute 'revoke execute on function public.protect_hotmart_cart_source_event() from anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'revoke all on public.hotmart_cart_abandonment_semantic_conflicts from authenticated';
        execute 'revoke execute on function public.hotmart_cart_abandonment_payload_is_processable(text, jsonb) from authenticated';
        execute 'revoke execute on function public.hotmart_cart_abandonment_semantic_tuple(jsonb) from authenticated';
        execute 'revoke execute on function public.admit_hotmart_cart_abandonment(text, jsonb) from authenticated';
        execute 'revoke execute on function public.validate_hotmart_cart_recovery_binding() from authenticated';
        execute 'revoke execute on function public.guard_cart_abandonment_semantic_conflict_request_start() from authenticated';
        execute 'revoke execute on function public.protect_hotmart_cart_recovery_binding() from authenticated';
        execute 'revoke execute on function public.protect_hotmart_cart_recovery_event() from authenticated';
        execute 'revoke execute on function public.protect_hotmart_cart_source_event() from authenticated';
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'revoke all on public.hotmart_cart_abandonment_semantic_conflicts from service_role';
        execute 'grant select on public.hotmart_cart_abandonment_semantic_conflicts to service_role';
        execute 'revoke execute on function public.hotmart_cart_abandonment_payload_is_processable(text, jsonb) from service_role';
        execute 'revoke execute on function public.hotmart_cart_abandonment_semantic_tuple(jsonb) from service_role';
        execute 'revoke execute on function public.validate_hotmart_cart_recovery_binding() from service_role';
        execute 'revoke execute on function public.guard_cart_abandonment_semantic_conflict_request_start() from service_role';
        execute 'revoke execute on function public.protect_hotmart_cart_recovery_binding() from service_role';
        execute 'revoke execute on function public.protect_hotmart_cart_recovery_event() from service_role';
        execute 'revoke execute on function public.protect_hotmart_cart_source_event() from service_role';
        execute 'grant execute on function public.admit_hotmart_cart_abandonment(text, jsonb) to service_role';
    end if;
end;
$privileges$;

commit;
