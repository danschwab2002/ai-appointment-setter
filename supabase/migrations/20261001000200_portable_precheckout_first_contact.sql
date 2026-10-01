-- Primer contacto portable tras el formulario (flujo precheckout, decisiones
-- D1, D2, D5, D20, D21, D24, D25 y D26).
--
-- Hasta aca el formulario de la landing de una instancia portable solo dejaba
-- la intencion de compra: quien no llegaba al checkout no recibia nada, porque
-- los dos planificadores del piloto arrancan de un evento de Hotmart (carrito
-- o pago fallido). Esta migracion suma el tercer disparador: el envio del
-- formulario con consentimiento de WhatsApp planifica un unico primer contacto,
-- demorado, que se cancela solo si en el medio la persona compra, llega su
-- carrito o su pago fallido, se da de baja o pierde el consentimiento.
--
-- 1. Tres checks suman un valor y nada mas: recovery_cases.source admite
--    landing; recovery_case_events.event_role y followup_sequences.reason
--    admiten precheckout_intent. scheduled_actions.anchor_type no tiene check.
-- 2. portable_precheckout_first_contact_plans: un renglon por envio admitido
--    con el resultado del plan (planned, not_planned, plan_failed) y su motivo.
--    Solo ids y codigos: sin nombre, email ni telefono.
-- 3. _portable_precheckout_stop_reason: el motivo por el que un primer contacto
--    no sale. Lo usan el planificador, la reevaluacion y el arranque del envio:
--    intencion comprada o no viva, compra ambigua, compra admitida del binding
--    con la misma identidad (sin ventana), carrito o pago fallido que lo
--    reemplazan, opt-out previo en cualquiera de las dos formas del telefono, y
--    una conversacion del contacto derivada a una persona, pausada, cerrada o
--    bloqueada (el criterio blocked_handoff del primer toque de Johanna).
-- 4. _find_portable_precheckout_contact y _ensure_portable_precheckout_contact:
--    el contacto se busca por punto de email, punto de telefono (las dos
--    formas), identidad de WhatsApp de la cuenta (las dos formas) y email del
--    contacto; dos duenos distintos no se planifican. Si no existe se crea, y a
--    uno existente se le completan telefono, nombre y email nulos (el contacto
--    que nacio de un mensaje entrante no tiene ninguno) y los puntos, con
--    fuente system.
-- 5. _plan_portable_precheckout_first_contact: con el control del scope
--    bloqueado, evalua el scope y la audiencia (evaluate_lancemos_pilot_scope
--    y _lancemos_pilot_audience_intent, que no se redefinen), exige que el
--    envio que dispara traiga el consentimiento, aplica los frenos de arriba y
--    un primer contacto vivo por persona (caso abierto, o un toque aceptado en
--    las ultimas 24 h), y crea el ancla (un webhook_events de fuente system por
--    envio, ya processed y con payload de ids), el caso de fuente landing, su
--    secuencia y la accion first_contact_review con ancla precheckout_intent,
--    que vence en submitted_at + expires_after y sale en submitted_at +
--    grace_period del ENVIO que dispara (no de la intencion, que puede ser de
--    antes). Resuelve la identidad de WhatsApp (reutiliza la del contacto en
--    cualquiera de las dos formas; si no hay, la crea con el telefono de la
--    intencion), concede el permiso si no hay fila activa y ata el caso al
--    scope del piloto con la evidencia de audiencia.
-- 6. admit_and_plan_portable_lead_precheckout: el entrypoint. Llama a
--    admit_portable_observed_lead_precheckout sin tocarla y, si el envio es
--    nuevo, intenta el plan. La admision nunca se pierde por el plan: todo lo
--    que sigue a la admision corre en un bloque que atrapa cualquier error y
--    deja el motivo en el renglon del punto 2. Adentro, lo que no necesita
--    contacto se decide antes de crear nada (intencion, scope publicado y
--    armado, cohorte, frenos), y recien despues van el contacto y el plan.
--    Los tres triggers diferidos que miran una accion o un caso nuevos
--    (compra conocida, compra ambigua, raiz comercial) se disparan adentro del
--    bloque con set constraints all immediate, para que su error tampoco
--    tumbe la admision en el commit. Solo se relanzan los errores
--    transitorios (clases 40, 53, 57 y 08, y 55P03): ahi conviene que el
--    emisor reintente el envio entero.
-- 7. reevaluate_portable_precheckout_action: envoltorio de la reevaluacion.
--    Cancela el caso (cancelled, no won) con el motivo del punto 3 o con
--    precheckout_authorization_lost; si nada frena, delega en
--    reevaluate_followup_action, que queda intacta. Es security definer porque
--    lee tablas que service_role no puede leer.
-- 8. mark_portable_precheckout_request_started: copia de
--    mark_portable_payment_failure_request_started (vigente 20260903000300)
--    con el ancla precheckout_intent y la autorizacion por landing /
--    PRECHECKOUT_FORM_SUBMITTED, mas un bloque marcado: con el lock de opt-out
--    de las dos formas del telefono tomado, vuelve a mirar los frenos. Un
--    rechazo deshace la autorizacion: no consume cupo.
-- 9. get_portable_precheckout_pilot_runtime_status: copia de
--    get_lancemos_pilot_runtime_status (vigente 20260929000200) para un scope
--    de fuente landing; ademas rechaza un scope manual_cohort, que el
--    planificador no acepta.
--
-- Privacidad: el contacto se crea solo con el scope armado y nunca en
-- consented_intent_in_cohort (ahi tiene que existir e integrar la cohorte). Un
-- envio que llega con el scope desarmado no se planifica despues. El ancla y
-- el renglon del plan llevan ids, nunca datos de la persona.
--
-- Johanna no cambia. Corre sin manifiesto y nada de lo que ejecuta se
-- redefine: aca solo hay funciones nuevas. No se tocan reevaluate_followup_action,
-- mark_followup_request_started, admit_portable_observed_lead_precheckout,
-- admit_observed_lead_precheckout, evaluate_lancemos_pilot_scope ni
-- authorize_lancemos_pilot_request_start. Los tres checks aceptan todo lo que
-- aceptaban. No siembra filas de ningun cliente.
--
-- Locks: los tres alter table toman ACCESS EXCLUSIVE sobre recovery_cases,
-- recovery_case_events y followup_sequences y validan sus filas; las FK de la
-- tabla nueva toman SHARE ROW EXCLUSIVE sobre precheckout_submissions,
-- purchase_intents, contacts y recovery_cases hasta el commit. Mientras corre,
-- las escrituras sobre esas tablas esperan. La tabla nueva nace vacia, asi que
-- la espera es la validacion de los tres checks. Si una transaccion larga
-- retiene alguna tabla, la migracion falla por lock_timeout (5 s) sin dejar
-- nada a medias y se reintenta. Conviene aplicarla con poco trafico.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- El check de recovery_cases.source nacio inline y sin nombre (baseline): el
-- nombre recovery_cases_source_check es el que Postgres le pone solo, y
-- ninguna migracion lo nombra. Una base que no nacio del baseline puede
-- tenerlo con otro nombre, asi que se busca por definicion: el unico check de
-- la tabla sobre la columna source que acepta exactamente hotmart y simulator.
-- Con cero o con mas de uno la migracion aborta entera, sin dejar nada a
-- medias, y dice cuantos encontro.
do $source_check$
declare
    v_names text[];
begin
    select coalesce(array_agg(con.conname::text order by con.conname), array[]::text[])
      into v_names
    from pg_constraint con
    where con.conrelid = 'public.recovery_cases'::regclass
      and con.contype = 'c'
      and con.conkey = array[(
          select att.attnum
          from pg_attribute att
          where att.attrelid = con.conrelid
            and att.attname = 'source'
      )]
      and array(
          select accepted.value[1]
          from regexp_matches(
              pg_get_constraintdef(con.oid), '''([a-z_]+)''', 'g'
          ) as accepted(value)
          order by 1
      ) = array['hotmart', 'simulator'];
    if cardinality(v_names) <> 1 then
        raise exception using
            errcode = '55000',
            message = 'recovery_cases_source_check_not_found',
            detail = cardinality(v_names)::text;
    end if;
    execute format(
        'alter table public.recovery_cases drop constraint %I', v_names[1]
    );
end;
$source_check$;
alter table public.recovery_cases
    add constraint recovery_cases_source_check
    check (source = any (array['hotmart', 'simulator', 'landing']));

alter table public.recovery_case_events
    drop constraint recovery_case_events_event_role_check;
alter table public.recovery_case_events
    add constraint recovery_case_events_event_role_check
    check (event_role in ('cart_abandonment', 'payment_failure', 'precheckout_intent'));

alter table public.followup_sequences
    drop constraint followup_sequences_reason_check;
alter table public.followup_sequences
    add constraint followup_sequences_reason_check
    check (reason = any (array[
        'cart_abandonment', 'payment_failure', 'precheckout_intent', 'no_reply',
        'contact_requested', 'prospect_commitment', 'agent_commitment',
        'proposal_pending', 'booking_pending', 'payment_pending',
        'nurture', 'manual', 'recovery'
    ]));

