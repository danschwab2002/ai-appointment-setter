"""La respuesta escrita a A2 reactiva el cupon, y el agente sabe cual es (E-267).

El arreglo de fondo de F-148 (diagnostico:
``documentacion/2026-11-01-tres-errores-de-mensajes-en-conversaciones-reales.md``,
error 1). Lo de la base (la renovacion por respuesta y la lectura del cupon) lo
prueba ``tests/sql/followup_engine/validate_recovery_coupon_by_reply.mjs`` con
las RPC reales; aca va lo del bridge:

* que cuenta como respuesta: lo que escribio la persona despues de nuestra
  ultima plantilla, salvo una baja o un «no» claro;
* la entrada ``recovery_coupon`` del agente (codigo, porcentaje, vencimiento
  en UTC y en la hora del pais), o nada;
* el cliente de la base: lo que pide y lo que acepta;
* el webhook: con el flag, la respuesta a A2 pide la renovacion y el agente
  corre con el cupon en la entrada; sin el flag, sin manifiesto (Johanna) o con
  un «no», nada cambia.

Datos:

* las respuestas a A2 son las que escribieron de verdad en ATT1 #471 y #517
  (``conversaciones.txt`` de la muestra enmascarada del 01/11, solo el texto
  de la persona, sin nombres ni telefonos); el «no» es el de #542 y la baja, la
  del boton de A2;
* A2 sale del catalogo capturado de la bandeja 11
  (``chatwoot_message_templates_inbox_11_carrito_20261018.json``), con la forma
  del mensaje saliente de la 158 que usa ``test_whatsapp_inbound_equivalence``
  y la marca ``recovery_followup_hash`` que escribe el bridge al mandar un paso
  del caso (no hay captura de un A2 con su marca);
* el webhook es la captura de la conversacion 200 con ``content`` reemplazado
  (``_text_webhook``), como en ``test_cart_followups_wiring``.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_whatsapp_inbound_equivalence import (
    CONVERSATION_158,
    FORM_ID,
    PAYMENT_LINK_PROPOSAL,
    WA_ID,
    _johanna_settings,
    _manifest_settings,
    _process,
    _TemplateConversation,
    _text_webhook,
)

from bridge import app as app_module
from bridge.app import Settings, create_app
from bridge.approved_templates import parse_approved_template
from bridge.recovery_coupon_reply import (
    prospect_texts_after_last_template,
    recovery_coupon_context,
    reply_may_renew,
)
from bridge.supabase import (
    CART_RENEWAL_BY_REPLY_OUTCOMES,
    RecoveryCouponForConversation,
    SupabaseClient,
    SupabaseError,
)

FIXTURES = Path(__file__).parent / "fixtures"
CARRITO = json.loads(
    (FIXTURES / "chatwoot_message_templates_inbox_11_carrito_20261018.json").read_text(
        encoding="utf-8"
    )
)

# Lo que escribio la persona despues de A2 en ATT1 (muestra del 01/11).
ATT1_471 = "El creer que será algo más que no funcionará 😢"
ATT1_517 = (
    "Me interesa, pero por ahora no puedo cubrir el pago, necesito esperar uno o "
    "dos meses, me interesa el descuento"
)
ATT1_542 = "Hola buenas tardes. Cambie de opinión. Ya no quiero el curso.. gracias"
OPT_OUT = "No más mensajes"
CODE = "ATT1A1B2C3D4E5F6"
NOW = datetime(2026, 10, 27, 15, 4, tzinfo=UTC)  # #517: la respuesta, 09:04 de CDMX


def _a2_text() -> str:
    templates = CARRITO.get("templates") or CARRITO.get("message_templates")
    template = parse_approved_template(
        {"message_templates": templates},
        template_name="att1_carrito_dudas_01",
        expected_language="es_MX",
        expected_category="MARKETING",
        parameter_count=2,
    )
    return template.render({"1": "María", "2": "Alimenta tu Tiroides"})


def _a2_message(reply: dict[str, Any]) -> dict[str, Any]:
    """A2 como la guarda Chatwoot, justo antes de la respuesta."""
    [template_message, _] = json.loads(CONVERSATION_158.read_text(encoding="utf-8"))["messages"]
    message = copy.deepcopy(template_message)
    message["content"] = _a2_text()
    message["conversation_id"] = reply["conversation_id"]
    message["id"] = reply["id"] - 1
    message["created_at"] = reply["created_at"] - 3_180
    message["content_attributes"] = {"recovery_followup_hash": "f" * 64}
    return message


def _contact(message_id: int, text: str) -> dict[str, Any]:
    return {"id": message_id, "message_type": 0, "private": False, "content": text,
            "sender": {"type": "contact", "id": 7}, "content_attributes": {}}


def _ours(message_id: int, text: str, *, template: bool) -> dict[str, Any]:
    attributes = {"recovery_followup_hash": "f" * 64} if template else {"appointment_setter_reply_hash": "a" * 64}
    return {"id": message_id, "message_type": 1, "private": False, "content": text,
            "sender": {"type": "agent_bot", "id": 1}, "content_attributes": attributes}


# --------------------------------------------------------- que es «respuesta»


def test_the_texts_are_the_ones_after_our_last_template() -> None:
    history = [
        _ours(10, "A1", template=True),
        _contact(11, "antes de A2"),
        _ours(12, "la respuesta del agente", template=False),
        _ours(13, _a2_text(), template=True),
        _contact(15, ATT1_517),
        {**_contact(14, "nota"), "private": True},
        {**_contact(16, "del equipo"), "sender": {"type": "user", "id": 3}},
    ]
    assert prospect_texts_after_last_template(history) == [ATT1_517]


def test_the_webhook_form_of_the_message_types_counts_too() -> None:
    history = [
        {**_ours(13, "A2", template=True), "message_type": "template"},
        {**_contact(14, ATT1_471), "message_type": "incoming"},
    ]
    assert prospect_texts_after_last_template(history) == [ATT1_471]


def test_without_any_template_everything_the_person_wrote_counts() -> None:
    history = [_contact(1, "hola"), _ours(2, "respuesta", template=False), _contact(3, ATT1_542)]
    assert prospect_texts_after_last_template(history) == ["hola", ATT1_542]
    assert reply_may_renew(history) is False


@pytest.mark.parametrize("text", [ATT1_471, ATT1_517, "Hola", "¿Cuánto cuesta?"])
def test_a_real_answer_to_a2_may_renew(text: str) -> None:
    assert reply_may_renew([_ours(13, "A2", template=True), _contact(14, text)]) is True


@pytest.mark.parametrize(
    "texts",
    [
        [ATT1_542],
        [OPT_OUT],
        ["Gracias 🙏, será en otra ocasión Saludos"],
        # «No, gracias» corta aunque no sea el primer mensaje (mas estricto que
        # el seguimiento: la renovacion la pide la primera respuesta a A2).
        ["Hola", "No, gracias"],
    ],
)
def test_an_opt_out_or_a_clear_no_never_renews(texts: list[str]) -> None:
    history = [_ours(13, "A2", template=True)] + [
        _contact(14 + index, text) for index, text in enumerate(texts)
    ]
    assert reply_may_renew(history) is False


def test_a_no_before_our_last_template_does_not_count() -> None:
    history = [_contact(10, ATT1_542), _ours(13, "A2", template=True), _contact(14, ATT1_517)]
    assert reply_may_renew(history) is True


# ------------------------------------------------------- la entrada del agente


def _coupon(**overrides: Any) -> RecoveryCouponForConversation:
    fields: dict[str, Any] = {
        "state": "valid", "code": CODE, "discount": Decimal("0.100"),
        "expires_at": NOW + timedelta(hours=23, minutes=5), "ordinal": 2,
    }
    fields.update(overrides)
    return RecoveryCouponForConversation(**fields)


def test_a_valid_coupon_goes_with_its_code_percent_and_mexican_time() -> None:
    context = recovery_coupon_context(_coupon(), now=NOW, phone=WA_ID)
    assert context == {
        "state": "valid",
        "code": CODE,
        "discount_percent": 10,
        "expires_at": "2026-10-28T14:09:00+00:00",
        # 14:09 UTC es 8:09 de la Ciudad de Mexico (UTC-6).
        "expires_local": "mañana a las 8:09 a. m.",
        "last_renewal": True,
    }


def test_coupon_one_is_not_the_last_renewal_and_the_time_follows_the_country() -> None:
    context = recovery_coupon_context(
        _coupon(ordinal=1, discount=Decimal("0.125")), now=NOW, phone="5491100000000"
    )
    assert context is not None
    assert context["last_renewal"] is False
    assert context["discount_percent"] == "12.5"
    # Buenos Aires (UTC-3): 14:09 UTC son las 11:09.
    assert context["expires_local"] == "mañana a las 11:09 a. m."


def test_a_renewal_on_its_way_says_so_without_a_code() -> None:
    coupon = _coupon(state="renewal_in_progress", code=None, discount=None, expires_at=None)
    assert recovery_coupon_context(coupon, now=NOW, phone=WA_ID) == {
        "state": "renewal_in_progress",
        "coupon_arrives_in_our_next_message": True,
    }


@pytest.mark.parametrize(
    "coupon",
    [None, _coupon(expires_at=NOW), _coupon(expires_at=NOW - timedelta(minutes=1)), _coupon(state="other")],
)
def test_nothing_to_say_gives_no_entry(coupon: object) -> None:
    assert recovery_coupon_context(coupon, now=NOW, phone=WA_ID) is None


# ------------------------------------------------------------- el cliente


def _client(handler: Any) -> SupabaseClient:
    return SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="test-key",
        transport=httpx.MockTransport(handler),
    )


def _rpc(rows: object, status: int = 200, seen: list[httpx.Request] | None = None) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=rows)

    return _client(handler)


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


RENEWAL_KWARGS: dict[str, Any] = {
    "chatwoot_account_id": 2, "chatwoot_inbox_id": 11, "chatwoot_conversation_id": 517,
    "chatwoot_message_id": 9_001, "external_user_id": FORM_ID,
    "occurred_at": "2026-10-27T15:04:00+00:00",
}
LOOKUP_KWARGS: dict[str, Any] = {
    "chatwoot_account_id": 2, "chatwoot_inbox_id": 11, "chatwoot_conversation_id": 517,
    "external_user_id": FORM_ID,
}


@pytest.mark.parametrize("outcome", sorted(CART_RENEWAL_BY_REPLY_OUTCOMES))
def test_the_client_asks_for_the_renewal_by_reply(outcome: str) -> None:
    seen: list[httpx.Request] = []
    client = _rpc([{"outcome": outcome}], seen=seen)
    assert _run(client.request_cart_renewal_by_reply(**RENEWAL_KWARGS)) == outcome
    [request] = seen
    assert request.url.path == "/rest/v1/rpc/request_recovery_renewal_by_reply_v1"
    assert json.loads(request.content) == {
        "p_chatwoot_account_id": 2, "p_chatwoot_inbox_id": 11, "p_chatwoot_conversation_id": 517,
        "p_chatwoot_message_id": 9_001, "p_external_user_id": FORM_ID,
        "p_occurred_at": "2026-10-27T15:04:00+00:00",
    }


@pytest.mark.parametrize(
    ("rows", "status"),
    [([{"outcome": "renewed"}], 200), ([], 200), ([{"outcome": "advanced"}] * 2, 200), ([], 503)],
)
def test_the_client_refuses_an_unknown_renewal_answer(rows: object, status: int) -> None:
    with pytest.raises(SupabaseError):
        _run(_rpc(rows, status).request_cart_renewal_by_reply(**RENEWAL_KWARGS))


def test_the_client_reads_the_coupon_of_the_turn() -> None:
    seen: list[httpx.Request] = []
    client = _rpc([{"state": "valid", "code": CODE, "discount": 0.1,
                    "expires_at": "2026-10-28T14:09:00+00:00", "ordinal": 2}], seen=seen)
    coupon = _run(client.get_recovery_coupon_for_conversation(**LOOKUP_KWARGS))
    assert coupon == RecoveryCouponForConversation(
        state="valid", code=CODE, discount=Decimal("0.1"),
        expires_at=datetime(2026, 10, 28, 14, 9, tzinfo=UTC), ordinal=2,
    )
    [request] = seen
    assert request.url.path == "/rest/v1/rpc/get_recovery_coupon_for_conversation_v1"
    assert json.loads(request.content) == {
        "p_chatwoot_account_id": 2, "p_chatwoot_inbox_id": 11,
        "p_chatwoot_conversation_id": 517, "p_external_user_id": FORM_ID,
    }


def test_the_client_reads_a_renewal_on_its_way_and_nothing() -> None:
    on_its_way = _run(_rpc([{"state": "renewal_in_progress", "code": None, "discount": None,
                             "expires_at": None, "ordinal": 2}])
                      .get_recovery_coupon_for_conversation(**LOOKUP_KWARGS))
    assert on_its_way == RecoveryCouponForConversation(
        state="renewal_in_progress", code=None, discount=None, expires_at=None, ordinal=2
    )
    assert _run(_rpc([]).get_recovery_coupon_for_conversation(**LOOKUP_KWARGS)) is None


@pytest.mark.parametrize(
    "row",
    [
        {"state": "valid", "code": "tiroides10", "discount": 0.1, "expires_at": "2026-10-28T14:09:00+00:00", "ordinal": 2},
        {"state": "valid", "code": CODE, "discount": 1.5, "expires_at": "2026-10-28T14:09:00+00:00", "ordinal": 2},
        {"state": "valid", "code": CODE, "discount": True, "expires_at": "2026-10-28T14:09:00+00:00", "ordinal": 2},
        {"state": "valid", "code": CODE, "discount": 0.1, "expires_at": "2026-10-28T14:09:00", "ordinal": 2},
        {"state": "valid", "code": CODE, "discount": 0.1, "expires_at": "2026-10-28T14:09:00+00:00", "ordinal": 3},
        {"state": "renewal_in_progress", "code": CODE, "discount": None, "expires_at": None, "ordinal": 2},
        {"state": "expired", "code": CODE, "discount": 0.1, "expires_at": "2026-10-28T14:09:00+00:00", "ordinal": 2},
    ],
)
def test_the_client_refuses_a_coupon_row_it_does_not_know(row: dict[str, object]) -> None:
    with pytest.raises(SupabaseError):
        _run(_rpc([row]).get_recovery_coupon_for_conversation(**LOOKUP_KWARGS))


# ---------------------------------------------------------------- el webhook


class _CouponConversation(_TemplateConversation):
    """La conversacion de A2 con las dos RPC nuevas."""

    def __init__(
        self,
        *,
        renewal: str | Exception = "advanced",
        coupon: RecoveryCouponForConversation | Exception | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.renewal = renewal
        self.coupon = coupon
        self.by_reply: list[dict[str, object]] = []
        self.lookups: list[dict[str, object]] = []
        self.button_renewals: list[dict[str, object]] = []

    async def request_cart_renewal(self, **kwargs: object) -> str:
        self.button_renewals.append(kwargs)
        return "no_pending_last_touch"

    async def request_cart_renewal_by_reply(self, **kwargs: object) -> str:
        self.by_reply.append(kwargs)
        if isinstance(self.renewal, Exception):
            raise self.renewal
        return self.renewal

    async def get_recovery_coupon_for_conversation(
        self, **kwargs: object
    ) -> RecoveryCouponForConversation | None:
        self.lookups.append(kwargs)
        if isinstance(self.coupon, Exception):
            raise self.coupon
        return self.coupon


def _settings(tmp_path: Path, *, enabled: bool = True) -> Settings:
    return replace(
        _manifest_settings(tmp_path),
        payment_link_enabled=True,
        recovery_cart_followups_enabled=True,
        recovery_cart_last_touch_template_name="att1_ultima_renovacion_01",
        recovery_coupon_by_reply_enabled=enabled,
    )


def _answer(
    tmp_path: Path,
    supabase: _CouponConversation,
    text: str = ATT1_517,
    *,
    settings: Settings | None = None,
    with_a2: bool = True,
) -> tuple[Any, Any, Any, dict[str, Any]]:
    webhook, history = _text_webhook(text)
    full_history = [_a2_message(history[0]), *history] if with_a2 else history
    chatwoot, shadow, app = _process(
        tmp_path,
        supabase,
        settings=settings or _settings(tmp_path),
        webhook=webhook,
        history=full_history,
        proposal=PAYMENT_LINK_PROPOSAL,
    )
    return chatwoot, shadow, app, webhook


def test_the_answer_to_a2_asks_for_the_renewal_and_the_agent_still_answers(tmp_path: Path) -> None:
    supabase = _CouponConversation(
        coupon=RecoveryCouponForConversation(
            state="renewal_in_progress", code=None, discount=None, expires_at=None, ordinal=2
        ),
        identities=(FORM_ID,),
    )

    chatwoot, shadow, _, webhook = _answer(tmp_path, supabase)

    [renewal] = supabase.by_reply
    assert renewal["chatwoot_account_id"] == 1
    assert renewal["chatwoot_inbox_id"] == 9
    assert renewal["chatwoot_conversation_id"] == 200
    assert renewal["chatwoot_message_id"] == webhook["id"]
    # La identidad del formulario, la misma que usa la admision.
    assert renewal["external_user_id"] == FORM_ID
    # El momento del mensaje, de la captura (created_at de Chatwoot).
    expected = datetime.fromtimestamp(
        webhook["conversation"]["messages"][0]["created_at"], tz=UTC
    ).isoformat()
    assert renewal["occurred_at"] == expected
    # El texto no es el boton: el atajo no corre y el agente contesta.
    assert supabase.button_renewals == []
    [(_, context)] = shadow.calls
    assert context["recovery_coupon"] == {
        "state": "renewal_in_progress",
        "coupon_arrives_in_our_next_message": True,
    }
    assert context["messages"][-1]["text"] == ATT1_517  # type: ignore[index]
    assert supabase.lookups == [{
        "chatwoot_account_id": 1, "chatwoot_inbox_id": 9,
        "chatwoot_conversation_id": 200, "external_user_id": FORM_ID,
    }]
    assert len(chatwoot.reply_calls) == 1


def test_a_delivered_coupon_reaches_the_agent(tmp_path: Path) -> None:
    expires = datetime.now(UTC) + timedelta(hours=20)
    supabase = _CouponConversation(
        renewal="no_pending_last_touch",
        coupon=RecoveryCouponForConversation(
            state="valid", code=CODE, discount=Decimal("0.100"), expires_at=expires, ordinal=2
        ),
        identities=(FORM_ID,),
    )

    _, shadow, _, _ = _answer(tmp_path, supabase, ATT1_471)

    [(_, context)] = shadow.calls
    entry = context["recovery_coupon"]
    assert isinstance(entry, dict)
    assert entry["state"] == "valid" and entry["code"] == CODE
    assert entry["discount_percent"] == 10 and entry["last_renewal"] is True
    assert entry["expires_at"] == expires.astimezone(UTC).isoformat()
    assert entry["expires_local"].endswith(("a. m.", "p. m."))


def test_without_a_coupon_the_entry_is_as_before(tmp_path: Path) -> None:
    supabase = _CouponConversation(renewal="no_pending_last_touch", identities=(FORM_ID,))

    _, shadow, _, _ = _answer(tmp_path, supabase)

    [(_, context)] = shadow.calls
    assert "recovery_coupon" not in context


@pytest.mark.parametrize("text", [ATT1_542, "Gracias 🙏, será en otra ocasión Saludos"])
def test_a_clear_no_does_not_renew_but_the_agent_still_reads_the_coupon(
    tmp_path: Path, text: str
) -> None:
    supabase = _CouponConversation(identities=(FORM_ID,))

    _, shadow, _, _ = _answer(tmp_path, supabase, text)

    assert supabase.by_reply == []
    assert len(supabase.lookups) == 1
    assert len(shadow.calls) == 1


def test_an_opt_out_never_reaches_the_renewal(tmp_path: Path) -> None:
    supabase = _CouponConversation(identities=(FORM_ID,))

    _, shadow, _, _ = _answer(tmp_path, supabase, OPT_OUT)

    assert supabase.by_reply == [] and supabase.lookups == []
    assert shadow.calls == []
    assert len(supabase.opt_outs) == 1


def test_a_person_who_opted_out_before_is_answered_without_renewing(tmp_path: Path) -> None:
    # F-036: con manifiesto el agente le contesta, pero Z es marketing y no sale.
    supabase = _CouponConversation(identities=(FORM_ID,), stopped_user_ids=(FORM_ID,))

    _, shadow, _, _ = _answer(tmp_path, supabase)

    assert supabase.by_reply == []
    [(_, context)] = shadow.calls
    assert context["opted_out_contact"] is True


def test_failures_of_the_base_never_stop_the_agent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    supabase = _CouponConversation(
        renewal=SupabaseError("request_recovery_renewal_by_reply_v1_failed: HTTP 503"),
        coupon=SupabaseError("get_recovery_coupon_for_conversation_v1_failed: HTTP 503"),
        identities=(FORM_ID,),
    )

    with caplog.at_level(logging.WARNING, logger="bridge.app"):
        chatwoot, shadow, app, _ = _answer(tmp_path, supabase)

    assert len(supabase.by_reply) == 1 and len(supabase.lookups) == 1
    [(_, context)] = shadow.calls
    assert "recovery_coupon" not in context
    assert len(chatwoot.reply_calls) == 1
    assert "cart_renewal_by_reply_failed error=SupabaseError conversation=200" in caplog.text
    assert "recovery_coupon_lookup_failed error=SupabaseError conversation=200" in caplog.text


def test_the_log_never_carries_the_code(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    supabase = _CouponConversation(
        coupon=RecoveryCouponForConversation(
            state="valid", code=CODE, discount=Decimal("0.1"),
            expires_at=datetime.now(UTC) + timedelta(hours=3), ordinal=1,
        ),
        identities=(FORM_ID,),
    )

    with caplog.at_level(logging.INFO, logger="bridge.app"):
        _answer(tmp_path, supabase)

    assert "recovery_coupon_context state=valid conversation=200" in caplog.text
    assert "cart_renewal_by_reply outcome=advanced conversation=200" in caplog.text
    assert CODE not in caplog.text


def test_with_the_flag_off_nothing_is_asked(tmp_path: Path) -> None:
    supabase = _CouponConversation(identities=(FORM_ID,))

    _, shadow, _, _ = _answer(tmp_path, supabase, settings=_settings(tmp_path, enabled=False))

    assert supabase.by_reply == [] and supabase.lookups == []
    [(_, context)] = shadow.calls
    assert "recovery_coupon" not in context


def test_the_button_keeps_its_shortcut(tmp_path: Path) -> None:
    supabase = _CouponConversation(identities=(FORM_ID,))
    supabase.request_cart_renewal = _advanced  # type: ignore[method-assign]

    _, shadow, _, _ = _answer(tmp_path, supabase, "Quiero mi 10 %")

    # Z es la respuesta: el agente no corre y no hace falta la renovacion por respuesta.
    assert shadow.calls == []
    assert supabase.by_reply == [] and supabase.lookups == []


async def _advanced(**_: object) -> str:
    return "advanced"


# ------------------------------------------------------------- el arranque


def test_the_flag_is_off_by_default_and_portable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.__dataclass_fields__["recovery_coupon_by_reply_enabled"].default is False
    assert "recovery_coupon_by_reply_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
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
    monkeypatch.delenv("RECOVERY_COUPON_BY_REPLY_ENABLED", raising=False)
    assert Settings.from_env().recovery_coupon_by_reply_enabled is False
    monkeypatch.setenv("RECOVERY_COUPON_BY_REPLY_ENABLED", "yes")
    assert Settings.from_env().recovery_coupon_by_reply_enabled is False
    monkeypatch.setenv("RECOVERY_COUPON_BY_REPLY_ENABLED", " TRUE ")
    assert Settings.from_env().recovery_coupon_by_reply_enabled is True


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"recovery_cart_followups_enabled": False}, "RECOVERY_CART_FOLLOWUPS_ENABLED"),
        ({"recovery_cart_last_touch_template_name": None}, "RECOVERY_CART_LAST_TOUCH_TEMPLATE_NAME"),
    ],
)
def test_an_incomplete_wiring_does_not_start(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        create_app(replace(_settings(tmp_path), **overrides))


def test_without_a_manifest_it_does_not_start(tmp_path: Path) -> None:
    # Johanna: sin manifiesto, el flag no arranca (y apagado no cambia nada).
    settings = replace(
        _johanna_settings(tmp_path),
        recovery_cart_followups_enabled=True,
        recovery_cart_last_touch_template_name="att1_ultima_renovacion_01",
        recovery_coupon_by_reply_enabled=True,
    )
    with pytest.raises(ValueError, match="instance manifest"):
        create_app(settings)
