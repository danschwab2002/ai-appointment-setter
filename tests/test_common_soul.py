"""SOUL comun del agente comercial (setter-producto-instalable-v1 §4.4).

El SOUL comun es el de Johanna sin lo que es de Johanna. Las secciones que no
dependen del cliente tienen que seguir siendo las mismas, byte a byte: si una
cambia en un lado y no en el otro, las dos instancias dejan de comportarse igual.

Desde el 2026-10-24 (decision de Dan, D1 a D4 de los arreglos del agente, y la
presentacion) el SOUL comun se aparta de Johanna a proposito en dos cosas: lo de
comprar se resuelve y no se deriva (el corte minimo de F-046), y el agente se
presenta como parte del equipo y dice la verdad si le preguntan si es una IA.
Johanna no cambia: su SOUL es otro archivo y la decision no la toca. Cada
diferencia queda escrita abajo, texto por texto, para que cualquier otra siga
rompiendo el test.

Estas pruebas leen el texto del SOUL: prueban que la regla esta escrita, no que
el modelo la cumpla. Lo que contesta el modelo se mide aparte, con el arnes del
conocimiento de la instancia.
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
        "### 2. Carrito abandonado",
        "### Comunicación de la derivación",
        "## Transparencia operacional del chat",
    ],
)
def test_invariant_sections_are_identical_to_johanna(heading: str) -> None:
    assert _section(COMMON, heading) == _section(JOHANNA, heading)


# Las diferencias con Johanna, seccion por seccion: (texto de Johanna, texto del
# SOUL comun). Se aplican en orden y cada texto de Johanna aparece una sola vez.
#
# - La 1.6.0 le da al SOUL comun la guia de pagos del conocimiento ([pagos]) y
#   el ultimo pago rechazado en la entrada (recent_payment_failure). Johanna no
#   tiene ninguna de las dos: su conocimiento vive en su SOUL y su bridge no
#   corre el runtime con manifiesto. La 2b (varios productos por aliada) suma en
#   la entrada `products` y `person_activity`.
# - El 2026-10-24 (corte minimo de F-046): lo de comprar se resuelve, el link va
#   cuando hay intencion de compra y nunca en salud ni angustia, y el descuento
#   deja de ser una condicion de derivacion. Para que entre bajo el corte de
#   Hermes (20.000 caracteres) se acortaron, sin cambiar la regla, la entrada y
#   dos parrafos de la politica.
# - El 2026-10-24 (F-036, D9 y D10): la entrada suma `opted_out_contact`, quien
#   se dio de baja y escribio por su cuenta. Johanna no lo recibe: el bridge
#   solo lo manda con manifiesto.
# - El 2026-11-02 (E-301, P-11, D11 de Dan): la ruta «Es cliente» y la entrada
#   `customer_status` (el bridge la manda con AGENT_CUSTOMER_STATUS_ENABLED). La
#   regla «si dice que ya compro» de «Cuando no pedirlo» queda absorbida por la
#   ruta, y para que entre bajo el corte se acortaron, sin cambiar la regla, la
#   entrada (`payment_link_action` remite a «Enlace de pago», que ya lo dice) y
#   la descripcion de `recent_payment_failure` y de `person_activity`.
_DIFFERENCES: dict[str, list[tuple[str, str]]] = {
    "### 1. Inbound regular": [
        (
            """simple: seguí la sección “Enlace de pago”. Si falta conocimiento aprobado o""",
            """simple: seguí la sección “Enlace de pago”. Las preguntas de compra también:
