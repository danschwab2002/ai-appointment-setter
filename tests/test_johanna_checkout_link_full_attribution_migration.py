"""Shape guards for the 2026-09-22 migration: the recuperador link carries the
lead's fbclid and keeps the ad's sck in front of the hermes marker.

The behaviour is exercised against a real schema in
tests/sql/followup_engine/validate_johanna_checkout_issuance_v2.mjs (PGlite).
These tests pin the parts of the file that a later edit could silently drop.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20260922000200_johanna_checkout_link_full_attribution_v1.sql"

RESERVE_SIGNATURE = (
    "public.reserve_chatwoot_checkout_issuance_v2("
    "uuid, text, bigint, bigint, bigint, text, text, timestamptz)"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_is_the_newest_and_keeps_the_rpc_signature() -> None:
    versions = sorted(path.name.split("_", 1)[0] for path in MIGRATIONS.glob("*.sql"))
    assert versions[-1] == "20260922000200"

    sql = _sql()
    # Same name and same signature: the bridge does not change and no redeploy
    # is needed to pick this up.
    assert "create or replace function public.reserve_chatwoot_checkout_issuance_v2(" in sql
    assert "create function public.reserve_chatwoot_checkout_issuance_v2(" not in sql
    assert f"revoke all on function {RESERVE_SIGNATURE} from public;" in sql
    assert sql.rstrip().endswith("commit;")


def test_the_row_records_what_it_could_compose() -> None:
    sql = _sql()
    assert "add column attribution_resolution text not null default 'marker_only'" in sql
    for value in ("'full'", "'sck_only'", "'fbclid_only'", "'marker_only'"):
        assert value in sql
    assert "add column dropped_unsafe_fields text" in sql
    assert "'sck', 'fbclid', 'sck,fbclid'" in sql


def test_the_new_columns_are_immutable_like_the_rest_of_the_row() -> None:
    sql = _sql()
    assert (
        "or old.attribution_resolution is distinct from new.attribution_resolution"
        in sql
    )
    assert "or old.dropped_unsafe_fields is distinct from new.dropped_unsafe_fields" in sql


def test_the_ad_sck_goes_first_and_whole() -> None:
    sql = _sql()
    # The ad's sck is the prefix, the hermes marker the suffix: a parser that
    # splits on "|" finds the original in the first field.
    assert "v_sck_value := v_preserved_sck || '|hermes|v1|' || p_issuance_ulid;" in sql
    assert "v_sck_value := 'hermes|v1|' || p_issuance_ulid;" in sql
    # The stored sck is the one that travelled, not just the marker.
    assert "'hermes', 'v1', v_sck_value," in sql
    assert "'hermes', 'v1', 'hermes|v1|' || p_issuance_ulid," not in sql
    assert "checkout_link_issuances_sck_value_shape" in sql


def test_only_url_safe_values_are_propagated() -> None:
    sql = _sql()
    # Postgres caps regex repetition counts at 255, so the length is checked
    # separately instead of inside the pattern.
    assert "length(v_original_sck) <= 200" in sql
    assert "v_original_sck ~ '^[A-Za-z0-9._|-]+$'" in sql
    assert "length(v_lead_fbclid) > 512" in sql
    assert "v_lead_fbclid !~ '^[A-Za-z0-9._-]+$'" in sql
    assert "{1,200}" not in sql and "{1,512}" not in sql and "{1,600}" not in sql


def test_the_fbclid_is_optional_and_carries_no_personal_data() -> None:
    sql = _sql()
    assert "v_url := v_url || '&fbclid=' || v_lead_fbclid;" in sql
    assert "(&fbclid=[A-Za-z0-9._-]+)?$" in sql
    # The prefill stays out by contract: no buyer identity in the query string.
    for personal in ("'&email='", "'&phone='", "'&name='", "'&phonenumber='"):
        assert personal not in sql


def test_the_attribution_is_the_lead_s_even_when_the_offer_is_uncatalogued() -> None:
    sql = _sql()
    # When the lead's landing/offer pair is not in the catalog the RPC builds a
    # synthetic intent, which has no submission behind it. The sck and fbclid
    # belong to the lead, so they are read from the lead's own intent.
    assert (
        "where link.purchase_intent_id = coalesce(v_lead_intent.id, v_intent.id)" in sql
    )


def test_the_function_stays_locked_down() -> None:
    sql = _sql()
    # reserve and correlate are security definer; protect is security invoker.
    assert sql.count("security definer") == 2
    assert sql.count("security invoker") == 1
    assert sql.count("set search_path = pg_catalog, public, pg_temp") == 3
    assert f"grant execute on function {RESERVE_SIGNATURE} to service_role" in sql
    for role in ("anon", "authenticated"):
        assert f"revoke all on function {RESERVE_SIGNATURE} from {role}" in sql
