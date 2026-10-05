"""Shape guards for the 2026-10-05 migration and the bridge readers that go
with it: the recuperador's marker in the sck is separated with ~, like the ad's
sck, and the purchase admission accepts the composed sck.

Why: since 2026-09-25 the Lancemos standard separates the ad's sck with ~
(E10/E13), but the marker kept the | on purpose (20260925000100: "No se unifica
sin una decision nueva"). Dan decided it on 2026-10-05 (E46): only the separator
changes, the fields and their order do not.

    antes:  <sck del anuncio>|hermes|v1|<ulid>   o   hermes|v1|<ulid>
    ahora:  <sck del anuncio>~hermes~v1~<ulid>   o   hermes~v1~<ulid>

The readers accept both forms for good: the links already sent keep reaching
Hotmart with the |. On the way, admit_and_correlate_hotmart_checkout_issuance_v2
stops rejecting the composed sck: it only took the marker alone, so a purchase
made with a link that carried the ad's sck raised 22023 and the bridge answered
503 to Hotmart.

The behaviour is exercised against a real schema in
tests/sql/followup_engine/validate_johanna_checkout_issuance_v2.mjs (PGlite),
on the full chain; the migration was also applied on Johanna's partial chain
(up to 20260928000400 plus this one) when it was written. These tests pin the
parts of the file a later edit could silently drop, that the texts it replaces
are the ones in force, and the bridge readers against the sck that Hotmart
really returned (tests/fixtures/hotmart_purchase_sck_shapes_20261005.json).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bridge.checkout_issuance import CheckoutOffer, build_checkout_issuance
from bridge.supabase import _sck_carries_hermes_marker, sck_carries_hermes_issuance

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261005000100_sck_marker_uses_tilde_v1.sql"
WRITER_IN_FORCE = MIGRATIONS / "20260927000200_sck_length_accepts_255_v1.sql"
RECOGNIZER_IN_FORCE = MIGRATIONS / "20260925000100_sck_alphabet_accepts_tilde_v1.sql"
ADMISSION_IN_FORCE = MIGRATIONS / "20260914000100_johanna_checkout_issuance_v2.sql"
PORTABLE_DERIVATION = MIGRATIONS / "20261001000100_whatsapp_phone_equivalence.sql"
FIXTURE = ROOT / "tests" / "fixtures" / "hotmart_purchase_sck_shapes_20261005.json"
INVENTORY = ROOT / "scripts" / "supabase_schema_inventory.sql"

ULID = "01K5ABCDEFX2VYB4M6X9CDPTD9"
SIGNATURES = (
    "public.correlate_hotmart_checkout_issuance_v2(uuid,text,timestamptz)",
    "public.admit_and_correlate_hotmart_checkout_issuance_v2(text,jsonb,text,timestamptz)",
    "public.reserve_chatwoot_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamptz)",
    "public.reserve_portable_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamptz)",
)
WRITER_IN_FORCE_BLOCK = (
    "    if v_preserved_sck is not null then\n"
    "        v_sck_value := v_preserved_sck || '|hermes|v1|' || p_issuance_ulid;\n"
    "    else\n"
    "        v_sck_value := 'hermes|v1|' || p_issuance_ulid;\n"
    "    end if;"
)
RECOGNIZER_IN_FORCE_REGEX = (
    "'^([A-Za-z0-9._|~-]+[|])?hermes[|]v1[|][0-7][0-9A-HJKMNP-TV-Z]{25}$'"
)
ADMISSION_IN_FORCE_REGEX = "'^hermes[|]v1[|][0-7][0-9A-HJKMNP-TV-Z]{25}$'"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _function_body(sql: str, name: str) -> str:
    start = sql.index(f"function public.{name}(")
    return sql[start : sql.index("$function$;", start)]


def _sql_literals(fragment: str) -> str:
    """Concatenate the SQL string literals of a fragment, unescaping '' (and the
    \\n of the E'' ones, the only escape the migration uses)."""
    parts = []
    for prefix, literal in re.findall(r"(E?)'((?:[^']|'')*)'", fragment):
        literal = literal.replace("''", "'")
        if prefix:
            literal = literal.replace("\\n", "\n")
        parts.append(literal)
    return "".join(parts)


def _block(sql: str, start: str, end: str) -> str:
    begin = sql.index(start)
    return sql[begin : sql.index(end, begin)]


def _url_shape() -> re.Pattern[str]:
    block = _block(
        _sql(),
        "add constraint checkout_link_issuances_url_shape check (",
        ");",
    )
    return re.compile(_sql_literals(block))


def _marker_regex() -> re.Pattern[str]:
    block = _block(_sql(), "v_marker_new constant text :=", ";\n")
    literal = _sql_literals(block)
    assert literal.startswith("'^") and literal.endswith("$'")
    return re.compile(literal[1:-1])


def _ad_scks() -> list[str]:
    return [
        compra["sck"]
        for compra in _fixture()["compras"]
        if compra["sck"] and "~" in compra["sck"]
    ]


def test_migration_comes_right_after_the_inbound_kind_one_and_creates_nothing() -> None:
    versions = sorted(path.name.split("_", 1)[0] for path in MIGRATIONS.glob("*.sql"))
    posicion = versions.index("20261005000100")
    assert versions[posicion - 1] == "20261001000500"

    sql = _sql()
    assert sql.lstrip().startswith("-- Migration:")
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    # Nothing new to grant: the four functions are replaced in place (create or
    # replace keeps the grants) and the DO block is anonymous.
    assert "create function" not in sql
    assert "create or replace function" not in sql
    assert "drop function" not in sql
    assert "grant " not in sql
    assert "set local lock_timeout = '5s';" in sql


def test_the_four_functions_are_replaced_on_their_live_definition_and_fail_closed() -> None:
    sql = _sql()
    for signature in SIGNATURES:
        assert sql.count(f"('{signature}',") == 1
    # The shared reserve, the recognizer and the admission must exist; the
    # portable one only exists where 20261001000100 ran (ATT1, not Johanna).
    assert re.search(r"reserve_portable_checkout_issuance_v2\([^)]*\)',\s*v_writer_old, v_writer_new, false\)", sql)
    assert sql.count(", true)") == 3
    assert "pg_get_functiondef(v_target.signature::regprocedure)" in sql
    assert "<> length(v_target.old_text)" in sql
    assert "position(v_target.new_text in v_definition) > 0" in sql
    for message in (
        "sck_marker_function_missing",
        "unexpected_sck_marker_definition",
        "sck_marker_definition_not_replaced",
    ):
        assert f"message = '{message}'" in sql
    assert sql.count("errcode = '55000'") == 3
    assert "execute replace(v_definition, v_target.old_text, v_target.new_text);" in sql


def test_the_replaced_texts_are_the_ones_in_force() -> None:
    sql = _sql()
    writer_old = _sql_literals(_block(sql, "v_writer_old constant text :=", ";\n    v_writer_new"))
    assert writer_old == WRITER_IN_FORCE_BLOCK
    writer = _function_body(
        WRITER_IN_FORCE.read_text(encoding="utf-8"),
        "reserve_chatwoot_checkout_issuance_v2",
    )
    assert writer.count(WRITER_IN_FORCE_BLOCK) == 1

    recognizer_old = _sql_literals(_block(sql, "v_recognizer_old constant text :=", ";\n"))
    assert recognizer_old == RECOGNIZER_IN_FORCE_REGEX
    recognizer = _function_body(
        RECOGNIZER_IN_FORCE.read_text(encoding="utf-8"),
        "correlate_hotmart_checkout_issuance_v2",
    )
    assert recognizer.count(RECOGNIZER_IN_FORCE_REGEX) == 1

    admission_old = _sql_literals(_block(sql, "v_admission_old constant text :=", ";\n"))
    assert admission_old == ADMISSION_IN_FORCE_REGEX
    admission = _function_body(
        ADMISSION_IN_FORCE.read_text(encoding="utf-8"),
        "admit_and_correlate_hotmart_checkout_issuance_v2",
    )
    assert admission.count(ADMISSION_IN_FORCE_REGEX) == 1
    # Nothing between the definitions in force and this migration redefined
    # them: the texts above are the ones live in every base.
    for name, in_force in (
        ("admit_and_correlate_hotmart_checkout_issuance_v2", ADMISSION_IN_FORCE),
        ("correlate_hotmart_checkout_issuance_v2", RECOGNIZER_IN_FORCE),
        ("reserve_chatwoot_checkout_issuance_v2", WRITER_IN_FORCE),
    ):
        redefinition = re.compile(rf"create (?:or replace )?function public\.{name}\(")
        for path in MIGRATIONS.glob("*.sql"):
            if in_force.name < path.name < MIGRATION.name:
                assert not redefinition.search(path.read_text(encoding="utf-8")), path.name


def test_the_portable_derivation_does_not_count_the_marker() -> None:
    # If 20261001000100 runs after this one (Johanna, some day), it derives the
    # portable from the shared reserve that already writes ~. Its exact-count
    # checks must not look at the marker, and the replaced block must not carry
    # any of the texts it counts, or the counts would change. The behaviour on
    # Johanna's chain is exercised in validate_sck_marker_on_johanna_chain.mjs.
    derivation = _block(
        PORTABLE_DERIVATION.read_text(encoding="utf-8"),
        "do $reserve$",
        "$reserve$;",
    )
    assert "hermes" not in derivation
    sql = _sql()
    writer_old = _sql_literals(_block(sql, "v_writer_old constant text :=", ";\n    v_writer_new"))
    writer_new = _sql_literals(_block(sql, "v_writer_new constant text :=", ";\n    v_target"))
    for counted in (
        "reserve_chatwoot_checkout_issuance_v2",
        "intent.normalized_phone",
        "has_chatwoot_opt_out_stop",
        "_whatsapp_phone_",
    ):
        assert counted in derivation
        assert counted not in writer_old and counted not in writer_new


def test_the_writer_writes_the_marker_with_tilde() -> None:
    writer_new = _sql_literals(_block(_sql(), "v_writer_new constant text :=", ";\n    v_target"))
    assert "v_preserved_sck || '~hermes~v1~' || p_issuance_ulid;" in writer_new
    assert "v_sck_value := 'hermes~v1~' || p_issuance_ulid;" in writer_new
    assert "|hermes|v1|" not in writer_new
    # Only the marker block changes: the | of an old-lineage ad sck is still
    # encoded when the URL is composed, outside the replaced block.
    assert "replace(v_sck_value, '|', '%7C')" not in writer_new
    assert "replace(v_sck_value, '|', '%7C')" in WRITER_IN_FORCE.read_text(encoding="utf-8")


def test_the_value_check_accepts_both_markers() -> None:
    block = _block(
        _sql(),
        "add constraint checkout_link_issuances_sck_value_shape check (",
        ");",
    )
    for clause in (
        "sck_value = 'hermes|v1|' || issuance_ulid",
        "= '|hermes|v1|' || issuance_ulid",
        "sck_value = 'hermes~v1~' || issuance_ulid",
        "= '~hermes~v1~' || issuance_ulid",
    ):
        assert clause in block
    # Both suffix clauses cut the same length: the separator plus "hermes", the
    # separator, "v1" and the separator again.
    assert block.count("length(issuance_ulid) + 11") == 2


@pytest.mark.parametrize(
    ("sck_in_url", "accepted"),
    [
        (f"hermes%7Cv1%7C{ULID}", True),
        (f"fb.paid.120210000000000000%7Chermes%7Cv1%7C{ULID}", True),
        (f"hermes~v1~{ULID}", True),
        (f"fb~120253261784760514~120253261784680514~paid~120253261784670514~hermes~v1~{ULID}", True),
        (f"meta%7Clegacy%7Cvalue~hermes~v1~{ULID}", True),
        (f"meta-ads~~~LNC-DraNina-ATT-ATT2-LIVE~~hermes~v1~{ULID}", True),
        (f"hermes%7Cv1~{ULID}", False),
        (f"hermes~v1%7C{ULID}", False),
        (f"hermes|v1|{ULID}", False),
        (f"hermes~v1~{ULID}~tail", False),
        (f"~hermes~v1~{ULID}", False),
    ],
)
def test_the_url_check_accepts_both_markers_and_nothing_mixed(sck_in_url: str, accepted: bool) -> None:
    url = (
        "https://pay.hotmart.com/F106691755G?off=mgbgpp19&checkoutMode=10"
        f"&src=hermes&sck={sck_in_url}&fbclid=IwAR2tildeclickid"
    )
    assert (_url_shape().match(url) is not None) is accepted


@pytest.mark.parametrize(
    ("sck", "accepted"),
    [
        (f"hermes|v1|{ULID}", True),
        (f"meta|legacy|value|hermes|v1|{ULID}", True),
        (f"hermes~v1~{ULID}", True),
        (f"meta|legacy|value~hermes~v1~{ULID}", True),
        (f"meta-ads~~~LNC-DraNina-ATT-ATT2-LIVE~~hermes~v1~{ULID}", True),
        # The links sent between 2026-09-25 and this migration: the core's ~ in
        # the ad's part, the | in the marker.
        (f"fb~120253261784760514~paid|hermes|v1|{ULID}", True),
        (f"hermes|v1~{ULID}", False),
        (f"hermes~v1|{ULID}", False),
        (f"ad~x|hermes~v1~{ULID}", False),
        (f"ad|x~hermes|v1|{ULID}", False),
        (f"hermes~v1~{ULID}~tail", False),
        ("hermes~v1~nope", False),
        ("fb~120253261784760514~120253261784680514~paid~120253261784670514", False),
        # The shapes where the first version of the bridge regex (a search with
        # any prefix) and the base disagreed: the bridge sent them to the
        # admission, the base raised 22023 and Hotmart got a 503.
        (f"~hermes~v1~{ULID}", False),
        (f"|hermes|v1|{ULID}", False),
        (f"a b~hermes~v1~{ULID}", False),
        (f"a b|hermes|v1|{ULID}", False),
        (f"Niños~hermes~v1~{ULID}", False),
        (f"x%7Cy~hermes~v1~{ULID}", False),
        (f"x%7Cy|hermes|v1|{ULID}", False),
    ],
)
def test_the_sql_readers_and_the_bridge_agree(sck: str, accepted: bool) -> None:
    # The recognizer and the admission use this regex; the bridge decides with
    # _HERMES_SCK_TAIL which purchases go to them. The two sides must agree on
    # every shape, or a purchase is routed to an RPC that rejects it.
    assert (_marker_regex().fullmatch(sck) is not None) is accepted
    assert sck_carries_hermes_issuance(sck) is accepted


def test_the_sql_readers_and_the_bridge_agree_on_generated_shapes() -> None:
    # Same agreement on 20.000 shapes built from the pieces that matter: the
    # two separators, the marker's parts, valid and invalid ULIDs, and
    # characters inside and outside the ad's alphabet. Fixed seed.
    import random

    rng = random.Random(20261005)
    pieces = [
        "hermes", "v1", "~", "|", "%7C", " ", "ñ", "fb", "paid", "120210000000000000",
        ULID, "01K5ABCDEFX2VYB4M6X9CDPTZU", "", "-", ".", "_",
    ]
    tails = [f"hermes~v1~{ULID}", f"hermes|v1|{ULID}", f"hermes~v1|{ULID}", ""]
    regex = _marker_regex()
    disagreements = []
    for _ in range(20_000):
        head = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 6)))
        sck = head + rng.choice(["~", "|", ""]) + rng.choice(tails)
        if (regex.fullmatch(sck) is not None) != sck_carries_hermes_issuance(sck):
            disagreements.append(sck)
    assert disagreements == []


def test_the_bridge_reads_every_sck_hotmart_really_returned() -> None:
    fixture = _fixture()
    capture = fixture["_capture"]
    assert capture["captured_at"].startswith("2026-10-05")
    compras = fixture["compras"]
    scks = [compra["sck"] for compra in compras if compra["sck"]]
    # The counts in the capture's metadata are derived here, not trusted: what
    # the fixture proves is that Hotmart returned the ~ and the | literally.
    assert len(compras) == capture["conteo"]["compras"]
    assert sum("%7C" in sck or "%7E" in sck for sck in scks) == capture["conteo"]["con_7C_o_7E"] == 0
    ad_scks = _ad_scks()
    assert len(ad_scks) == capture["conteo"]["sck_del_core_con_tilde"]
    assert all(len(sck.split("~")) in (5, 6) for sck in ad_scks)

    hermes = [compra["sck"] for compra in compras if compra["sck"] and "hermes" in compra["sck"]]
    assert hermes == ["hermes|v1|01M3R0TZC1E78RR0AS4XQKNCCX"]
    for compra in compras:
        assert sck_carries_hermes_issuance(compra["sck"]) is (compra["sck"] in hermes)

    legacy_ulid = hermes[0].rsplit("|", 1)[1]
    assert _sck_carries_hermes_marker(hermes[0], legacy_ulid)
    emitted = fixture["emision_att1_20261005"]["sck_value"]
    assert _sck_carries_hermes_marker(emitted, emitted.rsplit("|", 1)[1])

    for ad_sck in ad_scks:
        for marker in (f"~hermes~v1~{ULID}", f"|hermes|v1|{ULID}"):
            composed = f"{ad_sck}{marker}"
            assert sck_carries_hermes_issuance(composed)
            assert _marker_regex().fullmatch(composed)
            assert _sck_carries_hermes_marker(composed, ULID)


def test_the_bridge_validator_rejects_what_the_reserve_never_writes() -> None:
    assert _sck_carries_hermes_marker(f"hermes~v1~{ULID}", ULID)
    assert _sck_carries_hermes_marker(f"hermes|v1|{ULID}", ULID)
    assert not _sck_carries_hermes_marker(f"hermes~v1~{ULID}", "01K5ABCDEFX2VYB4M6X9CDPTD8")
    assert not _sck_carries_hermes_marker(f"hermes|v1~{ULID}", ULID)
    assert not _sck_carries_hermes_marker(f"~hermes~v1~{ULID}", ULID)
    assert not _sck_carries_hermes_marker(f"Niños 23/09~hermes~v1~{ULID}", ULID)
    assert not _sck_carries_hermes_marker(f"{'a' * 256}~hermes~v1~{ULID}", ULID)
    assert _sck_carries_hermes_marker(f"{'a' * 255}~hermes~v1~{ULID}", ULID)


def test_the_python_builder_writes_the_same_marker_as_the_reserve() -> None:
    result = build_checkout_issuance(
        offer=CheckoutOffer(
            tenant_ref="lancemos",
            product_ref="F106691755G",
            landing_ref="ads-a",
            offer_code="bxjge6zq",
            checkout_base_url="https://pay.hotmart.com/F106691755G",
            checkout_mode=10,
        ),
        issuance_ulid=ULID,
    )
    assert result.sck_value == f"hermes~v1~{ULID}"
    assert result.final_url.endswith(f"&sck=hermes~v1~{ULID}")
    # The marker alone is the one the reserve writes when there is no ad sck.
    writer_new = _sql_literals(_block(_sql(), "v_writer_new constant text :=", ";\n    v_target"))
    assert "v_sck_value := 'hermes~v1~' || p_issuance_ulid;" in writer_new
    assert result.sck_value == "hermes~v1~" + ULID


def test_the_inventory_fingerprints_the_migration() -> None:
    inventory = INVENTORY.read_text(encoding="utf-8")
    block = _block(inventory, "'20261005000100',", "'sck_marker_uses_tilde'")
    assert "'20261005000100_sck_marker_uses_tilde_v1.sql'" in block
    for marker in (
        "checkout_link_issuances_sck_value_shape",
        "checkout_link_issuances_url_shape",
        "reserve_chatwoot_checkout_issuance_v2(",
        "reserve_portable_checkout_issuance_v2(",
        "correlate_hotmart_checkout_issuance_v2(",
        "admit_and_correlate_hotmart_checkout_issuance_v2(",
    ):
        assert marker in block
