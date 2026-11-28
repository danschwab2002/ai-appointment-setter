"""La conversacion entera con dos productos (varios productos por aliada, paso 3).

El sandbox del bridge: una conversacion entra por el webhook firmado de Chatwoot y sale con el link
y la etiqueta, con todo lo que esta en el medio corriendo de verdad. Corre el procesador real de
Hermes (``HermesShadowProcessor``), con su pedido HTTP, y contesta la respuesta real del modelo.
Despues corren el parser, el validador de la propuesta con las claves de los dos productos, la
reserva del link por el funnel del producto que nombro el agente, la respuesta a la persona y la
etiqueta del producto por su macro.

Lo que no es real, y por que:
- Hermes no corre: el transporte HTTP del procesador guarda el pedido y devuelve la respuesta que dio
  el modelo el 2026-10-16, con dos productos. Que el Hermes de produccion no corte un ``system`` de
  mas de 20.000 bytes lo prueba ``hermes/prueba/probar_en_contenedor.sh``, con la imagen de ATT1.
- Chatwoot y la base son los stubs de las pruebas de la 2a a la 2c. La base devuelve, para cada
  funnel, el link de su oferta por defecto. Que la reserva real elija esa oferta lo prueba el caso
  12 de ``tests/sql/followup_engine/validate_att1_portable_chain.mjs``, con las RPC reales.
- Nada sale: no hay red, ni Hotmart, ni WhatsApp. Ninguna herramienta que escribe llega a produccion.

Fixtures:
- tests/fixtures/hermes_glm_two_products_20261016.json: las 18 respuestas reales de
  z-ai/glm-5.3-flash del arnes del paso 3 (2026-10-16 ≈23:29Z), con el caso que las pidio.
- tests/fixtures/att1_knowledge_v8_20261016.toml: el conocimiento v8 aprobado de ATT1. Con el
  segundo producto de prueba renderiza 20.830 bytes, el pedido real con dos productos.
- tests/fixtures/att1_person_activity_20261016.json, por ``_activities``: lo que hizo la persona,
  filas reales de att1-db. Lo del segundo producto se deriva de esas filas: ATT2 nunca mando nada.
- El webhook real de la conversacion 200 (por ``_text_webhook``).
"""

from __future__ import annotations

import asyncio
import copy
import json
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from bridge.app import create_app, recent_payment_failure_context
from bridge.commercial_knowledge import CommercialKnowledge
from bridge.hermes import HermesShadowProcessor
from bridge.instance_manifest import InstanceManifest
from bridge.supabase import CheckoutIssuanceReservation, PersonActivity, RecentPaymentFailure
from test_agent_link_product import _LinkSupabase
from test_checkout_link_for_funnel import ATT1_TOML, _two_products_settings
from test_commercial_knowledge import _second_product
from test_multi_product_manifest import SECOND
from test_payment_failure_context import _captured_failure
from test_person_activity import _activities
from test_webhook import StubChatwootClient, _post, _signed_headers
from test_whatsapp_inbound_equivalence import SECRET, _text_webhook

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURE = json.loads(
    (FIXTURES / "hermes_glm_two_products_20261016.json").read_text(encoding="utf-8")
)
KNOWLEDGE_V8 = FIXTURES / "att1_knowledge_v8_20261016.toml"

CONVERSATION = 200
MACROS = {"att1": 7, "att2": SECOND["macro_etiqueta"]}
LABELS = {macro: key for key, macro in MACROS.items()}
# El link que devuelve la reserva de cada funnel: la oferta por defecto de su producto.
LINKS = {
    "att1": "https://pay.hotmart.com/F106691755G?off=bxjge6zq&checkoutMode=10&src=hermes"
    "&sck=hermes%7Cv1%7C01K3F8QW7N2VYB4M6X9CDPTZRA",
    "att2": f"https://pay.hotmart.com/{SECOND['hotmart']['hotlink']}?off=prueba2a"
    "&checkoutMode=10&src=hermes&sck=hermes%7Cv1%7C01K3F8QW7N2VYB4M6X9CDPTZRA",
}
# Lo que hizo la persona en cada caso, con las mismas filas reales que vio el modelo el 16/10
# (el numero de persona de att1_person_activity_20261016.json, por producto).
ACTIVITY = {
    "02-enlace-del-segundo": {"att2": 17},
    "03-enlace-del-primero-nombrado": {"att2": 8, "att1": 17},
    "04-cambia-de-producto": {"att1": 17},
    "05-enlace-sin-saber-cual": {},
    "06-pago-rechazado-del-segundo": {"att2": 9},
}
# El ultimo pago rechazado de cada funnel: el PURCHASE_CANCELED real de ATT1 (su indice en
# hotmart_purchase_canceled_att1_20261012.json), puesto en el segundo producto, como el 16/10.
FAILURES = {"06-pago-rechazado-del-segundo": {"att2": 0}}


