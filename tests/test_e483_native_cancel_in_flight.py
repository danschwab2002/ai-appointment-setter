"""E-483 (C8 del mapa «el AE maneja todos los flujos», 2026-11-12): ``en_curso: cancelar`` en un nativo.

La base (20261112210100) acepta ``cancelar`` en la version o el volver de un flujo nativo: al publicar
cancela los toques que esperan de los recorridos de otra version y cierra el caso con
``cambio_de_version`` (lo mide ``validate_e483_native_cancel_in_flight.mjs`` en PGlite). Aca, el validador:
``cancelar`` pasa en un nativo y sigue bloqueando en el legado y en la ruta motor; ``migrar`` no existe.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import test_flow_validator as tfv

from bridge import flow_definition as fd
from bridge import flow_validator as fv
from bridge.flow_definition import ProposalItem

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20261112210100_e483_native_cancel_in_flight_v1.sql"


def _native_world(state: str = "prendido") -> tuple[fd.FlowWorld, fd.WorldFlow]:
    base = tfv.native_flow(tfv._form(), state=state)
    two = replace(base, versions={**base.versions, 2: {**base.versions[1], "version": 2}}, active_version=2,
                  generation=2)
    return tfv.world(two, tfv._native_welcome("bienvenida", tfv.WELCOME_ID, "prendido")), two


def _native_version(en_curso: str, *, actor: str = "automation_expert", state: str = "prendido") -> list[str]:
    flows, native = _native_world(state)
    draft = fd.as_draft(native.versions[2])
    result = tfv.check([tfv.version_item(draft, "nativo", flow_id=tfv.NATIVE_ID, en_curso=en_curso)],
                       actor=actor, in_world=flows, catalog=None, phase="draft")
    return [finding.code for finding in result.blocks]


def _native_rollback(en_curso: str) -> list[str]:
    flows, _ = _native_world()
    item = ProposalItem(producto="att1", flujo="nativo", flow_id=tfv.NATIVE_ID, tipo="volver", version=1,
                        en_curso=en_curso)
    return [finding.code for finding in tfv.check([item], actor="automation_expert", in_world=flows,
                                                  catalog=None, phase="draft").blocks]


@pytest.mark.parametrize("state", ["prendido", "cohorte"])
def test_cancel_passes_on_a_native_version(state: str) -> None:
    # Antes de C8 daba en_curso_not_supported.
    assert "en_curso_not_supported" not in _native_version("cancelar", state=state)
    assert _native_version("cancelar", state=state) == _native_version("seguir", state=state)


def test_cancel_passes_on_a_native_rollback() -> None:
    assert _native_rollback("cancelar") == _native_rollback("seguir")
    assert "en_curso_not_supported" not in _native_rollback("cancelar")


def test_cancel_does_not_need_the_multitouch_flag() -> None:
    flows, native = _native_world()
    item = tfv.version_item(fd.as_draft(native.versions[2]), "nativo", flow_id=tfv.NATIVE_ID, en_curso="cancelar")
    result = tfv.check([item], actor="automation_expert", in_world=flows, catalog=None, phase="draft",
                       instance=tfv._running(multitouch=False))
    assert "en_curso_not_supported" not in [finding.code for finding in result.blocks]


def test_migrate_still_blocks_on_a_native() -> None:
    assert "en_curso_not_supported" in _native_version("migrar")
    assert "en_curso_not_supported" in _native_rollback("migrar")


@pytest.mark.parametrize("executor", [fd.ROUTE_LEGACY, fd.ROUTE_MOTOR])
def test_cancel_blocks_on_the_legacy_and_the_motor_route(executor: str) -> None:
    routed = tfv._routed(executor)
    item = tfv.version_item(tfv.draft_of("carrito"), en_curso="cancelar")
    result = tfv.check([item], in_world=tfv.world(routed))
    assert [(finding.code, finding.path) for finding in result.blocks
            if finding.code == "en_curso_not_supported"] == [("en_curso_not_supported", "/0/en_curso")]


def test_the_owner_text_says_why_for_a_legacy_flow() -> None:
    spec = fv.EN_CURSO_NOT_SUPPORTED
    assert "sistema nuevo de mensajes" in spec.reason
    assert "sigue con la versión que lo empezó" in spec.reason
    assert spec.alternative is not None and spec.alternative.startswith("Usar en_curso: seguir.")
    assert "Hasta el motor" not in spec.reason


def test_the_migration_cancels_only_on_a_native_and_closes_with_the_contract_reason() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert text.count("begin;") == 1 and text.rstrip().endswith("commit;")
    assert "c_reason constant text := 'cambio_de_version'" in text
    assert "r.legacy_kind is null" in text and "run.legacy_kind is null" in text
    assert "e483_native_cancel_in_flight_already_applied" in text
    assert "''\"cancelar\"''::jsonb" in text and "migrar" not in text.split("c_new_policy")[1].split(";")[0]
