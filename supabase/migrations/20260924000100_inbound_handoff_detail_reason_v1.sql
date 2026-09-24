-- Migration: guardar el motivo real de una derivacion inbound y mostrarlo en la
-- nota privada que lee el equipo humano en Chatwoot.
--
-- Que problema resuelve. El 2026-09-24 a las 13:03:34 UTC el bridge derivo la
-- conversacion 158 de Chatwoot (inbox 9) a "johanna - revision humana". El
-- agente habia decidido bien -- send_payment_link, con la respuesta correcta
-- guardada en el shadow de la entrega -- pero deliver_checkout_issuance_v2
-- devolvio outcome='blocked' y el worker convirtio la propuesta en handoff. El
-- worker compone ahi un motivo fino, f"payment_link_{reason}", que no se
-- persiste en ningun lado:
--
--   1. src/bridge/inbound_handoff.py llama a esta RPC con el literal
--      'commercial_exception'. No es un descuido: primary_reason_code tiene un
--      CHECK cerrado de tres valores taxonomicos (explicit_human_request,
--      commercial_exception, policy_requires_human) y el motivo fino haria
--      fallar la llamada entera por parametros invalidos. El campo no esta mal
--      usado; falta un campo donde guardar el detalle.
--   2. El unico rastro del motivo era logger.info("payment_link_handoff
--      reason=%s"), y este bridge no emite INFO en produccion.
--
-- Consecuencia medida ese dia: para saber por que no habia salido el link hubo
-- que descartar a mano las diez guardas de reserve_chatwoot_checkout_issuance_v2
-- evaluandolas una por una contra los datos reales. El motivo resulto ser
-- purchase_already_approved -- el contacto ya habia comprado el producto el
-- 2026-09-23 a las 03:37 UTC, veinte horas antes de escribir "Enviame el
-- enlace" -- y el sistema habia hecho lo correcto. Nada de eso era legible: la
-- fila decia 'commercial_exception' y la nota privada de Chatwoot decia el mismo
-- texto fijo que reciben todas las derivaciones.
--
-- Estado de las filas al escribir esta migracion (2026-09-24 14:00 UTC):
--   human_handoff_requests           1 fila, la de la conversacion 158
--   human_handoff_request_evidence   0 filas
--
-- Que cambia:
--   a. human_handoff_requests.detail_reason_code, nullable, parte del snapshot
--      inmutable del request como el resto de su identidad.
--   b. inbound_handoff_reason_sentence(text): el codigo a una frase en
--      castellano para quien lee la nota. Un codigo que no este en el mapa no se
--      traga: se muestra crudo, porque un codigo feo en la nota es mejor que una
--      derivacion sin motivo.
--   c. request_inbound_human_handoff acepta p_detail_reason_code, lo persiste y
--      compone con el la nota privada antes de guardarla.
--
-- Que NO cambia:
--   - El CHECK de primary_reason_code. Sigue siendo la taxonomia de tres
--     valores; el detalle la complementa, no la reemplaza.
--   - El marcador [supportmagician-handoff:<request_id>:<template_key>:v<version>]
--     al pie de la nota. Es la clave de idempotencia con la que
--     ensure_private_handoff_note escanea el historial de la conversacion:
--     cero coincidencias habilitan un POST, una confirma que ya existe, mas de
--     una es conflicto. Sacarlo del texto haria que cada reintento posteara una
--     nota nueva.
--   - Las notas ya posteadas. El cuerpo se compone al crear el request y es
--     inmutable, asi que esto rige para las derivaciones nuevas.
--   - El cuerpo de la RPC, que se reescribe entero solamente porque cambia su
--     firma: salvo los puntos de arriba es identico al de
--     20260823000100_inbound_durable_handoff.sql.

begin;

alter table public.human_handoff_requests
    add column if not exists detail_reason_code text;

do $detail_check$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.human_handoff_requests'::regclass
          and conname = 'human_handoff_requests_detail_reason_code_check'
    ) then
        alter table public.human_handoff_requests
            add constraint human_handoff_requests_detail_reason_code_check
            check (
                detail_reason_code is null
                or detail_reason_code ~ '^[a-z][a-z0-9_]{0,99}$'
            );
    end if;
