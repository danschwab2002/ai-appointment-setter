-- automation-expert-change-v1: el rol de base del Automation Expert para
-- proponer cambios de flujo, sus cuatro envoltorios, y aprobar con la
-- simulacion de la vista previa (la T4 de la Etapa 1).
-- Contratos: docs/contracts/flow-change-cycle-v1.md (las rutas y los permisos)
-- y docs/contracts/flow-engine-v1.md (la simulacion, seccion 4). Diseno: el
-- almacen, §10.1.
--
-- 1. El rol automation_expert_change: NOLOGIN NOINHERIT, concedido solo a
--    authenticator, para que PostgREST lo asuma con el JWT del bridge. Un rol
--    es del cluster entero y la CI aplica la cadena en varias bases del mismo
--    servidor: el bloque es idempotente y falla cerrado si el rol tiene un
--    atributo de mas o si alguien que no es authenticator lo hereda o lo
--    asume (V2#8). La concesion implicita a quien lo crea (Postgres 16+:
--    admin_option, sin inherit ni set) pasa. Medido con la imagen de
--    Supabase de ATT1 (17.6.1.167) el 2026-10-21: dos pasadas, otra base del
--    mismo cluster, y con anon como miembro falla. Sin alter role ... set
--    statement_timeout (V3#12).
-- 2. Cuatro envoltorios SECURITY DEFINER con search_path fijo, sobre los
--    helpers del almacen (20261016000100) con automation_expert como clase:
--    leer el almacen, guardar o rehacer un borrador, registrar la validacion
--    (que corre el bridge) y releer una propuesta. Solo los ejecuta el rol. Ni
--    aprobar, ni publicar, ni pausar, ni descartar: la pausa y el descarte del
--    agente no se conceden hasta que la base pueda verificar a quien lo pide
--    (V2#5); requested_by lo manda el bridge y queda como declarado.
-- 3. El tope del agente (V2#6): 10 propuestas abiertas por instancia (con un
--    lock por instancia), 100 revisiones y 100 validaciones por propuesta. El
--    operador no tiene tope.
-- 4. _flow_change_approve registra la simulacion que quedo congelada en la
--    vista previa al validar (validation.preview.simulacion): built con el
--    sha256 del resumen, failed con su codigo, o not_built. La firma no cambia
--    y create or replace conserva sus permisos.
--
-- La instancia la elige quien llama (p_instance): el JWT del rol no la nombra
-- (V2#9). Cada instancia tiene su propia base (ATT1: att1-db), y esa es la
-- separacion; si dos instancias compartieran base, cada envoltorio tendria que
-- comparar la instancia con un claim del JWT.
--
-- No suma tablas ni permisos sobre tablas: el rol no lee ni escribe ninguna
-- tabla, solo ejecuta sus cuatro funciones.

begin;

do $role$
begin
    if to_regrole('automation_expert_change') is null then
        create role automation_expert_change nologin noinherit;
    elsif exists (
        select 1 from pg_roles r
        where r.rolname = 'automation_expert_change'
          and (r.rolcanlogin or r.rolsuper or r.rolbypassrls or r.rolcreaterole
               or r.rolcreatedb or r.rolreplication or r.rolinherit)
    ) or exists (
        select 1 from pg_auth_members m
        where m.member = 'automation_expert_change'::regrole
    ) or exists (
        -- Nadie mas que authenticator hereda o asume el rol (V2#8).
        select 1 from pg_auth_members m
        where m.roleid = 'automation_expert_change'::regrole
          and m.member is distinct from to_regrole('authenticator')
          and (m.inherit_option or m.set_option)
    ) then
        raise exception using errcode = '55000', message = 'automation_expert_change_role_unsafe';
    end if;
    if to_regrole('authenticator') is not null
       and not pg_has_role('authenticator', 'automation_expert_change', 'MEMBER') then
        grant automation_expert_change to authenticator;
    end if;
end;
$role$;

grant usage on schema public to automation_expert_change;

-- 1. Leer el almacen: la foto que llena FlowWorld en el bridge, con todas las
-- versiones de cada flujo (el validador necesita la activa, la mas vieja con
-- un horario objeto y el destino de un volver), y los productos de la
-- instancia. La misma forma que lee el comando del operador.
create or replace function public.read_flow_store_v1(p_instance jsonb)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_count integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    select count(*) into v_count from public.flow_controls c
     where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref;
    if v_count > 50 then
        raise exception using errcode = '54000', message = 'flow_store_response_too_large';
    end if;
    return jsonb_build_object(
        'database_at', clock_timestamp(),
        'products', coalesce((
            select jsonb_agg(distinct b.funnel_ref order by b.funnel_ref)
              from public.commercial_ally_runtime_bindings b
             where b.tenant_ref = v_instance.o_tenant_ref and b.ally_ref = v_instance.o_ally_ref
               and b.status = 'active'), '[]'::jsonb),
        'flows', coalesce((
            select jsonb_agg(jsonb_build_object(
                'flow_id', c.flow_id, 'product_ref', c.product_ref, 'flow_key', c.flow_key,
                'state', c.state, 'active_version', c.active_version, 'generation', c.generation,
                'changed_at', c.changed_at, 'changed_by_kind', c.changed_by_kind,
                'versions', coalesce((
                    select jsonb_object_agg(d.version::text, d.definition)
                      from public.flow_definitions d where d.flow_id = c.flow_id), '{}'::jsonb))
                order by c.product_ref, c.flow_key)
              from public.flow_controls c
             where c.tenant_ref = v_instance.o_tenant_ref and c.ally_ref = v_instance.o_ally_ref),
            '[]'::jsonb));
end;
$function$;

-- 2. Guardar o rehacer un borrador del agente, con su tope.
create or replace function public.save_flow_change_draft_v1(
    p_instance jsonb, p_proposal_id uuid, p_expected_revision integer, p_items jsonb, p_base jsonb,
    p_purpose text, p_requested_by_ref text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_open integer;
    v_revision integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    if p_proposal_id is null then
        -- Un lock por instancia: dos borradores a la vez no pasan el tope juntos.
        -- El lock_timeout va con set_config local y no en la firma: los
        -- helpers que esperan locks ya traen el suyo.
        perform set_config('lock_timeout', '5s', true);
        perform pg_advisory_xact_lock(hashtextextended(
            'automation_expert_change:open:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
        select count(*) into v_open from public.flow_change_proposals p
         where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
           and p.requested_by_kind = 'automation_expert'
           and (p.status = 'borrador'
                or (p.status in ('validada', 'aprobada') and clock_timestamp() < p.expires_at));
        if v_open >= 10 then
            return jsonb_build_object('ok', false, 'reason_code', 'flow_change_open_limit', 'open', v_open);
        end if;
    else
        select p.revision into v_revision from public.flow_change_proposals p
         where p.id = p_proposal_id and p.tenant_ref = v_instance.o_tenant_ref
           and p.ally_ref = v_instance.o_ally_ref and p.requested_by_kind = 'automation_expert';
        if v_revision >= 100 then
            return jsonb_build_object('ok', false, 'reason_code', 'flow_change_revision_limit');
        end if;
    end if;
    return public._flow_change_save_draft(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_proposal_id,
        p_expected_revision, p_items, p_base, p_purpose, 'automation_expert', p_requested_by_ref);
end;
$function$;

-- 3. Registrar la validacion que corrio el bridge, con su tope.
create or replace function public.record_flow_change_validation_v1(
    p_instance jsonb, p_proposal_id uuid, p_revision integer, p_validated_base jsonb,
    p_validator jsonb, p_result jsonb, p_preview jsonb, p_requested_by_ref text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_validations integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    select count(*) into v_validations from public.flow_change_events e
     where e.proposal_id = p_proposal_id and e.tenant_ref = v_instance.o_tenant_ref
       and e.ally_ref = v_instance.o_ally_ref
       and e.event_type in ('flow_change_validated', 'flow_change_validation_blocked',
                            'flow_change_validation_rejected');
    if v_validations >= 100 then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_change_validation_limit');
    end if;
    return public._flow_change_record_validation(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_proposal_id,
        p_revision, p_validated_base, p_validator, p_result, p_preview, 'automation_expert', p_requested_by_ref);
end;
$function$;

-- 4. Releer una propuesta, para mostrarla sin validarla otra vez. Con claves
-- nombradas, el estado efectivo y los ultimos 50 eventos (sin su data).
create or replace function public.get_flow_change_proposal_v1(p_instance jsonb, p_proposal_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_proposal public.flow_change_proposals%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
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
        'revision', v_proposal.revision,
        'items', v_proposal.items,
        'draft_base', v_proposal.draft_base,
        'base', v_proposal.base,
        'purpose', v_proposal.purpose,
        'requested_by_kind', v_proposal.requested_by_kind,
        'requested_by_ref', v_proposal.requested_by_ref,
        'validation', v_proposal.validation,
        'fingerprint', v_proposal.fingerprint,
        'validated_at', v_proposal.validated_at,
        'expires_at', v_proposal.expires_at,
        'approved_at', v_proposal.approved_at,
        'approved_by_kind', v_proposal.approved_by_kind,
        'approved_by_ref', v_proposal.approved_by_ref,
        'published_at', v_proposal.published_at,
        'closed_at', v_proposal.closed_at,
        'created_at', v_proposal.created_at,
        'events', coalesce((
            select jsonb_agg(jsonb_build_object('event_type', e.event_type, 'actor_kind', e.actor_kind,
                                                'reason_code', e.reason_code, 'created_at', e.created_at)
                             order by e.created_at desc, e.id)
              from (select * from public.flow_change_events e
                     where e.proposal_id = v_proposal.id
                     order by e.created_at desc, e.id limit 50) e), '[]'::jsonb));
end;
$function$;

-- Aprobar registra la simulacion que quedo congelada en la vista previa al
-- validar (contrato flow-engine-v1, seccion 4, paso 5). Igual que en
-- 20261016000100 salvo el ultimo evento.
create or replace function public._flow_change_approve(
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
    v_simulation jsonb;
    v_frozen jsonb;
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
    -- automation_expert_change_approve_simulation(20261021000100): la
    -- simulacion congelada al validar. Solo se cree con su forma exacta; lo
    -- demas es not_built, que es lo que el comando aprueba con --sin-simulacion.
    v_frozen := v_proposal.validation #> '{preview,simulacion}';
    v_simulation := case
        when jsonb_typeof(v_frozen) = 'object' and v_frozen ->> 'status' = 'built'
             and coalesce(v_frozen ->> 'summary_sha256' ~ '^[0-9a-f]{64}$', false)
            then jsonb_build_object('simulation', 'built', 'summary_sha256', v_frozen ->> 'summary_sha256')
        when jsonb_typeof(v_frozen) = 'object' and v_frozen ->> 'status' = 'failed'
             and coalesce(v_frozen ->> 'reason_code' ~ '^[a-z][a-z0-9_:]{0,79}$', false)
            then jsonb_build_object('simulation', 'failed', 'simulation_reason_code', v_frozen ->> 'reason_code')
        else jsonb_build_object('simulation', 'not_built')
    end;
    perform public._flow_change_event(v_proposal, 'flow_change_approved', p_approver_kind, p_approver_ref,
        'flow_change_approved', jsonb_build_object('fingerprint', v_proposal.fingerprint) || v_simulation);
    return jsonb_build_object('ok', true, 'replayed', false, 'status', 'aprobada',
        'approved_by_kind', p_approver_kind, 'approved_by_ref', p_approver_ref) || v_simulation;
end;
$function$;

-- Los permisos: solo el rol del agente ejecuta sus cuatro funciones. Los
-- privilegios por defecto de Supabase le dan EXECUTE a anon, authenticated y
-- service_role sobre toda funcion nueva: se revocan explicitamente.
do $acl$
declare
    v_function regprocedure;
    v_role text;
begin
    foreach v_function in array array[
        'public.read_flow_store_v1(jsonb)',
        'public.save_flow_change_draft_v1(jsonb,uuid,integer,jsonb,jsonb,text,text)',
        'public.record_flow_change_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,jsonb,text)',
        'public.get_flow_change_proposal_v1(jsonb,uuid)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role')
             order by r.rolname
        loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
        execute format('grant execute on function %s to automation_expert_change', v_function);
    end loop;
end;
$acl$;

commit;
