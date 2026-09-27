"""Shape guards for the 2026-09-27 migration and the bridge guard that go with
it: the ad's sck may have up to 255 characters (200 until now).

Why: the Lancemos core v1.13.0 appends utm_id (Meta's 18-digit campaign id)
as a sixth positional field, `utm_source~utm_term~utm_content~utm_medium~
utm_campaign~utm_id` (standard E02, v0.14). The longest ad sck measured on
Hotmart sales has 177 characters (2026-09-24, 707 sck) and the id takes it to
196: still under 200, but at the edge, and any longer ad name would have been
dropped to null in silence (marker_only, no error).

The behaviour is exercised against a real schema in
tests/sql/followup_engine/validate_johanna_checkout_issuance_v2.mjs (PGlite):
the core's own six-field output observed in production on draninagarza, the
measured maximum brought to six fields (196), 255 preserved and 256 dropped.
These tests pin the parts of the file that a later edit could silently drop
and pin that the migration was written from the version in force
(20260925000100), not from an older copy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bridge.supabase import (
    _CHECKOUT_SAFE_SCK,
    _sck_carries_hermes_marker,
    sck_carries_hermes_issuance,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20260927000200_sck_length_accepts_255_v1.sql"
PREVIOUS = MIGRATIONS / "20260925000100_sck_alphabet_accepts_tilde_v1.sql"
FIXTURE = ROOT / "tests" / "fixtures" / "lancemos_core_sck_utm_id_20260927.json"
INVENTORY = ROOT / "scripts" / "supabase_schema_inventory.sql"
CONTRACT = ROOT / "docs" / "contracts" / "johanna-payment-link-v2.md"
VALIDATOR = ROOT / "tests" / "sql" / "followup_engine" / "validate_johanna_checkout_issuance_v2.mjs"

RESERVE_SIGNATURE = (
    "public.reserve_chatwoot_checkout_issuance_v2("
    "uuid, text, bigint, bigint, bigint, text, text, timestamptz)"
)
GUARD = "v_original_sck ~ '^[A-Za-z0-9._|~-]+$'"
CAP_OLD = "length(v_original_sck) <= 200"
CAP_NEW = "length(v_original_sck) <= 255"
ULID = "01K5ABCDEFX2VYB4M6X9CDPTD9"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_migration_comes_right_after_the_review_context_one_and_keeps_the_signature() -> None:
    versions = sorted(path.name.split("_", 1)[0] for path in MIGRATIONS.glob("*.sql"))
    assert versions[-1] == "20260927000200"
    assert versions[-2] == "20260927000100"

    sql = _sql()
    assert sql.count("create or replace function public.reserve_chatwoot_checkout_issuance_v2(") == 1
    # The purchase recognizer and the URL check have no length cap: untouched.
    assert "create or replace function public.correlate_hotmart_checkout_issuance_v2(" not in sql
    assert "checkout_link_issuances_url_shape" not in sql
    assert "drop function" not in sql
    assert f"revoke all on function {RESERVE_SIGNATURE} from public;" in sql
    assert sql.rstrip().endswith("commit;")


def test_only_the_cap_changes_and_it_stays_outside_the_regex() -> None:
    sql = _sql()
    assert sql.count(CAP_NEW) == 1
    assert CAP_OLD not in sql
    # Postgres caps regex repetitions at 255: the bound lives in length(), not
    # in the pattern, on both sides.
    assert "{1,255}" not in sql and "{1,200}" not in sql
    assert sql.count(GUARD) == 1
    assert "[A-Za-z0-9._|-]" not in sql
    assert "utm_id" in sql


def test_the_body_comes_from_the_version_in_force() -> None:
    previous = PREVIOUS.read_text(encoding="utf-8")
    current = _sql()
    for anchor in (
        "where link.purchase_intent_id = coalesce(v_lead_intent.id, v_intent.id)",
        "v_submission.raw_payload #>> '{data,attribution,sck}'",
        "v_submission.raw_payload #>> '{data,attribution,fbclid}'",
        "when v_preserved_sck is not null and v_lead_fbclid is not null then 'full'",
        "v_dropped_unsafe := case",
        "v_sck_value := v_preserved_sck || '|hermes|v1|' || p_issuance_ulid;",
        "'&src=hermes&sck=' || replace(v_sck_value, '|', '%7C')",
        GUARD,
    ):
        assert anchor in previous
        assert anchor in current
    assert CAP_OLD in previous and CAP_NEW in current
    # Everything of the function after the guard is byte-identical.
    tail_previous = previous[previous.index(CAP_OLD) :]
    tail_previous = tail_previous[: tail_previous.index("$function$;")]
    tail_current = current[current.index(CAP_NEW) :]
    tail_current = tail_current[: tail_current.index("$function$;")]
    assert tail_previous.replace(CAP_OLD, CAP_NEW) == tail_current


def test_the_function_stays_locked_down() -> None:
    sql = _sql()
    assert sql.count("security definer") == 1
    assert sql.count("set search_path = pg_catalog, public, pg_temp") == 1
    assert f"grant execute on function {RESERVE_SIGNATURE} to service_role" in sql
    for role in ("anon", "authenticated"):
        assert f"revoke all on function {RESERVE_SIGNATURE} from {role}" in sql
    assert "protect_checkout_link_issuance" not in sql


def test_the_bridge_guard_moves_with_the_migration() -> None:
    assert _CHECKOUT_SAFE_SCK.pattern == r"[A-Za-z0-9._|~-]{1,255}"
    at_cap = "a" * 236 + "~120210000000000001"
    over_cap = "a" * 237 + "~120210000000000001"
    assert len(at_cap) == 255 and len(over_cap) == 256
    assert _sck_carries_hermes_marker(f"{at_cap}|hermes|v1|{ULID}", ULID)
    assert not _sck_carries_hermes_marker(f"{over_cap}|hermes|v1|{ULID}", ULID)
    # The marker alone and the purchase-side recognizer do not change.
    assert _sck_carries_hermes_marker(f"hermes|v1|{ULID}", ULID)
    assert sck_carries_hermes_issuance(f"{at_cap}|hermes|v1|{ULID}")


def test_the_fixture_is_the_core_s_own_six_field_output_and_the_guards_accept_it() -> None:
    fixture = _fixture()
    assert "lancemos/core v1.13.0" in fixture["_capture"]["origen"]
    assert fixture["_capture"]["captured_at"].startswith("2026-09-27")
    assert fixture["_capture"]["largos_medidos"]["hotmart_ventas_sck_maximo"] == 177
    assert fixture["_capture"]["largos_medidos"]["maximo_con_utm_id"] == 196

    with_id, without_id = fixture["casos"]
    fields = with_id["sck"].split("~")
    assert len(fields) == 6 and re.fullmatch(r"[0-9]{18}", fields[5])
    assert without_id["sck"].split("~") == fields[:5]
    assert not without_id["sck"].endswith("~")

    synthetic = fixture["sintetico_196"]["sck"]
    assert len(synthetic) == 196
    assert len(synthetic.rsplit("~", 1)[0]) == 177
    guard = re.compile(r"^[A-Za-z0-9._|~-]+$")
    for sck in (with_id["sck"], without_id["sck"], synthetic):
        assert guard.fullmatch(sck), sck
        assert _CHECKOUT_SAFE_SCK.fullmatch(sck), sck
        assert _sck_carries_hermes_marker(f"{sck}|hermes|v1|{ULID}", ULID)


def test_inventory_contract_and_validator_know_the_new_cap() -> None:
    inventory = INVENTORY.read_text(encoding="utf-8")
    assert "'20260927000200_sck_length_accepts_255_v1.sql'" in inventory
    assert "position('length(v_original_sck) <= 255' in definition) > 0" in inventory
    assert "'sck_length_accepts_255_reader'" in inventory

    contract = CONTRACT.read_text(encoding="utf-8")
    assert "be at most 255 characters" in contract
    assert "be at most 200 characters" not in contract

    validator = VALIDATOR.read_text(encoding="utf-8")
    assert "lancemos_core_sck_utm_id_20260927.json" in validator
    assert "'a'.repeat(236)" in validator and "'a'.repeat(237)" in validator
