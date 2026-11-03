-- handoff-wait-alerts-v1: las derivaciones que el equipo todavia no contesto
-- (E-309, P-20 del plan del 2026-11-02; diagnostico E-308). Contrato:
-- docs/contracts/handoff-wait-alerts-v1.md.
--
-- Por que. Medido el 2026-11-02 (E-308, esperas.sh, solo lectura): en ATT1 la
-- mediana de espera de una derivacion es de 6,1 h y el fin de semana de 59 h;
-- en Maribel 3 de las 4 derivaciones siguen sin una sola respuesta, con 66 a
-- 126 h de espera y la ventana de 24 h ya cerrada. La tarjeta HND-001 sale en
-- el momento de derivar y despues nadie vuelve a avisar. attended_at
-- (20260927000300) coincide con Chatwoot en 37 de 37 derivaciones, asi que es
-- la marca confiable de "ya la contestaron".
--
-- Que suma. Una sola funcion de lectura, list_unattended_human_handoffs_v1,
-- solo para service_role. Devuelve, por conversacion de Chatwoot de la cuenta y
-- la bandeja pedidas, la derivacion mas nueva sin atender (attended_at nulo y
-- status requested, projected o projection_failed, los mismos estados que el
-- indice human_handoff_requests_unattended_idx) creada desde p_since: el id del
-- pedido, el numero de conversacion, los dos motivos y la hora. El bridge la
-- cruza con Chatwoot y decide si avisa por Slack. No devuelve la nota privada,
-- ni el caso, ni nada de la persona.
--
-- Solo lectura: no cambia ninguna tabla, funcion, permiso ni fila. Correrla dos
-- veces aborta con 55000 sin cambiar nada. Errores: 22023 entrada invalida.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regclass('public.human_handoff_requests') is null
       or not exists (
           select 1 from pg_attribute
            where attrelid = 'public.human_handoff_requests'::regclass
              and attname = 'attended_at'
              and not attisdropped
       )
       or not exists (
           select 1 from pg_attribute
            where attrelid = 'public.human_handoff_requests'::regclass
              and attname = 'detail_reason_code'
              and not attisdropped
       ) then
        raise exception using errcode = '55000', message = 'handoff_wait_alerts_prerequisite_missing';
    end if;
    if to_regprocedure(
           'public.list_unattended_human_handoffs_v1(bigint,bigint,timestamptz,integer)'
       ) is not null then
        raise exception using errcode = '55000', message = 'handoff_wait_alerts_already_applied';
    end if;
end;
$already$;

create function public.list_unattended_human_handoffs_v1(
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_since timestamptz,
    p_limit integer default 200
) returns table (
    handoff_request_id uuid,
    external_conversation_id bigint,
    primary_reason_code text,
    detail_reason_code text,
    created_at timestamptz
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
begin
    if p_chatwoot_account_id is null or p_chatwoot_account_id <= 0
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_since is null
       or p_limit is null or p_limit < 1 or p_limit > 500 then
        raise exception using errcode = '22023',
            message = 'list_unattended_human_handoffs_invalid_input';
    end if;

    -- handoff_wait_alerts: begin
    -- Una conversacion derivada dos veces sin respuesta espera a la misma
    -- respuesta: va la derivacion mas nueva, que es la que tiene el motivo de
    -- hoy. distinct on + order by created_at desc, id desc la elige sin
    -- ambiguedad.
    return query
    select latest.id,
           latest.external_conversation_id,
           latest.primary_reason_code,
           latest.detail_reason_code,
           latest.created_at
      from (
          select distinct on (request.external_conversation_id)
                 request.id,
                 request.external_conversation_id,
                 request.primary_reason_code,
                 request.detail_reason_code,
                 request.created_at
            from public.human_handoff_requests request
           where request.chatwoot_account_id = p_chatwoot_account_id
             and request.chatwoot_inbox_id = p_chatwoot_inbox_id
             and request.attended_at is null
             and request.status in ('requested', 'projected', 'projection_failed')
             and request.created_at >= p_since
           order by request.external_conversation_id,
                    request.created_at desc,
                    request.id desc
      ) latest
     order by latest.created_at, latest.external_conversation_id
     limit p_limit;
    -- handoff_wait_alerts: end
end;
$function$;

-- Los permisos: solo service_role (el bridge). Los privilegios por defecto de
-- Supabase le dan EXECUTE a anon y authenticated sobre toda funcion nueva: se
-- revocan explicitamente y se verifica por presencia.
do $acl$
declare
    v_signature text := 'public.list_unattended_human_handoffs_v1(bigint,bigint,timestamptz,integer)';
    v_role text;
begin
    execute format('revoke all on function %s from public', v_signature);
    foreach v_role in array array['anon', 'authenticated'] loop
        if to_regrole(v_role) is not null then
            execute format('revoke all on function %s from %I', v_signature, v_role);
        end if;
    end loop;
    if to_regrole('service_role') is not null then
        execute format('grant execute on function %s to service_role', v_signature);
    end if;
    if exists (
           select 1 from pg_roles
            where rolname = 'service_role'
              and not has_function_privilege(rolname, v_signature, 'EXECUTE')
       )
       or exists (
           select 1 from pg_roles
            where rolname in ('anon', 'authenticated')
              and has_function_privilege(rolname, v_signature, 'EXECUTE')
       ) then
        raise exception using errcode = '55000',
            message = 'handoff_wait_alerts_acl_unexpected',
            detail = format('solo service_role ejecuta %s', v_signature);
    end if;
end;
$acl$;

commit;
