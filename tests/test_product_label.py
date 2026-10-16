"""La etiqueta de producto en Chatwoot (varios productos por aliada, paso 2a).

Fixtures:
- tests/fixtures/chatwoot_macros_account_2_20261016.json: el GET real de un macro
  de la cuenta de ATT1, el 404 de uno que no existe y los macros de la cuenta.
- tests/fixtures/chatwoot_message_created_inbox_9_conv_158_20260923.json: el
  webhook real de un mensaje entrante (conversacion 158).
- tests/fixtures/instances/att1: el manifiesto y el conocimiento de prueba de ATT1.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging
import shutil
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.app import Settings, create_app
from bridge.chatwoot import ChatwootClient, ChatwootProtocolError
from bridge.commercial_knowledge import CommercialKnowledge
from bridge.instance_cli import validate_instance
from bridge.instance_manifest import (
    MAX_PRODUCTS,
    RESERVED_CHATWOOT_LABELS,
    InstanceManifest,
    ManifestError,
    Product,
)
from bridge.messaging import ChatwootMessageSender
from bridge.product_label import ProductLabeler

FIXTURES = Path(__file__).parent / "fixtures"
ATT1 = FIXTURES / "instances" / "att1"
MACROS = json.loads(
    (FIXTURES / "chatwoot_macros_account_2_20261016.json").read_text(encoding="utf-8")
)
LABEL_MACRO_ID = 7
LABEL_MACRO = {
    "payload": {
        "id": LABEL_MACRO_ID,
        "name": "Etiqueta ATT1",
        "visibility": "global",
        "account_id": 2,
        "actions": [{"action_name": "add_label", "action_params": ["att1"]}],
    }
}


def _run(coroutine):
    return asyncio.run(coroutine)


# ----------------------------------------------------------------- manifiesto


def _manifest_payload() -> dict:
    return tomllib.loads((ATT1 / "instancia.toml").read_text(encoding="utf-8"))


def _with_products(*products: dict) -> dict:
    payload = _manifest_payload()
    payload["productos"] = [dict(product) for product in products]
    return payload


def test_without_productos_the_manifest_declares_no_products() -> None:
    manifest = InstanceManifest.from_mapping(_manifest_payload())

    assert manifest.products == ()
    assert manifest.labeled_product is None
    assert manifest.knowledge_products_error((None,)) is None
    assert manifest.knowledge_products_error(("att1", "att2")) is None


def test_a_product_with_its_label_and_macro() -> None:
    manifest = InstanceManifest.from_mapping(
        _with_products({"clave": "att1", "etiqueta": "att1", "macro_etiqueta": 7})
    )

    assert manifest.products == (
        Product(key="att1", chatwoot_label="att1", chatwoot_label_macro_id=7),
    )
    assert manifest.labeled_product == manifest.products[0]


def test_a_product_without_a_label_labels_nothing() -> None:
    manifest = InstanceManifest.from_mapping(_with_products({"clave": "att1"}))

    assert manifest.products == (Product(key="att1"),)
    assert manifest.labeled_product is None


@pytest.mark.parametrize(
    ("productos", "message"),
    [
        ([{"clave": "att1", "etiqueta": "att1"}], "van juntas"),
        ([{"clave": "att1", "macro_etiqueta": 7}], "van juntas"),
        (
            [{"clave": "att1", "etiqueta": "automation_paused", "macro_etiqueta": 7}],
            "etiqueta de la automatizacion",
        ),
        (
            [{"clave": "att1", "etiqueta": "automation_opted_out", "macro_etiqueta": 7}],
            "etiqueta de la automatizacion",
        ),
        ([{"clave": "att1", "etiqueta": "ATT1", "macro_etiqueta": 7}], "minusculas"),
        ([{"clave": "att1", "etiqueta": "att 1", "macro_etiqueta": 7}], "minusculas"),
        ([{"clave": "att1", "etiqueta": "att1", "macro_etiqueta": 0}], "entero positivo"),
        ([{"clave": "att1", "etiqueta": "att1", "macro_etiqueta": "7"}], "entero positivo"),
        ([{"clave": "ATT1"}], "slug"),
        ([{"clave": "att1", "precio": "47"}], "claves no soportadas precio"),
        # Desde el segundo, cada producto trae su funnel y su Hotmart (2c).
        (
            [{"clave": "att1"}, {"clave": "att2"}],
            "faltan binding_version, funnel_ref, hotmart",
        ),
        (
            [{"clave": "att1", "funnel_ref": "att1"}],
            "es el producto principal",
        ),
        ([], "lista de tablas"),
    ],
)
def test_invalid_productos_do_not_load(productos: list, message: str) -> None:
    payload = _manifest_payload()
    payload["productos"] = productos

    with pytest.raises(ManifestError, match=message):
        InstanceManifest.from_mapping(payload)


def test_productos_must_be_a_list_of_tables() -> None:
    payload = _manifest_payload()
    payload["productos"] = {"clave": "att1"}

    with pytest.raises(ManifestError, match="lista de tablas"):
        InstanceManifest.from_mapping(payload)


def test_the_reserved_labels_are_the_ones_the_bridge_writes() -> None:
    # Las dos que ponen los macros de pausa y de baja de la cuenta de ATT1.
    written = {
        label
        for macro in MACROS["macros_account_2"]
        for action in macro["actions"]
        if action["action_name"] == "add_label"
        for label in action["action_params"]
    }
    assert set(RESERVED_CHATWOOT_LABELS) == written
    # Varios productos desde la 2c: el principal y hasta siete mas.
    assert MAX_PRODUCTS == 8


def _by_product(*keys: str) -> CommercialKnowledge:
    payload = tomllib.loads(
        (ATT1 / "conocimiento" / "knowledge-v1.toml").read_text(encoding="utf-8")
    )
    oferta = payload.pop("oferta")
    contenido = payload.pop("contenido")
    payload["productos"] = []
    for index, product_ref in enumerate(keys):
        entry_offer = copy.deepcopy(oferta)
        if index:
            entry_offer["nombre"] = f"{entry_offer['nombre']} {index + 1}"
        payload["productos"].append(
            {"clave": product_ref, "oferta": entry_offer, "contenido": copy.deepcopy(contenido)}
        )
    return CommercialKnowledge.from_mapping(payload, require_approved=False)


def _keys(knowledge: CommercialKnowledge) -> tuple[str | None, ...]:
    return tuple(product.key for product in knowledge.products)


def test_the_knowledge_must_separate_the_products_of_the_manifest() -> None:
    manifest = InstanceManifest.from_mapping(
        _with_products({"clave": "att1", "etiqueta": "att1", "macro_etiqueta": 7})
    )
    single = CommercialKnowledge.from_toml_file(
        ATT1 / "conocimiento" / "knowledge-v1.toml", require_approved=False
    )

    assert manifest.knowledge_products_error(_keys(_by_product("att1"))) is None
    assert "no tiene secciones por producto" in (
        manifest.knowledge_products_error(_keys(single)) or ""
    )
    assert "(att1) no son los del conocimiento (att2)" in (
        manifest.knowledge_products_error(_keys(_by_product("att2"))) or ""
    )
    assert "(att1) no son los del conocimiento (att1, att2)" in (
        manifest.knowledge_products_error(_keys(_by_product("att1", "att2"))) or ""
    )


# ------------------------------------------------------------------- validate


@pytest.fixture
def att1_copy(tmp_path: Path) -> Path:
    target = tmp_path / "setter-instancia-att1"
    shutil.copytree(ATT1, target)
    return target


def _append_products(instance: Path, text: str) -> None:
    path = instance / "instancia.toml"
    path.write_text(path.read_text(encoding="utf-8") + "\n" + text, encoding="utf-8")


def test_validate_without_productos_reports_as_before() -> None:
    report = validate_instance(ATT1)

    assert report["valida"] is True
    assert "productos" not in report["manifiesto"]


def test_validate_lists_the_products_and_refuses_a_knowledge_that_does_not_separate_them(
    att1_copy: Path,
) -> None:
    _append_products(
        att1_copy,
        '[[productos]]\nclave = "att1"\netiqueta = "att1"\nmacro_etiqueta = 7\n',
    )

    report = validate_instance(att1_copy)

    assert report["manifiesto"]["productos"] == [
        {"clave": "att1", "etiqueta": "att1", "macro_etiqueta": 7}
    ]
    assert report["valida"] is False
    assert any(
        "no tiene secciones por producto: el bridge no arranca" in error
        for error in report["errores"]
    )


# ------------------------------------------------------- el cliente de Chatwoot


class _Chatwoot:
    """Chatwoot falso de la cuenta 2: macros, etiquetas y ejecuciones."""

    def __init__(
        self,
        *,
        macro: dict | None = None,
        labels: list[str] | None = None,
        labels_status: int = 200,
    ) -> None:
        self.macro = LABEL_MACRO if macro is None else macro
        self.labels = [] if labels is None else list(labels)
        self.labels_status = labels_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == f"/api/v1/accounts/2/macros/{LABEL_MACRO_ID}":
            if self.macro == "missing":
                return httpx.Response(
                    MACROS["get_missing_macro"]["status"],
                    content=MACROS["get_missing_macro"]["body"],
                )
            return httpx.Response(200, json=self.macro)
        if path.startswith("/api/v1/accounts/2/conversations/") and path.endswith("/labels"):
            if self.labels_status != 200:
                return httpx.Response(self.labels_status, json={})
            return httpx.Response(200, json={"payload": list(self.labels)})
        if path == f"/api/v1/accounts/2/macros/{LABEL_MACRO_ID}/execute":
            # Chatwoot suma la etiqueta en un job: aca, al toque.
            self.labels.append("att1")
            return httpx.Response(200)
        return httpx.Response(404, json={"error": f"unexpected {path}"})

    def paths(self, method: str | None = None) -> list[str]:
        return [
            request.url.path
            for request in self.requests
            if method is None or request.method == method
        ]


def _client(transport: _Chatwoot) -> ChatwootClient:
    return ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=2,
        access_token="control-token",
        allowed_jid=None,
        transport=httpx.MockTransport(transport),
    )


def test_get_macro_actions_reads_the_shape_chatwoot_returns() -> None:
    transport = _Chatwoot(macro=MACROS["get_macro_4"]["body"])

    actions = _run(_client(transport).get_macro_actions(macro_id=LABEL_MACRO_ID))

    assert actions == [{"action_name": "add_label", "action_params": ["automation_paused"]}]
    assert transport.requests[0].headers["api_access_token"] == "control-token"


def test_get_macro_actions_of_a_missing_macro_raises_404() -> None:
    with pytest.raises(httpx.HTTPStatusError) as raised:
        _run(_client(_Chatwoot(macro="missing")).get_macro_actions(macro_id=LABEL_MACRO_ID))

    assert raised.value.response.status_code == 404


def test_get_macro_actions_refuses_a_body_without_actions() -> None:
    with pytest.raises(ChatwootProtocolError, match="invalid_macro_payload"):
        _run(
            _client(_Chatwoot(macro={"payload": {"id": 7}})).get_macro_actions(
                macro_id=LABEL_MACRO_ID
            )
        )


def test_execute_macro_posts_the_conversation() -> None:
    transport = _Chatwoot()

    _run(_client(transport).execute_macro(macro_id=LABEL_MACRO_ID, conversation_id=158))

    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/v1/accounts/2/macros/7/execute"
    assert json.loads(request.content) == {"conversation_ids": [158]}


# ------------------------------------------------------------- la etiqueta


def _labeler(transport: _Chatwoot, **kwargs: object) -> ProductLabeler:
    return ProductLabeler(
        client=_client(transport), label="att1", macro_id=LABEL_MACRO_ID, **kwargs
    )


def test_the_label_is_added_by_the_macro_and_then_remembered() -> None:
    transport = _Chatwoot(labels=["automation_paused"])
    labeler = _labeler(transport)
    assert labeler.state == "unverified"

    assert _run(labeler.ensure(158, source="entrante")) == "added"
    assert labeler.state == "ready"
    assert _run(labeler.ensure(158, source="entrante")) == "present"
    assert _run(labeler.ensure(158, source="entrante")) == "present"

    # El macro se lee una vez, se ejecuta una vez, y nunca se escribe el
    # endpoint de etiquetas: la pausa queda donde estaba.
    assert transport.paths() == [
        "/api/v1/accounts/2/macros/7",
        "/api/v1/accounts/2/conversations/158/labels",
        "/api/v1/accounts/2/macros/7/execute",
        "/api/v1/accounts/2/conversations/158/labels",
    ]
    assert transport.paths("POST") == ["/api/v1/accounts/2/macros/7/execute"]
    assert transport.labels == ["automation_paused", "att1"]


def test_a_conversation_that_has_the_label_is_not_touched() -> None:
    transport = _Chatwoot(labels=["att1"])

    assert _run(_labeler(transport).ensure(158, source="plantilla")) == "present"
    assert transport.paths("POST") == []


@pytest.mark.parametrize(
    "macro",
    [
        # El id del macro de pausa cargado por error: pausaria cada conversacion.
        MACROS["get_macro_4"]["body"],
        {"payload": {"actions": [
            {"action_name": "add_label", "action_params": ["att1"]},
            {"action_name": "resolve_conversation", "action_params": []},
        ]}},
        {"payload": {"actions": [
            {"action_name": "add_label", "action_params": ["att1", "automation_paused"]},
        ]}},
        {"payload": {"actions": [{"action_name": "add_label", "action_params": ["att2"]}]}},
    ],
)
def test_a_macro_that_does_more_than_add_the_label_is_never_executed(
    macro: dict, caplog: pytest.LogCaptureFixture
) -> None:
    transport = _Chatwoot(macro=macro)
    labeler = _labeler(transport)
    caplog.set_level(logging.ERROR, logger="bridge.product_label")

    assert _run(labeler.ensure(158, source="entrante")) == "disabled"
    assert _run(labeler.ensure(159, source="plantilla")) == "disabled"

    assert labeler.state == "disabled:macro_does_more_than_add_the_label"
    assert transport.paths() == ["/api/v1/accounts/2/macros/7"]
    assert any("product_label_macro_invalid" in message for message in caplog.messages)


def test_a_missing_macro_turns_the_label_off() -> None:
    transport = _Chatwoot(macro="missing")
    labeler = _labeler(transport)

    assert _run(labeler.ensure(158, source="entrante")) == "disabled"
    assert labeler.state == "disabled:macro_not_found"
    assert transport.paths("POST") == []


def test_a_chatwoot_failure_does_not_raise_and_is_retried(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = _Chatwoot(labels_status=500)
    labeler = _labeler(transport)
    caplog.set_level(logging.WARNING, logger="bridge.product_label")

    assert _run(labeler.ensure(158, source="entrante")) == "failed"
    assert any(
        "product_label_failed source=entrante conversation=158 reason=http_500" in message
        for message in caplog.messages
    )

    transport.labels_status = 200
    assert _run(labeler.ensure(158, source="entrante")) == "added"


def test_an_invalid_conversation_id_fails_without_calling_chatwoot() -> None:
    transport = _Chatwoot()
    labeler = _labeler(transport)

    for conversation_id in (0, -1, True):
        assert _run(labeler.ensure(conversation_id, source="entrante")) == "failed"
    assert transport.requests == []


def test_the_remembered_conversations_are_bounded() -> None:
    transport = _Chatwoot(labels=["att1"])
    labeler = _labeler(transport, remembered=1)

    _run(labeler.ensure(158, source="entrante"))
    transport.requests.clear()
    _run(labeler.ensure(158, source="entrante"))
    assert transport.requests == []

    # La 159 desplaza a la 158, que se vuelve a leer.
    _run(labeler.ensure(159, source="entrante"))
    _run(labeler.ensure(158, source="entrante"))
    assert transport.paths() == [
        "/api/v1/accounts/2/conversations/159/labels",
        "/api/v1/accounts/2/conversations/158/labels",
    ]


# --------------------------------------------- la plantilla abre la conversacion


class _SenderChatwoot:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def find_contact_inbox_by_phone(self, **_: object) -> tuple[int, str]:
        return (31, "5215550100999")

    async def create_conversation(self, **_: object) -> int:
        return 501

    async def send_first_message(self, **_: object) -> dict[str, object]:
        if self.fail:
            raise ChatwootProtocolError("template_rejected")
        return {"message_id": 9001}

    async def send_followup_message(self, **_: object) -> dict[str, object]:
        if self.fail:
            raise ChatwootProtocolError("template_rejected")
        return {"message_id": 9002}


def _sender(chatwoot: _SenderChatwoot, hook) -> ChatwootMessageSender:
    from bridge.messaging import WhatsAppTemplateConfig

    return ChatwootMessageSender(
        chatwoot=chatwoot,  # type: ignore[arg-type]
        inbox_id=11,
        allowed_jid="5215550100999@s.whatsapp.net",
        template=WhatsAppTemplateConfig(
            first_touch_name="att1_carrito_abandonado_01",
            followup_name=None,
            language="es_MX",
            category="MARKETING",
            first_touch_parameter="buyer_name",
        ),
        on_conversation_opened=hook,
    )


def _first_touch(sender: ChatwootMessageSender):
    return _run(
        sender.send_first_touch(
            phone="5215550100999",
            buyer_name="Ana Prueba",
            buyer_email=None,
            content="Hola",
            delivery_id="attempt-2a",
        )
    )


def test_a_sent_first_touch_labels_the_conversation_it_opened() -> None:
    opened: list[int] = []

    async def hook(conversation_id: int) -> None:
        opened.append(conversation_id)

    result = _first_touch(_sender(_SenderChatwoot(), hook))

    assert (result.status, result.conversation_id) == ("sent", 501)
    assert opened == [501]


def test_a_first_touch_in_an_existing_conversation_labels_it_too() -> None:
    opened: list[int] = []

    async def hook(conversation_id: int) -> None:
        opened.append(conversation_id)

    result = _run(
        _sender(_SenderChatwoot(), hook).send_first_touch_to_conversation(
            conversation_id=777,
            phone="5215550100999",
            buyer_name="Ana Prueba",
            content="Hola",
            delivery_id="attempt-2a",
        )
    )

    assert result.status == "sent"
    assert opened == [777]


def test_a_failed_first_touch_labels_nothing() -> None:
    opened: list[int] = []

    async def hook(conversation_id: int) -> None:
        opened.append(conversation_id)

    result = _first_touch(_sender(_SenderChatwoot(fail=True), hook))

    assert result.status == "failed"
    assert opened == []


def test_a_hook_that_raises_does_not_undo_the_send(caplog: pytest.LogCaptureFixture) -> None:
    async def hook(conversation_id: int) -> None:
        raise RuntimeError("boom")

    caplog.set_level(logging.WARNING, logger="bridge.messaging")
    result = _first_touch(_sender(_SenderChatwoot(), hook))

    assert result.status == "sent"
    assert any("conversation_opened_hook_failed conversation=501" in m for m in caplog.messages)


# -------------------------------------------------------------- el bridge


def _labeled_manifest(macro_id: int = LABEL_MACRO_ID) -> InstanceManifest:
    return replace(
        InstanceManifest.from_toml_file(ATT1 / "instancia.toml"),
        products=(
            Product(key="att1", chatwoot_label="att1", chatwoot_label_macro_id=macro_id),
        ),
    )


def _bridge_settings(manifest: InstanceManifest, tmp_path: Path, **overrides: object) -> Settings:
    settings = Settings(
        webhook_secret="webhook-secret",
        allowed_jid="573000000158@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
        agent_bot_id=1,
        chatwoot_account_id=2,
        chatwoot_inbox_id=9,
        instance_manifest=manifest,
        hermes_model_name=manifest.agent_model_name,
    )
    return replace(settings, **overrides)


@pytest.mark.parametrize("macro_field", [
    "chatwoot_pause_macro_id",
    "chatwoot_resume_macro_id",
    "chatwoot_opt_out_macro_id",
])
def test_the_label_macro_cannot_be_an_automation_macro(
    macro_field: str, tmp_path: Path
) -> None:
    settings = _bridge_settings(
        _labeled_manifest(), tmp_path, **{macro_field: LABEL_MACRO_ID}
    )

    with pytest.raises(ValueError, match="cannot be a pause, resume or opt-out macro"):
        create_app(settings, chatwoot_client=_client(_Chatwoot()))


def test_the_label_requires_chatwoot_control(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires Chatwoot control"):
        create_app(_bridge_settings(_labeled_manifest(), tmp_path))


def test_the_bridge_does_not_start_with_a_knowledge_that_does_not_separate_the_products(
    tmp_path: Path,
) -> None:
    single = CommercialKnowledge.from_toml_file(
        ATT1 / "conocimiento" / "knowledge-v1.toml", require_approved=False
    )
    settings = _bridge_settings(_labeled_manifest(), tmp_path, commercial_knowledge=single)

    with pytest.raises(ValueError, match="instance products: .*no tiene secciones"):
        create_app(settings, chatwoot_client=_client(_Chatwoot()))

    two_layers = _by_product("att1")
    assert create_app(
        _bridge_settings(_labeled_manifest(), tmp_path, commercial_knowledge=two_layers),
        chatwoot_client=_client(_Chatwoot()),
    ) is not None


def _conversation_158() -> dict:
    return json.loads(
        (FIXTURES / "chatwoot_message_created_inbox_9_conv_158_20260923.json").read_text(
            encoding="utf-8"
        )
    )


class _Bridge158(_Chatwoot):
    """La cuenta 2 con la conversacion 158 del fixture adentro."""

    def __init__(self, fixture: dict) -> None:
        super().__init__(labels=["automation_paused"])
        self.fixture = fixture

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/accounts/2/conversations/158":
            self.requests.append(request)
            return httpx.Response(200, json=self.fixture["conversation"])
        if path == "/api/v1/accounts/2/conversations/158/messages":
            self.requests.append(request)
            if request.url.params.get("before") is not None:
                return httpx.Response(200, json={"meta": {}, "payload": []})
            return httpx.Response(200, json={"meta": {}, "payload": self.fixture["messages"]})
        return super().__call__(request)


class _AdmittingSupabase:
    def __init__(self, transport: _Chatwoot) -> None:
        self.transport = transport
        self.label_requests_at_admission: int | None = None

    async def admit_inbound_commercial_case(self, **_: object) -> object:
        self.label_requests_at_admission = len(self.transport.paths("POST"))
        return SimpleNamespace(
            outcome="created",
            commercial_case_id="case-158",
            contact_id="contact-158",
            channel_identity_id="identity-158",
            conversation_id="conversation-158",
            automation_status="draft_only",
        )

    # Con manifiesto, la admision va por la RPC portable.
    admit_portable_inbound_commercial_case = admit_inbound_commercial_case


def _post_158(app, webhook: dict) -> httpx.Response:
    # La cuenta del fixture es la 1 (Johanna); el bridge de la prueba es el de
    # la cuenta 2. Se cambia solo la cuenta: el resto del webhook es el real.
    webhook = copy.deepcopy(webhook)
    webhook["account"]["id"] = 2
    if isinstance(webhook.get("conversation"), dict):
        webhook["conversation"]["account_id"] = 2
    raw_body = json.dumps(webhook, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = "sha256=" + hmac.new(
        b"webhook-secret", timestamp.encode("ascii") + b"." + raw_body, hashlib.sha256
    ).hexdigest()

    async def exercise() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/webhooks/chatwoot",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Chatwoot-Signature": signature,
                    "X-Chatwoot-Timestamp": timestamp,
                    "X-Chatwoot-Delivery": "conv-158-msg-2233",
                },
            )
        await app.state.chatwoot_worker.run_once()
        return response

    return asyncio.run(exercise())


def test_an_inbound_message_labels_its_conversation_before_the_admission(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fixture = _conversation_158()
    transport = _Bridge158(fixture)
    supabase = _AdmittingSupabase(transport)
    app = create_app(
        _bridge_settings(
            _labeled_manifest(),
            tmp_path,
            chatwoot_cut_b_admission_enabled=True,
            chatwoot_cut_b_scope_key="att1-inbound",
            chatwoot_cut_b_scope_version=1,
        ),
        chatwoot_client=_client(transport),
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    caplog.set_level(logging.INFO)

    response = _post_158(app, fixture["webhook"])

    assert response.status_code == 202
    assert [m for m in caplog.messages if "chatwoot_work_failed" in m] == []
    assert transport.paths("POST") == ["/api/v1/accounts/2/macros/7/execute"]
    assert supabase.label_requests_at_admission == 1
    # La pausa sigue: el macro solo sumo la etiqueta del producto.
    assert transport.labels == ["automation_paused", "att1"]
    assert any(
        "product_label outcome=added source=entrante conversation=158 label=att1" in m
        for m in caplog.messages
    )


def test_without_a_labeled_product_an_inbound_message_reads_no_macro(
    tmp_path: Path,
) -> None:
    fixture = _conversation_158()
    transport = _Bridge158(fixture)
    app = create_app(
        _bridge_settings(
            InstanceManifest.from_toml_file(ATT1 / "instancia.toml"),
            tmp_path,
            chatwoot_cut_b_admission_enabled=True,
            chatwoot_cut_b_scope_key="att1-inbound",
            chatwoot_cut_b_scope_version=1,
        ),
        chatwoot_client=_client(transport),
        supabase_client=_AdmittingSupabase(transport),  # type: ignore[arg-type]
    )

    assert _post_158(app, fixture["webhook"]).status_code == 202
    assert not any("/macros/" in path for path in transport.paths())
    assert not any(path.endswith("/labels") for path in transport.paths())


def test_ready_publishes_the_label_state_without_failing(tmp_path: Path) -> None:
    app = create_app(
        _bridge_settings(_labeled_manifest(), tmp_path),
        chatwoot_client=_client(_Chatwoot()),
    )

    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["product_label"] == "unverified"

    without = create_app(
        _bridge_settings(InstanceManifest.from_toml_file(ATT1 / "instancia.toml"), tmp_path),
        chatwoot_client=_client(_Chatwoot()),
    )
    with TestClient(without) as client:
        assert "product_label" not in client.get("/ready").json()