def _case(case_id: str) -> dict[str, Any]:
    [case] = [case for case in CAPTURE["cases"] if case["id"] == case_id]
    return case


def _two_products_knowledge() -> CommercialKnowledge:
    payload = tomllib.loads(KNOWLEDGE_V8.read_text(encoding="utf-8"))
    payload["productos"].append(_second_product())
    return CommercialKnowledge.from_mapping(payload)


def _settings(tmp_path: Path):
    settings = _two_products_settings(tmp_path)
    assert settings.instance_manifest is not None
    payload = tomllib.loads(ATT1_TOML.read_text(encoding="utf-8"))
    payload["productos"] = [
        {"clave": "att1", "etiqueta": "att1", "macro_etiqueta": MACROS["att1"]},
        copy.deepcopy(SECOND),
    ]
    return replace(
        settings,
        instance_manifest=replace(
            settings.instance_manifest,
            products=InstanceManifest.from_mapping(payload).products,
            flows={**settings.instance_manifest.flows, "pago_fallido": True},
        ),
        commercial_knowledge=_two_products_knowledge(),
        payment_link_enabled=True,
        # Con el pago rechazado prendido, el agente recibe el ultimo de todos los funnels (2c-3b).
        portable_hotmart_payment_failure_enabled=True,
    )


class _Base(_LinkSupabase):
    """Lo que hizo la persona en cada funnel, y el link de la oferta de cada funnel."""

    def __init__(
        self,
        activities: dict[str, tuple[PersonActivity, ...]],
        failures: dict[str, RecentPaymentFailure],
    ) -> None:
        super().__init__()
        self.activities = activities
        self.failures = failures
        self.activity_lookups: list[str] = []

    async def get_portable_person_activity(self, **kwargs: object) -> tuple[PersonActivity, ...]:
        funnel = kwargs["config"].funnel_ref  # type: ignore[attr-defined]
        self.activity_lookups.append(funnel)
        return self.activities.get(funnel, ())

    async def get_portable_recent_payment_failure(self, **kwargs: object) -> RecentPaymentFailure | None:
        return self.failures.get(kwargs["config"].funnel_ref)  # type: ignore[attr-defined]

    async def reserve_chatwoot_checkout_issuance_v2(
        self, **kwargs: object
    ) -> CheckoutIssuanceReservation:
        reservation = await super().reserve_chatwoot_checkout_issuance_v2(**kwargs)
        return replace(reservation, checkout_url_final=LINKS[str(kwargs["funnel_ref"])])


