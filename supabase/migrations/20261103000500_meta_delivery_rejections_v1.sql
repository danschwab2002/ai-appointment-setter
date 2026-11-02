-- meta-delivery-rejections-v1: el rechazo de Meta deja de quedar como entregado
-- y el primer contacto con 131049 tiene UN reintento diferido (E-284, P-12a del
-- plan del 2026-11-02; investigacion: reporte E-282 del programa; decision D1 de
-- Dan del 2026-11-02: un solo reintento a las 24 h o mas, solo el primer
-- contacto, nunca en rafaga). Contrato: docs/contracts/meta-delivery-rejections-v1.md.
--
-- Por que. Chatwoot acepta la plantilla (el intento queda accepted_by_chatwoot y
-- el caso de un toque se cierra en sequence_exhausted) y despues Meta la rechaza:
-- Chatwoot marca el mensaje como failed y guarda el codigo en
-- content_attributes.external_error («131049: ...»). La base no se enteraba. En
-- 30 dias (medido el 02/11 por el lector del OS) ATT1 tuvo 69 mensajes con
-- 131049, 54 de ellos el primer mensaje al numero, y Maribel 7.
--
-- Que crea.
-- 1. meta_delivery_rejections: una fila por mensaje nuestro que Meta rechazo,
--    por (inbox, mensaje de Chatwoot). Guarda el tipo de toque, el codigo de
--    Meta (solo digitos, nunca el texto), la plantilla si se pudo leer, la hora
--    del mensaje y, si la base lo conoce, el intento (followup_delivery_attempts
--    por remote_message_id), su caso y la conversacion. Y el estado del unico
--    reintento: none, claimed, sent, failed o blocked con su motivo.
-- 2. record_meta_delivery_rejection_v1: registrar (idempotente: el mismo
--    mensaje devuelve replayed y no cambia nada, salvo completar la plantilla si
--    antes no se habia podido leer).
-- 3. list_due_meta_first_touch_retries_v1: los primeros contactos con 131049
--    sin reintento, de entre 24 h y el techo que pasa el bridge (hasta 7 dias).
-- 4. claim_meta_first_touch_retry_v1: reservar el reintento con las barreras de
--    la base (ya reintentado, antes de 24 h, despues del techo, conversacion
--    tomada o cerrada, derivacion sin atender, baja, compra, caso cerrado). Un
--    freno de la base deja la fila en blocked con el motivo: no se vuelve a
--    evaluar. Antes de 24 h no escribe nada.
-- 5. settle_meta_first_touch_retry_v1: cerrar la reserva con lo que contesto
--    Chatwoot (sent con el mensaje nuevo, o failed con el motivo);
--    block_meta_first_touch_retry_v1: frenarlo por lo que vio el bridge en
--    Chatwoot (bridge_<motivo>); mark_meta_delivery_rejection_notified_v1:
--    marcar el aviso al equipo de un 131026.
-- 6. 131026 (decision de Dan del 02/11, punto 5): con p_close_unreachable, el
--    registro cierra el caso como unreachable y cancela sus toques pendientes
--    sin lease (si el caso ya termino no lo toca), y pide el aviso al equipo.
-- 7. meta_delivery_rejection_counts_v1: la vista para contar por plantilla,
--    tipo de toque y codigo, con lo que paso con el reintento. Sin permisos
--    para la API: la lee el OS por psql de solo lectura.
--
-- Solo 131049 se reintenta (Meta, «Per-user marketing template limits»: esperar
-- al menos 24 h y no reintentar en rafaga). 131026, 130472 y el resto solo se
-- registran. El reintento tampoco se reintenta: su propio rechazo entra como
-- first_touch_retry y list_due no lo devuelve nunca.
--
-- Lo que NO hace: no toca tablas, filas ni funciones existentes; el intento y el
-- caso quedan como estaban (accepted_by_chatwoot y su estado), y la fila nueva
-- los referencia. Un bridge anterior no llama a nada de esto. El bridge la usa
-- solo con META_DELIVERY_REJECTIONS_ENABLED (registrar) y
-- META_FIRST_TOUCH_RETRY_ENABLED (reintentar), apagadas por defecto.
--
-- Errores: 22023 una entrada invalida; 55000 correrla dos veces (la tabla ya
-- existe).

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $guard$
begin
    if to_regclass('public.meta_delivery_rejections') is not null then
        raise exception using errcode = '55000',
            message = 'meta_delivery_rejections_already_applied';
    end if;
