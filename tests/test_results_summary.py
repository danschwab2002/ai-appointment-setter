"""El resumen de resultados para el Automation Expert (E-573, automation-expert-results-v1).

La base se simula con la salida real de read_results_v1 sobre la cadena completa de
migraciones en PGlite (validate_automation_expert_native_reads.mjs --emit-fixture,
tests/fixtures/automation_expert_native/rpc_read_results.json), la misma que usa
test_results_read.py. Meta, con la respuesta capturada de pricing_analytics de la cuenta
de Maribel del 2026-11-12 (tests/fixtures/meta_pricing_analytics_maribel_20261112.json).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge import results_read, results_summary
from bridge.app import Settings, create_app
from bridge.instance_manifest import InstanceManifest

FIXTURES = Path(__file__).parent / "fixtures"
ATT1 = FIXTURES / "instances" / "att1"
TOKEN = "e" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ROUTE = "/internal/automation-expert/v1/resultados/resumen"
BASE_ROUTE = "/internal/automation-expert/v1/resultados"
# 12:00 de CDMX del 12/11.
NOW = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
TZ = "America/Mexico_City"


def _rpc() -> dict[str, object]:
    return json.loads((FIXTURES / "automation_expert_native" / "rpc_read_results.json").read_text())["value"]


def _pricing() -> dict[str, object]:
    return json.loads((FIXTURES / "meta_pricing_analytics_maribel_20261112.json").read_text())


class FakeReader:
    """La base: contesta la RPC con la salida capturada; ``fail_from`` hace fallar del pedido N en adelante."""

    def __init__(self, value: object | None = None, status_code: int = 200, fail_from: int | None = None,
                 values: list[object] | None = None) -> None:
        self.value = _rpc() if value is None else value
        self.values = values
        self.status_code = status_code
        self.fail_from = fail_from
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    async def _request(self, method: str, path: str, *, params: object = None, content: str | None = None,
                       prefer: str | None = None) -> httpx.Response:
        self.calls.append((method, path, json.loads(content or "{}")))
        if self.fail_from is not None and len(self.calls) >= self.fail_from:
            return httpx.Response(500, json={"message": "secreto"})
        if self.values is not None:
            return httpx.Response(self.status_code, json=self.values[len(self.calls) - 1])
        return httpx.Response(self.status_code, json=self.value)


def _sin_ventas() -> dict[str, object]:
    raw = _rpc()
    raw["ventas"] = []
    raw["compras"] = []
    raw["flujos"] = []
    return raw


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


def _client(monkeypatch: pytest.MonkeyPatch, reader: FakeReader, *, results: bool = True, summary: bool = True,
            prefixes: str = "") -> TestClient:
    monkeypatch.setenv(results_read.ENV_ENABLED, "true" if results else "false")
    monkeypatch.setenv(results_read.ENV_TEST_PREFIXES, prefixes)
    monkeypatch.setenv(results_summary.ENV_ENABLED, "true" if summary else "false")
    return TestClient(create_app(_settings(), supabase_client=reader))  # type: ignore[arg-type]


def _registered(reader: FakeReader, *, meta: results_read.MetaPricing | None = None,
                env: dict[str, str] | None = None) -> TestClient:
    """La app real sin las dos rutas que registro create_app, vueltas a registrar con un reloj fijo
    y, si hace falta, un cliente de Meta de transporte simulado."""
    app = create_app(_settings(), supabase_client=reader)  # type: ignore[arg-type]
    app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) not in (ROUTE, BASE_ROUTE)]
    results_read.register(
        app, supabase=reader, instance_ref="att1", time_zone=TZ,
        require_token=lambda authorization: None, busy=lambda: None, slots=asyncio.Semaphore(2),
        failure=lambda exc: RuntimeError(),  # type: ignore[arg-type,return-value]
        meta_access_token=None, meta_waba_id=None, meta=meta,
        env=env if env is not None else {results_read.ENV_ENABLED: "true", results_summary.ENV_ENABLED: "true"},
        now=lambda: NOW)
    return TestClient(app)


# --------------------------------------------------------------- los flags y la clave


def test_the_route_does_not_exist_with_its_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader()
    client = _client(monkeypatch, reader, summary=False)
    assert client.get(ROUTE, headers=AUTH).status_code == 404
    assert reader.calls == []


def test_the_route_does_not_exist_with_the_results_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Las pruebas que no cuentan las declara la configuracion de /resultados: sin ella, nada.
    reader = FakeReader()
    client = _client(monkeypatch, reader, results=False, summary=True)
    assert client.get(ROUTE, headers=AUTH).status_code == 404
    assert client.get(BASE_ROUTE, headers=AUTH).status_code == 404
    assert reader.calls == []


def test_the_summary_flag_does_not_change_the_results_route(monkeypatch: pytest.MonkeyPatch) -> None:
    params = {"desde": "2026-11-01", "hasta": "2026-11-11"}
    off = _client(monkeypatch, FakeReader(), summary=False).get(BASE_ROUTE, headers=AUTH, params=params).json()
    on = _client(monkeypatch, FakeReader(), summary=True).get(BASE_ROUTE, headers=AUTH, params=params).json()
    off.pop("generado")
    on.pop("generado")
    assert on == off


def test_it_asks_for_the_automation_expert_read_token(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader()
    client = _client(monkeypatch, reader)
    assert client.get(ROUTE).status_code == 401
    assert client.get(ROUTE, headers={"Authorization": "Bearer " + "x" * 40}).status_code == 401
    assert reader.calls == []


# --------------------------------------------------------------- la respuesta


def test_by_default_the_last_seven_days_and_the_seven_before() -> None:
    reader = FakeReader()
    response = _registered(reader).get(ROUTE)
    assert response.status_code == 200
    body = response.json()
    # Del 06/11 al 12/11 de CDMX, con hoy parcial; la anterior, del 30/10 al 05/11 entera.
    assert body["periodo"] == {"desde": "2026-11-06", "hasta": "2026-11-12", "dias": 7, "parcial": True,
                               "since": "2026-11-06T06:00:00Z", "until": "2026-11-12T18:00:00Z"}
    # La anterior se corta a la misma hora que hoy: 6 dias y 12 horas desde su comienzo.
    assert body["anterior"]["periodo"] == {"desde": "2026-10-30", "hasta": "2026-11-05", "dias": 7,
                                           "parcial": True, "since": "2026-10-30T06:00:00Z",
                                           "until": "2026-11-05T18:00:00Z"}
    # Dos pedidos a la base, uno por ventana, con la misma vara de /resultados.
    assert [(m, p) for m, p, _ in reader.calls] == [("POST", results_read.RPC)] * 2
    assert reader.calls[0][2] == results_read.rpc_payload(
        datetime(2026, 11, 6, 6, tzinfo=UTC), NOW, tz=TZ, test_prefixes=())
    assert reader.calls[1][2]["p_desde"] == "2026-10-30T06:00:00Z"
    assert reader.calls[1][2]["p_hasta"] == "2026-11-05T18:00:00Z"
    assert "Los 7 días anteriores (del 30/10 al 05/11, hasta la misma hora): " \
        "1 venta recuperada, USD 39,08." in body["lineas"]


def test_it_answers_the_numbers_short_and_ready_to_say(monkeypatch: pytest.MonkeyPatch) -> None:
    # La venta del fixture es del 12/11 (CDMX); la anterior contesta sin ventas.
    reader = FakeReader(values=[_rpc(), _sin_ventas()])
    client = _client(monkeypatch, reader, prefixes="f9fb51bc,3de94ff9")
    response = client.get(ROUTE, headers=AUTH, params={"desde": "2026-11-02", "hasta": "2026-11-12"})
    assert response.status_code == 200
    body = response.json()
    assert body["contract"] == "automation-expert-results-v1"
    # El plugin de lectura del AE acepta solo el major 1 (tools.py, CONTRACT_MAJOR).
    assert body["contract_version"].split(".", 1)[0] == "1"
    assert (body["fuente"], body["regla"], body["instancia"], body["zona"]) == \
        ("resultados/v1", "venta_recuperada_v1", "lancemos", TZ)
    assert body["numeros"] == {
        "ventas_recuperadas": 1, "ventas_directas": 0, "ventas_atribuibles": 1,
        "neto_productor_usd": 39.08, "neto_directas_usd": 0.0, "neto_atribuibles_usd": 39.08,
        "compras_aprobadas": 2, "envios_aceptados": 1, "costo_meta_usd": None,
    }
    assert body["por_flujo"] == [{"flujo": "bienvenida", "ejecutor": "motor", "envios_aceptados": 1,
                                  "ventas": 1, "neto_usd": 39.08}]
    assert body["flujos_omitidos"] == 0
    assert body["ventas_sin_flujo"] == 0
    assert body["anterior"]["periodo"]["desde"] == "2026-10-22"
    assert body["anterior"]["numeros"]["ventas_recuperadas"] == 0
    assert body["anterior"]["numeros"]["envios_aceptados"] == 0
    assert body["gaps"] == ["conversaciones", "costo_modelo", "costo_meta"]
    assert body["lineas"] == [
        "Del 02/11 al 12/11: 1 venta recuperada, USD 39,08 de comisión del productor.",
        "Por un link nuestro: 0 (USD 0,00). Atribuibles por el caso o por la persona: 1 (USD 39,08).",
        "Mensajes de los flujos aceptados por WhatsApp: 1. Compras aprobadas en la ventana, de todo origen: 2.",
        "Los 11 días anteriores (del 22/10 al 01/11): 0 ventas recuperadas, USD 0,00.",
        "Esto no lo mido acá: las conversaciones, el costo del modelo y el costo de Meta.",
    ]
    # Las pruebas declaradas viajan en los dos pedidos.
    assert all(call[2]["p_pruebas"] == ["f9fb51bc", "3de94ff9"] for call in reader.calls)


def test_no_single_sale_comes_out() -> None:
    raw = _rpc()
    raw["ventas"][0]["email"] = "ana@example.test"
    text = _registered(FakeReader(value=raw)).get(ROUTE).text
    venta = raw["ventas"][0]
    for valor in ("example.test", venta["venta"], "CASHPAYMENT", "893.25", "MXN", '"MX"'):
        assert valor not in text


def test_a_bad_window_is_422_and_does_not_reach_the_base(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader()
    client = _client(monkeypatch, reader)
    for params, code in (({"dias": "0"}, "dias_invalid"), ({"dias": "91"}, "dias_invalid"),
                         ({"desde": "2026-07-01", "hasta": "2026-09-29"}, "window_too_long"),
                         ({"dias": "siete"}, "dias_invalid"), ({"dias": "-3"}, "dias_invalid"),
                         ({"desde": "2026-11-01", "dias": "7"}, "window_conflict"),
                         ({"desde": "01/11/2026"}, "desde_invalid"), ({"hasta": "2026-13-01"}, "hasta_invalid"),
                         ({"desde": "2026-11-10", "hasta": "2026-11-01"}, "window_empty"),
                         ({"desde": "2026-01-01", "hasta": "2026-11-01"}, "window_too_long")):
        response = client.get(ROUTE, headers=AUTH, params=params)
        assert response.status_code == 422, params
        assert response.json()["detail"] == code
    assert reader.calls == []


def test_the_edges_of_the_window_are_accepted() -> None:
    actual, _ = results_summary.parse_summary_window(None, None, "90", tz=TZ, now=NOW)
    assert actual.dias == 90
    actual, _ = results_summary.parse_summary_window("2026-07-01", "2026-09-28", None, tz=TZ, now=NOW)
    assert actual.dias == 90


def test_a_hasta_in_the_future_is_cut_to_today() -> None:
    actual, anterior = results_summary.parse_summary_window("2026-11-01", "2026-11-30", None, tz=TZ, now=NOW)
    assert (actual.ultimo, actual.dias, actual.parcial) == (date(2026, 11, 12), 12, True)
    assert (anterior.primero, anterior.ultimo) == (date(2026, 10, 20), date(2026, 10, 31))


def test_one_day_is_said_in_singular() -> None:
    body = _registered(FakeReader(values=[_rpc(), _sin_ventas()])).get(ROUTE, params={"dias": "1"}).json()
    assert body["lineas"][0].startswith("El 12/11 (hoy va parcial): 1 venta recuperada")
    assert "El día anterior (el 11/11, hasta la misma hora): 0 ventas recuperadas, USD 0,00." in body["lineas"]


def test_a_window_of_another_year_says_the_year() -> None:
    body = _registered(FakeReader()).get(ROUTE, params={"desde": "2025-12-28", "hasta": "2026-01-03"}).json()
    assert body["lineas"][0].startswith("Del 28/12/2025 al 03/01: ")


def test_a_sale_without_its_flow_is_counted_apart() -> None:
    raw = _rpc()
    raw["flujos"] = []
    body = _registered(FakeReader(value=raw)).get(ROUTE).json()
    assert body["ventas_sin_flujo"] == 1 and body["por_flujo"] == []
    assert "1 venta no queda en ningún flujo de la lista." in body["lineas"]


def test_a_full_quota_is_busy_and_does_not_reach_the_base() -> None:
    from fastapi import FastAPI, HTTPException

    reader = FakeReader()
    app = FastAPI()
    slots = asyncio.Semaphore(1)

    def busy() -> None:  # el mismo chequeo que automation_expert_busy de app.py
        if slots.locked():
            raise HTTPException(status_code=503, detail="automation_expert_busy")

    asyncio.run(slots.acquire())
    results_read.register(
        app, supabase=reader, instance_ref="att1", time_zone=TZ, require_token=lambda authorization: None,
        busy=busy, slots=slots, failure=lambda exc: RuntimeError(),  # type: ignore[arg-type,return-value]
        meta_access_token=None, meta_waba_id=None,
        env={results_read.ENV_ENABLED: "true", results_summary.ENV_ENABLED: "true"}, now=lambda: NOW)
    response = TestClient(app).get(ROUTE)
    assert response.status_code == 503 and response.json()["detail"] == "automation_expert_busy"
    assert reader.calls == []


def test_a_window_in_the_future_is_empty() -> None:
    with pytest.raises(results_read.ResultsRejected, match="window_empty"):
        results_summary.parse_summary_window("2026-11-20", None, None, tz=TZ, now=NOW)


def test_dias_with_hasta_counts_back_from_hasta() -> None:
    actual, anterior = results_summary.parse_summary_window(None, "2026-11-10", "3", tz=TZ, now=NOW)
    assert (actual.primero, actual.ultimo, actual.parcial) == (date(2026, 11, 8), date(2026, 11, 10), False)
    assert (anterior.primero, anterior.ultimo) == (date(2026, 11, 5), date(2026, 11, 7))


def test_a_base_failure_is_503_without_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader(fail_from=1)
    response = _client(monkeypatch, reader).get(ROUTE, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["detail"] == "automation_expert_read_unavailable"
    assert "secreto" not in response.text


def test_without_the_previous_window_the_summary_still_comes_out(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader(fail_from=2)
    response = _client(monkeypatch, reader).get(ROUTE, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["anterior"] is None
    assert body["gaps"][-1] == "periodo_anterior_no_leido"
    assert "No pude leer los días anteriores para comparar." in body["lineas"]
    assert "secreto" not in response.text


# --------------------------------------------------------------- Meta


def test_with_meta_the_cost_is_a_line_and_not_a_gap() -> None:
    captured = _pricing()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=captured["response"])

    pricing = results_read.MetaPricing(access_token="t" * 30, waba_id="100000000000001",
                                       transport=httpx.MockTransport(handler))
    body = _registered(FakeReader(), meta=pricing).get(
        ROUTE, params={"desde": "2026-11-09", "hasta": "2026-11-11"}).json()
    costo = round(0.689 + 0.322 + 0.5756, 2)
    assert body["numeros"]["costo_meta_usd"] == costo
    assert costo == 1.59
    assert "Costo de los mensajes de WhatsApp (Meta): USD 1,59." in body["lineas"]
    assert body["gaps"] == ["conversaciones", "costo_modelo"]
    # Meta se pide una vez, solo para la ventana actual.
    assert len(seen) == 1
    assert body["lineas"][-1] == "Esto no lo mido acá: las conversaciones y el costo del modelo."


def test_a_meta_failure_leaves_the_rest_of_the_summary() -> None:
    pricing = results_read.MetaPricing(
        access_token="t" * 30, waba_id="100000000000001",
        transport=httpx.MockTransport(lambda request: httpx.Response(500, json={"error": {"message": "x"}})))
    body = _registered(FakeReader(), meta=pricing).get(ROUTE).json()
    assert body["numeros"]["ventas_recuperadas"] == 1
    assert body["numeros"]["costo_meta_usd"] is None and "costo_meta" in body["gaps"]


# --------------------------------------------------------------- el armado


def _flujo(nombre: str, ejecutor: str, envios: int, directas: int, atribuibles: int) -> dict[str, object]:
    return {"flujo": nombre, "ejecutor": ejecutor, "envios": envios, "ventas_directas": directas,
            "neto_directas_usd": 10.0 * directas, "ventas_atribuibles": atribuibles,
            "neto_atribuibles_usd": 5.0 * atribuibles}


def test_the_flows_go_by_sales_and_nothing_is_cut_in_silence() -> None:
    flujos = [_flujo(f"f{i:02d}", "motor", envios=i, directas=0, atribuibles=0) for i in range(11)]
    flujos.append(_flujo("carrito", "legado", envios=3, directas=1, atribuibles=2))
    body = {"version": "resultados/v1", "regla": "venta_recuperada_v1", "resumen": [], "flujos": flujos,
            "faltan": []}
    actual, anterior = results_summary.parse_summary_window(None, None, None, tz=TZ, now=NOW)
    resumen = results_summary.build_summary(body, body, actual=actual, anterior=anterior, instancia="att1", zona=TZ)
    assert resumen["por_flujo"][0] == {"flujo": "carrito", "ejecutor": "legado", "envios_aceptados": 3,
                                       "ventas": 3, "neto_usd": 20.0}
    # Despues, por envios: f10, f09, ...
    assert [f["flujo"] for f in resumen["por_flujo"][1:3]] == ["f10", "f09"]
    assert len(resumen["por_flujo"]) == results_summary.MAX_FLUJOS
    assert resumen["flujos_omitidos"] == 2
    assert resumen["numeros"]["envios_aceptados"] == sum(range(11)) + 3


def test_the_money_is_written_the_way_it_is_said() -> None:
    assert results_summary.usd(1234.5) == "USD 1.234,50"
    assert results_summary.usd(0) == "USD 0,00"
    assert results_summary.usd(1234567.891) == "USD 1.234.567,89"
