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
    html, avisos, errores = DIBUJO.armar_dibujo(_capture(), None)
    assert errores == []
    assert html is not None and html.lstrip().lower().startswith("<!doctype html")
    datos = _datos(html)
    assert [f["id"] for f in datos["flujos"]] == ["precheckout", "carrito", "pago_fallido", "reactivacion", "descuento"]
    carrito = datos["flujos"][1]["versiones"]["hoy"]
    assert carrito["estado"] == "vigente"
    plantillas = [n["titulo"] for n in carrito["nodos"] if n["tipo"] == "plantilla"]
    assert plantillas == ["Mensaje por WhatsApp"]
    disparador = next(n for n in carrito["nodos"] if n["id"] == "dispara")
    assert disparador["metrica"] == "245 avisos, 197 entraron al flujo, 197 mensajes enviados en los últimos 7 días"
    # Cada nodo que dice que corre lleva la lectura como evidencia, sin nombres de adentro.
    assert all(n["evidencia"][0]["ref"] == "Datos del sistema" for n in carrito["nodos"])
    # Lo apagado se ve apagado, y un hueco de la lectura deja su frase en el resumen de su flujo.
    reactivacion = datos["flujos"][3]
    assert reactivacion["versiones"]["hoy"]["estado"] == "sin_activar"
    assert "puede no mostrar todos los detalles" in reactivacion["resumen"]
    assert "puede no mostrar" not in carrito.get("resumen", "") and "puede no mostrar" not in datos["flujos"][1]["resumen"]
    # Los textos de las plantillas no vienen en la lectura: es aviso, no error.
    assert all("sin texto de plantilla" in a for a in avisos)


def test_a_phone_in_the_reading_leaves_no_drawing() -> None:
    capture = _capture()
    capture["data"]["flows"][0]["definition"]["nombre"] = "Carrito de 55 1234 5678"
    html, _, errores = DIBUJO.armar_dibujo(capture, None)
    assert html is None
    assert any("teléfono" in e or "telefono" in e for e in errores)


def test_a_flow_that_is_on_without_a_reading_sha_still_says_where_it_came_from() -> None:
    capture = copy.deepcopy(_capture())
    capture["bridge_git_sha"] = None
    html, _, errores = DIBUJO.armar_dibujo(capture, None)
    assert errores == [] and html is not None
    nodo = _datos(html)["flujos"][0]["versiones"]["hoy"]["nodos"][0]
    assert nodo["evidencia"][0]["ref"] == "Datos del sistema"


def test_with_a_flow_key_the_drawing_brings_only_that_flow() -> None:
    html, _, errores = DIBUJO.armar_dibujo(_capture(), "carrito")
    assert errores == [] and html is not None
    datos = _datos(html)
    assert [f["id"] for f in datos["flujos"]] == ["carrito"]
    assert datos["titulo"] == "Carrito abandonado: cómo funciona hoy"
    # Las prioridades nombran a los otros flujos por lo que hacen, aunque no estén en el dibujo.
    disparador = datos["flujos"][0]["versiones"]["hoy"]["nodos"][0]
    assert ["Le gana a", "Primer contacto tras el formulario"] in disparador["datos"]
    assert ["No arranca mientras", "Pago fallido"] in disparador["datos"]


# Lo que se vio en la captura real del 09/11 y lo que el SOUL no deja decir. Se busca en lo que muestra
# la página: los `id` de un flujo (carrito_nativo) no se ven, y solo van en atributos.
_PALABRAS_DE_ADENTRO = (
    "PURCHASE_", "Chatwoot", "bridge", "legado", "scope", "bolsa", "esquema", "manifiesto", "cohorte", "motor",
    "nativo", "claim", "uuid", "setter-flujo", "Automation Expert", "maribel-recuperacion", "tres-toques",
    "un solo caso", "va en null", "att1_", "es_MX", "billet_url", "America/", "Escrito en la definición",
    "Desde el código", "rama «", "None", "sin-sha", "bridge@",
)


def _lo_que_se_ve(valor: object, clave: str = "") -> list[str]:
    if clave in ("id", "de", "a"):
        return []
    if isinstance(valor, str):
        return [valor]
    if isinstance(valor, dict):
        return [t for k, v in valor.items() for t in _lo_que_se_ve(v, k)]
    if isinstance(valor, list):
        return [t for v in valor for t in _lo_que_se_ve(v)]
    return []


