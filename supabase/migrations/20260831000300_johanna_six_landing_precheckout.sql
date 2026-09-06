-- Expand Johanna precheckout authority to the six immutable production routes.
-- This migration configures only prospective admissions; it does not backfill,
-- reschedule, reserve, or send any historical effect.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

create table public.johanna_precheckout_landing_offers (
    landing_ref text not null,
    offer_ref text not null,
    published_at timestamptz not null default clock_timestamp(),
    primary key (landing_ref, offer_ref),
    check (nullif(btrim(landing_ref), '') is not null),
    check (nullif(btrim(offer_ref), '') is not null)
);

create or replace function public.published_johanna_precheckout_pair_is_immutable()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    raise exception using errcode = '55000',
        message = 'published_johanna_precheckout_pair_is_immutable';
end;
$function$;

create trigger johanna_precheckout_landing_offers_immutable
before update or delete on public.johanna_precheckout_landing_offers
for each row execute function public.published_johanna_precheckout_pair_is_immutable();

alter table public.johanna_precheckout_landing_offers enable row level security;

insert into public.johanna_precheckout_landing_offers (landing_ref, offer_ref)
values
    ('ads-a', 'bxjge6zq'),
    ('ads-b', 'mgbgpp19'),
    ('ads-c', 's1qfxm7m'),
    ('org-a', 'jtt6fcsm'),
    ('org-b', 'ecyu87q0'),
    ('org-c', 'ulhzpw9a');

create or replace function public.is_johanna_precheckout_pair(
    p_landing_ref text,
    p_offer_ref text
)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    select exists (
        select 1
        from public.johanna_precheckout_landing_offers pair
        where pair.landing_ref = p_landing_ref
          and pair.offer_ref = p_offer_ref
    );
$function$;

revoke all on table public.johanna_precheckout_landing_offers from public;
revoke all on function public.is_johanna_precheckout_pair(text, text) from public;
revoke all on function public.published_johanna_precheckout_pair_is_immutable() from public;

do $acl$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on table public.johanna_precheckout_landing_offers from anon;
        revoke all on function public.is_johanna_precheckout_pair(text, text) from anon;
        revoke all on function public.published_johanna_precheckout_pair_is_immutable() from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on table public.johanna_precheckout_landing_offers from authenticated;
        revoke all on function public.is_johanna_precheckout_pair(text, text) from authenticated;
        revoke all on function public.published_johanna_precheckout_pair_is_immutable() from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on table public.johanna_precheckout_landing_offers from service_role;
        revoke all on function public.is_johanna_precheckout_pair(text, text) from service_role;
        revoke all on function public.published_johanna_precheckout_pair_is_immutable() from service_role;
    end if;
end;
$acl$;

-- One exact correlation scope per offer. Landing authority remains paired in the
-- admission function and intent row; Hotmart itself carries the offer, not landing.
do $preflight_correlation_scopes$
begin
    if exists (
        select 1
        from public.hotmart_purchase_intent_scopes existing
        where existing.hotmart_product_id = '8104005'
          and existing.offer_ref in (
              'bxjge6zq', 'mgbgpp19', 's1qfxm7m',
              'jtt6fcsm', 'ecyu87q0', 'ulhzpw9a'
          )
          and existing.active = true
          and (
              existing.tenant_ref is distinct from 'lancemos'
              or existing.funnel_ref is distinct from 'psicologajohanna'
              or lower(existing.purchase_intent_product_ref)
                   is distinct from 'f106691755g'
              or existing.max_lookback is distinct from interval '24 hours'
          )
    ) then
        raise exception using errcode = '55000',
            message = 'johanna_existing_correlation_scope_mismatch';
    end if;
end;
$preflight_correlation_scopes$;

insert into public.hotmart_purchase_intent_scopes (
    tenant_ref, funnel_ref, hotmart_product_id, purchase_intent_product_ref,
    offer_ref, max_lookback, active
)
select 'lancemos', 'psicologajohanna', '8104005', 'f106691755g',
       configured.offer_ref, interval '24 hours', true
