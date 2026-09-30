-- El permiso de contacto del pago fallido lo concede la intencion con
-- consentimiento (A5, decision D3 = opcion P1).
--
-- plan_portable_payment_failure_recovery ya exige que el evento quede
-- correlacionado con una intencion de compra del mismo telefono, pero no dejaba
-- ningun permiso de contacto: la reevaluacion real escalaba el caso con
-- contact_authorization_unknown y el pago fallido no podia salir. Los
-- validadores lo tapaban insertando el permiso a mano. Esta migracion:
--
-- 1. suma _portable_consented_intent_reason, el criterio de "intencion con
--    consentimiento" que usa Johanna (20260827000100) sin sus valores fijos: la
--    intencion viva, con whatsapp_contact_authorized y activation_authorized,
--    del telefono de destino y de un contact_point del contacto, con un envio
--    1.1.0 del formulario con whatsapp_contact y marketing_optin en true, la
--    copy_version del binding activo y ningun conflicto abierto. Devuelve un
--    motivo distinto por cada cosa que falla, para no borrar el diagnostico.
--    Dos condiciones de Johanna no se repiten, a proposito y documentado en
--    el cuerpo: la ventana entre el formulario y el evento (la garantiza la
--    correlacion) y el nombre y el producto del envio (las variables las
--    exige el dispatcher). No es un entrypoint: nadie mas que el owner la
--    ejecuta;
-- 2. plan_portable_payment_failure_recovery, copiada de su definicion vigente
--    (20260929000100), con dos cambios:
--    a. el contact_point del telefono puede venir de Hotmart o del sistema
--       (bootstrap de la identidad del formulario, 20260825000100); antes un
--       punto 'system' bloqueaba al de Hotmart por el indice unico y la
--       planificacion moria en payment_failure_contact_mismatch;
--    b. despues de planificar, con el contacto bloqueado, si el helper da
--       consented_intent_ok y no hay una fila de permiso activa, inserta
--       contact_authorizations allowed con fuente 'system' y la evidencia de la
--       intencion, el envio, la copy_version y el evento. Si el helper da otra
--       cosa no concede nada y la reevaluacion escala como hasta hoy.
--
-- El evento de Hotmart sigue sin conceder permiso por si solo. Johanna usa
-- admit_johanna_payment_failure y begin_johanna_payment_failure_hotmart_auto:
-- no pasa por esta funcion y no cambia. No siembra filas de ningun cliente.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

create or replace function public._portable_consented_intent_reason(
    p_purchase_intent_id uuid,
    p_contact_id uuid,
    p_destination_phone text,
    out reason_code text,
    out precheckout_submission_id uuid
)
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_intent public.purchase_intents%rowtype;
    v_binding public.commercial_ally_runtime_bindings%rowtype;
begin
    -- El criterio de Johanna (20260827000100) sin sus valores fijos. Dos de
    -- sus condiciones quedan afuera a proposito:
    -- 1. La ventana entre el formulario y el pago fallido (Johanna: el evento
    --    entre submitted_at y submitted_at + 24 h). La garantiza la
    --    correlacion: correlate_hotmart_purchase_intent solo resuelve una
    --    intencion con submitted_at en [observed_at - max_lookback,
    --    observed_at], con el max_lookback de hotmart_purchase_intent_scopes
    --    de la oferta, y plan_portable_payment_failure_recovery exige
    --    correlation_outcome = 'resolved' y pasa esa intencion. Un pago
    --    fallido fuera de la ventana no se correlaciona y no llega aca
    --    (validate_commercial_ally_payment_failure_recovery.mjs, caso F).
    -- 2. El nombre y el producto no vacios del envio (Johanna los usa como
    --    variables de su plantilla). En el camino portable las variables salen
    --    del contexto de ejecucion del caso, y el dispatcher las exige antes
    --    de enviar (template_parameters_missing). No son parte del permiso.
    precheckout_submission_id := null;

    if p_purchase_intent_id is null
       or p_contact_id is null
       or p_destination_phone is null
       or btrim(p_destination_phone) = '' then
        reason_code := 'consented_intent_input_invalid';
        return;
    end if;

    select intent.* into v_intent
    from public.purchase_intents intent
    where intent.id = p_purchase_intent_id;
    if not found then
        reason_code := 'consented_intent_not_found';
        return;
    end if;

    if v_intent.lifecycle_state <> 'waiting_for_purchase'
       or v_intent.provisional
       or not v_intent.provider_observed
       or coalesce(v_intent.current_classification, '') in (
           'identity_conflict', 'tracking_incomplete', 'expired_unknown'
       ) then
        reason_code := 'consented_intent_not_live';
        return;
    end if;

    if not v_intent.whatsapp_contact_authorized
       or not v_intent.activation_authorized then
        reason_code := 'consented_intent_not_authorized';
        return;
    end if;

    if v_intent.normalized_phone is null
       or v_intent.normalized_phone <> p_destination_phone
       or not exists (
           select 1
           from public.contact_points point
           where point.contact_id = p_contact_id
             and point.type = 'phone'
             and point.normalized_value = v_intent.normalized_phone
       ) then
        reason_code := 'consented_intent_phone_mismatch';
        return;
    end if;

    select binding.* into v_binding
    from public.commercial_ally_runtime_bindings binding
    where binding.tenant_ref = v_intent.tenant_ref
      and binding.funnel_ref = v_intent.funnel_ref
      and binding.status = 'active';
    if not found then
        reason_code := 'consented_intent_binding_unavailable';
        return;
    end if;

    select submission.id into precheckout_submission_id
    from public.purchase_intent_submissions link
    join public.precheckout_submissions submission
      on submission.id = link.submission_id
    where link.purchase_intent_id = v_intent.id
      and submission.contract_version = '1.1.0'
      and not submission.provisional
      and submission.provider_observed
      and submission.activation_authorized
      and submission.canonical_payload #>> '{consent,whatsapp_contact}' = 'true'
      and submission.canonical_payload #>> '{consent,marketing_optin}' = 'true'
      and submission.canonical_payload #>> '{consent,copy_version}'
          = v_binding.consent_copy_version
      and submission.canonical_payload #>> '{identity,phone}'
          = v_intent.normalized_phone
      and submission.canonical_payload #>> '{commerce,offer_ref}'
          = v_intent.offer_ref
      and not exists (
          select 1
          from public.precheckout_submission_conflicts conflict
          where conflict.existing_submission_id = submission.id
            and conflict.resolved_at is null
      )
    order by link.ordinal desc
    limit 1;
    if precheckout_submission_id is null then
        reason_code := 'consented_intent_submission_missing';
        return;
    end if;

    reason_code := 'consented_intent_ok';
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
    ) values (
        v_recovery_case_id, p_scope_key, p_scope_version, p_webhook_event_id
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
-- defecto a toda funcion nueva: el helper se revoca a todos, y la RPC se
-- reafirma como entrypoint solo de service_role.
revoke all on function public._portable_consented_intent_reason(uuid,uuid,text) from public;
revoke all on function public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) from public;
do $roles$
declare v_role text;
begin
 for v_role in select rolname from pg_roles where rolname in ('anon','authenticated','service_role') loop
  execute format('revoke all on function public._portable_consented_intent_reason(uuid,uuid,text) from %I',v_role);
  execute format('revoke all on function public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) from %I',v_role);
 end loop;
 if exists(select 1 from pg_roles where rolname='service_role') then
  grant execute on function public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) to service_role;
 end if;
end;
$roles$;

commit;
