-- Equivalencia de telefonos de WhatsApp entre fuentes, para el runtime
-- portable (decisiones D12, D15, D16 y D20).
--
-- El mismo movil llega con dos formas. El formulario (el adaptador de GHL y
-- /webhooks/lead) guarda el numero mexicano como 52 + 10 digitos; Hotmart y el
-- wa_id de WhatsApp lo traen como 521 + 10. En Argentina pasa lo mismo con 54 y
-- 549. Toda comparacion era exacta, asi que el lead que dejo el formulario se
-- perdia antes de llegar al permiso: el correlador encontraba la intencion por
-- email y no por telefono, daba conflict / email_phone_conflict, la intencion
-- quedaba en identity_conflict y el pago fallido moria en
-- payment_failure_correlation_unresolved. La compra portable tenia el mismo
-- defecto: no marcaba purchased y la intencion de quien ya compro seguia viva.
--
-- Se compara en forma canonica; nada se reescribe al guardar. Lo guardado se
-- sigue validando contra el payload crudo de cada fuente
-- (_admit_hotmart_purchase_intent_identity, la admision del formulario, el
-- trigger del carrito), y esas funciones no se tocan. Esta migracion:
--
-- 1. suma _whatsapp_phone_canonical(text): deja solo los digitos y reescribe
--    nada mas que 521 + 10 digitos a 52 + 10 y 549 + 10 a 54 + 10. La regla va
--    anclada por largo (13 digitos): un nacional de 10 digitos que empieza con
--    1 o con 9 (12 digitos en total) queda igual a si mismo. Brasil (el noveno
--    digito) queda afuera a proposito: no hay medicion;
-- 2. suma _whatsapp_phone_variants(text): las formas que comparten canonica
--    (una o dos), para buscar con = any(...) sobre una columna de solo digitos
--    sin envolverla en una funcion;
-- 3. suma _correlate_portable_hotmart_purchase_intent(uuid), derivada de
--    correlate_hotmart_purchase_intent con pg_get_functiondef + replace (el
--    precedente de 20260903000300: la definicion vigente del correlador no
--    esta en ningun archivo). Cambian el nombre y las dos comparaciones del
--    telefono, con sus ocurrencias exactas; si la definicion no es la
--    esperada la migracion falla con 55000 y no deja nada. Escribe en la misma
--    hotmart_purchase_intent_correlations, asi que una llamada posterior al
--    correlador compartido devuelve esa fila. En un match por la otra forma
--    reason_code sigue diciendo exact_phone o exact_email_and_phone;
-- 4. copia de su definicion vigente (20260928000200) las tres admisiones
--    portables de Hotmart: carrito y pago fallido pasan a llamar al correlador
--    portable; la compra aprobada compara el telefono por sus formas en las
--    dos consultas de candidatos que lo miran;
-- 5. copia de su definicion vigente (20260930000100)
--    _portable_consented_intent_reason, con la comparacion canonica del
--    telefono de destino y del contact_point, y dos chequeos nuevos entre
--    comentarios whatsapp_phone_equivalence:
--    - consented_intent_contact_phone_mismatch: contacts.phone, que es adonde
--      sale el envio, tiene que ser canonicamente el telefono de la intencion;
--    - consented_intent_prior_opt_out: no hay un opt-out de Chatwoot de la
--      cuenta del binding en ninguna de las dos formas del telefono, en los
--      estados que frena mark_followup_request_started.
--    La comparacion del envio del formulario con su propia intencion queda
--    exacta: son la misma fuente;
-- 6. copia de su definicion vigente (20260930000300)
--    plan_portable_payment_failure_recovery, con la comparacion canonica del
--    destino y del contact_point, y phone_match (exact o whatsapp_equivalent)
--    en la evidencia del permiso que concede.
--
-- El helper del punto 5 lo usan los dos planificadores del piloto y la
-- autorizacion del envio a traves de _lancemos_pilot_audience_intent, que no
-- se redefine: en los modos con consentimiento los dos chequeos nuevos corren
-- al planificar y al arrancar, para carrito y para pago fallido.
--
-- Johanna no cambia. No se redefine nada de lo que ejecuta:
-- correlate_hotmart_purchase_intent, _admit_hotmart_purchase_intent_identity,
-- sus admisiones, el opt-out de Chatwoot, mark_followup_request_started ni
-- reevaluate_followup_action. Lo reemplazado son las admit_portable_hotmart_*
-- (detras de los flags PORTABLE_HOTMART_*) y dos funciones del piloto
-- (LANCEMOS_PILOT_BOUNDARY_ENABLED=false en Johanna).
--
-- Locks: solo crea y reemplaza funciones. No toca tablas ni filas, y no
-- siembra nada de ningun cliente.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

