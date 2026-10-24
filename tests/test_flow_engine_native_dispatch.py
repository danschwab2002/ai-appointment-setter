"""La bienvenida de un flujo nativo sale y se marca la clienta (E-065, flow-engine-v2).

Contrato: ``docs/contracts/flow-engine-v2.md``, secciones 5, 9 y la 11 (lo del
PR 3). El paso de un flujo nativo disparado por la compra es un primer contacto
anclado a ``flow_engine_event`` con la plantilla de su version fijada
(``nombre`` y ``producto``, sin boton, de utilidad), y despues el paso
``etiquetar: cliente``: la etiqueta por un macro que solo la suma y una nota
privada con el producto y la fecha.

Datos:

* Los dobles de la base y de Chatwoot son los de
  ``test_durable_dispatcher_approved_template.py`` (el inbox 11 de ATT1).
* La cola del paso sale de ``tests/fixtures/flow_engine/native_bridge_rpc_rows.json``
  (la base real, PGlite, ``validate_flow_engine_native_bridge.mjs``).
* **La plantilla de la bienvenida no existe todavia en Meta** (se crea con el
  PR 4 y la carga asistida). Se deriva de la captura del 10/10 del inbox 11
  (``att1_reactivacion_01``, la unica sin boton): mismo sobre, otro nombre
  (``att1_bienvenida_01``), categoria ``UTILITY`` y un cuerpo de dos
  variables. Es lo unico inventado, y dice que.
* La conversacion que lee la nota es la captura
  ``chatwoot_conversation_api_show_conv_158_20260924.json`` con su id y su
  inbox cambiados a los del doble (200 y el 11). El macro de la etiqueta tiene
  la forma de ``chatwoot_macros_account_2_20261016.json`` (el GET real de un
  macro de ATT1) con la accion ``add_label`` de ``cliente``.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_durable_dispatcher_approved_template import (
    ACCOUNT_ID,
    ALLY,
    BOUNDARY,
    CATALOG,
    FINAL_NOW,
    INBOX_ID,
    NOW,
    TEMPLATE,
    _Authority,
    _Chatwoot,
)

from bridge.messaging import ChatwootMessageSender, FinalMetaEffectGate
from bridge.native_flows import (
    NATIVE_ANCHOR,
    NATIVE_NEEDS_DIRECT_TEMPLATE,
    NATIVE_NEEDS_ENGINE,
    client_note,
)
from bridge.product_label import ProductLabeler
from bridge.supabase import FlowEngineNativeStepTail, FlowEngineStepContext
from bridge.worker import FLOW_ENGINE_STEP_UNSUPPORTED, DurableDispatcher

FIXTURES = Path(__file__).parent / "fixtures"
ROWS = json.loads((FIXTURES / "flow_engine" / "native_bridge_rpc_rows.json").read_text())
TAIL = ROWS["step_tail"]
FLOW_ID = TAIL["flow_id"]
WELCOME = "att1_bienvenida_01"
LABEL_MACRO_ID = 31

_REACTIVATION = [
    template
    for template in json.loads(
        (FIXTURES / "chatwoot_inbox_11_message_templates_20261010.json").read_text(encoding="utf-8")
    )["message_templates"]
    if template["name"] == "att1_reactivacion_01"
][0]
WELCOME_BODY = "¡Hola {{1}}! Gracias por tu compra de {{2}}. Ya tienes tu acceso."
WELCOME_TEMPLATE = {
    **copy.deepcopy(_REACTIVATION),
    "name": WELCOME,
    "category": "UTILITY",
    "components": [
        {"type": "BODY", "text": WELCOME_BODY, "example": {"body_text": [["Edith", "Alimenta tu Tiroides"]]}}
    ],
}
SHOW = json.loads((FIXTURES / "chatwoot_conversation_api_show_conv_158_20260924.json").read_text(encoding="utf-8"))
SHOW_200 = {**copy.deepcopy(SHOW["conversation"]), "id": 200, "inbox_id": INBOX_ID}
MACRO = json.loads((FIXTURES / "chatwoot_macros_account_2_20261016.json").read_text(encoding="utf-8"))


def _step(**changes: Any) -> FlowEngineStepContext:
    return replace(
        FlowEngineStepContext(
            flow_id=FLOW_ID,
            version=1,
            product_ref=ALLY.funnel_ref,
            flow_key="activacion",
            step_key="first_contact",
            send_ordinal=1,
            template_name=WELCOME,
            template_language="es_MX",
            template_variables=("nombre", "producto"),
            template_button=None,
            legacy_dispatch_ok=False,
        ),
        **changes,
    )


class _NativeAuthority(_Authority):
    """La accion de la bienvenida: politica flujo:<flow_id> y ancla del evento."""

    def __init__(self, *, step: FlowEngineStepContext | None = None, native: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if native:
            self.action = replace(
                self.action,
                policy_key=f"flujo:{FLOW_ID}",
                anchor_type=NATIVE_ANCHOR,
            )
        self.step = step if step is not None else _step()
        self.engine_calls: list[str] = []
        self.handoffs: list[object] = []

    async def get_flow_engine_step_context(self, action_id: str) -> FlowEngineStepContext | None:
        self.engine_calls.append("context")
        return self.step

    async def mark_flow_engine_request_started(self, **_: object) -> Any:
        self.engine_calls.append("request_started")
        return replace(self.attempt, phase="request_started")

    async def get_flow_engine_native_step_tail(self, action_id: str) -> FlowEngineNativeStepTail | None:
        self.engine_calls.append("tail")
        if not self.action.policy_key.startswith("flujo:") or self.action.anchor_type != NATIVE_ANCHOR:
            return None
        return FlowEngineNativeStepTail(
            flow_id=TAIL["flow_id"], version=TAIL["version"], etiquetar=TAIL["etiquetar"],
            producto=TAIL["producto"], momento=TAIL["momento"],
        )

    async def request_human_handoff(self, **kwargs: object) -> object:
        self.handoffs.append(kwargs)
        raise AssertionError("una accion nativa nunca deriva")


class _NativeChatwoot(_Chatwoot):
    """El inbox 11 con la bienvenida, la conversacion 200, sus etiquetas y el macro."""

    def __init__(self) -> None:
        super().__init__([*CATALOG, WELCOME_TEMPLATE])
        self.labels: list[str] = []
        self.notes: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        prefix = f"/api/v1/accounts/{ACCOUNT_ID}"
        path = request.url.path
        if request.method == "GET" and path == f"{prefix}/conversations/200":
            self.requests.append(("GET", path, None))
            return httpx.Response(200, json=SHOW_200)
        if request.method == "GET" and path == f"{prefix}/conversations/200/messages":
            self.requests.append(("GET", path, None))
            return httpx.Response(200, json={"payload": [
                {"id": 888, "content": "plantilla", "message_type": 1, "private": False},
                *self.notes,
            ]})
        if request.method == "GET" and path == f"{prefix}/conversations/200/labels":
            self.requests.append(("GET", path, None))
            return httpx.Response(200, json={"payload": list(self.labels)})
        if request.method == "GET" and path == f"{prefix}/macros/{LABEL_MACRO_ID}":
            self.requests.append(("GET", path, None))
            payload = copy.deepcopy(MACRO["get_macro_4"]["body"]["payload"])
            return httpx.Response(200, json={"payload": {
                **payload,
                "id": LABEL_MACRO_ID,
                "name": "Etiqueta cliente",
                "actions": [{"action_name": "add_label", "action_params": ["cliente"]}],
            }})
        if request.method == "POST" and path == f"{prefix}/macros/{LABEL_MACRO_ID}/execute":
            body = json.loads(request.content)
            self.requests.append(("POST", path, body))
            self.labels.append("cliente")
            return httpx.Response(200, json={})
        if request.method == "POST" and path == f"{prefix}/conversations/200/messages":
            body = json.loads(request.content)
            if body.get("private") is True:
                self.requests.append(("POST", path, body))
                note = {"id": 900 + len(self.notes), "conversation_id": 200, "message_type": 1,
                        "private": True, "content": body["content"]}
                self.notes.append(note)
                return httpx.Response(200, json=note)
        return super().handler(request)


class _Hermes:
    """Un agente que no se tiene que llamar nunca para una accion nativa."""

    def __init__(self) -> None:
        self.calls = 0

    async def request_followup_message(self, **_: object) -> Any:
        self.calls += 1
        raise AssertionError("una accion nativa nunca va a Hermes")


def _dispatcher(
    authority: _NativeAuthority,
    chatwoot: _NativeChatwoot,
    tmp_path: Path,
    *,
    engine: bool = True,
    labeler: bool = True,
    direct: bool = True,
    hermes: _Hermes | None = None,
) -> DurableDispatcher:
    client = chatwoot.client()
    return DurableDispatcher(
        supabase=authority,  # type: ignore[arg-type]
        worker_id="att1-dispatcher",
        chatwoot=client,
        chatwoot_account_id=ACCOUNT_ID,
        chatwoot_inbox_id=INBOX_ID,
        recovery_agent=hermes,  # type: ignore[arg-type]
        sender=ChatwootMessageSender(
            chatwoot=client, inbox_id=INBOX_ID, allowed_jid=None, dynamic_recipient_enabled=True,
            template=TEMPLATE,
        ),
        allowed_jid=None,
        commercial_ally_config=ALLY,
        portable_recipient_enabled=True,
        pilot_boundary=BOUNDARY,
        clock=lambda: FINAL_NOW,
        final_meta_effect_gate=FinalMetaEffectGate(enabled=True, evidence_dir=tmp_path / "meta-effects"),
        waba_template=TEMPLATE,
        approved_template_direct=direct,
        flow_engine_enabled=engine,
        native_client_labeler=(
            ProductLabeler(client=client, label="cliente", macro_id=LABEL_MACRO_ID) if labeler else None
        ),
    )


def _run(dispatcher: DurableDispatcher) -> None:
    asyncio.run(dispatcher.dispatch_due(now=NOW))


def _templates(chatwoot: _NativeChatwoot) -> list[dict[str, Any]]:
    return [post for post in chatwoot.posts("/conversations/200/messages") if "template_params" in post]


def test_the_welcome_goes_out_with_its_pinned_utility_template_and_marks_the_client(tmp_path: Path) -> None:
    authority = _NativeAuthority()
    chatwoot = _NativeChatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    assert authority.engine_calls == ["context", "request_started", "tail"]
    assert "request_started" not in authority.events  # el arranque es el del motor
    [message] = _templates(chatwoot)
    params = message["template_params"]
    assert params["name"] == WELCOME
    assert params["category"] == "UTILITY"
    assert params["language"] == "es_MX"
    assert message["content"] == WELCOME_BODY.replace("{{1}}", "Edith García Pérez").replace(
        "{{2}}", "Alimenta tu Tiroides")
    assert authority.acceptances
    # etiquetar: el macro que solo suma cliente, y una nota con producto y fecha.
    assert chatwoot.labels == ["cliente"]
    [note] = chatwoot.notes
    assert note["content"].startswith(client_note(producto=TAIL["producto"], momento=TAIL["momento"]))
    assert "Alimenta tu Tiroides" in note["content"] and TAIL["momento"][:10] in note["content"]
    assert note["content"].endswith("[supportmagician-flujo:cliente:action-att1]")
    # Sin datos de la persona en la nota.
    assert "12025550124" not in note["content"] and "@" not in note["content"]


def test_a_retried_tail_writes_no_second_note_and_no_second_label(tmp_path: Path) -> None:
    authority = _NativeAuthority()
    chatwoot = _NativeChatwoot()
    _run(_dispatcher(authority, chatwoot, tmp_path))
    from bridge.native_flows import apply_native_tail

    client = chatwoot.client()
    again = asyncio.run(apply_native_tail(
        authority,  # type: ignore[arg-type]
        action_id="action-att1", conversation_id=200, inbox_id=INBOX_ID,
        labeler=ProductLabeler(client=client, label="cliente", macro_id=LABEL_MACRO_ID), notes=client,
    ))
    assert again == "hecho"
    assert len(chatwoot.notes) == 1
    assert chatwoot.labels == ["cliente"]


def test_without_the_label_macro_the_step_leaves_only_the_note(tmp_path: Path) -> None:
    authority = _NativeAuthority()
    chatwoot = _NativeChatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path, labeler=False))

    assert len(_templates(chatwoot)) == 1
    assert chatwoot.labels == []
    assert len(chatwoot.notes) == 1
    assert not chatwoot.posts(f"/macros/{LABEL_MACRO_ID}/execute")


def test_without_the_engine_flag_a_native_action_closes_and_never_sends_the_manifest_template(
    tmp_path: Path,
) -> None:
    authority = _NativeAuthority()
    chatwoot = _NativeChatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path, engine=False))

    assert authority.engine_calls == []
    assert _templates(chatwoot) == [] and chatwoot.posts("/conversations") == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == NATIVE_NEEDS_ENGINE
    assert closed["next_attempt_at"] is None


def test_a_native_action_never_goes_to_hermes_so_it_never_asks_for_a_handoff(tmp_path: Path) -> None:
    # El ⚠ de E-060: request_human_handoff exige el binding del piloto y un caso
    # nativo no lo tiene (handoff_pilot_scope_mismatch). El unico camino que la
    # pide es una sugerencia de Hermes en el modo sin plantilla directa: una
    # accion nativa cierra antes.
    authority = _NativeAuthority()
    chatwoot = _NativeChatwoot()
    hermes = _Hermes()

    _run(_dispatcher(authority, chatwoot, tmp_path, direct=False, hermes=hermes))

    assert hermes.calls == 0
    assert authority.handoffs == []
    assert _templates(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == NATIVE_NEEDS_DIRECT_TEMPLATE
    assert closed["next_attempt_at"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"template_button": {"link": "checkout", "cupon": "personal"}},
        {"template_variables": ("nombre", "precio")},
        {"template_variables": ()},
    ],
    ids=["con boton", "otra variable", "sin variables"],
)
def test_a_native_step_the_bridge_cannot_build_closes_for_good(tmp_path: Path, changes: dict[str, Any]) -> None:
    authority = _NativeAuthority(step=_step(**changes))
    chatwoot = _NativeChatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    assert _templates(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == FLOW_ENGINE_STEP_UNSUPPORTED
    assert closed["next_attempt_at"] is None


def test_a_marketing_template_is_not_the_welcome(tmp_path: Path) -> None:
    # La regla 17: con permiso compra, una sola plantilla de utilidad.
    authority = _NativeAuthority(step=_step(template_name="att1_reactivacion_01", template_variables=("nombre",)))
    chatwoot = _NativeChatwoot()
    chatwoot.catalog.append(copy.deepcopy(_REACTIVATION))

    _run(_dispatcher(authority, chatwoot, tmp_path))

    assert _templates(chatwoot) == []
    assert authority.finalizations and authority.finalizations[0]["reason_code"] != "eligible_for_execution"


def test_a_cart_action_with_the_flag_on_never_reads_the_native_tail(tmp_path: Path) -> None:
    # Lo que factura: una accion del legado con el flag y el macro hace lo de hoy.
    authority = _NativeAuthority(native=False)
    chatwoot = _NativeChatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    assert "tail" not in authority.engine_calls
    assert chatwoot.labels == [] and chatwoot.notes == []
    [message] = _templates(chatwoot)
    assert message["template_params"]["name"] == TEMPLATE.first_touch_name
