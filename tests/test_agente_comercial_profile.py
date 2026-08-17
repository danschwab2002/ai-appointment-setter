from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOUL = ROOT / "profiles" / "agente-comercial" / "SOUL.md"


def test_agente_comercial_profile_has_minimum_libre_de_ansiedad_release() -> None:
    content = SOUL.read_text(encoding="utf-8")
    lowered = content.lower()

    for required in (
        "Libre de Ansiedad",
        "USD 49",
        "garantía de 7 días",
        '"decision": "ask_question"',
        '"reason_code": "johanna_e2e_response"',
        "No diagnostiques",
        "no habilita contacto proactivo",
    ):
        assert required in content

    for case_name in ("inbound regular", "carrito abandonado", "compra fallida"):
        assert case_name in lowered

    assert "sin oferta activa confirmada" not in lowered
    assert "No ejecutes herramientas ni acciones externas" in content
