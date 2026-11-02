"""/ready con flow_engine (la T2.4, commit 6).

Contrato: ``docs/contracts/flow-engine-v1.md``, seccion 8. Decisiones D1 y D6:
ninguna clave del motor da 503, y /ready no hace ninguna llamada nueva a la
base (sale de la cache del lote y de la ultima lectura de divergencias).

Sobre la app de ATT1 de ``test_flow_engine_bridge_settings`` (el carrito con
sus tres toques) y la base falsa de A1 (``test_cart_discount_first_touch``),
con la lectura de rutas que da la base real (``bridge_rpc_rows.json``). Sin
lifespan: los workers no arrancan, solo se consulta /ready.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from test_cart_discount_first_touch import _Chatwoot, _CartAuthority
from test_flow_engine_bridge_settings import engine_settings

from bridge.app import create_app
from bridge.supabase import FlowEngineRoute, SupabaseError

ROOT = Path(__file__).resolve().parents[1]
ROWS = json.loads((ROOT / "tests" / "fixtures" / "flow_engine" / "bridge_rpc_rows.json").read_text())
FLOW_KEYS = {
    "flow_engine", "flow_engine_routes_motor", "flow_engine_routes_legado", "flow_engine_read",
    "flow_engine_divergences_unnotified", "flow_engine_alerts",
    # flow-engine-v2 (E-065): los flujos nativos, los rechazos de MOT-002 y la
    # cola de la compra nativa. Todas en memoria: /ready no llama a la base.
    "flow_engine_native_read", "flow_engine_native_on", "flow_engine_start_rejections_unnotified",
    "flow_engine_native_queue_pending",
    # E-165 (flow-engine-v2 2.8.0): si el bridge corre A1 con cupon y A2/Z, lo que pide una ruta
    # del carrito en motor. El de esta prueba los corre: enabled.
    "flow_engine_cart_touches",
    # E-252 (flow-engine-v2 2.13.0): si el formulario entra por la RPC que lo rutea al nativo. El de
    # esta prueba no: off.
    "flow_engine_native_intent",
    # E-269 (flow-engine-v2 2.14.0): el barrido nativo del seguimiento a 24 h, de lo que ya leyo el
    # refresco (en memoria: /ready no llama a la base).
    "flow_engine_native_silence_read", "flow_engine_native_silence_running", "flow_engine_native_silence_cohort",
    "flow_engine_native_silence_scan", "flow_engine_native_silence_last_scan",
    "flow_engine_native_silence_refused",
}


class _ReadyAuthority(_CartAuthority):
    def __init__(self, *, executor: str = "legado", routes_fail: bool = False) -> None:
        super().__init__()
        self.executor = executor
        self.routes_fail = routes_fail
        self.calls: list[str] = []

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    async def get_pilot_runtime_status(self, *, pilot_boundary: object) -> object:
        self.calls.append("pilot_status")
        return SimpleNamespace(
            configured=True, runtime_state="armed", runtime_generation=3, reason_code="pilot_runtime_armed",
        )

    async def get_human_handoff_projection_status(self) -> object:
        return SimpleNamespace(pending_count=0, retryable_count=0, delivery_unknown_count=0,
                               conflict_count=0, dead_letter_count=0)

    async def read_flow_engine_routes(self, instance: object) -> tuple[FlowEngineRoute, ...]:
        self.calls.append("routes")
        if self.routes_fail:
            raise SupabaseError("read_flow_engine_routes_v1_failed: HTTP 503")
        [row] = ROWS["reads"]["routes"]["routes"]
        return (FlowEngineRoute(
            product_ref=row["product_ref"], flow_key=row["flow_key"], executor=self.executor,
            legacy_kind=row["legacy_kind"], legacy_scope_key=row["legacy_scope_key"],
            generation=row["generation"], flow_state=row["flow_state"],
        ),)

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("read_flow_engine", "mark_flow_engine", "admit_flow_engine")):
            raise AssertionError(f"/ready no llama a {name}")
        raise AttributeError(name)


def _app(tmp_path: Path, authority: _ReadyAuthority, **overrides: Any) -> Any:
    return create_app(
        engine_settings(tmp_path, **overrides),
        supabase_client=authority,  # type: ignore[arg-type]
        chatwoot_client=_Chatwoot().client(),
    )


def _ready(app: Any) -> httpx.Response:
    async def get() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/ready")

    return asyncio.run(get())


def test_off_the_payload_has_no_flow_engine_key(tmp_path: Path) -> None:
    authority = _ReadyAuthority()
    response = _ready(_app(tmp_path, authority, flow_engine_enabled=False))

    assert response.status_code == 200
    assert not any(key.startswith("flow_engine") for key in response.json())
    assert "routes" not in authority.calls


def test_on_before_the_first_read_it_is_pending(tmp_path: Path) -> None:
    authority = _ReadyAuthority()
    response = _ready(_app(tmp_path, authority))

    assert response.status_code == 200
    payload = response.json()
    assert {key for key in payload if key.startswith("flow_engine")} == FLOW_KEYS
    assert payload["flow_engine"] == "enabled"
    assert payload["flow_engine_read"] == "pending"
    assert payload["flow_engine_divergences_unnotified"] == "unknown"
    assert payload["flow_engine_alerts"] == "sin_slack"
    assert payload["flow_engine_native_read"] == "pending"
    assert payload["flow_engine_native_on"] == "0"
    assert payload["flow_engine_start_rejections_unnotified"] == "unknown"
    assert payload["flow_engine_native_queue_pending"] == "unknown"
    assert payload["flow_engine_cart_touches"] == "enabled"
    assert payload["flow_engine_native_intent"] == "off"
    assert payload["flow_engine_native_silence_read"] == "pending"
    assert payload["flow_engine_native_silence_running"] == "0"
    assert payload["flow_engine_native_silence_scan"] == "none"
    # /ready no lee las rutas: eso es del lote.
    assert "routes" not in authority.calls


def test_on_with_zero_routes_in_motor(tmp_path: Path) -> None:
    authority = _ReadyAuthority(executor="legado")
    app = _app(tmp_path, authority)
    asyncio.run(app.state.flow_engine.routes.refresh_if_due())
    calls = len(authority.calls)
    response = _ready(app)

    assert response.status_code == 200
    payload = response.json()
    assert payload["flow_engine_routes_motor"] == "0"
    assert payload["flow_engine_routes_legado"] == "1"
    assert payload["flow_engine_read"] == "ok"
    assert all(isinstance(payload[key], str) for key in FLOW_KEYS)
    assert authority.calls[calls:] == ["pilot_status"]


def test_on_with_the_read_down_it_is_still_200(tmp_path: Path) -> None:
    authority = _ReadyAuthority(routes_fail=True)
    app = _app(tmp_path, authority)
    asyncio.run(app.state.flow_engine.routes.refresh_if_due())
    response = _ready(app)

    assert response.status_code == 200
    assert response.json()["flow_engine_read"] == "failing"
    assert response.json()["flow_engine_routes_motor"] == "0"


def test_the_route_in_motor_is_counted(tmp_path: Path) -> None:
    authority = _ReadyAuthority(executor="motor")
    app = _app(tmp_path, authority)
    asyncio.run(app.state.flow_engine.routes.refresh_if_due())
    payload = _ready(app).json()
    assert payload["flow_engine_routes_motor"] == "1"
    assert payload["flow_engine_routes_legado"] == "0"


def test_the_native_silence_sweep_is_built_by_the_engine_with_the_manifest_options(tmp_path: Path) -> None:
    # E-269: lo arma FLOW_ENGINE_ENABLED (no CONVERSATION_FOLLOWUP_ENABLED, que aca esta apagado), con
    # el producto del manifiesto de ATT1 y lo demas de la definicion publicada.
    from test_flow_engine_native_silence_sweep import _changed, _silence

    from bridge.app import allowed_phone_from_jid

    from bridge.followup_discount import ConversationFollowupSweeper

    class _WithSilence(_ReadyAuthority):
        async def read_flow_engine_native_silences(self, instance: object) -> tuple[Any, ...]:
            self.calls.append("silences")
            assert instance == {"tenant_ref": "lancemos", "ally_ref": "att1"}
            definition = _changed(("dispara", "silencio", "desde"), "30h")
            definition["dispara"]["silencio"]["conversaciones"] = ["abierta"]
            return (_silence(definition),)

    off = _app(tmp_path, _ReadyAuthority(), flow_engine_enabled=False)
    assert off.state.native_silence_sweepers is None
    authority = _WithSilence()
    app = _app(tmp_path, authority)
    sweepers = app.state.native_silence_sweepers
    assert app.state.conversation_followup_sweeper is None
    assert sweepers is not None and sweepers.sweepers == ()

    async def refresh_and_stop() -> ConversationFollowupSweeper:
        await sweepers.refresh_once()
        [sweeper] = sweepers.sweepers
        await sweepers.stop()
        return sweeper

    sweeper = asyncio.run(refresh_and_stop())
    assert sweeper.native_run is not None and sweeper.native_run.version == 1
    assert sweeper.only_conversation_ids == frozenset({21})
    assert sweeper._product_name == "Alimenta tu Tiroides"
    assert (sweeper._template_name, sweeper._coupon_code) == ("att1_seguimiento_descuento_01", "TIROIDES10")
    assert sweeper._phone_equivalence is True and sweeper._refuse_unsafe_greeting is True
    assert sweeper._external_user_id_resolver is not None
    assert sweeper._log_prefix == "flow_engine_silence"
    assert sweeper.send_hours is None and sweeper._send_window is not None
    # La ventana y las conversaciones de la definicion, y el alcance del legado (ALLOWED_WHATSAPP_JID).
    assert (sweeper._min_inbound_age_seconds, sweeper._max_inbound_age_seconds) == (108_000, 259_200)
    assert sweeper._conversation_statuses == ("open",)
    expected_scope = engine_settings(tmp_path)
    assert sweeper._allowed_phone == (
        None if expected_scope.chatwoot_scoped_inbound_senders_enabled
        else allowed_phone_from_jid(expected_scope.allowed_jid)
    )
    reads = authority.calls.count("silences")
    payload = _ready(app).json()
    # /ready no lee los silencios: sale de lo que el refresco ya leyo.
    assert authority.calls.count("silences") == reads == 1
    assert payload["flow_engine_native_silence_read"] == "ok"
