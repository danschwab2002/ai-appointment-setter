-- Migration: una compra con nuestro link cuenta como recuperada siempre,
-- aunque la secuencia ya haya terminado o el caso este cerrado (E-169).
--
-- Regla de Dan (2026-10-28): si la persona compra por uno de nuestros links
-- (src=hermes, sck=hermes~v1~<ulid>, con o sin el sck del anuncio delante),
-- emitido por el agente, por una plantilla con boton o por el seguimiento con
-- cupon, la venta es recuperada, en cualquier estado del caso. Si compra por
-- otro camino despues de terminar la secuencia, no cuenta (eso no cambia: la
-- admision por identidad sigue mirando solo casos abiertos).
--
-- Lo que se midio antes de tocar (28/10, sesiones de solo lectura):
-- - ATT1: 11 compras con nuestro sck, las 11 con su emision en
--   purchase_matched. 4 son de un link del carrito (recovery_first_touch): 3
--   ganaron su caso y 1 no, porque el caso ya estaba en sequence_exhausted
--   cuando compro (el helper solo cerraba casos en grace_period, active o
--   paused). Las otras 7 son links del agente (precheckout_request), que no
--   tienen caso del recuperador: cuentan por la emision, como hasta hoy.
-- - Maribel: 1 compra con nuestro sck (link del agente), en purchase_matched.
-- - Un flujo de un solo toque (el pago rechazado de Maribel, la bienvenida, un
--   carrito de un toque) deja su caso en sequence_exhausted apenas sale el
--   mensaje, asi que una compra con su link nunca ganaba el caso.
--
-- Que cambia:
-- 1. _close_recovery_case_for_checkout_purchase_v1 gana el caso del link en
--    cualquier estado que no sea won: abierto (como hasta hoy, outcome 'won')
--    o cerrado (sequence_exhausted, cancelled, expired, escalated, lost,
--    unreachable, error: outcome 'won_after_close'). El caso cerrado conserva
--    su closed_at; el contexto y el conversation_event purchase_detected
--    guardan el estado de antes (purchase_case_status_before /
--    case_status_before). Un caso que ya gano otra compra, o una compra que ya
--    gano otro caso, no se tocan (purchase_event_id es unico). El cupon se
--    canjea como hasta hoy.
-- 2. correlate_hotmart_checkout_issuance_v2 marca la emision purchase_matched
--    aunque su intencion ya no este en waiting_for_purchase (comprada por otra
--    emision que la comparte, o cancelada). La intencion solo pasa a purchased
--    si estaba esperando, como hasta hoy. Antes, esa compra devolvia
--    purchase_already_approved y no quedaba en ningun lado (medido: 0 casos en
--    ATT1 y en Maribel). Reemplazo contado sobre la definicion viva (el metodo
--    de 20261005000100): si el texto no aparece exactamente una vez, la
--    migracion falla con 55000 y no cambia nada.
-- 3. Se aplica a lo que ya paso: cada emision purchase_matched con caso del
--    recuperador que no es won se pasa por el helper nuevo, con la hora de la
--    compra (en ATT1, 1 caso).
--
-- Quien lee el resultado (nadie cambia de codigo):
-- - read_flow_sales_v1 (las ventas por flujo del AE): by_case lee
--   recovery_cases.purchase_event_id, asi que la compra con el link de un
--   recorrido del motor ya cerrado cuenta por su caso y no por identidad;
-- - la historia del AE (purchased sale de checkout_link_issuances.purchased_at)
--   y el reporte diario (purchase_recorded) ya leian la emision;
-- - flow_parity: un caso won da el final 'compro' a sus pasos ausentes, que es
--   lo que paso.
--
-- Lo que NO hace: no toca el bridge (el outcome del correlador sigue siendo
-- 'matched'), no cambia la admision por identidad, no marca el webhook como
-- processed (la ruta del link nunca lo hizo) y no crea casos para los links
-- del agente.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

-- 0. Una sola vez.
do $guard$
begin
    if to_regprocedure('public._close_recovery_case_for_checkout_purchase_v1(uuid,uuid,timestamptz)') is null
       or to_regprocedure('public.correlate_hotmart_checkout_issuance_v2(uuid,text,timestamptz)') is null then
        raise exception using errcode = '55000',
            message = 'purchase_by_our_link_counts_missing_function';
    end if;
    if position('purchase_by_our_link_counts' in pg_get_functiondef(
           'public._close_recovery_case_for_checkout_purchase_v1(uuid,uuid,timestamptz)'::regprocedure)) > 0
       or position('purchase_by_our_link_counts' in pg_get_functiondef(
           'public.correlate_hotmart_checkout_issuance_v2(uuid,text,timestamptz)'::regprocedure)) > 0 then
        raise exception using errcode = '55000',
            message = 'purchase_by_our_link_counts_already_applied';
    end if;
    if position('case_not_open' in pg_get_functiondef(
           'public._close_recovery_case_for_checkout_purchase_v1(uuid,uuid,timestamptz)'::regprocedure)) = 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_recovery_checkout_close_definition';
    end if;
