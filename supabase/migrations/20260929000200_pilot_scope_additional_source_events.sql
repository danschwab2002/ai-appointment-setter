-- Un scope del piloto acepta carrito y pago fallido (F2c-3).
--
-- El bridge arma una sola frontera del piloto con una sola
-- `LANCEMOS_PILOT_SCOPE_KEY` y una sola politica de seguimiento para todos sus
-- caminos, pero cada fila de `pilot_scope_versions` aceptaba un unico
-- `source_event_type`. Una instancia que recupera carritos
-- (`PURCHASE_OUT_OF_SHOPPING_CART`) y pagos fallidos (`PURCHASE_CANCELED`)
-- perdia uno de los dos caminos en `pilot_source_event_mismatch`. Esta
-- migracion:
--
-- 1. suma `additional_source_event_types` al scope: los tipos de evento de
--    Hotmart que el scope acepta ademas del suyo (`source_event_type` sigue
--    siendo el principal);
-- 2. `evaluate_lancemos_pilot_scope` y `authorize_lancemos_pilot_request_start`
--    aceptan cualquier tipo del conjunto;
-- 3. `get_lancemos_pilot_runtime_status` exige que el scope acepte carritos,
--    sea como tipo principal o en el conjunto.
--
-- La politica, los topes de envio y la cohorte siguen siendo del scope y pasan
-- a ser compartidos por los tipos que acepta. Las funciones se copian de su
-- definicion vigente (20260929000100 y 20260810000300) y solo cambia la
-- comparacion del tipo de evento; `create or replace` conserva los grants. Los
-- scopes existentes quedan con el conjunto vacio: Johanna no cambia. No siembra
-- filas de ningun cliente.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

alter table public.pilot_scope_versions
    add column additional_source_event_types text[] not null default '{}'::text[];

alter table public.pilot_scope_versions
    add constraint pilot_scope_versions_additional_source_event_types_shape
    check (
        cardinality(additional_source_event_types) = 0
        or (
            source = 'hotmart'
            and cardinality(additional_source_event_types) = 1
            and array_position(additional_source_event_types, null) is null
            and array_position(additional_source_event_types, source_event_type) is null
            and additional_source_event_types
                <@ array['PURCHASE_OUT_OF_SHOPPING_CART', 'PURCHASE_CANCELED']::text[]
        )
    );

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

    if not exists (
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
    );

    return query select true, 'pilot_request_start_authorized'::text,
        v_control.generation, v_authorization_id, false;
end;
$function$;

create or replace function public.get_lancemos_pilot_runtime_status(
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
           v_scope.source_event_type <> 'PURCHASE_OUT_OF_SHOPPING_CART'
           and 'PURCHASE_OUT_OF_SHOPPING_CART' <> all(v_scope.additional_source_event_types)
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

commit;
