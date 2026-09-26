-- Migration: cada derivacion a humano avisa en el canal de Slack (HND-001).
--
-- El hueco que cierra. Desde el 2026-08-23 el sistema derivo 24 conversaciones a
-- una persona (13 entre el 22 y el 25 de septiembre de 2026, medido sobre
-- public.human_handoff_requests el 2026-09-25 23:55Z). Las 24 quedaron en estado
-- 'projected': la automatizacion se paso a pausa durable, Chatwoot recibio la
-- nota privada y la asignacion al equipo 1, y la etiqueta quedo puesta. Nadie
-- fuera de Chatwoot se entero de ninguna. El equipo no vive en Chatwoot: vive en
-- Slack, donde ya recibe las correlaciones de Hotmart que no se pudieron resolver
-- (31 notificaciones admitidas en public.slack_correlation_notification_projection).
-- El codigo HND-001 "Nueva derivacion" existe en el catalogo del conector desde
-- el PR #128 y nunca tuvo emisor.
--
-- Como se resuelve. Con el mismo mecanismo durable que ya funciona para las
-- correlaciones, y no con un aviso en el momento: si el conector esta caido, un
-- aviso en linea se pierde en silencio, que es exactamente la falla que deja al
-- contacto esperando a nadie. La tabla de abajo es la cola; la RPC de claim
-- encola sola desde human_handoff_requests (no hace falta tocar las dos RPC que
-- piden la derivacion, ni un trigger) y leasea de a una; el bridge admite la
-- notificacion en el conector y confirma. Un reintento no duplica la tarjeta: la
-- clave semantica del aviso la deriva el bridge del handoff_request_id, y el
-- conector deduplica por ella.
--
-- Por que las 24 viejas no avisan. Al aplicar esta migracion se siembran como
-- 'suppressed' todas las derivaciones que ya existen. Sin eso, el primer poll del
-- worker vaciaria 24 tarjetas de golpe en el canal, la mitad de ellas de casos ya
-- atendidos, y el equipo aprenderia en el primer dia que el canal es ruido. En
-- esta tabla 'suppressed' significa exactamente eso y nada mas: derivacion
-- anterior al mecanismo. Las conversaciones que hoy siguen esperando respuesta se
-- atienden a mano, no con esta cola.
--
-- Orden de despliegue. El lector va primero: el bridge que sabe leer esta cola se
-- despliega antes de aplicar la migracion. Al revez no rompe nada (las filas
-- quedan 'pending' hasta que haya quien las lea), pero deja el aviso demorado sin
-- que nada lo diga.

create table public.slack_handoff_notification_projection (
    handoff_request_id uuid primary key
        references public.human_handoff_requests(id)
        on delete restrict,
    external_conversation_id bigint not null,
    primary_reason_code text not null,
    detail_reason_code text,
    occurred_at timestamptz not null,
    projection_status text not null default 'pending',
    attempt_count integer not null default 0,
    next_attempt_at timestamptz,
    lease_owner text,
    claim_token uuid,
    lease_generation bigint not null default 0,
    lease_expires_at timestamptz,
    notification_id uuid,
    last_failure_code text,
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    check (external_conversation_id > 0),
    check (primary_reason_code in (
        'explicit_human_request', 'commercial_exception', 'policy_requires_human'
    )),
    check (
        detail_reason_code is null
        or detail_reason_code ~ '^[a-z][a-z0-9_]{0,99}$'
    ),
    check (projection_status in (
        'pending', 'leased', 'retryable_failed', 'admitted', 'terminal_failed',
        'suppressed'
    )),
    check (attempt_count >= 0),
    check (lease_generation >= 0),
    check (
        (projection_status = 'leased'
            and lease_owner is not null
            and claim_token is not null
            and lease_expires_at is not null)
        or (projection_status <> 'leased'
            and lease_owner is null
            and claim_token is null
            and lease_expires_at is null)
    ),
    check (
        (projection_status = 'admitted' and notification_id is not null)
        or (projection_status <> 'admitted' and notification_id is null)
    )
);

