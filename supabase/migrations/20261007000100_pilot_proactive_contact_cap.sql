-- Migration: tope de mensajes proactivos por persona entre flujos, en la
-- frontera del piloto.
--
-- El hueco. Una instancia portable tiene tres flujos que le escriben primero a
-- un lead: el primer contacto tras el formulario (scope propio) y el carrito y
-- el pago fallido (scope de recuperacion). Cada scope tiene sus topes total y
-- diario, pero nada limita cuantos mensajes proactivos recibe una MISMA persona
-- sumando los flujos: quien recibio el primer contacto y despues abandona el
-- carrito recibe los dos, y despues el pago fallido. El primer contacto ya se
-- frena si llega antes un carrito o un pago fallido (superseded_by_provider_event,
-- 20261001000200), pero no al reves. CHANGELOG [1.3.0], «Lo que queda fuera», y
-- portable-precheckout-first-contact-v1.md lo dejaron como decision abierta,
-- anterior a abrir el primer contacto y el carrito a la vez en consented_intent.
--
-- Que trae:
-- 1. pilot_proactive_contact_caps: un renglon por tenant con el tope
--    (max_request_starts arranques en request_window). Sin renglon no hay tope
--    y todo queda como hoy: el valor lo decide cada instancia y lo carga su
--    aprovisionamiento. Con RLS y sin privilegios para la API.
-- 2. Un indice de pilot_outbound_request_authorizations por contacto y hora,
--    para el conteo.
-- 3. authorize_lancemos_pilot_request_start, la puerta comun de los tres
--    arranques (mark_lancemos_pilot_request_started,
--    mark_portable_payment_failure_request_started y
--    mark_portable_precheckout_request_started la llaman), suma un bloque antes
--    de los topes del scope: si el tenant del scope tiene tope, toma un lock por
--    persona y cuenta las autorizaciones de la persona en TODOS los scopes del
--    tenant dentro de la ventana. Llegado el tope devuelve authorized=false con
--    pilot_contact_proactive_cap_reached: no inserta, no consume cupo del scope
--    y no deja evento.
--
-- La persona: el contacto del arranque, y ademas cualquier contacto de la misma
-- cuenta de Chatwoot con una identidad de WhatsApp en alguna de las dos formas
-- del telefono de la identidad seleccionada (_whatsapp_phone_variants,
-- 20261001000100: 52/521 y 54/549). Asi dos contactos de la misma persona no
-- duplican el cupo.
--
-- Que cuenta: arranques autorizados (pilot_outbound_request_authorizations),
-- igual que los topes del scope. Un envio que Meta rechaza despues tambien
-- cuenta: el tope protege a la persona de recibir de mas, y frenar de mas es mas
-- barato que escribirle dos veces.
--
-- Lo que pasa con el arranque frenado: lo mismo que con un tope del scope
-- agotado. El bridge lo atrapa (PilotRequestStartRejectedError), el intento
-- queda reservado y se reintenta en cada lease hasta que la accion vence. Si la
-- ventana se libera antes de que venza, sale entonces (dentro del horario de su
-- politica); si no, vence sin salir. No se toca el bridge.
--
-- Carreras. El lock del control es por scope, asi que dos scopes no se
-- serializan entre si: sin un lock por persona, el primer contacto y el carrito
-- de la misma persona podrian autorizarse a la vez y pasar los dos. El bloque
-- toma pg_advisory_xact_lock sobre el tenant y la forma canonica del telefono
-- (el patron de johanna-recovery-budget, 20260829000300), despues del lock del
-- control y antes de los locks de opt-out del envoltorio: siempre en ese orden,
-- sin ciclos. El conteo va despues del lock y ve lo que la otra transaccion
-- confirmo.
--
-- El replay no pasa por aca: un arranque ya autorizado se devuelve antes
-- (replayed = true), con o sin tope.
--
-- Como se aplica. La funcion se modifica sobre su definicion VIVA
-- (pg_get_functiondef), insertando el bloque antes de un texto exacto que se
-- cuenta: si no aparece exactamente una vez, o si el bloque ya esta, la
-- migracion falla con 55000 y no cambia nada (el metodo de 20261005000100). El
-- texto esta igual en las cuatro definiciones de la funcion (20260810000100,
-- 20260929000100, 20260929000200, 20260930000300), asi que la migracion vale
-- para la base de Johanna, que no tiene las del 2026-09-29 al 2026-10-01, y
-- para la de ATT1. Johanna no pasa por esta funcion
-- (LANCEMOS_PILOT_BOUNDARY_ENABLED=false) y no tiene renglon de tope. El
-- bloque usa _whatsapp_phone_canonical y _whatsapp_phone_variants solo cuando
-- el tenant tiene tope, y plpgsql las resuelve al ejecutar. create or replace
-- conserva los grants.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- 1. El tope por tenant.
create table public.pilot_proactive_contact_caps (
    tenant_key text primary key check (length(btrim(tenant_key)) > 0),
    max_request_starts integer not null
        check (max_request_starts between 1 and 20),
    request_window interval not null
        check (request_window >= interval '1 hour'
               and request_window <= interval '30 days'),
    approved_by text not null check (length(btrim(approved_by)) > 0),
    approved_at timestamptz not null default clock_timestamp(),
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp()
);