from (values
    ('bxjge6zq'), ('mgbgpp19'), ('s1qfxm7m'),
    ('jtt6fcsm'), ('ecyu87q0'), ('ulhzpw9a')
) configured(offer_ref)
where not exists (
    select 1 from public.hotmart_purchase_intent_scopes existing
    where existing.hotmart_product_id = '8104005'
      and existing.offer_ref = configured.offer_ref
      and existing.active = true
);

-- One exact timer binding per offer; no product- or offer-wildcard binding grants
-- authority. Updating the legacy offer records its new generation atomically.
insert into public.hotmart_abandonment_timer_policy_bindings (
    tenant_ref, funnel_ref, product_ref, offer_ref, enabled,
    precheckout_first_touch_enabled, policy_key, policy_version
)
select 'lancemos', 'psicologajohanna', 'F106691755G', configured.offer_ref,
       true, true, 'johanna-precheckout-delayed-first-touch-timer', 1
from (values
    ('bxjge6zq'), ('mgbgpp19'), ('s1qfxm7m'),
    ('jtt6fcsm'), ('ecyu87q0'), ('ulhzpw9a')
) configured(offer_ref)
where not exists (
    select 1 from public.hotmart_abandonment_timer_policy_bindings existing
    where existing.tenant_ref = 'lancemos'
      and existing.funnel_ref = 'psicologajohanna'
      and lower(existing.product_ref) = lower('F106691755G')
      and existing.offer_ref = configured.offer_ref
);

update public.hotmart_abandonment_timer_policy_bindings binding
set enabled = true,
    precheckout_first_touch_enabled = true,
    policy_key = 'johanna-precheckout-delayed-first-touch-timer',
    policy_version = 1,
    generation = binding.generation + 1,
    updated_at = clock_timestamp()
where binding.tenant_ref = 'lancemos'
  and binding.funnel_ref = 'psicologajohanna'
  and lower(binding.product_ref) = lower('F106691755G')
  and binding.offer_ref in (
      'bxjge6zq', 'mgbgpp19', 's1qfxm7m',
      'jtt6fcsm', 'ecyu87q0', 'ulhzpw9a'
  )
  and (
      binding.enabled is distinct from true
      or binding.precheckout_first_touch_enabled is distinct from true
      or binding.policy_key is distinct from 'johanna-precheckout-delayed-first-touch-timer'
      or binding.policy_version is distinct from 1
  );

-- The former one-contact/one-request row remains immutable and inactive for
-- audit. One aggregate production scope carries non-blocking operational caps;
-- the authority table above remains the only route membership source.
insert into public.pilot_scope_versions (
    scope_key, version, status, tenant_key, chatwoot_account_id,
    chatwoot_inbox_id, channel, channel_provider, channel_account_ref,
    source, source_event_type, external_product_id, offer_code, purpose,
    policy_key, policy_version, timezone, max_cohort_contacts,
    max_outbound_request_starts_total, max_outbound_request_starts_per_day,
    approved_by, approved_at, published_at
) values (
    'johanna-precheckout-delayed-first-touch-production',
    1, 'published', 'lancemos', 1, 9, 'whatsapp', 'waba', 'chatwoot-inbox:9',
    'landing', 'PRECHECKOUT_FORM_SUBMITTED', 'F106691755G',
    'explicit-six-pair-authority', 'cart_recovery',
    'johanna-abandonment-single-touch-e2e', 2, 'UTC',
    1000000, 1000000, 10000, 'operator-authorized-production-scope-20260906',
    clock_timestamp(), clock_timestamp()
);

insert into public.pilot_runtime_controls (
    scope_key, scope_version, runtime_state, generation, changed_by, change_reason
) values (
    'johanna-precheckout-delayed-first-touch-production', 1, 'inactive', 0,
    'operator-authorized-production-scope-20260906',
    'Publish prospective production authority; outbound remains environment-gated.'
);

