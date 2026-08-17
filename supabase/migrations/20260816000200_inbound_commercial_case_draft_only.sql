-- Cut B: admit canonical Chatwoot inbound cases as draft-only durable roots.
-- No agent, handoff, scheduling, authorization, or outbound effects are created.

begin;

create table public.inbound_commercial_scope_versions (
    scope_key text not null check (scope_key ~ '^[a-z0-9_-]{1,100}$'),
    version integer not null check (version > 0),
    status text not null check (status = any (array['draft', 'published'])),
    tenant_key text not null check (length(btrim(tenant_key)) > 0),
    chatwoot_account_id bigint not null check (chatwoot_account_id > 0),
    chatwoot_inbox_id bigint not null check (chatwoot_inbox_id > 0),
    external_product_id text not null check (length(btrim(external_product_id)) > 0),
    offer_code text not null check (length(btrim(offer_code)) > 0),
    approved_by text,
    approved_at timestamptz,
    published_at timestamptz,
    created_at timestamptz not null default clock_timestamp(),
    primary key (scope_key, version),
    check (
        status <> 'published'
        or (
            nullif(btrim(approved_by), '') is not null
            and approved_at is not null
            and published_at is not null
        )
    )
);

alter table public.inbound_commercial_scope_versions enable row level security;

create function public.protect_inbound_commercial_scope_version()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    if tg_op = 'DELETE' and old.status = 'published' then
        raise exception using errcode = '55000', message = 'published_inbound_scope_is_immutable';
    end if;
    if tg_op = 'UPDATE' and old.status = 'published' then
        raise exception using errcode = '55000', message = 'published_inbound_scope_is_immutable';
    end if;
    return case when tg_op = 'DELETE' then old else new end;
end;
$function$;

create trigger inbound_commercial_scope_versions_protect
before update or delete on public.inbound_commercial_scope_versions
for each row execute function public.protect_inbound_commercial_scope_version();

alter table public.commercial_cases
    add column inbound_scope_key text,
    add column inbound_scope_version integer,
    add column tenant_ref text,
    add constraint commercial_cases_inbound_scope_shape check (
        (
            case_kind = 'inbound_sales'
            and inbound_scope_key is not null
            and inbound_scope_key ~ '^[a-z0-9_-]{1,100}$'
            and inbound_scope_version is not null
            and inbound_scope_version > 0
            and tenant_ref is not null
            and length(btrim(tenant_ref)) > 0
        )
        or (
            case_kind <> 'inbound_sales'
            and inbound_scope_key is null
            and inbound_scope_version is null
            and tenant_ref is null
        )
    ),
    add constraint commercial_cases_inbound_scope_fk
        foreign key (inbound_scope_key, inbound_scope_version)
        references public.inbound_commercial_scope_versions(scope_key, version)
        on delete restrict;

create table public.inbound_commercial_case_admissions (
    id uuid primary key default gen_random_uuid(),
    scope_key text not null,
    scope_version integer not null,
    external_conversation_id bigint not null check (external_conversation_id > 0),
    external_user_id text not null check (external_user_id ~ '^[0-9]+$'),
    commercial_case_id uuid not null unique
        references public.commercial_cases(id) on delete restrict,
    contact_id uuid not null references public.contacts(id) on delete restrict,
    channel_identity_id uuid not null
        references public.channel_identities(id) on delete restrict,
    conversation_id uuid not null
        references public.conversations(id) on delete restrict,
    created_at timestamptz not null default clock_timestamp(),
    foreign key (scope_key, scope_version)
        references public.inbound_commercial_scope_versions(scope_key, version)
        on delete restrict,
    unique (scope_key, scope_version, external_conversation_id)
);

alter table public.inbound_commercial_case_admissions enable row level security;

