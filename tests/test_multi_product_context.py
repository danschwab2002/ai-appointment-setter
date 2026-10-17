"""La etiqueta y lo que hizo la persona de cada producto (varios productos, 2c-3b).

Con mas de un producto:
- lo que hizo la persona se lee en el funnel de cada producto y llega junto al
  agente, cada cosa con su producto, de la mas vieja a la mas nueva y con un
  tope de 20;
- el ultimo pago rechazado es el mas reciente de todos los funnels y suma la
  clave `product`;
- cuando la persona escribe, la conversacion lleva la etiqueta de cada producto
  que aparece en lo que hizo; sin nada, ninguna. Cuando sale un link, la del
  producto del link.
Con uno solo, todo como antes (tests/test_person_activity.py,
tests/test_payment_failure_context.py y tests/test_product_label.py).

Fixtures:
- tests/fixtures/att1_person_activity_20261016.json y
  tests/fixtures/hotmart_purchase_canceled_att1_20261012.json, por los helpers de
  sus pruebas: lo real de ATT1. Lo del segundo producto se deriva de esas mismas
  filas reales y se nombra asi: ATT2 nunca mando nada a att1-db.
- tests/fixtures/chatwoot_macros_account_2_20261016.json y el webhook real de la
  conversacion 158, por los helpers de tests/test_product_label.py.
"""

from __future__ import annotations

import copy
import logging
import tomllib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.app import (
    PERSON_ACTIVITY_CONTEXT_LIMIT,
    create_app,
    person_activity_context,
    products_activity_context,
    recent_payment_failure_context,
)
from bridge.commercial_ally import JOHANNA_COMMERCIAL_ALLY
from bridge.instance_manifest import InstanceManifest
from bridge.supabase import PersonActivity, RecentPaymentFailure, SupabaseError
from test_checkout_link_for_funnel import _two_products_settings
from test_multi_product_manifest import SECOND
from test_payment_failure_context import _captured_failure
from test_person_activity import _activities
from test_product_label import (
    LABEL_MACRO,
    _AdmittingSupabase,
    _Bridge158,
    _bridge_settings,
    _client,
    _conversation_158,
    _post_158,
)
from test_whatsapp_inbound_equivalence import WA_ID, _process, _Supabase, _text_webhook

ATT1_TOML = Path(__file__).parent / "fixtures" / "instances" / "att1" / "instancia.toml"
SECOND_MACRO_ID = 8
# El arnes de la conversacion 158 (el de la 2a) corre con el binding por defecto
# como principal: la etiqueta va por la clave del producto, no por el binding.
PRINCIPAL_FUNNEL = JOHANNA_COMMERCIAL_ALLY.funnel_ref


def _derived(activities: tuple[PersonActivity, ...], hours: int) -> tuple[PersonActivity, ...]:
    """Filas reales de ATT1, corridas en el tiempo, como si fueran del segundo producto."""
    return tuple(replace(item, occurred_at=item.occurred_at + timedelta(hours=hours)) for item in activities)


# ------------------------------------------------------- lo que ve el agente


def test_the_activity_of_every_funnel_goes_together_oldest_first() -> None:
    first = _activities(7)  # carrito, pago rechazado, compra (13 y 14/10)
    second = _derived(_activities(17), hours=140)  # un carrito, el 13/10 a las 20:13

    context = products_activity_context(
        [*(("att1", item) for item in first), *(("att2", item) for item in second)]
    )

    assert [(item["product"], item["activity"]) for item in context] == [
        ("att2", "cart"),
        ("att1", "cart"),
        ("att1", "payment_rejected"),
        ("att1", "purchase"),
    ]
    assert [item["at"] for item in context] == sorted(item["at"] for item in context)


