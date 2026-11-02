-- recovery-coupon-by-reply-v1: la respuesta escrita a A2 reactiva el cupon
-- personal igual que el boton, y el agente sabe el cupon vigente de la persona
-- (E-267, el arreglo de fondo de F-148; diagnostico:
-- documentacion/2026-11-01-tres-errores-de-mensajes-en-conversaciones-reales.md,
-- error 1, «la correccion correcta»).
--
-- Por que. A2 del carrito de ATT1 (att1_carrito_dudas_01) dice «Tu 10 % ya
-- vencio, pero si me respondes te lo reactivo». Hoy solo el boton exacto
-- «Quiero mi 10 %» lo cumple: request_recovery_renewal_v1 (20261019000600, y
-- 20261019000700 la limita a cart_last_touch) adelanta Z, la unica renovacion
-- del caso, con el cupon 2. Una respuesta escrita con otras palabras va al
-- agente, que no sabe el cupon de la persona, y cuando Z llega a su hora la
-- reevaluacion lo cancela por la respuesta (prospect_replied). En ATT1 #471 y
-- #517 la persona contesto A2 escribiendo y el agente le dio el codigo
-- general (o le dijo que el descuento «no vence»).
--
-- Que agrega. Dos funciones nuevas, security definer y solo de service_role.
-- No redefine ninguna funcion, no crea tablas ni toca filas existentes.
--   1. request_recovery_renewal_by_reply_v1: la misma renovacion que el boton
--      (delega en request_recovery_renewal_v1, con su lock, su idempotencia
--      por mensaje y su fila en recovery_renewal_requests, que es lo que hace
--      que la reevaluacion no cancele Z por la respuesta), pero solo si la
--      respuesta es posterior a A2: Z se inserta al aceptarse A2, asi que un Z
--      creado despues del mensaje dice que el mensaje contestaba a A1 y da
--      reply_before_last_touch sin escribir nada. Lo que es una baja o un
--      «no» claro lo decide el bridge antes de llamarla (bridge/opt_out.py y
--      bridge/lead_decline.py): la base no lee texto.
--   2. get_recovery_coupon_for_conversation_v1: lo que el agente necesita en
--      su turno. A lo sumo una fila:
--      * state = 'valid': el cupon personal vigente que la persona ya recibio
--        en un mensaje nuestro (status valid, sin vencer, de un caso de esta
--        conversacion e identidad sin compra). El 1 cuenta si salio en A1
--        (recovery_coupon_was_delivered, 20261019000500, la misma prueba que
--        usa el pago rechazado); el 2, si salio en el paso con coupon_ordinal
--        2 (Z del carrito o el seguimiento del pago rechazado): su link
--        aceptado por Chatwoot, emitido despues de verificar el cupon. Un
--        cupon sembrado que todavia no se mando no cuenta: el agente no le da
--        a nadie un codigo que no le dimos. Si hay varios, el que vence mas
--        tarde.
--      * state = 'renewal_in_progress': no hay uno vigente entregado y la
--        persona ya pidio la renovacion (boton o respuesta) de un Z que
--        todavia no salio: el codigo le llega en Z en unos minutos.
--      * sin filas: lo de hoy.
--
-- La regla de dos codigos por caso (decision del 2026-10-17, regla 7) no
-- cambia: nada siembra un cupon nuevo. La respuesta solo adelanta el Z que ya
-- existe, con el cupon 2 que ya iba a llevar.
--
-- El freno. request_recovery_renewal_v1 tiene que ser la vigente conocida (la
-- de 20261019000700, solo cart_last_touch): si otra migracion la cambio, esta
-- aborta con 55000 en vez de delegar en algo que no conoce. Y si alguna de las
-- dos nuevas ya existe, tambien.

begin;

set local lock_timeout = '5s';

do $guard$
declare
    v_definition text;
