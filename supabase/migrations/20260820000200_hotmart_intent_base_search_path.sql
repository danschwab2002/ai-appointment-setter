-- Forward-only postflight hotfix for the owner-only Hotmart admission bases.
-- The expand migration renamed existing SECURITY DEFINER functions whose
-- historical search_path was public-first. Keep their ACL unchanged and make
-- catalog resolution explicit before the wrappers invoke them.

begin;

alter function public._admit_hotmart_purchase_approved_base(text, jsonb)
    set search_path = pg_catalog, public, pg_temp;

alter function public._admit_hotmart_cart_abandonment_base(text, jsonb)
    set search_path = pg_catalog, public, pg_temp;

commit;
