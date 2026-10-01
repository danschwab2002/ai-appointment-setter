"""El dispatcher manda la plantilla aprobada sin pedirle borrador a Hermes (A4).

Contrato: docs/contracts/approved-template-direct-dispatch-v1.md.

Datos:

* ``tests/fixtures/instances/att1/instancia.toml``: el binding, las tres ofertas y
  el inbox 11 de ATT1.
* ``chatwoot_followup_candidates_inbox_9_20260928.json``: el catalogo de
  plantillas del inbox 9 capturado el 28/09 (``johanna_carrito_abandonado_01`` y
  ``johanna_compra_fallida_01``, cada una con ``{{1}}``, ``{{2}}`` y tres botones
  QUICK_REPLY). Los tests escritos antes de tener el catalogo de ATT1 mandan las
  plantillas de Johanna con su idioma (``es_EC``). El catalogo se sirve bajo el
  id del inbox 11, el unico campo del sobre que el cliente chequea, igual que
  ``test_followup_discount.py`` envuelve la misma lista.
* ``chatwoot_inbox_11_message_templates_20261001.json``: el catalogo del inbox 11
  de ATT1 del 01/10 (psql sobre ``channel_whatsapp.message_templates``), con las
  tres plantillas ``att1_*`` de primer contacto en ``es_MX``. Lo usan la seccion
  del final (los tres disparadores con las plantillas de ``[plantillas]`` del
  manifiesto) y la del primer contacto tras el formulario. De la respuesta de
  ``GET /inboxes/11`` no hay captura: el sobre es el mismo de arriba.
* ``lead_names_inbox9_template_params_20260928.json``: nombres capturados (con
  los apellidos cambiados) para el saludo.
* Las respuestas de Chatwoot al envio (``contacts/search``, ``contacts``,
  ``conversations``, ``messages``) siguen el precedente inline de
  ``test_messaging.py``: no hay captura de esas respuestas (deuda de A0).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from bridge.app import Settings, create_app
from bridge.chatwoot import ChatwootClient
from bridge.instance_manifest import InstanceManifest, Template
from bridge.lead_first_name import FirstNameInference, lead_name_key
from bridge.messaging import (
    ChatwootMessageSender,
    FinalMetaEffectGate,
    WhatsAppTemplateConfig,
)
from bridge.recovery_agent import FollowupMessageProposal
from bridge.supabase import (
    ChatwootAuthorityContext,
    DeliveryAttempt,
    FollowupExecutionContext,
    PilotBoundaryConfig,
    PilotRequestStartRejectedError,
    ReevaluationDecision,
    ScheduledAction,
    SupabaseClient,
    SupabaseError,
)
from bridge.worker import DurableDispatcher

FIXTURES = Path(__file__).parent / "fixtures"
MANIFEST = InstanceManifest.from_toml_file(FIXTURES / "instances" / "att1" / "instancia.toml")
ALLY = MANIFEST.to_commercial_ally_config()
CATALOG: list[dict[str, Any]] = json.loads(
    (FIXTURES / "chatwoot_followup_candidates_inbox_9_20260928.json").read_text(
        encoding="utf-8"
    )
)["inbox_9_message_templates"]
LEAD_NAMES: list[dict[str, Any]] = json.loads(
    (FIXTURES / "lead_names_inbox9_template_params_20260928.json").read_text(
        encoding="utf-8"
    )
)["cases"]

CART_TEMPLATE = "johanna_carrito_abandonado_01"
PAYMENT_FAILURE_TEMPLATE = "johanna_compra_fallida_01"
ACCOUNT_ID = ALLY.chatwoot_account_id
INBOX_ID = ALLY.chatwoot_inbox_id
PHONE = "12025550124"
NOW = "2026-09-30T15:00:00+00:00"
FINAL_NOW = "2026-09-30T15:01:00+00:00"

TEMPLATE = WhatsAppTemplateConfig(
    first_touch_name=CART_TEMPLATE,
    payment_failure_name=PAYMENT_FAILURE_TEMPLATE,
    followup_name=None,
    language="es_EC",
    category="MARKETING",
    first_touch_parameter="buyer_name_and_product",
    first_touch_body_parameters=("nombre", "producto"),
    payment_failure_body_parameters=("nombre", "producto"),
)
BOUNDARY = PilotBoundaryConfig(
    scope_key="att1-recuperacion",
    scope_version=1,
    tenant_key=ALLY.tenant_ref,
    channel_provider="waba",
    channel_account_ref=f"chatwoot-inbox:{INBOX_ID}",
)

ATT1_CATALOG: list[dict[str, Any]] = json.loads(
    (FIXTURES / "chatwoot_inbox_11_message_templates_20261001.json").read_text(
        encoding="utf-8"
    )
)["templates"]


def _captured_body(name: str) -> str:
    [template] = [t for t in (*CATALOG, *ATT1_CATALOG) if t["name"] == name]
    [body] = [c["text"] for c in template["components"] if c["type"] == "BODY"]
    return body


def _expected(name: str, *, first: str, product: str) -> str:
    return _captured_body(name).replace("{{1}}", first).replace("{{2}}", product)


# ------------------------------------------------------------------ Supabase


class _Authority:
    """La base: una accion vencida que la reevaluacion deja ejecutar."""

    def __init__(
        self,
        *,
        offer_code: str | None = "gopi6lh7",
        buyer_name: str | None = "Edith García Pérez",
        product_name: str = "Alimenta tu Tiroides",
        anchor_type: str = "cart_abandonment",
        action_type: str = "first_contact_review",
        inference: FirstNameInference | None = None,
    ) -> None:
        step_key = (
            "payment_failure_first_contact"
            if anchor_type == "payment_failure"
            else "first_contact"
        )
        self.action = ScheduledAction(
            action_id="action-att1",
            recovery_case_id="case-att1",
            followup_sequence_id="sequence-att1",
            action_type=action_type,
            status="pending",
            due_at=NOW,
            expires_at="2026-10-01T15:00:00+00:00",
            expected_case_version=1,
            policy_key="att1-recuperacion-un-toque",
            policy_version=1,
            step_key=step_key,
            anchor_type=anchor_type,
            anchor_subject_internal_id="event-att1",
            anchor_observed_at="2026-09-30T14:00:00+00:00",
            lease_owner="att1-dispatcher",
            lease_generation=1,
            lease_expires_at="2026-09-30T15:05:00+00:00",
            idempotency_key=f"{anchor_type}:first_contact:case-att1",
        )
        self.attempt = DeliveryAttempt(
            attempt_id="attempt-att1",
            action_id=self.action.action_id,
            idempotency_key=self.action.idempotency_key,
            attempt_number=1,
            channel="whatsapp",
            mode="approved_template",
            phase="reserved",
            lease_generation=1,
            expected_case_version=1,
            expected_sequence_revision=1,
        )
        self.context = FollowupExecutionContext(
            action_id=self.action.action_id,
            action_type=action_type,
            step_key=step_key,
            recovery_case_id=self.action.recovery_case_id,
            contact_id="contact-att1",
            source_event_id="event-att1",
            buyer_name=buyer_name,
            buyer_email="buyer@example.test",
            buyer_phone=PHONE,
            product_name=product_name,
            offer_code=offer_code,
            current_goal=None,
            lead_stage="new",
        )
        self.inference = inference
        self.events: list[str] = []
        self.finalizations: list[dict[str, object]] = []
        self.acceptances: list[dict[str, object]] = []

    async def claim_due_followup_actions(self, **_: object) -> list[ScheduledAction]:
        return [self.action]

    async def get_followup_chatwoot_context(self, **_: object) -> ChatwootAuthorityContext:
        return ChatwootAuthorityContext(
            action_id=self.action.action_id,
            action_type=self.action.action_type,
            chatwoot_account_id=None,
            external_conversation_id=None,
            expected_inbox_id=None,
            anchor_external_message_id=None,
        )

    async def reevaluate_followup_action(self, **_: object) -> ReevaluationDecision:
        self.events.append("reevaluate")
        return ReevaluationDecision(
            action_id=self.action.action_id,
            decision="execute",
            reason_code="eligible_for_execution",
            case_version=1,
            sequence_revision=1,
        )

    async def reserve_followup_delivery_attempt(self, **kwargs: object) -> DeliveryAttempt:
        assert kwargs["mode"] == "approved_template"
        self.events.append("reserve")
        return self.attempt

    async def get_followup_execution_context(self, **_: object) -> FollowupExecutionContext:
        return self.context

    async def get_lead_first_name_inference(self, name_key: str) -> FirstNameInference | None:
        if self.inference is not None and name_key == lead_name_key(self.context.buyer_name):
            return self.inference
        return None

    async def mark_followup_request_started(self, **_: object) -> DeliveryAttempt:
        self.events.append("request_started")
        return replace(self.attempt, phase="request_started")

    async def finalize_followup_delivery_attempt(self, **kwargs: object) -> object:
        self.finalizations.append(kwargs)
        # Como _finalize_followup_delivery_attempt: sin proximo intento, un
        # failed_before_request cierra la accion como permanent_failed.
        status = {
            "failed_before_request": (
                "retryable_failed"
                if kwargs["next_attempt_at"] is not None
                else "permanent_failed"
            ),
            "rejected": "permanent_failed",
        }.get(str(kwargs["outcome"]), "delivery_unknown")
        return type("Finalized", (), {"status": status})()

    async def record_and_finalize_followup_acceptance(self, **kwargs: object) -> object:
        self.events.append("accepted")
        self.acceptances.append(kwargs)
        return type("Finalized", (), {"status": "accepted_by_chatwoot"})()


# ------------------------------------------------------------------ Chatwoot


class _Chatwoot:
    """Chatwoot del inbox 11: el catalogo capturado y el envio de la plantilla."""

    def __init__(self, catalog: list[dict[str, Any]] | None = None, *, inbox_status: int = 200) -> None:
        self.catalog = copy.deepcopy(CATALOG if catalog is None else catalog)
        self.inbox_status = inbox_status
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self._contact_created = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.requests.append((request.method, path, body))
        prefix = f"/api/v1/accounts/{ACCOUNT_ID}"
        if request.method == "GET" and path == f"{prefix}/inboxes/{INBOX_ID}":
            if self.inbox_status != 200:
                return httpx.Response(self.inbox_status, json={"error": "unavailable"})
            return httpx.Response(200, json={"id": INBOX_ID, "message_templates": self.catalog})
        if request.method == "GET" and path == f"{prefix}/contacts/search":
            if not self._contact_created:
                return httpx.Response(200, json={"payload": []})
            return httpx.Response(200, json={"payload": [{
                "id": 55,
                "phone_number": f"+{PHONE}",
                "blocked": False,
                # En un inbox de WhatsApp Cloud el source_id es el wa_id: los
                # digitos del telefono del contacto (medido el 2026-10-01 sobre
                # los 170 contactos de los inboxes 9 y 11: 170 de 170).
                "contact_inboxes": [{"source_id": PHONE, "inbox": {"id": INBOX_ID}}],
            }]})
        if request.method == "POST" and path == f"{prefix}/contacts":
            self._contact_created = True
            return httpx.Response(200, json={"payload": {"id": 55}})
        if request.method == "POST" and path == f"{prefix}/conversations":
            return httpx.Response(200, json={"id": 200})
        if request.method == "POST" and path == f"{prefix}/conversations/200/messages":
            assert body is not None
            return httpx.Response(200, json={
                "id": 888,
                "conversation_id": 200,
                "message_type": 1,
                "private": False,
                "content": body["content"],
                "content_attributes": body["content_attributes"],
            })
        return httpx.Response(404, json={"error": "not_emulated"})

    def client(self) -> ChatwootClient:
        return ChatwootClient(
            base_url="https://chatwoot.test",
            account_id=ACCOUNT_ID,
            access_token="control-token",
            agent_bot_access_token="bot-token",
            agent_bot_id=99,
            transport=httpx.MockTransport(self.handler),
        )

    def posts(self, suffix: str) -> list[dict[str, Any]]:
        return [
            body
            for method, path, body in self.requests
            if method == "POST" and path.endswith(suffix) and body is not None
        ]

    def inbox_reads(self) -> int:
        return sum(
            1
            for method, path, _ in self.requests
            if method == "GET" and path.endswith(f"/inboxes/{INBOX_ID}")
        )


def _dispatcher(
    authority: _Authority,
    chatwoot: _Chatwoot,
    tmp_path: Path,
    *,
    gate_open: bool = True,
    greeting: bool = False,
    template: WhatsAppTemplateConfig = TEMPLATE,
) -> DurableDispatcher:
    client = chatwoot.client()
    return DurableDispatcher(
        supabase=authority,  # type: ignore[arg-type]
        worker_id="att1-dispatcher",
        chatwoot=client,
        chatwoot_account_id=ACCOUNT_ID,
        chatwoot_inbox_id=INBOX_ID,
        sender=ChatwootMessageSender(
            chatwoot=client,
            inbox_id=INBOX_ID,
            allowed_jid=None,
            dynamic_recipient_enabled=True,
            template=template,
        ),
        allowed_jid=None,
        commercial_ally_config=ALLY,
        portable_recipient_enabled=True,
        pilot_boundary=BOUNDARY,
        clock=lambda: FINAL_NOW,
        final_meta_effect_gate=FinalMetaEffectGate(
            enabled=gate_open, evidence_dir=tmp_path / "meta-effects"
        ),
        waba_template=template,
        approved_template_direct=True,
        lead_first_name_greeting_enabled=greeting,
    )


def _run(dispatcher: DurableDispatcher) -> list[ReevaluationDecision]:
    return asyncio.run(dispatcher.dispatch_due(now=NOW))


# ---------------------------------------------------------------- the send


@pytest.mark.parametrize("offer_code", ["gopi6lh7", "bmaztyhg", "2uafw5bg"])
def test_cart_of_each_att1_offer_sends_the_approved_body_without_hermes(
    tmp_path: Path, offer_code: str
) -> None:
    authority = _Authority(offer_code=offer_code)
    chatwoot = _Chatwoot()

    decisions = _run(_dispatcher(authority, chatwoot, tmp_path))

    expected = _expected(
        CART_TEMPLATE, first="Edith García Pérez", product="Alimenta tu Tiroides"
    )
    assert decisions[-1].decision == "execute"
    assert authority.events == [
        "reevaluate", "reserve", "reevaluate", "request_started", "accepted",
    ]
    assert chatwoot.inbox_reads() == 1
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["content"] == expected
    assert message["template_params"] == {
        "name": CART_TEMPLATE,
        "category": "MARKETING",
        "language": "es_EC",
        "processed_params": {
            "body": {"1": "Edith García Pérez", "2": "Alimenta tu Tiroides"}
        },
    }
    assert authority.acceptances[0]["message_content"] == expected
    assert authority.finalizations == []


def test_payment_failure_sends_its_own_approved_template(tmp_path: Path) -> None:
    authority = _Authority(offer_code="2uafw5bg", anchor_type="payment_failure")
    chatwoot = _Chatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["name"] == PAYMENT_FAILURE_TEMPLATE
    assert message["content"] == _expected(
        PAYMENT_FAILURE_TEMPLATE,
        first="Edith García Pérez",
        product="Alimenta tu Tiroides",
    )
    assert authority.events[-1] == "accepted"


def test_a_name_with_whitespace_meta_refuses_goes_out_collapsed(tmp_path: Path) -> None:
    # Meta rechaza un parametro con salto de linea, tabulacion o mas de cuatro
    # espacios seguidos, despues de empezado el pedido. El valor sale
    # colapsado, igual en el texto hasheado, en processed_params y en la
    # aceptacion; el contacto de Chatwoot conserva el nombre tal como llego.
    raw_name = "Edith\nGarcía\t     Pérez"
    authority = _Authority(buyer_name=raw_name)
    chatwoot = _Chatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    expected = _expected(
        CART_TEMPLATE, first="Edith García Pérez", product="Alimenta tu Tiroides"
    )
    [contact] = chatwoot.posts("/contacts")
    [message] = chatwoot.posts("/conversations/200/messages")
    assert contact["name"] == raw_name
    assert message["template_params"]["processed_params"]["body"] == {
        "1": "Edith García Pérez",
        "2": "Alimenta tu Tiroides",
    }
    assert message["content"] == expected
    assert authority.acceptances[0]["message_content"] == expected


# Una plantilla de una sola variable (parametros = ["nombre"]): el ejemplo de la
# documentacion y el caso probable de ATT1. La unica captura con un solo
# marcador es johanna_reactivacion_01 (catalogo del inbox 9 del 23/09), servida
# bajo el inbox 11 como el resto del archivo.
ONE_VARIABLE_CATALOG: list[dict[str, Any]] = json.loads(
    (FIXTURES / "chatwoot_inbox_9_message_templates_20260923.json").read_text(
        encoding="utf-8"
    )
)["message_templates"]
ONE_VARIABLE_TEMPLATE = "johanna_reactivacion_01"
ONE_VARIABLE = replace(
    TEMPLATE,
    first_touch_name=ONE_VARIABLE_TEMPLATE,
    first_touch_body_parameters=("nombre",),
)


def _one_variable_body() -> str:
    [template] = [t for t in ONE_VARIABLE_CATALOG if t["name"] == ONE_VARIABLE_TEMPLATE]
    [body] = [c["text"] for c in template["components"] if c["type"] == "BODY"]
    return body


def test_a_one_variable_template_sends_only_its_variable_end_to_end(
    tmp_path: Path,
) -> None:
    [case] = [c for c in LEAD_NAMES if c["pattern"] == "all lowercase"]
    authority = _Authority(buyer_name=case["full_name"])
    chatwoot = _Chatwoot(ONE_VARIABLE_CATALOG)

    _run(_dispatcher(authority, chatwoot, tmp_path, greeting=True, template=ONE_VARIABLE))

    body = _one_variable_body()
    assert body.count("{{1}}") == 1 and "{{2}}" not in body
    expected = body.replace("{{1}}", case["deterministic"])
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"] == {
        "name": ONE_VARIABLE_TEMPLATE,
        "category": "MARKETING",
        "language": "es_EC",
        "processed_params": {"body": {"1": case["deterministic"]}},
    }
    assert message["content"] == expected
    assert authority.acceptances[0]["message_content"] == expected
    assert authority.events[-1] == "accepted"


def test_the_closed_gate_hashes_the_one_variable_text(tmp_path: Path) -> None:
    [case] = [c for c in LEAD_NAMES if c["pattern"] == "all lowercase"]
    authority = _Authority(buyer_name=case["full_name"])
    chatwoot = _Chatwoot(ONE_VARIABLE_CATALOG)

    _run(
        _dispatcher(
            authority, chatwoot, tmp_path,
            gate_open=False, greeting=True, template=ONE_VARIABLE,
        )
    )

    rendered = _one_variable_body().replace("{{1}}", case["deterministic"])
    assert chatwoot.posts("/messages") == []
    assert authority.finalizations[-1]["reason_code"] == "final_meta_gate_closed"
    [evidence_file] = (tmp_path / "meta-effects").glob("*.json")
    evidence = json.loads(evidence_file.read_text())
    assert evidence["content_sha256"] == hashlib.sha256(rendered.encode()).hexdigest()
    assert evidence["template_name"] == ONE_VARIABLE_TEMPLATE


def _payment_failure_as_utility() -> list[dict[str, Any]]:
    # El catalogo capturado con la plantilla del pago fallido aprobada como
    # UTILITY y la del carrito como MARKETING: la categoria de ATT1 es un dato
    # de A0 que falta, y nada obliga a que las dos coincidan.
    catalog = copy.deepcopy(CATALOG)
    for template in catalog:
        if template["name"] == PAYMENT_FAILURE_TEMPLATE:
            template["category"] = "UTILITY"
    return catalog


def test_each_template_is_checked_against_its_own_category(tmp_path: Path) -> None:
    template = replace(TEMPLATE, payment_failure_category="UTILITY")
    catalog = _payment_failure_as_utility()

    failure = _Authority(offer_code="2uafw5bg", anchor_type="payment_failure")
    failure_chatwoot = _Chatwoot(catalog)
    _run(_dispatcher(failure, failure_chatwoot, tmp_path, template=template))
    cart = _Authority(offer_code="gopi6lh7")
    cart_chatwoot = _Chatwoot(catalog)
    _run(_dispatcher(cart, cart_chatwoot, tmp_path, template=template))

    [failure_message] = failure_chatwoot.posts("/conversations/200/messages")
    [cart_message] = cart_chatwoot.posts("/conversations/200/messages")
    assert failure_message["template_params"]["name"] == PAYMENT_FAILURE_TEMPLATE
    assert failure_message["template_params"]["category"] == "UTILITY"
    assert cart_message["template_params"]["name"] == CART_TEMPLATE
    assert cart_message["template_params"]["category"] == "MARKETING"
    assert failure.events[-1] == "accepted"
    assert cart.events[-1] == "accepted"


def test_a_single_category_blocks_a_payment_failure_template_of_another_one(
    tmp_path: Path,
) -> None:
    # Sin WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY las dos plantillas se comparan
    # con la unica categoria: el pago fallido UTILITY no sale.
    authority = _Authority(offer_code="2uafw5bg", anchor_type="payment_failure")
    chatwoot = _Chatwoot(_payment_failure_as_utility())

    _run(_dispatcher(authority, chatwoot, tmp_path))

    assert authority.finalizations[-1]["reason_code"] == "approved_template_mismatch"
    assert chatwoot.posts("/messages") == []


def test_an_offer_outside_the_binding_is_not_sent(tmp_path: Path) -> None:
    # 83utgyow es la oferta de la recuperacion de GHL, fuera del binding a proposito.
    authority = _Authority(offer_code="83utgyow")
    chatwoot = _Chatwoot()

    with pytest.raises(SupabaseError, match="followup_recipient_not_allowlisted"):
        _run(_dispatcher(authority, chatwoot, tmp_path))

    assert chatwoot.posts("/messages") == []
    assert authority.finalizations[-1]["reason_code"] == "pre_request_failed"


# -------------------------------------------------------------- the greeting


_GREETING_CASES = [
    pytest.param(case["full_name"], None, case["deterministic"], id=case["pattern"])
    for case in LEAD_NAMES
    if case["deterministic"] is not None
] + [
    pytest.param(
        case["full_name"],
        FirstNameInference("confident", case["model_first_name"]),
        case["model_first_name"],
        id=f"inferred {case['pattern']}",
    )
    for case in LEAD_NAMES
    if case.get("model_first_name")
] + [
    pytest.param(case["full_name"], None, case["full_name"].strip(), id=case["pattern"])
    for case in LEAD_NAMES
    if case["deterministic"] is None
]


@pytest.mark.parametrize(("full_name", "inference", "greeting"), _GREETING_CASES)
def test_the_greeting_fills_the_variable_and_the_rendered_text(
    tmp_path: Path,
    full_name: str,
    inference: FirstNameInference | None,
    greeting: str,
) -> None:
    authority = _Authority(buyer_name=full_name, inference=inference)
    chatwoot = _Chatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path, greeting=True))

    expected = _expected(CART_TEMPLATE, first=greeting, product="Alimenta tu Tiroides")
    [contact] = chatwoot.posts("/contacts")
    [message] = chatwoot.posts("/conversations/200/messages")
    # El contacto de Chatwoot conserva el nombre completo; la variable y el
    # texto guardado llevan el saludo.
    assert contact["name"] == full_name
    assert message["template_params"]["processed_params"]["body"] == {
        "1": greeting,
        "2": "Alimenta tu Tiroides",
    }
    assert message["content"] == expected
    assert authority.acceptances[0]["message_content"] == expected


def test_the_closed_gate_hashes_the_text_rendered_with_the_greeting(tmp_path: Path) -> None:
    [case] = [c for c in LEAD_NAMES if c["pattern"] == "compound given name"]
    authority = _Authority(
        buyer_name=case["full_name"],
        anchor_type="payment_failure",
        inference=FirstNameInference("confident", case["model_first_name"]),
    )
    chatwoot = _Chatwoot()

    decisions = _run(
        _dispatcher(authority, chatwoot, tmp_path, gate_open=False, greeting=True)
    )

    assert decisions[-1].decision == "execute"
    assert "request_started" not in authority.events
    assert chatwoot.posts("/messages") == []
    assert authority.finalizations[-1]["outcome"] == "failed_before_request"
    assert authority.finalizations[-1]["reason_code"] == "final_meta_gate_closed"
    [evidence_file] = (tmp_path / "meta-effects").glob("*.json")
    evidence = json.loads(evidence_file.read_text())
    rendered = _expected(
        PAYMENT_FAILURE_TEMPLATE,
        first=case["model_first_name"],
        product="Alimenta tu Tiroides",
    )
    assert evidence["content_sha256"] == hashlib.sha256(rendered.encode()).hexdigest()
    assert evidence["template_name"] == PAYMENT_FAILURE_TEMPLATE
    assert evidence["template_language"] == "es_EC"
    assert case["full_name"] not in evidence_file.read_text()


# ------------------------------------------------------ what does not go out


def _mutated(name: str, mutate: Callable[[dict[str, Any]], None]) -> list[dict[str, Any]]:
    catalog = copy.deepcopy(CATALOG)
    for template in catalog:
        if template["name"] == name:
            mutate(template)
    return catalog


def _body_of(template: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in template["components"] if c["type"] == "BODY")


def _buttons_of(template: dict[str, Any]) -> list[dict[str, Any]]:
    return next(c for c in template["components"] if c["type"] == "BUTTONS")["buttons"]


@pytest.mark.parametrize(
    ("make_chatwoot", "reason"),
    [
        pytest.param(
            lambda: _Chatwoot([t for t in CATALOG if t["name"] != CART_TEMPLATE]),
            "approved_template_unavailable",
            id="template not in the catalog",
        ),
        pytest.param(
            lambda: _Chatwoot(_mutated(CART_TEMPLATE, lambda t: t.update(status="PAUSED"))),
            "approved_template_unavailable",
            id="paused in Meta",
        ),
        pytest.param(
            lambda: _Chatwoot(inbox_status=503),
            "approved_template_unavailable",
            id="catalog unreadable",
        ),
        pytest.param(
            lambda: _Chatwoot(_mutated(CART_TEMPLATE, lambda t: t.update(language="es_MX"))),
            "approved_template_mismatch",
            id="other language",
        ),
        pytest.param(
            lambda: _Chatwoot(_mutated(CART_TEMPLATE, lambda t: t.update(category="UTILITY"))),
            "approved_template_mismatch",
            id="other category",
        ),
        pytest.param(
            lambda: _Chatwoot(_mutated(
                CART_TEMPLATE,
                lambda t: _body_of(t).update(text=_body_of(t)["text"] + " {{3}}"),
            )),
            "approved_template_mismatch",
            id="a third variable",
        ),
        pytest.param(
            lambda: _Chatwoot(_mutated(
                CART_TEMPLATE,
                lambda t: _buttons_of(t)[0].update(type="URL", url="https://x.test/{{1}}"),
            )),
            "approved_template_mismatch",
            id="a url button",
        ),
    ],
)
def test_a_catalog_that_does_not_close_leaves_the_reason_and_posts_nothing(
    tmp_path: Path, make_chatwoot: Callable[[], _Chatwoot], reason: str
) -> None:
    authority = _Authority()
    chatwoot = make_chatwoot()

    decisions = _run(_dispatcher(authority, chatwoot, tmp_path))

    assert decisions[-1].decision == "execute"
    assert authority.events == ["reevaluate", "reserve"]
    assert authority.finalizations[-1]["outcome"] == "failed_before_request"
    assert authority.finalizations[-1]["reason_code"] == reason
    # Un catalogo lo puede arreglar Chatwoot o Meta: se reintenta al minuto,
    # mientras la accion tenga reintentos (max_execution_retries) y no venza.
    assert authority.finalizations[-1]["next_attempt_at"] is not None
    assert [method for method, _, _ in chatwoot.requests] == ["GET"]


@pytest.mark.parametrize(
    "overrides",
    [{"buyer_name": None}, {"buyer_name": "   "}],
    ids=["no name", "blank name"],
)
def test_a_missing_variable_closes_before_reading_the_catalog(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    authority = _Authority(**overrides)
    chatwoot = _Chatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    assert authority.finalizations[-1]["reason_code"] == "template_parameters_missing"
    # El nombre del caso no cambia solo: la accion se cierra sin reintento.
    assert authority.finalizations[-1]["next_attempt_at"] is None
    assert chatwoot.requests == []


def test_undeclared_variables_do_not_guess_the_body(tmp_path: Path) -> None:
    authority = _Authority()
    chatwoot = _Chatwoot()
    template = replace(TEMPLATE, first_touch_body_parameters=None)

    _run(_dispatcher(authority, chatwoot, tmp_path, template=template))

    assert authority.finalizations[-1]["reason_code"] == "approved_template_mismatch"
    assert authority.finalizations[-1]["next_attempt_at"] is None
    assert chatwoot.requests == []


def test_an_action_that_is_not_the_first_contact_is_closed_without_hermes(
    tmp_path: Path,
) -> None:
    authority = _Authority(action_type="no_reply_review")
    chatwoot = _Chatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    assert authority.finalizations[-1]["reason_code"] == (
        "approved_template_direct_unsupported_action"
    )
    assert authority.finalizations[-1]["next_attempt_at"] is None
    assert chatwoot.requests == []


# ------------------------------------------------------------ construction


class _Hermes:
    def __init__(self) -> None:
        self.calls = 0

    async def request_followup_message(self, **_: object) -> FollowupMessageProposal:
        self.calls += 1
        raise AssertionError("the direct mode must not ask Hermes")


def _kwargs(tmp_path: Path) -> dict[str, Any]:
    client = _Chatwoot().client()
    return {
        "supabase": _Authority(),
        "worker_id": "att1-dispatcher",
        "chatwoot": client,
        "chatwoot_account_id": ACCOUNT_ID,
        "chatwoot_inbox_id": INBOX_ID,
        "sender": ChatwootMessageSender(
            chatwoot=client, inbox_id=INBOX_ID, allowed_jid=None,
            dynamic_recipient_enabled=True, template=TEMPLATE,
        ),
        "commercial_ally_config": ALLY,
        "portable_recipient_enabled": True,
        "pilot_boundary": BOUNDARY,
        "final_meta_effect_gate": FinalMetaEffectGate(
            enabled=False, evidence_dir=tmp_path / "meta-effects"
        ),
        "waba_template": TEMPLATE,
        "approved_template_direct": True,
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"recovery_agent": _Hermes()}, "does not call Hermes"),
        ({"human_handoff_admission_enabled": True}, "no handoff suggestion"),
        ({"waba_template": None}, "requires the WABA templates"),
        ({"chatwoot_inbox_id": None}, "requires the Chatwoot inbox"),
        ({"sender": None}, "requires a sender"),
        (
            {"commercial_ally_config": None, "portable_recipient_enabled": False},
            "requires the portable binding",
        ),
        (
            {"approved_template_direct": False, "lead_first_name_greeting_enabled": True},
            "greeting requires approved template direct",
        ),
    ],
)
def test_the_direct_mode_refuses_an_incomplete_or_mixed_wiring(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        DurableDispatcher(**{**_kwargs(tmp_path), **overrides})


# ------------------------------------------------------ built by create_app


def test_create_app_builds_the_direct_dispatcher_without_hermes(tmp_path: Path) -> None:
    # El manifiesto de ATT1 con el carrito prendido y la plantilla de Johanna
    # en [plantillas] (la unica con catalogo capturado): la variable de entorno
    # tiene que nombrarla, o el bridge no arranca (A3).
    manifest = replace(MANIFEST, flows={**MANIFEST.flows, "carrito": True})
    manifest = replace(
        manifest,
        templates={
            **manifest.templates,
            "carrito": Template(name=CART_TEMPLATE, language="es_EC"),
        },
    )
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        commercial_ally_config=manifest.to_commercial_ally_config(),
        commercial_ally_manifest_path=Path("/instancia/instancia.toml"),
        instance_manifest=manifest,
        hermes_model_name=manifest.agent_model_name,
        chatwoot_account_id=ACCOUNT_ID,
        chatwoot_inbox_id=INBOX_ID,
        portable_hotmart_recovery_enabled=True,
        dispatcher_enabled=True,
        dispatcher_worker_id="att1-dispatcher",
        dispatcher_outbound_enabled=True,
        dispatcher_approved_template_direct_enabled=True,
        meta_final_effect_enabled=True,
        meta_final_effect_evidence_dir=tmp_path / "meta-effects",
        lead_first_name_greeting_enabled=True,
        pilot_boundary_enabled=True,
        pilot_scope_key=BOUNDARY.scope_key,
        pilot_scope_version=BOUNDARY.scope_version,
        pilot_tenant_key=BOUNDARY.tenant_key,
        pilot_channel_provider="waba",
        pilot_channel_account_ref=BOUNDARY.channel_account_ref,
        waba_first_touch_template_name=CART_TEMPLATE,
        waba_template_language="es_EC",
        waba_template_category="MARKETING",
    )
    [case] = [c for c in LEAD_NAMES if c["pattern"] == "all lowercase"]
    authority = _Authority(offer_code="bmaztyhg", buyer_name=case["full_name"])
    chatwoot = _Chatwoot()
    hermes = _Hermes()

    app = create_app(
        settings,
        supabase_client=authority,  # type: ignore[arg-type]
        chatwoot_client=chatwoot.client(),
        recovery_agent_client=hermes,  # type: ignore[arg-type]
    )
    dispatcher = app.state.durable_dispatcher
    asyncio.run(dispatcher.dispatch_due(now=NOW))

    assert hermes.calls == 0
    assert authority.events[-1] == "accepted"
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["processed_params"]["body"] == {
        "1": case["deterministic"],
        "2": "Alimenta tu Tiroides",
    }
    assert message["content"] == _expected(
        CART_TEMPLATE, first=case["deterministic"], product="Alimenta tu Tiroides"
    )


# ------------------------------------------ las RPC que manda el SupabaseClient
#
# Los tests de arriba reemplazan la base por _Authority, y
# validate_att1_portable_chain.mjs (A7) recorre la base real pero elige a mano
# el modo de la reserva y la RPC de arranque. Aca el DurableDispatcher en modo
# directo corre con el SupabaseClient real sobre un emulador de PostgREST que
# solo anota que RPC se llamo y con que cuerpo, y lo que manda se compara con
# lo que A7 declara. Si el worker cambia el modo, la RPC de arranque o el
# anchor_type que la decide, este test o A7 lo ven. El recorrido contra
# PostgREST y Postgres reales sigue siendo deuda (D10).

CHAIN_VALIDATOR = (
    Path(__file__).parent / "sql" / "followup_engine" / "validate_att1_portable_chain.mjs"
)
FORM_ANCHOR = "precheckout_intent"


class _PostgREST:
    """PostgREST de mentira: responde cada RPC con la forma de su fila real."""

    def __init__(self, authority: _Authority) -> None:
        self.authority = authority
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        prefix = "/rest/v1/rpc/"
        assert request.method == "POST" and request.url.path.startswith(prefix), (
            request.method,
            request.url.path,
        )
        operation = request.url.path.removeprefix(prefix)
        self.calls.append((operation, body))
        action = self.authority.action
        attempt = self.authority.attempt
        context = self.authority.context
        attempt_row = {
            "id": attempt.attempt_id,
            "action_id": action.action_id,
            "idempotency_key": attempt.idempotency_key,
            "attempt_number": attempt.attempt_number,
            "channel": "whatsapp",
            "mode": body.get("p_mode", attempt.mode),
            "phase": "reserved",
            "lease_generation": action.lease_generation,
            "expected_case_version": 1,
            "expected_sequence_revision": 1,
        }
        rows: dict[str, list[dict[str, Any]]] = {
            "claim_due_followup_actions": [{
                "id": action.action_id,
                "recovery_case_id": action.recovery_case_id,
                "followup_sequence_id": action.followup_sequence_id,
                "action_type": action.action_type,
                "status": action.status,
                "due_at": action.due_at,
                "expires_at": action.expires_at,
                "expected_case_version": action.expected_case_version,
                "policy_key": action.policy_key,
                "policy_version": action.policy_version,
                "step_key": action.step_key,
                "anchor_type": action.anchor_type,
                "anchor_subject_internal_id": action.anchor_subject_internal_id,
                "anchor_observed_at": action.anchor_observed_at,
                "lease_owner": action.lease_owner,
                "lease_generation": action.lease_generation,
                "lease_expires_at": action.lease_expires_at,
                "idempotency_key": action.idempotency_key,
            }],
            "get_followup_chatwoot_context": [{
                "action_id": action.action_id,
                "action_type": action.action_type,
                "chatwoot_account_id": None,
                "external_conversation_id": None,
                "expected_inbox_id": None,
                "anchor_external_message_id": None,
            }],
            "reevaluate_followup_action": [{
                "action_id": action.action_id,
                "decision": "execute",
                "reason_code": "eligible_for_execution",
                "case_version": 1,
                "sequence_revision": 1,
            }],
            "reserve_followup_delivery_attempt": [attempt_row],
            "get_followup_execution_context": [{
                "action_id": action.action_id,
                "action_type": context.action_type,
                "step_key": context.step_key,
                "recovery_case_id": context.recovery_case_id,
                "contact_id": context.contact_id,
                "source_event_id": context.source_event_id,
                "buyer_name": context.buyer_name,
                "buyer_email": context.buyer_email,
                "buyer_phone": context.buyer_phone,
                "product_name": context.product_name,
                "offer_code": context.offer_code,
                "current_goal": context.current_goal,
                "lead_stage": context.lead_stage,
            }],
            "record_and_finalize_followup_acceptance": [{
                "id": action.action_id,
                "status": "accepted_by_chatwoot",
                "terminal_reason": None,
            }],
        }
        for start in (
            "mark_lancemos_pilot_request_started",
            "mark_portable_payment_failure_request_started",
        ):
            rows[start] = [{
                **attempt_row,
                "mode": "approved_template",
                "phase": "request_started",
                "pilot_authorization_id": "pilot-authorization-att1",
                "pilot_runtime_generation": 1,
                "pilot_authorization_replayed": False,
            }]
        if operation not in rows:
            return httpx.Response(404, json={"message": f"not emulated: {operation}"})
        return httpx.Response(200, json=rows[operation])

    def client(self) -> SupabaseClient:
        return SupabaseClient(
            base_url="https://postgrest.test",
            service_role_key="service-role-test",
            transport=httpx.MockTransport(self.handler),
        )

    def operations(self) -> list[str]:
        return [operation for operation, _ in self.calls]

    def body_of(self, operation: str) -> dict[str, Any]:
        [body] = [body for name, body in self.calls if name == operation]
        return body


def _chain_validator_operations(name: str) -> dict[str, str]:
    """An ``anchor_type -> RPC`` table A7 declares as an object literal."""
    source = CHAIN_VALIDATOR.read_text(encoding="utf-8")
    [block] = re.findall(rf"const {name} = \{{\n(.*?)\n\}};", source, flags=re.DOTALL)
    lines = [line.strip() for line in block.splitlines()]
    entries = [re.fullmatch(r"([a-z_]+): '([a-z_]+)',", line) for line in lines]
    assert all(entries), lines
    return {entry.group(1): entry.group(2) for entry in entries if entry}


def _chain_validator_contract() -> tuple[str, dict[str, str], dict[str, str]]:
    """The reservation mode and the start and reevaluation RPCs A7 declares."""
    source = CHAIN_VALIDATOR.read_text(encoding="utf-8")
    [mode] = re.findall(r"const DIRECT_DELIVERY_MODE = '([a-z_]+)';", source)
    return (
        mode,
        _chain_validator_operations("START_OPERATION_BY_ANCHOR"),
        _chain_validator_operations("REEVALUATE_OPERATION_BY_ANCHOR"),
    )


def _chain_validator_anchors() -> set[str]:
    source = CHAIN_VALIDATOR.read_text(encoding="utf-8")
    return set(re.findall(r"anchor: '([a-z_]+)'", source))


@pytest.mark.parametrize(
    ("anchor_type", "offer_code"),
    [("cart_abandonment", "gopi6lh7"), ("payment_failure", "2uafw5bg")],
)
def test_the_real_supabase_client_sends_what_the_att1_chain_validates(
    tmp_path: Path, anchor_type: str, offer_code: str
) -> None:
    authority = _Authority(offer_code=offer_code, anchor_type=anchor_type)
    postgrest = _PostgREST(authority)
    chatwoot = _Chatwoot()
    client = chatwoot.client()
    dispatcher = DurableDispatcher(
        supabase=postgrest.client(),
        worker_id="att1-dispatcher",
        chatwoot=client,
        chatwoot_account_id=ACCOUNT_ID,
        chatwoot_inbox_id=INBOX_ID,
        sender=ChatwootMessageSender(
            chatwoot=client,
            inbox_id=INBOX_ID,
            allowed_jid=None,
            dynamic_recipient_enabled=True,
            template=TEMPLATE,
        ),
        allowed_jid=None,
        commercial_ally_config=ALLY,
        portable_recipient_enabled=True,
        pilot_boundary=BOUNDARY,
        clock=lambda: FINAL_NOW,
        final_meta_effect_gate=FinalMetaEffectGate(
            enabled=True, evidence_dir=tmp_path / "meta-effects"
        ),
        waba_template=TEMPLATE,
        approved_template_direct=True,
    )

    decisions = _run(dispatcher)

    mode, start_by_anchor, reevaluate_by_anchor = _chain_validator_contract()
    operations = postgrest.operations()
    assert decisions[-1].decision == "execute"
    assert postgrest.body_of("reserve_followup_delivery_attempt")["p_mode"] == mode
    assert [name for name in operations if name.startswith("mark_")] == [
        start_by_anchor[anchor_type]
    ]
    assert [name for name in operations if name.startswith("reevaluate_")] == [
        reevaluate_by_anchor[anchor_type]
    ] * 2
    assert "finalize_followup_delivery_attempt" not in operations
    assert operations[-1] == "record_and_finalize_followup_acceptance"
    # A7 recorre exactamente estos tres anchor_type, los que escriben los
    # planificadores (20260803000100, 20260903000300 y 20261001000200). El del
    # formulario se compara mas abajo, con sus RPC propias.
    assert anchor_type in _chain_validator_anchors()
    assert _chain_validator_anchors() == {
        "cart_abandonment", "payment_failure", FORM_ANCHOR,
    }
    assert chatwoot.posts("/conversations/200/messages")


def test_the_att1_chain_validator_still_declares_its_contract() -> None:
    # Si A7 deja de declarar el modo o la regla de arranque, el test de arriba
    # no tiene contra que comparar: se rompe aca, con un mensaje claro.
    assert _chain_validator_contract() == (
        "approved_template",
        {
            "cart_abandonment": "mark_lancemos_pilot_request_started",
            "payment_failure": "mark_portable_payment_failure_request_started",
            "precheckout_intent": "mark_portable_precheckout_request_started",
        },
        {
            "cart_abandonment": "reevaluate_followup_action",
            "payment_failure": "reevaluate_followup_action",
            "precheckout_intent": "reevaluate_portable_precheckout_action",
        },
    )


# ------------------------------ equivalencia de telefonos antes del gate final
#
# Con whatsapp_equivalence_enabled el dispatcher resuelve el contacto de
# Chatwoot ANTES del gate final y del arranque del pedido: el gate hashea el
# wa_id que Meta va a recibir, no el telefono como lo guarda el contacto de la
# base. El flag es explicito: no se deduce de portable_recipient_enabled, asi
# que los tests de arriba (sin el flag) no cambian.
#
# Chatwoot: el precedente inline de este archivo (no hay captura de
# /contacts/search). El source_id de un contacto de WhatsApp Cloud es su wa_id,
# medido el 2026-10-01. Los telefonos son de prueba.

MX_FORM = "525512345678"  # 52 + 10: lo que guardo el formulario
MX_WHATSAPP = "5215512345678"  # 521 + 10: el wa_id


class _ChatwootWithContacts(_Chatwoot):
    """El mismo Chatwoot, con los contactos que ya existen en el inbox."""

    def __init__(
        self,
        contacts: dict[str, tuple[int, str]] | None = None,
        *,
        search_status: int = 200,
    ) -> None:
        super().__init__()
        # telefono E.164 -> (id del contacto, source_id en el inbox)
        self.contacts = dict(contacts or {})
        self.search_status = search_status
        self.searches: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        prefix = f"/api/v1/accounts/{ACCOUNT_ID}"
        if request.method == "GET" and path == f"{prefix}/contacts/search":
            self.requests.append((request.method, path, None))
            query = request.url.params["q"]
            self.searches.append(query)
            if self.search_status != 200:
                return httpx.Response(self.search_status, json={"error": "unavailable"})
            found = self.contacts.get(query)
            if found is None:
                return httpx.Response(200, json={"payload": []})
            contact_id, source_id = found
            return httpx.Response(200, json={"payload": [{
                "id": contact_id,
                "phone_number": query,
                "blocked": False,
                "contact_inboxes": [{"source_id": source_id, "inbox": {"id": INBOX_ID}}],
            }]})
        if request.method == "POST" and path == f"{prefix}/contacts":
            body = json.loads(request.content)
            self.requests.append((request.method, path, body))
            phone = body["phone_number"]
            self.contacts[phone] = (55, phone.lstrip("+"))
            return httpx.Response(200, json={"payload": {"id": 55}})
        return super().handler(request)


class _AuthorityWatchingChatwoot(_Authority):
    """Anota que busquedas de Chatwoot ya habian salido al arrancar el pedido."""

    def __init__(self, chatwoot: _ChatwootWithContacts, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._chatwoot = chatwoot
        self.searches_before_request_start: list[str] | None = None
        self.context = replace(self.context, buyer_phone=MX_FORM)

    async def mark_followup_request_started(self, **kwargs: object) -> DeliveryAttempt:
        self.searches_before_request_start = list(self._chatwoot.searches)
        return await super().mark_followup_request_started(**kwargs)


def _equivalence_dispatcher(
    authority: _Authority,
    chatwoot: _Chatwoot,
    tmp_path: Path,
    *,
    gate_open: bool = True,
) -> DurableDispatcher:
    client = chatwoot.client()
    return DurableDispatcher(
        supabase=authority,  # type: ignore[arg-type]
        worker_id="att1-dispatcher",
        chatwoot=client,
        chatwoot_account_id=ACCOUNT_ID,
        chatwoot_inbox_id=INBOX_ID,
        sender=ChatwootMessageSender(
            chatwoot=client,
            inbox_id=INBOX_ID,
            allowed_jid=None,
            dynamic_recipient_enabled=True,
            template=TEMPLATE,
            whatsapp_equivalence_enabled=True,
        ),
        allowed_jid=None,
        commercial_ally_config=ALLY,
        portable_recipient_enabled=True,
        pilot_boundary=BOUNDARY,
        clock=lambda: FINAL_NOW,
        final_meta_effect_gate=FinalMetaEffectGate(
            enabled=gate_open, evidence_dir=tmp_path / "meta-effects"
        ),
        waba_template=TEMPLATE,
        approved_template_direct=True,
        whatsapp_equivalence_enabled=True,
    )


def _gate_target(tmp_path: Path) -> str:
    [evidence_file] = (tmp_path / "meta-effects").glob("*.json")
    return json.loads(evidence_file.read_text(encoding="utf-8"))["target_sha256"]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_the_gate_records_the_wa_id_of_the_chatwoot_contact_that_exists(
    tmp_path: Path,
) -> None:
    # La base guarda 52 + 10 y la persona ya escribio: su contacto de Chatwoot
    # es 521 + 10, y ahi es adonde va a entregar Meta.
    chatwoot = _ChatwootWithContacts({f"+{MX_WHATSAPP}": (41, MX_WHATSAPP)})
    authority = _AuthorityWatchingChatwoot(chatwoot)

    _run(_equivalence_dispatcher(authority, chatwoot, tmp_path, gate_open=False))

    assert _gate_target(tmp_path) == _sha256(f"+{MX_WHATSAPP}")
    assert _gate_target(tmp_path) != _sha256(f"+{MX_FORM}")
    assert authority.finalizations[-1]["reason_code"] == "final_meta_gate_closed"
    assert "request_started" not in authority.events
    # Con el gate cerrado no se escribe nada en Chatwoot: solo lecturas.
    assert chatwoot.posts("/contacts") == []
    assert chatwoot.posts("/conversations") == []


def test_the_send_goes_through_the_resolved_contact_without_searching_again(
    tmp_path: Path,
) -> None:
    chatwoot = _ChatwootWithContacts({f"+{MX_WHATSAPP}": (41, MX_WHATSAPP)})
    authority = _AuthorityWatchingChatwoot(chatwoot)

    _run(_equivalence_dispatcher(authority, chatwoot, tmp_path))

    assert authority.events == [
        "reevaluate", "reserve", "reevaluate", "request_started", "accepted",
    ]
    # Las dos formas se buscaron antes de arrancar el pedido, y despues nada.
    assert authority.searches_before_request_start == [
        f"+{MX_FORM}", f"+{MX_WHATSAPP}",
    ]
    assert chatwoot.searches == [f"+{MX_FORM}", f"+{MX_WHATSAPP}"]
    assert chatwoot.posts("/contacts") == []
    assert chatwoot.posts("/conversations") == [
        {"inbox_id": INBOX_ID, "contact_id": 41, "source_id": MX_WHATSAPP}
    ]
    assert chatwoot.posts("/conversations/200/messages")


def test_without_a_chatwoot_contact_the_gate_records_the_delivery_form(
    tmp_path: Path,
) -> None:
    # Nadie escribio todavia: el contacto nace con la forma de entrega (Mexico
    # con el 1), y esa es la que hashea el gate.
    chatwoot = _ChatwootWithContacts()
    authority = _AuthorityWatchingChatwoot(chatwoot)

    _run(_equivalence_dispatcher(authority, chatwoot, tmp_path, gate_open=False))

    assert _gate_target(tmp_path) == _sha256(f"+{MX_WHATSAPP}")
    assert chatwoot.posts("/contacts") == []

    opened_chatwoot = _ChatwootWithContacts()
    opened = _AuthorityWatchingChatwoot(opened_chatwoot)
    _run(_equivalence_dispatcher(opened, opened_chatwoot, tmp_path / "abierto"))

    assert opened.events[-1] == "accepted"
    [contact] = opened_chatwoot.posts("/contacts")
    assert contact["phone_number"] == f"+{MX_WHATSAPP}"
    assert opened_chatwoot.posts("/conversations") == [
        {"inbox_id": INBOX_ID, "contact_id": 55, "source_id": MX_WHATSAPP}
    ]


def test_a_chatwoot_contact_of_another_number_fails_closed_for_good(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # El contacto se encuentra por su telefono, pero Chatwoot entregaria a su
    # source_id y ese no es el movil consentido: no sale y no se reintenta.
    chatwoot = _ChatwootWithContacts({f"+{MX_WHATSAPP}": (41, "5215599999999")})
    authority = _AuthorityWatchingChatwoot(chatwoot)

    with caplog.at_level("WARNING"):
        decisions = _run(_equivalence_dispatcher(authority, chatwoot, tmp_path))

    assert decisions[-1].decision == "execute"
    [finalization] = authority.finalizations
    assert finalization["outcome"] == "failed_before_request"
    assert finalization["reason_code"] == "chatwoot_recipient_phone_mismatch"
    assert finalization["next_attempt_at"] is None
    assert "request_started" not in authority.events
    assert not (tmp_path / "meta-effects").exists()
    assert chatwoot.posts("/conversations") == []
    assert "durable_first_touch_recipient_mismatch" in caplog.text
    assert "region=MX" in caplog.text and "chatwoot_contact_id=41" in caplog.text
    for number in (MX_FORM, MX_WHATSAPP, "5215599999999"):
        assert number not in caplog.text


def test_a_chatwoot_failure_while_resolving_is_a_retryable_pre_request_failure(
    tmp_path: Path,
) -> None:
    chatwoot = _ChatwootWithContacts(search_status=503)
    authority = _AuthorityWatchingChatwoot(chatwoot)

    decisions = _run(_equivalence_dispatcher(authority, chatwoot, tmp_path))

    # No levanta: el resto del lote sigue.
    assert decisions[-1].decision == "execute"
    [finalization] = authority.finalizations
    assert finalization["outcome"] == "failed_before_request"
    assert finalization["reason_code"] == "pre_request_failed"
    assert finalization["next_attempt_at"] is not None
    # La busqueda es anterior a la segunda reevaluacion, al gate y al arranque.
    assert authority.events == ["reevaluate", "reserve"]
    assert not (tmp_path / "meta-effects").exists()


def test_the_equivalence_needs_the_portable_binding() -> None:
    with pytest.raises(ValueError, match="requires the portable binding"):
        DurableDispatcher(
            supabase=_Authority(),  # type: ignore[arg-type]
            worker_id="johanna-dispatcher",
            whatsapp_equivalence_enabled=True,
        )
    with pytest.raises(ValueError, match="invalid whatsapp equivalence flag"):
        DurableDispatcher(
            supabase=_Authority(),  # type: ignore[arg-type]
            worker_id="johanna-dispatcher",
            whatsapp_equivalence_enabled=1,  # type: ignore[arg-type]
        )


def test_without_the_flag_the_dispatcher_keeps_the_exact_phone(tmp_path: Path) -> None:
    # El mismo caso que el primero, sin el flag: el gate hashea el telefono
    # como lo guarda la base y el sender busca solo esa forma. Es lo que hace
    # hoy todo runtime sin el flag.
    chatwoot = _ChatwootWithContacts({f"+{MX_WHATSAPP}": (41, MX_WHATSAPP)})
    authority = _AuthorityWatchingChatwoot(chatwoot)

    _run(_dispatcher(authority, chatwoot, tmp_path, gate_open=False))

    assert _gate_target(tmp_path) == _sha256(f"+{MX_FORM}")
    assert chatwoot.searches == []


def test_create_app_turns_the_equivalence_on_only_for_the_portable_sender(
    tmp_path: Path,
) -> None:
    # Por create_app, con el carrito portable prendido: el dispatcher y el
    # sender que arma el bridge comparan por forma equivalente.
    manifest = replace(
        MANIFEST,
        flows={**MANIFEST.flows, "carrito": True},
        templates={
            **MANIFEST.templates,
            "carrito": Template(
                name=CART_TEMPLATE, language="es_EC", parameters=("nombre", "producto")
            ),
        },
    )
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        commercial_ally_config=manifest.to_commercial_ally_config(),
        commercial_ally_manifest_path=Path("/instancia/instancia.toml"),
        instance_manifest=manifest,
        hermes_model_name=manifest.agent_model_name,
        chatwoot_account_id=ACCOUNT_ID,
        chatwoot_inbox_id=INBOX_ID,
        portable_hotmart_recovery_enabled=True,
        dispatcher_enabled=True,
        dispatcher_worker_id="att1-dispatcher",
        dispatcher_outbound_enabled=True,
        dispatcher_approved_template_direct_enabled=True,
        meta_final_effect_enabled=False,
        meta_final_effect_evidence_dir=tmp_path / "meta-effects",
        pilot_boundary_enabled=True,
        pilot_scope_key=BOUNDARY.scope_key,
        pilot_scope_version=BOUNDARY.scope_version,
        pilot_tenant_key=BOUNDARY.tenant_key,
        pilot_channel_provider="waba",
        pilot_channel_account_ref=BOUNDARY.channel_account_ref,
        waba_first_touch_template_name=CART_TEMPLATE,
        waba_template_language="es_EC",
        waba_template_category="MARKETING",
    )
    chatwoot = _ChatwootWithContacts({f"+{MX_WHATSAPP}": (41, MX_WHATSAPP)})
    authority = _AuthorityWatchingChatwoot(chatwoot)

    app = create_app(
        settings,
        supabase_client=authority,  # type: ignore[arg-type]
        chatwoot_client=chatwoot.client(),
    )
    asyncio.run(app.state.durable_dispatcher.dispatch_due(now=NOW))

    assert _gate_target(tmp_path) == _sha256(f"+{MX_WHATSAPP}")
    assert chatwoot.searches == [f"+{MX_FORM}", f"+{MX_WHATSAPP}"]


# ------------------------------------------- primer contacto tras el formulario
#
# Una accion de ancla precheckout_intent (la planifica
# admit_and_plan_portable_lead_precheckout) sale con la plantilla propia del
# flujo, y sin ella no sale. La plantilla es att1_interes_precheckout_01, la de
# [plantillas.precheckout] del manifiesto de ATT1, leida del catalogo capturado
# del inbox 11 (dos variables, es_MX, MARKETING y tres botones QUICK_REPLY). El
# carrito y el pago fallido de esta configuracion son tambien los del
# manifiesto.

ATT1_TEMPLATE_NAMES = {
    "cart_abandonment": MANIFEST.templates["carrito"].name,
    "payment_failure": MANIFEST.templates["pago_fallido"].name,
    FORM_ANCHOR: MANIFEST.templates["precheckout"].name,
}
FORM_TEMPLATE = ATT1_TEMPLATE_NAMES[FORM_ANCHOR]
ATT1_CART_TEMPLATE = ATT1_TEMPLATE_NAMES["cart_abandonment"]
ATT1_LANGUAGE = MANIFEST.templates["precheckout"].language
# Sin la plantilla del formulario: lo que tiene una instancia que no prendio
# el primer contacto.
ATT1_WITHOUT_FORM_TEMPLATE = WhatsAppTemplateConfig(
    first_touch_name=ATT1_CART_TEMPLATE,
    payment_failure_name=ATT1_TEMPLATE_NAMES["payment_failure"],
    followup_name=None,
    language=ATT1_LANGUAGE,
    category="MARKETING",
    first_touch_parameter="buyer_name_and_product",
    first_touch_body_parameters=("nombre", "producto"),
    payment_failure_body_parameters=("nombre", "producto"),
)
WITH_FORM_TEMPLATE = replace(
    ATT1_WITHOUT_FORM_TEMPLATE,
    precheckout_name=FORM_TEMPLATE,
    precheckout_body_parameters=("nombre", "producto"),
)


def test_the_att1_manifest_names_the_templates_of_the_captured_catalog() -> None:
    assert ATT1_TEMPLATE_NAMES == {
        "cart_abandonment": "att1_carrito_abandonado_01",
        "payment_failure": "att1_compra_fallida_01",
        FORM_ANCHOR: "att1_interes_precheckout_01",
    }
    assert {MANIFEST.templates[key].language for key in (
        "carrito", "pago_fallido", "precheckout",
    )} == {"es_MX"}
    by_name = {template["name"]: template for template in ATT1_CATALOG}
    for name in ATT1_TEMPLATE_NAMES.values():
        captured = by_name[name]
        assert (captured["status"], captured["language"], captured["category"]) == (
            "APPROVED", "es_MX", "MARKETING",
        )


@pytest.mark.parametrize(
    ("anchor_type", "offer_code"),
    [
        ("cart_abandonment", "gopi6lh7"),
        ("payment_failure", "2uafw5bg"),
        (FORM_ANCHOR, "bmaztyhg"),
    ],
)
def test_each_att1_trigger_sends_its_template_of_the_captured_inbox_11_catalog(
    tmp_path: Path, anchor_type: str, offer_code: str
) -> None:
    name = ATT1_TEMPLATE_NAMES[anchor_type]
    authority = _Authority(offer_code=offer_code, anchor_type=anchor_type)
    chatwoot = _Chatwoot(ATT1_CATALOG)

    decisions = _run(
        _dispatcher(authority, chatwoot, tmp_path, template=WITH_FORM_TEMPLATE)
    )

    expected = _expected(name, first="Edith García Pérez", product="Alimenta tu Tiroides")
    assert decisions[-1].decision == "execute"
    assert authority.events == [
        "reevaluate", "reserve", "reevaluate", "request_started", "accepted",
    ]
    assert chatwoot.inbox_reads() == 1
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["content"] == expected
    assert message["template_params"] == {
        "name": name,
        "category": "MARKETING",
        "language": "es_MX",
        "processed_params": {
            "body": {"1": "Edith García Pérez", "2": "Alimenta tu Tiroides"}
        },
    }
    assert authority.acceptances[0]["message_content"] == expected
    assert authority.finalizations == []


def test_the_three_att1_triggers_send_three_different_texts() -> None:
    assert len({
        _expected(name, first="Edith", product="Alimenta tu Tiroides")
        for name in ATT1_TEMPLATE_NAMES.values()
    }) == 3


def test_the_att1_templates_asked_in_the_language_of_johanna_are_not_sent(
    tmp_path: Path,
) -> None:
    # WABA_TEMPLATE_LANGUAGE mal cargada (es_EC, la de Johanna) contra el
    # catalogo real: no cierra y no manda nada.
    authority = _Authority(offer_code="gopi6lh7", anchor_type=FORM_ANCHOR)
    chatwoot = _Chatwoot(ATT1_CATALOG)

    _run(
        _dispatcher(
            authority, chatwoot, tmp_path,
            template=replace(WITH_FORM_TEMPLATE, language="es_EC"),
        )
    )

    assert chatwoot.posts("/messages") == []
    assert "request_started" not in authority.events
    assert authority.finalizations[-1]["reason_code"] == "approved_template_mismatch"


def test_the_form_first_contact_sends_its_own_approved_template(tmp_path: Path) -> None:
    authority = _Authority(offer_code="gopi6lh7", anchor_type=FORM_ANCHOR)
    chatwoot = _Chatwoot(ATT1_CATALOG)

    decisions = _run(
        _dispatcher(authority, chatwoot, tmp_path, template=WITH_FORM_TEMPLATE)
    )

    expected = _expected(
        FORM_TEMPLATE, first="Edith García Pérez", product="Alimenta tu Tiroides"
    )
    assert expected != _expected(
        ATT1_CART_TEMPLATE, first="Edith García Pérez", product="Alimenta tu Tiroides"
    )
    assert expected == (
        "Hola, Edith García Pérez. Soy del equipo de Dra. Nina.\n\n"
        "Completaste el formulario para recibir información sobre "
        "Alimenta tu Tiroides. Si tienes alguna duda, puedo ayudarte. También "
        "puedo enviarte el enlace para que continúes cuando quieras."
    )
    assert decisions[-1].decision == "execute"
    assert authority.events == [
        "reevaluate", "reserve", "reevaluate", "request_started", "accepted",
    ]
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["content"] == expected
    assert message["template_params"] == {
        "name": FORM_TEMPLATE,
        "category": "MARKETING",
        "language": "es_MX",
        "processed_params": {
            "body": {"1": "Edith García Pérez", "2": "Alimenta tu Tiroides"}
        },
    }
    assert authority.acceptances[0]["message_content"] == expected
    assert authority.finalizations == []


def test_the_form_first_contact_final_gate_records_its_own_template(
    tmp_path: Path,
) -> None:
    authority = _Authority(offer_code="gopi6lh7", anchor_type=FORM_ANCHOR)
    chatwoot = _Chatwoot(ATT1_CATALOG)

    _run(
        _dispatcher(
            authority, chatwoot, tmp_path, gate_open=False, template=WITH_FORM_TEMPLATE
        )
    )

    [evidence_file] = list((tmp_path / "meta-effects").glob("*.json"))
    evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert evidence["template_name"] == FORM_TEMPLATE
    assert evidence["content_sha256"] == hashlib.sha256(
        _expected(
            FORM_TEMPLATE, first="Edith García Pérez", product="Alimenta tu Tiroides"
        ).encode("utf-8")
    ).hexdigest()
    assert authority.finalizations[-1]["reason_code"] == "final_meta_gate_closed"
    assert chatwoot.posts("/messages") == []


def test_the_form_first_contact_without_its_template_is_never_sent(
    tmp_path: Path,
) -> None:
    # Falla cerrado antes de leer el catalogo: no busca ni crea el contacto, no
    # pasa por el gate final ni arranca el pedido, y no usa la plantilla del
    # carrito. Cierra la accion en el primer intento.
    authority = _Authority(offer_code="gopi6lh7", anchor_type=FORM_ANCHOR)
    chatwoot = _Chatwoot(ATT1_CATALOG)

    decisions = _run(
        _dispatcher(authority, chatwoot, tmp_path, template=ATT1_WITHOUT_FORM_TEMPLATE)
    )

    assert decisions[-1].decision == "execute"
    assert authority.events == ["reevaluate", "reserve"]
    assert chatwoot.requests == []
    [finalization] = authority.finalizations
    assert finalization["outcome"] == "failed_before_request"
    assert finalization["reason_code"] == "first_touch_template_not_configured"
    assert finalization["next_attempt_at"] is None
    assert not (tmp_path / "meta-effects").exists()


# Un nombre que no es un nombre. El formulario publico (y Hotmart) traen el
# nombre con un telefono que nadie verifico, y el saludo cae al nombre completo
# cuando la primera palabra no sirve; sin saludo, el nombre va tal cual. La
# plantilla del formulario de ATT1 dice "Hola, {{1}}. Soy del equipo de Dra.
# Nina.": sin el filtro, una URL, un email o un texto largo salian ahi.

_NOT_A_NAME = [
    pytest.param("http://evil.example/premio", id="url"),
    pytest.param("soporte@evil.example", id="email"),
    pytest.param("\U0001F381 evil.example", id="emoji and domain"),
    pytest.param("x" * 61, id="61 characters"),
    pytest.param("5512345678", id="digits"),
]


@pytest.mark.parametrize("greeting", [True, False], ids=["greeting", "no greeting"])
@pytest.mark.parametrize("name", _NOT_A_NAME)
def test_the_form_first_contact_refuses_a_name_that_is_not_a_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, name: str, greeting: bool
) -> None:
    authority = _Authority(
        offer_code="gopi6lh7", anchor_type=FORM_ANCHOR, buyer_name=name
    )
    chatwoot = _Chatwoot(ATT1_CATALOG)

    with caplog.at_level(logging.WARNING, logger="bridge.worker"):
        decisions = _run(
            _dispatcher(
                authority, chatwoot, tmp_path,
                greeting=greeting, template=WITH_FORM_TEMPLATE,
            )
        )

    assert decisions[-1].decision == "execute"
    assert chatwoot.posts("/messages") == []
    # Cierra antes del catalogo: no busca ni crea el contacto.
    assert chatwoot.requests == []
    assert "request_started" not in authority.events
    [finalization] = authority.finalizations
    assert finalization["outcome"] == "failed_before_request"
    assert finalization["reason_code"] == "template_parameters_missing"
    # El nombre del caso no cambia solo: sin reintento.
    assert finalization["next_attempt_at"] is None
    assert not (tmp_path / "meta-effects").exists()
    assert "approved_template_name_refused" in caplog.text
    # El log no lleva el nombre: es dato personal y es el texto del atacante.
    assert name not in caplog.text


@pytest.mark.parametrize("greeting", [True, False], ids=["greeting", "no greeting"])
@pytest.mark.parametrize("case", LEAD_NAMES, ids=[c["pattern"] for c in LEAD_NAMES])
def test_the_form_first_contact_still_greets_every_captured_name(
    tmp_path: Path, case: dict[str, Any], greeting: bool
) -> None:
    # El costo del filtro, medido: los 19 patrones capturados del inbox 9
    # (incluidos una sola letra, iniciales con emoji y solo emoji) siguen
    # saliendo, con saludo y sin saludo.
    authority = _Authority(
        offer_code="gopi6lh7", anchor_type=FORM_ANCHOR, buyer_name=case["full_name"]
    )
    chatwoot = _Chatwoot(ATT1_CATALOG)

    _run(
        _dispatcher(
            authority, chatwoot, tmp_path, greeting=greeting, template=WITH_FORM_TEMPLATE
        )
    )

    if greeting:
        first = case["deterministic"] or case["full_name"].strip()
    else:
        first = " ".join(case["full_name"].split())
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["processed_params"]["body"] == {
        "1": first,
        "2": "Alimenta tu Tiroides",
    }
    assert message["content"] == _expected(
        FORM_TEMPLATE, first=first, product="Alimenta tu Tiroides"
    )
    assert authority.events[-1] == "accepted"


def test_a_template_without_the_name_does_not_look_at_it(tmp_path: Path) -> None:
    # El filtro mira lo que va a la plantilla. Si la plantilla no declara
    # `nombre`, el nombre no sale en el mensaje y no frena el envio.
    authority = _Authority(buyer_name="http://evil.example/premio")
    chatwoot = _Chatwoot(ONE_VARIABLE_CATALOG)
    template = replace(ONE_VARIABLE, first_touch_body_parameters=("producto",))

    _run(_dispatcher(authority, chatwoot, tmp_path, greeting=True, template=template))

    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["processed_params"] == {
        "body": {"1": "Alimenta tu Tiroides"}
    }
    assert "evil.example" not in message["content"]
    assert authority.events[-1] == "accepted"


class _TwoActionsAuthority(_Authority):
    """Dos acciones reclamadas en el mismo lote; la base rechaza el arranque de la primera."""

    def __init__(self, *, rejection: Exception, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rejection = rejection
        self.second_action = replace(
            self.action,
            action_id="action-att1-2",
            recovery_case_id="case-att1-2",
            followup_sequence_id="sequence-att1-2",
            idempotency_key=f"{self.action.anchor_type}:first_contact:case-att1-2",
        )
        self.starts: list[str] = []

    def _action(self, action_id: object) -> ScheduledAction:
        return self.action if action_id == self.action.action_id else self.second_action

    async def claim_due_followup_actions(self, **_: object) -> list[ScheduledAction]:
        return [self.action, self.second_action]

    async def get_followup_chatwoot_context(self, **kwargs: object) -> ChatwootAuthorityContext:
        return replace(
            await super().get_followup_chatwoot_context(),
            action_id=str(kwargs["action_id"]),
        )

    async def reevaluate_followup_action(self, **kwargs: object) -> ReevaluationDecision:
        return replace(
            await super().reevaluate_followup_action(),
            action_id=str(kwargs["action_id"]),
        )

    async def reserve_followup_delivery_attempt(self, **kwargs: object) -> DeliveryAttempt:
        action = self._action(kwargs["action_id"])
        return replace(
            await super().reserve_followup_delivery_attempt(**kwargs),
            attempt_id=f"attempt-of-{action.action_id}",
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
        )

    async def get_followup_execution_context(self, **kwargs: object) -> FollowupExecutionContext:
        action = self._action(kwargs["action_id"])
        return replace(
            self.context,
            action_id=action.action_id,
            recovery_case_id=action.recovery_case_id,
        )

    async def mark_followup_request_started(self, **kwargs: object) -> DeliveryAttempt:
        action = self._action(kwargs["action_id"])
        self.starts.append(action.action_id)
        if action.action_id == self.action.action_id:
            raise self.rejection
        self.events.append("request_started")
        return replace(
            self.attempt,
            attempt_id=str(kwargs["attempt_id"]),
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            phase="request_started",
        )


@pytest.mark.parametrize(
    "reason",
    ["pilot_daily_budget_exhausted", "pilot_runtime_not_armed", "precheckout_conversation_handoff"],
)
def test_a_rejected_request_start_does_not_cut_the_batch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, reason: str
) -> None:
    # La base rechaza el arranque de la primera accion (un tope del scope, el
    # piloto desarmado, o un freno que entro despues de la reevaluacion final).
    # Antes el error subia sin atrapar: cortaba el lote, la segunda accion
    # quedaba con el lease tomado y sin procesar, y el motivo se perdia.
    authority = _TwoActionsAuthority(
        rejection=PilotRequestStartRejectedError(reason),
        anchor_type="precheckout_intent",
        offer_code="2uafw5bg",
    )
    chatwoot = _Chatwoot(ATT1_CATALOG)

    with caplog.at_level("WARNING", logger="bridge.worker"):
        decisions = _run(
            _dispatcher(authority, chatwoot, tmp_path, template=WITH_FORM_TEMPLATE)
        )

    # Las dos acciones se intentaron; solo la segunda salio.
    assert authority.starts == ["action-att1", "action-att1-2"]
    assert [decision.action_id for decision in decisions] == ["action-att1", "action-att1-2"]
    [sent] = chatwoot.posts("/conversations/200/messages")
    assert sent["template_params"]["name"] == FORM_TEMPLATE
    [accepted] = authority.acceptances
    assert accepted["action_id"] == "action-att1-2"
    # El intento rechazado queda reservado: lo resuelve el lease siguiente (la
    # reevaluacion cancela el caso con el freno, o la accion vence con el).
    # Cerrarlo sin reintento dejaria la accion permanent_failed y el caso
    # abierto para siempre.
    assert authority.finalizations == []
    # Y el motivo queda en el log, con ids y un codigo.
    [warning] = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("durable_request_start_rejected")
    ]
    assert warning == (
        "durable_request_start_rejected action_id=action-att1 "
        f"attempt_id=attempt-of-action-att1 anchor=precheckout_intent reason={reason}"
    )
    assert PHONE not in warning


def test_any_other_failed_request_start_still_stops_the_batch(tmp_path: Path) -> None:
    # Una falla que no es un rechazo (la base caida, una respuesta rara) sigue
    # subiendo: no se sabe si el arranque quedo o no, y no se sigue mandando.
    authority = _TwoActionsAuthority(
        rejection=SupabaseError("mark_portable_precheckout_request_started_failed: HTTP 503"),
        anchor_type="precheckout_intent",
        offer_code="2uafw5bg",
    )
    chatwoot = _Chatwoot(ATT1_CATALOG)

    with pytest.raises(SupabaseError, match="HTTP 503"):
        _run(_dispatcher(authority, chatwoot, tmp_path, template=WITH_FORM_TEMPLATE))

    assert authority.starts == ["action-att1"]
    assert chatwoot.posts("/conversations/200/messages") == []


def test_the_composition_refuses_the_form_anchor_without_its_template(
    tmp_path: Path,
) -> None:
    # La segunda barrera, por si la del dispatcher cambia: la composicion del
    # modo directo tampoco arma nada, con el motivo propio y sin leer el
    # catalogo.
    authority = _Authority(offer_code="gopi6lh7", anchor_type=FORM_ANCHOR)
    chatwoot = _Chatwoot(ATT1_CATALOG)
    dispatcher = _dispatcher(
        authority, chatwoot, tmp_path, template=ATT1_WITHOUT_FORM_TEMPLATE
    )

    composition = asyncio.run(
        dispatcher._compose_approved_template_proposal(  # type: ignore[attr-defined]
            action=authority.action, execution_context=authority.context
        )
    )

    assert composition.proposal is None
    assert composition.failure_reason == "first_touch_template_not_configured"
    assert composition.retryable is False
    assert chatwoot.requests == []


class _FirstContactPostgREST(_PostgREST):
    """El mismo PostgREST, con las RPC propias del ancla precheckout_intent.

    Las dos devuelven la misma tabla que su par: la reevaluacion la de
    reevaluate_followup_action, y el arranque la de
    mark_portable_payment_failure_request_started, de la que es copia
    (migracion 20261001000200).
    """

    SIBLINGS = {
        "reevaluate_portable_precheckout_action": "reevaluate_followup_action",
        "mark_portable_precheckout_request_started": (
            "mark_portable_payment_failure_request_started"
        ),
    }

    def handler(self, request: httpx.Request) -> httpx.Response:
        prefix = "/rest/v1/rpc/"
        operation = request.url.path.removeprefix(prefix)
        sibling = self.SIBLINGS.get(operation)
        if sibling is None:
            return super().handler(request)
        response = super().handler(
            httpx.Request(
                request.method,
                request.url.copy_with(path=prefix + sibling),
                content=request.content,
            )
        )
        self.calls[-1] = (operation, self.calls[-1][1])
        return response


def test_the_real_supabase_client_uses_the_first_contact_rpcs_for_its_anchor(
    tmp_path: Path,
) -> None:
    # La reevaluacion compartida sola ejecutaria sin mirar los frenos del
    # primer contacto (compra, carrito, opt-out, consentimiento), y el arranque
    # compartido autorizaria contra otro scope: para este ancla el worker y el
    # cliente usan solo las RPC propias, las dos veces que reevaluan.
    authority = _Authority(offer_code="gopi6lh7", anchor_type=FORM_ANCHOR)
    postgrest = _FirstContactPostgREST(authority)
    chatwoot = _Chatwoot(ATT1_CATALOG)
    client = chatwoot.client()
    dispatcher = DurableDispatcher(
        supabase=postgrest.client(),
        worker_id="att1-dispatcher",
        chatwoot=client,
        chatwoot_account_id=ACCOUNT_ID,
        chatwoot_inbox_id=INBOX_ID,
        sender=ChatwootMessageSender(
            chatwoot=client,
            inbox_id=INBOX_ID,
            allowed_jid=None,
            dynamic_recipient_enabled=True,
            template=WITH_FORM_TEMPLATE,
        ),
        allowed_jid=None,
        commercial_ally_config=ALLY,
        portable_recipient_enabled=True,
        pilot_boundary=BOUNDARY,
        clock=lambda: FINAL_NOW,
        final_meta_effect_gate=FinalMetaEffectGate(
            enabled=True, evidence_dir=tmp_path / "meta-effects"
        ),
        waba_template=WITH_FORM_TEMPLATE,
        approved_template_direct=True,
    )

    decisions = _run(dispatcher)

    operations = postgrest.operations()
    assert decisions[-1].decision == "execute"
    assert [name for name in operations if name.startswith("reevaluate_")] == [
        "reevaluate_portable_precheckout_action",
        "reevaluate_portable_precheckout_action",
    ]
    assert [name for name in operations if name.startswith("mark_")] == [
        "mark_portable_precheckout_request_started"
    ]
    assert postgrest.body_of("reserve_followup_delivery_attempt")["p_mode"] == (
        "approved_template"
    )
    # Lo mismo que recorre validate_att1_portable_chain.mjs contra la base
    # para este ancla (caso 9): si una capa cambia de RPC, la otra se entera.
    mode, start_by_anchor, reevaluate_by_anchor = _chain_validator_contract()
    assert postgrest.body_of("reserve_followup_delivery_attempt")["p_mode"] == mode
    assert [name for name in operations if name.startswith("mark_")] == [
        start_by_anchor[FORM_ANCHOR]
    ]
    assert {name for name in operations if name.startswith("reevaluate_")} == {
        reevaluate_by_anchor[FORM_ANCHOR]
    }
    assert FORM_ANCHOR in _chain_validator_anchors()
    assert operations[-1] == "record_and_finalize_followup_acceptance"
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["name"] == FORM_TEMPLATE
