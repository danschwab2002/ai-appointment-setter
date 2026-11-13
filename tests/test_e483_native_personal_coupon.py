"""E-483 (C2 del mapa «el AE maneja todos los flujos», 2026-11-12; P8): el cupon propio por persona en el nativo.

La base (20261112210500) le da al carrito y al pago rechazado sin cadena el boton ``{link: checkout, cupon:
personal}`` en cualquier toque: el primero del camino es el ordinal 1 del caso y el segundo el 2, el trabajador
del cupon de ATT1 (``recovery_coupons.py``, sin cambios) lo crea, y la reserva emite nuestro link (lo mide
``validate_e483_native_personal_coupon.mjs`` en PGlite, con el carrito capturado de ATT1, y fija que la siembra,
el link y la politica de la ruta motor de ATT1 salen igual antes y despues). Aca: la regla 14 nativa, la lectura
del bridge, el envio con FLOW_ENGINE_NATIVE_PERSONAL_COUPON_ENABLED y que lo que factura ATT1 no cambia.

Datos: las filas de la base son las de ``tests/fixtures/flow_engine/native_personal_coupon_rows.json`` (las
escribe ``validate_e483_native_personal_coupon.mjs --emit``; el codigo, el id y el vencimiento, tapados); las
plantillas, las del carrito de ATT1 aprobadas el 18/10 (``chatwoot_message_templates_inbox_11_carrito_20261018.json``:
A1 con cinco variables y Z con tres, las dos con el boton del checkout); la respuesta de Hotmart al buscar un
cupon, la capturada en ATT1 el 20/10 (``tests/fixtures/hotmart/coupon_find_att1_2026-10-20.json``: no hay captura
de la creacion, que Hotmart contesta vacia). Los dobles del despachador son los de
``test_flow_engine_native_dispatch.py`` y ``test_cart_discount_first_touch.py``.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_cart_discount_first_touch import CARRITO_CATALOG, _CartAuthority
from test_cart_discount_first_touch import _dispatcher as _cart_dispatcher
from test_durable_dispatcher_approved_template import CATALOG, NOW, _Chatwoot
from test_flow_engine_native_dispatch import _dispatcher, _NativeAuthority, _NativeChatwoot, _run, _step, _templates
from test_recovery_coupon import FakeHotmart, _credentials

from bridge.approved_templates import REASON_PARAMETERS_MISSING
from bridge.flow_engine import (
    NATIVE_CAPABILITIES_BY_TRIGGER,
    NATIVE_PERSONAL_COUPON_TRIGGERS,
    native_not_supported,
)
from bridge.flow_validator import NativeRuntime
from bridge.hotmart_api import HotmartApiClient
from bridge.native_personal_coupon import (
    NATIVE_COUPON_NOT_DECLARED,
    NATIVE_COUPON_NOT_READY_IN_TIME,
    NATIVE_PERSONAL_COUPON_DISABLED,
    NativePersonalCouponConfig,
)
from bridge.recovery_coupons import RecoveryCouponWorker
from bridge.supabase import (
    FlowEngineNativeStepCoupon,
    RecoveryCheckoutAuthorization,
    RecoveryCheckoutReservation,
    RecoveryCouponClaim,
    RecoveryCouponForCase,
    RecoveryCouponSettlement,
    SupabaseClient,
    SupabaseError,
)
from bridge.worker import NATIVE_MULTITOUCH_DISABLED

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
MIGRATION = ROOT / "supabase" / "migrations" / "20261112210500_e483_native_personal_coupon_v1.sql"
ROWS = json.loads((FIXTURES / "flow_engine" / "native_personal_coupon_rows.json").read_text(encoding="utf-8"))
A1 = "att1_carrito_descuento_01"
Z = "att1_ultima_renovacion_01"
PERSONAL = {"link": "checkout", "cupon": "personal"}
CODE_1 = "ATT1A1B2C3D4E5F6"
CODE_2 = "ATT9F8E7D6C5B4A3"
EXPIRES = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
ISSUANCE_ID = "6f1d0c2a-7a4b-4c4e-9d1e-0b6a2f5c8e95"
LINK = ("https://pay.hotmart.com/D98014973Y?off=gopi6lh7&checkoutMode=10"
        "&src=hermes&sck=hermes~v1~01JABCDEFGHJKMNPQRSTVWXYZ5")
BARE = LINK.removeprefix("https://pay.hotmart.com/")
NAME = "Edith García Pérez"
PRODUCT = "Alimenta tu Tiroides"
CONFIG = NativePersonalCouponConfig(price="47", currency="USD")


def _send(name: str, variables: list[str], button: dict[str, str] | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {"plantilla": name, "idioma": "es_MX", "variables": variables}
    if button is not None:
        step["boton"] = dict(button)
    return step


A1_SEND = _send(A1, ["nombre", "producto", "cuando", "precio", "vence"], PERSONAL)
A2_SEND = _send("att1_carrito_dudas_01", ["nombre", "producto"])
Z_SEND = _send(Z, ["nombre", "producto", "vence"], PERSONAL)
CART = {
    "esquema": "setter-flujo/v1", "flujo": "carrito_cupon", "producto": "att1", "estado": "borrador",
    "nombre": "Carrito con cupon propio", "dispara": {"evento": "carrito"},
    "entra": {"permiso": "formulario", "contesto_a": []},
    "pasos": [A1_SEND, {"esperar": "24h"}, A2_SEND, {"esperar": "48h"}, Z_SEND],
    "termina": {"si": ["compra", "contesta"], "vence": "4d"}, "horario": "siempre",
    "topes": {"bolsa": "att1-carrito-cupon", "por_dia": 150, "total": 3000,
              "repeticion": {"una_por": "persona_y_oferta", "otra_despues_de": "0m"}},
    "prioridad": {"le_gana_a": [], "no_arranca_mientras": []},
}


def _cart(**changes: Any) -> dict[str, Any]:
    return {**copy.deepcopy(CART), **changes}


# -- La regla 14 nativa ------------------------------------------------------------------------------


def test_the_att1_cart_with_its_personal_coupons_runs_natively() -> None:
    # La forma de ATT1: A1 con cupon (cinco variables), A2 sin cupon, Z con cupon (tres variables).
    assert native_not_supported(CART) == ()
    # Y el cupon en A2 y en Z: los dos en toques siguientes.
    assert native_not_supported(_cart(pasos=[A2_SEND, {"esperar": "24h"}, Z_SEND, {"esperar": "48h"}, Z_SEND])) == ()
    # El pago rechazado sin cadena tambien.
    assert native_not_supported(_cart(dispara={"evento": "pago_fallido"},
                                      pasos=[A2_SEND, {"esperar": "24h"}, Z_SEND])) == ()
    assert NATIVE_PERSONAL_COUPON_TRIGGERS == {"carrito", "pago_fallido"}
    assert NATIVE_CAPABILITIES_BY_TRIGGER["carrito"].buttons == {"checkout"}
    assert NATIVE_CAPABILITIES_BY_TRIGGER["intencion"].buttons == frozenset()


@pytest.mark.parametrize(("change", "code"), [
    # Dos por caso como maximo (recovery_coupons, D3).
    ({"pasos": [A1_SEND, {"esperar": "24h"}, Z_SEND, {"esperar": "48h"}, Z_SEND]},
     "engine_not_supported:cupones_personales_de_mas"),
    # cuando es la hora del evento: solo el primer envio.
    ({"pasos": [A2_SEND, {"esperar": "24h"}, A1_SEND]}, "engine_not_supported:variables_differ"),
    # precio y vence, solo con el cupon propio.
    ({"pasos": [_send(A1, ["nombre", "vence"])], "termina": {"si": ["compra"], "vence": "1d"}},
     "engine_not_supported:variables_differ"),
    # En el carrito, el boton va solo con el cupon propio.
    ({"pasos": [_send(Z, ["nombre", "producto"], {"link": "checkout", "cupon": "ATT10"})],
      "termina": {"si": ["compra"], "vence": "1d"}}, "engine_not_supported:boton_sin_cupon_personal"),
    # La compra, el formulario y la cadena del pago rechazado, sin cupon propio.
    ({"dispara": {"evento": "compra"}, "entra": {"permiso": "compra", "contesto_a": []}, "pasos": [A1_SEND],
      "termina": {"si": ["compra"], "vence": "1d"}}, "engine_not_supported:cupon_personal"),
    ({"dispara": {"evento": "intencion"}}, "engine_not_supported:cupon_personal"),
    ({"dispara": {"evento": "pago_fallido"}, "pasos": [{"condicion": {"dato": "tipo_de_rechazo", "es": ["sin_saldo"]},
                                                         "entonces": [Z_SEND], "si_no": [A2_SEND]}]},
     "engine_not_supported:cupon_personal"),
    # Un boton de cupon propio que no es exactamente el del checkout.
    ({"pasos": [{**A1_SEND, "boton": {"cupon": "personal"}}], "termina": {"si": ["compra"], "vence": "1d"}},
     "engine_not_supported:cupon_personal"),
])
def test_what_the_native_personal_coupon_still_cannot_do(change: dict[str, Any], code: str) -> None:
    assert code in native_not_supported(_cart(**change))


def test_a_following_touch_keeps_refusing_any_other_button() -> None:
    other = _cart(pasos=[A2_SEND, {"esperar": "24h"}, {**A2_SEND, "boton": {"link": "checkout"}}])
    assert "engine_not_supported:boton_despues_del_primer_envio" in native_not_supported(other)


def test_the_migration_follows_the_mold() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'e483_native_personal_coupon_already_applied'" in sql
    for needed in ('"cupon_personal": true', "coupon_variables_de", "coupon_too_many:",
                   "_flow_engine_native_first_coupon_ordinal", "get_flow_engine_native_step_coupon_v1",
                   "grant execute on function public.get_flow_engine_native_step_coupon_v1(uuid) to service_role"):
        assert needed in sql, needed
    # El trabajador de Python del cupon no se toca: la siembra es de la base.
    assert "recovery_coupons.py" in sql


def test_the_new_test_runs_with_npm_test() -> None:
    package = json.loads((ROOT / "tests/sql/followup_engine/package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["test"].endswith("node validate_e483_native_personal_coupon.mjs --check"
        " && node validate_e483_native_audience_source.mjs && node validate_flow_audience_triggers.mjs && node validate_flow_audience_native_e2e.mjs"
        " && node validate_e483_native_legacy_double_case_touches.mjs --check && node validate_e483_native_person_cap.mjs")


# -- La lectura del bridge -------------------------------------------------------------------------


def _row(key: str, **changes: Any) -> dict[str, Any]:
    row = {**copy.deepcopy(ROWS[key])}
    filled = {"coupon_id": "2c9b1f4e-1d3a-4f6b-8a7c-5e4d3c2b1a09", "code": CODE_1,
              "expires_at": "2026-10-01T15:00:00+00:00"}
    for name, value in filled.items():
        if isinstance(row.get(name), str) and row[name].startswith("<"):
            row[name] = value
    row.update(changes)
    return row


def _client(answer: object) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/rpc/get_flow_engine_native_step_coupon_v1"
        assert json.loads(request.content) == {"p_action_id": "action-att1"}
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    return SupabaseClient(base_url="https://supabase.example.test", service_role_key="clave-de-prueba",
                          transport=httpx.MockTransport(handler))


def _read(answer: object) -> FlowEngineNativeStepCoupon | None:
    return asyncio.run(_client(answer).get_flow_engine_native_step_coupon(action_id="action-att1"))


def test_the_fixture_is_the_native_cart_of_att1() -> None:
    assert ROWS["_capture"]["encargo"] == "E-483 C2"
    assert ROWS["a1_context"]["template_name"] == A1 and ROWS["a1_context"]["template_button"] == PERSONAL
    assert ROWS["z_context"]["template_name"] == Z and ROWS["z_context"]["send_ordinal"] == 3
    assert (ROWS["a1_coupon"]["ordinal"], ROWS["z_coupon"]["ordinal"]) == (1, 2)


def test_the_client_reads_the_ordinal_and_the_coupon_of_the_step() -> None:
    read = _read([_row("z_coupon")])
    assert read == FlowEngineNativeStepCoupon(ordinal=2, coupon=RecoveryCouponForCase(
        coupon_id="2c9b1f4e-1d3a-4f6b-8a7c-5e4d3c2b1a09", code=CODE_1, status="reserved", expires_at=EXPIRES,
        verified_at=None))
    # Sin fila, el paso no lleva cupon; con la fila sin cupon, el trabajador todavia no lo sembro.
    assert _read([]) is None
    unseeded = {"ordinal": 1, "coupon_id": None, "code": None, "status": None, "expires_at": None, "verified_at": None}
    assert _read([unseeded]) == FlowEngineNativeStepCoupon(ordinal=1, coupon=None)


@pytest.mark.parametrize("answer", [
    [_row("a1_coupon", ordinal=3)],
    [_row("a1_coupon", code="otro-codigo")],
    [{"ordinal": 1, "coupon_id": None, "code": CODE_1, "status": None, "expires_at": None, "verified_at": None}],
    [_row("a1_coupon"), _row("z_coupon")],
    httpx.Response(404, json={"message": "no existe"}),
])
def test_the_client_refuses_a_row_it_cannot_trust(answer: object) -> None:
    with pytest.raises(SupabaseError):
        _read(answer)


# -- El envio --------------------------------------------------------------------------------------


class _CouponAuthority(_NativeAuthority):
    """El carrito nativo de ATT1: el contexto del paso con el cupon, su cupon y nuestro link."""

    def __init__(self, *, step_key: str = "first_contact", status: str | None = "valid",
                 coupon: FlowEngineNativeStepCoupon | None | str = "default", attempt_number: int = 1) -> None:
        context = ROWS["a1_context"] if step_key == "first_contact" else ROWS["z_context"]
        super().__init__(step=_step(
            step_key=step_key, send_ordinal=context["send_ordinal"], flow_key=context["flow_key"],
            template_name=context["template_name"], template_language=context["template_language"],
            template_variables=tuple(context["template_variables"]), template_button=dict(context["template_button"]),
            entry_permission=context["entry_permission"]))
        if step_key != "first_contact":
            self.action = replace(self.action, action_type="no_reply_review", step_key=step_key, anchor_type="message")
            self.context = replace(self.context, action_type="no_reply_review", step_key=step_key)
        self.attempt = replace(self.attempt, attempt_number=attempt_number)
        ordinal = 1 if step_key == "first_contact" else 2
        self.coupon = (FlowEngineNativeStepCoupon(ordinal=ordinal, coupon=None if status is None else RecoveryCouponForCase(
            coupon_id="2c9b1f4e-1d3a-4f6b-8a7c-5e4d3c2b1a09", code=CODE_1 if ordinal == 1 else CODE_2, status=status,
            expires_at=EXPIRES, verified_at=None)) if coupon == "default" else coupon)
        self.issuance_finalizations: list[dict[str, object]] = []

    async def get_flow_engine_native_step_coupon(self, *, action_id: str) -> FlowEngineNativeStepCoupon | None:
        assert action_id == self.action.action_id
        self.events.append("step_coupon")
        return self.coupon  # type: ignore[return-value]

    async def reserve_recovery_checkout_issuance(self, **kwargs: object) -> RecoveryCheckoutReservation:
        assert kwargs["attempt_id"] == self.attempt.attempt_id
        self.events.append("reserve_link")
        return RecoveryCheckoutReservation("reserved", ISSUANCE_ID, LINK, "hermes~v1~01JABCDEFGHJKMNPQRSTVWXYZ5")

    async def authorize_recovery_checkout_issuance(self, **kwargs: object) -> RecoveryCheckoutAuthorization:
        assert kwargs["issuance_id"] == ISSUANCE_ID
        self.events.append("authorize_link")
        return RecoveryCheckoutAuthorization("request_started", ISSUANCE_ID, "request_started")

    async def finalize_chatwoot_checkout_issuance_v2(self, **kwargs: object) -> object:
        self.issuance_finalizations.append(kwargs)
        return object()


def _chatwoot() -> _NativeChatwoot:
    chatwoot = _NativeChatwoot()
    chatwoot.catalog.extend(copy.deepcopy(CARRITO_CATALOG))
    return chatwoot


def _body(name: str, values: list[str]) -> str:
    [template] = [t for t in CARRITO_CATALOG if t["name"] == name]
    [body] = [c["text"] for c in template["components"] if c["type"] == "BODY"]
    for position, value in enumerate(values, start=1):
        body = body.replace("{{%d}}" % position, value)
    return body


def _native(authority: _CouponAuthority, chatwoot: _NativeChatwoot, tmp_path: Path, *,
            coupon: NativePersonalCouponConfig | None = CONFIG, multitouch: bool = True) -> Any:
    dispatcher = _dispatcher(authority, chatwoot, tmp_path)
    dispatcher._native_personal_coupon = coupon
    dispatcher._native_multitouch_enabled = multitouch
    return dispatcher


def test_without_the_flag_the_touch_with_the_personal_coupon_closes_and_reads_nothing(tmp_path: Path) -> None:
    authority = _CouponAuthority()
    chatwoot = _chatwoot()

    _run(_native(authority, chatwoot, tmp_path, coupon=None))

    assert _templates(chatwoot) == []
    assert "step_coupon" not in authority.events and "reserve_link" not in authority.events
    [closed] = authority.finalizations
    assert closed["reason_code"] == NATIVE_PERSONAL_COUPON_DISABLED
    assert closed["next_attempt_at"] is None


def test_a1_goes_out_with_its_own_coupon_our_link_and_the_hour_of_the_country(tmp_path: Path) -> None:
    authority = _CouponAuthority()
    chatwoot = _chatwoot()

    _run(_native(authority, chatwoot, tmp_path))

    # El telefono del arnes es de EE. UU.: el carrito de las 14:00Z son las 10:00 en Nueva York y el cupon
    # vence a las 24 h de A1 (la siembra de la base), el 01/10 a las 11:00.
    values = [NAME, PRODUCT, "hoy a las 10:00 a. m.", "USD 47", "mañana a las 11:00 a. m."]
    [message] = _templates(chatwoot)
    assert message["content"] == _body(A1, values)
    assert message["template_params"] == {
        "name": A1, "category": "MARKETING", "language": "es_MX",
        "processed_params": {"body": {str(i): v for i, v in enumerate(values, start=1)},
                             "buttons": [{"type": "url", "parameter": f"{BARE}&offDiscount={CODE_1}"}]},
    }
    assert [event for event in authority.events if event in ("step_coupon", "reserve_link", "authorize_link")] == [
        "step_coupon", "reserve_link", "authorize_link"]
    assert authority.issuance_finalizations[0]["status"] == "accepted_by_chatwoot"


def test_z_goes_out_as_a_following_touch_with_coupon_2(tmp_path: Path) -> None:
    authority = _CouponAuthority(step_key="native_touch_3")
    chatwoot = _chatwoot()
    dispatcher = _native(authority, chatwoot, tmp_path)

    composition = asyncio.run(dispatcher._compose_approved_template_proposal(
        action=authority.action, execution_context=authority.context, attempt=authority.attempt,
        step_template=authority.step))

    assert composition.failure_reason is None
    assert composition.template_name == Z
    assert composition.issuance_id == ISSUANCE_ID
    values = [NAME, PRODUCT, "mañana a las 11:00 a. m."]
    assert composition.proposal is not None and composition.proposal.message == _body(Z, values)
    assert composition.template_params["processed_params"]["buttons"] == [  # type: ignore[index]
        {"type": "url", "parameter": f"{BARE}&offDiscount={CODE_2}"}]


def test_z_without_the_following_touches_closes_as_any_following_touch(tmp_path: Path) -> None:
    authority = _CouponAuthority(step_key="native_touch_3")
    dispatcher = _native(authority, _chatwoot(), tmp_path, multitouch=False)

    composition = asyncio.run(dispatcher._compose_approved_template_proposal(
        action=authority.action, execution_context=authority.context, attempt=authority.attempt,
        step_template=authority.step))

    assert composition.failure_reason == NATIVE_MULTITOUCH_DISABLED and not composition.retryable
    assert "step_coupon" not in authority.events


@pytest.mark.parametrize(("status", "attempt", "reason", "retry"), [
    # Recien sembrado: espera como Z (Hotmart aplica el cupon unos 5 minutos despues de crearlo).
    ("reserved", 1, "recovery_coupon_not_ready", True),
    (None, 2, "recovery_coupon_not_ready", True),
    # Despues de los intentos de espera, no sale: el toque promete el descuento y no hay plantilla sin el.
    ("created", 3, NATIVE_COUPON_NOT_READY_IN_TIME, False),
    ("create_failed", 1, "recovery_coupon_create_failed", False),
    ("redeemed", 1, "recovery_coupon_redeemed", False),
])
def test_without_a_valid_coupon_nothing_goes_out(tmp_path: Path, status: str | None, attempt: int, reason: str,
                                                 retry: bool) -> None:
    authority = _CouponAuthority(status=status, attempt_number=attempt)
    chatwoot = _chatwoot()

    _run(_native(authority, chatwoot, tmp_path))

    assert _templates(chatwoot) == []
    assert "reserve_link" not in authority.events
    [closed] = authority.finalizations
    assert closed["reason_code"] == reason
    assert (closed["next_attempt_at"] is not None) is retry


def test_a_step_the_policy_does_not_declare_with_a_coupon_never_goes_out(tmp_path: Path) -> None:
    authority = _CouponAuthority(coupon=None)
    chatwoot = _chatwoot()

    _run(_native(authority, chatwoot, tmp_path))

    assert _templates(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == NATIVE_COUPON_NOT_DECLARED and closed["next_attempt_at"] is None


def test_a_price_the_instance_does_not_have_closes_the_touch_that_says_it(tmp_path: Path) -> None:
    authority = _CouponAuthority()
    chatwoot = _chatwoot()

    _run(_native(authority, chatwoot, tmp_path, coupon=NativePersonalCouponConfig(price=None)))

    assert _templates(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == REASON_PARAMETERS_MISSING


# -- Lo que factura ATT1 no cambia ------------------------------------------------------------------


@pytest.mark.parametrize("coupon_status", ["valid", "reserved"])
def test_the_legacy_a1_of_att1_goes_out_the_same_with_the_flag_off_and_on(tmp_path: Path, coupon_status: str) -> None:
    # A1 del legado (la ruta de hoy de ATT1, y la de la ruta motor: el mismo compose_cart_discount_first_touch):
    # el mismo mensaje, el mismo codigo en el boton, el mismo vencimiento dicho y el mismo cierre.
    outputs = []
    for coupon in (None, CONFIG):
        authority = _CartAuthority(coupon_status=coupon_status)
        chatwoot = _Chatwoot([*CATALOG, *CARRITO_CATALOG])
        dispatcher = _cart_dispatcher(authority, chatwoot, tmp_path / f"run-{len(outputs)}")
        dispatcher._native_personal_coupon = coupon
        asyncio.run(dispatcher.dispatch_due(now=NOW))
        outputs.append((chatwoot.posts("/conversations/200/messages"), authority.events,
                        [{k: v for k, v in f.items() if k != "attempt_id"} for f in authority.finalizations]))
    assert outputs[0] == outputs[1]
    if coupon_status == "valid":
        [message] = outputs[0][0]
        assert message["template_params"]["processed_params"]["buttons"][0]["parameter"].endswith(
            f"&offDiscount={CODE_1}")


def test_the_coupon_worker_creates_a_native_coupon_like_any_other(tmp_path: Path) -> None:
    # El trabajador de ATT1 no mira de que flujo es el cupon: el reclamo es una fila de recovery_coupons. Con la
    # respuesta capturada de Hotmart al buscarlo (sin la clave active; la creacion es la de FakeHotmart de
    # test_recovery_coupon: no hay captura), lo crea y a los 7 minutos lo verifica valid.
    hotmart = FakeHotmart()
    settles: list[tuple[str, str]] = []
    now = datetime(2026, 10, 18, 15, 0, tzinfo=UTC)
    claim = RecoveryCouponClaim(coupon_id="11111111-1111-4111-8111-111111111111",
                                recovery_case_id="22222222-2222-4222-8222-222222222222", code=CODE_2,
                                status="reserved", external_product_id="6712345",
                                expires_at=now + timedelta(hours=24, minutes=15), attempt_count=1)

    class _Store:
        def __init__(self) -> None:
            self.claims = [claim]

        async def claim_due_recovery_coupons(self, **_: object) -> list[RecoveryCouponClaim]:
            claims, self.claims = self.claims, []
            return claims

        async def settle_recovery_coupon(self, **kwargs: object) -> RecoveryCouponSettlement:
            settles.append((str(kwargs["status"]), str(kwargs["coupon_id"])))
            return RecoveryCouponSettlement(coupon_id=str(kwargs["coupon_id"]), status=str(kwargs["status"]),
                                            applied=True)

    store = _Store()
    worker = RecoveryCouponWorker(
        supabase=store,  # type: ignore[arg-type]
        hotmart=HotmartApiClient(credentials=_credentials(), transport=httpx.MockTransport(hotmart.handler)),
        code_prefix="ATT", clock=lambda: now)
    asyncio.run(worker.run_once())
    store.claims = [replace(claim, status="created", attempt_count=2)]
    asyncio.run(worker.run_once())

    assert [status for status, _ in settles] == ["request_started", "created", "valid"]


def test_the_runtime_says_whether_the_bridge_builds_the_personal_coupon() -> None:
    fields = dict(engine_enabled=True, native_intent_enabled=True, native_silence_sweeper=True, cart_touches=True,
                  main_product="att1")
    assert NativeRuntime(**fields).to_json()["native_personal_coupon_enabled"] is False
    assert NativeRuntime(**fields, native_personal_coupon=True).to_json()["native_personal_coupon_enabled"] is True
    with pytest.raises(ValueError):
        NativeRuntime(**fields, native_personal_coupon="si")  # type: ignore[arg-type]
