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


def test_agente_comercial_profile_has_restrictive_human_review_policy() -> None:
    content = SOUL.read_text(encoding="utf-8")
    normalized = " ".join(content.split())

    for required in (
        "No hagas preguntas por defecto",
        "como máximo una pregunta breve de orientación",
        "Ante la duda entre responder y derivar, derivá",
        'decision="handoff"',
        'qualification_status="needs_human"',
        "explicit_human_request",
        "commercial_exception",
        "policy_requires_human",
        "no digas que la persona ya fue derivada",
        "human_handoff_confirmed",
    ):
        assert required in normalized

    assert "`decision` siempre es `ask_question`" not in content
    assert "No inventes handoff" not in content
