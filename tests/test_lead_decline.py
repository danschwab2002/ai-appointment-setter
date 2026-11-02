"""El «no» claro de la persona (F-150, E-263): la lista corta de rechazos.

Los textos de las conversaciones reales de ATT1 y Maribel son los que la
persona escribio, tal cual (sin nombres ni telefonos; los dejo el diagnostico
del 01/11, ``documentacion/2026-11-01-tres-errores-de-mensajes-en-conversaciones-reales.md``):

* ATT1 #542 (28/10, despues de C1, la ficha de OXXO): «Hola buenas tardes.
  Cambie de opinión. Ya no quiero el curso.. gracias», y despues «Muchas
  gracias».
* ATT1 #491 (28/10, despues de Z): «Mil gracias pero si es ahorita muy caro para
  mi saludos» (una objecion de precio) y «Gracias 🙏, será en otra ocasión
  Saludos» (el rechazo).
* Maribel #9 (28 y 29/10): «Hola, voy a hacer el pago en OXXO. Muchas gracias.
  🌸», «¿Incluye las actualizaciones y poderla ver siempre?», «Sí, ya lo vi.
  Muchas gracias. 🌸»: ningun rechazo (lo frena la ficha, no esta lista).
"""

from __future__ import annotations

import pytest

from bridge.lead_decline import LEAD_DECLINED, detect_lead_decline
from bridge.opt_out import detect_explicit_opt_out

ATT1_542 = "Hola buenas tardes. Cambie de opinión. Ya no quiero el curso.. gracias"
ATT1_491_PRICE = "Mil gracias pero si es ahorita muy caro para mi saludos"
ATT1_491_NO = "Gracias 🙏, será en otra ocasión Saludos"
MARIBEL_9 = (
    "Hola, voy a hacer el pago en OXXO. Muchas gracias. 🌸",
    "¿Incluye las actualizaciones y poderla ver siempre?",
    "Gracias, buenos días.",
    "Sí, ya lo vi. Muchas gracias. 🌸",
)


def test_the_motive_is_its_own() -> None:
    assert LEAD_DECLINED == "lead_declined"


def test_542_said_no_to_the_course() -> None:
    match = detect_lead_decline([ATT1_542, "Muchas gracias"])
    assert match is not None
    assert match.message_index == 0
    assert match.rule_key in {"changed_mind", "no_longer_wants"}


def test_491_another_time_is_a_no_and_the_price_objection_is_not() -> None:
    assert detect_lead_decline([ATT1_491_PRICE]) is None
    match = detect_lead_decline([ATT1_491_PRICE, ATT1_491_NO])
    assert match is not None
    assert (match.rule_key, match.message_index) == ("another_time", 1)


def test_maribel_9_announced_the_payment_and_never_said_no() -> None:
    assert detect_lead_decline(list(MARIBEL_9)) is None


@pytest.mark.parametrize(
    "text",
    [
        "Ya no me interesa",
        "ya no me interesa, gracias",
        "La verdad ya no me interesa el curso",
        "No me interesa",
        "No estoy interesada",
        "Ya no quiero el curso",
        "No quiero el programa, gracias",
        "Cambié de opinión",
        "Cambie de idea. Saludos",
        "Será en otra ocasión",
        "Mejor en otra ocasión, bendiciones",
        "Hola. Lo dejo para otra ocasión",
    ],
)
def test_a_clear_no_counts(text: str) -> None:
    assert detect_lead_decline([text]) is not None


@pytest.mark.parametrize(
    "text",
    [
        # Una objecion, una duda o un «mas adelante»: los atiende el agente, y
        # el «mas adelante» es del estado comercial (F-046).
        ATT1_491_PRICE,
        "No me interesa por ahora, tal vez el mes que viene",
        "Ahorita no tengo dinero",
        "No sé si me sirve",
        "no, si me interesa",
        "no quiero",
        "¿Ya no me interesa?",
        "¿Y si cambié de opinión?",
        "«Ya no me interesa»",
        "> ya no me interesa",
        "Muchas gracias",
        "Ok",
        "",
    ],
)
def test_anything_else_does_not_count(text: str) -> None:
    assert detect_lead_decline([text]) is None


def test_no_thanks_counts_only_as_the_answer_to_our_template() -> None:
    # La primera respuesta a la plantilla: rechaza la oferta.
    assert detect_lead_decline(["No, gracias"]).rule_key == "no_thanks"  # type: ignore[union-attr]
    assert detect_lead_decline(["No gracias"]).rule_key == "no_thanks"  # type: ignore[union-attr]
    # Mas adelante en la charla suele contestar «¿alguna otra duda?».
    assert detect_lead_decline(["¿Aceptan PayPal?", "No, gracias"]) is None


def test_a_decline_is_not_an_opt_out_and_an_opt_out_stays_an_opt_out() -> None:
    # La baja no cambia: sus frases siguen siendo baja, y un rechazo no lo es.
    for text in (ATT1_542, ATT1_491_NO, "Ya no me interesa", "No, gracias"):
        assert detect_explicit_opt_out([text]) is None
    assert detect_explicit_opt_out(["No me escriban más"]) is not None
    assert detect_explicit_opt_out(["Ya no me interesa. No me escriban más"]) is not None


def test_what_is_not_text_or_too_long_does_not_count() -> None:
    assert detect_lead_decline([None, 42, {"content": "ya no me interesa"}]) is None
    assert detect_lead_decline(["ya no me interesa " + "x" * 2_000]) is None
