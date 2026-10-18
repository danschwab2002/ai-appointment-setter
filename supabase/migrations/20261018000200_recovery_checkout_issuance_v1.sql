-- Migration: el link de pago del primer mensaje del carrito (A1 de ATT1), atado
-- al caso de recuperacion, y la compra hecha con ese link cierra el caso.
--
-- Por que. A1 (la plantilla att1_carrito_descuento_01) sale con un boton URL al
-- checkout de Hotmart cuyo sufijo es NUESTRO link emitido mas
-- &offDiscount=<cupon personal> (el cupon lo agrega el bridge; la URL de la
-- tabla no lo lleva y sigue cumpliendo checkout_link_issuances_url_shape). La
-- emision de hoy (reserve_chatwoot_checkout_issuance_v2 y la portable) exige un
-- caso inbound_sales, una conversacion de Chatwoot y el mensaje que la
-- disparo. Cuando sale A1 el carrito no tiene conversacion: la crea el envio.
-- Y una compra con nuestro sck entra por
-- admit_and_correlate_hotmart_checkout_issuance_v2, no por la admision portable
-- por identidad, asi que nadie cerraba el caso del carrito: A2 y Z saldrian a
-- quien ya compro.
--
-- Que cambia en checkout_link_issuances:
-- - source_kind suma 'recovery_first_touch';
-- - chatwoot_conversation_id y trigger_external_message_id pasan a nullable;
-- - columnas nuevas recovery_case_id y delivery_attempt_id (un link por
--   intento de envio: indice unico parcial);
-- - checkout_link_issuances_origin_shape: una fila recovery_first_touch tiene
--   caso, intento y commercial_case_id = recovery_case_id, y no tiene
--   conversacion ni disparador; cualquier otra fila sigue como hoy
--   (conversacion y disparador, sin caso ni intento). Las filas que ya
--   existen pasan;
-- - la submission puede estar o no en una fila recovery_first_touch (la ficha
--   del formulario, si la intencion la tiene, da el sck del anuncio);
-- - offer_resolution suma 'recovery_case_offer' (la oferta del carrito,
--   recovery_cases.offer_code, encontrada en el catalogo);
-- - el trigger de inmutabilidad cubre las dos columnas nuevas.
-- La cuenta, el inbox y la identidad siguen obligatorios: salen de la
-- identidad del caso (la misma que exige mark_lancemos_pilot_request_started
-- para arrancar el envio) y se cruzan contra el binding de la aliada.
--
-- Funciones nuevas (entrypoints del bridge, solo service_role):
-- - reserve_recovery_checkout_issuance_v1: con el cerco del lease del
--   despachador (el de get_followup_execution_context), sobre el
--   first_contact_review de un carrito activo, y el intento reservado de esa
--   accion. Resuelve el binding por el evento del abandono
--   (commercial_ally_hotmart_event_bindings), la oferta del catalogo del scope
--   entrante del binding con offer_code = recovery_cases.offer_code (si no
--   esta, la default con la resolucion que lo dice), la intencion del caso (la
--   del caso, o la correlacionada al abandono, o una viva de la oferta, o la
--   fabrica como la reserva entrante) y compone el sck y el fbclid como la
--   reserva entrante: hermes~v1~<ulid>, con el sck del anuncio delante si la
--   intencion tiene ficha. Idempotente por intento: el reintento devuelve la
--   misma fila;
-- - authorize_recovery_checkout_issuance_v1: reserved -> request_started, con
--   el mismo cerco, revisando caso activo, contacto sin baja ni bloqueo, la
--   identidad, el opt-out de Chatwoot de la cuenta e intencion esperando
--   compra. El cierre reusa finalize_chatwoot_checkout_issuance_v2 tal cual:
--   busca por id y no mira la conversacion.
--
-- La compra: _close_recovery_case_for_checkout_purchase_v1, helper privado que
-- llama correlate_hotmart_checkout_issuance_v2 justo despues de marcar la
-- emision purchase_matched. Si la emision tiene recovery_case_id, el caso pasa
-- a won (purchase_event_id, won_at = approved_date de Hotmart, version + 1) con
-- su secuencia completada y sus acciones pendientes canceladas como hace
-- apply_hotmart_purchase_approved (20260808000100: las que tienen un envio en
-- vuelo quedan delivery_unknown, y el trigger de 20260808000400 cierra sus
-- intentos), y si data.purchase.offer.coupon_code es uno de los cupones de ese
-- caso en created o valid, queda redeemed con el evento. El correlador se
-- modifica sobre su definicion VIVA con un reemplazo contado (el metodo de
-- 20261005000100): si el texto no aparece exactamente una vez, la migracion
-- falla con 55000 y no cambia nada.
--
-- Los lectores de la tabla que se revisaron (ninguno se rompe ni toma una fila
-- recovery_first_touch por error):
-- - reserve_chatwoot_checkout_issuance_v2, la portable y la del funnel buscan
--   por (cuenta, inbox, conversacion, disparador) con los cuatro no nulos: una
--   fila con conversacion nula nunca coincide, y el unique de esas cuatro
--   columnas admite varios nulos;
-- - authorize_chatwoot_checkout_issuance_v2 comparaba la conversacion y el
--   disparador con <>: con NULL la condicion da NULL y el IF la toma como
--   falsa, o sea que esa guarda dejaba pasar una fila del carrito (despues la
--   frenaba el case_kind inbound_sales). Se cambia, sobre la definicion viva,
--   por is distinct from y se rechaza explicito recovery_first_touch;
-- - finalize_chatwoot_checkout_issuance_v2: por id, sirve para las dos;
-- - claim_conversation_followup_v1 y la portable: leen solo la emision que
--   ellas mismas reservaron;
-- - get_daily_feedback_conversation_context_v1: filtra por conversacion
--   igual, asi que el link de A1 no aparece en el contexto de la revision;
-- - get_portable_person_activity_v1 (20261016000300): la compra con el link
--   de A1 cuenta como 'purchase', que es lo correcto;
-- - las lecturas del Automation Expert (20261015000200): la fila entra por el
--   inbox, con conversation null; el numero de conversacion se descarta si es
--   nulo. La compra figura con link_source 'agent' (queda para el bridge).
--
-- Lo que NO hace: no toca el bridge ni prende nada; nadie llama a las RPC
-- nuevas todavia. No cambia el sck ni la URL de los links del entrante. No
-- marca el webhook como processed (la ruta del link nunca lo hizo).

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- 1. La tabla.
alter table public.checkout_link_issuances
    add column recovery_case_id uuid
        references public.recovery_cases(id) on delete restrict,
    add column delivery_attempt_id uuid
        references public.followup_delivery_attempts(id) on delete restrict;

