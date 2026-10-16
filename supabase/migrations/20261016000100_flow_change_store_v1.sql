-- flow-change-store-v1: el almacen de las definiciones de flujo (1b, pieza 1).
-- Contratos: docs/contracts/flow-change-cycle-v1.md y
-- docs/contracts/flow-definition-v1.md.
--
-- Agrega cuatro tablas vacias (flow_controls, flow_definitions,
-- flow_change_proposals y flow_change_events), sus disparadores, los helpers
-- internos y siete funciones que ejecuta solo el duenio de la base. Nada lee el
-- almacen para mandar un mensaje: publicar o pausar aca no cambia ningun envio
-- hasta que exista el motor (pieza 2).
--
-- Lo que no hace:
--   * no crea ni modifica roles, membresias ni la configuracion de un rol;
--   * no cambia el ACL del esquema public ni los privilegios por defecto;
--   * no redefine ninguna funcion que exista: todas se crean con create
--     function, asi que una firma repetida hace fallar la migracion;
--   * no agrega disparadores ni claves foraneas a tablas que existan, y no
--     toca ninguna fila. El binding de la instancia se mira adentro de las
--     funciones, sin clave foranea hacia commercial_ally_runtime_bindings.
--
-- Todas las funciones son security invoker y tienen el search_path fijo.
-- Ningun rol de la API (anon, authenticated, service_role) ejecuta nada de
-- esto ni tiene permisos sobre las tablas: el bloque final revoca todo, porque
-- los privilegios por defecto de Supabase conceden directo a esos roles.
--
-- Errores: 22023 entrada invalida; P0002 no existe o no es de esta instancia,
-- sin distinguir; 55000 un estado que no lo permite; 23514 un invariante del
-- almacen. Los resultados del ciclo (base vieja, revision vieja, vencida...)
-- vuelven como {"ok": false, "reason_code": ...} y dejan su evento.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- ---------------------------------------------------------------------------
-- 1. Los helpers de forma que usan los CHECK.
-- ---------------------------------------------------------------------------

-- La forma del actor, por clase.
create function public._flow_store_actor_is_valid(p_kind text, p_ref text)
returns boolean
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select coalesce(case p_kind
        when 'operador' then p_ref ~ '^[a-z][a-z0-9_-]{1,31}$'
        when 'slack' then p_ref ~ '^[UW][A-Z0-9]{8,20}$'
        when 'automation_expert' then p_ref ~ '^[UW][A-Z0-9]{8,20}$'
        when 'sistema' then p_ref ~ '^[a-z][a-z0-9_]{1,31}$'
    end, false)
$function$;

-- El texto libre: una linea de 1 a 500 caracteres, sin controles C0 ni C1, sin
-- guion suave, sin marcas de direccion ni de ancho cero, sin aislantes bidi y
-- sin BOM. La clase es explicita: [[:cntrl:]] no ve RLO y depende del locale.
create function public._flow_store_free_text_is_valid(p_text text)
returns boolean
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select coalesce(
        length(btrim(p_text)) between 1 and 500
        and p_text !~ '[\u0001-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]',
        false)
$function$;

-- ---------------------------------------------------------------------------
-- 2. Las tablas.
-- ---------------------------------------------------------------------------

create table public.flow_change_proposals (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    status text not null check (status in
        ('borrador', 'validada', 'aprobada', 'publicada', 'descartada', 'vencida')),
    revision integer not null check (revision > 0),
    items jsonb not null check (jsonb_typeof(items) = 'array'
        and jsonb_array_length(items) between 1 and 10
        and octet_length(items::text) <= 262144),
    draft_base jsonb not null check (jsonb_typeof(draft_base) = 'object'),
    base jsonb check (base is null or jsonb_typeof(base) = 'object'),
    purpose text not null check (public._flow_store_free_text_is_valid(purpose)),
    requested_by_kind text not null check (requested_by_kind in ('automation_expert', 'operador')),
    requested_by_ref text not null,
    validation jsonb check (validation is null or (jsonb_typeof(validation) = 'object'
        and octet_length(validation::text) <= 196608)),
    fingerprint text check (fingerprint ~ '^[0-9a-f]{64}$'),
    validated_at timestamptz,
    expires_at timestamptz,
    approved_at timestamptz,
    approved_by_kind text check (approved_by_kind in ('operador', 'slack')),
    approved_by_ref text,
    published_at timestamptz,
    closed_at timestamptz,
    closed_by_kind text check (closed_by_kind in ('automation_expert', 'operador', 'slack', 'sistema')),
    closed_by_ref text,
    close_reason text check (close_reason is null or public._flow_store_free_text_is_valid(close_reason)),
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    constraint flow_change_proposals_requested_by
        check (public._flow_store_actor_is_valid(requested_by_kind, requested_by_ref)),
    constraint flow_change_proposals_approved_by
        check ((approved_by_kind is null and approved_by_ref is null)
               or public._flow_store_actor_is_valid(approved_by_kind, approved_by_ref)),
    constraint flow_change_proposals_closed_by
        check ((closed_by_kind is null and closed_by_ref is null)
               or public._flow_store_actor_is_valid(closed_by_kind, closed_by_ref)),
    constraint flow_change_proposals_state_coherent check (case status
        when 'borrador' then base is null and fingerprint is null and validated_at is null
            and expires_at is null and approved_at is null and approved_by_kind is null
            and published_at is null and closed_at is null and closed_by_kind is null
            and close_reason is null
        when 'validada' then base is not null and fingerprint is not null and validation is not null
            and validated_at is not null and expires_at = validated_at + interval '24 hours'
            and approved_at is null and approved_by_kind is null and published_at is null
            and closed_at is null and closed_by_kind is null
        when 'aprobada' then base is not null and fingerprint is not null and validation is not null
            and validated_at is not null and expires_at = validated_at + interval '24 hours'
            and approved_at is not null and approved_by_kind is not null and published_at is null
            and closed_at is null and closed_by_kind is null
        when 'publicada' then base is not null and fingerprint is not null
            and approved_at is not null and approved_by_kind is not null and published_at is not null
            and closed_at is null and closed_by_kind is null
        when 'descartada' then closed_at is not null and closed_by_kind is not null
            and close_reason is not null and published_at is null
        when 'vencida' then fingerprint is not null and expires_at is not null
            and closed_at is not null and closed_by_kind = 'sistema' and published_at is null
    end)
);

