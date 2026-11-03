"""La etiqueta de un segmento en un flujo nativo (20261103001300, E-316), leida como texto.

El comportamiento lo prueba ``tests/sql/followup_engine/validate_flow_engine_native_segment_label.mjs`` con
la cadena entera en PGlite (antes y despues). Aca se fija lo que no tiene que romperse en silencio:

* es una sola transaccion con ``lock_timeout``, la ultima de la cadena, y aborta si ya corrio;
* cambia una sola funcion viva, ``_flow_engine_native_unsupported``, con un reemplazo contado que falla con
  55000 si el texto no esta exactamente una vez, y no crea ni borra nada ni toca permisos;
* el texto que reemplaza es el de la definicion vigente (20261101000200, el bloque de etiquetar);
* el validador de PGlite es el ultimo de la cadena de npm.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261103001300_flow_engine_native_segment_label_v1.sql"
SILENCE = MIGRATIONS / "20261101000200_flow_engine_native_silence_v1.sql"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(line for line in _sql().splitlines() if not line.lstrip().startswith("--"))


def test_it_is_one_transaction_with_lock_timeout_and_the_last() -> None:
    sql = _sql()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # El origen de un recorrido nativo para la campana por landing (20261103001700, E-321): una lectura de service_role.
    assert names[-1] == "20261103001700_flow_engine_native_campaign_origin_v1.sql"
    # La entrada a una campana: lo que contesto cada conversacion a la pregunta (20261103001500, E-317).
    assert names[-2] == "20261103001500_segment_entry_answers_v1.sql"
    assert names[-3] == MIGRATION.name
    assert names[-4] == "20261103001000_handoff_wait_alerts_v1.sql"
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'flow_engine_native_segment_label_already_applied'" in sql


def test_it_patches_only_the_native_shape_with_a_counted_replacement() -> None:
    code = " ".join(_code().split())
    assert "message = 'unexpected_flow_engine_native_unsupported_definition'" in code
    assert code.count("length(v_text) - length(replace(v_text, c_old, '')) <> length(c_old)") == 1
    assert "execute replace(v_text, c_old, c_new);" in code
    lowered = _code().lower()
    for forbidden in ("create function", "create or replace function", "drop ", "grant ", "revoke ",
                      "create table", "alter table", "insert ", "update ", "delete "):
        assert forbidden not in lowered, forbidden


def _literal(name: str) -> str:
    """El texto de una constante ``E'...' || E'...'`` del bloque, como lo ve Postgres."""
    block = re.search(rf"{name} constant text :=\s*(.*?);\n", _sql(), re.S)
    assert block is not None, name
    parts = re.findall(r"E'((?:[^']|'')*)'", block.group(1))
    return "".join(part.replace("''", "'").replace("\\n", "\n").replace("\\\\", "\\") for part in parts)


def test_the_replaced_text_is_the_live_definition() -> None:
    old = _literal("c_old")
    assert SILENCE.read_text(encoding="utf-8").count(old) == 1
    new = _literal("c_new")
    assert "flow_engine_native_segment_label" in new
    assert "step.value ->> 'etiquetar' = 'cliente'" in new
    assert "like 'automation\\_%'" in new
    # Lo de antes queda: solo se suma la condicion de la etiqueta.
    assert new.replace(
        "\n                                and (jsonb_typeof(step.value -> 'etiquetar') is distinct from 'string'"
        "\n                                     or step.value ->> 'etiquetar' = 'cliente'"
        "\n                                     or step.value ->> 'etiquetar' like 'automation\\_%')))", "))"
    ).replace(
        "             -- flow_engine_native_segment_label: la etiqueta de un segmento va con cualquier\n"
        "             -- disparador; cliente, la de la automatizacion o una que no es texto, solo con la\n"
        "             -- compra (20261103001300).\n", "") == old


def test_the_validator_is_the_last_of_the_npm_chain() -> None:
    chain = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert chain.rstrip().endswith(
        " && node validate_handoff_wait_alerts.mjs && node validate_flow_engine_native_segment_label.mjs && node validate_segment_entry_answers.mjs && node validate_flow_engine_native_campaign_origin.mjs --check")
