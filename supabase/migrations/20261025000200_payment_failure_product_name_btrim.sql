-- Migration: el pago rechazado portable compara el nombre del producto sin
-- los espacios de los bordes (E-087, 2026-10-25).
--
-- Por que. Hotmart manda el producto de Maribel como
-- "El Protocolo Biohacking + " (26 caracteres, con un espacio al final),
-- medido el 2026-10-25 en el primer carrito guardado de su base
-- (tests/fixtures/hotmart_product_name_trailing_space_maribel_20261025.json).
-- admit_portable_hotmart_payment_failure (definicion viva: 20261001000100)
-- compara data.product.name contra commercial_ally_runtime_bindings.product_name
-- tal cual, asi que con el nombre del manifiesto sin el espacio todo pago
-- rechazado de Maribel termina en portable_hotmart_payment_failure_binding_mismatch:
-- ni se rutea ni se guarda (la captura listen-only, 20261024000100, llama a la
-- misma funcion). El bridge ya recorta el nombre antes de comparar con el
-- manifiesto (hotmart._str hace strip()), y el carrito no compara el nombre:
-- solo el pago rechazado tenia este defecto.
--
-- Que cambia. Una sola linea de una sola funcion, con un reemplazo contado
-- sobre la definicion viva: los dos lados de la comparacion del nombre pasan
-- por btrim. El id del producto y la oferta se siguen comparando igual, y la
-- identidad, el scope y la evidencia no cambian. Un nombre que difiere en algo
-- mas que los espacios de los bordes sigue rechazandose.
--
-- Las otras comparaciones de nombre de producto de las RPC vigentes, revisadas:
--   - plan_portable_payment_failure_recovery compara el binding con
--     p_product_name, que el bridge toma del manifiesto (no del payload);
--   - las del precheckout (20260930000200, 20260821000200) comparan el payload
--     canonico, que ya se arma recortado con btrim(..., v_trim_chars);
--   - el carrito de Johanna (20260810000200) ya compara lower(btrim(...)) de los
--     dos lados, y el portable no compara el nombre.
-- Ninguna tiene el mismo defecto; no se tocan.
--
-- Grants: create or replace (lo que hace execute de la definicion) conserva los
-- privilegios de la funcion, asi que el ACL no cambia.

begin;

do $product_name_btrim$
declare
    v_signature constant text :=
        'public.admit_portable_hotmart_payment_failure(text,text,integer,text,jsonb,text,text)';
    v_old constant text :=
        E'       or p_payload #>> ''{data,product,name}'' is distinct from v_binding.product_name\n';
    v_new constant text :=
        E'       or btrim(p_payload #>> ''{data,product,name}'') is distinct from btrim(v_binding.product_name)\n';
    v_definition text;
begin
    select pg_get_functiondef(v_signature::regprocedure) into v_definition;
    if v_definition is null
       or length(v_definition) - length(replace(v_definition, v_old, ''))
          <> length(v_old)
       or position('btrim(v_binding.product_name)' in v_definition) > 0 then
        raise exception using errcode = '55000',
            message = 'unexpected_payment_failure_product_name_definition',
            detail = v_signature;
    end if;
    execute replace(v_definition, v_old, v_new);
    if (
        select length(definition) - length(replace(definition, v_new, ''))
        from (select pg_get_functiondef(v_signature::regprocedure) as definition) live
    ) <> length(v_new) then
        raise exception using errcode = '55000',
            message = 'payment_failure_product_name_btrim_not_applied',
            detail = v_signature;
    end if;
end;
$product_name_btrim$;

commit;