end
$guard$;

-- 1. La tabla.

create table public.meta_delivery_rejections (
    id uuid primary key default gen_random_uuid(),
    chatwoot_account_id bigint not null check (chatwoot_account_id > 0),
    chatwoot_inbox_id bigint not null check (chatwoot_inbox_id > 0),
    external_conversation_id bigint not null check (external_conversation_id > 0),
    chatwoot_message_id bigint not null check (chatwoot_message_id > 0),
    touch_kind text not null check (touch_kind in (
        'first_touch', 'first_touch_retry', 'purchase_welcome',
        'followup', 'reactivation', 'coupon_followup'
    )),
    meta_error_code text check (meta_error_code ~ '^[0-9]{3,6}$'),
    template_name text
        check (template_name is null or length(btrim(template_name)) between 1 and 512),
    message_created_at timestamptz not null,
    observed_at timestamptz not null,
    retry_of_message_id bigint
        check (retry_of_message_id is null or retry_of_message_id > 0),
    delivery_attempt_id uuid
        references public.followup_delivery_attempts(id) on delete restrict,
    recovery_case_id uuid
        references public.recovery_cases(id) on delete restrict,
    conversation_id uuid
        references public.conversations(id) on delete restrict,
    retry_status text not null default 'none'
        check (retry_status in ('none', 'claimed', 'sent', 'failed', 'blocked')),
    retry_reason text
        check (retry_reason is null or retry_reason ~ '^[a-z0-9_]{1,64}$'),
    retry_claimed_at timestamptz,
    retry_settled_at timestamptz,
    retry_message_id bigint check (retry_message_id is null or retry_message_id > 0),
    -- 131026 (D de Dan del 02/11, punto 5): el numero no recibe WhatsApp. Lo
    -- que hizo la base con el caso y cuando se aviso al equipo.
    unreachable_outcome text check (unreachable_outcome in (
        'case_closed', 'case_already_closed', 'no_case')),
    team_notified_at timestamptz,
    created_at timestamptz not null default clock_timestamp(),
    check (unreachable_outcome is null or meta_error_code = '131026'),
    check (team_notified_at is null or meta_error_code = '131026'),
    unique (chatwoot_inbox_id, chatwoot_message_id),
    check ((touch_kind = 'first_touch_retry') = (retry_of_message_id is not null)),
    -- Solo el primer contacto con 131049 puede tener reintento.
    check (retry_status = 'none'
           or (touch_kind = 'first_touch' and meta_error_code = '131049')),
    check (
        (retry_status = 'none' and retry_reason is null and retry_claimed_at is null
         and retry_settled_at is null and retry_message_id is null)
        or (retry_status = 'claimed' and retry_reason is null
            and retry_claimed_at is not null and retry_settled_at is null
            and retry_message_id is null)
        or (retry_status = 'sent' and retry_reason is null
            and retry_claimed_at is not null and retry_settled_at is not null
            and retry_message_id is not null)
        or (retry_status = 'failed' and retry_reason is not null
            and retry_claimed_at is not null and retry_settled_at is not null
            and retry_message_id is null)
        or (retry_status = 'blocked' and retry_reason is not null
            and retry_settled_at is not null and retry_message_id is null)
    )
);

