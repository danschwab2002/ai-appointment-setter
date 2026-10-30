-- flow-change-list-v1: el Automation Expert ve sus propuestas y en que estado
-- estan (el paso P7 del mapa M16). Contrato: docs/contracts/flow-change-cycle-v1.md
-- (1.10.0, «La lista de las propuestas»).
--
-- Hoy el agente relee una propuesta solo si sabe su id
-- (get_flow_change_proposal_v1), y no guarda nada de una charla para otra: una
-- propuesta que quedo validada, vencida o publicada en otra conversacion no la
-- ve nadie desde el chat. Esta migracion suma una sola lectura:
--
-- * list_flow_changes_v1(p_instance, p_only_open, p_limit): las propuestas de
--   la instancia, de la mas nueva a la mas vieja, hasta p_limit (1 a 50). Con
--   p_only_open, solo las abiertas con la regla del tope del agente
--   (save_flow_change_draft_v1, 20261021000100): un borrador, o una validada o
--   aprobada sin vencer. Cada fila trae su estado guardado y el efectivo
--   (vencida si paso expires_at), la revision, quien la pidio por clase, los
--   flujos que toca (producto, flujo y tipo de cada item), las fechas del
--   ciclo y el ultimo evento del ciclo de la propuesta (sin los
--   flow_control_changed que deja publicar: esos son del flujo). El «para
--   que» va solo en las del agente: el de una del operador no lo escribio el
--   agente. Suma tambien cuantas abiertas tiene el agente y su tope (10),
--   para que diga cuantas le quedan.
--
-- Nada de una persona: ni el ID de Slack de quien la pidio o la aprobo
-- (requested_by_ref, approved_by_ref), ni la vista previa, ni los items
-- completos (eso lo da get_flow_change_proposal_v1 con el id).
--
-- La ejecuta solo automation_expert_change (el rol del agente, 20261021000100),
-- como los cuatro envoltorios del agente: ni anon, ni authenticated, ni
-- service_role (el bridge la llama con la clave del rol). Aditiva: no cambia ninguna funcion,
-- tabla ni permiso existente, y no cambia lo que se manda. Correrla dos veces
-- aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida (la instancia, o p_only_open o p_limit fuera
-- de rango).

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public._flow_store_instance(jsonb)') is null
       or to_regclass('public.flow_change_proposals') is null
       or to_regclass('public.flow_change_events') is null then
        raise exception using errcode = '55000', message = 'flow_change_list_prerequisite_missing';
    end if;
    if to_regprocedure('public.list_flow_changes_v1(jsonb,boolean,integer)') is not null then
        raise exception using errcode = '55000', message = 'flow_change_list_already_applied';
    end if;
end;
$already$;

create function public.list_flow_changes_v1(p_instance jsonb, p_only_open boolean, p_limit integer)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_open integer;
    v_proposals jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_only_open is null or p_limit is null or p_limit < 1 or p_limit > 50 then
        raise exception using errcode = '22023', message = 'flow_change_list_parameters_invalid';
    end if;
    -- La misma cuenta que el tope de save_flow_change_draft_v1.
    select count(*)::integer into v_open from public.flow_change_proposals p
     where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
       and p.requested_by_kind = 'automation_expert'
       and (p.status = 'borrador'
            or (p.status in ('validada', 'aprobada') and clock_timestamp() < p.expires_at));
    select coalesce(jsonb_agg(row_data order by created_at desc, proposal_id), '[]'::jsonb)
      into v_proposals
      from (
        select p.created_at, p.id as proposal_id, jsonb_build_object(
                   'proposal_id', p.id,
                   'status', p.status,
                   'effective_status', case
                       when p.status in ('validada', 'aprobada') and clock_timestamp() >= p.expires_at
                       then 'vencida' else p.status end,
                   'revision', p.revision,
                   'requested_by_kind', p.requested_by_kind,
                   'purpose', case when p.requested_by_kind = 'automation_expert' then p.purpose end,
                   'flows', (select coalesce(jsonb_agg(jsonb_build_object(
                                 'producto', item.value ->> 'producto', 'flujo', item.value ->> 'flujo',
                                 'tipo', item.value ->> 'tipo') order by item.position), '[]'::jsonb)
                               from jsonb_array_elements(p.items) with ordinality as item(value, position)),
                   'created_at', p.created_at,
                   'updated_at', p.updated_at,
                   'validated_at', p.validated_at,
                   'expires_at', p.expires_at,
                   'approved_at', p.approved_at,
                   'approved_by_kind', p.approved_by_kind,
                   'published_at', p.published_at,
                   'closed_at', p.closed_at,
                   'closed_by_kind', p.closed_by_kind,
                   'last_event', (
                       select jsonb_build_object('event_type', e.event_type, 'actor_kind', e.actor_kind,
                                                 'reason_code', e.reason_code, 'created_at', e.created_at)
                         from public.flow_change_events e
                        where e.proposal_id = p.id and e.event_type <> 'flow_control_changed'
                        order by e.created_at desc, e.id desc
                        limit 1)) as row_data
          from public.flow_change_proposals p
         where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
           and (not p_only_open
                or p.status = 'borrador'
                or (p.status in ('validada', 'aprobada') and clock_timestamp() < p.expires_at))
         order by p.created_at desc, p.id
         limit p_limit) listed;
    return jsonb_build_object('proposals', v_proposals, 'open', v_open, 'open_limit', 10,
                              'database_at', clock_timestamp());
end;
$function$;

-- Los permisos: solo el rol del agente. Los privilegios por defecto de
-- Supabase le dan EXECUTE a anon, authenticated y service_role sobre toda
-- funcion nueva: se revocan explicitamente.
do $acl$
declare
    v_role text;
begin
    revoke all on function public.list_flow_changes_v1(jsonb, boolean, integer) from public;
    for v_role in
        select r.rolname from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'service_role')
         order by r.rolname
    loop
        execute format('revoke all on function public.list_flow_changes_v1(jsonb, boolean, integer) from %I',
                       v_role);
    end loop;
    -- El rol de la T4 lo crea 20261021000100; una base sin el no lo nombra.
    if to_regrole('automation_expert_change') is not null then
        grant execute on function public.list_flow_changes_v1(jsonb, boolean, integer) to automation_expert_change;
    end if;
end;
$acl$;

commit;
