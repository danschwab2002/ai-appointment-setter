-- flow-change-slack-approver-v1: el rol de base de la pagina de aprobacion
-- (A′ PR 2), sus envoltorios con el aprobador slack, las sesiones del «Sign in
-- with Slack» y la cohorte con el actor slack.
-- Contrato: docs/contracts/flow-change-cycle-v1.md, «El sí por un link en el
-- chat del Automation Expert (1.9.0)», «Lo que pide a la base». Molde:
-- 20261021000100 (el rol del Automation Expert).
--
-- 1. El rol flow_change_approver: NOLOGIN NOINHERIT, concedido solo a
--    authenticator, con el mismo bloque idempotente y fail-closed que el rol
--    del Automation Expert. El bridge lo asume con FLOW_CHANGE_APPROVER_DB_KEY.
-- 2. Dos tablas con solo hashes: flow_approval_oidc_states (el state y el
--    nonce del OIDC, 10 minutos, un solo uso, y la sesion que salio de cada
--    uno) y flow_approval_sessions (el sha256 de la cookie, el workspace, el ID
--    de Slack verificado, a lo sumo 8 horas, la salida). Ningun token de Slack,
--    ni el code, ni el id_token. Sin permisos para nadie de la API.
-- 3. Cinco funciones de la sesion: crear el state, consumirlo (devuelve el
--    hash del nonce, que el bridge compara con el del id_token), crear la
--    sesion con un state consumido, validarla en cada pedido y revocarla.
-- 4. Los envoltorios del aprobador, todos con la sesion: el actor slack sale
--    de la fila de la sesion viva, nunca de un parametro. Releer una
--    propuesta, leer el almacen, leer las otras propuestas abiertas de los
--    mismos flujos (sin su para que), aprobar, publicar, pausar, descartar y
--    sumar un telefono a la cohorte.
-- 5. La cohorte con el actor slack: flow_cohort_members suma added_by_kind
--    (las filas de hoy quedan operador) y operator_flow_cohort_v1 pasa a ser un
--    envoltorio de _flow_cohort_change, la misma funcion que usa la pagina. La
--    respuesta y el evento no cambian para el operador.
--
-- La lista de aprobadores no vive en la base (decision 7 del 2026-10-27): la
-- verifica el bridge en cada pedido, antes de crear la sesion y antes de cada
-- llamada. La base exige una sesion viva de la instancia.
--
-- Nada cambia sin el rol ni el flag: ninguna funcion de hoy cambia de firma ni
-- de permisos, y sin FLOW_APPROVAL_ENABLED el bridge no llama a ninguna de
-- estas. Correrla dos veces aborta con 55000 sin cambiar nada.
--
-- Errores: 22023 entrada invalida; 28000 sesion invalida
-- (flow_approval_session_invalid); P0002 no existe; 55000 un estado que no lo
-- permite.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $already$
begin
    if to_regprocedure('public.operator_flow_cohort_v1(jsonb,text,text,text,text,text,text)') is null
       or to_regprocedure('public.get_flow_change_proposal_v1(jsonb,uuid)') is null then
        raise exception using errcode = '55000', message = 'flow_change_store_missing';
    end if;
    if to_regclass('public.flow_approval_sessions') is not null then
        raise exception using errcode = '55000', message = 'flow_change_slack_approver_already_applied';
    end if;
end;
$already$;

-- ===========================================================================
-- 1. El rol.
-- ===========================================================================

do $role$
begin
    if to_regrole('flow_change_approver') is null then
        create role flow_change_approver nologin noinherit;
    elsif exists (
        select 1 from pg_roles r
        where r.rolname = 'flow_change_approver'
          and (r.rolcanlogin or r.rolsuper or r.rolbypassrls or r.rolcreaterole
               or r.rolcreatedb or r.rolreplication or r.rolinherit)
    ) or exists (
        select 1 from pg_auth_members m
        where m.member = 'flow_change_approver'::regrole
    ) or exists (
        -- Nadie mas que authenticator hereda o asume el rol.
        select 1 from pg_auth_members m
        where m.roleid = 'flow_change_approver'::regrole
          and m.member is distinct from to_regrole('authenticator')
          and (m.inherit_option or m.set_option)
    ) then
        raise exception using errcode = '55000', message = 'flow_change_approver_role_unsafe';
    end if;
    if to_regrole('authenticator') is not null
       and not pg_has_role('authenticator', 'flow_change_approver', 'MEMBER') then
        grant flow_change_approver to authenticator;
    end if;
