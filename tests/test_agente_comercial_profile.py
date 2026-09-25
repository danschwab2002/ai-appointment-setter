from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOUL = ROOT / "profiles" / "agente-comercial" / "SOUL.md"
BRAND_VOICE_EXAMPLES = (
    ROOT / "docs" / "design" / "lancemos-brand-voice-examples-v0.md"
)


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


def test_agente_comercial_profile_answers_basic_product_content_directly() -> None:
    content = SOUL.read_text(encoding="utf-8")
    normalized = " ".join(content.split())

    for required in (
        "Fase 1 — Entiende y calma",
        "Fase 2 — Desarma y renueva",
        "Fase 3 — Restaura y sostén",
        "Botiquín para la crisis",
        "Cuaderno de Restauración",
        "Test de evaluación",
        "Clase de fe y restauración",
        "respondé directamente con esta lista",
        "sin derivar el caso sólo por esa pregunta",
    ):
        assert required in normalized

    assert "No están confirmados para esta release: contenido detallado" not in normalized
    assert "El contenido detallado del programa no está confirmado" not in normalized


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


def test_agente_comercial_profile_avoids_observed_e2e_misrepresentations() -> None:
    content = SOUL.read_text(encoding="utf-8")
    normalized = " ".join(content.split())

    for required in (
        "No afirmes que no almacenás datos personales",
        "No prometas que la revisión humana gestionará una devolución",
        "envía exactamente `/nuevo`",
        "No digas que no hay comandos disponibles",
    ):
        assert required in normalized

    for prohibited_example in (
        "no almaceno datos personales",
        "no tengo comandos disponibles",
        "gestionar la devolución correctamente",
    ):
        assert f"No uses: “{prohibited_example}”" in normalized


def test_agente_comercial_profile_has_provisional_johanna_brand_voice() -> None:
    content = SOUL.read_text(encoding="utf-8")
    normalized = " ".join(content.split())

    for required in (
        "Brand Voice provisional V0",
        "subordinada al kernel, la política, los facts y el contrato JSON",
        "español latino neutral compatible con Ecuador",
        "tratamiento de `tú`",
        "`quieres`, `puedes`, `responde`, `te envío`",
        "`querés`, `podés`, `respondé`, `acá tenés`",
        "cercano, profesional y sereno",
        "un solo siguiente paso o una elección simple",
        "`Psic. Johanna`",
        "provisional y todavía no ratificada por Johanna",
    ):
        assert required in normalized


def test_provisional_brand_voice_examples_preserve_fail_closed_policy() -> None:
    content = BRAND_VOICE_EXAMPLES.read_text(encoding="utf-8")

    assert "Este caso requiere una revisión humana para verificar la situación." in content
    assert "La derivación al equipo quedó confirmada." in content
    assert "Puedo ayudarte con los pasos generales para intentarlo nuevamente." not in content
    assert "podrán continuar por este mismo medio" not in content


def test_profile_explains_what_to_do_with_a_human_agent_message() -> None:
    # El bridge empezo a mostrarle al agente lo que escribio una persona del
    # equipo (actor `human_agent`). Sin esta seccion, el modelo lo leeria como
    # propio y sostendria promesas que no hizo.
    soul = SOUL.read_text(encoding="utf-8")
    compacto = " ".join(soul.split())

    assert "`human_agent`" in compacto
    assert "una persona del equipo" in compacto
    for regla in ("no lo repitas", "no lo contradigas"):
        assert regla in compacto
    assert 'reason_code="commercial_exception"' in compacto
    # Y el tope de contexto de Hermes son 20.000 caracteres.
    assert len(soul) < 20_000


def test_profile_no_habla_de_piloto_ni_de_prueba_privada() -> None:
    # El SOUL atiende leads reales desde el 20/09, pero seguia redactado como
    # piloto cerrado. Claude filtraba esas frases; GLM 5.2 se las decia al lead
    # ("el precio confirmado para esta prueba es USD 49", medido el 25/09 con
    # el harness sobre casos reales). El documento no puede hablar de si mismo
    # como prueba, release o piloto: el modelo lo repite.
    soul = SOUL.read_text(encoding="utf-8")
    compacto = " ".join(soul.split())

    for prohibido in (
        "piloto controlado",
        "prueba privada",
        "único usuario autorizado",
        "para esta prueba",
        "para la prueba",
        "para esta release",
        "de esta release",
        "por esta release",
        "Esta release",
        "volver funcional la prueba",
    ):
        assert prohibido not in compacto, prohibido

    # `/nuevo` se explica solo si la persona lo menciona (Transparencia
    # operacional). Como "patron preferido" el modelo lo agregaba sin motivo.
    preferidos = compacto.split("Patrones preferidos:", 1)[1].split("Patrones prohibidos:", 1)[0]
    assert "/nuevo" not in preferidos
    assert "envía exactamente `/nuevo`" in compacto
