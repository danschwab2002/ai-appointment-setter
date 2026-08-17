begin;

-- Keep the SECURITY DEFINER purchase worker on an explicit trusted lookup order.
-- API roles cannot CREATE in public; pg_temp remains last as required for a safe
-- definer boundary.
alter function public.apply_hotmart_purchase_approved(
    uuid, text, text, text, text, text, timestamptz
) set search_path = pg_catalog, public, pg_temp;

commit;