def test_together_the_activity_keeps_only_the_20_most_recent() -> None:
    first = _activities(1)  # 7 cosas
    second = tuple(
        item
        for number, hours in ((2, 1), (3, 2), (4, 3), (5, 4))
        for item in _derived(_activities(number), hours=hours)
    )  # 18 cosas
    items = [*(("att1", item) for item in first), *(("att2", item) for item in second)]

    context = products_activity_context(items)

    assert len(items) > PERSON_ACTIVITY_CONTEXT_LIMIT == 20
    assert len(context) == 20
    newest = sorted(items, key=lambda pair: pair[1].occurred_at)[-20:]
    assert [item["activity"] for item in context] == [item.activity for _, item in newest]


def test_with_one_product_the_context_is_the_one_of_the_2b() -> None:
    assert person_activity_context(_activities(2), product="att1") == products_activity_context(
        ("att1", item) for item in _activities(2)
    )


class _ByFunnel(_Supabase):
    """Lo que hizo la persona y el ultimo pago rechazado, por funnel."""

    def __init__(
        self,
        *,
        activities: dict[str, tuple[PersonActivity, ...]] | None = None,
        failures: dict[str, RecentPaymentFailure] | None = None,
        failing: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self.activities = activities or {}
        self.failures = failures or {}
        self.failing = failing
        self.activity_lookups: list[tuple[str, str]] = []
        self.failure_lookups: list[tuple[str, str]] = []

    async def get_portable_person_activity(self, **kwargs: object) -> tuple[PersonActivity, ...]:
        funnel = kwargs["config"].funnel_ref  # type: ignore[attr-defined]
        self.activity_lookups.append((funnel, str(kwargs["whatsapp_phone"])))
        if funnel in self.failing:
            raise SupabaseError("portable_person_activity_get_failed: HTTP 503")
        return self.activities.get(funnel, ())

    async def get_portable_recent_payment_failure(self, **kwargs: object) -> RecentPaymentFailure | None:
        funnel = kwargs["config"].funnel_ref  # type: ignore[attr-defined]
        self.failure_lookups.append((funnel, str(kwargs["whatsapp_phone"])))
        if funnel in self.failing:
            raise SupabaseError("portable_recent_payment_failure_get_failed: HTTP 503")
        return self.failures.get(funnel)


def _two_products_turn_settings(tmp_path: Path):
    settings = _two_products_settings(tmp_path)
    manifest = settings.instance_manifest
    assert manifest is not None
    return replace(
        settings,
        instance_manifest=replace(manifest, flows={**manifest.flows, "pago_fallido": True}),
        portable_hotmart_payment_failure_enabled=True,
    )


def _turn(tmp_path: Path, supabase: _ByFunnel):
    webhook, history = _text_webhook("Hola, ¿qué pasó con mi pago?")
    _, shadow, _ = _process(
        tmp_path,
        supabase,
        settings=_two_products_turn_settings(tmp_path),
        webhook=webhook,
        history=history,
    )
    return shadow.calls[0][1]


def test_with_two_products_the_agent_gets_what_the_person_did_in_both(
    tmp_path: Path,
) -> None:
    supabase = _ByFunnel(
        activities={"att1": _activities(7), "att2": _derived(_activities(17), hours=140)}
    )

    context = _turn(tmp_path, supabase)

    assert sorted(supabase.activity_lookups) == [("att1", WA_ID), ("att2", WA_ID)]
    assert [(item["product"], item["activity"]) for item in context["person_activity"]] == [
        ("att2", "cart"),
        ("att1", "cart"),
        ("att1", "payment_rejected"),
        ("att1", "purchase"),
    ]


def test_with_two_products_a_failed_funnel_leaves_only_that_one_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    supabase = _ByFunnel(
        activities={"att1": _activities(7), "att2": _activities(17)},
        failing=frozenset({"att2"}),
    )

    with caplog.at_level(logging.WARNING):
        context = _turn(tmp_path, supabase)

    assert {item["product"] for item in context["person_activity"]} == {"att1"}
    assert "person_activity_lookup_failed error=SupabaseError product=att2" in caplog.text


def test_with_two_products_the_recent_failure_is_the_newest_and_says_its_product(
    tmp_path: Path,
) -> None:
    older, newer = sorted((_captured_failure(0), _captured_failure(1)), key=lambda f: f.failed_at)
    assert older.failed_at < newer.failed_at
    supabase = _ByFunnel(failures={"att1": older, "att2": newer})

    context = _turn(tmp_path, supabase)

    assert sorted(supabase.failure_lookups) == [("att1", WA_ID), ("att2", WA_ID)]
    assert context["recent_payment_failure"] == {
        **recent_payment_failure_context(newer),
        "product": "att2",
    }


# ------------------------------------------------------------- la etiqueta


def _second_macro() -> dict:
    macro = copy.deepcopy(LABEL_MACRO)
    macro["payload"].update(
        id=SECOND_MACRO_ID,
        name="Etiqueta de prueba ATT2",
        actions=[{"action_name": "add_label", "action_params": ["att2"]}],
    )
    return macro


class _TwoLabels(_Bridge158):
    """La cuenta 2 con la conversacion 158 y el macro de la etiqueta de cada producto."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/api/v1/accounts/2/macros/{SECOND_MACRO_ID}":
            self.requests.append(request)
            return httpx.Response(200, json=_second_macro())
        if path == f"/api/v1/accounts/2/macros/{SECOND_MACRO_ID}/execute":
            self.requests.append(request)
            self.labels.append("att2")
            return httpx.Response(200)
        return super().__call__(request)


class _LabelSupabase(_AdmittingSupabase):
    def __init__(self, transport: _TwoLabels, activities: dict[str, tuple[PersonActivity, ...]]) -> None:
        super().__init__(transport)
        self.activities = activities
        self.activity_lookups: list[tuple[str, str]] = []

    async def get_portable_person_activity(self, **kwargs: object) -> tuple[PersonActivity, ...]:
        funnel = kwargs["config"].funnel_ref  # type: ignore[attr-defined]
        self.activity_lookups.append((funnel, str(kwargs["whatsapp_phone"])))
        return self.activities.get(funnel, ())


def _two_labeled_manifest(second_macro: int = SECOND_MACRO_ID) -> InstanceManifest:
    payload = tomllib.loads(ATT1_TOML.read_text(encoding="utf-8"))
    second = copy.deepcopy(SECOND)
    second["macro_etiqueta"] = second_macro
    payload["productos"] = [
        {"clave": "att1", "etiqueta": "att1", "macro_etiqueta": 7},
        second,
    ]
    return InstanceManifest.from_mapping(payload)


def _label_app(tmp_path: Path, activities: dict[str, tuple[PersonActivity, ...]]):
    fixture = _conversation_158()
    transport = _TwoLabels(fixture)
    supabase = _LabelSupabase(transport, activities)
    manifest = _two_labeled_manifest()
    app = create_app(
        _bridge_settings(
            manifest,
            tmp_path,
            chatwoot_cut_b_admission_enabled=True,
            chatwoot_cut_b_scope_key="att1-inbound",
            chatwoot_cut_b_scope_version=1,
        ),
        chatwoot_client=_client(transport),
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    return app, fixture, transport, supabase


def test_an_inbound_message_gets_the_label_of_the_product_the_person_touched(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app, fixture, transport, supabase = _label_app(tmp_path, {"att2": _activities(17)})
    caplog.set_level(logging.INFO)

    assert _post_158(app, fixture["webhook"]).status_code == 202

    # Una lectura por funnel, con el movil de quien escribe.
    assert sorted(supabase.activity_lookups) == sorted(
        [(PRINCIPAL_FUNNEL, "573000000158"), ("att2", "573000000158")]
    )
    assert transport.paths("POST") == [f"/api/v1/accounts/2/macros/{SECOND_MACRO_ID}/execute"]
    assert transport.labels == ["automation_paused", "att2"]
    # Antes de la admision, como la etiqueta de un producto.
    assert supabase.label_requests_at_admission == 1
    assert any(
        "product_label outcome=added source=entrante conversation=158 label=att2" in m
        for m in caplog.messages
    )


def test_with_activity_in_both_products_the_conversation_gets_both_labels(
    tmp_path: Path,
) -> None:
    app, fixture, transport, _ = _label_app(
        tmp_path, {PRINCIPAL_FUNNEL: _activities(7), "att2": _activities(17)}
    )

    assert _post_158(app, fixture["webhook"]).status_code == 202

    assert sorted(transport.paths("POST")) == [
        "/api/v1/accounts/2/macros/7/execute",
        f"/api/v1/accounts/2/macros/{SECOND_MACRO_ID}/execute",
    ]
    assert sorted(transport.labels) == ["att1", "att2", "automation_paused"]


def test_without_activity_the_conversation_gets_no_product_label(tmp_path: Path) -> None:
    app, fixture, transport, _ = _label_app(tmp_path, {})

    assert _post_158(app, fixture["webhook"]).status_code == 202

    # No se adivina el producto: ni se lee un macro.
    assert not any("/macros/" in path for path in transport.paths())
    assert transport.labels == ["automation_paused"]


def test_ready_publishes_the_state_of_each_label(tmp_path: Path) -> None:
    app, _, _, _ = _label_app(tmp_path, {})

    with TestClient(app) as client:
        body = client.get("/ready").json()

    assert body["product_label_att1"] == "unverified"
    assert body["product_label_att2"] == "unverified"
    assert "product_label" not in body


def test_the_macro_of_every_label_is_checked_against_the_automation_macros(
    tmp_path: Path,
) -> None:
    manifest = _two_labeled_manifest(second_macro=3)
    settings = _bridge_settings(
        manifest,
        tmp_path,
        chatwoot_pause_macro_id=3,
    )

    with pytest.raises(ValueError, match="cannot be a pause, resume or opt-out macro"):
        create_app(settings, chatwoot_client=_client(_TwoLabels(_conversation_158())))


def test_a_link_that_goes_out_gets_the_label_of_its_product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_agent_link_product import _link_turn
    from test_webhook import StubChatwootClient
    from test_whatsapp_inbound_equivalence import PAYMENT_LINK_PROPOSAL

    labels_by_macro = {7: "att1", SECOND_MACRO_ID: "att2"}
    executed: list[tuple[int, int]] = []

    async def get_macro_actions(self, *, macro_id: int) -> list[dict[str, object]]:
        return [{"action_name": "add_label", "action_params": [labels_by_macro[macro_id]]}]

    async def get_conversation_labels(self, *, conversation_id: int) -> list[str]:
        return []

    async def execute_macro(self, *, macro_id: int, conversation_id: int) -> None:
        executed.append((macro_id, conversation_id))

    for name, method in (
        ("get_macro_actions", get_macro_actions),
        ("get_conversation_labels", get_conversation_labels),
        ("execute_macro", execute_macro),
    ):
        monkeypatch.setattr(StubChatwootClient, name, method, raising=False)
    settings = _two_products_settings(tmp_path)
    assert settings.instance_manifest is not None
    payload = tomllib.loads(ATT1_TOML.read_text(encoding="utf-8"))
    payload["productos"] = [
        {"clave": "att1", "etiqueta": "att1", "macro_etiqueta": 7},
        copy.deepcopy(SECOND),
    ]
    settings = replace(
        settings,
        instance_manifest=replace(
            settings.instance_manifest,
            products=InstanceManifest.from_mapping(payload).products,
        ),
    )

    chatwoot, _ = _link_turn(tmp_path, {**PAYMENT_LINK_PROPOSAL, "product": "att2"}, settings)

    assert any("src=hermes" in str(reply["content"]) for reply in chatwoot.reply_calls)
    # Sin nada en lo que hizo la persona, al entrar no se etiqueta; al salir el
    # link, la del producto del link y ninguna otra.
    assert executed == [(SECOND_MACRO_ID, 200)]
