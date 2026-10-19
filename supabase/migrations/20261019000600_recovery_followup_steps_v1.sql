-- Migration: los pasos siguientes con plantilla aprobada y cupon (A2 y Z del
-- carrito de ATT1, y lo que reusan B2 y Z del pago rechazado).
--
-- Contrato: docs/contracts/portable-cart-followups-v1.md.
--
-- Por que. El motor ya agenda el paso siguiente de una politica de varios
-- pasos (_finalize_followup_delivery_attempt, 20260813000100), y la ficha lo
-- estreno con sus recordatorios (20261019000400). Esa migracion reconoce sus
-- pasos por la secuencia (followup_sequences.reason = 'payment_slip'). El
-- carrito no puede hacer lo mismo: su secuencia (cart_abandonment) es la de
-- Johanna y la del legado. Por eso aca un paso lo declara la politica, con dos
-- marcas en su JSON de steps:
--
--   "approved_template": true  el paso sale con una plantilla aprobada, fuera
--                              de la ventana de 24 h;
--   "coupon_ordinal": 1 | 2    el paso lleva un cupon personal con ese ordinal
--                              y nuestro link.
--
-- Ninguna politica publicada hoy tiene esas marcas: nada cambia hasta que la
-- instancia publique una que las use.
--
-- Que cambia:
--   1. _recovery_followup_step(action): el paso de la politica de un
--      no_reply_review, o null. security definer y solo service_role, porque
--      lo llaman funciones security invoker que corre service_role.
--   2. reevaluate_followup_action:
--      a. un paso con "approved_template" no se pausa por la ventana de 24 h
--         cerrada (p_chatwoot_can_reply = false) ni por una conversacion
--         resuelta, igual que el recordatorio de la ficha. Despues de A1 (una
--         plantilla sin respuesta) la ventana esta cerrada: sin esto A2 queda
--         en pause para siempre;
--      b. un paso adelantado por la persona (la renovacion, punto 6) no se
--         cancela por su respuesta (prospect_replied): la respuesta es el
--         pedido.
--   3. admit_portable_inbound_commercial_case_v1: un paso con
--      "approved_template" que espera no frena la adopcion de la respuesta a
--      A1 o a A2. Si la persona contesto, la reevaluacion lo cancela.
--   4. claim_due_recovery_coupons_v1 siembra el cupon de un paso con
--      "coupon_ordinal", con la misma anticipacion (p_lead) y la misma
--      vigencia (p_validity) que el de A1.
--   5. reserve_recovery_checkout_issuance_v1 emite nuestro link tambien para
--      un paso con "coupon_ordinal".
--   6. request_recovery_renewal_v1 (nueva): la persona pide la renovacion
--      desde la conversacion (en el carrito, el boton «Quiero mi 10 %» de A2).
--      Adelanta el paso pendiente con "coupon_ordinal" de esa conversacion y
--      lo deja anotado en recovery_renewal_requests (nueva) para el punto 2b.
--   7. plan_cart_recovery: un carrito nuevo de la misma persona durante la
--      secuencia no adelanta un paso con "approved_template" (hoy hace
--      due_at = least(due_at, now()), y A2 o Z saldrian en el acto).
--
-- Como. Cada funcion existente se redefine desde su definicion viva
-- (pg_get_functiondef), reemplazando un texto exacto que tiene que aparecer
-- una sola vez: si otra migracion lo cambio, esta aborta con 55000 en vez de
-- pisarlo. La firma, el dueno, security definer o invoker, el search_path y
-- los permisos no cambian (se verifican al final).

begin;

-- ---------------------------------------------------------------------------
-- 1. El paso de la politica de una accion.
-- ---------------------------------------------------------------------------
create function public._recovery_followup_step(p_action_id uuid)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    -- recovery_followup_steps: el paso de la politica de un no_reply_review
    select policy_step
    from public.scheduled_actions action
    join public.followup_policy_versions policy
      on policy.policy_key = action.policy_key
     and policy.version = action.policy_version
    cross join lateral jsonb_array_elements(policy.steps) as policy_step
    where action.id = p_action_id
      and action.action_type = 'no_reply_review'
      and policy_step ->> 'step_key' = action.step_key
    limit 1;
$function$;

-- ---------------------------------------------------------------------------
-- 6a. Los pedidos de renovacion.
-- ---------------------------------------------------------------------------
create table public.recovery_renewal_requests (
    action_id uuid primary key references public.scheduled_actions (id),
    recovery_case_id uuid not null references public.recovery_cases (id),
    chatwoot_account_id bigint not null check (chatwoot_account_id >= 1),
    chatwoot_conversation_id bigint not null check (chatwoot_conversation_id >= 1),
    chatwoot_message_id bigint not null check (chatwoot_message_id >= 1),
    external_user_id text not null check (external_user_id ~ '^[0-9]+$'),
    occurred_at timestamptz not null,
    created_at timestamptz not null default now(),
    unique (chatwoot_account_id, chatwoot_message_id)
);

