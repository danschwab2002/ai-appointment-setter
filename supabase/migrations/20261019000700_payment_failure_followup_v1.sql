-- Migration: el seguimiento del pago rechazado (B2 o Z), el segundo y ultimo
-- toque a quien no contesto B1.
--
-- Contrato: docs/contracts/portable-payment-failure-followup-v1.md.
--
-- Por que. Decision de Dan del 2026-10-19: a quien no contesto, el pago
-- rechazado le manda como maximo dos mensajes, B1 (20261019000200) y un
-- seguimiento 24 h despues. El seguimiento es B2
-- (att1_pago_rechazado_descuento_01) si B1 salio sin cupon (dos rechazos o el
-- banco), y Z (att1_ultima_renovacion_01) si salio con cupon (el de otro caso o
-- el del saldo). Los dos llevan el cupon 2 del caso y nuestro link. El paso lo
-- declara la politica de la instancia con las marcas de 20261019000600:
--
--   {"step_key": "payment_failure_followup", "mode": "freeform",
--    "delay": "24 hours", "approved_template": true, "coupon_ordinal": 2}
--
-- y el motor lo inserta como no_reply_review al aceptarse B1
-- (_finalize_followup_delivery_attempt, 20260813000100). La reevaluacion, la
-- adopcion de la respuesta, la siembra del cupon 2 y la emision del link ya lo
-- reconocen por esas marcas. Faltan tres cosas, y un bug que el segundo paso
-- destapa:
--
-- 1. Que variante salio en B1. Hoy no queda en ningun lado: el bridge la elige
--    con el contexto de envio y la manda. payment_failure_first_touch_records
--    (nueva, una fila por intento de B1, inmutable) la guarda con el cupon que
--    llevo el mensaje, y record_payment_failure_first_touch_v1 (nueva) la
--    escribe con el cerco de la lease, antes del envio. Si B1 cae a la
--    plantilla de hoy (sin variante), no se registra nada.
-- 2. get_payment_failure_followup_context_v1 (nueva): con la lease vigente del
--    seguimiento, si es B2 (second_touch) o Z (last_touch). La regla: Z si el
--    registro del intento de B1 que Chatwoot acepto tiene cupon; si no (sin
--    cupon o sin registro), B2.
-- 3. mark_portable_payment_failure_request_started exige anchor_type =
--    'payment_failure', y el seguimiento esta anclado al mensaje aceptado de
--    B1: lo rechazaria con payment_failure_action_required. Acepta tambien un
--    no_reply_review cuya secuencia es de un pago rechazado
--    (followup_sequences.reason = 'payment_failure', la que escribe
--    plan_payment_failure_recovery), igual que 20261019000400 con la ficha.
--    Todo lo demas queda igual: la autorizacion contra el scope del caso con
--    PURCHASE_CANCELED, sus topes (el seguimiento cuenta como un arranque), y
--    la baja.
-- 4. El bug. Con una politica de un paso el caso se cierra al salir B1, y un
--    rechazo nuevo dentro de las 72 h se suma al caso cerrado. Con dos pasos el
--    caso sigue active hasta el seguimiento: plan_payment_failure_recovery
--    suma el rechazo, hace version = version + 1 y corre el
--    expected_case_version solo de B1. El seguimiento pendiente queda con la
--    version vieja y la reevaluacion lo pausa (authoritative_state_changed) y
--    pausa el caso. Ahora corre tambien el expected_case_version de los
--    no_reply_review pendientes del caso, sin tocar su due_at: los relojes no
--    se reinician y no sale ningun mensaje de mas.
-- 5. request_recovery_renewal_v1 (20261019000600) adelanta el paso pendiente
--    con coupon_ordinal de la conversacion. El seguimiento tambien lo tiene:
--    quien tiene el seguimiento del pago rechazado pendiente y toca
--    «Quiero mi 10 %» en un A2 del carrito de la misma conversacion lo
--    adelantaria, y la reevaluacion ya no lo cancelaria por la respuesta.
--    Ahora adelanta solo cart_last_touch, el unico paso que ese boton pide
--    (docs/contracts/portable-cart-followups-v1.md).
--
-- Como. Cada funcion existente se redefine desde su definicion viva
-- (pg_get_functiondef), reemplazando un texto exacto que tiene que aparecer
-- una sola vez: si otra migracion lo cambio, o esta ya se aplico, aborta con
-- 55000 sin tocar nada. La firma, el dueno, security definer o invoker, el
-- search_path y los permisos no cambian (se verifican al final).
--
-- Lo que no cambia: B1 (su eleccion, su cupon y su link), el carrito, la
-- ficha, Johanna, la audiencia, la baja y la compra. Ninguna politica
-- publicada tiene el paso payment_failure_followup: hasta que la instancia
-- publique una con dos pasos, el pago rechazado sigue mandando solo B1, y el
-- unico efecto de aplicar esto es el registro de la variante (cuando el bridge
-- lo escribe) y el arreglo del punto 4, que con un paso no tiene que corregir.
--
-- Locks: create table, create function y create or replace; ninguna tabla
-- existente se altera. No siembra filas de ningun cliente.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- ---------------------------------------------------------------------------
-- 3. y 4. Las funciones existentes. Va primero: correrla dos veces aborta aca,
--    con 55000, antes de crear nada.
-- ---------------------------------------------------------------------------
do $followup$
declare
    v_spec record;
    v_definition text;
    v_found integer;
    v_grants_before text;
    v_grants_after text;