alter table public.checkout_link_issuances
    alter column chatwoot_conversation_id drop not null,
    alter column trigger_external_message_id drop not null;

-- Los dos CHECK anonimos de 20260914000100 que nombran source_kind se buscan
-- por su definicion, como 20260922000200 busco el del sck.
do $source_checks$
declare
    v_kind text[];
    v_submission text[];
begin
    select array_agg(con.conname order by con.conname) into v_kind
    from pg_catalog.pg_constraint con
    where con.conrelid = 'public.checkout_link_issuances'::regclass
      and con.contype = 'c'
      and pg_catalog.pg_get_constraintdef(con.oid) like '%source_kind%'
      and pg_catalog.pg_get_constraintdef(con.oid) not like '%source_submission_id%';
    select array_agg(con.conname order by con.conname) into v_submission
    from pg_catalog.pg_constraint con
    where con.conrelid = 'public.checkout_link_issuances'::regclass
      and con.contype = 'c'
      and pg_catalog.pg_get_constraintdef(con.oid) like '%source_kind%'
      and pg_catalog.pg_get_constraintdef(con.oid) like '%source_submission_id%';
    if coalesce(cardinality(v_kind), 0) <> 1
       or coalesce(cardinality(v_submission), 0) <> 1 then
        raise exception using errcode = '55000',
            message = 'checkout_link_issuances_source_checks_not_found',
            detail = format('%s / %s', v_kind, v_submission);
    end if;
    execute format(
        'alter table public.checkout_link_issuances drop constraint %I', v_kind[1]
    );
    execute format(
        'alter table public.checkout_link_issuances drop constraint %I', v_submission[1]
    );
end
$source_checks$;

alter table public.checkout_link_issuances
    add constraint checkout_link_issuances_source_kind_check check (
        source_kind in (
            'inbound_request', 'precheckout_request', 'recovery_first_touch'
        )
    ),
    add constraint checkout_link_issuances_source_submission_shape check (
        (source_kind = 'inbound_request' and source_submission_id is null)
        or (source_kind = 'precheckout_request' and source_submission_id is not null)
        or source_kind = 'recovery_first_touch'
    ),
    add constraint checkout_link_issuances_origin_shape check (
        (source_kind = 'recovery_first_touch'
            and recovery_case_id is not null
            and delivery_attempt_id is not null
            and commercial_case_id = recovery_case_id
            and chatwoot_conversation_id is null
            and trigger_external_message_id is null)
        or (source_kind <> 'recovery_first_touch'
            and recovery_case_id is null
            and delivery_attempt_id is null
            and chatwoot_conversation_id is not null
            and trigger_external_message_id is not null)
    );