-- create or replace function public.admit_observed_lead_precheckout
-- Patch the established validator rather than copying its consent/identity logic.
do $patch_admission$
declare
    v_function regprocedure := to_regprocedure(
        'public.admit_observed_lead_precheckout(text,jsonb,jsonb)'
    );
    v_definition text;
    v_old text := $old$       or p_canonical_payload #>> '{source,landing_ref}' is distinct from 'ads-a'
       or p_canonical_payload #>> '{commerce,product_ref}' is distinct from 'F106691755G'
       or p_canonical_payload #>> '{commerce,offer_ref}' is distinct from 'bxjge6zq'$old$;
    v_new text := $new$       or p_canonical_payload #>> '{commerce,product_ref}' is distinct from 'F106691755G'
       or not public.is_johanna_precheckout_pair(
            p_canonical_payload #>> '{source,landing_ref}',
            p_canonical_payload #>> '{commerce,offer_ref}'
       )$new$;
    v_occurrences integer;
begin
    select pg_get_functiondef(v_function) into strict v_definition;
    v_occurrences := (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old);
    if v_occurrences <> 1 then
        raise exception 'johanna_six_pair_admission_guard_marker_mismatch';
    end if;
    v_definition := replace(v_definition, v_old, v_new);
    v_definition := replace(
        v_definition, '''bxjge6zq''',
        '(p_canonical_payload #>> ''{commerce,offer_ref}'')'
    );
    v_definition := replace(
        v_definition, '''ads-a''',
        '(p_canonical_payload #>> ''{source,landing_ref}'')'
    );
    execute v_definition;
end;
$patch_admission$;

