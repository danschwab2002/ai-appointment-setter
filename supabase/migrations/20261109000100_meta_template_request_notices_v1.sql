-- meta-template-request-notices-v1: el sondeo del estado en Meta y el aviso en el
-- hilo de Slack donde se pidio la plantilla (F-146 parte 4, E-361).
-- Contrato: docs/contracts/template-request-v1.md (1.3.0).
--
-- Decision de Dan del 2026-11-09: cuando Meta contesta, el sistema avisa en el
-- mismo hilo donde se le pidio la plantilla al Automation Expert.
--
-- 1. Dos columnas nuevas, vacias, en meta_template_requests: el canal y el hilo
--    de Slack. Las filas viejas quedan sin hilo: no reciben aviso y se cuentan.
-- 2. save_meta_template_request_draft_v2: la v1 y, en la misma transaccion, el
--    hilo. La v1 no cambia. Ninguna lectura del agente trae el hilo.
-- 3. Una tabla nueva, vacia: meta_template_request_notices. Una fila por
--    solicitud y aviso (la clave primaria es lo que hace que un aviso salga una
--    sola vez, tambien despues de un reinicio).
-- 4. Tres funciones de service_role: las solicitudes a sondear, reclamar los
--    avisos que tocan y cerrar uno.
--
-- El aviso sale del ESTADO de la solicitud y no del paso que la movio: una
-- plantilla que Meta contesto al crearla (la pagina) y una que el sondeo vio
-- cambiar avisan igual, y una caida entre anotar el estado y avisar no pierde
-- el aviso.
--
-- Una sola vez: se reclama antes de postear. Un reclamo que nadie cerro en 5
-- minutos (el proceso se cayo con el mensaje en el aire) queda 'unknown' y no
-- se reintenta: Slack no tiene idempotencia y repetir es peor que callar, porque
-- el AE igual lee el estado cuando el duenio vuelve. Solo se reintenta lo que
-- Slack rechazo de forma expresa (no llego), hasta 3 intentos. Si lo que rechazo
-- es el token o el canal, el aviso queda 'waiting': no gasta intentos, /ready lo
-- dice y sale cuando se corrige.
--
-- Aditiva: no cambia ninguna funcion, tabla ni permiso existente, y sin
-- META_TEMPLATES_ENABLED el bridge no llama a nada de esto. Correrla dos veces
-- aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida. Los resultados vuelven como
-- {"ok": false, "reason_code"}.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.meta_template_requests') is null
       or to_regprocedure('public.save_meta_template_request_draft_v1(jsonb,uuid,integer,jsonb,text,uuid,text,text)') is null
       or to_regprocedure('public._flow_store_instance(jsonb)') is null then
        raise exception using errcode = '55000', message = 'meta_template_request_notices_prerequisite_missing';
    end if;
    if to_regclass('public.meta_template_request_notices') is not null then
        raise exception using errcode = '55000', message = 'meta_template_request_notices_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. El hilo donde se pidio.
-- ===========================================================================

alter table public.meta_template_requests
    add column notify_slack_channel text check (notify_slack_channel ~ '^[CDG][A-Z0-9]{8,20}$'),
    add column notify_slack_thread_ts text check (notify_slack_thread_ts ~ '^[0-9]{1,12}\.[0-9]{1,8}$'),
    add constraint meta_template_requests_notify_thread_needs_channel
        check (notify_slack_thread_ts is null or notify_slack_channel is not null);

-- ===========================================================================
-- 2. Guardar con el hilo.
-- ===========================================================================