create table public.flow_controls (
    flow_id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    product_ref text not null check (product_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    flow_key text not null check (flow_key ~ '^[a-z][a-z0-9_]{1,39}$'),
    state text not null check (state in ('apagado', 'prendido', 'pausado')),
    active_version integer check (active_version > 0),
    generation bigint not null check (generation >= 0),
    change_kind text not null check (change_kind in ('registrado', 'publicado', 'pausado')),
    changed_by_kind text not null check (changed_by_kind in ('automation_expert', 'operador', 'slack')),
    changed_by_ref text not null,
    changed_by_proposal_id uuid references public.flow_change_proposals (id) on delete restrict,
    change_reason text not null check (public._flow_store_free_text_is_valid(change_reason)),
    changed_at timestamptz not null default clock_timestamp(),
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    constraint flow_controls_identity unique (tenant_ref, product_ref, flow_key),
    constraint flow_controls_changed_by
        check (public._flow_store_actor_is_valid(changed_by_kind, changed_by_ref)),
    constraint flow_controls_on_needs_version check (state = 'apagado' or active_version is not null),
    constraint flow_controls_publish_has_proposal
        check ((change_kind = 'publicado') = (changed_by_proposal_id is not null))
);

create table public.flow_definitions (
    flow_id uuid not null references public.flow_controls (flow_id) on delete restrict,
    version integer not null check (version > 0),
    schema_ref text not null check (schema_ref = 'setter-flujo/v1'),
    definition jsonb not null check (jsonb_typeof(definition) = 'object'
        and octet_length(definition::text) <= 65536),
    definition_sha256 text not null check (definition_sha256 ~ '^[0-9a-f]{64}$'),
    proposal_id uuid not null references public.flow_change_proposals (id) on delete restrict,
    approved_by_kind text not null check (approved_by_kind in ('operador', 'slack')),
    approved_by_ref text not null,
    published_at timestamptz not null default clock_timestamp(),
    primary key (flow_id, version),
    constraint flow_definitions_approved_by
        check (public._flow_store_actor_is_valid(approved_by_kind, approved_by_ref))
);

-- La version activa de un control es una version publicada de ese flujo. Con la
-- version nula (registrado y sin publicar), MATCH SIMPLE no la verifica.
alter table public.flow_controls
    add constraint flow_controls_active_version_fk
    foreign key (flow_id, active_version)
    references public.flow_definitions (flow_id, version) on delete restrict;

-- El id es un uuid y no un identity: una secuencia recibiria permisos por los
-- privilegios por defecto de Supabase.
create table public.flow_change_events (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null check (tenant_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    ally_ref text not null check (ally_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
    proposal_id uuid references public.flow_change_proposals (id) on delete restrict,
    flow_id uuid references public.flow_controls (flow_id) on delete restrict,
    event_type text not null check (event_type in (
        'flow_change_draft_saved', 'flow_change_validation_blocked',
        'flow_change_validation_rejected', 'flow_change_validated',
        'flow_change_approved', 'flow_change_approval_rejected',
        'flow_change_publish_rejected', 'flow_change_published',
        'flow_control_changed', 'flow_change_discarded', 'flow_change_expired')),
    actor_kind text not null check (actor_kind in ('automation_expert', 'operador', 'slack', 'sistema')),
    actor_ref text not null,
    reason_code text not null check (reason_code ~ '^[a-z][a-z0-9_:]{0,79}$'),
    generation bigint check (generation >= 0),
    data jsonb not null default '{}'::jsonb check (jsonb_typeof(data) = 'object'
        and octet_length(data::text) <= 65536),
    created_at timestamptz not null default clock_timestamp(),
    constraint flow_change_events_subject check (proposal_id is not null or flow_id is not null),
    constraint flow_change_events_actor check (public._flow_store_actor_is_valid(actor_kind, actor_ref))
);

-- ---------------------------------------------------------------------------
-- 3. Los indices y RLS.
-- ---------------------------------------------------------------------------

-- El orden en que se bloquean los controles de una instancia.
create index flow_controls_instance_idx on public.flow_controls (tenant_ref, ally_ref, flow_id);
create index flow_definitions_proposal_idx on public.flow_definitions (proposal_id);
create index flow_change_proposals_instance_time_idx
    on public.flow_change_proposals (tenant_ref, ally_ref, created_at desc);
create index flow_change_proposals_open_idx on public.flow_change_proposals (tenant_ref, ally_ref)
    where status in ('borrador', 'validada', 'aprobada');
create index flow_change_events_proposal_time_idx on public.flow_change_events (proposal_id, created_at);
create index flow_change_events_flow_time_idx on public.flow_change_events (flow_id, created_at);
create index flow_change_events_instance_time_idx
    on public.flow_change_events (tenant_ref, ally_ref, created_at desc);

alter table public.flow_change_proposals enable row level security;
alter table public.flow_controls enable row level security;
alter table public.flow_definitions enable row level security;
alter table public.flow_change_events enable row level security;

-- ---------------------------------------------------------------------------
-- 4. Las cinco funciones de disparador y los 14 disparadores.
-- ---------------------------------------------------------------------------

-- Una sola funcion para todo lo que no se borra ni se modifica.
create function public.reject_flow_store_mutation()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    raise exception using errcode = '55000', message = case tg_table_name
        when 'flow_definitions' then 'flow_definition_is_immutable'
        when 'flow_change_events' then 'flow_change_event_is_append_only'
        when 'flow_controls' then 'flow_control_is_permanent'
        else 'flow_change_proposal_is_permanent'
    end;
end;
$function$;

-- Un flujo nace apagado, todo cambio sube la generacion en 1 y las
-- transiciones de estado son las del contrato, solo si el estado cambia.
create function public.validate_flow_control_change()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if tg_op = 'INSERT' then
        if not coalesce(new.state = 'apagado' and new.generation = 0
                        and new.active_version is null and new.change_kind = 'registrado'
                        and new.changed_by_proposal_id is null, false) then
            raise exception using errcode = '23514', message = 'flow_control_must_start_off';
        end if;
        return new;
    end if;

    if row(new.flow_id, new.tenant_ref, new.ally_ref, new.product_ref, new.flow_key, new.created_at)
       is distinct from row(old.flow_id, old.tenant_ref, old.ally_ref, old.product_ref, old.flow_key, old.created_at) then
        raise exception using errcode = '55000', message = 'flow_control_identity_is_immutable';
    end if;
    if new.generation is distinct from old.generation + 1 then
        raise exception using errcode = '23514', message = 'flow_control_generation_must_increment';
    end if;
    if new.change_kind = 'registrado' then
        raise exception using errcode = '23514', message = 'flow_control_change_kind_invalid';
    end if;
    if old.active_version is not null and new.active_version is null then
        raise exception using errcode = '23514', message = 'flow_control_version_cannot_be_cleared';
    end if;
    -- Publicar sobre un flujo prendido es prendido -> prendido: no es una
    -- transicion, y la version activa puede cambiar en cualquier estado.
    if new.state is distinct from old.state and not (
        (old.state = 'apagado' and new.state = 'prendido')
        or (old.state = 'prendido' and new.state in ('pausado', 'apagado'))
        or (old.state = 'pausado' and new.state in ('prendido', 'apagado'))
    ) then
        raise exception using errcode = '23514', message = 'flow_control_transition_invalid';
    end if;
    if new.change_kind = 'pausado' and not coalesce(
        old.state = 'prendido' and new.state = 'pausado'
        and new.active_version is not distinct from old.active_version, false) then
        raise exception using errcode = '23514', message = 'flow_control_change_kind_invalid';
    end if;
    return new;
end;
$function$;

-- Todo cambio de un control deja su evento, lo haga una funcion o un UPDATE
-- directo del duenio. Por eso las funciones no escriben eventos de control.
create function public.record_flow_control_change()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    insert into public.flow_change_events (
        tenant_ref, ally_ref, proposal_id, flow_id, event_type, actor_kind, actor_ref,
        reason_code, generation, data)
    values (
        new.tenant_ref, new.ally_ref, new.changed_by_proposal_id, new.flow_id,
        'flow_control_changed', new.changed_by_kind, new.changed_by_ref,
        new.change_kind, new.generation,
        jsonb_build_object(
            'from_state', case when tg_op = 'UPDATE' then old.state end,
            'to_state', new.state,
            'from_version', case when tg_op = 'UPDATE' then old.active_version end,
            'to_version', new.active_version,
            'change_reason_sha256',
                encode(sha256(convert_to(new.change_reason, 'UTF8')), 'hex')));
    return null;
end;
$function$;

-- Los cuatro invariantes del almacen, que no dependen de ninguna entrada:
-- 1. la identidad de la definicion es la del control y la de la fila;
-- 2. la version es la siguiente del flujo, bajo el lock del control;
-- 3. nada sin resolver (a_medir, a_decidir o a_crear en cualquier rama);
-- 4. la zona: solo con un horario objeto, y la del flujo es la primera que
--    tuvo, aunque en el medio haya versiones con horario: siempre.
-- La huella de la definicion la calcula el disparador: no se confia en la que
-- llega.
create function public.validate_flow_definition_insert()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_control public.flow_controls%rowtype;
    v_next integer;
    v_zone text;
    v_fixed_zone text;
begin
    select * into v_control from public.flow_controls c where c.flow_id = new.flow_id for update;
    if not found then
        raise exception using errcode = '23503', message = 'flow_definition_flow_not_found';
    end if;

    -- 1. La identidad.
    if not coalesce(
        new.definition ->> 'esquema' = new.schema_ref
        and new.definition ->> 'flujo' = v_control.flow_key
        and new.definition ->> 'producto' = v_control.product_ref
        and jsonb_typeof(new.definition -> 'version') = 'number'
        and new.definition ->> 'version' = new.version::text
        and new.definition ->> 'estado' = 'publicada', false) then
        raise exception using errcode = '23514', message = 'flow_definition_identity_mismatch';
    end if;

    -- 2. La version correlativa.
    select coalesce(max(d.version), 0) + 1 into v_next
      from public.flow_definitions d where d.flow_id = new.flow_id;
    if new.version is distinct from v_next then
        raise exception using errcode = '23514', message = 'flow_definition_version_not_next';
    end if;

    -- 3. Nada sin resolver (regla 11 del esquema).
    if jsonb_path_exists(new.definition, 'strict $.** ? (@ == "a_medir" || @ == "a_decidir")')
       or jsonb_path_exists(new.definition, 'strict $.**.a_crear') then
        raise exception using errcode = '23514', message = 'flow_definition_unresolved_value';
    end if;

    -- 4. La zona.
    if jsonb_typeof(new.definition -> 'horario') = 'object' then
        v_zone := new.definition #>> '{horario,zona}';
        if v_zone is null or not exists (select 1 from pg_timezone_names tz where tz.name = v_zone) then
            raise exception using errcode = '22023', message = 'flow_definition_zone_invalid';
        end if;
        select d.definition #>> '{horario,zona}' into v_fixed_zone
          from public.flow_definitions d
         where d.flow_id = new.flow_id and jsonb_typeof(d.definition -> 'horario') = 'object'
         order by d.version
         limit 1;
        if v_fixed_zone is not null and v_fixed_zone is distinct from v_zone then
            raise exception using errcode = '23514', message = 'flow_definition_zone_must_remain_constant';
        end if;
    elsif new.definition -> 'horario' is distinct from '"siempre"'::jsonb then
        raise exception using errcode = '22023', message = 'flow_definition_zone_invalid';
    end if;

    new.definition_sha256 := encode(sha256(convert_to(new.definition::text, 'UTF8')), 'hex');
    return new;
end;
$function$;

-- El recorrido de la propuesta. Desde validada, el contenido, las dos bases,
-- la huella, las fechas y la validacion quedan congelados; desde aprobada,
-- tambien la aprobacion; un estado final no acepta ningun UPDATE.
create function public.validate_flow_change_proposal_change()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if old.status in ('publicada', 'descartada', 'vencida') then
        raise exception using errcode = '55000', message = 'flow_change_proposal_frozen';
    end if;
    if row(new.id, new.tenant_ref, new.ally_ref, new.requested_by_kind, new.requested_by_ref, new.created_at)
       is distinct from row(old.id, old.tenant_ref, old.ally_ref, old.requested_by_kind, old.requested_by_ref, old.created_at) then
        raise exception using errcode = '55000', message = 'flow_change_proposal_identity_is_immutable';
    end if;
    if new.status is distinct from old.status and not (
        (old.status = 'borrador' and new.status in ('validada', 'descartada'))
        or (old.status = 'validada' and new.status in ('aprobada', 'descartada', 'vencida'))
        or (old.status = 'aprobada' and new.status in ('publicada', 'descartada', 'vencida'))
    ) then
        raise exception using errcode = '23514', message = 'flow_change_transition_invalid';
    end if;
    if old.status = 'borrador' then
        if new.status = 'borrador' then
            if row(new.items, new.draft_base, new.purpose) is distinct from row(old.items, old.draft_base, old.purpose)
               and new.revision is distinct from old.revision + 1 then
                raise exception using errcode = '23514', message = 'flow_change_revision_must_increment';
            end if;
        elsif row(new.items, new.draft_base, new.purpose, new.revision)
              is distinct from row(old.items, old.draft_base, old.purpose, old.revision) then
            raise exception using errcode = '55000', message = 'flow_change_proposal_frozen';
        end if;
    else
        if row(new.items, new.draft_base, new.base, new.purpose, new.revision, new.fingerprint,
               new.validated_at, new.expires_at, new.validation)
           is distinct from row(old.items, old.draft_base, old.base, old.purpose, old.revision, old.fingerprint,
               old.validated_at, old.expires_at, old.validation) then
            raise exception using errcode = '55000', message = 'flow_change_proposal_frozen';
        end if;
        if old.status = 'aprobada'
           and row(new.approved_at, new.approved_by_kind, new.approved_by_ref)
               is distinct from row(old.approved_at, old.approved_by_kind, old.approved_by_ref) then
            raise exception using errcode = '55000', message = 'flow_change_proposal_frozen';
        end if;
    end if;
    return new;
end;
$function$;

create trigger flow_controls_validate
before insert or update on public.flow_controls
for each row execute function public.validate_flow_control_change();
create trigger flow_controls_record_change
after insert or update on public.flow_controls
for each row execute function public.record_flow_control_change();
create trigger flow_controls_permanent
before delete on public.flow_controls
for each row execute function public.reject_flow_store_mutation();
create trigger flow_controls_no_truncate
before truncate on public.flow_controls
for each statement execute function public.reject_flow_store_mutation();
create trigger flow_controls_set_updated_at
before update on public.flow_controls
for each row execute function public.set_updated_at();

create trigger flow_definitions_validate
before insert on public.flow_definitions
for each row execute function public.validate_flow_definition_insert();
create trigger flow_definitions_immutable
before update or delete on public.flow_definitions
for each row execute function public.reject_flow_store_mutation();
create trigger flow_definitions_no_truncate
before truncate on public.flow_definitions
for each statement execute function public.reject_flow_store_mutation();

create trigger flow_change_proposals_validate
before update on public.flow_change_proposals
for each row execute function public.validate_flow_change_proposal_change();
create trigger flow_change_proposals_permanent
before delete on public.flow_change_proposals
for each row execute function public.reject_flow_store_mutation();
create trigger flow_change_proposals_no_truncate
before truncate on public.flow_change_proposals
for each statement execute function public.reject_flow_store_mutation();
create trigger flow_change_proposals_set_updated_at
before update on public.flow_change_proposals
for each row execute function public.set_updated_at();

create trigger flow_change_events_append_only
before update or delete on public.flow_change_events
for each row execute function public.reject_flow_store_mutation();
create trigger flow_change_events_no_truncate
before truncate on public.flow_change_events
for each statement execute function public.reject_flow_store_mutation();

-- ---------------------------------------------------------------------------
-- 5. Los helpers internos. Corren como quien los llama: hoy, las siete del
--    operador; despues, los envoltorios del Automation Expert y del canal de
--    aprobacion, con su propia clase de actor.
-- ---------------------------------------------------------------------------

-- La instancia es el par (tenant_ref, ally_ref). Acepta la marca de la 1a
-- entera e ignora las demas claves: el almacen no usa funnel_ref, porque una
-- instancia puede tener varios productos.
create function public._flow_store_instance(p_instance jsonb, out o_tenant_ref text, out o_ally_ref text)
language plpgsql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if jsonb_typeof(p_instance) is distinct from 'object' then
        raise exception using errcode = '22023', message = 'flow_store_instance_invalid';
    end if;
    if not coalesce(jsonb_typeof(p_instance -> 'tenant_ref') = 'string'
        and jsonb_typeof(p_instance -> 'ally_ref') = 'string'
        and p_instance ->> 'tenant_ref' ~ '^[a-z0-9][a-z0-9-]{0,127}$'
        and p_instance ->> 'ally_ref' ~ '^[a-z0-9][a-z0-9-]{0,127}$', false) then
        raise exception using errcode = '22023', message = 'flow_store_instance_invalid';
    end if;
    o_tenant_ref := p_instance ->> 'tenant_ref';
    o_ally_ref := p_instance ->> 'ally_ref';
    if not exists (
        select 1 from public.commercial_ally_runtime_bindings b
         where b.tenant_ref = o_tenant_ref and b.ally_ref = o_ally_ref and b.status = 'active'
    ) then
        raise exception using errcode = '22023', message = 'flow_store_instance_not_bound';
    end if;
end;
$function$;

-- Un producto es de la instancia si tiene un binding activo con ese tenant,
-- ese funnel_ref y el ally_ref de la instancia.
create function public._flow_store_product_is_bound(p_tenant_ref text, p_ally_ref text, p_product_ref text)
returns boolean
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select exists (
        select 1 from public.commercial_ally_runtime_bindings b
         where b.tenant_ref = p_tenant_ref and b.funnel_ref = p_product_ref
           and b.ally_ref = p_ally_ref and b.status = 'active')
$function$;

-- La base: la version activa de TODOS los flujos de la instancia, de todos sus
-- productos, y la generacion solo de los flujos de la propuesta (el CAS sobre
-- el control que se va a mover). Se compara con igualdad de jsonb.
create function public._flow_store_base(p_tenant_ref text, p_ally_ref text, p_item_flow_ids uuid[])
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select coalesce(jsonb_object_agg(c.flow_id::text,
        case when c.flow_id = any (p_item_flow_ids)
             then jsonb_build_object('active_version', c.active_version, 'generation', c.generation)
             else jsonb_build_object('active_version', c.active_version)
        end), '{}'::jsonb)
      from public.flow_controls c
     where c.tenant_ref = p_tenant_ref and c.ally_ref = p_ally_ref
$function$;

create function public._flow_change_item_flow_ids(p_items jsonb)
returns uuid[]
language sql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select coalesce(array_agg(distinct (item ->> 'flow_id')::uuid), '{}'::uuid[])
      from jsonb_array_elements(p_items) item
$function$;

-- La forma minima de los items. La completa la mira el validador.
create function public._flow_change_items_are_valid(p_items jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_item jsonb;
    v_keys text[];
    v_definition jsonb;
    v_client_ok jsonb;
    v_seen text[] := '{}';
    v_class text;
begin
    if jsonb_typeof(p_items) is distinct from 'array' then
        return false;
    end if;
    if jsonb_array_length(p_items) not between 1 and 10 then
        return false;
    end if;
    for v_item in select value from jsonb_array_elements(p_items) loop
        if jsonb_typeof(v_item) is distinct from 'object' then
            return false;
        end if;
        select array_agg(k order by k) into v_keys from jsonb_object_keys(v_item) k;
        if not coalesce(
            jsonb_typeof(v_item -> 'flow_id') = 'string'
            and jsonb_typeof(v_item -> 'producto') = 'string'
            and jsonb_typeof(v_item -> 'flujo') = 'string'
            and jsonb_typeof(v_item -> 'tipo') = 'string'
            and v_item ->> 'flow_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            and v_item ->> 'producto' ~ '^[a-z0-9][a-z0-9-]{0,127}$'
            and v_item ->> 'flujo' ~ '^[a-z][a-z0-9_]{1,39}$', false) then
            return false;
        end if;
        if v_item ->> 'tipo' = 'version' then
            if not coalesce(
                v_keys <@ array['definicion', 'en_curso', 'flow_id', 'flujo', 'ok_del_cliente', 'producto', 'tipo']
                and v_keys @> array['definicion', 'en_curso', 'flow_id', 'flujo', 'producto', 'tipo'], false) then
                return false;
            end if;
            v_definition := v_item -> 'definicion';
            if jsonb_typeof(v_definition) is distinct from 'object' then
                return false;
            end if;
            -- en_curso vale solo seguir hasta que el motor pueda aplicar cancelar.
            if not coalesce(
                v_definition -> 'esquema' = '"setter-flujo/v1"'::jsonb
                and v_definition -> 'estado' = '"borrador"'::jsonb
                and not (v_definition ? 'version')
                and v_definition -> 'flujo' = v_item -> 'flujo'
                and v_definition -> 'producto' = v_item -> 'producto'
                and octet_length(v_definition::text) <= 32768
                and v_item -> 'en_curso' = '"seguir"'::jsonb, false) then
                return false;
            end if;
            -- El OK del cliente para permiso: compra.
            if v_item ? 'ok_del_cliente' then
                v_client_ok := v_item -> 'ok_del_cliente';
                if jsonb_typeof(v_client_ok) is distinct from 'object' then
                    return false;
                end if;
                if (select array_agg(k order by k) from jsonb_object_keys(v_client_ok) k)
                   is distinct from array['fecha', 'medio', 'quien'] then
                    return false;
                end if;
                if not coalesce(
                    jsonb_typeof(v_client_ok -> 'quien') = 'string'
                    and jsonb_typeof(v_client_ok -> 'medio') = 'string'
                    and jsonb_typeof(v_client_ok -> 'fecha') = 'string'
                    and length(v_client_ok ->> 'quien') <= 120
                    and length(v_client_ok ->> 'medio') <= 120
                    and public._flow_store_free_text_is_valid(v_client_ok ->> 'quien')
                    and public._flow_store_free_text_is_valid(v_client_ok ->> 'medio')
                    and v_client_ok ->> 'fecha' ~ '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$', false) then
                    return false;
                end if;
            end if;
            v_class := 'contenido';
        elsif v_item ->> 'tipo' = 'volver' then
            if v_keys is distinct from array['en_curso', 'flow_id', 'flujo', 'producto', 'tipo', 'version'] then
                return false;
            end if;
            if not coalesce(
                jsonb_typeof(v_item -> 'version') = 'number'
                and v_item ->> 'version' ~ '^[1-9][0-9]{0,8}$'
                and v_item -> 'en_curso' = '"seguir"'::jsonb, false) then
                return false;
            end if;
            v_class := 'contenido';
        elsif v_item ->> 'tipo' in ('prender', 'apagar') then
            if v_keys is distinct from array['flow_id', 'flujo', 'producto', 'tipo'] then
                return false;
            end if;
            v_class := 'estado';
        else
            return false;
        end if;
        -- Por flujo, a lo sumo un item de contenido y uno de estado.
        if ((v_item ->> 'flow_id') || ':' || v_class) = any (v_seen) then
            return false;
        end if;
        v_seen := v_seen || ((v_item ->> 'flow_id') || ':' || v_class);
    end loop;
    return true;
end;
$function$;

-- Una lista de hallazgos del validador.
create function public._flow_change_findings_are_valid(p_findings jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_finding jsonb;
    v_keys text[];
    v_text_key text;
begin
    if jsonb_typeof(p_findings) is distinct from 'array' then
        return false;
    end if;
    for v_finding in select value from jsonb_array_elements(p_findings) loop
        if jsonb_typeof(v_finding) is distinct from 'object' then
            return false;
        end if;
        select array_agg(k order by k) into v_keys from jsonb_object_keys(v_finding) k;
        if not coalesce(
            v_keys <@ array['alternative', 'code', 'kind', 'limit', 'path', 'reason', 'rule']
            and v_keys @> array['code', 'kind', 'path', 'rule'], false) then
            return false;
        end if;
        -- rule nula o de 1 a 17; code con la forma de los motivos; path vacio
        -- o un JSON Pointer (RFC 6901) de hasta 512.
        if not coalesce(
            (v_finding -> 'rule' = 'null'::jsonb
             or (jsonb_typeof(v_finding -> 'rule') = 'number'
                 and v_finding ->> 'rule' ~ '^([1-9]|1[0-7])$'))
            and jsonb_typeof(v_finding -> 'code') = 'string'
            and v_finding ->> 'code' ~ '^[a-z][a-z0-9_:]{0,79}$'
            and jsonb_typeof(v_finding -> 'kind') = 'string'
            and v_finding ->> 'kind' in ('forma', 'proteccion', 'todavia_no', 'ciclo', 'advertencia')
            and jsonb_typeof(v_finding -> 'path') = 'string'
            and length(v_finding ->> 'path') <= 512
            and v_finding ->> 'path' ~ '^(/([^/~]|~[01])*)*$', false) then
            return false;
        end if;
        foreach v_text_key in array array['limit', 'reason', 'alternative'] loop
            if v_finding ? v_text_key and not coalesce(
                jsonb_typeof(v_finding -> v_text_key) = 'string'
                and length(v_finding ->> v_text_key) <= 300
                and public._flow_store_free_text_is_valid(v_finding ->> v_text_key), false) then
                return false;
            end if;
        end loop;
    end loop;
    return true;
end;
$function$;

-- El resultado de una validacion: claves exactas blocks y warnings, dos listas
-- validas, hasta 200 hallazgos en total.
create function public._flow_change_result_is_valid(p_result jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if jsonb_typeof(p_result) is distinct from 'object' then
        return false;
    end if;
    if (select array_agg(k order by k) from jsonb_object_keys(p_result) k)
       is distinct from array['blocks', 'warnings'] then
        return false;
    end if;
    if not coalesce(public._flow_change_findings_are_valid(p_result -> 'blocks')
                    and public._flow_change_findings_are_valid(p_result -> 'warnings'), false) then
        return false;
    end if;
    return jsonb_array_length(p_result -> 'blocks') + jsonb_array_length(p_result -> 'warnings') <= 200;
end;
$function$;

-- El validador que corrio: claves exactas.
create function public._flow_change_validator_is_valid(p_validator jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if jsonb_typeof(p_validator) is distinct from 'object' then
        return false;
    end if;
    if (select array_agg(k order by k) from jsonb_object_keys(p_validator) k)
       is distinct from array['contract', 'git_sha', 'product_version', 'rules_version'] then
        return false;
    end if;
    return coalesce(
        p_validator -> 'contract' = '"setter-flujo/v1"'::jsonb
        and jsonb_typeof(p_validator -> 'rules_version') = 'number'
        and p_validator ->> 'rules_version' ~ '^[1-9][0-9]{0,3}$'
        and jsonb_typeof(p_validator -> 'product_version') = 'string'
        and p_validator ->> 'product_version' ~ '^v?[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'
        and jsonb_typeof(p_validator -> 'git_sha') = 'string'
        and p_validator ->> 'git_sha' ~ '^[0-9a-f]{7,40}$', false);
end;
$function$;

-- La huella la calcula solo la base, sobre el texto del jsonb, al congelar.
-- Es stable y no immutable: convert_to es STABLE en el catalogo.
create function public._flow_change_fingerprint(
    p_proposal_id uuid, p_revision integer, p_items jsonb, p_draft_base jsonb, p_base jsonb, p_purpose text)
returns text
language sql
stable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
    select encode(sha256(convert_to(jsonb_build_object(
        'contract', 'flow-change-cycle-v1', 'proposal_id', p_proposal_id, 'revision', p_revision,
        'items', p_items, 'draft_base', p_draft_base, 'base', p_base, 'purpose', p_purpose)::text,
        'UTF8')), 'hex')
$function$;

create function public._flow_change_event(
    p_proposal public.flow_change_proposals, p_event_type text, p_actor_kind text, p_actor_ref text,
    p_reason_code text, p_data jsonb)
returns void
language sql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    insert into public.flow_change_events (tenant_ref, ally_ref, proposal_id, event_type,
        actor_kind, actor_ref, reason_code, data)
    values (p_proposal.tenant_ref, p_proposal.ally_ref, p_proposal.id, p_event_type,
        p_actor_kind, p_actor_ref, p_reason_code, coalesce(p_data, '{}'::jsonb))
$function$;

-- Con la propuesta ya bloqueada: si esta validada o aprobada y vencio, la pasa
-- a vencida con sistema/vencimiento y deja su evento. No hay barrido.
create function public._flow_change_expire_if_due(p_proposal public.flow_change_proposals)
returns boolean
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_proposal.status not in ('validada', 'aprobada')
       or not coalesce(clock_timestamp() >= p_proposal.expires_at, false) then
        return false;
    end if;
    update public.flow_change_proposals
       set status = 'vencida', closed_at = clock_timestamp(),
           closed_by_kind = 'sistema', closed_by_ref = 'vencimiento'
     where id = p_proposal.id;
    perform public._flow_change_event(p_proposal, 'flow_change_expired', 'sistema', 'vencimiento',
        'flow_change_proposal_expired', jsonb_build_object('expires_at', p_proposal.expires_at,
        'from_status', p_proposal.status));
    return true;
end;
$function$;

-- Crear o rehacer un borrador.
create function public._flow_change_save_draft(
    p_tenant_ref text, p_ally_ref text, p_proposal_id uuid, p_expected_revision integer,
    p_items jsonb, p_base jsonb, p_purpose text, p_actor_kind text, p_actor_ref text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_proposal public.flow_change_proposals%rowtype;
    v_item jsonb;
    v_control public.flow_controls%rowtype;
    v_flow_ids uuid[];
    v_current jsonb;
begin
    if not coalesce(p_actor_kind in ('automation_expert', 'operador')
                    and public._flow_store_actor_is_valid(p_actor_kind, p_actor_ref), false) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_purpose) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    if not coalesce(public._flow_change_items_are_valid(p_items), false) then
        raise exception using errcode = '22023', message = 'flow_change_items_invalid';
    end if;
    if (p_proposal_id is null) is distinct from (p_expected_revision is null) then
        raise exception using errcode = '22023', message = 'flow_change_redo_invalid';
    end if;
    if jsonb_typeof(p_base) is distinct from 'object' then
        raise exception using errcode = '22023', message = 'flow_change_base_invalid';
    end if;

    for v_item in select value from jsonb_array_elements(p_items) loop
        select * into v_control from public.flow_controls c
         where c.flow_id = (v_item ->> 'flow_id')::uuid
           and c.tenant_ref = p_tenant_ref and c.ally_ref = p_ally_ref;
        if not found
           or v_control.product_ref is distinct from v_item ->> 'producto'
           or v_control.flow_key is distinct from v_item ->> 'flujo' then
            raise exception using errcode = 'P0002', message = 'flow_not_found';
        end if;
        if not public._flow_store_product_is_bound(p_tenant_ref, p_ally_ref, v_control.product_ref) then
            raise exception using errcode = '55000', message = 'flow_store_product_not_bound';
        end if;
        -- El Automation Expert no crea flujos en la 1b; el operador si escribe
        -- la primera version.
        if p_actor_kind = 'automation_expert' and v_item ->> 'tipo' = 'version'
           and v_control.active_version is null then
            raise exception using errcode = '55000', message = 'flow_creation_not_enabled';
        end if;
    end loop;

    -- La base que leyo quien propone: la version activa de cada flujo de los
    -- items, exacta.
    v_flow_ids := public._flow_change_item_flow_ids(p_items);
    select coalesce(jsonb_object_agg(c.flow_id::text, jsonb_build_object('active_version', c.active_version)), '{}'::jsonb)
      into v_current
      from public.flow_controls c
     where c.flow_id = any (v_flow_ids);
    if p_base is distinct from v_current then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_stale_base', 'base', v_current);
    end if;

    if p_proposal_id is null then
        insert into public.flow_change_proposals (tenant_ref, ally_ref, status, revision, items,
            draft_base, purpose, requested_by_kind, requested_by_ref)
        values (p_tenant_ref, p_ally_ref, 'borrador', 1, p_items, p_base, p_purpose,
            p_actor_kind, p_actor_ref)
        returning * into v_proposal;
    else
        select * into v_proposal from public.flow_change_proposals p
         where p.id = p_proposal_id and p.tenant_ref = p_tenant_ref and p.ally_ref = p_ally_ref
         for update;
        if not found or v_proposal.requested_by_kind is distinct from p_actor_kind then
            raise exception using errcode = 'P0002', message = 'flow_change_not_found';
        end if;
        if v_proposal.status is distinct from 'borrador' then
            return jsonb_build_object('ok', false, 'reason_code', 'flow_change_not_draft',
                'status', v_proposal.status);
        end if;
        if v_proposal.revision is distinct from p_expected_revision then
            return jsonb_build_object('ok', false, 'reason_code', 'flow_change_revision_mismatch',
                'revision', v_proposal.revision);
        end if;
        update public.flow_change_proposals
           set items = p_items, draft_base = p_base, purpose = p_purpose,
               revision = revision + 1, validation = null
         where id = v_proposal.id
        returning * into v_proposal;
    end if;

    perform public._flow_change_event(v_proposal, 'flow_change_draft_saved', p_actor_kind, p_actor_ref,
        'flow_change_draft_saved', jsonb_build_object(
            'revision', v_proposal.revision,
            'items', (select jsonb_agg(jsonb_build_object(
                        'flow_id', item ->> 'flow_id',
                        'tipo', item ->> 'tipo',
                        'definition_sha256', case when item ? 'definicion'
                            then encode(sha256(convert_to((item -> 'definicion')::text, 'UTF8')), 'hex') end)
                        order by ordinality)
                      from jsonb_array_elements(p_items) with ordinality as e(item, ordinality)),
            'purpose_sha256', encode(sha256(convert_to(p_purpose, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'proposal_id', v_proposal.id, 'revision', v_proposal.revision,
        'status', v_proposal.status);
end;
$function$;

-- Registrar una validacion. La base no corre el validador: le cree a quien
-- registra el resultado, pero arma ella la base y la huella.
create function public._flow_change_record_validation(
    p_tenant_ref text, p_ally_ref text, p_proposal_id uuid, p_revision integer,
    p_validated_base jsonb, p_validator jsonb, p_result jsonb, p_preview jsonb,
    p_actor_kind text, p_actor_ref text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_proposal public.flow_change_proposals%rowtype;
    v_flow_ids uuid[];
    v_base jsonb;
    v_draft_now jsonb;
    v_now timestamptz;
    v_fingerprint text;
begin
    if not coalesce(p_actor_kind in ('automation_expert', 'operador')
                    and public._flow_store_actor_is_valid(p_actor_kind, p_actor_ref), false) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not coalesce(public._flow_change_validator_is_valid(p_validator), false) then
        raise exception using errcode = '22023', message = 'flow_change_validator_invalid';
    end if;
    if not coalesce(public._flow_change_result_is_valid(p_result), false) then
        raise exception using errcode = '22023', message = 'flow_change_result_invalid';
    end if;
    if not coalesce(jsonb_typeof(p_preview) = 'object' and octet_length(p_preview::text) <= 131072, false) then
        raise exception using errcode = '22023', message = 'flow_change_preview_invalid';
    end if;
    if p_revision is null then
        raise exception using errcode = '22023', message = 'flow_change_revision_invalid';
    end if;

    select * into v_proposal from public.flow_change_proposals p
     where p.id = p_proposal_id and p.tenant_ref = p_tenant_ref and p.ally_ref = p_ally_ref
     for update;
    if not found or v_proposal.requested_by_kind is distinct from p_actor_kind then
        raise exception using errcode = 'P0002', message = 'flow_change_not_found';
    end if;
    if v_proposal.status is distinct from 'borrador' then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_not_draft', 'status', v_proposal.status);
    end if;
    if v_proposal.revision is distinct from p_revision then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_revision_mismatch',
            'revision', v_proposal.revision);
    end if;

    -- Los controles de toda la instancia, en una sola sentencia ordenada.
    perform 1 from public.flow_controls c
     where c.tenant_ref = p_tenant_ref and c.ally_ref = p_ally_ref
     order by c.flow_id
     for share;
    v_flow_ids := public._flow_change_item_flow_ids(v_proposal.items);
    v_base := public._flow_store_base(p_tenant_ref, p_ally_ref, v_flow_ids);
    select coalesce(jsonb_object_agg(key, jsonb_build_object('active_version', value -> 'active_version')), '{}'::jsonb)
      into v_draft_now
      from jsonb_each(v_base)
     where key::uuid = any (v_flow_ids);

    -- El borrador escrito sobre una version no se valida encima de otra que
    -- llego en el medio, y la foto contra la que corrio el validador tiene que
    -- ser exactamente la de hoy.
    if v_draft_now is distinct from v_proposal.draft_base
       or p_validated_base is distinct from v_base then
        perform public._flow_change_event(v_proposal, 'flow_change_validation_rejected', p_actor_kind, p_actor_ref,
            'flow_change_stale_base', jsonb_build_object('revision', v_proposal.revision));
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_stale_base', 'base', v_base);
    end if;

    if jsonb_array_length(p_result -> 'blocks') > 0 then
        update public.flow_change_proposals
           set validation = jsonb_build_object('validator', p_validator, 'result', p_result,
                   'preview', p_preview, 'base', v_base)
         where id = v_proposal.id;
        perform public._flow_change_event(v_proposal, 'flow_change_validation_blocked', p_actor_kind, p_actor_ref,
            'flow_change_validation_blocked', jsonb_build_object('revision', v_proposal.revision,
                'codes', (select coalesce(jsonb_agg(f ->> 'code'), '[]'::jsonb)
                            from jsonb_array_elements(p_result -> 'blocks') f),
                'validator', p_validator));
        return jsonb_build_object('ok', true, 'status', 'borrador',
            'blocks', jsonb_array_length(p_result -> 'blocks'));
    end if;

    -- Sin bloqueos, congela: la base guardada es la de la base, no la que llego.
    v_now := clock_timestamp();
    v_fingerprint := public._flow_change_fingerprint(v_proposal.id, v_proposal.revision, v_proposal.items,
        v_proposal.draft_base, v_base, v_proposal.purpose);
    update public.flow_change_proposals
       set status = 'validada', base = v_base, fingerprint = v_fingerprint,
           validated_at = v_now, expires_at = v_now + interval '24 hours',
           validation = jsonb_build_object('validator', p_validator, 'result', p_result, 'preview', p_preview)
     where id = v_proposal.id
    returning * into v_proposal;
    perform public._flow_change_event(v_proposal, 'flow_change_validated', p_actor_kind, p_actor_ref,
        'flow_change_validated', jsonb_build_object('revision', v_proposal.revision,
            'fingerprint', v_fingerprint, 'validator', p_validator,
            'warnings', (select coalesce(jsonb_agg(f ->> 'code'), '[]'::jsonb)
                           from jsonb_array_elements(p_result -> 'warnings') f),
            'expires_at', v_proposal.expires_at));
    return jsonb_build_object('ok', true, 'status', 'validada', 'fingerprint', v_fingerprint,
        'expires_at', v_proposal.expires_at);
end;
$function$;

-- Aprobar exactamente la propuesta validada: la huella viaja con el si.
create function public._flow_change_approve(
    p_tenant_ref text, p_ally_ref text, p_proposal_id uuid, p_fingerprint text,
    p_approver_kind text, p_approver_ref text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_proposal public.flow_change_proposals%rowtype;
    v_base jsonb;
begin
    if not coalesce(p_approver_kind in ('operador', 'slack')
                    and public._flow_store_actor_is_valid(p_approver_kind, p_approver_ref), false) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not coalesce(p_fingerprint ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'flow_change_fingerprint_invalid';
    end if;
    select * into v_proposal from public.flow_change_proposals p
     where p.id = p_proposal_id and p.tenant_ref = p_tenant_ref and p.ally_ref = p_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_change_not_found';
    end if;
    perform 1 from public.flow_controls c
     where c.tenant_ref = p_tenant_ref and c.ally_ref = p_ally_ref
     order by c.flow_id
     for share;
    if public._flow_change_expire_if_due(v_proposal) then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_proposal_expired');
    end if;
    if v_proposal.status = 'aprobada' then
        if p_fingerprint is distinct from v_proposal.fingerprint then
            raise exception using errcode = '22023', message = 'flow_change_fingerprint_mismatch';
        end if;
        return jsonb_build_object('ok', true, 'replayed', true,
            'approved_by_kind', v_proposal.approved_by_kind, 'approved_by_ref', v_proposal.approved_by_ref);
    end if;
    if v_proposal.status is distinct from 'validada' then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_not_validated',
            'status', v_proposal.status);
    end if;
    if p_fingerprint is distinct from v_proposal.fingerprint then
        raise exception using errcode = '22023', message = 'flow_change_fingerprint_mismatch';
    end if;
    -- Si la base cambio, no queda una aprobada inservible esperando a vencer.
    v_base := public._flow_store_base(p_tenant_ref, p_ally_ref,
        public._flow_change_item_flow_ids(v_proposal.items));
    if v_base is distinct from v_proposal.base then
        perform public._flow_change_event(v_proposal, 'flow_change_approval_rejected', p_approver_kind,
            p_approver_ref, 'flow_change_stale_base', jsonb_build_object('fingerprint', v_proposal.fingerprint));
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_stale_base');
    end if;
    update public.flow_change_proposals
       set status = 'aprobada', approved_at = clock_timestamp(),
           approved_by_kind = p_approver_kind, approved_by_ref = p_approver_ref
     where id = v_proposal.id;
    -- La simulacion es del motor: hoy no existe, y queda registrado.
    perform public._flow_change_event(v_proposal, 'flow_change_approved', p_approver_kind, p_approver_ref,
        'flow_change_approved', jsonb_build_object('fingerprint', v_proposal.fingerprint,
            'simulation', 'not_built'));
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'aprobada',
        'approved_by_kind', p_approver_kind, 'approved_by_ref', p_approver_ref);
end;
$function$;

-- Publicar: el paso atomico. Todo lo que rechaza con un resultado va antes de
-- la primera escritura; lo que ya miro la validacion y fallaria por otro
-- camino aborta la transaccion entera.
create function public._flow_change_publish(
    p_tenant_ref text, p_ally_ref text, p_proposal_id uuid, p_fingerprint text, p_revalidation jsonb)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    -- Una migracion sube la minima cuando las reglas cambian de forma
    -- incompatible: una imagen vieja ya no puede publicar.
    c_min_rules_version constant integer := 1;
    c_revalidation_window constant interval := interval '10 minutes';
    v_proposal public.flow_change_proposals%rowtype;
    v_flow_ids uuid[];
    v_base jsonb;
    v_validated_at_text text;
    v_revalidated_at timestamptz;
    v_reval_ok boolean := false;
    v_flow_id uuid;
    v_control public.flow_controls%rowtype;
    v_content jsonb;
    v_state_item jsonb;
    v_new_version integer;
    v_new_state text;
    v_summary jsonb := '[]'::jsonb;
    v_published jsonb := '[]'::jsonb;
begin
    if not coalesce(p_fingerprint ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '22023', message = 'flow_change_fingerprint_invalid';
    end if;
    select * into v_proposal from public.flow_change_proposals p
     where p.id = p_proposal_id and p.tenant_ref = p_tenant_ref and p.ally_ref = p_ally_ref
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_change_not_found';
    end if;
    if v_proposal.status = 'publicada' then
        if p_fingerprint is distinct from v_proposal.fingerprint then
            raise exception using errcode = '22023', message = 'flow_change_fingerprint_mismatch';
        end if;
        return jsonb_build_object('ok', true, 'replayed', true);
    end if;
    if v_proposal.status not in ('validada', 'aprobada') then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_not_approved',
            'status', v_proposal.status);
    end if;
    if p_fingerprint is distinct from v_proposal.fingerprint then
        raise exception using errcode = '22023', message = 'flow_change_fingerprint_mismatch';
    end if;

    -- Todos los controles de la instancia, en orden. Recien despues, el plazo:
    -- una publicacion que espero un lock no publica pasado expires_at.
    perform 1 from public.flow_controls c
     where c.tenant_ref = p_tenant_ref and c.ally_ref = p_ally_ref
     order by c.flow_id
     for update;
    if public._flow_change_expire_if_due(v_proposal) then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_proposal_expired');
    end if;
    if v_proposal.status is distinct from 'aprobada' then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_not_approved',
            'status', v_proposal.status);
    end if;

    -- La revalidacion de esta misma huella, con claves y tipos exactos y en
    -- forma positiva: cero bloqueos, el contrato, la minima de reglas y hecha
    -- hace menos de diez minutos. validated_at es una fecha ISO 8601 con zona:
    -- una palabra que Postgres lee como fecha (now, today) no cuenta.
    if jsonb_typeof(p_revalidation) = 'object' then
        if (select array_agg(k order by k) from jsonb_object_keys(p_revalidation) k)
           is not distinct from array['catalog_sha256', 'fingerprint', 'result', 'validated_at', 'validator'] then
            if coalesce(public._flow_change_validator_is_valid(p_revalidation -> 'validator')
                        and public._flow_change_result_is_valid(p_revalidation -> 'result'), false) then
                if jsonb_typeof(p_revalidation -> 'validated_at') = 'string' then
                    v_validated_at_text := p_revalidation ->> 'validated_at';
                    if v_validated_at_text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$' then
                        begin
                            v_revalidated_at := v_validated_at_text::timestamptz;
                        exception when others then
                            v_revalidated_at := null;
                        end;
                    end if;
                end if;
                v_reval_ok := coalesce(
                    p_revalidation -> 'fingerprint' = to_jsonb(v_proposal.fingerprint)
                    and (p_revalidation #>> '{validator,rules_version}')::integer >= c_min_rules_version
                    and p_revalidation #> '{result,blocks}' = '[]'::jsonb
                    and jsonb_typeof(p_revalidation -> 'catalog_sha256') = 'string'
                    and p_revalidation ->> 'catalog_sha256' ~ '^[0-9a-f]{64}$'
                    and v_revalidated_at > clock_timestamp() - c_revalidation_window
                    and v_revalidated_at <= clock_timestamp() + interval '1 minute', false);
            end if;
        end if;
    end if;
    if not v_reval_ok then
        perform public._flow_change_event(v_proposal, 'flow_change_publish_rejected',
            v_proposal.approved_by_kind, v_proposal.approved_by_ref,
            'flow_change_revalidation_blocked', '{}'::jsonb);
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_revalidation_blocked');
    end if;

    -- La base: si la de hoy no es la congelada, la propuesta queda aprobada
    -- hasta que vence y el rechazo queda registrado con los flujos que cambiaron.
    v_flow_ids := public._flow_change_item_flow_ids(v_proposal.items);
    v_base := public._flow_store_base(p_tenant_ref, p_ally_ref, v_flow_ids);
    if v_base is distinct from v_proposal.base then
        perform public._flow_change_event(v_proposal, 'flow_change_publish_rejected',
            v_proposal.approved_by_kind, v_proposal.approved_by_ref, 'flow_change_stale_base',
            jsonb_build_object('flows', (
                select coalesce(jsonb_agg(k order by k), '[]'::jsonb)
                  from (select key as k from jsonb_each(v_base)
                        union
                        select key from jsonb_each(v_proposal.base)) keys
                 where v_base -> k is distinct from v_proposal.base -> k)));
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_stale_base');
    end if;

    -- Lo que la validacion ya miro, por si la base cambio por otro camino. Un
    -- error aca es de programa: aborta la transaccion entera, antes de escribir.
    for v_flow_id in select f from unnest(v_flow_ids) f order by f loop
        select * into v_control from public.flow_controls c where c.flow_id = v_flow_id;
        if not found or not public._flow_store_product_is_bound(p_tenant_ref, p_ally_ref, v_control.product_ref) then
            raise exception using errcode = '55000', message = 'flow_store_product_not_bound';
        end if;
        v_content := null;
        v_state_item := null;
        select item into v_content from jsonb_array_elements(v_proposal.items) item
         where (item ->> 'flow_id')::uuid = v_flow_id and item ->> 'tipo' in ('version', 'volver');
        select item into v_state_item from jsonb_array_elements(v_proposal.items) item
         where (item ->> 'flow_id')::uuid = v_flow_id and item ->> 'tipo' in ('prender', 'apagar');
        v_new_version := v_control.active_version;
        if v_content ->> 'tipo' = 'version' then
            v_new_version := (select coalesce(max(d.version), 0) + 1
                                from public.flow_definitions d where d.flow_id = v_flow_id);
        elsif v_content ->> 'tipo' = 'volver' then
            v_new_version := (v_content ->> 'version')::integer;
            if not exists (select 1 from public.flow_definitions d
                            where d.flow_id = v_flow_id and d.version = v_new_version)
               or v_new_version is not distinct from v_control.active_version then
                raise exception using errcode = '55000', message = 'flow_change_rollback_target_invalid';
            end if;
        end if;
        v_new_state := case v_state_item ->> 'tipo'
            when 'prender' then 'prendido'
            when 'apagar' then 'apagado'
            else v_control.state end;
        if v_state_item is not null and v_new_state = v_control.state then
            raise exception using errcode = '55000', message = 'flow_change_state_transition_invalid';
        end if;
        if v_new_state <> 'apagado' and v_new_version is null then
            raise exception using errcode = '55000', message = 'flow_change_flow_has_no_version';
        end if;
    end loop;

    -- Las escrituras, por flujo y en orden de flow_id: primero el contenido y
    -- despues el estado, en un solo UPDATE del control. El disparador del
    -- control deja un flow_control_changed por flujo.
    for v_flow_id in select f from unnest(v_flow_ids) f order by f loop
        select * into v_control from public.flow_controls c where c.flow_id = v_flow_id;
        v_content := null;
        v_state_item := null;
        select item into v_content from jsonb_array_elements(v_proposal.items) item
         where (item ->> 'flow_id')::uuid = v_flow_id and item ->> 'tipo' in ('version', 'volver');
        select item into v_state_item from jsonb_array_elements(v_proposal.items) item
         where (item ->> 'flow_id')::uuid = v_flow_id and item ->> 'tipo' in ('prender', 'apagar');
        v_new_version := v_control.active_version;
        if v_content ->> 'tipo' = 'version' then
            v_new_version := (select coalesce(max(d.version), 0) + 1
                                from public.flow_definitions d where d.flow_id = v_flow_id);
            -- definition_sha256 lo calcula el disparador.
            insert into public.flow_definitions (flow_id, version, schema_ref, definition,
                definition_sha256, proposal_id, approved_by_kind, approved_by_ref)
            values (v_flow_id, v_new_version, 'setter-flujo/v1',
                (v_content -> 'definicion')
                    || jsonb_build_object('estado', 'publicada', 'version', v_new_version),
                repeat('0', 64), v_proposal.id, v_proposal.approved_by_kind, v_proposal.approved_by_ref);
        elsif v_content ->> 'tipo' = 'volver' then
            v_new_version := (v_content ->> 'version')::integer;
        end if;
        v_new_state := case v_state_item ->> 'tipo'
            when 'prender' then 'prendido'
            when 'apagar' then 'apagado'
            else v_control.state end;
        update public.flow_controls
           set active_version = v_new_version, state = v_new_state, generation = generation + 1,
               change_kind = 'publicado', changed_by_kind = v_proposal.approved_by_kind,
               changed_by_ref = v_proposal.approved_by_ref, changed_by_proposal_id = v_proposal.id,
               change_reason = v_proposal.purpose, changed_at = clock_timestamp()
         where flow_id = v_flow_id;
        v_summary := v_summary || jsonb_build_array(jsonb_build_object('flow_id', v_flow_id,
            'from_version', v_control.active_version, 'to_version', v_new_version,
            'from_state', v_control.state, 'to_state', v_new_state,
            'en_curso', v_content ->> 'en_curso'));
        v_published := v_published || jsonb_build_array(jsonb_build_object(
            'producto', v_control.product_ref, 'flujo', v_control.flow_key,
            'version', v_new_version, 'state', v_new_state,
            'generation', v_control.generation + 1));
    end loop;

    update public.flow_change_proposals
       set status = 'publicada', published_at = clock_timestamp()
     where id = v_proposal.id;
    perform public._flow_change_event(v_proposal, 'flow_change_published',
        v_proposal.approved_by_kind, v_proposal.approved_by_ref, 'flow_change_published',
        jsonb_build_object('flows', v_summary,
            'revalidation', jsonb_build_object('validator', p_revalidation -> 'validator',
                'catalog_sha256', p_revalidation -> 'catalog_sha256',
                'validated_at', p_revalidation -> 'validated_at')));
    return jsonb_build_object('ok', true, 'replayed', false, 'published', v_published);
end;
$function$;

-- Pausar no pasa por una propuesta. Con la generacion esperada es el CAS.
create function public._flow_change_pause(
    p_tenant_ref text, p_ally_ref text, p_product_ref text, p_flow_key text,
    p_expected_generation bigint, p_actor_kind text, p_actor_ref text, p_reason text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_control public.flow_controls%rowtype;
begin
    if not coalesce(p_actor_kind in ('automation_expert', 'operador', 'slack')
                    and public._flow_store_actor_is_valid(p_actor_kind, p_actor_ref), false) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    if p_expected_generation is null then
        raise exception using errcode = '22023', message = 'flow_control_generation_invalid';
    end if;
    select * into v_control from public.flow_controls c
     where c.tenant_ref = p_tenant_ref and c.ally_ref = p_ally_ref
       and c.product_ref = p_product_ref and c.flow_key = p_flow_key
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_not_found';
    end if;
    if v_control.generation is distinct from p_expected_generation then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_control_generation_mismatch',
            'generation', v_control.generation);
    end if;
    if v_control.state = 'pausado' then
        return jsonb_build_object('ok', true, 'changed', false, 'generation', v_control.generation);
    end if;
    -- De apagado solo se va a prendido.
    if v_control.state = 'apagado' then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_pause_requires_on');
    end if;
    update public.flow_controls
       set state = 'pausado', generation = generation + 1, change_kind = 'pausado',
           changed_by_kind = p_actor_kind, changed_by_ref = p_actor_ref,
           changed_by_proposal_id = null, change_reason = p_reason, changed_at = clock_timestamp()
     where flow_id = v_control.flow_id;
    return jsonb_build_object('ok', true, 'changed', true, 'generation', v_control.generation + 1);
end;
$function$;

-- Descartar lo puede quien la pidio (la misma clase) o un aprobador.
create function public._flow_change_discard(
    p_tenant_ref text, p_ally_ref text, p_proposal_id uuid, p_actor_kind text, p_actor_ref text, p_reason text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_proposal public.flow_change_proposals%rowtype;
begin
    if not coalesce(p_actor_kind in ('automation_expert', 'operador', 'slack')
                    and public._flow_store_actor_is_valid(p_actor_kind, p_actor_ref), false) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    select * into v_proposal from public.flow_change_proposals p
     where p.id = p_proposal_id and p.tenant_ref = p_tenant_ref and p.ally_ref = p_ally_ref
     for update;
    if not found or not coalesce(p_actor_kind = v_proposal.requested_by_kind
                                 or p_actor_kind in ('operador', 'slack'), false) then
        raise exception using errcode = 'P0002', message = 'flow_change_not_found';
    end if;
    if public._flow_change_expire_if_due(v_proposal) then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_final', 'status', 'vencida');
    end if;
    if v_proposal.status = 'descartada' then
        return jsonb_build_object('ok', true, 'replayed', true);
    end if;
    if v_proposal.status in ('publicada', 'vencida') then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_final', 'status', v_proposal.status);
    end if;
    update public.flow_change_proposals
       set status = 'descartada', closed_at = clock_timestamp(),
           closed_by_kind = p_actor_kind, closed_by_ref = p_actor_ref, close_reason = p_reason
     where id = v_proposal.id;
    perform public._flow_change_event(v_proposal, 'flow_change_discarded', p_actor_kind, p_actor_ref,
        'flow_change_discarded', jsonb_build_object('from_status', v_proposal.status,
            'close_reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    return jsonb_build_object('ok', true, 'replayed', false);
end;
$function$;

-- ---------------------------------------------------------------------------
-- 6. Las siete del operador. Solo las ejecuta el duenio, y como corren con los
--    permisos de quien llama, un rol que recibiera EXECUTE por error tampoco
--    podria escribir: no tiene permisos sobre las tablas. La primera linea de
--    cada una resuelve la instancia.
-- ---------------------------------------------------------------------------

create function public.operator_register_flow_v1(
    p_instance jsonb, p_product_ref text, p_flow_key text, p_actor_name text, p_reason text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_flow_id uuid;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not coalesce(p_product_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'
                    and p_flow_key ~ '^[a-z][a-z0-9_]{1,39}$', false) then
        raise exception using errcode = '22023', message = 'flow_store_flow_invalid';
    end if;
    if not public._flow_store_product_is_bound(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_product_ref) then
        raise exception using errcode = '22023', message = 'flow_store_product_not_bound';
    end if;
    if not coalesce(public._flow_store_actor_is_valid('operador', p_actor_name), false) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    insert into public.flow_controls (tenant_ref, ally_ref, product_ref, flow_key, state,
        active_version, generation, change_kind, changed_by_kind, changed_by_ref, change_reason)
    values (v_instance.o_tenant_ref, v_instance.o_ally_ref, p_product_ref, p_flow_key, 'apagado',
        null, 0, 'registrado', 'operador', p_actor_name, p_reason)
    on conflict (tenant_ref, product_ref, flow_key) do nothing
    returning flow_id into v_flow_id;
    if v_flow_id is null then
        -- El flow_id sale solo si el flujo es de esta instancia.
        select c.flow_id into v_flow_id from public.flow_controls c
         where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
           and c.product_ref = p_product_ref and c.flow_key = p_flow_key;
        return jsonb_build_object('ok', false, 'reason_code', 'flow_already_registered', 'flow_id', v_flow_id);
    end if;
    return jsonb_build_object('ok', true, 'flow_id', v_flow_id);
end;
$function$;

create function public.operator_save_flow_change_draft_v1(
    p_instance jsonb, p_proposal_id uuid, p_expected_revision integer, p_items jsonb, p_base jsonb,
    p_purpose text, p_actor_name text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
begin
    v_instance := public._flow_store_instance(p_instance);
    return public._flow_change_save_draft(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_proposal_id,
        p_expected_revision, p_items, p_base, p_purpose, 'operador', p_actor_name);
end;
$function$;

create function public.operator_record_flow_change_validation_v1(
    p_instance jsonb, p_proposal_id uuid, p_revision integer, p_validated_base jsonb,
    p_validator jsonb, p_result jsonb, p_preview jsonb, p_actor_name text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
begin
    v_instance := public._flow_store_instance(p_instance);
    return public._flow_change_record_validation(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_proposal_id,
        p_revision, p_validated_base, p_validator, p_result, p_preview, 'operador', p_actor_name);
end;
$function$;

create function public.operator_approve_flow_change_v1(
    p_instance jsonb, p_proposal_id uuid, p_fingerprint text, p_approver_name text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
begin
    v_instance := public._flow_store_instance(p_instance);
    return public._flow_change_approve(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_proposal_id,
        p_fingerprint, 'operador', p_approver_name);
end;
$function$;

create function public.operator_publish_flow_change_v1(
    p_instance jsonb, p_proposal_id uuid, p_fingerprint text, p_revalidation jsonb)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
begin
    v_instance := public._flow_store_instance(p_instance);
    return public._flow_change_publish(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_proposal_id,
        p_fingerprint, p_revalidation);
end;
$function$;

create function public.operator_pause_flow_v1(
    p_instance jsonb, p_product_ref text, p_flow_key text, p_expected_generation bigint,
    p_actor_name text, p_reason text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
begin
    v_instance := public._flow_store_instance(p_instance);
    return public._flow_change_pause(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_product_ref, p_flow_key,
        p_expected_generation, 'operador', p_actor_name, p_reason);
end;
$function$;

create function public.operator_discard_flow_change_v1(
    p_instance jsonb, p_proposal_id uuid, p_actor_name text, p_reason text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
begin
    v_instance := public._flow_store_instance(p_instance);
    return public._flow_change_discard(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_proposal_id,
        'operador', p_actor_name, p_reason);
end;
$function$;

-- ---------------------------------------------------------------------------
-- 7. Los permisos: ninguno para la API. Las 31 funciones nuevas, por firma
--    exacta (una firma que no exista hace fallar la migracion), y las cuatro
--    tablas. La revocacion explicita a cada rol hace falta porque los
--    privilegios por defecto de Supabase conceden directo a esos roles, y
--    revocarle a public no se los saca.
-- ---------------------------------------------------------------------------

do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public._flow_store_actor_is_valid(text,text)',
        'public._flow_store_free_text_is_valid(text)',
        'public.reject_flow_store_mutation()',
        'public.validate_flow_control_change()',
        'public.record_flow_control_change()',
        'public.validate_flow_definition_insert()',
        'public.validate_flow_change_proposal_change()',
        'public._flow_store_instance(jsonb)',
        'public._flow_store_product_is_bound(text,text,text)',
        'public._flow_store_base(text,text,uuid[])',
        'public._flow_change_item_flow_ids(jsonb)',
        'public._flow_change_items_are_valid(jsonb)',
        'public._flow_change_findings_are_valid(jsonb)',
        'public._flow_change_result_is_valid(jsonb)',
        'public._flow_change_validator_is_valid(jsonb)',
        'public._flow_change_fingerprint(uuid,integer,jsonb,jsonb,jsonb,text)',
        'public._flow_change_event(public.flow_change_proposals,text,text,text,text,jsonb)',
        'public._flow_change_expire_if_due(public.flow_change_proposals)',
        'public._flow_change_save_draft(text,text,uuid,integer,jsonb,jsonb,text,text,text)',
        'public._flow_change_record_validation(text,text,uuid,integer,jsonb,jsonb,jsonb,jsonb,text,text)',
        'public._flow_change_approve(text,text,uuid,text,text,text)',
        'public._flow_change_publish(text,text,uuid,text,jsonb)',
        'public._flow_change_pause(text,text,text,text,bigint,text,text,text)',
        'public._flow_change_discard(text,text,uuid,text,text,text)',
        'public.operator_register_flow_v1(jsonb,text,text,text,text)',
        'public.operator_save_flow_change_draft_v1(jsonb,uuid,integer,jsonb,jsonb,text,text)',
        'public.operator_record_flow_change_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,jsonb,text)',
        'public.operator_approve_flow_change_v1(jsonb,uuid,text,text)',
        'public.operator_publish_flow_change_v1(jsonb,uuid,text,jsonb)',
        'public.operator_pause_flow_v1(jsonb,text,text,bigint,text,text)',
        'public.operator_discard_flow_change_v1(jsonb,uuid,text,text)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role')
             order by r.rolname
        loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
    end loop;

    revoke all on table public.flow_controls, public.flow_definitions,
        public.flow_change_proposals, public.flow_change_events from public;
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on table public.flow_controls, public.flow_definitions, '
            'public.flow_change_proposals, public.flow_change_events from %I', v_role);
    end loop;
end;
$acl$;

commit;