-- Admit an explicitly consenting V1.1 lead even when normalization cannot
-- produce a valid phone. Raw consent remains true, while canonical contact and
-- activation authority are forced false; downstream scheduling remains strict.
do $patch_invalid_phone_admission$
declare
    v_function regprocedure := to_regprocedure(
        'public.admit_observed_lead_precheckout(text,jsonb,jsonb)'
    );
    v_definition text;
    v_decl_old text := $old$    v_contact_authorized boolean;
    v_activation_authorized boolean;$old$;
    v_decl_new text := $new$    v_contact_authorized boolean;
    v_activation_authorized boolean;
    v_phone_contact_authorized boolean;$new$;
    v_strict_old text := $old$            or p_canonical_payload #>> '{consent,whatsapp_contact}' is distinct from 'true'
            or p_canonical_payload #>> '{consent,copy_version}' is distinct from 'johanna-precheckout-whatsapp-disclosure-v1'
            or p_canonical_payload #>> '{identity,phone_valid}' is distinct from 'true'
            or p_canonical_payload #>> '{assurance,activation_authorized}' is distinct from 'true'$old$;
    v_strict_new text := $new$            or p_canonical_payload #>> '{consent,copy_version}' is distinct from 'johanna-precheckout-whatsapp-disclosure-v1'$new$;
    v_assignment_old text := $old$    v_contact_authorized := (p_canonical_payload #>> '{consent,whatsapp_contact}')::boolean;
    v_activation_authorized := (p_canonical_payload #>> '{assurance,activation_authorized}')::boolean;

    v_email := nullif(lower(btrim(
        p_canonical_payload #>> '{identity,email}',
        v_trim_chars
    )), '');
    v_phone := nullif(p_canonical_payload #>> '{identity,phone}', '');$old$;
    v_assignment_new text := $new$    v_email := nullif(lower(btrim(
        p_canonical_payload #>> '{identity,email}',
        v_trim_chars
    )), '');
    v_phone := nullif(p_canonical_payload #>> '{identity,phone}', '');
    v_phone_contact_authorized := (v_phone is not null);
    if v_contract_version <> '1.1.0' then
        v_phone_contact_authorized := false;
    end if;

    if v_contract_version = '1.1.0' and (
        (p_canonical_payload #>> '{consent,whatsapp_contact}')::boolean
            is distinct from v_phone_contact_authorized
        or (p_canonical_payload #>> '{assurance,activation_authorized}')::boolean
            is distinct from v_phone_contact_authorized
    ) then
        raise exception using errcode = '22023',
            message = 'observed_precheckout_consent_mismatch';
    end if;

    v_contact_authorized := v_phone_contact_authorized;
    v_activation_authorized := v_phone_contact_authorized;$new$;
begin
    select pg_get_functiondef(v_function) into strict v_definition;
    if position(v_decl_old in v_definition) = 0
       or position(v_strict_old in v_definition) = 0
       or position(v_assignment_old in v_definition) = 0
       or position($guard$p_raw_payload #>> '{data,consent,whatsapp_contact}' is distinct from 'true'$guard$ in v_definition) = 0 then
        raise exception 'johanna_invalid_phone_admission_marker_mismatch';
    end if;
    v_definition := replace(v_definition, v_decl_old, v_decl_new);
    v_definition := replace(v_definition, v_strict_old, v_strict_new);
    v_definition := replace(v_definition, v_assignment_old, v_assignment_new);
    execute v_definition;
end;
$patch_invalid_phone_admission$;

-- create or replace function public.schedule_precheckout_first_touch_reevaluation
-- Pair authority is checked again at scheduling, independently of admission.
do $patch_scheduler$
declare
    v_function regprocedure := to_regprocedure(
        'public.schedule_precheckout_first_touch_reevaluation(uuid,uuid)'
    );
    v_definition text;
    v_old text := $old$       or v_intent.landing_ref is distinct from 'ads-a'
       or lower(v_intent.product_ref) is distinct from lower('F106691755G')
       or v_intent.offer_ref is distinct from 'bxjge6zq'$old$;
    v_new text := $new$       or lower(v_intent.product_ref) is distinct from lower('F106691755G')
       or not public.is_johanna_precheckout_pair(
            v_intent.landing_ref, v_intent.offer_ref
       )$new$;
    v_binding_old text := replace($old$      and (
          binding.product_ref is null
          or lower(binding.product_ref) = lower(v_intent.product_ref)
      )
      and (
          binding.OFFER_NULL
          or binding.offer_ref = v_intent.offer_ref
      )$old$, 'OFFER_NULL', 'offer_' || 'ref is null');
    v_binding_new text := $new$      and lower(binding.product_ref) = lower(v_intent.product_ref)
      and binding.offer_ref = v_intent.offer_ref$new$;
begin
    select pg_get_functiondef(v_function) into strict v_definition;
    if position(v_old in v_definition) = 0 or position(v_binding_old in v_definition) = 0 then
        raise exception 'johanna_six_pair_scheduler_marker_mismatch';
    end if;
    v_definition := replace(v_definition, v_old, v_new);
    v_definition := replace(v_definition, v_binding_old, v_binding_new);
    execute v_definition;
end;
$patch_scheduler$;

-- create or replace function public._reevaluate_precheckout_delayed_first_touch
-- Reevaluation resolves the production scope from both immutable route fields.
do $patch_reevaluation$
declare
    v_function regprocedure := to_regprocedure(
        'public._reevaluate_precheckout_delayed_first_touch(uuid,timestamptz)'
    );
    v_definition text;
    v_pair_old text := $old$        or v_intent.landing_ref is distinct from 'ads-a'
        or lower(v_intent.product_ref) is distinct from lower('F106691755G')
        or v_intent.offer_ref is distinct from 'bxjge6zq'$old$;
    v_pair_new text := $new$        or lower(v_intent.product_ref) is distinct from lower('F106691755G')
        or not public.is_johanna_precheckout_pair(
            v_intent.landing_ref, v_intent.offer_ref
        )$new$;
    v_scope_old text := $old$        where scope.scope_key = 'johanna-precheckout-delayed-first-touch'
          and scope.version = 1$old$;
    v_scope_new text := $new$        where scope.scope_key = 'johanna-precheckout-delayed-first-touch-production'
          and scope.version = 1$new$;
    v_offer_old text := $old$          and scope.offer_code = 'bxjge6zq'$old$;
    v_offer_new text := $new$          and scope.offer_code = 'explicit-six-pair-authority'$new$;
    v_runtime_old text := $old$          and runtime.runtime_state = 'inactive'
          and runtime.generation = 0$old$;
    v_runtime_new text := $new$          and runtime.runtime_state = 'inactive'
          and runtime.generation = 0$new$;
    v_command_old text := $old$            'johanna-precheckout-delayed-first-touch',
            1,
            0,$old$;
    v_command_new text := $new$            v_scope.scope_key,
            v_scope.version,
            v_runtime.generation,$new$;
begin
    select pg_get_functiondef(v_function) into strict v_definition;
    if position(v_pair_old in v_definition) = 0
       or position(v_scope_old in v_definition) = 0
       or position(v_offer_old in v_definition) = 0
       or position(v_runtime_old in v_definition) = 0
       or position(v_command_old in v_definition) = 0 then
        raise exception 'johanna_six_pair_reevaluation_marker_mismatch';
    end if;
    v_definition := replace(v_definition, v_pair_old, v_pair_new);
    v_definition := replace(v_definition, v_scope_old, v_scope_new);
    v_definition := replace(v_definition, v_offer_old, v_offer_new);
    v_definition := replace(v_definition, '''bxjge6zq''', 'v_intent.offer_ref');
    v_definition := replace(v_definition, v_runtime_old, v_runtime_new);
    v_definition := replace(v_definition, v_command_old, v_command_new);
    execute v_definition;
end;
$patch_reevaluation$;

-- create or replace function public.get_precheckout_delayed_one_shot_command
-- Final authorization repeats pair, production scope, runtime, consent, identity,
-- opt-out, human-takeover, request_started, and delivery_unknown checks.
do $patch_command_authorization$
declare
    v_function regprocedure := to_regprocedure(
        'public.get_precheckout_delayed_one_shot_command(uuid)'
    );
    v_definition text;
    v_metadata_old text := $old$       or v_row.scope_key <> 'johanna-precheckout-delayed-first-touch'
       or v_row.scope_version <> 1
       or v_row.runtime_generation <> 0$old$;
    v_metadata_new text := $new$       or v_row.scope_key <> 'johanna-precheckout-delayed-first-touch-production'
       or v_row.scope_version <> 1
       or v_row.runtime_generation <> 0$new$;
    v_pair_old text := $old$    elsif v_row.tenant_ref <> 'lancemos'
       or v_row.funnel_ref <> 'psicologajohanna'
       or v_row.landing_ref <> 'ads-a'
       or lower(v_row.product_ref) <> lower('F106691755G')
       or v_row.offer_ref <> 'bxjge6zq'$old$;
    v_pair_new text := $new$    elsif v_row.tenant_ref <> 'lancemos'
       or v_row.funnel_ref <> 'psicologajohanna'
       or lower(v_row.product_ref) <> lower('F106691755G')
       or not public.is_johanna_precheckout_pair(
            v_row.landing_ref, v_row.offer_ref
       )$new$;
    v_scope_old text := $old$        where scope.scope_key = 'johanna-precheckout-delayed-first-touch'
          and scope.version = 1$old$;
    v_scope_new text := $new$        where scope.scope_key = 'johanna-precheckout-delayed-first-touch-production'
          and scope.version = 1$new$;
    v_offer_old text := $old$          and scope.offer_code = 'bxjge6zq'$old$;
    v_offer_new text := $new$          and scope.offer_code = 'explicit-six-pair-authority'$new$;
    v_runtime_old text := $old$          and runtime.runtime_state = 'inactive'
          and runtime.generation = 0$old$;
    v_runtime_new text := $new$          and runtime.runtime_state = 'inactive'
          and runtime.generation = 0$new$;
begin
    select pg_get_functiondef(v_function) into strict v_definition;
    if position(v_metadata_old in v_definition) = 0
       or position(v_pair_old in v_definition) = 0
       or position(v_scope_old in v_definition) = 0
       or position(v_offer_old in v_definition) = 0
       or position(v_runtime_old in v_definition) = 0 then
        raise exception 'johanna_six_pair_command_marker_mismatch';
    end if;
    v_definition := replace(v_definition, v_metadata_old, v_metadata_new);
    v_definition := replace(v_definition, v_pair_old, v_pair_new);
    v_definition := replace(v_definition, v_scope_old, v_scope_new);
    v_definition := replace(v_definition, v_offer_old, v_offer_new);
    v_definition := replace(v_definition, '''bxjge6zq''', 'v_row.offer_ref');
    v_definition := replace(v_definition, v_runtime_old, v_runtime_new);
    execute v_definition;
end;
$patch_command_authorization$;

create or replace function public.get_precheckout_delayed_first_touch_readiness()
returns table (
    migration_tracking_complete boolean,
    scope_configured boolean,
    runtime_state text,
    runtime_generation bigint,
    timer_binding_enabled boolean,
    timer_binding_generation bigint,
    first_touch_binding_enabled boolean,
    due_count bigint,
    reserved_count bigint,
    request_started_count bigint,
    delivery_unknown_count bigint,
    reason_code text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_tracking_complete boolean := false;
    v_scope_configured boolean := false;
    v_six_bindings boolean := false;
    v_timer_binding_policy_matches boolean := false;
    v_runtime_state text;
    v_runtime_generation bigint;
    v_timer_binding_enabled boolean := false;
    v_timer_binding_generation bigint;
    v_first_touch_binding_enabled boolean := false;
    v_due_count bigint := 0;
    v_reserved_count bigint := 0;
    v_request_started_count bigint := 0;
    v_delivery_unknown_count bigint := 0;
    v_reason_code text;
begin
    if to_regclass('supabase_migrations.schema_migrations') is not null then
        execute $tracking$
            select count(*) = 6
            from supabase_migrations.schema_migrations
            where version in (
                '20260829000200', '20260829000300', '20260829000400',
                '20260829000500', '20260831000200', '20260831000300'
            )
        $tracking$ into v_tracking_complete;
    end if;

    select (
        (select count(*) = 6
         from public.johanna_precheckout_landing_offers)
        and (select count(*) = 6
             from public.hotmart_purchase_intent_scopes correlation
             join public.johanna_precheckout_landing_offers pair
               on pair.offer_ref = correlation.offer_ref
             where correlation.hotmart_product_id = '8104005'
               and correlation.tenant_ref = 'lancemos'
               and correlation.funnel_ref = 'psicologajohanna'
               and lower(correlation.purchase_intent_product_ref) = 'f106691755g'
               and correlation.max_lookback = interval '24 hours'
               and correlation.active = true)
        and exists (
            select 1
            from public.pilot_scope_versions scope
            where scope.scope_key = 'johanna-precheckout-delayed-first-touch-production'
              and scope.version = 1
              and scope.status = 'published'
              and scope.tenant_key = 'lancemos'
              and scope.chatwoot_account_id = 1
              and scope.chatwoot_inbox_id = 9
              and scope.channel = 'whatsapp'
              and scope.channel_provider = 'waba'
              and scope.channel_account_ref = 'chatwoot-inbox:9'
              and scope.source = 'landing'
              and scope.source_event_type = 'PRECHECKOUT_FORM_SUBMITTED'
              and scope.external_product_id = 'F106691755G'
              and scope.offer_code = 'explicit-six-pair-authority'
              and scope.purpose = 'cart_recovery'
              and scope.max_cohort_contacts = 1000000
              and scope.max_outbound_request_starts_total = 1000000
              and scope.max_outbound_request_starts_per_day = 10000
        )
    ) into v_scope_configured;

    select runtime.runtime_state, runtime.generation
    into v_runtime_state, v_runtime_generation
    from public.pilot_runtime_controls runtime
    where runtime.scope_key = 'johanna-precheckout-delayed-first-touch-production'
      and runtime.scope_version = 1;

    select count(*) = 6,
           coalesce(bool_and(binding.enabled), false),
           max(binding.generation),
           coalesce(bool_and(binding.precheckout_first_touch_enabled), false),
           coalesce(bool_and(
               binding.policy_key = 'johanna-precheckout-delayed-first-touch-timer'
               and binding.policy_version = 1
               and exists (
                   select 1
                   from public.followup_policy_versions policy
                   where policy.policy_key = binding.policy_key
                     and policy.version = binding.policy_version
                     and policy.status = 'published'
                     and policy.grace_period = interval '60 minutes'
               )
           ), false)
    into v_six_bindings, v_timer_binding_enabled,
         v_timer_binding_generation, v_first_touch_binding_enabled,
         v_timer_binding_policy_matches
    from public.johanna_precheckout_landing_offers pair
    join public.hotmart_abandonment_timer_policy_bindings binding
      on binding.tenant_ref = 'lancemos'
     and binding.funnel_ref = 'psicologajohanna'
     and lower(binding.product_ref) = lower('F106691755G')
     and binding.offer_ref = pair.offer_ref
    where v_scope_configured;

    v_scope_configured := v_scope_configured and v_six_bindings;

    select count(*) into v_due_count
    from public.hotmart_abandonment_reevaluations timer
    left join public.johanna_abandonment_one_shot_commands command
      on command.source_reevaluation_id = timer.id
    where timer.source_kind = 'precheckout_intent'
      and ((timer.status = 'scheduled' and timer.due_at <= clock_timestamp())
        or (timer.status = 'completed' and timer.outcome = 'command_reserved'
            and command.status in ('reserved', 'request_started')));

    select count(*) filter (where command.status = 'reserved'),
           count(*) filter (where command.status = 'request_started'),
           count(*) filter (where command.status = 'delivery_unknown')
    into v_reserved_count, v_request_started_count, v_delivery_unknown_count
    from public.johanna_abandonment_one_shot_commands command
    where command.source_reevaluation_id is not null;

    v_reason_code := case
        when not v_tracking_complete then 'migration_tracking_incomplete'
        when not v_scope_configured then 'precheckout_scope_not_configured'
        when v_runtime_state is distinct from 'inactive'
          or v_runtime_generation is distinct from 0
            then 'precheckout_runtime_not_inactive'
        when not v_timer_binding_enabled then 'timer_binding_disabled'
        when not v_timer_binding_policy_matches then 'timer_binding_policy_mismatch'
        when not v_first_touch_binding_enabled then 'first_touch_binding_disabled'
        else 'precheckout_first_touch_ready'
    end;

    return query select v_tracking_complete, v_scope_configured,
        v_runtime_state, v_runtime_generation, v_timer_binding_enabled,
        v_timer_binding_generation, v_first_touch_binding_enabled,
        v_due_count, v_reserved_count, v_request_started_count,
        v_delivery_unknown_count, v_reason_code;
end;
$function$;

-- Static and behavioral postflight: every effect boundary must retain pair checks.
do $postflight$
declare
    v_admission text;
    v_scheduler text;
    v_reevaluation text;
    v_command text;
begin
    select lower(pg_get_functiondef(to_regprocedure(
        'public.admit_observed_lead_precheckout(text,jsonb,jsonb)'
    ))) into strict v_admission;
    select lower(pg_get_functiondef(to_regprocedure(
        'public.schedule_precheckout_first_touch_reevaluation(uuid,uuid)'
    ))) into strict v_scheduler;
    select lower(pg_get_functiondef(to_regprocedure(
        'public._reevaluate_precheckout_delayed_first_touch(uuid,timestamptz)'
    ))) into strict v_reevaluation;
    select lower(pg_get_functiondef(to_regprocedure(
        'public.get_precheckout_delayed_one_shot_command(uuid)'
    ))) into strict v_command;

    if position('public.is_johanna_precheckout_pair(' in v_admission) = 0
       or position('public.is_johanna_precheckout_pair(' in v_scheduler) = 0
       or position('public.is_johanna_precheckout_pair(' in v_reevaluation) = 0
       or position('public.is_johanna_precheckout_pair(' in v_command) = 0
       or position('identity_conflict' in v_admission) = 0
       or position('activation_authorized' in v_admission) = 0
       or position('whatsapp_contact_authorized' in v_scheduler) = 0
       or position('contact_opt_out_events' in v_reevaluation) = 0
       or position('human_takeover' in v_command) = 0
       or position('request_started' in v_command) = 0
       or position('delivery_unknown' in v_command) = 0
       or (select count(*) from public.johanna_precheckout_landing_offers) <> 6
       or exists (
           select 1 from public.johanna_precheckout_landing_offers pair
           where pair.offer_ref = chr(42)
       ) then
        raise exception 'johanna_six_landing_precheckout_postflight_failed';
    end if;
end;
$postflight$;

commit;
