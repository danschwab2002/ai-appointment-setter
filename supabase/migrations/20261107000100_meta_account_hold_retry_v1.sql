-- meta-account-hold-retry-v1: un rechazo de la cuenta no gasta el toque (E-348,
-- P-33 del plan del recuperador; diseno E-345; decisiones de Dan del
-- 2026-11-07, D1 a D6). Contrato: docs/contracts/meta-delivery-rejections-v1.md
-- (seccion «Cuando la cuenta no entrega»).
--
-- Por que. El 04/11 Maribel tuvo un problema de pago con Meta y desde las
-- 15:00Z Meta rechazo todas las plantillas con 131042 («Business eligibility
-- payment issue»). El bridge las daba por entregadas (accepted_by_chatwoot) y los
-- casos de un toque se cerraron en sequence_exhausted: 21 contactos perdidos.
-- Desde E-284 (migracion 20261103000500) el barrido los registra en
-- meta_delivery_rejections, pero solo el 131049 tiene reintento.
--
-- Que crea (sin tocar tablas, filas ni funciones existentes).
-- 1. meta_account_holds: una fila por primer contacto rechazado por la cuenta
--    (el codigo lo decide el bridge con META_ACCOUNT_HOLD_CODES, default 131042;
--    la base solo excluye el 131049 y el 131026, que tienen su propio camino).
--    Estados: waiting (la cuenta sigue caida), released (la cuenta volvio a
--    entregar una plantilla), claimed, sent, failed, blocked y expired.
-- 2. meta_account_hold_activations y mark_meta_account_hold_enabled_v1: la
--    marca de prendido por bandeja, la primera vez que el barrido corre con el
--    flag (no se mueve despues). hold_meta_account_rejection_v1 pone en
--    espera solo un rechazo cuyo mensaje es posterior a esa marca: nada
--    retroactivo (D4), aunque el registro y la espera se prendan juntos y el
--    primer barrido registre los rechazos de las ultimas 72 h. El bridge lo
--    llama con recorded y con replayed: si una llamada fallo, el barrido
--    siguiente la repite.
-- 3. release_meta_account_holds_v1: la senal pasiva de «la cuenta volvio» (D5):
--    el bridge pasa la hora de la ultima plantilla nuestra entregada o leida en
--    la bandeja; si es posterior al ultimo rechazo con un codigo de la cuenta
--    (de cualquier tipo de toque, el reintento incluido), las filas en espera
--    pasan a released. Antes vence lo que paso el vencimiento (D2, 72 h por
--    defecto desde el mensaje original).
-- 4. list_due_meta_account_hold_retries_v1, claim_meta_account_hold_retry_v1
--    (las mismas barreras de la base que el reintento del 131049: conversacion
--    tomada o cerrada, derivacion sin atender, contacto sin permiso, baja,
--    compra, caso cerrado por otra cosa), settle_meta_account_hold_retry_v1 y
--    block_meta_account_hold_retry_v1 (lo que vio el bridge en Chatwoot).
-- 5. requeue_meta_account_hold_v1: el reintento volvio rechazado con un codigo
--    de la cuenta: la cuenta sigue caida y la fila vuelve a esperar, hasta 3
--    intentos por fila. Rechazado por otro codigo, la fila queda sent: se
--    registra y se cierra ahi.
--
-- Lo que NO hace: la sonda sin trafico (D5, cada 2 h) no esta: la sonda es
-- mandarle el mensaje real a la persona mas vieja de la cola, y queda pendiente
-- de decision. Sin senal pasiva, la fila vence. Un bridge anterior no llama a
-- nada de esto; el bridge la usa solo con META_ACCOUNT_HOLD_RETRY_ENABLED,
-- apagado por defecto.
--
-- Errores: 22023 una entrada invalida; 55000 correrla dos veces (la tabla ya
-- existe) o sin la migracion 20261103000500.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $guard$
begin
    if to_regclass('public.meta_delivery_rejections') is null then
        raise exception using errcode = '55000',
            message = 'meta_account_holds_requires_meta_delivery_rejections';
    end if;
    if to_regclass('public.meta_account_holds') is not null then
        raise exception using errcode = '55000',
            message = 'meta_account_holds_already_applied';
    end if;
