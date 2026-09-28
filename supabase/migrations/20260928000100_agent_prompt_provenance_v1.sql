-- La procedencia del prompt del agente, por turno (agent-prompt-provenance-v1).
--
-- Decision: docs/decisions/0020-agent-prompt-provenance.md (Dan, 2026-09-28).
-- Contrato: docs/contracts/agent-prompt-provenance-v1.md
--
-- POR QUE EXISTE. Hasta hoy nada guardaba con que prompt contesto el agente.
-- `daily_feedback_items.release_id` existe en el esquema desde 20260910000100 y
-- `daily_feedback_export.py` lo escribia como el literal
-- 'release_lineage_unavailable' con version 0; habia incluso un guard que lo
-- EXIGIA asi. Medido el 2026-09-27: las cuatro decisiones de la revision de ese
-- dia salieron con ese literal, o sea que del feedback se sabia que se dijo y de
-- que dia, pero no sobre que version del agente --- que es justo lo que hace
-- falta para decidir si un cambio mejoro algo.
--
-- DOS TABLAS.
--   1. agent_prompt_releases: una fila por composicion distinta del prompt,
--      observada DONDE VIVE (dentro de infra_hermes) por
--      scripts/register_agent_prompt_release.py. Guarda el sha256 y el mtime de
--      cada artefacto (SOUL.md, config.yaml, .skills_prompt_snapshot.json) y el
--      TEXTO COMPLETO del SOUL. No se deriva de Git a proposito: el SOUL se
--      edita a mano en el VPS --- al 2026-09-27 habia tres copias fechadas de
--      septiembre al lado del archivo vivo --- asi que un hash sacado del repo
--      seria mentira el dia que alguien edite sin commitear.
--   2. agent_turn_provenance: una fila por turno del agente. El modelo pedido y
--      el que contesto (hasta hoy el bridge descartaba el cuerpo entero de la
--      respuesta salvo el texto), el sha del bridge, la version del armador de
--      contexto, el digest del contexto exacto que se mando y las informaciones
--      agregadas (`known_fields`), mas el release vigente en ese momento
--      DENORMALIZADO, para que registrar un release nuevo no reescriba historia.
--
-- LO QUE NO SE AFIRMA. El registrador corre por cron, no por turno, asi que en
-- el momento de escribir el turno no se puede garantizar que el release vigente
-- sea el que el agente leyo. No se afirma: agent_turn_release_confidence_v1 lo
-- calcula despues comparando el mtime de los artefactos del release SIGUIENTE
-- contra el momento del turno. 'verified' cuando el cambio vino despues del
-- turno; 'misattributed' cuando los archivos ya habian cambiado antes, o sea que
-- el turno corrio algo mas nuevo que lo atribuido; 'open' cuando todavia no hay
-- una observacion posterior con la que comparar.

begin;

-- 1. Los releases: la composicion del prompt, tal como esta donde vive ---------

