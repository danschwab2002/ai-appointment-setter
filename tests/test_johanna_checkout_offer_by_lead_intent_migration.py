"""Shape guards for the 2026-09-22 migration: the recuperador link carries the
offer the lead saw.

The behaviour is exercised against a real schema in
tests/sql/followup_engine/validate_johanna_checkout_issuance_v2.mjs (PGlite).
These tests pin the parts of the file that a later edit could silently drop.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20260922000100_johanna_checkout_offer_by_lead_intent_v1.sql"
V2_MIGRATION = MIGRATIONS / "20260914000100_johanna_checkout_issuance_v2.sql"

RESERVE_SIGNATURE = (
    "public.reserve_chatwoot_checkout_issuance_v2("
    "uuid, text, bigint, bigint, bigint, text, text, timestamptz)"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_exists_once_and_replaces_the_v2_reserve_rpc() -> None:
    versions = [path.name.split("_", 1)[0] for path in MIGRATIONS.glob("*.sql")]
    # Which file is the newest is pinned by test_supabase_release_readiness.py;
    # here what matters is that this migration is present exactly once.
    assert versions.count("20260922000100") == 1
    sql = _sql()
    assert "create or replace function public.reserve_chatwoot_checkout_issuance_v2(" in sql
    # Same signature as V2: the bridge client is untouched.
    v2 = V2_MIGRATION.read_text(encoding="utf-8")
    signature_start = v2.index("create function public.reserve_chatwoot_checkout_issuance_v2(")
    signature_end = v2.index("language plpgsql", signature_start)
    v2_signature = v2[signature_start:signature_end].replace("create function", "create or replace function")
    assert v2_signature in sql


def test_issuance_records_how_the_offer_was_resolved() -> None:
    sql = _sql()
    assert "add column offer_resolution text not null default 'catalog_default'" in sql
    for value in (
        "'lead_intent'",
        "'default_no_intent'",
        "'default_offer_not_in_catalog'",
        "'catalog_default'",
    ):
        assert value in sql
    assert "add column lead_offer_code text" in sql
    # Both columns enter the immutability trigger.
    assert "old.offer_resolution is distinct from new.offer_resolution" in sql
    assert "old.lead_offer_code is distinct from new.lead_offer_code" in sql
    assert "checkout_url_final, offer_resolution, lead_offer_code" in sql


def test_catalog_is_seeded_from_the_published_pairs_and_keeps_one_default() -> None:
    sql = _sql()
    assert "from public.johanna_precheckout_landing_offers pair" in sql
    # The five new rows are never default; ads-a is not inserted twice.
    assert "'https://pay.hotmart.com/F106691755G', 10, false, 'active'," in sql
    assert "and existing.offer_code = pair.offer_ref" in sql
    assert "message = 'johanna_precheckout_landing_offers_expected_six'" in sql
    assert "message = 'johanna_checkout_offer_catalog_expected_six_with_ads_a_default'" in sql
    # No offer code is typed by hand in this file except the default guard.
    for typed in ("'mgbgpp19'", "'s1qfxm7m'", "'jtt6fcsm'", "'ecyu87q0'", "'ulhzpw9a'"):
        assert typed not in sql


def test_offer_comes_from_the_lead_intent_with_default_fallback() -> None:
    sql = _sql()
    assert "select intent.* into v_lead_intent" in sql
    assert "and offer.landing_ref = v_lead_intent.landing_ref" in sql
    assert "and offer.offer_code = v_lead_intent.offer_ref" in sql
    assert "v_offer_resolution := 'lead_intent'" in sql
    assert "v_offer_resolution := 'default_offer_not_in_catalog'" in sql
    assert "v_offer_resolution := 'default_no_intent'" in sql
    # Real precheckout intents win over intents fabricated for inbound links.
    assert "select 1 from public.purchase_intent_submissions link" in sql
    assert "        ) desc,\n        intent.submitted_at desc," in sql


def test_known_purchase_guard_covers_every_offer_of_the_product() -> None:
    sql = _sql()
    guard_start = sql.index("-- A known purchase of the product blocks")
    guard_end = sql.index("return query select 'purchase_already_approved'", guard_start)
    guard = sql[guard_start:guard_end]
    assert "intent.lifecycle_state = 'purchased'" in guard
    assert "offer_ref" not in guard
    assert "landing_ref" not in guard


def test_rpc_stays_definer_and_service_role_only() -> None:
    sql = _sql()
    assert sql.count("security definer") == 1
    assert sql.count("set search_path = pg_catalog, public, pg_temp") == 2
    assert f"revoke all on function {RESERVE_SIGNATURE} from public;" in sql
    assert f"revoke all on function {RESERVE_SIGNATURE} from anon" in sql
    assert f"revoke all on function {RESERVE_SIGNATURE} from authenticated" in sql
    assert f"grant execute on function {RESERVE_SIGNATURE} to service_role" in sql
    assert sql.strip().endswith("commit;")
