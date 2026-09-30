"""El dispatcher manda la plantilla aprobada sin pedirle borrador a Hermes (A4).

Contrato: docs/contracts/approved-template-direct-dispatch-v1.md.

Datos:

* ``tests/fixtures/instances/att1/instancia.toml``: el binding, las tres ofertas y
  el inbox 11 de ATT1.
* ``chatwoot_followup_candidates_inbox_9_20260928.json``: el catalogo de
  plantillas del inbox 9 capturado el 28/09 (``johanna_carrito_abandonado_01`` y
  ``johanna_compra_fallida_01``, cada una con ``{{1}}``, ``{{2}}`` y tres botones
  QUICK_REPLY). No hay captura del catalogo del inbox 11 de ATT1 (paso A0), asi
  que el dispatcher de estos tests manda las plantillas de Johanna con su idioma
  (``es_EC``). El catalogo se sirve bajo el id del inbox 11, el unico campo del
  sobre que el cliente chequea, igual que ``test_followup_discount.py`` envuelve
  la misma lista.
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


def _captured_body(name: str) -> str:
    [template] = [t for t in CATALOG if t["name"] == name]
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
        product_name: str = "Alimenta Tu Tiroides",
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
                "contact_inboxes": [{"source_id": "source-55", "inbox": {"id": INBOX_ID}}],
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
        CART_TEMPLATE, first="Edith García Pérez", product="Alimenta Tu Tiroides"
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
            "body": {"1": "Edith García Pérez", "2": "Alimenta Tu Tiroides"}
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
        product="Alimenta Tu Tiroides",
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
        CART_TEMPLATE, first="Edith García Pérez", product="Alimenta Tu Tiroides"
    )
    [contact] = chatwoot.posts("/contacts")
    [message] = chatwoot.posts("/conversations/200/messages")
    assert contact["name"] == raw_name
    assert message["template_params"]["processed_params"]["body"] == {
        "1": "Edith García Pérez",
        "2": "Alimenta Tu Tiroides",
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

    expected = _expected(CART_TEMPLATE, first=greeting, product="Alimenta Tu Tiroides")
    [contact] = chatwoot.posts("/contacts")
    [message] = chatwoot.posts("/conversations/200/messages")
    # El contacto de Chatwoot conserva el nombre completo; la variable y el
    # texto guardado llevan el saludo.
    assert contact["name"] == full_name
    assert message["template_params"]["processed_params"]["body"] == {
        "1": greeting,
        "2": "Alimenta Tu Tiroides",
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
        product="Alimenta Tu Tiroides",
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
        "2": "Alimenta Tu Tiroides",
    }
    assert message["content"] == _expected(
        CART_TEMPLATE, first=case["deterministic"], product="Alimenta Tu Tiroides"
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


def _chain_validator_contract() -> tuple[str, str, str]:
    """The reservation mode and the two start RPCs A7 declares."""
    source = CHAIN_VALIDATOR.read_text(encoding="utf-8")
    [mode] = re.findall(r"const DIRECT_DELIVERY_MODE = '([a-z_]+)';", source)
    [(payment_failure, other)] = re.findall(
        r"const startOperationFor = \(anchorType\) => \(anchorType === 'payment_failure'"
        r"\s*\?\s*'([a-z_]+)'\s*:\s*'([a-z_]+)'\);",
        source,
    )
    return mode, payment_failure, other


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

    mode, payment_failure_start, cart_start = _chain_validator_contract()
    expected_start = payment_failure_start if anchor_type == "payment_failure" else cart_start
    starts = [
        operation for operation in postgrest.operations() if operation.startswith("mark_")
    ]
    assert decisions[-1].decision == "execute"
    assert postgrest.body_of("reserve_followup_delivery_attempt")["p_mode"] == mode
    assert starts == [expected_start]
    assert "finalize_followup_delivery_attempt" not in postgrest.operations()
    assert postgrest.operations()[-1] == "record_and_finalize_followup_acceptance"
    # A7 recorre exactamente estos dos anchor_type, los que escriben los
    # planificadores (20260803000100 y 20260903000300).
    assert anchor_type in _chain_validator_anchors()
    assert _chain_validator_anchors() == {"cart_abandonment", "payment_failure"}
    assert chatwoot.posts("/conversations/200/messages")


def test_the_att1_chain_validator_still_declares_its_contract() -> None:
    # Si A7 deja de declarar el modo o la regla de arranque, el test de arriba
    # no tiene contra que comparar: se rompe aca, con un mensaje claro.
    assert _chain_validator_contract() == (
        "approved_template",
        "mark_portable_payment_failure_request_started",
        "mark_lancemos_pilot_request_started",
    )
