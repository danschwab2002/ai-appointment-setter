"""Las guías del Automation Expert con lo que el motor nativo ejecuta desde E-483 (C11).

El motor nativo manda hasta tres mensajes con espera en el carrito, el primer contacto y el pago
rechazado (con `contesta` en `termina.si`), el pago rechazado completo por motivo, el cupón propio
por persona, `prioridad.le_gana_a` y `en_curso: cancelar`, cada uno detrás de su flag. Las guías
tienen que nombrarlo y no pueden seguir diciendo que eso no se puede o que lo arma el equipo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "profiles" / "automation-expert" / "skills"
FLOWS = ("crear-un-flujo", "cambiar-un-flujo", "pasar-flujo-al-sistema-nuevo")


def _skill(name: str, category: str = "flujos") -> str:
    return (SKILLS / category / name / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", FLOWS)
def test_each_flow_skill_names_what_the_native_engine_now_runs(name: str) -> None:
    text = _skill(name)
    for needed in ("`contesta`", "cupon: personal}", "le_gana_a", "`en_curso: cancelar`",
                   "`native_multitouch_not_running`"):
        assert needed in text, (name, needed)


def test_no_guide_says_the_team_builds_what_the_engine_already_runs() -> None:
    guides = "\n".join(_skill(name) for name in (*FLOWS, "programar-un-envio")) + _skill("hotmart", "plataformas")
    for stale in ("lo arma el equipo", "los arma el equipo", "hoy un flujo manda un solo mensaje",
                  "Tampoco un flujo de más de un mensaje", "sin segundo mensaje", "el segundo todavía no",
                  "todavía no va en un flujo del sistema nuevo", "Cada versión es una sola plantilla",
                  "Un solo mensaje por persona: la condición elige cuál"):
        assert stale not in guides, stale


def test_create_a_flow_has_the_cart_of_three_messages_as_the_example() -> None:
    text = _skill("crear-un-flujo")
    page, how = text.split("## Qué hace hoy y cómo se escribe", 1)
    # En la charla, en lenguaje de negocio: A1, a las 24 h A2 y a las 72 h Z, y que contestar lo corta.
    assert "a las 24 horas" in page and "a las 72 horas" in page and "si contesta" in page
    # En la segunda parte, cómo se escribe: las esperas son desde el mensaje anterior.
    assert "{esperar: 24h}" in how and "{esperar: 48h}" in how
    assert "termina: {si: [compra, contesta]}" in how
    assert "boton: {link: checkout, cupon: personal}" in how
    for variable in ("`precio`", "`vence`", "`cuando`"):
        assert variable in how, variable
    # Lo que sigue sin poder, nombrado.
    missing = how.split("**Todavía no hace**", 1)[1].split("\n\n", 1)[0]
    for needed in ("la ficha de pago", "la reactivación", "`derivar`", "`migrar`", "cupón propio"):
        assert needed in missing, needed


def test_a_flag_that_is_off_is_told_with_the_text_of_the_block() -> None:
    text = _skill("crear-un-flujo")
    for code in ("`native_multitouch_not_running`", "`native_personal_coupon_not_running`"):
        line = next(x for x in text.splitlines() if code in x)
        assert "repetí" in line and "no inventes" in line, code


def test_change_a_flow_proposes_cancel_when_the_old_version_had_an_error() -> None:
    text = _skill("cambiar-un-flujo")
    line = next(x for x in text.splitlines() if "`en_curso: cancelar`" in x)
    assert "error" in line and "`en_curso_not_supported`" in line and "segundo paso" in line
    assert "`native_change_priority_wins_ref`" in text


def test_moving_a_legacy_flow_of_several_messages_has_no_double_messages() -> None:
    text = _skill("pasar-flujo-al-sistema-nuevo")
    block = text.split("**Un flujo de varios mensajes**", 1)[1].split("\n\n", 1)[0]
    steps = ["grupo de prueba", "`prender`", "24 h", "pausa el del sistema anterior"]
    positions = [block.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "el que ya empezó sigue" in block and "no sale un mensaje doble" in block


def test_the_native_payment_slip_is_written_in_the_guides() -> None:
    """E-531 (E-528, C4): la ficha nativa, con su flag, su forma exacta y lo que no corta."""
    text = _skill("crear-un-flujo")
    how = text.split("## Qué hace hoy y cómo se escribe", 1)[1]
    line = next(x for x in how.splitlines() if "{evento: ficha_impresa}" in x)
    for needed in ("`FLOW_ENGINE_NATIVE_PAYMENT_SLIP_ENABLED`", "termina: {si: [compra, ficha_vencida], vence: 4d}",
                   '{esperar: {vencimiento: {dias_antes: 0, a_las: "09:00"}}}', "`boton: {link: ficha}`",
                   "`medio_de_pago`", "`vencimiento`", "sin `contesta`", "nunca `ficha`"):
        assert needed in line, needed
    # Ya no se dice que la ficha sigue solo en el sistema anterior.
    assert "(la ficha de pago, la reactivación;" not in text
    assert "**Todavía no hace** la ficha de pago ni la reactivación" not in text
    assert "**La ficha de pago**" in _skill("pasar-flujo-al-sistema-nuevo")
    assert "`vencimiento`" in _skill("pedir-plantilla-a-meta")
