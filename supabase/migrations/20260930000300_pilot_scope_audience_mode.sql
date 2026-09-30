-- Audiencia del scope del piloto: cohorte manual o intencion con
-- consentimiento (bloque C, decisiones D1 y D8).
--
-- Hasta aca un scope del piloto solo le escribia a un contacto que un operador
-- inscribia a mano en la cohorte. Para una instancia en produccion eso no
-- escala, y Johanna, que ya esta en produccion, le escribe a quien dejo el
-- formulario con consentimiento de WhatsApp. Esta migracion:
--
-- 1. suma pilot_scope_versions.audience_mode, parte de la version publicada
--    (inmutable), con tres valores:
--    - manual_cohort (por defecto, el de todo scope anterior): exige la
--      cohorte y no mira la intencion. Es exactamente lo de hoy;
--    - consented_intent_in_cohort: exige la cohorte Y la intencion con
--      consentimiento. Es el modo del E2E controlado con un solo telefono;
--    - consented_intent: no mira la cohorte y exige la intencion con
--      consentimiento. Es el modo de produccion.
--    Los modos con consentimiento se admiten con source hotmart o landing (el
--    primer contacto del formulario los va a usar). Los topes total y diario
--    siguen siendo el freno en los tres modos;
-- 2. suma a pilot_recovery_case_bindings el modo y la evidencia con que entro
--    cada caso: audience_mode, audience_purchase_intent_id y
--    audience_precheckout_submission_id (el envio 1.1.0 del formulario que dio
--    el consentimiento, con su copy_version), con un check de forma:
--    manual_cohort sin evidencia, los otros dos con las dos;
-- 3. suma _lancemos_pilot_audience_intent, un helper privado que ata la
--    evidencia al evento: la intencion con la que se correlaciono el evento
--    tiene que ser del tenant, el producto y la oferta del scope, de la misma
--    oferta del evento y con su mapeo activo en hotmart_purchase_intent_scopes;
--    despues aplica el criterio de Johanna sin sus valores fijos
--    (_portable_consented_intent_reason, 20260930000100). Devuelve un motivo
--    distinto por cada cosa que falla y, si pasa, la intencion y el envio;
-- 4. copia de su definicion vigente las cuatro funciones que toca, con los
--    cambios entre comentarios pilot_audience y nada mas:
--    - evaluate_lancemos_pilot_scope (20260929000200): la cohorte se exige
--      solo en los modos que la usan. En consented_intent devuelve allowed
--      sin mirar la intencion: sus llamadores (los planificadores) atan la
--      evidencia al evento en la misma transaccion, y la evaluacion nunca fue
--      un permiso de envio;
--    - authorize_lancemos_pilot_request_start (20260929000200): la cohorte
--      solo en los modos que la usan; fuera de manual_cohort re-verifica la
--      intencion del binding contra la identidad seleccionada, con lock
--      compartido de la fila de la intencion, ANTES de consumir presupuesto,
--      y el evento de control suma audience_mode, audience_purchase_intent_id
--      y el envio que dio el consentimiento al arrancar. En manual_cohort el
--      data queda identico;
--    - plan_lancemos_pilot_cart_recovery (20260810000300) y
--      plan_portable_payment_failure_recovery (20260930000100): fuera de
--      manual_cohort exigen la intencion del evento antes de planificar, y
--      escriben el modo, la intencion y el envio en el binding.
--
-- Un rechazo por audiencia al planificar sale como pilot_scope_rejected con el
-- motivo en detail, que el bridge guarda en webhook_events.processing_error
-- (A1, estado failed: D8). Al arrancar el envio sale como
-- pilot_request_start_rejected con el motivo en detail.
--
-- Johanna no pasa por estas funciones (LANCEMOS_PILOT_BOUNDARY_ENABLED=false).
-- Toda fila existente queda en manual_cohort, y en ese modo las cuatro
-- funciones ejecutan exactamente la rama de hoy: el helper nunca se llama y el
-- data de auditoria no cambia. No siembra filas de ningun cliente.
--
-- Locks: las dos FK nuevas del binding toman SHARE ROW EXCLUSIVE sobre
-- purchase_intents y precheckout_submissions hasta el commit. Johanna escribe
-- las dos en caliente (formulario, correlacion, compra): mientras la migracion
-- corre, esas escrituras esperan. La migracion no reescribe filas (las
-- columnas nuevas nacen con su valor por defecto o nulas) y los checks y las
-- FK recorren solo el scope y el binding, que son chicos, asi que la espera es
-- breve. Si una transaccion larga retiene alguna de las dos tablas, la
-- migracion falla por lock_timeout (5 s) sin dejar nada a medias y se
-- reintenta. Conviene aplicarla con poco trafico.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

alter table public.pilot_scope_versions
    add column audience_mode text not null default 'manual_cohort';

alter table public.pilot_scope_versions
    add constraint pilot_scope_versions_audience_mode_check
    check (
        audience_mode = 'manual_cohort'
        or (
            audience_mode in ('consented_intent_in_cohort', 'consented_intent')
            and source in ('hotmart', 'landing')
        )
    );

alter table public.pilot_recovery_case_bindings
    add column audience_mode text not null default 'manual_cohort',
    add column audience_purchase_intent_id uuid
        references public.purchase_intents(id) on delete restrict,
    add column audience_precheckout_submission_id uuid
        references public.precheckout_submissions(id) on delete restrict;