create or replace function public._whatsapp_phone_canonical(
    p_phone text
)
returns text
language sql
immutable
strict
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case
        when normalized.digits ~ '^521[0-9]{10}$'
            then '52' || substr(normalized.digits, 4)
        when normalized.digits ~ '^549[0-9]{10}$'
            then '54' || substr(normalized.digits, 4)
        else nullif(normalized.digits, '')
    end
    from (
        select regexp_replace(p_phone, '[^0-9]', '', 'g') as digits
    ) normalized
$function$;

create or replace function public._whatsapp_phone_variants(
    p_phone text
)
returns text[]
language sql
immutable
strict
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case
        when canonical.phone is null then null
        when canonical.phone ~ '^52[0-9]{10}$'
            then array[canonical.phone, '521' || substr(canonical.phone, 3)]
        when canonical.phone ~ '^54[0-9]{10}$'
            then array[canonical.phone, '549' || substr(canonical.phone, 3)]
        else array[canonical.phone]
    end
    from (
        select public._whatsapp_phone_canonical(p_phone) as phone
    ) canonical
$function$;

-- El correlador portable: la definicion viva del compartido con otro nombre y
-- el telefono comparado por sus formas. Se exige la cantidad exacta de
-- ocurrencias de cada texto: si alguien cambio el correlador compartido, esto
-- falla y hay que mirarlo, en vez de derivar una copia a ciegas.
do $migration$
declare
    v_definition text;
    v_shared_name constant text := 'correlate_hotmart_purchase_intent';
    v_shared_head constant text := 'public.correlate_hotmart_purchase_intent(';
    v_portable_head constant text :=
        'public._correlate_portable_hotmart_purchase_intent(';
    v_exact_phone constant text := 'intent.normalized_phone = v_phone';
    v_equivalent_phone constant text :=
        'intent.normalized_phone = any(public._whatsapp_phone_variants(v_phone))';
