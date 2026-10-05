-- Migration: el marcador del recuperador en el sck se separa con ~, como el sck
-- del anuncio, y la compra que trae el sck compuesto deja de rechazarse.
-- Decision de Dan del 2026-10-05 (E46 del estandar de tracking de Lancemos).
--
-- Que cambia. Desde el 2026-09-25 el sck del anuncio se separa con ~
-- (utm_source~utm_term~utm_content~utm_medium~utm_campaign, E10/E13), pero el
-- marcador del recuperador seguia con | a proposito: 20260925000100 dejo escrito
-- "No se unifica sin una decision nueva". Esta es esa decision:
--     antes:  <sck del anuncio>|hermes|v1|<ulid>   o   hermes|v1|<ulid>
--     ahora:  <sck del anuncio>~hermes~v1~<ulid>   o   hermes~v1~<ulid>
-- Solo cambia el separador: los campos y su orden no. src=hermes,
-- sck_format_version = 'v1' y la firma de las RPC tampoco. Con ~, quien parte el
-- sck por ~ lee los campos del anuncio sin que el ultimo arrastre el marcador. La
-- | se sigue encodeando a %7C en la URL, porque el tramo del anuncio de los
-- linajes viejos (drceo) puede traerla.
--
-- Los lectores aceptan las dos formas y las van a seguir aceptando: los links que
-- ya salieron con | siguen llegando a Hotmart, y una reserva que quedo en
-- 'reserved' se reusa tal cual (replay y seguimiento con cupon).
--
-- El bug que se arregla de paso. admit_and_correlate_hotmart_checkout_issuance_v2
-- (20260914000100, su unica definicion) exigia '^hermes[|]v1[|]<ulid>$', o sea el
-- marcador solo. 20260922000200 puso el sck del anuncio delante del marcador y
-- arreglo a dos lectores (el bridge y el correlador), pero no a este. Una compra
-- hecha con un link con atribucion del anuncio (full o sck_only) entra por esta
-- RPC porque el bridge la reconoce como del recuperador; la RPC lanzaba 22023, el
-- bridge le contestaba 503 a Hotmart y la compra no se guardaba ni marcaba la
-- intencion como comprada. Hotmart reintenta, y cuenta esas fallas para
-- desactivar el webhook. Ahora acepta lo mismo que el correlador, con | o con ~.
--
-- Dato real (2026-10-05):
--   - ATT1, la emision de la prueba del entrante (11:41Z): sck_value =
--     hermes|v1|01M45XXQRE387T4MYWFK2P6DXA, la forma que deja de escribirse.
--   - Johanna, webhook_events desde el 2026-09-25: 13 PURCHASE_APPROVED. 10 traen
--     el sck del core con la ~ literal en data.purchase.origin.sck, ninguno con
--     %7C; la venta correlacionada del 2026-10-03 trae
--     hermes|v1|01M3R0TZC1E78RR0AS4XQKNCCX. Hotmart devuelve la ~ intacta.
--     Fixture: tests/fixtures/hotmart_purchase_sck_shapes_20261005.json.
--
-- Como se aplica. Las cuatro funciones se modifican sobre su definicion VIVA
-- (pg_get_functiondef), reemplazando un texto exacto que se cuenta antes: si no
-- aparece exactamente una vez, o si el texto nuevo ya esta, la migracion falla
-- con 55000 y no cambia nada, en vez de pisar una definicion que alguien cambio.
-- Es el metodo con el que 20261001000100 derivo la reserva portable. Asi la misma
-- migracion vale para la base de Johanna, que no tiene las migraciones del
-- 2026-09-29 al 2026-10-01, y para la de ATT1, que tiene la cadena entera:
--   - correlate_hotmart_checkout_issuance_v2 (vigente en 20260925000100)
--   - admit_and_correlate_hotmart_checkout_issuance_v2 (20260914000100)
--   - reserve_chatwoot_checkout_issuance_v2 (vigente en 20260927000200)
--   - reserve_portable_checkout_issuance_v2 (20261001000100), solo si existe
-- create or replace conserva los grants y no se crea ninguna funcion. Si mas
-- adelante se aplica 20261001000100 en una base que ya tiene esta, la portable
-- se deriva de la compartida con ~: sus conteos no miran el marcador.
--
-- Orden de despliegue: el bridge que acepta las dos formas va PRIMERO y esta
-- migracion despues. Al reves, el bridge viejo rechaza la fila que devuelve la
-- reserva (_sck_carries_hermes_marker) y el link no sale, y no reconoce una
-- compra con ~ (_HERMES_SCK_TAIL), que cae a la ruta por identidad. Dentro de la
-- migracion, los CHECK se amplian antes de cambiar la escritura.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- 1. Los CHECK de la tabla aceptan las dos formas del marcador. Las filas que ya
--    existen, todas con |, se revalidan al agregar el constraint y pasan.
alter table public.checkout_link_issuances
    drop constraint checkout_link_issuances_sck_value_shape;