def _lectura_con_la_forma_de_maribel() -> dict[str, Any]:
    """La captura real de ATT1, con lo de la captura de Maribel del 09/11 y los huecos reales del bridge."""
    from bridge.automation_expert_read import EXECUTOR_GAPS

    capture = _capture()
    flows = capture["data"]["flows"]
    carrito = next(f for f in flows if f["flow_key"] == "carrito")
    carrito["version"] = {"scope": "maribel-recuperacion v4", "policy": "maribel-recuperacion-un-toque v1"}
    nativo = copy.deepcopy(carrito)
    nativo["flow_key"] = "carrito_nativo"
    nativo["definition"]["flujo"] = "carrito_nativo"
    nativo["definition"]["pasos"] = [
        {"esperar": "1h"},
        {"plantilla": "att1_carrito_abandonado_01", "idioma": "es_MX", "variables": ["nombre", "producto"]},
        {"condicion": {"dato": "ventana", "es": ["abierta"]}, "entonces": [{"conversar": "ayudarla con el pago"}], "si_no": []},
        {"etiquetar": "cliente"},
    ]
    nativo["definition"]["entra"] = {"permiso": "conversacion", "contesto_a": ["carrito", "pago_fallido"]}
    nativo["definition"]["topes"]["bolsa"] = "maribel-recuperacion"
    flows.append(nativo)
    capture["gaps"] = capture["gaps"] + [
        {"code": codigo, "scope": "flow:carrito", "what": what, "why": why}
        for codigo, (what, why) in EXECUTOR_GAPS.items()
    ]
    return capture


@pytest.mark.parametrize("solo", ["carrito", "carrito_nativo", None])
def test_the_drawing_speaks_the_language_of_the_owner(solo: str | None) -> None:
    html, _, errores = DIBUJO.armar_dibujo(_lectura_con_la_forma_de_maribel(), solo)
    assert errores == [] and html is not None
    datos = _datos(html)
    textos = " | ".join(_lo_que_se_ve(datos))
    for palabra in _PALABRAS_DE_ADENTRO:
        assert palabra not in textos, palabra
    assert "versión" not in textos and "v4" not in textos  # la versión de un flujo no se muestra
    if solo == "carrito":
        assert textos.count("puede no mostrar todos los detalles") == 1
        assert "Datos del sistema" in textos
    # La plantilla del lienzo tampoco las trae.
    fijo = html.replace(re.search(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S).group(1), "")
    for palabra in _PALABRAS_DE_ADENTRO:
        assert palabra not in fijo, palabra
    if solo == "carrito_nativo":
        nodos = datos["flujos"][0]["versiones"]["hoy"]["nodos"]
        assert any(n["titulo"] == "¿Escribió en las últimas 24 horas?" for n in nodos)
        assert any(n["titulo"] == "Le pone la etiqueta «cliente»" for n in nodos)
        disparador = nodos[0]
        assert ["Contestó a", "Carrito abandonado, Pago fallido"] in disparador["datos"]
        assert ["Topes", "240 por día · 5000 en total"] in disparador["datos"]
        assert ["Una vez por", "persona y producto · puede repetirse enseguida"] in disparador["datos"]


def test_what_the_reading_does_not_know_is_said_and_not_printed_as_none() -> None:
    # La forma real de un flujo del sistema anterior con el hueco legacy_scope_runtime_only.
    capture = _capture()
    carrito = next(f for f in capture["data"]["flows"] if f["flow_key"] == "carrito")
    carrito["definition"]["topes"] = {
        "bolsa": "att1-recuperacion", "por_dia": None, "total": None,
        "repeticion": {"una_por": "persona_y_oferta", "otra_despues_de": None},
    }
    carrito["definition"]["horario"] = None
    carrito["definition"]["termina"]["vence"] = None
    html, _, errores = DIBUJO.armar_dibujo(capture, "carrito")
    assert errores == [] and html is not None
    datos = _datos(html)
    assert "None" not in " | ".join(_lo_que_se_ve(datos))
    nodos = datos["flujos"][0]["versiones"]["hoy"]["nodos"]
    assert ["Topes", "sin confirmar"] in nodos[0]["datos"]
    assert ["Una vez por", "persona y producto · repetición sin confirmar"] in nodos[0]["datos"]
    assert next(n for n in nodos if n["id"] == "fin")["sub"].startswith("plazo sin confirmar")


