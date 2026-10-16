"""SOUL comun del agente comercial (setter-producto-instalable-v1 §4.4).

El SOUL comun es el de Johanna sin lo que es de Johanna. Las secciones que no
dependen del cliente tienen que seguir siendo las mismas, byte a byte: si una
cambia en un lado y no en el otro, las dos instancias dejan de comportarse igual.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON = (ROOT / "profiles" / "agente-comercial-comun" / "SOUL.md").read_text(encoding="utf-8")
JOHANNA = (ROOT / "profiles" / "agente-comercial" / "SOUL.md").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    start = lines.index(heading)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(#+) ", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end]).rstrip()


@pytest.mark.parametrize(
    "value",
    ["Johanna", "Libre de Ansiedad", "USD 49", "Ecuador", "Cuenca", "7 días", "Nina", "tiroid", "Raizana"],
)
def test_common_soul_has_no_customer_value(value: str) -> None:
    assert value.casefold() not in COMMON.casefold()


@pytest.mark.parametrize(
    "heading",
    [
        "### 1. Inbound regular",
        "### 2. Carrito abandonado",
        "### Comunicación de la derivación",
        "## Transparencia operacional del chat",
    ],
)
def test_invariant_sections_are_identical_to_johanna(heading: str) -> None:
    assert _section(COMMON, heading) == _section(JOHANNA, heading)


# La 1.6.0 le da al SOUL comun la guia de pagos del conocimiento ([pagos]) y el
# ultimo pago rechazado en la entrada (recent_payment_failure). Johanna no tiene
# ninguna de las dos: su conocimiento vive en su SOUL y su bridge no corre el
# runtime con manifiesto. Hasta que se mude al manifiesto (Etapa 2), estas son
# las unicas diferencias, texto por texto. La 2b (varios productos por aliada)
# suma en la entrada `products` y `person_activity`, que solo trae un manifiesto
# con [[productos]].
_PAYMENT_GUIDE = {
    "### 3. Compra fallida": (
        """Usá esta ruta sólo cuando la persona diga que su pago falló o fue rechazado. No
inventes la causa ni asegures que hubo un cobro. Podés pedir el texto general del
mensaje de error, pero nunca datos de tarjeta, cuenta, documento ni información
financiera sensible. No des consejo financiero. Si no existe una resolución
confirmada, indicá que ese detalle requiere revisión humana sin prometer plazo.""",
        """Usá esta ruta cuando la persona diga que su pago falló o fue rechazado, o cuando
la entrada traiga `recent_payment_failure` y la persona hable de su pago o de
comprar. No inventes la causa ni asegures que hubo un cobro. Podés pedir el texto
general del mensaje de error, pero nunca datos de tarjeta, cuenta, documento ni
información financiera sensible. No des consejo financiero.

Si el bloque trae la sección «Pagos», resolvé con ella antes de derivar:

- si `recent_payment_failure.refusal_reason` corresponde a un motivo de la
  sección, decí lo que indica para ese motivo, con tus palabras;
- ofrecé otro medio de la lista de medios activos, con el nombre que tiene en el
  país de la persona (`recent_payment_failure.checkout_country`, o el país
  principal si no lo sabés) y su paso a paso si la sección lo trae;
- si quiere volver a intentarlo, pedí el envío del enlace según “Enlace de pago”.

Derivá si dice que le cobraron, si su caso no está en la sección, si ya probó lo
que la sección indica y sigue sin poder pagar, o si el bloque no trae la sección.
Al derivar, indicá que ese detalle requiere revisión humana sin prometer plazo.""",
    ),
    "### Cuándo no pedirlo": (
        """- Si la persona dice que ya compró, que le cobraron o que su pago falló, seguí
  la ruta de compra fallida o derivá; no le mandes un enlace nuevo.""",
        """- Si la persona dice que ya compró o que le cobraron, no le mandes un enlace
  nuevo: respondé con lo aprobado o derivá. Si su pago falló, seguí la ruta de
  compra fallida; ahí el enlace sirve para volver a intentarlo.""",
    ),
    "## Entrada": (
        """del enlace está deshabilitado.""",
        """del enlace está deshabilitado.

