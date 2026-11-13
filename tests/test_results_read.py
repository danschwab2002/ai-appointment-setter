"""resultados/v1 (E-507): la ruta, la ventana, la forma de la respuesta y el costo de Meta.

La base se simula con la salida real de read_results_v1 sobre la cadena completa de
migraciones en PGlite (validate_automation_expert_native_reads.mjs --emit-fixture,
tests/fixtures/automation_expert_native/rpc_read_results.json). Meta se simula con la
respuesta capturada de pricing_analytics de la cuenta de Maribel del 2026-11-12
(tests/fixtures/meta_pricing_analytics_maribel_20261112.json).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge import results_read
from bridge.app import Settings, create_app
from bridge.instance_manifest import InstanceManifest

FIXTURES = Path(__file__).parent / "fixtures"
ATT1 = FIXTURES / "instances" / "att1"
TOKEN = "e" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ROUTE = "/internal/automation-expert/v1/resultados"
NOW = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)


def _rpc() -> dict[str, object]:
    return json.loads((FIXTURES / "automation_expert_native" / "rpc_read_results.json").read_text())["value"]


def _pricing() -> dict[str, object]:
    return json.loads((FIXTURES / "meta_pricing_analytics_maribel_20261112.json").read_text())


class FakeReader:
    """La base: contesta la RPC con la salida capturada y guarda lo que le pidieron."""

    def __init__(self, value: object | None = None, status_code: int = 200) -> None:
        self.value = _rpc() if value is None else value
        self.status_code = status_code
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    async def _request(self, method: str, path: str, *, params: object = None, content: str | None = None,
                       prefer: str | None = None) -> httpx.Response:
        self.calls.append((method, path, json.loads(content or "{}")))
        return httpx.Response(self.status_code, json=self.value)


def _settings(**overrides: object) -> Settings:
    manifest = InstanceManifest.from_toml_file(ATT1 / "instancia.toml")
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=Path("/tmp/automation-expert-captures"),
        max_age_seconds=300,
        commercial_ally_config=manifest.to_commercial_ally_config(),
        commercial_ally_manifest_path=Path("/instancia/instancia.toml"),
        instance_manifest=manifest,
        hermes_model_name=manifest.agent_model_name,
        agent_bot_id=2,
        chatwoot_base_url="https://chatwoot.example.org",
        agent_provenance_scope_ref="att1-agent-bot",
        pilot_boundary_enabled=False,
        automation_expert_read_enabled=True,
        automation_expert_read_token=TOKEN,
    )
    return replace(settings, **overrides)


def _client(monkeypatch: pytest.MonkeyPatch, reader: FakeReader, *, enabled: bool = True,
            prefixes: str = "", **overrides: object) -> TestClient:
    monkeypatch.setenv(results_read.ENV_ENABLED, "true" if enabled else "false")
    monkeypatch.setenv(results_read.ENV_TEST_PREFIXES, prefixes)
    return TestClient(create_app(_settings(**overrides), supabase_client=reader))  # type: ignore[arg-type]


# --------------------------------------------------------------- el flag y la clave


def test_the_route_does_not_exist_with_the_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader()
    client = _client(monkeypatch, reader, enabled=False)
    assert client.get(ROUTE, headers=AUTH).status_code == 404
    assert reader.calls == []


def test_the_route_does_not_exist_without_the_automation_expert_read(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader()
    client = _client(monkeypatch, reader, automation_expert_read_enabled=False, automation_expert_read_token=None)
    assert client.get(ROUTE, headers=AUTH).status_code == 404


def test_it_asks_for_the_automation_expert_read_token(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader()
    client = _client(monkeypatch, reader)
    assert client.get(ROUTE).status_code == 401
    assert client.get(ROUTE, headers={"Authorization": "Bearer " + "x" * 40}).status_code == 401
    assert reader.calls == []


def test_a_bad_test_prefix_does_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match=results_read.ENV_TEST_PREFIXES):
        _client(monkeypatch, FakeReader(), prefixes="f9fb51bc,x' or 1=1")


# --------------------------------------------------------------- la respuesta


def test_it_answers_the_numbers_of_the_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader()
    client = _client(monkeypatch, reader, prefixes="f9fb51bc,3de94ff9")
    response = client.get(ROUTE, headers=AUTH, params={"desde": "2026-11-01", "hasta": "2026-11-11"})
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "resultados/v1"
    assert body["zona"] == "America/Mexico_City"
    # desde = las 00:00 de CDMX del 01/11; hasta incluye el 11/11 entero.
    assert body["desde"] == "2026-11-01T06:00:00Z"
    assert body["hasta"] == "2026-11-12T06:00:00Z"
    resumen = {row["metrica"]: row["valor"] for row in body["resumen"]}
    assert resumen["ventas_recuperadas"] == 1
    assert resumen["ventas_atribuibles"] == 1 and resumen["ventas_directas"] == 0
    assert resumen["neto_productor_usd"] == resumen["neto_atribuibles_usd"] == 39.08
    venta = body["ventas"][0]
    assert venta["via"] == "identidad" and venta["tipo"] == "atribuible" and len(venta["venta"]) == 6
    flujo = next(row for row in body["flujos"] if row["flujo"] == "bienvenida")
    assert flujo["ventas_atribuibles"] == 1 and flujo["neto_atribuibles_usd"] == 39.08
    assert "prueba" not in {row["via"] for row in body["compras"]}
    # Sin la cuenta de Meta: el costo va en faltan, igual que lo que no vive en la base.
    assert body["faltan"] == ["conversaciones", "costo_modelo", "costo_meta"]
    assert resumen["costo_meta_usd"] is None
    # Lo que se le pidio a la base: la RPC, la ventana, la zona, las pruebas y la vara.
    method, path, payload = reader.calls[0]
    assert (method, path) == ("POST", "/rest/v1/rpc/read_results_v1")
    assert payload == {"p_desde": "2026-11-01T06:00:00Z", "p_hasta": "2026-11-12T06:00:00Z",
                       "p_tz": "America/Mexico_City", "p_pruebas": ["f9fb51bc", "3de94ff9"],
                       "p_ventana_dias": 7, "p_piso_min": 15}


def test_a_bad_window_is_422_and_does_not_reach_the_base(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader()
    client = _client(monkeypatch, reader)
    for params, code in (({"desde": "01/11/2026"}, "desde_invalid"), ({"hasta": "2026-13-01"}, "hasta_invalid"),
                         ({"desde": "2026-11-10", "hasta": "2026-11-01"}, "window_empty"),
                         ({"desde": "2024-01-01", "hasta": "2026-11-01"}, "window_too_long")):
        response = client.get(ROUTE, headers=AUTH, params=params)
        assert response.status_code == 422 and response.json()["detail"] == code
    assert reader.calls == []


def test_a_base_failure_is_503_without_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader(value={"message": "secreto"}, status_code=500)
    client = _client(monkeypatch, reader)
    response = client.get(ROUTE, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["detail"] == "automation_expert_read_unavailable"
    assert "secreto" not in response.text


def test_nothing_of_a_person_comes_out() -> None:
    raw = _rpc()
    raw["ventas"][0]["email"] = "ana@example.test"  # una clave de mas no pasa
    body = results_read.build_body(raw, instancia="att1", zona="America/Mexico_City", desde=NOW, hasta=NOW,
                                   generado=NOW, costo_meta=None)
    assert "example.test" not in json.dumps(body)


def test_the_partition_matches_the_flows_of_the_dashboard() -> None:
    raw = {
        "flujos": [{"flujo": "carrito", "ejecutor": "legado", "aceptados": 9, "ventas": 3, "por_link": 1,
                    "por_caso": 1, "por_identidad": 1}],
        "ventas": [
            {"venta": "aaaaaa", "fecha": "2026-11-02", "flujo": "carrito", "ejecutor": "legado", "via": "link",
             "neto_usd": 10.0},
            {"venta": "bbbbbb", "fecha": "2026-11-03", "flujo": "carrito", "ejecutor": "legado", "via": "caso",
             "neto_usd": 20.5},
            {"venta": "cccccc", "fecha": "2026-11-04", "flujo": "carrito", "ejecutor": "legado", "via": "identidad",
             "neto_usd": None},
        ],
        "compras": [{"via": "link", "n": 1}, {"via": "prueba", "n": 4}, {"via": "no_tocada", "n": 7}],
    }
    body = results_read.build_body(raw, instancia="att1", zona="America/Mexico_City", desde=NOW, hasta=NOW,
                                   generado=NOW, costo_meta=[{"fecha": "2026-11-02", "costo_usd": 0.5}])
    flujo = body["flujos"][0]
    assert (flujo["envios"], flujo["ventas_directas"], flujo["ventas_atribuibles"]) == (9, 1, 2)
    assert flujo["neto_directas_usd"] == 10.0 and flujo["neto_atribuibles_usd"] == 20.5
    resumen = {row["metrica"]: row["valor"] for row in body["resumen"]}
    assert resumen["compras_aprobadas"] == 8 and resumen["costo_meta_usd"] == 0.5
    # Una venta sin la comision en USD se dice, no se cuenta como cero en silencio.
    assert "monto_de_alguna_venta" in body["faltan"] and "costo_meta" not in body["faltan"]


# --------------------------------------------------------------- la ventana


def test_the_window_is_in_the_time_zone_of_the_instance() -> None:
    desde, hasta = results_read.parse_window("2026-11-05", "2026-11-05", tz="America/Mexico_City", now=NOW)
    assert (desde.isoformat(), hasta.isoformat()) == ("2026-11-05T06:00:00+00:00", "2026-11-06T06:00:00+00:00")
    # hasta en el futuro se recorta a ahora; sin desde, los ultimos 30 dias.
    desde, hasta = results_read.parse_window(None, "2026-12-01", tz="America/Mexico_City", now=NOW)
    assert hasta == NOW and (hasta - desde).days == 30


# --------------------------------------------------------------- Meta


def test_the_meta_cost_per_day_from_the_captured_answer() -> None:
    captured = _pricing()
    assert results_read.costo_por_dia(captured["response"], "America/Mexico_City") \
        == captured["_capture"]["esperado_por_dia_cdmx"]


def test_meta_is_asked_once_per_hour_and_window() -> None:
    captured = _pricing()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=captured["response"])

    clock = [1000.0]
    pricing = results_read.MetaPricing(access_token="t" * 30, waba_id="100000000000001",
                                       transport=httpx.MockTransport(handler), clock=lambda: clock[0])
    desde, hasta = datetime(2026, 11, 9, 6, tzinfo=UTC), datetime(2026, 11, 12, 6, tzinfo=UTC)
    asyncio.run(pricing.pricing_analytics(desde, hasta))
    asyncio.run(pricing.pricing_analytics(desde, hasta))
    assert len(seen) == 1
    clock[0] += results_read.META_CACHE_SECONDS + 1
    asyncio.run(pricing.pricing_analytics(desde, hasta))
    assert len(seen) == 2
    request = seen[0]
    assert request.url.path == "/v23.0/100000000000001"
    assert f"pricing_analytics.start({int(desde.timestamp())}).end({int(hasta.timestamp())})" \
        in request.url.params["fields"]
    assert request.headers["Authorization"] == "Bearer " + "t" * 30
    assert "t" * 30 not in repr(pricing)


def _app_without_the_route(reader: FakeReader):  # type: ignore[no-untyped-def]
    """La app real, sin la ruta que registro create_app: la prueba la vuelve a registrar con un
    cliente de Meta de transporte simulado."""
    app = create_app(_settings(), supabase_client=reader)  # type: ignore[arg-type]
    app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) != ROUTE]
    return app


def test_with_meta_the_cost_is_in_the_answer() -> None:
    captured = _pricing()
    reader = FakeReader()
    app = _app_without_the_route(reader)
    pricing = results_read.MetaPricing(
        access_token="t" * 30, waba_id="100000000000001",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=captured["response"])))
    results_read.register(app, supabase=reader, instance_ref="att1", time_zone="America/Mexico_City",
                          require_token=lambda authorization: None, busy=lambda: None,
                          slots=asyncio.Semaphore(2), failure=lambda exc: RuntimeError(),  # type: ignore[arg-type,return-value]
                          meta_access_token=None, meta_waba_id=None, meta=pricing,
                          env={results_read.ENV_ENABLED: "true"}, now=lambda: datetime(2026, 11, 12, 6, tzinfo=UTC))
    body = TestClient(app).get(ROUTE, params={"desde": "2026-11-09", "hasta": "2026-11-11"}).json()
    assert body["costo_meta"] == captured["_capture"]["esperado_por_dia_cdmx"]
    assert {row["metrica"]: row["valor"] for row in body["resumen"]}["costo_meta_usd"] == round(0.689 + 0.322 + 0.5756, 2)
    assert body["faltan"] == ["conversaciones", "costo_modelo"]


def test_a_meta_failure_leaves_the_rest_of_the_answer() -> None:
    reader = FakeReader()
    app = _app_without_the_route(reader)
    pricing = results_read.MetaPricing(
        access_token="t" * 30, waba_id="100000000000001",
        transport=httpx.MockTransport(lambda request: httpx.Response(500, json={"error": {"message": "x"}})))
    results_read.register(app, supabase=reader, instance_ref="att1", time_zone="America/Mexico_City",
                          require_token=lambda authorization: None, busy=lambda: None,
                          slots=asyncio.Semaphore(2), failure=lambda exc: RuntimeError(),  # type: ignore[arg-type,return-value]
                          meta_access_token=None, meta_waba_id=None, meta=pricing,
                          env={results_read.ENV_ENABLED: "true"}, now=lambda: NOW)
    body = TestClient(app).get(ROUTE).json()
    assert body["ventas"] and "costo_meta" in body["faltan"]
