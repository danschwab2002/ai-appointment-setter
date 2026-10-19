-- Migration: el primer contacto de la ficha de pago (OXXO y parecidos) en el
-- runtime con manifiesto. C1 de los flujos comerciales de ATT1.
--
-- Por que. Desde 20261012000100 el bridge guarda PURCHASE_BILLET_PRINTED con
-- admit_portable_hotmart_payment_slip_event_v1, sin correlacionar ni
-- planificar: el consumidor era el seguimiento de la ficha sin pagar. Medido
-- el 2026-10-18 en la base de ATT1: 13 fichas desde el 12/10, 6 en efectivo (5
-- de Mexico, OXXO), y ninguna recibio un mensaje. Decision del 2026-10-18 «la
-- ficha de OXXO va con scope propio y codigo global»: la ficha se recupera con
-- su propio scope (source_event_type = PURCHASE_BILLET_PRINTED), su propia
-- politica (una plantilla a la hora) y su propia RPC de arranque, sin tocar
-- una linea del pago fallido. La forma del payload esta capturada en
-- tests/fixtures/hotmart_payment_slip_printed_att1_20261018.json.
--
-- Que crea:
--
-- 1. La tabla commercial_ally_payment_slip_details, calcada de
--    commercial_ally_payment_failure_details (20260903000300): una fila por
--    ficha admitida con la correlacion de la intencion y los hechos crudos de
--    la ficha (medio, pais, billet_url, billet_barcode y la hora en que se
--    genero, de data.purchase.order_date). Inmutable: la unica escritura
--    posterior al insert es la que cierra la correlacion, una vez. La tienda,
--    el vencimiento y el sufijo del boton NO se calculan aca: los calcula el
--    bridge al componer (src/bridge/payment_slip.py), para no tener la regla
--    dos veces.
-- 2. hotmart_payment_slip_payload_is_processable(text, jsonb), la forma minima
--    de una ficha, calcada de hotmart_payment_failure_payload_is_processable.
-- 3. admit_portable_hotmart_payment_slip_v2, escrita a mano sobre
--    admit_portable_hotmart_payment_failure (vigente en 20261001000100) y la v1
--    de la ficha: guarda el evento como la v1 (webhook_events en received y su
--    procedencia en commercial_ally_hotmart_event_bindings), admite la
--    identidad, cruza la intencion con el correlador portable y guarda la fila
--    de detalle. Toda ficha se admite y se cruza, sea del medio que sea: el
--    filtro por medio y pais va en el worker. Como la v1, una ficha de otro
--    producto u otra oferta vuelve ignored_binding_mismatch sin error (Hotmart
--    desactiva el webhook entero si se le acumulan rechazos). La v1 queda
--    igual: sigue guardando PURCHASE_EXPIRED.
-- 4. Tres parches en el lugar, por pg_get_functiondef + replace con la
--    cantidad exacta de ocurrencias, equivalentes en efecto para el carrito,
--    la compra y el pago fallido: la ficha entra en la identidad del evento
--    (_hotmart_purchase_intent_payload_identity, como el pago fallido: primero
--    checkout_phone), en la forma procesable
--    (hotmart_purchase_intent_payload_is_processable) y en el correlador
--    portable (_correlate_portable_hotmart_purchase_intent): la oferta y la
--    hora salen como en el pago fallido (data.purchase.offer.code y
--    creation_date), y una ficha resuelta NO clasifica la intencion. Sin esa
--    rama caeria en el else y dejaria la intencion como confirmed_abandonment,
--    que frena el primer contacto del formulario y la adopcion de la respuesta
--    como si fuera un carrito. El correlador de Johanna
--    (correlate_hotmart_purchase_intent) no se toca: nunca recibe una ficha.
-- 5. Cuatro funciones derivadas de la definicion viva del pago fallido, con el
--    metodo de reserve_portable_checkout_issuance_v2 (20261001000100): cada
--    reemplazo con su cantidad exacta medida, y la migracion aborta con 55000
--    si alguna no coincide, si la funcion derivada ya existe o si queda algun
--    payment_failure o PURCHASE_CANCELED en el texto derivado. Si otra sesion
--    cambia el pago fallido, esto falla a proposito.
--    - plan_payment_slip_recovery, de plan_payment_failure_recovery. Ademas,
--      una segunda ficha de la misma persona, producto y oferta con el caso
--      abierto se agrega al caso y NO adelanta la hora del mensaje: due_at y
--      next_attempt_at quedan como estaban (el pago fallido los adelanta a
--      ahora).
--    - plan_payment_slip_recovery_with_identity, de
--      plan_payment_failure_recovery_with_identity.
--    - plan_portable_payment_slip_recovery, de
--      plan_portable_payment_failure_recovery: lee la fila de detalle de la
--      ficha y evalua el scope con PURCHASE_BILLET_PRINTED.
--    - mark_portable_payment_slip_request_started, de
--      mark_portable_payment_failure_request_started: exige
--      anchor_type = payment_slip y autoriza con PURCHASE_BILLET_PRINTED.
--    Los codigos de error y de contexto cambian con el prefijo
--    (payment_slip_correlation_unresolved, payment_slip_action_required,
--    latest_payment_slip_event_id, payment_slip_aggregated, ...).
-- 6. get_portable_payment_slip_pilot_runtime_status, copia de
--    get_lancemos_pilot_runtime_status para un scope de la ficha.
-- 7. get_portable_payment_slip_send_context: con la lease vigente de una accion
--    payment_slip, los hechos crudos de la ficha mas reciente del caso, para
--    que el bridge componga la plantilla.
--
-- Los checks que se amplian (cada uno acepta todo lo que aceptaba):
-- recovery_case_events.event_role y followup_sequences.reason con
-- payment_slip; pilot_scope_versions.source_event_type y
-- hotmart_purchase_intent_correlations.event_type con PURCHASE_BILLET_PRINTED.
-- scheduled_actions.step_key, scheduled_actions.anchor_type y
-- recovery_cases.context no tienen restricciones. El check de
-- additional_source_event_types no se toca: la ficha es el tipo principal de
-- su scope.
--
-- Johanna no cambia: nada de lo que ejecuta se redefine. El pago fallido no
-- cambia: sus funciones no se reemplazan (validate_portable_payment_slip_
-- recovery.mjs compara el md5 de su definicion con y sin esta migracion).
--
-- Riesgo que queda documentado y no se arregla aca (H3 del diseno):
-- plan_cart_recovery busca cualquier caso Hotmart abierto del contacto, el
-- producto y la oferta sin mirar el disparador, asi que un carrito posterior a
-- una ficha con el caso abierto se mete en el caso de la ficha. Lo fija el
-- validador; arreglarlo es tocar el planificador del carrito.
--
-- Locks: los cuatro alter table toman ACCESS EXCLUSIVE sobre
-- recovery_case_events, followup_sequences, pilot_scope_versions y
-- hotmart_purchase_intent_correlations y validan sus filas; las FK de la tabla
-- nueva toman SHARE ROW EXCLUSIVE sobre webhook_events y purchase_intents
-- hasta el commit. Si una transaccion larga retiene alguna, la migracion falla
-- por lock_timeout (5 s) sin dejar nada a medias y se reintenta. Conviene
-- aplicarla con poco trafico. No siembra filas de ningun cliente.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- ---------------------------------------------------------------------------
-- Los checks.
-- ---------------------------------------------------------------------------
alter table public.recovery_case_events
    drop constraint recovery_case_events_event_role_check;