alter table public.checkout_link_issuances
    drop constraint checkout_link_issuances_offer_resolution_check;

alter table public.checkout_link_issuances
    add constraint checkout_link_issuances_offer_resolution_check check (
        offer_resolution in (
            'lead_intent',
            'default_no_intent',
            'default_offer_not_in_catalog',
            'catalog_default',
            'recovery_case_offer'
        )
    );

create unique index checkout_link_issuances_delivery_attempt_idx
on public.checkout_link_issuances (delivery_attempt_id)
where delivery_attempt_id is not null;

create index checkout_link_issuances_recovery_case_idx
on public.checkout_link_issuances (recovery_case_id)
where recovery_case_id is not null;

-- 2. Las columnas nuevas tambien son inmutables (vigente: 20260922000200).
create or replace function public.protect_checkout_link_issuance()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op = 'DELETE'
       or old.id is distinct from new.id
       or old.issuance_ulid is distinct from new.issuance_ulid
       or old.commercial_case_id is distinct from new.commercial_case_id
       or old.purchase_intent_id is distinct from new.purchase_intent_id
       or old.contact_id is distinct from new.contact_id
       or old.channel_identity_id is distinct from new.channel_identity_id
       or old.offer_catalog_id is distinct from new.offer_catalog_id
       or old.chatwoot_account_id is distinct from new.chatwoot_account_id
       or old.chatwoot_inbox_id is distinct from new.chatwoot_inbox_id
       or old.chatwoot_conversation_id is distinct from new.chatwoot_conversation_id
       or old.trigger_external_message_id is distinct from new.trigger_external_message_id
       or old.source_kind is distinct from new.source_kind
       or old.source_submission_id is distinct from new.source_submission_id
       or old.original_sck is distinct from new.original_sck
       or old.source_value is distinct from new.source_value
       or old.sck_format_version is distinct from new.sck_format_version
       or old.sck_value is distinct from new.sck_value
       or old.checkout_url_final is distinct from new.checkout_url_final
       or old.offer_resolution is distinct from new.offer_resolution
       or old.lead_offer_code is distinct from new.lead_offer_code
       or old.attribution_resolution is distinct from new.attribution_resolution
       or old.dropped_unsafe_fields is distinct from new.dropped_unsafe_fields
       or old.recovery_case_id is distinct from new.recovery_case_id
       or old.delivery_attempt_id is distinct from new.delivery_attempt_id
       or old.created_at is distinct from new.created_at
       or not (
            (old.status = 'reserved'
                and new.status in ('request_started', 'purchase_matched'))
            or (old.status = 'request_started'
                and new.status in ('accepted_by_chatwoot', 'delivery_unknown', 'purchase_matched'))
            or (old.status = 'delivery_unknown'
                and new.status in ('accepted_by_chatwoot', 'purchase_matched'))
            or (old.status = 'accepted_by_chatwoot'
                and new.status = 'purchase_matched')
       ) then
        raise exception using errcode = '55000', message = 'checkout_link_issuance_immutable';
    end if;
    return new;
end;
$function$;

-- 3. La autorizacion del entrante deja de aceptar una fila sin conversacion
--    por la comparacion con NULL, y rechaza explicito la del carrito. Sobre la
--    definicion viva (20260914000100, su unica definicion), con el texto
--    contado.
do $authorize_guard$
declare
    v_signature constant text :=
        'public.authorize_chatwoot_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,timestamptz)';
    v_old constant text :=
        E'       or v_issuance.chatwoot_conversation_id <> p_chatwoot_conversation_id\n'
        || E'       or v_issuance.trigger_external_message_id <> p_trigger_external_message_id then\n';
    v_new constant text :=
        E'       or v_issuance.chatwoot_conversation_id is distinct from p_chatwoot_conversation_id\n'
        || E'       or v_issuance.trigger_external_message_id is distinct from p_trigger_external_message_id\n'
        || E'       -- recovery_checkout_issuance: la del carrito se autoriza con la suya.\n'
        || E'       or v_issuance.source_kind = ''recovery_first_touch'' then\n';
    v_definition text;