begin
    for v_spec in
        select *
        from (values
            (1,
             'public.mark_portable_payment_failure_request_started(uuid,uuid,text,bigint,timestamptz)',
             $old$        where action.id = p_action_id
          and action.anchor_type = 'payment_failure'
    ) then
$old$,
             $new$        where action.id = p_action_id
          and (
              action.anchor_type = 'payment_failure'
              -- payment_failure_followup (20261019000700): y el seguimiento
              -- (B2 o Z), el paso siguiente de una secuencia del pago
              -- rechazado.
              or (
                  action.action_type = 'no_reply_review'
                  and exists (
                      select 1
                      from public.followup_sequences refusal_sequence
                      where refusal_sequence.id = action.followup_sequence_id
                        and refusal_sequence.reason = 'payment_failure'
                  )
              )
          )
    ) then
$new$),
            (2,
             'public.plan_payment_failure_recovery(uuid,uuid,text,text,text,text,integer,timestamptz)',
             $old$                expected_case_version = v_case_version
            where id = v_action_id
              and status in ('pending', 'deferred', 'retryable_failed');
$old$,
             $new$                expected_case_version = v_case_version
            where id = v_action_id
              and status in ('pending', 'deferred', 'retryable_failed');

            -- payment_failure_followup (20261019000700): el seguimiento que
            -- espera (B2 o Z) sigue la version nueva del caso, sin cambiar su
            -- hora. Sin esto la reevaluacion lo pausa con
            -- authoritative_state_changed y pausa el caso.
            update public.scheduled_actions waiting
            set expected_case_version = v_case_version
            where waiting.recovery_case_id = v_case_id
              and waiting.action_type = 'no_reply_review'
              and waiting.status in ('pending', 'deferred', 'retryable_failed');
$new$),
            (3,
             'public.request_recovery_renewal_v1(bigint,bigint,bigint,bigint,text,timestamptz)',
             $old$      and coalesce(public._recovery_followup_step(action.id) ->> 'coupon_ordinal', '')
              in ('1', '2')
$old$,
             $new$      and coalesce(public._recovery_followup_step(action.id) ->> 'coupon_ordinal', '')
              in ('1', '2')
      -- payment_failure_followup (20261019000700): el boton «Quiero mi 10 %»
      -- de A2 adelanta solo Z del carrito, nunca el seguimiento del pago
      -- rechazado de la misma conversacion.
      and action.step_key = 'cart_last_touch'
$new$)
        ) as spec(ord, signature, old_text, new_text)
        order by spec.ord
    loop
        if to_regprocedure(v_spec.signature) is null then
            raise exception using errcode = '55000',
                message = 'payment_failure_followup_function_missing',
                detail = v_spec.signature;
        end if;
        select pg_get_functiondef(v_spec.signature::regprocedure)
          into v_definition;
        if position('payment_failure_followup (20261019000700)' in v_definition) > 0 then
            raise exception using errcode = '55000',
                message = 'payment_failure_followup_already_applied',
                detail = format('%s (%s)', v_spec.signature, v_spec.ord);
        end if;
        v_found := (
            length(v_definition)
            - length(replace(v_definition, v_spec.old_text, ''))
        ) / length(v_spec.old_text);
        if v_found <> 1 then
            raise exception using errcode = '55000',
                message = 'unexpected_payment_failure_followup_definition',
                detail = format(
                    '%s (%s): el texto aparece %s veces, se esperaba 1',
                    v_spec.signature, v_spec.ord, v_found
                );
        end if;
        select string_agg(rolname || ':' || has_function_privilege(rolname, v_spec.signature, 'EXECUTE'), ',' order by rolname)
          into v_grants_before
        from pg_roles
        where rolname in ('anon', 'authenticated', 'service_role');
        execute replace(v_definition, v_spec.old_text, v_spec.new_text);
        select pg_get_functiondef(v_spec.signature::regprocedure)
          into v_definition;
        if position(v_spec.new_text in v_definition) = 0 then
            raise exception using errcode = '55000',
                message = 'payment_failure_followup_not_applied',
                detail = format('%s (%s)', v_spec.signature, v_spec.ord);
        end if;
        -- Los permisos que create or replace conserva, iguales que antes.
        select string_agg(rolname || ':' || has_function_privilege(rolname, v_spec.signature, 'EXECUTE'), ',' order by rolname)
          into v_grants_after
        from pg_roles
        where rolname in ('anon', 'authenticated', 'service_role');
        if v_grants_after is distinct from v_grants_before then
            raise exception using errcode = '55000',
                message = 'payment_failure_followup_grants_changed',
                detail = v_spec.signature;
        end if;
    end loop;
