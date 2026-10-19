-- Migration: B1-cupon usa solo un cupon de otro caso que le llego a la persona.
--
-- Por que. La variante B1-cupon del pago rechazado (20261019000200) le escribe
-- a la persona por "su" cupon vigente de otro caso del mismo producto: el del
-- carrito, que llega con A1. Las dos funciones que lo buscan
-- (payment_failure_wants_first_coupon y
-- get_portable_payment_failure_send_context_v1) miraban solo que el cupon
-- estuviera valid y vencido a mas de una hora. Pero el cupon existe antes que
-- el mensaje: claim_due_recovery_coupons_v1 siembra el de cada carrito ~15 min
-- antes de A1 apenas corre RECOVERY_COUPON_WORKER_ENABLED, aunque
-- RECOVERY_DISCOUNT_FIRST_TOUCH_ENABLED este apagado y A1 salga con la
-- plantilla de hoy, sin cupon. Y A1 tambien sale sin cupon si el cupon no esta
-- listo a tiempo (cart_first_touch.py: CartDiscountFallback). B1 exige el
-- worker del cupon: con los dos prendidos y A1 apagado, B1-cupon le hablaria
-- a la persona de un descuento que nunca recibio. Medido en ATT1 el
-- 2026-10-19 sobre la base: unos 27 carritos por dia.
--
-- La prueba de que el cupon llego: el link de A1. A1 emite el link de
-- recuperacion (reserve_recovery_checkout_issuance_v1, source_kind
-- recovery_first_touch) solo despues de leer su cupon valid
-- (compose_cart_discount_first_touch), y ese link sale con el cupon en el
-- boton; el camino sin cupon no emite link. Entonces el cupon ordinal 1 de un
-- carrito llego si ese caso tiene un link recovery_first_touch de una accion
-- cart_abandonment que Chatwoot acepto (accepted_by_chatwoot, o
-- purchase_matched despues), emitido despues de que el cupon quedo valid.
-- delivery_unknown no cuenta: antes de ofrecerle un cupon que quiza no tiene,
-- B1 sale con su variante sin el.
--
-- Por que solo el carrito. El link del pago rechazado se emite en las cuatro
-- variantes, lleve cupon o no, asi que no prueba nada del cupon. Y no hace
-- falta: un cupon de B1-saldo vence a las 24 h, y un rechazo nuevo del mismo
-- producto dentro de las 72 h se suma al mismo caso (20261019000200), no abre
-- otro. Cuando B2 y Z manden cupones, su envio los sumara aca.
--
-- Que crea:
-- 1. recovery_coupon_was_delivered(uuid): la regla de arriba. Privada (nadie
--    de la API la ejecuta): la llaman las dos funciones security definer.
-- 2. Un reemplazo contado en cada una de las dos funciones de B1, sobre su
--    definicion viva: la condicion "and coupon.status = 'valid'" suma
--    "and public.recovery_coupon_was_delivered(coupon.id)". Si el texto no
--    aparece exactamente una vez, o ya fue reemplazado, aborta con 55000 y no
--    cambia nada.
--
-- Que cambia apenas se aplica: nada de lo que corre hoy en ATT1, porque
-- PAYMENT_FAILURE_VARIANTS_ENABLED esta apagado. Con B1 prendido, un cupon de
-- carrito sembrado pero no mandado deja de contar: B1-cupon no sale por el, y
-- B1-saldo pide su propio cupon. Johanna, el carrito, la ficha y la compra no
-- cambian.
--
-- Locks: create function y create or replace; ninguna tabla se altera.

begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

-- ---------------------------------------------------------------------------
-- 1. Si el cupon le llego a la persona.
-- ---------------------------------------------------------------------------
create function public.recovery_coupon_was_delivered(p_coupon_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
    select exists (
        select 1
        from public.recovery_coupons coupon
        join public.checkout_link_issuances issuance
          on issuance.recovery_case_id = coupon.recovery_case_id
        join public.followup_delivery_attempts attempt
          on attempt.id = issuance.delivery_attempt_id
        join public.scheduled_actions action
          on action.id = attempt.action_id
        where coupon.id = p_coupon_id
          and coupon.ordinal = 1
          and coupon.verified_at is not null
          and issuance.source_kind = 'recovery_first_touch'
          and issuance.status in ('accepted_by_chatwoot', 'purchase_matched')
          and issuance.created_at >= coupon.verified_at
          and action.recovery_case_id = coupon.recovery_case_id
          and action.anchor_type = 'cart_abandonment'
    );
$function$;

do $grants$
declare
    v_role text;
begin
    revoke all on function public.recovery_coupon_was_delivered(uuid) from public;
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if to_regrole(v_role) is not null then
            execute format(
                'revoke all on function public.recovery_coupon_was_delivered(uuid) from %I',
                v_role
            );
        end if;
    end loop;
end
$grants$;

-- ---------------------------------------------------------------------------
-- 2. Las dos funciones de B1, sobre su definicion viva, con el texto contado.
-- ---------------------------------------------------------------------------
do $other_coupon_delivered$
declare
    v_signatures constant text[] := array[
        'public.payment_failure_wants_first_coupon(uuid)',
        'public.get_portable_payment_failure_send_context_v1(uuid,text,bigint,timestamptz)'
    ];
    v_old constant text := E'and coupon.status = ''valid''\n';
    v_new constant text :=
        E'and coupon.status = ''valid''\n'
        || E'          and public.recovery_coupon_was_delivered(coupon.id)\n';
    v_signature text;
    v_definition text;
begin
    foreach v_signature in array v_signatures loop
        if to_regprocedure(v_signature) is null then
            raise exception using errcode = '55000',
                message = 'payment_failure_coupon_function_missing',
                detail = v_signature;
        end if;
        v_definition := pg_get_functiondef(v_signature::regprocedure);
        if length(v_definition) - length(replace(v_definition, v_old, ''))
               <> length(v_old)
           or position('recovery_coupon_was_delivered' in v_definition) > 0 then
            raise exception using errcode = '55000',
                message = 'unexpected_payment_failure_coupon_definition',
                detail = v_signature;
        end if;
        execute replace(v_definition, v_old, v_new);
        v_definition := pg_get_functiondef(v_signature::regprocedure);
        if position(v_new in v_definition) = 0 then
            raise exception using errcode = '55000',
                message = 'payment_failure_coupon_not_replaced',
                detail = v_signature;
        end if;
    end loop;
end
$other_coupon_delivered$;

commit;
