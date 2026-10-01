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


_INTENT_EVENTS = 'eventos = ["carrito", "pago_fallido", "compra", "entrante", "intencion"]'
# Una aceptacion DE PRUEBA del riesgo del adaptador: ningun manifiesto real la
# recibe de este codigo, la escribe a mano quien firma.
_TEST_ACCEPTANCE = (
    'riesgo_aceptado_por = "aceptacion de prueba (tests)"\n'
    "riesgo_aceptado_el = 2026-10-01\n"
    'riesgo_contrato = "ghl-precheckout-adapter-v1"\n'
)


def _add_intent_event(instance: Path) -> None:
    path = instance / "instancia.toml"
    text = path.read_text(encoding="utf-8")
    events = 'eventos = ["carrito", "pago_fallido", "compra", "entrante"]'
    assert events in text
    path.write_text(text.replace(events, _INTENT_EVENTS), encoding="utf-8")


def _add_ghl_adapter(instance: Path, forms: list[str], *, acceptance: str = "") -> None:
    # El fixture de ATT1 no tiene la seccion: se arma sobre la copia, nunca sobre el fixture.
    _add_intent_event(instance)
    path = instance / "instancia.toml"
    text = path.read_text(encoding="utf-8")
    text += "\n[adaptadores.ghl]\nformularios = [" + ", ".join(f'"{form}"' for form in forms) + "]\n"
    path.write_text(text + acceptance, encoding="utf-8")


def _turn_on(instance: Path, *flows: str) -> None:
    path = instance / "instancia.toml"
    text = path.read_text(encoding="utf-8")
    for flow in flows:
        assert f"{flow} = false" in text
        text = text.replace(f"{flow} = false", f"{flow} = true")
    path.write_text(text, encoding="utf-8")


def test_report_lists_the_ghl_adapter_forms(att1_copy: Path, capsys) -> None:
    _add_ghl_adapter(att1_copy, ["EgDqRl2xWc59YjVW1q8W"])

    report = validate_instance(att1_copy)

    assert report["valida"] is True
    assert report["manifiesto"]["adaptadores"] == {
        "ghl": {"formularios": ["EgDqRl2xWc59YjVW1q8W"], "riesgo": None}
    }
    assert main(["validate", str(att1_copy)]) == 0
    output = capsys.readouterr().out
    assert "adaptador ghl, formularios: EgDqRl2xWc59YjVW1q8W" in output
    assert "adaptador ghl, riesgo sin aceptar" in output


def test_the_ghl_adapter_without_the_acceptance_warns_what_it_cannot_turn_on(
    att1_copy: Path,
) -> None:
    # ATT1 hoy: la seccion, sin aceptacion y con todos los flujos apagados. Es
    # valida, y validate dice lo que esa instancia no puede prender. La audiencia
    # del scope del piloto vive en la base: validate la nombra, no la ve.
    _add_ghl_adapter(att1_copy, ["EgDqRl2xWc59YjVW1q8W"])

    report = validate_instance(att1_copy)

    assert report["valida"] is True
    [missing] = [w for w in report["avisos"] if "no tiene la aceptacion del riesgo" in w]
    assert "precheckout y pago_fallido no se pueden prender" in missing
    assert "consented_intent o consented_intent_in_cohort" in missing
    assert "validate no la ve" in missing
    # Y que quitar la seccion no es una salida.
    assert any(
        "quitar [adaptadores.ghl] no saca de la base las intenciones" in w
        for w in report["avisos"]
    )
    # Sin el adaptador en el manifiesto no hay aviso.
    assert not any("adaptadores.ghl" in warning for warning in validate_instance(ATT1)["avisos"])


@pytest.mark.parametrize(
    "flows",
    [("precheckout",), ("pago_fallido",), ("precheckout", "pago_fallido")],
    ids="+".join,
)
def test_a_gated_flow_on_with_the_adapter_unaccepted_is_an_error(
    att1_copy: Path, flows: tuple[str, ...]
) -> None:
    # La guarda del arranque cuelga de la seccion del manifiesto, no de una variable
    # del servicio: validate la ve entera y ya no es un aviso (el bridge no arranca,
    # test_instance_wiring lo prueba).
    _add_ghl_adapter(att1_copy, ["EgDqRl2xWc59YjVW1q8W"])
    _turn_on(att1_copy, *flows)

    report = validate_instance(att1_copy)

    assert report["valida"] is False
    assert [error.split(" esta prendido")[0] for error in report["errores"]] == [
        f"flujos.{flow}" for flow in flows
    ]
    for error in report["errores"]:
        assert "[adaptadores.ghl] sin la aceptacion del riesgo" in error
        assert "el bridge no arranca" in error
    assert main(["validate", str(att1_copy)]) == 1