end;
$role$;

grant usage on schema public to flow_change_approver;

-- ===========================================================================
-- 2. Las tablas de la sesion. Solo hashes.
-- ===========================================================================

create table public.flow_approval_oidc_states (
    state_hash text primary key check (state_hash ~ '^[0-9a-f]{64}$'),
    tenant_ref text not null,
    ally_ref text not null,
    nonce_hash text not null check (nonce_hash ~ '^[0-9a-f]{64}$'),
    -- A donde vuelve despues de entrar: la lista o una propuesta.
    return_path text not null check (return_path ~
        '^/flow-approval/(p/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?$'),
    created_at timestamptz not null default clock_timestamp(),
    expires_at timestamptz not null,
    consumed_at timestamptz,
    -- La sesion que salio de este state: una sola.
    session_hash text unique check (session_hash ~ '^[0-9a-f]{64}$'),
    check (expires_at > created_at and expires_at <= created_at + interval '10 minutes'),
    check (session_hash is null or consumed_at is not null)
);

create table public.flow_approval_sessions (
    session_hash text primary key check (session_hash ~ '^[0-9a-f]{64}$'),
    tenant_ref text not null,
    ally_ref text not null,
    slack_team_id text not null check (slack_team_id ~ '^T[A-Z0-9]{8,20}$'),
    slack_user_id text not null check (public._flow_store_actor_is_valid('slack', slack_user_id)),
    issued_at timestamptz not null default clock_timestamp(),
    expires_at timestamptz not null,
    revoked_at timestamptz,
    last_seen_at timestamptz not null default clock_timestamp(),
    check (expires_at > issued_at and expires_at <= issued_at + interval '8 hours'),
    check (revoked_at is null or revoked_at >= issued_at)
);

create index flow_approval_oidc_states_pending_idx
    on public.flow_approval_oidc_states (tenant_ref, ally_ref, expires_at) where consumed_at is null;
create index flow_approval_sessions_user_idx
    on public.flow_approval_sessions (tenant_ref, ally_ref, slack_user_id, expires_at);

-- ===========================================================================
-- 3. La sesion.
-- ===========================================================================

-- El actor de una sesion viva de la instancia, y la marca de que se uso.
-- Interna: la llaman los envoltorios del aprobador.
create function public._flow_approval_session_actor(
    p_tenant_ref text, p_ally_ref text, p_session_hash text)
returns text
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_session public.flow_approval_sessions%rowtype;
begin
    if not coalesce(p_session_hash ~ '^[0-9a-f]{64}$', false) then
        raise exception using errcode = '28000', message = 'flow_approval_session_invalid';
    end if;
    select * into v_session from public.flow_approval_sessions s
     where s.session_hash = p_session_hash and s.tenant_ref = p_tenant_ref and s.ally_ref = p_ally_ref;
    if not found or v_session.revoked_at is not null or v_session.expires_at <= clock_timestamp() then
        raise exception using errcode = '28000', message = 'flow_approval_session_invalid';
    end if;
    update public.flow_approval_sessions set last_seen_at = clock_timestamp()
     where session_hash = p_session_hash;
    return v_session.slack_user_id;
end;
$function$;