alter table public.pilot_recovery_case_bindings
    add constraint pilot_recovery_case_bindings_audience_shape
    check (
        (
            audience_mode = 'manual_cohort'
            and audience_purchase_intent_id is null
            and audience_precheckout_submission_id is null
        )
        or (
            audience_mode in ('consented_intent_in_cohort', 'consented_intent')
            and audience_purchase_intent_id is not null
            and audience_precheckout_submission_id is not null
        )
    );

create or replace function public._lancemos_pilot_audience_intent(
    p_scope_key text,
    p_scope_version integer,
    p_contact_id uuid,
    p_offer_code text,
    p_purchase_intent_id uuid,
    p_destination_phone text,
    out purchase_intent_id uuid,
    out precheckout_submission_id uuid,
    out reason_code text
)
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_scope public.pilot_scope_versions%rowtype;
    v_intent public.purchase_intents%rowtype;
    v_consent_reason text;
    v_consent_submission_id uuid;
begin
    -- La evidencia de audiencia de un modo con consentimiento: la intencion con
    -- la que el llamador ya ato el evento (la correlacion resuelta del evento
    -- de Hotmart, o la intencion del caso al autorizar). No la busca por
    -- contacto: una intencion de otra oferta, de otro producto o de otro
    -- telefono no cuenta, aunque sea de la misma persona.
    purchase_intent_id := null;
    precheckout_submission_id := null;

    select scope.* into v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';
    if not found
       or v_scope.audience_mode not in ('consented_intent_in_cohort', 'consented_intent')
       or p_contact_id is null
       or p_offer_code is null
       or btrim(p_offer_code) = '' then
        reason_code := 'pilot_audience_input_invalid';
        return;
    end if;

    if p_purchase_intent_id is null then
        reason_code := 'pilot_audience_intent_unresolved';
        return;
    end if;

    select intent.* into v_intent
    from public.purchase_intents intent
    where intent.id = p_purchase_intent_id;
    if not found
       or v_intent.tenant_ref is distinct from v_scope.tenant_key
       or v_intent.offer_ref is distinct from p_offer_code
       or (
           p_offer_code <> v_scope.offer_code
           and p_offer_code <> all(v_scope.additional_offer_codes)
       )
       or not exists (
           select 1
           from public.hotmart_purchase_intent_scopes mapping
           where mapping.active
             and mapping.tenant_ref = v_intent.tenant_ref
             and mapping.funnel_ref = v_intent.funnel_ref
             and mapping.hotmart_product_id = v_scope.external_product_id
             and lower(mapping.purchase_intent_product_ref) = lower(v_intent.product_ref)
             and mapping.offer_ref = v_intent.offer_ref
       ) then
        reason_code := 'pilot_audience_intent_scope_mismatch';
        return;
    end if;

    -- El criterio de Johanna sin sus valores fijos: intencion viva, con los dos
    -- permisos del formulario, del telefono de destino y de un contact_point
    -- del contacto, con un envio 1.1.0 con opt-in de WhatsApp y la
    -- copy_version del binding activo. Su motivo se conserva con el prefijo
    -- pilot_audience_ para no aplastar el diagnostico. El envio que devuelve
    -- (el ultimo que cumple) es la evidencia que se registra: su
    -- canonical_payload guarda la copy_version con que se dio el
    -- consentimiento.
    select consent.reason_code,
           consent.precheckout_submission_id
      into v_consent_reason,
           v_consent_submission_id
    from public._portable_consented_intent_reason(
        v_intent.id,
        p_contact_id,
        p_destination_phone
    ) consent;
    if v_consent_reason is distinct from 'consented_intent_ok' then
        reason_code := 'pilot_audience_'
            || coalesce(v_consent_reason, 'consented_intent_unknown');
        return;
    end if;

    purchase_intent_id := v_intent.id;
    precheckout_submission_id := v_consent_submission_id;
    reason_code := 'pilot_audience_allowed';
end;
$function$;