alter table public.recovery_renewal_requests enable row level security;

create function public._recovery_renewal_requested(p_action_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    -- recovery_followup_steps: la persona pidio adelantar este paso
    select exists (
        select 1
        from public.recovery_renewal_requests renewal
        where renewal.action_id = p_action_id
    );
$function$;

-- ---------------------------------------------------------------------------
-- 6b. El pedido.
-- ---------------------------------------------------------------------------
create function public.request_recovery_renewal_v1(
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_chatwoot_conversation_id bigint,
    p_chatwoot_message_id bigint,
    p_external_user_id text,
    p_occurred_at timestamptz
)
returns table (outcome text)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_action public.scheduled_actions%rowtype;
    v_case_id uuid;
begin
    if p_chatwoot_account_id is null or p_chatwoot_account_id < 1
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id < 1
       or p_chatwoot_conversation_id is null or p_chatwoot_conversation_id < 1
       or p_chatwoot_message_id is null or p_chatwoot_message_id < 1
       or p_external_user_id is null or p_external_user_id !~ '^[0-9]+$'
       or p_occurred_at is null then
        raise exception using errcode = '22023', message = 'invalid_recovery_renewal_input';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        concat_ws(':', 'recovery-renewal', p_chatwoot_account_id,
                  p_chatwoot_conversation_id), 0
    ));

    -- El mismo mensaje otra vez (un reintento del webhook).
    if exists (
        select 1
        from public.recovery_renewal_requests renewal
        where renewal.chatwoot_account_id = p_chatwoot_account_id
          and renewal.chatwoot_message_id = p_chatwoot_message_id
    ) then
        return query select 'already_requested'::text;
        return;
    end if;

    -- El paso pendiente con cupon de esa conversacion y esa persona.
    select action.*
      into v_action
    from public.scheduled_actions action
    join public.recovery_cases recovery
      on recovery.id = action.recovery_case_id
    join public.conversations conversation
      on conversation.id = recovery.conversation_id
    join public.channel_identities identity
      on identity.id = recovery.selected_channel_identity_id
    where conversation.commercial_context ->> 'chatwoot_conversation_id'
              = p_chatwoot_conversation_id::text
      and identity.channel = 'whatsapp'
      and identity.account_id = 'chatwoot:' || p_chatwoot_account_id::text
      and identity.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text
      and identity.external_user_id = any(
          coalesce(public._whatsapp_phone_variants(p_external_user_id), array[p_external_user_id])
      )
      and recovery.status in ('grace_period', 'active')
      and recovery.purchase_event_id is null
      and action.action_type = 'no_reply_review'
      and action.status in ('pending', 'deferred', 'retryable_failed')
      and action.expires_at > now()
      and coalesce(public._recovery_followup_step(action.id) ->> 'coupon_ordinal', '')
              in ('1', '2')
    order by action.due_at, action.id
    limit 1
    for update of action;

    if v_action.id is null then
        return query select 'no_pending_last_touch'::text;
        return;
    end if;
    v_case_id := v_action.recovery_case_id;

    -- Otro toque del boton sobre el mismo paso ya adelantado.
    if exists (
        select 1
        from public.recovery_renewal_requests renewal
        where renewal.action_id = v_action.id
    ) then
        return query select 'already_requested'::text;
        return;
    end if;

    insert into public.recovery_renewal_requests (
        action_id, recovery_case_id, chatwoot_account_id,
        chatwoot_conversation_id, chatwoot_message_id, external_user_id,
        occurred_at
    ) values (
        v_action.id, v_case_id, p_chatwoot_account_id,
        p_chatwoot_conversation_id, p_chatwoot_message_id, p_external_user_id,
        p_occurred_at
    );

    update public.scheduled_actions action
    set due_at = least(action.due_at, now()),
        next_attempt_at = null
    where action.id = v_action.id;

    return query select 'advanced'::text;
end;
$function$;

-- ---------------------------------------------------------------------------
-- 2. a 5. y 7. Las funciones existentes.
-- ---------------------------------------------------------------------------
do $steps$
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
             'public.reevaluate_followup_action(uuid,text,bigint,timestamptz,boolean,text,text,timestamptz,text,boolean,boolean,boolean,boolean,boolean)',
             $old$    elsif p_chatwoot_inbound_after_anchor then
        v_decision := 'cancel';
        v_reason := 'prospect_replied';