alter table public.checkout_link_issuances
    add constraint checkout_link_issuances_sck_value_shape check (
        sck_value = 'hermes|v1|' || issuance_ulid
        or right(sck_value, length(issuance_ulid) + 11)
           = '|hermes|v1|' || issuance_ulid
        or sck_value = 'hermes~v1~' || issuance_ulid
        or right(sck_value, length(issuance_ulid) + 11)
           = '~hermes~v1~' || issuance_ulid
    );

-- En la URL la | del marcador viaja como %7C y la ~ literal (es unreserved). El
-- tramo del anuncio, ya URL-encodeado, admite las dos.
alter table public.checkout_link_issuances
    drop constraint checkout_link_issuances_url_shape;

alter table public.checkout_link_issuances
    add constraint checkout_link_issuances_url_shape check (
        checkout_url_final ~ ('^https://pay[.]hotmart[.]com/[A-Za-z0-9_-]+'
            || '[?]off=[A-Za-z0-9_-]+&checkoutMode=[1-9][0-9]*&src=hermes'
            || '&sck=(([A-Za-z0-9._%~-]+%7C)?hermes%7Cv1%7C'
            || '|([A-Za-z0-9._%~-]+~)?hermes~v1~)'
            || '[0-7][0-9A-HJKMNP-TV-Z]{25}'
            || '(&fbclid=[A-Za-z0-9._-]+)?$')
    );

-- 2. Las cuatro funciones, sobre su definicion viva:
--    - el correlador y la admision de la compra aceptan el marcador con | o con
--      ~, solo o con el sck del anuncio delante (la admision, por el bug);
--    - las dos reservas escriben el marcador con ~.
do $marker$
declare
    v_recognizer_old constant text :=
        '''^([A-Za-z0-9._|~-]+[|])?hermes[|]v1[|][0-7][0-9A-HJKMNP-TV-Z]{25}$''';
    v_admission_old constant text :=
        '''^hermes[|]v1[|][0-7][0-9A-HJKMNP-TV-Z]{25}$''';
    v_marker_new constant text :=
        '''^(([A-Za-z0-9._|~-]+[|])?hermes[|]v1[|]|([A-Za-z0-9._|~-]+~)?hermes~v1~)'
        || '[0-7][0-9A-HJKMNP-TV-Z]{25}$''';
    v_writer_old constant text :=
        E'    if v_preserved_sck is not null then\n'
        || E'        v_sck_value := v_preserved_sck || ''|hermes|v1|'' || p_issuance_ulid;\n'
        || E'    else\n'
        || E'        v_sck_value := ''hermes|v1|'' || p_issuance_ulid;\n'
        || E'    end if;';
    v_writer_new constant text :=
        E'    -- 2026-10-05: el marcador se separa con ~, como el sck del anuncio\n'
        || E'    -- (E46). Los lectores siguen aceptando la | de los links anteriores.\n'
        || E'    if v_preserved_sck is not null then\n'
        || E'        v_sck_value := v_preserved_sck || ''~hermes~v1~'' || p_issuance_ulid;\n'
        || E'    else\n'
        || E'        v_sck_value := ''hermes~v1~'' || p_issuance_ulid;\n'
        || E'    end if;';
    v_target record;
    v_definition text;
begin
    for v_target in
        select target.signature, target.old_text, target.new_text, target.required
        from (values
            ('public.correlate_hotmart_checkout_issuance_v2(uuid,text,timestamptz)',
             v_recognizer_old, v_marker_new, true),
            ('public.admit_and_correlate_hotmart_checkout_issuance_v2(text,jsonb,text,timestamptz)',
             v_admission_old, v_marker_new, true),
            ('public.reserve_chatwoot_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamptz)',
             v_writer_old, v_writer_new, true),
            ('public.reserve_portable_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamptz)',
             v_writer_old, v_writer_new, false)
        ) as target(signature, old_text, new_text, required)
    loop
        if to_regprocedure(v_target.signature) is null then
            if v_target.required then
                raise exception using errcode = '55000',
                    message = 'sck_marker_function_missing',
                    detail = v_target.signature;
            end if;
            continue;
        end if;
        v_definition := pg_get_functiondef(v_target.signature::regprocedure);
        if length(v_definition) - length(replace(v_definition, v_target.old_text, ''))
               <> length(v_target.old_text)
           or position(v_target.new_text in v_definition) > 0 then
            raise exception using errcode = '55000',
                message = 'unexpected_sck_marker_definition',
                detail = v_target.signature;
        end if;
        execute replace(v_definition, v_target.old_text, v_target.new_text);
        if position(v_target.new_text in pg_get_functiondef(v_target.signature::regprocedure)) = 0 then
            raise exception using errcode = '55000',
                message = 'sck_marker_definition_not_replaced',
                detail = v_target.signature;
        end if;
    end loop;
end;
$marker$;

commit;