create function public.protect_inbound_commercial_case_admission()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    if tg_op = 'INSERT' then
        if not exists (
            select 1
            from public.commercial_cases commercial_case
            join public.inbound_commercial_scope_versions scope
              on scope.scope_key = new.scope_key
             and scope.version = new.scope_version
             and scope.status = 'published'
            join public.channel_identities identity
              on identity.id = new.channel_identity_id
             and identity.contact_id = new.contact_id
             and identity.channel = 'whatsapp'
             and identity.account_id = 'chatwoot:' || scope.chatwoot_account_id::text
             and identity.metadata ->> 'inbox_id' = scope.chatwoot_inbox_id::text
             and identity.identity_status = 'active'
            join public.conversations conversation
              on conversation.id = new.conversation_id
             and conversation.channel_identity_id = identity.id
             and conversation.contact_id = identity.contact_id
            where commercial_case.id = new.commercial_case_id
              and commercial_case.case_kind = 'inbound_sales'
              and commercial_case.contact_id = new.contact_id
              and commercial_case.selected_channel_identity_id = new.channel_identity_id
              and commercial_case.conversation_id = new.conversation_id
              and commercial_case.inbound_scope_key = new.scope_key
              and commercial_case.inbound_scope_version = new.scope_version
              and conversation.commercial_context = jsonb_build_object(
                  'chatwoot_conversation_id', new.external_conversation_id::text
              )
              and identity.external_user_id = new.external_user_id
        ) then
            raise exception using errcode = '23514', message = 'inbound_commercial_case_admission_mismatch';
        end if;
        return new;
    end if;
    raise exception using errcode = '55000', message = 'inbound_commercial_case_admission_is_immutable';
end;
$function$;

create trigger inbound_commercial_case_admissions_immutable
before insert or update or delete on public.inbound_commercial_case_admissions
for each row execute function public.protect_inbound_commercial_case_admission();

create table public.inbound_commercial_case_conflicts (
    id uuid primary key default gen_random_uuid(),
    admission_id uuid not null
        references public.inbound_commercial_case_admissions(id) on delete restrict,
    observed_external_user_id text not null check (observed_external_user_id ~ '^[0-9]+$'),
    reason text not null check (reason = any (array['input_mismatch', 'canonical_drift'])),
    created_at timestamptz not null default clock_timestamp(),
    unique (admission_id, observed_external_user_id, reason)
);

alter table public.inbound_commercial_case_conflicts enable row level security;

create trigger inbound_commercial_case_conflicts_immutable
before update or delete on public.inbound_commercial_case_conflicts
for each row execute function public.protect_inbound_commercial_case_admission();

create table public.commercial_case_intent_correlations (
    id uuid primary key default gen_random_uuid(),
    commercial_case_id uuid not null
        references public.commercial_cases(id) on delete cascade,
    observation_source text not null
        check (observation_source = any (array['precheckout', 'hotmart', 'manual'])),
    observation_ref text not null check (length(btrim(observation_ref)) > 0),
    resolution_status text not null check (
        resolution_status = any (
            array['resolved', 'candidate', 'ambiguous', 'conflict', 'unmatched']
        )
    ),
    evidence_ref text not null check (length(btrim(evidence_ref)) > 0),
    created_at timestamptz not null default clock_timestamp(),
    unique (commercial_case_id, observation_source, observation_ref)
);

alter table public.commercial_case_intent_correlations enable row level security;

create function public.protect_commercial_case_intent_correlation()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    raise exception using errcode = '55000', message = 'commercial_case_intent_correlation_is_immutable';
end;
$function$;

create trigger commercial_case_intent_correlations_immutable
before update or delete on public.commercial_case_intent_correlations
for each row execute function public.protect_commercial_case_intent_correlation();

create unique index commercial_cases_live_inbound_conversation_scope_idx
on public.commercial_cases (
    conversation_id,
    inbound_scope_key,
    inbound_scope_version,
    product_ref,
    coalesce(offer_ref, '')
)
where case_kind = 'inbound_sales'
  and status in ('active', 'paused');

drop trigger commercial_cases_protect_shadow on public.commercial_cases;

create trigger commercial_cases_protect_shadow
before insert or update on public.commercial_cases
for each row
when (new.case_kind <> 'inbound_sales')
execute function public.protect_commercial_case_shadow();

create trigger commercial_cases_protect_shadow_delete
before delete on public.commercial_cases
for each row
when (old.case_kind <> 'inbound_sales')
execute function public.protect_commercial_case_shadow();

