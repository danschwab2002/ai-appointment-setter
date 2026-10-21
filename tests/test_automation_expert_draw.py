"""El dibujo de los flujos del Automation Expert (draw_flow, automation-expert-read-v1).

La lectura es la de verdad: tests/fixtures/automation_expert_draw/att1_flows_20261021.json es la
respuesta de GET /internal/automation-expert/v1/flows del bridge de ATT1 (setter-bridge:v1.15.5),
pedida desde el contenedor de Hermes de setter-att1 el 2026-10-21 a las 20:11Z. No tiene datos
personales: solo definiciones, estados y conteos. El cliente se prueba contra un bridge falso de
verdad, un servidor HTTP local que devuelve esa captura.
"""

from __future__ import annotations

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import re
import sys
import threading
from types import ModuleType, SimpleNamespace
from typing import Any, Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "profiles" / "automation-expert" / "plugins" / "automation-expert-read"
FIXTURE = ROOT / "tests" / "fixtures" / "automation_expert_draw" / "att1_flows_20261021.json"
TOKEN = "e" * 40
FLOWS = "/internal/automation-expert/v1/flows"


def _load_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "automation_expert_read_plugin_draw", PLUGIN / "__init__.py",
        submodule_search_locations=[str(PLUGIN)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PLUGIN_MODULE = _load_plugin()
TOOLS = sys.modules[PLUGIN_MODULE.__name__ + ".tools"]
DIBUJO = importlib.import_module(PLUGIN_MODULE.__name__ + ".dibujo")


def _capture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _datos(html: str) -> dict[str, Any]:
    match = re.search(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S)
    assert match is not None
    return json.loads(match.group(1).replace("<\\/", "</"))


# ------------------------------------------------------------ el armado


def test_the_real_reading_draws_every_flow_with_its_state_and_numbers() -> None:
    html, avisos, errores = DIBUJO.armar_dibujo(_capture(), "carrito")
    assert errores == []
    assert html is not None and html.lstrip().lower().startswith("<!doctype html")
    datos = _datos(html)
    assert [f["id"] for f in datos["flujos"]] == ["carrito", "precheckout", "pago_fallido", "reactivacion", "descuento"]
    carrito = datos["flujos"][0]["versiones"]["hoy"]
    assert carrito["estado"] == "vigente"
    plantillas = [n["titulo"] for n in carrito["nodos"] if n["tipo"] == "plantilla"]
    assert plantillas == ["att1_carrito_abandonado_01"]
    disparador = next(n for n in carrito["nodos"] if n["id"] == "dispara")
    assert disparador["metrica"] == "245 disparos, 197 arranques, 197 aceptados por Chatwoot en 7 días"
    # Cada nodo que dice que corre lleva la lectura como evidencia.
    assert all(n["evidencia"][0]["ref"].startswith("bridge@b163669f57bf:") for n in carrito["nodos"])
    # La política que nombra la base queda a la vista, aunque la definición sea de un toque.
    assert "att1-carrito-tres-toques v1" in datos["flujos"][0]["resumen"]
    # Lo apagado se ve apagado, y un hueco de la lectura va al resumen de su flujo.
    reactivacion = datos["flujos"][3]
    assert reactivacion["versiones"]["hoy"]["estado"] == "sin_activar"
    assert "No se sabe desde cuándo vale lo que vale" in reactivacion["resumen"]
    # Los textos de las plantillas no vienen en la lectura: es aviso, no error.
    assert all("sin texto de plantilla" in a for a in avisos)


def test_a_phone_in_the_reading_leaves_no_drawing() -> None:
    capture = _capture()
    capture["data"]["flows"][0]["definition"]["nombre"] = "Carrito de 55 1234 5678"
    html, _, errores = DIBUJO.armar_dibujo(capture, None)
    assert html is None
    assert any("teléfono" in e or "telefono" in e for e in errores)


def test_a_flow_that_is_on_without_a_reading_sha_still_names_where_it_came_from() -> None:
    capture = copy.deepcopy(_capture())
    capture["bridge_git_sha"] = None
    html, _, errores = DIBUJO.armar_dibujo(capture, None)
    assert errores == [] and html is not None
    nodo = _datos(html)["flujos"][0]["versiones"]["hoy"]["nodos"][0]
    assert nodo["evidencia"][0]["ref"].startswith("bridge@sin-sha:")


# --------------------------------------------------------- la herramienta


class _Bridge(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, object]] = {}

    def log_message(self, *args: object) -> None:  # noqa: D401 - silencio
        return

    def do_GET(self) -> None:  # noqa: N802 - nombre de http.server
        status, payload = type(self).routes.get(self.path, (404, {"detail": "flow_not_found"}))
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def bridge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[type[_Bridge]]:
    _Bridge.routes = {FLOWS: (200, _capture())}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Bridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("AUTOMATION_EXPERT_API_URL", f"http://{host}")
    monkeypatch.setenv("AUTOMATION_EXPERT_INTERNAL_HOST", host)
    monkeypatch.setenv("AUTOMATION_EXPERT_API_TOKEN", TOKEN)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    try:
        yield _Bridge
    finally:
        server.shutdown()


def test_draw_flow_leaves_the_html_in_the_profile_cache_and_names_it_with_media(
    bridge: type[_Bridge], tmp_path: Path,
) -> None:
    result = json.loads(TOOLS.draw_flow({"flow_key": "pago_fallido"}))
    assert result["media"].startswith("MEDIA:")
    path = Path(result["media"].removeprefix("MEDIA:"))
    assert path.is_file() and path.suffix == ".html"
    assert path.parent == tmp_path / "cache" / "documents" / "automation-expert"
    assert _datos(path.read_text(encoding="utf-8"))["flujos"][0]["id"] == "pago_fallido"
    assert result["opened_first"] == "pago_fallido"
    assert result["read_at"] == _capture()["database_at"]
    assert [g["scope"] for g in result["gaps"]] == ["flow:reactivacion", "flow:descuento"]


def test_draw_flow_refuses_what_is_not_a_flow_before_going_out(bridge: type[_Bridge]) -> None:
    assert json.loads(TOOLS.draw_flow({"flow_key": "../etc"})) == {"error": "invalid_flow_key"}
    assert json.loads(TOOLS.draw_flow({"flow_key": "ficha"})) == {"error": "flow_not_found"}


def test_draw_flow_passes_the_bridge_error_and_writes_nothing(bridge: type[_Bridge], tmp_path: Path) -> None:
    bridge.routes = {FLOWS: (503, {"detail": "automation_expert_read_unavailable"})}
    assert json.loads(TOOLS.draw_flow({})) == {"error": "automation_expert_read_unavailable"}
    assert not (tmp_path / "cache").exists()


def test_the_plugin_registers_draw_flow_in_the_same_toolset() -> None:
    tools: list[tuple[str, str]] = []
    PLUGIN_MODULE.register(SimpleNamespace(
        profile_name="automation-expert",
        register_tool=lambda **kwargs: tools.append((kwargs["name"], kwargs["toolset"])),
        register_hook=lambda *args, **kwargs: None,
    ))
    assert ("draw_flow", "automation_expert_read") in tools
    assert {toolset for _, toolset in tools} == {"automation_expert_read"}