def test_a_gated_flow_on_with_the_acceptance_is_valid(att1_copy: Path, capsys) -> None:
    _add_ghl_adapter(att1_copy, ["EgDqRl2xWc59YjVW1q8W"], acceptance=_TEST_ACCEPTANCE)
    _turn_on(att1_copy, "precheckout", "pago_fallido")

    report = validate_instance(att1_copy)

    assert report["valida"] is True
    assert report["errores"] == []
    # Informa quien, cuando y que contrato.
    assert report["manifiesto"]["adaptadores"]["ghl"]["riesgo"] == {
        "aceptado_por": "aceptacion de prueba (tests)",
        "aceptado_el": "2026-10-01",
        "contrato": "ghl-precheckout-adapter-v1",
    }
    assert not any("no tiene la aceptacion del riesgo" in w for w in report["avisos"])
    # Aceptado tambien avisa que quitar la seccion reabre el riesgo.
    assert any(
        "quitar [adaptadores.ghl] no saca de la base las intenciones" in w
        for w in report["avisos"]
    )
    assert main(["validate", str(att1_copy), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["manifiesto"]["adaptadores"]["ghl"]["riesgo"][
        "aceptado_el"
    ] == "2026-10-01"
    assert main(["validate", str(att1_copy)]) == 0
    assert (
        "adaptador ghl, riesgo aceptado por aceptacion de prueba (tests) el 2026-10-01 "
        "(contrato ghl-precheckout-adapter-v1)"
    ) in capsys.readouterr().out


@pytest.mark.parametrize(
    ("acceptance", "message"),
    [
        (
            'riesgo_aceptado_por = "aceptacion de prueba (tests)"\n',
            "la aceptacion del riesgo lleva riesgo_aceptado_por, riesgo_aceptado_el y "
            "riesgo_contrato",
        ),
        (
            _TEST_ACCEPTANCE.replace("adapter-v1", "adapter-v0"),
            "riesgo_contrato debe ser 'ghl-precheckout-adapter-v1'",
        ),
        (
            _TEST_ACCEPTANCE.replace("2026-10-01", '"2026-10-01"'),
            "riesgo_aceptado_el debe ser una fecha",
        ),
    ],
    ids=["incompleta", "otro-contrato", "fecha-como-texto"],
)
def test_an_incomplete_or_stale_acceptance_is_an_error(
    att1_copy: Path, acceptance: str, message: str
) -> None:
    _add_ghl_adapter(att1_copy, ["EgDqRl2xWc59YjVW1q8W"], acceptance=acceptance)

    report = validate_instance(att1_copy)

    assert report["valida"] is False
    assert message in report["errores"][0]
    assert main(["validate", str(att1_copy)]) == 1


@pytest.mark.parametrize("flow", ["precheckout", "pago_fallido"])
def test_without_the_section_a_flow_that_uses_intents_is_a_warning(
    att1_copy: Path, flow: str
) -> None:
    # Sin [adaptadores.ghl] nada bloquea (D6): si la instancia uso el adaptador,
    # sus intenciones siguen en la base. validate lo avisa, como el arranque y /ready.
    _add_intent_event(att1_copy)
    _turn_on(att1_copy, flow)

    report = validate_instance(att1_copy)

    assert report["valida"] is True
    assert "adaptadores" not in report["manifiesto"]
    [warning] = [w for w in report["avisos"] if "no_adapter_section" in w]
    assert warning.startswith(
        f"hay flujos prendidos que usan la intencion como permiso ({flow}) y no hay "
        "[adaptadores.ghl]"
    )


def test_without_the_section_and_without_such_a_flow_there_is_no_warning(
    att1_copy: Path,
) -> None:
    _add_intent_event(att1_copy)
    _turn_on(att1_copy, "carrito")

    report = validate_instance(att1_copy)

    assert report["valida"] is True
    assert not any("adaptadores.ghl" in warning for warning in report["avisos"])


def test_report_has_no_adapter_without_the_section() -> None:
    report = validate_instance(ATT1)

    assert "adaptadores" not in report["manifiesto"]


def test_ghl_adapter_without_intencion_is_an_error(att1_copy: Path) -> None:
    path = att1_copy / "instancia.toml"
    path.write_text(
        path.read_text(encoding="utf-8") + '\n[adaptadores.ghl]\nformularios = ["EgDqRl2xWc59YjVW1q8W"]\n',
        encoding="utf-8",
    )

    report = validate_instance(att1_copy)

    assert report["valida"] is False
    assert "adaptadores.ghl exige el evento 'intencion'" in report["errores"][0]