begin
    if to_regprocedure('public.request_recovery_renewal_by_reply_v1(bigint,bigint,bigint,bigint,text,timestamptz)') is not null
       or to_regprocedure('public.get_recovery_coupon_for_conversation_v1(bigint,bigint,bigint,text)') is not null then
        raise exception using errcode = '55000',
            message = 'recovery_coupon_by_reply_already_applied';
    end if;
    if to_regprocedure('public.request_recovery_renewal_v1(bigint,bigint,bigint,bigint,text,timestamptz)') is null
       or to_regprocedure('public.recovery_coupon_was_delivered(uuid)') is null
       or to_regprocedure('public._recovery_followup_step(uuid)') is null
       or to_regclass('public.recovery_renewal_requests') is null then
        raise exception using errcode = '55000',
            message = 'recovery_coupon_by_reply_dependency_missing';
    end if;
    select pg_get_functiondef('public.request_recovery_renewal_v1(bigint,bigint,bigint,bigint,text,timestamptz)'::regprocedure)
      into v_definition;
    if position('and action.step_key = ''cart_last_touch''' in v_definition) = 0
       or position('insert into public.recovery_renewal_requests' in v_definition) = 0
       or position('''no_pending_last_touch''' in v_definition) = 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_request_recovery_renewal_definition';
    end if;
end;
$guard$;