create function public.protect_inbound_commercial_case()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    if tg_op <> 'INSERT' then
        raise exception using errcode = '55000', message = 'inbound_commercial_case_is_immutable';
    end if;
    if new.case_kind <> 'inbound_sales'
       or new.recovery_case_id is not null
       or new.status <> 'active'
       or new.automation_status <> 'draft_only'
       or new.identity_resolution_status <> 'resolved'
       or new.authority_mode <> 'shadow'
       or new.version <> 1
       or new.selected_channel_identity_id is null
       or new.conversation_id is null
       or new.inbound_scope_key is null
       or new.inbound_scope_version is null
       or nullif(btrim(new.tenant_ref), '') is null
       or nullif(btrim(new.product_ref), '') is null
       or nullif(btrim(new.offer_ref), '') is null then
        raise exception using errcode = '23514', message = 'invalid_inbound_commercial_case_state';
    end if;
    if not exists (
        select 1
        from public.inbound_commercial_scope_versions scope
        join public.channel_identities identity
          on identity.channel = 'whatsapp'
         and identity.account_id = 'chatwoot:' || scope.chatwoot_account_id::text
         and identity.metadata ->> 'inbox_id' = scope.chatwoot_inbox_id::text
        join public.conversations conversation
          on conversation.channel_identity_id = identity.id
         and conversation.contact_id = identity.contact_id
        where scope.scope_key = new.inbound_scope_key
          and scope.version = new.inbound_scope_version
          and scope.status = 'published'
          and identity.id = new.selected_channel_identity_id
          and identity.contact_id = new.contact_id
          and identity.identity_status = 'active'
          and conversation.id = new.conversation_id
          and conversation.commercial_context ->> 'chatwoot_conversation_id'
              ~ '^[1-9][0-9]*$'
          and conversation.commercial_context = jsonb_build_object(
              'chatwoot_conversation_id',
              conversation.commercial_context ->> 'chatwoot_conversation_id'
          )
    ) then
        raise exception using errcode = '23514', message = 'inbound_commercial_case_canonical_mismatch';
    end if;
    if not exists (
        select 1
        from public.inbound_commercial_scope_versions scope
        where scope.scope_key = new.inbound_scope_key
          and scope.version = new.inbound_scope_version
          and scope.status = 'published'
          and scope.tenant_key = new.tenant_ref
          and scope.external_product_id = new.product_ref
          and scope.offer_code = new.offer_ref
    ) then
        raise exception using errcode = '23514', message = 'inbound_commercial_case_scope_mismatch';
    end if;
    return new;
end;
$function$;

create trigger commercial_cases_protect_inbound
before insert or update on public.commercial_cases
for each row
when (new.case_kind = 'inbound_sales')
execute function public.protect_inbound_commercial_case();

create trigger commercial_cases_protect_inbound_delete
before delete on public.commercial_cases
for each row
when (old.case_kind = 'inbound_sales')
execute function public.protect_inbound_commercial_case();

