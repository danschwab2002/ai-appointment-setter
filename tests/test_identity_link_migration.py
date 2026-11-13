"""La forma de las migraciones del correlador que decide solo (E-536).

El efecto lo prueba ``tests/sql/followup_engine/validate_identity_link_decider.mjs``
(en rojo sin las migraciones). Aca, lo estatico: una transaccion cada una, las
ultimas de la cadena, los reemplazos solo sobre las cuatro funciones del
carrito y del permiso, y que la evidencia no devuelva columnas de identidad.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from bridge import identity_link

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
A = MIGRATIONS / "20261114000100_identity_link_decider_v1.sql"
B = MIGRATIONS / "20261114000150_identity_link_question_v1.sql"


def test_they_are_one_transaction_each_and_the_last_two() -> None:
    for path in (A, B):
        sql = path.read_text(encoding="utf-8")
        assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
        assert "set local lock_timeout = '5s';" in sql
        assert "already_applied" in sql
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # Despues de la ficha de OXXO nativa (20261113200000, E-528, C4), la ultima del #455.
    assert names[-3] == "20261113200000_e528_native_payment_slip_v1.sql"
    assert names[-2] == A.name
    assert names[-1] == B.name


def test_the_replacements_are_only_the_four_functions_of_the_cart_and_the_permission() -> None:
    sql = A.read_text(encoding="utf-8")
    block = sql.split("do $patch$", 1)[1].split("$patch$;", 1)[0]
    signatures = re.findall(r"'(public\.[a-z_0-9]+\([^']*\))'", block.split("v_anchors", 1)[0])
    assert sorted(set(signatures)) == sorted({
        "public.plan_lancemos_pilot_cart_recovery(uuid,uuid,text,text,text,text,integer,timestamptz,bigint,bigint,text,text,integer)",
        "public._portable_cart_audience_checks(text,integer,uuid,uuid,text,text)",
        "public._flow_engine_native_cart_audience(uuid,uuid,text,text,text)",
        "public._portable_consented_intent_reason(uuid,uuid,text)",
    })
    # El metodo: el texto exacto una vez, la marca, el duenio y la marca despues.
    assert "unexpected_identity_link_definition" in block
    assert "identity_link_owner_mismatch" in block
    assert "identity_link_definition_not_replaced" in block
    # reserve_recovery_checkout_issuance_v1 (que reemplaza el #455) no se toca.
    code = re.sub(r"--[^\n]*", "", sql)
    assert "reserve_recovery_checkout_issuance_v1" not in code
    # Ninguna funcion del correlador, del pre-resolutor ni de la resolucion humana.
    for untouched in ("correlate_hotmart_purchase_intent", "operator_correlation", "claim_operator_correlation"):
        assert untouched not in code


def test_the_question_migration_replaces_only_the_notices_read() -> None:
    code = re.sub(r"--[^\n]*", "", B.read_text(encoding="utf-8"))
    assert re.findall(r"create or replace function (public\.[a-z_0-9]+)", code) == [
        "public.read_identity_link_notices_v1"
    ]


def test_the_evidence_returns_only_traits() -> None:
    sql = A.read_text(encoding="utf-8")
    evidence = sql.split("create function public.get_identity_link_evidence_v1", 1)[1].split("$function$;", 1)[0]
    returns = re.findall(r"return jsonb_build_object\((.*?)\);", evidence, flags=re.S)
    assert len(returns) == 2
    for body in returns:
        assert re.findall(r"'(version|persona|guard_reason|rasgos)'", body) == [
            "version", "persona", "guard_reason", "rasgos"]
        assert not re.search(r"v_(intent|checkout|form|event)", body)
    traits = evidence.split("v_traits := jsonb_build_object(", 1)[1].split("    );\n", 1)[0]
    keys = set(re.findall(r"^\s+'([a-z_0-9]+)',", traits, flags=re.M))
    keys |= {"selector_pais_sospechoso"}
    expected = {
        "coincide_por", "pais_formulario", "pais_checkout",
        *identity_link._BOOL_TRAITS, *identity_link._PERCENT_TRAITS, *identity_link._COUNT_TRAITS,
    }
    assert keys == expected
    # Ninguna columna de identidad va como valor de un rasgo: solo comparaciones.
    for line in traits.splitlines():
        value = line.split(",", 1)[1] if line.strip().startswith("'") and "," in line else line
        assert not re.fullmatch(r"\s*v_(intent\.normalized_(email|phone)|checkout_(email|phone|name)|form_name)\s*,?\s*", value)


def test_the_validator_runs_last_with_npm_test() -> None:
    package = json.loads((ROOT / "tests/sql/followup_engine/package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["test"].endswith(
        " && node validate_e528_native_payment_slip.mjs && node validate_identity_link_decider.mjs"
    )