end
$guard$;

-- 1. El helper: el caso del link se gana abierto o cerrado.
create or replace function public._close_recovery_case_for_checkout_purchase_v1(
    p_issuance_id uuid,
    p_webhook_event_id uuid,
    p_now timestamptz
)
returns text
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_issuance public.checkout_link_issuances%rowtype;
    v_event public.webhook_events%rowtype;
    v_case public.recovery_cases%rowtype;
    v_approved_at timestamptz;
    v_coupon_code text;
    v_status_before text;
    v_outcome text := 'case_not_open';
begin
    -- recovery_checkout_issuance: cerrar el caso del carrito
    -- purchase_by_our_link_counts (20261028000400): la compra con nuestro link
    -- gana el caso aunque la secuencia haya terminado o el caso este cerrado.
    select issuance.* into v_issuance
    from public.checkout_link_issuances issuance
    where issuance.id = p_issuance_id;
    if not found or v_issuance.recovery_case_id is null then
        return 'not_recovery';
    end if;

    select event.* into strict v_event
    from public.webhook_events event
    where event.id = p_webhook_event_id;

    v_approved_at := case
        when v_event.payload #>> '{data,purchase,approved_date}' ~ '^[0-9]{10,16}$'
        then to_timestamp(
            (v_event.payload #>> '{data,purchase,approved_date}')::double precision
            / 1000.0
        )
        else p_now
    end;
    v_coupon_code := nullif(upper(btrim(
        v_event.payload #>> '{data,purchase,offer,coupon_code}'
    )), '');

    -- El orden de locks de apply_hotmart_purchase_approved: caso -> secuencia
    -- -> accion -> intento.
    select rc.* into v_case
    from public.recovery_cases rc
    where rc.id = v_issuance.recovery_case_id
    for update;

    if found
       and v_case.status <> 'won'
       and v_case.purchase_event_id is null
       and not exists (
           select 1 from public.recovery_cases other
           where other.purchase_event_id = p_webhook_event_id
       ) then
        v_status_before := v_case.status;

        perform 1
        from public.followup_sequences fs
        where fs.recovery_case_id = v_case.id
          and fs.status in ('active', 'paused')
        order by fs.id
        for update;
        perform 1
        from public.scheduled_actions sa
        where sa.recovery_case_id = v_case.id
          and sa.status in ('pending', 'deferred', 'retryable_failed')
        order by sa.id
        for update;
        perform 1
        from public.followup_delivery_attempts attempt
        join public.scheduled_actions sa on sa.id = attempt.action_id
        where sa.recovery_case_id = v_case.id
          and attempt.phase = 'request_started'
        order by attempt.id
        for update of attempt;

        update public.scheduled_actions sa
        set status = 'delivery_unknown',
            terminal_reason = 'purchase_detected_request_in_flight',
            lease_owner = null,
            lease_expires_at = null,
            next_attempt_at = null
        where sa.recovery_case_id = v_case.id
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
        where sa.recovery_case_id = v_case.id
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
            completed_at = coalesce(fs.completed_at, p_now),
            revision = fs.revision + 1
        where fs.recovery_case_id = v_case.id
          and fs.status in ('active', 'paused');

        -- Un caso cerrado conserva su closed_at: la secuencia termino antes.
        update public.recovery_cases rc
        set status = 'won',
            lead_stage = 'won',
            purchase_event_id = p_webhook_event_id,
            won_at = v_approved_at,
            closed_at = coalesce(rc.closed_at, p_now),
            next_contact_at = null,
            next_contact_reason = 'purchase_detected',
            context = rc.context || jsonb_build_object(
                'purchase_transaction', v_event.payload #>> '{data,purchase,transaction}',
                'purchase_approved_at', v_approved_at,
                'purchase_matched_by', 'checkout_link',
                'purchase_checkout_issuance_id', v_issuance.id,
                'purchase_case_status_before', v_status_before
            ),
            version = rc.version + 1
        where rc.id = v_case.id
          and rc.status = v_status_before
          and rc.purchase_event_id is null;
        if not found then
            raise exception using errcode = '40001',
                message = 'recovery_checkout_purchase_case_changed_concurrently';
        end if;

        insert into public.conversation_events (
            conversation_id, recovery_case_id, event_type, actor_type, data
        ) values (
            v_case.conversation_id, v_case.id, 'purchase_detected', 'integration',
            jsonb_build_object(
                'webhook_event_id', p_webhook_event_id,
                'matched_by', 'checkout_link',
                'checkout_issuance_id', v_issuance.id,
                'case_status_before', v_status_before
            )
        );
        v_outcome := case
            when v_status_before in ('grace_period', 'active', 'paused') then 'won'
            else 'won_after_close'
        end;
    end if;

    -- El cupon personal con que se compro, si es de este caso.
    if v_coupon_code is not null then
        update public.recovery_coupons coupon
        set status = 'redeemed',
            redeemed_webhook_event_id = p_webhook_event_id,
            lease_owner = null,
            lease_expires_at = null,
            next_attempt_at = null,
            updated_at = p_now
        where coupon.recovery_case_id = v_issuance.recovery_case_id
          and coupon.code = v_coupon_code
          and coupon.status in ('created', 'valid')
          and coupon.redeemed_webhook_event_id is null;
    end if;

    return v_outcome;
end;
$function$;

-- 2. El correlador: la emision se marca aunque la intencion ya no espere.
do $correlate$
declare
    v_signature constant text :=
        'public.correlate_hotmart_checkout_issuance_v2(uuid,text,timestamptz)';
    v_old constant text := $old$    perform 1
    from public.purchase_intents intent
    where intent.id = v_issuance.purchase_intent_id
      and intent.lifecycle_state = 'waiting_for_purchase'
    for update;
    if not found then
        return query select 'purchase_already_approved'::text,
            v_issuance.id, v_issuance.purchase_intent_id,
            v_issuance.commercial_case_id;
        return;
    end if;

    update public.purchase_intents
    set lifecycle_state = 'purchased', current_classification = null,
        activation_authorized = false, updated_at = p_now
    where id = v_issuance.purchase_intent_id
      and lifecycle_state = 'waiting_for_purchase';
    if not found then
        raise exception using errcode = '40001',
            message = 'checkout_issuance_purchase_intent_changed_concurrently';
    end if;

    perform public.cancel_hotmart_abandonment_reevaluations_for_purchase(
        v_issuance.purchase_intent_id, p_now
    );
$old$;
    v_new constant text := $new$    -- purchase_by_our_link_counts (20261028000400): la compra con nuestro
    -- link cuenta aunque la intencion ya no espere (comprada por otra emision
    -- que la comparte, o cancelada). La intencion pasa a purchased solo si
    -- estaba esperando.
    perform 1
    from public.purchase_intents intent
    where intent.id = v_issuance.purchase_intent_id
      and intent.lifecycle_state = 'waiting_for_purchase'
    for update;
    if found then
        update public.purchase_intents
        set lifecycle_state = 'purchased', current_classification = null,
            activation_authorized = false, updated_at = p_now
        where id = v_issuance.purchase_intent_id
          and lifecycle_state = 'waiting_for_purchase';
        if not found then
            raise exception using errcode = '40001',
                message = 'checkout_issuance_purchase_intent_changed_concurrently';
        end if;

        perform public.cancel_hotmart_abandonment_reevaluations_for_purchase(
            v_issuance.purchase_intent_id, p_now
        );
    end if;
$new$;
    v_definition text;
begin
    v_definition := pg_get_functiondef(v_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, v_old, ''))
           <> length(v_old)
       or position('_close_recovery_case_for_checkout_purchase_v1' in v_definition) = 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_checkout_issuance_correlate_definition',
            detail = v_signature;
    end if;
    execute replace(v_definition, v_old, v_new);
    if position(v_new in pg_get_functiondef(v_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000',
            message = 'checkout_issuance_correlate_not_replaced',
            detail = v_signature;
    end if;
end
$correlate$;

-- 3. Lo que ya paso: las emisiones compradas cuyo caso no quedo ganado.
do $backfill$
declare
    v_row record;
begin
    for v_row in
        select issuance.id, issuance.hotmart_webhook_event_id, issuance.purchased_at
        from public.checkout_link_issuances issuance
        join public.recovery_cases rc on rc.id = issuance.recovery_case_id
        where issuance.status = 'purchase_matched'
          and issuance.hotmart_webhook_event_id is not null
          and rc.status <> 'won'
          and rc.purchase_event_id is null
        order by issuance.purchased_at, issuance.id
    loop
        perform public._close_recovery_case_for_checkout_purchase_v1(
            v_row.id, v_row.hotmart_webhook_event_id,
            coalesce(v_row.purchased_at, clock_timestamp())
        );
    end loop;
end
$backfill$;

-- 4. Permisos. create or replace conserva los grants; el helper sigue sin
--    nadie (lo llama el correlador security definer).
revoke all on function public._close_recovery_case_for_checkout_purchase_v1(uuid, uuid, timestamptz) from public;

do $roles$
declare
    v_role text;
begin
    for v_role in
        select rolname from pg_catalog.pg_roles
        where rolname in ('anon', 'authenticated', 'service_role')
    loop
        execute format('revoke all on function public._close_recovery_case_for_checkout_purchase_v1(uuid, uuid, timestamptz) from %I', v_role);
    end loop;
end
$roles$;

commit;