create or replace function public.evaluate_lancemos_pilot_scope(
    p_scope_key text,
    p_scope_version integer,
    p_tenant_key text,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_channel_provider text,
    p_channel_account_ref text,
    p_source text,
    p_source_event_type text,
    p_external_product_id text,
    p_offer_code text,
    p_contact_id uuid
)
returns table (
    allowed boolean,
    reason_code text,
    runtime_generation bigint
)
language plpgsql
security definer
stable
set search_path = public, pg_temp
as $function$
declare
    v_scope public.pilot_scope_versions%rowtype;
    v_control public.pilot_runtime_controls%rowtype;
    v_reason text;
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1
       or p_tenant_key is null or btrim(p_tenant_key) = ''
       or p_chatwoot_account_id is null or p_chatwoot_account_id < 1
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1
       or p_channel_provider is null or btrim(p_channel_provider) = ''
       or p_channel_account_ref is null or btrim(p_channel_account_ref) = ''
       or p_source is null or btrim(p_source) = ''
       or p_source_event_type is null or btrim(p_source_event_type) = ''
       or p_external_product_id is null or btrim(p_external_product_id) = ''
       or p_offer_code is null or btrim(p_offer_code) = ''
       or p_contact_id is null then
        return query select false, 'pilot_scope_input_invalid'::text, null::bigint;
        return;
    end if;

    select scope.* into v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';
    if not found then
        return query select false, 'pilot_scope_not_published'::text, null::bigint;
        return;
    end if;

    select control.* into v_control
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key;
    if not found or v_control.scope_version <> p_scope_version then
        return query select false, 'pilot_scope_version_mismatch'::text,
            coalesce(v_control.generation, null::bigint);
        return;
    end if;

    if v_control.runtime_state <> 'armed' then
        v_reason := 'pilot_runtime_not_armed';
    elsif p_tenant_key <> v_scope.tenant_key then
        v_reason := 'pilot_tenant_mismatch';
    elsif p_chatwoot_account_id <> v_scope.chatwoot_account_id then
        v_reason := 'pilot_chatwoot_account_mismatch';
    elsif p_chatwoot_inbox_id <> v_scope.chatwoot_inbox_id then
        v_reason := 'pilot_chatwoot_inbox_mismatch';
    elsif p_channel_provider <> v_scope.channel_provider
       or p_channel_account_ref <> v_scope.channel_account_ref then
        v_reason := 'pilot_channel_account_mismatch';
    elsif p_source <> v_scope.source
       or (
           p_source_event_type <> v_scope.source_event_type
           and p_source_event_type <> all(v_scope.additional_source_event_types)
       ) then
        v_reason := 'pilot_source_event_mismatch';
    elsif p_external_product_id <> v_scope.external_product_id then
        v_reason := 'pilot_product_mismatch';
    elsif p_offer_code <> v_scope.offer_code
       and p_offer_code <> all(v_scope.additional_offer_codes) then
        v_reason := 'pilot_offer_mismatch';
    -- pilot_audience: begin
    -- La cohorte se exige solo en los modos que la usan (manual_cohort y
    -- consented_intent_in_cohort). En consented_intent la evaluacion no mira
    -- la intencion: sus llamadores, los planificadores, la atan al evento en
    -- la misma transaccion (_lancemos_pilot_audience_intent) y la autorizacion
    -- del envio la vuelve a verificar. Esto no es un permiso de envio. Todo
    -- llamador SQL de esta funcion tiene que llamar tambien al helper:
    -- validate_pilot_scope_audience_mode.mjs lo exige.
    elsif v_scope.audience_mode = 'consented_intent' then
        v_reason := 'pilot_scope_allowed';
    -- pilot_audience: end
    elsif not exists (
        select 1 from public.pilot_cohort_memberships member
        where member.scope_key = p_scope_key
          and member.scope_version = p_scope_version
          and member.contact_id = p_contact_id
          and member.member_status = 'active'
    ) then
        v_reason := 'pilot_contact_not_in_cohort';
    else
        v_reason := 'pilot_scope_allowed';
    end if;

    return query select
        v_reason = 'pilot_scope_allowed',
        v_reason,
        v_control.generation;
end;
$function$;