begin
    select pg_get_functiondef(
        'public.correlate_hotmart_purchase_intent(uuid)'::regprocedure
    ) into v_definition;
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_shared_name, ''))
          <> length(v_shared_name)
       or length(v_definition) - length(replace(v_definition, v_shared_head, ''))
          <> length(v_shared_head)
       or length(v_definition) - length(replace(v_definition, v_exact_phone, ''))
          <> 2 * length(v_exact_phone)
       or position('_whatsapp_phone_' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_hotmart_intent_correlator_definition';
    end if;
    execute replace(
        replace(v_definition, v_shared_head, v_portable_head),
        v_exact_phone,
        v_equivalent_phone
    );
    if to_regprocedure(
        'public._correlate_portable_hotmart_purchase_intent(uuid)'
    ) is null then
        raise exception using errcode = '55000',
            message = 'portable_hotmart_intent_correlator_not_created';
    end if;
end;
$migration$;

create or replace function public.admit_portable_hotmart_cart_abandonment(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
    p_external_event_id text,
    p_payload jsonb,
    p_normalized_email text,
    p_normalized_phone text
)
returns table (
    outcome text,
    webhook_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_scope public.hotmart_purchase_intent_scopes%rowtype;
    v_provenance public.commercial_ally_hotmart_event_bindings%rowtype;
    v_admission_outcome text;
    v_event_id uuid;
    v_correlation_outcome text;
begin
    if p_tenant_ref is null or nullif(btrim(p_tenant_ref), '') is null
       or p_funnel_ref is null or nullif(btrim(p_funnel_ref), '') is null
       or p_binding_version is null or p_binding_version < 1
       or p_external_event_id is null or nullif(btrim(p_external_event_id), '') is null
       or p_payload is null or jsonb_typeof(p_payload) <> 'object' then
        raise exception using errcode = '22023',
            message = 'invalid_portable_hotmart_cart_input';
    end if;

    select b.* into v_binding
    from public.commercial_ally_runtime_bindings b
    where b.tenant_ref = p_tenant_ref
      and b.funnel_ref = p_funnel_ref
      and b.binding_version = p_binding_version
      and b.status = 'active'
    for update;
    if not found then
        raise exception using errcode = '22023',
            message = 'commercial_ally_binding_unavailable';
    end if;

    if p_payload #>> '{id}' is distinct from p_external_event_id
       or p_payload #>> '{event}' is distinct from 'PURCHASE_OUT_OF_SHOPPING_CART'
       or p_payload #>> '{version}' is distinct from '2.0.0'
       or jsonb_typeof(p_payload #> '{data,product,id}') is distinct from 'number'
       or (p_payload #>> '{data,product,id}')::numeric
            is distinct from v_binding.hotmart_product_id::numeric
       or not coalesce((p_payload #>> '{data,offer,code}' = v_binding.offer_code
            or p_payload #>> '{data,offer,code}' = any(v_binding.additional_offer_codes)), false) then
        raise exception using errcode = '22023',
            message = 'portable_hotmart_cart_binding_mismatch';
    end if;

    select scope.* into v_scope
    from public.hotmart_purchase_intent_scopes scope
    where scope.tenant_ref = v_binding.tenant_ref
      and scope.funnel_ref = v_binding.funnel_ref
      and scope.hotmart_product_id = v_binding.hotmart_product_id::text
      and scope.purchase_intent_product_ref = v_binding.product_hotlink
      and scope.offer_ref = p_payload #>> '{data,offer,code}'
      and scope.active
    for update;
    if not found then
        raise exception using errcode = '22023',
            message = 'portable_hotmart_cart_scope_unavailable';
    end if;

    select admission.outcome, admission.webhook_event_id
    into strict v_admission_outcome, v_event_id
    from public._admit_hotmart_cart_abandonment_base(
        p_external_event_id,
        p_payload
    ) admission;

    if v_admission_outcome = 'inserted' then
        insert into public.commercial_ally_hotmart_event_bindings (
            webhook_event_id,
            scope_id,
            tenant_ref,
            funnel_ref,
            binding_version,
            hotmart_product_id,
            purchase_intent_product_ref,
            offer_ref
        ) values (
            v_event_id,
            v_scope.id,
            v_binding.tenant_ref,
            v_binding.funnel_ref,
            v_binding.binding_version,
            v_scope.hotmart_product_id,
            v_scope.purchase_intent_product_ref,
            v_scope.offer_ref
        );
    end if;

    select provenance.* into v_provenance
    from public.commercial_ally_hotmart_event_bindings provenance
    where provenance.webhook_event_id = v_event_id;
    if not found
       or v_provenance.scope_id is distinct from v_scope.id
       or v_provenance.tenant_ref is distinct from v_binding.tenant_ref
       or v_provenance.funnel_ref is distinct from v_binding.funnel_ref
       or v_provenance.binding_version is distinct from v_binding.binding_version
       or v_provenance.hotmart_product_id is distinct from v_scope.hotmart_product_id
       or v_provenance.purchase_intent_product_ref
            is distinct from v_scope.purchase_intent_product_ref
       or v_provenance.offer_ref is distinct from v_scope.offer_ref then
        raise exception using errcode = '22023',
            message = 'portable_hotmart_cart_replay_binding_mismatch';
    end if;

    if v_admission_outcome <> 'semantic_conflict' then
        perform public._admit_hotmart_purchase_intent_identity(
            v_event_id,
            p_normalized_email,
            p_normalized_phone
        );
        select correlation.outcome into strict v_correlation_outcome
        from public._correlate_portable_hotmart_purchase_intent(v_event_id) correlation;
    end if;

    return query select v_admission_outcome, v_event_id;
end;
$function$;

create or replace function public.admit_portable_hotmart_purchase_approved(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
    p_external_event_id text,
    p_payload jsonb,
    p_normalized_email text,
    p_normalized_phone text
)
returns table (
    outcome text,
    webhook_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_policy public.commercial_ally_hotmart_purchase_policies%rowtype;
    v_existing public.portable_hotmart_purchase_correlations%rowtype;
    v_admission_outcome text;
    v_event_id uuid;
    v_email text;
    v_phone text;
    v_approved_at timestamptz;
    v_email_ids uuid[] := array[]::uuid[];
    v_phone_ids uuid[] := array[]::uuid[];
    v_candidate_ids uuid[] := array[]::uuid[];
    v_resolved_intent_id uuid;
    v_correlation_outcome text;
    v_matched_by text;
    v_reason_code text;
    v_candidate_count integer := 0;
begin
    if p_tenant_ref is null or nullif(btrim(p_tenant_ref), '') is null
       or p_funnel_ref is null or nullif(btrim(p_funnel_ref), '') is null
       or p_binding_version is null or p_binding_version < 1
       or p_external_event_id is null or nullif(btrim(p_external_event_id), '') is null
       or p_payload is null or jsonb_typeof(p_payload) <> 'object' then
        raise exception using errcode = '22023', message = 'invalid_portable_hotmart_purchase_input';
    end if;

    select binding.* into v_binding
    from public.commercial_ally_runtime_bindings binding
    where binding.tenant_ref = p_tenant_ref
      and binding.funnel_ref = p_funnel_ref
      and binding.binding_version = p_binding_version
      and binding.status = 'active'
    for update;
    if not found then
        raise exception using errcode = '22023', message = 'commercial_ally_binding_unavailable';
    end if;

    if p_payload #>> '{id}' is distinct from p_external_event_id
       or p_payload #>> '{event}' is distinct from 'PURCHASE_APPROVED'
       or p_payload #>> '{version}' is distinct from '2.0.0'
       or p_payload #>> '{data,purchase,status}' is distinct from 'APPROVED'
       or jsonb_typeof(p_payload #> '{data,product,id}') is distinct from 'number'
       or (p_payload #>> '{data,product,id}')::numeric
            is distinct from v_binding.hotmart_product_id::numeric
       or nullif(btrim(p_payload #>> '{data,purchase,offer,code}'), '') is null then
        raise exception using errcode = '22023', message = 'portable_hotmart_purchase_binding_mismatch';
    end if;

    select policy.* into v_policy
    from public.commercial_ally_hotmart_purchase_policies policy
    where policy.tenant_ref = v_binding.tenant_ref
      and policy.funnel_ref = v_binding.funnel_ref
      and policy.binding_version = v_binding.binding_version
      and policy.enabled
    for update;
    if not found then
        raise exception using errcode = '22023', message = 'portable_hotmart_purchase_policy_unavailable';
    end if;

    begin
        v_approved_at := to_timestamp(
            (p_payload #>> '{data,purchase,approved_date}')::numeric / 1000
        );
    exception when others then
        raise exception using errcode = '22023', message = 'portable_hotmart_purchase_invalid_approved_date';
    end;
    if v_approved_at is null then
        raise exception using errcode = '22023', message = 'portable_hotmart_purchase_invalid_approved_date';
    end if;

    select admission.outcome, admission.webhook_event_id
    into strict v_admission_outcome, v_event_id
    from public._admit_hotmart_purchase_approved_base(
        p_external_event_id, p_payload
    ) admission;

    if v_admission_outcome = 'semantic_conflict' then
        return query select v_admission_outcome, v_event_id;
        return;
    end if;

    select correlation.* into v_existing
    from public.portable_hotmart_purchase_correlations correlation
    where correlation.webhook_event_id = v_event_id;
    if found then
        if v_existing.tenant_ref is distinct from v_binding.tenant_ref
           or v_existing.funnel_ref is distinct from v_binding.funnel_ref
           or v_existing.binding_version is distinct from v_binding.binding_version then
            raise exception using errcode = '23514', message = 'portable_hotmart_purchase_replay_binding_conflict';
        end if;
        return query select v_admission_outcome, v_event_id;
        return;
    end if;
    if v_admission_outcome = 'duplicate' then
        raise exception using errcode = '23514', message = 'portable_hotmart_purchase_preexisting_admission';
    end if;

    perform public._admit_hotmart_purchase_intent_identity(
        v_event_id, p_normalized_email, p_normalized_phone
    );
    select identity.normalized_email, identity.normalized_phone
    into strict v_email, v_phone
    from public.hotmart_purchase_intent_event_identities identity
    where identity.webhook_event_id = v_event_id;

    perform intent.id
    from public.purchase_intents intent
    where intent.tenant_ref = v_binding.tenant_ref
      and intent.funnel_ref = v_binding.funnel_ref
      and intent.product_ref = v_binding.product_hotlink
      and intent.lifecycle_state = 'waiting_for_purchase'
      and intent.provider_observed
      and not intent.provisional
      and intent.submitted_at >= v_approved_at - v_policy.max_lookback
      and intent.submitted_at <= v_approved_at
      and ((v_email is not null and intent.normalized_email = v_email)
        or (v_phone is not null and intent.normalized_phone = any(public._whatsapp_phone_variants(v_phone))))
    order by intent.id
    for update;

    select coalesce(array_agg(intent.id order by intent.id), array[]::uuid[])
    into v_email_ids
    from public.purchase_intents intent
    where intent.tenant_ref = v_binding.tenant_ref
      and intent.funnel_ref = v_binding.funnel_ref
      and intent.product_ref = v_binding.product_hotlink
      and intent.lifecycle_state = 'waiting_for_purchase'
      and intent.provider_observed and not intent.provisional
      and intent.submitted_at >= v_approved_at - v_policy.max_lookback
      and intent.submitted_at <= v_approved_at
      and v_email is not null and intent.normalized_email = v_email;

    select coalesce(array_agg(intent.id order by intent.id), array[]::uuid[])
    into v_phone_ids
    from public.purchase_intents intent
    where intent.tenant_ref = v_binding.tenant_ref
      and intent.funnel_ref = v_binding.funnel_ref
      and intent.product_ref = v_binding.product_hotlink
      and intent.lifecycle_state = 'waiting_for_purchase'
      and intent.provider_observed and not intent.provisional
      and intent.submitted_at >= v_approved_at - v_policy.max_lookback
      and intent.submitted_at <= v_approved_at
      and v_phone is not null and intent.normalized_phone = any(public._whatsapp_phone_variants(v_phone));

    select coalesce(array_agg(candidate_id order by candidate_id), array[]::uuid[])
    into v_candidate_ids
    from (
        select unnest(v_email_ids) as candidate_id
        union
        select unnest(v_phone_ids) as candidate_id
    ) candidates;
    v_candidate_count := cardinality(v_candidate_ids);

    if v_email is not null and v_phone is not null then
        if cardinality(v_email_ids) = 0 and cardinality(v_phone_ids) = 0 then
            v_correlation_outcome := 'unmatched';
            v_reason_code := 'identity_not_found';
        elsif cardinality(v_email_ids) = 1 and cardinality(v_phone_ids) = 1
          and v_email_ids[1] = v_phone_ids[1] then
            v_correlation_outcome := 'resolved';
            v_resolved_intent_id := v_email_ids[1];
            v_matched_by := 'email_and_phone';
            v_reason_code := 'exact_email_and_phone';
            v_candidate_count := 1;
        elsif cardinality(v_email_ids) = 0 or cardinality(v_phone_ids) = 0
          or not exists (
              select 1 from unnest(v_email_ids) email_id
              join unnest(v_phone_ids) phone_id on phone_id = email_id
          ) then
            v_correlation_outcome := 'conflict';
            v_reason_code := 'email_phone_conflict';
        else
            v_correlation_outcome := 'ambiguous';
            v_reason_code := 'multiple_candidates';
        end if;
    elsif v_email is not null then
        if cardinality(v_email_ids) = 0 then
            v_correlation_outcome := 'unmatched';
            v_reason_code := 'identity_not_found';
        elsif cardinality(v_email_ids) = 1 then
            v_correlation_outcome := 'resolved';
            v_resolved_intent_id := v_email_ids[1];
            v_matched_by := 'email';
            v_reason_code := 'exact_email';
            v_candidate_count := 1;
        else
            v_correlation_outcome := 'ambiguous';
            v_reason_code := 'multiple_candidates';
        end if;
    else
        if cardinality(v_phone_ids) = 0 then
            v_correlation_outcome := 'unmatched';
            v_reason_code := 'identity_not_found';
        elsif cardinality(v_phone_ids) = 1 then
            v_correlation_outcome := 'resolved';
            v_resolved_intent_id := v_phone_ids[1];
            v_matched_by := 'phone';
            v_reason_code := 'exact_phone';
            v_candidate_count := 1;
        else
            v_correlation_outcome := 'ambiguous';
            v_reason_code := 'multiple_candidates';
        end if;
    end if;

    insert into public.portable_hotmart_purchase_correlations (
        webhook_event_id, tenant_ref, funnel_ref, binding_version,
        policy_max_lookback, outcome, purchase_intent_id, matched_by,
        candidate_count, reason_code, observed_at
    ) values (
        v_event_id, v_binding.tenant_ref, v_binding.funnel_ref,
        v_binding.binding_version, v_policy.max_lookback,
        v_correlation_outcome, v_resolved_intent_id, v_matched_by,
        v_candidate_count, v_reason_code, v_approved_at
    );

    if cardinality(v_candidate_ids) > 0 then
        insert into public.portable_hotmart_purchase_correlation_candidates (
            webhook_event_id, purchase_intent_id, email_match, phone_match
        )
        select v_event_id, candidate_id,
               candidate_id = any(v_email_ids), candidate_id = any(v_phone_ids)
        from unnest(v_candidate_ids) candidate_id;
    end if;

    if v_correlation_outcome = 'resolved' then
        update public.purchase_intents
        set lifecycle_state = 'purchased',
            current_classification = null,
            activation_authorized = false,
            updated_at = clock_timestamp()
        where id = v_resolved_intent_id
          and lifecycle_state = 'waiting_for_purchase';
        if not found then
            raise exception using errcode = '40001', message = 'purchase_intent_changed_concurrently';
        end if;
        perform public.cancel_hotmart_abandonment_reevaluations_for_purchase(
            v_resolved_intent_id, clock_timestamp()
        );
    end if;

    return query select v_admission_outcome, v_event_id;
end;
$function$;

create or replace function public.admit_portable_hotmart_payment_failure(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
    p_external_event_id text,
    p_payload jsonb,
    p_normalized_email text,
    p_normalized_phone text
) returns table (outcome text, webhook_event_id uuid)
language plpgsql security definer
set search_path = pg_catalog, public, pg_temp as $function$
declare
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_scope public.hotmart_purchase_intent_scopes%rowtype;
    v_existing public.webhook_events%rowtype;
    v_event_id uuid;
    v_admission_outcome text;
    v_correlation record;
    v_payload_email text;
    v_payload_phone text;
begin
    if not public.hotmart_payment_failure_payload_is_processable(p_external_event_id, p_payload)
       or p_binding_version is null or p_binding_version < 1 then
        raise exception using errcode = '22023', message = 'invalid_portable_hotmart_payment_failure_input';
    end if;
    select b.* into v_binding from public.commercial_ally_runtime_bindings b
    where b.tenant_ref = p_tenant_ref and b.funnel_ref = p_funnel_ref
      and b.binding_version = p_binding_version and b.status = 'active' for update;
    if not found then raise exception using errcode='22023', message='commercial_ally_binding_unavailable'; end if;
    if (p_payload #>> '{data,product,id}')::numeric is distinct from v_binding.hotmart_product_id::numeric
       or p_payload #>> '{data,product,name}' is distinct from v_binding.product_name
       or not coalesce((p_payload #>> '{data,purchase,offer,code}' = v_binding.offer_code
            or p_payload #>> '{data,purchase,offer,code}' = any(v_binding.additional_offer_codes)), false) then
        raise exception using errcode='22023', message='portable_hotmart_payment_failure_binding_mismatch';
    end if;
    v_payload_email := lower(nullif(btrim(p_payload #>> '{data,buyer,email}'), ''));
    v_payload_phone := nullif(regexp_replace(coalesce(
        p_payload #>> '{data,buyer,checkout_phone}', p_payload #>> '{data,buyer,phone}', ''
    ), '[^0-9]', '', 'g'), '');
    if p_normalized_email is distinct from v_payload_email
       or p_normalized_phone is distinct from v_payload_phone then
        raise exception using errcode='22023', message='portable_hotmart_payment_failure_identity_mismatch';
    end if;
    select scope.* into v_scope from public.hotmart_purchase_intent_scopes scope
    where scope.tenant_ref=v_binding.tenant_ref and scope.funnel_ref=v_binding.funnel_ref
      and scope.hotmart_product_id=v_binding.hotmart_product_id::text
      and scope.purchase_intent_product_ref=v_binding.product_hotlink
      and scope.offer_ref=p_payload #>> '{data,purchase,offer,code}' and scope.active for update;
    if not found then raise exception using errcode='22023', message='portable_hotmart_payment_failure_scope_unavailable'; end if;
    perform pg_advisory_xact_lock(hashtextextended('payment-failure:' || p_external_event_id, 0));
    select event.* into v_existing from public.webhook_events event
    where event.source='hotmart' and event.external_event_id=p_external_event_id for update;
    if not found then
        insert into public.webhook_events(source,external_event_id,event_type,payload,processing_status)
        values ('hotmart',p_external_event_id,'PURCHASE_CANCELED',p_payload,'received') returning id into v_event_id;
        v_admission_outcome := 'inserted';
        insert into public.commercial_ally_hotmart_event_bindings(
            webhook_event_id,scope_id,tenant_ref,funnel_ref,binding_version,
            hotmart_product_id,purchase_intent_product_ref,offer_ref
        ) values (v_event_id,v_scope.id,v_binding.tenant_ref,v_binding.funnel_ref,
            v_binding.binding_version,v_scope.hotmart_product_id,
            v_scope.purchase_intent_product_ref,v_scope.offer_ref);
        insert into public.commercial_ally_payment_failure_details(
            webhook_event_id,transaction_ref,refusal_reason
        ) values (v_event_id,p_payload #>> '{data,purchase,transaction}',
            nullif(btrim(p_payload #>> '{data,purchase,payment,refusal_reason}'),''));
    elsif v_existing.event_type='PURCHASE_CANCELED' and v_existing.payload=p_payload then
        v_event_id := v_existing.id; v_admission_outcome := 'duplicate';
    else
        insert into public.commercial_ally_payment_failure_conflicts(
            existing_event_id,incoming_external_event_id,incoming_payload
        ) values (v_existing.id,p_external_event_id,p_payload) on conflict do nothing;
        return query select 'semantic_conflict'::text, v_existing.id; return;
    end if;
    if not exists (
        select 1 from public.commercial_ally_hotmart_event_bindings provenance
        where provenance.webhook_event_id=v_event_id and provenance.scope_id=v_scope.id
          and provenance.tenant_ref=v_binding.tenant_ref
          and provenance.funnel_ref=v_binding.funnel_ref
          and provenance.binding_version=v_binding.binding_version
    ) then raise exception using errcode='22023', message='portable_hotmart_payment_failure_replay_binding_mismatch'; end if;
    perform public._admit_hotmart_purchase_intent_identity(v_event_id,p_normalized_email,p_normalized_phone);
    select correlation.* into strict v_correlation from public._correlate_portable_hotmart_purchase_intent(v_event_id) correlation;
    if v_admission_outcome='inserted' then
        perform set_config('app.payment_failure_evidence_finalize', 'on', true);
        update public.commercial_ally_payment_failure_details details set
            correlation_outcome=v_correlation.outcome,
            purchase_intent_id=v_correlation.purchase_intent_id,
            updated_at=clock_timestamp()
        where details.webhook_event_id=v_event_id;
    end if;
    return query select v_admission_outcome,v_event_id;
end;
$function$;

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
       or public._whatsapp_phone_canonical(v_intent.normalized_phone)
           is distinct from public._whatsapp_phone_canonical(p_destination_phone)
       or not exists (
           select 1
           from public.contact_points point
           where point.contact_id = p_contact_id
             and point.type = 'phone'
             and point.normalized_value = any(
                 public._whatsapp_phone_variants(v_intent.normalized_phone)
             )
       ) then
        reason_code := 'consented_intent_phone_mismatch';
        return;
    end if;

    -- whatsapp_phone_equivalence: begin
    -- El envio sale a contacts.phone (get_followup_execution_context), no al
    -- telefono de la intencion ni a la identidad. Un contacto encontrado por
    -- email con otro numero en contacts.phone recibiria el mensaje que
    -- consintio otro telefono. Se compara en forma canonica, que ademas le
    -- saca a contacts.phone todo lo que no sea digito (puede traer '+' o
    -- espacios). Va despues de consented_intent_phone_mismatch para no
    -- cambiar ese motivo.
    if not exists (
        select 1
        from public.contacts contact
        where contact.id = p_contact_id
          and public._whatsapp_phone_canonical(contact.phone)
              = public._whatsapp_phone_canonical(v_intent.normalized_phone)
    ) then
        reason_code := 'consented_intent_contact_phone_mismatch';
        return;
    end if;
    -- whatsapp_phone_equivalence: end

    select binding.* into v_binding
    from public.commercial_ally_runtime_bindings binding
    where binding.tenant_ref = v_intent.tenant_ref
      and binding.funnel_ref = v_intent.funnel_ref
      and binding.status = 'active';
    if not found then
        reason_code := 'consented_intent_binding_unavailable';
        return;
    end if;

    -- whatsapp_phone_equivalence: begin
    -- Opt-out previo en cualquiera de las dos formas del telefono. El opt-out
    -- de Chatwoot se guarda con el wa_id textual (521..., 549...) y, si la
    -- persona escribio antes de tener contacto, queda unmatched: el freno del
    -- arranque (mark_followup_request_started) lo busca por el id exacto de la
    -- identidad y no lo cruza con una intencion guardada sin el 1 o el 9. Se
    -- miran los mismos estados que mira ese freno, en la cuenta de Chatwoot
    -- del binding. Es solo lectura: no toca el opt-out ni lo vuelve a aplicar.
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
        reason_code := 'consented_intent_prior_opt_out';
        return;
    end if;
    -- whatsapp_phone_equivalence: end

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
      and public._whatsapp_phone_canonical(intent.normalized_phone)
          = public._whatsapp_phone_canonical(p_external_user_id);
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
          and point.normalized_value = any(
              public._whatsapp_phone_variants(v_purchase_intent.normalized_phone)
          )
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
                -- whatsapp_phone_equivalence: begin
                -- Con que coincidencia entro el telefono: identico, o la otra
                -- forma del mismo movil (para medir cuantos entran asi).
                , 'phone_match', case
                    when v_purchase_intent.normalized_phone = p_external_user_id
                        then 'exact'
                    else 'whatsapp_equivalent'
                end
                -- whatsapp_phone_equivalence: end
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

-- create or replace conserva los grants de las funciones reemplazadas, pero
-- Supabase le da execute por defecto a toda funcion nueva: los tres helpers
-- nuevos y el helper del consentimiento se revocan a todos (los llaman las RPC
-- security definer), y las cuatro RPC se reafirman como entrypoints solo de
-- service_role.
revoke all on function public._whatsapp_phone_canonical(text) from public;
revoke all on function public._whatsapp_phone_variants(text) from public;
revoke all on function public._correlate_portable_hotmart_purchase_intent(uuid) from public;
revoke all on function public._portable_consented_intent_reason(uuid,uuid,text) from public;
revoke all on function public.admit_portable_hotmart_cart_abandonment(text,text,integer,text,jsonb,text,text) from public;
revoke all on function public.admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text) from public;
revoke all on function public.admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text) from public;
revoke all on function public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) from public;
do $roles$
declare v_role text;
begin
 for v_role in select rolname from pg_roles where rolname in ('anon','authenticated','service_role') loop
  execute format('revoke all on function public._whatsapp_phone_canonical(text) from %I',v_role);
  execute format('revoke all on function public._whatsapp_phone_variants(text) from %I',v_role);
  execute format('revoke all on function public._correlate_portable_hotmart_purchase_intent(uuid) from %I',v_role);
  execute format('revoke all on function public._portable_consented_intent_reason(uuid,uuid,text) from %I',v_role);
  execute format('revoke all on function public.admit_portable_hotmart_cart_abandonment(text,text,integer,text,jsonb,text,text) from %I',v_role);
  execute format('revoke all on function public.admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text) from %I',v_role);
  execute format('revoke all on function public.admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text) from %I',v_role);
  execute format('revoke all on function public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) from %I',v_role);
 end loop;
 if exists(select 1 from pg_roles where rolname='service_role') then
  grant execute on function public.admit_portable_hotmart_cart_abandonment(text,text,integer,text,jsonb,text,text) to service_role;
  grant execute on function public.admit_portable_hotmart_purchase_approved(text,text,integer,text,jsonb,text,text) to service_role;
  grant execute on function public.admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text) to service_role;
  grant execute on function public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer) to service_role;
 end if;
end;
$roles$;

commit;