seguí “Lo de comprar se resuelve”. Si en otro tema falta conocimiento aprobado o""",
        ),
    ],
    "### 3. Compra fallida": [
        (
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

Si contesta de su banco (lo nombra, ya habló con él, se lo rechazó, qué
decirle), no derives: sugerí reintentar tras habilitar las compras en línea en
la app del banco, otra tarjeta u otro medio, y pedí el enlace.

Derivá si dice que le cobraron, si su caso no está en la sección, si ya probó lo
que la sección indica y sigue sin poder pagar, o si el bloque no trae la sección.
Al derivar, indicá que ese detalle requiere revisión humana sin prometer plazo.""",
        ),
    ],
    "### Cuándo no pedirlo": [
        (
            """- Si la persona dice que ya compró, que le cobraron o que su pago falló, seguí
  la ruta de compra fallida o derivá; no le mandes un enlace nuevo.""",
            """- Si ya compró ese producto, según `customer_status` o ella: “Es cliente”.""",
        ),
        (
            """- Si junto con el enlace pide un descuento, una cuota, una excepción o una
  condición especial, derivá: sigue siendo una excepción comercial.""",
            """- Si la persona habla de un límite de salud o escribe con angustia, no lo
  pidas aunque pregunte el precio: seguí “Seguridad” y los límites sensibles.
- Si junto con el enlace pide una excepción o una condición especial que el
  bloque no trae, derivá. Un descuento o una cuota no lo son: ver “Lo de
  comprar se resuelve”.""",
        ),
    ],
    "## Entrada": [
        (
            """Cada mensaje trae `sent_at`, la fecha y hora en que se mandó (ISO 8601, UTC).
Tenela en cuenta: una conversación puede tener días o semanas de historia.
Tomá como «ahora» el `sent_at` del último mensaje de `prospect` y leé el resto
en relación a eso. Lo de hace días es contexto, no el pedido de hoy.""",
            """Cada mensaje trae `sent_at` (ISO 8601, UTC). Tomá como «ahora» el del último
mensaje de `prospect`: lo de hace días es contexto, no el pedido de hoy.""",
        ),
        (
            """Puede incluir además `payment_link_action`. Si viene con `enabled: true`, el
sistema puede enviar el enlace de pago cuando lo pidas con
`decision="send_payment_link"`; `bridge_injects_exact_url: true` y
`agent_must_not_include_url: true` significan que el enlace lo agrega el
sistema y que tu `reply` no debe contener ninguna URL. Si no viene, el envío
del enlace está deshabilitado.""",
            """Puede incluir además `payment_link_action`: ver “Enlace de pago”.

Puede incluir además `recent_payment_failure`: el último pago rechazado de este
número en la última semana. Trae `failed_at`, `payment_method` (`CREDIT_CARD`,
`PAYPAL`…), `installments`, `refusal_reason` (texto del medio, puede faltar) y
`checkout_country` (ISO). Usalo en la ruta de compra fallida: podés decir el
medio, pero no cites el rechazo ni los códigos. Si no viene, no sabés si hubo
un pago rechazado.

Si la aliada declara productos, trae `products` (`key` y `name`, como en
«# Producto: …») y puede traer `person_activity`: lo que hizo este número en 30
días, con `activity` (`form`, `cart`, `payment_rejected`, `payment_slip_printed`,
`payment_slip_expired`, `purchase`), `product` y `at`. Usalo para saber de qué
producto habla, sin recitárselo.

Puede traer `customer_status`: lo que compró, con `product`, `status`
(`cliente`, `reembolsada`, `contracargo`), `since` y `source`. Si no viene, no
sabés si compró.

Con `opted_out_contact: true`, la persona pidió no recibir mensajes y escribió
por su cuenta: contestá normal, sin ofrecer seguimiento ni pedir permiso para
escribirle, y pedí el enlace sólo si ella lo pide.""",
        ),
    ],
}


def _apply(johanna: str, changes: list[tuple[str, str]]) -> str:
    for johanna_text, common_text in changes:
        assert johanna.count(johanna_text) == 1, johanna_text[:60]
        johanna = johanna.replace(johanna_text, common_text)
    return johanna


@pytest.mark.parametrize("heading", sorted(_DIFFERENCES))
def test_sections_differ_from_johanna_only_in_the_written_differences(heading: str) -> None:
    assert _section(COMMON, heading) == _apply(_section(JOHANNA, heading), _DIFFERENCES[heading])


def test_output_contract_differs_only_in_the_neutral_reason_code_and_the_product() -> None:
    # La 2b: con `products` en la entrada, el pedido del link lleva `product`.
    # Sin productos declarados, la salida es la de Johanna.
    # E-328: el motivo de la derivacion por riesgo para la persona
    # (`safety_risk`, ver “Seguridad”), que el bridge reconoce para mandarle
    # el reply. Johanna no lo tiene.
    johanna = _section(JOHANNA, "## Salida obligatoria").replace(
        "johanna_e2e_response", "commercial_response"
    ).replace(
        "- No agregues ni elimines claves.\n",
        "- No agregues ni elimines claves. Única excepción: con `products` en la\n"
        "  entrada y `decision=\"send_payment_link\"`, va también `product` (ver “Enlace\n"
        "  de pago”).\n",
    ).replace(
        "  revisión particular o cualquier otra condición restrictiva.\n",
        "  revisión particular o cualquier otra condición restrictiva; `safety_risk`:\n"
        "  ver “Seguridad”.\n",
    )

    assert _section(COMMON, "## Salida obligatoria") == johanna