end;
$followup$;

-- ---------------------------------------------------------------------------
-- 1a. La variante de cada intento de B1.
-- ---------------------------------------------------------------------------
create table public.payment_failure_first_touch_records (
    delivery_attempt_id uuid primary key
        references public.followup_delivery_attempts (id) on delete restrict,
    action_id uuid not null
        references public.scheduled_actions (id) on delete restrict,
    recovery_case_id uuid not null
        references public.recovery_cases (id) on delete restrict,
    variant text not null check (variant in (
        'coupon', 'two_refusals', 'insufficient_funds', 'declined'
    )),
    coupon_code text check (coupon_code ~ '^[A-Z0-9]{8,25}$'),
    created_at timestamptz not null default now(),
    -- B1-cupon y B1-saldo salen con un cupon; dos rechazos y el banco, sin.
    constraint payment_failure_first_touch_records_coupon_check check (
        (variant in ('coupon', 'insufficient_funds')) = (coupon_code is not null)
    )
);

create index payment_failure_first_touch_records_case_idx
on public.payment_failure_first_touch_records (recovery_case_id);

alter table public.payment_failure_first_touch_records enable row level security;

create function public.protect_payment_failure_first_touch_record()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    -- payment_failure_followup: lo que salio en B1 no se reescribe
    raise exception using errcode = '55000',
        message = 'payment_failure_first_touch_record_immutable';
end;
$function$;

create trigger payment_failure_first_touch_record_immutable
before update or delete on public.payment_failure_first_touch_records
for each row execute function public.protect_payment_failure_first_touch_record();

