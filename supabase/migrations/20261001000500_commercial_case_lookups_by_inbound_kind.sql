-- Migration: en una conversacion adoptada por la admision portable, las RPC
-- que buscan el caso de la conversacion miran solo el inbound_sales (H7,
-- paso 2).
--
-- Que problema resuelve. La admision portable (20261001000400) adopta la
-- conversacion en la que salio una plantilla del piloto y crea ahi el caso
-- inbound_sales de la respuesta. Esa conversacion ya tenia un caso: el trigger
-- de sombra (20260816000100) copia cada recovery_case a commercial_cases como
-- cart_recovery, con la misma conversation_id. Quedan dos casos, y cuatro RPC
-- cuentan los casos de la conversacion sin mirar el tipo y abortan con
-- P0001 *_ambiguous_case:
--
--   * mark_human_handoff_attended      (vigente en 20260927000300)
--   * claim_conversation_reactivation  (vigente en 20260927000300)
--   * resume_paused_conversation       (vigente en 20260927000300)
--   * claim_conversation_followup_v1   (vigente en 20260928000400)
--
-- Medido en PGlite despues de adoptar y derivar: la marca de atencion y la
-- reanudacion daban P0001. Una conversacion derivada ("Necesito ayuda")
-- quedaba con el equipo para siempre, y ni el macro de reanudacion la
-- devolvia al agente.
--
-- Que hace. Redefine las cuatro, copiadas de su definicion vigente, con un
-- solo cambio: si la conversacion tiene un conversation_events
-- 'inbound_adopted_template_conversation', el conteo y el select del caso
-- miran solo case_kind = 'inbound_sales'. Ninguna funcion ni el bridge
-- escriben ese evento salvo la 000400, en la misma transaccion en la que la v2
-- crea el inbound_sales; la tabla no lo impone (event_type no tiene check y
-- service_role conserva el DML), asi que un insert a mano de ese evento en una
-- conversacion la pasaria a este filtro. Sin ese evento hacen exactamente lo
-- de antes: cuentan todos los casos. La guarda de ambiguedad se mantiene: dos
-- inbound_sales en una conversacion adoptada siguen dando P0001.
--
-- Por que condicionado al evento y no para todas. La medicion de solo lectura
-- en la base de Johanna (2026-10-01, autorizada por Dan) dio 2 conversaciones
-- con un caso que no es inbound_sales (un cart_recovery cada una) y ninguna
-- con mas de un caso. Un filtro incondicional les cambiaria el resultado: su
-- unico caso pasaria a not_found. Johanna nunca tiene el evento de adopcion,
-- porque su bridge no tiene manifiesto y no llama a la 000400, asi que para
-- ella las cuatro funciones devuelven lo mismo que antes, fila por fila. Que
-- siga asi se verifica: el count de ese evento en su base da 0 despues de
-- aplicar y tiene que seguir en 0 (docs/instalar.md).
--
-- El plan. El evento se busca una sola vez por llamada, por el indice
-- conversation_events_conversation_time_idx, despues de bloquear la
-- conversacion y antes del conteo: el conteo y el select usan la misma
-- decision.
--
-- Lo que NO hace: no toca la 000400, ni el trigger de sombra, ni los casos
-- existentes, ni ninguna otra funcion. create or replace conserva el owner y
-- los permisos; igual se repiten el revoke y el grant (solo service_role),
-- como en 20260927000300. No siembra filas.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- 1. mark_human_handoff_attended, copiada de 20260927000300.