create function public.admit_inbound_commercial_case(
    p_scope_key text,
    p_scope_version integer,
    p_external_conversation_id bigint,
    p_external_user_id text
)
returns table (
    outcome text,
    commercial_case_id uuid,
    contact_id uuid,
    channel_identity_id uuid,
    conversation_id uuid,
    automation_status text
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_scope public.inbound_commercial_scope_versions%rowtype;
    v_existing public.inbound_commercial_case_admissions%rowtype;
    v_identity public.channel_identities%rowtype;
    v_conversation public.conversations%rowtype;
    v_anchor_identity public.channel_identities%rowtype;
    v_anchor_conversation public.conversations%rowtype;
    v_anchor_count integer;
    v_contact_id uuid;
    v_case_id uuid;
    v_conflict_reason text;
begin
    if p_scope_key is null
       or p_scope_key !~ '^[a-z0-9_-]{1,100}$'
       or p_scope_version is null or p_scope_version < 1
       or p_external_conversation_id is null or p_external_conversation_id < 1
       or p_external_user_id is null or p_external_user_id !~ '^[0-9]+$' then
        raise exception using errcode = '22023', message = 'invalid_inbound_commercial_case_parameters';
    end if;

    select scope.* into v_scope
    from public.inbound_commercial_scope_versions scope
    where scope.scope_key = p_scope_key
      and scope.version = p_scope_version
      and scope.status = 'published'
    for share;
    if not found then
        raise exception using errcode = '55000', message = 'inbound_commercial_scope_unavailable';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        concat_ws(':', 'inbound-commercial-case', p_scope_key,
                  p_scope_version, p_external_conversation_id), 0
    ));
    perform pg_advisory_xact_lock(hashtextextended(
        concat_ws(':', 'chatwoot-conversation-owner',
                  v_scope.chatwoot_account_id, p_external_conversation_id), 0
    ));
    perform pg_advisory_xact_lock(hashtextextended(
        concat_ws(':', 'chatwoot-channel-identity',
                  v_scope.chatwoot_account_id, v_scope.chatwoot_inbox_id,
                  p_external_user_id), 0
    ));

    select admission.* into v_existing
    from public.inbound_commercial_case_admissions admission
    where admission.scope_key = p_scope_key
      and admission.scope_version = p_scope_version
      and admission.external_conversation_id = p_external_conversation_id
    for update;

    if v_existing.id is not null
       and v_existing.external_user_id <> p_external_user_id then
        insert into public.inbound_commercial_case_conflicts (
            admission_id, observed_external_user_id, reason
        ) values (
            v_existing.id, p_external_user_id, 'input_mismatch'
        ) on conflict do nothing;
        outcome := 'evidence_conflict';
        commercial_case_id := v_existing.commercial_case_id;
        contact_id := v_existing.contact_id;
        channel_identity_id := v_existing.channel_identity_id;
        conversation_id := v_existing.conversation_id;
        automation_status := 'draft_only';
        return next;
        return;
    end if;

    if v_existing.id is not null then
        v_conflict_reason := null;
        select identity.* into v_identity
        from public.channel_identities identity
        where identity.id = v_existing.channel_identity_id
          and identity.contact_id = v_existing.contact_id
          and identity.channel = 'whatsapp'
          and identity.account_id = 'chatwoot:' || v_scope.chatwoot_account_id::text
          and identity.metadata ->> 'inbox_id' = v_scope.chatwoot_inbox_id::text
          and identity.external_user_id = p_external_user_id
        for share;
        if not found or v_identity.identity_status <> 'active' then
            v_conflict_reason := 'canonical_drift';
        else
            select conversation.* into v_conversation
            from public.conversations conversation
            where conversation.id = v_existing.conversation_id
              and conversation.channel_identity_id = v_identity.id
              and conversation.contact_id = v_identity.contact_id
              and conversation.commercial_context = jsonb_build_object(
                  'chatwoot_conversation_id', p_external_conversation_id::text
              )
              and conversation.status in (
                  'active', 'awaiting_agent', 'awaiting_contact',
                  'snoozed', 'paused_human'
              )
            for share;
            if not found then
                v_conflict_reason := 'canonical_drift';
            end if;
        end if;
        if v_conflict_reason is null then
            outcome := 'already_exists';
        else
            insert into public.inbound_commercial_case_conflicts (
                admission_id, observed_external_user_id, reason
            ) values (
                v_existing.id, p_external_user_id, v_conflict_reason
            ) on conflict do nothing;
            outcome := 'evidence_conflict';
        end if;
        commercial_case_id := v_existing.commercial_case_id;
        contact_id := v_existing.contact_id;
        channel_identity_id := v_existing.channel_identity_id;
        conversation_id := v_existing.conversation_id;
        automation_status := 'draft_only';
        return next;
        return;
    end if;

    select count(*) into strict v_anchor_count
    from public.conversations conversation
    join public.channel_identities identity
      on identity.id = conversation.channel_identity_id
     and identity.contact_id = conversation.contact_id
    where identity.channel = 'whatsapp'
      and identity.account_id = 'chatwoot:' || v_scope.chatwoot_account_id::text
      and conversation.commercial_context ->> 'chatwoot_conversation_id'
          = p_external_conversation_id::text;
    if v_anchor_count > 1 then
        raise exception using errcode = '21000', message = 'inbound_external_conversation_ownership_ambiguous';
    elsif v_anchor_count = 1 then
        select identity.* into strict v_anchor_identity
        from public.conversations conversation
        join public.channel_identities identity
          on identity.id = conversation.channel_identity_id
         and identity.contact_id = conversation.contact_id
        where identity.channel = 'whatsapp'
          and identity.account_id = 'chatwoot:' || v_scope.chatwoot_account_id::text
          and conversation.commercial_context ->> 'chatwoot_conversation_id'
              = p_external_conversation_id::text
        for update of identity;
        select conversation.* into strict v_anchor_conversation
        from public.conversations conversation
        where conversation.channel_identity_id = v_anchor_identity.id
          and conversation.contact_id = v_anchor_identity.contact_id
          and conversation.commercial_context ->> 'chatwoot_conversation_id'
              = p_external_conversation_id::text
        for update;
        if v_anchor_identity.external_user_id is distinct from p_external_user_id
           or v_anchor_identity.identity_status <> 'active'
           or v_anchor_identity.metadata ->> 'inbox_id'
              is distinct from v_scope.chatwoot_inbox_id::text then
            raise exception using errcode = '23505', message = 'inbound_external_conversation_owned_by_another_identity';
        end if;
        if v_anchor_conversation.commercial_context <> jsonb_build_object(
            'chatwoot_conversation_id', p_external_conversation_id::text
        ) then
            raise exception using errcode = '22000', message = 'inbound_canonical_conversation_conflict';
        end if;
    end if;

    select identity.* into v_identity
    from public.channel_identities identity
    where identity.channel = 'whatsapp'
      and identity.account_id = 'chatwoot:' || v_scope.chatwoot_account_id::text
      and identity.external_user_id = p_external_user_id
    for update;

    if not found then
        if exists (
            select 1
            from public.channel_identities identity
            where identity.channel = 'whatsapp'
              and identity.account_id = 'chatwoot:' || v_scope.chatwoot_account_id::text
              and identity.external_conversation_id = p_external_conversation_id::text
        ) then
            raise exception using errcode = '23505', message = 'inbound_external_conversation_owned_by_another_identity';
        end if;
        insert into public.contacts (metadata)
        values (jsonb_build_object('source', 'chatwoot_inbound'))
        returning id into strict v_contact_id;
        insert into public.channel_identities (
            contact_id, channel, account_id, external_user_id,
            external_conversation_id, identity_status, metadata
        ) values (
            v_contact_id, 'whatsapp',
            'chatwoot:' || v_scope.chatwoot_account_id::text,
            p_external_user_id, p_external_conversation_id::text, 'active',
            jsonb_build_object('inbox_id', v_scope.chatwoot_inbox_id::text)
        ) returning * into strict v_identity;
    else
        if v_identity.identity_status <> 'active'
           or v_identity.metadata ->> 'inbox_id'
              is distinct from v_scope.chatwoot_inbox_id::text then
            raise exception using errcode = '22000', message = 'inbound_canonical_identity_conflict';
        end if;
        if v_identity.external_conversation_id
           is distinct from p_external_conversation_id::text then
            if exists (
                select 1
                from public.channel_identities other_identity
                where other_identity.channel = 'whatsapp'
                  and other_identity.account_id = v_identity.account_id
                  and other_identity.external_conversation_id = p_external_conversation_id::text
                  and other_identity.id <> v_identity.id
            ) then
                raise exception using errcode = '23505', message = 'inbound_external_conversation_owned_by_another_identity';
            end if;
            update public.channel_identities
            set external_conversation_id = p_external_conversation_id::text,
                updated_at = clock_timestamp()
            where id = v_identity.id
            returning * into strict v_identity;
        end if;
    end if;

    select conversation.* into v_conversation
    from public.conversations conversation
    where conversation.channel_identity_id = v_identity.id
      and conversation.contact_id = v_identity.contact_id
      and conversation.commercial_context = jsonb_build_object(
          'chatwoot_conversation_id', p_external_conversation_id::text
      )
    for update;
    if not found then
        insert into public.conversations (
            contact_id, channel_identity_id, status, automation_status,
            human_takeover, commercial_context
        ) values (
            v_identity.contact_id, v_identity.id, 'active', 'draft_only', false,
            jsonb_build_object(
                'chatwoot_conversation_id', p_external_conversation_id::text
            )
        ) returning * into strict v_conversation;
    elsif v_conversation.status not in (
              'active', 'awaiting_agent', 'awaiting_contact',
              'snoozed', 'paused_human'
          )
          or v_conversation.automation_status <> 'draft_only' then
        raise exception using errcode = '22000', message = 'inbound_canonical_conversation_conflict';
    end if;

    v_case_id := gen_random_uuid();
    insert into public.commercial_cases (
        id, case_kind, contact_id, selected_channel_identity_id,
        conversation_id, inbound_scope_key, inbound_scope_version, tenant_ref,
        product_ref, offer_ref, status, automation_status,
        identity_resolution_status, authority_mode, version
    ) values (
        v_case_id, 'inbound_sales', v_identity.contact_id, v_identity.id,
        v_conversation.id, p_scope_key, p_scope_version, v_scope.tenant_key,
        v_scope.external_product_id, v_scope.offer_code,
        'active', 'draft_only', 'resolved', 'shadow', 1
    );
    insert into public.inbound_commercial_case_admissions (
        scope_key, scope_version, external_conversation_id, external_user_id,
        commercial_case_id, contact_id, channel_identity_id, conversation_id
    ) values (
        p_scope_key, p_scope_version, p_external_conversation_id, p_external_user_id,
        v_case_id, v_identity.contact_id, v_identity.id, v_conversation.id
    );

    outcome := 'created';
    commercial_case_id := v_case_id;
    contact_id := v_identity.contact_id;
    channel_identity_id := v_identity.id;
    conversation_id := v_conversation.id;
    automation_status := 'draft_only';
    return next;