end;
$detail_check$;

comment on column public.human_handoff_requests.detail_reason_code is
    'Motivo fino compuesto por el worker, por ejemplo payment_link_purchase_already_approved. Complementa primary_reason_code, que es la taxonomia cerrada.';

create or replace function public.protect_human_handoff_request_identity()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $function$
begin
    if tg_op = 'DELETE'
       or new.id is distinct from old.id
       or new.commercial_case_id is distinct from old.commercial_case_id
       or new.recovery_case_id is distinct from old.recovery_case_id
       or new.conversation_id is distinct from old.conversation_id
       or new.source_action_id is distinct from old.source_action_id
       or new.source_attempt_id is distinct from old.source_attempt_id
       or new.command_key is distinct from old.command_key
       or new.primary_reason_code is distinct from old.primary_reason_code
       or new.detail_reason_code is distinct from old.detail_reason_code
       or new.requested_by is distinct from old.requested_by
       or new.projection_policy_key is distinct from old.projection_policy_key
       or new.projection_policy_version is distinct from old.projection_policy_version
       or new.scope_key is distinct from old.scope_key
       or new.scope_version is distinct from old.scope_version
       or new.inbound_scope_key is distinct from old.inbound_scope_key
       or new.inbound_scope_version is distinct from old.inbound_scope_version
       or new.chatwoot_account_id is distinct from old.chatwoot_account_id
       or new.chatwoot_inbox_id is distinct from old.chatwoot_inbox_id
       or new.external_conversation_id is distinct from old.external_conversation_id
       or new.expected_team_id is distinct from old.expected_team_id
       or new.note_template_key is distinct from old.note_template_key
       or new.note_template_version is distinct from old.note_template_version
       or new.private_note_body is distinct from old.private_note_body
       or new.created_at is distinct from old.created_at then
        raise exception using errcode = '55000',
            message = 'human_handoff_request_identity_is_immutable';
    end if;
    return new;
end;
$function$;

-- La frase que lee una persona en la nota privada de Chatwoot.
create or replace function public.inbound_handoff_reason_sentence(
    p_detail_reason_code text
)
returns text
language sql
immutable
set search_path = pg_catalog, public, pg_temp
as $function$
    select case p_detail_reason_code
        when 'payment_link_purchase_already_approved' then
            'el contacto ya compro este producto, asi que no se le envio un enlace de pago nuevo'
        when 'payment_link_disabled' then
            'el envio automatico de enlaces de pago esta apagado por configuracion'
        when 'payment_link_reply_rejected' then
            'el texto con el enlace de pago no paso la validacion de formato'
        when 'payment_link_builder_rejected' then
            'no se pudo construir un enlace de pago valido para este contacto'
        when 'payment_link_blocked_case' then
            'el caso comercial no estaba activo al intentar emitir el enlace'
        when 'payment_link_blocked_conversation' then
            'la conversacion ya estaba pausada o intervenida al intentar emitir el enlace'
        when 'payment_link_blocked_contact' then
            'el contacto esta bloqueado, dado de baja o marcado como no contactar'
        when 'payment_link_blocked_opt_out' then
            'el contacto pidio no recibir mas mensajes'
        when 'payment_link_blocked_identity' then
            'no se pudo confirmar que el numero de la conversacion sea el del caso'
        when 'payment_link_blocked_scope' then
            'la configuracion comercial del producto no esta publicada'
        when 'payment_link_missing_default_offer' then
            'el producto no tiene una oferta activa por defecto para responder por aqui'
        when 'payment_link_replay_conflict' then
            'ya existia un enlace emitido para este mensaje con otros datos'
        when 'payment_link_invalid_request' then
            'los datos de la conversacion no permitieron emitir el enlace'
        when 'payment_link_chatwoot_blocked' then
            'Chatwoot rechazo el envio del mensaje con el enlace'
        when 'payment_link_delivery_unknown' then
            'no se pudo confirmar si el enlace llego, asi que no se reintento solo'
        when 'direct_medication_guidance' then
            'el contacto pidio indicaciones sobre medicacion y eso lo responde una persona'
        else null
    end;
