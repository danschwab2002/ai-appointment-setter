"""Las cuatro ramas del pago rechazado nativo, en el bridge (P-16, E-306, fase A del plan de E-302: A4 y A5;
flow-engine-v2 §27). Migracion ``20261103000900_flow_engine_native_payment_failure_branches_v1.sql``.

Por que: Dan decidio el 2026-11-02 que el pago rechazado de Maribel lleve las cuatro versiones de ATT1 (D1
«Las 4»), por el motor nativo (D4), con el boton de nuestro link (D5) y el cupon fijo PROTOCOLO10 (D10). El
envio nativo mandaba una sola plantilla y cerraba para siempre un paso con boton.

Datos:

* Las filas de la base (el contexto del paso con sus ramas, los tres datos, el registro y la reserva) salen
  de la base real (PGlite): ``tests/fixtures/flow_engine/native_payment_failure_branches_rows.json``, que
  escribe ``validate_flow_engine_native_payment_failure_branches.mjs --emit`` con los rechazos capturados de
  Maribel por las admisiones portables de hoy.
* Las cuatro plantillas son las de Maribel, RECONSTRUIDAS del pedido a Meta
  (``chatwoot_message_templates_inbox_13_maribel_pago_rechazado_reconstruida_20261102.json``).
* Los dobles del despachador y de Chatwoot son los de ``test_flow_engine_native_dispatch.py`` (el inbox 11 de
  ATT1, con las cuatro plantillas sumadas al catalogo).

Lo que fijan: el cliente lee las ramas, los datos y el registro de la base; la eleccion es la de
``choose_variant`` del legado (la paridad de las cuatro de ATT1); el despachador pide los datos, elige,
registra antes del envio y manda la plantilla de la rama con nuestro link y ``offDiscount=PROTOCOLO10``; una
falla de los datos o del registro cierra con su motivo sin caer a otra rama (la eleccion vieja se reintenta);
sin ramas ni boton, el paso nativo de hoy sale igual.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_flow_engine_native_dispatch import _dispatcher, _NativeAuthority, _NativeChatwoot, _run, _step, _templates

from bridge.payment_failure_variant import (
    VARIANT_BY_NATIVE_DATUM,
    VARIANTS,
    choose_native_branch,
    choose_variant,
)
from bridge.supabase import (
    FlowEngineNativeBranch,
    NativePaymentFailureData,
    RecoveryCheckoutAuthorization,
    RecoveryCheckoutReservation,
    SupabaseClient,
    SupabaseError,
    SupabasePermanentError,
)
from bridge.worker import (
    FLOW_ENGINE_STEP_UNSUPPORTED,
    NATIVE_BRANCH_DATA_UNAVAILABLE,
    NATIVE_BRANCH_STALE,
)

FIXTURES = Path(__file__).parent / "fixtures"
ROWS = json.loads((FIXTURES / "flow_engine" / "native_payment_failure_branches_rows.json").read_text(encoding="utf-8"))
RECONSTRUCTED = json.loads((FIXTURES / (
    "chatwoot_message_templates_inbox_13_maribel_pago_rechazado_reconstruida_20261102.json")).read_text(
        encoding="utf-8"))
TEMPLATES = {template["name"]: template for template in RECONSTRUCTED["message_templates"]}
FLOW_ID = "0b3c5f00-0e30-4000-8000-0000000e3060"
ISSUANCE_ID = "6f1d0c2a-7a4b-4c4e-9d1e-0b6a2f5c8e93"
LINK = ("https://pay.hotmart.com/D98014973Y?off=u8ikyx78&checkoutMode=10"
        "&src=hermes&sck=hermes~v1~01JABCDEFGHJKMNPQRSTVWXYZ2")
BARE = LINK.removeprefix("https://pay.hotmart.com/")
NAME = "Edith García Pérez"
PRODUCT = "Alimenta tu Tiroides"
BRANCHES = ("cupon", "dos_veces", "saldo", "banco")


def _client(rows: dict[str, Any]) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        operation = request.url.path.removeprefix("/rest/v1/rpc/")
        answer = rows[operation]
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    return SupabaseClient(base_url="https://supabase.example.test", service_role_key="clave-de-prueba",
                          transport=httpx.MockTransport(handler))


def _context_row(**changes: Any) -> dict[str, Any]:
    row = {**copy.deepcopy(ROWS["step_context"]), "flow_id": FLOW_ID}
    row.update(changes)
    return row


def _data_row(key: str) -> dict[str, Any]:
    return {**ROWS[f"data_{key}"], "latest_failed_at": "2026-09-30T14:45:00+00:00"}


def _parsed_step() -> Any:
    step = asyncio.run(_client({"flow_engine_execution_context_v1": [_context_row()]})
                       .get_flow_engine_step_context("action-att1"))
    assert step is not None
    return step


def test_the_fixture_is_the_real_chain_of_maribel() -> None:
    assert ROWS["_capture"]["encargo"] == "E-306"
    context = ROWS["step_context"]
    assert [branch["paso"]["plantilla"] for branch in context["template_branches"]] == [
        f"maribel_pago_rechazado_{branch}_01" for branch in BRANCHES]
    assert ROWS["record"] == {"outcome": "recorded", "template_name": "maribel_pago_rechazado_banco_01"}
    assert ROWS["record_stale"]["message"] == NATIVE_BRANCH_STALE
    assert ROWS["reservation"]["offer_resolution"] == "recovery_case_offer"
    assert ROWS["sales_by_branch"] == {"maribel_pago_rechazado_banco_01": {"sends": 1, "purchases": 1}}
    assert set(TEMPLATES) == {branch["paso"]["plantilla"] for branch in context["template_branches"]}


# -- El cliente --------------------------------------------------------------------------------------


def test_the_client_reads_the_four_branches_of_the_step() -> None:
    step = _parsed_step()
    assert step.template_name == "maribel_pago_rechazado_banco_01"
    assert step.template_button == {"link": "checkout", "cupon": "PROTOCOLO10"}
    assert [(branch.datum, branch.values, branch.template_name) for branch in step.template_branches] == [
        ("descuento_previo", ("si",), "maribel_pago_rechazado_cupon_01"),
        ("rechazos", ("varios",), "maribel_pago_rechazado_dos_veces_01"),
        ("tipo_de_rechazo", ("sin_saldo",), "maribel_pago_rechazado_saldo_01"),
        (None, None, "maribel_pago_rechazado_banco_01"),
    ]
    assert all(branch.template_button == {"link": "checkout", "cupon": "PROTOCOLO10"}
               for branch in step.template_branches)


def test_without_the_migration_or_the_chain_there_are_no_branches() -> None:
    plain = ROWS["plain_step_context"]
    assert plain["template_branches"] is None
    step = asyncio.run(_client({"flow_engine_execution_context_v1": [{**plain, "flow_id": FLOW_ID}]})
                       .get_flow_engine_step_context("action-att1"))
    assert step is not None and step.template_branches is None
    before = {key: value for key, value in plain.items() if key != "template_branches"}
    older = asyncio.run(_client({"flow_engine_execution_context_v1": [{**before, "flow_id": FLOW_ID}]})
                        .get_flow_engine_step_context("action-att1"))
    assert older is not None and older.template_branches is None


@pytest.mark.parametrize("branches", [
    [],
    [{"cuando": None, "paso": {"plantilla": "x", "idioma": "es_MX", "variables": []}}],
    # La ultima rama lleva condicion.
    [{"cuando": {"dato": "rechazos", "es": ["varios"]}, "paso": {"plantilla": "x", "idioma": "es_MX", "variables": []}},
     {"cuando": {"dato": "rechazos", "es": ["uno"]}, "paso": {"plantilla": "y", "idioma": "es_MX", "variables": []}}],
    # Un dato que no es del pago rechazado.
    [{"cuando": {"dato": "ventana", "es": ["abierta"]}, "paso": {"plantilla": "x", "idioma": "es_MX", "variables": []}},
     {"cuando": None, "paso": {"plantilla": "y", "idioma": "es_MX", "variables": []}}],
])
def test_a_branch_row_out_of_contract_is_an_invalid_row(branches: list) -> None:
    with pytest.raises(SupabaseError, match="invalid_row"):
        asyncio.run(_client({"flow_engine_execution_context_v1": [_context_row(template_branches=branches)]})
                    .get_flow_engine_step_context("action-att1"))


def test_the_client_reads_the_data_and_the_record_of_the_base() -> None:
    client = _client({
        "get_flow_engine_native_payment_failure_context_v1": [_data_row("dos")],
        "record_flow_engine_native_branch_v1": [ROWS["record"]],
    })
    data = asyncio.run(client.get_flow_engine_native_payment_failure_context(
        action_id="action-att1", worker_id="w", lease_generation=1, now="2026-09-30T15:00:00+00:00"))
    assert (data.tipo_de_rechazo, data.rechazos, data.descuento_previo, data.refusal_count) == (
        "banco", "varios", "no", 2)
    assert data.latest_failed_at == datetime(2026, 9, 30, 14, 45, tzinfo=UTC)
    assert asyncio.run(client.record_flow_engine_native_branch(
        action_id="action-att1", attempt_id="attempt-att1", worker_id="w", lease_generation=1, branch_index=3,
        now="2026-09-30T15:00:00+00:00")) == ("recorded", "maribel_pago_rechazado_banco_01")
    stale = _client({"record_flow_engine_native_branch_v1": httpx.Response(400, json={
        "code": "55000", "message": ROWS["record_stale"]["message"], "details": None})})
    with pytest.raises(SupabasePermanentError) as raised:
        asyncio.run(stale.record_flow_engine_native_branch(
            action_id="action-att1", attempt_id="attempt-att1", worker_id="w", lease_generation=1, branch_index=2,
            now="2026-09-30T15:00:00+00:00"))
    assert raised.value.reason == NATIVE_BRANCH_STALE
    # «varios» con un rechazo no es una fila de la base.
    odd = _client({"get_flow_engine_native_payment_failure_context_v1": [{**_data_row("dos"), "refusal_count": 1}]})
    with pytest.raises(SupabaseError, match="invalid_row"):
        asyncio.run(odd.get_flow_engine_native_payment_failure_context(
            action_id="action-att1", worker_id="w", lease_generation=1, now="2026-09-30T15:00:00+00:00"))


# -- La eleccion: la de choose_variant ----------------------------------------------------------------


def _att1_chain() -> tuple[FlowEngineNativeBranch, ...]:
    """Las cuatro de ATT1 en el orden del legado, como cadena (flow-engine-v2 §27)."""
    def branch(datum: str | None, value: str | None, variant: str) -> FlowEngineNativeBranch:
        return FlowEngineNativeBranch(datum=datum, values=(value,) if value else None, template_name=variant,
                                      template_language="es_MX", template_variables=("nombre", "producto"),
                                      template_button={"link": "checkout"})
    return (*(branch(*VARIANT_BY_NATIVE_DATUM[variant], variant) for variant in VARIANTS[:3]),
            branch(None, None, VARIANTS[3]))


@pytest.mark.parametrize(("kind", "count", "coupon"), list(product(
    ("declined", "insufficient_funds"), (1, 2, 3), (False, True))))
def test_the_native_chain_picks_what_the_legacy_picks(kind: str, count: int, coupon: bool) -> None:
    legacy = choose_variant(refusal_kind=kind, payment_failure_count=count,
                            other_coupon_code="ATT1CARRITO00001" if coupon else None)
    data = NativePaymentFailureData(
        tipo_de_rechazo="sin_saldo" if kind == "insufficient_funds" else "banco",
        rechazos="varios" if count >= 2 else "uno", descuento_previo="si" if coupon else "no",
        refusal_count=count, latest_failed_at=datetime(2026, 9, 30, 14, 45, tzinfo=UTC))
    chain = _att1_chain()
    assert chain[choose_native_branch(chain, data.value_of)].template_name == legacy


def test_the_data_measured_in_the_base_pick_their_branch() -> None:
    step = _parsed_step()
    for key, branch in (("saldo", "saldo"), ("banco", "banco"), ("dos", "dos_veces"), ("previo", "cupon")):
        data = NativePaymentFailureData(**{**_data_row(key), "latest_failed_at": datetime(2026, 9, 30, tzinfo=UTC)})
        picked = step.template_branches[choose_native_branch(step.template_branches, data.value_of)]
        assert picked.template_name == f"maribel_pago_rechazado_{branch}_01", key


# -- El despachador ------------------------------------------------------------------------------------


class _BranchAuthority(_NativeAuthority):
    """La accion del pago rechazado nativo de Maribel: el contexto con las ramas, los datos y el link."""

    def __init__(self, *, data: str = "banco", step: Any = None, data_error: Exception | None = None,
                 record_error: Exception | None = None, reserve_outcome: str = "reserved") -> None:
        super().__init__(step=step if step is not None else _parsed_step())
        self.data = NativePaymentFailureData(**{**_data_row(data),
                                                "latest_failed_at": datetime(2026, 9, 30, 14, 45, tzinfo=UTC)})
        self.data_error = data_error
        self.record_error = record_error
        self.reserve_outcome = reserve_outcome
        self.records: list[int] = []
        self.issuance_finalizations: list[dict[str, object]] = []

    async def get_flow_engine_native_payment_failure_context(self, **kwargs: object) -> NativePaymentFailureData:
        assert kwargs["action_id"] == self.action.action_id
        assert kwargs["lease_generation"] == self.action.lease_generation
        self.events.append("branch_data")
        if self.data_error is not None:
            raise self.data_error
        return self.data

    async def record_flow_engine_native_branch(self, **kwargs: object) -> tuple[str, str]:
        assert kwargs["attempt_id"] == self.attempt.attempt_id
        self.events.append("branch_record")
        self.records.append(int(kwargs["branch_index"]))  # type: ignore[call-overload]
        if self.record_error is not None:
            raise self.record_error
        branch = self.step.template_branches[int(kwargs["branch_index"])]  # type: ignore[call-overload]
        return "recorded", branch.template_name

    async def reserve_recovery_checkout_issuance(self, **kwargs: object) -> RecoveryCheckoutReservation:
        assert kwargs["attempt_id"] == self.attempt.attempt_id
        self.events.append("reserve_link")
        if self.reserve_outcome != "reserved":
            return RecoveryCheckoutReservation(self.reserve_outcome, None, None, None)
        return RecoveryCheckoutReservation("reserved", ISSUANCE_ID, LINK, "hermes~v1~01JABCDEFGHJKMNPQRSTVWXYZ2")

    async def authorize_recovery_checkout_issuance(self, **kwargs: object) -> RecoveryCheckoutAuthorization:
        assert kwargs["issuance_id"] == ISSUANCE_ID
        self.events.append("authorize_link")
        return RecoveryCheckoutAuthorization("request_started", ISSUANCE_ID, "request_started")

    async def finalize_chatwoot_checkout_issuance_v2(self, **kwargs: object) -> object:
        self.issuance_finalizations.append(kwargs)
        return object()


def _chatwoot() -> _NativeChatwoot:
    chatwoot = _NativeChatwoot()
    chatwoot.catalog.extend(copy.deepcopy(list(TEMPLATES.values())))
    return chatwoot


def _body(name: str) -> str:
    [body] = [c["text"] for c in TEMPLATES[name]["components"] if c["type"] == "BODY"]
    return body.replace("{{1}}", NAME).replace("{{2}}", PRODUCT)


@pytest.mark.parametrize(("data", "branch"), [
    ("banco", "banco"), ("saldo", "saldo"), ("dos", "dos_veces"), ("previo", "cupon")])
def test_each_branch_goes_out_with_our_link_and_the_fixed_coupon(tmp_path: Path, data: str, branch: str) -> None:
    authority = _BranchAuthority(data=data)
    chatwoot = _chatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    name = f"maribel_pago_rechazado_{branch}_01"
    [message] = _templates(chatwoot)
    assert message["content"] == _body(name)
    assert message["template_params"] == {
        "name": name, "category": "MARKETING", "language": "es_MX",
        "processed_params": {"body": {"1": NAME, "2": PRODUCT},
                             "buttons": [{"type": "url", "parameter": f"{BARE}&offDiscount=PROTOCOLO10"}]},
    }
    # Los datos, la eleccion registrada antes del envio, el link reservado y autorizado justo antes.
    # (el arranque es el del motor: engine_calls)
    assert [event for event in authority.events if event != "reevaluate"] == [
        "reserve", "branch_data", "branch_record", "reserve_link", "authorize_link", "accepted"]
    assert authority.engine_calls[:2] == ["context", "request_started"]
    assert authority.records == [BRANCHES.index(branch)]
    assert authority.issuance_finalizations[0]["status"] == "accepted_by_chatwoot"


@pytest.mark.parametrize(("error", "reason", "retry"), [
    (SupabaseError("flow_engine_native_payment_failure_context_failed: HTTP 404"), NATIVE_BRANCH_DATA_UNAVAILABLE, True),
    (SupabasePermanentError("rejected", reason="payment_failure_details_missing"), "payment_failure_details_missing",
     False),
])
def test_without_the_data_nothing_goes_out_and_no_branch_is_guessed(tmp_path: Path, error: Exception, reason: str,
                                                                   retry: bool) -> None:
    authority = _BranchAuthority(data_error=error)
    chatwoot = _chatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    assert _templates(chatwoot) == []
    assert "branch_record" not in authority.events and "reserve_link" not in authority.events
    [closed] = authority.finalizations
    assert closed["reason_code"] == reason
    assert (closed["next_attempt_at"] is not None) is retry


@pytest.mark.parametrize(("reason", "retry"), [
    (NATIVE_BRANCH_STALE, True), ("flow_engine_native_branch_conflict", False)])
def test_a_refused_record_closes_and_only_a_stale_pick_is_read_again(tmp_path: Path, reason: str, retry: bool) -> None:
    authority = _BranchAuthority(record_error=SupabasePermanentError("rejected", reason=reason))
    chatwoot = _chatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    assert _templates(chatwoot) == []
    assert "reserve_link" not in authority.events
    [closed] = authority.finalizations
    assert closed["reason_code"] == reason
    assert (closed["next_attempt_at"] is not None) is retry


@pytest.mark.parametrize("outcome", ["purchase_already_approved", "blocked_case", "missing_offer"])
def test_without_our_link_the_branch_does_not_go_out(tmp_path: Path, outcome: str) -> None:
    authority = _BranchAuthority(reserve_outcome=outcome)
    chatwoot = _chatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    assert _templates(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == f"recovery_checkout_{outcome}"
    assert closed["next_attempt_at"] is None


def test_a_single_step_with_the_checkout_button_goes_out_with_our_link(tmp_path: Path) -> None:
    # D5: el boton del checkout en un paso nativo del pago rechazado, sin la cadena.
    step = replace(_parsed_step(), template_branches=None)
    authority = _BranchAuthority(step=step)
    chatwoot = _chatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    [message] = _templates(chatwoot)
    assert message["template_params"]["name"] == "maribel_pago_rechazado_banco_01"
    assert message["template_params"]["processed_params"]["buttons"] == [
        {"type": "url", "parameter": f"{BARE}&offDiscount=PROTOCOLO10"}]
    assert "branch_data" not in authority.events and authority.records == []


@pytest.mark.parametrize("button", [
    {"link": "checkout", "cupon": "personal"}, {"link": "ficha"}, {"link": "checkout", "cupon": "PROTOCOLO10+"}])
def test_a_button_the_bridge_cannot_build_closes_for_good(tmp_path: Path, button: dict[str, str]) -> None:
    step = replace(_parsed_step(), template_branches=None, template_button=button)
    authority = _BranchAuthority(step=step)
    chatwoot = _chatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    assert _templates(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == FLOW_ENGINE_STEP_UNSUPPORTED
    assert "reserve_link" not in authority.events


def test_the_native_step_of_today_goes_out_as_today(tmp_path: Path) -> None:
    # Sin ramas ni boton (la bienvenida, el carrito, el pago rechazado de un paso): ni datos, ni registro, ni link.
    authority = _BranchAuthority(step=_step())
    chatwoot = _chatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    assert len(_templates(chatwoot)) == 1
    assert not {"branch_data", "branch_record", "reserve_link", "authorize_link"} & set(authority.events)


# -- Lo que ve el AE (A6) -------------------------------------------------------------------------------


def test_the_ae_reads_the_sales_of_each_branch() -> None:
    from bridge.automation_expert_read import attach_sales

    flows: list[dict[str, Any]] = [{"flow_key": "pago_rechazado", "native": True}]
    raw = {"since": "2026-10-26T00:00:00Z", "until": "2026-11-02T00:00:00Z", "rule": "purchase_after_accepted_send_v1",
           "identity_window_days": 7, "flows": [{
               "flow_key": "pago_rechazado", "runs_started": 1, "purchases_after_send": 1, "by_case": 0,
               "by_identity": 1, "by_link": 0, "people_after_send": 1, "purchases_before_send": 0,
               "by_step": {"first_contact": 1}, "by_branch": {**ROWS["sales_by_branch"], "NO VALE": {"sends": 1}}}]}
    assert attach_sales(flows, raw) == []
    assert flows[0]["sales"]["by_branch"] == {"maribel_pago_rechazado_banco_01": {"sends": 1, "purchases": 1}}
    # Una base sin la migracion no trae by_branch: vacio.
    del raw["flows"][0]["by_branch"]
    attach_sales(flows, raw)
    assert flows[0]["sales"]["by_branch"] == {}


def test_the_library_says_how_the_ae_writes_the_branches() -> None:
    profile = Path(__file__).resolve().parents[1] / "profiles" / "automation-expert"
    page = (profile / "skills" / "plataformas" / "hotmart" / "SKILL.md").read_text(encoding="utf-8")
    for needed in ("descuento_previo", "rechazos", "tipo_de_rechazo", "si_no", "boton: {link: checkout, cupon: PROTOCOLO10}",
                   "by_branch", "maribel_pago_rechazado_banco_01"):
        assert needed in page, needed
    soul = (profile / "SOUL.md").read_text(encoding="utf-8")
    crear = (profile / "skills" / "flujos" / "crear-un-flujo" / "SKILL.md").read_text(encoding="utf-8")
    assert "sin condiciones ni ramas (salvo el pago rechazado)" in crear
    assert len(soul.strip()) <= 20_000
