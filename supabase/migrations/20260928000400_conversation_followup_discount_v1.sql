-- Migration: el seguimiento con cupon para quien contesto y no compro.
--
-- Decision de Dan, 2026-09-28 (vault: productos/soporte-infoproductores/
-- decisiones/2026-09-28-seguimiento-con-cupon-solo-a-quien-contesto.md):
--
--   * Solo recibe seguimiento quien ya nos contesto al menos una vez. A quien
--     nunca respondio no se le manda nada mas que el primer toque: mandarle
--     plantillas de marketing a quien no interactua degrada la cuenta de Meta.
--   * No compro el producto, por ninguna oferta. Se chequea por telefono y por
--     mail.
--   * Ni derivada, ni pausada, ni con opt-out.
--   * El ultimo mensaje es nuestro y el lead se quedo callado 24 h.
--   * Uno solo por conversacion, por plantilla aprobada de Meta, con un boton
--     que abre el checkout con el cupon ya cargado (offDiscount de Hotmart).
--
-- Medido el 2026-09-28 sobre las 154 conversaciones del inbox 9: 103 nunca
-- contestaron (quedan afuera), 6 recibieron el link y no compraron, 8
-- preguntaron y se callaron, 9 compraron, 24 estan derivadas o pausadas, 3
-- pidieron que no las contacten.
--
-- Que hace esta migracion.
--
--   1. conversation_followup_events: una fila por seguimiento, reservada ANTES
--      de llamar a Chatwoot. Mismo ciclo claimed -> sent | failed que la
--      reactivacion (20260923000200), por la misma razon: mandarle dos veces
--      una plantilla de marketing al mismo lead es peor que no mandarsela.
--   2. claim_conversation_followup_v1: aplica las barreras durables y, si
--      pasan, emite el link de pago con reserve_chatwoot_checkout_issuance_v2,
--      la misma RPC que usa el agente. Asi el link del seguimiento lleva la
--      misma oferta, el mismo src, el mismo sck con la marca del recuperador y
--      el mismo fbclid, y una compra por ese link se correlaciona sola.
--   3. settle_conversation_followup_v1: cierra la reserva con lo que Chatwoot
--      respondio.
--
-- El ancla de la emision. reserve_chatwoot_checkout_issuance_v2 ata cada link
-- al mensaje que lo disparo (unique por conversacion + mensaje). El seguimiento
-- no tiene un mensaje del lead que lo dispare, y el ultimo mensaje del lead
-- puede ser justamente el que ya produjo el link del agente (el caso R1): con
-- ese ancla, la RPC devolveria el link viejo. Se ancla en el ID de NUESTRO
-- ultimo mensaje, el que el lead dejo sin contestar. Los IDs de mensaje de
-- Chatwoot son globales, asi que nunca coinciden con el de un mensaje del lead,
-- y el link del seguimiento es siempre una emision nueva con su propio ULID.
--
-- Lo que NO hace, a proposito:
--
--   1. No decide a quien seguir. El criterio (ultimo mensaje nuestro, 24 a 72 h
--      desde el ultimo del lead, al menos un mensaje del lead, sin etiqueta de
--      pausa) lo evalua el bridge contra Chatwoot, que es la fuente de esos
--      hechos. Aca viven las barreras que tienen que sobrevivir a un bridge
--      mal configurado.
--   2. No guarda el cupon en la URL de la emision. checkout_url_final conserva
--      el contrato que ya leen la correlacion, la revision diaria y el
--      recuperador; el offDiscount lo agrega el bridge al armar el boton y el
--      codigo queda en esta tabla.
--   3. No toca la reactivacion. Las dos no pueden elegir la misma conversacion
--      en el mismo momento: la reactivacion exige que el ultimo mensaje sea
--      del lead y el seguimiento que sea nuestro. Si el lead contesta el
--      seguimiento y nadie le responde, la reactivacion tiene que poder actuar.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- 1. Auditoria y control de cada seguimiento.