Puede incluir además `recent_payment_failure`: el último pago rechazado de este
número en la última semana, según la plataforma de pago. Trae `failed_at`
(ISO 8601, UTC), `payment_method` (el código de la plataforma, como
`CREDIT_CARD` o `PAYPAL`), `installments`, `refusal_reason` (el texto del medio
de pago, a veces en otro idioma; puede faltar) y `checkout_country` (código ISO
del país). Usalo en la ruta de compra fallida. Podés decir con qué medio intentó
pagar, pero no cites el texto del rechazo ni los códigos. Si no viene, no sabés
si hubo un pago rechazado.

Si la aliada declara productos, trae `products`: la `key` y el `name` de cada
uno, el nombre con el que aparece en el bloque («# Producto: …»). Puede traer
además `person_activity`: lo que hizo este número en los últimos 30 días, de lo
más viejo a lo más nuevo. Cada elemento trae `activity` (`form`: llenó el
formulario; `cart`: dejó el pago sin terminar; `payment_rejected`;
`payment_slip_printed`: sacó una ficha para pagar en efectivo;
`payment_slip_expired`: la ficha venció; `purchase`: compró), `product` (una
`key` de `products`) y `at` (ISO 8601, UTC). Usalo para saber de qué producto
habla la persona, sin recitarle la lista. Si no viene, no sabés qué hizo.""",
    ),
}


@pytest.mark.parametrize("heading", sorted(_PAYMENT_GUIDE))
def test_payment_guide_sections_differ_from_johanna_only_in_the_guide(heading: str) -> None:
    johanna_text, common_text = _PAYMENT_GUIDE[heading]
    johanna = _section(JOHANNA, heading)
    assert johanna.count(johanna_text) == 1

    assert _section(COMMON, heading) == johanna.replace(johanna_text, common_text)


def test_output_contract_differs_only_in_the_neutral_reason_code_and_the_product() -> None:
    # La 2b: con `products` en la entrada, el pedido del link lleva `product`.
    # Sin productos declarados, la salida es la de Johanna.
    johanna = _section(JOHANNA, "## Salida obligatoria").replace(
        "johanna_e2e_response", "commercial_response"
    ).replace(
        "- No agregues ni elimines claves.\n",
        "- No agregues ni elimines claves. Única excepción: con `products` en la\n"
        "  entrada y `decision=\"send_payment_link\"`, va también `product` (ver “Enlace\n"
        "  de pago”).\n",
    )

    assert _section(COMMON, "## Salida obligatoria") == johanna


def test_resolution_policy_differs_only_in_where_the_facts_live() -> None:
    johanna = _section(JOHANNA, "## Política comercial de resolución y derivación humana")
    johanna = johanna.replace(
        "todos los datos necesarios están confirmados en este documento",
        "todos los datos necesarios están confirmados en el bloque «Conocimiento aprobado»",
    ).replace(
        "(pedir el enlace de compra no entra acá: ver “Enlace de pago”);",
        "(pedir el enlace de compra no entra acá: ver “Enlace de pago”; un pago\n"
        "  rechazado que la sección «Pagos» resuelve tampoco: ver “Compra fallida”);",
    )
    common = _section(COMMON, "## Política comercial de resolución y derivación humana")

    # La subseccion de comunicacion se compara aparte.
    assert common.split("### Comunicación de la derivación")[0] == johanna.split(
        "### Comunicación de la derivación"
    )[0]


def test_common_soul_names_the_knowledge_block_the_bridge_renders() -> None:
    from bridge.commercial_knowledge import CommercialKnowledge

    assert "«Conocimiento aprobado»" in COMMON
    assert CommercialKnowledge.render.__doc__ is not None
    # El render arranca con "# Conocimiento aprobado: <marca>".
    source = Path(__import__("bridge.commercial_knowledge", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    assert 'f"# Conocimiento aprobado: {self.brand}"' in source


def test_forbidden_missing_data_phrases_survive_in_the_common_soul() -> None:
    for phrase in ("No tengo información sobre…", "No tengo detalles sobre…", "Ese\n  dato todavía no lo tengo confirmado"):
        assert phrase in COMMON


def test_common_soul_stays_under_the_hermes_truncation() -> None:
    # Hermes deja los primeros 20.000 caracteres del SOUL (medido el 2026-10-01).
    assert len(COMMON) < 20_000


def test_payment_guide_names_the_knowledge_section_the_bridge_renders() -> None:
    source = Path(__import__("bridge.commercial_knowledge", fromlist=["x"]).__file__).read_text(encoding="utf-8")

    assert "la sección «Pagos»" in COMMON
    assert '"## Pagos"' in source
