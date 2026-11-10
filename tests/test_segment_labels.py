"""La etiqueta de un segmento antes del primer turno del agente (E-316, PR-C).

Contrato: ``docs/contracts/segment-labels-v1.md``. Diseno: E-310, §2.7 y PR-C.

Datos:

* El despacho nativo usa los dobles de ``test_flow_engine_native_dispatch.py``
  (la base y el inbox 11 de ATT1). El macro del segmento tiene la forma del GET
  real de un macro de ATT1 (``chatwoot_macros_account_2_20261016.json``) con la
  accion ``add_label`` de la etiqueta del segmento.
* La herencia lee ``GET /contacts/767/conversations`` de Maribel tal cual se
  capturo (``chatwoot_contact_conversations_inbox_13_contact_767_20261029.json``,
  el mismo de ``test_first_touch_active_conversation.py``). Lo unico cambiado es
  la etiqueta: la conversacion 6 lleva la del segmento (en la captura no tiene
  ninguna). El webhook es el entrante capturado de la conversacion 536 del inbox
  11 (``chatwoot_message_created_buttons_inbox_11_conv_536_20261028.json``) con
  la conversacion, el inbox y el contacto cambiados a los de Maribel (9, 13 y
  767).
* **Deuda:** no hay captura de un webhook con una etiqueta de campana puesta por
  un macro, ni de la conversacion de un contacto con una etiqueta de segmento:
  la etiqueta ``black-november`` es un ejemplo.

La declaracion de los segmentos es de PR-A (``[[agente.segmentos]]``): mientras
PR-A no este en ``main`` se lee por atributo, y aca va un doble con esos nombres.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from test_first_touch_active_conversation import CONTACT, CONVERSATIONS, INBOX, PREFIX, _MaribelInbox
from test_flow_engine_native_dispatch import (
    MACRO,
    TAIL,
    _dispatcher,
    _NativeAuthority,
    _NativeChatwoot,
    _run,
    _templates,
)
from test_durable_dispatcher_approved_template import ACCOUNT_ID
from test_flow_validator import SEGMENT_INSTANCE, found, run_new, welcome

from bridge.flow_change_operator import OperatorInputError, instance_from_input
from bridge.flow_definition import InstanceView
from bridge.product_label import ProductLabeler
from bridge.segment_labels import (
    ORIGIN_AGENT_CAMPAIGN,
    STATE_ACTIVE,
    STATE_EXPIRED,
    STATE_NOT_STARTED,
    STATE_UNKNOWN,
    NativeSegmentLabels,
    SegmentLabel,
    SegmentLabelInheritance,
    SegmentLabelsConfigError,
    automation_expert_segments,
    declared_segment_labels,
    segment_labels_setup,
    validator_segment_labels,
    webhook_contact_id,
)
from bridge.supabase import FlowEngineNativeStepTail, SupabaseError

FIXTURES = Path(__file__).parent / "fixtures"
WEBHOOKS = json.loads(
    (FIXTURES / "chatwoot_message_created_buttons_inbox_11_conv_536_20261028.json").read_text(encoding="utf-8")
)["webhooks"]
LABEL = "black-november"
SEGMENT_KEY = "black-november"
SEGMENT_MACRO_ID = 41
START = datetime(2026, 11, 20, 6, tzinfo=UTC)
END = datetime(2026, 12, 1, 6, tzinfo=UTC)
INSIDE = datetime(2026, 11, 25, tzinfo=UTC)


def _declared(*, macro: int | None = SEGMENT_MACRO_ID, label: str = LABEL) -> SimpleNamespace:
    """Un ``AgentSegment`` de PR-A (``InstanceManifest.agent_segments``), por sus atributos."""
    return SimpleNamespace(key=SEGMENT_KEY, label=label, knowledge_path="conocimiento/segmentos/bn-v1.toml",
                           priority=10, label_macro_id=macro)


def _catalog(start: datetime = START, end: datetime = END) -> SimpleNamespace:
    """Un ``SegmentCatalog`` de PR-A: los segmentos cargados con su vigencia."""
    knowledge = SimpleNamespace(valid_from=start, valid_until=end)
    return SimpleNamespace(segments=(SimpleNamespace(key=SEGMENT_KEY, knowledge=knowledge),))


def _segment(**changes: Any) -> SegmentLabel:
    return replace(SegmentLabel(key=SEGMENT_KEY, label=LABEL, macro_id=SEGMENT_MACRO_ID,
                                valid_from=START, valid_until=END), **changes)


# ---------------------------------------------------------------- la declaracion


def test_the_declaration_is_read_from_pr_a_with_its_window() -> None:
    manifest = SimpleNamespace(agent_segments=(_declared(),))

    [segment] = declared_segment_labels(manifest, _catalog())

    assert (segment.key, segment.label, segment.macro_id) == (SEGMENT_KEY, LABEL, SEGMENT_MACRO_ID)
    assert (segment.valid_from, segment.valid_until) == (START, END)
    # Sin el catalogo (AGENT_SEGMENTS_ENABLED apagado) no se sabe la vigencia.
    [unknown] = declared_segment_labels(manifest, None)
    assert unknown.state_at(INSIDE) == STATE_UNKNOWN


def test_without_pr_a_there_are_no_segments() -> None:
    # El InstanceManifest de main no tiene agent_segments: nada corre.
    assert declared_segment_labels(SimpleNamespace(products=()), None) == ()
    assert declared_segment_labels(None, None) == ()


@pytest.mark.parametrize("label", ["cliente", "automation_paused", "automation_opted_out", "automation_x"])
def test_a_client_or_automation_label_is_never_a_segment(label: str) -> None:
    with pytest.raises(SegmentLabelsConfigError):
        declared_segment_labels(SimpleNamespace(agent_segments=(_declared(label=label),)), None)


def test_the_window_is_inclusive_from_and_exclusive_until() -> None:
    segment = _segment()
    assert segment.state_at(START - timedelta(seconds=1)) == STATE_NOT_STARTED
    assert segment.state_at(START) == STATE_ACTIVE
    assert segment.state_at(END - timedelta(seconds=1)) == STATE_ACTIVE
    assert segment.state_at(END) == STATE_EXPIRED


def test_the_validator_only_gets_segments_with_a_macro() -> None:
    labels = validator_segment_labels([_segment(), _segment(label="otra", macro_id=None)], now=INSIDE)
    assert labels == [{"etiqueta": LABEL, "segmento": SEGMENT_KEY, "estado": STATE_ACTIVE}]


# ---------------------------------------------------------------- el validador


def test_a_cart_can_label_a_segment_but_not_the_client() -> None:
    cart = welcome(dispara={"evento": "carrito"}, entra={"permiso": "formulario"},
                   pasos=[{"etiquetar": LABEL}, welcome()["pasos"][0]])
    codes = found(run_new(cart, key="carrito_bn", instance=SEGMENT_INSTANCE))
    assert "etiquetar_requires_purchase_trigger" not in codes
    assert "etiquetar_segment_not_before_template" not in codes
    assert "invalid_value" not in codes
    # Sin el segmento en la instancia (el flag apagado), la etiqueta no es del esquema.
    assert "invalid_value" in found(run_new(cart, key="carrito_bn"))
    # Con cliente, sigue siendo solo de la compra.
    client_cart = welcome(dispara={"evento": "carrito"}, entra={"permiso": "formulario"},
                          pasos=[welcome()["pasos"][0], {"etiquetar": "cliente"}])
    assert "etiquetar_requires_purchase_trigger" in found(
        run_new(client_cart, key="carrito_bn", instance=SEGMENT_INSTANCE))


def test_a_segment_label_inside_a_condition_is_not_before_the_send() -> None:
    step = welcome()["pasos"][0]
    definition = welcome(pasos=[{"condicion": {"dato": "ventana", "es": ["abierta"]},
                                 "entonces": [{"etiquetar": LABEL}, step], "si_no": [step]}])
    assert "etiquetar_segment_not_before_template" in found(run_new(definition, instance=SEGMENT_INSTANCE))


def test_the_operator_input_carries_the_segment_labels() -> None:
    base = {"tenant_ref": "lancemos", "ally_ref": "att1", "products": ["att1"], "time_zone": "America/Mexico_City"}
    assert instance_from_input(base).segment_labels == ()
    view = instance_from_input({**base, "segment_labels": [{"etiqueta": LABEL, "estado": "vigente"}]})
    assert view.segment_label_states == {LABEL: "vigente"}
    assert view.labels == ("cliente", LABEL)
    for bad in ([{"etiqueta": "cliente", "estado": "vigente"}], [{"etiqueta": LABEL, "estado": "otro"}],
                [{"etiqueta": LABEL}], "x"):
        with pytest.raises(OperatorInputError):
            instance_from_input({**base, "segment_labels": bad})


def test_an_instance_view_rejects_a_repeated_or_reserved_segment_label() -> None:
    for labels in (((LABEL, "vigente"), (LABEL, "vencido")), (("automation_paused", "vigente"),)):
        with pytest.raises(ValueError, match="instance_segment_labels_invalid"):
            InstanceView("lancemos", "att1", frozenset({"att1"}), None, segment_labels=labels)


# ---------------------------------------------------------------- el motor nativo


class _SegmentAuthority(_NativeAuthority):
    """La accion nativa cuya version fijada lleva ``etiquetar: black-november``."""

    def __init__(self, *, label: str | None = LABEL, tail_fails: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.label = label
        self.tail_fails = tail_fails

    async def get_flow_engine_native_step_tail(self, action_id: str) -> FlowEngineNativeStepTail | None:
        self.engine_calls.append("tail")
        if self.tail_fails:
            raise SupabaseError("flow_engine_native_step_tail_v1_failed: HTTP 503")
        return FlowEngineNativeStepTail(
            flow_id=TAIL["flow_id"], version=TAIL["version"], etiquetar=self.label,
            producto=TAIL["producto"], momento=TAIL["momento"],
        )


class _SegmentChatwoot(_NativeChatwoot):
    """El inbox 11 con el macro del segmento, que solo suma su etiqueta."""

    def __init__(self, *, failures: int = 0) -> None:
        super().__init__()
        self.failures = failures

    def handler(self, request: httpx.Request) -> httpx.Response:
        prefix = f"/api/v1/accounts/{ACCOUNT_ID}"
        path = request.url.path
        if request.method == "GET" and path == f"{prefix}/macros/{SEGMENT_MACRO_ID}":
            self.requests.append(("GET", path, None))
            payload = copy.deepcopy(MACRO["get_macro_4"]["body"]["payload"])
            return httpx.Response(200, json={"payload": {
                **payload, "id": SEGMENT_MACRO_ID, "name": "Black November",
                "actions": [{"action_name": "add_label", "action_params": [LABEL]}],
            }})
        if request.method == "POST" and path == f"{prefix}/macros/{SEGMENT_MACRO_ID}/execute":
            body = json.loads(request.content)
            self.requests.append(("POST", path, body))
            if self.failures > 0:
                self.failures -= 1
                return httpx.Response(500, json={})
            self.labels.append(LABEL)
            return httpx.Response(200, json={})
        return super().handler(request)

    def order(self) -> list[str]:
        out = []
        for method, path, body in self.requests:
            if method == "POST" and path.endswith(f"/macros/{SEGMENT_MACRO_ID}/execute"):
                out.append("segmento")
            elif method == "POST" and path.endswith("/conversations/200/messages") and body \
                    and "template_params" in body:
                out.append("plantilla")
        return out


def _segment_dispatcher(authority: _NativeAuthority, chatwoot: _SegmentChatwoot, tmp_path: Path,
                        *, now: datetime = INSIDE, flag: bool = True) -> Any:
    dispatcher = _dispatcher(authority, chatwoot, tmp_path)
    if flag:
        client = chatwoot.client()
        dispatcher._native_segment_labels = NativeSegmentLabels(
            segments=[_segment()],
            labelers={LABEL: ProductLabeler(client=client, label=LABEL, macro_id=SEGMENT_MACRO_ID)},
            clock=lambda: now,
        )
    return dispatcher


def test_the_segment_label_goes_on_before_the_first_template(tmp_path: Path) -> None:
    authority = _SegmentAuthority()
    chatwoot = _SegmentChatwoot()

    _run(_segment_dispatcher(authority, chatwoot, tmp_path))

    # Antes de la plantilla, en la conversacion de la plantilla; una sola vez.
    assert chatwoot.order() == ["segmento", "plantilla"]
    [execute] = chatwoot.posts(f"/macros/{SEGMENT_MACRO_ID}/execute")
    assert execute == {"conversation_ids": [200]}
    assert chatwoot.labels == [LABEL]
    # La cola de cliente no corre con la etiqueta de un segmento: ni etiqueta cliente ni nota.
    assert chatwoot.notes == []
    assert authority.engine_calls == ["context", "request_started", "tail", "tail"]
    assert authority.acceptances


def test_a_failed_label_does_not_stop_the_template_and_is_tried_again_after(tmp_path: Path) -> None:
    authority = _SegmentAuthority()
    chatwoot = _SegmentChatwoot(failures=1)

    _run(_segment_dispatcher(authority, chatwoot, tmp_path))

    assert chatwoot.order() == ["segmento", "plantilla", "segmento"]
    assert chatwoot.labels == [LABEL]
    assert len(_templates(chatwoot)) == 1


def test_a_segment_out_of_its_window_is_not_put_and_the_template_goes(tmp_path: Path) -> None:
    for now in (START - timedelta(minutes=1), END):
        authority = _SegmentAuthority()
        chatwoot = _SegmentChatwoot()

        _run(_segment_dispatcher(authority, chatwoot, tmp_path / now.strftime("%H%M%S"), now=now))

        assert chatwoot.order() == ["plantilla"]
        assert chatwoot.labels == []


def test_without_an_answer_from_the_tail_the_template_goes_without_the_label(tmp_path: Path) -> None:
    authority = _SegmentAuthority(tail_fails=True)
    chatwoot = _SegmentChatwoot()

    _run(_segment_dispatcher(authority, chatwoot, tmp_path))

    assert chatwoot.order() == ["plantilla"]
    assert len(_templates(chatwoot)) == 1


def test_without_the_flag_the_dispatch_reads_nothing_new(tmp_path: Path) -> None:
    authority = _SegmentAuthority()
    chatwoot = _SegmentChatwoot()

    _run(_segment_dispatcher(authority, chatwoot, tmp_path, flag=False))

    assert authority.engine_calls == ["context", "request_started", "tail"]
    assert chatwoot.order() == ["plantilla"]
    assert chatwoot.labels == []


def test_the_client_tail_is_not_a_segment_plan() -> None:
    labels = NativeSegmentLabels(
        segments=[_segment()],
        labelers={LABEL: ProductLabeler(client=SimpleNamespace(), label=LABEL, macro_id=SEGMENT_MACRO_ID)},
        clock=lambda: INSIDE,
    )
    for label in ("cliente", None, "otra"):
        authority = _SegmentAuthority(label=label)
        assert asyncio.run(labels.plan(authority, action_id="a")) is None


# ---------------------------------------------------------------- el remitente


class _BoomInbox(_MaribelInbox):
    pass


def test_a_before_send_that_raises_does_not_stop_the_template(caplog: pytest.LogCaptureFixture) -> None:
    inbox = _BoomInbox()
    seen: list[int] = []

    async def boom(conversation_id: int) -> None:
        seen.append(conversation_id)
        raise RuntimeError("macro down")

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(inbox.sender().send_first_touch(
            phone="520000000767", buyer_name="Contacto de prueba", buyer_email=None,
            content="Texto de la plantilla aprobada", delivery_id="evt-e316", before_send=boom,
        ))

    assert result.status == "sent"
    assert seen == [result.conversation_id]
    assert "first_touch_before_send_failed" in caplog.text


# ---------------------------------------------------------------- la herencia


def _listing(labels_of_6: list[str]) -> dict[str, object]:
    listing = copy.deepcopy(CONVERSATIONS)
    listing.pop("_fixture")
    for conversation in listing["payload"]:
        if conversation["id"] == 6:
            conversation["labels"] = labels_of_6
    return listing


class _InheritInbox(_MaribelInbox):
    """Maribel, con el macro del segmento y las etiquetas de la conversacion 9."""

    def __init__(self, listing: dict[str, object]) -> None:
        super().__init__(listing)
        self.labels_9: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"{PREFIX}/macros/{SEGMENT_MACRO_ID}":
            self.requests.append((request.method, path))
            return httpx.Response(200, json={"payload": {
                "id": SEGMENT_MACRO_ID, "name": "Black November", "visibility": "global", "account_id": 3,
                "actions": [{"action_name": "add_label", "action_params": [LABEL]}],
            }})
        if request.method == "GET" and path == f"{PREFIX}/conversations/9/labels":
            self.requests.append((request.method, path))
            return httpx.Response(200, json={"payload": list(self.labels_9)})
        if request.method == "POST" and path == f"{PREFIX}/macros/{SEGMENT_MACRO_ID}/execute":
            self.requests.append((request.method, path))
            self.posts.append((path, json.loads(request.content)))
            self.labels_9.append(LABEL)
            return httpx.Response(200, json={})
        return super().handler(request)

    def listings(self) -> int:
        return sum(1 for request in self.requests if request == ("GET", f"{PREFIX}/contacts/{CONTACT}/conversations"))


def _webhook(labels: list[str] | None = None) -> dict[str, Any]:
    payload = copy.deepcopy(WEBHOOKS["4610"])
    payload["conversation"]["id"] = 9
    payload["conversation"]["inbox_id"] = INBOX
    payload["conversation"]["labels"] = labels if labels is not None else []
    payload["conversation"]["meta"]["sender"]["id"] = CONTACT
    payload["sender"]["id"] = CONTACT
    payload["inbox"]["id"] = INBOX
    return payload


def _inheritance(inbox: _InheritInbox, *, now: datetime = INSIDE, macro: bool = True) -> SegmentLabelInheritance:
    client = inbox.client()
    labelers = {LABEL: ProductLabeler(client=client, label=LABEL, macro_id=SEGMENT_MACRO_ID)} if macro else {}
    return SegmentLabelInheritance(client=client, inbox_id=INBOX, segments=[_segment()], labelers=labelers,
                                   clock=lambda: now)


def test_the_captured_webhook_names_the_contact() -> None:
    assert webhook_contact_id(WEBHOOKS["4610"]) == WEBHOOKS["4610"]["conversation"]["meta"]["sender"]["id"]
    broken = copy.deepcopy(WEBHOOKS["4610"])
    broken["sender"]["id"] = 1
    assert webhook_contact_id(broken) is None


def test_a_new_conversation_inherits_the_segment_of_another_one_of_the_contact() -> None:
    inbox = _InheritInbox(_listing([LABEL]))
    inheritance = _inheritance(inbox)
    payload = _webhook()

    turn = asyncio.run(inheritance.apply(payload, conversation_id=9))

    assert turn["conversation"]["labels"] == [LABEL]
    assert payload["conversation"]["labels"] == []  # el payload original no se toca
    assert inbox.posts[-1] == (f"{PREFIX}/macros/{SEGMENT_MACRO_ID}/execute", {"conversation_ids": [9]})
    # El turno siguiente (el webhook todavia sin la etiqueta: el macro corre en un job) no lee de nuevo.
    again = asyncio.run(inheritance.apply(_webhook(), conversation_id=9))
    assert again["conversation"]["labels"] == [LABEL]
    assert inbox.listings() == 1


def test_a_conversation_that_has_a_segment_label_reads_nothing() -> None:
    inbox = _InheritInbox(_listing([LABEL]))
    inheritance = _inheritance(inbox)

    turn = asyncio.run(inheritance.apply(_webhook([LABEL]), conversation_id=9))

    assert turn["conversation"]["labels"] == [LABEL]
    assert inbox.requests == []


def test_without_a_segment_in_the_other_conversations_it_remembers_and_reads_once() -> None:
    inbox = _InheritInbox(_listing([]))
    inheritance = _inheritance(inbox)

    for _ in range(3):
        turn = asyncio.run(inheritance.apply(_webhook(), conversation_id=9))
        assert turn["conversation"]["labels"] == []
    assert inbox.listings() == 1


def test_an_expired_segment_is_not_inherited() -> None:
    inbox = _InheritInbox(_listing([LABEL]))

    turn = asyncio.run(_inheritance(inbox, now=END).apply(_webhook(), conversation_id=9))

    assert turn["conversation"]["labels"] == []
    assert not [post for post in inbox.posts if post[0].endswith("/execute")]


def test_a_failed_listing_is_tried_again_on_the_next_turn() -> None:
    inbox = _InheritInbox(_listing([LABEL]))
    inbox.listing_status = 500
    inheritance = _inheritance(inbox)

    assert asyncio.run(inheritance.apply(_webhook(), conversation_id=9))["conversation"]["labels"] == []
    inbox.listing_status = 200
    assert asyncio.run(inheritance.apply(_webhook(), conversation_id=9))["conversation"]["labels"] == [LABEL]
    assert inbox.listings() == 2


def test_without_the_macro_the_turn_still_gets_the_segment() -> None:
    inbox = _InheritInbox(_listing([LABEL]))

    turn = asyncio.run(_inheritance(inbox, macro=False).apply(_webhook(), conversation_id=9))

    assert turn["conversation"]["labels"] == [LABEL]
    assert not [post for post in inbox.posts if post[0].endswith("/execute")]


# ---------------------------------------------------------------- el armado


def _settings(**changes: Any) -> SimpleNamespace:
    base = {
        "flow_engine_segment_labels_enabled": True, "segment_label_inheritance_enabled": False,
        "flow_engine_enabled": True, "instance_manifest": SimpleNamespace(agent_segments=(_declared(),)),
        "agent_segment_catalog": _catalog(), "agent_segments_enabled": True, "chatwoot_inbox_id": 11,
        "chatwoot_pause_macro_id": 4, "chatwoot_resume_macro_id": 5, "chatwoot_opt_out_macro_id": 6,
        "chatwoot_client_label_macro_id": 31,
    }
    base.update(changes)
    return SimpleNamespace(**base)


def test_the_setup_is_empty_without_the_flags() -> None:
    setup = segment_labels_setup(_settings(flow_engine_segment_labels_enabled=False), None)
    assert setup.native is None and setup.inheritance is None and not setup.validator_enabled
    assert setup.validator_labels(INSIDE) == []


def test_the_setup_builds_the_labelers_with_the_flags() -> None:
    setup = segment_labels_setup(_settings(segment_label_inheritance_enabled=True), SimpleNamespace())
    assert setup.native is not None and setup.native.labels == frozenset({LABEL})
    assert setup.inheritance is not None
    assert setup.validator_labels(INSIDE) == [{"etiqueta": LABEL, "segmento": SEGMENT_KEY, "estado": "vigente"}]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"flow_engine_enabled": False}, "requires FLOW_ENGINE_ENABLED"),
        ({"instance_manifest": SimpleNamespace(products=())}, r"requires \[\[agente.segmentos\]\]"),
        ({"instance_manifest": SimpleNamespace(agent_segments=(_declared(macro=None),))},
         "requires a segment with macro_etiqueta"),
        ({"instance_manifest": SimpleNamespace(agent_segments=(_declared(macro=4),))},
         "cannot be a pause, resume, opt-out or client macro"),
        ({"instance_manifest": SimpleNamespace(agent_segments=(_declared(macro=31),))},
         "cannot be a pause, resume, opt-out or client macro"),
        ({"instance_manifest": None}, "requires an instance manifest"),
        ({"segment_label_inheritance_enabled": True, "agent_segments_enabled": False},
         "requires AGENT_SEGMENTS_ENABLED"),
    ],
)
def test_the_setup_fails_closed(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        segment_labels_setup(_settings(**changes), SimpleNamespace())


def test_the_setup_needs_the_control_client() -> None:
    with pytest.raises(ValueError, match="requires the Chatwoot control client"):
        segment_labels_setup(_settings(), None)


# ---------------------------------------------------------------- lo que ve el AE


def test_the_automation_expert_sees_the_segments_and_who_labels_them() -> None:
    flows = [
        {"flow_key": "carrito_bn", "native": True,
         "definition": {"pasos": [{"etiquetar": LABEL}, {"plantilla": "att1_bn_01"}]}},
        {"flow_key": "bienvenida", "native": True,
         "definition": {"pasos": [{"plantilla": "att1_bienvenida_01"}, {"etiquetar": "cliente"}]}},
        {"flow_key": "carrito", "native": False},
    ]

    out = automation_expert_segments([_segment(), _segment(key="otro", label="otra", macro_id=None)], flows,
                                     now=INSIDE)

    assert out["allowed_labels"] == ["cliente", LABEL]
    first, second = out["segments"]  # type: ignore[misc]
    assert first == {"key": SEGMENT_KEY, "label": LABEL, "labelable": True, "state": "vigente",
                     "valid_from": START.isoformat(), "valid_until": END.isoformat(),
                     "labeled_by": ["carrito_bn"]}
    assert second["labelable"] is False and second["labeled_by"] == []


# ------------------------------------- la fuente viva (E-411 C9)
#
# Las campanas del Automation Expert llegan por el catalogo vivo: la herencia y
# el plan nativo leen la fuente en cada llamada. Las lecturas de Chatwoot son
# las capturadas de siempre (_InheritInbox, el contacto de Maribel); la cola
# del paso nativo es la del doble de test_flow_engine_native_dispatch.


class _LiveSource:
    """Una fuente viva: lo que el catalogo de la base sirve en cada momento."""

    live = True

    def __init__(self) -> None:
        self.items: list[SegmentLabel] = []
        self.labelers: dict[str, Any] = {}

    def segments(self) -> tuple[SegmentLabel, ...]:
        return tuple(self.items)

    def labeler(self, label: str) -> Any:
        return self.labelers.get(label)


def _campaign(**changes: Any) -> SegmentLabel:
    return _segment(origin=ORIGIN_AGENT_CAMPAIGN, **changes)


def test_the_native_plan_sees_a_campaign_that_appears_and_stops_seeing_it_when_it_goes() -> None:
    source = _LiveSource()
    labels = NativeSegmentLabels(source=source, clock=lambda: INSIDE)
    authority = _SegmentAuthority()

    assert asyncio.run(labels.plan(authority, action_id="a")) is None
    source.items = [_campaign()]
    source.labelers = {LABEL: ProductLabeler(client=SimpleNamespace(), label=LABEL, macro_id=SEGMENT_MACRO_ID)}
    plan = asyncio.run(labels.plan(authority, action_id="a"))
    assert plan is not None and plan.segment.origin == ORIGIN_AGENT_CAMPAIGN
    source.items, source.labelers = [], {}
    assert asyncio.run(labels.plan(authority, action_id="a")) is None


def test_the_fixed_source_keeps_the_validation_of_today() -> None:
    with pytest.raises(SegmentLabelsConfigError, match="segment_labelers_invalid"):
        NativeSegmentLabels(segments=[_segment()], labelers={})
    with pytest.raises(SegmentLabelsConfigError, match="segment_inheritance_invalid"):
        SegmentLabelInheritance(client=SimpleNamespace(), inbox_id=INBOX, segments=[], labelers={})


def _live_inheritance(inbox: _InheritInbox, source: _LiveSource, *, now: datetime = INSIDE) -> SegmentLabelInheritance:
    return SegmentLabelInheritance(client=inbox.client(), inbox_id=INBOX, source=source, clock=lambda: now)


def test_with_a_live_source_and_no_segment_in_force_chatwoot_is_not_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")
    inbox = _InheritInbox(_listing([LABEL]))
    source = _LiveSource()
    inheritance = _live_inheritance(inbox, source)

    assert asyncio.run(inheritance.apply(_webhook(), conversation_id=9))["conversation"]["labels"] == []
    assert inbox.listings() == 0
    assert "segment_label_inheritance outcome=sin_segmentos conversation=9" in caplog.text
    # Una campana vencida tampoco abre la lectura.
    source.items = [_campaign(valid_from=START - timedelta(days=30), valid_until=START - timedelta(days=1))]
    asyncio.run(inheritance.apply(_webhook(), conversation_id=9))
    assert inbox.listings() == 0


def test_a_campaign_that_appears_is_inherited_without_restarting() -> None:
    inbox = _InheritInbox(_listing([LABEL]))
    source = _LiveSource()
    inheritance = _live_inheritance(inbox, source)
    asyncio.run(inheritance.apply(_webhook(), conversation_id=9))

    # La conversacion 6 del contacto tuvo actividad antes del 20/11 de START: la
    # campana arranca antes de esa actividad para que se pueda heredar.
    source.items = [_campaign(valid_from=datetime.fromtimestamp(1793155086 - 60, UTC))]
    source.labelers = {LABEL: ProductLabeler(client=inbox.client(), label=LABEL, macro_id=SEGMENT_MACRO_ID)}
    turn = asyncio.run(inheritance.apply(_webhook(), conversation_id=9))

    assert turn["conversation"]["labels"] == [LABEL]
    assert inbox.posts[-1] == (f"{PREFIX}/macros/{SEGMENT_MACRO_ID}/execute", {"conversation_ids": [9]})


def test_a_campaign_of_the_ae_is_not_inherited_from_activity_before_it_started() -> None:
    # D1 de E-411: la conversacion 6 tuvo su ultima actividad (1793155086) antes
    # del desde de esta campana; la misma etiqueta del manifiesto si se hereda.
    late_start = datetime.fromtimestamp(1793155086 + 60, UTC)
    for origin, inherited in ((ORIGIN_AGENT_CAMPAIGN, []), ("manifiesto", [LABEL])):
        inbox = _InheritInbox(_listing([LABEL]))
        source = _LiveSource()
        source.items = [_segment(origin=origin, valid_from=late_start)]
        source.labelers = {LABEL: ProductLabeler(client=inbox.client(), label=LABEL, macro_id=SEGMENT_MACRO_ID)}
        turn = asyncio.run(_live_inheritance(inbox, source).apply(_webhook(), conversation_id=9))
        assert turn["conversation"]["labels"] == inherited, origin
        assert inbox.listings() == 1


def test_the_fixed_source_still_reads_chatwoot_with_every_segment_expired() -> None:
    # Lo de hoy, sin cambios: la fuente fija no corta por vigencia antes de leer.
    inbox = _InheritInbox(_listing([LABEL]))
    client = inbox.client()
    inheritance = SegmentLabelInheritance(
        client=client, inbox_id=INBOX, segments=[_segment()],
        labelers={LABEL: ProductLabeler(client=client, label=LABEL, macro_id=SEGMENT_MACRO_ID)},
        clock=lambda: END + timedelta(days=1),
    )
    asyncio.run(inheritance.apply(_webhook(), conversation_id=9))
    assert inbox.listings() == 1