end
$guard$;

-- 1. La tabla.

create table public.meta_account_holds (
    rejection_id uuid primary key
        references public.meta_delivery_rejections(id) on delete restrict,
    chatwoot_inbox_id bigint not null check (chatwoot_inbox_id > 0),
    chatwoot_message_id bigint not null check (chatwoot_message_id > 0),
    external_conversation_id bigint not null check (external_conversation_id > 0),
    meta_error_code text not null
        check (meta_error_code ~ '^[0-9]{3,6}$'
               and meta_error_code not in ('131049', '131026')),
    message_created_at timestamptz not null,
    expires_at timestamptz not null,
    status text not null default 'waiting' check (status in (
        'waiting', 'released', 'claimed', 'sent', 'failed', 'blocked', 'expired')),
    attempts integer not null default 0 check (attempts between 0 and 3),
    reason text check (reason is null or reason ~ '^[a-z0-9_]{1,64}$'),
    held_at timestamptz not null,
    released_at timestamptz,
    release_signal_at timestamptz,
    claimed_at timestamptz,
    settled_at timestamptz,
    retry_message_id bigint check (retry_message_id is null or retry_message_id > 0),
    -- Los ultimos 9 digitos del telefono, desde la primera reserva: para no
    -- escribirle dos veces a la misma persona desde dos conversaciones.
    phone_tail text check (phone_tail is null or phone_tail ~ '^[0-9]{1,9}$'),
    updated_at timestamptz not null,
    unique (chatwoot_inbox_id, chatwoot_message_id),
    check (expires_at > message_created_at),
    check (
        (status = 'waiting' and reason is null and retry_message_id is null
         and released_at is null and claimed_at is null)
        or (status = 'released' and reason is null and retry_message_id is null
            and released_at is not null and release_signal_at is not null
            and claimed_at is null)
        or (status = 'claimed' and reason is null and retry_message_id is null
            and claimed_at is not null and attempts >= 1)
        or (status = 'sent' and reason is null and retry_message_id is not null
            and claimed_at is not null and settled_at is not null and attempts >= 1)
        or (status = 'failed' and reason is not null and retry_message_id is null
            and settled_at is not null)
        or (status = 'blocked' and reason is not null and retry_message_id is null
            and settled_at is not null)
        or (status = 'expired' and reason is null and retry_message_id is null
            and settled_at is not null)
    )
);

create index meta_account_holds_open_idx
on public.meta_account_holds (chatwoot_inbox_id, message_created_at)
where status in ('waiting', 'released');

alter table public.meta_account_holds enable row level security;

create table public.meta_account_hold_activations (
    chatwoot_inbox_id bigint primary key check (chatwoot_inbox_id > 0),
    enabled_at timestamptz not null
);

alter table public.meta_account_hold_activations enable row level security;

-- 1b. La marca de prendido (idempotente: la primera gana).