$old$,
             $new$    elsif p_chatwoot_inbound_after_anchor
       -- recovery_followup_steps (20261019000600): la respuesta que pidio la
       -- renovacion adelanto este paso; no lo cancela.
       and not public._recovery_renewal_requested(v_action.id) then
        v_decision := 'cancel';
        v_reason := 'prospect_replied';
$new$),
            (2,
             'public.reevaluate_followup_action(uuid,text,bigint,timestamptz,boolean,text,text,timestamptz,text,boolean,boolean,boolean,boolean,boolean)',
             $old$                     and slip_sequence.reason = 'payment_slip'
               )
           )
       ) then
$old$,
             $new$                     and slip_sequence.reason = 'payment_slip'
               )
           )
           -- recovery_followup_steps (20261019000600): ni un paso que la
           -- politica declara con plantilla aprobada (A2 y Z del carrito).
           and not (
               v_action.action_type = 'no_reply_review'
               and coalesce(v_step ->> 'approved_template', '') = 'true'
           )
       ) then
$new$),
            (3,
             'public.admit_portable_inbound_commercial_case_v1(text,integer,bigint,text)',
             $old$                             -- portable_payment_slip_reminders: end
$old$,
             $new$                             -- portable_payment_slip_reminders: end
                             -- recovery_followup_steps: begin
                             -- Un paso con plantilla aprobada que espera
                             -- (20261019000600) no frena que el agente tome
                             -- la respuesta: si la persona contesto, la
                             -- reevaluacion lo cancela.
                             and not (
                                 action.action_type = 'no_reply_review'
                                 and action.status in (
                                     'pending', 'deferred', 'retryable_failed'
                                 )
                                 and coalesce(
                                     public._recovery_followup_step(action.id)
                                         ->> 'approved_template',
                                     ''
                                 ) = 'true'
                             )
                             -- recovery_followup_steps: end
$new$),
            (4,
             'public.claim_due_recovery_coupons_v1(timestamptz,text,integer,text,interval,interval,numeric,integer)',
             $old$    order by due.due_at
    limit p_limit
    on conflict do nothing;
$old$,
             $new$    order by due.due_at
    limit p_limit
    on conflict do nothing;

    -- recovery_followup_steps (20261019000600): el cupon de un paso siguiente
    -- que lo declara ("coupon_ordinal" en la politica), con la misma
    -- anticipacion y la misma vigencia.
    insert into public.recovery_coupons as coupon (
        recovery_case_id, ordinal, code, external_product_id, discount,
        expires_at, status, created_at, updated_at
    )
    select step_due.case_id,
           step_due.ordinal,
           v_prefix || upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 12)),
           step_due.product_id,
           p_discount,
           step_due.due_at + p_validity,
           'reserved',
           p_now,
           p_now
    from (
        select distinct on (rc.id)
               rc.id as case_id,
               rc.external_product_id as product_id,
               sa.due_at,
               (step.policy_step ->> 'coupon_ordinal')::smallint as ordinal
        from public.recovery_cases rc
        join public.contacts ct on ct.id = rc.contact_id
        join public.scheduled_actions sa on sa.recovery_case_id = rc.id
        cross join lateral (
            select public._recovery_followup_step(sa.id) as policy_step
        ) step
        where rc.status in ('grace_period', 'active')
          and rc.purchase_event_id is null
          and ct.contact_permission not in ('opted_out', 'blocked', 'restricted')
          and ct.lifecycle_status <> 'do_not_contact'
          and sa.action_type = 'no_reply_review'
          and sa.status in ('pending', 'deferred', 'retryable_failed')
          and sa.due_at - p_lead <= p_now
          and sa.expires_at > p_now
          and sa.due_at + p_validity > p_now
          and step.policy_step ->> 'coupon_ordinal' in ('1', '2')
          and not exists (
              select 1 from public.recovery_coupons existing
              where existing.recovery_case_id = rc.id
                and existing.ordinal = (step.policy_step ->> 'coupon_ordinal')::smallint
          )
        order by rc.id, sa.due_at
    ) step_due
    order by step_due.due_at
    limit p_limit
    on conflict do nothing;