create table public.agent_prompt_releases (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null
        check (tenant_ref ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    scope_ref text not null
        check (scope_ref ~ '^[a-z0-9][a-z0-9_-]{0,127}$'),
    profile_name text not null
        check (profile_name ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    -- sha256 sobre el manifiesto canonico de los artefactos: cambia si cambia
    -- cualquiera de ellos, y es la identidad del release.
    release_digest text not null check (release_digest ~ '^[a-f0-9]{64}$'),
    -- contador por scope: es lo que viaja como `release_version` al paquete de
    -- revision diaria, que lo declara integer.
    release_ordinal integer not null check (release_ordinal >= 1),
    -- {ruta: {sha256, bytes, modified_at}} de cada artefacto observado.
    artifacts jsonb not null,
    -- el mtime mas nuevo de todos los artefactos, desnormalizado para poder
    -- calcular la confianza de la atribucion sin abrir el jsonb.
    artifacts_modified_at timestamptz not null,
    soul_text text not null check (char_length(soul_text) between 1 and 200000),
    -- cuando el registrador lo miro, no cuando se escribio la fila.
    observed_at timestamptz not null,
    registered_by text not null check (char_length(btrim(registered_by)) between 1 and 128),
    created_at timestamptz not null default clock_timestamp(),
    constraint agent_prompt_releases_observed_after_modified
        check (observed_at >= artifacts_modified_at),
    constraint agent_prompt_releases_one_per_digest
        unique (tenant_ref, scope_ref, release_digest),
    constraint agent_prompt_releases_one_per_ordinal
        unique (tenant_ref, scope_ref, release_ordinal)
);

-- La busqueda del release vigente para un turno: el mas nuevo observado antes.
create index agent_prompt_releases_active_idx
    on public.agent_prompt_releases (tenant_ref, scope_ref, observed_at desc);

-- Un release es un hecho observado: no se corrige, se observa de nuevo.
create function public.agent_prompt_releases_immutable_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    raise exception 'agent_prompt_release_is_immutable';
end;
$$;

create trigger agent_prompt_releases_immutable
before update or delete on public.agent_prompt_releases
for each row execute function public.agent_prompt_releases_immutable_guard();

-- 2. Los turnos: con que contesto el agente, cada vez ------------------------

create table public.agent_turn_provenance (
    id uuid primary key default gen_random_uuid(),
    tenant_ref text not null
        check (tenant_ref ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    scope_ref text not null
        check (scope_ref ~ '^[a-z0-9][a-z0-9_-]{0,127}$'),
    -- el display_id de Chatwoot, que es con lo que se junta la revision diaria.
    external_conversation_id bigint
        check (external_conversation_id is null or external_conversation_id > 0),
    -- idempotencia: el bridge reintenta, y el mismo turno no se cuenta dos veces.
    turn_digest text not null unique check (turn_digest ~ '^[a-f0-9]{64}$'),
    occurred_at timestamptz not null,
    outcome text not null check (outcome in ('completed', 'failed')),
    -- el release vigente al momento del turno. La referencia deja rastro; las
    -- dos columnas denormalizadas son las que se leen, para que un release
    -- nuevo no cambie lo que ya se dijo de un turno viejo.
    release_id uuid references public.agent_prompt_releases(id) on delete restrict,
    release_digest text
        check (release_digest is null or release_digest ~ '^[a-f0-9]{64}$'),
    release_ordinal integer
        check (release_ordinal is null or release_ordinal >= 1),
    model_requested text not null
        check (char_length(btrim(model_requested)) between 1 and 128),
    -- lo que Hermes dijo que contesto. Puede faltar: si la peticion fallo, no
    -- hubo cuerpo del cual leerlo.
    model_answered text
        check (model_answered is null or char_length(btrim(model_answered)) between 1 and 128),
    bridge_release text not null
        check (char_length(btrim(bridge_release)) between 1 and 128),
    context_builder_version text not null
        check (context_builder_version ~ '^[a-z][a-z0-9_-]{0,63}$'),
    -- sha256 del JSON exacto que se le mando al agente: permite probar despues
    -- si una reproduccion coincide con lo que realmente vio.
    context_digest text not null check (context_digest ~ '^[a-f0-9]{64}$'),
    -- las informaciones agregadas: known_fields, la bandera de derivacion y la
    -- forma del historial. NO la transcripcion: esa vive en Chatwoot.
    context_added jsonb not null,
    created_at timestamptz not null default clock_timestamp(),
    constraint agent_turn_provenance_release_is_whole
        check (
            (release_id is null and release_digest is null and release_ordinal is null)
            or (release_id is not null and release_digest is not null
                and release_ordinal is not null)
        )
);

-- Lo que pregunta la revision diaria: los turnos de unas conversaciones en una
-- ventana, el mas nuevo primero.
create index agent_turn_provenance_lookup_idx
    on public.agent_turn_provenance
    (tenant_ref, scope_ref, external_conversation_id, occurred_at desc);

-- Los turnos que quedaron sin release: si el registrador nunca corrio, esto los
-- deja contables en vez de invisibles.
create index agent_turn_provenance_without_release_idx
    on public.agent_turn_provenance (occurred_at)
    where release_id is null;

-- 3. Registrar un release ------------------------------------------------------

create function public.register_agent_prompt_release_v1(
    p_tenant_ref text,
    p_scope_ref text,
    p_profile_name text,
    p_release_digest text,
    p_artifacts jsonb,
    p_artifacts_modified_at timestamptz,
    p_soul_text text,
    p_observed_at timestamptz,
    p_registered_by text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_existing public.agent_prompt_releases%rowtype;
    v_ordinal integer;
    v_id uuid;
begin
    if p_release_digest !~ '^[a-f0-9]{64}$'
        or jsonb_typeof(p_artifacts) <> 'object'
        or p_artifacts = '{}'::jsonb
        or coalesce(char_length(btrim(p_soul_text)), 0) = 0
    then
        raise exception 'invalid_agent_prompt_release';
    end if;

    -- Observar el mismo release otra vez no es un evento: el cron corre seguido
    -- y lo normal es que nada haya cambiado.
    select * into v_existing
    from public.agent_prompt_releases
    where tenant_ref = p_tenant_ref
      and scope_ref = p_scope_ref
      and release_digest = p_release_digest;

    if found then
        return jsonb_build_object(
            'outcome', 'unchanged',
            'release_id', v_existing.id,
            'release_digest', v_existing.release_digest,
            'release_ordinal', v_existing.release_ordinal,
            'first_observed_at', v_existing.observed_at
        );
    end if;

    -- El contador arranca en 1 y no se reusa: si dos registradores compiten, el
    -- unique de (tenant, scope, ordinal) hace fallar al segundo y reintenta.
    select coalesce(max(release_ordinal), 0) + 1 into v_ordinal
    from public.agent_prompt_releases
    where tenant_ref = p_tenant_ref and scope_ref = p_scope_ref;

    insert into public.agent_prompt_releases(
        tenant_ref, scope_ref, profile_name, release_digest, release_ordinal,
        artifacts, artifacts_modified_at, soul_text, observed_at, registered_by
    )
    values (
        p_tenant_ref, p_scope_ref, p_profile_name, p_release_digest, v_ordinal,
        p_artifacts, p_artifacts_modified_at, p_soul_text,
        coalesce(p_observed_at, clock_timestamp()), p_registered_by
    )
    returning id into v_id;

    return jsonb_build_object(
        'outcome', 'registered',
        'release_id', v_id,
        'release_digest', p_release_digest,
        'release_ordinal', v_ordinal,
        'first_observed_at', coalesce(p_observed_at, clock_timestamp())
    );
end;
$$;

-- 4. Registrar un turno --------------------------------------------------------

create function public.record_agent_turn_provenance_v1(
    p_tenant_ref text,
    p_scope_ref text,
    p_external_conversation_id bigint,
    p_turn_digest text,
    p_occurred_at timestamptz,
    p_outcome text,
    p_model_requested text,
    p_model_answered text,
    p_bridge_release text,
    p_context_builder_version text,
    p_context_digest text,
    p_context_added jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_existing public.agent_turn_provenance%rowtype;
    v_release public.agent_prompt_releases%rowtype;
    v_occurred timestamptz;
    v_id uuid;
begin
    if p_turn_digest !~ '^[a-f0-9]{64}$'
        or p_context_digest !~ '^[a-f0-9]{64}$'
        or p_outcome not in ('completed', 'failed')
        or jsonb_typeof(p_context_added) <> 'object'
    then
        raise exception 'invalid_agent_turn_provenance';
    end if;

    v_occurred := coalesce(p_occurred_at, clock_timestamp());

    select * into v_existing
    from public.agent_turn_provenance
    where turn_digest = p_turn_digest;

    if found then
        return jsonb_build_object(
            'outcome', 'replayed',
            'turn_id', v_existing.id,
            'release_digest', v_existing.release_digest,
            'release_ordinal', v_existing.release_ordinal
        );
    end if;

    -- El release vigente es el mas nuevo observado antes del turno. Si el
    -- registrador nunca corrio, el turno se guarda igual y sin release: un
    -- turno sin procedencia es un dato, no una falla.
    select * into v_release
    from public.agent_prompt_releases
    where tenant_ref = p_tenant_ref
      and scope_ref = p_scope_ref
      and observed_at <= v_occurred
    order by observed_at desc, release_ordinal desc
    limit 1;

    insert into public.agent_turn_provenance(
        tenant_ref, scope_ref, external_conversation_id, turn_digest,
        occurred_at, outcome, release_id, release_digest, release_ordinal,
        model_requested, model_answered, bridge_release,
        context_builder_version, context_digest, context_added
    )
    values (
        p_tenant_ref, p_scope_ref, p_external_conversation_id, p_turn_digest,
        v_occurred, p_outcome, v_release.id, v_release.release_digest,
        v_release.release_ordinal, p_model_requested, p_model_answered,
        p_bridge_release, p_context_builder_version, p_context_digest,
        p_context_added
    )
    returning id into v_id;

    return jsonb_build_object(
        'outcome', case when v_release.id is null
                        then 'recorded_without_release'
                        else 'recorded' end,
        'turn_id', v_id,
        'release_digest', v_release.release_digest,
        'release_ordinal', v_release.release_ordinal
    );
end;
$$;

-- 5. Que tan confiable es la atribucion ----------------------------------------

-- Compara el release SIGUIENTE al atribuido contra el momento del turno. Si los
-- artefactos de ese release siguiente ya estaban modificados antes del turno,
-- entonces el turno corrio algo mas nuevo que lo que se le atribuyo.
create function public.agent_turn_release_confidence_v1(
    p_tenant_ref text,
    p_scope_ref text,
    p_release_ordinal integer,
    p_occurred_at timestamptz
)
returns text
language sql
stable
set search_path = ''
as $$
    select case
        when p_release_ordinal is null then 'no_release'
        when v.artifacts_modified_at is null then 'open'
        when v.artifacts_modified_at > p_occurred_at then 'verified'
        else 'misattributed'
    end
    from (
        select (
            select r.artifacts_modified_at
            from public.agent_prompt_releases r
            where r.tenant_ref = p_tenant_ref
              and r.scope_ref = p_scope_ref
              and r.release_ordinal > p_release_ordinal
            order by r.release_ordinal
            limit 1
        ) as artifacts_modified_at
    ) v;
$$;

-- 6. Lo que lee la revision diaria --------------------------------------------

-- Por conversacion, la procedencia del ULTIMO turno del agente dentro de la
-- ventana: es con ese prompt que se produjo lo que el revisor esta juzgando.
create function public.get_agent_turn_provenance_v1(
    p_tenant_ref text,
    p_scope_ref text,
    p_conversation_ids bigint[],
    p_window_start timestamptz,
    p_window_end timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_result jsonb := '{}'::jsonb;
    v_row record;
begin
    if p_conversation_ids is null or array_length(p_conversation_ids, 1) is null then
        return v_result;
    end if;
    if array_length(p_conversation_ids, 1) > 500 then
        raise exception 'too_many_conversations';
    end if;

    for v_row in
        select distinct on (turn.external_conversation_id)
               turn.external_conversation_id as conversation_id,
               turn.occurred_at,
               turn.release_digest,
               turn.release_ordinal,
               turn.model_requested,
               turn.model_answered,
               turn.bridge_release,
               turn.context_builder_version,
               turn.context_digest,
               turn.context_added,
               turn.outcome,
               public.agent_turn_release_confidence_v1(
                   p_tenant_ref, p_scope_ref, turn.release_ordinal, turn.occurred_at
               ) as confidence
        from public.agent_turn_provenance turn
        where turn.tenant_ref = p_tenant_ref
          and turn.scope_ref = p_scope_ref
          and turn.external_conversation_id = any(p_conversation_ids)
          and (p_window_start is null or turn.occurred_at >= p_window_start)
          and (p_window_end is null or turn.occurred_at <= p_window_end)
        order by turn.external_conversation_id, turn.occurred_at desc
    loop
        v_result := v_result || jsonb_build_object(
            v_row.conversation_id::text,
            jsonb_build_object(
                'occurred_at', v_row.occurred_at,
                'release_digest', v_row.release_digest,
                'release_ordinal', v_row.release_ordinal,
                'model_requested', v_row.model_requested,
                'model_answered', v_row.model_answered,
                'bridge_release', v_row.bridge_release,
                'context_builder_version', v_row.context_builder_version,
                'context_digest', v_row.context_digest,
                'context_added', v_row.context_added,
                'outcome', v_row.outcome,
                'confidence', v_row.confidence
            )
        );
    end loop;

    return v_result;
end;
$$;

-- 7. ACL: las tablas no se tocan de afuera; las funciones, solo service_role ---

alter table public.agent_prompt_releases enable row level security;
alter table public.agent_turn_provenance enable row level security;
revoke all on table public.agent_prompt_releases from public;
revoke all on table public.agent_turn_provenance from public;

revoke execute on function public.agent_prompt_releases_immutable_guard() from public;
revoke execute on function public.register_agent_prompt_release_v1(
    text, text, text, text, jsonb, timestamptz, text, timestamptz, text
) from public;
revoke execute on function public.record_agent_turn_provenance_v1(
    text, text, bigint, text, timestamptz, text, text, text, text, text, text, jsonb
) from public;
revoke execute on function public.agent_turn_release_confidence_v1(
    text, text, integer, timestamptz
) from public;
revoke execute on function public.get_agent_turn_provenance_v1(
    text, text, bigint[], timestamptz, timestamptz
) from public;

-- Los roles de la API existen en Supabase pero no en PGlite ni en un Postgres
-- vacio: se tocan solo si estan (misma forma que 20260927000300).
do $roles$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on table public.agent_prompt_releases from anon;
        revoke all on table public.agent_turn_provenance from anon;
        revoke all on function public.agent_prompt_releases_immutable_guard() from anon;
        revoke all on function public.daily_feedback_item_context_valid(jsonb) from anon;
        revoke execute on function public.register_agent_prompt_release_v1(
            text, text, text, text, jsonb, timestamptz, text, timestamptz, text
        ) from anon;
        revoke execute on function public.record_agent_turn_provenance_v1(
            text, text, bigint, text, timestamptz, text, text, text, text, text,
            text, jsonb
        ) from anon;
        revoke execute on function public.agent_turn_release_confidence_v1(
            text, text, integer, timestamptz
        ) from anon;
        revoke execute on function public.get_agent_turn_provenance_v1(
            text, text, bigint[], timestamptz, timestamptz
        ) from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on table public.agent_prompt_releases from authenticated;
        revoke all on table public.agent_turn_provenance from authenticated;
        revoke all on function public.agent_prompt_releases_immutable_guard() from authenticated;
        revoke all on function public.daily_feedback_item_context_valid(jsonb) from authenticated;
        revoke execute on function public.register_agent_prompt_release_v1(
            text, text, text, text, jsonb, timestamptz, text, timestamptz, text
        ) from authenticated;
        revoke execute on function public.record_agent_turn_provenance_v1(
            text, text, bigint, text, timestamptz, text, text, text, text, text,
            text, jsonb
        ) from authenticated;
        revoke execute on function public.agent_turn_release_confidence_v1(
            text, text, integer, timestamptz
        ) from authenticated;
        revoke execute on function public.get_agent_turn_provenance_v1(
            text, text, bigint[], timestamptz, timestamptz
        ) from authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        -- Lo que NO es entrypoint: el guard del trigger lo invoca Postgres, y la
        -- confianza la llama `get_agent_turn_provenance_v1`, que es security
        -- definer. Sin este revoke, los privilegios por defecto del esquema los
        -- dejarian como dos entradas de servicio que nadie declaro.
        revoke all on function public.agent_prompt_releases_immutable_guard()
            from service_role;
        revoke all on function public.agent_turn_release_confidence_v1(
            text, text, integer, timestamptz
        ) from service_role;
        revoke all on function public.daily_feedback_item_context_valid(jsonb)
            from service_role;
        grant execute on function public.register_agent_prompt_release_v1(
            text, text, text, text, jsonb, timestamptz, text, timestamptz, text
        ) to service_role;
        grant execute on function public.record_agent_turn_provenance_v1(
            text, text, bigint, text, timestamptz, text, text, text, text, text,
            text, jsonb
        ) to service_role;
        grant execute on function public.get_agent_turn_provenance_v1(
            text, text, bigint[], timestamptz, timestamptz
        ) to service_role;
    end if;
end
$roles$;

-- 8. El paquete de revision diaria puede llevar la procedencia ----------------

-- `daily_feedback_item_context_valid` se redefine para admitir una clave mas:
-- `agent_release`, con el digest del release, su version, el modelo que
-- contesto y la CONFIANZA de la atribucion. El resto del validador es el de
-- 20260927000100, extraido textualmente para que no pueda divergir.
--
-- La confianza viaja hasta el revisor a proposito: si dice 'misattributed', lo
-- que se muestra como version del agente no es de fiar, y quien lee el feedback
-- tiene que saberlo en el momento y no tres semanas despues.

create or replace function public.daily_feedback_item_context_valid(p_context jsonb)
returns boolean
language sql
immutable
set search_path = ''
as $$
  select jsonb_typeof(p_context) = 'object'
     and octet_length(p_context::text) <= 32768
     and not exists (
       select 1 from jsonb_object_keys(p_context) k
       where k not in (
         'chatwoot_conversation_id','conversation_url','contact','conversation',
         'origin','events','payment_links','prior_reviews','summary',
         'agent_release'
       )
     )
     and (
       not (p_context ? 'chatwoot_conversation_id')
       or (jsonb_typeof(p_context->'chatwoot_conversation_id') = 'number'
           and (p_context->>'chatwoot_conversation_id') ~ '^[1-9][0-9]{0,17}$')
     )
     and (
       not (p_context ? 'conversation_url')
       or (jsonb_typeof(p_context->'conversation_url') = 'string'
           and char_length(p_context->>'conversation_url') between 9 and 400
           and (p_context->>'conversation_url') ~ '^https://[^[:space:]"''<>\\]+$')
     )
     and (not (p_context ? 'contact') or jsonb_typeof(p_context->'contact') = 'object')
     and (not (p_context ? 'conversation') or jsonb_typeof(p_context->'conversation') = 'object')
     and (not (p_context ? 'origin') or jsonb_typeof(p_context->'origin') = 'string')
     and (not (p_context ? 'events') or jsonb_typeof(p_context->'events') = 'array')
     and (not (p_context ? 'payment_links') or jsonb_typeof(p_context->'payment_links') = 'array')
     and (not (p_context ? 'prior_reviews') or jsonb_typeof(p_context->'prior_reviews') = 'array')
     and (not (p_context ? 'summary') or jsonb_typeof(p_context->'summary') = 'object')
     and (
       not (p_context ? 'agent_release')
       or (jsonb_typeof(p_context->'agent_release') = 'object'
           and (p_context->'agent_release'->>'release_digest') ~ '^[a-f0-9]{64}$'
           and (p_context->'agent_release'->>'release_ordinal') ~ '^[1-9][0-9]{0,8}$'
           and (p_context->'agent_release'->>'confidence')
                 in ('verified', 'open', 'misattributed', 'no_release'))
     )
     and p_context::text !~* 'bearer[[:space:]]+[a-z0-9._~+/=-]{12,}'
     and p_context::text !~* '(javascript|vbscript):'
$$;

revoke all on function public.daily_feedback_item_context_valid(jsonb) from public;

commit;