_RESOLUTION_POLICY: list[tuple[str, str]] = [
    (
        # E-486: recortes sin cambiar la regla, para que entre la ruta del banco.
        """Tu comportamiento comercial es restrictivo. No intentes resolver todas las
conversaciones por tu cuenta.""",
        "Tu comportamiento comercial es restrictivo: no resolvés todo por tu cuenta.",
    ),
    (
        """Sólo podés resolver autónomamente cuando se cumplen todas estas condiciones:

- entendés claramente qué necesita la persona;
- la situación corresponde inequívocamente a un caso comercial permitido;
- todos los datos necesarios están confirmados en este documento;
- existe una respuesta o procedimiento aprobado para ese caso;
- no necesitás inferir, completar, diagnosticar, investigar ni inventar nada;
- no existe contradicción, excepción ni señal de complejidad;
- podés responder de forma breve, segura y suficiente.""",
        """Sólo resolvés por tu cuenta cuando entendés qué necesita la persona, es un caso
comercial permitido, los datos están en el bloque «Conocimiento aprobado», hay
una respuesta aprobada, no tenés que inferir ni inventar nada, no hay
contradicción ni complejidad y podés responder breve y seguro.""",
    ),
    (
        """Podés hacer como máximo una pregunta breve de orientación únicamente cuando el
mensaje sea ambiguo, todavía pueda corresponder a un caso simple permitido, una
sola aclaración no sensible permita identificarlo y no haya señales de
complejidad. La pregunta sólo sirve para elegir entre casos permitidos; no la
uses para investigar ni reconstruir un problema. Si la respuesta sigue siendo
ambigua, incompleta o compleja, solicitá derivación. No hagas una segunda ronda
de preguntas para evitar derivar.""",
        """Podés hacer como máximo una pregunta breve de orientación, sólo si el mensaje
es ambiguo y una aclaración no sensible permite identificar un caso simple
permitido. No la uses para investigar ni reconstruir un problema; si la
respuesta sigue ambigua o compleja, derivá. No hagas una segunda ronda.""",
    ),
    (
        """Una respuesta plausible o probablemente correcta no es suficiente. No hagas
preguntas por defecto. Si la intención es clara, el caso es simple y disponés de
una respuesta aprobada y suficiente, respondé directamente.""",
        """Una respuesta plausible o probablemente correcta no es suficiente. No hagas
preguntas por defecto.""",
    ),
    (
        "(pedir el enlace de compra no entra acá: ver “Enlace de pago”);",
        "(pedir el enlace de compra no entra acá: ver “Enlace de pago”; un pago\n"
        "  rechazado que la sección «Pagos» resuelve tampoco: ver “Compra fallida”);",
    ),
    (
        "- solicitan una excepción, descuento, devolución, cambio o condición especial;",
        "- solicitan una excepción, devolución, cambio o condición especial;",
    ),
    (
        """- el caso requiere conocimiento, autoridad o herramientas que no tenés.
""",
        """- el caso requiere conocimiento, autoridad o herramientas que no tenés.

Lo de comprar no entra en esta lista aunque falte un dato: seguí “Lo de comprar
se resuelve”.
""",
    ),
    (
        """mensaje y el tramo inmediato. Un pedido de excepción, descuento o condición
especial de días atrás no obliga a derivar de nuevo si el último mensaje no lo
retoma; respondé a lo que pregunta hoy. Si lo vuelve a pedir, derivá otra vez.""",
        """mensaje y el tramo inmediato. Un pedido de excepción o condición especial de
días atrás no obliga a derivar de nuevo si el último mensaje no lo retoma;
respondé a lo que pregunta hoy. Si lo vuelve a pedir, derivá otra vez.""",
    ),
    (
        "datos sensibles. Ante la duda entre responder y derivar, derivá.",
        "datos sensibles. Ante la duda entre responder y derivar, derivá, salvo en lo\nde comprar.",
    ),
]


def test_resolution_policy_differs_only_in_where_the_facts_live_and_in_buying() -> None:
    johanna = _section(JOHANNA, "## Política comercial de resolución y derivación humana")
    common = _section(COMMON, "## Política comercial de resolución y derivación humana")

    # La subseccion de comunicacion se compara aparte.
    assert common.split("### Comunicación de la derivación")[0] == _apply(
        johanna.split("### Comunicación de la derivación")[0], _RESOLUTION_POLICY
    )


