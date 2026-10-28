-- Sumar una oferta a un scope del piloto publicado, sin version nueva (E-163).
--
-- Una aliada que ya corre suma una landing con otra oferta del mismo producto
-- (Maribel, 2026-10-28: la organica /epma/evg/vsl/org-a con la oferta
-- mmliigg5). La oferta tiene que entrar en el binding, en el catalogo, en los
-- scopes de intencion y en cada scope del piloto del producto: sin ella en
-- additional_offer_codes, el carrito de esa landing muere en
-- pilot_offer_mismatch.
--
-- Un scope del piloto publicado es inmutable (20260810000100), y la salida de
-- siempre es una version nueva. Para sumar una oferta esa salida pierde casos:
-- el arranque de cada envio pide la autorizacion con la version con la que se
-- planifico el caso (pilot_recovery_case_bindings.scope_version), y
-- authorize_lancemos_pilot_request_start la rechaza si el control apunta a otra
-- (pilot_scope_version_mismatch). Un caso planificado con la version vieja que
-- todavia no salio queda reservado sin salir nunca, y los carritos que entran
-- entre la activacion de la version nueva y el redeploy del bridge (que lee la
-- version de sus variables) mueren al planificar.
--
-- Esta migracion deja hacer UNA sola cosa sobre un scope publicado: agregar
-- ofertas al final de additional_offer_codes. Todo lo demas sigue inmutable.
-- validate_pilot_scope_version (el trigger) la admite solo si:
--
-- 1. la transaccion lo pidio para ESA fila: el ajuste local
--    pilot_scope.offer_append vale '<scope_key>/<version>' (set_config con
--    is_local = true, que lo pone scripts/add_instance_offers.py antes de cada
--    update y se borra solo al terminar la transaccion);
-- 2. la fila nueva es igual a la vieja salvo additional_offer_codes y
--    updated_at (comparadas como jsonb, columna por columna);
-- 3. la lista vieja es el principio de la nueva, la nueva es mas larga y no
--    repite codigos (el check de forma de 20260929000100 sigue mirando el resto:
--    hasta 16, sin nulos, sin la offer_code, alfanumericos).
--
-- Agregar solo amplia: ningun caso, intencion ni autorizacion se valido contra
-- una oferta que no estaba (antes se rechazaba). Es el criterio de
-- docs/instalar.md, «Por que se completa la fila», para el binding.
--
-- El resto del trigger se copia de su definicion vigente (20260810000100), con
-- el cambio entre comentarios pilot_scope_offer_append. create or replace
-- conserva el trigger que la usa y sus permisos. No siembra filas de ningun
-- cliente.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

create or replace function public.validate_pilot_scope_version()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $function$
begin
    if tg_op = 'DELETE' then
        if old.status = 'published' then
            raise exception using
                errcode = '55000',
                message = 'published_pilot_scope_is_immutable';
        end if;
        return old;
    end if;

    if tg_op = 'UPDATE' and old.status = 'published' then
        -- pilot_scope_offer_append: begin
        -- La unica excepcion: sumar ofertas al final, pedido por la
        -- transaccion para esta fila (20261028000300).
        if coalesce(current_setting('pilot_scope.offer_append', true), '')
               = old.scope_key || '/' || old.version
           and (to_jsonb(new) - 'additional_offer_codes' - 'updated_at')
               = (to_jsonb(old) - 'additional_offer_codes' - 'updated_at')
           and cardinality(new.additional_offer_codes)
               > cardinality(old.additional_offer_codes)
           and new.additional_offer_codes[1:cardinality(old.additional_offer_codes)]
               = old.additional_offer_codes
           and (select count(distinct code) from unnest(new.additional_offer_codes) code)
               = cardinality(new.additional_offer_codes)
        then
            return new;
        end if;
        -- pilot_scope_offer_append: end
        raise exception using
            errcode = '55000',
            message = 'published_pilot_scope_is_immutable';
    end if;

    if not exists (
        select 1 from pg_timezone_names tz where tz.name = new.timezone
    ) then
        raise exception using
            errcode = '22023',
            message = 'pilot_scope_timezone_invalid';
    end if;

    perform pg_advisory_xact_lock(
        hashtextextended('pilot_scope_timezone:' || new.scope_key, 0)
    );
    if exists (
        select 1
        from public.pilot_scope_versions existing
        where existing.scope_key = new.scope_key
          and existing.version <> new.version
          and existing.timezone <> new.timezone
    ) then
        raise exception using
            errcode = '23514',
            message = 'pilot_scope_timezone_must_remain_constant';
    end if;

    if new.status = 'published' and not exists (
        select 1
        from public.followup_policy_versions policy
        where policy.policy_key = new.policy_key
          and policy.version = new.policy_version
          and policy.status = 'published'
          and policy.purpose = 'cart_recovery'
    ) then
        raise exception using
            errcode = '23514',
            message = 'pilot_scope_policy_not_published';
    end if;

    return new;
end;
$function$;

commit;
