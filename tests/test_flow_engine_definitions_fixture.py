"""Las definiciones del carrito y del pago fallido de ATT1, version por version del scope.

Diseno de la T3a, §3.4 y §8.1. Fixture:
``tests/fixtures/flow_simulation/att1_definitions_by_scope_version.json``. Son
el carrito y el pago fallido de la v3, la v4 y la v5 de ``att1-recuperacion``,
las tres versiones que corrieron en la ventana de la captura de ATT1, como las
da el traductor (``tests/flow_store_translator.py``, sin tocar) sobre la
``config`` de ``flow_engine/att1_recovery_capture_20261016.json`` con el
control apuntando a cada version, mas ``att1_instancia_f01fbcc.toml`` y
``schema_examples_v1.json``, normalizadas y publicadas con
``bridge.flow_definition``. El numero de version del flujo es el del scope.

No es una captura: lo genera esta prueba. Para regenerarlo:
``FLOW_SIMULATION_FIXTURE_DIR=tests/fixtures/flow_simulation uv run pytest
tests/test_flow_engine_definitions_fixture.py``.

**El legado que cambia no la pone roja** (diseno de la T3a, §9): la parte de
la traduccion que vive en el codigo (``CODE`` del traductor) es de hoy, y la
sesion de los flujos comerciales la puede cambiar. La cabecera guarda el
sha256 de ``CODE`` en ``c7fa8ef``; si difiere, la prueba avisa
(``warnings.warn``) y no compara. Lo que afirma igual, siempre, es lo que el
fixture dice de la captura: los topes y la politica de cada version.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import sys
import tomllib
import warnings
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bridge import flow_definition as fd
from bridge.flow_policy import compile_policy, differences_from_row
from bridge.flow_validator import InstanceView, TemplateCatalog

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
FLOW_STORE = FIXTURES / "flow_store"
TARGET = FIXTURES / "flow_simulation" / "att1_definitions_by_scope_version.json"
CAPTURE_NAME = "att1_recovery_capture_20261016.json"

SPEC = importlib.util.spec_from_file_location("flow_store_translator", ROOT / "tests" / "flow_store_translator.py")
assert SPEC and SPEC.loader
TRANSLATOR = importlib.util.module_from_spec(SPEC)
# Una dataclass busca su modulo en sys.modules al definirse.
sys.modules[SPEC.name] = TRANSLATOR
SPEC.loader.exec_module(TRANSLATOR)

CAPTURE: dict[str, Any] = json.loads((FIXTURES / "flow_engine" / CAPTURE_NAME).read_text(encoding="utf-8"))
MANIFEST: dict[str, Any] = tomllib.loads((FLOW_STORE / "att1_instancia_f01fbcc.toml").read_text(encoding="utf-8"))
BLOCKS: list[dict[str, Any]] = json.loads(
    (FLOW_STORE / "schema_examples_v1.json").read_text(encoding="utf-8"))["blocks"]
CATALOG = TemplateCatalog.from_inbox_payload(json.loads(
    (FIXTURES / "chatwoot_inbox_11_message_templates_20261010.json").read_text(encoding="utf-8")))
INSTANCE = InstanceView(
    MANIFEST["instancia"]["tenant_ref"], MANIFEST["instancia"]["ally_ref"],
    frozenset({MANIFEST["instancia"]["funnel_ref"]}), MANIFEST["instancia"]["zona_horaria"],
)

SCOPE = "att1-recuperacion"
POLICY = "att1-recuperacion-un-toque"
# Las versiones del scope que corrieron desde «desde» (la activacion de la v3).
VERSIONS = (3, 4, 5)
# Los dos flujos del scope, con un flow_id inventado para la simulacion y su legacy_kind.
FLOWS = {
    "carrito": {"flow_id": "1b000003-0000-4000-8000-0000000c0001", "legacy_kind": "carrito"},
    "pago_fallido": {"flow_id": "1b000003-0000-4000-8000-0000000c0002", "legacy_kind": "pago_fallido"},
}
# El sha256 de CODE en c7fa8ef, sobre json.dumps(CODE, sort_keys=True,
# ensure_ascii=False, separators=(",", ":")) (diseno de la T3a, §3.4).
CODE_SHA256_C7FA8EF = "f1e183184fd9c37db941a711c800e3d6d01c979fe51ffd0fc512766206529c20"


def code_sha256() -> str:
    text = json.dumps(TRANSLATOR.CODE, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _config_at(version: int) -> dict[str, Any]:
    """La config de la captura con el control de att1-recuperacion en ``version``."""
    config = copy.deepcopy(CAPTURE["config"])
    controls = [row for row in config["pilot_runtime_controls"] if row["scope_key"] == SCOPE]
    assert len(controls) == 1
    controls[0]["scope_version"] = version
    return config


def _normalize(definition: dict[str, Any], version: int) -> dict[str, Any]:
    result = fd.normalize_definition(definition, new_flow=True, instance=INSTANCE, catalog=CATALOG)
    assert result.findings == (), [(f.code, f.path) for f in result.findings]
    return fd.as_published(result.definition, version)


def _document() -> dict[str, Any]:
    versions: dict[str, dict[str, Any]] = {}
    for version in VERSIONS:
        translated = TRANSLATOR.translate_att1(_config_at(version), MANIFEST, BLOCKS)
        versions[str(version)] = {key: _normalize(translated[key].definition, version) for key in FLOWS}
    return {
        "_generated": {
            "generator": (
                "tests/test_flow_engine_definitions_fixture.py (test_the_fixture_is_what_the_translator_gives), "
                "con tests/flow_store_translator.py y bridge.flow_definition"
            ),
            "date": datetime.now(timezone.utc).date().isoformat(),
            "from": (
                f"tests/flow_store_translator.py sobre la config de flow_engine/{CAPTURE_NAME} con el control de "
                f"{SCOPE} en la v3, la v4 y la v5, att1_instancia_f01fbcc.toml y schema_examples_v1.json; "
                "normalizado y publicado con bridge.flow_definition (as_published con la version del scope)"
            ),
            "regenerate": (
                "FLOW_SIMULATION_FIXTURE_DIR=tests/fixtures/flow_simulation "
                "uv run pytest tests/test_flow_engine_definitions_fixture.py"
            ),
            "base": "c7fa8ef",
            "code_sha256": CODE_SHA256_C7FA8EF,
            "code_sha256_of": 'json.dumps(CODE, sort_keys=True, ensure_ascii=False, separators=(",", ":"))',
        },
        "instance": {"tenant_ref": INSTANCE.tenant_ref, "ally_ref": INSTANCE.ally_ref,
                     "products": sorted(INSTANCE.products), "time_zone": INSTANCE.time_zone},
        "scope_key": SCOPE,
        "flows": FLOWS,
        "versions": versions,
    }


IN_REPO: dict[str, Any] = json.loads(TARGET.read_text(encoding="utf-8"))


def test_the_fixture_is_what_the_translator_gives() -> None:
    target = os.environ.get("FLOW_SIMULATION_FIXTURE_DIR")
    if target:
        (Path(target) / TARGET.name).write_text(
            json.dumps(_document(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return
    current = code_sha256()
    if current != IN_REPO["_generated"]["code_sha256"]:
        warnings.warn(
            f"tests/flow_store_translator.py: CODE cambio desde c7fa8ef (sha256 {current}); "
            f"{TARGET.name} sigue con las definiciones de c7fa8ef y no se compara. La 1c las vuelve a traducir.",
            stacklevel=1,
        )
        return
    in_repo = copy.deepcopy(IN_REPO)
    generated_on = in_repo["_generated"].pop("date", None)
    assert isinstance(generated_on, str) and date.fromisoformat(generated_on) >= date(2026, 10, 17)
    document = _document()
    document["_generated"].pop("date")
    assert in_repo == document, f"{TARGET.name} no es el de hoy: regenerarlo con FLOW_SIMULATION_FIXTURE_DIR"


def test_the_header_keeps_the_sha256_of_code_at_c7fa8ef() -> None:
    assert IN_REPO["_generated"]["code_sha256"] == CODE_SHA256_C7FA8EF
    assert IN_REPO["_generated"]["base"] == "c7fa8ef"


def _scope_row(version: int) -> dict[str, Any]:
    [row] = [row for row in CAPTURE["config"]["pilot_scope_versions"]
             if row["scope_key"] == SCOPE and row["version"] == version]
    return row


def test_each_version_has_the_caps_of_its_scope_row() -> None:
    # Las igualdades que hacen valer el prototipo (diseno de la T3a, §7.1, punto
    # 5): 40/400, 40/1000 y 80/1000, de 09 a 21 de CDMX, vence 1d y sin espera.
    expected = {3: (40, 400), 4: (40, 1000), 5: (80, 1000)}
    assert set(IN_REPO["versions"]) == {str(version) for version in VERSIONS}
    for version in VERSIONS:
        row = _scope_row(version)
        for key, definition in IN_REPO["versions"][str(version)].items():
            caps = definition["topes"]
            assert (caps["por_dia"], caps["total"]) == expected[version], (version, key)
            assert (caps["por_dia"], caps["total"]) == (
                row["max_outbound_request_starts_per_day"], row["max_outbound_request_starts_total"])
            assert caps["bolsa"] == SCOPE
            assert definition["horario"] == {"zona": "America/Mexico_City", "desde": "09:00", "hasta": "21:00"}
            assert definition["termina"] == {"si": ["compra"], "vence": "1d"}
            assert [fd.step_type(step) for step in definition["pasos"]] == ["plantilla"]
            assert definition["version"] == version and definition["estado"] == fd.PUBLISHED
            assert definition["flujo"] == key and definition["producto"] == "att1"


def test_each_version_compiles_to_the_policy_row_of_the_capture() -> None:
    [row] = [row for row in CAPTURE["config"]["followup_policy_versions"] if row["policy_key"] == POLICY]
    for version in VERSIONS:
        assert _scope_row(version)["policy_key"] == POLICY
        for key, definition in IN_REPO["versions"][str(version)].items():
            compiled = compile_policy(definition, flow_id=FLOWS[key]["flow_id"], version=version,
                                      legacy_kind=FLOWS[key]["legacy_kind"])
            assert differences_from_row(compiled, row) == (), (version, key)


def test_the_fixture_has_no_personal_data() -> None:
    # Ni un mail ni un numero que pueda ser un telefono: son definiciones, no personas.
    text = TARGET.read_text(encoding="utf-8")
    assert "@" not in text
    assert re.search(r"\b[0-9]{8,}\b", text) is None


def test_a_change_in_code_warns_and_does_not_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    # La otra sesion cambia lo que el traductor toma del codigo: la prueba avisa
    # y no se pone roja por un archivo que no es suyo.
    monkeypatch.delenv("FLOW_SIMULATION_FIXTURE_DIR", raising=False)
    monkeypatch.setattr(TRANSLATOR, "CODE", {**TRANSLATOR.CODE, "carrito": {**TRANSLATOR.CODE["carrito"],
                                                                          "nombre": "Otro nombre"}})
    with pytest.warns(UserWarning, match="CODE cambio desde c7fa8ef"):
        test_the_fixture_is_what_the_translator_gives()
