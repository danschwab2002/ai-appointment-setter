-- Migration: el modo «solo guardar» de Hotmart (E-057, opcion B de
-- documentacion/2026-10-24-maribel-guardar-carritos-sin-mandar.md, aprobada
-- por Dan el 2026-10-24).
--
-- Por que. Una instancia en solo escucha (Maribel, toda aliada nueva) guarda
-- hoy solo la compra aprobada. Para guardar un carrito
-- (PURCHASE_OUT_OF_SHOPPING_CART) o un pago rechazado (PURCHASE_CANCELED) el
-- bridge necesita PORTABLE_HOTMART_RECOVERY_ENABLED o
-- PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED, y cada uno exige su flujo prendido
-- y su plantilla aprobada en el manifiesto. Ademas, lo que esas admisiones
-- guardan queda en processing_status = 'received': el dia que se prende el
-- worker de resolucion, toma toda la pila sin filtro de antiguedad.
--
-- Que crea. Una funcion nueva, ninguna tabla, ninguna otra definicion cambia:
--
-- admit_portable_hotmart_listen_only_capture_v1(tenant, funnel, binding,
-- evento, payload, email, telefono): llama a la admision portable de hoy del
-- tipo del evento (admit_portable_hotmart_cart_abandonment o
-- admit_portable_hotmart_payment_failure, con las mismas validaciones del
-- binding, el scope, la procedencia, la identidad y la correlacion) y, si el
-- evento entro nuevo, lo deja en processing_status = 'ignored' con
-- processing_error = 'listen_only_capture', en la misma transaccion. Asi el
-- worker de resolucion (fetch_pending_events, que pide solo 'received') no lo
-- toma nunca, ni hoy ni el dia que se prendan los flujos.
--
-- - inserted: el evento entro y quedo ignored / listen_only_capture.
-- - duplicate: ya estaba; no se le cambia el estado. Un reintento de Hotmart de
--   un evento capturado lo encuentra ya ignored (entro en la misma
--   transaccion). Un evento que entro antes por el camino del flujo sigue en el
--   estado que le dio ese camino: capturar no le roba trabajo al worker.
-- - semantic_conflict: lo mismo que la admision de su tipo.
--
-- Cualquier otro tipo de evento da 22023 listen_only_capture_event_unsupported.
-- Los rechazos de las admisiones de hoy (binding, scope, identidad) salen tal
-- cual, con su mensaje: el bridge los descarta con 200 y una linea en el log.

begin;

create or replace function public.admit_portable_hotmart_listen_only_capture_v1(
    p_tenant_ref text,
    p_funnel_ref text,
    p_binding_version integer,
    p_external_event_id text,
    p_payload jsonb,
    p_normalized_email text,
    p_normalized_phone text
)
returns table (
    outcome text,
    webhook_event_id uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_outcome text;
    v_event_id uuid;
    v_marked integer;
begin
    if p_payload is null or jsonb_typeof(p_payload) <> 'object' then
        raise exception using errcode = '22023',
            message = 'invalid_listen_only_capture_input';
    end if;

    if p_payload #>> '{event}' = 'PURCHASE_OUT_OF_SHOPPING_CART' then
        select admission.outcome, admission.webhook_event_id
          into strict v_outcome, v_event_id
          from public.admit_portable_hotmart_cart_abandonment(
              p_tenant_ref, p_funnel_ref, p_binding_version,
              p_external_event_id, p_payload,
              p_normalized_email, p_normalized_phone
          ) admission;
    elsif p_payload #>> '{event}' = 'PURCHASE_CANCELED' then
        select admission.outcome, admission.webhook_event_id
          into strict v_outcome, v_event_id
          from public.admit_portable_hotmart_payment_failure(
              p_tenant_ref, p_funnel_ref, p_binding_version,
              p_external_event_id, p_payload,
              p_normalized_email, p_normalized_phone
          ) admission;
    else
        raise exception using errcode = '22023',
            message = 'listen_only_capture_event_unsupported';
    end if;

    if v_outcome = 'inserted' then
        update public.webhook_events event
           set processing_status = 'ignored',
               processing_error = 'listen_only_capture',
               processed_at = clock_timestamp()
         where event.id = v_event_id
           and event.processing_status = 'received';
        get diagnostics v_marked = row_count;
        if v_marked <> 1 then
            -- Recien insertado en esta misma transaccion: si no esta en
            -- received, algo cambio el contrato de la admision. Se aborta
            -- todo y el bridge contesta 503 para que Hotmart reintente.
            raise exception using errcode = '55000',
                message = 'listen_only_capture_mark_failed';
        end if;
    end if;

    return query select v_outcome, v_event_id;
end;
$function$;

-- Supabase le da execute por defecto a toda funcion nueva: se revoca a todos y
-- queda solo para service_role, el rol del bridge.
do $roles$
declare
    v_signature text := 'public.admit_portable_hotmart_listen_only_capture_v1(text,text,integer,text,jsonb,text,text)';
begin
    execute format('revoke all on function %s from public', v_signature);
    if to_regrole('anon') is not null then
        execute format('revoke all on function %s from anon', v_signature);
    end if;
    if to_regrole('authenticated') is not null then
        execute format('revoke all on function %s from authenticated', v_signature);
    end if;
    if to_regrole('service_role') is not null then
        execute format('revoke all on function %s from service_role', v_signature);
        execute format('grant execute on function %s to service_role', v_signature);
    end if;

    -- Por presencia: service_role la ejecuta y ningun rol de la API.
    if exists (
           select 1 from pg_roles
           where rolname = 'service_role'
             and not has_function_privilege(rolname, v_signature, 'EXECUTE')
       )
       or exists (
           select 1 from pg_roles
           where rolname in ('anon', 'authenticated')
             and has_function_privilege(rolname, v_signature, 'EXECUTE')
       ) then
        raise exception using errcode = '55000',
            message = 'listen_only_capture_acl_unexpected',
            detail = format('solo service_role ejecuta %s', v_signature);
    end if;
end
$roles$;

commit;
