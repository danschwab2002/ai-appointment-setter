"""Cada envio a una audiencia dice cuantas personas arrancaron, cuantas no y por que (E-666).

Por que: en Maribel, el 2026-11-16 17:09Z, la ocurrencia 8ed2f5be... de ``aviso_manual_prueba``
termino con su unica persona sin arrancar (``conversacion_viva``) y la pagina de envios decia
«Terminado · Salieron 1; el motor abrio el envio a 0», sin el motivo.

Las filas son las que contesta la base:

* ``maribel_occurrence_not_started_20261117.json``: el cuerpo de la migracion 20261117000700
  corrido como SELECT en ``maribel_db`` (solo lectura) sobre esa ocurrencia.
* ``occurrence_start_reasons_rows.json``: la lectura de la pagina en la cadena entera (PGlite,
  ``validate_flow_audience_start_reasons.mjs --emit``), con y sin la migracion.

La pagina se prueba de punta a punta con los dobles de ``test_flow_audience_page.py`` (la sesion
de «Entrar con Slack» y PostgREST emulados) y con ``render_audience``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from test_flow_audience_page import _client, _page

from bridge.flow_audience_page import render_audience

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/flow_audience"
MIGRATION = ROOT / "supabase/migrations/20261117000700_flow_audience_start_reasons_v1.sql"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


# Los uuid del fixture de la cadena van normalizados (<OCCURRENCE>, <FLOW>); la pagina pide uno de verdad.
OCCURRENCE = "0000000c-0000-4000-8000-00000000e666"


def _render(row: dict[str, Any]) -> str:
    row = {**row, "occurrence_id": OCCURRENCE}
    return _text(render_audience({"flows": [], "occurrences": [row]}, csrf="x", done={}, time_zone="UTC"))


def test_the_maribel_occurrence_says_who_did_not_start_and_why() -> None:
    row = _fixture("maribel_occurrence_not_started_20261117.json")["occurrence"]
    text = _render(row)

    assert "Terminado" in text
    assert "Salieron de la lista 1 : arrancaron 0 , no arrancaron 1 ." in text
    assert "Por qué no arrancaron: 1 estaba conversando con nosotros en las últimas 24 h." in text
    # Lo que decia antes, sin motivo, ya no sale.
    assert "el motor abrió el envío a" not in text


def test_the_chain_row_groups_every_outcome_in_plain_words() -> None:
    row = _fixture("occurrence_start_reasons_rows.json")["occurrence"]
    text = _render(row)

    assert "Saliendo" in text
    assert "Salieron de la lista 5 : arrancaron 1 , no arrancaron 3 , esperando al motor 1." in text
    assert ("Por qué no arrancaron: 1 el flujo se apagó o cambió de versión; "
            "1 estaba conversando con nosotros en las últimas 24 h; 1 no quedó registrado el motivo.") in text


def test_a_base_without_the_migration_shows_what_it_showed_before() -> None:
    row = _fixture("occurrence_start_reasons_rows.json")["occurrence_before_migration"]
    text = _render(row)

    assert "Salieron 5; el motor abrió el envío a 1." in text
    assert "Por qué no arrancaron" not in text


@pytest.mark.parametrize(("reasons", "expected"), [
    ({"no_arranca_mientras:carrito_nativo": 2, "no_arranca_mientras:pago_rechazado": 1},
     "3 tenía otro flujo en curso"),
    ({"error_interno:p0001": 1}, "1 falló el sistema al arrancar"),
    ({"no_entra:permiso": 1, "sin_permiso": 1}, "2 no tiene permiso vigente para recibir el mensaje"),
    ({"algo_nuevo": 1}, "1 otro motivo (código algo_nuevo)"),
])
def test_reasons_are_grouped_by_what_the_operator_reads(reasons: dict[str, int], expected: str) -> None:
    n = sum(reasons.values())
    row = {**_fixture("maribel_occurrence_not_started_20261117.json")["occurrence"],
           "released_count": n, "not_started_count": n, "not_started_by_reason": reasons}

    assert f"Por qué no arrancaron: {expected}." in _render(row)


def test_a_braked_occurrence_that_already_released_people_also_says_it() -> None:
    row = {**_fixture("maribel_occurrence_not_started_20261117.json")["occurrence"],
           "state": "frenada", "brake_reason": "a_mano"}
    text = _render(row)

    assert "Frenado: lo frenó una persona." in text
    assert "no arrancaron 1" in text


def test_the_page_route_shows_the_reason_without_phones_or_contacts() -> None:
    client, db, _ = _client()
    row = _fixture("maribel_occurrence_not_started_20261117.json")["occurrence"]
    db.occurrence = {**row, "occurrence_id": db.occurrence["occurrence_id"]}
    html = _page(client)
    text = _text(html)

    assert "Por qué no arrancaron: 1 estaba conversando con nosotros en las últimas 24 h." in text
    assert not re.search(r"\d{10,}", text)


def test_the_migration_replaces_only_the_rows_reading_with_a_guard() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    body = re.sub(r"--[^\n]*", "", sql)

    assert body.count("create or replace function") == 1
    assert "create or replace function public._flow_audience_occurrence_rows(p_tenant_ref text" in body
    # La definicion de 20261112220000 en produccion (maribel_db y att1-db, 2026-11-17), o aborta.
    assert "c_old_md5 constant text := 'fbddc394830be11898b98dc144b8a6d6'" in body
    assert "unexpected_flow_audience_occurrence_rows_definition" in body
    assert "flow_audience_start_reasons: ya aplicada" in body
    for key in ("not_started_count", "no_flow_count", "unsettled_count", "not_started_by_reason"):
        assert f"as {key}" in body
    assert "'sin_registro'" in body
    assert "grant " not in body and "revoke " not in body
    assert body.strip().startswith("begin;") and body.strip().endswith("commit;")


def test_the_new_validator_runs_with_npm_test() -> None:
    package = json.loads((ROOT / "tests/sql/followup_engine/package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["test"].endswith(" && node validate_flow_audience_start_reasons.mjs --check && node validate_flow_engine_admission_identity_mismatch.mjs && node validate_bulk_send_store.mjs && node validate_flow_engine_literal_variables.mjs && node validate_flow_engine_native_silence_literal.mjs")