# --- El corte minimo de F-046 (2026-10-24, D1 a D4) ---------------------------

BUYING = _section(COMMON, "## Lo de comprar se resuelve")
ASK_FOR_LINK = _section(COMMON, "### Cuándo pedirlo")
DONT_ASK_FOR_LINK = _section(COMMON, "### Cuándo no pedirlo")


@pytest.mark.parametrize(
    ("message", "rule"),
    [
        ("¿Cuánto sale?", "- pregunte el precio, los medios de pago, las cuotas o la garantía;"),
        ("¿Cómo pago?", "- pida el enlace, el link, el acceso o cómo pagar, en cualquier forma;"),
        ("¿Tiene garantía?", "- pregunte el precio, los medios de pago, las cuotas o la garantía;"),
        ("Quiero comprarlo", "- diga que quiere comprar, inscribirse, continuar o retomar la compra de la"),
    ],
)
def test_a_buying_question_asks_for_the_link_in_the_same_turn(message: str, rule: str) -> None:
    # D2: el link va cuando la persona muestra intencion de compra.
    assert rule in ASK_FOR_LINK, message


def test_the_reply_answers_the_question_before_the_link() -> None:
    assert "contestá lo que preguntó\n  con lo confirmado y decí que le envías el enlace" in COMMON


def test_a_discount_question_without_a_coupon_gets_the_price_and_the_link() -> None:
    # D4: «¿hay descuento?» sin cupon para el agente: el precio es ese, con el link, sin derivar.
    assert "**«¿Hay descuento?»:** si el bloque no trae uno para esta persona" in BUYING
    assert "precio es ese, con los medios, y pedí el enlace, sin derivar." in BUYING
    assert "Un descuento o una cuota no lo son" in DONT_ASK_FOR_LINK


@pytest.mark.parametrize(
    "case",
    [
        "Precio, qué incluye, formato, medios, cuotas, garantía, la desconfianza en el\npago y «no tengo el dinero» se contestan, no se derivan.",
        "Si falta un dato de\ncompra, contestá con lo que sí está y pedí el enlace.",
        "**«No tengo el dinero»:**",
        "**Desconfianza en el pago:** confianza, no otro canal",
    ],
)
def test_buying_is_resolved_not_handed_off(case: str) -> None:
    assert case in BUYING


def test_health_or_distress_never_gets_the_link() -> None:
    # «Tengo diabetes, ¿puedo hacer ayuno?»: no va el link aunque pregunte el precio.
    assert (
        "- Si la persona habla de un límite de salud o escribe con angustia, no lo\n"
        "  pidas aunque pregunte el precio: seguí “Seguridad” y los límites sensibles."
    ) in DONT_ASK_FOR_LINK


def test_what_needs_a_person_is_still_handed_off() -> None:
    assert (
        "Sí se derivan la salud, un reembolso, un reclamo, «ya me cobraron» y el pedido\n"
        "de hablar con alguien."
    ) in BUYING
    assert "- la persona pide hablar con alguien;" in COMMON
    assert "- solicitan una excepción, devolución, cambio o condición especial;" in COMMON


@pytest.mark.parametrize(
    "old_rule",
    [
        "Si junto con el enlace pide un descuento, una cuota, una excepción o una",
        "- solicitan una excepción, descuento, devolución, cambio o condición especial;",
        "Ante la duda entre responder y derivar, derivá.",
        "  bloque no trae la lista, esa pregunta se deriva con",
    ],
)
def test_the_old_buying_handoffs_are_gone_from_the_common_soul_only(old_rule: str) -> None:
    assert old_rule not in COMMON
    # Johanna no cambia (D1 toca solo el SOUL comun).
    if old_rule != "  bloque no trae la lista, esa pregunta se deriva con":
        assert old_rule in JOHANNA


# --- La presentacion (2026-10-24, Dan; regla 13 del 17/10) --------------------


def test_the_agent_does_not_define_itself_as_a_virtual_assistant() -> None:
    assert "asistente virtual de un negocio" not in COMMON
    assert COMMON.startswith(
        "# Agente comercial del setter\n\nAtendés por WhatsApp, como parte del equipo de un negocio"
    )


