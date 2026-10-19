"""A2 y Z del carrito armados por ``create_app``, y «Quiero mi 10 %» en el webhook.

Contrato: ``docs/contracts/portable-cart-followups-v1.md``.

El arranque reusa los ``Settings`` de A1 (``test_cart_discount_first_touch``):
A2 y Z siguen a A1 con cupon. El webhook reusa el arnes del entrante de ATT1
(``test_whatsapp_inbound_equivalence``): la captura del webhook de la
conversacion 200 con ``content`` reemplazado por el texto del boton, que es
como Chatwoot entrega una respuesta rapida (no hay captura de la pulsacion del
boton, la misma deuda que anota ese archivo para «No más mensajes»).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_cart_discount_first_touch import _app, _settings
from test_whatsapp_inbound_equivalence import (
    FORM_ID,
    PAYMENT_LINK_PROPOSAL,
    _manifest_settings,
    _pending,
    _process,
    _TemplateConversation,
    _text_webhook,
)

from bridge import app as app_module
from bridge.app import Settings
from bridge.cart_followups import CartFollowupConfig
from bridge.supabase import SupabaseError

A2 = "att1_carrito_dudas_01"
Z = "att1_ultima_renovacion_01"
BUTTON = "Quiero mi 10 %"


# ------------------------------------------------------------------ arranque


def _with_followups(tmp_path: Path, **overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "recovery_cart_followups_enabled": True,
        "recovery_cart_second_touch_template_name": A2,
        "recovery_cart_last_touch_template_name": Z,
    }
    fields.update(overrides)
    return _settings(tmp_path, **fields)


def test_create_app_gives_the_dispatcher_a2_and_z(tmp_path: Path) -> None:
    app = _app(_with_followups(tmp_path))

    assert app.state.durable_dispatcher._cart_followups == CartFollowupConfig(
        language="es_EC", second_touch_template=A2, last_touch_template=Z
    )
    assert "recovery_cart_followups_enabled" in (
        app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    )


def test_the_flag_is_off_by_default(tmp_path: Path) -> None:
    app = _app(_settings(tmp_path))

    assert app.state.durable_dispatcher._cart_followups is None
    assert Settings.__dataclass_fields__["recovery_cart_followups_enabled"].default is False


def test_a2_alone_does_not_need_the_coupon_worker_for_z(tmp_path: Path) -> None:
    app = _app(_with_followups(tmp_path, recovery_cart_last_touch_template_name=None))

    config = app.state.durable_dispatcher._cart_followups
    assert config.second_touch_template == A2
    assert config.last_touch_template is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"recovery_discount_first_touch_enabled": False},
            "RECOVERY_DISCOUNT_FIRST_TOUCH_ENABLED",
        ),
        (
            {
                "recovery_cart_second_touch_template_name": None,
                "recovery_cart_last_touch_template_name": None,
            },
            "RECOVERY_CART_SECOND_TOUCH_TEMPLATE_NAME",
        ),
    ],
)
def test_an_incomplete_wiring_does_not_start(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _app(_with_followups(tmp_path, **overrides))


def test_z_needs_the_coupon_worker(tmp_path: Path) -> None:
    # Sin el worker del cupon tampoco arranca A1 con cupon: lo que se prueba
    # aca es la guarda propia de Z, con la de A1 de por medio.
    with pytest.raises(ValueError, match="RECOVERY_COUPON_WORKER_ENABLED"):
        _app(_with_followups(tmp_path, recovery_coupon_worker_enabled=False))


def test_the_environment_turns_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # El minimo para leer el entorno, el mismo de test_recovery_coupon.
    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "RECOVERY_CART_FOLLOWUPS_ENABLED",
        "RECOVERY_CART_SECOND_TOUCH_TEMPLATE_NAME",
        "RECOVERY_CART_LAST_TOUCH_TEMPLATE_NAME",
    ):
        monkeypatch.delenv(name, raising=False)

    off = Settings.from_env()

    assert off.recovery_cart_followups_enabled is False
    assert off.recovery_cart_second_touch_template_name is None
    assert off.recovery_cart_last_touch_template_name is None

    monkeypatch.setenv("RECOVERY_CART_FOLLOWUPS_ENABLED", "true")
    monkeypatch.setenv("RECOVERY_CART_SECOND_TOUCH_TEMPLATE_NAME", f" {A2} ")
    monkeypatch.setenv("RECOVERY_CART_LAST_TOUCH_TEMPLATE_NAME", "")

    on = Settings.from_env()

    assert on.recovery_cart_followups_enabled is True
    assert on.recovery_cart_second_touch_template_name == A2
    assert on.recovery_cart_last_touch_template_name is None


# --------------------------------------------------------- «Quiero mi 10 %»


class _Renewal(_TemplateConversation):
    """La conversacion de A1 y A2, con la RPC de la renovacion."""

    def __init__(self, outcome: str | Exception = "advanced", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.renewal_outcome = outcome
        self.renewals: list[dict[str, object]] = []

    async def request_cart_renewal(self, **kwargs: object) -> str:
        self.renewals.append(kwargs)
        if isinstance(self.renewal_outcome, Exception):
            raise self.renewal_outcome
        return self.renewal_outcome


def _press(
    tmp_path: Path, supabase: _Renewal, text: str = BUTTON, *, enabled: bool = True
) -> tuple[Any, Any, Any]:
    webhook, history = _text_webhook(text)
    return _process(
        tmp_path,
        supabase,
        settings=replace(
            _manifest_settings(tmp_path),
            payment_link_enabled=True,
            recovery_cart_followups_enabled=enabled,
        ),
        webhook=webhook,
        history=history,
        proposal=PAYMENT_LINK_PROPOSAL,
    )


@pytest.mark.parametrize("outcome", ["advanced", "already_requested"])
def test_the_button_brings_z_forward_and_the_agent_does_not_run(
    tmp_path: Path, outcome: str
) -> None:
    supabase = _Renewal(outcome, identities=(FORM_ID,))

    chatwoot, shadow, app = _press(tmp_path, supabase)

    [renewal] = supabase.renewals
    webhook, _ = _text_webhook(BUTTON)
    assert renewal["chatwoot_account_id"] == 1
    assert renewal["chatwoot_inbox_id"] == 9
    assert renewal["chatwoot_conversation_id"] == 200
    assert renewal["chatwoot_message_id"] == webhook["id"]
    # La identidad del formulario, la misma que usa la admision.
    assert renewal["external_user_id"] == FORM_ID
    assert str(renewal["occurred_at"]).endswith("+00:00")
    # Ni la admision ni el agente: Z es la respuesta.
    assert supabase.admission_rpcs == []
    assert shadow.calls == [] and chatwoot.reply_calls == []
    assert _pending(app) == []


def test_without_a_pending_z_the_button_reaches_the_agent(tmp_path: Path) -> None:
    supabase = _Renewal("no_pending_last_touch", identities=(FORM_ID,))

    chatwoot, shadow, app = _press(tmp_path, supabase)

    assert len(supabase.renewals) == 1
    assert supabase.admission_rpcs == ["portable"]
    [(_, context)] = shadow.calls
    assert context["messages"][-1]["text"] == BUTTON  # type: ignore[index]


def test_any_other_text_never_asks_for_the_renewal(tmp_path: Path) -> None:
    supabase = _Renewal(identities=(FORM_ID,))

    _, shadow, _ = _press(tmp_path, supabase, "Quiero mi 10 % pero no tengo tarjeta")

    assert supabase.renewals == []
    assert supabase.admission_rpcs == ["portable"]
    assert len(shadow.calls) == 1


def test_with_the_flag_off_the_button_is_a_message_like_any_other(tmp_path: Path) -> None:
    supabase = _Renewal(identities=(FORM_ID,))

    _, shadow, _ = _press(tmp_path, supabase, enabled=False)

    assert supabase.renewals == []
    assert supabase.admission_rpcs == ["portable"]
    assert len(shadow.calls) == 1


def test_a_failed_renewal_keeps_the_work_for_a_retry(tmp_path: Path) -> None:
    supabase = _Renewal(SupabaseError("request_recovery_renewal_v1_failed: HTTP 503"), identities=(FORM_ID,))

    chatwoot, shadow, app = _press(tmp_path, supabase)

    assert len(supabase.renewals) == 1
    assert shadow.calls == [] and chatwoot.reply_calls == []
    [pending] = _pending(app)
    assert pending["attempts"] == 1
    assert pending["last_error_type"] == "RetryableChatwootWorkError"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # La captura del webhook de la conversacion 200.
        ("2026-09-28T18:39:45.415Z", "2026-09-28T18:39:45.415000+00:00"),
        # Un mensaje del historial de la conversacion.
        (1759084785, "2025-09-28T18:39:45+00:00"),
        ("2026-09-28T18:39:45", None),
        ("ayer", None),
        ("", None),
        (0, None),
        (True, None),
        (None, None),
    ],
)
def test_the_moment_of_the_message_is_read_in_both_forms(value: object, expected: str | None) -> None:
    assert app_module._webhook_created_at(value) == expected