alter table public.recovery_case_events
    add constraint recovery_case_events_event_role_check
    check (event_role in (
        'cart_abandonment', 'payment_failure', 'precheckout_intent',
        'payment_slip'
    ));

alter table public.followup_sequences
    drop constraint followup_sequences_reason_check;
alter table public.followup_sequences
    add constraint followup_sequences_reason_check
    check (reason = any (array[
        'cart_abandonment', 'payment_failure', 'precheckout_intent',
        'payment_slip', 'no_reply', 'contact_requested', 'prospect_commitment',
        'agent_commitment', 'proposal_pending', 'booking_pending',
        'payment_pending', 'nurture', 'manual', 'recovery'
    ]));

alter table public.pilot_scope_versions
    drop constraint pilot_scope_versions_source_event_type_check;
alter table public.pilot_scope_versions
    add constraint pilot_scope_versions_source_event_type_check
    check (source_event_type in (
        'PURCHASE_OUT_OF_SHOPPING_CART', 'PURCHASE_CANCELED',
        'PRECHECKOUT_FORM_SUBMITTED', 'PURCHASE_BILLET_PRINTED'
    ));

alter table public.hotmart_purchase_intent_correlations
    drop constraint hotmart_purchase_intent_correlations_event_type_check;
alter table public.hotmart_purchase_intent_correlations
    add constraint hotmart_purchase_intent_correlations_event_type_check
    check (event_type in (
        'PURCHASE_APPROVED',
        'PURCHASE_OUT_OF_SHOPPING_CART',
        'PURCHASE_CANCELED',
        'PURCHASE_BILLET_PRINTED'
    ));