-- ---------------------------------------------------------------------------
-- 1b. El registro, con el cerco de la lease (el de
--     get_portable_payment_failure_send_context_v1). Idempotente por intento:
--     el mismo valor otra vez da already_recorded; otro valor, 55000.
-- ---------------------------------------------------------------------------
create function public.record_payment_failure_first_touch_v1(
    p_action_id uuid,
    p_attempt_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_variant text,
    p_coupon_code text,
    p_now timestamptz
)
returns table (outcome text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
    v_attempt public.followup_delivery_attempts%rowtype;
    v_record public.payment_failure_first_touch_records%rowtype;
begin
    if p_action_id is null
       or p_attempt_id is null
       or p_worker_id is null or btrim(p_worker_id) = ''
       or p_lease_generation is null or p_lease_generation < 1
       or p_now is null then
        raise exception using errcode = '22023', message = 'invalid_followup_fence';
    end if;
    if p_variant is null
       or p_variant not in ('coupon', 'two_refusals', 'insufficient_funds', 'declined')
       or (p_variant in ('coupon', 'insufficient_funds')) <> (p_coupon_code is not null) then
        raise exception using errcode = '22023',
            message = 'invalid_payment_failure_first_touch_record';
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
    if v_action.action_type <> 'first_contact_review'
       or v_action.anchor_type is distinct from 'payment_failure'
       or v_action.recovery_case_id is null then
        raise exception using errcode = '55000', message = 'payment_failure_action_required';
    end if;

    select attempt.* into v_attempt
    from public.followup_delivery_attempts attempt
    where attempt.id = p_attempt_id
      and attempt.action_id = p_action_id
      and attempt.lease_generation = p_lease_generation;
    if not found then
        raise exception using errcode = '55000',
            message = 'payment_failure_first_touch_attempt_not_found';
    end if;

    select record.* into v_record
    from public.payment_failure_first_touch_records record
    where record.delivery_attempt_id = p_attempt_id;
    if found then
        if v_record.variant is distinct from p_variant
           or v_record.coupon_code is distinct from p_coupon_code then
            raise exception using errcode = '55000',
                message = 'payment_failure_first_touch_record_conflict';
        end if;
        return query select 'already_recorded'::text;
        return;
    end if;

    -- Se registra antes del envio: un intento que ya arranco no se registra.
    if v_attempt.phase <> 'reserved' then
        raise exception using errcode = '55000',
            message = 'payment_failure_first_touch_attempt_not_reserved';
    end if;

    insert into public.payment_failure_first_touch_records (
        delivery_attempt_id, action_id, recovery_case_id, variant, coupon_code,
        created_at
    ) values (
        p_attempt_id, p_action_id, v_action.recovery_case_id, p_variant,
        p_coupon_code, p_now
    );

    return query select 'recorded'::text;
end;
$function$;

-- ---------------------------------------------------------------------------
-- 2. Si el seguimiento es B2 o Z, con la lease vigente del seguimiento.
-- ---------------------------------------------------------------------------
create function public.get_payment_failure_followup_context_v1(
    p_action_id uuid,
    p_worker_id text,
    p_lease_generation bigint,
    p_now timestamptz
)
returns table (
    followup_kind text,
    first_touch_variant text,
    payment_failure_count integer
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
    v_case public.recovery_cases%rowtype;
    v_variant text;
    v_coupon_code text;
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

    select rc.* into v_case
    from public.recovery_cases rc
    where rc.id = v_action.recovery_case_id;
    if v_action.action_type <> 'no_reply_review'
       or v_action.step_key is distinct from 'payment_failure_followup'
       or v_case.id is null
       or v_case.context ->> 'trigger_kind' is distinct from 'payment_failure' then
        raise exception using errcode = '55000',
            message = 'payment_failure_followup_action_required';
    end if;

    -- Lo que llevo el B1 que Chatwoot acepto. Sin registro (cayo a la
    -- plantilla de hoy, o salio antes de esta migracion), B2.
    select record.variant, record.coupon_code
      into v_variant, v_coupon_code
    from public.scheduled_actions first_touch
    join public.followup_delivery_attempts attempt
      on attempt.action_id = first_touch.id
    left join public.payment_failure_first_touch_records record
      on record.delivery_attempt_id = attempt.id
    where first_touch.recovery_case_id = v_case.id
      and first_touch.action_type = 'first_contact_review'
      and first_touch.anchor_type = 'payment_failure'
      and attempt.outcome = 'accepted_by_chatwoot'
    order by attempt.accepted_at desc, attempt.id
    limit 1;

    return query select
        case when v_coupon_code is not null then 'last_touch' else 'second_touch' end,
        v_variant,
        coalesce((v_case.context ->> 'payment_failure_count')::integer, 1);
end;
$function$;

-- ---------------------------------------------------------------------------
-- Permisos de lo nuevo: las dos entradas del bridge, solo service_role; la
-- tabla y su trigger, de nadie de afuera (la escriben y la leen las funciones
-- security definer).
-- ---------------------------------------------------------------------------
do $roles$
declare
    v_signature text;
    v_role text;
    v_entrypoints constant text[] := array[
        'public.record_payment_failure_first_touch_v1(uuid,uuid,text,bigint,text,text,timestamptz)',
        'public.get_payment_failure_followup_context_v1(uuid,text,bigint,timestamptz)'
    ];
    v_private constant text[] := array[
        'public.protect_payment_failure_first_touch_record()'
    ];
begin
    execute 'revoke all on table public.payment_failure_first_touch_records from public';
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format(
                'revoke all on table public.payment_failure_first_touch_records from %I',
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
    -- de la API ejecuta nada ni lee la tabla.
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
                message = 'payment_failure_followup_acl_unexpected',
                detail = v_signature;
        end if;
    end loop;
    if exists (
        select 1 from pg_roles
        where rolname in ('anon', 'authenticated', 'service_role')
          and has_table_privilege(
              rolname, 'public.payment_failure_first_touch_records', 'SELECT'
          )
    ) then
        raise exception using errcode = '55000',
            message = 'payment_failure_followup_acl_unexpected',
            detail = 'public.payment_failure_first_touch_records';
    end if;
end
$roles$;

commit;
