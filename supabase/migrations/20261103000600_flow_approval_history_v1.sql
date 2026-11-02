-- flow-approval-history-v1: el historial de propuestas en la pagina de
-- aprobacion (E-295). Contrato: docs/contracts/flow-change-cycle-v1.md,
-- «El sí por un link en el chat del Automation Expert (1.9.0)»; molde de la
-- lista: 20261030000300 (list_flow_changes_v1, la del agente).
--
-- Hoy la portada de /flow-approval/ lista solo las validadas o aprobadas sin
-- vencer (approver_read_open_flow_changes_v1): una propuesta publicada,
-- descartada o vencida no la ve nadie desde la pagina, y su detalle no dice
-- quien la cerro ni que version genero. Esta migracion suma dos lecturas, las
-- dos con la sesion de la pagina (el actor sale de la fila de la sesion viva,
-- como en todos los envoltorios del aprobador):
--
-- * approver_list_flow_changes_v1(p_instance, p_session_hash,
--   p_before_proposal_id, p_limit): todas las propuestas de la instancia, de
--   la mas nueva a la mas vieja (created_at e id, descendentes), hasta p_limit
--   (1 a 50). Con p_before_proposal_id, las que van despues de esa en el
--   mismo orden (la pagina siguiente, por cursor y no por offset: una
--   propuesta nueva no corre la lista). Cada fila: estado guardado y
--   efectivo (vencida si paso expires_at), los flujos que toca (producto,
--   flujo y tipo de cada item), quien la pidio, la aprobo y la cerro (clase y
--   ref: un ID de Slack o el codigo del operador o del sistema, que la base ya
--   exige con _flow_store_actor_is_valid), las fechas del ciclo, el ultimo
--   evento y las versiones que publico. Ni el «para que», ni el motivo del
--   descarte, ni la vista previa, ni los items completos.
-- * approver_read_flow_change_record_v1(p_instance, p_session_hash,
--   p_proposal_id): lo que el detalle suma a get_flow_change_proposal_v1:
--   quien la cerro y su motivo (texto libre de una linea, que la pagina
--   muestra escapado como texto sin verificar), las versiones que publico y la
--   linea de tiempo entera de sus eventos (hasta 200, la mas vieja primero),
--   con el actor y el flujo de cada uno pero sin su data.
--
-- Solo lectura: no cambia ninguna tabla, funcion ni permiso existente, y
-- ninguna fila. Las ejecuta solo flow_change_approver (ni anon, ni
-- authenticated, ni service_role). Volatiles por la marca de uso de la sesion
-- (last_seen_at), como las demas lecturas del rol. Correrla dos veces aborta
-- con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida (la instancia, o p_limit fuera de rango);
-- 28000 sesion invalida (flow_approval_session_invalid); P0002 la propuesta
-- (o la del cursor) no es de la instancia (flow_change_not_found).

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_approval_session_actor(text,text,text)') is null
       or to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regclass('public.flow_change_proposals') is null
       or to_regclass('public.flow_change_events') is null
       or to_regclass('public.flow_definitions') is null then
        raise exception using errcode = '55000', message = 'flow_approval_history_prerequisite_missing';
    end if;
    if to_regprocedure('public.approver_list_flow_changes_v1(jsonb,text,uuid,integer)') is not null
       or to_regprocedure('public.approver_read_flow_change_record_v1(jsonb,text,uuid)') is not null then
        raise exception using errcode = '55000', message = 'flow_approval_history_already_applied';
    end if;
end;
$already$;

-- Las versiones que publico una propuesta: flujo, producto y numero.
create function public._flow_approval_published_versions(p_proposal_id uuid)
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
    select coalesce(jsonb_agg(jsonb_build_object(
               'flow_id', d.flow_id, 'producto', c.product_ref, 'flujo', c.flow_key,
               'version', d.version, 'published_at', d.published_at)
             order by c.product_ref, c.flow_key, d.version), '[]'::jsonb)
      from public.flow_definitions d
      join public.flow_controls c on c.flow_id = d.flow_id
     where d.proposal_id = p_proposal_id
$function$;