def test_the_agent_presents_itself_as_part_of_the_team_and_never_with_a_name() -> None:
    assert (
        "- **Identidad:** presentate como parte del equipo, como dice «Te presentas\n"
        "  como», cuando corresponda; nunca como «asistente virtual», aunque el bloque lo\n"
        "  diga, ni con un nombre de persona."
    ) in COMMON


def test_the_agent_tells_the_truth_when_asked_if_it_is_a_bot() -> None:
    # «¿Sos un bot?»: lo dice, nunca lo niega, nunca inventa un nombre.
    rule = (
        "- **«¿Sos un bot?»:** si te preguntan explícitamente si sos una IA, un bot o\n"
        "  una persona, decí en una línea que sos un asistente automático del equipo y\n"
        "  seguí. Nunca lo niegues ni inventes un nombre. Si no lo preguntan, no lo\n"
        "  menciones."
    )
    assert rule in COMMON


def test_the_knowledge_block_still_carries_the_presentation_line() -> None:
    # La regla de identidad lee «Te presentas como», que arma el bridge desde [identidad].presentacion.
    source = Path(__import__("bridge.commercial_knowledge", fromlist=["x"]).__file__).read_text(encoding="utf-8")

    assert '"- Te presentas como: {self.presentation}."' in source


# ------------------------------------------------------------------------------


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


def test_an_opted_out_contact_is_answered_without_follow_up_and_gets_the_link_only_on_request() -> None:
    # F-036 (D9 y D10, 2026-10-24): quien se dio de baja y escribe por su cuenta
    # recibe respuesta normal; nunca se le ofrece seguimiento ni se le pide
    # permiso para escribirle, y el link va solo si lo pide (no por preguntar el
    # precio). Johanna no tiene la regla: su bridge no manda el dato.
    rule = (
        "Con `opted_out_contact: true`, la persona pidió no recibir mensajes y escribió\n"
        "por su cuenta: contestá normal, sin ofrecer seguimiento ni pedir permiso para\n"
        "escribirle, y pedí el enlace sólo si ella lo pide."
    )
    assert rule in _section(COMMON, "## Entrada")
    assert "opted_out_contact" not in JOHANNA


def test_common_soul_stays_under_the_hermes_truncation() -> None:
    # Hermes deja los primeros 20.000 caracteres del SOUL (medido el 2026-10-01).
    assert len(COMMON) < 20_000


def test_payment_guide_names_the_knowledge_section_the_bridge_renders() -> None:
    source = Path(__import__("bridge.commercial_knowledge", fromlist=["x"]).__file__).read_text(encoding="utf-8")

    assert "la sección «Pagos»" in COMMON
    assert '"## Pagos"' in source


def test_the_agent_never_confirms_an_opt_out_and_points_to_the_phrase_the_system_takes() -> None:
    # Prueba del entrante de Maribel (2026-10-27 12:31Z, v1.20.1): con «no me
    # hablen más» el sistema no registro la baja y el agente contesto «Entendido,
    # no te escribiremos más». La baja la registra solo el bridge (opt_out.py);
    # el agente no la confirma y manda a la frase que el sistema si toma.
    style = _section(COMMON, "## Estilo")
    assert "Nunca prometas dejar de escribir ni confirmes una baja" in style
    assert "«No más mensajes», o derivá con `handoff`." in style


# --- La ruta «Es cliente» (E-301, P-11; decisiones de Dan del 2026-11-02) -------

CUSTOMER = _section(COMMON, "### 4. Es cliente")


def test_the_customer_route_exists_and_wins_over_buying() -> None:
    # D11: la ruta reemplaza la regla de «si dice que ya compro»; manda sobre lo de comprar.
    assert "## Los motivos conversacionales" in COMMON
    assert "Manda sobre “Lo de comprar se resuelve”." in CUSTOMER
    assert "- Si ya compró ese producto, según `customer_status` o ella: “Es cliente”." in DONT_ASK_FOR_LINK
    assert "Si la persona dice que ya compró o que le cobraron" not in COMMON


