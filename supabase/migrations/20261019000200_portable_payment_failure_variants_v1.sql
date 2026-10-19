-- Migration: B1, el primer toque del pago rechazado de ATT1 con cuatro
-- variantes. Flujo 1 de los flujos comerciales de ATT1.
--
-- Por que. Medido el 2026-10-18 en la base de ATT1 sobre los 17
-- PURCHASE_CANCELED guardados desde el 11/10 (13 personas; fixture
-- tests/fixtures/hotmart_purchase_canceled_att1_20261019.json):
-- - el mensaje sale en el minuto 0 (la politica compartida con el carrito
--   tiene gracia 0), y 4 de las 10 personas con rechazo compraron solas a los
--   0, 2, 4 y 13 minutos;
-- - la persona con dos rechazos en un minuto recibio dos mensajes: el primer
--   caso se cierra apenas sale su unico mensaje (sequence_exhausted) y el
--   segundo rechazo no encuentra caso abierto y abre otro;
-- - un Nequi que se cancelo sin motivo (un pendiente, no un rechazo) recibio
--   el mensaje de pago rechazado.
-- El diseno y las decisiones de Dan estan en el vault:
-- documentacion/2026-10-18-diseno-b1-el-pago-rechazado.md y
-- decisiones/2026-10-18-el-pago-rechazado-le-gana-al-primer-contacto-y-b1-banco-sale-sin-guia.md.
--
-- Que crea:
--
-- 1. payment_failure_refusal_kind(text, text): la unica regla que separa un
--    rechazo por saldo de uno del banco, y un pendiente cancelado de un
--    rechazo. refusal_reason no es un codigo: llega como texto libre y en dos
--    idiomas ('Transaction refused', 'Transação negada pelo banco emissor',
--    'Saldo insuficiente.', 'Saldo insuficiente para o cartão informado...').
--    El bridge no repite la regla: la lee del contexto de envio (5).
-- 2. payment_failure_wants_first_coupon(uuid): si el caso necesita el cupon
--    del primer toque (B1-saldo). Lo usa la siembra del cupon (4c).
-- 3. get_portable_payment_failure_pilot_runtime_status, copia de
--    get_portable_payment_slip_pilot_runtime_status con PURCHASE_CANCELED:
--    el pago rechazado puede tener su propio scope y su propia politica (la
--    gracia de 15 minutos), como la ficha.
-- 4. Parches en el lugar, por pg_get_functiondef + replace con la cantidad
--    exacta de ocurrencias (aborta con 55000 si alguna no coincide):
--    a. plan_payment_failure_recovery:
--       - un caso por persona y producto, sin mirar la oferta, y un rechazo
--         que llega dentro de las 72 h de un caso que ya mando su mensaje (o
--         que compro, o que tomo una persona) se suma a ese caso y no abre
--         otro: no sale un segundo mensaje. 72 h es lo que dura la secuencia
--         entera (B1, B2 a las 24 h, Z 48 h despues);
--       - el segundo rechazo con el mensaje pendiente ya no lo adelanta a
--         ahora: lo corre a la gracia contada desde el ultimo rechazo (sale un
--         solo mensaje, el del ultimo evento, a los 15 minutos);
--       - el contexto del caso cuenta los rechazos (payment_failure_count).
--    b. plan_portable_payment_failure_recovery: un cancelado sin motivo y con
--       un medio que no es tarjeta ni PayPal no es un rechazo y no se
--       planifica (payment_failure_not_a_refusal). Es el flujo 2.
--    c. claim_due_recovery_coupons_v1: siembra tambien el cupon del primer
--       toque de un pago rechazado que lo necesita (2). Misma firma, mismo
--       resultado.
--    d. reserve_recovery_checkout_issuance_v1: emite tambien el link del
--       primer toque de un pago rechazado. Misma firma, mismo resultado.
-- 5. get_portable_payment_failure_send_context_v1: con la lease vigente de una
--    accion payment_failure, lo que el bridge necesita para elegir la
--    variante: el tipo de rechazo del ultimo evento, cuantos rechazos van, la
--    hora del ultimo, el medio, y el cupon vigente de la persona en otro caso
--    (B1-cupon).
--
-- Lo que no cambia: Johanna (nada de lo que ejecuta se redefine), el carrito
-- (las dos funciones del cupon y del link se comportan igual para
-- cart_abandonment), la ficha (sus funciones derivadas ya existen y no se
-- regeneran), la audiencia, la baja, los topes y la compra. El corte por
-- compra de cualquier oferta lo hace otra migracion (20261019000300).
--
-- Lo que cambia apenas se aplica, sin tocar el bridge: el segundo rechazo
-- deja de mandar un segundo mensaje, y el cancelado sin motivo deja de
-- recibir el de pago rechazado. Las variantes, la gracia de 15 minutos y el
-- link salen recien con PAYMENT_FAILURE_VARIANTS_ENABLED y la politica nueva.
--
-- Locks: solo create function y create or replace; ninguna tabla se altera.
-- No siembra filas de ningun cliente.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- ---------------------------------------------------------------------------
-- 1. El tipo de rechazo.
-- ---------------------------------------------------------------------------
create function public.payment_failure_refusal_kind(
    p_refusal_reason text,
    p_payment_type text
)
returns text
language sql
immutable
set search_path = pg_catalog, pg_temp
as $function$
    select case
        when nullif(btrim(coalesce(p_refusal_reason, '')), '') is null
             and coalesce(upper(btrim(p_payment_type)), '') not in ('CREDIT_CARD', 'PAYPAL')
            then 'not_a_refusal'
        when translate(lower(coalesce(p_refusal_reason, '')), 'áàâãéêíóôõúç', 'aaaaeeiooouc')
             ~ '(saldo|insufficient|fondos insuficientes|sin fondos|limite)'
            then 'insufficient_funds'
        else 'declined'
    end;