create index slack_handoff_notification_projection_claim_idx
    on public.slack_handoff_notification_projection (
        projection_status,
        next_attempt_at,
        occurred_at,
        handoff_request_id
    );

alter table public.slack_handoff_notification_projection enable row level security;

-- Siembra: toda derivacion anterior a esta migracion queda marcada como avisada
-- para que la cola arranque vacia. Ver la cabecera.
insert into public.slack_handoff_notification_projection (
    handoff_request_id,
    external_conversation_id,
    primary_reason_code,
    detail_reason_code,
    occurred_at,
    projection_status
)
select
    request.id,
    request.external_conversation_id,
    request.primary_reason_code,
    request.detail_reason_code,
    request.created_at,
    'suppressed'
from public.human_handoff_requests request
on conflict on constraint slack_handoff_notification_projection_pkey
do nothing;

create or replace function public.claim_slack_handoff_notifications(
    p_worker_id text,
    p_limit integer default 1,
    p_lease_seconds integer default 60
)
returns table (
    handoff_request_id uuid,
    external_conversation_id bigint,
    primary_reason_code text,
    detail_reason_code text,
    occurred_at timestamptz,
    claim_token uuid,
    lease_generation bigint
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_now timestamptz := clock_timestamp();
begin
    if p_worker_id is null or nullif(btrim(p_worker_id), '') is null
       or char_length(p_worker_id) > 200
       or p_limit is distinct from 1
       or p_lease_seconds is null or p_lease_seconds < 30 or p_lease_seconds > 900 then
        raise exception using errcode = '22023',
            message = 'invalid_slack_handoff_projection_claim';
    end if;

    -- Toda derivacion entra en la cola, cualquiera sea el estado de su proyeccion
    -- a Chatwoot: una proyeccion fallida deja al contacto sin nadie asignado, o
    -- sea que el aviso urge mas, no menos.
    insert into public.slack_handoff_notification_projection (
        handoff_request_id,
        external_conversation_id,
        primary_reason_code,
        detail_reason_code,
        occurred_at
    )
    select
        request.id,
        request.external_conversation_id,
        request.primary_reason_code,
        request.detail_reason_code,
        request.created_at
    from public.human_handoff_requests request
    on conflict on constraint slack_handoff_notification_projection_pkey
    do nothing;

    return query
    with candidates as (
        select projection.handoff_request_id
        from public.slack_handoff_notification_projection projection
        where projection.projection_status in (
                  'pending', 'retryable_failed', 'leased'
              )
          and (
              projection.projection_status <> 'retryable_failed'
              or projection.next_attempt_at <= v_now
          )
          and (
              projection.projection_status <> 'leased'
              or projection.lease_expires_at <= v_now
          )
        order by projection.occurred_at, projection.handoff_request_id
        for update of projection skip locked
        limit p_limit
    ),
    claimed as (
        update public.slack_handoff_notification_projection projection
        set projection_status = 'leased',
            lease_owner = p_worker_id,
            claim_token = gen_random_uuid(),
            lease_generation = projection.lease_generation + 1,
            lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
            attempt_count = projection.attempt_count + 1,
            next_attempt_at = null,
            updated_at = v_now
        from candidates
        where projection.handoff_request_id = candidates.handoff_request_id
        returning projection.*
    )
    select
        claimed.handoff_request_id,
        claimed.external_conversation_id,
        claimed.primary_reason_code,
        claimed.detail_reason_code,
        claimed.occurred_at,
        claimed.claim_token,
        claimed.lease_generation
    from claimed
    order by claimed.occurred_at, claimed.handoff_request_id;
end;
$function$;

create or replace function public.complete_slack_handoff_notification(
    p_handoff_request_id uuid,
    p_claim_token uuid,
    p_lease_generation bigint,
    p_notification_id uuid
)
returns table (applied boolean)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_handoff_request_id is null or p_claim_token is null
       or p_lease_generation is null or p_lease_generation < 1
       or p_notification_id is null then
        raise exception using errcode = '22023',
            message = 'invalid_slack_handoff_projection_completion';
    end if;

    return query
    with changed as (
        update public.slack_handoff_notification_projection projection
        set projection_status = 'admitted',
            notification_id = p_notification_id,
            last_failure_code = null,
            lease_owner = null,
            claim_token = null,
            lease_expires_at = null,
            next_attempt_at = null,
            updated_at = clock_timestamp()
        where projection.handoff_request_id = p_handoff_request_id
          and projection.projection_status = 'leased'
          and projection.claim_token = p_claim_token
          and projection.lease_generation = p_lease_generation
        returning 1
    )
    select exists(select 1 from changed);
end;
$function$;

create or replace function public.release_slack_handoff_notification(
    p_handoff_request_id uuid,
    p_claim_token uuid,
    p_lease_generation bigint,
    p_failure_code text
)
returns table (applied boolean)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_handoff_request_id is null or p_claim_token is null
       or p_lease_generation is null or p_lease_generation < 1
       or p_failure_code not in (
           'connector_admission_unknown',
           'connector_semantic_conflict',
           'connector_rejected',
           'connector_unexpected_error'
       ) then
        raise exception using errcode = '22023',
            message = 'invalid_slack_handoff_projection_release';
    end if;

    return query
    with changed as (
        update public.slack_handoff_notification_projection projection
        set projection_status = case
                when p_failure_code = 'connector_admission_unknown'
                     and projection.attempt_count < 8
                    then 'retryable_failed'
                else 'terminal_failed'
            end,
            next_attempt_at = case
                when p_failure_code = 'connector_admission_unknown'
                     and projection.attempt_count < 8
                    then clock_timestamp() + make_interval(
                        secs => least(300, power(2, least(projection.attempt_count, 8))::integer)
                    )
                else null
            end,
            last_failure_code = p_failure_code,
            lease_owner = null,
            claim_token = null,
            lease_expires_at = null,
            updated_at = clock_timestamp()
        where projection.handoff_request_id = p_handoff_request_id
          and projection.projection_status = 'leased'
          and projection.claim_token = p_claim_token
          and projection.lease_generation = p_lease_generation
        returning 1
    )
    select exists(select 1 from changed);
end;
$function$;

revoke all on table public.slack_handoff_notification_projection from public;
revoke execute on function public.claim_slack_handoff_notifications(text, integer, integer) from public;
revoke execute on function public.complete_slack_handoff_notification(uuid, uuid, bigint, uuid) from public;
revoke execute on function public.release_slack_handoff_notification(uuid, uuid, bigint, text) from public;

do $roles$
begin
    if to_regrole('anon') is not null then
        revoke all on table public.slack_handoff_notification_projection from anon;
        revoke execute on function public.claim_slack_handoff_notifications(text, integer, integer) from anon;
        revoke execute on function public.complete_slack_handoff_notification(uuid, uuid, bigint, uuid) from anon;
        revoke execute on function public.release_slack_handoff_notification(uuid, uuid, bigint, text) from anon;
    end if;
    if to_regrole('authenticated') is not null then
        revoke all on table public.slack_handoff_notification_projection from authenticated;
        revoke execute on function public.claim_slack_handoff_notifications(text, integer, integer) from authenticated;
        revoke execute on function public.complete_slack_handoff_notification(uuid, uuid, bigint, uuid) from authenticated;
        revoke execute on function public.release_slack_handoff_notification(uuid, uuid, bigint, text) from authenticated;
    end if;
    if to_regrole('service_role') is not null then
        revoke all on table public.slack_handoff_notification_projection from service_role;
        grant execute on function public.claim_slack_handoff_notifications(text, integer, integer) to service_role;
        grant execute on function public.complete_slack_handoff_notification(uuid, uuid, bigint, uuid) to service_role;
        grant execute on function public.release_slack_handoff_notification(uuid, uuid, bigint, text) to service_role;
    end if;
end;
$roles$;