alter table public.pilot_proactive_contact_caps enable row level security;

-- 2. El conteo por persona.
create index pilot_outbound_authorizations_contact_time_idx
on public.pilot_outbound_request_authorizations(contact_id, authorized_at);

-- 3. El bloque en la puerta comun, sobre su definicion viva.
do $cap$
declare
    v_signature constant text :=
        'public.authorize_lancemos_pilot_request_start(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid,uuid,uuid,timestamptz)';
    v_anchor constant text :=
        E'    v_local_date := (v_authorized_at at time zone v_scope.timezone)::date;\n';
    v_block constant text :=
        E'    -- pilot_proactive_contact_cap: begin\n'
        || E'    -- Tope por persona entre flujos (20261007000100). Sin renglon del\n'
        || E'    -- tenant no hay tope. Lock por persona (tenant + telefono canonico)\n'
        || E'    -- y conteo de sus arranques en todos los scopes del tenant.\n'
        || E'    declare\n'
        || E'        v_cap public.pilot_proactive_contact_caps%rowtype;\n'
        || E'        v_cap_phone text;\n'
        || E'        v_cap_recent integer;\n'
        || E'    begin\n'
        || E'        select cap.* into v_cap\n'
        || E'        from public.pilot_proactive_contact_caps cap\n'
        || E'        where cap.tenant_key = v_scope.tenant_key;\n'
        || E'        if found then\n'
        || E'            v_cap_phone := coalesce(\n'
        || E'                public._whatsapp_phone_canonical(v_identity.external_user_id),\n'
        || E'                v_identity.external_user_id\n'
        || E'            );\n'
        || E'            perform pg_advisory_xact_lock(hashtextextended(\n'
        || E'                ''pilot-proactive-contact-cap:'' || v_scope.tenant_key\n'
        || E'                    || '':'' || coalesce(v_cap_phone, p_contact_id::text),\n'
        || E'                0\n'
        || E'            ));\n'
        || E'            select count(*)::integer into v_cap_recent\n'
        || E'            from public.pilot_outbound_request_authorizations authrow\n'
        || E'            join public.pilot_scope_versions scope_row\n'
        || E'              on scope_row.scope_key = authrow.scope_key\n'
        || E'             and scope_row.version = authrow.scope_version\n'
        || E'            where scope_row.tenant_key = v_scope.tenant_key\n'
        || E'              and authrow.authorized_at > v_authorized_at - v_cap.request_window\n'
        || E'              and (\n'
        || E'                  authrow.contact_id = p_contact_id\n'
        || E'                  or authrow.contact_id in (\n'
        || E'                      select other.contact_id\n'
        || E'                      from public.channel_identities other\n'
        || E'                      where other.channel = ''whatsapp''\n'
        || E'                        and other.account_id = v_identity.account_id\n'
        || E'                        and other.external_user_id = any(\n'
        || E'                            coalesce(\n'
        || E'                                public._whatsapp_phone_variants(v_identity.external_user_id),\n'
        || E'                                array[v_identity.external_user_id]\n'
        || E'                            )\n'
        || E'                        )\n'
        || E'                  )\n'
        || E'              );\n'
        || E'            if v_cap_recent >= v_cap.max_request_starts then\n'
        || E'                return query select false,\n'
        || E'                    ''pilot_contact_proactive_cap_reached''::text,\n'
        || E'                    v_control.generation, null::uuid, false;\n'
        || E'                return;\n'
        || E'            end if;\n'
        || E'        end if;\n'
        || E'    end;\n'
        || E'    -- pilot_proactive_contact_cap: end\n'
        || E'\n';
    v_definition text;
begin
    if to_regprocedure(v_signature) is null then
        raise exception using errcode = '55000',
            message = 'pilot_authorize_function_missing',
            detail = v_signature;
    end if;
    v_definition := pg_get_functiondef(v_signature::regprocedure);
    if length(v_definition) - length(replace(v_definition, v_anchor, ''))
           <> length(v_anchor)
       or position('pilot_proactive_contact_cap: begin' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_pilot_authorize_definition',
            detail = v_signature;
    end if;
    execute replace(v_definition, v_anchor, v_block || v_anchor);
    if position('pilot_proactive_contact_cap: begin'
                in pg_get_functiondef(v_signature::regprocedure)) = 0 then
        raise exception using errcode = '55000',
            message = 'pilot_authorize_definition_not_replaced',
            detail = v_signature;
    end if;
end;
$cap$;

-- Supabase le da todos los privilegios de tabla a service_role por defecto: la
-- tabla queda para nadie de la API (la lee la funcion security definer). La
-- carga la instancia con el rol duenio, como el resto de su aprovisionamiento.
revoke all on table public.pilot_proactive_contact_caps from public;
do $roles$
declare v_role text;
begin
 for v_role in select rolname from pg_roles where rolname in ('anon','authenticated','service_role') loop
  execute format('revoke all on table public.pilot_proactive_contact_caps from %I',v_role);
 end loop;
end;
$roles$;

commit;
