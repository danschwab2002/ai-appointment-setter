-- agent-campaign-phases-v1: el lanzamiento por fechas (4.8 de la lista definitiva).
-- Contrato: docs/contracts/agent-campaign-phases-v1.md.
--
-- Por que. Una campana del Automation Expert (agent-campaigns-v1, 20261108005000)
-- tiene una sola vigencia y un solo bloque. Un lanzamiento cambia de fase a horas
-- fijas (preventa, carrito abierto, ultimo dia, cierre) y hoy una persona lo hace a
-- mano: pausa una version y publica la siguiente. En Maribel, forever-young-2026
-- v3 entro 21 minutos tarde (19/11 16:21Z contra 16:00Z) y su pausa del 19/11
-- 22:38Z dejo 43 horas sin bloque con el trafico prendido: la ola de 15
-- derivaciones en 90 minutos del 20/11.
--
-- 1. _agent_campaign_draft_is_valid acepta dos claves mas en el borrador, con su
--    forma: fases (1 a 6 objetos con clave, nombre, desde y hasta) y bloque_minimo
--    (un objeto con asesoramiento). Todo lo demas es la funcion de 20261108005000,
--    sin cambios: un borrador sin las claves da exactamente lo mismo. Las reglas
--    (ventanas en orden, sin pisarse, adentro de la campana; el bloque minimo sin
--    cupon ni oferta propia) las corre el bridge, como las del resto del borrador.
-- 2. get_agent_campaign_rests_v1 (solo service_role): las campanas en reposo que
--    traen bloque_minimo, pausadas o terminadas hace menos de 14 dias. Con ellas
--    el bridge sigue sirviendo el bloque minimo a quien tiene la etiqueta, en vez
--    de nada. Sin IDs de Slack ni texto libre del aprobador.
--
-- Sin AGENT_CAMPAIGN_PHASES_ENABLED el bridge no llama a la funcion nueva y su
-- parser no deja guardar fases: la base no ve ningun borrador nuevo. Aditiva: no
-- cambia ninguna tabla ni permiso existente. Correrla dos veces aborta con 55000
-- sin cambiar nada.
--
-- Errores: 22023 entrada invalida.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.agent_campaigns') is null
       or to_regprocedure('public._agent_campaign_draft_is_valid(jsonb)') is null
       or to_regprocedure('public._flow_store_instance(jsonb)') is null then
        raise exception using errcode = '55000', message = 'agent_campaign_phases_prerequisite_missing';
    end if;
    if to_regprocedure('public.get_agent_campaign_rests_v1(jsonb)') is not null then
        raise exception using errcode = '55000', message = 'agent_campaign_phases_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. La forma del borrador, con fases y bloque minimo.
-- ===========================================================================