-- El historial de la pagina, por cursor.
create function public.approver_list_flow_changes_v1(
    p_instance jsonb, p_session_hash text, p_before_proposal_id uuid, p_limit integer)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_before_at timestamptz;
    v_rows jsonb;
    v_count integer;
    v_total integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if p_limit is null or p_limit < 1 or p_limit > 50 then
        raise exception using errcode = '22023', message = 'flow_approval_history_parameters_invalid';
    end if;
    if p_before_proposal_id is not null then
        select p.created_at into v_before_at from public.flow_change_proposals p
         where p.id = p_before_proposal_id and p.tenant_ref = v_instance.o_tenant_ref
           and p.ally_ref = v_instance.o_ally_ref;
        if not found then
            raise exception using errcode = 'P0002', message = 'flow_change_not_found';
        end if;
    end if;
    select count(*)::integer into v_total from public.flow_change_proposals p
     where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref;
    -- Una fila de mas para saber si hay otra pagina.
    select coalesce(jsonb_agg(row_data order by created_at desc, proposal_id desc), '[]'::jsonb), count(*)::integer
      into v_rows, v_count
      from (
        select p.created_at, p.id as proposal_id, jsonb_build_object(
                   'proposal_id', p.id,
                   'status', p.status,
                   'effective_status', case
                       when p.status in ('validada', 'aprobada') and clock_timestamp() >= p.expires_at
                       then 'vencida' else p.status end,
                   'revision', p.revision,
                   'flows', (select coalesce(jsonb_agg(jsonb_build_object(
                                 'producto', item.value ->> 'producto', 'flujo', item.value ->> 'flujo',
                                 'tipo', item.value ->> 'tipo') order by item.position), '[]'::jsonb)
                               from jsonb_array_elements(p.items) with ordinality as item(value, position)),
                   'requested_by_kind', p.requested_by_kind,
                   'requested_by_ref', p.requested_by_ref,
                   'approved_by_kind', p.approved_by_kind,
                   'approved_by_ref', p.approved_by_ref,
                   'closed_by_kind', p.closed_by_kind,
                   'closed_by_ref', p.closed_by_ref,
                   'created_at', p.created_at,
                   'validated_at', p.validated_at,
                   'expires_at', p.expires_at,
                   'approved_at', p.approved_at,
                   'published_at', p.published_at,
                   'closed_at', p.closed_at,
                   'versions', public._flow_approval_published_versions(p.id),
                   'last_event', (
                       select jsonb_build_object('event_type', e.event_type, 'actor_kind', e.actor_kind,
                                                 'actor_ref', e.actor_ref, 'created_at', e.created_at)
                         from public.flow_change_events e
                        where e.proposal_id = p.id
                        order by e.created_at desc, e.id desc
                        limit 1)) as row_data
          from public.flow_change_proposals p
         where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
           and (p_before_proposal_id is null or (p.created_at, p.id) < (v_before_at, p_before_proposal_id))
         order by p.created_at desc, p.id desc
         limit p_limit + 1) listed;
    if v_count > p_limit then
        v_rows := v_rows - p_limit;
    end if;
    return jsonb_build_object(
        'proposals', v_rows,
        'has_more', v_count > p_limit,
        'next_before', case when v_count > p_limit then v_rows -> (p_limit - 1) -> 'proposal_id' end,
        'total', v_total,
        'database_at', clock_timestamp());
end;
$function$;

-- Lo que el detalle de una propuesta suma a get_flow_change_proposal_v1.
create function public.approver_read_flow_change_record_v1(
    p_instance jsonb, p_session_hash text, p_proposal_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_proposal public.flow_change_proposals%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    select * into v_proposal from public.flow_change_proposals p
     where p.id = p_proposal_id and p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_change_not_found';
    end if;
    return jsonb_build_object(
        'proposal_id', v_proposal.id,
        'status', v_proposal.status,
        'effective_status', case
            when v_proposal.status in ('validada', 'aprobada') and clock_timestamp() >= v_proposal.expires_at
            then 'vencida' else v_proposal.status end,
        'requested_by_kind', v_proposal.requested_by_kind,
        'requested_by_ref', v_proposal.requested_by_ref,
        'approved_by_kind', v_proposal.approved_by_kind,
        'approved_by_ref', v_proposal.approved_by_ref,
        'closed_by_kind', v_proposal.closed_by_kind,
        'closed_by_ref', v_proposal.closed_by_ref,
        'close_reason', v_proposal.close_reason,
        'created_at', v_proposal.created_at,
        'validated_at', v_proposal.validated_at,
        'expires_at', v_proposal.expires_at,
        'approved_at', v_proposal.approved_at,
        'published_at', v_proposal.published_at,
        'closed_at', v_proposal.closed_at,
        'versions', public._flow_approval_published_versions(v_proposal.id),
        'events_total', (select count(*)::integer from public.flow_change_events e
                          where e.proposal_id = v_proposal.id),
        'events', coalesce((
            select jsonb_agg(jsonb_build_object(
                       'event_type', e.event_type, 'actor_kind', e.actor_kind, 'actor_ref', e.actor_ref,
                       'reason_code', e.reason_code, 'generation', e.generation, 'created_at', e.created_at,
                       'producto', c.product_ref, 'flujo', c.flow_key)
                     order by e.created_at, e.id)
              from (select * from public.flow_change_events e
                     where e.proposal_id = v_proposal.id
                     order by e.created_at, e.id limit 200) e
              left join public.flow_controls c on c.flow_id = e.flow_id), '[]'::jsonb));
end;
$function$;

-- Los permisos: solo el rol de la pagina. Los privilegios por defecto de
-- Supabase le dan EXECUTE a anon, authenticated y service_role sobre toda
-- funcion nueva: se revocan explicitamente. La interna no la ejecuta nadie.
do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public._flow_approval_published_versions(uuid)',
        'public.approver_list_flow_changes_v1(jsonb,text,uuid,integer)',
        'public.approver_read_flow_change_record_v1(jsonb,text,uuid)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role')
             order by r.rolname
        loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
        -- El rol de la pagina lo crea 20261028000100; una base sin el no lo nombra.
        if v_function <> 'public._flow_approval_published_versions(uuid)'::regprocedure
           and to_regrole('flow_change_approver') is not null then
            execute format('grant execute on function %s to flow_change_approver', v_function);
        end if;
    end loop;
end;
$acl$;

commit;
