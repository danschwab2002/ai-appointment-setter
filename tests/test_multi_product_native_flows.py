"""Los flujos nativos de un producto que no es el principal (varios productos, E-350).

Con mas de un producto:
- el carrito y el pago rechazado de un producto que no es el principal van a sus
  flujos nativos, solo con un flujo prendido o en cohorte de ese producto;
- lo que ningun flujo nativo reclama queda ``ignored`` con su motivo: nunca al
  legado ni al motor v1, que son del principal;
- el despacho autoriza al destinatario de un recorrido de cualquier producto de la
  instancia, y la plantilla lleva la etiqueta de su producto.
Con uno solo, nada de esto se mira.

Fixtures: el segundo producto y sus eventos, los de ``test_product_routing`` (los
reales de ATT1 con otro producto y otra oferta, nombrados como derivados). La cadena
real (Postgres, PostgREST y el bridge) la prueba el arnes del encargo E-350.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_flow_engine_native_cart import CONFIG, EVENT_ID, _answer, _NativeBase, _routes as _flow_routes, _row, _runtime
from test_product_routing import ATT1_TOML, _event, _manifest, _routes
from test_product_routing import _payload as _routing_payload
from test_product_routing import _Statuses as _BaseStatuses
from test_flow_engine_native_payment_failure import _refusal
from test_multi_product_single_layer_knowledge import FY_HOTLINK, FY_ID, FY_LABEL, FY_OFFER, FY_OTHER
from test_multi_product_single_layer_knowledge import MARIBEL as MARIBEL_DIR
from test_multi_product_single_layer_knowledge import PRODUCTS as MARIBEL_PRODUCTS

import bridge.messaging as messaging_module
from bridge import worker as worker_module
from bridge.flow_engine_runtime import READ_FAILING, READ_OK, READ_PENDING
from bridge.followup_discount import OTHER_PRODUCT_CONVERSATION, evaluate_followup_candidate
from bridge.hotmart import EVENT_CART_ABANDONMENT, EVENT_PURCHASE_CANCELED
from bridge.instance_manifest import InstanceManifest
from bridge.product_routes import ProductRoutes, followup_product_labels
from bridge.resolution import ResolutionError, resolve_event
from bridge.supabase import FollowupExecutionContext
from bridge.worker import ResolutionWorker

_spec = importlib.util.spec_from_file_location(
    "provision_instance_product", Path(__file__).resolve().parents[1] / "scripts" / "provision_instance_product.py"
)
assert _spec and _spec.loader
_provision = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_provision)
build_sql = _provision.build_sql

SECOND_ID, SECOND_OFFER = 8437428, "prueba2a"


def _second_cart() -> dict:
    return _event("carrito_2uafw5bg", product_id=SECOND_ID, offer=SECOND_OFFER)


class _Flows:
    state = READ_OK
    flows = ()

    def __init__(self, on: bool, trigger: str = "carrito") -> None:
        self.on = on
        self.trigger = trigger

    def any_on(self) -> bool:
        return self.on

    def wants(self, product_ref: str, trigger: str) -> bool:
        return self.on and product_ref == "att2" and trigger == self.trigger


class _Statuses(_BaseStatuses):
    async def enqueue_flow_engine_native_event(self, *args: object, **kwargs: object) -> None:
        return None


def _worker(native_on: bool, trigger: str = "carrito", **kwargs: Any) -> tuple[ResolutionWorker, _Statuses]:
    supabase = _Statuses()
    manifest = _manifest()
    worker = ResolutionWorker(
        supabase=supabase,  # type: ignore[arg-type]
        product_routes=_routes(manifest),
        commercial_ally_config=manifest.to_commercial_ally_config(),
        flow_engine=SimpleNamespace(native_flows=_Flows(native_on, trigger)),  # type: ignore[arg-type]
        policy_key="p",
        policy_version=1,
        pilot_boundary=SimpleNamespace(),  # type: ignore[arg-type]
        **kwargs,
    )
    return worker, supabase


def test_a_cart_of_a_second_product_with_its_native_flow_goes_native_only(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    async def resolve(**kwargs: Any) -> object:
        captured.append(kwargs)
        return SimpleNamespace(event_id=kwargs["webhook_event_id"])

    monkeypatch.setattr(worker_module, "resolve_event", resolve)
    worker, supabase = _worker(native_on=True)
    second = _second_cart()

    asyncio.run(worker._process_one({"id": "e1", "payload": second, "event_type": second["event"]}))

    assert supabase.updates == []
    [call] = captured
    assert call["native_only"] is True
    assert call["commercial_ally_config"].funnel_ref == "att2"
    assert call["native_cart_product_ref"] == "att2"


def test_without_its_native_flow_the_second_product_is_ignored_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[object] = []

    async def resolve(**kwargs: object) -> object:
        called.append(kwargs)
        return SimpleNamespace(event_id=kwargs["webhook_event_id"])

    monkeypatch.setattr(worker_module, "resolve_event", resolve)
    worker, supabase = _worker(native_on=False)
    second = _second_cart()

    asyncio.run(worker._process_one({"id": "e1", "payload": second, "event_type": second["event"]}))

    assert called == []
    assert supabase.updates == [{"event_id": "e1", "status": "ignored", "error": "product_without_recovery_flows"}]


def test_the_cart_of_the_principal_never_goes_native_only(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    async def resolve(**kwargs: Any) -> object:
        captured.append(kwargs)
        return SimpleNamespace(event_id=kwargs["webhook_event_id"])

    monkeypatch.setattr(worker_module, "resolve_event", resolve)
    worker, _ = _worker(native_on=True)
    first = _event("carrito_2uafw5bg")

    asyncio.run(worker._process_one({"id": "e1", "payload": first, "event_type": first["event"]}))

    [call] = captured
    assert "native_only" not in call  # principal
    assert call["commercial_ally_config"].funnel_ref == "att1"


# -- El resolver: native_only no cae al legado ------------------------------------


def _resolve_native_only(base: _NativeBase) -> Any:
    from test_flow_engine_resolution import POLICY_KEY
    from test_resolution_pilot_plan_rejection import BOUNDARY, _cart

    client = base.client()
    return asyncio.run(resolve_event(
        webhook_event_id=EVENT_ID, payload=_cart(CONFIG.offer_code), supabase=client, policy_key=POLICY_KEY,
        policy_version=3, allowed_jid=None, chatwoot_account_id=CONFIG.chatwoot_account_id,
        chatwoot_inbox_id=CONFIG.chatwoot_inbox_id, pilot_boundary=BOUNDARY, commercial_ally_config=CONFIG,
        event_type=EVENT_CART_ABANDONMENT, flow_engine=_runtime(base, client), native_cart_product_ref=CONFIG.funnel_ref,
        native_only=True,
    ))


@pytest.mark.parametrize(
    ("row", "error"),
    [
        ({**_row("sin_flujo"), "flow_id": None}, "product_without_recovery_flows"),
        (_row("motor_no_arranca", "fuera_de_cohorte"), "product_native_not_claimed:fuera_de_cohorte"),
    ],
    ids=["sin flujo", "fuera de la cohorte"],
)
def test_what_no_native_flow_takes_ends_ignored_without_the_legacy(row: dict[str, Any], error: str) -> None:
    base = _NativeBase([_answer(row)], routes=_flow_routes("legado"))

    with pytest.raises(ResolutionError):
        _resolve_native_only(base)

    assert not any(name.startswith("plan_") for name in base.names())
    assert "admit_flow_engine_hotmart_v1" not in base.names()
    assert [(patch["processing_status"], patch.get("processing_error")) for patch in base.status_patches] == [
        ("ignored", error)
    ]


def test_a_started_native_run_owns_the_event_like_with_one_product() -> None:
    base = _NativeBase([_answer(_row("motor_arranco"))], routes=_flow_routes("legado"))

    _resolve_native_only(base)

    assert not any(name.startswith("plan_") for name in base.names())
    assert [patch["processing_status"] for patch in base.status_patches] == ["processed"]


# -- El despacho: el destinatario y la etiqueta --------------------------------------


def _context(product: str, offer: str) -> FollowupExecutionContext:
    return FollowupExecutionContext(
        action_id="a", action_type="first_contact_review", step_key="first_contact", recovery_case_id="r",
        contact_id="c", source_event_id="s", buyer_name="Ana", buyer_email=None, buyer_phone="5215550100999",
        product_name=product, offer_code=offer, current_goal=None, lead_stage="new",
    )


def test_the_recipient_of_any_product_of_the_instance_is_authorized() -> None:
    manifest = _manifest()
    routes = _routes(manifest)
    principal = manifest.to_commercial_ally_config()
    second = routes.by_key("att2").config  # type: ignore[union-attr]

    allowed = worker_module._is_authorized_followup_recipient(
        execution_context=_context(second.product_name, second.offer_code), allowed_jid=None,
        commercial_ally_config=principal, portable_recipient_enabled=True, product_routes=routes,
    )
    without_routes = worker_module._is_authorized_followup_recipient(
        execution_context=_context(second.product_name, second.offer_code), allowed_jid=None,
        commercial_ally_config=principal, portable_recipient_enabled=True,
    )
    foreign = worker_module._is_authorized_followup_recipient(
        execution_context=_context("Otro producto", second.offer_code), allowed_jid=None,
        commercial_ally_config=principal, portable_recipient_enabled=True, product_routes=routes,
    )

    assert allowed is True
    assert without_routes is False
    assert foreign is False


def test_by_product_name_needs_exactly_one_owner() -> None:
    routes = _routes()
    assert routes.by_product_name("Producto de prueba ATT2").key == "att2"  # type: ignore[union-attr]
    assert routes.by_product_name("Otro") is None
    assert routes.by_product_name(None) is None


def test_with_two_products_the_conversation_gets_the_label_of_the_template() -> None:
    calls: list[tuple[int, str | None]] = []

    async def opened(conversation_id: int, product_name: str | None) -> None:
        calls.append((conversation_id, product_name))

    async def single(conversation_id: int) -> None:
        raise AssertionError("con varios productos no se usa el hook de un solo producto")

    sender = messaging_module.ChatwootMessageSender(
        chatwoot=SimpleNamespace(),  # type: ignore[arg-type]
        inbox_id=11, allowed_jid=None, dynamic_recipient_enabled=True,
        on_conversation_opened=single, on_product_conversation_opened=opened,
    )

    asyncio.run(sender._conversation_opened(7, product_name="Producto de prueba ATT2"))

    assert calls == [(7, "Producto de prueba ATT2")]


def test_with_one_product_the_hook_of_always_is_called() -> None:
    calls: list[int] = []

    async def single(conversation_id: int) -> None:
        calls.append(conversation_id)

    sender = messaging_module.ChatwootMessageSender(
        chatwoot=SimpleNamespace(),  # type: ignore[arg-type]
        inbox_id=11, allowed_jid=None, dynamic_recipient_enabled=True, on_conversation_opened=single,
    )

    asyncio.run(sender._conversation_opened(7, product_name="Alimenta tu Tiroides"))

    assert calls == [7]


# -- La siembra del segundo producto ---------------------------------------------------


def test_the_provision_sql_is_the_binding_the_bridge_expects_and_only_the_second() -> None:
    manifest = _manifest()
    second = manifest.to_commercial_ally_configs()[1]

    sql = build_sql(manifest, "quien-prueba")

    assert "commercial_ally_runtime_bindings" in sql and "checkout_offer_catalog" in sql
    assert "hotmart_purchase_intent_scopes" in sql and "commercial_ally_hotmart_purchase_policies" in sql
    assert "'lancemos', 'att1'" not in sql  # el principal lo siembra aprovisionar.sql
    assert f"'{second.funnel_ref}'" in sql and second.product_hotlink in sql
    assert "on conflict do nothing" in sql and "provision_instance_product: el binding" in sql
    assert "quien-prueba" in sql
    assert sql.count("insert into public.checkout_offer_catalog") == 1 + len(second.additional_offer_codes)


def test_the_provision_sql_refuses_an_instance_without_a_second_product() -> None:
    single = InstanceManifest.from_toml_file(ATT1_TOML)
    with pytest.raises(SystemExit):
        build_sql(single, "x")


# -- El pago rechazado de B (E-350, C2) -----------------------------------------------


def _second_refusal() -> dict:
    payload = _refusal()
    payload["data"]["product"] = {**payload["data"]["product"], "id": SECOND_ID}
    payload["data"]["purchase"]["offer"] = {"code": SECOND_OFFER}
    return payload


def test_a_payment_failure_of_a_second_product_goes_to_its_native_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    async def resolve(**kwargs: Any) -> object:
        captured.append(kwargs)
        return SimpleNamespace(event_id=kwargs["webhook_event_id"])

    monkeypatch.setattr(worker_module, "resolve_event", resolve)
    worker, supabase = _worker(native_on=True, trigger="pago_fallido", payment_failure_enabled=True)
    refusal = _second_refusal()

    asyncio.run(worker._process_one({"id": "e1", "payload": refusal, "event_type": refusal["event"]}))

    assert supabase.updates == []
    [call] = captured
    assert call["native_only"] is True and call["commercial_ally_config"].funnel_ref == "att2"
    assert call["native_payment_failure_product_ref"] == "att2"
    assert call["event_type"] == EVENT_PURCHASE_CANCELED


def _resolve_refusal_native_only(base: _NativeBase) -> Any:
    from test_flow_engine_resolution import POLICY_KEY
    from test_resolution_pilot_plan_rejection import BOUNDARY

    client = base.client()
    return asyncio.run(resolve_event(
        webhook_event_id=EVENT_ID, payload=_refusal(), supabase=client, policy_key=POLICY_KEY, policy_version=1,
        allowed_jid=None, chatwoot_account_id=CONFIG.chatwoot_account_id, chatwoot_inbox_id=CONFIG.chatwoot_inbox_id,
        pilot_boundary=BOUNDARY, commercial_ally_config=CONFIG, event_type=EVENT_PURCHASE_CANCELED,
        flow_engine=_runtime(base, client), native_payment_failure_product_ref=CONFIG.funnel_ref, native_only=True,
    ))


def test_the_resolver_starts_the_native_payment_failure_of_a_second_product() -> None:
    base = _NativeBase([_answer(_row("motor_arranco"))], routes=_flow_routes("legado"))

    _resolve_refusal_native_only(base)

    assert not any(name.startswith("plan_") for name in base.names())
    assert base.body_of("admit_flow_engine_event_v1")["p_flow_event"]["tipo"] == "pago_fallido"
    assert [patch["processing_status"] for patch in base.status_patches] == ["processed"]


def test_a_refusal_no_native_flow_takes_never_goes_to_the_legacy() -> None:
    base = _NativeBase([_answer({**_row("sin_flujo"), "flow_id": None})], routes=_flow_routes("legado"))

    with pytest.raises(ResolutionError):
        _resolve_refusal_native_only(base)

    assert not any(name.startswith("plan_") for name in base.names())
    assert [patch["processing_status"] for patch in base.status_patches] == ["ignored"]


# -- Lo que no es «no aplica» no queda ignored (E-350, M1) ------------------------------


@pytest.mark.parametrize("reason", ["forzado_al_legado", "flow_store_product_not_bound"])
def test_a_rejection_or_a_forced_legacy_of_a_second_product_stays_failed(reason: str) -> None:
    from bridge.native_flows import NATIVE_CART_UNCLAIMED, NativeCartAdmission

    async def admit(*_: object, **__: object) -> NativeCartAdmission:
        return NativeCartAdmission(NATIVE_CART_UNCLAIMED, reason=reason)

    base = _NativeBase([], routes=_flow_routes("legado"))
    import bridge.native_flows as native_module

    original = native_module.admit_native_cart
    native_module.admit_native_cart = admit  # type: ignore[assignment]
    try:
        with pytest.raises(ResolutionError):
            _resolve_native_only(base)
    finally:
        native_module.admit_native_cart = original

    assert [(patch["processing_status"], patch.get("processing_error")) for patch in base.status_patches] == [
        ("failed", f"product_native_unavailable:{reason}")
    ]
    assert not any(name.startswith("plan_") for name in base.names())


@pytest.mark.parametrize("state", [READ_PENDING, READ_FAILING])
def test_with_the_flows_never_read_the_event_waits(state: str, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[object] = []

    async def resolve(**kwargs: object) -> object:
        called.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(worker_module, "resolve_event", resolve)
    worker, supabase = _worker(native_on=False)
    worker._flow_engine.native_flows.state = state  # type: ignore[union-attr]
    second = _second_cart()

    asyncio.run(worker._process_one({"id": "e1", "payload": second, "event_type": second["event"]}))

    assert called == [] and supabase.updates == []  # received: lo retoma el lote siguiente


# -- El silencio del principal no le escribe a otro producto (E-350, C1) ------------------


def _silent_conversation(labels: list[str]) -> dict[str, Any]:
    return {"id": 7, "inbox_id": 11, "status": "open", "labels": labels, "meta": {"sender": {"phone_number": "+5215550100999"}}}


@pytest.mark.parametrize(
    ("labels", "skipped"),
    [
        (["segundo"], True),
        (["segundo", "principal"], False),
        (["principal"], False),
        ([], False),
    ],
    ids=["solo de B", "de los dos", "del principal", "sin etiqueta"],
)
def test_the_followup_skips_a_conversation_of_only_another_product(labels: list[str], skipped: bool) -> None:
    decision = evaluate_followup_candidate(
        _silent_conversation(labels), [], expected_inbox_id=11, now_epoch=1_800_000_000,
        other_product_labels=frozenset({"segundo"}), own_product_label="principal",
    )

    if skipped:
        assert decision.skip_reason == OTHER_PRODUCT_CONVERSATION
    else:
        assert decision.skip_reason != OTHER_PRODUCT_CONVERSATION


def test_without_the_labels_the_followup_is_as_before() -> None:
    decision = evaluate_followup_candidate(
        _silent_conversation(["segundo"]), [], expected_inbox_id=11, now_epoch=1_800_000_000,
    )

    assert decision.skip_reason != OTHER_PRODUCT_CONVERSATION


def test_the_followup_labels_come_from_the_manifest_only_with_two_products() -> None:
    manifest = InstanceManifest.from_mapping(_labeled_payload())

    assert followup_product_labels(manifest) == (frozenset({"att2"}), "att1")
    assert followup_product_labels(InstanceManifest.from_toml_file(ATT1_TOML)) == (frozenset(), None)
    assert followup_product_labels(None) == (frozenset(), None)


def _labeled_payload() -> dict[str, Any]:
    payload = _routing_payload()
    payload["productos"][0].update(etiqueta="att1", macro_etiqueta=7)
    payload["productos"][1].update(etiqueta="att2", macro_etiqueta=8)
    return payload


# -- El alambrado por create_app y el envio real (E-350, M3) ------------------------------


def test_the_sender_passes_the_product_of_the_template_to_the_label_hook() -> None:
    calls: list[tuple[int, str | None]] = []

    async def opened(conversation_id: int, product_name: str | None) -> None:
        calls.append((conversation_id, product_name))

    class _Chatwoot:
        async def find_contact_inbox_by_phone(self, **_: object) -> Any:
            return (3, "5215550100999")

        async def list_contact_conversations(self, **_: object) -> list[object]:
            return []

        async def create_conversation(self, **_: object) -> int:
            return 42

        async def send_first_message(self, **_: object) -> dict[str, object]:
            return {"status": "sent", "message_id": 5}

    sender = messaging_module.ChatwootMessageSender(
        chatwoot=_Chatwoot(),  # type: ignore[arg-type]
        inbox_id=11, allowed_jid=None, dynamic_recipient_enabled=True, on_product_conversation_opened=opened,
    )

    result = asyncio.run(sender.send_first_touch(
        phone="5215550100999", buyer_name="Ana", buyer_email=None, product_name="Producto Dos",
        content="Hola", delivery_id="d1",
    ))

    assert result.status == "sent"
    assert calls == [(42, "Producto Dos")]


def test_provision_refuses_one_hotmart_product_split_by_offer() -> None:
    payload = _routing_payload()
    for field in ("product_id", "hotlink", "product_name"):
        payload["productos"][1]["hotmart"][field] = payload["hotmart"][field]
    manifest = InstanceManifest.from_mapping(payload)

    with pytest.raises(SystemExit, match="mismo producto de Hotmart"):
        build_sql(manifest, "x")


# -- La forma de Maribel: el v9 de una capa y forever-young (E-563, MP-06) -----------------
#
# El carril A de Forever Young con la forma real de Maribel: el manifiesto vivo y el v9 firmado
# (tests/fixtures/instances/maribel-fbad25e-20261113, ver su ORIGEN.txt) mas [[productos]] con el
# principal (maribel, la clave de sus flujos publicados) y forever-young, SINTETICO (ID, hotlink,
# ofertas y landings inventados). Los eventos: la compra capturada de Maribel (29/10) y el carrito
# capturado de ATT1, con el producto y la oferta cambiados (derivados, como los de arriba).

_FIXTURES = Path(__file__).parent / "fixtures"


def _maribel_two(*, principal_label: bool = False) -> InstanceManifest:
    text = (MARIBEL_DIR / "instancia.toml").read_text(encoding="utf-8") + MARIBEL_PRODUCTS
    if principal_label:
        text = text.replace(
            'clave = "maribel"\n', 'clave = "maribel"\netiqueta = "el-protocolo"\nmacro_etiqueta = 9900\n', 1
        )
    return InstanceManifest.from_mapping(tomllib.loads(text))


def _maribel_purchase(**changes: object) -> dict:
    payload = json.loads(
        (_FIXTURES / "hotmart_purchase_approved_maribel_20261029.json").read_text(encoding="utf-8")
    )["payload"]
    if "product_id" in changes:
        payload["data"]["product"]["id"] = changes["product_id"]
        payload["data"]["purchase"]["offer"]["code"] = changes["offer"]
        payload["id"] = f"{payload['id']}-derivado"
    return payload


class _MaribelFlows(_Flows):
    def wants(self, product_ref: str, trigger: str) -> bool:
        return self.on and product_ref == "forever-young" and trigger == self.trigger


def _maribel_worker(native_on: bool, trigger: str = "carrito") -> tuple[ResolutionWorker, _Statuses]:
    supabase = _Statuses()
    manifest = _maribel_two()
    config = manifest.to_commercial_ally_config()
    worker = ResolutionWorker(
        supabase=supabase,  # type: ignore[arg-type]
        product_routes=ProductRoutes.build(config, manifest),
        commercial_ally_config=config,
        flow_engine=SimpleNamespace(native_flows=_MaribelFlows(native_on, trigger)),  # type: ignore[arg-type]
        policy_key="p",
        policy_version=1,
        pilot_boundary=SimpleNamespace(),  # type: ignore[arg-type]
    )
    return worker, supabase


def test_maribel_the_captured_purchase_goes_to_the_principal_and_forever_young_to_its_route() -> None:
    manifest = _maribel_two()
    routes = ProductRoutes.build(manifest.to_commercial_ally_config(), manifest)

    assert routes.for_hotmart(_maribel_purchase()) is routes.principal
    young = routes.for_hotmart(_maribel_purchase(product_id=FY_ID, offer=FY_OFFER))
    assert young is not None and young.principal is False
    assert young.config.funnel_ref == "forever-young"
    assert young.config.product_hotlink == FY_HOTLINK
    # Un producto que Maribel no declara sigue sin ruta.
    unrouted = routes.for_hotmart(_maribel_purchase(product_id=FY_ID + 1, offer=FY_OFFER))
    assert getattr(unrouted, "reason", None) == "product_not_declared"


def test_maribel_a_cart_of_forever_young_with_its_native_flow_goes_native_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    async def resolve(**kwargs: Any) -> object:
        captured.append(kwargs)
        return SimpleNamespace(event_id=kwargs["webhook_event_id"])

    monkeypatch.setattr(worker_module, "resolve_event", resolve)
    worker, supabase = _maribel_worker(native_on=True)
    cart = _event("carrito_2uafw5bg", product_id=FY_ID, offer=FY_OFFER)

    asyncio.run(worker._process_one({"id": "e1", "payload": cart, "event_type": cart["event"]}))

    assert supabase.updates == []
    [call] = captured
    assert call["native_only"] is True
    assert call["commercial_ally_config"].funnel_ref == "forever-young"
    assert call["native_cart_product_ref"] == "forever-young"


def test_maribel_without_its_native_flow_forever_young_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[object] = []

    async def resolve(**kwargs: object) -> object:
        called.append(kwargs)
        return SimpleNamespace(event_id=kwargs["webhook_event_id"])

    monkeypatch.setattr(worker_module, "resolve_event", resolve)
    worker, supabase = _maribel_worker(native_on=False)
    cart = _event("carrito_2uafw5bg", product_id=FY_ID, offer=FY_OTHER)

    asyncio.run(worker._process_one({"id": "e1", "payload": cart, "event_type": cart["event"]}))

    assert called == []
    assert supabase.updates == [{"event_id": "e1", "status": "ignored", "error": "product_without_recovery_flows"}]


def test_maribel_the_cart_of_el_protocolo_stays_with_the_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    async def resolve(**kwargs: Any) -> object:
        captured.append(kwargs)
        return SimpleNamespace(event_id=kwargs["webhook_event_id"])

    monkeypatch.setattr(worker_module, "resolve_event", resolve)
    worker, _ = _maribel_worker(native_on=True)
    # El carrito capturado de ATT1 con el producto y la oferta por defecto de Maribel (derivado).
    cart = _event("carrito_2uafw5bg", product_id=8443991, offer="u8ikyx78")

    asyncio.run(worker._process_one({"id": "e1", "payload": cart, "event_type": cart["event"]}))

    [call] = captured
    assert "native_only" not in call  # el principal, como hoy
    assert call["commercial_ally_config"].funnel_ref == "maribel"


def test_maribel_the_provision_sql_seeds_only_forever_young() -> None:
    manifest = _maribel_two()
    second = manifest.to_commercial_ally_configs()[1]

    sql = build_sql(manifest, "quien-prueba")

    assert second.funnel_ref == "forever-young" and second.product_hotlink == FY_HOTLINK
    assert f"'{second.funnel_ref}'" in sql and FY_HOTLINK in sql
    assert "'u8ikyx78'" not in sql  # las ofertas del principal ya estan sembradas
    assert sql.count("insert into public.checkout_offer_catalog") == 1 + len(second.additional_offer_codes)
    assert f"'{FY_OFFER}'" in sql and f"'{FY_OTHER}'" in sql


def test_maribel_the_followup_of_the_principal_skips_only_forever_young_conversations() -> None:
    # Sin etiqueta del principal, toda conversacion con la de forever-young se saltea; con
    # la del principal, solo las que no la tienen. Por eso la PR de la instancia la declara.
    assert followup_product_labels(_maribel_two()) == (frozenset({FY_LABEL}), None)
    assert followup_product_labels(_maribel_two(principal_label=True)) == (frozenset({FY_LABEL}), "el-protocolo")
    for labels, own, skipped in (
        ([FY_LABEL], None, True),
        ([FY_LABEL, "el-protocolo"], None, True),
        ([FY_LABEL, "el-protocolo"], "el-protocolo", False),
        (["el-protocolo"], "el-protocolo", False),
    ):
        decision = evaluate_followup_candidate(
            _silent_conversation(labels), [], expected_inbox_id=11, now_epoch=1_800_000_000,
            other_product_labels=frozenset({FY_LABEL}), own_product_label=own,
        )
        assert (decision.skip_reason == OTHER_PRODUCT_CONVERSATION) is skipped, (labels, own)


def test_maribel_without_products_nothing_of_this_applies() -> None:
    manifest = InstanceManifest.from_toml_file(MARIBEL_DIR / "instancia.toml")
    routes = ProductRoutes.build(manifest.to_commercial_ally_config(), manifest)

    assert manifest.products == ()
    assert followup_product_labels(manifest) == (frozenset(), None)
    assert routes.for_hotmart(_maribel_purchase()) is routes.principal
    with pytest.raises(SystemExit):
        build_sql(manifest, "x")