create index meta_delivery_rejections_due_idx
on public.meta_delivery_rejections (chatwoot_inbox_id, message_created_at)
where touch_kind = 'first_touch'
  and meta_error_code = '131049'
  and retry_status = 'none';

alter table public.meta_delivery_rejections enable row level security;

-- 2. Registrar un rechazo.

create function public.record_meta_delivery_rejection_v1(
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_external_conversation_id bigint,
    p_chatwoot_message_id bigint,
    p_touch_kind text,
    p_meta_error_code text,
    p_template_name text,
    p_message_created_at timestamptz,
    p_retry_of_message_id bigint default null,
    p_close_unreachable boolean default false,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    rejection_id uuid,
    retry_status text,
    unreachable_outcome text,
    needs_team_notice boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_existing public.meta_delivery_rejections%rowtype;
    v_attempt_id uuid;
    v_case_id uuid;
    v_conversation_id uuid;
    v_attempt_count integer;
    v_id uuid;
    v_now timestamptz;
    v_unreachable text;
    v_closed integer;
begin
    if p_chatwoot_account_id is null or p_chatwoot_account_id <= 0
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_external_conversation_id is null or p_external_conversation_id <= 0
       or p_chatwoot_message_id is null or p_chatwoot_message_id <= 0
       or p_touch_kind is null
       or p_touch_kind not in (
           'first_touch', 'first_touch_retry', 'purchase_welcome',
           'followup', 'reactivation', 'coupon_followup')
       or (p_meta_error_code is not null and p_meta_error_code !~ '^[0-9]{3,6}$')
       or (p_template_name is not null
           and length(btrim(p_template_name)) not between 1 and 512)
       or p_message_created_at is null
       or ((p_touch_kind = 'first_touch_retry') <> (p_retry_of_message_id is not null))
       or (p_retry_of_message_id is not null and p_retry_of_message_id <= 0)
       or p_close_unreachable is null then
        raise exception using errcode = '22023',
            message = 'record_meta_delivery_rejection_invalid_input';
    end if;
    v_now := coalesce(p_now, clock_timestamp());

    select rejection.* into v_existing
    from public.meta_delivery_rejections rejection
    where rejection.chatwoot_inbox_id = p_chatwoot_inbox_id
      and rejection.chatwoot_message_id = p_chatwoot_message_id
    for update;
    if v_existing.id is not null then
        -- La plantilla se lee de la conversacion solo mientras el mensaje es el
        -- ultimo: si un barrido anterior no la vio, este la completa.
        if v_existing.template_name is null and p_template_name is not null then
            update public.meta_delivery_rejections rejection
            set template_name = btrim(p_template_name)
            where rejection.id = v_existing.id;
        end if;
        outcome := 'replayed';
        rejection_id := v_existing.id;
        retry_status := v_existing.retry_status;
        unreachable_outcome := v_existing.unreachable_outcome;
        -- Un aviso que no llego a marcarse se vuelve a pedir (el conector lo
        -- deduplica por su id).
        needs_team_notice := p_close_unreachable
            and v_existing.meta_error_code = '131026'
            and v_existing.team_notified_at is null;
        return next;
        return;
    end if;

    -- El intento que la base conoce de ese mensaje (el dispatcher guarda el id
    -- de Chatwoot en remote_message_id). Con dos, ninguno: no se adivina.
    select count(*) into v_attempt_count
    from public.followup_delivery_attempts attempt
    where attempt.remote_message_id = p_chatwoot_message_id::text;
    if v_attempt_count = 1 then
        select attempt.id, action.recovery_case_id
          into v_attempt_id, v_case_id
        from public.followup_delivery_attempts attempt
        join public.scheduled_actions action on action.id = attempt.action_id
        where attempt.remote_message_id = p_chatwoot_message_id::text;
    end if;

    select conversation.id into v_conversation_id
    from public.conversations conversation
    where conversation.commercial_context = jsonb_build_object(
        'chatwoot_conversation_id', p_external_conversation_id::text
    )
    limit 1;

    -- 131026 con el cierre prendido: el numero no recibe WhatsApp, el caso se
    -- cierra como unreachable y sus toques pendientes se cancelan (los que no
    -- estan en vuelo: sin lease). Un caso que ya termino (sequence_exhausted,
    -- won, ...) no se toca. Nunca se reintenta.
    v_unreachable := null;
    if p_close_unreachable and p_meta_error_code = '131026' then
        if v_case_id is null then
            v_unreachable := 'no_case';
        else
            update public.recovery_cases recovery_case
            set status = 'unreachable',
                closed_at = v_now,
                next_contact_at = null,
                next_contact_reason = 'meta_131026_unreachable',
                version = recovery_case.version + 1
            where recovery_case.id = v_case_id
              and recovery_case.status in ('grace_period', 'active', 'paused');
            get diagnostics v_closed = row_count;
            if v_closed = 1 then
                v_unreachable := 'case_closed';
                update public.scheduled_actions action
                set status = 'cancelled',
                    terminal_reason = 'meta_131026_unreachable',
                    next_attempt_at = null
                where action.recovery_case_id = v_case_id
                  and action.status in ('pending', 'deferred', 'retryable_failed')
                  and action.lease_owner is null;
                update public.followup_sequences sequence
                set status = 'cancelled',
                    cancelled_at = v_now,
                    cancel_reason = 'meta_131026_unreachable',
                    revision = sequence.revision + 1
                where sequence.recovery_case_id = v_case_id
                  and sequence.status in ('active', 'paused');
            else
                v_unreachable := 'case_already_closed';
            end if;
        end if;
    end if;

    insert into public.meta_delivery_rejections (
        chatwoot_account_id, chatwoot_inbox_id, external_conversation_id,
        chatwoot_message_id, touch_kind, meta_error_code, template_name,
        message_created_at, observed_at, retry_of_message_id,
        delivery_attempt_id, recovery_case_id, conversation_id,
        unreachable_outcome
    ) values (
        p_chatwoot_account_id, p_chatwoot_inbox_id, p_external_conversation_id,
        p_chatwoot_message_id, p_touch_kind, p_meta_error_code,
        nullif(btrim(p_template_name), ''), p_message_created_at, v_now,
        p_retry_of_message_id, v_attempt_id, v_case_id, v_conversation_id,
        v_unreachable
    )
    returning id into v_id;

    outcome := 'recorded';
    rejection_id := v_id;
    retry_status := 'none';
    unreachable_outcome := v_unreachable;
    needs_team_notice := p_close_unreachable and p_meta_error_code = '131026';
    return next;
end;
$function$;

-- 3. Los reintentos que tocan.

create function public.list_due_meta_first_touch_retries_v1(
    p_chatwoot_inbox_id bigint,
    p_max_age_seconds integer,
    p_limit integer,
    p_now timestamptz default clock_timestamp()
)
returns table (
    rejection_id uuid,
    external_conversation_id bigint,
    chatwoot_message_id bigint,
    message_created_at timestamptz
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
       or p_max_age_seconds is null or p_max_age_seconds not between 86400 and 604800
       or p_limit is null or p_limit not between 1 and 50 then
        raise exception using errcode = '22023',
            message = 'list_due_meta_first_touch_retries_invalid_input';
    end if;
    v_now := coalesce(p_now, clock_timestamp());
    return query
    select rejection.id, rejection.external_conversation_id,
           rejection.chatwoot_message_id, rejection.message_created_at
    from public.meta_delivery_rejections rejection
    where rejection.chatwoot_inbox_id = p_chatwoot_inbox_id
      and rejection.touch_kind = 'first_touch'
      and rejection.meta_error_code = '131049'
      and rejection.retry_status = 'none'
      and rejection.message_created_at <= v_now - interval '24 hours'
      and rejection.message_created_at >= v_now - make_interval(secs => p_max_age_seconds)
    order by rejection.message_created_at, rejection.chatwoot_message_id
    limit p_limit;
end;
$function$;

-- 4. Reservar el reintento.

create function public.claim_meta_first_touch_retry_v1(
    p_chatwoot_inbox_id bigint,
    p_chatwoot_message_id bigint,
    p_external_user_id text,
    p_contact_email text,
    p_max_age_seconds integer,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    rejection_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
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
                or btrim(p_contact_email) !~ '^[^@\s]+@[^@\s]+$'))
       or p_max_age_seconds is null or p_max_age_seconds not between 86400 and 604800 then
        raise exception using errcode = '22023',
            message = 'claim_meta_first_touch_retry_invalid_input';
    end if;
    v_now := coalesce(p_now, clock_timestamp());
    v_email := nullif(lower(btrim(p_contact_email)), '');
    v_phone_tail := right(p_external_user_id, 9);

    select rejection.* into v_rejection
    from public.meta_delivery_rejections rejection
    where rejection.chatwoot_inbox_id = p_chatwoot_inbox_id
      and rejection.chatwoot_message_id = p_chatwoot_message_id
    for update;
    if v_rejection.id is null then
        outcome := 'not_found';
        return next;
        return;
    end if;
    rejection_id := v_rejection.id;
    if v_rejection.touch_kind <> 'first_touch'
       or v_rejection.meta_error_code is distinct from '131049' then
        outcome := 'not_eligible';
        return next;
        return;
    end if;
    -- Uno solo, para siempre: reservado, mandado, fallido o frenado.
    if v_rejection.retry_status <> 'none' then
        outcome := 'already_attempted';
        return next;
        return;
    end if;
    -- Meta: no antes de 24 h. No escribe: el barrido siguiente lo vuelve a ver.
    if v_rejection.message_created_at > v_now - interval '24 hours' then
        outcome := 'too_early';
        return next;
        return;
    end if;

    v_block := null;
    if v_rejection.message_created_at < v_now - make_interval(secs => p_max_age_seconds) then
        v_block := 'blocked_too_late';
    end if;

    if v_block is null then
        select conversation.* into v_conversation
        from public.conversations conversation
        where conversation.commercial_context = jsonb_build_object(
            'chatwoot_conversation_id', v_rejection.external_conversation_id::text
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
    end if;

    -- La baja por Chatwoot, por la conversacion o por el telefono (las dos
    -- formas del movil: los ultimos 9 digitos).
    if v_block is null and exists (
        select 1
        from public.contact_opt_out_events opt_out
        where opt_out.canonical_inbox_id = p_chatwoot_inbox_id
          and (opt_out.canonical_conversation_id = v_rejection.external_conversation_id
               or right(opt_out.external_user_id, 9) = v_phone_tail)
    ) then
        v_block := 'blocked_opt_out';
    end if;

    -- Ya compro, por cualquier oferta y por cualquier camino (el mismo
    -- predicado que la reserva del seguimiento con cupon).
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
              and event.received_at >= v_rejection.message_created_at - interval '30 days'
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

    -- El caso del intento, si la base lo conoce: cerrado por otra cosa que no
    -- sea haber mandado su unico toque.
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
        update public.meta_delivery_rejections rejection
        set retry_status = 'blocked',
            retry_reason = v_block,
            retry_settled_at = v_now
        where rejection.id = v_rejection.id;
        outcome := v_block;
        return next;
        return;
    end if;

    update public.meta_delivery_rejections rejection
    set retry_status = 'claimed',
        retry_claimed_at = v_now
    where rejection.id = v_rejection.id;
    outcome := 'claimed';
    return next;
end;
$function$;

-- 5. Cerrar la reserva.

create function public.settle_meta_first_touch_retry_v1(
    p_rejection_id uuid,
    p_status text,
    p_retry_message_id bigint default null,
    p_failure_reason text default null,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    rejection_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_rejection public.meta_delivery_rejections%rowtype;
begin
    if p_rejection_id is null
       or p_status is null or p_status not in ('sent', 'failed')
       or (p_status = 'sent' and (p_retry_message_id is null or p_retry_message_id <= 0
                                  or p_failure_reason is not null))
       or (p_status = 'failed' and (p_retry_message_id is not null
                                    or p_failure_reason is null
                                    or p_failure_reason !~ '^[a-z0-9_]{1,64}$')) then
        raise exception using errcode = '22023',
            message = 'settle_meta_first_touch_retry_invalid_input';
    end if;

    select rejection.* into v_rejection
    from public.meta_delivery_rejections rejection
    where rejection.id = p_rejection_id
      and rejection.retry_status = 'claimed'
    for update;
    if v_rejection.id is null then
        outcome := 'not_found';
        rejection_id := p_rejection_id;
        return next;
        return;
    end if;

    update public.meta_delivery_rejections rejection
    set retry_status = p_status,
        retry_message_id = case when p_status = 'sent' then p_retry_message_id end,
        retry_reason = case when p_status = 'failed' then p_failure_reason end,
        retry_settled_at = coalesce(p_now, clock_timestamp())
    where rejection.id = v_rejection.id;

    outcome := 'settled';
    rejection_id := v_rejection.id;
    return next;
end;
$function$;

-- 5b. Marcar el aviso al equipo de un 131026 (idempotente).

create function public.mark_meta_delivery_rejection_notified_v1(
    p_rejection_id uuid,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_rejection_id is null then
        raise exception using errcode = '22023',
            message = 'mark_meta_delivery_rejection_notified_invalid_input';
    end if;
    update public.meta_delivery_rejections rejection
    set team_notified_at = coalesce(p_now, clock_timestamp())
    where rejection.id = p_rejection_id
      and rejection.meta_error_code = '131026'
      and rejection.team_notified_at is null;
    if found then
        outcome := 'marked';
    elsif exists (select 1 from public.meta_delivery_rejections rejection
                  where rejection.id = p_rejection_id
                    and rejection.team_notified_at is not null) then
        outcome := 'replayed';
    else
        outcome := 'not_found';
    end if;
    return next;
end;
$function$;

-- 5c. Frenar el reintento por lo que vio el bridge en Chatwoot (la persona
-- escribio, la conversacion siguio, la plantilla no esta en la lista): queda
-- blocked con el motivo y no se vuelve a leer.

create function public.block_meta_first_touch_retry_v1(
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
begin
    if p_rejection_id is null
       or p_reason is null or p_reason !~ '^bridge_[a-z0-9_]{1,57}$' then
        raise exception using errcode = '22023',
            message = 'block_meta_first_touch_retry_invalid_input';
    end if;
    update public.meta_delivery_rejections rejection
    set retry_status = 'blocked',
        retry_reason = p_reason,
        retry_settled_at = coalesce(p_now, clock_timestamp())
    where rejection.id = p_rejection_id
      and rejection.retry_status = 'none'
      and rejection.touch_kind = 'first_touch'
      and rejection.meta_error_code = '131049';
    outcome := case when found then 'blocked' else 'not_found' end;
    return next;
end;
$function$;

-- 6. Contar por plantilla.

create view public.meta_delivery_rejection_counts_v1
with (security_invoker = true)
as
select rejection.chatwoot_inbox_id,
       coalesce(rejection.template_name, 'sin_plantilla') as template_name,
       rejection.touch_kind,
       coalesce(rejection.meta_error_code, 'sin_codigo') as meta_error_code,
       count(*)::bigint as rejected,
       count(*) filter (where rejection.retry_status = 'sent')::bigint as retried,
       count(*) filter (where rejection.retry_status = 'blocked')::bigint as retry_blocked,
       count(*) filter (where rejection.retry_status = 'failed')::bigint as retry_failed,
       count(*) filter (where rejection.retry_status = 'sent' and exists (
           select 1 from public.meta_delivery_rejections again
           where again.chatwoot_inbox_id = rejection.chatwoot_inbox_id
             and again.retry_of_message_id = rejection.chatwoot_message_id
       ))::bigint as retry_rejected_again,
       min(rejection.message_created_at) as first_at,
       max(rejection.message_created_at) as last_at
from public.meta_delivery_rejections rejection
group by rejection.chatwoot_inbox_id,
         coalesce(rejection.template_name, 'sin_plantilla'),
         rejection.touch_kind,
         coalesce(rejection.meta_error_code, 'sin_codigo');

-- Permisos. Postgres le da execute a PUBLIC en toda funcion nueva y Supabase
-- expone tablas y vistas a la API: todo se revoca, y solo service_role (el
-- bridge) ejecuta las seis funciones. La tabla y la vista no tienen permisos
-- para ningun rol de la API.
do $acl$
declare
    v_function text;
    v_role text;
begin
    revoke all on table public.meta_delivery_rejections from public;
    revoke all on table public.meta_delivery_rejection_counts_v1 from public;
    foreach v_function in array array[
        'public.record_meta_delivery_rejection_v1(bigint,bigint,bigint,bigint,text,text,text,timestamptz,bigint,boolean,timestamptz)',
        'public.mark_meta_delivery_rejection_notified_v1(uuid,timestamptz)',
        'public.block_meta_first_touch_retry_v1(uuid,text,timestamptz)',
        'public.list_due_meta_first_touch_retries_v1(bigint,integer,integer,timestamptz)',
        'public.claim_meta_first_touch_retry_v1(bigint,bigint,text,text,integer,timestamptz)',
        'public.settle_meta_first_touch_retry_v1(uuid,text,bigint,text,timestamptz)'
    ] loop
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
            execute format('revoke all on table public.meta_delivery_rejections from %I', v_role);
            execute format('revoke all on table public.meta_delivery_rejection_counts_v1 from %I', v_role);
        end if;
    end loop;

    -- Por presencia: service_role ejecuta las seis y ningun rol de la API.
    foreach v_function in array array[
        'public.record_meta_delivery_rejection_v1(bigint,bigint,bigint,bigint,text,text,text,timestamptz,bigint,boolean,timestamptz)',
        'public.mark_meta_delivery_rejection_notified_v1(uuid,timestamptz)',
        'public.block_meta_first_touch_retry_v1(uuid,text,timestamptz)',
        'public.list_due_meta_first_touch_retries_v1(bigint,integer,integer,timestamptz)',
        'public.claim_meta_first_touch_retry_v1(bigint,bigint,text,text,integer,timestamptz)',
        'public.settle_meta_first_touch_retry_v1(uuid,text,bigint,text,timestamptz)'
    ] loop
        if exists (select 1 from pg_roles where rolname = 'service_role'
                      and not has_function_privilege(rolname, v_function, 'EXECUTE'))
           or exists (select 1 from pg_roles where rolname in ('anon', 'authenticated')
                         and has_function_privilege(rolname, v_function, 'EXECUTE')) then
            raise exception using errcode = '55000',
                message = 'meta_delivery_rejections_acl_unexpected', detail = v_function;
        end if;
    end loop;
    if exists (select 1 from pg_roles where rolname in ('anon', 'authenticated', 'service_role')
                  and (has_table_privilege(rolname, 'public.meta_delivery_rejections', 'SELECT')
                       or has_table_privilege(rolname, 'public.meta_delivery_rejection_counts_v1', 'SELECT'))) then
        raise exception using errcode = '55000',
            message = 'meta_delivery_rejections_acl_unexpected', detail = 'tabla o vista';
    end if;
end
$acl$;

commit;