-- ---------------------------------------------------------------------------
-- 1. La renovacion por respuesta escrita.
-- ---------------------------------------------------------------------------
create function public.request_recovery_renewal_by_reply_v1(
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_chatwoot_message_id bigint,
    p_external_user_id text,
    p_occurred_at timestamptz
)
returns table (outcome text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
begin
    -- recovery_coupon_by_reply: la respuesta escrita a A2
    if p_chatwoot_account_id is null or p_chatwoot_account_id < 1
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1
       or p_chatwoot_conversation_id is null or p_chatwoot_conversation_id < 1
       or p_chatwoot_message_id is null or p_chatwoot_message_id < 1
       or p_external_user_id is null or p_external_user_id !~ '^[0-9]+$'
       or p_occurred_at is null then
        raise exception using errcode = '22023', message = 'invalid_recovery_renewal_input';
    end if;

    -- El mismo lock que la renovacion del boton (los advisory locks de una
    -- transaccion se apilan: request_recovery_renewal_v1 lo vuelve a tomar).
    perform pg_advisory_xact_lock(hashtextextended(
        concat_ws(':', 'recovery-renewal', p_chatwoot_account_id,
                  p_chatwoot_conversation_id), 0
    ));

    -- Un Z pendiente que nacio despues del mensaje: el mensaje contestaba a A1,
    -- no a A2 (un webhook demorado o reintentado). No se adelanta nada.
    if exists (
        select 1
        from public.scheduled_actions action
        join public.recovery_cases recovery
          on recovery.id = action.recovery_case_id
        join public.conversations conversation
          on conversation.id = recovery.conversation_id
        join public.channel_identities identity
          on identity.id = recovery.selected_channel_identity_id
        where conversation.commercial_context ->> 'chatwoot_conversation_id'
                  = p_chatwoot_conversation_id::text
          and identity.channel = 'whatsapp'
          and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
          and identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
          and identity.external_user_id = any(
              coalesce(public._whatsapp_phone_variants(p_external_user_id), array[p_external_user_id])
          )
          and recovery.status in ('grace_period', 'active')
          and recovery.purchase_event_id is null
          and action.action_type = 'no_reply_review'
          and action.step_key = 'cart_last_touch'
          and action.status in ('pending', 'deferred', 'retryable_failed')
          and action.expires_at > now()
          and action.created_at > p_occurred_at
    ) and not exists (
        select 1
        from public.recovery_renewal_requests renewal
        where renewal.chatwoot_account_id = p_chatwoot_account_id
          and renewal.chatwoot_message_id = p_chatwoot_message_id
    ) then
        return query select 'reply_before_last_touch'::text;
        return;
    end if;

    return query
    select renewal.outcome
    from public.request_recovery_renewal_v1(
        p_chatwoot_account_id,
        p_chatwoot_inbox_id,
        p_chatwoot_conversation_id,
        p_chatwoot_message_id,
        p_external_user_id,
        p_occurred_at
    ) renewal;
end;
$function$;

-- ---------------------------------------------------------------------------
-- 2. El cupon vigente para el turno del agente.
-- ---------------------------------------------------------------------------
create function public.get_recovery_coupon_for_conversation_v1(
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_external_user_id text
)
returns table (
    state text,
    code text,
    discount numeric,
    expires_at timestamptz,
    ordinal smallint
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
begin
    -- recovery_coupon_by_reply: el cupon que el agente puede nombrar
    if p_chatwoot_account_id is null or p_chatwoot_account_id < 1
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1
       or p_chatwoot_conversation_id is null or p_chatwoot_conversation_id < 1
       or p_external_user_id is null or p_external_user_id !~ '^[0-9]+$' then
        raise exception using errcode = '22023', message = 'invalid_recovery_coupon_lookup_input';
    end if;

    return query
    with person_case as (
        select recovery.id, recovery.status
        from public.recovery_cases recovery
        join public.conversations conversation
          on conversation.id = recovery.conversation_id
        join public.channel_identities identity
          on identity.id = recovery.selected_channel_identity_id
        where conversation.commercial_context ->> 'chatwoot_conversation_id'
                  = p_chatwoot_conversation_id::text
          and identity.channel = 'whatsapp'
          and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
          and identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
          and identity.external_user_id = any(
              coalesce(public._whatsapp_phone_variants(p_external_user_id), array[p_external_user_id])
          )
          and recovery.purchase_event_id is null
    ),
    delivered as (
        select coupon.code, coupon.discount, coupon.expires_at, coupon.ordinal
        from public.recovery_coupons coupon
        join person_case on person_case.id = coupon.recovery_case_id
        where coupon.status = 'valid'
          and coupon.expires_at > now()
          and coupon.verified_at is not null
          and (
              (coupon.ordinal = 1 and public.recovery_coupon_was_delivered(coupon.id))
              or (
                  coupon.ordinal = 2
                  and exists (
                      select 1
                      from public.checkout_link_issuances issuance
                      join public.followup_delivery_attempts attempt
                        on attempt.id = issuance.delivery_attempt_id
                      join public.scheduled_actions action
                        on action.id = attempt.action_id
                      where issuance.recovery_case_id = coupon.recovery_case_id
                        and issuance.source_kind = 'recovery_first_touch'
                        and issuance.status in ('accepted_by_chatwoot', 'purchase_matched')
                        and issuance.created_at >= coupon.verified_at
                        and action.recovery_case_id = coupon.recovery_case_id
                        and action.action_type = 'no_reply_review'
                        and coalesce(public._recovery_followup_step(action.id) ->> 'coupon_ordinal', '') = '2'
                  )
              )
          )
        order by coupon.expires_at desc, coupon.ordinal desc
        limit 1
    ),
    renewal as (
        select null::text as code, null::numeric as discount,
               null::timestamptz as expires_at, 2::smallint as ordinal
        from public.recovery_renewal_requests request
        join public.scheduled_actions action on action.id = request.action_id
        join person_case on person_case.id = request.recovery_case_id
        where action.recovery_case_id = request.recovery_case_id
          and person_case.status in ('grace_period', 'active')
          and action.status in ('pending', 'deferred', 'retryable_failed')
          and action.expires_at > now()
        limit 1
    )
    select 'valid'::text, delivered.code, delivered.discount,
           delivered.expires_at, delivered.ordinal
    from delivered
    union all
    select 'renewal_in_progress'::text, renewal.code, renewal.discount,
           renewal.expires_at, renewal.ordinal
    from renewal
    where not exists (select 1 from delivered);
end;
$function$;

-- ---------------------------------------------------------------------------
-- Permisos: solo service_role ejecuta.
-- ---------------------------------------------------------------------------
revoke all on function public.request_recovery_renewal_by_reply_v1(bigint, bigint, bigint, bigint, text, timestamptz) from public;
revoke all on function public.get_recovery_coupon_for_conversation_v1(bigint, bigint, bigint, text) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'revoke all on function public.request_recovery_renewal_by_reply_v1(bigint, bigint, bigint, bigint, text, timestamptz) from anon';
        execute 'revoke all on function public.get_recovery_coupon_for_conversation_v1(bigint, bigint, bigint, text) from anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'revoke all on function public.request_recovery_renewal_by_reply_v1(bigint, bigint, bigint, bigint, text, timestamptz) from authenticated';
        execute 'revoke all on function public.get_recovery_coupon_for_conversation_v1(bigint, bigint, bigint, text) from authenticated';
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'grant execute on function public.request_recovery_renewal_by_reply_v1(bigint, bigint, bigint, bigint, text, timestamptz) to service_role';
        execute 'grant execute on function public.get_recovery_coupon_for_conversation_v1(bigint, bigint, bigint, text) to service_role';
    end if;
end;
$roles$;

commit;
