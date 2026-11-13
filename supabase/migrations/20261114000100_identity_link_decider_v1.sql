-- identity-link-decider-v1: el correlador que decide solo (E-536, bloque A).
-- Contrato: docs/contracts/identity-link-decider-v1.md. Diseno: «El correlador
-- que decide solo» (E-535, 2026-11-13), con las decisiones de Dan del 13/11
-- (D1 la regla primero, D3 el +1 detras de su flag, D5 la pregunta, D6 sin
-- semana de sombra).
--
-- Por que. Cuando una persona deja un mail o un telefono en el formulario y
-- compra con otro, el correlador marca conflict / email_phone_conflict, la
-- intencion queda en identity_conflict y el carrito no le escribe a nadie. La
-- tarjeta COR-012 por Slack no sale nunca. Esta migracion le da a una pieza
-- nueva del bridge (src/bridge/identity_link.py) lo que necesita para resolver
-- cada conflicto de carrito sola, en una de tres ramas: ata, pregunta o nada.
--
-- La invariante (D2): solo se escribe al telefono que dio el permiso. El
-- bridge decide si es la misma persona; a que telefono se escribe lo fija la
-- base al atar (destination_source) y lo vuelve a exigir el chequeo del
-- permiso antes de cada envio.
--
-- Lo que trae:
--   1. Las tablas: identity_link_controls (la segunda llave, por tenant y
--      funnel), identity_link_cases (la cola, con su lease),
--      identity_link_decisions, identity_link_effects, identity_link_revocations,
--      identity_link_notices e identity_link_contact_rebinds (append-only; un
--      trigger rechaza UPDATE y DELETE). Ninguna guarda un mail ni un nombre;
--      el unico telefono es el anterior del contacto que la atadura cambio
--      (identity_link_contact_rebinds), para restaurarlo al revocar, y ninguna
--      lectura ni aviso lo devuelve.
--   2. Los helpers: _identity_text_similarity_pct (pares de letras, inmutable:
--      la base no tiene pg_trgm ni fuzzystrmatch), _identity_name_normalized,
--      _identity_phone_country, _identity_link_purchase_seen.
--   3. La evidencia, get_identity_link_evidence_v1: compara los datos personales
--      adentro de la base y devuelve solo rasgos y el freno (guard_reason).
--   4. La cola: claim_identity_link_cases_v1, complete_identity_link_decision_v1
--      (decision, efecto y reencolado en una transaccion, con la regla
--      re-verificada en la base), release_identity_link_case_v1,
--      requeue_identity_linked_events_v1 y revoke_identity_link_v1.
--   5. La atadura vigente: _identity_link_active_for_intent,
--      _identity_link_destination_for_intent, _cart_event_intent_v1 (la
--      intencion resuelta del evento, o la atada vigente) y, para el bridge,
--      get_identity_link_destination_v1 y bind_identity_link_contact_v1 (deja
--      el contacto del evento con el telefono de la atadura: el envio sale a
--      contacts.phone).
--   6. Los avisos al canal de alertas de la aliada: read_identity_link_notices_v1
--      y mark_identity_link_notice_v1 (sin telefonos ni mails: la rama, la regla
--      y el numero de conversacion).
--   7. Los reemplazos sobre la definicion VIGENTE (el metodo de 20261007000100,
--      20261021000200 y 20261022000100: un texto exacto que aparece una sola vez,
--      la marca que no puede estar, el duenio que es quien aplica, y la marca
--      presente despues). Las vigentes, con el #455 aplicado:
--        plan_lancemos_pilot_cart_recovery ..... 20260930000300 + 20261021000200
--                                                + 20261022000100
--        _portable_cart_audience_checks ........ 20261022000100
--        _flow_engine_native_cart_audience ..... 20261028000200
--        _portable_consented_intent_reason ..... 20261001000100
--      Los tres primeros leen la intencion del carrito con _cart_event_intent_v1
--      en vez de la correlacion resuelta. El chequeo del permiso deja de cortar
--      por identity_conflict y por activation_authorized = false SOLO con una
--      atadura vigente de esa intencion; con la atadura del +1 (D3) acepta como
--      destino el telefono del checkout (52 + los mismos 10 digitos). Todo lo
--      demas del chequeo queda igual y sigue cortando.
--      reserve_recovery_checkout_issuance_v1 NO se toca: usa primero la
--      intencion del caso (v_case.hotmart_purchase_intent_id), y el planificador
--      guarda la de la audiencia en el binding del caso (lo mide la prueba).
--
-- Nace apagada: sin un renglon en identity_link_controls, el claim no devuelve
-- nada y ninguna funcion de esta migracion decide ni encola. Sin una atadura
-- vigente, las cuatro funciones reemplazadas se comportan como antes.
--
-- Lo que no hace: no toca el correlador ni su ledger, ni el pre-resolutor, ni
-- operator_correlation_resolutions; no crea roles ni cambia el ACL del esquema;
-- no reabre conflictos anteriores a enabled_at (D8). Correrla dos veces aborta
-- con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; P0002 no existe; 55000 un estado que no lo
-- permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.hotmart_purchase_intent_correlations') is null
       or to_regprocedure('public._flow_engine_native_cart_audience(uuid,uuid,text,text,text)') is null
       or to_regprocedure('public._portable_cart_audience_checks(text,integer,uuid,uuid,text,text)') is null then
        raise exception using errcode = '55000', message = 'identity_link_prerequisites_missing';
    end if;
    if to_regclass('public.identity_link_decisions') is not null then
        raise exception using errcode = '55000', message = 'identity_link_decider_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. Las tablas.
-- ===========================================================================

-- La segunda llave (como pilot_runtime_controls): sin un renglon prendido para
-- el tenant y el funnel, el claim no devuelve nada aunque el bridge este
-- prendido. enabled_at es el corte: no se reabren conflictos anteriores (D8).
create table public.identity_link_controls (
    tenant_ref text not null check (btrim(tenant_ref) <> ''),
    funnel_ref text not null check (btrim(funnel_ref) <> ''),
    enabled_at timestamptz not null,
    enabled_by text not null check (btrim(enabled_by) <> ''),
    enable_reason text not null check (btrim(enable_reason) <> ''),
    disabled_at timestamptz,
    disabled_by text,
    disable_reason text,
    primary key (tenant_ref, funnel_ref),
    check ((disabled_at is null) = (disabled_by is null) and (disabled_at is null) = (disable_reason is null))
);