-- Un renglon por envio admitido con el flujo prendido. Guarda por que un envio
-- no termino en un primer contacto: sin esto el motivo se perderia al deshacer
-- el bloque del plan. contact_id y recovery_case_id son los que quedaron en la
-- base (un contacto creado adentro de un plan deshecho no figura).
create table public.portable_precheckout_first_contact_plans (
    submission_id uuid primary key
        references public.precheckout_submissions(id) on delete restrict,
    purchase_intent_id uuid not null
        references public.purchase_intents(id) on delete restrict,
    contact_id uuid references public.contacts(id) on delete set null,
    recovery_case_id uuid references public.recovery_cases(id) on delete restrict,
    scope_key text not null check (length(btrim(scope_key)) > 0),
    scope_version integer not null check (scope_version > 0),
    outcome text not null check (outcome in ('planned', 'not_planned', 'plan_failed')),
    reason_code text not null check (reason_code ~ '^[a-z0-9_]{1,64}$'),
    error_sqlstate text check (error_sqlstate ~ '^[0-9A-Z]{5}$'),
    created_at timestamptz not null default clock_timestamp(),
    constraint portable_precheckout_first_contact_plans_shape check (
        (outcome = 'planned' and recovery_case_id is not null and error_sqlstate is null)
        or (outcome = 'not_planned' and recovery_case_id is null and error_sqlstate is null)
        or (outcome = 'plan_failed' and recovery_case_id is null and error_sqlstate is not null)
    )
);
create index portable_precheckout_first_contact_plans_intent_idx
    on public.portable_precheckout_first_contact_plans (purchase_intent_id, created_at);

alter table public.portable_precheckout_first_contact_plans enable row level security;

create or replace function public._portable_precheckout_stop_reason(
    p_purchase_intent_id uuid,
    p_contact_id uuid
)
returns text
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_intent public.purchase_intents%rowtype;
    v_binding public.commercial_ally_runtime_bindings%rowtype;
begin
    -- Por que un primer contacto no sale. Devuelve null si nada lo frena. El
    -- contacto es opcional: antes de crearlo se miran los frenos que no lo
    -- necesitan. Es de solo lectura, y lo llaman las tres funciones security
    -- definer del primer contacto; ningun rol de la API lo ejecuta.
    select intent.* into v_intent
    from public.purchase_intents intent
    where intent.id = p_purchase_intent_id;
    if not found then
        return 'precheckout_intent_not_live';
    end if;

    if v_intent.lifecycle_state = 'purchased' then
        return 'intent_purchased';
    end if;
    if v_intent.lifecycle_state <> 'waiting_for_purchase'
       or v_intent.provisional
       or not v_intent.provider_observed
       or v_intent.normalized_phone is null
       or coalesce(v_intent.current_classification, '') in (
           'identity_conflict', 'tracking_incomplete', 'expired_unknown'
       ) then
        return 'precheckout_intent_not_live';
    end if;

    -- Una compra aprobada que no se pudo atribuir a una sola intencion (dos
    -- intenciones vivas de la persona, o el email cruza y el telefono no) deja
    -- a esta intencion en waiting_for_purchase. La admision de la compra
    -- registra los candidatos: con uno alcanza para no escribirle.
    if exists (
        select 1
        from public.portable_hotmart_purchase_correlation_candidates candidate
        where candidate.purchase_intent_id = v_intent.id
    ) then
        return 'intent_purchase_ambiguous';
    end if;

    select binding.* into v_binding
    from public.commercial_ally_runtime_bindings binding
    where binding.tenant_ref = v_intent.tenant_ref
      and binding.funnel_ref = v_intent.funnel_ref
      and binding.status = 'active';
    if not found then
        return 'precheckout_binding_unavailable';
    end if;

    -- Cualquier compra aprobada admitida del binding con la misma identidad:
    -- el email exacto o el telefono en cualquiera de sus dos formas, de la
    -- intencion o de un contact_point del contacto. No depende de la ventana
    -- de la correlacion: una intencion vieja reenviada no vuelve a quedar
    -- purchased, y a quien ya compro no se le escribe (D25).
    if exists (
        select 1
        from public.portable_hotmart_purchase_correlations purchase
        join public.hotmart_purchase_intent_event_identities buyer
          on buyer.webhook_event_id = purchase.webhook_event_id
        where purchase.tenant_ref = v_binding.tenant_ref
          and purchase.funnel_ref = v_binding.funnel_ref
          and (
              buyer.normalized_email = v_intent.normalized_email
              or buyer.normalized_phone = any(
                  public._whatsapp_phone_variants(v_intent.normalized_phone)
              )
              or (
                  p_contact_id is not null
                  and exists (
                      select 1
                      from public.contact_points point
                      where point.contact_id = p_contact_id
                        and (
                            (
                                point.type = 'email'
                                and point.normalized_value = buyer.normalized_email
                            )
                            or (
                                point.type = 'phone'
                                and point.normalized_value = any(
                                    public._whatsapp_phone_variants(buyer.normalized_phone)
                                )
                            )
                        )
                  )
              )
          )
    ) then
        return 'purchase_by_identity';
    end if;

    -- Hotmart ya informo algo mas preciso de esta persona: el carrito o el
    -- pago fallido de la intencion (la correlacion la clasifica), o un caso de
    -- recuperacion abierto del producto. Ese flujo le escribe; este no.
    if coalesce(v_intent.current_classification, '') in (
           'confirmed_abandonment', 'payment_failure_supported'
       )
       or (
           p_contact_id is not null
           and exists (
               select 1
               from public.recovery_cases recovery_case
               where recovery_case.contact_id = p_contact_id
                 and recovery_case.source = 'hotmart'
                 and recovery_case.external_product_id
                     = v_binding.hotmart_product_id::text
                 and recovery_case.status in ('grace_period', 'active', 'paused')
           )
       ) then
        return 'superseded_by_provider_event';
    end if;

    -- Opt-out de Chatwoot en cualquiera de las dos formas del telefono, en la
    -- cuenta del binding y en los estados que frena el arranque
    -- (mark_followup_request_started). Es el criterio de 20261001000100.
    if exists (
        select 1
        from public.contact_opt_out_events optout
        where optout.source = 'chatwoot'
          and optout.channel = 'whatsapp'
          and optout.canonical_account_id = v_binding.chatwoot_account_id
          and optout.external_user_id = any(
              public._whatsapp_phone_variants(v_intent.normalized_phone)
          )
          and optout.correlation_status in (
              'applied', 'unmatched', 'ambiguous', 'evidence_conflict'
          )
    ) then
        return 'precheckout_prior_opt_out';
    end if;

    -- La conversacion de la persona esta en manos de alguien del equipo, o
    -- pausada, cerrada o bloqueada: una plantilla automatica no entra ahi. Es
    -- el criterio blocked_handoff del primer toque de Johanna (20260829000300
    -- al reservar y 20260829000400 al arrancar): cualquier conversacion del
    -- contacto con human_takeover, en paused_human, closed o blocked, o con la
    -- automatizacion en paused, disabled, restricted o error. La derivacion
    -- del entrante marca la conversacion, no el caso landing (que nace sin
    -- conversacion), asi que la reevaluacion compartida no la veria. Va al
    -- final: un opt-out aplicado tambien bloquea la conversacion y conserva
    -- su propio motivo.
    if p_contact_id is not null
       and exists (
           select 1
           from public.conversations conversation
           where conversation.contact_id = p_contact_id
             and (
                 conversation.human_takeover
                 or conversation.status in (
                     'paused_human', 'closed', 'blocked'
                 )
                 or conversation.automation_status in (
                     'paused', 'disabled', 'restricted', 'error'
                 )
             )
       ) then
        return 'precheckout_conversation_handoff';
    end if;

    return null;
end;
$function$;

create or replace function public._find_portable_precheckout_contact(
    p_purchase_intent_id uuid,
    out contact_id uuid,
    out reason_code text
)
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_intent public.purchase_intents%rowtype;
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_owner_ids uuid[];
begin
    -- El contacto de quien dejo el formulario, sin crear nada. Quien escribio
    -- primero por WhatsApp tiene un contacto sin email, telefono ni puntos:
    -- solo la identidad con su wa_id, que puede venir en la otra forma del
    -- mismo movil. Por eso se mira tambien channel_identities de la cuenta.
    contact_id := null;

    select intent.* into v_intent
    from public.purchase_intents intent
    where intent.id = p_purchase_intent_id;
    if not found
       or v_intent.normalized_email is null
       or v_intent.normalized_phone is null then
        reason_code := 'precheckout_contact_input_invalid';
        return;
    end if;

    select binding.* into v_binding
    from public.commercial_ally_runtime_bindings binding
    where binding.tenant_ref = v_intent.tenant_ref
      and binding.funnel_ref = v_intent.funnel_ref
      and binding.status = 'active';
    if not found then
        reason_code := 'precheckout_binding_unavailable';
        return;
    end if;

    select coalesce(array_agg(owner.id order by owner.id), array[]::uuid[])
      into v_owner_ids
    from (
        select point.contact_id as id
        from public.contact_points point
        where point.type = 'email'
          and point.normalized_value = v_intent.normalized_email
        union
        select point.contact_id
        from public.contact_points point
        where point.type = 'phone'
          and point.normalized_value = any(
              public._whatsapp_phone_variants(v_intent.normalized_phone)
          )
        union
        select identity.contact_id
        from public.channel_identities identity
        where identity.channel = 'whatsapp'
          and identity.account_id = 'chatwoot:' || v_binding.chatwoot_account_id::text
          and identity.external_user_id = any(
              public._whatsapp_phone_variants(v_intent.normalized_phone)
          )
        union
        select contact.id
        from public.contacts contact
        where lower(contact.email) = v_intent.normalized_email
    ) owner;

    if cardinality(v_owner_ids) = 0 then
        reason_code := 'precheckout_contact_not_found';
    elsif cardinality(v_owner_ids) = 1 then
        contact_id := v_owner_ids[1];
        reason_code := 'precheckout_contact_found';
    else
        reason_code := 'precheckout_contact_ambiguous';
    end if;