def test_a_flow_in_the_test_group_says_so() -> None:
    capture = _capture()
    carrito = next(f for f in capture["data"]["flows"] if f["flow_key"] == "carrito")
    carrito["status"]["state"] = "cohort"
    html, _, errores = DIBUJO.armar_dibujo(capture, "carrito")
    assert errores == [] and html is not None
    flujo = _datos(html)["flujos"][0]
    assert flujo["resumen"].startswith("Hoy está en el grupo de prueba")
    assert flujo["versiones"]["hoy"]["estado"] == "sin_activar"


def test_a_flow_whose_definition_was_not_read_does_not_claim_it_writes_to_nobody() -> None:
    capture = _capture()
    next(f for f in capture["data"]["flows"] if f["flow_key"] == "carrito")["definition"] = None
    html, _, errores = DIBUJO.armar_dibujo(capture, "carrito")
    assert errores == [] and html is not None
    assert _datos(html)["flujos"][0]["versiones"]["hoy"]["vacio"] == "El detalle de este flujo no se pudo leer."


def test_the_page_has_no_empty_rules_panel_and_no_voseo() -> None:
    html, _, _ = DIBUJO.armar_dibujo(_capture(), "carrito")
    assert html is not None
    assert '<div class="solo-ancho" style="display:none">\n        <h2>Reglas de todos los flujos</h2>' in html
    assert 'id="lista-reglas"' in html  # el script de la pagina la escribe
    assert "fuentes" not in _datos(html)
    assert "Arrastrá" not in html and "tocá" not in html


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
    assert "pago_fallido" not in path.name  # Slack muestra el nombre del archivo
    assert [f["id"] for f in _datos(path.read_text(encoding="utf-8"))["flujos"]] == ["pago_fallido"]
    assert result["flows"] == ["pago_fallido"]
    assert result["read_at"] == _capture()["database_at"]
    assert result["gaps"] == []  # los huecos de otros flujos no son de este dibujo


def test_draw_flow_with_a_key_keeps_the_gaps_of_that_flow(bridge: type[_Bridge]) -> None:
    result = json.loads(TOOLS.draw_flow({"flow_key": "reactivacion"}))
    assert [g["scope"] for g in result["gaps"]] == ["flow:reactivacion"]


def test_draw_flow_without_a_key_keeps_every_flow_and_every_gap(bridge: type[_Bridge]) -> None:
    result = json.loads(TOOLS.draw_flow({}))
    assert result["flows"] == ["precheckout", "carrito", "pago_fallido", "reactivacion", "descuento"]
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


def test_the_payment_slip_of_the_legacy_draws_with_its_ficha_trigger() -> None:
    """1.3.0 (E-208): la ficha del legado entra a /flows; el dibujo la arma sin errores."""
    import bridge.automation_expert_read as reader

    payload = _capture()
    settings = reader.FlowSettings(
        manifest_flows={"ficha": True}, templates={
            "ficha": ("att1_ficha_efectivo_01", "es_MX", ("nombre", "tienda", "producto", "vencimiento"))},
        time_zone="America/Mexico_City", precheckout_enabled=False, pilot_enabled=True, recovery_enabled=True,
        payment_failure_enabled=True, coupon_enabled=False, coupon_template=None, coupon_code=None,
        coupon_product_name=None, coupon_min_age_seconds=86_400, coupon_max_age_seconds=259_200,
        coupon_send_hours=None, coupon_test_phone_set=False, reactivation_enabled=False, reactivation_template=None,
        reactivation_min_age_seconds=86_400, reactivation_max_age_seconds=2_592_000, stalled_monitor_enabled=False,
        resume_enabled=False, precheckout_test_phone_set=False, allowed_jid_set=False, payment_slip_enabled=True,
        payment_slip_scope=reader.ScopeRef("att1-ficha", 1))
    data, gaps = reader.apply_executors(payload["data"], settings=settings,
                                        scope_reads={"ficha": {"configured": True, "runtime_state": "armed"}},
                                        engine=None)
    payload = {**payload, "data": data, "gaps": [*payload.get("gaps", []), *gaps]}
    html, avisos, errores = DIBUJO.armar_dibujo(payload, "ficha")
    assert errores == [] and html is not None, errores
    flujos = {flujo["id"]: flujo for flujo in _datos(html)["flujos"]}
    assert "ficha" in flujos
    assert "Hoy está prendido" in flujos["ficha"]["resumen"]