$new$),
            (5,
             'public.reserve_recovery_checkout_issuance_v1(uuid,text,bigint,uuid,text,timestamptz)',
             $old$    if v_action.action_type <> 'first_contact_review'
       -- payment_failure_variants: tambien el primer toque del pago rechazado.
       or v_action.anchor_type is null
       or v_action.anchor_type not in ('cart_abandonment', 'payment_failure')
$old$,
             $new$    if (
           v_action.action_type <> 'first_contact_review'
           -- payment_failure_variants: tambien el primer toque del pago rechazado.
           or v_action.anchor_type is null
           or v_action.anchor_type not in ('cart_abandonment', 'payment_failure')
       )
       -- recovery_followup_steps (20261019000600): y un paso siguiente que la
       -- politica declara con su cupon.
       and coalesce(
           public._recovery_followup_step(v_action.id) ->> 'coupon_ordinal', ''
       ) not in ('1', '2')
$new$),
            (6,
             'public.plan_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz)',
             $old$                    when v_sequence_status = 'active' then least(due_at, now())
$old$,
             $new$                    when v_sequence_status = 'active'
                         -- recovery_followup_steps (20261019000600): un carrito
                         -- nuevo no adelanta un paso con plantilla aprobada.
                         and coalesce(
                             public._recovery_followup_step(scheduled_actions.id)
                                 ->> 'approved_template',
                             ''
                         ) <> 'true'
                        then least(due_at, now())
$new$),
            (7,
             'public.plan_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz)',
             $old$                    when v_sequence_status = 'active' then null
$old$,
             $new$                    when v_sequence_status = 'active'
                         and coalesce(
                             public._recovery_followup_step(scheduled_actions.id)
                                 ->> 'approved_template',
                             ''
                         ) <> 'true'
                        then null
$new$)
        ) as spec(ord, signature, old_text, new_text)
        order by spec.ord
    loop
        if to_regprocedure(v_spec.signature) is null then
            raise exception using errcode = '55000',
                message = 'recovery_followup_steps_function_missing',
                detail = v_spec.signature;
        end if;
        select pg_get_functiondef(v_spec.signature::regprocedure)
          into v_definition;
        if position(v_spec.new_text in v_definition) > 0 then
            raise exception using errcode = '55000',
                message = 'recovery_followup_steps_already_applied',
                detail = format('%s (%s)', v_spec.signature, v_spec.ord);
        end if;
        v_found := (
            length(v_definition)
            - length(replace(v_definition, v_spec.old_text, ''))
        ) / length(v_spec.old_text);
        if v_found <> 1 then
            raise exception using errcode = '55000',
                message = 'unexpected_recovery_followup_steps_definition',
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
                message = 'recovery_followup_steps_not_applied',
                detail = format('%s (%s)', v_spec.signature, v_spec.ord);
        end if;
        -- Los permisos que create or replace conserva, iguales que antes.
        select string_agg(rolname || ':' || has_function_privilege(rolname, v_spec.signature, 'EXECUTE'), ',' order by rolname)
          into v_grants_after
        from pg_roles
        where rolname in ('anon', 'authenticated', 'service_role');
        if v_grants_after is distinct from v_grants_before then
            raise exception using errcode = '55000',
                message = 'recovery_followup_steps_grants_changed',
                detail = v_spec.signature;
        end if;
    end loop;

end;
$steps$;

-- ---------------------------------------------------------------------------
-- Permisos de lo nuevo: solo service_role ejecuta; la tabla no la lee nadie
-- de afuera (la leen las funciones security definer).
-- ---------------------------------------------------------------------------
revoke all on table public.recovery_renewal_requests from public;
revoke all on function public._recovery_followup_step(uuid) from public;
revoke all on function public._recovery_renewal_requested(uuid) from public;
revoke all on function public.request_recovery_renewal_v1(bigint, bigint, bigint, bigint, text, timestamptz) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'revoke all on table public.recovery_renewal_requests from anon';
        execute 'revoke all on function public._recovery_followup_step(uuid) from anon';
        execute 'revoke all on function public._recovery_renewal_requested(uuid) from anon';
        execute 'revoke all on function public.request_recovery_renewal_v1(bigint, bigint, bigint, bigint, text, timestamptz) from anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'revoke all on table public.recovery_renewal_requests from authenticated';
        execute 'revoke all on function public._recovery_followup_step(uuid) from authenticated';
        execute 'revoke all on function public._recovery_renewal_requested(uuid) from authenticated';
        execute 'revoke all on function public.request_recovery_renewal_v1(bigint, bigint, bigint, bigint, text, timestamptz) from authenticated';
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'revoke all on table public.recovery_renewal_requests from service_role';
        execute 'grant execute on function public._recovery_followup_step(uuid) to service_role';
        execute 'grant execute on function public._recovery_renewal_requested(uuid) to service_role';
        execute 'grant execute on function public.request_recovery_renewal_v1(bigint, bigint, bigint, bigint, text, timestamptz) to service_role';
    end if;
end;
$roles$;

commit;
