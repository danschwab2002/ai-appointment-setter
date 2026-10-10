-- Migration: la reserva del seguimiento con cupon para el runtime con
-- manifiesto, claim_portable_conversation_followup_v1.
--
-- El hueco. El barredor del seguimiento con cupon (bridge,
-- followup_discount.py) reserva con claim_conversation_followup_v1, que emite
-- el link con reserve_chatwoot_checkout_issuance_v2. Esa reserva exige el
-- external_user_id exacto del caso, y en un runtime con manifiesto el caso
-- puede estar guardado con la otra forma del movil: en ATT1 el primer contacto
-- toma la identidad del formulario (52...) y Chatwoot escribe desde 521... Con
-- 52 el cupon no saldria nunca (issuance_blocked_identity); con 521 saldria
-- con la oferta por defecto y una intencion fabricada. El link del agente ya
-- lo resolvio con un par: el bridge resuelve la identidad con que la base
-- conoce a quien escribe (resolve_inbound_external_user_id) y reserva con
-- reserve_portable_checkout_issuance_v2 (20261001000100, punto 8), que busca
-- la intencion y el opt-out por las dos formas del movil.
--
-- Que crea. Una funcion nueva, con la misma firma, el mismo resultado y las
-- mismas barreras que claim_conversation_followup_v1, y dos cambios:
--
-- 1. El link sale de reserve_portable_checkout_issuance_v2, con los mismos
--    argumentos: la reserva del link del agente en ATT1.
-- 2. Una barrera nueva, justo despues de calcular v_inbound_only
--    (20261001000500): si la conversacion no fue adoptada, devuelve
--    blocked_not_template_reply y no reserva nada. v_inbound_only es
--    verdadero solo si la conversacion tiene el evento de adopcion, que deja
--    unicamente la admision portable (20261001000400) cuando salio ahi una
--    plantilla del piloto (carrito, primer contacto o pago fallido) y la
--    persona contesto. Es la politica aprobada later_step: el 10 % solo
--    despues de una respuesta a la plantilla inicial
--    (docs/design/att1-commercial-information-approval-v1.md). Quien escribio
--    por su cuenta, o mientras la plantilla estaba en vuelo, no recibe el
--    cupon: no mandarlo es el error barato.
--
-- La barrera va despues del bloqueo de la conversacion y del limite de uno por
-- conversacion, y antes del conteo de casos, la derivacion, la compra y la
-- reserva: una conversacion que no fue adoptada no escribe nada.
--
-- Como se aplica. La funcion nueva se deriva de la definicion VIVA de la
-- compartida (pg_get_functiondef), el metodo de la reserva portable en
-- 20261001000100: se reemplazan tres textos, se exige cada uno exactamente una
-- vez y que la compartida todavia no nombre la reserva portable ni la barrera.
-- Si algo no coincide, falla con 55000 y no cambia nada: si alguien cambio la
-- compartida hay que mirarlo, en vez de derivar una copia a ciegas.
--   - El nombre: public.claim_conversation_followup_v1( (en la cabecera).
--   - La llamada a la reserva: from public.reserve_chatwoot_checkout_issuance_v2(
--   - El cierre del calculo de v_inbound_only, al que se le agrega el bloque
--     entre portable_followup_template_reply: begin y end. En el ancla, el
--     nombre del evento va partido en dos: solo la 000400 lo escribe y solo la
--     000500 lo nombra entero
--     (tests/test_commercial_case_lookups_by_inbound_kind_migration.py), y
--     esta migracion solo lo busca en la definicion viva.
--
-- Sin la reserva portable no hay nada que crear: avisa y no cambia nada. Es la
-- base de Johanna, que llega hasta 20260928000400 mas 20261005000100, sin la
-- cadena portable del 2026-09-29 al 2026-10-01; su bridge no tiene manifiesto
-- y nunca llama a esta funcion. Si algun dia se le aplica esa cadena, esta
-- migracion se vuelve a correr despues.
--
-- Idempotente: la compartida no cambia, asi que una segunda corrida vuelve a
-- crear la misma funcion (create or replace conserva owner y grants) y repite
-- los permisos.
--
-- Permisos. Supabase le da execute por defecto a toda funcion nueva: se revoca
-- a public, anon y authenticated, y solo service_role la ejecuta (es un
-- entrypoint del bridge). Todo va guardado con to_regprocedure (sin la funcion
-- no hay nada que tocar) y con to_regrole (los validadores de PGlite no
-- siempre crean los roles), y se verifica por presencia al final.
--
-- Lo que NO hace: no toca tablas, filas ni ninguna funcion existente;
-- claim_conversation_followup_v1 queda identica. No siembra filas. Un bridge
-- sin manifiesto, o uno anterior a 1.5.0, sigue igual: nadie llama a la
-- funcion nueva hasta que el bridge con manifiesto prende
-- CONVERSATION_FOLLOWUP_ENABLED.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

do $followup$
declare
    v_portable_reserve constant text :=
        'public.reserve_portable_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamptz)';
    v_shared_claim constant text :=
        'public.claim_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)';
    v_portable_claim constant text :=
        'public.claim_portable_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)';
    -- Los tres textos que se reemplazan, cada uno exactamente una vez.
    v_shared_head constant text := 'public.claim_conversation_followup_v1(';
    v_portable_head constant text := 'public.claim_portable_conversation_followup_v1(';
    v_shared_reserve_call constant text :=
        'from public.reserve_chatwoot_checkout_issuance_v2(';
    v_portable_reserve_call constant text :=
        'from public.reserve_portable_checkout_issuance_v2(';
    -- El cierre del calculo de v_inbound_only (20261001000500), con el nombre
    -- del evento partido en dos (ver la cabecera).
    v_inbound_only_end constant text :=
        E'          and adoption.event_type = ''inbound_adopted_'
        || E'template_conversation''\n'
        || E'    );\n';
    v_template_reply_block constant text := $bloque$
    -- portable_followup_template_reply: begin
    -- Con manifiesto, el cupon sale solo en una conversacion adoptada: la
    -- respuesta a una plantilla nuestra (20261010000100).
    if not v_inbound_only then
        outcome := 'blocked_not_template_reply';
        return next;
        return;
    end if;
    -- portable_followup_template_reply: end
$bloque$;
    v_definition text;
    v_created text;
begin
    if to_regprocedure(v_portable_reserve) is null then
        raise notice 'portable_conversation_followup_claim: % no existe en esta base (sin 20261001000100); no se creo nada',
            v_portable_reserve;
        return;
    end if;

    v_definition := pg_get_functiondef(to_regprocedure(v_shared_claim));
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_shared_head, ''))
          <> length(v_shared_head)
       or length(v_definition) - length(replace(v_definition, v_shared_reserve_call, ''))
          <> length(v_shared_reserve_call)
       or length(v_definition) - length(replace(v_definition, v_inbound_only_end, ''))
          <> length(v_inbound_only_end)
       or position('reserve_portable_checkout_issuance_v2' in v_definition) > 0
       or position('blocked_not_template_reply' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_conversation_followup_claim_definition',
            detail = v_shared_claim;
    end if;

    execute replace(
        replace(
            replace(v_definition, v_shared_head, v_portable_head),
            v_shared_reserve_call,
            v_portable_reserve_call
        ),
        v_inbound_only_end,
        v_inbound_only_end || v_template_reply_block
    );

    v_created := pg_get_functiondef(to_regprocedure(v_portable_claim));
    if v_created is null
       or position('portable_followup_template_reply: begin' in v_created) = 0
       or position(v_portable_reserve_call in v_created) = 0 then
        raise exception using errcode = '55000',
            message = 'portable_conversation_followup_claim_not_created',
            detail = v_portable_claim;
    end if;
end
$followup$;

-- Supabase le da execute por defecto a toda funcion nueva: se revoca a todos y
-- queda solo para service_role, el rol del bridge. Sin la funcion (la base de
-- Johanna) no hay nada que tocar.
do $roles$
begin
    if to_regprocedure(
        'public.claim_portable_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)'
    ) is null then
        return;
    end if;

    revoke all on function public.claim_portable_conversation_followup_v1(
        bigint, bigint, bigint, text, text, text, text, text, text, text,
        bigint, bigint, integer, text, timestamptz
    ) from public;
    if to_regrole('anon') is not null then
        revoke all on function public.claim_portable_conversation_followup_v1(
            bigint, bigint, bigint, text, text, text, text, text, text, text,
            bigint, bigint, integer, text, timestamptz
        ) from anon;
    end if;
    if to_regrole('authenticated') is not null then
        revoke all on function public.claim_portable_conversation_followup_v1(
            bigint, bigint, bigint, text, text, text, text, text, text, text,
            bigint, bigint, integer, text, timestamptz
        ) from authenticated;
    end if;
    if to_regrole('service_role') is not null then
        grant execute on function public.claim_portable_conversation_followup_v1(
            bigint, bigint, bigint, text, text, text, text, text, text, text,
            bigint, bigint, integer, text, timestamptz
        ) to service_role;
    end if;

    -- Por presencia: service_role la ejecuta y ningun rol de la API.
    if exists (
           select 1 from pg_roles
           where rolname = 'service_role'
             and not has_function_privilege(
                 rolname,
                 'public.claim_portable_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)',
                 'EXECUTE'
             )
       )
       or exists (
           select 1 from pg_roles
           where rolname in ('anon', 'authenticated')
             and has_function_privilege(
                 rolname,
                 'public.claim_portable_conversation_followup_v1(bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz)',
                 'EXECUTE'
             )
       ) then
        raise exception using errcode = '55000',
            message = 'portable_conversation_followup_claim_acl_unexpected',
            detail = 'solo service_role ejecuta claim_portable_conversation_followup_v1';
    end if;
end
$roles$;

commit;
