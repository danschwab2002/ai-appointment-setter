"""`setter validate` sobre una carpeta de instancia (docs/design/setter-producto-instalable-v1.md §4.5)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from bridge.instance_cli import main, validate_instance

ATT1 = Path(__file__).parent / "fixtures" / "instances" / "att1"


@pytest.fixture
def att1_copy(tmp_path: Path) -> Path:
    target = tmp_path / "setter-instancia-att1"
    shutil.copytree(ATT1, target)
    return target


def test_att1_instance_is_valid_with_its_warnings() -> None:
    report = validate_instance(ATT1)

    assert report["valida"] is True
    assert report["errores"] == []
    assert report["conocimiento"]["estado"] == "borrador"
    assert report["flujos"]["carrito"] == {"prendido": False, "se_puede_prender": True, "falta": []}
    assert report["flujos"]["reactivacion"]["se_puede_prender"] is False
    assert any("borrador" in warning for warning in report["avisos"])


def test_exit_codes(att1_copy: Path, tmp_path: Path, capsys) -> None:
    assert main(["validate", str(att1_copy)]) == 0
    assert "VALIDA" in capsys.readouterr().out

    (att1_copy / "instancia.toml").write_text("schema = 'otra'\n", encoding="utf-8")
    assert main(["validate", str(att1_copy)]) == 1
    assert main(["validate", str(tmp_path / "no-existe")]) == 2


def test_json_output_is_parseable(capsys) -> None:
    assert main(["validate", str(ATT1), "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["manifiesto"]["ally_ref"] == "att1"


def test_missing_knowledge_file_is_an_error(att1_copy: Path) -> None:
    (att1_copy / "conocimiento" / "knowledge-v1.toml").unlink()

    report = validate_instance(att1_copy)

    assert report["valida"] is False
    assert "knowledge-v1.toml" in report["errores"][0]


def test_knowledge_of_another_ally_is_an_error(att1_copy: Path) -> None:
    path = att1_copy / "conocimiento" / "knowledge-v1.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace('ally_ref = "att1"', 'ally_ref = "johanna"'),
        encoding="utf-8",
    )

    report = validate_instance(att1_copy)

    assert report["valida"] is False
    assert "es de 'johanna'" in report["errores"][0]


def test_inbound_on_with_draft_knowledge_is_an_error(att1_copy: Path) -> None:
    path = att1_copy / "instancia.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("inbound = false", "inbound = true"),
        encoding="utf-8",
    )

    report = validate_instance(att1_copy)

    assert report["valida"] is False
    assert "inbound" in report["errores"][0]