class _Hermes:
    """El API server de Hermes: guarda cada pedido y contesta lo que contesto el modelo."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": self.content}}]})


def _history(case: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """El webhook real con el ultimo mensaje del caso, y la conversacion entera en el historial."""
    turns = case["turns"]
    webhook, [template] = _text_webhook(turns[-1][1])
    history = []
    for index, (speaker, text) in enumerate(turns):
        message = copy.deepcopy(template)
        message["id"] = int(template["id"]) - len(turns) + 1 + index
        message["content"] = text
        if speaker == "agent":
            message["message_type"] = 1
            message["sender_type"] = "AgentBot"
        history.append(message)
    return webhook, history


def _conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case_id: str, rep: int
) -> tuple[_Hermes, _Base, StubChatwootClient, list[str]]:
    case = _case(case_id)
    hermes = _Hermes(case["responses"][rep])
    settings = _settings(tmp_path)
    knowledge = settings.commercial_knowledge
    assert knowledge is not None
    processor = HermesShadowProcessor(
        base_url="https://hermes.sandbox.test/v1",
        api_key="sandbox-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path / "shadow",
        transport=httpx.MockTransport(hermes),
        system_prompt=knowledge.render(),
        product_keys=("att1", "att2"),
    )
    base = _Base(
        {key: _activities(person) for key, person in ACTIVITY[case_id].items()},
        {key: _captured_failure(index) for key, index in FAILURES.get(case_id, {}).items()},
    )
    webhook, history = _history(case)
    chatwoot = StubChatwootClient(messages=history)
    labels: list[str] = []

    async def get_macro_actions(self, *, macro_id: int) -> list[dict[str, object]]:
        return [{"action_name": "add_label", "action_params": [LABELS[macro_id]]}]

    async def get_conversation_labels(self, *, conversation_id: int) -> list[str]:
        return []

    async def execute_macro(self, *, macro_id: int, conversation_id: int) -> None:
        assert conversation_id == CONVERSATION
        labels.append(LABELS[macro_id])

    for name, method in (
        ("get_macro_actions", get_macro_actions),
        ("get_conversation_labels", get_conversation_labels),
        ("execute_macro", execute_macro),
    ):
        monkeypatch.setattr(StubChatwootClient, name, method, raising=False)

    app = create_app(
        settings,
        chatwoot_client=chatwoot,
        shadow_processor=processor,
        supabase_client=base,  # type: ignore[arg-type]
    )
    raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
    response = _post(app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery=f"{case_id}-{rep}"))
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return hermes, base, chatwoot, labels


def _proposal(case_id: str, rep: int) -> dict[str, Any]:
    return json.loads(_case(case_id)["responses"][rep])


def _what_hermes_got(hermes: _Hermes, case_id: str) -> dict[str, Any]:
    """El pedido a Hermes: el conocimiento entero de los dos productos y el contexto del caso."""
    [request] = hermes.requests
    system, user = request["messages"]
    rendered = _two_products_knowledge().render()
    assert system == {"role": "system", "content": rendered}
    # Mas de 20.000 bytes: el tope viejo, que se creia el corte de Hermes.
    assert len(system["content"].encode("utf-8")) == 20830
    context = json.loads(user["content"])
    case = _case(case_id)
    # Los productos y lo que hizo la persona, como los vio el modelo el 16/10 (sin las horas).
    assert context["products"] == case["contexto"]["products"]
    seen = sorted((item["activity"], item["product"]) for item in context.get("person_activity", []))
    expected = sorted((item["activity"], item["product"]) for item in case["contexto"].get("person_activity", []))
    assert seen == expected
    assert context.get("recent_payment_failure") == case["contexto"].get("recent_payment_failure")
    return context


def _sent(chatwoot: StubChatwootClient) -> str:
    [reply] = chatwoot.reply_calls
    return str(reply["content"])


@pytest.mark.parametrize("rep", [0, 1, 2])
def test_a_cart_of_the_second_product_gets_the_link_and_the_label_of_the_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rep: int
) -> None:
    hermes, base, chatwoot, labels = _conversation(tmp_path, monkeypatch, "02-enlace-del-segundo", rep)

    _what_hermes_got(hermes, "02-enlace-del-segundo")
    assert sorted(base.activity_lookups) == ["att1", "att2"]
    [reservation] = base.candidate_calls
    assert reservation["funnel_ref"] == "att2"
    sent = _sent(chatwoot)
    assert _proposal("02-enlace-del-segundo", rep)["reply"] in sent
    assert LINKS["att2"] in sent and LINKS["att1"] not in sent
    # Al escribir, la del producto que toco; al salir el link, la del producto del link.
    assert labels == ["att2", "att2"]


@pytest.mark.parametrize("rep", [0, 1, 2])
def test_naming_the_first_product_gets_its_link_although_the_person_touched_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rep: int
) -> None:
    hermes, base, chatwoot, labels = _conversation(
        tmp_path, monkeypatch, "03-enlace-del-primero-nombrado", rep
    )

    _what_hermes_got(hermes, "03-enlace-del-primero-nombrado")
    [reservation] = base.candidate_calls
    assert reservation["funnel_ref"] == "att1"
    sent = _sent(chatwoot)
    assert LINKS["att1"] in sent and LINKS["att2"] not in sent
    # Al escribir, las dos (toco los dos productos); al salir el link, solo la del primero.
    assert sorted(labels[:2]) == ["att1", "att2"]
    assert labels[2:] == ["att1"]


@pytest.mark.parametrize("rep", [0, 1, 2])
def test_a_conversation_that_changes_product_gets_the_link_of_the_last_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rep: int
) -> None:
    hermes, base, chatwoot, labels = _conversation(tmp_path, monkeypatch, "04-cambia-de-producto", rep)

    _what_hermes_got(hermes, "04-cambia-de-producto")
    [reservation] = base.candidate_calls
    assert reservation["funnel_ref"] == "att2"
    sent = _sent(chatwoot)
    assert LINKS["att2"] in sent and LINKS["att1"] not in sent
    # Al escribir, la del primero (dejo un carrito); al salir el link, la del segundo.
    assert labels == ["att1", "att2"]


@pytest.mark.parametrize("rep", [0, 1, 2])
def test_without_knowing_the_product_the_agent_asks_and_nothing_goes_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rep: int
) -> None:
    hermes, base, chatwoot, labels = _conversation(tmp_path, monkeypatch, "05-enlace-sin-saber-cual", rep)

    _what_hermes_got(hermes, "05-enlace-sin-saber-cual")
    proposal = _proposal("05-enlace-sin-saber-cual", rep)
    assert proposal["decision"] == "ask_question"
    assert base.candidate_calls == []
    sent = _sent(chatwoot)
    assert proposal["reply"] in sent
    assert "pay.hotmart.com" not in sent
    assert labels == []


@pytest.mark.parametrize("rep", [0, 1, 2])
def test_a_rejected_payment_of_the_second_product_gets_the_link_of_the_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rep: int
) -> None:
    hermes, base, chatwoot, labels = _conversation(
        tmp_path, monkeypatch, "06-pago-rechazado-del-segundo", rep
    )

    context = _what_hermes_got(hermes, "06-pago-rechazado-del-segundo")
    assert context["recent_payment_failure"] == {
        **recent_payment_failure_context(_captured_failure(0)),
        "product": "att2",
    }
    [reservation] = base.candidate_calls
    assert reservation["funnel_ref"] == "att2"
    sent = _sent(chatwoot)
    assert LINKS["att2"] in sent and LINKS["att1"] not in sent
    assert labels == ["att2", "att2"]


def test_the_capture_is_the_one_of_the_two_products_harness() -> None:
    """Las 18 respuestas son del modelo de produccion, y cada link nombra el producto del caso."""
    assert len(CAPTURE["cases"]) == 6
    for case in CAPTURE["cases"]:
        assert len(case["responses"]) == 3
        for content in case["responses"]:
            proposal = json.loads(content)
            expected = case["espera"].get("product")
            if expected is not None:
                assert proposal["decision"] == "send_payment_link"
                assert proposal["product"] == expected
            else:
                assert proposal["decision"] in {"ask_question", "reply"}
                assert "product" not in proposal


# ------------------------------------------------- lo que prueba el Hermes de ATT1


def test_the_knowledge_that_hermes_gets_in_the_container_test_is_the_two_products_render() -> None:
    """hermes/prueba/probar_en_contenedor.sh manda este archivo como `system`: tiene que ser hoy el
    render de los dos productos, y pasar los 20.000 bytes."""
    fixture = (FIXTURES / "hermes" / "conocimiento-dos-productos.md").read_text(encoding="utf-8")

    assert fixture == _two_products_knowledge().render()
    assert len(fixture.encode("utf-8")) > 20000


def _fake_model():
    import importlib.util

    path = Path(__file__).parents[1] / "hermes" / "prueba" / "modelo_de_mentira.py"
    spec = importlib.util.spec_from_file_location("modelo_de_mentira_dos_productos", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fake_model_says_if_the_knowledge_arrived_whole() -> None:
    mentira = _fake_model()
    knowledge = _two_products_knowledge().render()
    soul = "SOUL de prueba"

    def body(system: str) -> dict[str, Any]:
        return {"model": "m", "messages": [{"role": "system", "content": system}, {"role": "user", "content": "hola"}]}

    whole = mentira.resumir_pedido(body(soul + "\n" + knowledge), soul, knowledge)
    assert whole["conocimiento"] == "entero"
    assert whole["sistema"] == len((soul + "\n" + knowledge).encode("utf-8"))
    assert mentira.resumir_pedido(body(soul + knowledge[:20000]), soul, knowledge)["conocimiento"] == "cortado"
    assert mentira.resumir_pedido(body(soul), soul, knowledge)["conocimiento"] == "ausente"
    assert mentira.respuesta(whole).endswith(f"conocimiento=entero sistema={whole['sistema']}")
    # Sin --conocimiento, la respuesta es la de siempre (la lee el paso 5 de la prueba del contenedor).
    assert "conocimiento" not in mentira.resumir_pedido(body(soul), soul)