create or replace function public.authorize_lancemos_pilot_request_start(
    p_scope_key text,
    p_scope_version integer,
    p_tenant_key text,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_channel_provider text,
    p_channel_account_ref text,
    p_source text,
    p_source_event_type text,
    p_external_product_id text,
    p_offer_code text,
    p_contact_id uuid,
    p_action_id uuid,
    p_attempt_id uuid,
    p_now timestamptz
)
returns table (
    authorized boolean,
    reason_code text,
    runtime_generation bigint,
    request_authorization_id uuid,
    replayed boolean
)
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_scope public.pilot_scope_versions%rowtype;
    v_control public.pilot_runtime_controls%rowtype;
    v_existing public.pilot_outbound_request_authorizations%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_case public.recovery_cases%rowtype;
    v_identity public.channel_identities%rowtype;
    v_local_date date;
    v_total integer;
    v_daily integer;
    v_authorization_id uuid;
    v_authorized_at timestamptz;
    v_control_exists boolean;
    v_reason text;
    -- pilot_audience: begin
    v_binding public.pilot_recovery_case_bindings%rowtype;
    v_audience_intent_id uuid;
    v_audience_submission_id uuid;
    v_audience_reason text;
    v_audience_data jsonb := '{}'::jsonb;
    -- pilot_audience: end
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1
       or p_tenant_key is null or btrim(p_tenant_key) = ''
       or p_chatwoot_account_id is null or p_chatwoot_account_id < 1
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1
       or p_channel_provider is null or btrim(p_channel_provider) = ''
       or p_channel_account_ref is null or btrim(p_channel_account_ref) = ''
       or p_source is null or btrim(p_source) = ''
       or p_source_event_type is null or btrim(p_source_event_type) = ''
       or p_external_product_id is null or btrim(p_external_product_id) = ''
       or p_offer_code is null or btrim(p_offer_code) = ''
       or p_contact_id is null or p_action_id is null or p_attempt_id is null
       or p_now is null then
        return query select false, 'pilot_scope_input_invalid'::text,
            null::bigint, null::uuid, false;
        return;
    end if;

    select scope.* into v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';
    if not found then
        return query select false, 'pilot_scope_not_published'::text,
            null::bigint, null::uuid, false;
        return;
    end if;

    select authrow.* into v_existing
    from public.pilot_outbound_request_authorizations authrow
    where authrow.attempt_id = p_attempt_id;

    if found and (
        v_existing.scope_key <> p_scope_key
        or v_existing.scope_version <> p_scope_version
        or v_existing.action_id <> p_action_id
        or v_existing.contact_id <> p_contact_id
    ) then
        return query select false, 'pilot_attempt_mismatch'::text,
            v_existing.runtime_generation, null::uuid, false;
        return;
    end if;

    if p_tenant_key <> v_scope.tenant_key then
        v_reason := 'pilot_tenant_mismatch';
    elsif p_chatwoot_account_id <> v_scope.chatwoot_account_id then
        v_reason := 'pilot_chatwoot_account_mismatch';
    elsif p_chatwoot_inbox_id <> v_scope.chatwoot_inbox_id then
        v_reason := 'pilot_chatwoot_inbox_mismatch';
    elsif p_channel_provider <> v_scope.channel_provider
       or p_channel_account_ref <> v_scope.channel_account_ref then
        v_reason := 'pilot_channel_account_mismatch';
    elsif p_source <> v_scope.source
       or (
           p_source_event_type <> v_scope.source_event_type
           and p_source_event_type <> all(v_scope.additional_source_event_types)
       ) then
        v_reason := 'pilot_source_event_mismatch';
    elsif p_external_product_id <> v_scope.external_product_id then
        v_reason := 'pilot_product_mismatch';
    elsif p_offer_code <> v_scope.offer_code
       and p_offer_code <> all(v_scope.additional_offer_codes) then
        v_reason := 'pilot_offer_mismatch';
    else
        v_reason := null;
    end if;

    if v_reason is not null then
        return query select false, v_reason,
            case when v_existing.id is null then null else v_existing.runtime_generation end,
            null::uuid, false;
        return;
    end if;

    if v_existing.id is not null then
        return query select true, 'pilot_request_start_authorized'::text,
            v_existing.runtime_generation, v_existing.id, true;
        return;
    end if;

    v_authorized_at := clock_timestamp();
    if p_now < v_authorized_at - interval '5 minutes'
       or p_now > v_authorized_at + interval '5 minutes' then
        return query select false, 'pilot_request_time_invalid'::text,
            null::bigint, null::uuid, false;
        return;
    end if;

    select control.* into v_control
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key
    for update;
    v_control_exists := found;

    select authrow.* into v_existing
    from public.pilot_outbound_request_authorizations authrow
    where authrow.attempt_id = p_attempt_id;
    if found then
        if v_existing.scope_key <> p_scope_key
           or v_existing.scope_version <> p_scope_version
           or v_existing.action_id <> p_action_id
           or v_existing.contact_id <> p_contact_id then
            return query select false, 'pilot_attempt_mismatch'::text,
                v_existing.runtime_generation, null::uuid, false;
            return;
        end if;
        return query select true, 'pilot_request_start_authorized'::text,
            v_existing.runtime_generation, v_existing.id, true;
        return;
    end if;

    if not v_control_exists or v_control.scope_version <> p_scope_version then
        return query select false, 'pilot_scope_version_mismatch'::text,
            coalesce(v_control.generation, null::bigint), null::uuid, false;
        return;
    end if;

    if v_control.runtime_state <> 'armed' then
        return query select false, 'pilot_runtime_not_armed'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    if
        -- pilot_audience: begin
        -- La cohorte, solo en los modos que la usan.
        v_scope.audience_mode <> 'consented_intent' and
        -- pilot_audience: end
        not exists (
        select 1 from public.pilot_cohort_memberships member
        where member.scope_key = p_scope_key
          and member.scope_version = p_scope_version
          and member.contact_id = p_contact_id
          and member.member_status = 'active'
    ) then
        return query select false, 'pilot_contact_not_in_cohort'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    select attempt.* into v_attempt
    from public.followup_delivery_attempts attempt
    where attempt.id = p_attempt_id
      and attempt.action_id = p_action_id;
    if not found or v_attempt.phase <> 'reserved' or v_attempt.outcome is not null then
        return query select false, 'pilot_attempt_mismatch'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    select recovery_case.* into v_case
    from public.scheduled_actions action
    join public.recovery_cases recovery_case
      on recovery_case.id = action.recovery_case_id
    where action.id = p_action_id
      and recovery_case.contact_id = p_contact_id;
    if not found
       or v_case.external_product_id <> p_external_product_id
       or v_case.offer_code is distinct from p_offer_code
       or v_case.source <> p_source
       or v_case.policy_key <> v_scope.policy_key
       or v_case.policy_version <> v_scope.policy_version
       or v_case.selected_channel_identity_id is null then
        return query select false, 'pilot_attempt_mismatch'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    select identity.* into v_identity
    from public.channel_identities identity
    where identity.id = v_case.selected_channel_identity_id
      and identity.contact_id = p_contact_id
      and identity.channel = 'whatsapp'
      and identity.identity_status = 'active';
    if not found
       or v_identity.account_id <> 'chatwoot:' || p_chatwoot_account_id::text
       or v_identity.metadata ->> 'inbox_id' <> p_chatwoot_inbox_id::text then
        return query select false, 'pilot_attempt_mismatch'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    -- pilot_audience: begin
    -- Fuera de manual_cohort, la intencion con la que entro el caso se vuelve
    -- a verificar contra la identidad seleccionada, antes de consumir
    -- presupuesto: si entre el plan y el envio la intencion se compro, paso a
    -- identity_conflict o perdio sus permisos, el request no arranca. El lock
    -- compartido cubre solo la fila de la intencion: espera a quien la este
    -- cambiando (la correlacion de una compra, un formulario posterior de la
    -- misma oferta) y el helper lee la version confirmada. Lo que no cambia
    -- esa fila (un conflicto abierto del envio, la copy_version o el estado
    -- del binding comercial, el mapeo de la oferta) no queda serializado: se
    -- ve si ya estaba confirmado al leerlo. El replay de arriba no pasa por
    -- aca: ese efecto ya cruzo.
    if v_scope.audience_mode <> 'manual_cohort' then
        select binding.* into v_binding
        from public.pilot_recovery_case_bindings binding
        where binding.recovery_case_id = v_case.id;
        if not found
           or v_binding.scope_key <> p_scope_key
           or v_binding.scope_version <> p_scope_version
           or v_binding.audience_mode <> v_scope.audience_mode then
            return query select false, 'pilot_attempt_mismatch'::text,
                v_control.generation, null::uuid, false;
            return;
        end if;

        perform 1
        from public.purchase_intents intent
        where intent.id = v_binding.audience_purchase_intent_id
        for share;

        select audience.purchase_intent_id,
               audience.precheckout_submission_id,
               audience.reason_code
          into v_audience_intent_id,
               v_audience_submission_id,
               v_audience_reason
        from public._lancemos_pilot_audience_intent(
            p_scope_key,
            p_scope_version,
            p_contact_id,
            p_offer_code,
            v_binding.audience_purchase_intent_id,
            v_identity.external_user_id
        ) audience;
        if v_audience_reason is distinct from 'pilot_audience_allowed' then
            return query select false,
                coalesce(v_audience_reason, 'pilot_audience_intent_unresolved'),
                v_control.generation, null::uuid, false;
            return;
        end if;

        v_audience_data := jsonb_build_object(
            'audience_mode', v_scope.audience_mode,
            'audience_purchase_intent_id', v_audience_intent_id,
            'audience_precheckout_submission_id', v_audience_submission_id
        );
    end if;
    -- pilot_audience: end

    v_local_date := (v_authorized_at at time zone v_scope.timezone)::date;

    select count(*)::integer into v_total
    from public.pilot_outbound_request_authorizations authrow
    where authrow.scope_key = p_scope_key;
    if v_total >= v_scope.max_outbound_request_starts_total then
        return query select false, 'pilot_total_budget_exhausted'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    select count(*)::integer into v_daily
    from public.pilot_outbound_request_authorizations authrow
    where authrow.scope_key = p_scope_key
      and authrow.local_budget_date = v_local_date;
    if v_daily >= v_scope.max_outbound_request_starts_per_day then
        return query select false, 'pilot_daily_budget_exhausted'::text,
            v_control.generation, null::uuid, false;
        return;
    end if;

    insert into public.pilot_outbound_request_authorizations (
        scope_key, scope_version, action_id, attempt_id, contact_id,
        local_budget_date, runtime_generation, reason_code, authorized_at
    ) values (
        p_scope_key, p_scope_version, p_action_id, p_attempt_id, p_contact_id,
        v_local_date, v_control.generation,
        'pilot_request_start_authorized', v_authorized_at
    ) returning id into v_authorization_id;

    insert into public.pilot_control_events (
        scope_key, scope_version, event_type, runtime_generation,
        contact_id, action_id, attempt_id, actor, reason_code,
        data
    ) values (
        p_scope_key, p_scope_version, 'pilot_outbound_request_authorized',
        v_control.generation, p_contact_id, p_action_id, p_attempt_id,
        'system', 'pilot_request_start_authorized',
        jsonb_build_object('local_budget_date', v_local_date)
        -- pilot_audience: begin
        -- En manual_cohort v_audience_data es '{}' y el data queda identico.
        || v_audience_data
        -- pilot_audience: end
    );

    return query select true, 'pilot_request_start_authorized'::text,
        v_control.generation, v_authorization_id, false;