begin
    if to_regprocedure(v_signature) is null then
        raise exception using errcode = '55000',
            message = 'recovery_checkout_issuance_function_missing',
            detail = v_signature;
    end if;
    v_definition := pg_get_functiondef(v_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, v_old, ''))
           <> length(v_old)
       or position('recovery_checkout_issuance' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_checkout_issuance_authorize_definition',
            detail = v_signature;
    end if;
    execute replace(v_definition, v_old, v_new);
    if position(v_new in pg_get_functiondef(v_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000',
            message = 'checkout_issuance_authorize_not_replaced',
            detail = v_signature;
    end if;
end
$authorize_guard$;

-- 4. La reserva del link de A1.
create function public.reserve_recovery_checkout_issuance_v1(
    p_action_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_attempt_id uuid,
    p_issuance_ulid text,
    p_now timestamptz
)
returns table (
    outcome text,
    issuance_id uuid,
    checkout_url_final text,
    sck_value text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_action public.scheduled_actions%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_case public.recovery_cases%rowtype;
    v_contact public.contacts%rowtype;
    v_identity public.channel_identities%rowtype;
    v_provenance public.commercial_ally_hotmart_event_bindings%rowtype;
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_default_offer public.checkout_offer_catalog%rowtype;
    v_offer public.checkout_offer_catalog%rowtype;
    v_intent public.purchase_intents%rowtype;
    v_submission public.precheckout_submissions%rowtype;
    v_issuance public.checkout_link_issuances%rowtype;
    v_account_id bigint;
    v_inbox_id bigint;
    v_phones text[];
    v_intent_id uuid;
    v_offer_resolution text;
    v_original_sck text;
    v_preserved_sck text;
    v_lead_fbclid text;
    v_sck_value text;
    v_attribution_resolution text;
    v_dropped_unsafe text;
    v_url text;
begin
    -- recovery_checkout_issuance: validar la entrada
    if p_action_id is null
       or p_worker_id is null or btrim(p_worker_id) = ''
       or p_lease_generation is null or p_lease_generation < 1
       or p_attempt_id is null
       or p_issuance_ulid is null
       or p_issuance_ulid !~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'
       or p_now is null then
        raise exception using errcode = '22023',
            message = 'invalid_recovery_checkout_issuance_input';
    end if;

    -- El cerco del despachador, el de get_followup_execution_context.
    select sa.* into v_action
    from public.scheduled_actions sa
    where sa.id = p_action_id;
    if not found or not (
        v_action.lease_owner = p_worker_id
        and v_action.lease_generation = p_lease_generation
        and v_action.lease_expires_at > p_now
        and v_action.expires_at > p_now
        and v_action.status in ('pending', 'deferred', 'retryable_failed')
    ) then
        raise exception using errcode = '55000', message = 'current_action_lease_not_found';
    end if;
    if v_action.action_type <> 'first_contact_review'
       or v_action.anchor_type is distinct from 'cart_abandonment'
       or v_action.recovery_case_id is null then
        raise exception using errcode = '22023',
            message = 'recovery_checkout_issuance_requires_cart_first_contact';
    end if;

    select attempt.* into v_attempt
    from public.followup_delivery_attempts attempt
    where attempt.id = p_attempt_id;
    if not found
       or v_attempt.action_id <> p_action_id
       or v_attempt.lease_generation <> p_lease_generation then
        raise exception using errcode = '55000',
            message = 'recovery_checkout_issuance_attempt_not_found';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        'recovery-checkout-issuance:' || p_attempt_id::text, 0
    ));

    -- Idempotente por intento: el reintento devuelve la misma fila.
    select issuance.* into v_issuance
    from public.checkout_link_issuances issuance
    where issuance.delivery_attempt_id = p_attempt_id;
    if found then
        return query select
            case
                when v_issuance.status = 'accepted_by_chatwoot' then 'already_accepted'
                when v_issuance.status = 'delivery_unknown' then 'delivery_unknown'
                when v_issuance.status = 'purchase_matched' then 'purchase_already_approved'
                when v_issuance.status = 'request_started' then 'request_started_replay'
                else 'reserved'
            end,
            v_issuance.id, v_issuance.checkout_url_final, v_issuance.sck_value;
        return;
    end if;

    if v_attempt.phase <> 'reserved' then
        return query select 'blocked_attempt'::text, null::uuid, null::text, null::text;
        return;
    end if;

    select rc.* into v_case
    from public.recovery_cases rc
    where rc.id = v_action.recovery_case_id;
    if not found
       or v_case.status not in ('grace_period', 'active')
       or v_case.purchase_event_id is not null then
        return query select 'blocked_case'::text, null::uuid, null::text, null::text;
        return;
    end if;

    select contact.* into v_contact
    from public.contacts contact
    where contact.id = v_case.contact_id;
    if not found
       or v_contact.contact_permission in ('opted_out', 'blocked', 'restricted')
       or v_contact.lifecycle_status = 'do_not_contact' then
        return query select 'blocked_contact'::text, null::uuid, null::text, null::text;
        return;
    end if;

    -- La identidad a la que sale A1: la del caso, con su cuenta y su inbox.
    select identity.* into v_identity
    from public.channel_identities identity
    where identity.id = v_case.selected_channel_identity_id
      and identity.contact_id = v_case.contact_id
      and identity.channel = 'whatsapp'
      and identity.identity_status = 'active'
      and identity.external_user_id ~ '^[1-9][0-9]{7,14}$'
      and identity.account_id ~ '^chatwoot:[1-9][0-9]*$'
      and coalesce(identity.metadata ->> 'inbox_id', '') ~ '^[1-9][0-9]*$';
    if not found then
        return query select 'blocked_identity'::text, null::uuid, null::text, null::text;
        return;
    end if;
    v_account_id := substring(v_identity.account_id from '^chatwoot:([0-9]+)$')::bigint;
    v_inbox_id := (v_identity.metadata ->> 'inbox_id')::bigint;

    -- La aliada y el funnel: los del abandono, con el binding activo en la
    -- misma cuenta y el mismo inbox.
    select provenance.* into v_provenance
    from public.commercial_ally_hotmart_event_bindings provenance
    where provenance.webhook_event_id = v_case.abandonment_event_id;
    if found then
        select binding.* into v_binding
        from public.commercial_ally_runtime_bindings binding
        where binding.tenant_ref = v_provenance.tenant_ref
          and binding.funnel_ref = v_provenance.funnel_ref
          and binding.binding_version = v_provenance.binding_version
          and binding.status = 'active'
          and binding.chatwoot_account_id = v_account_id
          and binding.chatwoot_inbox_id = v_inbox_id;
    end if;
    if v_binding.tenant_ref is null then
        return query select 'blocked_binding'::text, null::uuid, null::text, null::text;
        return;
    end if;

    -- La oferta: la del carrito en el catalogo del scope entrante del binding;
    -- si no esta, la default.
    select offer.* into v_default_offer
    from public.checkout_offer_catalog offer
    where offer.tenant_ref = v_binding.tenant_ref
      and offer.funnel_ref = v_binding.funnel_ref
      and offer.scope_key = v_binding.inbound_scope_key
      and offer.scope_version = v_binding.inbound_scope_version
      and lower(offer.product_ref) = lower(v_binding.product_hotlink)
      and offer.status = 'active'
      and offer.default_for_inbound;

    if nullif(btrim(v_case.offer_code), '') is not null then
        select offer.* into v_offer
        from public.checkout_offer_catalog offer
        where offer.tenant_ref = v_binding.tenant_ref
          and offer.funnel_ref = v_binding.funnel_ref
          and offer.scope_key = v_binding.inbound_scope_key
          and offer.scope_version = v_binding.inbound_scope_version
          and lower(offer.product_ref) = lower(v_binding.product_hotlink)
          and offer.offer_code = v_case.offer_code
          and offer.status = 'active'
        order by offer.version desc, offer.default_for_inbound desc
        limit 1;
        if found then
            v_offer_resolution := 'recovery_case_offer';
        else
            v_offer := v_default_offer;
            v_offer_resolution := 'default_offer_not_in_catalog';
        end if;
    else
        v_offer := v_default_offer;
        v_offer_resolution := 'default_no_intent';
    end if;
    if v_offer.id is null then
        return query select 'missing_offer'::text, null::uuid, null::text, null::text;
        return;
    end if;

    v_phones := coalesce(
        public._whatsapp_phone_variants(v_identity.external_user_id),
        array[v_identity.external_user_id]
    ) || coalesce(public._whatsapp_phone_variants(v_contact.phone), array[]::text[]);

    -- Una compra conocida del producto frena, venga por la oferta que venga.
    if exists (
        select 1 from public.purchase_intents intent
        where intent.tenant_ref = v_offer.tenant_ref
          and intent.funnel_ref = v_offer.funnel_ref
          and lower(intent.product_ref) = lower(v_offer.product_ref)
          and intent.normalized_phone = any(v_phones)
          and intent.lifecycle_state = 'purchased'
    ) then
        return query select 'purchase_already_approved'::text, null::uuid, null::text, null::text;
        return;
    end if;

    -- La intencion: la del caso, o la correlacionada al abandono.
    v_intent_id := v_case.hotmart_purchase_intent_id;
    if v_intent_id is null then
        select correlation.purchase_intent_id into v_intent_id
        from public.hotmart_purchase_intent_correlations correlation
        where correlation.webhook_event_id = v_case.abandonment_event_id
          and correlation.outcome = 'resolved';
    end if;
    if v_intent_id is not null then
        select intent.* into v_intent
        from public.purchase_intents intent
        where intent.id = v_intent_id
        for update;
        if found and v_intent.lifecycle_state = 'purchased' then
            return query select 'purchase_already_approved'::text, null::uuid, null::text, null::text;
            return;
        end if;
        if not found or v_intent.lifecycle_state <> 'waiting_for_purchase' then
            v_intent := null;
        end if;
    end if;

    -- Si no, una viva de la oferta, o la fabrica como la reserva entrante.
    if v_intent.id is null then
        select intent.* into v_intent
        from public.purchase_intents intent
        where intent.tenant_ref = v_offer.tenant_ref
          and intent.funnel_ref = v_offer.funnel_ref
          and intent.landing_ref = v_offer.landing_ref
          and lower(intent.product_ref) = lower(v_offer.product_ref)
          and intent.offer_ref = v_offer.offer_code
          and intent.normalized_phone = any(v_phones)
          and intent.lifecycle_state = 'waiting_for_purchase'
        order by intent.submitted_at desc, intent.id desc
        limit 1
        for update;
        if not found then
            insert into public.purchase_intents (
                tenant_ref, funnel_ref, landing_ref, product_ref, offer_ref,
                normalized_email, normalized_phone, submitted_at, lifecycle_state,
                whatsapp_contact_authorized, provisional, provider_observed,
                activation_authorized
            ) values (
                v_offer.tenant_ref, v_offer.funnel_ref, v_offer.landing_ref,
                v_offer.product_ref, v_offer.offer_code, null,
                v_identity.external_user_id, p_now, 'waiting_for_purchase',
                true, false, false, true
            ) returning * into v_intent;
        end if;
    end if;

    -- El sck del anuncio y el fbclid, de la ficha de la intencion si la tiene:
    -- la misma composicion que la reserva entrante (20260927000200 con el
    -- marcador de 20261005000100).
    select submission.* into v_submission
    from public.purchase_intent_submissions link
    join public.precheckout_submissions submission on submission.id = link.submission_id
    where link.purchase_intent_id = v_intent.id
    order by link.ordinal desc, submission.id desc
    limit 1;

    if found then
        v_original_sck := nullif(
            btrim(v_submission.raw_payload #>> '{data,attribution,sck}'), ''
        );
        v_lead_fbclid := nullif(
            btrim(v_submission.raw_payload #>> '{data,attribution,fbclid}'), ''
        );
    end if;

    if v_original_sck is not null
       and length(v_original_sck) <= 255
       and v_original_sck ~ '^[A-Za-z0-9._|~-]+$' then
        v_preserved_sck := v_original_sck;
    end if;

    if v_lead_fbclid is not null and (
           length(v_lead_fbclid) > 512
           or v_lead_fbclid !~ '^[A-Za-z0-9._-]+$'
       ) then
        v_lead_fbclid := null;
        v_dropped_unsafe := 'fbclid';
    end if;

    if v_original_sck is not null and v_preserved_sck is null then
        v_dropped_unsafe := case
            when v_dropped_unsafe = 'fbclid' then 'sck,fbclid'
            else 'sck'
        end;
    end if;

    if v_preserved_sck is not null then
        v_sck_value := v_preserved_sck || '~hermes~v1~' || p_issuance_ulid;
    else
        v_sck_value := 'hermes~v1~' || p_issuance_ulid;
    end if;

    v_attribution_resolution := case
        when v_preserved_sck is not null and v_lead_fbclid is not null then 'full'
        when v_preserved_sck is not null then 'sck_only'
        when v_lead_fbclid is not null then 'fbclid_only'
        else 'marker_only'
    end;

    v_url := v_offer.checkout_base_url || '?off=' || v_offer.offer_code
        || '&checkoutMode=' || v_offer.checkout_mode::text
        || '&src=hermes&sck=' || replace(v_sck_value, '|', '%7C');
    if v_lead_fbclid is not null then
        v_url := v_url || '&fbclid=' || v_lead_fbclid;
    end if;

    insert into public.checkout_link_issuances (
        issuance_ulid, commercial_case_id, purchase_intent_id,
        contact_id, channel_identity_id, offer_catalog_id,
        chatwoot_account_id, chatwoot_inbox_id, chatwoot_conversation_id,
        trigger_external_message_id, source_kind, source_submission_id,
        original_sck, source_value, sck_format_version, sck_value,
        checkout_url_final, offer_resolution, lead_offer_code,
        attribution_resolution, dropped_unsafe_fields,
        recovery_case_id, delivery_attempt_id
    ) values (
        p_issuance_ulid, v_case.commercial_case_id, v_intent.id,
        v_case.contact_id, v_identity.id, v_offer.id,
        v_account_id, v_inbox_id, null,
        null, 'recovery_first_touch', v_submission.id,
        v_original_sck, 'hermes', 'v1', v_sck_value,
        v_url, v_offer_resolution, nullif(btrim(v_case.offer_code), ''),
        v_attribution_resolution, v_dropped_unsafe,
        v_case.id, p_attempt_id
    )
    returning * into v_issuance;

    return query select 'reserved'::text,
        v_issuance.id, v_issuance.checkout_url_final, v_issuance.sck_value;
end;
$function$;

-- 5. La autorizacion del envio de A1: reserved -> request_started.
create function public.authorize_recovery_checkout_issuance_v1(
    p_issuance_id uuid,
    p_action_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_now timestamptz
)
returns table (outcome text, issuance_id uuid, status text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
#variable_conflict use_column
declare
    v_action public.scheduled_actions%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_issuance public.checkout_link_issuances%rowtype;
    v_case public.recovery_cases%rowtype;
    v_contact public.contacts%rowtype;
    v_identity public.channel_identities%rowtype;
    v_intent public.purchase_intents%rowtype;
begin
    -- recovery_checkout_issuance: validar la entrada
    if p_issuance_id is null
       or p_action_id is null
       or p_worker_id is null or btrim(p_worker_id) = ''
       or p_lease_generation is null or p_lease_generation < 1
       or p_now is null then
        raise exception using errcode = '22023',
            message = 'invalid_recovery_checkout_issuance_input';
    end if;

    select sa.* into v_action
    from public.scheduled_actions sa
    where sa.id = p_action_id;
    if not found or not (
        v_action.lease_owner = p_worker_id
        and v_action.lease_generation = p_lease_generation
        and v_action.lease_expires_at > p_now
        and v_action.expires_at > p_now
        and v_action.status in ('pending', 'deferred', 'retryable_failed')
    ) then
        raise exception using errcode = '55000', message = 'current_action_lease_not_found';
    end if;

    select issuance.* into v_issuance
    from public.checkout_link_issuances issuance
    where issuance.id = p_issuance_id
    for update;
    if not found
       or v_issuance.source_kind <> 'recovery_first_touch'
       or v_issuance.recovery_case_id is distinct from v_action.recovery_case_id then
        return query select 'invalid_request'::text, null::uuid, null::text;
        return;
    end if;

    select attempt.* into v_attempt
    from public.followup_delivery_attempts attempt
    where attempt.id = v_issuance.delivery_attempt_id;
    if not found or v_attempt.action_id <> p_action_id then
        return query select 'invalid_request'::text, null::uuid, null::text;
        return;
    end if;

    if v_issuance.status <> 'reserved' then
        return query select
            case
                when v_issuance.status = 'request_started' then 'request_started_replay'
                when v_issuance.status = 'accepted_by_chatwoot' then 'already_accepted'
                when v_issuance.status = 'delivery_unknown' then 'delivery_unknown'
                else 'purchase_already_approved'
            end,
            v_issuance.id, v_issuance.status;
        return;
    end if;

    if v_attempt.lease_generation <> p_lease_generation
       or v_attempt.phase <> 'reserved' then
        return query select 'blocked_attempt'::text, v_issuance.id, v_issuance.status;
        return;
    end if;

    select rc.* into v_case
    from public.recovery_cases rc
    where rc.id = v_issuance.recovery_case_id;
    if not found
       or v_case.status not in ('grace_period', 'active')
       or v_case.purchase_event_id is not null then
        return query select 'blocked_case'::text, v_issuance.id, v_issuance.status;
        return;
    end if;

    select contact.* into v_contact
    from public.contacts contact
    where contact.id = v_issuance.contact_id;
    if not found
       or v_contact.contact_permission in ('opted_out', 'blocked', 'restricted')
       or v_contact.lifecycle_status = 'do_not_contact' then
        return query select 'blocked_contact'::text, v_issuance.id, v_issuance.status;
        return;
    end if;

    select identity.* into v_identity
    from public.channel_identities identity
    where identity.id = v_issuance.channel_identity_id
      and identity.id = v_case.selected_channel_identity_id
      and identity.identity_status = 'active'
      and identity.account_id = 'chatwoot:' || v_issuance.chatwoot_account_id::text
      and identity.metadata ->> 'inbox_id' = v_issuance.chatwoot_inbox_id::text;
    if not found then
        return query select 'blocked_identity'::text, v_issuance.id, v_issuance.status;
        return;
    end if;

    -- La baja de Chatwoot de la cuenta, en cualquiera de las formas del
    -- telefono (el freno de mark_lancemos_pilot_request_started).
    if public._portable_chatwoot_opt_out_stop(
        v_issuance.chatwoot_account_id,
        v_issuance.contact_id,
        v_identity.external_user_id
    ) then
        return query select 'blocked_opt_out'::text, v_issuance.id, v_issuance.status;
        return;
    end if;

    select intent.* into v_intent
    from public.purchase_intents intent
    where intent.id = v_issuance.purchase_intent_id
    for update;
    if found and v_intent.lifecycle_state = 'purchased' then
        return query select 'purchase_already_approved'::text,
            v_issuance.id, v_issuance.status;
        return;
    end if;
    if not found or v_intent.lifecycle_state <> 'waiting_for_purchase' then
        return query select 'blocked_intent'::text, v_issuance.id, v_issuance.status;
        return;
    end if;

    update public.checkout_link_issuances issuance
    set status = 'request_started'
    where issuance.id = v_issuance.id
      and issuance.status = 'reserved'
    returning issuance.* into strict v_issuance;

    return query select 'request_started'::text, v_issuance.id, v_issuance.status;
end;
$function$;

-- 6. La compra con el link de A1 cierra el caso y canjea el cupon.
create function public._close_recovery_case_for_checkout_purchase_v1(
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
    v_outcome text := 'case_not_open';
begin
    -- recovery_checkout_issuance: cerrar el caso del carrito
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
       and v_case.status in ('grace_period', 'active', 'paused')
       and v_case.purchase_event_id is null
       and not exists (
           select 1 from public.recovery_cases other
           where other.purchase_event_id = p_webhook_event_id
       ) then
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
                'purchase_checkout_issuance_id', v_issuance.id
            ),
            version = rc.version + 1
        where rc.id = v_case.id
          and rc.status in ('grace_period', 'active', 'paused')
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
                'checkout_issuance_id', v_issuance.id
            )
        );
        v_outcome := 'won';
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

-- El correlador llama al helper justo despues de marcar la emision. Sobre la
-- definicion viva (20260925000100 con el reconocedor de 20261005000100).
do $correlate$
declare
    v_signature constant text :=
        'public.correlate_hotmart_checkout_issuance_v2(uuid,text,timestamptz)';
    v_old constant text :=
        E'    update public.checkout_link_issuances\n'
        || E'    set status = ''purchase_matched'',\n'
        || E'        hotmart_webhook_event_id = p_hotmart_webhook_event_id,\n'
        || E'        purchased_at = p_now\n'
        || E'    where id = v_issuance.id;\n';
    v_new constant text :=
        v_old
        || E'\n'
        || E'    -- recovery_checkout_issuance: begin\n'
        || E'    -- El link del primer mensaje del carrito cierra su caso: ganado, sin\n'
        || E'    -- los toques que faltan, y el cupon personal canjeado.\n'
        || E'    perform public._close_recovery_case_for_checkout_purchase_v1(\n'
        || E'        v_issuance.id, p_hotmart_webhook_event_id, p_now\n'
        || E'    );\n'
        || E'    -- recovery_checkout_issuance: end\n';
    v_definition text;
begin
    if to_regprocedure(v_signature) is null then
        raise exception using errcode = '55000',
            message = 'recovery_checkout_issuance_function_missing',
            detail = v_signature;
    end if;
    v_definition := pg_get_functiondef(v_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, v_old, ''))
           <> length(v_old)
       or position('_close_recovery_case_for_checkout_purchase_v1' in v_definition) > 0 then
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

-- 7. Permisos. create or replace conserva los grants de las reemplazadas; las
--    nuevas: las dos RPC solo service_role, el helper nadie (lo llama el
--    correlador security definer).
revoke all on function public.protect_checkout_link_issuance() from public;
revoke all on function public.reserve_recovery_checkout_issuance_v1(uuid, text, bigint, uuid, text, timestamptz) from public;
revoke all on function public.authorize_recovery_checkout_issuance_v1(uuid, uuid, text, bigint, timestamptz) from public;
revoke all on function public._close_recovery_case_for_checkout_purchase_v1(uuid, uuid, timestamptz) from public;

do $roles$
declare
    v_role text;
begin
    for v_role in
        select rolname from pg_catalog.pg_roles
        where rolname in ('anon', 'authenticated', 'service_role')
    loop
        execute format('revoke all on function public.protect_checkout_link_issuance() from %I', v_role);
        execute format('revoke all on function public.reserve_recovery_checkout_issuance_v1(uuid, text, bigint, uuid, text, timestamptz) from %I', v_role);
        execute format('revoke all on function public.authorize_recovery_checkout_issuance_v1(uuid, uuid, text, bigint, timestamptz) from %I', v_role);
        execute format('revoke all on function public._close_recovery_case_for_checkout_purchase_v1(uuid, uuid, timestamptz) from %I', v_role);
    end loop;
    if exists (select 1 from pg_catalog.pg_roles where rolname = 'service_role') then
        execute 'grant execute on function public.reserve_recovery_checkout_issuance_v1(uuid, text, bigint, uuid, text, timestamptz) to service_role';
        execute 'grant execute on function public.authorize_recovery_checkout_issuance_v1(uuid, uuid, text, bigint, timestamptz) to service_role';
    end if;
end
$roles$;

commit;