$function$;

drop function if exists public.request_inbound_human_handoff(
    uuid, text, text, text, integer, timestamptz
);

create function public.request_inbound_human_handoff(
    p_commercial_case_id uuid,
    p_command_key text,
    p_reason_code text,
    p_projection_policy_key text,
    p_projection_policy_version integer,
    p_now timestamptz default clock_timestamp(),
    p_detail_reason_code text default null
)
returns table (
    outcome text,
    handoff_request_id uuid,
    affected_actions integer,
    affected_attempts integer
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_contact_id uuid;
    v_case public.commercial_cases%rowtype;
    v_policy public.human_handoff_projection_policies%rowtype;
    v_admission public.inbound_commercial_case_admissions%rowtype;
    v_scope public.inbound_commercial_scope_versions%rowtype;
    v_identity public.channel_identities%rowtype;
    v_conversation public.conversations%rowtype;
    v_request public.human_handoff_requests%rowtype;
    v_now timestamptz;
    v_sentence text;
    v_note_body text;
begin
    if p_commercial_case_id is null
       or p_command_key is null
       or p_command_key !~ '^[a-z0-9:_-]{1,200}$'
       or p_reason_code not in (
           'explicit_human_request',
           'commercial_exception',
           'policy_requires_human'
       )
       or p_projection_policy_key is null
       or p_projection_policy_key !~ '^[a-z0-9_-]{1,100}$'
       or p_projection_policy_version is null
       or p_projection_policy_version < 1
       or p_now is null
       or (
           p_detail_reason_code is not null
           and p_detail_reason_code !~ '^[a-z][a-z0-9_]{0,99}$'
       ) then
        raise exception using errcode = '22023',
            message = 'invalid_inbound_human_handoff_parameters';
    end if;

    perform pg_advisory_xact_lock(
        hashtextextended('human_handoff_command:' || p_command_key, 0)
    );

    select request.* into v_request
    from public.human_handoff_requests request
    where request.command_key = p_command_key;

    -- El detalle queda fuera de esta comparacion a proposito: un reintento
    -- del mismo comando no debe fallar por el motivo, y el primero que llego
    -- es el que describe la derivacion.
    if v_request.id is not null then
        if v_request.commercial_case_id <> p_commercial_case_id
           or v_request.recovery_case_id is not null
           or v_request.primary_reason_code <> p_reason_code
           or v_request.requested_by <> 'agent'
           or v_request.source_action_id is not null
           or v_request.source_attempt_id is not null
           or v_request.projection_policy_key <> p_projection_policy_key
           or v_request.projection_policy_version <> p_projection_policy_version then
            raise exception using errcode = '23505',
                message = 'human_handoff_command_conflict';
        end if;
        outcome := 'already_requested';
        handoff_request_id := v_request.id;
        affected_actions := 0;
        affected_attempts := 0;
        return next;
        return;
    end if;

    select commercial_case.contact_id into v_contact_id
    from public.commercial_cases commercial_case
    where commercial_case.id = p_commercial_case_id
      and commercial_case.case_kind = 'inbound_sales';
    if v_contact_id is null then
        raise exception using errcode = 'P0002',
            message = 'inbound_handoff_commercial_case_not_found';
    end if;

    perform 1 from public.contacts contact
    where contact.id = v_contact_id
    for update;

    select commercial_case.* into v_case
    from public.commercial_cases commercial_case
    where commercial_case.id = p_commercial_case_id
      and commercial_case.contact_id = v_contact_id
      and commercial_case.case_kind = 'inbound_sales'
    for update;

    if v_case.id is null then
        raise exception using errcode = 'P0002',
            message = 'inbound_handoff_commercial_case_not_found';
    end if;
    if v_case.status <> 'active'
       or v_case.automation_status <> 'draft_only' then
        raise exception using errcode = '55000',
            message = 'inbound_handoff_case_not_active_draft_only';
    end if;

    select policy.* into v_policy
    from public.human_handoff_projection_policies policy
    where policy.policy_key = p_projection_policy_key
      and policy.policy_version = p_projection_policy_version
      and policy.active
      and policy.scope_key is null
      and policy.scope_version is null
      and policy.inbound_scope_key = v_case.inbound_scope_key
      and policy.inbound_scope_version = v_case.inbound_scope_version;
    if v_policy.id is null then
        raise exception using errcode = '55000',
            message = 'handoff_projection_policy_unavailable';
    end if;

    select admission.* into v_admission
    from public.inbound_commercial_case_admissions admission
    where admission.commercial_case_id = v_case.id
      and admission.contact_id = v_case.contact_id
      and admission.channel_identity_id = v_case.selected_channel_identity_id
      and admission.conversation_id = v_case.conversation_id
      and admission.scope_key = v_case.inbound_scope_key
      and admission.scope_version = v_case.inbound_scope_version
    for share;
    if v_admission.id is null then
        raise exception using errcode = '55000',
            message = 'inbound_handoff_admission_mismatch';
    end if;

    select scope.* into v_scope
    from public.inbound_commercial_scope_versions scope
    where scope.scope_key = v_admission.scope_key
      and scope.version = v_admission.scope_version
      and scope.status = 'published'
      and scope.tenant_key = v_case.tenant_ref
      and scope.external_product_id = v_case.product_ref
      and scope.offer_code = v_case.offer_ref
    for share;
    if v_scope.scope_key is null then
        raise exception using errcode = '55000',
            message = 'inbound_handoff_scope_unavailable';
    end if;

    select identity.* into v_identity
    from public.channel_identities identity
    where identity.id = v_case.selected_channel_identity_id
      and identity.contact_id = v_case.contact_id
      and identity.channel = 'whatsapp'
      and identity.identity_status = 'active'
      and identity.external_user_id = v_admission.external_user_id
      and identity.account_id = 'chatwoot:' || v_scope.chatwoot_account_id::text
      and identity.metadata ->> 'inbox_id' = v_scope.chatwoot_inbox_id::text
    for share;
    if v_identity.id is null then
        raise exception using errcode = '55000',
            message = 'inbound_handoff_identity_mismatch';
    end if;

    select conversation.* into v_conversation
    from public.conversations conversation
    where conversation.id = v_case.conversation_id
      and conversation.contact_id = v_case.contact_id
      and conversation.channel_identity_id = v_identity.id
      and conversation.commercial_context = jsonb_build_object(
          'chatwoot_conversation_id', v_admission.external_conversation_id::text
      )
      and conversation.status in (
          'active', 'awaiting_agent', 'awaiting_contact', 'snoozed'
      )
      and conversation.automation_status = 'draft_only'
    for update;
    if v_conversation.id is null then
        raise exception using errcode = '55000',
            message = 'inbound_handoff_conversation_mismatch';
    end if;

    select request.* into v_request
    from public.human_handoff_requests request
    where request.commercial_case_id = v_case.id
      and request.status in ('requested', 'projection_failed')
    for update;
    if v_request.id is not null then
        raise exception using errcode = '23505',
            message = 'inbound_handoff_live_request_conflict';
    end if;

    v_now := clock_timestamp();
    update public.commercial_cases commercial_case
    set status = 'paused',
        automation_status = 'disabled',
        version = commercial_case.version + 1,
        updated_at = v_now
    where commercial_case.id = v_case.id
      and commercial_case.status = 'active'
      and commercial_case.automation_status = 'draft_only';
    if not found then
        raise exception using errcode = '40001',
            message = 'inbound_handoff_case_transition_rejected';
    end if;

    update public.conversations conversation
    set status = 'paused_human',
        automation_status = 'paused',
        human_takeover = true,
        version = conversation.version + 1,
        updated_at = v_now
    where conversation.id = v_conversation.id
      and conversation.status in (
          'active', 'awaiting_agent', 'awaiting_contact', 'snoozed'
      )
      and conversation.automation_status = 'draft_only';
    if not found then
        raise exception using errcode = '40001',
            message = 'inbound_handoff_conversation_transition_rejected';
    end if;

    -- La nota que va a leer una persona: el texto fijo de la politica y,
    -- debajo, el motivo concreto de esta derivacion. Sin detalle queda
    -- exactamente como antes. El marcador de idempotencia lo agrega despues
    -- la proyeccion, sobre este cuerpo.
    v_note_body := v_policy.private_note_body;
    if p_detail_reason_code is not null then
        v_sentence := public.inbound_handoff_reason_sentence(p_detail_reason_code);
        v_note_body := v_note_body || E'\n\nMotivo: ' || coalesce(
            v_sentence || ' (' || p_detail_reason_code || ').',
            p_detail_reason_code || '.'
        );
        if char_length(v_note_body) > 1800 then
            v_note_body := left(v_note_body, 1800);
        end if;
    end if;

    insert into public.human_handoff_requests (
        commercial_case_id, recovery_case_id, conversation_id,
        source_action_id, source_attempt_id, command_key,
        primary_reason_code, detail_reason_code, requested_by,
        projection_policy_key, projection_policy_version,
        scope_key, scope_version, inbound_scope_key, inbound_scope_version,
        chatwoot_account_id, chatwoot_inbox_id, external_conversation_id,
        expected_team_id, note_template_key, note_template_version,
        private_note_body
    ) values (
        v_case.id, null, v_conversation.id,
        null, null, p_command_key,
        p_reason_code, p_detail_reason_code, 'agent',
        v_policy.policy_key, v_policy.policy_version,
        null, null, v_policy.inbound_scope_key, v_policy.inbound_scope_version,
        v_scope.chatwoot_account_id, v_scope.chatwoot_inbox_id,
        v_admission.external_conversation_id,
        v_policy.expected_team_id, v_policy.note_template_key,
        v_policy.note_template_version, v_note_body
    ) returning * into v_request;

    insert into public.human_handoff_projection_effects (
        handoff_request_id, effect_kind
    ) values
        (v_request.id, 'assignment'),
        (v_request.id, 'private_note');

    outcome := 'requested';
    handoff_request_id := v_request.id;
    affected_actions := 0;
    affected_attempts := 0;
    return next;
end;
$function$;

revoke execute on function public.request_inbound_human_handoff(
    uuid, text, text, text, integer, timestamptz, text
) from public;
revoke all on function public.inbound_handoff_reason_sentence(text) from public;

-- Los roles de la API de Supabase son opcionales en stacks de prueba neutrales.
do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke execute on function public.request_inbound_human_handoff(
            uuid, text, text, text, integer, timestamptz, text
        ) from anon;
        revoke all on function public.inbound_handoff_reason_sentence(text)
        from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke execute on function public.request_inbound_human_handoff(
            uuid, text, text, text, integer, timestamptz, text
        ) from authenticated;
        revoke all on function public.inbound_handoff_reason_sentence(text)
        from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.request_inbound_human_handoff(
            uuid, text, text, text, integer, timestamptz, text
        ) to service_role;
        -- La frase la resuelve la RPC, que es security definer: nadie la llama
        -- desde afuera y no tiene por que quedar expuesta.
        revoke all on function public.inbound_handoff_reason_sentence(text)
        from service_role;
    end if;
end;
$roles$;

commit;