create table public.conversation_followup_events (
    id uuid primary key default gen_random_uuid(),
    conversation_id uuid not null
        references public.conversations(id) on delete restrict,
    commercial_case_id uuid not null
        references public.commercial_cases(id) on delete restrict,
    external_conversation_id bigint not null
        check (external_conversation_id > 0),
    command_key text not null
        check (command_key ~ '^[a-z0-9:_-]{1,200}$'),
    regime text not null
        check (regime in (
            'link_sent_no_purchase',
            'went_quiet',
            'payment_failed'
        )),
    status text not null default 'claimed'
        check (status in ('claimed', 'sent', 'failed')),
    template_name text not null
        check (length(btrim(template_name)) between 1 and 512),
    template_language text not null
        check (length(btrim(template_language)) between 2 and 32),
    coupon_code text not null
        check (coupon_code ~ '^[A-Za-z0-9_-]{1,64}$'),
    last_inbound_message_id bigint not null
        check (last_inbound_message_id > 0),
    last_outbound_message_id bigint not null
        check (last_outbound_message_id > 0),
    inbound_age_seconds integer not null
        check (inbound_age_seconds >= 0),
    checkout_issuance_id uuid not null
        references public.checkout_link_issuances(id) on delete restrict,
    provider_message_id bigint
        check (provider_message_id is null or provider_message_id > 0),
    failure_reason text
        check (failure_reason is null
               or length(btrim(failure_reason)) between 1 and 200),
    created_at timestamptz not null default clock_timestamp(),
    settled_at timestamptz,
    check (last_outbound_message_id > last_inbound_message_id),
    check (
        (status = 'claimed' and settled_at is null
         and provider_message_id is null and failure_reason is null)
        or (status = 'sent' and settled_at is not null
            and failure_reason is null)
        or (status = 'failed' and settled_at is not null
            and provider_message_id is null)
    )
);

-- Uno solo por conversacion. Parcial porque un envio que Chatwoot rechazo
-- explicitamente tiene que poder reintentarse; uno reservado o entregado, no.
create unique index conversation_followup_events_one_live_per_conversation_idx
on public.conversation_followup_events (conversation_id)
where status <> 'failed';

create unique index conversation_followup_events_command_key_idx
on public.conversation_followup_events (command_key)
where status <> 'failed';

alter table public.conversation_followup_events enable row level security;

-- 2. Reservar un seguimiento y emitir su link.