-- Crea el state y el nonce del OIDC (sus hashes), por 10 minutos como maximo.
-- Hasta 200 pendientes por instancia: la ruta de entrar no tiene sesion y un
-- visitante no llena la tabla. Los vencidos de mas de un dia se borran.
create function public.begin_flow_approval_oidc_v1(
    p_instance jsonb, p_state_hash text, p_nonce_hash text, p_return_path text, p_expires_at timestamptz)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_pending integer;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not coalesce(p_state_hash ~ '^[0-9a-f]{64}$' and p_nonce_hash ~ '^[0-9a-f]{64}$'
                    and p_state_hash <> p_nonce_hash, false) then
        raise exception using errcode = '22023', message = 'flow_approval_oidc_hash_invalid';
    end if;
    if not coalesce(p_return_path ~
            '^/flow-approval/(p/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?$', false) then
        raise exception using errcode = '22023', message = 'flow_approval_return_path_invalid';
    end if;
    if p_expires_at is null or p_expires_at <= clock_timestamp()
       or p_expires_at > clock_timestamp() + interval '10 minutes' then
        raise exception using errcode = '22023', message = 'flow_approval_oidc_expiry_invalid';
    end if;
    delete from public.flow_approval_oidc_states s
     where s.tenant_ref = v_instance.o_tenant_ref and s.ally_ref = v_instance.o_ally_ref
       and s.expires_at < clock_timestamp() - interval '1 day' and s.session_hash is null;
    perform pg_advisory_xact_lock(hashtextextended(
        'flow_change_approver:oidc:' || v_instance.o_tenant_ref || ':' || v_instance.o_ally_ref, 0));
    select count(*) into v_pending from public.flow_approval_oidc_states s
     where s.tenant_ref = v_instance.o_tenant_ref and s.ally_ref = v_instance.o_ally_ref
       and s.consumed_at is null and s.expires_at > clock_timestamp();
    if v_pending >= 200 then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_approval_oidc_busy');
    end if;
    insert into public.flow_approval_oidc_states (state_hash, tenant_ref, ally_ref, nonce_hash, return_path,
        expires_at)
    values (p_state_hash, v_instance.o_tenant_ref, v_instance.o_ally_ref, p_nonce_hash, p_return_path,
        p_expires_at);
    return jsonb_build_object('ok', true);
end;
$function$;