@pytest.mark.parametrize(
    ("decision", "rule"),
    [
        ("D6, D4: a una clienta no se le vende ni su enlace ni descuento",
         "- `cliente`: no se lo vendas, ni su enlace, descuento o seguimiento; atendela\n"
         "  como soporte con el bloque (acceso, contenido, formato, canal de dudas)."),
        ("D4: reembolsada o contracargo", "- `reembolsada`: sin venta ni descuento; si quiere comprar, derivá.\n"
         "  `contracargo`: derivá siempre."),
        ("D5: otro producto, el enlace solo si lo pide",
         "- Otro producto: contestá y pedí su enlace sólo si ella lo pide, sin descuento."),
        ("D7: dice que compro y no figura", "- Dice que compró y no figura: no le pidas el mail ni un enlace; contestá el\n"
         "  acceso con lo aprobado y, si no se resuelve, derivá."),
    ],
)
def test_the_customer_route_carries_dans_decisions(decision: str, rule: str) -> None:
    assert rule in CUSTOMER, decision


def test_the_customer_statuses_are_the_ones_the_base_returns() -> None:
    # La entrada nombra los tres estados de get_customer_status_v1 (migracion 20261103000700).
    from bridge.customer_status import CUSTOMER_STATUS_CONTEXT_KEY

    entry = _section(COMMON, "## Entrada")
    assert f"Puede traer `{CUSTOMER_STATUS_CONTEXT_KEY}`" in entry
    migration = next((ROOT / "supabase" / "migrations").glob("20261103000700_*.sql")).read_text(encoding="utf-8")
    for status in ("cliente", "reembolsada", "contracargo"):
        assert f"`{status}`" in entry
        assert f"'{status}'" in migration


def test_the_customer_route_does_not_write_the_handoff_notice() -> None:
    # D12 lo cumple el bridge: con decision=handoff el reply del modelo no sale
    # (F-033); a la persona le llega solo el aviso fijo de handoff_notice.
    from bridge.handoff_notice import HANDOFF_NOTICE_TEXT

    assert HANDOFF_NOTICE_TEXT not in COMMON
    assert "te escriben por acá" not in COMMON


# --- E-328: el riesgo para la persona ----------------------------------------

SAFETY = _section(COMMON, "## Seguridad")


def test_risk_for_the_person_is_handed_off_with_its_own_reason_and_the_reply_still_arrives() -> None:
    # E-301 y E-322: con «no derives», el caso 75 derivo 2 de 3 veces con
    # policy_requires_human y el bridge descartaba el reply. Ahora deriva con
    # un motivo propio y el bridge (SAFETY_HANDOFF_REPLY_ENABLED) le manda el
    # reply: la indicacion de pedir ayuda tiene que estar en el reply.
    compacto = " ".join(SAFETY.split())
    assert '`reason_code="safety_risk"`' in compacto
    for riesgo in ("hacerse daño", "no querer vivir", "dañar a otra persona", "no estar seguros"):
        assert riesgo in compacto
    assert "en `reply`, que le llega igual, indicá que busquen atención médica o ayuda de emergencia" in compacto
    assert "No evalúes el riesgo, no prometas confidencialidad y no sigas vendiendo." in compacto
    assert "no inventes teléfonos locales" in compacto
    # El motivo esta entre los de la salida.
    assert "`safety_risk`:\n  ver “Seguridad”." in _section(COMMON, "## Salida obligatoria")


def test_the_sensitive_data_rule_survives_in_the_policy_after_the_dedupe() -> None:
    # E-328 saco de «Seguridad» el «No pidas historia clinica…» para que el SOUL
    # entre bajo el corte de Hermes: la misma regla sigue en la politica.
    assert "No pidas historia clínica" not in COMMON
    assert (
        "No\nrecopiles información financiera, clínica, documentos, credenciales ni otros\ndatos sensibles."
        in COMMON
    )


# --- E-486: la respuesta del banco en la variante «banco» del pago rechazado --
#
# La plantilla de la variante «banco» pregunta «¿De qué banco es tu tarjeta? Te
# digo qué activar». El 12/11 en Maribel (conversación 260) la persona contestó
# el banco y el agente derivó. Con esta regla responde: reintentar habilitando
# las compras en línea, otra tarjeta u otro medio, y el enlace.


def test_the_bank_answer_after_a_refused_payment_is_answered_not_derived() -> None:
    route = _section(COMMON, "### 3. Compra fallida")
    bank = route.index("Si contesta de su banco")
    assert bank < route.index("Derivá si dice que le cobraron")
    rule = route[bank : route.index("Derivá si dice que le cobraron")]
    for words in (
        "lo nombra",
        "ya habló con él",
        "se lo rechazó",
        "qué\ndecirle",
        "no derives",
        "habilitar las compras en línea",
        "reintentar",
        "otra tarjeta u otro medio",
        "pedí el enlace",
    ):
        assert words in rule, words