create function public.save_meta_template_request_draft_v2(
    p_instance jsonb, p_request_id uuid, p_expected_revision integer, p_draft jsonb, p_purpose text,
    p_proposal_id uuid, p_step_pointer text, p_requested_by_ref text, p_slack_channel text, p_slack_thread_ts text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_saved jsonb;
begin
    if p_slack_channel is null and p_slack_thread_ts is not null
       or p_slack_channel !~ '^[CDG][A-Z0-9]{8,20}$'
       or p_slack_thread_ts !~ '^[0-9]{1,12}\.[0-9]{1,8}$' then
        raise exception using errcode = '22023', message = 'meta_template_slack_thread_invalid';
    end if;
    v_saved := public.save_meta_template_request_draft_v1(p_instance, p_request_id, p_expected_revision, p_draft,
        p_purpose, p_proposal_id, p_step_pointer, p_requested_by_ref);
    -- Rehacer desde otra charla mueve el aviso a la ultima; sin sesion de Slack, se queda el que habia.
    if (v_saved ->> 'ok')::boolean is true and p_slack_channel is not null then
        update public.meta_template_requests
           set notify_slack_channel = p_slack_channel, notify_slack_thread_ts = p_slack_thread_ts
         where id = (v_saved ->> 'request_id')::uuid;
    end if;
    return v_saved;
end;
$function$;

-- ===========================================================================
-- 3. Los avisos.
-- ===========================================================================

create table public.meta_template_request_notices (
    request_id uuid not null references public.meta_template_requests (id) on delete restrict,
    kind text not null check (kind in ('aprobada', 'rechazada', 'recategorizada')),
    state text not null check (state in ('claimed', 'sent', 'retry', 'waiting', 'failed', 'unknown', 'no_thread')),
    attempts integer not null default 0 check (attempts between 0 and 3),
    claimed_at timestamptz not null default clock_timestamp(),
    next_attempt_at timestamptz,
    finished_at timestamptz,
    last_error text check (last_error ~ '^[a-z][a-z0-9_:]{0,79}$'),
    primary key (request_id, kind)
);

alter table public.meta_template_request_notices enable row level security;

create index meta_template_request_notices_open on public.meta_template_request_notices (state)
    where state in ('claimed', 'retry', 'waiting');

-- ===========================================================================
-- 4. Las funciones del sondeo.
-- ===========================================================================

-- Las solicitudes que ya llegaron a Meta y toca mirar: cada 5 minutos en la
-- primera hora del envio y cada 30 despues, hasta 7 dias del envio. Anota
-- last_polled_at al entregarlas, asi un Meta caido no se pide en cada vuelta.
-- Una que se borro en WhatsApp Manager (meta_status DELETED) no se mira mas.
--
-- Una enviada sin id (el envio quedo a medias) se busca por nombre solo si Meta
-- pudo haberla creado: sin error, o con un error que no dice que no. Hasta 48
-- horas del ultimo intento y con 5 minutos de gracia para no pisar un envio en
-- curso.
create function public.list_meta_template_requests_to_poll_v1(p_instance jsonb, p_limit integer)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_now timestamptz;
    v_rows jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_limit is null or p_limit < 1 or p_limit > 50 then
        raise exception using errcode = '22023', message = 'meta_template_list_parameters_invalid';
    end if;
    v_now := clock_timestamp();
    with due as (
        select r.id from public.meta_template_requests r
         where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
           and (
               (r.status in ('en_revision', 'sin_respuesta_de_meta', 'aprobada_meta')
                and r.meta_template_id is not null
                and coalesce(r.sent_at, r.created_at) > v_now - interval '7 days'
                and r.meta_status is distinct from 'DELETED')
               or (r.status = 'enviada' and r.meta_template_id is null
                   and r.send_started_at <= v_now - interval '5 minutes'
                   and r.send_started_at > v_now - interval '48 hours'
                   and (r.last_send_error is null
                        or r.last_send_error in ('meta_unreachable', 'meta_unexpected_answer')
                        or r.last_send_error ~ '^(meta_http:5[0-9]{2}|meta:[12](:[0-9]+)?)$'))
           )
           and (r.last_polled_at is null or r.last_polled_at <= v_now - case
                   when coalesce(r.sent_at, r.created_at) > v_now - interval '1 hour' then interval '5 minutes'
                   else interval '30 minutes' end)
         order by r.last_polled_at nulls first, r.id
         limit p_limit
         for update skip locked
    ), touched as (
        update public.meta_template_requests r
           set last_polled_at = v_now
          from due
         where r.id = due.id
        returning r.id, r.status, r.meta_template_id, r.template_name, r.language, r.draft, r.validation
    )
    select coalesce(jsonb_agg(jsonb_build_object(
        'request_id', t.id, 'status', t.status, 'meta_template_id', t.meta_template_id,
        'template_name', t.template_name, 'language', t.language, 'draft', t.draft,
        'meta_components', t.validation #> '{meta_request,components}') order by t.id), '[]'::jsonb)
      into v_rows from touched t;
    return jsonb_build_object('ok', true, 'requests', v_rows);
end;
$function$;

-- Reclama los avisos que tocan y devuelve lo necesario para postearlos, con los
-- conteos de la instancia para /ready. Solicitud sin hilo: queda 'no_thread',
-- contada, y no vuelve.
create function public.claim_meta_template_notices_v1(p_instance jsonb, p_limit integer)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_now timestamptz;
    v_notices jsonb;
    v_counts jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_limit is null or p_limit < 1 or p_limit > 50 then
        raise exception using errcode = '22023', message = 'meta_template_list_parameters_invalid';
    end if;
    v_now := clock_timestamp();

    -- Quedo en el aire: no se sabe si salio, y no se repite.
    update public.meta_template_request_notices n
       set state = 'unknown', finished_at = v_now, last_error = 'claim_abandoned'
      from public.meta_template_requests r
     where r.id = n.request_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
       and n.state = 'claimed' and n.claimed_at <= v_now - interval '5 minutes';

    insert into public.meta_template_request_notices (request_id, kind, state, attempts, claimed_at, finished_at)
    select r.id, k.kind,
           case when r.notify_slack_channel is null then 'no_thread' else 'claimed' end,
           case when r.notify_slack_channel is null then 0 else 1 end,
           v_now,
           case when r.notify_slack_channel is null then v_now end
      from public.meta_template_requests r
     cross join lateral (select case
            when r.status = 'rechazada' then 'rechazada'
            when r.status in ('aprobada_meta', 'en_catalogo') and r.meta_category is distinct from r.category_requested
                then 'recategorizada'
            when r.status in ('aprobada_meta', 'en_catalogo') then 'aprobada'
        end as kind) k
     where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
       and k.kind is not null
       and not exists (select 1 from public.meta_template_request_notices n
                        where n.request_id = r.id and n.kind = k.kind)
     order by coalesce(r.meta_answered_at, r.updated_at), r.id
     limit p_limit
    on conflict (request_id, kind) do nothing;

    update public.meta_template_request_notices n
       set state = 'claimed', attempts = n.attempts + 1, claimed_at = v_now, next_attempt_at = null
      from public.meta_template_requests r
     where r.id = n.request_id and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
       and n.state in ('retry', 'waiting') and n.next_attempt_at <= v_now;

    select coalesce(jsonb_agg(jsonb_build_object(
        'request_id', n.request_id, 'kind', n.kind, 'channel', r.notify_slack_channel,
        'thread_ts', r.notify_slack_thread_ts, 'template_name', r.template_name, 'status', r.status,
        'category_requested', r.category_requested, 'meta_category', r.meta_category,
        'meta_rejected_reason', r.meta_rejected_reason, 'meta_template_id', r.meta_template_id) order by n.request_id, n.kind), '[]'::jsonb)
      into v_notices
      from public.meta_template_request_notices n
      join public.meta_template_requests r on r.id = n.request_id
     where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
       and n.state = 'claimed' and n.claimed_at = v_now;

    select coalesce(jsonb_object_agg(c.state, c.total), '{}'::jsonb) into v_counts
      from (select n.state, count(*) as total
              from public.meta_template_request_notices n
              join public.meta_template_requests r on r.id = n.request_id
             where r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
             group by n.state) c;

    return jsonb_build_object('ok', true, 'notices', v_notices, 'counts', v_counts);
end;
$function$;

-- Lo que paso con un aviso reclamado. 'retry' (Slack lo rechazo: no llego) vuelve
-- a los 5 minutos, y al tercer intento queda 'failed'. 'waiting' (el token o el
-- canal de Slack) vuelve a los 15 y devuelve el intento que gasto el reclamo.
create function public.finish_meta_template_notice_v1(
    p_instance jsonb, p_request_id uuid, p_kind text, p_outcome text, p_error_code text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_now timestamptz;
    v_notice public.meta_template_request_notices%rowtype;
    v_state text;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_outcome is null or p_outcome not in ('sent', 'retry', 'waiting', 'failed', 'unknown')
       or p_kind is null or p_kind not in ('aprobada', 'rechazada', 'recategorizada')
       or p_error_code !~ '^[a-z][a-z0-9_:]{0,79}$' then
        raise exception using errcode = '22023', message = 'meta_template_notice_invalid';
    end if;
    select n.* into v_notice from public.meta_template_request_notices n
      join public.meta_template_requests r on r.id = n.request_id
     where n.request_id = p_request_id and n.kind = p_kind
       and r.tenant_ref = v_instance.o_tenant_ref and r.ally_ref = v_instance.o_ally_ref
       for update of n;
    if not found or v_notice.state <> 'claimed' then
        return jsonb_build_object('ok', false, 'reason_code', 'meta_template_notice_not_claimed');
    end if;
    v_now := clock_timestamp();
    v_state := case when p_outcome = 'retry' and v_notice.attempts >= 3 then 'failed' else p_outcome end;
    update public.meta_template_request_notices
       set state = v_state, last_error = p_error_code,
           attempts = case when v_state = 'waiting' then attempts - 1 else attempts end,
           next_attempt_at = case v_state when 'retry' then v_now + interval '5 minutes'
                                          when 'waiting' then v_now + interval '15 minutes' end,
           finished_at = case when v_state in ('retry', 'waiting') then null else v_now end
     where request_id = p_request_id and kind = p_kind;
    return jsonb_build_object('ok', true, 'state', v_state);
end;
$function$;

-- ===========================================================================
-- 5. Los permisos. La tabla, para nadie de la API. Cada funcion, solo su rol.
-- ===========================================================================

do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    revoke all on table public.meta_template_request_notices from public;
    for v_role in
        select r.rolname from pg_roles r where r.rolname in ('anon', 'authenticated', 'service_role') order by 1
    loop
        execute format('revoke all on table public.meta_template_request_notices from %I', v_role);
    end loop;
    foreach v_function in array array[
        'public.save_meta_template_request_draft_v2(jsonb,uuid,integer,jsonb,text,uuid,text,text,text,text)',
        'public.list_meta_template_requests_to_poll_v1(jsonb,integer)',
        'public.claim_meta_template_notices_v1(jsonb,integer)',
        'public.finish_meta_template_notice_v1(jsonb,uuid,text,text,text)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in
            select r.rolname from pg_roles r where r.rolname in ('anon', 'authenticated', 'service_role') order by 1
        loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
    end loop;
    if to_regrole('automation_expert_change') is not null then
        grant execute on function public.save_meta_template_request_draft_v2(jsonb, uuid, integer, jsonb, text, uuid,
            text, text, text, text) to automation_expert_change;
    end if;
    if to_regrole('service_role') is not null then
        grant execute on function public.list_meta_template_requests_to_poll_v1(jsonb, integer) to service_role;
        grant execute on function public.claim_meta_template_notices_v1(jsonb, integer) to service_role;
        grant execute on function public.finish_meta_template_notice_v1(jsonb, uuid, text, text, text) to service_role;
    end if;
end;
$acl$;

commit;