create function public.mark_meta_account_hold_enabled_v1(
    p_chatwoot_inbox_id bigint,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    enabled_at timestamptz
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0 then
        raise exception using errcode = '22023',
            message = 'mark_meta_account_hold_enabled_invalid_input';
    end if;
    insert into public.meta_account_hold_activations (chatwoot_inbox_id, enabled_at)
    values (p_chatwoot_inbox_id, coalesce(p_now, clock_timestamp()))
    on conflict (chatwoot_inbox_id) do nothing;
    outcome := case when found then 'marked' else 'replayed' end;
    select activation.enabled_at into enabled_at
    from public.meta_account_hold_activations activation
    where activation.chatwoot_inbox_id = p_chatwoot_inbox_id;
    return next;
end;
$function$;

-- 2. Poner en espera un rechazo recien registrado.

create function public.hold_meta_account_rejection_v1(
    p_rejection_id uuid,
    p_max_age_seconds integer,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    expires_at timestamptz
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_rejection public.meta_delivery_rejections%rowtype;
    v_now timestamptz;
    v_expires timestamptz;
    v_enabled_at timestamptz;
begin
    if p_rejection_id is null
       or p_max_age_seconds is null or p_max_age_seconds not between 3600 and 604800 then
        raise exception using errcode = '22023',
            message = 'hold_meta_account_rejection_invalid_input';
    end if;
    v_now := coalesce(p_now, clock_timestamp());

    select rejection.* into v_rejection
    from public.meta_delivery_rejections rejection
    where rejection.id = p_rejection_id
    for update;
    if v_rejection.id is null then
        outcome := 'not_found';
        return next;
        return;
    end if;
    if exists (select 1 from public.meta_account_holds hold
               where hold.rejection_id = v_rejection.id) then
        select hold.expires_at into expires_at
        from public.meta_account_holds hold where hold.rejection_id = v_rejection.id;
        outcome := 'replayed';
        return next;
        return;
    end if;
    -- Solo el primer contacto (D3: la lista de plantillas de un mensaje la
    -- aplica el bridge), con un codigo que no sea de la persona ni del
    -- 131049, y sin el reintento del 131049 en curso.
    if v_rejection.touch_kind <> 'first_touch'
       or v_rejection.meta_error_code is null
       or v_rejection.meta_error_code in ('131049', '131026')
       or v_rejection.retry_status <> 'none' then
        outcome := 'not_eligible';
        return next;
        return;
    end if;
    -- D4: solo lo que Meta rechazo despues de prender la espera en la bandeja.
    select activation.enabled_at into v_enabled_at
    from public.meta_account_hold_activations activation
    where activation.chatwoot_inbox_id = v_rejection.chatwoot_inbox_id;
    if v_enabled_at is null then
        outcome := 'not_enabled';
        return next;
        return;
    end if;
    if v_rejection.message_created_at < v_enabled_at then
        outcome := 'before_enabled';
        return next;
        return;
    end if;
    v_expires := v_rejection.message_created_at + make_interval(secs => p_max_age_seconds);
    if v_expires <= v_now then
        outcome := 'too_late';
        expires_at := v_expires;
        return next;
        return;
    end if;

    insert into public.meta_account_holds (
        rejection_id, chatwoot_inbox_id, chatwoot_message_id,
        external_conversation_id, meta_error_code, message_created_at,
        expires_at, held_at, updated_at
    ) values (
        v_rejection.id, v_rejection.chatwoot_inbox_id, v_rejection.chatwoot_message_id,
        v_rejection.external_conversation_id, v_rejection.meta_error_code,
        v_rejection.message_created_at, v_expires, v_now, v_now
    );
    outcome := 'held';
    expires_at := v_expires;
    return next;
end;
$function$;

-- 3. La senal pasiva: la cuenta volvio a entregar una plantilla.

create function public.release_meta_account_holds_v1(
    p_chatwoot_inbox_id bigint,
    p_codes text[],
    p_account_ok_at timestamptz,
    p_now timestamptz default clock_timestamp()
)
returns table (
    released integer,
    expired integer,
    waiting integer,
    held_back integer,
    last_rejection_at timestamptz
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_now timestamptz;
    v_code text;
begin
    if p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_codes is null or cardinality(p_codes) not between 1 and 10 then
        raise exception using errcode = '22023',
            message = 'release_meta_account_holds_invalid_input';
    end if;
    foreach v_code in array p_codes loop
        if v_code is null or v_code !~ '^[0-9]{3,6}$' or v_code in ('131049', '131026') then
            raise exception using errcode = '22023',
                message = 'release_meta_account_holds_invalid_input';
        end if;
    end loop;
    v_now := coalesce(p_now, clock_timestamp());

    -- Lo que paso el vencimiento no sale nunca.
    with gone as (
        update public.meta_account_holds hold
        set status = 'expired',
            released_at = null,
            release_signal_at = null,
            settled_at = v_now,
            updated_at = v_now
        where hold.chatwoot_inbox_id = p_chatwoot_inbox_id
          and hold.status in ('waiting', 'released')
          and hold.expires_at <= v_now
        returning 1
    )
    select count(*)::integer into expired from gone;

    -- El ultimo rechazo de la cuenta en la bandeja, de cualquier tipo de toque
    -- (un reintento rechazado otra vez tambien dice que la cuenta sigue caida).
    select max(rejection.message_created_at) into last_rejection_at
    from public.meta_delivery_rejections rejection
    where rejection.chatwoot_inbox_id = p_chatwoot_inbox_id
      and rejection.meta_error_code = any(p_codes);

    -- La cuenta volvio a caer despues de liberar (un reintento volvio con un
    -- codigo de la cuenta): lo liberado con una senal anterior a ese rechazo
    -- vuelve a esperar, sin gastar intentos.
    held_back := 0;
    if last_rejection_at is not null then
        with back as (
            update public.meta_account_holds hold
            set status = 'waiting',
                released_at = null,
                release_signal_at = null,
                updated_at = v_now
            where hold.chatwoot_inbox_id = p_chatwoot_inbox_id
              and hold.status = 'released'
              and hold.release_signal_at < last_rejection_at
            returning 1
        )
        select count(*)::integer into held_back from back;
    end if;

    released := 0;
    if p_account_ok_at is not null and last_rejection_at is not null
       and p_account_ok_at > last_rejection_at and p_account_ok_at <= v_now then
        with freed as (
            update public.meta_account_holds hold
            set status = 'released',
                released_at = v_now,
                release_signal_at = p_account_ok_at,
                updated_at = v_now
            where hold.chatwoot_inbox_id = p_chatwoot_inbox_id
              and hold.status = 'waiting'
              and hold.meta_error_code = any(p_codes)
              and hold.message_created_at < p_account_ok_at
            returning 1
        )
        select count(*)::integer into released from freed;
    end if;

    select count(*)::integer into waiting
    from public.meta_account_holds hold
    where hold.chatwoot_inbox_id = p_chatwoot_inbox_id
      and hold.status = 'waiting';
    return next;
end;
$function$;

-- 4. Los que ya pueden salir.

create function public.list_due_meta_account_hold_retries_v1(
    p_chatwoot_inbox_id bigint,
    p_limit integer,
    p_now timestamptz default clock_timestamp()
)
returns table (
    rejection_id uuid,
    external_conversation_id bigint,
    chatwoot_message_id bigint,
    message_created_at timestamptz,
    attempts integer
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_now timestamptz;
begin
    if p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_limit is null or p_limit not between 1 and 50 then
        raise exception using errcode = '22023',
            message = 'list_due_meta_account_hold_retries_invalid_input';
    end if;
    v_now := coalesce(p_now, clock_timestamp());
    return query
    select hold.rejection_id, hold.external_conversation_id,
           hold.chatwoot_message_id, hold.message_created_at, hold.attempts
    from public.meta_account_holds hold
    where hold.chatwoot_inbox_id = p_chatwoot_inbox_id
      and hold.status = 'released'
      and hold.expires_at > v_now
    order by hold.message_created_at, hold.chatwoot_message_id
    limit p_limit;
end;
$function$;

-- 5. Reservar el reintento: las barreras del reintento del 131049.

create function public.claim_meta_account_hold_retry_v1(
    p_chatwoot_inbox_id bigint,
    p_chatwoot_message_id bigint,
    p_external_user_id text,
    p_contact_email text,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    rejection_id uuid,
    attempts integer
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_hold public.meta_account_holds%rowtype;
    v_rejection public.meta_delivery_rejections%rowtype;
    v_conversation public.conversations%rowtype;
    v_contact public.contacts%rowtype;
    v_case_status text;
    v_email text;
    v_phone_tail text;
    v_now timestamptz;
    v_block text;
begin
    if p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_chatwoot_message_id is null or p_chatwoot_message_id <= 0
       or p_external_user_id is null
       or p_external_user_id !~ '^[1-9][0-9]{7,14}$'
       or (nullif(btrim(p_contact_email), '') is not null
           and (length(btrim(p_contact_email)) > 320
                or btrim(p_contact_email) !~ '^[^@\s]+@[^@\s]+$')) then
        raise exception using errcode = '22023',
            message = 'claim_meta_account_hold_retry_invalid_input';
    end if;
    v_now := coalesce(p_now, clock_timestamp());
    v_email := nullif(lower(btrim(p_contact_email)), '');
    v_phone_tail := right(p_external_user_id, 9);

    select hold.* into v_hold
    from public.meta_account_holds hold
    where hold.chatwoot_inbox_id = p_chatwoot_inbox_id
      and hold.chatwoot_message_id = p_chatwoot_message_id
    for update;
    if v_hold.rejection_id is null then
        outcome := 'not_found';
        return next;
        return;
    end if;
    rejection_id := v_hold.rejection_id;
    attempts := v_hold.attempts;
    -- Solo lo que la senal libero: en espera, la cuenta sigue caida.
    if v_hold.status <> 'released' then
        outcome := 'not_released';
        return next;
        return;
    end if;
    if v_hold.expires_at <= v_now then
        update public.meta_account_holds hold
        set status = 'expired', released_at = null, release_signal_at = null,
            settled_at = v_now, updated_at = v_now
        where hold.rejection_id = v_hold.rejection_id;
        outcome := 'blocked_too_late';
        return next;
        return;
    end if;

    -- La misma guarda que la liberacion, por si la cuenta volvio a caer entre
    -- la liberacion y esta reserva: un rechazo con el mismo codigo posterior a
    -- la senal devuelve la fila a la espera, sin gastar el intento.
    if exists (
        select 1 from public.meta_delivery_rejections rejection
        where rejection.chatwoot_inbox_id = p_chatwoot_inbox_id
          and rejection.meta_error_code = v_hold.meta_error_code
          and rejection.message_created_at > v_hold.release_signal_at
    ) then
        update public.meta_account_holds hold
        set status = 'waiting', released_at = null, release_signal_at = null,
            updated_at = v_now
        where hold.rejection_id = v_hold.rejection_id;
        outcome := 'account_down_again';
        return next;
        return;
    end if;

    select rejection.* into v_rejection
    from public.meta_delivery_rejections rejection
    where rejection.id = v_hold.rejection_id;

    v_block := null;
    select conversation.* into v_conversation
    from public.conversations conversation
    where conversation.commercial_context = jsonb_build_object(
        'chatwoot_conversation_id', v_hold.external_conversation_id::text
    )
    limit 1;
    if v_conversation.id is not null then
        if v_conversation.human_takeover
           or v_conversation.status in (
                'snoozed', 'paused_human', 'completed', 'closed', 'blocked') then
            v_block := 'blocked_conversation';
        elsif exists (
            select 1
            from public.human_handoff_requests request
            join public.commercial_cases commercial_case
              on commercial_case.id = request.commercial_case_id
            where commercial_case.conversation_id = v_conversation.id
              and request.status in ('requested', 'projected', 'projection_failed')
              and request.attended_at is null
        ) then
            v_block := 'blocked_pending_handoff';
        else
            select contact.* into v_contact
            from public.contacts contact
            where contact.id = v_conversation.contact_id;
            if v_contact.id is not null
               and (v_contact.contact_permission in ('opted_out', 'blocked', 'restricted')
                    or v_contact.lifecycle_status = 'do_not_contact') then
                v_block := 'blocked_contact';
            end if;
        end if;
    end if;

    if v_block is null and exists (
        select 1
        from public.contact_opt_out_events opt_out
        where opt_out.canonical_inbox_id = p_chatwoot_inbox_id
          and (opt_out.canonical_conversation_id = v_hold.external_conversation_id
               or right(opt_out.external_user_id, 9) = v_phone_tail)
    ) then
        v_block := 'blocked_opt_out';
    end if;

    if v_block is null and (
        exists (
            select 1 from public.purchase_intents intent
            where intent.lifecycle_state = 'purchased'
              and (right(intent.normalized_phone, 9) = v_phone_tail
                   or (v_email is not null and intent.normalized_email = v_email))
        ) or exists (
            select 1
            from public.hotmart_purchase_intent_event_identities identity
            join public.webhook_events event on event.id = identity.webhook_event_id
            where event.event_type in ('PURCHASE_APPROVED', 'PURCHASE_COMPLETE')
              and (right(identity.normalized_phone, 9) = v_phone_tail
                   or (v_email is not null and identity.normalized_email = v_email))
        ) or exists (
            select 1
            from public.webhook_events event
            where event.source = 'hotmart'
              and event.event_type in ('PURCHASE_APPROVED', 'PURCHASE_COMPLETE')
              and event.received_at >= v_hold.message_created_at - interval '30 days'
              and (right(regexp_replace(coalesce(
                       event.payload #>> '{data,buyer,checkout_phone}',
                       event.payload #>> '{data,buyer,phone}',
                       ''), '[^0-9]', '', 'g'), 9) = v_phone_tail
                   or (v_email is not null
                       and lower(btrim(event.payload #>> '{data,buyer,email}')) = v_email))
        )
    ) then
        v_block := 'purchase_already_approved';
    end if;

    -- La misma persona en otra conversacion: otra fila suya ya reservada o
    -- mandada (dos primeros contactos rechazados al mismo telefono).
    if v_block is null and exists (
        select 1 from public.meta_account_holds other
        where other.chatwoot_inbox_id = p_chatwoot_inbox_id
          and other.rejection_id <> v_hold.rejection_id
          and other.phone_tail = v_phone_tail
          and other.status in ('claimed', 'sent')
    ) then
        v_block := 'blocked_same_person';
    end if;

    -- Un contacto nuestro posterior al mismo telefono que Chatwoot acepto (otro
    -- primer contacto, otro flujo): ya le llego algo despues, este no sale.
    if v_block is null and exists (
        select 1
        from public.followup_delivery_attempts attempt
        join public.scheduled_actions action on action.id = attempt.action_id
        join public.recovery_cases recovery_case on recovery_case.id = action.recovery_case_id
        join public.contacts contact on contact.id = recovery_case.contact_id
        where attempt.outcome = 'accepted_by_chatwoot'
          and attempt.accepted_at > v_hold.message_created_at
          and attempt.remote_message_id is distinct from v_hold.chatwoot_message_id::text
          and right(regexp_replace(coalesce(contact.phone, ''), '[^0-9]', '', 'g'), 9) = v_phone_tail
          and not exists (
              select 1 from public.meta_delivery_rejections again
              where again.chatwoot_inbox_id = p_chatwoot_inbox_id
                and again.chatwoot_message_id::text = attempt.remote_message_id
          )
    ) then
        v_block := 'blocked_newer_contact';
    end if;

    if v_block is null and v_rejection.recovery_case_id is not null then
        select recovery_case.status into v_case_status
        from public.recovery_cases recovery_case
        where recovery_case.id = v_rejection.recovery_case_id;
        if v_case_status in (
            'won', 'lost', 'cancelled', 'unreachable', 'error', 'expired', 'escalated') then
            v_block := 'blocked_case_closed';
        end if;
    end if;

    if v_block is not null then
        update public.meta_account_holds hold
        set status = 'blocked', reason = v_block, released_at = null,
            release_signal_at = null, settled_at = v_now, updated_at = v_now,
            phone_tail = coalesce(hold.phone_tail, v_phone_tail)
        where hold.rejection_id = v_hold.rejection_id;
        outcome := v_block;
        return next;
        return;
    end if;

    update public.meta_account_holds hold
    set status = 'claimed',
        attempts = hold.attempts + 1,
        phone_tail = coalesce(hold.phone_tail, v_phone_tail),
        claimed_at = v_now,
        settled_at = null,
        updated_at = v_now
    where hold.rejection_id = v_hold.rejection_id
    returning hold.attempts into attempts;
    outcome := 'claimed';
    return next;
end;
$function$;

-- 6. Cerrar la reserva con lo que contesto Chatwoot.

create function public.settle_meta_account_hold_retry_v1(
    p_rejection_id uuid,
    p_status text,
    p_retry_message_id bigint default null,
    p_failure_reason text default null,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_now timestamptz;
begin
    if p_rejection_id is null
       or p_status is null or p_status not in ('sent', 'failed')
       or (p_status = 'sent' and (p_retry_message_id is null or p_retry_message_id <= 0
                                  or p_failure_reason is not null))
       or (p_status = 'failed' and (p_retry_message_id is not null
                                    or p_failure_reason is null
                                    or p_failure_reason !~ '^[a-z0-9_]{1,64}$')) then
        raise exception using errcode = '22023',
            message = 'settle_meta_account_hold_retry_invalid_input';
    end if;
    v_now := coalesce(p_now, clock_timestamp());
    update public.meta_account_holds hold
    set status = p_status,
        retry_message_id = case when p_status = 'sent' then p_retry_message_id end,
        reason = case when p_status = 'failed' then p_failure_reason end,
        released_at = case when p_status = 'sent' then hold.released_at end,
        release_signal_at = case when p_status = 'sent' then hold.release_signal_at end,
        settled_at = v_now,
        updated_at = v_now
    where hold.rejection_id = p_rejection_id
      and hold.status = 'claimed';
    outcome := case when found then 'settled' else 'not_found' end;
    return next;
end;
$function$;

-- 7. Frenar por lo que vio el bridge en Chatwoot (bridge_<motivo>).

create function public.block_meta_account_hold_retry_v1(
    p_rejection_id uuid,
    p_reason text,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_now timestamptz;
begin
    if p_rejection_id is null
       or p_reason is null or p_reason !~ '^bridge_[a-z0-9_]{1,57}$' then
        raise exception using errcode = '22023',
            message = 'block_meta_account_hold_retry_invalid_input';
    end if;
    v_now := coalesce(p_now, clock_timestamp());
    update public.meta_account_holds hold
    set status = 'blocked', reason = p_reason, released_at = null,
        release_signal_at = null, settled_at = v_now, updated_at = v_now
    where hold.rejection_id = p_rejection_id
      and hold.status in ('waiting', 'released');
    outcome := case when found then 'blocked' else 'not_found' end;
    return next;
end;
$function$;

-- 8. El reintento volvio rechazado por la cuenta: la fila vuelve a esperar.

create function public.requeue_meta_account_hold_v1(
    p_chatwoot_inbox_id bigint,
    p_original_message_id bigint,
    p_retry_message_id bigint,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_hold public.meta_account_holds%rowtype;
    v_now timestamptz;
begin
    if p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_original_message_id is null or p_original_message_id <= 0
       or p_retry_message_id is null or p_retry_message_id <= 0 then
        raise exception using errcode = '22023',
            message = 'requeue_meta_account_hold_invalid_input';
    end if;
    v_now := coalesce(p_now, clock_timestamp());
    select hold.* into v_hold
    from public.meta_account_holds hold
    where hold.chatwoot_inbox_id = p_chatwoot_inbox_id
      and hold.chatwoot_message_id = p_original_message_id
      and hold.status = 'sent'
      and hold.retry_message_id = p_retry_message_id
    for update;
    if v_hold.rejection_id is null then
        outcome := 'not_found';
        return next;
        return;
    end if;
    if v_hold.attempts >= 3 then
        update public.meta_account_holds hold
        set status = 'blocked', reason = 'hold_attempts_exhausted',
            retry_message_id = null, released_at = null, release_signal_at = null,
            settled_at = v_now, updated_at = v_now
        where hold.rejection_id = v_hold.rejection_id;
        outcome := 'exhausted';
    elsif v_hold.expires_at <= v_now then
        update public.meta_account_holds hold
        set status = 'expired', retry_message_id = null, released_at = null,
            release_signal_at = null, settled_at = v_now, updated_at = v_now
        where hold.rejection_id = v_hold.rejection_id;
        outcome := 'expired';
    else
        update public.meta_account_holds hold
        set status = 'waiting', retry_message_id = null, released_at = null,
            release_signal_at = null, claimed_at = null, settled_at = null,
            updated_at = v_now
        where hold.rejection_id = v_hold.rejection_id;
        outcome := 'requeued';
    end if;
    return next;
end;
$function$;

-- Permisos: como 20261103000500, todo se revoca y solo service_role (el
-- bridge) ejecuta las ocho funciones. Las tablas no tienen permisos para ningun
-- rol de la API: las lee el OS por psql de solo lectura.
do $acl$
declare
    v_function text;
    v_role text;
    v_functions text[] := array[
        'public.mark_meta_account_hold_enabled_v1(bigint,timestamptz)',
        'public.hold_meta_account_rejection_v1(uuid,integer,timestamptz)',
        'public.release_meta_account_holds_v1(bigint,text[],timestamptz,timestamptz)',
        'public.list_due_meta_account_hold_retries_v1(bigint,integer,timestamptz)',
        'public.claim_meta_account_hold_retry_v1(bigint,bigint,text,text,timestamptz)',
        'public.settle_meta_account_hold_retry_v1(uuid,text,bigint,text,timestamptz)',
        'public.block_meta_account_hold_retry_v1(uuid,text,timestamptz)',
        'public.requeue_meta_account_hold_v1(bigint,bigint,bigint,timestamptz)'
    ];
begin
    revoke all on table public.meta_account_holds from public;
    revoke all on table public.meta_account_hold_activations from public;
    foreach v_function in array v_functions loop
        execute format('revoke all on function %s from public', v_function);
        foreach v_role in array array['anon', 'authenticated'] loop
            if to_regrole(v_role) is not null then
                execute format('revoke all on function %s from %I', v_function, v_role);
            end if;
        end loop;
        if to_regrole('service_role') is not null then
            execute format('grant execute on function %s to service_role', v_function);
        end if;
    end loop;
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format('revoke all on table public.meta_account_holds from %I', v_role);
            execute format('revoke all on table public.meta_account_hold_activations from %I', v_role);
        end if;
    end loop;

    -- Por presencia: service_role ejecuta las ocho y ningun rol de la API.
    foreach v_function in array v_functions loop
        if exists (select 1 from pg_roles where rolname = 'service_role'
                      and not has_function_privilege(rolname, v_function, 'EXECUTE'))
           or exists (select 1 from pg_roles where rolname in ('anon', 'authenticated')
                         and has_function_privilege(rolname, v_function, 'EXECUTE')) then
            raise exception using errcode = '55000',
                message = 'meta_account_holds_acl_unexpected', detail = v_function;
        end if;
    end loop;
    if exists (select 1 from pg_roles where rolname in ('anon', 'authenticated', 'service_role')
                  and (has_table_privilege(rolname, 'public.meta_account_holds', 'SELECT')
                       or has_table_privilege(rolname, 'public.meta_account_hold_activations', 'SELECT'))) then
        raise exception using errcode = '55000',
            message = 'meta_account_holds_acl_unexpected', detail = 'tabla';
    end if;
end
$acl$;

commit;
