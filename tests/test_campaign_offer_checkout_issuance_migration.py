"""La forma de 20261110000500 (la oferta de la campana en el link del agente, E-410).

Lo que hace la reserva lo prueba el caso 15 de
tests/sql/followup_engine/validate_att1_portable_chain.mjs contra la cadena
completa de migraciones; aca se fija la forma: de donde se deriva, que aborta si
la fuente cambio, los grants, el check y la columna en el trigger, y los
inventarios.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261110000500_campaign_offer_checkout_issuance_v1.sql"
SIGNATURE = (
    "public.reserve_portable_checkout_issuance_for_offer_v1"
    "(uuid,text,bigint,bigint,bigint,text,text,text,text,text,timestamptz)"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def test_is_the_last_migration_and_one_transaction() -> None:
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # El caso se cierra cuando su ultimo toque termina sin salir (20261111000100, E-423): una funcion privada y el envoltorio.
    # Un pago pendiente que se cancela no clasifica la intencion (20261111000200, E-423): una rama del correlador portable.
    assert names[-2] == "20261111000200_pending_cancel_keeps_first_contact_v1.sql"
    # Los numeros del tablero (20261112200000, E-507): una lectura nueva.
    assert names[-1] == "20261112200000_read_results_v1.sql"
    assert names[-3] == "20261111000100_close_recovery_case_without_pending_touch_v1.sql"
    assert names[-4] == MIGRATION.name
    sql = _sql()
    assert sql.lstrip().startswith("-- Migration:")
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")


def test_derives_from_the_live_funnel_reserve_with_counted_replacements() -> None:
    code = _code(_sql())
    assert (
        "pg_get_functiondef(\n        'public.reserve_portable_checkout_issuance_for_funnel_v1"
        "(uuid,text,bigint,bigint,bigint,text,text,text,timestamptz)'::regprocedure"
    ) in code
    # Cada ancla se cuenta antes de reemplazar: si la de funnel cambio, aborta.
    for anchor in (
        "v_name", "v_head", "v_params", "v_invalid", "v_replay", "v_resolution",
        "v_insert_columns", "v_insert_values",
    ):
        assert (
            f"length(v_definition) - length(replace(v_definition, {anchor}, ''))\n"
            f"          <> length({anchor})"
        ) in code, anchor
    assert "unexpected_portable_checkout_issuance_for_funnel_definition" in code
    # No pisa la de funnel: crea una funcion con otro nombre, sin "or replace".
    assert "'CREATE FUNCTION public.reserve_portable_checkout_issuance_for_offer_v1('" in code
    assert "portable_checkout_issuance_for_offer_reserve_not_created" in code


def test_the_offer_is_the_campaign_one_and_never_falls_back_to_the_default() -> None:
    code = _code(_sql())
    resolution = code.split("v_offer_resolution_block constant text := $new$", 1)[1].split("$new$", 1)[0]
    assert "and offer.offer_code = p_offer_code" in resolution
    assert "'blocked_campaign_offer'" in resolution
    assert "v_offer_resolution := 'campaign';" in resolution
    # D2: sin la oferta no hay caida a la por defecto; D3: la intencion no la reemplaza.
    assert "v_default_offer;" not in resolution
    assert "v_lead_intent.landing_ref" not in resolution
    # D12: el replay exige la misma oferta y la misma campana.
    assert "and replay_offer.offer_code = p_offer_code" in code
    assert "or v_issuance.campaign_ref is distinct from p_campaign_ref" in code


def test_the_row_keeps_the_campaign_key_and_it_is_immutable() -> None:
    code = _code(_sql())
    assert "add column campaign_ref text;" in code
    assert "'recovery_case_offer', 'campaign'" in code
    assert "(offer_resolution = 'campaign') = (campaign_ref is not null)" in code
    assert "campaign_ref ~ '^[a-z0-9][a-z0-9-]{0,127}$'" in code
    assert "'public.protect_checkout_link_issuance()'::regprocedure" in code
    assert "or old.campaign_ref is distinct from new.campaign_ref" in code
    assert "unexpected_protect_checkout_link_issuance_definition" in code


def test_only_service_role_executes_it() -> None:
    code = _code(_sql())
    roles = code.split("do $roles$", 1)[1]
    assert f"'{SIGNATURE}'" in roles
    assert "revoke all on function %s from public" in roles
    assert "if to_regrole('anon') is not null then" in roles
    assert "grant execute on function %s to service_role" in roles
    assert "portable_checkout_issuance_for_offer_acl_unexpected" in roles


def test_the_inventories_know_it() -> None:
    schema = (ROOT / "scripts" / "supabase_schema_inventory.sql").read_text(encoding="utf-8")
    acl = (ROOT / "scripts" / "supabase_acl_inventory.sql").read_text(encoding="utf-8")
    hardening = (
        ROOT / "tests" / "sql" / "followup_engine" / "validate_acl_hardening.mjs"
    ).read_text(encoding="utf-8")
    fingerprint = schema.split("'20261110000500'", 1)[1].split("select\n    version,", 1)[0]
    assert "'20261110000500_campaign_offer_checkout_issuance_v1.sql'" in fingerprint
    assert "'campaign_offer_checkout_issuance'" in fingerprint
    assert "v_offer_resolution := ''campaign''" in fingerprint
    assert (
        "public.reserve_portable_checkout_issuance_for_offer_v1(uuid, text, bigint, bigint, "
        "bigint, text, text, text, text, text, timestamp with time zone)"
    ) in acl
    assert (
        "reserve_portable_checkout_issuance_for_offer_v1(uuid,text,bigint,bigint,bigint,"
        "text,text,text,text,text,timestamp with time zone)"
    ) in hardening