end;
$function$;

revoke all on table public.inbound_commercial_scope_versions from public;
revoke all on table public.inbound_commercial_case_admissions from public;
revoke all on table public.inbound_commercial_case_conflicts from public;
revoke all on table public.commercial_case_intent_correlations from public;
revoke execute on function public.protect_inbound_commercial_scope_version() from public;
revoke execute on function public.protect_inbound_commercial_case_admission() from public;
revoke execute on function public.protect_commercial_case_intent_correlation() from public;
revoke execute on function public.protect_inbound_commercial_case() from public;
revoke execute on function public.admit_inbound_commercial_case(text, integer, bigint, text) from public;

do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on table public.inbound_commercial_scope_versions from anon;
        revoke all on table public.inbound_commercial_case_admissions from anon;
        revoke all on table public.inbound_commercial_case_conflicts from anon;
        revoke all on table public.commercial_case_intent_correlations from anon;
        revoke execute on function public.protect_inbound_commercial_scope_version() from anon;
        revoke execute on function public.protect_inbound_commercial_case_admission() from anon;
        revoke execute on function public.protect_commercial_case_intent_correlation() from anon;
        revoke execute on function public.protect_inbound_commercial_case() from anon;
        revoke execute on function public.admit_inbound_commercial_case(text, integer, bigint, text) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on table public.inbound_commercial_scope_versions from authenticated;
        revoke all on table public.inbound_commercial_case_admissions from authenticated;
        revoke all on table public.inbound_commercial_case_conflicts from authenticated;
        revoke all on table public.commercial_case_intent_correlations from authenticated;
        revoke execute on function public.protect_inbound_commercial_scope_version() from authenticated;
        revoke execute on function public.protect_inbound_commercial_case_admission() from authenticated;
        revoke execute on function public.protect_commercial_case_intent_correlation() from authenticated;
        revoke execute on function public.protect_inbound_commercial_case() from authenticated;
        revoke execute on function public.admit_inbound_commercial_case(text, integer, bigint, text) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        revoke all on table public.inbound_commercial_scope_versions from service_role;
        revoke all on table public.inbound_commercial_case_admissions from service_role;
        revoke all on table public.inbound_commercial_case_conflicts from service_role;
        revoke all on table public.commercial_case_intent_correlations from service_role;
        revoke execute on function public.protect_inbound_commercial_scope_version() from service_role;
        revoke execute on function public.protect_inbound_commercial_case_admission() from service_role;
        revoke execute on function public.protect_commercial_case_intent_correlation() from service_role;
        revoke execute on function public.protect_inbound_commercial_case() from service_role;
        grant execute on function public.admit_inbound_commercial_case(text, integer, bigint, text) to service_role;
    end if;
end;
$roles$;

commit;