end;
$function$;

create or replace function public.plan_lancemos_pilot_cart_recovery(
    p_webhook_event_id uuid,
    p_contact_id uuid,
    p_external_product_id text,
    p_product_name text,
    p_offer_code text,
    p_policy_key text,
    p_policy_version integer,
    p_abandoned_at timestamptz,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_external_user_id text,
    p_scope_key text,
    p_scope_version integer
)
returns table (
    recovery_case_id uuid,
    followup_sequence_id uuid,
    scheduled_action_id uuid,
    created boolean
)
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    v_scope public.pilot_scope_versions%rowtype;
    v_allowed boolean;
    v_reason text;
    v_generation bigint;
    v_recovery_case_id uuid;
    v_followup_sequence_id uuid;
    v_scheduled_action_id uuid;
    v_created boolean;
    v_binding public.pilot_recovery_case_bindings%rowtype;
    -- pilot_audience: begin
    v_audience_intent_id uuid;
    v_audience_submission_id uuid;
    v_audience_reason text;
    -- pilot_audience: end
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1
       or p_policy_key is null or btrim(p_policy_key) = ''
       or p_policy_version is null or p_policy_version < 1 then
        raise exception using
            errcode = '22023',
            message = 'invalid_pilot_plan_parameters';
    end if;

    -- Serialize planning with runtime pause/version activation and cohort changes.
    perform 1
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key
    for update;

    select scope.* into v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';

    select evaluation.allowed,
           evaluation.reason_code,
           evaluation.runtime_generation
      into v_allowed, v_reason, v_generation
    from public.evaluate_lancemos_pilot_scope(
        p_scope_key,
        p_scope_version,
        v_scope.tenant_key,
        p_chatwoot_account_id,
        p_chatwoot_inbox_id,
        v_scope.channel_provider,
        v_scope.channel_account_ref,
        'hotmart',
        'PURCHASE_OUT_OF_SHOPPING_CART',
        p_external_product_id,
        p_offer_code,
        p_contact_id
    ) evaluation;

    if not coalesce(v_allowed, false) then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = coalesce(v_reason, 'pilot_scope_unknown');
    end if;

    if v_scope.policy_key is distinct from p_policy_key
       or v_scope.policy_version is distinct from p_policy_version then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = 'pilot_policy_mismatch';
    end if;

    -- pilot_audience: begin
    -- Fuera de manual_cohort, el carrito entra solo con la intencion con
    -- consentimiento con la que la admision correlaciono ESTE evento, de la
    -- oferta del evento y del telefono al que se va a escribir. Sin
    -- correlacion resuelta no hay evidencia (pilot_audience_intent_unresolved).
    if v_scope.audience_mode <> 'manual_cohort' then
        select correlation.purchase_intent_id into v_audience_intent_id
        from public.hotmart_purchase_intent_correlations correlation
        where correlation.webhook_event_id = p_webhook_event_id
          and correlation.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
          and correlation.outcome = 'resolved';

        select audience.purchase_intent_id,
               audience.precheckout_submission_id,
               audience.reason_code
          into v_audience_intent_id,
               v_audience_submission_id,
               v_audience_reason
        from public._lancemos_pilot_audience_intent(
            p_scope_key,
            p_scope_version,
            p_contact_id,
            p_offer_code,
            v_audience_intent_id,
            p_external_user_id
        ) audience;
        if v_audience_reason is distinct from 'pilot_audience_allowed' then
            raise exception using
                errcode = '55000',
                message = 'pilot_scope_rejected',
                detail = coalesce(v_audience_reason, 'pilot_audience_intent_unresolved');
        end if;
    end if;
    -- pilot_audience: end

    select plan.recovery_case_id,
           plan.followup_sequence_id,
           plan.scheduled_action_id,
           plan.created
      into v_recovery_case_id,
           v_followup_sequence_id,
           v_scheduled_action_id,
           v_created
    from public.plan_cart_recovery_with_identity(
        p_webhook_event_id,
        p_contact_id,
        p_external_product_id,
        p_product_name,
        p_offer_code,
        p_policy_key,
        p_policy_version,
        p_abandoned_at,
        p_chatwoot_account_id,
        p_chatwoot_inbox_id,
        p_external_user_id
    ) plan;

    insert into public.pilot_recovery_case_bindings (
        recovery_case_id, scope_key, scope_version, source_event_id
        -- pilot_audience: begin
        , audience_mode, audience_purchase_intent_id,
        audience_precheckout_submission_id
        -- pilot_audience: end
    ) values (
        v_recovery_case_id, p_scope_key, p_scope_version, p_webhook_event_id
        -- pilot_audience: begin
        , v_scope.audience_mode, v_audience_intent_id,
        v_audience_submission_id
        -- pilot_audience: end
    ) on conflict on constraint pilot_recovery_case_bindings_pkey do nothing;

    select binding.* into strict v_binding
    from public.pilot_recovery_case_bindings binding
    where binding.recovery_case_id = v_recovery_case_id;
    if v_binding.scope_key <> p_scope_key
       or v_binding.scope_version <> p_scope_version
       or v_binding.source_event_id <> p_webhook_event_id then
        raise exception using
            errcode = '55000',
            message = 'pilot_case_binding_conflict';
    end if;

    return query select
        v_recovery_case_id,
        v_followup_sequence_id,
        v_scheduled_action_id,
        v_created;
