-- Migration: el cupon personal del carrito abandonado (A1 de ATT1).
--
-- Por que. El primer mensaje del carrito (la plantilla A1) va a llevar un cupon
-- PERSONAL del 10 % que vence de verdad a las 24 h. El cupon lo crea un worker
-- de Python contra la API de Hotmart (otra pieza). Un cupon recien creado tarda
-- ~5,5 min en aplicar en el checkout: por eso se siembra ~15 min (p_lead) antes
-- de que A1 tenga que salir, y A1 se atrasa (hasta un tope, del lado del
-- emisor) si el cupon todavia no esta `valid`. Decision de negocio: como mucho
-- DOS codigos por caso, el de A1 (ordinal 1) y una unica renovacion (ordinal
-- 2). El tope lo impone la tabla, no el worker.
--
-- Que crea:
-- - la tabla recovery_coupons, una fila por codigo, con las columnas de
--   identidad inmutables (caso, ordinal, codigo, producto, descuento y el
--   vencimiento REAL prometido) y un estado que no vuelve de un terminal;
-- - claim_due_recovery_coupons_v1: siembra el ordinal 1 de los carritos cuyo
--   first_contact_review sale dentro de p_lead, vence lo que dejo de ser
--   elegible y reclama con lease lo que hay que crear o verificar;
-- - settle_recovery_coupon_v1: el resultado de cada intento, solo con el lease
--   vigente, con las transiciones permitidas e idempotente;
-- - get_recovery_coupon_for_case_v1: la lectura que usa el emisor de A1.
--
-- Lo que NO hace, a proposito:
-- - no siembra el ordinal 2 (la renovacion la decide otra pieza);
-- - no marca `redeemed` (lo hara la compra, con su webhook_event);
-- - no atrasa A1: el emisor lee el estado con get_recovery_coupon_for_case_v1;
-- - no recalcula expires_at si el due_at de la accion cambia despues de
--   sembrar: el vencimiento prometido queda fijo una vez escrito.
--
-- Elegibilidad del caso (la misma en la siembra y en el reclamo):
-- recovery_cases.status en grace_period/active, sin purchase_event_id, y el
-- contacto con contact_permission fuera de opted_out/blocked/restricted y
-- lifecycle_status distinto de do_not_contact (lo que deja la baja durable de
-- 20260809000100 y lo que miran las admisiones portables de 20261001000400).
--
-- El codigo: upper(p_code_prefix) + los primeros 12 hex de gen_random_uuid()
-- en mayusculas (48 bits aleatorios: la version del uuid es el caracter 13).
-- Un choque de codigo no aborta la corrida: la fila no se siembra y la proxima
-- corrida la vuelve a intentar.
--
-- request_started: el settle a request_started CONSERVA el lease (el worker
-- esta por llamar a Hotmart). Si el lease vence sin otro settle, el reclamo lo
-- pasa a create_unknown (failure_code lease_expired_after_request_started) y lo
-- vuelve a ofrecer: no se sabe si Hotmart lo creo.

begin;