$function$;

-- ---------------------------------------------------------------------------
-- 2. Si el primer toque necesita su propio cupon: B1-saldo. No lo necesita si
--    ya hubo dos rechazos (B1-dos-rechazos va sin cupon) ni si la persona
--    tiene un cupon vigente de otro caso del producto (B1-cupon usa ese).
-- ---------------------------------------------------------------------------
create function public.payment_failure_wants_first_coupon(p_recovery_case_id uuid)
returns boolean
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_case public.recovery_cases%rowtype;
    v_kind text;
begin
    select rc.* into v_case
    from public.recovery_cases rc
    where rc.id = p_recovery_case_id
      and rc.context ->> 'trigger_kind' = 'payment_failure';
    if not found
       or coalesce((v_case.context ->> 'payment_failure_count')::integer, 1) <> 1 then
        return false;
    end if;

    select public.payment_failure_refusal_kind(
               details.refusal_reason,
               event.payload #>> '{data,purchase,payment,type}'
           )
      into v_kind
    from public.webhook_events event
    left join public.commercial_ally_payment_failure_details details
      on details.webhook_event_id = event.id
    where event.id = (v_case.context ->> 'latest_payment_failure_event_id')::uuid;
    if v_kind is distinct from 'insufficient_funds' then
        return false;
    end if;

    return not exists (
        select 1
        from public.recovery_coupons coupon
        join public.recovery_cases other on other.id = coupon.recovery_case_id
        where other.contact_id = v_case.contact_id
          and other.id <> v_case.id
          and coupon.external_product_id = v_case.external_product_id
          and coupon.status = 'valid'
          and coupon.expires_at > clock_timestamp() + interval '1 hour'
    );
end;
$function$;