-- ---------------------------------------------------------------------------
-- 1. La evidencia de la ficha.
-- ---------------------------------------------------------------------------
create table public.commercial_ally_payment_slip_details (
    webhook_event_id uuid primary key
        references public.webhook_events(id) on delete restrict,
    trigger_kind text not null default 'payment_slip'
        check (trigger_kind = 'payment_slip'),
    transaction_ref text not null,
    payment_type text,
    checkout_country_iso text,
    billet_url text,
    billet_barcode text,
    generated_at timestamptz,
    correlation_outcome text,
    purchase_intent_id uuid
        references public.purchase_intents(id) on delete restrict,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index commercial_ally_payment_slip_details_intent_idx
    on public.commercial_ally_payment_slip_details (purchase_intent_id)
    where purchase_intent_id is not null;

-- Inmutable como la del pago fallido: solo la admision, en la misma
-- transaccion y con la marca de la sesion, cierra la correlacion una vez.
create function public.protect_commercial_ally_payment_slip_evidence()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op = 'UPDATE'
       and current_setting('app.payment_slip_evidence_finalize', true) = 'on'
       and old.webhook_event_id = new.webhook_event_id
       and old.trigger_kind = new.trigger_kind
       and old.transaction_ref = new.transaction_ref
       and old.payment_type is not distinct from new.payment_type
       and old.checkout_country_iso is not distinct from new.checkout_country_iso
       and old.billet_url is not distinct from new.billet_url
       and old.billet_barcode is not distinct from new.billet_barcode
       and old.generated_at is not distinct from new.generated_at
       and old.created_at = new.created_at
       and old.correlation_outcome is null
       and old.purchase_intent_id is null then
        return new;
    end if;
    raise exception using
        errcode = '55000',
        message = 'commercial_ally_payment_slip_evidence_immutable';
end;
$function$;
create trigger commercial_ally_payment_slip_details_append_only
before update or delete on public.commercial_ally_payment_slip_details
for each row execute function public.protect_commercial_ally_payment_slip_evidence();

alter table public.commercial_ally_payment_slip_details enable row level security;

-- ---------------------------------------------------------------------------
-- 2. La forma minima de una ficha (el sobre comun de los eventos de compra
--    2.0.0, medido en las 13 fichas capturadas).
-- ---------------------------------------------------------------------------
create function public.hotmart_payment_slip_payload_is_processable(
    p_external_event_id text,
    p_payload jsonb
) returns boolean
language sql
immutable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    select p_external_event_id is not null
       and nullif(btrim(p_external_event_id), '') is not null
       and jsonb_typeof(p_payload) = 'object'
       and p_payload ->> 'id' = p_external_event_id
       and p_payload ->> 'event' = 'PURCHASE_BILLET_PRINTED'
       and p_payload ->> 'version' = '2.0.0'
       and jsonb_typeof(p_payload #> '{data,product,id}') = 'number'
       and nullif(btrim(p_payload #>> '{data,product,name}'), '') is not null
       and nullif(btrim(p_payload #>> '{data,purchase,offer,code}'), '') is not null
       and p_payload #>> '{data,purchase,status}' = 'BILLET_PRINTED'
       and nullif(btrim(p_payload #>> '{data,purchase,transaction}'), '') ~ '^[A-Za-z0-9._:-]+$'
       and (p_payload ->> 'creation_date') ~ '^[0-9]+$'
       and (p_payload ->> 'creation_date')::numeric > 0
       and (
           nullif(btrim(p_payload #>> '{data,buyer,email}'), '') is not null
           or nullif(regexp_replace(coalesce(
               p_payload #>> '{data,buyer,checkout_phone}',
               p_payload #>> '{data,buyer,phone}', ''
           ), '[^0-9]', '', 'g'), '') is not null
       );
$function$;

-- ---------------------------------------------------------------------------
-- 4. Los parches en el lugar de la correlacion de la intencion.
-- ---------------------------------------------------------------------------
-- La identidad del evento: la ficha lee el telefono como el pago fallido.
do $slip_identity$
declare
    v_definition text;
    v_old constant text :=
        E'    elsif p_event_type = ''PURCHASE_CANCELED'' then\n';
    v_new constant text :=
        E'    elsif p_event_type in (''PURCHASE_CANCELED'', ''PURCHASE_BILLET_PRINTED'') then\n';
begin
    select pg_get_functiondef(
        'public._hotmart_purchase_intent_payload_identity(text,jsonb)'::regprocedure
    ) into v_definition;
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_old, ''))
          <> length(v_old)
       or position('PURCHASE_BILLET_PRINTED' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_hotmart_intent_identity_definition';
    end if;
    execute replace(v_definition, v_old, v_new);
end;
$slip_identity$;

-- La forma procesable: una ficha se valida con su propia funcion.
do $slip_processable$
declare
    v_definition text;
    v_old constant text := $old$    if v_event.event_type = 'PURCHASE_CANCELED' then
        return coalesce(public.hotmart_payment_failure_payload_is_processable(
            v_event.external_event_id,
            v_event.payload
        ), false);
    end if;
    return false;$old$;
    v_new constant text := $new$    if v_event.event_type = 'PURCHASE_CANCELED' then
        return coalesce(public.hotmart_payment_failure_payload_is_processable(
            v_event.external_event_id,
            v_event.payload
        ), false);
    end if;
    if v_event.event_type = 'PURCHASE_BILLET_PRINTED' then
        return coalesce(public.hotmart_payment_slip_payload_is_processable(
            v_event.external_event_id,
            v_event.payload
        ), false);
    end if;
    return false;$new$;
begin
    select pg_get_functiondef(
        'public.hotmart_purchase_intent_payload_is_processable(uuid)'::regprocedure
    ) into v_definition;
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_old, ''))
          <> length(v_old)
       or position('PURCHASE_BILLET_PRINTED' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_hotmart_intent_processable_definition';
    end if;
    execute replace(v_definition, v_old, v_new);
end;
$slip_processable$;

-- El correlador portable: la oferta y la hora como el pago fallido, y una
-- ficha resuelta no clasifica la intencion. El perform deja FOUND como lo deja
-- el update de las otras ramas: si la intencion dejo de esperar la compra en el
-- medio, el chequeo de abajo aborta con 40001 igual que para ellas.
do $slip_correlator$
declare
    v_definition text;
    v_old_offer constant text := $old$    elsif v_event_type = 'PURCHASE_CANCELED' then
        v_offer_ref := nullif(btrim($old$;
    v_new_offer constant text := $new$    elsif v_event_type in ('PURCHASE_CANCELED', 'PURCHASE_BILLET_PRINTED') then
        v_offer_ref := nullif(btrim($new$;
    v_old_class constant text := $old$        elsif v_event_type = 'PURCHASE_CANCELED' then
            update public.purchase_intents
            set current_classification = 'payment_failure_supported',$old$;
    v_new_class constant text := $new$        elsif v_event_type = 'PURCHASE_BILLET_PRINTED' then
            -- payment_slip_first_contact: begin
            -- La ficha no es un abandono ni un pago rechazado: la intencion
            -- sigue esperando la compra y su clasificacion no cambia.
            perform 1
            from public.purchase_intents
            where id = v_resolved_intent_id
              and lifecycle_state = 'waiting_for_purchase';
            -- payment_slip_first_contact: end
        elsif v_event_type = 'PURCHASE_CANCELED' then
            update public.purchase_intents
            set current_classification = 'payment_failure_supported',$new$;
begin
    select pg_get_functiondef(
        'public._correlate_portable_hotmart_purchase_intent(uuid)'::regprocedure
    ) into v_definition;
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_old_offer, ''))
          <> length(v_old_offer)
       or length(v_definition) - length(replace(v_definition, v_old_class, ''))
          <> length(v_old_class)
       or position('PURCHASE_BILLET_PRINTED' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_portable_hotmart_intent_correlator_definition';
    end if;
    execute replace(
        replace(v_definition, v_old_offer, v_new_offer),
        v_old_class,
        v_new_class
    );
end;
$slip_correlator$;

-- ---------------------------------------------------------------------------
-- 3. La admision con cruce.
-- ---------------------------------------------------------------------------
create function public.admit_portable_hotmart_payment_slip_v2(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
    p_external_event_id text,
    p_payload jsonb,
    p_normalized_email text,
    p_normalized_phone text
) returns table (outcome text, webhook_event_id uuid)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_scope public.hotmart_purchase_intent_scopes%rowtype;
    v_existing public.webhook_events%rowtype;
    v_event_id uuid;
    v_admission_outcome text;
    v_correlation record;
    v_offer_code text;
    v_payload_email text;
    v_payload_phone text;
begin
    if not coalesce(public.hotmart_payment_slip_payload_is_processable(
           p_external_event_id, p_payload
       ), false)
       or p_binding_version is null
       or p_binding_version < 1 then
        raise exception using errcode = '22023',
            message = 'invalid_portable_hotmart_payment_slip_input';
    end if;

    select binding.* into v_binding
    from public.commercial_ally_runtime_bindings binding
    where binding.tenant_ref = p_tenant_ref
      and binding.funnel_ref = p_funnel_ref
      and binding.binding_version = p_binding_version
      and binding.status = 'active'
    for update;
    if not found then
        raise exception using errcode = '22023',
            message = 'commercial_ally_binding_unavailable';
    end if;

    -- Otro producto u otra oferta: no se guarda, y no es una falla del webhook
    -- (el criterio de la v1).
    v_offer_code := p_payload #>> '{data,purchase,offer,code}';
    if (p_payload #>> '{data,product,id}')::numeric
          is distinct from v_binding.hotmart_product_id::numeric
       or not coalesce(
              v_offer_code = v_binding.offer_code
              or v_offer_code = any(v_binding.additional_offer_codes),
              false
          ) then
        return query select 'ignored_binding_mismatch'::text, null::uuid;
        return;
    end if;
    select scope.* into v_scope
    from public.hotmart_purchase_intent_scopes scope
    where scope.tenant_ref = v_binding.tenant_ref
      and scope.funnel_ref = v_binding.funnel_ref
      and scope.hotmart_product_id = v_binding.hotmart_product_id::text
      and scope.purchase_intent_product_ref = v_binding.product_hotlink
      and scope.offer_ref = v_offer_code
      and scope.active
    for update;
    if not found then
        return query select 'ignored_binding_mismatch'::text, null::uuid;
        return;
    end if;

    -- La identidad que manda el bridge tiene que ser la del payload.
    v_payload_email := lower(nullif(btrim(p_payload #>> '{data,buyer,email}'), ''));
    v_payload_phone := nullif(regexp_replace(coalesce(
        p_payload #>> '{data,buyer,checkout_phone}',
        p_payload #>> '{data,buyer,phone}',
        ''
    ), '[^0-9]', '', 'g'), '');
    if p_normalized_email is distinct from v_payload_email
       or p_normalized_phone is distinct from v_payload_phone then
        raise exception using errcode = '22023',
            message = 'portable_hotmart_payment_slip_identity_mismatch';
    end if;

    -- El mismo lock que la v1: las dos admisiones serializan por evento.
    perform pg_advisory_xact_lock(
        hashtextextended('payment-slip:' || p_external_event_id, 0)
    );
    select event.* into v_existing
    from public.webhook_events event
    where event.source = 'hotmart'
      and event.external_event_id = p_external_event_id
    for update;
    if not found then
        insert into public.webhook_events (
            source, external_event_id, event_type, payload, processing_status
        ) values (
            'hotmart', p_external_event_id, 'PURCHASE_BILLET_PRINTED',
            p_payload, 'received'
        ) returning id into v_event_id;
        v_admission_outcome := 'inserted';
        insert into public.commercial_ally_hotmart_event_bindings (
            webhook_event_id, scope_id, tenant_ref, funnel_ref, binding_version,
            hotmart_product_id, purchase_intent_product_ref, offer_ref
        ) values (
            v_event_id, v_scope.id, v_binding.tenant_ref, v_binding.funnel_ref,
            v_binding.binding_version, v_scope.hotmart_product_id,
            v_scope.purchase_intent_product_ref, v_scope.offer_ref
        );
        insert into public.commercial_ally_payment_slip_details (
            webhook_event_id,
            transaction_ref,
            payment_type,
            checkout_country_iso,
            billet_url,
            billet_barcode,
            generated_at
        ) values (
            v_event_id,
            p_payload #>> '{data,purchase,transaction}',
            nullif(btrim(p_payload #>> '{data,purchase,payment,type}'), ''),
            upper(nullif(btrim(coalesce(
                p_payload #>> '{data,purchase,checkout_country,iso}',
                p_payload #>> '{data,buyer,address,country_iso}'
            )), '')),
            nullif(btrim(p_payload #>> '{data,purchase,payment,billet_url}'), ''),
            nullif(btrim(p_payload #>> '{data,purchase,payment,billet_barcode}'), ''),
            case
                when p_payload #>> '{data,purchase,order_date}' ~ '^[0-9]{1,15}$'
                    then to_timestamp(
                        (p_payload #>> '{data,purchase,order_date}')::numeric / 1000
                    )
            end
        );
    elsif v_existing.event_type = 'PURCHASE_BILLET_PRINTED'
          and v_existing.payload = p_payload then
        v_event_id := v_existing.id;
        -- Una ficha que guardo la v1 (antes de esta migracion) no tiene fila de
        -- detalle ni correlacion: no se reprocesa.
        if not exists (
            select 1
            from public.commercial_ally_payment_slip_details details
            where details.webhook_event_id = v_event_id
        ) then
            return query select 'ignored_legacy_slip'::text, v_event_id;
            return;
        end if;
        v_admission_outcome := 'duplicate';
    else
        return query select 'semantic_conflict'::text, v_existing.id;
        return;
    end if;

    if not exists (
        select 1
        from public.commercial_ally_hotmart_event_bindings provenance
        where provenance.webhook_event_id = v_event_id
          and provenance.scope_id = v_scope.id
          and provenance.tenant_ref = v_binding.tenant_ref
          and provenance.funnel_ref = v_binding.funnel_ref
          and provenance.binding_version = v_binding.binding_version
    ) then
        raise exception using errcode = '22023',
            message = 'portable_hotmart_payment_slip_replay_binding_mismatch';
    end if;

    perform public._admit_hotmart_purchase_intent_identity(
        v_event_id, p_normalized_email, p_normalized_phone
    );
    select correlation.* into strict v_correlation
    from public._correlate_portable_hotmart_purchase_intent(v_event_id) correlation;
    if v_admission_outcome = 'inserted' then
        perform set_config('app.payment_slip_evidence_finalize', 'on', true);
        update public.commercial_ally_payment_slip_details details
        set correlation_outcome = v_correlation.outcome,
            purchase_intent_id = v_correlation.purchase_intent_id,
            updated_at = clock_timestamp()
        where details.webhook_event_id = v_event_id;
        perform set_config('app.payment_slip_evidence_finalize', 'off', true);
    end if;
    return query select v_admission_outcome, v_event_id;
end;
$function$;

-- ---------------------------------------------------------------------------
-- 5. Las cuatro derivadas del pago fallido. Las cantidades se midieron sobre
--    pg_get_functiondef con la cadena completa de migraciones (2026-10-18, en
--    PGlite). El orden importa solo para leerlo: plpgsql no resuelve las
--    llamadas al crear.
-- ---------------------------------------------------------------------------
do $slip_derive$
declare
    v_spec record;
    v_definition text;
    v_derived text;
    v_index integer;
    v_found integer;
begin
    for v_spec in
        select *
        from (values
            (1,
             'public.plan_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz)',
             'public.plan_payment_slip_recovery(uuid,uuid,text,text,text,text,integer,timestamptz)',
             array[
                 'public.plan_payment_failure_recovery(',
                 $r$'PURCHASE_CANCELED'$r$,
                 $r$'payment_failure'$r$,
                 $r$'payment_failure_$r$,
                 $r$'payment_failure:$r$,
                 $r$'latest_payment_failure_$r$,
                 $r$'repeated_payment_failure'$r$,
                 'then least(due_at, now())',
                 $r$when v_sequence_status = 'active' then null$r$
             ],
             array[
                 'public.plan_payment_slip_recovery(',
                 $r$'PURCHASE_BILLET_PRINTED'$r$,
                 $r$'payment_slip'$r$,
                 $r$'payment_slip_$r$,
                 $r$'payment_slip:$r$,
                 $r$'latest_payment_slip_$r$,
                 $r$'repeated_payment_slip'$r$,
                 'then due_at',
                 $r$when v_sequence_status = 'active' then next_attempt_at$r$
             ],
             array[1, 1, 10, 4, 2, 4, 1, 1, 1]),
            (2,
             'public.plan_payment_failure_recovery_with_identity(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text)',
             'public.plan_payment_slip_recovery_with_identity(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text)',
             array[
                 'public.plan_payment_failure_recovery_with_identity(',
                 'public.plan_payment_failure_recovery(',
                 $r$'PURCHASE_CANCELED'$r$,
                 $r$'payment_failure'$r$
             ],
             array[
                 'public.plan_payment_slip_recovery_with_identity(',
                 'public.plan_payment_slip_recovery(',
                 $r$'PURCHASE_BILLET_PRINTED'$r$,
                 $r$'payment_slip'$r$
             ],
             array[1, 1, 0, 0]),
            (3,
             'public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)',
             'public.plan_portable_payment_slip_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)',
             array[
                 'public.plan_portable_payment_failure_recovery(',
                 'public.plan_payment_failure_recovery_with_identity(',
                 'public.commercial_ally_payment_failure_details',
                 'v_payment_failure',
                 $r$'PURCHASE_CANCELED'$r$,
                 $r$'payment_failure'$r$,
                 $r$'payment_failure_$r$
             ],
             array[
                 'public.plan_portable_payment_slip_recovery(',
                 'public.plan_payment_slip_recovery_with_identity(',
                 'public.commercial_ally_payment_slip_details',
                 'v_payment_slip',
                 $r$'PURCHASE_BILLET_PRINTED'$r$,
                 $r$'payment_slip'$r$,
                 $r$'payment_slip_$r$
             ],
             array[1, 1, 2, 3, 3, 1, 7]),
            (4,
             'public.mark_portable_payment_failure_request_started(uuid,uuid,text,bigint,timestamptz)',
             'public.mark_portable_payment_slip_request_started(uuid,uuid,text,bigint,timestamptz)',
             array[
                 'public.mark_portable_payment_failure_request_started(',
                 $r$'PURCHASE_CANCELED'$r$,
                 $r$'payment_failure'$r$,
                 $r$'payment_failure_$r$
             ],
             array[
                 'public.mark_portable_payment_slip_request_started(',
                 $r$'PURCHASE_BILLET_PRINTED'$r$,
                 $r$'payment_slip'$r$,
                 $r$'payment_slip_$r$
             ],
             array[1, 1, 1, 1])
        ) as spec(ord, source_signature, target_signature, olds, news, counts)
        order by spec.ord
    loop
        if to_regprocedure(v_spec.target_signature) is not null then
            raise exception using errcode = '55000',
                message = 'payment_slip_derivation_target_exists',
                detail = v_spec.target_signature;
        end if;
        select pg_get_functiondef(v_spec.source_signature::regprocedure)
          into v_definition;
        if v_definition is null
           or position('payment_slip' in v_definition) > 0
           or position('PURCHASE_BILLET_PRINTED' in v_definition) > 0 then
            raise exception using errcode = '55000',
                message = 'unexpected_payment_failure_definition',
                detail = v_spec.source_signature;
        end if;

        v_derived := v_definition;
        for v_index in 1 .. cardinality(v_spec.olds) loop
            v_found := (
                length(v_definition)
                - length(replace(v_definition, v_spec.olds[v_index], ''))
            ) / length(v_spec.olds[v_index]);
            if v_found <> v_spec.counts[v_index] then
                raise exception using errcode = '55000',
                    message = 'unexpected_payment_failure_definition',
                    detail = format(
                        '%s: %s aparece %s veces, se esperaban %s',
                        v_spec.source_signature, v_spec.olds[v_index],
                        v_found, v_spec.counts[v_index]
                    );
            end if;
            v_derived := replace(
                v_derived, v_spec.olds[v_index], v_spec.news[v_index]
            );
        end loop;

        -- Nada del pago fallido queda en la derivada (los comentarios dicen
        -- "pago fallido" en castellano y no cuentan).
        if position('payment_failure' in v_derived) > 0
           or position('PURCHASE_CANCELED' in v_derived) > 0 then
            raise exception using errcode = '55000',
                message = 'payment_slip_derivation_incomplete',
                detail = v_spec.target_signature;
        end if;

        execute v_derived;
        if to_regprocedure(v_spec.target_signature) is null then
            raise exception using errcode = '55000',
                message = 'payment_slip_derivation_not_created',
                detail = v_spec.target_signature;
        end if;
    end loop;
end;
$slip_derive$;

-- ---------------------------------------------------------------------------
-- 6. El estado del runtime del scope de la ficha: copia de
--    get_lancemos_pilot_runtime_status con PURCHASE_BILLET_PRINTED.
-- ---------------------------------------------------------------------------
create function public.get_portable_payment_slip_pilot_runtime_status(
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
       or v_scope.source <> 'hotmart'
       or (
           v_scope.source_event_type <> 'PURCHASE_BILLET_PRINTED'
           and 'PURCHASE_BILLET_PRINTED' <> all(v_scope.additional_source_event_types)
       ) then
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

-- ---------------------------------------------------------------------------
-- 7. Los hechos de la ficha para componer el envio, con la lease vigente
--    (el cerco de get_followup_execution_context). Si la persona genero mas
--    de una ficha con el caso abierto, va la mas reciente: es la que tiene la
--    referencia y el vencimiento vigentes.
-- ---------------------------------------------------------------------------
create function public.get_portable_payment_slip_send_context(
    p_action_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_now timestamptz
)
returns table (
    webhook_event_id uuid,
    payment_type text,
    checkout_country_iso text,
    billet_url text,
    billet_barcode text,
    generated_at timestamptz
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
begin
    if p_action_id is null
       or p_worker_id is null or btrim(p_worker_id) = ''
       or p_lease_generation is null or p_lease_generation < 1
       or p_now is null then
        raise exception using errcode = '22023', message = 'invalid_followup_fence';
    end if;

    select action.* into v_action
    from public.scheduled_actions action
    where action.id = p_action_id;
    if not found
       or not (
           v_action.lease_owner = p_worker_id
           and v_action.lease_generation = p_lease_generation
           and v_action.lease_expires_at > p_now
           and v_action.expires_at > p_now
           and v_action.status in ('pending', 'deferred', 'retryable_failed')
       ) then
        raise exception using errcode = '55000', message = 'current_action_lease_not_found';
    end if;
    if v_action.anchor_type is distinct from 'payment_slip' then
        raise exception using errcode = '55000', message = 'payment_slip_action_required';
    end if;

    return query
    select
        details.webhook_event_id,
        details.payment_type,
        details.checkout_country_iso,
        details.billet_url,
        details.billet_barcode,
        details.generated_at
    from public.recovery_case_events case_event
    join public.commercial_ally_payment_slip_details details
      on details.webhook_event_id = case_event.webhook_event_id
    where case_event.recovery_case_id = v_action.recovery_case_id
      and case_event.event_role = 'payment_slip'
      and details.correlation_outcome = 'resolved'
    order by case_event.observed_at desc,
             case_event.created_at desc,
             details.webhook_event_id desc
    limit 1;
    if not found then
        raise exception using errcode = '55000', message = 'payment_slip_details_missing';
    end if;
end;
$function$;

-- ---------------------------------------------------------------------------
-- ACL. Supabase le da execute por defecto a toda funcion nueva y todos los
-- privilegios de tabla a los roles de la API: todo se revoca explicito. Las
-- cinco entradas del bridge quedan solo para service_role; los dos planes
-- internos, la forma procesable, el trigger y la tabla, para nadie de la API
-- (los usan las funciones security definer de arriba). Los roles se miran con
-- to_regrole: los validadores que no los crean aplican igual la migracion.
-- ---------------------------------------------------------------------------
revoke all on table public.commercial_ally_payment_slip_details from public;
do $roles$
declare
    v_signature text;
    v_role text;
    v_entrypoints constant text[] := array[
        'public.admit_portable_hotmart_payment_slip_v2(text,text,integer,text,jsonb,text,text)',
        'public.plan_portable_payment_slip_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)',
        'public.mark_portable_payment_slip_request_started(uuid,uuid,text,bigint,timestamptz)',
        'public.get_portable_payment_slip_pilot_runtime_status(text,integer,text,text,text)',
        'public.get_portable_payment_slip_send_context(uuid,text,bigint,timestamptz)'
    ];
    v_private constant text[] := array[
        'public.plan_payment_slip_recovery(uuid,uuid,text,text,text,text,integer,timestamptz)',
        'public.plan_payment_slip_recovery_with_identity(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text)',
        'public.hotmart_payment_slip_payload_is_processable(text,jsonb)',
        'public.protect_commercial_ally_payment_slip_evidence()'
    ];
begin
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format(
                'revoke all on table public.commercial_ally_payment_slip_details from %I',
                v_role
            );
        end if;
    end loop;

    foreach v_signature in array v_entrypoints || v_private loop
        execute format('revoke all on function %s from public', v_signature);
        foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
            if to_regrole(v_role) is not null then
                execute format('revoke all on function %s from %I', v_signature, v_role);
            end if;
        end loop;
    end loop;
    if to_regrole('service_role') is not null then
        foreach v_signature in array v_entrypoints loop
            execute format('grant execute on function %s to service_role', v_signature);
        end loop;
    end if;

    -- Por presencia: service_role ejecuta las entradas y nada mas; ningun rol
    -- de la API ejecuta nada ni toca la tabla.
    foreach v_signature in array v_entrypoints || v_private loop
        if exists (
               select 1 from pg_roles
               where rolname = 'service_role'
                 and has_function_privilege(rolname, v_signature, 'EXECUTE')
                     is distinct from (v_signature = any(v_entrypoints))
           )
           or exists (
               select 1 from pg_roles
               where rolname in ('anon', 'authenticated')
                 and has_function_privilege(rolname, v_signature, 'EXECUTE')
           ) then
            raise exception using errcode = '55000',
                message = 'portable_payment_slip_acl_unexpected',
                detail = v_signature;
        end if;
    end loop;
    if exists (
        select 1
        from pg_roles role
        cross join unnest(array[
            'select', 'insert', 'update', 'delete', 'truncate', 'references', 'trigger'
        ]) privilege_name
        where role.rolname in ('anon', 'authenticated', 'service_role')
          and has_table_privilege(
              role.rolname, 'public.commercial_ally_payment_slip_details', privilege_name
          )
    ) then
        raise exception using errcode = '55000',
            message = 'portable_payment_slip_acl_unexpected',
            detail = 'public.commercial_ally_payment_slip_details';
    end if;
end
$roles$;

commit;
