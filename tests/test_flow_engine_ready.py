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