-- ---------------------------------------------------------------------------
-- 3. El estado del runtime de un scope del pago rechazado.
-- ---------------------------------------------------------------------------
create function public.get_portable_payment_failure_pilot_runtime_status(
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
           v_scope.source_event_type <> 'PURCHASE_CANCELED'
           and 'PURCHASE_CANCELED' <> all(v_scope.additional_source_event_types)
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
-- 4. Los parches en el lugar. Cada reemplazo con su cantidad exacta: si otra
--    migracion cambio el texto, esta falla a proposito.
-- ---------------------------------------------------------------------------
create function pg_temp.payment_failure_variants_patch(
    p_signature text,
    p_olds text[],
    p_news text[],
    p_already_marker text
)
returns void
language plpgsql
as $function$
declare
    v_definition text;
    v_index integer;
begin
    select pg_get_functiondef(p_signature::regprocedure) into v_definition;
    if v_definition is null
       or position(p_already_marker in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_payment_failure_variants_definition',
            detail = p_signature;
    end if;
    for v_index in 1 .. coalesce(array_length(p_olds, 1), 0) loop
        if length(v_definition) - length(replace(v_definition, p_olds[v_index], ''))
           <> length(p_olds[v_index]) then
            raise exception using errcode = '55000',
                message = 'unexpected_payment_failure_variants_definition',
                detail = p_signature || ' #' || v_index;
        end if;
        v_definition := replace(v_definition, p_olds[v_index], p_news[v_index]);
    end loop;
    execute v_definition;
end;
$function$;

-- 4a. plan_payment_failure_recovery.
select pg_temp.payment_failure_variants_patch(
    'public.plan_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz)',
    array[
$o1$      and rc.offer_code is not distinct from p_offer_code
      and rc.status in ('grace_period', 'active', 'paused')
      and rc.context ->> 'trigger_kind' = 'payment_failure'
    order by rc.created_at asc$o1$,
$o2$                    when v_sequence_status = 'active' then least(due_at, now())$o2$,
$o3$                    when v_sequence_status = 'active' then null
                    else next_attempt_at$o3$,
$o4$                'latest_payment_failure_at', p_failed_at
            ),
            version = version + 1$o4$,
$o5$            'latest_payment_failure_at', p_failed_at
        )
    )
    returning id into v_case_id;$o5$
    ],
    array[
$n1$      and rc.context ->> 'trigger_kind' = 'payment_failure'
      -- payment_failure_variants: un caso por persona y producto. Un rechazo
      -- dentro de las 72 h de un caso que ya mando su mensaje, compro o tomo
      -- una persona se suma a ese caso: no abre otro ni manda otro mensaje.
      and (
          rc.status in ('grace_period', 'active', 'paused')
          or (
              rc.status in ('sequence_exhausted', 'won', 'escalated')
              and rc.created_at > p_failed_at - interval '72 hours'
          )
      )
    order by (rc.status in ('grace_period', 'active', 'paused')) desc,
             rc.created_at asc$n1$,
$n2$                    -- payment_failure_variants: un solo mensaje, el del
                    -- ultimo rechazo, a la gracia contada desde ese rechazo.
                    when v_sequence_status = 'active'
                        then greatest(due_at, p_failed_at + v_policy.grace_period)$n2$,
$n3$                    when v_sequence_status = 'active' and next_attempt_at is not null
                        then greatest(next_attempt_at, p_failed_at + v_policy.grace_period)
                    else next_attempt_at$n3$,
$n4$                'latest_payment_failure_at', p_failed_at,
                'payment_failure_count',
                    coalesce((context ->> 'payment_failure_count')::integer, 1) + 1
            ),
            version = version + 1$n4$,
$n5$            'latest_payment_failure_at', p_failed_at,
            'payment_failure_count', 1
        )
    )
    returning id into v_case_id;$n5$
    ],
    'payment_failure_count'
);

-- 4b. plan_portable_payment_failure_recovery: un cancelado que no es un
--     rechazo no se planifica.
select pg_temp.payment_failure_variants_patch(
    'public.plan_portable_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)',
    array[
$o1$            message = 'payment_failure_correlation_unresolved';
    end if;$o1$
    ],
    array[
$n1$            message = 'payment_failure_correlation_unresolved';
    end if;

    -- payment_failure_variants: un cancelado sin motivo con un medio que no es
    -- tarjeta ni PayPal es un pendiente que se cancelo (flujo 2), no un
    -- rechazo.
    if public.payment_failure_refusal_kind(
           v_payment_failure.refusal_reason,
           v_webhook.payload #>> '{data,purchase,payment,type}'
       ) = 'not_a_refusal' then
        raise exception using
            errcode = '55000',
            message = 'payment_failure_not_a_refusal';
    end if;$n1$
    ],
    'payment_failure_not_a_refusal'
);

-- 4c. claim_due_recovery_coupons_v1: el cupon del primer toque de B1-saldo.
select pg_temp.payment_failure_variants_patch(
    'public.claim_due_recovery_coupons_v1(timestamptz,text,integer,text,interval,interval,numeric,integer)',
    array[
$o1$          and sa.anchor_type = 'cart_abandonment'$o1$
    ],
    array[
$n1$          and (
              sa.anchor_type = 'cart_abandonment'
              -- payment_failure_variants: el primer toque de un rechazo por
              -- saldo lleva su propio cupon (B1-saldo).
              or (
                  sa.anchor_type = 'payment_failure'
                  and public.payment_failure_wants_first_coupon(rc.id)
              )
          )$n1$
    ],
    'payment_failure_wants_first_coupon'
);

