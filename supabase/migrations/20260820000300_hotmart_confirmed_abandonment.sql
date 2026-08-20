-- Align the durable authoritative Hotmart abandonment classification with the
-- accepted correlation contract. This is forward-only because 20260820000100
-- has already been applied in Supabase Cloud.

begin;

do $confirmed_abandonment_constraint$
declare
    v_constraint_name name;
    v_constraint_count integer;
begin
    select count(*), min(constraint_row.conname)
      into v_constraint_count, v_constraint_name
    from pg_constraint constraint_row
    where constraint_row.conrelid = 'public.purchase_intents'::regclass
      and constraint_row.contype = 'c'
      and position(
          'current_classification' in pg_get_constraintdef(constraint_row.oid)
      ) > 0;

    if v_constraint_count <> 1 or v_constraint_name is null then
        raise exception 'purchase_intents_classification_constraint_mismatch';
    end if;

    execute format(
        'alter table public.purchase_intents drop constraint %I',
        v_constraint_name
    );
end;
$confirmed_abandonment_constraint$;

update public.purchase_intents
set current_classification = 'confirmed_abandonment',
    updated_at = clock_timestamp()
where current_classification = 'abandonment_candidate';

alter table public.purchase_intents
add constraint purchase_intents_current_classification_check
check (current_classification is null or current_classification in (
    'payment_failure_supported', 'confirmed_abandonment',
    'identity_conflict', 'tracking_incomplete', 'expired_unknown'
));

do $confirmed_abandonment_correlator$
declare
    v_function regprocedure :=
        to_regprocedure('public.correlate_hotmart_purchase_intent(uuid)');
    v_definition text;
    v_old text := '''abandonment_candidate''';
    v_new text := '''confirmed_abandonment''';
    v_occurrences integer;
begin
    if v_function is null then
        raise exception 'hotmart_intent_correlator_missing';
    end if;

    select pg_get_functiondef(v_function)
      into strict v_definition;

    v_occurrences := (
        length(v_definition) - length(replace(v_definition, v_old, ''))
    ) / length(v_old);

    if v_occurrences <> 1 then
        raise exception 'hotmart_intent_abandonment_marker_mismatch';
    end if;

    execute replace(v_definition, v_old, v_new);
end;
$confirmed_abandonment_correlator$;

alter function public.correlate_hotmart_purchase_intent(uuid)
    set search_path = pg_catalog, public, pg_temp;

commit;
