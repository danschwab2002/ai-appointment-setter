from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260820000100_hotmart_purchase_intent_correlation.sql"
)


def _sql() -> str:
    return MIGRATION.read_text().lower()


def test_correlation_schema_is_durable_scoped_and_effect_free() -> None:
    sql = _sql()

    assert "create table public.hotmart_purchase_intent_scopes" in sql
    assert "create table public.hotmart_purchase_intent_event_identities" in sql
    assert "create table public.hotmart_purchase_intent_correlations" in sql
    assert "create table public.hotmart_purchase_intent_correlation_candidates" in sql
    assert "'resolved', 'unmatched', 'ambiguous', 'conflict'" in sql
    assert "manual_handoff_required boolean not null" in sql
    assert "activation_authorized = false" in sql
    assert "insert into public.recovery_cases" not in sql
    assert "insert into public.scheduled_actions" not in sql
    assert "insert into public.followup_sequences" not in sql
    assert "insert into public.outbound_messages" not in sql


def test_scope_maps_hotmart_product_to_intent_hotlink() -> None:
    sql = _sql()

    assert "'8104005'" in sql
    assert "'f106691755g'" in sql
    assert "'bxjge6zq'" in sql
    assert "'lancemos'" in sql
    assert "'psicologajohanna'" in sql
    assert "interval '24 hours'" in sql
    assert "300,\n    true" not in sql


def test_exact_event_rpc_validates_locked_payload_and_identity() -> None:
    sql = _sql()

    assert "function public.correlate_hotmart_purchase_intent" in sql
    assert "for update" in sql
    assert "hotmart_purchase_intent_payload_is_processable" in sql
    assert "purchase_approved" in sql
    assert "purchase_out_of_shopping_cart" in sql
    assert "normalized_email" in sql
    assert "normalized_phone" in sql
    assert "submitted_at >= v_observed_at - v_scope.max_lookback" in sql
    assert "submitted_at <= v_observed_at" in sql
    assert "+ interval '5 minutes'" not in sql
    assert "future_skew_seconds" not in sql.split(
        "create or replace function public.correlate_hotmart_purchase_intent", 1
    )[1].split("$function$;", 1)[0]
    assert "name" not in sql.split("create or replace function public.correlate_hotmart_purchase_intent", 1)[1].split("$function$;", 1)[0]


def test_conflict_and_ambiguity_block_candidates_fail_closed() -> None:
    sql = _sql()

    assert "current_classification = 'identity_conflict'" in sql
    assert "current_classification = 'tracking_incomplete'" in sql
    assert "email_phone_conflict" in sql
    assert "multiple_candidates" in sql
    assert "manual_handoff_required" in sql


def test_resolved_purchase_supersedes_abandonment_and_abandonment_never_authorizes() -> None:
    sql = _sql()

    assert "lifecycle_state = 'purchased'" in sql
    assert "current_classification = null" in sql
    assert "current_classification = 'abandonment_candidate'" in sql
    assert "where id = v_resolved_intent_id" in sql
    assert "activation_authorized = false" in sql


def test_atomic_admission_wrappers_correlate_canonical_identities() -> None:
    sql = _sql()

    assert "admit_and_correlate_hotmart_purchase_approved" in sql
    assert "admit_and_correlate_hotmart_cart_abandonment" in sql
    assert "p_normalized_email text" in sql
    assert "p_normalized_phone text" in sql
    assert "perform * from public.correlate_hotmart_purchase_intent(v_event_id)" in sql
    assert "create trigger webhook_events_correlate_purchase_intent" not in sql


def test_admitted_identity_is_bound_to_the_authoritative_payload() -> None:
    sql = _sql()
    helper = sql.split(
        "create or replace function public._admit_hotmart_purchase_intent_identity",
        1,
    )[1].split("$function$;", 1)[0]

    assert "from public.webhook_events" in helper
    assert "v_payload_email" in helper
    assert "v_payload_phone" in helper
    assert "hotmart_intent_identity_payload_mismatch" in helper
    assert "is distinct from" in helper


def test_expand_phase_keeps_legacy_signatures_as_safe_correlating_shims() -> None:
    sql = _sql()
    compact_sql = " ".join(sql.split())

    assert "alter function public.admit_hotmart_purchase_approved(text, jsonb) rename to _admit_hotmart_purchase_approved_base" in compact_sql
    assert "alter function public.admit_hotmart_cart_abandonment(text, jsonb) rename to _admit_hotmart_cart_abandonment_base" in compact_sql
    assert "create or replace function public.admit_hotmart_purchase_approved(" in sql
    assert "create or replace function public.admit_hotmart_cart_abandonment(" in sql
    assert "public.admit_and_correlate_hotmart_purchase_approved(" in sql
    assert "public.admit_and_correlate_hotmart_cart_abandonment(" in sql
    assert "grant execute on function public.admit_hotmart_purchase_approved(text, jsonb) to service_role" in sql
    assert "grant execute on function public.admit_hotmart_cart_abandonment(text, jsonb) to service_role" in sql


def test_sql_payload_normalization_matches_python_type_and_whitespace_rules() -> None:
    sql = _sql()
    normalizer = sql.split(
        "create or replace function public._normalize_hotmart_purchase_intent_phone",
        1,
    )[1].split("$function$;", 1)[0]
    identity = sql.split(
        "create or replace function public._hotmart_purchase_intent_payload_identity",
        1,
    )[1].split("$function$;", 1)[0]

    assert "btrim(p_value)" not in normalizer
    assert "jsonb_typeof" in identity
    assert "= 'string'" in identity
    assert "{data,buyer,phone}" in identity
    assert "{data,buyer,checkout_phone}" in identity


def test_rpc_and_tables_are_closed_to_api_roles() -> None:
    sql = _sql()

    for role in ("public", "anon", "authenticated"):
        assert f"from {role}" in sql
    assert "grant execute on function public.correlate_hotmart_purchase_intent(uuid) to service_role" in sql
    for table in (
        "hotmart_purchase_intent_scopes",
        "hotmart_purchase_intent_event_identities",
        "hotmart_purchase_intent_correlations",
        "hotmart_purchase_intent_correlation_candidates",
    ):
        assert f"revoke all on table public.{table} from service_role" in sql
    assert "grant execute on function public.admit_and_correlate_hotmart_purchase_approved" in sql
    assert "grant execute on function public.admit_and_correlate_hotmart_cart_abandonment" in sql
    assert "revoke all on function public._admit_hotmart_purchase_approved_base(text, jsonb) from service_role" in sql
    assert "revoke all on function public._admit_hotmart_cart_abandonment_base(text, jsonb) from service_role" in sql