-- 4d. reserve_recovery_checkout_issuance_v1: el link del primer toque.
select pg_temp.payment_failure_variants_patch(
    'public.reserve_recovery_checkout_issuance_v1(uuid,text,bigint,uuid,text,timestamptz)',
    array[
$o1$       or v_action.anchor_type is distinct from 'cart_abandonment'$o1$
    ],
    array[
$n1$       -- payment_failure_variants: tambien el primer toque del pago rechazado.
       or v_action.anchor_type is null
       or v_action.anchor_type not in ('cart_abandonment', 'payment_failure')$n1$
    ],
    'payment_failure_variants'
);

-- ---------------------------------------------------------------------------
-- 5. Lo que el bridge necesita para elegir la variante, con la lease vigente
--    (el cerco de get_followup_execution_context).
-- ---------------------------------------------------------------------------
create function public.get_portable_payment_failure_send_context_v1(
    p_action_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_now timestamptz
)
returns table (
    refusal_kind text,
    payment_failure_count integer,
    latest_failed_at timestamptz,
    payment_type text,
    other_coupon_code text,
    other_coupon_expires_at timestamptz
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
    v_case public.recovery_cases%rowtype;
    v_event_id uuid;
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
    if v_action.anchor_type is distinct from 'payment_failure' then
        raise exception using errcode = '55000', message = 'payment_failure_action_required';
    end if;

    select rc.* into v_case
    from public.recovery_cases rc
    where rc.id = v_action.recovery_case_id;
    v_event_id := nullif(v_case.context ->> 'latest_payment_failure_event_id', '')::uuid;
    if v_event_id is null then
        raise exception using errcode = '55000', message = 'payment_failure_details_missing';
    end if;

    return query
    select
        public.payment_failure_refusal_kind(
            details.refusal_reason,
            event.payload #>> '{data,purchase,payment,type}'
        ),
        coalesce((v_case.context ->> 'payment_failure_count')::integer, 1),
        to_timestamp((event.payload ->> 'creation_date')::double precision / 1000.0),
        event.payload #>> '{data,purchase,payment,type}',
        other.code,
        other.expires_at
    from public.webhook_events event
    join public.commercial_ally_payment_failure_details details
      on details.webhook_event_id = event.id
    left join lateral (
        select coupon.code, coupon.expires_at
        from public.recovery_coupons coupon
        join public.recovery_cases other_case
          on other_case.id = coupon.recovery_case_id
        where other_case.contact_id = v_case.contact_id
          and other_case.id <> v_case.id
          and coupon.external_product_id = v_case.external_product_id
          and coupon.status = 'valid'
          and coupon.expires_at > p_now + interval '1 hour'
        order by coupon.expires_at desc, coupon.id
        limit 1
    ) other on true
    where event.id = v_event_id;
    if not found then
        raise exception using errcode = '55000', message = 'payment_failure_details_missing';
    end if;
end;
$function$;

-- ---------------------------------------------------------------------------
-- ACL. Supabase le da execute por defecto a toda funcion nueva: se revoca
-- explicito. Las dos entradas del bridge quedan solo para service_role; las dos
-- reglas, para nadie de la API (las usan las funciones security definer).
-- ---------------------------------------------------------------------------
do $roles$
declare
    v_signature text;
    v_role text;
    v_entrypoints constant text[] := array[
        'public.get_portable_payment_failure_pilot_runtime_status(text,integer,text,text,text)',
        'public.get_portable_payment_failure_send_context_v1(uuid,text,bigint,timestamptz)'
    ];
    v_private constant text[] := array[
        'public.payment_failure_refusal_kind(text,text)',
        'public.payment_failure_wants_first_coupon(uuid)'
    ];
begin
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
    -- de la API ejecuta nada.
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
                message = 'payment_failure_variants_acl_unexpected',
                detail = v_signature;
        end if;
    end loop;
end
$roles$;

commit;