create table public.recovery_coupons (
    id uuid primary key default gen_random_uuid(),
    recovery_case_id uuid not null
        references public.recovery_cases(id) on delete restrict,
    ordinal smallint not null check (ordinal in (1, 2)),
    code text not null unique check (code ~ '^[A-Z0-9]{8,25}$'),
    external_product_id text not null
        check (length(btrim(external_product_id)) > 0),
    discount numeric(4,3) not null check (discount > 0 and discount < 0.99),
    expires_at timestamptz not null,
    hotmart_start_ms bigint,
    hotmart_end_ms bigint,
    hotmart_coupon_id bigint,
    status text not null default 'reserved' check (status in (
        'reserved', 'request_started', 'created', 'valid',
        'create_failed', 'create_unknown', 'redeemed', 'expired'
    )),
    failure_code text,
    attempt_count integer not null default 0 check (attempt_count >= 0),
    lease_owner text,
    lease_expires_at timestamptz,
    next_attempt_at timestamptz,
    verified_at timestamptz,
    redeemed_webhook_event_id uuid references public.webhook_events(id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (recovery_case_id, ordinal),
    constraint recovery_coupons_lease_pair_check check (
        (lease_owner is null) = (lease_expires_at is null)
    ),
    constraint recovery_coupons_hotmart_window_check check (
        status not in ('created', 'valid', 'redeemed')
        or (hotmart_start_ms is not null
            and hotmart_end_ms is not null
            and hotmart_end_ms > hotmart_start_ms)
    ),
    constraint recovery_coupons_valid_verified_check check (
        status <> 'valid' or verified_at is not null
    ),
    constraint recovery_coupons_failed_has_code_check check (
        status <> 'create_failed' or failure_code is not null
    ),
    constraint recovery_coupons_redeemed_event_check check (
        status <> 'redeemed' or redeemed_webhook_event_id is not null
    )
);

create index recovery_coupons_claim_idx
on public.recovery_coupons (coalesce(next_attempt_at, created_at))
where status in ('reserved', 'request_started', 'created', 'create_unknown');

alter table public.recovery_coupons enable row level security;

create function public.protect_recovery_coupon()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op = 'DELETE' then
        raise exception using errcode = '55000', message = 'recovery_coupon_append_only';
    end if;
    if old.id is distinct from new.id
       or old.recovery_case_id is distinct from new.recovery_case_id
       or old.ordinal is distinct from new.ordinal
       or old.code is distinct from new.code
       or old.external_product_id is distinct from new.external_product_id
       or old.discount is distinct from new.discount
       or old.expires_at is distinct from new.expires_at
       or old.created_at is distinct from new.created_at then
        raise exception using errcode = '55000', message = 'recovery_coupon_identity_immutable';
    end if;
    if old.status in ('redeemed', 'expired', 'create_failed')
       and new.status is distinct from old.status then
        raise exception using errcode = '55000', message = 'recovery_coupon_terminal_status';
    end if;
    return new;
end;
$function$;

create trigger recovery_coupon_immutable
before update or delete on public.recovery_coupons
for each row execute function public.protect_recovery_coupon();

create function public.claim_due_recovery_coupons_v1(
    p_now timestamptz,
    p_worker text,
    p_lease_seconds integer,
    p_code_prefix text,
    p_lead interval,
    p_validity interval,
    p_discount numeric,
    p_limit integer
)
returns table (
    coupon_id uuid,
    recovery_case_id uuid,
    code text,
    status text,
    external_product_id text,
    expires_at timestamptz,
    attempt_count integer
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_prefix text := upper(btrim(coalesce(p_code_prefix, '')));
begin
    -- recovery_coupons: validar la entrada
    if p_now is null
       or nullif(btrim(coalesce(p_worker, '')), '') is null
       or p_lease_seconds is null or p_lease_seconds < 1 or p_lease_seconds > 3600
       or v_prefix !~ '^[A-Z0-9]{2,8}$'
       or p_lead is null or p_lead < interval '0'
       or p_validity is null or p_validity <= interval '0'
       or p_discount is null or p_discount <= 0 or p_discount >= 0.99
       or p_limit is null or p_limit < 1 or p_limit > 500 then
        raise exception using errcode = '22023', message = 'invalid_recovery_coupon_claim_input';
    end if;

    -- recovery_coupons: sembrar el ordinal 1 de los carritos cuyo A1 sale
    -- dentro de p_lead.
    insert into public.recovery_coupons as coupon (
        recovery_case_id, ordinal, code, external_product_id, discount,
        expires_at, status, created_at, updated_at
    )
    select due.case_id,
           1,
           v_prefix || upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 12)),
           due.product_id,
           p_discount,
           due.due_at + p_validity,
           'reserved',
           p_now,
           p_now
    from (
        select distinct on (rc.id)
               rc.id as case_id,
               rc.external_product_id as product_id,
               sa.due_at
        from public.recovery_cases rc
        join public.contacts ct on ct.id = rc.contact_id
        join public.scheduled_actions sa on sa.recovery_case_id = rc.id
        where rc.status in ('grace_period', 'active')
          and rc.purchase_event_id is null
          and ct.contact_permission not in ('opted_out', 'blocked', 'restricted')
          and ct.lifecycle_status <> 'do_not_contact'
          and sa.action_type = 'first_contact_review'
          and sa.anchor_type = 'cart_abandonment'
          and sa.status in ('pending', 'deferred', 'retryable_failed')
          and sa.due_at - p_lead <= p_now
          and sa.expires_at > p_now
          and sa.due_at + p_validity > p_now
          and not exists (
              select 1 from public.recovery_coupons existing
              where existing.recovery_case_id = rc.id
                and existing.ordinal = 1
          )
        order by rc.id, sa.due_at
    ) due
    order by due.due_at
    limit p_limit
    on conflict do nothing;

    -- recovery_coupons: vencer lo que no llego a crearse y cuyo caso dejo de
    -- ser elegible (compra, baja, caso cerrado).
    update public.recovery_coupons coupon
    set status = 'expired',
        failure_code = 'case_no_longer_eligible',
        lease_owner = null,
        lease_expires_at = null,
        next_attempt_at = null,
        updated_at = p_now
    from public.recovery_cases rc
    join public.contacts ct on ct.id = rc.contact_id
    where rc.id = coupon.recovery_case_id
      and coupon.status in ('reserved', 'create_unknown', 'request_started')
      and (coupon.lease_expires_at is null or coupon.lease_expires_at <= p_now)
      and not (
          rc.status in ('grace_period', 'active')
          and rc.purchase_event_id is null
          and ct.contact_permission not in ('opted_out', 'blocked', 'restricted')
          and ct.lifecycle_status <> 'do_not_contact'
      );

    -- recovery_coupons: un request_started cuyo lease vencio no se sabe si
    -- existe en Hotmart; pasa a create_unknown y se vuelve a ofrecer.
    update public.recovery_coupons coupon
    set status = 'create_unknown',
        failure_code = 'lease_expired_after_request_started',
        lease_owner = null,
        lease_expires_at = null,
        next_attempt_at = null,
        updated_at = p_now
    where coupon.status = 'request_started'
      and coupon.lease_expires_at <= p_now;

    -- recovery_coupons: reclamar con lease lo que hay que crear o verificar.
    return query
    with picked as (
        select coupon.id
        from public.recovery_coupons coupon
        join public.recovery_cases rc on rc.id = coupon.recovery_case_id
        join public.contacts ct on ct.id = rc.contact_id
        where coupon.status in ('reserved', 'create_unknown', 'created')
          and (coupon.lease_expires_at is null or coupon.lease_expires_at <= p_now)
          and (coupon.next_attempt_at is null or coupon.next_attempt_at <= p_now)
          and rc.status in ('grace_period', 'active')
          and rc.purchase_event_id is null
          and ct.contact_permission not in ('opted_out', 'blocked', 'restricted')
          and ct.lifecycle_status <> 'do_not_contact'
        order by coalesce(coupon.next_attempt_at, coupon.created_at), coupon.id
        limit p_limit
        for update of coupon skip locked
    ), claimed as (
        update public.recovery_coupons coupon
        set lease_owner = p_worker,
            lease_expires_at = p_now + make_interval(secs => p_lease_seconds),
            attempt_count = coupon.attempt_count + 1,
            updated_at = p_now
        from picked
        where coupon.id = picked.id
        returning coupon.id, coupon.recovery_case_id, coupon.code, coupon.status,
                  coupon.external_product_id, coupon.expires_at, coupon.attempt_count
    )
    select claimed.id, claimed.recovery_case_id, claimed.code, claimed.status,
           claimed.external_product_id, claimed.expires_at, claimed.attempt_count
    from claimed
    order by claimed.expires_at, claimed.id;