-- La cola: un renglon por evento de carrito en conflicto. Mutable (el lease).
create table public.identity_link_cases (
    webhook_event_id uuid primary key
        references public.hotmart_purchase_intent_correlations(webhook_event_id) on delete restrict,
    tenant_ref text not null,
    funnel_ref text not null,
    purchase_intent_id uuid references public.purchase_intents(id) on delete restrict,
    state text not null default 'pending' check (state in ('pending', 'decided')),
    claim_token uuid,
    claimed_by text,
    lease_generation bigint not null default 0 check (lease_generation >= 0),
    lease_expires_at timestamptz,
    attempt_count integer not null default 0 check (attempt_count >= 0),
    last_failure_code text check (last_failure_code is null or last_failure_code ~ '^[a-z0-9][a-z0-9_:-]{0,79}$'),
    evidence_snapshot jsonb,
    evidence_sha256 text check (evidence_sha256 is null or evidence_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz not null default clock_timestamp(),
    decided_at timestamptz,
    check ((evidence_snapshot is null) = (evidence_sha256 is null)),
    check ((state = 'decided') = (decided_at is not null))
);
create index identity_link_cases_pending_idx
    on public.identity_link_cases (tenant_ref, funnel_ref, created_at)
    where state = 'pending';

-- Las decisiones (append-only). Una por evento y modo; la de la persona (la
-- respuesta «Si, fui yo» del bloque B) va aparte, una por evento.
create table public.identity_link_decisions (
    decision_id uuid primary key default gen_random_uuid(),
    webhook_event_id uuid not null references public.identity_link_cases(webhook_event_id) on delete restrict,
    tenant_ref text not null,
    funnel_ref text not null,
    purchase_intent_id uuid references public.purchase_intents(id) on delete restrict,
    mode text not null check (mode in ('sombra', 'vivo')),
    decided_by text not null check (decided_by in ('freno', 'regla:R1', 'regla:R2', 'persona')
        or decided_by ~ '^modelo:[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$'),
    prompt_version text check (prompt_version is null or prompt_version ~ '^[a-z0-9][a-z0-9_.-]{0,79}$'),
    evidence_snapshot jsonb not null,
    evidence_sha256 text not null check (evidence_sha256 ~ '^[0-9a-f]{64}$'),
    model_decision text check (model_decision is null or model_decision in ('same_person', 'unsure', 'different_person')),
    model_confidence text check (model_confidence is null or model_confidence in ('high', 'medium', 'low')),
    reason_codes text[] not null default '{}'::text[],
    guard_reason text check (guard_reason is null or guard_reason ~ '^[a-z0-9][a-z0-9_]{0,79}$'),
    branch text not null check (branch in ('ata', 'pregunta', 'nada')),
    destination_source text check (destination_source is null or destination_source in ('intent_phone', 'intent_national_digits')),
    destination_sha256 text check (destination_sha256 is null or destination_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz not null default clock_timestamp(),
    check ((branch = 'nada') = (destination_source is null)),
    check ((destination_source is null) = (destination_sha256 is null)),
    check (branch <> 'ata' or guard_reason is null),
    check (decided_by <> 'freno' or (branch = 'nada' and guard_reason is not null)),
    check (cardinality(reason_codes) <= 20)
);
create unique index identity_link_decisions_one_per_event_mode
    on public.identity_link_decisions (webhook_event_id, mode) where decided_by <> 'persona';
create unique index identity_link_decisions_one_person_per_event
    on public.identity_link_decisions (webhook_event_id) where decided_by = 'persona';
create index identity_link_decisions_intent_idx
    on public.identity_link_decisions (purchase_intent_id) where branch = 'ata' and mode = 'vivo';

-- Que se hizo con cada decision (append-only).
create table public.identity_link_effects (
    effect_id uuid primary key default gen_random_uuid(),
    decision_id uuid not null references public.identity_link_decisions(decision_id) on delete restrict,
    effect text not null check (effect in ('requeued', 'ya_estaba_en_cola', 'pregunta_planificada', 'ninguno', 'sombra',
        'contacto_restaurado', 'contacto_sin_restaurar')),
    created_at timestamptz not null default clock_timestamp()
);
create index identity_link_effects_decision_idx on public.identity_link_effects (decision_id);

-- La auditora revoca (append-only). Desde ese momento la atadura deja de contar.
create table public.identity_link_revocations (
    decision_id uuid primary key references public.identity_link_decisions(decision_id) on delete restrict,
    actor_ref text not null check (btrim(actor_ref) <> '' and length(actor_ref) <= 200),
    reason text not null check (btrim(reason) <> '' and length(reason) <= 500),
    created_at timestamptz not null default clock_timestamp()
);

-- Los avisos ya admitidos por el conector de Slack (append-only).
create table public.identity_link_notices (
    notice_key text primary key check (notice_key ~ '^(decision|envio):[0-9a-f-]{36}(:[a-z0-9_]{1,40})?$'),
    tenant_ref text not null,
    funnel_ref text not null,
    created_at timestamptz not null default clock_timestamp()
);

-- El telefono que el contacto del evento tenia antes de que la atadura lo
-- dejara con el del permiso (bind_identity_link_contact_v1), para que la
-- revocacion lo restaure (append-only). Es el unico lugar con un telefono:
-- ninguna lectura ni aviso lo devuelve, y ningun rol de la API lo ve.
create table public.identity_link_contact_rebinds (
    rebind_id uuid primary key default gen_random_uuid(),
    decision_id uuid not null references public.identity_link_decisions(decision_id) on delete restrict,
    contact_id uuid not null references public.contacts(id) on delete restrict,
    previous_phone text,
    bound_phone text not null check (bound_phone ~ '^[1-9][0-9]{7,14}$'),
    created_at timestamptz not null default clock_timestamp(),
    unique (decision_id, contact_id)
);

create function public.reject_identity_link_mutation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, pg_temp
as $function$
begin
    raise exception using errcode = '55000', message = 'identity_link_append_only',
        detail = tg_table_name;
end;
$function$;

create trigger identity_link_decisions_append_only before update or delete on public.identity_link_decisions
    for each row execute function public.reject_identity_link_mutation();
create trigger identity_link_effects_append_only before update or delete on public.identity_link_effects
    for each row execute function public.reject_identity_link_mutation();
create trigger identity_link_revocations_append_only before update or delete on public.identity_link_revocations
    for each row execute function public.reject_identity_link_mutation();
create trigger identity_link_notices_append_only before update or delete on public.identity_link_notices
    for each row execute function public.reject_identity_link_mutation();
create trigger identity_link_contact_rebinds_append_only before update or delete on public.identity_link_contact_rebinds
    for each row execute function public.reject_identity_link_mutation();

do $tables_acl$
declare
    v_table text;
    v_role text;
begin
    foreach v_table in array array['identity_link_controls', 'identity_link_cases', 'identity_link_decisions',
        'identity_link_effects', 'identity_link_revocations', 'identity_link_notices',
        'identity_link_contact_rebinds'] loop
        execute format('alter table public.%I enable row level security', v_table);
        execute format('revoke all on table public.%I from public', v_table);
        for v_role in select r.rolname from pg_roles r
                       where r.rolname in ('anon', 'authenticated', 'service_role') order by r.rolname loop
            execute format('revoke all on table public.%I from %I', v_table, v_role);
        end loop;
    end loop;
end;
$tables_acl$;

-- ===========================================================================
-- 2. Los helpers.
-- ===========================================================================

-- El parecido de dos textos en pares de letras (coeficiente de Dice), 0-100.
create function public._identity_text_similarity_pct(p_left text, p_right text)
returns integer
language plpgsql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
declare
    v_left text := coalesce(p_left, '');
    v_right text := coalesce(p_right, '');
    v_common integer;
begin
    if v_left = '' or v_right = '' then
        return 0;
    end if;
    if v_left = v_right then
        return 100;
    end if;
    if length(v_left) < 2 or length(v_right) < 2 then
        return 0;
    end if;
    with left_pairs as (
        select substr(v_left, i, 2) as pair, count(*) as n
          from generate_series(1, length(v_left) - 1) i group by 1
    ), right_pairs as (
        select substr(v_right, i, 2) as pair, count(*) as n
          from generate_series(1, length(v_right) - 1) i group by 1
    )
    select coalesce(sum(least(left_pairs.n, right_pairs.n)), 0)::integer into v_common
      from left_pairs join right_pairs using (pair);
    return round(200.0 * v_common / ((length(v_left) - 1) + (length(v_right) - 1)))::integer;
end;
$function$;

-- El nombre para comparar: minusculas, sin acentos, solo letras y un espacio.
create function public._identity_name_normalized(p_name text)
returns text
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select nullif(btrim(regexp_replace(regexp_replace(
        translate(lower(coalesce(p_name, '')), 'áàäâãéèëêíìïîóòöôõúùüûñç', 'aaaaaeeeeiiiiooooouuuunc'),
        '[^a-z ]', ' ', 'g'), ' +', ' ', 'g')), '')
$function$;

-- El pais de un telefono, por el prefijo y nunca el numero: MX, NANP, AR u otro.
create function public._identity_phone_country(p_phone text)
returns text
language sql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case
        when canonical.value ~ '^52[0-9]{10}$' then 'MX'
        when canonical.value ~ '^1[0-9]{10}$' then 'NANP'
        when canonical.value ~ '^54[0-9]{10}$' then 'AR'
        else 'otro'
    end
    from (select public._whatsapp_phone_canonical(coalesce(p_phone, '')) as value) canonical
$function$;

-- Una compra aprobada del producto con el mail o con los 10 digitos de
-- cualquiera de los dos lados (el formulario y el checkout), desde 2 h antes
-- del carrito. Es el freno ya_compro y corta tambien una atadura vigente.
create function public._identity_link_purchase_seen(
    p_product_id text, p_since timestamptz, p_emails text[], p_phones text[])
returns boolean
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select exists (
        select 1
          from public.webhook_events purchase
          left join public.hotmart_purchase_intent_event_identities identity
            on identity.webhook_event_id = purchase.id
         where purchase.source = 'hotmart'
           and purchase.event_type = 'PURCHASE_APPROVED'
           and purchase.payload #>> '{data,product,id}' = p_product_id
           and coalesce(
                   to_timestamp(nullif(purchase.payload #>> '{data,purchase,approved_date}', '')::numeric / 1000),
                   purchase.received_at) >= p_since
           and (
               coalesce(identity.normalized_email, lower(btrim(purchase.payload #>> '{data,buyer,email}')))
                   = any(array_remove(p_emails, null))
               or right(regexp_replace(coalesce(identity.normalized_phone,
                        purchase.payload #>> '{data,buyer,checkout_phone}',
                        purchase.payload #>> '{data,buyer,phone}', ''), '[^0-9]', '', 'g'), 10)
                   = any(array(select right(regexp_replace(phone, '[^0-9]', '', 'g'), 10)
                                 from unnest(array_remove(p_phones, null)) phone
                                where length(regexp_replace(phone, '[^0-9]', '', 'g')) >= 10))
           )
    )
$function$;

-- ===========================================================================
-- 3. La evidencia: rasgos sin datos personales y el freno.
-- ===========================================================================

create function public.get_identity_link_evidence_v1(p_webhook_event_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_event public.webhook_events%rowtype;
    v_correlation public.hotmart_purchase_intent_correlations%rowtype;
    v_scope public.hotmart_purchase_intent_scopes%rowtype;
    v_intent public.purchase_intents%rowtype;
    v_binding public.commercial_ally_runtime_bindings%rowtype;
    v_candidates integer;
    v_phone_match boolean;
    v_checkout_email text;
    v_checkout_phone text;
    v_checkout_name text;
    v_form_name text;
    v_cart_at timestamptz;
    v_cart_offer text;
    v_cart_product text;
    v_intent_canonical text;
    v_checkout_canonical text;
    v_intent_digits text;
    v_checkout_digits text;
    v_guard text;
    v_has_consent boolean;
    v_contacts integer;
    v_traits jsonb;
begin
    if p_webhook_event_id is null then
        raise exception using errcode = '22023', message = 'identity_link_input_invalid';
    end if;
    select event.* into v_event from public.webhook_events event where event.id = p_webhook_event_id;
    if not found then
        raise exception using errcode = 'P0002', message = 'identity_link_event_not_found';
    end if;
    select correlation.* into v_correlation
      from public.hotmart_purchase_intent_correlations correlation
     where correlation.webhook_event_id = p_webhook_event_id;
    if not found then
        raise exception using errcode = 'P0002', message = 'identity_link_correlation_not_found';
    end if;
    select count(*)::integer into v_candidates
      from public.hotmart_purchase_intent_correlation_candidates candidate
     where candidate.webhook_event_id = p_webhook_event_id;

    -- fuera_de_alcance: solo el carrito, en conflicto de mail y telefono, con una candidata.
    if v_event.event_type is distinct from 'PURCHASE_OUT_OF_SHOPPING_CART'
       or v_correlation.event_type is distinct from 'PURCHASE_OUT_OF_SHOPPING_CART'
       or v_correlation.outcome is distinct from 'conflict'
       or v_correlation.reason_code is distinct from 'email_phone_conflict'
       or v_candidates <> 1 then
        return jsonb_build_object('version', 'identity-link-evidence-v1', 'persona', 'Persona 1',
            'guard_reason', 'fuera_de_alcance', 'rasgos', '{}'::jsonb);
    end if;

    select candidate.phone_match into v_phone_match
      from public.hotmart_purchase_intent_correlation_candidates candidate
     where candidate.webhook_event_id = p_webhook_event_id;
    select intent.* into v_intent
      from public.hotmart_purchase_intent_correlation_candidates candidate
      join public.purchase_intents intent on intent.id = candidate.purchase_intent_id
     where candidate.webhook_event_id = p_webhook_event_id;
    select scope.* into v_scope from public.hotmart_purchase_intent_scopes scope where scope.id = v_correlation.scope_id;
    select binding.* into v_binding
      from public.commercial_ally_runtime_bindings binding
     where binding.tenant_ref = v_intent.tenant_ref and binding.funnel_ref = v_intent.funnel_ref
       and binding.status = 'active';

    select identity.normalized_email, identity.normalized_phone into v_checkout_email, v_checkout_phone
      from public.hotmart_purchase_intent_event_identities identity
     where identity.webhook_event_id = p_webhook_event_id;
    v_checkout_name := v_event.payload #>> '{data,buyer,name}';
    v_cart_at := to_timestamp(nullif(v_event.payload ->> 'creation_date', '')::numeric / 1000);
    v_cart_offer := nullif(btrim(v_event.payload #>> '{data,offer,code}'), '');
    v_cart_product := v_event.payload #>> '{data,product,id}';
    select submission.canonical_payload #>> '{lead,full_name}' into v_form_name
      from public.purchase_intent_submissions link
      join public.precheckout_submissions submission on submission.id = link.submission_id
     where link.purchase_intent_id = v_intent.id
     order by link.ordinal desc limit 1;

    v_intent_canonical := public._whatsapp_phone_canonical(coalesce(v_intent.normalized_phone, ''));
    v_checkout_canonical := public._whatsapp_phone_canonical(coalesce(v_checkout_phone, ''));
    v_intent_digits := regexp_replace(coalesce(v_intent.normalized_phone, ''), '[^0-9]', '', 'g');
    v_checkout_digits := regexp_replace(coalesce(v_checkout_phone, ''), '[^0-9]', '', 'g');

    v_has_consent := v_intent.whatsapp_contact_authorized and v_binding.tenant_ref is not null and exists (
        select 1
          from public.purchase_intent_submissions link
          join public.precheckout_submissions submission on submission.id = link.submission_id
         where link.purchase_intent_id = v_intent.id
           and submission.contract_version = '1.1.0'
           and not submission.provisional
           and submission.provider_observed
           and submission.canonical_payload #>> '{consent,whatsapp_contact}' = 'true'
           and submission.canonical_payload #>> '{consent,marketing_optin}' = 'true'
           and submission.canonical_payload #>> '{consent,copy_version}' = v_binding.consent_copy_version
           and submission.canonical_payload #>> '{identity,phone}' = v_intent.normalized_phone);

    select count(distinct point.contact_id)::integer into v_contacts
      from public.contact_points point
     where (point.type = 'email' and v_checkout_email is not null and point.normalized_value = v_checkout_email)
        or (point.type = 'phone' and v_checkout_phone is not null
            and point.normalized_value = any(public._whatsapp_phone_variants(v_checkout_phone)))
        or (point.type = 'phone' and point.normalized_value = any(public._whatsapp_phone_variants(v_intent.normalized_phone)));

    v_traits := jsonb_build_object(
        'coincide_por', case when v_phone_match then 'telefono' else 'mail' end,
        'telefono_canonico_igual', v_checkout_canonical is not null and v_checkout_canonical = v_intent_canonical,
        'telefono_10_igual', length(v_intent_digits) >= 10 and length(v_checkout_digits) >= 10
            and right(v_intent_digits, 10) = right(v_checkout_digits, 10),
        'telefono_7_igual', length(v_intent_digits) >= 7 and length(v_checkout_digits) >= 7
            and right(v_intent_digits, 7) = right(v_checkout_digits, 7),
        'pais_formulario', public._identity_phone_country(v_intent.normalized_phone),
        'pais_checkout', public._identity_phone_country(v_checkout_phone),
        'mail_igual', v_checkout_email is not null and v_checkout_email = v_intent.normalized_email,
        'dominio_igual', v_checkout_email is not null and v_intent.normalized_email is not null
            and split_part(v_checkout_email, '@', 2) = split_part(v_intent.normalized_email, '@', 2),
        'usuario_igual', v_checkout_email is not null and v_intent.normalized_email is not null
            and split_part(v_checkout_email, '@', 1) = split_part(v_intent.normalized_email, '@', 1),
        'parecido_usuario', public._identity_text_similarity_pct(split_part(coalesce(v_checkout_email, ''), '@', 1),
            split_part(coalesce(v_intent.normalized_email, ''), '@', 1)),
        'parecido_dominio', public._identity_text_similarity_pct(split_part(coalesce(v_checkout_email, ''), '@', 2),
            split_part(coalesce(v_intent.normalized_email, ''), '@', 2)),
        'parecido_nombre', public._identity_text_similarity_pct(public._identity_name_normalized(v_checkout_name),
            public._identity_name_normalized(v_form_name)),
        'primer_nombre_igual', public._identity_name_normalized(v_checkout_name) is not null
            and split_part(public._identity_name_normalized(v_checkout_name), ' ', 1)
                = split_part(coalesce(public._identity_name_normalized(v_form_name), ''), ' ', 1),
        'minutos_formulario_a_carrito', greatest(0, floor(extract(epoch from (v_cart_at - v_intent.submitted_at)) / 60))::integer,
        'misma_oferta', v_cart_offer is not null and v_cart_offer = v_intent.offer_ref,
        'otras_intenciones_7d', (
            select count(*)::integer from public.purchase_intents other
             where other.id <> v_intent.id
               and other.tenant_ref = v_intent.tenant_ref
               and other.submitted_at >= v_cart_at - interval '7 days'
               and other.submitted_at <= v_cart_at
               and ((other.normalized_email is not null
                     and other.normalized_email in (v_intent.normalized_email, v_checkout_email))
                    or public._whatsapp_phone_canonical(other.normalized_phone) in (v_intent_canonical, v_checkout_canonical))),
        'conversacion_previa_con_el_telefono_del_permiso', exists (
            select 1 from public.conversations conversation
              join public.channel_identities identity on identity.id = conversation.channel_identity_id
             where identity.channel = 'whatsapp'
               and identity.external_user_id = any(public._whatsapp_phone_variants(v_intent.normalized_phone)))
    );
    -- El +1 del selector de pais del formulario (D3): el formulario guardo un
    -- numero de Estados Unidos con los mismos 10 digitos que el checkout de Mexico.
    v_traits := v_traits || jsonb_build_object('selector_pais_sospechoso',
        (v_traits ->> 'pais_formulario') = 'NANP' and (v_traits ->> 'pais_checkout') = 'MX'
        and (v_traits ->> 'telefono_10_igual')::boolean);

    v_guard := case
        when v_intent.lifecycle_state <> 'waiting_for_purchase'
             or v_intent.provisional
             or not v_intent.provider_observed
             or coalesce(v_intent.current_classification, '') in ('tracking_incomplete', 'expired_unknown')
            then 'intencion_no_viva'
        when not coalesce(v_has_consent, false) then 'sin_permiso'
        when exists (
            select 1 from public.contact_opt_out_events optout
             where optout.source = 'chatwoot' and optout.channel = 'whatsapp'
               and optout.canonical_account_id = v_binding.chatwoot_account_id
               and optout.external_user_id = any(public._whatsapp_phone_variants(v_intent.normalized_phone)
                   || public._whatsapp_phone_variants(coalesce(v_checkout_phone, v_intent.normalized_phone)))
               and optout.correlation_status in ('applied', 'unmatched', 'ambiguous', 'evidence_conflict'))
            then 'baja_previa'
        when public._identity_link_purchase_seen(v_cart_product, v_cart_at - interval '2 hours',
                 array[v_intent.normalized_email, v_checkout_email],
                 array[v_intent.normalized_phone, v_checkout_phone])
            then 'ya_compro'
        when v_cart_at is null or v_cart_at < clock_timestamp() - interval '24 hours' then 'carrito_viejo'
        when v_contacts > 1 then 'contacto_ambiguo'
        else null
    end;

    return jsonb_build_object('version', 'identity-link-evidence-v1', 'persona', 'Persona 1',
        'guard_reason', v_guard, 'rasgos', v_traits);
end;
$function$;

-- ===========================================================================
-- 4. La atadura vigente y la intencion del carrito.
-- ===========================================================================

-- La decision vigente de una intencion: modo vivo, rama ata, sin revocar, y
-- sin una compra desde 2 h antes del carrito (una compra corta la atadura: el
-- siguiente toque del carrito se cancela solo, con el motivo del permiso).
create function public._identity_link_active_decision(p_purchase_intent_id uuid)
returns public.identity_link_decisions
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select decision.*
      from public.identity_link_decisions decision
      join public.webhook_events event on event.id = decision.webhook_event_id
      join public.purchase_intents intent on intent.id = decision.purchase_intent_id
      left join public.hotmart_purchase_intent_event_identities identity on identity.webhook_event_id = event.id
     where decision.purchase_intent_id = p_purchase_intent_id
       and decision.mode = 'vivo'
       and decision.branch = 'ata'
       and not exists (select 1 from public.identity_link_revocations revocation
                        where revocation.decision_id = decision.decision_id)
       and not public._identity_link_purchase_seen(
               event.payload #>> '{data,product,id}',
               to_timestamp(nullif(event.payload ->> 'creation_date', '')::numeric / 1000) - interval '2 hours',
               array[intent.normalized_email, identity.normalized_email],
               array[intent.normalized_phone, identity.normalized_phone])
     order by decision.created_at desc
     limit 1
$function$;

create function public._identity_link_active_for_intent(p_purchase_intent_id uuid)
returns boolean
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select (public._identity_link_active_decision(p_purchase_intent_id)).decision_id is not null
$function$;

-- El telefono al que se escribe con la atadura vigente (en forma de la base:
-- el de la intencion, o 52 + sus 10 digitos con la atadura del +1). Null sin atadura.
create function public._identity_link_destination_for_intent(p_purchase_intent_id uuid)
returns text
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select case active.destination_source
        when 'intent_phone' then intent.normalized_phone
        when 'intent_national_digits' then '52' || right(regexp_replace(intent.normalized_phone, '[^0-9]', '', 'g'), 10)
        else null
    end
      from (select (public._identity_link_active_decision(p_purchase_intent_id)).*) active
      join public.purchase_intents intent on intent.id = p_purchase_intent_id
$function$;

-- La intencion de un evento de carrito: la de la correlacion resuelta, o la que
-- ato el correlador que decide solo, vigente.
create function public._cart_event_intent_v1(p_webhook_event_id uuid)
returns uuid
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select coalesce(
        (select correlation.purchase_intent_id
           from public.hotmart_purchase_intent_correlations correlation
          where correlation.webhook_event_id = p_webhook_event_id
            and correlation.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
            and correlation.outcome = 'resolved'
          limit 1),
        (select decision.purchase_intent_id
           from public.identity_link_decisions decision
          where decision.webhook_event_id = p_webhook_event_id
            and decision.mode = 'vivo'
            and decision.branch = 'ata'
            and not exists (select 1 from public.identity_link_revocations revocation
                             where revocation.decision_id = decision.decision_id)
            and public._identity_link_active_for_intent(decision.purchase_intent_id)
          order by decision.created_at desc
          limit 1))
$function$;

-- Para el bridge (resolution.py): el telefono al que el carrito de este evento
-- tiene que escribir, cuando el evento esta atado. Null si no.
create function public.get_identity_link_destination_v1(p_webhook_event_id uuid)
returns text
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    select public._identity_link_destination_for_intent(decision.purchase_intent_id)
      from public.identity_link_decisions decision
     where decision.webhook_event_id = p_webhook_event_id
       and decision.mode = 'vivo'
       and decision.branch = 'ata'
       and public._cart_event_intent_v1(p_webhook_event_id) = decision.purchase_intent_id
     order by decision.created_at desc
     limit 1
$function$;

-- Para el bridge (resolution.py), con el evento atado: deja el contacto del
-- evento con el telefono de la atadura, porque el envio sale a contacts.phone
-- (get_followup_execution_context). Solo:
--   - con una atadura vigente de este evento;
--   - si el contacto es el del checkout (tiene su mail o su telefono);
--   - si ningun otro contacto tiene ese telefono (si no, null: contacto_ambiguo);
--   - si contacts.phone es el del checkout, vacio, o (con el +1, D3) el del
--     formulario. Con cualquier otro telefono no lo toca y devuelve null.
-- Suma el contact_point del telefono (fuente hotmart, este evento). Devuelve el
-- telefono al que el carrito tiene que escribir, o null si no se pudo.
create function public.bind_identity_link_contact_v1(p_webhook_event_id uuid, p_contact_id uuid)
returns text
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_destination text;
    v_source text;
    v_intent public.purchase_intents%rowtype;
    v_checkout_email text;
    v_checkout_phone text;
    v_contact public.contacts%rowtype;
begin
    if p_webhook_event_id is null or p_contact_id is null then
        raise exception using errcode = '22023', message = 'identity_link_input_invalid';
    end if;
    v_destination := public.get_identity_link_destination_v1(p_webhook_event_id);
    if v_destination is null then
        return null;
    end if;
    select intent.* into v_intent from public.purchase_intents intent
     where intent.id = public._cart_event_intent_v1(p_webhook_event_id);
    v_source := (public._identity_link_active_decision(v_intent.id)).destination_source;
    select identity.normalized_email, identity.normalized_phone into v_checkout_email, v_checkout_phone
      from public.hotmart_purchase_intent_event_identities identity
     where identity.webhook_event_id = p_webhook_event_id;
    select contact.* into v_contact from public.contacts contact where contact.id = p_contact_id for update;
    if not found or not exists (
        select 1 from public.contact_points point
         where point.contact_id = p_contact_id
           and ((point.type = 'email' and point.normalized_value = v_checkout_email)
                or (point.type = 'phone' and v_checkout_phone is not null
                    and point.normalized_value = any(public._whatsapp_phone_variants(v_checkout_phone))))) then
        return null;
    end if;
    if exists (
        select 1 from public.contact_points point
         where point.contact_id <> p_contact_id
           and point.type = 'phone'
           and point.normalized_value = any(public._whatsapp_phone_variants(v_destination))) then
        return null;
    end if;
    if public._whatsapp_phone_canonical(coalesce(v_contact.phone, '')) is distinct from public._whatsapp_phone_canonical(v_destination) then
        if not (v_contact.phone is null or btrim(v_contact.phone) = ''
                or public._whatsapp_phone_canonical(v_contact.phone) = public._whatsapp_phone_canonical(coalesce(v_checkout_phone, ''))
                or (v_source = 'intent_national_digits'
                    and public._whatsapp_phone_canonical(v_contact.phone) = public._whatsapp_phone_canonical(v_intent.normalized_phone))) then
            return null;
        end if;
        -- El telefono anterior queda en el registro de la decision, para que
        -- revoke_identity_link_v1 lo restaure.
        insert into public.identity_link_contact_rebinds (decision_id, contact_id, previous_phone, bound_phone)
        values ((public._identity_link_active_decision(v_intent.id)).decision_id, p_contact_id,
                v_contact.phone, v_destination)
        on conflict (decision_id, contact_id) do nothing;
        update public.contacts contact set phone = v_destination, updated_at = now() where contact.id = p_contact_id;
    end if;
    insert into public.contact_points (contact_id, type, raw_value, normalized_value, source, source_event_id)
    select p_contact_id, 'phone', v_destination, v_destination, 'hotmart', p_webhook_event_id
     where not exists (select 1 from public.contact_points point
                        where point.contact_id = p_contact_id and point.type = 'phone'
                          and point.normalized_value = any(public._whatsapp_phone_variants(v_destination)));
    return v_destination;
end;
$function$;

-- ===========================================================================
-- 5. La cola.
-- ===========================================================================

-- Los errores con que el carrito termina hoy un conflicto (el legado, el motor
-- con su caida al envoltorio, y el nativo). Solo esos se vuelven a encolar.
create function public._identity_link_requeueable_error(p_error text)
returns boolean
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select coalesce(p_error, '') in (
        'pilot_scope_rejected:pilot_audience_intent_unresolved',
        'pilot_scope_rejected:no_entra_permiso',
        'flow_engine_no_start:no_entra:permiso')
$function$;

create function public.claim_identity_link_cases_v1(
    p_tenant_ref text, p_funnel_ref text, p_worker_id text, p_lease_seconds integer default 120)
returns table (webhook_event_id uuid, claim_token uuid, lease_generation bigint, attempt_count integer,
               evidence jsonb, evidence_sha256 text)
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_control public.identity_link_controls%rowtype;
    v_case public.identity_link_cases%rowtype;
    v_token uuid := gen_random_uuid();
begin
    if nullif(btrim(p_tenant_ref), '') is null or nullif(btrim(p_funnel_ref), '') is null
       or nullif(btrim(p_worker_id), '') is null or length(p_worker_id) > 200
       or p_lease_seconds is null or p_lease_seconds not between 10 and 900 then
        raise exception using errcode = '22023', message = 'identity_link_input_invalid';
    end if;
    select control.* into v_control from public.identity_link_controls control
     where control.tenant_ref = p_tenant_ref and control.funnel_ref = p_funnel_ref
       and control.disabled_at is null;
    if not found then
        return;
    end if;

    -- Los conflictos de carrito posteriores a la llave (D8: sin historicos).
    insert into public.identity_link_cases (webhook_event_id, tenant_ref, funnel_ref, purchase_intent_id)
    select correlation.webhook_event_id, scope.tenant_ref, scope.funnel_ref,
           (select candidate.purchase_intent_id from public.hotmart_purchase_intent_correlation_candidates candidate
             where candidate.webhook_event_id = correlation.webhook_event_id
             order by candidate.purchase_intent_id limit 1)
      from public.hotmart_purchase_intent_correlations correlation
      join public.hotmart_purchase_intent_scopes scope on scope.id = correlation.scope_id
     where correlation.event_type = 'PURCHASE_OUT_OF_SHOPPING_CART'
       and correlation.outcome = 'conflict'
       and correlation.created_at >= v_control.enabled_at
       and scope.tenant_ref = p_tenant_ref
       and scope.funnel_ref = p_funnel_ref
    on conflict do nothing;

    select ilc.* into v_case
      from public.identity_link_cases ilc
     where ilc.tenant_ref = p_tenant_ref and ilc.funnel_ref = p_funnel_ref
       and ilc.state = 'pending'
       and (ilc.lease_expires_at is null or ilc.lease_expires_at < clock_timestamp())
     order by ilc.created_at
     limit 1
     for update skip locked;
    if not found then
        return;
    end if;

    -- La evidencia se congela en el primer intento, con su huella.
    if v_case.evidence_snapshot is null then
        v_case.evidence_snapshot := public.get_identity_link_evidence_v1(v_case.webhook_event_id);
        v_case.evidence_sha256 := encode(sha256(convert_to(v_case.evidence_snapshot::text, 'UTF8')), 'hex');
    end if;

    update public.identity_link_cases ilc
       set claim_token = v_token,
           claimed_by = p_worker_id,
           lease_generation = ilc.lease_generation + 1,
           lease_expires_at = clock_timestamp() + make_interval(secs => p_lease_seconds),
           attempt_count = ilc.attempt_count + 1,
           evidence_snapshot = v_case.evidence_snapshot,
           evidence_sha256 = v_case.evidence_sha256
     where ilc.webhook_event_id = v_case.webhook_event_id
     returning ilc.* into v_case;

    return query select v_case.webhook_event_id, v_case.claim_token, v_case.lease_generation, v_case.attempt_count,
        v_case.evidence_snapshot, v_case.evidence_sha256;
end;
$function$;

create function public.release_identity_link_case_v1(
    p_webhook_event_id uuid, p_claim_token uuid, p_lease_generation bigint, p_failure_code text)
returns boolean
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_failure_code is null or p_failure_code !~ '^[a-z0-9][a-z0-9_:-]{0,79}$' then
        raise exception using errcode = '22023', message = 'identity_link_input_invalid';
    end if;
    update public.identity_link_cases ilc
       set lease_expires_at = clock_timestamp() + interval '30 seconds',
           claim_token = null,
           last_failure_code = p_failure_code
     where ilc.webhook_event_id = p_webhook_event_id
       and ilc.state = 'pending'
       and ilc.claim_token = p_claim_token
       and ilc.lease_generation = p_lease_generation;
    return found;
end;
$function$;

-- Escribe la decision y su efecto, y encola, en una transaccion. La base vuelve
-- a mirar los frenos en vivo y re-verifica la regla, el rasgo fuerte del modelo
-- y el destino: una decision que no cumple se rechaza (22023), nunca se ata.
create function public.complete_identity_link_decision_v1(
    p_webhook_event_id uuid, p_claim_token uuid, p_lease_generation bigint,
    p_mode text, p_decided_by text, p_branch text, p_guard_reason text,
    p_model_decision text, p_model_confidence text, p_reason_codes text[],
    p_prompt_version text, p_destination_source text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_case public.identity_link_cases%rowtype;
    v_intent public.purchase_intents%rowtype;
    v_event public.webhook_events%rowtype;
    v_live jsonb;
    v_traits jsonb;
    v_guard text;
    v_branch text := p_branch;
    v_destination_source text := p_destination_source;
    v_destination text;
    v_decision_id uuid;
    v_effect text;
    v_strong boolean;
begin
    if p_mode not in ('sombra', 'vivo') or p_branch not in ('ata', 'pregunta', 'nada')
       or p_decided_by is null or p_decided_by = 'persona'
       or coalesce(cardinality(p_reason_codes), 0) > 20
       or exists (select 1 from unnest(coalesce(p_reason_codes, '{}'::text[])) code
                   where code !~ '^[a-z0-9][a-z0-9_]{0,79}$') then
        raise exception using errcode = '22023', message = 'identity_link_input_invalid';
    end if;
    select ilc.* into v_case from public.identity_link_cases ilc
     where ilc.webhook_event_id = p_webhook_event_id for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'identity_link_case_not_found';
    end if;
    if v_case.state <> 'pending' or v_case.claim_token is distinct from p_claim_token
       or v_case.lease_generation <> p_lease_generation then
        raise exception using errcode = '55000', message = 'identity_link_lease_lost';
    end if;

    -- Los frenos, otra vez y en vivo: una baja o una compra entre el claim y aca ganan.
    v_live := public.get_identity_link_evidence_v1(p_webhook_event_id);
    v_traits := v_case.evidence_snapshot -> 'rasgos';
    v_guard := coalesce(v_live ->> 'guard_reason', v_case.evidence_snapshot ->> 'guard_reason');
    if v_guard is not null and v_branch <> 'nada' then
        v_branch := 'nada';
        v_destination_source := null;
    end if;

    if v_branch = 'nada' then
        v_destination_source := null;
        if p_decided_by = 'freno' and coalesce(p_guard_reason, v_guard) is null then
            raise exception using errcode = '22023', message = 'identity_link_guard_required';
        end if;
    else
        v_destination_source := coalesce(v_destination_source, 'intent_phone');
        -- El destino: el telefono del permiso; con el +1 (D3), solo el del checkout
        -- con el mail igual y los mismos 10 digitos. El +1 nunca es destino.
        if (v_traits ->> 'selector_pais_sospechoso')::boolean then
            if v_destination_source <> 'intent_national_digits' or not (v_traits ->> 'mail_igual')::boolean then
                raise exception using errcode = '22023', message = 'identity_link_destination_without_permission';
            end if;
        elsif v_destination_source <> 'intent_phone' then
            raise exception using errcode = '22023', message = 'identity_link_destination_without_permission';
        end if;
        -- La regla o el rasgo fuerte del modelo, re-verificados sobre la evidencia.
        if p_decided_by = 'regla:R1' then
            if v_branch <> 'ata' or not (v_traits ->> 'telefono_canonico_igual')::boolean then
                raise exception using errcode = '22023', message = 'identity_link_rule_not_met';
            end if;
        elsif p_decided_by = 'regla:R2' then
            if v_branch <> 'ata' or not (v_traits ->> 'mail_igual')::boolean
               or (v_traits ->> 'parecido_nombre')::integer < 90 then
                raise exception using errcode = '22023', message = 'identity_link_rule_not_met';
            end if;
        elsif p_decided_by like 'modelo:%' then
            v_strong := ('phone_canonical_equal' = any(p_reason_codes) and (v_traits ->> 'telefono_canonico_igual')::boolean)
                or ('email_exact_equal' = any(p_reason_codes) and (v_traits ->> 'mail_igual')::boolean)
                or ('phone_national_equal_country_differs' = any(p_reason_codes)
                    and v_destination_source = 'intent_national_digits'
                    and (v_traits ->> 'selector_pais_sospechoso')::boolean);
            if v_branch = 'ata' and not (p_model_decision = 'same_person' and p_model_confidence = 'high' and v_strong) then
                raise exception using errcode = '22023', message = 'identity_link_model_without_strong_trait';
            end if;
            if v_branch = 'pregunta' and not (
                (p_model_decision = 'same_person' and p_model_confidence = 'medium') or p_model_decision = 'unsure') then
                raise exception using errcode = '22023', message = 'identity_link_model_branch_mismatch';
            end if;
        else
            raise exception using errcode = '22023', message = 'identity_link_decider_invalid';
        end if;
    end if;

    select intent.* into v_intent from public.purchase_intents intent where intent.id = v_case.purchase_intent_id;
    if v_destination_source = 'intent_phone' then
        v_destination := public._whatsapp_phone_canonical(v_intent.normalized_phone);
    elsif v_destination_source = 'intent_national_digits' then
        v_destination := '52' || right(regexp_replace(v_intent.normalized_phone, '[^0-9]', '', 'g'), 10);
    end if;

    insert into public.identity_link_decisions (
        webhook_event_id, tenant_ref, funnel_ref, purchase_intent_id, mode, decided_by, prompt_version,
        evidence_snapshot, evidence_sha256, model_decision, model_confidence, reason_codes, guard_reason,
        branch, destination_source, destination_sha256)
    values (
        p_webhook_event_id, v_case.tenant_ref, v_case.funnel_ref, v_case.purchase_intent_id, p_mode,
        case when v_branch = 'nada' and v_guard is not null and p_decided_by not like 'modelo:%' then 'freno' else p_decided_by end,
        p_prompt_version, v_case.evidence_snapshot, v_case.evidence_sha256, p_model_decision, p_model_confidence,
        coalesce(p_reason_codes, '{}'::text[]),
        case when v_branch = 'nada' then coalesce(v_guard, p_guard_reason) else null end,
        v_branch, v_destination_source,
        case when v_destination is null then null else encode(sha256(convert_to(v_destination, 'UTF8')), 'hex') end)
    returning decision_id into v_decision_id;

    if p_mode = 'sombra' then
        v_effect := 'sombra';
    elsif v_branch = 'ata' then
        select event.* into v_event from public.webhook_events event where event.id = p_webhook_event_id for update;
        if v_event.processing_status = 'failed' and public._identity_link_requeueable_error(v_event.processing_error) then
            update public.webhook_events event
               set processing_status = 'received', processing_error = 'identity_link_requeued', processed_at = null
             where event.id = p_webhook_event_id;
            v_effect := 'requeued';
        elsif v_event.processing_status in ('received', 'processing') then
            v_effect := 'ya_estaba_en_cola';
        else
            v_effect := 'ninguno';
        end if;
    elsif v_branch = 'pregunta' then
        -- El bloque B (20261114000150) planifica la pregunta; sin el, ninguno.
        if to_regclass('public.identity_link_questions') is not null then
            execute 'insert into public.identity_link_questions (decision_id, webhook_event_id, tenant_ref, funnel_ref, purchase_intent_id)
                     values ($1, $2, $3, $4, $5)'
              using v_decision_id, p_webhook_event_id, v_case.tenant_ref, v_case.funnel_ref, v_case.purchase_intent_id;
            v_effect := 'pregunta_planificada';
        else
            v_effect := 'ninguno';
        end if;
    else
        v_effect := 'ninguno';
    end if;
    insert into public.identity_link_effects (decision_id, effect) values (v_decision_id, v_effect);

    update public.identity_link_cases ilc
       set state = 'decided', decided_at = clock_timestamp(), claim_token = null, lease_expires_at = null
     where ilc.webhook_event_id = p_webhook_event_id;

    return jsonb_build_object('decision_id', v_decision_id, 'branch', v_branch, 'effect', v_effect,
        'guard_reason', case when v_branch = 'nada' then coalesce(v_guard, p_guard_reason) else null end,
        'destination_source', v_destination_source);
end;
$function$;

-- La carrera: el worker de eventos falla el carrito justo despues de que se
-- ata. Idempotente: solo eventos con una atadura vigente, uno de los errores
-- del conflicto y todavia sin un reencolado de esa decision (nunca en bucle).
create function public.requeue_identity_linked_events_v1(p_tenant_ref text, p_funnel_ref text)
returns integer
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_row record;
    v_count integer := 0;
begin
    for v_row in
        select decision.decision_id, event.id as event_id
          from public.identity_link_decisions decision
          join public.webhook_events event on event.id = decision.webhook_event_id
         where decision.tenant_ref = p_tenant_ref and decision.funnel_ref = p_funnel_ref
           and decision.mode = 'vivo' and decision.branch = 'ata'
           and event.processing_status = 'failed'
           and public._identity_link_requeueable_error(event.processing_error)
           and public._cart_event_intent_v1(event.id) = decision.purchase_intent_id
           and not exists (select 1 from public.identity_link_effects effect
                            where effect.decision_id = decision.decision_id and effect.effect = 'requeued')
         for update of event skip locked
    loop
        update public.webhook_events event
           set processing_status = 'received', processing_error = 'identity_link_requeued', processed_at = null
         where event.id = v_row.event_id;
        insert into public.identity_link_effects (decision_id, effect) values (v_row.decision_id, 'requeued');
        v_count := v_count + 1;
    end loop;
    return v_count;
end;
$function$;

-- La auditora revoca una atadura. No borra nada ni toca la intencion: el
-- proximo toque del carrito se cancela solo (el chequeo del permiso vuelve a
-- cortar por identity_conflict).
create function public.revoke_identity_link_v1(p_decision_id uuid, p_actor_ref text, p_reason text)
returns boolean
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_rebind public.identity_link_contact_rebinds%rowtype;
begin
    if p_decision_id is null or nullif(btrim(p_actor_ref), '') is null or nullif(btrim(p_reason), '') is null then
        raise exception using errcode = '22023', message = 'identity_link_input_invalid';
    end if;
    if not exists (select 1 from public.identity_link_decisions decision
                    where decision.decision_id = p_decision_id and decision.branch = 'ata') then
        raise exception using errcode = 'P0002', message = 'identity_link_decision_not_found';
    end if;
    insert into public.identity_link_revocations (decision_id, actor_ref, reason)
    values (p_decision_id, btrim(p_actor_ref), btrim(p_reason))
    on conflict do nothing;
    if not found then
        return false;
    end if;
    -- El contacto que la atadura dejo con el telefono del permiso vuelve a su
    -- telefono anterior, solo si nadie lo cambio despues.
    for v_rebind in
        select rebind.* from public.identity_link_contact_rebinds rebind
         where rebind.decision_id = p_decision_id
         order by rebind.created_at
    loop
        update public.contacts contact
           set phone = v_rebind.previous_phone, updated_at = now()
         where contact.id = v_rebind.contact_id
           and public._whatsapp_phone_canonical(coalesce(contact.phone, ''))
               = public._whatsapp_phone_canonical(v_rebind.bound_phone);
        insert into public.identity_link_effects (decision_id, effect)
        values (p_decision_id, case when found then 'contacto_restaurado' else 'contacto_sin_restaurar' end);
    end loop;
    return true;
end;
$function$;

-- La llave de la base, para quien opera (no es de la API).
create function public.enable_identity_link_v1(p_tenant_ref text, p_funnel_ref text, p_actor_ref text, p_reason text)
returns timestamptz
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_at timestamptz := clock_timestamp();
begin
    if nullif(btrim(p_tenant_ref), '') is null or nullif(btrim(p_funnel_ref), '') is null
       or nullif(btrim(p_actor_ref), '') is null or nullif(btrim(p_reason), '') is null then
        raise exception using errcode = '22023', message = 'identity_link_input_invalid';
    end if;
    insert into public.identity_link_controls (tenant_ref, funnel_ref, enabled_at, enabled_by, enable_reason)
    values (p_tenant_ref, p_funnel_ref, v_at, p_actor_ref, p_reason)
    on conflict (tenant_ref, funnel_ref) do update
       set enabled_at = case when public.identity_link_controls.disabled_at is null
                             then public.identity_link_controls.enabled_at else excluded.enabled_at end,
           enabled_by = excluded.enabled_by, enable_reason = excluded.enable_reason,
           disabled_at = null, disabled_by = null, disable_reason = null;
    return (select control.enabled_at from public.identity_link_controls control
             where control.tenant_ref = p_tenant_ref and control.funnel_ref = p_funnel_ref);
end;
$function$;

create function public.disable_identity_link_v1(p_tenant_ref text, p_funnel_ref text, p_actor_ref text, p_reason text)
returns boolean
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if nullif(btrim(p_actor_ref), '') is null or nullif(btrim(p_reason), '') is null then
        raise exception using errcode = '22023', message = 'identity_link_input_invalid';
    end if;
    update public.identity_link_controls control
       set disabled_at = clock_timestamp(), disabled_by = p_actor_ref, disable_reason = p_reason
     where control.tenant_ref = p_tenant_ref and control.funnel_ref = p_funnel_ref and control.disabled_at is null;
    return found;
end;
$function$;

-- ===========================================================================
-- 6. Los avisos al canal de alertas de la aliada (sin telefonos ni mails).
-- ===========================================================================

-- El numero de conversacion de Chatwoot de un telefono, si ya hay una.
create function public._identity_link_conversation_number(p_phone text, p_account_id bigint)
returns bigint
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select identity.external_conversation_id::bigint
      from public.channel_identities identity
     where identity.channel = 'whatsapp'
       and identity.account_id = p_account_id::text
       and identity.external_user_id = any(public._whatsapp_phone_variants(coalesce(p_phone, '')))
       and identity.external_conversation_id ~ '^[1-9][0-9]{0,11}$'
     order by identity.updated_at desc
     limit 1
$function$;

-- Lo pendiente de avisar: cada decision vivo que ata o pregunta, y cada envio
-- de un carrito atado (sale o falla). notice_key es la clave del aviso.
create function public.read_identity_link_notices_v1(p_tenant_ref text, p_funnel_ref text, p_limit integer default 20)
returns table (notice_key text, kind text, branch text, decided_by text, outcome text,
               conversation_id bigint, occurred_at timestamptz)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    with pending as (
        select 'decision:' || decision.decision_id::text as notice_key, 'decision'::text as kind,
               decision.branch, decision.decided_by, null::text as outcome,
               public._identity_link_conversation_number(intent.normalized_phone, binding.chatwoot_account_id)
                   as conversation_id,
               decision.created_at as occurred_at
          from public.identity_link_decisions decision
          join public.purchase_intents intent on intent.id = decision.purchase_intent_id
          left join public.commercial_ally_runtime_bindings binding
            on binding.tenant_ref = decision.tenant_ref and binding.funnel_ref = decision.funnel_ref
           and binding.status = 'active'
         where decision.tenant_ref = p_tenant_ref and decision.funnel_ref = p_funnel_ref
           and decision.mode = 'vivo' and decision.branch in ('ata', 'pregunta')
        union all
        select 'envio:' || attempt.id::text || ':' || case when attempt.outcome = 'accepted_by_chatwoot'
                   then 'sale' else 'falla' end,
               'envio', decision.branch, decision.decided_by,
               case when attempt.outcome = 'accepted_by_chatwoot' then 'sale' else 'falla' end,
               case when identity.external_conversation_id ~ '^[1-9][0-9]{0,11}$'
                    then identity.external_conversation_id::bigint end,
               coalesce(attempt.accepted_at, attempt.updated_at)
          from public.identity_link_decisions decision
          join public.recovery_cases recovery on recovery.abandonment_event_id = decision.webhook_event_id
          join public.scheduled_actions action on action.recovery_case_id = recovery.id
          join public.followup_delivery_attempts attempt on attempt.action_id = action.id
          left join public.channel_identities identity on identity.id = recovery.selected_channel_identity_id
         where decision.tenant_ref = p_tenant_ref and decision.funnel_ref = p_funnel_ref
           and decision.mode = 'vivo' and decision.branch = 'ata'
           and attempt.phase = 'completed'
    )
    select pending.*
      from pending
     where not exists (select 1 from public.identity_link_notices notice where notice.notice_key = pending.notice_key)
     order by pending.occurred_at
     limit greatest(1, least(coalesce(p_limit, 20), 100))
$function$;

create function public.mark_identity_link_notice_v1(p_tenant_ref text, p_funnel_ref text, p_notice_key text)
returns boolean
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_notice_key is null or p_notice_key !~ '^(decision|envio):[0-9a-f-]{36}(:[a-z0-9_]{1,40})?$'
       or nullif(btrim(p_tenant_ref), '') is null or nullif(btrim(p_funnel_ref), '') is null then
        raise exception using errcode = '22023', message = 'identity_link_input_invalid';
    end if;
    insert into public.identity_link_notices (notice_key, tenant_ref, funnel_ref)
    values (p_notice_key, p_tenant_ref, p_funnel_ref)
    on conflict do nothing;
    return found;
end;
$function$;

-- ===========================================================================
-- 7. Los reemplazos sobre la definicion vigente.
-- ===========================================================================

do $patch$
declare
    v_signatures text[] := array[
        'public.plan_lancemos_pilot_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)',
        'public._portable_cart_audience_checks(text,integer,uuid,uuid,text,text)',
        'public._flow_engine_native_cart_audience(uuid,uuid,text,text,text)',
        'public._portable_consented_intent_reason(uuid,uuid,text)',
        'public._portable_consented_intent_reason(uuid,uuid,text)',
        'public._portable_consented_intent_reason(uuid,uuid,text)'
    ];
    v_anchors text[] := array[
        -- plan_lancemos_pilot_cart_recovery, bloque pilot_audience
        E'        select correlation.purchase_intent_id into v_audience_intent_id\n'
        || E'        from public.hotmart_purchase_intent_correlations correlation\n'
        || E'        where correlation.webhook_event_id = p_webhook_event_id\n'
        || E'          and correlation.event_type = ''PURCHASE_OUT_OF_SHOPPING_CART''\n'
        || E'          and correlation.outcome = ''resolved'';\n',
        -- _portable_cart_audience_checks
        E'          (select correlation.purchase_intent_id\n'
        || E'             from public.hotmart_purchase_intent_correlations correlation\n'
        || E'            where correlation.webhook_event_id = p_webhook_event_id\n'
        || E'              and correlation.event_type = ''PURCHASE_OUT_OF_SHOPPING_CART''\n'
        || E'              and correlation.outcome = ''resolved''\n'
        || E'            limit 1),\n',
        -- _flow_engine_native_cart_audience
        E'    select intent.* into v_intent\n'
        || E'      from public.hotmart_purchase_intent_correlations correlation\n'
        || E'      join public.purchase_intents intent on intent.id = correlation.purchase_intent_id\n'
        || E'     where correlation.webhook_event_id = p_webhook_event_id\n'
        || E'       and correlation.event_type = ''PURCHASE_OUT_OF_SHOPPING_CART''\n'
        || E'       and correlation.outcome = ''resolved''\n'
        || E'     limit 1;\n',
        -- _portable_consented_intent_reason: las variables
        E'    v_binding public.commercial_ally_runtime_bindings%rowtype;\nbegin\n',
        -- _portable_consented_intent_reason: vivo y autorizado
        E'    if v_intent.lifecycle_state <> ''waiting_for_purchase''\n'
        || E'       or v_intent.provisional\n'
        || E'       or not v_intent.provider_observed\n'
        || E'       or coalesce(v_intent.current_classification, '''') in (\n'
        || E'           ''identity_conflict'', ''tracking_incomplete'', ''expired_unknown''\n'
        || E'       ) then\n'
        || E'        reason_code := ''consented_intent_not_live'';\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'\n'
        || E'    if not v_intent.whatsapp_contact_authorized\n'
        || E'       or not v_intent.activation_authorized then\n'
        || E'        reason_code := ''consented_intent_not_authorized'';\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'\n'
        || E'    if v_intent.normalized_phone is null\n'
        || E'       or public._whatsapp_phone_canonical(v_intent.normalized_phone)\n'
        || E'           is distinct from public._whatsapp_phone_canonical(p_destination_phone)\n'
        || E'       or not exists (\n'
        || E'           select 1\n'
        || E'           from public.contact_points point\n'
        || E'           where point.contact_id = p_contact_id\n'
        || E'             and point.type = ''phone''\n'
        || E'             and point.normalized_value = any(\n'
        || E'                 public._whatsapp_phone_variants(v_intent.normalized_phone)\n'
        || E'             )\n'
        || E'       ) then\n',
        -- _portable_consented_intent_reason: contacts.phone
        E'          and public._whatsapp_phone_canonical(contact.phone)\n'
        || E'              = public._whatsapp_phone_canonical(v_intent.normalized_phone)\n'
    ];
    v_replacements text[] := array[
        E'        -- identity_link_decider: begin\n'
        || E'        -- La intencion resuelta del evento, o la que ato el correlador\n'
        || E'        -- que decide solo (20261114000100), vigente.\n'
        || E'        v_audience_intent_id := public._cart_event_intent_v1(p_webhook_event_id);\n'
        || E'        -- identity_link_decider: end\n',
        E'          -- identity_link_decider: la intencion resuelta, o la atada vigente.\n'
        || E'          public._cart_event_intent_v1(p_webhook_event_id),\n',
        E'    -- identity_link_decider: begin\n'
        || E'    -- La intencion resuelta del evento, o la que ato el correlador que\n'
        || E'    -- decide solo (20261114000100), vigente.\n'
        || E'    select intent.* into v_intent\n'
        || E'      from public.purchase_intents intent\n'
        || E'     where intent.id = public._cart_event_intent_v1(p_webhook_event_id);\n'
        || E'    -- identity_link_decider: end\n',
        E'    v_binding public.commercial_ally_runtime_bindings%rowtype;\n'
        || E'    -- identity_link_decider: begin\n'
        || E'    v_linked boolean := false;\n'
        || E'    v_link_phone text;\n'
        || E'    -- identity_link_decider: end\n'
        || E'begin\n',
        E'    -- identity_link_decider: begin\n'
        || E'    -- Una atadura vigente del correlador que decide solo (20261114000100)\n'
        || E'    -- levanta SOLO las dos condiciones que pone el conflicto: identity_conflict\n'
        || E'    -- y activation_authorized = false. El telefono al que se escribe es el\n'
        || E'    -- de la atadura: el de la intencion, o con el +1 (D3) el del checkout.\n'
        || E'    v_linked := coalesce(v_intent.current_classification, '''') = ''identity_conflict''\n'
        || E'        and public._identity_link_active_for_intent(v_intent.id);\n'
        || E'    v_link_phone := case when v_linked\n'
        || E'        then public._identity_link_destination_for_intent(v_intent.id)\n'
        || E'        else v_intent.normalized_phone end;\n'
        || E'    if v_intent.lifecycle_state <> ''waiting_for_purchase''\n'
        || E'       or v_intent.provisional\n'
        || E'       or not v_intent.provider_observed\n'
        || E'       or coalesce(v_intent.current_classification, '''') in (\n'
        || E'           ''tracking_incomplete'', ''expired_unknown''\n'
        || E'       )\n'
        || E'       or (coalesce(v_intent.current_classification, '''') = ''identity_conflict''\n'
        || E'           and not v_linked) then\n'
        || E'        reason_code := ''consented_intent_not_live'';\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'\n'
        || E'    if not v_intent.whatsapp_contact_authorized\n'
        || E'       or (not v_intent.activation_authorized and not v_linked) then\n'
        || E'        reason_code := ''consented_intent_not_authorized'';\n'
        || E'        return;\n'
        || E'    end if;\n'
        || E'\n'
        || E'    if v_intent.normalized_phone is null\n'
        || E'       or v_link_phone is null\n'
        || E'       or public._whatsapp_phone_canonical(v_link_phone)\n'
        || E'           is distinct from public._whatsapp_phone_canonical(p_destination_phone)\n'
        || E'       or not exists (\n'
        || E'           select 1\n'
        || E'           from public.contact_points point\n'
        || E'           where point.contact_id = p_contact_id\n'
        || E'             and point.type = ''phone''\n'
        || E'             and point.normalized_value = any(\n'
        || E'                 public._whatsapp_phone_variants(v_link_phone)\n'
        || E'             )\n'
        || E'       ) then\n'
        || E'    -- identity_link_decider: end\n',
        E'          -- identity_link_decider: el telefono de la atadura (sin ella, el de la intencion).\n'
        || E'          and public._whatsapp_phone_canonical(contact.phone)\n'
        || E'              = public._whatsapp_phone_canonical(v_link_phone)\n'
    ];
    v_definition text;
    i integer;
begin
    for i in 1 .. array_length(v_signatures, 1) loop
        if to_regprocedure(v_signatures[i]) is null then
            raise exception using errcode = '55000', message = 'identity_link_function_missing', detail = v_signatures[i];
        end if;
        if (select p.proowner from pg_proc p where p.oid = v_signatures[i]::regprocedure)
           is distinct from (select r.oid from pg_roles r where r.rolname = current_user) then
            raise exception using errcode = '55000', message = 'identity_link_owner_mismatch', detail = v_signatures[i];
        end if;
        v_definition := pg_get_functiondef(v_signatures[i]::regprocedure);
        if length(v_definition) - length(replace(v_definition, v_anchors[i], '')) <> length(v_anchors[i]) then
            raise exception using errcode = '55000', message = 'unexpected_identity_link_definition',
                detail = v_signatures[i] || ' #' || i;
        end if;
        -- La marca no puede estar antes del primer reemplazo de cada funcion.
        if i in (1, 2, 3, 4) and position('identity_link_decider' in v_definition) > 0 then
            raise exception using errcode = '55000', message = 'unexpected_identity_link_definition',
                detail = v_signatures[i] || ' ya tiene la marca';
        end if;
        execute replace(v_definition, v_anchors[i], v_replacements[i]);
        if position(v_replacements[i] in pg_get_functiondef(v_signatures[i]::regprocedure)) = 0 then
            raise exception using errcode = '55000', message = 'identity_link_definition_not_replaced',
                detail = v_signatures[i] || ' #' || i;
        end if;
    end loop;
end;
$patch$;

-- ===========================================================================
-- El ACL: las de la API, solo service_role; las internas y las del operador,
-- de nadie de la API.
-- ===========================================================================

do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public.reject_identity_link_mutation()',
        'public._identity_text_similarity_pct(text,text)',
        'public._identity_name_normalized(text)',
        'public._identity_phone_country(text)',
        'public._identity_link_purchase_seen(text,timestamptz,text[],text[])',
        'public.get_identity_link_evidence_v1(uuid)',
        'public._identity_link_active_decision(uuid)',
        'public._identity_link_active_for_intent(uuid)',
        'public._identity_link_destination_for_intent(uuid)',
        'public._cart_event_intent_v1(uuid)',
        'public.get_identity_link_destination_v1(uuid)',
        'public.bind_identity_link_contact_v1(uuid,uuid)',
        'public._identity_link_requeueable_error(text)',
        'public.claim_identity_link_cases_v1(text,text,text,integer)',
        'public.release_identity_link_case_v1(uuid,uuid,bigint,text)',
        'public.complete_identity_link_decision_v1(uuid,uuid,bigint,text,text,text,text,text,text,text[],text,text)',
        'public.requeue_identity_linked_events_v1(text,text)',
        'public.revoke_identity_link_v1(uuid,text,text)',
        'public.enable_identity_link_v1(text,text,text,text)',
        'public.disable_identity_link_v1(text,text,text,text)',
        'public._identity_link_conversation_number(text,bigint)',
        'public.read_identity_link_notices_v1(text,text,integer)',
        'public.mark_identity_link_notice_v1(text,text,text)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in select r.rolname from pg_roles r
                       where r.rolname in ('anon', 'authenticated', 'service_role') order by r.rolname loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.claim_identity_link_cases_v1(text,text,text,integer) to service_role;
        grant execute on function public.release_identity_link_case_v1(uuid,uuid,bigint,text) to service_role;
        grant execute on function public.complete_identity_link_decision_v1(uuid,uuid,bigint,text,text,text,text,text,text,text[],text,text) to service_role;
        grant execute on function public.requeue_identity_linked_events_v1(text,text) to service_role;
        grant execute on function public.get_identity_link_destination_v1(uuid) to service_role;
        grant execute on function public.bind_identity_link_contact_v1(uuid,uuid) to service_role;
        grant execute on function public.read_identity_link_notices_v1(text,text,integer) to service_role;
        grant execute on function public.mark_identity_link_notice_v1(text,text,text) to service_role;
    end if;
end;
$acl$;

commit;