create or replace function public.mark_human_handoff_attended(
    p_external_conversation_id bigint,
    p_attended_at timestamptz,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    attended_count integer,
    attended_commercial_case_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_conversation public.conversations%rowtype;
    v_case public.commercial_cases%rowtype;
    v_case_count integer;
    v_inbound_only boolean;
    v_marked integer;
    v_attended_at timestamptz;
begin
    if p_external_conversation_id is null
       or p_external_conversation_id <= 0
       or p_attended_at is null then
        raise exception using errcode = '22023',
            message = 'mark_human_handoff_attended_invalid_input';
    end if;

    attended_count := 0;

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

    -- 20261001000500: si la admision portable adopto esta conversacion (H7),
    -- ahi conviven el caso de la plantilla (la sombra cart_recovery) y el
    -- inbound_sales de la respuesta, y el caso es el inbound_sales. Sin el
    -- evento de adopcion se cuentan todos los casos, como antes.
    v_inbound_only := exists (
        select 1
        from public.conversation_events adoption
        where adoption.conversation_id = v_conversation.id
          and adoption.event_type = 'inbound_adopted_template_conversation'
    );

    -- Falla cerrado ante ambiguedad, igual que las otras dos funciones.
    select count(*) into v_case_count
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id
      and (commercial_case.case_kind = 'inbound_sales' or not v_inbound_only);
    if v_case_count > 1 then
        raise exception using errcode = 'P0001',
            message = 'mark_human_handoff_attended_ambiguous_case';
    end if;

    select commercial_case.* into v_case
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id
      and (commercial_case.case_kind = 'inbound_sales' or not v_inbound_only);
    if v_case.id is null then
        outcome := 'not_found';
        return next;
        return;
    end if;

    -- Un reloj adelantado del lado de Chatwoot no puede grabar una atencion en
    -- el futuro: se recorta a ahora.
    v_attended_at := least(p_attended_at, coalesce(p_now, clock_timestamp()));

    update public.human_handoff_requests request
    set attended_at = v_attended_at,
        updated_at = coalesce(p_now, clock_timestamp())
    where request.commercial_case_id = v_case.id
      and request.attended_at is null
      and request.status in ('requested', 'projected', 'projection_failed')
      and request.created_at <= v_attended_at;
    get diagnostics v_marked = row_count;

    attended_count := v_marked;
    attended_commercial_case_id := v_case.id;
    outcome := case when v_marked > 0 then 'attended' else 'noop' end;
    return next;
end;
$function$;

-- 2. claim_conversation_reactivation, copiada de 20260927000300.

create or replace function public.claim_conversation_reactivation(
    p_external_conversation_id bigint,
    p_command_key text,
    p_reason_code text,
    p_template_name text,
    p_template_language text,
    p_last_inbound_message_id bigint,
    p_inbound_age_seconds integer,
    p_quiet_seconds integer default null,
    p_max_reactivations integer default 1,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    reactivation_event_id uuid,
    reactivated_conversation_id uuid,
    reactivated_commercial_case_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_conversation public.conversations%rowtype;
    v_case public.commercial_cases%rowtype;
    v_contact public.contacts%rowtype;
    v_existing public.conversation_reactivation_events%rowtype;
    v_case_count integer;
    v_inbound_only boolean;
    v_live_count integer;
    v_event_id uuid;
    v_now timestamptz;
begin
    if p_external_conversation_id is null
       or p_external_conversation_id <= 0
       or p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_reason_code not in ('outside_service_window', 'operator_request')
       or p_template_name is null
       or length(btrim(p_template_name)) not between 1 and 512
       or p_template_language is null
       or length(btrim(p_template_language)) not between 2 and 32
       or p_last_inbound_message_id is null
       or p_last_inbound_message_id <= 0
       or p_inbound_age_seconds is null
       or p_inbound_age_seconds < 0
       or (p_quiet_seconds is not null and p_quiet_seconds < 0)
       or p_max_reactivations is null
       or p_max_reactivations < 1 then
        raise exception using errcode = '22023',
            message = 'claim_conversation_reactivation_invalid_input';
    end if;

    -- Idempotencia. Una reserva viva con el mismo command_key significa que el
    -- envio ya se pidio: no se vuelve a pedir ni se toca su fila.
    select event.* into v_existing
    from public.conversation_reactivation_events event
    where event.command_key = p_command_key
      and event.status <> 'failed';
    if v_existing.id is not null then
        outcome := 'replayed';
        reactivation_event_id := v_existing.id;
        reactivated_conversation_id := v_existing.conversation_id;
        reactivated_commercial_case_id := v_existing.commercial_case_id;
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

    -- 20261001000500: si la admision portable adopto esta conversacion (H7),
    -- ahi conviven el caso de la plantilla (la sombra cart_recovery) y el
    -- inbound_sales de la respuesta, y el caso es el inbound_sales. Sin el
    -- evento de adopcion se cuentan todos los casos, como antes.
    v_inbound_only := exists (
        select 1
        from public.conversation_events adoption
        where adoption.conversation_id = v_conversation.id
          and adoption.event_type = 'inbound_adopted_template_conversation'
    );

    -- Falla cerrado ante ambiguedad, igual que resume_paused_conversation: un
    -- select ... into de plpgsql con dos filas toma una arbitraria en silencio.
    select count(*) into v_case_count
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id
      and (commercial_case.case_kind = 'inbound_sales' or not v_inbound_only);
    if v_case_count > 1 then
        raise exception using errcode = 'P0001',
            message = 'claim_conversation_reactivation_ambiguous_case';
    end if;

    select commercial_case.* into v_case
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id
      and (commercial_case.case_kind = 'inbound_sales' or not v_inbound_only)
    for update;
    if v_case.id is null then
        outcome := 'not_found';
        reactivated_conversation_id := v_conversation.id;
        return next;
        return;
    end if;

    -- Una derivacion que ninguna persona atendio todavia es trabajo pendiente
    -- del equipo, no una conversacion que se enfrio. Medido el 2026-09-27
    -- sobre produccion: 9 de los 10 envios de reactivacion salieron sobre
    -- conversaciones derivadas y sin atender, y en dos de ellas la persona
    -- habia pedido explicitamente hablar con alguien del equipo
    -- (detail_reason_code = 'explicit_human_request', conversaciones 186 y
    -- 185). Reactivar ahi le contesta con un bot a quien pidio un humano y
    -- gasta la unica plantilla que podia reabrir la ventana de 24 h.
    --
    -- El bridge ya no deberia llegar hasta aca con una derivacion viva: la
    -- saltea al evaluar el candidato. Este guard es la capa durable, la que no
    -- se puede evadir si el scanner se reescribe o alguien llama a la RPC a
    -- mano.
    if exists (
        select 1
        from public.human_handoff_requests request
        where request.commercial_case_id = v_case.id
          and request.status in ('requested', 'projected', 'projection_failed')
          and request.attended_at is null
    ) then
        outcome := 'blocked_pending_handoff';
        reactivated_conversation_id := v_conversation.id;
        reactivated_commercial_case_id := v_case.id;
        return next;
        return;
    end if;

    select contact.* into v_contact
    from public.contacts contact
    where contact.id = v_conversation.contact_id;
    if v_contact.id is null
       or v_contact.contact_permission in (
           'opted_out', 'blocked', 'restricted'
       ) then
        outcome := 'blocked_contact';
        reactivated_conversation_id := v_conversation.id;
        reactivated_commercial_case_id := v_case.id;
        return next;
        return;
    end if;

    -- Limite por conversacion. Cuenta las reservas vivas y las entregadas: a
    -- quien ya recibio la plantilla y no contesto no se le insiste.
    select count(*) into v_live_count
    from public.conversation_reactivation_events event
    where event.conversation_id = v_conversation.id
      and event.status in ('claimed', 'sent');
    if v_live_count >= p_max_reactivations then
        outcome := 'blocked_reactivation_limit';
        reactivation_event_id := null;
        reactivated_conversation_id := v_conversation.id;
        reactivated_commercial_case_id := v_case.id;
        return next;
        return;
    end if;

    v_now := coalesce(p_now, clock_timestamp());

    insert into public.conversation_reactivation_events (
        conversation_id, commercial_case_id, external_conversation_id,
        command_key, reason_code, status, template_name, template_language,
        last_inbound_message_id, inbound_age_seconds, quiet_seconds,
        previous_conversation_status,
        previous_conversation_automation_status,
        previous_human_takeover,
        created_at
    ) values (
        v_conversation.id, v_case.id, p_external_conversation_id,
        p_command_key, p_reason_code, 'claimed',
        btrim(p_template_name), btrim(p_template_language),
        p_last_inbound_message_id, p_inbound_age_seconds, p_quiet_seconds,
        v_conversation.status,
        v_conversation.automation_status,
        v_conversation.human_takeover,
        v_now
    )
    returning id into v_event_id;

    outcome := 'claimed';
    reactivation_event_id := v_event_id;
    reactivated_conversation_id := v_conversation.id;
    reactivated_commercial_case_id := v_case.id;
    return next;
end;
$function$;

-- 3. resume_paused_conversation, copiada de 20260927000300.

create or replace function public.resume_paused_conversation(
    p_external_conversation_id bigint,
    p_command_key text,
    p_reason_code text,
    p_quiet_seconds integer default null,
    p_max_resumes integer default 3,
    p_now timestamptz default clock_timestamp()
)
returns table (
    outcome text,
    resumed_conversation_id uuid,
    resumed_commercial_case_id uuid,
    resume_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_conversation public.conversations%rowtype;
    v_case public.commercial_cases%rowtype;
    v_contact public.contacts%rowtype;
    v_existing public.conversation_resume_events%rowtype;
    v_case_count integer;
    v_inbound_only boolean;
    v_resume_count integer;
    v_event_id uuid;
    v_now timestamptz;
begin
    if p_external_conversation_id is null
       or p_external_conversation_id <= 0
       or p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_reason_code not in (
           'inbound_after_quiet_period',
           'operator_request'
       )
       or (p_quiet_seconds is not null and p_quiet_seconds < 0)
       or p_max_resumes is null
       or p_max_resumes < 1 then
        raise exception using errcode = '22023',
            message = 'resume_paused_conversation_invalid_input';
    end if;

    -- Idempotencia: el mismo command_key no reactiva dos veces.
    select event.* into v_existing
    from public.conversation_resume_events event
    where event.command_key = p_command_key;
    if v_existing.id is not null then
        outcome := 'replayed';
        resumed_conversation_id := v_existing.conversation_id;
        resumed_commercial_case_id := v_existing.commercial_case_id;
        resume_event_id := v_existing.id;
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

    -- 20261001000500: si la admision portable adopto esta conversacion (H7),
    -- ahi conviven el caso de la plantilla (la sombra cart_recovery) y el
    -- inbound_sales de la respuesta, y el caso es el inbound_sales. Sin el
    -- evento de adopcion se cuentan todos los casos, como antes.
    v_inbound_only := exists (
        select 1
        from public.conversation_events adoption
        where adoption.conversation_id = v_conversation.id
          and adoption.event_type = 'inbound_adopted_template_conversation'
    );

    -- Falla cerrado ante ambiguedad: reactivar un caso arbitrario de dos seria
    -- un error silencioso.
    select count(*) into v_case_count
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id
      and (commercial_case.case_kind = 'inbound_sales' or not v_inbound_only);
    if v_case_count > 1 then
        raise exception using errcode = 'P0001',
            message = 'resume_paused_conversation_ambiguous_case';
    end if;

    select commercial_case.* into v_case
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id
      and (commercial_case.case_kind = 'inbound_sales' or not v_inbound_only)
    for update;
    if v_case.id is null then
        outcome := 'not_found';
        resumed_conversation_id := v_conversation.id;
        return next;
        return;
    end if;

    select contact.* into v_contact
    from public.contacts contact
    where contact.id = v_conversation.contact_id;
    if v_contact.id is null
       or v_contact.contact_permission in (
           'opted_out', 'blocked', 'restricted'
       ) then
        outcome := 'blocked_contact';
        resumed_conversation_id := v_conversation.id;
        resumed_commercial_case_id := v_case.id;
        return next;
        return;
    end if;

    -- Anti-loop: reactivar una conversacion que el agente vuelve a derivar la
    -- devolveria a la cola del equipo una y otra vez. Pasado el limite, la
    -- conversacion se queda con las personas.
    select count(*) into v_resume_count
    from public.conversation_resume_events event
    where event.conversation_id = v_conversation.id;
    if v_resume_count >= p_max_resumes then
        outcome := 'blocked_resume_limit';
        resumed_conversation_id := v_conversation.id;
        resumed_commercial_case_id := v_case.id;
        return next;
        return;
    end if;

    -- Una derivacion sin atender bloquea la reanudacion, por dos motivos
    -- distintos que esta condicion junta.
    --
    -- 'requested' y 'projection_failed' son trabajo en curso del proyector de
    -- notas: reanudar ahi se pisa con el, y ademas dejaria viva una fila que
    -- hace fallar al proximo handoff con
    -- 'inbound_handoff_live_request_conflict'. Eso ya era asi.
    --
    -- 'projected' con attended_at nulo es la derivacion que llego a Chatwoot y
    -- que ninguna persona contesto todavia. Antes del 2026-09-27 esta funcion
    -- la despausaba igual: apagaba human_takeover y devolvia la conversacion
    -- al agente sin que nadie hubiera atendido al lead. Medido ese dia en la
    -- conversacion 186: se despauso dos veces (26/09 17:08 y 27/09 18:11)
    -- sobre una derivacion por pedido explicito de humano que seguia sin
    -- respuesta. La derivacion se borraba sola.
    --
    -- La salida de este estado no es automatica y es a proposito: una persona
    -- contesta (y el bridge marca attended_at), o saca la etiqueta a mano, o
    -- corre el macro de reanudacion. En los tres casos la decision de devolver
    -- la conversacion al agente la toma un humano.
    if exists (
        select 1
        from public.human_handoff_requests request
        where request.commercial_case_id = v_case.id
          and (
              request.status in ('requested', 'projection_failed')
              or (request.status = 'projected' and request.attended_at is null)
          )
    ) then
        outcome := 'blocked_pending_handoff';
        resumed_conversation_id := v_conversation.id;
        resumed_commercial_case_id := v_case.id;
        return next;
        return;
    end if;

    -- Ya admisible: no hay nada que levantar.
    if v_case.status = 'active'
       and v_case.automation_status = 'draft_only'
       and v_conversation.status in (
           'active', 'awaiting_agent', 'awaiting_contact', 'snoozed'
       )
       and v_conversation.automation_status = 'draft_only'
       and not v_conversation.human_takeover then
        outcome := 'already_active';
        resumed_conversation_id := v_conversation.id;
        resumed_commercial_case_id := v_case.id;
        return next;
        return;
    end if;

    v_now := coalesce(p_now, clock_timestamp());

    update public.conversations conversation
    set status = 'active',
        automation_status = 'draft_only',
        human_takeover = false,
        version = conversation.version + 1,
        updated_at = v_now
    where conversation.id = v_conversation.id;
    if not found then
        raise exception using errcode = '40001',
            message = 'resume_paused_conversation_transition_rejected';
    end if;

    update public.commercial_cases commercial_case
    set status = 'active',
        automation_status = 'draft_only',
        version = commercial_case.version + 1,
        updated_at = v_now
    where commercial_case.id = v_case.id;
    if not found then
        raise exception using errcode = '40001',
            message = 'resume_paused_conversation_case_transition_rejected';
    end if;

    insert into public.conversation_resume_events (
        conversation_id, commercial_case_id, external_conversation_id,
        command_key, reason_code, quiet_seconds,
        previous_conversation_status,
        previous_conversation_automation_status,
        previous_human_takeover,
        previous_case_status,
        previous_case_automation_status,
        created_at
    ) values (
        v_conversation.id, v_case.id, p_external_conversation_id,
        p_command_key, p_reason_code, p_quiet_seconds,
        v_conversation.status,
        v_conversation.automation_status,
        v_conversation.human_takeover,
        v_case.status,
        v_case.automation_status,
        v_now
    )
    returning id into v_event_id;

    outcome := 'resumed';
    resumed_conversation_id := v_conversation.id;
    resumed_commercial_case_id := v_case.id;
    resume_event_id := v_event_id;
    return next;
end;
$function$;

-- 4. claim_conversation_followup_v1, copiada de 20260928000400.

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
    v_inbound_only boolean;
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

    -- 20261001000500: si la admision portable adopto esta conversacion (H7),
    -- ahi conviven el caso de la plantilla (la sombra cart_recovery) y el
    -- inbound_sales de la respuesta, y el caso es el inbound_sales. Sin el
    -- evento de adopcion se cuentan todos los casos, como antes.
    v_inbound_only := exists (
        select 1
        from public.conversation_events adoption
        where adoption.conversation_id = v_conversation.id
          and adoption.event_type = 'inbound_adopted_template_conversation'
    );

    -- Un select ... into de plpgsql con dos filas toma una arbitraria en
    -- silencio: con dos casos no se adivina cual es el vigente.
    select count(*) into v_case_count
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id
      and (commercial_case.case_kind = 'inbound_sales' or not v_inbound_only);
    if v_case_count > 1 then
        raise exception using errcode = 'P0001',
            message = 'claim_conversation_followup_ambiguous_case';
    end if;

    select commercial_case.* into v_case
    from public.commercial_cases commercial_case
    where commercial_case.conversation_id = v_conversation.id
      and (commercial_case.case_kind = 'inbound_sales' or not v_inbound_only)
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

-- Los permisos: create or replace los conserva, pero se repiten explicitos
-- para que el estado no dependa de lo que haya quedado de antes. Las cuatro
-- son entrypoints del bridge: solo service_role.
revoke all on function public.mark_human_handoff_attended(
    bigint, timestamptz, timestamptz
) from public;
revoke all on function public.claim_conversation_reactivation(
    bigint, text, text, text, text, bigint, integer, integer, integer, timestamptz
) from public;
revoke all on function public.resume_paused_conversation(
    bigint, text, text, integer, integer, timestamptz
) from public;
revoke all on function public.claim_conversation_followup_v1(
    bigint, bigint, bigint, text, text, text, text, text, text, text,
    bigint, bigint, integer, text, timestamptz
) from public;

do $roles$
begin
    if to_regrole('anon') is not null then
        revoke all on function public.mark_human_handoff_attended(
            bigint, timestamptz, timestamptz
        ) from anon;
        revoke all on function public.claim_conversation_reactivation(
            bigint, text, text, text, text, bigint, integer, integer, integer,
            timestamptz
        ) from anon;
        revoke all on function public.resume_paused_conversation(
            bigint, text, text, integer, integer, timestamptz
        ) from anon;
        revoke all on function public.claim_conversation_followup_v1(
            bigint, bigint, bigint, text, text, text, text, text, text, text,
            bigint, bigint, integer, text, timestamptz
        ) from anon;
    end if;
    if to_regrole('authenticated') is not null then
        revoke all on function public.mark_human_handoff_attended(
            bigint, timestamptz, timestamptz
        ) from authenticated;
        revoke all on function public.claim_conversation_reactivation(
            bigint, text, text, text, text, bigint, integer, integer, integer,
            timestamptz
        ) from authenticated;
        revoke all on function public.resume_paused_conversation(
            bigint, text, text, integer, integer, timestamptz
        ) from authenticated;
        revoke all on function public.claim_conversation_followup_v1(
            bigint, bigint, bigint, text, text, text, text, text, text, text,
            bigint, bigint, integer, text, timestamptz
        ) from authenticated;
    end if;
    if to_regrole('service_role') is not null then
        grant execute on function public.mark_human_handoff_attended(
            bigint, timestamptz, timestamptz
        ) to service_role;
        grant execute on function public.claim_conversation_reactivation(
            bigint, text, text, text, text, bigint, integer, integer, integer,
            timestamptz
        ) to service_role;
        grant execute on function public.resume_paused_conversation(
            bigint, text, text, integer, integer, timestamptz
        ) to service_role;
        grant execute on function public.claim_conversation_followup_v1(
            bigint, bigint, bigint, text, text, text, text, text, text, text,
            bigint, bigint, integer, text, timestamptz
        ) to service_role;
    end if;
end
$roles$;

commit;