end;
$function$;

create or replace function public._ensure_portable_precheckout_contact(
    p_purchase_intent_id uuid,
    p_submission_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_intent public.purchase_intents%rowtype;
    v_submission public.precheckout_submissions%rowtype;
    v_contact_id uuid;
    v_reason text;
    v_full_name text;
    v_country_iso text;
    v_point_metadata jsonb;
begin
    -- Crea el contacto de la intencion o completa el que ya existe. Corre
    -- adentro del bloque protegido del entrypoint: si el plan se rechaza, todo
    -- lo que hizo se deshace.
    select intent.* into v_intent
    from public.purchase_intents intent
    where intent.id = p_purchase_intent_id;
    select submission.* into v_submission
    from public.purchase_intent_submissions link
    join public.precheckout_submissions submission
      on submission.id = link.submission_id
    where link.purchase_intent_id = p_purchase_intent_id
      and submission.id = p_submission_id;
    if v_intent.id is null
       or v_submission.id is null
       or v_intent.normalized_email is null
       or v_intent.normalized_phone is null then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = 'precheckout_contact_input_invalid';
    end if;

    v_full_name := nullif(btrim(v_submission.canonical_payload #>> '{lead,full_name}'), '');
    v_country_iso := nullif(
        btrim(v_submission.canonical_payload #>> '{identity,phone_country_iso}'), ''
    );

    -- Dos formularios de la misma persona (uno por landing) no crean dos
    -- contactos: se serializan por el telefono en forma canonica y por el
    -- email, siempre en ese orden.
    perform pg_advisory_xact_lock(hashtextextended(
        'portable-precheckout-contact:phone:'
            || public._whatsapp_phone_canonical(v_intent.normalized_phone),
        0
    ));
    perform pg_advisory_xact_lock(hashtextextended(
        'portable-precheckout-contact:email:' || v_intent.normalized_email,
        0
    ));

    select found.contact_id, found.reason_code
      into v_contact_id, v_reason
    from public._find_portable_precheckout_contact(p_purchase_intent_id) found;

    if v_reason = 'precheckout_contact_not_found' then
        insert into public.contacts (full_name, email, phone, country_iso)
        values (
            v_full_name,
            v_intent.normalized_email,
            v_intent.normalized_phone,
            v_country_iso
        )
        returning id into v_contact_id;
    elsif v_reason = 'precheckout_contact_found' then
        perform 1
        from public.contacts contact
        where contact.id = v_contact_id
        for update;

        -- Solo se completa lo que falta. Un telefono distinto del consentido
        -- no se pisa: lo rechaza el criterio del consentimiento
        -- (consented_intent_contact_phone_mismatch).
        update public.contacts contact
        set full_name = coalesce(nullif(btrim(contact.full_name), ''), v_full_name),
            email = coalesce(contact.email, v_intent.normalized_email),
            phone = coalesce(nullif(btrim(contact.phone), ''), v_intent.normalized_phone)
        where contact.id = v_contact_id
          and (
              nullif(btrim(contact.full_name), '') is null
              or contact.email is null
              or nullif(btrim(contact.phone), '') is null
          );
    else
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = coalesce(v_reason, 'precheckout_contact_unresolved');
    end if;

    v_point_metadata := jsonb_build_object(
        'reason', 'precheckout_form_submission',
        'purchase_intent_id', v_intent.id,
        'precheckout_submission_id', v_submission.id
    );

    insert into public.contact_points (
        contact_id, type, raw_value, normalized_value, source, metadata
    )
    select v_contact_id, 'email', v_intent.normalized_email,
           v_intent.normalized_email, 'system', v_point_metadata
    where not exists (
        select 1
        from public.contact_points point
        where point.contact_id = v_contact_id
          and point.type = 'email'
          and point.normalized_value = v_intent.normalized_email
    );

    -- El punto del telefono queda crudo, como lo guardo el formulario. Si el
    -- contacto ya tiene el mismo movil en la otra forma, no se suma otro.
    insert into public.contact_points (
        contact_id, type, raw_value, normalized_value, source, metadata
    )
    select v_contact_id, 'phone', '+' || v_intent.normalized_phone,
           v_intent.normalized_phone, 'system', v_point_metadata
    where not exists (
        select 1
        from public.contact_points point
        where point.contact_id = v_contact_id
          and point.type = 'phone'
          and point.normalized_value = any(
              public._whatsapp_phone_variants(v_intent.normalized_phone)
          )
    );

    return v_contact_id;
end;
$function$;

create or replace function public._plan_portable_precheckout_first_contact(
    p_submission_id uuid,
    p_purchase_intent_id uuid,
    p_contact_id uuid,
    p_scope_key text,
    p_scope_version integer
)
returns table (
    recovery_case_id uuid,
    followup_sequence_id uuid,
    scheduled_action_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_scope public.pilot_scope_versions%rowtype;
    v_policy public.followup_policy_versions%rowtype;
    v_intent public.purchase_intents%rowtype;
    v_submission public.precheckout_submissions%rowtype;
    v_runtime_binding public.commercial_ally_runtime_bindings%rowtype;
    v_identity public.channel_identities%rowtype;
    v_pilot_binding public.pilot_recovery_case_bindings%rowtype;
    v_submitted_at timestamptz;
    v_now timestamptz := clock_timestamp();
    v_account_id text;
    v_external_user_id text;
    v_allowed boolean;
    v_reason text;
    v_generation bigint;
    v_audience_intent_id uuid;
    v_audience_submission_id uuid;
    v_audience_reason text;
    v_stop_reason text;
    v_anchor_event_id uuid;
    v_case_id uuid;
    v_sequence_id uuid;
    v_action_id uuid;
begin
    if p_submission_id is null
       or p_purchase_intent_id is null
       or p_contact_id is null
       or p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1 then
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
    if not found then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = 'pilot_scope_not_published';
    end if;
    if v_scope.source <> 'landing'
       or v_scope.source_event_type <> 'PRECHECKOUT_FORM_SUBMITTED' then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = 'pilot_source_event_mismatch';
    end if;
    -- El primer contacto solo existe con evidencia de consentimiento: en
    -- manual_cohort el scope no la exige ni la registra, y la autorizacion del
    -- envio no volveria a mirar la intencion.
    if v_scope.audience_mode = 'manual_cohort' then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = 'precheckout_scope_audience_unsupported';
    end if;

    select intent.* into v_intent
    from public.purchase_intents intent
    where intent.id = p_purchase_intent_id
    for share;
    select submission.* into v_submission
    from public.purchase_intent_submissions link
    join public.precheckout_submissions submission
      on submission.id = link.submission_id
    where link.purchase_intent_id = p_purchase_intent_id
      and submission.id = p_submission_id;
    if v_intent.id is null or v_submission.id is null then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = 'precheckout_submission_mismatch';
    end if;
    -- La demora y el vencimiento corren desde el envio que dispara este plan.
    -- La intencion conserva el submitted_at de su primer envio: con ese, un
    -- reenvio naceria vencido o sin la demora que deja llegar la compra.
    v_submitted_at := (v_submission.canonical_payload #>> '{submitted_at}')::timestamptz;

    select binding.* into v_runtime_binding
    from public.commercial_ally_runtime_bindings binding
    where binding.tenant_ref = v_intent.tenant_ref
      and binding.funnel_ref = v_intent.funnel_ref
      and binding.status = 'active';
    if not found then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = 'precheckout_binding_unavailable';
    end if;

    -- Global order for this aggregate: contact -> case -> sequence -> action.
    perform 1
    from public.contacts contact
    where contact.id = p_contact_id
    for update;
    if not found then
        raise exception using errcode = '23503', message = 'contact_not_found';
    end if;

    select evaluation.allowed,
           evaluation.reason_code,
           evaluation.runtime_generation
      into v_allowed, v_reason, v_generation
    from public.evaluate_lancemos_pilot_scope(
        p_scope_key,
        p_scope_version,
        v_runtime_binding.tenant_ref,
        v_runtime_binding.chatwoot_account_id,
        v_runtime_binding.chatwoot_inbox_id,
        v_scope.channel_provider,
        v_scope.channel_account_ref,
        'landing',
        'PRECHECKOUT_FORM_SUBMITTED',
        v_runtime_binding.hotmart_product_id::text,
        v_intent.offer_ref,
        p_contact_id
    ) evaluation;
    if not coalesce(v_allowed, false) then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = coalesce(v_reason, 'pilot_scope_unknown');
    end if;

    -- La politica es la del scope: la demora (grace_period), el vencimiento y
    -- el paso first_contact salen de ahi.
    select policy.* into v_policy
    from public.followup_policy_versions policy
    where policy.policy_key = v_scope.policy_key
      and policy.version = v_scope.policy_version
      and policy.status = 'published'
      and policy.purpose = 'cart_recovery';
    if not found
       or not exists (
           select 1
           from jsonb_array_elements(v_policy.steps) as policy_step
           where policy_step ->> 'step_key' = 'first_contact'
       ) then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = 'precheckout_policy_unavailable';
    end if;
    if v_submitted_at is null
       or v_submitted_at + v_policy.expires_after <= v_now then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = 'precheckout_submission_expired';
    end if;

    -- La identidad de WhatsApp a la que se va a escribir: la activa del
    -- contacto para este movil en cualquiera de sus dos formas (primero la
    -- textual del formulario). Quien escribio antes tiene la del wa_id; si no
    -- hay ninguna se crea abajo con el telefono de la intencion, sin
    -- reescribirlo.
    v_account_id := 'chatwoot:' || v_runtime_binding.chatwoot_account_id::text;
    if exists (
        select 1
        from public.channel_identities other_identity
        where other_identity.channel = 'whatsapp'
          and other_identity.account_id = v_account_id
          and other_identity.external_user_id = any(
              public._whatsapp_phone_variants(v_intent.normalized_phone)
          )
          and other_identity.contact_id <> p_contact_id
    ) then
        raise exception using
            errcode = '23514',
            message = 'channel_identity_contact_mismatch';
    end if;
    select identity.* into v_identity
    from public.channel_identities identity
    where identity.channel = 'whatsapp'
      and identity.account_id = v_account_id
      and identity.external_user_id = any(
          public._whatsapp_phone_variants(v_intent.normalized_phone)
      )
      and identity.contact_id = p_contact_id
    order by (identity.identity_status = 'active') desc,
             (identity.external_user_id = v_intent.normalized_phone) desc,
             identity.external_user_id
    limit 1
    for update;
    v_external_user_id := coalesce(v_identity.external_user_id, v_intent.normalized_phone);

    -- La audiencia: la intencion de este envio, del tenant, el producto y la
    -- oferta del scope, con el consentimiento vigente para ese destino y para
    -- el telefono del contacto, y sin opt-out previo.
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
        v_intent.offer_ref,
        v_intent.id,
        v_external_user_id
    ) audience;
    if v_audience_reason is distinct from 'pilot_audience_allowed' then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = coalesce(v_audience_reason, 'pilot_audience_intent_unresolved');
    end if;
    -- El envio que dispara el plan es el que trae el consentimiento. Un envio
    -- sin opt-in sobre una intencion que ya lo tenia no dispara un mensaje.
    if v_audience_submission_id is distinct from p_submission_id then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = 'precheckout_submission_not_consented';
    end if;

    v_stop_reason := public._portable_precheckout_stop_reason(v_intent.id, p_contact_id);
    if v_stop_reason is not null then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = v_stop_reason;
    end if;

    -- Un primer contacto vivo por persona (D21): con un caso abierto, o con un
    -- toque aceptado (o de resultado desconocido) en las ultimas 24 h, otro
    -- formulario no planifica otro. El ancla es por envio, asi que este es el
    -- unico freno de repeticion.
    if exists (
        select 1
        from public.recovery_cases open_case
        where open_case.contact_id = p_contact_id
          and open_case.source = 'landing'
          and open_case.context ->> 'trigger_kind' = 'precheckout_intent'
          and open_case.status in ('grace_period', 'active', 'paused')
    ) or exists (
        select 1
        from public.scheduled_actions touched
        join public.recovery_cases touched_case
          on touched_case.id = touched.recovery_case_id
        where touched_case.contact_id = p_contact_id
          and touched.anchor_type = 'precheckout_intent'
          and touched.status in ('accepted_by_chatwoot', 'delivery_unknown')
          and coalesce(touched.executed_at, touched.updated_at)
              > v_now - interval '24 hours'
    ) then
        raise exception using
            errcode = '55000',
            message = 'pilot_scope_rejected',
            detail = 'precheckout_contact_already_planned';
    end if;

    -- El ancla del caso. recovery_cases exige un webhook_events unico: uno de
    -- fuente system por envio, que nace processed (el worker de resolucion
    -- solo toma received) y con payload de ids. creation_date es lo que lee el
    -- trigger de compra conocida para ordenar la compra contra el caso.
    insert into public.webhook_events (
        source, external_event_id, event_type, payload,
        processing_status, processed_at
    ) values (
        'system',
        'precheckout-submission:' || p_submission_id::text,
        'PRECHECKOUT_FORM_SUBMITTED',
        jsonb_build_object(
            'purchase_intent_id', v_intent.id,
            'precheckout_submission_id', p_submission_id,
            'creation_date', floor(extract(epoch from v_submitted_at) * 1000)::bigint
        ),
        'processed',
        v_now
    )
    returning id into v_anchor_event_id;

    insert into public.recovery_cases (
        contact_id,
        abandonment_event_id,
        source,
        external_product_id,
        product_name,
        offer_code,
        status,
        lead_stage,
        grace_expires_at,
        policy_key,
        policy_version,
        hotmart_purchase_intent_id,
        context
    ) values (
        p_contact_id,
        v_anchor_event_id,
        'landing',
        v_runtime_binding.hotmart_product_id::text,
        v_runtime_binding.product_name,
        v_intent.offer_ref,
        'grace_period',
        'new',
        v_submitted_at + v_policy.grace_period,
        v_scope.policy_key,
        v_scope.policy_version,
        v_intent.id,
        jsonb_build_object(
            'trigger_kind', 'precheckout_intent',
            'purchase_intent_id', v_intent.id,
            'precheckout_submission_id', p_submission_id,
            'precheckout_submitted_at', v_submitted_at
        )
    )
    returning id into v_case_id;

    insert into public.recovery_case_events (
        recovery_case_id,
        webhook_event_id,
        event_role,
        observed_at
    ) values (
        v_case_id,
        v_anchor_event_id,
        'precheckout_intent',
        v_submitted_at
    );

    insert into public.followup_sequences (
        recovery_case_id,
        status,
        reason,
        policy_key,
        policy_version,
        current_step,
        max_attempts
    ) values (
        v_case_id,
        'active',
        'precheckout_intent',
        v_scope.policy_key,
        v_scope.policy_version,
        0,
        v_policy.max_automatic_messages
    )
    returning id into v_sequence_id;

    insert into public.scheduled_actions (
        recovery_case_id,
        followup_sequence_id,
        action_type,
        status,
        due_at,
        expires_at,
        expected_case_version,
        idempotency_key,
        policy_key,
        policy_version,
        step_key,
        anchor_type,
        anchor_subject_internal_id,
        anchor_observed_at,
        anchor_checkpoint
    ) values (
        v_case_id,
        v_sequence_id,
        'first_contact_review',
        'pending',
        v_submitted_at + v_policy.grace_period,
        v_submitted_at + v_policy.expires_after,
        1,
        'precheckout_first_contact:' || v_case_id::text,
        v_scope.policy_key,
        v_scope.policy_version,
        'first_contact',
        'precheckout_intent',
        v_anchor_event_id,
        v_submitted_at,
        jsonb_build_object(
            'webhook_event_id', v_anchor_event_id,
            'purchase_intent_id', v_intent.id,
            'precheckout_submission_id', p_submission_id
        )
    )
    returning id into v_action_id;

    insert into public.conversation_events (
        recovery_case_id,
        event_type,
        actor_type,
        related_action_id,
        data
    ) values (
        v_case_id,
        'cart_recovery_planned',
        'system',
        v_action_id,
        jsonb_build_object(
            'policy_key', v_scope.policy_key,
            'policy_version', v_scope.policy_version,
            'from_status', null,
            'to_status', 'pending',
            'reason_code', 'precheckout_intent'
        )
    );

    -- La identidad, como plan_cart_recovery_with_identity (20260805000200),
    -- con la del contacto ya elegida arriba entre las dos formas.
    if v_identity.id is null then
        select identity.* into v_identity
        from public.channel_identities identity
        where identity.channel = 'whatsapp'
          and identity.account_id = v_account_id
          and identity.external_user_id = v_external_user_id
        for update;
    end if;
    if v_identity.id is null then
        begin
            insert into public.channel_identities (
                contact_id,
                channel,
                account_id,
                external_user_id,
                identity_status,
                metadata
            ) values (
                p_contact_id,
                'whatsapp',
                v_account_id,
                v_external_user_id,
                'active',
                jsonb_build_object('inbox_id', v_runtime_binding.chatwoot_inbox_id)
            )
            returning * into v_identity;
        exception when unique_violation then
            select identity.* into strict v_identity
            from public.channel_identities identity
            where identity.channel = 'whatsapp'
              and identity.account_id = v_account_id
              and identity.external_user_id = v_external_user_id
            for update;
        end;
    end if;

    if v_identity.contact_id <> p_contact_id then
        raise exception using
            errcode = '23514',
            message = 'channel_identity_contact_mismatch';
    end if;
    if v_identity.identity_status <> 'active' then
        raise exception using
            errcode = '23514',
            message = 'channel_identity_not_active';
    end if;
    if v_identity.metadata ? 'inbox_id'
       and v_identity.metadata ->> 'inbox_id'
           <> v_runtime_binding.chatwoot_inbox_id::text then
        raise exception using
            errcode = '23514',
            message = 'channel_identity_inbox_mismatch';
    end if;

    update public.channel_identities
    set metadata = metadata
            || jsonb_build_object('inbox_id', v_runtime_binding.chatwoot_inbox_id),
        updated_at = clock_timestamp()
    where id = v_identity.id;

    update public.recovery_cases
    set selected_channel_identity_id = v_identity.id,
        identity_resolution_status = 'resolved',
        identity_resolution_error = null,
        identity_resolution_last_attempt_at = clock_timestamp(),
        identity_resolution_attempt_count = identity_resolution_attempt_count + 1
    where id = v_case_id;

    -- El permiso lo concede la intencion con consentimiento, como en el pago
    -- fallido (20260930000100). El contacto ya esta bloqueado: serializa con
    -- el opt-out. Una fila activa, de cualquier estado, gana: nunca se pisa
    -- una baja ni se duplica el permiso.
    if not exists (
        select 1
        from public.contact_authorizations ca
        where ca.contact_id = p_contact_id
          and ca.channel = 'whatsapp'
          and ca.purpose = 'cart_recovery'
          and ca.valid_from <= v_now
          and (ca.valid_until is null or ca.valid_until > v_now)
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
                'purchase_intent_id', v_intent.id,
                'precheckout_submission_id', v_audience_submission_id,
                'consent_copy_version', v_runtime_binding.consent_copy_version,
                'webhook_event_id', v_anchor_event_id,
                'recovery_case_id', v_case_id,
                'phone_match', case
                    when v_intent.normalized_phone = v_external_user_id
                        then 'exact'
                    else 'whatsapp_equivalent'
                end
            ),
            v_now
        );
    end if;

    insert into public.pilot_recovery_case_bindings (
        recovery_case_id, scope_key, scope_version, source_event_id,
        audience_mode, audience_purchase_intent_id,
        audience_precheckout_submission_id
    ) values (
        v_case_id, p_scope_key, p_scope_version, v_anchor_event_id,
        v_scope.audience_mode, v_audience_intent_id,
        v_audience_submission_id
    );

    select binding.* into strict v_pilot_binding
    from public.pilot_recovery_case_bindings binding
    where binding.recovery_case_id = v_case_id;
    if v_pilot_binding.scope_key <> p_scope_key
       or v_pilot_binding.scope_version <> p_scope_version
       or v_pilot_binding.source_event_id <> v_anchor_event_id then
        raise exception using
            errcode = '55000',
            message = 'pilot_case_binding_conflict';
    end if;

    return query select v_case_id, v_sequence_id, v_action_id;
end;
$function$;

create or replace function public.admit_and_plan_portable_lead_precheckout(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
    p_external_submission_id text,
    p_raw_payload jsonb,
    p_canonical_payload jsonb,
    p_scope_key text,
    p_scope_version integer
)
returns table (
    outcome text,
    submission_id uuid,
    purchase_intent_id uuid,
    plan_outcome text,
    plan_reason text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_admission_outcome text;
    v_submission_id uuid;
    v_intent_id uuid;
    v_intent public.purchase_intents%rowtype;
    v_submission public.precheckout_submissions%rowtype;
    v_runtime_binding public.commercial_ally_runtime_bindings%rowtype;
    v_scope public.pilot_scope_versions%rowtype;
    v_control public.pilot_runtime_controls%rowtype;
    v_policy public.followup_policy_versions%rowtype;
    v_submitted_at timestamptz;
    v_found_contact_id uuid;
    v_contact_id uuid;
    v_find_reason text;
    v_case_id uuid;
    v_action_id uuid;
    v_action_status text;
    v_action_reason text;
    v_reason text;
    v_plan_outcome text;
    v_plan_reason text;
    v_sqlstate text;
    v_message text;
    v_detail text;
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1 then
        raise exception using
            errcode = '22023',
            message = 'invalid_pilot_plan_parameters';
    end if;

    -- El control del scope se bloquea antes que nada: serializa con armar,
    -- pausar y la cohorte, y fija el orden control -> intencion -> contacto,
    -- que es el de la autorizacion del envio (que bloquea el control y despues
    -- lee la intencion). Sin esto, un reenvio del formulario y el arranque del
    -- envio de la misma intencion se esperarian en cruz.
    perform 1
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key
    for update;

    select admission.outcome,
           admission.submission_id,
           admission.purchase_intent_id
      into strict v_admission_outcome, v_submission_id, v_intent_id
    from public.admit_portable_observed_lead_precheckout(
        p_tenant_ref,
        p_funnel_ref,
        p_binding_version,
        p_external_submission_id,
        p_raw_payload,
        p_canonical_payload
    ) admission;

    -- Solo un envio nuevo planifica. Un duplicado o un conflicto devuelve lo
    -- que ya se decidio para ese envio, si se decidio algo.
    if v_admission_outcome <> 'inserted' then
        select plan.outcome, plan.reason_code
          into v_plan_outcome, v_plan_reason
        from public.portable_precheckout_first_contact_plans plan
        where plan.submission_id = v_submission_id;
        return query select
            v_admission_outcome, v_submission_id, v_intent_id,
            v_plan_outcome, v_plan_reason;
        return;
    end if;

    -- Todo lo que sigue corre en un bloque que no deja caer la admision: lo
    -- que se decide sin crear nada (1 a 4), y el contacto y el plan (5).
    begin
        -- 1. La intencion y el envio: viva, con los dos permisos, y el envio
        --    con el consentimiento.
        select intent.* into strict v_intent
        from public.purchase_intents intent
        where intent.id = v_intent_id;
        select submission.* into strict v_submission
        from public.precheckout_submissions submission
        where submission.id = v_submission_id;
        v_submitted_at :=
            (v_submission.canonical_payload #>> '{submitted_at}')::timestamptz;

        if v_intent.lifecycle_state = 'purchased' then
            v_reason := 'intent_purchased';
        elsif v_intent.lifecycle_state <> 'waiting_for_purchase'
           or v_intent.provisional
           or not v_intent.provider_observed
           or v_intent.normalized_phone is null
           or coalesce(v_intent.current_classification, '') in (
               'identity_conflict', 'tracking_incomplete', 'expired_unknown'
           ) then
            v_reason := 'precheckout_intent_not_live';
        elsif not v_intent.whatsapp_contact_authorized
           or not v_intent.activation_authorized then
            v_reason := 'precheckout_intent_not_authorized';
        elsif v_submission.contract_version <> '1.1.0'
           or not v_submission.activation_authorized
           or v_submission.canonical_payload #>> '{consent,whatsapp_contact}'
               is distinct from 'true'
           or v_submission.canonical_payload #>> '{consent,marketing_optin}'
               is distinct from 'true' then
            v_reason := 'precheckout_submission_not_consented';
        end if;

        -- 2. El scope, sin contacto: lo que el planificador va a exigir y no
        --    depende de la persona. Con el scope desarmado no se crea nada.
        if v_reason is null then
            select binding.* into v_runtime_binding
            from public.commercial_ally_runtime_bindings binding
            where binding.tenant_ref = v_intent.tenant_ref
              and binding.funnel_ref = v_intent.funnel_ref
              and binding.status = 'active';
            select scope.* into v_scope
            from public.pilot_scope_versions scope
            where scope.scope_key = p_scope_key
              and scope.version = p_scope_version
              and scope.status = 'published';
            select control.* into v_control
            from public.pilot_runtime_controls control
            where control.scope_key = p_scope_key;

            if v_runtime_binding.tenant_ref is null then
                v_reason := 'precheckout_binding_unavailable';
            elsif v_scope.scope_key is null then
                v_reason := 'pilot_scope_not_published';
            elsif v_control.scope_key is null
               or v_control.scope_version <> p_scope_version then
                v_reason := 'pilot_scope_version_mismatch';
            elsif v_control.runtime_state <> 'armed' then
                v_reason := 'pilot_runtime_not_armed';
            elsif v_scope.source <> 'landing'
               or v_scope.source_event_type <> 'PRECHECKOUT_FORM_SUBMITTED' then
                v_reason := 'pilot_source_event_mismatch';
            elsif v_scope.audience_mode = 'manual_cohort' then
                v_reason := 'precheckout_scope_audience_unsupported';
            elsif v_scope.tenant_key <> v_runtime_binding.tenant_ref then
                v_reason := 'pilot_tenant_mismatch';
            elsif v_scope.chatwoot_account_id
                <> v_runtime_binding.chatwoot_account_id then
                v_reason := 'pilot_chatwoot_account_mismatch';
            elsif v_scope.chatwoot_inbox_id
                <> v_runtime_binding.chatwoot_inbox_id then
                v_reason := 'pilot_chatwoot_inbox_mismatch';
            elsif v_scope.external_product_id
                <> v_runtime_binding.hotmart_product_id::text then
                v_reason := 'pilot_product_mismatch';
            elsif v_intent.offer_ref <> v_scope.offer_code
               and v_intent.offer_ref <> all(v_scope.additional_offer_codes) then
                v_reason := 'pilot_offer_mismatch';
            else
                select policy.* into v_policy
                from public.followup_policy_versions policy
                where policy.policy_key = v_scope.policy_key
                  and policy.version = v_scope.policy_version
                  and policy.status = 'published'
                  and policy.purpose = 'cart_recovery';
                if v_policy.policy_key is null
                   or not exists (
                       select 1
                       from jsonb_array_elements(v_policy.steps) as policy_step
                       where policy_step ->> 'step_key' = 'first_contact'
                   ) then
                    v_reason := 'precheckout_policy_unavailable';
                elsif v_submitted_at is null
                   or v_submitted_at + v_policy.expires_after
                       <= clock_timestamp() then
                    v_reason := 'precheckout_submission_expired';
                end if;
            end if;
        end if;

        -- 3. El contacto, sin crearlo. En consented_intent_in_cohort tiene que
        --    existir e integrar la cohorte: ahi no se crean contactos.
        if v_reason is null then
            select found.contact_id, found.reason_code
              into v_found_contact_id, v_find_reason
            from public._find_portable_precheckout_contact(v_intent_id) found;
            if v_find_reason not in (
                'precheckout_contact_found', 'precheckout_contact_not_found'
            ) then
                v_reason := coalesce(v_find_reason, 'precheckout_contact_unresolved');
            elsif v_scope.audience_mode = 'consented_intent_in_cohort'
               and (
                   v_found_contact_id is null
                   or not exists (
                       select 1
                       from public.pilot_cohort_memberships member
                       where member.scope_key = p_scope_key
                         and member.scope_version = p_scope_version
                         and member.contact_id = v_found_contact_id
                         and member.member_status = 'active'
                   )
               ) then
                v_reason := 'pilot_contact_not_in_cohort';
            end if;
        end if;

        -- 4. Los frenos que no necesitan un contacto nuevo: compra por
        --    identidad, opt-out previo, carrito o pago fallido.
        if v_reason is null then
            v_reason := public._portable_precheckout_stop_reason(
                v_intent_id, v_found_contact_id
            );
        end if;

        if v_reason is not null then
            v_plan_outcome := 'not_planned';
            v_plan_reason := v_reason;
            v_contact_id := v_found_contact_id;
        else
            -- 5. El contacto y el plan.
            v_contact_id := public._ensure_portable_precheckout_contact(
                v_intent_id, v_submission_id
            );
            select plan.recovery_case_id, plan.scheduled_action_id
              into strict v_case_id, v_action_id
            from public._plan_portable_precheckout_first_contact(
                v_submission_id,
                v_intent_id,
                v_contact_id,
                p_scope_key,
                p_scope_version
            ) plan;

            -- Los triggers diferidos de la accion y del caso nuevos corren
            -- aca, adentro del bloque: si alguno falla se deshace el plan, no
            -- la admision.
            set constraints all immediate;

            select action.status, action.terminal_reason
              into strict v_action_status, v_action_reason
            from public.scheduled_actions action
            where action.id = v_action_id;
            v_plan_outcome := 'planned';
            -- Una compra ya conocida cierra la accion al nacer (el trigger de
            -- compra): el renglon lo dice.
            v_plan_reason := case
                when v_action_status = 'pending' then 'first_contact_scheduled'
                else coalesce(v_action_reason, 'first_contact_not_pending')
            end;
        end if;
    exception when others then
        get stacked diagnostics
            v_sqlstate = returned_sqlstate,
            v_message = message_text,
            v_detail = pg_exception_detail;
        -- Un error transitorio se relanza: deshace tambien la admision y el
        -- emisor reintenta el envio entero. Si se guardara como plan_failed,
        -- el formulario quedaria admitido y sin plan.
        if v_sqlstate like '40%'
           or v_sqlstate like '53%'
           or v_sqlstate like '57%'
           or v_sqlstate like '08%'
           or v_sqlstate = '55P03' then
            raise;
        end if;
        -- Lo que el bloque creo ya no existe. El contacto encontrado en el
        -- paso 3 si: ese paso solo lee.
        v_contact_id := v_found_contact_id;
        v_case_id := null;
        if v_sqlstate = '55000'
           and v_message = 'pilot_scope_rejected'
           and v_detail ~ '^[a-z0-9_]{1,64}$' then
            v_plan_outcome := 'not_planned';
            v_plan_reason := v_detail;
            v_sqlstate := null;
        else
            v_plan_outcome := 'plan_failed';
            -- El mensaje se guarda solo si tiene forma de codigo: un texto
            -- libre de la base puede traer datos de la persona.
            v_plan_reason := case
                when v_message ~ '^[a-z0-9_]{1,64}$' then v_message
                else 'plan_error_unclassified'
            end;
        end if;
    end;
    -- Vuelve al modo por defecto de los tres triggers (los tres nacen
    -- diferidos) para el resto de la transaccion.
    set constraints all deferred;

    -- 6. El renglon del plan, fuera del bloque. El motivo es siempre un
    --    codigo: lo que no lo sea no se guarda ni se devuelve.
    if v_plan_reason is null or v_plan_reason !~ '^[a-z0-9_]{1,64}$' then
        v_plan_reason := 'plan_reason_unclassified';
    end if;
    insert into public.portable_precheckout_first_contact_plans (
        submission_id, purchase_intent_id, contact_id, recovery_case_id,
        scope_key, scope_version, outcome, reason_code, error_sqlstate
    ) values (
        v_submission_id, v_intent_id, v_contact_id,
        case when v_plan_outcome = 'planned' then v_case_id else null end,
        p_scope_key, p_scope_version, v_plan_outcome, v_plan_reason,
        case when v_plan_outcome = 'plan_failed' then v_sqlstate else null end
    );

    return query select
        v_admission_outcome, v_submission_id, v_intent_id,
        v_plan_outcome, v_plan_reason;
end;
$function$;

create or replace function public.reevaluate_portable_precheckout_action(
    p_action_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_now timestamptz,
    p_chatwoot_checked boolean default false,
    p_chatwoot_conversation_id text default null,
    p_chatwoot_checkpoint_message_id text default null,
    p_chatwoot_checkpoint_at timestamptz default null,
    p_chatwoot_status text default null,
    p_chatwoot_can_reply boolean default null,
    p_chatwoot_anchor_found boolean default null,
    p_chatwoot_automation_paused boolean default null,
    p_chatwoot_inbound_after_anchor boolean default null,
    p_chatwoot_human_activity_after_anchor boolean default null
)
returns table (
    action_id uuid,
    decision text,
    reason_code text,
    case_version bigint,
    sequence_revision bigint
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
    v_case public.recovery_cases%rowtype;
    v_sequence public.followup_sequences%rowtype;
    v_identity public.channel_identities%rowtype;
    v_pilot_binding public.pilot_recovery_case_bindings%rowtype;
    v_replay_data jsonb;
    v_reason text;
    v_detail text;
    v_audience_reason text;
begin
    -- La reevaluacion del primer contacto: los frenos propios del formulario
    -- y, si ninguno aplica, reevaluate_followup_action entera (permisos,
    -- ventana, politica, vencimiento). Las validaciones, los locks y el replay
    -- son los de esa funcion (20260805000300).
    if p_action_id is null
       or p_worker_id is null or btrim(p_worker_id) = ''
       or p_lease_generation is null or p_lease_generation < 1
       or p_now is null then
        raise exception using errcode = '22023', message = 'invalid_followup_fence';
    end if;

    select sa.* into strict v_action
    from public.scheduled_actions sa
    where sa.id = p_action_id;
    if v_action.anchor_type <> 'precheckout_intent' then
        raise exception using
            errcode = '55000',
            message = 'precheckout_intent_action_required';
    end if;

    -- La intencion antes que el contacto: es el orden de la admision del
    -- formulario (que toma la intencion for update y despues el contacto) y
    -- del arranque. Si la reevaluacion la tomara despues del contacto, un
    -- reenvio del formulario de la misma persona y esta funcion se esperarian
    -- en cruz (40P01). El lock espera a quien este cambiando la intencion (la
    -- correlacion de una compra, un formulario posterior) y deja leer lo
    -- confirmado mas abajo.
    perform 1
    from public.purchase_intents intent
    join public.pilot_recovery_case_bindings binding
      on binding.audience_purchase_intent_id = intent.id
    where binding.recovery_case_id = v_action.recovery_case_id
    for share of intent;

    -- Global order for this aggregate: contact -> case -> sequence -> action.
    perform 1
    from public.contacts c
    join public.recovery_cases rc on rc.contact_id = c.id
    where rc.id = v_action.recovery_case_id
    for update of c;

    select rc.* into strict v_case
    from public.recovery_cases rc
    where rc.id = v_action.recovery_case_id
    for update;

    select fs.* into strict v_sequence
    from public.followup_sequences fs
    where fs.id = v_action.followup_sequence_id
    for update;

    select sa.* into strict v_action
    from public.scheduled_actions sa
    where sa.id = p_action_id
    for update;

    -- Replay after a committed response was lost.
    select ce.data into v_replay_data
    from public.conversation_events ce
    where ce.related_action_id = p_action_id
      and ce.event_type = 'followup_action_reevaluated'
      and ce.data ->> 'decision' <> 'execute'
      and ce.data ->> 'worker_id' = p_worker_id
      and (ce.data ->> 'lease_generation')::bigint = p_lease_generation
    order by ce.created_at desc
    limit 1;

    if found then
        return query select
            p_action_id,
            v_replay_data ->> 'decision',
            v_replay_data ->> 'reason_code',
            (v_replay_data ->> 'case_version')::bigint,
            (v_replay_data ->> 'sequence_revision')::bigint;
        return;
    end if;

    if not (
        v_action.lease_owner = p_worker_id
        and v_action.lease_generation = p_lease_generation
        and v_action.lease_expires_at > p_now
        and v_action.status in ('pending', 'deferred', 'retryable_failed')
    ) then
        raise exception using errcode = 'P0002', message = 'current_action_lease_not_found';
    end if;

    -- Una accion vencida la cierra la funcion compartida, igual que a las
    -- demas. Un caso que ya no esta vivo, tambien: ella decide.
    if v_action.expires_at > p_now
       and v_case.status in ('grace_period', 'active')
       and v_sequence.status = 'active' then
        select binding.* into v_pilot_binding
        from public.pilot_recovery_case_bindings binding
        where binding.recovery_case_id = v_case.id;
        select identity.* into v_identity
        from public.channel_identities identity
        where identity.id = v_case.selected_channel_identity_id;

        if v_pilot_binding.recovery_case_id is null
           or v_pilot_binding.audience_purchase_intent_id is null
           or v_identity.id is null then
            v_reason := 'precheckout_authorization_lost';
            v_detail := 'pilot_attempt_mismatch';
        else
            -- La intencion ya esta tomada for share desde el principio.
            v_reason := public._portable_precheckout_stop_reason(
                v_pilot_binding.audience_purchase_intent_id,
                v_case.contact_id
            );
            if v_reason is null then
                select audience.reason_code into v_audience_reason
                from public._lancemos_pilot_audience_intent(
                    v_pilot_binding.scope_key,
                    v_pilot_binding.scope_version,
                    v_case.contact_id,
                    v_case.offer_code,
                    v_pilot_binding.audience_purchase_intent_id,
                    v_identity.external_user_id
                ) audience;
                if v_audience_reason is distinct from 'pilot_audience_allowed' then
                    v_reason := 'precheckout_authorization_lost';
                    v_detail := coalesce(
                        v_audience_reason, 'pilot_audience_intent_unresolved'
                    );
                end if;
            end if;
        end if;
    end if;

    if v_reason is null then
        return query
        select reevaluation.action_id,
               reevaluation.decision,
               reevaluation.reason_code,
               reevaluation.case_version,
               reevaluation.sequence_revision
        from public.reevaluate_followup_action(
            p_action_id,
            p_worker_id,
            p_lease_generation,
            p_now,
            p_chatwoot_checked,
            p_chatwoot_conversation_id,
            p_chatwoot_checkpoint_message_id,
            p_chatwoot_checkpoint_at,
            p_chatwoot_status,
            p_chatwoot_can_reply,
            p_chatwoot_anchor_found,
            p_chatwoot_automation_paused,
            p_chatwoot_inbound_after_anchor,
            p_chatwoot_human_activity_after_anchor
        ) reevaluation;
        return;
    end if;

    -- El caso se cancela: no se gana (D2). La venta de quien compro sin que
    -- se le escribiera no es de este flujo.
    update public.scheduled_actions
    set status = 'cancelled', terminal_reason = v_reason,
        lease_owner = null, lease_expires_at = null
    where id = p_action_id;
    update public.followup_sequences
    set status = 'completed', completion_reason = v_reason,
        completed_at = p_now, revision = revision + 1
    where id = v_sequence.id and status = 'active';
    update public.recovery_cases
    set status = 'cancelled', closed_at = p_now, version = version + 1
    where id = v_case.id and status in ('grace_period', 'active');

    select rc.* into strict v_case
    from public.recovery_cases rc
    where rc.id = v_case.id;
    select fs.* into strict v_sequence
    from public.followup_sequences fs
    where fs.id = v_sequence.id;

    insert into public.conversation_events (
        recovery_case_id, event_type, actor_type, related_action_id, data
    ) values (
        v_case.id, 'followup_action_reevaluated', 'system', p_action_id,
        jsonb_build_object('decision', 'cancel', 'reason_code', v_reason,
                           'detail', v_detail,
                           'worker_id', p_worker_id,
                           'policy_key', v_action.policy_key,
                           'policy_version', v_action.policy_version,
                           'case_version', v_case.version,
                           'sequence_revision', v_sequence.revision,
                           'lease_generation', p_lease_generation,
                           'chatwoot_checked', p_chatwoot_checked)
    );

    return query select p_action_id, 'cancel'::text, v_reason,
                        v_case.version, v_sequence.revision;
end;
$function$;

create or replace function public.mark_portable_precheckout_request_started(
    p_action_id uuid,
    p_attempt_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_now timestamptz
)
returns table (
    id uuid,
    action_id uuid,
    idempotency_key text,
    attempt_number integer,
    channel text,
    mode text,
    phase text,
    lease_generation bigint,
    expected_case_version bigint,
    expected_sequence_revision bigint,
    pilot_authorization_id uuid,
    pilot_runtime_generation bigint,
    pilot_authorization_replayed boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_case public.recovery_cases%rowtype;
    v_identity public.channel_identities%rowtype;
    v_binding public.pilot_recovery_case_bindings%rowtype;
    v_scope public.pilot_scope_versions%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_account_id bigint;
    v_inbox_id bigint;
    v_authorized boolean;
    v_reason text;
    v_runtime_generation bigint;
    v_authorization_id uuid;
    v_replayed boolean;
    -- precheckout_first_contact: begin
    v_variant text;
    v_stop_reason text;
    -- precheckout_first_contact: end
begin
    if p_action_id is null
       or p_attempt_id is null
       or p_worker_id is null or btrim(p_worker_id) = ''
       or p_lease_generation is null or p_lease_generation < 1
       or p_now is null then
        raise exception using
            errcode = '22023',
            message = 'invalid_pilot_request_start_parameters';
    end if;

    if not exists (
        select 1 from public.scheduled_actions action
        where action.id = p_action_id
          and action.anchor_type = 'precheckout_intent'
    ) then
        raise exception using
            errcode = '55000',
            message = 'pilot_request_start_rejected',
            detail = 'precheckout_intent_action_required';
    end if;

    select recovery_case.* into v_case
    from public.scheduled_actions action
    join public.recovery_cases recovery_case
      on recovery_case.id = action.recovery_case_id
    join public.pilot_recovery_case_bindings binding
      on binding.recovery_case_id = recovery_case.id
    where action.id = p_action_id;
    if not found or v_case.selected_channel_identity_id is null then
        raise exception using
            errcode = '55000',
            message = 'pilot_request_start_rejected',
            detail = 'pilot_attempt_mismatch';
    end if;

    select binding.* into strict v_binding
    from public.pilot_recovery_case_bindings binding
    where binding.recovery_case_id = v_case.id;
    select scope.* into strict v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = v_binding.scope_key
      and scope.version = v_binding.scope_version;

    select attempt.* into v_attempt
    from public.followup_delivery_attempts attempt
    where attempt.id = p_attempt_id
      and attempt.action_id = p_action_id;
    if not found
       or v_attempt.channel <> 'whatsapp'
       or (
           v_scope.channel_provider = 'waba'
           and v_attempt.mode <> 'approved_template'
       )
       or (
           v_scope.channel_provider <> 'waba'
           and v_attempt.mode <> 'freeform'
       ) then
        raise exception using
            errcode = '55000',
            message = 'pilot_request_start_rejected',
            detail = 'pilot_delivery_mode_mismatch';
    end if;

    select identity.* into v_identity
    from public.channel_identities identity
    where identity.id = v_case.selected_channel_identity_id;
    if not found
       or v_identity.account_id !~ '^chatwoot:[0-9]+$'
       or coalesce(v_identity.metadata ->> 'inbox_id', '') !~ '^[0-9]+$' then
        raise exception using
            errcode = '55000',
            message = 'pilot_request_start_rejected',
            detail = 'pilot_attempt_mismatch';
    end if;

    v_account_id := substring(v_identity.account_id from '^chatwoot:([0-9]+)$')::bigint;
    v_inbox_id := (v_identity.metadata ->> 'inbox_id')::bigint;

    select auth_result.authorized,
           auth_result.reason_code,
           auth_result.runtime_generation,
           auth_result.request_authorization_id,
           auth_result.replayed
      into v_authorized,
           v_reason,
           v_runtime_generation,
           v_authorization_id,
           v_replayed
    from public.authorize_lancemos_pilot_request_start(
        v_binding.scope_key,
        v_binding.scope_version,
        v_scope.tenant_key,
        v_account_id,
        v_inbox_id,
        v_scope.channel_provider,
        v_scope.channel_account_ref,
        'landing',
        'PRECHECKOUT_FORM_SUBMITTED',
        v_case.external_product_id,
        v_case.offer_code,
        v_case.contact_id,
        p_action_id,
        p_attempt_id,
        p_now
    ) auth_result;

    if not coalesce(v_authorized, false) then
        raise exception using
            errcode = '55000',
            message = 'pilot_request_start_rejected',
            detail = coalesce(v_reason, 'pilot_request_start_unknown');
    end if;

    if v_replayed then
        select attempt.* into v_attempt
        from public.followup_delivery_attempts attempt
        where attempt.id = p_attempt_id
          and attempt.action_id = p_action_id;
        if not found or v_attempt.phase <> 'request_started' then
            raise exception using
                errcode = '55000',
                message = 'pilot_authorization_without_request_start';
        end if;
    end if;

    -- precheckout_first_contact: begin
    -- Los frenos del primer contacto, otra vez, justo antes de arrancar. La
    -- autorizacion de arriba re-verifico la intencion, pero no ve una compra
    -- ambigua ni una compra por identidad, y el opt-out de Chatwoot se guarda
    -- con el wa_id textual: se toma el lock de opt-out de las dos formas del
    -- telefono (en orden) para esperar a una baja en vuelo desde cualquiera, y
    -- recien ahi se mira. Va despues de la autorizacion para conservar el
    -- orden control -> lock de opt-out de los otros arranques; el rechazo
    -- deshace la autorizacion, asi que no consume cupo. El replay no pasa por
    -- aca: ese efecto ya cruzo.
    if not v_replayed then
        for v_variant in
            select variant.phone
            from unnest(
                public._whatsapp_phone_variants(v_identity.external_user_id)
            ) as variant(phone)
            order by variant.phone
        loop
            perform pg_advisory_xact_lock(hashtextextended(
                concat_ws(':', 'chatwoot-opt-out-user', v_account_id, v_variant),
                0
            ));
        end loop;
        v_stop_reason := public._portable_precheckout_stop_reason(
            v_binding.audience_purchase_intent_id,
            v_case.contact_id
        );
        if v_stop_reason is not null then
            raise exception using
                errcode = '55000',
                message = 'pilot_request_start_rejected',
                detail = v_stop_reason;
        end if;
    end if;
    -- precheckout_first_contact: end

    select attempt.* into strict v_attempt
    from public.mark_followup_request_started(
        p_action_id,
        p_attempt_id,
        p_worker_id,
        p_lease_generation,
        p_now
    ) attempt;

    return query select
        v_attempt.id,
        v_attempt.action_id,
        v_attempt.idempotency_key,
        v_attempt.attempt_number,
        v_attempt.channel,
        v_attempt.mode,
        v_attempt.phase,
        v_attempt.lease_generation,
        v_attempt.expected_case_version,
        v_attempt.expected_sequence_revision,
        v_authorization_id,
        v_runtime_generation,
        v_replayed;
end;
$function$;

create or replace function public.get_portable_precheckout_pilot_runtime_status(
    p_scope_key text,
    p_scope_version integer,
    p_tenant_key text,
    p_channel_provider text,
    p_channel_account_ref text
)
returns table (
    configured boolean,
    runtime_state text,
    runtime_generation bigint,
    reason_code text
)
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $function$
declare
    v_scope public.pilot_scope_versions%rowtype;
    v_control public.pilot_runtime_controls%rowtype;
begin
    if p_scope_key is null or btrim(p_scope_key) = ''
       or p_scope_version is null or p_scope_version < 1
       or p_tenant_key is null or btrim(p_tenant_key) = ''
       or p_channel_provider is null or btrim(p_channel_provider) = ''
       or p_channel_account_ref is null or btrim(p_channel_account_ref) = '' then
        return query select false, null::text, null::bigint,
            'pilot_runtime_config_invalid'::text;
        return;
    end if;

    select scope.* into v_scope
    from public.pilot_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published';
    if not found
       or v_scope.tenant_key <> p_tenant_key
       or v_scope.channel_provider <> p_channel_provider
       or v_scope.channel_account_ref <> p_channel_account_ref
       or v_scope.source <> 'landing'
       or (
           v_scope.source_event_type <> 'PRECHECKOUT_FORM_SUBMITTED'
           and 'PRECHECKOUT_FORM_SUBMITTED' <> all(v_scope.additional_source_event_types)
       )
       -- precheckout_first_contact: begin
       -- El planificador del primer contacto no acepta manual_cohort: un scope
       -- asi no esta configurado para este flujo.
       or v_scope.audience_mode = 'manual_cohort'
       -- precheckout_first_contact: end
       then
        return query select false, null::text, null::bigint,
            'pilot_scope_config_mismatch'::text;
        return;
    end if;

    select control.* into v_control
    from public.pilot_runtime_controls control
    where control.scope_key = p_scope_key;
    if not found or v_control.scope_version <> p_scope_version then
        return query select false, null::text, null::bigint,
            'pilot_active_scope_mismatch'::text;
        return;
    end if;

    return query select
        true,
        v_control.runtime_state,
        v_control.generation,
        ('pilot_runtime_' || v_control.runtime_state)::text;
end;
$function$;

-- Supabase le da execute por defecto a toda funcion nueva y todos los
-- privilegios de tabla a service_role: todo se revoca explicito. Los cuatro
-- entrypoints quedan solo para service_role; los cuatro helpers y la tabla, para
-- nadie de la API (los usan las funciones security definer de arriba).
revoke all on table public.portable_precheckout_first_contact_plans from public;
revoke all on function public._portable_precheckout_stop_reason(uuid,uuid) from public;
revoke all on function public._find_portable_precheckout_contact(uuid) from public;
revoke all on function public._ensure_portable_precheckout_contact(uuid,uuid) from public;
revoke all on function public._plan_portable_precheckout_first_contact(uuid,uuid,uuid,text,integer) from public;
revoke all on function public.admit_and_plan_portable_lead_precheckout(text,text,integer,text,jsonb,jsonb,text,integer) from public;
revoke all on function public.reevaluate_portable_precheckout_action(uuid,text,bigint,timestamptz,boolean,text,text,timestamptz,text,boolean,boolean,boolean,boolean,boolean) from public;
revoke all on function public.mark_portable_precheckout_request_started(uuid,uuid,text,bigint,timestamptz) from public;
revoke all on function public.get_portable_precheckout_pilot_runtime_status(text,integer,text,text,text) from public;
do $roles$
declare v_role text;
begin
 for v_role in select rolname from pg_roles where rolname in ('anon','authenticated','service_role') loop
  execute format('revoke all on table public.portable_precheckout_first_contact_plans from %I',v_role);
  execute format('revoke all on function public._portable_precheckout_stop_reason(uuid,uuid) from %I',v_role);
  execute format('revoke all on function public._find_portable_precheckout_contact(uuid) from %I',v_role);
  execute format('revoke all on function public._ensure_portable_precheckout_contact(uuid,uuid) from %I',v_role);
  execute format('revoke all on function public._plan_portable_precheckout_first_contact(uuid,uuid,uuid,text,integer) from %I',v_role);
  execute format('revoke all on function public.admit_and_plan_portable_lead_precheckout(text,text,integer,text,jsonb,jsonb,text,integer) from %I',v_role);
  execute format('revoke all on function public.reevaluate_portable_precheckout_action(uuid,text,bigint,timestamptz,boolean,text,text,timestamptz,text,boolean,boolean,boolean,boolean,boolean) from %I',v_role);
  execute format('revoke all on function public.mark_portable_precheckout_request_started(uuid,uuid,text,bigint,timestamptz) from %I',v_role);
  execute format('revoke all on function public.get_portable_precheckout_pilot_runtime_status(text,integer,text,text,text) from %I',v_role);
 end loop;
 if exists(select 1 from pg_roles where rolname='service_role') then
  grant execute on function public.admit_and_plan_portable_lead_precheckout(text,text,integer,text,jsonb,jsonb,text,integer) to service_role;
  grant execute on function public.reevaluate_portable_precheckout_action(uuid,text,bigint,timestamptz,boolean,text,text,timestamptz,text,boolean,boolean,boolean,boolean,boolean) to service_role;
  grant execute on function public.mark_portable_precheckout_request_started(uuid,uuid,text,bigint,timestamptz) to service_role;
  grant execute on function public.get_portable_precheckout_pilot_runtime_status(text,integer,text,text,text) to service_role;
 end if;
end;
$roles$;

commit;
