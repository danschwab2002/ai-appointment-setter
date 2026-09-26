"""Shape guards for the 2026-09-25 migration: the ad's sck accepts "~", the
separator the Lancemos core composes with since v1.10.0 (E10/E13).

The behaviour is exercised against a real schema in
tests/sql/followup_engine/validate_johanna_checkout_issuance_v2.mjs (PGlite),
with the core's own expected outputs captured in
tests/fixtures/lancemos_core_sck_tilde_20260925.json. These tests pin the parts
of the file that a later edit could silently drop, and pin that the migration
was written from the version in force (20260922000200), not from the original
(20260914000100).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20260925000100_sck_alphabet_accepts_tilde_v1.sql"
PREVIOUS = MIGRATIONS / "20260922000200_johanna_checkout_link_full_attribution_v1.sql"
FIXTURE = ROOT / "tests" / "fixtures" / "lancemos_core_sck_tilde_20260925.json"

RESERVE_SIGNATURE = (
    "public.reserve_chatwoot_checkout_issuance_v2("
    "uuid, text, bigint, bigint, bigint, text, text, timestamptz)"
)
CORRELATE_SIGNATURE = (
    "public.correlate_hotmart_checkout_issuance_v2(uuid, text, timestamptz)"
)

GUARD_OLD = "v_original_sck ~ '^[A-Za-z0-9._|-]+$'"
GUARD_NEW = "v_original_sck ~ '^[A-Za-z0-9._|~-]+$'"
RECOGNIZER_OLD = "'^([A-Za-z0-9._|-]+[|])?hermes[|]v1[|][0-7][0-9A-HJKMNP-TV-Z]{25}$'"
RECOGNIZER_NEW = "'^([A-Za-z0-9._|~-]+[|])?hermes[|]v1[|][0-7][0-9A-HJKMNP-TV-Z]{25}$'"
URL_SHAPE_OLD = "'&sck=([A-Za-z0-9._%-]+%7C)?hermes%7Cv1%7C'"
URL_SHAPE_NEW = "'&sck=([A-Za-z0-9._%~-]+%7C)?hermes%7Cv1%7C'"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_follows_the_reason_sentences_and_keeps_both_rpc_signatures() -> None:
    versions = sorted(path.name.split("_", 1)[0] for path in MIGRATIONS.glob("*.sql"))
    # Fue la cola de la cadena hasta el 2026-09-25; desde la 20260925000200
    # (aviso a Slack de cada derivacion) ya no lo es, pero sigue viniendo justo
    # despues de la 20260924000200.
    assert "20260925000100" in versions
    assert versions[versions.index("20260925000100") - 1] == "20260924000200"

    sql = _sql()
    # Same names and same signatures: the bridge keeps calling what it calls.
    assert "create or replace function public.reserve_chatwoot_checkout_issuance_v2(" in sql
    assert "create or replace function public.correlate_hotmart_checkout_issuance_v2(" in sql
    assert "drop function" not in sql
    assert f"revoke all on function {RESERVE_SIGNATURE} from public;" in sql
    assert f"revoke all on function {CORRELATE_SIGNATURE} from public;" in sql
    assert sql.rstrip().endswith("commit;")


def test_the_three_regexes_accept_the_tilde_and_nothing_else_new() -> None:
    sql = _sql()
    assert sql.count(GUARD_NEW) == 1
    assert sql.count(RECOGNIZER_NEW) == 1
    assert sql.count(URL_SHAPE_NEW) == 1
    # The old alphabet must be gone from this file entirely: a stray copy would
    # keep dropping the tilde on one of the three paths.
    assert GUARD_OLD not in sql
    assert RECOGNIZER_OLD not in sql
    assert URL_SHAPE_OLD not in sql
    assert "[A-Za-z0-9._|-]" not in sql
    assert "[A-Za-z0-9._%-]" not in sql
    # The length cap stays outside the pattern (Postgres caps repetitions at 255).
    assert "length(v_original_sck) <= 200" in sql
    assert "{1,200}" not in sql


def test_the_hermes_marker_and_the_src_do_not_change() -> None:
    sql = _sql()
    # The recuperador's marker keeps "|": the ad's "~" and the marker's "|" are
    # what make the two segments distinguishable. Decision of 2026-09-25.
    assert "v_sck_value := v_preserved_sck || '|hermes|v1|' || p_issuance_ulid;" in sql
    assert "v_sck_value := 'hermes|v1|' || p_issuance_ulid;" in sql
    assert "hermes[|]v1[|]" in sql
    assert "hermes~v1" not in sql
    assert "'&src=hermes&sck=' || replace(v_sck_value, '|', '%7C')" in sql
    # "~" is unreserved: nothing encodes it on the way to the URL.
    assert "replace(v_sck_value, '~'" not in sql


def test_the_url_check_is_recreated_not_appended() -> None:
    sql = _sql()
    assert "drop constraint checkout_link_issuances_url_shape;" in sql
    assert "add constraint checkout_link_issuances_url_shape check (" in sql
    assert sql.count("checkout_link_issuances_url_shape") == 2


def test_the_functions_stay_locked_down() -> None:
    sql = _sql()
    assert sql.count("security definer") == 2
    assert sql.count("set search_path = pg_catalog, public, pg_temp") == 2
    assert f"grant execute on function {RESERVE_SIGNATURE} to service_role" in sql
    for role in ("anon", "authenticated"):
        assert f"revoke all on function {RESERVE_SIGNATURE} from {role}" in sql
    # The immutability trigger is not this migration's business.
    assert "protect_checkout_link_issuance" not in sql


def test_the_bodies_come_from_the_version_in_force() -> None:
    # Everything the previous migration says about the two functions, this one
    # says too, except the three regexes: the diff is the alphabet and nothing
    # else. This is what catches a rewrite from the original 20260914000100.
    previous = PREVIOUS.read_text(encoding="utf-8")
    current = _sql()
    for anchor in (
        "where link.purchase_intent_id = coalesce(v_lead_intent.id, v_intent.id)",
        "v_submission.raw_payload #>> '{data,attribution,sck}'",
        "v_submission.raw_payload #>> '{data,attribution,fbclid}'",
        "when v_preserved_sck is not null and v_lead_fbclid is not null then 'full'",
        "v_dropped_unsafe := case",
        "return query select 'invalid_hermes_sck'::text,",
        "set status = 'purchase_matched',",
    ):
        assert anchor in previous
        assert anchor in current
    assert GUARD_OLD in previous and GUARD_NEW in current
    assert RECOGNIZER_OLD in previous and RECOGNIZER_NEW in current
    assert URL_SHAPE_OLD in previous and URL_SHAPE_NEW in current


def test_the_fixture_is_the_core_s_own_output_and_the_regexes_accept_it() -> None:
    import re

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "lancemos/core" in fixture["_capture"]["origen"]
    assert fixture["_capture"]["captured_at"].startswith("2026-09-25")
    guard = re.compile(r"^[A-Za-z0-9._|~-]+$")
    old_guard = re.compile(r"^[A-Za-z0-9._|-]+$")
    for caso in fixture["casos"]:
        assert "~" in caso["sck"]
        assert guard.fullmatch(caso["sck"]), caso
        # And the old alphabet would have dropped every one of them.
        assert old_guard.fullmatch(caso["sck"]) is None, caso
    for caso in fixture["casos_sin_tilde_que_siguen_vigentes"]:
        assert guard.fullmatch(caso["sck"]) and old_guard.fullmatch(caso["sck"])