end;
$function$;

create function public.settle_recovery_coupon_v1(
    p_coupon_id uuid,
    p_worker text,
    p_status text,
    p_hotmart_coupon_id bigint,
    p_hotmart_start_ms bigint,
    p_hotmart_end_ms bigint,
    p_failure_code text,
    p_next_attempt_at timestamptz,
    p_now timestamptz
)
returns table (
    coupon_id uuid,
    status text,
    applied boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_row public.recovery_coupons%rowtype;
    v_start bigint;
    v_end bigint;
    v_hotmart_id bigint;
begin
    -- recovery_coupons: validar la entrada
    if p_coupon_id is null
       or nullif(btrim(coalesce(p_worker, '')), '') is null
       or p_now is null
       or p_status is null
       or p_status not in (
           'request_started', 'created', 'valid', 'create_unknown', 'create_failed'
       )
       or (p_status = 'create_failed'
           and nullif(btrim(coalesce(p_failure_code, '')), '') is null) then
        raise exception using errcode = '22023', message = 'invalid_recovery_coupon_settle_input';
    end if;

    select * into v_row
    from public.recovery_coupons coupon
    where coupon.id = p_coupon_id
    for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'recovery_coupon_not_found';
    end if;

    v_start := coalesce(p_hotmart_start_ms, v_row.hotmart_start_ms);
    v_end := coalesce(p_hotmart_end_ms, v_row.hotmart_end_ms);
    v_hotmart_id := coalesce(p_hotmart_coupon_id, v_row.hotmart_coupon_id);

    -- recovery_coupons: sin el lease vigente, solo se acepta la repeticion del
    -- settle que ya quedo aplicado (applied = false).
    if v_row.lease_owner is distinct from p_worker
       or v_row.lease_expires_at is null
       or v_row.lease_expires_at <= p_now then
        if v_row.status = p_status
           and v_row.hotmart_start_ms is not distinct from v_start
           and v_row.hotmart_end_ms is not distinct from v_end
           and v_row.hotmart_coupon_id is not distinct from v_hotmart_id then
            return query select v_row.id, v_row.status, false;
            return;
        end if;
        raise exception using errcode = '55000', message = 'recovery_coupon_lease_not_held';
    end if;

    if not (
        (v_row.status in ('reserved', 'create_unknown')
            and p_status in ('request_started', 'created', 'create_unknown', 'create_failed'))
        or (v_row.status = 'request_started'
            and p_status in ('created', 'create_unknown', 'create_failed'))
        or (v_row.status = 'created'
            and p_status in ('valid', 'created', 'create_failed'))
    ) then
        raise exception using
            errcode = '55000',
            message = 'recovery_coupon_invalid_transition',
            detail = v_row.status || '->' || p_status;
    end if;

    if p_status in ('created', 'valid')
       and (v_start is null or v_end is null or v_end <= v_start) then
        raise exception using errcode = '22023', message = 'recovery_coupon_hotmart_window_required';
    end if;

    update public.recovery_coupons coupon
    set status = p_status,
        hotmart_start_ms = v_start,
        hotmart_end_ms = v_end,
        hotmart_coupon_id = v_hotmart_id,
        failure_code = case
            when p_status in ('created', 'valid', 'request_started') then null
            else p_failure_code
        end,
        next_attempt_at = case
            when p_status in ('created', 'create_unknown') then p_next_attempt_at
            else null
        end,
        verified_at = case when p_status = 'valid' then p_now else coupon.verified_at end,
        -- request_started conserva el lease: el worker sigue con la llamada.
        lease_owner = case when p_status = 'request_started' then coupon.lease_owner end,
        lease_expires_at = case when p_status = 'request_started' then coupon.lease_expires_at end,
        updated_at = p_now
    where coupon.id = v_row.id;

    return query select v_row.id, p_status, true;
end;
$function$;

create function public.get_recovery_coupon_for_case_v1(
    p_recovery_case_id uuid,
    p_ordinal smallint
)
returns table (
    coupon_id uuid,
    code text,
    status text,
    expires_at timestamptz,
    verified_at timestamptz
)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    -- recovery_coupons: la lectura del emisor de A1
    select coupon.id, coupon.code, coupon.status, coupon.expires_at, coupon.verified_at
    from public.recovery_coupons coupon
    where coupon.recovery_case_id = p_recovery_case_id
      and coupon.ordinal = p_ordinal;
$function$;

revoke all on table public.recovery_coupons from public;
revoke all on function public.protect_recovery_coupon() from public;
revoke all on function public.claim_due_recovery_coupons_v1(timestamptz, text, integer, text, interval, interval, numeric, integer) from public;
revoke all on function public.settle_recovery_coupon_v1(uuid, text, text, bigint, bigint, bigint, text, timestamptz, timestamptz) from public;
revoke all on function public.get_recovery_coupon_for_case_v1(uuid, smallint) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'revoke all on table public.recovery_coupons from anon';
        execute 'revoke all on function public.protect_recovery_coupon() from anon';
        execute 'revoke all on function public.claim_due_recovery_coupons_v1(timestamptz, text, integer, text, interval, interval, numeric, integer) from anon';
        execute 'revoke all on function public.settle_recovery_coupon_v1(uuid, text, text, bigint, bigint, bigint, text, timestamptz, timestamptz) from anon';
        execute 'revoke all on function public.get_recovery_coupon_for_case_v1(uuid, smallint) from anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'revoke all on table public.recovery_coupons from authenticated';
        execute 'revoke all on function public.protect_recovery_coupon() from authenticated';
        execute 'revoke all on function public.claim_due_recovery_coupons_v1(timestamptz, text, integer, text, interval, interval, numeric, integer) from authenticated';
        execute 'revoke all on function public.settle_recovery_coupon_v1(uuid, text, text, bigint, bigint, bigint, text, timestamptz, timestamptz) from authenticated';
        execute 'revoke all on function public.get_recovery_coupon_for_case_v1(uuid, smallint) from authenticated';
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'revoke all on table public.recovery_coupons from service_role';
        execute 'revoke all on function public.protect_recovery_coupon() from service_role';
        execute 'grant execute on function public.claim_due_recovery_coupons_v1(timestamptz, text, integer, text, interval, interval, numeric, integer) to service_role';
        execute 'grant execute on function public.settle_recovery_coupon_v1(uuid, text, text, bigint, bigint, bigint, text, timestamptz, timestamptz) to service_role';
        execute 'grant execute on function public.get_recovery_coupon_for_case_v1(uuid, smallint) to service_role';
    end if;
end;
$roles$;

commit;