end;
$function$;

create or replace function public.plan_portable_payment_failure_recovery(
    p_webhook_event_id uuid,
    p_contact_id uuid,
    p_external_product_id text,
    p_product_name text,
    p_offer_code text,
    p_policy_key text,
    p_policy_version integer,
    p_failed_at timestamptz,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_external_user_id text,
    p_scope_key text,
    p_scope_version integer
)
returns table (
    recovery_case_id uuid,
    followup_sequence_id uuid,
    scheduled_action_id uuid,
    created boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_scope public.pilot_scope_versions%rowtype;
    v_provenance public.commercial_ally_hotmart_event_bindings%rowtype;
    v_webhook public.webhook_events%rowtype;
    v_payment_failure public.commercial_ally_payment_failure_details%rowtype;
    v_purchase_intent public.purchase_intents%rowtype;
    v_runtime_binding public.commercial_ally_runtime_bindings%rowtype;
    v_canonical_failed_at timestamptz;
    v_allowed boolean;
    v_reason text;
    v_generation bigint;
    v_recovery_case_id uuid;
    v_followup_sequence_id uuid;
    v_scheduled_action_id uuid;
    v_created boolean;
    v_binding public.pilot_recovery_case_bindings%rowtype;
    -- consented_intent_grant: begin
    v_consent_reason text;
    v_consent_submission_id uuid;
    v_consent_now timestamptz;
    -- consented_intent_grant: end
    -- pilot_audience: begin
    v_audience_intent_id uuid;
    v_audience_submission_id uuid;
    v_audience_reason text;
    -- pilot_audience: end
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1
       or p_policy_key is null or btrim(p_policy_key) = ''
       or p_policy_version is null or p_policy_version < 1 then
        raise exception using
            errcode = '22023',
            message = 'invalid_pilot_plan_parameters';
    end if;

    select provenance.*
      into v_provenance
    from public.commercial_ally_hotmart_event_bindings provenance
    join public.webhook_events event
      on event.id = provenance.webhook_event_id
     and event.source = 'hotmart'
     and event.event_type = 'PURCHASE_CANCELED'
    where provenance.webhook_event_id = p_webhook_event_id;
    if not found then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_provenance_missing';
    end if;

    select event.*
      into v_webhook
    from public.webhook_events event
    where event.id = p_webhook_event_id
      and event.source = 'hotmart'
      and event.event_type = 'PURCHASE_CANCELED';
    v_canonical_failed_at := to_timestamp(
        (v_webhook.payload ->> 'creation_date')::double precision / 1000.0
    );
    if not found
       or p_failed_at is null
       or p_failed_at is distinct from v_canonical_failed_at then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_timestamp_mismatch';
    end if;

    select details.*
      into v_payment_failure
    from public.commercial_ally_payment_failure_details details
    where details.webhook_event_id = p_webhook_event_id
      and details.trigger_kind = 'payment_failure'
      and details.correlation_outcome = 'resolved'
      and details.purchase_intent_id is not null;
    if not found then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_correlation_unresolved';
    end if;

    select intent.*
      into v_purchase_intent
    from public.purchase_intents intent
    where intent.id = v_payment_failure.purchase_intent_id
      and intent.tenant_ref = v_provenance.tenant_ref
      and intent.funnel_ref = v_provenance.funnel_ref
      and intent.product_ref = v_provenance.purchase_intent_product_ref
      and intent.offer_ref = v_provenance.offer_ref
      and intent.normalized_phone = p_external_user_id;
    if not found then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_recipient_mismatch';
    end if;

    if not exists (
        select 1
        from public.contact_points point
        where point.contact_id = p_contact_id
          and point.type = 'phone'
          and point.normalized_value = v_purchase_intent.normalized_phone
          and point.source in ('hotmart', 'system')
    ) then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_contact_mismatch';
    end if;

    select binding.*
      into v_runtime_binding
    from public.commercial_ally_runtime_bindings binding
    where binding.tenant_ref = v_provenance.tenant_ref
      and binding.funnel_ref = v_provenance.funnel_ref
      and binding.binding_version = v_provenance.binding_version
      and binding.status = 'active';
    if not found then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_binding_unavailable';
    end if;

    -- Serialize planning with runtime pause/version activation and cohort changes.
    perform 1
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key
    for update;

    select scope.* into v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';

    if v_scope.tenant_key is distinct from v_provenance.tenant_ref
       or v_scope.external_product_id is distinct from v_provenance.hotmart_product_id
       or (
           v_provenance.offer_ref is distinct from v_scope.offer_code
           and v_provenance.offer_ref <> all(v_scope.additional_offer_codes)
       )
       or v_runtime_binding.hotmart_product_id::text is distinct from p_external_product_id
       or v_runtime_binding.product_name is distinct from p_product_name
       or p_offer_code is distinct from v_provenance.offer_ref
       or (
           p_offer_code is distinct from v_runtime_binding.offer_code
           and p_offer_code <> all(v_runtime_binding.additional_offer_codes)
       )
       or v_runtime_binding.chatwoot_account_id is distinct from p_chatwoot_account_id
       or v_runtime_binding.chatwoot_inbox_id is distinct from p_chatwoot_inbox_id then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_scope_binding_mismatch';
    end if;

    select evaluation.allowed,
           evaluation.reason_code,
           evaluation.runtime_generation
      into v_allowed, v_reason, v_generation
    from public.evaluate_lancemos_pilot_scope(
        p_scope_key,
        p_scope_version,
        v_scope.tenant_key,
        p_chatwoot_account_id,
        p_chatwoot_inbox_id,
        v_scope.channel_provider,
        v_scope.channel_account_ref,
        'hotmart',
        'PURCHASE_CANCELED',
        p_external_product_id,
        p_offer_code,
        p_contact_id
    ) evaluation;

    if not coalesce(v_allowed, false) then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = coalesce(v_reason, 'pilot_scope_unknown');
    end if;

    if v_scope.policy_key is distinct from p_policy_key
       or v_scope.policy_version is distinct from p_policy_version then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = 'pilot_policy_mismatch';
    end if;

    -- pilot_audience: begin
    -- Fuera de manual_cohort, el pago fallido entra solo si la intencion con la
    -- que se correlaciono (ya validada arriba contra el evento, la oferta y el
    -- telefono de destino) tiene el consentimiento vigente.
    if v_scope.audience_mode <> 'manual_cohort' then
        select audience.purchase_intent_id,
               audience.precheckout_submission_id,
               audience.reason_code
          into v_audience_intent_id,
               v_audience_submission_id,
               v_audience_reason
        from public._lancemos_pilot_audience_intent(
            p_scope_key,
            p_scope_version,
            p_contact_id,
            p_offer_code,
            v_purchase_intent.id,
            p_external_user_id
        ) audience;
        if v_audience_reason is distinct from 'pilot_audience_allowed' then
            raise exception using
                errcode = '55000',
                message = 'pilot_scope_rejected',
                detail = coalesce(v_audience_reason, 'pilot_audience_intent_unresolved');
        end if;
    end if;
    -- pilot_audience: end

    select plan.recovery_case_id,
           plan.followup_sequence_id,
           plan.scheduled_action_id,
           plan.created
      into v_recovery_case_id,
           v_followup_sequence_id,
           v_scheduled_action_id,
           v_created
    from public.plan_payment_failure_recovery_with_identity(
        p_webhook_event_id,
        p_contact_id,
        p_external_product_id,
        p_product_name,
        p_offer_code,
        p_policy_key,
        p_policy_version,
        v_canonical_failed_at,
        p_chatwoot_account_id,
        p_chatwoot_inbox_id,
        p_external_user_id
    ) plan;

    -- consented_intent_grant: begin
    -- El evento de pago fallido no concede permiso de contacto. Lo concede la
    -- intencion de compra con la que quedo correlacionado, si su formulario
    -- trae el consentimiento vigente de WhatsApp (opcion P1, el mismo criterio
    -- que Johanna sin sus valores fijos). El contacto se bloquea antes de leer
    -- los permisos: serializa con el opt-out, como el carrito en
    -- plan_cart_recovery_with_identity. Una fila activa, de cualquier estado,
    -- gana: nunca se pisa un opt-out ni se duplica el permiso en un replay.
    perform 1
    from public.contacts contact
    where contact.id = p_contact_id
    for update;

    select consent.reason_code,
           consent.precheckout_submission_id
      into v_consent_reason,
           v_consent_submission_id
    from public._portable_consented_intent_reason(
        v_purchase_intent.id,
        p_contact_id,
        p_external_user_id
    ) consent;

    v_consent_now := clock_timestamp();
    if v_consent_reason = 'consented_intent_ok'
       and not exists (
           select 1
           from public.contact_authorizations ca
           where ca.contact_id = p_contact_id
             and ca.channel = 'whatsapp'
             and ca.purpose = 'cart_recovery'
             and ca.valid_from <= v_consent_now
             and (ca.valid_until is null or ca.valid_until > v_consent_now)
       ) then
        insert into public.contact_authorizations (
            contact_id,
            channel,
            purpose,
            authorization_status,
            authorization_source,
            evidence,
            valid_from
        ) values (
            p_contact_id,
            'whatsapp',
            'cart_recovery',
            'allowed',
            'system',
            jsonb_build_object(
                'reason', 'precheckout_whatsapp_consent',
                'purchase_intent_id', v_purchase_intent.id,
                'precheckout_submission_id', v_consent_submission_id,
                'consent_copy_version', v_runtime_binding.consent_copy_version,
                'webhook_event_id', p_webhook_event_id,
                'recovery_case_id', v_recovery_case_id
            ),
            v_consent_now
        );
    end if;
    -- consented_intent_grant: end

    insert into public.pilot_recovery_case_bindings (
        recovery_case_id, scope_key, scope_version, source_event_id
        -- pilot_audience: begin
        , audience_mode, audience_purchase_intent_id,
        audience_precheckout_submission_id
        -- pilot_audience: end
    ) values (
        v_recovery_case_id, p_scope_key, p_scope_version, p_webhook_event_id
        -- pilot_audience: begin
        , v_scope.audience_mode, v_audience_intent_id,
        v_audience_submission_id
        -- pilot_audience: end
    ) on conflict on constraint pilot_recovery_case_bindings_pkey do nothing;

    select binding.* into strict v_binding
    from public.pilot_recovery_case_bindings binding
    where binding.recovery_case_id = v_recovery_case_id;
    if v_binding.scope_key <> p_scope_key
       or v_binding.scope_version <> p_scope_version then
        raise exception using
            errcode = '55000',
            message = 'pilot_case_binding_conflict';
    end if;

    return query select
        v_recovery_case_id,
        v_followup_sequence_id,
        v_scheduled_action_id,
        v_created;
end;
$function$;

-- create or replace conserva los grants, pero Supabase le da execute por
-- defecto a toda funcion nueva: el helper se revoca a todos, authorize sigue
-- sin ningun rol de la API (solo la llaman los dos mark_*_request_started), y
-- las otras tres se reafirman como entrypoints solo de service_role.
revoke all on function public._lancemos_pilot_audience_intent(text,integer,uuid,text,uuid,text) from public;
revoke all on function public.evaluate_lancemos_pilot_scope(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid) from public;
revoke all on function public.authorize_lancemos_pilot_request_start(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid,uuid,uuid,timestamptz) from public;
revoke all on function public.plan_lancemos_pilot_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) from public;
revoke all on function public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) from public;
do $roles$
declare v_role text;
begin
 for v_role in select rolname from pg_roles where rolname in ('anon','authenticated','service_role') loop
  execute format('revoke all on function public._lancemos_pilot_audience_intent(text,integer,uuid,text,uuid,text) from %I',v_role);
  execute format('revoke all on function public.evaluate_lancemos_pilot_scope(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid) from %I',v_role);
  execute format('revoke all on function public.authorize_lancemos_pilot_request_start(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid,uuid,uuid,timestamptz) from %I',v_role);
  execute format('revoke all on function public.plan_lancemos_pilot_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) from %I',v_role);
  execute format('revoke all on function public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) from %I',v_role);
 end loop;
 if exists(select 1 from pg_roles where rolname='service_role') then
  grant execute on function public.evaluate_lancemos_pilot_scope(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid) to service_role;
  grant execute on function public.plan_lancemos_pilot_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) to service_role;
  grant execute on function public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) to service_role;
 end if;
end;
$roles$;

commit;
