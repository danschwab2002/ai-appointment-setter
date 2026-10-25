-- Migration: al derivar, el bridge lee el ultimo link que le mandamos a la
-- persona para dejarlo en una nota privada (F-034, D8; E-083/E-091, 2026-10-25).
--
-- Por que. Cuando el agente deriva, la persona del equipo que toma la
-- conversacion no sabe que link de pago recibio la persona: si se lo vuelve a
-- pedir, el equipo manda uno a mano (sin src ni sck, una venta que no se puede
-- contar) o un cupon distinto del que ya tiene. Desde Chatwoot no se puede
-- saber: la API de mensajes no devuelve template_params, y las plantillas del
-- carrito (A1 y sus seguimientos) dejan en content solo el texto, sin el link ni
-- el cupon (fixtures chatwoot_daily_feedback_att1_conversations_20261015.json y
-- chatwoot_followup_candidate_inbox_11_20261010.json). La fuente es
-- checkout_link_issuances, que service_role no puede leer directo (revoke all
-- en 20260914000100): hace falta una lectura.
--
-- Que agrega. Una sola funcion de lectura, nueva:
-- read_handoff_note_checkout_link_v1(p_commercial_case_id, p_chatwoot_account_id,
-- p_chatwoot_inbox_id, p_now). Recibe el caso del entrante que se esta
-- derivando y devuelve, como mucho, una fila: el ultimo link aceptado por
-- Chatwoot (status accepted_by_chatwoot) del MISMO contacto en esa cuenta y ese
-- inbox, con su source_kind, la hora en que se acepto y, si el link es el del
-- carrito (recovery_first_touch), el cupon vigente de ese caso (created o valid,
-- sin vencer a p_now, el de ordinal mas alto: la renovacion gana sobre el de
-- A1). Sin link, cero filas.
--
-- Se busca por contacto y no por conversacion ni por caso: las emisiones del
-- carrito tienen chatwoot_conversation_id nulo y van atadas al caso del carrito,
-- no al del entrante (20261018000200).
--
-- Lo que no hace: no escribe nada, no crea tablas ni roles, no toca ninguna
-- funcion existente. Un link que no se acepto (reserved, request_started,
-- delivery_unknown) no se muestra: puede no haber salido. Un link con compra
-- (purchase_matched) tampoco: la persona ya compro con el. Solo service_role la
-- ejecuta.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $already$
begin
    if to_regclass('public.checkout_link_issuances') is null
       or to_regclass('public.recovery_coupons') is null
       or to_regclass('public.commercial_cases') is null then
        raise exception using errcode = '55000', message = 'handoff_link_note_tables_missing';
    end if;
    if to_regprocedure(
        'public.read_handoff_note_checkout_link_v1(uuid,bigint,bigint,timestamptz)'
    ) is not null then
        raise exception using errcode = '55000', message = 'handoff_link_note_already_applied';
    end if;
end;
$already$;

create function public.read_handoff_note_checkout_link_v1(
    p_commercial_case_id uuid,
    p_chatwoot_account_id bigint,
    p_chatwoot_inbox_id bigint,
    p_now timestamptz
)
returns table (
    checkout_url_final text,
    source_kind text,
    accepted_at timestamptz,
    coupon_code text
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_contact_id uuid;
    v_issuance record;
    v_coupon text;
begin
    if p_commercial_case_id is null
       or p_chatwoot_account_id is null or p_chatwoot_account_id <= 0
       or p_chatwoot_inbox_id is null or p_chatwoot_inbox_id <= 0
       or p_now is null then
        raise exception using errcode = '22023', message = 'invalid_handoff_link_note_read';
    end if;

    select c.contact_id into v_contact_id
    from public.commercial_cases c
    where c.id = p_commercial_case_id;
    if v_contact_id is null then
        return;
    end if;

    select i.checkout_url_final, i.source_kind, i.finalized_at, i.recovery_case_id
    into v_issuance
    from public.checkout_link_issuances i
    where i.contact_id = v_contact_id
      and i.chatwoot_account_id = p_chatwoot_account_id
      and i.chatwoot_inbox_id = p_chatwoot_inbox_id
      and i.status = 'accepted_by_chatwoot'
      and i.finalized_at <= p_now
    order by i.finalized_at desc, i.created_at desc, i.id desc
    limit 1;
    if not found then
        return;
    end if;

    if v_issuance.source_kind = 'recovery_first_touch'
       and v_issuance.recovery_case_id is not null then
        select rc.code into v_coupon
        from public.recovery_coupons rc
        where rc.recovery_case_id = v_issuance.recovery_case_id
          and rc.status in ('created', 'valid')
          and rc.expires_at > p_now
        order by rc.ordinal desc
        limit 1;
    end if;

    checkout_url_final := v_issuance.checkout_url_final;
    source_kind := v_issuance.source_kind;
    accepted_at := v_issuance.finalized_at;
    coupon_code := v_coupon;
    return next;
end;
$function$;

revoke all on function public.read_handoff_note_checkout_link_v1(uuid, bigint, bigint, timestamptz) from public;

do $roles$
declare
    v_role text;
begin
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format(
                'revoke all on function public.read_handoff_note_checkout_link_v1(uuid, bigint, bigint, timestamptz) from %I',
                v_role
            );
        end if;
    end loop;
    if to_regrole('service_role') is not null then
        grant execute on function public.read_handoff_note_checkout_link_v1(uuid, bigint, bigint, timestamptz) to service_role;
    end if;
end;
$roles$;

commit;