-- create or replace conserva los permisos (de nadie, 20261108005000) y el CHECK de
-- agent_campaigns sigue apuntando a la misma funcion. Las filas de hoy no traen las
-- claves nuevas: siguen validas.
create or replace function public._agent_campaign_draft_is_valid(p_draft jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog, pg_temp
as $function$
declare
    v_keys text[];
begin
    if jsonb_typeof(p_draft) is distinct from 'object' or octet_length(p_draft::text) > 32768 then
        return false;
    end if;
    select array_agg(k order by k) into v_keys from jsonb_object_keys(p_draft) k;
    if not v_keys <@ array['asesoramiento', 'bloque_minimo', 'clave', 'desde', 'etiqueta', 'faq', 'fases', 'hasta',
                           'instruccion', 'macro_etiqueta', 'no_confirmado', 'nombre', 'oferta', 'palabras_clave',
                           'pregunta_entrada', 'preguntas_del_alta', 'promesas_prohibidas', 'se_puede_decir']
       or not v_keys @> array['asesoramiento', 'clave', 'desde', 'etiqueta', 'hasta', 'macro_etiqueta', 'nombre'] then
        return false;
    end if;
    return coalesce(
        jsonb_typeof(p_draft -> 'clave') = 'string'
        and p_draft ->> 'clave' ~ '^[a-z0-9][a-z0-9-]{0,127}$'
        and jsonb_typeof(p_draft -> 'nombre') = 'string'
        and length(p_draft ->> 'nombre') between 1 and 120
        and jsonb_typeof(p_draft -> 'etiqueta') = 'string'
        and p_draft ->> 'etiqueta' ~ '^[a-z0-9][a-z0-9_-]{0,39}$'
        and jsonb_typeof(p_draft -> 'macro_etiqueta') = 'number'
        and (p_draft ->> 'macro_etiqueta') ~ '^[1-9][0-9]{0,11}$'
        and jsonb_typeof(p_draft -> 'desde') = 'string'
        and p_draft ->> 'desde' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+-][0-9]{2}:[0-9]{2})$'
        and jsonb_typeof(p_draft -> 'hasta') = 'string'
        and p_draft ->> 'hasta' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+-][0-9]{2}:[0-9]{2})$'
        and jsonb_typeof(p_draft -> 'asesoramiento') = 'array'
        and jsonb_array_length(p_draft -> 'asesoramiento') between 1 and 100
        -- Sin palabra clave y sin pregunta no entraria nadie.
        and (p_draft ? 'palabras_clave' or p_draft ? 'pregunta_entrada')
        and (not p_draft ? 'palabras_clave' or (jsonb_typeof(p_draft -> 'palabras_clave') = 'array'
             and jsonb_array_length(p_draft -> 'palabras_clave') between 1 and 8))
        and (not p_draft ? 'pregunta_entrada' or (jsonb_typeof(p_draft -> 'pregunta_entrada') = 'string'
             and length(p_draft ->> 'pregunta_entrada') between 1 and 200))
        and (not p_draft ? 'oferta' or jsonb_typeof(p_draft -> 'oferta') = 'object')
        -- 4.8: las fases, con su clave corta y su ventana; nunca sin el bloque minimo.
        and (not p_draft ? 'fases' or (p_draft ? 'bloque_minimo'
             and jsonb_typeof(p_draft -> 'fases') = 'array'
             and jsonb_array_length(p_draft -> 'fases') between 1 and 6
             and not exists (
                 select 1 from jsonb_array_elements(p_draft -> 'fases') f
                  where not coalesce(jsonb_typeof(f) = 'object'
                      and jsonb_typeof(f -> 'clave') = 'string'
                      and f ->> 'clave' ~ '^[a-z0-9]([a-z0-9-]{0,22}[a-z0-9])?$'
                      and jsonb_typeof(f -> 'nombre') = 'string'
                      and length(f ->> 'nombre') between 1 and 120
                      and jsonb_typeof(f -> 'desde') = 'string'
                      and f ->> 'desde' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+-][0-9]{2}:[0-9]{2})$'
                      and jsonb_typeof(f -> 'hasta') = 'string'
                      and f ->> 'hasta' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+-][0-9]{2}:[0-9]{2})$',
                      false))))
        -- 4.8: el bloque minimo, con su asesoramiento.
        and (not p_draft ? 'bloque_minimo' or (jsonb_typeof(p_draft -> 'bloque_minimo') = 'object'
             and jsonb_typeof(p_draft -> 'bloque_minimo' -> 'asesoramiento') = 'array'
             and jsonb_array_length(p_draft -> 'bloque_minimo' -> 'asesoramiento') between 1 and 100)), false);
end;
$function$;

-- ===========================================================================
-- 2. Las campanas en reposo, para el bloque minimo.
-- ===========================================================================

-- Lo que el bridge sirve fuera de la vigencia: las pausadas y las terminadas hace
-- menos de 14 dias que traen bloque_minimo, una fila por campana. El bridge decide
-- si la sirve (una publicada con su etiqueta o su clave gana) y recalcula el
-- chequeo_sha. Sin IDs de Slack.
create function public.get_agent_campaign_rests_v1(p_instance jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_rows jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    select coalesce(jsonb_agg(jsonb_build_object(
               'campaign_id', c.id,
               'status', c.status,
               'campaign_key', c.campaign_key,
               'segment_version', c.segment_version,
               'label', c.label,
               'label_macro_id', c.label_macro_id,
               'draft', c.draft,
               'fingerprint', c.fingerprint,
               'check_sha', c.check_sha,
               'approved_at', c.approved_at) order by c.approved_at desc, c.id), '[]'::jsonb)
      into v_rows
      from (select c.*
              from public.agent_campaigns c
             where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref
               and c.status in ('pausada', 'terminada')
               and c.approved_at is not null
               and c.draft ? 'bloque_minimo'
               and c.valid_until > clock_timestamp() - interval '14 days'
             order by c.approved_at desc, c.id
             limit 20) c;
    return jsonb_build_object('campaigns', v_rows, 'database_at', clock_timestamp());
end;
$function$;

-- ===========================================================================
-- 3. Los permisos: la nueva, solo para service_role.
-- ===========================================================================

do $acl$
declare
    v_role text;
begin
    revoke all on function public.get_agent_campaign_rests_v1(jsonb) from public;
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on function public.get_agent_campaign_rests_v1(jsonb) from %I', v_role);
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.get_agent_campaign_rests_v1(jsonb) to service_role;
    end if;
end;
$acl$;

commit;
