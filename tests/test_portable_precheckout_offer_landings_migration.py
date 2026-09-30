"""Migracion 20260930000200: el formulario se admite en cada landing del binding.

admit_portable_observed_lead_precheckout se copia de su definicion vigente. Este
test verifica que la copia sea exacta salvo los bloques marcados ``offer_landing``
y el reemplazo de los campos de la oferta por defecto (``v_binding.<campo>``) por
los de la oferta del envio (``v_offer_*``), y que el helper del check quede
privado. El comportamiento se prueba en
tests/sql/followup_engine/validate_commercial_ally_portable_precheckout.mjs.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20260930000200_portable_precheckout_offer_landings.sql"
VIGENT = MIGRATIONS / "20260901000200_commercial_ally_portable_precheckout.sql"
ADMISSION = "admit_portable_observed_lead_precheckout"
ADMISSION_SIGNATURE = f"public.{ADMISSION}(text,text,integer,text,jsonb,jsonb)"
HELPER = "commercial_ally_offer_landings_are_valid"
HELPER_SIGNATURE = f"public.{HELPER}(jsonb,text[],text,text)"
CONSTRAINT = "commercial_ally_runtime_bindings_offer_landings_shape"
MARKED_BLOCK = re.compile(
    r"[ \t]*-- offer_landing: begin\n.*?-- offer_landing: end\n(?:\n)?",
    re.DOTALL,
)
# La oferta del envio reemplaza a la por defecto en cada comparacion, lock,
# busqueda e insert. Nada mas cambia.
OFFER_FIELDS = {
    "v_offer_code": "v_binding.offer_code",
    "v_offer_landing_id": "v_binding.lead_landing_id",
    "v_offer_page_host": "v_binding.lead_page_host",
    "v_offer_page_path": "v_binding.lead_page_path",
    "v_offer_site": "v_binding.lead_site",
}


def _function(sql: str, name: str) -> str:
    pattern = re.compile(
        rf"create\s+(?:or\s+replace\s+)?function\s+public\.{name}\s*\(.*?\$function\$;",
        re.DOTALL,
    )
    matches = pattern.findall(sql)
    assert len(matches) == 1, f"expected one definition of {name}"
    return matches[0]


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


def test_the_copied_definition_is_the_vigent_one() -> None:
    later = [
        path.name
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if VIGENT.name < path.name < MIGRATION.name
        and f"function public.{ADMISSION}(" in path.read_text(encoding="utf-8")
    ]

    assert later == []


def test_admission_is_the_vigent_definition_with_the_offer_of_the_submission() -> None:
    vigent = _function(VIGENT.read_text(encoding="utf-8"), ADMISSION)
    new = _function(MIGRATION.read_text(encoding="utf-8"), ADMISSION)

    assert len(MARKED_BLOCK.findall(new)) == 2
    stripped = MARKED_BLOCK.sub("", new)
    for field in OFFER_FIELDS.values():
        assert field not in stripped, field
    for offer_variable, binding_field in OFFER_FIELDS.items():
        stripped = re.sub(rf"\b{offer_variable}\b", binding_field, stripped)
    stripped = stripped.replace(
        f"create or replace function public.{ADMISSION}(",
        f"create function public.{ADMISSION}(",
        1,
    )

    assert _normalized(stripped) == _normalized(vigent)


def test_the_offer_is_resolved_after_locking_the_binding_and_before_any_check() -> None:
    new = _function(MIGRATION.read_text(encoding="utf-8"), ADMISSION)
    resolution = MARKED_BLOCK.findall(new)[1]
    compact = _normalized(resolution)

    assert new.index("for update;") < new.index(resolution)
    assert new.index(resolution) < new.index("v_contract_version := ")
    assert "v_offer_code := p_canonical_payload #>> '{commerce,offer_ref}';" in compact
    assert "if v_offer_code = v_binding.offer_code then" in compact
    assert "from jsonb_array_elements(v_binding.additional_offer_landings) landing" in compact
    assert "and v_offer_code = any(v_binding.additional_offer_codes);" in compact
    assert "if not found then v_offer_code := null;" in compact


def test_the_check_helper_is_a_private_immutable_invoker() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    helper = _normalized(_function(sql, HELPER))
    compact = _normalized(sql)

    assert "language plpgsql immutable security invoker" in helper
    assert "set search_path = pg_catalog, public, pg_temp" in helper
    assert f"revoke all on function {HELPER_SIGNATURE} from public;" in compact
    assert (
        f"execute format('revoke all on function {HELPER_SIGNATURE} from %I',v_role);"
        in compact
    )
    assert "rolname in ('anon','authenticated','service_role')" in compact
    executable = re.sub(r"--[^\n]*", "", sql)
    assert not re.search(rf"\bgrant\b[^;]*{HELPER}", executable)
    assert re.search(
        rf"grant execute on function {re.escape(ADMISSION_SIGNATURE)} to service_role;",
        executable,
    )


def test_the_column_defaults_to_empty_and_the_check_uses_the_helper() -> None:
    compact = _normalized(MIGRATION.read_text(encoding="utf-8"))

    assert (
        "alter table public.commercial_ally_runtime_bindings add column "
        "additional_offer_landings jsonb not null default '[]'::jsonb;"
    ) in compact
    assert (
        f"add constraint {CONSTRAINT} check (coalesce(public.{HELPER}( "
        "additional_offer_landings, additional_offer_codes, lead_site, lead_landing_id "
        "), false));"
    ) in compact
    # Postgres corta los identificadores en 63 bytes: un nombre mas largo no es el
    # que buscan el inventario de esquema ni el validador.
    assert len(CONSTRAINT.encode("utf-8")) <= 63


def test_migration_is_transactional_and_seeds_nothing() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    outside_functions = re.sub(r"\$function\$.*?\$function\$", "", sql, flags=re.DOTALL)
    outside_functions = re.sub(r"--[^\n]*", "", outside_functions)

    assert re.search(r"^begin;\nset local lock_timeout = '5s';\n", sql, re.MULTILINE)
    assert sql.rstrip().endswith("commit;")
    assert not re.search(r"\b(insert|update|delete)\b", outside_functions, re.IGNORECASE)
    assert not re.search(r"\bdrop\b", outside_functions, re.IGNORECASE)
    assert len(re.findall(r"\balter\s+table\b", outside_functions, re.IGNORECASE)) == 2