-- Consume el state una sola vez: devuelve el hash del nonce (el bridge lo
-- compara con el del id_token) y a donde volver.
create function public.consume_flow_approval_oidc_v1(p_instance jsonb, p_state_hash text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_state public.flow_approval_oidc_states%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    select * into v_state from public.flow_approval_oidc_states s
     where s.state_hash = p_state_hash and s.tenant_ref = v_instance.o_tenant_ref
       and s.ally_ref = v_instance.o_ally_ref
     for update;
    if not found or v_state.consumed_at is not null or v_state.expires_at <= clock_timestamp() then
        raise exception using errcode = '28000', message = 'flow_approval_oidc_state_invalid';
    end if;
    update public.flow_approval_oidc_states set consumed_at = clock_timestamp()
     where state_hash = p_state_hash;
    return jsonb_build_object('ok', true, 'nonce_hash', v_state.nonce_hash, 'return_path', v_state.return_path);
end;
$function$;

-- Crea la sesion de quien entro, con el state que ya se consumio en los
-- ultimos 10 minutos y que todavia no dio una sesion. El bridge la llama
-- despues de validar el id_token y de mirar la lista de aprobadores.
create function public.create_flow_approval_session_v1(
    p_instance jsonb, p_state_hash text, p_session_hash text, p_slack_team_id text, p_slack_user_id text,
    p_expires_at timestamptz)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_state public.flow_approval_oidc_states%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    if not coalesce(p_session_hash ~ '^[0-9a-f]{64}$' and p_session_hash <> p_state_hash, false) then
        raise exception using errcode = '22023', message = 'flow_approval_session_hash_invalid';
    end if;
    if not coalesce(p_slack_team_id ~ '^T[A-Z0-9]{8,20}$'
                    and public._flow_store_actor_is_valid('slack', p_slack_user_id), false) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if p_expires_at is null or p_expires_at <= clock_timestamp()
       or p_expires_at > clock_timestamp() + interval '8 hours' then
        raise exception using errcode = '22023', message = 'flow_approval_session_expiry_invalid';
    end if;
    select * into v_state from public.flow_approval_oidc_states s
     where s.state_hash = p_state_hash and s.tenant_ref = v_instance.o_tenant_ref
       and s.ally_ref = v_instance.o_ally_ref
     for update;
    if not found or v_state.consumed_at is null or v_state.session_hash is not null
       or v_state.consumed_at <= clock_timestamp() - interval '10 minutes' then
        raise exception using errcode = '28000', message = 'flow_approval_oidc_state_invalid';
    end if;
    insert into public.flow_approval_sessions (session_hash, tenant_ref, ally_ref, slack_team_id, slack_user_id,
        expires_at)
    values (p_session_hash, v_instance.o_tenant_ref, v_instance.o_ally_ref, p_slack_team_id, p_slack_user_id,
        p_expires_at);
    update public.flow_approval_oidc_states set session_hash = p_session_hash where state_hash = p_state_hash;
    return jsonb_build_object('ok', true, 'return_path', v_state.return_path, 'expires_at', p_expires_at);
end;
$function$;

-- Valida la sesion en cada pedido, GET o POST. Devuelve quien es; nada mas.
create function public.validate_flow_approval_session_v1(p_instance jsonb, p_session_hash text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_actor text;
    v_session public.flow_approval_sessions%rowtype;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    select * into v_session from public.flow_approval_sessions s where s.session_hash = p_session_hash;
    return jsonb_build_object('ok', true, 'slack_user_id', v_actor, 'slack_team_id', v_session.slack_team_id,
        'expires_at', v_session.expires_at);
end;
$function$;

-- Salir: revoca la sesion. Una sesion ya revocada, vencida o de otra
-- instancia contesta igual, sin decir cual era.
create function public.revoke_flow_approval_session_v1(p_instance jsonb, p_session_hash text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
begin
    v_instance := public._flow_store_instance(p_instance);
    update public.flow_approval_sessions s set revoked_at = clock_timestamp()
     where s.session_hash = p_session_hash and s.tenant_ref = v_instance.o_tenant_ref
       and s.ally_ref = v_instance.o_ally_ref and s.revoked_at is null
       and s.expires_at > clock_timestamp();
    return jsonb_build_object('ok', true);
end;
$function$;

-- ===========================================================================
-- 4. La cohorte con el actor slack.
-- ===========================================================================

-- Quien cargo a cada miembro: la clase y su forma. Las filas de hoy son del
-- operador.
alter table public.flow_cohort_members
    add column added_by_kind text not null default 'operador' check (added_by_kind in ('operador', 'slack'));
alter table public.flow_cohort_members
    drop constraint flow_cohort_members_added_by_ref_check,
    add constraint flow_cohort_members_added_by check (public._flow_store_actor_is_valid(added_by_kind, added_by_ref));

-- La ruta nativa de un flujo nuevo la firma quien aprobo la propuesta
-- (_flow_engine_open_native_flow, 20261024000200): con la pagina es un ID de
-- Slack. Sin esto, publicar un item nuevo aprobado por slack da 23514
-- (medido en validate_flow_change_slack_approver.mjs antes del ajuste). Las
-- dos formas no se pisan: el operador es en minuscula, Slack empieza con U o W.
alter table public.flow_engine_routes
    drop constraint flow_engine_routes_changed_by_ref_check,
    add constraint flow_engine_routes_changed_by check (
        public._flow_store_actor_is_valid('operador', changed_by_ref)
        or public._flow_store_actor_is_valid('slack', changed_by_ref));

-- Y su evento (record_flow_engine_route_change, 20261021000200) dice la clase
-- que corresponde: hasta hoy, siempre operador. Reemplazo por ancla sobre la
-- definicion vigente, contado: si el ancla no esta una vez, aborta.
do $route_event$
declare
    c_signature constant text := 'public.record_flow_engine_route_change()';
    c_anchor constant text := E'new.flow_id, v_event, ''operador'', new.changed_by_ref,';
    c_marker constant text := 'flow_change_slack_approver: la clase del actor';
    v_definition text;
    v_acl_before text;
begin
    v_definition := pg_get_functiondef(c_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, c_anchor, '')) <> length(c_anchor)
       or position(c_marker in v_definition) > 0 then
        raise exception using errcode = '55000', message = 'unexpected_flow_route_event_definition';
    end if;
    select coalesce(p.proacl::text, '') into v_acl_before from pg_proc p where p.oid = c_signature::regprocedure;
    execute replace(v_definition, c_anchor,
        E'new.flow_id, v_event,\n'
        || E'        -- flow_change_slack_approver: la clase del actor (un ID de Slack es slack)\n'
        || E'        case when public._flow_store_actor_is_valid(''slack'', new.changed_by_ref) then ''slack''\n'
        || E'             else ''operador'' end, new.changed_by_ref,');
    if position(c_marker in pg_get_functiondef(c_signature::regprocedure)) = 0
       or (select coalesce(p.proacl::text, '') from pg_proc p where p.oid = c_signature::regprocedure)
          is distinct from v_acl_before then
        raise exception using errcode = '55000', message = 'flow_route_event_not_replaced';
    end if;
end;
$route_event$;

-- La de operator_flow_cohort_v1 (20261024000600), con la clase del actor como
-- parametro: operador (la terminal) o slack (la pagina, solo agregar). Mismos
-- motivos, mismo tope, y ni la respuesta ni el evento traen el telefono o el
-- contacto.
create function public._flow_cohort_change(
    p_tenant_ref text, p_ally_ref text, p_product_ref text, p_flow_key text, p_action text, p_phone text,
    p_actor_kind text, p_actor_ref text, p_reason text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    c_max_members constant integer := 5;
    v_control public.flow_controls%rowtype;
    v_canonical text;
    v_contacts uuid[];
    v_changed boolean := false;
    v_members integer;
begin
    if not coalesce(p_action in ('agregar', 'sacar'), false) then
        raise exception using errcode = '22023', message = 'flow_cohort_action_invalid';
    end if;
    -- La pagina solo agrega: sacar sigue en la terminal (contrato 1.9.0).
    if not coalesce((p_actor_kind = 'operador' or (p_actor_kind = 'slack' and p_action = 'agregar'))
                    and public._flow_store_actor_is_valid(p_actor_kind, p_actor_ref), false) then
        raise exception using errcode = '22023', message = 'flow_store_actor_invalid';
    end if;
    if not public._flow_store_free_text_is_valid(p_reason) then
        raise exception using errcode = '22023', message = 'flow_store_text_invalid';
    end if;
    select * into v_control from public.flow_controls c
     where c.tenant_ref = p_tenant_ref and c.ally_ref = p_ally_ref
       and c.product_ref = p_product_ref and c.flow_key = p_flow_key
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'flow_not_found';
    end if;
    if not exists (select 1 from public.flow_engine_routes r
                    where r.flow_id = v_control.flow_id and r.legacy_kind is null) then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_cohort_needs_native_flow');
    end if;
    v_canonical := public._whatsapp_phone_canonical(p_phone);
    if v_canonical is null or v_canonical !~ '^[0-9]{8,15}$' then
        raise exception using errcode = '22023', message = 'flow_cohort_phone_invalid';
    end if;
    select array_agg(distinct contact_row.id) into v_contacts
      from public.contacts contact_row
     where public._whatsapp_phone_canonical(contact_row.phone) = v_canonical;
    if coalesce(cardinality(v_contacts), 0) = 0 then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_cohort_contact_not_found');
    end if;
    if cardinality(v_contacts) > 1 then
        return jsonb_build_object('ok', false, 'reason_code', 'flow_cohort_contact_ambiguous');
    end if;
    if p_action = 'agregar' then
        if not exists (select 1 from public.flow_cohort_members member
                        where member.flow_id = v_control.flow_id and member.contact_id = v_contacts[1]) then
            if (select count(*) from public.flow_cohort_members member
                 where member.flow_id = v_control.flow_id) >= c_max_members then
                return jsonb_build_object('ok', false, 'reason_code', 'flow_cohort_full');
            end if;
            insert into public.flow_cohort_members (flow_id, contact_id, added_by_kind, added_by_ref)
            values (v_control.flow_id, v_contacts[1], p_actor_kind, p_actor_ref);
            v_changed := true;
        end if;
    else
        delete from public.flow_cohort_members member
         where member.flow_id = v_control.flow_id and member.contact_id = v_contacts[1];
        v_changed := found;
        if not v_changed then
            return jsonb_build_object('ok', false, 'reason_code', 'flow_cohort_member_not_found');
        end if;
    end if;
    select count(*)::integer into v_members from public.flow_cohort_members member
     where member.flow_id = v_control.flow_id;
    if v_changed then
        insert into public.flow_change_events (tenant_ref, ally_ref, flow_id, event_type, actor_kind,
            actor_ref, reason_code, data)
        values (v_control.tenant_ref, v_control.ally_ref, v_control.flow_id, 'flow_cohort_changed',
            p_actor_kind, p_actor_ref, 'flow_cohort_' || p_action,
            jsonb_build_object('action', p_action, 'members', v_members,
                'reason_sha256', encode(sha256(convert_to(p_reason, 'UTF8')), 'hex')));
    end if;
    return jsonb_build_object('ok', true, 'action', p_action, 'changed', v_changed, 'members', v_members,
        'max_members', c_max_members);
end;
$function$;

-- La del operador pasa a ser un envoltorio: misma firma, mismos permisos (create
-- or replace los conserva), la misma respuesta.
create or replace function public.operator_flow_cohort_v1(
    p_instance jsonb, p_product_ref text, p_flow_key text, p_action text, p_phone text,
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
    -- flow_change_slack_approver(20261028000100): la misma funcion que la pagina.
    return public._flow_cohort_change(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_product_ref, p_flow_key,
        p_action, p_phone, 'operador', p_actor_name, p_reason);
end;
$function$;

-- ===========================================================================
-- 5. Los envoltorios del aprobador. El actor sale de la sesion.
-- ===========================================================================

-- Releer una propuesta con su vista previa (la de get_flow_change_proposal_v1).
create function public.approver_get_flow_change_proposal_v1(
    p_instance jsonb, p_session_hash text, p_proposal_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    return public.get_flow_change_proposal_v1(p_instance, p_proposal_id);
end;
$function$;

-- Leer el almacen de la instancia (la de read_flow_store_v1).
create function public.approver_read_flow_store_v1(p_instance jsonb, p_session_hash text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    return public.read_flow_store_v1(p_instance);
end;
$function$;

-- Las propuestas abiertas, sin su para que (decision 5 del 2026-10-27).
-- Con p_proposal_id: las otras abiertas (borrador, o validada o aprobada sin
-- vencer) que tocan alguno de sus flujos, por producto y flujo (asi tambien un
-- item nuevo). Sin p_proposal_id: las validadas o aprobadas sin vencer de la
-- instancia, para la lista de la pagina. Hasta 50, las mas nuevas primero.
create function public.approver_read_open_flow_changes_v1(
    p_instance jsonb, p_session_hash text, p_proposal_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_flows jsonb;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    if p_proposal_id is not null then
        if not exists (select 1 from public.flow_change_proposals p
                        where p.id = p_proposal_id and p.tenant_ref = v_instance.o_tenant_ref
                          and p.ally_ref = v_instance.o_ally_ref) then
            raise exception using errcode = 'P0002', message = 'flow_change_not_found';
        end if;
        select coalesce(jsonb_agg(distinct jsonb_build_object('producto', item ->> 'producto',
                                                              'flujo', item ->> 'flujo')), '[]'::jsonb)
          into v_flows
          from public.flow_change_proposals p, jsonb_array_elements(p.items) item
         where p.id = p_proposal_id and p.tenant_ref = v_instance.o_tenant_ref
           and p.ally_ref = v_instance.o_ally_ref;
    end if;
    return jsonb_build_object('proposals', coalesce((
        select jsonb_agg(row_data order by created_at desc, proposal_id)
          from (
            select p.created_at, p.id as proposal_id, jsonb_build_object(
                       'proposal_id', p.id,
                       'status', p.status,
                       'validated_at', p.validated_at,
                       'expires_at', p.expires_at,
                       'requested_by_kind', p.requested_by_kind,
                       'flows', (select coalesce(jsonb_agg(distinct jsonb_build_object(
                                     'producto', item ->> 'producto', 'flujo', item ->> 'flujo',
                                     'tipo', item ->> 'tipo')), '[]'::jsonb)
                                   from jsonb_array_elements(p.items) item)) as row_data
              from public.flow_change_proposals p
             where p.tenant_ref = v_instance.o_tenant_ref and p.ally_ref = v_instance.o_ally_ref
               and (p_proposal_id is null or p.id <> p_proposal_id)
               and (case when p_proposal_id is null
                         then p.status in ('validada', 'aprobada') and clock_timestamp() < p.expires_at
                         else p.status = 'borrador'
                              or (p.status in ('validada', 'aprobada') and clock_timestamp() < p.expires_at)
                    end)
               and (p_proposal_id is null or exists (
                       select 1 from jsonb_array_elements(p.items) item
                        where v_flows @> jsonb_build_array(jsonb_build_object(
                                 'producto', item ->> 'producto', 'flujo', item ->> 'flujo'))))
             order by p.created_at desc, p.id
             limit 50) open_rows), '[]'::jsonb));
end;
$function$;

-- Aprobar, con el ID de Slack de la sesion.
create function public.approver_approve_flow_change_v1(
    p_instance jsonb, p_session_hash text, p_proposal_id uuid, p_fingerprint text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_actor text;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    return public._flow_change_approve(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_proposal_id,
        p_fingerprint, 'slack', v_actor);
end;
$function$;

-- Publicar: lo mismo que operator_publish_flow_change_v1 (la zona de la
-- instancia va en p_instance.time_zone), con una sesion viva.
create function public.approver_publish_flow_change_v1(
    p_instance jsonb, p_session_hash text, p_proposal_id uuid, p_fingerprint text, p_revalidation jsonb)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
begin
    v_instance := public._flow_store_instance(p_instance);
    perform public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    return public.operator_publish_flow_change_v1(p_instance, p_proposal_id, p_fingerprint, p_revalidation);
end;
$function$;

-- Pausar, con la generacion que mostro la pagina.
create function public.approver_pause_flow_v1(
    p_instance jsonb, p_session_hash text, p_product_ref text, p_flow_key text, p_expected_generation bigint,
    p_reason text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_actor text;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    return public._flow_change_pause(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_product_ref, p_flow_key,
        p_expected_generation, 'slack', v_actor, p_reason);
end;
$function$;

-- Descartar, con un motivo de una linea.
create function public.approver_discard_flow_change_v1(
    p_instance jsonb, p_session_hash text, p_proposal_id uuid, p_reason text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_instance record;
    v_actor text;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    return public._flow_change_discard(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_proposal_id,
        'slack', v_actor, p_reason);
end;
$function$;

-- Sumar un telefono a la cohorte (decision 11 del 2026-10-27). Solo agregar.
create function public.approver_flow_cohort_v1(
    p_instance jsonb, p_session_hash text, p_product_ref text, p_flow_key text, p_phone text, p_reason text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
set lock_timeout = '5s'
as $function$
declare
    v_instance record;
    v_actor text;
begin
    v_instance := public._flow_store_instance(p_instance);
    v_actor := public._flow_approval_session_actor(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_session_hash);
    return public._flow_cohort_change(v_instance.o_tenant_ref, v_instance.o_ally_ref, p_product_ref, p_flow_key,
        'agregar', p_phone, 'slack', v_actor, p_reason);
end;
$function$;

-- ===========================================================================
-- 6. Los permisos. Las tablas, para nadie de la API. Las dos internas, para
--    nadie. Los envoltorios y las de la sesion, solo para el rol.
-- ===========================================================================

do $acl$
declare
    v_table text;
    v_function regprocedure;
    v_role text;
begin
    foreach v_table in array array['flow_approval_oidc_states', 'flow_approval_sessions'] loop
        execute format('alter table public.%I enable row level security', v_table);
        execute format('revoke all on table public.%I from public', v_table);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role')
             order by r.rolname
        loop
            execute format('revoke all on table public.%I from %I', v_table, v_role);
        end loop;
    end loop;
    foreach v_function in array array[
        'public._flow_approval_session_actor(text,text,text)',
        'public._flow_cohort_change(text,text,text,text,text,text,text,text,text)',
        'public.begin_flow_approval_oidc_v1(jsonb,text,text,text,timestamptz)',
        'public.consume_flow_approval_oidc_v1(jsonb,text)',
        'public.create_flow_approval_session_v1(jsonb,text,text,text,text,timestamptz)',
        'public.validate_flow_approval_session_v1(jsonb,text)',
        'public.revoke_flow_approval_session_v1(jsonb,text)',
        'public.approver_get_flow_change_proposal_v1(jsonb,text,uuid)',
        'public.approver_read_flow_store_v1(jsonb,text)',
        'public.approver_read_open_flow_changes_v1(jsonb,text,uuid)',
        'public.approver_approve_flow_change_v1(jsonb,text,uuid,text)',
        'public.approver_publish_flow_change_v1(jsonb,text,uuid,text,jsonb)',
        'public.approver_pause_flow_v1(jsonb,text,text,text,bigint,text)',
        'public.approver_discard_flow_change_v1(jsonb,text,uuid,text)',
        'public.approver_flow_cohort_v1(jsonb,text,text,text,text,text)'
    ]::regprocedure[] loop
        execute format('revoke all on function %s from public', v_function);
        for v_role in
            select r.rolname from pg_roles r
             where r.rolname in ('anon', 'authenticated', 'service_role')
             order by r.rolname
        loop
            execute format('revoke all on function %s from %I', v_function, v_role);
        end loop;
        if v_function not in ('public._flow_approval_session_actor(text,text,text)'::regprocedure,
                              'public._flow_cohort_change(text,text,text,text,text,text,text,text,text)'::regprocedure) then
            execute format('grant execute on function %s to flow_change_approver', v_function);
        end if;
    end loop;
end;
$acl$;

commit;