create or replace function public.claim_conversation_followup_v1(
    p_external_conversation_id bigint,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_external_user_id text,
    p_contact_email text,
    p_command_key text,
    p_regime text,
    p_template_name text,
    p_template_language text,
    p_coupon_code text,
    p_last_inbound_message_id bigint,
    p_last_outbound_message_id bigint,
    p_inbound_age_seconds integer,
    p_issuance_ulid text,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    followup_event_id uuid,
    checkout_issuance_id uuid,
    checkout_url_final text,
    sck_value text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_conversation public.conversations%rowtype;
    v_case public.commercial_cases%rowtype;
    v_contact public.contacts%rowtype;
    v_existing public.conversation_followup_events%rowtype;
    v_issuance public.checkout_link_issuances%rowtype;
    v_reservation record;
    v_case_count integer;
    v_email text;
    v_phone_tail text;
    v_event_id uuid;
    v_now timestamptz;
begin
    if p_external_conversation_id is null
       or p_external_conversation_id <= 0
       or p_chatwoot_account_id is null or p_chatwoot_account_id <= 0
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_external_user_id is null
       or p_external_user_id !~ '^[1-9][0-9]{7,14}$'
       or (nullif(btrim(p_contact_email), '') is not null
           and (length(btrim(p_contact_email)) > 320
                or btrim(p_contact_email) !~ '^[^@\s]+@[^@\s]+$'))
       or p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_regime not in ('link_sent_no_purchase', 'went_quiet', 'payment_failed')
       or p_template_name is null
       or length(btrim(p_template_name)) not between 1 and 512
       or p_template_language is null
       or length(btrim(p_template_language)) not between 2 and 32
       or p_coupon_code is null
       or p_coupon_code !~ '^[A-Za-z0-9_-]{1,64}$'
       or p_last_inbound_message_id is null
       or p_last_inbound_message_id <= 0
       or p_last_outbound_message_id is null
       or p_last_outbound_message_id <= p_last_inbound_message_id
       or p_inbound_age_seconds is null
       or p_inbound_age_seconds < 0
       or p_issuance_ulid is null
       or p_issuance_ulid !~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$' then
        raise exception using errcode = '22023',
            message = 'claim_conversation_followup_invalid_input';
    end if;

    v_now := coalesce(p_now, clock_timestamp());
    v_email := nullif(lower(btrim(p_contact_email)), '');
    v_phone_tail := right(p_external_user_id, 9);

    -- Idempotencia: el mismo command_key vivo devuelve la reserva original y
    -- su link, sin tocar nada.
    select event.* into v_existing
    from public.conversation_followup_events event
    where event.command_key = p_command_key
      and event.status <> 'failed';
    if v_existing.id is not null then
        select issuance.* into v_issuance
        from public.checkout_link_issuances issuance
        where issuance.id = v_existing.checkout_issuance_id;
        outcome := 'replayed';
        followup_event_id := v_existing.id;
        checkout_issuance_id := v_existing.checkout_issuance_id;
        checkout_url_final := v_issuance.checkout_url_final;
        sck_value := v_issuance.sck_value;
        return next;
        return;
    end if;

    select conversation.* into v_conversation
    from public.conversations conversation
    where conversation.commercial_context = jsonb_build_object(
        'chatwoot_conversation_id', p_external_conversation_id::text
    )
    for update;
    if v_conversation.id is null then
        outcome := 'not_found';
        return next;
        return;
    end if;

    -- Uno solo por conversacion, venga del command_key que venga.
    if exists (
        select 1 from public.conversation_followup_events event
        where event.conversation_id = v_conversation.id
          and event.status <> 'failed'
    ) then
        outcome := 'blocked_followup_limit';
        return next;
        return;
    end if;

    -- Un select ... into de plpgsql con dos filas toma una arbitraria en
    -- silencio: con dos casos no se adivina cual es el vigente.
    select count(*) into v_case_count
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id;
    if v_case_count > 1 then
        raise exception using errcode = 'P0001',
            message = 'claim_conversation_followup_ambiguous_case';
    end if;

    select commercial_case.* into v_case
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id
    for update;
    if v_case.id is null then
        outcome := 'not_found';
        return next;
        return;
    end if;

    -- Una derivacion que ninguna persona atendio es trabajo del equipo, no una
    -- venta que se enfrio (mismo predicado que la reactivacion desde
    -- 20260927000300).
    if exists (
        select 1
        from public.human_handoff_requests request
        where request.commercial_case_id = v_case.id
          and request.status in ('requested', 'projected', 'projection_failed')
          and request.attended_at is null
    ) then
        outcome := 'blocked_pending_handoff';
        return next;
        return;
    end if;

    if v_conversation.human_takeover
       or v_conversation.status in (
            'snoozed', 'paused_human', 'completed', 'closed', 'blocked'
       ) then
        outcome := 'blocked_conversation';
        return next;
        return;
    end if;

    select contact.* into v_contact
    from public.contacts contact
    where contact.id = v_conversation.contact_id;
    if v_contact.id is null
       or v_contact.contact_permission in ('opted_out', 'blocked', 'restricted')
       or v_contact.lifecycle_status = 'do_not_contact' then
        outcome := 'blocked_contact';
        return next;
        return;
    end if;

    -- Ya compro, por cualquier oferta y por cualquier camino. La guarda de la
    -- reserva del link mira solo el telefono exacto contra purchase_intents;
    -- aca se suma el mail y las identidades de las compras que Hotmart aviso,
    -- y el telefono se compara por los ultimos 9 digitos: Hotmart puede
    -- guardarlo sin codigo de pais. Equivocarse para el lado de no mandar un
    -- cupon es el error barato.
    if exists (
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
    ) then
        outcome := 'purchase_already_approved';
        return next;
        return;
    end if;

    -- El link: la misma emision que usa el agente, anclada en nuestro ultimo
    -- mensaje. La RPC repite sus propias barreras (caso, scope, contacto,
    -- opt-out, conversacion, identidad, compra) y resuelve la oferta del lead.
    select reservation.* into v_reservation
    from public.reserve_chatwoot_checkout_issuance_v2(
        v_case.id,
        p_external_user_id,
        p_chatwoot_account_id,
        p_chatwoot_inbox_id,
        p_external_conversation_id,
        p_last_outbound_message_id::text,
        p_issuance_ulid,
        v_now
    ) reservation;
    -- 'reserved' incluye la reserva que quedo de un intento anterior que no
    -- llego a autorizar el envio: se reusa el mismo link. Cualquier otro
    -- estado (ya en vuelo, entregado, incierto) no se vuelve a mandar.
    if v_reservation.outcome is distinct from 'reserved'
       or v_reservation.issuance_id is null then
        outcome := 'issuance_' || coalesce(v_reservation.outcome, 'missing');
        return next;
        return;
    end if;

    insert into public.conversation_followup_events (
        conversation_id, commercial_case_id, external_conversation_id,
        command_key, regime, status, template_name, template_language,
        coupon_code, last_inbound_message_id, last_outbound_message_id,
        inbound_age_seconds, checkout_issuance_id, created_at
    ) values (
        v_conversation.id, v_case.id, p_external_conversation_id,
        p_command_key, p_regime, 'claimed',
        btrim(p_template_name), btrim(p_template_language),
        p_coupon_code, p_last_inbound_message_id, p_last_outbound_message_id,
        p_inbound_age_seconds, v_reservation.issuance_id, v_now
    )
    returning id into v_event_id;

    outcome := 'claimed';
    followup_event_id := v_event_id;
    checkout_issuance_id := v_reservation.issuance_id;
    checkout_url_final := v_reservation.checkout_url_final;
    sck_value := v_reservation.sck_value;
    return next;
end;
$function$;

-- 3. Cerrar la reserva con lo que Chatwoot respondio.

create or replace function public.settle_conversation_followup_v1(
    p_command_key text,
    p_status text,
    p_provider_message_id bigint default null,
    p_failure_reason text default null,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    followup_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_existing public.conversation_followup_events%rowtype;
begin
    if p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_status is null
       or p_status not in ('sent', 'failed')
       or (p_status = 'failed' and p_provider_message_id is not null)
       or (p_status = 'sent' and p_provider_message_id is null)
       or (p_provider_message_id is not null and p_provider_message_id <= 0)
       or (p_failure_reason is not null
           and length(btrim(p_failure_reason)) not between 1 and 200) then
        raise exception using errcode = '22023',
            message = 'settle_conversation_followup_invalid_input';
    end if;

    select event.* into v_existing
    from public.conversation_followup_events event
    where event.command_key = p_command_key
      and event.status = 'claimed'
    for update;
    if v_existing.id is null then
        -- O nunca se reservo, o ya se cerro: ninguna de las dos justifica
        -- reescribir una fila terminal.
        outcome := 'not_found';
        return next;
        return;
    end if;

    update public.conversation_followup_events event
    set status = p_status,
        provider_message_id = case
            when p_status = 'sent' then p_provider_message_id else null end,
        failure_reason = case
            when p_status = 'failed'
                then coalesce(btrim(p_failure_reason), 'unspecified')
            else null end,
        settled_at = coalesce(p_now, clock_timestamp())
    where event.id = v_existing.id;

    outcome := 'settled';
    followup_event_id := v_existing.id;
    return next;
end;
$function$;

-- Postgres concede execute a PUBLIC en toda funcion nueva: sin estos revoke,
-- una RPC security definer que autoriza mandarle un WhatsApp con un cupon a un
-- lead queda al alcance de anon.
revoke all on function public.claim_conversation_followup_v1(
    bigint, bigint, bigint, text, text, text, text, text, text, text,
    bigint, bigint, integer, text, timestamptz
) from public;
revoke all on function public.settle_conversation_followup_v1(
    text, text, bigint, text, timestamptz
) from public;
revoke all on table public.conversation_followup_events from public;

do $roles$
begin
    if to_regrole('anon') is not null then
        revoke all on function public.claim_conversation_followup_v1(
            bigint, bigint, bigint, text, text, text, text, text, text, text,
            bigint, bigint, integer, text, timestamptz
        ) from anon;
        revoke all on function public.settle_conversation_followup_v1(
            text, text, bigint, text, timestamptz
        ) from anon;
        revoke all on table public.conversation_followup_events from anon;
    end if;
    if to_regrole('authenticated') is not null then
        revoke all on function public.claim_conversation_followup_v1(
            bigint, bigint, bigint, text, text, text, text, text, text, text,
            bigint, bigint, integer, text, timestamptz
        ) from authenticated;
        revoke all on function public.settle_conversation_followup_v1(
            text, text, bigint, text, timestamptz
        ) from authenticated;
        revoke all on table public.conversation_followup_events from authenticated;
    end if;
    if to_regrole('service_role') is not null then
        revoke all on table public.conversation_followup_events from service_role;
        grant execute on function public.claim_conversation_followup_v1(
            bigint, bigint, bigint, text, text, text, text, text, text, text,
            bigint, bigint, integer, text, timestamptz
        ) to service_role;
        grant execute on function public.settle_conversation_followup_v1(
            text, text, bigint, text, timestamptz
        ) to service_role;
    end if;
end
$roles$;

commit;
